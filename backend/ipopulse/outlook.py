"""Reel 7's briefing: the numbers from the exchange, the words from the model.

This is the module that assembles a `Briefing`. It is deliberately the only
place where market data and a language model meet, and the boundary between
them is the whole design:

    providers/market.py    every price, level, percentage and count
    providers/news.py      every headline, its timestamp and its source
    this module            arithmetic on the above -> setup candidates
    the model              which candidates matter, and the prose

**The model never returns a number that reaches the sheet.** Not "is asked
not to" — cannot. `_apply` re-attaches the arithmetic by symbol after the
response comes back and throws the model's numbers away unread. If the model
hallucinates an entry of ₹9,999 the field is simply overwritten with the
computed one, because the response is only consulted for `rank`, `reason`,
`invalidates` and the news picks.

That is stricter than the house rule needed to be. `no-gemini-invented-numbers`
was written after ten fabricated ₹0 GMP days reached the sheet, where the cost
was a wrong premium on a card. Here the cost is a viewer taking a price. So
the rule is enforced structurally instead of by prompt discipline, which is
the only version of it that survives a model update.

── how a setup is built ───────────────────────────────────────────────────

Classic floor pivots, computed in `market.levels` from the session's own high,
low and close. Nothing fitted, nothing tuned, no parameters — which matters
because a fitted indicator can be quietly adjusted until its past calls look
good, and this one cannot.

    long   entry the pivot, target R1, stop S1
    short  entry the pivot, target S1, stop R1

**The first version of this was arithmetically self-defeating and it is worth
recording why.** It entered a long at R1, targeting R2 with the pivot as the
stop. Substituting the pivot formulas, that structure's reward-to-risk is
exactly `(H - P) / (P - L)` — and because a strong close pulls the pivot up,
`H - P` shrinks and `P - L` grows precisely when a stock closed near its high.
So the better the candidate looked, the worse its arithmetic, and the
reward/risk floor below rejected every one of the twenty candidates on the
first live run. Zero setups, in the half of the reel the whole thing was for.

Entering at the pivot inverts it to `(P - L) / (H - P)` for a long, which
clears 1.0 exactly when the close was strong — the same condition
`LONG_CLOSE_POS` already selects on. Filter and arithmetic now agree instead
of cancelling out. It is also the more conservative trade of the two: a
pullback to a published level rather than a chase through it.

Each setup therefore carries its own invalidation, and it is on the card. A
setup whose reward does not clear its risk is dropped here, before the model
ever sees it — arithmetic the reasoning cannot argue with.

── the model ──────────────────────────────────────────────────────────────

`BRIEFING_MODEL` pins the strongest model the key can reach, because the user
asked for accuracy over cost on this reel specifically and a late pipeline is
acceptable. Everything else in the project keeps the cheap model: `Gemini`
tries `self.model` first and walks its list, so pinning here changes nothing
anywhere else.

Uncached, on purpose. `_generate_json` does not touch the response cache, and
that is correct for this one caller — a briefing is about one morning, and a
cached answer would be yesterday's market described as today's. It is the
exact failure this reel exists to avoid.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

from .ai import AiUnavailable, Gemini
from .models import Briefing
from .providers import market, news

# The strongest model reachable on this key (checked 2026-09-02). Overridable
# by env for the same reason `GEMINI_MODEL` is: a deliberate choice should
# never be second-guessed by a constant in a file.
BRIEFING_MODEL = os.getenv("IPOPULSE_BRIEFING_MODEL") or "gemini-3.1-pro-preview"

# How many setups of each side reach the reel. Five and five, per the spec.
PER_SIDE = 5
# Candidates handed to the model per side. More than PER_SIDE so it has
# something to choose between, capped so the prompt stays about this morning.
CANDIDATES_PER_SIDE = 10
# A setup must promise more than it risks. Applied before the model sees the
# candidate, so no amount of good reasoning can rescue bad arithmetic.
MIN_RR = 1.0
# Where a stock closed in its own range, 0 = on the low, 1 = on the high.
# A long needs the buyers to have had the last word and a short the sellers;
# a stock that rose 3% but closed mid-range spent the afternoon giving it back
# and is a worse morning idea than the percentage suggests.
LONG_CLOSE_POS = 0.5
SHORT_CLOSE_POS = 0.5


def candidates(snap: dict[str, Any]) -> list[dict[str, Any]]:
    """Setup candidates, fully priced, from the day's movers.

    Every number on the returned dicts is arithmetic on exchange data. The
    model adds words to these; it does not produce them.
    """
    out: list[dict[str, Any]] = []

    def build(row: dict[str, Any], side: str) -> dict[str, Any] | None:
        lv = row.get("levels") or {}
        if not lv:
            return None                     # no range, no levels, no setup
        pos = lv.get("close_pos", 0)
        if side == "long":
            if pos < LONG_CLOSE_POS:
                return None
            entry, target, stop = lv["pivot"], lv["r1"], lv["s1"]
        else:
            if pos > SHORT_CLOSE_POS:
                return None
            entry, target, stop = lv["pivot"], lv["s1"], lv["r1"]
        if not (entry and target and stop):
            return None
        # Signed by side, so a long's target above entry and a short's below
        # both read as positive reward.
        reward = (target - entry) / entry * (1 if side == "long" else -1)
        risk = abs(entry - stop) / entry
        if risk <= 0 or reward <= 0 or (reward / risk) < MIN_RR:
            return None
        return {
            "side": side, "symbol": row["symbol"], "last": row["last"],
            "pct": row["pct"], "entry": round(entry, 2),
            "target": round(target, 2), "stop": round(stop, 2),
            "pivot": lv["pivot"], "r1": lv["r1"], "s1": lv["s1"],
            "close_pos": pos, "range_pct": lv.get("range_pct", 0),
            "reward_pct": round(100 * reward, 2),
            "risk_pct": round(100 * risk, 2),
            "rr": round(reward / risk, 2),
            "volume": row.get("volume", 0),
        }

    movers = snap.get("movers") or {}
    for row in (movers.get("gainers") or [])[:CANDIDATES_PER_SIDE]:
        got = build(row, "long")
        if got:
            out.append(got)
    for row in (movers.get("losers") or [])[:CANDIDATES_PER_SIDE]:
        got = build(row, "short")
        if got:
            out.append(got)
    return out


def _bias(snap: dict[str, Any]) -> str:
    """up | down | flat, from breadth and the index, not from the model.

    Breadth first and the index second: an index can fall on two heavyweights
    while most of the market rises, and the direction a viewer experiences is
    the one most of their screen is doing.
    """
    b = snap.get("breadth") or {}
    adv, dec = b.get("advances", 0), b.get("declines", 0)
    pct = ((snap.get("headline") or {}).get("NIFTY 50") or {}).get("pct", 0)
    if adv and dec:
        ratio = adv / dec
        if ratio > 1.3 and pct > -0.2:
            return "up"
        if ratio < 0.77 and pct < 0.2:
            return "down"
    if pct > 0.4:
        return "up"
    if pct < -0.4:
        return "down"
    return "flat"


PROMPT = """You are writing the script data for a pre-market briefing aimed \
at Indian intraday traders, to be published as a vertical short video before \
the 09:15 IST open on {day}.

Write in the voice of a desk analyst with twenty years on Indian equities: \
plain, specific, unexcited. No hype, no emoji, no exclamation marks. Never \
promise an outcome. Never address the viewer's own money or position size.

THE MARKET, as published by NSE (these numbers are facts; do not restate any \
of them incorrectly and do not invent others):
{market}

SECTORAL INDICES for the session, strongest first:
{sectors}

NEWS that broke between {news_from} and {news_to} IST. Each has an id, the \
time it broke, how many outlets carried it, and any sector our keyword map \
guessed (the guess may be wrong — judge from the text):
{news}

INTRADAY SETUP CANDIDATES. Every price here is already computed from the \
session's own high, low and close using classic floor pivots. A long is a \
pullback to the pivot targeting R1, invalidated at S1; a short is a bounce to \
the pivot targeting S1, invalidated at R1. rr is reward divided by risk. \
"closed at X of its range" is 1.0 for a close on the day's high, 0.0 for a \
close on its low.
{setups}

Return ONLY a JSON object with exactly these keys:

{{
  "outlook": "2-3 sentences on how the session is set up, naming the actual \
index level and what the overnight news implies for the open. State what \
would change the read.",
  "levels_note": "One sentence, first person, making clear these are \
published pivot levels computed from the previous session's range and that \
they are observations about a range rather than instructions to trade.",
  "news": [
    {{"id": <id from the list>, "headline": "the story in <=9 words, \
rewritten in your own words - do not copy the outlet's headline verbatim", \
"body": "one sentence of what happened", "why": "one sentence on what it \
means for an Indian trader today", "sector": "the NIFTY sector index it \
moves, or empty", "tickers": ["UPTO 3 NSE symbols it moves, or empty"]}}
  ],
  "sectors": [
    {{"sector": "<exact name from the sectoral list>", "stance": "strong|weak|flat", \
"note": "at most 12 words on why"}}
  ],
  "setups": [
    {{"symbol": "<exact symbol from the candidate list>", "rank": 1, \
"reason": "one sentence: why this level is worth watching today, referring to \
the session's own action and any news above. Be specific about the stock, not \
generic.", "invalidates": "one short clause naming the condition that voids \
the idea"}}
  ]
}}

Rules:
- Choose exactly 5 news items, the 5 that most affect Indian equities today. \
Rank them by that, not by how many outlets carried them.
- Cover every sector in the sectoral list, in the order given.
- Choose up to {per_side} long and up to {per_side} short setups from the \
candidates, ranked 1..n within each side. Use ONLY symbols from the list. If \
fewer than {per_side} on a side are genuinely worth naming, return fewer - a \
short list is better than a padded one.
- Do NOT include any price, level, target or stop in your output. Those are \
already computed and will be attached to your reasoning. Refer to levels by \
name if you need to ("above R1", "back below the pivot").
- Every "reason" must say something true of that specific stock today.
"""


def _prompt(day: str, snap: dict, cands: list[dict], stories: dict) -> str:
    head = snap.get("headline") or {}
    nifty = head.get("NIFTY 50") or {}
    bank = head.get("NIFTY BANK") or {}
    b = snap.get("breadth") or {}
    market_txt = "\n".join([
        f"  NIFTY 50: {nifty.get('last')} ({nifty.get('pct'):+}%), "
        f"previous close {nifty.get('prev_close')}, "
        f"day {nifty.get('low')}-{nifty.get('high')}, "
        f"52-week {nifty.get('year_low')}-{nifty.get('year_high')}",
        f"  NIFTY BANK: {bank.get('last')} ({bank.get('pct'):+}%)",
        f"  Breadth: {b.get('advances')} advancing, {b.get('declines')} "
        f"declining, {b.get('unchanged')} unchanged",
        f"  Computed bias from breadth and the index: {_bias(snap)}",
        f"  Exchange timestamp on this data: {snap.get('at')}",
    ])
    sectors_txt = "\n".join(
        f"  {r['name']}: {r['pct']:+}% (at {r['last']})"
        for r in (snap.get("sectors") or {}).get("all") or [])
    news_txt = "\n".join(
        f"  [{i}] {s['at'][11:16]} ({s['outlet_count']} outlet"
        f"{'s' if s['outlet_count'] != 1 else ''})"
        f"{' ' + ','.join(s['sectors']) if s['sectors'] else ''}: "
        f"{s['headline']} — {s['body'][:180]}"
        for i, s in enumerate(stories.get("items") or []))
    setups_txt = "\n".join(
        f"  {c['side']:5} {c['symbol']:12} last {c['last']} ({c['pct']:+}%), "
        f"closed at {c['close_pos']} of its range, day range "
        f"{c['range_pct']}%, entry {c['entry']} target {c['target']} "
        f"stop {c['stop']}, reward {c['reward_pct']}% risk {c['risk_pct']}% "
        f"rr {c['rr']}"
        for c in cands)
    return PROMPT.format(
        day=day, market=market_txt, sectors=sectors_txt or "  (none reported)",
        news=news_txt or "  (no stories in the window)",
        setups=setups_txt or "  (no candidate cleared the reward/risk floor)",
        news_from=stories.get("from", ""), news_to=stories.get("to", ""),
        per_side=PER_SIDE)


def _apply(day: str, snap: dict, cands: list[dict], stories: dict,
           said: dict, model: str) -> Briefing:
    """Fold the model's words onto our numbers.

    Every numeric field is taken from `snap` or `cands`, keyed by symbol. The
    response is read for `rank`, `reason`, `invalidates` and the news picks
    and for nothing else — see this module's header.
    """
    head = snap.get("headline") or {}
    nifty = head.get("NIFTY 50") or {}
    bank = head.get("NIFTY BANK") or {}
    b = snap.get("breadth") or {}

    # ── news: the model picks by id, we keep the source's own facts
    items = stories.get("items") or []
    picked: list[dict] = []
    for n, choice in enumerate(said.get("news") or [], 1):
        try:
            src = items[int(choice.get("id"))]
        except (TypeError, ValueError, IndexError):
            continue                      # a fabricated id is simply dropped
        picked.append({
            "idx": n,
            "headline": (choice.get("headline") or src["headline"])[:120],
            "body": (choice.get("body") or src["body"])[:300],
            "why": (choice.get("why") or "")[:300],
            "sector": choice.get("sector") or "",
            "tickers": [str(t).upper()[:20]
                        for t in (choice.get("tickers") or [])][:3],
            # The outlet and the link come from the feed, never the model:
            # attribution is a fact about where a story came from.
            "source": ", ".join(src.get("outlets") or [])[:80],
            "url": src.get("url", ""),
            "image": "",                  # filled by the image step
            "at": src.get("at", ""),
        })
        if len(picked) >= 5:
            break

    # ── sectors: our percentages, the model's stance word
    stance = {str(s.get("sector") or ""): (s.get("stance") or "").lower()
              for s in (said.get("sectors") or [])}
    sectors = []
    for row in (snap.get("sectors") or {}).get("all") or []:
        word = stance.get(row["name"], "")
        if word not in ("strong", "weak", "flat"):
            # Fall back to the sign rather than carrying an empty stance. The
            # sign is not a judgement, which is the point: it is what the
            # number already says.
            word = "strong" if row["pct"] > 0.25 else \
                   ("weak" if row["pct"] < -0.25 else "flat")
        sectors.append({"sector": row["name"], "pct": row["pct"],
                        "last": row["last"], "stance": word})

    # ── setups: our levels, the model's ranking and reasoning
    by_symbol = {c["symbol"]: c for c in cands}
    ranked: dict[str, list[dict]] = {"long": [], "short": []}
    for choice in said.get("setups") or []:
        c = by_symbol.get(str(choice.get("symbol") or "").upper())
        if not c:
            continue                      # not a candidate: not a setup
        side = c["side"]
        if len(ranked[side]) >= PER_SIDE:
            continue
        if any(s["symbol"] == c["symbol"] for s in ranked[side]):
            continue                      # named twice; once is enough
        ranked[side].append({
            "side": side, "rank": len(ranked[side]) + 1, "symbol": c["symbol"],
            "last": c["last"], "entry": c["entry"], "target": c["target"],
            "stop": c["stop"], "pivot": c["pivot"], "r1": c["r1"],
            "s1": c["s1"], "pct": c["pct"], "close_pos": c["close_pos"],
            "reason": (choice.get("reason") or "")[:400],
            # A setup with no invalidation gets the arithmetic one. The stop
            # IS the invalidation, so this is a restatement rather than an
            # invention — but a card with the field blank would read as an
            # idea that cannot be wrong.
            "invalidates": (choice.get("invalidates")
                            or f"a move back through {c['stop']}")[:200],
        })

    return Briefing.from_dict({
        "date": day,
        "trading": snap.get("trading", True),
        "why_closed": snap.get("why_closed", ""),
        "at": snap.get("at", ""),
        "nifty": nifty.get("last", 0), "nifty_pct": nifty.get("pct", 0),
        "nifty_prev": nifty.get("prev_close", 0),
        "banknifty": bank.get("last", 0), "banknifty_pct": bank.get("pct", 0),
        "advances": b.get("advances", 0), "declines": b.get("declines", 0),
        "unchanged": b.get("unchanged", 0),
        "bias": _bias(snap),
        "outlook": (said.get("outlook") or "")[:600],
        "levels_note": (said.get("levels_note") or "")[:400],
        "model": model,
        "partial": ", ".join(snap.get("partial") or []),
        "notes": f"news window {stories.get('from', '')} to "
                 f"{stories.get('to', '')}; {stories.get('count', 0)} stories "
                 f"from {len(stories.get('feeds') or [])} feeds",
        "news": picked,
        "sectors": sectors,
        "setups": ranked["long"] + ranked["short"],
    })


def build(day: str | None = None, model: str | None = None,
          gem: Gemini | None = None, verbose: bool = False) -> Briefing:
    """Assemble one morning's briefing. Reads the market, the news and a model.

    Raises `AiUnavailable` when no model answers, and `RuntimeError` when the
    exchange data is missing — a briefing with no index level is not a thinner
    briefing, it is a different and false one.
    """
    snap = market.snapshot()
    day = day or snap["day"]
    if not ((snap.get("headline") or {}).get("NIFTY 50") or {}).get("last"):
        raise RuntimeError(
            "NSE returned no index level — refusing to build a briefing with "
            "no market in it. Try again, or check /api/allIndices by hand.")

    cands = candidates(snap)
    stories = news.collect(day)
    if verbose:
        print(f"  market   : NIFTY {snap['headline']['NIFTY 50']['last']} "
              f"({snap['headline']['NIFTY 50']['pct']:+}%), bias {_bias(snap)}")
        print(f"  news     : {stories['count']} stories, "
              f"{len(stories['feeds'])} feeds, "
              f"{stories['from'][11:16]}->{stories['to'][11:16]} IST")
        print(f"  setups   : {sum(1 for c in cands if c['side'] == 'long')} long, "
              f"{sum(1 for c in cands if c['side'] == 'short')} short candidates "
              f"cleared rr >= {MIN_RR}")

    picked_model = model or BRIEFING_MODEL
    gem = gem or Gemini(model=picked_model)
    if not gem.available():
        raise AiUnavailable(
            "No GEMINI_API_KEY — the briefing's numbers are free but its "
            "words are not. Set the key, or write the outlook by hand.")
    if verbose:
        print(f"  model    : {picked_model} (uncached — a briefing is about "
              f"one morning)")

    said = gem._generate_json(_prompt(day, snap, cands, stories))
    if not isinstance(said, dict):
        raise RuntimeError(
            f"the model returned {type(said).__name__}, not an object — "
            f"nothing was written")
    return _apply(day, snap, cands, stories, said, picked_model)


if __name__ == "__main__":
    b = build(verbose=True)
    print(json.dumps(b.to_dict(), indent=1, ensure_ascii=False)[:3000])
