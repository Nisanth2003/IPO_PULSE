"""Shrink the narration clips Gemini writes as WAV under an .mp3 name.

WHY THIS EXISTS AT ALL
----------------------
voice.py has two providers and they do not return the same thing. ElevenLabs
returns real MP3. Gemini TTS returns raw little-endian PCM, which `_wav()`
wraps in a 44-byte RIFF header because a container is what an editor can open
(voice.py:905). Both then get written under the name `asset_name()` computes,
and that name ends in `.mp3` regardless — so a Gemini clip is RIFF/WAVE bytes
inside a file called .mp3, and nothing downstream notices because every player
sniffs content rather than trusting the extension.

Measured on this machine, 30 August 2026:

    esds-software-solution-r1-en-d6ad422b.mp3   5.7 MB   118.4 s   RIFF/WAVE
    an equivalent ElevenLabs read of the same length   ~1.4 MB     ID3/MP3

That is a 4x tax on every clip Gemini speaks, and Gemini is the DEFAULT
provider for a strictly-free setup (voice.py:95) — so it is most of them.

WHY IT IS WORTH A TOOL
----------------------
Clips live on Cloudflare R2. Requests, egress and Workers invocations on the
free tier are all far past what a six-reel channel can reach; the one limit
that actually binds is **10 GB of storage**. A full 54-clip run is roughly
180 MB as WAV and roughly 45 MB as MP3, which is the difference between 55
runs and 220 runs before the cap. There is no other change in this project
that saves that much for this little, which is why the transcode is a step
rather than a someday.

WHY IN PLACE, UNDER THE SAME NAME
---------------------------------
The filename is not ours to choose. It is `<slug>-r<reel>-<lang>-<hash8>.mp3`
where hash8 is SHA-256 over the SCRIPT TEXT, not over the audio (voice.py
`script_hash`), and studio.js recomputes the identical string in the browser to
fetch the clip. Rename anything — even to the honest `.wav` — and the published
site asks for a file that does not exist and goes silently mute. So this
rewrites bytes and leaves the name alone. The hash still describes the script,
which is all it ever claimed to describe.

    python tools/transcode_audio.py --dry-run
    python tools/transcode_audio.py --dir out/audio --bitrate 96k --jobs 4

Needs ffmpeg on PATH. It is preinstalled on GitHub's ubuntu runners, so CI
needs no setup step.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import struct
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ── the encode, and why these three numbers ────────────────────────────────
#
#   -b:a 96k    Transparent for one voice reading prose. Speech has none of
#               the cymbal transients that make 96k audible on music, and the
#               source is a TTS model's own output rather than a microphone —
#               there is no room tone or hiss for the encoder to spend bits on.
#               128k would cost a third more storage for a difference nobody
#               can hear on a phone speaker, which is where reels are watched.
#
#   -ac 1       Gemini returns mono (voice.py `_wav` defaults channels=1).
#               Encoding it as stereo would store the identical signal twice.
#
#   -ar 24000   The source rate, straight from the mime type Gemini sends
#               (`audio/L16;codec=pcm;rate=24000`). Resampling UP to 44.1k —
#               which is what `OUTPUT_FORMAT = "mp3_44100_128"` would imply if
#               anyone copied it here — cannot add information that was never
#               sampled. It only adds bytes. 24 kHz is a legal MPEG-2 Layer III
#               rate and every browser and editor tested plays it; if some
#               player ever balks, this flag is the one lever to move.
DEFAULT_BITRATE = "96k"
SAMPLE_RATE = "24000"

# Read enough for the RIFF/WAVE pair: "RIFF" + 4 size bytes + "WAVE".
SNIFF = 12

# min(4, cpu_count). LAME is single-threaded for a mono speech stream — its own
# -threads does nothing here — so parallelism has to come from running several
# ffmpegs, and clips are completely independent. Past four the disk becomes the
# limit and the extra processes only compete for it.
DEFAULT_JOBS = 4

BITRATE_RE = re.compile(r"^\d+k?$", re.I)


def sniff(path: Path) -> str:
    """What is ACTUALLY in this file: "wav", "mp3", or "other".

    The extension is a lie on purpose here (see the module docstring), so the
    only honest source of truth is the first few bytes. Getting this wrong in
    either direction is a real cost:

      * a WAV read as MP3 stays 4x too big and the whole exercise is pointless
      * an MP3 read as WAV gets re-encoded, which is generation loss — lossy
        into lossy, throwing away a little more of a read that cost real API
        quota — in exchange for approximately zero bytes saved.

    So MP3 is recognised two ways, because both appear in the wild: an ID3v2
    tag (what ElevenLabs returns) or a bare MPEG frame sync, 11 set bits, for a
    stream with no tag at all.
    """
    try:
        with path.open("rb") as fh:
            head = fh.read(SNIFF)
    except OSError:
        return "other"
    if len(head) >= SNIFF and head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head[:3] == b"ID3":
        return "mp3"
    # Frame sync: 0xFF then the top three bits of the next byte set.
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return "mp3"
    return "other"


def wav_seconds(path: Path) -> float:
    """Duration from the RIFF header alone — no ffprobe, no subprocess.

    Only used to project the saving for --dry-run, where spawning ffprobe once
    per clip would make a report that changes nothing slower than the run that
    changes everything. Walks the chunk list rather than assuming the canonical
    44-byte layout, because a WAV that arrived from anywhere but voice.py's own
    `_wav()` may carry a LIST or fact chunk before the data.

    Returns 0.0 if the header does not parse; the caller falls back to the
    known 24 kHz mono 16-bit byte rate, which is what this project actually
    produces.
    """
    try:
        with path.open("rb") as fh:
            if fh.read(4) != b"RIFF":
                return 0.0
            fh.seek(12)
            byte_rate = 0
            while True:
                header = fh.read(8)
                if len(header) < 8:
                    return 0.0
                cid, size = struct.unpack("<4sI", header)
                if cid == b"fmt " and size >= 16:
                    fmt = fh.read(size)
                    byte_rate = struct.unpack("<I", fmt[8:12])[0]
                elif cid == b"data":
                    return size / byte_rate if byte_rate else 0.0
                else:
                    fh.seek(size + (size & 1), os.SEEK_CUR)
    except (OSError, struct.error, IndexError):
        return 0.0


def projected(path: Path, bitrate: str) -> int:
    """Roughly what this clip will weigh once encoded, for --dry-run only."""
    seconds = wav_seconds(path)
    if seconds <= 0:
        # 24 kHz mono 16-bit is 48,000 bytes a second; subtract the header's
        # worth and the estimate is close enough to inform a decision.
        seconds = max(0.0, path.stat().st_size - 44) / 48_000
    kbps = int(bitrate.lower().rstrip("k"))
    return int(seconds * kbps * 1000 / 8)


def transcode(path: Path, bitrate: str) -> tuple[bool, int, int, str]:
    """RIFF -> MP3, in place, same filename. -> (ok, before, after, note).

    ── THE FAILURE THIS IS SHAPED AROUND ─────────────────────────────────
    Writing ffmpeg's output straight over the input would mean a killed
    process, a full disk or a codec error leaves a truncated file under the
    only name the site knows — and the audio is NOT reproducible for free. Each
    clip cost Gemini free-tier requests (capped per DAY, not per token) or
    ElevenLabs characters off a monthly cap, and the script that produced it
    may already have been re-worded, which changes the hash and therefore the
    filename. A destroyed clip can be genuinely unrecoverable.

    So: encode to a sibling temp file, prove the result is a non-empty file
    that begins with a real MP3 signature, and only then os.replace() it over
    the original. os.replace is atomic on POSIX and on Windows (MoveFileEx with
    REPLACE_EXISTING), and the temp file is deliberately in the SAME directory
    so the rename never crosses a filesystem boundary and degrades into a
    copy — which is exactly the non-atomic write this is avoiding.

    On any failure the original is left untouched and the temp is removed. A
    clip that will not encode is a clip to look at, not a clip to lose.
    """
    before = path.stat().st_size
    # The pid keeps two concurrent runs of this tool from colliding on the same
    # temp name; the .mp3 tail keeps the name honest if a crash ever leaves one.
    tmp = path.with_name(f"{path.name}.transcode-{os.getpid()}.mp3")

    cmd = [
        "ffmpeg",
        # -nostdin or a backgrounded ffmpeg can swallow the terminal; -y
        # because the temp name is ours and a stale one must not prompt.
        "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(path),
        "-vn",                       # ignore any cover art a tagged file carries
        "-c:a", "libmp3lame",
        "-b:a", bitrate,
        "-ac", "1",
        "-ar", SAMPLE_RATE,
        # Explicit container: the temp name ends in .mp3 today, but inferring
        # the format from a filename is exactly the habit that produced the WAV
        # -called-.mp3 problem in the first place.
        "-f", "mp3",
        str(tmp),
    ]

    try:
        done = subprocess.run(cmd, capture_output=True, text=True)
        if done.returncode != 0:
            # ffmpeg's own last stderr line, NOT its exit code, is what names
            # the fix. The code is worse than useless on Windows, where a
            # failure surfaces as an unsigned 32-bit value like 3199971767 that
            # looks alarming and means nothing; it is kept only for the case
            # where ffmpeg died without saying anything at all.
            tail = (done.stderr or "").strip().splitlines()
            reason = tail[-1] if tail else f"exit {done.returncode}, no message"
            return False, before, 0, f"ffmpeg failed: {reason}"

        # Exit 0 is necessary and not sufficient — verify the artefact itself
        # before anything irreversible happens to the original.
        if not tmp.exists() or tmp.stat().st_size == 0:
            return False, before, 0, "ffmpeg wrote nothing"
        after = tmp.stat().st_size
        if sniff(tmp) != "mp3":
            return False, before, after, "output is not an MP3 stream"

        os.replace(tmp, path)
        return True, before, after, ""
    except OSError as err:
        return False, before, 0, str(err)
    finally:
        # Belt and braces: a successful os.replace consumed the temp, anything
        # else must not leave one behind for the next run to trip over.
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:,.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} GB"


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Transcode Gemini's WAV-inside-.mp3 clips to real MP3, "
                    "in place, under the same filename.")
    ap.add_argument("--dir", default="out/audio",
                    help="directory of clips to scan (default: out/audio)")
    ap.add_argument("--bitrate", default=DEFAULT_BITRATE,
                    help=f"audio bitrate for the encode (default: "
                         f"{DEFAULT_BITRATE}; transparent for speech)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would convert and the projected saving, "
                         "and change nothing")
    ap.add_argument("--jobs", type=int, default=0,
                    help=f"parallel ffmpeg processes (default: "
                         f"min({DEFAULT_JOBS}, cpu count))")
    args = ap.parse_args()

    if not BITRATE_RE.match(args.bitrate):
        print(f"error: --bitrate {args.bitrate!r} is not a bitrate - "
              f"write it like 96k or 128k.", file=sys.stderr)
        return 2

    root = Path(args.dir)
    if not root.is_dir():
        print(f"error: no such directory: {root}", file=sys.stderr)
        return 2

    # Checked BEFORE any work, and reported as one line rather than the
    # FileNotFoundError traceback subprocess would raise per clip. A missing
    # binary is a setup problem with a one-sentence fix, not a bug report.
    if not args.dry_run and shutil.which("ffmpeg") is None:
        print("error: ffmpeg is not on PATH - install ffmpeg, or note that it "
              "is preinstalled on GitHub runners.", file=sys.stderr)
        return 2

    clips = sorted(root.glob("*.mp3"))
    if not clips:
        print(f"no .mp3 files in {root}")
        return 0

    wavs: list[Path] = []
    skipped: list[Path] = []
    strange: list[Path] = []
    for clip in clips:
        kind = sniff(clip)
        if kind == "wav":
            wavs.append(clip)
        elif kind == "mp3":
            skipped.append(clip)
        else:
            strange.append(clip)

    skipped_bytes = sum(p.stat().st_size for p in skipped + strange)

    tally = (f"{root}: {len(clips)} file(s) - {len(wavs)} RIFF/WAVE to "
             f"convert, {len(skipped)} already MP3")
    if strange:
        tally += f", {len(strange)} unrecognised"
    print(tally)
    for odd in strange:
        print(f"  ?  {odd.name} - not RIFF and not MP3, left alone")

    if not wavs:
        print("nothing to do.")
        return 0

    # ── the report that changes nothing ───────────────────────────────────
    if args.dry_run:
        before = sum(p.stat().st_size for p in wavs)
        after = sum(projected(p, args.bitrate) for p in wavs)
        for clip in wavs:
            size = clip.stat().st_size
            print(f"  ->  {clip.name}  {human(size)} -> ~"
                  f"{human(projected(clip, args.bitrate))}"
                  f"  ({wav_seconds(clip):.1f}s)")
        saved = before - after
        pct = (saved / before * 100) if before else 0.0
        print(f"\ndry run - nothing was written.")
        print(f"would convert {len(wavs)} clip(s) at {args.bitrate}: "
              f"{human(before)} -> ~{human(after)} "
              f"(saving ~{human(saved)}, {pct:.0f}%)")
        print(f"projection is duration x bitrate; the real encode lands within "
              f"a few percent of it.")
        return 0

    jobs = args.jobs if args.jobs > 0 else min(DEFAULT_JOBS, os.cpu_count() or 1)
    jobs = max(1, min(jobs, len(wavs)))

    total_before = total_after = 0
    done = failed = 0
    # ONE bad clip must not cost the other 53. Every result is collected and
    # reported; the run continues regardless and the exit code carries the bad
    # news at the end, where a human can see the whole picture and decide.
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {pool.submit(transcode, c, args.bitrate): c for c in wavs}
        for fut in as_completed(futures):
            clip = futures[fut]
            try:
                ok, before, after, note = fut.result()
            except Exception as err:                      # noqa: BLE001
                ok, before, after, note = False, 0, 0, repr(err)
            if ok:
                done += 1
                total_before += before
                total_after += after
                pct = (1 - after / before) * 100 if before else 0.0
                print(f"  ok  {clip.name}  {human(before)} -> {human(after)} "
                      f"(-{pct:.0f}%)")
            else:
                failed += 1
                print(f"  !!  {clip.name}  {note} - original left untouched",
                      file=sys.stderr)

    saved = total_before - total_after
    pct = (saved / total_before * 100) if total_before else 0.0
    print(f"\nconverted {done}, skipped {len(skipped) + len(strange)}, "
          f"failed {failed}")
    print(f"{human(total_before)} -> {human(total_after)} "
          f"(saved {human(saved)}, {pct:.0f}%)")
    if skipped_bytes:
        print(f"directory now {human(total_after + skipped_bytes)} "
              f"(was {human(total_before + skipped_bytes)})")
    # Non-zero when anything failed, so a CI step does not report green over a
    # clip that is still 4x too big.
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
