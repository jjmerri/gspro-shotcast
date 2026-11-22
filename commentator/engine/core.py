"""The Engine: what it holds (__init__) and its main loop. Its work is in the parts beside this
file, one per job, which it is put together from."""
import asyncio
import os
import random
import re
import threading
import time
from collections import Counter, deque
from pathlib import Path
from typing import Any

from .common import BoothCall, HoleWatch, PendingOpening, PreparedChat, PreparedNews, log
from .. import round_file
from ..alerts import Alerts
from ..board import Board
from ..chat import ChatMessage, ChatTable, cursor_at
from ..config import Config
from ..flight_pick import FlightPlan
from ..grounding import ngrams, note_keywords
from ..health import Health, RoundCounters
from ..history import History
from ..league import League, OnAir
from ..llm import LLMClient
from ..models import HoleChangeEvent, Line, PreShotEvent, RoundEndEvent, RoundStartEvent, ScorecardEvent, ShotEvent
from ..news import GolfNews
from ..prompt_log import PromptLog
from ..prompts import output_schema, system_prompt
from ..round import Round
from ..scores import LiveScores
from ..screen import Screen
from ..sources import gspro_files
from ..speech import SpeechQueue
from ..state import SessionState, ShotRecord
from ..store import Store
from ..trivia import CourseTrivia
from ..usage import UsageLog, audio_summary
from ..weather import Weather
from ..sources.base import ShotSource
from ..sources.gspro_log import GsproLogFollower, GsproState
from .hud_watch import HudWatchMixin
from .round_detect import RoundDetectMixin
from .services import ServicesMixin
from .recap import RecapMixin
from .asides import AsidesMixin
from .round_file import RoundFileMixin
from .chat import ChatMixin
from .on_air import OnAirMixin
from .facts import FactsMixin
from .identity import IdentityMixin
from .preshot import PreshotMixin
from .shot import ShotMixin
from .shot_reads import ShotReadsMixin
from .results import ResultsMixin
from .in_flight import InFlightMixin
from .course import CourseMixin
from .squabbit import SquabbitMixin
from .league_round import LeagueRoundMixin
from .round_start import RoundStartMixin
from .setup_watch import SetupWatchMixin
from .week import WeekMixin
from .round_end import RoundEndMixin
from .live import LiveMixin
from .upkeep import UpkeepMixin
from .generation import GenerationMixin


class Engine(HudWatchMixin,
             RoundDetectMixin,
             ServicesMixin,
             RecapMixin,
             AsidesMixin,
             RoundFileMixin,
             ChatMixin,
             OnAirMixin,
             FactsMixin,
             IdentityMixin,
             PreshotMixin,
             ShotMixin,
             ShotReadsMixin,
             ResultsMixin,
             InFlightMixin,
             CourseMixin,
             SquabbitMixin,
             LeagueRoundMixin,
             RoundStartMixin,
             SetupWatchMixin,
             WeekMixin,
             RoundEndMixin,
             LiveMixin,
             UpkeepMixin,
             GenerationMixin):
    """Three moments per shot: a setup line while the player addresses the ball, a call
    while it's in the air, and a reaction once it stops."""

    PHRASE_MEMORY_DAYS = 365   # how far back a 6-word phrase can't be said again

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
        chat_llm: LLMClient | None = None,
        health: Health | None = None,
        commentary_unavailable: str | None = None,
        news_llm: LLMClient | None = None,
    ) -> None:
        self.cfg = cfg
        # Is the booth still reading GSPro right? In memory unless the caller gives it a file.
        self.health = health or Health()
        self._hc = RoundCounters()      # this round's reads, for the post-round checkup
        self._follower: GsproLogFollower | None = None
        # GSPro's round file (currentRound.dat, 3.59+): the one watch of it, its strokes as last read, and
        # an event set each time it's read anew (see _round_file_loop, _wait_for_file).
        self._round_watch: gspro_files.RoundFileWatch | None = None
        self._round_watch_shared = False
        self._file_tracks: list[round_file.Track] = []
        self._file_entries: list = []
        self._file_version: int | None = None
        self._file_changed = asyncio.Event()
        self._round_file_kick = False   # a shot GSPro's file has was just recorded: apply the file now
        self._file_nudge = asyncio.Event()   # set to have _round_file_loop look now
        # shot id -> which of (carry, total) GSPro's round file gave live
        self._file_shot_ids: dict[int, tuple[bool, bool]] = {}
        self._file_pins: dict[str, int] = {}   # ShotID -> the shot the live follow tied it to
        self._file_pin_rounds: dict[str, int] = {}   # a pinned ShotID -> its GSPro RoundID
        # a ShotID _refile_from_round_file moved a shot for -> (the shot, where it was, where it went)
        self._file_unrefile: dict[str, tuple] = {}
        self._hole_watch: HoleWatch | None = None   # the HUD watch's banner tracker (_watch_hud)
        self._file_moved_to: int | None = None   # the hole GSPro's round file moved the group to, banner unseen
        self._file_refiled: set[int] = set()   # shots _refile_shot labelled before their hole's banner was read
        # shots the round file named (recorded without a player) -> ((hole, hole_seq, shot_number) as
        # recorded, the stored (player, hole, shot_number) last taken)
        self._file_named: dict[int, tuple] = {}
        self._file_claims: dict[str, int] = {}   # ShotID -> the impact count of the shot that took it
        # a ShotID whose hole may yet end on it with no log to say (a scramble's pick, later) ->
        # (the shot, its tag, record, putt?, monotonic deadline): _settle_file_ends
        self._file_ends_due: dict[str, tuple] = {}
        # The round's shots GSPro's log credited to a slot, fire -> (slot, launch numbers), and what
        # _learn_file_slots last worked them out from (the round, how many, the file's version).
        self._file_credits: dict[int, tuple[int, dict]] = {}
        self._file_slots_from: tuple | None = None
        self._booth_wall0 = time.time()   # a round file last written before this is no live GSPro's
        self.league = league
        self._own_handicaps_task: asyncio.Task | None = None   # (_refresh_own_handicaps)
        self._own_handicaps_wanted = False
        self._own_handicaps_inline = 0   # counts inline workings out, so an older thread's result is dropped
        self.source = source
        if hasattr(source, "on_impact"):
            source.on_impact = self._capture_impact
        self._club_feed = None
        if source is not None and cfg.source.club_feeds and cfg.source.type != "replay":
            from ..sources.club_feeds import ClubFeed

            self._club_feed = ClubFeed.from_config(cfg.source)
            self._club_feed.connection = lambda: self.gspro.lm_connection
            # GSPro 3.59's log shots can air straight from a monitor's line (sources/gspro_log_shots.py).
            if hasattr(log_source := getattr(source, "gspro_log", source), "club_feed"):
                log_source.club_feed = self._club_feed
        self.live_llm = live_llm
        self.segment_llm = segment_llm
        self.flight_llm = flight_llm or live_llm
        self.preshot_llm = preshot_llm or live_llm
        self.round_llm = round_llm or segment_llm
        # The opener is raced against the course load (fast model); the closer is pre-fetched on the
        # last hole, so it can be a richer, slower model. Both fall back to round_llm.
        self.opener_llm = opener_llm or self.round_llm
        self.closer_llm = closer_llm or self.round_llm
        self.chat_llm = chat_llm or live_llm
        self.news_llm = news_llm or self.closer_llm   # news, trivia and score asides
        self.speech = speech
        # Commentary off records shots and rounds but never calls a model or speaks (see _complete).
        # Why the model and voice clients couldn't be built (missing API keys): recording only, and the
        # booth can't be put on air.
        self.commentary_unavailable = commentary_unavailable
        self.commentary_on = cfg.commentary.enabled and not commentary_unavailable
        if commentary_unavailable and cfg.health.enabled:
            self.health.raise_issue("commentary.unavailable", "warn", "Commentary is on in config.yaml but "
                                    f"can't start: {commentary_unavailable}. Recording only.")
        else:
            self.health.clear("commentary.unavailable", why="commentary clients built or commentary off")
        if league is not None and speech is not None:
            self.speech.spoken = league.spoken
        if speech is not None and not self.commentary_on:
            self.speech.muted = True
        self.screen = screen
        self.usage = UsageLog(cfg.log_dir)
        # Built when the booth first goes on air, and only used on air (Jev is a paid model call).
        self.minimap = self._make_minimap() if self.commentary_on else None
        self._minimap_tried = self.commentary_on
        self._minimap_tasks: set[asyncio.Task] = set()   # reads in flight, cancelled on going off air
        self._last_minimap: tuple | None = None   # (image, to_pin, tee, read) of the last minimap read
        self._putt_read_ok = (self.cfg.putt_read.enabled and self.screen is not None
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
        self._news_aired: list[int] = []            # round hole seqs an aside played on
        self._news_used: set[str] = set()           # stories aired this session (for a round with no known players)
        self._news_moment: dict | None = None       # the last shot, for the aside that may follow it
        # Live scores and course trivia: more asides, sharing the news asides' slots and spacing.
        self.scores = LiveScores(cfg.scores.tours, cfg.scores.teams) if cfg.scores.enabled and self.commentary_on \
            else None
        self.trivia = CourseTrivia(cfg.trivia.pages) if cfg.trivia.enabled and self.commentary_on else None
        self.bookings = self._make_bookings()
        self._bookings_task: asyncio.Task | None = None
        self._bookings_fetched = float("-inf")
        self._score_holes: list[int] = []           # round hole seqs a live score is written for as they start
        self._score_said: dict[str, list[str]] = {}  # event key -> what the booth said about it, this session
        self._score_written: set[str] = set()       # "key@version": scores already written up, this session
        self._aside_tasks: set[asyncio.Task] = set()
        # The site stack's table, for the live chat and the phone alerts, each on its own.
        c = cfg.chat
        table = ChatTable(c.table, cfg.publish.region) if c.table and cfg.source.type != "replay" else None
        # The stats page's live chat (docs/tracking.md#live-chat); None when it's off.
        self.chat = table if c.enabled else None
        # The page's alerts on phones (alerts.py), through the table's subscriptions.
        self.alerts = Alerts(cfg.log_dir, table) if table is not None and c.alerts else None
        self._chat_cursor = cursor_at(time.time())    # the last message read (none before the booth started)
        self._chat_log: list[tuple[ChatMessage, float]] = []   # the recent conversation: (message, read at)
        self._chat_talked = ""                        # the newest message id the booth has talked about
        self._chat_written = ""                       # the newest message id a take was written with
        self._chat_ready: PreparedChat | None = None
        self._chat_task: asyncio.Task | None = None
        self._chat_glancing: asyncio.Task | None = None
        self._shots_unrecorded: dict[int, float] = {}   # id(event) -> when struck, for shots being handled
        self._chat_read_at = time.monotonic()         # when the chat was last read (or opened, unread)
        self._chat_aired_at = float("-inf")
        self._chat_aired_hole: tuple | None = None    # (round, hole seq) the booth last talked about the chat on
        self._chat_openers: list[str] = []            # the first lines of the chat takes aired this session
        self._chat_live: bool | None = None           # what the table was last told: on air or not
        self._chat_beat = float("-inf")
        self._chat_round: int | None = None           # the round the chat was last open for
        # The round the "we're live" alert went out for, and whether as a league or tournament one
        # (a tournament is only known after the first shot: the league-only phones hear then).
        self._alerted_live: tuple[int, bool] | None = None
        self._chat_since = ""                         # when that round's chat opened (a cursor_at key)
        self._chat_sent_since: str | None = None      # the one the table was last told
        self._aside_nines: set[int] = set()          # nines of this round whose asides are written
        self._trivia_planned = 0                     # trivia asides written this round (per_round caps it)
        self._signature_told: tuple | None = None    # (round id, hole, player) the famous-hole note went to
        # The booth's memory of earlier rounds, for one round: each side's rows, and which hole-history
        # and last-round facts went out (so they're rationed, and a rebuilt setup keeps its own).
        self._recall: dict[str, Any] = {"round": None}
        self.speakers = list(cfg.personas)
        self._persona_names = [p.name for p in cfg.personas.values()]  # for stripping leaked speaker labels
        self.schema = output_schema(self.speakers)
        # Three cached variants: a practice round has no stakes on the line, a tournament one has its
        # score but no league stakes (see system_prompt). The `system` property picks per the current
        # round; each is byte-identical across calls within a round type, so provider prompt-caching
        # still holds.
        self._system_league = system_prompt(cfg, league, is_league=True)
        self._system_practice = system_prompt(cfg, league, is_league=False)
        self._system_tournament = system_prompt(cfg, league, is_league=False, tournament=True)
        self.league_text = league.prompt_text()
        # The example lines a model might lift verbatim; the BAD ones are what not to say, so a
        # line sharing words with them isn't copying.
        examples = self._system_league.split("Examples", 1)[-1].split("League notes:", 1)[0]
        self.system_examples = "\n".join(l for l in examples.splitlines() if not l.startswith("BAD"))
        # Counters and samples read by the eval command.
        self.stats: Counter[str] = Counter()
        self.rejections: list[tuple[str, list[str]]] = []
        self.flight_latencies: list[float] = []
        if hasattr(self.speech, "usage"):
            self.speech.usage = self.usage   # per-request voice cost/latency rows
        if hasattr(getattr(self.speech, "tts", None), "load_usage"):
            self.speech.tts.load_usage(self.usage)   # this month's characters by voice provider
        Path(cfg.log_dir).mkdir(parents=True, exist_ok=True)
        self.transcript = Path(cfg.log_dir) / f"transcript-{time.strftime('%Y-%m-%d')}.txt"
        self.prompt_log = PromptLog(cfg.log_dir)
        self._events: asyncio.Queue[ShotEvent | HoleChangeEvent | PreShotEvent | None] = asyncio.Queue()
        self._preshot_task: asyncio.Task | None = None
        self._preshot_model_task: asyncio.Task | None = None
        # Wrap recaps pre-generated and pre-voiced during a putt, keyed by the score the hole might
        # finish on, so the hole wrap plays instantly on the hole change with no model call.
        self._wrap_branches: dict | None = None
        self._wrap_task: asyncio.Task | None = None
        self._hole_speech_mark: tuple[int, int, int] | None = None  # (round, hole seq, speech exchange) at its intro
        self._wrapped_hole: str | None = None  # hole already wrapped (on the scorecard), so the hole change doesn't repeat it
        # The round closer, pre-generated and pre-voiced on the last hole before the final putt,
        # branched by the deciding side's possible final score (solo rounds only). Played at round end
        # only when the actual final score matches a branch, else the on-demand closer runs.
        self.last_hole_result: str | None = None  # GSPro's last result banner (Birdie/Par/...), if read
        self._shot_id: int | None = None
        # The lie read at each setup, while the player stood over the ball, by _setup_key: the impact
        # read comes ~3 s after the swing, when the lie box may already show where the ball landed.
        self._setup_lies: dict[tuple, str] = {}
        self._setup_screen_lies: dict[tuple, str | None] = {}   # what the screen read where GSPro's file overrode it
        self._setup_slopes: dict[tuple, tuple] = {}   # (up/down, side, side degrees) by setup key
        # A lie the GSPro log filled in at setup when OCR missed it. The log has where the ball LANDED,
        # so this only fills an empty impact read; it never overrides one like a setup read does.
        self._setup_log_lies: dict[tuple, str] = {}
        # (who, shot) -> whether that stroke's setup showed the distance in yards: GSPro shows yards off
        # the green and feet on it. Kept apart from the distance, which switches to feet when the player
        # changes to putt mode, so a later read in feet can't hide a setup off the green.
        self._setup_yards: dict[tuple, bool] = {}
        # (who, shot) -> id of that player's swing before it, at its setup: a mulligan replays a stroke
        # that was set up, while one played before its setup settled leaves the HUD showing it a moment.
        self._set_up: dict[tuple, int | None] = {}
        self._box_since_swing: tuple | None = None   # (swing count, player) of the last setup read
        self._shot_seq = 0
        self._tree_called = 0   # the _shot_seq whose tree hit was called in flight
        # Hazard map and pre-written mid-flight lines from the latest pre-shot look.
        self.plan: FlightPlan | None = None
        # In-memory unless the caller passes the season database; replays use made-up shots.
        self.store = store or Store(None)
        if hasattr(self.speech, "on_aired") and hasattr(self.store, "mark_aired"):
            self.speech.on_aired = self.store.mark_aired   # each logged line's air time, for the reels' cuts
        self._publish_task: asyncio.Task | None = None
        self._publish_lock = asyncio.Lock()
        # The live stream on the stats page: OBS's output, mirrored to the bucket (live_stream.py).
        self.live_mirror = None
        self.live_local = None   # the stream kept on the PC (live_stream.Mirror with a LocalRecorder)
        self._live_stop_task: asyncio.Task | None = None
        self._obs_lock = threading.Lock()
        self._live_last_shot = time.monotonic()
        self._live_prerolled = False        # on air for the next round's setup: the last one's sign-off doesn't stop it
        self._live_idle_session: str | None = None   # the mirror session the idle watch stopped
        self._live_resume_on_shot = False   # stopped for being idle mid-round: the next shot restarts it
        # Every 6-word phrase said in the last year, so a line can't come back weeks later (not every
        # one ever said: years of a league's lines took seconds to read at each start).
        self._season_phrases: set[tuple[str, ...]] = set()
        for text in self.store.spoken_lines(since=time.time() - self.PHRASE_MEMORY_DAYS * 86400):
            self._season_phrases |= ngrams(text, 6)
        self.carry = self.store.carry_model(cfg.commentary.lie_carry_factors, cfg.commentary.course_altitude_ft)
        self.current_course: str | None = None
        self._hole_snaps: set[tuple[str, int]] = set()   # (course, hole) minimaps kept this session
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
        self._pen_called: dict[tuple[int, int], int] = {}    # of those, told on the shot's own call (4.0)
        # A player's personal note ("built this booth") is a callback, not a catchphrase: count the
        # aired lines that lean on it this round, and stop it after NOTE_USES_PER_ROUND.
        names = {w for n in [*league.players, *league.nicknamed().values()] for w in re.findall(r"[a-z]+", n.lower())}
        self._on_air_cache: tuple[tuple[str, ...], OnAir] | None = None
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
        self._hazards_done: set[int] = set()   # fires whose penalty-area entry point is stored
        self._picks_done: set[int] = set()     # fires whose scramble pick (used or not) is stored
        self._unnamed: set[int] = set()        # fires whose shooter the log has said, recorded with no name
        self._renames: set[int] = set()        # fires named by a slot order since changed: renamed once it's known
        self._picked_fires: dict[int, int | None] = {}   # fire -> shot id of each row a GSPro pick-up was given to
        # fire -> (GSPro slot, hole) its log line gave, kept as long as its row (GSPro's fire info goes sooner)
        self._fire_log: dict[int, tuple[int, int | None]] = {}
        self._resettle: set[int] = set()       # fires whose result a re-map voided: graded however old, while the log has it
        # Log-named rows of a guessed slot order, kept for the round once _fire_rows lets them go: a later
        # re-map renames them too.
        self._slot_archive: dict[int, tuple[int | None, ShotRecord, int, int | None]] = {}
        self._outcomes_due: set[int] = set()   # fires named after the fact, their result still to read
        self._row_source: dict[int, str] = {}  # fire -> where its recorded name came from
        self._reconciled_holes: set[tuple] = set()   # (round id, hole) whose unnamed strokes were placed
        self._owners_due: set[int] = set()     # fires recorded this round whose shooter the log hasn't said
        self._backup_lock = asyncio.Lock()
        self._backup_tasks: set[asyncio.Task] = set()
        # Recorded shots still waiting on their start position: GSPro attaches it only at the shooter's
        # score line, logged once the ball stops, and a putt is recorded before that.
        self._starts_due: set[int] = set()
        self._totals_due = False   # a logged position or mulligan since totals were last worked out from the log
        self._pickups_seen = 0   # GSPro pick-ups (score capped at the maximum) already applied
        # round id -> {(name, hole of the round): the log's (slot, hole)}: holes conceded on the tee
        self._tee_concedes: dict[str, dict[tuple[str, int], tuple[int, int]]] = {}
        self._pick_gimmes_seen = 0   # scramble picks inside the gimme circle already credited
        self._wrong_holes_seen = 0   # GSPro's wrong-hole score corrections already noted
        self._marks_due: list[tuple[str, int, int]] = []   # pickups/picks waiting on a shot's name
        self._void_watch: set[int] = set()   # shots whose result watch followed the wrong player's line
        self._before_result: dict[int, tuple] = {}   # a shot's finish as it was before its outcome landed
        self._conceded_ids: set[int] = set()   # shots whose conceded putt row is stored
        self._team_finished: set[int] = set()   # shared-ball shots given the team's next stroke as their finish
        # Scramble pick screens read off the screen: ({player index: fire}, {index: GSPro name}, cards,
        # read at), each card written to its ball's row once that row is recorded (the unpicked ball's
        # only finish).
        self._pick_cards_due: list[tuple[dict, dict, dict, float]] = []
        # Those written, watched a while yet for a penalty GSPro's file has only later: (balls, cards, when
        # queued, GSPro index -> the finish written).
        self._pick_cards_kept: list[tuple[dict, dict, float, dict]] = []
        self._gimme_ids: dict[int, int] = {}   # shot id -> its conceded putt row's id
        self._form_said: dict[tuple, list[tuple[int, list[str]]]] = {}   # (round, side) -> [(hole of round, angles)]
        self._form_pending: tuple | None = None   # (round, side, players, hole of round) the sheet last went to
        self._rehit_penalised: set[int] = set()   # shots whose missing re-hit penalty the booth added
        self._lie_corrected: set[int | None] = set()   # shots whose called finish the booth has corrected
        self._mulligans_called: set[int] = set()   # id() of swings already announced as taken back
        # Each index's last recorded shot (shot_id, ShotRecord, log hole, penalties then), so the next
        # shot's lie (where it was played from) can correct where that one came to rest.
        self._last_row: dict[int, tuple] = {}
        # Each player's latest strokes, in shot order, so a full shot can wait for the next stroke to say
        # how high it came to rest (that stroke's pin elevation, as the pin doesn't move) before it
        # teaches the carry model. Shot handlers can finish out of order, so entries are kept sorted by
        # shot sequence: player -> [[seq, (round, hole, shot number), elevation, to pin, observe args
        # (None once learned, or for a putt), (shared-ball format?, balls dropped so far)]].
        self._carry_trail: dict[str, list] = {}
        # Per player, a commentator's call (commentator key, predicted make?, hole at commit) on that
        # player's current putt, committed only when the pre-shot line airs and graded against the
        # result when the stamped hole matches. Keyed by player so a partner's shot can't bin it.
        self._pending_prediction: dict[str, tuple[str, bool, str | None]] = {}
        # The calls the booth made out loud this round: the only ones a line may hold a commentator to.
        # A wrap once turned "a chance to finish with a clean statement" into "you've called this a
        # clean finish", and the closer built on it.
        self._calls: list[BoothCall] = []
        self._pred_turn = 0
        self._pred_rested = True   # False right after a call: the next putt never gets one
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
        self._setup_began_at = float("-inf")   # when this setup was first seen (a tab lit since: it could read them)
        # (when, (start, count)) of the latest HOLES-tab nines the quick sampler caught (see _sample_setup_tabs).
        self._holes_snap: tuple[float, tuple[int, int]] | None = None
        self._opener_prepared_holes: tuple[int, int] | None = None
        # GSPro's game mode, off the setup screen's MATCH SETTINGS tab: "scramble", "shamble",
        # "alternate", or None (stroke play, GSPro's default, or a mode that shares no ball). Only a
        # scramble sets the round's format; leagues mostly leave GSPro on stroke play and score their
        # format in an app. A shamble or alternate shot only stops the log calling it a scramble.
        self._setup_format: str | None = None
        # The PLAYERS tab's slots (positional names) and each one's name-pill tint, the team colour
        # (see _setup_colour_teams); None until read.
        self._setup_tints: tuple[list[str | None], list[tuple[int, int, int] | None]] | None = None
        self._setup_mode_read = False   # the MATCH SETTINGS game mode was read (None then means stroke play)
        self._setup_format_at = float("-inf")   # when the game mode last changed
        self._opener_prepared_format: str | None = None
        self._opener_prepared_course: str | None = None   # the setup course it was written for
        self._opener_prepared_teams: frozenset | None = None   # the colour teams it was written for
        # The last roster (and slot order) read off a setup screen's PLAYERS tab, for a setup that never shows it.
        self._last_setup_roster: tuple[list[str], list[str]] | None = None
        # The setup screen Play was pressed on: (course, roster, slot order, when), for the round's start.
        self._played_setup: tuple[str, list[str], list[str | None], float] | None = None
        # The last setup read the watcher left the setup screen with, though no load screen it could read
        # followed (its title misread): still this round's players and slots if the tee agrees.
        self._setup_seen: tuple[str, list[str], list[str | None], float] | None = None
        # The course the setup's nines, game mode and team colours were read on.
        self._setup_course: str | None = None
        # The last setup the booth read: (its HUD, the shot count then, GSPro's logged active player,
        # when). The impact read's fallback when the player box is gone by the time it's grabbed.
        self._last_setup: tuple | None = None
        self._stymie_seq: int | None = None   # the shot count when the last setup had a tree on the line
        # Each player set up over the ball, for the chat: (when, on the PC's clock; who and the shot).
        # [wall time, brief] of each setup, for the chat; the minimap's trouble is added once it's read.
        self._setups_seen: deque[list] = deque(maxlen=60)
        self._hud_buffer: Any = None   # HudBuffer: the HUD's last seconds, for an impact read GSPro hid
        self._last_impact_at: float | None = None
        # GSPro's "hide UI on shot" keeps the tiles off screen until the next player is up; the
        # next setup read has them. The swing whose tiles are still wanted, the tiles a
        # setup read for a swing, and the recorded shot each swing's tiles go to: (shot id, record,
        # the tiles the shot before it showed, unit, health counts then).
        self._tiles_wanted: int | None = None
        self._late_tiles: dict[int, dict[str, str]] = {}
        self._shown_tiles: tuple | None = None   # the latest shot's (carry, raw carry, total, offline) read
        self._tiles_due: dict[int, tuple] = {}
        # (when, frame, tones) of the latest PLAYERS-tab frame the quick sampler caught (see _sample_players_tab).
        self._players_snap: tuple | None = None
        self._setup_tab_lit_at = float("-inf")   # when a setup tab was last seen selected (either reader)
        self._idle_card_logged: dict | None = None
        self._scramble_pairs: set = set()   # the log's teammate pairs last applied to the round
        self._round_started_at = 0.0   # monotonic time the current round started
        self._opener_gen = 0
        # Set when a round opens so the one redundant, late pre-shot on the opening tee shot is skipped.
        self._skip_opening_preshot = False
        self.history = History.load(cfg.history_file)
        # The league's week results and standings from Squabbit, refreshed with the handicaps at each
        # round start. None until a fetch works; the history file's standings stand in.
        self.board: Board | None = None
        self._league_page_on = False   # keeping the stats page's league.json (_publish_league_page)
        self._league_body: bytes | None = None   # the newest league.json to put up
        self._league_uploading = False
        self._recap_task: asyncio.Task | None = None   # building a league week's recap (_build_recap)
        self._recap_again = False   # something changed while it was: look again after
        self._recap_tried: tuple | None = None   # the last recap built or tried, (key, digest)
        self._recap_holes: dict[tuple[int, str], tuple[int, dict]] = {}   # (week, team) -> (net, Squabbit's holes)
        self._board_task: asyncio.Task | None = None
        # Posting a league round to Squabbit as it's played (squabbit_live.py), once the players say yes:
        # the booth's record of what it wrote (made on the first league round), and this round's state
        # {"round", "status": finding/asking/on/off, "targets", "question", "dirty", "task", "last"}.
        self._sq_live = None
        self._log_finish: dict[int, float] = {}   # shot id -> where GSPro's log put it (yards from the hole)
        self._sq: dict | None = None
        self._sq_lock = asyncio.Lock()   # one writer at a time, across rounds (a replayed round's included)
        self.hole_number: int | None = None
        # Whether the current hole's number was read off the screen rather than counted on.
        self.hole_number_read = False
        # For spotting the end of a round when GSPro's final scorecard isn't read.
        self._last_activity = time.monotonic()
        # When a ball was last struck (any: range, practice or round), wall clock, for the tray's
        # round status: an update installs with GSPro open only once nobody's hit one in a while.
        self._struck_at = time.time()
        # The activity timestamp before the current setup, i.e. how quiet it was just before this
        # tee: a long gap marks a round that was abandoned rather than finished.
        self._prev_activity = 0.0
        self._putted_on_last_hole = False
        self._round_end_queued = False
        self._round_ended_at = 0.0
        self._round_handling = 0   # round starts and ends still being handled (bookings, the sign-off)
        self._shots_at_round_end = 0
        self._fixture_saved_at: dict[str, float] = {}
        self._strategy_hole: str | None = None   # the hole a where-to-miss read was last offered on
        # GSPro log counters already acted on (round ends / completions), and whether the range quiet
        # has been logged, so each transition is handled once.
        self._log_round_ends = 0
        self._log_ended_round = None   # the round GSPro's log ended under the booth: its state is the snapshot
        self._log_round_completes = 0
        self._log_layouts = 0
        self._log_pins = 0   # pins GSPro logged (GsproState.pins) already kept
        self._log_menus = 0   # player menu openings (GsproState.player_menus) already noted
        self._range_noted = False
        self._practice = False   # GSPro's practice mode on screen: no round, so no commentary and nothing saved
        self._round_box_seen = False   # a round's player box has been on screen: practice isn't the first word
        self._join_tried = False   # joining a round already under way is tried once, at the first setup
        # A scorecard with holes played and no round in progress, on a course gone onto since the last
        # round ended: a round whose start the booth missed, joined at the next setup (_note_idle_card).
        self._card_join = False
        self._courses_at_round_end = self._round_course_entries = 0
        # GSPro's End Round count when an opening drive struck off a course's own range was taken: with
        # no round open (or the last one over), that drive began one the booth must join.
        self._new_round_struck: int | None = None
        self._range_exits_seen = 0
        self._opening_fire: tuple[int, int] | None = None   # (round id, its first fire): see _adopt_opening_shots
        # Shot handlers run in the background (the wait for the ball at rest and the reaction mustn't block the loop); keep
        # references so they aren't garbage-collected mid-flight.
        self._shot_tasks: set[asyncio.Task] = set()
        # Fire-and-forget background work (e.g. a handicap refresh once a prepared opener has aired).
        self._bg_tasks: set[asyncio.Task] = set()
        # Holes with a score on the last scorecard read, and when it was read. A fresh card
        # showing none means GSPro has started a new round.
        self._holes_played = 0
        self._holes_played_at = 0.0
        # (round id, hole of the round) -> each player's score GSPro's log has posted on it, so a new one
        # is acted on as the scorecard's would be (_note_log_scores).
        self._log_posted: tuple[tuple[int, int], dict[str, int]] | None = None


    async def run(self) -> None:
        models = (("pre-shot", self.preshot_llm), ("in-flight fallback", self.flight_llm),
                  ("reactions", self.live_llm), ("segments", self.segment_llm),
                  ("round opener", self.opener_llm), ("round closer", self.closer_llm),
                  ("news asides", self.news_llm))
        if self.commentary_unavailable:
            log.warning("Commentary can't start: %s. Recording shots and rounds only", self.commentary_unavailable)
            models = ()
        elif not self.commentary_on:
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
        self._fill_course_layouts()
        self._refresh_own_handicaps(backfill=True)
        if self.news is not None:
            asyncio.create_task(self._news_loop(), name="news")
        if self.weather is not None:
            asyncio.create_task(self._weather_loop(), name="weather")
        if self.chat is not None or self.alerts is not None:   # the loop also sends "we're live"
            asyncio.create_task(self._chat_loop(), name="chat")
        if self.alerts is not None:
            asyncio.create_task(asyncio.to_thread(self.alerts.start, self._alert_topics()), name="alerts-key")
        if self.bookings is not None:
            self._fetch_bookings("startup")
        if self.cfg.publish.enabled and self.cfg.publish.bucket and self.cfg.source.type != "replay":
            if self.bookings is not None and self.cfg.booking.schedule_days:
                asyncio.create_task(self._schedule_loop(), name="schedule")
            else:
                asyncio.create_task(asyncio.to_thread(self._remove_schedule), name="schedule-remove")
            self._league_page_on = bool(self.cfg.squabbit.enabled and self.league.squabbit_invite)
            if self._league_page_on:
                asyncio.create_task(self._league_page_midnight(), name="league-page-midnight")
            else:
                asyncio.create_task(asyncio.to_thread(self._remove_league_page), name="league-page-remove")
        tasks = [asyncio.create_task(self.speech.run(), name="speech")]
        if self.source is not None:
            tasks.append(asyncio.create_task(self._pump_source(), name="source"))
        if self._club_feed is not None:
            tasks.append(asyncio.create_task(self._club_feed.run(), name="club-feed"))
        if self._log_truth:
            self._follower = GsproLogFollower(self.cfg.source.gspro_log, self.gspro)
            tasks.append(asyncio.create_task(self._follower.run(), name="gspro-log"))
        health = None
        if self.cfg.health.enabled and self.cfg.source.type != "replay":
            health = asyncio.create_task(self._startup_health(), name="health")
        if ready := os.environ.get("SHOTCAST_READY_FILE"):
            asyncio.create_task(self._say_ready(ready, health), name="ready")
        self._schedule_backup("startup")
        self._start_live()
        self._start_hud_buffer()
        if self.cfg.source.type != "replay":
            asyncio.create_task(self._round_status_loop(), name="round-status")
            # (GSPro's own record, with its text log followed or not: the ball at rest is the file's)
            asyncio.create_task(self._round_file_loop(), name="round-file")
        if self.screen and self.cfg.screen.ocr:
            if self.cfg.health.enabled and self.cfg.source.type != "replay":
                asyncio.create_task(self._screen_cover_loop(), name="screen-cover")
            tasks.append(asyncio.create_task(self._watch_hud(), name="hud-watch"))
            # Always: the setup screen's players, holes and game mode are the round's stats too. Only the
            # early opener it can compose needs commentary.preround.
            tasks.append(asyncio.create_task(self._watch_setup(), name="setup-watch"))
        if self._log_truth and self.cfg.source.type != "replay":   # (GSPro's round file: see _round_file_loop)
            tasks.append(asyncio.create_task(self._watch_picks(), name="pick-watch"))
        try:
            while (event := await self._events.get()) is not None:
                try:
                    if isinstance(event, ShotEvent):
                        # In the background: the in-flight line plays at the top of handle_shot, but
                        # the wait for the ball at rest and the reaction take many seconds and must not block the next
                        # pre-shot or shot behind them.
                        task = asyncio.create_task(self._shot_then_news(event))
                        self._shot_tasks.add(task)
                        task.add_done_callback(self._on_shot_task_done)
                    elif isinstance(event, PreShotEvent):
                        # In the background: impact must never wait behind a setup line.
                        self._cancel_preshot()
                        if event.model_task is not None:
                            # (its lie fills ran before its first await: _emit_setup started it before this event)
                            self._count_lie_source(event.hud)
                        self._preshot_task = asyncio.create_task(self.handle_preshot(event))
                        if self.await_preshot:
                            t = self._preshot_task
                            await asyncio.wait({t})   # an impact capture may cancel it meanwhile
                            if not t.cancelled() and t.exception() is not None:
                                raise t.exception()
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
            for m in self._live_mirrors():
                await asyncio.to_thread(m.close)
            if self.chat is not None and self._chat_live:
                try:   # close the page's chat now, rather than when the heartbeat goes stale
                    await asyncio.wait_for(asyncio.to_thread(self.chat.set_on_air, False), 5)
                except Exception as e:
                    log.info("Chat: couldn't close the chat: %s", e)
            if self._hud_buffer is not None:
                self._hud_buffer.stop()

    async def _pump_source(self) -> None:
        try:
            async for event in self.source.events():
                if isinstance(event, ShotEvent):
                    event.received_at, event.received_fires = time.monotonic(), self.gspro.fires
                    if self._club_feed is not None and not event.shot.club_measured:
                        def impact(event=event) -> None:
                            # Waiting on the monitor's log: take the impact state now, as for a held shot.
                            if event.impact_state is None:
                                self._capture_impact(event)
                        try:
                            await self._club_feed.fill(event.shot, impact)
                        except Exception:
                            log.exception("Club feed: couldn't add club data to a shot")
                    self._keep_buffer_frames(event, event.received_at)
                await self._events.put(event)
        except Exception:
            log.exception("Shot source stopped; no more shots will come through")
        finally:
            await self._events.put(None)
