"""InvestorGain's JSON API — the desk this channel actually quotes.

investorgain.com is where the GMP shown on the cards is *supposed* to come
from: it is the site the channel checks by hand, and the reference the numbers
are argued about against. Until now nothing here could read it. The page is a
Next.js app that renders its tables client-side, so a plain HTTP scraper only
ever saw "No data available", and the fallback — asking Gemini to read the
page — cost quota, needed vetting, and still returned a *different desk's*
figure whenever the model landed on a cached copy.

The site's own front-end does not scrape itself. It calls a public JSON API,
and so can we:

    cloud/v2/ipo/list-read                     the live board
    cloud/v2/ipo/ipo-url-lists                 every IPO ever, id <-> slug
    cloud/v2/ipo/ipo-gmp-read/{id}/true        dated GMP, kostak, sauda
    cloud/v2/ipo/gmp-history-read/{id}         the same series, timestamped
    cloud/v2/ipo/ipo-subscription-read/{id}    day-wise QIB / NII / RII
    cloud/v2/ipo/ipo-allotment-read/{id}       registrar, BOA and listing dates

No key, no cookie, no session warm-up, no browser: verified 2026-08-16 that a
bare `requests.get` with no headers at all returns the same JSON the page
gets. That matters because the schedule runs on a GitHub runner where there is
no Chrome to drive.

**Everything is keyed on a numeric id, not a slug.** `/gmp/skyways-air-ipo/1820/`
is slug *and* id, and only the id is addressable — so `board()` carries the id
through and `history()` takes the row rather than a bare URL.

This provider supersedes ipoji rather than replacing it. ipoji stays wired in
as the fallback for the one case this cannot cover: the board being
unreachable. The two desks genuinely disagree — ipoji had Skyways at 24 on
16 Aug where InvestorGain had 28 — so which one answers is a decision about
whose quote the channel publishes, not a detail. InvestorGain answers first.
"""

from __future__ import annotations

import html
import re
from datetime import date, datetime
from typing import Any

import requests

API = "https://webnodejs.investorgain.com/cloud/v2/ipo"
SITE = "https://www.investorgain.com"
TIMEOUT = 25

# The site sends no headers of its own that matter, but identifying the caller
# is basic courtesy and makes this traceable in their logs if it ever loads them.
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Referer": SITE + "/",
}

# U = upcoming, O = open (bidding), C = closed. Listed issues drop off the
# board entirely, which is why `board()` cannot be the only status source.
STATUS = {"U": "upcoming", "O": "open", "C": "closed"}


def _get(path: str) -> dict[str, Any]:
    """One API call. Raises — callers decide whether a failure is fatal."""
    r = requests.get(f"{API}/{path}", headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _num(text: Any) -> float | None:
    """'2100' / '138.00' / '&#8377;301.62 Cr' / '--' -> float or None.

    Negative premiums are real and must survive; '--' and '' are absence, and
    absence is not zero (see tables.py — a written 0 invents a fact).
    """
    if text is None:
        return None
    s = html.unescape(str(text)).replace(",", "").strip()
    if not s or s in ("--", "-", "N/A", "NA"):
        return None
    m = re.search(r"(-|\+|−)?\s*₹?\s*(\d+(?:\.\d+)?)", s)
    if not m:
        return None
    val = float(m.group(2))
    return -val if m.group(1) in ("-", "−") else val


def _iso(text: str) -> str | None:
    """The three date shapes this API uses, all to ISO. None if unparseable.

    Never guess: a GMP filed under the wrong day is worse than a gap, and the
    trail on reel 2 is billed as daily.
    """
    if not text:
        return None
    s = str(text).strip()
    # 2026-08-24T00:00:00.000Z  — catalogue and board
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        try:
            return date.fromisoformat(s[:10]).isoformat()
        except ValueError:
            return None
    # 16-08-2026 — the GMP table
    m = re.fullmatch(r"(\d{2})-(\d{2})-(\d{4})", s)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
        except ValueError:
            return None
    # 12th Aug 2026 17:09 / 17th Aug 2026 — subscription and allotment
    m = re.match(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3})[a-z]*\s+(\d{4})", s)
    if m:
        try:
            return datetime.strptime(
                f"{m.group(1)} {m.group(2)} {m.group(3)}", "%d %b %Y"
            ).date().isoformat()
        except ValueError:
            return None
    return None


def page_url(row: dict[str, Any]) -> str:
    """The human-facing GMP page, which is also how `history()` finds the id."""
    return f"{SITE}/gmp/{row.get('slug_ig', '')}/{row.get('id', '')}/"


# ── the board ──────────────────────────────────────────────────────────────

def board() -> list[dict[str, Any]]:
    """Every IPO currently on InvestorGain's board. [] if it is unreachable.

    Shape matches `ipoji.board()` field for field so the two are drop-in
    alternatives, plus `id` — which is the only thing the rest of this module
    can address an IPO by.

    `has_gmp` is deliberately absent here: unlike ipoji, this board carries no
    premium at all, only the issue. The GMP costs a second call per IPO, so
    the caller decides which rows are worth one.
    """
    try:
        rows = _get("list-read").get("ipoList") or []
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        name = html.unescape((r.get("company_short_name") or "").strip())
        ident = r.get("id")
        if not name or not ident:
            continue
        out.append({
            "id": int(ident),
            "name": name,
            "slug_ig": r.get("urlrewrite_folder_name") or "",
            "board": "sme" if (r.get("issue_category") or "").upper() == "SME"
                     else "mainboard",
            "status": STATUS.get((r.get("ipo_status") or "").upper()),
            "sector": html.unescape((r.get("company_sector") or "").strip()),
            "issue_size_cr": _num(r.get("issue_size")),
            "open": _iso(r.get("issue_open_dt") or ""),
            "close": _iso(r.get("issue_end_dt") or ""),
            "exchanges": [e.strip() for e in
                          (r.get("ipo_listing_at") or "").split(",") if e.strip()],
            "logo": logo_url(r.get("logo_url")),
        })
        out[-1]["url"] = page_url(out[-1])
    return out


# Where `logo_url` actually lives. The API returns a bare filename
# ('skyways-air-logo.png'), and the host is not investorgain.com at all —
# InvestorGain is Chittorgarh's property and serves the artwork from there.
# Confirmed against the rendered page rather than guessed.
LOGO_BASE = "https://www.chittorgarh.net/images/ipo/"


def logo_url(filename: Any) -> str:
    """Absolute URL for a board row's logo, or '' if there isn't one.

    Worth having at all because the studio puts this in the card header on
    every scene, and every alternative was worse: the initials tile is a
    placeholder, and asking a model for a logo URL invents one that 404s.
    All 21 rows on the board carried a filename when this was added, and the
    host sends `Access-Control-Allow-Origin: *` — which is the part that
    matters, because html2canvas runs with `useCORS: true` and silently drops
    any image it cannot read, so a logo without that header would look fine on
    screen and vanish from every exported PNG.
    """
    name = str(filename or "").strip()
    if not name or "/" in name or "\\" in name:
        return ""                     # a path, not the bare filename expected
    return LOGO_BASE + name


def catalogue() -> list[dict[str, Any]]:
    """Every IPO the site has ever listed — ~2,400 rows, id <-> slug <-> dates.

    `board()` only carries what is live, and an issue drops off it the day it
    lists. This is how a slug that has already listed is still resolvable to
    an id, which is what makes a *backfill* possible at all.
    """
    try:
        rows = _get("ipo-url-lists").get("lists") or []
    except Exception:
        return []
    out = []
    for r in rows:
        ident, name = r.get("id"), (r.get("company_short_name") or "").strip()
        if not ident or not name:
            continue
        out.append({
            "id": int(ident),
            "name": html.unescape(name),
            "slug_ig": r.get("urlrewrite_folder_name") or "",
            "open": _iso(r.get("issue_open_dt") or ""),
            "close": _iso(r.get("issue_end_dt") or ""),
            # The catalogue carries the logo filename too, which is what lets a
            # listed IPO — already dropped off the board — still get its header
            # artwork on reels 2 and 6.
            "logo": logo_url(r.get("logo_url")),
        })
    return out


# ── the numbers ────────────────────────────────────────────────────────────

def _ipo_id(row_or_url: Any) -> int | None:
    """Accept a board row, a bare id, or a /gmp/<slug>/<id>/ URL."""
    if isinstance(row_or_url, dict):
        return int(row_or_url["id"]) if row_or_url.get("id") else None
    if isinstance(row_or_url, int):
        return row_or_url
    m = re.search(r"/(\d+)/?$", str(row_or_url or ""))
    return int(m.group(1)) if m else None


_gmp_cache: dict[int, list[dict[str, Any]]] = {}


def _gmp_rows(ident: int) -> list[dict[str, Any]]:
    """The GMP response, fetched once per id per run.

    `history()` and `band_high()` both live in it, and a sync over twenty
    IPOs would otherwise ask the same question forty times. Cached for the
    process, not to disk: the whole point of this source is that today's
    figure is current.
    """
    if ident not in _gmp_cache:
        try:
            _gmp_cache[ident] = _get(f"ipo-gmp-read/{ident}/true").get("ipoGmpData") or []
        except Exception:
            return []                    # not cached — a blip should be retryable
    return _gmp_cache[ident]


def history(row_or_url: Any) -> list[dict[str, Any]]:
    """Dated GMP series for one IPO, oldest first.

    Each row also carries the sauda rate, because it arrives in the same
    response and costs nothing extra. `kostak` is left out on purpose: the
    field that looks like it (`sub2`) is a pair — '2100/31500' — and guessing
    which half is the Kostak is exactly the misread `ai.vet_gmp` exists to
    catch.
    """
    ident = _ipo_id(row_or_url)
    if not ident:
        return []
    out: list[dict[str, Any]] = []
    for r in _gmp_rows(ident):
        day, gmp = _iso(r.get("gmp_date") or ""), _num(r.get("gmp"))
        if not day or gmp is None:
            continue
        point: dict[str, Any] = {"date": day, "gmp": gmp, "source": "investorgain"}
        sauda = _num(r.get("subject_to_sauda"))
        if sauda:
            point["sauda"] = sauda
        out.append(point)
    out.sort(key=lambda p: p["date"])

    # Drop the run of zeros before the grey market starts quoting.
    #
    # The table opens a row per day from the moment the issue is announced and
    # fills the premium with 0 until a dealer actually prices it — Behari Lal
    # reads 0 on 6 Aug and 30 on the 7th. Those leading zeros are "not quoted",
    # not "quoted at par", and writing them would draw a flat line across the
    # front of reel 2 and let compute score a grey-market signal that does not
    # exist yet. A zero *after* trading has begun is a real collapse to par and
    # is kept — which is why this trims a prefix rather than filtering on value.
    first = next((i for i, p in enumerate(out) if p["gmp"] != 0), None)
    return [] if first is None else out[first:]


def band_high(row_or_url: Any) -> float | None:
    """Upper price band, which rides along in the GMP response.

    Worth pulling separately: `doctor` blanks half of reel 1 without it, and
    for an issue NSE has not published yet this is the only free source.
    """
    ident = _ipo_id(row_or_url)
    if not ident:
        return None
    for r in _gmp_rows(ident):
        high = _num(r.get("max_ipo_price"))
        if high:
            return high
    return None


def _plain(value: Any) -> str:
    """HTML fragment -> one line of readable prose. '' for nothing usable."""
    s = html.unescape(re.sub(r"<[^>]+>", " ", str(value or "")))
    return re.sub(r"\s+", " ", s).strip()


_detail_cache: dict[int, dict[str, Any]] = {}


def detail(row_or_url: Any) -> dict[str, Any]:
    """The `ipo-detail-read` record, fetched once per process.

    Three separate readers want this one payload — the business description,
    the financial statement and the valuation KPIs — and they are called back
    to back from `enrich`. Sharing the cache turns three round trips into one.
    """
    ident = _ipo_id(row_or_url)
    if not ident:
        return {}
    if ident not in _detail_cache:
        try:
            payload = _get(f"ipo-detail-read/{ident}")
        except Exception:
            return {}                     # not cached — a blip should retry
        data = payload.get("ipoData")
        if isinstance(data, list):
            data = data[0] if data else {}
        _detail_cache[ident] = data if isinstance(data, dict) else {}
    return _detail_cache[ident]


# ── the financial statement ────────────────────────────────────────────────
# The single biggest gap in the whole store: reel 4 is called "Apply or Skip"
# and its financials and valuation scenes were blank for 16 of 19 tracked
# IPOs, because the only filler was a Gemini read of a 400-page RHP PDF that
# mostly came back empty and cost a request each time it did.
#
# This desk publishes the same restated statement as an HTML table on the
# detail record — three years of Assets / Total Income / PAT / EBITDA / Net
# Worth / Reserves / Total Borrowing, in rupees crore, keyed by period end.
# Free, keyless, deterministic and already fetched for the company brief.
#
# Their row labels, mapped to ours. Anything unlisted is skipped rather than
# guessed: 'Reserves and Surplus' is not net worth and 'Assets' is not revenue.
FIN_ROWS = {
    "total income": "revenue",
    "revenue": "revenue",
    "revenue from operations": "revenue",
    "ebitda": "ebitda",
    "profit after tax": "pat",
    "net profit": "pat",
    "net worth": "net_worth",
    "total borrowing": "total_debt",
    "total borrowings": "total_debt",
}

_CELL = re.compile(r"<t([dh])[^>]*>(.*?)</t\1>", re.S | re.I)
_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)


def _fy(period: str) -> str | None:
    """'31 Mar 2026' -> 'FY26'. Anything that is not a March year-end is
    dropped: a nine-month stub period stacked beside two full years would read
    as a collapse in revenue that never happened."""
    m = re.search(r"(\d{1,2})\s*(\w{3})\w*\s*(\d{4})", period or "")
    if not m:
        return None
    day, mon, year = int(m.group(1)), m.group(2).lower(), int(m.group(3))
    if mon != "mar" or day < 28:
        return None
    return f"FY{year % 100:02d}"


def financials(row_or_url: Any) -> dict[str, Any]:
    """Three years of restated figures, in the store's own column names.

    Returns {} rather than a half-filled shape when the table is absent or
    unparseable — an empty `years` list written over a hand-typed statement
    would be a silent deletion.
    """
    html_table = (detail(row_or_url) or {}).get("financial") or ""
    if "<t" not in str(html_table):
        return {}

    grid: list[list[str]] = []
    for raw_row in _ROW.findall(str(html_table)):
        cells = [_plain(c) for _, c in _CELL.findall(raw_row)]
        if cells:
            grid.append(cells)
    if len(grid) < 2:
        return {}

    # Header: 'Period Ended' then one column per year, newest first.
    head = grid[0]
    years, cols = [], []
    for i, cell in enumerate(head[1:], start=1):
        fy = _fy(cell)
        if fy:
            years.append(fy)
            cols.append(i)
    if not years:
        return {}

    series: dict[str, list[float | None]] = {}
    for row in grid[1:]:
        label = FIN_ROWS.get(row[0].strip().lower().rstrip("*").strip())
        if not label or label in series:
            continue
        series[label] = [_num(row[i]) if i < len(row) else None for i in cols]
    if not series:
        return {}

    # They print newest-first; the store and every chart read oldest-first.
    order = sorted(range(len(years)), key=lambda i: years[i])
    out: dict[str, Any] = {"years": [years[i] for i in order]}
    for label, vals in series.items():
        picked = [vals[i] for i in order]
        # All-absent means the row was there but empty. Absence is not zero.
        if any(v is not None for v in picked):
            out[label] = [0.0 if v is None else v for v in picked]
    return out


def valuation(row_or_url: Any) -> dict[str, Any]:
    """Post-issue EPS and the ratio KPIs behind reel 4's valuation scene.

    Post-issue and not pre-issue EPS, deliberately: the P/E a buyer pays is
    the band over the earnings per share the company will have AFTER the fresh
    issue dilutes it. `kpi_eps` is the pre-issue figure and flatters every
    valuation it touches.

    Peer P/E is NOT here. `peer_group_financial_stmt` comes back empty on every
    row checked, and a peer average is the one number on that scene that cannot
    be approximated — so it stays a gap `doctor` reports rather than something
    filled with the issue's own multiple under a different label.
    """
    d = detail(row_or_url) or {}
    if not d:
        return {}
    out: dict[str, Any] = {}
    for key, field in (("eps", "kpi_eps_post"), ("pe", "post_pe_ratio"),
                       ("ronw", "kpi_ronw"), ("roce", "kpi_roce"),
                       ("debt_equity", "kpi_debt_equity"),
                       ("pat_margin", "kpi_pat_margin"),
                       ("ebitda_margin", "kpi_ebitda"),
                       ("nav", "nav"), ("market_cap", "market_cap"),
                       ("price_to_book", "price_to_book_value")):
        val = _num(d.get(field))
        if val is not None:
            out[key] = val
    return out


def identity(row_or_url: Any) -> dict[str, Any]:
    """The exchange's own name for this issue: NSE symbol, BSE code, ISIN.

    The thing name matching should never have been standing in for. An IPO is
    identified by a ticker — `MOMSBELIEF` is Rays of Belief and nothing else —
    and this desk carries it on the same detail record `facts` already reads,
    so it costs no extra request.

    Why it matters more than any string signal: the sheet has carried the same
    company twice three times, and every one of those pairs shares a ticker
    while sharing almost nothing else. `purple-style-labs` and
    `pernia-s-pop-up-studio` have not one character in common as names — and
    NSE's symbol for both is PERNIASPOP. `rays-of-belief` and its 48-character
    twin were stamped `BSE:4775` and `NSE:MOMSBELIEF` respectively, because
    `sources.exchange` records whichever exchange answered first; asking this
    desk instead gives the same symbol for both.

    Empty fields are omitted rather than written blank, and that is not
    incidental — `isin` and `bse_scripcode` fill in only as an issue nears
    listing, so a pre-listing IPO legitimately has a symbol and no ISIN. A
    blank stored here would be an identity that matches every other blank.
    """
    d = detail(row_or_url) or {}
    if not d:
        return {}
    out: dict[str, Any] = {}
    # nse_symbol is the populated one before listing; nse_script_symbol and
    # the bse_* trio are all spellings this API carries and mostly leaves
    # empty, so each is a fallback rather than a separate fact.
    for key, fields in (
        ("nse_symbol", ("nse_symbol", "nse_script_symbol", "nse_cd")),
        ("bse_code", ("bse_scripcode", "bse_script_code", "bse_script_id",
                      "bse_cd")),
        ("isin", ("isin",)),
    ):
        for field in fields:
            val = str(d.get(field) or "").strip()
            if val:
                out[key] = val.upper()
                break
    return out


def categories(row_or_url: Any) -> dict[str, Any]:
    """Shares reserved per category, and the minimum bid each one must make.

    What reel 4's `stake` scene needs to quote allotment odds for anyone other
    than a retail applicant. The sHNI and bHNI tranches have their own minimum
    application sizes (14 lots and 67 lots on Tempsens), and since 2021 both
    allot that minimum by draw — so each has its own odds, and neither is the
    retail number.
    """
    d = detail(row_or_url) or {}
    if not d:
        return {}
    out: dict[str, Any] = {}
    # ── the QIB field is a trap, and picking the wrong one is silent ──────
    #
    # `shares_offered_qib` is QIB **excluding the anchor book** — the desk's own
    # `shares_offered_qib_ex_anchor` holds the identical value. The anchor
    # portion is carved out OF the QIB reservation, not beside it, so the number
    # that means "half this issue is set aside for institutions" is
    # `shares_offered_qib_with_anchor`.
    #
    # Reading the ex-anchor field made a standard mainboard issue look like
    # QIB 20 / NII 15 / retail 35 — a 70% book, with 30% unexplained. Symbiotec
    # says "Not more than 50% of the Net Offer" for QIB and its
    # with-anchor figure is exactly 50%. Verified across both shapes:
    #
    #   qib_with_anchor + nii + rii + emp + shareholders == shares_offered_total
    #
    # to the individual share on Symbiotec, Hy-Tech, Madhur and ABH. The market
    # maker sits OUTSIDE that total on SME issues — it is a market-making
    # reservation rather than part of the net offer — which is why it is not
    # summed in and not stored here.
    for key, field in (("qib", "shares_offered_qib_with_anchor"),
                       ("nii", "shares_offered_nii"),
                       ("nii_small", "shares_offered_small_nii"),
                       ("nii_big", "shares_offered_big_nii"),
                       ("retail", "shares_offered_rii"),
                       ("employee", "shares_offered_emp"),
                       # A real SEBI category, not padding: issues with a listed
                       # parent carve out a shareholder quota, and without it
                       # those records would report an unexplained gap.
                       ("shareholders", "shares_offered_shareholders"),
                       ("total", "shares_offered_total")):
        val = _num(d.get(field))
        if val:
            out[f"shares_{key}"] = val

    # Fallback for a record that carries only the plain field. Better a QIB
    # slice that understates the anchor than no QIB slice at all.
    if not out.get("shares_qib"):
        val = _num(d.get("shares_offered_qib"))
        if val:
            out["shares_qib"] = val

    # Informational, and a SUBSET of shares_qib — never summed with it. Worth
    # storing because "thirty percent of this issue went to anchor investors
    # before bidding even opened, and it is locked in" is a real thing to say
    # about an issue, and it cannot be recovered from the other fields.
    anchor = _num(d.get("shares_offered_anchor_investor"))
    if anchor:
        out["shares_anchor"] = anchor
    # '700 shares (14 lots)' — the share count is what the odds arithmetic
    # needs, and the lot count is already derivable from it.
    for key, field in (("min_shni_qty", "min_hni_qty"),
                       ("min_bhni_qty", "min_bhni_qty"),
                       ("max_retail_qty", "max_retail_qty")):
        val = _num(d.get(field))
        if val:
            out[key] = val

    # `market_lot_size`, NOT `minimum_order_quantity`. They differ on SME
    # issues — Madhur Knit Crafts has a 1200-share lot and a 2400-share retail
    # minimum, because SME retail must bid two lots — and it is the LOT that
    # every per-lot figure in this project multiplies by: minimum investment,
    # gain per lot, the trail's profit column, the allotment draw unit.
    # Storing the order minimum as the lot would inflate all of them 2×.
    lot = _num(d.get("market_lot_size"))
    if lot:
        out["lot_size"] = int(lot)
    return out


def company_brief(row_or_url: Any) -> dict[str, Any]:
    """What the company actually does, in its own filing's words.

    The single most valuable thing this endpoint carries, and the gap that made
    reel 1's company scene thin. `draft_analysis` is a facts-only prompt, and
    the facts it was given held no business description at all — just a sector
    string, the issue terms and the financials. Asked for four bullets on the
    business it could only pad ("operates in the jewellery sector", "generates
    income through jewellery sales") or admit the absence, and an admission is
    the one thing a caption must never be.

    So this is not a nicety. It is the difference between "operates in the
    jewellery sector" and "56 stores across 46 cities in Tamil Nadu, Telangana
    and Karnataka; gold was 93.96% of FY24 revenue".

    Free, keyless and deterministic, which is why it is preferred over the
    obvious alternative. Grounded search would answer the same question, but
    `google_search` is metered and 429s on a free key (see ai._generate_grounded)
    — and a model summarising a page it found is a second chance to invent a
    fact this returns verbatim.

    Absent keys come back missing rather than empty: a blank string written into
    the sheet is a fact claimed, and tables.py treats absence and 0 differently
    for exactly this reason.

    NOT included: recent news. `articles` is an empty list on every row checked
    and `article_ids` is blank, so this desk simply does not carry it. Do not
    add a news key here that quietly holds something else.
    """
    d = detail(row_or_url)
    if not d:
        return {}

    # `about_company` is the editorial write-up and `company_desc` the filing's
    # own summary. They overlap but not completely — one carries the revenue mix
    # and store count, the other the incorporation year and the business model —
    # so both go through and the prompt is told they may repeat each other.
    out: dict[str, Any] = {}
    # `company_sector` and NOT `ipo_industry`: the latter is a foreign key, so
    # it read "Industry: 53" on screen. Anything sourced here has to be the
    # display value, because nothing downstream can tell an id from a name.
    for key, field in (("about", "about_company"), ("summary", "company_desc"),
                       ("objects", "issue_objects"), ("promoters", "promoters"),
                       ("website", "website"), ("industry", "company_sector"),
                       ("incorporated", "company_incorporation")):
        text = _plain(d.get(field))
        if text:
            out[key] = text

    city, state = _plain(d.get("city_name")), _plain(d.get("state"))
    if city or state:
        out["hq"] = ", ".join(x for x in (city, state) if x)
    return out


def subscription(row_or_url: Any) -> list[dict[str, Any]]:
    """Day-wise subscription, in the store's own column names.

    This is a genuine *archive*, which NSE is not: NSE publishes a running
    total for today and nothing for yesterday, so a missed run used to lose a
    bidding day for good. Here day 1 is still readable on day 3.
    """
    ident = _ipo_id(row_or_url)
    if not ident:
        return []
    try:
        data = _get(f"ipo-subscription-read/{ident}").get("data") or {}
    except Exception:
        return []
    rows = data.get("ipoBiddingData")
    if not isinstance(rows, list):        # '' when bidding has not opened
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        total = _num(r.get("total"))
        if total is None:
            continue
        day = r.get("Seq")
        row: dict[str, Any] = {"day": int(day) if day else len(out) + 1,
                               "total": total}
        when = _iso(r.get("bid_date") or "")
        if when:
            row["date"] = when
        for field, key in (("qib", "qib"), ("nii", "nii"),
                           ("retail", "rii"), ("employee", "emp"),
                           ("nii_small", "nii_small"), ("nii_big", "nii_big")):
            val = _num(r.get(key))
            if val is not None:
                row[field] = val
        out.append(row)
    out.sort(key=lambda r: r["day"])
    return out


def allotment(row_or_url: Any) -> dict[str, Any]:
    """Registrar and the T+3 calendar. {} when the issue has not closed."""
    ident = _ipo_id(row_or_url)
    if not ident:
        return {}
    try:
        rows = _get(f"ipo-allotment-read/{ident}").get("allotmentStatusDetails") or []
    except Exception:
        return {}
    if not rows:
        return {}
    r = rows[0]
    out: dict[str, Any] = {}
    if r.get("reg_comp_short_name"):
        out["registrar"] = html.unescape(r["reg_comp_short_name"].strip())
    if r.get("reg_website"):
        out["registrar_url"] = r["reg_website"].strip()
    allot_dt, listing_dt = _iso(r.get("timetable_boa_dt") or ""), \
        _iso(r.get("timetable_listing_dt") or "")
    if allot_dt:
        out["allotment"] = allot_dt
    if listing_dt:
        out["listing"] = listing_dt
    size = _num(r.get("issue_size"))
    if size:
        out["total_cr"] = size
    return out


# ── matching their names to our slugs ──────────────────────────────────────
#
# Same problem and the same answer as ipoji: their slug is close to ours but
# not identical ('credent-connect-ipo' against our 'credent-connect-n-care').
# Reusing ipoji's matcher rather than writing a second one keeps a single
# definition of what counts as the same company — if one of them starts
# mis-filing a GMP, there is one place to fix it.

from .ipoji import match_slug as _match_slug            # noqa: E402


def match_slug(name: str, url: str, slugs: list[str],
               companies: dict[str, str]) -> str | None:
    """Best of our slugs for an InvestorGain row, or None if it is not clear.

    Their URL slug always ends '-ipo' and ipoji's matcher already strips that,
    so the shared implementation needs no adjusting here.
    """
    return _match_slug(name, url, slugs, companies)


class InvestorGainProvider:
    """The module above behind the standard `Provider` contract.

    `gmp-sync` calls the functions directly because it needs the board itself
    — which issues exist, which are ours, which are new. This class is for the
    other direction: `sync --provider investorgain` treating it as one feed
    among several, so it can drive the daily chain the way `nse` does.

    Deliberately narrower than NseProvider. NSE is the primary record for
    issue terms and this is not, so `fetch_ipo` fills only what NSE cannot
    supply before an issue opens — the band, the sector, the calendar — and
    `merge()` still gives anything already stored precedence.
    """

    name = "investorgain"

    def available(self) -> bool:
        return True                      # no key, no config, nothing to check

    def fetch_catalogue(self) -> list[dict[str, Any]]:
        from .scrape import slugify
        out = []
        for r in board():
            rec: dict[str, Any] = {
                "slug": slugify(r["name"]),
                "company": r["name"],
                "board": "SME" if r["board"] == "sme" else "Mainboard",
                "status": r.get("status"),
            }
            dates = {k: r[k] for k in ("open", "close") if r.get(k)}
            if dates:
                rec["dates"] = dates
            if r.get("issue_size_cr"):
                rec["issue"] = {"total_cr": r["issue_size_cr"]}
            out.append(rec)
        return out

    def _row(self, slug: str) -> dict[str, Any] | None:
        for r in board():
            if r["slug_ig"] == f"{slug}-ipo":
                return r
        return resolve(slug)

    def fetch_ipo(self, slug: str) -> dict[str, Any]:
        row = self._row(slug)
        if not row:
            return {}
        out: dict[str, Any] = {}
        if row.get("sector"):
            out["sector"] = row["sector"]
        issue: dict[str, Any] = {}
        high = band_high(row)
        if high:
            issue["price_high"] = high
        if row.get("issue_size_cr"):
            issue["total_cr"] = row["issue_size_cr"]
        if row.get("exchanges"):
            issue["exchanges"] = row["exchanges"]
        dates = {k: row[k] for k in ("open", "close") if row.get(k)}
        info = allotment(row)
        for key in ("registrar", "registrar_url"):
            if info.get(key):
                issue[key] = info[key]
        for key in ("allotment", "listing"):
            if info.get(key):
                dates[key] = info[key]
        if issue:
            out["issue"] = issue
        if dates:
            out["dates"] = dates
        return out

    def fetch_gmp(self, slug: str) -> list[dict[str, Any]]:
        row = self._row(slug)
        return history(row) if row else []

    def fetch_subscription(self, slug: str) -> list[dict[str, Any]]:
        row = self._row(slug)
        return subscription(row) if row else []


_catalogue_cache: list[dict[str, Any]] | None = None


def resolve(slug: str, company: str = "") -> dict[str, Any] | None:
    """Find one of our IPOs in the full catalogue, board or no board.

    An issue drops off `list-read` the day it lists, which is exactly when its
    trail is most worth completing — Leap India and Technocraft both listed on
    14 Aug still missing a bidding day. The catalogue keeps every one of them
    addressable, so this is what turns "cannot be backfilled" into a lookup.

    Returns a row in `board()` shape (minus the fields only the live board
    has) or None when the match is not clear enough to trust.
    """
    global _catalogue_cache
    if _catalogue_cache is None:
        _catalogue_cache = catalogue()
    if not _catalogue_cache:
        return None

    names = {r["slug_ig"]: r for r in _catalogue_cache}
    # The catalogue reuses a slug across an IPO and its later FPO, and ids
    # climb over time, so the highest id wins a straight slug hit.
    direct = [r for k, r in names.items() if k == f"{slug}-ipo"]
    if direct:
        best = max(direct, key=lambda r: r["id"])
        return {**best, "url": page_url(best)}

    # No slug hit: match their company name against ours, newest id first so
    # a company that has listed twice resolves to the current issue.
    scored = [r for r in _catalogue_cache
              if _match_slug(r["name"], r["slug_ig"], [slug],
                             {slug: company or slug}) == slug]
    if not scored:
        return None
    best = max(scored, key=lambda r: r["id"])
    return {**best, "url": page_url(best)}
