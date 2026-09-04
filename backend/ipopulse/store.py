"""Store front-end. The live Google Sheet behind it.

Every other module — cli, providers, doctor, ai, publish — talks to the data
through the handful of functions here and has no idea where it is kept. That
indirection is why moving the store from YAML files to a local workbook to
an online spreadsheet touched almost none of them.

    sheets.py     the store itself: tabs in your Google Sheet
    tables.py     the layout, shared with the browser's reader
    workbook.py   local .xlsx snapshots, for backups only
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import sheets
from .models import Ipo

# Re-exported so callers keep importing paths from one place.
BACKEND_ROOT = sheets.BACKEND_ROOT
DATA_DIR = sheets.DATA_DIR
CACHE_DIR = sheets.CACHE_DIR
OUT_DIR = sheets.OUT_DIR
FRONTEND_DATA = sheets.FRONTEND_DATA

SheetUnavailable = sheets.SheetUnavailable


def where() -> str:
    """Human-readable location of the store, for messages."""
    sid = sheets.sheet_id()
    return f"Google Sheet {sid[:8]}…" if sid else "no sheet configured"


def list_slugs() -> list[str]:
    return sorted(sheets.records())


def load(slug: str) -> Ipo:
    rec = sheets.records().get(slug)
    if rec is None:
        raise FileNotFoundError(f"No IPO '{slug}' in the sheet")
    return Ipo.from_dict(dict(rec))


def load_all() -> list[Ipo]:
    out = []
    for slug, rec in sorted(sheets.records().items()):
        try:
            out.append(Ipo.from_dict(dict(rec)))
        except Exception as exc:                  # keep one bad row from
            print(f"  ! skipping {slug}: {exc}")  # breaking the whole build
    return out


# ── batching ───────────────────────────────────────────────────────────────
#
# Sheets allows 60 write requests per minute per user. `save()` replaces the
# whole book, which is one update plus one trim — **two write requests per
# call**. A loop over 28 IPOs is therefore 56, and `gmp-sync` has four such
# loops. On 4 Sep it hit the ceiling mid-run, the quota error landed between
# a clear and a write, and the entire spreadsheet was left empty.
#
# The old comment here said the loops save per IPO on purpose, so "a failure
# on the tenth leaves the first nine written". That reasoning is inverted:
# saving per IPO is what *causes* the failure. One write at the end of a run
# cannot exhaust a per-minute quota, and if it fails nothing was half-applied.
#
# Inside `with store.batched():`, `save()` buffers instead of writing and one
# `write_records` happens on the way out. Outside it, `save()` behaves exactly
# as before, so nothing that has not opted in changes.

_buffer: dict[str, dict] | None = None


@contextmanager
def batched() -> Iterator[None]:
    """Collect every `save()` in this block and write once at the end.

    Re-entrant by ignoring nesting: an inner block joins the outer buffer
    rather than flushing early, so a helper that batches internally does not
    turn one write into two when called from a loop that also batches.
    """
    global _buffer
    if _buffer is not None:
        yield                          # already batching; join it
        return
    _buffer = {}
    try:
        yield
    finally:
        pending, _buffer = _buffer, None
        if pending:
            current = dict(sheets.records())
            current.update(pending)
            sheets.write_records(current)


def pending() -> int:
    """How many records are buffered. 0 when not batching."""
    return len(_buffer or {})


def save(ipo: Ipo) -> str:
    if _buffer is not None:
        _buffer[ipo.slug] = ipo.to_dict()
        return f"{where()} (buffered)"
    sheets.upsert(ipo)
    return where()


def save_all(ipos: list[Ipo]) -> str:
    """Write many IPOs in one pass.

    save() rewrites every tab per call, and each call is a network round
    trip — so a loop over every IPO is markedly slower than one batch. The
    loops in cli.py still save per IPO on purpose: a failure on the tenth
    leaves the first nine written, which is what the scheduler relies on
    when it runs with `if: always()`.
    """
    sheets.write_records({ipo.slug: ipo.to_dict() for ipo in ipos})
    return where()


def remove(slug: str) -> bool:
    """Drop an IPO from the store. False if there was nothing to drop."""
    return sheets.drop(slug)


def backup() -> Path | None:
    """Snapshot the sheet to a local file before a destructive change."""
    return sheets.backup()


def iter_ipos() -> Iterator[Ipo]:
    for slug in list_slugs():
        yield load(slug)


def scaffold(slug: str, overwrite: bool = False) -> str:
    """Add a blank row for `slug`."""
    if slug in sheets.records() and not overwrite:
        raise FileExistsError(
            f"{slug} is already in the sheet (use --force to reset it)")

    # Note `board`: nothing is set here, but Ipo.from_dict still defaults it
    # to Mainboard, which is the trap that once left four SME issues judged
    # against mainboard lot sizes. `sync --provider nse` corrects it from the
    # exchange series; until then treat the badge on a new row as unverified.
    sheets.upsert(Ipo.from_dict({
        "slug": slug,
        "financials": {"years": ["FY23", "FY24", "FY25"]},
    }))
    return where()
