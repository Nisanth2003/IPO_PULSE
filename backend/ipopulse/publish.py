"""Verify the workbook the site will read.

There used to be a publish step here: it recomputed every derived number and
wrote frontend/data/*.json for the browser to fetch. That JSON was a second
copy of the truth, regenerated from the store on every run, and the two
could disagree — which is exactly what a reader sees when a scheduled job
half-finishes.

The workbook is now the only copy. frontend/js/data.js fetches it directly
and derives the same numbers with compute.js, so there is nothing left to
publish. What remains worth doing under the name `build` is proving the file
the site is about to serve actually parses and derives cleanly, because the
alternative is finding out from a blank card on camera.

`derive` is called for its side effect of raising — if a record has, say, a
price band that will not divide, this is where it surfaces.
"""

from __future__ import annotations

from .compute import derive
from .models import Ipo
from .store import where


def verify(ipos: list[Ipo]) -> list[str]:
    """Derive every record. Returns one line per IPO that failed."""
    broken: list[str] = []
    for ipo in ipos:
        try:
            derive(ipo)
        except Exception as exc:
            broken.append(f"{ipo.slug}: {type(exc).__name__}: {exc}")
    return broken


def publish(ipos: list[Ipo]) -> list:
    """Kept so the callers that ran a build after writing still do.

    Returns the workbook path in a list, matching the old signature that
    returned the files it had written.
    """
    verify(ipos)
    return [where()]
