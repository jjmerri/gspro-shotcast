import asyncio
import json
import logging
import random
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .carry_model import CarryModel, correct_for_elevation
from .config import Config
from .facts import (
    SCORE_NAMES, _fmt_to_par, _to_par_value, club_delivery_facts, club_delivery_notable, club_name, flight_facts,
    history_facts, ordinal, preshot_facts, putt_elevation_inches, shot_facts, to_yards,
)
from .flight_pick import FlightPlan, Hazard, pick_line, pick_reaction, project
from .health import Health, RoundCounters, check_changed, check_files, evaluate_round, report_line
from .grounding import SCENERY, bare_acknowledgement, check_line, meta_talk, ngrams, note_keywords, repeats, uses_note
from .hud import NAME_DARK, Hud, describe, hole_key, name_sig_changed
from .interest import holed_putt_score, score_shot
from .league import League, course_match_score
from .llm import LLMClient
from .minimap import JevMinimap, MinimapRead
from .putt_read import REGION as PUTT_REGION, PuttRead, read_break
from .models import (
    HoleChangeEvent, Line, PreShotEvent, RoundEndEvent, RoundStartEvent, ScorecardEvent, Shot, ShotEvent,
)
from .prompts import (
    ANGLES, FLIGHT_OUTCOMES, NEWS_TRIGGERS, closer_prompt, flight_prompt, opener_prompt, output_schema, parse_lines,
    preshot_line_prompt, preshot_line_schema, preshot_hazard_prompt, preshot_hazard_schema,
    news_prompt, news_schema, preshot_branch_prompt, preshot_branch_schema,
    reaction_outcomes, retry_prompt, segment_prompt, shot_prompt, system_prompt,
    wrap_recap_prompt, hole_intro_prompt, scores_prompt, trivia_prompt, trivia_schema,
)
from .board import Board, place
from .history import History
from .news import GolfNews
from .scores import LiveScores
from .trivia import CourseTrivia
from .weather import Weather
from .round import FORMAT_NAMES, TEAM_BALL, Round, normalize_format, to_par
from .round import _join as _join_names
from .screen import Screen
from .sources.base import ShotSource
from .sources.gspro_log import M_TO_YD, MATERIAL_TO_LIE, GsproLogFollower, GsproState
from .speech import SpeechQueue
from .state import SessionState, ShotRecord
from .store import Store
from .usage import UsageLog, audio_summary

log = logging.getLogger(__name__)


class HoleWatch:
    """Says when the hole banner shows a new hole. One misread (158 yards read as 58 while a ball is
    followed) isn't a new hole: after the first sighting, a new banner counts once it reads the same
    on two polls running with no shot still in play."""

    def __init__(self) -> None:
        self.current: str | None = None
        self._candidate: str | None = None
        self._seen = 0

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


def _setup_key(hud: Hud) -> tuple | None:
    """One stroke's setup: who, which shot, and from how far, as the player box reads."""
    if not (hud.player_text and hud.shot_number and hud.to_pin):
        return None
    return hud.player_text.split()[0], hud.shot_number, hud.to_pin


def hole_label(hud: Hud, number: int | None) -> str:
    parts = [f"Hole {number}"] if number else []
    if hud.par:
        parts.append(f"par {hud.par}")
    if hud.hole_length:
        parts.append(hud.hole_length)
    return ", ".join(parts)


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
    if len(named) != 1 or named[0].lower() == "empty slot":   # GSPro's own "no player" dropdown entry
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


def _nines_picked(tones: dict) -> tuple[int, int] | None:
    """(start hole, count) the HOLES tab's nine boxes show, or None when that tab isn't up or neither
    nine is checked."""
    tab = tones.get("setup_holes_tab")
    front, back = tones.get("setup_front9_box"), tones.get("setup_back9_box")
    if tab is None or tab.mean < _HOLES_TAB_LIT or front is None or back is None:
        return None
    f, b = front.light >= _NINE_CHECKED, back.light >= _NINE_CHECKED
    return (1, 18) if f and b else (1, 9) if f else (10, 9) if b else None


def _unsettled(hud: Hud, tag: str = "") -> Hud:
    """A follow's last read from before the ball was seen at rest. GSPro often moves the player box on
    a second or two after touchdown, with the ball still rolling: off the green its lie then named the
    wrong spot about half the time (fairway for a ball that ran into the rough, rough for one that ran
    into a bunker), so it's dropped and the reaction names no lie; the next setup and GSPro's log fill
    in where it stopped. A ball already on the green stays there almost always, and the green read
    drives the wrap's pre-build, so that one is kept."""
    lie = (hud.lie or "").lower()
    if not lie or lie == "green":
        return hud
    log.info("%s lie %s read before the ball was at rest; not naming it", tag, hud.lie)
    return replace(hud, lie=None)


def _slot_state(tone) -> str:
    """'list' (an open dropdown covers it), 'filled' (a name pill), or 'empty', from a slot's Tone."""
    if tone.light >= _LIST_LIGHT:
        return "list"
    return "filled" if _PILL_MEDIAN[0] <= tone.median <= _PILL_MEDIAN[1] else "empty"


def _play_button_course(text: str) -> str | None:
    """The course name out of GSPro's bottom-right "Play <course>!" setup button, or None when the
    text isn't that button (so this doubles as "are we on the setup screen?"). GSPro shows its own
    course name here, which can differ from the league's (matched via the schedule's gspro_course)."""
    m = re.match(r"\s*play\b[\s:·.\-]*(.+)", text or "", re.I)
    course = (m.group(1) if m else "").strip().strip("!.").strip()
    return course or None


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
                "in_native"},
    "wayward": {"big_miss_left", "big_miss_right", "mishit"},
    "great_approach": {"found_green"},
}

# How long a putt's or short shot's setup line waits for the booth to stop talking before it's dropped.
PRESHOT_IDLE_WAIT_SECONDS = 5.0
# How long after a round ends a non-tee setup is taken as the finished round's stale HUD.
AFTER_ROUND_QUIET_SECONDS = 120.0
# How often a hole's where-to-miss read (when there is one) is offered to the pre-shot line.
STRATEGY_CHANCE = 0.6

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
    return fact + ". Call it plainly as a missed short putt that cost a stroke, never as good touch or a fine lag."


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


class Engine:
    """Three moments per shot: a setup line while the player addresses the ball, a call
    while it's in the air, and a reaction once it stops."""

    def __init__(
        self,
        cfg: Config,
        league: League,
        source: ShotSource,
        live_llm: LLMClient,
        segment_llm: LLMClient,
        speech: SpeechQueue,
        screen: Screen | None = None,
        rng: random.Random | None = None,
        flight_llm: LLMClient | None = None,
        preshot_llm: LLMClient | None = None,
        await_preshot: bool = False,
        store: Store | None = None,
        round_llm: LLMClient | None = None,
        opener_llm: LLMClient | None = None,
        closer_llm: LLMClient | None = None,
        health: Health | None = None,
    ) -> None:
        self.cfg = cfg
        # Is the booth still reading GSPro right? In memory unless the caller gives it a file.
        self.health = health or Health()
        self._hc = RoundCounters()      # this round's reads, for the post-round checkup
        self._follower: GsproLogFollower | None = None
        self.league = league
        self.source = source
        if hasattr(source, "on_impact"):
            source.on_impact = self._capture_impact
        self.live_llm = live_llm
        self.segment_llm = segment_llm
        self.flight_llm = flight_llm or live_llm
        self.preshot_llm = preshot_llm or live_llm
        self.round_llm = round_llm or segment_llm
        # The opener is raced against the course load (fast model); the closer is pre-fetched on the
        # last hole, so it can be a richer, slower model. Both fall back to round_llm.
        self.opener_llm = opener_llm or self.round_llm
        self.closer_llm = closer_llm or self.round_llm
        self.speech = speech
        # Commentary off records shots and rounds but never calls a model or speaks (see _complete).
        self.commentary_on = cfg.commentary.enabled
        if league is not None and speech is not None:
            self.speech.spoken = league.spoken
        if speech is not None and not self.commentary_on:
            self.speech.muted = True
        self.screen = screen
        self.minimap = self._make_minimap()
        self.putt_reader = (self.cfg.putt_read.enabled and self.screen is not None and self.commentary_on
                            and self.cfg.screen.capture == "monitor")
        self.rng = rng or random.Random()
        self.state = SessionState(recent_lines_max=cfg.commentary.recent_lines)
        # Live golf headlines for occasional between-shots banter; None when the feature is off.
        self.news = GolfNews(cfg.news.feeds, cfg.news.limit, max_age_days=cfg.news.max_age_days) if cfg.news.enabled and self.commentary_on else None
        w = cfg.weather
        self.weather = Weather(w.latitude, w.longitude, w.place, location=w.location, country=w.country) if (
            w.enabled and self.commentary_on
            and (w.location or (w.latitude is not None and w.longitude is not None))) else None
        self._news_bits: list[PreparedNews] = []    # this round's asides, written and voiced at the start
        self._news_task: asyncio.Task | None = None
        self._news_aired: list[int] = []            # round hole seqs an aside played on
        self._news_used: set[str] = set()           # stories already used this session
        self._news_moment: dict | None = None       # the last shot, for the aside that may follow it
        # Live scores and course trivia: more asides, sharing the news asides' slots and spacing.
        self.scores = LiveScores(cfg.scores.tours, cfg.scores.teams) if cfg.scores.enabled and self.commentary_on \
            else None
        self.trivia = CourseTrivia(cfg.trivia.pages) if cfg.trivia.enabled and self.commentary_on else None
        self._score_holes: list[int] = []           # round hole seqs a live score is written for as they start
        self._score_said: dict[str, list[str]] = {}  # event key -> what the booth said about it, this session
        self._score_written: set[str] = set()       # "key@version": scores already written up, this session
        self._aside_tasks: set[asyncio.Task] = set()
        self.speakers = list(cfg.personas)
        self._persona_names = [p.name for p in cfg.personas.values()]  # for stripping leaked speaker labels
        self.schema = output_schema(self.speakers)
        # Two cached variants: a practice round has no stakes on the line (see system_prompt). The
        # `system` property picks per the current round; both are byte-identical across calls within a
        # round type, so provider prompt-caching still holds.
        self._system_league = system_prompt(cfg, league, is_league=True)
        self._system_practice = system_prompt(cfg, league, is_league=False)
        self.league_text = league.prompt_text()
        # The example lines a model might lift verbatim; the BAD ones are what not to say, so a
        # line sharing words with them isn't copying.
        examples = self._system_league.split("Examples", 1)[-1].split("League notes:", 1)[0]
        self.system_examples = "\n".join(l for l in examples.splitlines() if not l.startswith("BAD"))
        # Counters and samples read by the eval command.
        self.stats: Counter[str] = Counter()
        self.rejections: list[tuple[str, list[str]]] = []
        self.flight_latencies: list[float] = []
        self.usage = UsageLog(cfg.log_dir)
        if hasattr(self.speech, "usage"):
            self.speech.usage = self.usage   # per-request voice cost/latency rows
        Path(cfg.log_dir).mkdir(parents=True, exist_ok=True)
        self.transcript = Path(cfg.log_dir) / f"transcript-{time.strftime('%Y-%m-%d')}.txt"
        self._events: asyncio.Queue[ShotEvent | HoleChangeEvent | PreShotEvent | None] = asyncio.Queue()
        self._preshot_task: asyncio.Task | None = None
        self._preshot_model_task: asyncio.Task | None = None
        # Wrap recaps pre-generated and pre-voiced during a putt, keyed by the score the hole might
        # finish on, so the hole wrap plays instantly on the hole change with no model call.
        self._wrap_branches: dict | None = None
        self._wrap_task: asyncio.Task | None = None
        self._wrapped_hole: str | None = None  # hole already wrapped (on the scorecard), so the hole change doesn't repeat it
        # The round closer, pre-generated and pre-voiced on the last hole before the final putt,
        # branched by the deciding side's possible final score (solo rounds only). Played at round end
        # only when the actual final score matches a branch, else the on-demand closer runs.
        self._closer_branches: dict | None = None
        self._closer_task: asyncio.Task | None = None
        self.last_hole_result: str | None = None  # GSPro's last result banner (Birdie/Par/...), if read
        self._shot_id: int | None = None
        # The lie read at each setup, while the player stood over the ball, by _setup_key: the impact
        # read comes ~3 s after the swing, when the lie box may already show where the ball landed.
        self._setup_lies: dict[tuple, str] = {}
        # (who, shot) -> whether that stroke's setup showed the distance in yards: GSPro shows yards off
        # the green and feet on it. Kept apart from the distance, which switches to feet when the player
        # changes to putt mode, so a later read in feet can't hide a setup off the green.
        self._setup_yards: dict[tuple, bool] = {}
        self._shot_seq = 0
        # Hazard map and pre-written mid-flight lines from the latest pre-shot look.
        self.plan: FlightPlan | None = None
        # In-memory unless the caller passes the season database; replays use made-up shots.
        self.store = store or Store(None)
        self._publish_task: asyncio.Task | None = None
        self._publish_lock = asyncio.Lock()
        # Every 6-word phrase said this season, so a line can't come back weeks later.
        self._season_phrases: set[tuple[str, ...]] = set()
        for text in self.store.spoken_lines():
            self._season_phrases |= ngrams(text, 6)
        self.carry = CarryModel(cfg.commentary.lie_carry_factors, self.store.carry_pairs(),
                                cfg.commentary.course_altitude_ft)
        self.current_course: str | None = None
        # GSPro reports each player's handedness with their shots; slope talk depends on it.
        self.handed: dict[str, str] = {}
        # Last HUD shot number seen per player on the current hole. GSPro counts a penalty in the
        # shot number but plays no shot for it, so the number jumps (2 -> 4 on a water drop); a jump
        # of more than one means a penalty the booth would otherwise miscount. Reset each hole.
        self._last_hud_shot: dict[str, int] = {}
        # Authoritative game state tailed from GSPro's own log (strokes, penalties, lie, pin, holed
        # shots), used instead of the OCR penalty heuristic when source.gspro_log_truth is on.
        self.gspro = GsproState()
        self._narrated_pen: dict[tuple[int, int], int] = {}  # penalties already told, per (player, hole)
        # A player's personal note ("built this booth") is a callback, not a catchphrase: count the
        # aired lines that lean on it this round, and stop it after NOTE_USES_PER_ROUND.
        names = {w for n in league.players for w in re.findall(r"[a-z]+", n.lower())}
        self._note_keys = {p: k for p, info in league.player_info.items()
                           if (note := info.get("notes")) and (k := note_keywords(note, names))}
        self._note_uses: dict[str, int] = {}
        self._delivery_last = False   # the last reaction carried club-delivery facts
        self._fir_streak: dict[str, int] = {}  # consecutive fairways off the tee, per player (live)
        # A full shot awaiting its finishing distance: GSPro logs the post-shot tdist only when it sets
        # up that player's NEXT shot, so the finish is filled in then. idx -> (shot_id, per-index
        # impact snapshot from _idx_snap).
        self._pending_finish: dict[int, tuple] = {}
        self._finish_seq: dict[int, int] = {}   # highest shot seq that resolved finish for an index,
        #                                         so overlapping concurrent handlers can't reorder it
        # Recorded shots by GSPro "Shot Fired" number: fire -> (shot_id, ShotRecord, GSPro index, log
        # hole), so a stroke GSPro later takes back (a mulligan) can be found and flagged.
        self._fire_rows: dict[int, tuple] = {}
        self._mulligans_done: set[int] = set()
        self._pickups_seen = 0   # GSPro pick-ups (score capped at the maximum) already applied
        self._pick_gimmes_seen = 0   # scramble picks inside the gimme circle already credited
        self._conceded_ids: set[int] = set()   # shots whose conceded putt row is stored
        self._form_said: dict[tuple, list[tuple[int, list[str]]]] = {}   # (round, side) -> [(hole of round, angles)]
        self._form_pending: tuple | None = None   # (round, side, players, hole of round) the sheet last went to
        self._lie_corrected: set[int | None] = set()   # shots whose called finish the booth has corrected
        self._mulligans_called: set[int] = set()   # id() of swings already announced as taken back
        # Each index's last recorded shot (shot_id, ShotRecord, log hole, penalties then), so the next
        # shot's lie (where it was played from) can correct where that one came to rest.
        self._last_row: dict[int, tuple] = {}
        # Per player, a commentator's call (commentator key, predicted make?, hole at commit) on that
        # player's current putt, committed only when the pre-shot line airs and graded against the
        # result when the stamped hole matches. Keyed by player so a partner's shot can't bin it.
        self._pending_prediction: dict[str, tuple[str, bool, str | None]] = {}
        self._pred_turn = 0
        self._plan_made_at = 0.0
        self.current_par: int | None = None
        # Replays at speed deliver the shot instantly, which would cancel every setup line.
        self.await_preshot = await_preshot
        self.round: Round | None = None
        # An opener built ahead off the setup screen, waiting for the tee HUD to confirm it (preround).
        self._pending_opening: PendingOpening | None = None
        # The league roster the opener was last front-loaded for, and a counter so a newer front-load
        # (the roster changed) supersedes an older compose still in flight.
        self._opener_prepared_for: frozenset = frozenset()
        # (start hole, hole count) picked on GSPro's setup screen for the next round: 18 from hole 1 (GSPro's
        # default) once the setup screen shows a course, then whatever the HOLES tab's nines say. None
        # when the setup screen wasn't seen (the booth started later): the schedule/config guess applies.
        self._setup_holes: tuple[int, int] | None = None
        # Whether _setup_holes came off the HOLES tab itself (else it's GSPro's default, unconfirmed), and
        # when that read's frame was grabbed, so an older sample never undoes a newer read.
        self._setup_holes_read = False
        self._setup_holes_at = float("-inf")
        # (when, (start, count)) of the latest HOLES-tab nines the quick sampler caught (see _sample_setup_tabs).
        self._holes_snap: tuple[float, tuple[int, int]] | None = None
        self._opener_prepared_holes: tuple[int, int] | None = None
        # GSPro's game mode, off the setup screen's MATCH SETTINGS tab: "scramble", "shamble",
        # "alternate", or None (stroke play, GSPro's default, or a mode that shares no ball). Only a
        # scramble sets the round's format; leagues mostly leave GSPro on stroke play and score their
        # format in an app. A shamble or alternate shot only stops the log calling it a scramble.
        self._setup_format: str | None = None
        self._opener_prepared_format: str | None = None
        # The last roster (and slot order) read off a setup screen's PLAYERS tab, for a setup that never shows it.
        self._last_setup_roster: tuple[list[str], list[str]] | None = None
        # (when, frame, tones) of the latest PLAYERS-tab frame the quick sampler caught (see _sample_players_tab).
        self._players_snap: tuple | None = None
        self._idle_card_logged: dict | None = None
        self._scramble_pairs: set = set()   # the log's teammate pairs last applied to the round
        self._opener_gen = 0
        # Set when a round opens so the one redundant, late pre-shot on the opening tee shot is skipped.
        self._skip_opening_preshot = False
        self.history = History.load(cfg.history_file)
        # The league's week results and standings from Squabbit, refreshed with the handicaps at each
        # round start. None until a fetch works; the history file's standings stand in.
        self.board: Board | None = None
        self._board_task: asyncio.Task | None = None
        self.hole_number: int | None = None
        # Whether the current hole's number was read off the screen rather than counted on.
        self.hole_number_read = False
        # For spotting the end of a round when GSPro's final scorecard isn't read.
        self._last_activity = time.monotonic()
        # The activity timestamp before the current setup, i.e. how quiet it was just before this
        # tee: a long gap marks a round that was abandoned rather than finished.
        self._prev_activity = 0.0
        self._putted_on_last_hole = False
        self._round_end_queued = False
        self._round_ended_at = 0.0
        self._shots_at_round_end = 0
        self._fixture_saved_at: dict[str, float] = {}
        self._strategy_hole: str | None = None   # the hole a where-to-miss read was last offered on
        # GSPro log counters already acted on (round ends / completions), and whether the range quiet
        # has been logged, so each transition is handled once.
        self._log_round_ends = 0
        self._log_round_completes = 0
        self._range_noted = False
        self._join_tried = False   # joining a round already under way is tried once, at the first setup
        # Shot handlers run in the background (the follow/reaction mustn't block the loop); keep
        # references so they aren't garbage-collected mid-flight.
        self._shot_tasks: set[asyncio.Task] = set()
        # Fire-and-forget background work (e.g. a handicap refresh once a prepared opener has aired).
        self._bg_tasks: set[asyncio.Task] = set()
        # Holes with a score on the last scorecard read, and when it was read. A fresh card
        # showing none means GSPro has started a new round.
        self._holes_played = 0
        self._holes_played_at = 0.0

    # ---- event plumbing -------------------------------------------------------------------

    async def run(self) -> None:
        models = (("pre-shot", self.preshot_llm), ("in-flight fallback", self.flight_llm),
                  ("reactions", self.live_llm), ("segments", self.segment_llm),
                  ("round opener", self.opener_llm), ("round closer", self.closer_llm))
        if not self.commentary_on:
            log.info("Commentary off: recording shots and rounds only")
            models = ()
        for role, llm in models:
            log.info("Model for %s: %s:%s%s", role, llm.provider, llm.model, " (with screenshots)" if llm.cfg.vision else "")
        # The voice model and the OCR model both load on first use (several seconds on CPU). Kick
        # both off now, in parallel with the rest of startup, so the first hole doesn't pay for them.
        if self.commentary_on:
            asyncio.create_task(self.speech.prepare(Line(self.speakers[0], "Warming up.")))
        if self.screen and self.cfg.screen.ocr:
            asyncio.create_task(self._warm_ocr())
        if self.news is not None:
            asyncio.create_task(self._news_loop(), name="news")
        if self.weather is not None:
            asyncio.create_task(self._weather_loop(), name="weather")
        tasks = [asyncio.create_task(self.speech.run(), name="speech")]
        if self.source is not None:
            tasks.append(asyncio.create_task(self._pump_source(), name="source"))
        if self._log_truth:
            self._follower = GsproLogFollower(self.cfg.source.gspro_log, self.gspro)
            tasks.append(asyncio.create_task(self._follower.run(), name="gspro-log"))
        if self.cfg.health.enabled and self.cfg.source.type != "replay":
            asyncio.create_task(self._startup_health(), name="health")
        if self.screen and self.cfg.screen.ocr:
            tasks.append(asyncio.create_task(self._watch_hud(), name="hud-watch"))
            if self.cfg.commentary.preround:
                tasks.append(asyncio.create_task(self._watch_setup(), name="setup-watch"))
        try:
            while (event := await self._events.get()) is not None:
                try:
                    if isinstance(event, ShotEvent):
                        # In the background: the in-flight line plays at the top of handle_shot, but
                        # the follow and reaction take many seconds and must not block the next
                        # pre-shot or shot behind them.
                        task = asyncio.create_task(self._shot_then_news(event))
                        self._shot_tasks.add(task)
                        task.add_done_callback(self._on_shot_task_done)
                    elif isinstance(event, PreShotEvent):
                        # In the background: impact must never wait behind a setup line.
                        self._cancel_preshot()
                        self._preshot_task = asyncio.create_task(self.handle_preshot(event))
                        if self.await_preshot:
                            await self._preshot_task
                    elif isinstance(event, RoundStartEvent):
                        await self.handle_round_start(event)
                    elif isinstance(event, ScorecardEvent):
                        await self.handle_scorecard(event)
                    elif isinstance(event, RoundEndEvent):
                        await self.handle_round_end(event)
                    else:
                        await self.handle_hole_change(event)
                except Exception:
                    # One bad call shouldn't take the booth off air for the night.
                    log.exception("Failed handling %s", type(event).__name__)
            pending = [t for t in ({self._preshot_task, self._preshot_model_task} | self._shot_tasks) if t]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            await self._drain_speech()
        finally:
            # Cancel first, so a future exception in the audio tally below can't leak these tasks.
            for t in tasks:
                t.cancel()
            # Session-level audio tally, so a session that never finished a round (handle_round_end
            # early-returns then) still logs what TTS synthesized vs actually aired.
            if stats_obj := getattr(self.speech, "audio_stats", None):
                log.info("%s", audio_summary(stats_obj))

    async def _pump_source(self) -> None:
        try:
            async for event in self.source.events():
                await self._events.put(event)
        except Exception:
            log.exception("Shot source stopped; no more shots will come through")
        finally:
            await self._events.put(None)

    async def _watch_hud(self) -> None:
        """Polls the HUD for a new hole and for a player settling over a new shot."""
        holes = HoleWatch()
        undo = None   # (banner, hole number, number read, shots so far) before the last hole change
        last_setup = candidate = None
        candidate_since = 0.0
        setup_seq = moved_on = 0
        card_read_at = 0.0
        while True:
            await asyncio.sleep(self.cfg.screen.hud_poll_seconds)
            try:
                hud = await self.screen.read_hud_async(["player_box", "hole_box", "scorecard_title"])
            except Exception:
                log.exception("HUD poll failed")
                continue
            self._check_log_round()
            self._check_round_idle()
            if self._on_range():
                # The range isn't a round: no holes, no setups, no round start. Forget the last tee seen
                # so the first one after the range reads as new.
                holes, last_setup, candidate = HoleWatch(), None, None
                continue
            if hud.scorecard_open:
                # GSPro shows the scorecard between holes. Once per showing is plenty.
                if time.monotonic() - card_read_at > 5:
                    card_read_at = time.monotonic()
                    card = (await self.screen.read_hud_async(["scorecard"])).scorecard
                    if card:
                        if any(card.scores.values()):
                            self._keep_fixture_frame("scorecard")
                        self._holes_played = len({h for s in card.scores.values() for h in s})
                        self._holes_played_at = time.monotonic()
                        await self._events.put(ScorecardEvent(card=card))
                continue
            before_key = holes.current
            if holes.changed(hole_key(hud), busy=any(not t.done() for t in self._shot_tasks)):
                on_card = self._hole_on_card(hud)
                if undo and undo[0] == holes.current and undo[3] == self._shot_seq:
                    # Back to the banner of the hole before, with no shot in between: that change was a
                    # misread ("par 4 340" read as "par 4 40" for a few seconds), not two new holes.
                    log.info("Banner flipped back to %s with no shot played: the last hole change was a misread",
                             holes.current)
                    self.hole_number, self.hole_number_read = undo[1], undo[2]
                    undo = None
                    await self._events.put(HoleChangeEvent(previous=None, current=hole_label(hud, self.hole_number),
                                                           course=hud.course, correction="revert"))
                elif on_card == 0:
                    log.info("Banner %s isn't the next hole on the card (par %s); still hole %s",
                             hole_key(hud), hud.par, self.hole_number)
                else:
                    # OCR misreads the big hole number now and then (and rarely reads a lone "1" at
                    # all); trust it only when it's plausible.
                    number, self.hole_number_read = self.hole_number, False
                    if on_card:
                        number, self.hole_number_read = on_card, True
                    elif hud.hole_number and (number is None or hud.hole_number == number + 1):
                        number, self.hole_number_read = hud.hole_number, True
                    elif number is not None:
                        number += 1
                    undo = (before_key, self.hole_number, self.hole_number_read, self._shot_seq)
                    self.hole_number = number
                    await self._events.put(HoleChangeEvent(previous=None, current=hole_label(hud, number),
                                                           course=hud.course))

            # The player box only changes once the previous ball has stopped, so a new
            # (shot number, distance) that holds steady is a player addressing the next shot.
            setup = (hud.player_text and hud.player_text.split()[0], hud.shot_number, hud.to_pin)
            # A mulligan puts the player box back exactly as it was, so the re-hit's setup would read as
            # the one already handled. It's a new setup when, after a swing, the box moved on to that
            # player's next stroke (seen even once: the take-back can follow within a second) and then
            # came back.
            if last_setup and setup != last_setup and setup[0] == last_setup[0] and hud.shot_number \
                    and last_setup[1] and hud.shot_number > last_setup[1] and self._shot_seq > setup_seq:
                moved_on += 1
            if setup == last_setup and moved_on:
                last_setup, moved_on = None, 0
            if not hud.shot_number or setup == last_setup:
                candidate = None
                continue
            if setup != candidate:
                candidate, candidate_since = setup, time.monotonic()
                continue
            if time.monotonic() - candidate_since >= self.cfg.commentary.preshot_settle_seconds:
                last_setup, candidate, setup_seq, moved_on = setup, None, self._shot_seq, 0
                if (logged := self._log_hole_disagrees(hud)) is not None:
                    self.hole_number, self.hole_number_read, undo = logged, True, None
                    await self._events.put(HoleChangeEvent(previous=None, current=hole_label(hud, logged),
                                                           course=hud.course, correction="log"))
                self._prev_activity, self._last_activity = self._last_activity, time.monotonic()
                await self._emit_setup(hud)

    def _make_minimap(self) -> "JevMinimap | None":
        """The Jev minimap reader when minimap.provider is jev and it can run; otherwise None, and the
        hazard read stays on the preshot vision model."""
        if self.cfg.minimap.provider != "jev" or self.screen is None or not self.commentary_on:
            return None
        if why := JevMinimap.available():
            log.warning("minimap.provider is jev but %s; using the preshot vision model for hazards", why)
            return None
        return JevMinimap(self.cfg.minimap)

    async def _read_minimap(self, to_pin: float | None) -> MinimapRead:
        await asyncio.to_thread(self.screen.detect_mirror)
        region = self.screen.region("minimap")
        # Full resolution: the downscaled vision snapshot leaves the minimap's numbers too small to read.
        img = await asyncio.to_thread(self.screen.grab, region)
        return await self.minimap.read(img, to_pin)

    async def _read_putt(self) -> PuttRead:
        p = self.cfg.putt_read
        frames = await asyncio.to_thread(self.screen.burst, PUTT_REGION, p.frames, p.interval_seconds)
        return await asyncio.to_thread(read_break, frames)

    def _start_putt_read(self) -> "asyncio.Task[PuttRead]":
        task = asyncio.create_task(self._read_putt())
        task.add_done_callback(lambda t: t.cancelled() or t.exception())
        return task

    async def _emit_setup(self, hud: Hud) -> None:
        """Read the rest of the HUD and start the pre-shot look. The model call is the longest step,
        so it's kicked off the moment the lie is known and runs while the slope (and, on the tee, the
        other players) are still being read, rather than after. The frame it sees is grabbed once."""
        if self._after_round(hud):
            log.info("Setup ignored: %s (shot %s)",
                     "GSPro is in the menus after End Round" if self._log_rounds and self.gspro.mode == "menu"
                     else f"round {self.round.id} is complete and this isn't a new tee", hud.shot_number)
            return
        # The minimap read starts first so it runs during the HUD read below. It's cancelled for a putt.
        minimap_task = asyncio.create_task(self._read_minimap(to_yards(hud.to_pin))) if self.minimap is not None else None
        if minimap_task is not None:
            minimap_task.add_done_callback(lambda t: t.cancelled() or t.exception())
        # A distance in feet is a putt: the grid burst starts now, so it runs during the HUD read.
        putt_task = (self._start_putt_read() if self.putt_reader and "feet" in (hud.to_pin or "") else None)
        image = None
        if self.cfg.screen.send_images and self.preshot_llm.cfg.vision and self.commentary_on:
            image = await self.screen.snapshot_jpeg()
        # The lie, slope, and (on the tee) the other players all feed the pre-shot facts, so they must
        # be read BEFORE the model call is kicked. Kicking the call first overlapped it with this read
        # to save latency, but _preshot_model builds its facts synchronously, so slope and the stakes
        # facts were built from an empty HUD and never reached the model. (Tradeoff: the model call no
        # longer overlaps this read; if the pre-shot line starts landing late, this is the place.)
        # "others" (the opponents' board) is needed on the tee (round-start, crediting the tee shot)
        # and late in a league round, where _stakes_facts compares this player to the field.
        rr = self.round
        late = (rr is not None and not rr.ended and rr.is_league and 1 <= rr.holes - rr.seq + 1 <= 4)
        join = self._should_join()
        rest = ["lie", "slope"] + (["others"] if hud.shot_number == 1 or late or join else [])
        extra = await self.screen.read_hud_async(rest)
        hud.lie = extra.lie
        if key := _setup_key(hud):
            self._setup_yards[key[:2]] = "yard" in key[2]
            while len(self._setup_yards) > 8:
                del self._setup_yards[next(iter(self._setup_yards))]
        if hud.lie and (key := _setup_key(hud)):
            self._setup_lies[key] = hud.lie
            while len(self._setup_lies) > 8:
                del self._setup_lies[next(iter(self._setup_lies))]
        hud.slope_updown, hud.slope_side = extra.slope_updown, extra.slope_side
        hud.slope_side_degrees = extra.slope_side_degrees
        if "others" in rest:
            hud.others = extra.others
        player = self._player(hud.player_text, None)
        model_task = asyncio.create_task(self._preshot_model(hud, player, [image] if image else [], minimap_task,
                                                             putt_task))
        # If this setup is superseded before handle_preshot awaits the call, the task is orphaned;
        # retrieve its outcome so a failed one doesn't log "exception never retrieved".
        model_task.add_done_callback(lambda t: t.cancelled() or t.exception())
        self._preshot_model_task = model_task
        if join:
            await self._events.put(RoundStartEvent(hud=hud, joined=True))
        elif hud.shot_number == 1 and self._is_round_start(hud):
            await self._events.put(RoundStartEvent(hud=hud))
        log.info("Setup: %r shot %s, score %r, %s to pin, lie %s, others %s, slope %s/%s %s, elevation %s yd (screen read %ss)",
                 hud.player_text, hud.shot_number, hud.score_to_par, hud.to_pin, hud.lie,
                 [(o.name, o.to_pin, o.score_to_par) for o in hud.others], hud.slope_updown, hud.slope_side,
                 hud.slope_side_degrees, None if hud.elevation_yards is None else round(hud.elevation_yards, 1),
                 f"{self.screen.last_read_seconds:.2f} (capture {self.screen.last_capture_seconds:.2f})")
        await self._events.put(PreShotEvent(hud=hud, image=image, model_task=model_task))

    def _after_round(self, hud: Hud) -> bool:
        """The HUD still showing the finished round's last green: after the sign-off, GSPro's stale HUD
        read as a fresh setup and aired "a tap-in for par on eighteen" over the end of the show. Only a
        new opening tee (shot 1) counts once a round is over, for a couple of minutes or until GSPro
        leaves the menus. The same goes from the moment GSPro's log says every hole is holed out: the
        sign-off can be a minute off, and a setup read meanwhile is the last green, not a shot to come.
        Once GSPro's log says End Round, even an opening tee is the quit round's, until a course is
        picked again: a round started and quit at once aired its first-tee line after the quit."""
        r = self.round
        if self._log_rounds and self.gspro.mode == "menu" and (r is None or r.ended):
            return True
        if r is None or hud.shot_number == 1:
            return False
        if not r.ended:
            return r.log_complete
        # Nothing hit since the round ended: whatever GSPro shows now (a 1,450-yard "putt" on a hole
        # past the end of the round got a line three minutes after the sign-off) isn't a shot to call.
        if self._log_rounds and self.gspro.fires <= self._shots_at_round_end:
            return True
        return time.monotonic() - self._round_ended_at < AFTER_ROUND_QUIET_SECONDS

    def _should_join(self) -> bool:
        """The booth started with a round already under way (GSPro's log shows shots on this course
        and the booth isn't tracking a round): pick it up at the first setup, once."""
        if self._join_tried or not self._log_rounds or self.round is not None:
            return False
        g = self.gspro
        if g.mode != "course" or not g.shots_since_course or g.first_hole is None or g.current_hole is None:
            return False
        self._join_tried = True
        return True

    def _is_round_start(self, hud: Hud) -> bool:
        """The opening-tee signal: everyone on shot 1 at even par, every other player still the
        full hole away. The hole number doesn't matter, so a round can start on the front or the
        back nine. The one look-alike is the next tee after everyone pars the opening hole, told
        apart by the scorecard already showing a played hole."""
        if hud.shot_number != 1 or self._on_range():
            return False
        if self._log_rounds and self.gspro.mode == "menu":
            # GSPro's last word was End Round: the tee on screen is a round on its way out, not a new one.
            log.info("Not a round start: GSPro is back in the menus (End Round), not on a course")
            return False
        if self._log_rounds and self.round is None and self.gspro.shots_since_course:
            # The booth came up after play began here: that's a round to join, not one to open.
            log.info("Not a round start: GSPro's log has %s shots on this course already",
                     self.gspro.shots_since_course)
            return False
        if hud.score_to_par != "E":
            log.info("Not a round start: shot 1 but score reads %r, not E", hud.score_to_par)
            return False
        # Every other shown player is also level: nobody has a score yet. The per-player distance
        # isn't checked; GSPro's elevation readout bleeds into the others region and misreads it.
        for other in hud.others:
            if other.score_to_par not in (None, "E"):
                log.info("Not a round start: other player %r reads %r, not E", other.name, other.score_to_par)
                return False
        if self.round and not self.round.ended:
            # A round is still tracked (it may have been abandoned without a clean finish). Only
            # treat this tee as a new round on firm evidence GSPro reset: a just-read scorecard
            # with no holes played, or a long enough quiet gap before this tee. Otherwise it's the
            # same round (e.g. walking to the next tee after an all-par hole).
            if self._holes_played == 0 and time.monotonic() - self._holes_played_at < 30:
                return True
            gap = time.monotonic() - self._prev_activity
            decision = bool(self._prev_activity) and gap > self.cfg.commentary.round_restart_idle_seconds
            log.info("Round-start look-alike while round %s in progress (holes_played=%s, gap=%.0fs): %s",
                     self.round.id, self._holes_played, gap, "new round" if decision else "same round")
            return decision
        course = self._tee_course(hud)
        if self.round is not None and self.round.ended and self.round.course == course:
            return True  # a finished round here: the next opening tee is a new round
        # No round tracked. A fresh opening tee is a new round unless a just-read scorecard shows
        # holes already played (the booth was started partway through a round). Shots recorded
        # earlier today no longer block it, so restarting the booth and teeing off still opens.
        mid_round = self._holes_played > 0 and time.monotonic() - self._holes_played_at < 30
        log.info("Round start? shot 1, score E, course %r: mid_round=%s -> %s", course, mid_round, not mid_round)
        return not mid_round

    @property
    def _log_truth(self) -> bool:
        """GSPro's own log is followed for game truth (source.gspro_log_truth, never for replays)."""
        return bool(self.cfg.source.gspro_log_truth) and self.cfg.source.type != "replay"

    @property
    def _log_rounds(self) -> bool:
        """GSPro's own log is followed, so it (not a guessed hole count) says when a round ends."""
        return self._log_truth

    def _on_range(self) -> bool:
        """GSPro is on the driving range, where the booth stays off: no commentary, nothing recorded."""
        on = self._log_rounds and self.gspro.mode == "range"
        if on != self._range_noted:
            self._range_noted = on
            log.info("GSPro %s the range: booth %s", "is on" if on else "left",
                     "off until a round is set up" if on else "back on")
        return on

    def _check_log_round(self) -> None:
        """Act on round-level lines in GSPro's log: a round holed out ("Round Is Complete", with the
        holes it actually covered) or left ("End Round", which before completion means quit early)."""
        if not self._log_rounds:
            return
        g, r = self.gspro, self.round
        live = r is not None and not r.ended
        if live and g.mode == "range" and not self._round_end_queued:
            # The range loaded with a round still open (it was left without the booth seeing it end).
            self._round_end_queued = True
            log.info("Round %s: GSPro is on the range; ending the round and cutting the booth off", r.id)
            self.speech.interrupt()
            self._events.put_nowait(RoundEndEvent(reason="GSPro went to the range", abandoned=True))
        if live:
            self._apply_log_holes(r)
            if not self._round_end_queued:   # once it's ending, End Round's reset mustn't wipe the scores
                self._sync_log_scores(r)
                if self.commentary_on and self.cfg.commentary.closer:
                    self._kick_group_closer(r)
        if g.round_completes != self._log_round_completes:
            self._log_round_completes = g.round_completes
            if live and not r.log_complete:
                r.log_complete = True
                log.info("Round %s: GSPro says the round is complete", r.id)
        if live and r.log_complete and g.active_holes and g.active_holes != r.holes:
            log.info("Round %s: GSPro's layout has %s holes in the round (was counting %s)", r.id,
                     g.active_holes, r.holes)
            r.set_holes(g.active_holes, confirmed=True)
        if live and r.log_complete and not self._round_end_queued and r.complete():
            self._round_end_queued = True
            self._events.put_nowait(RoundEndEvent(reason="final scorecard"))
        if g.round_ends != self._log_round_ends:
            self._log_round_ends = g.round_ends
            if live and not self._round_end_queued:
                self._round_end_queued = True
                if r.log_complete:
                    self._events.put_nowait(RoundEndEvent(reason="GSPro round finished"))
                else:
                    log.info("Round %s: GSPro's round was ended before it was complete (quit early)", r.id)
                    self._events.put_nowait(RoundEndEvent(reason="quit early in GSPro", abandoned=True))
            # Back to the menus: a fresh tee is a new round, and the setup watcher takes over.
            self._holes_played, self._holes_played_at = 0, 0.0

    def _sync_log_scores(self, r: Round) -> None:
        """Copy each finished hole's score from GSPro's log into the round, where it beats the scorecard
        read. Once End Round has reset the log state (and the booth hasn't acted on it yet), the ended
        round's scores are the ones kept from before the reset."""
        g = self.gspro
        if r.start_hole_assumed:
            return
        scores = g.last_hole_scores if g.round_ends != self._log_round_ends else g.hole_scores
        for name in r.roster:
            idx = self._gspro_idx(name)
            if idx is not None:   # the log's holes are 0-based
                r.set_log_scores(name, {hole + 2 - r.start_hole: v for (i, hole), v in scores.items() if i == idx})

    def _apply_log_holes(self, r: Round) -> None:
        """Where the round starts and how long it is, from GSPro's log. The league schedule's start and
        count (or the configured count) are the first guess. A "Skipping hole 1" at the load means it
        doesn't start on 1: assume the back nine (10 through 18) until the first score line names the
        real start hole, which beats any guess. A round starting on 1 keeps its count (GSPro doesn't log
        whether it's 9 or 18 until it completes; a round that plays on past it is extended)."""
        if not self._log_rounds or r.log_complete:
            return
        g = self.gspro
        if g.first_hole is not None and (r.start_hole_assumed or r.start_hole != g.first_hole + 1):
            was = None if r.start_hole_assumed else r.start_hole
            r.start_hole, r.start_hole_assumed = g.first_hole + 1, False   # the log's holes are 0-based
            log.info("Round %s started on hole %s (from GSPro's log%s)", r.id, r.start_hole,
                     f"; the schedule said {was}" if was else "")
            if r.course_card:
                r.apply_course_card(r.course_card)
        elif g.first_hole is None and g.starts_late and r.start_hole == 1:
            r.start_hole, r.start_hole_assumed = 10, True   # the first score line confirms or corrects it
            log.info("Round %s: GSPro skipped hole 1, so assuming a back-nine start", r.id)
            if r.course_card:
                r.apply_course_card(r.course_card)
        if g.starts_late and r.start_hole <= 18 and r.holes != 19 - r.start_hole and r.seq <= 19 - r.start_hole:
            log.info("Round %s: starting on hole %s, counting %s holes (through 18)", r.id, r.start_hole,
                     19 - r.start_hole)
            r.set_holes(19 - r.start_hole)
        if r is self.round:
            self._number_start_hole(r)

    def _number_start_hole(self, r: Round) -> None:
        """The round's first hole when its banner was read without a number. OCR often misses a lone
        "1", and the count was reset at the last round's end, so the opening hole was labelled by par
        and yardage alone; once the start hole is known (setup screen or GSPro's log), number it and
        move anything already recorded there onto the numbered label."""
        wrong = self.state.current_hole
        if self.hole_number is not None or r.ended or r.start_hole_assumed or not r.start_hole \
                or r.seq != 1 or not wrong or re.match(r"Hole \d", wrong):
            return
        self.hole_number, self.hole_number_read = r.start_hole, True
        label = f"Hole {r.start_hole}, {wrong}"
        self.state.current_hole = label
        moved = [x for x in self.state.shots if x.round_id == r.id and x.hole == wrong]
        for x in moved:
            x.hole = label
        if ids := [x.shot_id for x in moved if x.shot_id is not None]:
            self.store.relabel_hole(wrong, label, min(ids))
        log.info("Round %s: the opening banner had no hole number; it's hole %s (%s shots relabelled)",
                 r.id, r.start_hole, len(moved))

    def _extend_round(self, r: Round) -> None:
        """The guessed hole count ran out but GSPro hasn't finished the round: it's longer. Play on
        to the end of the course (GSPro's layout corrects the count when the round completes)."""
        holes = max(r.seq, 19 - r.start_hole if r.start_hole <= 18 else r.holes + 9, r.holes + 1)
        log.info("Round %s: GSPro hasn't finished the round after %s holes; now counting %s", r.id, r.holes, holes)
        r.set_holes(holes)
        self._putted_on_last_hole = False
        if self._closer_task and not self._closer_task.done():
            self._closer_task.cancel()
        self._closer_branches = None

    def _check_round_idle(self) -> None:
        r = self.round
        if not r or r.ended or self._round_end_queued:
            return
        if self._log_rounds:
            # The log says when it's over; the quiet only matters once it has (scorecard never read).
            if r.log_complete and time.monotonic() - self._last_activity > self.cfg.commentary.round_end_idle_seconds:
                self._round_end_queued = True
                self._events.put_nowait(RoundEndEvent(reason="quiet after GSPro completed the round"))
            return
        if r.seq < r.holes or not self._putted_on_last_hole:
            return
        if time.monotonic() - self._last_activity > self.cfg.commentary.round_end_idle_seconds:
            self._round_end_queued = True
            self._events.put_nowait(RoundEndEvent(reason="quiet after the last hole"))

    async def _shot_then_news(self, event: ShotEvent) -> None:
        await self.handle_shot(event)
        if self._asides_on() and (m := self._news_moment) and m["seq"] == self._shot_seq:
            self._bg(self._news_after_shot(m))

    def _on_shot_task_done(self, task: asyncio.Task) -> None:
        self._shot_tasks.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:
            log.error("Shot handler crashed, reaction dropped: %r", exc, exc_info=exc)

    def set_commentary(self, on: bool) -> None:
        """Put the running booth on or off air. Off air it stops talking at once (what's playing is cut
        and nothing more is voiced) and makes no model calls, but every shot and round is still read
        and recorded, so going back on air picks up mid-round with the full picture."""
        if on == self.commentary_on:
            return
        self.commentary_on = on
        log.info("Booth %s", "on air" if on else "off air: recording shots and rounds only")
        if self.speech is None:
            return
        if not on:
            self._cancel_preshot()
            self.speech.interrupt()
            self.speech.muted = True
            return
        self.speech.muted = False
        asyncio.create_task(self.speech.prepare(Line(self.speakers[0], "Warming up.")))
        if self.news is None and self.cfg.news.enabled:
            self.news = GolfNews(self.cfg.news.feeds, self.cfg.news.limit, max_age_days=self.cfg.news.max_age_days)
            asyncio.create_task(self._news_loop(), name="news")
        w = self.cfg.weather
        if self.weather is None and w.enabled and (w.location or (w.latitude is not None and w.longitude is not None)):
            self.weather = Weather(w.latitude, w.longitude, w.place, location=w.location, country=w.country)
            asyncio.create_task(self._weather_loop(), name="weather")
        if self.scores is None and self.cfg.scores.enabled:
            self.scores = LiveScores(self.cfg.scores.tours, self.cfg.scores.teams)
        if self.trivia is None and self.cfg.trivia.enabled:
            self.trivia = CourseTrivia(self.cfg.trivia.pages)

    def _cancel_preshot(self) -> None:
        # Only the plan-building task. The model call it awaits is NOT cancelled here: the run loop
        # calls this right before starting handle_preshot for a new setup, and that setup's own model
        # call is already in flight (started in _emit_setup). Cancelling it would kill the plan we're
        # about to build. A superseded call just finishes unused - a cheap wasted request.
        if self._preshot_task and not self._preshot_task.done():
            self._preshot_task.cancel()

    async def _warm_ocr(self) -> None:
        """Load the OCR model with a throwaway read at startup, so the first hole's reads aren't cold
        (RapidOCR's first inference is slow and otherwise lands on the critical path)."""
        try:
            await self.screen.read_hud_async(["player_box"])
        except Exception:
            log.debug("OCR warmup read failed", exc_info=True)

    async def _weather_loop(self) -> None:
        while True:
            await self.weather.refresh()
            await asyncio.sleep(max(self.cfg.weather.refresh_minutes, 1.0) * 60)

    def _weather_facts(self, rate: float = 1.0) -> list[str]:
        """The weather outside, when it's configured, fresh, and (between holes) this exchange's turn."""
        if self.weather is None or (rate < 1.0 and self.rng.random() >= rate):
            return []
        return self.weather.facts()

    async def _news_loop(self) -> None:
        """Keep the golf headlines fresh: fetch at startup, then re-fetch on a slow cadence."""
        while True:
            try:
                await self.news.refresh()
            except Exception:
                log.exception("Golf news refresh failed")
            await asyncio.sleep(max(self.cfg.news.refresh_hours, 0.1) * 3600)

    async def _prepare_news(self, r) -> None:
        """Write and voice this round's golf-world asides up front, with the round's richest model, so
        each plays instantly and whole when its moment comes (a live one was thin, and its reply could
        miss its voice deadline and leave the opener hanging)."""
        if not self.news.headlines:
            await self.news.refresh()
        n = self.cfg.news.per_nine * max(1, -(-r.holes // 9))
        picks = self.news.pick_many(self.rng, n, self._news_used)
        self._news_used |= {h.title for h in picks}
        # One at a time, each shown the openings already written, so they don't all start "did you see".
        for h in picks:
            try:
                bit = await self._prepare_story(h, [b.lines[0].text for b in self._news_bits])
            except Exception as e:
                log.warning("News aside on %r failed: %r", h.title, e)
                continue
            if bit is not None and self.round is r:
                self._news_bits.append(bit)
        log.info("News: %d of %d asides ready for this round: %s", len(self._news_bits), len(picks),
                 "; ".join(f"{b.title!r} ({b.trigger or 'any quiet moment'})" for b in self._news_bits))

    async def _write_aside(self, label: str, user: str, facts_text: str, schema: dict) -> tuple[list[Line], dict]:
        """A two-line exchange from the closer's model, checked against its facts; ([], {}) if none."""
        prompt, data = user, {}
        for _ in range(2):
            raw = await self._complete("news", self.closer_llm, prompt, [], schema=schema, system=self.system)
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = {}
            cap = self.cfg.commentary.max_words_per_line
            lines = parse_lines(raw, self.speakers, cap, self._persona_names)
            good, problems = self._grounded(lines, facts_text, mode="news")
            # An over-long line is cut back to its last full sentence, which can leave just a teaser
            # ("Interesting bit of news this morning, Duke.") with the story itself gone.
            items = data.get("lines") if isinstance(data, dict) else None
            if long := [i + 1 for i, item in enumerate(items if isinstance(items, list) else [])
                        if isinstance(item, dict) and len(str(item.get("text", "")).split()) > cap]:
                problems = problems + [f"line {', '.join(map(str, long))} ran over {cap} words: say each line, "
                                       f"story included, in at most {cap} words"]
            if len(lines) != 2 or len(good) != 2 or good[0].speaker == good[1].speaker:
                problems = problems + ["write exactly two lines, one from each commentator"]
            elif len(good[1].text.split()) < 6:
                problems = problems + ["the reply must give a take of its own, in at least one full sentence"]
            if not problems:
                return good, data
            self.stats["retries"] += 1
            prompt = retry_prompt(user, problems)
        log.info("%s dropped: no usable exchange (%s)", label, "; ".join(problems))
        return [], {}

    def _aside_openings(self) -> list[str]:
        return [b.lines[0].text for b in self._news_bits]

    async def _prepare_story(self, h, openings: list[str] = ()) -> PreparedNews | None:
        user, facts_text = news_prompt(topic=h.as_fact(), openings=openings)
        good, data = await self._write_aside(f"News aside on {h.title!r}", user, facts_text,
                                             news_schema(self.speakers))
        if not good:
            return None
        trigger = data.get("trigger") if data.get("trigger") in NEWS_TRIGGERS else None
        tie = None
        if trigger and (text := " ".join(str(data.get("tie_in") or "").split())):
            ok, _ = self._grounded([Line(speaker=good[0].speaker, text=text)], facts_text, mode="news")
            tie = ok[0] if ok else None
        kept, audio = await self._voice_ahead(good)
        if len(kept) != 2:
            log.info("News aside on %r dropped: it couldn't be voiced", h.title)
            return None
        tie_audio = None
        if tie is not None:
            voiced, clips = await self._voice_ahead([tie])
            tie_audio = clips[0] if voiced else None
            tie = tie if voiced else None
        if tie is None:
            trigger = None   # no bridge to play, so it airs on any quiet moment instead
        return PreparedNews(h.title, trigger, kept, audio, tie, tie_audio)

    # ---- course trivia and live scores --------------------------------------------------

    def _asides_on(self) -> bool:
        return self.news is not None or self.scores is not None or self.trivia is not None

    def _aside_task(self, coro, name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self._aside_tasks.add(task)
        task.add_done_callback(lambda t: (self._aside_tasks.discard(t), t.cancelled() or t.exception()))

    def _start_round_asides(self, r) -> None:
        """Cancel the last round's trivia and score work, then start this round's."""
        for task in list(self._aside_tasks):
            task.cancel()
        self._score_holes = []
        if self.trivia is not None:
            self._aside_task(self._prepare_trivia(r), "trivia-prep")
        if self.scores is not None:
            self._aside_task(self._plan_scores(r), "scores-plan")

    async def _prepare_trivia(self, r) -> None:
        """Write and voice this round's course-trivia asides up front, from the real course's article,
        steering clear of what earlier rounds here already told."""
        course = r.course or self.current_course
        found = await self.trivia.article(course)
        if not found or self.round is not r:
            return
        title, article = found
        told = self.store.recent_asides("trivia", title)
        made = 0
        for _ in range(max(0, self.cfg.trivia.per_round)):
            user, facts_text = trivia_prompt(course=course, title=title, article=article, told=told,
                                             openings=self._aside_openings())
            try:
                good, data = await self._write_aside(f"Trivia on {title!r}", user, facts_text,
                                                     trivia_schema(self.speakers))
            except Exception as e:
                log.warning("Trivia on %r failed: %r", title, e)
                continue
            if not good:
                continue
            told = told + [" ".join(l.text for l in good)]
            kept, audio = await self._voice_ahead(good)
            if len(kept) != 2 or self.round is not r:
                continue
            try:
                hole = int(data.get("hole") or 0)
            except (TypeError, ValueError):
                hole = 0
            self._news_bits.append(PreparedNews(title, None, kept, audio, kind="trivia",
                                                hole=hole if 1 <= hole <= 18 else None, topic=title))
            made += 1
        log.info("Trivia: %d asides ready on %r: %s", made, title,
                 "; ".join(f"hole {b.hole}" if b.hole else "any quiet moment"
                           for b in self._news_bits if b.kind == "trivia"))

    async def _plan_scores(self, r) -> None:
        """Pick the holes a live score is written for, if anything's on tonight. Only the plan is made
        now: each score is fetched as its hole starts, so it isn't stale by the time it airs."""
        items = await self.scores.fetch()
        if not items or self.round is not r:
            log.info("Scores: nothing live or on tonight; no score asides this round")
            return
        n = max(0, self.cfg.scores.per_nine) * max(1, -(-r.holes // 9))
        first, last = 2, r.holes - 2          # the asides' window: not the first hole or the last two
        if n == 0 or last < first:
            return
        # One hole from each equal stretch of the window, so two check-ins aren't back to back.
        span = (last - first + 1) / n
        holes = []
        for i in range(n):
            lo, hi = int(first + i * span), int(first + (i + 1) * span) - 1
            if i < n - 1:
                hi -= 2       # room before the next stretch's hole
            hole = self.rng.randint(lo, max(lo, hi))
            if not holes or hole - holes[-1] >= 3:
                holes.append(hole)
        self._score_holes = holes
        log.info("Scores: %d on tonight (%s); a check-in on round hole(s) %s", len(items),
                 "; ".join(i.key for i in items), holes)

    def _score_hole_started(self, r) -> None:
        due = [h for h in self._score_holes if h <= r.seq]
        if not due or self.scores is None:
            return
        self._score_holes = [h for h in self._score_holes if h > r.seq]
        # An aside aired too recently for this one to play on this hole: move it to the first hole it
        # can, rather than write up a score that will go stale waiting.
        if self._news_aired and r.seq - self._news_aired[-1] < 3:
            later = self._news_aired[-1] + 3
            if later <= r.holes - 2 and later not in self._score_holes:
                self._score_holes = sorted(self._score_holes + [later])
                log.info("Scores: check-in moved to round hole %s (an aside just aired)", later)
            return
        self._aside_task(self._prepare_score(r), "scores-prep")

    async def _prepare_score(self, r) -> None:
        """Fetch the scores now, as the hole starts, and write and voice a check-in on the best one."""
        items = await self.scores.fetch()
        now = time.monotonic()
        pending = {b.topic for b in self._news_bits if b.kind == "score" and (b.expires or now + 1) > now}
        # Nothing already waiting to air, and nothing unchanged since it was last written up.
        fresh = [i for i in items if i.key not in pending and f"{i.key}@{i.version}" not in self._score_written]
        # Something the booth hasn't touched tonight first; then an update on one that's moved.
        fresh.sort(key=lambda i: (i.key in self._score_said, i.rank))
        if not fresh or self.round is not r:
            log.info("Scores: nothing new to check in on at hole %s", r.seq)
            return
        item = fresh[0]
        earlier = self._score_said.get(item.key, [])
        user, facts_text = scores_prompt(item=item.fact, earlier=earlier, openings=self._aside_openings())
        good, _ = await self._write_aside(f"Score check-in on {item.key}", user, facts_text,
                                          output_schema(self.speakers))
        if not good:
            return
        kept, audio = await self._voice_ahead(good)
        if len(kept) != 2 or self.round is not r:
            return
        self._score_written.add(f"{item.key}@{item.version}")
        self._news_bits.append(PreparedNews(item.key, None, kept, audio, kind="score", topic=item.key,
                                            expires=time.monotonic() + self.cfg.scores.max_age_minutes * 60))
        log.info("Scores: check-in on %s ready for hole %s: %s", item.key, r.seq, item.fact)

    def _news_pick(self, moment: dict) -> tuple[PreparedNews, bool] | None:
        """The aside to play after this shot, if any, and whether it airs on its trigger. News, trivia
        and live scores are one bucket: on an ordinary shot, one picked at random from those ready. At
        most one every three holes, never on the first hole or the last two. A news aside held for a
        kind of shot plays on that shot, and waits for it until the round is well along. A trivia
        story about one hole is only ready on that hole, wherever it falls (two holes after the last
        aside at least)."""
        r = self.round
        if not self._news_bits or r is None or r.ended or not r.seq:
            return None
        now = time.monotonic()
        self._news_bits = [b for b in self._news_bits if b.expires is None or b.expires > now]
        gap = r.seq - self._news_aired[-1] if self._news_aired else r.seq + 99
        open_hole = r.seq >= 2 and r.holes - r.seq >= 2 and gap >= 3
        tags = set(moment["tags"])
        if open_hole:
            for bit in self._news_bits:
                if bit.trigger and tags & NEWS_TRIGGER_TAGS[bit.trigger]:
                    return bit, True
        if not moment["routine"]:
            return None
        late = r.seq >= -(-r.holes * 3 // 5)
        pool = [b for b in self._news_bits
                if (b.hole is not None and b.hole == self.hole_number and gap >= 2)
                or (b.hole is None and open_hole and (b.trigger is None or late))]
        if not pool or self.rng.random() > self.cfg.news.banter_rate:
            return None
        return self.rng.choice(pool), False

    async def _news_after_shot(self, moment: dict) -> bool:
        """Play a prepared aside once this shot's call is done, if one suits the moment. Returns
        whether it played."""
        if (pick := self._news_pick(moment)) is None:
            return False
        bit, on_trigger = pick
        r, seq, hole = self.round, self.round.seq, self.hole_number
        deadline = time.monotonic() + 8.0
        while not self.speech.is_idle() and time.monotonic() < deadline and self._shot_seq == moment["seq"]:
            await asyncio.sleep(0.2)
        await asyncio.sleep(0.8)   # a beat after the call, so the aside doesn't run straight on from it
        if not self.speech.is_idle() or self._shot_seq != moment["seq"] or bit not in self._news_bits:
            return False
        # The hole can end in the wait (a conceded putt, the round's last hole) without another shot.
        if not self.commentary_on or self.speech.muted:
            return False    # off air: keep it, since nobody would hear it
        if self.round is not r or r.ended or r.seq != seq or self.hole_number != hole \
                or (bit.expires is not None and bit.expires <= time.monotonic()):
            return False
        first = (bit.tie, bit.tie_audio) if on_trigger else (bit.lines[0], bit.audio[0])
        items = [first, (bit.lines[1], bit.audio[1])]
        self._news_bits.remove(bit)
        self._news_aired.append(self.round.seq)
        lines = [line for line, _ in items]
        self.state.record_lines(lines)
        self._air_exchange(items)
        self._write_transcript(lines, "banter")
        if bit.kind == "trivia":
            self.store.record_aside("trivia", bit.topic, " ".join(l.text for l in lines))
            log.info("Trivia aside%s: %r", f" on hole {bit.hole}" if bit.hole else "", bit.title)
        elif bit.kind == "score":
            self._score_said.setdefault(bit.topic, []).append(" ".join(l.text for l in lines))
            self.store.record_aside("score", bit.topic, " ".join(l.text for l in lines))
            log.info("Score check-in: %r", bit.title)
        else:
            log.info("News aside%s: %r", f" on a {bit.trigger.replace('_', ' ')}" if on_trigger else "", bit.title)
        return True

    async def _drain_speech(self, timeout: float | None = None) -> None:
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.speech.is_idle():
            if deadline is not None and time.monotonic() >= deadline:
                return
            await asyncio.sleep(0.2)

    async def _drain_shot_tasks(self, timeout: float) -> None:
        """Wait (bounded) for in-flight shot handlers to finish, so a hole's last reaction is queued
        before the wrap is, instead of the two talking over each other."""
        pending = {t for t in self._shot_tasks if not t.done() and t is not asyncio.current_task()}
        if pending:
            await asyncio.wait(pending, timeout=timeout)

    @staticmethod
    def _raw_yards(shot) -> float:
        return (shot.carry or 0) * (1 if (shot.units or "yards").lower().startswith("y") else 1.0936)

    def _player(self, *texts: str | None) -> str | None:
        for text in texts:
            if name := self.league.match_player(text):
                return name
        # Not on the league roster (no league file, or a guest): the round's own GSPro names.
        r = self.round
        if r and not r.ended and texts and texts[0]:
            raw = re.split(r"\s+(?:E|[+-]\d+|Shot)\b", texts[0].strip(), maxsplit=1)[0].strip().casefold()
            if name := next((n for n in r.roster if n and n.casefold() == raw), None):
                return name
        return next((t for t in texts[1:] if t), None)

    def _anonymous(self, player: str | None) -> bool:
        """Partners sharing one ball (alternate shot): GSPro's name on it isn't who hit it."""
        r = self.round
        return bool(r and not r.ended and player and player in r.anonymous_players())

    def _display(self, player: str | None) -> str | None:
        """How the booth refers to whoever hit: the team, when the hitter can't be known."""
        if self._anonymous(player):
            return self.round.side_for(player).label
        return player

    def _who(self, player: str | None) -> tuple[str | None, str | None]:
        """(player, team) for a prompt header."""
        if self._anonymous(player):
            return self.round.describe_side(self.round.side_for(player)), None
        return player, self.league.player_team.get(player or "")

    def _delivery_turn(self, notable: bool) -> bool:
        """Whether this reaction gets club-delivery facts: never two in a row."""
        return not self._delivery_last and self.rng.random() < (0.35 if notable else 0.08)

    NOTE_USES_PER_ROUND = 2

    def _spent_notes(self) -> list[str]:
        return [p for p, n in self._note_uses.items() if n >= self.NOTE_USES_PER_ROUND]

    def _format_facts(self) -> list[str]:
        if not self.round or self.round.ended:
            return []
        return self.round.format_facts() + self._pick_facts() + self._roster_facts()

    def _roster_facts(self) -> list[str]:
        """Who isn't here: a partner of someone playing may come up in passing, but must never be
        described hitting (a solo practice round had "a handy look for <absent partner> now"). And any player
        note already used enough this round, so the model doesn't keep reaching for it."""
        r = self.round
        facts: list[str] = []
        if r and r.format not in TEAM_BALL:
            playing = {p for p in r.roster.values() if p}
            teams = {self.league.player_team.get(p) for p in playing} - {None}
            absent = [p for t in teams for p in self.league.team_players.get(t, []) if p not in playing]
            if absent:
                facts.append(f"Not playing this round: {', '.join(absent)}. A passing mention is fine, but "
                             f"never describe them hitting, putting or having a shot here.")
        if spent := self._spent_notes():
            facts.append(f"Already used enough today, leave alone: the league notes about {', '.join(spent)}.")
        return facts

    _HOLE_TERMS = {-3: "albatross", -2: "eagle", -1: "birdie", 0: "par", 1: "bogey",
                   2: "double bogey", 3: "triple bogey"}

    def _score_fact(self, side, strokes: int, par: int, picked: bool = False) -> str:
        """One line stating a side's hole score with the numbers, so the wrap can say 'bogey'/'one
        over' without the grounding check rejecting it. A pick-up says so: the score is the cap the
        game gave, not strokes played."""
        diff = strokes - par
        term = self._HOLE_TERMS.get(diff)
        margin = "even" if diff == 0 else f"{abs(diff)} {'over' if diff > 0 else 'under'}"
        return (f"Score on the finished hole: {side.label} made {strokes} on the par-{par} hole, "
                f"{margin} par" + (f" (a {term})" if term else "")
                + (": they picked up, and the game scored the hole at its maximum" if picked else "") + ".")

    def _hole_score_facts(self, finished_hole: str | None) -> list[str]:
        """How each side scored on the hole that just finished, from the scorecard. This is the
        reliable par/bogey signal even when GSPro's result banner never shows. The par comes from the
        hole's own HUD label (e.g. 'par 4'), not r.par, which is seeded from a course card that can
        mismatch the holes actually being played (that mismatch called a par a bogey). Skipped when
        the scorecard hasn't caught up to the finished hole yet (better no claim than the wrong one)."""
        r = self.round
        m = re.search(r"par (\d)", finished_hole or "", re.IGNORECASE)
        if not r or r.ended or r.seq <= 1 or not m:
            return []
        par = int(m.group(1))
        self._apply_log_corrections()
        return [self._score_fact(side, s, par, picked=r.was_picked_up([*side.gspro, *side.players], r.seq - 1))
                for side in r.sides if (s := r.card_score(side, r.seq - 1)) is not None]

    def _prevoicing(self) -> bool:
        return getattr(self.speech, "prevoicing", True)

    async def _voice_ahead(self, lines: list[Line]) -> tuple[list[Line], list]:
        """Voice an exchange that may never air (a wrap/closer branch, an aside) now, or with
        tts.prevoice off on a cloud voice, keep just the text for _air_exchange to voice as it plays."""
        if self._prevoicing():
            return await self.speech.prepare_exchange(lines)
        return list(lines), [None] * len(lines)

    def _air_exchange(self, items: list[tuple[Line, object]]) -> None:
        """Play an exchange from _voice_ahead: its clips if they were voiced ahead, else voiced now
        (streamed, line by line) the same way a live line is."""
        if self._prevoicing():
            self.speech.say_prepared_sequence(items)
        else:
            self.speech.say([line for line, _ in items], interrupt=True)

    def _kick_wrap_branches(self, side_key: str, scores: list[int], other_facts: list[str]) -> None:
        """(Re)start the background pre-build of the hole wrap, branched by the one side still to
        finish (`side_key`) over its possible `scores`, with the sides already in fixed in
        `other_facts`."""
        if self._wrap_task and not self._wrap_task.done():
            self._wrap_task.cancel()
        self._wrap_task = asyncio.create_task(self._prepare_wrap_branches(side_key, scores, other_facts))
        self._wrap_task.add_done_callback(lambda t: t.cancelled() or t.exception())

    async def _prepare_wrap_branches(self, side_key: str, scores: list[int], other_facts: list[str]) -> None:
        """Pre-generate and pre-voice the hole wrap for each score the deciding side might finish on,
        so the recap plays the instant the scorecard posts the result, no model call, retry or banner.
        The deciding side is the last one still to hole out: solo that's the only player (fired when
        the ball reaches the green); in a group it's whoever is left once the second-to-last holes
        out, with the others' scores already fixed in `other_facts`."""
        r = self.round
        par = self.current_par
        side = next((s for s in (r.sides if r else []) if s.key == side_key), None)
        if not r or r.ended or par is None or side is None or self.segment_llm is None or not scores:
            return
        finished = self.state.current_hole
        recap = self.state.hole_recap(finished, name=lambda p: self._display(p) or p)
        sand_here = [x.player for x in self.state.shots if x.round_id == r.id and x.hole_seq == r.seq
                     and not x.mulligan and (x.hud.lie or "").lower() in ("sand", "bunker")]
        theme = [f for p in dict.fromkeys(sand_here) for f in self._bunker_facts(p)]
        roster = self._roster_facts()

        async def gen(strokes):
            # "round" mode: a recap spans several lies, so skip the single-finishing-lie check that
            # would otherwise reject "found the green, then the rough".
            result = other_facts + [self._score_fact(side, strokes, par)] + theme + roster
            user, facts = wrap_recap_prompt(finished_hole=finished, result=result,
                                            recap=recap, recent_lines=self.state.recent_lines)
            return self._grounded(await self._generate("segment", self.segment_llm, user, []), facts, "round", False)[0]

        # Generate the branches' text in parallel (network-bound); the final-score branch is then
        # ready sooner, in time to fire on the scorecard rather than only at the hole change.
        drafts = await asyncio.gather(*(gen(s) for s in scores), return_exceptions=True)
        written: dict[int, list] = {}
        for strokes, lines in zip(scores, drafts):
            if isinstance(lines, Exception):
                log.debug("Wrap branch for %s strokes failed: %s", strokes, lines)
            elif lines:
                written[strokes] = lines
        # On a network voice, voice every branch (and every line in it) at once; a local CPU voice goes
        # one line at a time. A branch whose opening line couldn't be voiced is left out, so the
        # on-demand fallback covers it. With tts.prevoice off (cloud), only the text is kept and the
        # branch that airs is voiced as it plays.
        if getattr(self.speech.tts, "concurrent", False):
            voiced = await asyncio.gather(*(self._voice_ahead(l) for l in written.values()))
        else:
            voiced = [await self._voice_ahead(l) for l in written.values()]
        branches: dict[int, tuple] = {k: v for k, v in zip(written, voiced) if v[0]}
        if self.state.current_hole == finished:  # a new hole hasn't started while we were working
            self._wrap_branches = {"hole": finished, "side": side_key, "by_score": branches} if branches else None
            if branches:
                log.info("Wrap: branches ready for %s (%s) on %s", sorted(branches), side.label, finished)

    def _play_prepared_wrap(self, finished_hole, finished_seq: int) -> bool:
        """Play the pre-made wrap branch for the score the deciding side actually came in on (from the
        scorecard at `finished_seq`). Returns False (fall back to the on-demand wrap) when there's no
        branch for that score, or the scorecard hasn't posted it yet."""
        wb = self._wrap_branches
        r = self.round
        side = next((s for s in (r.sides if r else []) if s.key == wb["side"]), None) if wb else None
        if not wb or wb["hole"] != finished_hole or side is None:
            return False
        strokes = r.card_score(side, finished_seq)
        branch = wb["by_score"].get(strokes) if strokes is not None else None
        if not branch:
            return False
        self._wrap_branches = None
        self._wrapped_hole = finished_hole
        lines, audio = branch
        self.state.record_lines(lines)
        self._air_exchange(list(zip(lines, audio)))
        self._write_transcript(lines, "segment")
        log.info("Wrap: pre-made branch played for %s strokes, no model call", strokes)
        return True

    def _kick_closer_branches(self, side_key: str, scores: list[int], fixed: dict[str, int] | None = None) -> None:
        """(Re)start the background pre-build of the round closer on the last hole, branched by the
        deciding side's possible final `scores`, so the sign-off is ready to play the instant the
        round ends with no model call (lets the closer use a rich, slower model with no felt latency).
        In a group the deciding side is the last one still on the last hole; `fixed` holds the other
        sides' last-hole scores the branches were written with, checked again before one plays."""
        # Only on cloud TTS: pre-voicing several branches is a network call there, but on local Kokoro
        # it's CPU-bound and would starve the OCR poller during the last putts. On Kokoro the closer
        # just runs on demand at round end (same as before).
        if not getattr(self.speech.tts, "concurrent", False):
            return
        if self._closer_branches:
            return  # already built for this round; don't rebuild on every late putt
        if self._closer_task and not self._closer_task.done():
            self._closer_task.cancel()
        self._closer_task = asyncio.create_task(self._prepare_closer_branches(side_key, scores, fixed or {}))
        self._closer_task.add_done_callback(lambda t: t.cancelled() or t.exception())

    def _kick_group_closer(self, r: Round) -> None:
        """A group on the last hole: once every side but one has finished it, pre-build the closer
        branched on that last side's score (par -1 to +2), with the others' scores fixed."""
        if len(r.sides) < 2 or r.ended or r.seq < r.holes or self._closer_branches \
                or (self._closer_task and not self._closer_task.done()):
            return
        if any(r.card_score(s, q) is None for s in r.sides for q in range(1, r.holes)):
            return   # an earlier hole isn't scored yet: the closer's totals would be wrong
        remaining = [s for s in r.sides if r.card_score(s, r.holes) is None]
        par = r.par.get(r.holes) or self.current_par
        if len(remaining) != 1 or not par:
            return
        fixed = {s.key: r.card_score(s, r.holes) for s in r.sides if s is not remaining[0]}
        log.info("Closer: pre-building for %s's finish on the last hole (others done: %s)",
                 remaining[0].label, fixed)
        self._kick_closer_branches(remaining[0].key, [p for p in (par - 1, par, par + 1, par + 2) if p > 0], fixed)

    async def _prepare_closer_branches(self, side_key: str, scores: list[int], fixed: dict[str, int]) -> None:
        r = self.round
        if not r or r.ended or not self.cfg.commentary.closer or not scores:
            return
        side = next((s for s in r.sides if s.key == side_key), None)
        if side is None:
            return
        season = {s.key: self.store.season_rounds(s.key) for s in r.sides}

        async def gen(strokes):
            facts = (r.closing_facts(self.state.shots, season, final_overrides={side_key: strokes},
                                     week_ctp=self._closer_week_ctp(r))
                     + self._league_facts(r, closing=True) + self._weather_facts())
            user, facts_text = closer_prompt(facts=facts, recent_lines=self.state.recent_lines)
            # "round" mode grounding: the closer spans the whole round, so skip the single-lie check.
            system = self._system_league if r.is_league else self._system_practice
            return self._grounded(await self._generate("closer", self.closer_llm, user, [], system=system),
                                  facts_text, "round", False)[0]

        drafts = await asyncio.gather(*(gen(s) for s in scores), return_exceptions=True)
        written: dict[int, list] = {}
        for strokes, lines in zip(scores, drafts):
            if isinstance(lines, Exception):
                log.debug("Closer branch for %s strokes failed: %s", strokes, lines)
            elif lines:
                written[strokes] = lines
        # On a network voice, voice every branch (and every line in it) at once; a local CPU voice goes
        # one line at a time. A branch whose opening line couldn't be voiced is left out, so the
        # on-demand fallback covers it. With tts.prevoice off (cloud), only the text is kept and the
        # branch that airs is voiced as it plays.
        if getattr(self.speech.tts, "concurrent", False):
            voiced = await asyncio.gather(*(self._voice_ahead(l) for l in written.values()))
        else:
            voiced = [await self._voice_ahead(l) for l in written.values()]
        branches: dict[int, tuple] = {k: v for k, v in zip(written, voiced) if v[0]}
        if not r.ended and branches:
            self._closer_branches = {"side": side_key, "by_score": branches, "fixed": fixed}
            log.info("Closer: branches ready for %s (%s)", sorted(branches), side.label)

    def _play_prepared_closer(self, final_strokes: int | None, side_key: str) -> bool:
        """Play the pre-made closer branch for the score the deciding side actually finished on.
        Returns False (fall back to the on-demand closer) when there's no branch for that score, so a
        mismatch or a never-built set can never air a wrong sign-off."""
        cb = self._closer_branches
        self._closer_branches = None
        if not cb or cb.get("side") != side_key or final_strokes is None:
            return False
        branch = cb["by_score"].get(final_strokes)
        if not branch:
            return False
        lines, audio = branch
        self.state.record_lines(lines)
        self._air_exchange(list(zip(lines, audio)))
        self._write_transcript(lines, "closer")
        log.info("Closer: pre-made branch played for %s strokes, no model call", final_strokes)
        return True

    def _banned_names(self) -> list[tuple[list[str], list[str]]]:
        # Naming an individual player is allowed in every format (a scramble can credit a person, a
        # team format can talk about the team; both read fine). Forcing team-only wording rejected a
        # line on nearly every call and triggered a full model retry, which was the biggest latency
        # hog, and it was plain wrong when one player is on the ball solo. So nothing is banned.
        return []

    def _ctp_facts(self, shot_number: int | None, hud: Hud | None = None, player: str | None = None) -> list[str]:
        """Closest-to-the-pin holes: flagged on the tee, and the tee shot's finish recorded from the
        same player's next setup (the player box then shows how far the ball is from the hole)."""
        r = self.round
        if not r or r.ended or r.hole_number(r.seq) not in r.ctp_holes:
            return []
        prize = self.league.data.get("ctp_prize")
        hole = r.hole_number(r.seq)
        if shot_number == 1:
            facts = ["This is one of the week's closest-to-the-pin holes: the closest tee shot across the whole "
                     "league this week wins" + (f" ${prize:g}." if prize else " the prize.")]
            if (lead := self._ctp_leader(r, hole)):
                facts.append(f"The one to beat this week: {lead[0]}, {lead[1]} from the hole.")
                facts += self._ctp_mark_source(lead)
            facts += self._ctp_group_facts(r, exclude=self._display(player) if player else None)
            return facts
        if shot_number == 2 and hud and hud.lie == "green" and hud.to_pin and player:
            who = self._display(player)
            lead = self._ctp_leader(r, hole, exclude=who)
            group = self._ctp_group_facts(r, exclude=who)
            r.ctp_results.setdefault(r.seq, {})[who] = hud.to_pin
            facts = [f"{who}'s tee shot on this closest-to-the-pin hole finished on the green, {hud.to_pin} from the hole."]
            mine = to_yards(hud.to_pin)
            if lead and mine is not None:
                facts.append(f"That's the new closest this week, inside {lead[0]}'s {lead[1]}." if mine < lead[2]
                             else f"{lead[0]}'s {lead[1]} is still the closest this week.")
                facts += self._ctp_mark_source(lead)
            elif not lead and r.format in ("best_ball", "aggregate"):
                facts.append("That's the closest tee shot on this hole this week so far.")
            return facts + group
        return []

    @staticmethod
    def _ctp_mark_source(lead: tuple) -> list[str]:
        # Without this the model guesses when the mark was set ("Dan's had the tape out all night").
        return [f"{lead[0]}'s mark came from an earlier round this week, not this one."] if lead[3] else []

    def _ctp_group_facts(self, r: Round, exclude: str | None = None) -> list[str]:
        """This round's tee shots already on the green on this hole, so a line about a playing partner's
        shot has something true to say (and isn't made up)."""
        return [f"Earlier on this hole in this round, {who}'s tee shot finished on the green, {dist} from the hole."
                for who, dist in (r.ctp_results.get(r.seq) or {}).items() if who != exclude]

    def _ctp_leader(self, r: Round, hole: int | None,
                    exclude: str | None = None) -> tuple[str, str, float, bool] | None:
        """The closest tee shot on the green on this hole this week (who, distance text, yards, whether it
        came from an earlier round): earlier rounds from the season store (everything recorded before
        this round started), and this round's own greens as the HUD read them. Individual-ball weeks only."""
        if hole is None or r.format not in ("best_ball", "aggregate"):
            return None
        options = []
        if (db := self._week_ctp(r, before_id=r.shot_floor).get(hole)):
            options.append((db[0], _distance_text(db[1]), db[1], True))
        for who, dist in (r.ctp_results.get(r.seq) or {}).items():
            if who != exclude and (y := to_yards(dist)) is not None:
                options.append((who, dist, y, False))
        return min(options, key=lambda o: o[2]) if options else None

    # ---- pre-shot -------------------------------------------------------------------------

    def _replay_facts(self, player: str | None, rec: ShotRecord) -> list[str]:
        """For a re-hit whose mulligan only showed up in GSPro's log (a take-back puts the HUD back
        exactly as it was, so no new setup is seen and the setup-time call can't fire): tell the
        reaction this swing replaced the last one."""
        if not player:
            return []
        mine = [x for x in self.state.shots if x.player == player]
        if len(mine) < 2 or mine[-1] is not rec:
            return []
        prev = mine[-2]
        if not prev.mulligan or id(prev) in self._mulligans_called or prev.hole != rec.hole \
                or prev.round_id != rec.round_id:
            return []
        self._mulligans_called.add(id(prev))
        who = self._display(player)
        club = club_name(prev.shot.club)
        log.info("Mulligan: %s replayed stroke %s (from GSPro's log)", who, rec.shot_number)
        return [f"This swing was a mulligan: {who} took back the previous one"
                f"{f' (the {club})' if club else ''} and replayed the stroke from the same spot. Mention "
                f"the mulligan, and judge this result as the one that counts."]

    def _bunker_facts(self, player: str | None) -> list[str]:
        """A recurring theme: how often this player has finished in a bunker this round (taken-back
        swings excluded), once it has happened at least twice. Fed only where a bunker is on topic
        (a shot into or out of the sand, the wrap of a hole that had one), so it airs now and then."""
        r = self.round
        if not r or r.ended or not player or self._anonymous(player):
            return []
        seqs = [x.hole_seq for x in self.state.shots
                if x.player == player and x.round_id == r.id and not x.mulligan
                and (x.hud.lie or "").lower() in ("sand", "bunker")]
        if len(seqs) < 2:
            return []
        holes = sorted({h for q in seqs if q and (h := r.hole_number(q))})
        where = f" (holes {', '.join(map(str, holes))})" if len(holes) > 1 else ""
        theme = " a recurring theme of the round" if len(seqs) >= 3 else ""
        return [f"{self._display(player)} has found a bunker {len(seqs)} times this round{where}:{theme or ' worth a mention'}"
                f"; bring it up now and then, not on every shot."]

    def _mulligan_facts(self, player: str | None, hud: Hud) -> list[str]:
        """A fact when the player is replaying their last swing (a mulligan). GSPro puts the ball back
        and the HUD back to the same stroke number, so a setup for the same stroke, from the same spot,
        as this player's last swing on this hole means that swing was taken back. Read off the HUD at
        the re-hit's setup because GSPro's log often says nothing about a mulligan until the re-hit has
        been played. Marks the HUD so the setup line is always voiced."""
        if not player or not hud.shot_number:
            return []
        r = self.round if self.round and not self.round.ended else None
        last = next((x for x in reversed(self.state.shots) if x.player == player), None)
        if last is None or id(last) in self._mulligans_called or last.shot_number != hud.shot_number:
            return []
        same_hole = (last.round_id == r.id and last.hole_seq == r.seq) if r else last.hole == self.state.current_hole
        # Where the taken-back swing was played from, not where it finished.
        here, there = to_yards(hud.to_pin), to_yards(last.to_pin_before or last.hud.to_pin)
        if not same_hole or here is None or there is None or abs(here - there) > max(1.0 if there < 30 else 3.0, there * 0.05):
            return []
        self._mulligans_called.add(id(last))
        hud._mulligan = True
        who = self._display(player)
        club = club_name(last.shot.club)
        lie = (last.hud.lie or "").lower()
        went = f" (the {club}" if club else " (the last one"
        went += f" that finished in the {lie})" if lie and lie not in ("green", "fairway") else ")"
        log.info("Mulligan: %s replaying stroke %s from %s", who, hud.shot_number, hud.to_pin)
        return [f"{who} is taking a mulligan: the last swing{went} was taken back, and {who} is replaying "
                f"stroke {hud.shot_number} from the same spot. Mention the mulligan."]

    def _penalty_facts(self, player: str | None, shot_number: int | None) -> list[str]:
        """A fact when a penalty stroke was taken, so the booth counts it instead of swings.

        GSPro folds a penalty into the stroke count without playing a shot for it, so the shot
        number jumps (2 -> 4 after a water ball and drop). Without this the model counts physical
        swings and calls the fourth shot a third. The GSPro log counts penalties exactly; when it's
        on we use it, else we infer the penalty from the HUD shot number jumping.
        """
        idx = self._gspro_idx(player)
        if idx is not None and self.gspro.current_hole is not None:
            return self._penalty_facts_from_truth(idx)
        if not shot_number:
            return []
        key = player or "unknown"
        last = self._last_hud_shot.get(key)
        self._last_hud_shot[key] = shot_number
        if last is None or shot_number <= last + 1:
            return []
        penalties = shot_number - last - 1
        return [f"A penalty stroke was taken (the stroke count jumped from {last} to {shot_number}: "
                f"{penalties} penalty stroke{'s' if penalties > 1 else ''} for a ball in the water or out "
                f"of bounds, then a drop). Counting the penalty, this is stroke {shot_number} on the hole: "
                f"call it the {ordinal(shot_number)}, not an earlier number."]

    @property
    def system(self) -> str:
        """The system prompt for the current round type: the practice variant (no stakes on the line)
        unless a real league round is in progress. Defaults to practice when no round is live, so the
        booth never invents league stakes before one is confirmed."""
        r = self.round
        return self._system_league if (r is not None and not r.ended and r.is_league) else self._system_practice

    def _round_ctx(self) -> str:
        """The current round's stat context: 'league' under a live league round, else 'practice'. Used
        to slice a player's season stats to the kind of round they're in (the 'under pressure' view).
        Honours `ended` so post-round facts don't read the league slice while shots write practice."""
        r = self.round
        return "league" if (r is not None and not r.ended and r.is_league) else "practice"

    @staticmethod
    def _ctx_phrase(ctx: str | None) -> str:
        """How a stat's context reads: the current round's kind, or 'this season' for the lifetime slice."""
        return {"league": "in league play", "practice": "outside league play"}.get(ctx, "this season")

    def _ctx_stat(self, query, thin) -> tuple[dict, str | None]:
        """A stat sliced to the current round's context (league/practice), falling back to the lifetime
        slice when that slice is thin. `query(ctx)` runs the store query for a context; `thin(row)` says
        the row is too small to trust. Returns (row, used_ctx), used_ctx None meaning the lifetime slice."""
        ctx = self._round_ctx()
        row = query(ctx)
        if thin(row):
            return query(None), None
        return row, ctx

    def _profile_facts(self, player: str | None, is_putt: bool, hud: Hud) -> list[str]:
        """Season profile colour for this player as grounded facts: how close they get from this
        range, how they putt from this distance, and (on the tee) their go-to club and typical miss.
        Each has a minimum sample so a thin database doesn't produce confident nonsense. The marquee
        rates (putting, driving accuracy) prefer the current context (league/practice) and fall back to
        the lifetime slice when that context is thin; a league-vs-practice contrast rides along too."""
        if not player or player == "unknown":
            return []
        who = self._display(player)
        ctx = self._round_ctx()
        facts: list[str] = []
        dist = to_yards(hud.to_pin)
        if is_putt and dist is not None:
            feet = dist * 3
            pm, used = self._ctx_stat(lambda c: self.store.putting_make_rate(player, feet, context=c),
                                      lambda d: (d.get("attempts") or 0) < 4)
            if (pm.get("attempts") or 0) >= 4:
                facts.append(f"{who} has made {pm['made'] or 0} of {pm['attempts']} putts from around "
                             f"{feet:.0f} feet {self._ctx_phrase(used)}.")
            # On a long lag the danger is the three-putt, which is what a broadcast leans on from here.
            if feet >= 25:
                tp, tused = self._ctx_stat(lambda c: self.store.three_putt_rate(player, feet, context=c),
                                           lambda d: (d.get("holes") or 0) < 4)
                if (tp.get("holes") or 0) >= 4 and tp.get("three_putts") is not None:
                    pct = round(100 * tp["three_putts"] / tp["holes"])
                    facts.append(f"{who} three-putts from around {feet:.0f} feet {pct}% of the time "
                                 f"{self._ctx_phrase(tused)} ({tp['three_putts']} of {tp['holes']}).")
        elif dist is not None:
            # A shot from trouble gets the lie-specific recovery stat (greenside sand, rough); any other
            # full shot gets the general approach-proximity one. One proximity fact either way, no spam.
            lie = (hud.lie or "").lower()
            thin_n = lambda d: (d.get("n") or 0) < 4
            if lie in ("sand", "bunker") and dist <= 30:
                rp, rused = self._ctx_stat(
                    lambda c: self.store.recovery_proximity(player, ("sand", "bunker"), max_to_pin=30, context=c), thin_n)
                if (rp.get("n") or 0) >= 4 and rp.get("avg_finish_yards") is not None:
                    facts.append(f"From greenside sand, {who} usually finishes about "
                                 f"{rp['avg_finish_yards'] * 3:.0f} feet from the pin {self._ctx_phrase(rused)}.")
            elif lie == "rough" and dist <= 120:
                rp, rused = self._ctx_stat(
                    lambda c: self.store.recovery_proximity(player, ("rough",), max_to_pin=120, context=c), thin_n)
                if (rp.get("n") or 0) >= 4 and rp.get("avg_finish_yards") is not None:
                    facts.append(f"From the rough, {who} usually finishes about "
                                 f"{rp['avg_finish_yards'] * 3:.0f} feet from the pin {self._ctx_phrase(rused)}.")
            else:
                ap, aused = self._ctx_stat(lambda c: self.store.approach_proximity(player, dist, context=c), thin_n)
                if (ap.get("n") or 0) >= 4 and ap.get("avg_finish_yards") is not None:
                    facts.append(f"From around {dist:.0f} yards, {who} usually finishes about "
                                 f"{ap['avg_finish_yards'] * 3:.0f} feet from the pin {self._ctx_phrase(aused)}.")
        if hud.shot_number == 1:
            # Driving accuracy only on a par 4/5 tee (a driver on a par 3 isn't a fairway chance, and
            # the shots table has no par to filter the query itself).
            if not is_putt and (self.current_par or 4) >= 4:
                fw, used = self._ctx_stat(lambda c: self.store.fairways_hit(player, context=c),
                                          lambda d: (d.get("tries") or 0) < 6)
                if (fw.get("tries") or 0) >= 6:
                    facts.append(f"{who} finds the fairway off the tee "
                                 f"{round(100 * (fw['hit'] or 0) / fw['tries'])}% of the time {self._ctx_phrase(used)}.")
            prof = self.store.player_profile(player)
            if (prof.get("favorite_club_n") or 0) >= 6 and prof.get("favorite_club"):
                facts.append(f"{who}'s most-used club this season is the {club_name(prof['favorite_club'])}.")
            bias = prof.get("avg_start_dir")
            if bias is not None and (prof.get("aimed") or 0) >= 8 and abs(bias) >= 2:
                facts.append(f"{who} tends to start the ball {'right' if bias > 0 else 'left'} of the aim.")
        # Season colour (scrambling, GIR, birdie rate, putts a hole...) widens the palette so the booth
        # isn't on the same beat every shot. On the tee always, and about half of other shots; the
        # selectivity instruction picks one angle, and each stat is sliced to the round's context.
        if hud.shot_number == 1 or self.rng.random() < 0.5:
            facts += self._player_season_facts(player, ctx)
        facts += self._contrast_facts(player, is_putt, hud)   # at most one league-vs-practice contrast
        return facts

    def _contrast_facts(self, player: str, is_putt: bool, hud: Hud) -> list[str]:
        """At most ONE league-vs-practice contrast for a marquee stat, so the booth can note a player
        who steps up when it counts (or the reverse). The sample gates are DELIBERATELY larger than the
        single-rate gates and the margins wider: a difference needs a real sample behind it, since one
        made putt out of a tiny sample can swing a rate 25 points. Both slices clear the gate and differ
        past the margin, else nothing - a contrast that isn't really there would mislead."""
        who = self._display(player)

        def pct(d, hit, tries):
            return round(100 * (d.get(hit) or 0) / d[tries]) if (d.get(tries) or 0) else None

        if is_putt and (dist := to_yards(hud.to_pin)) is not None:
            feet = dist * 3
            lg = self.store.putting_make_rate(player, feet, context="league")
            pr = self.store.putting_make_rate(player, feet, context="practice")
            if (lg.get("attempts") or 0) >= 10 and (pr.get("attempts") or 0) >= 10:
                a, b = pct(lg, "made", "attempts"), pct(pr, "made", "attempts")
                if abs(a - b) >= 25:   # wide margin: one made putt (~10pp at n=10) can't conjure the gap
                    tail = "he steps up when it counts" if a > b else "sharper in practice than when it counts"
                    return [f"{who} makes {a}% of these in league but {b}% in practice - {tail}."]
            return []
        if hud.shot_number == 1 and not is_putt:
            # Driving accuracy, then the round-level rates (GIR, scrambling); first marquee gap wins.
            if (self.current_par or 4) >= 4:
                lg, pr = self.store.fairways_hit(player, context="league"), self.store.fairways_hit(player, context="practice")
                if (lg.get("tries") or 0) >= 12 and (pr.get("tries") or 0) >= 12:
                    a, b = pct(lg, "hit", "tries"), pct(pr, "hit", "tries")
                    if abs(a - b) >= 15:
                        where = "practice rounds" if b > a else "league play"
                        return [f"{who} finds more fairways in {where} ({max(a, b)}%) than the other ({min(a, b)}%)."]
            lg = self.store.player_broadcast_stats(player, context="league")
            pr = self.store.player_broadcast_stats(player, context="practice")
            for hit, tries, min_t, label in (("gir_hit", "gir_tries", 18, "greens in regulation"),
                                             ("scr_hit", "scr_tries", 12, "up-and-downs")):
                if (lg.get(tries) or 0) >= min_t and (pr.get(tries) or 0) >= min_t:
                    a, b = pct(lg, hit, tries), pct(pr, hit, tries)
                    if abs(a - b) >= 15:
                        tail = "a league gear" if a > b else "it slips when it counts"
                        return [f"{who}: {label} {a}% in league vs {b}% in practice - {tail}."]
        return []

    def _player_season_facts(self, player: str | None, ctx: str | None = None) -> list[str]:
        """Per-individual-player season rates a broadcast leans on - scrambling, sand saves, greens in
        regulation, plus the scores-vs-par stats (birdie rate, bounce-back, par-type scoring, closing
        stretch, putts per hole) - each gated on a real sample, and sliced to the round's context
        (league/practice) with a lifetime fallback when that slice is thin. The per-team version is
        _team_season_facts + _team_hole_stats; this is the same for one player's own ball, counting
        individual-ball rounds only (gated in the store). Noun-phrased so it reads as a player or a team."""
        if not player or player == "unknown":
            return []
        who = self._display(player)
        bs = self.store.player_broadcast_stats(player, context=ctx)
        hs = self.store.player_hole_stats(player, context=ctx)
        used = ctx
        # Thin in this context (too few rounds of this kind): fall back to the lifetime slice so the
        # player still has season colour, labelled "this season" rather than the context.
        if (bs.get("gir_tries") or 0) < 18 and (bs.get("scr_tries") or 0) < 6 and (hs.get("holes") or 0) < 18:
            bs = self.store.player_broadcast_stats(player)
            hs = self.store.player_hole_stats(player)
            used = None
        when = self._ctx_phrase(used)
        facts: list[str] = []
        if (bs.get("scr_tries") or 0) >= 6:
            facts.append(f"{who}: up and down on {round(100 * (bs['scr_hit'] or 0) / bs['scr_tries'])}% of missed "
                         f"greens {when} ({bs['scr_hit'] or 0} of {bs['scr_tries']}).")
        if (bs.get("sand_tries") or 0) >= 4:
            facts.append(f"{who}: par saved from greenside sand {round(100 * (bs['sand_hit'] or 0) / bs['sand_tries'])}% "
                         f"of the time {when} ({bs['sand_hit'] or 0} of {bs['sand_tries']}).")
        if (bs.get("gir_tries") or 0) >= 18:
            facts.append(f"{who}: {round(100 * (bs['gir_hit'] or 0) / bs['gir_tries'])}% of greens hit in "
                         f"regulation {when}.")
        if (bs.get("putt_holes") or 0) >= 18:
            facts.append(f"{who}: averages {bs['putts'] / bs['putt_holes']:.1f} putts a hole {when}.")
        if (hs.get("holes") or 0) >= 18:
            facts.append(f"{who}: birdie or better on {round(100 * (hs['birdies'] or 0) / hs['holes'])}% of holes {when}.")
        if (hs.get("bounce_tries") or 0) >= 5:
            facts.append(f"{who}: answers a bogey with a birdie {round(100 * (hs['bounce_hit'] or 0) / hs['bounce_tries'])}% "
                         f"of the time {when}.")
        # Par-type and closing-stretch scoring are NOT added here: in an individual-ball format each
        # player is already their own Side, so _hole_type_fact and _closing_fact (team-level, keyed by
        # the player's side) fire those exact lines in this same tee fact list. player_hole_stats still
        # exists for them, but surfacing here would duplicate verbatim.
        return facts

    def _stakes_facts(self, hud: Hud, player: str | None) -> list[str]:
        """The match situation late in a league round: holes left and where this player stands against
        the field on the card, so the booth can put weight on a shot that's actually for something."""
        r = self.round
        if r is None or not r.is_league or not player:
            return []
        holes_left = r.holes - r.seq + 1   # includes the hole being played
        if 1 <= holes_left <= 4 and (net := self._net_stakes(r, player, holes_left)):
            return net
        me = _to_par_value(hud.score_to_par)
        if me is None or holes_left > 4 or holes_left < 1:
            return []
        field = [(self._display(player), me)]
        field += [(o.name, _to_par_value(o.score_to_par)) for o in hud.others
                  if _to_par_value(o.score_to_par) is not None]
        if len(field) < 2:
            return []
        low = min(v for _, v in field)
        who = self._display(player)
        facts = [f"{holes_left} hole{'s' if holes_left != 1 else ''} to play."]
        if me == low:
            shared = sum(1 for _, v in field if v == low) > 1
            verb = "shares the lead" if shared else "leads"
            facts.append(f"{who} {verb} on the card at {_fmt_to_par(me)}.")
        else:
            leader = next(n for n, v in field if v == low)
            facts.append(f"{who} is {_fmt_to_par(me)}, {me - low} back of {leader}.")
        if (cf := self._closing_fact(r.side_for(player))):
            facts.append(cf)
        return facts

    FORM_PUTT_BANDS = ((0, 5), (5, 10), (10, 15), (15, 25), (25, None))     # feet
    FORM_APPROACH_BANDS = ((50, 100), (100, 150), (150, 250))               # yards
    FORM_BIRDIE_LOOK_FEET = 15

    def _round_form_facts(self, player: str | None, is_putt: bool, hud: Hud) -> list[str]:
        """The round so far as a stat sheet, from `round_form_after_holes` finished holes on: putting by
        distance, approach leaves by distance, greens, fairways, driving, scrambling, birdie looks,
        each beside the player's season number. The booth finds the pattern, if there is one; the sheet
        only says what happened. It's held back for two holes after the booth uses it, and comes with the
        angles already used this round and in recent rounds, so the booth doesn't settle into a refrain."""
        self._form_pending = None   # a line is only credited to the sheet its own setup was given
        r, after = self.round, self.cfg.commentary.round_form_after_holes
        side = r.side_for(player) if r and not r.ended and player and after > 0 else None
        if side is None:
            return []
        done = {q for q in range(1, r.holes + 1) if r.card_score(side, q) is not None}
        if len(done) < after:
            return []
        said = self._form_said.get((r.id, side.key), [])
        if any(r.seq - seq < 2 for seq, _ in said):
            return []
        recs = sorted((x for x in self.state.shots if x.round_id == r.id and not x.mulligan
                       and x.hole_seq in done and (x.player in side.players or x.player in side.gspro)),
                      key=lambda x: x.shot.timestamp or 0)
        stats = r.side_stats(side, recs)
        name = side.players[0] if len(side.players) == 1 else None   # season numbers only for one player

        def feet(x) -> float | None:
            y = to_yards(x.to_pin_before)
            return None if y is None else y * 3

        def rate(a, b) -> str:
            return f" (season {round(100 * a / b)}%)" if b else ""

        def band(lo, hi, unit) -> str:
            return f"{lo}-{hi} {unit}" if hi is not None else f"over {lo} {unit}"

        rows = []
        to_par = stats.gross - sum(r.par[q] for q in done) if all(r.par.get(q) for q in done) else None
        counts = {}
        for q in done:
            if (sc := stats.scores.get(q)) is not None and r.par.get(q):
                d = sc - r.par[q]
                key = "eagles or better" if d <= -2 else "birdies" if d == -1 else "pars" if d == 0 \
                    else "bogeys" if d == 1 else "doubles or worse"
                counts[key] = counts.get(key, 0) + 1
        if to_par is not None:
            rows.append(f"Score: {_fmt_to_par(to_par)} through {len(done)} holes ("
                        + ", ".join(f"{n} {k}" for k, n in counts.items()) + ").")

        putts = [x for x in recs if x.shot.is_putt and x.outcome in ("holed", "missed", "conceded")
                 and feet(x) is not None]
        cells = []
        for lo, hi in self.FORM_PUTT_BANDS:
            got = [x for x in putts if lo < feet(x) <= (hi if hi is not None else 1e9)]
            if not got:
                continue
            base = self.store.putt_band_rate(name, lo, hi) if name else {}
            season = rate(base.get("made") or 0, base["attempts"]) if (base.get("attempts") or 0) >= 10 else ""
            cells.append(f"{band(lo, hi, 'ft')} {sum(x.outcome == 'holed' for x in got)} of {len(got)}{season}")
        if cells:
            rows.append("Putts holed by length: " + "; ".join(cells) + ".")
        if stats.putt_holes:
            rows.append(f"Putting: {stats.putts} putts on {stats.putt_holes} holes, {stats.one_putts} one-putts, "
                        f"{stats.three_putts} three-putts.")
        looks = made_looks = 0
        for q in done:
            first = next((x for x in recs if x.hole_seq == q and x.lie_before == "green"), None)
            hole_par = r.par.get(q)
            if first and hole_par and first.shot_number and first.shot_number <= hole_par - 1 \
                    and (f := feet(first)) is not None and f <= self.FORM_BIRDIE_LOOK_FEET:
                looks += 1
                made_looks += stats.scores.get(q, 99) <= hole_par - 1
        if looks:
            rows.append(f"Birdie putts from inside {self.FORM_BIRDIE_LOOK_FEET} feet: {made_looks} of {looks} converted.")

        leaves: dict[tuple, list[float | None]] = {}
        # In a scramble every partner hits each stroke and the next record is usually a partner's shot,
        # not this ball's next one, so a finish can't be paired with its approach.
        for i, x in enumerate(recs if r.format != "scramble" else []):
            if x.shot.is_putt or (y := to_yards(x.to_pin_before)) is None \
                    or (x.shot_number == 1 and (x.par or r.par.get(x.hole_seq) or 0) >= 4):
                continue
            for lo, hi in self.FORM_APPROACH_BANDS:
                if lo <= y < (hi if hi is not None else 1e9):
                    nxt = recs[i + 1] if i + 1 < len(recs) else None
                    if x.outcome in ("holed", "conceded"):
                        leaves.setdefault((lo, hi), []).append(0.0 if x.outcome == "holed" else 3.0)
                    elif nxt and nxt.hole_seq == x.hole_seq and nxt.lie_before == "green" \
                            and (f := feet(nxt)) is not None:
                        leaves.setdefault((lo, hi), []).append(f)
                    else:
                        leaves.setdefault((lo, hi), []).append(None)
        cells = []
        for (lo, hi), got in leaves.items():
            on = [f for f in got if f is not None]
            base = self.store.approach_leave(name, lo, hi) if name else {}
            season = f", season {base['avg_finish_yards'] * 3:.0f} ft" if (base.get("n") or 0) >= 10 else ""
            avg = f", average {sum(on) / len(on):.0f} ft from the hole{season}" if on else ""
            cells.append(f"from {band(lo, hi, 'yd')} {len(on)} of {len(got)} on the green{avg}")
        if cells:
            rows.append("Approaches: " + "; ".join(cells) + ".")

        season = self.store.player_broadcast_stats(name) if name else {}
        hit, tries = stats.gir
        if tries:
            rows.append(f"Greens in regulation: {hit} of {tries}"
                        f"{rate(season.get('gir_hit') or 0, season['gir_tries']) if (season.get('gir_tries') or 0) >= 18 else ''}.")
        hit, tries = stats.fir
        if tries:
            fw = self.store.fairways_hit(name) if name else {}
            rows.append(f"Fairways: {hit} of {tries}"
                        f"{rate(fw.get('hit') or 0, fw['tries']) if (fw.get('tries') or 0) >= 12 else ''}.")
        tees = [x for x in recs if x.shot_number == 1 and (x.par or r.par.get(x.hole_seq) or 0) >= 4]
        lengths = list(self.store.shot_lengths([x.shot_id for x in tees]).values())
        if lengths:
            rows.append(f"Drives on par 4s and 5s: average {sum(lengths) / len(lengths):.0f} yards over {len(lengths)}"
                        + (f", longest {stats.longest_drive:.0f}" if stats.longest_drive else "") + ".")
        hit, tries = stats.scramble
        if tries:
            rows.append(f"Up and down after missing the green: {hit} of {tries}"
                        f"{rate(season.get('scr_hit') or 0, season['scr_tries']) if (season.get('scr_tries') or 0) >= 10 else ''}.")
        hit, tries = stats.sand
        if tries:
            rows.append(f"Sand saves: {hit} of {tries}.")
        if stats.penalties:
            rows.append(f"Penalty strokes: {stats.penalties}.")
        if len(rows) <= 1:
            return []
        self._form_pending = (r.id, side.key, list(side.players), r.seq)

        used_now = sorted({t for _, topics in said for t in topics})
        recent = [] if name is None else self.store.recent_form_mentions(name)
        guide = ("Optional colour, not a script: most shots need none of it. If something here is genuinely "
                 "telling (a strength or weakness today, a contrast with the season, something that matters for "
                 "this shot) you may work in one point, in your own words. A count of one or two is not a "
                 "pattern. Season figures are in brackets.")
        if used_now:
            guide += f" Already used this round: {', '.join(used_now)}; find a different angle or skip it."
        if recent:
            guide += f" Angles the booth leaned on for {side.label} in recent rounds: {', '.join(recent)}; prefer something fresher."
        return [f"Round so far for {side.label} (completed holes only). {guide}\n" + "\n".join("  " + x for x in rows)]

    LENGTH_NORM_MIN_SHOTS = 5

    @staticmethod
    def _is_drive(hud: Hud) -> bool:
        """A tee shot on a hole too long to reach (as the season's drive norm counts them)."""
        on_tee = hud.shot_number == 1 or (hud.lie or "").lower() == "tee"
        return on_tee and (to_yards(hud.to_pin) or 0) >= 250

    def _length_norm_facts(self, player: str | None, club: str | None, tee: bool, to_pin: float | None,
                           total: float | None = None) -> list[str]:
        """The player's usual length (drives off a par-4/5 tee, else this club's full swings), so the
        booth judges a shot against this player's game, not a long hitter's. With `total` (after the
        shot), how this one compared. Nothing until there's a sample; the prompt then says to leave
        length alone."""
        if not player or player == "unknown" or self._anonymous(player):
            return []
        who = self._display(player)
        if tee:
            norm, what = self.store.length_norm(player), "drives"
        elif club:
            norm, what = self.store.length_norm(player, club), f"full {club_name(club)} shots"
        else:
            return []
        if (norm.get("n") or 0) < self.LENGTH_NORM_MIN_SHOTS or not norm.get("avg_total"):
            return []
        avg = norm["avg_total"]
        if not tee and (to_pin is None or to_pin <= avg * 1.15):
            return []   # a partial or a shot at the green: a full swing's length says nothing about it
        fact = f"{who}'s {what} usually go about {avg:.0f} yards in total."
        if total:
            d = total - avg
            fact += (" This one was about that, a normal length for them: nothing to remark on." if abs(d) <= max(10, 0.1 * avg)
                     else f" This one went {abs(d):.0f} yards {'further' if d > 0 else 'shorter'} than that.")
        return [fact]

    def _finish_correction_facts(self, hud: Hud, player: str | None) -> list[str]:
        """The setup screen shows where the last ball really stopped. The result call reads the lie while
        the ball may still be rolling (a read takes over a second), so a ball that trickled off the green
        got called on it. When the same ball's next setup shows a different lie, own it in a few words."""
        if self.round and not self.round.ended and self.round.format == "scramble":
            return []   # the next shot may be from a partner's picked ball, not this one
        recs = self.state.player_shots(player or "unknown")
        last = recs[-1] if recs else None
        if (last is None or last.shot.is_putt or last.mulligan or last.shot_id in self._lie_corrected
                or last.hole != self.state.current_hole or not hud.shot_number or not last.shot_number
                or hud.shot_number != last.shot_number + 1):
            return []
        called, now = (last.hud.lie or "").lower(), (hud.lie or "").lower()
        if not called or not now or now == "tee" or _lie_family(called) == _lie_family(now):
            return []
        self._lie_corrected.add(last.shot_id)
        hud._lie_correction = True
        log.info("Last call had shot %s finishing on the %s; the setup shows %s", last.shot_id, called, now)
        return [f"Correction: the last shot's call read the ball {_lie_prep(called)} the {called} while it was "
                f"still moving, but it finished {_lie_prep(now)} the {now}. If the booth said {called}, set it "
                f"straight in a few words (it trickled off into the {now}, say) before the setup."]

    def _team_score_facts(self, player: str | None) -> list[str]:
        """Best ball: the team's running gross score beside each partner's own. GSPro's HUD score is the
        individual's, and with no team number the booth credited a partner's or team's figure to one
        player ("keeps Derek at +1" when +1 was the team's)."""
        r = self.round
        side = r.side_for(player) if r and not r.ended and player else None
        tg = r.team_gross(side.team) if side and side.team else None
        if tg is None:
            return []
        team_par, holes, own = tg
        mine = next(v for s, v in own if s is side)
        partners = [(s, v) for s, v in own if s is not side]
        if holes:
            parts = "; ".join(f"{s.label} on their own is {_fmt_to_par(v)}"
                              for s, v in [(side, mine)] + partners)
            facts = [f"Team {side.team} best-ball score through {holes} completed "
                     f"hole{'s' if holes != 1 else ''}: {_fmt_to_par(team_par)} "
                     f"(the better ball each hole). {parts}. The player's round score in the other facts is "
                     f"their own, not the team's: never give the team number as one player's score, or one "
                     f"player's as the team's."]
        else:
            facts = []
        par = r.par.get(r.seq)
        for s, _ in partners:
            if (sc := r.card_score(s, r.seq)) is not None and par:
                name = SCORE_NAMES.get(sc - par)
                facts.append(f"{s.label} has already finished this hole in {sc}"
                             + (f" ({name})" if name else "")
                             + f", so the team has {sc} here unless {side.label} beats it.")
        return facts

    def _prediction_facts(self, player: str | None, is_putt: bool, hud: Hud) -> list[str]:
        """A commentator calls whether this putt drops (from the player's make-rate, or distance when
        there's no data), to be graded against the result. Puts the commentators in the game and gives
        a running 'how good are their calls' stat. Only for putts worth a call, not tap-ins or lags."""
        if not is_putt or not player or player == "unknown" or len(self.speakers) == 0:
            return []
        dist = to_yards(hud.to_pin)
        if dist is None:
            return []
        feet = dist * 3
        if feet < 3 or feet > 25:
            return []
        pm = self.store.putting_make_rate(player, feet)
        if (pm.get("attempts") or 0) >= 4 and pm.get("made") is not None:
            predicted_make = pm["made"] / pm["attempts"] >= 0.5
        else:
            predicted_make = feet <= 8
        commentator = self.speakers[self._pred_turn % len(self.speakers)]
        self._pred_turn += 1
        # Stash the call on this setup's HUD; handle_preshot commits it only if the line actually airs,
        # so an unspoken call is never written to the season prediction record.
        hud._prediction = (commentator, predicted_make, player)
        name = self.cfg.personas[commentator].name
        facts = [f"{name} is calling this putt to {'drop' if predicted_make else 'slide by'}; have "
                 f"{name} make that prediction out loud before the stroke."]
        rec = self.store.prediction_record(commentator)
        if rec["total"] >= 5:
            facts.append(f"{name} has been right on {rec['hits']} of {rec['total']} shot calls this season.")
        return facts

    def _net_stakes(self, r: Round, player: str, holes_left: int) -> list[str]:
        """Late in a league round, the match on net: this team, the other teams in the group, and the
        best score other teams have posted for the week. [] when the net isn't known (falls back to the
        card's gross)."""
        side = r.side_for(player)
        team = side and (side.team or side.key)
        tn = r.team_net(team) if team else None
        if not tn:
            return []
        facts = [f"{holes_left} hole{'s' if holes_left != 1 else ''} to play.",
                 f"{self.league.team_name(team)} are net {to_par(tn[0])} through {tn[1]} holes."]
        for t in r.teams:
            if t and t != team and (o := r.team_net(t)):
                facts.append(f"{self.league.team_name(t)} are net {to_par(o[0])} through {o[1]} holes.")
        if (others := self._week_others(r)):
            best = self.league.team_name(others[0].team)
            facts.append(f"The best week {r.schedule.get('week')} score posted on Squabbit: "
                         f"{best} at net {to_par(others[0].net)}.")
            if tn[1] + holes_left == r.holes:   # the card is current, so "the last N holes" is exact
                facts.append(_week_gap_fact(self.league.team_name(team), tn[0], best, others[0].net, holes_left))
        if (cf := self._closing_fact(side)):
            facts.append(cf)
        return facts

    def _hole_field_facts(self, player: str | None) -> list[str]:
        """On the first shot of a hole: the hole's difficulty (stroke index), how the league has
        scored it this season, and whether this player gets a handicap stroke here (so the booth can
        talk raw and net). All from the course card + season rounds, no OCR."""
        r = self.round
        if r is None or not r.is_league:
            return []
        hole, par, sidx = r.hole_number(r.seq), r.par.get(r.seq), r.index.get(r.seq)
        if hole is None or par is None:
            return []
        facts: list[str] = []
        if sidx:
            hard = " (one of the toughest on the card)" if sidx <= 4 else \
                   " (one of the easiest)" if sidx >= 15 else ""
            facts.append(f"This hole is stroke index {sidx}{hard}.")
        scores = self.store.hole_scoring(r.course, hole, context="league")   # this fact is league-only
        if len(scores) >= 4:
            avg = sum(scores) / len(scores)
            under = sum(1 for s in scores if s < par)
            facts.append(f"The league has played this hole {len(scores)} times this season, averaging "
                         f"{avg:.1f} ({avg - par:+.1f} to par); {under} were under par.")
            if (low := min(scores)) < par:
                facts.append(f"The low score on this hole this season is a {low}.")
        # Use the round's own per-hole allocation (it applies the holes/18 scaling and the format's
        # handicap allowance), so the net the booth states matches how the round is actually scored.
        side = r.side_for(player)
        strokes = side.hole_strokes.get(r.seq, 0) if side else 0
        if strokes > 0:
            facts.append(f"{self._display(player)} gets {strokes} handicap stroke"
                         f"{'s' if strokes > 1 else ''} here, so a {par} plays to net {par - strokes}.")
        if (ht := self._hole_type_fact(side, par)):
            facts.append(ht)
        if side and r.seq > 1 and (tn := r.team_net(side.team or side.key)):
            facts.append(f"{self.league.team_name(side.team or side.key)} are net {to_par(tn[0])} through "
                         f"{tn[1]} hole{'s' if tn[1] != 1 else ''}.")
            if (others := self._week_others(r)):
                facts.append(f"The best week {r.schedule.get('week')} score posted on Squabbit is "
                             f"{self.league.team_name(others[0].team)} at net {to_par(others[0].net)}.")
        return facts

    def _team_season_facts(self, side) -> list[str]:
        """Season team stats a broadcast leans on - scrambling, sand saves, GIR, birdie rate, bounce-back,
        course history - each gated on a real sample so a thin season can't produce confident numbers.
        Noun-phrased so they read right whether the side is a team or a single player, and the caller
        picks among them (so it isn't always the first one that airs)."""
        r = self.round
        if side is None or r is None:
            return []
        who = side.label
        facts: list[str] = []
        bs = self.store.team_broadcast_stats(side.key)
        if (bs.get("scr_tries") or 0) >= 6:
            facts.append(f"{who}: up and down on {round(100 * (bs['scr_hit'] or 0) / bs['scr_tries'])}% of missed "
                         f"greens this season ({bs['scr_hit'] or 0} of {bs['scr_tries']}).")
        if (bs.get("sand_tries") or 0) >= 4:
            facts.append(f"{who}: par saved from greenside sand {round(100 * (bs['sand_hit'] or 0) / bs['sand_tries'])}% "
                         f"of the time this season ({bs['sand_hit'] or 0} of {bs['sand_tries']}).")
        if (bs.get("gir_tries") or 0) >= 18:
            facts.append(f"{who}: {round(100 * (bs['gir_hit'] or 0) / bs['gir_tries'])}% of greens hit in regulation this season.")
        hs = self.store.team_hole_stats(side.key)
        if (hs.get("holes") or 0) >= 18:
            facts.append(f"{who}: birdie or better on {round(100 * (hs['birdies'] or 0) / hs['holes'])}% of holes this season.")
        if (hs.get("bounce_tries") or 0) >= 5:
            facts.append(f"{who}: a bogey answered with a birdie {round(100 * (hs['bounce_hit'] or 0) / hs['bounce_tries'])}% "
                         f"of the time this season.")
        ch = self.store.team_course_history(side.key, r.course)
        if (ch.get("rounds") or 0) >= 2 and ch.get("avg_net_to_par") is not None:
            scale = r.holes / 9   # stored per nine holes; put on this round's length
            facts.append(f"{who} on this course: {round(ch['avg_net_to_par'] * scale):+d} net over {ch['rounds']} "
                         f"rounds this season (best {round(ch['best_net_to_par'] * scale):+d}"
                         f"{'' if r.holes == 9 else f', scaled to {r.holes} holes'}).")
        return facts

    def _hole_type_fact(self, side, par: int | None) -> str | None:
        """How this side scores on holes of THIS par (3/4/5) this season, for the hole intro."""
        if side is None or par not in (3, 4, 5):
            return None
        s, n = self.store.team_hole_stats(side.key).get(f"par{par}", (0, 0))
        return f"{side.label}: {(round(s / n, 1) or 0.0):+.1f} to par on the par {par}s this season." if n >= 4 else None

    def _closing_fact(self, side) -> str | None:
        """A side's scoring over the closing stretch this season, for the late-round stakes."""
        if side is None:
            return None
        hs = self.store.team_hole_stats(side.key)
        n = hs.get("closing_holes") or 0
        return (f"{side.label}: {(round(hs['closing_to_par'] / n, 1) or 0.0):+.1f} to par on each of their closing holes "
                f"this season.") if n >= 6 else None

    def _gspro_idx(self, player: str | None) -> int | None:
        """The GSPro player index for the name being commentated, for reading the log's per-player
        data (lie, distance, strokes, penalties). GSPro indexes players by setup slot, so the index is
        the slot position (slot 1 = 0). None when the log can't be safely attributed: not enabled, no
        round, alternate shot (shared ball), or a MULTIPLAYER round not built from the setup slots (a
        tee-time roster is in honour order, so its position isn't GSPro's index)."""
        r = self.round
        if not self._log_truth or r is None or r.ended or r.format == "alternate":
            return None
        if r.slot_names:                       # captured setup slots: the trustworthy name -> index map
            return r.roster_position(player)
        if len(r.roster) <= 1 and not self.gspro.multiplayer:
            return 0                           # genuinely one player (the log shows one too): index 0
        return None                            # no slot order and more than one player: don't attribute

    def _shot_wet(self, player: str | None, since: float | None = None) -> bool:
        """True when the GSPro log's Hit_water says this player's current shot went in the water. Used
        for the in-flight tease and the instant splash reaction. False when the log can't be attributed
        to a player (idx None) - don't fall through to wet(None), which would borrow the active player's
        wet state and mis-credit the splash to the wrong player. `since` (a monotonic time) gates the
        in-flight tease to a splash seen after this shot's impact, so a stale earlier one can't fire it."""
        gs = getattr(self, "gspro", None)
        idx = self._gspro_idx(player)
        if gs is None or idx is None:
            return False
        return gs.wet(idx, since=since)

    def _truth_context_facts(self, is_putt: bool, idx: int | None) -> list[str]:
        """Gimme-circle context from the GSPro log for this player.

        The league concedes putts inside a set radius (the log's gimme distance, nominally four feet),
        so a ball inside the circle is auto-conceded and never actually putted. A putt the player is
        over is therefore outside the circle; the one worth a word is a short one left just beyond it.
        """
        g = self.gspro
        to_pin, gimme = g.to_pin_of(idx), g.gimme_of(idx)
        if idx is None or not (is_putt and to_pin is not None and gimme):
            return []
        gimme_ft, dist_ft = gimme * 3, to_pin * 3
        if dist_ft <= gimme_ft + 3:
            return [f"This putt sits just outside the {gimme_ft:.0f}-foot gimme circle, about "
                    f"{dist_ft:.0f} feet; it was not conceded, so it has to be holed."]
        return []

    def _penalty_facts_from_truth(self, idx: int) -> list[str]:
        """The penalty fact from GSPro's own stroke count, told once per new penalty on a hole."""
        hole = self.gspro.hole_by.get(idx, self.gspro.current_hole)   # this player's hole, not the global one
        penalties = self.gspro.penalties(idx)
        if penalties <= self._narrated_pen.get((idx, hole), 0):
            return []
        self._narrated_pen[(idx, hole)] = penalties
        n = self.gspro.shot_number(idx)
        return [f"A penalty stroke was taken (a ball in the water or out of bounds, then a drop); "
                f"{penalties} penalty stroke{'s' if penalties > 1 else ''} on this hole so far. Counting "
                f"the penalty, this is stroke {n} on the hole: call it the {ordinal(n)}, not an earlier number."]

    def _strategy_facts(self, minimap: "MinimapRead | None", hud: Hud) -> list[str]:
        """The minimap's read on where to miss, offered at most once a hole and not every hole, so the
        booth plays the course now and then ("with those bunkers short, he can't come up short") rather
        than on every swing."""
        hole = self.state.current_hole
        if minimap is None or hole is None or self._strategy_hole == hole:
            return []
        if not (line := minimap.strategy(to_yards(hud.to_pin), hud.lie)) or self.rng.random() >= STRATEGY_CHANCE:
            return []
        self._strategy_hole = hole
        return [line]

    async def _preshot_model(self, hud: Hud, player: str | None, images: list[bytes],
                             minimap_task: "asyncio.Task[MinimapRead] | None" = None,
                             putt_task: "asyncio.Task[PuttRead] | None" = None) -> tuple[dict[str, str], str]:
        """Build the pre-shot facts and make the model calls, returning ({call: raw_json}, facts_text).
        Three concurrent calls, each playing to a model's strength so no one call is slow or truncates:
        the spoken setup line (fast text model, swing-critical), the hazard read (vision model, its own
        minimal prompt so it stays fast - Luna hallucinates hazards, so this stays on the vision model),
        and the 11 flight/result branches (fast text model). Kept separate so the watcher can start it at
        setup, before the full HUD read finishes."""
        g = self.gspro
        idx = self._gspro_idx(player)   # this player's GSPro index, or None when the log isn't attributable
        # The log's material is where the ball first LANDED, not where it came to rest (a drive that
        # kicked off the cart path into the rough logs "concrete"; across a season's log, a fairway
        # landing was played from the rough about 1 time in 12), so the HUD's resting lie wins and the
        # log only fills in a lie OCR missed. Never on the tee (the HUD's shot 1, or a tee lie), where
        # the log can still hold the previous hole's ball: its "green" made a par-3 tee shot a putt.
        on_tee = hud.shot_number == 1 or (hud.lie or "").lower() == "tee"
        if idx is not None and not on_tee and not hud.lie and g.shot_number(idx) > 1 and (lie := g.lie_of(idx)):
            hud.lie = lie
        is_putt = (hud.lie or "").lower() == "green"
        if self.round and not self.round.ended:
            hc = self._hc
            hc.setups += 1
            hc.hud_shot_missing += hud.shot_number is None
            hc.hud_to_pin_missing += not hud.to_pin
            hc.hud_lie_missing += not hud.lie
        if self._log_truth:
            log.info("GSPro-log truth: idx %s (%r) hole %s stroke %s pen %s lie %s to_pin %s | HUD shot %s lie %s to_pin %s",
                     idx, self._display(player), g.current_hole,
                     g.shot_number(idx) if idx is not None else None,
                     g.penalties(idx) if idx is not None else None, g.lie_of(idx),
                     f"{tp:.1f}" if (tp := g.to_pin_of(idx)) is not None else None,
                     hud.shot_number, hud.lie, hud.to_pin)
            # GSPro's measured distance to the pin, in place of the OCR read, for full shots (the HUD's
            # feet/inches reading stays for putts, where it's finer). Logged at ball-settle, so already
            # current for the shot being set up.
            if idx is not None and not is_putt and (tp := g.to_pin_of(idx)) is not None:
                # Trust the log's measured distance, but if it disagrees wildly with what's on screen
                # for the player at address, keep the OCR reading: a big gap means the per-player slot
                # was mis-attributed (grabbing the partner's distance in a multiplayer round).
                ocr_pin = to_yards(hud.to_pin)
                if ocr_pin is None or abs(tp - ocr_pin) <= 20:
                    hud.to_pin = f"{tp:.1f} yards"
                else:   # on a tee the log can still hold the last hole's ball: expected, not a warning
                    (log.debug if on_tee else log.warning)(
                        "Log to_pin %.1f yd diverges from HUD %.1f yd for idx %s (%r); keeping HUD "
                        "(likely a mis-attributed slot)", tp, ocr_pin, idx, self._display(player))
        minimap = await self._await_minimap(minimap_task, is_putt)
        if is_putt and putt_task is None and self.putt_reader:
            putt_task = self._start_putt_read()   # a putt the distance didn't give away (metres)
        putt = await self._await_putt(putt_task, is_putt)
        facts = (preshot_facts(hud, self.current_par, self.handed.get(player or ""))
                 + self._mulligan_facts(player, hud)
                 + self._penalty_facts(player, hud.shot_number) + self._truth_context_facts(is_putt, idx)
                 + history_facts(self._display(player), self.state.player_shots(player or "unknown"))
                 + self._format_facts() + self._ctp_facts(hud.shot_number, hud, player)
                 + self._profile_facts(player, is_putt, hud) + self._prediction_facts(player, is_putt, hud)
                 + self._stakes_facts(hud, player) + self._team_score_facts(player)
                 + self._finish_correction_facts(hud, player)
                 + self._round_form_facts(player, is_putt, hud)
                 + ([] if is_putt else self._length_norm_facts(player, hud.club, self._is_drive(hud),
                                                                       to_yards(hud.to_pin)))
                 + (self._bunker_facts(player) if (hud.lie or "").lower() in ("sand", "bunker")
                    and self.rng.random() < 0.5 else [])
                 + (self._hole_field_facts(player) if hud.shot_number == 1 else [])
                 + (minimap.facts(to_yards(hud.to_pin), hud.lie) if minimap else [])
                 + self._strategy_facts(minimap, hud)
                 + (putt.facts(self.cfg.putt_read, slope=putt_elevation_inches(hud) is None) if putt else []))
        who, team = self._who(player)
        second = self.speakers[1] if len(self.speakers) > 1 else None
        lag_feet = self.cfg.commentary.gimme_lag_feet
        lag = is_putt and lag_feet > 0 and (to_yards(hud.to_pin) or 0) * 3 >= lag_feet
        line_user, facts_text = preshot_line_prompt(
            player=who, team=team, hole=self.state.current_hole, facts=facts,
            recent_lines=self.state.recent_lines, speaker=self.speakers[0], second=second, is_putt=is_putt,
        )
        branch_user, _ = preshot_branch_prompt(
            player=who, team=team, hole=self.state.current_hole, facts=facts,
            recent_lines=self.state.recent_lines, speaker=self.speakers[0], second=second, is_putt=is_putt,
            named=minimap.named(to_yards(hud.to_pin)) if minimap else None, lag=lag,
        )
        # Three concurrent calls: the spoken line (live_llm, text) and the branches (flight_llm, text)
        # always run; the hazard read (preshot_llm, vision) runs only for a full shot with an image to
        # read. A lean fact set (lie + pin) keeps the vision call fast. Order the dict so the swing-
        # critical line is first in the gather for the clearest failure logging.
        calls: dict[str, Any] = {
            "line": self._complete("preshot-line", self.live_llm, line_user, [], preshot_line_schema(self.speakers)),
            "branches": self._complete("preshot-branches", self.flight_llm, branch_user, [],
                                       preshot_branch_schema(self.speakers, is_putt, lag)),
        }
        if minimap is not None:
            calls["hazards"] = self._done(json.dumps({"hazards": [
                {"kind": h.kind, "side": h.side, "from_yards": h.from_yards, "to_yards": h.to_yards,
                 "edge": [list(e) for e in h.edge], "off_yards": h.off_yards,
                 "wide_yards": h.wide_yards}
                for h in minimap.hazards(to_yards(hud.to_pin))], "named": minimap.named(to_yards(hud.to_pin))}))
        elif images and not is_putt:
            haz_facts = [f for f in (f"Lie: {hud.lie}." if hud.lie else None,
                                     f"To the pin: {hud.to_pin}." if hud.to_pin else None) if f]
            haz_user, _ = preshot_hazard_prompt(player=who, hole=self.state.current_hole,
                                                facts=haz_facts, speaker=self.speakers[0])
            calls["hazards"] = self._complete("preshot-hazards", self.preshot_llm, haz_user, images,
                                              preshot_hazard_schema())
        # return_exceptions so one call's transport failure (a 500/timeout on the vision call, say)
        # can't abort the others - the merge below keeps whatever succeeded. The split has three
        # failure points; none may take the swing-critical line down with it.
        results = await asyncio.gather(*calls.values(), return_exceptions=True)
        return dict(zip(calls.keys(), results)), facts_text

    @staticmethod
    async def _done(value: str) -> str:
        return value

    async def _await_minimap(self, task: "asyncio.Task[MinimapRead] | None", is_putt: bool) -> MinimapRead | None:
        """The Jev minimap read, or None (a putt, a failure, or late: the caller falls back to the vision
        model's hazard read). Bounded by minimap.timeout_seconds so it can't hold up the setup line."""
        if task is None:
            return None
        if is_putt:
            task.cancel()
            return None
        try:
            read = await asyncio.wait_for(task, self.cfg.minimap.timeout_seconds)
        except asyncio.TimeoutError:
            log.warning("Minimap read timed out after %.1fs", self.cfg.minimap.timeout_seconds)
            return None
        except Exception as e:
            log.warning("Minimap read failed: %r", e)
            return None
        log.info("Minimap read in %.2fs: %s", read.seconds,
                 "; ".join(read.facts()) or "nothing confident")
        return read

    async def _await_putt(self, task: "asyncio.Task[PuttRead] | None", is_putt: bool) -> PuttRead | None:
        """The green-grid break read, or None (not a putt, a failure, or late). Bounded by
        putt_read.timeout_seconds so it can't hold up the setup line."""
        if task is None:
            return None
        if not is_putt:
            task.cancel()
            return None
        try:
            read = await asyncio.wait_for(task, self.cfg.putt_read.timeout_seconds)
        except asyncio.TimeoutError:
            log.warning("Putt read timed out after %.1fs", self.cfg.putt_read.timeout_seconds)
            return None
        except Exception as e:
            log.warning("Putt read failed: %r", e)
            return None
        log.info("Putt read (%d frames, %.2fs): %s; sideways %s, up %d down %d (%.2f)", read.frames, read.seconds,
                 "; ".join(read.facts(self.cfg.putt_read)) or "nothing confident",
                 [(d.right, d.left, round(d.dx, 1)) for d in read.sections], read.uphill, read.downhill, read.dy)
        return read

    async def handle_preshot(self, event: PreShotEvent) -> None:
        """Looks at the hole while the player sets up: maybe a setup line, always a flight plan.

        The plan (hazards plus one mid-flight line per outcome) is what lets impact pick a
        line with no model call, so it's built for every full shot even when nothing is said.
        """
        c = self.cfg.commentary
        player = self._player(event.hud.player_text, event.player_hint)
        images = [event.image] if event.image else []
        # The call was usually started at setup (overlapping the HUD read); await that. Replay and
        # tests post no task, so fall back to making it here.
        if event.model_task is not None:
            raws, facts_text = await event.model_task
        else:
            raws, facts_text = await self._preshot_model(event.hud, player, images)
        # Merge the calls' JSON. Each is independent: if one isn't valid JSON (a truncation or a refusal),
        # keep whatever the others returned - a missing line, hazards or branches degrades to silence /
        # no hazards / a live reaction for this shot, never a lost plan across the board.
        data: dict = {}
        for label, raw in raws.items():
            if not isinstance(raw, str):  # the call raised (gathered as an exception); keep the others
                log.warning("Pre-shot %s call failed: %r", label, raw)
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("Pre-shot %s reply wasn't JSON: %r", label, raw[:200])
                continue
            if isinstance(obj, dict):  # a bare null/list/string parses fine but would blow up .update
                data.update(obj)
            else:
                log.warning("Pre-shot %s reply wasn't a JSON object: %r", label, raw[:120])
        if not data:
            self.plan = None  # every call failed; don't let the previous shot's plan air for this one
            return
        is_putt = (event.hud.lie or "").lower() == "green"
        # The flight branches come from the text-only branch call, which never saw the hole, so ground
        # them as unseen: a flight line may only name scenery/hazards present in the facts, not invent
        # them. Only the separate hazard call sees the image, and its output isn't line-grounded.
        plan = self._build_plan(data, player, event.hud.to_pin, facts_text, seen_image=False, is_putt=is_putt)
        plan.elevation_yards, plan.lie = event.hud.elevation_yards, event.hud.lie
        # A tee shot on a par 4 or 5 is rarely played at the pin: a 3-wood laid back on a 273-yard
        # par 4 got "needs a little more carry". Off the tee only the line is called.
        plan.lengthless = event.hud.shot_number == 1 and (self.current_par or 3) >= 4
        self.plan, self._plan_made_at = plan, time.monotonic()

        # Solo fallback: branches are normally built when the ball first reaches the green. On a putt
        # with none built yet (e.g. a chip-in that skipped the green-reach, or a missed read), build
        # them now for holing this putt or a two-putt.
        r = self.round
        if is_putt and event.hud.shot_number and r and not r.ended and len(r.sides) == 1 and not (
            self._wrap_branches and self._wrap_branches.get("hole") == self.state.current_hole
        ):
            self._kick_wrap_branches(r.sides[0].key, [event.hud.shot_number, event.hud.shot_number + 1], [])
            if r.seq >= r.holes:  # last hole, putt with no closer built yet: branch the closer too
                self._kick_closer_branches(r.sides[0].key, [event.hud.shot_number, event.hud.shot_number + 1])

        # Weight the dice toward the moments worth setting up: every putt (a long lag and a close
        # scoring look both deserve a word) and a short scoring approach. Routine full shots keep the
        # baseline rate. A busy booth defers a putt or short setup line until idle; it never talks over a live call.
        rate = c.preshot_rate
        pin_yd = to_yards(event.hud.to_pin)
        if is_putt or (pin_yd is not None and pin_yd <= 40):
            rate = max(rate, 0.9)
        correction = getattr(event.hud, "_lie_correction", False)
        if getattr(event.hud, "_mulligan", False) or correction:
            rate = 1.0   # a mulligan, or a wrong call to set straight, always gets its word
        # The opening tee shot skips the spoken setup line (the opener just set the round up, and a
        # line here would land late), but still pre-voices the flight and result branches below so the
        # first shot's calls are ready at impact like every other shot's.
        skip_setup_line = self._skip_opening_preshot
        self._skip_opening_preshot = False
        def setup_lines() -> list:
            lines = parse_lines(json.dumps({"lines": data.get("lines") or []}), self.speakers, c.max_words_per_line, self._persona_names)
            # No retry: a setup line that needs a second attempt has usually missed its moment.
            good, _ = self._grounded(lines, facts_text, mode="preshot", seen_image=False)  # the line call is text-only
            return good

        def aired(good: list) -> None:
            self.state.record_lines(good)
            self._write_transcript(good, "preshot")
            # The pre-shot (incl. any commentator call) aired; make its prediction gradeable, keyed
            # by player and stamped with the hole so a stale call (its putt produced no shot event)
            # is discarded at grading rather than graded against a later hole's putt.
            if (pred := getattr(event.hud, "_prediction", None)) is not None:
                self._pending_prediction[pred[2]] = (pred[0], pred[1], self.state.current_hole)

        if not skip_setup_line and c.preshot and self.rng.random() <= rate and (good := setup_lines()):
            if self.speech.is_idle():
                self.speech.say(good)
                aired(good)
            elif is_putt or correction or (pin_yd is not None and pin_yd <= 40):
                task = asyncio.create_task(self._air_setup_when_idle(good, self._shot_seq, PRESHOT_IDLE_WAIT_SECONDS, aired))
                task.add_done_callback(lambda t: t.cancelled() or t.exception())

        # Then voice the mid-flight lines while the player sets up. Voicing takes over a second on
        # some CPUs, which would land a wedge call after the ball does. Likeliest outcomes first;
        # whatever isn't ready at impact is voiced then instead.
        order = ("on_target", "left", "right", "short", "long", "left_trouble", "right_trouble")
        voicing = time.monotonic()
        outcomes = sorted(plan.lines, key=lambda o: order.index(o) if o in order else len(order))
        concurrent = getattr(self.speech.tts, "concurrent", False)
        if not self._prevoicing():
            return   # cloud voice with tts.prevoice off: the call that airs is voiced at impact/result
        if concurrent:
            # Network TTS: voice every outcome at once so they're all ready in one synth's time,
            # not one-after-another (which would land a trouble call after the ball on a quick swing).
            # On cloud TTS the synth is a network call, not local CPU, so it doesn't starve the OCR
            # loop: pre-voice the result branches too, so the result call is instant instead of paying
            # a synth round-trip after the ball stops. The branches are short and cheap; the one that
            # never airs is a negligible waste.
            # Write each clip into the plan as its own synth finishes, not in one update() after the
            # whole gather: impact cancels this task (_cancel_preshot), and a single post-gather update
            # would throw away every already-finished clip on a quick swing. Per-clip keeps what's done.
            async def voice_into(target: dict, key: str, text: str) -> None:
                if (audio := await self.speech.prepare(Line(self.speakers[0], text))) is not None:
                    target[key] = audio
            await asyncio.gather(
                *(voice_into(plan.audio, o, plan.lines[o]) for o in outcomes),
                *(voice_into(plan.reaction_audio, o, plan.reactions[o]) for o in plan.reactions),
            )
        else:
            # Local CPU (Kokoro): one at a time, likeliest first, so concurrent synths don't starve OCR.
            # Result lines stay on-demand here (they have slack after the ball stops) to keep a pile of
            # unused branch audio off the CPU at setup, where it was starving OCR reads.
            for outcome in outcomes:
                audio = await self.speech.prepare(Line(self.speakers[0], plan.lines[outcome]))
                if audio is not None:
                    plan.audio[outcome] = audio
        if plan.audio or plan.reaction_audio:
            log.info("Voiced %s/%s flight, %s/%s result lines in %.1fs, %.1fs after the setup was seen",
                     len(plan.audio), len(plan.lines), len(plan.reaction_audio), len(plan.reactions),
                     time.monotonic() - voicing, time.monotonic() - self._plan_made_at)

    def _build_plan(self, data: dict, player: str | None, to_pin: str | None, facts_text: str,
                    seen_image: bool, is_putt: bool = False) -> FlightPlan:
        hazards = []
        for h in data.get("hazards") or []:
            if isinstance(h, dict) and h.get("side") in ("left", "right", "short", "long"):
                edge = tuple((float(e[0]), float(e[1])) for e in h.get("edge") or ()
                             if isinstance(e, (list, tuple)) and len(e) == 2
                             and all(isinstance(v, (int, float)) for v in e))
                off, wide = h.get("off_yards"), h.get("wide_yards")
                hazards.append(Hazard(kind=str(h.get("kind", "trouble")), side=h["side"],
                                      from_yards=h.get("from_yards"), to_yards=h.get("to_yards"), edge=edge,
                                      off_yards=float(off) if isinstance(off, (int, float)) else None,
                                      wide_yards=float(wide) if isinstance(wide, (int, float)) else None))
        named = {side: kind for side, kind in (data.get("named") or {}).items()
                 if side in ("left", "right") and isinstance(kind, str)}
        plan = FlightPlan(player=player, to_pin=to_pin, hazards=hazards, is_putt=is_putt, named=named)
        for outcome, text in (data.get("flight_lines") or {}).items():
            if outcome not in FLIGHT_OUTCOMES or not text:
                continue
            line = parse_lines(json.dumps({"lines": [{"speaker": self.speakers[0], "text": text}]}),
                               self.speakers, self.cfg.commentary.max_words_per_line, self._persona_names)
            if outcome == "water":
                # The water tease names the water, which the unseen-scenery grounding would reject.
                # It's only ever played when the GSPro log confirms the ball is wet, so the water is a
                # fact, not a guess; guard only against an invented number (as the reactions do).
                if line and not re.search(r"\d", line[0].text):
                    plan.lines[outcome] = line[0].text
                continue
            if line and _names_other_scenery(line[0].text, plan.named.get(outcome.removesuffix("_trouble"))
                                             if outcome.endswith("_trouble") else None):
                continue
            good, _ = self._grounded(line, facts_text, mode="flight", seen_image=seen_image)
            if good:
                plan.lines[outcome] = good[0].text
        plan.reactions = self._build_reactions(data, is_putt, facts_text)
        log.info("Flight plan: hazards %s; lines for %s; reactions for %s",
                 [f"{h.kind} {h.side}" + (f" {h.from_yards}-{h.to_yards}yd" if h.from_yards else "") for h in hazards] or "none",
                 sorted(plan.lines), sorted(plan.reactions))
        return plan

    def _build_reactions(self, data: dict, is_putt: bool, facts_text: str = "") -> dict[str, str]:
        """The pre-written result lines. Each is written for one known outcome and is played only
        when that outcome happens, so the usual result-grounding (which would reject "found the
        green" before the ball lands) doesn't apply; only invented numbers and reused phrasing do."""
        out: dict[str, str] = {}
        recent = [l.text for l in self.state.recent_lines]
        for outcome, text in (data.get("reaction_lines") or {}).items():
            if outcome not in reaction_outcomes(is_putt, lag=True) or not text:
                continue
            parsed = parse_lines(json.dumps({"lines": [{"speaker": self.speakers[0], "text": text}]}),
                                 self.speakers, self.cfg.commentary.max_words_per_line, self._persona_names)
            if not parsed:
                log.info("Dropped the pre-written %s reaction (unparseable): %r", outcome, text)
                continue
            body = parsed[0].text
            # The result claim ("found the green") is trusted - it only plays when that outcome
            # happens. Guard only against a quoted number (a guess at a distance we don't have yet)
            # and reused phrasing.
            # A number from the facts (the putt's length) is fine; any other is a guess at a
            # distance we don't have yet.
            known = set(re.findall(r"\d+(?:\.\d+)?", facts_text))
            if any(n not in known for n in re.findall(r"\d+(?:\.\d+)?", body)):
                log.info("Dropped the pre-written %s reaction (quotes a number not in the facts): %r", outcome, body)
                continue
            if phrase := repeats(body, [self.system_examples]) or repeats(body, recent):
                log.info("Dropped the pre-written %s reaction (reuses %r): %r", outcome, phrase, body)
                continue
            out[outcome] = body
        return out

    # ---- the shot -------------------------------------------------------------------------

    def _impact_state(self) -> tuple:
        # Snapshot this player's per-index log state at impact, BEFORE _follow_shot advances the log to
        # the next shot's setup. Used to resolve the previous shot's finishing distance (this shot's
        # setup distance is where the previous ball came to rest) and to baseline this putt's outcome.
        gs = self.gspro
        impact_snap = (dict(gs.to_pin_by), dict(gs.hole_by), dict(gs.hole_outs_by),
                       dict(gs.gimmies_by), dict(gs._penalties), gs.current_hole,
                       {i: gs.shot_number(i) for i in range(8)},  # stroke count AT IMPACT, before the
                       #                                            slow read, so a putt-miss shows the
                       #                                            instant the next state line advances it
                       dict(gs.dropped_by))   # scramble balls not picked, to leave their finish alone
        fires0 = gs.fires   # GSPro's shot counter at impact, to find this shot's putting/regular mode
        setups0 = dict(gs.setups_by)   # each player's setups so far: the next one settles this shot
        lies0 = dict(gs.lie_by)   # each ball's logged surface going into this shot
        return impact_snap, fires0, setups0, lies0

    async def _read_impact_hud(self, tag: str) -> tuple:
        """The screen at impact: the player box still describes this shot (who, shot number,
        distance to the pin), the minimap still shows the lie it was hit from, and CARRY (raw) has
        just appeared."""
        ocr = self.cfg.screen.ocr
        wants_image = self.cfg.screen.send_images and self.flight_llm.cfg.vision and self.commentary_on
        # Only what the reaction and the carry model actually use. Slope isn't read here (unused
        # once the ball is struck). The club box: with local-CPU TTS (Kokoro) it's skipped and we
        # fall back to the loft-guessed club, to keep OCR off the synth's back; with cloud audio
        # nothing contends for the cores, so read GSPro's own club box (it beats the loft guess,
        # which misfired as "driver" on a mishit).
        regions = ["player_name", "player_box", "lie", "tiles", "tiles_left"]
        if self.cfg.tts.provider != "kokoro":
            regions.append("club")
        before, impact_image = await asyncio.gather(
            self.screen.read_hud_async(regions) if ocr else asyncio.sleep(0, Hud()),
            self.screen.snapshot_jpeg() if wants_image else asyncio.sleep(0, None),
        )
        if ocr and before.lie and before.name_sig not in (None, NAME_DARK):
            # The regions are grabbed one after another, not from one frame: if the name cell
            # changed by the end of the read, the lie may be the next player's ball.
            after = await self.screen.read_hud_async(["player_name"])
            if name_sig_changed(before.name_sig, after.name_sig):
                log.info("%s the player box changed during the impact read; lie %s dropped", tag, before.lie)
                before.lie = None
        return before, impact_image

    def _impact_hole(self) -> tuple:
        """(hole, par, round, the round's hole number) the shot was struck on: a hole change while
        its handler is still reading mustn't move it to the next hole."""
        r = self.round if self.round and not self.round.ended else None
        return self.state.current_hole, self.current_par, r, r.seq if r else None

    def _capture_impact(self, event: ShotEvent) -> None:
        event.impact_at = time.monotonic()
        event.impact_state = (self._impact_state(), *self._impact_hole())
        # Read the screen now, while it still shows this shot's player, lie and tiles.
        if self.screen is not None and event.hud_hint is None:
            event.impact_read = asyncio.ensure_future(self._read_impact_hud("[held shot]"))

    async def handle_shot(self, event: ShotEvent) -> None:
        if self._on_range():
            log.info("Range shot ignored (club %s, carry %.1f)", event.shot.club, event.shot.carry or 0)
            return
        # A shot the feed held back (GSPconnect skipped it) carries its impact time and state.
        impact = event.impact_at or time.monotonic()
        self._last_activity = time.monotonic()
        self._cancel_preshot()
        self._shot_id = None  # set once this shot is recorded; mid-flight lines come before that
        held = event.impact_state
        impact_snap, fires0, setups0, lies0 = held[0] if held else self._impact_state()
        self._apply_log_corrections()
        # The reaction runs in the background and may finish after the hole has changed; pin the
        # hole and par to this shot so a late reaction still talks about the right hole.
        shot_hole, shot_par, shot_round, round_hole = held[1:] if held else self._impact_hole()
        shot = event.shot
        self._shot_seq += 1
        my_seq = self._shot_seq   # this shot's order, to keep concurrent handlers from reordering finish
        tag = f"[shot {self._shot_seq}]"
        lag = f", {time.time() - event.logged_at:.2f}s after GSPconnect logged it" if event.logged_at else ""
        log.info("%s detected%s: club %s, ball %.1f mph, launch %s, start %.1f, spin %s @ axis %.1f, carry %.1f %s, putt=%s",
                 tag, lag, shot.club, shot.ball_speed or 0,
                 "none" if shot.vla is None else f"{shot.vla:.1f}", shot.hla or 0,
                 round(shot.total_spin) if shot.total_spin else None, shot.spin_axis or 0, shot.carry or 0,
                 shot.units, shot.is_putt)
        use_screen = self.screen is not None and event.hud_hint is None
        ocr = use_screen and self.cfg.screen.ocr

        # Play the pre-voiced flight line immediately, from the setup plan and the launch data, before
        # the (slow, under shot-animation load) impact OCR read. The line was voiced for this moment;
        # it must not wait on OCR. The impact read below still runs, for the reaction and learning.
        c = self.cfg.commentary
        flight_played = False
        plan = shot_plan = self.plan  # shot_plan survives the one-swing nulling below, for the reaction
        shot_plan_made_at = self._plan_made_at  # snapshot: the next player's pre-shot can rebind the live one
        if c.in_flight and not shot.is_putt and (shot.carry or 0) >= c.min_flight_carry and plan \
                and time.monotonic() - self._plan_made_at < 180:
            carry0 = self._raw_yards(shot)
            factor0 = self.carry.factor(plan.lie, carry0, self.current_course, shot.vla, "lm")
            picked = pick_line(plan, replace(shot, carry=carry0, units="Yards"), plan.to_pin, factor0, plan.elevation_yards)
            # The sim logs the splash only when the ball ARRIVES at the water, so a mid-flight check is
            # almost always still dry; this stays a cheap best-effort (no wait, so it never delays the
            # flight line). The reliable water call is the splash reaction, fired once the ball lands.
            if plan.lines.get("water") and self._shot_wet(plan.player, since=impact):
                picked = ("water", plan.lines["water"])  # already confirmed wet: tease the water
            if picked:
                outcome, text = picked
                self.stats["flight_planned"] += 1
                line = Line(speaker=self.speakers[0], text=text)
                self.state.record_lines([line])
                if outcome in plan.audio:
                    self.speech.say_prepared(line, plan.audio[outcome])
                else:
                    self.speech.say([line], interrupt=True, deadline=impact + self.cfg.tts.flight_audio_deadline_seconds)
                self._write_transcript([line], "flight")
                self.flight_latencies.append(time.monotonic() - impact)
                log.info("%s mid-flight call (%s) from the plan, audio %s, %.2fs after detection "
                         "[carry %.0f x %.2f, pin %s, elevation %s]: %r", tag, outcome,
                         "ready" if outcome in plan.audio else "voiced now", time.monotonic() - impact,
                         carry0, factor0, plan.to_pin, plan.elevation_yards, text)
                flight_played = True
                self.plan = None

        # At impact the player box still describes this shot (who, shot number, distance to the
        # pin), the minimap still shows the lie it was hit from, and CARRY (raw) has just appeared.
        before = event.impact_hud_hint or Hud()
        impact_image = event.impact_image
        if use_screen:
            read = getattr(event, "impact_read", None)
            before, impact_image = await (read if read is not None else self._read_impact_hud(tag))
            self._use_setup_lie(before, tag)
        player = self._player(before.player_text, event.player_hint)
        # GSPro's own club box beats guessing the club from loft.
        if before.club:
            shot = replace(shot, club=before.club)
        handed = before.handed or shot.handed
        if player and handed:
            self.handed[player] = handed
        unit = "metres" if (before.to_pin or "").endswith("metres") else "yards"

        # GSPro's own no-penalty carry beats the launch monitor's number when it's on screen.
        raw_now = _tile_yards(before.tiles, "carry (raw)", unit)
        game_now = _tile_yards(before.tiles, "carry (game)", unit)
        if raw_now and (game_now is None or game_now < raw_now * 0.5):  # fresh, not last shot's tiles
            base, base_carry = "gspro", raw_now
        else:
            base, base_carry = "lm", self._raw_yards(shot)
        projected = replace(shot, carry=base_carry, units="Yards")
        factor = self.carry.factor(before.lie, base_carry, self.current_course, shot.vla, base)
        if use_screen:
            log.info("%s impact HUD (read %.2fs): %r shot %s, %s to pin, lie %s, club %s, elevation %s; "
                     "carry %.1f yd from %s x %.3f for the lie", tag, self.screen.last_read_seconds if self.screen else 0,
                     before.player_text, before.shot_number, before.to_pin, before.lie, before.club,
                     before.elevation_yards, base_carry, "GSPro raw" if base == "gspro" else "launch monitor", factor)
        if self.screen and self.cfg.screen.save_review_images:
            await asyncio.to_thread(self._save_review_image, tag, "impact")

        flight_task = None
        plan, self.plan = self.plan, None  # a plan is good for one swing
        # Only the fallback here now: the fresh-plan line already played above, before this read.
        if not flight_played and c.in_flight and not shot.is_putt and (shot.carry or 0) >= c.min_flight_carry:
            fresh = plan and time.monotonic() - self._plan_made_at < 180 and plan.player in (None, player)
            picked = pick_line(plan, projected, before.to_pin, factor, before.elevation_yards) if fresh else None
            if fresh and self._shot_wet(plan.player, since=impact) and plan.lines.get("water"):
                picked = ("water", plan.lines["water"])  # the log already says it's wet: tease the water
            proj = project(projected, before.to_pin or (plan.to_pin if plan else None), plan.hazards if plan else [],
                           factor, before.elevation_yards)
            if proj:
                pin = to_yards(before.to_pin)
                log.info("%s projection: carry %.0f yd vs pin %s (elevation %s), drift %+.0f yd -> %s; plan %s",
                         tag, proj.carry, f"{pin:.0f} yd" if pin else "unknown", before.elevation_yards,
                         proj.lateral, proj.outcome, "ready" if fresh else ("stale" if plan else "none"))
            if picked:
                outcome, text = picked
                self.stats["flight_planned"] += 1
                log.info("%s mid-flight call (%s) from the plan, audio %s, %.2fs after detection: %r", tag, outcome,
                         "ready" if outcome in plan.audio else "voiced now", time.monotonic() - impact, text)
                line = Line(speaker=self.speakers[0], text=text)
                self.state.record_lines([line])
                if outcome in plan.audio:
                    self.speech.say_prepared(line, plan.audio[outcome])
                else:
                    self.speech.say([line], interrupt=True, deadline=impact + self.cfg.tts.flight_audio_deadline_seconds)
                self._write_transcript([line], "flight")
                self.flight_latencies.append(time.monotonic() - impact)
            else:
                flight_task = asyncio.create_task(
                    self._in_flight(projected, before, player, impact_image, impact, factor))

        # A ball in the water is a penalty with no resting lie to wait for, so SAY its splash NOW, off
        # the GSPro log's Hit_water (ingested by this point, after the impact read), instead of behind
        # the slow ball-follow. It does NOT return: the handler still runs the follow and records the
        # shot below (so the fairway streak resets and the stroke is counted); `splash_said` then skips
        # the reaction section. Falls through to the reaction-path water check if not wet yet here.
        splash_said = False
        if c.pre_reactions and not shot.is_putt and shot_plan and shot_plan.reactions.get("water") \
                and time.monotonic() - shot_plan_made_at < 180 and shot_plan.player in (None, player) \
                and self._shot_wet(player, since=impact):
            text = shot_plan.reactions["water"]
            line = Line(speaker=self.speakers[0], text=text)
            self.state.record_lines([line])
            audio = shot_plan.reaction_audio.get("water")
            if flight_played:
                self.speech.say([line], interrupt=False)  # let the just-played flight line finish first
            elif audio is not None:
                self.speech.say_prepared(line, audio)
            else:
                self.speech.say([line], interrupt=True)
            self._write_transcript([line], "reaction")
            self.stats["reaction_planned"] = self.stats.get("reaction_planned", 0) + 1
            splash_said = True
            log.info("%s water splash reaction (early, off Hit_water) %.1fs after impact: %r", tag,
                     time.monotonic() - impact, text)

        await self._read_putter(shot, fires0, impact, tag)
        after, landed = await self._follow_shot(before, tag, shot.is_putt) if ocr else (Hud(), event.hud_hint is not None)
        if use_screen:
            log.info("%s final reading %.1fs after impact (landed=%s): lie %s, carry %s, total %s, offline %s", tag,
                     time.monotonic() - impact, landed, after.lie, after.tiles.get("carry (game)"),
                     after.tiles.get("total length"), after.tiles.get("offline (raw)"))
        try:
            self._health_shot(before, after, shot.is_putt, ocr)
        except Exception:
            log.exception("Health shot count failed")
        hud = event.hud_hint or Hud(
            player_text=before.player_text, shot_number=before.shot_number, to_pin=before.to_pin,
            score_to_par=before.score_to_par, lie=after.lie, tiles=after.tiles,
        )
        player = player or self._player(hud.player_text, event.player_hint)
        unit = "metres" if (hud.to_pin or before.to_pin or "").endswith("metres") else "yards"

        def tile_yards(name: str) -> float | None:
            return _tile_yards(hud.tiles, name, unit)

        # Values read before the ball was seen to stop are partial; they'd teach the model nonsense.
        carry_game = None if shot.is_putt or not landed else tile_yards("carry (game)")
        carry_flat = None if shot.is_putt or not landed else tile_yards("carry (raw)")
        if carry_game:
            learn_base, learn_carry = ("gspro", carry_flat) if carry_flat else ("lm", self._raw_yards(shot))
            game = correct_for_elevation(carry_game, before.elevation_yards, to_yards(before.to_pin), learn_carry)
            self.carry.observe(before.lie, learn_carry, game, self.current_course, shot.vla, learn_base)
        if flight_task:
            try:
                await flight_task  # bounded by the flight deadline; keeps the call ahead of the reaction
            except Exception:   # a failed call mustn't cost the shot its record
                log.warning("%s mid-flight call failed", tag, exc_info=True)

        prior = self.state.player_shots(player or "unknown")
        score, tags = score_shot(shot, prior, hud)
        # A standout club delivery (big angle of attack, a real shape, a flush or a clear mishit) is
        # worth a word even when the result was ordinary, so it crosses the interest threshold and
        # gets a live reaction carrying the club facts below. Only for players with the markers.
        # Club delivery is spice: a notable one now and then, an ordinary one rarely, never on two
        # reactions in a row (every shot's face-to-path got old fast).
        notable_delivery = club_delivery_notable(shot)
        use_delivery = self._delivery_turn(notable_delivery)
        if notable_delivery and use_delivery:
            score += 0.5
            tags.append("club_notable")
        # The player's own longest/avg drive is a lifetime personal best (a long drive is a long drive,
        # league or practice). The LEAGUE record, though, must be league-only: a huge practice drive is
        # not "the league's best drive this season", so that bar reads from the league slice.
        season = self.store.season_stats(player) if player else {}
        season_facts = []
        # Drives are measured by total distance (carry and roll), as the season records are.
        drive = (tile_yards("total length") if landed else None) or carry_game or shot.carry
        # Only a drive off the tee counts against the driving records, never a putt or a fairway driver.
        tee_drive = ((shot.club or "").upper() == "DR" and not shot.is_putt
                     and (before.shot_number == 1 or (before.lie or "").lower() == "tee"
                          or (before.shot_number is None and not before.lie)))
        if tee_drive and drive and season.get("longest_drive"):
            if drive > season["longest_drive"]:
                tags.append("season_best_drive")
                score += 0.5
                season_facts.append(f"That beats {player}'s longest drive from earlier weeks "
                                    f"({season['longest_drive']:.0f} yards).")
            elif (best := self.store.season_longest_drive(context="league")) and drive > best[1]:
                tags.append("league_best_drive")
                score += 0.6
                season_facts.append(f"That's longer than the league's best drive this season ({best[0]}, {best[1]:.0f} yards).")
        if tee_drive and season.get("avg_drive"):
            season_facts.append(f"{player}'s average drive in earlier weeks: {season['avg_drive']:.0f} yards.")
        idx = self._gspro_idx(player)
        # No live "closest approach of the season" callout: a full shot's finishing distance isn't in
        # the log until its NEXT shot, so it can't be known at impact. The finish is recorded later
        # (_resolve_pending_finish) for the proximity stats.
        # Fairways in a row (live): a tee shot on a par 4/5 that finds the fairway extends the streak,
        # a miss resets it. Only called out once it's a run.
        if player and not self._anonymous(player) and not shot.is_putt and before.shot_number == 1 \
                and (shot_par or 4) >= 4:
            # A lie not read at rest (None) breaks the run too: better a missed streak than a false one.
            if (after.lie or "").lower() == "fairway":
                self._fir_streak[player] = self._fir_streak.get(player, 0) + 1
                if self._fir_streak[player] >= 3:
                    season_facts.append(f"That's {self._fir_streak[player]} fairways in a row for {player}.")
            else:
                self._fir_streak[player] = 0
        if self._anonymous(player):
            season_facts = []  # one player's records, on a ball both partners hit
        if shot.putter is None:
            await self._read_putter(shot, fires0, impact, tag, wait=0.0)  # the log may have caught up
        fire = self.gspro.fire_of(fires0, impact)
        played_from = self._played_from(before, shot, self.gspro.fire_info(fire),
                                        MATERIAL_TO_LIE.get(lies0.get(idx)) if idx is not None else None)
        if played_from == "green" and self._set_up_in_yards(before):
            played_from = self._off_green_lie(before)
            log.info("%s set up in yards: off the green, recorded as %s", tag, played_from)
        if played_from != before.lie:
            log.info("%s played from %s (the HUD read %s)", tag, played_from, before.lie)
            before = replace(before, lie=played_from)
        if not shot.is_putt and "in_water" not in tags and self._shot_wet(player, since=impact):
            tags = [*tags, "in_water"]   # the GSPro log saw the splash: the wrap can say where it went
        r = shot_round
        rec = ShotRecord(
            player or "unknown", shot, shot_hole, hud, tags,
            round_id=r.id if r else None, hole_seq=round_hole, par=shot_par,
            lie_before=before.lie, shot_number=before.shot_number, to_pin_before=before.to_pin,
        )
        self.state.record_shot(rec)
        if r and r.seq >= r.holes and before.lie == "green":
            self._putted_on_last_hole = True
        # Per-player stats from GSPro's log, attached to the player via their GSPro index (None on an
        # alternate-shot round, where the ball is shared and the log can't say which partner hit, so
        # those shots aren't attributed). finish_yards (how close the ball got) is set now for full
        # shots; a putt's finishing distance and holed/conceded outcome are filled in below. (idx was
        # resolved above for the closest-approach record.)
        fmt = r.format if r else None
        # Attribution is independent of the GSPro log: a shot is attributed to its player unless the
        # ball is shared (any one-ball team format, via _anonymous), where GSPro's name isn't who hit.
        # The log-only fields (finish/gimme distance) are gated separately on idx, so turning the log
        # off doesn't silently exclude every row from the stats.
        attributed = bool(player) and player != "unknown" and not self._anonymous(player)
        # The impact snapshot is where this player's ball sat going into this shot = the previous
        # shot's resting place, so it resolves the previous full shot's finishing distance (GSPro
        # doesn't log a shot's finish until the following shot is set up, and _follow_shot has since
        # advanced the live state, so a snapshot taken at THIS impact is the reliable source). Guarded
        # by shot order: a slow earlier handler (stuck _follow_shot) must not resolve/stash after a
        # later shot already did, which would scramble the "next setup = this rest" pairing.
        in_order = idx is not None and my_seq > self._finish_seq.get(idx, 0)
        if in_order:
            self._finish_seq[idx] = my_seq
            self._resolve_pending_finish(idx, impact_snap)
            self._correct_last_finish(idx, impact_snap, before.lie, shot_hole, before.shot_number, fmt)
        shot_id = self.store.record_shot(
            player=player, hole=shot_hole, shot=shot, before=before, after=hud, tags=tags,
            carry_game_yards=carry_game, total_yards=tile_yards("total length"), offline_yards=tile_yards("offline (raw)"),
            course=self.current_course, carry_raw_tile_yards=carry_flat,
            to_pin_yards=to_yards(before.to_pin),
            finish_yards=None,   # filled in on this player's next shot (see _resolve_pending_finish)
            gimme_yards=self.gspro.gimme_of(idx), fmt=fmt, attributed=attributed,
            is_league=bool(r and r.is_league),
        )
        rec.shot_id = shot_id
        # Not gated on the screen seeing touchdown: the finish comes from GSPro's log at the next shot,
        # and a water or OB ball is caught there by its penalty. (Gating on it lost half the finishes.)
        if in_order and not shot.is_putt:
            self._pending_finish[idx] = (shot_id, self._idx_snap(impact_snap, idx))
        if in_order:
            snap = self._idx_snap(impact_snap, idx)
            self._last_row[idx] = (shot_id, rec, snap["hole"], snap["pen"], snap["dropped"])
        if fire is not None:
            self._fire_rows[fire] = (shot_id, rec, idx, impact_snap[1].get(idx) if idx is not None else None)
            for old in [f for f in self._fire_rows if f <= fire - 60]:
                del self._fire_rows[old]
            self._apply_log_corrections()   # a mulligan logged before this re-hit was recorded
        replay_facts = self._replay_facts(player, rec)
        must = "mulligan" if replay_facts else None
        if replay_facts:
            score = max(score, c.interest_threshold)   # the mulligan gets its word
        if kick := await self._kick_facts(fire, hud.lie, shot.is_putt):
            score = max(score, c.interest_threshold)   # a bounce off the cart path gets its word
            replay_facts = replay_facts + kick
        self._shot_id = shot_id   # for record_lines linkage; the local guards against a concurrent task
        log.info("%s by %s: interest %.2f %s", tag, player or "unknown", score, tags)

        # Solo: the ball reaching the green on a full shot is the cue to pre-build the wrap (one-,
        # two- or three-putt finish), ready well before the putts end. In a group the trigger is the
        # scorecard instead (the second-to-last side holing out), handled in handle_scorecard.
        if r and len(r.sides) == 1 and not shot.is_putt and (hud.lie or "").lower() == "green" and before.shot_number:
            n = before.shot_number
            self._kick_wrap_branches(r.sides[0].key, [n + 1, n + 2, n + 3], [])
            if r.seq >= r.holes:  # last hole: also pre-build the round closer, branched by final score
                self._kick_closer_branches(r.sides[0].key, [n + 1, n + 2, n + 3])

        # Resolve a putt's result once, for EVERY putt, so the outcome, finishing distance and the
        # prediction grade are never skipped (a notable putt, a too-soon one, or a failed dice roll all
        # used to skip this). Prefer GSPro's log (holed vs conceded); the banner is the fallback, and
        # when it's the only signal an absent banner is 'unknown', not a miss, so a call isn't misgraded.
        holed = None
        close_feet = None
        roll_yards = tile_yards("total length")
        outcome = full_result = None
        if shot.is_putt and self.screen is not None and ocr:
            base_outs, base_gim = impact_snap[2].get(idx, 0), impact_snap[3].get(idx, 0)
            if idx is not None:
                # Log-truth: resolve from the log, no banner wait. Holed/conceded fire the instant the
                # log shows them; a miss the instant the stroke count advances (the ball settled and the
                # player is on to the next shot). The window is only the worst-case fallback now.
                base_shot = impact_snap[6].get(idx, self.gspro.shot_number(idx))  # baselined AT IMPACT
                base_hole = impact_snap[1].get(idx)
                base_setup = setups0.get(idx, 0)
                # A long putt with a gimme line can still be rolling when the usual window closes;
                # watch it to rest so its pre-written result plays the moment it stops.
                lag_plan = bool(c.pre_reactions and shot_plan and shot_plan.is_putt
                                and shot_plan.player in (None, player) and shot_plan.reactions.get("gimme"))
                window = max(c.hole_result_window_seconds, c.lag_result_window_seconds) if lag_plan \
                    else c.hole_result_window_seconds
                outcome = await self._wait_putt_result(idx, base_outs, base_gim, base_shot,
                                                       window, base_hole=base_hole,
                                                       base_setup=base_setup)
                if outcome == "unknown":
                    # The settle window passed with no log signal (holed/conceded/stroke-advance): a long
                    # miss, whose stroke only advances at the next setup. Commentary can't wait for it,
                    # but the record can: keep watching the log in the background, and grade the call
                    # there, rather than leave the putt with no outcome.
                    holed = None
                    log.info("%s putt outcome not in the log after %.1fs; watching for it in the background",
                             tag, window)
                    self._bg(self._late_result(shot_id, idx, base_outs, base_gim, base_shot, tag, True,
                                               self._pending_prediction.pop(player, None), shot_hole,
                                               base_hole=base_hole, base_setup=base_setup, rec=rec))
                else:
                    self._record_result(shot_id, outcome, tag, putt=True, rec=rec)
                    holed = outcome == "holed"
                    log.info("%s putt outcome from GSPro log: %s (%.1fs after impact)", tag, outcome,
                             time.monotonic() - impact)
                    if holed:
                        score = holed_putt_score(score, before, tags, c.interest_threshold,
                                                 c.big_moment_threshold)
            else:
                # No log attribution (practice/range mode, or an unmapped multiplayer slot): fall back to
                # the on-screen result banner. It only says the hole ENDED - a conceded miss inside the
                # gimme circle ends it too - so it's a holed HINT, not a definitive drop; no outcome written.
                self.last_hole_result = await self._hole_result(c.hole_result_window_seconds)
                holed = True if self.last_hole_result else None
                if self.last_hole_result:
                    log.info("%s hole result banner: %s (%.1fs after impact)", tag,
                             self.last_hole_result, time.monotonic() - impact)
            # Grade this player's pre-putt call, only when the log could attribute the result (the
            # banner can't distinguish a drop from a conceded miss).
            pending_prediction = self._pending_prediction.pop(player, None)
            if pending_prediction and idx is not None and outcome not in (None, "unknown") \
                    and pending_prediction[2] == shot_hole:
                commentator, predicted_make, _ = pending_prediction
                made = outcome == "holed"
                self.store.record_prediction(commentator, "putt", "make" if predicted_make else "miss",
                                             "made" if made else "missed", predicted_make == made)
            # The impact read caught the ball mid-roll; the banner window has let it settle, so re-read
            # the roll and keep whichever travelled further.
            settled = _tile_yards((await self.screen.read_hud_async(["tiles"])).tiles, "total length", unit)
            if settled is not None and (roll_yards is None or settled > roll_yards):
                roll_yards = settled

        elif not shot.is_putt and idx is not None and self._log_truth:
            # A full shot can end the hole: holed out, or stopped inside the gimme circle (the putt is
            # conceded, never struck). The log says so a second or two after the ball stops, so for a
            # ball that finished on or near the green, wait briefly: that's a moment worth a live call,
            # not the pre-written "on the green" line. Otherwise (or if the log is slow) keep watching
            # in the background so the record still gets it.
            base = (impact_snap[2].get(idx, 0), impact_snap[3].get(idx, 0),
                    impact_snap[6].get(idx, self.gspro.shot_number(idx)))
            near = (hud.lie or "").lower() in ("", "green", "fringe")
            base_hole = impact_snap[1].get(idx)
            base_setup = setups0.get(idx, 0)
            # A short chip or splash that drops often ends the follow before touchdown is seen (GSPro
            # moves straight on to the next hole), so it counts as near without `landed`, with a longer
            # window: the hole-out reaches the log a few seconds later. A chip that stays out resolves
            # as soon as GSPro sets the player up again, so the wait costs a miss almost nothing.
            short = (start_yards := to_yards(before.to_pin)) is not None and start_yards <= SHORT_GAME_YARDS
            watch = near and (landed or short)
            full_result = await self._wait_putt_result(idx, *base, 1.5 if landed else 5.0,
                                                       base_hole=base_hole, base_setup=base_setup) \
                if watch else "unknown"
            if full_result in ("holed", "conceded"):
                self._record_result(shot_id, full_result, tag, putt=False, rec=rec)
                log.info("%s full shot %s (GSPro log, %.1fs after impact)", tag,
                         "holed out" if full_result == "holed" else "finished inside the gimme circle",
                         time.monotonic() - impact)
                score = max(score, c.big_moment_threshold if full_result == "holed" else c.interest_threshold)
            elif full_result == "unknown":
                self._bg(self._late_result(shot_id, idx, *base, tag, False, rec=rec, base_hole=base_hole,
                                           base_setup=base_setup))
            if near and landed and start_yards and start_yards >= c.close_approach_from_yards:
                if full_result == "conceded":
                    score = max(score, c.big_moment_threshold)
                elif full_result != "holed":
                    finish = await self._wait_finish_yards(idx, impact_snap[0].get(idx), 1.5)
                    if finish is not None and finish * 3 <= c.close_approach_feet:
                        close_feet = finish * 3
                        score = max(score, c.big_moment_threshold)
                        log.info("%s stuffed it: %.0f feet from the hole from %.0f yards (GSPro log)",
                                 tag, close_feet, start_yards)

        # The early splash (above) already aired and was the call for this shot; the shot has now been
        # recorded, so skip every further reaction.
        if splash_said:
            return

        # A ball in the water gets its splash reaction instantly, from the plan, off the GSPro log's
        # Hit_water (logged at the splash, unlike the penalty score which only lands after the drop).
        # The ball never rests in the hazard, so this takes precedence over the lie-based pick below
        # (and over the live reaction): the splash IS the call. OB still has no such signal, so it's
        # told reliably on the RE-TEE instead (_penalty_facts_from_truth feeds the next pre-shot).
        # This is the fallback for a shot not yet seen wet at the early (pre-follow) check.
        if c.pre_reactions and not shot.is_putt and shot_plan and self._shot_wet(player, since=impact) \
                and shot_plan.reactions.get("water") and time.monotonic() - shot_plan_made_at < 180 \
                and shot_plan.player in (None, player):
            text = shot_plan.reactions["water"]
            line = Line(speaker=self.speakers[0], text=text)
            self.state.record_lines([line])
            audio = shot_plan.reaction_audio.get("water")
            if audio is not None:
                self.speech.say_prepared(line, audio)
            else:
                self.speech.say([line], interrupt=True)
            self._write_transcript([line], "reaction")
            self.stats["reaction_planned"] = self.stats.get("reaction_planned", 0) + 1
            log.info("%s water splash reaction from the plan, audio %s, %.1fs after impact: %r", tag,
                     "ready" if audio is not None else "voiced now", time.monotonic() - impact, text)
            return

        notable = score >= c.interest_threshold
        # An ordinary full shot is a quiet moment a prepared golf-world aside can follow, once its call
        # (if any) is done. Not a putt: the next putt, or the hole's wrap, is right behind it.
        self._news_moment = {"seq": my_seq, "tags": list(tags), "routine": not notable and not shot.is_putt}
        too_soon = time.time() - self.state.last_comment_at < c.min_seconds_between_comments
        # A makeable putt always gets a word (make or miss), and so does a long lag that stopped in the
        # gimme circle (its pre-voiced "gimme" line), skipping the routine dice but still yielding to
        # too_soon so it never talks over the last line.
        lag_gimme = bool(shot.is_putt and outcome == "conceded" and shot_plan
                         and shot_plan.player in (None, player) and shot_plan.reactions.get("gimme"))
        if not notable:
            if too_soon:
                return
            if self.rng.random() > c.routine_comment_rate and "makeable_putt" not in tags and not lag_gimme:
                return

        # Routine shots take the pre-written result line (voiced at setup, played now with no model
        # call), using the putt result resolved above. Notable moments still get the model reaction -
        # EXCEPT a holed putt, which always takes its instant pre-written "holed" line: a made putt is
        # the worst thing to react to late or miss, and a live call here can lag or drop.
        # An "unknown" putt (no log signal in the window) must NOT take the pre-written line: with no
        # result we can't say short vs holed, so fall through to the grounded live reaction instead.
        if c.pre_reactions and outcome != "unknown" and (not notable or (shot.is_putt and holed) or lag_gimme) \
                and shot_plan \
                and time.monotonic() - shot_plan_made_at < 180 \
                and shot_plan.player in (None, player) and shot_plan.is_putt == shot.is_putt:
            picked = pick_reaction(shot_plan, after_lie=hud.lie, start_yards=to_yards(before.to_pin),
                                   roll_yards=roll_yards, holed=holed, conceded=outcome == "conceded")
            if picked:
                outcome, text = picked
                line = Line(speaker=self.speakers[0], text=text)
                self.state.record_lines([line])
                audio = shot_plan.reaction_audio.get(outcome)
                if audio is not None:
                    self.speech.say_prepared(line, audio)
                else:
                    self.speech.say([line], interrupt=True)
                self._write_transcript([line], "reaction")
                self.stats["reaction_planned"] = self.stats.get("reaction_planned", 0) + 1
                log.info("%s result call (%s) from the plan, audio %s, %.1fs after impact: %r", tag, outcome,
                         "ready" if audio is not None else "voiced now", time.monotonic() - impact, text)
                return

        units = " metres" if (hud.to_pin or "").endswith("metres") else " yards"
        lead = [f"Before this shot the pin was {hud.to_pin} away."] if hud.to_pin else []
        if hud.shot_number:
            lead.append(f"This is shot {hud.shot_number} on this hole.")
        # The HUD can't tell us whether a putt dropped, and "on the green" plus the roll length
        # just got misread as distance left. Pace, line and the starting distance are enough.
        result = [] if shot.is_putt else describe(hud, units)
        if shot.is_putt and outcome == "conceded":
            result = ["The putt didn't drop, but it stopped inside the gimme circle: the next one is conceded, "
                      "so the hole is over. No more putting is needed."]
            if (miss := _short_miss_fact(before, shot_par)):
                result.append(miss)
        if full_result == "holed":
            result = ["The ball went in the hole: holed out without a putt."]
        elif full_result == "conceded":
            gimme = self.gspro.gimme_of(idx)
            circle = f"{gimme * 3:.0f}-foot " if gimme else ""
            result = [f"The ball stopped inside the {circle}gimme circle, right by the hole: the putt is "
                      f"conceded, so the hole is over without a putt being struck."]
        # On the green in regulation or better: say what the next putt is for, so "ten feet for
        # birdie" is a fact, not a guess (a par 5 in two is an eagle putt).
        next_putt = None
        if before.shot_number and shot_par and (name := SCORE_NAMES.get(before.shot_number + 1 - shot_par)) \
                and before.shot_number + 1 - shot_par <= -1:
            next_putt = (f"The ball is on the green in {before.shot_number}: the next putt "
                         f"is for {name.split(' ', 1)[1]}.")
        if close_feet is not None:
            result = result + [f"The ball finished {close_feet:.0f} feet from the hole, from {to_yards(before.to_pin):.0f} "
                               f"yards out: a superb approach, stuffed close. This is the moment to make a big deal of."]
        elif not shot.is_putt and full_result not in ("holed", "conceded"):
            # GSPro's own finishing distance (logged as the ball settles), so the model never reads the
            # starting distance as what's left: "still 102 yards" after a pitch to 8 yards.
            left = None
            if idx is not None and self._log_truth:
                left = await self._wait_finish_yards(idx, impact_snap[0].get(idx), 1.0)
            if left is not None:
                on_green = (hud.lie or "").lower() in ("green", "fringe")
                where = f"{left * 3:.0f} feet" if on_green or left < 10 else f"{left:.0f} yards"
                result = result + [f"After this shot the ball is {where} from the pin."]
            else:
                result = result + ["How far the ball is from the pin now is not known: don't state a distance "
                                   "left, and don't repeat the starting distance as if it were."]
        if next_putt and not shot.is_putt and full_result not in ("holed", "conceded") and \
                (close_feet is not None or (hud.lie or "").lower() == "green"):
            result = result + [next_putt]
        # No in-the-moment "distance from the pin": the only estimate available (start distance minus
        # how far the ball travelled, from a tile that can read stale) was unreliable enough to call a
        # ball in a greenside bunker "three yards, beautifully done". The lie (describe) is read
        # accurately; crediting a stuffed shot belongs on the next setup's exact distance instead.
        flight = shot_facts(shot)
        if "carry (game)" in hud.tiles and not shot.is_putt:
            # GSPro's carry accounts for elevation and conditions; two different carries confuse the model.
            flight = [f for f in flight if not f.startswith("Carry:")]
        # Club delivery (angle of attack, face, path) is colour, not every-shot material - on every
        # shot it gets repetitive. Feed it only when the delivery is actually standout, or now and then
        # on an ordinary one, so it stays a spice.
        club = club_delivery_facts(shot) if use_delivery else []
        self._delivery_last = bool(club)
        if not shot.is_putt:
            club += self._length_norm_facts(
                player, shot.club, self._is_drive(before), to_yards(before.to_pin),
                tile_yards("total length") if landed and "total length" in hud.tiles else None)
        sand_theme = (self._bunker_facts(player) if (hud.lie or "").lower() in ("sand", "bunker")
                      and not shot.is_putt else [])
        facts = (lead + replay_facts + flight + club + result + sand_theme
                 + history_facts(self._display(player), prior) + season_facts
                 + self._format_facts() + (self._ctp_facts(1) if hud.shot_number == 1 else []))
        who, team = self._who(player)
        user, facts_text = shot_prompt(
            player=who,
            team=team,
            facts=facts,
            hole=shot_hole,
            recent_lines=self.state.recent_lines,
            routine=not notable,
            has_images=False,
            angle=self.rng.choice(ANGLES),
            big=score >= c.big_moment_threshold,
        )
        await self._speak("shot", self.live_llm, user, facts_text, [], must=must, shot_id=shot_id)

    @staticmethod
    def _idx_snap(snap: tuple, idx: int) -> dict:
        """One player's scalars out of an impact snapshot (to_pin_by, hole_by, hole_outs_by,
        gimmies_by, _penalties, current_hole, shot_number_by[, dropped_by]). The hole falls back to the
        snapshot's own current_hole (not live state), so stash and resolve, seconds apart, compare the
        same frozen value. to_pin stays raw metres (converted to yards where it's used)."""
        to_pin, hole, outs, gim, pen, cur_hole, _shots, *rest = snap
        h = hole.get(idx, cur_hole)
        return {"to_pin_m": to_pin.get(idx), "hole": h, "outs": outs.get(idx, 0),
                "gim": gim.get(idx, 0), "pen": pen.get((idx, h), 0),
                "dropped": rest[0].get(idx, 0) if rest else 0}

    def _resolve_pending_finish(self, idx: int | None, after_snap: tuple) -> None:
        """Fill in the previous full shot's finishing distance using the CURRENT shot's impact snapshot
        (where that ball came to rest). A shot that holed finishes at 0; one that ended inside the gimme
        circle finishes within the gimme radius; a shot that found water/OB has no finish. The snapshot
        is used rather than live state because _follow_shot has since advanced the log another shot."""
        if idx is None or idx not in self._pending_finish:
            return
        prev_id, before = self._pending_finish.pop(idx)
        after = self._idx_snap(after_snap, idx)
        if after["dropped"] > before["dropped"]:
            return   # a scramble picked another ball: this one's resting spot was never logged
        if before["hole"] is None:
            return   # the shot's hole was unknown (log not tailed yet); can't compare, so don't guess
        if after["hole"] != before["hole"]:              # the shot ended the hole
            if after["outs"] > before["outs"]:
                self.store.set_shot_result(prev_id, finish_yards=0.0)                  # holed
            elif after["gim"] > before["gim"]:
                self.store.set_shot_result(prev_id, finish_yards=(self.gspro.gimme_of(idx) or 1.3))  # conceded
            return
        if after["pen"] > before["pen"]:   # same hole, a penalty on this shot: the next setup is the drop
            return
        if after["to_pin_m"] is not None:
            self.store.set_shot_result(prev_id, finish_yards=after["to_pin_m"] * M_TO_YD)

    async def _hole_result(self, timeout: float, poll: float = 0.3) -> str | None:
        """Poll GSPro's result banner until it shows or the window closes. It appears about a second
        after the ball settles, during the hole-end transition, and stays up ~2s, so a single read
        right after the putt would catch the dark fade and miss it."""
        deadline = time.monotonic() + timeout
        while True:
            banner = (await self.screen.read_hud_async(["score_banner"])).hole_result
            if banner:
                return banner
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(poll)

    async def _read_putter(self, shot: Shot, base_fires: int, impact: float, tag: str = "",
                           wait: float = 1.5) -> None:
        """Take GSPro's own putt/full-shot call for this shot from its log (the putter selected or
        not), overriding the speed-and-launch guess: a chip off the green is a full shot and a putt
        from the fringe is a putt. Waits briefly (up to `wait` after impact) for the line to be
        tailed; does nothing on a GSPro that doesn't log the mode, so it never slows those down."""
        g = self.gspro
        if not g.logs_launch_mode:
            return
        deadline = max(impact + wait, time.monotonic() + (0.2 if wait else 0.0))
        while (mode := g.putting_mode(base_fires, impact)) is None and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        if mode is None:
            return
        if mode != shot.is_putt:
            log.info("%s GSPro played it as %s (the launch data alone said otherwise)", tag,
                     "a putt" if mode else "a full shot")
        shot.putter = mode
        if mode:
            # GSPro played it with the putter, whatever the club box read (a late read can catch the
            # next hole's tee and say "DR").
            shot.club = "PT"

    # Surfaces GSPro slows the ball from, logging a "Penalty Surface" line when a shot is struck off
    # one. Semi-rough is left out: GSPro often doesn't log it.
    _PENALIZING_LIES = ("rough", "deep rough", "sand", "bunker")

    async def _air_setup_when_idle(self, good: list, seq: int, limit: float, aired) -> None:
        """Air a setup line once the booth stops talking. The last shot's reaction is often still
        playing when a putt is set up (an eagle putt went unmentioned that way). The line is voiced
        while that plays, so it goes the moment the booth is quiet; it's dropped if the ball is struck
        first or the wait runs out."""
        deadline = time.monotonic() + limit
        voicing = asyncio.create_task(self.speech.prepare_exchange(good))
        while not self.speech.is_idle() and time.monotonic() < deadline and self._shot_seq == seq:
            await asyncio.sleep(0.1)
        if not (self.speech.is_idle() and self._shot_seq == seq):
            voicing.cancel()
            log.info("Setup line dropped: %s", "the ball was struck" if self._shot_seq != seq else "the booth stayed busy")
            return
        try:
            kept, audio = await asyncio.wait_for(voicing, max(0.0, deadline - time.monotonic()))
        except asyncio.TimeoutError:
            log.info("Setup line dropped: its voice wasn't ready in time")
            return
        if kept and self.speech.is_idle() and self._shot_seq == seq:
            self.speech.say_prepared_sequence(list(zip(kept, audio)))
            aired(kept)

    async def _kick_facts(self, fire: int | None, lie: str | None, is_putt: bool, wait: float = 0.5) -> list[str]:
        """A fact when the ball came down on the cart path (or a man-made structure) and bounced on to
        rest somewhere else. GSPro logs the landing surface as the ball settles, so give it a moment."""
        if is_putt or fire is None or not lie:
            return []
        deadline = time.monotonic() + wait
        while "landed" not in self.gspro.fire_info(fire) and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        landed = MATERIAL_TO_LIE.get(self.gspro.fire_info(fire).get("landed") or "")
        if landed not in ("cart path", "structure") or landed == lie.lower():
            return []
        what = "the cart path" if landed == "cart path" else "something man-made (a bridge or wall)"
        return [f"The ball landed on {what} and kicked off it, finishing in the {lie}. That bounce is "
                f"worth a word."]

    def _use_setup_lie(self, before: Hud, tag: str = "") -> None:
        """The lie this stroke's setup read, while the player stood over the ball, over the impact
        read's: that comes ~3 s after the swing, when the lie box may show where the ball landed."""
        if (setup_lie := self._setup_lies.get(_setup_key(before))) and setup_lie != before.lie:
            log.info("%s lie %s at setup (the impact read, after the swing, said %s)", tag, setup_lie, before.lie)
            before.lie = setup_lie

    def _set_up_in_yards(self, before: Hud) -> bool:
        """Whether this stroke was set up off the green: GSPro shows the distance in yards there and in
        feet on the green. Only that way round: switching to putt mode off the green shows feet too."""
        key = _setup_key(before)
        return bool(key and ("yard" in key[2] or self._setup_yards.get(key[:2])))

    def _off_green_lie(self, before: Hud) -> str:
        """The lie for a stroke set up off the green that the reads put on it: the HUD's, when it's a
        lie GSPro played it from (no rough or sand without GSPro's penalty for it), else the fringe."""
        return before.lie if before.lie not in (None, "", "green", *self._PENALIZING_LIES) else "fringe"

    def _played_from(self, before: Hud, shot: Shot, info: dict, material: str | None) -> str | None:
        """The lie this shot was really struck from, for the record. The HUD's lie read and the log's
        material (where the previous shot LANDED, not where it stopped) both misread: a ball that
        landed on the green and rolled into a bunker, a bunker ball shown as rough. GSPro's own word
        wins: the first stroke on a hole is off the tee, and a shot struck from a surface that costs it
        speed or spin is logged with that surface. When this GSPro logs those and logged none for this
        shot, it wasn't played from rough or sand, whatever the other reads said."""
        if before.shot_number == 1:
            return "tee"
        if pen := info.get("penalty"):
            return MATERIAL_TO_LIE.get(pen, before.lie)
        if not (info and self.gspro.logs_penalty_surface):
            return before.lie
        if before.lie == "green" and shot.is_putt:
            # The material is where the last ball first landed: short of the green, for one that ran on.
            # (Only for a putt: a full shot off a "green" read is more likely the fringe, misread.)
            return "green"
        if material == "green" and before.lie not in (None, "", "green", "tee", "water", *self._PENALIZING_LIES):
            # The reverse: it landed on the green and ran off. The setup read saw where it stopped.
            return before.lie
        for lie in (material, before.lie):
            if lie and lie not in self._PENALIZING_LIES and lie not in ("tee", "water"):
                return lie
        return "green" if shot.is_putt else "fairway"

    def _hole_on_card(self, hud: Hud) -> int | None:
        """Which course hole a new banner is, checked against the course card's pars during a round:
        the next hole, or the hole the banner's own number or GSPro's log names (a skipped hole),
        whichever has the banner's par. 0 when none does: a misread ("par 3, 58 yards" for a par 3,
        158 yards hole whose next is a par 5), not a new hole. None when there's nothing to check
        against (no round, no card, the starting hole not known yet)."""
        r, card = self.round, self.round and self.round.course_card
        if not r or r.ended or r.start_hole_assumed or not card or not card.par or not hud.par \
                or self.hole_number is None or self.hole_number + 1 not in card.par:
            return None   # (past the card's last hole: the round-end checks have that)
        g = self.gspro
        logged = g.current_hole + 1 if g is not None and g.current_hole is not None else None
        for n in (self.hole_number + 1, hud.hole_number, logged):
            if n and n > self.hole_number and card.par.get(n) == hud.par:
                return n
        return 0

    def _log_hole_disagrees(self, hud: Hud) -> int | None:
        """The hole GSPro's log has the player on, when it isn't the hole the booth counted. Only once the
        player has a stroke on this hole (the log's hole lags at the tee until the first one), only when
        the log's stroke count matches the HUD's (so the log line is this hole's), and only for a hole in
        the round whose par, on the card, is the banner's."""
        r = self.round
        if not r or r.ended or r.start_hole_assumed or self.hole_number is None or not hud.shot_number \
                or hud.shot_number < 2:
            return None
        raw = re.split(r"\s+(?:E|[+-]\d+|Shot)\b", (hud.player_text or "").strip(), maxsplit=1)[0].strip()
        idx = self._gspro_idx(self._player(hud.player_text, raw or None))
        g = self.gspro
        if idx is None or g.hole_by.get(idx) is None or g.shot_number(idx) != hud.shot_number:
            return None
        logged = g.hole_by[idx] + 1
        if logged == self.hole_number or not r.start_hole <= logged < r.start_hole + r.holes:
            return None
        card = r.course_card
        if card and card.par and hud.par and card.par.get(logged) != hud.par:
            return None
        log.info("GSPro's log has %s on hole %s, stroke %s; the booth counted hole %s",
                 raw, logged, hud.shot_number, self.hole_number)
        return logged

    async def _correct_hole(self, event: HoleChangeEvent) -> None:
        """Puts the round back on the right hole without a recap: undoes a misread hole change, or moves
        to the hole GSPro's log names. Shots already recorded on the wrong hole move with it."""
        wrong, r = self.state.current_hole, self.round
        self.state.current_hole = event.current
        if m := re.search(r"par (\d)", event.current, re.IGNORECASE):
            self.current_par = int(m.group(1))
        log.info("Hole corrected (%s): %s -> %s", event.correction, wrong, event.current)
        if not r or r.ended:
            return
        old_seq = r.seq
        if not r.start_hole_assumed and (m := re.match(r"Hole (\d+)", event.current)):
            new_seq = int(m.group(1)) - r.start_hole + 1
        elif event.correction == "revert":
            new_seq = old_seq - 1   # the misread counted one hole
        else:
            return
        if not 1 <= new_seq <= r.holes or new_seq == old_seq:
            return
        await self._drain_shot_tasks(self.cfg.commentary.wrap_wait_seconds)   # shots in flight record first
        r.seq = new_seq
        for seq in [s for s in r.par if s > new_seq]:
            del r.par[seq]   # set by the wrong count; set again when play gets there
        if self.current_par:
            r.par[new_seq] = self.current_par
        moved = [x for x in self.state.shots if x.round_id == r.id and x.hole_seq == old_seq and x.hole == wrong]
        for x in moved:
            x.hole_seq, x.hole = new_seq, event.current
        ids = [x.shot_id for x in moved if x.shot_id is not None]
        if wrong and ids:
            self.store.relabel_hole(wrong, event.current, min(ids))
        log.info("Round %s: hole %s of %s (was %s); %s shots moved", r.id, r.seq, r.holes, old_seq, len(moved))

    def _sync_scramble(self) -> None:
        """GSPro's log shows a scramble once it copies the first picked ball onto a teammate (after
        everyone's first tee shot). From then the round is a scramble: the format, the teams the copies
        tied together, and every shot already recorded this round."""
        g, r = self.gspro, self.round
        if not g.teammates or r is None or r.ended or r.no_scramble or self._scramble_pairs == g.teammates:
            return
        self._scramble_pairs = set(g.teammates)
        groups: list[set[int]] = []
        for pair in g.teammates:
            hit = [grp for grp in groups if grp & pair]
            merged = set(pair).union(*hit)
            groups = [grp for grp in groups if grp not in hit] + [merged]
        order = r.slot_names or (list(r.roster) if len(r.roster) == len(g.players_seen) else [])
        names = [[order[i] for i in sorted(grp) if i < len(order) and order[i]] for grp in groups]
        was = r.format
        if r.play_scramble([n for n in names if len(n) > 1]):
            ids = [x.shot_id for x in self.state.shots if x.round_id == r.id and x.shot_id is not None]
            if was != "scramble":
                self.store.set_format(ids, "scramble")
            log.info("GSPro's log shows a scramble (was %s): teams %s; %s earlier shots relabelled",
                     was, [s.label for s in r.sides], 0 if was == "scramble" else len(ids))

    def _pick_facts(self) -> list[str]:
        """Whose ball the team is playing, from GSPro's last scramble pick on this hole."""
        g, r = self.gspro, self.round
        if not r or r.ended or r.format != "scramble" or not g.pick:
            return []
        idx, hole = g.pick
        order = r.slot_names or list(r.roster)
        if hole != g.current_hole or idx >= len(order) or not order[idx]:
            return []
        name = r.league.match_player(order[idx]) or order[idx]
        return [f"The team picked {name}'s ball from the last round of shots; everyone plays from there."]

    def _correct_last_finish(self, idx: int, snap: tuple, lie: str | None, hole_label: str | None = None,
                             shot_number: int | None = None, fmt: str | None = None) -> None:
        """Where the player's previous shot came to rest is where this one was played from. Corrects
        that shot's resting lie (the HUD's read at rest misses, e.g. a tee shot onto the green read as
        fairway) when it's the same hole, no penalty came between (a drop), and it wasn't taken back."""
        prev = self._last_row.get(idx)
        if not prev or not lie or lie == "tee":
            return
        shot_id, rec, hole, pen, dropped = prev
        now = self._idx_snap(snap, idx)
        same = hole is not None and now["hole"] == hole and now["pen"] == pen
        # GSPro's log moves to a new hole only once its tee shot is logged, so a tee shot's snapshot
        # still names the hole before. The HUD's strokes (1, then 2: no penalty between) on the same
        # hole say it's this ball. Not in a scramble, where the second shot may be a partner's ball.
        tee_then_second = (rec.shot_number == 1 and shot_number == 2 and hole_label is not None
                           and _hole_key(rec.hole) == _hole_key(hole_label) and now["pen"] == 0
                           and fmt != "scramble")
        if rec.mulligan or not (same or tee_then_second) or now["dropped"] != dropped:
            return   # (a scramble that picked another ball: this shot is played from the team's ball)
        if (rec.hud.lie or "").lower() != lie:
            log.info("Shot %s finished on the %s (the HUD read %s)", shot_id, lie, rec.hud.lie)
            rec.hud = replace(rec.hud, lie=lie)
            self.store.set_lie_after(shot_id, lie)

    def _apply_log_corrections(self) -> None:
        """Apply what GSPro's log has since said about recorded shots: strokes it took back
        (mulligans), flagged so they leave the stats, and holes picked up (scored at the maximum)."""
        g, r = self.gspro, self.round
        self._sync_scramble()
        # A score line that merely repeats is a mulligan only when each player has their own ball and
        # score; GSPro's shared-ball formats repeat it for other reasons. Best ball is still one ball each.
        team = r is not None and r.format in ("scramble", "alternate")
        for fire, kind in list(g.mulligan_fires.items()):
            if fire in self._mulligans_done or fire not in self._fire_rows:
                continue
            self._mulligans_done.add(fire)
            if kind == "repeat" and team:
                continue
            shot_id, rec, idx, hole = self._fire_rows[fire]
            rec.mulligan = True
            self.store.set_mulligan(shot_id)
            # No conceded-putt row to flag with it: a shot that ends in the gimme circle ends the hole,
            # and GSPro offers no shot options (so no mulligan) after that.
            # The re-hit replaces it: the same stroke, from the same spot.
            nxt = next((v for f, v in sorted(self._fire_rows.items()) if f > fire and v[2] == idx), None)
            if nxt and nxt[3] == hole and nxt[1].round_id == rec.round_id:
                nxt[1].shot_number, nxt[1].lie_before = rec.shot_number, rec.lie_before
                self.store.set_shot_start(nxt[0], rec.shot_number, rec.lie_before)
            log.info("Shot %s was a mulligan (GSPro took it back): left out of the stats", shot_id)
        while self._pickups_seen < len(g.pickups):
            idx, hole = g.pickups[self._pickups_seen]
            self._pickups_seen += 1
            row = next((v for v in reversed(self._fire_rows.values()) if v[2] == idx and v[3] == hole), None)
            if r and row and row[1].round_id == r.id and row[1].hole_seq:
                r.picked_up.add((row[1].player, row[1].hole_seq))
                log.info("%s picked up on hole %s of the round: scored at the maximum", row[1].player,
                         row[1].hole_seq)
        # A scramble pick inside the gimme circle concedes the picked ball's stroke. Its player usually
        # hit before a teammate, so that stroke's watch had already settled (as a miss) when GSPro
        # picked it, and the team's conceded putt went unrecorded.
        while self._pick_gimmes_seen < len(g.pick_gimmes):
            idx, hole = g.pick_gimmes[self._pick_gimmes_seen]
            self._pick_gimmes_seen += 1
            if not r or r.format != "scramble":
                continue
            row = next((v for v in reversed(self._fire_rows.values())
                        if v[2] == idx and v[3] == hole and not v[1].mulligan), None)
            if row and row[0] is not None and row[1].round_id == r.id and row[1].outcome in (None, "missed"):
                self._record_result(row[0], "conceded", f"[shot {row[0]}] (scramble pick)",
                                    putt=row[1].shot.is_putt, rec=row[1])

    def _record_result(self, shot_id: int | None, outcome: str, tag: str, putt: bool,
                       rec: ShotRecord | None = None) -> None:
        """Store how a stroke ended. A putt keeps its outcome, and so does a full shot that holed out;
        any stroke that ended inside the gimme circle also gets a row for the conceded putt GSPro
        counts but nobody hits, so putting stats (putts per hole, three-putts) see every putt on the
        card. A full shot that ended inside the circle finished on the green, whatever the HUD read."""
        if shot_id in self._conceded_ids:
            return   # a scramble pick already conceded it; a later watch can't undo or repeat that
        if rec is not None:
            rec.outcome = outcome
        if putt or outcome == "holed":
            self.store.set_shot_result(shot_id, outcome=outcome)
        if outcome == "conceded" and shot_id is not None:
            self._conceded_ids.add(shot_id)
            self.store.record_gimme(shot_id)
            log.info("%s conceded putt recorded", tag)
            if not putt:
                self.store.set_lie_after(shot_id, "green")
                if rec is not None and rec.hud.lie != "green":
                    rec.hud = replace(rec.hud, lie="green")

    async def _late_result(self, shot_id: int | None, idx: int, base_outs: int, base_gim: int, base_shot: int,
                           tag: str, putt: bool, prediction: tuple | None = None, hole: str | None = None,
                           window: float = 60.0, rec: ShotRecord | None = None,
                           base_hole: int | None = None, base_setup: int | None = None) -> None:
        """Background: keep watching the log for how a stroke ended after commentary has moved on. A
        long missed putt only shows in the log when the next one is set up, well past the reaction
        window; a full shot can come to rest inside the gimme circle. Gives up after `window`."""
        try:
            outcome = await self._wait_putt_result(idx, base_outs, base_gim, base_shot, window, poll=0.5,
                                                   base_hole=base_hole, base_setup=base_setup)
            if outcome == "unknown" or (not putt and outcome == "missed"):
                return
            self._record_result(shot_id, outcome, tag, putt, rec)
            if putt:
                log.info("%s putt outcome from GSPro log (late): %s", tag, outcome)
            if prediction and prediction[2] == hole:
                commentator, predicted_make, _ = prediction
                made = outcome == "holed"
                self.store.record_prediction(commentator, "putt", "make" if predicted_make else "miss",
                                             "made" if made else "missed", predicted_make == made)
        except Exception:
            log.exception("%s late result watch failed", tag)

    async def _wait_finish_yards(self, idx: int, at_impact: float | None, window: float,
                                 poll: float = 0.1) -> float | None:
        """Where the ball came to rest, in yards from the pin, from GSPro's `Logging tdist` line
        (written as the ball settles, just after the score line). None if it doesn't change from the
        impact value within the window, so a stale distance is never taken for this shot's."""
        deadline = time.monotonic() + window
        while True:
            now = self.gspro.to_pin_by.get(idx)
            if now is not None and now != at_impact:
                return self.gspro.to_pin_of(idx)
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(poll)

    async def _wait_putt_result(self, idx: int, base_outs: int, base_gim: int, base_shot: int,
                                window: float, poll: float = 0.2, base_hole: int | None = None,
                                base_setup: int | None = None, setup_grace: float = 0.3,
                                no_setup_after: float = 2.0) -> str:
        """Resolve a stroke from the GSPro log, no banner wait: 'holed' (a hole-out registered),
        'conceded' (a gimmie registered), or 'missed'. Returns 'unknown' when the window closes without
        a result, so the caller doesn't claim a definitive miss it never saw.

        `base_setup` is the player's setup count at impact. GSPro sets the shooter up again the moment
        the ball stops, and logs any gimmie right after that setup, so that first setup settles THIS
        stroke: a gimmie with it is this stroke's, none means it missed (after `setup_grace`, as the
        gimmie line can land a read later). Anything after belongs to a later stroke. Going by the
        stroke count alone got both wrong: a putt's own score line advances it a read before its gimmie
        lands (a conceded putt stored as a miss), and a chip whose score line didn't move (GSPro took
        it back) kept watching and claimed the next putt's gimmie. Without a setup count (or if no
        setup follows within `no_setup_after` of the count advancing) the advance is the miss signal.
        `base_hole` is the player's hole in the log at impact. On a tee shot the log can still be on
        the last hole (stroke 6 there), and the new hole's count restarts below that, so the player's
        hole moving on counts as the stroke being taken as well; without it a tee shot's watch ran on
        and claimed the gimme the player's next stroke earned."""
        g = self.gspro
        deadline = time.monotonic() + window
        setups = getattr(g, "setups_by", None) if base_setup is not None else None
        settled_at = advanced_at = None
        while True:
            now = time.monotonic()
            if g.hole_outs_of(idx) > base_outs:
                return "holed"
            if setups is None or setups.get(idx, 0) > base_setup:
                if g.gimmies_of(idx) > base_gim:
                    return "conceded"
                if setups is not None:
                    settled_at = settled_at or now
                    if now - settled_at >= setup_grace:
                        return "missed"
            if g.shot_number(idx) > base_shot:  # stroke advanced: this stroke counted, next one is up
                if setups is None:
                    return "missed"
                advanced_at = advanced_at or now
                if now - advanced_at >= no_setup_after:
                    return "missed"
            if base_hole is not None and g.hole_by.get(idx, base_hole) != base_hole and g.shot_number(idx) > 1:
                return "missed"
            if now >= deadline:
                return "unknown"  # window passed with no log signal: not a definitive miss
            await asyncio.sleep(poll)

    async def _follow_shot(self, before: Hud, tag: str = "", is_putt: bool = False) -> tuple[Hud, bool]:
        """Follows the ball through the HUD from impact until it stops.

        Checked on gameplay video: CARRY (game) counts up and freezes at touchdown (the lie flips
        to where it LANDED), Total Length keeps counting while the ball rolls and then holds, and
        1-2 s later GSPro blanks the HUD and moves the player box on to the next shot.

        We follow past touchdown until the ball comes to rest (total length holds steady) and read
        the lie THERE, so the reaction uses the resting lie, not the landing lie: a ball that lands
        on the fairway and releases into the rough reads "rough", as it should. Touchdown is still
        where CARRY (game) is final, so carry learning is unaffected. Returns the last read that
        still belonged to this shot (never the next player's, via _moved_on), and whether it stopped.

        Putts don't drive those tiles, so their stop can never be detected this way; the loop would
        just burn an OCR read every poll for the whole cap, learn nothing, and starve the next shot's
        reads (the main cause of late commentary and audio jitter late in a hole). A putt's result
        comes from the score banner in the reaction path, not these tiles, so skip the follow for it.
        """
        if is_putt:
            return before, False
        poll = max(self.cfg.screen.follow_poll_seconds, 0.05)
        max_seconds = self.cfg.screen.follow_max_seconds
        # CARRY (game) as it read at impact: the previous shot's frozen value if its tiles are still on
        # screen, else this shot's starting value. A freeze that DIFFERS from it means the tile has
        # turned over to this shot (so a short chip already frozen at the first follow read still lands);
        # a freeze EQUAL to it is the stale previous value and must not count as touchdown.
        game_baseline = _tile_yards(before.tiles, "carry (game)", "yards")
        # Who's up at impact (top row of the player box), as a pixel signature. GSPro keeps the last
        # shot's tiles on screen after it moves to the next player (measured on video: ball at rest,
        # ~1.3s, fade, then the next player's lie under the SAME tiles), so the tiles can't tell us the
        # switch happened; the name cell can. A dark cell (the fade) counts as switched too.
        who = before.name_sig if before.name_sig not in (None, NAME_DARK) else None
        last, landing, prev_total, moving, steady = None, None, None, False, 0
        started, prev_game, touchdown, rose = time.monotonic(), None, None, False
        reads, mine = 0, False   # mine: the tiles have been seen holding this shot's values
        # Bound by wall-clock, not iteration count: OCR can be slow, and a slow follow blocks the
        # next shot's reaction in the queue (a stuck follow ran two minutes once).
        # Poll `lie`, the `player_name` cell (a pixel compare, no OCR) and `tiles`, in that order: the
        # name is grabbed AFTER the lie, so an unchanged name proves the lie was captured before any
        # switch to the next player.
        deadline = max_seconds
        while time.monotonic() - started < deadline:
            reads += 1
            hud = await self.screen.read_hud_async(["lie", "player_name", "tiles"])
            if who is None and hud.name_sig not in (None, NAME_DARK):
                who = hud.name_sig
            elif name_sig_changed(who, hud.name_sig):
                log.info("%s player box switched after %s reads (%.1fs); using the last this-shot read (lie %s)",
                         tag, reads, time.monotonic() - started, (last or landing or Hud()).lie)
                return _unsettled(last or landing or Hud(), tag), touchdown is not None
            game = _tile_yards(hud.tiles, "carry (game)", "yards")
            total = _tile_yards(hud.tiles, "total length", "yards")
            # A freeze counts as touchdown only once the tile is known to belong to THIS shot: either
            # CARRY (game) was seen climbing (the ball flew), or the frozen value differs from the
            # impact baseline (the tile turned over, e.g. a chip already landed by the first read).
            # A value still equal to the baseline is the previous shot's stale tile and is rejected.
            if game is not None and prev_game is not None and game > prev_game:
                rose = True
            turned_over = game is not None and game_baseline is not None and game != game_baseline
            mine = mine or rose or turned_over
            if touchdown is None and (rose or turned_over) and game and game == prev_game:
                touchdown = time.monotonic() - started
                deadline = max(max_seconds, touchdown + self.cfg.screen.follow_roll_seconds)
                landing, steady = hud, 0  # landing read; kept only as a fallback if the HUD moves on
                log.info("%s touchdown ~%.1fs into the follow; following to rest", tag, touchdown)
            prev_game = game
            # Moved on to the next player: their address HUD shows carry (game) AND total length both
            # blank or the "--.--" placeholder (both parse to None) - the next shot hasn't been hit. The
            # tiles dict stays NON-empty on that placeholder, so test the parsed values, not emptiness.
            # Requiring BOTH None avoids a one-tile OCR dropout ending the follow early. Once we've seen
            # this shot's tiles, that reset means stop with the last this-shot read, never the next one's.
            # A short chip can land and stop before the second read, so it never shows a touchdown or a
            # roll: its tiles having turned over to this shot is enough (it waited out the cap before).
            if last is not None and (touchdown is not None or moving or mine) and game is None and total is None:
                log.info("%s HUD moved on after %s reads (%.1fs); using the last this-shot read (lie %s)",
                         tag, reads, time.monotonic() - started, last.lie)
                return last, True
            if hud.tiles:
                last = hud
            if total is not None and prev_total is not None and total != prev_total:
                moving, steady = True, 0  # still climbing (flight) or rolling out
            elif touchdown is not None and total is not None and total == prev_total:
                # Landed, and total length has stopped changing: the ball is at rest. Covers a long
                # release and a plug that never rolls (total already steady at touchdown).
                steady += 1
                if steady >= 2:
                    log.info("%s ball at rest: total steady after %s reads (%.1fs), resting lie %s", tag,
                             reads, time.monotonic() - started, hud.lie)
                    if self.cfg.screen.save_review_images:
                        await asyncio.to_thread(self._save_review_image, tag, "final")
                    return hud, True
            prev_total = total
            await asyncio.sleep(poll)
        # Timed out without a confirmed rest. If touchdown was seen, CARRY (game) is already final, so
        # the carry is still learnable (landed=True); only an unconfirmed touchdown skips learning.
        log.info("%s ball not seen to come to rest within %.0fs (touchdown=%s)", tag, deadline, touchdown is not None)
        return _unsettled(last or landing or Hud(), tag), touchdown is not None

    def _save_review_image(self, tag: str, moment: str) -> None:
        folder = Path(self.cfg.log_dir) / "shots" / time.strftime("%Y-%m-%d")
        folder.mkdir(parents=True, exist_ok=True)
        try:
            self.screen.save_jpeg(folder / f"{time.strftime('%H%M%S')}-{tag.strip('[]').replace(' ', '')}-{moment}.jpg")
        except Exception:
            log.exception("Couldn't save review image")

    async def _in_flight(
        self, shot, before: Hud, player: str | None, image: bytes | None, impact: float, factor: float
    ) -> None:
        deadline = self.cfg.commentary.flight_deadline_seconds
        images = [image] if image and self.flight_llm.cfg.vision else []
        who, team = self._who(player)
        user, facts_text = flight_prompt(
            player=who, team=team, hole=self.state.current_hole,
            facts=flight_facts(shot, before.to_pin, factor, before.lie, before.elevation_yards),
            recent_lines=self.state.recent_lines,
            speaker=self.speakers[0], has_image=bool(images),
        )
        try:
            lines = await asyncio.wait_for(
                self._generate("flight", self.flight_llm, user, images),
                timeout=max(deadline - (time.monotonic() - impact), 0.1),
            )
        except asyncio.TimeoutError:
            self.stats["flight_late"] += 1
            log.info("Flight call missed the %.1fs deadline", deadline)
            return
        elapsed = time.monotonic() - impact
        self.flight_latencies.append(elapsed)
        # No retry: there isn't time. A bad line is simply not said.
        good, _ = self._grounded(lines[:1], facts_text, mode="flight", seen_image=bool(images))
        if not good:
            return
        line = Line(speaker=self.speakers[0], text=good[0].text)
        self.state.record_lines([line])
        self.speech.say([line], interrupt=True, deadline=impact + self.cfg.tts.flight_audio_deadline_seconds)
        self._write_transcript([line], "flight")

    async def handle_hole_change(self, event: HoleChangeEvent) -> None:
        if event.correction:
            await self._correct_hole(event)
            return
        previous = self.state.current_hole
        previous_course = self.current_course
        self.state.current_hole = event.current
        self._last_hud_shot = {}  # stroke counts start over on a new hole
        self._setup_lies.clear()   # a stroke's key (who, shot, distance) can recur on another hole
        self._setup_yards.clear()
        self.current_course = event.course or self.current_course
        if m := re.search(r"par (\d)", event.current, re.IGNORECASE):
            self.current_par = int(m.group(1))
        log.info("Hole change: %s -> %s", previous, event.current)
        # First sighting of the hole banner at startup isn't a transition.
        if previous is None:
            return
        r = self.round
        if r and not r.ended:
            r.seq += 1
            if self.hole_number and not r.start_hole_assumed:
                r.seq = self.hole_number - r.start_hole + 1  # a read hole number beats counting changes
            if self.current_par:
                r.par.setdefault(r.seq, self.current_par)
            log.info("Round %s: hole %s of %s", r.id, r.seq, r.holes)
            self._score_hole_started(r)
            if r.seq > r.holes:
                if self._log_rounds and not r.log_complete:
                    self._extend_round(r)
                else:
                    await self.handle_round_end(RoundEndEvent(reason="play moved past the round's last hole"))
                    return
        if not self.cfg.commentary.segments_on_hole_change:
            return
        # A hole change after the round ended, or onto another course, is a new round loading, not a
        # hole to recap: a wrap here recapped the last course ("three under at the turn") on the next.
        if (r and r.ended) or (event.course and previous_course and event.course != previous_course):
            log.info("Hole change outside a round in progress; no recap")
            return
        if self._wrapped_hole == previous:  # recap already played on the scorecard, earlier
            self._wrapped_hole = self._wrap_branches = None
            await self._tee_up(event.current)  # the pre-made recap didn't tee up the new hole
            return
        # Let the hole's last shot finish its reaction before the wrap, so the two don't talk over
        # each other. Bounded, so a slow shot handler can't hold the booth off air.
        await self._drain_shot_tasks(self.cfg.commentary.wrap_wait_seconds)
        # A wrap pre-voiced during the last putt plays instantly, no model call. The scorecard has
        # bumped seq to the next hole, so the finished hole is seq - 1. Fall back to the on-demand
        # wrap when there's no branch for the score the hole actually came in on.
        if self.round and self._wrap_branches:
            # The hole's last reaction is queued now; let it finish airing (bounded) so the wrap's
            # interrupt doesn't cut a big exchange off mid-sentence.
            await self._drain_speech(self.cfg.commentary.wrap_wait_seconds + 10)
        if self.round and self._play_prepared_wrap(previous, self.round.seq - 1):
            self._wrapped_hole = None
            await self._tee_up(event.current)  # pre-made recap is recap-only; introduce the new hole
            return
        user, facts_text = segment_prompt(
            previous_hole=previous,
            current_hole=event.current,
            recap=self.state.hole_recap(previous, name=lambda p: self._display(p) or p),
            recent_lines=self.state.recent_lines,
            result=self._hole_score_facts(previous) + self._roster_facts()
            + self._weather_facts(self.cfg.weather.between_holes_rate),
        )
        await self._speak("segment", self.segment_llm, user, facts_text + "\n" + "\n".join(self._format_facts()), [])

    async def _tee_up(self, current_hole: str) -> None:
        """Introduce the hole that's starting. Used after a pre-made recap, which is recap-only; the
        on-demand wrap already tees up the next hole itself."""
        user, facts_text = hole_intro_prompt(
            hole=current_hole, recent_lines=self.state.recent_lines,
            extra=self._roster_facts() + self._weather_facts(self.cfg.weather.between_holes_rate))
        await self._speak("segment", self.segment_llm, user, facts_text, [], mode="round")

    # ---- the round ------------------------------------------------------------------------

    def _load_course_card(self, course: str | None):
        """GSPro's static card (par, stroke index) for this course, best-effort."""
        try:
            from . import course_card

            # GSPro's log names the selected course's folder, which settles a name two courses share.
            folder = self.gspro.course if self._log_truth else None
            return course_card.load(self.cfg.source.gspro_courses_dirs, course, folder=folder)
        except Exception as e:
            log.warning("Course card load failed (%s); par and index come from the scorecard", e)
            return None

    def _round_players(self, names: list[str]) -> list[str]:
        """Roster players in this round: every player on a team that has a GSPro name here.
        Both partners, so a one-ball team's net still has both handicaps."""
        teams = set()
        for n in names:
            player = self.league.match_player(n)
            if player:
                teams.add(self.league.player_team.get(player))
            team = self.league.match_team(n)
            if team:
                teams.add(team)
        return [p for t in teams if t for p in self.league.team_players.get(t, [])]

    async def _apply_live_handicaps(self, players: list[str]) -> None:
        """Refresh the current round's players' handicaps from Squabbit. Best-effort: on any
        failure the file's handicaps stand, so a slow or offline Squabbit never holds up the opener."""
        sq = self.cfg.squabbit
        if not sq.enabled or not self.league.squabbit_invite:
            return
        # The week's leaderboards load alongside, in their own task: a slow read never costs the handicaps.
        if self._board_task is None or self._board_task.done():
            self._board_task = asyncio.create_task(self._refresh_board())
        ids = [i for p in players if (i := self.league.player_info.get(p, {}).get("squabbit_id"))]
        if ids:
            try:
                from .squabbit import SquabbitClient

                def fetch() -> dict:
                    with SquabbitClient() as client:
                        return client.handicaps(ids)

                by_id = await asyncio.wait_for(asyncio.to_thread(fetch), timeout=sq.timeout_seconds)
                updated = self.league.apply_handicaps(by_id)
                log.info("Squabbit handicaps applied to %d of %d round players: %s",
                         len(updated), len(players), ", ".join(sorted(updated)))
            except Exception as e:
                log.warning("Squabbit handicap fetch failed (%s); using the league file's handicaps", e)
        # Give the leaderboards a moment more so the opener can use them (they usually land with the
        # handicaps); after that they fill in for the rest of the round.
        await asyncio.wait({self._board_task}, timeout=BOARD_WAIT_SECONDS)

    async def _refresh_board(self) -> None:
        """Read every league week's Squabbit leaderboard into self.board. Best-effort: on failure the
        last board read (or the history file's standings) stands."""
        try:
            from .squabbit import SquabbitClient

            def fetch() -> list | None:
                with SquabbitClient() as client:
                    league = client.league_by_invite(self.league.squabbit_invite)
                    return [(t, client.leaderboard(t["_id"])) for t in client.tournaments(league["_id"])] \
                        if league else None

            items = await asyncio.wait_for(asyncio.to_thread(fetch), timeout=self.cfg.squabbit.timeout_seconds)
            if items is None:
                return
            season = self.history.current() or {}
            self.board = Board.from_squabbit(items, self.league, season.get("points_table"))
            log.info("Squabbit leaderboards: %s", "; ".join(
                f"week {w} {len(self.board.posted(w))}/{self.board.team_count(w)} posted"
                for w in sorted(self.board.weeks) if self.board.posted(w)) or "no week posted yet")
        except Exception as e:
            log.warning("Squabbit leaderboard fetch failed (%s)", e)

    def _lone_team(self, names: list[str]) -> tuple[str | None, str | None]:
        """The single league team these HUD names belong to, or (None, reason) when they don't form
        one registered team. A one-ball format shows the team or one partner; a two-ball format shows
        both partners, who must be on the same team (Toby and Kyle are never a team)."""
        teams: set[str] = set()
        for n in names:
            player = self.league.match_player(n)
            team = self.league.player_team.get(player) if player else self.league.match_team(n)
            if not team:
                return None, f"{n!r} isn't a league player"
            teams.add(team)
        if len(teams) != 1:
            return None, "the players on the tee aren't partners on one team"
        return teams.pop(), None

    @staticmethod
    def _course_matches(entry: dict, course: str | None) -> bool:
        if not course:
            return False
        return max(course_match_score(entry.get("course") or "", course),
                   course_match_score(entry.get("gspro_course") or "", course)) >= 0.6

    def _classify_round(self, names: list[str], course: str | None) -> tuple[dict, str | None]:
        """Decide whether this opening tee is a league round. Returns (schedule entry, practice reason):
        a None reason means it's a league round on that entry; a string reason means practice (still
        commentated, just without league framing). A league round needs a scheduled course that's still
        in date, the right number of balls for the format (two, or one for alternate shot), those
        balls to be one registered team, and (when the setup screen was read) the week's nines. `round_week` in config forces a week when the course can't be
        read, bypassing only the course and date gates. Works the same for the tee's ball count and the
        setup screen's slots, since a player sets up exactly the balls they'll play (one for alternate)."""
        c = self.cfg.commentary
        today = time.strftime("%Y-%m-%d")
        if not self.league.players:
            return {}, "no league roster configured"
        if not self.league.data.get("schedule"):
            return {}, None  # no schedule configured: treat any round as the round
        forced = self.league.week_entry(c.round_week) if c.round_week else None
        entry = forced
        if not entry:
            # Course only: schedule_entry falls back to a date match, which would wrongly claim an
            # unscheduled course played on a scheduled day, so require the entry to match the course.
            found = self.league.schedule_entry(today, course) if course else None
            entry = found if found and self._course_matches(found, course) else None
            if not entry:
                return {}, f"{course!r} isn't on this year's schedule"
            end = entry.get("end_date")
            if end and str(end) < today:
                return {}, f"week {entry.get('week')} ({entry.get('course')}) was due by {end}"
        fmt = normalize_format(c.round_format or entry.get("format"))
        expected = 1 if fmt == "alternate" else 2
        if len(names) != expected:
            return {}, (f"{len(names)} ball(s) on the tee, {FORMAT_NAMES.get(fmt or '', 'league play')} "
                        f"shows {expected}")
        team, bad = self._lone_team(names)
        if bad:
            return {}, bad
        if (not forced and self._setup_holes
                and (bad := self._wrong_holes(entry, *self._setup_holes))):
            return {}, bad
        if not forced and team:
            # A team posts one round per week: once it has a full league round on every week scheduled
            # on this course, another round here is practice.
            weeks = [e for e in self.league.data.get("schedule") or [] if self._course_matches(e, course)]
            played = [c for c in self.store.league_round_courses(team) if self._course_matches(entry, c)]
            if weeks and len(played) >= len(weeks):
                return {}, f"{team} already played week {entry.get('week')}'s league round on {entry.get('course')}"
        return entry, None

    @staticmethod
    def _wrong_holes(entry: dict, start: int, count: int) -> str | None:
        """Why the holes picked on the setup screen (18 unless its HOLES tab said otherwise) aren't
        this week's league holes, or None when they match."""
        want_start, want_count = entry.get("start_hole"), entry.get("hole_count")
        if (want_start is None or want_start == start) and (want_count is None or want_count == count):
            return None
        names = {(1, 18): "all 18", (1, 9): "the front 9", (10, 9): "the back 9"}
        picked = names.get((start, count), f"{count} holes from hole {start}")
        week = entry.get("holes") or names.get((want_start or start, want_count or count), "other holes")
        return f"set up for {picked}, but week {entry.get('week')} is {week}"

    def _canonical_course(self, course: str | None) -> str | None:
        """The league's name for a course when it's on the schedule, else the name as given. GSPro
        names some courses differently (Balsams Panorama = Wentworth by the Sea); this maps both the
        setup/load name and the HUD name to one string so a prepared opener matches its tee."""
        if not course:
            return course
        entry = self.league.schedule_entry(time.strftime("%Y-%m-%d"), course)
        return entry.get("course") if entry and self._course_matches(entry, course) else course

    def _card_course(self, course: str | None) -> str | None:
        """The name to find a setup-read course's card folder by: GSPro's own name when the schedule
        gives one (Balsams Panorama for Wentworth), else the name as read."""
        sched = self.league.schedule_entry(time.strftime("%Y-%m-%d"), course) if course else None
        return (sched.get("gspro_course") if sched and self._course_matches(sched, course) else None) or course

    def _course_test(self, r: Round):
        """Whether a course name (a past league week's) is this round's course, or None to compare
        names. With the round's GSPro course known, the name must be that installed course's own
        (or its schedule alias): "Alpine Valley" is in "Swiss Alpine Valley" by name, but the
        two are different courses, and a course that isn't installed isn't its nearest namesake."""
        mine = r.course_card.folder if r.course_card else None
        if not mine:
            return None
        from . import course_card

        dirs = self.cfg.source.gspro_courses_dirs
        plain = course_card.plain_name
        # The schedule's own name for a GSPro course (Wentworth by the Sea is Balsams Panorama), but only
        # by exact name: a near one would turn "Swiss Alpine Valley" into "Alpine Valley".
        alias = {plain(e["course"]): e["gspro_course"] for e in self.league.data.get("schedule") or []
                 if e.get("course") and e.get("gspro_course")}
        return lambda name: (course_card.identify(dirs, alias.get(plain(name), name)) or "").lower() == mine.lower()

    def _tee_course(self, hud: Hud) -> str | None:
        """The course a round starting at this tee is on. The HUD's read is the short name players
        know, but it's OCR of whatever is on screen (an open settings menu reads "GAME MENU"); GSPro's
        log names the course it loaded, so a HUD read that isn't that course gives way to it."""
        read = self.current_course or hud.course
        g = self.gspro
        logged = g.course_display if self._log_truth and g.mode == "course" else None
        if not logged or (read and self._same_course(read, logged)):
            return read
        if read:
            log.info("Tee course: HUD read %r, but GSPro loaded %r; using GSPro's", read, logged)
        return logged

    def _refresh_course_card(self, r: Round, course: str | None) -> None:
        """Re-find a pre-built round's card now that GSPro's log has surely named the selected folder:
        the setup screen can be read before it does, and a name two courses share took the wrong one."""
        card = self._load_course_card(self._card_course(course))
        if card and (r.course_card is None or card.name != r.course_card.name):
            log.info("Course card for %r is %s, not %s, now GSPro's log names the course", course, card.name,
                     r.course_card.name if r.course_card else "none")
            r.apply_course_card(card)

    def _build_round(self, names: list[str], course: str | None, entry: dict, is_league: bool,
                     card_course: str | None = None, use_current_hole: bool = True,
                     slot_names: list[str] | None = None) -> Round:
        """A Round from a resolved roster, course, and schedule entry, with its course card applied.
        Shared by the tee path and the pre-round setup read. `card_course` overrides the name used to
        find the course-card folder (GSPro's own name), for a practice round with no schedule entry.
        `use_current_hole` is off for the pre-round build, where the HUD hole number is last round's
        (stale) rather than this round's start, so an unscheduled practice round's start stays unknown."""
        if self.round and self.round.ended:
            # The previous round is over: its lines (and their scores) must not colour this one.
            self.state.recent_lines.clear()
        c = self.cfg.commentary
        scheduled = (c.round_format or entry.get("format")) if is_league else None
        start = entry.get("start_hole") or (self.hole_number if use_current_hole and self.hole_number_read else None)
        holes = entry.get("hole_count") or c.round_holes
        confirmed = bool(entry.get("hole_count"))
        if self._setup_holes:   # what the setup screen showed beats the schedule's or config's guess
            start, holes = self._setup_holes
            confirmed = self._setup_holes_read or entry.get("hole_count") == holes
        r = Round(league=self.league, gspro_players=names, course=course, scheduled_format=scheduled,
                  holes=holes, start_hole=start, schedule=entry,
                  is_league=is_league, slot_names=slot_names)
        r.holes_confirmed = confirmed
        self._apply_setup_format(r)
        # GSPro's course folder is named by GSPro's own course name, which the schedule carries as
        # gspro_course when it differs from the league's name (e.g. Balsams Panorama vs Wentworth).
        card = self._load_course_card(card_course or entry.get("gspro_course") or course)
        if card:
            r.apply_course_card(card)
            # Feed the course's own altitude to the carry model (unless config already set one).
            if card.altitude_ft is not None and course:
                self.carry.altitude.setdefault(course, card.altitude_ft)
            log.info("Course card for %r: %s, par %s over holes %s, altitude %s ft", course, card.name,
                     sum(card.par.values()), sorted(card.par), card.altitude_ft)
        if self.current_par:
            r.par.setdefault(1, self.current_par)
        return r

    def _bg(self, coro) -> None:
        """Run a coroutine detached, keeping a reference so it isn't GC'd and swallowing its result."""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(lambda t: (self._bg_tasks.discard(t), t.cancelled() or t.exception()))

    def _start_round(self, r: Round) -> None:
        self.round = r
        r.shot_floor = self.store.last_shot_id()
        self._hc = RoundCounters(fires_at_start=self.gspro.fires, kinds_at_start=dict(self.gspro.kinds))
        if self.cfg.health.enabled and self.cfg.source.type != "replay":
            # The booth runs for days: what startup checked may have changed since (GSPro updated and
            # restarted, the monitor swapped), so check again as each round starts.
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None     # a round started outside the booth's loop (tests): nothing to check
            if loop:
                loop.create_task(asyncio.to_thread(self._health_checks, "round start"), name="health")
        self._setup_holes = self._opener_prepared_holes = None   # the next round reads its own setup
        self._setup_holes_read, self._holes_snap = False, None
        self._setup_format = self._opener_prepared_format = None
        self._scramble_pairs = set()
        self._putted_on_last_hole = self._round_end_queued = False
        # Close the "fresh empty scorecard" window so this same tee can't re-trigger a start.
        self._holes_played_at = 0.0
        # A closer pre-built for the previous round must not leak into this one.
        if self._closer_task and not self._closer_task.done():
            self._closer_task.cancel()
        self._closer_branches = None
        if self._news_task and not self._news_task.done():
            self._news_task.cancel()
        self._news_bits, self._news_aired, self._news_moment = [], [], None
        if self.news is not None:
            self._news_task = asyncio.create_task(self._prepare_news(r), name="news-prep")
            self._news_task.add_done_callback(lambda t: t.cancelled() or t.exception())
        self._start_round_asides(r)
        # Per-round live state must not carry into a new round (a reused (idx, hole) would suppress a
        # real penalty call, a streak would continue, a stale call would be graded against a new putt).
        self._narrated_pen.clear()
        self._note_uses.clear()
        self._fir_streak.clear()
        self._last_hud_shot.clear()
        self._pending_finish.clear()
        self._last_row.clear()
        self._mulligans_called.clear()
        self._finish_seq.clear()
        self._pending_prediction.clear()

    def _opener_io(self, r: Round, first: str | None = None, preround: bool = False) -> tuple[str, str]:
        season = {s.key: self.store.season_rounds(s.key) for s in r.sides}
        # The opening hole comes from the round's start hole and the course card's par when we know it
        # (league rounds: Front/Back 9 from the schedule). For a pre-round practice build the start is
        # unknown, so leave the first hole out rather than claim the stale HUD hole; a tee-time opener
        # (no preload) still has the live HUD hole.
        if r.start_hole and not r.start_hole_assumed:
            par = r.par.get(r.start_hole)
            hole = f"hole {r.start_hole}" + (f", par {par}" if par else "")
        else:
            hole = None if preround else self.state.current_hole
        facts = (r.opening_facts(season, first_up=self._display(first), hole=hole)
                 + self._league_facts(r) + self._weather_facts())
        return opener_prompt(facts=facts, recent_lines=self.state.recent_lines)

    def _same_course(self, x: str | None, y: str | None) -> bool:
        a, b = self._canonical_course(x), self._canonical_course(y)
        return bool(a and b and (a.lower() == b.lower() or course_match_score(a, b) >= 0.6))

    def _pending_mismatch(self, p: PendingOpening, is_league: bool, course: str | None,
                          names: list[str]) -> str | None:
        """Why a prepared opener (league or practice) doesn't fit this tee, or None when it does.
        Matched on: the same league-vs-practice call, the same course (by the league's canonical name,
        so the GSPro setup name and the HUD name agree), and the same set of league players."""
        if p.is_league != is_league:
            return f"prepared {'league' if p.is_league else 'practice'}, tee reads {'league' if is_league else 'practice'}"
        if not self._same_course(p.course, course):
            return f"course {p.course!r} != tee {course!r}"
        tee_players = frozenset(pl for n in names if (pl := self.league.match_player(n)))
        if p.players != tee_players:
            return f"players {sorted(p.players)} != tee {sorted(tee_players)}"
        if p.guessed and p.roster_key != self._roster_key(names):
            return f"made for the last roster {sorted(p.roster_key)}, tee reads {sorted(self._roster_key(names))}"
        return None

    def _reconcile_slots(self, r: Round, names: list[str]) -> None:
        """Make sure the round has a GSPro player-index order. The setup-screen slots are the source
        when they cover everyone on the first tee; when they're missing or disagree with the tee (a
        misread or skipped setup screen), the tee HUD's top-to-bottom order is used instead (the player
        up first, then the others list), so the GSPro log can still be attributed per player."""
        if not names:
            return
        tee = self._roster_key(names)
        if r.slot_names and tee <= self._roster_key([n for n in r.slot_names if n]):
            return
        log.info("GSPro player order from the first tee, top to bottom: %s (setup slots were %s)",
                 names, r.slot_names)
        r.slot_names = list(names)

    def _take_matching_pending(self, is_league: bool, course: str | None,
                               names: list[str]) -> PendingOpening | None:
        p, self._pending_opening = self._pending_opening, None  # consumed either way
        self._opener_prepared_for = frozenset()  # next round front-loads fresh
        self._opener_gen += 1  # a front-load still composing must not revive _pending_opening post-tee
        if p is None:
            return None
        if why := self._pending_mismatch(p, is_league, course, names):
            log.info("Prepared opener set aside (%s); making the opener at the tee", why)
            return None
        if (age := time.monotonic() - p.created) > self.cfg.commentary.preround_ttl_seconds:
            log.info("Prepared opener set aside (%.0fs old); making the opener at the tee", age)
            return None
        return p

    async def handle_round_start(self, event: RoundStartEvent) -> None:
        hud, c = event.hud, self.cfg.commentary
        if event.joined:
            await self._join_round(hud)
            return
        # The opener (aired over the course load) already introduces the opening hole and who's up, so
        # the live pre-shot on that first tee shot is redundant and lands late, just as the player is
        # ready to swing. Skip it for that one shot; the in-flight call still covers the shot.
        self._skip_opening_preshot = c.opener
        # GSPro's own name for the player (the text before the score badge), which the scorecard uses.
        raw = re.split(r"\s+(?:E|[+-]\d+|Shot)\b", (hud.player_text or "").strip(), maxsplit=1)[0].strip()
        first = self.league.match_player(hud.player_text) or raw or None
        names = [n for n in [raw or first] + [o.name for o in hud.others] if n]
        course = self._tee_course(hud)
        entry, practice = self._classify_round(names, course)
        is_league = practice is None
        # Peek at an opener that already played over the load, before the match below consumes it. Only
        # one for this course, and recent, stands in for this round's: an opener that aired for a
        # course the player then backed out of (or long ago) doesn't.
        p = self._pending_opening
        aired_players = (p.players if p and p.aired and self._same_course(p.course, course)
                         and time.monotonic() - p.created <= c.preround_ttl_seconds else None)
        # An opener prepared off the setup screen airs now, with no tee-time model or grounding wait,
        # when the tee confirms the same course and the same league team(s). Else it's made here.
        if (pending := self._take_matching_pending(is_league, course, names)) is not None:
            self._reconcile_slots(pending.round, names)
            self._refresh_course_card(pending.round, pending.course)
            self._apply_setup_holes(pending.round)
            self._apply_setup_format(pending.round)
            self._apply_log_holes(pending.round)
            self._start_round(pending.round)
            self._number_start_hole(pending.round)
            # League: net scoring. Practice: no strokes, but the closer judges form against the index.
            self._bg(self._apply_live_handicaps(self._round_players(names)))
            if pending.aired:
                log.info("Round %s started on %s (%s); opener already aired during the load",
                         pending.round.id, pending.round.course, "league" if is_league else "practice")
            else:
                log.info("Round %s started on %s (%s); prepared opener aired, no tee-time model call",
                         pending.round.id, pending.round.course, "league" if is_league else "practice")
                if c.opener:
                    self._air_lines("opener", pending.lines)
            return
        if is_league:
            await self._apply_live_handicaps(self._round_players(names))
            log.info("League round: course %r is week %s (%s)", course, entry.get("week"), entry.get("course"))
        else:
            # Off the critical path: practice only needs the index for the closer's read on form.
            self._bg(self._apply_live_handicaps(self._round_players(names)))
            log.info("Practice, not a league round (%s): calling the golf without league framing", practice)
        r = self._build_round(names, course, entry, is_league)
        self._reconcile_slots(r, names)
        self._apply_log_holes(r)
        self._start_round(r)
        self._number_start_hole(r)
        log.info("Round %s started on %s: %s; format %s; start hole %s%s", r.id, r.course,
                 "; ".join(f"{s.label} [{', '.join(s.gspro)}] strokes {s.strokes}" for s in r.sides),
                 FORMAT_NAMES.get(r.format or "", "not set"), r.start_hole,
                 " (assumed)" if r.start_hole_assumed else "")
        if r.format_warning:
            log.warning("Round %s: %s", r.id, r.format_warning)
        if not c.opener:
            return
        # An opener already aired over the load and the tee's roster is a subset of what it covered (a
        # partner dropped by an occluded HUD read, not a different round): keep it, don't air a second.
        tee_players = frozenset(p for n in names if (p := self.league.match_player(n)))
        if aired_players is not None and tee_players <= aired_players:
            log.info("An opener already aired over the load for %s; the tee read %s (a subset) - not "
                     "airing a second", sorted(aired_players), sorted(tee_players))
            return
        user, facts_text = self._opener_io(r, first)
        await self._speak("opener", self.opener_llm, user, facts_text, [], mode="round")

    async def _join_round(self, hud: Hud) -> None:
        """Track a round the booth came up in the middle of: same roster and course as a fresh start,
        but the start hole and current hole come from GSPro's log, and there's no opener."""
        raw = re.split(r"\s+(?:E|[+-]\d+|Shot)\b", (hud.player_text or "").strip(), maxsplit=1)[0].strip()
        names = [n for n in [raw or self.league.match_player(hud.player_text)] + [o.name for o in hud.others] if n]
        course = self._tee_course(hud)
        entry, practice = self._classify_round(names, course)
        is_league = practice is None
        if is_league:
            await self._apply_live_handicaps(self._round_players(names))
        else:
            self._bg(self._apply_live_handicaps(self._round_players(names)))
        r = self._build_round(names, course, entry, is_league, use_current_hole=False)
        g = self.gspro
        r.start_hole, r.start_hole_assumed = g.first_hole + 1, False   # the log's holes are 0-based
        if r.course_card:
            r.apply_course_card(r.course_card)
        r.seq = max(1, g.current_hole - g.first_hole + 1)
        if r.seq > r.holes:
            r.set_holes(max(r.seq, min(18, r.start_hole + 17) - r.start_hole + 1))
        self.hole_number, self.hole_number_read = g.current_hole + 1, True
        self._reconcile_slots(r, names)
        self._start_round(r)
        log.info("Round %s joined mid-round on %s (%s): %s; started on hole %s, now on hole %s (%s of %s); no opener",
                 r.id, r.course, "league" if is_league else "practice", ", ".join(s.label for s in r.sides),
                 r.start_hole, self.hole_number, r.seq, r.holes)

    async def _watch_setup(self) -> None:
        probed = callable(getattr(self.screen, "setup_frame", None)) and callable(
            getattr(self.screen, "region_tones", None)) and callable(getattr(self.screen, "region", None))
        sampler = asyncio.create_task(self._sample_setup_tabs()) if probed else None
        try:
            await self._watch_setup_loop()
        finally:
            if sampler is not None:
                sampler.cancel()

    async def _sample_setup_tabs(self) -> None:
        """Keep the latest frame showing the setup screen's PLAYERS tab with names on it, and the nines
        last checked on its HOLES tab, sampled every quarter second. The watch's own polls are ~2 s apart
        once a region needs reading, so a player who lands on PLAYERS (GSPro's default tab) and clicks on
        to MATCH SETTINGS within a second or two would otherwise never have the names read, and one who
        unchecks a nine and presses Play just as quickly would get an 18-hole round. Only the small tab
        boxes are grabbed each time; the full frame only when a tab is lit."""
        slots = [f"setup_slot_{i}" for i in range(1, 9)]
        region = self.screen.region("setup_players_tab")
        holes_region = self.screen.region("setup_holes_tab")
        last_nines: tuple[int, int] | None = None

        def lit(r, level):
            hist = self.screen.grab(r).convert("L").histogram()
            return sum(v * c for v, c in enumerate(hist)) / (sum(hist) or 1) >= level

        def sample_holes():
            if not lit(holes_region, _HOLES_TAB_LIT):
                return None
            at = time.monotonic()
            frame = self.screen.setup_frame()
            return at, _nines_picked(self.screen.region_tones(
                ["setup_holes_tab", "setup_front9_box", "setup_back9_box"], frame))

        def sample():
            if not lit(region, _PLAYERS_TAB_LIT):
                return None
            at = time.monotonic()
            frame = self.screen.setup_frame()
            tones = self.screen.region_tones(["setup_players_tab", *slots], frame)
            states = [_slot_state(tones[n]) for n in slots if n in tones]
            if tones["setup_players_tab"].mean < _PLAYERS_TAB_LIT or "list" in states or "filled" not in states:
                return None
            return at, frame, tones

        while True:
            await asyncio.sleep(0.25)
            if self.round is not None and not self.round.ended:
                self._players_snap = self._holes_snap = None
                last_nines = None
                continue
            # Kept up even with an opener out or held: backing out to set up again is caught the same way.
            try:
                got = await asyncio.to_thread(sample)
            except Exception:
                log.debug("PLAYERS-tab sample failed", exc_info=True)
                continue
            if got is not None:
                self._players_snap = got
            try:
                nines = await asyncio.to_thread(sample_holes)
            except Exception:
                log.debug("HOLES-tab sample failed", exc_info=True)
                nines = None
            # Two samples in a row must agree, so a frame mid-transition can't pass for a pick.
            if nines and nines[1] and nines[1] == last_nines:
                self._holes_snap = nines
            last_nines = nines[1] if nines else None

    async def _watch_setup_loop(self) -> None:
        """Between rounds, read GSPro's setup screen for the course and roster, then fire the opener
        when the course's LOAD screen appears (Play was pressed and it's loading).

        Firing on the load screen, not on the Play button merely going away, is what makes this robust
        to a window (a notification, say) popping up over the setup screen: a popup can hide the Play
        button but it can't put the course's name on GSPro's loading screen, so a stray overlay never
        triggers a spurious opener. The roster itself is likewise hard to corrupt: the "Play <course>"
        button shows on every setup tab, so the slots are read only while the pixels show the PLAYERS tab
        selected, only for slots holding a name pill, and never while a player dropdown is open (its
        list spills over the slots below). With that pixel probe available every pill counts (league
        player or guest); without it, a slot counts only when it resolves to a league player. The last
        good read is kept across polls.

        Speed: a quick swap must register before the players leave the tab. Each poll is a frame grab
        plus a ~2 ms pixel probe; OCR (~0.7 s a region on the sim PC) runs only on a region whose pixels
        changed, and never on an empty slot or a settings tab."""
        c = self.cfg.commentary
        slots = [f"setup_slot_{i}" for i in range(1, 9)]
        seen_setup = False  # the setup screen has been up this session (so a load screen is a real Play)
        course: str | None = None
        roster: list[str] = []
        slot_order: list[str] = []   # every named setup slot in order (for the GSPro index mapping)
        logged_roster: list[str] = []   # last roster printed, so a live read can be watched changing
        last_read: list[str] = []    # previous poll's roster, to confirm a read before trusting it
        slot_cache: dict = {}        # per-slot (pixel digest, OCR lines); skips OCR on unchanged slots
        gate_cache: dict = {}        # the same for the Play button and load-screen title
        mode_cache: dict = {}        # the same for the MATCH SETTINGS game mode
        last_diag = 0.0
        players_read_at = -1e9   # when the latest PLAYERS frame this loop read itself was grabbed
        frame_at = holes_since = float("-inf")   # this poll's grab; when this setup was first seen to begin
        seen_play: str | None = None   # the Play button's course on this poll (None off the setup screen)
        while True:
            await asyncio.sleep(max(c.preround_poll_seconds, 0.2))
            prev_at, frame_at = frame_at, time.monotonic()
            if self.round is not None and not self.round.ended:
                # Mid-round the setup regions land on the live HUD; nothing to read until it's over.
                seen_setup, course, roster, self._opener_prepared_for = False, None, [], frozenset()
                slot_order, slot_cache, gate_cache = [], {}, {}
                continue
            p = self._pending_opening
            held = False
            if p is not None:
                if time.monotonic() - p.created > c.preround_ttl_seconds:
                    log.info("Prepared opener expired unused; resuming the setup watch")
                    self._pending_opening, self._opener_prepared_for = None, frozenset()
                elif not p.awaiting_load:
                    # Already aired over the load (league) or held for the tee (practice): only watch the
                    # Play button (OCR'd only when its pixels change), not the roster, through the load.
                    # If it's back, the player backed out to set up again, so that opener is stale. A
                    # front-loaded opener still waiting for the load screen (awaiting_load) keeps polling.
                    seen_setup, course, roster, slot_order, slot_cache = False, None, [], [], {}
                    held = True
            probed = callable(getattr(self.screen, "setup_frame", None)) and callable(
                getattr(self.screen, "region_tones", None))
            tones: dict = {}
            players_tab = False
            try:
                if probed:
                    grabbed_at = time.monotonic()
                    frame = await asyncio.to_thread(self.screen.setup_frame)
                    tones = self.screen.region_tones(
                        ["setup_players_tab", "setup_holes_tab", "setup_match_tab", "setup_front9_box",
                         "setup_back9_box", *slots],
                        frame)
                    tab = tones.get("setup_players_tab")
                    players_tab = tab is not None and tab.mean >= _PLAYERS_TAB_LIT
                    if players_tab:
                        players_read_at = grabbed_at
                    # On the PLAYERS tab only the Play button matters; elsewhere also the load title.
                    gate_names = ["setup_course"] if players_tab else ["setup_course", "load_course"]
                    gate_lines, gate_cache = await asyncio.to_thread(
                        self.screen.read_region_lines_cached, gate_names, frame, gate_cache)
                    gate = {n: " ".join(gate_lines.get(n, [])) for n in ("setup_course", "load_course")}
                else:
                    frame, gate = await asyncio.to_thread(self.screen.grab_and_read, ["setup_course", "load_course"])
            except Exception:
                log.exception("Setup-screen read failed")
                continue
            play_course = _play_button_course(gate.get("setup_course", ""))
            last_play, seen_play = seen_play, play_course
            if held:
                if not play_course:
                    continue
                log.info("Back on the setup screen (%r) after the %s opener for %r: dropping it and "
                         "reading the new setup", play_course, "aired" if p.aired else "held", p.course)
                self._opener_gen += 1
                self._pending_opening, self._opener_prepared_for = None, frozenset()
            if play_course:
                to_read = slots
                slot_frame = frame
                if probed and not players_tab and (snap := self._players_snap) is not None \
                        and snap[0] > players_read_at and time.monotonic() - snap[0] <= 3.0:
                    # This frame is on another tab, but the sampler caught PLAYERS moments ago: read
                    # the names off that frame (the settings/holes reads below still use this one), once
                    # its own Play button shows it's this setup and not the course before.
                    self._players_snap = None
                    try:
                        snap_gate, _ = await asyncio.to_thread(
                            self.screen.read_region_lines_cached, ["setup_course"], snap[1], {})
                    except Exception:
                        log.exception("Setup read of the sampled PLAYERS tab failed")
                        snap_gate = {}
                    if _play_button_course(" ".join(snap_gate.get("setup_course", []))) == play_course:
                        slot_frame, players_tab = snap[1], True
                        tones = {**tones, **snap[2]}
                        log.info("Setup: reading the players off the PLAYERS tab seen %.1fs ago",
                                 time.monotonic() - snap[0])
                if probed:
                    fresh = not seen_setup or play_course != course
                    if fresh:
                        # A HOLES sample since the last poll that didn't show this setup is this setup's;
                        # straight from another course's setup, one in between could be either's.
                        holes_since = frame_at if last_play and last_play != play_course else prev_at
                    self._read_setup_holes(tones, fresh=fresh, at=grabbed_at)
                    self._take_holes_sample(holes_since)
                    try:
                        mode_cache = await self._read_setup_format(tones, frame, fresh, mode_cache)
                    except Exception:
                        log.exception("Setup game-mode read failed")
                    if (tab := tones.get("setup_holes_tab")) is not None and tab.mean >= _HOLES_TAB_LIT:
                        self._keep_fixture_frame("setup_holes", frame)
                    if course and play_course == course and roster:
                        self._maybe_front_load(play_course, roster, slot_order)   # recompose if the nines changed
                    states = {s: _slot_state(tones[s]) for s in slots if s in tones}
                    if not players_tab or "list" in states.values():
                        # A settings/holes tab (no player slots on screen), or an open player dropdown
                        # whose entries sit where other slots' names go: nothing trustworthy to read.
                        # Keep the roster; the next PLAYERS-tab frame with the list closed reads it.
                        if not seen_setup:
                            log.info("Setup read: course %r, waiting for the PLAYERS tab (%s)", play_course,
                                     "dropdown open" if players_tab else "another tab selected")
                        seen_setup, course = True, play_course
                        continue
                    to_read = [s for s in slots if states.get(s) == "filled"]   # empty slots: no OCR
                try:
                    # Only OCR a slot whose pixels changed since last poll; the static slot UI is
                    # pixel-stable, so an unchanged name is reused and a single swap reads on its own -
                    # catching a quick roster change in ~one slot's time, not all eight.
                    slot_lines, slot_cache = await asyncio.to_thread(
                        self.screen.read_region_lines_cached, to_read, slot_frame, slot_cache)
                except Exception:
                    log.exception("Setup slot read failed")
                    continue
                # On the setup screen. Keep only real player slots.
                # Positional: index = slot number - 1, None for an empty or unreadable slot, so one
                # slot's OCR dropout can't shift the later players' GSPro indices.
                positional = [_setup_slot_name(slot_lines.get(s, [])) for s in slots]
                # With the probe, every name pill on the PLAYERS tab is a player (league or guest).
                # Without it, only a league player is trusted on the name alone, which keeps an empty
                # "+" slot's OCR junk and settings labels out.
                names = [n for n in positional if n and (probed or self.league.match_player(n))]
                # Fast but flicker-robust: trust a grown or changed roster IMMEDIATELY (so the opener
                # front-loads right away, no extra poll of delay), but a SHRUNK one - a player who
                # vanished from just this frame - only after it repeats. A one-frame slot dropout is
                # always a shrink, so this can't let it poison the roster the opener front-loads for,
                # yet adding or swapping a player still fires the front-load with no wait.
                accept = bool(names) and (not set(names) < set(roster) or names == last_read)
                last_read = names
                if accept:
                    roster = names
                    slot_order = positional
                    self._last_setup_roster = (list(names), list(positional))
                    if probed and players_tab:
                        self._keep_fixture_frame("setup_players", slot_frame)
                # Log on first detection and on every accepted roster change, so a live setup read can
                # be watched tracking the picker rather than looking stuck on the first team seen.
                if (not seen_setup) or (accept and names != logged_roster):
                    raw = {s: ls for s in slots if (ls := slot_lines.get(s))}
                    log.info("Setup read: course %r, roster %s (raw slots %s)", play_course, names, raw)
                    logged_roster = names
                seen_setup, course = True, play_course
                # Compose the league opener now, while still on the setup screen, so a fast course load
                # doesn't catch the LLM mid-call. Recomposes if the roster changes.
                self._maybe_front_load(play_course, roster, slot_order)
                continue
            # Not on the setup screen. Fire only when the course's load screen is up (a positive signal
            # that Play was pressed): the load-screen title matches the course we set up. A popup over
            # the button reads neither, so we just keep state and wait.
            load_title = gate.get("load_course", "").strip()
            loading = bool(seen_setup and course and load_title and course_match_score(load_title, course) >= 0.6)
            if loading and probed:
                self._take_holes_sample(holes_since)   # nines changed in the moment before Play was pressed
            if loading and not roster and self._last_setup_roster:
                # Play was pressed without the PLAYERS tab ever being shown, so this setup read no players.
                # GSPro keeps the players from the last round, so make the opener for the last roster read
                # and hold it for the tee, which airs it only if it shows exactly those players.
                guess, guess_order = self._last_setup_roster
                log.info("Load screen for %r up, but its PLAYERS tab was never read; preparing the opener "
                         "for the last roster %s, held for the tee", course, guess)
                self._opener_gen += 1
                self._pending_opening = None
                try:
                    await self._prepare_speculative_round(course, list(guess), air=False, gen=self._opener_gen,
                                                          slot_names=list(guess_order), guessed=True)
                except Exception:
                    log.exception("Preparing the early opener failed")
                seen_setup, course, roster, slot_order, slot_cache = False, None, [], [], {}
            elif loading and roster:
                p = self._pending_opening
                if p is not None and p.awaiting_load and self._front_loaded_fits(course, roster):
                    # The opener was composed on the setup screen and still fits. Air it over the load,
                    # league or practice alike (the roster is locked once Play is pressed, and the fits
                    # check just confirmed it matches, so there's no wrong-opener risk to hold against).
                    # Bump the gen so a front-load still composing (a roster flicker back to this team)
                    # bails at its gen guard instead of overwriting this with a fresh awaiting_load object
                    # that would double-air at the tee.
                    self._opener_gen += 1
                    p.awaiting_load = False
                    p.aired = True
                    self._air_lines("opener", p.lines)
                    log.info("Front-loaded opener aired during the load (%s) for %s %s",
                             "league" if p.is_league else "practice", p.course, sorted(p.players))
                else:
                    log.info("Load screen for %r up with roster %s; preparing the opener", course, roster)
                    # Supersede any front-load still composing in the background: bump the gen so its
                    # stale result bails at the gen guard instead of overwriting the opener we air here
                    # (which would leave an unaired awaiting_load object to double-air at the tee).
                    self._opener_gen += 1
                    self._pending_opening = None
                    try:
                        # Carry the gen: if the tee confirms and composes its own opener while this
                        # load-screen compose is still in flight (the course loaded faster than the
                        # model), _take_matching_pending bumps the gen, so this one bails instead of
                        # airing a second opener over the load.
                        await self._prepare_speculative_round(course, roster, gen=self._opener_gen,
                                                              slot_names=slot_order)
                    except Exception:
                        log.exception("Preparing the early opener failed")
                seen_setup, course, roster, slot_order, slot_cache = False, None, [], [], {}
            elif time.monotonic() - last_diag > 8:
                last_diag = time.monotonic()
                log.info("Setup watch: not on setup (button %r, load title %r)",
                         gate.get("setup_course", ""), load_title)

    def _roster_key(self, roster: list[str]) -> frozenset:
        """Who's on the setup screen: league players by roster name, anyone else by their GSPro name."""
        return frozenset(self.league.match_player(n) or n.strip() for n in roster if n and n.strip())

    def _apply_setup_holes(self, r: Round) -> None:
        """An opener composed before a late change on the HOLES tab: put its round on the nines picked."""
        if self._setup_holes and (r.start_hole, r.holes) != self._setup_holes:
            log.info("Round %s: the setup screen changed to %s holes from hole %s after the opener was made",
                     r.id, self._setup_holes[1], self._setup_holes[0])
            r.start_hole, r.start_hole_assumed = self._setup_holes[0], False
            r.set_holes(self._setup_holes[1], confirmed=self._setup_holes_read)   # re-applies the course card for the new start

    def _apply_setup_format(self, r: Round) -> None:
        """A scramble picked on the setup screen: the round is one from the start (two players are one
        team; with more, the log's picks say who's together)."""
        if self._setup_format in ("shamble", "alternate"):
            r.no_scramble = True   # GSPro copies balls between teammates here too: not a scramble
        if self._setup_format == "scramble" and r.format != "scramble":
            names = [n for n in (r.slot_names or list(r.roster)) if n]
            r.play_scramble([names] if len(names) == 2 else [])
            log.info("Round %s: a scramble, from the setup screen", r.id)

    async def _read_setup_format(self, tones: dict, frame, fresh: bool, cache: dict) -> dict:
        """GSPro's game mode, while the MATCH SETTINGS tab is up (see _setup_format)."""
        if fresh:
            self._setup_format = None
        tab = tones.get("setup_match_tab")
        if tab is None or tab.mean < _MATCH_TAB_LIT:
            return cache
        lines, cache = await asyncio.to_thread(self.screen.read_region_lines_cached, ["setup_game_mode"],
                                               frame, cache)
        text = " ".join(lines.get("setup_game_mode", [])).upper()
        if not text.strip():
            return cache
        self._keep_fixture_frame("setup_match", frame)
        fmt = next((f for word, f in (("SCRAMBLE", "scramble"), ("SHAMBLE", "shamble"), ("ALTERNATE", "alternate"))
                    if word in text), None)
        if fmt != self._setup_format:
            log.info("Setup MATCH SETTINGS tab: game mode %r%s", text.strip(), f" ({fmt})" if fmt else "")
            self._setup_format = fmt
        return cache

    def _read_setup_holes(self, tones: dict, fresh: bool, at: float | None = None) -> None:
        """Which nines the round will play, from the setup screen's HOLES tab: both boxes is 18 from hole
        1, FRONT 9 alone is 9 from 1, BACK 9 alone is 9 from 10. GSPro defaults to all 18, so a setup
        screen whose HOLES tab is never opened is taken as 18 (unconfirmed). `fresh` marks a new setup
        (first seen, or another course), which starts back at that default. `at` is when the frame was
        grabbed. Single holes left out aren't tracked."""
        if fresh:
            self._setup_holes, self._setup_holes_read, self._setup_holes_at = (1, 18), False, float("-inf")
        picked = _nines_picked(tones)
        if picked is None:
            if fresh:
                log.info("Setup: assuming 18 holes from hole 1 (GSPro's default) unless the HOLES tab says otherwise")
            return
        self._set_setup_holes(picked, time.monotonic() if at is None else at)

    def _set_setup_holes(self, picked: tuple[int, int], at: float, how: str = "") -> None:
        """Take nines read off the HOLES tab at `at`, unless a newer read already stands."""
        if at < self._setup_holes_at:
            return
        self._setup_holes_at = at
        if picked != self._setup_holes or not self._setup_holes_read:
            log.info("Setup HOLES tab%s: %s", how, {(1, 18): "both nines, 18 holes", (1, 9): "front 9 only",
                                                    (10, 9): "back 9 only"}[picked])
        self._setup_holes, self._setup_holes_read = picked, True

    def _take_holes_sample(self, since: float) -> None:
        """Apply the quick sampler's HOLES-tab read if it was caught on this setup (after `since`)."""
        snap = self._holes_snap
        if snap is not None and snap[0] > since and self._setup_holes is not None:
            self._set_setup_holes(snap[1], snap[0], " (sampled)")

    def _maybe_front_load(self, course: str, roster: list[str], slot_order: list[str] | None = None) -> None:
        """While still on the setup screen, start composing the opener for a confident roster so it's
        ready before a fast course load, recomposing each time the roster changes (cheap, one extra
        call). Covers practice rounds too: they're held for the tee rather than aired over the load,
        but they still pre-render here so a quick load doesn't catch the compose mid-flight. The only
        gate is a confident player read (see _watch_setup), so empty/noise slots never trigger a compose."""
        if not self.cfg.commentary.opener or not roster:
            return
        key = self._roster_key(roster)
        if not key or (key == self._opener_prepared_for and self._setup_holes == self._opener_prepared_holes
                       and self._setup_format == self._opener_prepared_format):
            return
        self._opener_prepared_for, self._opener_prepared_holes = key, self._setup_holes
        self._opener_prepared_format = self._setup_format
        self._opener_gen += 1
        log.info("Front-loading the opener for roster %s on %r", sorted(key), course)
        self._bg(self._prepare_speculative_round(course, list(roster), air=False, gen=self._opener_gen,
                                                 slot_names=list(slot_order) if slot_order else None))

    def _front_loaded_fits(self, course: str, roster: list[str]) -> bool:
        """The front-loaded opener still matches the roster and course now on the load screen (league
        or practice; the caller airs a league one over the load and holds a practice one for the tee)."""
        p = self._pending_opening
        if p is None:
            return False
        return (p.roster_key == self._roster_key(roster) and bool(p.course) and bool(course)
                and course_match_score(p.course, course) >= 0.6
                and (self._setup_holes is None or (p.round.start_hole, p.round.holes) == self._setup_holes)
                and (self._setup_format != "scramble" or p.round.format == "scramble"))

    async def _prepare_speculative_round(self, course: str, names: list[str], air: bool = True,
                                         gen: int | None = None, slot_names: list[str] | None = None,
                                         guessed: bool = False) -> None:
        """Compose the opener from the setup screen's course and roster. With `air` (the load-screen
        trigger) the opener plays immediately over the load, league or practice alike. Without `air`
        (front-loaded while still on the setup screen; `gen` drops a compose the roster has since moved
        past) it's composed and held with awaiting_load, to be aired when the load screen confirms Play.
        handle_round_start airs any held opener at the tee if it still matches, so even a backed-out
        round never plays a wrong opener early. `guessed` (the roster is the last one read, not this
        setup's) holds it for the tee, never airing it over the load."""
        c = self.cfg.commentary
        if not c.opener:
            return
        entry, practice = self._classify_round(names, course)
        is_league = practice is None
        # Say the league's name for the course, not GSPro's (e.g. Wentworth, not Balsams Panorama), and
        # still find the course-card folder by GSPro's name. Works whether or not it's a league round.
        sched = self.league.schedule_entry(time.strftime("%Y-%m-%d"), course)
        sched = sched if sched and self._course_matches(sched, course) else {}
        if is_league:
            await self._apply_live_handicaps(self._round_players(names))
        elif air:
            log.info("Setup read %s on %r is a practice round (%s); preparing its opener", names, course, practice)
        r = self._build_round(names, sched.get("course") or course, entry if is_league else {}, is_league,
                              card_course=self._card_course(course), use_current_hole=False,
                              slot_names=slot_names)   # None (not the filtered roster) is the safe signal
        # The first setup slot (top-left) is first on the tee, EXCEPT in alternate shot, where who tees
        # off isn't fixed to one person, so no starter is named there. An unplaceable practice round's
        # opening hole is still unknown until the tee, so _opener_io leaves the hole out too.
        first = None if r.format == "alternate" else (
            (self.league.match_player(names[0]) or names[0]) if names else None)
        user, facts_text = self._opener_io(r, first=first, preround=True)
        lines = await self._compose("opener", self.opener_llm, user, facts_text, [], mode="round",
                                    system=self._system_league if is_league else self._system_practice)
        if not lines:
            log.info("Early opener for %r produced no lines; it'll be made at the tee", r.course)
            if gen is not None and gen == self._opener_gen:
                self._opener_prepared_for = frozenset()  # let a later poll re-trigger; don't stomp a newer gen
            return
        if gen is not None and gen != self._opener_gen:
            return  # the roster moved on; a newer front-load supersedes this compose
        players = frozenset(pl for n in names if (pl := self.league.match_player(n)))
        # Air over the load for league and practice alike (the user wants both to behave the same). The
        # roster is locked once Play is pressed, so there's no incomplete-roster risk at the load screen;
        # if a late setup read did move the roster on, handle_round_start airs the tee opener instead.
        aired = bool(air)
        # Front-loaded openers (league or practice) wait on the load screen, so the watcher keeps
        # polling and recomposes on a roster change. The load-screen handler airs a league one over
        # the load and commits a practice one to be aired at the tee (never a wrong opener early).
        awaiting = not air and not guessed
        self._pending_opening = PendingOpening(course=course, teams=frozenset(r.teams), players=players,
                                               is_league=is_league, round=r, lines=lines,
                                               aired=aired, awaiting_load=awaiting,
                                               roster_key=self._roster_key(names), guessed=guessed)
        if aired:
            self._air_lines("opener", lines)  # play over the course load
            log.info("Early opener aired during the load (%s) for %s %s: %d line(s)",
                     "league" if is_league else "practice", r.course, sorted(players), len(lines))
        elif awaiting:
            log.info("Opener front-loaded (%s) for %s %s, ready to air/hold on the load: %d line(s)",
                     "league" if is_league else "practice", r.course, sorted(players), len(lines))
        else:
            log.info("Early opener prepared (%s, last roster) for %s %s, held for the tee: %d line(s)",
                     "league" if is_league else "practice", r.course, sorted(players), len(lines))

    def _league_facts(self, r: Round, closing: bool = False) -> list[str]:
        """Each team's league history and standings: Squabbit's when it was read at the round start,
        else the imported spreadsheets'. Plus the week so far and the week's closest to the pin."""
        facts = []
        standings = self._board_standings(r) if r.is_league else None
        for team in r.teams:
            players = self.league.team_players.get(team)
            if not players:
                continue
            label = self.league.team_name(team)
            if not closing:
                here = {p for side in r.sides if side.team == team for p in side.players}
                if here and (away := [p for p in players if p not in here]):
                    # A practice round with part of the team: the team's history still counts, but
                    # the opener read "Corey and I are in the building" off it with Josh alone.
                    facts.append(f"Only {_join_names(sorted(here))} of {label} is playing this round; "
                                 f"{_join_names(away)} is not here. The team facts below are the partnership's "
                                 f"history: credit them to the pair, never say {_join_names(away)} is "
                                 f"playing, here or in the building.")
                facts += self.history.team_facts(label, players, r.format, r.course,
                                                 same_course=self._course_test(r))
                # One season stat per side for opener colour, picked at random among the ones with a
                # sample so it isn't always the same stat and the opener doesn't read like a stat sheet.
                side = next((s for s in r.sides if s.team == team), None)
                if (ssf := self._team_season_facts(side)):
                    facts.append(self.rng.choice(ssf))
            if r.is_league and standings is None:
                facts += self.history.standings_facts(label, players)
        if r.is_league:
            facts += standings or []
            facts += self._week_board_facts(r, closing)
            if not closing:
                facts += self._week_ctp_facts(r)
        return facts

    # ---- the league week: Squabbit's leaderboard and the sim's closest to the pin -------------

    def _week_others(self, r: Round) -> list:
        """This week's results posted on Squabbit by teams not in this round, best first ([] when unknown)."""
        week = r.schedule.get("week")
        if not self.board or not r.is_league or not week:
            return []
        here = {t for t in r.teams if t}
        return [e for e in self.board.posted(week) if e.team not in here]

    def _board_standings(self, r: Round) -> list[str] | None:
        """The season standings from Squabbit's final weeks. None when the board can't say (no fetch,
        no points table), so the history file's standings stand in."""
        b = self.board
        if not b or not b.points_table:
            return None
        table, weeks = b.standings(time.strftime("%Y-%m-%d"))
        if not weeks:
            return ["No league week is final yet this season, so there are no standings to talk about."]
        def where(pts, pl):
            return ("tied " if sum(1 for _, q, _ in table if q == pts) > 1 else "") + ordinal(pl)
        facts = [f"Season standings after {weeks} final week{'s' if weeks != 1 else ''} (from Squabbit): "
                 + ", ".join(f"{where(pts, pl)} {self.league.team_name(t)} with {pts:g} points"
                             for t, pts, pl in table[:3]) + f", of {len(table)} teams."]
        top = {t for t, _, _ in table[:3]}
        for t, pts, pl in table:
            if t in r.teams and t not in top:
                facts.append(f"{self.league.team_name(t)} are {where(pts, pl)} in the standings with {pts:g} points.")
        return facts

    def _week_board_facts(self, r: Round, closing: bool) -> list[str]:
        """Where this week stands on Squabbit: the other teams' posted nets (opener), and where this
        round's net would place among them (closer)."""
        week = r.schedule.get("week")
        if not self.board or not r.is_league or not week or week not in self.board.weeks:
            if closing and r.schedule and r.is_league:
                return ["Where this round places for the week isn't known yet: every team plays the same week "
                        "on its own schedule."]
            return []
        others = self._week_others(r)
        here = [t for t in r.teams if t]
        field = self.board.team_count(week) - len(here)
        names = " and ".join(self.league.team_name(t) for t in here) or "this group"
        if not closing:
            if not others:
                return [f"No other team has posted a week {week} score on Squabbit yet, so {names} can set the "
                        f"number to beat."]
            best = [e for e in others if e.net == others[0].net]
            lead = (" and ".join(self.league.team_name(e.team) for e in best)
                    + f" {'share' if len(best) > 1 else 'have'} the best at net {to_par(others[0].net)}")
            nxt = next((e for e in others if e.net != others[0].net), None)
            return [f"Week {week} so far on Squabbit: {len(others)} of the other {field} teams have posted. {lead}"
                    + (f", then {self.league.team_name(nxt.team)} at net {to_par(nxt.net)}" if nxt else "") + "."]
        facts = []
        for team in here:
            tn = r.team_net(team)
            if not tn or tn[1] < r.holes:
                continue
            label = self.league.team_name(team)
            if not others:
                facts.append(f"By the booth's count {label} finish week {week} at net {to_par(tn[0])}. No other team "
                             f"has posted week {week} on Squabbit yet, so that's the score to beat.")
                continue
            pos, tied = place(tn[0], [e.net for e in others] + [tn[0]])
            facts.append(f"By the booth's count {label} finish week {week} at net {to_par(tn[0])}. Against the "
                         f"{len(others)} other team{'s' if len(others) != 1 else ''} posted on Squabbit so far, that "
                         f"would be {'tied ' if tied else ''}{ordinal(pos)} for the week (Squabbit posts the "
                         f"official result).")
        return facts

    def _week_ctp(self, r: Round, before_id: int | None = None) -> dict[int, tuple[str, float]]:
        """The week's closest tee shot on the green per closest-to-the-pin hole, from every league round
        of this week's course on the sim. Individual-ball weeks only (see Store.week_ctp)."""
        if not r.is_league or not r.ctp_holes or r.format not in ("best_ball", "aggregate"):
            return {}
        ends = [e["end_date"] for e in self.league.data.get("schedule") or [] if e.get("end_date")]
        if not ends:
            return {}
        import datetime

        since = (datetime.date.fromisoformat(min(ends)) - datetime.timedelta(days=120)).isoformat()
        courses = [c for c in (r.course, r.schedule.get("course"), r.schedule.get("gspro_course")) if c]
        return self.store.week_ctp(courses, r.ctp_holes, since, before_id)

    def _closer_week_ctp(self, r: Round) -> dict[int, tuple[str, str, float]] | None:
        """The week's best tee shot per CTP hole from rounds before this one, for the closer to measure
        tonight's against. None outside individual-ball league weeks (the week can't be counted)."""
        if not r.is_league or not r.ctp_holes or r.format not in ("best_ball", "aggregate") \
                or not any(e.get("end_date") for e in self.league.data.get("schedule") or []):
            return None
        return {h: (who, _distance_text(y), y) for h, (who, y) in self._week_ctp(r, before_id=r.shot_floor).items()}

    def _week_ctp_facts(self, r: Round) -> list[str]:
        if not r.is_league or r.format not in ("best_ball", "aggregate"):
            return []
        best = self._week_ctp(r)
        facts = []
        for hole in r.ctp_holes:
            if hole in best:
                who, yards = best[hole]
                facts.append(f"Closest to the pin on hole {hole} this week so far: {who}, {_distance_text(yards)} "
                             f"from the hole.")
            else:
                facts.append(f"Nobody has a tee shot on the green on hole {hole} this week yet.")
        return facts

    async def handle_scorecard(self, event: ScorecardEvent) -> None:
        r = self.round
        if not r or r.ended:
            if event.card.scores != self._idle_card_logged:   # the card stays up after a round: once per change
                self._idle_card_logged = event.card.scores
                log.info("Scorecard read with no round in progress: %s", event.card.scores)
            return
        if not r.apply_scorecard(event.card):
            log.info("Scorecard didn't match the round's players (%s): rows %s", list(r.roster), list(event.card.scores))
            return
        log.info("Round %s scorecard: %s", r.id, {g: dict(sorted(v.items())) for g, v in r.card_scores.items()})
        if self.commentary_on and self.cfg.commentary.closer:
            self._kick_group_closer(r)
        if self.cfg.commentary.segments_on_hole_change and self.state.current_hole \
                and self._wrapped_hole != self.state.current_hole:
            # In a group, the moment the second-to-last side holes out (one side left to putt), pre-
            # build the wrap branched on that last side's finish, with the others' scores fixed. Solo
            # builds earlier, at green-reach, so this is multiplayer-only.
            if len(r.sides) > 1 and self.current_par and not (
                self._wrap_branches and self._wrap_branches.get("hole") == self.state.current_hole
            ):
                remaining = [s for s in r.sides if r.card_score(s, r.seq) is None]
                if len(remaining) == 1:
                    par = self.current_par
                    others = [self._score_fact(s, r.card_score(s, r.seq), par) for s in r.sides
                              if r.card_score(s, r.seq) is not None]
                    self._kick_wrap_branches(remaining[0].key, [par - 1, par, par + 1, par + 2], others)
            # The finished hole's score posts here, ~right after the last putt and well before GSPro's
            # hole change. If the wrap is pre-voiced, play it now so it lands on the green instead of
            # after the between-hole screen; the hole change then sees it's done and skips.
            await self._wrap_after_last_putt(self.state.current_hole, r.seq)
        if r.complete() and self._log_rounds and not r.log_complete:
            # GSPro logs the round's completion at the last putt, a beat before its scorecard shows;
            # give the log follower a moment to catch up before deciding the round goes on.
            for _ in range(10):
                self._check_log_round()
                if r.log_complete or r.ended:
                    break
                await asyncio.sleep(0.2)
        if r.ended:
            return
        if r.complete():
            if self._log_rounds and not r.log_complete:
                # Every hole we were counting is scored but GSPro's round goes on (an 18 counted as 9).
                self._extend_round(r)
            else:
                await self.handle_round_end(RoundEndEvent(reason="final scorecard"))

    async def _wrap_after_last_putt(self, finished_hole, finished_seq: int) -> bool:
        """Fire the pre-made wrap the moment the scorecard shows the hole's score. Lets the hole's
        last reaction queue first (bounded) so the wrap doesn't jump ahead of it."""
        if not self._wrap_branches or self._wrap_branches.get("hole") != finished_hole:
            return False
        await self._drain_shot_tasks(self.cfg.commentary.wrap_wait_seconds)
        await self._drain_speech(self.cfg.commentary.wrap_wait_seconds + 10)
        return self._play_prepared_wrap(finished_hole, finished_seq)

    def _record_round_stats(self, r: Round, reason: str, complete: bool = True) -> dict:
        """Log and store each side's (and individual's) round. A round quit early is stored as
        incomplete, with net to par over just the holes played."""
        self._apply_log_corrections()
        if not complete:
            done = all(r.card_score(side, r.seq) is not None for side in r.sides)
            r.stats_through = max(0, r.seq if done else r.seq - 1)
            if not r.stats_through:
                log.info("Round %s quit before a hole was finished: nothing to record", r.id)
                return {}
        stats = {s.key: r.side_stats(s, self.state.shots) for s in r.sides}
        net = {s.key: (r.net_to_par(s, stats[s.key]) if complete else r.net_to_par_played(s, stats[s.key]))
               for s in r.sides}
        for side in r.sides:
            st = stats[side.key]
            log.info("Round %s over (%s): %s gross %s, strokes %s, net to par %s, FIR %s/%s, GIR %s/%s, putts %s "
                     "over %s holes, penalties %s, scores %s (from %s)%s", r.id, reason, side.label, st.gross,
                     side.strokes, net[side.key], *st.fir, *st.gir, st.putts, st.putt_holes, st.penalties,
                     st.scores, st.source, "" if complete else f"; incomplete, {st.holes_scored} of {r.holes} holes")
        for side in r.sides:
            st = stats[side.key]
            if st.holes_scored:
                self.store.record_round(course=r.course, fmt=r.format, side=side.key, team=side.team, holes=r.holes,
                                        stats=st, strokes=side.strokes, net_to_par=net[side.key],
                                        hole_number=r.hole_number, hole_pars=r.par, is_league=r.is_league,
                                        complete=complete)
        # Per-player FIR/GIR/scrambling/sand-save for individual season stats. Alternate shot isn't
        # attributed to one player, so skip it; otherwise record each individual (distinct names from
        # this round's own shots, so a two-ball team isn't double-counted), skipping any shared-ball
        # (anonymous) player. Scrambling/sand are gated to individual-ball formats at query time.
        if r.format != "alternate":
            anon = set(r.anonymous_players())
            names = {rec.player for rec in self.state.shots if rec.round_id == r.id
                     and rec.player and rec.player != "unknown" and rec.player not in anon}
            for name in names:
                pst = r.player_stats(name, self.state.shots)
                if pst.holes_scored:
                    self.store.record_player_round(player=name, course=r.course, fmt=r.format, stats=pst,
                                                   hole_number=r.hole_number, hole_pars=r.par, is_league=r.is_league,
                                                   complete=complete)
        return stats

    async def handle_round_end(self, event: RoundEndEvent) -> None:
        r = self.round
        if not r or r.ended:
            return
        # The last shot's handler may still be reading: its record goes into the round's totals.
        await self._drain_shot_tasks(self.cfg.commentary.wrap_wait_seconds)
        if r.ended:
            return
        r.ended = True
        self._round_ended_at = time.monotonic()
        self._shots_at_round_end = self.gspro.fires
        self.hole_number = None  # the next round starts its own count
        if self.cfg.health.enabled and self.cfg.source.type != "replay":
            try:
                self._health_round_end(r)
            except Exception:
                log.exception("Post-round health checkup failed")
        if event.abandoned:
            # Quit partway: no sign-off. Pre-built wrap/closer are dropped. The holes played are still
            # recorded (marked incomplete): their per-hole stats count, and a full nine or more counts
            # as a round in the season totals, scored per nine.
            if self._closer_task and not self._closer_task.done():
                self._closer_task.cancel()
            self._closer_branches = self._wrap_branches = None
            self._record_round_stats(r, event.reason, complete=False)
            self._schedule_publish(after_speech=True)
            log.info("Round %s over (%s) after %s of %s holes: no closer", r.id, event.reason, r.seq, r.holes)
            return
        if stats_obj := getattr(self.speech, "audio_stats", None):
            log.info("%s", audio_summary(stats_obj))
        stats = self._record_round_stats(r, event.reason)
        self._schedule_publish()   # the backup, if the sign-off never finishes
        await self._sign_off(r, stats)
        self._schedule_publish(after_speech=True)

    async def _sign_off(self, r: Round, stats: dict) -> None:
        season = {s.key: self.store.season_rounds(s.key) for s in r.sides}
        facts = (r.closing_facts(self.state.shots, season, week_ctp=self._closer_week_ctp(r))
                 + self._league_facts(r, closing=True))
        if not self.cfg.commentary.closer:
            return
        # A closer pre-built on the last hole plays instantly when its branch matches the deciding side's
        # actual final score and every other side finished as it was written; any mismatch or absence
        # falls through to the on-demand closer below.
        cb = self._closer_branches
        side = next((s for s in r.sides if cb and s.key == cb["side"]), None)
        if side is not None and side.key in stats and all(
                stats[k].scores.get(r.holes) == v for k, v in cb.get("fixed", {}).items() if k in stats):
            # The last hole's prepared wrap fires just before this with no yield between them; let it
            # finish airing (bounded) before the closer, so the sign-off follows the wrap instead of
            # its say_prepared_sequence interrupt cutting the wrap off mid-exchange.
            await self._drain_speech(self.cfg.commentary.wrap_wait_seconds + 20)
            if self._play_prepared_closer(stats[side.key].scores.get(r.holes), side.key):
                return
        self._closer_branches = None
        user, facts_text = closer_prompt(facts=facts + self._weather_facts(), recent_lines=self.state.recent_lines)
        # r.ended is set above, so the `system` property would pick practice; pass the round's own
        # variant so a league round's sign-off keeps its stakes framing.
        await self._speak("closer", self.closer_llm, user, facts_text, [], mode="round",
                          system=self._system_league if r.is_league else self._system_practice)

    # ---- publishing the stats page --------------------------------------------------------

    def _schedule_publish(self, after_speech: bool = False) -> None:
        """Publish the stats page after a round, once the sign-off has aired (its lines are stored
        as they're queued, so they're in it). Without `after_speech`, it's the backup timer
        (publish.delay_seconds) in case the sign-off never finishes. A publish that's still waiting
        is replaced by the newer one."""
        p = self.cfg.publish
        if not (p.enabled and p.after_round and p.bucket) or self.cfg.source.type == "replay":
            return
        if self._publish_task and not self._publish_task.done() and not self._publish_lock.locked():
            self._publish_task.cancel()   # still waiting: this one covers it
        self._publish_task = asyncio.create_task(self._publish_after(0 if after_speech else p.delay_seconds,
                                                                     drain=after_speech))

    async def _publish_after(self, delay: float, drain: bool = False) -> None:
        await asyncio.sleep(delay)
        if drain:
            # Not while the sign-off is still being voiced: building the site takes CPU.
            await self._drain_speech(120)
        async with self._publish_lock:
            try:
                await self._publish()
            except Exception:
                log.exception("Stats page publish failed")

    async def _publish(self) -> None:
        p = self.cfg.publish
        names = {key: persona.name for key, persona in self.cfg.personas.items()}
        cmd = [sys.executable, "-m", "commentator.stats_site", "--db", str(self.cfg.db_path),
               "--site", str(Path(self.cfg.log_dir) / "stats-site"), "--bucket", p.bucket, "--prefix", p.prefix,
               "--names", json.dumps(names)] + (["--region", p.region] if p.region else [])
        # Its own process, so building the stats never holds up the booth during the next round.
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=600)
        except asyncio.TimeoutError:
            proc.kill()
            out, code = b"timed out after 10 minutes", -1
        else:
            code = proc.returncode
        text = out.decode("utf-8", "replace").strip()
        if code == 0:
            log.info("Stats page: %s", text.splitlines()[-1] if text else "published")
            self.health.clear("publish.failed", why="published")
        else:
            last = text.splitlines()[-1] if text else f"exit {code}"
            log.warning("Stats page publish failed: %s", text[-2000:])
            if self.cfg.health.enabled:
                self.health.raise_issue("publish.failed", "warn", f"Couldn't publish the stats page: {last}")

    # ---- health checks --------------------------------------------------------------------

    async def _startup_health(self) -> None:
        # The version comes from the log's backlog, so wait (bounded) for the follower to read it.
        for _ in range(240):
            if self._follower is None or self._follower.primed:
                break
            await asyncio.sleep(0.25)
        try:
            await asyncio.to_thread(self._health_checks, "startup")
        except Exception:
            log.exception("Startup health check failed")

    def _health_checks(self, when: str) -> None:
        """The files the booth follows exist; GSPro's version and the screen size are the ones the booth
        last ran clean against (a change raises a warning a clean round clears)."""
        h = self.health
        files = {}
        feed = type(self.source).__name__
        if self._log_truth or feed in ("GsproLogShotSource", "ConnectorWithLogFallback"):
            files["GSPro log"] = self.cfg.source.gspro_log
        if feed in ("ConnectorLogSource", "ConnectorWithLogFallback"):   # not GSPro's log alone
            files["connector log"] = self.cfg.source.connector_log
        check_files(h, files)
        if self._log_truth:
            check_changed(h, "gspro_version", self.gspro.version, "GSPro's version")
        if self.screen:
            try:
                w, ht = self.screen.monitor_size()
                check_changed(h, "screen", f"{w}x{ht}", "the screen size")
            except Exception:
                log.exception("Couldn't read the screen size")
        issues = h.issues()
        log.info("Health check (%s): GSPro %s; %s", when, self.gspro.version or "version unknown",
                 "; ".join(f"{i.level} {i.message}" for i in issues) if issues else "all clear")

    def _health_shot(self, before: Hud, after: Hud, is_putt: bool, ocr: bool) -> None:
        """Count this shot's reads, and flag at once (not at round end) a data tile that never reads or
        a GSPro log that isn't moving: either breaks the rest of the round."""
        if not self.cfg.health.enabled or not self.round or self.round.ended:
            return
        hc, h = self._hc, self.health
        hc.shots += 1
        # Not gated on the ball being seen to land: that's read off the carry tile, the very thing checked.
        if ocr and not is_putt and (before.lie or "").lower() != "green":
            hc.full_shots += 1
            if hc.full_shots == 1 and self.screen is not None and self.cfg.source.type != "replay":
                # A full frame with the tiles holding real values, for `fixture save/check` over ssh.
                asyncio.create_task(asyncio.to_thread(self._save_fixture_frame), name="fixture-frame")
            seen = set(before.tiles) | set(after.tiles)
            hc.tiles_seen |= seen
            for t in self.cfg.health.required_tiles:
                if t not in seen:
                    hc.tiles_missing[t] = hc.tiles_missing.get(t, 0) + 1
            if hc.full_shots >= 3:
                if absent := [t for t in self.cfg.health.required_tiles if t not in hc.tiles_seen]:
                    h.raise_issue("round.tiles_absent", "fail",
                                  f"no {', '.join(repr(t) for t in absent)} tile read in {hc.full_shots} shots: "
                                  f"a data tile changed or the HUD moved?")
                else:
                    h.clear("round.tiles_absent", "tiles read")
        if self._log_truth and self.cfg.source.type != "replay" and hc.shots >= 3:
            if self.gspro.fires == hc.fires_at_start:
                h.raise_issue("round.log_silent", "fail",
                              f"GSPro's log logged no shots while the booth saw {hc.shots}: is it the right file?")
            else:
                h.clear("round.log_silent", "log moving")

    def _keep_fixture_frame(self, kind: str, frame=None) -> None:
        """Save a frame of this kind for `fixture save` (scorecard, setup tabs), at most every ten
        minutes per kind, off the event loop."""
        if self.screen is None or self.cfg.source.type == "replay":
            return
        now = time.monotonic()
        if now - self._fixture_saved_at.get(kind, -1e9) < 600:
            return
        self._fixture_saved_at[kind] = now
        asyncio.create_task(asyncio.to_thread(self._save_fixture_frame, kind, frame), name=f"fixture-{kind}")

    def _save_fixture_frame(self, kind: str = "shot", frame=None) -> None:
        try:
            frame = frame if frame is not None else self.screen.grab()
            d = Path(self.cfg.log_dir)
            d.mkdir(parents=True, exist_ok=True)
            stem = "fixture-frame" if kind == "shot" else f"fixture-frame-{kind}"
            frame.save(d / f"{stem}.png", compress_level=1)
            (d / f"{stem}.json").write_text(json.dumps({
                "captured": time.strftime("%Y-%m-%d %H:%M:%S"), "gspro_version": self.gspro.version,
                "course": self.round.course if self.round else None}), encoding="utf-8")
        except Exception:
            log.exception("Couldn't save the fixture frame")

    def _health_round_end(self, r: Round) -> None:
        """The post-round checkup: judge the round's reads, raise or clear the round's findings, and
        keep the report card. A round whose checks all pass also proves a pending change (a GSPro
        update, a new screen) didn't break anything."""
        h = self.health
        found, report = evaluate_round(self._hc, self.gspro.fires, dict(self.gspro.kinds), list(self.gspro.unparsed),
                                       self.cfg.health.required_tiles, self._log_truth,
                                       bool(self.screen and self.cfg.screen.ocr))
        report = {"round": r.id, "course": r.course, "ended": time.strftime("%Y-%m-%d %H:%M:%S"),
                  "gspro_version": self.gspro.version, **report}
        for key, level, msg in found:
            h.raise_issue(key, level, msg)
        if report["checked"]:
            h.clear_prefix("round.", keep={k for k, _, _ in found}, why="passed this round")
            if not found:
                h.clear_verified()
        h.record_round(report)
        log.info("Health checkup, round %s: %s", r.id, report_line(report))

    # ---- generation and checking ----------------------------------------------------------

    async def _complete(
        self, kind: str, llm: LLMClient, user: str, images: list[bytes], schema: dict | None = None,
        system: str | None = None,
    ) -> str:
        # `system` lets the opener/closer force their round's variant (league/practice): they compose
        # before (opener) or around the edge of (closer) `self.round` being the live round, so the
        # property can't be trusted to pick the right stakes framing for them.
        if not self.commentary_on:
            # Every model call funnels through here; with commentary off it answers "nothing to say",
            # so the opener, plans, reactions, wraps and closer all come out empty and are skipped.
            return "{}"
        result = await llm.complete_json(system or self.system, user, schema or self.schema, images)
        self.usage.record(kind, llm, result)
        log.info("%s call (%s:%s): %.1fs, %s in / %s out tokens", kind, llm.provider, llm.model,
                 result.latency_seconds, result.input_tokens, result.output_tokens)
        return result.text

    async def _generate(self, kind: str, llm: LLMClient, user: str, images: list[bytes],
                        system: str | None = None) -> list[Line]:
        raw = await self._complete(kind, llm, user, images, system=system)
        return parse_lines(raw, self.speakers, self.cfg.commentary.max_words_per_line, self._persona_names)

    def _grounded(
        self, lines: list[Line], facts_text: str, mode: str = "result", seen_image: bool = False
    ) -> tuple[list[Line], list[str]]:
        good, problems = [], []
        # Small models lift the prompt's example lines verbatim, and settle into catchphrases.
        # 5 words against the examples catches lifted lines; 6 against recent lines lets common
        # phrasing ("in the air with a") through while still stopping catchphrases.
        recent = [l.text for l in self.state.recent_lines]
        banned = self._banned_names() if mode not in ("round", "news") else []
        for line in lines:
            check = check_line(line.text, facts_text, self.league_text, mode=mode, seen_image=seen_image,
                               banned_names=banned)
            if (meta := meta_talk(line.text)):
                check.ok = False
                check.problems.append(f'"{meta}": talks about the booth\'s own rules or data, not the golf')
            if bare_acknowledgement(line.text):
                check.ok = False
                check.problems.append("a bare acknowledgement adds nothing")
            for player in self._spent_notes():
                if uses_note(line.text, self._note_keys[player]):
                    check.ok = False
                    check.problems.append(f"{player}'s note has been used enough this round")
            if self.cfg.commentary.block_repeats:
                if phrase := repeats(line.text, [self.system_examples]) or repeats(
                    line.text, recent + [g.text for g in good], n=6
                ):
                    check.ok = False
                    check.problems.append(f'reuses the phrase "{phrase}" from earlier or the examples')
                # The opener and wrap-up are templated ("welcome to...", "the defending champions"), so
                # the season-wide repeat check only forces costly retries there; it guards shot lines.
                elif mode not in ("round", "news") and (said := ngrams(line.text, 6) & self._season_phrases):
                    check.ok = False
                    check.problems.append(f'"{" ".join(min(said))}" was already said this season')
            # Each line stands on its own: a valid line is kept even when a sibling was rejected. (An
            # earlier "drop the reply to a dropped line" rule was pulled: it couldn't tell a dependent
            # reply from a standalone line that just addresses the partner, and ate good commentary.)
            if check.ok:
                good.append(line)
                self.stats["lines_ok"] += 1
            else:
                problems += check.problems
                self.stats["lines_rejected"] += 1
                self.rejections.append((line.text, check.problems))
                log.info("Rejected line %r: %s", line.text, "; ".join(check.problems))
        return good, problems

    async def _compose(
        self, kind: str, llm: LLMClient, user: str, facts_text: str, images: list[bytes], mode: str = "result",
        system: str | None = None, must: str | None = None,
    ) -> list[Line]:
        """Generate, ground, and retry a line without airing it. `_speak` airs the result now; the
        pre-round opener holds it until the tee confirms the round. `system` forces a round variant.
        `must` is a word the lines have to say (a mulligan the model would otherwise only hint at)."""
        seen = bool(images)
        lines, problems = self._grounded(await self._generate(kind, llm, user, images, system=system),
                                         facts_text, mode, seen)
        if must and lines and not any(must in l.text.lower() for l in lines):
            problems = problems + [f'none of the lines says "{must}": say it in so many words']
        if problems:
            # One retry with the reasons; a second failure means those lines just aren't said.
            self.stats["retries"] += 1
            retried, still_bad = self._grounded(
                await self._generate(kind, llm, retry_prompt(user, problems), images, system=system),
                facts_text, mode, seen
            )
            if retried:
                lines = retried
            if still_bad:
                self.stats["dropped_after_retry"] += len(still_bad)
        return lines

    def _air_lines(self, kind: str, lines: list[Line], shot_id: int | None = None) -> None:
        if not lines:
            return
        self.state.record_lines(lines)
        self.speech.say(lines)
        self._write_transcript(lines, kind, shot_id)

    async def _speak(
        self, kind: str, llm: LLMClient, user: str, facts_text: str, images: list[bytes], mode: str = "result",
        system: str | None = None, must: str | None = None, shot_id: int | None = None,
    ) -> None:
        self._air_lines(kind, await self._compose(kind, llm, user, facts_text, images, mode, system=system,
                                                  must=must), shot_id)

    def _note_form_mentions(self, lines: list[Line]) -> None:
        """Which round-sheet angles a line just used, for the side the sheet last went to."""
        if not self._form_pending:
            return
        round_id, side_key, players, seq = self._form_pending
        if not self.round or self.round.id != round_id:
            return

        def names(side) -> set[str]:
            return {n.split()[0].casefold() for n in side.players if n} | {side.label.casefold()}

        ours = next((x for x in self.round.sides if x.key == side_key), None)
        if ours is None:
            return
        mine = names(ours)
        others = set().union(*(names(x) for x in self.round.sides if x.key != side_key),
                             {n.split()[0].casefold() for n in self.league.player_info if n}) - mine

        def says(text: str, words: set[str]) -> bool:
            return any(re.search(rf"\b{re.escape(w)}\b", text, re.I) for w in words)

        # Each line's subject is whoever it names, else the line before's; only stat lines about the
        # side the sheet went to count (a line naming them and another about someone else don't add up).
        subject, topics = None, set()
        for line in lines:
            me, them = says(line.text, mine), says(line.text, others)
            subject = None if me and them else "ours" if me else "theirs" if them else subject
            if subject == "ours":
                topics |= set(form_topics(line.text))
        topics = sorted(topics)
        if not topics:
            return
        self._form_said.setdefault((round_id, side_key), []).append((seq, topics))
        self.store.record_form_mentions(players, topics)
        self._form_pending = None

    def _write_transcript(self, lines: list[Line], kind: str = "shot", shot_id: int | None = None) -> None:
        # The handler's own shot id beats the shared one, which the next shot may have moved on.
        if shot_id is None and kind in ("shot", "flight"):
            shot_id = self._shot_id
        self.store.record_lines(kind, lines, shot_id)
        self._note_form_mentions(lines)
        for line in lines:
            self._season_phrases |= ngrams(line.text, 6)
            for player, keys in self._note_keys.items():
                if uses_note(line.text, keys):
                    self._note_uses[player] = self._note_uses.get(player, 0) + 1
        stamp = time.strftime("%H:%M:%S")
        with self.transcript.open("a", encoding="utf-8") as f:
            for line in lines:
                name = self.cfg.personas[line.speaker].name
                f.write(f"{stamp} {name}: {line.text}\n")
