"""The Red Herring Prospectus, read as data.

EBITDA, net worth, total debt and the listed-peer P/E were the four fields
this project had written off as "in the RHP PDF, type them in by hand". They
are — but the RHP is not hidden. NSE links it from the same detail endpoint
that already supplies the price band, as a zip of proper text PDFs:

    Red Herring Prospectus -> .../content/ipo/RHP_<SYMBOL>.zip

So the whole chain is keyless and free: resolve the link, pull the zip, pull
the text, and hand the few relevant pages to the model instead of asking it
to remember a 575-page document it has never seen.

Three things make that practical rather than merely possible:

  * **Only the pages that matter are sent.** A 575-page RHP is ~1.5M
    characters. The peer table, the KPI table and the capitalisation
    statement are perhaps a dozen pages. Selecting them by keyword keeps one
    lookup inside a single request instead of a hundred.
  * **The extract is cached on disk.** Pulling 10 MB and parsing 575 pages
    takes about a hundred seconds; doing that again for the next field would
    be indefensible when the document never changes after filing.
  * **Some of these PDFs are unreadable and must be detected, not trusted.**
    The companion "Ratios / Basis of Issue Price" document is a scanned
    newspaper advertisement whose fonts carry no ToUnicode map: pypdf returns
    thousands of characters of which none are digits. Text that shape must be
    rejected outright — feeding it to a model is how a plausible, invented
    balance sheet ends up on a card.
"""

from __future__ import annotations

import io
import json
import re
import urllib.request
import zipfile
from datetime import date
from pathlib import Path
from typing import Any

from .scrape import _Session, _rows

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

# Sections worth sending. Ordered by how much they carry, because the budget
# is characters and the first ones in win.
SECTIONS: list[tuple[str, str]] = [
    ("peers",   r"comparison with listed industry peer|industry peer group"),
    ("kpi",     r"details of our kpis|key performance indicator"),
    ("basis",   r"basis for the offer price"),
    ("capital", r"capitalisation statement|total borrowings|total debt"),
    ("ratios",  r"return on net worth|net asset value per|earnings per share"),
]

MAX_CHARS = 90_000          # what we are willing to put in one prompt
MIN_DIGIT_DENSITY = 0.005   # below this the extract is a scanned image


def cache_path(slug: str) -> Path:
    from .. import store
    d = store.BACKEND_ROOT / ".cache" / "rhp"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{slug}.json"


def rhp_url(symbol: str, series: str = "EQ") -> str | None:
    """The RHP zip link from NSE's IPO detail endpoint."""
    detail = _Session().json(f"/api/ipo-detail?symbol={symbol}&series={series}")
    for row in _rows(detail.get("issueInfo")):
        title = (row.get("title") or "").strip().lower()
        if "red herring prospectus" in title:
            value = str(row.get("value") or "")
            m = re.search(r"https?://\S+?\.zip", value)
            if m:
                return m.group(0)
    return None


def _download(url: str, timeout: int = 240) -> bytes:
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Referer": "https://www.nseindia.com/"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _pdf_pages(blob: bytes) -> list[str]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:                    # pragma: no cover
        raise RuntimeError("pypdf is required to read the RHP") from exc
    reader = PdfReader(io.BytesIO(blob))
    out = []
    for page in reader.pages:
        try:
            out.append(page.extract_text() or "")
        except Exception:
            out.append("")
    return out


def _readable(text: str) -> bool:
    """False for a scanned PDF whose glyphs carry no usable encoding."""
    if len(text) < 2000:
        return False
    digits = sum(c.isdigit() for c in text)
    return (digits / len(text)) >= MIN_DIGIT_DENSITY


def pages_for(slug: str, symbol: str, series: str = "EQ",
              refresh: bool = False) -> dict[str, Any]:
    """Extracted RHP pages for one IPO, cached on disk.

    Returns {url, pages: [...], readable: bool}. An RHP is filed once and
    never changes, so the cache has no TTL.
    """
    path = cache_path(slug)
    if path.exists() and not refresh:
        return json.loads(path.read_text(encoding="utf-8"))

    url = rhp_url(symbol, series)
    if not url:
        return {"url": None, "pages": [], "readable": False}

    z = zipfile.ZipFile(io.BytesIO(_download(url)))
    # The zip carries the RHP and a General Information Document; the RHP is
    # the large one, and GID.pdf is boilerplate identical across every issue.
    pdfs = [n for n in z.namelist() if n.lower().endswith(".pdf")
            and "gid" not in n.rsplit("/", 1)[-1].lower()]
    if not pdfs:
        return {"url": url, "pages": [], "readable": False}
    biggest = max(pdfs, key=lambda n: z.getinfo(n).file_size)

    pages = _pdf_pages(z.read(biggest))
    payload = {"url": url, "file": biggest, "pages": pages,
               "readable": _readable("\n".join(pages[:40]))}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


_DATED = re.compile(
    r"\bdated\s+"
    r"(january|february|march|april|may|june|july|august|september|october|"
    r"november|december)\s+(\d{1,2}),?\s+(\d{4})", re.I)
_MONTHS = ["january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december"]


def filing_date(pages: list[str], opens: date | None = None,
                max_lead_days: int = 200) -> str | None:
    """The RHP's own cover date — the day the issue became public.

    `dates.announced` had no source at all: no exchange feed carries it and
    nothing derives it, so it was blank for every IPO and reel 1 simply
    dropped the row. It is printed on the cover of the document we are
    already holding:

        RED HERRING PROSPECTUS
        Dated August 3, 2026

    Bounded two ways, because "Dated <month> <d>, <y>" is not unique. It
    appears on cover pages of superseded drafts, auditor certificates and
    board resolutions, and taking the first match gave Credent Connect an
    announcement date of May 2024 for an issue opening in August 2026 — 825
    days early, and perfectly plausible-looking on its own.

      * only the first three pages are read, and
      * given `opens`, the date must fall in the {max_lead_days} days before
        it. An RHP is filed one to three weeks ahead; anything outside that
        window is a different document's date.

    Of the candidates that survive, the latest wins: a cover page can carry
    both the current filing and a reference to the draft it replaces.
    """
    found: list[date] = []
    for page in pages[:3]:
        for m in _DATED.finditer(page or ""):
            try:
                d = date(int(m.group(3)),
                         _MONTHS.index(m.group(1).lower()) + 1,
                         int(m.group(2)))
            except ValueError:
                continue
            if opens and not (0 <= (opens - d).days <= max_lead_days):
                continue
            found.append(d)
    return max(found).isoformat() if found else None


def excerpts(pages: list[str], budget: int = MAX_CHARS) -> dict[str, str]:
    """The handful of pages that actually carry the numbers.

    Keyed by section so the prompt can say what each block is, which measurably
    helps the model keep a peer's figure out of the company's column.
    """
    picked: dict[str, list[str]] = {}
    used: set[int] = set()
    spent = 0
    for name, pattern in SECTIONS:
        rx = re.compile(pattern, re.I)
        for i, text in enumerate(pages):
            if i in used or not rx.search(text):
                continue
            body = re.sub(r"[ \t]+", " ", text).strip()
            if spent + len(body) > budget:
                continue
            picked.setdefault(name, []).append(f"[RHP page {i + 1}]\n{body}")
            used.add(i)
            spent += len(body)
    return {k: "\n\n".join(v) for k, v in picked.items()}
