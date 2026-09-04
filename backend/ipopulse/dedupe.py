"""One offer, one row. What decides that, and how to repair it when it broke.

The sheet has carried the same company twice three times now — `skyways-air`
against `skyways-air-services`, `purple-style-labs` against
`pernia-s-pop-up-studio`, and `rays-of-belief` against
`rays-of-belief-limited-for-profit-social-enterpr`. Every one of them cost the
same three things: a second entry in the dropdown that reads as a display bug,
two half-filled records where one complete one should be, and an enrich budget
spent twice on one company.

Every one of them also had the same shape of cause. Discovery asks the name
matcher "is this one of ours?", the matcher says no, and **nothing else is
asked** before a new row is scaffolded. The matcher is a string comparison and
string comparisons have a false-negative rate; the bug was never the matcher
being wrong, it was the matcher being the only question.

So this module is the second question, and it is deliberately the *only* place
that answers it. Three callers share it:

    monitor.duplicates()      the watchdog, reporting a pair already stored
    cli discovery doors       refusing to create the second row at all
    ipopulse merge / dedupe   folding a pair that got in back into one

Three copies of "same company" is how this happened in the first place — the
matcher said no, `monitor` said yes, and the two never spoke. There is now one
definition and `same_offer` is it.

── Why names alone are not enough ─────────────────────────────────────────

Name matching catches `skyways-air` / `skyways-air-services`, where one is a
prefix of the other, and `rays-of-belief`, where one name is the other plus
its legal suffix. It cannot catch a company filed under its BRAND: the sheet
carried both `purple-style-labs` and `pernia-s-pop-up-studio` as separate
mainboard issues and they share not one character. Same open, close and
listing dates, same ₹680 Cr, same lot, the same logo file, and NSE's own
symbol for Purple Style Labs is PERNIASPOP. No string comparison of those two
names will ever agree.

Hence two signals that come from upstream rather than from spelling:

  * **the logo URL** — the desk publishes one image per offer, so two rows
    pointing at the same file are two rows for the same offer. Strongest of
    the three and the cheapest.
  * **the calendar plus the size** — identical open, close AND total. Two
    genuinely different issues can share a bidding window; sharing the window
    and the rupee size is what makes it a finding.

── certain vs likely ──────────────────────────────────────────────────────

`same_offer` grades rather than returning a bool, because the two callers have
opposite cost asymmetries. For the watchdog a false positive is a line of
output; for the discovery door a false positive means an IPO never gets
tracked. A bare name prefix is the one signal with a real false-positive mode
(Reliance Power and Reliance Powergrid would collide), so it grades `likely`
on its own and `certain` once the calendar or the issue size corroborates it.

The discovery door still refuses on `likely` — a duplicate row poisons the
dropdown, the reels and the AI budget, whereas a wrongly-refused discovery
prints the slug it was folded into and can be overridden with
`--allow-duplicate`. Visible and recoverable beats silent and not.

Read-only apart from `apply()`, which is the only function here that writes.
"""

from __future__ import annotations

from typing import Any

from .models import Ipo
from .providers.base import merge, merge_series

# Below this, an alnum-squashed name is too short for the prefix rule to mean
# anything — a three-letter stem is a prefix of half the market. Every real
# collision found so far cleared this comfortably ('raysofbelief' is 12,
# 'skywaysair' is 10).
MIN_PREFIX_LEN = 8

# How much a GMP row's source is trusted when two rows claim the same date.
# InvestorGain is the desk this channel quotes and the only source that
# distinguishes quoted-at-par from unquoted; a model's number is a last
# resort and its zero is not a number at all. See the rule in `_series`.
SOURCE_RANK = {"investorgain": 4, "ipoji": 3, "nse": 3, "bse": 3,
               "manual": 5, "": 1, "gemini": 0}


def _name_key(name: str) -> str:
    """A name squashed to letters and digits.

    Punctuation and spacing are exactly what differs between the three
    catalogues — 'Rays of Belief Limited- For Profit Social Enterprise' from
    NSE against 'Rays of Belief' from InvestorGain — so neither survives into
    the comparison.
    """
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


def signature(rec: Any) -> dict[str, Any]:
    """The identifying marks of one offer, from an `Ipo` or a raw board row.

    The comparison has to work *before* a row exists — that is the whole
    point of the discovery door — so a stored record and a catalogue row both
    normalise to this shape first. Anything missing is simply absent; a signal
    with no data on one side never fires rather than firing on two blanks.
    """
    if isinstance(rec, Ipo):
        src = rec.sources or {}
        return {
            "slug": rec.slug,
            "name": rec.company or rec.slug,
            "logo": (src.get("logo") or "").strip().lower(),
            # The exchange's own identifiers, written by `facts`. These are
            # checked before anything else in `same_offer` — see the note
            # there about why a ticker beats every string signal.
            "nse_symbol": (src.get("nse_symbol") or "").strip().upper(),
            "bse_code": (src.get("bse_code") or "").strip().upper(),
            "isin": (src.get("isin") or "").strip().upper(),
            "open": str(rec.dates.open or ""),
            "close": str(rec.dates.close or ""),
            "total": round(float(rec.issue.total_cr or 0), 2),
        }
    # A discovery row: InvestorGain's `board()`/`catalogue()` shape, which
    # ipoji matches field for field, or the `fetch_catalogue()` dict that
    # `sync --discover` walks. Accept both spellings of each fact.
    d = rec or {}
    dates = d.get("dates") or {}
    issue = d.get("issue") or {}
    # A discovery row carries no ticker of its own — InvestorGain's board does
    # not publish one — so a caller that has fetched `investorgain.identity()`
    # merges it into the row before asking. `collides` does exactly that.
    src = d.get("sources") or {}
    return {
        "slug": d.get("slug") or d.get("slug_ig") or "",
        "name": d.get("name") or d.get("company") or "",
        "logo": str(d.get("logo") or "").strip().lower(),
        "nse_symbol": str(d.get("nse_symbol")
                          or src.get("nse_symbol") or "").strip().upper(),
        "bse_code": str(d.get("bse_code")
                        or src.get("bse_code") or "").strip().upper(),
        "isin": str(d.get("isin") or src.get("isin") or "").strip().upper(),
        "open": str(d.get("open") or dates.get("open") or ""),
        "close": str(d.get("close") or dates.get("close") or ""),
        "total": round(float(d.get("issue_size_cr")
                             or issue.get("total_cr") or 0), 2),
    }


def same_offer(a: dict[str, Any], b: dict[str, Any]) -> tuple[str, str]:
    """Are these two the same offer? ('certain'|'likely'|'', why).

    Takes two `signature()` dicts. Returns the strongest signal that fired and
    the sentence a report can print, so nothing has to assert a duplicate and
    leave somebody to work out the basis.
    """
    # ── the exchange's own identifier for the issue
    #
    # First, and it should always have been first. An IPO is identified by a
    # ticker, not by a name: MOMSBELIEF is Rays of Belief and nothing else,
    # PERNIASPOP is Purple Style Labs however the row spells the company. The
    # signals below are all attempts to infer identity from things that
    # merely correlate with it — spelling, an image filename, a calendar —
    # and every duplicate this project has had would have been caught here
    # instead, exactly, with no threshold to tune.
    #
    # ISIN is the strongest of the three (one security, one ISIN, both
    # exchanges publish it) but fills in only as an issue nears listing, so
    # the symbol carries the pre-listing case and that is the case discovery
    # actually runs in.
    #
    # `facts` writes these; a row that has never had `facts` run against it
    # has none of them and falls through to the string signals unchanged.
    for field, why in (("isin", "the same ISIN"),
                       ("nse_symbol", "the same NSE symbol"),
                       ("bse_code", "the same BSE scrip code")):
        if a.get(field) and a[field] == b.get(field):
            return "certain", f"{why} ({a[field]})"

    # ── and a ticker that DISAGREES settles it the other way
    #
    # The signals below infer identity from things that correlate with it, and
    # the name-prefix rule has a real false-positive mode: Reliance Power and
    # Reliance Powergrid would collide on it. Two rows the exchange gives
    # different symbols are two different companies, whatever their names
    # share — so a known-and-different identifier stops the comparison here
    # rather than letting a weaker signal overrule the exchange.
    #
    # Only when BOTH sides have one. An absent symbol is not a disagreement,
    # and most upcoming issues have none until the desk publishes a detail
    # record.
    for field in ("isin", "nse_symbol", "bse_code"):
        if a.get(field) and b.get(field) and a[field] != b[field]:
            return "", ""

    # ── the logo the desk publishes for the offer
    if a["logo"] and a["logo"] == b["logo"]:
        return "certain", "the same logo file upstream"

    ka, kb = _name_key(a["name"]), _name_key(b["name"])
    same_window = bool(a["open"]) and a["open"] == b["open"] \
        and bool(a["close"]) and a["close"] == b["close"]
    same_size = bool(a["total"]) and a["total"] == b["total"]

    # ── the name, exactly
    if ka and ka == kb:
        return "certain", "the same name"

    # ── the name, one a prefix of the other
    #
    # This is the shape the matcher gets wrong, and the only signal here with
    # a real false-positive mode, so on its own it grades `likely`. The
    # calendar or the issue size agreeing as well removes the doubt: two
    # different companies whose names share a stem do not also open and close
    # on the same days for the same amount of money.
    if ka and kb and min(len(ka), len(kb)) >= MIN_PREFIX_LEN \
            and (ka.startswith(kb) or kb.startswith(ka)):
        if same_window or same_size:
            return "certain", ("one name is the other plus a suffix, and they "
                               "share the bidding window or the issue size")
        if not (bool(a["logo"]) and bool(b["logo"])
                and a["logo"] != b["logo"]):
            return "likely", "one name is the other plus a suffix"

    # ── a DIFFERING logo vetoes the weak signals
    #
    # The desk publishes one image per offer, which is what makes a shared
    # logo `certain` above. The converse carries nearly as much weight: two
    # rows pointing at different images are two offers, whatever else they
    # happen to share.
    #
    # Needed because the calendar signal has a real false-positive mode. `nse`
    # and `qualiance-international` both opened 4 Sep, both closed 8 Sep, and
    # both carried a total of ₹138.11 Cr — so they matched, and they are the
    # National Stock Exchange and a small SME textile issue. Their logos are
    # `nse-logo.png` and `qualiance-ipo-logo.jpg`.
    #
    # Applied only to the two `likely` signals below, never to an exact name
    # or a shared ticker: a company can change its logo, so a mismatch there
    # is weaker evidence than the identifier that already agreed.
    logos_differ = bool(a["logo"]) and bool(b["logo"]) and a["logo"] != b["logo"]

    # ── the calendar plus the size
    if same_window and same_size and not logos_differ:
        return "likely", "identical open, close and issue size"

    return "", ""


def groups(ipos: list[Ipo]) -> list[dict[str, Any]]:
    """Every set of stored rows that describe one offer.

    Pairwise rather than bucketed by key, because the signals disagree about
    what a key even is — a logo groups two rows a name never would. Overlapping
    pairs are then unioned so a three-row pile-up reports once, carrying every
    reason that found it, instead of three times under three headings.
    """
    sigs = {i.slug: signature(i) for i in ipos}
    slugs = sorted(sigs)

    # Union-find over the pairs. `parent` maps a slug to its group's
    # representative; `reasons` accumulates the why for each pair found.
    parent = {s: s for s in slugs}

    def root(s: str) -> str:
        while parent[s] != s:
            parent[s] = parent[parent[s]]
            s = parent[s]
        return s

    found: dict[frozenset[str], tuple[str, str]] = {}
    for n, left in enumerate(slugs):
        for right in slugs[n + 1:]:
            confidence, why = same_offer(sigs[left], sigs[right])
            if not confidence:
                continue
            found[frozenset((left, right))] = (confidence, why)
            parent[root(left)] = root(right)

    out: dict[str, dict[str, Any]] = {}
    for pair, (confidence, why) in found.items():
        key = root(next(iter(pair)))
        entry = out.setdefault(key, {"slugs": set(), "why": set(),
                                     "confidence": "likely"})
        entry["slugs"] |= set(pair)
        entry["why"].add(why)
        if confidence == "certain":
            entry["confidence"] = "certain"

    return [{"company": sorted(e["slugs"])[0],
             "slugs": sorted(e["slugs"]),
             "confidence": e["confidence"],
             "why": " and ".join(sorted(e["why"]))}
            for e in out.values()]


def collides(candidate: Any, ipos: list[Ipo]) -> dict[str, Any] | None:
    """Would tracking `candidate` create a second row for a stored offer?

    The question the discovery doors ask before they scaffold. `candidate` is
    a raw catalogue row; `ipos` is everything already stored. Returns the
    strongest collision found, or None when this really is a new offer.

    Checked against every stored row rather than stopping at the first hit,
    and the result carries the confidence, so a caller can print which
    existing slug the row was folded into instead of just declining.
    """
    sig = signature(candidate)
    if not (sig["name"] or sig["logo"]):
        return None                     # nothing to compare on; not our call

    # Ask the desk for this row's ticker before comparing any string.
    #
    # One extra request, and only for a row that is about to become a new
    # entry in the store — which is a handful per run at most, against an
    # enrich budget of six model calls for every row wrongly created. The
    # board itself carries no symbol, so without this the strongest signal in
    # `same_offer` is simply blank on the one side that matters.
    #
    # Best-effort: an unreachable desk leaves the string signals to decide,
    # which is what they did before this existed.
    if not sig["nse_symbol"]:
        try:
            from .providers import investorgain as _ig
            sig.update({k: v.upper() for k, v in
                        (_ig.identity(candidate) or {}).items()})
        except Exception:
            pass
    best: dict[str, Any] | None = None
    for ipo in ipos:
        confidence, why = same_offer(sig, signature(ipo))
        if not confidence:
            continue
        hit = {"slug": ipo.slug, "company": ipo.company or ipo.slug,
               "confidence": confidence, "why": why}
        if confidence == "certain":
            return hit                  # cannot do better than certain
        best = best or hit
    return best


# ── repair ─────────────────────────────────────────────────────────────────

def completeness(ipo: Ipo) -> int:
    """A rough count of what a row actually knows, for picking the keeper.

    Not a quality score and not comparable between companies — the only thing
    it is for is answering "of these two rows for one offer, which one has
    more in it". Series count their rows because a GMP trail is the most
    expensive thing on a record to rebuild: it is a *history*, and a day not
    captured while the issue was live cannot be fetched back later.
    """
    d = ipo.to_dict()
    score = 0

    def walk(val: Any) -> int:
        if isinstance(val, dict):
            return sum(walk(v) for v in val.values())
        if isinstance(val, list):
            return len(val)
        # 0 and "" both mean absent here — the sheet's rule, and the reason a
        # row of zeros must not outrank a row that left them blank.
        return 1 if val not in (None, "", 0, 0.0, False) else 0

    for key, val in d.items():
        if key in ("slug", "i18n"):
            continue
        score += walk(val)
    # The GMP trail and the subscription trail are worth more than a scalar
    # each, for the reason in the docstring.
    score += 2 * len(d.get("gmp_history") or [])
    score += 2 * len(d.get("subscription") or [])
    return score


def _series(keep: list[dict], drop: list[dict], key: str) -> list[dict]:
    """Union two trails, the better-sourced row winning a shared key.

    `merge_series` lets *incoming* win a collision, so the keeper is passed as
    incoming — but only after the two are ranked, because "keeper" is about
    which row is more complete overall and says nothing about which of two
    readings for one day is the real one.

    Two rules from the house rule that a model may not invent a number:

      * a model-sourced reading never displaces one from a desk, whatever
        side of the merge it is on;
      * a model-sourced GMP of exactly 0 is dropped outright. A real zero
        exists — an issue collapsing to par — but those come from
        InvestorGain, which marks unquoted by omitting the day. A model asked
        the same question has no way to express an absence and returns 0.
    """
    def rank(row: dict) -> int:
        return SOURCE_RANK.get(str(row.get("source") or "").lower(), 1)

    def usable(row: dict) -> bool:
        if str(row.get("source") or "").lower() != "gemini":
            return True
        return bool(row.get("gmp"))     # refuse the model's zero, keep the rest

    pool: dict[str, dict] = {}
    for row in list(drop or []) + list(keep or []):
        if row.get(key) is None or not usable(row):
            continue
        k = str(row[key])
        if k not in pool or rank(row) >= rank(pool[k]):
            pool[k] = dict(row)
    # Back through merge_series so the ordering and key handling stay that
    # one function's business rather than being re-implemented here.
    return merge_series([], [pool[k] for k in sorted(pool)], key)


def plan(keep: Ipo, drop: Ipo) -> dict[str, Any]:
    """What folding `drop` into `keep` would do. Writes nothing.

    Field precedence is `merge()`'s, unchanged: the keeper wins every conflict
    and only its genuinely empty fields get filled. That is the same rule the
    whole pipeline uses for a fetched value against a stored one, and it is
    the right one here too — the row with more in it is the row to trust.

    The two trails and the slug are the exceptions, and both for the same
    reason: `merge()` treats a list as one atomic value, so the keeper's
    eight GMP days would silently discard the other row's six rather than
    union with them.
    """
    a, b = keep.to_dict(), drop.to_dict()
    merged = merge(a, b, prefer_incoming=False)

    # Never inherit the dropped row's identity. A merge that renamed the
    # keeper would break every reference to it — the frontend's dropdown, the
    # audio filenames, the attempt log, the monitor's fingerprint.
    merged["slug"] = keep.slug

    merged["gmp_history"] = _series(a.get("gmp_history") or [],
                                    b.get("gmp_history") or [], "date")
    # Subscription keys on `day`, not `date`: the exchange reports a running
    # total for the whole window, so two rows for day 2 are one reading twice,
    # not two readings.
    merged["subscription"] = _series(a.get("subscription") or [],
                                     b.get("subscription") or [], "day")

    # i18n merges per language, not atomically. A keeper translated before its
    # background was written has complete-looking hi/te that are missing a
    # whole scene, and the dropped row may hold exactly that scene — this is
    # the case that created `skyways-air`, which had no lot size but did have
    # the translations.
    langs = dict(a.get("i18n") or {})
    for lang, block in (b.get("i18n") or {}).items():
        langs[lang] = merge(langs.get(lang) or {}, block or {},
                            prefer_incoming=False)
    merged["i18n"] = langs

    # What a dry run prints. Only the fields the fold would actually change,
    # so a plan reads as a diff rather than as the whole record twice.
    changes: list[dict[str, Any]] = []

    def diff(path: str, before: Any, after: Any) -> None:
        if isinstance(after, dict):
            for k, v in after.items():
                diff(f"{path}.{k}" if path else k,
                     (before or {}).get(k) if isinstance(before, dict) else None,
                     v)
            return
        if isinstance(after, list):
            if len(after) != len(before or []):
                changes.append({"field": path,
                                "from": f"{len(before or [])} rows",
                                "to": f"{len(after)} rows"})
            return
        if (before in (None, "", 0, 0.0) and after not in (None, "", 0, 0.0)) \
                or (before != after and path != "slug"):
            changes.append({"field": path, "from": before, "to": after})

    for key, val in merged.items():
        if key == "i18n":
            continue
        diff(key, a.get(key), val)

    # Where the two rows hold different numbers for the same day. `_series`
    # resolves these by source rank and says nothing, which is right for a
    # model's guess losing to a desk's quote — but two desks disagreeing about
    # a *cumulative* subscription total is a finding, not a tie to break. NSE
    # and InvestorGain have disagreed before (Lalithaa, 19 Aug: day 3 = 3.07x
    # against a verified day 2 = 3.25x, and a running total cannot fall), and
    # that is the whole reason NSE's subscription is not imported. A merge is
    # the one moment both readings exist side by side, so it is the moment to
    # print them rather than the moment to quietly halve them.
    def conflicts(left: list[dict], right: list[dict],
                  key: str, field: str) -> list[dict[str, Any]]:
        by_key = {str(r[key]): r for r in (right or []) if r.get(key) is not None}
        out = []
        for row in left or []:
            other = by_key.get(str(row.get(key)))
            if not other:
                continue
            if row.get(field) != other.get(field) \
                    and row.get(field) is not None \
                    and other.get(field) is not None:
                out.append({
                    "at": f"{key} {row[key]}",
                    "kept": f"{row[field]} ({row.get('source') or 'untagged'})",
                    "dropped": f"{other[field]} "
                               f"({other.get('source') or 'untagged'})",
                })
        return out

    return {
        "keep": keep.slug,
        "drop": drop.slug,
        "record": merged,
        "changes": changes,
        "conflicts": (
            conflicts(a.get("gmp_history") or [], b.get("gmp_history") or [],
                      "date", "gmp")
            + conflicts(a.get("subscription") or [], b.get("subscription") or [],
                        "day", "total")),
        # Named separately because it is the one thing a merge cannot undo and
        # the one thing worth reading twice before saying yes.
        "losing": {
            "company": drop.company,
            "gmp_days": len(b.get("gmp_history") or []),
            "sub_days": len(b.get("subscription") or []),
            "exchange": (b.get("sources") or {}).get("exchange", ""),
        },
    }


def choose(ipos: list[Ipo]) -> tuple[Ipo, list[Ipo]]:
    """Which row of a duplicate group to keep, and which to fold into it.

    Completeness first — the row that knows more is the row to build on.
    Then the shorter slug, which is not cosmetic: the long ones are the ones
    NSE's legal names produced ('rays-of-belief-limited-for-profit-social-
    enterpr', truncated mid-word at the slug's 48-character limit), and a slug
    is what a filename, a URL and a dropdown label are made of.
    """
    ranked = sorted(ipos, key=lambda i: (-completeness(i), len(i.slug), i.slug))
    return ranked[0], ranked[1:]


def apply(p: dict[str, Any]) -> None:
    """Write the plan: save the merged keeper, then drop the other row.

    In that order, and it matters. Saving first means a crash between the two
    leaves a complete keeper and a stale duplicate — recoverable, and the
    watchdog will say so. Dropping first would leave a window where the only
    copy of the dropped row's data exists in memory.
    """
    from . import store
    store.save(Ipo.from_dict(p["record"]))
    store.remove(p["drop"])
