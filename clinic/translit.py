"""Compare names across scripts: Devanagari speech ("अमित दुआ") against names
stored in Roman letters ("Amit Dua"), and the other way round.

Speech-to-text writes Hindi names in Devanagari while many clinics type them in
Roman letters, so a plain string comparison scores such a pair at 0 and the
patient is "not found". This module turns both into a rough *sound skeleton*
(consonants only, with the usual spelling variants folded together) and
compares those. It is deliberately approximate: it is only used when the two
names are in different scripts, it never reaches a perfect score (so it never
counts as an exact match), and the caller still shows the human a card to
check, or asks "which one?" when two names fit equally well.
"""

import re
import unicodedata
from difflib import SequenceMatcher

_DEVANAGARI = re.compile(r"[ऀ-ॿ]")

_VOWELS = {
    "अ": "a", "आ": "aa", "इ": "i", "ई": "ii", "उ": "u", "ऊ": "uu", "ऋ": "ri",
    "ए": "e", "ऐ": "ai", "ओ": "o", "औ": "au", "ऑ": "o", "ऍ": "e",
}
_MATRAS = {
    "ा": "aa", "ि": "i", "ी": "ii", "ु": "u", "ू": "uu", "ृ": "ri",
    "े": "e", "ै": "ai", "ो": "o", "ौ": "au", "ॉ": "o", "ॅ": "e",
}
_CONSONANTS = {
    "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "n",
    "च": "ch", "छ": "chh", "ज": "j", "झ": "jh", "ञ": "n",
    "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n",
    "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n",
    "प": "p", "फ": "f", "ब": "b", "भ": "bh", "म": "m",
    "य": "y", "र": "r", "ल": "l", "व": "v", "श": "sh", "ष": "sh", "स": "s", "ह": "h",
    "क़": "q", "ख़": "kh", "ग़": "g", "ज़": "z", "ड़": "r", "ढ़": "rh", "फ़": "f", "य़": "y",
}
_VIRAMA = "्"
_NUKTA = "़"
_ANUSVARA = {"ं": "n", "ँ": "n", "ः": "h"}


def has_devanagari(text):
    return bool(_DEVANAGARI.search(text or ""))


def to_latin(text):
    """Rough Devanagari -> Roman transliteration (sound, not spelling). Other
    characters pass through unchanged."""
    text = unicodedata.normalize("NFD", text or "")
    # NFD splits the nukta consonants (ज़ = ज + nukta); put them back together.
    text = re.sub("(.)" + _NUKTA, lambda m: (m.group(1) + _NUKTA) if (m.group(1) + _NUKTA) in _CONSONANTS else m.group(0), text)
    out = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        pair = text[i:i + 2]
        if pair in _CONSONANTS:
            cons, i = _CONSONANTS[pair], i + 2
        elif ch in _CONSONANTS:
            cons, i = _CONSONANTS[ch], i + 1
        elif ch in _VOWELS:
            out.append(_VOWELS[ch])
            i += 1
            continue
        elif ch in _ANUSVARA:
            out.append(_ANUSVARA[ch])
            i += 1
            continue
        else:
            out.append(ch)
            i += 1
            continue
        # a consonant: its vowel follows (matra), is suppressed (virama), or is the inherent "a"
        nxt = text[i] if i < n else ""
        if nxt == _NUKTA:
            i += 1
            nxt = text[i] if i < n else ""
        if nxt in _MATRAS:
            out.append(cons + _MATRAS[nxt])
            i += 1
        elif nxt == _VIRAMA:
            out.append(cons)
            i += 1
        else:
            end_of_word = i >= n or not _DEVANAGARI.match(text[i])
            out.append(cons if end_of_word else cons + "a")  # Hindi drops the final "a"
    return "".join(out)


def phonetic_key(text):
    """A consonant skeleton of a name, in either script, with common spelling
    variants folded together (w/v, ph/f, z/j, aspirates, doubled letters)."""
    latin = to_latin(text).lower()
    latin = re.sub(r"[^a-z\s]", " ", latin)
    latin = latin.replace("sh", "S").replace("ch", "C")
    latin = latin.replace("ph", "f").replace("w", "v").replace("z", "j").replace("q", "k").replace("x", "ks")
    latin = latin.replace("c", "k")
    latin = re.sub(r"(?<=[a-zSCf])h", "", latin)       # aspirates: kh, gh, th, dh, bh, jh ...
    latin = re.sub(r"[aeiouy\s]", "", latin)            # the sound skeleton: consonants only
    return re.sub(r"(.)\1+", r"\1", latin)


def cross_script_similarity(a, b):
    """0..0.95 when exactly one of the two names is Devanagari, else 0. Never 1.0:
    a sound-alike is a good guess, not an exact match."""
    if has_devanagari(a) == has_devanagari(b):
        return 0.0
    ka, kb = phonetic_key(a), phonetic_key(b)
    if len(ka) < 2 or len(kb) < 2:
        return 0.0
    return 0.95 * SequenceMatcher(None, ka, kb).ratio()
