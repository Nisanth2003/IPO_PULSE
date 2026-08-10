"""API adapter — the seam where your future data source plugs in.

Nothing here fetches anything yet, by design: you said you would pick the API.
What this file does is pin down the *contract*, so wiring a real source is a
matter of filling in three methods and mapping fields — no changes to compute,
publish, the Excel report, or the frontend.

To implement:

    1. Set IPOPULSE_API_BASE (and IPOPULSE_API_KEY if the source needs one).
    2. Fill in `_get()` with whatever auth/headers the provider wants.
    3. Map the response into canonical shape in the `fetch_*` methods.
    4. Run `ipopulse sync --slug <slug>` to merge it into your YAML.

A caution worth writing down: there is no official free GMP feed. Every public
one is a scrape of a community site — unofficial, rate-limited and liable to
change shape without notice. Keep `merge()` on manual-wins (the default) so a
bad fetch degrades into a stale number rather than a wrong published one, and
keep the `source` field on every GMP point so you can tell later where a
figure came from.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

USER_AGENT = "ipo-pulse/1.0 (+https://github.com/)"


class ApiProvider:
    """Skeleton HTTP provider. Returns nothing until you implement it."""

    name = "api"

    def __init__(self, base: str | None = None, key: str | None = None, timeout: int = 15):
        self.base = (base or os.getenv("IPOPULSE_API_BASE") or "").rstrip("/")
        self.key = key or os.getenv("IPOPULSE_API_KEY") or ""
        self.timeout = timeout

    def available(self) -> bool:
        return bool(self.base)

    # ── plumbing ──────────────────────────────────────────────────────────
    def _get(self, path: str, params: dict | None = None) -> Any:
        if not self.available():
            raise RuntimeError(
                "No IPOPULSE_API_BASE configured — set it in .env, or keep "
                "using the manual provider."
            )
        url = f"{self.base}/{path.lstrip('/')}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"{self.name} HTTP {exc.code} for {url}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"{self.name} unreachable: {exc.reason}") from exc

    def _headers(self) -> dict[str, str]:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        return headers

    # ── contract ──────────────────────────────────────────────────────────
    # Each method must return canonical shapes (see models.py). Raise
    # NotImplementedError until mapped, so `sync` reports it honestly instead
    # of silently publishing empty data.

    def fetch_catalogue(self) -> list[dict[str, Any]]:
        """-> [{slug, company, board, sector, dates: {...}}, ...]"""
        raise NotImplementedError("Map your provider's IPO list here.")

    def fetch_ipo(self, slug: str) -> dict[str, Any]:
        """-> partial Ipo dict: {company, sector, issue: {...}, dates: {...},
        financials: {...}}"""
        raise NotImplementedError("Map your provider's IPO detail here.")

    def fetch_gmp(self, slug: str) -> list[dict[str, Any]]:
        """-> [{date: 'YYYY-MM-DD', gmp: float, kostak?, sauda?, source}]

        Return the full history if the source exposes it; `merge_series`
        de-duplicates on date, so returning only today's point also works.
        """
        raise NotImplementedError("Map your provider's GMP feed here.")

    def fetch_subscription(self, slug: str) -> list[dict[str, Any]]:
        """-> [{day, date, qib, nii, retail, employee, total}]"""
        raise NotImplementedError("Map your provider's subscription feed here.")
