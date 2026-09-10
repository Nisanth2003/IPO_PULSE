"""Reel 8: what reel 7 said this morning, and what the market then did.

The spec, in the user's words: reel 7 says a stock may move this much, and at
the end of the day we ask what actually happened — "this is what we expected
it happened", or "this is what we expected but that did not happend due to
this" — and close on prediction success over total predictions.

── why this is stored and not computed in the browser ─────────────────────

Everything else the studio shows is read straight off the sheet, and
`no-duplicate-data-layers` says an extra copy has to justify itself. This one
does: scoring needs NSE's bhavcopy, and the browser cannot fetch it — no CORS
headers on nsearchives and no key to proxy it with. So the scoring runs
backend-side once an evening and the result is written, exactly like the
briefing it grades.

── the two claims, kept apart ─────────────────────────────────────────────

A briefing makes two separate claims and they fail independently, so reel 8
states both rather than blending them into one flattering average:

    the CALL    "this stock goes up/down today"     -> judged on the close
    the LEVEL   "enter here, target here, stop here" -> judged on the tape

9 Sep 2026, the first session with clean inputs, was 5 of 8 on the call and
0 of 2 on the level. An average of those two numbers would describe nothing
that happened.

── the honesty rules it inherits ──────────────────────────────────────────

All of them live in `review` and this module only stores what it says. The
ones that matter most, because each is a place a scorecard would otherwise
flatter itself:

  * a day that touched both target and stop is `unresolved`, never a win —
    the exchange's file says WHETHER a level was hit, never in what order;
  * a setup whose day opened past its own stop is `void`, not a loss, because
    you cannot be stopped out before you are filled;
  * a briefing built from the session it was predicting is EXCLUDED, and the
    reel must not quote it — see `review.lookahead`;
  * the reason a call failed is triangulated from the stock's close, its
    sector index and NIFTY 50, never written by the model.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from . import sheets

# Sessions to look back over when nothing is asked for by name. Ten is about
# a fortnight of trading, which is the shortest window where a rate means
# anything — see `record`.
LOOKBACK = 10


def today() -> str:
    """Today in IST, as the store key. Mirrors `briefing.today`."""
    from .briefing import today as _today

    return _today()


def _key(day: str | date | None) -> str:
    if day is None:
        return today()
    if isinstance(day, (date, datetime)):
        return day.isoformat()[:10]
    return str(day).strip()[:10]


def where() -> str:
    sid = sheets.sheet_id()
    return f"Google Sheet {sid[:8]}… (Scorecard tabs)" if sid \
        else "no sheet configured"


def list_days() -> list[str]:
    """Every scored session, oldest first."""
    return sorted(sheets.scorecard_records())


def exists(day: str | date | None = None) -> bool:
    return _key(day) in sheets.scorecard_records()


def load(day: str | date | None = None) -> dict[str, Any]:
    """One scored session. Raises FileNotFoundError like `briefing.load`."""
    key = _key(day)
    rec = sheets.scorecard_records().get(key)
    if rec is None:
        raise FileNotFoundError(f"No scorecard for {key} in the sheet")
    return dict(rec)


def latest() -> dict[str, Any] | None:
    days = list_days()
    return load(days[-1]) if days else None


def build(day: str) -> dict[str, Any]:
    """Score one session into the shape the Scorecard tabs hold.

    A pure function of the stored briefing and the exchange's settled files,
    which is what makes re-running it safe: scoring the same day twice cannot
    produce a different answer, so unlike `briefing.save` there is no
    overwrite guard to argue with.

    Returns `{"skipped": reason}` when the day cannot be scored — no briefing,
    no bhavcopy yet, or a briefing that saw the session it called. Callers
    must not write a skipped day: a row of zeros reads as "we got everything
    wrong" rather than "we did not measure".
    """
    from . import review

    r = review.review(day)
    if r.get("skipped"):
        return {"date": day, "skipped": r["skipped"],
                "contaminated": r.get("contaminated", False)}

    counts = r.get("counts") or {}
    bias = r.get("bias") or {}
    calls = []
    for i, s in enumerate(r.get("setups") or [], 1):
        calls.append({
            "idx": i,
            "symbol": s.get("symbol"), "side": s.get("side"),
            "entry": s.get("entry"), "target": s.get("target"),
            "stop": s.get("stop"),
            "high": s.get("high"), "low": s.get("low"), "close": s.get("close"),
            "verdict": s.get("verdict"), "why": s.get("why"),
            # `direction` is the CALL alone, and it is kept for setups that
            # never triggered: a read that was right is right whether or not
            # there was a trade behind it.
            "direction": s.get("direction"),
            "stock_pct": s.get("stock_pct"),
            "cause": s.get("cause"), "because": s.get("because"),
            "sector": s.get("sector"), "sector_pct": s.get("sector_pct"),
            "market_pct": s.get("market_pct"),
            "stop_share": s.get("stop_share"),
            "target_share": s.get("target_share"),
            "gap_pct": s.get("gap_pct"), "entry_pos": s.get("entry_pos"),
        })

    # The geometry medians, so a drift in the setup structure is visible on
    # the sheet rather than only in a terminal. Computed per day here; the
    # running versions come from `review.record`.
    shares = sorted(c["stop_share"] for c in calls if c.get("stop_share"))
    targets = sorted(c["target_share"] for c in calls if c.get("target_share"))

    def _med(xs):
        return round(xs[len(xs) // 2], 3) if xs else None

    nifty = bias.get("close")
    return {
        "date": day,
        "scored_at": datetime.now().isoformat(timespec="seconds"),
        "setups": len(calls),
        "directional": r.get("directional", 0),
        "direction_right": r.get("direction_right", 0),
        "direction_rate": r.get("direction_rate"),
        "resolved": r.get("resolved", 0),
        "target_hit": counts.get(review.TARGET, 0),
        "hit_rate": r.get("hit_rate"),
        "voided": counts.get(review.VOID, 0) + counts.get(review.GAPPED, 0),
        "no_trade": counts.get(review.NO_TRADE, 0),
        "bias_called": bias.get("called"),
        "bias_actual": bias.get("actual"),
        "bias_correct": bias.get("correct"),
        "nifty_close": nifty,
        "nifty_pct": bias.get("moved_pct"),
        "median_stop_share": _med(shares),
        "median_target_share": _med(targets),
        # Rendered rather than nested: a dict in a cell is unreadable, and
        # the per-call `cause` column already carries the detail.
        "causes": ", ".join(f"{k} {v}" for k, v in
                            sorted((r.get("causes") or {}).items())),
        "note": (f"{r.get('direction_right', 0)} of {r.get('directional', 0)} "
                 f"stocks closed as called; "
                 f"{counts.get(review.TARGET, 0)} of {r.get('resolved', 0)} "
                 f"resolved setups reached target"),
        "calls": calls,
    }


def save(rec: dict[str, Any]) -> str:
    """Write one scored session, keeping every other day.

    Refuses a skipped day. A row of zeros for a session nobody could score
    reads on the reel as "we got everything wrong", which is a worse lie than
    having no row at all.

    Verifies against the sheet afterwards rather than trusting the write —
    the same reason `briefing.save` does: a concurrent job can revert it, and
    a silent revert is how a reel ends up quoting numbers the sheet no longer
    holds.
    """
    day = _key(rec.get("date"))
    if not day:
        raise ValueError("a scorecard needs a date before it can be saved")
    if rec.get("skipped"):
        raise ValueError(
            f"refusing to store {day}: {rec['skipped']}. An unscoreable day "
            f"must have no row, not a row of zeros — zeros read as 'every "
            f"call was wrong' rather than 'this was not measured'.")

    sheets.upsert_scorecard(day, rec)
    after = sheets.scorecard_records(force=True).get(day)
    if after is None:
        raise RuntimeError(
            f"wrote {day} but it is not on the sheet — another job may have "
            f"been writing at the same time. Nothing else was changed; run "
            f"the command again.")
    return where()


def record(days: list[str] | None = None) -> dict[str, Any]:
    """The running rate across stored scorecards — the number reel 8 states.

    Read off the STORED rows rather than by re-scoring, so the reel and the
    sheet can never disagree about what was published. One session is noise;
    `enough` is what the reel should gate its headline on.
    """
    rows = sheets.scorecard_records()
    picked = [rows[d] for d in sorted(rows) if not days or d in days]

    def _n(row, key):
        try:
            return float(row.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0

    called = sum(_n(r, "directional") for r in picked)
    right = sum(_n(r, "direction_right") for r in picked)
    resolved = sum(_n(r, "resolved") for r in picked)
    hits = sum(_n(r, "target_hit") for r in picked)
    bias_days = [r for r in picked if str(r.get("bias_correct") or "").strip()]
    bias_right = sum(1 for r in bias_days
                     if str(r.get("bias_correct")).strip().lower()
                     in ("true", "yes", "1"))
    return {
        "sessions": len(picked),
        "days": [r.get("date") for r in picked],
        "setups": int(sum(_n(r, "setups") for r in picked)),
        "directional": int(called),
        "direction_right": int(right),
        "direction_rate": round(100 * right / called, 1) if called else None,
        "resolved": int(resolved),
        "target_hit": int(hits),
        "hit_rate": round(100 * hits / resolved, 1) if resolved else None,
        "voided": int(sum(_n(r, "voided") for r in picked)),
        "bias_scored": len(bias_days),
        "bias_right": bias_right,
        "bias_rate": (round(100 * bias_right / len(bias_days), 1)
                      if bias_days else None),
        # Whether the reel may state a rate at all. Below this the honest
        # line is "too early to say", and saying it is better for a trust
        # channel than a percentage computed from three trades.
        "enough": len(picked) >= 5,
    }


def report(rec: dict[str, Any]) -> list[str]:
    """One scored session, as lines."""
    if rec.get("skipped"):
        return [f"── {rec['date']}: {rec['skipped']}"]
    out = [f"── {rec['date']}  ·  {rec['setups']} call(s) scored"]
    for c in rec.get("calls") or []:
        mark = ("✓" if c.get("direction") is True else
                "✗" if c.get("direction") is False else "·")
        pct = c.get("stock_pct")
        moved = f"{float(pct):+.2f}%" if pct not in (None, "") else "—"
        out.append(f"   {mark} {str(c.get('symbol') or ''):<12}"
                   f"{str(c.get('side') or ''):<6}{moved:>9}  "
                   f"{c.get('verdict')}")
        if c.get("because"):
            out.append(f"       {c['because']}")
    out.append("")
    out.append(f"   the CALL  : {rec['direction_right']} of "
               f"{rec['directional']} ({rec['direction_rate']}%)")
    out.append(f"   the LEVEL : {rec['target_hit']} of {rec['resolved']} "
               f"resolved ({rec['hit_rate']}%)")
    if rec.get("voided"):
        out.append(f"   voided    : {rec['voided']} settled by the opening "
                   f"print before an entry existed")
    if rec.get("bias_called"):
        tick = "✓" if rec.get("bias_correct") else "✗"
        out.append(f"   bias {tick} called {rec['bias_called']}, NIFTY closed "
                   f"{rec.get('nifty_pct')}%")
    return out


def record_report(r: dict[str, Any]) -> list[str]:
    """The running record, as lines — what reel 8 would state."""
    out = [f"── Scorecard  ·  {r['sessions']} session(s), {r['setups']} call(s)"]
    if not r["sessions"]:
        out.append("   Nothing scored yet. `ipopulse score --write` after the "
                   "exchange publishes, from about 18:30 IST.")
        return out
    out.append(f"   {', '.join(str(d) for d in r['days'])}")
    out.append("")
    if r["direction_rate"] is not None:
        out.append(f"   the CALL  : {r['direction_right']} of "
                   f"{r['directional']} stocks closed as called "
                   f"({r['direction_rate']}%)")
    if r["hit_rate"] is not None:
        out.append(f"   the LEVEL : {r['target_hit']} of {r['resolved']} "
                   f"resolved setups reached target ({r['hit_rate']}%)")
    if r["voided"]:
        out.append(f"   voided    : {r['voided']} of {r['setups']} never had "
                   f"a tradeable entry")
    if r["bias_rate"] is not None:
        out.append(f"   the BIAS  : {r['bias_right']} of {r['bias_scored']} "
                   f"index calls right ({r['bias_rate']}%)")
    if not r["enough"]:
        out.append("")
        out.append(f"   ! {r['sessions']} session(s) is not a track record. "
                   f"Reel 8 must say 'too early to say' rather than state "
                   f"these as a rate.")
    return out
