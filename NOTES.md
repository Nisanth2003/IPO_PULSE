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

### Publishing: three configurations in one day, and why it settled where it did

Worth writing down because each step looked correct at the time.

**1. Actions-based Pages in one repo** (the original `pages.yml`). Failed with
*"Get Pages site failed … Not Found"*. That reads like a misconfiguration and
isn't: the repo was **private**, and Pages on a private repo needs a paid plan.
Pages could not be enabled at all, so `configure-pages` had nothing to find.
`enablement: true` would not have helped for the same reason.

**2. Split repos.** Keep code private, mirror only `frontend/` into a small
public repo whose Pages deploys from a branch. Correct for a private repo, and
the right Pages source too — the frontend is pre-built, so there is nothing for
an Actions workflow on the far side to build.

**3. Back to one repo, Actions source.** The repo was then made public, which
removes the entire reason for the split: no second repo, no personal access
token, no `SITE_REPO` variable. This is where it now sits.

The trap in between: with **Deploy from a branch**, Pages can only serve the
repo *root* or `/docs`. The root here is the README, so the published site was
Jekyll rendering README.md while the actual studio sat at `/frontend/index.html`.
Serving a subfolder as the site root is precisely what the Actions source is
for — `upload-pages-artifact` with `path: frontend`.

The one step that survived all three versions is the **secret scan**, and it
always runs *before* publishing, never after. The repo and the site are both
public now, so it is the last thing between a key and the open internet.
Verified to block `AIza…` and `AQ.…` keys while passing ordinary code.

### `inputs.x != false` is false on a push event

The publish workflow guarded its build step with `if: inputs.rebuild != false`,
intending "build unless the manual checkbox was unticked". On a push there is
no `inputs`, so `inputs.rebuild` is null — and GitHub coerces both null and
false to `0` when comparing across types, making the test `0 != 0`, i.e.
**false**. The build silently skipped on exactly the trigger it existed for.
The run went green in 18 seconds and deployed whatever JSON happened to be
committed.

Guard on the event instead:

    if: github.event_name != 'workflow_dispatch' || inputs.rebuild

Related: while Pages was still set to branch-deploy, *both* `publish.yml` and
GitHub's built-in `pages-build-deployment` ran on the same push, and whichever
finished last won. That is why the site kept showing the README even after a
green publish run.

### Known and open

- **Issue size renders as ₹0 Cr on the published site.** NSE's `issueSize` is a
  *share count* (e.g. 24,956,363), not rupees crore, and nothing maps it to
  `fresh_cr` / `ofs_cr` — which is what the About IPO scene adds up. Shares x
  price band / 1e7 would give the total in crore, but NSE does not publish the
  fresh-vs-OFS split, so only the total is recoverable from it. Until then the
  number has to be typed into the YAML by hand.
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
