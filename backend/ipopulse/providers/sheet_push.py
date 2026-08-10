"""Write IPO data back into a Google Sheet.

The mirror image of `sheet.py`. That module reads a spreadsheet leniently,
because the sheet you are handed never matches anyone's schema. This one has
the opposite problem: it decides what the schema *is*, so the one rule it must
never break is that `sheet.parse()` can read back everything written here.
The header row is built from `sheet.ALIASES` for exactly that reason — the
canonical field name is always one of its own aliases, so the round trip is
closed by construction rather than by two lists staying in sync by luck.

Writes are an upsert, not an append. A row is matched on the slug of its
company name (plus the date, for GMP), so re-running after every refresh
updates yesterday's row instead of growing a duplicate underneath it. Columns
the sheet has that IPO Pulse doesn't know about are left alone — you can add
your own notes column and it will survive a push.

Needs a service account, unlike reading (publish-to-web CSV needs nothing):

    GOOGLE_SHEETS_KEY   path to the service-account JSON
    GOOGLE_SHEETS_ID    the id from the sheet URL
    GOOGLE_SHEETS_TAB   worksheet name; blank = the first one

The sheet must be shared with the key's client_email as **Editor**. Read
access is not enough and fails with the same 403 as no access at all.
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any, Iterable

from . import sheet as sheetmod

SCOPE = "https://www.googleapis.com/auth/spreadsheets"   # NOT .readonly

# Column order for a sheet we create from scratch. Existing sheets keep their
# own order — this is only the layout for an empty one.
IPO_COLUMNS = [
    "company", "board", "sector",
    "fresh_cr", "ofs_cr", "price_low", "price_high", "lot_size", "registrar",
    "announced", "open", "close", "allotment", "refund", "listing",
    "revenue", "ebitda", "pat", "net_worth", "total_debt", "eps", "pe_peer_avg",
]
GMP_COLUMNS = ["company", "date", "gmp", "kostak", "sauda"]

# Prettier than the bare field name, and still an exact alias hit on re-read.
HEADINGS = {
    "fresh_cr": "Fresh Issue (Rs Cr)", "ofs_cr": "OFS (Rs Cr)",
    "price_low": "Price Low", "price_high": "Price High",
    "lot_size": "Lot Size", "pe_peer_avg": "Peer PE",
    "net_worth": "Net Worth", "total_debt": "Total Debt",
    "revenue": "Revenue (Rs Cr)", "ebitda": "EBITDA (Rs Cr)",
    "pat": "PAT (Rs Cr)", "gmp": "GMP", "eps": "EPS",
}

DATE_FIELDS = {"announced", "open", "close", "allotment", "refund", "listing", "date"}


class SheetsUnavailable(RuntimeError):
    """Raised instead of an ImportError or a raw 403, so the CLI can explain."""


def heading(field: str) -> str:
    return HEADINGS.get(field, field.replace("_", " ").title())


# ── connection ─────────────────────────────────────────────────────────────

def connect(key_path: str | None = None):
    """Return (service, service_account_email)."""
    key_path = key_path or os.getenv("GOOGLE_SHEETS_KEY") or ""
    if not key_path:
        raise SheetsUnavailable(
            "No GOOGLE_SHEETS_KEY in the environment. Copy .env.example to "
            ".env and point it at your service-account JSON."
        )
    if not os.path.exists(key_path):
        raise SheetsUnavailable(f"No service-account key at {key_path}")

    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise SheetsUnavailable(
            "google-api-python-client is not installed. "
            "pip install -r requirements.txt"
        ) from exc

    creds = service_account.Credentials.from_service_account_file(
        key_path, scopes=[SCOPE]
    )
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    return service, creds.service_account_email


def _explain(exc, email: str, sheet_id: str) -> SheetsUnavailable:
    """Turn Google's uniformly unhelpful 403 into the actual cause."""
    status = getattr(getattr(exc, "resp", None), "status", None)
    text = str(exc)
    if status == 403 and "has not been used" in text:
        return SheetsUnavailable(
            "The Sheets API is not enabled on this key's GCP project.\n"
            "  https://console.cloud.google.com/apis/library/sheets.googleapis.com"
        )
    if status == 403:
        return SheetsUnavailable(
            f"Denied. Share the sheet with {email} as Editor (Viewer is not "
            "enough to write)."
        )
    if status == 404:
        return SheetsUnavailable(f"No spreadsheet with id {sheet_id}")
    return SheetsUnavailable(f"Sheets API error {status}: {text}")


def _tab_title(service, sheet_id: str, tab: str | None, email: str) -> str:
    from googleapiclient.errors import HttpError
    try:
        meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    except HttpError as exc:
        raise _explain(exc, email, sheet_id) from exc

    titles = [s["properties"]["title"] for s in meta["sheets"]]
    if tab and tab not in titles:
        raise SheetsUnavailable(
            f"No tab named {tab!r}. This sheet has: {', '.join(titles)}"
        )
    return tab or titles[0]


# ── shaping an Ipo into rows ───────────────────────────────────────────────

def _latest(values: list[float]) -> float | None:
    """Financials are per-year arrays here but a single number in the sheet.

    `sheet.to_ipo_dict` reads one cell back as a one-element series labelled
    "Latest", so the newest year is the only one that survives a round trip.
    """
    return values[-1] if values else None


def row_for_ipo(ipo) -> dict[str, Any]:
    """One IPO -> {canonical field: value}. Missing values are omitted."""
    fin, issue, dates = ipo.financials, ipo.issue, ipo.dates
    raw: dict[str, Any] = {
        "company": ipo.company, "board": ipo.board, "sector": ipo.sector,
        "fresh_cr": issue.fresh_cr, "ofs_cr": issue.ofs_cr,
        "price_low": issue.price_low, "price_high": issue.price_high,
        "lot_size": issue.lot_size, "registrar": issue.registrar,
        "announced": dates.announced, "open": dates.open, "close": dates.close,
        "allotment": dates.allotment, "refund": dates.refund,
        "listing": dates.listing,
        "revenue": _latest(fin.revenue), "ebitda": _latest(fin.ebitda),
        "pat": _latest(fin.pat), "net_worth": _latest(fin.net_worth),
        "total_debt": _latest(fin.total_debt),
        "eps": fin.eps, "pe_peer_avg": fin.pe_peer_avg,
    }
    # 0.0 is the dataclass default for "not filled in", not a real zero, so
    # writing it would put a misleading number in a cell a human reads.
    return {k: v for k, v in raw.items()
            if v not in (None, "", 0, 0.0) or k in ("company", "board")}


def rows_for_gmp(ipo) -> list[dict[str, Any]]:
    """One row per GMP observation, newest last."""
    out = []
    for point in ipo.gmp_history:
        if point.date is None:
            continue
        rec = {"company": ipo.company, "date": point.date, "gmp": point.gmp}
        if point.kostak:
            rec["kostak"] = point.kostak
        if point.sauda:
            rec["sauda"] = point.sauda
        out.append(rec)
    return out


def _cell(field: str, value: Any) -> Any:
    """Render one value for the API.

    Sent with valueInputOption=RAW, which preserves JSON types: a number lands
    as a numeric cell, a string as text. Dates are deliberately written as ISO
    *text* rather than real date cells. A real date renders per the sheet's
    locale, and a US-locale sheet exports "8/12/2026" — which `sheet.to_date`
    reads as 8 December, because it tries %d/%m/%Y before %m/%d/%Y. ISO text
    is unambiguous, and still sorts correctly because ISO sorts lexically.
    """
    if isinstance(value, date):
        return value.isoformat()
    if field in DATE_FIELDS and value:
        return str(value)
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return value
    return "" if value is None else str(value)


# ── A1 helpers ─────────────────────────────────────────────────────────────

def col_letter(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    out = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        out = chr(65 + rem) + out
    return out


def a1(tab: str, row: int, col: int, ncols: int) -> str:
    """1-indexed row, 0-indexed col. Tab quoted, since names contain spaces."""
    return (f"'{tab}'!{col_letter(col)}{row}"
            f":{col_letter(col + ncols - 1)}{row}")


# ── the push ───────────────────────────────────────────────────────────────

def _key(rec: dict[str, Any], kind: str) -> tuple:
    """What makes two rows 'the same row'."""
    company = sheetmod.slugify(str(rec.get("company") or ""))
    if kind == "gmp":
        return (company, str(rec.get("date") or ""))
    return (company,)


def push(ipos: Iterable, *, kind: str = "ipos", sheet_id: str | None = None,
         tab: str | None = None, key_path: str | None = None,
         dry_run: bool = False) -> dict[str, Any]:
    """Upsert IPO or GMP rows into the sheet. Returns a summary dict."""
    from googleapiclient.errors import HttpError

    ipos = list(ipos)
    records: list[dict[str, Any]] = []
    for ipo in ipos:
        if kind == "gmp":
            records.extend(rows_for_gmp(ipo))
        else:
            records.append(row_for_ipo(ipo))
    records = [r for r in records if r.get("company")]

    summary: dict[str, Any] = {
        "records": records, "updated": 0, "appended": 0,
        "tab": tab or "", "header_written": False, "extra_columns": [],
        "dropped_fields": [],
    }
    if not records:
        return summary

    sheet_id = sheet_id or os.getenv("GOOGLE_SHEETS_ID") or ""
    if not sheet_id:
        raise SheetsUnavailable("No GOOGLE_SHEETS_ID in the environment.")

    service, email = connect(key_path)
    summary["service_account"] = email
    tab = _tab_title(service, sheet_id, tab or os.getenv("GOOGLE_SHEETS_TAB") or None, email)
    summary["tab"] = tab

    values_api = service.spreadsheets().values()
    try:
        existing = values_api.get(
            spreadsheetId=sheet_id, range=f"'{tab}'",
            valueRenderOption="UNFORMATTED_VALUE",
        ).execute().get("values", [])
    except HttpError as exc:
        raise _explain(exc, email, sheet_id) from exc

    default_cols = GMP_COLUMNS if kind == "gmp" else IPO_COLUMNS

    if not any(any(c not in (None, "") for c in row) for row in existing):
        # Empty sheet: we choose the layout.
        header_row = 1
        columns = list(default_cols)
        summary["header_written"] = True
        header_values = [[heading(c) for c in columns]]
        rows_below: list[list[Any]] = []
        by_key: dict[tuple, int] = {}
    else:
        # Existing sheet: adopt its layout, touch only the columns we map to.
        hrow = sheetmod.find_header_row(existing)
        mapping, unmatched = sheetmod.build_header_map(existing[hrow])
        header_row = hrow + 1
        summary["extra_columns"] = unmatched
        ncols = max(mapping) + 1 if mapping else 0
        columns = [""] * ncols
        for idx, field in mapping.items():
            columns[idx] = field
        header_values = []
        rows_below = existing[hrow + 1:]
        by_key = {}
        for offset, row in enumerate(rows_below):
            rec = {}
            for idx, field in mapping.items():
                if idx < len(row) and row[idx] not in (None, ""):
                    rec[field] = row[idx]
            if not rec.get("company"):
                continue
            by_key.setdefault(_key(rec, kind), header_row + 1 + offset)

    known = {c for c in columns if c}
    dropped = sorted({f for r in records for f in r} - known)
    summary["dropped_fields"] = dropped

    updates: list[dict[str, Any]] = []
    appends: list[list[Any]] = []
    if header_values:
        updates.append({"range": a1(tab, 1, 0, len(columns)), "values": header_values})

    next_row = header_row + len(rows_below) + 1
    for rec in records:
        line = [_cell(field, rec.get(field)) if field else "" for field in columns]
        target = by_key.get(_key(rec, kind))
        if target:
            # Rewrite in place. Unmapped columns sit outside `columns`, so a
            # notes column the user added to the right is never touched.
            updates.append({"range": a1(tab, target, 0, len(columns)), "values": [line]})
            summary["updated"] += 1
        else:
            appends.append(line)
            by_key[_key(rec, kind)] = next_row
            next_row += 1
            summary["appended"] += 1

    if dry_run:
        return summary

    try:
        if updates:
            values_api.batchUpdate(
                spreadsheetId=sheet_id,
                body={"valueInputOption": "RAW", "data": updates},
            ).execute()
        if appends:
            values_api.append(
                spreadsheetId=sheet_id,
                range=f"'{tab}'!A{header_row}",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": appends},
            ).execute()
    except HttpError as exc:
        raise _explain(exc, email, sheet_id) from exc

    return summary
