"""What the engine's parts share: its constants, small helpers and records (the top of the old
engine.py, as it was)."""
import contextvars
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..facts import (SCORE_NAMES, to_yards,
)
from ..grounding import (SCENERY)
from ..hud import Hud
from ..league import OnAir
from .. import round_file
from ..models import (
    Line,
)
from ..chat import ChatMessage
from ..tree_calls import TREE_CALLS, TREE_CALLS_BY_LANGUAGE, TREE_LOW_CALLS  # noqa: F401 (tests import them here)
from ..round import Round, to_par
from ..state import ShotRecord

log = logging.getLogger("commentator.engine")   # (one logger for the whole package)
# Whose round a model call is about, when it isn't the live one (an early opener): see Engine._compose.
_ON_AIR: contextvars.ContextVar["OnAir | None"] = contextvars.ContextVar("on_air", default=None)


class HoleWatch:
    """Says when the hole banner shows a new hole. One misread (158 yards read as 58 while a ball is
    followed) isn't a new hole: after the first sighting, a new banner counts once it reads the same
    on two polls running with no shot still in play."""

    def __init__(self) -> None:
        self.current: str | None = None
        self._candidate: str | None = None
        self._seen = 0

    @property
    def pending(self) -> bool:
        """A new banner seen, not yet taken as the next hole."""
        return self._candidate is not None

    def changed(self, key: str | None, busy: bool = False) -> bool:
        if not key or key == self.current:
            self._candidate, self._seen = None, 0
            return False
        if self.current is not None:
            self._seen = self._seen + 1 if key == self._candidate else 1
            self._candidate = key
            if self._seen < (8 if self._dropped_digit(self.current, key) else 2) or busy:
                return False
        self.current, self._candidate, self._seen = key, None, 0
        return True

    @staticmethod
    def _dropped_digit(a: str, b: str) -> bool:
        """The same par with one length's digits inside the other's ("par 3 158" / "par 3 58"): the
        OCR losing a digit, not the next hole, unless it holds for several seconds."""
        pa, pb = a.split()[:2], b.split()[:2]
        da, db = re.sub(r"\D", "", " ".join(a.split()[2:])), re.sub(r"\D", "", " ".join(b.split()[2:]))
        return pa == pb and da != db and bool(da) and bool(db) and (da in db or db in da)


def _box_player(player_text: str | None) -> str | None:
    """The player box's first word, past any stray marks with no letter in them: GSPro 4.0's tee box
    can read "9 Josh E …" on one poll and "Josh E …" on the next, which must be the same player. Only
    the name's own words count, so an all-digit name stays itself rather than becoming its score."""
    words = _gspro_name(player_text or "").split() or (player_text or "").split()
    return next((w for w in words if any(ch.isalpha() for ch in w)), words[0] if words else None)


def _setup_key(hud: Hud) -> tuple | None:
    """One stroke's setup: who, which shot, and from how far, as the player box reads."""
    if not (hud.player_text and hud.shot_number and hud.to_pin):
        return None
    return _box_player(hud.player_text), hud.gspro_shot or hud.shot_number, hud.to_pin


def _opening_tee(hud: Hud) -> bool:
    """A round's first tee on screen: a player on shot 1 at even par, under a hole banner. The range
    shows neither a shot number nor a banner."""
    return bool(hud.player_text and hud.shot_number == 1 and hud.score_to_par == "E" and hud.par)


def _practice_toolbar(text: str | None) -> bool:
    """GSPro's practice mode on screen: its toolbar (… ROUND EDIT, QUICK CTP) sits where a round's
    player box would be. The range's own toolbar reads DRIVING RANGE … QUICK CTP, with no ROUND EDIT."""
    words = set((text or "").upper().split())
    return {"ROUND", "EDIT"} <= words


def hole_label(hud: Hud, number: int | None) -> str:
    parts = [f"Hole {number}"] if number else []
    if hud.par:
        parts.append(f"par {hud.par}")
    if hud.hole_length:
        parts.append(hud.hole_length)
    return ", ".join(parts)


def _gspro_name(player_text: str) -> str:
    """GSPro's name in the player box: cut at the score or shot marker ("David E Shot 1" on the tee,
    "David 2nd (E) Shot 2" after)."""
    return re.split(r"\s+(?:(?:E|[+-]\d+|Shot|\d+(?:st|nd|rd|th))\b|\()", player_text.strip(), maxsplit=1)[0].strip()


def _tile_values(tiles: dict[str, str], unit: str) -> tuple:
    """A shot's (carry (game), carry (raw), total length, offline (raw)) tiles, in yards."""
    return tuple(_tile_yards(tiles, t, unit) for t in ("carry (game)", "carry (raw)", "total length", "offline (raw)"))


def _tile_yards(tiles: dict[str, str], name: str, unit: str) -> float | None:
    """A data tile's value in yards; None for a missing tile or a "--.--" placeholder."""
    value = re.search(r"-?\d+(?:\.\d+)?", tiles.get(name, ""))
    return to_yards(f"{abs(float(value.group(0)))} {unit}") if value else None


def _setup_slot_name(lines: list[str]) -> str | None:
    """A player name out of a setup-screen slot's OCR lines, or None. An empty slot shows a centered
    "+" that OCRs to a stray mark ("+", "1", a lone letter, a CJK glyph); a real name has a letter and
    at least two characters, which filters those out. A slot holds exactly ONE name (first and last
    on a single line count as one); clicking the pill opens a dropdown that stacks several players, so
    more than one name-like line means the picker is open - ignore the read and let the next poll (with
    it closed) get a clean one, rather than mangle several players into one slot."""
    named = [t for ln in lines if len(t := ln.strip()) >= 2 and re.search(r"[A-Za-z]", t)]
    # GSPro's own "no player" entry (and 3.59's added-but-unpicked pill), however the OCR spaces it.
    if len(named) != 1 or " ".join(named[0].lower().split()) == "empty slot":
        return None
    return named[0]


# Setup-screen pixel probes (grey 0-255), measured on GSPro at 3450x2160 (regions are fractional, so
# resolution-independent): the selected PLAYERS tab has mean ~242 (Match Settings/Holes selected: ~28);
# a filled name pill's median is ~122 with almost no near-white pixels; an empty "+" slot's median ~29;
# an open player dropdown is a near-white list (median ~238, ~60% near-white) that spills over the slots
# below it, where its entries would otherwise read as players.
_PLAYERS_TAB_LIT = 180
# The HOLES tab reads ~240 selected and under 30 otherwise; a checked nine's box is ~0.8 near-white
# pixels, an unchecked one none.
_HOLES_TAB_LIT = 180
_MATCH_TAB_LIT = 180
_NINE_CHECKED = 0.4
_PILL_MEDIAN = (90, 170)
_LIST_LIGHT = 0.25
# GSPro 3.59's setup screen (Screen.ui "new", NEW_UI_REGIONS) marks the selected tab with a blue
# underline (its strip is ~0.6 blue pixels, an unselected tab's none) and fills a name pill with a dark
# team colour (~0.9 saturated pixels; an empty slot is plain dark grey, none). Its checked nine is a
# white box like the old one, and its open dropdown the same near-white list.
_TAB_BLUE = 0.3
_PILL_COLOR = 0.5


def _tab_lit(tone, level: float) -> bool:
    """A setup tab's region shows it selected: bright (the classic menus' white pill) or blue (3.59's
    underline)."""
    return tone.mean >= level or tone.blue >= _TAB_BLUE


def gspro_ui(version: str | None, log_path: str | None = None) -> str:
    """Which setup menus GSPro shows: "new" from 3.59 (the redesign first out as a public beta),
    "classic" before. From the version its log reports; until that's read, from the log's name (3.59
    writes Unity's Player.log, 3.1 output_log.txt)."""
    if version and (m := re.match(r"\s*(\d+)\.(\d+)", version)):
        return "new" if (int(m.group(1)), int(m.group(2))) >= (3, 59) else "classic"
    return "new" if log_path and re.split(r"[\\/]", log_path)[-1].lower() == "player.log" else "classic"


def _nines_picked(tones: dict) -> tuple[int, int] | None:
    """(start hole, count) the HOLES tab's nine boxes show, or None when that tab isn't up or neither
    nine is checked."""
    tab = tones.get("setup_holes_tab")
    front, back = tones.get("setup_front9_box"), tones.get("setup_back9_box")
    if tab is None or not _tab_lit(tab, _HOLES_TAB_LIT) or front is None or back is None:
        return None
    f, b = front.light >= _NINE_CHECKED, back.light >= _NINE_CHECKED
    return (1, 18) if f and b else (1, 9) if f else (10, 9) if b else None


# How long after a shot is recorded to wait for GSPro's log to have its ball stop, to note when (a putt's
# recorded before it stops).
REST_WAIT_SECONDS = 45.0
# How long to keep reading a scramble's pick screen for its cards (it showed ~6.5 s, Bethpage 11-07).
PICK_SCREEN_SECONDS = 15.0
# How often the setup watcher OCRs the Play button while GSPro's log has it on a course (no setup tab
# lit): there the region is the live scene, so its pixels change every poll and it was OCR'd every poll
# (50,782 times on 11-07). Still read now and then, in case the log's state is wrong. (The range isn't
# read at all: its sky lights the tabs, and only End Round, which the log shows, leaves it for setup.)
SETUP_AWAY_POLL_SECONDS = 5.0


def _slot_state(tone) -> str:
    """'list' (an open dropdown covers it), 'filled' (a name pill), or 'empty', from a slot's Tone."""
    if tone.light >= _LIST_LIGHT:
        return "list"
    if _PILL_MEDIAN[0] <= tone.median <= _PILL_MEDIAN[1] or tone.color >= _PILL_COLOR:
        return "filled"
    return "empty"


# Two name pills whose tints are this close (largest channel difference) are one team colour: the
# same colour reads within a few levels, GSPro's team colours sit far further apart.
_TEAM_TINT_SAME = 30


def _tint_groups(slots: list[str | None], tints: list) -> list[list[str]] | None:
    """The named slots grouped by pill tint, in slot order; None when any named slot's tint is unread."""
    groups: list[tuple[tuple[int, int, int], list[str]]] = []
    for name, tint in zip(slots, tints):
        if not name:
            continue
        if tint is None:
            return None
        same = next((g for t, g in groups if max(abs(a - b) for a, b in zip(t, tint)) <= _TEAM_TINT_SAME), None)
        if same is not None:
            same.append(name)
        else:
            groups.append((tint, [name]))
    return [g for _, g in groups] or None
BOOKINGS_FRESH_SECONDS = 300.0
# A game-mode change alone (same players and nines) re-writes the front-loaded opener only once the
# mode has sat unchanged this long: clicking through the modes rewrote it on every click.
FORMAT_SETTLE_SECONDS = 3.0


def _play_button_course(text: str) -> str | None:
    """The course name out of GSPro's bottom-right "Play <course>!" setup button, or None when the
    text isn't that button (so this doubles as "are we on the setup screen?"). GSPro shows its own
    course name here, which can differ from the league's (matched via the schedule's gspro_course).
    Anything read past the "!" is a stray mark beside the button ("PLAY ABACOA GOLF CLUB! 0")."""
    m = re.match(r"\s*play\b[\s:·.\-]*(.+)", text or "", re.I)
    course = (m.group(1) if m else "").split("!")[0].strip().strip(".").strip()
    return course or None


@dataclass
class BoothCall:
    """A call the booth made out loud, and the stroke it's about: the stroke `shot_id` or `impact_seq`
    (a call made while that ball was in the air), or with `after` the same player's next one (a
    reaction's call); else `player`'s first stroke at or after `start` in the session's shots (anyone's,
    with no player). Bound to that stroke once it's hit."""
    speaker: str
    seq: int
    text: str
    player: str | None
    start: int
    shot_id: int | None = None
    impact_seq: int | None = None
    after: bool = False
    struck_after: int | None = None   # with no stroke named: the impact count when the call was made
    stroke: ShotRecord | None = field(default=None, repr=False)


@dataclass
class PendingOpening:
    """An opener composed and grounded from the setup screen while the course loads, held until the
    tee HUD confirms the same course and the same league team(s) before it airs."""

    course: str | None
    teams: frozenset
    players: frozenset   # the league players it covers (what the tee read is checked against)
    is_league: bool
    round: "Round"
    lines: list
    aired: bool = False  # the opener already played during the course load; the tee just sets the round up
    awaiting_load: bool = False  # composed ahead on the setup screen, waiting for the load screen to air it
    created: float = field(default_factory=time.monotonic)
    # Everyone on the setup screen, league players and guests alike, for matching the load screen's roster.
    roster_key: frozenset = frozenset()
    # Composed for the last roster read on an earlier setup screen (this one's PLAYERS tab was never
    # opened), so the tee must show exactly that roster, guests included, before it airs.
    guessed: bool = False
    # The setup's team colours (see Engine._setup_teams_key) its round was built with.
    colour_teams: frozenset | None = None


@dataclass
class PreparedChat:
    """The booth's take on the live chat: written and voiced, waiting for a gap between shots."""
    new: list[ChatMessage]         # the messages it's the first take on; they're marked answered once it airs
    items: list[tuple[Line, object]]
    written: float                 # monotonic
    expires: float                 # monotonic: when the oldest of `new` drops out of the conversation


@dataclass
class PreparedNews:
    """A golf-world aside written and voiced at round start, held until a moment suits it."""
    title: str
    trigger: str | None            # a NEWS_TRIGGERS key it's held for, or None for any quiet moment
    lines: list[Line]
    audio: list
    tie: Line | None = None        # replaces lines[0] when it airs on its trigger
    tie_audio: object = None
    kind: str = "news"             # "news", "trivia" (the course's history) or "score" (a live score)
    hole: int | None = None        # a course hole number a trivia aside waits for
    expires: float | None = None   # monotonic time a score aside goes stale and is dropped
    topic: str = ""                # the course, or the score's event key


# Shot tags that set off each news trigger.
NEWS_TRIGGER_TAGS = {
    "big_drive": {"monster_carry", "big_carry", "season_best_drive", "league_best_drive"},
    "trouble": {"in_bunker", "in_sand", "in_water", "in_hazard", "in_trees", "in_deep_rough", "in_fescue",
                "in_native", "out_of_bounds"},
    "wayward": {"big_miss_left", "big_miss_right", "mishit"},
    "great_approach": {"found_green"},
    "long_putt": {"long_putt"},
    "pure_strike": {"pure_strike"},
    "big_curve": {"big_slice_or_fade", "big_hook_or_draw"},
    # Tags the round carries rather than the ball (see _round_moment_tags). Golf headlines are mostly
    # about scoring and the state of a round, not the shape of one shot, so without these almost
    # every aside falls through to "any quiet moment".
    "good_hole": {"birdie_posted"},
    "bad_hole": {"blow_up_posted"},
    "the_turn": {"at_the_turn"},
}

# A foliage hit worth calling while the ball is in the air: at least this much of its speed lost (a
# brush through the leaves costs less and isn't worth cutting the flight line off for).
TREE_CALL_PENALTY = 15.0
# How long after impact a tree hit is watched for (GSPro logs foliage hits within ~0.3 s of impact).
TREE_CALL_WATCH_SECONDS = 3.0
# A shot the feed delivers this long after impact (GSPro 3.59's saved shot, written once the ball
# stops) gets no mid-flight call: the ball is down. A held GSPconnect-skipped shot is ~1.5 s late.
FLIGHT_CALL_LATE_SECONDS = 3.0
# How long after impact, outside a round, GSPro's log gets to say it played a shot (its "Shot Fired"
# comes within about half a second): one it never logs was hit while GSPro couldn't take it.
BANTER_WAIT_SECONDS = 40.0   # the most a routine shot's call waits for an aside on air to finish
UNPLAYED_SHOT_WAIT_SECONDS = 2.5
# How long a shot struck on a course's own range waits for GSPro's score line to say it was the course's
# first tee shot (see _course_shot_off_own_range): a drive's flight and roll, with room to spare.
RANGE_TEE_SHOT_WAIT_SECONDS = 30.0
RECAP_POLL_SECONDS = 30.0      # how often a league recap waiting on a round or its publish looks again
LAYOUT_WAIT_SECONDS = 20.0     # after a round's end, for GSPro 3.1's logged layout before the course file's
ROUND_FILE_CHECK_S = 2.0       # how often strokes in GSPro's round file with no shot yet are tried again
# GSPro writes its round file (currentRound.dat) in the frame it has the ball at rest, so a live one's
# stroke is the ball at rest, waited for after the swing (_wait_for_file). That wait wakes on the file's
# writes, and this often to look again and for a reason to stop (_file_wait_over).
FILE_WAIT_POLL_S = 0.25
FILE_WAIT_MAX_S = 120.0   # no ball rolls this long: GSPro has stopped writing (hung, or its file elsewhere)
# A stroke's penalty or drop is GSPro's next entry, written by its own rewrite in the same frame: a stroke
# read that soon after the file's write is read again once this has passed.
FILE_SETTLE_S = 0.05
# How long a full shot fed in flight, with no screen or log to see it stop, is waited on in GSPro's round file.
FILE_LATE_END_S = 20.0
# How long such a stroke, or one GSPro's file has at rest short of the hole with no log to say how the hole
# ended, is watched in the file for a drop written later (a scramble's pick of it).
FILE_END_DUE_S = 600.0
SCREEN_COVER_CHECK_S = 15.0    # how often the booth checks whether another program is covering GSPro
SCREEN_COVERED_S = 180.0       # covered this long and it's a health problem: the booth is reading blind
ROUND_FILE_RETRY_S = 180.0     # how long it's applied again after it changes, for shots recorded since
# The scorecard title is read every HUD poll for this long after GSPro's log says a hole (or the round)
# ended, the card being due; otherwise on the first poll SCORECARD_IDLE_SECONDS after the last read, to
# catch a card opened from the menu or one whose hole end the log missed. GSPro 4 shows the card for about
# 5 s; reads at most this far apart plus one poll stay under 4 s apart (every poll, when polls are slower).
SCORECARD_DUE_SECONDS = 30.0
SCORECARD_IDLE_SECONDS = 2.0
KEEP_GSPRO_LOGS = 5             # copies of GSPro's log kept from round ends (see _keep_gspro_log)
GSPRO_LOG_KEEP_BYTES = 8_000_000   # from the end of it: a round's lines and plenty before
# Calls whose prompt cache costs more to write (1.25x) than it saves: the closer runs once a round,
# so its cache is rarely read back before it expires (usage log, Oct 2026).
UNCACHED_KINDS = {"closer"}
CLOSER_PROMPTS = "closer-prompts.jsonl"   # in log_dir: every closer call's prompt and reply

# How long a putt's or short shot's setup line waits for the booth to stop talking before it's dropped.
PRESHOT_IDLE_WAIT_SECONDS = 5.0
# Beyond this share of a player's usual drive the green's out of reach, so a flight call aims at the
# fairway or the layup, not the flag. In the season's shots none from 1.15 times that (1.2 off a tee)
# found the green, and 6% from 1.1. A player with too few drives on record: a fixed distance.
REACH_SHARE = 1.1
UNKNOWN_REACH_YARDS = 300
# A lie leaving less than this share of the carry (rough, sand) is worth naming when it decides the reach.
LIE_REACH_NOTE = 0.97
# Within this share of the reach the green was plainly gettable, so a miss from a bad lie isn't excused.
REACH_COMFORT = 0.85
# How long after a round ends a non-tee setup is taken as the finished round's stale HUD.
AFTER_ROUND_QUIET_SECONDS = 120.0
# How often a hole's where-to-miss read (when there is one) is offered to the pre-shot line.
STRATEGY_CHANCE = 0.6
# An empty lie read at setup is retried this many times, this far apart (only the lie box and the
# name cell, so each is quick), before the pre-shot look goes ahead without it.
SETUP_LIE_RETRIES = 3
SETUP_LIE_RETRY_SECONDS = 0.25

# The words a flight line may use for the hazard its side is allowed to name.
NAMED_WORDS = {"water": ("water", "lake", "pond"), "out of bounds": ("out of bounds", "ob"),
               "penalty area": ("penalty area",),
               "bunker": ("bunker", "bunkers", "sand", "trap")}


def _names_other_scenery(text: str, allowed_kind: str | None) -> bool:
    """A flight line naming a course feature other than its side's named hazard. The minimap facts now
    put water and bunkers in the facts, which would let any flight line name them; only the trouble line
    whose hazard impact checks may (see FlightPlan.named)."""
    allowed = NAMED_WORDS.get(allowed_kind or "", ())
    lowered = text.lower()
    return any(re.search(rf"\b{re.escape(w)}\b", lowered) for w in SCENERY if w not in allowed)


# How much longer than the handicap fetch the league opener waits for Squabbit's leaderboards.
BOARD_WAIT_SECONDS = 3.0


_RATE_WORDS = ((0.0, "never"), (1 / 10, "one time in ten"), (1 / 5, "one time in five"),
               (1 / 4, "one time in four"), (1 / 3, "one time in three"), (2 / 5, "two times in five"),
               (1 / 2, "half the time"), (3 / 5, "three times in five"), (2 / 3, "two times in three"),
               (3 / 4, "three times in four"), (4 / 5, "four times in five"), (9 / 10, "nine times in ten"),
               (1.0, "every time"))


def _rate_words(hits: int, total: int) -> str:
    """A success rate the way a person says it: 'about two times in three' (33 of 50), not the count."""
    rate = hits / total
    frac, words = min(_RATE_WORDS, key=lambda fw: abs(fw[0] - rate))
    if abs(frac - rate) < 1e-9:
        return words
    return f"almost {words}" if frac in (0.0, 1.0) else f"about {words}"


def _week_gap_fact(team: str, net: int, leader: str, best: int, holes_left: int) -> str:
    """Where a team stands against the week's best posted net with holes to play, and what it takes."""
    last = "the last hole" if holes_left == 1 else f"the last {holes_left} holes"
    gap = net - best
    if gap > 0:
        return (f"{team} are {gap} shot{'s' if gap != 1 else ''} behind {leader}: net {to_par(-gap)} over {last} "
                f"ties them, net {to_par(-gap - 1)} beats them.")
    if gap == 0:
        return f"{team} are level with {leader}: net under par over {last} puts them ahead, net par keeps it tied."
    return (f"{team} are {-gap} shot{'s' if gap != -1 else ''} ahead of {leader}: net {to_par(-gap - 1)} or better "
            f"over {last} keeps them in front, net {to_par(-gap)} ties.")


def _distance_text(yards: float) -> str:
    """A finishing distance the way the HUD says it: feet and inches up close, else yards."""
    if yards >= 20:
        return f"{yards:.1f} yards"
    inches = round(yards * 36)
    feet, inches = divmod(inches, 12)
    return f"{feet} feet {inches} inches" if inches else f"{feet} feet"


SHORT_MISS_FEET = 10
SHORT_GAME_YARDS = 40
_ROUGH = {"rough", "deep rough", "semi rough", "first cut"}


def _lie_family(lie: str) -> str:
    """Lies that count as the same place for a correction: OCR flips rough and deep rough, and the
    fringe is "on the green" in a call."""
    return "rough" if lie in _ROUGH else "green" if lie in ("green", "fringe") else lie


def _lie_prep(lie: str) -> str:
    return "on" if lie in ("green", "fringe", "fairway", "tee") else "in"


def file_putt_outcome(t: round_file.Track) -> str:
    """How a putt GSPro's round file has ending the hole went: in the cup, inside the gimme circle, or
    short of it with the hole picked up at the maximum (a miss)."""
    return "holed" if t.in_cup else "missed" if t.picked_up else "conceded"


MISREAD_PUTT_MPH = 3.0   # no real putt this season left the face under 3 mph; a nudged ball does


def _misread_putt(rec: ShotRecord) -> bool:
    """A taken-back putt that barely moved: the launch monitor reading the player nudging the ball."""
    putt = rec.shot.is_putt or (rec.lie_before or "").lower() == "green"
    return putt and rec.shot.ball_speed is not None and rec.shot.ball_speed < MISREAD_PUTT_MPH


# How the ball met the cup (GsproState.cup_of), for the live reaction. Keyed (is_putt, cup).
_CUP_FACTS = {
    (True, "lip_out"): "It caught the cup and lipped out: it very nearly went in.",
    (True, "lip_in"): "It only just dropped: it caught the edge of the cup on the way in.",
    (False, "lip_out"): "The ball caught the edge of the cup and lipped out: it very nearly went in.",
    (False, "flagstick"): "The ball hit the flagstick.",
}


def _short_miss_fact(before: Hud, par: int | None) -> str | None:
    """A short putt that missed and was conceded: the model heard only "inside the gimme circle" and
    called a missed four-footer for par "a gentle touch". Say it was a miss and what it cost."""
    feet = (to_yards(before.to_pin) or 0) * 3
    if not feet or feet > SHORT_MISS_FEET:
        return None
    fact = f"This was a short putt, about {feet:.0f} feet, and it MISSED"
    if before.shot_number and par:
        was_for, ends = SCORE_NAMES.get(before.shot_number - par), SCORE_NAMES.get(before.shot_number + 1 - par)
        if was_for:
            fact += f": it was for {was_for.split(' ', 1)[1]}"
        if ends:
            fact += f", so the hole goes down as {ends}"
    return fact + ". It was a miss that cost a stroke, not a good lag."


def _conceded_hole_fact(before: Hud, par: int | None) -> str | None:
    """What a lag that stopped in the gimme circle scores: the conceded putt is a stroke too, so a lag
    for bogey makes a double ("a two-putt bogey" was called on one)."""
    if not (before.shot_number and par):
        return None
    strokes = before.shot_number + 1
    name = SCORE_NAMES.get(strokes - par)
    return (f"With the conceded putt counted, the hole goes down as {strokes} on the par {par}"
            + (f": {name}" if name else "") + ".")


def _feet_text(feet: float) -> str:
    """A tap-in distance as it's said: inches inside two feet ("14 inches"), else feet."""
    if feet < 2:
        return f"{max(round(feet * 12), 1)} inches"
    return f"{feet:.0f} feet"


def _great_shot_fact(start_yards: float | None, where: str, strokes: int | None = None,
                     par: int | None = None, lie: str | None = None, level: int | None = None) -> str:
    """The hype fact for a full shot that holed out or finished by the cup. It states the achievement
    and asks for excitement without handing over a phrase: "a superb approach, stuffed close" came
    back as "he's stuffed it" call after call. `level` is how big (out of 10, as a holed putt's is), so
    a chip to the gimme isn't called like a hole in one; a short-game shot also names its lie."""
    lie = (lie or "").lower()
    source = f" {'off' if _lie_prep(lie) == 'on' else 'out of'} the {lie}" if lie else " out"
    fact = f"From {start_yards:.0f} yards{source}, the ball {where}" if start_yards else f"The ball {where}"
    name = "a hole in one" if strokes == 1 and where.startswith("went in") else \
        SCORE_NAMES.get(strokes - par) if strokes and par else None
    if name:
        fact += f", which makes {name}"
    if level:
        return fact + (f". Excitement {level} out of 10: react in your own words rather than a stock golf "
                       "phrase.")
    return fact + (". That is an exceptional shot: react with real excitement, in your own words rather "
                   "than a stock golf phrase.")


# How a pre-written wrap says the hole was finished. It's written before the last putt, so it can only
# guess: three birdie putts that dropped aired as "stopped inside the gimme circle".
_WRAP_TAP_IN = re.compile(
    r"\b((?<!no )(?<!not a )gimme|tap[- ]?in|tap(s|ped)? (it )?in|conceded|concession"
    # a last putt that missed (so the next was conceded)
    r"|(didn't|did not|doesn't|does not|won't|never) (quite )?(drop|fall|go in|find the (cup|hole|bottom))"
    r"|never (quite )?(dropped|fell|went in|found the (cup|hole|bottom))"
    r"|lip(ped|s)? out|slid(es)? (by|past)|burn(ed|t|s)? the edge|missed (the|his|her|their|that) (\w+ )?putt|putt (just )?(missed|misses|stayed out|stays out)"
    r"|inside the (\w+ )?circle|inside (four|4|three|3) feet|left (it|himself|herself|themselves) (a|an) \w+)\b",
    re.I)
_WRAP_HOLED = re.compile(
    r"\b(holed|holes (it|the|out)|drain(s|ed)?|sinks?|sunk|sank|roll(s|ed)? (it )?(in|home)"
    r"|found the (bottom|cup)|in the (cup|hole)\b|(putt|it|ball) (dropped|drops|went in|goes in)"
    r"|makes? (the|his|her|their|that) (\w+ )?putt|made (the|his|her|their|that) (\w+ )?putt)\b", re.I)


# A hole-out from off the green: a pre-written wrap can't know the last stroke wasn't a putt.
_WRAP_HOLE_OUT = re.compile(r"\b(chip(ped|s)?[- ]in|hol(ed|es) out from|pitch(ed|es)? in|from off the green)\b", re.I)
# Any putt named in a wrap for a hole ended on a full shot (a chip-in) is a guess: the on-demand wrap
# sees how it finished.
_WRAP_PUTT_WORD = re.compile(r"\bputt", re.I)
_NEGATION = re.compile(r"\b(never|not|no|didn't|doesn't|won't|wouldn't|couldn't|nearly|almost)\b[\w\s,'-]{0,25}$", re.I)


def _wrap_contradicts_finish(lines, finish: str | None, shared: bool = False) -> bool:
    """A pre-written wrap that tells a different finish than the hole had: "holed", "holed_out" (holed
    with a full shot, a chip-in: any putt it names is wrong) or "conceded".
    With the finish not known yet (None) or "mixed" (one side holed, another was conceded, so who's
    meant can't be checked), any finish it tells is a guess. A make it denies ("never found the
    cup") or a hole-out off the green (a chip-in) isn't checked: always a guess. Nor is a make on a
    `shared` ball (a scramble side's partners): which of them holed it isn't checked."""
    text = " ".join(line.text for line in lines).replace("\u2019", "'")
    if _WRAP_HOLE_OUT.search(text):
        return True
    tap_in = _WRAP_TAP_IN.search(text)
    holed = [m for m in _WRAP_HOLED.finditer(text)]
    if holed and (shared or any(_NEGATION.search(text[:m.start()]) for m in holed)):
        return True
    if finish == "holed":
        return bool(tap_in)
    if finish == "holed_out":
        return bool(tap_in or _WRAP_PUTT_WORD.search(text))
    if finish == "conceded":
        return bool(holed)
    return bool(tap_in or holed)


WELCOME = re.compile(r"\b(welcome|good (morning|afternoon|evening|day)|hello|evening,? everyone)\b", re.I)
# A sign-off that opens like a broadcast ("Good afternoon, and what a finish") after the whole show.
CLOSER_GREETING = re.compile(r"^\W*good (morning|afternoon|evening|day)\b[\s,.!-]*(and\b\s*)?", re.I)
_OWNED = r"(?:round|card|scorecard|day|night|driver|putter|tee shot|short game|iron play|season)"
_FORM_MARK = re.compile(
    r"\b(\d+|zero|none|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s+(of|for|from)\s+"
    r"(his|her|their|the)?\s*(last\s+)?(\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\b"
    r"|averag|\bso far\b|\bthis round\b|\btoday\b|\btonight\b|per ?cent|%|\bseason\b|\bstart\b|\bstory\b"
    r"|\btrend\b|\ball (day|round)\b|\blately\b|\bof late\b|\bform\b|\brun of\b", re.I)
_FORM_TOPICS = (
    ("three-putts", r"three[- ]putt|3[- ]putt"),
    ("birdie looks", r"birdie (look|chance|putt|try|tries)"),
    ("approach leaves", r"approach|iron play|irons|leav|from the (hole|pin|cup)|proximity"),
    ("putting", r"putt|footer|from \w+ feet|inside \w+ feet|flat ?stick|\btouch\b|on the greens"),
    ("greens", r"greens in reg|\bGIR\b|\b(of|from|for) \w+ greens\b|\bgreens (hit|found)\b|\bfound \w+ greens\b"),
    ("fairways", r"\bfairways\b"),
    ("driving", r"\bdriv|off the tee"),
    ("scrambling", r"scrambl|up[- ]and[- ]down"),
    ("sand saves", r"sand save|bunker save"),
)


def form_topics(text: str) -> list[str]:
    """The round-sheet angles a spoken line used: a stat marker (a count like 'two of six', an average,
    'so far', 'this round') plus the subject it's about."""
    if not _FORM_MARK.search(text):
        return []
    return [name for name, pat in _FORM_TOPICS if re.search(pat, text, re.I)]


def _hole_key(label: str | None) -> str:
    """A hole label without its number, which can be read late ('par 4, 257 yards')."""
    return re.sub(r"^Hole \d+,\s*", "", label or "")


def _own_handicaps(db: Any) -> dict[str, float]:
    """Each player's own handicap index (handicap_index.indexes), for those with one."""
    from .. import handicap_index

    return {p: v["index"] for p, v in handicap_index.indexes(db).items() if v["index"] is not None}


def _own_handicaps_from(path: str) -> dict[str, float]:
    """_own_handicaps on a connection of its own to the season file, for a thread."""
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(f"{Path(path).as_uri()}?mode=rw", uri=True, timeout=30)) as db:
        db.execute("PRAGMA query_only = 1")
        db.row_factory = sqlite3.Row
        with db:   # one read transaction: the rounds and the layouts as of one moment
            db.execute("BEGIN")
            return _own_handicaps(db)
