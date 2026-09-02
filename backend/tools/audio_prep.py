"""Get out/audio ready to record from, and keep it from growing forever.

WHAT THIS IS FOR
----------------
The daily loop is: narrate, record the reel with the narration in it, edit,
upload. That loop runs LOCALLY — `ipopulse serve` plus the studio's capture —
and this is the one step between `ipopulse narrate` and pressing record.

Three jobs, in the order they have to happen:

  1. SHRINK. Gemini TTS returns 24 kHz RIFF/WAVE bytes under the `.mp3` name
     the site derives (the name hashes the SCRIPT, not the audio, so it cannot
     be changed to match the container). Measured 30 Aug 2026: 5.7 MB for 118 s
     — about 4x the same speech from ElevenLabs. Transcoding is a flat 75% off.

  2. SWEEP. Clip names carry a hash of the script, and scripts interpolate live
     GMP and subscription. The moment a number moves, every affected clip
     becomes permanently unreachable — the studio derives a new name and asks
     for that instead. Those files are not "old versions", they are garbage the
     page can never request again, and without a sweep they accumulate at
     roughly one full run per GMP change.

  3. STAGE. Copy what survives into frontend/audio/, which is where
     `ipopulse serve` looks and what cli._audio_base auto-detects. That is what
     makes narration audible locally with no configuration and no cloud.

WHY THERE IS NO UPLOAD HERE
---------------------------
There deliberately is not one. Recording happens against a LOCAL studio, so the
clips only ever need to be same-origin — no bucket, no CORS, no account. That
is not a downgrade from hosting them: `capture.js` routes narration through
`createMediaElementSource`, which emits SILENCE for a cross-origin element the
server never granted, and a network stall mid-take would ruin a two-minute
recording. Local files have neither failure mode.

Archiving the clips so a re-edit does not have to re-synthesise them is a
separate problem with a much easier answer — it is a file backup, nothing
fetches it from a browser, so any synced folder does. See tools/r2_sync.py for
the day the studio is driven from the published site instead; the keep-set
logic below is shared with it precisely so the two can never disagree.

    python tools/audio_prep.py                 # shrink, stage, REPORT sweepable
    python tools/audio_prep.py --sweep         # ... and actually delete them
    python tools/audio_prep.py --dry-run       # change nothing at all
"""

from __future__ import annotations

import argparse
import importlib.util
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
REPO = BACKEND.parent


def _sibling(name: str):
    """Import a sibling tool by path.

    These are scripts in tools/, not an installed package, so a plain `import
    r2_sync` only works when cwd happens to be tools/. Loading by explicit path
    means this behaves the same run from backend/, from the repo root, or by
    absolute path — which matters because it is going to be typed by hand every
    morning rather than invoked by a workflow with a fixed working-directory.
    """
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:,.1f} GB"


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Shrink, sweep and stage the narration for a local "
                    "recording session.",
        epilog="Deleting a clip costs real voice quota to replace, so the "
               "sweep only reports unless you pass --sweep.")
    ap.add_argument("--dir", default="out/audio",
                    help="where ipopulse narrate wrote the clips "
                         "(default: out/audio)")
    ap.add_argument("--scripts", default="out/scripts.json",
                    help="the script book the keep-set is derived from "
                         "(default: out/scripts.json)")
    ap.add_argument("--stage-dir", default=None,
                    help="where the studio reads clips from "
                         "(default: frontend/audio)")
    ap.add_argument("--sweep", action="store_true",
                    help="actually delete clips the studio can no longer ask "
                         "for. Without this they are only reported.")
    ap.add_argument("--drop-status", default="listed",
                    help="statuses whose clips are finished work and may be "
                         "swept (default: listed). An issue that has already "
                         "listed will never be filmed again.")
    ap.add_argument("--no-transcode", action="store_true",
                    help="skip the WAV->MP3 step")
    ap.add_argument("--no-stage", action="store_true",
                    help="skip copying into frontend/audio/")
    ap.add_argument("--bitrate", default="96k")
    ap.add_argument("--dry-run", action="store_true",
                    help="report everything, change nothing")
    args = ap.parse_args()

    def resolve(raw: str) -> Path:
        p = Path(raw)
        if p.is_absolute():
            return p
        # cwd first, then backend/ — so this works from either place.
        return p if p.exists() else (BACKEND / raw)

    folder = resolve(args.dir)
    scripts = resolve(args.scripts)
    stage = Path(args.stage_dir) if args.stage_dir else (REPO / "frontend" / "audio")

    if not folder.is_dir():
        print(f"error: {folder} is not a directory - run `ipopulse narrate` "
              f"first.", file=sys.stderr)
        return 2

    sync = _sibling("r2_sync")
    clips = sorted(folder.glob("*.mp3"))
    if not clips:
        print(f"{folder} holds no clips - nothing to do.")
        return 0

    before = sum(p.stat().st_size for p in clips)
    print(f"{len(clips)} clip(s), {human(before)} in {folder}\n")

    # ── 1. shrink ────────────────────────────────────────────────────────
    if not args.no_transcode:
        tc = _sibling("transcode_audio")
        todo = [p for p in clips if tc.sniff(p) == "wav"]
        if not todo:
            print("shrink : nothing to convert, every clip is already MP3")
        elif args.dry_run:
            saving = sum(p.stat().st_size - tc.projected(p, args.bitrate)
                         for p in todo)
            print(f"shrink : would convert {len(todo)} WAV clip(s), "
                  f"saving ~{human(saving)}")
        else:
            done = failed = 0
            for p in todo:
                ok, _was, _now, err = tc.transcode(p, args.bitrate)
                if ok:
                    done += 1
                else:
                    failed += 1
                    print(f"         !! {p.name}: {err}", file=sys.stderr)
            after = sum(q.stat().st_size for q in folder.glob("*.mp3"))
            print(f"shrink : converted {done} clip(s)"
                  + (f", {failed} FAILED" if failed else "")
                  + f" - {human(before)} -> {human(after)}")

    # ── 2. sweep ─────────────────────────────────────────────────────────
    #
    # The keep-set is imported from r2_sync rather than reimplemented. It is
    # the same question ("what can the studio still ask for?") and two copies
    # of that rule would drift, with the failure being deleted audio.
    drop = {s.strip().lower() for s in (args.drop_status or "").split(",")
            if s.strip()}
    try:
        keep = sync.keep_set(scripts, drop)
    except Exception as err:                      # SyncError and friends
        # Refusing is correct: no keep-set means "delete everything", and a
        # missing scripts.json must never be able to say that.
        print(f"sweep  : SKIPPED - {err}", file=sys.stderr)
        keep = None

    stale: list[Path] = []
    if keep is not None:
        for p in sorted(folder.glob("*.mp3")):
            # Second brake, same as the bucket pruner: anything that is not
            # shaped like a generated clip is somebody's file, not ours.
            if not sync.ASSET_RE.match(p.name):
                continue
            if p.name not in keep:
                stale.append(p)

        freed = sum(p.stat().st_size for p in stale)
        if not stale:
            print(f"sweep  : nothing stale, all {len(keep)} reachable name(s) accounted for")
        elif args.sweep and not args.dry_run:
            for p in stale:
                p.unlink()
            print(f"sweep  : deleted {len(stale)} unreachable clip(s), freed {human(freed)}")
        else:
            print(f"sweep  : {len(stale)} clip(s) the studio can no longer ask "
                  f"for, {human(freed)}")
            for p in stale[:8]:
                print(f"         - {p.name}")
            if len(stale) > 8:
                print(f"         ... and {len(stale) - 8} more")
            print("         (pass --sweep to delete them)")

    # ── 3. stage ─────────────────────────────────────────────────────────
    if not args.no_stage:
        live = [p for p in sorted(folder.glob("*.mp3"))
                if keep is None or not sync.ASSET_RE.match(p.name)
                or p.name in keep]
        if args.dry_run:
            print(f"stage  : would place {len(live)} clip(s) in {stage}")
        else:
            stage.mkdir(parents=True, exist_ok=True)
            # Clear first, so a clip swept from out/audio does not linger here
            # and keep playing under a script that no longer exists.
            for old in stage.glob("*.mp3"):
                if sync.ASSET_RE.match(old.name):
                    old.unlink()
            for p in live:
                shutil.copy2(p, stage / p.name)
            total = sum(q.stat().st_size for q in stage.glob("*.mp3"))
            print(f"stage  : {len(live)} clip(s) in {stage} ({human(total)})")

    print("\nready - run `ipopulse serve` and record.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
