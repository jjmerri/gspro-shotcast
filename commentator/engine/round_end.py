"""The scorecard, the end of a hole and of the round, and the sign-off.

Part of Engine (commentator/engine/core.py), which is these parts together."""
import asyncio
import sqlite3
import time

from .common import log
from .. import perf_log
from ..models import RoundEndEvent, ScorecardEvent
from ..prompts import closer_prompt
from ..round import Round
from ..store import COUNTS_AS_ROUND_HOLES
from ..usage import audio_summary


class RoundEndMixin:
    """The scorecard, the end of a hole and of the round, and the sign-off."""

    async def handle_scorecard(self, event: ScorecardEvent) -> None:
        r = self.round
        if event.card is None:   # GSPro's log posted a score: the card's numbers are already in
            if r and not r.ended:
                await self._after_hole_scores(r)
            return
        if not r or r.ended:
            if event.card.scores != self._idle_card_logged:   # the card stays up after a round: once per change
                self._idle_card_logged = event.card.scores
                log.info("Scorecard read with no round in progress: %s", event.card.scores)
            self._note_idle_card(event.card)
            return
        if not r.apply_scorecard(event.card):
            log.info("Scorecard didn't match the round's players (%s): rows %s", list(r.roster), list(event.card.scores))
            return
        if self._admit_from_card(r, event.card):
            r.apply_scorecard(event.card)
        log.info("Round %s scorecard: %s", r.id, {g: dict(sorted(v.items())) for g, v in r.card_scores.items()})
        self._squabbit_kick()
        await self._after_hole_scores(r)
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

    async def _after_hole_scores(self, r: Round) -> None:
        """A score posted on the hole in play, read off the scorecard or GSPro's log (_note_log_scores)."""
        if self.cfg.commentary.segments_on_hole_change and self.state.current_hole \
                and self._wrapped_hole != self.state.current_hole:
            # In a group, the moment the second-to-last side holes out (one side left to putt), pre-
            # build the wrap branched on that last side's finish, with the others' scores fixed. Solo
            # builds earlier, at green-reach, so this is multiplayer-only.
            if len(r.sides) > 1 and self.current_par and not (
                self._wrap_branches and self._wrap_branches.get("hole") == self.state.current_hole
            ):
                remaining = [s for s in r.sides if r.card_score(s, r.seq) is None and r.plays(s, r.seq)]
                if len(remaining) == 1:
                    par = self.current_par
                    done = [s for s in r.sides if r.card_score(s, r.seq) is not None and r.plays(s, r.seq)]
                    others = [self._score_fact(s, r.card_score(s, r.seq), par) for s in done]
                    self._kick_wrap_branches(remaining[0].key, [par - 1, par, par + 1, par + 2], others,
                                             {s.key: r.card_score(s, r.seq) for s in done})
            # The finished hole's score posts here, ~right after the last putt and well before GSPro's
            # hole change. If the wrap is pre-voiced, play it now so it lands on the green instead of
            # after the between-hole screen; the hole change then sees it's done and skips.
            await self._wrap_after_last_putt(self.state.current_hole, r.seq)

    async def _wrap_after_last_putt(self, finished_hole, finished_seq: int) -> bool:
        """Fire the pre-made wrap the moment the scorecard shows the hole's score. Lets the hole's
        last reaction queue first (bounded) so the wrap doesn't jump ahead of it."""
        if not self._wrap_branches or self._wrap_branches.get("hole") != finished_hole:
            return False
        await self._drain_shot_tasks(self.cfg.commentary.wrap_wait_seconds)
        await self._drain_speech(self.cfg.commentary.wrap_wait_seconds + 10)
        await self._wait_out_held_banter()
        # The hole change tries again, unless this was the round's last hole (the closer comes next).
        r = self.round
        return self._play_prepared_wrap(finished_hole, finished_seq, last_chance=bool(r and r.complete()))

    def _record_round_stats(self, r: Round, reason: str, complete: bool = True) -> dict:
        """Log and store each side's (and individual's) round. A round quit early is stored as
        incomplete, with net to par over just the holes played."""
        self._note_tournament(r)
        self._apply_log_corrections()
        if not complete:
            # A side with no score on any hole (a misread name that became a phantom player) can't
            # say whether the current hole was finished; only the sides actually playing count.
            playing = [s for s in r.sides if r.plays(s, r.seq)
                       and any(r.card_score(s, q) is not None for q in range(1, r.seq + 1))]
            done = bool(playing) and all(r.card_score(side, r.seq) is not None for side in playing)
            r.stats_through = max(0, r.seq if done else r.seq - 1)
            if not r.stats_through:
                log.info("Round %s quit before a hole was finished: nothing to record", r.id)
                self.store.end_round(r.uuid, complete=False)
                return {}
        stats = {s.key: r.side_stats(s, self.state.shots) for s in r.sides}
        # A finished round can still hold a side that didn't play all of it (joined late, or left
        # early): that side is stored as part-played, so a seven-hole card isn't a season round.
        whole = {s.key: complete and stats[s.key].holes_scored >= r.holes for s in r.sides}
        net = {s.key: (r.net_to_par(s, stats[s.key]) if whole[s.key] else r.net_to_par_played(s, stats[s.key]))
               for s in r.sides}
        for side in r.sides:
            st = stats[side.key]
            log.info("Round %s over (%s): %s gross %s, strokes %s, net to par %s, FIR %s/%s, GIR %s/%s, putts %s "
                     "over %s holes, penalties %s, scores %s (from %s)%s", r.id, reason, side.label, st.gross,
                     side.strokes, net[side.key], *st.fir, *st.gir, st.putts, st.putt_holes, st.penalties,
                     st.scores, st.source, "" if whole[side.key] else f"; incomplete, {st.holes_scored} of {r.holes} holes")
        row_of: dict[str, int] = {}   # each team's rounds row, for its week result
        stored: list[int] = []   # every side's row (teammates on their own balls share a row_of key)
        for side in r.sides:
            st = stats[side.key]
            if st.holes_scored:
                # A one-person scramble team is an individual round, filed under the player.
                row_of[side.team or side.key] = row = self.store.record_round(course=r.course, fmt=r.side_format(side),
                                        side=side.players[0] if r.solo(side) else side.key, team=side.team,
                                        holes=r.holes, players=side.players,
                                        stats=st, strokes=side.strokes, net_to_par=net[side.key],
                                        hole_number=r.hole_number, hole_pars=r.par, is_league=r.is_league,
                                        is_tournament=r.is_tournament, complete=whole[side.key],
                                        first_nine=r.first_nine(st, r.hole_strokes_for(side)), physics=r.physics,
                                        record=r.uuid)
                stored.append(row)
        r.stored_rows = stored
        if complete:
            self._record_week_results(r, row_of)
        # Per-player FIR/GIR/scrambling/sand-save for individual season stats. Alternate shot isn't
        # attributed to one player, so skip it (but not a player alone on their own ball in it); otherwise
        # record each individual (distinct names from this round's own shots, so a two-ball team isn't
        # double-counted), skipping any shared-ball (anonymous) player. Scrambling/sand are gated to
        # individual-ball formats at query time.
        anon = set(r.anonymous_players())
        names = {rec.player for rec in self.state.shots if rec.round_id == r.id
                 and rec.player and rec.player != "unknown" and rec.player not in anon
                 and r.player_format(rec.player) != "alternate"}
        for name in names:
            pst = r.player_stats(name, self.state.shots)
            if pst.holes_scored:
                nine = r.first_nine(pst)
                self.store.record_player_round(player=name, course=r.course, fmt=r.player_format(name), stats=pst,
                                               hole_number=r.hole_number, hole_pars=r.par, is_league=r.is_league,
                                               is_tournament=r.is_tournament,
                                               complete=complete and pst.holes_scored >= r.holes,
                                               nine_to_par=nine[0] if nine else None,
                                               record=r.uuid)
        self.store.round_players(r.uuid, dict(r.roster), r.slot_names)   # its players and seats as they ended
        self.store.end_round(r.uuid, complete=complete)
        self._refresh_own_handicaps()
        return stats

    async def handle_round_end(self, event: RoundEndEvent) -> None:
        self._round_handling += 1   # in a round for the round status until the sign-off is written
        try:
            await self._handle_round_end(event)
        finally:
            self._round_handling -= 1

    async def _handle_round_end(self, event: RoundEndEvent) -> None:
        r = self.round
        if not r or r.ended:
            return
        # The last shot's handler may still be reading: its record goes into the round's totals. (One
        # waiting on GSPro's round file for a stroke it won't write stops at `closing`: _file_wait_over.)
        r.closing = True
        try:
            await self._drain_shot_tasks(self.cfg.commentary.wrap_wait_seconds)
            if r.ended:
                return
            self._apply_log_corrections()   # whose the last unnamed shots were, while the round still maps them
            self._log_name_summary(r)
            self._log_lie_summary(r)
            r.ended = True
        finally:
            r.closing = r.ended and r.closing   # (a close that failed: the round goes on)
        self._squabbit_round_end(r)
        if self._log_truth and self.cfg.source.type != "replay":
            self._bg(asyncio.to_thread(self._keep_gspro_log))
        if self.cfg.source.type != "replay":
            # From the record: a round continued after a booth restart started, and has strokes, from before it.
            try:
                started, speeds = self.store.round_start_and_speeds(r.uuid)
                courses, ids = self.store.courses_played(), self.store.round_gspro_shots(r.uuid)
            except sqlite3.Error:
                log.exception("Reading round %s's record for its round file failed", r.id)
                started, speeds, courses, ids = None, [], [], []
            started = started or time.time() - (time.monotonic() - self._round_started_at)
            self._bg(asyncio.to_thread(self._keep_round_file, r, started, self._round_file_names(r), speeds,
                                       courses, ids, self._round_course_folder(r)))
        self._round_ended_at = time.monotonic()
        perf_log.mark("round_end", round=r.id)
        # Asides can no longer play, so prep still in flight is money spent on nothing. The guards in
        # _prepare_news stop it starting another story; only a cancel stops the one already running
        # from retrying its lines and voicing them.
        for task in list(self._aside_tasks):
            task.cancel()
        self._shots_at_round_end = self.gspro.fires
        self._courses_at_round_end = self._round_course_entries
        if r.voice_from_seq is not None:   # the holes voiced since the voice was picked
            self.speech.end_round(r.seq - r.voice_from_seq + 1, len(r.roster))
        self.hole_number = None  # the next round starts its own count
        if self.cfg.health.enabled and self.cfg.source.type != "replay":
            try:
                self._health_round_end(r)
            except Exception:
                log.exception("Post-round health checkup failed")
        if event.abandoned:
            # Quit partway: the wrap prepared for a finished round is dropped. The holes
            # played are still recorded (marked incomplete): their per-hole stats count, and a full
            # nine or more counts as a round in the season totals, scored per nine. A quit that far in
            # earns a sign-off over the holes actually played; a shorter one is left alone.
            self._wrap_branches = None
            if self._league_after_quit(r):
                stats = self._record_round_stats(r, event.reason)
            else:
                stats = self._record_round_stats(r, event.reason, complete=False)
            self._fill_course_layouts(since=time.time() - 12 * 3600)
            mark = self._hole_speech_mark
            if mark and mark[:2] == (r.id, r.seq) \
                    and not any(x.round_id == r.id and x.hole_seq == r.seq for x in self.state.shots):
                # Quit on the tee of a hole nobody played: its intro and segment lines, still waiting
                # to air, would talk up a hole that never happens. The last hole's wrap still airs.
                log.info("Round %s quit on unplayed hole %s: dropping its lines still waiting to air", r.id, r.seq)
                self.speech.drop_since(mark[2])
            played = max((st.holes_scored for st in stats.values()), default=0)
            if played < COUNTS_AS_ROUND_HOLES:
                self._schedule_publish(after_speech=True)
                self._schedule_backup("round end")
                self._schedule_live_stop()
                log.info("Round %s over (%s) after %s of %s holes: no closer", r.id, event.reason, r.seq, r.holes)
                return
            log.info("Round %s over (%s) after %s of %s holes: signing off on the %s played",
                     r.id, event.reason, r.seq, r.holes, played)
            self._schedule_publish()   # the backup, if the sign-off never finishes
            try:
                await self._sign_off(r, stats)
                self._schedule_publish(after_speech=True)
            finally:
                self._schedule_backup("round end")   # the round is stored even if the closer fails
                self._schedule_live_stop()
            return
        if stats_obj := getattr(self.speech, "audio_stats", None):
            log.info("%s", audio_summary(stats_obj))
        stats = self._record_round_stats(r, event.reason)
        self._bg(self._fill_course_layouts_later())
        self._schedule_publish()   # the backup, if the sign-off never finishes
        try:
            await self._sign_off(r, stats)
            self._schedule_publish(after_speech=True)
        finally:
            self._schedule_backup("round end")   # the round is stored even if the closer fails
            self._schedule_live_stop()

    async def _sign_off(self, r: Round, stats: dict) -> None:
        # Measured against every earlier round, earlier today's included (a second round of the day
        # isn't a season best when the first beat it), leaving out only this round's own rows.
        season = {s.key: self.store.season_rounds(s.key, "league" if r.is_league else "practice",
                                                  skip_rows=r.stored_rows) for s in r.sides}
        facts = (r.closing_facts(self.state.shots, season, week_ctp=self._closer_week_ctp(r))
                 + self._league_facts(r, closing=True))
        if not self.cfg.commentary.closer:
            return
        # One call, written now that the final score is known, while the last hole's wrap airs: it
        # used to be pre-built on the last hole in three or four score branches, one of which aired.
        cap = self.cfg.commentary.closer_max_words
        if standing := next((f for q in range(r.holes, 0, -1) if (f := r.standing_fact(q))), None):
            facts = facts + [standing]
        user, facts_text = closer_prompt(facts=facts + self._weather_facts() + self._call_facts(whole_round=True),
                                         recent_lines=self.state.recent_lines,
                                         max_words=cap)
        # r.ended is set above, so the `system` property would pick practice; pass the round's own
        # variant so a league round's sign-off keeps its stakes framing.
        await self._speak("closer", self.closer_llm, user, facts_text, [], mode="round",
                          system=self._system_of(r), max_words=cap)
