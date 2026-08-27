"""Narration from ElevenLabs, cached hard because it is billed per character.

── Why this takes text instead of writing it ──────────────────────────────

Every script in this project is generated in the browser: `voRupees`,
`voTakeGmp`, `scriptFor`, `strategyScript` all live in frontend/js/output.js
and nowhere else. There is no Python equivalent and there should not be one —
compute.js/compute.py is already a mirror pair somebody has to keep in step,
and a second mirror carrying seven hundred lines of prose logic would be a
worse bargain than any convenience it bought.

So the split is: the browser owns the words, this module owns the API key and
the money. `synthesize()` takes finished text. That is also why the studio
POSTs to /api/voice rather than asking a job to render a reel's narration —
the job would have no way to know what the narration says.

── Why the cache is not optional ──────────────────────────────────────────

ElevenLabs bills per character of input. The studio's normal rhythm is to
render a script, hear a number pronounced wrong, fix one field and render
again — which at full price would charge for the whole script every time. The
cache is keyed on the exact bytes that affect the output (text, voice, model,
settings), so those re-renders are free and only a genuine change costs.

It is a plain directory of mp3 files with no TTL, deliberately. An ai.py-style
30-day expiry would delete audio for a script that has not changed, and paying
twice for the same sentence because a calendar rolled over is not a cache, it
is a subscription.

── The spend guard ────────────────────────────────────────────────────────

Two limits, because a metered key behind an HTTP endpoint is a way to lose
money quietly:

  per request   MAX_CHARS, so one runaway paste cannot become one huge bill
  per month     ELEVENLABS_MONTHLY_CHAR_CAP, tracked in .cache/voice/ledger.json

The ledger counts only characters actually sent upstream — a cache hit spends
nothing and is not recorded. It is advisory arithmetic on this machine, not a
reading of your ElevenLabs balance; treat it as a tripwire, not an invoice.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any

from .store import CACHE_DIR

API_ROOT = "https://api.elevenlabs.io/v1"

VOICE_DIR = CACHE_DIR / "voice"
LEDGER = VOICE_DIR / "ledger.json"

# Multilingual by default because two of the three output languages are Hindi
# and Telugu, and the English-only models mispronounce Indian company names
# even in the English scripts. The faster/cheaper models exist for real-time
# use, which a pre-recorded narration is not.
DEFAULT_MODEL = "eleven_multilingual_v2"

# mp3 at 44.1kHz/128kbps: what every editor imports without complaint, and
# small enough that a bundle download is not a wait.
OUTPUT_FORMAT = "mp3_44100_128"

# The playbook's settings for this persona, in API terms (§8). Calm, mid-energy
# and credible: stability high-ish so a long read does not drift, style low
# because the script already carries the emotion, speed a touch under 1.0
# because authority reads slow.
DEFAULT_SETTINGS: dict[str, Any] = {
    "stability": 0.65,
    "similarity_boost": 0.75,
    "style": 0.0,
    "use_speaker_boost": True,
    "speed": 0.95,
}

# One long-form strategy script is the biggest thing that legitimately comes
# through here. Anything much past this is a mistake or a paste accident, and
# it is cheaper to refuse it than to charge for it.
MAX_CHARS = 8000

DEFAULT_MONTHLY_CAP = 100_000


class VoiceError(RuntimeError):
    """Anything that stops audio coming back, with a message worth showing."""


def configured() -> bool:
    return bool(api_key() and voice_id())


def api_key() -> str:
    return (os.getenv("ELEVENLABS_API_KEY") or "").strip()


def voice_id() -> str:
    return (os.getenv("ELEVENLABS_VOICE_ID") or "").strip()


def model() -> str:
    return (os.getenv("ELEVENLABS_MODEL") or DEFAULT_MODEL).strip()


def monthly_cap() -> int:
    raw = (os.getenv("ELEVENLABS_MONTHLY_CHAR_CAP") or "").strip()
    try:
        return int(raw) if raw else DEFAULT_MONTHLY_CAP
    except ValueError:
        return DEFAULT_MONTHLY_CAP


# ── the spend ledger ───────────────────────────────────────────────────────

def _month() -> str:
    return date.today().strftime("%Y-%m")


def spent() -> int:
    """Characters billed this calendar month, per this machine's own count."""
    try:
        blob = json.loads(LEDGER.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    return int(blob.get(_month(), 0))


def _record(chars: int) -> None:
    try:
        blob = json.loads(LEDGER.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        blob = {}
    blob[_month()] = int(blob.get(_month(), 0)) + chars
    # Keep the last few months and drop the rest: it is a tripwire, not a
    # financial record, and an unbounded file that nothing ever reads is just
    # a slow leak.
    for key in sorted(blob)[:-6]:
        blob.pop(key, None)
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(blob, indent=1), encoding="utf-8")


def budget() -> dict[str, int]:
    cap = monthly_cap()
    used = spent()
    return {"cap": cap, "used": used, "left": max(0, cap - used)}


# ── the cache ──────────────────────────────────────────────────────────────

def _key(text: str, vid: str, mdl: str, settings: dict[str, Any]) -> str:
    """Hash everything that can change the audio, and nothing that cannot.

    Settings are serialised sorted so an identical dict written in a different
    order does not read as a different voice and re-bill the whole script.
    """
    payload = json.dumps(
        {"t": text, "v": vid, "m": mdl, "s": settings},
        sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def cached_path(text: str, vid: str = "", mdl: str = "",
                settings: dict[str, Any] | None = None) -> Path:
    vid = vid or voice_id()
    mdl = mdl or model()
    merged = {**DEFAULT_SETTINGS, **(settings or {})}
    return VOICE_DIR / f"{_key(text, vid, mdl, merged)}.mp3"


# ── the call ───────────────────────────────────────────────────────────────

def synthesize(text: str, vid: str = "", mdl: str = "",
               settings: dict[str, Any] | None = None,
               force: bool = False) -> tuple[bytes, bool]:
    """Text -> mp3 bytes, and whether it came from the cache.

    Raises VoiceError with the upstream message intact on any refusal. That is
    deliberate: this is an API nobody here can test against every account tier,
    and "ElevenLabs said 401 voice_not_found" is a fixable message where
    "could not generate audio" is a support ticket.
    """
    text = (text or "").strip()
    if not text:
        raise VoiceError("Nothing to say — the script was empty.")
    if len(text) > MAX_CHARS:
        raise VoiceError(
            f"{len(text)} characters is past the {MAX_CHARS} cap for one "
            f"request. Split it, or raise MAX_CHARS if this is really one read.")

    key = api_key()
    vid = vid or voice_id()
    mdl = mdl or model()
    merged = {**DEFAULT_SETTINGS, **(settings or {})}

    path = cached_path(text, vid, mdl, merged)
    if path.exists() and not force:
        return path.read_bytes(), True

    if not key:
        raise VoiceError("ELEVENLABS_API_KEY is not set in .env.")
    if not vid:
        raise VoiceError(
            "ELEVENLABS_VOICE_ID is not set. Run `ipopulse voice --voices` to "
            "list the voices on this account and copy the id of yours.")

    left = budget()["left"]
    if len(text) > left:
        raise VoiceError(
            f"This would spend {len(text)} characters and only {left} are left "
            f"under ELEVENLABS_MONTHLY_CHAR_CAP ({monthly_cap()}). Raise the "
            f"cap if that is deliberate.")

    body = json.dumps({
        "text": text,
        "model_id": mdl,
        "voice_settings": merged,
    }).encode("utf-8")

    request = urllib.request.Request(
        f"{API_ROOT}/text-to-speech/{vid}?output_format={OUTPUT_FORMAT}",
        data=body,
        headers={
            "xi-api-key": key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        },
        method="POST")

    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            audio = response.read()
    except urllib.error.HTTPError as err:
        raise VoiceError(_explain(err)) from err
    except OSError as err:
        raise VoiceError(f"Could not reach api.elevenlabs.io — {err}") from err

    if not audio:
        raise VoiceError("ElevenLabs returned no audio and no error.")

    # Bill first, write second. A crash between the two must not leave the
    # ledger short — undercounting spend is the failure that costs money.
    _record(len(text))
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_bytes(audio)
    return audio, False


def _explain(err: urllib.error.HTTPError) -> str:
    """Turn an HTTPError into something that names the actual fix.

    ElevenLabs puts a machine-readable reason in the body; the status alone is
    ambiguous in exactly the cases that matter (401 is a bad key OR a key
    without permission for this voice).
    """
    detail = ""
    try:
        blob = json.loads(err.read().decode("utf-8", "replace"))
        node = blob.get("detail", blob)
        if isinstance(node, dict):
            detail = str(node.get("message") or node.get("status") or "")
        else:
            detail = str(node)
    except (ValueError, OSError):
        pass

    hint = {
        401: " — check ELEVENLABS_API_KEY, and that the key's account owns "
             "this voice id.",
        403: " — the key is valid but not permitted to use this voice or model.",
        404: " — no such voice id. `ipopulse voice --voices` lists the real ones.",
        422: " — the request shape was rejected. If your account predates the "
             "`speed` voice setting, that is the field to drop first.",
        429: " — rate limited, or the account is out of characters.",
    }.get(err.code, "")
    return f"ElevenLabs said {err.code}: {detail or 'request refused'}{hint}"


def voices() -> list[dict[str, str]]:
    """Every voice this key can use. Read-only, and free."""
    key = api_key()
    if not key:
        raise VoiceError("ELEVENLABS_API_KEY is not set in .env.")
    request = urllib.request.Request(
        f"{API_ROOT}/voices", headers={"xi-api-key": key})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            blob = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        raise VoiceError(_explain(err)) from err
    except OSError as err:
        raise VoiceError(f"Could not reach api.elevenlabs.io — {err}") from err

    out = []
    for v in blob.get("voices", []):
        out.append({
            "id": v.get("voice_id", ""),
            "name": v.get("name", ""),
            # `category` is how you tell your own clone from a stock voice,
            # which is the distinction §3.1 of the playbook turns on.
            "category": v.get("category", ""),
        })
    return out
