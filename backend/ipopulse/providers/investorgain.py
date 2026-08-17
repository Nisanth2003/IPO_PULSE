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
                           ("retail", "rii"), ("employee", "emp")):
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
