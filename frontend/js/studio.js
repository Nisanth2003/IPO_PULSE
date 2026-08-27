/* IPO Pulse Studio — the visualiser.
 *
 * Holds no facts of its own: everything comes from the JSON the backend
 * publishes, gets recomputed by compute.js when you edit live, and is drawn
 * as scenes. Reel = one video. Scene = one idea, one screen.
 */

const RING = 2 * Math.PI * 66;                 // gauge circumference, r=66

// Sparkline coordinate space. These MUST match the literal viewBox on the
// trail <svg> in index.html — it is written out rather than bound, because a
// `:viewBox` binding silently does nothing (HTML lowercases the attribute
// name; SVG's is case-sensitive).
const SPARK_W = 300, SPARK_H = 70;

function studio() {
  const component = {
    // ── catalogue / selection ──────────────────────────────────────────
    catalogue: [], boardRows: [], slug: '',
    ipo: null, d: null, loc: null,
    loading: true, loadError: '',

    // ── view state ─────────────────────────────────────────────────────
    lang: 'en', reelIndex: 0, scene: 0, gmpMode: 'single',
    ratio: 'reel', scale: 1, autoFit: true,
    densityBase: 1, density: 1, autoShrink: true,
    bg: 'gradient', focus: false,
    theme: 'midnight',          // card palette; see THEMES in reels.js
    showLogo: true,             // company artwork in the card header
    showGif: true,              // pinned sticker, corner of every scene
    gifSize: 4.6,               // in --fs units, so it tracks the frame
    showSafe: false, showFooter: true, showProgress: true, rounded: true,
    leftOpen: true, rightOpen: true,
    playing: false, speed: 1, sceneProg: 0,
    /* Timing. `script` derives each scene's hold from how long its narration
       takes to say; `fixed` keeps the designed holds from reels.js.
       reelTarget is per reel number — 0 means "no target, run naturally" —
       because reel 2 and reel 5 do not want the same length. */
    timingMode: 'script',
    reelTarget: { 1: 0, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0 },
    mounted: false, overflow: false, copied: '',
    now: Date.now(),
    a: { gmp: 0, pct: 0, est: 0, total: 0, score: 0 },
    handle: '@IPOPulse',
    hasH2C: typeof html2canvas !== 'undefined',
    hasBackend: false,          // set by probeBackend(); gates the Trigger button
    /* Where the trigger API is. '' means same-origin (a local `ipopulse
       serve`); a URL means the hosted one from config.js. `api` is whichever
       actually answered — set by probeBackend, and what every /api/* call
       below is prefixed with. */
    apiBase: (typeof API_BASE !== 'undefined' && API_BASE) || '',
    api: '',
    /* The hosted API is password-protected the same way the local panel is,
       and the token it returns lives only in this tab. */
    run: { open: false, pw: '', token: '', busy: false, msg: '',
           ok: true, lines: [], jobs: [], timer: null },

    /* Remote trigger, for the published site.
     *
     * GitHub Pages serves files and runs no processes, so /api/* 404s there
     * and the local Trigger button cannot appear — correct, but it left the
     * public site with no way to run anything at all.
     *
     * The jobs already run somewhere else: .github/workflows/schedule.yml,
     * which accepts `workflow_dispatch`. So this dispatches that workflow
     * through api.github.com directly from the browser.
     *
     * ── On the token, and why it is NOT persisted ────────────────────
     *
     * It is the OWNER'S own fine-grained PAT, typed in by them, held in this
     * object for the life of the tab and sent only to api.github.com. It is
     * never written to the repo and — since this build — never written to the
     * machine either. Closing or reloading the tab forgets it.
     *
     * Earlier builds kept it in localStorage. That is one plain-text file on
     * whichever desk you happened to be sitting at, readable by DevTools and
     * by anything that ever manages to run script on this origin, and it does
     * not follow you to another device anyway — localStorage is per browser
     * profile and per origin, and Chrome does not sync it. So it bought a
     * saved paste and paid for it in a credential left lying around. Gone.
     * `initRemote` now DELETES the old key, so upgrading actually cleans the
     * machine rather than orphaning the token on it.
     *
     * ── Why it cannot come from a secret, the way the password does ───
     *
     * IPOPULSE_TRIGGER_PASSWORD works that way because there is a SERVER on
     * its side: the secret sits in the host's environment, the browser posts a
     * typed guess, and the comparison happens where the visitor cannot look.
     * The site gate does the same trick one-way — config.js ships a PBKDF2
     * HASH, which is useless to an attacker and still enough to check against.
     *
     * A PAT has neither property. GitHub Actions secrets exist only inside a
     * running workflow, on the runner; GitHub Pages is a file server with no
     * process to hold one. Baking the PAT into config.js at build time would
     * publish it verbatim from a public repo — the exact leak this panel is
     * careful to avoid. And a token cannot be hashed, because unlike a
     * password it has to be REPLAYED to api.github.com to be of any use.
     *
     * ── The normal path: no paste at all ─────────────────────────────
     *
     * When GH_DISPATCH_PAT was set at build time, config.js carries the token
     * AES-GCM sealed under the site password, gate.js unseals it the moment
     * you type that password at the front door, and this panel finds it in
     * window.GH_PAT with nothing to type. That is what `sealed` tracks.
     *
     * It cannot be a plain build-time substitution the way SHEET_ID is: the
     * repo and the site are public, and a token has to be REPLAYED to
     * api.github.com, so it cannot be shipped as a one-way hash the way the
     * password is. Sealed is the only shape that fits. See _sealed_pat() in
     * backend/ipopulse/cli.py.
     *
     * Nothing is unsealed on a reload, because the gate only asks for the
     * password once per session — so `unsealToken()` asks for it again here,
     * which is also the fallback when the seal is absent entirely.
     *
     * Two ways to run a job with no credential in this page at all, still
     * worth preferring on a machine that is not yours:
     *   - github.com -> Actions -> schedule.yml -> "Run workflow". Your normal
     *     GitHub session authorises it.
     *   - Point API_BASE at the hosted backend and use the Run job panel
     *     above. That one takes the trigger PASSWORD, and the secret it is
     *     checked against never leaves the host.
     *
     * Scope the PAT to Actions:write on this one repo with a short expiry, and
     * the worst case stays "someone at this keyboard, right now, can run the
     * same jobs the cron already runs". */
    gh: { open: false, token: '', draft: '', owner: '', repo: '',
          msg: '', ok: false, runs: [], sealed: false, pw: '', busy: false },

    /* Narration and the recording made from it.
     *
     * `blob` is the source of truth and `url` exists only so an <audio> or
     * <video> element has something to point at. Both are dropped together in
     * setVoice/startCapture, because a URL outliving its blob is a player that
     * silently plays nothing. */
    /* Narration, keyed by language.
     *
     * Three entries rather than one, because all three ship in the bundle and
     * the point is to generate them in one pass. `lang` (the studio's own
     * selector) still decides which one the capture mixes in — the card text
     * is localised too, so one video cannot serve three languages. Three
     * languages means three recordings, and that is not a limitation to work
     * around; it is what the audience needs.
     *
     * blob is truth, url exists only for an <audio> to point at, and they are
     * always replaced together — a url outliving its blob is a player that
     * silently plays nothing. */
    voice: { en: null, hi: null, te: null,
             urls: { en: '', hi: '', te: '' },
             /* Which provider spoke, and in what container. Gemini returns wav
                and ElevenLabs mp3, so the bundle cannot hardcode an extension
                and a listener comparing takes needs to know which is which. */
             fmt: { en: 'mp3', hi: 'mp3', te: 'mp3' },
             from: { en: '', hi: '', te: '' },
             busy: '', msg: '', ok: true, left: null },
    cap: { rec: null, blob: null, url: '', msg: '', ok: true },
    /* Does pressing Play also speak? On by default — hearing the voice against
       the picture is the point of a preview. Off is for checking visual timing
       repeatedly without the same read twenty times. */
    previewVoice: true,
    /* The three languages, in one place.
     *
     * There were two lists — this one for the voice panel and a literal
     * `[['en','EN'],['hi','हिं'],['te','తె']]` inline in the language switcher —
     * plus LANG_INDEX in i18n.js, which is the one that decides which slot a
     * translated string comes out of. Three copies of an ordered set that must
     * agree, and the `L` shortcut needed a fourth to cycle through.
     *
     * `short` is the switcher's keycap-sized label, `label` the voice panel's.
     * The ORDER is load-bearing: it is the cycle order for `L` and it must match
     * i18n.js LANG_INDEX, because that maps en/hi/te to positions 0/1/2 in every
     * translated tuple. Reordering here without reordering there would silently
     * hand Hindi text to a Telugu card. */
    LANGS: [
      { code: 'en', short: 'EN', label: 'English' },
      { code: 'hi', short: 'हिं', label: 'हिन्दी' },
      { code: 'te', short: 'తె', label: 'తెలుగు' },
    ],
    /* Must match control.CHAINS. The `push` steps these used to name were
       removed with the local store — the sheet IS the store now, so there is
       nothing left to push it to. */
    GH_JOBS: [
      { id: 'daily',  label: 'Daily chain',  detail: 'sync → enrich → doctor → build' },
      { id: 'grey',   label: 'GMP chain',    detail: 'free keyless GMP, then the model fills gaps' },
      { id: 'enrich', label: 'Fill gaps',    detail: 'research + RHP + analyse + translate' },
      { id: 'build',  label: 'Check sheet',  detail: 'verify every record still renders' },
    ],

    REELS, PRESETS, REGISTRARS, RING,

    // ── lifecycle ──────────────────────────────────────────────────────
    async init() {
      this.restorePrefs();
      setInterval(() => { this.now = Date.now(); }, 1000);
      document.addEventListener('visibilitychange', () => {
        if (!document.hidden) this.settle();     // rAF stalls in hidden tabs
      });
      ['leftOpen', 'rightOpen', 'focus'].forEach((k) =>
        this.$watch(k, () => requestAnimationFrame(() => this.autoFit && this.fit())));
      // Two lists, and which one a key belongs in is decided by one question:
      // can changing it alter how much room the scene has? `showLogo` can —
      // the plate sits in the header, and a taller header is a shorter scene —
      // so it has to re-measure. `theme` and the sticker keys cannot: the theme
      // only repaints, and the sticker is absolutely positioned precisely so it
      // takes part in no layout. Anything in neither list is not persisted at
      // all, which is the trap: `savePrefs` runs from these watchers only, so a
      // key added to its payload and to no list here silently never saves.
      ['ratio', 'lang', 'densityBase', 'autoShrink', 'showFooter', 'showProgress',
       'gmpMode', 'showLogo']
        .forEach((k) => this.$watch(k, () => { this.savePrefs(); this.check(); }));
      ['scale', 'autoFit', 'bg', 'rounded', 'speed', 'handle',
       'theme', 'showGif', 'gifSize', 'timingMode', 'reelTarget',
       'defaultGif', 'themePerReel', 'previewVoice']
        .forEach((k) => this.$watch(k, () => this.savePrefs()));

      this.probeBackend();
      this.initRemote();
      await this.loadCatalogue();
      this.$nextTick(() => this.autoFit && this.fit());
    },

    /* Is a local ipopulse server behind this page, or is this the published
       static site? The backend exists either way — it just is not reachable
       at a Pages URL, which serves files and runs nothing. Only the local
       server can be POSTed to, so the Trigger button is hidden unless
       /api/health answers and the Run job button takes its place. Deliberately
       not awaited: no answer is the normal case on Pages and must not delay
       first paint. */
    probeBackend() {
      // Same-origin first (a local `ipopulse serve`), then the hosted API if
      // one is configured. Local wins: if you are running the server you are
      // working on this machine, and a round trip to a sleeping free-tier
      // instance would be a slower answer to the same question.
      const bases = ['', this.apiBase].filter((b, i, a) => a.indexOf(b) === i);
      const tryNext = (i) => {
        if (i >= bases.length) { this.hasBackend = false; return; }
        fetch(`${bases[i]}/api/health`, { cache: 'no-store' })
          .then((r) => (r.ok ? r.json() : null))
          .then((d) => {
            if (d && d.ok && d.auth) { this.hasBackend = true; this.api = bases[i]; }
            else tryNext(i + 1);
          })
          .catch(() => tryNext(i + 1));
      };
      tryNext(0);
    },

    /* ── running jobs against the trigger API ──────────────────────────
     *
     * Same endpoints the local /trigger page uses, called from here so the
     * published site can drive a hosted backend. Password in, short-lived
     * token back, then poll /api/status for the log while it runs.
     *
     * The token is kept in memory only, not localStorage: this one unlocks a
     * job runner, and unlike the GitHub PAT there is no per-repo scoping to
     * fall back on if the browser is someone else's. Closing the tab ends it.
     */
    async _apiCall(path, opts = {}) {
      const r = await fetch(`${this.api}${path}`, {
        ...opts,
        cache: 'no-store',
        headers: { 'Content-Type': 'application/json',
                   ...(this.run.token ? { 'X-Token': this.run.token } : {}),
                   ...(opts.headers || {}) },
      });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
      return body;
    },
    async runLogin() {
      this.run.msg = 'Signing in…'; this.run.ok = true;
      try {
        const d = await this._apiCall('/api/login', {
          method: 'POST', body: JSON.stringify({ password: this.run.pw }) });
        this.run.token = d.token; this.run.pw = '';
        this.run.msg = '';
        const j = await this._apiCall('/api/jobs');
        this.run.jobs = j.jobs || [];
        this.runPoll();
      } catch (e) { this.run.ok = false; this.run.msg = e.message; }
    },
    async runJob(id) {
      this.run.msg = `Starting ${id}…`; this.run.ok = true;
      try {
        await this._apiCall('/api/run', {
          method: 'POST', body: JSON.stringify({ job: id }) });
        this.run.msg = '';
        this.runPoll();
      } catch (e) { this.run.ok = false; this.run.msg = e.message; }
    },
    runPoll() {
      clearInterval(this.run.timer);
      const tick = async () => {
        try {
          const s = await this._apiCall('/api/status');
          this.run.busy = !!s.running;
          this.run.lines = s.lines || [];
          if (!s.running) {
            clearInterval(this.run.timer); this.run.timer = null;
            // The jobs write to the sheet, and this page reads the sheet —
            // so the numbers on screen are stale the moment a run finishes.
            if (s.rc === 0) this.loadCatalogue();
          }
        } catch (e) {
          clearInterval(this.run.timer); this.run.timer = null;
          this.run.ok = false; this.run.msg = e.message;
        }
      };
      this.run.timer = setInterval(tick, 1500);
      tick();
    },
    get runLog() { return (this.run.lines || []).join('\n'); },
    runClose() {
      clearInterval(this.run.timer); this.run.timer = null;
      this.run.open = false;
    },

    /* owner/repo from the Pages URL, so nothing is hardcoded and a fork just
       works: nisanth2003.github.io/IPO_PULSE/ -> nisanth2003 / IPO_PULSE */
    initRemote() {
      const host = location.hostname.split('.')[0];
      const seg = location.pathname.split('/').filter(Boolean)[0];
      this.gh.owner = host || '';
      this.gh.repo = seg || host || '';
      /* Deliberately not read back — the token is memory-only, see above.
         This removes what older builds persisted, so the upgrade takes the
         credential off the machine instead of leaving it there unread. */
      try { localStorage.removeItem('ipoPulse.ghToken'); } catch (e) {}

      this.gh.sealed = !!(window.IPO_GATE && window.IPO_GATE.sealed);
      this.adoptSealed();
    },

    /* Take the token gate.js unsealed, if it left one.
     *
     * Called from init AND from ghOpen, and the second call is the one that
     * matters. Alpine initialises while the gate overlay is still up — the page
     * is only hidden, not un-mounted — so at init time the password has usually
     * not been typed yet and window.GH_PAT does not exist. Adopting once at
     * startup would therefore ask for the password again on every fresh load,
     * which is the exact friction the seal exists to remove. */
    adoptSealed() {
      if (this.gh.token) return;
      if (window.GH_PAT) {
        this.gh.token = window.GH_PAT;
        this.gh.ok = true;
        this.gh.msg = 'Token unsealed from this deployment. Nothing to paste.';
      } else if (window.IPO_GATE && window.IPO_GATE.error) {
        this.gh.ok = false;
        this.gh.msg = window.IPO_GATE.error;
      }
    },
    ghOpen() { this.adoptSealed(); this.gh.open = true; },

    /* The reload case, and the only reason this panel still has a password
       field. gate.js unseals on password entry, but it only asks once per
       session — so after F5 the studio is unlocked and the token is gone,
       by design: it is never written to any storage. One field, one derive.

       AES-GCM authenticates, so a wrong password throws here rather than
       handing api.github.com a corrupt credential and getting a 401 that
       blames the token. */
    async unsealToken() {
      const pw = this.gh.pw;
      if (!pw || !window.IPO_GATE || !window.IPO_GATE.sealed) return;
      this.gh.busy = true; this.gh.ok = true; this.gh.msg = 'Unsealing…';
      try {
        const token = await window.IPO_GATE.unseal(pw);
        if (!token) throw new Error('this deployment carries no sealed token');
        this.gh.token = token; this.gh.pw = '';
        this.gh.ok = true; this.gh.msg = 'Unsealed. Nothing was stored.';
        this.ghRuns();
      } catch (err) {
        this.gh.ok = false;
        this.gh.msg = 'Wrong password, or the token was sealed under a '
          + 'different one. You can also paste a token by hand below.';
      } finally { this.gh.busy = false; }
    },
    /* Every GitHub token prefix. Checked because the field sits next to a
       panel about running jobs, and the other credential in this project —
       IPOPULSE_TRIGGER_PASSWORD — is also "the password that runs jobs".
       Pasting that gets a bare "401 Bad credentials" from GitHub, which does
       not hint that the wrong secret was used at all. Rejected before it is
       stored, rather than saved and failing later on every click. */
    _looksLikeGhToken(t) {
      return /^(github_pat_|ghp_|gho_|ghu_|ghs_|ghr_)/.test(t);
    },
    saveToken() {
      const t = (this.gh.draft || '').trim();
      if (!t) return;
      if (!this._looksLikeGhToken(t)) {
        this.gh.ok = false;
        this.gh.msg = 'That is not a GitHub token. It must start with '
          + '"github_pat_". This is NOT your IPOPULSE_TRIGGER_PASSWORD — that '
          + 'one only unlocks the local /trigger page. Create a token at '
          + 'github.com/settings/personal-access-tokens.';
        return;
      }
      this.gh.token = t; this.gh.draft = '';
      this.gh.ok = true;
      this.gh.msg = 'Held in this tab only — nothing written to this machine. '
        + 'Reloading or closing the tab forgets it.';
      this.ghRuns();
    },
    forgetToken() {
      this.gh.token = ''; this.gh.draft = ''; this.gh.runs = [];
      /* Also drop the copy gate.js handed over, or "Forget" would clear the
         panel while leaving the token reachable from the console. */
      try { window.GH_PAT = ''; } catch (e) {}
      /* Nothing is stored any more, but an upgrade can still meet a key an
         older build left behind. Cheap to clear twice, expensive to miss. */
      try { localStorage.removeItem('ipoPulse.ghToken'); } catch (e) {}
      this.gh.ok = true; this.gh.msg = 'Token forgotten.';
    },
    _ghHeaders() {
      return { Accept: 'application/vnd.github+json',
               Authorization: `Bearer ${this.gh.token}`,
               'X-GitHub-Api-Version': '2022-11-28' };
    },
    async ghRun(jobs) {
      const { owner, repo } = this.gh;
      this.gh.msg = `Asking GitHub to run “${jobs}”…`; this.gh.ok = true;
      try {
        const r = await fetch(
          `https://api.github.com/repos/${owner}/${repo}/actions/workflows/schedule.yml/dispatches`,
          { method: 'POST', headers: this._ghHeaders(),
            body: JSON.stringify({ ref: 'main', inputs: { jobs } }) });
        if (r.status === 204) {
          this.gh.ok = true;
          this.gh.msg = `Started “${jobs}”. It takes a few minutes; the site redeploys when it commits.`;
          setTimeout(() => this.ghRuns(), 4000);
        } else {
          // GitHub's message is far more useful than a generic failure —
          // 401 means the token, 404 usually means the workflow file name.
          const body = await r.json().catch(() => ({}));
          this.gh.ok = false;
          const hint = r.status === 401
            ? ' — the saved token is wrong or expired. It must be a GitHub '
              + 'token starting "github_pat_", not IPOPULSE_TRIGGER_PASSWORD. '
              + 'Press Forget and paste a new one.'
            : r.status === 403
              ? ' — the token is valid but lacks Actions → Read and write on '
                + 'this repository.'
              : r.status === 404
                ? ' — check the token can see this repo, and that '
                  + 'schedule.yml still exists on main.'
                : '';
          this.gh.msg = `GitHub said ${r.status}: `
            + `${body.message || 'request refused'}${hint}`;
        }
      } catch (err) {
        this.gh.ok = false;
        this.gh.msg = `Could not reach api.github.com — ${err.message}`;
      }
    },
    async ghRuns() {
      const { owner, repo } = this.gh;
      try {
        const r = await fetch(
          `https://api.github.com/repos/${owner}/${repo}/actions/runs?per_page=5`,
          { headers: this._ghHeaders() });
        if (!r.ok) { this.gh.runs = []; return; }
        const d = await r.json();
        this.gh.runs = (d.workflow_runs || []).map((w) => ({
          id: w.id, name: w.display_title || w.name, status: w.status,
          ok: w.conclusion === 'success',
          when: new Date(w.created_at).toLocaleString('en-GB',
                  { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' }),
        }));
      } catch (err) { this.gh.runs = []; }
    },

    async loadCatalogue() {
      this.loading = true; this.loadError = '';
      // Always re-read the workbook: this runs on load and again after a job
      // has rewritten the file, and a cached parse would hide the new data.
      DATA.refresh();
      try {
        const [idx, board] = await Promise.all([DATA.index(), DATA.board()]);
        this.catalogue = idx.ipos || [];
        this.boardRows = board.rows || [];
        const saved = localStorage.getItem('ipoPulse.slug');
        const pick = this.catalogue.find((c) => c.slug === saved) || this.catalogue[0];
        if (pick) await this.select(pick.slug);
        else this.loadError = 'The workbook has no IPOs in it yet. Run:  ipopulse sync';
      } catch (err) {
        this.loadError =
          `Could not read ${DATA.BASE}/ipo-pulse.xlsx — ${err.message}. ` +
          `Open the site over http (ipopulse serve), not by double-clicking the file.`;
      } finally {
        this.loading = false;
      }
    },

    async select(slug) {
      try {
        const payload = await DATA.ipo(slug);
        this.slug = slug;
        this.ipo = payload.ipo;
        localStorage.setItem('ipoPulse.slug', slug);
        this.recompute();
        this.go(this.reelIndex, 0);
        // one more pass once the card has actually laid out
        setTimeout(() => this.check(), 150);
      } catch (err) {
        this.loadError = `Could not load ${slug}: ${err.message}`;
      }
    },

    /* Switch language. One implementation, because `lang` alone is not enough.
     *
     * The $watch on `lang` saves prefs and re-fits the card, but the localised
     * strings come out of DATA.localized(ipo, lang) and that only runs inside
     * recompute(). Assigning `lang` on its own therefore highlights the new
     * language and leaves the card showing the old one — which is why the
     * EN/हिं/తె buttons always called both, and why the `L` shortcut routes
     * through here instead of repeating that pair a third time.
     *
     * Safe mid-reel, including mid-playback. `holdSeconds` is read fresh on
     * every 50ms tick, so when Script timing gives Telugu a longer hold than
     * English the remaining time re-scales smoothly instead of jumping — the
     * progress already earned is kept and only the rate changes. */
    setLang(code) {
      if (!code || code === this.lang) return;
      this.lang = code;
      this.recompute();
      /* Swap the narration too. Pressing L mid-playback otherwise leaves the
         previous language still speaking over cards that have changed, which
         is a confusing enough thing to hear that it reads as a bug. Restarting
         from the top is the honest option — the two reads are different
         lengths, so there is no position in one that maps onto the other. */
      if (this.playing) this.startPreview();
    },

    /* Cycle EN → हिं → తె, wrapping.
     *
     * One key rather than three: with three languages every one is at most two
     * presses away, and digit keys are spoken for by the 1–6 reel jumps. */
    cycleLang(dir = 1) {
      const n = this.LANGS.length;
      const at = this.LANGS.findIndex((l) => l.code === this.lang);
      this.setLang(this.LANGS[((at < 0 ? 0 : at) + dir + n) % n].code);
    },

    /** Re-derive everything after a live edit. */
    recompute() {
      if (!this.ipo) return;
      this.d = derive(this.ipo);
      this.loc = DATA.localized(this.ipo, this.lang);
      this.check();
    },

    // ── prefs ──────────────────────────────────────────────────────────
    savePrefs() {
      try {
        localStorage.setItem('ipoPulse.prefs', JSON.stringify({
          lang: this.lang, ratio: this.ratio, densityBase: this.densityBase,
          autoShrink: this.autoShrink, scale: this.scale, autoFit: this.autoFit,
          bg: this.bg, showFooter: this.showFooter, showProgress: this.showProgress,
          rounded: this.rounded, speed: this.speed, handle: this.handle,
          gmpMode: this.gmpMode, theme: this.theme, showLogo: this.showLogo,
          showGif: this.showGif, gifSize: this.gifSize,
          timingMode: this.timingMode, reelTarget: this.reelTarget,
          defaultGif: this.defaultGif, themePerReel: this.themePerReel,
          previewVoice: this.previewVoice,
        }));
      } catch (e) { /* private mode */ }
    },
    restorePrefs() {
      try {
        const s = JSON.parse(localStorage.getItem('ipoPulse.prefs') || '{}');
        Object.keys(s).forEach((k) => { if (s[k] !== undefined) this[k] = s[k]; });
        this.density = this.densityBase;
        // Same defensiveness as `th` applies to a stale theme key: a
        // reelTarget restored as null, an array, or short a reel would make
        // every timing getter read `undefined` and hold every scene for
        // NaN seconds, which stops playback dead.
        const t = this.reelTarget;
        const clean = { 1: 0, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0 };
        if (t && typeof t === 'object' && !Array.isArray(t)) {
          for (const k of Object.keys(clean)) clean[k] = Math.max(0, Number(t[k]) || 0);
        }
        this.reelTarget = clean;
        if (this.timingMode !== 'fixed' && this.timingMode !== 'script') {
          this.timingMode = 'script';
        }
      } catch (e) { /* ignore */ }
    },

    // ── i18n ───────────────────────────────────────────────────────────
    t(key) { return label(key, this.lang); },
    get translated() { return this.ipo ? DATA.hasTranslation(this.ipo, this.lang) : true; },

    // ── reel / scene navigation ────────────────────────────────────────
    get reel() { return REELS[this.reelIndex]; },
    get scenes() { return scenesFor(this.reel, this.gmpMode, this.ipo); },
    get sceneId() { return (this.scenes[this.scene] || this.scenes[0]).id; },
    get sceneCount() { return this.scenes.length; },
    // ── theme ──────────────────────────────────────────────────────────
    /* Always a theme object, never undefined: a stale `theme` key in
       localStorage from a renamed or removed theme would otherwise take the
       accent, the card and the export background down with it.
     *
     * `themePerReel` rotates the palette by reel instead of using one for the
     * whole set. Six videos published the same day in identical colours is
     * exactly the "interchangeable from video to video" look YouTube's
     * inauthentic-content policy describes, and it also makes a viewer's feed
     * read as one repeated upload. Deterministic — reel 3 is always the same
     * theme — so a series stays recognisable rather than becoming random. */
    themePerReel: true,
    get th() {
      if (this.themePerReel) {
        const i = (this.reelIndex + THEME_ROTATION_OFFSET) % THEMES.length;
        return THEMES[i] || THEMES[0];
      }
      return THEME_BY_KEY[this.theme] || THEMES[0];
    },

    /* Accent for the reel on screen, picked from the active theme.
       Falls back to the reel's own `acc` if a theme is short a hue, so adding
       a seventh reel cannot render it colourless. */
    get acc() { return this.reelAcc(this.reel); },

    /* Any reel's accent under the active theme. Needed by the reel tabs, which
       tint themselves per reel and would otherwise be the one part of the UI
       still showing the un-themed colours. */
    reelAcc(reel) { return this.th.hues[reel.n - 1] || reel.acc; },

    /* The card's own background. Applied inline rather than by swapping a
       class, because .stage-card sets `background` in the stylesheet and an
       inline style is the only thing that reliably wins without !important. */
    get cardBg() { return this.th.card; },

    /* The company logo URL, or '' when there is nothing usable to show.
     *
     * Written by `gmp-sync` into the Sources tab as role `logo`, so it arrives
     * as ordinary IPO data with no new column anywhere. Suppressed once the
     * <img> has reported an error for this exact URL: a broken image in the
     * header is worse than no image, and the initials tile on reel 1's company
     * scene is still there as the readable fallback. Keyed on the URL rather
     * than a boolean so switching IPO re-tries rather than staying suppressed. */
    logoBroken: '',
    get brandLogo() {
      const url = (this.ipo && this.ipo.sources && this.ipo.sources.logo) || '';
      return url && url !== this.logoBroken ? url : '';
    },

    /* An optional animated sticker, shown in the corner of every scene.
     *
     * Nothing populates this automatically and nothing can: no IPO data source
     * publishes a company GIF, and a model asked for one returns a URL that
     * 404s. So it is a pin — put any GIF or image URL on the Sources tab under
     * role `gif` and it appears. Same mechanism as `logo`, deliberately: one
     * free-form role -> url tab already round-trips to the browser, so this
     * needed no schema change either.
     *
     * It animates in a screen recording, which is the point. It will NOT
     * animate in an exported PNG — html2canvas draws whatever frame the browser
     * is showing — so a still export gets one frame of it, not a broken image.
     *
     * Cross-origin note: an <img> marked crossorigin that the host does not
     * grant CORS to fails to load at all, so unlike the logo (whose host sends
     * `*`) this one is left un-marked. That means it can taint the canvas and
     * cost you the PNG — hence `gifTaints`, which drops it for the capture
     * only, so a pinned GIF can never break the export path. */
    gifBroken: '',
    gifTaints: false,
    /* A channel-wide sticker, used when the IPO has not pinned its own.
       Per-IPO-only meant nobody ever set one: all 20 IPOs had role `gif`
       empty, so the whole feature had never appeared on a single reel. */
    defaultGif: '',
    get brandGif() {
      const pinned = (this.ipo && this.ipo.sources && this.ipo.sources.gif) || '';
      const url = pinned || this.defaultGif || '';
      if (!url || url === this.gifBroken) return '';
      return this.showGif ? url : '';
    },

    get P() { return PRESETS[this.ratio]; },

    /** True when reel r's scene s is the one on screen. */
    at(r, id) { return this.reel.n === r && this.sceneId === id; },

    go(reelIndex, scene = 0) {
      this.reelIndex = reelIndex;
      this.scene = Math.max(0, Math.min(scene, this.scenes.length - 1));
      this.sceneProg = 0;
      this.replay();
    },
    nextScene() {
      if (this.scene < this.sceneCount - 1) this.go(this.reelIndex, this.scene + 1);
      else if (this.playing) this.stopPlay();       // a reel ends; it's one video
      else this.go(this.reelIndex, 0);
    },
    prevScene() {
      if (this.scene > 0) this.go(this.reelIndex, this.scene - 1);
      else this.go(this.reelIndex, this.sceneCount - 1);
    },
    nextReel() { this.go((this.reelIndex + 1) % REELS.length, 0); },
    prevReel() { this.go((this.reelIndex + REELS.length - 1) % REELS.length, 0); },

    /* ── how long a scene should hold ────────────────────────────────────
     *
     * Until now `hold` was a hand-tuned constant in reels.js with no
     * relationship to what the narrator says over it: a scene with four
     * bullets and a scene with one both held for five seconds, so the
     * voiceover and the picture drifted apart and had to be fixed by hand in
     * CapCut on every single video.
     *
     * Two controls replace that:
     *
     *   timingMode 'script'  — hold each scene for as long as its narration
     *                          actually takes to say (see speakSeconds)
     *   reelTarget[n]        — force a reel to a total length; every scene
     *                          scales to hit it, keeping their proportions
     *
     * `speed` stays as the final multiplier so the existing control is
     * unchanged.
     */

    /* Estimated seconds to speak `text` aloud in `lang`.
     *
     * English is counted in WORDS: 155 wpm is the Audible/ACX narration
     * standard, so 0.387 s/word.
     *
     * Hindi and Telugu are counted in GRAPHEME CLUSTERS, not words and not
     * codepoints. Words are hopeless across languages — Telugu is
     * agglutinative, so one word can carry what English needs four for, and
     * the two studies that pin cross-language speech rate (Pellegrino 2011,
     * Coupé 2019, ~39 bits/s) work in syllables rather than words for
     * exactly that reason. Codepoints are just as wrong in the other
     * direction: one Devanagari or Telugu akshara is typically 2-4
     * codepoints (base + virama + vowel sign), so `.length` over-counts by
     * roughly double. A grapheme cluster ≈ one akshara ≈ one syllable, which
     * is the unit that actually tracks time. Intl.Segmenter implements the
     * Unicode rules for it natively, so no library is needed.
     *
     * 0.15 s/akshara (≈6.7/sec) is a SEED, interpolated from clinical
     * speech-rate norms for Dravidian languages (4-10 syllables/s, adults
     * 6-8) and slowed a little for deliberate narration. Neither Hindi nor
     * Telugu appears in the cross-linguistic literature, so this is an
     * engineering estimate, not a measured constant. Calibrate it against
     * real output when convenient: ElevenLabs' timestamps endpoint returns
     * exact per-character timings, so a few hundred words of real script
     * would let these be fitted properly.
     */
    speakSeconds(text, lang) {
      const s = String(text || '').trim();
      if (!s) return 0;
      const L = lang || this.lang || 'en';
      let body;
      if (L === 'en') {
        body = (s.match(/\S+/g) || []).length * 0.387;
      } else {
        let units = 0;
        try {
          const seg = new Intl.Segmenter(L, { granularity: 'grapheme' });
          for (const _ of seg.segment(s)) units++;
        } catch (e) {
          // No Intl.Segmenter: fall back to codepoints and discount for the
          // over-count rather than silently returning a doubled duration.
          units = [...s].length * 0.55;
        }
        body = units * 0.15;
      }
      // Pauses are added on top rather than folded into the rate, because
      // punctuation density varies independently of length.
      const sentences = (s.match(/[.!?।॥]/g) || []).length;
      const commas = (s.match(/[,;:—]/g) || []).length;
      return body + sentences * 0.5 + commas * 0.25;
    },

    /* Seconds each scene of the current reel needs for its narration.
     *
     * Only English is written per scene (`enSegments`); Hindi and Telugu are
     * still one block of template text, deliberately — see the note in
     * output.js about not machine-translating a voice.
     *
     * So the SHAPE of the reel comes from English, where the per-scene split
     * is known, and the LENGTH comes from the language actually being
     * narrated. Each scene keeps its English share of the reel, and that
     * share is applied to the real duration of the Hindi or Telugu script.
     *
     * Measuring the English text with the Indic rate — which an earlier
     * version did — is nonsense twice over: it counts Latin characters as
     * though they were aksharas, and it reports a duration for words nobody
     * is going to speak. It read 168s for a Hindi reel whose English
     * equivalent was 93s.
     */
    get scriptHolds() {
      const segs = (this.enSegments ? this.enSegments(this.reel.n) : {}) || {};
      const ids = this.scenes.map((sc) => sc.id);

      // English seconds per scene — the shape.
      const shape = {};
      let shapeTotal = 0;
      for (const sc of this.scenes) {
        const text = segs[sc.id];
        const secs = text ? this.speakSeconds(text, 'en') : 0;
        shape[sc.id] = secs;
        shapeTotal += secs;
      }

      // Total seconds for the language actually being read.
      const spoken = this.lang === 'en'
        ? shapeTotal
        : this.speakSeconds(this.scriptFor(this.reel.n), this.lang);

      const out = {};
      for (const sc of this.scenes) {
        // A scene with no narration still has to be readable on screen, so
        // it keeps its designed hold rather than collapsing to nothing.
        if (!shape[sc.id] || shapeTotal <= 0) { out[sc.id] = sc.hold || 4; continue; }
        const share = shape[sc.id] / shapeTotal;
        out[sc.id] = Math.max(2, spoken * share + 0.6);
      }
      return out;
    },

    /* The natural hold per scene before any target is applied. */
    get baseHolds() {
      if (this.timingMode === 'script' && this.lang === 'en') return this.scriptHolds;
      if (this.timingMode === 'script') return this.scriptHolds;
      const out = {};
      for (const sc of this.scenes) out[sc.id] = sc.hold || 4;
      return out;
    },

    /* Scale factor that turns the natural total into the requested total. */
    get targetScale() {
      const want = Number(this.reelTarget[this.reel.n] || 0);
      if (!want) return 1;
      const natural = this.scenes.reduce((t, sc) => t + (this.baseHolds[sc.id] || 4), 0);
      return natural > 0 ? want / natural : 1;
    },

    /* Final per-scene seconds, target and speed applied. */
    get finalHolds() {
      const base = this.baseHolds, k = this.targetScale, sp = this.speed || 1;
      const out = {};
      for (const sc of this.scenes) {
        out[sc.id] = Math.max(1, ((base[sc.id] || 4) * k) / sp);
      }
      return out;
    },

    get holdSeconds() {
      const sc = this.scenes[this.scene];
      return sc ? (this.finalHolds[sc.id] || 4) : 4;
    },
    get reelSeconds() {
      return Math.round(
        this.scenes.reduce((sum, sc) => sum + (this.finalHolds[sc.id] || 4), 0));
    },
    /* What the reel would run to on its own, ignoring any target — shown
     * next to the control so the number being overridden is visible. */
    get naturalSeconds() {
      return Math.round(
        this.scenes.reduce((t, sc) => t + (this.baseHolds[sc.id] || 4), 0) / (this.speed || 1));
    },
    setReelTarget(v) {
      const n = Math.max(0, Math.round(Number(v) || 0));
      this.reelTarget = { ...this.reelTarget, [this.reel.n]: n };
    },

    togglePlay() { this.playing ? this.stopPlay() : this.startPlay(); },
    startPlay() {
      this.playing = true; this.go(this.reelIndex, 0);
      clearInterval(this._timer);
      this.startPreview();
      this._timer = setInterval(() => {
        this.sceneProg += 100 / (this.holdSeconds * 20);
        if (this.sceneProg >= 100) { this.sceneProg = 0; this.nextScene(); }
      }, 50);
    },

    /* ── narration alongside the reel ────────────────────────────────────
     *
     * Play used to be silent: a setInterval advancing scenes, and nothing
     * else. The narration existed but only came out of the panel's own audio
     * controls, or mixed into a recording — so the first time anyone heard the
     * voice against the picture was in the finished take, which is the most
     * expensive possible moment to discover they drift.
     *
     * Its own Audio object, deliberately NOT the panel's <audio> element:
     * that one has controls a person may be scrubbing, and driving it from
     * here would fight them. Same reason capture.js makes its own.
     *
     * Skipped while a capture is running. capture.js is already playing this
     * narration through WebAudio to get it into the recording, and starting a
     * second copy here would put two overlapping voices in the room and one of
     * them in the file. */
    startPreview() {
      this.stopPreview();
      if (!this.previewVoice || this.cap.rec) return;
      const url = this.voice.urls[this.lang];
      if (!url) return;
      try {
        this._preview = new Audio(url);
        this._preview.currentTime = 0;
        // Rejects if the browser withholds autoplay. Space and a click are
        // both gestures so it normally resolves, and a silent reel is a far
        // better failure than an unhandled rejection in the console.
        this._preview.play().catch(() => {});
      } catch (e) { this._preview = null; }
    },
    stopPreview() {
      if (!this._preview) return;
      try { this._preview.pause(); } catch (e) {}
      this._preview = null;
    },

    stopPlay() {
      this.playing = false; this.sceneProg = 0; clearInterval(this._timer);
      this.stopPreview();
      /* A reel ending IS the end of the video — nextScene() calls this when it
         runs off the last scene. So the recorder stops itself, and a take is
         exactly one reel with no trailing frames of a paused card. */
      if (this.cap.rec) this.stopCapture();
    },

    /* ── voice, capture, bundle ──────────────────────────────────────────
     *
     * Three steps of the playbook's daily loop, collapsed into the studio:
     * generate the narration (§8), record the reel with it (§9), and take away
     * one file that holds everything the upload needs (§10).
     *
     * The order matters and the UI enforces it: no voice, no narrated capture.
     * Recording first and hoping to sync later is the workflow this replaces.
     */

    /* Narration for the script on screen.
     *
     * Goes through the backend, never straight to ElevenLabs: the key is
     * metered money, and a key in this page is a key in everyone's page. That
     * is a harder line than the GitHub PAT — a leaked PAT runs cron jobs, a
     * leaked voice key runs up a bill.
     *
     * Which is also why this needs the trigger token: the same sign-in as
     * running a job, because this one spends per call. */
    /* Every reel's script in every language, without the studio changing
       under you.
     *
       The script getters read `this.lang` directly, so the only way to get all
       three is to swap it, read, and put it back. Safe because the getters are
       pure and synchronous, and because Alpine's watchers are queued rather
       than immediate — setting lang three times in one tick fires the
       savePrefs/check watcher once, with the restored value. Doing this across
       an await would NOT be safe: the UI would paint mid-swap. */
    scriptsByLang() {
      const original = this.lang;
      const out = {};
      try {
        for (const { code } of this.LANGS) {
          this.lang = code;
          out[code] = (this.script || '').trim();
        }
      } finally {
        this.lang = original;
      }
      return out;
    },

    /* Narration for one language, or for all three.
     *
     * Sends `lang` rather than a voice id: which voice reads Telugu is a
     * policy decision, and it lives in voice.py where one file owns it. If
     * this page picked the voice, that mapping would exist twice.
     *
     * Sequential, not parallel. Three concurrent requests would each pass the
     * budget check against the same pre-call balance and could jointly
     * overspend the cap they each individually cleared. */
    async genVoice(only = '') {
      if (!this.api) {
        this.voice.msg = 'Voice needs a backend — this is the static site. '
          + 'Run `ipopulse serve`, or set IPOPULSE_TRIGGER_API to a hosted one.';
        this.voice.ok = false;
        return;
      }
      if (!this.run.token) {
        // The password panel already exists for jobs; reuse it rather than
        // growing a second sign-in for the same token.
        this.run.open = true;
        this.voice.msg = 'Sign in first — same password as running a job.';
        this.voice.ok = false;
        return;
      }

      const scripts = this.scriptsByLang();
      const wanted = only ? [only] : this.LANGS.map((l) => l.code);
      const done = [];
      let billed = 0;
      this.voice.ok = true;

      for (const code of wanted) {
        const text = scripts[code];
        if (!text) { continue; }
        this.voice.busy = code;
        this.voice.msg = `Generating ${code.toUpperCase()}…`;
        try {
          const r = await fetch(`${this.api}/api/voice`, {
            method: 'POST', cache: 'no-store',
            headers: { 'Content-Type': 'application/json',
                       'X-Token': this.run.token },
            body: JSON.stringify({ text, lang: code }),
          });
          if (!r.ok) {
            const body = await r.json().catch(() => ({}));
            throw new Error(body.error || `HTTP ${r.status}`);
          }
          billed += Number(r.headers.get('X-Voice-Chars') || 0);
          this.voice.left = Number(r.headers.get('X-Voice-Left') || 0);
          this.voice.fmt[code] = r.headers.get('X-Voice-Format') || 'mp3';
          this.voice.from[code] = r.headers.get('X-Voice-Provider') || '';
          this.setVoice(code, await r.blob());
          done.push(code.toUpperCase());
        } catch (err) {
          this.voice.ok = false;
          this.voice.busy = '';
          // Stop rather than press on: the usual cause is the key, the budget
          // or a missing voice id, and all three would fail the rest too —
          // three identical errors is worse than one.
          this.voice.msg = `${code.toUpperCase()}: ${err.message}`
            + (done.length ? ` (${done.join(', ')} did work)` : '');
          return;
        }
      }

      this.voice.busy = '';
      if (!done.length) {
        this.voice.ok = false;
        this.voice.msg = 'No script to narrate for this reel.';
        return;
      }
      this.voice.ok = true;
      this.voice.msg = `${done.join(', ')} ready · `
        + (billed ? `${billed.toLocaleString()} characters billed`
                  : 'all from cache, nothing billed')
        + (this.voice.left != null
            ? ` · ${this.voice.left.toLocaleString()} left this month` : '');
    },

    /* One object URL per language at a time. Every blob: URL pins its blob in
       memory until revoked, and re-rendering while tuning a number would
       otherwise leak one per attempt per language. */
    setVoice(code, blob) {
      if (this.voice.urls[code]) URL.revokeObjectURL(this.voice.urls[code]);
      this.voice[code] = blob || null;
      this.voice.urls[code] = blob ? URL.createObjectURL(blob) : '';
    },

    /* The one the capture will mix in: the language on screen, because the
       card text is in that language too. */
    get voiceReady() { return !!this.voice[this.lang]; },
    get voiceCount() {
      return this.LANGS.filter((l) => this.voice[l.code]).length;
    },

    async startCapture() {
      if (!CAPTURE.supported) {
        this.cap.ok = false;
        this.cap.msg = 'This browser has no screen recorder. Chrome or Edge.';
        return;
      }
      /* Focus mode is not advisory here: getDisplayMedia captures the whole
         tab, so a visible panel is a panel in the video. */
      this.focus = true;
      if (this.cap.url) { URL.revokeObjectURL(this.cap.url); this.cap.url = ''; }
      this.cap.blob = null;
      this.cap.ok = true;
      try {
        this.cap.rec = await CAPTURE.start({
          voiceUrl: this.voice.urls[this.lang] || '',
          onState: (m) => { this.cap.msg = m; },
          reset: () => { this.go(this.reelIndex, 0); this.replay(); },
          play: () => this.startPlay(),
          onStop: (blob) => {
            this.cap.rec = null;
            this.cap.blob = blob;
            this.cap.url = URL.createObjectURL(blob);
            this.cap.ok = true;
            this.cap.msg = `Recorded ${(blob.size / 1e6).toFixed(1)} MB.`;
          },
        });
      } catch (err) {
        this.cap.rec = null;
        this.cap.ok = false;
        // A cancelled picker is a NotAllowedError, which is not a fault.
        this.cap.msg = err.name === 'NotAllowedError'
          ? 'Capture cancelled.' : err.message;
      }
    },
    stopCapture() {
      if (this.cap.rec) this.cap.rec.stop();
      if (this.playing) { this.playing = false; clearInterval(this._timer); }
    },

    /* One zip per upload.
     *
     * The pristine voice.mp3 goes in alongside the video even though the video
     * already carries the narration — the webm's audio has been through an
     * mp3 -> WebAudio -> Opus round trip, and a re-edit should never have to
     * start from the transcoded copy. */
    async downloadBundle() {
      const stem = [this.slug || 'ipo', `reel${this.reel.n}`]
        .join('-').replace(/[^a-z0-9-]+/gi, '-').toLowerCase();

      /* The video is named for the language it was recorded in, not left as a
         bare reel.webm: the card text on screen is localised, so a file that
         does not say which language it is becomes unidentifiable the moment
         there are two of them in a folder. */
      const entries = [
        { name: `video/reel-${this.lang}.webm`, blob: this.cap.blob },
        { name: 'thumbnail-prompt.txt', text: this.thumbnailPrompt },
        { name: 'README.txt', text: this.bundleReadme() },
      ];

      const scripts = this.scriptsByLang();
      const originalLang = this.lang;
      try {
        for (const { code } of this.LANGS) {
          // Extension from what the provider actually returned, never assumed:
          // a wav named .mp3 is a file that fails to open in half of editors.
          entries.push({ name: `audio/voice-${code}.${this.voice.fmt[code] || 'mp3'}`,
                         blob: this.voice[code] });
          entries.push({ name: `script/script-${code}.txt`, text: scripts[code] });
          // packaging reads this.lang the same way the scripts do, so the
          // titles and description have to be collected the same way.
          this.lang = code;
          entries.push({ name: `pack/publishing-pack-${code}.txt`,
                         text: this.packaging });
        }
      } finally {
        this.lang = originalLang;
      }

      const zip = await ZIP.fromMixed(entries);
      if (!zip) {
        this.cap.ok = false;
        this.cap.msg = 'Nothing to bundle yet — generate a voice or record.';
        return;
      }
      this.saveBlob(zip, `${stem}.zip`);
    },

    /* A bundle that arrives without instructions gets opened once and guessed
       at. This is the file that says what the folders are and, more
       importantly, what still has to be done by hand. */
    bundleReadme() {
      const have = this.LANGS
        .filter((l) => this.voice[l.code]).map((l) => l.code.toUpperCase());
      const missing = this.LANGS
        .filter((l) => !this.voice[l.code]).map((l) => l.code.toUpperCase());
      return [
        `${this.ipo?.company || this.slug} · reel ${this.reel.n} `
          + `(${this.t(this.reel.key)})`,
        `Recorded in: ${this.lang.toUpperCase()}`
          + (this.cap.blob ? '' : '  (NO VIDEO — nothing was recorded)'),
        `Narration present: ${have.join(', ') || 'none'}`
          + (missing.length ? `   missing: ${missing.join(', ')}` : ''),
        `Voice from: ${[...new Set(this.LANGS
            .map((l) => this.voice.from[l.code]).filter(Boolean))].join(', ')
            || 'n/a'}`,
        '',
        'video/   the reel with its narration already mixed in.',
        'audio/   the untranscoded narration. Use THESE to re-edit — the video',
        '         audio has been through a WebAudio -> Opus round trip.',
        'script/  what the narration says, per language.',
        'pack/    titles, description and hashtags, per language.',
        '',
        'STILL TO DO BY HAND:',
        '  - Generate the thumbnail from thumbnail-prompt.txt (16:9, 2K).',
        '  - Tick the altered/synthetic content box at upload if the voice',
        '    is AI. Disclosure does not block monetisation; skipping it can.',
        '  - Fill the 00:00 chapter line in the description after editing.',
        missing.length
          ? `  - The reel text on screen is in ${this.lang.toUpperCase()}, so `
            + `${missing.concat(have.filter((h) => h !== this.lang.toUpperCase()))
                 .join('/')} need their own recording — one video cannot serve `
            + 'a language it is not written in.'
          : '  - Each language needs its own recording: the card text is'
            + ' localised, so one video cannot serve all three.',
      ].join('\n');
    },

    /* Blob -> a file on disk. Revoked on the next tick rather than
       immediately: some browsers have not started reading the URL by the time
       click() returns, and revoking too early produces a 0-byte download. */
    saveBlob(blob, filename) {
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 30_000);
    },

    // ── entrance animation ─────────────────────────────────────────────
    replay() {
      this.mounted = false;
      this.a = { gmp: 0, pct: 0, est: 0, total: 0, score: 0 };
      clearTimeout(this._replayT);
      this._replayT = setTimeout(() => {           // setTimeout, not rAF:
        this.mounted = true;                       // rAF is dead in hidden tabs
        if (!this.d) return;
        this.tw('gmp', this.d.gmp.gmp);
        this.tw('pct', this.d.gmp.pct);
        this.tw('est', this.d.gmp.est_listing);
        this.tw('total', this.d.subscription.total || 0);
        this.tw('score', this.d.score.effective || 0);
        this.check();
      }, 50);
    },
    settle() {
      this.mounted = true;
      if (!this.d) return;
      this.a = {
        gmp: this.d.gmp.gmp, pct: this.d.gmp.pct, est: this.d.gmp.est_listing,
        total: this.d.subscription.total || 0, score: this.d.score.effective || 0,
      };
    },
    tw(key, to, dur = 900) {
      const target = Number(to) || 0;
      const from = Number(this.a[key]) || 0;
      const t0 = performance.now();
      const step = (t) => {
        const p = Math.min(1, (t - t0) / dur);
        this.a[key] = from + (target - from) * (1 - (1 - p) ** 3);
        if (p < 1) requestAnimationFrame(step);
      };
      requestAnimationFrame(step);
    },

    // ── fitting ────────────────────────────────────────────────────────
    fit() {
      const s = this.$refs.stage; if (!s) return;
      const availH = s.clientHeight - (this.focus ? 56 : 108);
      const availW = s.clientWidth - 56;
      this.scale = Math.max(0.3, Math.min(1.8, availH / this.P.h, availW / this.P.w));
    },
    /* How much bigger this script has to be set to read as the same size.
     *
     * Point size is not perceived size. Telugu and Devanagari carry vowel
     * signs above and below the base line, so the base glyph gets a smaller
     * share of the em than a Latin letter does, and the whole run looks
     * smaller and lighter set at an identical px value. Latin's own x-height
     * is unusually generous by comparison.
     *
     * So the card is set larger for those scripts rather than asking the
     * viewer to squint. Telugu needs the most; Devanagari's shirorekha (the
     * connecting top bar) gives it more visual weight already, so it needs
     * less.
     *
     * Applied to `--fs`, which every size on the card derives from, so this
     * is one multiplier rather than a per-element override — and it lands in
     * the PNG export too, since html2canvas renders the computed pixel value.
     * The auto-shrink search still runs afterwards, so a dense scene shrinks
     * to fit exactly as before; this only decides how large it starts.
     */
    get scriptScale() {
      return { te: 1.12, hi: 1.05 }[this.lang] || 1;
    },

    /** Shrink text until the scene fits; synchronous so hidden tabs still work. */
    check() {
      // Measuring before the record has loaded gives a meaningless "overflow"
      // and strands the text at the shrink floor.
      if (!this.ipo || !this.d) return;
      this.$nextTick(() => {
        const el = this.$refs.body, card = document.getElementById('capture');
        if (!el || !card) return;
        const setFs = (v) =>
          card.style.setProperty('--fs', this.P.fs * this.scriptScale * v + 'px');
        const fits = () => el.scrollHeight <= el.clientHeight + 2;

        // Freeze the entrance animation for the duration of the measurement,
        // otherwise a scene still translated down by 46px measures as overflow.
        card.classList.add('measuring');
        try {
          this.measure(setFs, fits);
        } finally {
          card.classList.remove('measuring');
        }
      });
    },

    measure(setFs, fits) {
        if (!this.autoShrink) {
          this.density = this.densityBase; setFs(this.densityBase);
          this.overflow = !fits(); return;
        }

        // Binary search for the largest text scale that fits. Reading
        // scrollHeight forces a reflow, so this must stay cheap: stepping down
        // 0.02 at a time cost ~60 reflows per scene change and visibly froze
        // the page. Eight probes get within 0.004 of the same answer.
        // Floor at 0.5 — the short frames (4:5, 1:1) leave ~445px of usable
        // height and a display-type hook legitimately needs most of it.
        const FLOOR = 0.5;
        setFs(this.densityBase);
        if (fits()) {
          this.density = this.densityBase; this.overflow = false; return;
        }
        let lo = FLOOR, hi = this.densityBase;
        for (let i = 0; i < 8; i++) {
          const mid = (lo + hi) / 2;
          setFs(mid);
          if (fits()) lo = mid; else hi = mid;
        }
        this.density = +lo.toFixed(3);
        setFs(this.density);
        this.overflow = !fits();
    },
    setRatio(k) {
      this.ratio = k;
      this.$nextTick(() => { if (this.autoFit) this.fit(); this.check(); });
    },

    // ── keyboard ───────────────────────────────────────────────────────
    /* Move to the previous / next IPO in the dropdown's own order.
     *
     * That order is deliberate, not alphabetical — data.js sorts open first,
     * then upcoming, closed, allotment, listed, and by GMP within each. So
     * stepping through it walks the board from most actionable to least, which
     * is the order you want to review it in at 9am.
     *
     * Wraps around: with the list this long, a key that silently does nothing
     * at one end reads as a broken key rather than as a boundary.
     *
     * `_hopping` guards against a held key. `select()` awaits a fetch, and
     * without the guard three quick presses start three loads that can land
     * out of order, leaving the card showing an IPO the dropdown does not. */
    async hopIpo(delta) {
      if (this._hopping || this.catalogue.length < 2) return;
      const at = this.catalogue.findIndex((c) => c.slug === this.slug);
      const n = this.catalogue.length;
      const next = this.catalogue[((at < 0 ? 0 : at) + delta + n) % n];
      if (!next || next.slug === this.slug) return;
      this._hopping = true;
      // Stop playback first. Changing IPO mid-reel leaves the timer running
      // over a card that is still loading, which plays scenes of two IPOs.
      if (this.playing) this.stopPlay();
      try { await this.select(next.slug); } finally { this._hopping = false; }
    },

    key(e) {
      if (/INPUT|TEXTAREA|SELECT/.test(e.target.tagName)) return;
      const k = e.key;
      if (k >= '1' && k <= '6') { this.go(+k - 1, 0); return; }
      /* IPO is the level above reel, so Shift+↑/↓ sits one modifier above the
         plain ↑/↓ that moves reel. Checked before the switch because the
         unshifted arrows are handled there and would otherwise win. */
      if (e.shiftKey && (k === 'ArrowDown' || k === 'ArrowUp')) {
        e.preventDefault();
        this.hopIpo(k === 'ArrowDown' ? 1 : -1);
        return;
      }
      switch (k) {
        case 'ArrowRight': this.nextScene(); break;
        case 'ArrowLeft':  this.prevScene(); break;
        case 'ArrowDown':  this.nextReel(); break;
        case 'ArrowUp':    this.prevReel(); break;
        case ' ':          e.preventDefault(); this.togglePlay(); break;
        case 'f': case 'F': this.focus = !this.focus; break;
        case 'Escape':      this.focus = false; break;
        case 'g': case 'G': this.showSafe = !this.showSafe; break;
        case 's': case 'S': this.leftOpen = !this.leftOpen; break;
        case 'd': case 'D': this.rightOpen = !this.rightOpen; break;
        case 'r': case 'R': this.replay(); break;
        case 'e': case 'E': this.exportScene(); break;
        /* Lowercase cycles forward, uppercase (Shift) back — the same pairing
           , / . uses for the IPO list. Works mid-playback: the reel keeps
           running and the card re-renders in place. */
        case 'l': this.cycleLang(1); break;
        case 'L': this.cycleLang(-1); break;
        case 'b': case 'B': {
          const list = ['gradient', 'black', 'green', 'blue', 'checker'];
          this.bg = list[(list.indexOf(this.bg) + 1) % list.length]; break;
        }
        case '[': this.densityBase = Math.max(0.70, +(this.densityBase - 0.02).toFixed(2)); break;
        case ']': this.densityBase = Math.min(1.20, +(this.densityBase + 0.02).toFixed(2)); break;
        /* The one-handed pair, for walking the board without reaching for a
           modifier — same adjacent-keys idea as [ and ] for text scale. Both
           the shifted and unshifted forms, so a caps-happy moment still works
           and `<`/`>` (which is what the keycaps actually say) is not a dead
           key. */
        case ',': case '<': this.hopIpo(-1); break;
        case '.': case '>': this.hopIpo(1); break;
      }
    },

    // ── formatting ─────────────────────────────────────────────────────
    fmt(n) {
      const v = Number(n);
      if (!Number.isFinite(v)) return '0';
      return v.toLocaleString('en-IN', {
        maximumFractionDigits: Math.abs(v) < 100 && v % 1 !== 0 ? 2 : 0,
      });
    },
    fmtDate(s, withYear) {
      if (!s) return '—';
      const dt = new Date(s + 'T00:00:00');
      if (isNaN(dt)) return s;
      return dt.toLocaleDateString('en-GB', withYear
        ? { day: '2-digit', month: 'short', year: 'numeric' }
        : { day: '2-digit', month: 'short' });
    },
    fmtShort(s) {
      if (!s) return '—';
      const dt = new Date(s + 'T00:00:00');
      return isNaN(dt) ? s : dt.toLocaleDateString('en-GB', { day: '2-digit', month: 'short' });
    },
    signed(v, dp = 1) { const n = Number(v) || 0; return (n >= 0 ? '+' : '') + n.toFixed(dp); },
    slugify(s) {
      return String(s).toLowerCase().replace(/[^a-z0-9]+/g, '-')
        .replace(/^-|-$/g, '').slice(0, 40) || 'ipo';
    },
    rgba(hex, alpha) {
      const n = parseInt(String(hex).replace('#', ''), 16);
      return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`;
    },
    get accTint() {
      return `border-color:${this.rgba(this.acc, 0.45)};` +
             `background:linear-gradient(180deg,${this.rgba(this.acc, 0.16)},rgba(255,255,255,.03))`;
    },
    get accGlow() {
      return `z-index:0;background:radial-gradient(90% 55% at 50% -8%,` +
             `${this.rgba(this.acc, 0.22)} 0%,rgba(15,23,42,0) 60%)`;
    },
    get today() {
      return new Date(this.now).toLocaleDateString('en-GB',
        { day: '2-digit', month: 'short', year: 'numeric' });
    },

    // ── view helpers ───────────────────────────────────────────────────
    get verdict() { return VERDICTS[this.ipo?.analysis?.verdict] || VERDICTS.apply; },
    get verdictText() {
      const custom = this.ipo?.analysis?.verdict_text;
      if (custom) return custom;
      return this.verdict.text[LANG_INDEX[this.lang] ?? 0] || this.verdict.text[0];
    },
    get moveIcon() {
      return { surge: '🚀', drop: '🔴', stable: '🟢' }[this.d?.gmp.movement] || '🟢';
    },
    get sentiment() {
      const s = this.d?.subscription?.sentiment;
      return {
        heavy: { key: 'demHeavy', cls: 'glass-g text-emerald-300' },
        good:  { key: 'demGood',  cls: 'glass-g text-emerald-300' },
        ok:    { key: 'demOk',    cls: 'glass-a text-amber-300' },
        weak:  { key: 'demWeak',  cls: 'glass-r text-red-300' },
      }[s] || { key: 'demWeak', cls: 'glass-r text-red-300' };
    },
    get steps() {
      const custom = this.loc?.allotment_steps;
      if (custom && custom.length) return custom.slice(0, 5);
      return DEFAULT_STEPS[this.lang] || DEFAULT_STEPS.en;
    },
    /**
     * The health-check rows: value, the line it's judged against, and where
     * both sit on a 0-100 track so the meter can draw a fill and a tick.
     */
    get benchRows() {
      const marks = this.d?.financials?.marks;
      if (!marks) return [];
      const spec = [
        ['ebitda_margin', 'ebitdaMargin'],
        ['pat_margin',    'patMargin'],
        ['revenue_cagr',  'revCagr'],
        ['ronw',          'ronw'],
        ['debt_equity',   'debtEquity'],
        ['pe',            'peRatio'],
      ];
      return spec.map(([key, labelKey]) => {
        const m = marks[key];
        if (!m || m.verdict === 'na') return null;
        const dp = m.unit === 'x' ? 2 : 1;
        const hint = key === 'pe'
          ? `${this.t('vsPeers')} ${m.good_at}${m.unit}`
          : `${m.higher_is_better ? this.t('goodAbove') : this.t('goodBelow')} ${m.good_at}${m.unit}`;
        return {
          key,
          label: this.t(labelKey),
          value: Number(m.value).toFixed(dp) + m.unit,
          good: m.verdict === 'good',
          pos: m.pos, mark: m.mark, hint,
        };
      }).filter(Boolean);
    },
    get benchScore() {
      const f = this.d?.financials;
      return f?.score_total ? `${f.score_good}/${f.score_total}` : '';
    },

    /* What the score is made of, ordered by how much it moved the number.
       A 0-10 with no visible basis is just an opinion in a circle. */
    get scoreRows() {
      return (this.d?.score?.components || [])
        .filter((p) => p.has_data)
        .sort((a, b) => b.weight - a.weight)
        .map((p) => ({
          key: p.key,
          label: this.t('sc_' + p.key),
          mark: p.mark,
          weight: p.weight,
          detail: p.detail,
          good: p.mark >= 6,
          pos: Math.round(p.mark * 10),
        }));
    },
    /* True when so little has been entered that the number is not a verdict
       yet. The scene says so rather than printing a confident low mark. */
    get scoreThin() { return !(this.d?.score?.has_data); },

    /* "Today's GMP" is only honest when the reading is from today. On any day
       the refresh does not run — or the source is down — this swaps the label
       for the date the figure actually belongs to. */
    get gmpLabel() {
      const g = this.d?.gmp;
      if (!g || !g.updated) return this.t('todayGmp');
      return g.is_stale
        ? `${this.t('gmpAsOf')} ${this.fmtShort(g.updated)}`
        : this.t('todayGmp');
    },
    get gmpStaleNote() {
      const g = this.d?.gmp;
      if (!g || !g.is_stale) return '';
      return g.age_days === 1
        ? this.t('gmpStale1')
        : `${g.age_days} ${this.t('gmpStaleN')}`;
    },

    /* Key-dates rows that actually have a date. */
    get dateRows() {
      const d = this.ipo?.dates || {};
      return [
        ['announced', d.announced], ['opens', d.open], ['closes', d.close],
        ['allot', d.allotment], ['listing', d.listing],
      ].filter(([, v]) => !!v).map(([key, date]) => ({ key, date }));
    },

    /**
     * Day-wise total subscription as column heights.
     *
     * The table below it is for looking a day's number up; this is for seeing the
     * shape — whether demand crept or hockey-sticked on the last day, which is
     * the story and which no column of figures tells at a glance.
     *
     * Scaled to the peak rather than to a fixed ceiling: these run from 0.4x on a
     * quiet SME to 200x+ on a hot mainboard, so any fixed axis makes one of those
     * two unreadable. The peak column is therefore always full height, and the
     * value labels are what carry the absolute scale.
     *
     * Floored at 3% so a genuine near-zero day is still a visible stub rather
     * than looking like a missing bar.
     */
    get subTrend() {
      const days = (this.d?.subscription?.days || []).filter((x) => x.total > 0);
      if (days.length < 2) return { has: false, days: [] };
      const peak = Math.max(...days.map((x) => x.total));
      return {
        has: true, peak,
        days: days.map((x) => ({
          day: x.day, total: x.total,
          pct: Math.max(3, (x.total / peak) * 100),
          isPeak: x.total === peak,
        })),
      };
    },

    /**
     * The price band as a ruler, plus where the grey market puts the listing.
     *
     * "₹190 – ₹201" is two numbers a viewer has no scale for. Drawn, the band
     * becomes a width and the expected listing becomes a distance beyond it —
     * which is the actual question ("how far above what I pay?").
     *
     * The domain runs from the floor price to whichever is higher, the cap or the
     * GMP-implied listing, with 8% padding so the end markers are not flush to
     * the edge. Returns percentages, so the scene needs no measuring.
     *
     * `has` is false unless both band ends exist: an issue whose price band NSE
     * has not published yet would otherwise draw a ruler from 0, making a ₹0
     * floor look like a real published fact.
     */
    get bandRuler() {
      const lo = Number(this.ipo?.issue?.price_low) || 0;
      const hi = Number(this.ipo?.issue?.price_high) || 0;
      if (!lo || !hi || hi < lo) return { has: false };
      const est = Number(this.d?.gmp?.est_listing) || 0;
      const top = Math.max(hi, est);
      const pad = (top - lo) * 0.08 || hi * 0.04;
      const min = lo - pad, max = top + pad;
      const at = (v) => ((v - min) / (max - min)) * 100;
      return {
        has: true, lo, hi, est,
        loPct: at(lo), hiPct: at(hi),
        estPct: est > hi ? at(est) : null,
        // Width of the shaded band segment, as a percentage of the track.
        bandPct: at(hi) - at(lo),
      };
    },

    /**
     * The same dates as a timeline: each stage tagged done / now / future.
     *
     * A list of five dates makes a viewer do the arithmetic — "is the 19th
     * before or after today?" — while the reel is already moving on. The stage
     * tag answers it, and the scene draws it as a rail so the answer is
     * positional as well as coloured.
     *
     * `now` is the NEXT stage still ahead, not the most recent one behind: on an
     * IPO that opened yesterday the thing a viewer needs is the close date, not
     * a highlight on the open. Exactly one stage is ever `now`, so the pulse
     * cannot land on two rows.
     *
     * Day granularity throughout — comparing a date-only value against a
     * timestamp would flip a stage a few hours early and mark the close "done"
     * on the morning of the close, which is the one day it matters most.
     */
    get timeline() {
      const today = new Date(this.now); today.setHours(0, 0, 0, 0);
      const rows = this.dateRows.map((r) => {
        const dt = new Date(r.date + 'T00:00:00');
        return { ...r, done: dt <= today };
      });
      const next = rows.findIndex((r) => !r.done);
      return rows.map((r, i) => ({
        ...r,
        state: i === next ? 'now' : (r.done ? 'done' : 'future'),
      }));
    },

    /* Expected listing range. Falls back to what the grey market implies,
       clearly labelled — the hand-entered range is almost never filled, and
       "₹0 – ₹0" was being read off the card as a real forecast. */
    get listingRange() {
      const l = this.d?.listing, g = this.d?.gmp;
      if (l && (l.low || l.high)) {
        return { low: l.low, high: l.high, low_pct: l.low_pct, high_pct: l.high_pct,
                 implied: false, has: true };
      }
      const band = Number(this.ipo?.issue?.price_high) || 0;
      if (!band || !g?.gmp) return { has: false, implied: false };
      // A band around the GMP estimate rather than a false point forecast:
      // the grey market is directional, not a price.
      const est = band + g.gmp;
      const low = Math.round(band + g.gmp * 0.6);
      const high = Math.round(est + g.gmp * 0.15);
      const pc = (v) => Math.round((v - band) / band * 100);
      return { low, high, low_pct: pc(low), high_pct: pc(high), implied: true, has: true };
    },

    /**
     * The expected listing range restated as rupees on one lot.
     *
     * Reel 6 gave the answer only as a percentage range, which asks the viewer
     * to do two sums while the scene is on screen: percent of the band, times
     * the lot. This is the same range they already trust, in the unit they
     * actually apply in — and it deliberately reuses `listingRange` rather than
     * recomputing from GMP, so the rupees can never disagree with the percent
     * printed directly above them.
     */
    get listingGain() {
      const r = this.listingRange;
      const band = Number(this.ipo?.issue?.price_high) || 0;
      const lot = Number(this.ipo?.issue?.lot_size) || 0;
      if (!r.has || !band || !lot) return { has: false };
      const low = Math.round((r.low - band) * lot);
      const high = Math.round((r.high - band) * lot);
      return { has: true, low, high, lot, same: low === high };
    },

    /* The ring is drawn against the number printed inside it, so it has to be
       that number. It used to divide by 50 — a 0-50% scale, which is a
       defensible choice for making typical GMPs look substantial, except
       nothing on screen said so: 25.4% drew a half-full ring and looked like
       a bug to everyone who saw it. A ring around a figure is read as that
       figure. Over 100% still caps at full. */
    get gaugeFrac() { return Math.min(1, Math.abs(this.d?.gmp.pct || 0) / 100); },
    get gmpPositive() { return (this.d?.gmp.gmp || 0) >= 0; },
    get shortUrl() {
      return String(this.ipo?.issue?.registrar_url || '')
        .replace(/^https?:\/\//, '').replace(/\/$/, '');
    },
    subBarPct(v) {
      const max = this.d?.subscription?.max_category || 1;
      return Math.max(4, (Number(v) || 0) / max * 100);
    },
    /** Sparkline path for the GMP trail. */
    /**
     * GMP sparkline path, in a fixed 300×70 coordinate space.
     *
     * The literal `viewBox="0 0 300 70"` in index.html must match SPARK_W /
     * SPARK_H — it cannot be bound, see the comment on that element.
     *
     * Inset by PAD so nothing is drawn on the boundary. The first and last
     * points used to sit exactly on x=0 and x=300, which put half of the
     * 3-wide stroke and most of the r=5 end dot outside the viewBox, so both
     * ends were sheared off even once the coordinate system worked.
     */
    get spark() {
      const pts = (this.d?.gmp?.series || []).map((p) => p.gmp);
      if (pts.length < 2) return null;
      const w = SPARK_W, h = SPARK_H, PAD_X = 7, PAD_Y = 8;
      const mn = Math.min(...pts), mx = Math.max(...pts), r = (mx - mn) || 1;
      const innerW = w - PAD_X * 2, innerH = h - PAD_Y * 2;
      const xy = pts.map((v, i) => [
        PAD_X + (i * innerW) / (pts.length - 1),
        h - PAD_Y - ((v - mn) / r) * innerH,
      ]);
      const line = 'M' + xy.map((p) => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' L');
      const last = xy[xy.length - 1];
      return {
        w, h, line,
        area: `${line} L${(w - PAD_X).toFixed(1)},${h} L${PAD_X.toFixed(1)},${h} Z`,
        lastX: last[0].toFixed(1), lastY: last[1].toFixed(1),
      };
    },
    /**
     * Newest-first slice of the GMP trail. The short frames only have ~445px
     * of usable height, so cap the rows there rather than let auto-shrink
     * squeeze the type down to unreadable.
     */
    trailRows(limit) {
      const cap = limit ?? (this.P.h >= 700 ? 7 : 4);
      return (this.d?.gmp?.series || []).slice(-cap).reverse();
    },
    /**
     * The company-profile strip for reel 1: [{label, value}].
     *
     * Stored as "Founded: 1985" on the Lists tab, split here so the label can
     * be styled and localised while the value is left exactly as filed. The
     * split is on the FIRST colon only — "HQ: Chennai, Tamil Nadu" is fine, but
     * so is a value that itself contains a colon, which a greedy split would
     * truncate.
     *
     * Read straight off `ipo.analysis`, not `loc`: these are language-
     * independent by design, so they are not part of the localised payload.
     * Only the label crosses languages, via `factFounded` and friends, and an
     * unrecognised label falls through to its stored English form rather than
     * rendering an i18n key at a viewer.
     */
    get aboutFacts() {
      const raw = (this.ipo && this.ipo.analysis && this.ipo.analysis.about_facts) || [];
      const KEY = { Founded: 'factFounded', HQ: 'factHQ',
                    Industry: 'factIndustry', Promoters: 'factPromoters' };
      return raw.map((line) => {
        const at = String(line).indexOf(':');
        if (at < 1) return { label: '', value: String(line).trim() };
        const label = String(line).slice(0, at).trim();
        const key = KEY[label];
        return { label: key ? this.t(key) : label,
                 value: String(line).slice(at + 1).trim() };
      }).filter((f) => f.value);
    },

    /**
     * Inline style for one row of the IPO dropdown, coloured by status.
     *
     * Reads `c.status`, which `DATA.index()` already fills from
     * `derive(ipo).dates.status` — the same derivation the card uses. Deriving
     * it again here would be a second opinion that could disagree with the
     * badge on screen, which is the one thing this must not do.
     *
     * A native <select> is what it is: Chrome honours `background` and `color`
     * on an <option> and essentially nothing else, so this is colour only — no
     * dots, no badges, no bold. That was the deliberate trade for keeping the
     * element (and its keyboard behaviour) rather than hand-rolling a listbox.
     *
     * The background stays near-black on every row instead of tinting: options
     * render on the OS popup surface, not the page, so a light tint that looks
     * right in the closed control can turn unreadable when the list opens.
     * Colouring the text alone is the version that survives both.
     */
    statusInk(status) {
      const INK = {
        open:      '#4ADE80',   // bidding now — the only one you can act on
        upcoming:  '#FBBF24',   // announced, not open
        closed:    '#F87171',   // bidding done, awaiting allotment
        allotment: '#60A5FA',   // allotment out / imminent
        listed:    '#94A3B8',   // history; muted so it recedes
      };
      return INK[status] || '#E2E8F0';
    },
    /* Is this the last day anyone can apply?
     *
     * Delegates to applyState rather than re-testing `close === today` here,
     * because that rule has a detail worth not duplicating: bidding stops at
     * the close-day cut-off, so "closes today" and "still open tomorrow" are
     * genuinely different situations, and two copies of the comparison would
     * eventually disagree about which. The catalogue row carries the same
     * status/open/close fields applyState reads off a board row.
     *
     * Reads `this.now`, which ticks every second — so an issue crossing
     * midnight relabels itself without a reload. */
    isLastDay(c) {
      return !!c && this.applyState(c).key === 'ap_lastday';
    },

    /* What a dropdown entry reads as.
     *
     * A native <option> can hold no elements, so every signal here has to be a
     * character — hence "●3" for readiness and the hourglass rather than a
     * blinking dot. Built in JS instead of concatenated in the template because
     * the last-day case changes two parts of the string at once, and an inline
     * expression doing that was already the longest line in index.html. */
    optionLabel(c) {
      if (!c) return '';
      const ready = c.ready_count ? `●${c.ready_count}` : '○';
      const last = this.isLastDay(c);
      // The status word carries the urgency, not just the glyph: the list is
      // colour-coded, and colour alone is no use to anyone who cannot separate
      // amber from green — the same reason the status word is here at all.
      const status = last ? 'LAST DAY' : (c.status || '');
      return `${last ? '⏳ ' : ''}${ready}  ${c.company}  ·  ${c.board}  ·  ${status}`;
    },

    optionStyle(c) {
      // Amber and bold beats the status colour on a last day. It is the one
      // row in the list that stops being actionable in a few hours.
      if (this.isLastDay(c)) {
        return 'background:#0B1120;color:#FBBF24;font-weight:800';
      }
      return `background:#0B1120;color:${this.statusInk(c && c.status)}`;
    },

    /* How many issues close today — for the count beside the picker, so a last
       day is visible without opening the list at all. */
    get lastDayCount() {
      return this.catalogue.filter((c) => this.isLastDay(c)).length;
    },

    /* Jump to the next issue closing today, cycling if there are several.
     *
     * Starts the search AFTER the current position rather than at the top, so
     * on a day with three closing issues the button walks all three instead of
     * bouncing back to the first every press. */
    async hopToLastDay() {
      const n = this.catalogue.length;
      if (!n) return;
      const from = this.catalogue.findIndex((c) => c.slug === this.slug);
      for (let step = 1; step <= n; step++) {
        const c = this.catalogue[((from < 0 ? -1 : from) + step + n) % n];
        if (this.isLastDay(c) && c.slug !== this.slug) {
          if (this.playing) this.stopPlay();
          await this.select(c.slug);
          return;
        }
      }
    },

    /* The closed control's own colour.
     *
     * A native <select> does not inherit the selected <option>'s colour — the
     * button face is styled entirely separately — so without this the list was
     * colour-coded and the thing you look at 99% of the time was not. Reads the
     * catalogue entry rather than `d.dates.status` so it cannot flicker to a
     * default while a newly picked IPO is still loading. */
    get selectStyle() {
      const c = this.catalogue.find((x) => x.slug === this.slug);
      return `color:${this.statusInk(c && c.status)}`;
    },

    /* Is the lot size published? Gates the trail's profit column and the rupee
       figure on reel 6 — both are `premium × lot`, and without a lot they are
       zero, which on screen reads as "no profit" rather than "not known yet".
       NSE publishes the lot only once an issue is near opening, so an upcoming
       IPO genuinely hits this. */
    get hasLot() { return Number(this.ipo?.issue?.lot_size) > 0; },

    /**
     * The application-level grey-market prices that actually have a figure.
     *
     * Returned as a list so the scene can size its grid to the count instead of
     * assuming two. Kostak is absent from InvestorGain by design (see
     * providers/investorgain.py — the field that looks like it is a pair, and
     * guessing which half is the Kostak is the misread ai.vet_gmp exists to
     * catch), so in practice this is usually one entry, not two.
     */
    get greyDeals() {
      const g = this.d?.gmp || {};
      return [
        { key: 'kostak', value: g.kostak },
        { key: 'sauda', value: g.sauda },
      ].filter((x) => x.value > 0);
    },

    /** Trail rows plus how many older days were dropped to fit the frame. */
    get trailTable() {
      const all = this.d?.gmp?.series || [];
      const rows = this.trailRows();
      return { rows, hidden: Math.max(0, all.length - rows.length) };
    },

    /**
     * The all-IPOs board, capped to what the frame can hold.
     *
     * This was a hardcoded `.slice(0, 7)` while the scene immediately before
     * it announces the full count — so an 11-IPO board promised 11 and then
     * showed 7, with half the card empty underneath. The cap now follows the
     * frame height like trailRows does, and `hidden` lets the scene admit
     * what it left out instead of truncating silently.
     */
    get boardTable() {
      const rows = this.boardRows || [];
      const cap = this.P.h >= 700 ? 12 : (this.P.h >= 520 ? 8 : 6);
      return { rows: rows.slice(0, cap), hidden: Math.max(0, rows.length - cap) };
    },

    /* Does the reel on screen have an all-IPOs mode at all?
     *
     * The mode toggle used to render on every reel and belonged to only one
     * of them — worse, its handler jumped to reel 2, so pressing "All IPOs"
     * while working on reel 4 silently moved you somewhere else. A control
     * that does nothing on four of six screens, and something unwanted on
     * the other two, is worse than no control. */
    get canBoard() { return !!(this.reel && this.reel.boardScenes); },
    get boardOn() { return this.canBoard && this.gmpMode === 'board'; },
    setBoardMode(mode) {
      this.gmpMode = mode;
      // Re-enter the CURRENT reel — switching mode changes its scene list, so
      // the scene index has to reset or it can point past the end.
      this.go(this.reelIndex, 0);
    },

    /* The subscription round-up, sorted the way a viewer reads it.
     *
     * Only issues actually taking bids: subscription on an issue that closed
     * last week is a historical fact, not something anyone can act on, and
     * it would push a live one off a board that only holds a dozen rows.
     * Sorted by closing date first — the ones running out of time are the
     * point of the reel — then by demand.
     */
    get subBoardTable() {
      const today = isoDate(new Date(this.now));
      const live = (this.boardRows || []).filter(
        (r) => r.subscription != null && r.open && r.open <= today
               && (!r.close || r.close >= today));
      live.sort((a, b) =>
        String(a.close || '9999').localeCompare(String(b.close || '9999'))
        || (b.subscription || 0) - (a.subscription || 0));
      const cap = this.P.h >= 700 ? 10 : (this.P.h >= 520 ? 7 : 5);
      return { rows: live.slice(0, cap), hidden: Math.max(0, live.length - cap),
               total: live.length };
    },

    /* Rough allotment odds from a category's own subscription multiple.
     *
     * When a category is oversubscribed, SEBI's rules put minimum-size
     * applications into a computerised draw, and the multiple IS the odds —
     * 5x means about one in five. Stated as "1 in N" rather than a percentage
     * because that is how a lottery is understood.
     *
     * This applies to retail AND to both HNI tranches: since the October 2021
     * reform, sHNI and bHNI allot their minimum application by draw too. It
     * does NOT apply to QIB, which is proportionate and partly discretionary
     * — see `oddsRows` for why that one is stated in words instead. */
    odds(multiple) {
      const m = Number(multiple) || 0;
      if (m <= 0) return '—';
      if (m < 1) return this.t('likely');
      return '1 in ' + (m < 10 ? m.toFixed(1) : Math.round(m));
    },
    get allotOdds() { return this.odds(this.d?.subscription?.retail); },

    /* Allotment odds for every category that HAS odds, plus the cheque each
     * one has to write.
     *
     * The stake scene only ever answered this for retail, which quietly
     * assumed the whole audience applies with ₹15,000. An HNI watching gets a
     * different answer on both halves — Tempsens closed at 61x retail against
     * 281x sHNI and 331x bHNI, and the sHNI ticket is fourteen lots, not one.
     *
     * A row is dropped when its multiple is absent rather than shown as a
     * zero: rows written before nii_small / nii_big existed have no split,
     * and "0x" would read as "nobody bid" rather than "not published". */
    get oddsRows() {
      const s = this.d?.subscription || {};
      if (!s.has_data) return [];
      const band = Number(this.ipo?.issue?.price_high) || 0;
      const lot = Number(this.ipo?.issue?.lot_size) || 0;
      const iss = this.ipo?.issue || {};

      const rows = [
        { key: 'retail', label: this.t('catRetail'), mult: s.retail,
          qty: lot, tone: SERIES.retail },
        // Both HNI rows stay in the NII pink family — they ARE the NII book,
        // and reel 3's bars have already taught the viewer that pink is NII.
        // Split by lightness rather than by hue so the two are separable
        // without claiming they are different categories. Every row is
        // direct-labelled, so no meaning rests on the colour alone.
        { key: 'shni', label: this.t('catShni'), mult: s.nii_small,
          qty: Number(iss.min_shni_qty) || 0, tone: SERIES.nii },
        { key: 'bhni', label: this.t('catBhni'), mult: s.nii_big,
          qty: Number(iss.min_bhni_qty) || 0, tone: '#F9A8D4' },
      ].filter((r) => Number(r.mult) > 0);

      return rows.map((r) => ({
        ...r,
        odds: this.odds(r.mult),
        // The minimum ticket, so "1 in 281" comes with what it costs to enter.
        cheque: r.qty && band ? r.qty * band : 0,
        lots: r.qty && lot ? Math.round(r.qty / lot) : 0,
      }));
    },

    /* QIB is not a draw and must not be printed beside three that are.
     * Institutional allotment is proportionate, and the anchor book is
     * allocated at the issuer's discretion before bidding even opens — so a
     * "1 in 303" beside a 302.88x QIB figure would be a fabricated statistic
     * about a process that holds no lottery. Stated in words, or not at all. */
    get qibNote() {
      const q = Number(this.d?.subscription?.qib) || 0;
      return q > 0 ? this.t('qibProportionate') : '';
    },

    /* ── is this reel recordable, and for how much longer ───────────────
     *
     * The studio could always render every reel for every IPO. What it could
     * not do was tell you which ones were worth recording — so a blank
     * financials table looked like a rendering fault, and a subscription reel
     * for an issue that shut on Friday looked exactly like one for an issue
     * closing tonight.
     *
     * Keyed off `this.now`, the ticking clock the countdown already uses, so
     * a window that shuts while the studio is open goes grey by itself
     * instead of waiting for a reload. Recomputed on every tick, which is
     * cheap: it is arithmetic over one record, not a fetch.
     */
    get ready() {
      if (!this.ipo || !this.d) return null;
      return readinessReport(this.ipo, this.d, new Date(this.now));
    },
    /** State of one reel, for the tab dots. `r` is a REELS entry. */
    reelReady(r) {
      const rr = this.ready;
      return rr ? rr.reels[r.n] : null;
    },
    readyTone(state) { return READY_TONE[state] || READY_TONE.blocked; },

    /* Colour per reservation category, fixed rather than positional.
     *
     * The rows are sorted biggest-slice-first, so a palette indexed by
     * position would recolour every category the moment one issue reserved
     * more for NII than QIB — and the stacked track and the cards under it
     * would stop agreeing across two IPOs in the same video.
     *
     * Retail is emerald on purpose: it is the slice the viewer is applying
     * into, and it should be the one the eye finds first. */
    resTone(key) {
      return ({
        retail:   { bar: 'linear-gradient(90deg,#22C55E,#4ADE80)', dot: '#22C55E' },
        qib:      { bar: 'linear-gradient(90deg,#6366F1,#818CF8)', dot: '#818CF8' },
        nii:      { bar: 'linear-gradient(90deg,#F59E0B,#FBBF24)', dot: '#FBBF24' },
        employee: { bar: 'linear-gradient(90deg,#0EA5E9,#38BDF8)', dot: '#38BDF8' },
      })[key] || { bar: 'linear-gradient(90deg,#475569,#64748B)', dot: '#64748B' };
    },

    /* The window, as a sentence you can act on.
     *
     * "Valid until Mon 25 Aug, 17:00" is the whole answer to the question
     * that started this — a card reading `open` with a countdown at 00:00 and
     * no way to tell which one was lying. */
    get readyNote() {
      const rs = this.reelReady(this.reel);
      if (!rs) return null;
      const w = rs.window;
      const when = (dt) => (dt ? this.fmtStamp(dt) : null);
      if (w.state === 'early') {
        return { tone: 'text-sky-300',
                 text: `Not yet — recordable from ${when(w.from)} (${w.starts}).` };
      }
      if (w.state === 'expired') {
        return { tone: 'text-rose-400',
                 text: `Window shut ${when(w.to)} — ${w.ends}.` };
      }
      const missing = rs.missing.length
        ? ` Missing: ${rs.missing.join(', ')}.`
        : (rs.stale.length ? ` ${rs.stale.join(' and ')} is not today's reading.` : '');
      const till = w.to ? `Valid until ${when(w.to)} — ${w.ends}.` : 'No end date on file.';
      return {
        tone: rs.state === 'ready' ? 'text-emerald-300'
          : rs.state === 'partial' ? 'text-amber-300' : 'text-rose-400',
        text: till + missing,
      };
    },

    /** 'Mon 25 Aug, 17:00' — a moment, not a date. */
    fmtStamp(dt) {
      const d = dt instanceof Date ? dt : new Date(dt);
      if (isNaN(d)) return '—';
      const day = d.toLocaleDateString('en-IN',
        { weekday: 'short', day: 'numeric', month: 'short' });
      const p = (n) => String(n).padStart(2, '0');
      return `${day}, ${p(d.getHours())}:${p(d.getMinutes())}`;
    },

    /* Colour for a subscription multiple, on the same scale compute.js uses
       for `sentiment` so the board and the single-IPO reel never disagree. */
    subTone(x) {
      const v = Number(x) || 0;
      if (v >= 10) return 'text-emerald-400';
      if (v >= 3) return 'text-emerald-300';
      if (v >= 1) return 'text-amber-300';
      return 'text-red-400';
    },
    /**
     * Can a viewer act on this IPO today, and how urgently?
     *
     * The board already showed GMP and a close date, which asks the viewer to
     * do the date arithmetic themselves while the reel is playing. This
     * answers the question they actually have — apply, wait, or too late —
     * and colours it so the answer survives being seen for two seconds.
     *
     * `open` alone is not the whole answer: an issue open until Friday and
     * one closing tonight are both "open", and only one of them is urgent.
     * Bids also stop at the close-day cut-off (17:00 by default, see
     * dates.close_time), so the last day is genuinely the last chance.
     */
    applyState(row) {
      const today = isoDate(new Date(this.now));
      const opens = row.open, shuts = row.close;

      if (row.status === 'open') {
        if (shuts && shuts === today) {
          return { key: 'ap_lastday', cls: 'text-amber-300', dot: '#F59E0B',
                   sub: this.fmtShort(shuts), urgent: true };
        }
        return { key: 'ap_open', cls: 'text-emerald-400', dot: '#22C55E',
                 sub: shuts ? this.fmtShort(shuts) : '', urgent: false };
      }
      if (row.status === 'upcoming') {
        return { key: 'ap_soon', cls: 'text-sky-300', dot: '#38BDF8',
                 sub: opens ? this.fmtShort(opens) : '', urgent: false };
      }
      // closed, allotment, listed — all mean the same thing to someone
      // deciding whether to put money in: the window is shut.
      //
      // Red rather than grey, deliberately. Grey reads as "inactive" and the
      // eye skips it; on a reel that plays in seconds the viewer needs a
      // stop signal as loud as the go signal. Muted rather than alarm-red,
      // because a closed issue is unavailable, not a loss — the one place a
      // true warning red belongs is a negative GMP, which the % column
      // already owns.
      return { key: 'ap_shut', cls: 'text-rose-400/80', dot: '#FB7185',
               sub: '', urgent: false };
    },

    /** How many are open right now — the number the board hook leads with. */
    get openCount() {
      return (this.boardRows || []).filter((r) => r.status === 'open').length;
    },

    /** Open issues whose last day is today. Worth calling out on its own. */
    get lastDayCount() {
      const today = isoDate(new Date(this.now));
      return (this.boardRows || [])
        .filter((r) => r.status === 'open' && r.close === today).length;
    },

    get countdown() {
      const at = this.d?.dates?.close_at;
      if (!at) return { over: false, txt: '—' };
      let ms = new Date(at).getTime() - this.now;
      if (isNaN(ms)) return { over: false, txt: '—' };
      if (ms <= 0) return { over: true, txt: '00:00' };
      const dd = Math.floor(ms / 864e5); ms -= dd * 864e5;
      const hh = Math.floor(ms / 36e5); ms -= hh * 36e5;
      const mm = Math.floor(ms / 6e4); ms -= mm * 6e4;
      const ss = Math.floor(ms / 1e3);
      const p = (n) => String(n).padStart(2, '0');
      return { over: false, txt: dd > 0 ? `${dd}d ${p(hh)}h ${p(mm)}m` : `${p(hh)}:${p(mm)}:${p(ss)}` };
    },

    // ── live editing ───────────────────────────────────────────────────
    /** Log/replace today's GMP without leaving the studio. */
    setTodayGmp(value) {
      if (!this.ipo) return;
      const v = Number(value);
      if (!Number.isFinite(v)) return;
      const today = isoDate();
      const hist = this.ipo.gmp_history || (this.ipo.gmp_history = []);
      const row = hist.find((p) => p.date === today);
      if (row) row.gmp = v;
      else hist.push({ date: today, gmp: v, source: 'studio' });
      this.recompute();
    },
    get todayGmpValue() {
      const today = isoDate();
      const row = (this.ipo?.gmp_history || []).find((p) => p.date === today);
      return row ? row.gmp : (this.d?.gmp.gmp ?? 0);
    },
    setSubDay(field, value) {
      if (!this.ipo) return;
      const days = this.ipo.subscription || (this.ipo.subscription = []);
      let last = days[days.length - 1];
      if (!last) { last = { day: 1, date: isoDate() }; days.push(last); }
      last[field] = Number(value) || 0;
      this.recompute();
    },

    copy(text, tag = 'x') {
      navigator.clipboard.writeText(text).then(() => {
        this.copied = tag; setTimeout(() => { this.copied = ''; }, 1600);
      }).catch(() => alert('Clipboard blocked — select and copy manually.'));
    },
  };

  // Merge in scripts/exports by descriptor, NOT by spread: `{...OUTPUT}` would
  // invoke its getters (script, caption) against the wrong `this` and freeze
  // them as stale strings — which also throws and kills the whole component.
  return Object.defineProperties(component, Object.getOwnPropertyDescriptors(OUTPUT));
}
