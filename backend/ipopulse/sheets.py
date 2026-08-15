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

MAX_ROWS = 5000


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
    width = len(tables.TABS.get(tab, [])) or 26
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

def _tab_titles(service) -> list[str]:
    meta = service.spreadsheets().get(spreadsheetId=sheet_id()).execute()
    return [s["properties"]["title"] for s in meta.get("sheets", [])]


def ensure_tabs(service=None) -> list[str]:
    """Create any missing tab. Returns the ones created."""
    service = service or _connect()
    have = set(_tab_titles(service))
    missing = [name for name in tables.TABS if name not in have]
    if missing:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id(),
            body={"requests": [{"addSheet": {"properties": {"title": name}}}
                               for name in missing]}).execute()
    return missing


# ── read ───────────────────────────────────────────────────────────────────

_cache: dict[str, Any] = {"loaded": False, "records": {}}


def _fetch() -> dict[str, list[list]]:
    """Every tab in one round trip."""
    service = _connect()
    have = set(_tab_titles(service))
    wanted = [name for name in tables.TABS if name in have]
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

def write_records(updated: dict[str, dict]) -> None:
    """Replace every tab with `updated`."""
    service = _connect()
    ensure_tabs(service)
    grid = tables.to_tables(updated)

    try:
        # Clear first: a shrinking tab would otherwise keep its old tail, and
        # a stale row that still parses is worse than no row at all.
        service.spreadsheets().values().batchClear(
            spreadsheetId=sheet_id(),
            body={"ranges": [_span(name) for name in grid]},
        ).execute()
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id(),
            body={
                "valueInputOption": "RAW",   # never let Sheets reinterpret a
                                             # date string or a leading zero
                "data": [{"range": f"{name}!A1", "values": rows}
                         for name, rows in grid.items()],
            },
        ).execute()
    except Exception as exc:
        raise _explain(exc) from exc

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
