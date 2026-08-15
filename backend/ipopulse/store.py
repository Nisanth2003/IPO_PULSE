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


def save(ipo: Ipo) -> str:
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
