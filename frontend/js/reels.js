/* Reel structure.
 *
 * Each of the six topics is its own video. A reel is an ordered list of
 * scenes; a scene fills the 9:16 frame with exactly one idea and holds for
 * `hold` seconds before the next slides up. Recording one reel start-to-finish
 * gives you a finished Short with no cutting.
 *
 * `hold` values are a starting point tuned to roughly 15-30s per reel — the
 * sweet spot for Shorts retention. Override them with the speed slider.
 */

const REELS = [
  {
    n: 1, key: 'reel1', acc: '#60A5FA',
    scenes: [
      { id: 'hook',    hold: 3 },   // the size + the name
      { id: 'company', hold: 5 },   // what it actually does
      // Its own scene rather than more blocks on `company`: that one already
      // carries four bullets and the profile strip and fits at full text scale
      // with nothing to spare, so folding this in would have shrunk the type on
      // both. Skipped automatically when `analysis.background` is empty — see
      // `scenesFor` — so an obscure SME with nothing to say plays a 5-scene
      // reel rather than holding on a blank frame for 5 seconds.
      { id: 'background', hold: 5 },   // who they are, in context
      { id: 'split',   hold: 5 },   // fresh vs OFS — the money question
      // Who the issue is actually FOR. Sits next to `split` because both
      // answer "how is this structured", and before `terms` because knowing
      // retail gets a tenth of the book changes how you read the minimum
      // investment that follows.
      //
      // Skipped when shares_total is missing — see `scenesFor`. That is the
      // same treatment `background` gets: a reservation scene with no
      // denominator can only draw bars that mean nothing, and a 5-second hold
      // on three empty tracks is worse than a 6-scene reel.
      { id: 'reservation', hold: 5 },
      /* The anchor book, split OUT of `reservation` rather than added to it.
       *
       * voReservation() used to concatenate four separate ideas — the split,
       * what the tilt means, the anchor carve-out, and the employee quota —
       * into one scene joined with a space. Measured 28 Aug 2026 that ran
       * 65.3 seconds of narration against a single motionless card, on a reel
       * whose other scenes averaged 20. No amount of background motion rescues
       * a minute on one frame; the fix is a second frame.
       *
       * It is also a genuinely different claim. The reservation scene answers
       * "how is the book divided"; this one answers "how much of it was
       * already spoken for before bidding opened", which is the part that
       * changes how the subscription numbers should be read.
       *
       * Dropped when there is no anchor placement — see `scenesFor`. */
      { id: 'anchor',  hold: 5 },
      { id: 'terms',   hold: 4 },   // price band, lot, minimum
      { id: 'dates',   hold: 4 },   // when it opens/closes/lists
    ],
  },
  {
    n: 2, key: 'reel2', acc: '#22C55E',
    scenes: [
      { id: 'hook',    hold: 3 },
      { id: 'gauge',   hold: 5 },   // today's GMP + movement
      { id: 'listing', hold: 4 },   // estimated listing + gain per lot
      { id: 'trail',   hold: 7 },   // announcement -> listing daily table
    ],
    // Mode B: every live IPO at once, for a single round-up video.
    boardScenes: [
      { id: 'boardhook', hold: 3 },
      { id: 'board',     hold: 9 },
    ],
  },
  {
    n: 3, key: 'reel3', acc: '#22D3EE',
    scenes: [
      { id: 'hook',  hold: 3 },
      { id: 'bars',  hold: 6 },     // QIB / NII / Retail
      { id: 'trend', hold: 5 },     // day-wise build-up
    ],
    // Mode B, same idea as reel 2's: every issue currently taking bids, in
    // one round-up. Subscription is the question with the shortest shelf
    // life on the whole board — it moves all day and stops mattering the
    // moment an issue closes — so a daily "where is everything at" cut is
    // worth more here than almost anywhere else.
    boardScenes: [
      { id: 'subboardhook', hold: 3 },
      { id: 'subboard',     hold: 9 },
    ],
  },
  {
    n: 4, key: 'reel4', acc: '#F59E0B',
    scenes: [
      { id: 'hook',       hold: 3 },
      { id: 'financials', hold: 7 },  // revenue / EBITDA / PAT + margins
      { id: 'valuation',  hold: 5 },  // P/E vs peers, RoNW, D/E
      { id: 'flags',      hold: 6 },  // green vs red
      // The reel is called "Apply or Skip" and had four scenes of company
      // analysis without ever saying what applying costs or returns. This is
      // the one a viewer actually acts on: the cheque, the upside at today's
      // premium, and the odds of getting any of it.
      { id: 'stake',      hold: 6 },
      // Split from `stake`, which narrated 43 seconds over one card. What a
      // lot costs and whether you will get one are two questions.
      { id: 'odds',       hold: 6 },
    ],
  },
  {
    n: 5, key: 'reel5', acc: '#A78BFA',
    scenes: [
      { id: 'score',   hold: 4 },
      { id: 'verdict', hold: 5 },
      { id: 'who',     hold: 5 },   // retail / HNI / long term
      // Split from `who`, which narrated 172 words - 77 seconds, 60% of the
      // reel - on a single static frame.
      { id: 'cutoff',  hold: 5 },   // the closing countdown and the warning
    ],
  },
  {
    n: 6, key: 'reel6', acc: '#34D399',
    scenes: [
      { id: 'status',    hold: 4 },
      { id: 'checklist', hold: 7 },
      { id: 'listing',   hold: 5 },
    ],
  },
  /* Reel 7 · the pre-market briefing.
   *
   * The only reel that is not about an IPO, which is why it carries `market:
   * true`: the card's scene block for reels 1-6 sits inside `x-if="ipo && d"`
   * and nothing in it can render without a selected company. This one reads
   * `briefing` instead — the newest day in the sheet's four Market tabs, put
   * there by `ipopulse market --write`.
   *
   * Holds are longer than the IPO reels' because the content is denser: five
   * headlines and ten setups are a lot to read off a phone, and the narration
   * for those two scenes runs 15-20 seconds each on its own. `Script` timing
   * overrides all of this from the real narration anyway — see scriptHolds.
   */
  {
    n: 7, key: 'reel7', acc: '#F472B6', market: true,
    scenes: [
      { id: 'mkthook', hold: 3 },   // the date, the bias, the one-line read
      { id: 'indices', hold: 5 },   // NIFTY, BANK NIFTY, breadth
      { id: 'news',    hold: 9 },   // the five overnight stories
      { id: 'sectors', hold: 6 },   // strongest and weakest sectoral indices
      // One side per scene. Together they were 55s of narration over one
      // motionless card — the same failure reel 1's `reservation` scene had,
      // and the same fix: a second frame. Either drops out when that side has
      // no setups, which happens on a day with no clean structure.
      { id: 'longs',   hold: 7 },
      { id: 'shorts',  hold: 7 }
    ],
  },

  /* Reel 8 — the scorecard. What reel 7 said this morning, and what the
   * market then did.
   *
   * `score: true` is to this reel what `market: true` is to reel 7: it
   * selects a different data source (the Scorecard tabs, keyed by DATE) and a
   * different card wrapper. The three wrappers in index.html are mutually
   * exclusive on these flags for a reason - see `one card wrapper at a time`.
   *
   * The scene order is the user's spec, in their words: reel 7 says a stock
   * may move this much, then at the end of the day we say "this is what we
   * expected it happened", or "this is what we expected but that did not
   * happend due to this", and close on prediction success over total.
   *
   * `right` and `wrong` are separate scenes and either can drop out. A day
   * where everything worked has no `wrong` card and vice versa, and an empty
   * frame reads as a mistake. Same reasoning as reel 7's longs/shorts split.
   *
   * `record` is last and is the only scene allowed to state a rate. Below
   * five stored sessions the backend reports `enough: false` and the card
   * says "too early to say" instead of a percentage - a trust channel cannot
   * quote a hit rate computed from three trades.
   */
  {
    n: 8, key: 'reel8', acc: '#FBBF24', score: true,
    scenes: [
      { id: 'scorehook', hold: 3 },  // the date and the headline number
      { id: 'called',    hold: 6 },  // what we said this morning
      { id: 'right',     hold: 8 },  // the ones that happened
      { id: 'wrong',     hold: 8 },  // the ones that did not, and why
      { id: 'record',    hold: 6 },  // hits over total, across sessions
    ],
  },
];

/**
 * Scenes for a reel, honouring the GMP board toggle and dropping any scene
 * that has no data behind it.
 *
 * `background` is the only optional scene so far, and it has to be optional:
 * ai.research_background returns an empty list for a company it does not
 * genuinely know, which is the correct answer for most SME issues. Every other
 * scene degrades into something readable when a field is blank; this one would
 * be an empty frame the reel still holds on for five seconds, and the progress
 * bar would promise six scenes and show five.
 *
 * `ipo` is optional so callers that only want the shape — the scene-count
 * label, the nav strip — can omit it and get the full list.
 */
/* The sign-off card, appended to every reel by `scenesFor`.
 *
 * Appended in one place rather than typed into all seven REELS entries: a
 * reel added later gets its CTA for free, and there is no way to add a reel
 * and forget it. 2.4 seconds is deliberate - long enough to read a wordmark
 * and register the ask, short enough that it does not drag average view
 * duration down. `scriptHolds` overrides it from the narration anyway once a
 * voice exists, and the spoken sign-off is about that long.
 */
const OUTRO_SCENE = { id: 'outro', hold: 2.4 };

function scenesFor(reel, gmpMode, ipo, briefing) {
  return [..._sceneBody(reel, gmpMode, ipo, briefing), OUTRO_SCENE];
}

function _sceneBody(reel, gmpMode, ipo, briefing) {
  /* Reel 7 reads the briefing, never the IPO. Scenes with nothing behind them
     drop out for the same reason reel 1's `background` does: a five-second
     hold on an empty frame is worse than a shorter reel. */
  if (reel.market) {
    if (!briefing) return reel.scenes;          // shape only, for the label
    const drop = new Set();
    if (!(briefing.news || []).length) drop.add('news');
    if (!(briefing.sectors || []).length) drop.add('sectors');
    if (!(briefing.longs || []).length) drop.add('longs');
    if (!(briefing.shorts || []).length) drop.add('shorts');
    return drop.size ? reel.scenes.filter((sc) => !drop.has(sc.id)) : reel.scenes;
  }
  return _ipoScenesFor(reel, gmpMode, ipo);
}

function _ipoScenesFor(reel, gmpMode, ipo) {
  // Any reel that declares boardScenes gets the all-IPOs cut, not just reel
  // 2. This was `reel.n === 2` while reel 2 was the only one with a board,
  // so adding one to reel 3 changed nothing until the check asked about the
  // property instead of the number.
  if (gmpMode === 'board' && reel.boardScenes) return reel.boardScenes;
  if (reel.n === 1 && ipo) {
    const drop = new Set();
    const bg = (ipo.analysis && ipo.analysis.background) || [];
    if (!bg.length) drop.add('background');
    /* Reservation needs its denominator. Checked on the raw field rather than
       through issueMetrics because this runs for the scene-count label too,
       where no derived record has been built yet. Same field the compute
       mirrors gate on, so the scene cannot appear with nothing in it. */
    const iss = ipo.issue || {};
    const hasSlices = (Number(iss.shares_qib) || 0) + (Number(iss.shares_nii) || 0)
                    + (Number(iss.shares_retail) || 0) + (Number(iss.shares_employee) || 0);
    if (!(Number(iss.shares_total) > 0 && hasSlices > 0)) drop.add('reservation');
    /* No anchor placement, no anchor scene. Same reasoning as `reservation`:
       a card reading "0% went to anchor investors" is not a neutral blank, it
       is a false claim stated with total confidence. Anchor shares are also
       absent for most SME issues, so this drops often and must drop cleanly. */
    if (!(Number(iss.shares_anchor) > 0)) drop.add('anchor');
    if (drop.size) return reel.scenes.filter((s) => !drop.has(s.id));
  }
  return reel.scenes;
}

/** Output frames. Every scene is single-column, so all four just work. */
// `fs` is the base type size; auto-shrink pulls it down on the denser scenes,
// so these are set generously enough that a sparse scene actually fills the
// frame rather than floating in the middle of it.
const PRESETS = {
  reel:   { k: 'reel',   w: 450, h: 800, exp: 2.4, fs: 17,   short: '9:16', label: '9:16 Reel / Short',  out: '1080×1920' },
  post:   { k: 'post',   w: 432, h: 540, exp: 2.5, fs: 16,   short: '4:5',  label: '4:5 Feed Post',      out: '1080×1350' },
  square: { k: 'square', w: 540, h: 540, exp: 2.0, fs: 16.5, short: '1:1',  label: '1:1 Square',         out: '1080×1080' },
  video:  { k: 'video',  w: 960, h: 540, exp: 2.0, fs: 19,   short: '16:9', label: '16:9 YouTube Video', out: '1920×1080' },
};

/* Card themes.
 *
 * Four looks for the same data, so consecutive uploads do not all look like
 * the same video. Purely cosmetic — no theme changes a number, a label or a
 * layout, which is what makes switching safe mid-series.
 *
 * Each theme carries its own six accents, one per reel, rather than a single
 * colour. A flat per-theme accent would have made every reel in a theme
 * identical, throwing away the "which topic is this" signal the accents
 * already carry; keeping six preserves it inside every theme.
 *
 * Two hard constraints, both from the export path (see css/studio.css):
 *
 *  1. Plain hex and rgba() only. No color-mix(), no oklch() — Chrome resolves
 *     those to color(srgb ...) and html2canvas throws on it, killing the whole
 *     PNG. Every value below is a literal a 2015 parser would accept.
 *  2. `bg` must be the flat colour nearest the top of `card`. html2canvas is
 *     handed it as `backgroundColor`, and it is what shows through the rounded
 *     corners — a mismatch draws a dark ring around a light card.
 */
const THEMES = [
  {
    key: 'midnight', name: 'Midnight', swatch: '#0F172A',
    card: 'linear-gradient(180deg, #101B33 0%, #0F172A 42%, #0B1120 100%)',
    bg: '#0B1120',
    // The original palette. Kept first and unchanged so every reel recorded
    // before themes existed still matches what this produces today.
    hues: ['#60A5FA', '#22C55E', '#22D3EE', '#F59E0B', '#A78BFA', '#34D399'],
  },
  {
    key: 'carbon', name: 'Carbon', swatch: '#17191D',
    card: 'linear-gradient(180deg, #23262C 0%, #191B1F 45%, #0E0F12 100%)',
    bg: '#0E0F12',
    // Neutral grey ground, so the accents read louder than they do on navy.
    hues: ['#7DD3FC', '#4ADE80', '#67E8F9', '#FBBF24', '#C4B5FD', '#6EE7B7'],
  },
  {
    key: 'royal', name: 'Royal', swatch: '#1E1B4B',
    card: 'linear-gradient(180deg, #2A2560 0%, #1E1B4B 45%, #120F2E 100%)',
    bg: '#120F2E',
    // Indigo ground pushes the accents warm — amber and rose separate from it
    // far better than blue-greens, which sink into the background.
    hues: ['#93C5FD', '#86EFAC', '#5EEAD4', '#FCD34D', '#D8B4FE', '#A7F3D0'],
  },
  {
    key: 'ember', name: 'Ember', swatch: '#2A1512',
    card: 'linear-gradient(180deg, #3A1D18 0%, #2A1512 45%, #170A08 100%)',
    bg: '#170A08',
    // Warm ground. Greens are lifted towards lime here: a mid green on brown
    // is the one combination in this set that loses contrast badly.
    hues: ['#7DD3FC', '#A3E635', '#5EEAD4', '#FDBA74', '#F0ABFC', '#BEF264'],
  },
];

const THEME_BY_KEY = Object.fromEntries(THEMES.map((t) => [t.key, t]));

/* Where the per-reel rotation starts. Reel 1 keeps Midnight — the look
 * every reel recorded before themes existed already has — and the rest
 * step through from there. Change this to re-skin the whole set at once. */
const THEME_ROTATION_OFFSET = 0;

/* Categorical series colours — QIB / NII / Retail on reel 3.
 *
 * These are IDENTITY colours, not the theme accent, so they stay fixed across
 * themes: a viewer who learns "pink is NII" on one video must not find it means
 * something else on the next. Assigned in fixed order and never cycled.
 *
 * Blue / pink / cyan, and every part of that is a result rather than a taste:
 *
 *  - The previous set was #60A5FA blue, #A78BFA violet, #22C55E green. Measured,
 *    blue↔violet is ΔE 0.3 under deuteranopia and 10.2 under NORMAL vision —
 *    indistinguishable to roughly one man in twelve and hard for everyone else.
 *    The QIB and NII bars were effectively the same colour.
 *  - No green, amber or red here on purpose. Those are the status vocabulary in
 *    this app — green flags, red flags, positive vs negative GMP — and reusing
 *    them for "which investor category" makes green mean two things on one card.
 *  - Mid-tone rather than pastel: OKLCH L must sit in 0.48-0.67 against a dark
 *    card, and the old set was 0.71+, which is why it looked washed out on
 *    export as well as being hard to separate.
 *
 * Verified with the dataviz validator against all four theme surfaces: lightness
 * band, chroma floor, CVD separation (worst adjacent ΔE 10.3 deutan), normal
 * vision (31.1) and contrast all pass. Re-run it before changing any value:
 *   node validate_palette.js "#2563EB,#DB2777,#0891B2" --mode dark --surface "#0F172A"
 *
 * Each bar is also direct-labelled with its name and multiple, so identity never
 * rests on colour alone.
 */
const SERIES = {
  qib:    '#2563EB',
  nii:    '#DB2777',
  retail: '#0891B2',
};

const REGISTRARS = {
  'KFintech': 'https://kosmic.kfintech.com/ipostatus/',
  'MUFG Intime (Link Intime)': 'https://in.mpms.mufg.com/Initial_Offer/public-issues.html',
  'Bigshare Services': 'https://ipo.bigshareonline.com/ipo_status.html',
  'Maashitla Securities': 'https://maashitla.com/allotment-status/public-issues',
  'Skyline Financial': 'https://www.skylinerta.com/ipo.php',
  'Cameo Corporate': 'https://ipo.cameoindia.com/',
  'BSE (any registrar)': 'https://www.bseindia.com/investors/appli_check.aspx',
};
