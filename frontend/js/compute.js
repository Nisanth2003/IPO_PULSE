/* Derived metrics — a faithful mirror of backend/ipopulse/compute.py.
 *
 * Why duplicated: the backend computes these for the Excel report and the
 * published JSON, but you also edit numbers live in the sidebar mid-recording,
 * and the card has to update instantly without a rebuild. Both sides must
 * agree, so if you change a formula here, change it there too.
 *
 * As on the backend: no AI-generated numbers ever land in this file.
 */

const num = (v) => { const n = Number(v); return Number.isFinite(n) ? n : 0; };
const pct = (part, whole) => (whole ? (part / whole) * 100 : 0);
const round = (v, dp = 0) => { const m = 10 ** dp; return Math.round(num(v) * m) / m; };

/* The LOCAL calendar date as YYYY-MM-DD.
 *
 * `toISOString()` would be shorter and is what this used to do, but it
 * returns the UTC date — so between midnight and 05:30 IST the browser
 * thought it was still yesterday while the backend, which uses
 * `datetime.now().date()`, had already rolled over. Status and GMP staleness
 * then disagreed between the published JSON and the studio for five and a
 * half hours every night. The audience is Indian; the day that matters is
 * the local one. */
const isoDate = (d) => {
  const dt = d instanceof Date ? d : new Date();
  const p = (n) => String(n).padStart(2, '0');
  return `${dt.getFullYear()}-${p(dt.getMonth() + 1)}-${p(dt.getDate())}`;
};

function cagr(first, last, years) {
  if (first <= 0 || last <= 0 || years <= 0) return 0;
  return ((last / first) ** (1 / years) - 1) * 100;
}

/* Benchmarks — mirror of BENCHMARKS in compute.py.
 * A number alone means nothing to a viewer: is a 15% EBITDA margin good?
 * Every headline metric gets a reference line so the card can show which side
 * of "healthy" it lands on. Broad rules of thumb, overridable per IPO. */
const BENCHMARKS = {
  ebitda_margin: { good_at: 15,   higher: true,  unit: '%' },
  pat_margin:    { good_at: 8,    higher: true,  unit: '%' },
  revenue_cagr:  { good_at: 15,   higher: true,  unit: '%' },
  pat_cagr:      { good_at: 15,   higher: true,  unit: '%' },
  ronw:          { good_at: 15,   higher: true,  unit: '%' },
  debt_equity:   { good_at: 1,    higher: false, unit: 'x' },
  pe:            { good_at: null, higher: false, unit: 'x' },   // vs peers
};

function judge(metric, value, goodAt = null, overrides = null) {
  const spec = { ...(BENCHMARKS[metric] || { good_at: null, higher: true, unit: '' }) };
  if (overrides && overrides[metric] != null) spec.good_at = num(overrides[metric]);
  if (goodAt != null) spec.good_at = goodAt;

  const line = spec.good_at, higher = !!spec.higher, v = num(value);
  if (!line) {
    return { value: v, good_at: line, higher_is_better: higher,
             verdict: 'na', unit: spec.unit, pos: 0, mark: 0, gap_pct: 0 };
  }
  const good = higher ? v >= line : v <= line;
  // benchmark sits at 50% of the track — the marker is the story
  const span = Math.max(Math.abs(v), Math.abs(line)) * 2 || 1;
  const clamp = (x) => Math.max(0, Math.min(100, Math.round(x)));
  return {
    value: round(v, 2), good_at: line, higher_is_better: higher,
    verdict: good ? 'good' : 'poor', unit: spec.unit,
    pos: clamp(v / span * 100), mark: clamp(line / span * 100),
    gap_pct: round((v - line) / line * 100, 1),
  };
}

function issueMetrics(ipo) {
  const i = ipo.issue || {};
  // Mirrors compute.py issue_metrics: fall back to a stated total when the
  // fresh/OFS split is unknown, and flag that so a scene can say so.
  const split = num(i.fresh_cr) + num(i.ofs_cr);
  const total = split || num(i.total_cr);
  return {
    total_cr: round(total, 2),
    has_split: split > 0,
    fresh_pct: round(pct(num(i.fresh_cr), split), 1),
    ofs_pct: round(pct(num(i.ofs_cr), split), 1),
    min_investment: Math.round(num(i.lot_size) * num(i.price_high)),
    is_fresh_heavy: num(i.fresh_cr) >= num(i.ofs_cr),
    reservation: reservation(ipo),
  };
}

/* How the issue is carved up between QIB, NII, retail and employees.
 * Mirrors compute.py reservation() — change one, change the other.
 *
 * Not the Subscription tab's qib/nii/retail, which is a different quantity:
 * this is how big each slice IS, that is how many times it was BID for. An
 * issue can reserve 35% for retail and have retail bid it 40x.
 *
 * Two rules, both about not inventing numbers:
 *  - the denominator is shares_total, always. Missing it, nothing is computed,
 *    because normalising against the slices that happen to be present would
 *    report a 50% QIB tranche as 100% on a record still missing its retail row.
 *  - no standard split is assumed for a missing slice. Employee and
 *    shareholder quotas move everything, and SME issues do not follow the
 *    mainboard pattern at all.
 */
function reservation(ipo) {
  const i = ipo.issue || {};
  const total = num(i.shares_total);
  /* shares_anchor is deliberately NOT a slice here: it is a subset of
     shares_qib, and listing it would double-count 30% of a mainboard issue and
     push accounted_pct past 100. Reported separately as anchor_pct. */
  const parts = [
    ['qib', num(i.shares_qib)],
    ['nii', num(i.shares_nii)],
    ['retail', num(i.shares_retail)],
    ['employee', num(i.shares_employee)],
    ['shareholders', num(i.shares_shareholders)],
  ].filter(([, v]) => v > 0);

  if (total <= 0 || !parts.length) {
    return { has_data: false, rows: [], accounted_pct: 0, retail_pct: 0,
             institutional_pct: 0, anchor_pct: 0, has_employee: false, tilt: '' };
  }

  const find = (key) => (parts.find(([k]) => k === key) || [, 0])[1];
  const rows = parts
    .map(([key, shares]) => ({ key, shares, pct: round(pct(shares, total), 1) }))
    // Biggest slice first: the story is "who gets most of this", and a fixed
    // QIB/NII/retail order buries that when an issue is unusual.
    .sort((a, b) => b.shares - a.shares);

  const retailPct = round(pct(find('retail'), total), 1);
  const sum = parts.reduce((acc, [, v]) => acc + v, 0);

  return {
    has_data: true,
    rows,
    /* Makes a gap visible instead of silent. The named slices normally sum to
       the total; when they do not, something is carved out that this record
       does not name, and the scene says so rather than drawing bars that
       quietly fail to fill the row. */
    accounted_pct: round(pct(sum, total), 1),
    retail_pct: retailPct,
    institutional_pct: round(pct(find('qib'), total), 1),
    /* Share of the WHOLE issue placed with anchors before bidding opened.
       Inside institutional_pct, not additional to it. */
    anchor_pct: round(pct(num(i.shares_anchor), total), 1),
    has_employee: find('employee') > 0,
    /* What the split means to the viewer — arithmetic on these numbers, not a
       claim about which regulation produced them. Someone deciding whether to
       apply cares that a 10% retail slice is thin, not which clause set it. */
    tilt: retailPct >= 50 ? 'retail_led'
        : retailPct >= 30 ? 'balanced'
        : retailPct > 0 ? 'institution_led' : '',
  };
}

function gmpMetrics(ipo, now = new Date()) {
  const hist = (ipo.gmp_history || []).slice().sort((a, b) =>
    String(a.date || '').localeCompare(String(b.date || '')));
  const band = num(ipo.issue?.price_high);
  const latest = hist[hist.length - 1] || null;
  const prev = hist.length > 1 ? hist[hist.length - 2] : null;
  const gmp = latest ? num(latest.gmp) : 0;
  const prevGmp = prev ? num(prev.gmp) : 0;

  let movement = 'stable';
  if (prev) {
    if (gmp > prevGmp * 1.05 && gmp > prevGmp) movement = 'surge';
    else if (gmp < prevGmp * 0.95) movement = 'drop';
  }

  // `per_lot` is what one application was worth on that day: the premium times
  // the lot size. It is the figure a viewer actually acts on — nobody buys one
  // share of an IPO, the lot is the unit — and reading it per day turns the
  // trail from a price chart into "what this trade was worth, daily". Zero when
  // the lot size is not published yet, which the scene checks before adding the
  // column rather than printing a row of ₹0.
  const lot = num(ipo.issue?.lot_size);
  const series = hist.map((p) => ({
    date: p.date, gmp: num(p.gmp), pct: round(pct(num(p.gmp), band), 2),
    est: round(band + num(p.gmp), 2),
    per_lot: Math.round(num(p.gmp) * lot),
    kostak: num(p.kostak), sauda: num(p.sauda), source: p.source || 'manual',
  }));
  const values = series.map((p) => p.gmp);

  // Age of the newest reading. Mirror of compute.py — the reels label this
  // number "Today's GMP", so they need to know when it is not today's.
  const todayIso = isoDate(now);
  const age = latest && latest.date
    ? Math.round((Date.parse(todayIso) - Date.parse(latest.date)) / 864e5)
    : null;

  return {
    // Is there a quote at all? Every field below defaults to 0 when the trail
    // is empty, and 0 is a real premium — an issue trading at par — so
    // without this flag "nobody has quoted it" and "quoted at exactly par"
    // render identically as ₹0, and reel 2 reads a premium of zero aloud as
    // though a desk had said so.
    //
    // Not hypothetical: Fascinate Textiles and Pramodini Medicare each carried
    // a fortnight of model-written ₹0 rows for exactly this reason. Those were
    // removed from the sheet, and InvestorGain — which omits an unquoted day
    // rather than zeroing it — leaves both with no trail at all. That absence
    // is the honest answer, and this is what lets the card say so.
    has_data: series.length > 0,
    gmp, prev: prevGmp, delta: round(gmp - prevGmp, 2),
    pct: round(pct(gmp, band), 2),
    est_listing: round(band + gmp, 2),
    gain_per_lot: Math.round(gmp * num(ipo.issue?.lot_size)),
    movement,
    kostak: latest ? num(latest.kostak) : 0,
    sauda: latest ? num(latest.sauda) : 0,
    updated: latest ? latest.date : null,
    series,
    peak: values.length ? Math.max(...values) : 0,
    trough: values.length ? Math.min(...values) : 0,
    days_tracked: series.length,
    age_days: age,
    is_stale: age !== null && age > 0,
  };
}

function subscriptionMetrics(ipo) {
  const days = (ipo.subscription || []).slice().sort((a, b) => num(a.day) - num(b.day));
  const last = days[days.length - 1];
  if (!last) return { has_data: false, days: [] };

  const total = num(last.total);
  let sentiment = 'weak';
  if (total >= 10) sentiment = 'heavy';
  else if (total >= 3) sentiment = 'good';
  else if (total >= 1) sentiment = 'ok';

  const cats = [['qib', num(last.qib)], ['nii', num(last.nii)], ['retail', num(last.retail)]];
  return {
    has_data: true,
    day: num(last.day),
    qib: num(last.qib), nii: num(last.nii), retail: num(last.retail),
    employee: num(last.employee), total,
    // The NII book split at SEBI's ₹10 lakh line. Passed through rather than
    // folded into `nii`: each tranche allots its minimum bid by its own draw,
    // so each has its own odds. Mirrors compute.py.
    nii_small: num(last.nii_small), nii_big: num(last.nii_big),
    max_category: Math.max(num(last.qib), num(last.nii), num(last.retail), num(last.employee), 1),
    sentiment,
    leader: cats.reduce((a, b) => (b[1] > a[1] ? b : a))[0],
    days: days.map((s) => ({
      day: num(s.day), date: s.date, qib: num(s.qib), nii: num(s.nii),
      retail: num(s.retail), employee: num(s.employee), total: num(s.total),
      nii_small: num(s.nii_small), nii_big: num(s.nii_big),
    })),
  };
}

function financialMetrics(ipo) {
  const f = ipo.financials || {};
  const years = f.years || [];
  // Mirrors compute.py: `years` alone is not data. The scaffold pre-fills
  // FY23/FY24/FY25, so an unfilled IPO would render a scene of confident
  // zeros. Require at least one real figure.
  const anyFigure = [f.revenue, f.ebitda, f.pat, f.net_worth]
    .some((a) => (a || []).some((v) => num(v) !== 0));
  // `present` belongs here too, all false. The reels read
  // `financials.present.revenue` before checking `has_data`, so leaving the
  // key out threw for every IPO with no financials typed in. Mirror of
  // compute.py's early return.
  if (!years.length || !anyFigure) {
    return { has_data: false, rows: [],
             present: { revenue: false, ebitda: false, pat: false,
                        net_worth: false, total_debt: false } };
  }

  const at = (arr, i) => num((arr || [])[i]);
  const rows = years.map((yr, i) => {
    const rev = at(f.revenue, i), ebitda = at(f.ebitda, i), pat = at(f.pat, i);
    const nw = at(f.net_worth, i), debt = at(f.total_debt, i);
    return {
      year: yr, revenue: rev, ebitda,
      ebitda_margin: round(pct(ebitda, rev), 1),
      pat, pat_margin: round(pct(pat, rev), 1),
      net_worth: nw, total_debt: debt,
      ronw: round(pct(pat, nw), 1),
      debt_equity: nw ? round(debt / nw, 2) : 0,
    };
  });

  const span = years.length - 1;
  const first = rows[0], last = rows[rows.length - 1];
  const band = num(ipo.issue?.price_high);
  const eps = num(f.eps);
  // Only a POSITIVE EPS has a price/earnings multiple. A loss-making issue
  // divided out to a confident negative "multiple" that looks like a
  // valuation and sorts like a cheap one. Mirrors compute.py.
  const pe = eps > 0 ? round(band / eps, 1) : 0;
  const peer = num(f.pe_peer_avg);
  const shares = num(ipo.issue?.shares_post_issue_cr);

  const revCagr = round(cagr(first.revenue, last.revenue, span), 1);
  const patCagr = round(cagr(first.pat, last.pat, span), 1);
  const ov = ipo.benchmarks || null;

  // Mirror of compute.py: a metric is judged only when its series exists.
  // An absent array is not a column of zeros, and a zero is not a "poor".
  const hasRev = !!(f.revenue || []).length, hasPat = !!(f.pat || []).length;
  const hasEbitda = !!(f.ebitda || []).length, hasNw = !!(f.net_worth || []).length;
  const hasDebt = !!(f.total_debt || []).length;
  const mark = (metric, value, available, goodAt = null) => {
    if (available) return judge(metric, value, goodAt, ov);
    const spec = BENCHMARKS[metric] || {};
    return { value: 0, good_at: spec.good_at ?? null,
             higher_is_better: spec.higher !== false, verdict: 'na',
             unit: spec.unit || '', pos: 0, mark: 0, gap_pct: 0 };
  };

  const marks = {
    ebitda_margin: mark('ebitda_margin', last.ebitda_margin, hasEbitda && hasRev),
    pat_margin:    mark('pat_margin', last.pat_margin, hasPat && hasRev),
    revenue_cagr:  mark('revenue_cagr', revCagr, hasRev && span > 0),
    pat_cagr:      mark('pat_cagr', patCagr, hasPat && span > 0),
    ronw:          mark('ronw', last.ronw, hasPat && hasNw),
    debt_equity:   mark('debt_equity', last.debt_equity, hasDebt && hasNw),
    pe:            mark('pe', pe, !!(eps && peer), peer || null),
  };

  return {
    has_data: true, rows, latest: last,
    revenue_cagr: revCagr,
    ebitda_cagr: round(cagr(first.ebitda, last.ebitda, span), 1),
    pat_cagr: patCagr,
    margin_shift_bps: Math.round((last.ebitda_margin - first.ebitda_margin) * 100),
    eps, pe, pe_peer_avg: peer,
    pe_premium_pct: peer ? round(pct(pe - peer, peer), 1) : 0,
    market_cap_cr: band && shares ? Math.round(band * shares) : 0,
    marks,
    score_good: Object.values(marks).filter((m) => m.verdict === 'good').length,
    score_total: Object.values(marks).filter((m) => m.verdict !== 'na').length,
    // Mirror of compute.py: lets the table drop a column rather than print a
    // row of zeros under a heading for data nobody has.
    present: { revenue: hasRev, ebitda: hasEbitda, pat: hasPat,
               net_worth: hasNw, total_debt: hasDebt },
  };
}

/* The calendar, plus where the clock currently sits in it.
 *
 * `status` used to compare dates against dates, which made an issue "open" for
 * the seven hours between its 17:00 cut-off and midnight — the countdown read
 * 00:00 while the card still said apply. It now compares MOMENTS, so the two
 * agree by construction. The ladder itself lives in readiness.js, because the
 * reel-validity windows are built from the same instants and two copies of
 * "when does bidding actually stop" is one too many. Mirrors compute.py. */
function dateMetrics(ipo, now = new Date()) {
  const d = ipo.dates || {};
  const shut = rCloseAt(ipo), opens = rOpenAt(ipo);
  const stamp = (dt) => (dt ? new Date(dt.getTime() - dt.getTimezoneOffset() * 6e4)
    .toISOString().slice(0, 19) : null);
  return {
    ...d,
    open_at: stamp(opens),
    close_at: stamp(shut),
    status: rStatus(ipo, now),
  };
}

function listingMetrics(ipo) {
  const band = num(ipo.issue?.price_high);
  const a = ipo.allotment || {};
  return {
    status: a.status || 'expected',
    low: num(a.listing_low), high: num(a.listing_high),
    low_pct: band ? round(pct(num(a.listing_low) - band, band), 1) : 0,
    high_pct: band ? round(pct(num(a.listing_high) - band, band), 1) : 0,
  };
}

function deriveInitials(ipo) {
  if (ipo.initials) return String(ipo.initials).toUpperCase().slice(0, 3);
  const skip = new Set(['ltd', 'limited', 'pvt', 'private', 'and', 'the', 'india', '&']);
  const words = String(ipo.company || '').split(/\s+/)
    .filter((w) => w && !skip.has(w.toLowerCase().replace(/[.,]/g, '')));
  return (words.slice(0, 2).map((w) => w[0]).join('') || 'IP').toUpperCase();
}

// ── score ──────────────────────────────────────────────────────────────────
// Mirror of compute.py's score_metrics — keep the weights and the bands in
// step with it, or the studio will preview a number the published JSON does
// not agree with.
const SCORE_WEIGHTS = { grey: 25, demand: 20, fundamentals: 30, valuation: 15, structure: 10 };
const HONEST_FLOOR = 40;
const GREY_BAND   = [[-10, 0], [0, 2], [5, 4], [10, 5.5], [20, 7.5], [30, 9], [50, 10]];
const DEMAND_BAND = [[0, 0], [0.5, 2], [1, 4], [3, 6], [10, 7.5], [30, 9], [50, 10]];
const VALUE_BAND  = [[-50, 10], [-30, 9], [0, 6], [30, 3.5], [100, 1], [200, 0]];

function curve(x, pts) {
  if (x <= pts[0][0]) return pts[0][1];
  for (let i = 0; i < pts.length - 1; i++) {
    const [x0, y0] = pts[i], [x1, y1] = pts[i + 1];
    if (x <= x1) { const span = x1 - x0; return y0 + (y1 - y0) * (span ? (x - x0) / span : 0); }
  }
  return pts[pts.length - 1][1];
}

function scoreMetrics(ipo, d) {
  const { gmp, subscription: sub, financials: fin, issue: iss } = d;
  const parts = [];
  // `share` = how much of this component's evidence exists. See compute.py.
  const add = (key, has, mark, detail, share = 1) => parts.push({
    key, weight: round(SCORE_WEIGHTS[key] * (has ? share : 1), 1),
    full_weight: SCORE_WEIGHTS[key], has_data: !!has,
    mark: has ? round(Math.max(0, Math.min(10, mark)), 1) : null, detail,
  });

  const band = num(ipo.issue?.price_high);
  const hasGrey = !!(ipo.gmp_history || []).length && band > 0;
  add('grey', hasGrey, curve(gmp.pct, GREY_BAND),
      hasGrey ? `GMP is ${gmp.pct}% of the ₹${band} band` : 'no GMP logged yet');

  add('demand', sub.has_data, curve(sub.total || 0, DEMAND_BAND),
      sub.has_data ? `${sub.total}x overall on day ${sub.day}`
                   : 'issue has not opened / no subscription read');

  const hasFun = fin.has_data && fin.score_total > 0;
  const slots = Object.keys(fin.marks || {}).length || Object.keys(BENCHMARKS).length;
  const measured = fin.score_total || 0;
  add('fundamentals', hasFun, 10 * (fin.score_good || 0) / (measured || 1),
      hasFun ? `${fin.score_good} of ${measured} benchmarks met`
               + (measured < slots ? ` (${measured} of ${slots} measurable)` : '')
             : 'no financials entered',
      slots ? measured / slots : 1);

  const hasVal = fin.has_data && fin.pe > 0 && fin.pe_peer_avg > 0;
  add('valuation', hasVal, curve(fin.pe_premium_pct || 0, VALUE_BAND),
      hasVal ? `P/E ${fin.pe} vs peers ${fin.pe_peer_avg} (${fin.pe_premium_pct > 0 ? '+' : ''}${fin.pe_premium_pct}%)`
             : 'no EPS / peer P/E');

  add('structure', iss.has_split, 2 + (iss.fresh_pct || 0) / 100 * 8,
      iss.has_split ? `${iss.fresh_pct}% fresh issue` : 'fresh/OFS split not disclosed');

  const covered = parts.filter((p) => p.has_data).reduce((a, p) => a + p.weight, 0);
  const totalW = Object.values(SCORE_WEIGHTS).reduce((a, b) => a + b, 0);
  const earned = parts.filter((p) => p.has_data).reduce((a, p) => a + p.weight * p.mark, 0);
  const value = covered ? round(earned / covered, 1) : 0;
  const coveredPct = Math.round(covered / totalW * 100);
  const manual = num(ipo.analysis?.score);

  return {
    value, components: parts, covered_pct: coveredPct,
    has_data: coveredPct >= HONEST_FLOOR,
    missing: parts.filter((p) => !p.has_data).map((p) => p.key),
    manual, source: manual > 0 ? 'manual' : 'auto',
    effective: manual > 0 ? manual : value,
  };
}

/** The full derived block — same keys the backend publishes. */
function derive(ipo, now = new Date()) {
  const out = {
    initials: deriveInitials(ipo),
    issue: issueMetrics(ipo),
    gmp: gmpMetrics(ipo, now),
    subscription: subscriptionMetrics(ipo),
    financials: financialMetrics(ipo),
    dates: dateMetrics(ipo, now),
    listing: listingMetrics(ipo),
  };
  out.score = scoreMetrics(ipo, out);
  return out;
}
