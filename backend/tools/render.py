"""Render a reel to a finished .mp4 with no human in the room.

This is the piece the pipeline was missing. Everything else — the data, the
scripts, the translations, the voice — already runs on a timer; the video did
not, because making one meant a person sitting at the studio pressing F, then
Space, then clicking a screen-capture picker.

── Why not capture.js ─────────────────────────────────────────────────────

`frontend/js/capture.js` records the tab with `getDisplayMedia`, and its own
header says why that can never be automated:

    "It needs a user gesture and a picker click EVERY time. There is no
     permission to remember and no way to pre-select the tab."

That is a browser security rule, not a gap in the code. Any amount of
scripting still leaves a dialog waiting for a click, which is exactly the one
thing "leave the machine on and it publishes" cannot contain.

── What this does instead ─────────────────────────────────────────────────

A reel is not really a video. It is a handful of still cards, each held for a
measured number of seconds, with a transition between them. So:

    1. drive the studio headlessly and screenshot each scene once
    2. hand the stills to ffmpeg with their durations
    3. let ffmpeg do the motion, with `xfade`
    4. mux the narration that was already generated for that script

The result is better than a screen recording in three ways that matter. It is
**deterministic** — the same record renders the same file, so a re-run after a
data fix is a diff rather than a re-shoot. It is **clean** — 1080x1920 of pure
card, no cursor, no browser chrome, no dropped frames on a busy machine. And
it is **cheap**: eight screenshots and an ffmpeg pass, rather than thirty
frames a second of live compositing.

The cost is that motion has to be expressible as a transition between stills.
For this format that is not a real limitation — the cards do not animate, they
replace each other.

── The gate ───────────────────────────────────────────────────────────────

`gate.js` opens immediately when `SITE_GATE_HASH` is empty, so the local
server this drives is configured without a password and there is no gate to
get past. Nothing here reads, stores or types a credential; the gate exists to
keep strangers off the public URL, and the renderer is not on the public URL.

Usage:

    python tools/render.py --slug rays-of-belief --reel 2 --lang en
    python tools/render.py --all --reel 2            # every recordable IPO
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
sys.path.insert(0, str(BACKEND))

# 9:16 at the resolution YouTube actually wants for Shorts. The studio's frame
# is a CSS aspect box, so this is applied to the screenshot rather than to the
# page — the card lays itself out at whatever size the viewport gives it and
# scaling a 540-wide shot to 1080 would be visibly soft on the type.
WIDTH, HEIGHT = 1080, 1920

# How long one scene takes to replace another. Short enough not to eat the
# hold, long enough to read as deliberate rather than as a glitch.
XFADE = 0.45

# The transition rotation.
#
# Rotated rather than fixed, and that is a content decision as much as a visual
# one: YouTube's inauthentic-content policy names mass-produced, identically
# templated output, and a channel where every cut for a year is the same
# dissolve is squarely in that description. It also just reads better — the
# same wipe eight times in twenty seconds becomes visible as a mechanism.
#
# `fade` leads because it is the one that never draws attention to itself, and
# scene 1 -> 2 is where a viewer is deciding whether to stay.
TRANSITIONS = [
    "fade",          # the safe one, first
    "slideleft",     # the "fold left" feel — the card is pushed off
    "smoothright",
    "wipeleft",
    "slideright",
    "circleopen",
    "smoothleft",
    "dissolve",
]

STUDIO_JS = """
async ([slug, reel, lang]) => {
  const root = document.querySelector('[x-data]');
  if (!root || !window.Alpine) throw new Error('Alpine component not found');
  const d = Alpine.$data(root);

  // Same 45s as extract_scripts.py, for the same reason: eight sheet tabs
  // over a cold connection, and an empty catalogue would render a blank card
  // rather than fail.
  for (let i = 0; i < 450 && !(d.catalogue || []).length; i++) {
    await new Promise(r => setTimeout(r, 100));
  }
  if (!(d.catalogue || []).length) {
    throw new Error('catalogue never loaded'
      + (d.loadError ? ' — ' + d.loadError : ''));
  }

  if (slug) {
    // await: select() fetches, and skipping the await renders the PREVIOUS
    // company's card under this company's name.
    await d.select(slug);
    await new Promise(r => setTimeout(r, 250));
  }
  // lang AND recompute(), never lang alone — the sentences follow `lang` but
  // every value they interpolate comes from `d.loc`, which only recompute()
  // rebuilds. extract_scripts.py has the long version of this note.
  d.lang = lang;
  d.recompute();
  d.reelIndex = reel - 1;
  d.scene = 0;
  d.focus = true;                 // hides the panels; the frame is all that

  // Freeze every animation before any screenshot is taken.
  //
  // A still is one instant of a moving page, so anything on a loop is
  // sampled at whatever phase it happened to be in — and consecutive scenes
  // then differ by however much that animation moves. The scene-level
  // "breathe" (a 1.012 scale on a 23-second cycle) made every card a
  // slightly different size for no reason a viewer could name. That rule is
  // gone now, but this stays: it makes the render deterministic against ALL
  // animation, including any added later, so the same record always produces
  // the same file.
  const freeze = document.createElement('style');
  freeze.textContent =
    '*,*::before,*::after{animation:none!important;' +
    'transition:none!important}';
  document.head.appendChild(freeze);
  await new Promise(r => setTimeout(r, 400));

  return {
    company: (d.ipo && d.ipo.company) || slug,
    scenes: d.scenes.map(s => s.id),
    // finalHolds is keyed by scene ID, not by index — it is built from
    // `scenes` as an object so `holdSeconds` can look a scene up by name.
    // Flattened to a list in scene order here so the caller cannot pair a
    // duration with the wrong card.
    holds: d.scenes.map(s => d.finalHolds[s.id] || 4),
    ready: d.reelReady ? !!d.reelReady(d.reel) : null,
    // The publishing pack, lifted from the studio rather than rebuilt here.
    // `output.js` already composes the title, description and hashtags from
    // the same record the cards are drawn from — recomputing them in Python
    // would be a second implementation free to disagree with what is on
    // screen, which is exactly the mirrored-pair problem this project keeps
    // paying for. Taken while the page is already open, so it costs nothing.
    titles: (d.ytTitles || []),
    description: d.ytDescription || '',
    hashtags: (d.ytHashtags || []),
  };
}
"""


def _shot(page, index: int, into: Path) -> Path:
    """One scene, as a PNG."""
    page.evaluate(
        """(i) => {
             const d = Alpine.$data(document.querySelector('[x-data]'));
             d.scene = i;
           }""", index)
    # Two rAFs plus a beat: Alpine writes the DOM on the next microtask, the
    # browser lays out on the next frame, and web fonts and the gradient want
    # one more. Screenshotting too early yields the PREVIOUS scene, which is
    # invisible in the logs and obvious in the video.
    page.wait_for_timeout(450)
    dest = into / f"scene-{index:02d}.png"
    page.locator("#capture").screenshot(path=str(dest))
    return dest


def _ffmpeg_build(stills: list[tuple[Path, float]], out: Path,
                  audio: Path | None, fps: int = 30) -> None:
    """Stills with durations, crossfaded, muxed with the narration."""
    if not stills:
        raise SystemExit("nothing to render")

    args: list[str] = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    for path, seconds in stills:
        args += ["-loop", "1", "-t", f"{seconds:.3f}", "-i", str(path)]
    if audio:
        args += ["-i", str(audio)]

    chains: list[str] = []
    for i in range(len(stills)):
        # Scale into the frame, pad if the aspect is off, and pin the SAR.
        # Without setsar the xfade chain refuses to join inputs whose sample
        # aspect ratios differ by a rounding error.
        chains.append(
            f"[{i}:v]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
            f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"setsar=1,fps={fps},format=yuv420p[v{i}]")

    if len(stills) == 1:
        last = "v0"
    else:
        # xfade's offset is measured on the OUTPUT built so far, not on the
        # next input, and it overlaps the two clips — so each join shortens the
        # running total by XFADE. Getting this recurrence wrong does not error;
        # it silently drifts the audio out of sync a little more with every
        # scene, which is the failure that is hardest to spot in review.
        running = stills[0][1]
        last = "v0"
        for i in range(1, len(stills)):
            transition = TRANSITIONS[(i - 1) % len(TRANSITIONS)]
            offset = max(0.0, running - XFADE)
            label = f"x{i}"
            chains.append(
                f"[{last}][v{i}]xfade=transition={transition}:"
                f"duration={XFADE}:offset={offset:.3f}[{label}]")
            running = running + stills[i][1] - XFADE
            last = label

    args += ["-filter_complex", ";".join(chains), "-map", f"[{last}]"]
    if audio:
        args += ["-map", f"{len(stills)}:a", "-c:a", "aac", "-b:a", "192k",
                 # The video is built to the script's own timing, so the two
                 # should already agree. -shortest stops a few hundred
                 # milliseconds of audio tail from extending the last frame.
                 "-shortest"]
    args += ["-c:v", "libx264", "-preset", "medium", "-crf", "20",
             "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]

    done = subprocess.run(args, capture_output=True, text=True)
    if done.returncode != 0:
        raise SystemExit(f"ffmpeg failed:\n{done.stderr[-1800:]}")


def _serve_gateless(port: int = 8771):
    """Serve a throwaway copy of the site with no password gate.

    The renderer used to need "run a local server whose config has no gate"
    as a documented prerequisite, and that was a trap: with the real
    `config.js` in place the studio shows its password screen, `#capture` is
    never visible, and Playwright sits there retrying "element is not stable"
    for thirty seconds before failing with something that looks nothing like
    "there is a login in the way".

    So the renderer brings its own. The frontend is copied to a temp
    directory, `config.js` is rewritten there with the same sheet id and an
    EMPTY gate hash, and that copy is served. `gate.js` opens immediately
    when the hash is empty, so there is nothing to get past.

    Nothing about this reads, stores or types a password, and the user's own
    `config.js` is never touched — the previous approach edited it in place
    and left the site un-gated if the render crashed halfway.
    """
    import http.server
    import os
    import shutil
    import threading

    src = BACKEND.parent / "frontend"
    tmp = Path(tempfile.mkdtemp(prefix="studio-"))
    shutil.copytree(src, tmp / "site", dirs_exist_ok=True)

    # .env, because this runs as a script and not through the CLI — which is
    # where `load_dotenv` normally happens. Without it SHEET_ID is empty, the
    # studio tries to read `undefined/ipo-pulse.xlsx`, and the failure reads
    # as "catalogue never loaded" rather than "no sheet configured".
    from ipopulse.cli import load_dotenv
    load_dotenv()
    sheet_id = os.getenv("GOOGLE_SHEETS_ID", "").strip()
    if not sheet_id:
        raise SystemExit(
            "GOOGLE_SHEETS_ID is not set, so the studio would have no data "
            "to draw. Check backend/.env.")
    lines = [
        "/* generated for a headless render - no gate, never deployed */",
        f'const SHEET_ID = "{sheet_id}";',
        'const API_BASE = "";',
        'const AUDIO_BASE = "audio/";',
        'const SITE_GATE_HASH = "";',
        "const SITE_GATE_ITER = 310000;",
        'const GH_PAT_CIPHER = "";',
        'const GH_PAT_SALT = "";',
        'const GH_PAT_IV = "";',
    ]
    (tmp / "site" / "js" / "config.js").write_text(
        chr(10).join(lines) + chr(10), encoding="utf-8")

    root = str(tmp / "site")

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=root, **kw)

        def log_message(self, *_):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, tmp, f"http://127.0.0.1:{port}/index.html"


def render(url: str, slug: str, reel: int, lang: str, out: Path,
           audio: Path | None = None, opener: Path | None = None,
           endcard: Path | None = None, channel: str = "chrome",
           keep: Path | None = None) -> dict:
    from playwright.sync_api import sync_playwright

    work = Path(keep) if keep else Path(tempfile.mkdtemp(prefix="reel-"))
    work.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        launch = {"headless": True}
        if channel:
            launch["channel"] = channel        # the Chrome already installed
        browser = p.chromium.launch(**launch)
        try:
            # Viewport WIDTH is what sizes the card, not height — the stage is
            # what is left after the two side panels, and the frame is an
            # aspect box inside it. Measured: a 900px viewport gives a 224px
            # card, which screenshots at 448 and then has to be blown up 2.4x
            # to reach 1080 — visibly soft type on every frame. At 1600 the
            # card is 810 CSS px, so a 2x shot is 1620 native and ffmpeg scales
            # DOWN to 1080, which is the direction that costs nothing.
            page = browser.new_page(
                viewport={"width": 1600, "height": 2600},
                device_scale_factor=2)
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            # Name the login if there is one. Without this the failure is a
            # thirty-second Playwright timeout about an unstable element.
            blocked = (page.locator("#capture").count() == 0
                       or not page.locator("#capture").is_visible())
            if blocked and page.locator("input[type=password]").count():
                raise SystemExit(
                    "The studio at this URL is behind its password gate, so "
                    "there is no reel to photograph. Run without --url and "
                    "the renderer will serve its own un-gated copy instead.")
            info = page.evaluate(STUDIO_JS, [slug, reel, lang])
            shots: list[tuple[Path, float]] = []
            holds = info["holds"] or []
            for i, scene in enumerate(info["scenes"]):
                seconds = float(holds[i] if i < len(holds) else 4.0)
                shots.append((_shot(page, i, work), max(1.2, seconds)))
        finally:
            browser.close()

    # The generated cards top and tail the reel. Both are optional so the
    # pipeline runs before the image step has ever been called, and both are
    # held for a fixed beat rather than a measured one — neither is narrated.
    stills: list[tuple[Path, float]] = []
    if opener and Path(opener).exists():
        stills.append((Path(opener), 2.0))
    stills += shots
    if endcard and Path(endcard).exists():
        stills.append((Path(endcard), 2.5))

    out.parent.mkdir(parents=True, exist_ok=True)
    _ffmpeg_build(stills, out, audio)
    if not keep:
        shutil.rmtree(work, ignore_errors=True)

    total = sum(s for _, s in stills) - XFADE * max(0, len(stills) - 1)
    return {"company": info["company"], "scenes": len(shots),
            "seconds": round(total, 1), "out": str(out),
            "ready": info.get("ready"),
            "titles": info.get("titles") or [],
            "description": info.get("description") or "",
            "hashtags": info.get("hashtags") or []}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="",
                    help="an already-running server to drive. Omit it and the "
                         "renderer serves its own un-gated copy, which is the "
                         "normal case")
    ap.add_argument("--port", type=int, default=8771,
                    help="port for that temporary server")
    ap.add_argument("--slug", help="the IPO; omit with --reel 7")
    ap.add_argument("--reel", type=int, required=True)
    ap.add_argument("--lang", default="en", choices=["en", "hi", "te"])
    ap.add_argument("--out", type=Path)
    ap.add_argument("--audio", type=Path, help="narration mp3 to mux in")
    ap.add_argument("--opener", type=Path, help="generated title card")
    ap.add_argument("--endcard", type=Path, help="generated subscribe card")
    ap.add_argument("--thumbnail", type=Path, help="thumbnail to upload with it")
    ap.add_argument("--channel", default="chrome",
                    help='browser channel; "" for playwright\'s chromium')
    ap.add_argument("--keep", type=Path,
                    help="keep the scene PNGs in this directory")
    ap.add_argument("--queue", action="store_true",
                    help="add the finished video to the publish queue for "
                         "review, with the studio's own title and description")
    args = ap.parse_args()

    out = args.out or (BACKEND / "out" / "video" /
                       f"{args.slug or 'market'}-r{args.reel}-{args.lang}.mp4")
    if not shutil.which("ffmpeg"):
        print("ffmpeg is not on PATH — it does the transitions and the mux.",
              file=sys.stderr)
        return 1

    httpd = tmp = None
    url = args.url
    if not url:
        httpd, tmp, url = _serve_gateless(port=args.port)
    try:
        got = render(url, args.slug or "", args.reel, args.lang, out,
                     audio=args.audio, opener=args.opener,
                     endcard=args.endcard, channel=args.channel,
                     keep=args.keep)
    finally:
        if httpd:
            httpd.shutdown()
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)

    if args.queue:
        # Queued, never uploaded. Rendering is automatic; publishing is a
        # decision, and this command does not get to make it.
        from ipopulse import pubqueue
        titles = got["titles"]
        item = pubqueue.add(
            slug=args.slug or "market", reel=args.reel, lang=args.lang,
            video=out, company=got["company"], seconds=got["seconds"],
            title=titles[0] if titles else f"{got['company']} IPO",
            description=got["description"],
            tags=got["hashtags"], thumbnail=args.thumbnail)
        got["queued_as"] = item["id"]
        got["status"] = item["status"]

    print(json.dumps({k: v for k, v in got.items()
                      if k not in ("description", "titles", "hashtags")},
                     indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
