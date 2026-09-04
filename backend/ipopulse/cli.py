"""ipopulse — the command line for the data side.

    ipopulse new <slug>              scaffold a YAML file for a new IPO
    ipopulse import <file|url>       pull IPOs / GMP rows out of Excel or CSV
    ipopulse sync --provider nse --discover   pull live IPOs from NSE, no key
    ipopulse push [slug]             write IPOs / GMP rows into a Google Sheet
    ipopulse sources <slug>          pin the exact pages to read for this IPO
    ipopulse research <slug>         Gemini web lookup, with citations
    ipopulse gmp-sync                GMP + subscription from InvestorGain, keyless
    ipopulse refresh                 model fallback for what InvestorGain lacks
    ipopulse gmp <slug> 46           log today's GMP (defaults to today)
    ipopulse sub <slug> 2 --qib 12.4 --nii 24.9 --retail 9.2 --total 14.6
    ipopulse translate [slug]        Gemini -> hi/te, cached, written to YAML
    ipopulse analyse <slug>          Gemini drafts overview / flags
    ipopulse enrich [slug]           fill whatever is missing, automatically
    ipopulse rhp <slug> --write      financials from NSE's prospectus
    ipopulse doctor [slug] [--fix]   what is missing; repair what is derivable
    ipopulse facts [slug] --write    financials + KPIs from InvestorGain, keyless
    ipopulse validate [slug]         which reels are recordable, and until when
    ipopulse monitor                 is the sheet still being updated? (cron)
    ipopulse cache --prune           drop Gemini responses past their TTL
    ipopulse build                   compute + publish JSON for the frontend
    ipopulse report [slug]           Excel workbook into backend/out/
    ipopulse serve                   local static server for the frontend
    ipopulse list                    what is tracked and where it stands

Ingestion translates as it goes (`import`, `sync`, `analyse --write`), so
Gemini runs once per data change instead of once per build. Pass
--no-translate to skip it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from . import dedupe, store, tables
from .sheets import SheetUnavailable
from .workbook import WorkbookLocked
from .ai import (ALLOTMENT_STEPS, OVERVIEW_BULLETS, AiUnavailable, Gemini,
                 default_model)
from .compute import derive
from .models import Briefing, Ipo
from .providers import get_provider
from .providers.base import merge, merge_series
from .publish import publish, verify
from .report import write_report

LANGS = ["hi", "te"]

# The local default. A cloud host overrides it through $PORT.
DEFAULT_PORT = 8000


# ── .env ───────────────────────────────────────────────────────────────────

def load_dotenv() -> None:
    """Minimal .env reader so the key never has to live in a shell profile."""
    for candidate in (store.BACKEND_ROOT / ".env", store.BACKEND_ROOT.parent / ".env"):
        if not candidate.exists():
            continue
        for line in candidate.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


# ── commands ───────────────────────────────────────────────────────────────

def cmd_new(args) -> int:
    path = store.scaffold(args.slug, overwrite=args.force)
    print(f"Added {args.slug} to {path.name}")
    print("Fill it in, then run:  ipopulse build")
    return 0


def cmd_remove(args) -> int:
    """Drop an IPO. Deleting a row is not deleting a file, so it needs saying."""
    try:
        ipo = store.load(args.slug)
    except FileNotFoundError:
        print(f"No IPO '{args.slug}' in the sheet.")
        return 1

    if not args.yes:
        gmp = len(ipo.gmp_history)
        print(f"About to remove {args.slug} ({ipo.company or 'unnamed'}) — "
              f"{gmp} GMP day(s), {len(ipo.subscription)} subscription day(s).")
        print("This rewrites the sheet. Re-run with --yes to confirm.")
        return 1

    store.backup()
    store.remove(args.slug)
    print(f"Removed {args.slug}. Previous workbook kept at "
          f"{store.OUT_DIR / 'ipo-pulse.prev.xlsx'}")
    return 0


def _print_plan(p: dict[str, Any]) -> None:
    """A merge plan as a reviewable diff."""
    lose = p["losing"]
    print(f"\n  keep {p['keep']}   ←   fold in {p['drop']}")
    if not p["changes"]:
        print("    nothing to gain — the dropped row holds nothing the "
              "keeper is missing")
    for c in p["changes"]:
        was = "(blank)" if c["from"] in (None, "", 0, 0.0) else str(c["from"])
        print(f"    {c['field']:<34} {was[:26]:<28} → {str(c['to'])[:34]}")
    for c in p.get("conflicts") or []:
        print(f"    ? {c['at']}: keeping {c['kept']}, "
              f"discarding {c['dropped']}")
    print(f"    dropping: company name {lose['company'][:40]!r}"
          + (f", exchange {lose['exchange']}" if lose["exchange"] else ""))


def cmd_dedupe(args) -> int:
    """Fold every set of rows that describe one offer into a single row.

    Dry run unless `--write`, and that default is not politeness. A merge is
    the one repair in this project that destroys data — every other fixer
    fills a blank — so the diff is meant to be read before it is applied.

    Which row survives is decided by `dedupe.choose`: the one that knows more,
    then the shorter slug. `--keep` overrides it when the automatic pick is
    wrong, which is worth having because completeness is a count and not a
    judgement.
    """
    ipos = store.load_all()
    found = dedupe.groups(ipos)
    if not found:
        print(f"No duplicates among {len(ipos)} row(s). "
              "Every offer is stored once.")
        return 0

    by_slug = {i.slug: i for i in ipos}
    print(f"{len(found)} duplicate group(s) among {len(ipos)} row(s):")
    plans = []
    for group in found:
        rows = [by_slug[s] for s in group["slugs"]]
        print(f"\n{', '.join(group['slugs'])}")
        print(f"  why: {group['why']} ({group['confidence']})")
        for r in rows:
            print(f"    {r.slug:<50} completeness {dedupe.completeness(r):>4}"
                  f"  {len(r.gmp_history)} GMP, {len(r.subscription)} sub")
        if args.keep and args.keep in group["slugs"]:
            keeper = by_slug[args.keep]
            losers = [r for r in rows if r.slug != args.keep]
        else:
            keeper, losers = dedupe.choose(rows)
        for loser in losers:
            p = dedupe.plan(keeper, loser)
            _print_plan(p)
            plans.append(p)

    if not args.write:
        print(f"\n(dry run — nothing written. {len(plans)} merge(s) ready; "
              f"re-run with --write)")
        return 0

    # A snapshot before the first write, not after. `remove` takes one too,
    # but a merge is two writes and the interesting state is the one before
    # either of them.
    snap = store.backup()
    print(f"\nsnapshot: {snap}" if snap else "\nsnapshot: nothing to back up")
    for p in plans:
        dedupe.apply(p)
        print(f"  merged {p['drop']} into {p['keep']}")

    # Read the store back rather than trusting the writes. A scheduled job
    # that overlapped this run would have rewritten the tab underneath it, and
    # a merge that silently half-applied is worse than one that failed.
    after = {i.slug: i for i in store.load_all()}
    ok = True
    for p in plans:
        if p["drop"] in after:
            print(f"  ! {p['drop']} is still on the sheet — the write did not "
                  f"take. Another job may have been writing at the same time.")
            ok = False
        elif p["keep"] not in after:
            print(f"  ! {p['keep']} is gone — restore from {snap}")
            ok = False
    print("\nVerified against the sheet." if ok else "\nVERIFY FAILED.")
    if ok:
        print("Next:  ipopulse facts && ipopulse build")
    return 0 if ok else 1


def cmd_merge(args) -> int:
    """Fold one named row into another. `dedupe` for a pair it did not find.

    The manual door, for the case `same_offer` cannot see — two rows for one
    offer under a brand name and a legal name with no shared token, no shared
    logo and a calendar that has not been filled in yet. `dedupe` handles
    everything it can recognise; this handles the rest, and it does not ask
    `same_offer` for permission because the point of it is that the answer
    would be no.
    """
    try:
        keep, drop = store.load(args.keep), store.load(args.drop)
    except FileNotFoundError as exc:
        print(f"{exc}")
        return 1
    if keep.slug == drop.slug:
        print("Those are the same row.")
        return 2

    confidence, why = dedupe.same_offer(dedupe.signature(keep),
                                        dedupe.signature(drop))
    print(f"{keep.slug} ({dedupe.completeness(keep)}) ← "
          f"{drop.slug} ({dedupe.completeness(drop)})")
    print(f"  automatic signals: {why} ({confidence})" if confidence
          else "  automatic signals: none — this is your call, not the "
               "matcher's")
    p = dedupe.plan(keep, drop)
    _print_plan(p)

    if not args.write:
        print("\n(dry run — nothing written. Re-run with --write)")
        return 0
    snap = store.backup()
    print(f"\nsnapshot: {snap}" if snap else "\nsnapshot: nothing to back up")
    dedupe.apply(p)
    after = {i.slug for i in store.load_all()}
    if args.drop in after or args.keep not in after:
        print(f"! the write did not take as expected — restore from {snap}")
        return 1
    print(f"Merged {args.drop} into {args.keep}, verified against the sheet.")
    print("Next:  ipopulse facts && ipopulse build")
    return 0


def cmd_list(args) -> int:
    ipos = store.load_all()
    if not ipos:
        print("No IPOs yet. Start one with:  ipopulse new <slug>")
        return 0
    print(f"{'SLUG':<26} {'COMPANY':<32} {'STATUS':<10} {'GMP':>8} {'SUB':>8}")
    print("-" * 90)
    for ipo in ipos:
        d = derive(ipo)
        sub = d["subscription"]
        print(
            f"{ipo.slug:<26} {(ipo.company or '—')[:32]:<32} "
            f"{d['dates']['status']:<10} "
            f"{('₹' + str(d['gmp']['gmp'])) if ipo.gmp_history else '—':>8} "
            f"{(str(sub['total']) + 'x') if sub['has_data'] else '—':>8}"
        )
    return 0


def cmd_gmp(args) -> int:
    ipo = store.load(args.slug)
    raw = ipo.to_dict()
    when = (args.date or date.today().isoformat())
    point = {"date": when, "gmp": args.value, "source": args.source}
    if args.kostak:
        point["kostak"] = args.kostak
    if args.sauda:
        point["sauda"] = args.sauda
    raw["gmp_history"] = merge_series(raw.get("gmp_history", []), [point])
    updated = Ipo.from_dict(raw)
    store.save(updated)
    d = derive(updated)
    print(f"{ipo.company}: GMP ₹{args.value} on {when} "
          f"({d['gmp']['pct']}%, est. listing ₹{d['gmp']['est_listing']})")
    print(f"  {d['gmp']['days_tracked']} day(s) tracked")
    return 0


def cmd_sub(args) -> int:
    ipo = store.load(args.slug)
    raw = ipo.to_dict()
    row = {
        "day": args.day,
        "date": args.date or date.today().isoformat(),
        "qib": args.qib, "nii": args.nii, "retail": args.retail,
        "employee": args.employee, "total": args.total,
    }
    rows = {int(r["day"]): r for r in raw.get("subscription", [])}
    rows[int(args.day)] = {**rows.get(int(args.day), {}), **row}
    raw["subscription"] = [rows[k] for k in sorted(rows)]
    store.save(Ipo.from_dict(raw))
    print(f"{ipo.company}: day {args.day} subscription saved ({args.total}x overall)")
    return 0


def cmd_sync(args) -> int:
    """Batched wrapper. See _cmd_sync_body for what this actually does.

    Every save inside one run collapses into a single sheet write. Without
    this, a loop over 28 IPOs is 56 write requests against a 60-per-minute
    quota — which is how the whole spreadsheet came to be emptied on 4 Sep.
    """
    with store.batched():
        return _cmd_sync_body(args)


def _cmd_sync_body(args) -> int:
    """Pull from a provider and merge into the YAML (manual values win)."""
    provider = get_provider(args.provider)
    if not provider.available():
        print(f"Provider {args.provider!r} is not configured — nothing to sync.")
        print("See backend/ipopulse/providers/api.py for the contract to implement.")
        return 1

    slugs = [args.slug] if args.slug else store.list_slugs()

    if getattr(args, "discover", False) and not args.slug:
        # Without this, sync can only ever refresh IPOs someone already typed
        # in by hand — a live catalogue that cannot introduce a new listing is
        # only half a feed.
        try:
            catalogue = provider.fetch_catalogue()
        except Exception as exc:
            print(f"  ! catalogue unavailable: {exc}")
            catalogue = []
        known = set(store.list_slugs())
        # Everything already stored, loaded once, because the collision check
        # below needs the whole store and this loop can be 40 rows long.
        stored = store.load_all()
        for rec in catalogue:
            slug = rec.get("slug")
            if not slug or slug in known:
                continue
            # `slug in known` only catches a row we would file under the exact
            # same slug. NSE publishes legal names, so its slug for an issue
            # InvestorGain already gave us is a different string for the same
            # company — which is how 'rays-of-belief' acquired a twin called
            # 'rays-of-belief-limited-for-profit-social-enterpr'. Ask the one
            # definition of "same offer" before creating anything.
            if not getattr(args, "allow_duplicate", False):
                hit = dedupe.collides(rec, stored)
                if hit:
                    print(f"  = {slug} is {hit['slug']} ({hit['company']}) — "
                          f"{hit['why']}, {hit['confidence']}. Not scaffolded; "
                          f"this provider's facts will merge into that row. "
                          f"Override with --allow-duplicate.")
                    if hit["slug"] not in slugs:
                        slugs.append(hit["slug"])
                    continue
            store.scaffold(slug, overwrite=True)
            print(f"  + discovered {slug} ({rec.get('company', '')})")
            known.add(slug)
            slugs.append(slug)
            stored.append(store.load(slug))

    for slug in slugs:
        raw = store.load(slug).to_dict()
        try:
            raw = merge(raw, provider.fetch_ipo(slug), prefer_incoming=args.prefer_api)
            raw["gmp_history"] = merge_series(
                raw.get("gmp_history", []), provider.fetch_gmp(slug)
            )
            # Subscription keys on `day`, not `date` — the exchange reports a
            # running total for the whole bidding window, so syncing twice in
            # one day must overwrite that day rather than append to it.
            raw["subscription"] = merge_series(
                raw.get("subscription", []),
                provider.fetch_subscription(slug),
                key="day",
            )
            store.save(Ipo.from_dict(raw))
            print(f"synced {slug}")
            maybe_translate(slug, args)     # translate at ingestion, once
        except NotImplementedError as exc:
            print(f"  ! {slug}: adapter not implemented yet — {exc}")
        except Exception as exc:
            print(f"  ! {slug}: {exc}")
    return 0


def translate_one(gem: Gemini, slug: str, langs: list[str], force: bool = False) -> bool:
    """Translate an IPO's prose and write it into the YAML. Returns True if saved.

    Called automatically after any command that changes the prose, so Gemini
    runs once per data change rather than once per build.
    """
    ipo = store.load(slug)
    a = ipo.analysis
    fields = {
        "overview": a.overview, "green_flags": a.green_flags, "red_flags": a.red_flags,
        "growth": a.growth, "valuation": a.valuation, "risk": a.risk,
        "sector": ipo.sector,
        # `background` is prose and gets translated; `about_facts` deliberately
        # is not and must stay out of here — a promoter's name and a city have
        # one correct form, and only the labels cross languages (see i18n.js).
        "background": a.background,
    }
    if not any(fields.values()):
        return False                      # nothing written yet; nothing to translate

    raw = ipo.to_dict()
    raw.setdefault("i18n", {})
    changed = False
    for lang in langs:
        try:
            out = gem.translate_fields(fields, lang, company=ipo.company, force=force)
        except AiUnavailable as exc:
            print(f"  ! {slug}/{lang}: {exc}")
            continue
        out["allotment_steps"] = ipo.allotment.steps or ALLOTMENT_STEPS.get(lang, [])
        raw["i18n"][lang] = out
        changed = True
        print(f"  {slug} -> {lang} ok")
    if changed:
        store.save(Ipo.from_dict(raw))
    return changed


def maybe_translate(slug: str, args) -> None:
    """Auto-translate after ingestion unless --no-translate was passed."""
    if getattr(args, "no_translate", False):
        return
    gem = Gemini(model=getattr(args, "model", None) or default_model())
    if not gem.available():
        return                            # silent: the site still builds in English
    translate_one(gem, slug, LANGS)


def cmd_translate(args) -> int:
    gem = Gemini(model=args.model)
    if not gem.available():
        print("Gemini not configured (no GEMINI_API_KEY, or google-genai missing).")
        print("The site still builds — captions just stay in English.")
        return 1
    slugs = [args.slug] if args.slug else store.list_slugs()
    langs = args.langs.split(",") if args.langs else LANGS
    for slug in slugs:
        translate_one(gem, slug, langs, force=args.force)
    stats = gem.cache_stats()
    print(f"Cached {stats['entries']} responses ({stats['kb']} KB), "
          f"expiring after {stats['ttl_days']} days.")
    return 0


def cmd_cache(args) -> int:
    gem = Gemini(cache_days=args.days)
    if args.clear:
        print(f"Cleared {gem.clear_cache()} cached responses.")
        return 0
    if args.prune:
        removed, kept = gem.prune_cache(args.days)
        print(f"Pruned {removed} entries older than {args.days} days; {kept} kept.")
        return 0
    s = gem.cache_stats()
    print(f"{s['entries']} entries · {s['kb']} KB · oldest {s['oldest_days']} days "
          f"· TTL {s['ttl_days']} days")
    print("\nTranslations are also written into each IPO's YAML, so pruning the")
    print("cache never loses text — it only means the next edit re-asks Gemini.")
    return 0


def cmd_import(args) -> int:
    """Pull IPOs (or GMP rows) out of a spreadsheet — local file or URL."""
    from .providers import sheet as sheetmod

    parsed = sheetmod.parse(args.source, kind=args.kind, sheet=args.sheet)
    recs = parsed["records"]
    print(f"Read {len(recs)} row(s) from {args.source}")
    print(f"  header row {parsed['header_row'] + 1}, "
          f"matched columns: {', '.join(sorted(parsed['columns'])) or 'none'}")
    if parsed["unmatched"]:
        print(f"  ignored columns: {', '.join(parsed['unmatched'][:12])}")
    if parsed["skipped"]:
        print(f"  skipped {parsed['skipped']} row(s) with no "
              f"{'GMP' if args.kind == 'gmp' else 'company name'}")
    if not recs:
        print("Nothing to import. Check --kind and the header row.")
        return 1

    if args.dry_run:
        for r in recs[:8]:
            print("   ", {k: v for k, v in r.items() if v is not None})
        print("\n(dry run — nothing written)")
        return 0

    touched: list[str] = []
    for rec in recs:
        incoming = sheetmod.to_ipo_dict(rec)
        slug = args.slug or incoming["slug"]
        try:
            base = store.load(slug).to_dict()
        except FileNotFoundError:
            # An outside spreadsheet is the one door where a human chose the
            # name, so this warns and proceeds rather than refusing: they may
            # genuinely be adding a second row on purpose, and `import` is
            # not a scheduled job that runs unattended. The watchdog will
            # still report the pair.
            hit = dedupe.collides(incoming, store.load_all())
            if hit:
                print(f"  ! {slug} looks like {hit['slug']} "
                      f"({hit['company']}) — {hit['why']}, "
                      f"{hit['confidence']}. Creating it anyway because you "
                      f"named it; run `ipopulse dedupe` if that was not "
                      f"deliberate.")
            store.scaffold(slug, overwrite=True)
            base = store.load(slug).to_dict()
            print(f"  + created {slug}")
        gmp_rows = incoming.pop("gmp_history", [])
        merged = merge(base, incoming, prefer_incoming=args.prefer_sheet)
        if gmp_rows:
            merged["gmp_history"] = merge_series(merged.get("gmp_history", []), gmp_rows)
        merged["slug"] = slug
        store.save(Ipo.from_dict(merged))
        if slug not in touched:
            touched.append(slug)

    print(f"Imported into {len(touched)} IPO file(s): {', '.join(touched)}")
    # Translate here, at ingestion, so Gemini is called once per data change.
    for slug in touched:
        maybe_translate(slug, args)
    print("\nNext:  ipopulse build")
    return 0


def cmd_job(args) -> int:
    """Run a named job from control.JOBS — the scheduler's entry point.

    The timers call this rather than spelling out flags, so a schedule and the
    Trigger panel can never drift apart: both resolve the same name through the
    same dict. Change a job's arguments in one place and both follow.
    """
    from . import control

    if not args.name:
        print(f"{'JOB':<12}{'SCHEDULE':<38}WHAT IT DOES")
        print("-" * 90)
        for name, spec in control.JOBS.items():
            print(f"{name:<12}{spec['schedule']:<38}{spec['label']}")
        print("\nRun one       :  ipopulse job daily")
        print("Run in order  :  ipopulse job sync build push report")
        return 0

    unknown = [n for n in args.name if n not in control.JOBS]
    if unknown:
        print(f"error: unknown job(s): {', '.join(unknown)}. "
              f"Known: {', '.join(control.JOBS)}", file=sys.stderr)
        return 2

    steps = control.expand(args.name)
    for i, (step, argv) in enumerate(steps, 1):
        print(f"[{i}/{len(steps)}] $ ipopulse {' '.join(argv)}", flush=True)
        rc = main(argv)
        if rc:
            # Stop rather than continue: a push behind a failed sync would
            # publish stale numbers as though they were today's.
            print(f"! step {i} ({step}) exited {rc} — stopping, "
                  f"{len(steps) - i} step(s) skipped", file=sys.stderr)
            return rc
    return 0


def cmd_research(args) -> int:
    """Ask Gemini to look up a GMP or the issue details, with citations."""
    from .providers.research import ResearchProvider

    provider = ResearchProvider(Gemini(model=args.model))
    if not provider.available():
        print("Gemini not configured — set GEMINI_API_KEY in .env.")
        return 1

    ipo = store.load(args.slug)
    urls = args.url.split(",") if args.url else None
    if urls:
        provider.sources = urls

    if args.what in ("ipo", "both"):
        print(f"Researching issue details for {ipo.company or args.slug}…")
        try:
            # `ipo=ipo` is what makes a pinned `sources.issue` URL take effect.
            # Without it urls_for falls back to the site defaults, which are
            # index pages listing every IPO — so a pin was silently ignored
            # and the lookup read a directory instead of the company's page.
            found = provider.fetch_ipo(args.slug, company=ipo.company, ipo=ipo)
        except AiUnavailable as exc:
            print(f"  ! {exc}")
            return 1
        meta = found.pop("_meta", {})
        print(f"  confidence: {meta.get('confidence')}  |  {meta.get('note', '')[:160]}")
        for u in meta.get("sources", [])[:4]:
            print(f"    source: {u}")
        print(f"  {json.dumps(found, ensure_ascii=False, default=str)[:400]}")
        if args.write:
            merged = merge(ipo.to_dict(), found, prefer_incoming=False)
            store.save(Ipo.from_dict(merged))
            print("  written (existing values kept; only blanks filled)")

    if args.what in ("background", "all"):
        print(f"\nResearching background on {ipo.company or args.slug}…")
        # The filing's own description goes in so the model adds context around
        # it rather than paraphrasing it back. Best-effort: without it the
        # lookup still runs, it just has less to build on.
        known, hq = "", ""
        try:
            from .providers import investorgain as _ig
            row = _ig.resolve(args.slug, ipo.company or "")
            brief = _ig.company_brief(row) if row else {}
            known = brief.get("about") or brief.get("summary") or ""
            hq = brief.get("hq") or ""
        except Exception:
            pass
        try:
            got = Gemini(model=args.model).research_background(
                ipo.company or args.slug, sector=ipo.sector, hq=hq,
                known=known, force=args.force)
        except AiUnavailable as exc:
            print(f"  ! {exc}")
            return 1
        points = got.get("background") or []
        print(f"  confidence: {got.get('confidence')}  |  "
              f"{got.get('note', '')[:160]}")
        for u in (got.get("sources") or [])[:3]:
            print(f"    source: {u[:100]}")
        for p in points:
            print(f"  • {p}")
        if not points:
            # An empty list is the honest answer for an obscure SME, not a
            # failure — so say which it is rather than looking like a crash.
            print("  (nothing it could vouch for — the scene will hide this "
                  "block rather than show invented lines)")
        elif args.write:
            raw = ipo.to_dict()
            raw["analysis"]["background"] = points
            store.save(Ipo.from_dict(raw))
            print(f"  written ({len(points)} point(s)) — read them before "
                  f"publishing; this is the one block on reel 1 that no "
                  f"filing backs.")
            maybe_translate(args.slug, args)
        ipo = store.load(args.slug)

    flagged = False

    if args.what in ("gmp", "both", "all"):
        print(f"\nResearching today's GMP for {ipo.company or args.slug}…")
        print(f"  reading: {', '.join(provider.urls_for('gmp', ipo)[:2])}")
        try:
            points = provider.fetch_gmp(args.slug, company=ipo.company,
                                        price_high=ipo.issue.price_high, ipo=ipo)
        except AiUnavailable as exc:
            print(f"  ! {exc}")
            return 1
        if not points:
            print("  No GMP found. That is a valid answer — enter it manually:")
            print(f"    ipopulse gmp {args.slug} <value>")
        else:
            p = points[0]
            band = ipo.issue.price_high
            pct = f" ({p['gmp'] / band * 100:.1f}% of band)" if band else ""
            print(f"  GMP ₹{p['gmp']}{pct} as of {p['date']}  ·  confidence {p['confidence']}")
            for u in p.get("sources", [])[:4]:
                print(f"    source: {u}")

            if p.get("needs_review"):
                flagged = True
                print(f"\n  ⚠ FLAGGED: {p['review_reason']}")
                print("  Check the sources above, then either:")
                print(f"    ipopulse gmp {args.slug} <value>                 (type the real one)")
                print(f"    ipopulse research {args.slug} --write --force    (accept anyway)")

            if args.write and (not p.get("needs_review") or args.force):
                raw = store.load(args.slug).to_dict()
                clean = {k: p[k] for k in ("date", "gmp", "kostak", "source") if k in p}
                raw["gmp_history"] = merge_series(raw.get("gmp_history", []), [clean])
                store.save(Ipo.from_dict(raw))
                print("  written to the YAML")
            elif args.write:
                print("  not written (flagged)")
            else:
                print("  (not saved — re-run with --write)")

    if args.what in ("gmp-history", "all"):
        from . import doctor

        gaps = doctor.gmp_gaps(ipo)
        print(f"\nReading the GMP history for {ipo.company or args.slug}…")
        print(f"  reading: {', '.join(provider.urls_for('gmp', ipo)[:2])}")
        if gaps:
            print(f"  {len(gaps)} day(s) missing: {', '.join(gaps[:8])}")
        try:
            rows = provider.fetch_gmp_history(
                args.slug, company=ipo.company,
                price_high=ipo.issue.price_high, ipo=ipo,
                since=(gaps[0] if gaps else None),
            )
        except AiUnavailable as exc:
            print(f"  ! {exc}")
            return 1

        if not rows:
            print("  No dated table found. Enter the missing days by hand:")
            for day in gaps[:5]:
                print(f"    ipopulse gmp {args.slug} <value> --date {day}")
        else:
            have = {p.date.isoformat(): p.gmp for p in ipo.gmp_history if p.date}
            fresh = [r for r in rows if r["date"] not in have]
            # A stored value that disagrees with the source is worth more
            # attention than a missing one: it is already on the card, and it
            # is wrong there.
            clashes = [r for r in rows
                       if r["date"] in have and abs(have[r["date"]] - r["gmp"]) > 0.01]
            print(f"  {len(rows)} row(s) read, {len(fresh)} new, {len(clashes)} conflicting")
            for r in rows:
                if r["date"] in have:
                    mine = have[r["date"]]
                    mark = "≠" if abs(mine - r["gmp"]) > 0.01 else "·"
                    note = f"  (yours: ₹{mine:g})" if mark == "≠" else ""
                else:
                    mark = "⚠" if r["needs_review"] else "+"
                    note = f"  {r['review_reason']}" if r["needs_review"] else ""
                print(f"    {mark} {r['date']}  ₹{r['gmp']:g}{note}")
            if clashes:
                print(f"\n  ≠ {len(clashes)} stored value(s) disagree with the source.")
                print("    Left alone — a reading taken live on the day is not "
                      "automatically worse\n    than a page's later memory of it. "
                      "Overwrite with --force if the\n    source is right.")
            for u in (rows[0].get("sources") or [])[:3]:
                print(f"    source: {u}")

            # Only ever fills holes. A researched value must not silently
            # replace a reading that was taken on the day itself — that one
            # was live, this one is a page's memory of it.
            candidates = fresh + (clashes if args.force else [])
            writable = [r for r in candidates if not r["needs_review"] or args.force]
            skipped = [r for r in fresh if r["needs_review"] and not args.force]
            if skipped:
                flagged = True
                print(f"\n  ⚠ {len(skipped)} flagged row(s) not written "
                      f"(re-run with --force to accept)")

            if args.write and writable:
                raw = store.load(args.slug).to_dict()
                clean = [{k: r[k] for k in ("date", "gmp", "kostak", "source")}
                         for r in writable]
                raw["gmp_history"] = merge_series(raw.get("gmp_history", []), clean)
                store.save(Ipo.from_dict(raw))
                print(f"  wrote {len(writable)} new day(s) into the YAML")
                left = doctor.gmp_gaps(store.load(args.slug))
                print(f"  {len(left)} gap(s) remaining"
                      + (f": {', '.join(left[:6])}" if left else ""))
            elif writable:
                print(f"  ({len(writable)} new day(s) not saved — re-run with --write)")

    if args.what in ("financials", "all"):
        print(f"\nResearching financials for {ipo.company or args.slug}…")
        try:
            fin = provider.fetch_financials(args.slug, company=ipo.company, ipo=ipo)
        except AiUnavailable as exc:
            print(f"  ! {exc}")
            return 1
        meta = fin.pop("_meta", {})
        rows = {k: v for k, v in fin.items() if k != "years" and v}
        print(f"  confidence: {meta.get('confidence')}  |  {meta.get('note', '')[:160]}")
        for u in meta.get("sources", [])[:4]:
            print(f"    source: {u}")
        if not rows:
            print("  Nothing usable found. The RHP PDF is the fallback — type it in:")
            print(f"    the Financials tab of the sheet, rows for {args.slug}")
        else:
            print(f"  years: {', '.join(fin['years'])}")
            for key, vals in rows.items():
                print(f"    {key:<13} {vals}")

        if meta.get("needs_review"):
            flagged = True
            print(f"\n  ⚠ FLAGGED: {meta.get('review_reason')}")
            print("  These drive the fundamentals and valuation halves of the "
                  "score, so a wrong figure is worse than a missing one.")

        if args.write and rows and (not meta.get("needs_review") or args.force):
            base = store.load(args.slug).to_dict()
            block = base.setdefault("financials", {})
            # Merge field by field, and never let an empty result clear a
            # populated one. Two runs of the same lookup do not return the
            # same thing: a re-run of LEAP India came back with rounded
            # revenue and no PAT at all, and a blanket .update() wrote that
            # over a precise three-year series it already had. A second
            # opinion should be able to add, not to erase.
            kept = []
            for key, value in fin.items():
                if isinstance(value, list) and not value and block.get(key):
                    kept.append(key)
                    continue
                block[key] = value
            store.save(Ipo.from_dict(base))
            print("  written to the YAML")
            if kept:
                print(f"  kept existing {', '.join(kept)} "
                      f"(this read returned nothing for them)")
        elif args.write and rows:
            print("  not written (flagged) — re-run with --force to accept")
        elif rows:
            print("  (not saved — re-run with --write)")

    if args.what in ("sub", "subscription", "all"):
        print(f"\nResearching subscription for {ipo.company or args.slug}…")
        print(f"  reading: {', '.join(provider.urls_for('subscription', ipo)[:2])}")
        try:
            rows = provider.fetch_subscription(args.slug, company=ipo.company, ipo=ipo)
        except AiUnavailable as exc:
            print(f"  ! {exc}")
            return 1
        if not rows:
            print("  No subscription figures found (issue may not have opened).")
            print(f"    ipopulse sub {args.slug} <day> --qib .. --nii .. --retail .. --total ..")
        else:
            r = rows[0]
            print(f"  Day {r['day']} as of {r['date']}  ·  confidence {r['confidence']}")
            print(f"    QIB {r['qib']}x · NII {r['nii']}x · Retail {r['retail']}x "
                  f"· Total {r['total']}x")
            for u in r.get("sources", [])[:4]:
                print(f"    source: {u}")

            if r.get("needs_review"):
                flagged = True
                print(f"\n  ⚠ FLAGGED: {r['review_reason']}")

            if args.write and (not r.get("needs_review") or args.force):
                raw = store.load(args.slug).to_dict()
                clean = {k: r[k] for k in
                         ("day", "date", "qib", "nii", "retail", "employee", "total")}
                rows_by_day = {int(x["day"]): x for x in raw.get("subscription", [])}
                rows_by_day[int(clean["day"])] = {**rows_by_day.get(int(clean["day"]), {}), **clean}
                raw["subscription"] = [rows_by_day[k] for k in sorted(rows_by_day)]
                store.save(Ipo.from_dict(raw))
                print("  written to the YAML")
            elif args.write:
                print("  not written (flagged)")
            else:
                print("  (not saved — re-run with --write)")

    return 2 if flagged and not args.force else 0


def cmd_rhp(args) -> int:
    """Read the Red Herring Prospectus off NSE and pull the hard fields out.

    EBITDA, net worth, total debt and the peer P/E live nowhere free except
    the RHP itself — which NSE publishes, so this needs no key beyond Gemini.
    """
    from .providers import rhp as rhpmod
    from .providers.scrape import NseProvider, slugify

    ipo = store.load(args.slug)
    gem = Gemini(model=args.model)
    if not gem.available():
        print("Gemini not configured — set GEMINI_API_KEY in .env.")
        return 1

    # NSE keys the document on (symbol, series), not on our slug.
    symbol, series = args.symbol, args.series
    if not symbol:
        prov = NseProvider()
        ref = prov._ref_for(args.slug)
        if not ref:
            print(f"{args.slug} is not in NSE's current catalogue — pass "
                  f"--symbol to name it explicitly.")
            return 1
        symbol, series = ref

    print(f"Reading the RHP for {ipo.company or args.slug} ({symbol}/{series})…")
    try:
        doc = rhpmod.pages_for(args.slug, symbol, series, refresh=args.refresh)
    except Exception as exc:
        print(f"  ! could not fetch the RHP: {exc}")
        return 1

    if not doc.get("url"):
        print("  No Red Herring Prospectus linked on NSE for this issue.")
        return 1
    print(f"  {doc['url']}")
    print(f"  {len(doc['pages'])} pages"
          + (f" from {doc.get('file')}" if doc.get("file") else ""))

    if not doc.get("readable"):
        # The scanned-advertisement case. Saying so is the whole point: the
        # alternative is handing a model a wall of mojibake and believing the
        # tidy JSON it invents from it.
        print("  ! this PDF has no extractable text (scanned image). Nothing "
              "was sent to the model.")
        return 1

    sections = rhpmod.excerpts(doc["pages"])
    if not sections:
        print("  ! none of the expected sections were found in the text.")
        return 1
    total = sum(len(v) for v in sections.values())
    print(f"  sending {len(sections)} section(s), {total:,} chars: "
          f"{', '.join(sections)}")

    years = list(ipo.financials.years or ["FY24", "FY25", "FY26"])
    try:
        raw = gem.read_rhp(ipo.company or args.slug, sections, years=years)
    except AiUnavailable as exc:
        print(f"  ! {exc}")
        return 1

    print(f"  confidence: {raw.get('confidence')}  |  {str(raw.get('note',''))[:150]}")
    if raw.get("peers"):
        print(f"  peers seen: {', '.join(str(p) for p in raw['peers'][:5])}")

    # Same shape vetting as the web lookup: a short array is the dangerous
    # case, because compute pairs years to values by position.
    found = [str(y).strip() for y in (raw.get("years") or []) if str(y).strip()]
    if found:
        years = found
    # Unit conversion happens HERE, never in the model. Asked to "convert to
    # crore" it politely reported millions instead and noted the unit in prose
    # — every figure 10x too big, and nothing about a plausible ₹8,365 crore
    # revenue looks wrong on a card. So the prompt now demands the figures
    # exactly as printed plus the unit as a field, and the arithmetic is done
    # in code where it can be read.
    TO_CRORE = {"crore": 1.0, "cr": 1.0, "million": 0.1, "mn": 0.1,
                "billion": 100.0, "bn": 100.0, "lakh": 0.01, "rupees": 1e-7}

    def _f(v: Any) -> float:
        """Tolerate "3,719.44" — the RHP prints thousands separators and the
        model copies figures as printed, which is exactly what it was told to
        do. Rejecting those as non-numeric threw away a whole clean read."""
        if isinstance(v, str):
            v = v.replace(",", "").replace("−", "-").strip()
        return float(v)
    unit = str(raw.get("unit") or "").strip().lower()
    factor = TO_CRORE.get(unit)

    n = len(years)
    series_out, problems = {}, []
    if factor is None:
        problems.append(f"unrecognised unit {unit or '(none given)'}")
        factor = 0.0
    for key in ("revenue", "ebitda", "pat", "net_worth", "total_debt"):
        vals = raw.get(key) or []
        if not vals:
            continue
        if len(vals) != n:
            problems.append(f"{key}: {len(vals)} values for {n} years")
            continue
        if any(v is None for v in vals):
            # Distinct from a length mismatch, and it used to print
            # "3 values for 3 years", which reads as a contradiction.
            missing = [years[i] for i, v in enumerate(vals) if v is None]
            problems.append(f"{key}: no figure for {', '.join(missing)}")
            continue
        try:
            series_out[key] = [round(_f(v) * factor, 2) for v in vals]
        except (TypeError, ValueError):
            problems.append(f"{key}: non-numeric")
    if factor == 0.0:
        series_out = {}

    block: dict[str, Any] = {"years": years, **series_out}
    for key in ("eps", "pe_peer_avg"):
        if raw.get(key) is not None:
            try:
                block[key] = _f(raw[key])
            except (TypeError, ValueError):
                problems.append(f"{key}: non-numeric")

    if not series_out and len(block) <= 1:
        print("  Nothing usable extracted.")
        return 1
    print(f"  years: {', '.join(years)}   (source unit: {unit or '?'} "
          f"-> ₹ crore)")
    for key, vals in block.items():
        if key != "years":
            print(f"    {key:<13} {vals}")

    flagged = bool(problems) or raw.get("confidence") != "high"
    if problems:
        print(f"\n  ⚠ {'; '.join(problems)}")
    elif flagged:
        print(f"\n  ⚠ confidence {raw.get('confidence')}")

    # The cover date is free and independent of whether the figures vetted —
    # it comes from the text, not the model, so it is written either way.
    announced = rhpmod.filing_date(doc["pages"], opens=ipo.dates.open)
    if announced:
        print(f"  filed (announced): {announced}")

    if args.write and (not flagged or args.force):
        base = store.load(args.slug).to_dict()
        blk = base.setdefault("financials", {})
        kept = []
        for key, value in block.items():
            if isinstance(value, list) and not value and blk.get(key):
                kept.append(key)
                continue
            blk[key] = value
        if announced and not base.get("dates", {}).get("announced"):
            base.setdefault("dates", {})["announced"] = announced
        store.save(Ipo.from_dict(base))
        print("  written to the YAML")
        if kept:
            print(f"  kept existing {', '.join(kept)}")
    elif args.write and announced:
        # Even a flagged read still yields a trustworthy filing date.
        base = store.load(args.slug).to_dict()
        if not base.get("dates", {}).get("announced"):
            base.setdefault("dates", {})["announced"] = announced
            store.save(Ipo.from_dict(base))
            print("  wrote dates.announced only (figures were flagged)")
    elif args.write:
        print("  not written (flagged) — re-run with --force to accept")
    else:
        print("  (not saved — re-run with --write)")
    return 0


def cmd_sources(args) -> int:
    """Show or pin the exact pages this IPO should be read from."""
    from .providers.research import SITES

    ipo = store.load(args.slug)
    if args.set:
        role, _, url = args.set.partition("=")
        role = role.strip()
        if role not in SITES:
            print(f"Unknown role {role!r}. Expected one of: {', '.join(SITES)}")
            return 1
        raw = ipo.to_dict()
        raw.setdefault("sources", {})
        if url.strip():
            raw["sources"][role] = url.strip()
            print(f"Pinned {role} -> {url.strip()}")
        else:
            raw["sources"].pop(role, None)
            print(f"Unpinned {role}")
        store.save(Ipo.from_dict(raw))
        return 0

    print(f"{ipo.company or args.slug}\n")
    for role, defaults in SITES.items():
        pinned = ipo.sources.get(role)
        print(f"  {role:<13} {pinned or '(not pinned)'}")
        if not pinned:
            for u in defaults[:2]:
                print(f"                fallback: {u}")
    print("\nPin one with:")
    print(f"  ipopulse sources {args.slug} --set gmp=https://www.investorgain.com/...")
    print(f"  ipopulse sources {args.slug} --set subscription=https://groww.in/ipo/...")
    print("\nA pinned page is read directly, which removes the two ways an open")
    print("web lookup goes wrong: the wrong company, and a stale cached page.")
    return 0


def _investorgain_covers(slug: str, company: str = "") -> bool:
    """Is this IPO one InvestorGain carries?

    Answers "is the desk covering this company", not "has it quoted a premium
    today" — the second question is the desk's to answer, including when the
    answer is silence. Unreachable counts as *not* covered, so a bad morning
    on InvestorGain lets the model fall back rather than blanking the GMP for
    every IPO at once.
    """
    try:
        from .providers import investorgain as ig
        return ig.resolve(slug, company) is not None
    except Exception:
        return False


def cmd_refresh(args) -> int:
    """Batched wrapper. See _cmd_refresh_body for what this actually does.

    Every save inside one run collapses into a single sheet write. Without
    this, a loop over 28 IPOs is 56 write requests against a 60-per-minute
    quota — which is how the whole spreadsheet came to be emptied on 4 Sep.
    """
    with store.batched():
        return _cmd_refresh_body(args)


def _cmd_refresh_body(args) -> int:
    """The daily loop: re-read GMP (+ subscription), then republish."""
    from .providers.research import ResearchProvider

    provider = ResearchProvider(Gemini(model=args.model))
    if not provider.available():
        print("Gemini not configured — set GEMINI_API_KEY in .env.")
        print("Without it, log numbers by hand:  ipopulse gmp <slug> <value>")
        return 1

    slugs = [args.slug] if args.slug else store.list_slugs()
    today = date.today().isoformat()
    flagged: list[str] = []
    covered = 0
    for slug in slugs:
        ipo = store.load(slug)
        status = derive(ipo)["dates"]["status"]
        if status in ("listed",) and not args.all:
            continue                       # nothing left to track

        # The free desk already answered for today — don't pay for the same
        # number twice, and don't let a model's reading overwrite it.
        #
        # This is what the `grey` chain always claimed to do ("gmp then fills
        # only what the free source did not cover") and never did: this loop
        # had no such check, and `merge_series` is last-writer-wins per date,
        # so every night the model re-read pages InvestorGain had already
        # priced and replaced its figures with its own. Molbio on 16 Aug was
        # written 128 by the free source and then overwritten 130.
        if not args.all and any(
            p.date and p.date.isoformat() == today
            and (p.source or "manual") in SUPERSEDABLE
            for p in ipo.gmp_history
        ):
            covered += 1
            continue

        # Stronger version of the same rule: if InvestorGain carries this IPO
        # at all, the model has no business writing its GMP — not today, not
        # any day.
        #
        # The check above only skips an IPO the free desk has *already priced
        # today*, which quietly let the model answer for one InvestorGain
        # covers but has not quoted. That is not a gap. InvestorGain opens a
        # GMP row the day an issue is announced and leaves the premium at 0
        # until a dealer actually prices it, and `history()` trims that run of
        # leading zeros deliberately — "not quoted" is an absence, and an
        # absence is not a number. The model, asked the same question, does
        # not return an absence: it returns 0. Fascinate Textiles and
        # Pramodini Medicare each carried a fortnight of ₹0 "quotes" written
        # this way, and every one of them read as a real premium of zero on
        # the card and in the trail.
        #
        # So the desk's silence counts as its answer. The model is left with
        # the one job it is actually needed for: an IPO InvestorGain does not
        # carry.
        if not args.all and _investorgain_covers(slug, ipo.company or ""):
            covered += 1
            continue

        print(f"\n── {ipo.company or slug} ({status})")
        try:
            points = provider.fetch_gmp(slug, company=ipo.company,
                                        price_high=ipo.issue.price_high, ipo=ipo)
        except AiUnavailable as exc:
            print(f"  ! {exc}")
            continue
        if not points:
            print("  no GMP found")
        else:
            p = points[0]
            mark = "⚠" if p.get("needs_review") else "✓"
            print(f"  {mark} GMP ₹{p['gmp']} ({p['date']}, {p['confidence']})")
            if p.get("needs_review"):
                print(f"    {p['review_reason']}")
                flagged.append(slug)
            # A model's ₹0 is "I could not find a quote", not "the premium is
            # zero", and the two are written identically. Refuse it.
            #
            # A real zero exists — an issue whose premium collapses to par
            # after trading has begun genuinely is 0, and Optimystix has four
            # such days. But those arrive from InvestorGain, which distinguishes
            # a quoted par from an unquoted row by omitting the latter. A
            # grounded lookup has no such vocabulary: it returns 0 for "no
            # premium found on the page", and merge_series then files it as a
            # reading. This is the guard `tables.py` describes as its own rule
            # — "absence is not zero; a written 0 invents a fact" — applied at
            # the one door that was not enforcing it.
            if not p.get("needs_review") and float(p.get("gmp") or 0) == 0:
                print("    ₹0 from a model reads as 'no quote found', not "
                      "'quoted at par' — not written")
            elif not p.get("needs_review") or args.force:
                raw = store.load(slug).to_dict()
                clean = {k: p[k] for k in ("date", "gmp", "kostak", "source") if k in p}
                raw["gmp_history"] = merge_series(raw.get("gmp_history", []), [clean])
                store.save(Ipo.from_dict(raw))

        if args.subscription and status == "open":
            try:
                rows = provider.fetch_subscription(slug, company=ipo.company, ipo=ipo)
            except AiUnavailable:
                rows = []
            if rows:
                r = rows[0]
                mark = "⚠" if r.get("needs_review") else "✓"
                print(f"  {mark} Day {r['day']} total {r['total']}x")
                if r.get("needs_review"):
                    print(f"    {r['review_reason']}")
                    flagged.append(slug)
                if not r.get("needs_review") or args.force:
                    raw = store.load(slug).to_dict()
                    clean = {k: r[k] for k in
                             ("day", "date", "qib", "nii", "retail", "employee", "total")}
                    by_day = {int(x["day"]): x for x in raw.get("subscription", [])}
                    by_day[int(clean["day"])] = {**by_day.get(int(clean["day"]), {}), **clean}
                    raw["subscription"] = [by_day[k] for k in sorted(by_day)]
                    store.save(Ipo.from_dict(raw))

    print()
    if covered:
        print(f"{covered} IPO(s) skipped — already priced today by a free "
              f"source. Use --all to re-read them anyway.")
    if flagged:
        print(f"⚠ {len(set(flagged))} IPO(s) flagged and NOT written: "
              f"{', '.join(sorted(set(flagged)))}")
        print("  Check the source pages and enter those by hand.")
    publish(store.load_all())
    print("Saved to the sheet — the site shows it on the next reload.")
    return 0


def cmd_analyse(args) -> int:
    gem = Gemini(model=args.model)
    ipo = store.load(args.slug)
    d = derive(ipo)
    context = {
        "company": ipo.company,
        "sector": ipo.sector,
        "board": ipo.board,
        "issue": {
            "total_cr": d["issue"]["total_cr"],
            "fresh_pct": d["issue"]["fresh_pct"],
            "ofs_pct": d["issue"]["ofs_pct"],
            "price_band": f"₹{ipo.issue.price_low:g}-₹{ipo.issue.price_high:g}",
        },
        "financials": d["financials"],
        "gmp_pct": d["gmp"]["pct"],
        "subscription": d["subscription"].get("total"),
        "notes": ipo.notes,
    }

    # What the company actually does, fetched live rather than stored.
    #
    # Without it the prompt has no business description at all and the overview
    # bullets can only restate the sector. Fetched here instead of being written
    # into the sheet because it is 4KB of prose per IPO that only this command
    # ever reads — storing it would be a second copy of InvestorGain's page,
    # kept in step by nobody. One free keyless call at draft time is cheaper
    # than a column, and always current.
    #
    # Best-effort on purpose: if the desk is unreachable the draft still runs on
    # the financial facts, which is how it behaved before this existed. A
    # thinner scene beats no scene.
    brief = {}
    try:
        from .providers import investorgain as _ig
        found = _ig.resolve(args.slug, ipo.company or "")
        if found:
            brief = _ig.company_brief(found)
    except Exception as exc:
        print(f"  · no company brief ({type(exc).__name__}) — "
              f"drafting from the numbers only.")
    # The company-profile strip, copied straight through rather than drafted.
    #
    # These four are facts with one correct form — a promoter's name, a city, a
    # founding year — so sending them through the model would only add a chance
    # of it rewriting one. `incorporated` is the sparsest (16 of 21 rows carry
    # it) and every entry is skipped when absent, so the strip shrinks rather
    # than printing "Founded: —".
    # No Industry row: the scene already prints `ipo.sector` under the company
    # name, and a second sector line two blocks below it is the same fact twice.
    # `brief["industry"]` still reaches the prompt, where it is useful context.
    about_facts: list[str] = []
    if brief:
        for label, key in (("Founded", "incorporated"), ("HQ", "hq"),
                           ("Promoters", "promoters")):
            val = str(brief.get(key) or "").strip()
            if val:
                about_facts.append(f"{label}: {val}")

    if brief:
        context["business"] = brief
        print(f"  · company brief from investorgain: "
              f"{', '.join(sorted(brief))}")
        if about_facts:
            print(f"  · {len(about_facts)} profile fact(s): "
                  f"{'; '.join(f.split(':')[0] for f in about_facts)}")
    else:
        print("  ! no company brief — the overview can only describe the "
              "sector and the offer. Check the IPO is on investorgain.")

    try:
        draft = gem.draft_analysis(context, force=args.force)
    except AiUnavailable as exc:
        print(f"Cannot draft: {exc}")
        return 1

    # Merged in after the draft, not into it: `draft` is what the model said and
    # is cached under the prompt hash, while these are copied facts. Keeping them
    # apart means a re-draft cannot lose the strip and the strip cannot be
    # mistaken for something that needs reviewing.
    if about_facts:
        draft["about_facts"] = about_facts

    print("\n--- DRAFT (review before using) ---")
    for key, val in draft.items():
        print(f"\n{key.upper()}:")
        if isinstance(val, list):
            for item in val:
                print(f"  • {item}")
        else:
            print(f"  {val}")

    if args.write:
        raw = ipo.to_dict()
        for key, val in draft.items():
            if val:
                raw["analysis"][key] = val
        store.save(Ipo.from_dict(raw))
        print(f"\nWritten into the sheet (Lists tab, {args.slug}) "
              f"— read it before publishing.")
        maybe_translate(args.slug, args)      # new prose -> translate now, once
    else:
        print("\n(Not saved. Re-run with --write to put it in the sheet.)")
    return 0


# Which `source` labels on a stored GMP row may be superseded by a better
# desk. Everything absent from this set — "manual" above all — is somebody's
# deliberate correction and is never rewritten by a machine.
SUPERSEDABLE = {"ipoji", "gemini", "investorgain", "research", ""}


def _reachable(url: str) -> bool:
    """Does this page answer? Used before replacing a link a viewer clicks.

    A dead registrar link is worse than a stale one, and the incoming URL is
    not always better: InvestorGain lists Purva Sharegistry's allotment page
    at an address that 404s. Anything other than a clean answer keeps what we
    already have.
    """
    try:
        import requests
        return requests.get(
            url, timeout=10, allow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"}).status_code == 200
    except Exception:
        return False


def _gmp_source(preferred: str = "auto"):
    """The GMP desk to read, and the module that reads it.

    InvestorGain is the desk this channel actually quotes, so it answers
    first; ipoji stays wired in behind it for the one case InvestorGain
    cannot cover, which is being unreachable. The fallback is on the *board*
    being empty rather than on any single IPO missing, because a desk that
    answers at all is a desk whose silence about one issue is information.

    Returns (module, name). Falls through to ipoji only from "auto".
    """
    from .providers import investorgain, ipoji

    if preferred == "ipoji":
        return ipoji, "ipoji"
    rows = investorgain.board()
    if rows:
        return investorgain, "investorgain"
    if preferred == "investorgain":
        return investorgain, "investorgain"      # asked for it; report its silence
    print("  ! investorgain returned no rows — falling back to ipoji.")
    return ipoji, "ipoji"


def cmd_gmp_sync(args) -> int:
    """Batched wrapper. See _cmd_gmp_sync_body for what this actually does.

    Every save inside one run collapses into a single sheet write. Without
    this, a loop over 28 IPOs is 56 write requests against a 60-per-minute
    quota — which is how the whole spreadsheet came to be emptied on 4 Sep.
    """
    with store.batched():
        return _cmd_gmp_sync_body(args)


def _cmd_gmp_sync_body(args) -> int:
    """Pull GMP from InvestorGain, with ipoji behind it. Free, keyless, no model.

    Every previous GMP came from a model reading a page, which cost quota,
    needed vetting, and returned nothing at all once the site started
    blocking. Both of these serve the figures as numbers over plain HTTP.

    The two are not interchangeable and the order is the point. InvestorGain
    is the desk the channel checks by hand; ipoji is a different dealer
    network that quotes consistently lower — it had Skyways at 24 on 16 Aug
    where InvestorGain had 28. Publishing whichever answered first would make
    the trail on reel 2 a blend of two markets.

    `--history` additionally walks each IPO's dated table, which is what makes
    a missed day recoverable: the days lost to the CI bug in early August were
    written off as gone forever because the old source published only today.
    InvestorGain carries no premium on its board at all, only the issue, so
    against that source the history walk is not optional and is turned on for
    you rather than silently returning nothing.
    """
    src, src_name = _gmp_source(getattr(args, "source", "auto"))

    # Rewriting stored readings is the one thing in this command that can lose
    # something. Take the snapshot before the first write, not after.
    if getattr(args, "reconcile", False) and args.write:
        snap = store.backup()
        print(f"snapshot: {snap}" if snap else "snapshot: nothing to back up")

    rows = src.board()
    if not rows:
        print(f"{src_name} returned no rows — the site may be unreachable, "
              "or the markup may have changed.")
        return 1
    print(f"{src_name} board: {len(rows)} IPO(s)")

    # InvestorGain's board carries no GMP, only the issue. Walking the dated
    # table is the only way to get a number out of it, so asking for the board
    # alone would look like a working run that found nothing.
    walk_history = getattr(args, "history", False) or src_name == "investorgain"

    ipos = [store.load(args.slug)] if args.slug else store.load_all()
    slugs = [i.slug for i in ipos]
    companies = {i.slug: (i.company or "") for i in ipos}
    by_slug = {i.slug: i for i in ipos}

    matched: dict[str, dict] = {}
    for row in rows:
        slug = src.match_slug(row["name"], row.get("url") or "", slugs, companies)
        if slug:
            matched[slug] = row
    seen = {matched[s]["name"] for s in matched}
    unmatched = [r for r in rows if r["name"] not in seen]
    print(f"matched {len(matched)} of ours; "
          f"unmatched on {src_name}: "
          f"{', '.join(r['name'] for r in unmatched)[:90] or 'none'}")

    # Discovery from the GMP board, not just NSE.
    #
    # `sync --discover` reads NSE's catalogue, and NSE lists an issue only
    # once it is at or near opening — so a mainboard IPO announced days
    # earlier was invisible to the pipeline until it was almost too late to
    # cover. These boards carry them while they are still upcoming: nine were
    # on ipoji's board and untracked here, three of them mainboard, one
    # already quoting a premium.
    #
    # Opt-in because this board also carries SME issues the channel may not
    # want, and a scaffolded row costs an enrich budget to fill.
    if getattr(args, "discover", False) and not args.slug:
        from .providers.scrape import slugify
        wanted = [r for r in unmatched
                  if not args.mainboard_only or r.get("board") == "mainboard"]
        for row in wanted:
            slug = slugify(row["name"].replace("&amp;", "&"))
            if not slug or slug in set(store.list_slugs()):
                continue
            # The same guard as `sync --discover`, and needed here for the
            # same reason from the other direction: a row reaches this loop
            # only because `match_slug` did not recognise it, and a string
            # matcher's false negative is precisely what a second row for one
            # offer is made of. `ipos` is the store as loaded at the top of
            # this command, which is what the matcher was given too.
            if not getattr(args, "allow_duplicate", False):
                hit = dedupe.collides(row, ipos)
                if hit:
                    print(f"  = {slug} is {hit['slug']} ({hit['company']}) — "
                          f"{hit['why']}, {hit['confidence']}. Not scaffolded. "
                          f"Override with --allow-duplicate.")
                    # File this board row against the existing slug so the GMP
                    # pass below still collects it. Without this the premium
                    # the row was carrying is simply dropped — refusing the
                    # duplicate must not also refuse the data.
                    matched.setdefault(hit["slug"], row)
                    continue
            if args.write:
                store.scaffold(slug, overwrite=True)
                ipo = store.load(slug)
                ipo.company = row["name"].replace("&amp;", "&")
                ipo.board = "SME" if row.get("board") == "sme" else "Mainboard"
                store.save(ipo)
                print(f"  + discovered {slug} ({ipo.company}) [{ipo.board}]")
            else:
                print(f"  · would discover {slug} ({row['name']})")
        if wanted and not args.write:
            print(f"  ({len(wanted)} discoverable — re-run with --write)")
        if args.write and wanted:
            # Reload so the new rows take part in the GMP pass below.
            ipos = store.load_all()
            slugs = [i.slug for i in ipos]
            companies = {i.slug: (i.company or "") for i in ipos}
            by_slug = {i.slug: i for i in ipos}
            for row in rows:
                s = src.match_slug(row["name"], row.get("url") or "", slugs, companies)
                if s:
                    matched[s] = row

    # The board only carries what is live, and an issue drops off it the day
    # it lists — which is exactly when its trail is most worth completing.
    # The catalogue keeps every IPO the site has ever listed addressable by
    # id, so anything of ours the board missed gets one lookup here.
    if hasattr(src, "resolve"):
        for slug in slugs:
            if slug in matched:
                continue
            found = src.resolve(slug, companies.get(slug, ""))
            if found:
                matched[slug] = found
                print(f"  · {slug}: off the board, resolved to "
                      f"{src_name} id {found['id']}")

    today = date.today().isoformat()
    wrote = filled = clashed = redone = logos = 0
    for slug, row in sorted(matched.items()):
        ipo = by_slug[slug]

        # The company logo, stored as a Sources role rather than a new column.
        #
        # `sources` is already a free-form role -> url map with its own tab, so
        # this needs no schema change and no matching edit in data.js — which a
        # new column on the IPOs tab would have required on both sides. The
        # studio reads it as `ipo.sources.logo`.
        #
        # Gap-fill, never overwrite: a hand-pinned logo is someone correcting a
        # wrong or ugly one off the board, and that decision outranks this.
        if row.get("logo") and not (ipo.sources or {}).get("logo"):
            if args.write:
                fresh_ipo = store.load(slug)
                fresh_ipo.sources["logo"] = row["logo"]
                store.save(fresh_ipo)
                by_slug[slug] = ipo = fresh_ipo
            logos += 1
            print(f"  {'+' if args.write else '·'} {slug:<32}logo")

        have = {p.date.isoformat(): p.gmp for p in ipo.gmp_history if p.date}
        source_of = {p.date.isoformat(): (p.source or "manual")
                     for p in ipo.gmp_history if p.date}
        incoming: list[dict] = []

        # ipoji publishes today's premium on the board itself; InvestorGain
        # does not, and leaves both keys absent rather than zero.
        if row.get("gmp") is not None and row.get("has_gmp"):
            incoming.append({"date": today, "gmp": row["gmp"], "source": src_name})
        if walk_history and row.get("url"):
            incoming.extend(src.history(row["url"]))

        # Gap-fill only. A reading already on file was taken live on its own
        # day, from whichever desk was quoting then; this page is a different
        # desk's later record of the same day. Filling a hole is strictly an
        # improvement, replacing a reading is a judgement nobody asked for.
        fresh = [p for p in incoming if p["date"] not in have]
        clash = [p for p in incoming
                 if p["date"] in have and abs(have[p["date"]] - p["gmp"]) > 0.01]

        # --reconcile turns the standing rule off for the days another
        # *machine* filed. The trail is billed as one desk's quote, and it was
        # not: ipoji reads consistently lower than InvestorGain, so a chart
        # mixing them shows movement that is a change of source rather than a
        # change of price. Hand-typed days are never touched — a human who
        # typed a figure was correcting this pipeline, not feeding it.
        if getattr(args, "reconcile", False) and clash:
            redo = [p for p in clash
                    if source_of.get(p["date"]) in SUPERSEDABLE]
            if redo:
                redone += len(redo)
                fresh = fresh + redo
                shown = ", ".join(
                    f"{p['date'][5:]} {have[p['date']]:g}→{p['gmp']:g}"
                    for p in sorted(redo, key=lambda x: x["date"])[:5])
                print(f"  {'~' if args.write else '·'} {slug:<32}"
                      f"{len(redo):>2} rewritten: {shown}")
            held = len(clash) - len(redo)
            if held:
                print(f"    ({held} hand-typed day(s) left alone)")
            clashed += held
        elif clash:
            clashed += len(clash)

        if not fresh:
            continue

        # The upper band rides along in the same GMP response, and without it
        # the implausibility check below is a no-op — an issue whose band we
        # do not know is one where every figure looks equally plausible. So
        # fill it from the source before judging the source's own numbers.
        band = ipo.issue.price_high
        if not band and hasattr(src, "band_high"):
            band = src.band_high(row["url"]) or 0.0
            if band and args.write:
                raw = store.load(slug).to_dict()
                raw.setdefault("issue", {})["price_high"] = band
                store.save(Ipo.from_dict(raw))
                print(f"  · {slug}: price band high ₹{band:g} from {src_name}")

        # Same implausibility bound the model-sourced path uses.
        sane, wild = [], []
        for p in fresh:
            pct = (p["gmp"] / band * 100) if band else 0
            (wild if band and (pct > 150 or pct < -30) else sane).append(p)
        for p in wild:
            print(f"  ! {slug} {p['date']}: ₹{p['gmp']:g} is "
                  f"{p['gmp'] / band * 100:.0f}% of the band — skipped")
        if not sane:
            continue

        if args.write:
            raw = store.load(slug).to_dict()
            raw["gmp_history"] = merge_series(raw.get("gmp_history", []), sane)
            store.save(Ipo.from_dict(raw))
            wrote += 1
        filled += len(sane)
        days = ", ".join(f"{p['date'][5:]}=₹{p['gmp']:g}" for p in sane[:6])
        print(f"  {'+' if args.write else '·'} {slug:<32}{len(sane):>2} day(s): {days}")

    # Subscription, from the same board, for the same reason.
    #
    # `doctor` tells you a missing bidding day "cannot be backfilled: the
    # exchange publishes today's running total, not an archive". That is true
    # of NSE and false of this source — day 1 is still readable on day 3 — so
    # the gaps it calls permanent are only permanent against the old feed.
    # Gap-fill on `day`, same rule as the premium: a figure already on file
    # was taken live and stays — unless --reconcile, which also corrects the
    # days already there.
    #
    # Correcting them matters more than it looks. NSE reports a running total
    # *during* the day and the job reads it at whatever hour it happened to
    # run, so the stored figure is a mid-afternoon snapshot rather than the
    # close: Behari Lal's final day was filed at 108.44x against a settled
    # 118.07x, and Shiprocket at 99.38x against 102.28x. The headline number
    # on reel 3 was wrong on almost every closed issue.
    subs_filled = subs_fixed = 0
    if hasattr(src, "subscription"):
        for slug, row in sorted(matched.items()):
            rows_in = src.subscription(row["url"])
            if not rows_in:
                continue
            have = {s.day: s for s in by_slug[slug].subscription}
            new = [r for r in rows_in if r["day"] not in have]
            fixed = []
            if getattr(args, "reconcile", False):
                for r in rows_in:
                    old = have.get(r["day"])
                    if not old:
                        continue
                    if abs((old.total or 0) - r["total"]) > 0.005:
                        fixed.append(r)
                        continue
                    # A settled total is not the only thing that can be out of
                    # date. The NII split (sHNI / bHNI) was added to the store
                    # after these rows were written, so every existing day has
                    # a correct total and no split at all — and reel 4 cannot
                    # quote an HNI their own allotment odds without it.
                    # Backfill a field the source has and the row does not,
                    # without treating that as a disagreement worth printing
                    # as a correction.
                    if any(r.get(k) and not getattr(old, k, 0)
                           for k in ("nii_small", "nii_big")):
                        fixed.append(r)
            if not new and not fixed:
                continue
            subs_filled += len(new)
            subs_fixed += len(fixed)
            shown = [f"day {r['day']}={r['total']:g}x" for r in new]
            shown += [f"day {r['day']} {have[r['day']].total:g}→{r['total']:g}x"
                      for r in fixed]
            print(f"  {'+' if args.write else '·'} {slug:<32}   "
                  f"{', '.join(shown)}")
            if args.write:
                raw = store.load(slug).to_dict()
                raw["subscription"] = merge_series(
                    raw.get("subscription", []), new + fixed, key="day")
                store.save(Ipo.from_dict(raw))

    # The issue itself: band, size, calendar, registrar, board, sector.
    #
    # `doctor` can derive the T+3 dates from the close date, but derived is a
    # guess and this is the registrar's own published timetable. Filling them
    # here is also a Gemini call `enrich` no longer has to spend.
    #
    # Under --reconcile this corrects what is already there, which is how a
    # revised issue gets picked up at all: Fascinate Textiles was
    # undersubscribed, had its window extended to 19 Aug and its band cut to
    # ₹151, and the sheet still said closed on the 13th at ₹156 — so the card
    # was showing "allotment" for an issue still taking bids.
    terms_filled = terms_fixed = 0
    redo_terms = getattr(args, "reconcile", False)
    if hasattr(src, "allotment"):
        for slug, row in sorted(matched.items()):
            ipo = by_slug[slug]
            info = src.allotment(row["url"])
            want: dict[str, Any] = {}
            dates: dict[str, Any] = {}
            fixed: list[str] = []

            def offer(bucket: dict, key: str, new: Any, current: Any) -> None:
                """Take `new` when the field is blank, or when reconciling."""
                if new in (None, "", [], 0):
                    return
                blank = current in (None, "", [], 0, 0.0)
                if blank:
                    bucket[key] = new
                elif redo_terms and str(current) != str(new):
                    bucket[key] = new
                    fixed.append(key)

            offer(want, "registrar", info.get("registrar"), ipo.issue.registrar)
            offer(want, "price_high", src.band_high(row), ipo.issue.price_high)
            offer(want, "total_cr", info.get("total_cr") or row.get("issue_size_cr"),
                  ipo.issue.total_cr)
            for key in ("allotment", "listing"):
                offer(dates, key, info.get(key), getattr(ipo.dates, key, None))
            for key in ("open", "close"):
                offer(dates, key, row.get(key), getattr(ipo.dates, key, None))

            # The registrar's allotment page is a link a viewer clicks, so a
            # replacement has to be reachable before it is worth having.
            # InvestorGain lists Purva Sharegistry's as a URL that 404s, and
            # both KFin variants are live — so ours is only displaced by one
            # that actually answers.
            new_url = info.get("registrar_url")
            if new_url and new_url != ipo.issue.registrar_url:
                if not ipo.issue.registrar_url or (redo_terms and _reachable(new_url)):
                    offer(want, "registrar_url", new_url, ipo.issue.registrar_url)

            # Board only from the live board. A catalogue row carries no
            # category at all, and treating that absence as "Mainboard" would
            # have relabelled Optimystix, an SME issue.
            board_new = None
            if row.get("board"):
                theirs = "SME" if row["board"] == "sme" else "Mainboard"
                if redo_terms and ipo.board != theirs:
                    board_new = theirs
                    fixed.append("board")

            # Sector is filled, never overwritten. Theirs is a taxonomy label
            # and ours is a description: "Other Consumer Services" is what
            # they call a maker of temperature sensors, and "Other Food
            # Products" a dairy. As reel 1's subtitle the description wins.
            sector_new = (row.get("sector")
                          if row.get("sector") and not ipo.sector else None)

            n = len(want) + len(dates) + bool(board_new) + bool(sector_new)
            if not n:
                continue
            terms_filled += n - len(fixed)
            terms_fixed += len(fixed)
            shown = ", ".join(
                [f"{k}→{v}" if k in fixed else k for k, v in
                 list(want.items()) + list(dates.items())]
                + ([f"board→{board_new}"] if board_new else [])
                + (["sector"] if sector_new else []))
            print(f"  {'+' if args.write else '·'} {slug:<32}   {shown}")
            if args.write:
                raw = store.load(slug).to_dict()
                raw.setdefault("issue", {}).update(want)
                raw.setdefault("dates", {}).update(dates)
                if board_new:
                    raw["board"] = board_new
                if sector_new:
                    raw["sector"] = sector_new
                store.save(Ipo.from_dict(raw))

    print()
    if subs_filled:
        print(f"{subs_filled} bidding day(s) "
              f"{'written' if args.write else 'found'}.")
    if subs_fixed:
        print(f"{subs_fixed} bidding day(s) corrected to the settled figure.")
    if terms_filled:
        print(f"{terms_filled} issue field(s) filled.")
    if terms_fixed:
        print(f"{terms_fixed} issue field(s) corrected.")
    if logos:
        print(f"{logos} company logo(s) "
              f"{'stored' if args.write else 'found'} — the studio puts these "
              f"in the card header on every scene.")
    if redone:
        print(f"{redone} day(s) rewritten to {src_name}'s figure "
              f"{'' if args.write else '(dry run) '}— hand-typed days untouched.")
    print(f"{filled} reading(s) across {wrote} IPO(s) "
          f"{'written' if args.write else 'found (re-run with --write)'}.")
    if clashed:
        # Reported, never silently reconciled — see the module docstring.
        print(f"{clashed} day(s) where {src_name} disagrees with a reading "
              f"already on file; left alone.")
    if args.write and wrote:
        publish(store.load_all())
        print("Saved to the sheet — the site shows it on the next reload.")
    return 0


def enrich_plan(ipo: Ipo) -> list[tuple[str, list[str], int]]:
    """What this IPO still needs, as (label, argv, ai_calls).

    Ordered by dependency, not by importance: issue details name the sector
    and the registrar, the RHP supplies the figures, and `analyse` is last
    because a draft written before the financials land describes an empty
    company. Everything is keyed on what is *absent*, so running this twice
    does nothing the second time.
    """
    f, a = ipo.financials, ipo.analysis
    steps: list[tuple[str, list[str], int]] = []

    if not ipo.sector or not ipo.issue.registrar:
        steps.append(("issue details",
                      ["research", ipo.slug, "--what", "ipo", "--write"], 1))

    # The RHP read is the fallback now, not the first resort. `facts` pulls the
    # same restated statement out of InvestorGain's detail record for free, and
    # runs before this in the daily chain — so by the time enrich looks, the
    # four core series are usually already there and this costs nothing.
    #
    # `pe_peer_avg` is deliberately NOT in the trigger. InvestorGain does not
    # carry a peer average, so including it would make this condition
    # permanently true and spend a Gemini request on every enrich run for every
    # IPO, re-reading a prospectus whose numbers are already stored. Peer P/E
    # stays a gap `doctor` reports and a human types.
    if not (f.revenue and f.ebitda and f.net_worth and f.eps):
        steps.append(("financials from the RHP",
                      ["rhp", ipo.slug, "--write"], 1))

    # An IPO can be known by name and nothing else: ipoji carries mainboard
    # issues days before NSE or Groww publish any terms, so discovery adds a
    # row whose price band, dates and financials are all still empty.
    #
    # `analyse` on that returns an empty draft — correctly, since it may not
    # invent facts — but it costs a call to find out, then marks the step
    # attempted and blocks a retry for a week. On a free tier metered by
    # requests per day, spending one to learn there is nothing to say is the
    # wrong trade. Wait until the issue has terms.
    has_facts = bool(ipo.issue.price_high or f.revenue or ipo.dates.open)
    # `len(a.overview) < OVERVIEW_BULLETS`, not `not a.overview`: every IPO
    # drafted before the count went to 4 has two bullets, and a presence test
    # calls that done. It re-drafts once per IPO and then stops, because the
    # new draft satisfies the same condition that triggered it.
    short_overview = len(a.overview) < OVERVIEW_BULLETS
    if has_facts and (short_overview or not (a.green_flags or a.red_flags)):
        steps.append(("analysis draft",
                      ["analyse", ipo.slug, "--write", "--no-translate"], 1))

    # General awareness, once per IPO and then never again.
    #
    # Presence is the right test here, unlike the overview above: this is the
    # model's own knowledge of a company, and that does not improve between
    # Tuesday and Thursday. `enrich` runs twice a day, so anything re-triggered
    # by a condition it cannot satisfy would burn a request every run — and an
    # obscure SME legitimately returns an empty list, and `not a.background`
    # cannot tell that apart from never having asked. What stops it re-asking
    # twice a day is the attempt log in `_attempts_path`, which holds a tried
    # step back for `--retry-after` days (7 by default) — so an IPO the model
    # knows nothing about costs one request a week, not fourteen. That is a
    # deliberate trickle rather than zero: the filing brief this leans on fills
    # in as an issue approaches, so the same question can start returning
    # something it could not answer the first time.
    #
    # Needs a company name and nothing else, so it does not wait on `has_facts`
    # the way `analyse` does: a freshly discovered row with only a name is
    # exactly when this is worth writing.
    if (ipo.company or "").strip() and not a.background:
        steps.append(("company background",
                      ["research", ipo.slug, "--what", "background",
                       "--write", "--no-translate"], 1))

    # Translation is two calls (hi + te) and only makes sense once there is
    # prose to translate — which the step above may have just created.
    #
    # "Missing" is not the only way a translation is wrong. A re-draft that
    # takes the overview from two bullets to four leaves hi and te present but
    # two lines short, and `not ipo.i18n.get("hi")` calls that translated — so
    # the Hindi and Telugu cuts of reel 1 would keep rendering half the scene
    # the English one shows. Compare the bullet counts instead of testing for
    # presence, which catches both cases with one condition.
    #
    # Checked per list field, not just `overview`: `background` is written by a
    # later step than the draft, so an IPO whose hi/te were translated before it
    # existed has complete-looking translations that are missing a whole scene.
    # Any list field that gets translated belongs in this tuple.
    def out_of_step(lang: str) -> bool:
        got = ipo.i18n.get(lang) or {}
        if not got:
            return True
        return any(len(got.get(key) or []) != len(src)
                   for key, src in (("overview", a.overview),
                                    ("background", a.background)))

    if a.overview and not short_overview and (
            out_of_step("hi") or out_of_step("te")):
        steps.append(("hi / te translation", ["translate", ipo.slug], 2))

    return steps


def _attempts_path() -> Path:
    d = store.BACKEND_ROOT / ".cache"
    d.mkdir(parents=True, exist_ok=True)
    return d / "enrich.json"


def _load_attempts() -> dict[str, dict[str, str]]:
    try:
        return json.loads(_attempts_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _record_attempt(log: dict[str, dict[str, str]], slug: str, label: str) -> None:
    log.setdefault(slug, {})[label] = date.today().isoformat()
    try:
        _attempts_path().write_text(json.dumps(log, indent=1), encoding="utf-8")
    except OSError:
        pass                                  # a lost log costs a retry, not data


def cmd_enrich(args) -> int:
    """Batched wrapper. See _cmd_enrich_body for what this actually does.

    Every save inside one run collapses into a single sheet write. Without
    this, a loop over 28 IPOs is 56 write requests against a 60-per-minute
    quota — which is how the whole spreadsheet came to be emptied on 4 Sep.
    """
    with store.batched():
        return _cmd_enrich_body(args)


def _cmd_enrich_body(args) -> int:
    """Take every IPO from 'discovered' to 'complete', automatically.

    The tools to fill each field already existed; nothing ran them. A newly
    discovered IPO was scaffolded by `sync` and then sat empty until somebody
    typed four commands at it, which is not automation. This is the step that
    was missing from the chain.

    Two things keep it safe to run unattended:

      * **Budget.** Gemini's free tier binds on requests per day, so a run
        that fans out over a dozen IPOs can exhaust the day's quota and leave
        the next job with nothing. `--max-ai` caps the spend and the tail is
        reported, never silently dropped.
      * **Vetting is untouched.** Each step is the same command a human would
        run, dispatched through `main()`, so every guard still applies: a
        flagged GMP is not written, a short financial series is refused, a
        pre-issue EPS is dropped.
    """
    ipos = [store.load(args.slug)] if args.slug else store.load_all()
    plans = [(ipo, enrich_plan(ipo)) for ipo in ipos]
    todo = [(ipo, s) for ipo, steps in plans for s in steps]

    # A step is planned from what is ABSENT, but absence is not always
    # fillable: Dhoot's prospectus prints no peer P/E, so "financials from the
    # RHP" would stay outstanding forever and re-run on every scheduled pass,
    # spending the day's quota on a step that can never finish. Remember the
    # attempt, not just the outcome.
    log = _load_attempts()
    cutoff = date.today().toordinal() - args.retry_after
    if not args.retry:
        held = [(i, s) for i, s in todo
                if date.fromisoformat(
                    log.get(i.slug, {}).get(s[0], "1970-01-01")).toordinal() > cutoff]
        todo = [x for x in todo if x not in held]
        if held:
            print(f"({len(held)} step(s) tried within the last "
                  f"{args.retry_after} day(s) — use --retry to force)")

    if not todo:
        print(f"Nothing to enrich — all {len(ipos)} IPO(s) are complete "
              f"or recently attempted.")
        return 0

    print(f"{len(todo)} step(s) across "
          f"{len({i.slug for i, _ in todo})} IPO(s); "
          f"budget {args.max_ai} AI call(s)\n")

    spent = ran = failed = 0
    skipped: list[str] = []
    failures: list[str] = []
    for ipo, (label, argv, cost) in todo:
        if spent + cost > args.max_ai:
            skipped.append(f"{ipo.slug}: {label}")
            continue
        print(f"── {ipo.slug}: {label}")
        if args.dry_run:
            print(f"   $ ipopulse {' '.join(argv)}")
            spent += cost
            continue
        try:
            rc = main(argv)
        except Exception as exc:                     # one bad IPO must not
            print(f"   ! {type(exc).__name__}: {exc}")   # stop the rest
            rc = 1
        spent += cost
        ran += 1
        # ── record the attempt ONLY when the step actually completed ───────
        #
        # The backoff exists for a real case: Dhoot's prospectus prints no peer
        # P/E, so that step would re-run forever and spend the day's quota
        # learning the same thing. But "ran fine, the field is genuinely not
        # available" and "crashed" are not the same outcome, and recording both
        # meant one bug bought itself a week of silence.
        #
        # That is exactly what happened: draft_analysis hit an
        # AttributeError on every IPO, each crash was filed as an attempt, and
        # `doctor` reported 54 blank fields for a week while `enrich` politely
        # declined to look again. A failure now retries on the next run, where
        # the budget cap bounds the cost and the summary below makes it loud.
        if rc:
            failed += 1
            failures.append(f"{ipo.slug}: {label}  ($ ipopulse {' '.join(argv)})")
            print(f"   ! exited {rc} — will retry next run")
        else:
            _record_attempt(log, ipo.slug, label)

    print()
    if skipped:
        # Never a silent cap: the tail is the whole reason a budget is safe.
        print(f"⏸ {len(skipped)} step(s) left for the next run (budget spent):")
        for s in skipped[:8]:
            print(f"   {s}")
        if len(skipped) > 8:
            print(f"   … and {len(skipped) - 8} more")
    print(f"{ran} step(s) run, {failed} failed, {spent} AI call(s) used.")

    # Loud, and with the command to reproduce it. A step failing the same way
    # on every IPO is a bug in this code rather than a gap in the data, and the
    # only way to tell is to run one by hand and read the traceback — so the
    # summary hands over the exact command.
    if failures:
        print(f"\n✗ {len(failures)} step(s) FAILED and were not recorded, so "
              f"they retry next run.")
        print("  If they keep failing, run one by hand to see the real error:")
        for line in failures[:8]:
            print(f"   {line}")
        if len(failures) > 8:
            print(f"   … and {len(failures) - 8} more")

    if not args.dry_run and ran:
        # doctor picks up what the new figures make derivable — post-issue
        # shares from PAT/EPS, a total from its parts — then republish.
        main(["doctor", "--fix"])
        publish(store.load_all())
        print("Saved to the sheet — the site shows it on the next reload.")
    # 0 by default even with failures, because this runs mid-chain: `doctor`,
    # `build` and `validate` come after it and must still run — a publish
    # blocked by one bad analysis is worse than the missing analysis.
    # --strict is for a human or a gate that wants the non-zero.
    return 1 if (failures and args.strict) else 0


def cmd_facts(args) -> int:
    """Batched wrapper. See _cmd_facts_body for what this actually does.

    Every save inside one run collapses into a single sheet write. Without
    this, a loop over 28 IPOs is 56 write requests against a 60-per-minute
    quota — which is how the whole spreadsheet came to be emptied on 4 Sep.
    """
    with store.batched():
        return _cmd_facts_body(args)


def _cmd_facts_body(args) -> int:
    """Fill the financial statement, the valuation KPIs and the HNI tranche
    minimums from InvestorGain. Free, keyless, no model involved.

    This is the step reel 4 was missing. Its financials and valuation scenes
    were blank for all but three tracked IPOs, and the only filler in the
    pipeline was `rhp` — a Gemini read of a 400-page prospectus PDF that
    usually returned nothing and spent a request finding out. The same
    restated statement is published as a plain HTML table on the detail
    record this project already fetches for the company brief.

    Gap-filling only. A stored value is never overwritten, because the reason
    a figure is on the sheet may be that you corrected it by hand; `--force`
    is the explicit way to say otherwise.
    """
    from .providers import investorgain as ig

    slugs = args.slug or store.list_slugs()
    changed, skipped = [], []

    for slug in slugs:
        ipo = store.load(slug)
        row = ig.resolve(slug, ipo.company or "")
        if not row:
            skipped.append((slug, "not on InvestorGain's board"))
            continue

        raw = ipo.to_dict()
        wrote: list[str] = []

        # ── the statement
        fin = ig.financials(row)
        if fin:
            block = raw.setdefault("financials", {})
            stored_years = [str(y) for y in (block.get("years") or [])]
            # A different set of years is a different statement, not extra
            # columns to merge into this one. Replacing the year axis while
            # keeping a hand-typed series would silently re-label FY23 revenue
            # as FY24, so the two only ever move together.
            fresh_axis = stored_years != fin["years"]
            has_any = any(any(float(v or 0) for v in (block.get(k) or []))
                          for k in ("revenue", "ebitda", "pat", "net_worth"))
            if fresh_axis and has_any and not args.force:
                skipped.append((slug, f"years differ ({', '.join(stored_years)} "
                                      f"vs {', '.join(fin['years'])}) — --force to replace"))
            else:
                if fresh_axis:
                    block["years"] = fin["years"]
                    wrote.append("years")
                for key in ("revenue", "ebitda", "pat", "net_worth", "total_debt"):
                    if key not in fin:
                        continue
                    have = [v for v in (block.get(key) or []) if float(v or 0)]
                    if have and not args.force:
                        continue
                    block[key] = fin[key]
                    wrote.append(key)

        # ── the ratios
        val = ig.valuation(row)
        if val.get("eps") and (not raw.get("financials", {}).get("eps") or args.force):
            raw.setdefault("financials", {})["eps"] = val["eps"]
            wrote.append("eps")

        # ── the lot, and the HNI tranche minimums
        #
        # Lot size belongs here rather than in `sync`: NSE publishes it only
        # once an issue is nearly open, and this desk has it from the day the
        # terms are filed. Without it reels 1, 2 and 4 are all blocked — the
        # minimum investment, the gain per lot and the whole stake scene are
        # `something × lot`, and a zero lot renders them as ₹0, which reads as
        # "no profit" rather than "not known yet".
        cats = ig.categories(row)
        issue = raw.setdefault("issue", {})
        for key in ("lot_size", "min_shni_qty", "min_bhni_qty"):
            if cats.get(key) and (not issue.get(key) or args.force):
                issue[key] = cats[key]
                wrote.append(key)

        # ── the reservation split
        #
        # `categories()` has always returned these and nothing stored them, so
        # every run fetched the reservation block and dropped it. They are the
        # share counts behind reel 1's reservation scene; the percentages are
        # derived in compute, never stored.
        #
        # Keyed by the provider's own names (`shares_qib`) rather than renamed,
        # so a value that looks wrong on the sheet can be traced back to the
        # field it came from without reading this loop.
        for key in ("shares_qib", "shares_nii", "shares_retail",
                    "shares_employee", "shares_shareholders", "shares_total",
                    "shares_anchor"):
            if cats.get(key) and (not issue.get(key) or args.force):
                issue[key] = cats[key]
                wrote.append(key)

        # ── how the issue is sized, from the desk rather than from a model
        #
        # Overwritten when it disagrees, unlike everything else in this
        # command, and the reason is the same as for the ticker: these are
        # published figures with one right answer, and the desk is the
        # authority on them.
        #
        # They used to come from a Gemini lookup and from parsing NSE's
        # prose, which is the inversion of this project's rule — the model
        # fills what InvestorGain does not cover, never the reverse. The cost
        # was a phantom `ofs_cr` of 93 crore pasted across ten unrelated IPOs
        # and a `fresh_cr` of 274.18 across three, with `doctor` then
        # recomputing every total from those two so the contradiction check
        # went green over numbers that were simply wrong.
        #
        # Every change is printed, so an overwrite is auditable rather than
        # silent.
        size = {**ig.issue_size(row), **ig.price_band(row)}
        for key, val in size.items():
            was = issue.get(key)
            if was is not None and abs(float(was or 0) - float(val)) < 0.01:
                continue
            if was:
                print(f"  ~ {slug:<32} {key}: {was} -> {val} (desk)")
            issue[key] = val
            wrote.append(key)

        # ── the timetable, from the same desk
        #
        # Authoritative for the same reason as the band: six rows held a
        # refund date that fell BEFORE their own allotment, which is a
        # calendar that cannot happen. Taking all three from one timetable
        # keeps them consistent by construction rather than by luck.
        info = ig.allotment(row)
        dates = raw.setdefault("dates", {})
        for key in ("allotment", "refund", "listing"):
            val = info.get(key)
            if val and dates.get(key) != val:
                if dates.get(key):
                    print(f"  ~ {slug:<32} {key}: {dates[key]} -> {val} (desk)")
                dates[key] = val
                wrote.append(key)

        # ── the exchange's own name for the issue
        #
        # NSE symbol, BSE scrip code and ISIN, off the same detail record
        # everything above already read — so this costs no extra request.
        #
        # It belongs here and not in `verify` because `verify` stamps
        # whichever exchange happened to answer first, as ONE value: the two
        # Rays of Belief rows carried `BSE:4775` and `NSE:MOMSBELIEF`, which
        # is why comparing that field could not tell they were one company.
        # This desk gives the same symbol for both. See `dedupe.signature`,
        # which now reads these before it looks at any name.
        #
        # Overwritten even when present, unlike everything else in this
        # command, and deliberately: a ticker is not a judgement somebody
        # might have corrected by hand, it is a fact with one right answer,
        # and a stale one is worse than none — it would key the duplicate
        # check to a company this row is not.
        ident = ig.identity(row)
        if ident:
            src = raw.setdefault("sources", {})
            for key, val in ident.items():
                if src.get(key) != val:
                    src[key] = val
                    wrote.append(key)

        if not wrote:
            continue
        changed.append((slug, wrote))
        if not args.dry_run:
            store.save(Ipo.from_dict(raw))

    for slug, wrote in changed:
        print(f"  {slug:<34} {', '.join(wrote)}")
    for slug, why in skipped:
        print(f"  {slug:<34} — {why}")
    verb = "would fill" if args.dry_run else "filled"
    print(f"\n{verb} {len(changed)} of {len(slugs)} IPO(s). "
          f"Free and keyless — no Gemini request was spent.")
    return 0


def cmd_validate(args) -> int:
    """Is every record internally consistent, and which reels can be shot?

    `doctor` asks what is absent and `grade` asks whether the numbers match
    InvestorGain. This asks the two questions neither covers: does the record
    contradict itself, and — given the calendar and the clock — is reel N
    recordable right now or has its window shut.
    """
    from . import readiness
    from .compute import derive

    now = datetime.now()
    ipos = [store.load(s) for s in (args.slug or store.list_slugs())]
    reports = [readiness.report(i, derive(i, now), now) for i in ipos]

    if args.json:
        print(json.dumps(reports, indent=1, default=str))
        return 0

    # Live issues first: a window that shuts tonight is the only thing on this
    # page anybody needs to act on today.
    rank = {"open": 0, "upcoming": 1, "closed": 2, "allotment": 3, "listed": 4}
    reports.sort(key=lambda r: (rank.get(r["status"], 9), -r["ready_count"]))

    glyph = {"ready": "●", "partial": "◐", "blocked": "✗",
             "early": "·", "expired": "·"}
    print(f"{'IPO':<34}{'STATUS':<11}{'1':^3}{'2':^3}{'3':^3}{'4':^3}"
          f"{'5':^3}{'6':^3}  READY  ISSUES")
    print("-" * 96)
    for r in reports:
        dots = "".join(f"{glyph[r['reels'][n]['state']]:^3}" for n in range(1, 7))
        errs = r["errors"]
        note = f"{errs} error(s)" if errs else (
            f"{len(r['problems'])} warning(s)" if r["problems"] else "")
        print(f"{r['company'][:33]:<34}{r['status']:<11}{dots}  "
              f"{r['ready_count']}/6    {note}")

    print("\n  ● ready   ◐ recordable but thin or stale   "
          "✗ a required field is missing   · outside its window")

    for r in reports:
        detail_lines: list[str] = []
        for n in range(1, 7):
            rs = r["reels"][n]
            if rs["state"] == "blocked":
                detail_lines.append(f"    reel {n}  missing: {', '.join(rs['missing'])}")
            elif rs["state"] == "partial" and args.verbose:
                bits = rs["soft"] + [f"{k} is stale" for k in rs["stale"]]
                detail_lines.append(f"    reel {n}  thin: {', '.join(bits)}")
            elif rs["state"] == "expired" and args.verbose:
                detail_lines.append(f"    reel {n}  window shut — {rs['window']['ends']}")
        for p in r["problems"]:
            mark = "!!" if p["severity"] == "error" else " !"
            detail_lines.append(f"   {mark} {p['what']}: {p['detail']}")
        if detail_lines:
            print(f"\n── {r['company']}  ({r['slug']})")
            print("\n".join(detail_lines))

    total_err = sum(r["errors"] for r in reports)
    if total_err:
        print(f"\n{total_err} contradiction(s) that would render as a "
              f"confident wrong number.")
    return 1 if (total_err and args.strict) else 0


def cmd_market(args) -> int:
    """Build (and store) the daily pre-market briefing — reel 7's record.

    Dry run unless `--write`, like every other command here that touches the
    sheet. Unlike them, the dry run is genuinely useful on its own: the whole
    briefing prints, so the morning's read can be checked before anything is
    committed to a row a reel will be recorded from.
    """
    from . import briefing, outlook
    from .providers import market as mkt

    day = args.day or briefing.today()

    if args.show:
        try:
            b = briefing.load(day)
        except FileNotFoundError as exc:
            print(f"{exc}")
            return 1
        _print_briefing(b)
        return 0

    session = mkt.trading_day(date.fromisoformat(day))
    if not session["trading"] and not args.force:
        # Not an error: a briefing for a day with no session is a page of
        # yesterday's numbers presented as this morning's, which is the exact
        # failure the watchdog exists to catch on the IPO side.
        print(f"{day} is not a trading day ({session['why']}) — nothing to "
              f"brief. Use --force to build one anyway.")
        return 0

    if briefing.exists(day) and not args.replace:
        print(f"{day} already has a briefing. `--show` to read it, "
              f"`--replace --write` to rebuild it.")
        return 1

    try:
        b = outlook.build(day=day, model=args.model, verbose=True)
    except AiUnavailable as exc:
        print(f"  ! {exc}")
        return 1
    except RuntimeError as exc:
        print(f"  ! {exc}")
        return 1

    print()
    _print_briefing(b)

    if not args.write:
        print("\n(dry run — nothing written. Re-run with --write)")
        return 0

    try:
        where = briefing.save(b, replace=args.replace)
    except (FileExistsError, RuntimeError) as exc:
        print(f"  ! {exc}")
        return 1
    print(f"\nWritten to {where}")
    print("Next:  ipopulse build")
    return 0


def _print_briefing(b) -> None:
    """One briefing, as the morning read it should be checked against."""
    arrow = {"up": "▲", "down": "▼"}.get(b.bias, "▬")
    print(f"── {b.date} {arrow} {b.bias.upper()}"
          + ("" if b.trading else f"  [CLOSED: {b.why_closed}]"))
    print(f"   NIFTY {b.nifty:,.2f} ({b.nifty_pct:+.2f}%)   "
          f"BANK NIFTY {b.banknifty:,.2f} ({b.banknifty_pct:+.2f}%)   "
          f"breadth {b.advances}/{b.declines}")
    if b.partial:
        print(f"   ! partial data: {b.partial}")
    if b.outlook:
        print(f"\n   {b.outlook}")

    if b.news:
        print("\n   OVERNIGHT")
        for n in b.news:
            tick = f"  [{', '.join(n.tickers)}]" if n.tickers else ""
            pic = "" if n.image else "  (no image yet)"
            print(f"    {n.idx}. {n.headline}{tick}{pic}")
            if n.why:
                print(f"       {n.why}")
            print(f"       {n.at[11:16] if n.at else '':5}  {n.source[:52]}")

    if b.sectors:
        strong = [s for s in b.sectors if s.stance == "strong"]
        weak = [s for s in b.sectors if s.stance == "weak"]
        fmt = lambda rows: ", ".join(
            f"{s.sector.replace('NIFTY ', '')} {s.pct:+.2f}%" for s in rows)
        print(f"\n   STRONG  {fmt(strong) or '—'}")
        print(f"   WEAK    {fmt(weak[::-1]) or '—'}")

    for side, label in (("long", "LONG SETUPS"), ("short", "SHORT SETUPS")):
        rows = b.side(side)
        if not rows:
            continue
        print(f"\n   {label}")
        for s in rows:
            print(f"    {s.rank}. {s.symbol:<12} entry {s.entry:>10,.2f}  "
                  f"target {s.target:>10,.2f} ({s.reward:+.2f}%)  "
                  f"stop {s.stop:>10,.2f} ({s.risk:.2f}%)  rr {s.rr}")
            if s.reason:
                print(f"       {s.reason}")
            print(f"       voids: {s.invalidates}")

    if b.levels_note:
        print(f"\n   {b.levels_note}")
    if b.notes:
        print(f"   ({b.notes})")
    print(f"   words by {b.model or 'nobody'}")


def cmd_migrate(args) -> int:
    """Rebuild the store in a NEW spreadsheet, verify it, leave the old alone.

    The layout has grown on the fly — reservation columns, the NII split, the
    exchange identifiers, four Market* tabs, and the blank-instead-of-zero
    correction — and every one of those arrived as an append to a live book.
    This is how a corrected layout gets adopted without a moment where the
    only copy is half-written.

    What it does NOT do is switch over. It creates, writes, reads back and
    compares; you change `GOOGLE_SHEETS_ID` when the comparison is clean. The
    old book keeps running until you do, and stays intact afterwards, so
    reverting is editing one variable back.

    The comparison is the whole value of the command. `to_dict()` on every
    record, on both sides, field for field — the same check the round-trip
    test uses, run against a real spreadsheet instead of a dict in memory.
    """
    from . import briefing, sheets as sh

    records = {i.slug: i.to_dict() for i in store.load_all()}
    market = {b.key: b.to_dict() for b in briefing.load_all()}
    if not records:
        print("Nothing to migrate — the store is empty.")
        return 1

    print(f"source : {store.where()}")
    print(f"         {len(records)} IPO(s), {len(market)} briefing(s)")

    # A local snapshot first, and not as a formality: this is the only copy
    # that survives both books being wrong.
    snap = store.backup()
    print(f"backup : {snap}" if snap else "backup : nothing written")

    if args.into:
        book = args.into
        print(f"target : existing book {book[:12]}…")
    else:
        title = args.title or f"IPO Pulse (schema {date.today().isoformat()})"
        if not args.write:
            print(f"target : a new book titled {title!r}")
            print(f"\n(dry run — nothing created. Re-run with --write)")
            return 0
        book = sh.create_book(title)
        print(f"target : created {book}")

    if not args.write:
        print("\n(dry run — nothing written. Re-run with --write)")
        return 0

    sh.mirror_to(book, records, market)
    print(f"wrote  : {len(tables.ALL_TABS)} tab(s)")

    # ── verify, field for field
    got_ipos, got_market = sh.read_book(book)
    problems: list[str] = []

    missing = sorted(set(records) - set(got_ipos))
    extra = sorted(set(got_ipos) - set(records))
    if missing:
        problems.append(f"{len(missing)} IPO(s) did not arrive: "
                        f"{', '.join(missing[:5])}")
    if extra:
        problems.append(f"{len(extra)} unexpected row(s): "
                        f"{', '.join(extra[:5])}")

    for slug in sorted(set(records) & set(got_ipos)):
        before = records[slug]
        after = Ipo.from_dict(dict(got_ipos[slug])).to_dict()
        if before != after:
            diffs = [k for k in set(before) | set(after)
                     if before.get(k) != after.get(k)]
            problems.append(f"{slug} differs on: {', '.join(sorted(diffs))}")

    for day in sorted(set(market) & set(got_market)):
        before = market[day]
        after = Briefing.from_dict(dict(got_market[day])).to_dict()
        if before != after:
            diffs = [k for k in set(before) | set(after)
                     if before.get(k) != after.get(k)]
            problems.append(f"briefing {day} differs on: "
                            f"{', '.join(sorted(diffs))}")
    for day in sorted(set(market) - set(got_market)):
        problems.append(f"briefing {day} did not arrive")

    print(f"verify : {len(got_ipos)} IPO(s), {len(got_market)} briefing(s) "
          f"read back")
    if problems:
        print(f"\n{len(problems)} PROBLEM(S) — do NOT switch over:")
        for line in problems[:20]:
            print(f"  ! {line}")
        print(f"\nThe old book is untouched and still live. The new one is at\n"
              f"  {book}\nLeave GOOGLE_SHEETS_ID alone until this is clean.")
        return 1

    print("         every record matches the source, field for field")
    print(f"\nMigrated cleanly. The new book:\n  {book}")
    print(f"  https://docs.google.com/spreadsheets/d/{book}/edit")
    print("\nTo adopt it:")
    print("  1. Share it — the book belongs to the service account, so it is")
    print("     not in your Drive yet. Give your own account edit access, and")
    print("     set link sharing to 'Anyone with the link can view' or the")
    print("     site cannot read it.")
    print(f"  2. Set GOOGLE_SHEETS_ID={book} in .env")
    print("  3. `ipopulse config` to rewrite frontend/js/config.js, then")
    print("     redeploy the site.")
    print("  4. `ipopulse monitor` and `ipopulse validate` against the new")
    print("     book before recording anything from it.")
    print("\nThe old book is unchanged and still works. Reverting is putting")
    print("the old id back in .env.")
    return 0


def cmd_publish(args) -> int:
    """The approval gate: review what was rendered, then send it to YouTube.

    Four things in one command because they are four steps of one decision:

        --authorise      once, ever. Opens a browser; you click allow.
        (no flags)       show the queue
        --approve ID     you say yes, and choose the visibility
        --upload         send everything approved

    Nothing here can publish on its own. `--upload` only ever touches items a
    person put into `approved`, and `--approve` is the only thing that does
    that. A scheduled run can render all day and reach exactly as far as the
    queue.
    """
    from . import pubqueue as q
    from . import youtube_upload as yt

    # ── one-time consent
    if args.authorise:
        if not yt.configured():
            print("No OAuth client found.\n")
            print("In Google Cloud Console, once:")
            print("  1. Enable the YouTube Data API v3 on a project")
            print("  2. Create credentials → OAuth client ID → "
                  "type 'Desktop app'")
            print("  3. Download the JSON, save it as "
                  "backend/client_secret.json")
            print("\nIt must be a Desktop app client — a Web client refuses "
                  "the loopback redirect this uses.")
            return 1
        try:
            channel = yt.authorise(port=args.port)
        except Exception as exc:
            print(f"  ! {exc}")
            return 1
        print(f"\nAuthorised as: {channel}")
        print("Check that is the right channel. The token is stored in "
              "backend/.cache/ and is all that is needed from now on.")
        return 0

    # ── the review list
    if not (args.approve or args.reject or args.upload):
        rows = q.items()
        if not rows:
            print("The publish queue is empty.")
            print("\nRender something into it:")
            print("  python tools/render.py --slug <slug> --reel 5 "
                  "--lang en --queue")
            return 0
        counts = q.summary()
        print(f"{'ID':<34}{'STATUS':<10}{'LEN':>6}  TITLE")
        print("-" * 100)
        for item in rows:
            mark = {"queued": "·", "approved": "+", "uploaded": "✓",
                    "failed": "!", "rejected": "x"}.get(item["status"], "?")
            print(f"{item['id']:<34}{mark} {item['status']:<8}"
                  f"{item['seconds']:>5.0f}s  {item['title'][:52]}")
            if item["status"] == "uploaded" and item["url"]:
                print(f"{'':36}{item['privacy']}  {item['url']}")
            if item["error"]:
                print(f"{'':36}! {item['error'][:70]}")
        print(f"\n{counts['queued']} awaiting review, "
              f"{counts['approved']} approved and not yet sent, "
              f"{counts['uploaded']} on the channel.")
        if counts["queued"]:
            print("\nApprove one:   ipopulse publish --approve <ID>")
            print("Make it public: ipopulse publish --approve <ID> --public")
            print("Then send:      ipopulse publish --upload")
        if not yt.authorised():
            print("\nNote: not authorised with YouTube yet — "
                  "`ipopulse publish --authorise` once, before --upload.")
        return 0

    # ── decisions
    for ident in (args.reject or []):
        try:
            q.reject(ident, args.why or "rejected by hand")
            print(f"  x {ident} rejected")
        except (KeyError, ValueError) as exc:
            print(f"  ! {exc}")
            return 1

    for ident in (args.approve or []):
        targets = ([i["id"] for i in q.items(q.QUEUED)]
                   if ident == "all" else [ident])
        for one in targets:
            try:
                item = q.approve(one, public=args.public)
            except (KeyError, ValueError) as exc:
                print(f"  ! {exc}")
                return 1
            print(f"  + {one} approved as {item['privacy']}")
    if args.approve and not args.upload:
        print("\nSend them with:  ipopulse publish --upload")

    # ── the upload
    if args.upload:
        ready = q.take_approved()
        if not ready:
            print("Nothing approved is waiting to go.")
            return 0
        # Deliberately skipped for a dry run. Its whole purpose is to show
        # what would go out, and refusing to do that until OAuth is set up
        # would make the safest command the one you cannot run first.
        if not args.dry_run and not yt.authorised():
            print("Not authorised with YouTube. Run "
                  "`ipopulse publish --authorise` once.")
            return 1
        print(f"{len(ready)} video(s) to send.\n")
        sent = 0
        for item in ready:
            size = Path(item["video"]).stat().st_size / 1048576
            print(f"  {item['id']}  ({size:.1f} MB, {item['privacy']})")
            print(f"    {item['title'][:76]}")
            if args.dry_run:
                print("    (dry run — not sent)")
                continue
            try:
                got = yt.upload(
                    Path(item["video"]), title=item["title"],
                    description=item["description"], tags=item["tags"],
                    privacy=item["privacy"],
                    thumbnail=Path(item["thumbnail"])
                    if item["thumbnail"] else None,
                    on_progress=lambda p: print(f"\r    {p}%", end="",
                                                flush=True))
            except Exception as exc:
                print(f"\n    ! failed: {str(exc)[:200]}")
                q.mark_failed(item["id"], str(exc))
                continue
            q.mark_uploaded(item["id"], got["id"], got["url"])
            sent += 1
            print(f"\r    done → {got['url']}")
            if got.get("thumbnail_error"):
                print(f"    ! thumbnail not set: "
                      f"{got['thumbnail_error'][:90]}")
                print("      (a custom thumbnail needs a verified channel; "
                      "the video is up either way)")
        if not args.dry_run:
            print(f"\n{sent} of {len(ready)} sent.")
    return 0


def cmd_brief(args) -> int:
    """Flatten the store into documents a notebook can read.

    Gemini Notebook has no API, so this produces the thing its four doors all
    want — one self-contained document per IPO — and you add it with a drag or
    a paste. `--public` writes into the website instead, which makes it a URL
    the notebook can fetch itself; read `brief.py`'s header before using that,
    because a file beside index.html is NOT behind the password gate.
    """
    from . import brief, briefing

    made: list = []
    if args.market:
        days = [args.market] if args.market != "all" else briefing.list_days()
        for day in days:
            try:
                b = briefing.load(day)
            except FileNotFoundError:
                print(f"  ! no briefing stored for {day}")
                continue
            made.append(brief.write(brief.for_briefing(b),
                                    f"market-{day}.md", public=args.public))
    else:
        slugs = args.slug or store.list_slugs()
        for slug in slugs:
            try:
                ipo = store.load(slug)
            except FileNotFoundError:
                print(f"  ! no such IPO: {slug}")
                continue
            made.append(brief.write(brief.for_ipo(ipo), f"{slug}.md",
                                    public=args.public))

    if not made:
        print("Nothing written.")
        return 1

    total = sum(p.stat().st_size for p in made)
    for p in made:
        print(f"  {p.stat().st_size / 1024:6.1f} KB  {p}")
    print()
    print(f"{len(made)} document(s), {total / 1024:.0f} KB total.")
    print(f"in: {made[0].parent}")

    if args.public:
        print()
        print("These are on the WEBSITE and not behind the password gate.")
        print("Commit and deploy, then add them in Gemini Notebook with")
        print("  Add sources -> Websites -> paste the URL")
    else:
        print()
        print("Add them in Gemini Notebook with")
        print("  Add sources -> Upload files -> select this folder's .md files")
        print("Then Studio -> Audio Overview, which generates in Hindi and")
        print("Telugu as well as English.")
    return 0


def cmd_monitor(args) -> int:
    """Did the data actually arrive today? The watchdog behind the timers."""
    from . import monitor as watch

    r = watch.check()
    if args.json:
        print(json.dumps(r, indent=1, default=str))
    else:
        for line in watch.report(r):
            print(line)
    # Non-zero only under --strict. The scheduled entry passes it, so a run
    # where nothing arrived shows up as a failed task in Task Scheduler rather
    # than as a green tick over an empty sheet.
    return 1 if (r["errors"] and args.strict) else 0


def cmd_grade(args) -> int:
    """Score the stored numbers against InvestorGain. Read-only."""
    from . import grade as grader

    r = grader.collect(days=args.days)
    for line in grader.report(r):
        print(line)
    # Non-zero only under --strict, so the daily chain sees the report without
    # a disagreement stopping the publish behind it — a GMP that moved since
    # the last read is normal drift, not a reason to skip a build.
    #
    # `impossible` is the exception and always fails. Those are not
    # disagreements with a source; they are records that contradict
    # themselves — a price band whose low is above its high, a refund before
    # its own allotment, a book more than 110% reserved. Nothing downstream
    # can render those correctly, so a run that produced one should be visibly
    # red whether or not anyone asked for strictness.
    if r["impossible"]:
        print(f"\n! {len(r['impossible'])} record(s) contradict themselves — "
              f"these cannot render correctly in any reel.")
        return 1
    bad = (r["gmp_bad"] or r["sub_bad"] or r["orphans"] or r["terms_bad"])
    return 1 if (bad and args.strict) else 0


def cmd_videos(args) -> int:
    """What the channel has published, and which tracked IPOs still have no
    video. Keyless — see providers/youtube.py for why no API key is needed."""
    from .providers import youtube

    cid = youtube.channel_id()
    if not cid:
        print("No channel id. Set YOUTUBE_STUDIO_URL in .env to your Studio")
        print("URL (https://studio.youtube.com/channel/UC...) — the id is read")
        print("out of it; the Studio page itself is never fetched.")
        return 1

    name = youtube.channel_name()
    vids = youtube.videos()
    print(f"Channel: {name or '(unnamed)'}  [{cid}]")
    print(f"{len(vids)} recent upload(s) in the public feed\n")
    for v in vids:
        print(f"  {v['published']}  {v['title']}")

    ipos = store.load_all()
    cov = youtube.coverage([i.slug for i in ipos],
                           {i.slug: (i.company or "") for i in ipos})
    missing = [i for i in ipos if not cov.get(i.slug)]
    if vids:
        print()
        for i in ipos:
            if cov.get(i.slug):
                print(f"  ✓ {i.slug:<34}{len(cov[i.slug])} video(s)")
    print(f"\n{len(missing)} tracked IPO(s) with no video yet:")
    for i in missing:
        d = derive(i)["dates"]["status"]
        print(f"    {i.slug:<34}{d}")
    if not vids:
        print("\n(The feed is empty — nothing published yet. It carries roughly")
        print(" the 15 most recent uploads and excludes private/unlisted ones.)")
    return 0


def _audio_base() -> str:
    """Base URL for the pre-rendered narration, ending in '/'.

    The site derives each clip's filename itself (voice.asset_name, mirrored in
    studio.js) so all it needs is where to look.

    IT MUST BE SAME-ORIGIN, and that is not a preference. studio.js playLang
    FETCHES the bytes rather than pointing an <audio> at the URL, because the
    reel is cut to the narration's real decoded duration — and a cross-origin
    fetch needs the server to send Access-Control-Allow-Origin. GitHub Release
    assets send none: verified 30 Aug 2026, both the github.com 302 and the
    release-assets.githubusercontent.com 200 come back with no CORS header at
    all, and OPTIONS on the asset URL is a 404. So a Release URL here means the
    fetch is blocked by the browser and every language on every reel is silent
    — the same trap as the YouTube RSS feed the studio still cannot read.

    Pointing an <audio> straight at the Release would play, but it costs both
    of the things this path exists for: decodeAudioData never sees the bytes so
    the scene holds fall back to the estimate, and capture.js routes narration
    through createMediaElementSource, which outputs SILENCE for a cross-origin
    element the server never granted — a recording that looks right and has no
    voice on it.

    So the clips live on the Release (out of git, see AUDIO_TAG) and publish.yml
    mirrors them into frontend/audio/ on the way to Pages. The site then reads
    them from its own origin and no CORS header is needed from anyone.

    IPOPULSE_AUDIO_BASE overrides — set it to an absolute URL only if that host
    sends `Access-Control-Allow-Origin`. Otherwise it is 'audio/' under Actions
    and '' locally: with no base the page shows the buttons as un-narrated and
    falls back to generating through /api/voice, which is exactly what a machine
    with a backend and a key should do.
    """
    explicit = (os.getenv("IPOPULSE_AUDIO_BASE") or "").strip()
    if explicit:
        return explicit if explicit.endswith("/") else explicit + "/"
    # GITHUB_REPOSITORY is always set by Actions and never locally, so this is
    # "am I building the published site?" without needing new configuration.
    if not (os.getenv("GITHUB_REPOSITORY") or "").strip():
        # Locally, point at frontend/audio/ only if something is actually in it
        # — `ipopulse serve` serves that path, so a local narration run is
        # audible with no configuration. Empty when the directory is empty or
        # absent, which is the honest answer: the buttons read "not narrated
        # yet" and the /api/voice path stays the advice, rather than lighting
        # up a play button that can only 404.
        local = store.BACKEND_ROOT.parent / "frontend" / "audio"
        if local.is_dir() and any(local.glob("*.mp3")):
            return "audio/"
        return ""
    # Relative, so it resolves under whatever path Pages serves the site at —
    # https://<user>.github.io/<repo>/audio/... — with no hostname to keep in
    # step and no CORS in the picture.
    return "audio/"


# The Release these assets hang off. A single rolling tag rather than one per
# day: the filenames already carry a script hash, so old clips are harmless and
# a viewer's URL keeps working after the next run. Twenty-four dated releases a
# month would be noise in the repo's release list for no benefit.
AUDIO_TAG = "audio"


def cmd_narrate(args) -> int:
    """Narrate a whole batch of scripts into Release-ready audio files.

    Input is the JSON that `scripts.js` (driven by Playwright in narrate.yml)
    writes out, because the scripts are assembled in the BROWSER — the voTake*
    functions in frontend/js/output.js — and the sheet holds only the raw data
    they are built from. Reimplementing 1,300 lines of that in Python would put
    the same editorial logic in two places, which is the one thing this repo is
    consistent about not doing. So the browser stays the author and this is the
    consumer.

        { "esds-software-solution": { "1": { "en": "...", "hi": "...", ... } } }

    Files land in out/audio/ named by voice.asset_name, which is what makes them
    findable: the site derives the same name from the script it renders. Nothing
    is written to the sheet — there is no URL to store, only one to compute.

    --budget is not optional politeness. Three open IPOs across six reels and
    three languages measured 35,454 characters, and a free ElevenLabs plan is
    10,000 a month, so an unbounded run is a bill or a hard stop mid-batch. The
    default stops well short and says what it skipped.
    """
    import json as _json
    from . import voice as tts

    try:
        book = _json.loads(Path(args.scripts).read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        print(f"error: could not read {args.scripts}: {err}", file=sys.stderr)
        return 2

    langs = args.langs.split(",") if args.langs else ["en", "hi", "te"]
    only = set(args.slug.split(",")) if args.slug else None
    out_dir = store.BACKEND_ROOT / "out" / "audio"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Plan the whole batch before spending anything, so the character count is
    # a decision rather than a surprise discovered at item 40.
    plan: list[tuple[str, int, str, str]] = []
    for slug, reels in sorted(book.items()):
        if only and slug not in only:
            continue
        for reel, per_lang in sorted(reels.items(), key=lambda kv: int(kv[0])):
            for lang in langs:
                text = (per_lang.get(lang) or "").strip()
                if text:
                    plan.append((slug, int(reel), lang, text))

    if not plan:
        print("Nothing to narrate — no scripts matched.")
        return 0

    total_chars = sum(len(t) for _, _, _, t in plan)
    print(f"{len(plan)} clips, {total_chars:,} characters across "
          f"{len({p[0] for p in plan})} IPO(s), languages: {','.join(langs)}")

    made = skipped = billed = 0
    for slug, reel, lang, text in plan:
        name = tts.asset_name(slug, reel, lang, text)
        dest = out_dir / name
        if dest.exists() and not args.force:
            # Same script, same hash, same file. Nothing to buy.
            skipped += 1
            continue
        if not args.dry_run and billed + len(text) > args.budget:
            print(f"  budget reached ({args.budget:,}) — stopping. "
                  f"{len(plan) - made - skipped} clip(s) not made.")
            break
        if args.dry_run:
            print(f"  would make {name}  ({len(text):,} chars)")
            made += 1
            continue
        try:
            audio, hit, used, fmt = tts.synthesize(text, lang=lang)
        except tts.VoiceError as err:
            # One language failing is not a reason to abandon the rest: a
            # missing Telugu voice id should not cost you English.
            print(f"  ! {slug} r{reel} {lang}: {err}", file=sys.stderr)
            continue
        dest.write_bytes(audio)
        if not hit:
            billed += len(text)
        made += 1
        print(f"  {name}  {len(audio):,} B  via {used}"
              f"{' (cached)' if hit else ''}")

    print(f"\n{made} made, {skipped} already present, "
          f"{billed:,} characters billed.")
    print(f"Files: {out_dir}")
    return 0


def cmd_voice(args) -> int:
    """Narrate a script with ElevenLabs, or list the voices on the account.

    The studio calls /api/voice for this; the command exists because the first
    thing anybody needs is their own voice id, and because when a key is wrong
    a terminal error is worth ten browser ones.
    """
    from . import voice as tts

    if args.voices:
        try:
            found = tts.voices()
        except tts.VoiceError as err:
            print(err, file=sys.stderr)
            return 1
        if not found:
            print("No voices on this account.")
            return 0
        current = tts.voice_id()
        for v in found:
            mark = " <- ELEVENLABS_VOICE_ID" if v["id"] == current else ""
            # `cloned` is the one that matters: the playbook's §3.1 sweet spot
            # is narrating as yourself, and a stock voice presenting as an
            # adviser is the branch with the monetisation risk on it.
            print(f"  {v['id']}  {v['name']:<24}{v['category']}{mark}")
        return 0

    if args.plan:
        order = tts.providers()
        print("providers, in order:")
        for name in order:
            ok = tts.available(name)
            mark = "ready" if ok else "not configured"
            extra = ""
            if name == "elevenlabs" and ok:
                keys = tts.api_keys()
                extra = (f"  {len(keys)} key(s): "
                         f"{', '.join(tts.key_label(k) for k in keys)}")
            if name == "gemini":
                extra = "  free tier is licensed for commercial use"
            print(f"  {name:<12}{mark}{extra}")
        if not any(tts.available(n) for n in order):
            print("\n  Nothing configured. Set GEMINI_API_KEY (free, and its "
                  "output\n  may be used commercially) or ELEVENLABS_API_KEY "
                  "(needs a PAID plan\n  for a monetised channel — the free "
                  "plan grants no commercial licence).")
        b = tts.budget()
        print(f"\nElevenLabs spend {b['used']:,} / {b['cap']:,} characters this "
              f"month ({b['per_key_cap']:,} per key)")
        print()
        print(f"{'lang':<6}{'provider':<13}{'voice':<26}{'model':<30}"
              f"{'limit':>8}  ok")
        rows = tts.plan()
        for code, row in rows.items():
            ok = "yes" if row["speaks"] else "NO"
            cap = f"{row['limit']:,}" if row["limit"] else "—"
            print(f"  {code:<4}{row['provider']:<13}{row['voice'] or '(unset)':<26}"
                  f"{row['model']:<30}{cap:>8}  {ok}")
        bad = [c for c, r in rows.items() if not r["speaks"]]
        if bad:
            print()
            print(f"  {', '.join(bad)} is on a model that cannot speak it.")
            print("  On ElevenLabs, Telugu needs eleven_v3 — multilingual_v2 has")
            print("  Hindi and Tamil but no Telugu. Set ELEVENLABS_MODEL_TE=eleven_v3")
        print()
        print("Compare the providers by ear before committing to one:")
        print("  ipopulse voice --compare --lang te \"ఇది ఒక పరీక్ష\"")
        return 0

    if args.compare:
        text = (args.text or "").strip()
        if args.text_file:
            text = Path(args.text_file).read_text(encoding="utf-8")
        if not text:
            print("Give some text to compare, or --text-file.", file=sys.stderr)
            return 1
        outdir = Path(args.out or "voice-compare")
        outdir.mkdir(parents=True, exist_ok=True)
        lang = args.lang or "en"
        any_ok = False
        # Every provider, same text, same language, side by side. The point is
        # a listen, so the files are named for what made them.
        for name in ("gemini", "elevenlabs"):
            if not tts.available(name):
                print(f"  {name:<12}skipped — not configured")
                continue
            try:
                audio, hit, used, fmt = tts.synthesize(
                    text, lang=lang, provider=name, force=args.force)
            except tts.VoiceError as err:
                print(f"  {name:<12}failed — {err}")
                continue
            dest = outdir / f"{lang}-{name}.{fmt}"
            dest.write_bytes(audio)
            any_ok = True
            print(f"  {name:<12}{'cached' if hit else 'generated'}  "
                  f"{len(audio):,} bytes  ->  {dest}")
        if any_ok:
            print(f"\nListen to both in {outdir}. Things to judge:")
            print("  - number and company-name pronunciation (where TTS breaks)")
            print("  - whether it sounds like a person reading, or a machine")
            print("  - Telugu and Hindi especially: accent and stress")
        return 0 if any_ok else 1

    if args.budget:
        b = tts.budget()
        print(f"{b['used']:,} of {b['cap']:,} characters used this month "
              f"({b['left']:,} left) across {b['keys']} key(s).")
        for k in tts.api_keys():
            per = tts.budget(k)
            print(f"  {tts.key_label(k)}  {per['used']:,} / {per['cap']:,}")
        print("Local and advisory — it counts what this machine sent, not your "
              "real balance. ELEVENLABS_MONTHLY_CHAR_CAP is the per-key cap.")
        return 0

    if args.text_file:
        text = Path(args.text_file).read_text(encoding="utf-8")
    elif args.text:
        text = args.text
    else:
        text = sys.stdin.read()

    try:
        audio, hit, used, fmt = tts.synthesize(
            text, vid=args.voice or "", lang=args.lang or "",
            provider=args.provider or "", force=args.force)
    except tts.VoiceError as err:
        print(err, file=sys.stderr)
        return 1

    if args.out:
        out = Path(args.out)
        # An explicit --out with the wrong extension would lie about the
        # contents: Gemini returns wav and ElevenLabs mp3.
        if out.suffix.lstrip(".").lower() != fmt:
            out = out.with_suffix(f".{fmt}")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(audio)
    else:
        out = tts.cached_path(text.strip(), lang=args.lang or "",
                              provider=used).with_suffix(f".{fmt}")

    print(f"{'cached' if hit else 'generated'}  via {used}  "
          f"{len(audio):,} bytes  {fmt}  ->  {out}")
    if not hit and used == "elevenlabs":
        print(f"{len(text.strip())} characters billed, "
              f"{tts.budget()['left']:,} left this month.")
    elif not hit:
        print("Free tier — nothing billed. Note that unpaid Gemini quota means "
              "the prompt and output may be used to improve Google's products.")
    return 0


def cmd_verify(args) -> int:
    """Does every tracked IPO actually exist? Ask NSE and BSE.

    Separate from `doctor` on purpose. Doctor answers "what is missing from
    this record"; this answers "should this record be here at all", and the
    second question is the one nothing was asking. The sheet carried Meridian
    Logistics as a ₹720 crore mainboard issue open for bidding, and every
    check in the pipeline was busy grading how complete its data was.
    """
    from . import roster

    ipos = store.load_all()
    rows = roster.check(ipos)
    by_slug = {i.slug: i for i in ipos}

    reachable = rows[0]["reachable"] if rows else []
    print(f"Exchange feeds answering: {', '.join(reachable) or 'NONE'}")
    if not reachable:
        print("\nNeither NSE nor BSE answered, so nothing was verified. This is")
        print("not a finding about the data — try again when the feeds are up.")
        return 0 if not args.strict else 1

    order = {"suspect": 0, "unchecked": 1, "corroborated": 2, "confirmed": 3}
    stamped = 0
    for r in sorted(rows, key=lambda x: (order.get(x["verdict"], 9), x["slug"])):
        mark = {"suspect": "!", "unchecked": "?",
                "corroborated": "·", "confirmed": "+"}[r["verdict"]]
        print(f"  {mark} {r['verdict']:<13}{r['slug']:<34}{r['why']}")

        # Stamp a fresh confirmation so it survives the issue listing and
        # dropping off both feeds. Only when the exchange answered *this* run
        # — re-writing an existing stamp would be a no-op write per IPO.
        if args.write and r["stamp"] and not (by_slug[r["slug"]].sources or {}).get("exchange"):
            fresh = store.load(r["slug"])
            fresh.sources["exchange"] = r["stamp"]
            store.save(fresh)
            stamped += 1

    suspects = [r for r in rows if r["verdict"] == "suspect"]
    print()
    if stamped:
        print(f"{stamped} exchange confirmation(s) stamped onto the sheet.")
    if suspects:
        print(f"! {len(suspects)} IPO(s) no exchange can account for:")
        for r in suspects:
            print(f"    {r['slug']:<34}{r['company']}")
        print("  Check by hand before recording anything about them. If the")
        print("  company is not real:  ipopulse remove <slug> --yes")
    else:
        print("Every tracked IPO is accounted for by an exchange or by "
              "InvestorGain's catalogue.")
    return 1 if (suspects and args.strict) else 0


def cmd_doctor(args) -> int:
    """What is missing, what it breaks, and what can be repaired from here."""
    from . import doctor

    ipos = [store.load(args.slug)] if args.slug else store.load_all()
    if not ipos:
        print("Nothing to check.")
        return 1

    total_blank = fixed_total = 0
    for ipo in ipos:
        rep = doctor.inspect(ipo)
        gaps = rep["gmp_gaps"]
        clean = (not rep["missing"] and not gaps and not rep["gmp_stale"]
                 and not rep["inconsistent"] and not rep["sub_gaps"])

        print(f"\n── {rep['company']}  ({ipo.slug})")
        if clean:
            print("  ✓ complete")
            continue

        for m in rep["blank"]:
            print(f"  ✗ {m['field']:<24} blank → {m['breaks']}")
        for m in rep["missing"]:
            if m["severity"] != "blank":
                print(f"  · {m['field']:<24} → {m['breaks']}")
        total_blank += len(rep["blank"])

        for bad in rep["inconsistent"]:
            print(f"  ‼ {bad}")

        if rep["gmp_stale"]:
            n = rep["gmp_age_days"]
            print(f"  ⚠ GMP is {n} day(s) old — the card labels it "
                  f"\"today's GMP\" until this is refreshed")

        if rep["sub_gaps"]:
            days = ", ".join(f"day {d}" for d in rep["sub_gaps"][:6])
            print(f"  ⚠ subscription is missing {days} — reel 3 is headed "
                  f"'day-wise' and will jump straight over them")
            print("    The exchange publishes today's running total, not an "
                  "archive, so these")
            print("    cannot be backfilled: enter them by hand with "
                  f"`ipopulse sub {ipo.slug} <day> …` or accept the gap.")

        if gaps:
            shown = ", ".join(gaps[:6]) + (f" … +{len(gaps) - 6}" if len(gaps) > 6 else "")
            print(f"  ⚠ GMP trail has {len(gaps)} missing day(s): {shown}")
            print("    The reel 2 trail is billed as daily, so a gap reads as "
                  "'no movement'.")
            print(f"    Backfill all:  ipopulse research {ipo.slug} --what gmp-history --write")
            print(f"    Or by hand  :  ipopulse gmp {ipo.slug} <value> --date {gaps[0]}")

        if args.fix:
            updated, done = doctor.repair(ipo)
            if done:
                store.save(updated)
                fixed_total += len(done)
                for what in done:
                    print(f"  ✓ fixed: {what}")
        elif rep["repairs"]:
            for what in rep["repairs"]:
                print(f"  → repairable: {what}")

        # Only name a filler once per IPO, not once per missing field.
        for who in dict.fromkeys(m["who"] for m in rep["missing"]):
            print(f"    {who:<9} {doctor.FILLERS[who]}")

    print()
    if args.fix:
        print(f"Repaired {fixed_total} field(s) across {len(ipos)} IPO(s).")
        if fixed_total:
            publish(store.load_all())
            print("Saved to the sheet — the site shows it on the next reload.")
    else:
        print(f"{total_blank} field(s) would render a scene blank or as a "
              f"confident zero.")
        print("Repair what is derivable:  ipopulse doctor --fix")

    # Findings are the normal state of a live IPO — an exit code of 1 here
    # would make the daily chain stop on an IPO that simply has no financials
    # yet. --strict is for a pre-recording gate, where blanks matter.
    return 1 if (args.strict and total_blank) else 0


def cmd_build(args) -> int:
    ipos = store.load_all()
    if not ipos:
        print("Nothing to build. Create an IPO first:  ipopulse new <slug>")
        return 1
    if getattr(args, "prune_cache", False):
        removed, kept = Gemini(cache_days=args.days).prune_cache(args.days)
        print(f"Cache: pruned {removed}, kept {kept}")
    broken = verify(ipos)
    print(f"Checked {len(ipos)} IPO(s) in {store.where()}")
    if broken:
        print(f"\n{len(broken)} record(s) the site cannot render:")
        for line in broken:
            print(f"  ! {line}")
        return 1
    print("Every record derives cleanly — the site can read this sheet.")
    # Nothing to commit any more: the site reads the sheet directly, so a
    # write is live the moment it lands. No deploy sits between them.
    print("\nThe site reads this sheet directly — the change is already live.")
    return 0


def cmd_report(args) -> int:
    ipos = [store.load(args.slug)] if args.slug else store.load_all()
    if not ipos:
        print("Nothing to report on.")
        return 1
    name = args.output or (
        f"ipo-pulse-{args.slug}.xlsx" if args.slug else "ipo-pulse-all.xlsx"
    )
    path = write_report(ipos, store.OUT_DIR / name, board=not args.slug)
    print(f"Wrote {path}")
    return 0


def write_frontend_config() -> Path:
    """Generate frontend/js/config.js — the two addresses the page needs.

    Neither is committed. Both live in .env locally and in GitHub secrets for
    CI, and this writes the file the page reads, which is gitignored — so a
    fork points at its own sheet and its own API without editing a tracked
    file.

        SHEET_ID   which spreadsheet to read
        API_BASE   where the hosted trigger API is, if there is one

    API_BASE empty is the normal case: no hosted backend, and the run panel
    falls back to dispatching GitHub Actions.

    Read from IPOPULSE_TRIGGER_API, deliberately NOT IPOPULSE_API_BASE —
    that one already names the future IPO *data source* in providers/api.py,
    and pointing it at a trigger endpoint would quietly break that provider.
    """
    from . import sheets
    sid = sheets.sheet_id()
    api = (os.getenv("IPOPULSE_TRIGGER_API") or "").strip().rstrip("/")
    # Where the pre-rendered narration lives; see _audio_base.
    audio = _audio_base()
    gate_hash, gate_iter = _site_gate(sid)
    pat = _sealed_pat()
    dest = store.BACKEND_ROOT.parent / "frontend" / "js" / "config.js"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        '/* GENERATED — do not edit, do not commit.\n'
        ' * Written from GOOGLE_SHEETS_ID and IPOPULSE_API_BASE by\n'
        ' * `ipopulse serve` / `ipopulse config`, and by the publish workflow.\n'
        ' *\n'
        ' * The sheet must be shared as "Anyone with the link can view" or the\n'
        ' * site cannot read it. API_BASE, when set, must be an https:// origin\n'
        ' * that lists this site in IPOPULSE_ALLOWED_ORIGINS.\n'
        ' *\n'
        ' * SITE_GATE_HASH is PBKDF2-SHA256 over IPOPULSE_TRIGGER_PASSWORD,\n'
        ' * salted with the sheet id. The password itself is never written here.\n'
        ' * Read js/gate.js before relying on it — it says plainly what this\n'
        ' * does and does not protect.\n'
        ' *\n'
        ' * GH_PAT_* is the Actions-dispatch token, AES-GCM sealed under that\n'
        ' * same password with an independent random salt. Ciphertext only, so\n'
        ' * it is safe to publish and the leak scan in publish.yml has nothing\n'
        ' * to trip on. Empty unless GH_DISPATCH_PAT was set at build time.\n'
        ' * See _sealed_pat() for why that salt must NOT be the sheet id.\n'
        ' */\n'
        f'const SHEET_ID = "{sid}";\n'
        f'const API_BASE = "{api}";\n'
        f'const AUDIO_BASE = "{audio}";\n'
        f'const SITE_GATE_HASH = "{gate_hash}";\n'
        f'const SITE_GATE_ITER = {gate_iter};\n'
        f'const GH_PAT_CIPHER = "{pat["cipher"]}";\n'
        f'const GH_PAT_SALT = "{pat["salt"]}";\n'
        f'const GH_PAT_IV = "{pat["iv"]}";\n',
        encoding="utf-8")
    return dest


# Deliberately high: this hash ships to every visitor, so the only thing
# standing between a leaked config.js and the password is how long each guess
# costs. 310k is the OWASP figure for PBKDF2-SHA256 and lands around 150-300ms
# in a browser — unnoticeable once, ruinous a billion times.
GATE_ITERATIONS = 310_000


def _site_gate(sheet_id: str) -> tuple[str, int]:
    """Hash IPOPULSE_TRIGGER_PASSWORD for the front-door gate.

    Returns ("", 0) when no password is set, which leaves the gate off. That
    is safe locally and is NOT safe on a published site, so `gate.js` refuses
    to open on any host other than localhost when the hash is empty — a
    missing secret in CI must fail closed, not quietly publish the studio.

    The salt is the sheet id: stable across regenerations (so a redeploy does
    not invalidate anything), unique to this deployment (so no shared rainbow
    table helps), and not secret, which is all a salt has to be.
    """
    password = (os.getenv("IPOPULSE_TRIGGER_PASSWORD") or "").strip()
    if not password or not sheet_id:
        return "", 0
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), sheet_id.encode("utf-8"),
        GATE_ITERATIONS, dklen=32)
    return digest.hex(), GATE_ITERATIONS


EMPTY_PAT = {"cipher": "", "salt": "", "iv": ""}


def _sealed_pat() -> dict[str, str]:
    """Seal GH_DISPATCH_PAT so config.js can carry it in public.

    The problem this solves, stated exactly, because the obvious shortcut is
    wrong and looks right:

    The site gate ships a one-way HASH of the password, which is safe to
    publish precisely because nobody can turn it back into the password. A
    GitHub token cannot work that way. The browser does not *check* it, it
    *replays* it to api.github.com — so the original bytes have to come back,
    and anything config.js can recover on its own, a visitor can recover too.
    Writing the token in straight from a secret would publish it verbatim from
    a public repo. (The leak scan in publish.yml greps frontend/ for
    `github_pat_` and refuses to deploy, so that mistake fails loudly rather
    than quietly — worth keeping that scan exactly as it is.)

    So the token is encrypted under the password the owner already types at the
    front door: AES-256-GCM, key from PBKDF2-SHA256. Public ciphertext,
    recoverable only by someone who supplies the password.

    **The salt is random per build and NOT the sheet id, and that is the whole
    security of this.** `_site_gate` publishes PBKDF2(password, salt=sheet id)
    as the gate hash. Deriving the AES key the same way would make that
    published hash *be* the key — the ciphertext would decrypt with a value
    printed two lines above it in the same file. An independent salt breaks the
    link: the gate hash then says nothing at all about the key.

    What this does NOT do, so nobody over-trusts it: the password is now worth
    more than it was. Someone who learns it gets the studio *and* a token that
    can run Actions on this repo, and they can attack it offline from the
    published ciphertext at PBKDF2 cost per guess. So the password must be long
    and random, and the token scoped to Actions:write on this one repo with a
    short expiry — then the worst case stays "can run the same jobs the cron
    already runs".

    Returns empty strings when either half is unset, which leaves the feature
    off and manual paste as the studio's only route.
    """
    password = (os.getenv("IPOPULSE_TRIGGER_PASSWORD") or "").strip()
    token = (os.getenv("GH_DISPATCH_PAT") or "").strip()
    if not password or not token:
        return dict(EMPTY_PAT)

    import base64
    import secrets as randbytes

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    salt = randbytes.token_bytes(16)
    iv = randbytes.token_bytes(12)         # 96 bits, the standard GCM nonce
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                              GATE_ITERATIONS, dklen=32)
    sealed = AESGCM(key).encrypt(iv, token.encode("utf-8"), None)

    def b64(raw: bytes) -> str:
        return base64.b64encode(raw).decode("ascii")

    return {"cipher": b64(sealed), "salt": b64(salt), "iv": b64(iv)}


def cmd_config(args) -> int:
    from . import sheets
    dest = write_frontend_config()
    if not sheets.configured():
        print("warning: GOOGLE_SHEETS_ID is empty — the site will load with "
              "no data.", file=sys.stderr)
        print(f"Wrote {dest} (empty id)")
        return 1
    print(f"Wrote {dest}")
    return 0


def cmd_serve(args) -> int:
    import functools
    import http.server
    import socketserver

    root = store.BACKEND_ROOT.parent / "frontend"
    # The page needs the sheet id and the file is gitignored, so a fresh
    # clone would otherwise serve a site that loads nothing.
    write_frontend_config()

    from . import control

    class Handler(http.server.SimpleHTTPRequestHandler):
        # The studio is edited constantly; a cached stylesheet after an edit is
        # a confusing five minutes. Never cache in the dev server.
        def end_headers(self):
            self.send_header("Cache-Control", "no-store, must-revalidate")
            super().end_headers()

        def do_GET(self):
            if not control.handle(self, "GET"):
                super().do_GET()

        def do_POST(self):
            if not control.handle(self, "POST"):
                self.send_error(405)

        def do_OPTIONS(self):
            # CORS preflight. SimpleHTTPRequestHandler has no OPTIONS at all,
            # so without this a cross-origin POST fails before it is sent.
            if not control.handle(self, "OPTIONS"):
                self.send_error(405)

        def log_message(self, fmt, *a):      # quieter console
            pass

    # Threaded: a job run holds its request open while it streams, and the
    # single-threaded server would block every other call — including the
    # status polling that draws the log — until it finished.
    class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
        daemon_threads = True
        allow_reuse_address = True

    handler = functools.partial(Handler, directory=str(root))
    # $PORT is how every cloud host tells a container where to listen, and it
    # is assigned at boot rather than chosen — so it has to win over the
    # default. An explicit --port still beats it.
    port = args.port if args.port != DEFAULT_PORT else int(
        os.getenv("PORT") or os.getenv("IPOPULSE_PORT") or DEFAULT_PORT)
    host = args.host or os.getenv("IPOPULSE_HOST") or "127.0.0.1"

    # Binding beyond loopback exposes /trigger to the network. Without a
    # password that is a remote command runner, so refuse rather than warn.
    local = host in ("127.0.0.1", "localhost", "::1")
    if not local and not control.AUTH.configured():
        print(f"error: refusing to bind {host} with no IPOPULSE_TRIGGER_PASSWORD "
              "set — /trigger would run jobs for anyone who can reach this port.",
              file=sys.stderr)
        print("Set one in .env, or serve on 127.0.0.1.", file=sys.stderr)
        return 1

    with Server((host, port), handler) as httpd:
        shown = "127.0.0.1" if host in ("0.0.0.0", "") else host
        print(f"IPO Pulse Studio -> http://{shown}:{port}/")
        if control.AUTH.configured():
            print(f"Trigger panel    -> http://{shown}:{port}/trigger")
        else:
            print("Trigger panel    -> disabled (no IPOPULSE_TRIGGER_PASSWORD in .env)")
        origins = control.allowed_origins()
        print(f"Browser origins  -> {', '.join(origins) if origins else 'same-origin only'
              } (IPOPULSE_ALLOWED_ORIGINS)")
        print("Ctrl+C to stop.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")
    return 0


# ── wiring ─────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ipopulse", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("new", help="add a blank IPO row to the workbook")
    sp.add_argument("slug")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_new)

    sp = sub.add_parser("remove", help="drop an IPO from the workbook")
    sp.add_argument("slug")
    sp.add_argument("--yes", action="store_true", help="skip the confirmation")
    sp.set_defaults(func=cmd_remove)

    sp = sub.add_parser("dedupe", help="fold rows that describe one offer "
                                       "into one row")
    sp.add_argument("--write", action="store_true",
                    help="apply the merges (dry run without it)")
    sp.add_argument("--keep", metavar="SLUG",
                    help="override which row survives, when the automatic "
                         "pick (most complete, then shortest slug) is wrong")
    sp.set_defaults(func=cmd_dedupe)

    sp = sub.add_parser("merge", help="fold one named IPO row into another")
    sp.add_argument("keep", help="the slug that survives")
    sp.add_argument("drop", help="the slug folded into it and then removed")
    sp.add_argument("--write", action="store_true",
                    help="apply it (dry run without it)")
    sp.set_defaults(func=cmd_merge)

    sp = sub.add_parser("list", help="show tracked IPOs")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("gmp", help="log a GMP data point")
    sp.add_argument("slug")
    sp.add_argument("value", type=float)
    sp.add_argument("--date")
    sp.add_argument("--kostak", type=float, default=0)
    sp.add_argument("--sauda", type=float, default=0)
    sp.add_argument("--source", default="manual")
    sp.set_defaults(func=cmd_gmp)

    sp = sub.add_parser("sub", help="log a day of subscription")
    sp.add_argument("slug")
    sp.add_argument("day", type=int)
    sp.add_argument("--date")
    sp.add_argument("--qib", type=float, default=0)
    sp.add_argument("--nii", type=float, default=0)
    sp.add_argument("--retail", type=float, default=0)
    sp.add_argument("--employee", type=float, default=0)
    sp.add_argument("--total", type=float, default=0)
    sp.set_defaults(func=cmd_sub)

    sp = sub.add_parser("sync", help="pull from a provider into the YAML")
    sp.add_argument("--slug")
    sp.add_argument("--provider", default="api",
                    choices=["manual", "api", "sheet", "nse", "research"])
    sp.add_argument("--prefer-api", action="store_true",
                    help="let fetched values overwrite hand-typed ones")
    sp.add_argument("--discover", action="store_true",
                    help="also scaffold IPOs the provider lists but we do not track")
    sp.add_argument("--allow-duplicate", action="store_true",
                    help="scaffold a discovered row even when it looks "
                         "like "
                         "an offer already tracked under another slug")
    sp.add_argument("--no-translate", action="store_true")
    sp.add_argument("--model", default=default_model())
    sp.set_defaults(func=cmd_sync)

    sp = sub.add_parser("translate", help="Gemini translation into hi/te")
    sp.add_argument("slug", nargs="?")
    sp.add_argument("--langs", help="comma separated, default hi,te")
    sp.add_argument("--model", default=default_model())
    sp.add_argument("--force", action="store_true", help="bypass the cache")
    sp.set_defaults(func=cmd_translate)

    sp = sub.add_parser("analyse", help="Gemini drafts overview and flags")
    sp.add_argument("slug")
    sp.add_argument("--model", default=default_model())
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--write", action="store_true", help="save the draft into the YAML")
    sp.add_argument("--no-translate", action="store_true")
    sp.set_defaults(func=cmd_analyse)

    sp = sub.add_parser("import", help="import IPOs or GMP rows from Excel/CSV (file or URL)")
    sp.add_argument("source", help="path or https:// link to .xlsx / .csv")
    sp.add_argument("--kind", default="ipos", choices=["ipos", "gmp"],
                    help="one row per IPO, or one row per GMP day")
    sp.add_argument("--sheet", help="worksheet name (xlsx only)")
    sp.add_argument("--slug", help="force everything into this one IPO")
    sp.add_argument("--prefer-sheet", action="store_true",
                    help="let the sheet overwrite hand-typed values")
    sp.add_argument("--dry-run", action="store_true", help="show what would import")
    sp.add_argument("--no-translate", action="store_true")
    sp.add_argument("--model", default=default_model())
    sp.set_defaults(func=cmd_import)

    sp = sub.add_parser("job", help="run a named scheduled job (what the timers call)")
    sp.add_argument("name", nargs="*",
                    help="one or more job names; they run in the order given. "
                         "Omit to list every job and its schedule.")
    sp.set_defaults(func=cmd_job)

    # No `push` command. It wrote the local store up into the Google Sheet,
    # via a flat 22-column layout that predates the tab structure the store
    # now uses. With the sheet as the store, running it would overwrite live
    # tabs with a different schema — a data-loss bug wearing the name of a
    # sync step. `import` covers the remaining real case: pulling an OUTSIDE
    # spreadsheet in.

    sp = sub.add_parser("research", help="Gemini web lookup for GMP / subscription / issue")
    sp.add_argument("slug")
    sp.add_argument("--what", default="gmp",
                    choices=["gmp", "gmp-history", "sub", "subscription", "ipo",
                             "financials", "background", "both", "all"])
    sp.add_argument("--url", help="comma-separated pages to read instead of searching")
    sp.add_argument("--write", action="store_true", help="save the result")
    sp.add_argument("--force", action="store_true", help="save even if flagged for review")
    # `--what background` writes prose, which is the only branch here that can
    # trigger a translation. The flag exists so `enrich` can batch its own
    # translation at the end instead of paying two calls per step.
    sp.add_argument("--no-translate", dest="no_translate", action="store_true",
                    help="skip the hi/te translation of written prose")
    sp.add_argument("--model", default=default_model())
    sp.set_defaults(func=cmd_research)

    sp = sub.add_parser("rhp", help="read EBITDA / net worth / debt / peer P/E "
                                    "out of NSE's Red Herring Prospectus")
    sp.add_argument("slug")
    sp.add_argument("--symbol", help="NSE symbol, if the IPO has left the catalogue")
    sp.add_argument("--series", default="EQ", help="EQ (mainboard) or SME")
    sp.add_argument("--write", action="store_true", help="save the result")
    sp.add_argument("--force", action="store_true", help="save even if flagged")
    sp.add_argument("--refresh", action="store_true", help="re-download, ignore the cache")
    sp.add_argument("--model", default=default_model())
    sp.set_defaults(func=cmd_rhp)

    sp = sub.add_parser("sources", help="show or pin the pages to read for an IPO")
    sp.add_argument("slug")
    sp.add_argument("--set", metavar="ROLE=URL",
                    help="e.g. gmp=https://investorgain.com/... (empty URL unpins)")
    sp.set_defaults(func=cmd_sources)

    sp = sub.add_parser("refresh", help="daily loop: re-read GMP, then publish")
    sp.add_argument("--slug", help="just this one; default is everything still live")
    sp.add_argument("--subscription", action="store_true",
                    help="also read subscription for open issues")
    sp.add_argument("--all", action="store_true", help="include already-listed IPOs")
    sp.add_argument("--force", action="store_true", help="write even flagged values")
    sp.add_argument("--model", default=default_model())
    sp.set_defaults(func=cmd_refresh)

    sp = sub.add_parser("cache", help="inspect / prune the Gemini response cache")
    sp.add_argument("--days", type=int, default=30, help="TTL in days (default 30)")
    sp.add_argument("--prune", action="store_true", help="delete entries past the TTL")
    sp.add_argument("--clear", action="store_true", help="delete everything")
    sp.set_defaults(func=cmd_cache)

    sp = sub.add_parser("gmp-sync",
                        help="GMP from investorgain.com (ipoji as fallback) — "
                             "free, keyless, no AI")
    sp.add_argument("slug", nargs="?", help="just this one; default is every IPO")
    sp.add_argument("--source", choices=("auto", "investorgain", "ipoji"),
                    default="auto",
                    help="which desk to read. auto = investorgain, falling back "
                         "to ipoji only if its board is unreachable")
    sp.add_argument("--history", action="store_true",
                    help="also walk each IPO's dated page and backfill missing "
                         "days (always on for investorgain — its board carries "
                         "no premium)")
    sp.add_argument("--reconcile", action="store_true",
                    help="rewrite days another machine filed (ipoji, gemini) "
                         "with this desk's figure, so the trail is one source. "
                         "Hand-typed days are never touched. Snapshots the "
                         "sheet first.")
    sp.add_argument("--write", action="store_true", help="save what was found")
    sp.add_argument("--discover", action="store_true",
                    help="also add IPOs on ipoji's board that we do not track "
                         "yet — NSE only lists them once they are about to open")
    sp.add_argument("--mainboard-only", action="store_true",
                    help="with --discover, skip SME issues")
    sp.add_argument("--allow-duplicate", action="store_true",
                    help="scaffold a discovered row even when it looks "
                         "like "
                         "an offer already tracked under another slug")
    sp.set_defaults(func=cmd_gmp_sync)

    sp = sub.add_parser("enrich", help="fill whatever each IPO is still missing "
                                       "(research + RHP + analyse + translate)")
    sp.add_argument("slug", nargs="?", help="just this one; default is every IPO")
    sp.add_argument("--max-ai", type=int, default=12,
                    help="cap on Gemini calls this run (free tier is per-day)")
    sp.add_argument("--dry-run", action="store_true",
                    help="print the plan without running or spending anything")
    sp.add_argument("--retry-after", type=int, default=7, metavar="DAYS",
                    help="how long before a tried-and-incomplete step is "
                         "attempted again (default 7)")
    sp.add_argument("--retry", action="store_true",
                    help="ignore the attempt log and run every planned step")
    sp.add_argument("--strict", action="store_true",
                    help="exit 1 if any step failed (the chain does not pass "
                         "this — build and validate must still run)")
    sp.set_defaults(func=cmd_enrich)

    sp = sub.add_parser("facts", help="financials + valuation KPIs from "
                                      "InvestorGain — free, keyless, no AI")
    sp.add_argument("slug", nargs="*", help="just these; default is every IPO")
    sp.add_argument("--force", action="store_true",
                    help="overwrite stored figures instead of filling gaps only")
    sp.add_argument("--dry-run", action="store_true",
                    help="print what would be written and write nothing")
    sp.set_defaults(func=cmd_facts)

    sp = sub.add_parser("validate", help="which reels are recordable right now, "
                                         "until when, and what contradicts itself")
    sp.add_argument("slug", nargs="*", help="just these; default is every IPO")
    sp.add_argument("--verbose", "-v", action="store_true",
                    help="also list the thin scenes and the shut windows")
    sp.add_argument("--json", action="store_true", help="machine-readable")
    sp.add_argument("--strict", action="store_true",
                    help="exit 1 if any record contradicts itself")
    sp.set_defaults(func=cmd_validate)

    sp = sub.add_parser("monitor", help="is the sheet still being updated? "
                                        "the watchdog the timers call")
    sp.add_argument("--json", action="store_true", help="machine-readable")
    sp.add_argument("--strict", action="store_true",
                    help="exit 1 on any error-level finding, so a failed "
                         "timer run is visible as a failed task")
    sp.set_defaults(func=cmd_monitor)

    sp = sub.add_parser("publish", help="review rendered videos and send "
                                        "the approved ones to YouTube")
    sp.add_argument("--authorise", action="store_true",
                    help="one-time OAuth consent for the channel")
    sp.add_argument("--port", type=int, default=8765,
                    help="loopback port for the consent redirect")
    sp.add_argument("--approve", action="append", metavar="ID",
                    help="approve a queued video (or 'all'). Repeatable")
    sp.add_argument("--public", action="store_true",
                    help="approve it as PUBLIC. Without this it goes up "
                         "unlisted — a wrong figure in an unlisted video is "
                         "an embarrassment, public it is somebody's money")
    sp.add_argument("--reject", action="append", metavar="ID",
                    help="reject a queued video. Repeatable")
    sp.add_argument("--why", help="note stored with a rejection")
    sp.add_argument("--upload", action="store_true",
                    help="send everything approved")
    sp.add_argument("--dry-run", action="store_true",
                    help="with --upload, list what would go and send nothing")
    sp.set_defaults(func=cmd_publish)

    sp = sub.add_parser("brief", help="write per-IPO documents for a "
                                      "notebook / Gemini Notebook")
    sp.add_argument("slug", nargs="*", help="which IPOs; default all")
    sp.add_argument("--market", metavar="DAY",
                    help="a market briefing instead: an ISO date, or 'all'")
    sp.add_argument("--public", action="store_true",
                    help="write into frontend/brief/ so it becomes a public "
                         "URL. NOT behind the password gate — this publishes "
                         "the analysis before a video does")
    sp.set_defaults(func=cmd_brief)

    sp = sub.add_parser("migrate", help="rebuild the store in a new "
                                        "spreadsheet and verify it")
    sp.add_argument("--write", action="store_true",
                    help="actually create and write (dry run without it)")
    sp.add_argument("--title", help="title for the new spreadsheet")
    sp.add_argument("--into", metavar="SHEET_ID",
                    help="write into an existing book instead of creating "
                         "one — for a second attempt at the same target")
    sp.set_defaults(func=cmd_migrate)

    sp = sub.add_parser("market", help="build the daily pre-market briefing "
                                       "(reel 7) — indices, news, setups")
    sp.add_argument("--day", help="ISO date; defaults to today in IST")
    sp.add_argument("--write", action="store_true",
                    help="store it (dry run without it)")
    sp.add_argument("--replace", action="store_true",
                    help="rebuild a day that already has a briefing")
    sp.add_argument("--show", action="store_true",
                    help="print the stored briefing instead of building one")
    sp.add_argument("--force", action="store_true",
                    help="build even on a day the exchange is shut")
    sp.add_argument("--model",
                    help="override the model. Defaults to the strongest one "
                         "reachable, not the cheap one the rest of the "
                         "pipeline uses — this reel is judged on accuracy")
    sp.set_defaults(func=cmd_market)

    sp = sub.add_parser("grade", help="score the stored numbers against InvestorGain")
    sp.add_argument("--days", type=int, default=7,
                    help="how many days without a GMP counts as stale (default 7)")
    sp.add_argument("--strict", action="store_true",
                    help="exit 1 if anything disagrees (for a pre-record gate)")
    sp.set_defaults(func=cmd_grade)

    sp = sub.add_parser("videos", help="what the channel has published, and what is missing")
    sp.set_defaults(func=cmd_videos)

    sp = sub.add_parser("narrate", help="batch-narrate a scripts JSON into "
                                        "Release-ready audio files")
    sp.add_argument("scripts", help="JSON from scripts.js / narrate.yml")
    sp.add_argument("--slug", help="only these slugs (comma separated)")
    sp.add_argument("--langs", help="default en,hi,te")
    sp.add_argument("--budget", type=int, default=12_000,
                    help="stop after this many BILLED characters (default "
                         "12000; a free plan is 10000 a MONTH, and three open "
                         "IPOs in three languages is ~35000)")
    sp.add_argument("--dry-run", action="store_true",
                    help="list what would be made, and what it would cost")
    sp.add_argument("--force", action="store_true",
                    help="re-make clips whose file already exists")
    sp.set_defaults(func=cmd_narrate)

    sp = sub.add_parser("voice", help="narrate a script with ElevenLabs "
                                      "(cached; billed per character)")
    sp.add_argument("text", nargs="?", help="the script; omit to read stdin")
    sp.add_argument("--text-file", help="read the script from a file instead")
    sp.add_argument("--out", help="write the mp3 here as well as to the cache")
    sp.add_argument("--voice", help="voice id, default ELEVENLABS_VOICE_ID")
    sp.add_argument("--lang", choices=["en", "hi", "te"],
                    help="pick the voice and model configured for this "
                         "language (ELEVENLABS_VOICE_ID_TE etc.)")
    sp.add_argument("--voices", action="store_true",
                    help="list the voices this key can use, and exit")
    sp.add_argument("--plan", action="store_true",
                    help="show which provider, voice and model each language uses")
    sp.add_argument("--provider", choices=["gemini", "elevenlabs"],
                    help="force one provider instead of the configured order")
    sp.add_argument("--compare", action="store_true",
                    help="render the same text through every configured "
                         "provider, side by side, so you can pick by ear")
    sp.add_argument("--budget", action="store_true",
                    help="show this month's character spend, and exit")
    sp.add_argument("--force", action="store_true",
                    help="re-generate even if it is cached (this costs money)")
    sp.set_defaults(func=cmd_voice)

    sp = sub.add_parser("verify", help="does every tracked IPO exist? ask NSE and BSE")
    sp.add_argument("--write", action="store_true",
                    help="stamp each exchange confirmation onto the sheet")
    sp.add_argument("--strict", action="store_true",
                    help="exit 1 if any IPO is unaccounted for (for a pre-record gate)")
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser("doctor", help="what is missing, and repair what is derivable")
    sp.add_argument("slug", nargs="?", help="just this one; default is every IPO")
    sp.add_argument("--fix", action="store_true",
                    help="apply the derivable repairs and republish")
    sp.add_argument("--strict", action="store_true",
                    help="exit 1 if anything would render blank (for a pre-record gate)")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("build", help="publish JSON for the frontend")
    sp.add_argument("--prune-cache", action="store_true",
                    help="also drop Gemini responses past their TTL")
    sp.add_argument("--days", type=int, default=30)
    sp.set_defaults(func=cmd_build)

    sp = sub.add_parser("report", help="write the Excel workbook")
    sp.add_argument("slug", nargs="?")
    sp.add_argument("-o", "--output")
    sp.set_defaults(func=cmd_report)

    sp = sub.add_parser("config", help="write frontend/js/config.js from "
                                       "GOOGLE_SHEETS_ID (the deploy does "
                                       "this too)")
    sp.set_defaults(func=cmd_config)

    sp = sub.add_parser("serve", help="serve the frontend locally")
    sp.add_argument("--port", type=int, default=DEFAULT_PORT)
    sp.add_argument("--host", help="bind address; 0.0.0.0 inside Docker")
    sp.set_defaults(func=cmd_serve)

    return p


def _force_utf8_stdout() -> None:
    """Windows consoles default to cp1252, which cannot encode ₹ or —."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdout()
    load_dotenv()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except FileExistsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except SheetUnavailable as exc:
        # The store is online now, so "cannot reach it" is the most likely
        # failure of all. Say which of the three causes it is, not a traceback.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except WorkbookLocked as exc:
        # Only snapshots are files now, but they still land in Excel.
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
