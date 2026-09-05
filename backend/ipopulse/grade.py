"""Score the data against InvestorGain and say what is wrong with it.

`doctor` answers "what is missing from this record" and `verify` answers
"should this record exist". Neither answers the question that actually decides
whether a video can be recorded: **are the numbers right?**

That question only has an answer relative to something. InvestorGain is the
desk this channel quotes, so it is the yardstick: every stored GMP day and
subscription day is compared against theirs, and the result is a percentage
with the disagreements named. A grade nobody can act on is a vanity metric,
so every band below carries the specific rows that cost the marks.

Run weekly (see deploy/windows/Register-IpoPulseTasks.ps1). It is read-only —
it writes nothing and fixes nothing, deliberately: a grader that quietly
repaired what it measured would always report an A.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from . import store
from .compute import derive


def _band(pct: float) -> str:
    if pct >= 99.5:
        return "A"
    if pct >= 97:
        return "B"
    if pct >= 90:
        return "C"
    if pct >= 75:
        return "D"
    return "F"



# The terms the desk is authoritative for, and how close counts as agreeing.
#
# Added after 5 Sep 2026, when a phantom OFS of 93 crore sat on ten unrelated
# IPOs, three rows held a price band whose low was above its high, and six had
# a refund date before their own allotment — none of it visible, because this
# grader only ever compared GMP and subscription. The numbers that describe
# the OFFER were never checked against anything.
#
# Tolerances are per-field rather than one number: a rupee-crore total that
# differs by 0.01 is rounding, a lot size that differs by 1 share is not.
TERMS: list[tuple[str, str, float]] = [
    ("total_cr", "issue size", 0.5),
    ("fresh_cr", "fresh issue", 0.5),
    ("ofs_cr", "offer for sale", 0.5),
    ("price_low", "band low", 0.5),
    ("price_high", "band high", 0.5),
]


def _terms_check(ipo, row, ig) -> list[tuple[str, Any, Any]]:
    """Stored issue terms against the desk's. [] when they agree."""
    desk = {**ig.issue_size(row), **ig.price_band(row)}
    out = []
    for key, label, tol in TERMS:
        if key not in desk:
            continue                      # the desk does not publish it
        ours = float(getattr(ipo.issue, key, 0) or 0)
        theirs = float(desk[key])
        if abs(ours - theirs) > tol:
            out.append((label, ours, theirs))
    return out


def _impossible(ipo) -> list[str]:
    """Things that cannot be true of any IPO, whatever the desk says.

    Separate from the desk comparison on purpose: these need no yardstick.
    An issue the desk has stopped carrying still cannot have a band whose low
    is above its high, and those are exactly the rows a reconciliation would
    otherwise fall silent on.
    """
    out = []
    iss, d, f = ipo.issue, ipo.dates, ipo.financials
    if iss.price_low and iss.price_high and iss.price_low > iss.price_high:
        out.append(f"band low {iss.price_low:g} is above high {iss.price_high:g}")
    if d.refund and d.allotment and d.refund < d.allotment:
        out.append(f"refund {d.refund} falls before allotment {d.allotment}")
    if d.listing and d.allotment and d.listing < d.allotment:
        out.append(f"listing {d.listing} falls before allotment {d.allotment}")
    if d.open and d.close and d.close < d.open:
        out.append(f"close {d.close} falls before open {d.open}")
    if iss.shares_total:
        sl = (iss.shares_qib + iss.shares_nii + iss.shares_retail
              + iss.shares_employee + iss.shares_shareholders)
        if sl > iss.shares_total * 1.10:
            out.append(f"the book is {100 * sl / iss.shares_total:.0f}% reserved")
    if f.years and len(f.years) != len(set(f.years)):
        out.append(f"the year axis repeats itself: {', '.join(f.years)}")
    for key in ("revenue", "ebitda", "pat", "net_worth", "total_debt"):
        vals = getattr(f, key, None) or []
        if vals and f.years and len(vals) != len(f.years):
            out.append(f"{key} has {len(vals)} values for {len(f.years)} years")
    return out


def _twins(ipos) -> list[tuple[str, str, str]]:
    """Rows holding an identical set of issue terms. Almost never a coincidence.

    This is the check that would have caught every contamination of 4-5 Sep
    without anyone reading a list. Values bled into alphabetically adjacent
    rows — `ofs = 93.0` across ten IPOs, `fresh = 274.18` across three, and
    NSE carrying Qualiance's band and lot — and each time it was found by a
    human noticing the same number twice.

    A single shared figure is ordinary: two issues can be the same size, and
    the desk confirmed several that were. What is not ordinary is a **pair of
    rows agreeing on band low AND band high AND lot size**, or on fresh AND
    OFS AND total. Those are independent quantities; matching on all three is
    a copy, not a coincidence.

    Reported, never repaired — which of the two rows is the wrong one is not
    something arithmetic can decide.
    """
    groups = [
        ("price band and lot", ("price_low", "price_high", "lot_size")),
        ("issue size split", ("fresh_cr", "ofs_cr", "total_cr")),
        ("share reservation", ("shares_qib", "shares_nii", "shares_retail",
                               "shares_total")),
    ]
    out: list[tuple[str, str, str]] = []
    for label, fields in groups:
        seen: dict[tuple, str] = {}
        for ipo in sorted(ipos, key=lambda i: i.slug):
            key = tuple(round(float(getattr(ipo.issue, f, 0) or 0), 2)
                        for f in fields)
            if not any(key):
                continue                  # all blank: nothing asserted
            if key in seen:
                out.append((seen[key], ipo.slug, label))
            else:
                seen[key] = ipo.slug
    return out


def collect(days: int = 7) -> dict[str, Any]:
    """Compare every stored figure against InvestorGain. Read-only."""
    from .providers import investorgain as ig

    since = date.today() - timedelta(days=days)
    r: dict[str, Any] = {
        "gmp_total": 0, "gmp_match": 0, "gmp_bad": [],
        "sub_total": 0, "sub_match": 0, "sub_bad": [],
        "orphans": [], "unmatched": [], "stale": [], "ipos": 0,
        "window_days": days,
        # Issue terms, and the same rows grouped by where each issue is in
        # its own life — which is how the question actually gets asked:
        # "is anything wrong with what is open right now?"
        "terms_total": 0, "terms_match": 0, "terms_bad": [],
        "impossible": [], "by_status": {}, "twins": [],
    }
    r["twins"] = _twins(store.load_all())

    for ipo in store.load_all():
        r["ipos"] += 1
        status = derive(ipo)["dates"]["status"]
        r["by_status"].setdefault(status, {"n": 0, "clean": 0, "slugs": []})
        r["by_status"][status]["n"] += 1

        # Checked before the desk lookup, because an issue the desk no longer
        # carries still must not hold a band that inverts.
        wrong = _impossible(ipo)
        if wrong:
            r["impossible"].append((ipo.slug, status, wrong))

        m = ig.resolve(ipo.slug, ipo.company or "")
        if not m:
            # Not automatically wrong — a listed issue can age out — but it
            # means nothing here can be checked, so it is reported.
            r["unmatched"].append(ipo.slug)
            if not wrong:
                r["by_status"][status]["clean"] += 1
            else:
                r["by_status"][status]["slugs"].append(ipo.slug)
            continue

        for label, ours, theirs in _terms_check(ipo, m, ig):
            r["terms_total"] += 1
            r["terms_bad"].append((ipo.slug, status, label, ours, theirs))
        r["terms_total"] += 0

        theirs = {p["date"]: p["gmp"] for p in ig.history(m)}
        for p in ipo.gmp_history:
            if not p.date:
                continue
            d = p.date.isoformat()
            if d not in theirs:
                # A day we hold and the desk does not. Usually a model-written
                # figure that should never have been stored.
                r["orphans"].append((ipo.slug, d, p.gmp, p.source or "?"))
                continue
            r["gmp_total"] += 1
            if abs(theirs[d] - p.gmp) < 0.01:
                r["gmp_match"] += 1
            else:
                r["gmp_bad"].append((ipo.slug, d, p.gmp, theirs[d]))

        their_sub = {x["day"]: x["total"] for x in ig.subscription(m)}
        for s in ipo.subscription:
            if s.day not in their_sub:
                continue
            r["sub_total"] += 1
            if abs(their_sub[s.day] - (s.total or 0)) < 0.005:
                r["sub_match"] += 1
            else:
                r["sub_bad"].append((ipo.slug, s.day, s.total, their_sub[s.day]))

        if not wrong and not any(b[0] == ipo.slug for b in r["terms_bad"]):
            r["by_status"][status]["clean"] += 1
        else:
            r["by_status"][status]["slugs"].append(ipo.slug)

        # A live issue whose premium has not moved in days is either genuinely
        # flat or not being read at all, and the card cannot tell you which.
        if status in ("open", "upcoming") and ipo.gmp_history:
            newest = max((p.date for p in ipo.gmp_history if p.date), default=None)
            if newest and newest < since:
                r["stale"].append((ipo.slug, newest.isoformat()))

    r["gmp_pct"] = 100.0 * r["gmp_match"] / r["gmp_total"] if r["gmp_total"] else 100.0
    r["sub_pct"] = 100.0 * r["sub_match"] / r["sub_total"] if r["sub_total"] else 100.0
    checked = r["gmp_total"] + r["sub_total"]
    matched = r["gmp_match"] + r["sub_match"]
    r["overall_pct"] = 100.0 * matched / checked if checked else 100.0
    # Orphans are not in `checked` — they cannot be compared — so they would
    # otherwise cost nothing. They are the worst failure mode there is
    # (a number nobody published), so they take the grade down directly.
    r["grade"] = _band(r["overall_pct"])
    if r["orphans"] and r["grade"] == "A":
        r["grade"] = "B"
    # An issue term that disagrees with the desk, or a record that
    # contradicts itself, is not allowed to leave an A on the board. The
    # whole failure of 5 Sep was a grade that stayed green while ten IPOs
    # carried a phantom OFS — because nothing here looked at the terms.
    if r["terms_bad"] and r["grade"] in ("A", "B"):
        r["grade"] = "C"
    if r["twins"] and r["grade"] in ("A", "B"):
        r["grade"] = "C"
    if r["impossible"]:
        r["grade"] = "F" if len(r["impossible"]) > 3 else "D"
    return r


def report(r: dict[str, Any]) -> list[str]:
    """The grade as printable lines."""
    out = [
        f"IPO PULSE — data grade: {r['grade']}   ({r['overall_pct']:.1f}% agreement "
        f"with InvestorGain across {r['ipos']} IPOs)",
        "",
        f"  GMP          {r['gmp_match']}/{r['gmp_total']} days match  ({r['gmp_pct']:.1f}%)",
        f"  Subscription {r['sub_match']}/{r['sub_total']} days match  ({r['sub_pct']:.1f}%)",
    ]

    # By status first, because that is how the question gets asked: not
    # "how is the data" but "is anything wrong with what is open right now".
    order = ["open", "upcoming", "closed", "allotment", "listed"]
    rows = [(k, v) for k, v in sorted(
        r["by_status"].items(),
        key=lambda kv: order.index(kv[0]) if kv[0] in order else 9)]
    if rows:
        out += ["", "  BY STATUS"]
        for name, v in rows:
            mark = "ok" if v["clean"] == v["n"] else f"{v['n'] - v['clean']} to check"
            out.append(f"    {name:<12}{v['clean']}/{v['n']} clean   {mark}")
            if v["slugs"]:
                out.append(f"    {'':12}{', '.join(sorted(set(v['slugs']))[:6])}")

    if r["impossible"]:
        out += ["", f"  {len(r['impossible'])} IPO(s) hold something that cannot "
                    f"be true of any issue:"]
        for slug, status, why in r["impossible"][:10]:
            out.append(f"    {slug:<32}[{status}]")
            out += [f"      - {w}" for w in why]
    if r["twins"]:
        out += ["", f"  {len(r['twins'])} pair(s) of IPOs hold IDENTICAL terms "
                    f"— independent numbers do not match by accident:"]
        out += [f"    {a} and {b} share the same {what}"
                for a, b, what in r["twins"][:10]]
    if r["terms_bad"]:
        out += ["", f"  {len(r['terms_bad'])} issue term(s) disagree with the desk "
                    f"— ours vs theirs:"]
        out += [f"    {s:<30}[{st[:9]:<9}] {lab:<15}{a:g} vs {b:g}"
                for s, st, lab, a, b in r["terms_bad"][:14]]
    if r["gmp_bad"]:
        out += ["", f"  {len(r['gmp_bad'])} GMP day(s) disagree — ours vs theirs:"]
        out += [f"    {s:<32}{d}  {a:g} vs {b:g}" for s, d, a, b in r["gmp_bad"][:12]]
    if r["sub_bad"]:
        out += ["", f"  {len(r['sub_bad'])} subscription day(s) disagree:"]
        out += [f"    {s:<32}day {d}  {a:g} vs {b:g}" for s, d, a, b in r["sub_bad"][:12]]
    if r["orphans"]:
        out += ["", f"  {len(r['orphans'])} stored day(s) InvestorGain never published "
                    f"— these are invented numbers:"]
        out += [f"    {s:<32}{d}  {v:g}  [{src}]" for s, d, v, src in r["orphans"][:12]]
    if r["stale"]:
        out += ["", f"  {len(r['stale'])} live IPO(s) with no GMP in "
                    f"{r['window_days']} days:"]
        out += [f"    {s:<32}last {d}" for s, d in r["stale"]]
    if r["unmatched"]:
        out += ["", f"  {len(r['unmatched'])} IPO(s) InvestorGain could not be asked about: "
                    + ", ".join(r["unmatched"][:8])]
    if not (r["gmp_bad"] or r["sub_bad"] or r["orphans"] or r["stale"]
            or r["terms_bad"] or r["impossible"] or r["twins"]):
        out += ["", "  Nothing to fix. Every stored figure matches the desk, "
                    "and nothing contradicts itself."]
    else:
        out += ["", "  Repair what the desk can settle:  ipopulse facts",
                "  Nothing here is fixed automatically — a grader that "
                "repaired what it measured would always report an A."]
    return out
