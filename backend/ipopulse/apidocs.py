"""The API explorer — a Swagger-shaped page for this server's own endpoints.

Served at ``/api-docs`` by ``control.handle``, from the same process and the
same port as the studio and ``/trigger``. Nothing is fetched from a CDN and
there is no OpenAPI toolchain in the dependency list: this is one page, twelve
endpoints, and a spec that lives beside the handlers it describes.

── Why not Swagger UI proper ──────────────────────────────────────────────

Swagger UI is 1.4 MB of JavaScript from a CDN, and this project deliberately
ships a page that works with no network beyond the sheet. It also assumes an
OpenAPI document, which would be a second description of these endpoints free
to drift from ``control.handle`` — the same "one more copy of the truth"
problem the store had. ``ENDPOINTS`` below sits in the same package as the
handler and is small enough to read in one screen.

── The part Swagger gets wrong for this API ───────────────────────────────

"Try it out" on a read endpoint is free. On THIS API four of the twelve spend
something that cannot be taken back:

    POST /api/run              starts a pipeline job that rewrites sheet tabs
    POST /api/video/open       starts a process on the machine
    POST /api/voice            spends TTS credit / daily quota
    POST /api/youtube/publish  renders and UPLOADS to the channel

So every endpoint carries an ``effect`` and the page treats them differently:
read endpoints send on one click; ``spend`` shows what it will cost; ``write``
asks for confirmation; ``publish`` is pinned to ``dry_run: true`` and only
lets go of it when the word UPLOAD is typed. A docs page that makes it easy to
publish a video by accident is worse than no docs page.

── Auth ───────────────────────────────────────────────────────────────────

Same door as everything else: POST /api/login with IPOPULSE_TRIGGER_PASSWORD,
get a token, send it as ``X-Token``. The page holds that token in memory only —
never localStorage — for the same reason studio.js does not: it unlocks a job
runner, and a tab that is closed should not leave one behind.
"""

from __future__ import annotations

import json
from typing import Any

# ── the spec ───────────────────────────────────────────────────────────────
#
# `effect` drives the colour and the guard rails:
#
#   read     nothing changes; send it
#   spend    costs API credit or quota
#   write    changes the Google Sheet
#   publish  puts a video on the channel
#
# Keep this list in the same order as the branches in `control.handle`, so a
# new endpoint is one entry here next to the code it documents.

ENDPOINTS: list[dict[str, Any]] = [
    {
        "method": "GET", "path": "/api/health", "auth": False, "effect": "read",
        "summary": "Is the backend alive, and is it usable?",
        "why": "The studio calls this on load to decide whether a backend exists "
               "at all. `auth: false` means no IPOPULSE_TRIGGER_PASSWORD is set — "
               "and the studio then treats the backend as unusable, because every "
               "endpoint that does anything needs a token it could never get.",
        "params": [],
        "returns": {"ok": True, "auth": True},
    },
    {
        "method": "POST", "path": "/api/login", "auth": False, "effect": "read",
        "summary": "Password in, short-lived token out.",
        "why": "The token is what every endpoint below wants in an X-Token header. "
               "It lasts 8 hours. Five wrong passwords lock that client address out "
               "for 5 minutes — the lockout is per caller, and X-Forwarded-For is "
               "only believed when IPOPULSE_TRUST_PROXY is set.",
        "params": [
            {"name": "password", "in": "body", "type": "string", "required": True,
             "note": "IPOPULSE_TRIGGER_PASSWORD from .env (or the GitHub secret on the VM)"},
        ],
        "returns": {"token": "…", "ttl": 28800},
    },
    {
        "method": "GET", "path": "/api/jobs", "auth": True, "effect": "read",
        "summary": "Every named job and when it is scheduled.",
        "why": "The same dict the schedulers and the Trigger panel resolve names "
               "through (control.JOBS), so this list cannot drift from what will "
               "actually run.",
        "params": [],
        "returns": {"jobs": [{"id": "daily", "label": "Daily chain",
                              "detail": "…", "schedule": "10:00 & 18:35 IST daily"}]},
    },
    {
        "method": "GET", "path": "/api/status", "auth": True, "effect": "read",
        "summary": "What the runner is doing, and its recent log.",
        "why": "Poll this after POST /api/run. `running` is null when idle; `rc` is "
               "the exit code of the last step; `lines` is the tail of its output.",
        "params": [],
        "returns": {"running": "daily", "elapsed": 12.4, "rc": None,
                    "lines": ["…"], "history": []},
    },
    {
        "method": "GET", "path": "/api/check", "auth": True, "effect": "read",
        "summary": "The watchdog sweep: is anything wrong with the data right now?",
        "why": "Re-runs the insertion-time rules from invariants.py against what "
               "is ALREADY stored — those only ever see a record at the moment "
               "it is written, so a row that predates a rule was never checked "
               "by it — plus staleness, self-contradictions, and our Gemini "
               "spend against the free-tier caps. Always 200: the findings are "
               "the answer, and a 5xx here would make an uptime monitor page "
               "somebody over a stale premium. `ok: false` means at least one "
               "error-level finding. Same sweep the scheduled workflow turns "
               "into a tagged issue, and the same one `ipopulse check` prints.",
        "params": [
            {"name": "full", "in": "query", "type": "1", "required": False,
             "note": "also run `grade`, which calls InvestorGain once per "
                     "tracked IPO — tens of seconds. Omitted, that one check "
                     "is skipped so the request answers inside a browser's "
                     "patience"},
        ],
        "returns": {"ok": False, "at": "2026-09-06T19:38:07",
                    "checks_run": ["usage", "models", "invariants", "monitor"],
                    "counts": {"error": 1, "warn": 8},
                    "findings": [{"severity": "error", "source": "monitor",
                                  "slug": "…", "what": "…", "detail": "…"}],
                    "usage": {"models": {}, "scope": "this machine only…"}},
    },
    {
        "method": "POST", "path": "/api/run", "auth": True, "effect": "write",
        "summary": "Start a job, or a sequence of them, in order.",
        "why": "This is the Trigger panel's button. It rewrites Google Sheet tabs, "
               "and a save clears a tab before rewriting it with no lock in "
               "between — so never start one while a scheduled Actions run may be "
               "mid-write. 409 means one is already running.",
        "params": [
            {"name": "job", "in": "body", "type": "string", "required": False,
             "note": "one job id, e.g. \"gmp-sync\""},
            {"name": "jobs", "in": "body", "type": "string[]", "required": False,
             "note": "a sequence, run in order, max 20. Takes precedence over `job`"},
        ],
        "returns": {"started": ["gmp-sync"], "steps": 2},
    },
    {
        "method": "GET", "path": "/api/voice/status", "auth": True, "effect": "read",
        "summary": "Which TTS provider will speak, and what is left of the budget.",
        "why": "`plan` is per language, because the question that matters is not "
               "\"is a key configured\" but \"can this model speak Telugu at all\". "
               "`keys` is a count, never the keys.",
        "params": [],
        "returns": {"configured": True, "budget": {"left": 9000},
                    "keys": 2, "plan": {"en": {}, "hi": {}, "te": {}}},
    },
    {
        "method": "POST", "path": "/api/voice", "auth": True, "effect": "spend",
        "summary": "Synthesise narration. Returns audio bytes, not JSON.",
        "why": "Billed per character on ElevenLabs and counted against a daily free "
               "quota on Gemini — but cached, so re-sending an unchanged script "
               "costs nothing and comes back with X-Voice-Cached: 1. The response "
               "is wav or mp3; read X-Voice-Format to know which.",
        "params": [
            {"name": "text", "in": "body", "type": "string", "required": True,
             "note": "the script, exactly as the studio shows it — do not \"fix\" "
                     "\"105 rupees\" back to \"₹105\", it is written that way for the model"},
            {"name": "lang", "in": "body", "type": "en | hi | te", "required": False,
             "default": "en", "note": "selects the voice AND the model, server-side"},
            {"name": "provider", "in": "body", "type": "string", "required": False,
             "note": "pin one provider; omitted, the configured order decides"},
            {"name": "voice_id", "in": "body", "type": "string", "required": False},
            {"name": "model", "in": "body", "type": "string", "required": False},
            {"name": "force", "in": "body", "type": "boolean", "required": False,
             "default": False, "note": "ignore the cache and re-synthesise (this one bills)"},
        ],
        "returns": "audio/mpeg or audio/wav + X-Voice-Cached, X-Voice-Chars, "
                   "X-Voice-Left, X-Voice-Provider, X-Voice-Format",
    },
    {
        "method": "GET", "path": "/api/video", "auth": True, "effect": "read",
        "summary": "Stream a rendered video, so the panel can play it.",
        "why": "A page cannot read a local path, so the file is served here "
               "rather than linked as file://. Whole body, no Range support: "
               "these are 2-20 MB files on localhost and seeking simply "
               "re-fetches. `name` is a BASENAME under out/video and nothing "
               "else - see the note on POST /api/video/open for why that rule "
               "is strict.",
        "params": [
            {"name": "name", "in": "query", "type": "string", "required": True,
             "note": "e.g. \"rays-of-belief-r2-en.mp4\". Anything with a path "
                     "separator, a leading dot, or a suffix other than .mp4 is "
                     "refused with 400"},
        ],
        "returns": "video/mp4 bytes, or 400 for a bad name / 404 if not rendered yet",
    },
    {
        "method": "POST", "path": "/api/video/open", "auth": True, "effect": "write",
        "summary": "Hand a rendered video to Clipchamp, and reveal it in Explorer.",
        "why": "The other half of the render-edit-upload loop: the panel can "
               "render an mp4 but had no way to let you touch it, so any hand "
               "edit meant leaving the studio and uploading through YouTube "
               "Studio by hand. This launches the OS `edit` verb (which is what "
               "Clipchamp and Photos register against .mp4) and falls back to "
               "the plain open verb, reporting which it used rather than "
               "pretending. It always reveals the file too, so right-click -> "
               "Open with is available when nothing claims the verb.\n\n"
               "Marked `write` because it starts a process on the machine. "
               "`name` is validated exactly as GET /api/video does: it is a "
               "browser-supplied string that reaches the shell, and a name of "
               "`../../.env` must never resolve.",
        "params": [
            {"name": "name", "in": "body", "type": "string", "required": True,
             "note": "basename of a file in out/video"},
        ],
        "returns": {"ok": True, "file": "…-r2-en.mp4", "dir": "…/out/video",
                    "revealed": ["revealed in Explorer"], "verb": "edit",
                    "note": "Edit it, then export over the same file…"},
    },
    {
        "method": "GET", "path": "/api/youtube/status", "auth": True, "effect": "read",
        "summary": "Is a channel connected, and what is in the publish queue?",
        "why": "`configured` means an OAuth client exists; `authorised` means "
               "someone has actually clicked allow. Uploading needs both — run "
               "`ipopulse publish --authorise` once if authorised is false.",
        "params": [],
        "returns": {"configured": True, "authorised": False,
                    "queue": {"queued": 0, "approved": 0, "uploaded": 0,
                              "failed": 1, "rejected": 0}},
    },
    {
        "method": "POST", "path": "/api/youtube/publish", "auth": True,
        "effect": "publish",
        "summary": "Render one reel and upload it to the channel.",
        "why": "The studio's ▶ YouTube button. It renders the reel headlessly "
               "(about a minute), files it in the publish queue as the record of "
               "what went out, approves it with the visibility given, and uploads. "
               "With dry_run it stops after rendering and queueing — nothing "
               "reaches YouTube.",
        "params": [
            {"name": "slug", "in": "body", "type": "string", "required": True,
             "note": "the IPO's row name, e.g. \"rays-of-belief\""},
            {"name": "reel", "in": "body", "type": "integer", "required": True,
             "note": "1–6"},
            {"name": "lang", "in": "body", "type": "en | hi | te", "required": False,
             "default": "en"},
            {"name": "title", "in": "body", "type": "string", "required": True,
             "note": "max 100 characters, like YouTube itself"},
            {"name": "description", "in": "body", "type": "string", "required": False},
            {"name": "tags", "in": "body", "type": "string[]", "required": False,
             "note": "first 40 are kept"},
            {"name": "privacy", "in": "body", "type": "private | unlisted | public",
             "required": False, "default": "unlisted"},
            {"name": "audio_b64", "in": "body", "type": "string", "required": False,
             "note": "narration as a data URL or bare base64, max 40 MB. Without it "
                     "the reel is uploaded silent"},
            {"name": "video_name", "in": "body", "type": "string", "required": False,
             "note": "a file in out/video to upload AS IS, skipping the render "
                     "entirely. This is how an edit survives: render once, open "
                     "it in Clipchamp, export over the same name, then send it "
                     "back here. Same basename validation as /api/video"},
            {"name": "dry_run", "in": "body", "type": "boolean", "required": False,
             "default": True, "note": "render and queue, do not upload"},
        ],
        "returns": {"ok": True, "id": "slug-r5-en", "video_id": "…",
                    "url": "https://youtu.be/…", "privacy": "unlisted",
                    "seconds": 48.6},
    },
]


def _spec() -> str:
    return json.dumps(ENDPOINTS, ensure_ascii=False)


def page() -> str:
    """The explorer, with the spec baked in."""
    return PAGE.replace("/*__SPEC__*/[]", _spec())


PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>IPO Pulse — API</title>
<style>
  :root{--bg:#060A14;--panel:#0A0F1C;--panel2:#0F172A;--line:rgba(255,255,255,.10);
        --ink:#E2E8F0;--dim:#94A3B8;--faint:#64748B;--pulse:#22C55E;--warn:#F59E0B;
        --danger:#EF4444;--sky:#60A5FA;--violet:#A78BFA;
        --mono:ui-monospace,"Cascadia Mono","SF Mono",Consolas,monospace}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
       font:14px/1.6 ui-sans-serif,system-ui,"Segoe UI",Roboto,sans-serif}
  a{color:var(--sky)}
  code,kbd{font-family:var(--mono)}
  /* header */
  header{position:sticky;top:0;z-index:20;background:rgba(6,10,20,.92);
         backdrop-filter:blur(8px);border-bottom:1px solid var(--line);
         padding:.7rem 1.1rem;display:flex;align-items:center;gap:.8rem;flex-wrap:wrap}
  .brand{font-weight:900;letter-spacing:-.02em;font-size:14px;white-space:nowrap}
  .brand span{color:var(--pulse)}
  .brand small{display:block;font-size:9.5px;letter-spacing:.16em;color:var(--faint);
               font-weight:700;text-transform:uppercase}
  .grow{flex:1}
  input,select,textarea{background:#020509;border:1px solid var(--line);color:var(--ink);
        border-radius:7px;padding:.35rem .55rem;font:12.5px var(--mono);outline:none}
  input:focus,textarea:focus,select:focus{border-color:rgba(34,197,94,.55)}
  textarea{width:100%;line-height:1.55;resize:vertical}
  .btn{background:rgba(255,255,255,.07);border:1px solid var(--line);color:var(--ink);
       border-radius:7px;padding:.35rem .7rem;font-size:12.5px;font-weight:700;
       cursor:pointer;white-space:nowrap}
  .btn:hover{background:rgba(255,255,255,.13)}
  .btn:disabled{opacity:.4;cursor:not-allowed}
  .btn-go{background:var(--pulse);border-color:var(--pulse);color:#04140A}
  .btn-red{background:#dc2626;border-color:#ef4444;color:#fff}
  .pill{font-size:10.5px;font-weight:800;letter-spacing:.06em;padding:.15rem .5rem;
        border-radius:999px;border:1px solid var(--line);white-space:nowrap}
  .p-ok{color:#86EFAC;border-color:rgba(34,197,94,.45);background:rgba(34,197,94,.12)}
  .p-no{color:#FCA5A5;border-color:rgba(239,68,68,.45);background:rgba(239,68,68,.12)}
  .p-wait{color:#FCD34D;border-color:rgba(245,158,11,.45);background:rgba(245,158,11,.12)}
  /* layout */
  .wrap{display:grid;grid-template-columns:250px minmax(0,1fr);gap:1.6rem;
        max-width:1240px;margin:0 auto;padding:1.4rem 1.1rem 5rem}
  aside{position:sticky;top:4.6rem;align-self:start;max-height:calc(100vh - 6rem);overflow:auto}
  .grp{font-size:9.5px;text-transform:uppercase;letter-spacing:.16em;color:var(--faint);
       font-weight:800;margin:1rem 0 .4rem .3rem}
  .navitem{display:flex;align-items:center;gap:.45rem;padding:.3rem .4rem;border-radius:6px;
           cursor:pointer;font-size:12.5px;color:var(--dim);border-left:2px solid transparent}
  .navitem:hover{background:rgba(255,255,255,.05);color:#fff}
  .navitem.on{background:rgba(34,197,94,.10);color:#fff;border-left-color:var(--pulse)}
  .verb{font:800 9px var(--mono);letter-spacing:.05em;padding:.1rem .3rem;border-radius:4px;
        min-width:34px;text-align:center}
  .v-GET{background:rgba(96,165,250,.18);color:#93C5FD}
  .v-POST{background:rgba(34,197,94,.18);color:#86EFAC}
  /* endpoint card */
  .ep{background:var(--panel);border:1px solid var(--line);border-radius:12px;
      margin:0 0 1.1rem;overflow:hidden;scroll-margin-top:5rem}
  .ep.hide{display:none}
  .ep>.head{display:flex;align-items:center;gap:.6rem;padding:.75rem .95rem;
            background:var(--panel2);border-bottom:1px solid var(--line);flex-wrap:wrap}
  .ep .route{font:700 13.5px var(--mono);color:#fff}
  .ep .body{padding:.95rem}
  .sum{font-weight:700;margin:0 0 .3rem}
  .why{color:var(--dim);font-size:13px;margin:.1rem 0 .9rem;max-width:72ch}
  table{width:100%;border-collapse:collapse;font-size:12.5px;margin:.2rem 0 .9rem}
  th{text-align:left;font-size:9.5px;letter-spacing:.13em;text-transform:uppercase;
     color:var(--faint);padding:.3rem .5rem;border-bottom:1px solid var(--line)}
  td{padding:.4rem .5rem;border-bottom:1px solid rgba(255,255,255,.05);vertical-align:top}
  td.k{font:700 12px var(--mono);color:#E2E8F0;white-space:nowrap}
  td.t{font:12px var(--mono);color:var(--violet);white-space:nowrap}
  .req{color:var(--danger);font-weight:800}
  .lbl{font-size:9.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--faint);
       font-weight:800;margin:.9rem 0 .35rem}
  pre{background:#020509;border:1px solid var(--line);border-radius:9px;padding:.7rem .85rem;
      margin:.2rem 0;overflow:auto;font:12px/1.65 var(--mono);color:#D5E0EC;max-height:22rem}
  .row{display:flex;gap:.5rem;align-items:center;flex-wrap:wrap;margin-top:.6rem}
  .warnbox{border-left:3px solid var(--warn);background:rgba(245,158,11,.08);
           padding:.55rem .8rem;border-radius:0 7px 7px 0;font-size:12.5px;margin:.6rem 0}
  .dangerbox{border-left-color:var(--danger);background:rgba(239,68,68,.09)}
  .okbox{border-left-color:var(--pulse);background:rgba(34,197,94,.08)}
  .stat{font:700 12px var(--mono);padding:.1rem .45rem;border-radius:5px}
  .s2{background:rgba(34,197,94,.18);color:#86EFAC}
  .s4{background:rgba(245,158,11,.18);color:#FCD34D}
  .s5{background:rgba(239,68,68,.18);color:#FCA5A5}
  .muted{color:var(--faint);font-size:11.5px}
  h1{font-size:22px;margin:.2rem 0 .4rem;letter-spacing:-.02em}
  .intro{color:var(--dim);max-width:74ch;font-size:13.5px}
  @media(max-width:900px){.wrap{grid-template-columns:1fr}aside{position:static;max-height:none}}
</style>
</head><body>

<header>
  <div class="brand">IPO PULSE <span>API</span><small>try it, from the server itself</small></div>

  <span class="muted">Base</span>
  <input id="base" style="width:270px" spellcheck="false">
  <span id="health" class="pill p-wait">checking…</span>

  <div class="grow"></div>

  <input id="pw" type="password" placeholder="IPOPULSE_TRIGGER_PASSWORD" style="width:230px">
  <button class="btn btn-go" id="authbtn">Authorize</button>
  <span id="authstate" class="pill p-no">no token</span>
  <a class="btn" href="/">← Studio</a>
  <a class="btn" href="/trigger">Trigger</a>
</header>

<div class="wrap">
  <aside>
    <div class="grp">Open</div>
    <div id="nav-open"></div>
    <div class="grp">Needs a token</div>
    <div id="nav-auth"></div>
    <div class="grp">Filter</div>
    <div class="navitem on" data-filter="all">All endpoints</div>
    <div class="navitem" data-filter="read">Read-only</div>
    <div class="navitem" data-filter="acts">Changes something</div>
  </aside>

  <main>
    <h1>Every endpoint this server answers</h1>
    <p class="intro">
      The studio, the Trigger panel and this page all talk to the same nine routes on
      the same port. Fill a request in, send it, read the real response — nothing here
      is a mock. Three of them cost something that cannot be taken back; those are
      marked, and guarded.
    </p>
    <div class="warnbox okbox" style="margin-bottom:1.2rem">
      <b>Pointing this at another machine.</b> Change <b>Base</b> above to
      <code>http://your-vm:8000</code> to drive that server instead. The browser will
      then send a cross-origin request, which the backend only answers if this page's
      origin is listed in <code>IPOPULSE_ALLOWED_ORIGINS</code> on <i>that</i> machine —
      it is never <code>*</code>, because these routes start jobs.
    </div>
    <div id="eps"></div>
  </main>
</div>

<script>
const SPEC = /*__SPEC__*/[];
const $ = (s, r) => (r || document).querySelector(s);
const esc = (s) => String(s).replace(/[&<>]/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const idOf = (e) => (e.method + e.path).replace(/[^a-z0-9]+/gi, '-').toLowerCase();

/* The token lives here and nowhere else. Not localStorage: it unlocks a job
   runner, and a closed tab should not leave one behind. */
let TOKEN = '';

const EFFECT = {
  read:    {pill:'p-ok',   label:'read only',        note:''},
  spend:   {pill:'p-wait', label:'spends credit',    note:'Costs TTS characters or daily quota unless the cache answers.'},
  write:   {pill:'p-wait', label:'writes the sheet', note:'Rewrites Google Sheet tabs. Never run this while a scheduled job may be mid-write.'},
  publish: {pill:'p-no',   label:'uploads a video',  note:'With dry_run off, this puts a video on the channel. There is no undo but deleting it.'},
};

function base() { return ($('#base').value || '').replace(/\/+$/, ''); }

/* ── the page ──────────────────────────────────────────────────────────── */
function render() {
  const eps = $('#eps'), open = $('#nav-open'), auth = $('#nav-auth');
  SPEC.forEach((e) => {
    const id = idOf(e), fx = EFFECT[e.effect] || EFFECT.read;

    const nav = document.createElement('div');
    nav.className = 'navitem';
    nav.innerHTML = `<span class="verb v-${e.method}">${e.method}</span>
                     <span>${esc(e.path.replace('/api/', ''))}</span>`;
    nav.onclick = () => document.getElementById(id).scrollIntoView({behavior: 'smooth'});
    (e.auth ? auth : open).appendChild(nav);

    const card = document.createElement('section');
    card.className = 'ep';
    card.id = id;
    card.dataset.effect = e.effect;
    card.innerHTML = `
      <div class="head">
        <span class="verb v-${e.method}">${e.method}</span>
        <span class="route">${esc(e.path)}</span>
        <span class="pill ${fx.pill}">${fx.label}</span>
        ${e.auth ? '<span class="pill">X-Token</span>' : '<span class="pill p-ok">open</span>'}
      </div>
      <div class="body">
        <p class="sum">${esc(e.summary)}</p>
        <p class="why">${esc(e.why)}</p>
        ${paramTable(e)}
        <div class="lbl">Returns</div>
        <pre>${esc(typeof e.returns === 'string' ? e.returns
                    : JSON.stringify(e.returns, null, 1))}</pre>
        ${fx.note ? `<div class="warnbox ${e.effect === 'publish' ? 'dangerbox' : ''}">${esc(fx.note)}</div>` : ''}
        ${tryBlock(e)}
      </div>`;
    eps.appendChild(card);
    wireTry(e, card);
  });
}

function paramTable(e) {
  if (!e.params.length) return '<div class="lbl">Parameters</div><p class="muted">None.</p>';
  return '<div class="lbl">Parameters</div><table><thead><tr>'
    + '<th>Name</th><th>Type</th><th>Required</th><th>Notes</th></tr></thead><tbody>'
    + e.params.map((p) => `<tr>
        <td class="k">${esc(p.name)}</td>
        <td class="t">${esc(p.type)}</td>
        <td>${p.required ? '<span class="req">yes</span>'
             : (p.default !== undefined ? `default <code>${esc(JSON.stringify(p.default))}</code>` : 'no')}</td>
        <td>${esc(p.note || '')}</td></tr>`).join('')
    + '</tbody></table>';
}

/* A prefilled body, so "Try it" is one click for the read endpoints and a
   considered edit for the rest. dry_run is TRUE in the sample on purpose. */
function sampleBody(e) {
  if (e.method !== 'POST') return '';
  const out = {};
  e.params.forEach((p) => {
    if (p.default !== undefined) out[p.name] = p.default;
    else if (p.required) out[p.name] = p.type === 'integer' ? 0 : '';
  });
  if (e.path === '/api/login') out.password = '';
  if (e.path === '/api/run') out.job = 'monitor';
  if (e.path === '/api/voice') { out.text = 'One lot is fifteen thousand rupees.'; out.lang = 'en'; }
  if (e.path === '/api/youtube/publish') {
    out.reel = 5; out.lang = 'en'; out.privacy = 'unlisted'; out.dry_run = true;
  }
  return JSON.stringify(out, null, 1);
}

function tryBlock(e) {
  const id = idOf(e);
  return `
    <div class="lbl">Try it</div>
    ${e.method === 'POST'
      ? `<textarea id="body-${id}" rows="${Math.min(12, (sampleBody(e).split('\n').length + 1))}"
                   spellcheck="false">${esc(sampleBody(e))}</textarea>` : ''}
    ${e.effect === 'publish'
      ? `<div class="warnbox dangerbox">
           <b>Guard.</b> While <code>dry_run</code> is true this only renders and queues.
           To actually upload, set it to false <i>and</i> type <code>UPLOAD</code> here:
           <input id="confirm-${id}" placeholder="UPLOAD" style="width:110px;margin-left:.4rem">
         </div>` : ''}
    <div class="row">
      <button class="btn ${e.effect === 'publish' ? 'btn-red' : 'btn-go'}" id="send-${id}">Send</button>
      <span class="muted" id="meta-${id}"></span>
    </div>
    <pre id="out-${id}" style="display:none"></pre>
    <audio id="audio-${id}" controls style="display:none;width:100%;margin-top:.5rem"></audio>`;
}

function wireTry(e, card) {
  const id = idOf(e);
  $(`#send-${id}`, card).onclick = () => send(e, card);
}

async function send(e, card) {
  const id = idOf(e);
  const out = $(`#out-${id}`, card), meta = $(`#meta-${id}`, card),
        audio = $(`#audio-${id}`, card), btn = $(`#send-${id}`, card);
  let body = null;

  if (e.method === 'POST') {
    const raw = $(`#body-${id}`, card).value.trim();
    try { body = raw ? JSON.parse(raw) : {}; }
    catch (err) { show(out, meta, '', 'Not valid JSON — ' + err.message, 's5'); return; }
  }

  // ── the guards ──────────────────────────────────────────────────────
  if (e.effect === 'publish' && body && body.dry_run === false) {
    const typed = ($(`#confirm-${id}`, card).value || '').trim();
    if (typed !== 'UPLOAD') {
      show(out, meta, '', 'dry_run is false, so this would upload. Type UPLOAD in the box to confirm.', 's4');
      return;
    }
    if (!confirm('Upload to the channel?\n\n' + (body.title || '(no title)')
                 + '\nvisibility: ' + (body.privacy || 'unlisted'))) return;
  }
  if (e.effect === 'write'
      && !confirm('Start ' + JSON.stringify(body.jobs || body.job)
                  + '?\n\nThis rewrites Google Sheet tabs.')) return;
  if (e.effect === 'spend' && body && body.force
      && !confirm('force: true skips the cache, so this one is billed. Continue?')) return;

  btn.disabled = true;
  const t0 = performance.now();
  try {
    const r = await fetch(base() + e.path, {
      method: e.method, cache: 'no-store',
      headers: {'Content-Type': 'application/json',
                ...(TOKEN ? {'X-Token': TOKEN} : {})},
      body: body === null ? undefined : JSON.stringify(body),
    });
    const ms = Math.round(performance.now() - t0);
    const kind = r.headers.get('Content-Type') || '';
    const cls = r.status < 300 ? 's2' : (r.status < 500 ? 's4' : 's5');

    // /api/voice answers with audio, not JSON — play it rather than dumping bytes.
    if (kind.startsWith('audio/')) {
      const blob = await r.blob();
      audio.src = URL.createObjectURL(blob);
      audio.style.display = 'block';
      const hdr = ['X-Voice-Provider', 'X-Voice-Format', 'X-Voice-Cached',
                   'X-Voice-Chars', 'X-Voice-Left']
        .map((h) => h + ': ' + (r.headers.get(h) ?? '—')).join('\n');
      show(out, meta, `${kind}, ${(blob.size / 1024).toFixed(0)} KB\n${hdr}`,
           `${r.status} · ${ms} ms`, cls);
    } else {
      const text = await r.text();
      let pretty = text;
      try { pretty = JSON.stringify(JSON.parse(text), null, 1); } catch (_) {}
      show(out, meta, pretty, `${r.status} · ${ms} ms`, cls);
      // Logging in here also authorises the rest of the page.
      if (e.path === '/api/login' && r.ok) {
        try { TOKEN = JSON.parse(text).token || ''; authState(); } catch (_) {}
      }
    }
  } catch (err) {
    show(out, meta, String(err),
         'The request never completed — wrong base URL, server down, or a CORS '
         + 'refusal (see IPOPULSE_ALLOWED_ORIGINS).', 's5');
  } finally {
    btn.disabled = false;
  }
}

function show(out, meta, text, note, cls) {
  out.style.display = 'block';
  out.textContent = text;
  meta.innerHTML = `<span class="stat ${cls || 's4'}">${esc(note)}</span>`;
}

/* ── auth + health ─────────────────────────────────────────────────────── */
function authState() {
  const el = $('#authstate');
  el.className = 'pill ' + (TOKEN ? 'p-ok' : 'p-no');
  el.textContent = TOKEN ? 'token held · 8h' : 'no token';
}

async function login() {
  const pw = $('#pw').value;
  const el = $('#authstate');
  el.className = 'pill p-wait'; el.textContent = 'signing in…';
  try {
    const r = await fetch(base() + '/api/login', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({password: pw}),
    });
    const d = await r.json();
    if (!r.ok) { el.className = 'pill p-no'; el.textContent = d.error || ('HTTP ' + r.status); return; }
    TOKEN = d.token; $('#pw').value = ''; authState();
  } catch (err) {
    el.className = 'pill p-no'; el.textContent = 'unreachable';
  }
}

async function health() {
  const el = $('#health');
  el.className = 'pill p-wait'; el.textContent = 'checking…';
  try {
    const r = await fetch(base() + '/api/health', {cache: 'no-store'});
    const d = await r.json();
    if (d.ok && d.auth) { el.className = 'pill p-ok'; el.textContent = 'up · password set'; }
    else if (d.ok) { el.className = 'pill p-wait'; el.textContent = 'up · NO password — nothing below will work'; }
    else { el.className = 'pill p-no'; el.textContent = 'answered, but not ok'; }
  } catch (_) {
    el.className = 'pill p-no'; el.textContent = 'no answer at this base';
  }
}

document.querySelectorAll('[data-filter]').forEach((el) => {
  el.onclick = () => {
    document.querySelectorAll('[data-filter]').forEach((o) => o.classList.remove('on'));
    el.classList.add('on');
    const f = el.dataset.filter;
    document.querySelectorAll('.ep').forEach((card) => {
      const read = card.dataset.effect === 'read';
      card.classList.toggle('hide', f === 'read' ? !read : (f === 'acts' ? read : false));
    });
  };
});

$('#base').value = location.origin;
$('#authbtn').onclick = login;
$('#pw').addEventListener('keydown', (e) => { if (e.key === 'Enter') login(); });
$('#base').addEventListener('change', health);
render();
authState();
health();
</script>
</body></html>
"""
