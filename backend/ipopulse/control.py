"""Password-gated trigger panel for running ipopulse jobs from a browser.

The timers (systemd, or Task Scheduler on Windows) run the same jobs on a
schedule. This is the manual override for when you don't want to wait for the
next tick — the same fixed set of commands, nothing more.

**This never ships to GitHub Pages.** The panel HTML lives here in the backend
rather than in `frontend/`, because that folder is published verbatim by
.github/workflows/pages.yml. A password checked in browser JavaScript on a
public static site is not a password, so the check has to happen here, in a
process only you run.

Security, such as it is — this is a localhost tool, not a public service:

  * `IPOPULSE_TRIGGER_PASSWORD` comes from .env, so forgetting it means
    editing one line rather than reinstalling anything.
  * Compared with `hmac.compare_digest`, so a wrong guess takes the same time
    as a right one.
  * Five bad attempts locks the door for five minutes, per client address.
  * The password is exchanged once for a random session token with a TTL; it
    is not resent with every request.
  * Jobs are a fixed dict of argv lists run with shell=False. There is no
    endpoint that accepts a command, so there is nothing to inject into.
  * Binds 127.0.0.1 unless told otherwise, and refuses to bind anything else
    without a password set.
"""

from __future__ import annotations

import hmac
import json
import os
import queue
import secrets
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from typing import Any

TOKEN_TTL = 8 * 3600          # a working day
MAX_FAILS = 5
LOCKOUT = 300                 # seconds
LOG_LINES = 400               # ring buffer per job run


# ── the jobs ───────────────────────────────────────────────────────────────
# argv only — never a shell string. Adding an entry here is the only way to
# make a new command reachable from the browser.

JOBS: dict[str, dict[str, Any]] = {
    "sync": {
        "label": "Sync from NSE",
        "detail": "Live issue terms and subscription. Discovers new IPOs. No key needed.",
        "argv": ["sync", "--provider", "nse", "--discover", "--no-translate"],
        "schedule": "part of daily",
    },
    "gmp-sync": {
        "label": "GMP from InvestorGain",
        "detail": "Free, keyless, no AI. The dated GMP table for every IPO on "
                  "InvestorGain's board — the desk this channel quotes — plus "
                  "any mainboard IPO there we do not track yet. ipoji answers "
                  "only if InvestorGain's board is unreachable. Fills gaps, "
                  "never overwrites.",
        # --discover because NSE, the other catalogue, lists an issue only
        # once it is about to open. Three mainboard IPOs sat on ipoji's board
        # untracked — one already quoting a premium — because nothing here
        # looked at that list for names rather than numbers.
        # --mainboard-only: the same board carries SME issues the channel
        # does not cover, and each scaffolded row costs enrich budget.
        # --reconcile keeps the trail one desk's quote for good. Without it
        # the chart drifts back into a blend the moment any other source
        # writes a day, and a change of source reads as a change of price.
        # Hand-typed days are exempt, so a correction still sticks.
        "argv": ["gmp-sync", "--history", "--write", "--reconcile",
                 "--discover", "--mainboard-only"],
        "schedule": "11:15, 14:15, 21:15 IST + part of grey",
    },
    "gmp": {
        "label": "Refresh GMP (model)",
        "detail": "Grey-market premium via Gemini grounded search, for the "
                  "few IPOs InvestorGain does not carry. Skips anything a "
                  "free source already priced today, so it costs nothing on "
                  "a normal night.",
        "argv": ["refresh"],
        "schedule": "part of grey",
    },
    "translate": {
        "label": "Translate",
        "detail": "Gemini → Hindi and Telugu, written onto the sheet's I18n tab. Cached 30 days.",
        "argv": ["translate"],
        "schedule": "Sun 03:00 IST",
    },
    "enrich": {
        "label": "Fill missing data",
        "detail": "For any IPO still missing them: issue details, RHP "
                  "financials, analysis draft, hi/te translation. Budgeted, "
                  "idempotent, and skips whatever is already there.",
        "argv": ["enrich", "--max-ai", "6"],
        "schedule": "part of daily",
    },
    "facts": {
        "label": "Financials from InvestorGain",
        "detail": "Three years of restated revenue / EBITDA / PAT / net worth "
                  "/ debt, plus post-issue EPS and the sHNI and bHNI minimum "
                  "bids — read from the same detail record the company brief "
                  "already uses. Free, keyless, no AI. Fills gaps only, so a "
                  "figure you corrected by hand survives it. This is what "
                  "reel 4's financials and valuation scenes read.",
        "argv": ["facts"],
        "schedule": "part of daily",
    },
    "monitor": {
        "label": "Is the data still arriving?",
        "detail": "The watchdog. Compares the store against the issue calendar "
                  "(an issue taking bids today MUST have a subscription row "
                  "dated today) and against its own last run, so a timer that "
                  "silently stopped firing shows up as a failed task instead "
                  "of as a stale premium in a reel. Also catches the same "
                  "company stored twice. Read-only, keyless, no AI.",
        # --strict so a run where nothing arrived exits non-zero and Task
        # Scheduler shows red. Without it the watchdog can only be found by
        # going and reading its output, which is the thing nobody does.
        "argv": ["monitor", "--strict"],
        "schedule": "12:30, 19:30, 22:30 IST daily",
    },
    "validate": {
        "label": "Which reels are recordable",
        "detail": "Per IPO and per reel: is every field the scenes read "
                  "present, are the moving numbers fresh, and is the reel "
                  "still inside its validity window. Also names the "
                  "contradictions a record can hold while looking complete — "
                  "a listing before the close, an EBITDA above revenue.",
        "argv": ["validate"],
        "schedule": "part of daily",
    },
    "market": {
        "label": "Pre-market briefing (reel 7)",
        "detail": "The daily market briefing: index levels, breadth and every "
                  "sectoral index from NSE; the overnight news in a stated "
                  "07:30-to-07:30 IST window from seven feeds; and ten "
                  "intraday pivot setups whose every level is arithmetic on "
                  "the session's own high, low and close. The strongest model "
                  "reachable writes the words and chooses which candidates "
                  "matter — it never returns a number that reaches the sheet.",
        # 08:00 IST, and the hour is the whole point. It has to be after the
        # overnight news window closes at 07:30 and before the pre-open
        # auction at 09:00, so the briefing has the complete overnight picture
        # and nothing from the session it is about to describe. `readiness`
        # expires the reel at 09:15, which leaves 75 minutes to record it.
        #
        # Deliberately NOT in the `daily` chain. That chain runs at 10:00 and
        # 18:35 — both after the open — and a briefing built then is a
        # description of a session the viewer can already see.
        "argv": ["market", "--write"],
        "schedule": "08:00 IST daily",
    },
    "dedupe": {
        "label": "One offer, one row",
        "detail": "Finds rows that describe the same offer — by the logo the "
                  "desk publishes, by one name being the other plus a legal "
                  "suffix, or by an identical bidding window and issue size — "
                  "and shows the fold that would repair them. Free, keyless, "
                  "no AI.",
        # Dry run on purpose, and it is the one job here deliberately denied
        # its --write. Every other repair in the chain fills a blank; a merge
        # destroys a row, and a chain step that quietly destroyed data while
        # nobody was watching is a worse failure than the duplicate it fixed.
        # The prevention lives at the discovery doors, which refuse to create
        # the second row at all — this is the report that says whether that is
        # still working.
        "argv": ["dedupe"],
        "schedule": "part of daily",
    },
    "grade": {
        "label": "Grade the data",
        "detail": "Scores every stored GMP and subscription figure against "
                  "InvestorGain and names the disagreements. Read-only — it "
                  "measures, it never repairs, because a grader that fixed "
                  "what it measured would always report an A. Free, keyless.",
        "argv": ["grade"],
        # Daily now, not weekly. It was weekly because it only compared GMP
        # and subscription — figures that move slowly and get re-read three
        # times a day anyway. It now reconciles the issue terms as well, and
        # those are exactly what went wrong unnoticed for days: a phantom OFS
        # on ten IPOs, three inverted price bands, six impossible calendars.
        # An early-warning system that runs once a week is six days late.
        "schedule": "part of daily + Sun 05:00 IST",
    },
    "verify": {
        "label": "Verify against the exchanges",
        "detail": "Asks NSE and BSE whether each tracked IPO exists at all, "
                  "and stamps the confirmation so a listed issue dropping off "
                  "both feeds is not mistaken for a fabricated one. Free, "
                  "keyless, no AI. GMP still comes from InvestorGain — the "
                  "exchanges publish no grey-market data and never will.",
        "argv": ["verify", "--write"],
        "schedule": "part of daily",
    },
    "doctor": {
        "label": "Check & repair",
        "detail": "Lists what is missing and which scene it blanks; repairs "
                  "what is derivable (T+3 dates, issue total, registrar URL).",
        "argv": ["doctor", "--fix"],
        "schedule": "part of daily",
    },
    "build": {
        "label": "Check the sheet",
        "detail": "Verify every record in the sheet still derives cleanly, so "
                  "a broken row surfaces here and not as a blank card.",
        "argv": ["build"],
        "schedule": "part of daily & grey",
    },
    # There is no `push` job any more. It existed to copy a local store up
    # into the Google Sheet; the sheet IS the store now, so pushing it to
    # itself is a no-op that could only ever lose data by rewriting rows the
    # run had not read. `import` still exists for pulling an OUTSIDE
    # spreadsheet in — that is a different direction and still useful.
    "report": {
        "label": "Excel report",
        "detail": "Formatted, human-readable .xlsx into backend/out/ — a printout, not the store.",
        "argv": ["report"],
        "schedule": "Sun 04:00 IST",
    },
    "daily": {
        "label": "Daily chain",
        # Spelled out step by step, and kept in step with CHAINS below.
        # This string was stale for a while — it still read "sync → enrich →
        # doctor → build" after `verify`, `facts`, `dedupe` and `validate`
        # had joined the chain, so the panel was describing a pipeline that
        # had not existed for weeks. A description of what a button does is
        # part of what the button does.
        "detail": "The one to run if you run one. Eight steps: discover new "
                  "IPOs from NSE (sync), challenge them against both "
                  "exchanges (verify), check nothing is stored twice "
                  "(dedupe), pull financials free from InvestorGain (facts), "
                  "let the model fill what is still missing (enrich), repair "
                  "what is derivable (doctor), re-derive every record "
                  "(build), then report which reels are recordable "
                  "(validate).",
        "argv": None,                       # composite; see CHAINS
        "schedule": "10:00 & 18:35 IST daily",
    },
    "grey": {
        "label": "GMP chain",
        "detail": "Three steps, for the grey market only: today's premium "
                  "from InvestorGain (free, keyless), then the model fills "
                  "the few IPOs that desk does not carry, then build "
                  "verifies what the night wrote. Cheaper and narrower than "
                  "the daily chain — run this when only the GMP has moved.",
        "argv": None,
        "schedule": "23:45 IST daily",
    },
}

# Chained rather than scheduled separately, and that is the point: steps
# that read what the previous one wrote cannot be timed apart and hoped for.
# Inside a chain, step N+1 cannot start until step N has exited 0.
# `doctor` sits between sync and build so the T+3 calendar and the registrar
# URL are filled from whatever NSE just supplied, before the JSON is written.
# It never exits non-zero without --strict, so it cannot break the chain — a
# repair step that could stop a publish would be worse than the gaps it fixes.
# `enrich` sits right after `sync`, because sync is what discovers a new IPO
# and enrich is what makes it usable. Without that pairing, discovery only
# ever produced an empty card that waited for somebody to type four commands
# at it — the tools to fill it existed but nothing ran them.
#
# Its budget is small (6 calls) precisely because the chain runs twice a day:
# the work spreads across runs instead of exhausting the free tier in one.
CHAINS = {
    # `verify` runs immediately after `sync`, which is the step that discovers
    # new IPOs — so a row invented by a discovery source is challenged in the
    # same run that created it, before `enrich` spends model budget writing an
    # analysis of a company that does not exist. It cannot break the chain: it
    # exits non-zero only under --strict, which the job does not pass.
    # `facts` sits BEFORE `enrich` and that ordering is the point: it fills the
    # financials for free, so `enrich`'s RHP step — a Gemini read of a 400-page
    # PDF — finds them already present and skips. The free source runs first
    # and the metered one only covers what it could not.
    #
    # `validate` closes the chain, after `build`. It is the answer to "can I
    # record anything today", and it is only worth asking once the run has
    # finished writing — asked earlier it reports gaps the same run then fills.
    # It exits 0 without --strict, so it cannot break the chain it reports on.
    # `dedupe` sits right after `sync` for the same reason `verify` does: sync
    # is the step that discovers, so a second row for an offer already tracked
    # is challenged in the run that would have created it rather than five
    # days later. It runs before `enrich` so a pile-up is visible before the
    # model budget is spent twice on one company, and it is a dry run, so it
    # reports and cannot break the chain — see the job's comment for why the
    # write is withheld.
    # `grade` closes the chain, after `validate`.
    #
    # Last on purpose, and read-only like the two before it: it is the answer
    # to "are the numbers RIGHT", which is a different question from
    # "are they PRESENT" (doctor) and "can this be filmed" (validate), and it
    # can only be asked once the run has finished writing. It reconciles every
    # stored issue term against InvestorGain and groups the result by status,
    # so a bad figure on an issue that is open today is the first thing the
    # log says rather than something found a week later by hand.
    "daily": ["sync", "verify", "dedupe", "facts", "enrich", "doctor",
              "build", "validate", "grade"],
    # gmp-sync runs BEFORE the model-based refresh: it is free, keyless and
    # deterministic, so anything it can supply should not cost a Gemini call
    # or need vetting. `gmp` then fills only what InvestorGain did not cover.
    #
    # `build` closes the chain so a night that wrote GMP also gets verified,
    # rather than a broken row waiting until 10:00 to surface. It is `build`
    # and NOT the whole `daily` chain on purpose: this slot fires at 23:45
    # (see the timer for why), and `sync` fifteen minutes before midnight is
    # a bidding day filed under tomorrow the moment the chain runs long.
    # `build` only re-derives what is already stored, so it has no date of
    # its own to get wrong.
    "grey": ["gmp-sync", "gmp", "build"],
}


def job_argv(name: str) -> list[list[str]]:
    """Resolve a job to the list of argv lists it runs."""
    if name in CHAINS:
        return [JOBS[step]["argv"] for step in CHAINS[name]]
    return [JOBS[name]["argv"]]


def expand(names: list[str]) -> list[tuple[str, list[str]]]:
    """Flatten a requested sequence into (label, argv) pairs, in order.

    Manual runs let you pick the order yourself, so this has to preserve
    duplicates and sequence exactly as given — `sync build push build` is a
    legitimate thing to ask for.
    """
    out: list[tuple[str, list[str]]] = []
    for name in names:
        steps = CHAINS.get(name, [name])
        for step in steps:
            out.append((step, JOBS[step]["argv"]))
    return out


# ── runner ─────────────────────────────────────────────────────────────────

class Runner:
    """Runs one job at a time and keeps a tail of its output."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.current: str | None = None
        self.started: float = 0.0
        self.lines: deque[str] = deque(maxlen=LOG_LINES)
        self.history: list[dict] = []
        self.rc: int | None = None

    def busy(self) -> bool:
        return self.current is not None

    def start(self, names: list[str]) -> bool:
        """Queue a sequence. Runs strictly in the order given."""
        with self.lock:
            if self.current:
                return False
            self.current = " → ".join(names)
            self.started = time.time()
            self.rc = None
            self.lines.clear()
        threading.Thread(target=self._run, args=(names,), daemon=True).start()
        return True

    def _emit(self, text: str) -> None:
        self.lines.append(f"{datetime.now():%H:%M:%S}  {text}")

    def _run(self, names: list[str]) -> None:
        rc = 0
        steps = expand(names)
        self._emit(f"── {' → '.join(names)}  ({len(steps)} step"
                   f"{'s' if len(steps) != 1 else ''}) ──")
        try:
            for i, (step, argv) in enumerate(steps, 1):
                self._emit(f"[{i}/{len(steps)}] $ ipopulse {' '.join(argv)}")
                rc = self._spawn(argv)
                if rc != 0:
                    # Stop rather than carry on: a failed sync means the push
                    # behind it would publish stale data as if it were fresh.
                    self._emit(f"! step {i} ({step}) exited {rc} — "
                               f"stopping, {len(steps) - i} step(s) skipped")
                    break
        except Exception as exc:                      # noqa: BLE001
            rc = 1
            self._emit(f"! {type(exc).__name__}: {exc}")
        finally:
            self._emit(f"── finished (exit {rc}) in "
                       f"{time.time() - self.started:.1f}s ──")
            with self.lock:
                self.rc = rc
                self.history.insert(0, {
                    "job": " → ".join(names), "rc": rc,
                    "at": datetime.now().isoformat(timespec="seconds"),
                    "secs": round(time.time() - self.started, 1),
                })
                del self.history[20:]
                self.current = None

    def _spawn(self, argv: list[str]) -> int:
        """Run the CLI as a child process. shell=False, fixed argv."""
        proc = subprocess.Popen(
            [sys.executable, "-m", "ipopulse.cli", *argv],
            cwd=str(_backend_root()),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        for line in proc.stdout:                      # type: ignore[union-attr]
            self._emit(line.rstrip())
        return proc.wait()

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "running": self.current,
                "elapsed": round(time.time() - self.started, 1) if self.current else 0,
                "rc": self.rc,
                "lines": list(self.lines),
                "history": list(self.history),
            }


def _backend_root():
    from .store import BACKEND_ROOT
    return BACKEND_ROOT


# ── auth ───────────────────────────────────────────────────────────────────

class Auth:
    """Password -> short-lived random token."""

    def __init__(self) -> None:
        self.tokens: dict[str, float] = {}
        self.fails: dict[str, list] = {}
        self.lock = threading.Lock()

    @staticmethod
    def password() -> str:
        return os.getenv("IPOPULSE_TRIGGER_PASSWORD") or ""

    def configured(self) -> bool:
        return bool(self.password())

    def locked_for(self, who: str) -> int:
        record = self.fails.get(who)
        if not record:
            return 0
        count, last = record
        if count < MAX_FAILS:
            return 0
        left = int(LOCKOUT - (time.time() - last))
        return max(0, left)

    def login(self, who: str, given: str) -> str | None:
        with self.lock:
            if self.locked_for(who):
                return None
            expected = self.password()
            ok = bool(expected) and hmac.compare_digest(given or "", expected)
            if not ok:
                count, _ = self.fails.get(who, (0, 0.0))
                self.fails[who] = [count + 1, time.time()]
                return None
            self.fails.pop(who, None)
            token = secrets.token_urlsafe(32)
            self.tokens[token] = time.time() + TOKEN_TTL
            return token

    def valid(self, token: str) -> bool:
        with self.lock:
            expiry = self.tokens.get(token or "")
            if not expiry:
                return False
            if time.time() > expiry:
                self.tokens.pop(token, None)
                return False
            return True


RUNNER = Runner()
AUTH = Auth()


# ── request handling ───────────────────────────────────────────────────────

def handle(handler, method: str) -> bool:
    """Serve a control-panel route. Returns False if the path isn't ours.

    Called from the static file server in cli.cmd_serve, so one process and
    one port serve both the studio and this.
    """
    path = handler.path.split("?", 1)[0].rstrip("/") or "/"

    if path == "/trigger":
        if method != "GET":
            return False
        _send(handler, 200, PANEL_HTML, "text/html; charset=utf-8")
        return True

    if not path.startswith("/api/"):
        return False

    # Preflight. A cross-origin POST carrying X-Token is never "simple", so
    # the browser asks permission first and will not send the real request
    # until this answers. Nothing is executed here.
    if method == "OPTIONS":
        handler.send_response(204)
        handler.send_header("Content-Length", "0")
        _cors(handler)
        handler.end_headers()
        return True

    who = _client_ip(handler)

    if path == "/api/health":
        _json(handler, 200, {"ok": True, "auth": AUTH.configured()})
        return True

    if path == "/api/login" and method == "POST":
        body = _body(handler)
        wait = AUTH.locked_for(who)
        if wait:
            _json(handler, 429, {"error": f"Too many attempts. Wait {wait}s."})
            return True
        if not AUTH.configured():
            _json(handler, 500, {
                "error": "IPOPULSE_TRIGGER_PASSWORD is not set in .env — "
                         "nothing can be triggered until it is."})
            return True
        token = AUTH.login(who, str(body.get("password", "")))
        if not token:
            _json(handler, 401, {"error": "Wrong password."})
            return True
        _json(handler, 200, {"token": token, "ttl": TOKEN_TTL})
        return True

    # everything below needs a token
    token = handler.headers.get("X-Token", "")
    if not AUTH.valid(token):
        _json(handler, 401, {"error": "Not signed in."})
        return True

    if path == "/api/jobs":
        _json(handler, 200, {"jobs": [
            {"id": k, "label": v["label"], "detail": v["detail"],
             "schedule": v["schedule"]}
            for k, v in JOBS.items()
        ]})
        return True

    if path == "/api/status":
        _json(handler, 200, RUNNER.snapshot())
        return True

    # Narration for the script the studio is showing.
    #
    # It takes the TEXT rather than a slug and a reel number, and that is not
    # laziness: every script in this project is generated in output.js and has
    # no Python equivalent, so the server genuinely does not know what reel 2
    # of Augmont says. See voice.py's header for why porting it would be the
    # wrong trade.
    #
    # Behind the same token as /api/run, because unlike the jobs this one
    # spends money per call.
    if path == "/api/voice" and method == "POST":
        from . import voice as tts

        body = _body(handler)
        try:
            audio, hit, used, fmt = tts.synthesize(
                str(body.get("text", "")),
                # Lets the studio's compare button pin one provider; omitted,
                # the configured order decides.
                provider=str(body.get("provider", "") or ""),
                # `lang` selects the voice and the model server-side. Sending
                # the language rather than a resolved voice id keeps the
                # "which voice reads Telugu" policy in one file instead of
                # mirrored into studio.js, where it would drift.
                lang=str(body.get("lang", "") or ""),
                vid=str(body.get("voice_id", "") or ""),
                mdl=str(body.get("model", "") or ""),
                settings=body.get("settings") if isinstance(
                    body.get("settings"), dict) else None,
                force=bool(body.get("force")))
        except tts.VoiceError as err:
            # 400, not 500: every one of these is something the caller can fix
            # — an empty script, a missing key, a wrong voice id, a spent
            # budget — and a 500 would read as "the server is broken".
            _json(handler, 400, {"error": str(err)})
            return True

        left = tts.budget()
        # Gemini returns wav, ElevenLabs mp3 — so the content type is whatever
        # actually came back, and the studio is told the extension separately so
        # it can name a download correctly.
        ctype = "audio/wav" if fmt == "wav" else "audio/mpeg"
        _bytes(handler, 200, audio, ctype, {
            "X-Voice-Cached": "1" if hit else "0",
            # 0 on a cache hit, because a hit is not billed. The studio shows
            # this so a re-render visibly costs nothing. Gemini also bills
            # nothing, but its characters still count against a daily free
            # quota, so they are reported rather than zeroed.
            "X-Voice-Chars": "0" if hit else str(len(str(body.get("text", "")))),
            "X-Voice-Left": str(left["left"]),
            "X-Voice-Provider": used,
            "X-Voice-Format": fmt,
        })
        return True

    if path == "/api/voice/status":
        from . import voice as tts

        _json(handler, 200, {
            "configured": tts.configured(),
            "budget": tts.budget(),
            # Key COUNT, never the keys. The studio only needs to know whether
            # a rotation exists to explain a fallback in its status line.
            "keys": len(tts.api_keys()),
            # Per language, so the studio can show which voice and model each
            # one will actually use rather than implying they share — and, more
            # importantly, whether that model can speak that language at all.
            "plan": tts.plan(),
        })
        return True

    # ── YouTube: publish one reel from the studio ──────────────────────────
    #
    # The browser cannot upload to YouTube itself and should not be able to.
    # Doing so would mean a refresh token — a credential that can upload, edit
    # and delete on the channel — living in page JavaScript, on a site that is
    # also published to GitHub Pages. So the studio collects the details and
    # this process does the work.
    #
    # Which means the button only exists where this process does: a local
    # `ipopulse serve`. On the public Pages site there is no /api at all, and
    # `/api/youtube/status` simply never answers — the panel says so rather
    # than offering a button that cannot work.

    if path == "/api/youtube/status":
        from . import pubqueue as q
        from . import youtube_upload as yt
        _json(handler, 200, {
            "configured": yt.configured(),   # is there an OAuth client?
            "authorised": yt.authorised(),   # has someone clicked allow?
            "queue": q.summary(),
        })
        return True

    if path == "/api/youtube/publish" and method == "POST":
        import base64
        import tempfile
        from pathlib import Path

        from . import pubqueue as q
        from . import youtube_upload as yt

        body = _body(handler)
        slug = str(body.get("slug") or "").strip()
        reel = int(body.get("reel") or 0)
        lang = str(body.get("lang") or "en").strip()
        title = str(body.get("title") or "").strip()
        desc = str(body.get("description") or "")
        privacy = str(body.get("privacy") or "unlisted").strip()
        tags = [str(t) for t in (body.get("tags") or [])][:40]
        dry = bool(body.get("dry_run"))

        if not (slug and reel and title):
            _json(handler, 400,
                  {"error": "slug, reel and title are all required."})
            return True
        if privacy not in ("private", "unlisted", "public"):
            _json(handler, 400, {"error": f"bad visibility: {privacy}"})
            return True

        # The optional narration, sent as base64 by the panel's file picker.
        #
        # Written to a temp file rather than streamed: ffmpeg needs a seekable
        # path, and a reel's narration is a few hundred kilobytes — small
        # enough that the simple thing is also the right one.
        audio_path = None
        blob = body.get("audio_b64") or ""
        if blob:
            try:
                raw = base64.b64decode(blob.split(",")[-1], validate=False)
            except Exception:
                _json(handler, 400,
                      {"error": "the audio did not decode; re-pick the file."})
                return True
            if len(raw) > 40 * 1024 * 1024:
                _json(handler, 400, {"error": "audio over 40 MB — that is not "
                                              "narration for a Short."})
                return True
            fd, tmp = tempfile.mkstemp(suffix=".mp3")
            os.close(fd)
            Path(tmp).write_bytes(raw)
            audio_path = Path(tmp)

        try:
            # ── render, with the generated cards and whatever audio came in
            sys.path.insert(0, str(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))))
            from tools import render as renderer          # noqa: E402

            from . import art, store
            try:
                ipo = store.load(slug)
                opener, endcard = art.for_reel(ipo, reel, lang)
                company = ipo.company or slug
            except Exception:
                opener = endcard = None
                company = slug

            out = (store.OUT_DIR / "video" /
                   f"{slug}-r{reel}-{lang}.mp4")
            httpd, tmpdir, url = renderer._serve_gateless(port=8772)
            try:
                got = renderer.render(url, slug, reel, lang, out,
                                      audio=audio_path, opener=opener,
                                      endcard=endcard)
            finally:
                httpd.shutdown()
                import shutil as _sh
                _sh.rmtree(tmpdir, ignore_errors=True)

            # ── queue it, then approve it with the visibility the panel chose
            #
            # Still goes through the queue even though it is about to upload,
            # and that is deliberate: the queue is the record of what was
            # published, with what title, at whose instruction. A path that
            # skipped it would leave uploads with no history.
            item = q.add(slug=slug, reel=reel, lang=lang, video=out,
                         company=company, seconds=got["seconds"],
                         title=title, description=desc, tags=tags,
                         notes="published from the studio")
            if dry:
                _json(handler, 200, {
                    "dry_run": True, "id": item["id"],
                    "video": str(out), "seconds": got["seconds"],
                    "scenes": got["scenes"], "privacy": privacy,
                    "audio": bool(audio_path),
                })
                return True

            if not yt.authorised():
                _json(handler, 400, {
                    "error": "Not authorised with YouTube yet. Run "
                             "`ipopulse publish --authorise` once in a "
                             "terminal, then try again.",
                    "id": item["id"], "video": str(out)})
                return True

            q.approve(item["id"], privacy=privacy)
            sent = yt.upload(out, title=title, description=desc, tags=tags,
                             privacy=privacy)
            q.mark_uploaded(item["id"], sent["id"], sent["url"])
            _json(handler, 200, {
                "ok": True, "id": item["id"], "video_id": sent["id"],
                "url": sent["url"], "privacy": sent["privacy"],
                "thumbnail": sent.get("thumbnail"),
                "thumbnail_error": sent.get("thumbnail_error", ""),
                "seconds": got["seconds"],
            })
        except Exception as err:
            _json(handler, 400, {"error": str(err)[:400]})
        finally:
            if audio_path and audio_path.exists():
                try:
                    audio_path.unlink()
                except OSError:
                    pass
        return True


    if path == "/api/run" and method == "POST":
        body = _body(handler)
        # {"job": "sync"} for one, {"jobs": [...]} for a sequence in order.
        raw = body.get("jobs") if body.get("jobs") is not None else body.get("job")
        names = [str(n) for n in raw] if isinstance(raw, list) else [str(raw or "")]
        names = [n for n in names if n]
        if not names:
            _json(handler, 400, {"error": "No job given."})
            return True
        unknown = [n for n in names if n not in JOBS]
        if unknown:
            _json(handler, 400, {"error": f"Unknown job(s): {', '.join(unknown)}"})
            return True
        if len(names) > 20:
            _json(handler, 400, {"error": "Sequence too long (max 20)."})
            return True
        if not RUNNER.start(names):
            _json(handler, 409, {"error": f"{RUNNER.current} is still running."})
            return True
        _json(handler, 200, {"started": names, "steps": len(expand(names))})
        return True

    _json(handler, 404, {"error": "No such endpoint"})
    return True


def _body(handler) -> dict:
    try:
        length = int(handler.headers.get("Content-Length") or 0)
        return json.loads(handler.rfile.read(length) or "{}")
    except (ValueError, json.JSONDecodeError):
        return {}


def _client_ip(handler) -> str:
    """Who is calling, for the failed-login lockout.

    Behind a hosted proxy every request arrives from the proxy, so
    `client_address` is one shared value and five wrong guesses would lock
    out everyone at once. X-Forwarded-For carries the real caller; take the
    left-most entry, which is the client the edge saw.

    Only trusted when IPOPULSE_TRUST_PROXY is set, because the header is
    caller-supplied: on a directly-exposed server anyone could send a fresh
    one per attempt and walk straight through the lockout.
    """
    if os.getenv("IPOPULSE_TRUST_PROXY"):
        fwd = handler.headers.get("X-Forwarded-For") or ""
        first = fwd.split(",")[0].strip()
        if first:
            return first
    return handler.client_address[0]


def allowed_origins() -> list[str]:
    """Origins permitted to call /api/*, from IPOPULSE_ALLOWED_ORIGINS.

    Deliberately a list and never `*`. These endpoints start jobs, and a
    wildcard would let any page on the internet put a run request in front
    of a browser that already holds a valid token.
    """
    raw = os.getenv("IPOPULSE_ALLOWED_ORIGINS") or ""
    return [o.strip().rstrip("/") for o in raw.split(",") if o.strip()]


def _cors(handler) -> None:
    """Echo the caller's origin when it is on the allow-list.

    Echoing rather than sending the whole list is what the spec requires:
    Access-Control-Allow-Origin takes exactly one origin, and `Vary: Origin`
    stops a cache handing one site's response to another.
    """
    origin = (handler.headers.get("Origin") or "").rstrip("/")
    if origin and origin in allowed_origins():
        handler.send_header("Access-Control-Allow-Origin", origin)
        handler.send_header("Vary", "Origin")
        handler.send_header("Access-Control-Allow-Headers", "Content-Type, X-Token")
        handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        handler.send_header("Access-Control-Max-Age", "600")
        # Without this, a cross-origin caller can read the mp3 body but not
        # these — the browser hides every response header that is not on the
        # CORS-safelist unless it is named here. /api/voice reports the cache
        # hit and the spend in headers, so the studio would show "0 characters,
        # not cached" for every render and quietly look broken.
        handler.send_header(
            "Access-Control-Expose-Headers",
            "X-Voice-Cached, X-Voice-Chars, X-Voice-Left, "
            "X-Voice-Provider, X-Voice-Format")


def _send(handler, code: int, text: str, ctype: str) -> None:
    raw = text.encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Cache-Control", "no-store")
    _cors(handler)
    handler.end_headers()
    handler.wfile.write(raw)


def _json(handler, code: int, payload: dict) -> None:
    _send(handler, code, json.dumps(payload), "application/json")


def _bytes(handler, code: int, raw: bytes, ctype: str,
           extra: dict[str, str] | None = None) -> None:
    """_send for a body that is not text. Only /api/voice needs it so far.

    Kept separate rather than widening _send: that one encodes utf-8, and an
    mp3 that survived a str round-trip would be a corrupt file rather than an
    error, which is the worst kind of bug to ship.
    """
    handler.send_response(code)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Cache-Control", "no-store")
    for name, value in (extra or {}).items():
        handler.send_header(name, value)
    _cors(handler)
    handler.end_headers()
    handler.wfile.write(raw)


PANEL_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>IPO Pulse — Trigger</title>
<style>
  :root{--bg:#0A0F1C;--card:#121A2B;--line:#22304a;--tx:#E6EDF7;--dim:#8CA0BF;
        --acc:#22C55E;--bad:#EF4444;--warn:#F59E0B}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--tx);
       font:14px/1.5 ui-sans-serif,system-ui,Inter,sans-serif}
  .wrap{max-width:980px;margin:0 auto;padding:28px 18px 60px}
  h1{font-size:19px;margin:0 0 2px;letter-spacing:-.01em}
  .sub{color:var(--dim);font-size:12.5px;margin-bottom:22px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
  input{background:#0B1220;border:1px solid var(--line);color:var(--tx);
        border-radius:8px;padding:9px 11px;font-size:14px;width:100%}
  button{background:var(--acc);color:#04140A;border:0;border-radius:8px;
         padding:9px 15px;font-weight:800;font-size:13px;cursor:pointer}
  button:disabled{opacity:.45;cursor:not-allowed}
  button.ghost{background:#1B2740;color:var(--tx)}
  .grid{display:grid;gap:10px;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));
        margin-top:16px}
  .job{background:var(--card);border:1px solid var(--line);border-radius:11px;padding:13px}
  .job h3{margin:0 0 3px;font-size:13.5px}
  .job p{margin:0 0 10px;color:var(--dim);font-size:12px;min-height:32px}
  .when{color:#6F86A8;font-size:11px;font-family:ui-monospace,monospace}
  .row{display:flex;gap:9px;align-items:center}
  .queue{background:var(--card);border:1px dashed var(--line);border-radius:11px;
         padding:12px 13px;margin-top:14px}
  .queue.armed{border-style:solid;border-color:#2F6B45}
  .chips{display:flex;flex-wrap:wrap;gap:7px;align-items:center;min-height:28px}
  .chip{display:inline-flex;align-items:center;gap:6px;background:#1B2740;
        border:1px solid var(--line);border-radius:99px;padding:3px 5px 3px 10px;
        font-size:12px;font-family:ui-monospace,monospace}
  .chip b{color:var(--acc);font-weight:800}
  .chip button{background:transparent;color:var(--dim);padding:0 5px;font-size:14px;
               line-height:1;border-radius:99px}
  .chip button:hover{color:var(--bad)}
  .hint{color:var(--dim);font-size:12px}
  .mini{background:#1B2740;color:var(--tx);padding:5px 10px;font-size:11.5px}
  pre{background:#070C16;border:1px solid var(--line);border-radius:10px;padding:12px;
      max-height:340px;overflow:auto;font:12px/1.55 ui-monospace,SFMono-Regular,monospace;
      white-space:pre-wrap;margin:12px 0 0}
  .err{color:var(--bad);font-size:12.5px;margin-top:9px;min-height:17px}
  .pill{font-size:11px;padding:2px 8px;border-radius:99px;background:#1B2740;color:var(--dim)}
  .on{background:#0E2F1C;color:var(--acc)}
  .hide{display:none}
</style></head><body><div class="wrap">

<h1>IPO Pulse — Trigger</h1>
<div class="sub">Manual runs of the same jobs the timers run. Served locally; never published.</div>

<div id="gate" class="card">
  <div class="row">
    <input id="pw" type="password" placeholder="Password (IPOPULSE_TRIGGER_PASSWORD)"
           autocomplete="current-password">
    <button id="go">Unlock</button>
  </div>
  <div class="err" id="gateErr"></div>
</div>

<div id="panel" class="hide">
  <div class="row" style="justify-content:space-between">
    <span class="pill" id="state">idle</span>
    <button class="ghost" id="out">Sign out</button>
  </div>
  <div class="queue" id="queue">
    <div class="row" style="justify-content:space-between;margin-bottom:8px">
      <strong style="font-size:12.5px">Run in order</strong>
      <div class="row">
        <button class="mini" id="runSeq" disabled>Run sequence</button>
        <button class="mini" id="clearSeq" disabled>Clear</button>
      </div>
    </div>
    <div class="chips" id="chips">
      <span class="hint">Press <b>+ Queue</b> on any job to build a sequence.
        Steps run one after another, in this order, and stop at the first failure.</span>
    </div>
  </div>

  <div class="grid" id="jobs"></div>
  <pre id="log">No output yet — trigger a job above.</pre>
</div>

<script>
const $ = s => document.querySelector(s);
let token = sessionStorage.getItem('ipopulse-token') || '';
let poll = null;

async function api(path, opts={}) {
  const r = await fetch(path, {...opts, headers:{
    'Content-Type':'application/json', 'X-Token':token, ...(opts.headers||{})}});
  const body = await r.json().catch(()=>({}));
  if (!r.ok) throw new Error(body.error || ('HTTP '+r.status));
  return body;
}

async function unlock() {
  $('#gateErr').textContent = '';
  try {
    const r = await api('/api/login', {method:'POST',
      body: JSON.stringify({password: $('#pw').value})});
    token = r.token; sessionStorage.setItem('ipopulse-token', token);
    $('#pw').value = '';
    await enter();
  } catch (e) { $('#gateErr').textContent = e.message; }
}

let seq = [];   // the manual ordering — duplicates allowed on purpose

async function enter() {
  const {jobs} = await api('/api/jobs');
  $('#gate').classList.add('hide');
  $('#panel').classList.remove('hide');
  $('#jobs').innerHTML = jobs.map(j => `
    <div class="job">
      <h3>${j.label}</h3>
      <p>${j.detail}</p>
      <div class="row" style="justify-content:space-between">
        <div class="row">
          <button data-run="${j.id}">Run</button>
          <button class="mini" data-add="${j.id}">+ Queue</button>
        </div>
        <span class="when">${j.schedule}</span>
      </div>
    </div>`).join('');
  $('#jobs').querySelectorAll('[data-run]').forEach(b =>
    b.onclick = () => run([b.dataset.run]));
  $('#jobs').querySelectorAll('[data-add]').forEach(b =>
    b.onclick = () => { seq.push(b.dataset.add); drawSeq(); });
  drawSeq();
  if (!poll) poll = setInterval(tick, 1200);
  tick();
}

function drawSeq() {
  const box = $('#chips');
  $('#queue').classList.toggle('armed', seq.length > 0);
  $('#runSeq').disabled = !seq.length;
  $('#clearSeq').disabled = !seq.length;
  if (!seq.length) {
    box.innerHTML = '<span class="hint">Press <b>+ Queue</b> on any job to build a '
      + 'sequence. Steps run one after another, in this order, and stop at the '
      + 'first failure.</span>';
    return;
  }
  box.innerHTML = seq.map((id, i) =>
    `<span class="chip"><b>${i + 1}</b> ${id}<button data-i="${i}" title="Remove">×</button></span>`
    + (i < seq.length - 1 ? '<span class="hint">→</span>' : '')).join('');
  box.querySelectorAll('button').forEach(b =>
    b.onclick = () => { seq.splice(+b.dataset.i, 1); drawSeq(); });
}

async function run(jobs) {
  try {
    await api('/api/run', {method:'POST', body: JSON.stringify({jobs})});
    tick();
  } catch (e) { $('#log').textContent = e.message; }
}

async function tick() {
  let s;
  try { s = await api('/api/status'); }
  catch (e) { if (String(e.message).includes('signed in')) signOut(); return; }
  const busy = !!s.running;
  $('#state').textContent = busy ? `running ${s.running} · ${s.elapsed}s`
    : (s.rc === null ? 'idle' : `last exit ${s.rc}`);
  $('#state').className = 'pill' + (busy ? ' on' : '');
  // Only the launchers lock while something runs — queueing the next sequence
  // is exactly what you want to be doing during a long sync.
  $('#jobs').querySelectorAll('[data-run]').forEach(b => b.disabled = busy);
  $('#runSeq').disabled = busy || !seq.length;
  if (s.lines.length) {
    const box = $('#log'), stick = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
    box.textContent = s.lines.join('\\n');
    if (stick) box.scrollTop = box.scrollHeight;
  }
}

function signOut() {
  token=''; sessionStorage.removeItem('ipopulse-token');
  clearInterval(poll); poll=null;
  $('#panel').classList.add('hide'); $('#gate').classList.remove('hide');
}

$('#go').onclick = unlock;
$('#pw').addEventListener('keydown', e => { if (e.key === 'Enter') unlock(); });
$('#out').onclick = signOut;
$('#runSeq').onclick = () => run(seq);
$('#clearSeq').onclick = () => { seq = []; drawSeq(); };
if (token) enter().catch(signOut);
</script></div></body></html>
"""
