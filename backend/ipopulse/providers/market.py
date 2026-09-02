"""The market as it stands this morning. Indices, sectors, movers, breadth.

Reel 7 is the first thing in this project that is not about an IPO. It is a
pre-market briefing: where the index closed, which sectors carried it, what
moved, and what the pre-open book says about the first minutes of trading.
This module supplies every *number* in it, and it supplies them all from the
exchange.

That last part is the whole design. Reel 7's hardest scene is ten intraday
setups with entry, target and stop levels, and a model asked for a price level
will produce one — fluently, plausibly, and from nothing. The house rule is
already written down for GMP (`no-gemini-invented-numbers`: a model's ₹0 was
being filed as a real premium of zero) and it applies with far more force to a
number a viewer might trade on. So the division of labour here is strict:

    this module          every price, level, percentage and count
    ai.py's generator    which candidates are interesting, and the words

A level the model states must be one this module computed. `levels()` exists
so there is a single arithmetic definition of support, resistance and stop,
derived from the day's own range and the previous close — reproducible, and
checkable against the exchange by anyone who doubts it.

── the endpoints ──────────────────────────────────────────────────────────

All keyless, all `www.nseindia.com`, all verified 2026-09-02:

    /api/allIndices                          139 indices, last + %change
    /api/market-data-pre-open?key=NIFTY      50-row pre-open book
    /api/live-analysis-variations?index=…    session gainers / losers
    /api/holiday-master?type=trading         the trading calendar

Two things about them that will otherwise cost somebody an afternoon:

- **`index=loosers`. Two o's.** That is NSE's spelling. `losers` returns
  nothing, silently — an empty losers scene rather than an error.
- **`/api/holiday-master` keys by segment.** `CM` is the cash market, which is
  the one that decides whether there is a session to brief. `FO`, `CD` and the
  rest are different calendars and mostly agree, but not always.

Reuses `_Session` from `scrape.py` rather than opening its own opener: that
class already carries the cookie jar NSE requires and a 0.7 s floor between
calls, and this module makes four calls where the IPO side makes one. Note
`json()` takes a **path** — it prepends the host, so a full URL builds a
nonsense hostname and fails with `getaddrinfo failed`, which looks exactly
like a network outage and is not one.
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .scrape import _Session

# IST, hardcoded. The whole module is about one exchange in one timezone, and
# a server drifting to UTC is how a briefing for Tuesday gets filed under
# Monday. Same reasoning as the IST pin in the verifier.
IST = timezone(timedelta(hours=5, minutes=30))

# 09:15 IST. Reel 7 is a *pre-market* briefing: after the bell its outlook
# scene is describing a session the viewer can already see, so this is the
# moment its content stops being a forecast. `readiness` uses it as the reel's
# expiry; nothing here enforces it.
MARKET_OPEN = (9, 15)

# The indices worth naming in a 30-second reel, in the order a trader reads
# them. `allIndices` returns 139 rows — every strategy, factor and thematic
# index NSE publishes — and a sector scene listing all of them says nothing.
# These eleven are the sectoral cuts a morning call actually turns on.
HEADLINE = ["NIFTY 50", "NIFTY BANK"]
SECTORS = [
    "NIFTY IT", "NIFTY BANK", "NIFTY AUTO", "NIFTY PHARMA", "NIFTY FMCG",
    "NIFTY METAL", "NIFTY REALTY", "NIFTY ENERGY", "NIFTY MEDIA",
    "NIFTY PSU BANK", "NIFTY FINANCIAL SERVICES",
]

CACHE_TTL = 300          # seconds. The board moves all day; a briefing is
                         # built once. Five minutes is enough to stop a
                         # multi-step build hitting the same path four times.

_cache: dict[str, tuple[float, Any]] = {}
_session: _Session | None = None


def _get(path: str) -> Any:
    """One NSE call, cached briefly, or None if it fails.

    Returns None rather than raising, deliberately. A briefing missing its
    losers scene is worth publishing; one that failed to build because a
    single path 500'd is not. Every caller below treats None as "that scene
    has no data" — which is the same absence-is-not-zero rule the sheet
    applies to a blank cell.
    """
    global _session
    hit = _cache.get(path)
    if hit and time.monotonic() - hit[0] < CACHE_TTL:
        return hit[1]
    if _session is None:
        _session = _Session()
    try:
        payload = _session.json(path)
    except Exception:
        return None
    _cache[path] = (time.monotonic(), payload)
    return payload


def _f(val: Any) -> float:
    """A number out of NSE's display strings. 0.0 when there isn't one.

    NSE writes numbers as text with thousands separators ('57,172.00') and
    uses '-' for a value it has no figure for. `float()` on either raises, and
    a raise here would take down the whole briefing for one bad cell.
    """
    if isinstance(val, (int, float)):
        return float(val)
    text = str(val or "").replace(",", "").replace("%", "").strip()
    if not text or text in ("-", "--", "NA"):
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


# ── the trading calendar ───────────────────────────────────────────────────

def trading_day(day: date | None = None) -> dict[str, Any]:
    """Is there a session on `day`? And if not, why not.

    Asked before anything else is built. A briefing generated on Republic Day
    would be a page of yesterday's numbers presented as this morning's, which
    is precisely the failure mode `monitor` was written to catch on the IPO
    side — data that did not change, published as though it had.

    Falls open when the endpoint is unreachable: a weekday with an unknown
    holiday list is treated as a trading day, because refusing to brief on a
    real session is the worse of the two errors and the reel is reviewed by a
    human before it is published either way.
    """
    day = day or datetime.now(IST).date()
    if day.weekday() >= 5:
        return {"trading": False, "why": "weekend", "day": day.isoformat()}
    payload = _get("/api/holiday-master?type=trading")
    rows = (payload or {}).get("CM") or []
    for row in rows:
        # 'tradingDate' is the holiday's date, spelled '15-Jan-2026'.
        raw = str(row.get("tradingDate") or "").strip()
        try:
            when = datetime.strptime(raw, "%d-%b-%Y").date()
        except ValueError:
            continue
        if when == day:
            return {"trading": False, "day": day.isoformat(),
                    "why": (row.get("description") or "trading holiday").strip()}
    return {"trading": True, "why": "", "day": day.isoformat(),
            "calendar": bool(rows)}


# ── indices and sectors ────────────────────────────────────────────────────

def indices() -> dict[str, Any]:
    """Every index NSE publishes, keyed by name, plus the day's breadth.

    `advances` / `declines` come back on the same payload as the levels, so
    breadth — how many of the 50 rose against how many fell — costs nothing
    extra. It is the one number that separates "the index fell" from "the
    market fell": an index can drop on two heavyweights while most of its
    constituents rise, and a briefing that cannot tell those apart is
    describing the wrong day.
    """
    payload = _get("/api/allIndices")
    if not payload:
        return {}
    rows = payload.get("data") or []
    out: dict[str, Any] = {}
    for row in rows:
        name = str(row.get("index") or "").strip()
        if not name:
            continue
        out[name] = {
            "name": name,
            "last": _f(row.get("last")),
            "prev_close": _f(row.get("previousClose")),
            "open": _f(row.get("open")),
            "high": _f(row.get("high")),
            "low": _f(row.get("low")),
            "pct": _f(row.get("percentChange")),
            # NSE reports a 52-week range on the index rows too, which is what
            # lets the outlook scene say where today sits in the year.
            "year_high": _f(row.get("yearHigh")),
            "year_low": _f(row.get("yearLow")),
        }
    return {
        "at": str(payload.get("timestamp") or ""),
        "advances": int(_f(payload.get("advances"))),
        "declines": int(_f(payload.get("declines"))),
        "unchanged": int(_f(payload.get("unchanged"))),
        "index": out,
    }


def sectors(snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    """The eleven sectoral indices, sorted strongest first.

    Sorted rather than returned in a fixed order because the scene's whole
    job is the ranking — "which sectors look good and bad today" is answered
    by the ends of this list, and a caller should not have to sort it again to
    find them.

    A sector absent from the payload is left out rather than given a 0%. A
    zero here would render as a flat sector on the card, which is a claim; the
    truth is that NSE did not report it.
    """
    snap = snapshot if snapshot is not None else indices()
    idx = snap.get("index") or {}
    rows = [idx[name] for name in SECTORS if name in idx]
    rows.sort(key=lambda r: r["pct"], reverse=True)
    return {
        "all": rows,
        "strong": [r for r in rows if r["pct"] > 0][:3],
        "weak": [r for r in rows if r["pct"] < 0][-3:][::-1],
    }


# ── the pre-open book ──────────────────────────────────────────────────────

def pre_open() -> dict[str, Any]:
    """The NIFTY 50 pre-open call auction: where the day is set to start.

    This is the only feed here that is genuinely forward-looking. Between
    09:00 and 09:08 IST the exchange collects orders without matching them and
    publishes the indicative price for each stock; the reel's "how does the
    market open" scene is that, and nothing else in this module can answer it.

    Outside that window it returns the last auction's result, which is still
    the right answer for a briefing written at 08:00 about a session that has
    not started — it is the previous day's open, clearly stamped. Callers get
    `at` so they can say which.
    """
    payload = _get("/api/market-data-pre-open?key=NIFTY")
    if not payload:
        return {}
    rows = []
    for row in payload.get("data") or []:
        meta = row.get("metadata") or {}
        sym = str(meta.get("symbol") or "").strip()
        if not sym:
            continue
        rows.append({
            "symbol": sym,
            "last": _f(meta.get("lastPrice")),
            "prev_close": _f(meta.get("previousClose")),
            "change": _f(meta.get("change")),
            "pct": _f(meta.get("pChange")),
            "qty": _f(meta.get("finalQuantity")),
        })
    rows.sort(key=lambda r: r["pct"], reverse=True)
    return {
        "at": str(payload.get("timestamp") or ""),
        "advances": int(_f(payload.get("advances"))),
        "declines": int(_f(payload.get("declines"))),
        "rows": rows,
        # The ends of the book, which is what the opening-direction scene
        # shows. Kept short: five is what fits a 9:16 frame at readable type.
        "up": rows[:5],
        "down": rows[-5:][::-1],
    }


# ── movers ─────────────────────────────────────────────────────────────────

def movers(bucket: str = "NIFTY") -> dict[str, Any]:
    """Session gainers and losers for one index bucket.

    `bucket` picks the universe: 'NIFTY', 'BANKNIFTY', 'NIFTYNEXT50',
    'FOSec' (the F&O list), 'allSec', or 'SecGtr20' / 'SecLwr20' — NSE's split
    of securities priced above and below ₹20. Default 'NIFTY', because a
    briefing whose movers are microcaps nobody can exit is not a briefing.

    Note the spelling of the losers endpoint. `index=loosers`, with two o's,
    is NSE's, and `losers` returns an empty body rather than a 404 — so
    getting it wrong produces a reel with no losers and no error anywhere.
    """
    out: dict[str, Any] = {"bucket": bucket, "gainers": [], "losers": []}
    for key, path in (("gainers", "gainers"), ("losers", "loosers")):
        payload = _get(f"/api/live-analysis-variations?index={path}")
        block = (payload or {}).get(bucket) or {}
        for row in (block.get("data") or [])[:10]:
            out[key].append({
                "symbol": str(row.get("symbol") or "").strip(),
                "last": _f(row.get("ltp") or row.get("lastPrice")),
                "prev_close": _f(row.get("prev_price") or row.get("previousPrice")),
                "pct": _f(row.get("perChange") or row.get("pChange")),
                "high": _f(row.get("high_price") or row.get("dayHigh")),
                "low": _f(row.get("low_price") or row.get("dayLow")),
                "volume": _f(row.get("trade_quantity") or row.get("totalTradedVolume")),
            })
        out[key].sort(key=lambda r: r["pct"], reverse=(key == "gainers"))
    return out


# ── levels ─────────────────────────────────────────────────────────────────
#
# The one place in the pipeline that produces a price a viewer might act on,
# and therefore the one place that must not involve a model at all.
#
# Every figure below is arithmetic on numbers the exchange published: the
# session's own high, low and close, and the previous close. That is a
# deliberate ceiling on the claim being made. This is not a prediction and the
# reel must not present it as one — it is the day's pivot band, the oldest and
# most widely published intraday reference there is, computed the standard way
# so that a viewer who checks it gets the same numbers.
#
# Why the classic floor pivot and not something cleverer: it is reproducible
# from public data with no parameters to tune, so there is nothing here that
# could be quietly fitted to make a past call look good. A model-chosen level,
# or one from a fitted indicator, cannot make that claim.

def levels(row: dict[str, Any]) -> dict[str, Any]:
    """The day's pivot band for one stock, from its own high/low/close.

    Takes a row from `movers()` or `pre_open()` — anything carrying `last`,
    `high` and `low`. Returns {} when the range is missing rather than
    inventing one from the last price: a pivot computed off a zero high is a
    number that looks like a level and is not one.

    `stop_long` / `stop_short` are placed just beyond S1 / R1 rather than at
    them, because a stop resting exactly on a widely published level is the
    one most likely to be taken out by noise before the move it was protecting
    against actually happens.
    """
    high, low, close = _f(row.get("high")), _f(row.get("low")), _f(row.get("last"))
    if not (high and low and close) or high <= low:
        return {}
    pivot = (high + low + close) / 3
    span = high - low
    r1, s1 = 2 * pivot - low, 2 * pivot - high
    out = {
        "pivot": round(pivot, 2),
        "r1": round(r1, 2), "r2": round(pivot + span, 2),
        "s1": round(s1, 2), "s2": round(pivot - span, 2),
        "range_pct": round(100 * span / close, 2),
        # Where in the day's range it closed. 1.0 = on the high, 0.0 = on the
        # low. The single most useful number for a morning call: a stock that
        # closed on its high after a wide range is a different setup from one
        # that closed mid-range, and the percentage change alone cannot tell
        # them apart.
        "close_pos": round((close - low) / span, 2),
        "stop_long": round(s1 - 0.25 * (close - s1), 2) if close > s1 else round(s1, 2),
        "stop_short": round(r1 + 0.25 * (r1 - close), 2) if r1 > close else round(r1, 2),
    }
    return out


def snapshot() -> dict[str, Any]:
    """Everything the briefing needs, in one call, with levels attached.

    Assembled here rather than in the generator so there is exactly one
    definition of "the market this morning" — the AI step, the CLI and any
    future check all read the same dict, and a number that appears in the reel
    can be traced to the endpoint that produced it.

    `partial` names what failed. A briefing built on three of four feeds is
    publishable and should say so; one that silently dropped its losers scene
    is the kind of gap that reaches a video.
    """
    day = trading_day()
    snap = indices()
    pre = pre_open()
    mv = movers()
    for group in ("gainers", "losers"):
        for row in mv.get(group) or []:
            row["levels"] = levels(row)
    missing = [name for name, got in (("indices", snap), ("pre_open", pre),
                                      ("movers", mv.get("gainers")))
               if not got]
    head = {name: (snap.get("index") or {}).get(name, {}) for name in HEADLINE}
    return {
        "day": day["day"],
        "trading": day["trading"],
        "why_closed": day["why"],
        "at": snap.get("at") or pre.get("at") or "",
        "headline": head,
        "breadth": {"advances": snap.get("advances", 0),
                    "declines": snap.get("declines", 0),
                    "unchanged": snap.get("unchanged", 0)},
        "sectors": sectors(snap),
        "pre_open": pre,
        "movers": mv,
        "partial": missing,
    }


if __name__ == "__main__":                     # a quick look, by hand
    print(json.dumps(snapshot(), indent=1, default=str)[:4000])
