"""Derived metrics.

Nothing here is stored — it is all recomputed from `Ipo`. Keep this file the
single source of truth for the arithmetic, and keep frontend/js/compute.js a
faithful mirror of it (the browser recomputes live while you type in the
sidebar; the backend computes the same values for the Excel report).

Rule: an LLM never produces any number in this file.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from .models import Ipo


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
        "value": round(value, 2),
        "good_at": line,
        "higher_is_better": higher,
        "verdict": "good" if good else "poor",
        "unit": spec.get("unit", ""),
        "pos": max(0, min(100, round(value / span * 100))),
        "mark": max(0, min(100, round(line / span * 100))),
        "gap_pct": round((value - line) / line * 100, 1) if line else 0.0,
    }


# ── issue ──────────────────────────────────────────────────────────────────

def issue_metrics(ipo: Ipo) -> dict[str, Any]:
    iss = ipo.issue
    total = iss.fresh_cr + iss.ofs_cr
    return {
        "total_cr": round(total, 2),
        "fresh_pct": round(_pct(iss.fresh_cr, total), 1),
        "ofs_pct": round(_pct(iss.ofs_cr, total), 1),
        "min_investment": round(iss.lot_size * iss.price_high),
        # a fresh-heavy issue funds the company; an OFS-heavy one cashes out
        "is_fresh_heavy": iss.fresh_cr >= iss.ofs_cr,
    }


# ── grey market ────────────────────────────────────────────────────────────

def gmp_metrics(ipo: Ipo) -> dict[str, Any]:
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
            "pct": round(_pct(p.gmp, band), 2),
            "kostak": p.kostak,
            "sauda": p.sauda,
            "source": p.source,
        }
        for p in ipo.gmp_history
    ]
    values = [p.gmp for p in ipo.gmp_history]

    return {
        "gmp": gmp,
        "prev": prev_gmp,
        "delta": round(delta, 2),
        "pct": round(_pct(gmp, band), 2),
        "est_listing": round(band + gmp, 2),
        "gain_per_lot": round(gmp * ipo.issue.lot_size),
        "movement": movement,
        "kostak": latest.kostak if latest else 0.0,
        "sauda": latest.sauda if latest else 0.0,
        "updated": latest.date.isoformat() if latest and latest.date else None,
        "series": series,
        "peak": max(values) if values else 0.0,
        "trough": min(values) if values else 0.0,
        "days_tracked": len(series),
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
    if n == 0:
        return {"has_data": False, "rows": []}

    rows = []
    for i, yr in enumerate(f.years):
        rev, ebitda, pat = _safe(f.revenue, i), _safe(f.ebitda, i), _safe(f.pat, i)
        nw, debt = _safe(f.net_worth, i), _safe(f.total_debt, i)
        rows.append({
            "year": yr,
            "revenue": rev,
            "ebitda": ebitda,
            "ebitda_margin": round(_pct(ebitda, rev), 1),
            "pat": pat,
            "pat_margin": round(_pct(pat, rev), 1),
            "net_worth": nw,
            "total_debt": debt,
            "ronw": round(_pct(pat, nw), 1),
            "debt_equity": round(debt / nw, 2) if nw else 0.0,
        })

    span = n - 1
    first, last = rows[0], rows[-1]
    band = ipo.issue.price_high
    pe = round(band / f.eps, 1) if f.eps else 0.0
    shares = ipo.issue.shares_post_issue_cr        # crore shares, post-issue
    mcap = round(band * shares) if (band and shares) else 0.0

    rev_cagr = round(_cagr(first["revenue"], last["revenue"], span), 1)
    pat_cagr = round(_cagr(first["pat"], last["pat"], span), 1)
    overrides = getattr(ipo, "benchmarks", None) or {}

    out = {
        "has_data": True,
        "rows": rows,
        "latest": last,
        "revenue_cagr": rev_cagr,
        "ebitda_cagr": round(_cagr(first["ebitda"], last["ebitda"], span), 1),
        "pat_cagr": pat_cagr,
        "margin_shift_bps": round((last["ebitda_margin"] - first["ebitda_margin"]) * 100),
        "eps": f.eps,
        "pe": pe,
        "pe_peer_avg": f.pe_peer_avg,
        # positive => priced above peers
        "pe_premium_pct": round(_pct(pe - f.pe_peer_avg, f.pe_peer_avg), 1) if f.pe_peer_avg else 0.0,
        "market_cap_cr": mcap,
    }

    # Each headline metric gets a reference line so the card can show whether
    # it lands on the healthy side, not just what it is.
    out["marks"] = {
        "ebitda_margin": judge("ebitda_margin", last["ebitda_margin"], overrides=overrides),
        "pat_margin":    judge("pat_margin", last["pat_margin"], overrides=overrides),
        "revenue_cagr":  judge("revenue_cagr", rev_cagr, overrides=overrides),
        "pat_cagr":      judge("pat_cagr", pat_cagr, overrides=overrides),
        "ronw":          judge("ronw", last["ronw"], overrides=overrides),
        "debt_equity":   judge("debt_equity", last["debt_equity"], overrides=overrides),
        # P/E is judged against this IPO's own peer group, not a fixed number
        "pe":            judge("pe", pe, good_at=f.pe_peer_avg or None, overrides=overrides),
    }
    out["score_good"] = sum(1 for m in out["marks"].values() if m["verdict"] == "good")
    out["score_total"] = sum(1 for m in out["marks"].values() if m["verdict"] != "na")
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
        "low_pct": round(_pct(a.listing_low - band, band), 1) if band else 0.0,
        "high_pct": round(_pct(a.listing_high - band, band), 1) if band else 0.0,
    }


# ── everything, in one bag ─────────────────────────────────────────────────

def derive(ipo: Ipo, now: datetime | None = None) -> dict[str, Any]:
    """The full derived block the frontend and the Excel report both consume."""
    return {
        "initials": ipo.display_initials,
        "issue": issue_metrics(ipo),
        "gmp": gmp_metrics(ipo),
        "subscription": subscription_metrics(ipo),
        "financials": financial_metrics(ipo),
        "dates": date_metrics(ipo, now),
        "listing": listing_metrics(ipo),
    }
