"""Watching GSPro's screen between shots: the HUD, the minimap, the putt read, and the setup it emits.

Part of Engine (commentator/engine/core.py), which is these parts together."""
import asyncio
import time
from dataclasses import replace

from .common import (
    HoleWatch, SCORECARD_DUE_SECONDS, SCORECARD_IDLE_SECONDS, SETUP_LIE_RETRIES, SETUP_LIE_RETRY_SECONDS,
    _box_player, _opening_tee, _setup_key, _tile_yards, hole_label, log,
)
from ..facts import to_yards
from ..hud import Hud, hole_key, name_sig_changed
from ..minimap import JevMinimap, MinimapRead, snapshot as minimap_snapshot, same_map
from ..models import HoleChangeEvent, PreShotEvent, RoundStartEvent, ScorecardEvent
from ..putt_read import REGION as PUTT_REGION, PuttRead, read_break


class HudWatchMixin:
    """Watching GSPro's screen between shots: the HUD, the minimap, the putt read, and the setup it emits."""

    async def _watch_hud(self) -> None:
        """Polls the HUD for a new hole and for a player settling over a new shot."""
        holes = self._hole_watch = HoleWatch()
        undo = None   # (banner, hole number, number read, shots so far) before the last hole change
        last_setup = candidate = None
        candidate_since = 0.0
        candidate_stamp: tuple | None = None
        setup_seq = moved_on = 0
        card_read_at = 0.0
        card_signals = self._card_signals()   # the log's hole and round ends at the last poll
        card_due_until, title_at, card_up = 0.0, None, False   # title_at: the last title read
        away_at, away_seen, tee_polls, away_fires = None, False, 0, 0   # the screen leaving the range or menus
        range_read_at = float("-inf")
        while True:
            await asyncio.sleep(self.cfg.screen.hud_poll_seconds)
            if self._on_range_by_log():
                # The range's HUD only matters for spotting a course's first tee after Tee Off from its
                # own range (unlogged), and OCR'ing it every second there cost GSPro several cores.
                now = time.monotonic()
                if now - range_read_at < self.cfg.screen.range_hud_poll_seconds:
                    self._check_log_round()
                    self._check_round_idle()
                    continue
                range_read_at = now
            # Who was up when this poll began: a setup is kept for the impact read only if no ball was
            # struck, nor the log's player changed, from its first sighting to the end of its read.
            stamp = (self._shot_seq, self.gspro.active_player, time.monotonic())
            poll_fires = self.gspro.fires
            if (signals := self._card_signals()) is None or signals != card_signals:
                card_signals, card_due_until = signals, stamp[2] + SCORECARD_DUE_SECONDS
            read_title = (signals is None or card_up or stamp[2] < card_due_until or title_at is None
                          or stamp[2] - title_at >= SCORECARD_IDLE_SECONDS)
            if read_title:
                title_at = stamp[2]
            try:
                hud = await self.screen.read_hud_async(
                    ["player_box", "hole_box"] + (["scorecard_title"] if read_title else []))
            except Exception:
                log.exception("HUD poll failed")
                continue
            if read_title:
                card_up = hud.scorecard_open
            self._check_log_round()
            self._check_round_idle()
            if self._log_rounds and self.gspro.mode in ("range", "menu"):
                # GSPro logs nothing on Tee Off from a course's own range, so its log can still say
                # range (or menus) at the first tee. An opening tee on screen, held for two polls and
                # after a poll with no player box (the range or the menus themselves, not a stale HUD),
                # is a course. Each new stretch away (End Round included) needs its own such poll. A
                # swing since that poll is left to GSPro's score line for it, which says course itself.
                if (away := (self.gspro.mode, self.gspro.round_ends)) != away_at:
                    away_at, away_seen, tee_polls = away, False, 0
                if not hud.shot_number and not hud.scorecard_open:
                    # Fires counted before the read: one during it is after this sighting of the range.
                    away_seen, tee_polls, away_fires = True, 0, poll_fires
                elif away_seen and _opening_tee(hud) and self.gspro.fires == away_fires:
                    tee_polls += 1
                    if tee_polls >= 2:
                        self._tee_seen_unlogged(hud)
                        away_seen, tee_polls = False, 0
                else:
                    tee_polls = 0
            else:
                away_at, away_seen, tee_polls = None, False, 0
            self._note_practice(hud.player_text, hud.shot_number)
            if self._on_range() or self._practice:
                # The range (and practice mode) isn't a round: no holes, no setups, no round start. Forget
                # the last tee seen so the first one after it reads as new.
                holes, last_setup, candidate = HoleWatch(), None, None
                self._hole_watch = holes
                continue
            if hud.scorecard_open:
                # GSPro shows the scorecard between holes. Once per showing is plenty, and none while
                # GSPro's log has every player's scores (_log_scores_cover): it posts them as they finish.
                if time.monotonic() - card_read_at > 5 and not self._log_scores_cover(self.round):
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
                elif (moved := self._file_moved_to) is not None and moved == self.hole_number \
                        and not self._banner_past(hud, moved):
                    log.info("Banner %s taken as hole %s, where GSPro's round file already had the group",
                             hole_key(hud), moved)
                    self._file_moved_to, undo = None, None
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
                    elif number is not None and self.round is not None and not self.round.ended:
                        number += 1
                    else:
                        number = None   # outside a round there's no count to go on: the round start numbers it
                    undo = (before_key, self.hole_number, self.hole_number_read, self._shot_seq)
                    self.hole_number, self._file_moved_to = number, None
                    await self._events.put(HoleChangeEvent(previous=None, current=hole_label(hud, number),
                                                           course=hud.course))

            # The player box only changes once the previous ball has stopped, so a new
            # (shot number, distance) that holds steady is a player addressing the next shot.
            setup = (_box_player(hud.player_text), hud.shot_number, hud.to_pin)
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
                candidate, candidate_since, candidate_stamp = setup, time.monotonic(), stamp
                continue
            if time.monotonic() - candidate_since >= self.cfg.commentary.preshot_settle_seconds:
                last_setup, candidate, setup_seq, moved_on = setup, None, self._shot_seq, 0
                if (logged := self._log_hole_disagrees(hud)) is not None:
                    self.hole_number, self.hole_number_read, undo = logged, True, None
                    await self._events.put(HoleChangeEvent(previous=None, current=hole_label(hud, logged),
                                                           course=hud.course, correction="log"))
                self._prev_activity, self._last_activity = self._last_activity, time.monotonic()
                await self._emit_setup(hud, candidate_stamp)

    @property
    def putt_reader(self) -> bool:
        """Read putt breaks off the screen: they only feed the commentary, so only on air."""
        return self._putt_read_ok and self.commentary_on

    def _make_minimap(self) -> "JevMinimap | None":
        """The Jev minimap reader when minimap.provider is jev and it can run; otherwise None, and the
        hazard read stays on the preshot vision model."""
        if self.cfg.minimap.provider != "jev" or self.screen is None:
            return None
        if why := JevMinimap.available():
            log.warning("minimap.provider is jev but %s; using the preshot vision model for hazards", why)
            return None
        return JevMinimap(self.cfg.minimap, usage=self.usage)

    def _snap_hole(self, hud: Hud) -> None:
        """Keep the hole's minimap off the tee as the hole replay's background, once per course and hole.
        On or off air: it's a record, like the shots. Runs in the background so the setup read isn't held up.
        Only in a round under way, on the hole it has counted: an opening tee is seen before its round
        starts, when the course and the hole count can still be the last round's (or a menu banner's);
        handle_round_start snaps it once the round has its course and opening hole."""
        r = self.round
        if r is None or r.ended:
            return
        course, hole = r.course or self.current_course, self.hole_number
        if self.screen is None or not course or not hole or (course, hole) in self._hole_snaps:
            return
        # One map per hole serves every round there. Asked at each tee, not remembered: Shotcast Cloud
        # can have the one kept removed (cloud_rounds.send_pictures deletes it), and then it's grabbed again.
        if self.store.has_hole_image(course, hole):
            return
        self._hole_snaps.add((course, hole))   # (a grab under way, or one that failed: not again this session)

        def grab():
            self.screen.detect_mirror()
            return minimap_snapshot(self.screen.grab(self.screen.region("minimap")))

        async def snap() -> None:
            image, ball, pin = await asyncio.to_thread(grab)
            # The store's connection belongs to this thread, so the write happens back here.
            if self.store.record_hole_image(course, hole, image, ball, pin) is not None:
                log.info("Kept the minimap of %s hole %s for the replay (ball and pin %s)", course, hole,
                         "found" if pin else "not found")
            self._hole_snaps.discard((course, hole))   # (kept, or the pin missed: the next tee shot asks again)
            if pin is None:
                self._keep_report_frame("minimap-miss", ball_found=ball is not None)

        task = asyncio.create_task(snap())
        task.add_done_callback(lambda t: t.cancelled() or t.exception() and log.warning(
            "Couldn't keep the minimap of hole %s: %r", hole, t.exception()))

    async def _read_minimap(self, to_pin: float | None, tee: bool | None = None) -> MinimapRead:
        await asyncio.to_thread(self.screen.detect_mirror)
        region = self.screen.region("minimap")
        # Full resolution: the downscaled vision snapshot leaves the minimap's numbers too small to read.
        img = await asyncio.to_thread(self.screen.grab, region)
        if not self.commentary_on:   # went off air during the grab: don't start the paid read
            raise RuntimeError("off air")
        last = self._last_minimap
        if last and last[1:3] == (to_pin, tee) and await asyncio.to_thread(same_map, last[0], img):
            log.info("Minimap unchanged since the last read; reusing it")
            return replace(last[3], seconds=0.0)
        read = await self.minimap.read(img, to_pin, tee)
        self._last_minimap = (img, to_pin, tee, read)
        return read

    async def _read_putt(self) -> PuttRead:
        p = self.cfg.putt_read
        frames = await asyncio.to_thread(self.screen.burst, PUTT_REGION, p.frames, p.interval_seconds)
        return await asyncio.to_thread(read_break, frames)

    def _start_putt_read(self) -> "asyncio.Task[PuttRead]":
        task = asyncio.create_task(self._read_putt())
        task.add_done_callback(lambda t: t.cancelled() or t.exception())
        return task

    async def _emit_setup(self, hud: Hud, stamp: tuple | None = None) -> None:
        """Read the rest of the HUD and start the pre-shot look. The model call is the longest step,
        so it's kicked off the moment the lie is known and runs while the slope (and, on the tee, the
        other players) are still being read, rather than after. The frame it sees is grabbed once."""
        if self._after_round(hud):
            log.info("Setup ignored: %s (shot %s)",
                     "GSPro is in the menus after End Round" if self._log_rounds and self.gspro.mode == "menu"
                     else f"round {self.round.id} is complete and this isn't a new tee", hud.shot_number)
            return
        # Who was up when the player box was first read (the watcher's poll): the rest of the read
        # takes a while, and a swing (and the next player up) meanwhile mustn't be stamped on it.
        stamp = stamp or (self._shot_seq, self.gspro.active_player, time.monotonic())
        if hud.shot_number == 1:
            self._snap_hole(hud)
        # The minimap read starts first so it runs during the HUD read below. It's cancelled for a putt.
        minimap_task = asyncio.create_task(self._read_minimap(
            to_yards(hud.to_pin), None if hud.shot_number is None else hud.shot_number == 1)) if self.minimap is not None and self.commentary_on else None
        if minimap_task is not None:
            self._minimap_tasks.add(minimap_task)
            minimap_task.add_done_callback(self._minimap_tasks.discard)
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
        # and late in a league round, where _stakes_facts compares this player to the field: only
        # commentary uses that, so off air it isn't read.
        rr = self.round
        late = (self.commentary_on and rr is not None and not rr.ended and rr.is_league
                and 1 <= rr.holes - rr.seq + 1 <= 4)
        join = self._should_join(hud)
        rest = ["lie", "player_name", "slope"] + (["others"] if hud.shot_number == 1 or late or join else [])
        tiles_for = stamp[0] if self._tiles_wanted == stamp[0] == self._shot_seq else None
        if tiles_for is not None:
            rest += ["tiles", "tiles_left"]   # the last shot's tiles, hidden while it was followed
        extra = await self.screen.read_hud_async(rest)
        if tiles_for is not None and self._shot_seq == tiles_for and any(
                _tile_yards(extra.tiles, t, "yards") is not None for t in ("carry (game)", "total length")):
            self._late_tiles[tiles_for] = extra.tiles   # read before any swing since: still that shot's
            self._tiles_wanted = None
            while len(self._late_tiles) > 4:
                del self._late_tiles[next(iter(self._late_tiles))]
            self._apply_late_tiles(tiles_for)
        hud.lie = hud.box_lie or extra.lie   # the player box's lie (3.59/4.0), else the minimap's
        screen_lie = hud.lie
        off_tee = (hud.lie or "").lower() != "tee" and hud.shot_number != 1
        file_lie, why = self._file_rest_lie_why(self._player(hud.player_text, None), hud.shot_number) \
            if off_tee else (None, "")
        if file_lie:
            # Where GSPro's round file has the ball at rest outranks the screen.
            agreed = file_lie == (hud.lie or "").lower()
            log.info("Setup lie %s from GSPro's round file (%s)", file_lie,
                     "the screen agreed" if agreed else f"the screen read {hud.lie}")
            if not agreed:
                hud.lie = file_lie
        if not hud.lie and hud.shot_number != 1 and self.cfg.screen.ocr:
            seq = self._shot_seq
            hud.lie = screen_lie = await self._reread_setup_lie(extra.name_sig, seq)
            if self._shot_seq != seq:   # swung during the re-reads: this setup's moment has passed
                log.info("Setup dropped: the ball was struck during the lie re-read")
                for task in (minimap_task, putt_task):
                    if task is not None:
                        task.cancel()
                return
        if off_tee:
            if not file_lie:
                log.info("Setup lie %s (GSPro's round file: %s)",
                         f"{hud.lie} from the screen" if hud.lie else "unread on screen", why)
            # (counted for the round once the setup's taken: handle_preshot)
            hud._lie_source = ("round file", "") if file_lie else ("screen" if hud.lie else "none", why)
        if key := _setup_key(hud):
            self._setup_yards[key[:2]] = "yard" in key[2]
            while len(self._setup_yards) > 8:
                del self._setup_yards[next(iter(self._setup_yards))]
        if hud.lie and (key := _setup_key(hud)):
            self._setup_lies[key] = hud.lie
            while len(self._setup_lies) > 8:
                del self._setup_lies[next(iter(self._setup_lies))]
            if hud.lie != screen_lie:   # (should GSPro's file turn out another round's)
                self._setup_screen_lies[key] = screen_lie
            else:
                self._setup_screen_lies.pop(key, None)
            while len(self._setup_screen_lies) > 8:
                del self._setup_screen_lies[next(iter(self._setup_screen_lies))]
        hud.slope_updown, hud.slope_side = extra.slope_updown, extra.slope_side
        hud.slope_side_degrees = extra.slope_side_degrees
        if (hud.slope_updown is not None or hud.slope_side) and (key := _setup_key(hud)):
            self._setup_slopes[key] = (hud.slope_updown, hud.slope_side, hud.slope_side_degrees)
            while len(self._setup_slopes) > 8:
                del self._setup_slopes[next(iter(self._setup_slopes))]
        if "others" in rest:
            hud.others = extra.others
        self._note_player(hud)
        player = self._player(hud.player_text, None)
        self._add_rehit_penalty(hud, player)
        self._count_added_penalty(hud, player)
        if player and self.plan is not None and self.plan.player and self.plan.player != player:
            # The box shows someone else now: the plan's flight lines name its player (Derek's call aired
            # over Dan's mulligan at Kiawah's 10th, 11-11).
            log.info("Flight plan for %r dropped: the box shows %r", self._display(self.plan.player),
                     self._display(player))
            self.plan = None
        box_before, self._box_since_swing = self._box_since_swing, (self._shot_seq, player)
        if player and hud.shot_number:
            swings = [x for x in self.state.shots if x.player == player]
            stroke = (player, hud.shot_number)
            last = self._replayed_swing(player, hud)
            # The box moved on to someone else after this player's swing and came back to the same stroke:
            # GSPro took the swing back (Dan's re-tees at Kiawah's 10th with no setup of his seen between).
            taken_back = last is not None and box_before is not None and box_before[0] == self._shot_seq \
                and box_before[1] not in (None, player) and bool(self.state.shots) and self.state.shots[-1] is last
            if last is not None and last.outcome in ("holed", "conceded"):
                # That stroke finished the hole, so it can't be replayed: the HUD just hadn't moved on
                # from it yet (a conceded putt was called a mulligan, and its replay announced).
                log.info("Setup dropped: %r's stroke %s already finished the hole (a late read, not a mulligan)",
                         self._display(player), hud.shot_number)
                for task in (minimap_task, putt_task):
                    if task is not None:
                        task.cancel()
                return
            if last is None:
                self._set_up[stroke] = id(swings[-1]) if swings else None
                while len(self._set_up) > 32:
                    del self._set_up[next(iter(self._set_up))]
            elif not taken_back and (stroke not in self._set_up or self._set_up[stroke] not in (
                    id(last), id(swings[-2]) if len(swings) > 1 else None)):
                # The player hit this stroke before its setup settled (a quick putt), and the HUD still
                # showed it as the ball rolled: it's been played, so it isn't a setup (nor a mulligan).
                log.info("Setup dropped: %r already played stroke %s (a late read, not a mulligan)",
                         self._display(player), hud.shot_number)
                for task in (minimap_task, putt_task):
                    if task is not None:
                        task.cancel()
                return
            else:   # a mulligan, set up again: the stroke can be taken back again
                self._set_up[stroke] = id(last)
        if player and stamp[0] == self._shot_seq:
            # Timed from when the player box was first read; a read a swing overtook isn't a setup to come.
            hud._chat_setup = [time.time() - (time.monotonic() - stamp[2]),
                               self._setup_brief(hud, player, hole=self.hole_number, par=self.current_par)]
            self._setups_seen.append(hud._chat_setup)
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
        if (self._shot_seq, self.gspro.active_player) == stamp[:2]:
            self._last_setup = (replace(hud, others=[], tiles={}), *stamp)
        else:
            self._last_setup = None
        await self._events.put(PreShotEvent(hud=hud, image=image, model_task=model_task))

    async def _reread_setup_lie(self, name_sig: tuple | None, seq: int) -> str | None:
        """The lie box read empty at setup (a fade, or something over it): read it again a few times
        while the player is still over the ball. Stops if the name cell shows another player or the
        ball is struck."""
        for attempt in range(1, SETUP_LIE_RETRIES + 1):
            await asyncio.sleep(SETUP_LIE_RETRY_SECONDS)
            if self._shot_seq != seq:
                return None
            again = await self.screen.read_hud_async(["lie", "player_name"])
            if name_sig_changed(name_sig, again.name_sig):
                log.info("Setup lie unread; the player box changed before a re-read")
                return None
            if again.lie:
                log.info("Setup lie unread; re-read %s on attempt %s", again.lie, attempt)
                return again.lie
        log.info("Setup lie unread after %s re-reads", SETUP_LIE_RETRIES)
        return None
