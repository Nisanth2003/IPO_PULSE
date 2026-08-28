"""Render one real script three ways so a person can decide by ear.

The question this settles: is the read flat because of the SCRIPT or because of
the VOICE? Those need completely different fixes — one is output.js, the other
is buying a voice — and no amount of reasoning about sliders answers it. So:

    A  raw          exactly what the pipeline narrates today. The control.
    B  normalised   speech.for_speech only — currency spoken in the right place,
                    percent and multiplier glyphs said out loud, doubled dandas
                    collapsed, leading zeros dropped. NO tags.
    C  + tags       B plus a few eleven_v3 audio tags at structural boundaries.

A vs B isolates pronunciation. B vs C isolates performance. Listening to all
three in order tells you which lever is actually worth building out.

    python tools/ear_test.py --slug esds-software-solution --reel 1 --lang hi

Costs real characters — roughly 3x the script length. It prints the bill before
spending and refuses past --budget.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ipopulse import speech, voice as tts  # noqa: E402
from ipopulse.cli import load_dotenv  # noqa: E402

# Without this every provider reports "not configured" and the run costs nothing
# and produces nothing — the keys live in .env and nothing else here reads it.
# `ipopulse ...` gets this for free from its own entry point; a standalone tool
# under tools/ does not.
load_dotenv()

# Deliberately few, and only at structural boundaries.
#
# Tags are a performance instruction, not decoration: one per beat that actually
# turns. Sprinkling them every sentence is how you get a read that lurches, and
# v3 will also SPEAK a tag it does not recognise, so an invented one is a bug you
# hear. These four are from ElevenLabs' documented set.
OPEN_TAG = "[thoughtful]"
PIVOT_TAG = "[emphatic]"
CLOSE_TAG = "[thoughtful]"


def add_tags(text: str) -> str:
    """Open, pivot before the last third, and settle before the final sentence.

    Sentence splitting on danda AND full stop because the Hindi and Telugu
    scripts use । while English uses '.' — one function has to handle both.
    """
    parts = [p for p in re.split(r"(?<=[।.])\s*", text) if p.strip()]
    if not parts:
        return text
    if len(parts) == 1:
        return f"{OPEN_TAG} {parts[0]}"

    pivot = max(1, int(len(parts) * 0.6))
    last = len(parts) - 1
    out = []
    for i, p in enumerate(parts):
        if i == 0:
            out.append(f"{OPEN_TAG} {p}")
        elif i == pivot and pivot != last:
            out.append(f"{PIVOT_TAG} {p}")
        elif i == last:
            out.append(f"{CLOSE_TAG} {p}")
        else:
            out.append(p)
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scripts", default="out/scripts_fixed.json")
    ap.add_argument("--slug", default="esds-software-solution")
    ap.add_argument("--reel", default="1")
    ap.add_argument("--lang", default="hi")
    ap.add_argument("--out", default="out/eartest")
    ap.add_argument("--budget", type=int, default=2600)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    book = json.loads(Path(args.scripts).read_text(encoding="utf-8"))
    try:
        raw = book[args.slug][str(args.reel)][args.lang].strip()
    except KeyError:
        print(f"error: no {args.lang} script for {args.slug} reel {args.reel}",
              file=sys.stderr)
        return 2

    normalised = speech.for_speech(raw, args.lang)
    tagged = add_tags(normalised)

    variants = [("A-raw", raw), ("B-normalised", normalised), ("C-tagged", tagged)]
    total = sum(len(t) for _, t in variants)

    print(f"{args.slug} reel {args.reel} [{args.lang}]")
    for name, text in variants:
        print(f"  {name:<14} {len(text):>5} chars")
    print(f"  {'TOTAL':<14} {total:>5} chars")

    found = speech.problems(raw, args.lang)
    if found:
        print("\nscript problems worth fixing in output.js:")
        for p in found:
            print(f"  * {p}")

    if total > args.budget:
        print(f"\nrefusing: {total} exceeds --budget {args.budget}.",
              file=sys.stderr)
        return 1
    if args.dry_run:
        print("\n(dry run — nothing spent)")
        for name, text in variants:
            print(f"\n----- {name} -----\n{text}")
        return 0

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    billed = 0
    for name, text in variants:
        dest = out_dir / f"{args.slug}-r{args.reel}-{args.lang}-{name}.mp3"
        try:
            audio, hit, used, fmt = tts.synthesize(
                text, lang=args.lang, provider="elevenlabs")
        except tts.VoiceError as err:
            print(f"  ! {name}: {err}", file=sys.stderr)
            continue
        dest.write_bytes(audio)
        if not hit:
            billed += len(text)
        print(f"  {dest.name}  {len(audio):,} B  via {used}"
              f"{' (cached)' if hit else ''}")
        # The exact text that was spoken, next to the audio. Without this you are
        # comparing three files and guessing which was which by the minute mark.
        dest.with_suffix(".txt").write_text(text, encoding="utf-8")

    print(f"\n{billed:,} characters billed. Files in {out_dir}")
    print("Listen A -> B -> C. A/B is pronunciation; B/C is performance.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
