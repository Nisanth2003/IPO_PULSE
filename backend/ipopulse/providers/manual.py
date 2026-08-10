"""Manual provider — the YAML files you maintain by hand.

This is the source of record today and stays authoritative even after you wire
an API: `merge()` gives hand-typed values precedence, so a wrong number from a
scraper can always be corrected by editing the file.
"""

from __future__ import annotations

from typing import Any

from .. import store


class ManualProvider:
    name = "manual"

    def available(self) -> bool:
        return store.IPO_DIR.exists()

    def fetch_catalogue(self) -> list[dict[str, Any]]:
        return [
            {
                "slug": ipo.slug,
                "company": ipo.company,
                "board": ipo.board,
                "sector": ipo.sector,
            }
            for ipo in store.load_all()
        ]

    def fetch_ipo(self, slug: str) -> dict[str, Any]:
        return store.load(slug).to_dict()

    def fetch_gmp(self, slug: str) -> list[dict[str, Any]]:
        return store.load(slug).to_dict().get("gmp_history", [])

    def fetch_subscription(self, slug: str) -> list[dict[str, Any]]:
        return store.load(slug).to_dict().get("subscription", [])
