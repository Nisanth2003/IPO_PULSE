"""The storage layout, in one place, independent of where the cells live.

An `Ipo` is a nested object; a spreadsheet is a grid. This module owns the
translation between the two, and nothing else — no files, no network. Both
backends are thin wrappers around it:

    sheets.py     the store: tabs in the live Google Sheet
    workbook.py   a local .xlsx snapshot, for backups only

Keeping the layout here is what lets the site read the same structure the
backend writes: `frontend/js/data.js` mirrors `from_tables` tab for tab.
Change a column here and that file has to change with it.

The shape is normalised, not pretty. One wide tab keyed by `slug`, and long
tabs for anything repeating — years, days, bullets, translations. A single
row per IPO cannot hold three years of financials or two languages of prose,
which is exactly why the summary sheet this replaced could not be the store.

    IPOs          one row per IPO, dotted-path columns for every scalar
    Financials    slug, year, revenue, ebitda, pat, net_worth, total_debt
    GMP           slug, date, gmp, kostak, sauda, source
    Subscription  slug, day, date, qib, nii, retail, employee, total,
                  nii_small, nii_big
    Lists         slug, field, idx, value      (analysis bullets, steps)
    I18n          slug, lang, key, idx, value  (hi / te)
    Benchmarks    slug, metric, value
    Sources       slug, role, value  (logo, nse_symbol, isin, exchange)

A blank cell means "absent" and is not the same as 0 — compute.py judges a
metric only when its series exists, so writing 0.0 into an empty revenue
year would invent a company with no revenue.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

# ── column plans ───────────────────────────────────────────────────────────

# (column header, path into to_dict(), kind). Order is the column order.
SCALARS: list[tuple[str, tuple[str, ...], str]] = [
    ("slug",                        ("slug",),                          "text"),
    ("company",                     ("company",),                       "text"),
    ("initials",                    ("initials",),                      "text"),
    ("board",                       ("board",),                         "text"),
    ("sector",                      ("sector",),                        "text"),
    ("issue.fresh_cr",              ("issue", "fresh_cr"),              "num"),
    ("issue.ofs_cr",                ("issue", "ofs_cr"),                "num"),
    ("issue.total_cr",              ("issue", "total_cr"),              "num"),
    ("issue.price_low",             ("issue", "price_low"),             "num"),
    ("issue.price_high",            ("issue", "price_high"),            "num"),
    ("issue.lot_size",              ("issue", "lot_size"),              "num"),
    ("issue.shares_post_issue_cr",  ("issue", "shares_post_issue_cr"),  "num"),
    ("issue.min_shni_qty",          ("issue", "min_shni_qty"),          "num"),
    ("issue.min_bhni_qty",          ("issue", "min_bhni_qty"),          "num"),
    ("issue.registrar",             ("issue", "registrar"),             "text"),
    ("issue.registrar_url",         ("issue", "registrar_url"),         "text"),
    ("issue.exchanges",             ("issue", "exchanges"),             "csv"),
    ("dates.announced",             ("dates", "announced"),             "date"),
    ("dates.open",                  ("dates", "open"),                  "date"),
    ("dates.close",                 ("dates", "close"),                 "date"),
    ("dates.close_time",            ("dates", "close_time"),            "text"),
    ("dates.allotment",             ("dates", "allotment"),             "date"),
    ("dates.refund",                ("dates", "refund"),                "date"),
    ("dates.listing",               ("dates", "listing"),               "date"),
    ("financials.eps",              ("financials", "eps"),              "num"),
    ("financials.pe_peer_avg",      ("financials", "pe_peer_avg"),      "num"),
    ("analysis.growth",             ("analysis", "growth"),             "text"),
    ("analysis.valuation",          ("analysis", "valuation"),          "text"),
    ("analysis.risk",               ("analysis", "risk"),               "text"),
    ("analysis.growth_tone",        ("analysis", "growth_tone"),        "text"),
    ("analysis.valuation_tone",     ("analysis", "valuation_tone"),     "text"),
    ("analysis.score",              ("analysis", "score"),              "num"),
    ("analysis.verdict",            ("analysis", "verdict"),            "text"),
    ("analysis.verdict_text",       ("analysis", "verdict_text"),       "text"),
    ("analysis.reco_retail",        ("analysis", "reco_retail"),        "text"),
    ("analysis.reco_hni",           ("analysis", "reco_hni"),           "text"),
    ("analysis.reco_long",          ("analysis", "reco_long"),          "text"),
    ("allotment.status",            ("allotment", "status"),            "text"),
    ("allotment.listing_low",       ("allotment", "listing_low"),       "num"),
    ("allotment.listing_high",      ("allotment", "listing_high"),      "num"),
    ("notes",                       ("notes",),                         "text"),
    # Reservation, appended for the same reason nii_small/nii_big were: _dicts
    # keys by header text so position carries no meaning, and appending leaves
    # every existing cell exactly where a human last saw it. Putting these
    # beside the other issue.* columns would read better and would visually
    # shuffle every row of a tab somebody edits by hand.
    ("issue.shares_qib",            ("issue", "shares_qib"),            "num"),
    ("issue.shares_nii",            ("issue", "shares_nii"),            "num"),
    ("issue.shares_retail",         ("issue", "shares_retail"),         "num"),
    ("issue.shares_employee",       ("issue", "shares_employee"),       "num"),
    ("issue.shares_total",          ("issue", "shares_total"),          "num"),
    ("issue.shares_shareholders",   ("issue", "shares_shareholders"),   "num"),
    ("issue.shares_anchor",         ("issue", "shares_anchor"),         "num"),
]

FIN_METRICS = ["revenue", "ebitda", "pat", "net_worth", "total_debt"]
FIN_COLS = ["slug", "year"] + FIN_METRICS
GMP_COLS = ["slug", "date", "gmp", "kostak", "sauda", "source"]
# nii_small / nii_big go on the end, not beside `nii`. _dicts keys by header
# text so position is free, and appending leaves every existing cell where a
# human last saw it — a mid-tab insert would visually shuffle 34 rows.
SUB_COLS = ["slug", "day", "date", "qib", "nii", "retail", "employee", "total",
            "nii_small", "nii_big"]
LIST_COLS = ["slug", "field", "idx", "value"]
I18N_COLS = ["slug", "lang", "key", "idx", "value"]
BENCH_COLS = ["slug", "metric", "value"]
# `value`, not `url`. The column has never held only URLs — `exchange` is a
# stamp like `NSE:TEMPSENS` — and since `facts` began writing the exchange's
# own identifiers (`nse_symbol`, `bse_code`, `isin`) most of what is in it is
# a ticker. A column called `url` holding MOMSBELIEF misleads the one audience
# this spreadsheet has: somebody reading it.
#
# `url` is still ACCEPTED on read, so a sheet written before this rename keeps
# working and the old book can go on running alongside the new one. Only the
# write side moved. Drop the fallback once no live sheet has a `url` header.
SRC_COLS = ["slug", "role", "value"]
SRC_COLS_LEGACY = ["slug", "role", "url"]

IPO_COLS = [col for col, _, _ in SCALARS]

# Tab name -> its header row. Order is the tab order when creating them.
TABS: dict[str, list[str]] = {
    "IPOs": IPO_COLS,
    "Financials": FIN_COLS,
    "GMP": GMP_COLS,
    "Subscription": SUB_COLS,
    "Lists": LIST_COLS,
    "I18n": I18N_COLS,
    "Benchmarks": BENCH_COLS,
    "Sources": SRC_COLS,
}

# ── the daily market briefing ──────────────────────────────────────────────
#
# Reel 7's record, and the first thing in this store that is not an IPO. It is
# keyed by DATE, not by slug, and it lives in its own tabs read and written by
# its own functions — `to_market_tables` / `from_market_tables` below, and
# `sheets.market_records` / `write_market_records`.
#
# Kept separate rather than folded into the eight tabs above, and the reason is
# concrete: `to_tables` and `from_tables` build exactly one root dict whose
# keys are slugs, and every series tab resolves its parent through that dict
# (a row whose slug is unknown is silently dropped). Filing a briefing under a
# pseudo-slug like `market-2026-09-02` would put a row in the IPOs tab that
# every consumer of `store.load_all()` — the dropdown, `readiness`, `doctor`,
# `dedupe` — then has to recognise and filter back out. A slug that lies about
# what it names is a worse cost than four more tabs.
#
# `write_market_records` clears only these tabs. That is load-bearing: the
# IPO writer already replaces all eight of its tabs on every save with no
# lock, and a second writer whose clear range overlapped the first would
# double the blast radius of a bug that already exists.

# One row per trading day: the outlook, the numbers behind it, the model and
# the disclosure. Everything scalar about the day.
MARKET_COLS = [
    "date", "trading", "why_closed", "at",
    "nifty", "nifty_pct", "nifty_prev", "banknifty", "banknifty_pct",
    "advances", "declines", "unchanged",
    "bias", "outlook", "levels_note", "model", "partial", "notes",
]

# The five overnight stories. `image` is a URL to a generated illustration,
# never a publisher's own photo — the reel is monetised and an RSS image is
# somebody's copyright. `window_from`/`window_to` stamp the 07:30-to-07:30 IST
# span the story was collected in, so a briefing can prove what it looked at.
MARKET_NEWS_COLS = ["date", "idx", "headline", "body", "why", "sector",
                    "tickers", "source", "url", "image", "at"]

# Every sectoral index for the day, strongest first. Stored rather than
# recomputed because the reel is recorded hours after the numbers were read,
# and `allIndices` will have moved on by then — the briefing has to be able to
# show the market it was actually written about.
MARKET_SECTOR_COLS = ["date", "sector", "pct", "last", "stance"]

# The intraday setups: five long, five short. Every level here is arithmetic
# on exchange data (`providers/market.levels`), never a model's number — see
# that module's header for why that line is drawn where it is.
MARKET_SETUP_COLS = ["date", "side", "rank", "symbol", "last", "entry",
                     "target", "stop", "pivot", "r1", "s1", "pct",
                     "close_pos", "reason", "invalidates"]

MARKET_TABS: dict[str, list[str]] = {
    "Market": MARKET_COLS,
    "MarketNews": MARKET_NEWS_COLS,
    "MarketSectors": MARKET_SECTOR_COLS,
    "MarketSetups": MARKET_SETUP_COLS,
}

# Every tab this store owns, for the two places that need a width or a
# creation list regardless of which record type a tab belongs to (`_span`
# and `ensure_tabs`). Deliberately NOT what `_fetch` or `to_tables` iterate —
# those stay on `TABS` so the IPO path is untouched by any of this.
ALL_TABS: dict[str, list[str]] = {**TABS, **MARKET_TABS}


# The list-valued fields, as paths into to_dict().
# Adding one here needs no column change and no matching edit in data.js: the
# Lists tab is keyed by field NAME, and the browser side splits that name on '.'
# and plants the array at that path. That is the whole cost of a new list field.
LIST_FIELDS = [
    ("analysis", "overview"),
    ("analysis", "green_flags"),
    ("analysis", "red_flags"),
    ("analysis", "about_facts"),
    ("analysis", "background"),
    ("allotment", "steps"),
]

# Sentinel idx on the I18n tab meaning "this key's value is an empty list",
# which is a different claim from the key being absent.
EMPTY_LIST = -1


# ── cell helpers ───────────────────────────────────────────────────────────

def _raw(cell: Any) -> str:
    """A cell's exact text, whitespace intact.

    For prose — notes and translated copy — where a trailing newline is part
    of the value a round-trip has to give back unchanged. Everything
    structural (slugs, keys, langs) goes through `_txt` instead.
    """
    if cell is None:
        return ""
    if isinstance(cell, (datetime, date)):
        return cell.isoformat()[:10]
    return str(cell)


def _txt(cell: Any) -> str:
    """A cell as trimmed text. Blank and the literal 'None' both mean empty."""
    s = _raw(cell).strip()
    return "" if s.lower() in ("none", "null", "nan") else s


def _blank(cell: Any) -> bool:
    return _txt(cell) == ""


def _date_cell(value: Any) -> Any:
    """Dates go in as ISO text.

    A real date cell carries the spreadsheet's own epoch and re-formats by
    locale, which silently shifted listing dates. Text is unambiguous, sorts
    correctly, and models._d parses it either way.
    """
    if value in (None, "", "null"):
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()[:10]
    return str(value)[:10]


def _num_cell(value: Any) -> Any:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return int(f) if f.is_integer() else f


def _opt_num_cell(value: Any) -> Any:
    """A number, or a blank when it is zero. For a field 0 cannot mean.

    This module's own docstring says a blank cell means absent and never 0,
    and the read path has always honoured it — `_blank` skips an empty cell so
    the model's default stands. The WRITE path did not, and the two together
    quietly defeated the rule: a field nobody supplied leaves `models.py` as
    `0.0`, `to_dict` emits `0.0`, and `_num_cell` writes a literal `0`.

    Measured on the live sheet, 2 Sep 2026: **not one blank numeric cell in
    the whole IPOs tab.** 25 of 28 rows asserted a peer P/E of exactly zero
    for a figure the project has no source for at all; 28 asserted a listing
    range of 0-0; 4 asserted a price band starting at ₹0. Every one of those
    reads as a fact to anyone opening the spreadsheet, and one of them —
    `issue.price_low` on Rays of Belief — is exactly what made a duplicate row
    look like it held data the original was missing.

    Blanking is round-trip lossless, which is what makes it safe: a blank
    reads back as the model's `0.0`, the identical in-memory value. Nothing
    downstream changes. What changes is that the sheet stops stating something
    nobody ever measured.

    NOT used for the series tabs, and that is the whole reason this is a
    separate function rather than a change to `_num_cell`. A GMP of 0 is a
    real quote (an issue priced at par — InvestorGain marks *unquoted* by
    omitting the day, not by sending a zero), and a subscription of 0 on day
    one is a real reading. Those keep their zeros. See
    `no-gemini-invented-numbers` for why that distinction is load-bearing.
    """
    if value in (None, ""):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f == 0:
        return None
    return int(f) if f.is_integer() else f


def _dig(d: dict, path: tuple[str, ...]) -> Any:
    for key in path:
        if not isinstance(d, dict):
            return None
        d = d.get(key)
    return d


def _plant(d: dict, path: tuple[str, ...], value: Any) -> None:
    for key in path[:-1]:
        d = d.setdefault(key, {})
    d[path[-1]] = value


def _dicts(rows: list[list], cols: list[str]) -> list[dict]:
    """Rows -> dicts keyed by the tab's own header row.

    Keyed by header text, not column position, so dragging a column sideways
    in the spreadsheet cannot silently swap two metrics.
    """
    if not rows:
        return []
    header = [_txt(c) for c in rows[0]]
    index = {name: header.index(name) for name in cols if name in header}
    out = []
    for row in rows[1:]:
        if all(_blank(c) for c in row):
            continue
        out.append({name: (row[i] if i < len(row) else None)
                    for name, i in index.items()})
    return out


# ── rows -> records ────────────────────────────────────────────────────────

def from_tables(tables: dict[str, list[list]]) -> dict[str, dict]:
    """{tab: rows-including-header} -> {slug: plain dict for Ipo.from_dict}."""
    out: dict[str, dict] = {}

    for row in _dicts(tables.get("IPOs") or [], IPO_COLS):
        slug = _txt(row.get("slug"))
        if not slug:
            continue
        rec: dict = {"slug": slug}
        for col, path, kind in SCALARS:
            if path == ("slug",):
                continue
            cell = row.get(col)
            if _blank(cell):
                continue                          # absent, not zero
            if kind == "csv":
                _plant(rec, path,
                       [p.strip() for p in _txt(cell).split(",") if p.strip()])
            elif kind == "text":
                _plant(rec, path, _raw(cell))     # prose keeps its whitespace
            else:
                # num and date land as text; models.from_dict already coerces
                # via _f / _d and is the single place that decides what a
                # malformed value degrades to.
                _plant(rec, path, _txt(cell))
        out[slug] = rec

    def at(slug: Any) -> dict | None:
        return out.get(_txt(slug))

    # Financials: long rows back into parallel arrays, column by column.
    grouped: dict[str, list[dict]] = {}
    for row in _dicts(tables.get("Financials") or [], FIN_COLS):
        slug = _txt(row.get("slug"))
        if slug:
            grouped.setdefault(slug, []).append(row)
    for slug, rows in grouped.items():
        rec = at(slug)
        if rec is None:
            continue
        fin = rec.setdefault("financials", {})
        fin["years"] = [y for y in (_txt(r.get("year")) for r in rows) if y]
        for metric in FIN_METRICS:
            fin[metric] = [float(_txt(r.get(metric))) for r in rows
                           if not _blank(r.get(metric))]

    for row in _dicts(tables.get("GMP") or [], GMP_COLS):
        rec = at(row.get("slug"))
        if rec is None:
            continue
        rec.setdefault("gmp_history", []).append({
            "date": _txt(row.get("date")),
            "gmp": _txt(row.get("gmp")),
            "kostak": _txt(row.get("kostak")),
            "sauda": _txt(row.get("sauda")),
            "source": _txt(row.get("source")) or "manual",
        })

    for row in _dicts(tables.get("Subscription") or [], SUB_COLS):
        rec = at(row.get("slug"))
        if rec is None:
            continue
        rec.setdefault("subscription", []).append({
            k: _txt(row.get(k))
            for k in ("day", "date", "qib", "nii", "retail", "employee", "total",
                      "nii_small", "nii_big")
        })

    staged: dict[tuple[str, str], list[tuple[int, str]]] = {}
    for row in _dicts(tables.get("Lists") or [], LIST_COLS):
        slug, field = _txt(row.get("slug")), _txt(row.get("field"))
        value = _raw(row.get("value"))
        if not slug or not field or not value.strip():
            continue
        try:
            idx = int(float(_txt(row.get("idx")) or 0))
        except ValueError:
            idx = 0
        staged.setdefault((slug, field), []).append((idx, value))
    for (slug, field), items in staged.items():
        rec = at(slug)
        if rec is None:
            continue
        items.sort(key=lambda p: p[0])
        _plant(rec, tuple(field.split(".")), [v for _, v in items])

    tongues: dict[tuple[str, str, str], list[tuple[Any, str]]] = {}
    for row in _dicts(tables.get("I18n") or [], I18N_COLS):
        slug, lang = _txt(row.get("slug")), _txt(row.get("lang"))
        key, value = _txt(row.get("key")), _raw(row.get("value"))
        if not (slug and lang and key):
            continue
        raw_idx = _txt(row.get("idx"))
        try:
            idx: Any = None if raw_idx == "" else int(float(raw_idx))
        except ValueError:
            idx = None
        # A row is kept because the key is there, not because the value is
        # non-empty: "" and [] are both real translation states, and neither
        # means the same as the key being absent.
        tongues.setdefault((slug, lang, key), []).append((idx, value))
    for (slug, lang, key), items in tongues.items():
        rec = at(slug)
        if rec is None:
            continue
        block = rec.setdefault("i18n", {}).setdefault(lang, {})
        if any(i == EMPTY_LIST for i, _ in items):
            block[key] = []
        elif len(items) == 1 and items[0][0] is None:
            block[key] = items[0][1]              # blank idx = a plain string
        else:
            items.sort(key=lambda p: (p[0] is None, p[0]))
            block[key] = [v for _, v in items]

    for row in _dicts(tables.get("Benchmarks") or [], BENCH_COLS):
        rec = at(row.get("slug"))
        metric, value = _txt(row.get("metric")), _txt(row.get("value"))
        if rec is not None and metric and value:
            rec.setdefault("benchmarks", {})[metric] = value

    # Both spellings of the third column. `_dicts` keys by header text, so a
    # tab still headed `url` yields no `value` key and every source would be
    # silently dropped — which on a sheet where `sources.logo` drives reel 1's
    # image and `nse_symbol` drives the duplicate check is a quiet, total loss
    # rather than an error anybody would see.
    for row in _dicts(tables.get("Sources") or [],
                      SRC_COLS + ["url"]):
        rec = at(row.get("slug"))
        role = _txt(row.get("role"))
        value = _txt(row.get("value")) or _txt(row.get("url"))
        if rec is not None and role and value:
            rec.setdefault("sources", {})[role] = value

    return out


# ── records -> rows ────────────────────────────────────────────────────────

def to_tables(records: dict[str, dict]) -> dict[str, list[list]]:
    """{slug: dict} -> {tab: rows-including-header}, slugs in sorted order."""
    order = sorted(records)
    tables: dict[str, list[list]] = {name: [list(cols)]
                                     for name, cols in TABS.items()}

    for slug in order:
        d = records[slug]
        line = []
        for _, path, kind in SCALARS:
            value = _dig(d, path)
            if kind == "num":
                # _opt_num_cell, not _num_cell: a zero here means nobody
                # supplied the figure. See that function for the measurement.
                line.append(_opt_num_cell(value))
            elif kind == "csv":
                line.append(", ".join(str(v) for v in (value or [])) or None)
            elif kind == "date":
                line.append(_date_cell(value))
            else:
                line.append(str(value) if value not in (None, "") else None)
        tables["IPOs"].append(line)

        fin = d.get("financials") or {}
        series = {m: list(fin.get(m) or []) for m in FIN_METRICS}
        years = list(fin.get("years") or [])
        depth = max([len(years)] + [len(v) for v in series.values()])
        for i in range(depth):
            tables["Financials"].append(
                [slug, years[i] if i < len(years) else None]
                # `_num_cell`, NOT `_opt_num_cell`, even though this tab is
                # the case the module docstring names ("writing 0.0 into an
                # empty revenue year invents a company with no revenue").
                #
                # These are PARALLEL ARRAYS and position is the only thing
                # binding a value to its year. Blanking a zero drops the
                # element on the way back in: Rays of Belief's total_debt
                # [0.0, 4.36, 3.61] came back as [4.36, 3.61], which re-labels
                # FY25's debt as FY24's — the same silent year-shift the
                # `fresh_axis` guard in `cmd_facts` exists to prevent, arrived
                # at from the other direction.
                #
                # And a zero here is usually real anyway: a company with no
                # borrowings has a total_debt of exactly 0, and Shanti
                # Inorganics' latest year is one. Absence in a series is
                # expressed by the series being SHORTER or absent entirely,
                # never by a hole in the middle of it.
                + [_num_cell(series[m][i]) if i < len(series[m]) else None
                   for m in FIN_METRICS])

        for point in (d.get("gmp_history") or []):
            tables["GMP"].append([
                slug, _date_cell(point.get("date")), _num_cell(point.get("gmp")),
                _num_cell(point.get("kostak")), _num_cell(point.get("sauda")),
                point.get("source") or "manual"])

        for day in (d.get("subscription") or []):
            tables["Subscription"].append(
                [slug, _num_cell(day.get("day")), _date_cell(day.get("date"))]
                + [_num_cell(day.get(k))
                   for k in ("qib", "nii", "retail", "employee", "total",
                             "nii_small", "nii_big")])

        for field_path in LIST_FIELDS:
            for i, value in enumerate(_dig(d, field_path) or []):
                tables["Lists"].append([slug, ".".join(field_path), i, str(value)])

        for lang, block in sorted((d.get("i18n") or {}).items()):
            for key, value in (block or {}).items():
                if isinstance(value, list):
                    if not value:
                        tables["I18n"].append([slug, lang, key, EMPTY_LIST, None])
                        continue
                    for i, item in enumerate(value):
                        tables["I18n"].append([slug, lang, key, i, str(item)])
                elif value is not None:
                    # Blank idx marks a scalar, not a one-element list — the
                    # read side keys off exactly that.
                    tables["I18n"].append([slug, lang, key, None, str(value) or None])

        for metric, value in sorted((d.get("benchmarks") or {}).items()):
            # A benchmark override of 0 is not a threshold anybody set; it
            # is an override that was never filled in, and compute.py would
            # judge every company as meeting it.
            tables["Benchmarks"].append([slug, metric, _opt_num_cell(value)])

        for role, url in sorted((d.get("sources") or {}).items()):
            tables["Sources"].append([slug, role, str(url)])

    return tables


# ── the market briefing round-trip ─────────────────────────────────────────
#
# The same shape as the IPO pair above and the same rules — blank means
# absent, dates go in as text, prose keeps its whitespace — but keyed by date
# and reading only the four Market* tabs. Two independent functions rather
# than a branch inside the IPO pair, because those two build one root dict
# whose keys are slugs and every series tab in them resolves its parent
# through that dict.

def from_market_tables(tabs: dict[str, list[list]]) -> dict[str, dict]:
    """{tab: rows-including-header} -> {date: plain dict for Briefing}."""
    out: dict[str, dict] = {}

    for row in _dicts(tabs.get("Market") or [], MARKET_COLS):
        day = _txt(row.get("date"))
        if not day:
            continue
        rec: dict[str, Any] = {"date": day}
        for col in MARKET_COLS:
            if col == "date":
                continue
            cell = row.get(col)
            if _blank(cell):
                continue                  # absent, not zero and not False
            # `outlook`, `levels_note` and `notes` are prose the model wrote
            # and a human may have edited; the rest is structural.
            rec[col] = _raw(cell) if col in ("outlook", "levels_note", "notes") \
                else _txt(cell)
        out[day] = rec

    def at(day: Any) -> dict | None:
        return out.get(_txt(day))

    for row in _dicts(tabs.get("MarketNews") or [], MARKET_NEWS_COLS):
        rec = at(row.get("date"))
        if rec is None:
            continue
        rec.setdefault("news", []).append({
            "idx": _txt(row.get("idx")),
            # The story itself is prose and round-trips exactly. A headline
            # with a trailing space is a headline somebody typed.
            "headline": _raw(row.get("headline")),
            "body": _raw(row.get("body")),
            "why": _raw(row.get("why")),
            "sector": _txt(row.get("sector")),
            "tickers": [t.strip() for t in _txt(row.get("tickers")).split(",")
                        if t.strip()],
            "source": _txt(row.get("source")),
            "url": _txt(row.get("url")),
            "image": _txt(row.get("image")),
            "at": _txt(row.get("at")),
        })

    for row in _dicts(tabs.get("MarketSectors") or [], MARKET_SECTOR_COLS):
        rec = at(row.get("date"))
        if rec is None:
            continue
        rec.setdefault("sectors", []).append({
            "sector": _txt(row.get("sector")),
            "pct": _txt(row.get("pct")),
            "last": _txt(row.get("last")),
            "stance": _txt(row.get("stance")),
        })

    for row in _dicts(tabs.get("MarketSetups") or [], MARKET_SETUP_COLS):
        rec = at(row.get("date"))
        if rec is None:
            continue
        rec.setdefault("setups", []).append({
            "side": _txt(row.get("side")),
            "rank": _txt(row.get("rank")),
            "symbol": _txt(row.get("symbol")),
            "last": _txt(row.get("last")),
            "entry": _txt(row.get("entry")),
            "target": _txt(row.get("target")),
            "stop": _txt(row.get("stop")),
            "pivot": _txt(row.get("pivot")),
            "r1": _txt(row.get("r1")),
            "s1": _txt(row.get("s1")),
            "pct": _txt(row.get("pct")),
            "close_pos": _txt(row.get("close_pos")),
            "reason": _raw(row.get("reason")),
            "invalidates": _raw(row.get("invalidates")),
        })

    return out


def to_market_tables(records: dict[str, dict]) -> dict[str, list[list]]:
    """{date: dict} -> {tab: rows-including-header}, oldest day first.

    Ascending by date on purpose: this tab grows by one briefing every trading
    day and is meant to be scrolled by a human. Newest-first would put the
    header next to the oldest row and move every row down every morning.
    """
    order = sorted(records)
    tabs: dict[str, list[list]] = {name: [list(cols)]
                                   for name, cols in MARKET_TABS.items()}

    for day in order:
        d = records[day] or {}
        tabs["Market"].append([
            _date_cell(day),
            *[_cell_for(col, d.get(col)) for col in MARKET_COLS[1:]],
        ])

        for i, item in enumerate(d.get("news") or [], 1):
            tickers = item.get("tickers")
            tabs["MarketNews"].append([
                _date_cell(day), _num_cell(item.get("idx") or i),
                item.get("headline") or None, item.get("body") or None,
                item.get("why") or None, item.get("sector") or None,
                ",".join(tickers) if isinstance(tickers, list)
                else (tickers or None),
                item.get("source") or None, item.get("url") or None,
                item.get("image") or None, item.get("at") or None,
            ])

        for row in d.get("sectors") or []:
            tabs["MarketSectors"].append([
                _date_cell(day), row.get("sector") or None,
                _num_cell(row.get("pct")), _num_cell(row.get("last")),
                row.get("stance") or None,
            ])

        for row in d.get("setups") or []:
            tabs["MarketSetups"].append([
                _date_cell(day), row.get("side") or None,
                _num_cell(row.get("rank")), row.get("symbol") or None,
                _num_cell(row.get("last")), _num_cell(row.get("entry")),
                _num_cell(row.get("target")), _num_cell(row.get("stop")),
                _num_cell(row.get("pivot")), _num_cell(row.get("r1")),
                _num_cell(row.get("s1")), _num_cell(row.get("pct")),
                _num_cell(row.get("close_pos")),
                row.get("reason") or None, row.get("invalidates") or None,
            ])

    return tabs


# Which columns on the Market tab are numbers and which are text. Spelled out
# rather than inferred, because `trading` is a boolean and `advances` is a
# count, and `_num_cell(True)` is 1 — a briefing that says the market was
# open on day 1 is not what anybody meant.
_MARKET_NUM = {"nifty", "nifty_pct", "nifty_prev", "banknifty", "banknifty_pct",
               "advances", "declines", "unchanged"}


def _cell_for(col: str, value: Any) -> Any:
    if col == "trading":
        # Written as the words, not TRUE/FALSE: Sheets renders a real boolean
        # as a checkbox in some locales and `_txt` on that gives 'TRUE' in
        # others. One spelling, ours, round-trips the same everywhere.
        return None if value in (None, "") else ("yes" if value else "no")
    if col in _MARKET_NUM:
        return _num_cell(value)
    return value or None
