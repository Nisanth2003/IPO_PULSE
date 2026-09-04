"""The store: a live Google Sheet.

There is one copy of the data and it is online. The backend reads and writes
it through the service account; the browser reads the same tabs over
Google's credential-free CSV export. No file in the repo holds IPO data, and
no publish step copies it anywhere — which is the point, because every copy
is a chance for the site to show something the backend no longer believes.

    GOOGLE_SHEETS_ID    which spreadsheet          (.env / GitHub secret)
    GOOGLE_SHEETS_KEY   service-account JSON path  (writing only)

`tables.py` owns the tab layout. This module is only I/O.

Three things worth knowing before changing it:

  * **Reads are batched, writes are whole-tab.** One `batchGet` pulls every
    tab; a save clears and rewrites them. Sheets has no transaction, so a
    save that dies midway can leave some tabs new and some old. `verify()`
    exists for that, and the previous state is always one file away in
    `backend/out/` — see `store.backup()`.

  * **There is no lock.** A file store could take one; a spreadsheet several
    machines can open cannot. Two concurrent writers means last-write-wins
    over the whole book. The scheduler's `concurrency:` group serialises CI
    runs, which is the only place two writers were ever likely.

  * **Everything needs the network now.** `list`, `doctor` and `build` used
    to work offline against a local file. They no longer can, and that is the
    price of the sheet being the only copy rather than a mirror of one.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from . import tables
from .models import Ipo

# backend/ipopulse/sheets.py -> backend/
BACKEND_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = BACKEND_ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
OUT_DIR = BACKEND_ROOT / "out"
FRONTEND_DATA = BACKEND_ROOT.parent / "frontend" / "data"

# Our own read/write ceiling, NOT Google's. `_span` bounds every range at
# this row, and `_fetch` reads through `_span` — so a row past it is not
# "extra capacity", it is **invisible**, silently, with no error anywhere.
#
# Measured 4 Sep 2026: I18n was at 1,116 rows for 28 IPOs, about 40 rows per
# IPO. At thirty new IPOs a month that tab gains ~1,200 rows a month and
# would have reached 5,000 inside four months — at which point translations
# would simply have started vanishing from the site.
#
# Raised to 50,000, which is roughly a decade of headroom on the fastest-
# growing tab. Costs nothing: Sheets returns only the populated rows in a
# range, so a taller bound does not make the read bigger. The real ceiling is
# Google's 10,000,000 cells per spreadsheet, and the whole store is currently
# using 12,034 of them — 0.12%.
MAX_ROWS = 50000


def _col_letter(n: int) -> str:
    """1 -> A, 26 -> Z, 27 -> AA."""
    out = ""
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def _span(tab: str) -> str:
    """A1-notation covering the whole tab, with room to grow.

    Not hard-coded to 'Z': the IPOs tab is 39 columns wide, so a Z-bounded
    range would have read back two-thirds of a record and, worse, cleared
    only two-thirds of it before a rewrite — leaving live cells from the
    previous save sitting past the new data.
    """
    # ALL_TABS, not TABS: the Market* tabs need a width here too, and
    # getting it wrong is the failure this docstring describes — a
    # range too narrow clears part of a row and leaves the rest live.
    width = len(tables.ALL_TABS.get(tab, [])) or 26
    return f"{tab}!A1:{_col_letter(width)}{MAX_ROWS}"


class SheetUnavailable(RuntimeError):
    """No credentials, no sheet id, or Google said no."""


def sheet_id() -> str:
    return (os.getenv("GOOGLE_SHEETS_ID") or "").strip()


def configured() -> bool:
    return bool(sheet_id())


# ── connection ─────────────────────────────────────────────────────────────

_service: Any = None


def _connect():
    """Authorised Sheets client, built once per process."""
    global _service
    if _service is not None:
        return _service

    key = (os.getenv("GOOGLE_SHEETS_KEY") or "").strip()
    if not key:
        raise SheetUnavailable(
            "GOOGLE_SHEETS_KEY is not set — the store is a Google Sheet and "
            "reaching it needs the service-account credentials.")
    if not sheet_id():
        raise SheetUnavailable("GOOGLE_SHEETS_ID is not set")

    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError as exc:                      # pragma: no cover
        raise SheetUnavailable(
            "google-api-python-client is not installed. "
            "pip install -r requirements.txt") from exc

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]

    # The variable holds EITHER the JSON itself or a path to it, because the
    # two places it comes from disagree by nature: a .env line points at a
    # file on your disk, and a CI secret can only carry the contents. Making
    # the caller normalise that is how a Windows path ended up written into a
    # file on a Linux runner and handed to a JSON parser, which then failed
    # at "line 1 column 1" — technically accurate and useless to read.
    try:
        if key.startswith("{"):
            creds = service_account.Credentials.from_service_account_info(
                json.loads(key), scopes=scopes)
        elif os.path.exists(key):
            creds = service_account.Credentials.from_service_account_file(
                key, scopes=scopes)
        else:
            raise SheetUnavailable(
                f"GOOGLE_SHEETS_KEY is neither service-account JSON nor a file "
                f"that exists ({key[:60]}...). In .env give it the path to the "
                f"key file; in GitHub secrets paste the file's CONTENTS.")
    except SheetUnavailable:
        raise
    except (ValueError, KeyError) as exc:
        # Covers malformed JSON and JSON that parses but is not a key.
        raise SheetUnavailable(
            "GOOGLE_SHEETS_KEY does not contain a usable service-account key. "
            "Paste the whole JSON file, including the outer braces.") from exc

    _service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    return _service


def _explain(exc: Exception) -> SheetUnavailable:
    """Google's 403 is uniformly unhelpful; name the actual cause."""
    status = getattr(getattr(exc, "resp", None), "status", None)
    if status == 404:
        return SheetUnavailable(f"No spreadsheet with id {sheet_id()}")
    if status == 403:
        return SheetUnavailable(
            "The service account cannot open that spreadsheet. Share the "
            "sheet with it as an Editor and try again.")
    return SheetUnavailable(f"Google Sheets refused the request: {exc}")


# ── tab plumbing ───────────────────────────────────────────────────────────

def _tab_titles(service, book_id: str | None = None) -> list[str]:
    """Tab names in a book. Defaults to the configured store.

    `book_id` exists for the migration helpers at the bottom of this module,
    which have to inspect a book that is deliberately NOT the one
    `GOOGLE_SHEETS_ID` points at.
    """
    # Retried: this is the first call every command makes, so a transient
    # 503 here took down `brief`, `list` and everything else with a raw
    # googleapiclient traceback rather than a sentence.
    meta = _with_retry(lambda: service.spreadsheets().get(
        spreadsheetId=book_id or sheet_id()).execute())
    return [s["properties"]["title"] for s in meta.get("sheets", [])]


def ensure_tabs(service=None) -> list[str]:
    """Create any missing tab. Returns the ones created."""
    service = service or _connect()
    have = set(_tab_titles(service))
    missing = [name for name in tables.ALL_TABS if name not in have]
    if missing:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id(),
            body={"requests": [{"addSheet": {"properties": {"title": name}}}
                               for name in missing]}).execute()
    return missing


# ── read ───────────────────────────────────────────────────────────────────

# How long to wait before re-reading a store that came back entirely
# empty. One batchUpdate round trip is the window being stepped over.
RETRY_PAUSE = 4.0

_cache: dict[str, Any] = {"loaded": False, "records": {}}


def _fetch() -> dict[str, list[list]]:
    """Every tab in one round trip."""
    service = _connect()
    have = set(_tab_titles(service))
    wanted = [name for name in tables.TABS if name in have]
    if not wanted:
        return {}
    res = _with_retry(lambda: service.spreadsheets().values().batchGet(
        spreadsheetId=sheet_id(),
        ranges=[_span(name) for name in wanted],
        valueRenderOption="UNFORMATTED_VALUE",
    ).execute())
    out = {name: block.get("values", [])
           for name, block in zip(wanted, res.get("valueRanges", []))}

    # Every tab empty at once is not a state this store can legitimately be
    # in — it always holds at least a header row. What it actually means is
    # that `write_records` is between its batchClear and its batchUpdate, and
    # the reader landed in the gap.
    #
    # Found the hard way: a monitor run during a chain's enrich loop read zero
    # IPOs, reported the entire store as vanished, and wrote a fingerprint of
    # nothing — which would have made the NEXT run report 131 phantom new rows.
    # A reader that cannot tell "mid-write" from "wiped" turns a routine
    # overlap into a false alarm, and a caller that then WRITES what it read
    # would turn it into real data loss.
    #
    # One retry, because the window is a single API round trip wide. If it is
    # still empty, that is a genuinely empty spreadsheet and the caller should
    # see it.
    if wanted and all(not rows for rows in out.values()):
        time.sleep(RETRY_PAUSE)
        try:
            res = service.spreadsheets().values().batchGet(
                spreadsheetId=sheet_id(),
                ranges=[_span(name) for name in wanted],
                valueRenderOption="UNFORMATTED_VALUE",
            ).execute()
        except Exception as exc:
            raise _explain(exc) from exc
        out = {name: block.get("values", [])
               for name, block in zip(wanted, res.get("valueRanges", []))}
    return out


def records(force: bool = False) -> dict[str, dict]:
    """Parsed sheet, held for the process.

    cli.py calls load() dozens of times in a run and each one would otherwise
    be a network round trip.
    """
    if force or not _cache["loaded"]:
        _cache.update(loaded=True, records=tables.from_tables(_fetch()))
    return _cache["records"]


def invalidate() -> None:
    _cache.update(loaded=False, records={})


# ── write ──────────────────────────────────────────────────────────────────

# How much of the store a single write is allowed to delete.
#
# 3 Sep 2026: the sheet went from 28 IPOs to 11, and from 1,116 I18n rows to
# zero, in one write. Every translation and every analysis bullet, gone. The
# cause was a `records()` that came back short — a partial read, or a read
# taken while another process was mid-write — followed by an `upsert` that
# faithfully wrote that short view back over everything.
#
# The mechanism does not actually matter, and that is the point of this
# guard. `write_records` replaces the whole book from one in-memory dict, so
# ANY path that produces a thin dict destroys the store, and no amount of
# care at the call sites closes that off — there will always be one more
# path. A floor under the write closes all of them at once.
#
# Two thirds, not 100%: the store legitimately shrinks. `remove` drops a row,
# `dedupe` folds two into one. What it does not do is lose more than a third
# of itself in one go, and anything that wants to is a bug or a human being
# very sure, and both should have to say so.
MIN_KEEP_FRACTION = 0.67


def _clearable(grid: dict[str, list[list]]) -> dict[str, list[list]]:
    """`None` -> `""` on the way to the API, so a blank actually blanks.

    The two layers disagree about how to say "nothing here" and the mismatch
    silently ate writes. `tables.py` expresses absence as Python `None` —
    correct, and what `_opt_num_cell` returns for a zero that means "nobody
    supplied this". But **Sheets reads `null` in a values array as "leave
    this cell as it is"**, not as "clear it". An empty string is what clears.

    So a field could be filled but never emptied. Veegaland's phantom
    `ofs_cr` of 93 crore was written as `None`, the API skipped the cell, and
    the 93 survived a repair that reported itself as having worked — `facts`
    printed `ofs_cr: 93.0 -> 0.0` and the sheet did not change.

    Done here rather than in `tables.py` on purpose: `None` is the right
    in-memory word for absent, and `""` is the API's idiom for the same
    thing. This is the boundary between them.
    """
    return {name: [["" if cell is None else cell for cell in row]
                   for row in rows]
            for name, rows in grid.items()}


def _trim(service, book_id: str, grid: dict[str, list[list]]) -> None:
    """Clear whatever sits BELOW the rows just written, per tab.

    Open-ended ranges (`I18n!A1118:E`, no end row) rather than a fixed
    `MAX_ROWS` bound. A tab's grid is only as tall as it needs to be — the
    I18n tab was 1,117 rows — and asking Sheets to clear `A1118:E5000` on it
    is a **400 Invalid range**, not a no-op. Found on the restore: the data
    landed and then the tidy-up failed, which is the harmless half of the
    ordering but still an error in the log.

    Best effort. A stale tail past the new data is untidy; failing the whole
    write over it would undo the point of writing first.
    """
    stale = [f"{name}!A{len(rows) + 1}:"
             f"{_col_letter(len(tables.ALL_TABS.get(name, [])) or 26)}"
             for name, rows in grid.items() if rows]
    if not stale:
        return
    try:
        service.spreadsheets().values().batchClear(
            spreadsheetId=book_id, body={"ranges": stale}).execute()
        return
    except Exception as exc:
        # A batchClear is ALL OR NOTHING. One invalid range fails the whole
        # call, so a single tab whose data already fills its grid ("exceeds
        # grid limits") silently prevented every other tab from being
        # trimmed. That is how a deleted row survived: the write put 29
        # records down, the tail was never cleared, and the 30th row was
        # still there to be read back.
        #
        # So fall back to clearing one range at a time. Now a tab with
        # nothing to trim costs one ignored error instead of cancelling the
        # work for all the others.
        if "exceeds grid limits" not in str(exc):
            print(f"  · batch trim failed ({str(exc)[:90]}); "
                  f"trimming tab by tab", flush=True)

    for one in stale:
        try:
            service.spreadsheets().values().batchClear(
                spreadsheetId=book_id, body={"ranges": [one]}).execute()
        except Exception as exc:
            if "exceeds grid limits" in str(exc):
                continue          # nothing below the data; genuinely fine
            print(f"  · could not trim {one.split('!')[0]} "
                  f"({str(exc)[:70]})", flush=True)


def _is_transient(exc: Exception) -> bool:
    """Worth waiting out rather than failing on.

    Two kinds. **Rate limits** (429) are our own doing and clear within the
    minute. **503 / UNAVAILABLE** is Google's, and it happens: two `brief`
    runs in a row died on a 503 from `spreadsheets.get`, which is not a
    condition any caller can fix by being told about it.

    Everything else — a bad range, a revoked key, a missing spreadsheet —
    would fail identically five times, so it is raised at once.
    """
    text = str(exc)
    status = getattr(getattr(exc, "resp", None), "status", None)
    return (status in (429, 500, 502, 503, 504)
            or "RATE_LIMIT_EXCEEDED" in text or "429" in text
            or "Quota exceeded" in text
            or "currently unavailable" in text or "UNAVAILABLE" in text)


# Kept as an alias: the write path reads better saying "rate limit".
_is_rate_limit = _is_transient


def _with_retry(fn, tries: int = 5):
    """Run `fn`, waiting out the Sheets write quota rather than failing.

    Sheets allows **60 write requests per minute per user**, and this project
    writes far more than it looks like: `store.save()` replaces the whole
    book, so a loop over 30 IPOs is 30 updates plus 30 trims — 60 requests,
    exactly the ceiling. `gmp-sync` reached it on 4 Sep and the run died.
    That ceiling is not a bug to be argued with; it is arithmetic, and it
    gets closer every time an IPO is added.

    Waiting is the right response because the window is per MINUTE. Backing
    off 5, 10, 20, 40 seconds costs a slow run; giving up costs the data.
    Only rate limits are retried — a bad range or a permission problem would
    fail identically five times.
    """
    delay = 5.0
    for attempt in range(tries):
        try:
            return fn()
        except Exception as exc:
            if not _is_transient(exc) or attempt == tries - 1:
                raise _explain(exc) from exc
            why = ("write quota reached" if "Quota" in str(exc)
                   or "RATE_LIMIT" in str(exc) else "Sheets is unavailable")
            print(f"  · {why}; waiting {delay:.0f}s "
                  f"(attempt {attempt + 1} of {tries})", flush=True)
            time.sleep(delay)
            delay *= 2
    return None


def write_records(updated: dict[str, dict], force: bool = False) -> None:
    """Replace every tab with `updated`.

    Refuses a write that would delete most of the store — see
    MIN_KEEP_FRACTION. Pass `force=True` only when you have looked at what is
    about to go and are certain.
    """
    service = _connect()
    ensure_tabs(service)

    if not force:
        try:
            # Read fresh, not from `_cache`: the whole failure this guards
            # against starts with a cache holding a short read, and comparing
            # against that cache would compare the mistake to itself.
            on_sheet = tables.from_tables(_fetch())
        except Exception:
            on_sheet = {}                # cannot compare; do not block
        if on_sheet:
            floor = int(len(on_sheet) * MIN_KEEP_FRACTION)
            if len(updated) < floor:
                raise SheetUnavailable(
                    f"refusing to write: this would take the store from "
                    f"{len(on_sheet)} record(s) to {len(updated)}, deleting "
                    f"{len(on_sheet) - len(updated)}. That is more than a "
                    f"third of it and is almost certainly a short read rather "
                    f"than an intention.\n  Nothing was written. If it IS "
                    f"intended, the caller must pass force=True.")

    grid = tables.to_tables(updated)

    # ── WRITE FIRST, THEN TRIM. Never the other way round.
    #
    # 4 Sep 2026: this function emptied the entire spreadsheet. It used to
    # `batchClear` every tab and then `batchUpdate` the new values, on the
    # reasoning that a shrinking tab would otherwise keep a stale tail. Then
    # a scheduled `gmp-sync` hit the Sheets write quota (60 requests per
    # minute) **between the two calls**. The clear had succeeded. The update
    # never ran. Eight tabs, zero rows, and a live website showing nothing.
    #
    # The order is the whole bug. Writing first means:
    #
    #   * update fails  -> nothing was cleared, the sheet is exactly as it was
    #   * update works, trim fails -> a stale tail past the new data, which
    #     still parses and which the next successful write removes
    #
    # Both are survivable. "Cleared but not rewritten" is not.
    #
    # The trim only touches rows BELOW the new data, computed per tab, so it
    # can never reach a row this write just put down.
    def _attempt() -> None:
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id(),
            body={
                "valueInputOption": "RAW",   # never let Sheets reinterpret a
                                             # date string or a leading zero
                "data": [{"range": f"{name}!A1", "values": rows}
                         for name, rows in _clearable(grid).items()],
            },
        ).execute()
        _trim(service, sheet_id(), grid)

    _with_retry(_attempt)
    _cache.update(loaded=True, records=updated)


def upsert(ipo: Ipo) -> None:
    current = dict(records())
    current[ipo.slug] = ipo.to_dict()
    write_records(current)


def drop(slug: str) -> bool:
    current = dict(records())
    if slug not in current:
        return False
    del current[slug]
    write_records(current)
    return True


# ── the market briefing: a second record type, deliberately at arm's length ─
#
# Everything above is keyed by IPO slug. Reel 7's briefing is keyed by date,
# and it gets its own cache slot and its own reader and writer rather than
# being folded into `records()` / `write_records()`.
#
# The separation is not tidiness. `write_records` clears and rewrites all
# eight IPO tabs from one in-memory snapshot with no lock, so two writers
# sharing that path means either one can revert the other's whole book. These
# functions touch the four Market* tabs and nothing else — `_span` is called
# only on names from `MARKET_TABS`, so the clear range cannot reach an IPO tab
# even if this code is wrong about everything else.
#
# The cache is separate for the same reason: `invalidate()` on one record type
# must not drop the other's, and a briefing write must not leave
# `_cache["records"]` claiming to hold a store it never read.

_market_cache: dict[str, Any] = {"loaded": False, "records": {}}


def _fetch_market() -> dict[str, list[list]]:
    """The four Market* tabs in one round trip.

    Does not inherit `_fetch`'s all-tabs-empty retry, and that is correct
    rather than an omission: an empty *IPO* store is impossible and therefore
    diagnostic of a mid-write read, but an empty Market store is the honest
    state of this sheet until the first briefing is ever written. Retrying it
    would add four seconds to every run before the feature has any data.
    """
    service = _connect()
    have = set(_tab_titles(service))
    wanted = [name for name in tables.MARKET_TABS if name in have]
    if not wanted:
        return {}
    try:
        res = service.spreadsheets().values().batchGet(
            spreadsheetId=sheet_id(),
            ranges=[_span(name) for name in wanted],
            valueRenderOption="UNFORMATTED_VALUE",
        ).execute()
    except Exception as exc:
        raise _explain(exc) from exc
    return {name: block.get("values", [])
            for name, block in zip(wanted, res.get("valueRanges", []))}


def market_records(force: bool = False) -> dict[str, dict]:
    """Every stored briefing, keyed by ISO date, held for the process."""
    if force or not _market_cache["loaded"]:
        _market_cache.update(
            loaded=True, records=tables.from_market_tables(_fetch_market()))
    return _market_cache["records"]


def invalidate_market() -> None:
    _market_cache.update(loaded=False, records={})


def write_market_records(updated: dict[str, dict]) -> None:
    """Replace the four Market* tabs with `updated`. Touches nothing else."""
    service = _connect()
    ensure_tabs(service)
    grid = tables.to_market_tables(updated)

    # Belt and braces on the one mistake that would be expensive. `grid` comes
    # from `to_market_tables`, which cannot emit an IPO tab — but this function
    # is the only writer in the module whose ranges are computed from a dict
    # rather than a constant, so it states the constraint rather than trusting
    # it. An assertion here is cheaper than restoring the book from a backup.
    stray = [name for name in grid if name not in tables.MARKET_TABS]
    if stray:
        raise RuntimeError(
            f"refusing to write: {', '.join(stray)} is not a Market tab. "
            f"This writer must never clear an IPO tab.")

    # Write first, then trim the tail — the same ordering as write_records,
    # for the same reason. See the long note there: clearing before writing
    # is what emptied the whole book on 4 Sep when the quota hit between the
    # two calls.
    def _attempt() -> None:
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id(),
            body={
                "valueInputOption": "RAW",
                "data": [{"range": f"{name}!A1", "values": rows}
                         for name, rows in _clearable(grid).items()],
            },
        ).execute()
        _trim(service, sheet_id(), grid)

    _with_retry(_attempt)
    _market_cache.update(loaded=True, records=updated)


def upsert_market(day: str, record: dict) -> None:
    """Write one day's briefing, keeping every other day.

    Read-modify-write of the whole Market tab set, the same way `upsert` does
    for IPOs. It is the same trade: simple and correct against a single
    writer, last-write-wins against two. The mitigation is scheduling — this
    runs at 08:00 IST, the IPO chain at 10:00 — plus `briefing.py` re-reading
    after it writes, which is the check the IPO side learned to do the hard
    way when a scheduled job reverted an edit made while it ran.
    """
    current = dict(market_records())
    current[day] = record
    write_market_records(current)


def drop_market(day: str) -> bool:
    current = dict(market_records())
    if day not in current:
        return False
    del current[day]
    write_market_records(current)
    return True


# ── backup ─────────────────────────────────────────────────────────────────

def backup() -> Path | None:
    """Snapshot the sheet to a local .xlsx before anything destructive.

    The sheet has version history of its own, but it is per-cell and awkward
    to roll back in bulk; a single file you can open is a better undo.
    """
    from . import workbook
    data = records()
    if not data:
        return None
    dest = OUT_DIR / "ipo-pulse.prev.xlsx"
    workbook.write(dest, data)
    return dest


# ── migration: a second book, written without disturbing the first ─────────
#
# The store has grown a lot on the fly — reservation columns, the NII split,
# the exchange identifiers, four Market* tabs — and each of those arrived as
# an append to a live spreadsheet. `create_book` and `mirror_to` exist so a
# corrected layout can be built somewhere else, verified against the original,
# and adopted by changing one environment variable.
#
# Neither function reads `sheet_id()` and neither touches `_cache` or
# `_market_cache`. That is the point: the old book has to keep running while
# the new one is checked, and a migration that quietly pointed the process
# cache at the wrong spreadsheet would be indistinguishable from data loss.

def create_book(title: str) -> str:
    """Create a new spreadsheet and return its id.

    Needs only the `spreadsheets` scope this module already requests —
    `spreadsheets.create` is part of the Sheets API, not Drive, so no new
    permission has to be granted to the service account.

    The new book belongs to the SERVICE ACCOUNT, not to you. It will not
    appear in your Drive and the site cannot read it until it is shared, which
    is why `cmd_migrate` prints that as the first step rather than leaving it
    to be discovered.
    """
    service = _connect()
    body = {
        "properties": {"title": title},
        # Every tab, created up front with its header width, so the first
        # write is an update rather than a create-then-update.
        "sheets": [{"properties": {"title": name}}
                   for name in tables.ALL_TABS],
    }
    try:
        made = service.spreadsheets().create(
            body=body, fields="spreadsheetId").execute()
    except Exception as exc:
        raise _explain(exc) from exc
    return made["spreadsheetId"]


def _write_grid(book_id: str, grid: dict[str, list[list]]) -> None:
    """Write named tabs in one book, then trim. No cache, no sheet_id()."""
    service = _connect()
    have = set(_tab_titles(service, book_id))
    missing = [name for name in grid if name not in have]
    if missing:
        service.spreadsheets().batchUpdate(
            spreadsheetId=book_id,
            body={"requests": [{"addSheet": {"properties": {"title": n}}}
                               for n in missing]}).execute()

    def _attempt() -> None:
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=book_id,
            body={"valueInputOption": "RAW",
                  "data": [{"range": f"{name}!A1", "values": rows}
                           for name, rows in _clearable(grid).items()]}
        ).execute()
        _trim(service, book_id, grid)

    _with_retry(_attempt)


def mirror_to(book_id: str, records: dict[str, dict],
              market: dict[str, dict] | None = None) -> None:
    """Write the whole store into another book, in the current layout."""
    try:
        _write_grid(book_id, tables.to_tables(records))
        if market:
            _write_grid(book_id, tables.to_market_tables(market))
    except Exception as exc:
        raise _explain(exc) from exc


def read_book(book_id: str) -> tuple[dict[str, dict], dict[str, dict]]:
    """Parse another book's IPO and Market records. For verifying a mirror."""
    service = _connect()
    have = set(_tab_titles(service, book_id))
    wanted = [n for n in tables.ALL_TABS if n in have]
    if not wanted:
        return {}, {}
    try:
        res = service.spreadsheets().values().batchGet(
            spreadsheetId=book_id,
            ranges=[_span(n) for n in wanted],
            valueRenderOption="UNFORMATTED_VALUE").execute()
    except Exception as exc:
        raise _explain(exc) from exc
    grid = {name: block.get("values", [])
            for name, block in zip(wanted, res.get("valueRanges", []))}
    return tables.from_tables(grid), tables.from_market_tables(grid)
