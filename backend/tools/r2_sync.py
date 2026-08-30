"""Mirror out/audio/ into a Cloudflare R2 bucket, and prune what is unreachable.

    python tools/r2_sync.py --dir out/audio --scripts out/scripts.json
    python tools/r2_sync.py --prune --dry-run          # see before you delete

WHY THERE IS A BUCKET AT ALL
----------------------------
The narration is the one artefact in this project that is big, permanent and not
text. Thirteen clips for a single IPO measured 34,046,294 bytes on 30 Aug 2026 —
34 MB for ONE company across four reels and three languages. The pipeline is
built to run daily over every open IPO, so the total grows in tens of megabytes
per run and never shrinks on its own.

Neither of the two places the audio has lived so far survives that:

  * **The GitHub Release** (cli.AUDIO_TAG) holds the bytes fine, but the site
    cannot read them. Release assets send no `Access-Control-Allow-Origin` at
    all — verified 30 Aug 2026 on both the github.com 302 and the
    release-assets.githubusercontent.com 200 — and studio.js FETCHES the clip
    rather than pointing an <audio> at it, because the reel is cut to the
    narration's real decoded duration. No CORS header means no bytes means a
    silent reel. See cli._audio_base, which is the long version of this.

  * **frontend/audio/**, which publish.yml copies into so the site reads
    same-origin, works today and is a dead end: every clip then ships inside the
    Pages deployment, which is capped at 1 GB, and the deploy gets slower every
    run for files 99% of visitors never play.

R2 is the third option and the only one that is both readable and free at this
scale: S3-compatible so boto3 talks to it unchanged, 10 GB of storage free, and
**zero egress charges** — which is the number that matters, because a clip that
gets popular on a monetised channel is otherwise a bill that arrives after the
fact. A bucket's public URL sends CORS headers the operator controls, so the
fetch + decodeAudioData path keeps working from GitHub Pages.

Operator note, once, outside this script: the bucket needs public read and an
`Access-Control-Allow-Origin` covering the Pages origin, and IPOPULSE_AUDIO_BASE
then points at it. Everything below assumes that is already done.

WHAT THIS IS ALLOWED TO DELETE
------------------------------
`--prune` is the only destructive thing here and it is deliberately awkward. The
keep-set is computed from **--scripts and never from --dir** — that distinction
is the whole safety story and it is explained at keep_set() and prune(). Read it
before changing anything in that path.

CONFIGURATION
-------------
Five environment variables, from .env locally and from the workflow's `env:` in
Actions. Four are required; the fifth has the only value R2 accepts anyway.

    IPOPULSE_S3_BUCKET             the bucket name
    IPOPULSE_S3_ENDPOINT           https://<account-id>.r2.cloudflarestorage.com
    IPOPULSE_S3_ACCESS_KEY_ID      R2 API token, "Object Read & Write"
    IPOPULSE_S3_SECRET_ACCESS_KEY  its secret
    IPOPULSE_S3_REGION             optional, defaults to "auto"

They are read through cli.load_dotenv, which uses `os.environ.setdefault` — so a
real environment variable always beats the .env line. That ordering is not a
detail: in Actions the secret arrives as a real variable, and a stale .env that
somehow reached the runner must not be able to redirect a write into a bucket
nobody is watching.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ipopulse import store, voice as tts  # noqa: E402
from ipopulse.cli import load_dotenv  # noqa: E402

# `ipopulse ...` gets this from its own entry point; a standalone tool under
# tools/ does not, and without it every credential below reads as unset — the
# run then fails with "IPOPULSE_S3_BUCKET is not set" on a machine where it very
# much is set, in .env, one line down. Same reason ear_test.py opens this way.
load_dotenv()


# ── the numbers this script exists to respect ──────────────────────────────
#
# R2's free tier, from Cloudflare's pricing page as of 30 Aug 2026:
#
#     storage      10 GB-month
#     Class A ops  1,000,000 / month   PUT, LIST, DELETE — the writes
#     Class B ops  10,000,000 / month  GET, HEAD — the reads
#     egress       free, unmetered
#
# Only the first one actually binds. A daily run over a few dozen IPOs is a few
# thousand PUTs and HEADs a month against a million, so the op counts are not
# worth optimising for their own sake — but storage grows monotonically unless
# something prunes, and at ~34 MB per IPO the 10 GB ceiling is roughly 300
# IPO-runs away, which is one busy season and not a distant hypothetical.
#
# The warning fires at 8 GB rather than at the ceiling because the failure at
# the ceiling is not a bill, it is a PUT that starts failing mid-run and leaves
# the site missing exactly the newest clips. 2 GB of headroom is about two
# months of notice at the current rate.
WARN_BYTES = 8_000_000_000
FREE_TIER_BYTES = 10_000_000_000

# The object key is the filename, which is `{slug}-r{reel}-{lang}-{hash8}.mp3`,
# which is a hash of the script text — so the same key can only ever hold the
# same audio. That makes an immutable year-long cache honest rather than
# optimistic: re-word a script and the site asks for a DIFFERENT key, so there
# is no cached copy to go stale and nothing to purge on deploy.
CACHE_CONTROL = "public, max-age=31536000, immutable"

# DeleteObjects takes at most 1000 keys per call. Both a protocol limit and a
# billing one: 1000 deletes in one Class A op instead of 1000 of them.
DELETE_BATCH = 1000

# What a narration asset's key looks like — voice.asset_name's output, spelled
# as a pattern. Only keys matching this are ever considered for deletion.
#
# This is a second, independent brake on prune. The bucket may hold things this
# script did not put there — a favicon, an index.html, another project's folder
# if the account reuses a bucket — and "delete everything not in the keep-set"
# would take those with it, silently, on the first run. Anything that does not
# look like a clip is counted as kept and left alone. An unusual slug (uppercase,
# an underscore) also falls out of this pattern and is therefore kept, which is
# the safe direction to be wrong in: worst case is a few stale bytes, not a
# deleted asset somebody else depends on.
ASSET_RE = re.compile(r"^[a-z0-9][a-z0-9-]*-r\d+-[a-z]{2,3}-[0-9a-f]{8}\.mp3$")

ENV_BUCKET = "IPOPULSE_S3_BUCKET"
ENV_ENDPOINT = "IPOPULSE_S3_ENDPOINT"
ENV_KEY_ID = "IPOPULSE_S3_ACCESS_KEY_ID"
ENV_SECRET = "IPOPULSE_S3_SECRET_ACCESS_KEY"
ENV_REGION = "IPOPULSE_S3_REGION"


class SyncError(RuntimeError):
    """Anything that stops the sync, carrying a message worth showing as-is."""


# ── content type, by sniffing rather than by trusting the name ─────────────

def content_type(head: bytes, name: str = "") -> str:
    """The real media type of these bytes. The extension is not evidence here.

    Every file in out/audio/ ends in `.mp3` and most of them are not mp3. The
    filename is derived from the script hash (voice.asset_name) because the
    browser has to be able to compute it before the file exists, so the
    extension is fixed at `.mp3` by contract and CANNOT follow the format — but
    voice._call_gemini returns Gemini TTS's raw PCM wrapped in a RIFF/WAVE
    header, and only the ElevenLabs path returns actual MPEG.

    Counted on the 13 clips present on 30 Aug 2026: 10 begin `52 49 46 46 ...
    57 41 56 45` (RIFF/WAVE) and 3 begin `49 44 33` (ID3). Ten of thirteen
    files would have been served as audio/mpeg by extension, and every one of
    those would have been a lie.

    Why the lie costs something: Chrome and Firefox sniff the body and play a
    mislabelled file anyway, so this passes every local test. Safari is the one
    that takes the header at its word and refuses, and some CDNs decide
    transfer behaviour from Content-Type. The failure therefore shows up only
    on somebody else's machine, in a published video's companion page, which is
    the most expensive place to discover it.

    An unrecognised header falls back to audio/mpeg rather than
    application/octet-stream on purpose: octet-stream makes a browser offer a
    download instead of playing, so a wrong guess there is worse than a wrong
    guess that at least stays in the audio family.
    """
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav"
    if head[:3] == b"ID3":
        return "audio/mpeg"
    # An MPEG frame sync is 11 set bits: 0xFF then the top three bits of the
    # next byte. `0xFB` is the common one (MPEG-1 Layer III, no CRC) but 0xF2,
    # 0xF3 and 0xFA are all equally valid, so this masks rather than matching a
    # literal — a file that starts on a Layer II or MPEG-2 frame is still mp3
    # to every player that matters.
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return "audio/mpeg"
    if head[:4] == b"OggS":
        return "audio/ogg"
    if head[:4] == b"fLaC":
        return "audio/flac"
    return "audio/mpeg"


def _sniff(path: Path) -> str:
    with path.open("rb") as fh:
        return content_type(fh.read(12), path.name)


# ── the keep-set ───────────────────────────────────────────────────────────

def _dropped_slugs(scripts: Path, drop: set[str]) -> tuple[set[str], dict[str, str]]:
    """Slugs whose clips are dead weight, from the sidecar extract_scripts writes.

    Once an issue LISTS, its reels are finished work — nobody records a GMP
    video about a stock that is already trading — so keeping those objects only
    spends the 10 GB R2 allowance on audio no page will ever request. On the
    24-IPO book measured 30 Aug 2026 that is 7 of 24 IPOs, roughly a third of
    the bucket.

    Deliberately NOT recomputed from the dates here. `status` is derived by the
    board and the studio already handed it to extract_scripts.py, which writes
    it beside the script book; deriving it a second time in the pruner would
    put the same open/closed/listed rule in two places, and the copy that
    drifted would silently delete live clips.

    A missing sidecar is not an error. It means the book was written by an
    older extractor, and the correct response is to keep everything — dropping
    nothing costs storage, while guessing could delete audio the site is
    actively serving. Same for a slug the sidecar does not mention.
    """
    if not drop:
        return set(), {}
    side = scripts.with_name(scripts.stem + ".meta.json")
    try:
        meta = json.loads(side.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set(), {}
    if not isinstance(meta, dict):
        return set(), {}
    seen: dict[str, str] = {}
    out: set[str] = set()
    for slug, entry in meta.items():
        status = ""
        if isinstance(entry, dict):
            status = str(entry.get("status") or "").strip().lower()
        seen[slug] = status
        if status in drop:
            out.add(slug)
    return out, seen


def keep_set(scripts: Path, drop_status: set[str] | None = None) -> set[str]:
    """Every filename the site can ever ask for, from scripts.json.

    `drop_status` removes whole IPOs whose status says their reels are
    finished — see _dropped_slugs. Everything else is kept.

    THIS IS THE PART THAT MUST NOT BE COMPUTED FROM --dir, and the reasoning is
    worth the paragraph because the wrong version looks more natural:

        keep = {p.name for p in Path(args.dir).glob("*.mp3")}   # NEVER

    A narration run is routinely a SUBSET. `ipopulse narrate` takes --slug and
    --langs, the daily job commonly runs only the IPOs whose status is "open",
    and a run that hits its --budget stops early by design (cli.cmd_narrate).
    out/audio/ on a CI runner is also freshly checked out and starts EMPTY. So
    the uploaded set answers "what did this run happen to make", while the
    question prune has to answer is "what can the published site still request"
    — and those differ by every IPO not in today's batch. Pruning against the
    directory would delete every other IPO's clips on the first run and the
    symptom would be a silent play button on pages nobody looked at that day.

    scripts.json is the right source because it is what the STUDIO would
    compute: extract_scripts.py walks the whole catalogue, every reel, every
    language, and writes the exact text. Hashing that text with the same
    function the browser uses reproduces the exact filename the browser will
    ask for. Every slug, every reel, every language — no filtering by --langs
    or by status, because a keep-set that is too large costs storage and one
    that is too small costs audio.

    Raises SyncError rather than returning an empty set when the file is
    missing, unreadable or empty: an empty keep-set combined with --prune means
    "delete every clip in the bucket", and a typo in a path must never be able
    to say that. Refusing loudly is the only correct behaviour here.
    """
    try:
        raw = scripts.read_text(encoding="utf-8")
    except OSError as err:
        raise SyncError(
            f"--prune needs a readable --scripts file and {scripts} is not one "
            f"({err}). Refusing: an unreadable script book yields an empty "
            f"keep-set, and pruning against an empty keep-set empties the "
            f"bucket. Run tools/extract_scripts.py first.") from err

    try:
        book = json.loads(raw)
    except ValueError as err:
        raise SyncError(
            f"--prune needs valid JSON and {scripts} would not parse ({err}). "
            f"Refusing rather than treating a truncated file as 'nothing to "
            f"keep' — a half-written scripts.json is exactly how a run would "
            f"delete the clips it could not see.") from err

    if not isinstance(book, dict) or not book:
        raise SyncError(
            f"{scripts} holds no IPOs. Refusing to prune against an empty "
            f"keep-set, which would delete every object in the bucket.")

    dropped, _seen = _dropped_slugs(scripts, drop_status or set())

    keep: set[str] = set()
    for slug, reels in book.items():
        if slug in dropped:
            continue
        if not isinstance(reels, dict):
            continue
        for reel, per_lang in reels.items():
            if not isinstance(per_lang, dict):
                continue
            try:
                number = int(reel)
            except (TypeError, ValueError):
                continue
            for lang, text in per_lang.items():
                # `.strip()` before hashing, matching cli.cmd_narrate exactly.
                # script_hash strips again so this is belt-and-braces, but the
                # emptiness test below is not: narrate skips a blank script
                # entirely and never writes a file for it, so a blank must not
                # contribute a name to the keep-set either.
                body = (text or "").strip() if isinstance(text, str) else ""
                if body:
                    keep.add(tts.asset_name(slug, number, lang, body))
    if not keep:
        raise SyncError(
            f"{scripts} parsed but yielded no clip names — every script was "
            f"empty or the file is not in the shape extract_scripts.py writes "
            f'({{"slug": {{"1": {{"en": "..."}}}}}}). Refusing to prune.')
    return keep


# ── the client ─────────────────────────────────────────────────────────────

def make_client():
    """(client, bucket) pointed at R2, or SyncError naming the missing setting.

    Credentials are checked here rather than left to boto3 because boto3's
    answer to an unset key is a NoCredentialsError raised from inside the
    signer, four frames deep, that says "Unable to locate credentials" and
    names none of the five variables this project actually uses. In a workflow
    log that is indistinguishable from a broken secret, so it is worth the
    explicit check.
    """
    missing = [name for name in (ENV_BUCKET, ENV_ENDPOINT, ENV_KEY_ID, ENV_SECRET)
               if not (os.getenv(name) or "").strip()]
    if missing:
        raise SyncError(
            f"{missing[0]} is not set — add it to backend/.env, or to the "
            f"workflow's env: block from a repository secret."
            + (f" (also unset: {', '.join(missing[1:])})" if len(missing) > 1 else ""))

    try:
        import boto3
        from botocore.config import Config
    except ImportError as err:
        raise SyncError("boto3 is not installed — pip install boto3") from err

    # R2 has no regions and rejects a real AWS one; "auto" is its documented
    # placeholder. It is still required because SigV4 signs the region string,
    # so an empty value produces a signature mismatch rather than a clear error.
    region = (os.getenv(ENV_REGION) or "").strip() or "auto"

    settings = {
        "region_name": region,
        # Path-style addressing (endpoint/bucket/key) rather than letting
        # botocore promote the bucket into a subdomain. R2 serves both, but the
        # virtual-hosted form puts the bucket name inside the TLS hostname,
        # which breaks the moment a bucket name contains a dot — a wildcard
        # certificate matches one label, not two. Path-style has no such edge.
        "s3": {"addressing_style": "path"},
        "retries": {"max_attempts": 5, "mode": "standard"},
    }
    try:
        # botocore >= 1.36 computes a CRC32 checksum on every upload by
        # default. R2 accepts it today, but this script's whole job is to be
        # boring against a non-AWS implementation, and "when_required" sends
        # checksums only where the protocol demands them. If the installed
        # botocore predates the option it raises TypeError, which is why this
        # is a try and not a line.
        config = Config(request_checksum_calculation="when_required", **settings)
    except TypeError:
        config = Config(**settings)

    return boto3.client(
        "s3",
        endpoint_url=(os.getenv(ENV_ENDPOINT) or "").strip(),
        aws_access_key_id=(os.getenv(ENV_KEY_ID) or "").strip(),
        aws_secret_access_key=(os.getenv(ENV_SECRET) or "").strip(),
        config=config,
    ), (os.getenv(ENV_BUCKET) or "").strip()


def explain(err: Exception) -> str:
    """Turn a botocore failure into a line that names the variable to fix.

    Same bargain as voice._explain: the upstream message is kept, and a hint is
    added for the codes whose plain text points at the wrong thing. R2's
    InvalidAccessKeyId in particular reads as if the key were malformed when
    the usual cause is a token scoped read-only or issued for another account.
    """
    from botocore.exceptions import (BotoCoreError, ClientError,
                                     EndpointConnectionError, NoCredentialsError)

    if isinstance(err, NoCredentialsError):
        return f"No credentials — check {ENV_KEY_ID} and {ENV_SECRET}."
    if isinstance(err, EndpointConnectionError):
        return (f"Could not reach {os.getenv(ENV_ENDPOINT) or '(unset)'} — check "
                f"{ENV_ENDPOINT}. It is the account endpoint "
                f"(https://<account-id>.r2.cloudflarestorage.com), not the "
                f"bucket's public URL.")
    if isinstance(err, ClientError):
        info = err.response.get("Error", {}) or {}
        code = str(info.get("Code") or "")
        detail = str(info.get("Message") or "request refused")
        hint = {
            "NoSuchBucket": f" — no such bucket. Check {ENV_BUCKET}.",
            "InvalidAccessKeyId": f" — check {ENV_KEY_ID}; an R2 token is also "
                                  f"account-scoped, so a token from another "
                                  f"account fails exactly like a typo.",
            "SignatureDoesNotMatch": f" — check {ENV_SECRET}, and that "
                                     f"{ENV_REGION} is 'auto'.",
            "AccessDenied": " — the token is valid but lacks write access. R2 "
                            "tokens default to read-only; this needs "
                            "'Object Read & Write'.",
        }.get(code, "")
        return f"R2 said {code or 'error'}: {detail}{hint}"
    if isinstance(err, BotoCoreError):
        return str(err)
    return str(err)


# ── listing ────────────────────────────────────────────────────────────────

def list_bucket(client, bucket: str) -> dict[str, dict]:
    """Every object in the bucket -> {key: {"size": int, "modified": datetime}}.

    Paginated properly, which is not optional even at today's scale:
    ListObjectsV2 returns at most 1000 keys per call and simply sets
    IsTruncated rather than failing. A single un-paginated call would be
    correct for the first 1000 clips and then quietly report the bucket as
    smaller than it is — under --prune that reads as "these objects do not
    exist", and the next run re-uploads them. The bug would appear the day the
    bucket crosses roughly 80 IPOs and would look like a sync that had started
    doing nothing.
    """
    out: dict[str, dict] = {}
    token = ""
    while True:
        kwargs = {"Bucket": bucket, "MaxKeys": 1000}
        if token:
            kwargs["ContinuationToken"] = token
        page = client.list_objects_v2(**kwargs)
        for obj in page.get("Contents", []) or []:
            out[obj["Key"]] = {"size": int(obj.get("Size") or 0),
                               "modified": obj.get("LastModified")}
        if not page.get("IsTruncated"):
            return out
        token = page.get("NextContinuationToken") or ""
        if not token:
            # Truncated with no token should be impossible; looping forever on
            # the same page would be worse than stopping with what we have.
            return out


# ── upload ─────────────────────────────────────────────────────────────────

def upload(client, bucket: str, folder: Path, dry_run: bool) -> dict:
    """PUT every *.mp3 in `folder`, skipping ones already there at the same size.

    The skip is what makes a daily run cheap and re-runnable. Narration is
    content-addressed, so an object with this key already holds the audio for
    this exact script — there is no version of "the same key, different bytes"
    that is legitimate, only a truncated upload. Size is therefore the whole
    check: it catches the interrupted PUT (the only real corruption mode here)
    without downloading anything.

    ETag would be a stricter check and is not used deliberately: R2 returns a
    non-MD5 ETag for anything uploaded in multiple parts, so an ETag comparison
    would report a permanent false mismatch on exactly the largest files and
    re-upload them on every single run — the opposite of what this is for.
    """
    stats = {"uploaded": 0, "skipped": 0, "bytes": 0, "delta": 0, "failed": 0}
    if not folder.is_dir():
        print(f"note: {folder} does not exist — nothing to upload.", file=sys.stderr)
        return stats

    from botocore.exceptions import ClientError

    for path in sorted(folder.glob("*.mp3")):
        key = path.name
        size = path.stat().st_size
        remote: int | None = None
        try:
            head = client.head_object(Bucket=bucket, Key=key)
            remote = int(head.get("ContentLength") or 0)
        except ClientError as err:
            code = str((err.response.get("Error", {}) or {}).get("Code") or "")
            if code not in ("404", "NoSuchKey", "NotFound"):
                raise

        if remote == size:
            stats["skipped"] += 1
            continue

        ctype = _sniff(path)
        stats["bytes"] += size
        # A replaced object costs the difference, not the whole file — that is
        # what makes the projected bucket size below honest.
        stats["delta"] += size - (remote or 0)
        if dry_run:
            verb = "replace" if remote is not None else "upload"
            print(f"  would {verb} {key}  {size:,} B  {ctype}")
            stats["uploaded"] += 1
            continue

        try:
            with path.open("rb") as body:
                client.put_object(
                    Bucket=bucket, Key=key, Body=body,
                    ContentType=ctype, CacheControl=CACHE_CONTROL)
        except ClientError as err:
            # One clip failing is not a reason to abandon the rest — the same
            # judgement cli.cmd_narrate makes about one language failing.
            print(f"  ! {key}: {explain(err)}", file=sys.stderr)
            stats["failed"] += 1
            stats["bytes"] -= size
            stats["delta"] -= size - (remote or 0)
            continue
        stats["uploaded"] += 1
        print(f"  {'replaced' if remote is not None else 'uploaded'} {key}  "
              f"{size:,} B  {ctype}")
    return stats


# ── prune ──────────────────────────────────────────────────────────────────

def prune(client, bucket: str, listing: dict[str, dict], keep: set[str],
          grace_hours: float, dry_run: bool) -> dict:
    """Delete objects the site can no longer ask for. Three guards, all needed.

    1. **The keep-set comes from scripts.json**, never from the upload
       directory. See keep_set() — it is the mistake that would empty the
       bucket of every IPO not in today's batch.

    2. **Only asset-shaped keys are candidates** (ASSET_RE). Anything else in
       the bucket belongs to somebody else and is counted as kept.

    3. **Nothing newer than --grace-hours is touched.** Two runs can overlap:
       the daily job is scheduled and the studio's trigger panel can start
       another by hand, and a run that is still narrating has uploaded clips
       whose scripts are in ITS scripts.json but not necessarily in the copy
       this process read. Without the grace window, the pruning run deletes the
       narrating run's finished work while it is still going — and the loss is
       invisible, because the narrating run's own log says it uploaded them.
       24 hours is far longer than any run takes, and the cost of holding a
       stale clip one extra day is a few megabytes.

    A clip whose script changed leaves its old hash behind, so this is not a
    rare path: the 13 files measured on 30 Aug 2026 already contained three
    superseded pairs (r1 en/hi/te each present under two different hashes),
    which is 40% of that directory that no page can reach.
    """
    stats = {"pruned": 0, "kept": 0, "bytes": 0, "failed": 0}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=grace_hours)

    doomed: list[str] = []
    for key, meta in sorted(listing.items()):
        if key in keep or not ASSET_RE.match(key):
            stats["kept"] += 1
            continue
        when = meta.get("modified")
        if isinstance(when, datetime) and when > cutoff:
            stats["kept"] += 1
            print(f"  holding {key}  (uploaded {when:%Y-%m-%d %H:%M} UTC, "
                  f"inside the {grace_hours:g}h grace window)")
            continue
        doomed.append(key)
        stats["bytes"] += int(meta.get("size") or 0)

    if not doomed:
        return stats

    for key in doomed:
        print(f"  would delete {key}" if dry_run else f"  deleting {key}")
    if dry_run:
        stats["pruned"] = len(doomed)
        return stats

    from botocore.exceptions import ClientError

    # Batched, because DeleteObjects caps at 1000 keys and because 1000 keys in
    # one call is one Class A operation instead of a thousand.
    for start in range(0, len(doomed), DELETE_BATCH):
        chunk = doomed[start:start + DELETE_BATCH]
        try:
            result = client.delete_objects(
                Bucket=bucket,
                Delete={"Objects": [{"Key": k} for k in chunk], "Quiet": True})
        except ClientError as err:
            print(f"  ! delete batch failed: {explain(err)}", file=sys.stderr)
            stats["failed"] += len(chunk)
            continue
        # DeleteObjects reports per-key failures in the 200 body rather than by
        # status, so a batch can "succeed" while deleting nothing. Counting the
        # request as done would overstate the freed space in the summary.
        errors = result.get("Errors", []) or []
        for bad in errors:
            print(f"  ! {bad.get('Key')}: {bad.get('Code')} {bad.get('Message')}",
                  file=sys.stderr)
        stats["failed"] += len(errors)
        stats["pruned"] += len(chunk) - len(errors)
    return stats


# ── reporting ──────────────────────────────────────────────────────────────

def human(n: int) -> str:
    if abs(n) >= 1_000_000_000:
        return f"{n / 1_000_000_000:,.2f} GB"
    if abs(n) >= 1_000_000:
        return f"{n / 1_000_000:,.1f} MB"
    return f"{n:,} B"


def _resolve(raw: str) -> Path:
    """Absolute, or relative to wherever this is being run from.

    The documented invocation is `python tools/r2_sync.py --dir out/audio` with
    backend/ as the working directory, but the publish workflow runs from the
    repo root, so the same relative path has to work from one directory up. A
    path that does not exist as given is retried under backend/ before being
    reported missing.
    """
    path = Path(raw).expanduser()
    if path.is_absolute() or path.exists():
        return path
    fallback = store.BACKEND_ROOT / raw
    return fallback if fallback.exists() else path


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Mirror narration clips to Cloudflare R2 and prune what the "
                    "published site can no longer ask for.")
    ap.add_argument("--dir", default="out/audio",
                    help="directory of clips to upload (default: out/audio)")
    ap.add_argument("--scripts", default="out/scripts.json",
                    help="the script book that defines the keep-set; required "
                         "for --prune (default: out/scripts.json)")
    ap.add_argument("--prune", action="store_true",
                    help="delete bucket objects no script in --scripts can "
                         "produce. Read keep_set() before trusting this.")
    ap.add_argument("--dry-run", action="store_true",
                    help="print every upload and delete, change nothing")
    ap.add_argument("--drop-status", default="listed",
                    help="comma-separated IPO statuses whose clips are dead "
                         "weight and may be pruned (default: listed). An issue "
                         "that has already listed is finished work — nobody "
                         "records a GMP video about a stock that is trading — "
                         "so its reels only spend the 10 GB R2 allowance. Pass "
                         "an empty string to keep every status.")
    ap.add_argument("--grace-hours", type=float, default=24.0,
                    help="never delete an object newer than this, so a "
                         "concurrent narration run survives (default: 24)")
    args = ap.parse_args()

    folder = _resolve(args.dir)
    scripts = _resolve(args.scripts)

    # The keep-set is built BEFORE the client, so a missing or broken
    # scripts.json fails without having touched the bucket at all — and, more
    # usefully, fails the same way whether or not credentials happen to be
    # configured on this machine.
    keep: set[str] = set()
    if args.prune:
        try:
            drop = {p.strip().lower() for p in (args.drop_status or '').split(',')
                    if p.strip()}
            keep = keep_set(scripts, drop)
        except SyncError as err:
            print(f"error: {err}", file=sys.stderr)
            return 2
        print(f"keep-set: {len(keep)} clip name(s) derived from {scripts}")

    try:
        client, bucket = make_client()
    except SyncError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2

    prefix = "[dry run] " if args.dry_run else ""
    print(f"{prefix}{bucket} at {os.getenv(ENV_ENDPOINT)}")

    try:
        up = upload(client, bucket, folder, args.dry_run)
        listing = list_bucket(client, bucket)
        pr = {"pruned": 0, "kept": len(listing), "bytes": 0, "failed": 0}
        if args.prune:
            pr = prune(client, bucket, listing, keep, args.grace_hours,
                       args.dry_run)
    except Exception as err:  # noqa: BLE001 — turned into one line, then re-raised as exit code
        print(f"error: {explain(err)}", file=sys.stderr)
        return 1

    # The listing is taken after the uploads, so in a real run it already
    # includes them and only the pruned bytes have to come off. In a dry run
    # nothing happened, so the projection adds what would have been written
    # (the delta, not the file size, since a replaced object frees its old
    # bytes) and removes what would have been deleted.
    current = sum(int(m.get("size") or 0) for m in listing.values())
    projected = current - pr["bytes"] + (up["delta"] if args.dry_run else 0)
    objects = len(listing) - pr["pruned"] + (up["uploaded"] if args.dry_run else 0)

    print()
    print(f"{prefix}uploaded {up['uploaded']}, skipped {up['skipped']} already "
          f"present, pruned {pr['pruned']}, kept {pr['kept']}")
    print(f"{prefix}transferred {human(up['bytes'])}"
          + (f", freed {human(pr['bytes'])}" if pr["bytes"] else ""))
    print(f"bucket now {objects} object(s), {human(projected)} of "
          f"{human(FREE_TIER_BYTES)} free tier")
    if not args.prune:
        print("(--prune not given; nothing was deleted)")

    if projected > WARN_BYTES:
        print(f"\nwarning: {human(projected)} is past the {human(WARN_BYTES)} "
              f"mark on a {human(FREE_TIER_BYTES)} free tier. Storage is the "
              f"only R2 limit this project can realistically hit, and hitting "
              f"it fails the PUTs mid-run rather than sending a bill — run "
              f"with --prune, or start paying for the bucket.", file=sys.stderr)

    if up["failed"] or pr["failed"]:
        print(f"\n{up['failed']} upload(s) and {pr['failed']} delete(s) failed.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
