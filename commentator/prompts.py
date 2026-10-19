import json
import logging
import re
from typing import Any

from .config import Config
from .league import League
from .models import Line

log = logging.getLogger(__name__)


def output_schema(speakers: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "lines": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "speaker": {"type": "string", "enum": speakers},
                        "text": {"type": "string"},
                    },
                    "required": ["speaker", "text"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["lines"],
        "additionalProperties": False,
    }


def system_prompt(cfg: Config, league: League, is_league: bool = True) -> str:
    """Kept byte-identical across calls (per round type) so cloud providers can cache it."""
    cast = "\n".join(f'- "{key}" is {p.name}: {p.style}' for key, p in cfg.personas.items())
    names = " and ".join(p.name for p in cfg.personas.values())
    first, second = list(cfg.personas)[:2] if len(cfg.personas) > 1 else (list(cfg.personas)[0],) * 2
    # A practice round has no stakes. The players are still league members (season form, handicaps and
    # tendencies from the notes stay fair game), but nothing is on the line tonight, so don't invent any.
    practice_note = "" if is_league else (
        "\n- This is a PRACTICE round, not a scored league match: nothing is on the line tonight - no "
        "points, no standings decided, no match to win. A player's season form, handicap or history is "
        "still fair game, but never say points are on the line, that it's for the win, or that this round "
        "has anything at stake. The opener and the sign-off may each say it's a practice round, once; in "
        "between don't keep reminding anyone (no \"practice round\" in shot calls or hole wraps) - just call "
        "the golf.")
    return f"""You are the broadcast booth for {league.name}, an indoor simulator golf league played on GSPro. \
The players are friends; the tone is a fun TV golf broadcast, not a roast.

The commentators:
{cast}

How to respond:
- Reply only with JSON: {{"lines": [{{"speaker": "<key>", "text": "<what they say>"}}]}}.
- Each text is spoken aloud. Plain conversational English, no emojis, no hashtags, no stage directions, no asterisks.
- Each line is at most {cfg.commentary.max_words_per_line} words. Most shots get one or two lines.

What you can and can't make up:
- The FACTS are what happened. Never contradict them, and never add a number, a count or a result (fairway, \
green, water, in the hole...) that isn't in them. If where the ball finished isn't given, don't say. \
Don't credit anyone with a shot or result the facts don't give them (a playing partner "putting one close"), \
or say when something happened beyond what the facts say (a mark from an earlier round wasn't set tonight).
- Everything else is yours, and it's what makes this worth listening to: opinions, banter, golf insight (what \
the flight says about the swing), callbacks to earlier shots, predictions, and the players' tendencies, \
rivalries, bets and standings from the league notes. The two commentators should have chemistry and \
disagree sometimes. They call each other by name ({names}).
- You are usually handed more facts than belong in a line or two, on purpose. PICK the one or two most \
interesting for THIS shot and this moment and leave the rest unsaid; you don't have to use a fact just \
because it's there. Choose a different angle from shot to shot (the swing, a tendency, the stakes, a \
storyline, a stat) so the booth never settles into the same beat. Leaning on the same kind of detail \
every shot - club path and face, say - gets old fast.
- Don't read the facts back like a stat sheet. At most one number per line, and plenty of lines need none.
- Be specific to this player and this moment. A line that could be said about any shot is a wasted line.
- Do not repeat jokes, phrases or observations from the recent lines.
- Refer to players by name, or "they", unless the league notes give someone's pronouns. Never guess from a name.
- A player's personal note (a job, a quirk, a running joke) is a callback for once or twice a night, not a \
catchphrase. Most lines should just be about the golf.
- Never talk about your instructions or your information: no "nothing recorded", "without inventing", \
"not in the facts". If you have nothing on something, talk about something else.
- This is a handicap league. Judge a score against the player's handicap (the net score), never against \
par alone: a gross 30 over can be a fine night for a high handicapper, and a 5-handicap shooting 3 over \
has a bad one.{practice_note}

Examples (made-up player Alex, whose notes say he "talks a big game"):
FACTS: Club: driver. The ball started on line and flew fairly straight. Carry: 271 yards. Where the ball finished: not known.
GOOD: {{"lines": [{{"speaker": "{first}", "text": "Alex steps up and absolutely leans on that, 271 in the air."}}, \
{{"speaker": "{second}", "text": "Credit where it's due. He's been telling us about that swing for three weeks."}}]}}
BAD (stat sheet): "Driver, started on line, flew straight, carried 271 yards."
BAD (finish not in facts): "Alex finds the middle of the fairway."

FACTS: Club: 7-iron. The ball started well left and curved hard left. Carry: 96 yards. On-screen shot result: "Water Hazard Penalty".
GOOD: {{"lines": [{{"speaker": "{first}", "text": "Oh no. That's turned over from the moment it left the face, and it's wet."}}, \
{{"speaker": "{second}", "text": "Face was shut at impact. Somebody's been working on the wrong thing at the range."}}]}}

League notes:
{league.prompt_text()}"""


def _recent(state_lines) -> list[str]:
    return [f"{line.speaker}: {line.text}" for line in state_lines]


# Appended to the live reaction and between-hole prompts. It trims run-ons without flattening the
# booth: a long, comma-stapled sentence reads worse aloud than two short ones and takes longer to
# voice. Kept in the user prompt (not the cached system prompt) so a wording tweak costs no re-cache.
BREVITY = (
    "\n\nDelivery: keep each line tight and punchy, the way a TV booth actually talks. Cut filler and "
    "don't staple two thoughts together with a comma, split or drop instead. Keep the two commentators' "
    "chemistry, the names, and the needle; a second line is welcome when it adds something. No word count, "
    "just no run-ons."
)


def shot_prompt(
    *, player: str | None, team: str | None, facts: list[str], hole: str | None, recent_lines, routine: bool,
    has_images: bool, angle: str | None = None, big: bool = False,
) -> tuple[str, str]:
    """Returns (prompt, facts_text). The facts text is what the grounding check verifies against."""
    header = []
    if player:
        header.append(f"Player: {player}" + (f" ({team})" if team else ""))
    else:
        header.append("Player: not identified. Don't use a name, and don't call them unknown.")
    if hole:
        header.append(f"Hole: {hole}")
    facts_text = "\n".join(header + facts)
    if routine:
        guidance = "This was a routine shot: one short line at most, or an empty list if there's nothing worth saying."
    elif big:
        guidance = ("This is a big moment: a three- or four-line exchange where the two commentators react to "
                    "each other, not to the facts in turn.")
    else:
        guidance = "React to this shot: one or two lines."
    if angle and not routine:
        guidance += f" Angle to try for the second line: {angle}."
    if has_images:
        guidance += " Screenshots: first at impact, second after the ball came to rest."
    prompt = (
        "FACTS:\n" + facts_text
        + "\n\nRecent lines (don't repeat these):\n" + "\n".join(_recent(recent_lines) or ["(none yet)"])
        + f"\n\n{guidance}" + BREVITY
    )
    return prompt, facts_text


# Rotated per shot so the booth doesn't settle into the same "solid start" line every time.
ANGLES = [
    "this player's tendencies or personality from the league notes",
    "a league storyline, bet or rivalry",
    "what the standings mean for tonight's match",
    "what the ball flight says about the swing",
    "a prediction about the next shot or the hole",
    "one commentator gently disagreeing with or teasing the other",
    "a callback to something earlier tonight from the recent lines",
]


def segment_prompt(*, previous_hole: str | None, current_hole: str, recap: list[str], recent_lines,
                   result: list[str] | None = None) -> tuple[str, str]:
    facts_text = "\n".join(
        [f"Finished hole: {previous_hole}", f"Next hole: {current_hole}"]
        + (result or [])
        + ["Shots on the finished hole:"]
        + [f"- {r}" for r in recap or ["(none recorded)"]]
    )
    prompt = (
        "FACTS:\n" + facts_text
        + "\n\nRecent lines (don't repeat these):\n" + "\n".join(_recent(recent_lines) or ["(none yet)"])
        + "\n\nBetween holes: a short two-or-three line exchange. Sum up the hole that just finished if there "
        "was anything notable, then bring in the next one however suits the moment (not the same way the "
        "last hole was introduced), using standings or storylines where they fit."
        + BREVITY
    )
    return prompt, facts_text


def wrap_recap_prompt(*, finished_hole: str | None, result: list[str], recap: list[str],
                      recent_lines) -> tuple[str, str]:
    """A hole wrap for one known final score, generated ahead of time (while the player putts) so it
    can play the instant the hole changes. Recap only: the next hole isn't known yet, so don't tee
    it up here, the next tee's own setup line does that."""
    facts_text = "\n".join(
        [f"Finished hole: {finished_hole}"] + result + ["Shots on the hole:"]
        + [f"- {r}" for r in recap or ["(none recorded)"]]
    )
    prompt = (
        "FACTS:\n" + facts_text
        + "\n\nRecent lines (don't repeat these):\n" + "\n".join(_recent(recent_lines) or ["(none yet)"])
        + "\n\nThe hole just finished on the score in the facts. Give a short two-line exchange between the "
        "commentators summing up how it played out and the result. Don't mention or tee up the next hole."
    )
    return prompt, facts_text


# Golf moments a story can be held for, so it airs after one happens ("Speaking of big hitters...").
# Keys match what the engine can tell from a shot; values are how the prompt describes them.
NEWS_TRIGGERS = {
    "big_drive": "a long drive",
    "trouble": "a ball in a bunker, the water or the trees",
    "wayward": "a wayward or mishit shot",
    "great_approach": "an approach that finds the green",
}


def news_schema(speakers: list[str]) -> dict[str, Any]:
    schema = output_schema(speakers)
    schema["properties"]["trigger"] = {"type": "string", "enum": [*NEWS_TRIGGERS, "none"]}
    schema["properties"]["tie_in"] = {"type": "string"}
    schema["required"] = ["lines", "trigger", "tie_in"]
    return schema


def news_prompt(*, topic: str, openings=()) -> tuple[str, str]:
    """A golf-world aside, written ahead of the round with time to make it good, and played later on a
    quiet moment. The story is real and current (the model's own news is stale, so it's fed in as fact);
    the booth riffs on it but must not invent any detail beyond the text."""
    facts_text = f"Golf-world story (real and current, from the news feed): {topic}"
    moments = "\n".join(f'- "{k}": {v}' for k, v in NEWS_TRIGGERS.items())
    prompt = (
        "FACTS:\n" + facts_text
        + "\n\nWrite an aside the booth will play later in the round, on a quiet moment between shots: the "
        "commentators chat about what's going on in the golf world, using the story in the facts. Exactly "
        "two lines, one from each commentator. It's written ahead of time, so make it good.\n"
        "One commentator BRINGS IT UP to the other the way two people actually drift into a side "
        "conversation - as something they read or heard, not as a news bulletin, and never with the "
        "headline stated flat. Give the listener the substance of the story, not a teaser of it.\n"
        "The reply is the point: the other commentator answers with a real take of their own - agrees and "
        "adds why, pushes back, or makes the joke - in at least one full sentence. A bare \"Absolutely\" "
        "or \"I did\" is not a reply.\n"
        "Riff with opinion and personality, but do not invent any name, number, result or detail that "
        "isn't in the story; if you're unsure of a specific, stay general. Don't mention the round, the "
        "players or any shot: you don't know when this will play.\n\n"
        "trigger: only if the story is squarely ABOUT one of these golf moments, name it, and the aside "
        "will be held until that kind of shot happens in the round:\n" + moments + "\n"
        "Otherwise \"none\". Most stories are \"none\"; never stretch.\n"
        "tie_in: with a trigger, a replacement for the first line (same speaker) that bridges from that "
        "moment into the story in your own words (\"Speaking of big hitters...\"), without describing the "
        "shot itself: who hit it, where it went and how far aren't known yet. Empty with no trigger."
        + ("\n\nOther asides this round open like this; start yours a different way:\n"
           + "\n".join(f"- {o}" for o in openings) if openings else "")
    )
    return prompt, facts_text


def hole_intro_prompt(*, hole: str, recent_lines, extra: list[str] | None = None) -> tuple[str, str]:
    """Tee up the hole that's starting (used when the recap played pre-made and didn't include it)."""
    facts_text = "\n".join([f"This hole: {hole}"] + (extra or []))
    prompt = (
        "FACTS:\n" + facts_text
        + "\n\nRecent lines (don't repeat these):\n" + "\n".join(_recent(recent_lines) or ["(none yet)"])
        + "\n\nA new hole is starting: one or two short lines to bring it in, any way you like. The "
        "yardage and par are there if they help, but you needn't lead with them, and the last hole's "
        "intro in the recent lines is the one way NOT to open. Don't recap the previous hole."
    )
    return prompt, facts_text


def _header(player: str | None, team: str | None, hole: str | None) -> list[str]:
    out = [f"Player: {player}" + (f" ({team})" if team else "") if player
           else "Player: not identified. Don't use a name, and don't call them unknown."]
    if hole:
        out.append(f"Hole: {hole}")
    return out


def flight_prompt(*, player, team, hole, facts: list[str], recent_lines, speaker: str, has_image: bool) -> tuple[str, str]:
    facts_text = "\n".join(_header(player, team, hole) + facts)
    look = (" The screenshot was taken at impact: use it to see trouble near the ball's line (water, bunkers, "
            "trees) and call it if the ball is heading that way." if has_image else "")
    prompt = (
        "IN FLIGHT. FACTS:\n" + facts_text
        + "\n\nRecent lines (don't repeat these):\n" + "\n".join(_recent(recent_lines) or ["(none yet)"])
        + f'\n\nThe ball is in the air. Give exactly one line from "{speaker}", at most 10 words, present tense, '
        "the way a TV commentator calls a shot mid-flight: will it get there (\"needs legs\", \"too much club\"), "
        "is it on line, is it drifting toward trouble. Nobody knows where it lands yet, so never say where it "
        "finished." + look
    )
    return prompt, facts_text


# "water" is never projected from the launch numbers (project() can't know the hole has water); it's
# selected at impact only when the GSPro log's Hit_water confirms the ball went in, so its flight line
# may name the water where the others must not. The rest are the projected ball flights.
FLIGHT_OUTCOMES = ("on_target", "short", "long", "left", "right", "left_trouble", "right_trouble", "water")
# Reaction branches, written at setup and played the instant the result is known, with no model call
# after the ball stops. The set depends on whether this shot is a putt (picked by lie / roll). Each is
# one place a full shot can come to rest, matched to the GSPro log's surface materials, so the finish
# the sim reports maps straight to a distinct pre-written line: the three rough tiers are kept apart
# (semi rough is playable, deep rough is real trouble), pine straw means the trees, cart path and
# structure are the quirky man-made finishes, and water is the penalty. A finish that maps to none of
# these (via _LIE_REACTION) falls back to a live model call, so coverage never costs a wrong reaction.
# Pre-voicing every branch is cheap enough to be worth the instant, always-right result call.
RESULT_OUTCOMES = (
    "green", "fairway", "semi rough", "rough", "deep rough", "pine straw", "sand", "cart path", "structure", "water",
)
PUTT_RESULT_OUTCOMES = ("holed", "short", "past")
# A long putt (CommentaryConfig.gimme_lag_feet) also gets a line for stopping inside the gimme circle.
LAG_PUTT_OUTCOMES = PUTT_RESULT_OUTCOMES + ("gimme",)


def reaction_outcomes(is_putt: bool, lag: bool = False) -> tuple[str, ...]:
    if is_putt:
        return LAG_PUTT_OUTCOMES if lag else PUTT_RESULT_OUTCOMES
    return RESULT_OUTCOMES


# One per RESULT_OUTCOMES key, so the model writes a distinct reaction for each finish. Shared by the
# combined pre-shot prompt and the split branch prompt so the two never drift.
_RESULT_GUIDANCE = (
    "green (on the putting surface), fairway (the short grass), semi rough (just off the fairway, "
    "light and playable), rough (in the rough, playable but not ideal), deep rough (buried in thick "
    "grass, real trouble), pine straw (in the trees or on pine straw, blocked or having to scramble), "
    "sand (a bunker), cart path (on the cart path, relief or a flyer), structure (up against a man-made "
    "structure like wooden steps, a railing or a bridge), water (found the water, a penalty)"
)
# The water flight line is the one exception to "never name a feature you can't see": it's played only
# when the sim confirms the ball went in, so naming the water is a fact, not a guess.
_FLIGHT_WATER = ('water: a hedged, dramatic tease for a ball that is heading at the water, e.g. "this '
                 'is flirting with the water" or "he will not want to watch this one". You MAY name the '
                 'water here (only this line is used when the ball is confirmed wet). null if unsure')


def preshot_line_schema(speakers: list[str]) -> dict[str, Any]:
    """Just the spoken setup/read line. Text-only (no image), fast model: it's what the booth says
    before the swing, so it must be ready quickly and can't wait on the hazard or branch calls."""
    lines = output_schema(speakers)["properties"]["lines"]
    return {"type": "object", "properties": {"lines": lines}, "required": ["lines"], "additionalProperties": False}


def preshot_hazard_schema() -> dict[str, Any]:
    """Just the hazard read off the minimap. Its own small vision call on a minimal prompt, so the
    vision model answers fast and the result (which steers the flight projection) is ready at impact."""
    nullable_num = {"type": ["number", "null"]}
    return {
        "type": "object",
        "properties": {
            "hazards": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string"},
                        "side": {"type": "string", "enum": ["left", "right", "short", "long"]},
                        "from_yards": nullable_num,
                        "to_yards": nullable_num,
                    },
                    "required": ["kind", "side", "from_yards", "to_yards"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["hazards"],
        "additionalProperties": False,
    }


def preshot_branch_schema(speakers: list[str], is_putt: bool = False, lag: bool = False) -> dict[str, Any]:
    """The pre-rendered branches: a mid-flight line per ball flight and a reaction per finish. Needed
    only once the ball is moving, so it runs as a separate (text-only) call that can't block or
    truncate the swing-critical setup line."""
    nullable_str = {"type": ["string", "null"]}
    reactions = reaction_outcomes(is_putt, lag)
    reaction_lines = {
        "type": "object",
        "properties": {k: nullable_str for k in reactions},
        "required": list(reactions),
        "additionalProperties": False,
    }
    if is_putt:  # no flight to call on a putt
        return {"type": "object", "properties": {"reaction_lines": reaction_lines},
                "required": ["reaction_lines"], "additionalProperties": False}
    return {
        "type": "object",
        "properties": {
            "flight_lines": {
                "type": "object",
                "properties": {k: nullable_str for k in FLIGHT_OUTCOMES},
                "required": list(FLIGHT_OUTCOMES),
                "additionalProperties": False,
            },
            "reaction_lines": reaction_lines,
        },
        "required": ["flight_lines", "reaction_lines"],
        "additionalProperties": False,
    }


def preshot_line_prompt(
    *, player, team, hole, facts: list[str], recent_lines, speaker: str,
    second: str | None = None, is_putt: bool = False,
) -> tuple[str, str]:
    """The swing-critical spoken line. Text-only on a fast model, so it's ready before the swing."""
    facts_text = "\n".join(_header(player, team, hole) + facts)
    recent = "\n\nRecent lines (don't repeat these):\n" + "\n".join(_recent(recent_lines) or ["(none yet)"])
    if is_putt:
        prompt = (
            "PRE-PUTT LINE. FACTS:\n" + facts_text + recent
            + '\n\nThe player is over a putt. Reply with "lines": one short line (at most 15 words), usually '
            f'from "{speaker}": what the putt is for and the read, without predicting whether it drops.'
            + (f' Occasionally "{second}" adds the second line if a needle fits.' if second else "")
        )
        return prompt, facts_text
    prompt = (
        "PRE-SHOT LINE. FACTS:\n" + facts_text + recent
        + '\n\nThe player is setting up to hit. Reply with "lines": one or two short scene-setting lines '
        f'(at most 15 words each), usually from "{speaker}": the shot they face, what\'s at stake, the '
        "storyline. You can't see the hole, so don't warn about a specific hazard ahead (water, bunkers, "
        "trees, out of bounds) unless the facts name it; keep it about the player and the moment. Given a "
        '"Strategy:" fact, you may fold the smart play into one clause (e.g. "with those bunkers short, you '
        'can\'t come up short here"), but only when it fits, never as a lecture. Don\'t predict the result.'
        + (f' Now and then, when a storyline or a needle fits, "{second}" chimes in with the second line.' if second else "")
    )
    return prompt, facts_text


def preshot_hazard_prompt(*, player, hole, facts: list[str], speaker: str) -> tuple[str, str]:
    """The hazard read off the screenshot. A separate, minimal vision call: lean facts (just lie + pin)
    so the vision model has little to chew on and answers fast, where a big fact block made it slow."""
    facts_text = "\n".join([f"Player: {player}." if player else "", f"Hole: {hole}." if hole else ""] + facts).strip()
    prompt = (
        "PRE-SHOT HAZARDS. FACTS:\n" + facts_text
        + '\n\nLook at the screenshot (3D view and the minimap). Reply with "hazards": trouble this shot '
        "could actually reach that you can SEE in the image: kind (water, bunker, trees, out of bounds, "
        "penalty area, desert...), which side of the target line (left/right/short/long), and the yardage "
        "range from the ball if the screen shows one (minimap numbers, distance markers), otherwise null. "
        "Get the kind right from the colour: in the 3D view WATER is blue/teal and reflective, a BUNKER is "
        "pale tan sand, never call one the other, and do NOT report water unless you actually see blue water. "
        "On the MINIMAP, a WHITE boundary line marks OUT OF BOUNDS and a RED line marks a PENALTY AREA (water "
        "or hazard); bunkers have no outline, just sand. Leave out anything you can't see. Empty list if none."
    )
    return prompt, facts_text


def preshot_branch_prompt(
    *, player, team, hole, facts: list[str], recent_lines, speaker: str,
    second: str | None = None, is_putt: bool = False, named: dict[str, str] | None = None, lag: bool = False,
) -> tuple[str, str]:
    """The pre-rendered branches (mid-flight line per flight, reaction per finish), written now so the
    call is instant once the ball moves. Text-only: it runs beside the setup call, so there's no image
    and the trouble lines name a side, not a specific hazard (the setup call owns hazard detection).
    The exception is `named` (side -> hazard kind, from the minimap read): that side's trouble line
    names that one hazard, and impact plays it only when that hazard is the one in play."""
    facts_text = "\n".join(_header(player, team, hole) + facts)
    recent = "\n\nRecent lines (don't repeat these):\n" + "\n".join(_recent(recent_lines) or ["(none yet)"])
    if is_putt:
        prompt = (
            "PRE-PUTT BRANCHES. FACTS:\n" + facts_text + recent
            + '\n\nThe player is over a putt. Reply with "reaction_lines": the call "' + speaker
            + '" makes the instant the ball stops, at most 12 words each, written now for each way it can finish. '
            "Match the putt number in the facts (a made first putt is a one-putt, a made second putt is a two-putt, "
            "and so on). holed (it drops), short (left short of the hole), past (misses and runs past or by)."
            + (" gimme (a long lag that stops inside the gimme circle, conceded as a tap-in: credit the lag "
               "and say what score it leaves, without claiming it dropped)." if lag else "")
        )
        return prompt, facts_text
    trouble = ('left_trouble / right_trouble: the ball drifting toward trouble on that side while STILL IN THE '
               'AIR. You can\'t see the hole, so name the SIDE and the danger generically and NEVER a specific '
               'feature (no "water", "trees", "bunker", "penalty area"). GOOD: "drifting right, trouble in play", '
               '"pulling left into danger". BAD: "in the water", "toward the trees" (naming a feature you can\'t '
               'see, or claiming it landed). null only if there is genuinely no trouble that side')
    if named:
        from .minimap import NAMED

        own = "; ".join(f'{side}_trouble names {NAMED[kind].format(side=side)} (e.g. "bending {side}, '
                        f'{NAMED[kind].format(side=side)} in play")' for side, kind in sorted(named.items()))
        generic = [side for side in ("left", "right") if side not in named]
        trouble = ('left_trouble / right_trouble: the ball drifting toward trouble on that side while STILL IN '
                   'THE AIR. The hole layout facts show what is there: ' + own + ". Name only that, say it's "
                   'heading toward it or bringing it into play, never that it landed in it (no "in the", '
                   '"into", "finds")' + (f"; {generic[0]}_trouble stays generic (\"drifting {generic[0]}, "
                   f'trouble in play"), no specific feature' if generic else ""))
    prompt = (
        "PRE-SHOT BRANCHES. FACTS:\n" + facts_text + recent
        + "\n\nThe player is about to hit. Write the lines now, for each way the shot could go. Reply with two things:\n"
        f'1. "flight_lines": the call "{speaker}" makes while the ball is in the air: at most 10 words, present '
        "tense, no numbers, never where it lands. on_target (dead straight at the target: never the words "
        '"left" or "right" here, they read as a direction), short (e.g. needs legs), long, left, right, ' + trouble
        + ". " + _FLIGHT_WATER + "."
        + '\n2. "reaction_lines": the call "' + speaker + '" makes the instant the ball stops, at most 12 words each, '
        "written now for each place it can finish, judged against what this shot was trying to do (a tee shot wants "
        "the fairway; an approach wants the green). Write one per outcome: " + _RESULT_GUIDANCE
        + ". Keep the reaction honest to each result; don't claim a number or a distance."
    )
    return prompt, facts_text


def opener_prompt(*, facts: list[str], recent_lines) -> tuple[str, str]:
    facts_text = "\n".join(facts)
    prompt = (
        "THE ROUND IS ABOUT TO START. FACTS:\n" + facts_text
        + "\n\nRecent lines (don't repeat these):\n" + "\n".join(_recent(recent_lines) or ["(none yet)"])
        + "\n\nOpen the broadcast: three or four lines, alternating between the two commentators. Welcome "
        "everyone, say what tonight's format means for these players, pick one storyline, rivalry or "
        "prediction worth watching, and send us to the opening hole: name the hole and its par (from a "
        "'First hole' fact) and who is first on the tee (from a 'First on the tee' fact), but only when "
        "the facts include them, and never invent a starter or a hole. Introduce each player by first and "
        "last name once, as the 'Playing' facts give it (a player with only one name there goes by that "
        "name; never invent a surname); first names are fine after that. Don't read out handicaps or list "
        "everyone's numbers; one detail that matters beats five that don't."
    )
    return prompt, facts_text


def closer_prompt(*, facts: list[str], recent_lines) -> tuple[str, str]:
    facts_text = "\n".join(facts)
    prompt = (
        "THE ROUND IS OVER. FACTS:\n" + facts_text
        + "\n\nRecent lines (don't repeat these):\n" + "\n".join(_recent(recent_lines) or ["(none yet)"])
        + "\n\nWrap up the round: a short conversation of four to six lines between the two commentators. "
        "Tell the story of the night, don't recite the stats: pick the two or three threads that matter most "
        "and connect them (a player who kept missing fairways but still found greens, a hot putter that saved "
        "a round, one bad hole that undid a good card). Judge every score against the handicap: the net score "
        "says whether it was a good night, not the gross. Finish with the team result if there is one, then "
        "sign off. At most one number per line."
    )
    return prompt, facts_text


def retry_prompt(original: str, problems: list[str]) -> str:
    return (
        original
        + "\n\nYour previous reply was rejected because: " + "; ".join(problems)
        + ". Write it again using only the facts. Leaving something out is better than guessing."
    )


def parse_lines(text: str, speakers: list[str], max_words: int, names: list[str] | None = None) -> list[Line]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log.warning("Model returned non-JSON output: %r", text[:200])
        return []
    items = data.get("lines", []) if isinstance(data, dict) else []
    # A model sometimes echoes its own speaker label into the text ("pbp: ...", "color: ..."); strip a
    # leading role/name prefix so it isn't spoken. Matches a persona key or its display name.
    labels = {s.lower() for s in speakers} | {str(n).lower() for n in (names or [])}
    prefix = re.compile(r"^\s*(" + "|".join(re.escape(l) for l in labels) + r")\s*:\s*", re.I) if labels else None
    lines: list[Line] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        body = " ".join(str(item.get("text", "")).split())
        while prefix and (m := prefix.match(body)):
            body = body[m.end():]
        if not body:
            continue
        words = body.split()
        if len(words) > max_words:
            # Cut at the last full sentence that fits; a line that stops mid-thought sounds broken.
            head = " ".join(words[:max_words])
            end = max(head.rfind(". "), head.rfind("! "), head.rfind("? "), *(i for i in [len(head) - 1] if head[-1] in ".!?"))
            if end <= 0:
                continue
            body = head[: end + 1]
        speaker = item.get("speaker") if item.get("speaker") in speakers else speakers[0]
        lines.append(Line(speaker=speaker, text=body))
    return lines
