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
        "label": "GMP from ipoji",
        "detail": "Free, keyless, no AI. Today's board plus any missing days "
                  "from each IPO's dated page. Fills gaps, never overwrites.",
        "argv": ["gmp-sync", "--history", "--write"],
        "schedule": "part of grey",
    },
    "gmp": {
        "label": "Refresh GMP",
        "detail": "Grey-market premium via Gemini grounded search. Needs billing enabled.",
        "argv": ["refresh"],
        "schedule": "part of grey",
    },
    "translate": {
        "label": "Translate",
        "detail": "Gemini → Hindi and Telugu, written into the YAML. Cached 30 days.",
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
        "schedule": "part of daily",
    },
    # There is no `push` job any more. It existed to copy a local store up
    # into the Google Sheet; the sheet IS the store now, so pushing it to
    # itself is a no-op that could only ever lose data by rewriting rows the
    # run had not read. `import` still exists for pulling an OUTSIDE
    # spreadsheet in — that is a different direction and still useful.
    "report": {
        "label": "Excel report",
        "detail": "Formatted workbook into backend/out/.",
        "argv": ["report"],
        "schedule": "Sun 04:00 IST",
    },
    "daily": {
        "label": "Daily chain",
        "detail": "sync → enrich → doctor → build. The scheduled one; "
                  "run this if you run one.",
        "argv": None,                       # composite; see CHAINS
        "schedule": "13:00 & 16:30 Mon-Fri, 18:00 daily",
    },
    "grey": {
        "label": "GMP chain",
        "detail": "free keyless GMP, then the model fills what it missed.",
        "argv": None,
        "schedule": "21:00 IST daily",
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
# Its budget is small (6 calls) precisely because the chain runs three times a
# day: the work spreads across runs instead of exhausting the free tier in one.
CHAINS = {
    "daily": ["sync", "enrich", "doctor", "build"],
    # gmp-sync runs BEFORE the model-based refresh: it is free, keyless and
    # deterministic, so anything it can supply should not cost a Gemini call
    # or need vetting. `gmp` then fills only what ipoji did not cover.
    "grey": ["gmp-sync", "gmp"],
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

    who = handler.client_address[0]

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


def _send(handler, code: int, text: str, ctype: str) -> None:
    raw = text.encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(raw)


def _json(handler, code: int, payload: dict) -> None:
    _send(handler, code, json.dumps(payload), "application/json")


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
