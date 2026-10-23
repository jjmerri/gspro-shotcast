"""Which descriptive words the booth keeps reaching for this round ("tidy", "gentle", "proper"...).

The models have pet words that a phrase-level repeat check never sees, because each line words them
differently. A part-of-speech tagger picks out the colour words (adjectives and adverbs) from the rest,
so names, verbs and plain words never count, and a short list drops the golf terms and factual words
it also tags that way (rough, lob, left, short...). The prompts then tell the booth which words it has
leaned on; nothing is banned or filtered.
"""

from collections import Counter
from functools import cache

# Factual adjectives and adverbs (direction, distance, order, slope, amount): saying them often is
# just describing the golf.
FACTUAL = frozenset("""short long left right straight uphill downhill first second third last next early late
back front even level low high open closed same other own only just still already again away out off up
down here there now then also too very much more most less least well so not never always ever far close
near inside outside past over under above below around ahead behind little sharply""".split())

# Golf vocabulary the tagger reads as adjectives: lies, clubs, formats, scores, ordinals and the like.
GOLF = frozenset("""rough semi deep fescue native pine green sand lob gap pitching scramble alternate double
triple single best worst hard soft firm fast slow rightward leftward airborne round total fourth fifth sixth
seventh eighth ninth tenth eleventh twelfth thirteenth fourteenth fifteenth sixteenth seventeenth eighteenth
welcome usual gimme""".split())

# Pet words the tagger reads as nouns ("a lovely touch").
NOUNS = frozenset({"touch"})

THRESHOLD = 3   # lines in a round before a word counts as leaned on
SHOWN = 6       # most words named in a prompt


@cache
def _nlp():
    import spacy

    return spacy.load("en_core_web_sm", disable=["ner", "parser"])


def colour_words(text: str) -> set[str]:
    """The descriptive words in a line, as lemmas ("gently" stays an adverb; "tidier" is "tidy")."""
    out = set()
    for t in _nlp()(text):
        if not t.is_alpha or len(t.text) < 3:
            continue
        word = t.lemma_.lower()
        if word in NOUNS or (t.pos_ in ("ADJ", "ADV") and not t.is_stop
                             and word not in FACTUAL and word not in GOLF):
            out.add(word)
    return out


class WordWatch:
    """Counts, per round, how many aired lines used each descriptive word."""

    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()

    def add(self, text: str) -> None:
        self.counts.update(colour_words(text))

    def reset(self) -> None:
        self.counts.clear()

    def overused(self) -> list[tuple[str, int]]:
        ranked = sorted(self.counts.items(), key=lambda wn: (-wn[1], wn[0]))
        return [(w, n) for w, n in ranked if n >= THRESHOLD][:SHOWN]

    def note(self) -> str:
        words = self.overused()
        if not words:
            return ""
        listed = ", ".join(f"{w} ({n} times)" if i == 0 else f"{w} ({n})" for i, (w, n) in enumerate(words))
        return (f"\nWords the booth has leaned on this round: {listed}. "
                "Reach for something else unless it's genuinely the right word.")
