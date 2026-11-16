"""Rejects commentary lines that state numbers or outcomes the facts don't support."""

import re
from dataclasses import dataclass

# No trailing \b, so ordinals ("2nd") and club codes ("7-iron") still yield their number.
# A comma with one or two digits after it is a decimal comma ("7,5 mètres" in French); three digits is
# English's thousands separator ("1,200"), read as two numbers as before.
NUMBER = re.compile(r"(?<![\w.])\d+(?:\.\d+|,\d{1,2}(?!\d))?")
NUMBER_WORDS = {
    "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100,
}
# Spelled-out numbers are only claims when attached to a count or unit ("three in a row",
# "twenty feet"); "those two" or "one of those" are just English.
WORD_CLAIM = re.compile(
    r"\b(" + "|".join(NUMBER_WORDS) + r")[\s-]+(in a row|straight|yards?|feet|foot|ft|mph|putts?|shots?|strokes?|holes?|points?)\b"
)

# A round score to par stated in a shot line ("three over for the round", "moving to five over"). The
# hole's own score ("two over on the hole") isn't matched: only a round marker or a move to a score.
_TENS = ("twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")
_SCORE_NUM = r"(?<![\w-])(even|level|(?:" + "|".join(_TENS) + r")(?:[\s-](?:one|two|three|four|five|six|seven|eight|nine))?|" \
    + "|".join(sorted((w for w in NUMBER_WORDS if w not in _TENS), key=len, reverse=True)) + r"|one|\d+)"
ROUND_SCORE_CLAIMS = (
    re.compile(rf"{_SCORE_NUM}(?:\s+(over|under))?(?:\s+par)?\s+(?:for|on)\s+the\s+(?:round|day|night)\b"),
    re.compile(rf"\b(?:moves?|moving|slips?|slipping|drops?|dropping|stays?|staying|stay|sits?|sitting|remains?|"
               rf"goes|going|gets|getting|falls?|falling|climbs?|climbing|back|keeps?|keeping|holds?|holding|"
               rf"takes?|taking|puts?|putting|brings?|bringing|leaves?|leaving)"
               # Who it moves: "moving the team to one under", "hold Dan and Derek at one over".
               rf"\s+(?:[\w'’&-]+\s+){{0,4}}?(?:to|at)\s+{_SCORE_NUM}(?:\s+(over|under)|\s+par)\b"),
)
# Only whole signed numbers state a score to par: a "+3.5" swing path or a "+3 degrees" slope doesn't.
_ROUND_FACT = re.compile(r"(?<![\w.])([+-]\d+)(?!\d|\.\d|\s*(?:°|%|(?:degrees?|mph|rpm|yards?|feet|ft|percent)\b))"
                         r"|\beven par\b|\bE\b")


# A score to par on one type of hole ("two under on the par threes"): only the wrap's tally fact gives
# one, and the model adding it up itself got it wrong.
_PAR_TYPE = r"par[\s-]?(3|4|5|three|four|five)(?:s|'s|es)\b"
PAR_TYPE_CLAIM = re.compile(rf"{_SCORE_NUM}(?:\s+(over|under))?(?:\s+par)?\s+(?:on|across|over)\s+(?:the\s+|those\s+|all\s+the\s+)?{_PAR_TYPE}")
# "On the par threes, Corey is two under": a few words on, a verb, then a score with a direction (or
# "even"), not one scoped elsewhere ("two over for the round") or an adverb ("even Corey").
PAR_TYPE_CLAIM_AFTER = re.compile(
    rf"{_PAR_TYPE}(?:,?\s+[\w&-]+){{0,6}}?(?:\s+(?:is|are|sits|stands|was|were|went|goes|has gone)|'s)\s+(?:at\s+)?"
    rf"{_SCORE_NUM}(?:\s+(over|under)|(?:(?<=even)|(?<=level))(?=\s+par\b|\s*(?:[,;.!?]|$|and\b|but\b)))(?:\s+par)?\b"
    rf"(?!\s+(?:par\s+)?(?:for|on|in|through|after|since)\b)")
_PAR_TYPE_FACT = re.compile(r"\b(even|(\d+) (over|under)) on the par ([345])s\b")
_PAR_TYPE_SIDE = re.compile(r"score by hole type through \d+ holes: (.+?) is ([^\n]*)")
_PAR_NUM = {"3": 3, "4": 4, "5": 5, "three": 3, "four": 4, "five": 5}


def wrong_par_type_tally(line: str, facts: str) -> str | None:
    """A score on the par 3s/4s/5s the line claims that no fact gives (or that differs from the fact).
    Each side's tally is kept apart: a claim in a sentence naming one side is checked against that
    side's; with no side named, any side's tally for that par will do."""
    lowered = line.lower()
    sides: dict[str, dict[int, int]] = {}
    for fact in _PAR_TYPE_SIDE.finditer(facts.lower()):
        sides[fact.group(1)] = {int(m.group(4)): 0 if m.group(1) == "even" else int(m.group(2)) * (-1 if m.group(3) == "under" else 1)
                                for m in _PAR_TYPE_FACT.finditer(fact.group(2))}

    def allowed(m: re.Match, par: int) -> set[int]:
        start = max(lowered.rfind(c, 0, m.start()) for c in ".;!?") + 1
        end = min((i for c in ".;!?" if (i := lowered.find(c, m.end())) >= 0), default=len(lowered))
        sentence = lowered[start:end]
        named = [t for side, t in sides.items() if re.search(rf"\b{re.escape(side)}\b", sentence)]
        return {t[par] for t in (named if len(named) == 1 else sides.values()) if par in t}

    # A bare "even" leading into "on the par threes" is a score only after a verb ("he's even on the par
    # threes"), not the adverb opening "Even on the par threes, club selection matters".
    claims = [(m, m.group(1), m.group(2), m.group(3)) for m in PAR_TYPE_CLAIM.finditer(lowered)
              if m.group(1) not in ("even", "level") or re.match(r"\s+par\b", lowered[m.end(1):])
              or re.search(r"(?:\b(?:is|are|sits|stands|was|were|stays?|remains?|at)|'s)\s*$", lowered[:m.start()])]
    claims += [(m, m.group(2), m.group(3), m.group(1)) for m in PAR_TYPE_CLAIM_AFTER.finditer(lowered)
               if not any(f.start() < m.end() and m.start() < f.end() for f, *_ in claims)]
    for m, word, direction, par in claims:
        value = _score_word_value(word)
        if value is None or (value and not direction):
            continue
        if (-value if direction == "under" else value) not in allowed(m, _PAR_NUM[par]):
            return m.group(0)
    return None


def _score_word_value(word: str) -> int | None:
    word = word.replace("-", " ")
    if word in ("even", "level"):
        return 0
    if word.isdigit():
        return int(word)
    parts = word.split()
    words = {**NUMBER_WORDS, "one": 1}
    if all(p in words for p in parts):
        return sum(words[p] for p in parts)
    return None


def wrong_round_score(line: str, facts: str) -> str | None:
    """The round score to par a line claims when no fact gives it ("three over for the round" at +23)."""
    lowered = line.lower()
    allowed = {0 if m.group(1) is None else int(m.group(1)) for m in _ROUND_FACT.finditer(facts)}
    for pattern in ROUND_SCORE_CLAIMS:
        for m in pattern.finditer(lowered):
            value = _score_word_value(m.group(1))
            if value is None:
                continue
            if value and not m.group(2):
                continue   # "five for the round" isn't a score to par
            claim = -value if m.group(2) == "under" else value
            if claim not in allowed:
                return m.group(0)
    return None

# A lead or deficit on the card ("leads by two", "one behind", "a two-shot buffer"): only the standing
# facts give one. Adding the scores up itself, the booth had the leader "one behind" and a one-shot lead
# as "two".
_MARGIN_NUM = r"(a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|\d+)"
_SHOTS = r"(?:[\s-](?:shots?|strokes?))"
MARGIN_CLAIMS = (
    (re.compile(rf"\b(?:leads?|leading|led|ahead|up|clear)\s+by\s+{_MARGIN_NUM}{_SHOTS}?\b"), "ahead"),
    (re.compile(rf"\b{_MARGIN_NUM}{_SHOTS}?\s+(behind|back|adrift|ahead|in front)\b"
                rf"(?!-|\s+(?:the|this|that|it|its|his|her|a|an|into|in|on|onto|to|out|up|down|from|towards?|over|"
                rf"across|through|home|inside|where|there)\b)"),
     None),
    (re.compile(rf"\b{_MARGIN_NUM}[\s-](?:shot|stroke|under)[\s-](?:lead|buffer|advantage|cushion|edge|margin|gap)\b"),
     "ahead"),
    (re.compile(rf"\b{_MARGIN_NUM}[\s-](?:shot|stroke)[\s-]deficit\b"), "behind"),
)
# "gets one back" / "picks a shot back" is a stroke recovered, and "brought that one back" a ball, not a margin.
_MARGIN_RECOVER = re.compile(r"\b(?:gets?|got|getting|grabs?|grabbed|claws?|clawed|picks?|picked|wins?|won|takes?|took|"
                             r"earns?|earned|gives?|gave|giving|brings?|brought|bringing|pulls?|pulled|drags?|dragged|"
                             r"draws?|drew|spins?|spun|sucks?|sucked|hits?|knocks?|knocked|sends?|sent|steps?|stepped)"
                             r"\s+(?:\w+\s+)?$")
_ONE_PRONOUN = re.compile(r"\b(?:that|this|the|which|each|every)\s+$")   # "that one back": a ball
# A margin some time ago ("was three back"): the comeback fact gives that, not the standing now.
_MARGIN_PAST = re.compile(r"\b(?:was|were|had been|earlier|before|started|began|once)\b")
_STANDING = re.compile(r"on the card[^:]*: (.+?) (?:leads by (\d+) shots?|(?:are|is) tied for the lead)((?:; .+? is \d+ back)*)\.")
_TRAILER = re.compile(r"; (.+?) is (\d+) back")
# Any margin a fact states: the standing, a lead change, the league's stakes and week-gap facts, a recap's.
_MARGIN_FACT = re.compile(r"\b(?:leads?|leading|outright,) by (\d+)\b|\b(\d+) (?:shots? )?(?:behind|ahead|back|clear)\b")
_PAST_MARGIN_FACT = re.compile(r"\b(\d+) shots off the lead\b")


def wrong_margin(line: str, facts: str) -> str | None:
    """A lead or deficit the line claims that the facts don't give, or that has the leader trailing (or,
    head to head, the trailer ahead)."""
    lowered, facts_lower = line.lower(), facts.lower()
    behind: dict[str, int] = {}   # each competitor on the card: shots off the lead
    for m in _STANDING.finditer(facts_lower):
        for name in re.split(r", | and ", m.group(1)):
            behind[name.strip()] = 0
        for t in _TRAILER.finditer(m.group(3)):
            behind[t.group(1)] = int(t.group(2))
    margins = {int(a or b) for a, b in _MARGIN_FACT.findall(facts_lower)}
    past = {int(n) for n in _PAST_MARGIN_FACT.findall(facts_lower)}
    margins |= {abs(x - y) for x in behind.values() for y in behind.values() if x != y}
    for pattern, direction in MARGIN_CLAIMS:
        for m in pattern.finditer(lowered):
            word = m.group(1)
            if word in ("a", "an") and not re.match(_SHOTS, lowered[m.end(1):]) \
                    and not re.match(r"[\s-](?:shot|stroke)", lowered[m.end(1):]):
                continue   # "a back" isn't a count
            if _MARGIN_RECOVER.search(lowered[:m.start()]) or (
                    word == "one" and direction is None and _ONE_PRONOUN.search(lowered[:m.start()])):
                continue
            value = 1 if word in ("a", "an") else _score_word_value(word)
            if not value:
                continue
            start = max(lowered.rfind(c, 0, m.start()) for c in ".;!?") + 1
            sentence = lowered[start:m.start()]
            # A margin some time ago only from the comeback fact, its direction not the standing now's. The
            # tense is the claim's own clause: "Corey was three back, but he leads by three" claims three now.
            clause = re.split(r",|;|\b(?:but|and|while|yet|now)\b", sentence)[-1]
            if _MARGIN_PAST.search(clause):
                if value not in past | margins:
                    return m.group(0)
                continue
            if value not in margins:
                return m.group(0)
            way = direction or ("behind" if m.group(2) in ("behind", "back", "adrift") else "ahead")

            def card_names(text: str) -> list[str]:
                hits = [(min(x.start() for w in re.split(r"[\s&]+", n) if len(w) > 2
                             for x in re.finditer(rf"\b{re.escape(w)}\b", text)), n) for n in behind
                        if any(len(w) > 2 and re.search(rf"\b{re.escape(w)}\b", text) for w in re.split(r"[\s&]+", n))]
                return [n for _, n in sorted(hits)]
            named = card_names(sentence)
            # Two on the card against each other ("Aces' two-shot buffer over Birdies"): their own gap only,
            # not a margin some other pairing or the week's board gives.
            ends = [i for c in ".;!?" if (i := lowered.find(c, m.end())) >= 0]
            after = re.split(r",|;|\b(?:but|and|while|yet|now)\b", lowered[m.end():min(ends, default=len(lowered))])[0]
            pair = card_names(clause)
            pair += [n for n in card_names(after) if n not in pair]
            if len(pair) == 2:
                gap = behind[pair[1]] - behind[pair[0]]   # > 0: the first named is ahead
                if (gap if way == "ahead" else -gap) != value:
                    return m.group(0)
                continue
            # "two behind Duffers": up against someone not on this card (the week's board), not the standing.
            if len(named) != 1 or ((nxt := re.match(r"\s+(?:(?:of|than|to|against|on|over)\s+)?([A-Z][\w'-]*)", line[m.end():]))
                                   and not any(nxt.group(1).lower() in re.split(r"[\s&]+", n) for n in behind)):
                continue
            if way == "behind" and behind[named[0]] == 0:   # a leader, tied or not, trails nobody
                return m.group(0)
            if way == "ahead" and len(behind) == 2 and behind[named[0]] > 0:
                return m.group(0)
    return None


# Words that always need explicit support: they claim something happened, whatever the phrasing.
OUTCOMES = {
    "water": ("water", "hazard", "penalty"),
    "hazard": ("water", "hazard", "penalty"),
    "penalty": ("water", "hazard", "penalty", "ob", "out of bounds"),
    "out of bounds": ("out of bounds", "ob"),
    "in the hole": ("in the hole", "holed"),
    "tap in": ("in the hole", "holed"),
    "taps in": ("in the hole", "holed"),
    "tapping in": ("in the hole", "holed"),
    "in the cup": ("in the hole", "holed"),
    "holed": ("in the hole", "holed"),
    "drains": ("in the hole", "holed"),
    "sinks": ("in the hole", "holed"),
    "drops for": ("in the hole", "holed"),
    "rolls it in": ("in the hole", "holed"),
    "rolls in for": ("in the hole", "holed"),
    "finds the cup": ("in the hole", "holed"),
    "finds the bottom": ("in the hole", "holed"),
    "birdie": ("birdie",),
    "eagle": ("eagle",),
    "bogey": ("bogey",),
}

# Lie words are only claims when they place the ball ("finds the fairway"), not in "missed the
# fairway" or "a rough swing". Synonyms map to the lie the HUD reports.
LIE_WORDS = {
    "fairway": "fairway", "short grass": "fairway", "rough": "rough", "green": "green",
    "putting surface": "green", "bunker": "bunker", "sand": "bunker", "trap": "bunker", "fringe": "fringe",
}
PLACING = r"(?:in|into|on|onto|finds?|found|finding|hits?|hitting|middle of|lands?|landed|landing|stays?|stayed|safely)"
# While the ball is in the air, "tracking down the middle of the fairway" describes its line, not
# where it lands; only words that say it has come down count.
LANDING = r"(?:finds?|found|finding|lands?|landed|landing|settles?|settled|comes? to rest|stops?|ends? up|drops?)"
# Shot facts say "the ball is now in the rough"; hole recaps say "finished in the rough" per shot.
ACTUAL_LIE = re.compile(
    r"(?:the ball is now|the ball currently lies|finished) (?:in|on) the ([a-z ]+?)(?:\.|,|$)", re.MULTILINE
)
# Hazards a model may point out when it has seen the screen, as long as it doesn't place the ball in one.
HAZARDS = ("water", "hazard", "penalty", "out of bounds")
# Course features a model can only know about by seeing the hole. Without a screenshot, a pre-shot
# or mid-flight line mentioning one is invented ("avoid the bunkers left" from a text-only model).
SCENERY = ("bunker", "bunkers", "sand", "trap", "water", "lake", "pond", "creek", "tree", "trees",
           "cactus", "cacti", "desert", "out of bounds", "ob", "penalty area", "hazard", "fescue",
           "cart path", "waste area")


def _scenery_support(word: str) -> tuple[str, ...]:
    """The fact words that support a scenery word in a line. A lie word is backed by any of its
    synonyms (sand/bunker/trap are one lie), so a correct current-lie mention isn't rejected on
    wording; anything else is backed only by itself, singular or plural."""
    bucket = LIE_WORDS.get(word) or LIE_WORDS.get(word.rstrip("s"))
    if bucket is None:
        return (word[:-1],) if word.endswith("s") and word[:-1] in SCENERY else (word,)
    return tuple(k for k, v in LIE_WORDS.items() if v == bucket)


# Shape words that contradict the computed flight. Directions are for a right-hander; GSPro
# reports them absolutely, so a lefty's fade still curves left.
SHAPE_CONFLICTS = {
    "curved gently left": ("fade", "slice", "left-to-right", "left to right", "cut"),
    "curved hard left": ("fade", "slice", "left-to-right", "left to right", "cut"),
    "curved gently right": ("draw", "hook", "right-to-left", "right to left", "turned over"),
    "curved hard right": ("draw", "hook", "right-to-left", "right to left", "turned over"),
    "started well right": ("down the middle", "dead straight", "on line"),
    "started well left": ("down the middle", "dead straight", "on line"),
}


@dataclass
class Check:
    ok: bool
    problems: list[str]


def _numbers(text: str) -> set[float]:
    """Digits, plus spelled-out numbers ("five points off the lead" in the league notes)."""
    words = re.findall(r"[a-z]+", text.lower())
    found = set()
    for n in NUMBER.findall(text):
        found |= _readings(n)
    return found | {float(NUMBER_WORDS[w]) for w in words if w in NUMBER_WORDS}


def _readings(raw: str) -> set[float]:
    """What a matched number could mean: "7,5" is 7.5 in French, or 7 and 5 ("holes 7,5")."""
    if "," not in raw:
        return {float(raw)}
    return {float(raw.replace(",", ".")), *(float(p) for p in raw.split(","))}


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


def check_line(
    line: str, facts: str, context: str, mode: str = "result", seen_image: bool = False,
    banned_names: list[tuple[list[str], list[str]]] | None = None,
) -> Check:
    """`facts` is about this shot (outcome words must come from here); `context`
    adds league notes, which may supply numbers like points or handicaps.

    mode: "result" (after the ball stops), "flight" (ball in the air, so nothing has landed
    anywhere), "preshot" (player at address) or "round" (an opener or wrap-up, where the facts
    are round totals rather than one ball's lie) or "news" (a golf-news aside: like "round", but
    the story's course isn't this one, so naming its trees or water is no guess). `seen_image` means the model was shown the
    screen, so it may mention hazards it could see, but never put the ball in one.
    `banned_names`: (a player's names, their partner's names) for partners sharing one ball in
    alternate shot: naming one without the other credits a shot nobody can attribute.
    """
    lowered = line.lower()
    allowed = _numbers(facts) | _numbers(context)
    problems: list[str] = []

    # Yardages are quoted rounded (a 258-yard carry called "260", or "nineteen" for 18.7), so allow
    # a few yards either way around a yardage-scale figure. Small counts stay near-exact, or a loose
    # window lets "25 feet" match a team's 24 points. Same rule for digits and spelled-out numbers.
    def near_a_fact(n: float) -> bool:
        return any(abs(n - a) <= 5 if a >= 40 else abs(n - a) < 1 for a in allowed)

    for raw in NUMBER.findall(line):
        whole = float(raw.replace(",", "."))
        if not (near_a_fact(whole) or ("," in raw and all(near_a_fact(float(p)) for p in raw.split(",")))):
            problems.append(f'the number "{raw}" is not in the facts')
    for word, _unit in WORD_CLAIM.findall(lowered):
        if not near_a_fact(float(NUMBER_WORDS[word])):
            problems.append(f'"{word}" is a count the facts don\'t give')
    if mode not in ("round", "news") and (said := wrong_round_score(line, facts)):
        problems.append(f'"{said}": that round score isn\'t in the facts')
    if said := wrong_par_type_tally(line, facts):
        problems.append(f'"{said}": that score on the par-type holes isn\'t in the facts')
    if mode != "news" and (said := wrong_margin(line, facts + "\n" + context)):
        problems.append(f'"{said}": that lead or deficit isn\'t what the standing facts give')

    facts_lower = facts.lower()
    # Normalised the same way as the line's words: GSPro's "sand" lie is a bunker for this purpose.
    # "deep rough" is still the rough (and a "greenside bunker" a bunker): its last word names the lie.
    actual = {LIE_WORDS.get(lie, LIE_WORDS.get(lie.split()[-1], lie)) for lie in ACTUAL_LIE.findall(facts_lower)}
    def says(names: list[str]) -> str | None:
        return next((n for n in names if re.search(rf"\b{re.escape(n.lower())}\b", lowered)), None)

    for names, partner in banned_names or []:
        if (name := says(names)) and not says(partner):
            problems.append(f'names {name}, but nobody knows which partner hit it; talk about the team')
    for word, lie in LIE_WORDS.items():
        if mode in ("preshot", "round", "news"):
            break  # at address, "needs to find the fairway" is a goal; round totals carry the lies
        if mode == "flight":
            placed = re.search(rf"\b{LANDING}\s+(?:\w+\s+){{0,5}}?{re.escape(word)}\b", lowered) or \
                re.search(rf"\b(?:in|into|on|onto)\s+the\s+{re.escape(word)}\b", lowered)
        else:
            placed = re.search(rf"\b{PLACING}\s+(?:\w+\s+){{0,3}}?{re.escape(word)}\b", lowered)
        if not placed:
            continue
        if mode == "flight":
            problems.append(f'"{word}": the ball hasn\'t landed yet')
        elif not actual:
            problems.append(f'"{word}": where the ball finished isn\'t in the facts')
        elif lie not in actual:
            problems.append(f'"{word}" contradicts the facts (ball finished in the {"/".join(sorted(actual))})')
    for term, support in OUTCOMES.items():
        if seen_image and mode != "result" and term in HAZARDS:
            if re.search(rf"\b{PLACING}\s+(?:\w+\s+){{0,3}}?{re.escape(term)}\b", lowered):
                problems.append(f'"{term}": says the ball is in it, which can\'t be known yet')
            continue
        # A round's facts count results in the plural ("two bogeys"), which backs "a bogey" just the same.
        if re.search(rf"\b{re.escape(term)}\b", lowered) and not any(
            re.search(rf"\b{re.escape(s)}s?\b", facts_lower) for s in support
        ):
            problems.append(f'"{term}" isn\'t shown in the facts')

    if mode == "flight":
        # "Toward the desert" is a call; "into the desert" says where it lands, which nobody knows yet.
        for word in SCENERY:
            if re.search(rf"\b{PLACING}\s+(?:the\s+)?{re.escape(word)}\b", lowered):
                problems.append(f'"{word}": says where the ball lands; it\'s still in the air')
    if mode not in ("result", "news") and not seen_image:
        for word in SCENERY:
            # A lie word in the line is supported by any of its synonyms in the facts: the facts say
            # "sand", the line "bunker", both are this one lie, so a correct current-lie mention stands.
            # Either may be plural: "a bunker left" backs "the bunkers" - a general plural ("stay out of
            # the trees") is ordinary golf talk, not a count, so it isn't held to the number seen.
            support = _scenery_support(word)
            if re.search(rf"\b{re.escape(word)}\b", lowered) and not any(
                re.search(rf"\b{re.escape(s)}s?\b", facts_lower) for s in support
            ):
                problems.append(f'"{word}": the course layout wasn\'t shown, so this is a guess')

    # Only this shot's flight; the history facts describe earlier shots' shapes.
    this_shot = "\n".join(
        f for f in facts_lower.splitlines() if "previous" not in f and "before this one" not in f
    )
    for flight, banned in SHAPE_CONFLICTS.items():
        if flight in this_shot:
            for word in banned:
                if re.search(rf"\b{re.escape(word)}\b", lowered):
                    problems.append(f'"{word}" contradicts the facts ({flight})')
    return Check(ok=not problems, problems=problems)


# The booth narrating its own rules or inputs ("we'll move on without inventing a story", "no
# shot-by-shot record to dissect"): never broadcast material.
_META = re.compile(
    r"\b(?:invent\w*|made[- ]up|the facts|nothing (?:on )?record(?:ed)?|no (?:[\w-]+ )?records? (?:to|of)|"
    r"(?:not|isn't|wasn't) (?:recorded|known|on record)|no data|no information)\b")


def meta_talk(line: str) -> str | None:
    """The phrase if the line talks about the booth's own instructions or missing data."""
    m = _META.search(line.lower())
    return m.group(0) if m else None


# A reply with nothing in it ("I did, Duke."): dead air in a two-line exchange.
_BARE = re.compile(r"^\s*(?:i did|i saw(?: that| it)?|i have|yes|yeah|yep|indeed|sure|quite|right)"
                   r"(?:[,!.]?\s*\w+)?\s*[.!?]?\s*$", re.IGNORECASE)


def bare_acknowledgement(line: str) -> bool:
    return bool(_BARE.match(line))


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


def _plain(line: str) -> str:
    return line.lower().replace("\u2019", "'")


def makes_call(line: str) -> bool:
    return bool(_CALL.search(_plain(line)))


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
