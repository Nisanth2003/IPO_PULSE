"""The pictures: a title card to open each reel, and a card to close it.

Two images per reel video, both generated once and then never again — which
is the point. A reel is re-rendered whenever its data moves, and paying an
image request every time would be both slow and, on a metered tier, the single
most expensive thing in the pipeline.

── The split that makes "once" possible ───────────────────────────────────

The obvious approach is to ask the image model for a finished card with the
company name on it. That is wrong here for two reasons, and both of them cost
real money in regenerations:

1. **Image models cannot be trusted with text**, and are actively bad at
   Devanagari and Telugu — they produce letterforms that look like the script
   and are not words. This channel publishes in three languages.
2. A card with the name baked in is **one language's card**. Three languages
   would be three generations of the same picture.

So the model is asked for a **textless background** — atmosphere, palette,
depth, no lettering anywhere — and the words are drawn on top with PIL, in
real fonts, from the record. One generation serves English, Hindi and Telugu,
the type is exactly right in all three, and re-rendering a reel after a data
change re-composites in milliseconds without touching the API.

The closing card goes further: its background has nothing company-specific in
it at all, so **one generation serves the entire channel, forever.**

── Caching ────────────────────────────────────────────────────────────────

Backgrounds are keyed by `(slug, reel)` and stored under
`backend/data/cache/art/`. `background()` returns an existing file without so
much as constructing a client. Deleting a file is how you ask for a new one;
nothing else regenerates.

── When there is no model ─────────────────────────────────────────────────

Every function degrades to a drawn gradient rather than failing. The image
step must never be what stops a video being made — a reel with a plain title
card is publishable, and a pipeline that halts at 6am because a quota reset
late is not. `available()` says which you are getting.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .sheets import CACHE_DIR

ART_DIR = CACHE_DIR / "art"

W, H = 1080, 1920

# Image models, strongest first. Kept here and not in `ai.py` on purpose:
# `ai._EXCLUDE` filters "image" out of the text-model discovery list, so these
# have never been reachable through that path and should not start being.
IMAGE_MODELS = [
    "gemini-3-pro-image",
    "nano-banana-pro-preview",
    "gemini-3.1-flash-image",
    "gemini-2.5-flash-image",
]

# The channel palette, matching the studio's dark theme so a generated card
# and a rendered scene look like the same video.
NAVY = (10, 16, 32)
GOLD = (232, 182, 51)
MINT = (34, 197, 94)
WHITE = (245, 248, 252)
MUTED = (148, 163, 184)

# Latin gets a heavy geometric face; Indic gets Nirmala, which is the only
# Windows face that covers both Devanagari and Telugu properly. See the
# `indic-script-rendering` note — Telugu at the same point size reads much
# lighter than Latin, which is why `_font` scales it up.
# A repo-local `fonts/` directory is searched FIRST, and that ordering is what
# makes this runnable anywhere.
#
# The Windows paths below are convenient and not portable. In a Linux
# container `Nirmala.ttc` does not exist, `_font` falls through to
# `ImageFont.load_default()`, and every Telugu and Hindi card renders as a row
# of empty boxes — silently, because a missing glyph is not an error. That is
# the single most likely way a containerised build ships broken output while
# every log line says success.
#
# Dropping Noto Sans Devanagari and Noto Sans Telugu into `backend/fonts/`
# fixes it on every platform at once and removes the machine dependency from
# the render entirely. Both are SIL Open Font License, so they can ship in the
# repo and in an image.
_REPO_FONTS = Path(__file__).resolve().parent.parent / "fonts"

FONTS = {
    "latin": [
        str(_REPO_FONTS / "NotoSans-Black.ttf"),
        "C:/Windows/Fonts/seguibl.ttf", "C:/Windows/Fonts/ariblk.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ],
    "devanagari": [
        str(_REPO_FONTS / "NotoSansDevanagari-Bold.ttf"),
        "C:/Windows/Fonts/Nirmala.ttc", "C:/Windows/Fonts/mangal.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf",
        "/usr/share/fonts/truetype/lohit-devanagari/Lohit-Devanagari.ttf",
    ],
    "telugu": [
        str(_REPO_FONTS / "NotoSansTelugu-Bold.ttf"),
        "C:/Windows/Fonts/Nirmala.ttc", "C:/Windows/Fonts/gautami.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansTelugu-Bold.ttf",
        "/usr/share/fonts/truetype/lohit-telugu/Lohit-Telugu.ttf",
    ],
}

# What the opening card says, per language. Short — it is on screen for two
# seconds under a company name.
OPENER_KICKER = {"en": "IPO PULSE", "hi": "IPO पल्स", "te": "IPO పల్స్"}
REEL_LABEL = {
    1: {"en": "ISSUE DETAILS", "hi": "इश्यू डिटेल्स", "te": "ఇష్యూ వివరాలు"},
    2: {"en": "TODAY'S GMP", "hi": "आज का GMP", "te": "నేటి GMP"},
    3: {"en": "SUBSCRIPTION", "hi": "सब्सक्रिप्शन", "te": "సబ్‌స్క్రిప్షన్"},
    4: {"en": "APPLY OR SKIP", "hi": "अप्लाई या स्किप", "te": "అప్లై లేదా స్కిప్"},
    5: {"en": "FINAL VERDICT", "hi": "फाइनल फैसला", "te": "తుది తీర్పు"},
    6: {"en": "ALLOTMENT", "hi": "अलॉटमेंट", "te": "అలాట్‌మెంట్"},
    7: {"en": "MARKET TODAY", "hi": "आज का मार्केट", "te": "నేటి మార్కెట్"},
}
ENDCARD = {
    "en": ["THANKS FOR WATCHING", "SUBSCRIBE", "Like · Share · Follow for daily IPO updates"],
    "hi": ["देखने के लिए धन्यवाद", "सब्सक्राइब करें", "लाइक · शेयर · रोज़ IPO अपडेट के लिए फॉलो करें"],
    "te": ["చూసినందుకు ధన్యవాదాలు", "సబ్‌స్క్రైబ్ చేయండి", "లైక్ · షేర్ · రోజువారీ IPO అప్‌డేట్‌ల కోసం ఫాలో అవ్వండి"],
}


def available() -> bool:
    """True when a real background can be generated rather than drawn."""
    if not (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")):
        return False
    try:
        import google.genai  # noqa: F401
    except ImportError:
        return False
    return True


def _font(lang: str, size: int, text: str | None = None
          ) -> ImageFont.FreeTypeFont:
    """A face that can actually draw this text at this size.

    Chosen by what the STRING contains, not by the card's language. Most
    company names stay in Latin on a Telugu card — 'Rays of Belief' is not
    transliterated — and routing them through Nirmala because the card is
    Telugu drew the headline in a regular weight where the design wants a
    black one. The result looked like a different, lighter channel.

    That also matches how the titles are already written: financial nouns in
    Latin, connective language in the local script (see the Hindi packaging
    note in `youtube-monetization-constraints`).

    Telugu and Devanagari read visually lighter than Latin at the same point
    size — the observation behind the studio's `scriptScale` bump — so Indic
    is stepped up rather than matched.
    """
    probe = text if text is not None else ""
    if any("ఀ" <= ch <= "౿" for ch in probe):
        kind = "telugu"
    elif any("ऀ" <= ch <= "ॿ" for ch in probe):
        kind = "devanagari"
    elif probe:
        kind = "latin"
    else:
        # No sample to judge from — fall back to the card's language. Telugu
        # and Devanagari are separate lists because Noto ships one face per
        # script; only Windows' Nirmala happens to cover both.
        kind = {"te": "telugu", "hi": "devanagari"}.get(lang, "latin")
    if kind != "latin":
        size = int(size * 1.12)
    for path in FONTS[kind]:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, ValueError):
            continue
    # Nothing on this machine can draw this script. Say so — a silent
    # fallback here means a card full of empty boxes that no log mentions.
    print(f"  ! no {kind} font found; text will not render correctly. "
          f"Put a Noto {kind.title()} .ttf in {_REPO_FONTS}")
    return ImageFont.load_default()


def _gradient(seed: str) -> Image.Image:
    """The fallback background: a drawn gradient, no model involved.

    Deterministic from `seed`, so the same reel always gets the same card and
    a re-render is not a visual change. Deliberately plain — it should read as
    the channel's own styling, not as a failed generation.
    """
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    tilt = digest[0] / 255.0
    img = Image.new("RGB", (W, H), NAVY)
    draw = ImageDraw.Draw(img)
    for y in range(H):
        k = y / H
        draw.line([(0, y), (W, y)], fill=(
            int(NAVY[0] + 26 * k + 10 * tilt),
            int(NAVY[1] + 34 * k + 8 * tilt),
            int(NAVY[2] + 58 * k)))
    # One soft accent bloom, positioned from the hash so consecutive reels do
    # not look identical.
    glow = Image.new("RGB", (W, H), (0, 0, 0))
    gd = ImageDraw.Draw(glow)
    cx = int(W * (0.25 + 0.5 * (digest[1] / 255.0)))
    cy = int(H * (0.30 + 0.35 * (digest[2] / 255.0)))
    gd.ellipse([cx - 420, cy - 420, cx + 420, cy + 420], fill=(8, 62, 40))
    glow = glow.filter(ImageFilter.GaussianBlur(180))
    return Image.blend(img, glow, 0.55)


def _prompt(company: str, sector: str, reel: int) -> str:
    """The background brief. Textless is the load-bearing instruction."""
    mood = {
        1: "measured and informative, a sense of something about to begin",
        2: "energetic, upward momentum, a trading-floor charge",
        3: "busy and demand-driven, many participants",
        4: "analytical and sober, weighing something up",
        5: "decisive, the moment of judgement",
        6: "resolved, a result arriving",
        7: "early morning, the market about to open",
    }.get(reel, "premium financial")
    return (
        f"A premium vertical 9:16 background image for an Indian stock-market "
        f"video about {company}"
        + (f", a company in {sector}. " if sector else ". ")
        + f"Mood: {mood}. Deep navy-to-black gradient, a large soft glowing "
        f"emerald-green upward arrow behind everything, a faint candlestick "
        f"chart texture, restrained metallic gold accents, and a modern glass "
        f"office tower shot from below on one side. Cinematic depth, shallow "
        f"focus, high-end corporate finance look, not cartoonish. "
        f"\n\nABSOLUTELY NO TEXT, NO LETTERS, NO NUMBERS, NO LOGOS and no "
        f"watermark anywhere in the image — every word is composited on top "
        f"afterwards and any lettering you draw will collide with it. "
        f"Keep the upper third and the lower quarter visually calm and darker: "
        f"a headline sits in one and a caption in the other."
    )


def background(slug: str, reel: int, company: str = "", sector: str = "",
               force: bool = False) -> Path:
    """The textless plate for one reel, generated once and cached forever.

    Returns a path that always exists — a drawn gradient when there is no
    model, no quota, or no network.
    """
    ART_DIR.mkdir(parents=True, exist_ok=True)
    dest = ART_DIR / f"bg-{slug}-r{reel}.png"
    if dest.exists() and not force:
        return dest

    if available():
        try:
            from google import genai
            from google.genai import types
            client = genai.Client(
                api_key=os.getenv("GEMINI_API_KEY")
                or os.getenv("GOOGLE_API_KEY"))
            prompt = _prompt(company or slug, sector, reel)
            for model in IMAGE_MODELS:
                try:
                    resp = client.models.generate_content(
                        model=model, contents=prompt,
                        config=types.GenerateContentConfig(
                            response_modalities=["IMAGE"],
                            # The control, not the prompt words — these models
                            # default to square and ignore a written ratio.
                            image_config=types.ImageConfig(aspect_ratio="9:16")))
                    for part in resp.candidates[0].content.parts:
                        blob = getattr(part, "inline_data", None)
                        if blob and blob.data:
                            dest.write_bytes(blob.data)
                            return dest
                except Exception:
                    continue          # retired, out of quota, or no free tier
        except Exception:
            pass                      # fall through to the drawn plate

    _gradient(f"{slug}-{reel}").save(dest)
    return dest


def _fit(draw: ImageDraw.ImageDraw, text: str, lang: str, size: int,
         max_width: int, floor: int = 28) -> ImageFont.FreeTypeFont:
    """Shrink until it fits. Long legal names are the norm here, not the edge.

    'Rays of Belief Limited- For Profit Social Enterprise' at a headline size
    is four times the frame width. Wrapping is handled by the caller; this
    stops any single line from running off the card.
    """
    while size > floor:
        font = _font(lang, size, text)
        if draw.textlength(text, font=font) <= max_width:
            return font
        size -= 4
    return _font(lang, floor, text)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont,
          max_width: int) -> list[str]:
    lines, current = [], ""
    for word in text.split():
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=font) <= max_width or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _shadowed(draw, xy, text, font, fill, anchor="mm") -> None:
    """Text with a soft dark halo, so it survives any background."""
    x, y = xy
    for dx, dy in ((-3, 0), (3, 0), (0, -3), (0, 3), (-2, -2), (2, 2)):
        draw.text((x + dx, y + dy), text, font=font, fill=(0, 0, 0),
                  anchor=anchor)
    draw.text((x, y), text, font=font, fill=fill, anchor=anchor)


def _scrim(img: Image.Image) -> Image.Image:
    """Darken top and bottom so composited type always has contrast.

    Applied to a generated plate as well as a drawn one: the model is asked
    for calm areas but cannot be relied on to deliver them, and white text on
    an unexpectedly bright sky is the one failure that ruins a card.
    """
    out = img.convert("RGB").copy()
    veil = Image.new("L", (W, H), 0)
    vd = ImageDraw.Draw(veil)
    for y in range(H):
        k = y / H
        alpha = 0
        if k < 0.42:
            alpha = int(165 * (1 - k / 0.42))
        elif k > 0.72:
            alpha = int(185 * ((k - 0.72) / 0.28))
        vd.line([(0, y), (W, y)], fill=alpha)
    dark = Image.new("RGB", (W, H), (4, 8, 18))
    return Image.composite(dark, out, veil)


def opener(slug: str, reel: int, lang: str, company: str,
           sector: str = "", force: bool = False) -> Path:
    """The card the reel opens on. Cached per (slug, reel, lang)."""
    ART_DIR.mkdir(parents=True, exist_ok=True)
    dest = ART_DIR / f"open-{slug}-r{reel}-{lang}.png"
    if dest.exists() and not force:
        return dest

    plate = Image.open(background(slug, reel, company, sector)).convert("RGB")
    plate = plate.resize((W, H), Image.LANCZOS)
    img = _scrim(plate)
    draw = ImageDraw.Draw(img)

    _shadowed(draw, (W // 2, 300), OPENER_KICKER.get(lang, OPENER_KICKER["en"]),
              _font(lang, 58, OPENER_KICKER.get(lang, OPENER_KICKER['en'])), GOLD)

    name = (company or slug).upper() if lang == "en" else (company or slug)
    font = _fit(draw, name, lang, 130, W - 150)
    lines = _wrap(draw, name, font, W - 150)
    if len(lines) > 2:                      # three lines of a legal name is
        lines = lines[:2]                   # a wall; two reads as a title
        lines[-1] += "…"
    y = 620 - (len(lines) - 1) * 78
    for line in lines:
        _shadowed(draw, (W // 2, y), line, font, WHITE)
        y += 152

    label = REEL_LABEL.get(reel, {}).get(lang, "")
    if label:
        lf = _font(lang, 62, label)
        tw = draw.textlength(label, font=lf)
        box = [(W - tw) / 2 - 46, 950, (W + tw) / 2 + 46, 1070]
        draw.rounded_rectangle(box, radius=60, fill=(16, 24, 44),
                               outline=MINT, width=4)
        _shadowed(draw, (W // 2, 1010), label, lf, MINT)

    img.save(dest)
    return dest


def endcard(lang: str, handle: str = "@IPOPulse", force: bool = False) -> Path:
    """The closing card. One background for the whole channel, forever.

    Nothing on it is company-specific, so unlike `opener` this is generated
    once and reused by every reel of every IPO in every language — three
    composites off one plate, and then never an image request again.
    """
    ART_DIR.mkdir(parents=True, exist_ok=True)
    dest = ART_DIR / f"end-{lang}.png"
    if dest.exists() and not force:
        return dest

    plate = Image.open(background("channel", 0, "IPO Pulse India",
                                  "financial media")).convert("RGB")
    img = _scrim(plate.resize((W, H), Image.LANCZOS))
    draw = ImageDraw.Draw(img)

    thanks, sub, tail = ENDCARD.get(lang, ENDCARD["en"])
    _shadowed(draw, (W // 2, 560), thanks,
              _fit(draw, thanks, lang, 74, W - 160), WHITE)

    sf = _fit(draw, sub, lang, 116, W - 220)
    tw = draw.textlength(sub, font=sf)
    draw.rounded_rectangle([(W - tw) / 2 - 70, 800, (W + tw) / 2 + 70, 990],
                           radius=95, fill=(220, 38, 38))
    _shadowed(draw, (W // 2, 895), sub, sf, WHITE)

    tf = _fit(draw, tail, lang, 42, W - 180)
    for i, line in enumerate(_wrap(draw, tail, tf, W - 180)[:2]):
        _shadowed(draw, (W // 2, 1110 + i * 62), line, tf, MUTED)
    _shadowed(draw, (W // 2, 1420), handle, _font("en", 56, handle), GOLD)

    img.save(dest)
    return dest


def for_reel(ipo: Any, reel: int, lang: str,
             force: bool = False) -> tuple[Path, Path]:
    """Both cards for one reel video. What `render.py` calls."""
    slug = getattr(ipo, "slug", "market")
    return (
        opener(slug, reel, lang, getattr(ipo, "company", "") or slug,
               getattr(ipo, "sector", "") or "", force=force),
        endcard(lang, force=force),
    )
