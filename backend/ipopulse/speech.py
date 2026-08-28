"""Turn written script into speakable script.

The scripts in frontend/js/output.js are written to be READ ON A CARD. Spoken
aloud, the same text has defects that are invisible on screen and obvious in
your ears — and they are most of what makes a synthetic read sound synthetic.
Every rule here came from looking at real output (reel 1, esds-software-solution,
28 Aug 2026) rather than from imagining what might go wrong:

    देता है।।        a DOUBLE danda. Reads as a broken double stop.
    ₹408             symbol before the number; Hindi and Telugu speak the
                     currency AFTER it ("चार सौ आठ रुपये", not "रुपया 408").
    ₹720 करोड़       the unit sits between the amount and the currency word,
                     so a naive swap gives "720 रुपये करोड़".
    100%             the glyph is either skipped or read in English.
    01 सितंबर        a leading zero becomes "zero one".

WHAT THIS DELIBERATELY DOES NOT DO
----------------------------------
No number-to-words. In Devanagari and Telugu context the v3 models read digit
strings in the right language already, and a hand-rolled Hindi/Telugu numbering
system (लाख / करोड़, gendered forms, decimals) is a large surface with a bad
failure mode: silently saying the wrong figure about somebody's money. If the
ear test shows digits being misread, that is the moment to add it — with the
model's actual mistakes in hand.

Nor does it rewrite content. `₹0 करोड़` is a SCRIPT defect, not a pronunciation
one — the fix is for output.js to not emit the clause at all — so it is reported
by `problems()` and left alone here. Silently deleting a figure would be the
worst of both.
"""

from __future__ import annotations

import re

# The currency word, per language. Spoken position is after the amount in all
# three, which is why this is a substitution and not a prefix strip.
RUPEES = {"en": "rupees", "hi": "रुपये", "te": "రూపాయలు"}
PERCENT = {"en": "percent", "hi": "प्रतिशत", "te": "శాతం"}
# "2.1x subscribed" — the x is a multiplier, never the letter.
TIMES = {"en": "times", "hi": "गुना", "te": "రెట్లు"}

# Units that may sit between the amount and the currency word. Order matters:
# longest first, so करोड़ is matched before any shorter prefix of it.
UNITS = {
    "en": ["crore", "lakh"],
    "hi": ["करोड़", "लाख"],
    "te": ["కోట్లు", "కోటి", "లక్షలు", "లక్ష"],
}


# A written amount, and it MUST end in a digit.
#
# The obvious `[\d][\d,]*` is wrong and was wrong in the first version here:
# against "₹429," it greedily eats the trailing comma, so the currency word
# lands after the punctuation — "429, रुपये". Requiring a final digit keeps
# grouping commas (14,586) and leaves sentence commas alone.
AMOUNT = r"\d(?:[\d,]*\d)?(?:\.\d+)?"

# "from A to B" — the join word in a price band, per language.
RANGE_JOIN = {"en": r"to", "hi": r"से", "te": r"నుంచి|నుండి"}


def _currency(text: str, lang: str) -> str:
    """Written amounts to spoken ones. Three passes, and the order is the point.

    RANGE first, then UNIT, then BARE. Each earlier pass would otherwise be
    mangled by a later one:

      "₹408 से ₹429"   -> "408 से 429 रुपये"        one currency word, not two.
                          Saying it twice is what a form does, not a person.
      "₹720 करोड़"     -> "720 करोड़ रुपये"          the unit has to stay between
                          the amount and the currency; doing BARE first gives
                          "720 रुपये करोड़", which is nonsense aloud.
      "₹14,586"        -> "14,586 रुपये"
    """
    word = RUPEES.get(lang, RUPEES["en"])
    units = UNITS.get(lang, UNITS["en"])
    join = RANGE_JOIN.get(lang, RANGE_JOIN["en"])

    # 1. Range: collapse the pair onto a single trailing currency word.
    text = re.sub(rf"₹\s?({AMOUNT})\s+({join})\s+₹\s?({AMOUNT})",
                  rf"\1 \2 \3 {word}", text)

    # 2. Amount + unit.
    if units:
        unit_alt = "|".join(re.escape(u) for u in units)
        text = re.sub(rf"₹\s?({AMOUNT})\s*({unit_alt})",
                      rf"\1 \2 {word}", text)

    # 3. Anything left.
    return re.sub(rf"₹\s?({AMOUNT})", rf"\1 {word}", text)


def for_speech(text: str, lang: str = "en") -> str:
    """The whole normalisation, in the order the rules have to run."""
    if not text:
        return ""
    out = text
    lang = (lang or "en").lower()

    # 0. Acronyms first. Later passes insert Devanagari/Telugu words next to
    #    digits, and doing this afterwards would have to match around them.
    out = _acronyms(out, lang)

    # 1. Currency, before punctuation work — the patterns rely on the digits and
    #    the unit still sitting next to each other.
    out = _currency(out, lang)

    # 2. Percent and multiplier glyphs.
    out = re.sub(r"(\d(?:[\d,]*(?:\.\d+)?)?)\s*%",
                 rf"\1 {PERCENT.get(lang, PERCENT['en'])}", out)
    #    Only a digit-attached x, so "x" inside a word is untouched.
    out = re.sub(r"(\d(?:\.\d+)?)\s*[xX](?![A-Za-z])",
                 rf"\1 {TIMES.get(lang, TIMES['en'])}", out)

    # 3. Leading zero on a small number: "01 सितंबर" is read "zero one".
    #    Bounded to 1-2 digits so an id or a code is never touched.
    out = re.sub(r"(?<!\d)0(\d)(?!\d)", r"\1", out)

    # 4. Punctuation that only hurts aloud.
    #    Doubled sentence terminators — observed as "है।।" in real output. A
    #    danda already ends the sentence; the second one is a stutter.
    out = re.sub(r"।\s*।+", "।", out)
    out = re.sub(r"(?<![.\d])\.\s*\.+(?!\d)", ".", out)
    #    An em dash is a beat in print and a swallowed word in speech; a comma
    #    is the pause it was standing in for.
    out = out.replace("—", ",")
    #    Collapse the whitespace all of the above leaves behind.
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\s+([,।.])", r"\1", out)
    out = re.sub(r"\n{3,}", "\n\n", out)

    return out.strip()


# ── finance acronyms, spelled for an Indic voice ───────────────────────────
#
# A Latin acronym sitting in a Devanagari or Telugu sentence makes the model
# switch to an English mouth for two syllables and switch back, and that seam is
# a large part of what "the Hindi sounds wrong" actually is. Written in the local
# script, the same letters are read with the same phonetics as everything around
# them — which is also how a person says them: "आईपीओ", not an English "I-P-O"
# dropped into a Hindi clause.
#
# AN ALLOWLIST, DELIBERATELY, and this is the important design decision. These
# scripts also carry company names — ESDS, ABH, MUFG, HTLS, FPSE — and those are
# proper nouns. Transliterating a brand makes it unrecognisable to the one
# audience that already knows it, and worse, unsearchable. Counted across the
# whole book: IPO 184, GMP 98, EBITDA 60, OFS 52, HNI 48, BSE 48, PAN 48,
# QIB 36, NII 36, CAGR 34, PAT 26 — the terms of art are frequent and finite,
# the company names are the long tail. So only the terms of art are converted
# and everything unrecognised is left exactly as written.
ACRONYMS = {
    "hi": {
        "IPO": "आईपीओ", "GMP": "जीएमपी", "OFS": "ओएफएस", "QIB": "क्यूआईबी",
        "NII": "एनआईआई", "HNI": "एचएनआई", "BSE": "बीएसई", "NSE": "एनएसई",
        "SME": "एसएमई", "RHP": "आरएचपी", "DRHP": "डीआरएचपी",
        "CAGR": "सीएजीआर", "PAT": "पीएटी", "EPS": "ईपीएस", "ROE": "आरओई",
        # Said as a word, not letters, in Indian finance media — both of these.
        "EBITDA": "एबिटडा", "PAN": "पैन",
    },
    "te": {
        "IPO": "ఐపీవో", "GMP": "జీఎంపీ", "OFS": "ఓఎఫ్ఎస్", "QIB": "క్యూఐబీ",
        "NII": "ఎన్ఐఐ", "HNI": "హెచ్ఎన్ఐ", "BSE": "బీఎస్ఈ", "NSE": "ఎన్ఎస్ఈ",
        "SME": "ఎస్ఎంఈ", "RHP": "ఆర్హెచ్పీ", "DRHP": "డీఆర్హెచ్పీ",
        "CAGR": "సీఏజీఆర్", "PAT": "పీఏటీ", "EPS": "ఈపీఎస్", "ROE": "ఆర్ఓఈ",
        "EBITDA": "ఎబిట్డా", "PAN": "పాన్",
    },
}


def _acronyms(text: str, lang: str) -> str:
    """Rewrite known finance acronyms into the local script."""
    table = ACRONYMS.get((lang or "").lower())
    if not table:
        return text
    # Longest first: without it "NSE" inside a longer token, or "PAT" inside
    # "PATH", could be replaced piecemeal. \b on both sides does most of the
    # work; ordering closes the rest.
    for src in sorted(table, key=len, reverse=True):
        text = re.sub(rf"\b{re.escape(src)}\b", table[src], text)
    return text


# ── deriving the delivery from the script ──────────────────────────────────
#
# Reference numeric density, in digits per 100 characters, per language.
#
# MEASURED, not guessed — over all 24 IPOs x 6 reels in the book on
# 28 Aug 2026. The per-language split is the whole reason this constant exists:
#
#     reel   en    hi    te
#      1    3.3   9.7   9.9
#      2    2.9   9.9   9.6
#      3    1.7   6.3   5.8
#      4    4.4   6.6   6.5
#      5    1.7   5.3   5.0
#      6    3.0   5.9   5.3
#
# Hindi and Telugu run two to three times denser than English, and not because
# they say more numbers — they say the SAME numbers in a third of the words, so
# the digits are a far larger share of the text. Feeding raw density into one
# formula would therefore peg every Indic reel at maximum stability and lose all
# variation *within* a language, which is the variation actually worth having.
# So density is scored as a RATIO against its own language's typical value.
REF_DENSITY = {"en": 3.0, "hi": 7.5, "te": 7.4}

# A long sentence needs air. English averages 41-74 characters per sentence and
# the Indic scripts 24-52, so this threshold is deliberately generous.
LONG_SENTENCE = 60


def profile(text: str, lang: str = "en") -> dict:
    """Measurable properties of a script that should change how it is read."""
    body = (text or "").strip()
    if not body:
        return {"chars": 0, "density": 0.0, "ratio": 1.0,
                "questions": 0, "mean_sentence": 0.0}
    digits = sum(c.isdigit() for c in body)
    density = digits * 100.0 / len(body)
    ref = REF_DENSITY.get((lang or "en").lower(), REF_DENSITY["en"])
    sentences = [s for s in re.split(r"[।.!?]", body) if s.strip()]
    mean_sentence = (sum(len(s) for s in sentences) / len(sentences)
                     if sentences else 0.0)
    return {
        "chars": len(body),
        "density": density,
        "ratio": density / ref if ref else 1.0,
        # Only reel 4 asks anything ("Apply or Skip?"), which makes this a clean
        # signal for "this reel is an argument, not a readout".
        "questions": len(re.findall(r"[?？]", body)),
        "mean_sentence": mean_sentence,
    }


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def delivery(text: str, lang: str = "en") -> dict:
    """Voice settings derived from the script itself.

    The governing idea, and it is the opposite of the obvious one: STABILITY
    RISES WITH NUMERIC DENSITY. Low stability is what lets v3 act, but it also
    lets it improvise — and on a line that is mostly figures, "expressive" means
    a real risk of speaking a different number than the card is showing. That is
    not a delivery flaw, it is a false statement about somebody's money.

    So a figure-heavy reel is read straight and clearly, and a reel that is
    argument rather than readout gets the latitude, because there is nothing in
    it to get factually wrong. Questions are the marker for the second kind.

    Returns only the three fields worth varying. similarity_boost and
    use_speaker_boost are voice-identity settings, not performance ones — moving
    them per reel would make the same voice sound like different people between
    scenes, which is the complaint that started this.
    """
    p = profile(text, lang)
    if not p["chars"]:
        return {}
    r = p["ratio"]

    # 0.58 at typical density, rising toward 0.78 as a reel gets figure-heavy and
    # falling toward 0.42 when it is mostly prose.
    stability = _clamp(0.58 + (r - 1.0) * 0.18, 0.42, 0.78)

    # Exaggeration: down with density, up with rhetorical questions.
    style = _clamp(0.18 - (r - 1.0) * 0.12 + p["questions"] * 0.06, 0.05, 0.42)

    # Dense or long-sentenced copy wants a fraction more room.
    speed = 1.0 - (r - 1.0) * 0.05
    if p["mean_sentence"] > LONG_SENTENCE:
        speed -= 0.02
    speed = _clamp(speed, 0.92, 1.03)

    return {"stability": round(stability, 3),
            "style": round(style, 3),
            "speed": round(speed, 3)}


# Patterns that are wrong in the SCRIPT rather than in its pronunciation. These
# are reported, never rewritten: each one wants an editorial fix in output.js,
# and a normaliser that quietly papered over them would hide the actual bug.
def problems(text: str, lang: str = "en") -> list[str]:
    """Things worth fixing upstream, in plain language."""
    found: list[str] = []
    if re.search(r"₹\s?0(?![\d.])", text):
        found.append("says a zero rupee amount aloud — output.js should drop "
                     "the clause instead of reading '0 crore'")
    if re.search(r"।\s*।", text) or re.search(r"(?<!\.)\.\s*\.(?!\.)", text):
        found.append("doubled sentence terminator")
    if lang != "en":
        # Only the ones for_speech CANNOT fix. Reporting IPO or OFS here would
        # be noise — the allowlist converts them — and a report you learn to
        # ignore is worse than no report. What is left is company names and
        # anything new, which are the cases that genuinely need a human to
        # decide whether they should be spelled out, transliterated or left.
        known = set(ACRONYMS.get(lang, {}))
        acr = sorted({a for a in re.findall(r"\b[A-Z]{2,}[a-z]?[A-Z]*\b", text)
                      if a not in known})
        if acr:
            found.append("Latin acronyms with no transliteration, which the "
                         f"voice will read in English: {', '.join(acr[:6])}")
    # A sentence nobody can say in one breath is the commonest cause of a read
    # that runs out of air and flattens.
    #
    # Split on ALL terminators at once. Doing them one at a time — as this first
    # did — splits Hindi on "." alone, and Hindi ends sentences with "।", so the
    # whole script came back as a single 681-character "sentence" and every
    # Indic script was reported as too long. The real longest sentence in the
    # entire book is 230 characters.
    if any(len(s.strip()) > 260 for s in re.split(r"[।.!?]", text)):
        found.append("a sentence over 260 characters — split it or the "
                     "read flattens")
    return found
