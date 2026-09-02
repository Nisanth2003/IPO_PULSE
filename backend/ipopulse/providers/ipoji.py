"""GMP from ipoji.com — free, keyless, and no model in the loop.

Grey-market premium was the one field nothing could supply. No exchange
publishes it (it is an unofficial market by definition), the site this project
already used renders its tables with JavaScript and lazy-loads them on scroll,
and asking a model to read a page costs quota and returns something that has
to be vetted before it can be believed.

ipoji.com serves the same data **server-side**, with no JavaScript, no cookies
and no session warm-up, and every row carries the figures as `data-*`
attributes rather than as formatted text:

    <tr class="gmp-row" data-name="Behari Lal Engineering" data-gmp="65"
        data-pct="23" data-indicative="350" data-status="open"
        data-rowurl="/ipo-gmp/behari-lal-engineering-ipo">

Per-IPO pages carry a dated history with ISO timestamps, which is what makes
a missed day recoverable at all:

    <td data-label="Date"><time datetime="2026-08-12T16:00:00.000Z">…
    <td data-label="GMP">+₹141</td>

robots.txt allows everything except /profile and /bids, and states no content
signal — checked 2026-08-13.

Two honest caveats.

**This is HTML, not an API.** The `data-*` attributes exist for the site's own
sort/filter code, which makes them far more stable than presentational markup,
but they are not a contract. `board()` returning nothing is a normal outcome
to handle, not an exception to raise.

**Sources disagree, because the grey market is a dealer network rather than an
exchange.** ipoji quotes Molbio at ₹115 for 10 Aug where the previously stored
reading was ₹135. Neither is wrong exactly; they are different desks. So this
provider is wired to fill *gaps* and never to overwrite a reading already
taken — see cmd_gmp_sync.
"""

from __future__ import annotations

import gzip
import re
import time
import urllib.request
from datetime import date
from typing import Any

BASE = "https://www.ipoji.com"
BOARD = f"{BASE}/ipo-gmp"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
MIN_INTERVAL = 1.0          # seconds between calls — this is someone's server

_last_call = 0.0


def _get(url: str, timeout: int = 30) -> str:
    global _last_call
    wait = MIN_INTERVAL - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "text/html,*/*",
        "Accept-Encoding": "gzip", "Referer": BOARD,
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    _last_call = time.monotonic()
    return raw.decode("utf-8", "replace")


_ROW = re.compile(r"<tr[^>]*class=\"[^\"]*gmp-row[^\"]*\"[^>]*>", re.I)
_ATTR = re.compile(r"data-([a-z]+)=\"([^\"]*)\"", re.I)
_MONEY = re.compile(r"(-|\+|−)?\s*₹?\s*([\d,]+(?:\.\d+)?)")


def _money(text: str) -> float | None:
    """'+₹141' / '-₹5' / '₹0' -> float. Negative GMPs are real and must survive."""
    m = _MONEY.search(text or "")
    if not m:
        return None
    value = float(m.group(2).replace(",", ""))
    return -value if m.group(1) in ("-", "−") else value


def board() -> list[dict[str, Any]]:
    """Every IPO on the GMP board, today. [] if the markup changed."""
    try:
        html = _get(BOARD)
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for tag in _ROW.findall(html):
        d = dict(_ATTR.findall(tag))
        name = (d.get("name") or "").strip()
        if not name:
            continue
        try:
            gmp = float(d["gmp"]) if d.get("gmp") not in (None, "") else None
        except ValueError:
            gmp = None
        out.append({
            "name": name,
            "gmp": gmp,
            "pct": d.get("pct"),
            "indicative": d.get("indicative"),
            "status": d.get("status"),
            "board": d.get("type"),
            "url": BASE + d["rowurl"] if d.get("rowurl") else None,
            "has_gmp": d.get("hasgmp") == "true",
        })
    return out


_HISTROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_WHEN = re.compile(r"<time[^>]*datetime=\"([^\"]+)\"", re.I)
_GMPCELL = re.compile(r"data-label=\"GMP\"[^>]*>([^<]*)<", re.I)


def history(url: str) -> list[dict[str, Any]]:
    """Dated GMP series from a per-IPO page, oldest first.

    The date comes from the <time datetime> attribute rather than the visible
    text: the display format is localised and the attribute is ISO-8601, so
    only one of the two can be parsed without guessing.
    """
    try:
        html = _get(url)
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    for body in _HISTROW.findall(html):
        when, cell = _WHEN.search(body), _GMPCELL.search(body)
        if not (when and cell):
            continue
        gmp = _money(cell.group(1))
        if gmp is None:
            continue
        try:
            day = date.fromisoformat(when.group(1)[:10]).isoformat()
        except ValueError:
            continue
        rows.append({"date": day, "gmp": gmp, "source": "ipoji"})
    rows.sort(key=lambda r: r["date"])
    return rows


# ── matching ipoji's names to our slugs ────────────────────────────────────
_NOISE = {"limited", "ltd", "private", "pvt", "india", "the", "and", "ipo",
          "company", "co", "industries", "enterprises"}


def _tokens(text: str) -> set[str]:
    words = re.split(r"[^a-z0-9]+", (text or "").lower())
    return {w for w in words if w and w not in _NOISE}


def match_slug(name: str, url: str, slugs: list[str],
               companies: dict[str, str]) -> str | None:
    """Best of our slugs for an ipoji row, or None.

    ipoji's slugs are close to ours but not identical — 'credent-connect-ipo'
    against our 'credent-connect-n-care'. Exact-match first on both the URL
    slug and the company name, then fall back to token overlap, which is what
    bridges the abbreviated ones. A tie or a weak overlap returns None: a GMP
    filed against the wrong company is far worse than a GMP not filed.
    """
    url_slug = ""
    if url:
        url_slug = url.rstrip("/").rsplit("/", 1)[-1]
        url_slug = re.sub(r"-ipo$", "", url_slug)
    if url_slug in slugs:
        return url_slug

    want = _tokens(name)
    if not want:
        return None

    # ── containment, before the overlap score
    #
    # Jaccard puts the union in the denominator, so a name that is one of ours
    # *plus* its legal description scores badly however certain the match is.
    # 'Rays of Belief Limited- For Profit Social Enterprise' against our 'Rays
    # of Belief' overlaps on all three of our tokens and still scores 3/7 =
    # 0.43 — under the 0.5 floor. The matcher returned None, discovery took
    # that as "new company", and the sheet carried Rays of Belief twice for
    # five days with the financials on one row and the fresh-issue split on
    # the other.
    #
    # One token set being a subset of the other is a different and stronger
    # statement than a good overlap ratio, so it is tested first and is not
    # subject to the ratio floor. Two guards keep it honest: the smaller set
    # needs at least two real tokens (a single shared word is a coincidence,
    # and `_NOISE` has already removed the words most likely to be shared),
    # and two of ours containing the same name is an ambiguity, not a match —
    # the whole point of returning None is that a GMP filed against the wrong
    # company is far worse than a GMP not filed.
    contained = []
    for slug in slugs:
        have = _tokens(companies.get(slug, "")) | _tokens(slug)
        if len(have) < 2 or len(want) < 2:
            continue
        if have <= want or want <= have:
            contained.append(slug)
    if len(contained) == 1:
        return contained[0]

    scored: list[tuple[float, str]] = []
    for slug in slugs:
        have = _tokens(companies.get(slug, "")) | _tokens(slug)
        if not have:
            continue
        overlap = len(want & have)
        if not overlap:
            continue
        scored.append((overlap / len(want | have), slug))
    if not scored:
        return None
    scored.sort(reverse=True)
    best, runner = scored[0], (scored[1] if len(scored) > 1 else (0.0, ""))
    # Needs to be both good and clearly better than the next candidate.
    if best[0] >= 0.5 and best[0] - runner[0] >= 0.15:
        return best[1]
    return None
