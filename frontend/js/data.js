/* Data access.
 *
 * One seam, two futures. Today `BASE` points at the static JSON the backend
 * publishes into frontend/data/ — which is all GitHub Pages can serve. If you
 * later stand up a real HTTP API, change BASE to its origin and nothing else
 * in the app has to move, because the shapes are identical.
 *
 * Reading order for a session:
 *    index.json  -> populates the company dropdown
 *    board.json  -> the all-IPOs GMP board (Daily GMP reel, mode B)
 *    ipo/<slug>.json -> the full record for the selected company
 */

const DATA = {
  // Relative so it works on GitHub Pages under /<repo>/ without config.
  BASE: './data',

  async _json(path) {
    const res = await fetch(`${this.BASE}/${path}`, { cache: 'no-store' });
    if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`);
    return res.json();
  },

  /** Catalogue for the company dropdown. */
  async index() {
    return this._json('index.json');
  },

  /** All tracked IPOs, one row each. */
  async board() {
    return this._json('board.json');
  },

  /** Full record: { ipo, derived, generated_at }. */
  async ipo(slug) {
    return this._json(`ipo/${slug}.json`);
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
