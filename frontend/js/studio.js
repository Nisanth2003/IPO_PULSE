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
    showSafe: false, showFooter: true, showProgress: true, rounded: true,
    leftOpen: true, rightOpen: true,
    playing: false, speed: 1, sceneProg: 0,
    mounted: false, overflow: false, copied: '',
    now: Date.now(),
    a: { gmp: 0, pct: 0, est: 0, total: 0, score: 0 },
    handle: '@IPOPulse',
    hasH2C: typeof html2canvas !== 'undefined',
    hasBackend: false,          // set by probeBackend(); gates the Trigger button

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
    GH_JOBS: [
      { id: 'daily',  label: 'Daily chain',   detail: 'sync → enrich → doctor → build → push' },
      { id: 'grey',   label: 'GMP chain',     detail: 'refresh GMP → push to the GMP tab' },
      { id: 'enrich', label: 'Fill gaps',     detail: 'research + RHP + analyse + translate' },
      { id: 'build',  label: 'Check workbook', detail: 'verify every record still renders, no network' },
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
      ['ratio', 'lang', 'densityBase', 'autoShrink', 'showFooter', 'showProgress', 'gmpMode']
        .forEach((k) => this.$watch(k, () => { this.savePrefs(); this.check(); }));
      ['scale', 'autoFit', 'bg', 'rounded', 'speed', 'handle']
        .forEach((k) => this.$watch(k, () => this.savePrefs()));

      this.probeBackend();
      this.initRemote();
      await this.loadCatalogue();
      this.$nextTick(() => this.autoFit && this.fit());
    },

    /* Is a local ipopulse server behind this page, or is this the published
       static site? Only the former can run jobs, so the Trigger button is
       hidden unless /api/health answers. Deliberately not awaited — a missing
       backend is the normal case on Pages and must not delay first paint. */
    probeBackend() {
      fetch('/api/health', { cache: 'no-store' })
        .then((r) => (r.ok ? r.json() : null))
        .then((d) => { this.hasBackend = !!(d && d.ok && d.auth); })
        .catch(() => { this.hasBackend = false; });
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
    saveToken() {
      const t = (this.gh.draft || '').trim();
      if (!t) return;
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
          this.gh.msg = `GitHub said ${r.status}: ${body.message || 'request refused'}`;
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
          gmpMode: this.gmpMode,
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
    get scenes() { return scenesFor(this.reel, this.gmpMode); },
    get sceneId() { return (this.scenes[this.scene] || this.scenes[0]).id; },
    get sceneCount() { return this.scenes.length; },
    get acc() { return this.reel.acc; },
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
