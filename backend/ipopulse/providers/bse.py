"""BSE's public-issue feed — the second exchange, used to verify the roster.

This provider answers exactly one question: **does this IPO actually exist?**

It is deliberately not a source of GMP, and never will be. No exchange
publishes grey-market data — see `scrape.py` for the same note about NSE —
so the numbers on reel 2 keep coming from InvestorGain. What BSE gives us is
the thing InvestorGain cannot: authority over the *list*.

Why a second exchange at all, when `scrape.py` already reads NSE:

- **An issue can list on one exchange and not the other.** NSE's
  `all-upcoming-issues` covers NSE and NSE Emerge; BSE's covers BSE and BSE
  SME. An issue listing only on BSE SME is invisible to NSE's feed and would
  read as unverified against it alone.
- **They fail independently.** NSE needs a session cookie and blocks a cold
  request with an HTML page; this endpoint needs only a Referer. A morning
  where one of them is unreachable should not turn every tracked IPO into a
  suspect.
- **Two agreeing sources is what makes an absence meaningful.** Meridian
  Logistics sat on the sheet as a ₹720 Cr mainboard issue "open today" and
  was on neither exchange, nor on InvestorGain's 2,010-row all-time
  catalogue, nor anywhere on the web. One missing feed is a blip; both
  missing, on a day the issue claims to be taking bids, is a fabrication.

The endpoint, verified 2026-08-18:

    api.bseindia.com/BseIndiaAPI/api/GetPublicIssue/w?type=1

Two things about it that are not obvious:

- **`type` is ignored.** 0, 1, 2 and 3 all return the identical 26 rows. Do
  not read meaning into it; filter on the payload instead.
- **The feed is not only IPOs.** `IR_flag` separates them: `IPO` is an equity
  public issue, `DPI` is a debt public issue (ICL Fincorp, Kosamattam and
  friends — NCDs, not shares). Taking the rows unfiltered would have put four
  bond issues on an IPO board.

Like NSE's, this feed carries current and upcoming issues only. An issue drops
off once it lists, so "absent from BSE" is evidence of nothing on its own for
an issue whose window has closed — which is why `roster.py` stamps a
confirmation the first time it sees one rather than re-asking every day.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

import requests

API = "https://api.bseindia.com/BseIndiaAPI/api/GetPublicIssue/w"
SITE = "https://www.bseindia.com"
TIMEOUT = 25

# api.bseindia.com answers a bare request with the site's HTML shell rather
# than JSON. The Referer is what makes it return the feed — not the
# User-Agent, which it does not check. Origin is sent for the same reason a
# browser would.
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Referer": SITE + "/",
    "Origin": SITE,
}


def _iso(text: Any) -> str | None:
    """'2026-08-18T00:00:00' -> '2026-08-18'. None when unparseable.

    Never guess a date: a window filed under the wrong day would make a live
    issue look closed, which is the exact failure this feed exists to catch.
    """
    if not text:
        return None
    s = str(text).strip()
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        try:
            return date.fromisoformat(s[:10]).isoformat()
        except ValueError:
            return None
    return None


def _band(text: Any) -> tuple[float, float]:
    """'190.00 - 201.00' -> (190.0, 201.0). (0, 0) when absent.

    Debt issues carry no band at all and arrive as None, so this has to treat
    "no band" as a normal answer rather than an error.
    """
    if not text:
        return 0.0, 0.0
    nums = re.findall(r"\d+(?:\.\d+)?", str(text))
    if not nums:
        return 0.0, 0.0
    if len(nums) == 1:
        return float(nums[0]), float(nums[0])
    return float(nums[0]), float(nums[1])


def _get() -> list[dict[str, Any]]:
    """The whole feed, unfiltered. Raises — the caller decides if that is fatal."""
    r = requests.get(API, params={"type": 1}, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return (r.json() or {}).get("Table") or []


def board() -> list[dict[str, Any]]:
    """Every equity public issue BSE currently lists. [] if unreachable.

    Shaped to match `investorgain.board()` field for field where the fields
    overlap, so a caller can walk either without special-casing. `[]` on
    failure rather than an exception, because an exchange being down must
    weaken a verification rather than break a run — see `roster.py`, which
    treats an empty roster as "could not check" and not as "does not exist".
    """
    try:
        rows = _get()
    except Exception:
        return []

    out: list[dict[str, Any]] = []
    for r in rows:
        # DPI is a debt public issue — an NCD, not an IPO. The board would
        # otherwise carry Kosamattam Finance next to Sunshine Pictures.
        if (r.get("IR_flag") or "").strip().upper() != "IPO":
            continue
        name = (r.get("Scrip_Name") or "").strip()
        code = r.get("Scrip_cd")
        if not name or not code:
            continue
        low, high = _band(r.get("Price_Band"))
        out.append({
            "id": int(code),                       # BSE scrip code
            "name": name,
            "long_name": (r.get("LONG_NAME") or "").strip(),
            "open": _iso(r.get("Start_Dt")),
            "close": _iso(r.get("End_Dt")),
            "price_low": low,
            "price_high": high,
            "face_value": r.get("Face_Val"),
            "exchange": "BSE",
            "url": f"{SITE}/publicissue.html",
        })
    return out


def available() -> bool:
    """Is the feed answering? Used to tell 'absent' apart from 'unreachable'."""
    return bool(board())
