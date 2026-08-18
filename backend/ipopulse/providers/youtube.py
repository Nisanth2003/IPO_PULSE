"""What this channel has already published. Keyless, no API, no OAuth.

Two things this is for:

- **Planning.** Which IPOs already have a video, which reels are still
  missing, what has gone stale. That question was previously answered by
  scrolling YouTube by hand.
- **Scripts that can point somewhere.** Reel 1 ends by saying the full
  breakdown is on the channel. With this it can name the actual video
  instead of gesturing at one.

**On the URL in .env.** `YOUTUBE_STUDIO_URL` points at
`studio.youtube.com/channel/UC…`, which is the owner's private dashboard —
it requires their Google session, so nothing external can read it. Not curl,
not a scraper, and not a model with a URL-reading tool either: an
unauthenticated fetch gets a sign-in page, and asking a model to "read" that
gets a confident description of a login screen.

What the URL *does* carry is the channel id, and that id opens a completely
public, keyless feed:

    https://www.youtube.com/feeds/videos.xml?channel_id=UC…

That is the whole trick here — take the id out of the private URL and ask the
public endpoint. No key, no quota, no consent screen, and it works from a
GitHub runner. The Data API v3 would also work but needs a key and spends
quota to answer a question this feed answers for free.

Limits worth knowing: the feed carries roughly the **15 most recent uploads**
and nothing older, and it excludes private and unlisted videos. For "what did
I publish this fortnight" that is the right shape; for a full archive it is
not, and the Data API would be needed.
"""

from __future__ import annotations

import os
import re
import urllib.request
from typing import Any

FEED = "https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
TIMEOUT = 20

# UC + 22 more base64url characters. Matching the shape rather than "the bit
# after /channel/" means a studio URL, a plain channel URL, or the bare id all
# work, and a handle URL (youtube.com/@name, which carries no id) correctly
# fails rather than silently yielding a wrong id.
_CID = re.compile(r"(UC[0-9A-Za-z_-]{22})")


def channel_id(source: str | None = None) -> str:
    """The channel id, from an explicit value or from YOUTUBE_STUDIO_URL."""
    raw = (source or os.getenv("YOUTUBE_STUDIO_URL") or "").strip()
    m = _CID.search(raw)
    return m.group(1) if m else ""


def _text(block: str, tag: str) -> str:
    m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", block, re.S)
    if not m:
        return ""
    s = m.group(1)
    for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                 ("&quot;", '"'), ("&#39;", "'")):
        s = s.replace(a, b)
    return s.strip()


def videos(source: str | None = None) -> list[dict[str, Any]]:
    """Recent uploads, newest first. [] when unreachable or not configured.

    Parsed with regex rather than an XML parser on purpose: the feed is a
    fixed, small Atom document, and this keeps the module dependency-free so
    it can run anywhere the rest of the pipeline does.
    """
    cid = channel_id(source)
    if not cid:
        return []
    try:
        req = urllib.request.Request(
            FEED.format(cid=cid),
            headers={"User-Agent": "Mozilla/5.0 (compatible; ipopulse/1.0)"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            xml = r.read().decode("utf-8", "replace")
    except Exception:
        return []

    out: list[dict[str, Any]] = []
    for block in re.findall(r"<entry>(.*?)</entry>", xml, re.S):
        vid = _text(block, "yt:videoId")
        title = _text(block, "title")
        if not vid or not title:
            continue
        out.append({
            "id": vid,
            "title": title,
            "url": f"https://www.youtube.com/watch?v={vid}",
            "published": _text(block, "published")[:10],
            "views": _text(block, "media:statistics") or "",
        })
    return out


def channel_name(source: str | None = None) -> str:
    """The channel's own title. Answers even when it has no uploads yet,
    which makes it the cheapest way to confirm the id is right."""
    cid = channel_id(source)
    if not cid:
        return ""
    try:
        req = urllib.request.Request(
            FEED.format(cid=cid),
            headers={"User-Agent": "Mozilla/5.0 (compatible; ipopulse/1.0)"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            xml = r.read().decode("utf-8", "replace")
    except Exception:
        return ""
    # The channel's own <title> is the first one, before any <entry>.
    head = xml.split("<entry>", 1)[0]
    return _text(head, "title")


def coverage(slugs: list[str], companies: dict[str, str],
             source: str | None = None) -> dict[str, list[dict[str, Any]]]:
    """Which tracked IPOs already have a video, by slug.

    Matched on the company's distinctive words appearing in the title, which
    is how these titles are actually written ("Lalithaa Jewellery Mart IPO GMP
    Today"). Deliberately loose in the other direction from `roster`: a false
    positive here costs a duplicate-video warning, not a wrong claim about a
    company existing.
    """
    vids = videos(source)
    noise = {"limited", "ltd", "private", "pvt", "india", "the", "and", "ipo"}
    out: dict[str, list[dict[str, Any]]] = {s: [] for s in slugs}
    for slug in slugs:
        words = [w for w in re.findall(r"[a-z0-9]+", (companies.get(slug) or slug).lower())
                 if w not in noise and len(w) > 2]
        if not words:
            continue
        for v in vids:
            low = v["title"].lower()
            if sum(1 for w in words if w in low) >= min(2, len(words)):
                out[slug].append(v)
    return out
