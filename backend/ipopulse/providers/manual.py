"""Manual provider — the records you maintain by hand in the sheet.

This is the source of record and stays authoritative even with the API
providers wired up: `merge()` gives hand-typed values precedence, so a wrong
number from a scraper can always be corrected by editing the spreadsheet.
"""

from __future__ import annotations

from typing import Any

from .. import store


class ManualProvider:
    name = "manual"

    def available(self) -> bool:
        # One workbook now, not a directory of files — but the question is the
        # same one: is there a store to read at all?
        return bool(store.list_slugs())

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
