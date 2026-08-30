"""Prove a narration bucket is actually usable by the studio, before a run.

`ipopulse doctor` checks the DATA. This checks the PIPE the audio travels
down — and it exists because that pipe has one failure mode that looks like
success from every angle except the browser's.

    PUBLIC-READ IS NOT CORS.

Verified 30 Aug 2026: a world-readable Google Cloud Storage object with no
CORS policy returns no `Access-Control-Allow-Origin` header at all, and a
GitHub Release asset does the same (the github.com 302 and the
release-assets.githubusercontent.com 200 both send none, and OPTIONS on the
asset URL is a 404). `curl` fetches those objects perfectly. So does a browser
tab pointed straight at the URL. The bucket console says "public". And every
language on every reel is still silent, because studio.js does not point an
<audio> at the URL — it `fetch()`es the bytes, and a cross-origin fetch the
server never granted is blocked before the first byte arrives.

Why fetch and not <audio src>, since <audio src> would have played: the reel is
cut to the narration's REAL decoded duration, which means decodeAudioData has
to see the bytes; and capture.js routes the clip through
createMediaElementSource, which emits SILENCE for a cross-origin element the
server never granted — a recording that looks right and has no voice on it.
Both of those are the reason the bucket exists at all, so "just use <audio>"
gives up the feature and keeps the bug.

Hence the shape of this tool. Every check below is answerable from a shell, and
the one that matters — check 6 — is the one nobody thinks to run, because the
five before it all pass on a bucket that cannot serve this site.

    python tools/r2_doctor.py
    python tools/r2_doctor.py --origin https://nisanth2003.github.io
    python tools/r2_doctor.py --keep-probe        # leave the object behind

Reads the same variables narrate.yml passes as secrets, so a green run here is
a statement about what the workflow will do, not about this laptop.
"""

from __future__ import annotations

import argparse
import secrets
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os  # noqa: E402

from ipopulse.cli import _force_utf8_stdout, load_dotenv  # noqa: E402

# The keys live in .env and nothing else under tools/ reads it. `ipopulse ...`
# gets this for free from its own entry point; a standalone script does not, and
# without it every check below reports "not configured" on a machine that is in
# fact configured — the most confusing possible false negative for a tool whose
# whole job is telling you whether you are configured.
#
# load_dotenv uses setdefault, so a real environment variable always wins over
# the file. That ordering matters here: it is how you point this at a second
# bucket for one run without editing .env.
load_dotenv()


# ── configuration ──────────────────────────────────────────────────────────
#
# Deliberately the IPOPULSE_-prefixed names only, with no fallback to the plain
# AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY pair that narrate.yml maps them onto
# inside the job. A fallback would read whatever AWS credentials happen to be on
# the developer's machine and report a healthy bucket that the workflow has no
# access to whatsoever — the tool would be answering a question nobody asked.

BUCKET_VAR = "IPOPULSE_S3_BUCKET"
ENDPOINT_VAR = "IPOPULSE_S3_ENDPOINT"
KEY_ID_VAR = "IPOPULSE_S3_ACCESS_KEY_ID"
SECRET_VAR = "IPOPULSE_S3_SECRET_ACCESS_KEY"
REGION_VAR = "IPOPULSE_S3_REGION"
BASE_VAR = "IPOPULSE_AUDIO_BASE"

# R2 ignores the region but botocore refuses to sign without one. "auto" is what
# Cloudflare documents and what narrate.yml defaults to; keeping the same string
# here means a mismatch is impossible rather than merely unlikely.
DEFAULT_REGION = "auto"

# Scheme + host, no path. That is exactly what a browser puts in the `Origin`
# header — the site lives at /IPO_PULSE/ but the path is never sent, so a CORS
# rule naming the full page URL matches nothing and is a common way to "have
# CORS configured" and still be blocked.
DEFAULT_ORIGIN = "https://nisanth2003.github.io"

# One silent MPEG-1 Layer III frame: a valid frame header (0xFFFB) plus a
# zero-filled payload. It is not meant to be listened to — the object exists so
# there is something real to GET, and to prove the Content-Type survives a
# round trip. Small enough that a failed cleanup costs nothing.
PROBE_BYTES = b"\xff\xfb\x90\x64" + bytes(400)
PROBE_TYPE = "audio/mpeg"

# Long enough for a cold R2 edge, short enough that a wrong hostname fails in
# under a minute instead of hanging until someone gives up on the tool.
HTTP_TIMEOUT = 20

# Some CDNs answer python-urllib's default UA with a 403 that reads exactly like
# a permissions problem, which would send you to the bucket policy to fix a
# problem that is not there.
USER_AGENT = "ipopulse-r2-doctor/1.0"


# ── reporting ──────────────────────────────────────────────────────────────
#
# Same glyph vocabulary as `ipopulse doctor`: ✓ is fine, ✗ breaks something,
# ⚠ is worth reading, · is context. A failure always carries a fix line, because
# "public read: FAIL" without the next step is a bug report, not a doctor.

class Report:
    def __init__(self) -> None:
        self.failed: list[str] = []
        self.warned: list[str] = []
        self.passed: list[str] = []

    def section(self, n: int, title: str) -> None:
        print(f"\n── {n}. {title}")

    def ok(self, label: str, detail: str = "") -> None:
        self.passed.append(label)
        print(f"  ✓ PASS  {label}" + (f" — {detail}" if detail else ""))

    def fail(self, label: str, detail: str, *fix: str) -> None:
        self.failed.append(label)
        print(f"  ✗ FAIL  {label} — {detail}")
        for i, line in enumerate(fix):
            print(f"          fix: {line}" if i == 0 else f"               {line}")

    def warn(self, label: str, detail: str, *notes: str) -> None:
        self.warned.append(label)
        print(f"  ⚠ WARN  {label} — {detail}")
        for line in notes:
            print(f"          {line}")

    def info(self, text: str) -> None:
        print(f"  · {text}")

    def skip(self, label: str, why: str) -> None:
        # Not a pass and not a failure: a check that could not run tells you
        # nothing, and counting it either way would lie about coverage.
        print(f"  – SKIP  {label} — {why}")


# ── HTTP helpers ───────────────────────────────────────────────────────────

def _fetch(url: str, *, origin: str | None = None, method: str = "GET",
           extra: dict[str, str] | None = None):
    """Plain HTTP with NO credentials, deliberately.

    boto3 would sign this request and it would succeed against a bucket that is
    entirely private — which is the opposite of what checks 5-7 are asking. The
    browser has no keys, so neither does this.

    Returns (status, headers, body) for any HTTP answer including 4xx, and
    raises only when there was no answer at all (DNS, TLS, timeout). A 403 is
    data here, not an exception: it is one of the specific outcomes the fix
    lines distinguish between.
    """
    req = urllib.request.Request(url, method=method)
    req.add_header("User-Agent", USER_AGENT)
    if origin:
        req.add_header("Origin", origin)
    for key, val in (extra or {}).items():
        req.add_header(key, val)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        # HTTPError IS the response — headers included, which is the point: a
        # server can send ACAO on a 403, and knowing that separates "CORS is
        # wrong" from "the object is not readable".
        return exc.code, dict(exc.headers or {}), exc.read() if exc.fp else b""


def _same_host(a: str, b: str) -> bool:
    return urlparse(a).netloc.lower() == urlparse(b).netloc.lower()


# ── the checks ─────────────────────────────────────────────────────────────

def run(origin: str, keep_probe: bool) -> int:
    rep = Report()
    print(f"R2 doctor — checking the narration bucket against {origin}")

    # 1. Env present ────────────────────────────────────────────────────────
    rep.section(1, "Configuration")
    cfg = {
        BUCKET_VAR: (os.getenv(BUCKET_VAR) or "").strip(),
        ENDPOINT_VAR: (os.getenv(ENDPOINT_VAR) or "").strip(),
        KEY_ID_VAR: (os.getenv(KEY_ID_VAR) or "").strip(),
        SECRET_VAR: (os.getenv(SECRET_VAR) or "").strip(),
        BASE_VAR: (os.getenv(BASE_VAR) or "").strip(),
    }
    region = (os.getenv(REGION_VAR) or "").strip() or DEFAULT_REGION
    missing = [name for name, val in cfg.items() if not val]

    if missing:
        # Named one per line rather than as a list, because the fix is one .env
        # line per variable and a comma-separated blob invites missing one.
        rep.fail("environment", f"{len(missing)} variable(s) not set",
                 "add these to the repo-root .env (one per line):")
        for name in missing:
            print(f"               {name}=")
        print("          The first four come from the R2 API token you create in")
        print("          the Cloudflare dashboard (R2 → Manage API tokens); the")
        print("          endpoint is https://<account-id>.r2.cloudflarestorage.com")
        print(f"          and {BASE_VAR} is the PUBLIC base URL with a trailing '/'")
        print("          (the r2.dev address, or your custom domain).")
    else:
        rep.ok("environment", "all five variables set")
    rep.info(f"{REGION_VAR:<30} {region}"
             + ("  (default)" if not (os.getenv(REGION_VAR) or "").strip() else ""))
    for name in (BUCKET_VAR, ENDPOINT_VAR, BASE_VAR):
        if cfg[name]:
            rep.info(f"{name:<30} {cfg[name]}")
    if cfg[KEY_ID_VAR]:
        # Enough to tell two tokens apart, not enough to be a leak in a log
        # someone pastes into an issue.
        rep.info(f"{KEY_ID_VAR:<30} {cfg[KEY_ID_VAR][:4]}…{cfg[KEY_ID_VAR][-4:]}")

    have_creds = not [n for n in (BUCKET_VAR, ENDPOINT_VAR, KEY_ID_VAR, SECRET_VAR)
                      if not cfg[n]]

    bucket, endpoint = cfg[BUCKET_VAR], cfg[ENDPOINT_VAR]
    base = cfg[BASE_VAR]
    client = None
    probe_key: str | None = None
    # Bound before the try because the cleanup block reads them, and an
    # exception raised in check 2 would otherwise turn a bucket problem into a
    # NameError from the handler — hiding the finding under a traceback.
    base_usable = False
    content_type = ""

    try:
        # 2. Credentials work ───────────────────────────────────────────────
        rep.section(2, "Credentials")
        if not have_creds:
            rep.skip("HeadBucket", "credentials not configured (see 1)")
        else:
            try:
                import boto3
                from botocore.config import Config
                from botocore.exceptions import BotoCoreError, ClientError
            except ImportError:
                rep.fail("boto3", "not importable",
                         "pip install boto3  (it is in backend/requirements.txt)")
                return _finish(rep)

            client = boto3.client(
                "s3",
                endpoint_url=endpoint,
                region_name=region,
                aws_access_key_id=cfg[KEY_ID_VAR],
                aws_secret_access_key=cfg[SECRET_VAR],
                # R2 speaks SigV4 only, and short timeouts because a wrong
                # endpoint hostname otherwise sits in botocore's default retry
                # ladder for the better part of a minute per call.
                config=Config(signature_version="s3v4",
                              retries={"max_attempts": 2},
                              connect_timeout=10, read_timeout=20),
            )
            try:
                client.head_bucket(Bucket=bucket)
                rep.ok("HeadBucket", f"reached '{bucket}'")
            except ClientError as exc:
                # The three outcomes need three different fixes, and the raw
                # botocore message distinguishes none of them for a reader.
                code = str(exc.response.get("Error", {}).get("Code", "")).strip()
                status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                if code in ("404", "NoSuchBucket") or status == 404:
                    rep.fail("HeadBucket", f"no bucket named '{bucket}' at this endpoint",
                             f"create it, or correct {BUCKET_VAR} — the name is the",
                             "bucket alone, with no s3:// prefix and no path")
                elif code in ("InvalidAccessKeyId", "SignatureDoesNotMatch"):
                    rep.fail("HeadBucket", f"the endpoint rejected the key ({code})",
                             f"re-issue the R2 API token and update {KEY_ID_VAR}",
                             f"and {SECRET_VAR} — an R2 token shows its secret once")
                elif code in ("403", "AccessDenied") or status == 403:
                    rep.fail("HeadBucket", "the key is valid but has no access to this bucket",
                             "give the token Object Read & Write on this bucket",
                             "(a token scoped to a different bucket looks exactly like this)")
                else:
                    rep.fail("HeadBucket", f"{code or 'error'}: {exc}",
                             "check the endpoint URL and the bucket name")
                client = None
            except BotoCoreError as exc:
                # No HTTP answer at all — DNS, TLS, proxy, offline. Nothing
                # below can mean anything, so say so rather than cascading.
                rep.fail("HeadBucket", f"could not reach {endpoint} ({type(exc).__name__})",
                         f"check {ENDPOINT_VAR} is the account endpoint",
                         "https://<account-id>.r2.cloudflarestorage.com — with no",
                         "bucket name appended — and that this machine is online")
                client = None

        # 3. Write ──────────────────────────────────────────────────────────
        rep.section(3, "Write access")
        if client is None:
            rep.skip("PutObject", "no working client (see 2)")
        else:
            # Random suffix so two people running this at once cannot delete
            # each other's probe, and the '_' prefix keeps it sorting away from
            # the real clips if a cleanup ever does fail.
            probe_key = f"_r2doctor-probe-{secrets.token_hex(4)}.mp3"
            try:
                client.put_object(
                    Bucket=bucket, Key=probe_key, Body=PROBE_BYTES,
                    ContentType=PROBE_TYPE,
                    # Same header narrate.yml sets. Written here too so the
                    # probe exercises the identical request shape rather than a
                    # simplified one that might be permitted when the real one
                    # is not.
                    CacheControl="public, max-age=31536000, immutable",
                )
                rep.ok("PutObject", f"wrote {probe_key} ({len(PROBE_BYTES)} bytes, {PROBE_TYPE})")
            except Exception as exc:  # noqa: BLE001 — any failure means no upload
                rep.fail("PutObject", f"could not write to '{bucket}' ({exc})",
                         "the R2 API token needs Object Read & Write, not Read only",
                         "— narrate.yml uploads with these same credentials")
                probe_key = None

        # 4. The public base agrees with the bucket ──────────────────────────
        #
        # A base pointing at a DIFFERENT bucket is the quietest failure in the
        # whole chain: narrate.yml uploads happily, this tool's write check
        # passes, and the site 404s on every clip while every log says success.
        rep.section(4, f"{BASE_VAR} points at this bucket")
        base_usable = False
        if not base:
            rep.skip("base URL", "not set (see 1)")
        elif not base.startswith(("http://", "https://")):
            # 'audio/' is the legitimate relative default (see cli.py
            # _audio_base) — it means the site reads clips mirrored into Pages,
            # not from a bucket. Correct, but not what this tool is checking.
            rep.fail("base URL", f"'{base}' is relative, not an absolute URL",
                     "a relative base means the site reads frontend/audio/ from",
                     "its own origin and ignores the bucket entirely; set it to",
                     "the public https:// base if the bucket is meant to serve")
        elif not base.endswith("/"):
            rep.fail("base URL", f"'{base}' has no trailing slash",
                     "the site appends the filename directly, so without the",
                     f"slash every URL is malformed: set {BASE_VAR}={base}/")
        else:
            base_usable = True
            host = urlparse(base).netloc
            # Three shapes are all legitimate, so this can only ever be a
            # warning: <bucket>.<account>.r2.dev, a custom domain that mentions
            # nothing, or an endpoint-style URL with the bucket in the path.
            if bucket and bucket.lower() in base.lower():
                rep.ok("base URL", f"names bucket '{bucket}' — {host}")
            elif host.lower().endswith(".r2.dev"):
                # R2's public development URL is pub-<hash>.r2.dev — it never
                # carries the bucket name, so demanding one here would fire a
                # scary warning on the single most common correct setup. The
                # byte-identical fetch in check 5 is the real proof, and it is
                # a better one than a string match could ever be.
                rep.ok("base URL", f"{host} — an r2.dev public URL "
                                   "(check 5 confirms which bucket it serves)")
            elif endpoint and _same_host(base, endpoint):
                # The S3 API endpoint is NOT a public read endpoint: it answers
                # only signed requests, so this will show up again as a 401 in
                # check 5. Say it here, where the fix is obvious.
                rep.fail("base URL", "points at the S3 API endpoint, not a public URL",
                         "the API endpoint serves signed requests only; enable the",
                         "r2.dev public URL for the bucket or attach a custom",
                         f"domain, and set {BASE_VAR} to that instead")
                base_usable = False
            else:
                rep.warn("base URL", f"cannot confirm {host} serves '{bucket}'",
                         "that is expected for a custom domain, and checks 5-7",
                         "settle it either way — but if this is a leftover URL",
                         "from another bucket the site 404s on every single clip",
                         "while every upload log still says success.")

        public_url = f"{base}{probe_key}" if (base_usable and probe_key) else None

        # 5. Public read ────────────────────────────────────────────────────
        rep.section(5, "Public read (no credentials)")
        read_ok = False
        content_type = ""
        if not public_url:
            rep.skip("public GET", "no probe object or no usable base URL")
        else:
            rep.info(f"GET {public_url}")
            try:
                status, headers, body = _fetch(public_url)
            except Exception as exc:  # noqa: BLE001 — no HTTP answer at all
                status, headers, body = 0, {}, b""
                rep.fail("public GET", f"no response ({type(exc).__name__}: {exc})",
                         f"check the host in {BASE_VAR} resolves and serves HTTPS")
            if status in (401, 403):
                rep.fail("public GET", f"HTTP {status} — the object is not world-readable",
                         "enable public access on the bucket (R2 → Settings → Public",
                         "Development URL / r2.dev), or attach a custom domain and",
                         f"point {BASE_VAR} at it. The browser sends no credentials,",
                         "so a bucket that only answers signed requests cannot serve",
                         "this site at all.")
            elif status == 404:
                rep.fail("public GET", "HTTP 404 — the object is not at this base URL",
                         f"the write in check 3 succeeded, so {BASE_VAR} is serving a",
                         "different bucket (or a subpath): make the base resolve to",
                         f"the root of '{bucket}'")
            elif status and 200 <= status < 300:
                content_type = (headers.get("Content-Type")
                                or headers.get("content-type") or "").split(";")[0].strip()
                read_ok = body == PROBE_BYTES
                detail = f"HTTP {status}, {len(body)} bytes"
                if read_ok:
                    # Byte-identical is also the answer to check 4: the object
                    # was written through the API to THIS bucket and came back
                    # off the public base, so the two are the same bucket. No
                    # URL-shape heuristic can establish that.
                    rep.ok("public GET", detail + " — byte-identical, so the base "
                                                  f"does serve '{bucket}'")
                else:
                    # A 200 whose body is not the probe means something is
                    # intercepting: a login wall or an SPA index.html, both of
                    # which decode to noise rather than failing loudly.
                    rep.fail("public GET", detail + " — body is not the probe",
                             "something other than the bucket answered (a login",
                             "page or a catch-all index.html returns 200 too)")
            elif status:
                rep.fail("public GET", f"HTTP {status}",
                         "not a permissions answer — check the host is the bucket's")

        # 6. CORS — the check this tool exists for ───────────────────────────
        rep.section(6, "CORS — Access-Control-Allow-Origin")
        cors_json = ('[{"AllowedOrigins": ["' + origin + '"], '
                     '"AllowedMethods": ["GET","HEAD"], '
                     '"AllowedHeaders": ["*"], "MaxAgeSeconds": 3600}]')
        if not public_url:
            rep.skip("ACAO on GET", "nothing fetchable to test")
        else:
            rep.info(f"GET with Origin: {origin}")
            try:
                status, headers, _ = _fetch(public_url, origin=origin)
            except Exception as exc:  # noqa: BLE001
                status, headers = 0, {}
                rep.fail("ACAO on GET", f"no response ({type(exc).__name__})", "see check 5")
            acao = next((v for k, v in headers.items()
                         if k.lower() == "access-control-allow-origin"), "")
            if not status:
                pass
            elif not acao:
                # THE failure. Everything above can be green and the studio is
                # still silent in every language on every reel.
                rep.fail("ACAO on GET",
                         "no Access-Control-Allow-Origin header — the browser will "
                         "block this fetch",
                         "public read is NOT CORS: this object is readable and still",
                         "unusable. Set the bucket's CORS policy to exactly:",
                         "", "  " + cors_json, "",
                         "In Cloudflare: R2 → the bucket → Settings → CORS Policy.",
                         "Without it studio.js gets a network error instead of bytes,",
                         "and every reel plays silent in every language.")
            elif acao == "*" or acao.rstrip("/").lower() == origin.rstrip("/").lower():
                rep.ok("ACAO on GET", f"Access-Control-Allow-Origin: {acao}")
                vary = next((v for k, v in headers.items() if k.lower() == "vary"), "")
                if acao != "*" and "origin" not in vary.lower():
                    # Not fatal here — it is a caching correctness issue, and it
                    # bites through a shared CDN cache rather than on this GET.
                    rep.warn("Vary", "the echoed ACAO is not accompanied by 'Vary: Origin'",
                             "a shared cache can serve one origin's header to another;",
                             "harmless with a single origin, worth knowing with two")
            else:
                rep.fail("ACAO on GET",
                         f"header is '{acao}', which does not match {origin}",
                         "the browser compares scheme+host exactly — no path, no",
                         "trailing slash, and http:// never matches https://.",
                         "Set the policy to:", "", "  " + cors_json)

        # Preflight is INFORMATIONAL, not fatal, and that is not a hedge.
        # studio.js issues a plain GET with no custom headers, which is a CORS
        # "simple request": the browser sends it straight out and never asks
        # OPTIONS first. So a bucket that 404s the preflight still works fine
        # here — reporting it as a failure would send someone hunting a problem
        # that has no effect on this site. It is printed because it is a useful
        # signal about whether a CORS policy exists at all.
        if public_url:
            try:
                status, headers, _ = _fetch(
                    public_url, origin=origin, method="OPTIONS",
                    extra={"Access-Control-Request-Method": "GET"})
                allow = next((v for k, v in headers.items()
                              if k.lower() == "access-control-allow-methods"), "")
                acao = next((v for k, v in headers.items()
                             if k.lower() == "access-control-allow-origin"), "")
                if 200 <= status < 300 and acao:
                    rep.info(f"preflight OPTIONS: {status}, allows "
                             f"{allow or 'unspecified'} (informational)")
                else:
                    rep.info(f"preflight OPTIONS: {status}"
                             f"{'' if acao else ', no ACAO'} — informational only, "
                             "a simple GET needs no preflight")
            except Exception as exc:  # noqa: BLE001
                rep.info(f"preflight OPTIONS: no response ({type(exc).__name__}) "
                         "— informational only")

        # 7. Content-Type round-trip ────────────────────────────────────────
        #
        # decodeAudioData sniffs the bytes and would cope, but the <audio>
        # fallback path and Safari both take the header seriously, and a bucket
        # that rewrites the type is telling you the upload's ContentType was
        # dropped — which is a bug in the upload, not just a cosmetic header.
        rep.section(7, "Content-Type round-trip")
        if not public_url:
            rep.skip("Content-Type", "no successful public GET")
        elif not content_type:
            rep.warn("Content-Type", "the response carried no Content-Type header",
                     "browsers then guess from the bytes; usually survivable,",
                     "but nothing about it is guaranteed")
        elif content_type.lower() in ("audio/mpeg", "audio/mp3"):
            rep.ok("Content-Type", content_type)
        elif content_type.lower() in ("application/octet-stream", "binary/octet-stream"):
            rep.fail("Content-Type", f"'{content_type}' — the uploaded type was dropped",
                     "the object was PUT as audio/mpeg and came back generic:",
                     "the bucket or the CDN in front of it is overriding it")
        else:
            rep.fail("Content-Type", f"'{content_type}' is not audio",
                     "text/html here means a proxy or error page answered, not",
                     "the object — check the base URL host")

    finally:
        # 8. Cleanup ────────────────────────────────────────────────────────
        #
        # In `finally` because a probe left behind by a crash is a stray object
        # in the bucket the site derives its filenames from — harmless to
        # playback, but it accumulates once per failed run, and the one run most
        # likely to fail is the first one someone ever does.
        print("\n── 8. Cleanup")
        if probe_key is None:
            rep.info("nothing to clean up")
        elif keep_probe:
            rep.info(f"kept {probe_key} (--keep-probe) — delete it by hand when done")
            if base_usable:
                rep.info(f"       {base}{probe_key}")
        else:
            try:
                client.delete_object(Bucket=bucket, Key=probe_key)
                rep.info(f"deleted {probe_key}")
            except Exception as exc:  # noqa: BLE001
                # A warning, not a failure: the checks that matter already ran,
                # and the fix is a one-click delete in the dashboard.
                rep.warn("cleanup", f"could not delete {probe_key} ({exc})",
                         "remove it from the bucket by hand")

    return _finish(rep)


def _finish(rep: Report) -> int:
    """Summary, and the one instruction that follows from a clean run."""
    print("\n── summary")
    print(f"  {len(rep.passed)} passed, {len(rep.failed)} failed, {len(rep.warned)} warning(s)")
    if rep.failed:
        for label in rep.failed:
            print(f"  ✗ {label}")
        print("\nThe bucket is not ready. Fix the ✗ lines above and re-run —")
        print("a narration run against this bucket would upload fine and play")
        print("nothing, which is the failure this tool exists to prevent.")
        return 1

    print("  ✓ the bucket is public, CORS-enabled and typed correctly —")
    print("    studio.js can fetch and decode these bytes.")
    print(f"\nNext: on GitHub, set the {BASE_VAR} repo variable to the public base URL")
    print("(with the trailing '/') and add the four IPOPULSE_S3_* secrets — "
          "BUCKET,")
    print("ENDPOINT, ACCESS_KEY_ID, SECRET_ACCESS_KEY. narrate.yml then uploads to")
    print("the bucket instead of the Release, and publish.yml stops mirroring.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Check that the narration bucket is public, CORS-enabled "
                    "and correctly typed — before a narration run needs it.",
        epilog="Public-read is NOT CORS: a bucket can be perfectly public and "
               "still leave every reel silent. Check 6 is the one that matters.")
    ap.add_argument("--origin", default=DEFAULT_ORIGIN,
                    help="the browser origin to test CORS against, scheme+host "
                         f"only (default: {DEFAULT_ORIGIN})")
    ap.add_argument("--keep-probe", action="store_true",
                    help="leave the probe object in the bucket, e.g. to retry a "
                         "fetch by hand from a real browser console")
    args = ap.parse_args(argv)

    # Borrowed from cli.py rather than re-solved: a Windows console defaults to
    # cp1252, which cannot encode the ✓/✗/── this report is built from, and the
    # UnicodeEncodeError lands on the first line — so the tool that tells you
    # what is wrong with the bucket would instead die before saying anything.
    _force_utf8_stdout()

    # Normalised the way a browser sends it: no path, no trailing slash. A user
    # who pastes the full studio URL should still get a meaningful answer.
    parts = urlparse(args.origin)
    origin = f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc \
        else args.origin.rstrip("/")

    return run(origin, args.keep_probe)


if __name__ == "__main__":
    raise SystemExit(main())
