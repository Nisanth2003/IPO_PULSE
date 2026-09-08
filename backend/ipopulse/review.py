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
RESOLVED = (TARGET, STOPPED)


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
            }
        except (KeyError, ValueError):
            continue                      # a malformed row is not a reason to fail
    return out


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
    }
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
    rng = tape["high"] - tape["low"]
    if rng > 0:
        # The diagnostic: how far the stop and the target sat from the entry,
        # measured in units of the range the stock actually traded.
        out["stop_share"] = round(abs(entry - stop) / rng, 3)
        out["target_share"] = round(abs(target - entry) / rng, 3)

    if not (entry and target and stop):
        out.update(verdict=NO_DATA, why="the setup carried no levels")
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
                           "skipped": ""}
    try:
        brief = briefing.load(day)
    except FileNotFoundError:
        out["skipped"] = f"no briefing stored for {day}"
        return out

    tape = bhavcopy(day)
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
        out["setups"].append(score_setup(setup, row, prior_tape.get(sym)))

    for row in out["setups"]:
        out["counts"][row["verdict"]] = out["counts"].get(row["verdict"], 0) + 1
    out["resolved"] = sum(out["counts"].get(v, 0) for v in RESOLVED)
    if out["resolved"]:
        out["hit_rate"] = round(
            100 * out["counts"].get(TARGET, 0) / out["resolved"], 1)
    out["bias"] = score_bias(brief, index_close(day))
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
            per_day.append({"date": day, "skipped": r["skipped"]})
            continue
        per_day.append({"date": day, "counts": r["counts"],
                        "hit_rate": r["hit_rate"], "resolved": r["resolved"],
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
        if s.get("entry_pos") is not None:
            where = ("near the high" if s["entry_pos"] > 0.7 else
                     "near the low" if s["entry_pos"] < 0.3 else "mid-range")
            out.append(f"        entry sat at {s['entry_pos']:.2f} of the "
                       f"day's range ({where})")
    if r["resolved"]:
        out.append(f"\n   resolved {r['resolved']}, hit rate {r['hit_rate']}% "
                   f"({r['counts'].get(TARGET, 0)} of {r['resolved']})")
    else:
        out.append("\n   nothing resolved either way")
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
    for day in r["days"]:
        if day.get("skipped"):
            out.append(f"   {day['date']}  {day['skipped']}")
        else:
            b = day.get("bias") or {}
            mark = ("" if not b.get("resolved")
                    else "  bias ✓" if b["correct"] else "  bias ✗")
            out.append(f"   {day['date']}  {day['counts']}  "
                       f"hit rate {day['hit_rate']}%{mark}")
    out.append("")
    out.append(f"   verdicts: {r['counts']}")
    if r["resolved"]:
        out.append(f"   resolved {r['resolved']}, hit rate {r['hit_rate']}% "
                   f"({r['counts'].get(TARGET, 0)} of {r['resolved']})")
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
