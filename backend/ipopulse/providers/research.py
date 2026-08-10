"""Gemini-backed research provider.

Implements the same Provider contract as everything else, so `sync` treats it
like any other source. What makes it different is that its values are
*proposed*: each carries source URLs, a confidence, and a `needs_review` flag.

**Use each site for what it is actually authoritative on.** That is the single
biggest accuracy lever here:

  investorgain.com   a GMP-focused site with a dated live table. Good for GMP.
  ipowatch.in        same role, useful as a second opinion.
  groww.in           a SEBI-registered broker. Publishes exchange-sourced issue
                     details and live subscription — but generally NOT grey
                     market premium, because a regulated broker stays away from
                     unofficial data. Excellent for issue + subscription;
                     the wrong place to ask for GMP.
  nseindia / bseindia the primary record for subscription and issue terms.

Better still, pin the exact page for an IPO in its YAML:

    sources:
      gmp: https://www.investorgain.com/ipo/…/
      subscription: https://groww.in/ipo/vertex-aerospace

A pinned URL removes the two ways grounded lookup goes wrong — reading about a
similarly-named company, and answering from a page that went stale without
changing its date line.
"""

from __future__ import annotations

import re
from typing import Any

from ..ai import Gemini, AiUnavailable, iso_today, vet_subscription

# role -> ordered fallback pages, used when an IPO has nothing pinned.
#
# ipowatch.in was the second GMP source here and has been dropped as a
# default: its robots.txt sets `Content-Signal: ai-train=no` and disallows a
# list of AI fetchers outright, so routing it through url_context reads its
# content into a model against a preference it stated explicitly.
# investorgain.com publishes `Content-Signal: search=yes, ai-input=yes` and
# `Allow: /`, which is permission for exactly this use. Pin ipowatch per-IPO
# with `ipopulse sources <slug> --set gmp=...` if you decide otherwise.
# A NOTE ON GMP HISTORY, learned the hard way on 2026-08-10.
#
# The board URL below carries only TODAY's value per IPO. The dated
# day-by-day table lives on the per-IPO page instead:
#
#     https://www.investorgain.com/gmp/<company>-ipo/<id>/
#
# and `<id>` is not derivable from the slug — it has to be read off the board
# once and pinned with `ipopulse sources <slug> --set gmp=<url>`.
#
# Worse, that table is lazy-loaded on scroll. A plain HTTP fetch sees an empty
# tbody, and so does Gemini's url_context fetcher, which renders JavaScript but
# does not scroll. Only a real browser produces the rows. So `--what
# gmp-history` works on sources that render their history server-side and
# returns nothing here — which is why the CI fix matters more than the
# backfill: a day lost to a failed job on this source cannot be recovered
# automatically afterwards.
SITES: dict[str, list[str]] = {
    "gmp": [
        "https://www.investorgain.com/report/live-ipo-gmp/331/",
    ],
    "issue": [
        "https://groww.in/ipo",
        "https://www.nseindia.com/market-data/all-upcoming-issues-ipo",
    ],
    "subscription": [
        "https://groww.in/ipo",
        "https://www.nseindia.com/market-data/issue-information",
    ],
}

# Kept for backwards compatibility with the earlier flat list.
DEFAULT_GMP_SOURCES = SITES["gmp"]


class ResearchProvider:
    name = "research"

    def __init__(self, gemini: Gemini | None = None, sources: list[str] | None = None):
        self.ai = gemini or Gemini()
        # explicit override wins; otherwise resolved per role at call time
        self.sources = sources

    def available(self) -> bool:
        return self.ai.available()

    def urls_for(self, role: str, ipo: Any = None) -> list[str]:
        """Pinned URL for this IPO first, then the site defaults for the role."""
        if self.sources:
            return self.sources
        pinned = []
        pin = (getattr(ipo, "sources", None) or {}).get(role) if ipo else None
        if pin:
            pinned = [u.strip() for u in str(pin).split(",") if u.strip()]
        return pinned + [u for u in SITES.get(role, []) if u not in pinned]

    # ── contract ──────────────────────────────────────────────────────────
    def fetch_catalogue(self) -> list[dict[str, Any]]:
        raise NotImplementedError(
            "Research provider works per-company; give it a slug to look up."
        )

    def fetch_ipo(self, slug: str, company: str | None = None,
                  ipo: Any = None) -> dict[str, Any]:
        """Structural facts, mapped into canonical `Ipo` shape."""
        name = company or slug.replace("-", " ")
        raw = self.ai.research_ipo(name, urls=self.urls_for("issue", ipo))
        out: dict[str, Any] = {}

        if raw.get("company"):
            out["company"] = raw["company"]
        if raw.get("board") in ("Mainboard", "SME"):
            out["board"] = raw["board"]
        if raw.get("sector"):
            out["sector"] = raw["sector"]

        issue = {k: raw[k] for k in
                 ("fresh_cr", "ofs_cr", "price_low", "price_high", "lot_size",
                  "shares_post_issue_cr", "registrar")
                 if raw.get(k) is not None}
        if issue:
            out["issue"] = issue

        dates = {k: raw[k] for k in
                 ("announced", "open", "close", "allotment", "listing")
                 if raw.get(k)}
        if dates:
            out["dates"] = dates

        out["_meta"] = {
            "confidence": raw.get("confidence", "low"),
            "note": raw.get("note", ""),
            "sources": raw.get("sources", []),
        }
        return out

    def fetch_gmp_history(self, slug: str, company: str | None = None,
                          price_high: float = 0.0, ipo: Any = None,
                          since: str | None = None) -> list[dict[str, Any]]:
        """The full dated GMP series, for repairing gaps in the trail.

        Points that fail vetting are kept but marked, so the caller can show
        what was rejected and why rather than silently returning a shorter
        list than the page had.
        """
        name = company or slug.replace("-", " ")
        rows = self.ai.research_gmp_history(
            name, price_high=price_high, since=since,
            urls=self.urls_for("gmp", ipo),
        )
        return [r for r in rows if r.get("date")]

    def fetch_financials(self, slug: str, company: str | None = None,
                         ipo: Any = None) -> dict[str, Any]:
        """The FY table, vetted for shape before it is offered.

        A financials block that is the wrong *length* is more dangerous than
        one that is absent: compute.py zips years against values by index, so
        a short array silently slides FY25's revenue into FY24's row and every
        CAGR, margin and benchmark downstream is then computed from a table
        nobody typed. Length is checked here, once, rather than trusted.
        """
        name = company or slug.replace("-", " ")
        years = list(getattr(getattr(ipo, "financials", None), "years", None)
                     or ["FY23", "FY24", "FY25"])
        raw = self.ai.research_financials(name, years=years,
                                          urls=self.urls_for("issue", ipo))

        # The source's own year labels win over the scaffold's. `new` defaults
        # to FY23-FY25, which is simply wrong for an issue coming to market in
        # 2026 — its RHP shows FY24-FY26. Forcing our labels onto that table
        # made the model return a null for the year it could not supply, and
        # the whole read was then rejected as incomplete when it had in fact
        # found everything.
        found = [str(y).strip() for y in (raw.get("years") or []) if str(y).strip()]
        if found:
            years = found

        n = len(years)
        series: dict[str, list[float]] = {}
        problems: list[str] = []
        for key in ("revenue", "ebitda", "pat", "net_worth", "total_debt"):
            vals = raw.get(key) or []
            if not vals:
                continue
            if len(vals) != n:
                problems.append(f"{key}: {len(vals)} values for {n} years")
                continue
            if any(v is None for v in vals):
                problems.append(f"{key}: has a null year")
                continue
            try:
                series[key] = [float(v) for v in vals]
            except (TypeError, ValueError):
                problems.append(f"{key}: non-numeric value")

        out: dict[str, Any] = {"years": years, **series}
        note = str(raw.get("note") or "")
        # Summary pages very often print the PRE-issue EPS, and `eps` here
        # means post-issue — the P/E on reel 4 is computed straight off it.
        # Pre-issue EPS is the larger number (fewer shares), so accepting one
        # silently publishes a P/E that flatters the issue. If the model says
        # it is pre-IPO, believe it and drop the field.
        pre_issue_eps = re.search(r"\bpre[- ]?(ipo|issue)\b", note, re.I) is not None
        for key in ("eps", "pe_peer_avg"):
            if raw.get(key) is None:
                continue
            if key == "eps" and pre_issue_eps:
                problems.append("eps: source reports it pre-issue, not post-issue")
                continue
            try:
                out[key] = float(raw[key])
            except (TypeError, ValueError):
                problems.append(f"{key}: non-numeric")

        confidence = raw.get("confidence", "low")
        # Financials feed 45% of the score's weight. Anything less than a
        # clean, full-length, high-confidence read gets a human's eyes first.
        needs_review = bool(problems) or confidence != "high" or not series
        out["_meta"] = {
            "confidence": confidence,
            "note": raw.get("note", ""),
            "sources": raw.get("sources", []),
            "problems": problems,
            "needs_review": needs_review,
            "review_reason": "; ".join(problems) if problems else
                             ("nothing found" if not series
                              else f"confidence {confidence}"),
        }
        return out

    def fetch_gmp(self, slug: str, company: str | None = None,
                  price_high: float = 0.0, ipo: Any = None) -> list[dict[str, Any]]:
        """A single vetted GMP point, or [] when nothing trustworthy was found."""
        name = company or slug.replace("-", " ")
        res = self.ai.research_gmp(name, price_high=price_high,
                                   urls=self.urls_for("gmp", ipo))
        if res.get("gmp") is None:
            return []
        return [{
            "date": res.get("date") or iso_today(),
            "gmp": res["gmp"],
            "kostak": res.get("kostak") or 0,
            "source": "gemini",
            # provenance travels with the number, all the way into the YAML
            "confidence": res.get("confidence"),
            "needs_review": res.get("needs_review"),
            "review_reason": res.get("reason"),
            "sources": res.get("sources", []),
        }]

    def fetch_subscription(self, slug: str, company: str | None = None,
                           ipo: Any = None) -> list[dict[str, Any]]:
        """Day-wise subscription, read from a broker/exchange page.

        Safer than GMP because the underlying figure is published by the
        exchanges — but it moves through the day, so the reported day number
        and timestamp matter as much as the multiples.
        """
        name = company or slug.replace("-", " ")
        res = self.ai.research_subscription(name, urls=self.urls_for("subscription", ipo))
        if not res.get("total") and not res.get("retail"):
            return []
        row = {
            "day": int(res.get("day") or 1),
            "date": res.get("as_of") or iso_today(),
            "qib": res.get("qib") or 0,
            "nii": res.get("nii") or 0,
            "retail": res.get("retail") or 0,
            "employee": res.get("employee") or 0,
            "total": res.get("total") or 0,
        }
        row.update({
            "confidence": res.get("confidence"),
            "needs_review": res.get("needs_review"),
            "review_reason": res.get("reason"),
            "sources": res.get("sources", []),
        })
        return [row]
