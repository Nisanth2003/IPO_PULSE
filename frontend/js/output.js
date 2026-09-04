/* Outputs: voiceover scripts, captions, CSV report, PNG export.
 *
 * Mixed into the Alpine component (see `...OUTPUT` in studio.js) so these can
 * use `this` for state. Kept separate because none of it draws anything — it
 * all turns the current state into something you take away.
 */

const OUTPUT = {

  /* ── the English voice ────────────────────────────────────────────────
   *
   * Who is speaking, because a voice model can only perform what the text
   * already is:
   *
   *   Someone who has watched a lot of these, talking to a twenty-year-old
   *   with their first ₹15,000. Medium energy — interested, never shouting.
   *   States a number precisely, then says what it MEANS, because a number
   *   nobody can act on is decoration. Says "I" for judgement ("I wait for
   *   the closing number") and "you" for the decision, so the viewer is
   *   never told what to do. Warns without frightening. Explains the jargon
   *   the first time it appears, because half the audience is under
   *   twenty-five and nobody ever told them what NII means.
   *
   * It carries the CALM of experience and makes no claim to a biography.
   * That is deliberate and it is not a style note. Drafts of this said "in
   * twenty years I have almost never seen…", which is two separate problems
   * once a synthetic voice reads it onto a finance video: it asserts
   * credentials that belong to nobody, and YouTube's monetization rules bar
   * an AI persona that presents itself as a human expert giving financial
   * guidance — naming that exact case. The authority here therefore comes
   * from the market's own patterns ("early subscription numbers almost
   * never tell you where an issue finishes"), which is both true and
   * checkable, rather than from a tenure the narrator does not have.
   * See docs/YOUTUBE-PLAYBOOK.md.
   *
   * Why this is generated rather than one template with the numbers slotted
   * in: the old script said the same sentence about a 0.9x issue on day one
   * and a 150x issue on day three. A veteran's value is entirely in reading
   * those two differently — light demand on day one is *nothing*, light
   * demand on the final afternoon is the story. So every judgement line
   * below is chosen by the data, and the caution is loudest exactly where
   * the numbers are weakest.
   *
   * Hindi and Telugu are untouched. Emotion does not survive being machine
   * translated, and a veteran voice in a language written by a translator is
   * worse than the plain one it replaced — those two stay as they are until
   * a person who speaks them writes them.
   */

  // ── spoken numbers ───────────────────────────────────────────────────
  /* ElevenLabs and every other TTS reads "₹1,617.48" unreliably — sometimes
   * "rupees one six one seven point four eight", sometimes the glyph is
   * dropped and the figure becomes meaningless. Symbols never reach the
   * voice: they are spelled into words here first. Same for "%", and for the
   * "x" in "102.28x", which is read as the letter.
   */
  voRupees(n) {
    const v = Number(n) || 0;
    const abs = Math.abs(v);
    // Past a lakh, nobody says the digits — and a reader that does sounds
    // like a machine reading a spreadsheet.
    if (abs >= 1e7) return `${this.voNum(v / 1e7)} crore rupees`;
    if (abs >= 1e5) return `${this.voNum(v / 1e5)} lakh rupees`;
    // "a premium of 1 rupees" is the kind of thing a listener notices and a
    // writer never does, because on screen it was "₹1".
    if (abs === 1) return `${this.voNum(v)} rupee`;
    return `${this.voNum(v)} rupees`;
  },
  voCrore(n) { return `${this.voNum(n)} crore rupees`; },
  voPct(n) { return `${this.voNum(n)} percent`; },
  voTimes(n) { return `${this.voNum(n)} times`; },
  /* Trailing zeros are noise out loud: "one hundred and five point zero
   * rupees" is how a script sounds when it was written for a screen. */
  voNum(n) {
    const v = Number(n) || 0;
    const r = Math.abs(v) >= 100 ? Math.round(v) : Math.round(v * 100) / 100;
    return String(r).replace(/\.0+$/, '');
  },
  /* "A, B and C" — a spoken list, not a semicolon-separated one. */
  voList(items) {
    const xs = (items || []).map((s) => String(s).trim().replace(/[.;]+$/, ''))
      .filter(Boolean);
    if (!xs.length) return '';
    if (xs.length === 1) return xs[0];
    return `${xs.slice(0, -1).join(', ')}, and ${xs[xs.length - 1]}`;
  },

  // ── where the issue is in its own calendar ───────────────────────────
  /* The subscription judgement turns entirely on this, so it is computed
   * once and shared: "0.89 times" means nothing until you know whether the
   * window shuts tonight or on Thursday. */
  get voClock() {
    const ipo = this.ipo, d = this.d;
    // isoDate, not toISOString: the latter is UTC, so between midnight
    // and 05:30 IST it reports yesterday and every "closes today"
    // comparison silently misses. compute.js already solved this.
    const today = isoDate(new Date(this.now));
    const open = ipo.dates.open, close = ipo.dates.close;
    const span = (open && close)
      ? Math.round((Date.parse(close) - Date.parse(open)) / 864e5) + 1 : 3;
    return {
      today,
      status: d.dates.status,
      isOpen: d.dates.status === 'open',
      isLastDay: !!close && today === close,
      totalDays: Math.max(span, 1),
      close,
    };
  },

  // ── the judgement lines ──────────────────────────────────────────────
  /* Each returns the sentence a person on the desk would actually say about
   * THIS number, or '' when the honest answer is to say nothing. */

  voTakeStructure() {
    const d = this.d, fresh = Number(d.issue.fresh_pct) || 0;
    if (!Number(this.ipo.issue.fresh_cr) && !Number(this.ipo.issue.ofs_cr)) return '';
    if (fresh >= 90) return "Almost all of it is fresh capital. That's the version I like — you're funding a business, not buying somebody's exit.";
    if (fresh >= 60) return "Most of the money reaches the company. That's a healthy split.";
    if (fresh >= 35) return "It's a mix — some growth capital, some exit. Normal for an issue this size.";
    return "More than half of this is existing shareholders selling to you. That isn't automatically bad — early backers are entitled to an exit — but be clear that most of your money is not going into the business.";
  },

  /* Reel 1's reservation scene, spoken.
   *
   * The scene is skipped without a denominator, so this is only reached with
   * real data — but it returns '' on the same condition anyway, because the
   * script is also copied on its own by the publishing pack and a caption that
   * describes bars nobody saw is worse than a shorter caption.
   *
   * What it deliberately does NOT say: which regulation produced the split. A
   * 75/15/10 book is the shape SEBI requires in some cases and issuers land on
   * it for other reasons too, and reading a rule off a ratio is exactly the
   * kind of confident wrong claim §3.3 warns about. So it reports the numbers
   * and then the ONE consequence that is pure arithmetic: a thinner retail
   * slice is longer odds for the person listening.
   */
  voReservation() {
    const r = this.d.issue.reservation;
    if (!r || !r.has_data) return '';
    const name = { qib: 'institutional investors', nii: 'high net worth investors',
                   retail: 'retail investors', employee: 'employees',
                   shareholders: 'existing shareholders' };
    const parts = r.rows
      .map((row) => `${this.voPct(row.pct)} to ${name[row.key] || row.key}`);

    const lines = [
      `Here's how the issue is actually divided up, because it decides your `
      + `odds before you apply. ${parts.join(', ')}.`,
    ];

    if (r.tilt === 'institution_led') {
      lines.push(`So the retail slice is the small one here. That means the `
        + `institutions largely decide whether this issue gets covered, and it `
        + `also means retail applications are competing for a narrower pool — `
        + `if this one gets subscribed heavily, allotment is a lottery.`);
    } else if (r.tilt === 'retail_led') {
      lines.push(`Retail is getting the biggest share of this one, which is `
        + `unusual and it works in your favour on allotment — more shares in `
        + `the retail pool means more applications get filled.`);
    } else if (r.tilt === 'balanced') {
      lines.push(`That's a fairly standard split, and the retail portion here `
        + `is the pool your application would be drawn from.`);
    }

    return lines.join(' ');
  },

  /* The anchor book — its own scene since the split (see reels.js).
   *
   * These two paragraphs used to be tacked onto voReservation, which made that
   * scene four ideas long and 65 seconds of narration over one static card.
   * They are a separate claim anyway: reservation is how the book is divided,
   * this is how much of it was already committed before bidding opened.
   *
   * Phrased as "of that institutional portion" because the anchor book is
   * carved OUT of the QIB slice, not added alongside it — stated the other way
   * it would double-count, and the viewer would read the retail odds wrong.
   */
  voAnchor() {
    const r = this.d.issue.reservation;
    if (!r || !r.has_data || !r.anchor_pct) return '';
    const lines = [
      `One thing worth knowing: of that institutional portion, `
      + `${this.voPct(r.anchor_pct)} of the whole issue was placed with `
      + `anchor investors before bidding opened. That money is committed and `
      + `locked in for a period after listing, so it is not part of the `
      + `demand you see building on the subscription numbers.`,
    ];
    if (r.has_employee) {
      lines.push(`There's an employee quota carved out too, which is normal and `
        + `does not affect what retail is bidding into.`);
    }
    return lines.join(' ');
  },

  voTakeGmp() {
    const g = this.d.gmp;
    if (!g.has_data) return "Nobody is quoting a premium on this one yet. That's not a bad sign — it just means the grey market hasn't priced it. I'd rather tell you that than invent a number for you.";
    const pct = Number(g.pct) || 0;
    const lines = [];
    // A premium that has gone to par is not "holding steady" — movement
    // reads 'stable' because zero equals zero, and the reassuring sentence
    // that follows is the opposite of what a desk would say about an issue
    // the grey market has stopped paying up for.
    if (Number(g.gmp) === 0) {
      return Number(g.peak) > 0
        ? `It was quoted as high as ${this.voRupees(g.peak)} earlier and it is at par now. A premium that bleeds to nothing before listing is the grey market changing its mind in public, and I would not talk myself out of noticing it.`
        : `It has been at par throughout. No dealer is paying up for this one, and that silence is itself a view.`;
    }
    // One reading is not a trend, and 'stable' is merely what movement
    // defaults to when there is no previous day to compare against.
    if (g.days_tracked < 2) return '';
    if (g.movement === 'surge') lines.push(`It's climbing — ${this.voRupees(g.prev)} to ${this.voRupees(g.gmp)}. Momentum is real, but a premium that runs up this fast can come off just as fast in the final two days.`);
    else if (g.movement === 'drop') lines.push(`It's come down, ${this.voRupees(g.prev)} to ${this.voRupees(g.gmp)}. I watch a falling premium far more closely than a rising one — it usually means the grey market is losing conviction, and it tends to keep going.`);
    else lines.push(`It's holding steady around ${this.voRupees(g.gmp)}. Steady is a better sign than spiky.`);
    if (pct >= 60) lines.push("And a word on a premium this large: it prices in a perfect listing. Anything less than perfect and it deflates fast.");
    else if (pct < 0) lines.push("That's a discount, not a premium. The grey market is saying it expects this to list below the issue price. Take that seriously.");
    return lines.join(' ');
  },

  /* The one the whole rewrite exists for. */
  voTakeSubscription() {
    const s = this.d.subscription, c = this.voClock;
    if (!s.has_data) return c.isOpen ? "No subscription figures have come through yet today." : "Bidding hasn't opened yet, so there's nothing to read.";
    const total = Number(s.total) || 0;
    const when = this.fmtDate(c.close, true);

    if (c.isOpen && !c.isLastDay && total < 2) {
      return `Now — this looks light, and I don't want you reading anything into it. It's day ${s.day} of ${c.totalDays}. Early subscription numbers almost never tell you where an issue finishes. The institutions and the HNI money arrive on the last afternoon, and they arrive all at once. The number worth reading is the closing one, so that's the one I wait for — and I'd suggest you do the same. Look again on ${when}, after three in the afternoon.`;
    }
    if (c.isLastDay && total < 1) {
      return "Final day, and it's still not fully subscribed. That one I do take seriously. An issue that doesn't get covered can be pulled, or it can list soft. This is the point where I'd rather keep my money and wait for the next one.";
    }
    if (c.isLastDay && total < 3) {
      return "Final day and it's only just covered. Thin demand usually means a thin listing. Nothing here is exciting me.";
    }
    if (total >= 50) {
      return `${this.voTimes(total)} over. At this level allotment is a lottery, and I mean that literally — it goes to a computerised draw. Applying for five lots doesn't improve your odds in the retail category. One lot, and hope.`;
    }
    if (total >= 10) {
      return `${this.voTimes(total)} is heavy demand. Expect allotment to be a draw rather than a certainty — so size your application knowing you may well get nothing.`;
    }
    if (total >= 3) return "Comfortably covered. That's a healthy book without being a frenzy — and honestly, those often list better than the frenzied ones.";
    return "Covered, but only just. I'd want to see where it finishes.";
  },

  voTakeLeader() {
    const s = this.d.subscription;
    if (!s.has_data) return '';
    if (s.leader === 'qib') return "The institutions are leading this one. They do the deepest homework of anyone in the room, so that's the signal I weight most.";
    if (s.leader === 'nii') return "The HNI money is out in front. That money is often borrowed for a few days and it chases listing pops — it tells you about expected excitement, not about the business.";
    return "Retail is carrying this one. That's us — and we're usually the last to know. I'd want to see the institutions turn up before I called it strong.";
  },

  voTakeValuation() {
    const fin = this.d.financials;
    if (!fin.has_data || !fin.pe || !fin.pe_peer_avg) return '';
    const prem = Number(fin.pe_premium_pct) || 0;
    if (prem <= -20) return `At ${this.voNum(fin.pe)} times earnings against a peer average of ${this.voNum(fin.pe_peer_avg)}, this is priced below its competition. Cheap for a reason is a thing that exists — but on the numbers, it's asking less than the sector.`;
    if (prem < 20) return `At ${this.voNum(fin.pe)} times earnings it's priced roughly in line with its peers at ${this.voNum(fin.pe_peer_avg)}. Fair, not a bargain.`;
    return `At ${this.voNum(fin.pe)} times earnings against peers at ${this.voNum(fin.pe_peer_avg)}, you're paying a premium of ${this.voPct(Math.abs(prem))} to competitors already listed. For that to work, this company has to grow faster than they do — every single year. That's the bet.`;
  },

  voTakeAllotmentOdds() {
    const s = this.d.subscription;
    if (!s.has_data || !s.retail) return '';
    const r = Number(s.retail) || 0;
    let line;
    if (r >= 5) line = `Retail is ${this.voTimes(r)} over, so roughly one application in ${Math.round(r)} gets a lot. Apply for one lot. Extra lots do not improve your chances.`;
    else if (r >= 1) line = "Retail is covered, so allotment will be a draw — but a kind one. Most single-lot applications should get something.";
    else line = "Retail isn't fully covered, so if you apply you'll very likely get the full allotment. Ask yourself why nobody else wanted it.";
    return line + this.voTakeHniOdds();
  },

  /* The same question for the two HNI tranches, which the card now prices
     beside retail — so the narration has to cover them or it contradicts what
     is on screen for six seconds.
   *
   * Worth saying out loud rather than leaving on the card: an HNI hears
   * "retail is 61 times over" and assumes their own book is comparable, and on
   * Tempsens it was five times worse. The two tranches also diverge from each
   * other often enough to be worth a sentence — sHNI 281 against bHNI 331 here
   * — because which side of ten lakh you apply on is a choice, and this is the
   * number that should decide it.
   *
   * Silent when the exchange did not publish the split, which is every issue
   * whose subscription rows predate those columns. */
  voTakeHniOdds() {
    const s = this.d.subscription;
    const sh = Number(s.nii_small) || 0, bh = Number(s.nii_big) || 0;
    if (sh < 1 && bh < 1) return '';
    const say = (n) => `one in ${Math.round(n)}`;
    if (sh >= 1 && bh >= 1) {
      const cheaper = sh < bh ? 'the smaller' : 'the bigger';
      return ` If you're applying as an HNI, the numbers are different and they're worse: `
        + `${this.voTimes(sh)} in the two-to-ten lakh book, about ${say(sh)}, and `
        + `${this.voTimes(bh)} above ten lakh, about ${say(bh)}. `
        + `On this issue ${cheaper} ticket has the better odds — that is not always true, `
        + `so it is worth checking rather than assuming.`;
    }
    const only = sh >= 1 ? sh : bh;
    const which = sh >= 1 ? 'the two-to-ten lakh book' : 'the above-ten-lakh book';
    return ` As an HNI in ${which}, it's ${this.voTimes(only)} over — about ${say(only)}.`;
  },

  /* ── the English scripts, per scene ───────────────────────────────────
   *
   * Returns a map of scene id -> the narration for that scene, not one
   * block of text.
   *
   * That shape is the whole point. A reel is a sequence of scenes, each
   * holding for a fixed number of seconds, and until now those seconds were
   * hand-tuned constants in reels.js with no relationship to what the
   * narrator is saying over them. A scene with four bullets and a scene with
   * one both held for five seconds. Keyed by scene, the card can measure the
   * words it is actually reading and hold for exactly that long — see
   * `speakSeconds` and `autoHolds` in studio.js.
   *
   * It is also where a hand-written reference script slots in later: one
   * scene's line can be replaced without touching the rest of the reel.
   *
   * `enScript` below flattens this back into the single block the Script
   * panel has always shown, in scene order, so nothing downstream changed.
   */
  enSegments(n) {
    const ipo = this.ipo, d = this.d, L = this.loc;
    // The header reads `reelSeconds`, which reaches here through scriptHolds,
    // and Alpine evaluates that on the first paint — before loadCatalogue has
    // resolved and `ipo` is still null. Every scene below dereferences
    // ipo.issue, so without this the boot throws once per timing expression on
    // the page. Alpine swallows them and the numbers fill in on the next
    // render, which is why it was invisible; it still buried any real error in
    // ten identical ones.
    if (!ipo || !d) return {};
    const iss = ipo.issue, g = d.gmp, s = d.subscription, fin = d.financials;
    const dt = (x) => this.fmtDate(x, true);
    const c = this.voClock;
    const R = this.voRupees.bind(this);

    if (n === 1) {
      const sme = ipo.board === 'SME'
        ? " One thing to know up front: this is an SME issue. Bigger lot size, thinner trading after listing, and a wider swing in both directions. That isn't a warning not to apply — it's a warning to size it properly."
        : '';
      // An issue that has filed its papers but not its terms leaves these at
      // 0, and 0 spoken aloud is a claim: "price band, zero rupees to 788",
      // "the smallest cheque you can write is zero rupees". Both are said
      // with total confidence and both are false. Say only what is known.
      const band = (iss.price_low && iss.price_high)
        ? `Price band, ${R(iss.price_low)} to ${R(iss.price_high)}.`
        : iss.price_high
          ? `The upper end of the price band is ${R(iss.price_high)}; the floor isn't out yet.`
          : `The price band hasn't been announced yet.`;
      const lot = (iss.lot_size && d.issue.min_investment)
        ? `One lot is ${iss.lot_size} shares, so the smallest cheque you can write is ${R(d.issue.min_investment)}.`
        : `The lot size isn't published yet, so I can't tell you the minimum application today — I'll bring it the moment it's out.`;
      return {
hook: `${ipo.company}. Let me give you the terms first, then tell you what I make of them.`,
// Bullets, not list items — each is already a full sentence, so voList()
// would run them together as "A, B, and C." with capitals mid-clause.
company: L.overview.length
  ? L.overview.map((x) => String(x).replace(/\.$/, '')).join('. ') + '.' : '',
background: (L.background || []).length
  ? L.background.map((x) => String(x).replace(/\.$/, '')).join('. ') + '.' : '',
// An all-fresh issue has no OFS leg, and naming it anyway spends the
// listener's attention on "0 crore rupees is an offer for sale" — the one
// sentence in the reel that is pure noise.
split: [Number(iss.ofs_cr)
  ? `Now the part most people scroll past. The issue is ${this.voCrore(d.issue.total_cr)}. Of that, ${this.voCrore(iss.fresh_cr)} is a fresh issue — new money going into the company. ${this.voCrore(iss.ofs_cr)} is an offer for sale, which is existing shareholders selling their stake to you. So ${this.voPct(d.issue.fresh_pct)} of it is fresh.`
  : `Now the part most people scroll past. The whole ${this.voCrore(d.issue.total_cr)} is a fresh issue — every rupee goes into the company. There's no offer for sale here, so nobody is using this listing to cash out.`,
  this.voTakeStructure()].filter(Boolean).join(' '),
reservation: this.voReservation(),
        anchor: this.voAnchor(),
terms: `${band} ${lot}${sme}`,
// Closing line points at the long-form cut of the same IPO. Reel 1 is the
// one people find first — it is the "what is this company" search — so it
// is the right place to send them somewhere longer, and it is the only reel
// whose job is finished once the viewer knows what the issue is.
dates: `It opens ${dt(ipo.dates.open)}, closes ${dt(ipo.dates.close)}, and lists ${dt(ipo.dates.listing)}. The full breakdown of this one — financials, valuation and the risks — is on the channel, so go and watch that before you decide anything.`,
      };
    }

    if (n === 2 && this.gmpMode === 'board') {
      const quoted = this.boardRows.filter((r) => r.has_gmp).slice(0, 5);
      return {
boardhook: `Today's grey market board. ${this.boardRows.length} IPOs on the radar.`,
board: [
  quoted.map((r) => `${r.company}, ${R(r.gmp)}, that's ${this.voPct(r.gmp_pct)}`).join('. ') + '.',
  `Quick reminder on what you're looking at. The grey market premium is what people are unofficially willing to pay for these shares before they list. No exchange publishes it. No regulator stands behind it. It is a rumour with a number attached — a useful rumour, and one I check every day, but a rumour. It moves, and it can move hard in the last forty-eight hours.`,
].join(' '),
      };
    }

    if (n === 2) {
      if (!g.has_data) {
        return {
hook: `${ipo.company}, grey market premium.`,
gauge: `If this is new to you: the grey market premium is what people are unofficially willing to pay for these shares before they list. It's not an exchange price and no regulator publishes it.`,
listing: this.voTakeGmp(),
trail: `When there's a quote worth reporting, you'll get it here.`,
        };
      }
      const when = g.is_stale
        ? `The most recent reading, from ${dt(g.updated)}, is`
        : `Today it's`;
      // A premium of exactly zero is a real quote — the grey market pricing
      // the issue at par — and it is the one reading that must not be read
      // out in the "X rupees over the band, that's Y percent" frame, where
      // it lands as "zero rupees over the band, that's zero percent" and
      // sounds like missing data. Said plainly it is the most useful
      // sentence on the reel.
      const headline = Number(g.gmp) === 0
        ? `${when} zero. Nothing. The grey market is pricing this one at par — no premium at all over the ${R(iss.price_high)} band.`
        : `${when} ${R(g.gmp)} over the upper band of ${R(iss.price_high)}. That's ${this.voPct(g.pct)}.`;
      // Only worth saying when the lot is known, and only when there is a
      // premium to multiply by it.
      const perLot = (iss.lot_size && Number(g.gain_per_lot))
        ? ` Put differently — if it listed there, one lot of ${iss.lot_size} shares would be worth about ${R(g.gain_per_lot)} more than you paid for it.`
        : '';
      const tracked = g.days_tracked === 1
        ? `We've only got one day of readings on this so far, at ${R(g.gmp)} — too early to call a trend.`
        : `We've tracked it ${g.days_tracked} days since the announcement. High of ${R(g.peak)}, low of ${R(g.trough)}.`;
      return {
hook: [`${ipo.company}, grey market premium.`,
  `If this is new to you: GMP is what people are unofficially willing to pay for these shares before they list. It is not an exchange price. No regulator publishes it. It's a rumour with a number attached — a useful one, which is why I track it daily, but a rumour.`].join(' '),
gauge: [`${headline}${perLot}`, this.voTakeGmp()].filter(Boolean).join(' '),
listing: tracked,
trail: `And I'll say it again because it matters: this number is unofficial and it changes every single day. Never let it be the only reason you apply.`,
      };
    }

    // Reel 3's all-IPOs cut: every issue taking bids, soonest to close first.
    if (n === 3 && this.gmpMode === 'board') {
      const tb = this.subBoardTable;
      if (!tb.total) {
        return {
subboardhook: `Nothing is taking bids today.`,
subboard: `No issue is open right now, so there is no subscription to report. That is an ordinary week rather than a bad one — and the money you did not commit is the money you get to use when the next one opens.`,
        };
      }
      const closingToday = tb.rows.filter((r) => r.close === this.voClock.today);
      return {
subboardhook: `Today's subscription board. ${tb.total} ${tb.total === 1 ? 'issue is' : 'issues are'} taking bids right now.`,
subboard: [
  tb.rows.map((r) => `${r.company}, ${this.voTimes(r.subscription)} overall`
    + (r.retail != null ? `, retail ${this.voTimes(r.retail)}` : '')).join('. ') + '.',
  closingToday.length
    ? `${this.voList(closingToday.map((r) => r.company))} ${closingToday.length === 1 ? 'shuts' : 'shut'} tonight — and the last afternoon is when the institutional money arrives, so this morning's figure is not the one it finishes on.`
    : '',
  `One thing to hold on to: subscription moves all day, and the closing figure is the only one worth acting on.`,
].filter(Boolean).join(' '),
      };
    }

    if (n === 3) {
      if (!s.has_data) {
        return { hook: `${ipo.company}, subscription.`, bars: this.voTakeSubscription() };
      }
      return {
hook: `${ipo.company}, day ${s.day} of subscription.`,
bars: [
  `Quick translation, because these three letters put people off. QIB is the big institutions — mutual funds, insurers, banks. NII is high net worth individuals, the large private money. Retail is you and me, anything up to two lakh rupees.`,
  `QIB, ${this.voTimes(s.qib)}. NII, ${this.voTimes(s.nii)}. Retail, ${this.voTimes(s.retail)}. Overall the issue is subscribed ${this.voTimes(s.total)}.`,
].join(' '),
trend: [this.voTakeSubscription(), this.voTakeLeader()].filter(Boolean).join(' '),
      };
    }

    if (n === 4) {
      const growth = fin.has_data
        ? `Revenue has grown ${this.voPct(fin.revenue_cagr)} a year, to ${this.voCrore(fin.latest.revenue)}. Profit, ${this.voCrore(fin.latest.pat)}.`
        : `The financials aren't published in enough detail to take apart properly yet, and I won't pretend otherwise.`;
      const margin = (fin.has_data && fin.present && fin.present.ebitda)
        ? `Operating margin is ${this.voPct(fin.latest.ebitda_margin)}, ${Number(fin.margin_shift_bps) >= 0 ? 'up' : 'down'} ${Math.abs(Number(fin.margin_shift_bps) || 0)} basis points on last year.`
        : '';
      return {
hook: `So — should you apply to ${ipo.company}? Here's how I'd think about it.`,
financials: [growth, margin].filter(Boolean).join(' '),
valuation: this.voTakeValuation(),
flags: [
  L.green_flags.length ? `What I like: ${this.voList(L.green_flags)}.` : '',
  L.red_flags.length ? `What worries me: ${this.voList(L.red_flags)}.` : '',
  L.risk ? `And the single biggest risk — ${String(L.risk).replace(/\.$/, '')}.` : '',
  `None of this is a recommendation. It's the homework. The decision is yours.`,
].filter(Boolean).join(' '),
stake: [
  iss.lot_size && d.issue.min_investment
    ? `So what does applying actually cost you? One lot is ${iss.lot_size} shares at ${R(iss.price_high)}, which is ${R(d.issue.min_investment)}.`
    : `The lot size isn't out yet, so I can't tell you the cheque today.`,
  (g.has_data && iss.lot_size && Number(g.gain_per_lot))
    ? `At today's premium that one lot would be worth about ${R(Math.abs(g.gain_per_lot))} ${Number(g.gain_per_lot) >= 0 ? 'more' : 'less'} than you paid — ${this.voPct(g.pct)}. That is today's number, not a forecast.`
    : '',
  (s.has_data && Number(s.retail) >= 1)
    ? `Retail is ${this.voTimes(s.retail)} over, so roughly one application in ${Number(s.retail) < 10 ? Number(s.retail).toFixed(1) : Math.round(s.retail)} gets a lot.`
    : (s.has_data ? `Retail isn't covered yet, so an application now would very likely get the full allotment.` : ''),
  `And the rule that saves people money: extra lots do not improve your odds in retail. One lot enters the draw exactly like five do. Apply with money you can afford to have locked up.`,
].filter(Boolean).join(' '),
      };
    }

    if (n === 5) {
      const scoreLine = d.score.has_data
        ? `Our IPO Pulse score comes out at ${Number(d.score.effective).toFixed(1)} out of 10. ${this.verdictText}.`
        : `There isn't enough published data to score this one honestly yet — so I'm not going to give you a number that looks confident and isn't.`;
      return {
score: [`Final word on ${ipo.company}.`, scoreLine].join(' '),
verdict: [
// Spoken immediately BEFORE the call, not filed at the end of the video.
// India's Research Analyst regulations exempt an opinion on a public offer
// made only through public media, but the exemption is conditional: the
// name, the registration status and any financial interest have to be
// disclosed *at the time the recommendation is made*. A disclaimer that
// arrives forty seconds later, after the viewer has heard "apply", is not
// the thing the rule asks for. See docs/YOUTUBE-PLAYBOOK.md.
`Before I give you the calls, the part that has to sit right next to them. I am not a SEBI-registered research analyst or investment adviser. I hold no position in this issue. What follows is my opinion on public information, published openly to everyone — it is not personalised advice, and it is not a solicitation to buy or sell anything.`,
// Only spoken when all three calls have actually been made. They default to
// empty now (see models.py's Analysis), and `t('r_')` would have read out a
// missing label key — but the real reason for the guard is that a
// half-answered set of recommendations is worse than none: the viewer cannot
// tell which of the three was withheld and which was a considered "no".
(ipo.analysis.reco_retail && ipo.analysis.reco_hni && ipo.analysis.reco_long)
  ? `With that said — for retail, ${this.t('r_' + ipo.analysis.reco_retail)}. For HNI, ${this.t('r_' + ipo.analysis.reco_hni)}. And holding it long term, ${this.t('r_' + ipo.analysis.reco_long)}.`
  : `I am not going to hand you a call on this one yet. The published data does not support one, and inventing a verdict to fill the slot is exactly how these channels lose people money.`,
].filter(Boolean).join(' '),
who: [
this.voTakeAllotmentOdds(),
`The issue closes ${dt(ipo.dates.close)} at ${ipo.dates.close_time}. If you're applying, do it before the cut-off — your bank's UPI mandate needs time to clear, and every year people miss it by an hour.`,
`And the thing I would most want you to take away. Only ever apply with money you can afford to have locked up, or to lose. An IPO is not a savings account. Do your own research, read the offer document, and if you want advice that fits your situation, speak to a SEBI-registered adviser.`,
].filter(Boolean).join(' '),
      };
    }

    if (n === 6) {
      const out = d.listing.status === 'out';
      const lr = this.listingRange;
      return {
status: `${ipo.company} allotment ${out ? 'is out — go and check it now' : `is expected on ${dt(ipo.dates.allotment)}`}. The registrar is ${iss.registrar}.`,
checklist: `Here's how to check it in about ten seconds. ${this.voList(this.steps)}.`,
listing: [
// No premium and no published range means there is no forecast to give,
// and the fallback rendered it as "zero rupees to zero rupees, that's zero
// percent to zero percent" — a forecast of total loss, stated confidently,
// where the truth is that nobody has one.
(lr.has && (lr.low || lr.high))
  ? `Listing is on ${dt(ipo.dates.listing)}. On the current premium the expected range is ${R(lr.low)} to ${R(lr.high)} — that's ${this.voPct(lr.low_pct)} to ${this.voPct(lr.high_pct)} on the issue price.`
  : `Listing is on ${dt(ipo.dates.listing)}. There's no grey market premium to project a range from, so I'm not going to guess one for you.`,
`And whatever happens on listing day: have your exit decided before the bell, not during it. If you didn't get an allotment, don't chase it at the open — that's the most expensive hour of the stock's life.`,
`Follow for the allotment alert.`,
].filter(Boolean).join(' '),
      };
    }
    return {};
  },

  /* Scene ids for a reel, in play order — mirrors scenesFor() in reels.js so
   * the script walks the same scenes the card actually shows. A reel 1 whose
   * `background` scene was dropped for want of copy must not be narrated as
   * though it still had one. */
  sceneIdsFor(n) {
    const reel = REELS[n - 1];
    if (!reel) return [];
    return scenesFor(reel, this.gmpMode, this.ipo).map((sc) => sc.id);
  },

  /* The flat script the Script panel has always shown: every segment, in
   * scene order, one per line. Nothing downstream had to change. */
  enScript(n) {
    const segs = this.enSegments(n) || {};
    return this.sceneIdsFor(n)
      .map((id) => String(segs[id] || '').trim())
      .filter(Boolean)
      .join('\n');
  },

  // ── the weekly strategy, across every IPO on the board ───────────────
  /* Every other output on this page is about one company. This one is not,
   * and that is the point: nobody in the community is choosing whether to
   * apply to Lalithaa in isolation. They have a fixed amount of money, three
   * issues open in the same week, and a UPI mandate that freezes the cash
   * until allotment — so applying to the first one they see is a decision
   * about the other two, whether or not they realise it.
   *
   * Ranked on what actually survives contact with a listing: the premium as
   * a percentage of the band (an absolute rupee premium flatters an
   * expensive share), whether that premium is holding rather than sliding,
   * and mainboard ahead of SME at equal merit, because an SME lot is larger
   * and its exit is thinner.
   */
  get strategyScript() {
    const rows = this.boardRows || [];
    if (!rows.length) return '';
    const R = this.voRupees.bind(this);
    const dt = (x) => this.fmtDate(x, true);
    // isoDate, not toISOString: the latter is UTC, so between midnight
    // and 05:30 IST it reports yesterday and every "closes today"
    // comparison silently misses. compute.js already solved this.
    const today = isoDate(new Date(this.now));

    const open = rows.filter((r) => r.status === 'open');
    const soon = rows.filter((r) => r.status === 'upcoming');
    const score = (r) => (Number(r.gmp_pct) || 0)
      + (r.movement === 'surge' ? 4 : r.movement === 'drop' ? -8 : 0)
      + (r.board === 'SME' ? -5 : 0);
    const ranked = open.slice().sort((a, b) => score(b) - score(a));

    const out = [];
    out.push(`IPO Pulse — where I'd put my money this week, and why.`);

    if (!open.length) {
      out.push(`Nothing is open for bidding right now. That's not a dead week — it's the week you do the reading, so you're not deciding in a hurry when the next one opens.`);
    } else {
      out.push(`${open.length} ${open.length === 1 ? 'issue is' : 'issues are'} taking bids right now.`);
      ranked.forEach((r, idx) => {
        const closing = r.close === today ? 'closes TODAY' : `closes ${dt(r.close)}`;
        const prem = r.has_gmp
          ? `premium ${R(r.gmp)}, ${this.voPct(r.gmp_pct)}`
          : `no grey market quote yet`;
        const sub = r.subscription != null
          ? `subscribed ${this.voTimes(r.subscription)}`
          : `no subscription figure yet`;
        const lot = r.min_investment ? `One lot is ${R(r.min_investment)}. ` : '';
        out.push(`${idx + 1}. ${r.company}${r.board === 'SME' ? ', an SME issue' : ''}. ${closing}. ${lot}${prem}, ${sub}.`);
      });

      const top = ranked[0];
      if (top && (Number(top.gmp_pct) || 0) > 0) {
        out.push(`If you can only fund one application this week, ${top.company} is where the numbers point. That is not the same as saying it will list well — it's saying it has the best combination of premium and demand of what's in front of us today.`);
      } else {
        out.push(`Honestly? Nothing here is compelling on the numbers. There is no rule that says you have to apply every week, and the money you didn't lose is the money you get to use on a better issue next month.`);
      }

      const closingToday = open.filter((r) => r.close === today);
      if (closingToday.length) {
        out.push(`${this.voList(closingToday.map((r) => r.company))} ${closingToday.length === 1 ? 'shuts' : 'shut'} tonight. This is the afternoon the institutional money shows up, so the subscription number you saw this morning is not the one it will finish on. Check it after three, then decide.`);
      }
    }

    if (soon.length) {
      out.push(`Coming up: ${this.voList(soon.slice(0, 4).map((r) => `${r.company} from ${dt(r.open)}`))}. Don't commit every rupee this week — keep something back for those.`);
    }

    out.push(`Now the part that matters more than any pick on that list.`);
    out.push(`One. In the retail category, extra lots do not improve your odds. When an issue is oversubscribed it goes to a computerised draw, and one lot enters that draw exactly like five do. Apply for one, across more issues — not five, across one.`);
    out.push(`Two. Your money is blocked from the moment you apply until allotment, usually five or six days. If you put everything into a Monday issue, you cannot touch the Thursday one. Plan the week, not the day.`);
    out.push(`Three. Never apply on the premium alone. It is unofficial, it is a rumour with a number attached, and it moves hardest in the last forty-eight hours — which is exactly when most people commit.`);
    out.push(`And four, the one that matters most if you are just starting out. Listing gains are a bonus, not a plan. Only apply with money you can afford to have locked up, or to lose. Not one rupee of borrowed money for a listing pop — that is the mistake that turns a bad week into a bad year.`);
    out.push(`None of this is investment advice. Do your own research, or speak to a SEBI-registered adviser.`);
    return out.join('\n');
  },

  // ── voiceover script, one per reel ───────────────────────────────────
  scriptFor(reelNumber) {
    if (!this.ipo || !this.d) return '';
    const i = LANG_INDEX[this.lang] ?? 0;
    // English is authored by enScript(); hi/te keep the template below.
    if (i === 0) return this.enScript(reelNumber).replace(/\n{2,}/g, '\n').trim();
    const ipo = this.ipo, d = this.d, L = this.loc;
    const f = this.fmt.bind(this), dt = (x) => this.fmtDate(x, true);
    const iss = ipo.issue, g = d.gmp, s = d.subscription, fin = d.financials;
    const pctTxt = g.pct.toFixed(1);
    // The card's reel-6 range falls back to the GMP-implied band when nobody
    // has typed an expected range; the script used the raw d.listing and so
    // read "expected range ₹0 to ₹0 — that's -100% to -100%" while the screen
    // beside it showed a real band. Same source for both now.
    const listingRange = this.listingRange;

    const move = {
      surge: ['GMP is surging.', 'GMP में तेज़ी है।', 'GMP జోరుగా ఉంది.'],
      stable: ['GMP is holding steady.', 'GMP स्थिर है।', 'GMP స్థిరంగా ఉంది.'],
      drop: ['GMP has dropped.', 'GMP गिरा है।', 'GMP తగ్గింది.'],
    }[g.movement][i];

    // The script is read aloud onto a recorded video, so "today's GMP" over a
    // reading that is not today's is a false claim in the most permanent
    // place it could be made. When the figure is stale, name its date and
    // stop calling the previous point "yesterday".
    const asOf = g.updated ? dt(g.updated) : '';
    const gmpWhen = g.is_stale
      ? [`the latest grey market premium, from ${asOf}, is`,
         `${asOf} का ताज़ा ग्रे मार्केट प्रीमियम है`,
         `${asOf} నాటి తాజా గ్రే మార్కెట్ ప్రీమియం`][i]
      : [`today's grey market premium is`,
         `आज का ग्रे मार्केट प्रीमियम है`,
         `నేటి గ్రే మార్కెట్ ప్రీమియం`][i];
    // Same rule for the spoken financials: without an EBITDA series this
    // sentence read "EBITDA margin is 0%, down 0 basis points" out loud.
    const ebitdaSentence = (fin.has_data && fin.present && fin.present.ebitda)
      ? [` EBITDA margin is ${fin.latest.ebitda_margin}%, ${fin.margin_shift_bps >= 0 ? 'up' : 'down'} ${Math.abs(fin.margin_shift_bps)} basis points.`,
         ` EBITDA मार्जिन ${fin.latest.ebitda_margin}%, ${Math.abs(fin.margin_shift_bps)} बेसिस पॉइंट ${fin.margin_shift_bps >= 0 ? 'ऊपर' : 'नीचे'}।`,
         ` EBITDA మార్జిన్ ${fin.latest.ebitda_margin}%, ${Math.abs(fin.margin_shift_bps)} బేసిస్ పాయింట్లు ${fin.margin_shift_bps >= 0 ? 'పెరిగింది' : 'తగ్గింది'}.`][i]
      : '';

    // Withheld rather than stated when coverage is too thin: a freshly
    // discovered IPO with only its fresh/OFS split scores a clean 10.0, which
    // is true arithmetic and a nonsense verdict to read aloud.
    const scoreValue = Number(d.score.effective).toFixed(1);
    const scoreSentence = d.score.has_data
      ? [`IPO Pulse score: ${scoreValue} out of 10.`,
         `IPO पल्स स्कोर: 10 में से ${scoreValue}।`,
         `IPO పల్స్ స్కోర్: 10కి ${scoreValue}.`][i]
      : [`Too little data to score this one yet.`,
         `इसे स्कोर देने के लिए अभी बहुत कम डेटा है।`,
         `దీనికి స్కోర్ ఇవ్వడానికి ఇంకా చాలా తక్కువ డేటా.`][i];

    const gmpMoveLine = g.is_stale
      ? [`Previous reading ₹${g.prev}, latest ₹${g.gmp}.`,
         `पिछली रीडिंग ₹${g.prev}, ताज़ा ₹${g.gmp}।`,
         `మునుపటి రీడింగ్ ₹${g.prev}, తాజాది ₹${g.gmp}.`][i]
      : [`Yesterday ₹${g.prev}, today ₹${g.gmp}.`,
         `कल ₹${g.prev}, आज ₹${g.gmp}।`,
         `నిన్న ₹${g.prev}, ఈరోజు ₹${g.gmp}.`][i];

    const S = {
      1: [
`${ipo.company} IPO. Price band ₹${iss.price_low} to ₹${iss.price_high}, lot size ${iss.lot_size} shares — minimum ₹${f(d.issue.min_investment)}.
${L.overview.join('. ')}.
Out of the ₹${f(d.issue.total_cr)} crore issue, ₹${f(iss.fresh_cr)} crore is a FRESH ISSUE that goes into the company's growth, and ${iss.ofs_cr > 0 ? `₹${f(iss.ofs_cr)} crore is an OFS — promoters cashing out` : `there is no OFS at all, so nobody is using this listing to cash out`}. That's ${d.issue.fresh_pct}% fresh.
Opens ${dt(ipo.dates.open)}, closes ${dt(ipo.dates.close)}, lists ${dt(ipo.dates.listing)}.`,

`${ipo.company} का IPO। प्राइस बैंड ₹${iss.price_low} से ₹${iss.price_high}, लॉट साइज़ ${iss.lot_size} शेयर — कम से कम ₹${f(d.issue.min_investment)}।
${L.overview.join('। ')}।
₹${f(d.issue.total_cr)} करोड़ के इश्यू में ₹${f(iss.fresh_cr)} करोड़ फ्रेश इश्यू है जो कंपनी की ग्रोथ में जाएगा, और ${iss.ofs_cr > 0 ? `₹${f(iss.ofs_cr)} करोड़ OFS है — यानी प्रमोटर पैसा निकाल रहे हैं` : `OFS बिल्कुल नहीं है — कोई प्रमोटर पैसा नहीं निकाल रहा`}। यानी ${d.issue.fresh_pct}% फ्रेश।
${dt(ipo.dates.open)} को खुलेगा, ${dt(ipo.dates.close)} को बंद, ${dt(ipo.dates.listing)} को लिस्टिंग।`,

`${ipo.company} IPO. ప్రైస్ బ్యాండ్ ₹${iss.price_low} నుంచి ₹${iss.price_high}, లాట్ సైజ్ ${iss.lot_size} షేర్లు — కనీసం ₹${f(d.issue.min_investment)}.
${L.overview.join('. ')}.
₹${f(d.issue.total_cr)} కోట్ల ఇష్యూలో ₹${f(iss.fresh_cr)} కోట్లు ఫ్రెష్ ఇష్యూ — కంపెనీ గ్రోత్‌కి. ${iss.ofs_cr > 0 ? `₹${f(iss.ofs_cr)} కోట్లు OFS — ప్రమోటర్లు డబ్బు తీసుకుంటున్నారు` : `OFS అస్సలు లేదు — ఎవరూ డబ్బు తీసుకోవడం లేదు`}. అంటే ${d.issue.fresh_pct}% ఫ్రెష్.
${dt(ipo.dates.open)}న ఓపెన్, ${dt(ipo.dates.close)}న క్లోజ్, ${dt(ipo.dates.listing)}న లిస్టింగ్.`,
      ],
      2: this.gmpMode === 'board' ? [
`Today's GMP board. ${this.boardRows.length} IPOs on the radar.
${this.boardRows.filter((r) => r.has_gmp).slice(0, 5).map((r) => `${r.company}: GMP ₹${r.gmp}, that's ${r.gmp_pct}%`).join('. ')}.
GMP is unofficial grey-market data and it changes every day.`,

`आज का GMP बोर्ड। ${this.boardRows.length} IPO नज़र में हैं।
${this.boardRows.filter((r) => r.has_gmp).slice(0, 5).map((r) => `${r.company}: GMP ₹${r.gmp}, यानी ${r.gmp_pct}%`).join('। ')}।
याद रखें, GMP अनऑफिशियल है और रोज़ बदलता है।`,

`నేటి GMP బోర్డ్. ${this.boardRows.length} IPOలు రాడార్‌లో ఉన్నాయి.
${this.boardRows.filter((r) => r.has_gmp).slice(0, 5).map((r) => `${r.company}: GMP ₹${r.gmp}, అంటే ${r.gmp_pct}%`).join('. ')}.
GMP అనధికారికం, ప్రతిరోజూ మారుతుంది.`,
      ] : [
`${ipo.company} — ${gmpWhen} ₹${g.gmp}, that is ${pctTxt}% over the upper band of ₹${iss.price_high}.
Estimated listing price ₹${f(g.est_listing)}. On one lot of ${iss.lot_size} shares that's about ₹${f(g.gain_per_lot)}.
${gmpMoveLine} ${move}
We've tracked it for ${g.days_tracked} days since announcement — peak ₹${g.peak}, low ₹${g.trough}.
GMP is unofficial and changes every single day.`,

`${ipo.company} — ${gmpWhen} ₹${g.gmp}, यानी ₹${iss.price_high} के अपर बैंड से ${pctTxt}% ऊपर।
अनुमानित लिस्टिंग प्राइस ₹${f(g.est_listing)}। एक लॉट यानी ${iss.lot_size} शेयर पर करीब ₹${f(g.gain_per_lot)}।
${gmpMoveLine} ${move}
ऐलान से अब तक ${g.days_tracked} दिन ट्रैक किया — सबसे ऊपर ₹${g.peak}, सबसे नीचे ₹${g.trough}।
GMP अनऑफिशियल है और रोज़ बदलता है।`,

`${ipo.company} — ${gmpWhen} ₹${g.gmp}, అంటే ₹${iss.price_high} అప్పర్ బ్యాండ్ కంటే ${pctTxt}% ఎక్కువ.
అంచనా లిస్టింగ్ ధర ₹${f(g.est_listing)}. ఒక లాట్ ${iss.lot_size} షేర్లపై సుమారు ₹${f(g.gain_per_lot)}.
${gmpMoveLine} ${move}
ప్రకటన నుంచి ${g.days_tracked} రోజులు ట్రాక్ చేశాం — గరిష్టం ₹${g.peak}, కనిష్టం ₹${g.trough}.
GMP అనధికారికం, ప్రతిరోజూ మారుతుంది.`,
      ],
      3: s.has_data ? [
`${ipo.company}, day ${s.day} subscription. QIB ${s.qib} times, NII ${s.nii} times, Retail ${s.retail} times.
Overall the IPO is subscribed ${s.total} times.
${s.leader === 'nii' ? 'The big money is leading — watch the final-day surge.' : s.leader === 'qib' ? 'Institutions are leading, usually a good sign.' : 'Retail is carrying this one — institutions often pile in on the last day.'}`,

`${ipo.company}, दिन ${s.day} का सब्सक्रिप्शन। QIB ${s.qib} गुना, NII ${s.nii} गुना, रिटेल ${s.retail} गुना।
कुल मिलाकर IPO ${s.total} गुना सब्सक्राइब हुआ है।
${s.leader === 'nii' ? 'बड़ा पैसा आगे है — आखिरी दिन की तेज़ी देखिए।' : s.leader === 'qib' ? 'इंस्टीट्यूशन आगे हैं, आमतौर पर अच्छा संकेत।' : 'रिटेल आगे है — इंस्टीट्यूशन अक्सर आखिरी दिन आते हैं।'}`,

`${ipo.company}, ${s.day}వ రోజు సబ్‌స్క్రిప్షన్. QIB ${s.qib} రెట్లు, NII ${s.nii} రెట్లు, రిటైల్ ${s.retail} రెట్లు.
మొత్తంగా IPO ${s.total} రెట్లు సబ్‌స్క్రైబ్ అయ్యింది.
${s.leader === 'nii' ? 'పెద్ద డబ్బు ముందుంది — చివరి రోజు జోరు గమనించండి.' : s.leader === 'qib' ? 'సంస్థలు ముందున్నాయి, సాధారణంగా మంచి సంకేతం.' : 'రిటైల్ ముందుంది — సంస్థలు చివరి రోజు వస్తాయి.'}`,
      ] : ['Subscription has not opened yet.', 'सब्सक्रिप्शन अभी शुरू नहीं हुआ।', 'సబ్‌స్క్రిప్షన్ ఇంకా మొదలవలేదు.'],
      4: [
`Should you apply to ${ipo.company}?
${fin.has_data ? `Revenue grew ${fin.revenue_cagr}% a year to ₹${f(fin.latest.revenue)} crore.${ebitdaSentence} Profit ₹${f(fin.latest.pat)} crore.` : ''}
${fin.pe ? `At ₹${iss.price_high} it trades at ${fin.pe} times earnings, against a peer average of ${fin.pe_peer_avg} — ${fin.pe_premium_pct < 0 ? `${Math.abs(fin.pe_premium_pct)}% cheaper` : `${fin.pe_premium_pct}% pricier`} than peers.` : ''}
Green flags: ${L.green_flags.join('; ')}.
Red flags: ${L.red_flags.join('; ')}.
Biggest risk — ${L.risk}.`,

`${ipo.company} में अप्लाई करें या नहीं?
${fin.has_data ? `रेवेन्यू हर साल ${fin.revenue_cagr}% बढ़कर ₹${f(fin.latest.revenue)} करोड़।${ebitdaSentence} मुनाफ़ा ₹${f(fin.latest.pat)} करोड़।` : ''}
${fin.pe ? `₹${iss.price_high} पर यह ${fin.pe} गुना earnings पर है, पीयर औसत ${fin.pe_peer_avg} के मुकाबले — ${fin.pe_premium_pct < 0 ? `${Math.abs(fin.pe_premium_pct)}% सस्ता` : `${fin.pe_premium_pct}% महँगा`}।` : ''}
पॉज़िटिव: ${L.green_flags.join('; ')}।
रेड फ्लैग्स: ${L.red_flags.join('; ')}।
सबसे बड़ा रिस्क — ${L.risk}।`,

`${ipo.company}కి అప్లై చేయాలా?
${fin.has_data ? `ఆదాయం ఏటా ${fin.revenue_cagr}% పెరిగి ₹${f(fin.latest.revenue)} కోట్లు.${ebitdaSentence} లాభం ₹${f(fin.latest.pat)} కోట్లు.` : ''}
${fin.pe ? `₹${iss.price_high} వద్ద ఇది ${fin.pe} రెట్ల earnings, పీర్ సగటు ${fin.pe_peer_avg}తో పోలిస్తే ${fin.pe_premium_pct < 0 ? `${Math.abs(fin.pe_premium_pct)}% చౌక` : `${fin.pe_premium_pct}% ఖరీదు`}.` : ''}
సానుకూలం: ${L.green_flags.join('; ')}.
రెడ్ ఫ్లాగ్స్: ${L.red_flags.join('; ')}.
అతిపెద్ద రిస్క్ — ${L.risk}.`,
      ],
      5: [
`Final verdict on ${ipo.company}. IPO Pulse score: ${Number(d.score.effective).toFixed(1)} out of 10. ${this.verdictText}.
${this.hasRecos ? `Retail — ${this.t('r_' + ipo.analysis.reco_retail)}. HNI — ${this.t('r_' + ipo.analysis.reco_hni)}. Long term — ${this.t('r_' + ipo.analysis.reco_long)}.` : `No call on this one yet — the published data does not support one.`}
The issue closes ${dt(ipo.dates.close)} at ${ipo.dates.close_time}. Apply before the cut-off.
This is not investment advice — do your own research.`,

`${ipo.company} पर फाइनल फैसला। IPO पल्स स्कोर: 10 में से ${Number(d.score.effective).toFixed(1)}। ${this.verdictText}।
${this.hasRecos ? `रिटेल — ${this.t('r_' + ipo.analysis.reco_retail)}। HNI — ${this.t('r_' + ipo.analysis.reco_hni)}। लॉन्ग टर्म — ${this.t('r_' + ipo.analysis.reco_long)}।` : `इस पर अभी कोई कॉल नहीं — पब्लिश्ड डेटा इसे सपोर्ट नहीं करता।`}
इश्यू ${dt(ipo.dates.close)} को ${ipo.dates.close_time} बजे बंद होगा। कट-ऑफ से पहले अप्लाई करें।
यह निवेश सलाह नहीं है — खुद रिसर्च करें।`,

`${ipo.company}పై తుది తీర్పు. IPO పల్స్ స్కోర్: 10కి ${Number(d.score.effective).toFixed(1)}. ${this.verdictText}.
${this.hasRecos ? `రిటైల్ — ${this.t('r_' + ipo.analysis.reco_retail)}. HNI — ${this.t('r_' + ipo.analysis.reco_hni)}. లాంగ్ టర్మ్ — ${this.t('r_' + ipo.analysis.reco_long)}.` : `దీనిపై ఇప్పటికి కాల్ లేదు — పబ్లిష్ అయిన డేటా దానికి సరిపోదు.`}
ఇష్యూ ${dt(ipo.dates.close)}న ${ipo.dates.close_time}కి ముగుస్తుంది. కట్-ఆఫ్‌కి ముందే అప్లై చేయండి.
ఇది పెట్టుబడి సలహా కాదు — మీరే పరిశోధించండి.`,
      ],
      6: [
`${ipo.company} allotment ${d.listing.status === 'out' ? 'is OUT — check right now' : `is expected on ${dt(ipo.dates.allotment)}`}. Registrar is ${iss.registrar}.
How to check in 10 seconds: ${this.steps.join('. ')}.
Listing on ${dt(ipo.dates.listing)}, expected range ₹${listingRange.low} to ₹${listingRange.high} — that's ${this.signed(listingRange.low_pct, 0)}% to ${this.signed(listingRange.high_pct, 0)}% on the issue price.
Follow for the allotment alert.`,

`${ipo.company} का अलॉटमेंट ${d.listing.status === 'out' ? 'आ चुका है — अभी चेक करें' : `${dt(ipo.dates.allotment)} को आने की उम्मीद है`}। रजिस्ट्रार है ${iss.registrar}।
10 सेकंड में ऐसे चेक करें: ${this.steps.join('। ')}।
लिस्टिंग ${dt(ipo.dates.listing)} को, संभावित रेंज ₹${listingRange.low} से ₹${listingRange.high} — यानी इश्यू प्राइस पर ${this.signed(listingRange.low_pct, 0)}% से ${this.signed(listingRange.high_pct, 0)}%।
अलॉटमेंट अलर्ट के लिए फॉलो करें।`,

`${ipo.company} అలాట్‌మెంట్ ${d.listing.status === 'out' ? 'వచ్చేసింది — ఇప్పుడే చెక్ చేయండి' : `${dt(ipo.dates.allotment)}న వస్తుందని అంచనా`}. రిజిస్ట్రార్ ${iss.registrar}.
10 సెకన్లలో ఇలా చెక్ చేయండి: ${this.steps.join('. ')}.
లిస్టింగ్ ${dt(ipo.dates.listing)}న, అంచనా రేంజ్ ₹${listingRange.low} నుంచి ₹${listingRange.high} — అంటే ఇష్యూ ధరపై ${this.signed(listingRange.low_pct, 0)}% నుంచి ${this.signed(listingRange.high_pct, 0)}%.
అలాట్‌మెంట్ అలర్ట్ కోసం ఫాలో అవ్వండి.`,
      ],
    };
    return (S[reelNumber] || [''])[i].replace(/\n{2,}/g, '\n').trim();
  },

  get script() { return this.scriptFor(this.reel.n); },

  /* ── YouTube packaging ────────────────────────────────────────────────
   *
   * Title, description, hashtags and a thumbnail brief — the four things
   * that decide whether a video is watched, none of which the reel itself
   * carries. All of them per language, because they are not translations of
   * each other: a Hindi viewer searching for this video types different
   * words than an English one, and a hashtag set that works in one reads as
   * spam in the other.
   *
   * The company's legal suffix is dropped throughout. "Lalithaa Jewellery
   * Mart Limited IPO GMP Today" spends nine characters of a ~60-character
   * search display on the word "Limited", which nobody searches for.
   */

  get voShortName() {
    return String(this.ipo?.company || '')
      .replace(/\s*\b(Limited|Ltd\.?|Private|Pvt\.?|Corporation|Corp\.?)\b/gi, '')
      .replace(/\s{2,}/g, ' ').trim();
  },

  /* A hashtag needs to be one token; a company name is not. */
  get voTagName() {
    return String(this.voShortName).replace(/[^A-Za-z0-9]/g, '');
  },

  /* Three title variants per reel: the search-led one first (front-loads the
   * words people actually type), then a curiosity-led one, then a short one
   * for Shorts where the title is truncated hard. Pick per upload, or A/B. */
  get ytTitles() {
    if (!this.ipo || !this.d) return [];
    const n = this.reel.n, name = this.voShortName, d = this.d;
    const g = d.gmp, s = d.subscription;
    const lang = this.lang;
    const gmpTxt = g.has_data ? `₹${g.gmp}` : '';
    const pctTxt = g.has_data ? `${g.pct}%` : '';
    const score = Number(d.score.effective || 0).toFixed(1);

    // Finance vocabulary stays in English in all three languages, because
    // that is how it is searched — a Hindi speaker types "IPO GMP today",
    // not a Devanagari transliteration of it. Only the framing changes.
    const T = {
      en: {
        1: [`${name} IPO Review | Price Band, Lot Size & Full Details`,
            `${name} IPO — Everything You Need To Know Before You Apply`,
            `${name} IPO Full Details`],
        2: [`${name} IPO GMP Today ${gmpTxt} | ${pctTxt} Listing Gain?`,
            `${name} IPO GMP ${gmpTxt} — Is The Grey Market Right?`,
            `${name} IPO GMP Today`],
        3: [`${name} IPO Subscription Day ${s.day} | ${s.total}x Subscribed`,
            `${name} IPO Subscription Status — Should You Wait?`,
            `${name} IPO Subscription Day ${s.day}`],
        4: [`${name} IPO — Apply Or Not? Honest Analysis`,
            `${name} IPO Review | Financials, Valuation & Red Flags`,
            `${name} IPO — Apply Or Skip?`],
        5: [`${name} IPO Final Verdict | Score ${score}/10`,
            `${name} IPO — My Final Take Before It Closes`,
            `${name} IPO Verdict`],
        6: [`${name} IPO Allotment Status | How To Check In 10 Seconds`,
            `${name} IPO Allotment & Listing — What To Expect`,
            `${name} IPO Allotment Status`],
      },
      hi: {
        1: [`${name} IPO पूरी जानकारी | Price Band, Lot Size, Details`,
            `${name} IPO — Apply करने से पहले ये जान लीजिए`,
            `${name} IPO पूरी जानकारी`],
        2: [`${name} IPO GMP Today ${gmpTxt} | ${pctTxt} Listing Gain?`,
            `${name} IPO GMP ${gmpTxt} — क्या Grey Market सही है?`,
            `${name} IPO GMP आज`],
        3: [`${name} IPO Subscription Day ${s.day} | ${s.total}x Subscribe`,
            `${name} IPO Subscription — क्या Last Day तक रुकें?`,
            `${name} IPO Subscription Day ${s.day}`],
        4: [`${name} IPO — Apply करें या नहीं? पूरा Analysis`,
            `${name} IPO Review | Financials, Valuation और Red Flags`,
            `${name} IPO — Apply या Skip?`],
        5: [`${name} IPO Final Verdict | Score ${score}/10`,
            `${name} IPO — Closing से पहले मेरी आखिरी राय`,
            `${name} IPO Verdict`],
        6: [`${name} IPO Allotment Status कैसे Check करें | 10 Second`,
            `${name} IPO Allotment और Listing — क्या उम्मीद रखें`,
            `${name} IPO Allotment Status`],
      },
      // Telugu titles are written in LATIN script with "in Telugu" appended
      // as a keyword, which looks wrong and is not. Every Telugu IPO channel
      // that actually ranks does it this way — "Timescan Logistics IPO Review
      // In Telugu", "nykaa ipo gmp in telugu" — and Telugu script is
      // essentially absent from their titles. The audience types the query in
      // Latin, so a beautifully localised తెలుగు title is one nobody searches
      // for. The spoken script stays Telugu; only the packaging is Latin.
      te: {
        1: [`${name} IPO Review in Telugu | ${name} IPO Details Telugu | Price Band, Lot Size`,
            `${name} IPO Full Details in Telugu | Apply Cheyyala Redda?`,
            `${name} IPO Details in Telugu`],
        2: [`${name} IPO GMP Today in Telugu ${gmpTxt} | ${pctTxt} Listing Gain?`,
            `${name} IPO GMP in Telugu | Grey Market Premium Today`,
            `${name} IPO GMP Today Telugu`],
        3: [`${name} IPO Subscription Day ${s.day} in Telugu | ${s.total}x Subscribed`,
            `${name} IPO Subscription Status in Telugu | Last Day Varaku Aagala?`,
            `${name} IPO Subscription Telugu`],
        4: [`${name} IPO Apply or Not in Telugu | ${name} IPO Analysis Telugu`,
            `${name} IPO Review in Telugu | Financials, Valuation, Red Flags`,
            `${name} IPO Apply or Not Telugu`],
        5: [`${name} IPO Final Verdict in Telugu | Score ${score}/10`,
            `${name} IPO Review Telugu | Apply Cheyyala Vadda?`,
            `${name} IPO Verdict Telugu`],
        6: [`${name} IPO Allotment Status in Telugu | How to Check`,
            `${name} IPO Allotment & Listing in Telugu | Expected Listing Price`,
            `${name} IPO Allotment Status Telugu`],
      },
    };
    const set = (T[lang] || T.en)[n] || [];
    // A GMP-less or subscription-less IPO would otherwise title itself
    // "GMP Today ₹ | %" or "Day 0 | 0x".
    return set.filter((t) => !/₹\s*\||\s\|\s%|Day\s0\b|\b0x\b|\s{2,}\|/.test(t))
      .map((t) => t.replace(/\s+\|\s*$/, '').replace(/\s{2,}/g, ' ').trim());
  },

  get ytTitle() { return this.ytTitles[0] || ''; },

  /* Hashtags, kept few on purpose.
   *
   * YouTube's documented limit is 60 across title and description together,
   * past which it ignores every one of them — so the cap is not the
   * constraint people assume. The real reason to stay small is that
   * hashtags link to a browse page and are not a documented search-ranking
   * signal; the description TEXT is what search matches against. YouTube
   * also picks which three appear above the title itself, by "most
   * engaging", so stuffing the list buys nothing and reads as spam.
   *
   * Six well-aimed tags, therefore, not thirty. */
  get ytHashtags() {
    if (!this.ipo) return [];
    const tag = this.voTagName;
    const byLang = {
      // Latin script across all three. Indian finance search runs on English
      // terms even among Hindi and Telugu speakers — the language tag is what
      // carries the audience signal, not a translated noun.
      en: ['#IPOReview', '#StockMarketIndia'],
      hi: ['#IPOHindi', '#ShareMarketHindi'],
      te: ['#IPOTelugu', '#StockMarketTelugu'],
    };
    return ['#IPO', '#IPOGMP', tag ? `#${tag}IPO` : '',
            ...(byLang[this.lang] || byLang.en)].filter(Boolean);
  },

  /* The legal line. Not boilerplate to skim past: in India, publishing a
   * specific "apply / avoid" call on a named security is the activity SEBI
   * regulates, and this channel's whole output is exactly that shape. This
   * wording frames the video as education and opinion and says plainly that
   * it is not advice — which is the minimum, not a shield. See
   * docs/YOUTUBE-PLAYBOOK.md, which explains why the framing of the video
   * matters more than the disclaimer under it. */
  get ytDisclaimer() {
    const D = {
      en: `DISCLAIMER: This video is for education and information only. It is NOT investment advice and NOT a recommendation to buy or sell any security. I am not a SEBI-registered investment adviser or research analyst. Grey Market Premium (GMP) is unofficial, unregulated data from the informal market — it is not published by NSE, BSE or SEBI, it cannot be verified, and it changes daily. Do not make an investment decision based on GMP. Investments in securities are subject to market risks; read all offer documents carefully. Please consult a SEBI-registered adviser before investing. I hold no position in this IPO unless stated.`,
      hi: `डिस्क्लेमर: यह वीडियो केवल शिक्षा और जानकारी के लिए है। यह निवेश सलाह नहीं है और किसी भी शेयर को खरीदने या बेचने की सिफारिश नहीं है। मैं SEBI-पंजीकृत निवेश सलाहकार या रिसर्च एनालिस्ट नहीं हूँ। GMP (ग्रे मार्केट प्रीमियम) अनऑफिशियल और अनियमित डेटा है — इसे NSE, BSE या SEBI प्रकाशित नहीं करते, इसे सत्यापित नहीं किया जा सकता, और यह रोज़ बदलता है। GMP के आधार पर निवेश का फैसला न लें। शेयर बाज़ार में निवेश जोखिमों के अधीन है; सभी दस्तावेज़ ध्यान से पढ़ें। निवेश से पहले SEBI-पंजीकृत सलाहकार से सलाह लें।`,
      te: `డిస్‌క్లెయిమర్: ఈ వీడియో కేవలం విద్య మరియు సమాచారం కోసం మాత్రమే. ఇది పెట్టుబడి సలహా కాదు, ఏ షేర్‌ను కొనమని లేదా అమ్మమని సిఫారసు కాదు. నేను SEBI-రిజిస్టర్డ్ ఇన్వెస్ట్‌మెంట్ అడ్వైజర్ లేదా రీసెర్చ్ అనలిస్ట్ కాదు. GMP (గ్రే మార్కెట్ ప్రీమియం) అనధికారిక, నియంత్రణ లేని డేటా — దీన్ని NSE, BSE లేదా SEBI ప్రచురించవు, ధృవీకరించలేము, ప్రతిరోజూ మారుతుంది. GMP ఆధారంగా పెట్టుబడి నిర్ణయం తీసుకోవద్దు. మార్కెట్ పెట్టుబడులు రిస్క్‌కు లోబడి ఉంటాయి; అన్ని పత్రాలను జాగ్రత్తగా చదవండి. పెట్టుబడికి ముందు SEBI-రిజిస్టర్డ్ సలహాదారుని సంప్రదించండి.`,
    };
    return D[this.lang] || D.en;
  },

  get ytDescription() {
    if (!this.ipo || !this.d) return '';
    const ipo = this.ipo, d = this.d, iss = ipo.issue, g = d.gmp, s = d.subscription;
    const name = this.voShortName;
    const dt = (x) => this.fmtDate(x, true);
    const H = {
      en: { facts: 'KEY DETAILS', band: 'Price band', lot: 'Lot size', size: 'Issue size',
            win: 'Open / Close', list: 'Listing', gmp: 'GMP today', sub: 'Subscription',
            chapters: 'CHAPTERS', more: 'Daily IPO updates — subscribe for the allotment alert.' },
      hi: { facts: 'ज़रूरी जानकारी', band: 'Price Band', lot: 'Lot Size', size: 'Issue Size',
            win: 'Open / Close', list: 'Listing', gmp: 'आज का GMP', sub: 'Subscription',
            chapters: 'CHAPTERS', more: 'रोज़ाना IPO अपडेट — allotment alert के लिए subscribe करें।' },
      te: { facts: 'ముఖ్య వివరాలు', band: 'Price Band', lot: 'Lot Size', size: 'Issue Size',
            win: 'Open / Close', list: 'Listing', gmp: 'నేటి GMP', sub: 'Subscription',
            chapters: 'CHAPTERS', more: 'రోజువారీ IPO అప్‌డేట్‌లు — allotment alert కోసం subscribe చేయండి.' },
    };
    const L = H[this.lang] || H.en;

    // The first two lines are the only ones shown before "...more", so the
    // number that makes someone click has to live there.
    const hook = g.has_data
      ? `${name} IPO — GMP ₹${g.gmp} (${g.pct}%), estimated listing ₹${this.fmt(g.est_listing)}.`
      : `${name} IPO — full review, terms and dates.`;
    const second = s.has_data
      ? `Subscribed ${s.total}x on day ${s.day}. Closes ${dt(ipo.dates.close)}.`
      : `Opens ${dt(ipo.dates.open)}, closes ${dt(ipo.dates.close)}.`;

    const facts = [
      `${L.band}: ₹${iss.price_low} – ₹${iss.price_high}`,
      iss.lot_size ? `${L.lot}: ${iss.lot_size} shares (₹${this.fmt(d.issue.min_investment)} minimum)` : '',
      d.issue.total_cr ? `${L.size}: ₹${this.fmt(d.issue.total_cr)} crore` : '',
      `${L.win}: ${dt(ipo.dates.open)} – ${dt(ipo.dates.close)}`,
      `${L.list}: ${dt(ipo.dates.listing)}`,
      g.has_data ? `${L.gmp}: ₹${g.gmp} (${g.pct}%)` : '',
      s.has_data ? `${L.sub}: ${s.total}x (day ${s.day})` : '',
      iss.registrar ? `Registrar: ${iss.registrar}` : '',
    ].filter(Boolean);

    return [
      hook, second, '',
      `📊 ${L.facts}`, ...facts, '',
      `⏱️ ${L.chapters}`,
      '00:00 — (fill in after editing)', '',
      L.more, '',
      `⚠️ ${this.ytDisclaimer}`, '',
      this.ytHashtags.join(' '),
    ].join('\n');
  },

  /* Kept: the Copy caption button and anything else still calling it. */
  get caption() {
    if (!this.ipo || !this.d) return '';
    return `TITLE:\n${this.ytTitles.join('\n')}\n\nDESCRIPTION:\n${this.ytDescription}`;
  },

  /* ── thumbnail brief for an image model ───────────────────────────────
   *
   * A prompt to paste into Gemini's image model (the one nicknamed Nano
   * Banana), not an image — a static page cannot hold an API key, and this
   * is the one step where a human should look at the result anyway.
   *
   * Built around what actually earns a click on a phone: one number, three
   * words, and a colour that tells you the direction before you have read
   * anything. The number is pulled from the data so the thumbnail cannot
   * promise something the video does not say — a thumbnail that overstates
   * the premium is both a YouTube policy problem and, for financial
   * content, a much worse one.
   */
  get thumbnailPrompt() {
    if (!this.ipo || !this.d) return '';
    const n = this.reel.n;
    const name = this.voShortName.toUpperCase();

    /* The hook is the QUESTION, never the answer.
     *
     * This used to lead with the figure — a giant "₹32" for the GMP, "8.4/10"
     * for the verdict — on the theory that one number earns the click. It does
     * the opposite. A viewer who can read the premium off the thumbnail has
     * already been told the thing the video exists to tell them, and scrolls
     * on. The number is the payoff; spending it in the thumbnail leaves the
     * video with nothing to pay.
     *
     * Checked against a channel doing this well (@TheIPOPulseIndia, 76 shorts,
     * same beat, same language mix): not one thumbnail carries a number, a
     * percentage or a date. Every one is the company name, an urgency banner,
     * and three labelled chips naming the questions answered — "LATEST GMP",
     * "ALLOTMENT CHANCES", "LISTING EXPECTATIONS". The label sells the answer
     * without giving it away. Their titles follow the identical rule.
     *
     * Dates are excluded for a second reason on top of that one: a thumbnail
     * is permanent and a date is not. "3 SEP" is wrong from the 4th onward and
     * turns an evergreen asset into a stale one. Relative words — TODAY,
     * TOMORROW, FINAL DAY — carry the same urgency and never expire.
     */
    const HOOKS = {
      1: { banner: 'ISSUE DETAILS',      chips: ['PRICE BAND', 'LOT SIZE', 'ISSUE DATES'] },
      2: { banner: 'LATEST GMP',         chips: ['TODAY’S GMP', 'LISTING ESTIMATE', 'GAIN PER LOT'] },
      3: { banner: 'SUBSCRIPTION UPDATE',chips: ['QIB · NII · RETAIL', 'DEMAND TREND', 'FINAL DAY NUMBERS'] },
      4: { banner: 'APPLY OR SKIP?',     chips: ['FINANCIALS', 'VALUATION', 'RED FLAGS'] },
      5: { banner: 'FINAL VERDICT',      chips: ['LATEST GMP', 'ALLOTMENT CHANCES', 'LISTING EXPECTATIONS'] },
      6: { banner: 'ALLOTMENT OUT',      chips: ['HOW TO CHECK', 'ALLOTMENT CHANCES', 'LISTING DATE'] },
      7: { banner: 'MARKET TODAY',       chips: ['TOP 5 NEWS', 'SECTORS IN FOCUS', 'LEVELS TO WATCH'] },
    };
    const hook = HOOKS[n] || HOOKS[1];

    /* The urgency line, derived from where the issue actually is in its own
     * calendar. Relative words only — see above. */
    const status = this.d.dates.status;
    const URGENCY = {
      upcoming:  'OPENS SOON',
      open:      'OPEN NOW',
      closed:    'CLOSES TODAY',
      allotment: 'ALLOTMENT EXPECTED TODAY',
      listed:    'LISTING DAY',
    };
    const urgency = URGENCY[status] || hook.banner;

    return [
`Create a vertical 9:16 YouTube Shorts thumbnail for an Indian IPO channel. Premium financial-media style: rich, high-contrast, designed to be legible as a small tile on a phone.`,
``,
`Background: a deep navy blue gradient with a large soft glowing emerald-green upward arrow behind everything, a faint candlestick-chart texture, and a modern glass office tower photographed from below on the right third. Add restrained metallic gold accents. Cinematic and corporate, not cartoonish.`,
``,
`Render this text EXACTLY as written and nothing else:`,
`  · "${name}" across the top, very large, heavy condensed uppercase, in two tones — white for the first word and metallic gold for the rest`,
`  · directly beneath it the word "IPO", boxed`,
`  · below that, a navy banner with a thin gold border reading "${urgency}", in white and gold uppercase`,
`  · down the left side, three white rounded pill-shaped chips stacked vertically, each with a small circular colour icon on its left — a blue chart icon, a green target icon, an orange calendar icon — reading, top to bottom:`,
`      "${hook.chips[0]}"`,
`      "${hook.chips[1]}"`,
`      "${hook.chips[2]}"`,
``,
`HARD CONSTRAINTS — these are the point of the design:`,
`  · Do NOT put any number, digit, price, percentage, multiple, rupee figure or date anywhere in the image. Not in the banner, not in the chips, not in the background. The chips name the questions the video answers; they must never show the answers.`,
`  · No calendar dates and no month names. Urgency is expressed only by the relative words already given.`,
`  · No words implying a promise: avoid "guaranteed", "sure shot", "profit", "multibagger", "jackpot".`,
`  · Keep every element inside the middle 80% of the frame — Shorts overlays the bottom third with the title, channel name and action buttons, so leave that area quiet.`,
`  · Set all text in a heavy geometric sans-serif with a dark outline so it survives being shrunk to 160px wide.`,
``,
`— Set 9:16 with the model’s aspect-ratio control, not in the prompt text; it defaults to square. Attach the previous thumbnail as a reference image so the palette, type weight and chip positions stay identical across uploads — a channel is recognised by its thumbnails before its name.`,
      ].join('\n');
  },

  /* Everything needed to publish one video, in one copy. */
  get packaging() {
    if (!this.ipo || !this.d) return '';
    const bar = (s) => `\n${'─'.repeat(58)}\n${s}\n${'─'.repeat(58)}`;
    return [
`IPO PULSE — publishing pack`,
`${this.ipo.company} · reel ${this.reel.n} (${this.t(this.reel.key)}) · ${this.lang.toUpperCase()}`,
bar('TITLE — pick one'),
this.ytTitles.map((t, i) => `${i + 1}. ${t}   [${t.length} chars]`).join('\n'),
bar('DESCRIPTION'),
this.ytDescription,
bar('HASHTAGS (first 3 show above the title)'),
this.ytHashtags.join(' '),
bar('THUMBNAIL PROMPT — paste into the Gemini image model'),
this.thumbnailPrompt,
bar('VOICEOVER SCRIPT'),
this.script,
    ].join('\n');
  },

  // ── CSV report ───────────────────────────────────────────────────────
  /**
   * Everything about this IPO in one file, opens straight in Excel.
   * (For the formatted multi-sheet .xlsx, run `ipopulse report <slug>` —
   * a real workbook needs a library this static page deliberately avoids.)
   */
  buildCsv() {
    const ipo = this.ipo, d = this.d;
    const rows = [];
    const esc = (v) => {
      const s = v === null || v === undefined ? '' : String(v);
      return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
    };
    const push = (...cells) => rows.push(cells.map(esc).join(','));
    const section = (name) => { rows.push(''); push(`== ${name} ==`); };

    push('IPO PULSE REPORT', ipo.company);
    push('Generated', new Date().toISOString().slice(0, 16).replace('T', ' '));

    section('SUMMARY');
    push('Company', ipo.company); push('Board', ipo.board); push('Sector', ipo.sector);
    push('Status', d.dates.status);
    push('Issue size (Cr)', d.issue.total_cr);
    push('Fresh issue (Cr)', ipo.issue.fresh_cr); push('Fresh %', d.issue.fresh_pct);
    push('OFS (Cr)', ipo.issue.ofs_cr); push('OFS %', d.issue.ofs_pct);
    push('Price band', `${ipo.issue.price_low} - ${ipo.issue.price_high}`);
    push('Lot size', ipo.issue.lot_size);
    push('Min investment', d.issue.min_investment);
    push('Registrar', ipo.issue.registrar); push('Registrar URL', ipo.issue.registrar_url);

    section('DATES');
    ['announced', 'open', 'close', 'allotment', 'refund', 'listing']
      .forEach((k) => push(k, ipo.dates[k] || ''));

    section('GMP');
    push('Latest GMP', d.gmp.gmp); push('GMP %', d.gmp.pct);
    push('Est. listing', d.gmp.est_listing); push('Gain per lot', d.gmp.gain_per_lot);
    push('Movement', d.gmp.movement); push('Peak', d.gmp.peak); push('Low', d.gmp.trough);
    push('Days tracked', d.gmp.days_tracked);

    // `Profit / lot` and `Sauda` are here because the trail scene shows both,
    // and a report that omits a column the card displays reads as a
    // disagreement between them rather than as a shorter report.
    section('GMP HISTORY (announcement to listing)');
    push('Date', 'GMP', 'GMP %', 'Est. listing', 'Profit / lot', 'Kostak', 'Sauda', 'Source');
    d.gmp.series.forEach((p) =>
      push(p.date, p.gmp, p.pct, p.est, p.per_lot, p.kostak, p.sauda, p.source));

    section('SUBSCRIPTION');
    push('Day', 'Date', 'QIB', 'NII/HNI', 'Retail', 'Employee', 'Total');
    (d.subscription.days || []).forEach((s) =>
      push(s.day, s.date, s.qib, s.nii, s.retail, s.employee, s.total));

    section('FINANCIALS (Rs Cr)');
    push('Year', 'Revenue', 'EBITDA', 'EBITDA %', 'PAT', 'PAT %', 'Net worth',
         'Total debt', 'RoNW %', 'Debt/Equity');
    (d.financials.rows || []).forEach((r) => push(r.year, r.revenue, r.ebitda,
      r.ebitda_margin, r.pat, r.pat_margin, r.net_worth, r.total_debt, r.ronw, r.debt_equity));
    if (d.financials.has_data) {
      push('Revenue CAGR %', d.financials.revenue_cagr);
      push('EBITDA CAGR %', d.financials.ebitda_cagr);
      push('PAT CAGR %', d.financials.pat_cagr);
      push('EBITDA margin shift (bps)', d.financials.margin_shift_bps);
      push('EPS', d.financials.eps);
      push('P/E at upper band', d.financials.pe);
      push('Peer average P/E', d.financials.pe_peer_avg);
      push('Premium to peers %', d.financials.pe_premium_pct);
      push('Market cap (Cr)', d.financials.market_cap_cr);
    }

    section('ANALYSIS');
    push('Growth', this.loc.growth); push('Valuation', this.loc.valuation);
    push('Key risk', this.loc.risk);
    this.loc.overview.forEach((x, n) => push(`Overview ${n + 1}`, x));
    this.loc.green_flags.forEach((x, n) => push(`Green flag ${n + 1}`, x));
    this.loc.red_flags.forEach((x, n) => push(`Red flag ${n + 1}`, x));
    push('Score', Number(d.score.effective).toFixed(1)); push('Verdict', this.verdictText);
    push('Retail', ipo.analysis.reco_retail); push('HNI', ipo.analysis.reco_hni);
    push('Long term', ipo.analysis.reco_long);

    section('ALL TRACKED IPOs');
    push('Company', 'Status', 'Price band', 'Lot', 'GMP', 'GMP %', 'Est. listing', 'Sub (x)', 'Closes');
    this.boardRows.forEach((r) => push(r.company, r.status,
      `${r.price_low} - ${r.price_high}`, r.lot_size, r.gmp, r.gmp_pct,
      r.est_listing, r.subscription ?? '', r.close || ''));

    rows.push('');
    push('GMP is unofficial grey-market data. Not investment advice.');
    return rows.join('\r\n');
  },

  downloadCsv() {
    // BOM so Excel reads the rupee sign and Devanagari/Telugu correctly
    const blob = new Blob(['﻿' + this.buildCsv()], { type: 'text/csv;charset=utf-8;' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `ipo-pulse-${this.slugify(this.ipo.company)}.csv`;
    a.click();
    URL.revokeObjectURL(a.href);
  },

  // ── PNG export ───────────────────────────────────────────────────────
  /**
   * html2canvas serialises <svg> to XML and loads it as an image. Alpine
   * leaves its `:attr` bindings on the element, and a bare leading colon is an
   * invalid XML namespace prefix — the image then fails silently and every
   * ring and sparkline vanishes from the PNG. Strip them for the capture only.
   */
  stripBindings(root) {
    const undo = [];
    root.querySelectorAll('svg').forEach((svg) => {
      [svg, ...svg.querySelectorAll('*')].forEach((el) => {
        [...el.attributes].forEach((a) => {
          if (a.name.startsWith(':') || a.name.startsWith('@')) {
            undo.push([el, a.name, a.value]);
            el.removeAttribute(a.name);
          }
        });
      });
    });
    return () => undo.forEach(([el, n, v]) => el.setAttribute(n, v));
  },

  /**
   * Would drawing this URL onto a canvas taint it?
   *
   * A cross-origin image the host does not grant CORS to poisons the canvas,
   * and `toDataURL` then throws SecurityError — so one pinned sticker from a
   * host without `Access-Control-Allow-Origin` would take down every export
   * with an error naming neither the sticker nor the host. Asked here instead,
   * by loading the same URL with crossOrigin set: if that succeeds the host
   * allows it and html2canvas is safe; if it fails the sticker is dropped from
   * the capture and the PNG still comes out.
   *
   * Cached per URL — this runs on every export and the answer cannot change
   * for a given URL within a session.
   */
  async gifWouldTaint(url) {
    if (!url) return false;
    this._taintCache = this._taintCache || {};
    if (url in this._taintCache) return this._taintCache[url];
    const ok = await new Promise((resolve) => {
      const probe = new Image();
      probe.crossOrigin = 'anonymous';
      probe.onload = () => resolve(true);
      probe.onerror = () => resolve(false);
      probe.src = url;
      // Never let a dead host hold the export open.
      setTimeout(() => resolve(false), 4000);
    });
    this._taintCache[url] = !ok;
    return !ok;
  },

  async shoot() {
    if (typeof html2canvas === 'undefined') {
      alert('html2canvas did not load (offline?).\nUse Win+Shift+S to screenshot instead.');
      return null;
    }
    const node = document.getElementById('capture');
    const keepScale = this.scale, keepSafe = this.showSafe;
    this.gifTaints = await this.gifWouldTaint(this.brandGif);
    this.scale = 1; this.showSafe = false;
    this.settle();                       // never capture a half-counted number
    node.classList.add('capturing');     // freeze transitions mid-flight
    await new Promise((r) => setTimeout(r, 200));
    const restore = this.stripBindings(node);
    let canvas = null;
    try {
      canvas = await html2canvas(node, {
        // Follows the theme. Hardcoding the Midnight navy here drew a dark
        // ring inside the rounded corners of every other theme, because this
        // colour is what shows through wherever the card's own radius clips.
        scale: this.P.exp, backgroundColor: this.th.bg,
        useCORS: true, logging: false, width: this.P.w, height: this.P.h,
      });
    } catch (err) {
      alert(`PNG export failed: ${err.message}\nUse Win+Shift+S instead.`);
    } finally {
      restore();
      node.classList.remove('capturing');
      this.scale = keepScale; this.showSafe = keepSafe;
      this.gifTaints = false;         // preview shows it again
    }
    return canvas;
  },

  async exportScene() {
    const canvas = await this.shoot();
    if (!canvas) return;
    const a = document.createElement('a');
    a.download = `ipopulse_${this.slugify(this.ipo.company)}_r${this.reel.n}` +
                 `-${this.sceneId}_${this.P.short.replace(':', 'x')}.png`;
    a.href = canvas.toDataURL('image/png');
    a.click();
  },

  /** Every scene of the current reel — one video's worth of stills. */
  async exportReel() {
    const start = this.scene;
    for (let i = 0; i < this.sceneCount; i++) {
      this.go(this.reelIndex, i);
      await new Promise((r) => setTimeout(r, 900));
      await this.exportScene();
      await new Promise((r) => setTimeout(r, 250));
    }
    this.go(this.reelIndex, start);
  },

  async copyPng() {
    const canvas = await this.shoot();
    if (!canvas) return;
    canvas.toBlob((blob) => {
      if (!navigator.clipboard || !window.ClipboardItem) {
        alert('This browser cannot copy images. Use Export PNG.');
        return;
      }
      navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })])
        .then(() => { this.copied = 'png'; setTimeout(() => { this.copied = ''; }, 1600); })
        .catch(() => alert('Clipboard blocked. Use Export PNG.'));
    });
  },
};
