/* UI labels in English / Hindi / Telugu.
 *
 * This file holds only *chrome* — field names, units, verdicts. The IPO's own
 * prose (business overview, green/red flags, risk) is translated by Gemini in
 * the backend and arrives inside the published JSON under `ipo.i18n`, so a
 * company's copy reads naturally rather than being word-swapped in the browser.
 */

const LANG_INDEX = { en: 0, hi: 1, te: 2 };

const LABELS = {
  live:        ['Live Updates', 'लाइव अपडेट', 'లైవ్ అప్‌డేట్స్'],

  // reels
  reel1:       ['About IPO', 'IPO की जानकारी', 'IPO వివరాలు'],
  reel2:       ['Daily GMP', 'आज का GMP', 'నేటి GMP'],
  reel3:       ['Subscription', 'सब्सक्रिप्शन', 'సబ్‌స్క్రిప్షన్'],
  reel4:       ['Apply or Skip?', 'अप्लाई या स्किप?', 'అప్లై చేయాలా?'],
  reel5:       ['Final Verdict', 'फाइनल फैसला', 'తుది తీర్పు'],
  reel6:       ['Allotment & Listing', 'अलॉटमेंट व लिस्टिंग', 'అలాట్‌మెంట్ & లిస్టింగ్'],

  // issue
  overview:    ['Business Overview', 'बिज़नेस क्या है', 'బిజినెస్ ఏమిటి'],
  issueSize:   ['Issue Size', 'इश्यू साइज़', 'ఇష్యూ సైజ్'],
  fresh:       ['Fresh Issue', 'फ्रेश इश्यू', 'ఫ్రెష్ ఇష్యూ'],
  freshSub:    ['Company Growth', 'कंपनी की ग्रोथ', 'కంపెనీ గ్రోత్'],
  ofs:         ['OFS', 'OFS', 'OFS'],
  ofsSub:      ['Promoter Exit', 'प्रमोटर एग्ज़िट', 'ప్రమోటర్ ఎగ్జిట్'],
  priceBand:   ['Price Band', 'प्राइस बैंड', 'ప్రైస్ బ్యాండ్'],
  lotSize:     ['Lot Size', 'लॉट साइज़', 'లాట్ సైజ్'],
  minInv:      ['Min Investment', 'न्यूनतम निवेश', 'కనీస పెట్టుబడి'],
  listOn:      ['Lists On', 'लिस्टिंग', 'లిస్టింగ్'],
  keyDates:    ['Key Dates', 'ज़रूरी तारीखें', 'ముఖ్య తేదీలు'],
  announced:   ['Announced', 'ऐलान', 'ప్రకటన'],
  opens:       ['Opens', 'ओपन', 'ఓపెన్'],
  closes:      ['Closes', 'क्लोज़', 'క్లోజ్'],
  allot:       ['Allotment', 'अलॉटमेंट', 'అలాట్‌మెంట్'],
  listing:     ['Listing', 'लिस्टिंग', 'లిస్టింగ్'],
  cr:          ['Cr', 'करोड़', 'కోట్లు'],
  shares:      ['shares', 'शेयर', 'షేర్లు'],

  // gmp
  todayGmp:    ["Today's GMP", 'आज का GMP', 'నేటి GMP'],
  overBand:    ['over upper band', 'अपर बैंड से ऊपर', 'అప్పర్ బ్యాండ్ కంటే'],
  estListing:  ['Est. Listing Price', 'अनुमानित लिस्टिंग प्राइस', 'అంచనా లిస్టింగ్ ధర'],
  perLot:      ['Est. Gain / Lot', 'प्रति लॉट मुनाफ़ा', 'లాట్‌కి లాభం'],
  gmpTrend:    ['GMP Trend', 'GMP ट्रेंड', 'GMP ట్రెండ్'],
  gmpTrail:    ['GMP: Announcement → Listing', 'GMP: ऐलान से लिस्टिंग तक', 'GMP: ప్రకటన నుంచి లిస్టింగ్ వరకు'],
  peak:        ['Peak', 'सबसे ऊपर', 'గరిష్టం'],
  trough:      ['Low', 'सबसे नीचे', 'కనిష్టం'],
  daysTracked: ['Days Tracked', 'दिन ट्रैक किए', 'ట్రాక్ చేసిన రోజులు'],
  /* Short enough for a fifth table column on a 9:16 frame — "Profit / Lot"
     wraps and pushes the row height up, which costs a whole day off the trail. */
  profitLot:   ['Profit', 'मुनाफ़ा', 'లాభం'],
  profitLotNote: ['profit per lot of {n} shares, if it lists at the GMP',
                  '{n} शेयर के एक लॉट पर मुनाफ़ा, GMP पर लिस्ट हुआ तो',
                  '{n} షేర్ల లాట్‌కు లాభం, GMP వద్ద లిస్ట్ అయితే'],
  /* Reel 6's range can come from a real expected-listing range rather than the
     GMP, so it must not claim the GMP as its basis the way the trail note does. */
  profitLotOn: ['on one lot of {n} shares',
                '{n} शेयर के एक लॉट पर',
                '{n} షేర్ల ఒక లాట్‌పై'],
  /* Labels for the company-profile strip on reel 1. The VALUES behind them are
     deliberately never translated — a promoter's name, a city and a founding
     year have one correct form — so only the label crosses languages, and the
     scene falls back to the stored English label for any key not listed here. */
  knowThem:      ['Know the company', 'कंपनी को जानिए', 'కంపెనీ గురించి తెలుసుకోండి'],
  /* Tag on the one timeline stage still ahead. Short because it sits inline
     between the stage name and its date on a 9:16 frame. */
  nowNext:       ['NEXT', 'अगला', 'తదుపరి'],
  /* Labels under the lot × price = cost strip on reel 1. Deliberately short —
     they sit under three side-by-side figures on a 9:16 frame. */
  capPrice:      ['Cap price', 'कैप प्राइस', 'క్యాప్ ధర'],
  /* GMP is already an initialism every viewer of this channel knows, so it is
     left untranslated — see KEEP_VERBATIM in ai.py for the same rule. */
  gmpShort:      ['GMP', 'GMP', 'GMP'],
  youPay:        ['You pay', 'आप देंगे', 'మీరు చెల్లించేది'],
  factFounded:   ['Founded', 'स्थापना', 'స్థాపన'],
  factHQ:        ['Head office', 'मुख्यालय', 'ప్రధాన కార్యాలయం'],
  factIndustry:  ['Industry', 'इंडस्ट्री', 'పరిశ్రమ'],
  factPromoters: ['Promoters', 'प्रोमोटर', 'ప్రమోటర్లు'],
  kostak:      ['Kostak', 'कोस्टक', 'కోస్టక్'],
  sauda:       ['Subject to Sauda', 'सब्जेक्ट टू सौदा', 'సబ్జెక్ట్ టు సౌదా'],
  /* Both of these are prices for the APPLICATION, not the shares, and both are
     jargon a retail viewer will not know — the figure alone reads like a second
     GMP. One line each, in the space under the number, saying which risk the
     buyer is taking off your hands. */
  kostakNote:  ['paid even if you get no allotment',
                'अलॉटमेंट न मिले तो भी मिलता है',
                'కేటాయింపు రాకపోయినా చెల్లిస్తారు'],
  saudaNote:   ['paid only if you get allotment',
                'अलॉटमेंट मिलने पर ही मिलता है',
                'కేటాయింపు వచ్చినప్పుడే చెల్లిస్తారు'],
  updated:     ['Updated', 'अपडेट', 'అప్‌డేట్'],
  surge:       ['SURGE', 'तेज़ी', 'జోరు'],
  stable:      ['STABLE', 'स्थिर', 'స్థిరం'],
  drop:        ['DROP', 'गिरावट', 'తగ్గుదల'],
  gmpNote:     ['GMP is unofficial grey-market data and changes daily',
                'GMP अनऑफिशियल ग्रे-मार्केट डेटा है, रोज़ बदलता है',
                'GMP అనధికారిక గ్రే-మార్కెట్ డేటా, రోజూ మారుతుంది'],
  allIpos:     ['All Live IPOs', 'सभी लाइव IPO', 'అన్ని లైవ్ IPOలు'],
  todayBoard:  ["Today's GMP Board", 'आज का GMP बोर्ड', 'నేటి GMP బోర్డ్'],
  // Reel 3's all-IPOs cut. Its own labels rather than reusing the GMP
  // board's — the two boards answer different questions and a viewer who
  // sees "GMP Board" over a subscription table has been told the wrong thing.
  subBoard:    ['Subscription Board', 'सब्सक्रिप्शन बोर्ड', 'సబ్‌స్క్రిప్షన్ బోర్డ్'],
  // Reel 4's "what applying costs you" scene.
  yourStake:   ['YOUR STAKE', 'आपका दांव', 'మీ పెట్టుబడి'],
  oneLotCost:  ['ONE LOT COSTS', 'एक लॉट की कीमत', 'ఒక లాట్ ఖరీదు'],
  atTodayGmp:  ['AT TODAY’S GMP', 'आज के GMP पर', 'నేటి GMP వద్ద'],
  allotOdds:   ['ALLOTMENT ODDS', 'अलॉटमेंट चांस', 'అలాట్‌మెంట్ ఛాన్స్'],
  likely:      ['Likely', 'संभावित', 'సాధ్యం'],
  // The three categories that allot by DRAW, and so have odds worth quoting.
  // sHNI and bHNI are the NII book either side of SEBI's ₹10 lakh line; since
  // October 2021 both allot their minimum application by lottery exactly as
  // retail does, which is why each gets its own row rather than one "NII".
  catRetail:   ['Retail', 'रिटेल', 'రిటైల్'],
  catShni:     ['sHNI ₹2-10L', 'sHNI ₹2-10 लाख', 'sHNI ₹2-10 లక్షలు'],
  catBhni:     ['bHNI ₹10L+', 'bHNI ₹10 लाख+', 'bHNI ₹10 లక్షలు+'],
  minTicket:   ['MIN TICKET', 'न्यूनतम रकम', 'కనీస మొత్తం'],
  // Its own heading, not a second 'ALLOTMENT ODDS'. The tile above already
  // carries that label for retail, and the same words twice on one frame
  // reads as a duplicated block rather than as a breakdown of it.
  oddsByCat:   ['IF YOU APPLY AS', 'अगर आप हैं', 'మీరు దరఖాస్తు చేస్తే'],
  // QIB is proportionate and the anchor book is discretionary — there is no
  // draw, so printing "1 in N" beside it would invent a statistic about a
  // process that holds no lottery.
  qibProportionate: ['QIB is allotted proportionately — no draw, no odds.',
                     'QIB में अनुपात से अलॉटमेंट होता है — कोई लॉटरी नहीं।',
                     'QIB నిష్పత్తి ప్రకారం కేటాయిస్తారు — లాటరీ లేదు.'],
  lotPending:  ['Lot size not published yet.', 'लॉट साइज़ अभी नहीं आया।', 'లాట్ సైజ్ ఇంకా రాలేదు.'],
  stakeNote:   ['Extra lots do not improve retail odds. Apply with money you can lock up.',
                'ज़्यादा लॉट से रिटेल चांस नहीं बढ़ते। उतना ही लगाएं जितना रोक सकें।',
                'ఎక్కువ లాట్లు రిటైల్ ఛాన్స్ పెంచవు. ఆపగలిగే డబ్బే పెట్టండి.'],
  takingBids:  ['TAKING BIDS NOW', 'अभी बिडिंग जारी', 'ఇప్పుడు బిడ్డింగ్'],
  closesCol:   ['CLOSES', 'बंद', 'ముగుస్తుంది'],
  overallCol:  ['OVERALL', 'कुल', 'మొత్తం'],
  subNote:     ['Subscription moves all day — the closing figure is the one that counts.',
                'सब्सक्रिप्शन दिन भर बदलता है — आखिरी आंकड़ा ही मायने रखता है।',
                'సబ్‌స్క్రిప్షన్ రోజంతా మారుతుంది — చివరి సంఖ్యే ముఖ్యం.'],

  // subscription
  subDay:      ['Subscription Day', 'सब्सक्रिप्शन डे', 'సబ్‌స్క్రిప్షన్ డే'],
  qib:         ['QIB', 'QIB', 'QIB'],
  nii:         ['NII / HNI', 'NII / HNI', 'NII / HNI'],
  retail:      ['RETAIL', 'रिटेल', 'రిటైల్'],
  employee:    ['EMPLOYEE', 'एम्प्लॉई', 'ఎంప్లాయీ'],
  totalSub:    ['Total Overall Subscription', 'कुल सब्सक्रिप्शन', 'మొత్తం సబ్‌స్క్రిప్షన్'],
  times:       ['times subscribed', 'गुना सब्सक्राइब', 'రెట్లు సబ్‌స్క్రైబ్'],
  dayWise:     ['Day-wise', 'दिन के हिसाब से', 'రోజువారీ'],
  demHeavy:    ['🔥 HEAVY DEMAND', '🔥 ज़बरदस्त डिमांड', '🔥 భారీ డిమాండ్'],
  demGood:     ['✅ HEALTHY DEMAND', '✅ अच्छी डिमांड', '✅ మంచి డిమాండ్'],
  demOk:       ['🟡 MODERATE DEMAND', '🟡 ठीक-ठाक डिमांड', '🟡 మధ్యస్థ డిమాండ్'],
  demWeak:     ['⚠️ WEAK DEMAND', '⚠️ कमज़ोर डिमांड', '⚠️ బలహీన డిమాండ్'],

  // financials
  financials:  ['Financials', 'फाइनेंशियल्स', 'ఫైనాన్షియల్స్'],
  revenue:     ['Revenue', 'रेवेन्यू', 'ఆదాయం'],
  ebitda:      ['EBITDA', 'EBITDA', 'EBITDA'],
  ebitdaMargin:['EBITDA Margin', 'EBITDA मार्जिन', 'EBITDA మార్జిన్'],
  pat:         ['PAT', 'PAT (मुनाफ़ा)', 'PAT (లాభం)'],
  patMargin:   ['PAT Margin', 'PAT मार्जिन', 'PAT మార్జిన్'],
  revCagr:     ['Revenue CAGR', 'रेवेन्यू CAGR', 'ఆదాయం CAGR'],
  patCagr:     ['PAT CAGR', 'PAT CAGR', 'PAT CAGR'],
  marginShift: ['Margin Shift', 'मार्जिन में बदलाव', 'మార్జిన్ మార్పు'],
  valuation:   ['Valuation', 'वैल्यूएशन', 'వాల్యుయేషన్'],
  peRatio:     ['P/E at upper band', 'अपर बैंड पर P/E', 'అప్పర్ బ్యాండ్ వద్ద P/E'],
  peerPe:      ['Peer Average P/E', 'पीयर औसत P/E', 'పీర్ సగటు P/E'],
  cheaper:     ['cheaper than peers', 'पीयर से सस्ता', 'పీర్ల కంటే చౌక'],
  pricier:     ['premium to peers', 'पीयर से महँगा', 'పీర్ల కంటే ఖరీదు'],
  ronw:        ['RoNW', 'RoNW', 'RoNW'],
  debtEquity:  ['Debt / Equity', 'डेट / इक्विटी', 'డెట్ / ఈక్విటీ'],
  marketCap:   ['Market Cap', 'मार्केट कैप', 'మార్కెట్ క్యాప్'],

  // benchmark meters
  healthCheck: ['Health Check', 'हेल्थ चेक', 'హెల్త్ చెక్'],
  goodAbove:   ['good above', 'अच्छा इससे ऊपर', 'దీని కంటే ఎక్కువ మంచిది'],
  goodBelow:   ['good below', 'अच्छा इससे नीचे', 'దీని కంటే తక్కువ మంచిది'],
  vsPeers:     ['vs peers', 'पीयर के मुक़ाबले', 'పీర్లతో పోలిస్తే'],
  markGood:    ['GOOD', 'अच्छा', 'మంచిది'],
  markWeak:    ['WEAK', 'कमज़ोर', 'బలహీనం'],
  aboveBench:  ['above benchmark', 'बेंचमार्क से ऊपर', 'బెంచ్‌మార్క్ కంటే ఎక్కువ'],

  // apply or skip
  applySkip:   ['Apply or Skip?', 'अप्लाई करें या नहीं?', 'అప్లై చేయాలా వద్దా?'],
  growth:      ['Financial Growth', 'फाइनेंशियल ग्रोथ', 'ఫైనాన్షియల్ గ్రోత్'],
  keyRisk:     ['Key Risk', 'मुख्य रिस्क', 'ప్రధాన రిస్క్'],
  greenFlags:  ['Green Flags', 'ग्रीन फ्लैग्स', 'గ్రీన్ ఫ్లాగ్స్'],
  redFlags:    ['Red Flags', 'रेड फ्लैग्स', 'రెడ్ ఫ్లాగ్స్'],

  // verdict
  verdictL:    ['Verdict', 'फैसला', 'తీర్పు'],
  score:       ['IPO Pulse Score', 'IPO पल्स स्कोर', 'IPO పల్స్ స్కోర్'],
  scoreBasis:  ['What this score is built on', 'यह स्कोर किस पर आधारित है',
                'ఈ స్కోర్ దేని ఆధారంగా'],
  noVerdict:   ['No verdict yet', 'अभी कोई फैसला नहीं', 'ఇంకా తీర్పు లేదు'],
  noVerdictWhy: ['Not enough data on this IPO to recommend anything',
                 'इस IPO पर सिफारिश करने लायक डेटा अभी नहीं है',
                 'ఈ IPOపై సిఫారసు చేయడానికి తగినంత డేటా లేదు'],
  scoreThin:   ['Too little data to score yet',
                'स्कोर देने के लिए अभी बहुत कम डेटा है',
                'స్కోర్ ఇవ్వడానికి ఇంకా చాలా తక్కువ డేటా'],
  scoreOn:     ['scored on', 'इस पर आधारित', 'దీని ఆధారంగా'],
  ofData:      ['of the inputs', 'इनपुट का', 'ఇన్‌పుట్‌లలో'],

  // "we do not have this" states. Every one of these replaces a panel that
  // used to render a zero, an empty box, or a 0/0 bar — all of which read on
  // camera as a fact about the company rather than a hole in the data.
  notDisclosed: ['Split not disclosed yet',
                 'फ्रेश/OFS बंटवारा अभी घोषित नहीं',
                 'ఫ్రెష్/OFS విభజన ఇంకా వెల్లడించలేదు'],
  splitNote:   ['The RHP has it — fresh vs OFS is not on the exchange feed',
                'RHP में है — एक्सचेंज फीड पर नहीं',
                'RHP లో ఉంది — ఎక్స్ఛేంజ్ ఫీడ్‌లో లేదు'],
  noFinancials: ['Financials not entered yet',
                 'फाइनेंशियल्स अभी दर्ज नहीं',
                 'ఫైనాన్షియల్స్ ఇంకా నమోదు కాలేదు'],
  noFlags:     ['No flags written yet', 'अभी कोई फ्लैग नहीं लिखा गया',
                'ఇంకా ఫ్లాగ్‌లు రాయలేదు'],
  fromGmp:     ['implied by today’s GMP', 'आज के GMP से अनुमानित',
                'నేటి GMP ఆధారంగా'],
  noRange:     ['No expected range set', 'कोई अनुमानित रेंज सेट नहीं',
                'అంచనా శ్రేణి సెట్ కాలేదు'],
  more:        ['more not shown', 'और भी हैं', 'ఇంకా ఉన్నాయి'],
  gmpAsOf:     ['GMP as of', 'GMP इस तारीख का', 'GMP ఈ తేదీ నాటికి'],
  gmpStale1:   ['not updated today', 'आज अपडेट नहीं हुआ', 'ఈరోజు అప్‌డేట్ కాలేదు'],
  gmpStaleN:   ['days old', 'दिन पुराना', 'రోజుల నాటిది'],
  scoreManual: ['set by hand', 'हाथ से सेट', 'చేతితో సెట్'],
  sc_grey:         ['Grey market', 'ग्रे मार्केट', 'గ్రే మార్కెట్'],
  sc_demand:       ['Demand', 'डिमांड', 'డిమాండ్'],
  sc_fundamentals: ['Fundamentals', 'फंडामेंटल्स', 'ఫండమెంటల్స్'],
  sc_valuation:    ['Valuation', 'वैल्यूएशन', 'వాల్యుయేషన్'],
  sc_structure:    ['Issue structure', 'इश्यू स्ट्रक्चर', 'ఇష్యూ స్ట్రక్చర్'],
  forRetail:   ['Retail Investor', 'रिटेल निवेशक', 'రిటైల్ ఇన్వెస్టర్'],
  forHni:      ['HNI / NII', 'HNI / NII', 'HNI / NII'],
  forLong:     ['Long Term Investor', 'लॉन्ग टर्म निवेशक', 'లాంగ్ టర్మ్ ఇన్వెస్టర్'],
  r_apply:     ['APPLY', 'अप्लाई', 'అప్లై'],
  r_avoid:     ['AVOID', 'अवॉइड', 'వద్దు'],
  r_watch:     ['WATCHLIST', 'वॉचलिस्ट', 'వాచ్‌లిస్ట్'],
  closesIn:    ['Closes In', 'बंद होने में', 'ముగియడానికి'],
  closed:      ['Issue Closed', 'इश्यू बंद', 'ఇష్యూ ముగిసింది'],
  applyBefore: ['Apply before the cut-off', 'कट-ऑफ से पहले अप्लाई करें', 'కట్-ఆఫ్‌కి ముందే అప్లై చేయండి'],

  // allotment
  allotStatus: ['Allotment Status', 'अलॉटमेंट स्टेटस', 'అలాట్‌మెంట్ స్టేటస్'],
  out:         ['OUT', 'आ गया', 'వచ్చింది'],
  expected:    ['EXPECTED', 'संभावित', 'అంచనా'],
  registrar:   ['Official Registrar', 'ऑफिशियल रजिस्ट्रार', 'అధికారిక రిజిస్ట్రార్'],
  listDate:    ['Listing Date', 'लिस्टिंग डेट', 'లిస్టింగ్ తేదీ'],
  expRange:    ['Expected Listing', 'संभावित लिस्टिंग', 'అంచనా లిస్టింగ్'],
  how10:       ['Check allotment in 10 seconds', '10 सेकंड में अलॉटमेंट चेक करें', '10 సెకన్లలో అలాట్‌మెంట్ చెక్ చేయండి'],
  follow:      ['Follow for daily IPO updates', 'डेली IPO अपडेट के लिए फॉलो करें', 'రోజువారీ IPO అప్‌డేట్‌ల కోసం ఫాలో అవ్వండి'],
  disclaimer:  ['Not investment advice. Do your own research.',
                'यह निवेश सलाह नहीं है। खुद रिसर्च करें।',
                'ఇది పెట్టుబడి సలహా కాదు. మీరే పరిశోధించండి.'],

  // status
  st_upcoming: ['OPENS SOON', 'जल्द खुलेगा', 'త్వరలో ఓపెన్'],
  st_open:     ['OPEN NOW', 'अभी खुला है', 'ఇప్పుడు ఓపెన్'],
  st_closed:   ['CLOSED', 'बंद', 'ముగిసింది'],
  st_allotment:['ALLOTMENT', 'अलॉटमेंट', 'అలాట్‌మెంట్'],
  st_listed:   ['LISTED', 'लिस्ट हो गया', 'లిస్ట్ అయ్యింది'],

  // "can I apply right now?" — the board's whole point. `st_open` says the
  // window is open; these say how much of it is left, which is the part a
  // viewer acts on.
  ap_lastday:  ['LAST DAY', 'आख़िरी दिन', 'చివరి రోజు'],
  ap_open:     ['APPLY NOW', 'अभी अप्लाई करें', 'ఇప్పుడే అప్లై చేయండి'],
  ap_soon:     ['OPENS', 'खुलेगा', 'ఓపెన్ అవుతుంది'],
  ap_shut:     ['WINDOW SHUT', 'विंडो बंद', 'విండో మూసివేయబడింది'],
  canApply:    ['CAN YOU APPLY?', 'क्या अप्लाई कर सकते हैं?', 'అప్లై చేయవచ్చా?'],
  openNowCount:['OPEN NOW', 'अभी खुले', 'ఇప్పుడు ఓపెన్'],
};

const VERDICTS = {
  apply:    { hex: '#22C55E', text: ['APPLY FOR LISTING GAIN', 'लिस्टिंग गेन के लिए अप्लाई करें', 'లిస్టింగ్ గెయిన్ కోసం అప్లై చేయండి'] },
  both:     { hex: '#22C55E', text: ['APPLY — LISTING + LONG TERM', 'अप्लाई — लिस्टिंग + लॉन्ग टर्म', 'అప్లై — లిస్టింగ్ + లాంగ్ టర్మ్'] },
  longterm: { hex: '#F59E0B', text: ['LONG TERM ONLY', 'सिर्फ़ लॉन्ग टर्म के लिए', 'దీర్ఘకాలానికి మాత్రమే'] },
  risky:    { hex: '#F59E0B', text: ['RISKY — APPLY SMALL', 'रिस्की — छोटा दांव लगाएँ', 'రిస్క్ — చిన్నగా అప్లై చేయండి'] },
  avoid:    { hex: '#EF4444', text: ['AVOID / SKIP', 'अवॉइड करें / स्किप करें', 'స్కిప్ చేయండి'] },
};

const DEFAULT_STEPS = {
  en: ['Open the registrar or BSE allotment page', 'Pick the IPO name from the dropdown',
       'Enter PAN or Application Number', 'Hit Search — status shows instantly'],
  hi: ['रजिस्ट्रार या BSE अलॉटमेंट पेज खोलें', 'ड्रॉपडाउन से IPO का नाम चुनें',
       'PAN या एप्लिकेशन नंबर डालें', 'सर्च दबाएँ — स्टेटस तुरंत दिखेगा'],
  te: ['రిజిస్ట్రార్ లేదా BSE అలాట్‌మెంట్ పేజీ తెరవండి', 'డ్రాప్‌డౌన్‌లో IPO పేరు ఎంచుకోండి',
       'PAN లేదా అప్లికేషన్ నంబర్ ఎంటర్ చేయండి', 'సెర్చ్ నొక్కండి — స్టేటస్ వెంటనే కనిపిస్తుంది'],
};

function label(key, lang) {
  const entry = LABELS[key];
  if (!entry) return key;
  return entry[LANG_INDEX[lang] ?? 0] || entry[0];
}
