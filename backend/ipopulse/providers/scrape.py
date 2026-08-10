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
from datetime import date, datetime
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
        if issue:
            out["issue"] = issue

        period = info.get("issue period", "")
        if " to " in period:
            start, _, end = period.partition(" to ")
            dates = dict(out.get("dates") or {})
            dates["open"] = _date(start) or dates.get("open")
            dates["close"] = _date(end) or dates.get("close")
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
