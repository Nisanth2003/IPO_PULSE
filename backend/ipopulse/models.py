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
    score: float = 0.0               # 0-10
    verdict: str = "apply"           # apply|both|longterm|risky|avoid
    verdict_text: str = ""           # optional override
    reco_retail: str = "apply"       # apply|watch|avoid
    reco_hni: str = "watch"
    reco_long: str = "watch"

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
            verdict=d.get("verdict", "apply") or "apply",
            verdict_text=d.get("verdict_text", "") or "",
            reco_retail=d.get("reco_retail", "apply") or "apply",
            reco_hni=d.get("reco_hni", "watch") or "watch",
            reco_long=d.get("reco_long", "watch") or "watch",
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
