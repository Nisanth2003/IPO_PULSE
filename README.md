# IPO Pulse

Reel studio for the **IPO Pulse** YouTube Shorts channel. Six topics, six
videos, one screen recording each.

The repo is split cleanly in two:

| | |
|---|---|
| **`backend/`** | Owns the numbers and the prose. Python. Reads and writes **one Google Sheet** — the only copy of the data — computes every derived metric, and runs Gemini for analysis and translation. |
| **`frontend/`** | Owns nothing but pixels. A static page that reads that JSON and draws it as animated scenes. Deploys to GitHub Pages as-is. |

The frontend never calculates a figure it wasn't given, and never holds a
secret. The backend never draws anything.

---

## How a reel works

Each of the six topics is **one video**, made of **scenes**. A scene fills the
9:16 frame with exactly one idea and holds for a few seconds before the next
slides up. Press play, record the screen once, and you have a finished Short —
no cutting.

| Reel | Scenes |
|---|---|
| 1 · About IPO | hook → company → **know the company** → fresh vs OFS → price/lot/minimum → key dates |
| 2 · Daily GMP | hook → gauge → estimated listing → **announcement-to-listing trail, with profit per lot for every day** |
| 2 · Daily GMP *(board mode)* | hook → **every live IPO in one table** |
| 3 · Subscription | hook → QIB/NII/Retail bars → day-wise build-up |
| 4 · Apply or Skip | hook → **financials** → **valuation** → green vs red flags |
| 5 · Final Verdict | score → verdict badge → who should apply + countdown |
| 6 · Allotment | status + registrar → how to check in 10s → listing range |

Every scene renders in 9:16, 4:5, 1:1 and 16:9. Text auto-shrinks to fit, so a
long company name never spills out of frame.

**Every scene carries a figure where a figure helps**, not just formatted text:

| Reel · scene | Figure |
|---|---|
| 1 · terms | price band drawn to scale, with the GMP-implied listing marked beyond the cap; and `lot × cap = what you pay` spelled out |
| 1 · dates | a timeline rail — ticks for stages passed, a pulsing marker on the next one, dimmed ahead |
| 2 · gauge | percentage ring |
| 2 · listing | the estimate as a stack, so the premium's size *relative to* the price is visible |
| 2 · trail | sparkline + profit-per-lot per day |
| 3 · bars | QIB / NII / Retail, direct-labelled |
| 3 · trend | day-wise columns scaled to the peak — shows whether demand crept or hockey-sticked |
| 4 · financials · valuation | table + benchmark meters with a "healthy" tick |
| 4 · flags | green-vs-red balance bar with counts |
| 5 · score | scored ring |
| 6 · checklist | numbered steps joined by a connector |

Series colours are **validated, not chosen by eye** — see `SERIES` in
`frontend/js/reels.js` and the note in `NOTES.md`. The old QIB/NII pair measured
ΔE 0.3 apart under deuteranopia, i.e. identical.

The **IPO picker is colour-coded by status** — green bidding now, amber upcoming,
red closed and awaiting allotment, blue allotment, grey already listed — on both
the list and the closed control, with the status word in the label so the colour
is never the only signal. The status comes from the same `derive()` call the card
uses, so the two cannot disagree.

**Four card themes** — Midnight, Carbon, Royal, Ember — switchable per video so
a run of uploads doesn't look like one long take. A theme changes the card
palette and nothing else: no number, label or layout moves, which is what makes
it safe to change mid-series. Each carries its own six accents, one per reel, so
"which topic is this" still reads inside every theme.

The **company logo** rides in the card header, so it appears on every scene of
every reel. `gmp-sync` stores it as the `logo` role on the Sources tab; pin your
own there to override one you don't like, and it will never be overwritten.

---

## Quick start

```bash
# 1. install
cd backend
pip install -r requirements.txt

# 2. publish the sample data as JSON
python -m ipopulse.cli build

# 3. open the studio
python -m ipopulse.cli serve      # -> http://127.0.0.1:8000
```

Or with Docker, which needs nothing installed but Docker itself:

```bash
cp .env.example .env              # add GEMINI_API_KEY if you have one
docker compose up                 # studio on http://localhost:8000

docker compose run --rm cli build
docker compose run --rm cli gmp vertex-aerospace 46
docker compose run --rm cli import /data/ipos.xlsx    # drop files in ./import
```

Your data is not in the image or the volumes — it is in the Google Sheet. Edit
the sheet in a browser and the next run sees it; the volumes carry only the
Gemini cache and generated reports.

> Open it over **http**, not by double-clicking `index.html`. The page fetches
> the sheet over the network, and browsers block `fetch` on `file://`.

Two sample IPOs ship with the repo (fictional companies, so nothing false can
be published by accident). Delete them once you have a real one.

---

## Daily workflow

Once an IPO is set up, a day's update is one command and a recording:

```bash
python -m ipopulse.cli refresh --subscription     # reads the web, republishes
```

Or type the numbers yourself, which is always the fallback:

```bash
python -m ipopulse.cli gmp vertex-aerospace 46          # today's GMP
python -m ipopulse.cli sub vertex-aerospace 2 \
    --qib 12.42 --nii 24.86 --retail 9.18 --total 14.63
python -m ipopulse.cli build
```

Then in the studio: pick the company, pick the reel, press **F** for focus
mode, start your recorder, press **Space**. Copy the voiceover script from the
right panel — it already has the numbers in it.

You can also type a number straight into the left panel to see it live. Those
edits are **session-only** and are not written back; put the final value in the
sheet so the report and the published site agree.

---

## Getting data in

### From a spreadsheet

```bash
ipopulse import ./import/ipos.xlsx --dry-run   # see what it matched first
ipopulse import ./import/ipos.xlsx
ipopulse import "https://…/pub?output=csv"     # a published Google Sheet
ipopulse import ./gmp-log.xlsx --kind gmp      # one row per GMP day
```

Headers are matched fuzzily, so `Price Band (High) ₹`, `pricehigh` and
`Upper Band` all land in the same field. Title banners above the header row are
skipped, `(1,234)` parses as negative, and a dozen date formats are understood.
Columns it doesn't recognise are printed rather than silently dropped — always
run `--dry-run` on a new sheet.

**Translation happens here, at import.** Gemini is called once per data change,
not once per build, and the result is written into the sheet.

### From the web, via Gemini

Google Search grounding plus URL-context fetching. **Use each site for what it
is authoritative on** — that's the biggest accuracy lever:

| Site | Good for | Not for |
|---|---|---|
| investorgain.com | GMP (dated live table) | — |
| ipowatch.in | GMP, as a second opinion | — |
| **groww.in** | issue details, **live subscription** | **GMP** — a SEBI-registered broker won't publish unofficial grey-market data |
| nseindia / bseindia | the primary record | GMP |

**Pin the exact page per IPO.** This matters more than anything else here:

```bash
ipopulse sources vertex-aerospace
ipopulse sources vertex-aerospace --set gmp=https://www.investorgain.com/ipo/…/
ipopulse sources vertex-aerospace --set subscription=https://groww.in/ipo/vertex-aerospace
```

A pinned page is read directly, which removes the two ways an open web lookup
goes wrong: reading about a similarly-named company, and answering from a page
that went stale without changing its date line.

```bash
ipopulse research vertex-aerospace                     # GMP; look, don't save
ipopulse research vertex-aerospace --what sub          # subscription
ipopulse research vertex-aerospace --what all --write  # both, saved
ipopulse research vertex-aerospace --what ipo --write  # issue details
ipopulse research vertex-aerospace --what financials   # the FY table
```

Everything **proposes rather than publishes**. Each value returns with source
URLs and a confidence, and is vetted before it can be written:

*GMP* — flagged if uncited, undated, low-confidence, or outside roughly
−30%…+150% of the price band.
*Subscription* — flagged if uncited, undated, negative, above 1000x, or if the
overall total doesn't sit within the per-category range.
*Financials* — flagged unless every array is exactly as long as `years`, with
no nulls and high confidence. A short array is the dangerous case, not a
missing one: `compute.py` pairs years to values by position, so three years of
labels against two of revenue silently files FY25's number under FY24 and every
margin, CAGR and benchmark downstream is then computed off a table nobody
typed.

Flagged values need `--write --force`. Those bounds are not theoretical: on the
sample data they correctly catch a Kostak rate (₹900) and a per-lot amount
(₹5,566) being mistaken for a ₹46 GMP, while still letting a genuine *negative*
GMP through. Community sites print GMP, Kostak and subject-to-sauda in adjacent
cells, so that misread is the likely one — and a wrong GMP delivered
confidently is the mistake that costs a finance channel its audience.
**Spot-check GMP before you publish.**

### The daily loop

```bash
ipopulse refresh --subscription
```

Re-reads GMP for every IPO that hasn't listed yet, adds subscription for the
ones currently open, skips and reports anything flagged, then republishes. One
command between you and recording.

### Before you record: `doctor`

```bash
ipopulse doctor                       # what is missing, and which scene it blanks
ipopulse doctor molbio-diagnostics    # just one
ipopulse doctor --fix                 # repair what is derivable, then republish
ipopulse doctor --strict              # exit 1 if anything would render blank
```

A blank panel in the studio looks like a rendering bug and is almost always a
field nobody filled, three tabs away in the sheet. `doctor` names the field,
the scene it breaks, and the command that fills it:

```
── Molbio Diagnostics Limited  (molbio-diagnostics)
  ✗ Revenue                  blank → reel 4 financials, score: fundamentals
  ✗ Fresh/OFS split          blank → reel 1 split, score: structure
  · Announced date           → reel 1 dates omits the row
  ⚠ GMP trail has 2 weekday gap(s): 2026-08-11, 2026-08-12
```

`--fix` only applies repairs that follow arithmetically — a total from its two
parts, the T+3 calendar from a close date, a registrar's status URL from the
registrar's name. The line is: if two people with the same sheet would write
down different numbers, it is not a repair. That deliberately excludes the
tempting ones (carrying a GMP forward across a day nobody read it, guessing a
listing range, inferring a sector from the name), which stay as findings rather
than becoming quiet fabrications that later read as data.

`doctor --fix` runs inside the `daily` chain, between `sync` and `build`.

### Filling reel 4 for free: `facts`

```bash
ipopulse facts                        # every IPO, gaps only
ipopulse facts tempsens-instruments-india --dry-run
ipopulse facts <slug> --force         # replace stored figures, not just gaps
```

Three years of restated revenue / EBITDA / PAT / net worth / borrowings, plus
post-issue EPS and the sHNI and bHNI minimum bid sizes — read straight out of
InvestorGain's detail record, which publishes them as an HTML table. Free,
keyless, no Gemini request.

This exists because reel 4 is called *Apply or Skip* and its financials and
valuation scenes were blank on 16 of 19 tracked IPOs. The only filler in the
pipeline was `rhp`, a model read of a 400-page prospectus PDF that usually came
back empty and spent a request finding out. `facts` runs before `enrich` in the
`daily` chain, so by the time the metered step looks, there is nothing left for
it to do.

Gap-filling by default: a figure you corrected by hand survives it. The one
case it refuses without `--force` is a **different year axis** — replacing
FY23–FY25 with FY24–FY26 while keeping a hand-typed series would silently
re-label FY23's revenue as FY24, so the years and the values only ever move
together. (Three IPOs were already in exactly that state: correct figures filed
under scaffold year labels the RHP step never corrected.)

### Can I record this? `validate`

```bash
ipopulse validate                     # every IPO × every reel
ipopulse validate -v                  # also the thin scenes and shut windows
ipopulse validate --strict            # exit 1 if a record contradicts itself
```

```
IPO                               STATUS      1  2  3  4  5  6   READY  ISSUES
Augmont Enterprises               open        ●  ●  ●  ◐  ●  ·   4/6
Tempsens Instruments (India)      closed      ·  ●  ●  ·  ·  ●   3/6

  ● ready   ◐ recordable but thin or stale   ✗ a required field is missing
  · outside its window
```

Two independent judgements, and keeping them apart is the point:

* **the window** — every reel has a shelf life that follows from the issue
  calendar alone. A subscription reel cannot be shot before bidding opens and
  is worthless once allotment is out; a GMP reel runs until the listing settles
  the bet. Pure arithmetic on the dates, no model asked.
* **the data** — are the fields those particular scenes read present, do they
  contradict each other, and were the two that move daily read recently enough
  to still be true.

A reel is green only when both say yes. That is what stops a confident green
light on an issue that closed last Friday: the data is complete, valid, and
publishing it would still be misinformation.

`validate` also reports contradictions a record can hold while every `doctor`
check passes — a listing dated before the close, an EBITDA larger than the
revenue it came out of, a lot value outside SEBI's ₹10k–₹15k retail minimum, a
subscription total that went *down* between two days.

The same judgement drives the studio: a coloured dot on each reel tab, a
`●3`-style count in the company dropdown, and a line under the tabs reading
*"Valid until Thu, 27 Aug, 17:00 — bidding closes."*

### Is the data still arriving? `monitor`

```bash
ipopulse monitor                      # the health check
ipopulse monitor --strict             # exit 1 on anything that should have arrived and did not
```

The watchdog, and the only scheduled job whose purpose is to go **red**.

Every other job writes something and reports success on exit 0 — which it does
just as happily when the sheet was already full and nothing new arrived. A
timer that quietly stopped firing, a slug that stopped matching upstream, an
expired credential: none of them fail a run. The first visible symptom is a
reel quoting a three-day-old premium as today's.

`monitor` compares the store against two things:

* **the calendar** — an issue taking bids today *must* have a subscription row
  dated today and a GMP no older than yesterday. What should have arrived is
  derivable from the issue's own dates, so a missing row is a finding rather
  than something to eyeball.
* **its own last run** — a fingerprint goes to `backend/.cache/monitor.json`
  every run, so the next one can say what moved. Nothing moving is not
  automatically wrong (a weekend, no live issues); nothing moving *while an
  issue is open* always is.

It also catches the same company stored twice, which discovery does when a
near-miss in the slug matcher scaffolds a second row instead of filling the
first — both then collect GMP and both appear in the dropdown.

Read-only. It makes no sheet write, so unlike every other job it cannot race
one. Scheduled at 12:30, 19:30 and 22:30 IST, each slot placed after a writing
job has had time to finish.

## Adding an IPO

```bash
python -m ipopulse.cli new zenith-motors
```

Open the Google Sheet and fill in its row on the **IPOs** tab (the long tabs
— Financials, GMP, Subscription — are keyed by the same `slug`). Edits you
make there are what the backend reads on its next run. The fields that
matter most:

- **`issue.fresh_cr` / `issue.ofs_cr`** — drives the "company growth vs
  promoter exit" scene, the one viewers actually care about.
- **`dates.announced`** — GMP tracking runs from here to listing.
- **`financials`** — revenue / EBITDA / PAT / net worth / debt per year, plus
  `eps` and `pe_peer_avg`. Margins, CAGRs, RoNW, D/E and P/E are all computed.
- **`analysis`** — your editorial call, in English. Translations are generated.
- **`benchmarks`** — optional; overrides the "what counts as good" lines below.

Then `ipopulse build`.

---

## Health check benchmarks

A figure on its own tells a viewer nothing — is a 15% EBITDA margin good? So
Reel 4 draws each metric as a bar with a tick at the healthy threshold: past
the tick is green, short of it is red, and the scene header shows the tally
(`6/7`).

| Metric | Good when | |
|---|---|---|
| EBITDA margin | ≥ 15% | higher is better |
| PAT margin | ≥ 8% | higher is better |
| Revenue CAGR | ≥ 15% | higher is better |
| RoNW | ≥ 15% | higher is better |
| Debt / Equity | ≤ 1.0x | **lower** is better |
| P/E | ≤ peer average | **lower** is better |

These are broad rules of thumb for mainboard issues, not sector truth. A bank
or a utility will fail several of them while being a perfectly good business —
override per IPO:

```yaml
benchmarks:
  ronw: 12
  debt_equity: 3        # leverage is normal in lending
```

---

## The IPO Pulse score

Reel 5's 0-10 is computed, not typed. Five inputs, each marked out of 10 from
figures already derived elsewhere, then weighted:

| Input | Weight | Marked on |
|---|---|---|
| Grey market | 25% | GMP as a % of the upper band |
| Demand | 20% | overall subscription, latest day |
| Fundamentals | 30% | how many of the seven benchmarks above are met |
| Valuation | 15% | P/E against this issue's own peer average |
| Issue structure | 10% | fresh issue % — money into the company, not out |

**A component only counts when its data exists, and the total is rescaled by
the weight that actually applied.** So an IPO with nothing but a GMP is scored
*on its GMP* and says so on the card, rather than being marked down to near
zero for the four things nobody has typed in yet. Below 40% coverage the scene
stops showing a verdict at all and says the data is too thin.

That rescaling is the whole design. Before it, `analysis.score` was a slider
defaulting to `0.0`, so every IPO nobody had hand-scored published a confident
**0.0/10** — which means "no one has judged this" and reads as "this is a
terrible IPO". Those are opposite claims.

The bands live in `SCORE_WEIGHTS`, `GREY_BAND`, `DEMAND_BAND` and `VALUE_BAND`
in `compute.py`, mirrored in `compute.js`. They are anchors rather than a
formula on purpose: "10x subscribed is a 7.5" is a judgement you can see and
argue with, where a tuned logarithm hides the same judgement inside a constant.
Change them in both files, or the studio previews a number the published JSON
disagrees with.

The slider is still there and still wins — leave it at 0 for the computed
score, move it to override. The card shows which of the two it is showing, and
the score scene lists every component with its weight, its mark and the figure
behind it, so the number always shows its working.

---

## Gemini

Used for **words only** — translating the business overview, flags and risk
into Hindi and Telugu, and optionally drafting the analysis. It never produces
a number: GMP %, subscription multiples, EBITDA margins and P/E are computed in
`backend/ipopulse/compute.py` from stored data. On a channel where people act
on the figures, a hallucinated percentage is the one failure you cannot ship.

```bash
cp .env.example .env          # add GEMINI_API_KEY
python -m ipopulse.cli translate vertex-aerospace          # -> hi, te
python -m ipopulse.cli analyse vertex-aerospace            # draft, prints only
python -m ipopulse.cli analyse vertex-aerospace --write    # save into the sheet
python -m ipopulse.cli build
```

Three things worth knowing:

- **Responses are cached to disk by content hash, expiring after 30 days.**
  Re-running is free and byte-identical, so re-recording next week doesn't
  silently reword your captions. `--force` regenerates.

  ```bash
  ipopulse cache                      # entries, size, oldest, TTL
  ipopulse cache --prune --days 30    # drop anything older
  ipopulse cache --clear
  ipopulse build --prune-cache        # prune as part of the build
  ```

  Set the TTL with `IPOPULSE_CACHE_DAYS` in `.env`. Pruning never loses text:
  the translations themselves live on the sheet's I18n tab, so a pruned cache only
  means the next *edit* re-asks Gemini.
- **The key stays local.** Translations are written into the sheet;
  the published site only ever sees the finished text. A GitHub Pages site is
  fully public — a key in frontend JS is a leaked key within a day. The deploy
  workflow greps for key patterns and refuses to publish if it finds one.
- **Without a key everything still builds** — captions just stay in English, and
  the header shows an "EN fallback" chip.

---

## Reports

```bash
python -m ipopulse.cli report vertex-aerospace   # one IPO, 6 sheets
python -m ipopulse.cli report                    # all IPOs + board sheet
```

Writes a formatted `.xlsx` into `backend/out/`: Summary, Issue & Dates,
Financials, GMP History, Subscription, Analysis. The studio's **Report** button
downloads the same content as CSV for a quick grab without leaving the browser.

---

## Wiring a real data source

`backend/ipopulse/providers/api.py` is a skeleton with the contract already
pinned down. Fill in three methods, map the fields, then:

```bash
python -m ipopulse.cli sync --slug vertex-aerospace
```

Hand-typed values win by default (`merge()` only fills blanks), so a bad fetch
degrades to a stale number rather than a wrong published one. Pass
`--prefer-api` when the feed really is more current.

One caution: there is **no official free GMP API**. Every public one is a scrape
of a community site — unofficial, rate-limited, and liable to change shape
without notice. Every GMP point carries a `source` field so you can tell later
where a figure came from.

---

## Deploying to GitHub Pages

1. Push to `main`.
2. Settings → Pages → Source: **GitHub Actions**.
3. Done — `.github/workflows/pages.yml` publishes `frontend/`.

**Nothing about the data is committed.** The site fetches the Google Sheet at
runtime, so a pipeline run is live the moment it finishes — there is no build
to re-run and no file to push. The deploy ships code only.

Two requirements for that to work:

1. The sheet must be shared as **Anyone with the link can view**, or the
   browser cannot read it. Everything in it is therefore public.
2. `GOOGLE_SHEETS_ID` must be set as a repository secret, so the publish
   workflow can generate `frontend/js/config.js` (gitignored — the id is not
   committed, though it is visible in the deployed page).

Paths are relative, so it works under `https://<user>.github.io/<repo>/`
without configuration.

### What goes in Settings → Secrets and variables → Actions

| Name | Kind | Read by |
|---|---|---|
| `GOOGLE_SHEETS_ID` | secret | both workflows — `publish.yml` writes it into `config.js`, `schedule.yml` reads the sheet |
| `GOOGLE_SHEETS_KEY` | secret | the service-account JSON, pasted whole (the code takes contents or a path) |
| `IPOPULSE_TRIGGER_PASSWORD` | secret | `publish.yml`. Only a PBKDF2 hash of it reaches the published JS |
| `GEMINI_API_KEY` | secret | `schedule.yml` — `enrich`, `research`, `translate` |
| `GH_DISPATCH_PAT` | secret, optional | `publish.yml`. Sealed, not shipped — see below |
| `GEMINI_MODEL` | **variable** | model id |
| `GOOGLE_SHEETS_TAB` | **variable** | tab name |
| `IPOPULSE_TRIGGER_API` | **variable**, optional | the hosted backend, once there is one |

The last three are variables rather than secrets deliberately:
`IPOPULSE_TRIGGER_API` ends up in the published JS anyway, so hiding it would
be theatre.

### The one that cannot be shipped in the clear

`GH_DISPATCH_PAT` is a fine-grained PAT (Actions → Read and write, this repo
only) that lets the studio's **⚡ Run job** panel dispatch `schedule.yml`
without pasting a token every session.

It cannot be substituted into `config.js` the way `SHEET_ID` is. The browser
does not *check* a token, it *replays* it to `api.github.com`, so the original
bytes have to come back — and anything the page can recover, a visitor can
recover too, from a public repo's published output. That is a different problem
from the password, which only ever has to be *compared*, and therefore ships as
a one-way hash.

So `ipopulse config` encrypts it: **AES-256-GCM, key from PBKDF2-SHA256 over
`IPOPULSE_TRIGGER_PASSWORD` with its own random salt**, and only the ciphertext
reaches `config.js`. `gate.js` unseals it the moment you type the site password
at the front door, in memory, and writes it to no storage at all. Leave the
secret unset and the panel falls back to asking for a token by hand.

Two things this costs, worth knowing before you enable it:

- **The site password is now load-bearing.** Whoever learns it gets the studio
  *and* a token that can run Actions here, attackable offline from the
  published ciphertext at PBKDF2 cost per guess. Use a long random one.
- **Scope the token tightly** — Actions:write, one repo, short expiry. Then the
  worst case stays "can run the same jobs the cron already runs".

The salt is random per build and **not** the sheet id, which is the salt the
gate hash uses. Sharing one salt would publish the decryption key next to the
ciphertext it opens; `publish.yml`'s leak scan is the independent backstop and
already refuses to deploy any `frontend/` file containing a raw `github_pat_`.

---

## Keyboard

| | |
|---|---|
| `1`–`6` | jump to reel |
| `←` `→` | previous / next scene |
| `↑` `↓` | previous / next reel |
| `Space` | play the reel |
| `F` / `Esc` | focus mode on / off |
| `G` | Shorts safe-zone overlay |
| `B` | cycle backdrop (incl. green screen) |
| `E` | export PNG |
| `[` `]` | text scale |

---

## Before you publish

- **Verify the registrar link.** The built-in URLs are current but registrars
  move them, and Link Intime is now MUFG Intime.
- **Keep the disclaimer on.** GMP is unofficial grey-market data.
- Nothing here is investment advice, and the tool won't pretend otherwise.

---

## Layout

```
backend/
  ipopulse/
    models.py       canonical IPO schema
    compute.py      every derived number (single source of truth)
    sheets.py       THE STORE: read/write the live Google Sheet
    tables.py       the tab layout, shared with the browser's reader
    workbook.py     local .xlsx snapshots, for backups only
    store.py        load/save on top of it
    providers/      NSE, InvestorGain, ipoji, RHP, sheets
    ai.py           Gemini + on-disk cache
    report.py       the formatted, human-readable Excel report
    publish.py      verifies every record still renders
    cli.py          the commands above
  data/cache/       Gemini responses (gitignored)
frontend/
  index.html        the studio shell + all scenes
  css/studio.css
  js/               i18n · compute (mirrors compute.py) · data · reels
                    · output (scripts, CSV, PNG) · studio
  js/config.js      generated, gitignored: the sheet id
  js/sheet.js       reads the sheet as CSV in the browser, no key, no library
legacy/             the original single-file prototype
```

`frontend/js/compute.js` is a deliberate mirror of `backend/ipopulse/compute.py`
— the browser recomputes live while you type, the backend computes the same
values for the report. **Change a formula in one, change it in the other.**
