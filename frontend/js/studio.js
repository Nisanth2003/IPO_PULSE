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
     * On the token: it is the OWNER'S own fine-grained PAT, typed in by them,
     * kept in this browser's localStorage and sent only to api.github.com. It
     * is never written to the repo — which is the thing that actually matters,
     * because the repo is public. A visitor who is not the owner simply has no
     * token and sees a setup panel. Scope it to Actions:write on this one repo
     * and the worst case is that someone with access to this browser can run
     * the same jobs the cron already runs. */
    gh: { open: false, token: '', draft: '', owner: '', repo: '',
          msg: '', ok: false, runs: [] },
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
       'theme', 'showGif', 'gifSize']
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
      try { this.gh.token = localStorage.getItem('ipoPulse.ghToken') || ''; } catch (e) {}
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
      try { localStorage.setItem('ipoPulse.ghToken', t); } catch (e) {}
      this.gh.ok = true; this.gh.msg = 'Token saved in this browser.';
      this.ghRuns();
    },
    forgetToken() {
      this.gh.token = ''; this.gh.runs = [];
      try { localStorage.removeItem('ipoPulse.ghToken'); } catch (e) {}
      this.gh.ok = true; this.gh.msg = 'Token removed from this browser.';
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
        }));
      } catch (e) { /* private mode */ }
    },
    restorePrefs() {
      try {
        const s = JSON.parse(localStorage.getItem('ipoPulse.prefs') || '{}');
        Object.keys(s).forEach((k) => { if (s[k] !== undefined) this[k] = s[k]; });
        this.density = this.densityBase;
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
       accent, the card and the export background down with it. */
    get th() { return THEME_BY_KEY[this.theme] || THEMES[0]; },

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
    get brandGif() {
      const url = (this.ipo && this.ipo.sources && this.ipo.sources.gif) || '';
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

    get holdSeconds() {
      const base = (this.scenes[this.scene] || {}).hold || 4;
      return Math.max(1, base / (this.speed || 1));
    },
    get reelSeconds() {
      return Math.round(this.scenes.reduce((sum, s) => sum + s.hold, 0) / (this.speed || 1));
    },

    togglePlay() { this.playing ? this.stopPlay() : this.startPlay(); },
    startPlay() {
      this.playing = true; this.go(this.reelIndex, 0);
      clearInterval(this._timer);
      this._timer = setInterval(() => {
        this.sceneProg += 100 / (this.holdSeconds * 20);
        if (this.sceneProg >= 100) { this.sceneProg = 0; this.nextScene(); }
      }, 50);
    },
    stopPlay() { this.playing = false; this.sceneProg = 0; clearInterval(this._timer); },

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
    /** Shrink text until the scene fits; synchronous so hidden tabs still work. */
    check() {
      // Measuring before the record has loaded gives a meaningless "overflow"
      // and strands the text at the shrink floor.
      if (!this.ipo || !this.d) return;
      this.$nextTick(() => {
        const el = this.$refs.body, card = document.getElementById('capture');
        if (!el || !card) return;
        const setFs = (v) => card.style.setProperty('--fs', this.P.fs * v + 'px');
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
    key(e) {
      if (/INPUT|TEXTAREA|SELECT/.test(e.target.tagName)) return;
      const k = e.key;
      if (k >= '1' && k <= '6') { this.go(+k - 1, 0); return; }
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
        case 'b': case 'B': {
          const list = ['gradient', 'black', 'green', 'blue', 'checker'];
          this.bg = list[(list.indexOf(this.bg) + 1) % list.length]; break;
        }
        case '[': this.densityBase = Math.max(0.70, +(this.densityBase - 0.02).toFixed(2)); break;
        case ']': this.densityBase = Math.min(1.20, +(this.densityBase + 0.02).toFixed(2)); break;
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
    optionStyle(c) {
      return `background:#0B1120;color:${this.statusInk(c && c.status)}`;
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
