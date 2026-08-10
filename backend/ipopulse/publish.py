"""Publish static JSON for the frontend.

GitHub Pages serves files, not code, so the "backend" ships its output as
plain JSON that the static site fetches. Same contract a real HTTP API would
expose, which is why frontend/js/data.js can point at either:

    frontend/data/index.json          catalogue + board snapshot
    frontend/data/board.json          all IPOs, one row each
    frontend/data/ipo/<slug>.json     one full record (facts + derived + i18n)

Committing these files is the deploy.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .compute import derive
from .models import Ipo
from .store import FRONTEND_DATA


def _write(path: Path, payload: dict | list) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return path


def ipo_payload(ipo: Ipo) -> dict:
    """Facts + derived numbers + translations, in one object."""
    return {
        "schema": 1,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "ipo": ipo.to_dict(),
        "derived": derive(ipo),
    }


def board_row(ipo: Ipo) -> dict:
    """One line on the all-IPOs board (Daily GMP reel, mode B)."""
    d = derive(ipo)
    g, s = d["gmp"], d["subscription"]
    return {
        "slug": ipo.slug,
        "company": ipo.company or ipo.slug,
        "initials": d["initials"],
        "board": ipo.board,
        "status": d["dates"]["status"],
        "price_low": ipo.issue.price_low,
        "price_high": ipo.issue.price_high,
        "lot_size": ipo.issue.lot_size,
        "min_investment": d["issue"]["min_investment"],
        "gmp": g["gmp"],
        "gmp_pct": g["pct"],
        "est_listing": g["est_listing"],
        "gain_per_lot": g["gain_per_lot"],
        "movement": g["movement"],
        "subscription": s["total"] if s["has_data"] else None,
        "open": d["dates"]["open"],
        "close": d["dates"]["close"],
        "listing": d["dates"]["listing"],
    }


def publish(ipos: list[Ipo], out_dir: Path | None = None) -> list[Path]:
    out_dir = out_dir or FRONTEND_DATA
    written: list[Path] = []

    for ipo in ipos:
        written.append(_write(out_dir / "ipo" / f"{ipo.slug}.json", ipo_payload(ipo)))

    rows = [board_row(ipo) for ipo in ipos]
    # liveliest first: open issues, then upcoming, then done
    order = {"open": 0, "upcoming": 1, "closed": 2, "allotment": 3, "listed": 4}
    rows.sort(key=lambda r: (order.get(r["status"], 9), -(r["gmp_pct"] or 0)))

    written.append(_write(out_dir / "board.json", {
        "schema": 1,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "rows": rows,
    }))

    written.append(_write(out_dir / "index.json", {
        "schema": 1,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "count": len(ipos),
        "ipos": [
            {
                "slug": r["slug"],
                "company": r["company"],
                "initials": r["initials"],
                "board": r["board"],
                "status": r["status"],
            }
            for r in rows
        ],
    }))
    return written
