from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator


class Region(BaseModel):
    """Pixels, or fractions of the monitor when every value is <= 1 (resolution-independent).

    GSPro scales its HUD with the screen *width* and pins each panel to an edge, so on a screen of a
    different shape a panel keeps its horizontal fractions but its height (and its distance from the
    edge it's pinned to) changes by (screen aspect / calibration aspect). Set `aspect` to the
    width/height ratio the fractions were measured at and `anchor` to the pinned edge, and the box is
    adjusted to the screen it's read on. Without `aspect` the fractions are used as they are.
    """

    left: float
    top: float
    width: float
    height: float
    anchor: Literal["top", "bottom", "middle"] = "top"
    aspect: float | None = None

    def to_pixels(self, screen_w: int, screen_h: int) -> tuple[int, int, int, int]:
        if max(self.left, self.top, self.width, self.height) <= 1:
            top, height = self.top, self.height
            if self.aspect and screen_h:
                r = (screen_w / screen_h) / self.aspect
                height = min(1.0, self.height * r)
                if self.anchor == "top":
                    top = self.top * r
                elif self.anchor == "bottom":
                    top = 1 - (1 - self.top - self.height) * r - height
                else:
                    top = 0.5 + (self.top + self.height / 2 - 0.5) * r - height / 2
                top = min(max(top, 0.0), 1 - height)
            return (round(self.left * screen_w), round(top * screen_h),
                    round(self.width * screen_w), round(height * screen_h))
        return int(self.left), int(self.top), int(self.width), int(self.height)


# The aspect (width / height) the HUD defaults were measured at: GSPro on a 16:10 screen.
HUD_ASPECT = 1.6


# Generous boxes that fit the GSPro 3.1.x release HUD, measured on a 16:10 screen and adjusted to other
# screen shapes (checked on 16:9) by the pinned edge each panel sits against (see Region).
DEFAULT_REGIONS = {
    # Stops above the list of other players GSPro shows under the active one.
    "player_box": Region(left=0.0, top=0.0, width=0.32, height=0.086, aspect=HUD_ASPECT),
    # Just the top row's name cell inside player_box (current player is always on top): opaque box
    # background, left of the score badge, above the shot/distance row. Pixel-compared, never OCR'd.
    "player_name": Region(left=0.02, top=0.024, width=0.13, height=0.026, aspect=HUD_ASPECT),
    "hole_box": Region(left=0.76, top=0.0, width=0.22, height=0.10, aspect=HUD_ASPECT),
    "tiles": Region(left=0.85, top=0.08, width=0.15, height=0.40, aspect=HUD_ASPECT),
    # A second tile panel some players show down the left edge (spin, launch, face, offline...).
    "tiles_left": Region(left=0.0, top=0.08, width=0.16, height=0.40, aspect=HUD_ASPECT),
    # Tall, because the minimap header moves with the minimap's size (collapsed, it sits near the bottom).
    "lie": Region(left=0.84, top=0.45, width=0.16, height=0.53, anchor="bottom", aspect=HUD_ASPECT),
    # Bottom-left club and handedness boxes ("Driver" / "RH").
    "club": Region(left=0.0, top=0.84, width=0.1, height=0.14, anchor="bottom", aspect=HUD_ASPECT),
    # GSPro's overhead minimap, read for hazards by minimap.provider: jev.
    "minimap": Region(left=0.855, top=0.574, width=0.133, height=0.387, anchor="bottom", aspect=HUD_ASPECT),
    "slope": Region(left=0.85, top=0.945, width=0.15, height=0.055, anchor="bottom", aspect=HUD_ASPECT),
    # The other players in the match, one row each under the active player's box (up to 7).
    "others": Region(left=0.0, top=0.083, width=0.22, height=0.27, aspect=HUD_ASPECT),
    # GSPro's scorecard pops up mid-screen between holes: its title, then the whole table.
    "scorecard_title": Region(left=0.3, top=0.12, width=0.4, height=0.2, anchor="middle", aspect=HUD_ASPECT),
    "scorecard": Region(left=0.04, top=0.15, width=0.92, height=0.68, anchor="middle", aspect=HUD_ASPECT),
    # GSPro's "For Birdie" result banner, centre-screen on hole completion: "BIRDIE", "PAR",
    # "DBL BOGEY"... A reliable "hole is over" + score signal. Generous default box (centre, around
    # mid-height); calibrate it tighter to your resolution with `calibrate` / `ocr-test`.
    "score_banner": Region(left=0.25, top=0.40, width=0.50, height=0.26, anchor="middle", aspect=HUD_ASPECT),
    # --- GSPro setup screen (pre-round), read for the early opener (commentary.preround). Calibrated
    # to 3450x2160 and checked at 1920x1080; recheck with `calibrate --out setup` if your screen shape
    # or HUD scale differs. The course is in the bottom-right "Play <course>!" button (which also
    # marks that the setup screen is up); players fill a 4x2 grid of name slots, slot 1 starting with
    # your name. Boxes are kept tight to each name pill: OCR returns nothing from a box that also
    # swallows the pill border or the dropdown chevron. GSPro scales this screen with the screen's
    # width, pinned to the top (the Play button to the bottom), so the boxes adjust to other screen
    # shapes the way the HUD boxes do.
    # An empty slot shows a centered "+" add-player button (not the name pill), so the boxes are kept
    # tight to the top-of-card pill and the watcher drops any read without a letter (see _watch_setup).
    "setup_course": Region(left=0.797, top=0.922, width=0.165, height=0.064, anchor="bottom", aspect=HUD_ASPECT),
    "setup_slot_1": Region(left=0.070, top=0.254, width=0.139, height=0.050, aspect=HUD_ASPECT),
    "setup_slot_2": Region(left=0.305, top=0.254, width=0.139, height=0.050, aspect=HUD_ASPECT),
    "setup_slot_3": Region(left=0.540, top=0.254, width=0.139, height=0.050, aspect=HUD_ASPECT),
    "setup_slot_4": Region(left=0.778, top=0.252, width=0.135, height=0.054, aspect=HUD_ASPECT),
    "setup_slot_5": Region(left=0.070, top=0.518, width=0.139, height=0.050, aspect=HUD_ASPECT),
    "setup_slot_6": Region(left=0.305, top=0.518, width=0.139, height=0.050, aspect=HUD_ASPECT),
    "setup_slot_7": Region(left=0.540, top=0.518, width=0.139, height=0.050, aspect=HUD_ASPECT),
    "setup_slot_8": Region(left=0.772, top=0.518, width=0.139, height=0.050, aspect=HUD_ASPECT),
    # The PLAYERS tab header. It's a solid white pill only while that tab is selected, so its brightness
    # says the slot boxes hold player names (not Match Settings / Holes labels). Pixel check, no OCR.
    "setup_players_tab": Region(left=0.305, top=0.105, width=0.130, height=0.045, aspect=HUD_ASPECT),
    # The HOLES tab header (a white pill while selected) and the FRONT 9 / BACK 9 checkboxes on that
    # tab (white when checked, an outline when not). Pixel checks, no OCR: they say which nines are in
    # the round.
    "setup_holes_tab": Region(left=0.570, top=0.108, width=0.120, height=0.040, aspect=HUD_ASPECT),
    # The MATCH SETTINGS tab header (a white pill while selected) and its GAME MODE value. Only a
    # SCRAMBLE is read from it: leagues leave GSPro on stroke play for their other formats.
    "setup_match_tab": Region(left=0.440, top=0.108, width=0.120, height=0.040, aspect=HUD_ASPECT),
    "setup_game_mode": Region(left=0.360, top=0.285, width=0.170, height=0.042, aspect=HUD_ASPECT),
    "setup_front9_box": Region(left=0.074, top=0.221, width=0.018, height=0.028, aspect=HUD_ASPECT),
    "setup_back9_box": Region(left=0.074, top=0.491, width=0.018, height=0.028, aspect=HUD_ASPECT),
    # GSPro's course-load screen shows the course name big and centered up top. The watcher fires the
    # opener when this matches the course that was set up (a positive "Play was pressed and it's
    # loading" signal), so a window popping up over the setup screen can't trigger a spurious opener.
    "load_course": Region(left=0.20, top=0.10, width=0.60, height=0.10, anchor="middle", aspect=HUD_ASPECT),
    # The SELECT COURSE screen's title, top left: the first screen of a new round, so the booth reads
    # the SuperSaaS bookings here (or at the setup screen, whichever it sees first).
    "course_select": Region(left=0.012, top=0.095, width=0.13, height=0.05, aspect=HUD_ASPECT),
}


class PersonaConfig(BaseModel):
    name: str
    style: str


class LLMConfig(BaseModel):
    # Cloud by default (OPENAI_API_KEY); `ollama` runs a model on the PC itself instead.
    provider: Literal["ollama", "anthropic", "openai"] = "openai"
    model: str = "gpt-6-luna"
    base_url: str | None = None
    # Only send screenshots when the model can actually see them.
    vision: bool = False
    temperature: float | None = 0.9
    # Reasoning effort, for providers that take it. Anthropic (not Haiku 4.5): low/medium/high, and
    # xhigh/max on Haiku 5.5 and the 5-series. OpenAI reasoning models (gpt-6): none/low/medium/high/
    # xhigh, where "none" skips reasoning for the lowest latency. On Claude, "none" turns thinking off
    # (or falls back to "low" where a model can't). Higher effort trades latency for quality; for the
    # booth's short commentary the fast settings ("none" or "low") win.
    effort: Literal["none", "low", "medium", "high", "xhigh", "max"] | None = "none"
    # Ollama only. Thinking models (Qwen3, Gemma 4) otherwise spend the whole token budget
    # reasoning and return nothing. Harmless on models without a thinking mode.
    ollama_think: bool = False
    # Roomy by default so the pre-shot BRANCH call (every flight + result line in one JSON reply) can't
    # truncate: a cut-off reply fails json.loads and loses ALL branches for that shot. It is an upper
    # bound, not a target, so the short calls that set it lower (e.g. live=400) are unaffected.
    max_tokens: int = 900
    timeout_seconds: float = 30.0
    # OpenAI only. "fast" (service_tier) buys up to 2.5x faster, more consistent latency at ~2x the
    # per-token price - trivial on gpt-6-luna, which is already cheap, so it's worth it on every call.
    service_tier: Literal["auto", "default", "fast", "priority", "flex"] | None = "fast"


class LLMSection(BaseModel):
    live: LLMConfig = Field(default_factory=LLMConfig)
    # Between-hole segments. Falls back to `live` when omitted.
    segment: LLMConfig | None = None
    # The pre-shot BRANCH call (all flight + result lines, written at setup) and the mid-flight
    # fallback. Speed matters most, and the branch JSON is large (8 flight + 10 result lines), so it
    # needs the roomy max_tokens (~900) the default gives - `live` is capped lower for the short spoken
    # line and would truncate the branch reply. Falls back to `live`, so set `flight` explicitly.
    flight: LLMConfig | None = None
    # Lines while the player sets up. Falls back to `live`.
    preshot: LLMConfig | None = None
    # The round's opener and wrap-up. Falls back to `segment` with room for a longer reply.
    # `round` is the shared fallback for both; `opener` and `closer` override it individually. The
    # opener wants a fast model (it's raced against a quick course load); the closer is pre-fetched on
    # the last hole, so it can take a richer, slower model with the latency fully hidden.
    round: LLMConfig | None = None
    opener: LLMConfig | None = None
    closer: LLMConfig | None = None


class TTSConfig(BaseModel):
    # Cloud voices by default (OPENAI_API_KEY); `google` for Google's Chirp 3 HD voices
    # (GOOGLE_TTS_API_KEY); `kokoro` voices on the PC's CPU instead.
    provider: Literal["kokoro", "openai", "google", "console"] = "openai"
    # Persona key -> provider voice id. Each provider has its own voice ids, so they're kept apart:
    # flipping `provider` is then the whole toggle, with no voice edits (and an easy switch back).
    voices: dict[str, str] = Field(default_factory=lambda: {"pbp": "bm_george", "color": "am_michael"})
    openai_voices: dict[str, str] = Field(default_factory=lambda: {"pbp": "fable", "color": "ballad"})
    # Google Chirp 3 HD voice names; the first five characters are the language code.
    google_voices: dict[str, str] = Field(default_factory=lambda: {
        "pbp": "en-GB-Chirp3-HD-Iapetus", "color": "en-US-Chirp3-HD-Orus"})
    # Google's pace (0.25-2.0). Kept apart from `speed`, which was tuned for the OpenAI voices.
    google_speaking_rate: float = 1.0
    speed: float = 1.0
    kokoro_model: str = "models/kokoro-v1.0.onnx"
    kokoro_voices: str = "models/voices-v1.0.bin"
    openai_model: str = "gpt-4o-mini-tts"
    # Persona key -> delivery instructions for the OpenAI voice (gpt-4o-mini-tts `instructions`):
    # steers tone/pacing/emotion, not the words. Only the gpt-4o models honor it; tts-1 ignores it.
    openai_instructions: dict[str, str] = Field(default_factory=lambda: {
        "pbp": (
            "Voice Affect: Polished, warm, and authoritative; the lead voice of a televised golf "
            "broadcast, assured but never showy.\n"
            "Tone: Refined and measured, with a light British inflection; genuinely engaged, quietly "
            "delighted by a fine shot and sympathetic to a poor one.\n"
            "Pacing: Unhurried and even; let a good shot breathe, lift and quicken just a touch on the "
            "big moments, then settle back.\n"
            "Emotion: Understated enthusiasm and real warmth for the golf; rising naturally on a great "
            "strike or a made putt, never over-excited.\n"
            "Pronunciation: Crisp and clear, landing the numbers and the players' and teams' names "
            "cleanly; crisp consonants, rounded vowels.\n"
            "Pauses: a brief, deliberate pause only before a result. Keep direct address tight: barely a "
            "breath before a name like 'Duke' at the end of a line, never a long beat handing over."
        ),
        "color": (
            "Voice Affect: Dry, sharp, and quick; a wry color analyst who needles the players and enjoys "
            "the mischief, not a cheerleader.\n"
            "Tone: Deadpan-leaning and plain-spoken American, amused and a touch sardonic; the wit is in "
            "the understatement, never gushing.\n"
            "Pacing: Brisk and snappy; get in, land the line, get out. Keep it moving, clip the words, no "
            "drawl and no dwelling.\n"
            "Emotion: Understated; a flat, knowing delivery on the jabs, a quick flash of genuine respect "
            "for a great shot, then straight back to dry.\n"
            "Pronunciation: Crisp and clipped, consonants sharp; land the needle cleanly.\n"
            "Pauses: One tight beat before the punchline, then move on; never let a line sag."
        ),
    })
    # Console provider: pause roughly as long as the line would take to say.
    console_simulate_duration: bool = True
    # Cloud voice (openai) only. Off: a line is voiced when it's about to be said, so only what airs
    # is paid for. On: every possible flight/result call is voiced at setup, and the wrap, closer and
    # asides as soon as they're written, so they play with no voicing wait; most of that audio never
    # airs (about 80% of the TTS bill in practice). A local voice (kokoro) always voices ahead, since
    # it costs nothing and is slow on CPU.
    prevoice: bool = False
    # Cloud voice (openai): stream the audio and start playing at its first bytes instead of waiting
    # for the whole clip (first audio is ~0.5 s vs ~1.4 s for a full line). Off: whole clips, with the
    # hedge settings below.
    stream: bool = True
    # Streaming: each time this long passes with no audio yet, another request is sent alongside the
    # others, up to `retries` extra; whichever starts first is played and the rest are cancelled. A
    # stalled request sends nothing at all, so it never recovers; a working one answers in ~0.3-0.8 s.
    # About 1 in 7 requests stalls, so one retry still loses ~2-6% of lines; two lose well under 1%.
    first_audio_timeout_seconds: float = 1.0
    retries: int = 2
    # Streaming: audio to have in hand before a line starts playing. The stream comes in uneven bursts
    # (the first is often a few ms of sound) and fell 0.1-0.45 s behind real time on most measured
    # lines, so playing from the first byte left gaps near the start.
    stream_prebuffer_seconds: float = 0.3
    # A beat of silence between consecutive lines: when the other commentator takes over, and when
    # the same one carries on. Only fills up to this since the last line ended, so a line after a
    # longer silence (a mid-flight call) starts at once. 0 plays lines back to back.
    turn_gap_seconds: float = 0.4
    line_gap_seconds: float = 0.15
    # Deadlines for a cloud voice (openai), whose requests sometimes stall for 30 s+ while the next
    # one answers in ~1 s. A comment is useless once its moment has passed, so lines get a deadline and
    # are dropped, never played late, when they miss it.
    # A request still unanswered after this long gets a second one sent alongside it (a normal line
    # voices in ~1-2 s); whichever answers first is used.
    hedge_after_seconds: float = 2.5
    # Lines voiced ahead (flight/result branches at setup, wraps, the closer) keep hedging up to this
    # long; the moment they were voiced for (impact, the hole-out) usually ends it sooner.
    prepare_deadline_seconds: float = 12.0
    # A line voiced on demand must be ready within this long of its turn to be voiced, or it's dropped.
    live_deadline_seconds: float = 5.0
    # A mid-flight call must start playing within this long of impact, while the ball is still in the
    # air; one voiced on demand that can't be ready by then is dropped.
    flight_audio_deadline_seconds: float = 4.0

    @property
    def active_voices(self) -> dict[str, str]:
        """The persona->voice map for the provider in use (voice ids differ by provider)."""
        if self.provider == "openai":
            return self.openai_voices
        if self.provider == "google":
            return self.google_voices
        return self.voices

    @property
    def active_instructions(self) -> dict[str, str]:
        """Per-persona delivery instructions, only for the OpenAI provider; empty otherwise."""
        return self.openai_instructions if self.provider == "openai" else {}


class ScreenConfig(BaseModel):
    # Read the monitor GSPro's window is on, rechecked every few seconds; `monitor` (1 = the primary)
    # is used when GSPro's window can't be found (not running, minimized) or with this off.
    follow_window: bool = True
    monitor: int = 1
    # "monitor" grabs the framebuffer and never touches GSPro's render path; "window" captures
    # GSPro's own window via PrintWindow so a popup in front can't corrupt the read, but it forces
    # the game to render synchronously each grab and can stutter/freeze it, so it's off by default.
    capture: Literal["monitor", "window"] = "monitor"
    window_title: str = "GSPro"
    # Cap OCR (onnxruntime) threads so it can't grab every core and starve GSPro. None = all cores.
    ocr_threads: int | None = 6
    # Shrink each region before OCR: a high-res HUD reads fine smaller and far faster, which is
    # what lets the booth keep up while following a shot. 1.0 = full size.
    ocr_scale: float = 1.0
    # After impact the HUD is polled to follow the ball: CARRY (game) counts up until touchdown,
    # Total Length until it stops, and about 1-2 s later GSPro moves on to the next shot.
    follow_poll_seconds: float = 0.3
    # How long to follow a shot from impact. The cap bites on odd outcomes (a water penalty, a lost
    # ball) where the stop is never cleanly seen, so keep it short.
    follow_max_seconds: float = 12.0
    # Once the ball has landed, keep following at least this long after touchdown for it to roll out
    # and stop. A long drive lands 6-8 s in and can still be rolling at follow_max_seconds.
    follow_roll_seconds: float = 8.0
    # Saves a small screenshot at impact and at the final reading of every shot under
    # log_dir/shots/, for checking what the booth read against what was on screen. ~150 KB each.
    save_review_images: bool = False
    send_images: bool = False
    image_max_width: int = 1024
    ocr: bool = True
    # Named screen rectangles to OCR: player_box, hole_box, tiles, tiles_left, lie, slope, club.
    # Regions set in the config replace those defaults one by one; the rest keep their default.
    regions: dict[str, Region] = Field(default_factory=lambda: dict(DEFAULT_REGIONS))

    @field_validator("regions", mode="after")
    @classmethod
    def _merge_defaults(cls, regions: dict[str, Region]) -> dict[str, Region]:
        return {**DEFAULT_REGIONS, **regions}
    # How often the HUD is polled for new shots and holes.
    hud_poll_seconds: float = 1.0


class SquabbitConfig(BaseModel):
    # Pull live handicaps from Squabbit at the start of each round. Needs SQUABBIT_EMAIL and
    # SQUABBIT_PASSWORD in the booth .env; the invite code comes from the league file
    # ("squabbit_invite"). Off by default so the booth never needs a login.
    enabled: bool = False
    # Keep a slow or unreachable Squabbit from stalling the opener; the file handicaps stand in.
    timeout_seconds: float = 15.0


class BookingConfig(BaseModel):
    # The sim's SuperSaaS schedule, read so a round played in a "Practice" booking is practice even
    # on this week's course with a full team. A "League Week N" booking, or no booking, leaves the
    # usual schedule matching to decide. Needs SUPERSAAS_API_KEY (the account's API key, Account Info
    # page) in the booth .env. Off by default.
    enabled: bool = False
    account: str = ""            # the SuperSaaS account name, e.g. GolfShed
    schedule_id: int | None = None   # the number at the end of the schedule's Configure page URL
    type_field: str = "field_1_r"    # the booking field holding the League/Practice dropdown
    # A round this close before a booking starts or after it ends still counts as in it.
    slack_minutes: float = 30.0
    # With publish set up, the stats page's Schedule tab: the next schedule_days of bookings (times,
    # type and the booker's name), uploaded as schedule.json every schedule_refresh_minutes. 0: none.
    schedule_days: int = Field(14, ge=0)
    schedule_refresh_minutes: float = Field(30.0, gt=0)


class SourceConfig(BaseModel):
    # connector_log: follow GSPconnect's debug log (Uneekor and other built-in connectors). Falls
    #   back to gspro_log when that log doesn't exist.
    # gspro_log: read each shot from GSPro's own log. Works with any launch monitor; ball data only
    #   (no club speed, path, face or strike location).
    # gspro_sniffer: capture Open Connect traffic on port 921 (third-party connectors; needs Npcap).
    type: Literal["connector_log", "gspro_log", "gspro_sniffer", "replay"] = "connector_log"
    connector_log: str = r"C:\GSProV1\Core\GSPC\ConnectDebug.txt"
    # GSPro's own Unity log, tailed alongside the shot feed for game truth OCR only guesses at:
    # strokes taken (penalties included), lie, distance to pin, whose turn, and holed shots. On by
    # default: stats, round ends and range detection depend on it. Off falls back to OCR guesses.
    # See sources/gspro_log.py.
    gspro_log: str = str(Path.home() / "AppData" / "LocalLow" / "GSPro" / "GSPro" / "output_log.txt")
    gspro_log_truth: bool = True
    # GSPro keeps courses in two libraries; each folder holds the par, stroke index and yardages.
    gspro_courses_dirs: list[str] = Field(default_factory=lambda: [
        r"C:\GSProV1\Courses", r"C:\GSProV1\Core\GSP\Courses"])
    port: int = 921
    # Defaults to scapy's loopback adapter (Npcap loopback on Windows).
    iface: str | None = None
    replay_file: str = "data/sample_shots.jsonl"
    replay_speed: float = 1.0


class CommentaryConfig(BaseModel):
    # Off: no commentary at all (no model or voice calls, no API keys needed), but every shot and
    # round is still read from GSPro and recorded in the database. `run --no-commentary` flips it.
    enabled: bool = True
    interest_threshold: float = 0.5
    # Chance of a line on an unremarkable shot.
    routine_comment_rate: float = 0.3
    min_seconds_between_comments: float = 4.0
    # Lines shown to the model so it doesn't repeat itself. A season-wide check in code
    # catches anything older.
    recent_lines: int = 30
    # Reject a generated line that reuses a phrase from recent lines or was said earlier this season.
    # Off lets phrasing recur (the model still sees recent lines and mostly varies on its own); on
    # forces a retry when it doesn't, which can be too eager and drop otherwise-good lines.
    block_repeats: bool = True
    max_words_per_line: int = 18
    # News, trivia and score asides tell a whole story in one line, so they get more room; at 18
    # most lines ran over and were retried or dropped.
    aside_max_words: int = 30
    # The round's sign-off isn't racing the next shot, so it gets room for a rich closing conversation;
    # at 18 its longer lines were cut to a fragment or dropped.
    closer_max_words: int = 40
    # The opener's welcome ran 19-24 words; at 18 it was dropped whole and the opener retried for a
    # missing welcome. Shorter than the closer's: it plays over a load screen that can be quick.
    opener_max_words: int = 25
    opener_max_lines: int = 3
    segments_on_hole_change: bool = True
    # Before voicing the between-holes wrap, wait up to this long for the hole's last shot handler to
    # finish, so the final-shot reaction and the wrap don't talk over each other. Bounded so a slow
    # shot handler can't hold the booth off air.
    wrap_wait_seconds: float = 5.0
    # Drop queued lines once speech falls this far behind play.
    max_speech_backlog: int = 4
    in_flight: bool = True
    # Play a pre-written result line (voiced at setup) the instant the ball stops on routine shots,
    # with no model call. Keeps putts and approaches instant and commentary from falling behind.
    pre_reactions: bool = True
    # GSPro's hole-result banner appears ~1-1.5s after a putt settles (during the hole-end
    # transition) and stays up ~2s, so the booth polls for it across this window to confirm a make.
    hole_result_window_seconds: float = 3.0
    # A putt from at least this far (feet) also gets a pre-voiced "gimme" line, always played when it
    # stops inside the gimme circle: a lag to tap-in range from here is worth a word. Amateurs leave a
    # 30-footer around 5-6 ft on average, so inside 4 ft is a good lag; shorter ends in the circle
    # routinely. 0 turns it off.
    gimme_lag_feet: float = 30.0
    # Round-so-far stats (short putts, approach leave, birdie chances, greens, fairways, three-putts)
    # as optional colour, from this many finished holes on, and only those well off the player's norm.
    # 0 turns them off.
    round_form_after_holes: int = 3
    # Such a long putt can still be rolling when hole_result_window_seconds runs out, so its result
    # is watched this long instead, letting the pre-written line play the moment it stops.
    lag_result_window_seconds: float = 8.0
    # A mid-flight line that can't start speaking this soon after impact is dropped: the ball
    # has usually landed by then, and "needs legs" after it lands sounds broken.
    flight_deadline_seconds: float = 3.0
    # Skip mid-flight calls for chips and putts; they're on the ground too fast.
    min_flight_carry: float = 50.0
    preshot: bool = True
    # Not every shot needs a setup line; this keeps the booth from narrating every address.
    preshot_rate: float = 0.6
    # The player box must hold steady this long before it counts as a new setup. Kept short: the
    # whole pre-shot plan (settle + Haiku vision call) has to finish before the swing, or the
    # instant pre-written flight line can't play. A phantom setup from a flickered read is cheap.
    preshot_settle_seconds: float = 1.0
    # Flat starting penalty for a lie (GSPro carry / launch monitor carry), replacing the
    # built-in curves until the season database has enough shots from that lie to learn it.
    lie_carry_factors: dict[str, float] = Field(default_factory=dict)
    # Course name as GSPro shows it -> altitude in feet, for a course with no shots yet.
    course_altitude_ft: dict[str, float] = Field(default_factory=dict)
    # A shot this interesting (0..1, from the interest scorer) gets a three- or four-line exchange.
    big_moment_threshold: float = 0.9
    # An approach from at least this far that GSPro's log says finished this close is a big moment.
    close_approach_from_yards: float = 100.0
    close_approach_feet: float = 15.0
    # Rounds: an opener when everyone is on the first tee, a wrap-up when the round is done.
    opener: bool = True
    closer: bool = True
    round_holes: int = 9
    # Tonight's format (scramble, best ball, team sum, alternate shot). Beats the league file's
    # schedule; leave unset to take it from the schedule week whose course GSPro is showing.
    round_format: str | None = None
    # Force a schedule week (format, nine and CTP holes) when the course can't be matched.
    round_week: int | None = None
    # With no final scorecard seen, the round counts as over once the last hole has been putted
    # on and nothing has happened for this long.
    round_end_idle_seconds: float = 60.0
    # A fresh opening tee (everyone on shot 1 at even par) supersedes a round still tracked as in
    # progress once this much time has passed since that round's last activity, so abandoning a
    # round and starting another still opens the broadcast even without a clean finish. A scorecard
    # showing no holes played supersedes it immediately, regardless of this.
    round_restart_idle_seconds: float = 180.0
    # Build the first-hole opener early: read the course and roster off GSPro's setup screen, compose
    # and ground the opener while the course loads, then air it the instant the tee HUD confirms the
    # same course and the same league team(s). Needs the setup-screen regions calibrated (see
    # `calibrate --out setup`). Off until then; when off the opener is made at the tee as before.
    preround: bool = False
    # How often to read the setup screen. Fast, so even a quick glance at the Players tab is caught;
    # affordable because an idle poll OCRs only one gate region (see _watch_setup) and it pauses
    # mid-round and once an opener is prepared.
    preround_poll_seconds: float = 0.3
    # A prepared opener this old is dropped (the course never loaded, or the player backed out).
    preround_ttl_seconds: float = 900.0


class OverlayConfig(BaseModel):
    # A small always-on-top "ON AIR" badge over GSPro (a pulsing red dot). Click it to take the booth
    # off air (commentary off, shots and rounds still recorded) and back on. Changes the running booth
    # only; `commentary.enabled` still decides how it starts. Only shown by `run` on a live source, and
    # not when commentary is off and its API keys are missing (it couldn't go on air).
    enabled: bool = True
    # Where the badge's top-left corner sits, as fractions of the primary screen measured on a 16:10
    # screen; on another shape it moves with GSPro's HUD (see overlay.badge_geometry). The default is
    # the top strip between GSPro's wind readout and the hole box, clear of every OCR region.
    left: float = 0.595
    top: float = 0.008
    # Badge size relative to the default (which, like GSPro's HUD, scales with the screen width).
    scale: float = 1.0


class HealthConfig(BaseModel):
    # Checks that the booth still reads GSPro the way it was built to (see commentator/health.py): the
    # files it follows exist, GSPro's version and the screen size haven't changed (checked at startup
    # and every round start), and after each round, that the HUD reads, data tiles and log lines all
    # came through. Anything off lights a warning chip on the ON AIR badge (hover it for the list,
    # right-click to dismiss until something changes) and goes to log_dir/health.json.
    enabled: bool = True
    # Data tiles the booth needs on screen (labels as OCR reads them, lowercase). The carry tiles drive
    # the carry model and the reaction; Total Length says when the ball stopped.
    required_tiles: list[str] = Field(default_factory=lambda: ["carry (game)", "carry (raw)", "total length"])


class NotifyConfig(BaseModel):
    # Push health issues to a phone through ntfy (https://ntfy.sh): install the ntfy app, subscribe to a
    # hard-to-guess topic, and put that topic in .env as NTFY_TOPIC (the topic is the only secret, so
    # it stays out of this file); with no NTFY_TOPIC nothing is sent. "none" turns pushes off.
    provider: str = "ntfy"
    server: str = "https://ntfy.sh"
    # Which issue levels push ("fail": a read the booth depends on broke; "warn": worth a look).
    levels: list[str] = Field(default_factory=lambda: ["fail", "warn"])
    # Also push when everything that was pushed has cleared.
    cleared: bool = True
    # Issues raised together (one check finding several things) go out as one push after this wait.
    gather_seconds: float = 5.0
    # The same unchanged issue isn't pushed again within this many hours.
    repeat_hours: float = 12.0


class NewsConfig(BaseModel):
    # On an ordinary shot it would otherwise pass on, the booth sometimes takes a breather to riff on
    # a real, current golf headline instead of going quiet. The headline is fetched live and handed
    # to the model verbatim, because the model's own sense of current events is stale. Off by default.
    enabled: bool = False
    # Public RSS/Atom feeds. ESPN's golf feed needs no key; add tour or outlet feeds here.
    feeds: list[str] = Field(default_factory=lambda: ["https://www.espn.com/espn/rss/golf/news"])
    # Headlines kept in rotation after a fetch.
    limit: int = 12
    # Re-fetch the feeds this often during a long session (golf news moves slowly).
    refresh_hours: float = 6.0
    # Golf-world asides per nine holes. Each is written (by the closer's model) and voiced at round
    # start, then held for a quiet moment, or for the kind of shot its story is about.
    per_nine: int = 2
    # Chance, on an ordinary shot the booth would otherwise skip, of playing an aside that isn't
    # waiting on a particular kind of shot (one every three holes at most either way). Shared by every
    # aside: news, live scores and course trivia are one bucket, picked from at random.
    banter_rate: float = 0.35
    # Stories older than this are left out (a feed can carry weeks-old ones); newer ones are told as
    # of when they came out ("over the weekend"), not as breaking news.
    max_age_days: float = 3.0


class WeatherConfig(BaseModel):
    # The current weather outside, as colour ("a wet one out there, dry in the shed"). Open-Meteo,
    # free and keyless; needs the shed's latitude and longitude. Off by default.
    enabled: bool = False
    # Where the shed is: a town or postal code ("03290", "Nottingham, NH"), looked up at startup. Or
    # give latitude and longitude instead, which win when set.
    location: str = ""
    country: str = "US"        # ISO country code the location is in ("" searches everywhere)
    place: str = ""            # how the booth names it, e.g. "Nottingham" (defaults to the looked-up town)
    latitude: float | None = None
    longitude: float | None = None
    refresh_minutes: float = 30.0
    # The opener and the sign-off always get it; a between-holes exchange this often.
    between_holes_rate: float = 0.2


class ScoresConfig(BaseModel):
    # Live scores as a between-shots aside: a tour leaderboard ("Scheffler's at -12 thru 14") and the
    # home teams' games (live, just finished, or later tonight), from ESPN's public scoreboards, no key.
    # Which holes get one is set at round start; the scores are fetched as that hole starts and the
    # aside is written (closer's model) and voiced then, so what's said is minutes old. Off by default.
    enabled: bool = False
    tours: list[str] = Field(default_factory=lambda: ["pga"])   # pga, lpga, eur, champions-tour
    # The local teams, by name or ESPN abbreviation: a list ([Bruins, Celtics, Red Sox, Patriots]) is
    # looked for in the NHL, NBA, MLB and NFL; or per league ({nhl: [BOS], mls: [Revolution]}) for the
    # others (wnba, mls) or to pin a name down.
    teams: list[str] | dict[str, list[str]] = Field(default_factory=list)
    per_nine: int = 1
    # A written aside that hasn't found a quiet moment by then is dropped: the score has moved on.
    max_age_minutes: float = 10.0


class TriviaConfig(BaseModel):
    # Course trivia: the real course behind GSPro's, from its Wikipedia article (found by name; a
    # course with no confident match gets none). Written (closer's model) and voiced at round start
    # like the news asides; a story about one hole waits for that hole. Off by default.
    enabled: bool = False
    per_round: int = 2
    # GSPro course name or id -> Wikipedia article title ("Title#Section" for one section), on top of
    # trivia.KNOWN_PAGES, for courses the search can't place on its own.
    pages: dict[str, str] = Field(default_factory=lambda: {"The Ancient Course": "Old Course at St Andrews"})


class MinimapConfig(BaseModel):
    """Where the pre-shot hazard read comes from. "llm" is the preshot vision model reading the whole
    screenshot (needs llm.preshot.vision and screen.send_images); "jev" is TypeSafe's Jev reading just
    the minimap region: about a third of a second instead of several, and it also reads the carry over
    water and the front and back of the green. Needs the jev extra and TYPESAFE_API_KEY; if Jev fails
    or is late, the llm read is used when it's available."""

    provider: Literal["llm", "jev"] = "llm"
    model: str = "jev-latest"
    # Finding the ball and pin dots needs Jev's Box questions, which only the preview model answers.
    box_model: str = "jev-preview"
    # A yes needs at least this probability to become a fact; a doubtful read says nothing.
    threshold: float = 0.7
    # Below this the ball or pin box is likely wrong (another player's dot), so no left/right reads.
    box_confidence: float = 0.6
    # A sand-coloured patch near the line counts as a bunker only when Jev is at least this sure.
    sand_threshold: float = 0.85
    # The same for a leafy patch near the line being a tree (or a tree line).
    tree_threshold: float = 0.85
    # A multiple-choice read (which side of the green the pin is on; a dogleg) needs this confidence.
    choice_confidence: float = 0.8
    # Below this a yardage read is dropped.
    number_confidence: float = 0.5
    timeout_seconds: float = 1.5


class PuttReadConfig(BaseModel):
    """Reads a putt's break from the drift of GSPro's green-grid dots over a quick burst of frames
    (needs the grid showing on the green, and screen.capture: monitor)."""

    enabled: bool = False
    frames: int = 20
    interval_seconds: float = 0.04
    # The read must land within this long of the setup, or the pre-shot line goes without it.
    timeout_seconds: float = 2.5
    # A section needs this many matched dot moves before it says anything.
    min_steps: int = 6
    # The share of those moves that must agree on a direction.
    slope_share: float = 0.7
    # Mean sideways drift, in pixels per frame on a 4K screen: below gentle_px it's silent (a flat
    # putt still shows up to about 1.4 near the hole); then "a little", plain, "hard", "severely".
    gentle_px: float = 1.5
    moderate_px: float = 2.0
    hard_px: float = 4.0
    severe_px: float = 8.0
    # Mean drift toward or away from the golfer to call it uphill or downhill: the view foreshortens
    # it, so a flat putt showed 0.6 and a steep uphill one 0.75; a gentle slope stays silent.
    slope_px: float = 0.7


class PublishConfig(BaseModel):
    # Put the stats page online (docs/tracking.md#sharing-the-stats-page): `publish` builds the static
    # site and uploads it to this S3 bucket, served through CloudFront with the league password. The
    # upload keys go in .env (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY), never here.
    enabled: bool = False
    bucket: str | None = None
    prefix: str = ""
    region: str | None = None
    # Publish on its own after every round, as soon as the sign-off has aired; delay_seconds after the
    # round ends is the backup, in case the sign-off never finishes.
    after_round: bool = True
    delay_seconds: float = 180.0
    # The page's name (its title and header) and the name of its last section's tab (Schedule,
    # Broadcasts, Booth), here and on the booth's own copy of the page.
    site_name: str = "Shotcast"
    more_tab: str = "More"

    @property
    def brand(self) -> dict[str, str]:
        return {"site_name": self.site_name, "more_tab": self.more_tab}


class LiveConfig(BaseModel):
    # The live stream on the stats page (docs/tracking.md#live-stream): OBS writes HLS into `dir`, and
    # the booth mirrors it to publish.bucket under live/, behind the league password. Needs publish
    # set up. OBS's WebSocket password goes in .env (OBS_WEBSOCKET_PASSWORD), never here.
    enabled: bool = False
    dir: str = "live"
    playlist: str = "stream.m3u8"
    # Start OBS's output when a round starts, and stop it stop_after_seconds after the sign-off.
    with_rounds: bool = True
    stop_after_seconds: float = 30.0
    # Stop it after this long with no shots (a round left on a hole); the next shot of the round starts
    # it again. 0: never. OFF AIR on the badge stops it too, and ON AIR mid-round starts it again.
    idle_minutes: float = 20.0
    # Start OBS (minimized to the tray) when the booth starts, if it isn't running.
    launch_obs: bool = True
    obs_path: str | None = None   # default: the tray's OBS (obs\ here), else C:\Program Files\obs-studio
    obs_host: str = "localhost"
    obs_port: int = 4455
    # GSPro's own sound in the stream (docs/tracking.md#game-sound): the Windows output GSPro plays to,
    # one nothing's plugged into so the room stays quiet (e.g. "Realtek Digital Output"; GSPro is sent
    # there in Windows' Volume mixer). OBS records it game_sound_db below full, under the booth's voices.
    game_sound_device: str | None = None
    game_sound_db: float = -10.0
    # Keep each stream this many days, so the page's Round replays tab can play whole rounds from the start
    # (about 1.2 GB an hour in the bucket). 0: nothing kept, the video deleted as it goes. The stack's
    # lifecycle rule (tools/aws/stats-site.yaml) must keep live/s/ at least a day longer.
    keep_days: int = Field(7, ge=0)


class BackupConfig(BaseModel):
    # Copies of the season database (commentator/backup.py), taken when the booth starts and after
    # every round, only when something changed. Kept: the newest of each of the last `daily` days with
    # a copy, then one a week for `weekly` weeks before those.
    enabled: bool = True
    # Default: log_dir/backups, on the same drive as the database. A synced or other drive (OneDrive,
    # Dropbox, a NAS, a USB stick) keeps the copies safe if the PC's drive dies.
    dir: str | None = None
    daily: int = Field(14, ge=0)
    weekly: int = Field(4, ge=0)
    # Also upload each copy to publish.bucket under s3_prefix (with the same keys as publish). It must
    # stay under backups/: the stack keeps CloudFront from serving only that (tools/aws/stats-site.yaml).
    s3: bool = False
    s3_prefix: str = "backups/"

    @field_validator("s3_prefix", mode="after")
    @classmethod
    def _under_backups(cls, v: str) -> str:
        if v.strip("/").split("/")[0] != "backups" or ".." in v.split("/"):
            raise ValueError("backup.s3_prefix must be backups/ or a folder under it")
        return v


class Config(BaseModel):
    # Optional. None = no league: every round is practice, players go by their GSPro names.
    league_file: str | None = None
    # Earlier seasons and this season's standings, from `import-league`. Optional.
    history_file: str = "data/history.json"
    personas: dict[str, PersonaConfig] = Field(
        default_factory=lambda: {
            "pbp": PersonaConfig(name="Nigel", style="Calm, polished play-by-play voice."),
            "color": PersonaConfig(name="Duke", style="Opinionated color analyst; warm, quick wit and real enthusiasm, playful with the needle, clearly having fun."),
        }
    )
    llm: LLMSection = Field(default_factory=LLMSection)
    tts: TTSConfig = Field(default_factory=TTSConfig)
    screen: ScreenConfig = Field(default_factory=ScreenConfig)
    source: SourceConfig = Field(default_factory=SourceConfig)
    squabbit: SquabbitConfig = Field(default_factory=SquabbitConfig)
    booking: BookingConfig = Field(default_factory=BookingConfig)
    commentary: CommentaryConfig = Field(default_factory=CommentaryConfig)
    news: NewsConfig = Field(default_factory=NewsConfig)
    weather: WeatherConfig = Field(default_factory=WeatherConfig)
    scores: ScoresConfig = Field(default_factory=ScoresConfig)
    trivia: TriviaConfig = Field(default_factory=TriviaConfig)
    overlay: OverlayConfig = Field(default_factory=OverlayConfig)
    health: HealthConfig = Field(default_factory=HealthConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    minimap: MinimapConfig = Field(default_factory=MinimapConfig)
    putt_read: PuttReadConfig = Field(default_factory=PuttReadConfig)
    publish: PublishConfig = Field(default_factory=PublishConfig)
    live: LiveConfig = Field(default_factory=LiveConfig)
    backup: BackupConfig = Field(default_factory=BackupConfig)
    log_dir: str = "logs"
    # Season history: every shot and line. Replays and evals never write to it.
    db_path: str = "logs/season.db"
    # Keep the booth from stealing CPU from GSPro: run it below normal priority so GSPro always
    # wins contention, and optionally pin it to at most this many cores (None = no limit).
    process_priority: Literal["below_normal", "normal", "idle"] = "below_normal"
    max_cpu_cores: int | None = None

    @property
    def segment_llm(self) -> LLMConfig:
        return self.llm.segment or self.llm.live

    @property
    def flight_llm(self) -> LLMConfig:
        return self.llm.flight or self.llm.live

    @property
    def preshot_llm(self) -> LLMConfig:
        return self.llm.preshot or self.llm.live

    @property
    def round_llm(self) -> LLMConfig:
        if self.llm.round:
            return self.llm.round
        return self.segment_llm.model_copy(update={"max_tokens": max(self.segment_llm.max_tokens, 900)})

    @property
    def opener_llm(self) -> LLMConfig:
        return self.llm.opener or self.round_llm

    @property
    def closer_llm(self) -> LLMConfig:
        return self.llm.closer or self.round_llm


def load_config(path: str | Path) -> Config:
    with open(path, encoding="utf-8-sig") as f:  # tolerate a Windows byte-order mark
        return Config.model_validate(yaml.safe_load(f) or {})
