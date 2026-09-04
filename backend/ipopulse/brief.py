"""Everything known about an IPO, as one document a notebook can read.

Gemini Notebook (formerly NotebookLM) has no API. What it has is four ways in
— **Upload files**, **Websites**, **Drive** and **Copied text** — and all four
want the same thing: one self-contained document, in prose, with its facts
stated rather than implied.

That is not what this project stores. The store is normalised across eight
tabs precisely so nothing is duplicated: an IPO's premium lives in a dated
series, its translations in a key/lang/idx grid, its bullets in a `Lists` tab.
Excellent for a pipeline, useless as a source — a notebook handed those tabs
would have to reconstruct the company before it could say anything about it.

So this flattens one IPO (or one trading day) back into readable prose, with
every number labelled and every date spelled out. No new data and nothing
stored: it is a projection of the sheet, regenerated on demand. Deleting the
output loses nothing.

── What it is FOR ─────────────────────────────────────────────────────────

Two things worth having, both of which the pipeline cannot do:

- **Audio Overview in Hindi and Telugu.** Confirmed available in the Studio
  panel. Long-form is what earns watch hours and the reels cannot supply it
  (see `youtube-monetization-constraints`), and the hi/te narration for reels
  is hand-written because there is no generation path — a notebook is a
  different route to the same shortage.
- **Asking questions of the record.** "Which of these issues has the weakest
  interest cover", across thirty briefs at once, is a question no command
  here answers and no spreadsheet formula wants to.

── The one thing to decide before publishing ──────────────────────────────

`--url` writes into `frontend/`, which GitHub Pages serves verbatim — and
`gate.js` is client-side JavaScript inside `index.html`, so **a file dropped
beside it is NOT behind the password.** A brief at a public URL is a brief
anyone can read.

Most of what is in one is public already (NSE, BSE and the desk publish it).
The analysis is not: the flags, the risk sentence and the verdict are this
channel's own opinion, and a public URL publishes them before a video does.
So the default is a local file to drag in, and the URL is opt-in.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from .sheets import BACKEND_ROOT, OUT_DIR

BRIEF_DIR = OUT_DIR / "brief"
# Served verbatim by GitHub Pages, and NOT behind the gate. See the header.
PUBLIC_DIR = BACKEND_ROOT.parent / "frontend" / "brief"


def _money(cr: float) -> str:
    if not cr:
        return "not disclosed"
    return f"Rs {cr:,.2f} crore"


def _when(d: Any) -> str:
    """A date spelled out. '2026-09-08' is ambiguous prose; the month is not."""
    if not d:
        return "not announced"
    if isinstance(d, str):
        try:
            d = date.fromisoformat(d[:10])
        except ValueError:
            return d
    return d.strftime("%d %B %Y")


def _pct(a: float, b: float) -> str:
    return f"{100 * a / b:.1f}%" if b else "n/a"


def for_ipo(ipo: Any, derived: dict[str, Any] | None = None) -> str:
    """One IPO as a readable brief.

    Written as sentences rather than a table dump. A notebook asked "is this
    expensive" can work with "the issue is priced at 100.8 times its latest
    earnings against a peer average of 43.5"; it cannot do much with
    `pe: 100.8, pe_peer_avg: 43.5` beyond reading it back.
    """
    iss, dts, fin, an = ipo.issue, ipo.dates, ipo.financials, ipo.analysis
    L: list[str] = []
    add = L.append

    add(f"# {ipo.company or ipo.slug}")
    add("")
    add(f"An Indian {'SME' if ipo.board == 'SME' else 'mainboard'} initial "
        f"public offering"
        + (f" in the {ipo.sector} sector." if ipo.sector else "."))
    src = ipo.sources or {}
    ident = [f"NSE symbol {src['nse_symbol']}" for _ in [0]
             if src.get("nse_symbol")]
    if src.get("bse_code"):
        ident.append(f"BSE scrip code {src['bse_code']}")
    if src.get("isin"):
        ident.append(f"ISIN {src['isin']}")
    if ident:
        add(f"The exchanges identify it as {', '.join(ident)}.")
    add("")

    # ── the offer
    add("## The offer")
    add("")
    if iss.price_low and iss.price_high:
        add(f"The price band is Rs {iss.price_low:g} to Rs {iss.price_high:g} "
            f"per share.")
    elif iss.price_high:
        add(f"The upper end of the price band is Rs {iss.price_high:g} per "
            f"share; the lower end is not recorded.")
    if iss.lot_size:
        add(f"One lot is {iss.lot_size} shares, so the smallest retail "
            f"application is "
            f"Rs {iss.lot_size * (iss.price_high or 0):,.0f}.")
    add(f"The total issue size is {_money(iss.total_cr)}.")
    if iss.fresh_cr or iss.ofs_cr:
        add(f"Of that, {_money(iss.fresh_cr)} is fresh capital raised by the "
            f"company itself and {_money(iss.ofs_cr)} is an offer for sale by "
            f"existing shareholders"
            + (" — money that does not reach the business."
               if iss.ofs_cr else "."))
    if iss.shares_total:
        add(f"The book is divided as "
            f"{_pct(iss.shares_qib, iss.shares_total)} to qualified "
            f"institutional buyers, "
            f"{_pct(iss.shares_nii, iss.shares_total)} to non-institutional "
            f"investors and {_pct(iss.shares_retail, iss.shares_total)} to "
            f"retail.")
        if iss.shares_anchor:
            add(f"Anchor investors took "
                f"{_pct(iss.shares_anchor, iss.shares_total)} of the issue "
                f"before bidding opened; that portion was committed in "
                f"advance and is a subset of the institutional share.")
    if iss.registrar:
        add(f"The registrar is {iss.registrar}.")
    add("")

    # ── the calendar
    add("## Dates")
    add("")
    add(f"Bidding opens {_when(dts.open)} and closes {_when(dts.close)}"
        + (f" at {dts.close_time}." if dts.close_time else "."))
    add(f"Allotment is expected {_when(dts.allotment)}, refunds "
        f"{_when(dts.refund)}, and listing {_when(dts.listing)}.")
    add("")

    # ── the numbers
    if fin.years and fin.revenue:
        add("## Financial record")
        add("")
        add("Figures in rupees crore, oldest year first.")
        add("")
        rows = [("Year", *fin.years),
                ("Revenue", *[f"{v:g}" for v in fin.revenue]),
                ("EBITDA", *[f"{v:g}" for v in fin.ebitda]),
                ("Profit after tax", *[f"{v:g}" for v in fin.pat]),
                ("Net worth", *[f"{v:g}" for v in fin.net_worth]),
                ("Total debt", *[f"{v:g}" for v in fin.total_debt])]
        for row in rows:
            if len(row) > 1:
                add("| " + " | ".join(str(c) for c in row) + " |")
                if row[0] == "Year":
                    add("|" + "---|" * len(row))
        add("")
        if len(fin.revenue) >= 2 and fin.revenue[0]:
            growth = (fin.revenue[-1] / fin.revenue[0]) ** (
                1 / max(1, len(fin.revenue) - 1)) - 1
            add(f"Revenue compounded at about {100 * growth:.1f}% a year over "
                f"that period.")
        if fin.eps:
            add(f"Post-issue earnings per share is Rs {fin.eps:g}.")
            if iss.price_high:
                add(f"At the top of the band that is "
                    f"{iss.price_high / fin.eps:.1f} times earnings.")
        if fin.pe_peer_avg:
            add(f"The peer group average price-to-earnings ratio is "
                f"{fin.pe_peer_avg:g}.")
        else:
            add("No peer group average is on record, so the multiple above "
                "cannot be compared with the sector.")
        add("")

    # ── the grey market
    if ipo.gmp_history:
        add("## Grey market premium")
        add("")
        add("The grey market is unofficial and unregulated. A premium is what "
            "dealers were quoting, not a price anyone is obliged to honour, "
            "and it can move or vanish before listing.")
        add("")
        for p in ipo.gmp_history[-12:]:
            est = (iss.price_high + p.gmp) if iss.price_high else 0
            add(f"- {_when(p.date)}: Rs {p.gmp:g}"
                + (f", implying a listing near Rs {est:g}" if est else "")
                + (f" (source: {p.source})" if p.source else ""))
        add("")
        last = ipo.gmp_history[-1]
        if iss.price_high and last.gmp:
            add(f"The latest premium of Rs {last.gmp:g} is "
                f"{100 * last.gmp / iss.price_high:.1f}% of the upper band "
                f"price.")
        add("")

    # ── demand
    if ipo.subscription:
        add("## Subscription")
        add("")
        add("How many times each category bid for the shares reserved for it. "
            "Figures are cumulative for the bidding window.")
        add("")
        for s in ipo.subscription:
            add(f"- Day {s.day} ({_when(s.date)}): overall {s.total:g} times "
                f"— QIB {s.qib:g}, NII {s.nii:g}, retail {s.retail:g}")
        add("")

    # ── the opinion, clearly labelled as one
    parts = [("Strengths", an.green_flags), ("Concerns", an.red_flags),
             ("What the company does", an.overview),
             ("Background", an.background)]
    if any(v for _, v in parts) or an.growth or an.valuation or an.risk:
        add("## Assessment")
        add("")
        add("This section is opinion formed from the figures above, not "
            "reported fact. It is the view of the IPO Pulse channel and is "
            "not investment advice.")
        add("")
        for heading, items in parts:
            if items:
                add(f"**{heading}**")
                add("")
                for it in items:
                    add(f"- {it}")
                add("")
        for label, text in (("Growth", an.growth), ("Valuation", an.valuation),
                            ("Main risk", an.risk)):
            if text:
                add(f"**{label}.** {text}")
                add("")
        if an.verdict:
            add(f"**Stated verdict:** {an.verdict}.")
        else:
            add("**No verdict has been recorded for this issue.** The "
                "apply/avoid call is deliberately left empty until someone "
                "makes it, rather than defaulting to one.")
        add("")

    add("---")
    add("")
    add(f"Compiled from the IPO Pulse store on {_when(date.today())}. Issue "
        f"terms and subscription come from NSE and BSE; the grey market "
        f"premium and the restated financials come from InvestorGain. "
        f"Nothing here is a recommendation to buy or sell a security.")
    return "\n".join(L) + "\n"


def for_briefing(b: Any) -> str:
    """One trading day's pre-market briefing as prose."""
    L: list[str] = []
    add = L.append
    add(f"# Indian market briefing, {_when(b.date)}")
    add("")
    if not b.trading:
        add(f"The exchanges were closed: {b.why_closed}.")
        return "\n".join(L) + "\n"
    add(f"The NIFTY 50 stood at {b.nifty:,.2f}, {b.nifty_pct:+.2f}% on the "
        f"session, having closed at {b.nifty_prev:,.2f} previously. The NIFTY "
        f"BANK was at {b.banknifty:,.2f}, {b.banknifty_pct:+.2f}%.")
    add(f"Market breadth was {b.advances} advancing against {b.declines} "
        f"declining, with {b.unchanged} unchanged — so the direction most of "
        f"the market experienced was {b.bias}.")
    add("")
    if b.outlook:
        add("## Outlook")
        add("")
        add(b.outlook)
        add("")
    if b.news:
        add("## Overnight news")
        add("")
        for n in b.news:
            add(f"### {n.idx}. {n.headline}")
            add("")
            if n.body:
                add(n.body)
            if n.why:
                add(f"Why it matters: {n.why}")
            meta = [x for x in (n.sector, ", ".join(n.tickers), n.source) if x]
            if meta:
                add(f"({' · '.join(meta)})")
            add("")
    if b.sectors:
        add("## Sector performance")
        add("")
        for s in b.sectors:
            add(f"- {s.sector}: {s.pct:+.2f}% ({s.stance})")
        add("")
    if b.setups:
        add("## Intraday levels")
        add("")
        add("Every level below is classic floor-pivot arithmetic on the "
            "previous session's own high, low and close. They are published "
            "reference levels, not predictions, and not advice.")
        add("")
        for s in b.setups:
            add(f"- **{s.symbol}** ({s.side}): pivot {s.entry:g}, target "
                f"{s.target:g}, invalidated at {s.stop:g} "
                f"(reward {s.reward:+.2f}%, risk {s.risk:.2f}%, "
                f"ratio {s.rr:g}). {s.reason} Voided if {s.invalidates}")
        add("")
    if b.levels_note:
        add(b.levels_note)
    return "\n".join(L) + "\n"


def write(text: str, name: str, public: bool = False) -> Path:
    """Save a brief. Local by default; `public` puts it on the website."""
    root = PUBLIC_DIR if public else BRIEF_DIR
    root.mkdir(parents=True, exist_ok=True)
    dest = root / name
    dest.write_text(text, encoding="utf-8")
    return dest
