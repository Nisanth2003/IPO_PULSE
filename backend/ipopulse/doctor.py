"""Check what is missing, and repair what is derivable.

Two jobs, deliberately kept apart:

  `inspect()`  says what is absent and **which scene goes blank because of
               it**. That last part is the whole point. A blank panel in the
               studio looks like a rendering bug; it is almost always a field
               nobody filled, three layers away in a YAML file.

  `repair()`   fills only what follows arithmetically from data already
               present — a total from its two parts, the T+3 calendar from a
               close date, a registrar's status URL from the registrar's name.

Nothing here invents a figure. The line is: if two people with the same YAML
would write down different numbers, it is not a repair. That rules out the
tempting ones — carrying a GMP forward across a day nobody read it, guessing a
listing range from the current premium, inferring a sector from the name.
Those stay in `inspect()` as findings, forever, rather than becoming quiet
fabrications that later read as data.

`ipopulse doctor` prints the findings; `--fix` applies the repairs.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Callable

from .models import Ipo

# Registrar -> the page where an applicant checks allotment. Mirrors REGISTRARS
# in frontend/js/reels.js; a name with no URL renders a dead link on reel 6.
REGISTRAR_URLS: dict[str, str] = {
    "KFintech": "https://kosmic.kfintech.com/ipostatus/",
    "MUFG Intime (Link Intime)": "https://in.mpms.mufg.com/Initial_Offer/public-issues.html",
    "Link Intime": "https://in.mpms.mufg.com/Initial_Offer/public-issues.html",
    "Bigshare Services": "https://ipo.bigshareonline.com/ipo_status.html",
    "Maashitla Securities": "https://maashitla.com/allotment-status/public-issues",
    "Skyline Financial": "https://www.skylinerta.com/ipo.php",
    "Cameo Corporate": "https://ipo.cameoindia.com/",
}


def _match_registrar(name: str) -> str | None:
    """Registrars are written a dozen ways — 'KFin Technologies Limited',
    'Kfintech', 'MUFG Intime India Private Limited'. Match on a squashed
    substring rather than equality, or the lookup only ever hits on the
    handful of spellings someone happened to type."""
    squash = "".join(ch for ch in name.lower() if ch.isalnum())
    if not squash:
        return None
    for key, url in REGISTRAR_URLS.items():
        token = "".join(ch for ch in key.lower() if ch.isalnum())[:8]
        if token and token in squash:
            return url
    return None


def _working_days_after(start: date, days: int) -> date:
    """SEBI's T+3 calendar, weekends skipped. Does not know exchange
    holidays, so a date landing on one is a day early — acceptable for
    filling a blank, which is why a typed value always wins."""
    day, added = start, 0
    while added < days:
        day += timedelta(days=1)
        if day.weekday() < 5:
            added += 1
    return day


# ── what to check ──────────────────────────────────────────────────────────
# (label, getter, who fills it, severity, what breaks without it)
#
# `severity` drives the exit code: "blank" means a scene renders empty or
# renders a zero as though it were a fact, which is the failure mode worth
# catching before a recording rather than after.

Check = tuple[str, Callable[[Ipo], Any], str, str, str]

CHECKS: list[Check] = [
    ("Price band",      lambda i: i.issue.price_high,                "nse",      "blank", "reel 1 terms, every % figure, the score"),
    ("Lot size",        lambda i: i.issue.lot_size,                  "nse",      "blank", "reel 1 terms, minimum investment"),
    ("Issue size",      lambda i: i.issue.fresh_cr + i.issue.ofs_cr or i.issue.total_cr, "nse", "blank", "reel 1 hook headline"),
    ("Fresh/OFS split", lambda i: i.issue.fresh_cr + i.issue.ofs_cr, "rhp",      "blank", "reel 1 split, score: structure"),
    ("Open / close",    lambda i: 1 if i.dates.close else 0,         "nse",      "blank", "status, countdown, reel 1 dates"),
    ("Listing date",    lambda i: 1 if i.dates.listing else 0,       "derived",  "blank", "reel 1 dates, reel 6 listing"),
    ("Announced date",  lambda i: 1 if i.dates.announced else 0,     "you",      "note",  "reel 1 dates omits the row"),
    ("GMP",             lambda i: len(i.gmp_history),                "research", "blank", "reel 2 entirely, score: grey"),
    ("Subscription",    lambda i: len(i.subscription),               "nse",      "blank", "reel 3 entirely, score: demand"),
    ("Revenue",         lambda i: len(i.financials.revenue),         "rhp",      "blank", "reel 4 financials, score: fundamentals"),
    ("EBITDA",          lambda i: len(i.financials.ebitda),          "rhp",      "blank", "reel 4 financials + margins"),
    ("PAT",             lambda i: len(i.financials.pat),             "rhp",      "blank", "reel 4 financials, PAT margin"),
    ("Net worth",       lambda i: len(i.financials.net_worth),       "rhp",      "blank", "RoNW and D/E marks"),
    ("EPS",             lambda i: i.financials.eps,                  "rhp",      "blank", "P/E, reel 4 valuation"),
    ("Peer P/E",        lambda i: i.financials.pe_peer_avg,          "you",      "blank", "reel 4 valuation, score: valuation"),
    ("Shares post-issue", lambda i: i.issue.shares_post_issue_cr,    "rhp",      "note",  "market cap on reel 1 terms"),
    ("Sector",          lambda i: 1 if i.sector else 0,              "you",      "note",  "reel 1 hook + company subtitle"),
    ("Registrar",       lambda i: 1 if i.issue.registrar else 0,     "nse",      "blank", "reel 6 status"),
    ("Registrar URL",   lambda i: 1 if i.issue.registrar_url else 0, "derived",  "note",  "reel 6 allotment link"),
    ("Overview bullets", lambda i: len(i.analysis.overview),         "analyse",  "blank", "reel 1 company"),
    ("Green / red flags", lambda i: len(i.analysis.green_flags) + len(i.analysis.red_flags), "analyse", "blank", "reel 4 flags"),
    ("Valuation line",  lambda i: 1 if i.analysis.valuation else 0,  "analyse",  "note",  "reel 4 valuation caption"),
    ("Key risk",        lambda i: 1 if i.analysis.risk else 0,       "analyse",  "note",  "reel 4 flags footer"),
    ("Expected listing range", lambda i: i.allotment.listing_high,   "you",      "note",  "reel 6 falls back to the GMP-implied range"),
]

# who -> the command that fills it
FILLERS = {
    "nse":      "ipopulse sync --provider nse",
    "research": "ipopulse refresh            (or: ipopulse research <slug> --write)",
    "analyse":  "ipopulse analyse <slug> --write",
    "derived":  "ipopulse doctor <slug> --fix",
    "rhp":      "type it into backend/data/ipos/<slug>.yaml — it is in the RHP PDF",
    "you":      "type it into backend/data/ipos/<slug>.yaml",
}


# ── inspect ────────────────────────────────────────────────────────────────

def gmp_gaps(ipo: Ipo, today: date | None = None) -> list[str]:
    """Days inside the tracking window with no GMP reading.

    Every calendar day counts, weekends included. The grey market is an
    informal dealer network rather than an exchange — it quotes through the
    weekend, the tracking sites publish a value every day, and the `grey` job
    is scheduled `30 15 * * *`, which fires Saturday and Sunday too. An
    earlier version of this skipped Sat/Sun, which quietly reclassified two
    genuinely lost readings per week as "expected", and so hid exactly the
    data loss it existed to surface.

    It matters because reel 2's trail is billed as a *daily* series: a missing
    day reads as "the premium held steady" rather than "nobody looked".
    """
    dates = sorted({p.date for p in ipo.gmp_history if p.date})
    if not dates:
        return []
    today = today or date.today()
    # The window closes at listing: after that there is no grey market left.
    end = min(today, ipo.dates.listing or today)
    have, out, day = set(dates), [], dates[0]
    while day <= end:
        if day not in have:
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out


def sub_gaps(ipo: Ipo) -> list[int]:
    """Bidding days with no subscription row, below the latest day recorded.

    The same hole as `gmp_gaps`, in the other daily series. Reel 3's trend
    scene is a table headed "day-wise", so a jump from Day 1 to Day 5 is a
    visible claim that nothing happened in between. Unlike GMP these are also
    unrecoverable after the fact: the exchange publishes a running cumulative
    figure for *today*, not an archive, so a day the job did not run is gone.
    """
    days = sorted({int(s.day) for s in ipo.subscription if s.day})
    if not days:
        return []
    return [d for d in range(1, days[-1] + 1) if d not in set(days)]


def inconsistencies(ipo: Ipo) -> list[str]:
    """Data that is present but cannot all be true at once.

    A missing field blanks a scene, which is visible. A *wrong* one renders
    perfectly and is believed — so these checks are about internal agreement
    rather than completeness. Everything here is arithmetic or calendar
    ordering: no judgement, no thresholds anyone could argue about, except
    the two clearly-labelled plausibility bands at the end.
    """
    out: list[str] = []
    f, iss, d = ipo.financials, ipo.issue, ipo.dates
    n = len(f.years)

    # 1. Series that do not line up with `years`. compute.py zips them by
    #    index, so a short array silently shifts every figure into the wrong
    #    financial year — the single most dangerous shape in the file.
    for key in ("revenue", "ebitda", "pat", "net_worth", "total_debt"):
        vals = getattr(f, key, None) or []
        if vals and len(vals) != n:
            out.append(f"financials.{key} has {len(vals)} values for {n} years "
                       f"— figures will land in the wrong year")

    # 2. Relationships that cannot hold. EBITDA is earnings BEFORE interest,
    #    tax, depreciation — it cannot exceed the revenue it is computed from.
    rev, ebitda, pat = f.revenue or [], f.ebitda or [], f.pat or []
    for i, yr in enumerate(f.years):
        r = rev[i] if i < len(rev) else None
        e = ebitda[i] if i < len(ebitda) else None
        p = pat[i] if i < len(pat) else None
        if r is not None and e is not None and r > 0 and e > r:
            out.append(f"{yr}: EBITDA ₹{e:g} Cr exceeds revenue ₹{r:g} Cr")
        if e is not None and p is not None and e > 0 and p > e * 1.05:
            out.append(f"{yr}: PAT ₹{p:g} Cr exceeds EBITDA ₹{e:g} Cr")
        if r is not None and r < 0:
            out.append(f"{yr}: negative revenue ₹{r:g} Cr")

    # 3. The issue must add up.
    split = iss.fresh_cr + iss.ofs_cr
    if split > 0 and iss.total_cr > 0 and abs(split - iss.total_cr) > 0.5:
        out.append(f"issue total ₹{iss.total_cr:g} Cr != fresh ₹{iss.fresh_cr:g} "
                   f"+ OFS ₹{iss.ofs_cr:g} = ₹{split:g} Cr")
    if iss.price_low and iss.price_high and iss.price_low > iss.price_high:
        out.append(f"price band inverted: ₹{iss.price_low:g} low > ₹{iss.price_high:g} high")

    # 4. The calendar has to run forwards.
    seq = [("open", d.open), ("close", d.close), ("allotment", d.allotment),
           ("refund", d.refund), ("listing", d.listing)]
    seen = [(k, v) for k, v in seq if v]
    for (ka, va), (kb, vb) in zip(seen, seen[1:]):
        if vb < va:
            out.append(f"dates.{kb} ({vb}) falls before dates.{ka} ({va})")
    if d.announced and d.open and d.announced > d.open:
        out.append(f"dates.announced ({d.announced}) is after the open ({d.open})")

    # 5. Financial years that predate the issue. An IPO listing in 2026 files
    #    an RHP with FY24-FY26; a table ending at FY25 is a stale document,
    #    which is worth knowing before those numbers go on camera.
    # Only when a series actually carries figures: `years` alone is the
    # scaffold's FY23-FY25 placeholder, so checking it unconditionally flagged
    # every IPO that simply has no financials yet — duplicating the "Revenue
    # blank" finding as a scarier-looking one.
    year_of = (d.listing or d.open or d.close)
    if year_of and f.years and any(rev or ebitda or pat or f.net_worth):
        last = str(f.years[-1])
        digits = "".join(ch for ch in last if ch.isdigit())
        if len(digits) in (2, 4):
            fy = int(digits) + (2000 if len(digits) == 2 else 0)
            # Two full years behind, not one. An SME filing in mid-2026 often
            # carries FY25 as its latest audited set quite legitimately, so a
            # one-year gap is normal rather than a finding.
            if fy <= year_of.year - 2:
                out.append(f"financials end at {last} for an issue listing in "
                           f"{year_of.year} — likely a superseded document")

    # 6. Two plausibility bands, stated as such. SEBI fixes the retail lot at
    #    roughly ₹14-15k on the mainboard and ₹1-1.5 lakh on SME, so a lot
    #    value far outside that usually means the lot size or the band is wrong.
    lot_value = iss.lot_size * iss.price_high
    if lot_value:
        lo, hi = (95_000, 210_000) if ipo.board == "SME" else (10_000, 20_000)
        if not (lo <= lot_value <= hi):
            out.append(f"one lot is ₹{lot_value:,.0f} — outside the usual "
                       f"₹{lo:,}-₹{hi:,} for {ipo.board or 'Mainboard'}; check "
                       f"lot_size ({iss.lot_size}) against the band")
    return out


def inspect(ipo: Ipo, today: date | None = None) -> dict[str, Any]:
    """Findings for one IPO, ordered worst-first."""
    from .compute import date_metrics, gmp_metrics

    status = date_metrics(ipo)["status"]
    missing = []
    for label, getter, who, severity, breaks in CHECKS:
        try:
            ok = bool(getter(ipo))
        except Exception:
            ok = False
        if ok:
            continue
        # An issue that has not opened has no subscription to report. That is
        # the calendar, not a hole — flagging it made every upcoming IPO look
        # defective and buried the gaps that are real.
        if label == "Subscription" and status == "upcoming":
            continue
        missing.append({"field": label, "who": who,
                        "severity": severity, "breaks": breaks})
    missing.sort(key=lambda m: (m["severity"] != "blank", m["field"]))
    gmp = gmp_metrics(ipo)
    return {
        "slug": ipo.slug,
        "company": ipo.company or ipo.slug,
        "status": status,
        "missing": missing,
        "blank": [m for m in missing if m["severity"] == "blank"],
        "gmp_gaps": gmp_gaps(ipo, today),
        "sub_gaps": sub_gaps(ipo),
        # Stale is worse than missing here: the card still shows the number,
        # labelled as today's.
        "gmp_age_days": gmp["age_days"],
        "gmp_stale": bool(gmp["is_stale"]) and status not in ("listed",),
        # Present-but-contradictory data. A blank blanks a scene, which shows;
        # a wrong figure renders perfectly and gets believed.
        "inconsistent": inconsistencies(ipo),
        "repairs": [r["what"] for r in plan_repairs(ipo)],
    }


# ── repair ─────────────────────────────────────────────────────────────────

def plan_repairs(ipo: Ipo) -> list[dict[str, Any]]:
    """Every repair that follows arithmetically, as {what, apply}.

    Returned as a plan rather than applied directly so `inspect` can list what
    `--fix` would do without doing it.
    """
    out: list[dict[str, Any]] = []
    iss, dts = ipo.issue, ipo.dates

    def add(what: str, fn: Callable[[dict], None]) -> None:
        out.append({"what": what, "apply": fn})

    split = iss.fresh_cr + iss.ofs_cr

    if split > 0 and abs(split - iss.total_cr) > 0.01:
        # total IS fresh + OFS — arithmetic, not a judgement, so it qualifies
        # as a repair even when a different total is already recorded. That
        # case is common rather than theoretical: NSE's catalogue `issueSize`
        # counts only the fresh shares, so the derived total understates any
        # issue with an OFS leg (Molbio published ₹658 Cr against ₹939.7 Cr).
        was = f" (was ₹{iss.total_cr:g})" if iss.total_cr else ""
        add(f"total_cr = fresh + OFS = ₹{split:g} Cr{was}",
            lambda raw, v=split: raw["issue"].__setitem__("total_cr", round(v, 2)))
    elif iss.total_cr > 0 and 0 < iss.fresh_cr and not iss.ofs_cr and iss.total_cr > iss.fresh_cr:
        rest = round(iss.total_cr - iss.fresh_cr, 2)
        add(f"ofs_cr = total - fresh = ₹{rest:g} Cr",
            lambda raw, v=rest: raw["issue"].__setitem__("ofs_cr", v))
    elif iss.total_cr > 0 and 0 < iss.ofs_cr and not iss.fresh_cr and iss.total_cr > iss.ofs_cr:
        rest = round(iss.total_cr - iss.ofs_cr, 2)
        add(f"fresh_cr = total - OFS = ₹{rest:g} Cr",
            lambda raw, v=rest: raw["issue"].__setitem__("fresh_cr", v))

    # T+3: allotment, refund, listing all hang off the close date.
    if dts.close:
        for field, offset in (("allotment", 1), ("refund", 2), ("listing", 3)):
            if not getattr(dts, field):
                when = _working_days_after(dts.close, offset).isoformat()
                add(f"dates.{field} = {when} (close + {offset} working day"
                    f"{'s' if offset > 1 else ''})",
                    lambda raw, f=field, w=when: raw["dates"].__setitem__(f, w))

    if not dts.close_time:
        add("dates.close_time = 17:00 (standard bidding cut-off)",
            lambda raw: raw["dates"].__setitem__("close_time", "17:00"))

    if iss.registrar and not iss.registrar_url:
        url = _match_registrar(iss.registrar)
        if url:
            add(f"issue.registrar_url = {url}",
                lambda raw, u=url: raw["issue"].__setitem__("registrar_url", u))

    return out


def repair(ipo: Ipo) -> tuple[Ipo, list[str]]:
    """Apply every planned repair. Returns the new IPO and what changed."""
    plan = plan_repairs(ipo)
    if not plan:
        return ipo, []
    raw = ipo.to_dict()
    raw.setdefault("issue", {})
    raw.setdefault("dates", {})
    for step in plan:
        step["apply"](raw)
    return Ipo.from_dict(raw), [step["what"] for step in plan]
