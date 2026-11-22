"""Telling when a round starts, goes on and ends, from the screen and GSPro's log, and keeping the log's scores.

Part of Engine (commentator/engine/core.py), which is these parts together."""
import asyncio
import re
import time

from .common import (
    AFTER_ROUND_QUIET_SECONDS, RANGE_TEE_SHOT_WAIT_SECONDS, UNPLAYED_SHOT_WAIT_SECONDS, _gspro_name,
    _practice_toolbar, gspro_ui, hole_label, log,
)
from ..hud import Hud, Scorecard
from ..models import RoundEndEvent, ScorecardEvent, ShotEvent
from ..round import Round, TEAM_BALL


class RoundDetectMixin:
    """Telling when a round starts, goes on and ends, from the screen and GSPro's log, and keeping the log's scores."""

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
        if r is None or hud.shot_number == 1 or self._struck_new_round():
            return False
        if not r.ended:
            return r.log_complete
        # Nothing hit since the round ended: whatever GSPro shows now (a 1,450-yard "putt" on a hole
        # past the end of the round got a line three minutes after the sign-off) isn't a shot to call.
        if self._log_rounds and self.gspro.fires <= self._shots_at_round_end:
            return True
        return time.monotonic() - self._round_ended_at < AFTER_ROUND_QUIET_SECONDS

    def _should_join(self, hud: Hud) -> bool:
        """The booth started with a round already under way (GSPro's log shows shots on this course
        and the booth isn't tracking a round): pick it up at the first setup, once. Or one whose start it
        missed, which a scorecard showed (_note_idle_card)."""
        struck = self._struck_new_round()
        card = self._card_join and self._log_rounds and (self.round is None or self.round.ended)
        if not struck and not card and (self._join_tried or not self._log_rounds or self.round is not None):
            return False
        g = self.gspro
        if g.mode != "course" or not g.shots_since_course or g.first_hole is None or g.current_hole is None:
            return False
        if hud.shot_number == 1 and not card and self._teed_off_before_setup():
            return False   # a round start, whose first tee shot beat the first setup read (_is_round_start)
        self._join_tried = True   # _card_join stays armed until a round starts: this setup may yet be dropped
        return True

    def _note_idle_card(self, card: Scorecard) -> None:
        """A scorecard read with no round in progress. Holes on it, with GSPro on a course gone onto since
        the last round ended and shots played there, mean a round is under way whose start the booth
        missed (Bethpage 11-12: GSPro windowed, its title bar pushed the HUD down, and no tee's shot number
        read, so no opening tee was ever seen): join it at the next setup. The last round's own card, or
        that round played on past where the booth ended it, isn't a new one."""
        g = self.gspro
        if self._card_join or not self._log_rounds or not any(card.scores.values()):
            return
        if g.mode != "course" or not g.shots_since_course or g.course_entries == self._courses_at_round_end:
            return
        self._card_join = True
        log.info("Scorecard shows holes played with no round in progress: a round under way the booth "
                 "missed the start of; joining it at the next setup")

    def _teed_off_before_setup(self) -> bool:
        """Every shot GSPro logged on this course came while the booth was watching, all on the hole
        the round started on."""
        g = self.gspro
        return g.shots_since_course <= g.live_shots_since_course and g.current_hole in (None, g.first_hole)

    def _is_round_start(self, hud: Hud) -> bool:
        """The opening-tee signal: everyone on shot 1 at even par, every other player still the
        full hole away. The hole number doesn't matter, so a round can start on the front or the
        back nine. The one look-alike is the next tee after everyone pars the opening hole, told
        apart by the scorecard already showing a played hole."""
        if hud.shot_number != 1 or self._on_range() or self._practice:
            return False
        if self._log_rounds and self.gspro.mode == "menu":
            # GSPro's last word was End Round: the tee on screen is a round on its way out, not a new one.
            log.info("Not a round start: GSPro is back in the menus (End Round), not on a course")
            return False
        if self._log_rounds and self.round is None and (n := self.gspro.shots_since_course):
            # The booth came up after play began here: that's a round to join, not one to open. Unless
            # it saw every one of them, all tee shots on the opening hole by players other than this
            # one: a quick first swing, struck before the first setup read (2 s after the hole showed).
            if not (self._teed_off_before_setup() and n <= len(hud.others)):
                log.info("Not a round start: GSPro's log has %s shots on this course already", n)
                return False
            log.info("Round start: the %s shot(s) GSPro logged on this course were opening tee shots, struck "
                     "before the first setup read", n)
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

    async def _gspro_played(self, fires0: int, impact: float) -> bool:
        """Whether GSPro took the shot. The launch monitor sends a swing in the menus or during a
        course's loading screen too (a practice swing while it loads), but GSPro plays none of them:
        its log never says "Shot Fired". Those aired as shots on the last hole and kept the opener
        waiting. Checked here only outside a round, so a shot in play never waits on the log before its
        flight call; in a round, handle_shot checks it once the ball is followed (_gspro_fired)."""
        if not self._fire_log_followed():
            return True   # GSPro's log isn't being followed: nothing to tell by
        r = self.round
        if r is not None and not r.ended and self.gspro.mode == "course":
            return True
        return await self._gspro_fired(fires0, impact)

    def _sync_screen_ui(self) -> None:
        """Point the screen reader at the menus of the GSPro that's running (see gspro_ui)."""
        if self.screen is None:
            return
        ui = gspro_ui(self.gspro.version, self._follower.path if self._follower else None)
        if ui != getattr(self.screen, "ui", None):
            log.info("GSPro %s: reading its %s menus", self.gspro.version or "(version not read yet)", ui)
            self.screen.ui = ui

    def _fire_log_followed(self) -> bool:
        return bool(self._log_truth and self._follower is not None and self._follower.primed and self.gspro.version)

    def _card_signals(self) -> tuple | None:
        """The hole and round ends GSPro's log has shown, which put the scorecard on screen, when the log
        can be trusted to show them: a followed log from 3.59 on, which logs "AllPlayersHoledOut" for
        every hole. None (3.1, which misses some, or no log followed): the card's title is read every poll."""
        if not self._fire_log_followed() or gspro_ui(self.gspro.version) != "new":
            return None
        g = self.gspro
        return g.holes_finished, g.round_completes, g.round_ends, g.hole_outs, g.gimmies

    async def _gspro_fired(self, fires0: int, impact: float, event: ShotEvent | None = None) -> bool:
        """Whether GSPro's log has this shot's "Shot Fired", waiting until UNPLAYED_SHOT_WAIT_SECONDS
        after impact. GSPconnect can also send a swing GSPro never plays mid-round (it hung and was
        restarted): it aired as a 2-yard drive and went into the stats. A shot that queued behind other
        events is looked for from when it arrived, so its fire isn't taken for an earlier or later shot's."""
        if event is not None and event.received_at is not None and event.received_fires is not None:
            fires0, impact = event.received_fires, event.received_at
        deadline = impact + UNPLAYED_SHOT_WAIT_SECONDS

        def fired() -> bool:
            # Only a fire within this shot's window: a later real swing mustn't vouch for a phantom.
            n = self.gspro.fire_of(fires0, impact)
            return n is not None and self.gspro._fire_at.get(n, float("inf")) <= deadline

        while not fired():
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.1)
        return True

    def _on_range(self) -> bool:
        """GSPro is on the driving range, where the booth stays off: no commentary, nothing recorded."""
        on = self._log_rounds and self.gspro.mode == "range"
        if on != self._range_noted:
            self._range_noted = on
            log.info("GSPro %s the range: booth %s", "is on" if on else "left",
                     "off until a round is set up" if on else "back on")
        return on

    def _note_practice(self, player_text: str | None, shot_number: int | None) -> None:
        """Track GSPro's practice mode from the player box: its toolbar there means practice; a real
        player box (a shot number) means a round's course. Anything else (menus, a blank read) keeps
        the last word."""
        if _practice_toolbar(player_text):
            on = True
        elif player_text and shot_number is not None:
            on, self._round_box_seen = False, True
        else:
            return
        if on != self._practice:
            self._practice = on
            if on:
                self.plan = None   # a setup's flight lines are for a round's shot, not a practice swing
            log.info("GSPro %s practice mode: booth %s", "is in" if on else "left",
                     "off (no commentary, no shots saved)" if on else "back on")

    def _practice_or_unseen(self) -> bool:
        """No early call (one before the impact read): GSPro's in practice mode, or the screen's never
        shown a round's player box, so it may be practice's first swing, struck before a HUD poll."""
        return self._practice or (self.screen is not None and self.cfg.screen.ocr and not self._round_box_seen)

    async def _course_shot_off_own_range(self, event: ShotEvent) -> bool:
        """Whether a shot struck while GSPro's log says range was really its course's first: Tee Off from
        a course's own range logs nothing, and a swing before the screen showed the tee came as a range
        shot. GSPro logs a score line for it once the ball stops (the range logs none), and that line
        crediting this shot's own fire (fired as it was struck) makes it a course shot, held since
        impact. The screen is read now, while it still shows this shot rather than the next."""
        g = self.gspro
        if not (g.own_range and self._fire_log_followed()):
            return False
        impact = event.impact_at or event.received_at or time.monotonic()
        base = event.received_fires if event.received_fires is not None else g.fires
        state = event.impact_state[0] if event.impact_state else self._impact_state()
        setup = event.setup_hud or self._setup_at_impact(impact)
        read = event.impact_read
        if read is None and self.screen is not None and self.cfg.screen.ocr:
            read = asyncio.ensure_future(self._read_impact_hud("[range-held shot]"))
        fired_by = impact + UNPLAYED_SHOT_WAIT_SECONDS
        deadline = time.monotonic() + RANGE_TEE_SHOT_WAIT_SECONDS
        while True:
            n = g.fire_of(base, impact)
            if n is None and time.monotonic() > fired_by or g._fire_at.get(n, float("-inf")) > fired_by:
                taken = False   # GSPro never played it: a later fire is another swing's
            elif g.mode == "course" and g.fire_info(n).get("player") is not None:
                taken = True
            elif g.mode == "menu" or time.monotonic() >= deadline:
                taken = False
            else:
                await asyncio.sleep(0.2)
                continue
            break
        if not taken:
            if read is not None and read is not event.impact_read:
                read.cancel()
            return False
        log.info("Shot struck with GSPro's log on the range: its score line puts it on the course; taking it "
                 "as a course shot (club %s, carry %.1f)", event.shot.club, event.shot.carry or 0)
        # Its hole is the course's opening tee, not the one the booth last had (the round before the
        # range, often another course): GSPro's log numbers it (0-based), the banner on screen has its par.
        banner = Hud()
        if self.screen is not None and self.cfg.screen.ocr:
            try:
                banner = await self.screen.read_hud_async(["hole_box"])
            except Exception:
                log.exception("Hole banner read failed")
        logged = g.fire_info(n).get("hole")
        hole = hole_label(banner, logged + 1 if logged is not None else banner.hole_number) or None
        event.impact_at, event.impact_state = impact, (state, hole, banner.par, *self._impact_hole()[2:])
        event.impact_read, event.setup_hud = read, setup
        return True

    def _struck_new_round(self) -> bool:
        """An opening drive taken off a course's own range (GSPro's score line for it took the log off the
        range) began a round the booth hasn't got: join it at the next setup, even with the last round
        still held as over (its shot 2 can't open one)."""
        g = self.gspro
        if g.range_exits != self._range_exits_seen:
            self._range_exits_seen = g.range_exits
            if self.round is None or self.round.ended:
                self._new_round_struck = g.round_ends
        if self._new_round_struck is not None and (g.round_ends != self._new_round_struck or g.mode != "course"):
            self._new_round_struck = None
        return self._new_round_struck is not None and (self.round is None or self.round.ended)

    def _tee_seen_unlogged(self, hud: Hud) -> None:
        """An opening tee on screen while GSPro's log says range or menus: it's on a course. From a
        course's own range that's the course selected before it; otherwise the tee's read names it."""
        g = self.gspro
        folder = g.course if g.mode == "range" and g.own_range else None
        log.info("GSPro's log says %s, but the screen shows %r on the opening tee of %r (par %s): on a course "
                 "(%s)", g.mode if g.mode == "range" else "menus", hud.player_text, hud.course, hud.par,
                 folder or "course from the tee")
        g.enter_course(folder, None)

    def _check_log_round(self) -> None:
        """Act on round-level lines in GSPro's log: a round holed out ("Round Is Complete", with the
        holes it actually covered) or left ("End Round", which before completion means quit early)."""
        g, r = self.gspro, self.round
        while self._log_pins < len(g.pins):
            # The pin GSPro set the hole up with: which hole is worked out later, from the shots.
            try:
                self.store.record_logged_pin(g.pins[self._log_pins])
            except Exception as e:
                log.warning("Couldn't keep GSPro's logged pin (%s)", e)
            self._log_pins += 1
        if g.layouts != self._log_layouts:
            # The completed round's hole list: each played hole's tee and pin, for the hole replays.
            self._log_layouts = g.layouts
            # Under the round's course, as its shots are (a hole box read can be cut short), unless the
            # round has ended and this is another course's: one whose start the booth missed.
            read = self.current_course or g.course_display
            mine = r is not None and r.course and (not r.ended or not read or self._same_course(r.course, read))
            course = r.course if mine else read
            if (n := self.store.record_hole_layout(course, g.layout_holes)):
                log.info("Stored the layout of %d holes at %s", n, course)
                self._fill_log_totals()   # the tee shots: their start is the layout's tee
                self._name_tee_boxes()
        if not self._log_rounds:
            return
        live = r is not None and not r.ended
        if live and g.mode == "range" and not self._round_end_queued:
            # The range loaded with a round still open (it was left without the booth seeing it end).
            self._round_end_queued = True
            log.info("Round %s: GSPro is on the range; ending the round and cutting the booth off", r.id)
            self.speech.interrupt()
            self._events.put_nowait(RoundEndEvent(reason="GSPro went to the range", abandoned=True))
        if g.player_menus != self._log_menus:
            self._log_menus = g.player_menus
            if live and not self._round_end_queued:
                seq = r.seq if g.menu_hole is None else g.menu_hole + 2 - r.start_hole
                r.menus.append((seq, self._shot_seq))
                log.info("Round %s: GSPro's player menu was opened on %s: watching for a player added or removed",
                         r.id, r.hole_name(seq))
        if live:
            # (once End Round has reset the log, its start is the next round's)
            if g.round_ends == self._log_round_ends and not self._round_end_queued:
                self._apply_log_holes(r)
            if not self._round_end_queued:   # once it's ending, End Round's reset mustn't wipe the scores
                self._sync_log_scores(r)
                self._note_departures(r)
                self._squabbit_kick()
                self._note_log_scores(r)
        if g.round_completes != self._log_round_completes:
            self._log_round_completes = g.round_completes
            if live and not r.log_complete:
                r.log_complete = True
                log.info("Round %s: GSPro says the round is complete", r.id)
                if not self._round_end_queued:
                    self._note_departures(r)   # the last hole's finished too
        if live and r.log_complete and g.active_holes and g.active_holes != r.holes:
            log.info("Round %s: GSPro's layout has %s holes in the round (was counting %s)", r.id,
                     g.active_holes, r.holes)
            r.set_holes(g.active_holes, confirmed=True)
        if live and r.log_complete and not self._round_end_queued and r.complete():
            self._round_end_queued = True
            self._events.put_nowait(RoundEndEvent(reason="final scorecard"))
        if g.round_ends != self._log_round_ends:
            self._log_round_ends = g.round_ends
            if live:
                self._log_ended_round = r.id
            if live and not self._round_end_queued:
                self._round_end_queued = True
                if r.log_complete:
                    self._events.put_nowait(RoundEndEvent(reason="GSPro round finished"))
                else:
                    log.info("Round %s: GSPro's round was ended before it was complete (quit early)", r.id)
                    self._events.put_nowait(RoundEndEvent(reason="quit early in GSPro", abandoned=True))
            # Back to the menus: a fresh tee is a new round, and the setup watcher takes over.
            self._holes_played, self._holes_played_at = 0, 0.0

    def _log_scores_cover(self, r: Round | None) -> bool:
        """Whether GSPro's log has every player of the live round placed in its slot, so their scores are
        the log's as each hole finishes and the scorecard's OCR adds nothing: a log being followed, no more
        players in it than the round has, the first hole known, none of them in a slot the booth can't tie
        a name to, each (or their side, on a shared ball) with a logged score on every hole through the one
        just played, and every hole's par and stroke
        index already known (the card fills those in too, for handicap strokes)."""
        if r is None or r.ended or not self._fire_log_followed() or r.start_hole_assumed or not r.roster:
            return False
        if len(self.gspro.players_seen) > len(r.roster):
            return False   # someone the round doesn't have (the card admits them: _admit_from_card)
        holes = range(1, r.holes + 1)
        if any(s not in r.par or s not in r.index for s in holes):
            return False
        # A score on every hole through the one just played (the card's up between holes) for each side
        # sharing a ball (a scramble's is logged under one partner), else each player: one missed, the
        # card has it.
        groups = [s.gspro for s in r.sides] if r.format in TEAM_BALL else [[g] for g in r.roster]
        if any(not any(s in r.log_scores.get(g, {}) for g in grp) for grp in groups for s in range(1, r.seq + 1)):
            return False
        return all(self._gspro_idx(g, scores=True) is not None for g in r.roster)

    def _note_log_scores(self, r: Round) -> None:
        """A score GSPro's log has just posted on the hole in play: what the scorecard showing it would set
        off (the group's last-side wrap, the wrap played on the last putt), with no card read."""
        key = (r.id, r.seq)
        posted = {g: v for g in r.roster if (v := r.log_scores.get(g, {}).get(r.seq)) is not None}
        was = self._log_posted[1] if self._log_posted and self._log_posted[0] == key else {}
        self._log_posted = (key, posted)
        if any(was.get(g) != v for g, v in posted.items()):
            self._events.put_nowait(ScorecardEvent(card=None))

    def _sync_log_scores(self, r: Round) -> None:
        """Copy each finished hole's score from GSPro's log into the round, where it beats the scorecard
        read. Once End Round has reset the log state (and the booth hasn't acted on it yet), the ended
        round's scores are the ones kept from before the reset."""
        g = self.gspro
        self._recheck_slots(r, list(r.slot_names or []))   # the log may have shown a slot that unplaces someone
        if self._renames and r is self.round:
            self._rename_remapped(r)
        if r.start_hole_assumed:
            return
        # (also once acted on: the round can still be closing, a late slot correction re-syncing it)
        ended = g.round_ends != self._log_round_ends or self._log_ended_round == r.id
        scores = g.last_hole_scores if ended else g.hole_scores
        # Holes conceded on the tee: no stroke for the log's pick-up mark to settle on, so the slot says
        # who. Worked out afresh each time, so a slot order corrected, a rename or a replayed hole takes
        # back one given before.
        conceded = g.last_tee_concedes if ended else g.tee_concedes
        # (not by _gspro_idx's one-player shortcut when the round had more players than the live state
        # shows: some before the booth started, or End Round has cleared it)
        seen = g.last_players_seen if ended else g.players_seen | g.primed_seen
        trust = len(seen) <= 1 or bool(r.slot_names)
        tee = self._tee_concedes.setdefault(r.id, {})
        held, since_of, want = [], {}, {}
        for name in r.roster:
            idx = self._gspro_idx(name, scores=True)
            if idx is not None:   # the log's holes are 0-based
                since = r.slot_since.get(name, 1)   # the slot's earlier holes were someone else's
                # A concede's score another slot gave them (a slot order corrected since) wasn't theirs:
                # out first, so copying their slot doesn't take it for a replay and drop their card read.
                for (n, seq), at in tee.items():
                    if r.current_name(n) == name and at[0] != idx and seq >= since:
                        r.reopen(name, seq, card=False)
                r.set_log_scores(name, {hole + 2 - r.start_hole: v for (i, hole), v in scores.items() if i == idx})
                held.append((idx, since))
                since_of[name] = since
                for i, hole in conceded if trust else ():
                    if i == idx and (seq := hole + 2 - r.start_hole) >= since:
                        want[(name, seq)] = (i, hole)
        for (name, seq), at in list(tee.items()):
            now = r.current_name(name)
            del tee[(name, seq)]
            if (now, seq) in want or (not trust and at in conceded and at not in want.values()):
                tee[(now, seq)] = at   # (who holds the slot unknown now: only a replay takes it back)
                continue
            # Still theirs only from a slot they held before: they left the round after it, or moved up
            # a slot when someone ahead of them did.
            before = (now in r.left and seq <= r.left[now]) or (now in since_of and seq < since_of[now])
            if at not in conceded or not before or any(i == at[0] and since <= seq for i, since in held):
                for n in {name, now}:
                    self._drop_pickup(r, n, seq)
                    # The concede's score, not re-copied from a slot of theirs: replayed, it's no one's
                    # (the card's copy too, unless the log has their replay's); else someone else's.
                    stale = seq < since_of.get(n, seq + 1)   # (unplaced, or kept from a slot held before)
                    if at not in conceded and (stale or seq not in r.log_scores.get(n, {})):
                        r.reopen(n, seq)
                    elif stale:
                        r.reopen(n, seq, card=False)
            else:
                tee[(now, seq)] = at
        for key, at in want.items():
            if key not in r.picked_up:
                log.info("%s conceded hole %s of the round on the tee: scored at the maximum", *key)
            r.picked_up.add(key)
            tee[key] = at

    def _note_departures(self, r: Round) -> None:
        """Players who left partway (removed in GSPro): the holes GSPro last moved on from were finished
        by every slot but the last k (GSPro closes a removed player's slot up, so it's always the last that
        go quiet), by players who'd scored before them. The slots closed up on the first hole the last
        went quiet; who left are the k players the screen never named on a shot after it (fewer, or
        more: not yet). Only the player box counts: a name from GSPro's log came by the old slots.
        They stay in the round with their scores; nothing waits on them now. Two such holes, so a hole
        the log missed a score on isn't taken for a leaving, or one with GSPro's player menu opened on
        it or the hole before (the only sign GSPro gives of someone removed): then only shots since the
        menu count, so the leaver's own before it on that hole don't. Leavings on different holes not
        yet told apart wait."""
        done = r.seq if r.log_complete else r.seq - 1   # the last hole finished: GSPro's moved on, or it's over
        if r.ended or r.start_hole_assumed or r.slot_guess or not r.slot_names or None in r.slot_names \
                or r.format in TEAM_BALL or done < 2:
            return
        n = len(r.slot_names)
        scores = self._log_view()[0]

        def slots(seq: int) -> int | None:
            """How many slots finished hole `seq`, if it was the first so many (else None)."""
            got = {i for i, h in scores if h == seq + r.start_hole - 2}
            return len(got) if got == set(range(len(got))) else None
        m = slots(done)
        since = max(r.slot_since.get(g, 1) for g in r.slot_names)
        if r.arrivals and done > r.arrivals[0][2] + 1:
            log.info("Round %s: who %s took the place of isn't clear two holes on: not added",
                     r.id, ", ".join(repr(a[0]) for a in r.arrivals))
            r.arrivals = []
        arrived = r.arrivals
        if arrived:
            # Added in place of someone removed, on the hole they teed off on: the slots it finished are
            # the ones that stayed and the newcomers', theirs the last of them.
            first = arrived[0][2]
            if not m or first > done or first < max(2, since) \
                    or any(slots(seq) != m for seq in range(first, done + 1)) \
                    or sorted(a[3] for a in arrived) != list(range(m - len(arrived), m)):
                return
        else:
            if not m or m >= n:
                return
            first = done
            while first - 1 >= max(2, since) and slots(first - 1) == m:
                first -= 1
            if first < max(2, since):
                return
            if first - 1 >= max(2, since) and (slots(first - 1) or 0) > m and slots(first - 1) != n:
                return   # fewer slots before, but not all: more than one leaving, on different holes
        k = n + len(arrived) - m   # how many left
        if k < 1:
            return
        menu = next((at for seq, at in reversed(r.menus) if first - 1 <= seq <= first), None)

        def quiet_by(after: int | None) -> list[str]:
            """Who the screen named on no shot since the slots closed up: struck after the menu was
            opened (on its hole too), or else on the holes after it."""
            seen = set()
            for x in self.state.shots:
                if after is not None:
                    on = x.impact_seq is not None and x.impact_seq > after and first <= (x.hole_seq or 0) <= done
                else:
                    on = first < (x.hole_seq or 0) <= done
                if x.round_id == r.id and on and not x.mulligan and x.hud:
                    raw = _gspro_name(x.hud.player_text or "").casefold()
                    match = self.league.match_player(x.hud.player_text) if raw else None
                    seen |= {g for g in r.slot_names if raw and (g.casefold() == raw or (match and r.roster.get(g) == match))}
            return [g for g in r.slot_names if g not in seen]
        quiet = quiet_by(menu) if menu is not None else []
        if len(quiet) != k:
            if first == done or arrived:
                return   # one quiet hole, and nothing (yet) says who was removed: wait for the next
            quiet = quiet_by(None)
        if len(quiet) != k:
            return   # who left isn't clear (any of them would leave the last slots quiet): wait
        if first < max(r.slot_since.get(g, 1) for g in quiet + r.slot_names[n - k:]):
            return   # those holes were before they took the slot (joined, or back)
        if not all(any(seq < first for seq in r.log_scores.get(g, {})) for g in quiet):
            return   # never scored here: not someone who was playing and left
        old = list(r.slot_names)
        if arrived:
            # In first, so those they replaced can all leave (a round keeps someone playing): GSPro adds
            # each as the next slot, and the leavers' slots closing up puts them where GSPro has them.
            if any(g not in r.roster or g in r.left for g in quiet):
                return
            r.arrivals = []
            for name, match, _, _ in sorted(arrived, key=lambda a: a[3]):
                last = next((x for x in reversed(r.slot_names) if x), None)
                self._add_to_round(r, name, match, after=last, joined=first)
        gone = [g for g in quiet if r.leave_player(g, first - 1)]
        if not gone:
            return
        new = r.slot_names
        self.store.round_players(r.uuid, dict(r.roster), new)
        moved = {i for i in range(len(old)) if old[i] != (new[i] if i < len(new) else None)}
        for cache in (self._pending_finish, self._finish_seq, self._last_row):
            for k in [k for k in cache if k in moved]:
                del cache[k]
        # Shots since that the log named by the old slots are whoever moved up into them (renamed below);
        # a slot left empty is nobody's until someone's added to it.
        for fire, (_, rec, slot, _) in self._fire_rows.items():
            if rec.round_id == r.id and slot in moved and slot < len(new) and (rec.hole_seq or 0) >= first \
                    and rec.player != "unknown" \
                    and self._row_source.get(fire) not in ("screen", "setup read", "address frame"):
                self._renames.add(fire)
        log.info("Round %s: %s left after %s (GSPro played on without them; players now %s, GSPro order %s)",
                 r.id, ", ".join(map(repr, gone)), r.hole_name(first - 1), [x for x in r.roster if x not in r.left], new)
        self._sync_log_scores(r)   # those after them moved up a slot

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

    def _number_start_hole(self, r: Round, opening: bool = False, joined: bool = False) -> None:
        """The round's first hole when its banner was read without a number. OCR often misses a lone
        "1", and the count was reset at the last round's end, so the opening hole was labelled by par
        and yardage alone; once the start hole is known (setup screen or GSPro's log), number it and
        move anything already recorded there onto the numbered label. At the round's start (`opening`)
        a number already counted is replaced too: it was counted before the round, off menu banners or
        a round quit and restarted on the same tee. A round `joined` on its opening hole: its number is
        GSPro's log's, so only the label's missing; the tee shots struck before it was joined move too."""
        wrong = self.state.current_hole
        if r.ended or r.start_hole_assumed or not r.start_hole or r.seq != 1 or not wrong:
            return
        if joined and re.match(r"Hole \d+", wrong):
            return
        if not joined and self.hole_number is not None and not (opening and self.hole_number != r.start_hole):
            return
        if opening and self.hole_number is not None:
            log.info("Round %s: the hole counted before it started (%s) isn't its start hole %s",
                     r.id, self.hole_number, r.start_hole)
        self.hole_number, self.hole_number_read = r.start_hole, True
        label = f"Hole {r.start_hole}, {re.sub(r'^Hole \d+, ', '', wrong)}"
        if label == wrong:
            return
        self.state.current_hole = label
        # (and a tee shot struck just before the round was picked up, not yet anyone's)
        moved = [x for x in self.state.shots if x.round_id in (r.id, None) and x.hole == wrong]
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
        self._shots_unrecorded[id(event)] = event.shot.timestamp
        self._chat_glance_on_shot()
        try:
            await self.handle_shot(event)
        finally:
            self._shots_unrecorded.pop(id(event), None)
        # Only a shot that ran to its reaction, and is still the latest: an older handler finishing (early,
        # or late) must not start an aside under a newer shot's call, even one that set the moment already.
        m = self._news_moment
        if not (m and event.seq is not None and m["seq"] == event.seq == self._shot_seq):
            return
        if self._asides_on() or self._chat_ready is not None or self._chat_writing():
            self._bg(self._after_shot(m if self._asides_on() else None, m["seq"]))

    async def _after_shot(self, moment: dict | None, seq: int) -> None:
        """Between this shot and the next: a prepared aside if one suits the moment, else a viewer's
        message. Never both, so the two don't talk over each other."""
        if moment is not None and await self._news_after_shot(moment):
            return
        await self._chat_after_shot(seq)

    def _on_shot_task_done(self, task: asyncio.Task) -> None:
        self._shot_tasks.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:
            log.error("Shot handler crashed, reaction dropped: %r", exc, exc_info=exc)
