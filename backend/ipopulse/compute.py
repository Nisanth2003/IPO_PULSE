"""Derived metrics.

Nothing here is stored — it is all recomputed from `Ipo`. Keep this file the
single source of truth for the arithmetic, and keep frontend/js/compute.js a
faithful mirror of it (the browser recomputes live while you type in the
sidebar; the backend computes the same values for the Excel report).

Rule: an LLM never produces any number in this file.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any

from .models import Ipo


def _round(value: float, dp: int = 0) -> float:
    """Round half UP, exactly as JavaScript's Math.round does.

    Python's built-in `round` is half-to-even, so `round(7.05, 1)` is 7.0
    while `Math.round(70.5) / 10` is 7.1. That difference is invisible until
    a value lands precisely on the boundary — Lalithaa Jewellery's score did,
    and the published JSON then said 7.0 while the studio recomputed 7.1 from
    the same inputs. compute.js is the reference because the browser's
    arithmetic is not ours to change.

    `floor(v * m + 0.5)` is Math.round's actual definition, negatives
    included: Math.round(-70.5) is -70, not -71.
    """
    m = 10 ** dp
    return math.floor(value * m + 0.5) / m


def _pct(part: float, whole: float) -> float:
    return (part / whole * 100.0) if whole else 0.0


def _cagr(first: float, last: float, years: int) -> float:
    """Compound annual growth rate in %. Needs positive endpoints."""
    if first <= 0 or last <= 0 or years <= 0:
        return 0.0
    return ((last / first) ** (1.0 / years) - 1.0) * 100.0


def _safe(seq: list[float], i: int) -> float:
    try:
        return float(seq[i])
    except (IndexError, TypeError, ValueError):
        return 0.0


def _curve(x: float, points: list[tuple[float, float]]) -> float:
    """Piecewise-linear lookup over ascending (x, y) anchors.

    Used for the score bands. A formula would be shorter, but anchors are the
    thing worth arguing about — "10x subscribed is a 7.5" is a judgement you
    can see and change, where a tuned logarithm hides the same judgement
    inside a constant.
    """
    if x <= points[0][0]:
        return points[0][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x <= x1:
            span = x1 - x0
            return y0 + (y1 - y0) * ((x - x0) / span if span else 0.0)
    return points[-1][1]


# ── benchmarks ─────────────────────────────────────────────────────────────
# A number on its own means nothing to a retail viewer: is a 15% EBITDA margin
# good? These give every headline metric a reference line so the card can say
# "above this is healthy" instead of just printing a figure.
#
# Deliberately broad rules of thumb for Indian mainboard issues, not sector
# truth — override per IPO with a `benchmarks:` block in the YAML when you are
# covering something like a bank or a utility where these don't apply.
BENCHMARKS: dict[str, dict[str, Any]] = {
    "ebitda_margin": {"good_at": 15.0, "higher_is_better": True,  "unit": "%"},
    "pat_margin":    {"good_at": 8.0,  "higher_is_better": True,  "unit": "%"},
    "revenue_cagr":  {"good_at": 15.0, "higher_is_better": True,  "unit": "%"},
    "pat_cagr":      {"good_at": 15.0, "higher_is_better": True,  "unit": "%"},
    "ronw":          {"good_at": 15.0, "higher_is_better": True,  "unit": "%"},
    "debt_equity":   {"good_at": 1.0,  "higher_is_better": False, "unit": "x"},
    "pe":            {"good_at": None, "higher_is_better": False, "unit": "x"},  # vs peers
}


def judge(metric: str, value: float, good_at: float | None = None,
          overrides: dict | None = None) -> dict[str, Any]:
    """Score one metric against its benchmark.

    Returns the value, the line it is judged against, which side it falls on,
    and two 0-100 positions so the UI can draw a track with a marker on it.
    """
    spec = dict(BENCHMARKS.get(metric, {"good_at": None, "higher_is_better": True, "unit": ""}))
    if overrides and metric in overrides:
        spec["good_at"] = overrides[metric]
    if good_at is not None:
        spec["good_at"] = good_at

    line = spec.get("good_at")
    higher = bool(spec.get("higher_is_better", True))
    value = float(value or 0.0)

    if line in (None, 0):
        return {"value": value, "good_at": line, "higher_is_better": higher,
                "verdict": "na", "unit": spec.get("unit", ""), "pos": 0, "mark": 0}

    good = value >= line if higher else value <= line
    # Scale the track so the benchmark sits at 50% — the marker is the story.
    span = max(abs(value), abs(line)) * 2 or 1
    return {
        "value": _round(value, 2),
        "good_at": line,
        "higher_is_better": higher,
        "verdict": "good" if good else "poor",
        "unit": spec.get("unit", ""),
        "pos": max(0, min(100, _round(value / span * 100))),
        "mark": max(0, min(100, _round(line / span * 100))),
        "gap_pct": _round((value - line) / line * 100, 1) if line else 0.0,
    }


# ── issue ──────────────────────────────────────────────────────────────────

def issue_metrics(ipo: Ipo) -> dict[str, Any]:
    iss = ipo.issue
    split = iss.fresh_cr + iss.ofs_cr
    # Fall back to the stated total when the split is unknown, so the headline
    # size is right even though fresh-vs-OFS is not. `has_split` lets a scene
    # say "not disclosed" instead of drawing a 0/0 bar that reads as a fact.
    total = split or iss.total_cr
    return {
        "total_cr": _round(total, 2),
        "has_split": split > 0,
        "fresh_pct": _round(_pct(iss.fresh_cr, split), 1),
        "ofs_pct": _round(_pct(iss.ofs_cr, split), 1),
        "min_investment": _round(iss.lot_size * iss.price_high),
        # a fresh-heavy issue funds the company; an OFS-heavy one cashes out
        "is_fresh_heavy": iss.fresh_cr >= iss.ofs_cr,
    }


# ── grey market ────────────────────────────────────────────────────────────

def gmp_metrics(ipo: Ipo, now: datetime | None = None) -> dict[str, Any]:
    latest, prev = ipo.latest_gmp, ipo.prev_gmp
    band = ipo.issue.price_high
    gmp = latest.gmp if latest else 0.0
    prev_gmp = prev.gmp if prev else 0.0
    delta = gmp - prev_gmp

    if not prev:
        movement = "stable"
    elif gmp > prev_gmp * 1.05 and gmp > prev_gmp:
        movement = "surge"
    elif gmp < prev_gmp * 0.95:
        movement = "drop"
    else:
        movement = "stable"

    series = [
        {
            "date": p.date.isoformat() if p.date else None,
            "gmp": p.gmp,
            "pct": _round(_pct(p.gmp, band), 2),
            "kostak": p.kostak,
            "sauda": p.sauda,
            "source": p.source,
        }
        for p in ipo.gmp_history
    ]
    values = [p.gmp for p in ipo.gmp_history]

    today = (now or datetime.now()).date()
    age = (today - latest.date).days if (latest and latest.date) else None

    return {
        # Mirror of compute.js. Is there a quote at all? Every field here
        # defaults to 0 on an empty trail, and 0 is a real premium — an issue
        # trading at par — so without this flag "nobody has quoted it" and
        # "quoted at exactly par" are indistinguishable, and both read as ₹0.
        "has_data": len(series) > 0,
        "gmp": gmp,
        "prev": prev_gmp,
        "delta": _round(delta, 2),
        "pct": _round(_pct(gmp, band), 2),
        "est_listing": _round(band + gmp, 2),
        "gain_per_lot": _round(gmp * ipo.issue.lot_size),
        "movement": movement,
        "kostak": latest.kostak if latest else 0.0,
        "sauda": latest.sauda if latest else 0.0,
        "updated": latest.date.isoformat() if latest and latest.date else None,
        "series": series,
        "peak": max(values) if values else 0.0,
        "trough": min(values) if values else 0.0,
        "days_tracked": len(series),
        # How old the newest reading is. The reels call this number "Today's
        # GMP" in three places; on any day the refresh does not run — or the
        # source is down, which is what happened on 11 Aug — that label turns
        # yesterday's premium into a claim about today. For a channel whose
        # whole premise is a *daily* number, that is the same confident-wrong
        # failure as printing a zero for a figure nobody has.
        "age_days": age,
        "is_stale": age is not None and age > 0,
    }


# ── subscription ───────────────────────────────────────────────────────────

def subscription_metrics(ipo: Ipo) -> dict[str, Any]:
    last = ipo.latest_sub
    if not last:
        return {"has_data": False, "days": []}

    total = last.total
    if total >= 10:
        sentiment = "heavy"
    elif total >= 3:
        sentiment = "good"
    elif total >= 1:
        sentiment = "ok"
    else:
        sentiment = "weak"

    return {
        "has_data": True,
        "day": last.day,
        "qib": last.qib,
        "nii": last.nii,
        "retail": last.retail,
        "employee": last.employee,
        "total": total,
        "max_category": max(last.qib, last.nii, last.retail, last.employee, 1.0),
        "sentiment": sentiment,
        # who is driving demand — useful line for the voiceover
        "leader": max(
            (("qib", last.qib), ("nii", last.nii), ("retail", last.retail)),
            key=lambda kv: kv[1],
        )[0],
        "days": [
            {
                "day": s.day,
                "date": s.date.isoformat() if s.date else None,
                "qib": s.qib, "nii": s.nii, "retail": s.retail,
                "employee": s.employee, "total": s.total,
            }
            for s in ipo.subscription
        ],
    }


# ── financials ─────────────────────────────────────────────────────────────

def financial_metrics(ipo: Ipo) -> dict[str, Any]:
    f = ipo.financials
    n = len(f.years)
    # `years` alone is not data. The scaffold pre-fills FY23/FY24/FY25, so a
    # brand-new IPO passed this check with every figure empty and rendered a
    # financials scene of confident zeros — revenue ₹0, margin 0%, "poor" on
    # every mark. Require an actual number somewhere before claiming data.
    if n == 0 or not any(f.revenue or f.ebitda or f.pat or f.net_worth):
        # `present` belongs here too, all False. The reels read
        # `financials.present.revenue` before checking `has_data`, so leaving
        # the key out threw for every IPO with no financials typed in — six
        # uncaught TypeErrors per render, which is how a card ends up half
        # drawn. An empty answer still has to answer the question.
        return {"has_data": False, "rows": [],
                "present": {"revenue": False, "ebitda": False, "pat": False,
                            "net_worth": False, "total_debt": False}}

    rows = []
    for i, yr in enumerate(f.years):
        rev, ebitda, pat = _safe(f.revenue, i), _safe(f.ebitda, i), _safe(f.pat, i)
        nw, debt = _safe(f.net_worth, i), _safe(f.total_debt, i)
        rows.append({
            "year": yr,
            "revenue": rev,
            "ebitda": ebitda,
            "ebitda_margin": _round(_pct(ebitda, rev), 1),
            "pat": pat,
            "pat_margin": _round(_pct(pat, rev), 1),
            "net_worth": nw,
            "total_debt": debt,
            "ronw": _round(_pct(pat, nw), 1),
            "debt_equity": _round(debt / nw, 2) if nw else 0.0,
        })

    span = n - 1
    first, last = rows[0], rows[-1]
    band = ipo.issue.price_high
    pe = _round(band / f.eps, 1) if f.eps else 0.0
    shares = ipo.issue.shares_post_issue_cr        # crore shares, post-issue
    mcap = _round(band * shares) if (band and shares) else 0.0

    rev_cagr = _round(_cagr(first["revenue"], last["revenue"], span), 1)
    pat_cagr = _round(_cagr(first["pat"], last["pat"], span), 1)
    overrides = getattr(ipo, "benchmarks", None) or {}

    out = {
        "has_data": True,
        "rows": rows,
        "latest": last,
        "revenue_cagr": rev_cagr,
        "ebitda_cagr": _round(_cagr(first["ebitda"], last["ebitda"], span), 1),
        "pat_cagr": pat_cagr,
        "margin_shift_bps": _round((last["ebitda_margin"] - first["ebitda_margin"]) * 100),
        "eps": f.eps,
        "pe": pe,
        "pe_peer_avg": f.pe_peer_avg,
        # positive => priced above peers
        "pe_premium_pct": _round(_pct(pe - f.pe_peer_avg, f.pe_peer_avg), 1) if f.pe_peer_avg else 0.0,
        "market_cap_cr": mcap,
    }

    # Each headline metric gets a reference line so the card can show whether
    # it lands on the healthy side, not just what it is.
    #
    # A metric is only judged when the series behind it actually exists.
    # Without this, an absent array reads as a column of zeros: an IPO that
    # simply had no EBITDA typed in scored a confident "0.0% — poor" against
    # the 15% line, dragged down the fundamentals half of the score, and the
    # analysis draft then wrote "EBITDA margin is poor at 0.0 percent" as a
    # finding about the company. Missing is not zero, and zero is not poor.
    has_rev, has_pat = bool(f.revenue), bool(f.pat)
    has_ebitda, has_nw = bool(f.ebitda), bool(f.net_worth)
    has_debt = bool(f.total_debt)

    def mark(metric: str, value: float, available: bool, **kw) -> dict[str, Any]:
        if not available:
            spec = BENCHMARKS.get(metric, {})
            return {"value": 0.0, "good_at": spec.get("good_at"),
                    "higher_is_better": bool(spec.get("higher_is_better", True)),
                    "verdict": "na", "unit": spec.get("unit", ""),
                    "pos": 0, "mark": 0, "gap_pct": 0.0}
        return judge(metric, value, overrides=overrides, **kw)

    out["marks"] = {
        "ebitda_margin": mark("ebitda_margin", last["ebitda_margin"], has_ebitda and has_rev),
        "pat_margin":    mark("pat_margin", last["pat_margin"], has_pat and has_rev),
        "revenue_cagr":  mark("revenue_cagr", rev_cagr, has_rev and span > 0),
        "pat_cagr":      mark("pat_cagr", pat_cagr, has_pat and span > 0),
        "ronw":          mark("ronw", last["ronw"], has_pat and has_nw),
        "debt_equity":   mark("debt_equity", last["debt_equity"], has_debt and has_nw),
        # P/E is judged against this IPO's own peer group, not a fixed number
        "pe":            mark("pe", pe, bool(f.eps and f.pe_peer_avg),
                              good_at=f.pe_peer_avg or None),
    }
    out["score_good"] = sum(1 for m in out["marks"].values() if m["verdict"] == "good")
    out["score_total"] = sum(1 for m in out["marks"].values() if m["verdict"] != "na")
    # Which series actually exist, so a table can drop a column instead of
    # printing a row of zeros under it. The marks already refuse to judge an
    # absent series; without this the *table* still showed "EBITDA 0 / 0% "
    # for all three years, which is the same false claim in another place.
    out["present"] = {
        "revenue": has_rev, "ebitda": has_ebitda, "pat": has_pat,
        "net_worth": has_nw, "total_debt": has_debt,
    }
    return out


# ── dates / countdown ──────────────────────────────────────────────────────

def date_metrics(ipo: Ipo, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now()
    d = ipo.dates
    out: dict[str, Any] = {
        "announced": d.announced.isoformat() if d.announced else None,
        "open": d.open.isoformat() if d.open else None,
        "close": d.close.isoformat() if d.close else None,
        "close_time": d.close_time,
        "allotment": d.allotment.isoformat() if d.allotment else None,
        "refund": d.refund.isoformat() if d.refund else None,
        "listing": d.listing.isoformat() if d.listing else None,
        "close_at": None,
        "status": "upcoming",
    }
    if d.close:
        hh, _, mm = d.close_time.partition(":")
        try:
            close_at = datetime.combine(
                d.close, datetime.min.time().replace(hour=int(hh), minute=int(mm or 0))
            )
        except ValueError:
            close_at = datetime.combine(d.close, datetime.min.time())
        out["close_at"] = close_at.isoformat()

    today = now.date()
    if d.listing and today >= d.listing:
        out["status"] = "listed"
    elif d.allotment and today >= d.allotment:
        out["status"] = "allotment"
    elif d.close and today > d.close:
        out["status"] = "closed"
    elif d.open and today >= d.open:
        out["status"] = "open"
    return out


# ── listing expectations ───────────────────────────────────────────────────

def listing_metrics(ipo: Ipo) -> dict[str, Any]:
    band = ipo.issue.price_high
    a = ipo.allotment
    return {
        "status": a.status,
        "low": a.listing_low,
        "high": a.listing_high,
        "low_pct": _round(_pct(a.listing_low - band, band), 1) if band else 0.0,
        "high_pct": _round(_pct(a.listing_high - band, band), 1) if band else 0.0,
    }


# ── score ──────────────────────────────────────────────────────────────────
# Five inputs, each marked out of 10, then weighted. A component only counts
# when the data behind it exists, and the total is rescaled by the weight that
# actually applied — so a brand-new IPO with nothing but a GMP is scored *on
# its GMP*, and says so, rather than being marked down to near-zero for the
# four things nobody has typed in yet. That was the old behaviour: the score
# was a hand-moved slider defaulting to 0.0, so every IPO published a
# confident "0.0/10" that meant "no one has judged this", and read as "this is
# a terrible IPO".
#
# `covered_pct` is the honesty valve. Below HONEST_FLOOR the number is not
# worth showing as a verdict, and `has_data` goes False.

SCORE_WEIGHTS = {
    "grey":         25,   # what the grey market will pay over the band
    "demand":       20,   # how many times the book is covered
    "fundamentals": 30,   # the benchmark marks — margins, growth, RoNW, D/E
    "valuation":    15,   # P/E against this issue's own peer set
    "structure":    10,   # fresh money into the company vs a promoter exit
}
HONEST_FLOOR = 40.0       # % of weight that must have data to call it a score

# (input value -> mark out of 10). Read these as the editorial line.
GREY_BAND    = [(-10.0, 0.0), (0.0, 2.0), (5.0, 4.0), (10.0, 5.5),
                (20.0, 7.5), (30.0, 9.0), (50.0, 10.0)]
DEMAND_BAND  = [(0.0, 0.0), (0.5, 2.0), (1.0, 4.0), (3.0, 6.0),
                (10.0, 7.5), (30.0, 9.0), (50.0, 10.0)]
VALUE_BAND   = [(-50.0, 10.0), (-30.0, 9.0), (0.0, 6.0), (30.0, 3.5),
                (100.0, 1.0), (200.0, 0.0)]      # x = P/E premium to peers, %


def score_metrics(ipo: Ipo, d: dict[str, Any]) -> dict[str, Any]:
    """A 0-10 score built only from figures already derived above.

    Returns the mark, and the full breakdown that produced it, so a scene can
    show *why* rather than asking anyone to trust a number.
    """
    gmp, sub, fin, iss = d["gmp"], d["subscription"], d["financials"], d["issue"]
    parts: list[dict[str, Any]] = []

    def add(key: str, has: bool, mark: float, detail: str,
            share: float = 1.0) -> None:
        """`share` is how much of this component's evidence actually exists.

        Fundamentals is seven benchmarks; when only three of them can be
        measured, a clean 3/3 is a real 10 out of 10 *on what was measured*,
        but it is not 30% of the total picture. Carrying a reduced weight
        keeps the mark honest and stops `covered_pct` claiming a completeness
        the data does not have.
        """
        parts.append({
            "key": key,
            "weight": _round(SCORE_WEIGHTS[key] * (share if has else 1.0), 1),
            "full_weight": SCORE_WEIGHTS[key],
            "has_data": bool(has),
            "mark": _round(max(0.0, min(10.0, mark)), 1) if has else None,
            "detail": detail,
        })

    has_grey = bool(ipo.gmp_history) and ipo.issue.price_high > 0
    add("grey", has_grey, _curve(gmp["pct"], GREY_BAND),
        f"GMP is {gmp['pct']}% of the ₹{ipo.issue.price_high:g} band"
        if has_grey else "no GMP logged yet")

    has_demand = bool(sub.get("has_data"))
    add("demand", has_demand, _curve(sub.get("total") or 0.0, DEMAND_BAND),
        f"{sub.get('total')}x overall on day {sub.get('day')}"
        if has_demand else "issue has not opened / no subscription read")

    has_fun = bool(fin.get("has_data")) and fin.get("score_total", 0) > 0
    slots = len(fin.get("marks") or {}) or len(BENCHMARKS)
    measured = fin.get("score_total", 0)
    add("fundamentals", has_fun,
        10.0 * fin.get("score_good", 0) / (measured or 1),
        f"{fin.get('score_good')} of {measured} benchmarks met"
        + (f" ({measured} of {slots} measurable)" if measured < slots else "")
        if has_fun else "no financials entered",
        share=(measured / slots if slots else 1.0))

    has_val = bool(fin.get("has_data")) and fin.get("pe", 0) > 0 and fin.get("pe_peer_avg", 0) > 0
    add("valuation", has_val, _curve(fin.get("pe_premium_pct") or 0.0, VALUE_BAND),
        f"P/E {fin.get('pe')} vs peers {fin.get('pe_peer_avg')} "
        f"({fin.get('pe_premium_pct'):+g}%)" if has_val else "no EPS / peer P/E")

    has_struct = bool(iss.get("has_split"))
    add("structure", has_struct, 2.0 + (iss.get("fresh_pct") or 0.0) / 100.0 * 8.0,
        f"{iss.get('fresh_pct')}% fresh issue" if has_struct
        else "fresh/OFS split not disclosed")

    covered = sum(p["weight"] for p in parts if p["has_data"])
    total_w = float(sum(SCORE_WEIGHTS.values()))
    earned = sum(p["weight"] * p["mark"] for p in parts if p["has_data"])
    value = _round(earned / covered, 1) if covered else 0.0
    covered_pct = _round(covered / total_w * 100)

    # A hand-set score in the YAML always wins — the slider is the editor's
    # override, and an editor who has read the DRHP knows more than this does.
    manual = float(ipo.analysis.score or 0.0)
    return {
        "value": value,
        "components": parts,
        "covered_pct": covered_pct,
        "has_data": covered_pct >= HONEST_FLOOR,
        "missing": [p["key"] for p in parts if not p["has_data"]],
        "manual": manual,
        "source": "manual" if manual > 0 else "auto",
        "effective": manual if manual > 0 else value,
    }


# ── everything, in one bag ─────────────────────────────────────────────────

def derive(ipo: Ipo, now: datetime | None = None) -> dict[str, Any]:
    """The full derived block the frontend and the Excel report both consume."""
    out = {
        "initials": ipo.display_initials,
        "issue": issue_metrics(ipo),
        "gmp": gmp_metrics(ipo, now),
        "subscription": subscription_metrics(ipo),
        "financials": financial_metrics(ipo),
        "dates": date_metrics(ipo, now),
        "listing": listing_metrics(ipo),
    }
    # Last, and it takes `out`: the score is a weighting of the blocks above,
    # never a fresh reading of the IPO.
    out["score"] = score_metrics(ipo, out)
    return out
