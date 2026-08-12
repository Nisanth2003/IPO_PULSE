/* Outputs: voiceover scripts, captions, CSV report, PNG export.
 *
 * Mixed into the Alpine component (see `...OUTPUT` in studio.js) so these can
 * use `this` for state. Kept separate because none of it draws anything — it
 * all turns the current state into something you take away.
 */

const OUTPUT = {

  // ── voiceover script, one per reel ───────────────────────────────────
  scriptFor(reelNumber) {
    if (!this.ipo || !this.d) return '';
    const i = LANG_INDEX[this.lang] ?? 0;
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
Out of the ₹${f(d.issue.total_cr)} crore issue, ₹${f(iss.fresh_cr)} crore is a FRESH ISSUE that goes into the company's growth, and ₹${f(iss.ofs_cr)} crore is an OFS — promoters cashing out. That's ${d.issue.fresh_pct}% fresh.
Opens ${dt(ipo.dates.open)}, closes ${dt(ipo.dates.close)}, lists ${dt(ipo.dates.listing)}.`,

`${ipo.company} का IPO। प्राइस बैंड ₹${iss.price_low} से ₹${iss.price_high}, लॉट साइज़ ${iss.lot_size} शेयर — कम से कम ₹${f(d.issue.min_investment)}।
${L.overview.join('। ')}।
₹${f(d.issue.total_cr)} करोड़ के इश्यू में ₹${f(iss.fresh_cr)} करोड़ फ्रेश इश्यू है जो कंपनी की ग्रोथ में जाएगा, और ₹${f(iss.ofs_cr)} करोड़ OFS है — यानी प्रमोटर पैसा निकाल रहे हैं। यानी ${d.issue.fresh_pct}% फ्रेश।
${dt(ipo.dates.open)} को खुलेगा, ${dt(ipo.dates.close)} को बंद, ${dt(ipo.dates.listing)} को लिस्टिंग।`,

`${ipo.company} IPO. ప్రైస్ బ్యాండ్ ₹${iss.price_low} నుంచి ₹${iss.price_high}, లాట్ సైజ్ ${iss.lot_size} షేర్లు — కనీసం ₹${f(d.issue.min_investment)}.
${L.overview.join('. ')}.
₹${f(d.issue.total_cr)} కోట్ల ఇష్యూలో ₹${f(iss.fresh_cr)} కోట్లు ఫ్రెష్ ఇష్యూ — కంపెనీ గ్రోత్‌కి. ₹${f(iss.ofs_cr)} కోట్లు OFS — ప్రమోటర్లు డబ్బు తీసుకుంటున్నారు. అంటే ${d.issue.fresh_pct}% ఫ్రెష్.
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
Retail — ${this.t('r_' + ipo.analysis.reco_retail)}. HNI — ${this.t('r_' + ipo.analysis.reco_hni)}. Long term — ${this.t('r_' + ipo.analysis.reco_long)}.
The issue closes ${dt(ipo.dates.close)} at ${ipo.dates.close_time}. Apply before the cut-off.
This is not investment advice — do your own research.`,

`${ipo.company} पर फाइनल फैसला। IPO पल्स स्कोर: 10 में से ${Number(d.score.effective).toFixed(1)}। ${this.verdictText}।
रिटेल — ${this.t('r_' + ipo.analysis.reco_retail)}। HNI — ${this.t('r_' + ipo.analysis.reco_hni)}। लॉन्ग टर्म — ${this.t('r_' + ipo.analysis.reco_long)}।
इश्यू ${dt(ipo.dates.close)} को ${ipo.dates.close_time} बजे बंद होगा। कट-ऑफ से पहले अप्लाई करें।
यह निवेश सलाह नहीं है — खुद रिसर्च करें।`,

`${ipo.company}పై తుది తీర్పు. IPO పల్స్ స్కోర్: 10కి ${Number(d.score.effective).toFixed(1)}. ${this.verdictText}.
రిటైల్ — ${this.t('r_' + ipo.analysis.reco_retail)}. HNI — ${this.t('r_' + ipo.analysis.reco_hni)}. లాంగ్ టర్మ్ — ${this.t('r_' + ipo.analysis.reco_long)}.
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

  get caption() {
    if (!this.ipo || !this.d) return '';
    const ipo = this.ipo, d = this.d;
    const topic = this.t(this.reel.key);
    const title = `${ipo.company} IPO — ${topic} | GMP ₹${d.gmp.gmp} (${d.gmp.pct}%)`;
    const tags = ['#IPO', '#IPOPulse', `#${this.slugify(ipo.company).replace(/-/g, '')}IPO`,
      '#GMP', '#GreyMarketPremium', '#StockMarketIndia', '#IPOAlert', '#ShareMarket',
      '#Investing', '#IPOReview', '#Allotment', '#Nifty'];
    return `TITLE:\n${title}\n\nDESCRIPTION:\n${ipo.company} IPO — price band ₹${ipo.issue.price_low}–₹${ipo.issue.price_high}, lot ${ipo.issue.lot_size} shares (₹${this.fmt(d.issue.min_investment)}). Today's GMP ₹${d.gmp.gmp} (${d.gmp.pct}%), estimated listing ₹${this.fmt(d.gmp.est_listing)}.${d.subscription.has_data ? ` Subscribed ${d.subscription.total}x on day ${d.subscription.day}.` : ''} Verdict: ${this.verdictText}. Closes ${this.fmtDate(ipo.dates.close, true)}, lists ${this.fmtDate(ipo.dates.listing, true)}.\n\nGMP is unofficial grey-market data. Not investment advice — do your own research or speak to a SEBI-registered adviser.\n\n${tags.join(' ')}`;
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

    section('GMP HISTORY (announcement to listing)');
    push('Date', 'GMP', 'GMP %', 'Est. listing', 'Kostak', 'Source');
    d.gmp.series.forEach((p) => push(p.date, p.gmp, p.pct, p.est, p.kostak, p.source));

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

  async shoot() {
    if (typeof html2canvas === 'undefined') {
      alert('html2canvas did not load (offline?).\nUse Win+Shift+S to screenshot instead.');
      return null;
    }
    const node = document.getElementById('capture');
    const keepScale = this.scale, keepSafe = this.showSafe;
    this.scale = 1; this.showSafe = false;
    this.settle();                       // never capture a half-counted number
    node.classList.add('capturing');     // freeze transitions mid-flight
    await new Promise((r) => setTimeout(r, 200));
    const restore = this.stripBindings(node);
    let canvas = null;
    try {
      canvas = await html2canvas(node, {
        scale: this.P.exp, backgroundColor: '#0B1120',
        useCORS: true, logging: false, width: this.P.w, height: this.P.h,
      });
    } catch (err) {
      alert(`PNG export failed: ${err.message}\nUse Win+Shift+S instead.`);
    } finally {
      restore();
      node.classList.remove('capturing');
      this.scale = keepScale; this.showSafe = keepSafe;
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
