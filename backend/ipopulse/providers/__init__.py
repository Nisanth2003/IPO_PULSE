"""Data providers.

  manual    the YAML files you maintain — the source of record
  sheet     an Excel/CSV file or URL
  nse       live issue terms + subscription from NSE, no key needed
  research  Gemini with search grounding (proposes, never decides)
  api       skeleton for a future HTTP feed

`merge()` gives hand-typed values precedence, so a fetched figure fills blanks
rather than overwriting a correction you made deliberately.
"""

from .base import Provider, merge, merge_series
from .manual import ManualProvider
from .api import ApiProvider
from .sheet import SheetProvider
from .research import ResearchProvider
from .scrape import NseProvider

__all__ = [
    "Provider", "merge", "merge_series",
    "ManualProvider", "ApiProvider", "SheetProvider", "ResearchProvider",
    "NseProvider",
]


def get_provider(name: str = "manual", **kwargs):
    """Resolve a provider by name."""
    if name == "manual":
        return ManualProvider()
    if name == "api":
        return ApiProvider()
    if name == "sheet":
        return SheetProvider(**kwargs)
    if name == "research":
        return ResearchProvider(**kwargs)
    if name == "nse":
        return NseProvider()
    raise ValueError(
        f"Unknown provider {name!r} (expected manual, sheet, nse, research or api)"
    )
