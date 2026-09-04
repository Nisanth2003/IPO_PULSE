"""Putting a finished video on the channel. The last step that wasn't automated.

`providers/youtube.py` reads what the channel has already published, keylessly.
This is the other direction, and it is a different animal: uploading needs
OAuth, because it acts *as the channel owner* rather than as a member of the
public.

── The one thing that cannot be automated, and why that is fine ───────────

Google will not issue a token for an account without that account's owner
clicking "allow" in a browser, once. There is no key, no service account and
no header that substitutes for it — a service account is a robot identity and
cannot own a YouTube channel.

So the shape is: **one consent, then forever.** `authorise()` is run by hand a
single time; it stores a refresh token, and `credentials()` turns that into a
working access token unattended from then on. A refresh token does not expire
on a schedule — it survives until it is revoked, the password changes, or it
goes six months unused. A pipeline that uploads daily never trips any of those.

── No google-auth-oauthlib ────────────────────────────────────────────────

That library is the usual way to do this and it is not installed here. It is
also not needed: the installed-app flow is a redirect to Google, a
`?code=` coming back to a loopback address, and one POST to exchange it. That
is implemented below in about forty lines against `urllib`, which means one
fewer dependency to pin in a container later.

── What is deliberately NOT here ──────────────────────────────────────────

**Nothing in this module decides to publish.** It uploads what it is handed.
The decision — which video, what visibility, and whether the numbers on it are
right — belongs to `pubqueue.py`, where a human approves it. Splitting those
two apart is the point: a bug in a render loop should not be able to reach the
channel, and it cannot, because the only path to `upload()` runs through an
approval somebody gave.

Visibility defaults to **unlisted**, never public. A wrong figure in an
unlisted video is an embarrassment; in a public one on a finance channel it is
somebody's money. Going public is an explicit choice made at approval time.
"""

from __future__ import annotations

import json
import os
import secrets
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from .sheets import BACKEND_ROOT, CACHE_DIR

# Upload plus the read scope, so the same token can confirm what landed.
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"

# Where the refresh token lives. Under .cache, which is gitignored — this is
# a credential and must never reach the repo.
TOKEN_FILE = CACHE_DIR / "youtube-token.json"

# The OAuth client. A **Desktop app** client from Google Cloud Console, with
# the YouTube Data API v3 enabled on the project. Not a Web client: a web
# client refuses a loopback redirect, which is exactly what the flow below
# uses.
CLIENT_FILE_ENV = "YOUTUBE_CLIENT_SECRET"

# 27 is "Education", 25 is "News & Politics". News fits a daily market
# briefing better and is what comparable channels use.
CATEGORY_NEWS = "25"


class NotAuthorised(RuntimeError):
    """No stored token, or it no longer works."""


def _client() -> dict[str, str]:
    """The OAuth client id and secret.

    Env vars win over the downloaded file, because the two places this runs
    disagree by nature: on a desktop the natural form is the JSON Google
    hands you, and in CI a secret can only be a string. Same reasoning as
    `GOOGLE_SHEETS_KEY` accepting either a path or the contents.
    """
    if os.getenv("YOUTUBE_CLIENT_ID"):
        return {"client_id": os.environ["YOUTUBE_CLIENT_ID"].strip(),
                "client_secret": (os.getenv("YOUTUBE_CLIENT_SECRET")
                                  or "").strip()}
    raw = os.getenv(CLIENT_FILE_ENV) or ""
    candidates = [Path(raw)] if raw else []
    candidates += [BACKEND_ROOT / "client_secret.json",
                   BACKEND_ROOT.parent / "client_secret.json"]
    for path in candidates:
        if path and path.is_file():
            blob = json.loads(path.read_text(encoding="utf-8"))
            # Console exports the credentials nested under "installed" for a
            # desktop client and "web" for a web client. Accept either shape
            # and let the redirect fail loudly if it is the wrong kind.
            node = blob.get("installed") or blob.get("web") or blob
            if node.get("client_id"):
                return {"client_id": node["client_id"],
                        "client_secret": node.get("client_secret", "")}
    raise NotAuthorised(
        "No OAuth client found. In Google Cloud Console: enable the YouTube "
        "Data API v3, create an OAuth client of type 'Desktop app', download "
        "the JSON, and save it as backend/client_secret.json (or point "
        f"{CLIENT_FILE_ENV} at it).")


def configured() -> bool:
    try:
        _client()
        return True
    except NotAuthorised:
        return False


def authorised() -> bool:
    return bool(os.getenv("YOUTUBE_REFRESH_TOKEN")) or TOKEN_FILE.is_file()


class _Catch(BaseHTTPRequestHandler):
    """Catches the one redirect Google sends back."""

    code: str | None = None
    state: str = ""

    def do_GET(self):                                  # noqa: N802
        query = urllib.parse.parse_qs(
            urllib.parse.urlparse(self.path).query)
        ok = query.get("state", [""])[0] == _Catch.state
        _Catch.code = query.get("code", [None])[0] if ok else None
        body = ("<h2>Authorised.</h2><p>You can close this tab and go back to "
                "the terminal.</p>" if _Catch.code else
                "<h2>Something went wrong.</h2><p>No code came back. Try "
                "again.</p>")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *_):                         # noqa: N802
        pass                                           # do not print requests


def authorise(port: int = 8765) -> str:
    """The one-time consent. Opens a browser; you click allow.

    Returns the channel title it ended up connected to, so a wrong-account
    mistake is caught immediately rather than after a video lands somewhere
    unexpected.

    `state` is a random nonce checked on the way back — without it, anything
    on this machine could hit the loopback port with its own code.
    """
    client = _client()
    _Catch.state = secrets.token_urlsafe(24)
    _Catch.code = None
    redirect = f"http://localhost:{port}/"

    url = AUTH_URI + "?" + urllib.parse.urlencode({
        "client_id": client["client_id"],
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "state": _Catch.state,
        # offline + consent is what actually yields a refresh token. Without
        # `prompt=consent` Google reuses a prior grant and returns an access
        # token only — the flow appears to succeed and then nothing works
        # unattended tomorrow.
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    })

    print("Opening your browser to authorise the channel.")
    print("If it does not open, paste this in yourself:\n")
    print(f"  {url}\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass

    server = HTTPServer(("localhost", port), _Catch)
    server.timeout = 300
    print(f"Waiting for the redirect on {redirect} …")
    while _Catch.code is None:
        server.handle_request()
        if _Catch.code is None:
            raise NotAuthorised("No authorisation code came back.")
    server.server_close()

    payload = urllib.parse.urlencode({
        "code": _Catch.code,
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
        "redirect_uri": redirect,
        "grant_type": "authorization_code",
    }).encode()
    with urllib.request.urlopen(
            urllib.request.Request(TOKEN_URI, data=payload), timeout=60) as r:
        tok = json.loads(r.read())

    if not tok.get("refresh_token"):
        raise NotAuthorised(
            "Google returned no refresh token, so this would stop working "
            "tomorrow. Revoke this app at myaccount.google.com/permissions "
            "and run it again.")

    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps({
        "refresh_token": tok["refresh_token"],
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
        "scopes": SCOPES,
    }, indent=1), encoding="utf-8")
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass

    return whoami()


def credentials():
    """A usable credential from the stored refresh token. Unattended.

    `YOUTUBE_REFRESH_TOKEN` is checked first so this works on a runner with
    no filesystem to have authorised on. Note what that secret is: a
    refresh token for the channel, which can upload, edit and delete videos
    until it is revoked. It is a channel-takeover credential, and it belongs
    in far fewer places than a read-only API key.
    """
    env_token = (os.getenv("YOUTUBE_REFRESH_TOKEN") or "").strip()
    if env_token:
        client = _client()
        blob = {"refresh_token": env_token,
                "client_id": client["client_id"],
                "client_secret": client["client_secret"],
                "scopes": SCOPES}
    elif TOKEN_FILE.is_file():
        blob = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    else:
        raise NotAuthorised(
            "Not authorised yet. Run `ipopulse publish --authorise` once, "
            "or set YOUTUBE_REFRESH_TOKEN.")
    from google.oauth2.credentials import Credentials
    return Credentials(
        token=None,
        refresh_token=blob["refresh_token"],
        client_id=blob["client_id"],
        client_secret=blob["client_secret"],
        token_uri=TOKEN_URI,
        scopes=blob.get("scopes") or SCOPES,
    )


def _service():
    from googleapiclient.discovery import build
    return build("youtube", "v3", credentials=credentials(),
                 cache_discovery=False)


def whoami() -> str:
    """The channel this token is attached to."""
    res = _service().channels().list(part="snippet", mine=True).execute()
    items = res.get("items") or []
    if not items:
        return "(no channel on this account)"
    return items[0]["snippet"]["title"]


def upload(video: Path, title: str, description: str,
           tags: list[str] | None = None, privacy: str = "unlisted",
           thumbnail: Path | None = None,
           category: str = CATEGORY_NEWS,
           on_progress=None) -> dict[str, Any]:
    """Upload one video. Returns {id, url, privacy}.

    Resumable, in 4 MB chunks, because a 60-second Short is a few megabytes
    and a dropped connection on a home line should resume rather than restart.

    `privacy` is passed through rather than defaulted here — the caller has
    already decided, and a default buried in an upload function is the wrong
    place for that decision to live.
    """
    from googleapiclient.http import MediaFileUpload

    video = Path(video)
    if not video.is_file():
        raise FileNotFoundError(f"no such video: {video}")
    if privacy not in ("private", "unlisted", "public"):
        raise ValueError(f"bad privacy: {privacy}")

    body = {
        "snippet": {
            # YouTube truncates a title at 100 characters and a description at
            # 5000, silently. Trimmed here so what is stored in the queue is
            # what actually appears.
            "title": title[:100],
            "description": description[:5000],
            "tags": [t.lstrip("#") for t in (tags or [])][:40],
            "categoryId": category,
            "defaultLanguage": "en",
        },
        "status": {
            "privacyStatus": privacy,
            # Required since 2020 and rejected if absent. False: this is
            # financial commentary for adults, and a "made for kids" flag
            # would disable comments and end screens for no reason.
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(str(video), chunksize=4 * 1024 * 1024,
                            resumable=True, mimetype="video/mp4")
    request = _service().videos().insert(
        part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status and on_progress:
            on_progress(int(status.progress() * 100))

    vid = response["id"]
    out = {"id": vid, "url": f"https://www.youtube.com/shorts/{vid}",
           "privacy": privacy, "thumbnail": False}

    if thumbnail and Path(thumbnail).is_file():
        try:
            _service().thumbnails().set(
                videoId=vid, media_body=str(thumbnail)).execute()
            out["thumbnail"] = True
        except Exception as exc:
            # A custom thumbnail needs a verified channel. Not fatal — the
            # video is already up and YouTube picks a frame — but worth
            # saying, because silently getting an auto-frame instead of the
            # designed thumbnail undoes the whole point of designing one.
            out["thumbnail_error"] = str(exc)[:200]
    return out
