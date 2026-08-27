"""Can this reel be recorded right now — and until when?

Three existing modules each answer part of "is this IPO usable", and none of
them answers the question you actually have with a camera open:

    doctor   what is MISSING from the record
    grade    are the stored numbers RIGHT, against InvestorGain
    verify   should this record EXIST at all, per NSE and BSE

This one answers **"is reel N ready, and how long does it stay ready"**. Two
independent halves, and keeping them apart is the whole design:

  `window()`     a *time* judgement. Every reel has a natural shelf life that
                 follows from the issue calendar alone — a subscription reel
                 cannot be recorded before bidding opens and is worthless once
                 allotment is out. Pure arithmetic on `dates`, no data needed.

  `data_state()` a *content* judgement. Which fields the scenes in that reel
                 read, whether they are present, whether they are internally
                 consistent, and whether the ones that move daily were read
                 recently enough to still be true.

A reel is READY only when both say yes. That distinction is what stops the
studio showing a confident green light on a subscription reel for an issue
that closed five days ago — the data is all there, complete and valid, and
publishing it would still be misinformation.

Nothing here calls a model or the network. Every judgement is arithmetic on
what is already stored, so it gives the same answer twice and can run in the
browser (mirrored in frontend/js/readiness.js) as cheaply as in a cron job.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from .models import Ipo

# ── clock ──────────────────────────────────────────────────────────────────
# Bidding opens at 10:00 and closes at `dates.close_time` (17:00 unless the
# issue says otherwise). The basis of allotment lands in the evening and
# listing happens at the market open. None of these are in any feed — they are
# how Indian public issues run — so they live here as named constants rather
# than as magic numbers three functions deep.
OPEN_HOUR = 10          # bidding starts
ALLOT_HOUR = 18         # basis of allotment, evening of the allotment date
LIST_HOUR = 10          # listing pop, at the market open

# How long a reading stays true. GMP moves on a grey market that trades all
# day, so yesterday's is a different number; subscription is republished by the
# exchanges every evening of the bidding window.
GMP_FRESH_DAYS = 1      # today or yesterday
GMP_STALE_DAYS = 3      # beyond this it is not a slow day, it is a broken job
SUB_FRESH_DAYS = 1

# A subscription figure read before the exchanges publish the day's close is a
# partial day, correct at the time and wrong by evening. Anything read before
# this hour is treated as provisional rather than as the day's number.
SUB_SETTLED_HOUR = 18


def _at(day: date | None, hour: int, minute: int = 0) -> datetime | None:
    return datetime.combine(day, time(hour=hour, minute=minute)) if day else None


def close_at(ipo: Ipo) -> datetime | None:
    """The bidding cut-off as a *moment*, not a date.

    This is the fix for the whole class of "it says open but the timer reads
    zero" bug. `dates.close` is a day; bidding stops at 17:00 on that day. Any
    comparison that uses the date alone calls an issue open for the seven
    hours after it shut, which is the one window where a reel telling people
    to apply is actively harmful.
    """
    if not ipo.dates.close:
        return None
    hh, _, mm = (ipo.dates.close_time or "17:00").partition(":")
    try:
        return _at(ipo.dates.close, int(hh), int(mm or 0))
    except ValueError:
        return _at(ipo.dates.close, 17, 0)


def open_at(ipo: Ipo) -> datetime | None:
    return _at(ipo.dates.open, OPEN_HOUR)


def allot_at(ipo: Ipo) -> datetime | None:
    return _at(ipo.dates.allotment, ALLOT_HOUR)


def list_at(ipo: Ipo) -> datetime | None:
    return _at(ipo.dates.listing, LIST_HOUR)


def status(ipo: Ipo, now: datetime | None = None) -> str:
    """upcoming | open | closed | allotment | listed, judged on the clock.

    Mirrors compute.date_metrics, which now delegates here rather than keeping
    a second copy of the ladder. Read top-down: the latest milestone reached
    wins, so an issue past its listing is `listed` even though it is also past
    its close.
    """
    now = now or datetime.now()
    lst, alt, shut = list_at(ipo), allot_at(ipo), close_at(ipo)
    if lst and now >= lst:
        return "listed"
    if alt and now >= alt:
        return "allotment"
    if shut and now > shut:
        return "closed"
    opn = open_at(ipo)
    if opn and now >= opn:
        return "open"
    return "upcoming"


# ── the windows ────────────────────────────────────────────────────────────
# One entry per reel. `frm` and `to` are functions of the IPO's calendar, and
# each carries the reason it ends where it does — because "why can I no longer
# record this" is the question the studio has to answer at a glance.
#
# Where a reel has no anchor date at all (an issue announced with no calendar
# yet) the window comes back open-ended rather than empty: a missing date is a
# gap in the record, which is `data_state`'s business, not a reason to declare
# a reel un-recordable.

# How early an apply/skip case can be made. A day before bidding opens is a
# useful preview; a week before, the terms are still moving.
PREVIEW_DAYS = 1
# Announcement is often unrecorded. Fall back to a week before the open, which
# is roughly when a mainboard issue's terms become public.
ANNOUNCE_FALLBACK_DAYS = 7


def _announced_at(ipo: Ipo) -> datetime | None:
    if ipo.dates.announced:
        return _at(ipo.dates.announced, OPEN_HOUR)
    if ipo.dates.open:
        return _at(ipo.dates.open - timedelta(days=ANNOUNCE_FALLBACK_DAYS), OPEN_HOUR)
    return None


def _preview_at(ipo: Ipo) -> datetime | None:
    opn = open_at(ipo)
    return opn - timedelta(days=PREVIEW_DAYS) if opn else None


WINDOWS: dict[int, dict[str, Any]] = {
    1: {
        "from": _announced_at,
        "to": close_at,
        # Terms, lot size and the fresh/OFS split are a decision aid. Once
        # bidding is shut nobody can act on them, and a "here is the price
        # band" Short published that evening reads as an invitation to apply.
        "ends": "bidding closes",
        "starts": "terms are public",
    },
    2: {
        "from": _announced_at,
        "to": list_at,
        # The grey market is the one thing that keeps trading after the issue
        # shuts — right up to the listing print, which settles the bet. That
        # makes the GMP reel the longest-lived of the six.
        "ends": "it lists and the premium becomes a fact",
        "starts": "the grey market starts quoting",
    },
    3: {
        "from": open_at,
        "to": allot_at,
        # Day-wise demand exists only from the first bid. It stays interesting
        # through the close (the final multiple is the headline) and dies at
        # allotment, when the question becomes "did I get it".
        "ends": "allotment replaces demand as the story",
        "starts": "bidding opens",
    },
    4: {
        "from": _preview_at,
        "to": close_at,
        "ends": "bidding closes",
        "starts": "the day before bidding opens",
    },
    5: {
        "from": _preview_at,
        "to": close_at,
        "ends": "bidding closes",
        "starts": "the day before bidding opens",
    },
    6: {
        # Allotment odds, the registrar link and the listing estimate. None of
        # it is actionable while you can still apply, and all of it is what a
        # viewer wants the hour after bidding shuts.
        "from": close_at,
        "to": lambda i: (list_at(i) + timedelta(days=1)) if list_at(i) else None,
        "ends": "the day after listing",
        "starts": "bidding closes",
    },
}


def window(ipo: Ipo, reel: int, now: datetime | None = None) -> dict[str, Any]:
    """When reel N may be recorded, and where `now` sits in that span."""
    now = now or datetime.now()
    spec = WINDOWS.get(reel)
    if not spec:
        return {"state": "live", "from": None, "to": None}

    frm, to = spec["from"](ipo), spec["to"](ipo)
    # An unknown edge is an open edge. The alternative — treating "no listing
    # date yet" as "the window is closed" — would grey out every reel on a
    # freshly discovered IPO, which is exactly when you want to record one.
    if frm and now < frm:
        state = "early"
    elif to and now > to:
        state = "expired"
    else:
        state = "live"

    left = None
    if state == "live" and to:
        left = round((to - now).total_seconds() / 3600, 1)

    return {
        "state": state,
        "from": frm.isoformat(timespec="minutes") if frm else None,
        "to": to.isoformat(timespec="minutes") if to else None,
        "starts": spec["starts"],
        "ends": spec["ends"],
        "hours_left": left,
    }


# ── what each reel reads ───────────────────────────────────────────────────
# (key, label, level, test). `level` is "need" when the scene is unrecordable
# without it and "want" when the scene degrades but still plays.
#
# Every entry names a real scene in frontend/js/reels.js. Adding a scene there
# without adding its inputs here produces a reel that lights green and records
# an empty frame, which is the failure this module exists to prevent.

def _fin(ipo: Ipo, name: str) -> bool:
    return any(float(v or 0) for v in getattr(ipo.financials, name, []) or [])


NEEDS: dict[int, list[tuple[str, str, str, Any]]] = {
    1: [
        ("company",   "Company name",   "need", lambda i: bool(i.company.strip())),
        ("band",      "Price band",     "need", lambda i: i.issue.price_high > 0),
        ("lot",       "Lot size",       "need", lambda i: i.issue.lot_size > 0),
        ("size",      "Issue size",     "need", lambda i: (i.issue.fresh_cr + i.issue.ofs_cr) > 0 or i.issue.total_cr > 0),
        ("dates",     "Open / close",   "need", lambda i: bool(i.dates.open and i.dates.close)),
        ("overview",  "Overview bullets", "need", lambda i: len(i.analysis.overview) >= 3),
        ("split",     "Fresh / OFS split", "want", lambda i: (i.issue.fresh_cr + i.issue.ofs_cr) > 0),
        # "want", not "need": without it the reservation scene is dropped from
        # the reel rather than rendering empty, so the video is still
        # recordable — one scene shorter and honest. Both halves are required,
        # because a slice with no total is a number with no denominator.
        ("reservation", "Reservation split", "want",
         lambda i: i.issue.shares_total > 0 and any(
             (i.issue.shares_qib, i.issue.shares_nii,
              i.issue.shares_retail, i.issue.shares_employee))),
        ("about",     "Company facts",  "want", lambda i: len(i.analysis.about_facts) > 0),
        ("sector",    "Sector",         "want", lambda i: bool(i.sector.strip())),
        ("listing",   "Listing date",   "want", lambda i: bool(i.dates.listing)),
    ],
    2: [
        ("gmp",       "A GMP reading",  "need", lambda i: len(i.gmp_history) > 0),
        ("band",      "Price band",     "need", lambda i: i.issue.price_high > 0),
        ("lot",       "Lot size",       "need", lambda i: i.issue.lot_size > 0),
        # One point is a dot, not a trail. The `trail` scene is 7 of the
        # reel's ~19 seconds and renders a single-row table without this.
        ("trail",     "Several GMP days", "want", lambda i: len(i.gmp_history) >= 3),
    ],
    3: [
        ("sub",       "Subscription",   "need", lambda i: len(i.subscription) > 0),
        ("cats",      "Category split", "need", lambda i: bool(i.subscription) and any(
            (s.qib or s.nii or s.retail) for s in i.subscription)),
        ("trend",     "Two or more days", "want", lambda i: len(i.subscription) >= 2),
    ],
    4: [
        ("revenue",   "Revenue",        "need", lambda i: _fin(i, "revenue")),
        ("pat",       "PAT",            "need", lambda i: _fin(i, "pat")),
        ("flags",     "Green / red flags", "need",
         lambda i: bool(i.analysis.green_flags and i.analysis.red_flags)),
        ("band",      "Price band",     "need", lambda i: i.issue.price_high > 0),
        ("lot",       "Lot size",       "need", lambda i: i.issue.lot_size > 0),
        ("ebitda",    "EBITDA",         "want", lambda i: _fin(i, "ebitda")),
        ("worth",     "Net worth",      "want", lambda i: _fin(i, "net_worth")),
        ("eps",       "EPS",            "want", lambda i: i.financials.eps > 0),
        ("peer",      "Peer P/E",       "want", lambda i: i.financials.pe_peer_avg > 0),
        # The `stake` scene prices the cheque and the upside. Without a GMP it
        # can still show the cheque, so this is a want and not a need.
        ("gmp",       "A GMP reading",  "want", lambda i: len(i.gmp_history) > 0),
    ],
    5: [
        ("verdict",   "Verdict",        "need", lambda i: bool(i.analysis.verdict)),
        ("reco",      "Retail / HNI / long calls", "need",
         lambda i: bool(i.analysis.reco_retail and i.analysis.reco_hni and i.analysis.reco_long)),
        # The score is computed, never typed — but it is honest only when
        # enough of the inputs exist. See compute.score_metrics's HONEST_FLOOR.
        ("score",     "Enough data to score", "need",
         lambda i: None),          # filled from `derived`, see data_state
    ],
    6: [
        ("registrar", "Registrar",      "need", lambda i: bool(i.issue.registrar.strip())),
        ("regurl",    "Registrar link", "need", lambda i: bool(i.issue.registrar_url.strip())),
        ("allotdate", "Allotment date", "need", lambda i: bool(i.dates.allotment)),
        ("listdate",  "Listing date",   "need", lambda i: bool(i.dates.listing)),
        ("range",     "Expected listing range", "want",
         lambda i: i.allotment.listing_high > 0 or len(i.gmp_history) > 0),
    ],
}


# ── freshness ──────────────────────────────────────────────────────────────

def freshness(ipo: Ipo, now: datetime | None = None) -> dict[str, Any]:
    """Were the two moving numbers read recently enough to still be true?

    Only meaningful while they are still moving. A GMP last read the day an
    issue listed is not stale, it is final — so every judgement here is fenced
    by the status ladder rather than by the calendar alone.
    """
    now = now or datetime.now()
    today = now.date()
    state = status(ipo, now)
    out: dict[str, Any] = {}

    # ── GMP: quoted from announcement right through to listing.
    gmp_matters = state in ("upcoming", "open", "closed", "allotment")
    last_gmp = ipo.gmp_history[-1].date if ipo.gmp_history else None
    age = (today - last_gmp).days if last_gmp else None
    out["gmp"] = {
        "matters": gmp_matters,
        "last": last_gmp.isoformat() if last_gmp else None,
        "age_days": age,
        "state": (
            "n/a" if not gmp_matters else
            "missing" if age is None else
            "fresh" if age <= GMP_FRESH_DAYS else
            "stale" if age <= GMP_STALE_DAYS else "cold"
        ),
    }

    # ── Subscription: exists only during bidding, and the day's real number
    # is not published until the evening. A day-3 figure read at noon is a
    # running total, so it is reported `provisional` rather than `fresh` —
    # recording a "final demand" reel off it is how a 60x closes at 184x.
    sub_matters = state == "open"
    last_sub = ipo.subscription[-1].date if ipo.subscription else None
    sub_age = (today - last_sub).days if last_sub else None
    settled = now.hour >= SUB_SETTLED_HOUR
    out["subscription"] = {
        "matters": sub_matters,
        "last": last_sub.isoformat() if last_sub else None,
        "age_days": sub_age,
        "state": (
            "n/a" if not sub_matters else
            "missing" if sub_age is None else
            "provisional" if sub_age == 0 and not settled else
            "fresh" if sub_age <= SUB_FRESH_DAYS else "stale"
        ),
    }
    return out


# ── validity ───────────────────────────────────────────────────────────────
# Presence is not correctness. These are the contradictions a record can hold
# while every `doctor` check passes — a band whose low exceeds its high, a
# listing before the close, an EBITDA larger than the revenue it came out of.
# Each one would render as a confident number on a card.

def problems(ipo: Ipo, now: datetime | None = None) -> list[dict[str, str]]:
    """Internal contradictions, worst first. Empty means the record hangs
    together — not that it is complete, which is `doctor`'s question."""
    now = now or datetime.now()
    out: list[dict[str, str]] = []

    def bad(sev: str, what: str, detail: str) -> None:
        out.append({"severity": sev, "what": what, "detail": detail})

    iss, d, f = ipo.issue, ipo.dates, ipo.financials

    # ── issue terms
    if iss.price_low and iss.price_high and iss.price_low > iss.price_high:
        bad("error", "Price band", f"low ₹{iss.price_low:g} is above high ₹{iss.price_high:g}")
    if iss.price_high and iss.lot_size:
        cheque = iss.price_high * iss.lot_size
        # SEBI sets the retail minimum application at ₹10,000–₹15,000. A lot
        # value far outside that is a lot size read from the wrong row, which
        # is silent everywhere else and wrong in every "minimum investment".
        if ipo.board == "Mainboard" and not (9_000 <= cheque <= 16_500):
            bad("warn", "Lot value",
                f"₹{cheque:,.0f} per lot — SEBI's retail minimum is ₹10k–₹15k, "
                f"so the lot size or the band is likely wrong")
    if iss.fresh_cr and iss.ofs_cr and iss.total_cr:
        parts = iss.fresh_cr + iss.ofs_cr
        if abs(parts - iss.total_cr) > max(1.0, iss.total_cr * 0.02):
            bad("warn", "Issue size",
                f"fresh + OFS = ₹{parts:,.0f} Cr but total says ₹{iss.total_cr:,.0f} Cr")

    # ── calendar, in the only order it can happen in
    seq = [("open", d.open), ("close", d.close), ("allotment", d.allotment),
           ("refund", d.refund), ("listing", d.listing)]
    known = [(name, day) for name, day in seq if day]
    for (n1, d1), (n2, d2) in zip(known, known[1:]):
        if d2 < d1:
            bad("error", "Calendar", f"{n2} ({d2}) falls before {n1} ({d1})")
    if d.announced and d.open and d.announced > d.open:
        bad("error", "Calendar", f"announced ({d.announced}) is after open ({d.open})")

    # ── GMP
    if ipo.gmp_history:
        days = [p.date for p in ipo.gmp_history if p.date]
        if len(days) != len(set(days)):
            bad("error", "GMP", "two readings share a date")
        if iss.price_high:
            for p in ipo.gmp_history:
                if abs(p.gmp) > iss.price_high * 2:
                    bad("warn", "GMP",
                        f"₹{p.gmp:g} on {p.date} is more than 2× the ₹{iss.price_high:g} "
                        f"band — likely a units or a company mix-up")
                    break
        if d.listing:
            late = [p.date for p in ipo.gmp_history if p.date and p.date > d.listing]
            if late:
                bad("warn", "GMP", f"{len(late)} reading(s) dated after the listing date")
        future = [p.date for p in ipo.gmp_history if p.date and p.date > now.date()]
        if future:
            bad("error", "GMP", f"{len(future)} reading(s) dated in the future")

    # ── subscription
    if ipo.subscription:
        nums = [s.day for s in ipo.subscription]
        if len(nums) != len(set(nums)):
            bad("error", "Subscription", "two rows share a day number")
        for s in ipo.subscription:
            cats = [s.qib, s.nii, s.retail]
            if s.total and max(cats) and s.total > max(cats) * 1.05 and s.total > sum(cats):
                bad("warn", "Subscription",
                    f"day {s.day} total {s.total:g}× exceeds every category and their sum")
            if any(v < 0 for v in cats + [s.total]):
                bad("error", "Subscription", f"day {s.day} has a negative multiple")
        if d.open and d.close:
            span = (d.close - d.open).days + 1
            if len(ipo.subscription) > span:
                bad("warn", "Subscription",
                    f"{len(ipo.subscription)} days recorded for a {span}-day window")
        # Demand is cumulative through the bidding window: an exchange total
        # that falls day-on-day means a day was overwritten with a partial read.
        totals = [s.total for s in sorted(ipo.subscription, key=lambda s: s.day)]
        for i in range(1, len(totals)):
            if totals[i] and totals[i - 1] and totals[i] < totals[i - 1] * 0.98:
                bad("warn", "Subscription",
                    f"day {i + 1} total ({totals[i]:g}×) is below day {i} "
                    f"({totals[i - 1]:g}×) — demand does not go backwards")
                break

    # ── financials
    n = len(f.years)
    for name in ("revenue", "ebitda", "pat", "net_worth", "total_debt"):
        series = getattr(f, name)
        if series and len(series) != n:
            bad("warn", "Financials",
                f"{name} has {len(series)} values for {n} year(s)")
    for i, yr in enumerate(f.years):
        rev = f.revenue[i] if i < len(f.revenue) else 0
        ebitda = f.ebitda[i] if i < len(f.ebitda) else 0
        pat = f.pat[i] if i < len(f.pat) else 0
        if rev and ebitda and ebitda > rev:
            bad("error", "Financials", f"{yr}: EBITDA ₹{ebitda:g} Cr exceeds revenue ₹{rev:g} Cr")
        if ebitda and pat and pat > ebitda * 1.05:
            bad("warn", "Financials", f"{yr}: PAT ₹{pat:g} Cr exceeds EBITDA ₹{ebitda:g} Cr")
        if rev < 0:
            bad("error", "Financials", f"{yr}: negative revenue")
    if f.eps and iss.price_high:
        if f.eps < 0:
            # Not a bad number — a company with no earnings to price. Worth
            # saying because reel 4's valuation scene is built around a P/E,
            # and "no P/E" is a different script from "an expensive one".
            bad("warn", "Valuation",
                f"EPS is negative ({f.eps:g}) — the issue is loss-making, so "
                f"there is no P/E and reel 4's valuation scene has no multiple "
                f"to show")
        else:
            pe = iss.price_high / f.eps
            if pe < 1 or pe > 400:
                bad("warn", "Valuation",
                    f"implied P/E of {pe:,.0f}× — check the EPS")

    out.sort(key=lambda p: 0 if p["severity"] == "error" else 1)
    return out


# ── the verdict ────────────────────────────────────────────────────────────

def data_state(ipo: Ipo, reel: int, derived: dict[str, Any] | None = None,
               now: datetime | None = None) -> dict[str, Any]:
    """Which of reel N's inputs are present, and how fresh the moving ones are."""
    now = now or datetime.now()
    missing_need: list[str] = []
    missing_want: list[str] = []

    for key, label, level, test in NEEDS.get(reel, []):
        if key == "score":
            # The only input that is derived rather than stored.
            ok = bool((derived or {}).get("score", {}).get("has_data"))
        else:
            ok = bool(test(ipo))
        if not ok:
            (missing_need if level == "need" else missing_want).append(label)

    fresh = freshness(ipo, now)
    # Only the reels that actually render the moving number care about it.
    watched = {2: ["gmp"], 3: ["subscription"], 4: ["gmp"]}.get(reel, [])
    stale = [k for k in watched
             if fresh[k]["matters"] and fresh[k]["state"] in ("stale", "cold", "provisional")]

    return {"missing": missing_need, "soft": missing_want,
            "stale": stale, "freshness": fresh}


def reel_state(ipo: Ipo, reel: int, derived: dict[str, Any] | None = None,
               now: datetime | None = None) -> dict[str, Any]:
    """The one call the studio and the cron job both make.

    `state` is what the dot shows:
        ready    green, and blinking if the window is closing today
        partial  amber — recordable, but a scene will be thin or a number old
        blocked  red — a required field is absent; recording produces a lie
        early    grey — the window has not opened
        expired  grey — the window has shut
    """
    now = now or datetime.now()
    win = window(ipo, reel, now)
    dat = data_state(ipo, reel, derived, now)

    if win["state"] in ("early", "expired"):
        state = win["state"]
    elif dat["missing"]:
        state = "blocked"
    elif dat["stale"] or dat["soft"]:
        state = "partial"
    else:
        state = "ready"

    # "Blinking" is not decoration: it separates "you can record this" from
    # "you can record this today and not tomorrow", which is the only reason
    # to look at the board in the morning.
    urgent = bool(state in ("ready", "partial")
                  and win["hours_left"] is not None and win["hours_left"] <= 24)

    return {"reel": reel, "state": state, "urgent": urgent,
            "window": win, **dat}


def report(ipo: Ipo, derived: dict[str, Any] | None = None,
           now: datetime | None = None) -> dict[str, Any]:
    """Every reel for one IPO, plus the record-level problems."""
    now = now or datetime.now()
    reels = {n: reel_state(ipo, n, derived, now) for n in sorted(NEEDS)}
    bad = problems(ipo, now)
    ready = [n for n, r in reels.items() if r["state"] == "ready"]
    # READY reels only, mirroring readiness.js. A per-reel `urgent` may fire on
    # an amber reel — "thin AND expires today" is worth flagging on that reel.
    # The roll-up drives one dot per IPO and has to mean one thing: there is a
    # video you can shoot and it expires today.
    urgent = [n for n in ready if reels[n]["urgent"]]
    return {
        "slug": ipo.slug,
        "company": ipo.company or ipo.slug,
        "status": status(ipo, now),
        "close_at": (close_at(ipo).isoformat(timespec="minutes")
                     if close_at(ipo) else None),
        "reels": reels,
        "ready": ready,
        "ready_count": len(ready),
        "urgent": bool(urgent),
        "problems": bad,
        "errors": sum(1 for p in bad if p["severity"] == "error"),
        # One IPO, one line: how many of the six can be shot right now, and
        # whether anything in the record is self-contradictory.
        "ok": bool(ready) and not any(p["severity"] == "error" for p in bad),
    }
