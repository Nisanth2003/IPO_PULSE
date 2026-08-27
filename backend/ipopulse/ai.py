"""Gemini: translation, editorial drafting, and grounded research.

Two very different uses of the same model, held to different standards:

  Translation and drafting  — words. Safe. Cached hard, reused forever.
  Research (GMP, dates)     — numbers. NOT safe by default. Every value is
                              proposed with a source URL and a confidence,
                              sanity-checked against the price band, and
                              written as `needs_review` unless it passes.

The rule the rest of the codebase relies on: nothing Gemini returns is ever
*computed with*. Derived metrics come from compute.py operating on stored data.
A fetched GMP is stored like any other data point, with `source: gemini` so you
can always tell where a figure came from.

The key never reaches the browser. This is a local build step; only its output
is committed and published.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .store import CACHE_DIR

# Google retires model ids on a schedule, and a retired one fails with a 404
# that reads like a typo rather than an expiry ("no longer available to new
# users"). gemini-2.5-flash went that way mid-project, which is why nothing
# here pins an id: the list is discovered from the API and re-ranked on every
# cache miss. FALLBACK_MODEL is only the guess used before the first list call
# succeeds, and it is allowed to be wrong.
FALLBACK_MODEL = "gemini-flash-lite-latest"

# Families to never auto-select: they either cannot do chat completion or bill
# differently enough that picking one by accident would be unwelcome.
_EXCLUDE = ("tts", "image", "robotics", "computer-use", "deep-research",
            "lyria", "nano-banana", "embedding", "aqa", "omni", "customtools",
            "antigravity", "veo", "imagen")

# Free-tier ranking, and the ordering here is driven by requests-per-day, not
# by model quality. The free tier's binding limit for this workload is RPD:
#
#     gemini-*-flash        5 RPM   250K TPM    20 RPD
#     gemini-*-flash-lite  15 RPM   250K TPM   500 RPD
#     gemma-4-*            30 RPM    16K TPM  14400 RPD
#
# Observed peak on this key was 847 TPM against 250K — 0.3% of the token
# budget — while sitting at 10 of 20 daily requests. This project makes many
# small calls (one per IPO per language), so it runs out of *requests* roughly
# 150x sooner than it runs out of tokens. A single full translate pass is 20
# calls, which is the entire daily flash allowance and a twenty-fifth of
# flash-lite's. Hence lite first.
#
# gemma has enormous RPD but only 16K TPM and no tool support, so it sits
# below as a bulk last resort. pro is generally not free at all.
#
# Order matters: the test is `in name`, so "flash-lite" must be checked before
# "flash" or every lite model would match "flash" first.
_TIER = (("flash-lite", 3), ("flash", 2), ("gemma", 1), ("pro", 0))

MODEL_CACHE_HOURS = 12


def _rank(name: str) -> tuple:
    """Higher sorts first: stable over preview, then tier, then version."""
    tier = next((score for key, score in _TIER if key in name), -1)
    stable = 0 if "preview" in name or "exp" in name else 1
    ver = re.search(r"gemini-(\d+(?:\.\d+)?)", name)
    version = float(ver.group(1)) if ver else 0.0
    # "-latest" aliases survive retirement by definition, so nudge them up.
    alias = 1 if name.endswith("-latest") else 0
    return (stable, tier, version, alias)


def list_models(client) -> list[str]:
    """Every model this key can actually call, best free candidate first."""
    names = []
    for m in client.models.list():
        name = (m.name or "").replace("models/", "")
        if not name or "generateContent" not in (m.supported_actions or []):
            continue
        if any(bad in name for bad in _EXCLUDE):
            continue
        names.append(name)
    return sorted(set(names), key=_rank, reverse=True)


def default_model() -> str:
    """The id to try first.

    Env wins, so a deliberate choice is never second-guessed. Otherwise use
    whatever discovery last found to work — read at call time, because
    `load_dotenv` runs after this module is imported and a module-level
    `os.getenv` would never see .env.
    """
    pinned = os.getenv("GEMINI_MODEL")
    if pinned:
        return pinned
    cached = _read_model_cache()
    return cached or FALLBACK_MODEL


def _model_cache_path() -> Path:
    return CACHE_DIR / "working-model.json"


def _read_model_cache() -> str | None:
    path = _model_cache_path()
    if not path.exists():
        return None
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if _now() - blob.get("at", 0) > MODEL_CACHE_HOURS * 3600:
        return None                       # re-discover; a model may have died
    return blob.get("model") or None


def _write_model_cache(model: str) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _model_cache_path().write_text(
            json.dumps({"model": model, "at": _now()}), encoding="utf-8"
        )
    except OSError:
        pass                              # a cache that cannot be written is fine


DEFAULT_MODEL = FALLBACK_MODEL          # kept for `Gemini(model=...)` defaults
DEFAULT_CACHE_DAYS = 30          # translations go stale slowly; numbers don't cache at all

# How many "what the business does" bullets reel 1's company scene wants.
#
# One number, read by three places that used to disagree: the prompt in
# draft_analysis, the cap applied to what comes back, and doctor's threshold.
# Two bullets left the scene half empty on a 9:16 frame — the card is tall and
# a two-line answer to "what does this company do" floated in the middle of it.
# Raising it is not free: each bullet is a claim that has to come from the
# facts, so the prompt names four distinct angles rather than asking for
# "more", which would only get the same point reworded.
OVERVIEW_BULLETS = 4

LANG_NAMES = {
    "hi": "Hindi (Devanagari script)",
    "te": "Telugu (Telugu script)",
    "en": "English",
}

# Terms that must survive translation untouched — a retail investor searches
# for "GMP" and "OFS", not their translated equivalents.
KEEP_VERBATIM = [
    "IPO", "GMP", "QIB", "NII", "HNI", "OFS", "DRHP", "RHP", "PAN",
    "BSE", "NSE", "SEBI", "EBITDA", "PAT", "P/E", "CAGR", "RoNW", "EPS",
]


class AiUnavailable(RuntimeError):
    pass


def _hash(payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:20]


def _now() -> float:
    return time.time()


class Gemini:
    """Cached wrapper around google-genai."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        cache_dir: Path | None = None,
        cache_days: int | None = None,
    ):
        self.api_key = api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        self.model = model
        self.cache_dir = cache_dir or (CACHE_DIR / "ai")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        env_days = os.getenv("IPOPULSE_CACHE_DAYS")
        self.cache_days = int(cache_days if cache_days is not None
                              else (env_days or DEFAULT_CACHE_DAYS))
        self._client = None

    # ── availability ──────────────────────────────────────────────────────
    def available(self) -> bool:
        if not self.api_key:
            return False
        try:
            import google.genai  # noqa: F401
        except ImportError:
            return False
        return True

    def _client_or_raise(self):
        if self._client is not None:
            return self._client
        if not self.api_key:
            raise AiUnavailable(
                "No GEMINI_API_KEY in the environment. Copy .env.example to "
                ".env and add your key, or run with --no-ai."
            )
        try:
            from google import genai
        except ImportError as exc:
            raise AiUnavailable(
                "google-genai is not installed. pip install -r requirements.txt"
            ) from exc
        # A request timeout, because the default is effectively forever.
        #
        # Observed 2026-08-17: `translate` sat on a single IPO for over ten
        # minutes at zero CPU, blocked inside generate_content with nothing
        # printed. That is worse than an error. `_call` already walks to the next
        # model on a failure and every caller turns AiUnavailable into one line,
        # so a request that gives up is handled; a request that hangs is not —
        # it stalls the whole `daily` chain until Task Scheduler's 20-minute
        # ExecutionTimeLimit kills the lot, so one wedged call costs `sync`,
        # `doctor` and `build` too.
        #
        # Milliseconds, and generous: grounded search legitimately takes 30-60s
        # and translation runs two calls per IPO. This is a hang-breaker, not a
        # latency budget — set it too tight and slow-but-working runs start
        # failing. Wrapped because `http_options` is not accepted by every
        # google-genai version, and no timeout beats no client.
        try:
            from google.genai import types as _types
            self._client = genai.Client(
                api_key=self.api_key,
                http_options=_types.HttpOptions(timeout=180_000),
            )
        except (ImportError, AttributeError, TypeError, ValueError):
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    # ── cache, with expiry ────────────────────────────────────────────────
    # Entries carry their own timestamp so the TTL can be changed without
    # invalidating everything, and so `cache --prune` is a pure file operation.

    def _cache_path(self, kind: str, key: str) -> Path:
        return self.cache_dir / f"{kind}-{key}.json"

    def _cached(self, kind: str, key: str) -> Any | None:
        path = self._cache_path(kind, key)
        if not path.exists():
            return None
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            path.unlink(missing_ok=True)
            return None
        if not isinstance(blob, dict) or "value" not in blob:
            path.unlink(missing_ok=True)         # pre-TTL format
            return None
        if self.cache_days > 0:
            age_days = (_now() - blob.get("at", 0)) / 86400
            if age_days > self.cache_days:
                path.unlink(missing_ok=True)
                return None
        return blob["value"]

    def _store(self, kind: str, key: str, value: Any) -> None:
        self._cache_path(kind, key).write_text(
            json.dumps({"at": _now(), "model": self.model, "value": value},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def prune_cache(self, days: int | None = None) -> tuple[int, int]:
        """Delete entries older than `days`. Returns (removed, kept)."""
        limit = self.cache_days if days is None else days
        removed = kept = 0
        for path in self.cache_dir.glob("*.json"):
            try:
                blob = json.loads(path.read_text(encoding="utf-8"))
                age = (_now() - blob.get("at", 0)) / 86400
            except Exception:
                path.unlink(missing_ok=True); removed += 1; continue
            if limit > 0 and age > limit:
                path.unlink(missing_ok=True); removed += 1
            else:
                kept += 1
        return removed, kept

    def clear_cache(self) -> int:
        n = 0
        for path in self.cache_dir.glob("*.json"):
            path.unlink(); n += 1
        return n

    def cache_stats(self) -> dict:
        ages, total = [], 0
        for path in self.cache_dir.glob("*.json"):
            total += path.stat().st_size
            try:
                blob = json.loads(path.read_text(encoding="utf-8"))
                ages.append((_now() - blob.get("at", 0)) / 86400)
            except Exception:
                ages.append(0)
        return {
            "entries": len(ages),
            "kb": round(total / 1024, 1),
            "oldest_days": round(max(ages), 1) if ages else 0,
            "ttl_days": self.cache_days,
        }

    # ── raw calls ─────────────────────────────────────────────────────────
    @staticmethod
    def _fault(exc: Exception) -> str | None:
        """Classify an API error into the kinds worth reacting to."""
        text = str(exc)
        if "RESOURCE_EXHAUSTED" in text or "429" in text:
            return "quota"
        if "NOT_FOUND" in text and "model" in text.lower():
            return "retired"
        if "API_KEY_INVALID" in text or "PERMISSION_DENIED" in text:
            return "key"
        # A timeout is transport, not a verdict on the model or the key, and
        # `_client_or_raise` sets one precisely so a wedged request dies. Left
        # unclassified it returned None, which makes `_call` re-raise — so the
        # timeout that replaced a silent hang produced a raw httpx traceback
        # instead, killing the command and reading like a bug in ipopulse. This
        # is exactly what the docstring below says must not escape.
        #
        # Classified as its own kind rather than folded into "quota" so the
        # message at the end can say which happened: a run that timed out is
        # worth retrying now, a run out of quota is worth retrying tomorrow.
        #
        # DEADLINE_EXCEEDED / 504 belongs here too, and matching it is not
        # optional: it is the SERVER giving up rather than our client, so none of
        # the words above appear in it and it fell through to `return None` —
        # which makes `_call` re-raise, so a plain slow response killed the whole
        # command with a google.genai traceback. Observed on leap-india twice.
        name = type(exc).__name__
        low = text.lower()
        if ("Timeout" in name or "timed out" in low or "timeout" in low
                or "DEADLINE_EXCEEDED" in text or "504" in text):
            return "timeout"
        # 503 / UNAVAILABLE is different in the one way that matters: it means
        # *this* model is overloaded, so the next candidate is genuinely worth a
        # try. Given its own kind so it walks the list like a quota error rather
        # than stopping like a timeout.
        if "UNAVAILABLE" in text or "503" in text or "overloaded" in low:
            return "busy"
        return None

    def _candidates(self) -> list[str]:
        """Models to try, in order. A pinned GEMINI_MODEL is tried alone."""
        if os.getenv("GEMINI_MODEL"):
            return [self.model]
        try:
            found = list_models(self._client_or_raise())
        except Exception:
            return [self.model]
        return [self.model] + [m for m in found if m != self.model]

    def _call(self, **kwargs) -> Any:
        """Generate, walking down the free-model list when one is unusable.

        Two things fail routinely and neither is a bug: a model id gets retired,
        and a free-tier quota runs out partway through a run. Both are per-model,
        so the fix is to try the next candidate rather than abort the batch. The
        winner is cached so the next command starts on a model that works.

        Whatever survives is turned into AiUnavailable, which every caller
        already catches and prints as one line — an escaping stack trace reads
        like a bug in ipopulse rather than a fact about the key.
        """
        client = self._client_or_raise()
        tried: list[str] = []
        last: Exception | None = None

        for model in self._candidates():
            tried.append(model)
            try:
                resp = client.models.generate_content(model=model, **kwargs)
            except Exception as exc:
                fault = self._fault(exc)
                if fault == "key":
                    raise AiUnavailable("GEMINI_API_KEY was rejected.") from exc
                if fault == "timeout":
                    # Stop, do not walk the list. Quota and retirement are facts
                    # about one model, so the next candidate is worth a try; a
                    # timeout is the network or the service, and every candidate
                    # would wait the full timeout to learn the same thing. With
                    # a dozen models on the free tier that turns one stalled
                    # call into an hour of stalled calls — slower than the hang
                    # this timeout exists to prevent.
                    last = exc
                    break
                if fault is None:
                    raise
                last = exc
                continue                  # quota or retired: try the next one
            if model != self.model:
                self.model = model        # stick with it for the rest of the run
            _write_model_cache(model)
            return resp

        kind = self._fault(last) or "error"

        # A timeout says nothing about the model, so "no usable Gemini model" —
        # correct for quota and retirement — would send you looking in the wrong
        # place. Name what actually happened and that it is worth retrying.
        if kind == "timeout":
            raise AiUnavailable(
                "Gemini did not respond within the request timeout. This is the "
                "network or the service, not your key or the model — the same "
                "call usually works on a retry.\n  "
                f"Model: {self.model}. Nothing was written for this step."
            ) from last

        grounded = "tools" in str(kwargs.get("config", ""))
        detail = ("Grounded search has a much smaller free allowance than plain "
                  "generation and usually needs billing enabled.\n  "
                  if grounded else "")
        raise AiUnavailable(
            f"No usable Gemini model ({kind}). "
            f"Tried: {', '.join(tried[:6])}"
            f"{' …' if len(tried) > 6 else ''}\n  {detail}"
            "https://ai.dev/rate-limit"
        ) from last

    def _generate_json(self, prompt: str) -> Any:
        return _parse_json(self._generate_json_text(prompt))

    def _generate_json_text(self, prompt: str, temperature: float = 0.2) -> str:
        """Raw JSON-mode response text.

        Drafting keeps the existing 0.2; extraction passes 0.0, where the only
        acceptable creativity is none.
        """
        resp = self._call(
            contents=prompt,
            config={"response_mime_type": "application/json",
                    "temperature": temperature},
        )
        return resp.text or ""

    def _generate_grounded(self, prompt: str, urls: list[str] | None = None) -> tuple[str, list[str]]:
        """Run with Google Search grounding (and URL context when given).

        Returns (text, source_urls). Structured-output mode cannot be combined
        with tools, so the JSON is parsed leniently from the text instead.
        """
        self._client_or_raise()
        try:
            from google.genai import types
        except ImportError as exc:
            raise AiUnavailable("google-genai too old for grounded search") from exc

        # The two tools are metered completely differently on the free tier:
        # url_context is free, google_search is not and 429s immediately
        # without billing. So when a URL is pinned, read *only* that URL and
        # do not attach search — otherwise one unnecessary tool turns a call
        # that would have worked into a quota error. Search stays the fallback
        # for the un-pinned case, where there is nothing else to go on.
        #
        # Verified 2026-08-10: url_context alone succeeds on this key, and
        # Google's fetcher renders the JavaScript GMP tables that a plain HTTP
        # scraper only sees as "No data available".
        tools = []
        try:
            if urls:
                tools.append(types.Tool(url_context=types.UrlContext()))
            else:
                tools.append(types.Tool(google_search=types.GoogleSearch()))
        except AttributeError as exc:
            raise AiUnavailable(
                "This google-genai version has no Search/URL tools. "
                "pip install -U google-genai"
            ) from exc

        resp = self._call(
            contents=prompt,
            config=types.GenerateContentConfig(tools=tools, temperature=0.0),
        )
        return (resp.text or ""), _grounding_urls(resp)

    # ── translation ───────────────────────────────────────────────────────
    def translate_fields(
        self,
        fields: dict[str, list[str] | str],
        lang: str,
        *,
        company: str = "",
        force: bool = False,
    ) -> dict[str, Any]:
        if lang == "en":
            return dict(fields)

        key = _hash({"m": self.model, "l": lang, "f": fields})
        if not force:
            hit = self._cached("tr", key)
            if hit is not None:
                return hit

        if not self.available():
            return dict(fields)      # graceful: keep the English

        prompt = f"""You are translating scripts for an Indian retail-investing
YouTube Shorts channel called IPO Pulse. Translate the values below into
{LANG_NAMES.get(lang, lang)}.

Rules:
- Keep it short, spoken, and punchy. These appear as on-screen captions AND
  are read aloud by a text-to-speech voice, so every word must be in the
  target script or the voice will mispronounce it.
- Keep these terms in Latin script exactly as written: {', '.join(KEEP_VERBATIM)}.
  Keep company, brand and product names in Latin script too.
- EVERY OTHER WORD must be written in the target script — no exceptions.
  Ordinary finance vocabulary is the trap here: words like revenue, margin,
  crore, percent, times, ratio, working capital, net worth, fresh issue,
  growth, debt and subscription must NOT be left in Latin script. Write them
  in the target script, transliterating where that is the natural way to say
  them (Telugu: రెవెన్యూ, మార్జిన్, కోట్లు, శాతం, రెట్లు — Hindi: रेवेन्यू,
  मार्जिन, करोड़, प्रतिशत, गुना). Be consistent: the same English word must
  get the same rendering every time it appears.
- Keep digits, currency symbols and the % sign exactly as they appear. This
  does NOT extend to the English words "percent" or "times" — translate those.
- Do not add, remove, explain or embellish anything.
- Preserve the JSON shape exactly: a string stays a string, a list of N items
  returns a list of N items in the same order.

Company: {company}

Return ONLY a JSON object with the same keys.

{json.dumps(fields, ensure_ascii=False, indent=2)}"""

        # Was `if not isinstance(out, dict): raise`, which failed cleanly but
        # still failed on `[{...}]` — the same array-wrapping that crashed
        # draft_analysis. Salvaging it here means a translation survives the
        # model being sloppy about its container.
        out = _as_object(self._generate_json(prompt), "translation")

        safe: dict[str, Any] = {}
        for name, original in fields.items():
            got = out.get(name)
            if isinstance(original, list):
                safe[name] = got if isinstance(got, list) and len(got) == len(original) else original
            else:
                safe[name] = got if isinstance(got, str) and got.strip() else original

        self._store("tr", key, safe)
        return safe

    # ── editorial drafting ────────────────────────────────────────────────
    def draft_analysis(self, context: dict, *, force: bool = False) -> dict[str, Any]:
        prompt = f"""You are a sell-side equity research analyst with twenty
years covering Indian primary markets. You have read hundreds of RHPs and
watched most of these issues list, so you know which numbers actually
predict a listing and which are dressed up for the roadshow. Write with
that judgement: name the one thing that decides this issue rather than
listing every metric, and say plainly when the pricing is asking investors
to pay for growth that has not happened yet.

Using ONLY the facts given below, draft the editorial copy. Experience is
how you read the facts, never a licence to add to them: do not invent any
figure, date or claim that is not present. If the facts are insufficient
for a field, return an empty list or empty string for that field rather
than guessing — a veteran says "the RHP does not show this", not a number
they assumed.

Write for a retail viewer, in plain spoken English, not research-desk
jargon. Each bullet must be one short line, under about 12 words, suitable
as an on-screen caption.

Return JSON with exactly these keys:
  "overview":    exactly {OVERVIEW_BULLETS} bullets introducing this company.
                 This is the whole of what reel 1 says about it, and most
                 viewers have never heard the name — so it has to leave them
                 actually knowing what the business is, not just that an IPO
                 exists. Where `business` is present below, it is the company's
                 own filing describing itself: that is your main source, and
                 these bullets should read as if written by someone who had
                 read it. Each bullet takes a DIFFERENT angle, in this order:
                   1. what it sells or operates, concretely — the products,
                      brands or services a viewer would recognise
                   2. its scale and footprint: stores, plants, fleet, capacity,
                      cities, states, customers, how long it has been going
                   3. how it earns — the revenue model, the mix, which segment
                      carries the business
                   4. what this IPO is for: the offer's size, fresh capital vs
                      existing holders selling, and what the money funds
                 Prefer a specific figure from `business` over a general
                 statement every time: "56 stores across 46 cities" beats
                 "a wide retail presence", and "gold was 93.96% of FY24
                 revenue" beats "gold is the core product".
                 Hard rules, because breaking them is worse than a short scene:
                   * Never write that a fact is missing. "The RHP does not show
                     store counts" is a true sentence and a useless caption; a
                     viewer reads it as the company having nothing to show. If
                     an angle has no fact behind it, write a different real one.
                   * Never restate a figure or claim another bullet already
                     used. Four ways of saying "sells jewellery" is padding.
                   * `business.about` and `business.summary` overlap — they are
                     two write-ups of the same company. Treat them as one
                     source and do not let the same detail fill two bullets.
                   * If `business` is absent, fall back to the sector and the
                     offer, and invent nothing: no customer names, plant counts,
                     geographies or brands that are not written below.
  "green_flags": up to 3 genuine positives, each citing a fact given
  "red_flags":   up to 3 genuine risks, each citing a fact given
  "growth":      one line summarising the growth trajectory
  "valuation":   one line on how it is priced versus peers
  "risk":        the single biggest risk, one line

FACTS:
{json.dumps(context, ensure_ascii=False, indent=2, default=str)}"""

        # Key on the prompt, not just the facts. The old key hashed model +
        # context only, so editing the instructions above changed nothing for
        # any IPO already in the cache — the previous wording kept being
        # served and a prompt fix looked like it had silently failed.
        key = _hash({"m": self.model, "p": prompt})
        if not force:
            hit = self._cached("an", key)
            if hit is not None:
                return hit
        if not self.available():
            raise AiUnavailable("Gemini not configured; cannot draft analysis.")

        out = _as_object(self._generate_json(prompt), "the analysis draft")
        # The cap is per key, not shared. It was one `[:3]` for every list with
        # `overview` then trimmed to `[:2]`, so raising the overview count in
        # the prompt above silently did nothing — the model returned four and
        # two were thrown away. OVERVIEW_BULLETS is the single number the
        # prompt, this cap and doctor's threshold all read.
        listy = lambda k, n=3: [
            str(x).strip() for x in (out.get(k) or []) if str(x).strip()][:n]
        drafted = {
            "overview": listy("overview", OVERVIEW_BULLETS),
            "green_flags": listy("green_flags"),
            "red_flags": listy("red_flags"),
            "growth": str(out.get("growth") or "").strip(),
            "valuation": str(out.get("valuation") or "").strip(),
            "risk": str(out.get("risk") or "").strip(),
        }
        self._store("an", key, drafted)
        return drafted

    def read_rhp(self, company: str, sections: dict[str, str],
                 years: list[str] | None = None) -> dict[str, Any]:
        """Pull the balance-sheet fields out of RHP excerpts.

        No search tool and no URL tool: the text is supplied, so the model has
        nothing to browse and nothing to remember. That is the whole point —
        every figure it returns has to be sittting in the prompt, which makes
        "I could not find it" a cheap answer and invention an expensive one.
        """
        if not self.available():
            raise AiUnavailable("Gemini not configured; cannot read the RHP.")

        span = years or ["FY24", "FY25", "FY26"]
        blocks = "\n\n".join(
            f"===== {name.upper()} =====\n{text}" for name, text in sections.items())

        prompt = f"""Below are verbatim pages from the Red Herring Prospectus
of the Indian IPO "{company}". Extract the figures listed, using ONLY what
appears in these pages.

Years wanted, oldest first: {", ".join(span)}.

The tables lost their column alignment when the PDF was converted to text, so
read them carefully:

  * Several pages put OUR COMPANY beside its listed PEERS. Every figure you
    return must be our company's own column. If you cannot tell which column
    is ours, return null rather than guessing — a peer's number filed as ours
    is far worse than a gap.
  * Report every amount EXACTLY as printed — do not convert units. State the
    unit the tables use in "unit" (one of: million, crore, lakh, billion,
    rupees). The conversion is done downstream; a silent one here is a 10x
    error nobody can see.
  * "pe_peer_avg" is the mean P/E of the listed peers shown, NOT our company's.
    Only give it if peer P/E values are actually printed.
  * Every array must be exactly {len(span)} long, oldest first. If a year is
    missing, return that whole array empty rather than padding or shifting.
  * A metric you cannot find is an empty array. That does not stop you
    reporting the ones you can find.

Return ONLY JSON:
{{"years": {json.dumps(span)},
  "revenue": [], "ebitda": [], "pat": [], "net_worth": [], "total_debt": [],
  "unit": "million" | "crore" | "lakh" | "billion" | "rupees",
  "eps": <number or null>, "pe_peer_avg": <number or null>,
  "peers": ["<listed peer names you saw>"],
  "confidence": "high" | "medium" | "low",
  "note": "<anything you could not separate from a peer's column>"}}

"eps" is in rupees per share and "pe_peer_avg" is a multiple — neither is
affected by "unit".

{blocks}"""

        return _parse_json(self._generate_json_text(prompt, temperature=0.0),
                           default={})

    # ── grounded research ─────────────────────────────────────────────────
    # Deliberately NOT cached: a cached GMP is a wrong GMP tomorrow.

    def research_gmp(
        self, company: str, *, price_high: float = 0.0, urls: list[str] | None = None
    ) -> dict[str, Any]:
        """Look up today's grey-market premium. Proposes; never decides.

        Returns {gmp, date, confidence, sources, note, needs_review, reason}.
        `needs_review` is True whenever the answer is unsupported by a citation
        or implausible against the price band — see `vet_gmp`.
        """
        if not self.available():
            raise AiUnavailable("Gemini not configured; cannot research.")

        hint = f"\nThe IPO's upper price band is ₹{price_high:g}." if price_high else ""
        target = ("\nPrefer these sources:\n" + "\n".join(urls)) if urls else ""
        prompt = f"""Find TODAY's grey market premium (GMP) for the Indian IPO
"{company}".{hint}{target}

This is for a finance channel, so accuracy matters more than answering.

Rules:
- Report the GMP in rupees per share. This is NOT the Kostak rate and NOT the
  subject-to-sauda rate — if a page shows several numbers, pick the one
  labelled GMP or "grey market premium".
- Only report a number you actually found on a page you retrieved. Do not
  estimate, interpolate, or reason your way to a figure.
- If you cannot find a clearly dated GMP for this exact company, set
  "gmp" to null and explain in "note". A null is a correct answer.
- "as_of" must be the date the figure applies to, as printed on the source.

Return ONLY JSON:
{{"gmp": <number or null>,
  "as_of": "YYYY-MM-DD or null",
  "kostak": <number or null>,
  "confidence": "high" | "medium" | "low",
  "note": "<where it came from, or why you could not find it>"}}"""

        text, sources = self._generate_grounded(prompt, urls)
        data = _parse_json(text, default={})
        gmp = data.get("gmp")
        out = {
            "gmp": float(gmp) if isinstance(gmp, (int, float)) else None,
            "date": data.get("as_of") or None,
            "kostak": data.get("kostak") if isinstance(data.get("kostak"), (int, float)) else 0,
            "confidence": str(data.get("confidence") or "low").lower(),
            "note": str(data.get("note") or "").strip(),
            "sources": sources,
            "source": "gemini",
        }
        out.update(vet_gmp(out, price_high))
        return out

    def research_gmp_history(
        self, company: str, *, price_high: float = 0.0,
        since: str | None = None, urls: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Read the whole dated GMP table, not just today's row.

        `research_gmp` asks for one figure, which makes a missed day permanently
        unrecoverable — the number is gone the moment the page updates. The GMP
        sites publish a dated history per IPO, so a day lost to a failed job can
        be read back afterwards instead of being a hole in the trail forever.

        Every row is vetted individually by `vet_gmp`, so one bad row in an
        otherwise good table is dropped rather than poisoning the backfill.
        """
        if not self.available():
            raise AiUnavailable("Gemini not configured; cannot research.")

        hint = f"\nThe IPO's upper price band is ₹{price_high:g}." if price_high else ""
        window = f"\nOnly rows dated {since} or later are of interest." if since else ""
        target = ("\nPrefer these sources:\n" + "\n".join(urls)) if urls else ""
        prompt = f"""Find the FULL dated grey market premium (GMP) history for
the Indian IPO "{company}" — every day the page lists, not just today.{hint}{window}{target}

This is for a finance channel, so accuracy matters more than completeness.

Rules:
- One entry per date the source actually prints. Copy the dates exactly as
  shown; do not fill in days the table does not have.
- GMP in rupees per share. NOT the Kostak rate, NOT subject-to-sauda. If a row
  shows several numbers pick the one under the GMP column.
- Never interpolate a missing day, never carry a value forward into a date the
  page does not show, and never extend the series past the last printed row.
  A short list is a correct answer; an invented row is not.
- If you find no dated table at all, return {{"points": []}}.

Return ONLY JSON:
{{"points": [{{"as_of": "YYYY-MM-DD", "gmp": <number>, "kostak": <number or null>}}, ...],
  "confidence": "high" | "medium" | "low",
  "note": "<which page the table came from, or why none was found>"}}"""

        text, sources = self._generate_grounded(prompt, urls)
        data = _parse_json(text, default={})
        confidence = str(data.get("confidence") or "low").lower()
        note = str(data.get("note") or "").strip()

        out: list[dict[str, Any]] = []
        for row in (data.get("points") or []):
            if not isinstance(row, dict):
                continue
            gmp = row.get("gmp")
            if not isinstance(gmp, (int, float)):
                continue
            point = {
                "gmp": float(gmp),
                "date": row.get("as_of") or None,
                "kostak": row.get("kostak") if isinstance(row.get("kostak"), (int, float)) else 0,
                "confidence": confidence,
                "note": note,
                "sources": sources,
                "source": "gemini",
            }
            point.update(vet_gmp(point, price_high))
            out.append(point)

        out.sort(key=lambda p: p["date"] or "")
        return out

    def research_subscription(
        self, company: str, *, urls: list[str] | None = None
    ) -> dict[str, Any]:
        """Read the live subscription figures off a broker or exchange page.

        Safer than GMP — the underlying numbers come from the exchanges and are
        republished by regulated brokers. The risk here isn't fabrication, it's
        staleness: subscription climbs through the day, so the timestamp and
        the day number matter as much as the multiples.
        """
        if not self.available():
            raise AiUnavailable("Gemini not configured; cannot research.")

        target = ("\nRead these pages:\n" + "\n".join(urls)) if urls else ""
        prompt = f"""Find the CURRENT subscription figures for the Indian IPO
"{company}".{target}

These are the "times subscribed" multiples per investor category, as published
by the exchanges (NSE/BSE) or a broker republishing them.

Rules:
- Report multiples, not application counts or amounts. "2.31x" -> 2.31.
- QIB, NII (also shown as HNI or NIB), Retail (RII), Employee, and the overall
  total. Omit any category the page does not show.
- "day" is which day of bidding the figures are from (1, 2 or 3).
- "as_of" is the date the figures apply to, from the page.
- If the issue has not opened, or you cannot find figures for this exact
  company, set every number to null. That is a correct answer.
- Do not compute the total yourself — report the overall figure the page shows.

Return ONLY JSON:
{{"qib": <number or null>, "nii": <number or null>, "retail": <number or null>,
  "employee": <number or null>, "total": <number or null>,
  "day": <1|2|3 or null>, "as_of": "YYYY-MM-DD or null",
  "confidence": "high" | "medium" | "low",
  "note": "<which page, and the timestamp printed on it>"}}"""

        text, sources = self._generate_grounded(prompt, urls)
        data = _parse_json(text, default={})
        num = lambda k: (float(data[k]) if isinstance(data.get(k), (int, float)) else None)
        out = {
            "qib": num("qib"), "nii": num("nii"), "retail": num("retail"),
            "employee": num("employee"), "total": num("total"),
            "day": int(data["day"]) if isinstance(data.get("day"), (int, float)) else None,
            "as_of": data.get("as_of") or None,
            "confidence": str(data.get("confidence") or "low").lower(),
            "note": str(data.get("note") or "").strip(),
            "sources": sources,
        }
        out.update(vet_subscription(out))
        return out

    def research_ipo(self, company: str, *, urls: list[str] | None = None) -> dict[str, Any]:
        """Look up the structural facts of an IPO — the slow-moving ones.

        Much safer than GMP: price band, lot size and dates are published by
        the exchanges and don't change hourly. Still returned for review.
        """
        if not self.available():
            raise AiUnavailable("Gemini not configured; cannot research.")

        target = ("\nRead these pages:\n" + "\n".join(urls)) if urls else ""
        prompt = f"""Find the published issue details for the Indian IPO
"{company}".{target}

Use only figures stated on pages you retrieved (exchange notices, RHP
summaries, or major financial press). Leave any field null rather than
guessing. Amounts in rupees crore, prices in rupees.

Return ONLY JSON:
{{"company": "<full registered name>",
  "board": "Mainboard" | "SME" | null,
  "sector": "<short sector description>",
  "fresh_cr": <number or null>, "ofs_cr": <number or null>,
  "price_low": <number or null>, "price_high": <number or null>,
  "lot_size": <number or null>,
  "shares_post_issue_cr": <number or null>,
  "registrar": "<name or null>",
  "announced": "YYYY-MM-DD or null",
  "open": "YYYY-MM-DD or null", "close": "YYYY-MM-DD or null",
  "allotment": "YYYY-MM-DD or null", "listing": "YYYY-MM-DD or null",
  "confidence": "high" | "medium" | "low",
  "note": "<sources used, or what was missing>"}}

"announced" is the date the price band was made public (the RHP filing or
band announcement), NOT the opening date. No exchange feed carries it, so it
is null unless a page states it outright."""

        text, sources = self._generate_grounded(prompt, urls)
        data = _parse_json(text, default={})
        data["sources"] = sources
        return data

    def research_background(
        self, company: str, *, sector: str = "", hq: str = "",
        known: str = "", force: bool = False,
    ) -> dict[str, Any]:
        """General awareness about the company: who they are, in context.

        The one thing on reel 1 that cannot come from the filing. `about_company`
        says what the business does; this says what a viewer who has never heard
        the name should know — how established it is, what it is known for, who
        it competes with, where it sits in its market.

        Written ONCE, when the IPO is first discovered, and never refreshed:
        none of it changes over an issue's three-week life, so re-asking would
        spend a request a day to receive the same paragraph.

        No dates, and that restriction is the whole design. An earlier attempt at
        this asked for *recent news* and got dated headlines back with zero
        grounding sources attached — "2024-06-12: Shiprocket expanded to 220
        countries" — which is parametric recall wearing a citation's clothes, on
        a card that says LIVE. Undated background carries no recency claim, so
        stale knowledge is merely background rather than a false statement. If
        you ever want real news here, it needs a source that can be cited.

        `known` is the filing's own description, passed in so the model can add
        context around it instead of repeating it back.
        """
        if not self.available():
            raise AiUnavailable("Gemini not configured; cannot research.")

        facts = "\n".join(x for x in (
            f"Sector: {sector}" if sector else "",
            f"Head office: {hq}" if hq else "",
            f"What its filing says it does: {known[:1200]}" if known else "",
        ) if x)

        prompt = f"""Write general-awareness background on the Indian company
"{company}" — the context a retail viewer who has never heard the name needs
before deciding anything. This is for an explainer video, not a news bulletin.

{facts}

Wanted, in rough order of usefulness:
  * how long it has been going and how established it is
  * what it is best known for — brands, flagship products, reputation
  * where it sits in its market: leader, challenger, niche; who it is up
    against by name if that is widely known
  * anything a reasonably informed Indian viewer would already associate
    with the name

Hard rules:
  * NO DATES and no claims about recent or current events. You cannot verify
    when anything happened, so anything time-sensitive is out: no "recently",
    no "last year", no funding rounds, launches, results or appointments.
    Durable facts only — things that were true five years ago and still are.
  * NO FIGURES unless they appear in the facts above. Revenue, store counts,
    headcount, market share and valuations are exactly what gets misremembered.
  * Do not repeat what the filing already says. Add context around it.
  * If you do not actually know this company — most SME issues are genuinely
    obscure — return FEWER points, or an empty list. Three real lines beat
    five invented ones, and an empty list is a correct answer that the caller
    handles. Never pad to reach a count.

Each point one short line, under about 16 words, plain spoken English.

Return ONLY JSON:
{{"background": ["...", "..."],
  "confidence": "high" | "medium" | "low",
  "note": "<what you actually knew, or why the list is short>"}}"""

        key = _hash({"m": self.model, "p": prompt})
        if not force:
            hit = self._cached("bg", key)
            if hit is not None:
                return hit

        text, sources = self._generate_grounded(prompt)
        data = _parse_json(text, default={})
        out = {
            "background": [str(x).strip() for x in (data.get("background") or [])
                           if str(x).strip()][:4],
            "confidence": str(data.get("confidence") or "low"),
            "note": str(data.get("note") or ""),
            "sources": sources,
        }
        self._store("bg", key, out)
        return out

    def research_financials(
        self, company: str, *, years: list[str] | None = None,
        urls: list[str] | None = None,
    ) -> dict[str, Any]:
        """The three-year table out of the RHP, plus EPS and the peer P/E.

        The one block nothing free publishes as data — it lives in a PDF
        hundreds of pages long, which is why it stayed empty on every IPO and
        blanked reel 4 entirely. Grounded search can read the summary pages
        that reproduce it. Returned for review like everything else: these
        numbers drive the fundamentals and valuation halves of the score, so a
        wrong one is worse than a missing one.
        """
        if not self.available():
            raise AiUnavailable("Gemini not configured; cannot research.")

        span = years or ["FY23", "FY24", "FY25"]
        target = ("\nRead these pages:\n" + "\n".join(urls)) if urls else ""
        prompt = f"""Find the restated financials for the Indian IPO
"{company}" as disclosed in its RHP / DRHP.{target}

Report the THREE most recent complete financial years the source actually
shows, oldest first, and name them in "years" exactly as the source labels
them (e.g. ["FY24","FY25","FY26"]). Do not force them to {", ".join(span)} —
report what is there. All amounts in RUPEES CRORE.

Rules that matter more than completeness:
  * Use only figures stated on pages you actually retrieved.
  * Every array must be the same length as "years", in the same order.
  * Do not interpolate a year, do not shift the others along, and never
    substitute a nine-month or half-year figure for a full year. If a year
    is genuinely absent, drop it from "years" too rather than padding.
  * Report each metric independently. A metric you cannot find should be an
    empty array — that does NOT stop you reporting the ones you can find.
    Revenue and PAT alone are a useful answer; net worth and total debt are
    often absent from summary pages and their absence must not suppress the
    rest.
  * If you find no financial figures at all, return "years": [] and every
    array empty. That is a correct answer.
  * "eps" is post-issue diluted EPS for the latest year, in rupees.
  * "pe_peer_avg" is the average P/E of the listed peer group as printed in
    the RHP's "Comparison with listed industry peers" section.

Return ONLY JSON:
{{"years": ["FY..", "FY..", "FY.."],
  "revenue": [<number>, ...],
  "ebitda": [...], "pat": [...], "net_worth": [...], "total_debt": [...],
  "eps": <number or null>, "pe_peer_avg": <number or null>,
  "confidence": "high" | "medium" | "low",
  "note": "<which document/section, or what was missing>"}}"""

        text, sources = self._generate_grounded(prompt, urls)
        data = _parse_json(text, default={})
        data["sources"] = sources
        return data


# ── helpers ────────────────────────────────────────────────────────────────

def _as_object(value: Any, what: str) -> dict:
    """Coerce a parsed JSON payload to the dict the caller expects.

    Why this exists: `_parse_json` returns whatever the model emitted, and two
    callers immediately do `out.get(...)`. When Gemini wrapped its answer in a
    top-level ARRAY — `[{"overview": [...], ...}]`, which it does intermittently
    however firmly the prompt asks for an object — that became

        AttributeError: 'list' object has no attribute 'get'

    and every `analyse` call died. The visible symptom was nothing to do with
    JSON: overview bullets, green/red flags and the peer P/E stayed blank on
    every tracked IPO, `doctor` reported 54 empty fields, and `enrich` recorded
    the crashed step as "attempted" and then declined to retry it for a week.

    Two shapes are accepted because both are the model being sloppy rather than
    wrong: a single-element array around the object, and the answer split
    across several objects in one array. Merging is safe — the keys are
    disjoint field names, and a later duplicate winning is no worse than the
    arbitrary choice any other rule would make.

    Anything else raises, deliberately. A string or a number here means the
    model answered a different question, and inventing an empty dict from it
    would write a blank analysis over a real one.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        merged: dict = {}
        for item in value:
            if isinstance(item, dict):
                merged.update(item)
        if merged:
            return merged
    raise AiUnavailable(
        f"Gemini returned a {type(value).__name__} where {what} needed an "
        f"object: {str(value)[:160]}")


def _parse_json(text: str, default: Any = None) -> Any:
    text = (text or "").strip()
    if not text:
        if default is not None:
            return default
        raise AiUnavailable("Gemini returned an empty response")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"[\[{].*[\]}]", text, re.S)   # fenced or chatty output
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
    if default is not None:
        return default
    raise AiUnavailable(f"Gemini returned unparseable output: {text[:200]}")


def _grounding_urls(resp: Any) -> list[str]:
    """Pull citation URLs out of the response, defensively.

    Two tools, two places. `google_search` reports what it read under
    `grounding_metadata.grounding_chunks`; `url_context` reports it under
    `url_context_metadata.url_metadata`, and only counts as a citation when
    retrieval actually succeeded. Reading just the first is what made every
    pinned-URL lookup come back "no source citation" and get flagged by
    vet_gmp — the answer was properly sourced, we were looking in the wrong
    field for the proof.
    """
    urls: list[str] = []

    def add(uri: Any) -> None:
        if uri and uri not in urls:
            urls.append(str(uri))

    for cand in (getattr(resp, "candidates", None) or []):
        try:
            meta = getattr(cand, "grounding_metadata", None)
            for chunk in (getattr(meta, "grounding_chunks", None) or []):
                add(getattr(getattr(chunk, "web", None), "uri", None))
        except Exception:
            pass
        try:
            ctx = getattr(cand, "url_context_metadata", None)
            for item in (getattr(ctx, "url_metadata", None) or []):
                status = str(getattr(item, "url_retrieval_status", "") or "")
                if "SUCCESS" not in status.upper():
                    continue          # a failed fetch is not a source
                add(getattr(item, "retrieved_url", None))
        except Exception:
            pass
    return urls[:6]


def vet_gmp(result: dict, price_high: float) -> dict:
    """Decide whether a researched GMP is safe to accept unreviewed.

    This is the guardrail that keeps a hallucinated number off the channel.
    Anything uncited, undated, low-confidence or implausible against the price
    band gets flagged; the CLI then refuses to write it without --force.
    """
    gmp = result.get("gmp")
    if gmp is None:
        return {"needs_review": True, "reason": "no figure found"}
    if not result.get("sources"):
        return {"needs_review": True, "reason": "no source citation"}
    if result.get("confidence") == "low":
        return {"needs_review": True, "reason": "model reported low confidence"}
    if not result.get("date"):
        return {"needs_review": True, "reason": "undated figure"}
    if price_high:
        pct = gmp / price_high * 100
        # A real GMP outside roughly -30%..+150% of the band is almost always a
        # misread — usually the Kostak rate or a per-lot amount.
        if pct > 150 or pct < -30:
            return {"needs_review": True,
                    "reason": f"implausible: {pct:.0f}% of the price band"}
    return {"needs_review": False, "reason": ""}


def vet_subscription(result: dict) -> dict:
    """Sanity-check researched subscription figures.

    The failure mode here is staleness and unit confusion (an amount in crore
    read as a multiple), not invention — so the checks are about shape.
    """
    cats = {k: result.get(k) for k in ("qib", "nii", "retail", "employee")}
    total = result.get("total")
    present = {k: v for k, v in cats.items() if v is not None}

    if total is None and not present:
        return {"needs_review": True, "reason": "no figures found"}
    if not result.get("sources"):
        return {"needs_review": True, "reason": "no source citation"}
    if result.get("confidence") == "low":
        return {"needs_review": True, "reason": "model reported low confidence"}
    if not result.get("as_of"):
        return {"needs_review": True, "reason": "undated figures"}
    if any(v < 0 for v in present.values()) or (total is not None and total < 0):
        return {"needs_review": True, "reason": "negative multiple"}
    # Multiples above ~1000x essentially never happen; that reads as an
    # application count or a rupee amount rather than a subscription multiple.
    if any(v > 1000 for v in present.values()) or (total or 0) > 1000:
        return {"needs_review": True, "reason": "implausible multiple (>1000x)"}
    # The overall total must sit within the per-category range; well outside it
    # usually means a category was misread.
    if total is not None and present:
        lo, hi = min(present.values()), max(present.values())
        if total > hi * 1.5 + 1 or total < lo / 1.5 - 1:
            return {"needs_review": True,
                    "reason": f"total {total}x inconsistent with categories {lo}-{hi}x"}
    return {"needs_review": False, "reason": ""}


def iso_today() -> str:
    return datetime.now(timezone.utc).astimezone().date().isoformat()


# ── fallbacks used when AI is off ──────────────────────────────────────────

ALLOTMENT_STEPS = {
    "en": [
        "Open the registrar or BSE allotment page",
        "Pick the IPO name from the dropdown",
        "Enter PAN or Application Number",
        "Hit Search — status shows instantly",
    ],
    "hi": [
        "रजिस्ट्रार या BSE अलॉटमेंट पेज खोलें",
        "ड्रॉपडाउन से IPO का नाम चुनें",
        "PAN या एप्लिकेशन नंबर डालें",
        "सर्च दबाएँ — स्टेटस तुरंत दिखेगा",
    ],
    "te": [
        "రిజిస్ట్రార్ లేదా BSE అలాట్‌మెంట్ పేజీ తెరవండి",
        "డ్రాప్‌డౌన్‌లో IPO పేరు ఎంచుకోండి",
        "PAN లేదా అప్లికేషన్ నంబర్ ఎంటర్ చేయండి",
        "సెర్చ్ నొక్కండి — స్టేటస్ వెంటనే కనిపిస్తుంది",
    ],
}
