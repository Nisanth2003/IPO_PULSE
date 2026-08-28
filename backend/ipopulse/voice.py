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

import base64
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any

from .store import CACHE_DIR

API_ROOT = "https://api.elevenlabs.io/v1"
GEMINI_ROOT = "https://generativelanguage.googleapis.com/v1beta"

VOICE_DIR = CACHE_DIR / "voice"
LEDGER = VOICE_DIR / "ledger.json"

# ── two providers, and which one a free channel can actually use ───────────
#
# Both were checked against their own terms on 27 August 2026, and they land in
# opposite places on the only question that matters for a monetised channel:
#
#   ElevenLabs  "The free plan does not include a commercial license and cannot
#               be used for any commercial purpose", and content made outside a
#               paid subscription "cannot be used commercially" — before or
#               after. So free ElevenLabs audio cannot go in a monetised video
#               at all. Paid plans do include the licence.
#
#   Gemini      The Additional Terms say only "Google won't claim ownership
#               over that content", and state the Services are "for developers
#               building ... for professional or business purposes". The
#               unpaid/paid split governs HOW GOOGLE USES YOUR DATA, not what
#               you may do with the output. Free tier output is usable
#               commercially.
#
# So for a strictly-free setup Gemini is not the last resort, it is the only
# one of the two that works — hence the default order. ElevenLabs stays first
# choice the day a paid plan exists, because it can clone the owner's own voice
# and playbook §3.1 makes that the policy-safe branch for finance narration;
# Gemini offers prebuilt voices only, which is the branch that needs the script
# to keep making no claim to credentials.
#
# The price of the Gemini free tier is not money: unpaid quota means prompts
# and outputs are used to improve Google's products and "human reviewers may
# read, annotate, and process" them. IPO scripts are assembled from public
# market data, so that is an acceptable trade here — but it is a real one, and
# it is the reason this is a documented choice rather than a silent default.
DEFAULT_PROVIDERS = ("gemini", "elevenlabs")

# Gemini TTS is in Preview, and its own docs name the consequences: quality
# drifts on outputs "longer than a few minutes", it "occasionally returns text
# tokens instead of audio tokens, causing the server to fail the request with a
# 500", and a vague prompt can be rejected or read aloud as instructions.
# Retries and the preamble below exist because of those three, in that order.
GEMINI_MODEL = "gemini-2.5-flash-preview-tts"
# Any of the 30 prebuilt names. Charon is "Informative" per the voice list,
# which is the register these scripts are written in; Rasalgethi is the other
# informative option and Alnilam/Kore read firmer if this sounds too soft.
GEMINI_VOICE = "Charon"
GEMINI_RETRIES = 3

# Gemini TTS returns raw little-endian 16-bit PCM, not a container, and the
# sample rate arrives in the mime type (audio/L16;codec=pcm;rate=24000). A
# 44-byte RIFF header is all that stands between those bytes and a file every
# editor will open, so this wraps rather than shelling out to a converter.
GEMINI_FALLBACK_RATE = 24_000

# ── which model can speak which language ───────────────────────────────────
#
# Checked against elevenlabs.io/docs/models on 27 August 2026. This table is
# the whole reason the per-language model override exists, and it is not a
# detail — it decides whether a third of this channel's output is possible.
#
#   eleven_multilingual_v2   29 languages. Has Hindi. Has TAMIL. **No Telugu.**
#   eleven_flash_v2_5        those 29 + hu/no/vi. Still no Telugu.
#   eleven_v3                70+ languages, Telugu (tel) explicitly listed.
#
# So Telugu is not a tuning problem, it is a model requirement: nothing below
# v3 can say it. An unsupported language does not raise — it returns confident
# nonsense — which is exactly the failure a table like this can catch locally
# before it costs characters and a listen.
#
# Only the languages this project outputs are tracked. `None` means "70+,
# assume anything we produce", which is honest about the fact that the docs
# give a list too long and too fluid to pin here.
MODEL_LANGS: dict[str, set[str] | None] = {
    "eleven_multilingual_v2": {"en", "hi"},
    "eleven_flash_v2_5": {"en", "hi"},
    "eleven_flash_v2": {"en"},
    "eleven_v3": None,
    "eleven_v3_conversational": None,
}

# Per-model, not one number: eleven_v3 caps a request at 5,000 characters where
# multilingual_v2 allows 10,000 and flash 40,000. A flat 8,000 cap looked safe
# and was not — it let a Telugu long-form script through to v3, which would
# have rejected it upstream after the request was already on the wire.
MODEL_CHAR_LIMITS: dict[str, int] = {
    "eleven_v3": 5_000,
    "eleven_v3_conversational": 5_000,
    "eleven_multilingual_v2": 10_000,
    "eleven_flash_v2_5": 40_000,
    "eleven_flash_v2": 30_000,
}
# The most restrictive real limit, for a model not in the table. Refusing early
# beats discovering the ceiling from a 400 after the characters are counted.
FALLBACK_CHAR_LIMIT = 5_000

# Multilingual v2 for English and Hindi: it is the most stable on long-form
# reads, which is what a 6-10 minute narration is, and the English-only models
# mispronounce Indian company names even in the English scripts.
#
# v3 for Telugu because it is the only model that speaks it. It costs the same
# per character as multilingual v2 ($0.10/1K) but caps a request at 5,000
# characters, so a long Telugu script has to be split where the English one
# need not be.
DEFAULT_MODEL = "eleven_multilingual_v2"
DEFAULT_MODEL_BY_LANG = {"te": "eleven_v3"}

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

DEFAULT_MONTHLY_CAP = 100_000


def char_limit(mdl: str = "") -> int:
    """The upstream per-request character ceiling for this model."""
    return MODEL_CHAR_LIMITS.get(mdl or model(), FALLBACK_CHAR_LIMIT)


# Kept as a name because the API layer reports it, but it is now the ceiling of
# the DEFAULT model rather than a project-wide constant.
MAX_CHARS = MODEL_CHAR_LIMITS[DEFAULT_MODEL]


def speaks(mdl: str, lang: str) -> bool:
    """Can this model say this language? Unknown model or lang -> assume yes.

    Only ever used to refuse BEFORE spending characters, so the bias is
    deliberate: a wrong `False` would block work the API would have done, and a
    wrong `True` costs one listen. Guessing 'no' is the more expensive mistake.
    """
    if not lang:
        return True
    allowed = MODEL_LANGS.get(mdl, None)
    return True if allowed is None else lang in allowed


class VoiceError(RuntimeError):
    """Anything that stops audio coming back, with a message worth showing."""


def providers() -> list[str]:
    """Which providers to try, in order. IPOPULSE_VOICE_PROVIDERS overrides.

    Names not recognised are dropped rather than raising: a typo in an env var
    should not take the whole feature down, and `--plan` shows what resolved.
    """
    raw = (os.getenv("IPOPULSE_VOICE_PROVIDERS") or "").strip()
    if not raw:
        wanted = list(DEFAULT_PROVIDERS)
    else:
        wanted = [p.strip().lower() for p in re.split(r"[,\s]+", raw) if p.strip()]
    return [p for p in wanted if p in ("gemini", "elevenlabs")]


def available(provider: str) -> bool:
    """Is this provider actually usable right now?"""
    if provider == "gemini":
        return bool(gemini_key())
    if provider == "elevenlabs":
        return bool(api_keys() and voice_id())
    return False


def gemini_key() -> str:
    # Same variable ai.py reads, because it is the same key and the same free
    # quota. A second name would mean two places to paste it and one of them
    # silently stale.
    return (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()


def gemini_voice(lang: str = "") -> str:
    return _per_lang("GEMINI_TTS_VOICE", lang, GEMINI_VOICE)


def gemini_model(lang: str = "") -> str:
    return _per_lang("GEMINI_TTS_MODEL", lang, GEMINI_MODEL)


def gemini_style(lang: str = "") -> str:
    """The director's note prepended to the transcript.

    Gemini TTS is prompt-steered rather than parameter-steered: there is no
    stability slider, the delivery comes from words. This is the same persona
    the ElevenLabs settings aim at — calm, credible, unhurried — expressed the
    way this model takes direction.

    It deliberately describes a REGISTER and never a résumé. Playbook §3.1 bars
    an AI persona presenting itself as a human expert on finance, and the
    scripts had their tenure claims removed for exactly that reason; a style
    prompt that put "veteran analyst with twenty years on the desk" back in
    would reintroduce the thing the scripts were cleaned of.
    """
    override = _per_lang("GEMINI_TTS_STYLE", lang, "")
    if override:
        return override
    return (
        "Read the transcript below aloud as a market news presenter. "
        "Style: calm, measured and factual, with quiet authority. Not salesy, "
        "not dramatic, no rising excitement on numbers. "
        "Pace: unhurried, with a clear beat between sentences. "
        "Articulation: numbers and company names crisply and fully pronounced."
    )


def configured() -> bool:
    return any(available(p) for p in providers())


def api_key() -> str:
    """The first key. Kept for callers that only need to know one exists."""
    keys = api_keys()
    return keys[0] if keys else ""


def api_keys() -> list[str]:
    """Every key, in the order they will be tried.

    Read from ELEVENLABS_API_KEY — which may hold several separated by commas
    or whitespace — plus ELEVENLABS_API_KEY_2 … _5. Both spellings because a
    hosted panel is easier with one variable and a .env is easier to read with
    several, and neither should be the only way.

    ── What rotation is and is not for ──────────────────────────────────

    It buys resilience against a key that is momentarily or permanently unable
    to serve: a concurrency 429 (the plans allow 2-5 simultaneous requests), a
    key that has been revoked or rotated, a workspace that has run out mid-run.
    A paid key with a paid spare is the case this serves well.

    It does NOT buy quota. Stacking free keys to get past a monthly character
    allowance runs into two walls that no code here can move:

      * the free plan carries **no commercial licence at all** — see the note
        in .env.example. A monetised channel is a commercial purpose.
      * the arithmetic. Three free plans is 30,000 characters a month, which
        is roughly one day of three-language output.

    ── The wrinkle that bites ───────────────────────────────────────────

    A cloned voice belongs to ONE workspace. If the keys are separate accounts,
    ELEVENLABS_VOICE_ID exists in only one of them and every other key answers
    404 for it — so rotation across accounts needs the same voice cloned in
    each, or it degrades into a slower way to fail. Keys on one workspace share
    a quota, so rotating them helps with concurrency and nothing else.
    """
    found: list[str] = []
    raw = os.getenv("ELEVENLABS_API_KEY") or ""
    found.extend(p for p in re.split(r"[,\s]+", raw) if p)
    for n in range(2, 6):
        extra = (os.getenv(f"ELEVENLABS_API_KEY_{n}") or "").strip()
        if extra:
            found.append(extra)
    # Order-preserving dedupe: the same key pasted into two variables should
    # not be tried twice, which would double every retry delay for nothing.
    seen: set[str] = set()
    return [k for k in found if not (k in seen or seen.add(k))]


def script_hash(text: str) -> str:
    """First 8 hex of SHA-256 over the script — the whole cache-invalidation
    scheme in one function.

    MIRRORED IN JAVASCRIPT: studio.js `scriptHash()` must produce the identical
    value, because the browser derives the audio's filename from the script it
    is about to render and then asks GitHub for exactly that file. If the two
    implementations disagree by even a space, every lookup 404s and the site is
    silently mute. The contract, and it is deliberately the simplest thing that
    can agree across two languages:

        strip surrounding whitespace -> encode UTF-8 -> SHA-256 -> first 8 hex

    Why a hash at all: it makes the URL self-invalidating. Re-word a script and
    its hash changes, so the page asks for a file that does not exist and shows
    "not narrated yet" rather than confidently playing yesterday's read over
    today's numbers. No manifest, no sheet column, nothing to keep in step —
    which is the point, because a stored URL is a second copy of the truth and
    it goes stale silently.

    Eight hex is 4 billion values over a corpus of a few thousand scripts. The
    cost of a collision is one wrong narration, not corruption, and the birthday
    bound at that scale is negligible.
    """
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:8]


def asset_name(slug: str, reel: int, lang: str, text: str) -> str:
    """The one filename both sides compute. See script_hash for the contract."""
    return f"{slug}-r{int(reel)}-{lang}-{script_hash(text)}.mp3"


def key_label(key: str) -> str:
    """A stable, non-secret name for one key — for logs and the ledger.

    A hash prefix rather than the key's own last characters: those are enough
    to recognise a leaked key in a log, and this ends up in a file on disk.
    """
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]


# ── per-language voice and model ───────────────────────────────────────────
#
# Three languages, and they do NOT have the same requirements, so a single
# voice id and a single model would be the wrong shape:
#
# * **Telugu is the open question.** `eleven_multilingual_v2` covers a long
#   list that includes Hindi and Tamil; Telugu is the one to verify before
#   building a week of production on it, and the newer/larger models are where
#   support was added. Nothing here can test that for you — hence a per-language
#   model override, so switching costs an env var rather than a code change.
#   Check it with:  ipopulse voice --lang te "ఇది ఒక పరీక్ష"
#
# * **A clone carries its source accent.** One voice cloned from English
#   speech will read Telugu intelligibly and still sound like an English
#   speaker reading Telugu, which a Telugu audience hears immediately. So the
#   voice is overridable per language too.
#
# The policy tension is real and yours to settle: playbook §3.1 makes your OWN
# cloned voice the safe branch and a stock voice presenting as a knowledgeable
# adviser the risky one — but your own clone may be the weaker read in Telugu.
# Own clone everywhere is the conservative answer.

def _per_lang(prefix: str, lang: str, fallback: str = "") -> str:
    """ELEVENLABS_<PREFIX>_<LANG>, falling back to ELEVENLABS_<PREFIX>."""
    lang = (lang or "").strip().lower()
    if lang:
        specific = (os.getenv(f"{prefix}_{lang.upper()}") or "").strip()
        if specific:
            return specific
    return (os.getenv(prefix) or "").strip() or fallback


def voice_id(lang: str = "") -> str:
    return _per_lang("ELEVENLABS_VOICE_ID", lang)


# Which env var tunes which API field. Names are the plain-English ones a
# person would look for, not the API's spelling.
# The sentinel that turns one field over to the script. A word rather than a
# magic number, because "0.6" and "derive it" are different KINDS of answer and
# a number can never mean the second one.
AUTO = "auto"

SETTING_ENV = (
    ("stability", "ELEVENLABS_STABILITY"),
    ("similarity_boost", "ELEVENLABS_SIMILARITY"),
    ("style", "ELEVENLABS_STYLE"),
    ("speed", "ELEVENLABS_SPEED"),
)


def tuned_settings(lang: str = "", text: str = "") -> dict[str, Any]:
    """DEFAULT_SETTINGS with any env overrides applied, per language.

    Named `tuned_settings` and not `settings` on purpose: `settings` is already
    the parameter name on synthesize() and cached_path(), and a module-level
    function of that name would be shadowed inside exactly the functions that
    need to call it — silently, and only at runtime.

    DEFAULT_SETTINGS encodes the playbook's calm-and-credible read (§8), and
    that is a real editorial position, not an accident — so it stays the
    default. But it is one position, and the opposite one is legitimate: a
    reel wants energy, and the two knobs that decide whether a delivery has
    any are exactly the two the default pins flat.

        stability  HIGH is consistent and monotone; LOW lets the model act.
                   0.65 will not emote no matter what the words say.
        style      0.0 is "no exaggeration at all". This is the emotion dial.

    Per-language because the three do not want the same read: Telugu on
    eleven_v3 takes style differently from English, and being able to fix one
    without unsettling the other is the difference between tuning and
    guesswork. ELEVENLABS_STYLE_TE beats ELEVENLABS_STYLE for Telugu.

    A value that will not parse is ignored rather than raised: a typo in .env
    should cost you the tuning, not the whole narration.
    """
    out = dict(DEFAULT_SETTINGS)

    # `auto` on any of these hands that one field to speech.delivery(), which
    # reads it off the script — see AUTO below. Everything else is unchanged:
    # an explicit number still pins the field, and an unset one keeps the
    # documented default.
    auto_fields = set()
    for field, env in SETTING_ENV:
        raw = _per_lang(env, lang)
        if not raw:
            continue
        if raw.strip().lower() == AUTO:
            auto_fields.add(field)
            continue
        try:
            out[field] = float(raw)
        except ValueError:
            pass

    if auto_fields and text:
        from . import speech
        derived = speech.delivery(text, lang)
        for field in auto_fields:
            if field in derived:
                out[field] = derived[field]

    return out


def model(lang: str = "") -> str:
    """Which model this language uses, env override winning over the default.

    The default is per-language rather than one value, because Telugu genuinely
    needs a different model — see MODEL_LANGS. Getting that wrong is silent, so
    it is a default rather than something to remember to configure.
    """
    return _per_lang("ELEVENLABS_MODEL", lang,
                     DEFAULT_MODEL_BY_LANG.get((lang or "").lower(),
                                               DEFAULT_MODEL))


def plan() -> dict[str, dict[str, str]]:
    """What each language would actually use. For `--plan` and /api/voice/status.

    Worth being able to print: with two override tiers it is otherwise easy to
    believe Telugu is on a different model than it is, and discover otherwise
    only from the accent in a finished video.
    """
    order = providers()
    first = next((p for p in order if available(p)), "")
    out = {}
    for code in ("en", "hi", "te"):
        if first == "gemini":
            out[code] = {
                "provider": "gemini",
                "voice": gemini_voice(code),
                "model": gemini_model(code),
                "limit": 0,          # token-bounded, not character-bounded
                # Gemini TTS lists Telugu, Hindi and English among 70+, and
                # detects the language from the text rather than being told.
                "speaks": True,
            }
        else:
            out[code] = {
                "provider": "elevenlabs" if first else "(none configured)",
                "voice": voice_id(code),
                "model": model(code),
                "limit": char_limit(model(code)),
                # The check that matters: a model that cannot speak this
                # language returns nonsense rather than an error, so the plan is
                # the only place it gets caught before a listen.
                "speaks": speaks(model(code), code),
            }
    return out


def monthly_cap() -> int:
    raw = (os.getenv("ELEVENLABS_MONTHLY_CHAR_CAP") or "").strip()
    try:
        return int(raw) if raw else DEFAULT_MONTHLY_CAP
    except ValueError:
        return DEFAULT_MONTHLY_CAP


# ── the spend ledger ───────────────────────────────────────────────────────

def _month() -> str:
    return date.today().strftime("%Y-%m")


def _read_ledger() -> dict:
    try:
        blob = json.loads(LEDGER.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return blob if isinstance(blob, dict) else {}


def spent(key: str = "") -> int:
    """Characters billed this calendar month, per this machine's own count.

    Tracked PER KEY, because the cap it guards is per account. One shared
    counter across rotated keys would refuse the second key at the moment the
    first ran out, which is precisely when the second is meant to take over.

    With no key given, sums every key — the number to look at when asking "what
    has this project spent this month".
    """
    month = _read_ledger().get(_month(), {})
    if not isinstance(month, dict):
        return 0
    if key:
        return int(month.get(key_label(key), 0))
    return sum(int(v) for v in month.values() if isinstance(v, (int, float)))


def _record(chars: int, key: str) -> None:
    blob = _read_ledger()
    month = blob.setdefault(_month(), {})
    if not isinstance(month, dict):
        month = blob[_month()] = {}
    label = key_label(key)
    month[label] = int(month.get(label, 0)) + chars
    # Keep the last few months and drop the rest: it is a tripwire, not a
    # financial record, and an unbounded file that nothing ever reads is just
    # a slow leak.
    for stale in sorted(blob)[:-6]:
        blob.pop(stale, None)
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(blob, indent=1), encoding="utf-8")


def budget(key: str = "") -> dict[str, int]:
    """The cap, and what is left — for one key, or across all of them."""
    cap = monthly_cap()
    keys = api_keys()
    if key:
        used = spent(key)
        return {"cap": cap, "used": used, "left": max(0, cap - used)}
    # The cap is per key, so the pool is cap × keys. Reported this way so the
    # studio's "characters left" line means the same thing whether one key is
    # configured or three.
    total = cap * max(1, len(keys))
    used = spent()
    return {"cap": total, "used": used, "left": max(0, total - used),
            "keys": len(keys), "per_key_cap": cap}


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
                settings: dict[str, Any] | None = None,
                lang: str = "", provider: str = "") -> Path:
    """Where this exact reading lives on disk, if it has been made.

    The extension is not known until the audio is: ElevenLabs returns mp3 and
    Gemini returns PCM this module wraps as wav. So a lookup globs for the stem
    and a write names the file after what actually came back — see `find_cached`.
    """
    provider = provider or (providers() or ["elevenlabs"])[0]
    if provider == "gemini":
        vid = vid or gemini_voice(lang)
        mdl = mdl or gemini_model(lang)
        # The style prompt IS the delivery on this provider, so it belongs in
        # the key: changing the director's notes must not return the old read.
        merged = {"style": gemini_style(lang)}
    else:
        vid = vid or voice_id(lang)
        mdl = mdl or model(lang)
        # tuned_settings, not DEFAULT_SETTINGS: the env overrides have to be in
        # the KEY as well as in the request, or raising ELEVENLABS_STYLE would
        # hash to the same stem and hand back the old flat read from cache —
        # looking like the setting had no effect.
        merged = {**tuned_settings(lang, text), **(settings or {})}
    # The language is not in the key: it only ever selects a voice, a model and
    # a style, and all three ARE in the key. Adding it would cache the same
    # reading twice under two labels.
    stem = _key(text, f"{provider}:{vid}", mdl, merged)
    return VOICE_DIR / f"{stem}.mp3"


def find_cached(path: Path) -> Path | None:
    """The cached file for this stem in whichever format it was stored."""
    for ext in (".mp3", ".wav"):
        candidate = path.with_suffix(ext)
        if candidate.exists():
            return candidate
    return None


# ── the call ───────────────────────────────────────────────────────────────

def synthesize(text: str, vid: str = "", mdl: str = "",
               settings: dict[str, Any] | None = None,
               force: bool = False, lang: str = "",
               provider: str = "") -> tuple[bytes, bool, str, str]:
    """Text -> (audio bytes, from_cache, provider used, format).

    Walks `providers()` in order unless one is named. The four-value return is
    deliberate: the caller has to know WHICH provider spoke, because a listener
    comparing two takes needs to know what they are comparing, and it has to
    know the format, because the two providers do not return the same one.
    """
    wanted = [provider] if provider else providers()
    if not wanted:
        raise VoiceError(
            "No usable voice provider. IPOPULSE_VOICE_PROVIDERS resolved to "
            "nothing — valid names are 'gemini' and 'elevenlabs'.")

    trouble: list[str] = []
    for name in wanted:
        if not available(name):
            trouble.append(f"{name}: not configured")
            continue
        try:
            return _synthesize_one(name, text, vid, mdl, settings, force, lang)
        except VoiceError as err:
            # Fall through to the next provider rather than stopping: the whole
            # point of a chain is that an exhausted or misconfigured provider is
            # survivable. The reasons are collected so a total failure explains
            # every one of them instead of only the last.
            #
            # With one provider there is no chain and nothing to summarise, so
            # its own message is raised untouched — "Every provider refused"
            # above a single line is noise that buries the actual reason.
            if len(wanted) == 1:
                raise
            trouble.append(f"{name}: {err}")
    raise VoiceError("Every provider refused.\n"
                     + "\n".join(f"  - {t}" for t in trouble))


def _synthesize_one(provider: str, text: str, vid: str, mdl: str,
                    settings: dict[str, Any] | None, force: bool,
                    lang: str) -> tuple[bytes, bool, str, str]:
    """Text -> mp3 bytes, and whether it came from the cache.

    Raises VoiceError with the upstream message intact on any refusal. That is
    deliberate: this is an API nobody here can test against every account tier,
    and "ElevenLabs said 401 voice_not_found" is a fixable message where
    "could not generate audio" is a support ticket.
    """
    text = (text or "").strip()
    if not text:
        raise VoiceError("Nothing to say — the script was empty.")

    path = cached_path(text, vid, mdl, settings, lang, provider)
    if not force:
        hit = find_cached(path)
        if hit:
            return hit.read_bytes(), True, provider, hit.suffix.lstrip(".")

    # ── Gemini: one key, one quota, no per-voice licensing to check ────────
    if provider == "gemini":
        # 32k TOKENS per session per the docs, which no single reel script
        # approaches; the practical ceiling is the quality drift past a few
        # minutes, which is a splitting decision rather than a hard refusal.
        audio, fmt = _call_gemini(text, lang)
        _record(len(text), f"gemini:{gemini_key()}")
        VOICE_DIR.mkdir(parents=True, exist_ok=True)
        out = path.with_suffix(f".{fmt}")
        out.write_bytes(audio)
        return audio, False, provider, fmt

    # ── ElevenLabs ────────────────────────────────────────────────────────
    vid = vid or voice_id(lang)
    mdl = mdl or model(lang)
    # Must match cached_path's merge exactly, or every render misses the cache.
    merged = {**tuned_settings(lang, text), **(settings or {})}

    # ── refusals that no key can fix, checked before any of them is tried ──

    limit = char_limit(mdl)
    if len(text) > limit:
        raise VoiceError(
            f"{len(text)} characters is past {mdl}'s {limit:,} limit for one "
            f"request. Split the script, or use a model with a longer limit — "
            f"eleven_multilingual_v2 allows 10,000 and eleven_flash_v2_5 "
            f"40,000, but neither can speak Telugu.")

    if not speaks(mdl, lang):
        raise VoiceError(
            f"{mdl} does not support '{lang}'. Telugu needs eleven_v3 — set "
            f"ELEVENLABS_MODEL_{(lang or '').upper()}=eleven_v3. Sending it "
            f"anyway does not fail, it returns confident nonsense, which is "
            f"why this refuses instead.")

    keys = api_keys()
    if not keys:
        raise VoiceError("ELEVENLABS_API_KEY is not set in .env.")
    if not vid:
        suffix = f" (or ELEVENLABS_VOICE_ID_{lang.upper()})" if lang else ""
        raise VoiceError(
            f"ELEVENLABS_VOICE_ID{suffix} is not set. Run `ipopulse voice "
            f"--voices` to list the voices on this account and copy the id "
            f"of yours.")

    body = json.dumps({
        "text": text,
        "model_id": mdl,
        "voice_settings": merged,
    }).encode("utf-8")

    # ── try each key in turn ──────────────────────────────────────────────
    #
    # A key is skipped when its own local budget cannot cover the request, and
    # abandoned when the API says it cannot serve — see _rotatable(). Anything
    # else is raised immediately: a malformed request or a missing voice fails
    # identically on all three keys, and walking them would turn one clear
    # error into a slow one.
    problems: list[str] = []
    for key in keys:
        label = key_label(key)
        left = budget(key)["left"]
        if len(text) > left:
            problems.append(f"{label}: only {left} characters left locally")
            continue

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
            detail = _explain(err)
            if _rotatable(err, detail) and key is not keys[-1]:
                problems.append(f"{label}: {detail}")
                continue
            raise VoiceError(
                _with_history(detail, problems, len(keys))) from err
        except OSError as err:
            # A network fault is the machine's, not the key's — another key
            # over the same dead connection fails the same way.
            raise VoiceError(f"Could not reach api.elevenlabs.io — {err}") from err

        if not audio:
            problems.append(f"{label}: returned no audio and no error")
            continue

        # Bill first, write second. A crash between the two must not leave the
        # ledger short — undercounting spend is the failure that costs money.
        _record(len(text), key)
        VOICE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_bytes(audio)
        return audio, False, provider, "mp3"

    raise VoiceError(_with_history(
        "no key could serve this request", problems, len(keys)))


# Statuses where a DIFFERENT key might succeed. Everything absent from this set
# is a property of the request rather than of the credential — 422 is a bad
# body, 404 is a voice id that does not exist — and retrying those on two more
# keys only delays the same message.
ROTATABLE_STATUS = {401, 403, 429}
# ElevenLabs puts the real reason in the body, and the status alone is
# ambiguous exactly where it matters: quota exhaustion arrives as a 401, which
# is otherwise "bad key".
ROTATABLE_MARKERS = ("quota_exceeded", "quota", "rate_limit",
                     "too_many_concurrent_requests", "concurrent",
                     "detected_unusual_activity")


def _rotatable(err: urllib.error.HTTPError, detail: str) -> bool:
    low = detail.lower()
    return (err.code in ROTATABLE_STATUS
            or any(m in low for m in ROTATABLE_MARKERS))


def _with_history(final: str, problems: list[str], total: int) -> str:
    """One message that names every key that was tried and why each declined.

    Without this a three-key rotation reports only the last failure, and "401
    bad credentials" gives no hint that two other keys were out of quota — the
    most likely real cause and an entirely different fix.
    """
    if not problems:
        return final
    tried = "\n".join(f"    {p}" for p in problems)
    return (f"{final}\n  Tried {len(problems) + 1} of {total} key(s):\n"
            f"{tried}\n    (last) {final}")


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


def _wav(pcm: bytes, rate: int, channels: int = 1, width: int = 2) -> bytes:
    """Wrap raw PCM in a RIFF/WAVE header.

    Written by hand rather than via the `wave` module purely to keep it in
    memory without a temp file; the layout is the canonical 44-byte header.
    """
    import struct

    block = channels * width
    return (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, channels, rate,
                          rate * block, block, width * 8)
            + b"data" + struct.pack("<I", len(pcm)) + pcm)


def _rate_from_mime(mime: str) -> int:
    """`audio/L16;codec=pcm;rate=24000` -> 24000, falling back to the default."""
    found = re.search(r"rate=(\d+)", mime or "")
    return int(found.group(1)) if found else GEMINI_FALLBACK_RATE


def _call_gemini(text: str, lang: str) -> tuple[bytes, str]:
    """Gemini TTS -> (wav bytes, "wav"). Raises VoiceError on refusal.

    Two things here are not incidental:

    * **The preamble.** The docs warn that a vague prompt can fail the speech
      classifier or make the model read the director's notes aloud, so the
      style block is explicitly separated from the transcript by a labelled
      marker. Without that separation this reads "Style: calm, measured" to the
      listener, which is a very confusing IPO video.

    * **The retry.** Also documented: the model "occasionally returns text
      tokens instead of audio tokens, causing the server to fail the request
      with a 500", randomly and rarely. That is a documented instruction to
      implement retries, not a fault to report to the user.
    """
    key = gemini_key()
    if not key:
        raise VoiceError("GEMINI_API_KEY is not set in .env.")

    mdl = gemini_model(lang)
    prompt = f"{gemini_style(lang)}\n\nTRANSCRIPT TO SPEAK:\n{text}"
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {
                    "prebuiltVoiceConfig": {"voiceName": gemini_voice(lang)},
                },
            },
        },
    }).encode("utf-8")

    last = ""
    for attempt in range(1, GEMINI_RETRIES + 1):
        request = urllib.request.Request(
            f"{GEMINI_ROOT}/models/{mdl}:generateContent",
            data=body,
            headers={"Content-Type": "application/json",
                     # In a header, never the query string: a URL with a key in
                     # it lands in logs, proxies and crash reports.
                     "x-goog-api-key": key},
            method="POST")
        try:
            with urllib.request.urlopen(request, timeout=240) as response:
                blob = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            detail = _explain_gemini(err)
            # 500s are the documented flake; 429 is the free tier's rate limit
            # and worth one more try after a pause.
            if err.code in (429, 500, 503) and attempt < GEMINI_RETRIES:
                last = detail
                time.sleep(attempt * 2)
                continue
            raise VoiceError(detail) from err
        except OSError as err:
            raise VoiceError(
                f"Could not reach generativelanguage.googleapis.com — {err}") from err

        part = _first_audio_part(blob)
        if part:
            pcm = base64.b64decode(part.get("data") or "")
            if pcm:
                mime = part.get("mimeType") or ""
                # Already a container? Hand it back untouched.
                if "mpeg" in mime or "mp3" in mime:
                    return pcm, "mp3"
                if "wav" in mime:
                    return pcm, "wav"
                return _wav(pcm, _rate_from_mime(mime)), "wav"

        # No audio came back. This is the "text tokens instead of audio tokens"
        # case, plus PROHIBITED_CONTENT rejections, and both are worth naming
        # rather than reporting as an empty response.
        last = _no_audio_reason(blob)
        if attempt < GEMINI_RETRIES:
            time.sleep(attempt * 2)

    raise VoiceError(
        f"Gemini returned no audio after {GEMINI_RETRIES} attempts. {last}")


def _first_audio_part(blob: dict) -> dict | None:
    for cand in blob.get("candidates") or []:
        for part in (cand.get("content") or {}).get("parts") or []:
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                return inline
    return None


def _no_audio_reason(blob: dict) -> str:
    cands = blob.get("candidates") or []
    reason = (cands[0].get("finishReason") if cands else "") or ""
    if reason.upper() in ("PROHIBITED_CONTENT", "SAFETY", "BLOCKLIST"):
        return (f"The request was rejected as {reason}. Per Google's own docs a "
                f"vague prompt can trip the speech classifier — usually a "
                f"script problem rather than a real safety issue.")
    # A text part where audio should be is the documented flake, and it reads
    # as a total mystery unless it is named.
    for cand in cands:
        for part in (cand.get("content") or {}).get("parts") or []:
            if part.get("text"):
                return ("The model replied with text instead of audio, which "
                        "its docs list as a known random failure.")
    return f"finishReason={reason or 'none given'}."


def _explain_gemini(err: urllib.error.HTTPError) -> str:
    detail = ""
    try:
        blob = json.loads(err.read().decode("utf-8", "replace"))
        detail = str((blob.get("error") or {}).get("message") or "")
    except (ValueError, OSError):
        pass
    hint = {
        400: " — often an unsupported voice name or model id.",
        403: " — the key is rejected or the API is not enabled on that project.",
        404: f" — no such model. Check GEMINI_TTS_MODEL; the default is "
             f"{GEMINI_MODEL}.",
        429: " — free-tier rate limit. Preview models have tighter limits than "
             "the stable ones; wait, or try again with fewer languages at once.",
    }.get(err.code, "")
    return f"Gemini said {err.code}: {detail or 'request refused'}{hint}"


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
