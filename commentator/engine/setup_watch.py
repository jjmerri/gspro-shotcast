"""GSPro's setup screen before a round: its players, format, holes and teams, and the round prepared from them.

Part of the Engine (commentator/engine/core.py), which is these parts together."""
import asyncio
import time

from .common import (
    BOOKINGS_FRESH_SECONDS, FORMAT_SETTLE_SECONDS, PendingOpening, SETUP_AWAY_POLL_SECONDS, _HOLES_TAB_LIT,
    _MATCH_TAB_LIT, _PLAYERS_TAB_LIT, _nines_picked, _play_button_course, _setup_slot_name, _slot_state, _tab_lit,
    _tint_groups, log,
)
from ..league import course_match_score
from ..round import Round, _join as _join_names
from ..screen import log_failure as log_capture_failure, tone_of


class SetupWatchMixin:
    """GSPro's setup screen before a round: its players, format, holes and teams, and the round prepared from them."""

    async def _watch_setup(self) -> None:
        probed = callable(getattr(self.screen, "setup_frame", None)) and callable(
            getattr(self.screen, "region_tones", None)) and callable(getattr(self.screen, "region", None))
        sampler = asyncio.create_task(self._sample_setup_tabs()) if probed else None
        try:
            await self._watch_setup_loop()
        finally:
            if sampler is not None:
                sampler.cancel()

    def _on_range_by_log(self) -> bool:
        """GSPro's log has it on the range, where its setup screen isn't up (only End Round leaves it)."""
        return self._log_rounds and self.gspro.mode == "range"

    async def _sample_setup_tabs(self) -> None:
        """Keep the latest frame showing the setup screen's PLAYERS tab with names on it, and the nines
        last checked on its HOLES tab, sampled every quarter second. The watch's own polls are ~2 s apart
        once a region needs reading, so a player who lands on PLAYERS (GSPro's default tab) and clicks on
        to MATCH SETTINGS within a second or two would otherwise never have the names read, and one who
        unchecks a nine and presses Play just as quickly would get an 18-hole round. Only the small tab
        boxes are grabbed each time; the full frame only when a tab is lit."""
        slots = [f"setup_slot_{i}" for i in range(1, 9)]
        last_nines: tuple[int, int] | None = None

        def lit(name, level):
            # Looked up each time: the regions move when the booth learns which GSPro is running.
            region = self.screen.region(name)
            return region is not None and _tab_lit(tone_of(self.screen.grab(region)), level)

        def sample_holes():
            if not lit("setup_holes_tab", _HOLES_TAB_LIT):
                return None
            at = time.monotonic()
            frame = self.screen.setup_frame()
            picked = _nines_picked(self.screen.region_tones(
                ["setup_holes_tab", "setup_front9_box", "setup_back9_box"], frame))
            if picked:   # (a tab read as selected only on a frame that showed the setup screen's)
                self._setup_tab_lit_at = at
            return at, picked

        def sample():
            if not lit("setup_players_tab", _PLAYERS_TAB_LIT):
                return None
            at = time.monotonic()
            frame = self.screen.setup_frame()
            tones = self.screen.region_tones(["setup_players_tab", *slots], frame)
            states = [_slot_state(tones[n]) for n in slots if n in tones]
            if not _tab_lit(tones["setup_players_tab"], _PLAYERS_TAB_LIT) or "list" in states or "filled" not in states:
                return None
            self._setup_tab_lit_at = at
            return at, frame, tones

        while True:
            await asyncio.sleep(0.25)
            self._sync_screen_ui()
            if (self.round is not None and not self.round.ended) or self._on_range_by_log():
                # (The range's sky behind the tabs reads as a lit tab: full frames 4x a second for nothing.)
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
        roster_course: str | None = None   # the setup course `roster` was read on
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
        gate_read_at = float("-inf")   # when the Play button was last OCR'd away from the menus
        setup_began = float("inf")     # when this setup was first seen (for the no-tab-lit check)
        unlit_frame = None             # this setup's latest frame, kept should no tab ever read as selected
        while True:
            await asyncio.sleep(max(c.preround_poll_seconds, 0.2))
            self._sync_screen_ui()
            prev_at, frame_at = frame_at, time.monotonic()
            if self.round is not None and not self.round.ended:
                # Mid-round the setup regions land on the live HUD; nothing to read until it's over.
                seen_setup, course, roster, self._opener_prepared_for = False, None, [], frozenset()
                slot_order, slot_cache, gate_cache = [], {}, {}
                continue
            if self._on_range_by_log():
                # No setup screen on the range (only End Round, which the log shows, leaves it for one), and
                # its live scene changes every poll, so each read was OCR the game paid for.
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
                    players_tab = tab is not None and _tab_lit(tab, _PLAYERS_TAB_LIT)
                    if players_tab:
                        players_read_at = grabbed_at
                    lit = players_tab or any(
                        (t := tones.get(n)) is not None and _tab_lit(t, level)
                        for n, level in (("setup_holes_tab", _HOLES_TAB_LIT), ("setup_match_tab", _MATCH_TAB_LIT)))
                    # Away from the menus by GSPro's log: on a course with no setup tab showing (`Selected
                    # Course:` is logged as the setup screen opens, and its load screen must still be read
                    # at once once the setup was seen). The range was skipped above.
                    away = self._log_rounds and self.gspro.mode == "course" and not seen_setup and not lit
                    if p is None and away:
                        if frame_at - gate_read_at < SETUP_AWAY_POLL_SECONDS:
                            continue
                        gate_read_at = frame_at
                    # On the PLAYERS tab only the Play button matters; elsewhere also the load title.
                    gate_names = ["setup_course"] if players_tab else ["setup_course", "load_course", "course_select"]
                    gate_lines, gate_cache = await asyncio.to_thread(
                        self.screen.read_region_lines_cached, gate_names, frame, gate_cache)
                    gate = {n: " ".join(gate_lines.get(n, [])) for n in ("setup_course", "load_course", "course_select")}
                else:
                    frame, gate = await asyncio.to_thread(self.screen.grab_and_read,
                                                          ["setup_course", "load_course", "course_select"])
            except Exception:
                log_capture_failure("Setup-screen read failed", logger=log)
                continue
            play_course = _play_button_course(gate.get("setup_course", ""))
            # The course list's title: "Select Course" (3.1), "LOCAL MATCH" (3.59).
            if not play_course and any(t in gate.get("course_select", "").lower() for t in ("select course", "local match")):
                self._fetch_bookings("course select", fresh_within=BOOKINGS_FRESH_SECONDS)
            last_play, seen_play = seen_play, play_course
            if held:
                if not play_course:
                    continue
                log.info("Back on the setup screen (%r) after the %s opener for %r: dropping it and "
                         "reading the new setup", play_course, "aired" if p.aired else "held", p.course)
                self._opener_gen += 1
                self._pending_opening, self._opener_prepared_for = None, frozenset()
            if play_course:
                if not seen_setup:
                    self._fetch_bookings("setup screen", fresh_within=BOOKINGS_FRESH_SECONDS)
                    self._live_preroll()
                if roster and roster_course and self._setup_course_moved(roster_course, play_course):
                    # Another course set up: the players read on the last one are only a guess at this
                    # one's (held for the tee) until its PLAYERS tab is read.
                    self._last_setup_roster = (list(roster), list(slot_order))
                    roster, slot_order, self._played_setup, self._setup_seen = [], [], None, None
                to_read = slots
                states: dict[str, str] = {}
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
                    self._setup_course = play_course
                    fresh = not seen_setup or play_course != course
                    if fresh:
                        setup_began = grabbed_at
                        self._setup_tints = None   # another setup's team colours aren't this one's
                        # A HOLES sample since the last poll that didn't show this setup is this setup's;
                        # straight from another course's setup, one in between could be either's.
                        holes_since = frame_at if last_play and last_play != play_course else prev_at
                    if any((t := tones.get(n)) is not None and _tab_lit(t, level) for n, level in (
                            ("setup_players_tab", _PLAYERS_TAB_LIT), ("setup_holes_tab", _HOLES_TAB_LIT),
                            ("setup_match_tab", _MATCH_TAB_LIT))):
                        self._setup_tab_lit_at = grabbed_at   # (the Play button read: the setup screen's tabs)
                    self._read_setup_holes(tones, fresh=fresh, at=grabbed_at)
                    self._take_holes_sample(holes_since)
                    try:
                        mode_cache = await self._read_setup_format(tones, frame, fresh, mode_cache)
                    except Exception:
                        log.exception("Setup game-mode read failed")
                    if (tab := tones.get("setup_holes_tab")) is not None and _tab_lit(tab, _HOLES_TAB_LIT):
                        self._keep_fixture_frame("setup_holes", frame)
                    if not players_tab:
                        unlit_frame = frame
                    if course and play_course == course and roster and c.preround:
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
                # yet adding or swapping a player still fires the front-load with no wait. A slot the
                # pixels show empty isn't a dropout, though: whoever was read there is gone at once (a
                # name misread off a closing dropdown's last frame, say, mustn't outlast its one frame).
                gone = set(roster) - set(names)
                emptied = bool(gone) and all(
                    i < len(slots) and states.get(slots[i]) == "empty"
                    for i, n in enumerate(slot_order) if n in gone) and gone <= set(slot_order)
                accept = bool(names) and (not set(names) < set(roster) or names == last_read or emptied)
                last_read = names
                if accept:
                    roster, roster_course = names, play_course
                    slot_order = positional
                    self._last_setup_roster = (list(names), list(positional))
                    if probed and players_tab:
                        self._keep_fixture_frame("setup_players", slot_frame)
                        self._note_setup_tints(positional, [getattr(tones.get(s), "tint", None) for s in slots])
                # Log on first detection and on every accepted roster change, so a live setup read can
                # be watched tracking the picker rather than looking stuck on the first team seen.
                if (not seen_setup) or (accept and names != logged_roster):
                    raw = {s: ls for s in slots if (ls := slot_lines.get(s))}
                    log.info("Setup read: course %r, roster %s (raw slots %s)", play_course, names, raw)
                    logged_roster = names
                seen_setup, course = True, play_course
                # Compose the league opener now, while still on the setup screen, so a fast course load
                # doesn't catch the LLM mid-call. Recomposes if the roster changes.
                if c.preround:
                    self._maybe_front_load(play_course, roster, slot_order)
                continue
            # Not on the setup screen. Fire only when the course's load screen is up (a positive signal
            # that Play was pressed): the load-screen title matches the course we set up. A popup over
            # the button reads neither, so we just keep state and wait.
            load_title = gate.get("load_course", "").strip()
            # Backing out to course select puts its search bar ("pebble  FILTERS ...") where the load
            # title sits; a search still holding the course's name isn't its load screen.
            search_bar = "FILTERS" in load_title.upper()
            loading = bool(seen_setup and course and load_title and not search_bar
                           and course_match_score(load_title, course) >= 0.6)
            if loading and self._other_course(course, load_title):
                # Another course loading than the setup last read (Pinehurst No. 4, not No. 2): its own
                # opener, not the setup's, and its players went unread (a setup missed), so the setup's
                # are only a guess, held for the tee to confirm.
                log.info("Load screen for %r, not the setup's %r: the opener is for the course loading",
                         load_title, course)
                if roster:
                    self._last_setup_roster = (list(roster), list(slot_order))
                course, roster, slot_order, self._setup_seen, self._played_setup = load_title, [], [], None, None
                # (nor its nines, game mode or team colours: GSPro's defaults until the tee says otherwise)
                self._setup_holes, self._setup_holes_read, self._holes_snap = None, False, None
                self._setup_format, self._setup_mode_read, self._setup_tints = None, False, None
                self._setup_course = None
            if loading and probed:
                self._take_holes_sample(holes_since)   # nines changed in the moment before Play was pressed
                if self._setup_tab_lit_at < setup_began and unlit_frame is not None:
                    # Every tab click went unseen (11-17: GSPro's picture moved 17 px inside its window),
                    # so the players and nines went unread. Keep what the setup looked like to see why.
                    log.warning("Setup for %r: no setup tab ever read as selected, so its players and holes "
                                "went unread; keeping the setup frame for a problem report", course)
                    self._keep_fixture_frame("setup_unlit", unlit_frame)
                setup_began, unlit_frame = float("inf"), None
            if loading and roster:
                self._played_setup = (course, list(roster), list(slot_order), time.monotonic())
                self._setup_seen = None
            elif seen_setup and course and roster and not search_bar:
                left = (course, list(roster), list(slot_order))
                if self._setup_seen is None or self._setup_seen[:3] != left:
                    self._setup_seen = (*left, time.monotonic())
            if loading and not c.preround:
                seen_setup, course, roster, slot_order, slot_cache = False, None, [], [], {}
                continue
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
                    self._pick_round_voice(p.round)
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
        setup = self._setup_holes if self._setup_holes_read or not r.is_league else self._unread_holes()
        if setup and (r.start_hole, r.holes) != setup:
            log.info("Round %s: the setup screen changed to %s holes from hole %s after the opener was made",
                     r.id, setup[1], setup[0])
            r.start_hole, r.start_hole_assumed = setup[0], False
            r.set_holes(setup[1], confirmed=self._setup_holes_read)   # re-applies the course card for the new start

    def _apply_setup_format(self, r: Round) -> None:
        """A scramble or alternate shot picked on the setup screen: the round is one from the start. Two
        players are one team; with more, the team colours on the PLAYERS tab say who's together (and a
        scramble's log picks still have the last word). Kept on the round: the setup screen's read is
        gone once it starts, and a league round made practice plays it again (_play_setup_format)."""
        names = [n for n in (r.slot_names or list(r.roster)) if n]
        groups = ([names] if len(names) == 2 else self._setup_colour_teams(r)) \
            if self._setup_format in ("scramble", "alternate") else None
        r.setup_mode, r.setup_groups = self._setup_format, groups
        self._play_setup_format(r)

    def _play_setup_format(self, r: Round) -> None:
        mode, groups = r.setup_mode, r.setup_groups
        if mode in ("shamble", "alternate"):
            r.no_scramble = True   # GSPro copies balls between teammates here too: not a scramble
        if mode not in ("scramble", "alternate"):
            return
        names = [n for n in (r.slot_names or list(r.roster)) if n]
        if len(names) > 2 and groups and not r.is_league:
            r.colour_teams = groups
        if mode == "scramble" and (r.format != "scramble" or groups) \
                and r.play_scramble(groups or []):
            log.info("Round %s: a scramble, from the setup screen%s", r.id,
                     f" (teams {groups})" if groups and len(names) > 2 else "")
        if mode == "alternate" and (groups or r.is_league) and r.play_alternate(groups or []):
            log.info("Round %s: alternate shot, from the setup screen%s", r.id,
                     f" (teams {groups})" if len(names) > 2 else "")

    def _note_setup_tints(self, slots: list[str | None], tints: list) -> None:
        """Keep the PLAYERS tab's pill tints for its slots (logged when the teams they make change)."""
        before = self._setup_tints
        self._setup_tints = (list(slots), list(tints))
        if before is None or _tint_groups(*before) != _tint_groups(slots, tints):
            if (groups := _tint_groups(slots, tints)) is not None:
                log.info("Setup PLAYERS tab: team colours %s", groups)

    def _setup_colour_teams(self, r: Round) -> list[list[str]] | None:
        """The round's players grouped by their name pills' team colour on the setup screen, as the
        round spells them; None unless every slot's colour was read and its players are the round's."""
        if self._setup_tints is None:
            return None
        slots, tints = self._setup_tints
        groups = _tint_groups(self._as_round_names(r, slots, pool=list(r.roster)), tints)
        if groups is None or sorted(n for g in groups for n in g) != sorted(r.roster):
            return None
        return groups

    async def _read_setup_format(self, tones: dict, frame, fresh: bool, cache: dict) -> dict:
        """GSPro's game mode, while the MATCH SETTINGS tab is up (see _setup_format)."""
        if fresh:
            self._setup_format, self._setup_mode_read = None, False
        tab = tones.get("setup_match_tab")
        if tab is None or not _tab_lit(tab, _MATCH_TAB_LIT):
            return cache
        lines, cache = await asyncio.to_thread(self.screen.read_region_lines_cached, ["setup_game_mode"],
                                               frame, cache)
        text = " ".join(lines.get("setup_game_mode", [])).upper()
        if not text.strip():
            return cache
        self._keep_fixture_frame("setup_match", frame)
        self._setup_mode_read = True   # (None below is then stroke play or another every-ball mode)
        fmt = next((f for word, f in (("SCRAMBLE", "scramble"), ("SHAMBLE", "shamble"), ("ALTERNATE", "alternate"))
                    if word in text), None)
        if fmt != self._setup_format:
            log.info("Setup MATCH SETTINGS tab: game mode %r%s", text.strip(), f" ({fmt})" if fmt else "")
            self._setup_format, self._setup_format_at = fmt, time.monotonic()
        return cache

    def _read_setup_holes(self, tones: dict, fresh: bool, at: float | None = None) -> None:
        """Which nines the round will play, from the setup screen's HOLES tab: both boxes is 18 from hole
        1, FRONT 9 alone is 9 from 1, BACK 9 alone is 9 from 10. GSPro defaults to all 18, so a setup
        screen whose HOLES tab is never opened is taken as 18 (unconfirmed). `fresh` marks a new setup
        (first seen, or another course), which starts back at that default. `at` is when the frame was
        grabbed. Single holes left out aren't tracked."""
        if fresh:
            self._setup_holes, self._setup_holes_read, self._setup_holes_at = (1, 18), False, float("-inf")
            self._setup_began_at = time.monotonic() if at is None else at
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
        if key == self._opener_prepared_for and self._opener_prepared_course is not None \
                and self._setup_course_moved(self._opener_prepared_course, course):
            # Another course picked for the same players: the opener written for the last one is out of
            # date, so drop it (and any compose still running) and write this course's.
            log.info("Setup course changed from %r to %r; the opener is written again",
                     self._opener_prepared_course, course)
            p = self._pending_opening
            if p is not None and p.awaiting_load and not p.aired:
                self._pending_opening = None
            self._opener_gen += 1
            self._opener_prepared_for = frozenset()
        if not key or (key == self._opener_prepared_for and self._setup_holes == self._opener_prepared_holes
                       and self._setup_format == self._opener_prepared_format
                       and self._setup_teams_key() == self._opener_prepared_teams):
            return
        if (key == self._opener_prepared_for and self._setup_holes == self._opener_prepared_holes
                and self._setup_format != self._opener_prepared_format
                and time.monotonic() - self._setup_format_at < FORMAT_SETTLE_SECONDS):
            # Only the game mode changed, and it may still be changing; a later poll writes it. The
            # opener written for the old mode is out of date: drop it (and any compose still running)
            # so neither the load screen nor the tee airs it.
            p = self._pending_opening
            if p is not None and p.awaiting_load and not p.aired:
                self._pending_opening = None
            self._opener_gen += 1
            # Matches no mode, so a switch back to the original one still gets its opener re-written.
            self._opener_prepared_format = "stale"
            return
        self._opener_prepared_for, self._opener_prepared_holes = key, self._setup_holes
        self._opener_prepared_course = course
        self._opener_prepared_format = self._setup_format
        self._opener_prepared_teams = self._setup_teams_key()
        self._opener_gen += 1
        log.info("Front-loading the opener for roster %s on %r", sorted(key), course)
        self._bg(self._prepare_speculative_round(course, list(roster), air=False, gen=self._opener_gen,
                                                 slot_names=list(slot_order) if slot_order else None))

    def _setup_teams_key(self) -> frozenset | None:
        """The team colours' teams in a scramble or alternate shot set up (what an opener written for
        it says about partners), None in any other mode or before they're read."""
        if self._setup_format not in ("scramble", "alternate") or self._setup_tints is None:
            return None
        groups = _tint_groups(*self._setup_tints)
        return frozenset(frozenset(g) for g in groups) if groups else None

    def _front_loaded_fits(self, course: str, roster: list[str]) -> bool:
        """The front-loaded opener still matches the roster and course now on the load screen (league
        or practice; the caller airs a league one over the load and holds a practice one for the tee)."""
        p = self._pending_opening
        if p is None:
            return False
        return (p.roster_key == self._roster_key(roster) and bool(p.course) and bool(course)
                and course_match_score(p.course, course) >= 0.6 and not self._other_course(p.course, course)
                and (self._setup_holes is None or (p.round.start_hole, p.round.holes) == self._setup_holes)
                and (self._setup_format != "scramble" or p.round.format == "scramble")
                # Written before a game-mode change still waiting to settle: out of date.
                and self._setup_format == self._opener_prepared_format
                and self._setup_teams_key() == p.colour_teams
                # Still the same kind of round: a practice booking can turn up after it was written.
                and (self._classify_round(roster, course)[1] is None) == p.is_league)

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
        await self._bookings_ready()
        entry, practice = self._classify_round(names, course)
        is_league = practice is None
        # Say the league's name for the course, not GSPro's (e.g. Wentworth, not Balsams Panorama), and
        # still find the course-card folder by GSPro's name. Works whether or not it's a league round.
        sched = self._course_week(course) or {}
        if is_league:
            await self._apply_live_handicaps(self._round_players(names))
        elif air:
            log.info("Setup read %s on %r is a practice round (%s); preparing its opener", names, course, practice)
        colour_teams = self._setup_teams_key()   # what _build_round's teams come from
        lookup = self._card_course(course)
        r = self._build_round(names, sched.get("course") or course, entry if is_league else {}, is_league,
                              card_course=lookup, use_current_hole=False,
                              slot_names=slot_names)   # None (not the filtered roster) is the safe signal
        self._doubt_card_notes(r.course_card, course, lookup)
        if guessed:
            r.setup_slots = None   # the last roster read, not this setup's
            r.slot_guess = True
        # The first setup slot (top-left) is first on the tee, EXCEPT in alternate shot, where who tees
        # off isn't fixed to one person, so no starter is named there. An unplaceable practice round's
        # opening hole is still unknown until the tee, so _opener_io leaves the hole out too.
        first = None if r.format == "alternate" else (
            (self.league.match_player(names[0]) or names[0]) if names else None)
        user, facts_text = self._opener_io(r, first=first, preround=True)
        lines = await self._compose("opener", self.opener_llm, user, facts_text, [], mode="round",
                                    system=self._system_league if is_league else self._system_practice,
                                    max_words=self.cfg.commentary.opener_max_words, about=r)
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
                                               roster_key=self._roster_key(names), guessed=guessed,
                                               colour_teams=colour_teams)
        if aired:
            self._pick_round_voice(r)
            self._air_lines("opener", lines)  # play over the course load
            log.info("Early opener aired during the load (%s) for %s %s: %d line(s)",
                     "league" if is_league else "practice", r.course, sorted(players), len(lines))
        elif awaiting:
            log.info("Opener front-loaded (%s) for %s %s, ready to air/hold on the load: %d line(s)",
                     "league" if is_league else "practice", r.course, sorted(players), len(lines))
        else:
            log.info("Early opener prepared (%s, last roster) for %s %s, held for the tee: %d line(s)",
                     "league" if is_league else "practice", r.course, sorted(players), len(lines))

    def _no_members(self, r: Round | None) -> bool:
        """A practice round with nobody from this season's league roster (a guest playing alone)."""
        return bool(r and not r.is_league and r.sides and not any(
            self.league.player_team.get(p) for s in r.sides for p in s.players))

    def _outsider_facts(self, r: Round | None) -> list[str]:
        if not self._no_members(r):
            return []
        return [f"Nobody in this round plays in {self.league.name} this season. Don't welcome anyone to the "
                "league, and don't mention the league, points, standings or league play: no \"practice "
                "round\" talk either. It's just a round of golf; call it."]

    def _league_facts(self, r: Round, closing: bool = False) -> list[str]:
        """Each team's league history and standings: Squabbit's when it was read at the round start,
        else the imported spreadsheets'. Plus the week so far and the week's closest to the pin."""
        facts = self._outsider_facts(r)
        standings = self._board_standings(r) if r.is_league else None
        for team in r.teams:
            players = self.league.team_players.get(team)
            if not players:
                continue
            label = self.league.team_name(team)
            if not closing:
                here = {p for side in r.sides if side.team == team for p in side.players}
                if here and (away := [p for p in players if p not in here]) and r.teams_guessed():
                    # Tonight's teams aren't known: "only Paul of Paul and Travis" aired as "Paul's
                    # flying solo" in a scramble he played with Josh.
                    facts.append(f"{_join_names(away)} ({label}) is not here: never say {_join_names(away)} "
                                 "is playing, here or in the building.")
                elif here and away:
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
