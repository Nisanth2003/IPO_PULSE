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
      { id: 'split',   hold: 5 },   // fresh vs OFS — the money question
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
  },
  {
    n: 4, key: 'reel4', acc: '#F59E0B',
    scenes: [
      { id: 'hook',       hold: 3 },
      { id: 'financials', hold: 7 },  // revenue / EBITDA / PAT + margins
      { id: 'valuation',  hold: 5 },  // P/E vs peers, RoNW, D/E
      { id: 'flags',      hold: 6 },  // green vs red
    ],
  },
  {
    n: 5, key: 'reel5', acc: '#A78BFA',
    scenes: [
      { id: 'score',   hold: 4 },
      { id: 'verdict', hold: 5 },
      { id: 'who',     hold: 5 },   // retail / HNI / long term + countdown
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
];

/** Scenes for a reel, honouring the GMP board toggle. */
function scenesFor(reel, gmpMode) {
  if (reel.n === 2 && gmpMode === 'board' && reel.boardScenes) return reel.boardScenes;
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

const REGISTRARS = {
  'KFintech': 'https://kosmic.kfintech.com/ipostatus/',
  'MUFG Intime (Link Intime)': 'https://in.mpms.mufg.com/Initial_Offer/public-issues.html',
  'Bigshare Services': 'https://ipo.bigshareonline.com/ipo_status.html',
  'Maashitla Securities': 'https://maashitla.com/allotment-status/public-issues',
  'Skyline Financial': 'https://www.skylinerta.com/ipo.php',
  'Cameo Corporate': 'https://ipo.cameoindia.com/',
  'BSE (any registrar)': 'https://www.bseindia.com/investors/appli_check.aspx',
};
