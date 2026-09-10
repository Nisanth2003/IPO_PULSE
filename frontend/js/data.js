/* Data access — the live Google Sheet, read directly.
 *
 * The backend writes the sheet (backend/ipopulse/sheets.py) and this reads
 * the same tabs. There is no published copy in between: no JSON to rebuild,
 * no workbook to commit, nothing that can be stale relative to the store.
 * A pipeline run is visible here on the next reload.
 *
 * The three accessors keep the shapes they have always had, so nothing
 * downstream changed when the store moved:
 *    index()     -> { count, ipos: [...] }        the company dropdown
 *    board()     -> { rows: [...] }               the all-IPOs GMP board
 *    ipo(slug)   -> { ipo, derived }              one full record
 *
 * The rebuild below mirrors two Python files and has to stay in step with
 * both: tables.from_tables for the tab layout, and models.from_dict for the
 * defaults. compute.js reads nested fields directly, so a record missing
 * `issue` or `dates` throws rather than degrading — normalise() guarantees
 * every container exists.
 */

const TABS = ['IPOs', 'Financials', 'GMP', 'Subscription',
              'Lists', 'I18n', 'Benchmarks', 'Sources', 'Published',
              // Reel 7's four. Separate from the IPO tabs on purpose: a
              // briefing is keyed by DATE, not by slug, so it cannot join the
              // record rebuild below — see `briefing()`.
              'Market', 'MarketNews', 'MarketSectors', 'MarketSetups',
              // Reel 8's two. Keyed by date like the Market tabs, and for the
              // same reason they cannot join the record rebuild: a scored
              // session is not a company.
              'Scorecard', 'ScorecardCalls',
              // Global key/value, not per-IPO: where the backend lives, and
              // which host this sheet's pipeline is meant to run on. Read at
              // runtime so it can be changed without rebuilding config.js.
              'Settings'];

/* The Market tabs carry a header row (tables.to_market_tables writes the
 * column list as row 0), so SHEET.table() keys them by name like every other
 * tab and there is no positional contract to keep in step. */

/* Coercions, mirroring models.py's _f / _d / _list. */
const _f = (v, dflt = 0) => {
  if (v === null || v === undefined || v === '') return dflt;
  const n = Number(v);
  return Number.isFinite(n) ? n : dflt;
};
const _s = (v) => (v === null || v === undefined ? '' : String(v));
const _d = (v) => {
  const s = _s(v).trim();
  return s ? s.slice(0, 10) : null;
};
/* Blank entries are dropped, not zeroed — an absent year is not a zero. */
const _nums = (arr) => arr.filter(v => _s(v).trim() !== '').map(v => _f(v));

const DATA = {
  _loading: null,
  _raw: null,

  /**
   * Drop the parsed copy so the next read re-fetches.
   *
   * The sheet is fetched once and held: a job run through the trigger panel
   * rewrites it underneath us, and without this the studio would keep
   * rendering what it read at page load and look like the job did nothing.
   */
  refresh() {
    this._loading = null;
    this._raw = null;
  },

  /** Fetch and parse every tab once per page load. */
  async _load() {
    if (!this._loading) {
      this._loading = (async () => {
        const book = await SHEET.all(TABS);
        // Kept, not discarded. `_rebuild` folds the IPO tabs into records
        // keyed by slug and drops everything else; the Market tabs are keyed
        // by DATE and have no slug to fold into, so `briefing()` needs the
        // rows as fetched. One fetch still serves both.
        this._raw = book;
        return this._rebuild(book);
      })().catch(err => {
        this._loading = null;            // let a retry actually retry
        throw err;
      });
    }
    return this._loading;
  },

  /** Tabs -> { slug: record }, normalised. */
  _rebuild(book) {
    const rows = (name) => SHEET.table(book.get(name));
    const records = {};

    for (const row of rows('IPOs')) {
      const slug = _s(row.slug).trim();
      if (!slug) continue;
      const rec = { slug, i18n: {}, benchmarks: {}, sources: {},
                    gmp_history: [], subscription: [], published: [] };
      for (const [col, value] of Object.entries(row)) {
        if (col === 'slug' || value === null || _s(value).trim() === '') continue;
        const path = col.split('.');
        let node = rec;
        for (const key of path.slice(0, -1)) node = (node[key] ??= {});
        node[path.at(-1)] = value;
      }
      records[slug] = rec;
    }

    const at = (slug) => records[_s(slug).trim()];

    // Financials: long rows back into parallel arrays, column by column.
    const fin = {};
    for (const row of rows('Financials')) {
      (fin[_s(row.slug).trim()] ??= []).push(row);
    }
    for (const [slug, group] of Object.entries(fin)) {
      const rec = at(slug);
      if (!rec) continue;
      const f = (rec.financials ??= {});
      f.years = group.map(r => _s(r.year).trim()).filter(Boolean);
      for (const metric of ['revenue', 'ebitda', 'pat', 'net_worth', 'total_debt']) {
        f[metric] = _nums(group.map(r => r[metric]));
      }
    }

    for (const row of rows('GMP')) {
      const rec = at(row.slug);
      if (!rec) continue;
      rec.gmp_history.push({
        date: _d(row.date), gmp: _f(row.gmp), kostak: _f(row.kostak),
        sauda: _f(row.sauda), source: _s(row.source) || 'manual',
      });
    }

    for (const row of rows('Subscription')) {
      const rec = at(row.slug);
      if (!rec) continue;
      rec.subscription.push({
        day: _f(row.day, 1), date: _d(row.date), qib: _f(row.qib),
        nii: _f(row.nii), retail: _f(row.retail),
        employee: _f(row.employee), total: _f(row.total),
        // NII split at the ₹10 lakh line SEBI drew in 2021. Separately
        // published and routinely 4x apart — Tempsens closed day 1 at 20.75x
        // sHNI against 8.39x bHNI, and "NII 12.51x" tells neither of them
        // their own odds. Absent on rows written before the columns existed,
        // which _f turns into 0 and the scene reads as "not published".
        nii_small: _f(row.nii_small), nii_big: _f(row.nii_big),
      });
    }

    // Lists: analysis.overview / green_flags / red_flags, allotment.steps
    const lists = {};
    for (const row of rows('Lists')) {
      const slug = _s(row.slug).trim(), field = _s(row.field).trim();
      const value = _s(row.value);
      if (!slug || !field || !value.trim()) continue;
      ((lists[slug] ??= {})[field] ??= []).push([_f(row.idx), value]);
    }
    for (const [slug, fields] of Object.entries(lists)) {
      const rec = at(slug);
      if (!rec) continue;
      for (const [field, items] of Object.entries(fields)) {
        items.sort((a, b) => a[0] - b[0]);
        const path = field.split('.');
        let node = rec;
        for (const key of path.slice(0, -1)) node = (node[key] ??= {});
        node[path.at(-1)] = items.map(([, v]) => v);
      }
    }

    // I18n: idx -1 marks an empty list, a blank idx marks a plain string.
    // Both are real states and differ from the key being absent, so the
    // presence of the row is what counts — not whether it has a value.
    const tongues = {};
    for (const row of rows('I18n')) {
      const slug = _s(row.slug).trim(), lang = _s(row.lang).trim();
      const key = _s(row.key).trim();
      if (!slug || !lang || !key) continue;
      const rawIdx = _s(row.idx).trim();
      const idx = rawIdx === '' ? null : _f(rawIdx);
      (((tongues[slug] ??= {})[lang] ??= {})[key] ??= []).push([idx, _s(row.value)]);
    }
    for (const [slug, langs] of Object.entries(tongues)) {
      const rec = at(slug);
      if (!rec) continue;
      for (const [lang, keys] of Object.entries(langs)) {
        const block = (rec.i18n[lang] ??= {});
        for (const [key, items] of Object.entries(keys)) {
          if (items.some(([i]) => i === -1)) block[key] = [];
          else if (items.length === 1 && items[0][0] === null) block[key] = items[0][1];
          else {
            items.sort((a, b) => (a[0] ?? 0) - (b[0] ?? 0));
            block[key] = items.map(([, v]) => v);
          }
        }
      }
    }

    /* What is already on the channel, so the studio can grey out a reel that
       is done. The mirror of tables.py's Published tab — read-only here; the
       backend writes it after an upload lands. */
    for (const row of rows('Published')) {
      const rec = at(row.slug);
      if (!rec || !_s(row.video_id).trim()) continue;
      (rec.published ??= []).push({
        reel: _f(row.reel), lang: _s(row.lang).trim(),
        video_id: _s(row.video_id).trim(), url: _s(row.url).trim(),
        privacy: _s(row.privacy).trim(),
        published: _s(row.published).trim(), title: _s(row.title),
      });
    }

    for (const row of rows('Benchmarks')) {
      const rec = at(row.slug);
      const metric = _s(row.metric).trim();
      if (rec && metric && _s(row.value).trim()) rec.benchmarks[metric] = _f(row.value);
    }

    /* `value` with a `url` fallback — the mirror of tables.py's SRC_COLS.
       The column was renamed when `facts` started writing NSE symbols and
       ISINs into it; a sheet written before that is still headed `url`, and
       reading only the new name would silently drop every source on it,
       taking reel 1's logo and the duplicate check's ticker with it. */
    for (const row of rows('Sources')) {
      const rec = at(row.slug);
      const role = _s(row.role).trim();
      const value = _s(row.value).trim() || _s(row.url).trim();
      if (rec && role && value) rec.sources[role] = value;
    }

    const out = {};
    for (const [slug, rec] of Object.entries(records)) out[slug] = normalise(rec);
    return out;
  },

  /** Catalogue for the company dropdown.
   *
   * Carries `ready` and `urgent` so the dropdown can say which companies have
   * a video waiting in them without opening each one. That was the gap: the
   * list showed 19 names and the only way to learn that 12 of them were
   * blocked on a missing field was to click through all 19. */
  async index() {
    const rows = await this._rows();
    return {
      schema: 1,
      count: rows.length,
      ipos: rows.map(r => ({
        slug: r.slug, company: r.company, initials: r.initials,
        board: r.board, status: r.status,
        ready: r.ready, ready_count: r.ready_count, urgent: r.urgent,
        /* The calendar, because the dropdown judges more than status.
         *
         * `status` alone cannot answer "does this shut tonight" — an issue open
         * until Friday and one closing in three hours are both 'open'. The
         * studio's applyState() reads `open` and `close` to tell them apart,
         * and with these absent it silently took the not-urgent branch for
         * every row: the ⏳ LAST DAY badge never appeared and no option ever
         * said LAST DAY, on a day when six issues were closing.
         *
         * Nothing errored, which is what made it invisible — `undefined ===
         * today` is simply false. Keep these in step with what applyState
         * reads. */
        open: r.open, close: r.close, listing: r.listing,
      })),
    };
  },

  /** All tracked IPOs, one row each, liveliest first. */
  async board() {
    return { schema: 1, rows: await this._rows() };
  },

  /* ── reel 7: the pre-market briefing ───────────────────────────────
   *
   * Keyed by ISO date rather than by slug, because a briefing is a statement
   * about one morning and there is no company to hang it on. `briefing()`
   * returns the NEWEST stored day, which is what the studio wants: the reel
   * is recorded the morning it was built, and `readiness` expires it at
   * 09:15 IST rather than this function guessing.
   *
   * Numbers are coerced here and not in the card, so a blank cell reaches the
   * scene as 0 rather than as the string "" — the same rule `_f` enforces for
   * every IPO field.
   */
  /* Reel 8's scorecard, keyed by date. {days, latest, record}.
   *
   * `direction` is decoded from the sheet's own yes/no words rather than
   * coerced with `_f`, because THREE states matter and a boolean cast
   * collapses two of them: true is a call that worked, false is a call that
   * was wrong, and null is one that could not be scored either way. Writing
   * `!!cell` here would turn every unscoreable call into a wrong one.
   */
  async scorecard(day) {
    await this._load();
    const rows = (name) => SHEET.table(this._raw.get(name));

    const yesno = (v) => {
      const t = _s(v).trim().toLowerCase();
      if (t === 'yes' || t === 'true' || t === '1') return true;
      if (t === 'no' || t === 'false' || t === '0') return false;
      return null;
    };

    const days = {};
    for (const r of rows('Scorecard')) {
      const date = _d(r.date);
      if (!date) continue;
      days[date] = {
        date,
        scored_at: _s(r.scored_at),
        setups: _f(r.setups),
        directional: _f(r.directional),
        direction_right: _f(r.direction_right),
        direction_rate: _f(r.direction_rate),
        resolved: _f(r.resolved),
        target_hit: _f(r.target_hit),
        hit_rate: _f(r.hit_rate),
        voided: _f(r.voided), no_trade: _f(r.no_trade),
        bias_called: _s(r.bias_called), bias_actual: _s(r.bias_actual),
        bias_correct: yesno(r.bias_correct),
        nifty_close: _f(r.nifty_close), nifty_pct: _f(r.nifty_pct),
        median_stop_share: _f(r.median_stop_share),
        median_target_share: _f(r.median_target_share),
        causes: _s(r.causes), note: _s(r.note),
        calls: [],
      };
    }

    for (const r of rows('ScorecardCalls')) {
      const date = _d(r.date);
      const host = days[date];
      if (!host) continue;
      host.calls.push({
        idx: _f(r.idx), symbol: _s(r.symbol), side: _s(r.side),
        entry: _f(r.entry), target: _f(r.target), stop: _f(r.stop),
        high: _f(r.high), low: _f(r.low), close: _f(r.close),
        verdict: _s(r.verdict), why: _s(r.why),
        direction: yesno(r.direction), stock_pct: _f(r.stock_pct),
        cause: _s(r.cause), because: _s(r.because),
        sector: _s(r.sector), sector_pct: _f(r.sector_pct),
        market_pct: _f(r.market_pct),
        stop_share: _f(r.stop_share), target_share: _f(r.target_share),
        gap_pct: _f(r.gap_pct), entry_pos: _f(r.entry_pos),
      });
    }
    for (const d of Object.values(days)) {
      d.calls.sort((a, b) => a.idx - b.idx);
      // Split once here rather than filtering in three places in the
      // template: the cards need the two groups and Alpine re-evaluates an
      // inline filter on every render pass.
      d.right = d.calls.filter((c) => c.direction === true);
      d.wrong = d.calls.filter((c) => c.direction === false);
    }

    const keys = Object.keys(days).sort();
    const picked = days[_d(day)] || (keys.length ? days[keys[keys.length - 1]] : null);

    /* The running rate — the only number the reel may state. Summed from the
     * stored rows so the reel and the sheet cannot disagree about what was
     * published. `enough` mirrors scorecard.record's five-session floor. */
    const all = keys.map((k) => days[k]);
    const sum = (f) => all.reduce((n, d) => n + f(d), 0);
    const called = sum((d) => d.directional);
    const right = sum((d) => d.direction_right);
    const resolved = sum((d) => d.resolved);
    const hits = sum((d) => d.target_hit);
    const biasDays = all.filter((d) => d.bias_correct !== null);

    return {
      days, latest: picked, dates: keys,
      record: {
        sessions: keys.length,
        setups: sum((d) => d.setups),
        directional: called, direction_right: right,
        direction_rate: called ? Math.round(1000 * right / called) / 10 : null,
        resolved, target_hit: hits,
        hit_rate: resolved ? Math.round(1000 * hits / resolved) / 10 : null,
        voided: sum((d) => d.voided),
        bias_scored: biasDays.length,
        bias_right: biasDays.filter((d) => d.bias_correct).length,
        enough: keys.length >= 5,
      },
    };
  },

  async briefing(day) {
    await this._load();                 // fills this._raw
    const rows = (name) => SHEET.table(this._raw.get(name));

    const days = {};
    for (const r of rows('Market')) {
      const date = _d(r.date);
      if (!date) continue;
      days[date] = {
        date,
        // `trading` and `partial` are written as text by the Python side.
        trading: !/^(false|0|no)$/i.test(_s(r.trading).trim()),
        why_closed: _s(r.why_closed),
        // Where the numbers came from and how stale they are. `trading` and
        // `why_closed` above are about TODAY; these are about the gap behind
        // the levels. On a Monday levels_age_days is 3 and market_closed
        // reads "market closed Sat, Sun" — the difference between a
        // one-night-old range and one with a weekend of news over it.
        // Absent on rows written before 9 Sep 2026, where 0/'' honestly means
        // "unknown" rather than "fresh".
        levels_from: _s(r.levels_from),
        levels_age_days: _f(r.levels_age_days),
        market_closed: _s(r.market_closed),
        at: _s(r.at),
        nifty: _f(r.nifty), nifty_pct: _f(r.nifty_pct),
        nifty_prev: _f(r.nifty_prev),
        banknifty: _f(r.banknifty), banknifty_pct: _f(r.banknifty_pct),
        advances: _f(r.advances), declines: _f(r.declines),
        unchanged: _f(r.unchanged),
        bias: _s(r.bias) || 'flat',
        outlook: _s(r.outlook), levels_note: _s(r.levels_note),
        model: _s(r.model), partial: _s(r.partial), notes: _s(r.notes),
        news: [], sectors: [], longs: [], shorts: [],
      };
    }

    for (const r of rows('MarketNews')) {
      const b = days[_d(r.date)];
      if (!b) continue;
      b.news.push({
        idx: _f(r.idx), headline: _s(r.headline), body: _s(r.body),
        why: _s(r.why), sector: _s(r.sector),
        // Stored comma-separated; the card wants chips.
        tickers: _s(r.tickers).split(',').map(t => t.trim()).filter(Boolean),
        source: _s(r.source), url: _s(r.url), image: _s(r.image),
        at: _s(r.at),
      });
    }
    for (const r of rows('MarketSectors')) {
      const b = days[_d(r.date)];
      if (!b) continue;
      b.sectors.push({ sector: _s(r.sector), pct: _f(r.pct),
                       last: _f(r.last), stance: _s(r.stance) });
    }
    for (const r of rows('MarketSetups')) {
      const b = days[_d(r.date)];
      if (!b) continue;
      const row = {
        side: _s(r.side).toLowerCase(), rank: _f(r.rank),
        symbol: _s(r.symbol), last: _f(r.last), entry: _f(r.entry),
        target: _f(r.target), stop: _f(r.stop), pivot: _f(r.pivot),
        r1: _f(r.r1), s1: _f(r.s1), pct: _f(r.pct),
        close_pos: _f(r.close_pos), reason: _s(r.reason),
        invalidates: _s(r.invalidates),
      };
      (row.side === 'short' ? b.shorts : b.longs).push(row);
    }

    for (const b of Object.values(days)) {
      b.news.sort((x, y) => x.idx - y.idx);
      // Strongest first, which is the order the sector strip reads in.
      b.sectors.sort((x, y) => y.pct - x.pct);
      b.longs.sort((x, y) => x.rank - y.rank);
      b.shorts.sort((x, y) => x.rank - y.rank);
      // Risk/reward per setup, computed here rather than stored: it is
      // arithmetic on three numbers already on the row, and a stored copy
      // could disagree with them after a hand edit.
      for (const row of [...b.longs, ...b.shorts]) {
        const risk = Math.abs(row.entry - row.stop);
        row.rr = risk ? +(Math.abs(row.target - row.entry) / risk).toFixed(2) : 0;
      }
    }

    const stored = Object.keys(days).sort();
    const pick = day && days[day] ? day : stored[stored.length - 1];
    return { schema: 1, days: stored, briefing: pick ? days[pick] : null };
  },

  /* The sheet's Settings tab as {key: value}.
   *
   * Never throws and never rejects: this is read on the studio's boot path
   * before anything is on screen, and a missing tab is the normal state of a
   * sheet that predates it. An absent key means "auto", which is what every
   * caller already does when it gets ''.
   */
  async settings() {
    try {
      await this._load();
      const rows = SHEET.table(this._raw.get('Settings'));
      const out = {};
      for (const r of rows) {
        const k = _s(r.key).trim();
        if (k) out[k] = _s(r.value).trim();
      }
      return out;
    } catch (_) {
      return {};
    }
  },

  /** Full record: { ipo, derived }. */
  async ipo(slug) {
    const records = await this._load();
    const rec = records[slug];
    if (!rec) throw new Error(`${slug}: not in the sheet`);
    return { schema: 1, ipo: rec, derived: derive(rec) };
  },

  /** Every IPO as a board row, sorted. Mirrors publish.board_row. */
  async _rows() {
    const records = await this._load();
    const rows = Object.values(records).map(ipo => {
      const d = derive(ipo);
      const g = d.gmp, s = d.subscription;
      // null, not 0, when nothing has been read: an empty gmp_history is
      // not a genuine zero premium, and publishing 0 put "₹0 · 0.00%" on
      // the board for IPOs nobody had a reading for.
      const seen = ipo.gmp_history.length > 0;
      // Which of the six can actually be shot, judged on the clock as well as
      // on the data — see readiness.js. Computed here rather than in the
      // component so the dropdown, the board and the reel tabs all read one
      // answer instead of three that can disagree.
      const rr = readinessReport(ipo, d);
      return {
        slug: ipo.slug,
        ready: rr.ready,
        ready_count: rr.ready_count,
        urgent: rr.urgent,
        company: ipo.company || ipo.slug,
        initials: d.initials,
        board: ipo.board,
        status: d.dates.status,
        price_low: ipo.issue.price_low,
        price_high: ipo.issue.price_high,
        lot_size: ipo.issue.lot_size,
        min_investment: d.issue.min_investment,
        has_gmp: seen,
        gmp: seen ? g.gmp : null,
        gmp_pct: seen ? g.pct : null,
        est_listing: seen ? g.est_listing : null,
        gain_per_lot: seen ? g.gain_per_lot : null,
        movement: seen ? g.movement : null,
        subscription: s.has_data ? s.total : null,
        // Retail separately, for reel 3's all-IPOs board: it is the column
        // the audience is actually in, and "overall 40x" hides the fact that
        // retail was 2x. null, not 0, for the usual reason — no reading is
        // not a reading of zero.
        retail: s.has_data ? s.retail : null,
        sub_day: s.has_data ? s.day : null,
        open: d.dates.open,
        close: d.dates.close,
        listing: d.dates.listing,
      };
    });
    const order = { open: 0, upcoming: 1, closed: 2, allotment: 3, listed: 4 };
    rows.sort((a, b) =>
      (order[a.status] ?? 9) - (order[b.status] ?? 9)
      || (b.gmp_pct || 0) - (a.gmp_pct || 0));
    return rows;
  },

  /**
   * Pull the translated prose for a language, falling back to the English
   * source field by field. The backend writes ipo.i18n.<lang> via Gemini;
   * anything it could not translate simply stays English rather than blank.
   */
  localized(ipo, lang) {
    const src = ipo.analysis || {};
    const base = {
      overview: src.overview || [],
      background: src.background || [],
      green_flags: src.green_flags || [],
      red_flags: src.red_flags || [],
      growth: src.growth || '',
      valuation: src.valuation || '',
      risk: src.risk || '',
      sector: ipo.sector || '',
      allotment_steps: (ipo.allotment && ipo.allotment.steps) || [],
    };
    if (lang === 'en') return base;

    const tr = (ipo.i18n || {})[lang] || {};
    const pick = (key) => {
      const val = tr[key];
      if (Array.isArray(base[key])) {
        return Array.isArray(val) && val.length ? val : base[key];
      }
      return typeof val === 'string' && val.trim() ? val : base[key];
    };
    return {
      overview: pick('overview'),
      background: pick('background'),
      green_flags: pick('green_flags'),
      red_flags: pick('red_flags'),
      growth: pick('growth'),
      valuation: pick('valuation'),
      risk: pick('risk'),
      sector: pick('sector'),
      allotment_steps: pick('allotment_steps'),
    };
  },

  /** Has this IPO actually been translated into `lang`? */
  hasTranslation(ipo, lang) {
    if (lang === 'en') return true;
    const tr = (ipo.i18n || {})[lang];
    return !!(tr && Object.keys(tr).length);
  },
};

/* Defaults and coercions, mirroring models.py's from_dict.
 *
 * compute.js reads ipo.issue.price_low and friends straight through, so a
 * record with a missing container throws rather than rendering a blank.
 * Every field the model guarantees is guaranteed here too. */
function normalise(r) {
  const issue = r.issue || {}, dates = r.dates || {};
  const fin = r.financials || {}, an = r.analysis || {}, al = r.allotment || {};
  const exchanges = typeof issue.exchanges === 'string'
    ? issue.exchanges.split(',').map(s => s.trim()).filter(Boolean)
    : (issue.exchanges || []);

  return {
    slug: r.slug,
    company: _s(r.company),
    initials: _s(r.initials),
    board: _s(r.board) || 'Mainboard',
    sector: _s(r.sector),
    issue: {
      fresh_cr: _f(issue.fresh_cr),
      ofs_cr: _f(issue.ofs_cr),
      total_cr: _f(issue.total_cr),
      price_low: _f(issue.price_low),
      price_high: _f(issue.price_high),
      lot_size: Math.trunc(_f(issue.lot_size)),
      shares_post_issue_cr: _f(issue.shares_post_issue_cr),
      // Minimum application in SHARES for the two HNI tranches. Retail's
      // minimum is one lot and needs no field; sHNI and bHNI have their own
      // floors (₹2 lakh and ₹10 lakh worth) and nothing else implies them.
      min_shni_qty: _f(issue.min_shni_qty),
      min_bhni_qty: _f(issue.min_bhni_qty),
      /* ── the reservation split, in SHARES ────────────────────────────
       *
       * This normaliser is the browser's half of the models.py schema, and a
       * field absent HERE does not exist in the studio no matter how correctly
       * the backend wrote it. That is exactly what happened: the columns were
       * added to models.py, tables.py, compute.py, compute.js, readiness.py and
       * readiness.js, the sheet was backfilled — and reel 1's reservation scene
       * still never appeared, because this map dropped every value on the way
       * in. compute.js then read undefined, reported has_data:false, and
       * `scenesFor` correctly hid a scene that had nothing to show.
       *
       * A silent one, too: no error anywhere, just a scene that never rendered.
       * If you add a field to Issue in models.py, add it here in the same
       * commit.
       *
       * shares_qib INCLUDES the anchor book; shares_anchor is a subset of it
       * and is never summed with the others. See compute.js reservation().
       */
      shares_qib: _f(issue.shares_qib),
      shares_nii: _f(issue.shares_nii),
      shares_retail: _f(issue.shares_retail),
      shares_employee: _f(issue.shares_employee),
      shares_shareholders: _f(issue.shares_shareholders),
      shares_total: _f(issue.shares_total),
      shares_anchor: _f(issue.shares_anchor),
      registrar: _s(issue.registrar),
      registrar_url: _s(issue.registrar_url),
      exchanges: exchanges.length ? exchanges : ['BSE', 'NSE'],
    },
    dates: {
      announced: _d(dates.announced), open: _d(dates.open),
      close: _d(dates.close), close_time: _s(dates.close_time) || '17:00',
      allotment: _d(dates.allotment), refund: _d(dates.refund),
      listing: _d(dates.listing),
    },
    financials: {
      years: (fin.years || []).map(_s),
      revenue: fin.revenue || [], ebitda: fin.ebitda || [],
      pat: fin.pat || [], net_worth: fin.net_worth || [],
      total_debt: fin.total_debt || [],
      eps: _f(fin.eps), pe_peer_avg: _f(fin.pe_peer_avg),
    },
    // Sorted the way the model sorts them: the reels read the last entry as
    // "latest", so order is load-bearing, not cosmetic.
    gmp_history: (r.gmp_history || []).sort((a, b) =>
      _s(a.date).localeCompare(_s(b.date))),
    subscription: (r.subscription || []).sort((a, b) => a.day - b.day),
    analysis: {
      overview: an.overview || [],
      green_flags: an.green_flags || [],
      red_flags: an.red_flags || [],
      // Reel 1's company-profile strip. Reading the Lists tab is generic — it
      // splits the field name on '.' and plants the array — but THIS step is
      // not: it rebuilds `analysis` key by key, so a field missing from here is
      // silently dropped however correctly it was stored. That is the mirror
      // tables.py means by "change a column here and this file has to change
      // with it", and it applies to LIST_FIELDS too.
      about_facts: an.about_facts || [],
      background: an.background || [],
      growth: _s(an.growth), valuation: _s(an.valuation), risk: _s(an.risk),
      growth_tone: _s(an.growth_tone) || 'good',
      valuation_tone: _s(an.valuation_tone) || 'warn',
      score: _f(an.score),
      // Empty, deliberately — the mirror of models.py's Analysis, where the
      // reasoning for it is written out. Short version: nothing in the
      // pipeline writes these, so a default here published an unearned buy
      // call on every issue. Blank means no call has been made, and
      // `readiness` already refuses to call reel 5 recordable without one.
      verdict: _s(an.verdict),
      verdict_text: _s(an.verdict_text),
      reco_retail: _s(an.reco_retail),
      reco_hni: _s(an.reco_hni),
      reco_long: _s(an.reco_long),
    },
    allotment: {
      status: _s(al.status) || 'expected',
      listing_low: _f(al.listing_low), listing_high: _f(al.listing_high),
      steps: al.steps || [],
    },
    i18n: r.i18n || {},
    benchmarks: r.benchmarks || {},
    sources: r.sources || {},
    notes: _s(r.notes),
  };
}
