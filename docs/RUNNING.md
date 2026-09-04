# Running IPO Pulse

How to start everything, in order, and what each thing is for.

Companion to [HOW-IT-WORKS.html](HOW-IT-WORKS.html), which explains *why* the
pieces fit together the way they do. This one is just commands.

---

## 0. One-time setup

```bash
cd backend
pip install -r requirements.txt
```

Then three things that are not pip packages:

| Need | Why | Check it |
|---|---|---|
| **ffmpeg** on PATH | builds the video and does the transitions | `ffmpeg -version` |
| **Chrome** installed | Playwright drives the real Chrome, not a bundled Chromium — saves a 150 MB download | `python -c "from playwright.sync_api import sync_playwright; print('ok')"` |
| **`backend/.env`** | every key and the sheet id | see `.env.example` |

If Chrome is missing (a CI runner, a container), install Playwright's own
browser instead and pass `--channel ""` to the tools that take it:

```bash
python -m playwright install chromium
```

---

## 1. Start the server — this is both frontend and backend

**One command. One process. One port.** There is no separate API server to
start.

```bash
cd backend
python -m ipopulse.cli serve --port 8000
```

That gives you:

| URL | What it is |
|---|---|
| <http://localhost:8000> | **The studio** — the seven reels, the data, the scripts |
| <http://localhost:8000/trigger> | **The control panel** — run pipeline jobs from the browser |
| <http://localhost:8000/api/health> | Liveness check; says whether auth is configured |

`serve` also rewrites `frontend/js/config.js` from your `.env` on every start,
so a fresh clone serves a site that can actually find the sheet.

### Verifying it came up

```bash
curl -s http://localhost:8000/api/health
# {"ok": true, "auth": true}
```

`"auth": true` means `IPOPULSE_TRIGGER_PASSWORD` is set, so the studio and the
trigger panel will both ask for it. `/api/jobs` returning **401** is correct —
the job endpoints sit behind a token you get by logging in.

### Other flags

```bash
python -m ipopulse.cli serve --port 8080          # different port
python -m ipopulse.cli serve --host 0.0.0.0       # reachable from the LAN / inside Docker
```

> **A note on `--host 0.0.0.0`.** That exposes the trigger panel — which can
> run pipeline jobs — to anything on your network. The password gate is the
> only thing in front of it. Keep it on `localhost` unless you specifically
> need otherwise.

---

## 2. Ports, and what listens on them

| Port | Started by | Notes |
|---|---|---|
| **8000** | `ipopulse serve` | studio + API + trigger panel |
| **8771** | `tools/render.py`, automatically | a throwaway un-gated copy of the site, for headless rendering. Starts and stops with the render; you never start it yourself |
| **8765** | `ipopulse publish --authorise` | catches the Google OAuth redirect, for a few seconds, once |

If port 8000 is taken, find and clear it:

```powershell
Get-NetTCPConnection -LocalPort 8000 -State Listen |
  Select-Object -ExpandProperty OwningProcess -Unique |
  ForEach-Object { Stop-Process -Id $_ -Force }
```

A symptom worth knowing: if **two** servers end up on 8000, the studio loads
fine but every `/api/…` call 404s — because a plain static server grabbed the
port first and knows nothing about the API routes.

---

## 3. The data pipeline

Everything is a named job. `ipopulse job` with no arguments lists them all
with their schedules.

```bash
cd backend
python -m ipopulse.cli job                 # what exists, and when it runs
python -m ipopulse.cli job daily           # the main chain
python -m ipopulse.cli job market          # tomorrow's pre-market briefing
```

The chain is `sync → verify → dedupe → facts → enrich → doctor → build →
validate`, and the order matters — a bad row is challenged in the same run
that discovered it, before any AI budget is spent on it.

Run steps individually while testing:

```bash
python -m ipopulse.cli list                # what is tracked, and where it stands
python -m ipopulse.cli validate            # which reels are recordable right now
python -m ipopulse.cli monitor             # is the data still arriving?
python -m ipopulse.cli dedupe              # one offer, one row (dry run)
python -m ipopulse.cli facts               # financials + tickers, free, no AI
python -m ipopulse.cli market --show       # read today's stored briefing
```

Anything that writes takes `--write`. Without it you get a dry run — that is
the convention across the whole CLI, not a per-command choice.

**In production this is Windows Task Scheduler**, not systemd, calling these
same job names. Changing a job's arguments in `control.py` changes both the
schedule and the trigger panel at once, because both resolve the same dict.

---

## 4. Making a video

`render.py` needs **no server running** — it starts its own un-gated copy of
the site, renders, and shuts it down. Do not point it at port 8000; the
password gate would hide the reel and you would get a confusing
"element is not stable" timeout.

```bash
cd backend
python tools/render.py --slug rays-of-belief --reel 2 --lang en
```

Output lands in `backend/out/video/<slug>-r<reel>-<lang>.mp4` — a normal
1080×1920 H.264 file. Nothing is downloaded; it is built here.

With the extras:

```bash
python tools/render.py \
  --slug rays-of-belief --reel 5 --lang en \
  --opener  data/cache/art/open-rays-of-belief-r5-en.png \
  --endcard data/cache/art/end-en.png \
  --audio   out/audio/rays-of-belief-r5-en.mp3 \
  --queue
```

| Flag | Effect |
|---|---|
| `--opener` / `--endcard` | the generated title and subscribe cards |
| `--audio` | narration mp3, muxed in |
| `--queue` | file the finished video for review (see §5) |
| `--keep DIR` | keep the per-scene PNGs instead of deleting them |
| `--url` | drive an already-running server instead of self-hosting |

### The cards

```bash
python -c "
import sys; sys.path.insert(0,'.')
from ipopulse.cli import load_dotenv; load_dotenv()
from ipopulse import art, store
print(art.for_reel(store.load('rays-of-belief'), 5, 'en'))
"
```

Generated once per `(slug, reel)` and cached in `backend/data/cache/art/`
forever. Deleting a file is the only way to ask for a new one. The background
is textless so one generation serves English, Hindi and Telugu.

---

## 5. Publishing

Rendering is automatic. Publishing is a decision, and it has its own gate.

```bash
cd backend
python -m ipopulse.cli publish                          # the review list
python -m ipopulse.cli publish --approve <ID>           # yes → unlisted
python -m ipopulse.cli publish --approve <ID> --public  # yes → public
python -m ipopulse.cli publish --reject  <ID> --why "GMP looked stale"
python -m ipopulse.cli publish --upload --dry-run       # what would go
python -m ipopulse.cli publish --upload                 # send it
```

Nothing reaches YouTube without an `--approve` you typed. The uploader only
ever takes what the queue hands it, and the queue only hands over what a
person approved. A scheduled render loop reaches the queue and stops.

### One-time YouTube authorisation

1. **Google Cloud Console** → enable **YouTube Data API v3** on a project
2. Credentials → Create → **OAuth client ID** → application type
   **Desktop app**
   *(not Web — a Web client refuses the loopback redirect this uses)*
3. Download the JSON → save as `backend/client_secret.json`
4. Then:

```bash
python -m ipopulse.cli publish --authorise
```

A browser opens, you click allow, and it prints which channel it connected
to — check that, before a video lands somewhere unexpected. From then on it
runs unattended: the stored refresh token does not expire on a schedule.

---

## 6. A full end-to-end pass

```bash
cd backend

# 1. fresh data
python -m ipopulse.cli job daily

# 2. what can be filmed?
python -m ipopulse.cli validate

# 3. pick one from that list and render it into the queue
python tools/render.py --slug <slug> --reel 5 --lang en --queue

# 4. look at the mp4 in out/video/, then decide
python -m ipopulse.cli publish
python -m ipopulse.cli publish --approve <slug>-r5-en

# 5. send it
python -m ipopulse.cli publish --upload
```

---

## 7. When something is wrong

| Symptom | Cause | Fix |
|---|---|---|
| Studio loads, `/api/*` all 404 | two servers on 8000; a static one won | kill both, start `serve` once (§2) |
| `catalogue never loaded` | `GOOGLE_SHEETS_ID` unset, or the sheet is not link-viewable | check `.env`; share the sheet as "anyone with the link can view" |
| Render: `element is not stable` for ~30s | pointed at the gated site | drop `--url` and let it self-host |
| `429 RESOURCE_EXHAUSTED` | daily Gemini free-tier quota gone | wait for the reset; `art.py` and the briefing both degrade rather than fail |
| Hindi/Telugu cards show empty boxes | no Indic font on this machine | put Noto Sans Devanagari / Telugu in `backend/fonts/` |
| Sheet writes silently reverted | a scheduled job overlapped your edit | there is no lock; re-apply and check `ipopulse monitor` |
| `spreadsheets.create` 403 | the service account cannot make files | create the sheet by hand, share it with the service account, use `migrate --into <id>` |

Health checks, cheapest first:

```bash
python -m ipopulse.cli monitor      # did today's data arrive?
python -m ipopulse.cli validate     # is anything recordable?
python -m ipopulse.cli doctor       # what is missing?
python -m ipopulse.cli grade        # do our numbers match the desk's?
```
