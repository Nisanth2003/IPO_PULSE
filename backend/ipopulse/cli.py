"""ipopulse — the command line for the data side.

    ipopulse new <slug>              scaffold a YAML file for a new IPO
    ipopulse import <file|url>       pull IPOs / GMP rows out of Excel or CSV
    ipopulse sync --provider nse --discover   pull live IPOs from NSE, no key
    ipopulse push [slug]             write IPOs / GMP rows into a Google Sheet
    ipopulse sources <slug>          pin the exact pages to read for this IPO
    ipopulse research <slug>         Gemini web lookup, with citations
    ipopulse refresh                 daily: re-read GMP for every live IPO, publish
    ipopulse gmp <slug> 46           log today's GMP (defaults to today)
    ipopulse sub <slug> 2 --qib 12.4 --nii 24.9 --retail 9.2 --total 14.6
    ipopulse translate [slug]        Gemini -> hi/te, cached, written to YAML
    ipopulse analyse <slug>          Gemini drafts overview / flags
    ipopulse doctor [slug] [--fix]   what is missing; repair what is derivable
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
import json
import os
import sys
from datetime import date
from pathlib import Path

from . import store
from .ai import ALLOTMENT_STEPS, AiUnavailable, Gemini, default_model
from .compute import derive
from .models import Ipo
from .providers import get_provider
from .providers.base import merge, merge_series
from .publish import publish
from .report import write_report

LANGS = ["hi", "te"]


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
    print(f"Created {path}")
    print("Fill it in, then run:  ipopulse build")
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
        for rec in catalogue:
            slug = rec.get("slug")
            if not slug or slug in known:
                continue
            store.scaffold(slug, overwrite=True)
            print(f"  + discovered {slug} ({rec.get('company', '')})")
            known.add(slug)
            slugs.append(slug)

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


def cmd_push(args) -> int:
    """Write IPOs (or GMP rows) up into the Google Sheet."""
    from .providers import sheet_push

    if args.slug:
        ipos = [store.load(args.slug)]
    else:
        ipos = store.load_all()
    if not ipos:
        print("No IPOs to push.")
        return 1

    try:
        s = sheet_push.push(ipos, kind=args.kind, tab=args.tab,
                            sheet_id=args.sheet_id, dry_run=args.dry_run)
    except sheet_push.SheetsUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    n = len(s["records"])
    print(f"{n} row(s) from {len(ipos)} IPO(s) -> tab '{s['tab']}'"
          f" as {s.get('service_account', '?')}")
    if s["header_written"]:
        print("  wrote a header row (the sheet was empty)")
    if s["extra_columns"]:
        print(f"  left alone: {', '.join(s['extra_columns'][:12])}")
    if s["dropped_fields"]:
        print(f"  no column for: {', '.join(s['dropped_fields'])}"
              "  (add one and it fills next push)")

    if args.dry_run:
        for rec in s["records"][:8]:
            print("   ", rec)
        print(f"\n(dry run — {s['updated']} would update, "
              f"{s['appended']} would append)")
        return 0

    print(f"  updated {s['updated']}, appended {s['appended']}")
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
            print(f"    backend/data/ipos/{args.slug}.yaml  (financials: block)")
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


def cmd_refresh(args) -> int:
    """The daily loop: re-read GMP (+ subscription), then republish."""
    from .providers.research import ResearchProvider

    provider = ResearchProvider(Gemini(model=args.model))
    if not provider.available():
        print("Gemini not configured — set GEMINI_API_KEY in .env.")
        print("Without it, log numbers by hand:  ipopulse gmp <slug> <value>")
        return 1

    slugs = [args.slug] if args.slug else store.list_slugs()
    flagged: list[str] = []
    for slug in slugs:
        ipo = store.load(slug)
        status = derive(ipo)["dates"]["status"]
        if status in ("listed",) and not args.all:
            continue                       # nothing left to track
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
            if not p.get("needs_review") or args.force:
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
    if flagged:
        print(f"⚠ {len(set(flagged))} IPO(s) flagged and NOT written: "
              f"{', '.join(sorted(set(flagged)))}")
        print("  Check the source pages and enter those by hand.")
    publish(store.load_all())
    print(f"Published -> {store.FRONTEND_DATA}")
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
    try:
        draft = gem.draft_analysis(context, force=args.force)
    except AiUnavailable as exc:
        print(f"Cannot draft: {exc}")
        return 1

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
        print(f"\nWritten into {store.ipo_path(args.slug)} — edit before publishing.")
        maybe_translate(args.slug, args)      # new prose -> translate now, once
    else:
        print("\n(Not saved. Re-run with --write to put it in the YAML.)")
    return 0


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
        clean = not rep["missing"] and not gaps and not rep["gmp_stale"]

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

        if rep["gmp_stale"]:
            n = rep["gmp_age_days"]
            print(f"  ⚠ GMP is {n} day(s) old — the card labels it "
                  f"\"today's GMP\" until this is refreshed")

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
            print(f"Republished -> {store.FRONTEND_DATA}")
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
    written = publish(ipos)
    print(f"Published {len(ipos)} IPO(s) -> {store.FRONTEND_DATA}")
    for path in written[-2:]:
        print(f"  {path.name}")
    print("\nCommit frontend/data/ and GitHub Pages serves the update.")
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


def cmd_serve(args) -> int:
    import functools
    import http.server
    import socketserver

    root = store.BACKEND_ROOT.parent / "frontend"

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

        def log_message(self, fmt, *a):      # quieter console
            pass

    handler = functools.partial(Handler, directory=str(root))
    socketserver.TCPServer.allow_reuse_address = True
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

    with socketserver.TCPServer((host, args.port), handler) as httpd:
        shown = "127.0.0.1" if host in ("0.0.0.0", "") else host
        print(f"IPO Pulse Studio -> http://{shown}:{args.port}/")
        if control.AUTH.configured():
            print(f"Trigger panel    -> http://{shown}:{args.port}/trigger")
        else:
            print("Trigger panel    -> disabled (no IPOPULSE_TRIGGER_PASSWORD in .env)")
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

    sp = sub.add_parser("new", help="scaffold a new IPO YAML")
    sp.add_argument("slug")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_new)

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

    sp = sub.add_parser("push", help="write IPOs or GMP rows up into a Google Sheet")
    sp.add_argument("slug", nargs="?", help="just this one; default is every IPO")
    sp.add_argument("--kind", default="ipos", choices=["ipos", "gmp"],
                    help="one row per IPO, or one row per GMP day")
    sp.add_argument("--tab", help="worksheet name; default GOOGLE_SHEETS_TAB, else the first")
    sp.add_argument("--sheet-id", help="override GOOGLE_SHEETS_ID")
    sp.add_argument("--dry-run", action="store_true", help="show what would be written")
    sp.set_defaults(func=cmd_push)

    sp = sub.add_parser("research", help="Gemini web lookup for GMP / subscription / issue")
    sp.add_argument("slug")
    sp.add_argument("--what", default="gmp",
                    choices=["gmp", "gmp-history", "sub", "subscription", "ipo",
                             "financials", "both", "all"])
    sp.add_argument("--url", help="comma-separated pages to read instead of searching")
    sp.add_argument("--write", action="store_true", help="save the result")
    sp.add_argument("--force", action="store_true", help="save even if flagged for review")
    sp.add_argument("--model", default=default_model())
    sp.set_defaults(func=cmd_research)

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

    sp = sub.add_parser("serve", help="serve the frontend locally")
    sp.add_argument("--port", type=int, default=8000)
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


if __name__ == "__main__":
    raise SystemExit(main())
