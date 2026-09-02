"""Is the data still arriving? The watchdog for the scheduled jobs.

Every other check in this project reads the sheet and asks whether what it
says is any good. This one asks the question none of them can: **did anything
change since last time, and should it have?**

That gap is real and it is silent. A Task Scheduler entry that stopped firing,
an InvestorGain slug that quietly stopped matching, an expired credential —
none of them produce an error anybody sees. The pipeline "succeeds", the sheet
keeps its last-good contents, the site keeps rendering them, and the first
symptom is a reel quoting a three-day-old premium as today's.

So this compares two things:

  * **against the calendar** — an issue taking bids today must have a
    subscription row dated today, and a GMP no older than yesterday. What
    *should* have arrived is derivable from the issue's own dates, so a
    missing row is a finding rather than something to eyeball.

  * **against last run** — a fingerprint of the store is written to
    `.cache/monitor.json` on every run, so the next one can say what moved.
    Nothing moving is not automatically wrong (Sunday, a market holiday, no
    live issues) but nothing moving *while an issue is open* always is.

Read-only against the store. It writes exactly one local file, its own
history, and never touches the sheet — a watchdog that repaired what it
watched could not report a fault.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any

from . import readiness, store
from .models import Ipo

STATE = store.BACKEND_ROOT / ".cache" / "monitor.json"

# Nothing is expected on a weekend: the exchanges do not publish subscription
# and the grey market desks do not refresh. A Saturday with no new rows is a
# quiet market, not a broken job, and reporting it every weekend is how a
# monitor gets ignored by Monday.
def _market_day(day: date) -> bool:
    return day.weekday() < 5


# The tightest gap between a writing job and the monitor slot that follows it
# (daily 10:00 → monitor 12:30). Two runs closer together than this cannot say
# anything about whether data arrived, because nothing was scheduled to write
# in between.
MIN_GAP_HOURS = 2.5


def _hours_since(stamp: str | None, now: datetime) -> float | None:
    if not stamp:
        return None
    try:
        return (now - datetime.fromisoformat(stamp)).total_seconds() / 3600
    except ValueError:
        return None


def fingerprint(ipos: list[Ipo]) -> dict[str, Any]:
    """A small, comparable summary of what the store currently holds."""
    return {
        "at": datetime.now().isoformat(timespec="seconds"),
        "ipos": len(ipos),
        "rows": {
            "gmp": sum(len(i.gmp_history) for i in ipos),
            "subscription": sum(len(i.subscription) for i in ipos),
            "financial_years": sum(len(i.financials.years) for i in ipos),
        },
        "latest": {
            i.slug: {
                "gmp": i.gmp_history[-1].date.isoformat() if i.gmp_history and i.gmp_history[-1].date else None,
                "sub": i.subscription[-1].date.isoformat() if i.subscription and i.subscription[-1].date else None,
                "sub_day": i.subscription[-1].day if i.subscription else None,
            }
            for i in ipos
        },
    }


def _previous() -> dict[str, Any]:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _remember(fp: dict[str, Any]) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(fp, indent=1), encoding="utf-8")
    except OSError:
        pass                              # a monitor that cannot write its own
                                          # history is degraded, not broken


def duplicates(ipos: list[Ipo]) -> list[dict[str, Any]]:
    """Two rows for one company.

    The signals themselves live in `dedupe`, not here, and that is the fix for
    the bug this check kept finding but could not stop. Discovery used to ask
    the name matcher whether a catalogue row was one of ours, get a no, and
    scaffold a second row — while this function, three modules away, held a
    perfectly good definition of "same company" that nothing at the door ever
    consulted. Both now read the one definition, so a pair this would report
    is a pair discovery already refused to create.

    Kept as a name here because the watchdog is where it is *reported*, and
    `check()` below is the only caller.
    """
    from .dedupe import groups
    return groups(ipos)


def check(now: datetime | None = None) -> dict[str, Any]:
    """The full health picture. Read-only apart from its own history file."""
    now = now or datetime.now()
    today = now.date()
    ipos = store.load_all()
    prev = _previous()

    # Belt and braces over the retry in sheets._fetch. An empty store is never
    # a real observation — the sheet always holds at least a header row — so
    # the only thing it can mean is that a writing job is mid-rewrite. Recording
    # a fingerprint of nothing would make the next run report the whole store as
    # newly arrived, which is worse than not checking at all.
    if not ipos and prev.get("ipos"):
        return {
            "at": now.isoformat(timespec="seconds"),
            "since": prev.get("at"), "ipos": 0, "open": 0, "moved": {},
            "skipped": "the store read back empty — a job is mid-write. "
                       "Nothing recorded; the next run will compare against "
                       "the last good fingerprint.",
            "findings": [], "errors": 0, "warnings": 0,
            "recordable": [], "fingerprint": prev.get("rows", {}),
        }

    fp = fingerprint(ipos)
    findings: list[dict[str, Any]] = []

    def flag(sev: str, slug: str, what: str, detail: str) -> None:
        findings.append({"severity": sev, "slug": slug, "what": what,
                         "detail": detail})

    # ── 1. did the store move at all since the last run?
    moved: dict[str, int] = {}
    if prev.get("rows"):
        for key, count in fp["rows"].items():
            moved[key] = count - prev["rows"].get(key, 0)

    live = [i for i in ipos if readiness.status(i, now) == "open"]
    # "Nothing changed" only means something once enough time has passed that a
    # writing job SHOULD have run in between. The scheduled slots are at most
    # 2.5 hours behind the job before them (daily 10:00 → monitor 12:30), so
    # anything closer than that is simply too soon to say — and firing on it
    # would make every back-to-back run a red alert, which is how a watchdog
    # gets ignored.
    gap_h = _hours_since(prev.get("at"), now)
    comparable = gap_h is not None and gap_h >= MIN_GAP_HOURS
    quiet = comparable and not any(v for v in moved.values())
    if quiet and live and _market_day(today):
        flag("error", "-", "Nothing changed",
             f"no new GMP, subscription or financial rows in {gap_h:.1f}h "
             f"(since {prev.get('at')}) — and {len(live)} issue(s) are taking "
             f"bids. The scheduled jobs are probably not running.")

    # ── 2. what should have arrived today, per IPO
    for ipo in ipos:
        fresh = readiness.freshness(ipo, now)
        state = readiness.status(ipo, now)

        g = fresh["gmp"]
        if g["matters"] and _market_day(today):
            if g["state"] == "missing":
                # Only worth reporting once the issue is close enough that a
                # desk would actually be quoting it.
                near = ipo.dates.open and (ipo.dates.open - today).days <= 5
                if near or state in ("open", "closed"):
                    flag("warn", ipo.slug, "No GMP at all",
                         "gmp-sync has never written a reading for this issue "
                         "— check it resolves on InvestorGain's board")
            elif g["state"] == "cold":
                flag("error", ipo.slug, "GMP is cold",
                     f"last read {g['last']} ({g['age_days']} days ago) while "
                     f"the issue is {state}")
            elif g["state"] == "stale":
                flag("warn", ipo.slug, "GMP is stale",
                     f"last read {g['last']} ({g['age_days']} days ago)")

        s = fresh["subscription"]
        if s["matters"]:
            # Bidding opened today and the exchanges have not published yet —
            # nothing is late until the evening.
            settled = now.hour >= readiness.SUB_SETTLED_HOUR
            if s["state"] == "missing" and settled:
                flag("error", ipo.slug, "No subscription",
                     "the issue is open and no bidding day has been recorded")
            elif s["state"] == "stale":
                flag("error", ipo.slug, "Subscription is stale",
                     f"last day recorded {s['last']}, but bidding is open today")
            elif s["state"] == "provisional" and settled:
                flag("warn", ipo.slug, "Subscription may be partial",
                     f"today's row was written before {readiness.SUB_SETTLED_HOUR}:00, "
                     f"so it is a running total rather than the day's close")



    # ── a single row that stopped while the others kept moving
    #
    # The signature of a slug that stopped matching upstream, and it is only
    # legible as a CONTRAST. On a quiet evening every open row is unchanged and
    # flagging all of them says nothing; when eight rows moved and one did not,
    # that one is the finding. So this runs only if something moved at all.
    if comparable and any(v > 0 for v in moved.values()) and _market_day(today):
        for ipo in ipos:
            if readiness.status(ipo, now) != "open":
                continue
            last = (prev.get("latest") or {}).get(ipo.slug) or {}
            if last and last == fp["latest"][ipo.slug]:
                flag("warn", ipo.slug, "Row is frozen",
                     f"other rows moved in the last {gap_h:.1f}h and this one "
                     f"did not — check it still resolves upstream")

    # ── 3. contradictions inside a record
    for ipo in ipos:
        for p in readiness.problems(ipo, now):
            if p["severity"] == "error":
                flag("error", ipo.slug, p["what"], p["detail"])

    # ── 4. the same company stored twice
    #
    # This should now be unreachable for anything discovery created — the
    # doors refuse a colliding row. It stays an error rather than a warning
    # because the pairs it can still catch are the ones that got in another
    # way (a hand-typed row, an `import` from an outside spreadsheet, or a
    # name that only became a collision once a later sync filled in the
    # calendar), and those are exactly the ones nobody is watching for.
    for dup in duplicates(ipos):
        flag("error", "-", "Duplicate record",
             f"{' and '.join(dup['slugs'])} share {dup['why']} "
             f"({dup['confidence']}) — both collect data and both appear in "
             f"the dropdown. Fold them with `ipopulse dedupe` (dry run) then "
             f"`ipopulse dedupe --write`; `ipopulse verify` says which one an "
             f"exchange can account for")

    # ── 5. reel readiness, rolled up
    from .compute import derive
    reports = [readiness.report(i, derive(i, now), now) for i in ipos]
    recordable = [r for r in reports if r["ready"]]

    _remember(fp)
    return {
        "at": now.isoformat(timespec="seconds"),
        "since": prev.get("at"),
        "ipos": len(ipos),
        "open": len(live),
        "moved": moved,
        "findings": findings,
        "errors": sum(1 for f in findings if f["severity"] == "error"),
        "warnings": sum(1 for f in findings if f["severity"] == "warn"),
        "recordable": [{"slug": r["slug"], "company": r["company"],
                        "reels": r["ready"]} for r in recordable],
        "fingerprint": fp["rows"],
    }


def report(r: dict[str, Any]) -> list[str]:
    """The health check as printable lines."""
    out = [f"IPO Pulse data health — {r['at']}"]
    if r.get("skipped"):
        out.append(f"  skipped: {r['skipped']}")
        return out
    out.append(f"  {r['ipos']} tracked, {r['open']} taking bids now")
    if r["since"]:
        moved = r["moved"] or {}
        bits = ", ".join(f"{k} {v:+d}" for k, v in moved.items()) or "nothing"
        out.append(f"  since {r['since']}: {bits}")
    else:
        out.append("  first run — no previous fingerprint to compare against")

    if not r["findings"]:
        out.append("\n  Everything that should have arrived today has arrived.")
    else:
        out.append("")
        for f in sorted(r["findings"],
                        key=lambda f: 0 if f["severity"] == "error" else 1):
            mark = "!!" if f["severity"] == "error" else " !"
            who = "" if f["slug"] == "-" else f"{f['slug']}: "
            out.append(f"  {mark} {who}{f['what']} — {f['detail']}")

    out.append("")
    if r["recordable"]:
        out.append(f"  Ready to record ({len(r['recordable'])}):")
        for row in r["recordable"]:
            reels = ", ".join(f"reel {n}" for n in row["reels"])
            out.append(f"     {row['company'][:38]:<40}{reels}")
    else:
        out.append("  Nothing is fully ready to record — run "
                   "`ipopulse validate` to see what each reel is waiting on.")
    return out
