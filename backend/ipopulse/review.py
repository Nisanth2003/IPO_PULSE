"""Did the morning briefing hold up? Reel 7, marked against the tape.

Reel 7 goes out at 08:00 IST with a bias call and a set of intraday levels.
Nothing in this project ever checked what happened next, so "how much of this
is worth trusting" had no answer — which is the one question a viewer acting
on those levels actually has.

This module answers it from the exchange's own end-of-day file.

── The source, and why this one ───────────────────────────────────────────

NSE's daily bhavcopy: one CSV per session, every symbol, official OPEN /
HIGH / LOW / CLOSE.

    https://nsearchives.nseindia.com/products/content/
        sec_bhavdata_full_DDMMYYYY.csv

The two live endpoints this project already uses cannot do this job.
`/api/live-analysis-variations` caps at ~20 gainers and ~20 losers per bucket,
so a setup whose symbol finished mid-pack is simply absent — and "absent"
correlates with "did not move", which is exactly the outcome a scorecard must
not silently drop. `/api/quote-equity` and `/api/equity-stockIndices` return
nothing to this client at all (checked 8 Sep 2026). The bhavcopy has every
symbol, carries the official close rather than a last-traded print, and — the
property that matters most here — anybody can download the same file and
re-run this arithmetic. A scorecard nobody can audit is marketing.

Cached per day under `data/cache/bhav/`, because a published session is
immutable. Re-scoring a week costs one fetch per day, once, ever.

── The honesty rule that shapes the verdicts ──────────────────────────────

OHLC says whether a price was touched. It cannot say in what ORDER. So a day
that touched both the target and the stop is `unresolved`, and never counted
as a win. Everything else here follows from that one line:

    target      the target was reached and the stop never was
    stopped     the stop was hit and the target never was
    unresolved  both touched; the tape cannot say which came first
    open        entry traded, neither level reached — closed in between
    no-trade    the entry price never traded; there was no position to have
    no-data     the symbol is not in that day's bhavcopy

`hit_rate` is computed over target + stopped only. Counting `no-trade` as
anything would flatter the record, and counting `unresolved` as a win would
be inventing a result the data does not contain.

── The diagnostic, which is the actually useful part ──────────────────────

A verdict says what happened. `stop_share` says why: the stop's distance from
the entry, as a fraction of the range the stock actually traded that day. A
stop sitting inside a third of the day's range, with an entry at the pivot —
which is mid-range by construction — will be touched by ordinary noise before
the target is approached. That is a property of the setup's geometry, not of
the market's mood, and it is visible in one number per setup.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta
from typing import Any

from .providers.market import IST

BHAV_URL = ("https://nsearchives.nseindia.com/products/content/"
            "sec_bhavdata_full_{stamp}.csv")

# The index equivalent. Same archive, different shape: one row per index with
# OHLC, points change and Change(%) — the exchange's own settled numbers,
# which is what "NIFTY closed at" has to mean.
INDEX_URL = ("https://nsearchives.nseindia.com/content/indices/"
             "ind_close_all_{stamp}.csv")

# The file names indices in title case ("Nifty 50"); every other part of the
# codebase uses NSE's live-feed spelling ("NIFTY 50"). Upper-casing on read
# makes the two agree without a lookup table.

# Only the cash-market equity series. The same file carries government
# securities, ETFs and the rest; scoring an equity setup against a GS row
# would be arithmetic on the wrong instrument.
SERIES = "EQ"

TARGET, STOPPED, UNRESOLVED, OPEN, NO_TRADE, NO_DATA = (
    "target", "stopped", "unresolved", "open", "no-trade", "no-data")

# Voided by the opening print. `VOID` is the open already past the stop —
# never a trade, so never a loss. `GAPPED` is the open already past the
# target — the read was right and there was no entry to be had.
VOID, GAPPED = "void", "gapped-away"

# Only these two count towards the hit rate. Everything else either never
# traded or cannot be ordered from OHLC, and folding any of it in would be
# scoring the setup on days it was not in the market.
RESOLVED = (TARGET, STOPPED)

# The sector indices, and the constituent list that names their members.
# Both keyless, both from the same archive as the bhavcopy.
SECTOR_LISTS = {
    "NIFTY BANK": "niftybanklist",
    "NIFTY IT": "niftyitlist",
    "NIFTY AUTO": "niftyautolist",
    "NIFTY PHARMA": "niftypharmalist",
    "NIFTY FMCG": "niftyfmcglist",
    "NIFTY METAL": "niftymetallist",
    "NIFTY REALTY": "niftyrealtylist",
    "NIFTY ENERGY": "niftyenergylist",
    "NIFTY FINANCIAL SERVICES": "niftyfinancelist",
    "NIFTY MEDIA": "niftymedialist",
    "NIFTY CONSUMER DURABLES": "niftyconsumerdurableslist",
    "NIFTY HEALTHCARE INDEX": "niftyhealthcarelist",
    "NIFTY OIL & GAS": "niftyoilgaslist",
    "NIFTY INFRASTRUCTURE": "niftyinfralist",
}

CONSTITUENTS_URL = ("https://nsearchives.nseindia.com/content/indices/"
                    "ind_{name}.csv")

# Tradeable universes for candidate selection, by the same constituent-list
# mechanism as the sectors. NIFTY 50 is the default and the reason is the one
# `movers(bucket="NIFTY")` gave: a briefing whose movers are microcaps nobody
# can exit is not a briefing.
UNIVERSE_LISTS = {
    "NIFTY50": "nifty50list",
    "NIFTYNEXT50": "niftynext50list",
    "NIFTY100": None,                 # the two above, combined
}

# What to CALL each sector in a sentence. Derived names do not survive
# contact with acronyms — str.title() renders "NIFTY IT" as "It" — and these
# go into a voiceover, so they are written out rather than computed.
SECTOR_LABEL = {
    "NIFTY BANK": "banks",
    "NIFTY IT": "IT",
    "NIFTY AUTO": "autos",
    "NIFTY PHARMA": "pharma",
    "NIFTY FMCG": "FMCG",
    "NIFTY METAL": "metals",
    "NIFTY REALTY": "realty",
    "NIFTY ENERGY": "energy",
    "NIFTY FINANCIAL SERVICES": "financials",
    "NIFTY MEDIA": "media",
    "NIFTY CONSUMER DURABLES": "consumer durables",
    "NIFTY HEALTHCARE INDEX": "healthcare",
    "NIFTY OIL & GAS": "oil and gas",
    "NIFTY INFRASTRUCTURE": "infrastructure",
}

# How far a move has to go before it is offered as an explanation. Below this
# the honest answer is "nothing moved against us", which points the finger at
# the setup rather than at the market — and that is the finding, so it must
# not be dressed up as a sector story.
MATERIAL_PCT = 0.5


def _cache_path(day: str, kind: str = "bhav"):
    from .store import CACHE_DIR

    return CACHE_DIR / kind / f"{day}.csv"


def _fetch_csv(day: str, url: str, marker: str, kind: str,
               force: bool = False) -> str:
    """One session's CSV, from the cache or the archive. "" when unpublished.

    `marker` is a string the real file's header carries. NSE answers a missing
    file with an HTML error page and a 200, so the header check is what
    separates "not published yet" from "published and empty" — and an error
    page must never reach the cache, or the day is poisoned until someone
    clears it by hand.
    """
    path = _cache_path(day, kind)
    if path.exists() and not force:
        try:
            body = path.read_text(encoding="utf-8")
            if body:
                return body
        except OSError:
            pass
    try:
        stamp = datetime.strptime(day, "%Y-%m-%d").strftime("%d%m%Y")
    except ValueError:
        return ""
    from .providers import scrape

    try:
        body = scrape._Session()._get(url.format(stamp=stamp), timeout=40)
    except Exception:                                         # noqa: BLE001
        return ""
    if marker not in body[:300]:
        return ""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    except OSError:
        pass                              # a cache that cannot be written is fine
    return body


def universe(name: str = "NIFTY50", force: bool = False) -> list[str]:
    """The symbols in one index, from NSE's own constituent list.

    Cached under a fixed key like `sector_map`: membership changes on a
    quarterly review, and a stale roster is a far smaller error than
    refetching on every run. Empty list when the file cannot be read, and
    callers must treat that as "cannot select" rather than falling back to
    every symbol in the bhavcopy — a top-ten mover list drawn from the whole
    cash market is microcaps and nothing else.
    """
    import json

    from .store import CACHE_DIR

    name = name.upper()
    if name == "NIFTY100":
        return universe("NIFTY50", force) + universe("NIFTYNEXT50", force)
    listing = UNIVERSE_LISTS.get(name)
    if not listing:
        return []

    path = CACHE_DIR / "index" / f"universe-{name}.json"
    if path.exists() and not force:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass

    from .providers import scrape

    try:
        body = scrape._Session()._get(
            CONSTITUENTS_URL.format(name=listing), timeout=30)
    except Exception:                                         # noqa: BLE001
        return []
    if "Symbol" not in body[:200]:
        return []
    out = []
    for row in csv.DictReader(io.StringIO(body), skipinitialspace=True):
        sym = (row.get("Symbol") or "").strip().upper()
        if sym:
            out.append(sym)
    if out:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(out, indent=1), encoding="utf-8")
        except OSError:
            pass
    return out


def settled_movers(day: str, name: str = "NIFTY50",
                   top: int = 10) -> dict[str, Any]:
    """The previous session's biggest movers, from the exchange's own file.

    The settled counterpart to `providers.market.movers`. Same row shape, so
    it drops into the snapshot unchanged, plus `volume` and `delivery` which
    the live feed does not carry.

    Percentage change is computed from the bhavcopy's own PREV_CLOSE rather
    than from the session before it, so a corporate action that the exchange
    adjusted for is adjusted here too — deriving it from two days of closes
    would report a bonus issue as a 50% crash.
    """
    prev = previous_session(day)
    if not prev:
        return {"date": "", "universe": name, "gainers": [], "losers": []}
    tape = bhavcopy(prev)
    syms = universe(name)
    rows = []
    for sym in syms:
        t = tape.get(sym)
        if not t or not t["prev"]:
            continue
        rows.append({
            "symbol": sym,
            "last": t["close"], "prev_close": t["prev"],
            "pct": round(100 * (t["close"] - t["prev"]) / t["prev"], 2),
            "high": t["high"], "low": t["low"],
            "volume": t["volume"], "delivery": t["delivery"],
        })
    rows.sort(key=lambda r: r["pct"], reverse=True)
    return {"date": prev, "universe": name,
            "gainers": rows[:top],
            # Reversed so the worst is first, matching the live feed's order.
            "losers": list(reversed(rows[-top:])) if len(rows) > top else []}


# Sessions a swing position is given to resolve. Five, about a trading week:
# the shortest hold the swing description fits. A longer default would let
# every failed call sit in "still open" instead of being counted.
SWING_HORIZON = 5

# A position still open when the horizon expires.
EXPIRED = "expired"


def next_sessions(day: str, count: int) -> list[str]:
    """Up to `count` trading sessions from `day` forward, `day` included.

    Walks the calendar looking for a published index file, the same way
    `previous_session` walks backwards, so weekends and exchange holidays are
    skipped without this module needing its own holiday list. Stops early at
    the edge of the archive, which is what bounds a horizon that runs into
    the future: a position opened on Friday can only be scored as far as the
    exchange has published.
    """
    try:
        d = datetime.strptime(day, "%Y-%m-%d").date()
    except ValueError:
        return []
    out: list[str] = []
    # `count * 2 + 10` calendar days is enough slack for two weekends and a
    # holiday cluster; beyond that the archive has simply not caught up.
    for step in range(count * 2 + 10):
        if len(out) >= count:
            break
        cand = (d + timedelta(days=step)).isoformat()
        if index_close(cand):
            out.append(cand)
    return out


def score_swing(setup: Any, day: str,
                horizon: int = SWING_HORIZON) -> dict[str, Any]:
    """One setup held across sessions until it resolves or the horizon ends.

    The intraday scorer answers "did this work today". This answers "did it
    work at all, and what did holding it cost" — a different question, and it
    needs the honesty rules in this module's header: resolution is decided by
    the first session that touches a level, an overnight gap past the stop
    exits at the OPEN rather than at the stop, and a position still open when
    the horizon expires is `expired`, not a win in waiting.
    """
    side = (_text(setup, "side") or "long").lower()
    long_ = side != "short"
    entry, target, stop = (_num(setup, "entry"), _num(setup, "target"),
                           _num(setup, "stop"))
    out: dict[str, Any] = {
        "symbol": _text(setup, "symbol").upper(), "side": side,
        "entry": entry, "target": target, "stop": stop,
        "horizon": horizon, "verdict": "", "why": "",
        "entered_on": "", "exit_on": "", "exit": 0.0,
        "sessions_held": 0, "slippage": 0.0, "gapped_exit": False,
        "return_pct": None,
    }
    if not (entry and target and stop):
        out.update(verdict=NO_DATA, why="the setup carried no levels")
        return out

    days = next_sessions(day, horizon)
    if not days:
        out.update(verdict=NO_DATA,
                   why=f"no settled session at or after {day}")
        return out

    entered = False
    for n, d in enumerate(days, 1):
        tape = bhavcopy(d).get(out["symbol"])
        if not tape:
            continue                      # not traded that session

        if not entered:
            # The entry is a resting order at the pivot: it fills on the
            # first session whose range reaches it. Unlike the intraday
            # scorer there is no rush — that is the whole point of a horizon.
            #
            # The fill is booked AT the entry even when the session opened
            # beyond it, where a resting order would really have filled at
            # the better opening price. That understates the result, and it
            # is deliberate: the bias has to point away from flattering the
            # record, and unlike the exit gap — which is booked honestly at
            # the open, against us — an optimistic entry has no offsetting
            # cost to a viewer who acts on it.
            if not (tape["low"] <= entry <= tape["high"]):
                continue
            entered = True
            out["entered_on"] = d

        out["sessions_held"] = n
        opened = tape["open"]

        # A gap past a level is an exit AT THE OPEN. Only from the session
        # after entry: on the entry session the fill and the gap cannot be
        # ordered, so that case falls through to the touch tests below.
        if d != out["entered_on"]:
            if (opened <= stop) if long_ else (opened >= stop):
                out.update(verdict=STOPPED, exit_on=d, exit=opened,
                           gapped_exit=True,
                           slippage=round(abs(opened - stop), 2),
                           why=f"gapped through the {stop:g} stop and left at "
                               f"the {opened:g} open, {abs(opened - stop):g} "
                               f"worse than the stop")
                break
            if (opened >= target) if long_ else (opened <= target):
                out.update(verdict=TARGET, exit_on=d, exit=opened,
                           gapped_exit=True,
                           slippage=round(abs(opened - target), 2),
                           why=f"gapped past the {target:g} target and left "
                               f"at the {opened:g} open")
                break

        hit_t = tape["high"] >= target if long_ else tape["low"] <= target
        hit_s = tape["low"] <= stop if long_ else tape["high"] >= stop
        if hit_t and hit_s:
            out.update(verdict=UNRESOLVED, exit_on=d, exit=tape["close"],
                       why=f"{d} touched both levels; within one session the "
                           f"tape cannot say which came first")
            break
        if hit_t:
            out.update(verdict=TARGET, exit_on=d, exit=target,
                       why=f"reached {target:g} on {d}")
            break
        if hit_s:
            out.update(verdict=STOPPED, exit_on=d, exit=stop,
                       why=f"broke {stop:g} on {d}")
            break
    else:
        if entered:
            last = days[-1]
            tape = bhavcopy(last).get(out["symbol"]) or {}
            close = tape.get("close", 0.0)
            out.update(verdict=EXPIRED, exit_on=last, exit=close,
                       why=f"still open after {len(days)} session(s); marked "
                           f"to the {close:g} close on {last}")
        else:
            out.update(verdict=NO_TRADE,
                       why=f"{entry:g} never traded in {len(days)} session(s) "
                           f"from {day}")

    if out["exit"] and entry:
        gain = (out["exit"] - entry) if long_ else (entry - out["exit"])
        out["return_pct"] = round(100 * gain / entry, 2)
    return out


def swing(day: str, horizon: int = SWING_HORIZON) -> dict[str, Any]:
    """One stored briefing's setups, scored on a multi-session horizon."""
    from . import briefing

    out: dict[str, Any] = {"date": day, "horizon": horizon, "setups": [],
                           "counts": {}, "skipped": "", "contaminated": False,
                           "sessions": []}
    try:
        brief = briefing.load(day)
    except FileNotFoundError:
        out["skipped"] = f"no briefing stored for {day}"
        return out

    blocked = lookahead(brief, day)
    if blocked:
        out.update(skipped=blocked, contaminated=True)
        return out

    out["sessions"] = next_sessions(day, horizon)
    if not out["sessions"]:
        out["skipped"] = f"no settled session at or after {day}"
        return out
    # An honest horizon needs the whole window published. Scoring a 5-session
    # hold against 2 available sessions would report every unresolved position
    # as expired, which reads as a loss.
    if len(out["sessions"]) < horizon:
        out["partial"] = (f"only {len(out['sessions'])} of {horizon} sessions "
                          f"have settled; positions still open are not final")

    for setup in (getattr(brief, "setups", None) or []):
        out["setups"].append(score_swing(setup, day, horizon))
    for row in out["setups"]:
        out["counts"][row["verdict"]] = out["counts"].get(row["verdict"], 0) + 1

    resolved = [r for r in out["setups"] if r["verdict"] in RESOLVED]
    out["resolved"] = len(resolved)
    out["hit_rate"] = (round(100 * sum(1 for r in resolved
                                       if r["verdict"] == TARGET)
                             / len(resolved), 1) if resolved else None)
    rets = [r["return_pct"] for r in out["setups"]
            if r["return_pct"] is not None]
    # The average outcome per position TAKEN, losers included. A hit rate
    # alone cannot distinguish a strategy with small wins and large losses
    # from its opposite.
    out["avg_return_pct"] = (round(sum(rets) / len(rets), 2) if rets else None)
    out["gapped_exits"] = sum(1 for r in out["setups"] if r["gapped_exit"])
    out["total_slippage"] = round(sum(r["slippage"] for r in out["setups"]), 2)
    held = [r["sessions_held"] for r in out["setups"] if r["sessions_held"]]
    out["avg_sessions_held"] = (round(sum(held) / len(held), 1)
                                if held else None)
    return out


def swing_report(r: dict[str, Any]) -> list[str]:
    """One day's swing scoring, as lines."""
    if r.get("skipped"):
        return [f"── {r['date']} (swing): {r['skipped']}"]
    out = [f"── {r['date']}  ·  {r['horizon']}-session horizon  ·  "
           f"{len(r['setups'])} setup(s)"]
    if r.get("partial"):
        out.append(f"   ! {r['partial']}")
    out.append(f"   sessions: {', '.join(r['sessions'])}")
    out.append("")
    for s in r["setups"]:
        ret = "" if s["return_pct"] is None else f"{s['return_pct']:+.2f}%"
        out.append(f"   {s['symbol']:<12} {s['side']:<6} "
                   f"{s['verdict']:<10} {ret:>8}  "
                   f"held {s['sessions_held']} session(s)")
        if s["why"]:
            out.append(f"        {s['why']}")
        if s["gapped_exit"]:
            out.append(f"        ! exited on a gap, {s['slippage']:g} worse "
                       f"than the level — a stop is not a fill overnight")
    out.append("")
    out.append(f"   verdicts: {r['counts']}")
    if r["resolved"]:
        out.append(f"   resolved {r['resolved']}, hit rate {r['hit_rate']}%")
    if r["avg_return_pct"] is not None:
        out.append(f"   average return per position taken: "
                   f"{r['avg_return_pct']:+.2f}%  (losers included)")
    if r["avg_sessions_held"]:
        out.append(f"   average hold: {r['avg_sessions_held']} session(s)")
    if r["gapped_exits"]:
        out.append(f"   {r['gapped_exits']} of {len(r['setups'])} exited on an "
                   f"overnight gap rather than at their level")
    return out


def swing_record(days: list[str] | None = None,
                 horizon: int = SWING_HORIZON) -> dict[str, Any]:
    """The running swing record across every stored briefing.

    Reports the average return per position alongside the hit rate, because
    the two answer different questions and a swing hold makes the gap between
    them wide. A 30% hit rate with targets three times the stop distance beats
    a 60% one with the reverse, and a hit rate on its own cannot tell them
    apart — over a multi-session hold, where the gaps land decides which of
    those two a strategy actually is.
    """
    from . import briefing

    days = days or briefing.list_days()
    per_day, setups = [], []
    for day in days:
        r = swing(day, horizon)
        if r.get("skipped"):
            per_day.append({"date": day, "skipped": r["skipped"],
                            "contaminated": r.get("contaminated", False)})
            continue
        per_day.append({"date": day, "counts": r["counts"],
                        "hit_rate": r["hit_rate"], "resolved": r["resolved"],
                        "avg_return_pct": r["avg_return_pct"],
                        "partial": r.get("partial", "")})
        setups.extend(r["setups"])

    counts: dict[str, int] = {}
    for row in setups:
        counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
    resolved = sum(counts.get(v, 0) for v in RESOLVED)
    rets = [r["return_pct"] for r in setups if r["return_pct"] is not None]
    wins = [x for x in rets if x > 0]
    losses = [x for x in rets if x <= 0]
    held = [r["sessions_held"] for r in setups if r["sessions_held"]]
    return {
        "horizon": horizon,
        "days": per_day,
        "sessions_scored": sum(1 for d in per_day if not d.get("skipped")),
        "excluded": sum(1 for d in per_day if d.get("contaminated")),
        "setups": len(setups),
        "counts": counts,
        "resolved": resolved,
        "hit_rate": (round(100 * counts.get(TARGET, 0) / resolved, 1)
                     if resolved else None),
        "expired": counts.get(EXPIRED, 0),
        "avg_return_pct": (round(sum(rets) / len(rets), 2) if rets else None),
        "avg_win_pct": (round(sum(wins) / len(wins), 2) if wins else None),
        "avg_loss_pct": (round(sum(losses) / len(losses), 2)
                         if losses else None),
        "avg_sessions_held": (round(sum(held) / len(held), 1)
                              if held else None),
        # How often an overnight gap decided the exit instead of the level.
        # The number that says whether a swing hold is being priced honestly.
        "gapped_exits": sum(1 for r in setups if r["gapped_exit"]),
        "total_slippage": round(sum(r["slippage"] for r in setups), 2),
    }


def swing_record_report(r: dict[str, Any]) -> list[str]:
    """The running swing record, as lines."""
    out = [f"── Swing record  ·  {r['horizon']}-session horizon  ·  "
           f"{r['sessions_scored']} session(s), {r['setups']} setup(s)"]
    if r.get("excluded"):
        out.append(f"   ! {r['excluded']} briefing(s) excluded for hindsight. "
                   f"Nothing here is a track record until the first briefing "
                   f"built from settled inputs has been scored.")
    for day in r["days"]:
        if day.get("skipped"):
            flag = "EXCLUDED " if day.get("contaminated") else ""
            out.append(f"   {day['date']}  {flag}{day['skipped']}")
        else:
            ret = ("—" if day["avg_return_pct"] is None
                   else f"{day['avg_return_pct']:+.2f}%")
            out.append(f"   {day['date']}  hit "
                       f"{day['counts'].get(TARGET, 0)}/{day['resolved']}  "
                       f"avg {ret}   {day['counts']}")
    if not r["setups"]:
        return out
    out.append("")
    out.append(f"   verdicts: {r['counts']}")
    if r["resolved"]:
        out.append(f"   resolved {r['resolved']}, hit rate {r['hit_rate']}% "
                   f"({r['counts'].get(TARGET, 0)} of {r['resolved']})")
    if r["expired"]:
        out.append(f"   {r['expired']} still open when the horizon expired — "
                   f"counted as neither, marked to the last close")
    if r["avg_return_pct"] is not None:
        out.append("")
        out.append(f"   average per position   : {r['avg_return_pct']:+.2f}% "
                   f"(every position taken, losers included)")
    if r["avg_win_pct"] is not None and r["avg_loss_pct"] is not None:
        out.append(f"   average win vs loss    : {r['avg_win_pct']:+.2f}% "
                   f"against {r['avg_loss_pct']:+.2f}%")
        # The reason both numbers are printed: this ratio is what decides
        # whether a low hit rate is a problem or the design.
        if r["avg_loss_pct"]:
            edge = abs(r["avg_win_pct"] / r["avg_loss_pct"])
            out.append(f"   win/loss size ratio    : {edge:.2f}x — a hit rate "
                       f"below {100 / (1 + edge):.0f}% loses money at this "
                       f"ratio")
    if r["avg_sessions_held"]:
        out.append(f"   average hold           : {r['avg_sessions_held']} "
                   f"session(s) of {r['horizon']}")
    if r["gapped_exits"]:
        out.append(f"   exits decided by a gap : {r['gapped_exits']} of "
                   f"{r['setups']}, {r['total_slippage']:g} total worse than "
                   f"the levels — overnight risk a day trade never carries")
    return out


def sector_map(force: bool = False) -> dict[str, dict[str, str]]:
    """{SYMBOL: {"index": "NIFTY IT", "industry": "Information Technology"}}.

    Built from NSE's own index constituent lists, so nothing is guessed. A
    symbol in several sector indices keeps the FIRST it is found in, and the
    lists are walked narrowest-first: NIFTY BANK before NIFTY FINANCIAL
    SERVICES before NIFTY INFRASTRUCTURE, so HDFCBANK is attributed to banks
    rather than to the 30-stock infrastructure basket it also sits in.

    Cached under a fixed key rather than per day: constituents change on a
    quarterly review, and a stale membership is a far smaller error than
    re-downloading fifteen files on every scoring run.
    """
    from .store import CACHE_DIR

    path = CACHE_DIR / "index" / "sectors.json"
    if path.exists() and not force:
        try:
            import json

            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass                          # a bad cache is a reason to refetch

    from .providers import scrape

    session = scrape._Session()
    out: dict[str, dict[str, str]] = {}
    for index, name in SECTOR_LISTS.items():
        try:
            body = session._get(CONSTITUENTS_URL.format(name=name), timeout=30)
        except Exception:                                     # noqa: BLE001
            continue                      # one missing list is not a failure
        if "Symbol" not in body[:200]:
            continue
        for row in csv.DictReader(io.StringIO(body), skipinitialspace=True):
            sym = (row.get("Symbol") or "").strip().upper()
            if sym and sym not in out:
                out[sym] = {"index": index,
                            "industry": (row.get("Industry") or "").strip()}
    if out:
        try:
            import json

            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(out, indent=1), encoding="utf-8")
        except OSError:
            pass
    return out


def index_close(day: str, force: bool = False) -> dict[str, dict[str, float]]:
    """{INDEX NAME: {open, high, low, close, change, pct}} for one session.

    The settled figures, straight from the exchange. Empty when the file is
    not published — a holiday, a weekend, or a session that has not settled.
    """
    body = _fetch_csv(day, INDEX_URL, "Index Name", "index", force)
    if not body:
        return {}
    out: dict[str, dict[str, float]] = {}
    for row in csv.DictReader(io.StringIO(body), skipinitialspace=True):
        name = (row.get("Index Name") or "").strip().upper()
        if not name:
            continue
        try:
            out[name] = {
                "open": float(row["Open Index Value"]),
                "high": float(row["High Index Value"]),
                "low": float(row["Low Index Value"]),
                "close": float(row["Closing Index Value"]),
                "change": float(row["Points Change"]),
                # The file writes "-.5" for -0.5%, which float() reads fine.
                "pct": float(row["Change(%)"]),
            }
        except (KeyError, ValueError):
            continue                      # a malformed row is not a reason to fail
    return out


def bhavcopy(day: str, force: bool = False) -> dict[str, dict[str, float]]:
    """{symbol: {prev, open, high, low, close}} for one session.

    Empty dict when the file is not published (a holiday, a weekend, or a
    session that has not settled yet). Callers treat empty as "cannot score
    this day", which is the truth rather than a zero.
    """
    body = _fetch_csv(day, BHAV_URL, "SYMBOL", "bhav", force)
    if not body:
        return {}

    out: dict[str, dict[str, float]] = {}
    for row in csv.DictReader(io.StringIO(body), skipinitialspace=True):
        if (row.get("SERIES") or "").strip() != SERIES:
            continue
        sym = (row.get("SYMBOL") or "").strip().upper()
        if not sym:
            continue
        try:
            out[sym] = {
                "prev": float(row["PREV_CLOSE"]),
                "open": float(row["OPEN_PRICE"]),
                "high": float(row["HIGH_PRICE"]),
                "low": float(row["LOW_PRICE"]),
                "close": float(row["CLOSE_PRICE"]),
                # Kept because the live feed cannot supply them. Delivery
                # quantity against total traded quantity is the share of the
                # day's volume that was actually taken into demat rather than
                # squared off — the difference between a move with conviction
                # behind it and a session of churn.
                "volume": float(row.get("TTL_TRD_QNTY") or 0),
                "delivery": float(row.get("DELIV_QTY") or 0),
            }
        except (KeyError, ValueError):
            continue                      # a malformed row is not a reason to fail
    return out


# The open. A briefing stamped after this on the day it covers had the
# session's own prices available to it.
OPEN_HHMM = (9, 15)


def lookahead(brief: Any, day: str) -> str:
    """"" if the briefing saw nothing of `day`, else why it cannot be scored.

    Judged on the INPUTS the row records, not on when it was written. Both
    halves of a setup now come from the previous session's settled file, so a
    briefing built at 12:57 whose pivots and candidates are both dated
    yesterday is a genuine forecast — late to publish, but honest to score.
    Excluding it on the clock alone would throw away real evidence.

    The timestamp is still the fallback for rows written before these notes
    existed, because it is the only evidence they carry. That keeps the three
    original briefings excluded on the correct grounds.
    """
    notes = _text(brief, "notes")

    # ── the modern test: did any input come from the scored session? ─────
    if "pivots from" in notes or "selection from" in notes:
        bad = []
        if f"pivots from {day}" in notes:
            bad.append("its pivot levels were built on the session it calls")
        if "pivots from none" in notes:
            bad.append("it carries no pivot session at all")
        if "selection from LIVE FEED" in notes:
            bad.append("its candidates came from the live feed, which reports "
                       "the current session once the market is open")
        if f"selection from {day}" in notes:
            bad.append("its candidates were chosen from the session it calls")
        if bad:
            return "; ".join(bad)
        return ""

    # ── the legacy test: all we have is when it was written ─────────────
    stamp = _text(brief, "at")
    if not stamp:
        return ("no timestamp and no input record — nothing in this row says "
                "which session it was built from")
    for fmt in ("%d-%b-%Y %H:%M", "%d-%b-%Y %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            at = datetime.strptime(stamp, fmt)
        except ValueError:
            continue
        if at.date().isoformat() != day:
            return ""                     # written on another day: fine
        if (at.hour, at.minute) >= OPEN_HHMM:
            return (f"built at {at:%H:%M} on {day}, after the "
                    f"{OPEN_HHMM[0]:02d}:{OPEN_HHMM[1]:02d} open, and it "
                    f"records no input session — before that record existed "
                    f"the levels came from the session being called, so this "
                    f"would measure hindsight")
        return ""
    return (f"unreadable timestamp {stamp!r} and no input record")


def previous_session(day: str) -> str:
    """The trading session before `day`, found by walking back for a bhavcopy.

    Walks rather than subtracting a day, because weekends and the exchange
    holiday calendar both sit in the way. Caps at seven days: a gap longer
    than that is a market closure nobody is scoring setups across.
    """
    try:
        d = datetime.strptime(day, "%Y-%m-%d").date()
    except ValueError:
        return ""
    for back in range(1, 8):
        cand = (d - timedelta(days=back)).isoformat()
        if index_close(cand):
            return cand
    return ""


def _num(obj: Any, name: str) -> float:
    val = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, 0)
    try:
        return float(val or 0)
    except (TypeError, ValueError):
        return 0.0


def _text(obj: Any, name: str) -> str:
    val = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, "")
    return str(val or "").strip()


def score_setup(setup: Any, tape: dict[str, float],
                prior: dict[str, float] | None = None) -> dict[str, Any]:
    """One setup against one session's OHLC. See the module note on ordering.

    `prior` is the session the pivots were BUILT from. Given it, two
    diagnostics come out that a verdict alone cannot give: how far the stock
    gapped overnight (in units of the range the level was derived from) and
    where the entry sat inside the range the stock actually traded.
    """
    side = (_text(setup, "side") or "long").lower()
    long_ = side != "short"
    entry, target, stop = (_num(setup, "entry"), _num(setup, "target"),
                           _num(setup, "stop"))
    out: dict[str, Any] = {
        "symbol": _text(setup, "symbol").upper(), "side": side,
        "entry": entry, "target": target, "stop": stop,
        "high": tape["high"], "low": tape["low"], "close": tape["close"],
        "verdict": "", "why": "", "stop_share": 0.0, "target_share": 0.0,
        "gap": 0.0, "gap_pct": 0.0, "gap_share": 0.0, "entry_pos": None,
        "open": tape["open"], "close_pct": None,
    }
    if tape.get("prev"):
        # The stock's own day, on the same basis the indices are quoted on —
        # so "the stock fell 1.2% while its sector rose 0.4%" compares like
        # with like rather than a close against a gap.
        out["close_pct"] = round(
            100 * (tape["close"] - tape["prev"]) / tape["prev"], 2)
    rng_today = tape["high"] - tape["low"]
    if rng_today > 0 and entry:
        # 0 at the low, 1 at the high. For a long, near 1 means price sat
        # below the trigger all session; for a short, near 0 means it sat
        # above. Either way the entry was on the wrong side of the day.
        out["entry_pos"] = round((entry - tape["low"]) / rng_today, 3)
    if prior:
        prior_rng = prior["high"] - prior["low"]
        out["gap"] = round(tape["open"] - prior["close"], 2)
        if prior["close"]:
            out["gap_pct"] = round(100 * out["gap"] / prior["close"], 2)
        if prior_rng > 0:
            # The overnight move measured against the range the level was
            # built from — the honest way to ask "was this level stale?"
            out["gap_share"] = round(out["gap"] / prior_rng, 3)
    if rng_today > 0:
        # The diagnostic: how far the stop and the target sat from the entry,
        # measured in units of the range the stock actually traded.
        out["stop_share"] = round(abs(entry - stop) / rng_today, 3)
        out["target_share"] = round(abs(target - entry) / rng_today, 3)

    if not (entry and target and stop):
        out.update(verdict=NO_DATA, why="the setup carried no levels")
        return out
    # The opening print comes before everything else, and it can settle the
    # day on its own. This check has to run BEFORE the touch tests, because
    # OHLC cannot order events within a session: without it, a stock that
    # opened beyond its stop is recorded as "stopped" on the strength of the
    # opening print itself — a loss booked on a trade that never existed.
    opened = tape["open"]
    if (opened <= stop) if long_ else (opened >= stop):
        out.update(verdict=VOID,
                   why=f"opened at {opened:g}, already past the {stop:g} stop "
                       f"— the setup was void before the entry could trigger")
        return out
    if (opened >= target) if long_ else (opened <= target):
        out.update(verdict=GAPPED,
                   why=f"opened at {opened:g}, already past the {target:g} "
                       f"target — the direction was right and there was no "
                       f"entry left to take")
        return out
    # Entry sits at the pivot, so it triggers if the session's range covers it.
    if not (tape["low"] <= entry <= tape["high"]):
        out.update(verdict=NO_TRADE,
                   why=f"{entry:g} never traded (range {tape['low']:g}"
                       f"-{tape['high']:g})")
        return out

    hit_t = tape["high"] >= target if long_ else tape["low"] <= target
    hit_s = tape["low"] <= stop if long_ else tape["high"] >= stop
    if hit_t and hit_s:
        out.update(verdict=UNRESOLVED,
                   why="both target and stop were touched; the tape cannot "
                       "say which came first")
    elif hit_t:
        out.update(verdict=TARGET, why=f"reached {target:g}")
    elif hit_s:
        out.update(verdict=STOPPED, why=f"broke {stop:g}")
    else:
        out.update(verdict=OPEN,
                   why=f"closed at {tape['close']:g} between the two levels")
    return out


# Verdicts where there was never a fair trade to score. The CALL still gets
# scored on these — that is the whole point of separating the two.
NO_FAIR_TRADE = (VOID, GAPPED, NO_TRADE, NO_DATA)


def attribute(row: dict[str, Any], index: dict[str, dict[str, float]],
              sectors: dict[str, dict[str, str]]) -> dict[str, Any]:
    """Did the call work, did the level work, and what drove it.

    Three settled numbers, no model: the stock's own close, its sector index,
    and NIFTY 50, all from the same day's archive. Triangulating them keeps
    apart four stories a viewer must not have blended:

        the market moved against us   -> nothing to do with our read
        the sector moved against us   -> the theme was wrong, not the stock
        it moved against us alone     -> something happened at the company
        it moved OUR way and we lost  -> the read was right, the level was not

    That last one is the branch the first version of this function got wrong,
    and it is the most important: a stop hit on the way to a correct close is
    a fault in our arithmetic, and dressing it up as a market story would be
    exactly the flattery a trust reel cannot survive.
    """
    sym = row.get("symbol", "")
    long_ = row.get("side") != "short"
    verdict = row.get("verdict", "")

    stock_pct = row.get("close_pct")
    mkt = (index.get("NIFTY 50") or {}).get("pct")
    info = sectors.get(sym) or {}
    sector_name = info.get("index", "")
    sec = (index.get(sector_name) or {}).get("pct") if sector_name else None

    out: dict[str, Any] = {
        "cause": "", "because": "", "direction": None,
        "market_pct": mkt, "sector_pct": sec, "sector": sector_name,
        "industry": info.get("industry", ""), "stock_pct": stock_pct,
    }
    if stock_pct is None or mkt is None:
        return out

    # Signed by the side, so one set of comparisons serves both: positive is
    # "against the call" whichever way the call went.
    def against(pct):
        return -pct if long_ else pct

    # The call, judged on the close alone. A dead-flat close is not a
    # direction, so it counts as neither right nor wrong.
    if abs(stock_pct) < 0.05:
        out["direction"] = None
    else:
        out["direction"] = against(stock_pct) < 0

    moved = "rose" if stock_pct > 0 else "fell"
    short_name = SECTOR_LABEL.get(sector_name) or "its sector"
    drift = f"NIFTY {mkt:+.2f}%"
    sector_bit = f"{short_name} {sec:+.2f}%" if sec is not None else ""

    # The STRONGEST explanation, not the first one that clears the bar. Both
    # the market and the sector can qualify on the same day, and taking
    # whichever was tested first credited NIFTY's -0.50% for INFY's -3.76%
    # when NIFTY IT had fallen -2.28% — the sector was plainly the story.
    def dominant(sign: int) -> str:
        """"market", "sector" or "stock". `sign` is +1 against, -1 for."""
        moves = {"market": against(mkt) * sign}
        if sec is not None:
            moves["sector"] = against(sec) * sign
        best = max(moves, key=lambda k: moves[k])
        if moves[best] < MATERIAL_PCT:
            return "stock"
        # A stock that moved far beyond whatever the index did was carried by
        # its own news even when the index also happened to be moving.
        if abs(stock_pct) > 2 * abs(sec if best == "sector" else mkt):
            return "stock"
        return best

    def driver(prefix: str) -> dict[str, Any]:
        """Which of the three levels explains a move against the call."""
        best = dominant(1)
        if best == "market":
            return {"cause": "market", "because": (
                f"{prefix} the whole market went the other way — {drift}, "
                f"and {sym} {moved} {abs(stock_pct):.2f}% with it")}
        if best == "sector":
            return {"cause": "sector", "because": (
                f"{prefix} {sector_bit} against {drift} — the theme was "
                f"wrong, not the stock")}
        rel = sector_bit or drift
        return {"cause": "stock", "because": (
            f"{prefix} {sym} {moved} {abs(stock_pct):.2f}% on its own while "
            f"{rel} — that is company news, not the market")}

    def support(prefix: str) -> dict[str, Any]:
        """What carried a call that worked."""
        best = dominant(-1)
        if best == "market":
            return {"cause": "market", "because": (
                f"{prefix} the whole market was behind it — {drift}, {sym} "
                f"{stock_pct:+.2f}%")}
        if best == "sector":
            return {"cause": "sector", "because": (
                f"{prefix} {sector_bit} led it while {drift}, and {sym} went "
                f"with the theme at {stock_pct:+.2f}%")}
        return {"cause": "stock", "because": (
            f"{prefix} {sym} moved {stock_pct:+.2f}% on its own while "
            f"{sector_bit or drift}")}

    right = out["direction"]

    # ── no fair trade: only the call can be scored ───────────────────────
    if verdict in NO_FAIR_TRADE:
        why = ("void at the open" if verdict == VOID else
               "gone before there was an entry" if verdict == GAPPED else
               "never triggered")
        if right:
            out.update(support(f"the call was right but the setup was {why} —"))
            out["cause"] = "no-entry"
        elif right is False:
            out.update(driver(f"the setup was {why}, and the call missed too —"))
        else:
            out.update(cause="flat", because=(
                f"the setup was {why} and {sym} closed flat at "
                f"{stock_pct:+.2f}%"))
        return out

    # ── a fair trade that reached its target ─────────────────────────────
    if verdict == TARGET:
        out.update(support("it worked —"))
        return out

    # ── a fair trade that did not ────────────────────────────────────────
    if right:
        # The important branch. The close vindicated the call; the stop did
        # not survive the path to it. That is geometry, not the market.
        out.update(cause="level", because=(
            f"the call was right — {sym} closed {stock_pct:+.2f}% — but the "
            f"stop at {row.get('stop', 0):g} was hit first, on a day whose "
            f"range was {row.get('low', 0):g} to {row.get('high', 0):g}. The "
            f"read held and the level did not"))
        return out
    if right is False:
        out.update(driver("it did not work —"))
        return out
    out.update(cause="flat", because=(
        f"{sym} closed flat at {stock_pct:+.2f}%, so there was no move to "
        f"catch either way"))
    return out


# What the index has to do for a call to count as right. Below this the day
# was a drift, and claiming a drift as a correct "up" would be the kind of
# flattery a trust reel cannot survive: on any given day roughly half of all
# flat sessions close green, so a 0.0% threshold scores a coin toss as skill.
FLAT_BAND = 0.25


def score_bias(brief: Any, index: dict[str, dict[str, float]]) -> dict[str, Any]:
    """Was the morning's directional call right?

    Judged on NIFTY 50's own settled close and Change(%), taken from the
    exchange's index file for the day being scored — not from the briefing's
    own reading of the tape, which would be marking its own homework.

    A call is `flat` when the index moved less than FLAT_BAND either way, so
    "up" and "down" both lose a day that went nowhere. That is stricter than
    counting sign alone and it is the honest version.
    """
    called = (_text(brief, "bias") or "flat").lower()
    row = index.get("NIFTY 50") or {}
    if not row:
        return {"called": called, "resolved": False, "moved_pct": None,
                "correct": None, "actual": "",
                "note": "no index bhavcopy for this session"}
    pct = row["pct"]
    actual = "up" if pct > FLAT_BAND else "down" if pct < -FLAT_BAND else "flat"
    return {"called": called, "actual": actual, "resolved": True,
            "moved_pct": pct, "close": row["close"],
            "correct": called == actual,
            "note": f"NIFTY 50 closed {row['close']:g}, {pct:+.2f}% — "
                    f"a {actual} session against a {called} call"}


def review(day: str) -> dict[str, Any]:
    """Score one stored briefing against that session's tape."""
    from . import briefing

    out: dict[str, Any] = {"date": day, "setups": [], "counts": {},
                           "hit_rate": None, "resolved": 0, "bias": {},
                           "skipped": "", "contaminated": False}
    try:
        brief = briefing.load(day)
    except FileNotFoundError:
        out["skipped"] = f"no briefing stored for {day}"
        return out

    blocked = lookahead(brief, day)
    if blocked:
        out["skipped"] = blocked
        out["contaminated"] = True
        return out

    tape = bhavcopy(day)
    index = index_close(day)
    sectors = sector_map()
    prior_day = previous_session(day)
    prior_tape = bhavcopy(prior_day) if prior_day else {}
    out["prior_session"] = prior_day or ""
    if not tape:
        out["skipped"] = (f"no bhavcopy for {day} — a holiday, a weekend, or "
                          f"the session has not settled yet")
        return out

    for setup in (getattr(brief, "setups", None) or []):
        sym = _text(setup, "symbol").upper()
        row = tape.get(sym)
        if not row:
            out["setups"].append({
                "symbol": sym, "side": (_text(setup, "side") or "").lower(),
                "verdict": NO_DATA, "why": "not in that session's bhavcopy",
                "entry": _num(setup, "entry"), "target": _num(setup, "target"),
                "stop": _num(setup, "stop"), "high": 0, "low": 0, "close": 0,
                "stop_share": 0.0, "target_share": 0.0})
            continue
        scored = score_setup(setup, row, prior_tape.get(sym))
        scored.update(attribute(scored, index, sectors))
        out["setups"].append(scored)

    for row in out["setups"]:
        out["counts"][row["verdict"]] = out["counts"].get(row["verdict"], 0) + 1
    out["resolved"] = sum(out["counts"].get(v, 0) for v in RESOLVED)
    # The call, scored separately from the level. Every setup counts here,
    # including the ones the opening gap voided — a read that was right is
    # right whether or not there was a tradeable entry behind it.
    called = [r["direction"] for r in out["setups"]
              if r.get("direction") is not None]
    out["directional"] = len(called)
    out["direction_right"] = sum(1 for d in called if d)
    out["direction_rate"] = (round(100 * out["direction_right"] / len(called), 1)
                             if called else None)
    # Why the resolved ones went the way they did, grouped. `level` is the
    # one to watch: it means the read held and our arithmetic did not.
    out["causes"] = {}
    for r in out["setups"]:
        if r.get("cause"):
            out["causes"][r["cause"]] = out["causes"].get(r["cause"], 0) + 1
    if out["resolved"]:
        out["hit_rate"] = round(
            100 * out["counts"].get(TARGET, 0) / out["resolved"], 1)
    out["bias"] = score_bias(brief, index)
    return out


def record(days: list[str] | None = None) -> dict[str, Any]:
    """The running track record across every stored briefing.

    This is the number the reel exists to state: not today's score, but "over
    N sessions, this many of the resolved calls worked". One day is noise.
    """
    from . import briefing

    days = days or briefing.list_days()
    per_day, setups = [], []
    for day in days:
        r = review(day)
        if r.get("skipped"):
            per_day.append({"date": day, "skipped": r["skipped"],
                            "contaminated": r.get("contaminated", False)})
            continue
        per_day.append({"date": day, "counts": r["counts"],
                        "hit_rate": r["hit_rate"], "resolved": r["resolved"],
                        "directional": r.get("directional", 0),
                        "direction_right": r.get("direction_right", 0),
                        "direction_rate": r.get("direction_rate"),
                        # Carried through so the roll-up can tally the
                        # directional call, which is scored per DAY rather
                        # than per setup.
                        "bias": r.get("bias") or {}})
        setups.extend(r["setups"])

    counts: dict[str, int] = {}
    for row in setups:
        counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
    resolved = sum(counts.get(v, 0) for v in RESOLVED)
    shares = [row["stop_share"] for row in setups if row.get("stop_share")]
    targets = [row["target_share"] for row in setups if row.get("target_share")]
    gaps = [abs(row["gap_share"]) for row in setups if row.get("gap_share")]
    called = [row["direction"] for row in setups
              if row.get("direction") is not None]
    causes: dict[str, int] = {}
    for row in setups:
        if row.get("cause"):
            causes[row["cause"]] = causes.get(row["cause"], 0) + 1
    biases = [d["bias"] for d in per_day
              if (d.get("bias") or {}).get("resolved")]
    bias_right = sum(1 for b in biases if b["correct"])
    long_pos = [row["entry_pos"] for row in setups
                if row.get("entry_pos") is not None and row.get("side") != "short"]
    short_pos = [row["entry_pos"] for row in setups
                 if row.get("entry_pos") is not None and row.get("side") == "short"]

    def _med(xs):
        return round(sorted(xs)[len(xs) // 2], 3) if xs else None
    return {
        "days": per_day,
        "sessions_scored": sum(1 for d in per_day if not d.get("skipped")),
        "setups": len(setups),
        "counts": counts,
        "resolved": resolved,
        "hit_rate": round(100 * counts.get(TARGET, 0) / resolved, 1)
                    if resolved else None,
        # The geometry, averaged. A median stop share near or below a third
        # says the stop is inside the day's ordinary noise, which is a fact
        # about the setup rather than about the market.
        "median_stop_share": round(sorted(shares)[len(shares) // 2], 3)
                             if shares else None,
        "median_target_share": round(sorted(targets)[len(targets) // 2], 3)
                               if targets else None,
        # Was the level stale on arrival? The overnight move, in units of the
        # range the level was computed from.
        "median_gap_share": _med(gaps),
        "gapped_over_half": sum(1 for g in gaps if g > 0.5),
        # Which side of the day the entry landed on. A long wants this LOW
        # (bought near the bottom) and a short wants it HIGH.
        "median_long_entry_pos": _med(long_pos),
        "median_short_entry_pos": _med(short_pos),
        # The bias call, scored separately: it is the claim most viewers
        # actually act on, and it can be right on a day every setup fails.
        # The headline the scorecard reel states: of the stocks we named,
        # how many closed the way we said. Scored on EVERY setup, because the
        # opening gap voiding an entry says nothing about whether the read was
        # right — see `attribute`.
        "directional": len(called),
        "direction_right": sum(1 for d in called if d),
        "direction_rate": (round(100 * sum(1 for d in called if d) / len(called), 1)
                           if called else None),
        "voided": sum(counts.get(v, 0) for v in (VOID, GAPPED)),
        "causes": causes,
        "excluded": sum(1 for d in per_day if d.get("contaminated")),
        "bias_scored": len(biases),
        "bias_correct": bias_right,
        "bias_rate": round(100 * bias_right / len(biases), 1) if biases else None,
    }


def report(r: dict[str, Any]) -> list[str]:
    """One day's review, as lines."""
    if r.get("skipped"):
        return [f"── {r['date']}: {r['skipped']}"]
    out = [f"── {r['date']}  ·  {len(r['setups'])} setup(s)"]
    out.append(f"   {'symbol':<12} {'side':<6} {'entry':>10} {'target':>10} "
               f"{'stop':>10} {'high':>10} {'low':>10}  verdict")
    for s in r["setups"]:
        out.append(f"   {s['symbol']:<12} {s['side']:<6} {s['entry']:>10.2f} "
                   f"{s['target']:>10.2f} {s['stop']:>10.2f} {s['high']:>10.2f} "
                   f"{s['low']:>10.2f}  {s['verdict']}")
        if s["why"]:
            out.append(f"        {s['why']}")
        if s.get("stop_share"):
            out.append(f"        stop sat {s['stop_share']:.0%} of the day's "
                       f"range from entry; target {s['target_share']:.0%}")
        if s.get("gap_share"):
            out.append(f"        gapped {s['gap_pct']:+.2f}% overnight "
                       f"({s['gap_share']:+.2f}x the range the level was "
                       f"built from)")
        if s.get("because"):
            out.append(f"        WHY [{s['cause']}] {s['because']}")
        if s.get("entry_pos") is not None:
            where = ("near the high" if s["entry_pos"] > 0.7 else
                     "near the low" if s["entry_pos"] < 0.3 else "mid-range")
            out.append(f"        entry sat at {s['entry_pos']:.2f} of the "
                       f"day's range ({where})")
    out.append("")
    if r.get("directional"):
        out.append(f"   the CALL  : {r['direction_right']} of "
                   f"{r['directional']} stocks closed the way we "
                   f"said ({r['direction_rate']}%)")
    if r["resolved"]:
        out.append(f"   the LEVEL : {r['counts'].get(TARGET, 0)} of "
                   f"{r['resolved']} resolved setups reached target "
                   f"({r['hit_rate']}%)")
    else:
        out.append("   the LEVEL : nothing resolved either way")
    voided = r["counts"].get(VOID, 0) + r["counts"].get(GAPPED, 0)
    if voided:
        out.append(f"   voided    : {voided} of {len(r['setups'])} "
                   f"were settled by the opening print before the "
                   f"entry could trigger")
    if r.get("causes"):
        out.append(f"   causes    : {r['causes']}")
    b = r.get("bias") or {}
    if b.get("resolved"):
        mark = "✓" if b["correct"] else "✗"
        out.append(f"   bias {mark} called {b['called']}, {b['note']}")
    elif b.get("called"):
        out.append(f"   bias · called {b['called']}, {b.get('note', '')}")
    return out


def record_report(r: dict[str, Any]) -> list[str]:
    """The running record, as lines."""
    out = [f"── Track record  ·  {r['sessions_scored']} session(s), "
           f"{r['setups']} setup(s)"]
    if r.get("excluded"):
        out.append(f"   ! {r['excluded']} briefing(s) excluded for hindsight "
                   f"— see below. Nothing here is a track record until the "
                   f"first briefing built before 09:15 has been scored.")
    for day in r["days"]:
        if day.get("skipped"):
            flag = "EXCLUDED " if day.get("contaminated") else ""
            out.append(f"   {day['date']}  {flag}{day['skipped']}")
        else:
            b = day.get("bias") or {}
            mark = ("" if not b.get("resolved")
                    else "  bias ✓" if b["correct"] else "  bias ✗")
            call = (f"call {day['direction_right']}/{day['directional']}"
                    if day.get("directional") else "call —")
            out.append(f"   {day['date']}  {call}   level "
                       f"{day['counts'].get(TARGET, 0)}/{day['resolved']}"
                       f"{mark}   {day['counts']}")
    out.append("")
    out.append(f"   verdicts: {r['counts']}")
    if r["resolved"]:
        out.append(f"   resolved {r['resolved']}, hit rate {r['hit_rate']}% "
                   f"({r['counts'].get(TARGET, 0)} of {r['resolved']})")
    if r.get("directional"):
        out.append("")
        out.append(f"   the CALL               : {r['direction_right']} of "
                   f"{r['directional']} stocks closed the way we said "
                   f"({r['direction_rate']}%)")
    if r["resolved"]:
        out.append(f"   the LEVEL              : {r['counts'].get(TARGET, 0)} "
                   f"of {r['resolved']} resolved setups hit target "
                   f"({r['hit_rate']}%)")
    if r.get("voided"):
        out.append(f"   voided at the open     : {r['voided']} of "
                   f"{r['setups']} — settled by the opening print before the "
                   f"entry could trigger")
    if r.get("causes"):
        out.append(f"   causes                 : {r['causes']}")

    # The bias call stands on its own: it can be right on a day when every
    # setup failed, and it is the claim most viewers actually act on.
    if r.get("bias_scored"):
        out.append("")
        out.append(f"   bias calls            : {r['bias_correct']} of "
                   f"{r['bias_scored']} right ({r['bias_rate']}%)")

    if r["median_stop_share"] is not None:
        out.append("")
        out.append(f"   median stop distance  : {r['median_stop_share']:.0%} "
                   f"of the day's realised range")
        out.append(f"   median target distance: {r['median_target_share']:.0%} "
                   f"of the day's realised range")

    # Was the level stale on arrival, or was it just in the wrong place? These
    # two answer that, and they answer it differently — see the module header.
    if r.get("median_gap_share") is not None:
        out.append(f"   median overnight gap  : {r['median_gap_share']:.2f}x "
                   f"the range the level was built from "
                   f"({r['gapped_over_half']} of {r['setups']} gapped >0.5x)")
    if r.get("median_long_entry_pos") is not None:
        out.append(f"   longs  entered at     : "
                   f"{r['median_long_entry_pos']:.2f} of the day's range "
                   f"(1.0 = the high — price sat BELOW the trigger)")
    if r.get("median_short_entry_pos") is not None:
        out.append(f"   shorts entered at     : "
                   f"{r['median_short_entry_pos']:.2f} of the day's range "
                   f"(0.0 = the low — price sat ABOVE the trigger)")

    if (r["median_stop_share"] or 1) <= 0.4:
        out.append("   ! A stop inside ~40% of the day's range, with the "
                   "entry at the pivot (mid-range by construction), is")
        out.append("     touched by ordinary noise before the target is "
                   "approached. That is the setup's geometry, not the")
        out.append("     market's mood — see this module's header.")
    return out
