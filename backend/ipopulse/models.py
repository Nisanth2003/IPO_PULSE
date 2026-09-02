"""Canonical IPO data model.

Everything the frontend renders traces back to one of these structures. The
shapes are deliberately provider-agnostic: a manual YAML file and a future
NSE/GMP API adapter both normalise into `Ipo`, so the visualiser never learns
where a number came from.

Numbers live here. Prose lives here. Derived values do NOT — see compute.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date
from typing import Any


# ── helpers ────────────────────────────────────────────────────────────────

def _d(value: Any) -> date | None:
    """Accept a date, an ISO string, or nothing."""
    if value in (None, "", "null"):
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value).strip()[:10])


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [v for v in value if v not in (None, "")]
    # a YAML block string: one item per line
    return [line.strip() for line in str(value).splitlines() if line.strip()]


# ── pieces ─────────────────────────────────────────────────────────────────

@dataclass
class Issue:
    fresh_cr: float = 0.0            # fresh issue size, rupees crore
    ofs_cr: float = 0.0              # offer for sale, rupees crore
    # Total issue size when the fresh/OFS split is not known. NSE publishes
    # the share count and the price band, which multiply out to the total but
    # say nothing about how it splits — and inventing a split would misstate
    # how much money reaches the company rather than its selling shareholders.
    # compute() falls back to this so the headline size is right while the
    # split scene stays honestly blank.
    total_cr: float = 0.0
    price_low: float = 0.0
    price_high: float = 0.0
    lot_size: int = 0
    shares_post_issue_cr: float = 0.0   # for market cap / P-E; optional
    # The minimum application, in SHARES, for the two HNI tranches. Retail's
    # minimum is one lot and needs no field; sHNI and bHNI have their own
    # floors (₹2 lakh and ₹10 lakh worth, rounded to whole lots) and nothing
    # else in the record implies them.
    #
    # Needed because "1 in 281" is only half an answer for an HNI: the other
    # half is what the ticket costs. 14 lots at ₹300 is ₹2.1 lakh locked up
    # for a week, and a viewer deciding between tranches needs both numbers.
    min_shni_qty: float = 0.0
    min_bhni_qty: float = 0.0

    # ── how the issue is carved up, in SHARES ──────────────────────────
    #
    # InvestorGain's "IPO Reservation" block. Stored as share counts and never
    # as percentages: the counts are what the desk publishes, and percentages
    # are one division away in compute — storing both would be two versions of
    # the same fact that can disagree after a hand edit.
    #
    # Not to be confused with the Subscription tab's qib/nii/retail, which are
    # a different quantity entirely. These say how big each slice IS; those say
    # how many times each slice was BID for. An issue can reserve 35% for
    # retail and have retail bid it 40x.
    #
    # `shares_total` is the denominator and the reason this is safe: with it
    # absent, no percentage is computed at all rather than normalising against
    # whichever slices happen to be known — which would show a 50% QIB slice as
    # 100% on a record that is only missing its retail row.
    # `shares_qib` is QIB **including the anchor book**, which is the number
    # that means "half the issue is set aside for institutions". InvestorGain's
    # plain `shares_offered_qib` excludes it and made a standard mainboard issue
    # read as a 70% book with 30% unexplained — see providers/investorgain.py.
    shares_qib: float = 0.0
    shares_nii: float = 0.0
    shares_retail: float = 0.0
    shares_employee: float = 0.0
    # A shareholder quota, for issues with a listed parent. Usually 0.
    shares_shareholders: float = 0.0
    shares_total: float = 0.0
    # A SUBSET of shares_qib, never added to it. Kept because "thirty percent
    # went to anchor investors before bidding opened, and it is locked in" is
    # worth saying and cannot be derived from anything else here.
    shares_anchor: float = 0.0

    registrar: str = ""
    registrar_url: str = ""
    exchanges: list[str] = field(default_factory=lambda: ["BSE", "NSE"])

    @classmethod
    def from_dict(cls, d: dict) -> "Issue":
        d = d or {}
        return cls(
            fresh_cr=_f(d.get("fresh_cr")),
            ofs_cr=_f(d.get("ofs_cr")),
            total_cr=_f(d.get("total_cr")),
            price_low=_f(d.get("price_low")),
            price_high=_f(d.get("price_high")),
            lot_size=int(_f(d.get("lot_size"))),
            shares_post_issue_cr=_f(d.get("shares_post_issue_cr")),
            min_shni_qty=_f(d.get("min_shni_qty")),
            min_bhni_qty=_f(d.get("min_bhni_qty")),
            shares_qib=_f(d.get("shares_qib")),
            shares_nii=_f(d.get("shares_nii")),
            shares_retail=_f(d.get("shares_retail")),
            shares_employee=_f(d.get("shares_employee")),
            shares_shareholders=_f(d.get("shares_shareholders")),
            shares_total=_f(d.get("shares_total")),
            shares_anchor=_f(d.get("shares_anchor")),
            registrar=d.get("registrar", "") or "",
            registrar_url=d.get("registrar_url", "") or "",
            exchanges=_list(d.get("exchanges")) or ["BSE", "NSE"],
        )


@dataclass
class Dates:
    """`announced` matters: GMP tracking runs announcement -> listing."""
    announced: date | None = None
    open: date | None = None
    close: date | None = None
    close_time: str = "17:00"
    allotment: date | None = None
    refund: date | None = None
    listing: date | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "Dates":
        d = d or {}
        return cls(
            announced=_d(d.get("announced")),
            open=_d(d.get("open")),
            close=_d(d.get("close")),
            close_time=str(d.get("close_time") or "17:00"),
            allotment=_d(d.get("allotment")),
            refund=_d(d.get("refund")),
            listing=_d(d.get("listing")),
        )


@dataclass
class Financials:
    """Parallel arrays, one entry per year in `years`. Rupees crore.

    EBITDA and the margins derived from it were the big gap in v1 — the old
    'valuation' field was a free-text string with no numbers behind it.
    """
    years: list[str] = field(default_factory=list)      # ["FY23","FY24","FY25"]
    revenue: list[float] = field(default_factory=list)
    ebitda: list[float] = field(default_factory=list)
    pat: list[float] = field(default_factory=list)
    net_worth: list[float] = field(default_factory=list)
    total_debt: list[float] = field(default_factory=list)
    eps: float = 0.0                 # post-issue EPS, latest year
    pe_peer_avg: float = 0.0         # peer group average P/E for comparison

    @classmethod
    def from_dict(cls, d: dict) -> "Financials":
        d = d or {}
        nums = lambda k: [_f(v) for v in _list(d.get(k))]
        return cls(
            years=[str(y) for y in _list(d.get("years"))],
            revenue=nums("revenue"),
            ebitda=nums("ebitda"),
            pat=nums("pat"),
            net_worth=nums("net_worth"),
            total_debt=nums("total_debt"),
            eps=_f(d.get("eps")),
            pe_peer_avg=_f(d.get("pe_peer_avg")),
        )


@dataclass
class GmpPoint:
    """One day on the grey-market trail."""
    date: date | None = None
    gmp: float = 0.0
    kostak: float = 0.0
    sauda: float = 0.0
    source: str = "manual"

    @classmethod
    def from_dict(cls, d: dict) -> "GmpPoint":
        return cls(
            date=_d(d.get("date")),
            gmp=_f(d.get("gmp")),
            kostak=_f(d.get("kostak")),
            sauda=_f(d.get("sauda")),
            source=d.get("source", "manual") or "manual",
        )


@dataclass
class SubDay:
    day: int = 1
    date: date | None = None
    qib: float = 0.0
    nii: float = 0.0
    retail: float = 0.0
    employee: float = 0.0
    total: float = 0.0
    # NII, split at the ₹10 lakh line SEBI drew in 2021: sHNI is the ₹2-10 lakh
    # third of the NII book, bHNI the ₹10 lakh-plus two thirds. Kept as their
    # own fields rather than derived from `nii`, because they are separately
    # published and routinely diverge by 4x — Tempsens closed day 1 at 20.75x
    # sHNI against 8.39x bHNI, and a viewer told "NII 12.51x" learns neither.
    #
    # They matter beyond colour: both tranches allot the minimum application by
    # draw, so each multiple IS that tranche's odds. One NII number cannot give
    # an HNI their own answer.
    nii_small: float = 0.0
    nii_big: float = 0.0

    @classmethod
    def from_dict(cls, d: dict) -> "SubDay":
        return cls(
            day=int(_f(d.get("day"), 1)),
            date=_d(d.get("date")),
            qib=_f(d.get("qib")),
            nii=_f(d.get("nii")),
            retail=_f(d.get("retail")),
            employee=_f(d.get("employee")),
            total=_f(d.get("total")),
            nii_small=_f(d.get("nii_small")),
            nii_big=_f(d.get("nii_big")),
        )


@dataclass
class Analysis:
    """Editorial judgement. Written by you, optionally drafted by Gemini.

    Always stored in the source language (English); translations live in
    `Ipo.i18n` so the original is never overwritten.
    """
    overview: list[str] = field(default_factory=list)     # ai.OVERVIEW_BULLETS
    green_flags: list[str] = field(default_factory=list)
    red_flags: list[str] = field(default_factory=list)
    # "Founded: 1985", "HQ: Chennai, Tamil Nadu", "Promoters: ..." — the
    # company-profile facts reel 1 shows under the bullets. Stored "Label: value"
    # so the scene can style the label separately without a second field, and so
    # a human reading the Lists tab sees what each row means.
    #
    # Deliberately NOT model-written: every one is copied verbatim from
    # InvestorGain's filing data, so there is nothing here to hallucinate. Kept
    # out of `overview` because those are prose captions that get translated,
    # and a promoter's name must survive a Hindi cut unchanged.
    about_facts: list[str] = field(default_factory=list)
    # General awareness: how established the company is, what it is known for,
    # who it competes with. Model-written from its own knowledge, because no
    # filing carries it — so unlike `about_facts` this one IS reviewable copy,
    # and it is translated like the rest of the prose.
    #
    # Undated on purpose. See ai.research_background: dated "recent news" came
    # back ungrounded and stale, and a date is what turns old knowledge into a
    # false claim. Background with no date is just background.
    #
    # Written once when the IPO is discovered and never refreshed — none of it
    # moves over a three-week issue.
    background: list[str] = field(default_factory=list)
    growth: str = ""
    valuation: str = ""
    risk: str = ""
    growth_tone: str = "good"        # good | warn | bad
    valuation_tone: str = "warn"
    score: float = 0.0               # 0-10; a manual override, 0 = derive it
    # ── these four default to EMPTY, and that is the whole point ──────────
    #
    # They used to default to `apply` / `apply` / `watch` / `watch`. Nothing
    # in the pipeline ever writes them — `analyse` does not ask the model for
    # a verdict and no other command sets one — so every row on the sheet
    # carried that triple, and reel 5, whose entire job is the verdict, spoke
    # it aloud in three languages for 28 issues including two that are
    # loss-making. A default that nobody chose was being published as a
    # recommendation to buy a security.
    #
    # This is the sheet's own rule about numbers ("a blank cell means absent,
    # never 0 — a written 0 invents a fact") applied to a string, at the one
    # door that was not enforcing it. Empty means *no call has been made*,
    # which is a state the pipeline needs to be able to represent.
    #
    # Nothing else has to change to make that safe: `readiness` already
    # requires a non-empty verdict and all three recommendations before it
    # will call reel 5 recordable. That gate has been correct all along and
    # simply never fired, because the default made it impossible to fail.
    verdict: str = ""                # apply|both|longterm|risky|avoid
    verdict_text: str = ""           # optional override
    reco_retail: str = ""            # apply|watch|avoid
    reco_hni: str = ""
    reco_long: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Analysis":
        d = d or {}
        return cls(
            overview=_list(d.get("overview")),
            green_flags=_list(d.get("green_flags")),
            red_flags=_list(d.get("red_flags")),
            about_facts=_list(d.get("about_facts")),
            background=_list(d.get("background")),
            growth=d.get("growth", "") or "",
            valuation=d.get("valuation", "") or "",
            risk=d.get("risk", "") or "",
            growth_tone=d.get("growth_tone", "good") or "good",
            valuation_tone=d.get("valuation_tone", "warn") or "warn",
            score=_f(d.get("score")),
            verdict=d.get("verdict", "") or "",
            verdict_text=d.get("verdict_text", "") or "",
            reco_retail=d.get("reco_retail", "") or "",
            reco_hni=d.get("reco_hni", "") or "",
            reco_long=d.get("reco_long", "") or "",
        )


@dataclass
class Allotment:
    status: str = "expected"         # expected | out
    listing_low: float = 0.0
    listing_high: float = 0.0
    steps: list[str] = field(default_factory=list)   # blank -> translated defaults

    @classmethod
    def from_dict(cls, d: dict) -> "Allotment":
        d = d or {}
        return cls(
            status=d.get("status", "expected") or "expected",
            listing_low=_f(d.get("listing_low")),
            listing_high=_f(d.get("listing_high")),
            steps=_list(d.get("steps")),
        )


# ── the record ─────────────────────────────────────────────────────────────

@dataclass
class Ipo:
    slug: str
    company: str = ""
    initials: str = ""               # blank -> derived from company
    board: str = "Mainboard"         # Mainboard | SME
    sector: str = ""
    issue: Issue = field(default_factory=Issue)
    dates: Dates = field(default_factory=Dates)
    financials: Financials = field(default_factory=Financials)
    gmp_history: list[GmpPoint] = field(default_factory=list)
    subscription: list[SubDay] = field(default_factory=list)
    analysis: Analysis = field(default_factory=Analysis)
    allotment: Allotment = field(default_factory=Allotment)
    # {"hi": {"overview": [...], "green_flags": [...]}, "te": {...}}
    i18n: dict[str, dict] = field(default_factory=dict)
    # Override the default "what counts as good" lines, e.g. {"ronw": 12}.
    # Useful for banks/utilities where the generic thresholds don't apply.
    benchmarks: dict[str, float] = field(default_factory=dict)
    # Exact pages to read for this IPO, by role:
    #   {"gmp": "https://investorgain.com/...", "subscription": "https://groww.in/ipo/..."}
    # Pinning a URL beats letting the model search — it removes the two ways
    # grounded lookup goes wrong: the wrong company, and a stale cached page.
    sources: dict[str, str] = field(default_factory=dict)
    notes: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Ipo":
        gmp = sorted(
            (GmpPoint.from_dict(x) for x in (d.get("gmp_history") or [])),
            key=lambda p: p.date or date.min,
        )
        subs = sorted(
            (SubDay.from_dict(x) for x in (d.get("subscription") or [])),
            key=lambda s: s.day,
        )
        return cls(
            slug=d["slug"],
            company=d.get("company", "") or "",
            initials=d.get("initials", "") or "",
            board=d.get("board", "Mainboard") or "Mainboard",
            sector=d.get("sector", "") or "",
            issue=Issue.from_dict(d.get("issue")),
            dates=Dates.from_dict(d.get("dates")),
            financials=Financials.from_dict(d.get("financials")),
            gmp_history=gmp,
            subscription=subs,
            analysis=Analysis.from_dict(d.get("analysis")),
            allotment=Allotment.from_dict(d.get("allotment")),
            i18n=d.get("i18n") or {},
            benchmarks={k: _f(v) for k, v in (d.get("benchmarks") or {}).items()},
            sources={str(k): str(v) for k, v in (d.get("sources") or {}).items() if v},
            notes=d.get("notes", "") or "",
        )

    def to_dict(self) -> dict:
        """JSON-ready: dates become ISO strings."""
        def conv(o):
            if isinstance(o, dict):
                return {k: conv(v) for k, v in o.items()}
            if isinstance(o, (list, tuple)):
                return [conv(v) for v in o]
            if isinstance(o, date):
                return o.isoformat()
            return o
        return conv(asdict(self))

    # ── small conveniences ────────────────────────────────────────────────
    @property
    def display_initials(self) -> str:
        if self.initials:
            return self.initials.upper()[:3]
        skip = {"ltd", "limited", "pvt", "private", "and", "the", "india", "&"}
        words = [w for w in self.company.split() if w.lower().strip(".,") not in skip]
        return ("".join(w[0] for w in words[:2]) or "IP").upper()

    @property
    def latest_gmp(self) -> GmpPoint | None:
        return self.gmp_history[-1] if self.gmp_history else None

    @property
    def prev_gmp(self) -> GmpPoint | None:
        return self.gmp_history[-2] if len(self.gmp_history) > 1 else None

    @property
    def latest_sub(self) -> SubDay | None:
        return self.subscription[-1] if self.subscription else None


# ── the daily market briefing ──────────────────────────────────────────────
#
# Reel 7's record. Keyed by date, not by slug, and deliberately not part of
# the `Ipo` tree — see `tables.MARKET_TABS` for why a pseudo-slug was rejected.
#
# Every numeric field here that a viewer might act on (`entry`, `target`,
# `stop`, and the pivot levels behind them) is computed by
# `providers/market.levels` from exchange data. The model writes `reason` and
# `invalidates` — the words — and chooses which candidates are interesting.
# It does not write a price. See `providers/market.py`'s header for why that
# line is drawn exactly there.

@dataclass
class NewsItem:
    """One overnight story: a picture, a headline, and why it matters."""

    idx: int = 0
    headline: str = ""
    body: str = ""
    why: str = ""                    # the market read, not the news itself
    sector: str = ""
    tickers: list[str] = field(default_factory=list)
    source: str = ""
    url: str = ""
    # A generated illustration, never the publisher's own photo. An RSS image
    # is somebody's copyright and this channel is monetised.
    image: str = ""
    at: str = ""                     # when the story broke, ISO, IST

    @classmethod
    def from_dict(cls, d: dict) -> "NewsItem":
        d = d or {}
        return cls(
            idx=int(_f(d.get("idx"))),
            headline=d.get("headline", "") or "",
            body=d.get("body", "") or "",
            why=d.get("why", "") or "",
            sector=d.get("sector", "") or "",
            tickers=_list(d.get("tickers")),
            source=d.get("source", "") or "",
            url=d.get("url", "") or "",
            image=d.get("image", "") or "",
            at=d.get("at", "") or "",
        )


@dataclass
class SectorMark:
    """One sectoral index for the day. `stance` is strong | weak | flat."""

    sector: str = ""
    pct: float = 0.0
    last: float = 0.0
    stance: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "SectorMark":
        d = d or {}
        return cls(sector=d.get("sector", "") or "", pct=_f(d.get("pct")),
                   last=_f(d.get("last")), stance=d.get("stance", "") or "")


@dataclass
class TradeSetup:
    """One intraday setup. `side` is long | short.

    `entry`, `target` and `stop` are levels off the day's own pivot band, and
    `pivot` / `r1` / `s1` are stored beside them so the arithmetic is visible
    on the sheet rather than only in the code that produced it — an editor or
    a viewer can check any level against the exchange's own high, low and
    close.

    `invalidates` is the condition that voids the setup, and it is a required
    part of the idea rather than a nicety: a level with no invalidation reads
    as a prediction, and a level with one reads as an observation about a
    range. The second is what this reel is allowed to claim.
    """

    side: str = ""
    rank: int = 0
    symbol: str = ""
    last: float = 0.0
    entry: float = 0.0
    target: float = 0.0
    stop: float = 0.0
    pivot: float = 0.0
    r1: float = 0.0
    s1: float = 0.0
    pct: float = 0.0
    close_pos: float = 0.0
    reason: str = ""
    invalidates: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "TradeSetup":
        d = d or {}
        return cls(
            side=(d.get("side", "") or "").lower(),
            rank=int(_f(d.get("rank"))),
            symbol=(d.get("symbol", "") or "").upper(),
            last=_f(d.get("last")), entry=_f(d.get("entry")),
            target=_f(d.get("target")), stop=_f(d.get("stop")),
            pivot=_f(d.get("pivot")), r1=_f(d.get("r1")), s1=_f(d.get("s1")),
            pct=_f(d.get("pct")), close_pos=_f(d.get("close_pos")),
            reason=d.get("reason", "") or "",
            invalidates=d.get("invalidates", "") or "",
        )

    @property
    def reward(self) -> float:
        """Distance to target as a percentage of entry, signed by side."""
        if not (self.entry and self.target):
            return 0.0
        return round(100 * (self.target - self.entry) / self.entry
                     * (1 if self.side == "long" else -1), 2)

    @property
    def risk(self) -> float:
        """Distance to stop as a percentage of entry."""
        if not (self.entry and self.stop):
            return 0.0
        return round(abs(100 * (self.entry - self.stop) / self.entry), 2)

    @property
    def rr(self) -> float:
        """Reward against risk. 0.0 when either leg is missing.

        Worth surfacing because it is the one number that can disqualify a
        setup on its own arithmetic, whatever the reasoning attached to it
        sounds like: a 0.4% target against a 1.2% stop is a bad idea.
        """
        return round(self.reward / self.risk, 2) if self.risk else 0.0


@dataclass
class Briefing:
    """One trading day's pre-market briefing — the whole of reel 7."""

    date: date | None = None
    trading: bool = True
    why_closed: str = ""
    at: str = ""                     # the exchange timestamp the data carried
    nifty: float = 0.0
    nifty_pct: float = 0.0
    nifty_prev: float = 0.0
    banknifty: float = 0.0
    banknifty_pct: float = 0.0
    advances: int = 0
    declines: int = 0
    unchanged: int = 0
    bias: str = ""                   # up | down | flat
    outlook: str = ""                # the prose for the opening scene
    levels_note: str = ""            # the disclosure spoken before the setups
    model: str = ""                  # which model wrote the words, on the row
    partial: str = ""                # which feeds were missing, if any
    notes: str = ""
    news: list[NewsItem] = field(default_factory=list)
    sectors: list[SectorMark] = field(default_factory=list)
    setups: list[TradeSetup] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "Briefing":
        d = d or {}
        raw = d.get("trading")
        return cls(
            date=_d(d.get("date")),
            # 'yes'/'no' on the sheet, a bool in memory, and a missing flag
            # means trading — a briefing exists only for a day somebody built
            # one for, and defaulting an absent flag to "closed" would hide
            # the record rather than describe it.
            trading=raw if isinstance(raw, bool)
            else str("yes" if raw is None else raw).strip().lower()
            not in ("no", "false", "0"),
            why_closed=d.get("why_closed", "") or "",
            at=d.get("at", "") or "",
            nifty=_f(d.get("nifty")), nifty_pct=_f(d.get("nifty_pct")),
            nifty_prev=_f(d.get("nifty_prev")),
            banknifty=_f(d.get("banknifty")),
            banknifty_pct=_f(d.get("banknifty_pct")),
            advances=int(_f(d.get("advances"))),
            declines=int(_f(d.get("declines"))),
            unchanged=int(_f(d.get("unchanged"))),
            bias=(d.get("bias", "") or "").lower(),
            outlook=d.get("outlook", "") or "",
            levels_note=d.get("levels_note", "") or "",
            model=d.get("model", "") or "",
            partial=d.get("partial", "") or "",
            notes=d.get("notes", "") or "",
            news=sorted((NewsItem.from_dict(x) for x in (d.get("news") or [])),
                        key=lambda n: n.idx),
            sectors=sorted(
                (SectorMark.from_dict(x) for x in (d.get("sectors") or [])),
                key=lambda s: -s.pct),
            # Longs first, then shorts, each by rank. The order is
            # load-bearing, not cosmetic: the two board scenes render this
            # list in slices, so a shuffled list puts a short setup on the
            # longs card. Same reasoning as the GMP sort above.
            setups=sorted(
                (TradeSetup.from_dict(x) for x in (d.get("setups") or [])),
                key=lambda s: (s.side != "long", s.rank)),
        )

    def to_dict(self) -> dict:
        def conv(o):
            if isinstance(o, dict):
                return {k: conv(v) for k, v in o.items()}
            if isinstance(o, (list, tuple)):
                return [conv(v) for v in o]
            if isinstance(o, date):
                return o.isoformat()
            return o
        return conv(asdict(self))

    @property
    def key(self) -> str:
        """The store key: the ISO date."""
        return self.date.isoformat() if self.date else ""

    def side(self, which: str) -> list[TradeSetup]:
        return [s for s in self.setups if s.side == which]
