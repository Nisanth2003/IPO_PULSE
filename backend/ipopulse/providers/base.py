"""Provider interface.

The point of this layer: the frontend must never learn where a number came
from. A provider's only job is to return partial `Ipo`-shaped dicts, which
`merge()` folds into the stored record. Add an API adapter later and nothing
downstream changes.

Precedence is deliberate — a fetched value never silently overwrites something
you typed by hand. Manual data is treated as the correction of record.
"""

from __future__ import annotations

from typing import Any, Protocol


class Provider(Protocol):
    """Anything that can supply IPO facts."""

    name: str

    def available(self) -> bool:
        """False when the provider is unconfigured (no key, no network)."""
        ...

    def fetch_catalogue(self) -> list[dict[str, Any]]:
        """Currently open/upcoming IPOs: [{slug, company, board, dates...}]."""
        ...

    def fetch_ipo(self, slug: str) -> dict[str, Any]:
        """Partial record for one IPO, in canonical `Ipo` dict shape."""
        ...

    def fetch_gmp(self, slug: str) -> list[dict[str, Any]]:
        """Grey-market points: [{date, gmp, kostak?, sauda?, source}]."""
        ...

    def fetch_subscription(self, slug: str) -> list[dict[str, Any]]:
        """Day-wise subscription: [{day, date, qib, nii, retail, total}]."""
        ...


def merge(base: dict, incoming: dict, *, prefer_incoming: bool = False) -> dict:
    """Deep-merge `incoming` into `base`.

    By default `base` (your hand-typed YAML) wins any conflict; only genuinely
    empty fields get filled in. Pass prefer_incoming=True for a live refresh
    where the API really is more current than the file.
    """
    out = dict(base)
    for key, val in (incoming or {}).items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = merge(out[key], val, prefer_incoming=prefer_incoming)
            continue
        existing = out.get(key)
        empty = existing in (None, "", [], {}, 0)
        if prefer_incoming or empty:
            if val not in (None, "", [], {}):
                out[key] = val
    return out


def merge_series(
    existing: list[dict], incoming: list[dict], key: str = "date"
) -> list[dict]:
    """Union two time series on `key`, newest data winning, sorted ascending.

    Used for GMP history so a daily fetch appends without duplicating days.
    """
    by_key: dict[Any, dict] = {}
    for row in existing or []:
        if row.get(key) is not None:
            by_key[str(row[key])] = dict(row)
    for row in incoming or []:
        if row.get(key) is not None:
            by_key[str(row[key])] = {**by_key.get(str(row[key]), {}), **row}
    return [by_key[k] for k in sorted(by_key)]
