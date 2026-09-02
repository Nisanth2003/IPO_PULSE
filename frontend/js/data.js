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
              'Lists', 'I18n', 'Benchmarks', 'Sources'];

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

  /**
   * Drop the parsed copy so the next read re-fetches.
   *
   * The sheet is fetched once and held: a job run through the trigger panel
   * rewrites it underneath us, and without this the studio would keep
   * rendering what it read at page load and look like the job did nothing.
   */
  refresh() {
    this._loading = null;
  },

  /** Fetch and parse every tab once per page load. */
  async _load() {
    if (!this._loading) {
      this._loading = (async () => {
        const book = await SHEET.all(TABS);
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
                    gmp_history: [], subscription: [] };
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

    for (const row of rows('Benchmarks')) {
      const rec = at(row.slug);
      const metric = _s(row.metric).trim();
      if (rec && metric && _s(row.value).trim()) rec.benchmarks[metric] = _f(row.value);
    }

    for (const row of rows('Sources')) {
      const rec = at(row.slug);
      const role = _s(row.role).trim(), url = _s(row.url).trim();
      if (rec && role && url) rec.sources[role] = url;
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
