"""What we have spent against the Gemini free tier, and the guard that keeps
us inside it.

── Why this exists ────────────────────────────────────────────────────────

The console's own "peak usage per model over the last 1 day" table, read on
2026-09-06, said this:

    Gemini 3.5 Flash Lite    16 / 15 RPM     10.46K / 250K TPM    0 / 500 RPD
    Gemini 3.1 Flash Lite    13 / 15 RPM      9.71K / 250K TPM    0 / 500 RPD
    Gemma 4 31B               2 / 30 RPM      3.05K / 16K  TPM    3 / 14.4K RPD
    Gemma 4 26B               1 / 30 RPM        739 / 16K  TPM    3 / 14.4K RPD

**16 / 15.** We went over the requests-per-minute cap on the model the whole
pipeline leans on. Nothing in the codebase was counting, so nothing could have
stopped it: `enrich` and `translate` loop over IPOs and languages and call as
fast as the network allows, and at four or five calls a second a fifteen-a-
minute cap is gone in three seconds.

A 429 is not a soft failure here either. `ai.Gemini._generate` treats quota as
a fact about one model and walks down to the next candidate — so an RPM breach
does not raise, it silently *demotes* the run onto a weaker model. That is
exactly how the analysis ended up being written by Gemma 4 26B (16K TPM, no
tool support) while flash-lite sat there rate-limited. The symptom was "the
prose got worse", three files away from the cause.

── What this module does, and what it cannot do ───────────────────────────

`wait_for_slot()` is the enforcement, and it is the only part that actually
prevents anything: before each call it counts what this process has sent in
the trailing 60 seconds and sleeps until there is room. It targets one below
the cap (see HEADROOM) because Google's minute window and ours are not the
same minute, and landing exactly on 15 is how you get 16.

`snapshot()` is the reporting, and it is honest about its blind spot: the
ledger is a file on **this machine**, while the quota is per API key. A
GitHub Actions runner starts with an empty ledger every run, so an Actions run
and a local run can each believe they are at 8 RPM while the key sees 16. The
one signal that sees through that is a 429 — the key telling us it was hit,
whoever hit it — so those are recorded separately and weighed heaviest.

Nothing here talks to a console API to read the real figures. There isn't one
on the free tier; the table above is a web page.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

from .store import CACHE_DIR

# ── the caps ───────────────────────────────────────────────────────────────
#
# Matched on substring, longest-specific first — "flash-lite" must be tested
# before "flash" or every lite model reads as flash. Same ordering trap as
# `ai._TIER`, which is why both live next to a comment saying so.
#
# Figures are the free tier as published in the AI Studio rate-limit table.
# When Google changes them, change them here: this is the only copy, and
# `ai.py`'s ranking comment points at it rather than repeating the numbers.
FAMILIES: tuple[tuple[str, dict[str, int]], ...] = (
    ("flash-lite", {"rpm": 15, "tpm": 250_000, "rpd": 500}),
    ("flash",      {"rpm": 5,  "tpm": 250_000, "rpd": 20}),
    ("gemma",      {"rpm": 30, "tpm": 16_000,  "rpd": 14_400}),
    ("pro",        {"rpm": 5,  "tpm": 250_000, "rpd": 100}),
)
UNKNOWN = {"rpm": 5, "tpm": 16_000, "rpd": 20}   # assume the meanest tier

# One request of headroom. The cap is enforced by Google over its own sliding
# minute, which starts wherever it starts; aiming at exactly `rpm` means any
# overlap between the two windows spills over — which is precisely what the
# 16/15 reading was. Costs one request a minute and removes the whole class.
HEADROOM = 1

# Kept for a day and a bit: long enough for "peak over the last 24h" to mean
# what the console means by it, short enough that the file stays small.
KEEP_SECONDS = 30 * 3600

# A cap this close to the limit is worth saying out loud before it bites.
NEAR = 0.80

_LOCK = threading.Lock()
_LEDGER = CACHE_DIR / "ai-usage.json"


def limits_for(model: str) -> dict[str, int]:
    """The free-tier caps for whichever family this model id belongs to."""
    name = (model or "").lower()
    for key, caps in FAMILIES:
        if key in name:
            return caps
    return UNKNOWN


def family(model: str) -> str:
    name = (model or "").lower()
    for key, _ in FAMILIES:
        if key in name:
            return key
    return "unknown"


# ── the ledger ─────────────────────────────────────────────────────────────

def _load() -> list[dict[str, Any]]:
    try:
        rows = json.loads(_LEDGER.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(rows, list):
        return []
    cut = time.time() - KEEP_SECONDS
    return [r for r in rows if isinstance(r, dict) and float(r.get("t", 0)) > cut]


def _save(rows: list[dict[str, Any]]) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _LEDGER.write_text(json.dumps(rows), encoding="utf-8")
    except OSError:
        pass          # a ledger that cannot be written degrades reporting only


def record(model: str, tokens: int = 0, quota: bool = False,
           ok: bool = True) -> None:
    """One call happened. Cheap enough to sit in the hot path.

    `quota=True` means the API answered 429 / RESOURCE_EXHAUSTED, which is the
    only observation here that sees the whole key rather than this machine.
    """
    with _LOCK:
        rows = _load()
        rows.append({"t": round(time.time(), 2), "m": model or "?",
                     "tok": int(tokens or 0),
                     **({"q": 1} if quota else {}),
                     **({} if ok else {"e": 1})})
        _save(rows)


def wait_for_slot(model: str, now: float | None = None) -> float:
    """Sleep until this process may call `model` again. Returns seconds slept.

    Counts only this process's own calls in the trailing minute — see the
    module docstring on why that is a floor and not a guarantee. Sleeping is
    the right verb rather than raising: every caller is a batch loop that would
    otherwise abandon work over a limit that clears itself in seconds.
    """
    if os.getenv("IPOPULSE_NO_THROTTLE"):
        return 0.0                        # for tests and for a deliberate burst
    cap = max(1, limits_for(model)["rpm"] - HEADROOM)
    now = now or time.time()
    with _LOCK:
        rows = _load()
    fam = family(model)
    recent = sorted(float(r["t"]) for r in rows
                    if family(str(r.get("m", ""))) == fam
                    and float(r.get("t", 0)) > now - 60)
    if len(recent) < cap:
        return 0.0
    # The oldest call inside the window is the one whose expiry frees a slot.
    wait = max(0.0, 60.0 - (now - recent[-cap]) + 0.05)
    if wait > 0:
        time.sleep(wait)
    return round(wait, 2)


# ── reporting ──────────────────────────────────────────────────────────────

def _peak_per_minute(times: list[float]) -> int:
    """Most calls seen in any 60-second window, by sliding over the calls.

    Same definition the console's "peak RPM" uses, so the two numbers are
    comparable — an average would have read 0.2 RPM on the day we hit 16.
    """
    times = sorted(times)
    best = 0
    start = 0
    for end in range(len(times)):
        while times[end] - times[start] > 60:
            start += 1
        best = max(best, end - start + 1)
    return best


def snapshot(now: float | None = None) -> dict[str, Any]:
    """Peak usage per model over the last day, against the caps."""
    now = now or time.time()
    rows = _load()
    day = [r for r in rows if float(r.get("t", 0)) > now - 86_400]
    models: dict[str, dict[str, Any]] = {}
    for r in day:
        m = str(r.get("m", "?"))
        slot = models.setdefault(m, {"times": [], "tokens": [], "quota": 0,
                                     "errors": 0})
        slot["times"].append(float(r.get("t", 0)))
        slot["tokens"].append(int(r.get("tok", 0)))
        slot["quota"] += int(r.get("q", 0))
        slot["errors"] += int(r.get("e", 0))

    out = {}
    for m, slot in sorted(models.items()):
        caps = limits_for(m)
        peak_rpm = _peak_per_minute(slot["times"])
        out[m] = {
            "family": family(m),
            "calls_1d": len(slot["times"]),
            "peak_rpm": peak_rpm,
            "tokens_1d": sum(slot["tokens"]),
            "quota_429": slot["quota"],
            "errors": slot["errors"],
            "limits": caps,
            "pct_rpm": round(100 * peak_rpm / caps["rpm"], 1) if caps["rpm"] else 0,
            "pct_rpd": round(100 * len(slot["times"]) / caps["rpd"], 1) if caps["rpd"] else 0,
        }
    return {
        "at": now,
        "models": out,
        "ledger": str(_LEDGER),
        "seen_calls": len(day),
        # Said in the payload, not just in this file's docstring, because the
        # figure is quoted in an issue somebody reads without the source open.
        "scope": "this machine only; the quota is per API key. A 429 count "
                 "above zero is the key's own answer and is authoritative.",
    }


def findings(now: float | None = None) -> list[dict[str, str]]:
    """Anything about our own spend that a person should look at."""
    out: list[dict[str, str]] = []
    snap = snapshot(now)
    for model, s in snap["models"].items():
        caps, lim = s["limits"], s["limits"]["rpm"]
        if s["quota_429"]:
            out.append({
                "severity": "error", "slug": model, "source": "usage",
                "what": "Rate limited by the API",
                "detail": f"{s['quota_429']} call(s) came back 429 in the last "
                          f"day. `ai._generate` answers a 429 by demoting to a "
                          f"weaker model rather than failing, so the visible "
                          f"symptom is worse output, not an error.",
            })
        if s["peak_rpm"] > lim:
            out.append({
                "severity": "error", "slug": model, "source": "usage",
                "what": "Over the RPM cap",
                "detail": f"peak {s['peak_rpm']} calls/min against a cap of "
                          f"{lim} ({s['pct_rpm']}%). The throttle targets "
                          f"{max(1, lim - HEADROOM)}; a reading above the cap "
                          f"means two processes ran at once.",
            })
        elif s["peak_rpm"] >= lim * NEAR:
            out.append({
                "severity": "warn", "slug": model, "source": "usage",
                "what": "Close to the RPM cap",
                "detail": f"peak {s['peak_rpm']}/min against {lim} "
                          f"({s['pct_rpm']}%).",
            })
        if s["pct_rpd"] >= 100:
            out.append({
                "severity": "error", "slug": model, "source": "usage",
                "what": "Daily request budget spent",
                "detail": f"{s['calls_1d']} of {caps['rpd']} requests. "
                          f"Nothing model-written will work until the reset.",
            })
        elif s["pct_rpd"] >= NEAR * 100:
            out.append({
                "severity": "warn", "slug": model, "source": "usage",
                "what": "Daily request budget nearly spent",
                "detail": f"{s['calls_1d']} of {caps['rpd']} ({s['pct_rpd']}%).",
            })
    return out
