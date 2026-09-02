"""Overnight news, in a stated window. Reel 7's five stories come from here.

The window is the point. A pre-market briefing has to be able to say *what it
looked at* — "everything that broke between 07:30 yesterday and 07:30 this
morning, IST" — because a headline with no window is a headline that might be
three days old, and the one thing that would sink this reel's credibility
faster than a wrong call is a stale story presented as this morning's news.
That is the same failure the company-background feature was cut down to avoid
(`strip-the-risk-not-the-feature`): the *date* was the whole problem there, and
here it is the whole product.

── the feeds, tested 2026-09-02 ───────────────────────────────────────────

    Google News RSS      100 items on an India-markets query, 44 on a world
                         query, every one with a parseable pubDate in GMT and
                         a <source> attribution. The primary, because the
                         query is ours and `when:1d` does the first cut.
    Economic Times       50 items, every one with pubDate (+0530) and an
                         image. A publisher feed, for corroboration.
    Livemint             35 items, same.
    Business Standard    35 items, 10 with images.

**Moneycontrol's marketreports.xml is dead.** It answers 200 with four items
whose newest pubDate is 23 Apr 2024. It looks like a working feed and is a
two-year-old snapshot — exactly the shape of source that would put a stale
story on a card marked LIVE. Do not add it back.

Reuters' own feed does not resolve from here.

── what this module does NOT do ───────────────────────────────────────────

**It does not take the publisher's image.** Every one of these feeds ships
`<media:content>` or `<enclosure>` art, and using it would be republishing a
news organisation's copyrighted photograph inside a monetised video. The
picture on a news scene is generated instead; `image` on `NewsItem` is filled
by the image step, not by this module. What travels from here is the headline,
the link and the attribution — which is quoting a source, not reusing its
assets.

**It does not choose the five.** It returns every candidate in the window,
deduplicated, with the raw signal a chooser needs (how many outlets carried
it, which sectors and tickers it touches). The model picks and writes the
words. Ranking headlines is a judgement; collecting them is not, and mixing
the two would make it impossible to tell a bad pick from a bad feed.
"""

from __future__ import annotations

import html
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

from .market import IST

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")
TIMEOUT = 20

# The window's edges, in IST. 07:30 is the user's spec and it is a good one:
# it is after the overnight US close and Asia's open, and before India's
# pre-open auction at 09:00 — so a briefing written at 08:00 has the complete
# overnight picture and nothing from the session it is about to describe.
WINDOW_HOUR, WINDOW_MIN = 7, 30

# Google News search feeds. Ours to shape, which is why they are primary: the
# query decides the beat, and `when:1d` throws away most of what a publisher
# feed would make us filter by hand.
#
# `gl=IN&ceid=IN:en` matters — the same query from a US locale returns a
# different and much less India-weighted set.
GOOGLE = "https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"
QUERIES = {
    "india": "indian stock market nifty sensex when:1d",
    "world": "global markets fed oil dollar treasury yields when:1d",
    "policy": "RBI repo rate inflation GDP india economy when:1d",
    "corporate": "india company results order win merger stake when:1d",
}

# Publisher feeds, for corroboration. A story two independent desks carried is
# a story; one outlet's exclusive on a slow morning may be neither.
PUBLISHERS = {
    "Economic Times":
        "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "Livemint": "https://www.livemint.com/rss/markets",
    "Business Standard":
        "https://www.business-standard.com/rss/markets-106.rss",
}

# Sector words in a headline, mapped to the NIFTY sectoral index they move.
# Crude on purpose. Its only job is to give the chooser a hint it can override
# — the model sees the headline text too — so a miss costs a hint and not a
# fact. The alternative, asking a model which sector a headline is about,
# spends a request on something a dictionary answers most of the time.
SECTOR_WORDS = {
    "NIFTY IT": ("it services", "software", "tcs", "infosys", "wipro", "hcl",
                 "tech mahindra", "h-1b", "ai deal"),
    "NIFTY BANK": ("bank", "lender", "credit growth", "npa", "hdfc", "icici",
                   "axis", "kotak", "sbi"),
    "NIFTY AUTO": ("auto", "car", "vehicle", "ev ", "two-wheeler", "maruti",
                   "tata motors", "mahindra", "bajaj auto", "eicher"),
    "NIFTY PHARMA": ("pharma", "drug", "usfda", "generic", "sun pharma",
                     "cipla", "dr reddy"),
    "NIFTY FMCG": ("fmcg", "consumer goods", "hul", "itc", "nestle",
                   "britannia", "monsoon demand"),
    "NIFTY METAL": ("steel", "metal", "aluminium", "copper", "iron ore",
                    "tata steel", "jsw", "hindalco", "vedanta", "coal"),
    "NIFTY REALTY": ("realty", "real estate", "housing", "dlf", "property"),
    "NIFTY ENERGY": ("oil", "crude", "brent", "gas", "opec", "reliance",
                     "ongc", "power", "ntpc", "refinery"),
    "NIFTY MEDIA": ("media", "broadcast", "streaming", "zee", "pvr"),
    "NIFTY PSU BANK": ("psu bank", "public sector bank", "pnb", "bank of baroda",
                       "canara"),
    "NIFTY FINANCIAL SERVICES": ("nbfc", "insurance", "amc", "mutual fund",
                                 "bajaj finance", "sebi", "fpi", "fii"),
}

_ITEM = re.compile(r"<item[ >](.*?)</item>", re.S)
_TAG = re.compile(r"<{t}[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{t}>", re.S)


def _field(block: str, tag: str) -> str:
    m = re.search(_TAG.pattern.format(t=tag), block, re.S)
    if not m:
        return ""
    # Twice: these feeds double-escape, so one pass leaves '&#39;' behind.
    return html.unescape(html.unescape(m.group(1))).strip()


def _strip(text: str) -> str:
    """Markup out of a description. Google's carries a whole <ol> of links."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()


def window(day: str | None = None) -> tuple[datetime, datetime]:
    """The 07:30-to-07:30 IST span ending on the morning of `day`.

    Steps back over a weekend at the opening edge, so a Monday briefing's
    window starts Friday morning and covers everything that broke while the
    market was shut — which on a Monday is most of what matters.
    """
    end_day = datetime.fromisoformat(day).date() if day \
        else datetime.now(IST).date()
    end = datetime(end_day.year, end_day.month, end_day.day,
                   WINDOW_HOUR, WINDOW_MIN, tzinfo=IST)
    start = end - timedelta(days=1)
    while start.weekday() >= 5:
        start -= timedelta(days=1)
    return start, end


def _fetch(url: str) -> str:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        return urllib.request.urlopen(req, timeout=TIMEOUT).read() \
            .decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError):
        return ""


def _when(block: str) -> datetime | None:
    raw = _field(block, "pubDate")
    if not raw:
        return None
    try:
        stamp = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        return None                    # a feed with no offset cannot be placed
    return stamp.astimezone(IST)       # Google sends GMT, ET sends +0530


def _sectors_for(text: str) -> list[str]:
    low = text.lower()
    return [name for name, words in SECTOR_WORDS.items()
            if any(w in low for w in words)]


# Words that appear in half of all market headlines and identify nothing.
# Left in the text the model reads; taken out of the fingerprint, because two
# unrelated stories both containing 'market' and 'stocks' are not one story.
_STOP = {"market", "markets", "stock", "stocks", "share", "shares", "today",
         "sensex", "nifty", "live", "updates", "update", "news", "india",
         "indian", "trade", "trading", "session", "close", "closing", "open",
         "opening", "here", "what", "why", "know", "check", "amid", "after",
         "before", "ahead", "over", "with", "from", "into", "these", "this",
         "that", "than", "then", "will", "your"}


def _tokens(title: str) -> set[str]:
    """A headline's content words, for duplicate detection."""
    words = re.findall(r"[a-z0-9]+", title.lower())
    return {w for w in words if len(w) > 3 and w not in _STOP}


# How much two headlines must share to be one story. Measured against the
# smaller set, not the union: 'Dollar gains as oil, yields rise on renewed
# inflation fears' and 'FOREX-Dollar gains as oil, rising bond yields stoke
# inflation fears' are the same Reuters wire under two desks' subject lines,
# and a union-denominator ratio scores them 0.45 — under any sane floor —
# because the longer headline adds words rather than disagreeing.
#
# This is the same false negative that put two Rays of Belief rows on the
# sheet, found the same way. See `dedupe.same_offer`.
SAME_STORY = 0.6


def collect(day: str | None = None, verbose: bool = False) -> dict[str, Any]:
    """Every story in the window, deduplicated, newest first.

    Returns the candidates and the window they were drawn from. `feeds` names
    which sources answered — a briefing built on one feed instead of seven is
    still publishable and should say so, the same way `market.snapshot`
    reports a partial run.
    """
    start, end = window(day)
    seen: dict[str, dict[str, Any]] = {}
    answered: list[str] = []
    silent: list[str] = []

    def take(block: str, beat: str, fallback_source: str) -> None:
        title = _field(block, "title")
        stamp = _when(block)
        if not title or stamp is None:
            return
        if not (start <= stamp <= end):
            return
        # Google appends ' - Publisher' to every headline and also gives the
        # publisher in <source>; prefer the tag and trim the suffix so the
        # same story from two feeds produces the same text.
        source = _field(block, "source") or fallback_source
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3].strip()
        toks = _tokens(title)
        if len(toks) < 3:
            return                     # too generic to place or to dedupe
        body = _strip(_field(block, "description"))[:400]
        item = None
        for cand in seen.values():
            shared = len(toks & cand["tokens"])
            if shared / min(len(toks), len(cand["tokens"])) >= SAME_STORY:
                item = cand
                break
        if item:
            # Same story, another desk. Count it and keep the earliest
            # timestamp — when it broke, not when the last outlet noticed.
            if source and source not in item["outlets"]:
                item["outlets"].append(source)
            item["at"] = min(item["at"], stamp)
            if beat not in item["beats"]:
                item["beats"].append(beat)
            if len(body) > len(item["body"]):
                item["body"] = body        # the fullest summary of the set
            # Union the fingerprints so the next desk's rewording matches the
            # cluster rather than only the first headline that formed it.
            item["tokens"] |= toks
            return
        seen[" ".join(sorted(toks))] = {
            "tokens": toks,
            "headline": title,
            "body": body,
            "url": _field(block, "link"),
            "at": stamp,
            "outlets": [source] if source else [],
            "beats": [beat],
            "sectors": _sectors_for(f"{title} {body}"),
        }

    for beat, query in QUERIES.items():
        url = GOOGLE.format(q=urllib.parse.quote(query))
        raw = _fetch(url)
        blocks = _ITEM.findall(raw)
        (answered if blocks else silent).append(f"google:{beat}")
        for block in blocks:
            take(block, beat, "")

    for name, url in PUBLISHERS.items():
        raw = _fetch(url)
        blocks = _ITEM.findall(raw)
        (answered if blocks else silent).append(name)
        for block in blocks:
            take(block, "publisher", name)

    items = sorted(seen.values(), key=lambda i: (-len(i["outlets"]), -i["at"].timestamp()))
    for item in items:
        item["at"] = item["at"].isoformat(timespec="minutes")
        item["outlet_count"] = len(item["outlets"])
        item.pop("tokens", None)       # a set: not JSON, and of no use onward

    if verbose:
        for item in items[:20]:
            print(f"  [{item['outlet_count']}] {item['at'][11:16]} "
                  f"{item['headline'][:70]}")

    return {
        "from": start.isoformat(timespec="minutes"),
        "to": end.isoformat(timespec="minutes"),
        "feeds": answered,
        "silent": silent,
        "count": len(items),
        # Capped before it reaches a prompt. Every story in the window is the
        # honest answer to "what happened", but 200 headlines in a context
        # window is mostly noise about the same six events, and the dedupe
        # count has already sorted the signal to the top.
        "items": items[:60],
    }


if __name__ == "__main__":
    import json
    out = collect(verbose=True)
    print(json.dumps({k: v for k, v in out.items() if k != "items"}, indent=1))
