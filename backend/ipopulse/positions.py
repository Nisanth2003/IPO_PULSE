"""Reel 9: the same calls, held for days instead of minutes.

The user's framing, 8 Sep 2026: "some people do swing trading or something
like but the stock in delivery hold this long and sell them at after some days
something like that". That is a different audience from reel 7's, asking a
different question of the same setups — not "did this work today" but "did it
work at all, and what did holding it cost".

`review.score_swing` does the scoring; this module stores it, which is what
makes it a reel. Named `positions` rather than `swing` because `review.swing`
already exists as a function and a module shadowing it reads badly.

── why this rewrites history and the scorecard does not ───────────────────

A reel 8 row is final the evening it is written: one session, scored, done. A
position is not. It can still be OPEN, and a trade opened five sessions ago
may only resolve today — so every day inside the horizon has to be rescored
and rewritten on every run. `open_now` is how a row says it is still
provisional.

That is the whole reason the Swing tabs are a separate pair rather than extra
columns on Scorecard: those must never be rewritten once a day closes, and
these must be.

── the number reel 9 leads with is NOT the hit rate ───────────────────────

Over a multi-session hold, hit rate and profit come apart. A 30% hit rate at
3:1 beats a 60% one at 1:2, and a hit rate alone cannot tell them apart. So
`win_loss_ratio` and the `breakeven_rate` it implies are stored beside it, and
the card leads with those — quoting the rate on its own is how a losing
structure looks respectable.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from . import sheets

# Sessions a position gets to resolve, and therefore how far back a run has
# to rescore. Mirrors `review.SWING_HORIZON`; read from there rather than
# duplicated so the two cannot drift.
from .review import SWING_HORIZON  # noqa: E402


def today() -> str:
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
    return f"Google Sheet {sid[:8]}… (Swing tabs)" if sid \
        else "no sheet configured"


def list_days() -> list[str]:
    return sorted(sheets.swing_records())


def load(day: str | date | None = None) -> dict[str, Any]:
    key = _key(day)
    rec = sheets.swing_records().get(key)
    if rec is None:
        raise FileNotFoundError(f"No swing row for {key} in the sheet")
    return dict(rec)


def build(day: str, horizon: int = SWING_HORIZON) -> dict[str, Any]:
    """Score one briefing's positions over `horizon` sessions.

    Returns `{"skipped": reason}` when the day cannot be scored. Callers must
    not store a skipped day — see `scorecard.save` for why a row of zeros is
    worse than no row.
    """
    from . import review

    r = review.swing(day, horizon)
    if r.get("skipped"):
        return {"date": day, "skipped": r["skipped"],
                "contaminated": r.get("contaminated", False)}

    counts = r.get("counts") or {}
    rows = r.get("setups") or []
    positions = []
    for i, p in enumerate(rows, 1):
        positions.append({
            "idx": i,
            "symbol": p.get("symbol"), "side": p.get("side"),
            "entry": p.get("entry"), "target": p.get("target"),
            "stop": p.get("stop"),
            "verdict": p.get("verdict"), "why": p.get("why"),
            "entered_on": p.get("entered_on"), "exit_on": p.get("exit_on"),
            "exit": p.get("exit"),
            "sessions_held": p.get("sessions_held"),
            "return_pct": p.get("return_pct"),
            "gapped_exit": p.get("gapped_exit"),
            "slippage": p.get("slippage"),
        })

    # Realised returns only. An OPEN position's return_pct is a mark to the
    # last close, not a result, and averaging marks into a track record is
    # how an unfinished window flatters itself.
    rets = [p["return_pct"] for p in positions
            if p["return_pct"] is not None and p["verdict"] != review.OPEN]
    wins = [x for x in rets if x > 0]
    losses = [x for x in rets if x <= 0]
    avg_win = round(sum(wins) / len(wins), 2) if wins else None
    avg_loss = round(sum(losses) / len(losses), 2) if losses else None
    ratio = (round(abs(avg_win / avg_loss), 2)
             if avg_win is not None and avg_loss else None)
    # The hit rate this structure needs just to break even at the sizes it is
    # actually producing. Stating it beside the achieved rate is what stops a
    # 30% hit rate reading as a failure or a 60% one as a success.
    breakeven = round(100 / (1 + ratio), 1) if ratio else None

    # Still running: the window has sessions left to settle, so this row is
    # provisional and will be rewritten. Counted directly off the verdict
    # rather than by excluding a list of closed ones — an unless-list silently
    # reclassifies any verdict added later.
    open_now = sum(1 for p in positions if p["verdict"] == review.OPEN)

    return {
        "date": day,
        "scored_at": datetime.now().isoformat(timespec="seconds"),
        "horizon": horizon,
        # How much of the horizon the archive can actually cover. Short of
        # `horizon` means the window is not finished and the open positions
        # are not final — `review.swing` sets `partial` for the same reason.
        "sessions": len(r.get("sessions") or []),
        "position_count": len(positions),
        "resolved": r.get("resolved", 0),
        "target_hit": counts.get(review.TARGET, 0),
        "hit_rate": r.get("hit_rate"),
        "expired": counts.get(review.EXPIRED, 0),
        "no_trade": counts.get(review.NO_TRADE, 0),
        "open_now": open_now,
        "avg_return_pct": r.get("avg_return_pct"),
        "avg_win_pct": avg_win, "avg_loss_pct": avg_loss,
        "win_loss_ratio": ratio, "breakeven_rate": breakeven,
        "avg_sessions_held": r.get("avg_sessions_held"),
        "gapped_exits": r.get("gapped_exits", 0),
        "total_slippage": r.get("total_slippage", 0),
        "note": r.get("partial") or (
            f"{counts.get(review.TARGET, 0)} of {r.get('resolved', 0)} "
            f"resolved positions reached target over {horizon} session(s)"),
        "positions": positions,
    }


def rescore(horizon: int = SWING_HORIZON,
            days: list[str] | None = None) -> dict[str, Any]:
    """Rescore every briefing whose horizon could still be running.

    The window is `horizon` sessions back, not one day: a position opened five
    sessions ago may only resolve today, so scoring yesterday alone would
    leave four days of rows permanently stale. Returns
    `{"days": {...}, "skipped": [...]}` — the caller writes.
    """
    from . import briefing

    stored = briefing.list_days()
    if days is None:
        # The last `horizon` briefings, not the last `horizon` calendar days:
        # a weekend has no briefing and would otherwise eat the window.
        days = stored[-horizon:] if stored else []

    out: dict[str, dict] = {}
    skipped: list[tuple[str, str]] = []
    for day in days:
        rec = build(day, horizon)
        if rec.get("skipped"):
            skipped.append((day, rec["skipped"]))
            continue
        out[day] = rec
    return {"days": out, "skipped": skipped, "horizon": horizon}


def save(days: dict[str, dict]) -> str:
    """Write several scored days at once, keeping every other day."""
    for day, rec in days.items():
        if rec.get("skipped"):
            raise ValueError(
                f"refusing to store {day}: {rec['skipped']}. An unscoreable "
                f"day must have no row, not a row of zeros.")
    if not days:
        return where()
    sheets.upsert_swing(days)
    after = sheets.swing_records(force=True)
    missing = [d for d in days if d not in after]
    if missing:
        raise RuntimeError(
            f"wrote {len(days)} day(s) but {', '.join(missing)} is not on the "
            f"sheet — another job may have been writing at the same time. "
            f"Run the command again.")
    return where()


def record() -> dict[str, Any]:
    """The running swing record, summed off the STORED rows.

    Read from the sheet rather than by rescoring, so the reel and the sheet
    cannot disagree about what was published.
    """
    rows = sheets.swing_records()
    picked = [rows[d] for d in sorted(rows)]

    def _n(row, key):
        try:
            return float(row.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0

    resolved = sum(_n(r, "resolved") for r in picked)
    hits = sum(_n(r, "target_hit") for r in picked)
    # Averaged over POSITIONS, not over days: a day with one position must
    # not weigh the same as a day with ten.
    all_pos = [p for r in picked for p in (r.get("positions") or [])]

    def _ret(p):
        try:
            return float(p.get("return_pct"))
        except (TypeError, ValueError):
            return None

    rets = [x for x in (_ret(p) for p in all_pos) if x is not None]
    wins = [x for x in rets if x > 0]
    losses = [x for x in rets if x <= 0]
    avg_win = round(sum(wins) / len(wins), 2) if wins else None
    avg_loss = round(sum(losses) / len(losses), 2) if losses else None
    ratio = (round(abs(avg_win / avg_loss), 2)
             if avg_win is not None and avg_loss else None)

    gapped = sum(1 for p in all_pos
                 if str(p.get("gapped_exit")).strip().lower()
                 in ("true", "yes", "1"))
    return {
        "sessions": len(picked),
        "days": [r.get("date") for r in picked],
        "positions": len(all_pos),
        "resolved": int(resolved), "target_hit": int(hits),
        "hit_rate": round(100 * hits / resolved, 1) if resolved else None,
        "avg_return_pct": round(sum(rets) / len(rets), 2) if rets else None,
        "avg_win_pct": avg_win, "avg_loss_pct": avg_loss,
        "win_loss_ratio": ratio,
        "breakeven_rate": round(100 / (1 + ratio), 1) if ratio else None,
        "gapped_exits": gapped,
        "open_now": int(sum(_n(r, "open_now") for r in picked)),
        # Same floor as reel 8's, and it binds harder here: a multi-session
        # hold produces fewer resolved outcomes per session, so five days is
        # already a thin sample.
        "enough": len(picked) >= 5,
    }


def report(rec: dict[str, Any]) -> list[str]:
    """One day's positions, as lines."""
    if rec.get("skipped"):
        return [f"── {rec['date']} (swing): {rec['skipped']}"]
    out = [f"── {rec['date']}  ·  {rec['horizon']}-session horizon  ·  "
           f"{rec['position_count']} position(s)"]
    for p in rec.get("positions") or []:
        ret = p.get("return_pct")
        shown = f"{float(ret):+.2f}%" if ret not in (None, "") else "—"
        out.append(f"   {str(p.get('symbol') or ''):<12}"
                   f"{str(p.get('side') or ''):<6}"
                   f"{str(p.get('verdict') or ''):<11}{shown:>9}  "
                   f"held {p.get('sessions_held')}")
        if p.get("gapped_exit"):
            out.append(f"       ! left on a gap, {p.get('slippage')} worse "
                       f"than the level")
    out.append("")
    out.append(f"   {rec['target_hit']} of {rec['resolved']} resolved "
               f"reached target; average per position "
               f"{rec['avg_return_pct']}%")
    if rec.get("win_loss_ratio"):
        out.append(f"   win/loss {rec['avg_win_pct']}% vs "
                   f"{rec['avg_loss_pct']}% = {rec['win_loss_ratio']}x "
                   f"→ break-even hit rate {rec['breakeven_rate']}%")
    if rec.get("gapped_exits"):
        out.append(f"   {rec['gapped_exits']} exited on an overnight gap "
                   f"rather than at their level")
    return out


def record_report(r: dict[str, Any]) -> list[str]:
    """The running swing record, as lines — what reel 9 would state."""
    out = [f"── Swing record  ·  {r['sessions']} session(s), "
           f"{r['positions']} position(s)"]
    if not r["sessions"]:
        out.append("   Nothing stored yet. `ipopulse swing --write` after the "
                   "exchange publishes, from about 18:30 IST.")
        return out
    if r["hit_rate"] is not None:
        out.append(f"   hit rate      : {r['target_hit']} of {r['resolved']} "
                   f"resolved ({r['hit_rate']}%)")
    if r["avg_return_pct"] is not None:
        out.append(f"   per position  : {r['avg_return_pct']:+.2f}% "
                   f"(every position taken, losers included)")
    if r["win_loss_ratio"]:
        out.append(f"   win vs loss   : {r['avg_win_pct']:+.2f}% against "
                   f"{r['avg_loss_pct']:+.2f}% = {r['win_loss_ratio']}x")
        out.append(f"   break-even at : {r['breakeven_rate']}% — below that "
                   f"this structure loses money at the sizes it produces")
    if r["gapped_exits"]:
        out.append(f"   gap exits     : {r['gapped_exits']} of "
                   f"{r['positions']} left at an open, not at their level")
    if r["open_now"]:
        out.append(f"   still open    : {r['open_now']} — these rows are "
                   f"provisional and get rewritten")
    if not r["enough"]:
        out.append("")
        out.append(f"   ! {r['sessions']} session(s) is not a track record. "
                   f"Reel 9 must say 'too early to say'.")
    return out
