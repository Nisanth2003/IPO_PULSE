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
    if re.search(r"\b[A-Z]{2,}[a-z]?[A-Z]*\b", text) and lang != "en":
        acr = sorted(set(re.findall(r"\b[A-Z]{2,}[a-z]?[A-Z]*\b", text)))
        found.append("Latin acronyms inside non-Latin text, which the voice "
                     f"will read in English: {', '.join(acr[:6])}")
    # A sentence nobody can say in one breath is the commonest cause of a read
    # that runs out of air and flattens.
    for sep in ("।", "."):
        for s in text.split(sep):
            if len(s.strip()) > 260:
                found.append("a sentence over 260 characters — split it or the "
                             "read flattens")
                break
        else:
            continue
        break
    return found
