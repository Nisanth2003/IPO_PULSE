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
  const total = num(i.fresh_cr) + num(i.ofs_cr);
  return {
    total_cr: round(total, 2),
    fresh_pct: round(pct(num(i.fresh_cr), total), 1),
    ofs_pct: round(pct(num(i.ofs_cr), total), 1),
    min_investment: Math.round(num(i.lot_size) * num(i.price_high)),
    is_fresh_heavy: num(i.fresh_cr) >= num(i.ofs_cr),
  };
}

function gmpMetrics(ipo) {
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

  const series = hist.map((p) => ({
    date: p.date, gmp: num(p.gmp), pct: round(pct(num(p.gmp), band), 2),
    est: round(band + num(p.gmp), 2),
    kostak: num(p.kostak), sauda: num(p.sauda), source: p.source || 'manual',
  }));
  const values = series.map((p) => p.gmp);

  return {
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
    max_category: Math.max(num(last.qib), num(last.nii), num(last.retail), num(last.employee), 1),
    sentiment,
    leader: cats.reduce((a, b) => (b[1] > a[1] ? b : a))[0],
    days: days.map((s) => ({
      day: num(s.day), date: s.date, qib: num(s.qib), nii: num(s.nii),
      retail: num(s.retail), employee: num(s.employee), total: num(s.total),
    })),
  };
}

function financialMetrics(ipo) {
  const f = ipo.financials || {};
  const years = f.years || [];
  if (!years.length) return { has_data: false, rows: [] };

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
  const pe = eps ? round(band / eps, 1) : 0;
  const peer = num(f.pe_peer_avg);
  const shares = num(ipo.issue?.shares_post_issue_cr);

  const revCagr = round(cagr(first.revenue, last.revenue, span), 1);
  const patCagr = round(cagr(first.pat, last.pat, span), 1);
  const ov = ipo.benchmarks || null;

  const marks = {
    ebitda_margin: judge('ebitda_margin', last.ebitda_margin, null, ov),
    pat_margin:    judge('pat_margin', last.pat_margin, null, ov),
    revenue_cagr:  judge('revenue_cagr', revCagr, null, ov),
    pat_cagr:      judge('pat_cagr', patCagr, null, ov),
    ronw:          judge('ronw', last.ronw, null, ov),
    debt_equity:   judge('debt_equity', last.debt_equity, null, ov),
    pe:            judge('pe', pe, peer || null, ov),
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
  };
}

function dateMetrics(ipo, now = new Date()) {
  const d = ipo.dates || {};
  const out = { ...d, close_at: null, status: 'upcoming' };
  if (d.close) {
    const [hh, mm] = String(d.close_time || '17:00').split(':');
    out.close_at = `${d.close}T${String(hh || 17).padStart(2, '0')}:${String(mm || 0).padStart(2, '0')}:00`;
  }
  const today = now.toISOString().slice(0, 10);
  if (d.listing && today >= d.listing) out.status = 'listed';
  else if (d.allotment && today >= d.allotment) out.status = 'allotment';
  else if (d.close && today > d.close) out.status = 'closed';
  else if (d.open && today >= d.open) out.status = 'open';
  return out;
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

/** The full derived block — same keys the backend publishes. */
function derive(ipo, now = new Date()) {
  return {
    initials: deriveInitials(ipo),
    issue: issueMetrics(ipo),
    gmp: gmpMetrics(ipo),
    subscription: subscriptionMetrics(ipo),
    financials: financialMetrics(ipo),
    dates: dateMetrics(ipo, now),
    listing: listingMetrics(ipo),
  };
}
