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
    ipopulse enrich [slug]           fill whatever is missing, automatically
    ipopulse rhp <slug> --write      financials from NSE's prospectus
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
from typing import Any

from . import store
from .sheets import SheetUnavailable
from .workbook import WorkbookLocked
from .ai import ALLOTMENT_STEPS, AiUnavailable, Gemini, default_model
from .compute import derive
from .models import Ipo
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
        print(f"\nWritten into the sheet (Lists tab, {args.slug}) "
              f"— read it before publishing.")
        maybe_translate(args.slug, args)      # new prose -> translate now, once
    else:
        print("\n(Not saved. Re-run with --write to put it in the sheet.)")
    return 0


def cmd_gmp_sync(args) -> int:
    """Pull GMP from ipoji.com — free, keyless, no model, no vetting needed.

    This is the source that was missing. Every previous GMP came from a model
    reading a page, which cost quota, needed vetting, and returned nothing at
    all once the site started blocking. ipoji serves it server-side with the
    figures in `data-*` attributes, so the numbers arrive as numbers.

    `--history` additionally walks each IPO's dated page, which is what makes
    a missed day recoverable: the days lost to the CI bug in early August were
    written off as gone forever because the old source published only today.
    """
    from .providers import ipoji

    rows = ipoji.board()
    if not rows:
        print("ipoji returned no rows — the board markup may have changed, "
              "or the site is unreachable.")
        return 1
    print(f"ipoji board: {len(rows)} IPO(s)")

    ipos = [store.load(args.slug)] if args.slug else store.load_all()
    slugs = [i.slug for i in ipos]
    companies = {i.slug: (i.company or "") for i in ipos}
    by_slug = {i.slug: i for i in ipos}

    matched: dict[str, dict] = {}
    for row in rows:
        slug = ipoji.match_slug(row["name"], row.get("url") or "", slugs, companies)
        if slug:
            matched[slug] = row
    seen = {matched[s]["name"] for s in matched}
    unmatched = [r for r in rows if r["name"] not in seen]
    print(f"matched {len(matched)} of ours; "
          f"unmatched on ipoji: "
          f"{', '.join(r['name'] for r in unmatched)[:90] or 'none'}")

    # Discovery from ipoji, not just NSE.
    #
    # `sync --discover` reads NSE's catalogue, and NSE lists an issue only
    # once it is at or near opening — so a mainboard IPO announced days
    # earlier was invisible to the pipeline until it was almost too late to
    # cover. ipoji carries them while they are still upcoming: nine were on
    # its board and untracked here, three of them mainboard, one already
    # quoting a premium.
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
                s = ipoji.match_slug(row["name"], row.get("url") or "", slugs, companies)
                if s:
                    matched[s] = row

    today = date.today().isoformat()
    wrote = filled = clashed = 0
    for slug, row in sorted(matched.items()):
        ipo = by_slug[slug]
        have = {p.date.isoformat(): p.gmp for p in ipo.gmp_history if p.date}
        incoming: list[dict] = []

        if row["gmp"] is not None and row.get("has_gmp"):
            incoming.append({"date": today, "gmp": row["gmp"], "source": "ipoji"})
        if args.history and row.get("url"):
            incoming.extend(ipoji.history(row["url"]))

        # Gap-fill only. A reading already on file was taken live on its own
        # day, from whichever desk was quoting then; this page is a different
        # desk's later record of the same day. Filling a hole is strictly an
        # improvement, replacing a reading is a judgement nobody asked for.
        fresh = [p for p in incoming if p["date"] not in have]
        clash = [p for p in incoming
                 if p["date"] in have and abs(have[p["date"]] - p["gmp"]) > 0.01]
        if clash:
            clashed += len(clash)

        if not fresh:
            continue
        # Same implausibility bound the model-sourced path uses.
        band = ipo.issue.price_high
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

    print()
    print(f"{filled} reading(s) across {wrote} IPO(s) "
          f"{'written' if args.write else 'found (re-run with --write)'}.")
    if clashed:
        # Reported, never silently reconciled — see the module docstring.
        print(f"{clashed} day(s) where ipoji disagrees with a reading already "
              f"on file; left alone.")
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

    # One RHP read fills five fields, so it is worth running if any is absent.
    if not (f.revenue and f.ebitda and f.net_worth and f.eps and f.pe_peer_avg):
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
    if has_facts and (not a.overview or not (a.green_flags or a.red_flags)):
        steps.append(("analysis draft",
                      ["analyse", ipo.slug, "--write", "--no-translate"], 1))

    # Translation is two calls (hi + te) and only makes sense once there is
    # prose to translate — which the step above may have just created.
    if a.overview and (not ipo.i18n.get("hi") or not ipo.i18n.get("te")):
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
        _record_attempt(log, ipo.slug, label)
        if rc:
            failed += 1
            print(f"   ! exited {rc} — continuing")

    print()
    if skipped:
        # Never a silent cap: the tail is the whole reason a budget is safe.
        print(f"⏸ {len(skipped)} step(s) left for the next run (budget spent):")
        for s in skipped[:8]:
            print(f"   {s}")
        if len(skipped) > 8:
            print(f"   … and {len(skipped) - 8} more")
    print(f"{ran} step(s) run, {failed} failed, {spent} AI call(s) used.")

    if not args.dry_run and ran:
        # doctor picks up what the new figures make derivable — post-issue
        # shares from PAT/EPS, a total from its parts — then republish.
        main(["doctor", "--fix"])
        publish(store.load_all())
        print("Saved to the sheet — the site shows it on the next reload.")
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
        ' */\n'
        f'const SHEET_ID = "{sid}";\n'
        f'const API_BASE = "{api}";\n',
        encoding="utf-8")
    return dest


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
                             "financials", "both", "all"])
    sp.add_argument("--url", help="comma-separated pages to read instead of searching")
    sp.add_argument("--write", action="store_true", help="save the result")
    sp.add_argument("--force", action="store_true", help="save even if flagged for review")
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

    sp = sub.add_parser("gmp-sync", help="GMP from ipoji.com — free, keyless, no AI")
    sp.add_argument("slug", nargs="?", help="just this one; default is every IPO")
    sp.add_argument("--history", action="store_true",
                    help="also walk each IPO's dated page and backfill missing days")
    sp.add_argument("--write", action="store_true", help="save what was found")
    sp.add_argument("--discover", action="store_true",
                    help="also add IPOs on ipoji's board that we do not track "
                         "yet — NSE only lists them once they are about to open")
    sp.add_argument("--mainboard-only", action="store_true",
                    help="with --discover, skip SME issues")
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
    sp.set_defaults(func=cmd_enrich)

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
