"""What cannot be true of an IPO, checked at the moment of writing.

── Why this exists, and why it is not in doctor.py or grade.py ────────────

Those two already check plenty, and they found nothing wrong with a Rentomojo
lot size of 15 against the desk's 37 — a ₹6,060 minimum investment on a
mainboard issue whose real minimum was ₹14,948. Prasol carried lot 160 against
22 the same way: ₹1,08,160 against ₹14,872. Both had been wrong since the
terms were first written, and both survived every pass:

  * `grade._terms_check` reconciles against the desk, but its TERMS list is
    issue size and price band. The lot lives in a different desk endpoint
    (`categories`, not `issue_size`/`price_band`), so it was never compared.
  * `grade._impossible` had no rule about lots or minimums at all.
  * `doctor` and `validate` report *after* the fact — and they report to a
    person, on a command somebody has to remember to run. Neither one is in
    the path of a write.

That last point is the whole design here. A check you have to run is a check
that does not run on the day it mattered. These are enforced by
`sheets.write_records`, which every IPO write funnels through, so a value that
cannot be true never reaches the sheet in the first place.

── Two severities, and why blocking is the right default ──────────────────

`BLOCK`  the value cannot be true, and writing it corrupts something
         downstream that has no way to notice. The write is refused whole.
`WARN`   the value is suspicious but has legitimate exceptions. Printed,
         written anyway.

Refusing the *whole* write rather than dropping the bad record is deliberate.
`write_records` clears and rewrites every tab from one snapshot; dropping a
record from that snapshot deletes it from the sheet, which turns a bad field
into a lost row. Failing loudly and changing nothing is the only safe verb.

`force=True` bypasses BLOCK, and means the same thing it already means on
`write_records`: I have looked at what is about to go, and I am certain.

── The minimum-investment rule is the load-bearing one ────────────────────

SEBI holds a mainboard retail minimum to roughly ₹14,000-16,000, which is what
makes `lot x cap price` checkable at all: it is the one issue term with an
externally fixed answer. The window enforced below is wider (₹10,000-₹20,000)
because the rule has been revised before and a check that fires on a
regulatory change is worse than no check. Both real bugs sat far outside even
that: ₹6,060 and ₹1,08,160.
"""

from __future__ import annotations

from datetime import date
from typing import Any, NamedTuple

BLOCK = "block"
WARN = "warn"

# Mainboard retail minimum. SEBI's own band is 14,000-16,000; this is
# deliberately slack — see the module docstring.
MAINBOARD_MIN_LOW = 10_000
MAINBOARD_MIN_HIGH = 20_000

# SME minimums are a different regime and have moved more than once, so this
# is a WARN band, not a BLOCK one.
SME_MIN_LOW = 50_000
SME_MIN_HIGH = 5_00_000


class Violation(NamedTuple):
    slug: str
    severity: str
    field: str
    message: str

    def __str__(self) -> str:
        mark = "!!" if self.severity == BLOCK else "??"
        return f"  {mark} {self.slug}: {self.message}"


def _f(d: dict, *path: str) -> float:
    """A float from a nested record, 0.0 for anything absent or unparseable."""
    cur: Any = d
    for key in path:
        if not isinstance(cur, dict):
            return 0.0
        cur = cur.get(key)
    try:
        return float(cur or 0)
    except (TypeError, ValueError):
        return 0.0


def _d(d: dict, *path: str) -> date | None:
    cur: Any = d
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    if isinstance(cur, date):
        return cur
    if isinstance(cur, str) and cur.strip():
        try:
            return date.fromisoformat(cur.strip()[:10])
        except ValueError:
            return None
    return None


def check(slug: str, rec: dict) -> list[Violation]:
    """Every violation in one record. [] when it is fine."""
    out: list[Violation] = []

    def bad(sev: str, field: str, msg: str) -> None:
        out.append(Violation(slug, sev, field, msg))

    board = str(rec.get("board") or "").strip().lower()
    is_sme = "sme" in board

    low = _f(rec, "issue", "price_low")
    high = _f(rec, "issue", "price_high")
    lot = _f(rec, "issue", "lot_size")

    # ── the price band ────────────────────────────────────────────────────
    if low and high and low > high:
        bad(BLOCK, "price_low", f"band low {low:g} is above high {high:g}")
    if low < 0 or high < 0:
        bad(BLOCK, "price_high", f"negative price band {low:g}-{high:g}")

    # ── the lot, and the minimum it implies ───────────────────────────────
    #
    # A blank lot is fine and common: the desk publishes it when the terms are
    # filed, which can be weeks after the row exists. A lot that is present
    # and wrong is the dangerous state, because every per-lot figure in reels
    # 1, 2 and 4 multiplies by it and none of them can tell.
    if lot < 0 or (lot and lot != int(lot)):
        bad(BLOCK, "lot_size", f"lot size {lot:g} is not a positive whole number")
    elif lot and high:
        minimum = lot * high
        if not is_sme and not (MAINBOARD_MIN_LOW <= minimum <= MAINBOARD_MIN_HIGH):
            bad(BLOCK, "lot_size",
                f"lot {int(lot)} x cap {high:g} = Rs {minimum:,.0f}, outside the "
                f"Rs {MAINBOARD_MIN_LOW:,}-{MAINBOARD_MIN_HIGH:,} a mainboard "
                f"retail minimum must fall in. The lot or the band is wrong.")
        elif is_sme and not (SME_MIN_LOW <= minimum <= SME_MIN_HIGH):
            bad(WARN, "lot_size",
                f"lot {int(lot)} x cap {high:g} = Rs {minimum:,.0f}, unusual "
                f"for an SME issue")

    # ── the HNI tranches, which are multiples of the lot by definition ────
    shni = _f(rec, "issue", "min_shni_qty")
    bhni = _f(rec, "issue", "min_bhni_qty")
    if lot and shni:
        if shni <= lot:
            bad(WARN, "min_shni_qty",
                f"S-HNI minimum {shni:g} is not above the retail lot {lot:g}")
        elif shni % lot:
            bad(WARN, "min_shni_qty",
                f"S-HNI minimum {shni:g} is not a multiple of the lot {lot:g}")
    # Mainboard only. An SME issue has ONE non-institutional category, not the
    # S-HNI / B-HNI split a mainboard book has, and the desk reports the same
    # quantity for both — so equality here is the correct value, not a defect.
    # Checked against the sheet: all seven equal-HNI rows are SME.
    if not is_sme and shni and bhni and bhni <= shni:
        bad(WARN, "min_bhni_qty",
            f"B-HNI minimum {bhni:g} is not above the S-HNI minimum {shni:g}")

    # ── the issue size, three ways of saying the same number ──────────────
    fresh = _f(rec, "issue", "fresh_cr")
    ofs = _f(rec, "issue", "ofs_cr")
    total = _f(rec, "issue", "total_cr")
    if total and (fresh or ofs) and abs((fresh + ofs) - total) > max(1.0, total * 0.02):
        bad(WARN, "total_cr",
            f"fresh {fresh:g} + OFS {ofs:g} = {fresh + ofs:g}, but total is {total:g}")

    # Shares x cap price should reproduce the issue size. This is what would
    # have caught Rentomojo's share count sitting at 17.8M against a
    # ₹1,255 Cr book that needs 31.1M at the cap.
    shares = _f(rec, "issue", "shares_total")
    if shares and high and total:
        implied = shares * high / 1e7          # rupees -> crore
        if implied > total * 1.35 or implied < total * 0.45:
            bad(WARN, "shares_total",
                f"{shares:,.0f} shares x cap {high:g} = Rs {implied:,.0f} Cr, "
                f"against a stated issue size of Rs {total:g} Cr")

    # ── the book cannot be more than fully reserved ───────────────────────
    if shares:
        parts = sum(_f(rec, "issue", k) for k in
                    ("shares_qib", "shares_nii", "shares_retail",
                     "shares_employee", "shares_shareholders"))
        if parts > shares * 1.10:
            bad(BLOCK, "shares_total",
                f"the book is {100 * parts / shares:.0f}% reserved")
    anchor = _f(rec, "issue", "shares_anchor")
    if shares and anchor and anchor > shares:
        bad(BLOCK, "shares_anchor",
            f"anchor {anchor:,.0f} exceeds the whole issue {shares:,.0f}")

    # ── the timetable, in the only order it can happen in ─────────────────
    opened, closed = _d(rec, "dates", "open"), _d(rec, "dates", "close")
    allot = _d(rec, "dates", "allotment")
    refund, listing = _d(rec, "dates", "refund"), _d(rec, "dates", "listing")
    for earlier, later, a, b in (
            (opened, closed, "open", "close"),
            (closed, allot, "close", "allotment"),
            (allot, refund, "allotment", "refund"),
            (allot, listing, "allotment", "listing")):
        if earlier and later and later < earlier:
            bad(BLOCK, b, f"{b} {later} falls before {a} {earlier}")

    # ── the statement ─────────────────────────────────────────────────────
    fin = rec.get("financials") or {}
    years = list(fin.get("years") or [])
    if years and len(years) != len(set(years)):
        bad(BLOCK, "years", f"the year axis repeats itself: {', '.join(map(str, years))}")
    for key in ("revenue", "ebitda", "pat", "net_worth", "total_debt"):
        vals = list(fin.get(key) or [])
        if vals and years and len(vals) != len(years):
            bad(BLOCK, key,
                f"{key} has {len(vals)} values for {len(years)} year(s)")

    # PAT above EBITDA is possible (other income, a one-off gain) but is far
    # more often a mis-parse, so it warns rather than blocks. `validate`
    # already surfaces it; saying it at write time is what makes it fixable
    # before a reel quotes it.
    pats, ebs = list(fin.get("pat") or []), list(fin.get("ebitda") or [])
    for idx, (p, e) in enumerate(zip(pats, ebs)):
        try:
            p, e = float(p or 0), float(e or 0)
        except (TypeError, ValueError):
            continue
        if e and p > e:
            year = years[idx] if idx < len(years) else f"#{idx}"
            bad(WARN, "pat", f"{year}: PAT {p:g} exceeds EBITDA {e:g}")

    return out


# Allocation fields. Each is a share count derived from ONE issue's own size
# and price, so the same value on two companies is not a coincidence — it is
# one record written from another's numbers.
_ALLOCATION = ("shares_anchor", "shares_qib", "shares_nii", "shares_retail",
               "shares_total")

# Below this a collision is plausible arithmetic rather than contamination:
# small SME issues with round lot sizes genuinely land on the same figure.
_COLLISION_FLOOR = 100_000


def cross_record(records: dict[str, dict]) -> list[Violation]:
    """Allocations that appear on more than one company.

    Found 11 Sep 2026 via the Antigravity agent, which traced
    manipal-payment's wrong share count to the concurrent Purple Style Labs
    issue; this check then found a second pair nobody had looked at
    (rentomojo / symbiotec-pharmalab).

    Invisible to every per-record rule, because a contaminated breakdown SUMS
    correctly to its own wrong total. Only a comparison across records — or
    against the issue size, which is what `shares_total` already flagged
    without being able to explain — can see it.
    """
    from collections import defaultdict

    seen: dict[tuple[str, float], list[str]] = defaultdict(list)
    for slug, rec in records.items():
        if not isinstance(rec, dict):
            continue
        issue = rec.get("issue") or {}
        for field in _ALLOCATION:
            try:
                val = float(issue.get(field) or 0)
            except (TypeError, ValueError):
                continue
            if val >= _COLLISION_FLOOR:
                seen[(field, val)].append(slug)

    out: list[Violation] = []
    for (field, val), slugs in sorted(seen.items()):
        if len(slugs) < 2:
            continue
        for slug in slugs:
            others = ", ".join(s for s in slugs if s != slug)
            out.append(Violation(
                slug=slug, field=field, severity=WARN,
                message=(f"{val:,.0f} is also {others}'s {field} — an "
                         f"allocation is derived from one issue's own size "
                         f"and price, so one of these records was written "
                         f"from the other's numbers")))
    return out


def check_all(records: dict[str, dict]) -> list[Violation]:
    """Every violation across a whole snapshot, worst first."""
    out: list[Violation] = []
    for slug, rec in records.items():
        if isinstance(rec, dict):
            out.extend(check(slug, rec))
    # Cross-record rules run after the per-record ones and cannot BLOCK: the
    # collision proves one of the pair is wrong without saying which, and
    # refusing the write would freeze the correct record too.
    out.extend(cross_record(records))
    return sorted(out, key=lambda v: (v.severity != BLOCK, v.slug))


def blocking(violations: list[Violation]) -> list[Violation]:
    return [v for v in violations if v.severity == BLOCK]
