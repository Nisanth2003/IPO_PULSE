/* Read the Google Sheet the backend writes to.
 *
 * The store is a live spreadsheet (backend/ipopulse/sheets.py). The site does
 * not get a published copy of it — it reads the same tabs the pipeline wrote,
 * so what you see here is the data as it stands right now, not as it stood
 * when someone last deployed.
 *
 * No key, no server, no library. Google exposes any sheet that is readable by
 * "anyone with the link" as CSV:
 *
 *   https://docs.google.com/spreadsheets/d/<ID>/gviz/tq?tqx=out:csv&sheet=<TAB>
 *
 * That endpoint takes no credentials, which is the only reason a static page
 * can read it at all — a service-account key could never ship to a browser.
 * The flip side, worth being deliberate about: the sheet id sits in this
 * file, and this file is public, so the sheet is public. Do not put anything
 * in it you would not publish.
 */

const SHEET = {
  /* Filled in from config.js so the id is one edit, not many. */
  id: (typeof SHEET_ID !== 'undefined' && SHEET_ID) || '',

  url(tab) {
    return `https://docs.google.com/spreadsheets/d/${this.id}` +
           `/gviz/tq?tqx=out:csv&sheet=${encodeURIComponent(tab)}` +
           /* Google caches these hard; the whole point is live data. */
           `&_=${Date.now()}`;
  },

  /**
   * Parse CSV into rows of strings.
   *
   * Hand-written rather than split(',') because the data makes both of the
   * classic mistakes fatal: translated prose contains commas, and analysis
   * bullets can contain a newline — both inside quotes, where they are
   * content rather than structure. A doubled "" inside a quoted field is one
   * literal quote.
   */
  parseCsv(text) {
    const rows = [];
    let row = [], field = '', quoted = false;

    for (let i = 0; i < text.length; i++) {
      const ch = text[i];

      if (quoted) {
        if (ch === '"') {
          if (text[i + 1] === '"') { field += '"'; i++; }   // escaped quote
          else quoted = false;
        } else field += ch;
        continue;
      }

      if (ch === '"') { quoted = true; }
      else if (ch === ',') { row.push(field); field = ''; }
      else if (ch === '\n' || ch === '\r') {
        // Swallow CRLF as one break, and ignore blank lines between rows.
        if (ch === '\r' && text[i + 1] === '\n') i++;
        row.push(field); field = '';
        if (row.some((c) => c !== '')) rows.push(row);
        row = [];
      }
      else field += ch;
    }
    row.push(field);
    if (row.some((c) => c !== '')) rows.push(row);
    return rows;
  },

  /** One tab as rows. Missing tab -> [] rather than an error. */
  async tab(name) {
    const res = await fetch(this.url(name), { cache: 'no-store' });
    if (!res.ok) throw new Error(`${name}: HTTP ${res.status}`);
    const text = await res.text();
    // Google answers an unreadable sheet with an HTML sign-in page, not a
    // 4xx — so a body that is not CSV means the sharing setting, every time.
    if (/^\s*</.test(text)) {
      throw new Error(
        'the sheet is not readable without signing in — set it to ' +
        '"Anyone with the link can view"');
    }
    return this.parseCsv(text);
  },

  /** Every tab at once. */
  async all(names) {
    const got = await Promise.all(names.map((n) => this.tab(n)));
    return new Map(names.map((n, i) => [n, got[i]]));
  },

  /** Rows -> objects keyed by the tab's own header row. */
  table(rows) {
    if (!rows || !rows.length) return [];
    const header = rows[0].map((h) => String(h == null ? '' : h).trim());
    return rows.slice(1)
      .filter((r) => r.some((c) => c != null && String(c).trim() !== ''))
      .map((r) => {
        const obj = {};
        header.forEach((name, i) => { if (name) obj[name] = r[i] ?? null; });
        return obj;
      });
  },
};
