"""The Antigravity managed agent: open-ended research, read-only.

Everything else in `providers/` fetches a known endpoint and parses a known
shape. This does the opposite job — it answers questions nobody wrote a
scraper for, because the answer lives in prose across several pages that
disagree with each other.

The three findings that motivated it had all sat unresolved for a week,
because each needs judgement rather than a fetch:

    manipal-payment   6,504,348 shares x cap 339 = Rs 220 Cr, against a
                      stated issue size of Rs 805 Cr. Which figure is wrong?
    gaja / lalithaa   PAT above EBITDA across several years. A restatement,
    / rays-of-belief  an other-income line, or a transcription error?

`invariants` can tell you those cannot both be true. It cannot tell you which
one to believe, and no endpoint returns that.

── the hard rule, enforced structurally ─────────────────────────────────

**Nothing this module returns is ever written to the sheet.**

It returns prose and citations for a person to read. There is no `--write`
path, no field mapping, and no parser: `answer` is a string and `sources` is
a list of URLs. That is deliberate and it is stricter than
`no-gemini-invented-numbers` needed to be, for a specific reason — an agent
that browses the web and executes code is BETTER at producing a plausible
wrong number than a plain model call, not worse. It can show its working and
still be wrong, which is the most persuasive kind of wrong.

So the division of labour is: the agent finds the argument and the evidence,
a human decides, and the existing verified paths do the writing.

── cost, measured rather than assumed ───────────────────────────────────

Measured twice, and the second number is the one to plan on:

    a one-sentence question, search only        20,355 tokens
    one real cross-check of a company's filings 153,374 tokens

Both against a stated `max_total_tokens` of 8,000 and 60,000 respectively —
so the budget is ADVISORY and does not cap anything. 153k for one question
means this is not a per-IPO tool: 42 companies would be ~6.4M tokens. It is
for the handful of questions that are actually stuck, which is what
`investigate --finding` restricts it to.

The 153k answer was worth it. It resolved a week-old finding AND named the
mechanism — the wrong share count came from a concurrent issue — which a
deterministic check then generalised into `invariants.cross_record`, finding
a second contaminated pair for free. That is the shape to aim for: pay for
the mechanism once, then encode it.
"""

from __future__ import annotations

import os
import time
from typing import Any

# Pinned rather than "latest": this is a preview agent and its behaviour is
# the thing being relied on. A silent version change here would look like a
# research quality regression with no commit behind it.
AGENT = os.getenv("IPOPULSE_AGENT_MODEL") or "antigravity-preview-05-2026"

# Advisory, not a cap — measured 20,355 tokens against a stated 8,000. Set
# high enough that a genuine multi-page cross-check is not cut off halfway,
# which would produce a confident half-answer: the worst outcome available.
BUDGET = int(os.getenv("IPOPULSE_AGENT_BUDGET") or 60000)

# `google_search` and `url_context` are what make this better than a plain
# grounded call: the agent can follow a disagreement across pages.
#
# `code_execution` is NOT here, and that was measured rather than assumed.
# The same question with it ran past nine minutes without completing; without
# it, it completed inside `create`. It also earns less than it looks like it
# should — the arithmetic in these questions is one multiplication, and the
# agent got Rs 805 Cr from 23,746,313 x 339 correctly in prose. Add it back
# only with a question that genuinely needs a program, and expect the wait.
TOOLS = [{"type": "google_search"},
         {"type": "url_context"}]

# An interaction is ASYNCHRONOUS. `create` returns status `incomplete` with no
# output — it has started a loop, not answered. Reading `output_text` off the
# create response reports "returned 0 characters", which is true and useless.
#
# A one-sentence question does finish inside `create`, which is exactly what
# made this look like it worked when first tried. A question that opens
# several pages takes minutes. So: poll.
POLL_EVERY = 10.0
# 20 minutes. Measured: a one-sentence question finishes inside `create`, and
# a real cross-check of one company's filings was still running at 9 minutes.
# 600s was too short and produced a "still incomplete" error on a question
# that was working correctly — a timeout that fires on healthy work is worse
# than no timeout, because it teaches you the feature is broken.
POLL_LIMIT = float(os.getenv("IPOPULSE_AGENT_TIMEOUT") or 1200)
DONE = ("completed", "failed", "cancelled", "expired")


class AgentUnavailable(RuntimeError):
    """No key, no SDK support, or the interaction failed."""


def available() -> bool:
    """Can an interaction be created at all?"""
    if not (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")):
        return False
    try:
        import google.genai as genai
    except ImportError:
        return False
    try:
        return hasattr(genai.Client(), "interactions")
    except Exception:                                         # noqa: BLE001
        return False


# The instruction every question is wrapped in. It exists to stop the two
# failure modes that would make this worse than useless here:
#
#   * a confident answer with no source, which is indistinguishable from a
#     guess and is exactly what the citation requirement exists to prevent;
#   * an answer that silently picks one figure when the real finding is that
#     two published figures disagree — the disagreement IS the answer, and
#     flattening it hides the thing worth knowing.
FRAME = """You are checking a data contradiction for an Indian IPO research \
desk. Answer ONLY from sources you actually opened, and cite each claim.

Rules:
1. Every number you state must come with the source that published it. If you \
cannot find a source for a figure, say so rather than inferring it.
2. If two sources disagree, SAY THEY DISAGREE and give both figures with both \
sources. Do not silently pick one — the disagreement is the finding.
3. If the honest answer is "the filings do not say", answer that. A wrong \
confident answer here is worse than no answer, because it will be believed.
4. Prefer the company's own RHP/prospectus and the exchange (NSE/BSE) over \
aggregator sites. Name which you used.
5. Be brief. At most 200 words, then the sources.

The question:
{question}
"""


def ask(question: str, budget: int | None = None,
        verbose: bool = False) -> dict[str, Any]:
    """Put one open-ended question to the agent. Returns prose and sources.

    Read-only by construction: the return value has no field mapping and no
    caller writes it. See the module note on why that is stricter here than
    elsewhere.
    """
    if not available():
        raise AgentUnavailable(
            "No GEMINI_API_KEY, or this google-genai has no `interactions`. "
            "The agent is an extra: every other provider still works without "
            "it.")

    import google.genai as genai

    client = genai.Client()
    if verbose:
        print(f"  agent    : {AGENT} (search + url + code, budget {budget or BUDGET:,})")
    try:
        r = client.interactions.create(
            agent=AGENT,
            input=FRAME.format(question=question.strip()),
            # A fresh sandbox each time. Reusing one would carry state between
            # unrelated questions, and the state here is "what I concluded
            # last time" — which is how one wrong answer becomes several.
            environment="remote",
            tools=TOOLS,
            agent_config={"type": "antigravity",
                          "max_total_tokens": budget or BUDGET},
        )
    except Exception as exc:                                  # noqa: BLE001
        raise AgentUnavailable(f"{type(exc).__name__}: {str(exc)[:300]}") from exc

    ident = str(getattr(r, "id", "") or "")
    status = str(getattr(r, "status", "") or "")
    waited = 0.0
    # `get` takes the id POSITIONALLY — `interaction_id=` is a TypeError.
    while status not in DONE and ident and waited < POLL_LIMIT:
        time.sleep(POLL_EVERY)
        waited += POLL_EVERY
        try:
            r = client.interactions.get(ident)
        except Exception as exc:                              # noqa: BLE001
            raise AgentUnavailable(
                f"polling {ident[:12]}… failed: {type(exc).__name__}: "
                f"{str(exc)[:200]}") from exc
        status = str(getattr(r, "status", "") or "")
        if verbose:
            print(f"             {int(waited):>4}s  {status}")

    text = (getattr(r, "output_text", "") or "").strip()
    usage = getattr(r, "usage", None)
    tokens = int(getattr(usage, "total_tokens", 0) or 0) if usage else 0

    if status not in DONE:
        # Left running rather than cancelled: it may still finish, and the id
        # is printed so it can be collected instead of paid for twice.
        raise AgentUnavailable(
            f"still {status} after {int(waited)}s. It may yet finish — "
            f"interaction id {ident}. Raise IPOPULSE_AGENT_TIMEOUT to wait "
            f"longer.")
    if status != "completed" or not text:
        raise AgentUnavailable(
            f"interaction ended {status or 'with no status'} and returned "
            f"{len(text)} characters — nothing to report")

    return {
        "question": question.strip(),
        "answer": text,
        # Extracted for convenience only. The prose already carries inline
        # markers, so this is a summary of them and not a separate claim.
        "sources": _urls(text),
        "tokens": tokens,
        "agent": AGENT,
        "id": ident,
        "waited": int(waited),
    }


def _urls(text: str) -> list[str]:
    """Every distinct URL the answer cited, in the order it cited them."""
    import re

    out: list[str] = []
    for m in re.finditer(r"https?://[^\s\)\]<>\"']+", text):
        u = m.group(0).rstrip(".,;")
        if u not in out:
            out.append(u)
    return out


# ── the questions the watchdog already knows to ask ───────────────────────
#
# Built from a `watch` finding rather than typed by hand, so the thing being
# investigated is provably the thing that was flagged. A question retyped from
# memory is a question about a slightly different number.

def question_for(finding: dict[str, str], ipo: Any = None) -> str:
    """Turn one watchdog finding into a question worth 20k tokens.

    Carries the company's real name and the exact figures from the finding.
    Without the name the agent researches a slug; without the figures it
    researches the general shape of the problem rather than this instance.
    """
    slug = finding.get("slug") or "?"
    what = finding.get("what") or ""
    detail = finding.get("detail") or ""
    company = ""
    if ipo is not None:
        company = str(getattr(ipo, "company", "") or "")

    who = f'"{company}" (slug {slug})' if company else f"the IPO {slug}"
    return (
        f"Our stored record for {who}, an Indian IPO, fails an internal "
        f"consistency check:\n\n"
        f"    {what}\n"
        f"    {detail}\n\n"
        f"Find the figures as actually filed. Which of the two numbers is "
        f"wrong, and what are the correct ones? Check the RHP or prospectus "
        f"and the NSE/BSE issue page. If the two published figures genuinely "
        f"disagree, say so and give both."
    )
