"""NSE provider — real IPO data with no API key and no AI.

`research.py` asks Gemini to read the web, which costs grounded-search quota
and returns numbers that have to be vetted before they can be trusted. This
module skips both: NSE publishes the same facts as JSON, and NSE *is* the
primary record, so there is nothing to vet. Issue terms and subscription come
from the exchange itself rather than from a model's reading of a page about it.

What it can supply:

    fetch_catalogue()     every IPO currently open or upcoming
    fetch_ipo(slug)       price band, dates, lot size, registrar
    fetch_subscription()  live category-wise demand, updated through the day

What it cannot: **GMP**. Grey-market premium is by definition unofficial — no
exchange publishes it, and the community sites that do render it with
JavaScript, so there is no free structured feed. `fetch_gmp` returns nothing
rather than guessing. Use `ipopulse gmp <slug> <value>` or the research
provider for that one field.

The endpoints are the ones nseindia.com's own pages call. They are public but
undocumented, and they refuse a request that has not been given a session
cookie first — hence `_Session`, which loads a normal page before the JSON.
"""

from __future__ import annotations

import gzip
import http.cookiejar
import json
import re
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from typing import Any

BASE = "https://www.nseindia.com"
WARMUP = f"{BASE}/market-data/all-upcoming-issues-ipo"

# A browser UA is required: the plain urllib default is rejected outright.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

MIN_INTERVAL = 0.7          # seconds between calls — this is someone's server


class _Session:
    """Cookie-bearing opener. NSE 401s any JSON call made without one."""

    def __init__(self) -> None:
        jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar)
        )
        self._opener.addheaders = [
            ("User-Agent", UA),
            ("Accept", "*/*"),
            ("Accept-Language", "en-IN,en;q=0.9"),
            ("Accept-Encoding", "gzip"),
            ("Referer", WARMUP),
        ]
        self._warm = False
        self._last = 0.0

    def _get(self, url: str, timeout: int = 30) -> str:
        wait = MIN_INTERVAL - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        resp = self._opener.open(url, timeout=timeout)
        self._last = time.monotonic()
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return raw.decode("utf-8", "replace")

    def json(self, path: str) -> Any:
        if not self._warm:
            try:
                self._get(WARMUP)
            except urllib.error.URLError:
                pass                       # try the call anyway; it may be cached
            self._warm = True
        text = self._get(f"{BASE}{path}")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # NSE answers an unauthenticated call with an HTML block page.
            raise RuntimeError(f"NSE returned non-JSON for {path}") from None


def _rows(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "dataList", "bidDetails"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


# ── parsing NSE's display strings ──────────────────────────────────────────

def _date(text: str) -> str | None:
    """'10-Aug-2026' -> '2026-08-10'."""
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime((text or "").strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _band(text: str) -> tuple[float | None, float | None]:
    """'Rs.829 to Rs.871' / 'Rs. 829/- to Rs. 871/- per Equity Share'."""
    nums = [float(n.replace(",", "")) for n in
            re.findall(r"(\d[\d,]*(?:\.\d+)?)", text or "")]
    if not nums:
        return None, None
    if len(nums) == 1:
        return nums[0], nums[0]
    return min(nums[:2]), max(nums[:2])


def _int(text: str) -> int | None:
    m = re.search(r"(\d[\d,]*)", text or "")
    return int(m.group(1).replace(",", "")) if m else None


def _num(value: Any) -> float:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return 0.0


# ── the fresh / OFS split ──────────────────────────────────────────────────
# This was written off as "RHP only, type it in by hand" for every IPO. It is
# not: NSE states it in the `Issue Size` line of the detail endpoint, in one
# of three shapes —
#
#   both in rupees   Fresh Issue aggregating upto Rs. 14,280 million and
#                    Offer for Sale aggregating upto Rs. 1,250 million
#   both in shares   Fresh Issue of up to 9,505,000 equity shares and
#                    Offer for Sale of up to 2,376,000 equity shares
#   mixed            Fresh Issue aggregating up to Rs. 2,000 million and
#                    Offer for Sale of up to 9,166,000 Equity Shares
#
# The trailing "(including Employee Reservation Portion aggregating up to
# Rs. 15 million ...)" is a *subset* of the offer, not another leg, so each
# side takes only the first figure in its own segment.

_MONEY = re.compile(
    r"(?:rs\.?|inr|₹)\s*([\d,]+(?:\.\d+)?)\s*(million|crore|cr\b|lakh|billion)",
    re.I)
_SHARES = re.compile(r"([\d,]+)\s*equity\s*shares", re.I)
# rupees crore per unit
_UNIT_CR = {"million": 0.1, "billion": 100.0, "crore": 1.0, "cr": 1.0, "lakh": 0.01}


def _leg_cr(segment: str, price_high: float) -> float:
    """First money-or-share figure in `segment`, as rupees crore.

    Whichever appears first wins, because the two shapes can both occur in
    one segment and the leading one is the leg itself.
    """
    money, shares = _MONEY.search(segment), _SHARES.search(segment)
    if money and (not shares or money.start() < shares.start()):
        unit = money.group(2).lower().rstrip(".")
        return round(float(money.group(1).replace(",", "")) * _UNIT_CR.get(unit, 0.0), 2)
    if shares and price_high:
        # A share count only becomes a rupee figure at the upper band.
        return round(int(shares.group(1).replace(",", "")) * price_high / 1e7, 2)
    return 0.0


def _split_from_issue_size(text: str, price_high: float) -> tuple[float, float]:
    """(fresh_cr, ofs_cr) from NSE's `Issue Size` prose. 0 for either if absent."""
    if not text:
        return 0.0, 0.0
    # Split on the OFS keyword: everything before it describes the fresh leg.
    parts = re.split(r"offer\s+for\s+sale", text, maxsplit=1, flags=re.I)
    head = parts[0]
    tail = parts[1] if len(parts) > 1 else ""

    fresh = 0.0
    if re.search(r"fresh\s+(?:issue|offer)", head, re.I):
        after = re.split(r"fresh\s+(?:issue|offer)", head, maxsplit=1, flags=re.I)[1]
        fresh = _leg_cr(after, price_high)
    return fresh, _leg_cr(tail, price_high) if tail else 0.0


def _working_days_after(start: str, days: int) -> str | None:
    """`start` + N working days, skipping weekends.

    SEBI's T+3 rule fixes the post-close calendar relative to the close date,
    so allotment / refund / listing are derivable rather than unknowable. It
    does not know about exchange holidays, so a date landing on one will be a
    day early — these fill blanks only, and a hand-typed value always wins.
    """
    try:
        day = date.fromisoformat(start)
    except (TypeError, ValueError):
        return None
    added = 0
    while added < days:
        day += timedelta(days=1)
        if day.weekday() < 5:               # Mon-Fri
            added += 1
    return day.isoformat()


def slugify(text: str) -> str:
    """Same shape as the sheet importer's, so both agree on a slug."""
    s = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    s = re.sub(r"-(ltd|limited|pvt|private)$", "", s)
    return s[:48] or "ipo"


# ── the provider ───────────────────────────────────────────────────────────

class NseProvider:
    """Live issue terms and subscription, straight from the exchange."""

    name = "nse"

    def __init__(self, session: _Session | None = None) -> None:
        self._s = session or _Session()
        self._catalogue: list[dict[str, Any]] | None = None

    def available(self) -> bool:
        """True when NSE answers. Network-only — there is no key to check."""
        try:
            return bool(_rows(self._s.json("/api/all-upcoming-issues?category=ipo")))
        except Exception:
            return False

    # ── catalogue ─────────────────────────────────────────────────────────
    def _raw_catalogue(self) -> list[dict[str, Any]]:
        if self._catalogue is not None:
            return self._catalogue
        seen: dict[str, dict[str, Any]] = {}
        for path in ("/api/all-upcoming-issues?category=ipo",
                     "/api/ipo-current-issue"):
            try:
                rows = _rows(self._s.json(path))
            except Exception:
                continue
            for row in rows:
                symbol = (row.get("symbol") or "").strip()
                if symbol:
                    seen.setdefault(symbol, row)
        self._catalogue = list(seen.values())
        return self._catalogue

    def fetch_catalogue(self) -> list[dict[str, Any]]:
        out = []
        for row in self._raw_catalogue():
            company = (row.get("companyName") or "").strip()
            if not company:
                continue
            low, high = _band(row.get("issuePrice", ""))
            # NSE's series doubles as the board: EQ is the mainboard, SME is
            # NSE Emerge. Getting this wrong is not cosmetic — the detail
            # endpoint keys on series and answers an EQ query about an SME
            # issue with a full set of nulls rather than an error.
            series = (row.get("series") or "EQ").strip().upper()
            rec: dict[str, Any] = {
                "slug": slugify(company),
                "company": company,
                "board": "SME" if series == "SME" else "Mainboard",
                "symbol": row.get("symbol"),
                "series": series,
                "status": row.get("status"),
            }
            issue = {k: v for k, v in
                     (("price_low", low), ("price_high", high)) if v}
            # issueSize is a SHARE COUNT, not rupees — 24,956,363 for Dhoot,
            # not 24,956,363 crore. Multiplied by the upper band it gives the
            # total issue size, which is what the headline needs. It says
            # nothing about the fresh/OFS split, so that stays blank.
            shares = _int(str(row.get("issueSize") or ""))
            if shares and high:
                issue["total_cr"] = round(shares * high / 1e7, 2)
            if issue:
                rec["issue"] = issue
            dates = {k: v for k, v in (
                ("open", _date(row.get("issueStartDate", ""))),
                ("close", _date(row.get("issueEndDate", ""))),
            ) if v}
            if dates:
                rec["dates"] = dates
            out.append(rec)
        return out

    def _ref_for(self, slug: str) -> tuple[str, str] | None:
        """(symbol, series) — both are needed to address the detail endpoint."""
        for row in self._raw_catalogue():
            if slugify(row.get("companyName") or "") == slug:
                symbol = (row.get("symbol") or "").strip()
                if symbol:
                    return symbol, (row.get("series") or "EQ").strip().upper()
        return None

    # ── one IPO ───────────────────────────────────────────────────────────
    def fetch_ipo(self, slug: str) -> dict[str, Any]:
        base = next((r for r in self.fetch_catalogue() if r["slug"] == slug), None)
        if not base:
            return {}
        symbol, series = base.get("symbol"), base.get("series", "EQ")
        out = {k: v for k, v in base.items()
               if k not in ("symbol", "series", "status")}
        if not symbol:
            return out

        try:
            detail = self._s.json(
                f"/api/ipo-detail?symbol={symbol}&series={series}")
        except Exception:
            return out

        info = {}
        for row in _rows(detail.get("issueInfo")):
            title = (row.get("title") or "").strip()
            if title:
                info[title.lower()] = (row.get("value") or "").strip()

        issue = dict(out.get("issue") or {})
        for key, field in (("price range", "band"), ("bid lot", "lot"),
                           ("minimum order quantity", "lot"),
                           ("name of the registrar", "registrar")):
            raw = info.get(key)
            if not raw:
                continue
            if field == "band":
                low, high = _band(raw)
                if low:
                    issue["price_low"], issue["price_high"] = low, high
            elif field == "lot" and not issue.get("lot_size"):
                lot = _int(raw)
                if lot:
                    issue["lot_size"] = lot
            elif field == "registrar" and not issue.get("registrar"):
                issue["registrar"] = raw

        # Fresh vs OFS, out of the `Issue Size` prose. This is the number the
        # whole "company growth vs promoter exit" scene turns on, and it was
        # believed to be RHP-only — so it sat at 0/0 for every IPO and the
        # scene had to say "not disclosed". It is right here, keyless.
        #
        # It also corrects the headline size: the catalogue's `issueSize` is
        # the FRESH share count alone, so Molbio published ₹658 Cr against a
        # real ₹939.7 Cr. fresh+ofs is the true total, and compute.issue_metrics
        # already prefers that sum over `total_cr` when it exists.
        band = issue.get("price_high") or 0.0
        fresh, ofs = _split_from_issue_size(info.get("issue size", ""), band)
        if fresh:
            issue["fresh_cr"] = fresh
        if ofs:
            issue["ofs_cr"] = ofs
        if fresh or ofs:
            issue["total_cr"] = round(fresh + ofs, 2)

        if issue:
            out["issue"] = issue

        dates = dict(out.get("dates") or {})
        period = info.get("issue period", "")
        if " to " in period:
            start, _, end = period.partition(" to ")
            dates["open"] = _date(start) or dates.get("open")
            dates["close"] = _date(end) or dates.get("close")

        # NSE publishes the bidding window and nothing after it, which left
        # the whole post-close timeline blank on screen. SEBI's T+3 rule fixes
        # that calendar relative to the close, so derive it. `announced` is
        # not derivable from anything here and stays empty.
        close = dates.get("close")
        if close:
            for field, offset in (("allotment", 1), ("refund", 2), ("listing", 3)):
                if not dates.get(field):
                    derived = _working_days_after(close, offset)
                    if derived:
                        dates[field] = derived

        if dates:
            out["dates"] = {k: v for k, v in dates.items() if v}
        return out

    # ── GMP: deliberately empty ───────────────────────────────────────────
    def fetch_gmp(self, slug: str) -> list[dict[str, Any]]:
        """Always []. No exchange publishes grey-market data — see the module
        docstring. Returning nothing is the honest answer; inventing a number
        here would put an unsourced figure straight into the YAML."""
        return []

    # ── subscription ──────────────────────────────────────────────────────
    def fetch_subscription(self, slug: str) -> list[dict[str, Any]]:
        """Today's demand as a single row.

        NSE reports a live snapshot, not a per-day series, so this is day-N of
        the bidding window rather than a history. `merge_series` keys on `day`,
        so calling it daily builds the series one row at a time.
        """
        ref = self._ref_for(slug)
        if not ref:
            return []
        symbol, series = ref
        try:
            detail = self._s.json(
                f"/api/ipo-detail?symbol={symbol}&series={series}")
        except Exception:
            return []

        # Two schemas for the same thing. Mainboard fills bidDetails.noOfTime;
        # SME leaves it absent and reports the multiple as activeCat's
        # noOfTotalMeant instead. activeCat carries it on both, so prefer it
        # and keep bidDetails as the fallback.
        sources = ((_rows(detail.get("activeCat")), "noOfTotalMeant"),
                   (_rows(detail.get("bidDetails")), "noOfTime"))

        # Long category names, with sub-rows repeating under each heading. The
        # first match is always the heading itself, which is the one we want.
        wanted = (
            ("qib", "qualified institutional"),
            ("nii", "non institutional"),
            ("retail", "retail individual"),
            ("employee", "employee"),
            ("total", "total"),
        )

        row: dict[str, Any] = {}
        for rows, field_name in sources:
            for field, needle in wanted:
                if field in row:
                    continue
                for bid in rows:
                    if needle not in (bid.get("category") or "").lower():
                        continue
                    raw = bid.get(field_name)
                    # activeCat's first row is a header whose values are the
                    # column captions, not numbers.
                    if raw in (None, "") or not re.match(r"^[\d.]+$", str(raw)):
                        break
                    row[field] = _num(raw)
                    break

        # Before bidding opens, every category reads zero or null — which is
        # indistinguishable from "nobody has bid yet". Report nothing rather
        # than writing a row of zeros over a real figure.
        if not row or not any(row.values()):
            return []

        opened = None
        for rec in self.fetch_catalogue():
            if rec["slug"] == slug:
                opened = (rec.get("dates") or {}).get("open")
                break
        today = date.today()
        day = 1
        if opened:
            try:
                day = max(1, (today - date.fromisoformat(opened)).days + 1)
            except ValueError:
                day = 1

        row["day"] = day
        row["date"] = today.isoformat()
        return [row]
