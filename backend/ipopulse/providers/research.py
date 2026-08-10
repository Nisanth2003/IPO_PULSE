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
                 ("fresh_cr", "ofs_cr", "price_low", "price_high", "lot_size", "registrar")
                 if raw.get(k) is not None}
        if issue:
            out["issue"] = issue

        dates = {k: raw[k] for k in ("open", "close", "allotment", "listing")
                 if raw.get(k)}
        if dates:
            out["dates"] = dates

        out["_meta"] = {
            "confidence": raw.get("confidence", "low"),
            "note": raw.get("note", ""),
            "sources": raw.get("sources", []),
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
