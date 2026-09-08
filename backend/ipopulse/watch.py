"""The regular sweep: run the insertion-time checks again, on a timer.

── The gap this closes ────────────────────────────────────────────────────

`invariants.py` refuses a write that cannot be true, and it works — but it
only ever sees a record at the moment something writes it. That leaves two
holes, and both have already happened:

1. **Rows written before the rule existed.** The Rentomojo lot of 15 and the
   Prasol lot of 160 were wrong from the day the terms were first stored. A
   write-time check cannot go back and look at them, so they sat there.
2. **Rows that go wrong without being written.** Nothing writes to a record to
   make its GMP three days old, or to spend the day's API quota, or to let a
   subscription day stop arriving. The record does not change; the world
   around it does. No write, no check.

So this runs the same rules against the *stored* snapshot on a schedule,
alongside the checks that only make sense with a clock — staleness, spend
against the free-tier caps, and which model is actually answering.

── Composition, not a fourth opinion ──────────────────────────────────────

Everything here already exists somewhere. `monitor` knows about staleness,
`invariants` knows what cannot be true, `grade` knows what contradicts
itself, `usage` knows what we have spent. This module calls them and merges
their answers into one list with one severity vocabulary. It deliberately
implements no rule of its own: a fifth place that decides what "suspicious"
means is how two of them end up disagreeing.

One finding shape throughout, the one `monitor` already emits:

    {"severity": "error" | "warn", "source": str, "slug": str,
     "what": str, "detail": str}

── Every sub-check is wrapped ─────────────────────────────────────────────

A watchdog whose own crash is indistinguishable from silence is not a
watchdog. `grade` reaches the network, `sheets.records()` reaches the sheet,
and either can fail for reasons that have nothing to do with the data. When
one does, that becomes a finding — named as a check that could not run,
never as a clean bill of health.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

ERROR = "error"
WARN = "warn"


def _finding(sev: str, source: str, slug: str, what: str,
             detail: str) -> dict[str, str]:
    return {"severity": sev, "source": source, "slug": slug,
            "what": what, "detail": detail}


def _broken(source: str, exc: Exception) -> dict[str, str]:
    """A check that could not run. An error, not an absence of findings."""
    return _finding(ERROR, source, "-", f"The {source} check failed to run",
                    f"{type(exc).__name__}: {str(exc)[:200]}")


# ── the sub-checks ─────────────────────────────────────────────────────────

def _non_trading_reason() -> str:
    """Why the exchange is shut today, or "" when it is a normal session.

    Asks `providers.market`, which is the same answer `ipopulse market` uses
    to refuse a briefing for a day with no session — so the sweep and the
    briefing can never disagree about whether today counts.
    """
    try:
        from datetime import date

        from .providers import market as mkt

        session = mkt.trading_day(date.today())
        return "" if session.get("trading") else str(session.get("why") or "market shut")
    except Exception:                     # noqa: BLE001
        return ""                         # unknown: judge it as a normal day


def _from_monitor() -> list[dict[str, str]]:
    """Staleness and arrival. `monitor` already speaks this shape."""
    from . import monitor

    r = monitor.check()
    out = []
    # Staleness is judged against the issue calendar, which does not know
    # about weekends: an issue whose window spans Saturday and Sunday is
    # "open" on both, so its last subscription day is two days old by Sunday
    # evening and monitor calls that an error. It is right about the fact and
    # wrong about the alarm — no bidding day was missed, because there was no
    # bidding day. Left as an error it would tag us every single weekend,
    # which is how a watchdog gets muted.
    #
    # Downgraded rather than dropped, and monitor's own verdict is untouched:
    # this is the sweep deciding what is worth waking somebody for, not a new
    # opinion about the data.
    closed = _non_trading_reason()
    for f in r.get("findings", []):
        sev = f.get("severity", WARN)
        detail = f.get("detail", "")
        if closed and sev == ERROR and "stale" in f.get("what", "").lower():
            sev = WARN
            detail = f"{detail} (not paging: {closed})"
        out.append(_finding(sev, "monitor", f.get("slug", "-"),
                            f.get("what", ""), detail))
    # `skipped` means the store read back empty mid-write. Not a data fault,
    # but the sweep must not report "all clear" off a snapshot it never saw.
    if r.get("skipped"):
        out.append(_finding(WARN, "monitor", "-", "Sweep ran mid-write",
                            str(r["skipped"])))
    return out


def _from_invariants() -> list[dict[str, str]]:
    """The write-time rules, applied to what is already stored."""
    from . import invariants, sheets

    records = sheets.records()
    out = []
    for v in invariants.check_all(records):
        # BLOCK means the write path would refuse this record today. Finding
        # one in the stored snapshot means it predates the rule (or went in
        # under force=True), and it is corrupting a reel right now.
        sev = ERROR if v.severity == invariants.BLOCK else WARN
        out.append(_finding(sev, "invariants", v.slug,
                            f"{v.field} cannot be true", v.message))
    return out


def _text_of(obj: Any, name: str) -> str:
    val = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, "")
    return str(val or "").strip()


def _after_open(stamp: str, day: str) -> str:
    """"HH:MM" when `stamp` is at or after the open on `day`, else "".

    Same stamp formats `review.lookahead` reads, and the same 09:15 boundary
    `readiness` expires reel 7 on — deliberately duplicated as a read rather
    than shared, because this asks a different question of the same field:
    lookahead asks "can it be scored", this asks "could it be recorded".
    """
    from datetime import datetime as _dt

    for fmt in ("%d-%b-%Y %H:%M", "%d-%b-%Y %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            at = _dt.strptime(stamp, fmt)
        except ValueError:
            continue
        if at.date().isoformat() != day:
            return ""
        return f"{at:%H:%M}" if (at.hour, at.minute) >= (9, 15) else ""
    return ""


def _from_briefing() -> list[dict[str, str]]:
    """Reel 7: did it run, were its inputs settled, and was it scored?

    Runs against today and the previous session, which is the window where
    something can still be done about what it finds. Older days are history
    and `ipopulse review --record` already reports them.
    """
    from datetime import date

    from . import briefing, review
    from .providers import market as mkt

    out: list[dict[str, str]] = []
    closed = _non_trading_reason()
    today = date.today().isoformat()

    # ── 1. did it run at all? ────────────────────────────────────────────
    if closed:
        out.append(_finding(WARN, "briefing", today,
                            "No briefing expected today", closed))
    elif not briefing.exists(today):
        # After the open this is a missed reel, not a pending one. Before it,
        # the 08:00 slot may simply not have come round yet.
        hour = datetime.now().hour
        sev = ERROR if hour >= 10 else WARN
        out.append(_finding(
            sev, "briefing", today, "No briefing stored for today",
            f"`ipopulse job market` should have written one at 08:00; it is "
            f"{hour:02d}:xx. Two schedulers can produce it and both may have "
            f"missed: the local task 'IPO Pulse - market' only fires while "
            f"the machine is awake, and the GitHub Actions slot was measured "
            f"at a median 388 minutes late, so it often has not arrived yet. "
            f"Reel 7 expires at 09:15 either way."))

    # ── 2. were the inputs dated to a COMPLETED session? ─────────────────
    #
    # The check that cannot be done by eye. `review.lookahead` reads the
    # provenance the row records and says whether anything in it came from
    # the session it was calling.
    for day in (today, review.previous_session(today)):
        if not day or not briefing.exists(day):
            continue
        try:
            brief = briefing.load(day)
        except Exception as exc:                              # noqa: BLE001
            out.append(_broken("briefing", exc))
            continue
        # Late but honest. Now that the inputs are settled, a briefing
        # written after the open is still scoreable — it just miscarried as a
        # reel, because `readiness` expires reel 7 at 09:15. Worth a WARN and
        # not an error: the record keeps it, only the video was lost.
        stamp = _text_of(brief, "at")
        if day == today and stamp and not review.lookahead(brief, day):
            late = _after_open(stamp, day)
            if late:
                out.append(_finding(
                    WARN, "briefing", day,
                    "Briefing arrived too late to record",
                    f"written at {late}, after the 09:15 expiry. Its numbers "
                    f"are settled so it still counts towards the track "
                    f"record, but reel 7 could not be cut from it. The local "
                    f"08:00 task is the only scheduler that hits that window."))

        why = review.lookahead(brief, day)
        if why:
            out.append(_finding(
                ERROR, "briefing", day,
                "Briefing cannot be scored — it may have seen the session "
                "it called",
                f"{why}. Nothing looks wrong in the row: the prices are real "
                f"and the arithmetic is right. It is excluded from the track "
                f"record, so reel 8 will not quote it."))

    # ── 3. once the archive is out, has the day been scored? ─────────────
    prev = review.previous_session(today)
    if prev and briefing.exists(prev):
        try:
            scored = review.review(prev)
        except Exception as exc:                              # noqa: BLE001
            out.append(_broken("briefing", exc))
            scored = {}
        if scored.get("skipped") and not scored.get("contaminated"):
            out.append(_finding(WARN, "briefing", prev,
                                "Last session not scored yet",
                                str(scored["skipped"])))
        elif scored.get("setups"):
            # 4. the geometry. Reported as information rather than a fault:
            # it is a fact about the setup structure, and the decision it
            # informs is an editorial one. Only stated once there is enough
            # to mean anything — a single day of it is noise.
            rec = review.record()
            n = rec.get("setups", 0)
            if n >= 10 and rec.get("median_stop_share") is not None:
                if rec["median_stop_share"] <= 0.4:
                    out.append(_finding(
                        WARN, "briefing", "-",
                        "Setup stops are inside the day's noise band",
                        f"Median stop sits {rec['median_stop_share']:.0%} of "
                        f"the realised range from entry against a "
                        f"{rec['median_target_share']:.0%} target, over "
                        f"{n} setups. The nearer level is reached first, so "
                        f"the structure loses even when the call is right."))
            if n >= 10 and rec.get("voided"):
                share = rec["voided"] / n
                if share >= 0.4:
                    out.append(_finding(
                        WARN, "briefing", "-",
                        "Most setups are voided by the opening print",
                        f"{rec['voided']} of {n} opened past their own stop "
                        f"or target, so no entry was available. The levels "
                        f"are being placed where price has already been."))
    return out


def _from_grade() -> list[dict[str, str]]:
    """Only the self-contradictions, not the disagreements.

    `grade` also reports where our numbers differ from InvestorGain's, and
    that is normal drift — a GMP that moved since the last read. Reporting it
    here would cry wolf every single day, which is how a watchdog gets muted.
    Records that contradict *themselves* are a different thing: nothing
    downstream can render them correctly.
    """
    from . import grade

    r = grade.collect(days=1)
    out = []
    # grade files these as (slug, status, [reasons]) tuples.
    for row in r.get("impossible", []):
        try:
            slug, status, reasons = row
        except (TypeError, ValueError):
            slug, status, reasons = str(row), "", []
        out.append(_finding(ERROR, "grade", str(slug),
                            "Record contradicts itself",
                            f"[{status}] " + "; ".join(str(x) for x in reasons)))
    return out


def _from_usage() -> list[dict[str, str]]:
    from . import usage

    return [_finding(f["severity"], "usage", f["slug"], f["what"], f["detail"])
            for f in usage.findings()]


def _from_models() -> list[dict[str, str]]:
    """Which model is actually answering, and whether that is the one we want.

    Worth a finding of its own because the failure is silent: `ai._generate`
    demotes to the next candidate on a 429, and gemma has a sixteenth of the
    token budget and no tool support. The prose gets worse, three modules
    away from anything that logs.
    """
    from . import ai, usage

    out = []
    model = ai.default_model()
    fam = usage.family(model)
    if fam == "gemma":
        out.append(_finding(
            WARN, "models", model, "Running on a bulk fallback model",
            "gemma is the last-resort tier: 16K TPM against flash-lite's 250K, "
            "and no tool support. Either flash-lite is rate-limited or "
            "GEMINI_MODEL pins this. Unset it to let discovery re-rank."))
    if fam == "unknown":
        out.append(_finding(
            WARN, "models", model, "Model is not in the known-limits table",
            "usage.FAMILIES has no entry matching this id, so it is being "
            "throttled at the meanest tier (5 RPM). Add it there."))
    return out


CHECKS = (
    ("usage", _from_usage),
    ("models", _from_models),
    ("invariants", _from_invariants),
    ("monitor", _from_monitor),
    ("briefing", _from_briefing),
    ("grade", _from_grade),
)


# ── the sweep ──────────────────────────────────────────────────────────────

def sweep(skip: tuple[str, ...] = ()) -> dict[str, Any]:
    """Every check, merged. Read-only apart from monitor's own history file."""
    from . import usage

    findings: list[dict[str, str]] = []
    ran: list[str] = []
    for name, fn in CHECKS:
        if name in skip:
            continue
        try:
            findings.extend(fn())
            ran.append(name)
        except Exception as exc:        # noqa: BLE001 — see the module docstring
            findings.append(_broken(name, exc))

    errors = [f for f in findings if f["severity"] == ERROR]
    warns = [f for f in findings if f["severity"] == WARN]
    # Errors first, then grouped by the check that raised them, so the report
    # reads as "what is broken" rather than in the order the checks happen to
    # be listed above.
    findings.sort(key=lambda f: (f["severity"] != ERROR, f["source"], f["slug"]))
    return {
        "at": datetime.now().isoformat(timespec="seconds"),
        "ok": not errors,
        "checks_run": ran,
        "counts": {"error": len(errors), "warn": len(warns)},
        "findings": findings,
        "usage": usage.snapshot(),
    }


def headline(r: dict[str, Any]) -> str:
    """One line, for an issue title or a notification."""
    e, w = r["counts"]["error"], r["counts"]["warn"]
    if not e and not w:
        return f"All clear — {len(r['checks_run'])} checks, nothing to report"
    parts = []
    if e:
        parts.append(f"{e} error{'s' if e != 1 else ''}")
    if w:
        parts.append(f"{w} warning{'s' if w != 1 else ''}")
    return " and ".join(parts)


def report(r: dict[str, Any]) -> list[str]:
    """The human-readable sweep, as lines."""
    out = [f"── Watchdog {r['at']}  ({headline(r)})"]
    if r["counts"]["error"] or r["counts"]["warn"]:
        out.append("")
        source = None
        for f in r["findings"]:
            if f["source"] != source:
                source = f["source"]
                out.append(f"  [{source}]")
            mark = "!!" if f["severity"] == ERROR else "??"
            slug = "" if f["slug"] in ("-", "") else f" {f['slug']}:"
            out.append(f"   {mark}{slug} {f['what']}")
            if f["detail"]:
                out.append(f"      {f['detail']}")
    skipped = [n for n, _ in CHECKS if n not in r["checks_run"]]
    if skipped:
        out.append(f"\n  (not run: {', '.join(skipped)})")

    models = r["usage"]["models"]
    out.append("")
    if not models:
        out.append("  No model calls recorded on this machine in the last day.")
    else:
        out.append("  Model spend, last day (this machine only):")
        for name, s in models.items():
            out.append(f"   {name:<28} {s['peak_rpm']:>3}/{s['limits']['rpm']} RPM"
                       f"   {s['calls_1d']:>4}/{s['limits']['rpd']} RPD"
                       f"   {s['tokens_1d']:>7} tok"
                       + (f"   {s['quota_429']} x 429" if s["quota_429"] else ""))
    return out


def markdown(r: dict[str, Any]) -> str:
    """The sweep as a GitHub issue body.

    Deliberately holds no company-level numbers beyond a slug and the rule
    that fired: this repo is public, so an issue body is public. The slugs and
    the sheet behind them already are (the studio reads it keylessly), but
    there is no reason to widen that with a table of figures.
    """
    lines = [f"**{headline(r)}** — swept {r['at']}", ""]
    if r["findings"]:
        lines.append("| | Check | Record | What |")
        lines.append("|---|---|---|---|")
        for f in r["findings"]:
            mark = "🔴" if f["severity"] == ERROR else "🟡"
            lines.append(f"| {mark} | `{f['source']}` | `{f['slug']}` | "
                         f"{f['what']} |")
        lines.append("")
        lines.append("<details><summary>Detail</summary>\n")
        for f in r["findings"]:
            lines.append(f"- **{f['slug']} · {f['what']}** — {f['detail']}")
        lines.append("\n</details>")
    else:
        lines.append("Nothing to report.")
    lines += ["", f"Checks run: {', '.join(r['checks_run']) or 'none'}.",
              "", "<sub>Posted by `.github/workflows/watch.yml`. "
                  "Re-run it, or `ipopulse check` locally, to refresh. "
                  "Model spend is per machine — see `usage.py`.</sub>"]
    return "\n".join(lines)
