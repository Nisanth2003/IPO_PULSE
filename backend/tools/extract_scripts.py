"""Pull every reel's narration out of the studio, in every language.

WHY A BROWSER IS IN THIS PIPELINE AT ALL
----------------------------------------
The scripts do not exist anywhere until a browser builds them. The sheet holds
raw data — price band, subscription, GMP, the analysis prose — and the narration
is assembled from it by the voTake* functions in frontend/js/output.js, which is
1,300 lines of editorial judgement written in JavaScript. Reimplementing that in
Python to make the pipeline "pure" would put the same decisions in two places
and guarantee they drift; the first wrong number would appear in a video, not in
a test. So the browser stays the author of the script and this is just the thing
that writes it down.

That is also why this cannot run on GitHub Pages: it needs `ipopulse serve` so
the page has a backend to answer /api/health, and it needs the gate open, which
on localhost with no IPOPULSE_TRIGGER_PASSWORD it is (see gate.js:137).

    python tools/extract_scripts.py --out out/scripts.json

Then:  python -m ipopulse.cli narrate out/scripts.json --dry-run

Uses channel="chrome" — the Chrome already on the machine — so no 150 MB
bundled-chromium download. On a CI runner without Chrome, pass --channel "" to
fall back to the bundled build that `playwright install chromium` provides.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# One place that knows the shape of the studio's internals. If the component's
# API changes, this breaks loudly here rather than producing empty scripts.
EXTRACT_JS = """
async ([reels, langs]) => {
  const root = document.querySelector('[x-data]');
  if (!root || !window.Alpine) throw new Error('Alpine component not found');
  const d = Alpine.$data(root);

  // Wait for the catalogue: loadCatalogue() is async and an empty list here
  // would look like "this IPO has no scripts" rather than "we asked too early".
  //
  // 45s, not the 10s this first had. The page fetches EIGHT sheet tabs from
  // Google before the catalogue exists, and a cold headless browser has no HTTP
  // cache and no warm connection — 10s passed on a warm machine and then failed
  // on the same data a few minutes later, which is the worst kind of flake
  // because it looks like a code change broke it.
  for (let i = 0; i < 450 && !(d.catalogue || []).length; i++) {
    await new Promise(r => setTimeout(r, 100));
  }
  if (!(d.catalogue || []).length) {
    // loadError is where data.js puts the reason, and it is far more useful
    // than the timeout: a shared-sheet permission problem reads identically to
    // slowness otherwise.
    throw new Error('catalogue never loaded'
      + (d.loadError ? ' — ' + d.loadError : ' (no loadError reported)'));
  }

  const origSlug = d.slug, origLang = d.lang;
  const book = {}, meta = {};

  for (const entry of d.catalogue) {
    // select() fetches, so it must be awaited or the scripts belong to the
    // PREVIOUS IPO — the kind of bug that ships a video about the wrong company.
    await d.select(entry.slug);
    await new Promise(r => setTimeout(r, 250));
    book[entry.slug] = {};
    meta[entry.slug] = { company: d.ipo && d.ipo.company, status: entry.status };
    for (const n of reels) {
      book[entry.slug][n] = {};
      for (const lang of langs) {
        // lang AND recompute(), never lang alone.
        //
        // The script's sentences follow `lang`, but every value it interpolates
        // comes from `d.loc`, which only recompute() rebuilds. Assigning lang on
        // its own yields this language's phrasing around the PREVIOUS language's
        // data — a hybrid that no card ever renders. Measured on
        // esds-software-solution reel 1: EN came out 1,998 characters this way
        // against the 1,937 the page actually shows.
        //
        // That matters more here than anywhere: this file decides what gets
        // paid for and spoken aloud, so a hybrid becomes a published video of a
        // voice reading text that was never on screen.
        d.lang = lang;
        d.recompute();
        const text = (d.scriptFor(n) || '').trim();
        if (text) book[entry.slug][n][lang] = text;
      }
    }
  }

  d.lang = origLang;
  d.recompute();
  await d.select(origSlug);
  return { book, meta };
}
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="http://localhost:8000",
                    help="a running `ipopulse serve`")
    ap.add_argument("--out", default="out/scripts.json")
    ap.add_argument("--reels", default="1,2,3,4,5,6")
    ap.add_argument("--langs", default="en,hi,te")
    ap.add_argument("--channel", default="chrome",
                    help='browser channel; "" for playwright\'s bundled chromium')
    ap.add_argument("--timeout", type=int, default=120_000)
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("error: pip install playwright", file=sys.stderr)
        return 2

    reels = [int(r) for r in args.reels.split(",") if r.strip()]
    langs = [s.strip() for s in args.langs.split(",") if s.strip()]

    with sync_playwright() as p:
        launch: dict = {"headless": True}
        if args.channel:
            launch["channel"] = args.channel
        browser = p.chromium.launch(**launch)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        try:
            page.goto(args.url, timeout=args.timeout, wait_until="load")
            # The gate must be open. On localhost with no password configured it
            # opens itself; if a hash IS baked into config.js this hangs forever
            # with no clue why, so say so instead.
            try:
                page.wait_for_function(
                    "document.documentElement.classList.contains('gate-open')",
                    timeout=15_000)
            except Exception:
                print("error: the studio's password gate did not open.\n"
                      "  Run the server with IPOPULSE_TRIGGER_PASSWORD unset so "
                      "config.js carries no SITE_GATE_HASH — gate.js opens\n"
                      "  automatically on localhost when none is set.",
                      file=sys.stderr)
                return 1
            result = page.evaluate(EXTRACT_JS, [reels, langs])
        finally:
            browser.close()

    if errors:
        print(f"note: {len(errors)} page error(s), first: {errors[0][:160]}",
              file=sys.stderr)

    book, meta = result["book"], result["meta"]
    dest = Path(args.out)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # ensure_ascii=False keeps Devanagari and Telugu readable in the file, which
    # matters because this is the artefact a human diffs when a script is wrong.
    dest.write_text(json.dumps(book, ensure_ascii=False, indent=1),
                    encoding="utf-8")

    # `meta` goes to a SIDECAR, not into scripts.json.
    #
    # scripts.json is a bare {slug: {reel: {lang: text}}} and `ipopulse narrate`
    # iterates it with book.items() — wrapping it as {"book":…,"meta":…} would
    # make every slug look like a reel and break narration outright. So the
    # status lives beside it instead, and nothing that already reads the book
    # has to change.
    #
    # What needs it: tools/r2_sync.py prunes clips for IPOs that have already
    # LISTED. Once an issue lists, its reels are finished work — nobody records
    # a GMP video for a stock that is already trading — so those objects are
    # dead weight against R2's 10 GB free tier. Status is derived from the
    # dates by the board and is already in the payload the studio handed us;
    # recomputing it in the pruner would put the same rule in two places.
    meta_dest = dest.with_name(dest.stem + ".meta.json")
    meta_dest.write_text(json.dumps(meta, ensure_ascii=False, indent=1),
                         encoding="utf-8")

    clips = sum(len(p) for r in book.values() for p in r.values())
    chars = sum(len(t) for r in book.values() for p in r.values()
                for t in p.values())
    print(f"{len(book)} IPO(s), {clips} clips, {chars:,} characters -> {dest}")
    print(f"status for {len(meta)} IPO(s) -> {meta_dest}")
    by_status: dict[str, int] = {}
    for slug, m in meta.items():
        by_status[m.get("status") or "?"] = by_status.get(m.get("status") or "?", 0) + 1
    print("by status:", ", ".join(f"{k}={v}" for k, v in sorted(by_status.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
