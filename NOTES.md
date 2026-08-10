# Working notes

A running log of what changed, and — more useful later — *why*, especially
where the obvious approach turned out to be wrong. The README says how to use
the thing; this says what we learned building it.

---

## 2026-08-10 — data pipeline, Google Sheets, scheduling

Went from a repo with two fictional sample IPOs and no working integrations to
a live pipeline: real IPO data in, Google Sheet and static site out, on a
schedule, with a manual override page. **Everything below runs on free tiers.**

### Where the data comes from now

| Source | Gives | Costs |
|---|---|---|
| **NSE JSON** | issue terms, dates, lot, registrar, live subscription | nothing — no key at all |
| **Gemini + `url_context`** | grey-market premium (GMP) | nothing — free tier |
| **Manual** | anything, always the fallback | — |

`ipopulse sync --provider nse --discover` finds IPOs we don't track yet and
pulls them. It found 8 live ones on day one.

### Things that were wrong, and what they cost

These are the notes worth keeping. Each one looked fine and wasn't.

**`google_search` is metered, `url_context` is not.**
The GMP path was dead with a 429 and the apparent answer was "enable billing".
It wasn't. The two grounding tools bill separately, and our code attached
`google_search` to *every* grounded call — even when the IPO already had a URL
pinned and search was redundant. Dropping it when a URL is pinned made the
whole GMP feature work for free. Google's fetcher also renders investorgain's
JavaScript table, which a plain HTTP scraper only sees as "No data available",
so the headless browser we thought we needed was never needed.

**The free tier binds on requests, not tokens — by ~150x.**
Peak usage was 847 TPM against a 250K limit (0.3%) while sitting at 10 of 20
daily *requests* (50%). Flash has 20 RPD; flash-lite has 500 at identical TPM.
One full translate pass is 20 calls — the entire daily flash allowance. The
model ranking now puts **lite above flash** for this reason. Ranking by model
quality is the intuitive mistake and it exhausts the quota in one run.

**A push scheduled 15 minutes after a sync is a race.**
One slow NSE day and the sheet publishes yesterday's numbers as if they were
today's. Chained instead: step N+1 cannot start until step N exits 0. Six
scheduled tasks became four.

**One evening data pull misses the entire story.**
Bidding is 10:00–17:00 IST and subscription is a running total that only moves
inside that window. A single 18:30 run captured one number a day and missed
the last-day surge, which is the thing worth making a video about. Now 13:00
and 16:30 on weekdays plus 18:00 daily.

**NSE's `series` is not always `EQ`.**
`EQ` is the mainboard, `SME` is NSE Emerge. Querying an SME issue with
`series=EQ` does not error — it returns a full payload of nulls. That silently
zeroed one IPO's subscription and hid its price band entirely. The field also
turns out to be the `board` value, so it was worth reading anyway.

**SME and mainboard report subscription in different fields.**
Mainboard fills `bidDetails[].noOfTime`; SME leaves it absent and uses
`activeCat.dataList[].noOfTotalMeant`. `activeCat` carries it for both, so
prefer that — but its first row is a header whose "values" are column captions.

**Windows PowerShell 5.1 reads a BOM-less `.ps1` as ANSI.**
Two UTF-8 em-dashes inside strings were a parse error pointing at the wrong
line. Deploy scripts are kept pure ASCII.

**`-Remove` that only knows current job names orphans tasks.**
Renaming `sync` → `daily` left four scheduled tasks running `ipopulse job
<gone>` nightly with nobody watching. It now sweeps the whole folder.

**A secret in `.env.example` is a committed secret.**
Only `.env` is gitignored. A live API key sat in the *template* file, which is
the one that ships. The template is now labelled and blank, and the key should
be rotated.

### What got built

- `providers/scrape.py` — NSE provider (catalogue, issue detail, subscription).
  Deliberately returns `[]` for GMP rather than guessing; no exchange has it.
- `providers/sheet_push.py` — Google Sheets write-back. Upsert, not append, so
  a daily run rewrites today's row instead of growing a duplicate. Columns it
  doesn't know about are left alone, so your own notes column survives. Headers
  come from the same alias table the importer reads, so the round trip is
  closed by construction — verified 22/22 columns, values identical.
- `control.py` — password-gated Trigger panel and HTTP API. Jobs are a fixed
  dict of argv lists run with `shell=False`; there is no endpoint that accepts
  a command. Lives in the backend, never in `frontend/`, because that folder is
  published verbatim to a public site.
- `ipopulse job <name>...` — named jobs, several run in the order given. The
  schedulers and the panel both resolve names through the same dict, so a
  timer and the UI cannot drift apart.
- `deploy/windows` (Task Scheduler), `deploy/systemd`, and a GitHub Actions
  workflow. **GitHub Pages cannot run any of these** — it is static hosting
  with no processes. Actions is the GitHub-native option.

### Decisions taken deliberately

- **Dates into the sheet are ISO text, not date cells.** A real date renders
  per the sheet's locale, and a US-locale sheet exports `8/12/2026`, which the
  importer reads as 8 December — a silent four-month corruption. ISO text is
  unambiguous and still sorts correctly.
- **ipowatch.in dropped as a default GMP source.** Its robots.txt sets
  `ai-train=no` and disallows AI fetchers by name. investorgain publishes
  `ai-input=yes`. Pin ipowatch per-IPO if you disagree.
- **No batching of translation calls.** It would cut 20 requests to 2, but at
  500 RPD there is no pressure and it complicates the cache. Revisit if the
  IPO count grows a lot.

### Why the site lives in a second repo

This repo is **private**, and GitHub Pages on a private repo needs a paid plan
— on the free plan Pages only serves public repos. That is what
`configure-pages` was really reporting when it failed with *"Get Pages site
failed … Not Found"*: not a misconfiguration, but Pages being unavailable.
Adding `enablement: true` would not have helped for the same reason.

So: code, data, notes and secrets stay private here; only the contents of
`frontend/` are mirrored into a small public repo that Pages serves.

That public repo uses **Deploy from a branch**, not GitHub Actions. The
frontend is plain HTML/CSS/JS reading JSON that `ipopulse build` already
produced — there is nothing to build on the far side, so an Actions workflow
there would only copy files to themselves. Branch-deploy is exactly the case
for pre-built files. `pages.yml` was written for the single-repo Actions model
and has been deleted.

`publish.yml` does the mirroring, and it keeps the one step worth keeping from
`pages.yml`: **the secret scan, which now runs before the push rather than
after.** Everything past that gate lands somewhere genuinely public, so a scan
that ran afterwards would be decoration. It is verified to block both an
`AIza…` key and an `AQ.…` one while passing ordinary code.

Two details that bite otherwise: the mirror **replaces** the tree rather than
copying over it, so deleting a file here deletes it there instead of leaving
it served forever; and it writes `.nojekyll`, because Pages runs Jekyll by
default and Jekyll silently drops anything whose name starts with `_`.

### Known and open

- The two original IPOs (Vertex Aerospace, Meridian Logistics) are **fictional
  sample records**. Delete them once the real ones are established.
- **Researched GMP lands on the page's own date**, not today's — e.g. 08-07
  while running on 08-10. Arguably correct, and `merge_series` keys on date so
  nothing duplicates, but watch that the daily series actually advances.
- **The service-account key still sits at the repo root.** Gitignored, but
  `secrets/` would be a better home.
- **Actions runners are datacenter IPs and NSE is known to block those.**
  Untested until pushed. If sync starts returning block pages, that is why,
  and the schedule has to stay on a local machine.
- **Financials collapse to the latest year** through the sheet round trip —
  one column per field means FY24/FY25 cannot survive. Per-year columns would
  need a change on the read side too.
