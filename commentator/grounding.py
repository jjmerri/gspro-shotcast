"""Rejects commentary lines that state numbers or outcomes the facts don't support.

The checks of a line against its facts (check_line, the number, score and margin rules, meta_talk,
bare_acknowledgement) are shotcast_scoring.grounding's, shared with Shotcast Cloud; the booth's own
(repeated phrases, calls held to, players' notes) are here.
"""

import re

from shotcast_scoring.grounding import (SCENERY, Check, bare_acknowledgement, check_line, meta_talk,
                                        wrong_margin, wrong_par_type_tally, wrong_round_score)

__all__ = ["SCENERY", "Check", "bare_acknowledgement", "call_words", "check_line", "cites_call", "credited_words",
           "makes_call", "meta_talk", "ngrams", "note_keywords", "repeats", "uses_note", "wrong_margin",
           "wrong_par_type_tally", "wrong_round_score"]


def ngrams(text: str, n: int = 5) -> set[tuple[str, ...]]:
    words = re.findall(r"[a-z0-9']+", text.lower().replace("\u2019", "'"))
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


def repeats(line: str, earlier: list[str], n: int = 5) -> str | None:
    """Returns the first n-word phrase `line` shares with any earlier text."""
    mine = ngrams(line, n)
    for text in earlier:
        shared = mine & ngrams(text, n)
        if shared:
            return " ".join(min(shared))
    return None


# A call made out loud, which the booth can later hold a commentator to ("you called it"). Broad on
# purpose: a call missed here only lets a made-up one ("you've called this a clean finish", from "a
# chance to finish with a clean statement") be rejected.
_AHEAD = (r"(?=[^.!?;]*\b(?:will|'ll|going to|gonna|goes|go|drops?|dropping|slides?|sliding|miss(?:es)?|missing|holes?"
          r"|holing|makes?|finds?|stays?|breaks?|falls?|lips?|sinks?|gets?|comes?|has to|needs?)\b)")
_CALL = re.compile(
    r"\b(?:(?:i (?:think|reckon|bet|fancy|expect)|i've got (?:this|it|him|her))\b" + _AHEAD +
    r"|(?:i predict|i'm (?:calling|backing|tipping|predicting|going with)|i'll (?:call|back|bet|predict|go with|tip)"
    r"|i call|mark my words|calling it|watch this|my money(?:'s| is))\b"
    r"|my (?:bet|call|tip|prediction|pick)(?::| is\b| for\b| -))")
# A line holding someone to an earlier prediction: "you" is the other commentator, "I"/"my" the speaker.
# Not "you called": "you called that a pull" is the booth quoting how a shot was described, said on air.
_CITED = re.compile(r"\b(?:(you|i)(?:'ve| have)? predicted|(your|my|that|the) predictions?)\b")


# Looser: a line crediting the other commentator with a call ("you called for simple", "you said keep it
# level", "your call was fairway"). Also how the booth quotes a description ("you called that a pull"),
# so it's only held against the calls actually made (Engine._uncalled), never on its own.
_CREDITS_YOU = re.compile(r"\b(?:you (?:called|said|backed|tipped|fancied|reckoned)|your (?:call|tip|pick))\b")


def _plain(line: str) -> str:
    return line.lower().replace("\u2019", "'")


def makes_call(line: str) -> bool:
    return bool(_CALL.search(_plain(line)))


_CREDIT_STOP = {"that", "this", "with", "would", "there", "then", "than", "about", "just", "what", "they",
                "them", "have", "been", "from", "into", "your", "were", "when", "it'd", "he'd", "he's", "it's"}


def _content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z][a-z']{3,}", text) if w not in _CREDIT_STOP}


def credited_words(line: str) -> set[str]:
    """The words a line says the other commentator called or said ("you called for simple" -> {"simple"}),
    to the end of that sentence; empty when it credits them with nothing."""
    plain = _plain(line)
    m = _CREDITS_YOU.search(plain)
    return _content_words(re.split(r"[.!?;]", plain[m.end():], maxsplit=1)[0]) if m else set()


def call_words(line: str) -> set[str]:
    return _content_words(_plain(line))


def cites_call(line: str) -> str | None:
    """Whose earlier prediction the line holds them to: "you" (the other commentator), "me" (the
    speaker), "any" ("that prediction"), or None. One in a line that makes a call ("my prediction: he
    holes it") is that new call."""
    m = _CITED.search(_plain(line))
    if not m:
        return None
    word = m.group(1) or m.group(2)
    if word in ("you", "your"):
        return "you"
    return None if makes_call(line) else "me" if word in ("i", "my") else "any"


_NOTE_STOP = {
    "this", "that", "with", "about", "most", "time", "just", "call", "golf", "fine", "lean", "occasional",
    "light", "ribbing", "dont", "don't", "plays", "play", "playing", "another", "other", "team", "teams",
    "together", "their", "they", "them", "his", "her", "not", "and", "the", "for", "who", "has", "have",
    "but", "also", "from", "on", "of", "as", "is", "a", "an", "to", "in", "it", "he", "she",
    "league", "best", "under", "round", "tonight", "week", "season", "every", "always", "never",
}


def note_keywords(note: str, names: set[str]) -> set[str]:
    """The distinctive words of a player's note (its first sentence, less names and filler), for
    spotting when a line leans on it. "Built this booth." -> {"built", "booth"}."""
    first = re.split(r"(?<=[.!?])\s", note.strip(), maxsplit=1)[0]
    words = {w.strip("'").removesuffix("'s") for w in re.findall(r"[a-z']+", first.lower())}
    return {w for w in words if len(w) >= 4 and w not in _NOTE_STOP and w not in names}


def uses_note(line: str, keywords: set[str]) -> bool:
    """Two of the note's words (or its only one): "booth" alone is just where they sit."""
    lowered = line.lower()
    # A loose stem, so "builder" and "build" count for "built".
    hits = sum(bool(re.search(rf"\b{re.escape(k[:-1] if len(k) > 5 else k[:4])}", lowered)) for k in keywords)
    return bool(keywords) and hits >= min(2, len(keywords))
