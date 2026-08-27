/* Can this reel be recorded right now — and until when?
 *
 * Mirror of backend/ipopulse/readiness.py, the same way compute.js mirrors
 * compute.py. Change one and change the other: the studio's dots and the cron
 * job's report have to agree, or the light you record by and the light the
 * watchdog goes red on are two different lights.
 *
 * Two independent halves, deliberately kept apart:
 *
 *   windowFor()  a TIME judgement, from the issue calendar alone. A
 *                subscription reel cannot be shot before bidding opens and is
 *                worthless once allotment is out.
 *   dataState()  a CONTENT judgement. Are the fields those scenes read
 *                present, consistent, and — for the two numbers that move
 *                daily — recent enough to still be true.
 *
 * A reel is READY only when both say yes. That is what stops a green light on
 * an issue that closed five days ago: the data is complete, valid, and
 * publishing it would still be misinformation.
 */

/* Bidding opens at 10:00 and closes at dates.close_time (17:00 unless the
 * issue says otherwise). Basis of allotment lands in the evening, listing at
 * the market open. None of this is in any feed — it is how Indian public
 * issues run — so it is named here rather than buried as magic numbers. */
const R_OPEN_HOUR = 10;
const R_ALLOT_HOUR = 18;
const R_LIST_HOUR = 10;

const R_GMP_FRESH_DAYS = 1;
const R_GMP_STALE_DAYS = 3;
const R_SUB_FRESH_DAYS = 1;
/* A subscription figure read before the exchanges publish the day's close is
 * a running total — correct at the time and wrong by evening. */
const R_SUB_SETTLED_HOUR = 18;

const R_PREVIEW_DAYS = 1;
const R_ANNOUNCE_FALLBACK_DAYS = 7;

/* 'YYYY-MM-DD' + hour -> Date. Local time throughout, matching compute.js:
 * every stored date is an IST calendar day and the studio runs on the desk
 * that records the reels. */
function rAt(day, hour, minute = 0) {
  if (!day) return null;
  const [y, m, d] = String(day).slice(0, 10).split('-').map(Number);
  if (!y || !m || !d) return null;
  return new Date(y, m - 1, d, hour, minute, 0, 0);
}

const rDays = (n) => n * 864e5;
/* Whole calendar days between two Dates, ignoring the time of day. */
function rDayDiff(a, b) {
  const da = new Date(a.getFullYear(), a.getMonth(), a.getDate());
  const db = new Date(b.getFullYear(), b.getMonth(), b.getDate());
  return Math.round((da - db) / 864e5);
}

function rCloseAt(ipo) {
  const d = ipo.dates || {};
  if (!d.close) return null;
  const [hh, mm] = String(d.close_time || '17:00').split(':');
  const h = Number(hh), m = Number(mm || 0);
  return rAt(d.close, Number.isFinite(h) ? h : 17, Number.isFinite(m) ? m : 0);
}
const rOpenAt = (ipo) => rAt((ipo.dates || {}).open, R_OPEN_HOUR);
const rAllotAt = (ipo) => rAt((ipo.dates || {}).allotment, R_ALLOT_HOUR);
const rListAt = (ipo) => rAt((ipo.dates || {}).listing, R_LIST_HOUR);

/**
 * upcoming | open | closed | allotment | listed, judged on the CLOCK.
 *
 * The fix for the whole "it says open but the timer reads zero" class of bug.
 * `dates.close` is a day; bidding stops at 17:00 on that day. Comparing dates
 * against dates called an issue open for the seven hours after it shut —
 * exactly the window where a reel telling people to apply does harm.
 */
function rStatus(ipo, now = new Date()) {
  const lst = rListAt(ipo), alt = rAllotAt(ipo), shut = rCloseAt(ipo);
  if (lst && now >= lst) return 'listed';
  if (alt && now >= alt) return 'allotment';
  if (shut && now > shut) return 'closed';
  const opn = rOpenAt(ipo);
  if (opn && now >= opn) return 'open';
  return 'upcoming';
}

function rAnnouncedAt(ipo) {
  const d = ipo.dates || {};
  if (d.announced) return rAt(d.announced, R_OPEN_HOUR);
  const opn = rOpenAt(ipo);
  return opn ? new Date(opn.getTime() - rDays(R_ANNOUNCE_FALLBACK_DAYS)) : null;
}
function rPreviewAt(ipo) {
  const opn = rOpenAt(ipo);
  return opn ? new Date(opn.getTime() - rDays(R_PREVIEW_DAYS)) : null;
}

/* One entry per reel. Each carries the reason its window ends where it does,
 * because "why can I no longer record this" is the question the studio has to
 * answer at a glance. An unknown edge is an OPEN edge — treating "no listing
 * date yet" as "the window is shut" would grey out every reel on a freshly
 * discovered IPO, which is exactly when you want to record one. */
const R_WINDOWS = {
  1: { from: rAnnouncedAt, to: rCloseAt,
       starts: 'terms are public', ends: 'bidding closes' },
  2: { from: rAnnouncedAt, to: rListAt,
       starts: 'the grey market starts quoting',
       ends: 'it lists and the premium becomes a fact' },
  3: { from: rOpenAt, to: rAllotAt,
       starts: 'bidding opens',
       ends: 'allotment replaces demand as the story' },
  4: { from: rPreviewAt, to: rCloseAt,
       starts: 'the day before bidding opens', ends: 'bidding closes' },
  5: { from: rPreviewAt, to: rCloseAt,
       starts: 'the day before bidding opens', ends: 'bidding closes' },
  6: { from: rCloseAt,
       to: (i) => (rListAt(i) ? new Date(rListAt(i).getTime() + rDays(1)) : null),
       starts: 'bidding closes', ends: 'the day after listing' },
};

function windowFor(ipo, reel, now = new Date()) {
  const spec = R_WINDOWS[reel];
  if (!spec) return { state: 'live', from: null, to: null };
  const from = spec.from(ipo), to = spec.to(ipo);
  let state = 'live';
  if (from && now < from) state = 'early';
  else if (to && now > to) state = 'expired';
  return {
    state, from, to, starts: spec.starts, ends: spec.ends,
    hours_left: state === 'live' && to
      ? Math.round(((to - now) / 36e5) * 10) / 10 : null,
  };
}

/* What each reel reads. Every entry names a real scene in reels.js — adding a
 * scene there without adding its inputs here produces a reel that lights green
 * and records an empty frame, which is the failure this file exists to stop.
 * `need` means the scene is unrecordable without it; `want` means it degrades
 * but still plays. Mirrors readiness.NEEDS. */
const rNum = (v) => { const n = Number(v); return Number.isFinite(n) ? n : 0; };
const rFin = (i, k) => ((i.financials || {})[k] || []).some((v) => rNum(v) !== 0);
const rList = (i, k) => (((i.analysis || {})[k]) || []).length;

const R_NEEDS = {
  1: [
    ['Company name', 'need', (i) => !!String(i.company || '').trim()],
    ['Price band', 'need', (i) => rNum((i.issue || {}).price_high) > 0],
    ['Lot size', 'need', (i) => rNum((i.issue || {}).lot_size) > 0],
    ['Issue size', 'need', (i) => rNum((i.issue || {}).fresh_cr) + rNum((i.issue || {}).ofs_cr) > 0
                                  || rNum((i.issue || {}).total_cr) > 0],
    ['Open / close', 'need', (i) => !!((i.dates || {}).open && (i.dates || {}).close)],
    ['Overview bullets', 'need', (i) => rList(i, 'overview') >= 3],
    ['Fresh / OFS split', 'want', (i) => rNum((i.issue || {}).fresh_cr) + rNum((i.issue || {}).ofs_cr) > 0],
    ['Company facts', 'want', (i) => rList(i, 'about_facts') > 0],
    ['Sector', 'want', (i) => !!String(i.sector || '').trim()],
    ['Listing date', 'want', (i) => !!(i.dates || {}).listing],
  ],
  2: [
    ['A GMP reading', 'need', (i) => (i.gmp_history || []).length > 0],
    ['Price band', 'need', (i) => rNum((i.issue || {}).price_high) > 0],
    ['Lot size', 'need', (i) => rNum((i.issue || {}).lot_size) > 0],
    // One point is a dot, not a trail. `trail` is 7 of the reel's ~19 seconds.
    ['Several GMP days', 'want', (i) => (i.gmp_history || []).length >= 3],
  ],
  3: [
    ['Subscription', 'need', (i) => (i.subscription || []).length > 0],
    ['Category split', 'need', (i) => (i.subscription || []).some(
      (s) => rNum(s.qib) || rNum(s.nii) || rNum(s.retail))],
    ['Two or more days', 'want', (i) => (i.subscription || []).length >= 2],
  ],
  4: [
    ['Revenue', 'need', (i) => rFin(i, 'revenue')],
    ['PAT', 'need', (i) => rFin(i, 'pat')],
    ['Green / red flags', 'need', (i) => rList(i, 'green_flags') > 0 && rList(i, 'red_flags') > 0],
    ['Price band', 'need', (i) => rNum((i.issue || {}).price_high) > 0],
    ['Lot size', 'need', (i) => rNum((i.issue || {}).lot_size) > 0],
    ['EBITDA', 'want', (i) => rFin(i, 'ebitda')],
    ['Net worth', 'want', (i) => rFin(i, 'net_worth')],
    ['EPS', 'want', (i) => rNum((i.financials || {}).eps) > 0],
    ['Peer P/E', 'want', (i) => rNum((i.financials || {}).pe_peer_avg) > 0],
    ['A GMP reading', 'want', (i) => (i.gmp_history || []).length > 0],
  ],
  5: [
    ['Verdict', 'need', (i) => !!(i.analysis || {}).verdict],
    ['Retail / HNI / long calls', 'need', (i) => {
      const a = i.analysis || {};
      return !!(a.reco_retail && a.reco_hni && a.reco_long);
    }],
    // The only input that is derived rather than stored — see dataState.
    ['Enough data to score', 'need', null],
  ],
  6: [
    ['Registrar', 'need', (i) => !!String((i.issue || {}).registrar || '').trim()],
    ['Registrar link', 'need', (i) => !!String((i.issue || {}).registrar_url || '').trim()],
    ['Allotment date', 'need', (i) => !!(i.dates || {}).allotment],
    ['Listing date', 'need', (i) => !!(i.dates || {}).listing],
    ['Expected listing range', 'want',
      (i) => rNum((i.allotment || {}).listing_high) > 0 || (i.gmp_history || []).length > 0],
  ],
};

/**
 * Were the two moving numbers read recently enough to still be true?
 *
 * Only meaningful while they are still moving: a GMP last read the day an
 * issue listed is not stale, it is final. So every judgement is fenced by the
 * status ladder rather than by the calendar alone.
 */
function rFreshness(ipo, now = new Date()) {
  const state = rStatus(ipo, now);
  const out = {};

  const gmpMatters = ['upcoming', 'open', 'closed', 'allotment'].includes(state);
  const hist = ipo.gmp_history || [];
  const lastGmp = hist.length ? hist[hist.length - 1].date : null;
  const gAge = lastGmp ? rDayDiff(now, rAt(lastGmp, 0)) : null;
  out.gmp = {
    matters: gmpMatters, last: lastGmp, age_days: gAge,
    state: !gmpMatters ? 'n/a'
      : gAge === null ? 'missing'
      : gAge <= R_GMP_FRESH_DAYS ? 'fresh'
      : gAge <= R_GMP_STALE_DAYS ? 'stale' : 'cold',
  };

  // Subscription exists only during bidding, and the day's real number is not
  // published until the evening. A day-3 figure read at noon is a running
  // total — recording a "final demand" reel off it is how a 60x closes at 184x.
  const subMatters = state === 'open';
  const subs = ipo.subscription || [];
  const lastSub = subs.length ? subs[subs.length - 1].date : null;
  const sAge = lastSub ? rDayDiff(now, rAt(lastSub, 0)) : null;
  const settled = now.getHours() >= R_SUB_SETTLED_HOUR;
  out.subscription = {
    matters: subMatters, last: lastSub, age_days: sAge,
    state: !subMatters ? 'n/a'
      : sAge === null ? 'missing'
      : (sAge === 0 && !settled) ? 'provisional'
      : sAge <= R_SUB_FRESH_DAYS ? 'fresh' : 'stale',
  };
  return out;
}

function dataState(ipo, reel, derived, now = new Date()) {
  const missing = [], soft = [];
  for (const [label, level, test] of (R_NEEDS[reel] || [])) {
    const ok = test ? !!test(ipo) : !!(derived && derived.score && derived.score.has_data);
    if (!ok) (level === 'need' ? missing : soft).push(label);
  }
  const fresh = rFreshness(ipo, now);
  const watched = { 2: ['gmp'], 3: ['subscription'], 4: ['gmp'] }[reel] || [];
  const stale = watched.filter(
    (k) => fresh[k].matters && ['stale', 'cold', 'provisional'].includes(fresh[k].state));
  return { missing, soft, stale, freshness: fresh };
}

/**
 * The one call the studio makes.
 *
 * `state` is what the dot shows:
 *   ready    green — and blinking when the window shuts within a day
 *   partial  amber — recordable, but a scene will be thin or a number old
 *   blocked  red   — a required field is absent; recording produces a lie
 *   early / expired  grey — outside the window
 */
function reelState(ipo, reel, derived, now = new Date()) {
  const win = windowFor(ipo, reel, now);
  const dat = dataState(ipo, reel, derived, now);

  let state;
  if (win.state === 'early' || win.state === 'expired') state = win.state;
  else if (dat.missing.length) state = 'blocked';
  else if (dat.stale.length || dat.soft.length) state = 'partial';
  else state = 'ready';

  // Blinking is not decoration: it separates "you can record this" from "you
  // can record this today and not tomorrow", which is the only reason to look
  // at the board in the morning.
  const urgent = (state === 'ready' || state === 'partial')
    && win.hours_left !== null && win.hours_left <= 24;

  return { reel, state, urgent, window: win, ...dat };
}

/** Every reel for one IPO, plus the roll-up the dropdown shows. */
function readinessReport(ipo, derived, now = new Date()) {
  const reels = {};
  for (let n = 1; n <= 6; n++) reels[n] = reelState(ipo, n, derived, now);
  const ready = Object.keys(reels).map(Number).filter((n) => reels[n].state === 'ready');
  const live = Object.keys(reels).map(Number)
    .filter((n) => ['ready', 'partial'].includes(reels[n].state));
  return {
    reels, ready, live,
    ready_count: ready.length,
    // READY reels only, not `live`. A per-reel dot may blink on an amber tab —
    // "this one is thin AND expires today" is worth flagging on the tab
    // itself. But the roll-up drives the dot beside the company dropdown, and
    // there it has to mean one thing: you have a video you can shoot and it
    // expires today. An IPO with nothing recordable was blinking a red dot
    // purely because a blocked reel's window happened to close within a day,
    // which is an alarm about work you cannot do.
    urgent: ready.some((n) => reels[n].urgent),
  };
}

/* Palette for the dots. Shared by the reel tabs, the dropdown and the board so
 * one colour never means two things. Deliberately the same green/amber/rose
 * the apply-state signals already use (studio.js applyState). */
const READY_TONE = {
  ready:   { dot: '#22C55E', cls: 'text-emerald-400', label: 'Ready to record' },
  partial: { dot: '#F59E0B', cls: 'text-amber-300',  label: 'Thin or stale' },
  blocked: { dot: '#FB7185', cls: 'text-rose-400',   label: 'Missing data' },
  early:   { dot: '#475569', cls: 'text-slate-500',  label: 'Not yet' },
  expired: { dot: '#475569', cls: 'text-slate-500',  label: 'Window shut' },
};
