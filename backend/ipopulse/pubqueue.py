r"""The approval gate between a rendered video and the channel.

Everything upstream of this runs on a timer with nobody watching. That is the
goal and it is also the risk: a pipeline that can publish on its own can
publish a mistake on its own, and this one puts numbers about people's money
on a public channel.

So there is exactly one door, and a person holds it:

    render  ->  QUEUED  ->  [ a human looks at it ]  ->  APPROVED  ->  uploaded

Nothing reaches YouTube without passing through `approve()`. Not a scheduled
job, not a retry loop, not a run that went wrong at 3am. `youtube_upload` has
no idea what a queue is and only uploads what it is handed; the only code that
hands it anything is `take_approved()` below.

── Why a file and not a sheet ─────────────────────────────────────────────

Every other record in this project lives in the Google Sheet, and this one
deliberately does not. Two reasons, and the second is the real one:

1. The queue references **local files** — an mp4 and a PNG on this machine's
   disk. A row in a shared spreadsheet pointing at `D:\…\demo-r5-en.mp4` is
   meaningless to anything but this computer.
2. The sheet is written by unlocked read-modify-write passes that replace
   whole tabs (see `sheets.write_records`). An approval is a *decision*, and
   a decision must not be recoverable by a job that happened to be mid-write
   when it was made. A separate file cannot be clobbered by the pipeline.

── States ─────────────────────────────────────────────────────────────────

    queued     rendered, waiting for a person
    approved   a person said yes, and chose the visibility
    uploaded   it is on the channel; carries the video id
    failed     the upload was attempted and did not work; carries why
    rejected   a person said no. Kept, not deleted — knowing a reel was
               rejected is why the same bad render does not get re-queued
               tomorrow.

An item is keyed by `(slug, reel, lang)` — one queue entry per video, so a
re-render replaces its entry rather than adding a second one. Except once it
is `uploaded`: that entry is history and a re-render queues a fresh item, so
the record of what actually went out is never rewritten.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .sheets import OUT_DIR

QUEUE = OUT_DIR / "publish-queue.json"

QUEUED, APPROVED, UPLOADED, FAILED, REJECTED = (
    "queued", "approved", "uploaded", "failed", "rejected")

# What a fresh approval defaults to when no visibility is named.
#
# Unlisted, never public. A wrong figure in an unlisted video is an
# embarrassment; the same figure public on a finance channel is somebody's
# money. Going public is a thing you say out loud — `approve(..., public=True)`
# — not a default you inherit.
DEFAULT_PRIVACY = "unlisted"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load() -> list[dict[str, Any]]:
    if not QUEUE.is_file():
        return []
    try:
        blob = json.loads(QUEUE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return blob.get("items", []) if isinstance(blob, dict) else []


def _save(items: list[dict[str, Any]]) -> None:
    """Write atomically. A half-written queue is an unreadable one."""
    QUEUE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(QUEUE.parent), suffix=".json.tmp")
    os.close(fd)
    try:
        Path(tmp).write_text(
            json.dumps({"updated": _now(), "items": items}, indent=1,
                       ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, QUEUE)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def key(slug: str, reel: int, lang: str) -> str:
    return f"{slug or 'market'}-r{reel}-{lang}"


def add(slug: str, reel: int, lang: str, video: Path, title: str,
        description: str, tags: list[str] | None = None,
        thumbnail: Path | None = None, company: str = "",
        seconds: float = 0.0, notes: str = "") -> dict[str, Any]:
    """Queue a rendered video for review. Replaces an unsent entry."""
    items = _load()
    ident = key(slug, reel, lang)
    item = {
        "id": ident,
        "slug": slug, "reel": reel, "lang": lang, "company": company,
        "video": str(Path(video).resolve()),
        "thumbnail": str(Path(thumbnail).resolve()) if thumbnail else "",
        "title": title, "description": description, "tags": tags or [],
        "seconds": round(float(seconds), 1),
        "status": QUEUED, "privacy": "", "queued_at": _now(),
        "decided_at": "", "uploaded_at": "", "video_id": "", "url": "",
        "error": "", "notes": notes,
    }
    # An entry that already went out is history. Re-rendering the same reel
    # queues a new one under a suffixed id rather than overwriting the record
    # of what was actually published.
    for i, existing in enumerate(items):
        if existing["id"] == ident and existing["status"] != UPLOADED:
            items[i] = item
            _save(items)
            return item
    if any(e["id"] == ident and e["status"] == UPLOADED for e in items):
        n = sum(1 for e in items if e["id"].startswith(ident))
        item["id"] = f"{ident}#{n + 1}"
    items.append(item)
    _save(items)
    return item


def items(status: str | None = None) -> list[dict[str, Any]]:
    got = _load()
    return [i for i in got if i["status"] == status] if status else got


def find(ident: str) -> dict[str, Any] | None:
    for item in _load():
        if item["id"] == ident:
            return item
    return None


def approve(ident: str, public: bool = False,
            privacy: str | None = None) -> dict[str, Any]:
    """A person says yes. This is the only route to an upload.

    `public` is a separate argument from `privacy` on purpose: the common case
    is a yes/no click, and making "public" a distinct explicit word means it
    cannot be reached by passing a string through from somewhere else.
    """
    chosen = privacy or ("public" if public else DEFAULT_PRIVACY)
    if chosen not in ("private", "unlisted", "public"):
        raise ValueError(f"bad visibility: {chosen}")
    return _set(ident, APPROVED, privacy=chosen)


def reject(ident: str, why: str = "") -> dict[str, Any]:
    return _set(ident, REJECTED, error=why)


def mark_uploaded(ident: str, video_id: str, url: str) -> dict[str, Any]:
    """Record the upload here AND on the sheet.

    Two places on purpose, holding different things. This file is the working
    queue and knows about mp4s on one machine's disk. The sheet's `Published`
    tab holds only what went live — slug, reel, lang, video id — because that
    is durable channel state: it outlives this machine, and the studio reads
    it to grey out the reels that are already done without asking YouTube.

    The sheet write is best-effort. An upload that succeeded must not be
    reported as failed because a spreadsheet was briefly unreachable; the
    local record is still correct and `ipopulse publish --sync` re-files any
    that did not make it.
    """
    item = _set(ident, UPLOADED, video_id=video_id, url=url,
                uploaded_at=_now())
    try:
        record_on_sheet(item)
    except Exception as exc:
        print(f"  · uploaded, but could not write it to the sheet ({exc}). "
              f"`ipopulse publish --sync` will file it.")
    return item


def record_on_sheet(item: dict[str, Any]) -> None:
    """Put one published video on the IPO's row. Idempotent."""
    from . import store
    from .models import Ipo

    slug = item.get("slug") or ""
    if not slug or not item.get("video_id"):
        return
    try:
        ipo = store.load(slug)
    except FileNotFoundError:
        return                            # reel 7 and anything not an IPO
    raw = ipo.to_dict()
    rows = [v for v in (raw.get("published") or [])
            # Replace any earlier row for the same video: a re-upload of the
            # same reel supersedes it rather than appearing twice.
            if not (int(v.get("reel") or 0) == int(item["reel"])
                    and str(v.get("lang") or "") == item["lang"])]
    rows.append({
        "reel": int(item["reel"]), "lang": item["lang"],
        "video_id": item["video_id"], "url": item.get("url", ""),
        "privacy": item.get("privacy", ""),
        "published": (item.get("uploaded_at") or "")[:10],
        "title": item.get("title", ""),
    })
    raw["published"] = sorted(rows, key=lambda v: (v["reel"], v["lang"]))
    store.save(Ipo.from_dict(raw))


def coverage() -> dict[str, Any]:
    """Which reels are published, and which are still to do.

    Reads the SHEET, not this file — so it answers correctly on a machine
    that has never rendered anything, and it keeps answering after the queue
    is pruned.
    """
    from . import store

    langs = ("en", "hi", "te")
    reels = (1, 2, 3, 4, 5, 6)
    out, done_n, total_n = [], 0, 0
    for ipo in store.load_all():
        have = {(int(v.get("reel") or 0), v.get("lang")) for v in ipo.published}
        missing = [(r, l) for r in reels for l in langs if (r, l) not in have]
        done_n += len(reels) * len(langs) - len(missing)
        total_n += len(reels) * len(langs)
        out.append({"slug": ipo.slug, "company": ipo.company or ipo.slug,
                    "done": sorted(have), "missing": missing})
    return {"rows": out, "done": done_n, "total": total_n,
            "pct": round(100 * done_n / total_n, 1) if total_n else 0.0}


def mark_failed(ident: str, why: str) -> dict[str, Any]:
    return _set(ident, FAILED, error=why[:500])


def _set(ident: str, status: str, **fields) -> dict[str, Any]:
    got = _load()
    for item in got:
        if item["id"] != ident:
            continue
        if item["status"] == UPLOADED and status != UPLOADED:
            raise ValueError(
                f"{ident} is already on the channel — its record is history "
                f"and does not get rewritten.")
        item["status"] = status
        item["decided_at"] = _now()
        item.update(fields)
        _save(got)
        return item
    raise KeyError(f"nothing queued under {ident!r}")


def take_approved() -> list[dict[str, Any]]:
    """Everything a person has approved and that still exists on disk.

    The file check is not paranoia. A queue entry is a path, and between the
    approval and the upload somebody may have cleaned out `out/video/` — and
    an upload of a missing file fails in a way that reads like an auth
    problem. Better to say plainly which file went away.
    """
    out = []
    for item in items(APPROVED):
        if Path(item["video"]).is_file():
            out.append(item)
        else:
            mark_failed(item["id"],
                        f"the rendered file is gone: {item['video']}")
    return out


def summary() -> dict[str, int]:
    counts = {s: 0 for s in (QUEUED, APPROVED, UPLOADED, FAILED, REJECTED)}
    for item in _load():
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return counts


def prune(keep_uploaded: int = 60) -> int:
    """Trim old uploaded/rejected entries. Returns how many went."""
    got = _load()
    done = [i for i in got
            if i["status"] in (UPLOADED, REJECTED)]
    done.sort(key=lambda i: i.get("uploaded_at") or i.get("decided_at") or "")
    drop = {id(i) for i in done[:-keep_uploaded]} if len(done) > keep_uploaded \
        else set()
    if not drop:
        return 0
    _save([i for i in got if id(i) not in drop])
    return len(drop)
