"""Store front-end for the daily market briefing. `store.py`, for reel 7.

Deliberately the same shape as `store.py` — `load`, `load_all`, `save`,
`remove`, `latest` — so that anything reading briefings looks like anything
reading IPOs and neither has to know where the data lives. The difference is
the key: this is keyed by ISO date, and `store.py` is keyed by IPO slug.

    sheets.market_records()        the four Market* tabs, parsed
    sheets.write_market_records()  those four tabs and nothing else
    tables.MARKET_TABS             their layout

Two things this module exists to enforce.

**A briefing is written once and then it is history.** Every other record in
this project is a live thing that gets corrected as an issue progresses — a
GMP fills in, a subscription day arrives, a verdict changes. A briefing is a
statement about one morning, and the morning is over. `save` will not
overwrite a stored day without `replace=True`, because a second run of the
generator against a market that has since moved would quietly rewrite what
the reel was recorded from, leaving a video and a sheet that disagree.

**Verify after writing.** The sheet has no lock and a scheduled job rewriting
its tabs can revert an edit made while it ran. The IPO side learned this the
hard way. `save` re-reads and compares rather than trusting the write.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Iterator

from . import sheets
from .models import Briefing
from .providers.market import IST

SheetUnavailable = sheets.SheetUnavailable


def today() -> str:
    """Today in IST, as the store key.

    IST rather than the host's clock. The scheduler runs on a Windows box
    whose timezone nobody has promised, and a briefing for Tuesday morning
    filed under Monday is worse than no briefing: `readiness` would call it
    expired and the studio would show yesterday's outlook as today's.
    """
    return datetime.now(IST).date().isoformat()


def where() -> str:
    sid = sheets.sheet_id()
    return f"Google Sheet {sid[:8]}… (Market tabs)" if sid \
        else "no sheet configured"


def list_days() -> list[str]:
    """Every stored briefing date, oldest first."""
    return sorted(sheets.market_records())


def load(day: str | date | None = None) -> Briefing:
    """One day's briefing. Defaults to today in IST."""
    key = _key(day)
    rec = sheets.market_records().get(key)
    if rec is None:
        raise FileNotFoundError(f"No briefing for {key} in the sheet")
    return Briefing.from_dict(dict(rec))


def exists(day: str | date | None = None) -> bool:
    return _key(day) in sheets.market_records()


def load_all() -> list[Briefing]:
    out = []
    for day, rec in sorted(sheets.market_records().items()):
        try:
            out.append(Briefing.from_dict(dict(rec)))
        except Exception as exc:                   # one bad row must not take
            print(f"  ! skipping briefing {day}: {exc}")   # the history with it
        continue
    return out


def latest() -> Briefing | None:
    """The most recent stored briefing, whatever day it is for."""
    days = list_days()
    return load(days[-1]) if days else None


def recent(limit: int = 10) -> list[Briefing]:
    """The last `limit` briefings, newest first.

    What the accuracy review reads. A day's calls can only be judged against
    what the market then did, so the value of this record is cumulative — a
    single briefing proves nothing and thirty of them are a track record.
    """
    return [load(day) for day in reversed(list_days()[-limit:])]


def save(b: Briefing, replace: bool = False) -> str:
    """Write one briefing, keeping every other day.

    Refuses to overwrite a stored day unless `replace=True`. See the module
    docstring: re-running the generator hours later against a market that has
    moved would rewrite the thing a recorded reel was built from.

    Verifies against the sheet afterwards rather than trusting the write,
    because a concurrent job can revert it and a silent revert is how a reel
    ends up quoting numbers the sheet no longer holds.
    """
    key = b.key
    if not key:
        raise ValueError("a briefing needs a date before it can be saved")
    if key in sheets.market_records() and not replace:
        raise FileExistsError(
            f"{key} already has a briefing (pass --replace to overwrite it). "
            f"A briefing is a statement about one morning; rewriting it after "
            f"the fact leaves the sheet and any recorded reel disagreeing.")

    sheets.upsert_market(key, b.to_dict())

    after = sheets.market_records(force=True).get(key)
    if after is None:
        raise RuntimeError(
            f"wrote {key} but it is not on the sheet — another job may have "
            f"been writing at the same time. Nothing else was changed; run "
            f"the command again.")
    return where()


def remove(day: str | date) -> bool:
    return sheets.drop_market(_key(day))


def iter_briefings() -> Iterator[Briefing]:
    for day in list_days():
        yield load(day)


def _key(day: str | date | None) -> str:
    if day is None:
        return today()
    if isinstance(day, (date, datetime)):
        return day.isoformat()[:10]
    return str(day).strip()[:10]


def previous_session(day: str | None = None) -> str:
    """The trading day before `day`, for the news window's opening edge.

    Calendar arithmetic only — it steps back over weekends and stops. It does
    not consult the holiday list, and that is a deliberate limit rather than
    an oversight: this is used to *label* a 07:30-to-07:30 window, and a
    Tuesday after a Monday holiday still has news from Monday in it. Whether
    there was a session is `market.trading_day`'s question, asked separately.
    """
    cursor = date.fromisoformat(_key(day)) - timedelta(days=1)
    while cursor.weekday() >= 5:
        cursor -= timedelta(days=1)
    return cursor.isoformat()
