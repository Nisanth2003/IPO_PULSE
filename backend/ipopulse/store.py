"""YAML-backed store: one file per IPO under backend/data/ipos/<slug>.yaml.

Plain text on purpose — it diffs cleanly in git, so the repo doubles as the
history of what you published and when.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Iterator

import yaml

from .models import Ipo

# backend/ipopulse/store.py -> backend/
BACKEND_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = BACKEND_ROOT / "data"
IPO_DIR = DATA_DIR / "ipos"
CACHE_DIR = DATA_DIR / "cache"
OUT_DIR = BACKEND_ROOT / "out"
# backend/ -> repo root -> frontend/data
FRONTEND_DATA = BACKEND_ROOT.parent / "frontend" / "data"


def _represent_date(dumper, value):
    return dumper.represent_scalar("tag:yaml.org,2002:str", value.isoformat())


class _Dumper(yaml.SafeDumper):
    pass


_Dumper.add_representer(date, _represent_date)


def ipo_path(slug: str) -> Path:
    return IPO_DIR / f"{slug}.yaml"


def list_slugs() -> list[str]:
    if not IPO_DIR.exists():
        return []
    return sorted(p.stem for p in IPO_DIR.glob("*.yaml"))


def load(slug: str) -> Ipo:
    path = ipo_path(slug)
    if not path.exists():
        raise FileNotFoundError(f"No IPO file at {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw.setdefault("slug", slug)
    return Ipo.from_dict(raw)


def load_all() -> list[Ipo]:
    out = []
    for slug in list_slugs():
        try:
            out.append(load(slug))
        except Exception as exc:                      # keep one bad file from
            print(f"  ! skipping {slug}: {exc}")      # breaking the whole build
    return out


def save(ipo: Ipo) -> Path:
    IPO_DIR.mkdir(parents=True, exist_ok=True)
    path = ipo_path(ipo.slug)
    path.write_text(
        yaml.dump(ipo.to_dict(), Dumper=_Dumper, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return path


def iter_ipos() -> Iterator[Ipo]:
    for slug in list_slugs():
        yield load(slug)


# ── scaffolding ────────────────────────────────────────────────────────────

TEMPLATE = """\
slug: {slug}
company: ""
initials: ""          # blank = derived from company name
board: Mainboard      # Mainboard | SME
sector: ""

issue:
  fresh_cr: 0         # fresh issue, rupees crore -> funds the company
  ofs_cr: 0           # offer for sale, rupees crore -> promoters cash out
  price_low: 0
  price_high: 0
  lot_size: 0
  shares_post_issue_cr: 0    # optional, only needed for market cap
  # Left blank on purpose. This used to default to KFintech with its status
  # URL, which made every scaffolded IPO *claim* a registrar — and because
  # sync and research only fill blanks, the wrong one then stuck. Reel 6 sent
  # viewers to KFintech to check an allotment held by Bigshare or Cameo.
  # `ipopulse doctor --fix` derives the URL once the name is right.
  registrar: ""
  registrar_url: ""
  exchanges: [BSE, NSE]

dates:
  announced:          # GMP tracking starts here
  open:
  close:
  close_time: "17:00"
  allotment:
  refund:
  listing:

financials:           # rupees crore, one value per year
  years: [FY23, FY24, FY25]
  revenue: []
  ebitda: []
  pat: []
  net_worth: []
  total_debt: []
  eps: 0              # post-issue EPS
  pe_peer_avg: 0      # peer average P/E

gmp_history: []
# - {{date: 2026-01-02, gmp: 22}}
# - {{date: 2026-01-03, gmp: 28}}

subscription: []
# - {{day: 1, date: 2026-01-05, qib: 0.8, nii: 2.1, retail: 3.4, total: 2.3}}

analysis:             # English source text; translations are generated
  overview: []
  green_flags: []
  red_flags: []
  growth: ""
  valuation: ""
  risk: ""
  growth_tone: good   # good | warn | bad
  valuation_tone: warn
  score: 0            # 0-10
  verdict: apply      # apply | both | longterm | risky | avoid
  verdict_text: ""
  reco_retail: apply  # apply | watch | avoid
  reco_hni: watch
  reco_long: watch

allotment:
  status: expected    # expected | out
  listing_low: 0
  listing_high: 0
  steps: []           # blank = translated defaults

sources: {{}}           # exact pages to read for this IPO — beats searching:
                      #   gmp: https://www.investorgain.com/ipo/<name>/
                      #   subscription: https://groww.in/ipo/<name>
                      #   issue: https://groww.in/ipo/<name>
                      # Pin them with: ipopulse sources <slug> --set gmp=<url>

benchmarks: {{}}        # override the "what counts as good" lines, e.g.
                      #   ronw: 12
                      #   ebitda_margin: 20
                      # Defaults: EBITDA margin 15%, PAT margin 8%, revenue
                      # CAGR 15%, RoNW 15%, D/E below 1x, P/E vs peer average.
                      # Worth overriding for banks, utilities, and anything
                      # else the generic thresholds misjudge.

i18n: {{}}              # written by `ipopulse translate`
notes: ""
"""


def scaffold(slug: str, overwrite: bool = False) -> Path:
    IPO_DIR.mkdir(parents=True, exist_ok=True)
    path = ipo_path(slug)
    if path.exists() and not overwrite:
        raise FileExistsError(f"{path} already exists (use --force to overwrite)")
    path.write_text(TEMPLATE.format(slug=slug), encoding="utf-8")
    return path
