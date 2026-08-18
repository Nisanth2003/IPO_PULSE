"""Does this IPO exist? NSE and BSE answer; nobody else gets a vote.

The split this module enforces:

    NSE + BSE   which IPOs are real          (the exchanges file them)
    InvestorGain   what the GMP is           (no exchange publishes it)

Those are different questions and they had been answered by the same source.
InvestorGain's board is a *view* — issues appear on it when the desk starts
covering them and drop off it the day they list — so its roster moves for
reasons that have nothing to do with whether a company is really raising
money. Trusting it for existence is how the sheet ended up carrying Meridian
Logistics, a ₹720 Cr "mainboard issue, open today" that was on no exchange,
no GMP desk, and no search result. Trusting it the other way is just as
wrong: an issue InvestorGain has not picked up yet is not fake.

So this asks the exchanges, and grades the answer honestly:

    confirmed     on an exchange feed now, or on one before (stamped)
    corroborated  gone from the feeds, but in InvestorGain's all-time
                  catalogue — i.e. it listed, which is why it is gone
    suspect       on no exchange, never stamped, and unknown to InvestorGain
    unchecked     the feeds did not answer, so nothing can be concluded

Two rules keep this from crying wolf:

- **The exchange feeds are current-and-upcoming only.** An issue disappears
  from both the day it lists. "Absent from NSE and BSE" therefore proves
  nothing by itself for a closed issue, and a checker that ignored this would
  flag every IPO the channel has ever covered. Hence the stamp: the first
  time an exchange confirms an issue, that fact is written to the sheet
  (`sources.exchange`) and never has to be re-derived.
- **An unreachable feed is not an absence.** NSE blocks a cold request with
  an HTML page and BSE has its own bad mornings. If neither exchange answers,
  every verdict is `unchecked` — silence is not evidence.

The stamp rides in the `sources` map rather than a new column, the same trick
`gmp-sync` uses for `logo`: it is already a free-form role -> value map with
its own tab, so this needs no schema change and no matching edit in data.js.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from .models import Ipo

# Corporate furniture that carries no identity. "Lalithaa Jewellery Mart
# Limited" and "Lalithaa Jewellery Mart" are the same company; matching on the
# raw strings would say otherwise.
_NOISE = {
    "limited", "ltd", "private", "pvt", "public", "company", "co",
    "india", "indian", "the", "and", "of",
}


def _tokens(name: str) -> set[str]:
    """Company name -> the words that actually identify it."""
    words = re.findall(r"[a-z0-9]+", (name or "").lower())
    return {w for w in words if w not in _NOISE and len(w) > 1}


def _same_company(a: str, b: str) -> bool:
    """Do these two names denote one company?

    Deliberately strict. A loose match here does the opposite of what this
    module is for: it would let a fabricated row borrow a real issue's
    confirmation. Requires that the shorter name's identifying words are
    entirely contained in the longer one's, which accepts
    "SUNSHINE PICTURES LIMITED" vs "Sunshine Pictures Limited" and rejects
    "Meridian Logistics" against every logistics company on the board.
    """
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    small, large = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return small <= large


def exchange_roster() -> tuple[list[dict[str, Any]], list[str]]:
    """Every issue NSE and BSE currently carry, and which of them answered.

    Returns (rows, reachable) where `reachable` names the feeds that replied.
    An empty `reachable` is the caller's signal that nothing can be judged.
    """
    rows: list[dict[str, Any]] = []
    reachable: list[str] = []

    try:
        from .providers.scrape import NseProvider
        nse = NseProvider().fetch_catalogue()
        if nse:
            reachable.append("NSE")
            for r in nse:
                rows.append({
                    "exchange": "NSE",
                    "name": r.get("company") or "",
                    "ref": r.get("symbol") or r.get("slug") or "",
                })
    except Exception:
        pass

    try:
        from .providers import bse
        for r in bse.board():
            if "BSE" not in reachable:
                reachable.append("BSE")
            rows.append({
                "exchange": "BSE",
                "name": r.get("long_name") or r.get("name") or "",
                "ref": str(r.get("id") or ""),
            })
    except Exception:
        pass

    return rows, reachable


def _in_catalogue(ipo: Ipo) -> bool:
    """Has InvestorGain ever heard of this company?

    Only consulted for an issue the exchanges no longer carry, where it
    separates "listed, so it dropped off" from "never existed". Never used to
    *confirm* an issue on its own — that is the exchanges' job.
    """
    try:
        from .providers import investorgain as ig
        return ig.resolve(ipo.slug, ipo.company or "") is not None
    except Exception:
        return False


def check(ipos: list[Ipo], today: date | None = None) -> list[dict[str, Any]]:
    """Grade every IPO's existence. One dict per IPO, order preserved."""
    today = today or date.today()
    roster, reachable = exchange_roster()

    out: list[dict[str, Any]] = []
    for ipo in ipos:
        stamped = (ipo.sources or {}).get("exchange") or ""
        hit = next((r for r in roster
                    if _same_company(r["name"], ipo.company or ipo.slug)), None)

        # Is the issue one the feeds *must* be carrying right now?
        #
        # Only if it is taking bids today. The exchange feeds are narrower
        # than "not yet closed": NSE's `all-upcoming-issues` carried 7 rows on
        # 18 Aug while InvestorGain's board had 22, because an exchange files
        # an issue days before it opens, not weeks. Grading on "has not closed
        # yet" therefore called Augmont, Skyways and Tempsens fabrications —
        # three real issues whose windows simply had not started.
        #
        # An issue that is open, though, is unarguable: money is being
        # collected through the exchange, so the exchange has it. That is
        # exactly where Meridian ("open today") separates from Dhoot
        # ("listed last week") and from Augmont ("opens on the 21st").
        opens, close = ipo.dates.open, ipo.dates.close
        taking_bids = bool(opens and opens <= today and (close is None or close >= today))

        if hit:
            verdict, why = "confirmed", f"on {hit['exchange']} now"
            stamp = f"{hit['exchange']}:{hit['ref']}"
        elif stamped:
            verdict, why, stamp = "confirmed", f"confirmed earlier ({stamped})", stamped
        elif not reachable:
            verdict, why, stamp = "unchecked", "no exchange feed answered", ""
        elif taking_bids:
            # The loud case. Both exchanges answered, the issue says it is
            # collecting money today, and neither of them has it.
            verdict, stamp = "suspect", ""
            why = (f"claims to be taking bids today (open {opens} -> "
                   f"{close or 'n/a'}) but is on neither "
                   f"{' nor '.join(reachable)}")
            if not _in_catalogue(ipo):
                why += "; InvestorGain has never listed it either"
        elif _in_catalogue(ipo):
            # Not open, so the exchange feeds are not expected to carry it —
            # either it has listed and dropped off, or it opens later this
            # month. InvestorGain knowing the company is enough here; the
            # exchanges get the final word once bidding starts.
            when = "listed" if (close and close < today) else "not open yet"
            verdict, why, stamp = ("corroborated",
                                   f"off the exchange feeds ({when}), but in "
                                   f"InvestorGain's catalogue", "")
        else:
            verdict, stamp = "suspect", ""
            why = ("on no exchange feed and unknown to InvestorGain — "
                   "nothing anywhere records it")

        out.append({"slug": ipo.slug, "company": ipo.company,
                    "verdict": verdict, "why": why, "stamp": stamp,
                    "reachable": reachable})
    return out
