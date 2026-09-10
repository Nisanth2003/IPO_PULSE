"""Constants that exist twice, checked for having drifted apart.

The studio is JavaScript and the pipeline is Python, and the browser cannot
import Python. So a handful of numbers and lists exist in both languages, and
nothing has ever verified they agree.

That is not hypothetical. On 11 Sep 2026 the user noticed reel 8 saying "5 of
8 stocks" while reel 7 showed 6, and the cause was exactly this: the card
sliced three setups a side, the scorer read every stored row, and the two
numbers had no relationship. Two calls nobody had seen were being graded.
Nothing was broken, nothing failed, and it had been live for as long as reel 8
had existed.

The failure mode is what makes it worth a check rather than a comment: a
drifted mirror produces a *plausible wrong answer* on both sides
independently, so neither side looks wrong on its own. The only way to see it
is to compare them.

── what this can and cannot see ─────────────────────────────────────────

It reads the JavaScript as TEXT, with comments stripped, and pulls out
`const NAME = value`. That is deliberately crude:

  * it cannot evaluate JS, so a constant computed at runtime is invisible to
    it and must not be used for a mirrored value;
  * it strips comments first, because a `5` inside a sentence explaining the
    5 is not a second definition — an earlier version of this check reported
    the prose as a mismatch.

Anything it cannot parse is reported as a finding rather than skipped. A
mirror check that silently passes when it failed to read the file is worse
than no check, because it converts "unknown" into "fine".
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# The studio, relative to this package. `backend/ipopulse/` -> repo root.
FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


def _strip_comments(js: str) -> str:
    """Remove // and /* */ comments, leaving string literals alone.

    Needed because every mirrored constant here is documented in a comment
    that quotes its own value, and a naive search finds the prose. Done by
    scanning rather than by regex: a regex for /* */ eats the contents of any
    string containing those characters, and several of these files hold URLs.
    """
    out: list[str] = []
    i, n = 0, len(js)
    state: str | None = None
    while i < n:
        c, two = js[i], js[i:i + 2]
        if state == "line":
            if c == "\n":
                state = None
                out.append(c)
        elif state == "block":
            if two == "*/":
                state = None
                i += 1
        elif state in ('"', "'", "`"):
            out.append(c)
            if c == "\\":
                i += 1
                if i < n:
                    out.append(js[i])
            elif c == state:
                state = None
        else:
            if two == "//":
                state = "line"
                i += 1
            elif two == "/*":
                state = "block"
                i += 1
            else:
                if c in ('"', "'", "`"):
                    state = c
                out.append(c)
        i += 1
    return "".join(out)


def js_const(path: Path, name: str) -> tuple[Any, str]:
    """`(value, "")` for a top-level `const NAME = …`, or `(None, why)`.

    Handles a number and a bracketed list of quoted strings, which is every
    mirrored shape here. Anything else returns a reason rather than a guess.
    """
    try:
        src = _strip_comments(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return None, f"cannot read {path.name}: {exc}"

    m = re.search(rf"\bconst\s+{re.escape(name)}\s*=\s*(.+?);", src, re.S)
    if not m:
        return None, f"{path.name} has no top-level `const {name}`"
    raw = m.group(1).strip()

    if re.fullmatch(r"-?\d+(\.\d+)?", raw):
        return (float(raw) if "." in raw else int(raw)), ""
    if raw.startswith("["):
        items = re.findall(r"""['"]([^'"]*)['"]""", raw)
        if not items:
            return None, f"{path.name}: `{name}` is a list this cannot read"
        return items, ""
    return None, (f"{path.name}: `{name}` is not a plain number or list of "
                  f"strings — a mirrored constant has to be readable as text")


# ── the mirrors ──────────────────────────────────────────────────────────
#
# Each entry is (label, python value, js file, js const, why it matters).
# Add to this list whenever a value starts existing in both languages; the
# cost of forgetting is a wrong number on air that nothing reports.

def _pairs() -> list[tuple]:
    from . import scorecard, tables
    from .outlook import PUBLISHED_PER_SIDE

    return [
        ("setups published per side",
         PUBLISHED_PER_SIDE, "js/reels.js", "PUBLISHED_PER_SIDE",
         "the card shows this many a side and reels 8/9 grade exactly that. "
         "Drifted once already: the scorer read 8 stored rows while the card "
         "showed 6, so two unpublished calls were being graded."),

        ("sessions before a rate may be stated",
         scorecard.ENOUGH_SESSIONS, "js/data.js", "ENOUGH_SESSIONS",
         "below this both scorecard reels must say 'too early to say'. A "
         "backend raise that the cards ignore means a percentage on air the "
         "pipeline considers too thin to quote."),

        ("tabs the studio reads",
         sorted(tables.ALL_TABS), "js/data.js", "TABS",
         "a tab the backend writes but data.js does not list is a feature "
         "that silently shows nothing in the studio."),
    ]


def check() -> list[dict[str, str]]:
    """Every mirrored constant that disagrees, as findings."""
    out: list[dict[str, str]] = []
    for label, py, rel, const, why in _pairs():
        path = FRONTEND / rel
        got, err = js_const(path, const)
        if err:
            out.append({"severity": "error", "mirror": label,
                        "what": f"cannot verify `{const}`",
                        "detail": f"{err}. {why}"})
            continue

        if isinstance(py, list):
            # The studio is allowed to read tabs the backend does not own —
            # nothing breaks. The reverse is the fault: a tab written and
            # never read is a feature that shows nothing.
            missing = sorted(set(py) - set(got))
            if missing:
                out.append({
                    "severity": "error", "mirror": label,
                    "what": f"{rel} does not read {len(missing)} tab(s)",
                    "detail": f"{', '.join(missing)} — {why}"})
            continue

        if py != got:
            out.append({
                "severity": "error", "mirror": label,
                "what": f"{const} disagrees: Python {py}, {rel} {got}",
                "detail": why})
    return out


def report(findings: list[dict[str, str]]) -> list[str]:
    if not findings:
        return ["── Mirrors: every cross-language constant agrees"]
    out = [f"── Mirrors: {len(findings)} disagreement(s)"]
    for f in findings:
        out.append(f"   !! {f['mirror']}: {f['what']}")
        out.append(f"      {f['detail']}")
    return out
