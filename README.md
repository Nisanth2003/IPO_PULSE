# IPO Pulse

Reel studio for the **IPO Pulse** YouTube Shorts channel. Six topics, six
videos, one screen recording each.

The repo is split cleanly in two:

| | |
|---|---|
| **`backend/`** | Owns the numbers and the prose. Python. Stores each IPO as YAML, computes every derived metric, runs Gemini for translation, writes the Excel report, and publishes static JSON. |
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
| 1 · About IPO | hook → company → fresh vs OFS → price/lot/minimum → key dates |
| 2 · Daily GMP | hook → gauge → estimated listing → **announcement-to-listing trail** |
| 2 · Daily GMP *(board mode)* | hook → **every live IPO in one table** |
| 3 · Subscription | hook → QIB/NII/Retail bars → day-wise build-up |
| 4 · Apply or Skip | hook → **financials** → **valuation** → green vs red flags |
| 5 · Final Verdict | score → verdict badge → who should apply + countdown |
| 6 · Allotment | status + registrar → how to check in 10s → listing range |

Every scene renders in 9:16, 4:5, 1:1 and 16:9. Text auto-shrinks to fit, so a
long company name never spills out of frame.

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

Your data is bind-mounted, not baked into the image: edit the YAML in your
editor and the container sees it immediately.

> Open it over **http**, not by double-clicking `index.html`. The page fetches
> JSON, and browsers block `fetch` on `file://`.

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
YAML so the Excel report and the published site agree.

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
not once per build, and the result is written into the YAML.

### From NSE, with no key at all

```bash
ipopulse sync --provider nse --discover     # find new IPOs and pull them
ipopulse sync --provider nse --slug leap-india
```

NSE publishes issue terms and live subscription as JSON, and it *is* the
primary record — so unlike the research provider there is nothing to vet and
nothing to spend. No key, no AI, no quota. It supplies company, board, price
band, dates, lot size, registrar, and category-wise demand updated through the
bidding day.

It does **not** supply GMP, and never will: grey-market premium is by
definition unofficial and no exchange publishes it. `fetch_gmp` returns `[]`
rather than guessing. Use `ipopulse research <slug> --what gmp --write`, or
type it by hand with `ipopulse gmp <slug> 46`.

### GMP on the free tier — pin the URL, don't search

The two Gemini grounding tools are metered separately, and only one is free:

| | free tier |
|---|---|
| `google_search` | 429s immediately without billing |
| `url_context` | works |

So a grounded call attaches url_context **alone** when an IPO has a URL
pinned, and only falls back to search when it has nothing to go on. Attaching
search "as well" is what made every lookup fail. Google's url_context fetcher
also renders investorgain's JavaScript GMP table — the same page a plain HTTP
scraper only ever sees as *"No data available"* — so no headless browser is
needed either.

```bash
ipopulse sources dhoot-transmission --set gmp=https://www.investorgain.com/report/live-ipo-gmp/331/
ipopulse job grey        # refresh GMP for every IPO, then push it
```

`ipowatch.in` was dropped as a default source: its robots.txt sets
`ai-train=no` and disallows several AI fetchers by name. `investorgain.com`
publishes `ai-input=yes`, which permits exactly this use. Pin ipowatch
per-IPO if you decide otherwise.

### Model choice is bound by requests, not tokens

The free tier's limits, and the reason `ai._TIER` ranks lite **above** flash:

```
gemini-*-flash         5 RPM   250K TPM     20 RPD
gemini-*-flash-lite   15 RPM   250K TPM    500 RPD
```

Observed peak on this project: **847 TPM against 250K** — 0.3% of the token
budget — while sitting at **10 of 20 daily requests**. One call per IPO per
language means requests run out roughly 150x sooner than tokens. A single full
translate pass is 20 calls: the entire daily flash allowance, or 4% of
flash-lite's. Picking the "better" model here is what exhausts the quota.

Two details worth knowing:

- **`--discover` scaffolds IPOs you don't track yet.** Without it `sync` can
  only refresh slugs someone already typed in, which makes a live catalogue
  half a feed.
- **Subscription merges on `day`, not `date`.** The exchange reports a running
  total for the whole window, so syncing twice in a day overwrites that day
  rather than appending a second row for it.

The endpoints are the ones nseindia.com's own pages call — public but
undocumented, and they reject any request without a session cookie, so the
provider loads a normal page first. Calls are spaced 0.7s apart.

## Running it on a schedule

Every job has a name, and one command runs it — in any order you like:

```bash
ipopulse job                          # list every job and when it runs
ipopulse job daily                    # the chain: sync -> build -> push
ipopulse job sync build push report   # your own order, run left to right
```

A sequence stops at the first non-zero exit, and says how many steps it
skipped. That is deliberate: a push behind a failed sync would publish stale
numbers as though they were today's.

The schedulers call exactly this, so a timer and the Trigger panel cannot
drift apart — both resolve the same names through `control.JOBS`.

### The schedule, and why these hours

Indian IPO bidding runs **10:00–17:00 IST**. Subscription is a running total
that only moves while that window is open, so a single evening pull would miss
the whole intraday story.

| Job | When (IST) | Why then |
|---|---|---|
| `daily` = sync → build → push | 13:00 Mon–Fri | mid-window, three hours of demand in |
| | 16:30 Mon–Fri | 30 min before close, where the last-day surge happens |
| | 18:00 every day | an hour after close: the day's final, settled figure |
| `grey` = gmp → push-gmp | 21:00 every day | grey-market quotes settle later than the exchange |
| `translate` | Sun 03:00 | cached 30 days; only changes when the prose does |
| `report` | Sun 04:00 | after translate, so the workbook has the final copy |

Two design points worth stating:

- **Chained, not spaced.** An earlier draft scheduled `push` 15 minutes after
  `sync`. That is a race — one slow NSE day and the sheet publishes yesterday.
  Inside a chain, step N+1 cannot start until step N exits 0.
- **The intraday runs are Mon–Fri**, because there is no bidding at the
  weekend. The 18:00 run is every day so a weekend catalogue change still lands.

### Three places it can run

**Windows — Task Scheduler.** systemd does not exist on Windows, and this is
the machine the project runs on:

```powershell
deploy\windows\Register-IpoPulseTasks.ps1
deploy\windows\Register-IpoPulseTasks.ps1 -Remove
Get-ScheduledTask -TaskPath '\IPO Pulse\'
```

Chained jobs are one task carrying several triggers, so runs cannot overlap.
`-StartWhenAvailable` is the `Persistent=true` equivalent; add `-RunAsSystem`
to fire when you are not logged in. Re-running the script clears the whole
`\IPO Pulse\` folder first, so a renamed job cannot orphan a task that keeps
firing forever.

Times are the machine's local clock — there is no per-task timezone. Set the
machine to IST or the hours above mean nothing.

**Linux / Docker host — systemd:**

```bash
deploy/systemd/install.sh            # system-wide
deploy/systemd/install.sh --user     # no root
systemctl list-timers 'ipopulse-*'
journalctl -u 'ipopulse@*' -f
```

One templated unit (`ipopulse@.service`) plus a timer per job, each pinning
`Asia/Kolkata` explicitly so the schedule cannot drift with the host clock.

### Triggering a run by hand

Four ways, depending on where you are:

| Where | How |
|---|---|
| Terminal | `ipopulse job daily` — or any order: `ipopulse job sync build push` |
| Browser, locally | `ipopulse serve` → **⚡ Trigger** → Run, or queue a sequence |
| GitHub, data refresh | Actions → **Scheduled data refresh** → *Run workflow* (takes a `jobs` input, e.g. `sync build push`) |
| GitHub, publish site | Actions → **Publish site** → *Run workflow* |

Both workflows carry `workflow_dispatch`, so the **Run workflow** button is
always there — no need to wait for a cron or fake a commit.

### Publishing the site

`publish.yml` deploys `frontend/` to GitHub Pages **as the site root**, so the
studio is at `https://<user>.github.io/<repo>/` and not one folder down.

One setting has to match, in **Settings → Pages**:

> **Source: GitHub Actions** — not "Deploy from a branch".

That is the whole difference. Branch-deploy can only serve the repo *root* or
`/docs`, and the root here is the README — so it publishes a rendered README
while the studio sits unreachable at `/frontend/index.html`. Serving a
subfolder as the root is exactly what the Actions source exists for.

It runs on any push touching `frontend/**` or `backend/data/ipos/**`, and on
demand. Before deploying it re-runs `ipopulse build` (so the site can never lag
the YAML) and then **scans `frontend/` for secrets and fails the job if it
finds any** — the repo and the site are both public, so that gate runs before
the deploy, never after.

**GitHub — Actions** (`.github/workflows/schedule.yml`):

> **GitHub Pages cannot run any of this.** Pages is static file hosting — no
> processes, no cron, no backend. Pushing this repo does not make a timer run
> anywhere. Actions is the GitHub-native equivalent: it runs the job on a
> hosted runner, commits the regenerated JSON, and that push makes `pages.yml`
> redeploy the site.

Cron is UTC with no timezone option, so each entry is IST − 5:30 with the IST
time in a comment. Actions cron is also best-effort — a run can start 5–20
minutes late under load, which is why the one that matters is 18:00, an hour
clear of the close.

Set these in the repo (Settings → Secrets and variables → Actions):

| | Name |
|---|---|
| Secret | `GEMINI_API_KEY`, `GOOGLE_SHEETS_ID`, `GOOGLE_SHEETS_KEY_JSON` (the whole key file) |
| Variable | `GEMINI_MODEL`, `GOOGLE_SHEETS_TAB` (both optional) |

Run it by hand from the Actions tab — **Run workflow** takes a `jobs` input,
so `sync build push` works there too.

One caveat I could not test without pushing: **NSE sometimes blocks
datacenter IP ranges**, and Actions runners are datacenter IPs. If the sync
step starts returning non-JSON block pages, that is why, and the schedule has
to live on your own machine instead.

## The Trigger panel

`ipopulse serve` also serves a password-gated page for running those same jobs
by hand:

```
IPO Pulse Studio -> http://127.0.0.1:8000/
Trigger panel    -> http://127.0.0.1:8000/trigger
```

The studio toolbar grows a **⚡ Trigger** button when a local backend answers
`/api/health`. On the published site nothing answers, so the button never
appears there.

Each job has **Run** and **+ Queue**. Queue builds a sequence — chips numbered
`1 sync → 2 build → 3 push`, each removable — and **Run sequence** executes
them strictly in that order. Duplicates are allowed, because `sync build push
build` is a legitimate thing to ask for. Queueing stays enabled while a job
runs, so you can line up the next sequence while waiting.

Set `IPOPULSE_TRIGGER_PASSWORD` in `.env`; blank disables the panel. Forgotten
it? Change that line and restart — nothing else stores it.

**This deliberately does not live in `frontend/`.** That folder is published
verbatim to GitHub Pages, and a password checked in browser JavaScript on a
public static site is not a password. The panel HTML is in
`backend/ipopulse/control.py` and is only ever served by a process you run.

For the same reason `serve` refuses to bind anything but loopback unless a
password is set — otherwise `/trigger` is a command runner for anyone who can
reach the port. Jobs are a fixed dict of argv lists run with `shell=False`;
there is no endpoint that accepts a command. Five bad guesses lock the caller
out for five minutes, and the password is exchanged once for a session token
rather than resent.

The HTTP API, if you want to drive it yourself:

```
GET  /api/health                     {ok, auth}          no auth
POST /api/login   {password}      -> {token, ttl}
GET  /api/jobs                       every job + schedule   X-Token
GET  /api/status                     live output, history   X-Token
POST /api/run     {job}           -> {started, steps}      X-Token
POST /api/run     {jobs:[...]}    -> runs them in order    X-Token
```

## Getting data back out, into a Google Sheet

Reading a sheet needs no credentials — publish it as CSV and `import` it.
Writing does, because there is no anonymous way to change someone's document.

```bash
ipopulse push --dry-run            # every IPO, one row each — see it first
ipopulse push
ipopulse push vertex-aerospace     # just one
ipopulse push --kind gmp --tab GMP # one row per GMP day, onto its own tab
```

Set three things in `.env` (see `.env.example`), then share the sheet with the
key's `client_email` as **Editor** — Viewer fails with the same 403 as no
access at all, which is the only genuinely confusing part of the setup:

```
GOOGLE_SHEETS_KEY=…/service-account.json    the path, never the JSON itself
GOOGLE_SHEETS_ID=…                          the id from the sheet URL
GOOGLE_SHEETS_TAB=                          blank = the first tab
```

Two properties worth knowing, because they decide how you can use the sheet:

- **It upserts, it does not append.** Rows are matched on the slug of the
  company name (plus the date, for GMP), so running it after every `refresh`
  rewrites today's row instead of growing a duplicate underneath it.
- **Columns it doesn't know about are left alone.** Add your own notes column
  and it survives every push — only the mapped columns are rewritten.

The header row is generated from the same `ALIASES` table `import` reads, so
anything `push` writes, `import` can read back. Dates are written as ISO text
rather than real date cells on purpose: a real date renders per the sheet's
locale, and a US-locale sheet exports `8/12/2026`, which the importer reads as
8 December. ISO is unambiguous and still sorts correctly.

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
```

Everything **proposes rather than publishes**. Each value returns with source
URLs and a confidence, and is vetted before it can be written:

*GMP* — flagged if uncited, undated, low-confidence, or outside roughly
−30%…+150% of the price band.
*Subscription* — flagged if uncited, undated, negative, above 1000x, or if the
overall total doesn't sit within the per-category range.

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

## Adding an IPO

```bash
python -m ipopulse.cli new zenith-motors
```

Edit `backend/data/ipos/zenith-motors.yaml`. The fields that matter most:

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
python -m ipopulse.cli analyse vertex-aerospace --write    # save into the YAML
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
  the translations themselves live in each IPO's YAML, so a pruned cache only
  means the next *edit* re-asks Gemini.
- **The key stays local.** Translations are written into the YAML and committed;
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

`frontend/data/` is committed, so the deploy carries your data with it. After a
`ipopulse build`, commit the changed JSON and the site updates.

Paths are relative, so it works under `https://<user>.github.io/<repo>/`
without configuration.

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
    store.py        YAML load/save
    providers/      manual today, API-ready tomorrow
    ai.py           Gemini + on-disk cache
    report.py       Excel workbook
    publish.py      writes frontend/data/*.json
    cli.py          the commands above
  data/ipos/        one YAML per IPO  <- you edit these
frontend/
  index.html        the studio shell + all scenes
  css/studio.css
  js/               i18n · compute (mirrors compute.py) · data · reels
                    · output (scripts, CSV, PNG) · studio
  data/             generated JSON  <- committed, this is the deploy
legacy/             the original single-file prototype
```

`frontend/js/compute.js` is a deliberate mirror of `backend/ipopulse/compute.py`
— the browser recomputes live while you type, the backend computes the same
values for the report. **Change a formula in one, change it in the other.**
