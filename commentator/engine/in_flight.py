"""Calls while the ball is in the air, and the change to the next hole.

Part of Engine (commentator/engine/core.py), which is these parts together."""
import asyncio
import re
import time

from .common import TREE_CALL_PENALTY, TREE_CALL_WATCH_SECONDS, log
from ..facts import flight_facts
from ..flight import point_at
from ..hud import Hud
from ..models import HoleChangeEvent, Line, RoundEndEvent, Shot
from ..prompts import flight_prompt, hole_intro_prompt, segment_prompt
from ..tree_calls import TREE_CALLS_BY_LANGUAGE


class InFlightMixin:
    """Calls while the ball is in the air, and the change to the next hole."""

    def _tree_hit_logged(self, fires0: int, impact: float, shot: Shot) -> tuple[float, tuple] | None:
        """The tree hit worth a flight call that GSPro's log already has for this shot: when it
        happens (monotonic) and the stock lines to call it with. None if there's none (yet)."""
        fire = self.gspro.fire_of(fires0, impact)
        info = self.gspro.fire_info(fire)
        hits = info.get("leaves") or []
        if hits and hits[-1][3] >= TREE_CALL_PENALTY:
            t = hits[0][0]
            p = point_at(shot.ball_speed, shot.vla, shot.total_spin, shot.spin_axis, self._raw_yards(shot), t)
            low = info.get("bush") or (p is not None and (p.landed or p.height_ft < 10))
            high, low_calls = TREE_CALLS_BY_LANGUAGE[self.cfg.commentary.language]
            return impact + t, low_calls if low else high
        return None

    async def _tree_flight_call(self, fires0: int, impact: float, shot: Shot, seq: int, tag: str) -> None:
        """Call a tree hit while the ball is in the air. GSPro works the flight out at launch and logs
        each foliage hit among the launch lines (within ~0.3 s of impact) with its time into the
        flight, so the call is timed to the moment the ball reaches the branches. A trunk (wood, no
        leaves) isn't called here: only GSPro's carry, read once the ball stops, tells it from a bridge."""
        deadline = impact + TREE_CALL_WATCH_SECONDS
        found = None
        while self._shot_seq == seq:   # checked at least once, even when started late
            if (found := self._tree_hit_logged(fires0, impact, shot)) or time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.1)
        if found is None or self._shot_seq != seq or self._practice_or_unseen():
            return
        hit_at, choices = found
        self._tree_called = seq
        line = Line(speaker=self.speakers[0], text=self.rng.choice(choices))
        if not self._prevoicing():
            # Voiced as it plays: start it early enough to land near the hit, and never past it by 2 s.
            await asyncio.sleep(max(0.0, hit_at - 0.8 - time.monotonic()))
            if self._shot_seq != seq or self._banter_held() or self._practice_or_unseen():
                return
            self.state.record_lines([line])
            self.speech.say([line], interrupt=True, deadline=hit_at + 2.0)
            self._write_transcript([line], "flight", impact_seq=seq)
            log.info("%s tree call in flight, %.2fs after impact (the hit at %.2fs): %r", tag,
                     time.monotonic() - impact, hit_at - impact, line.text)
            return
        kept, audio = await self._voice_ahead([line])
        # Land the word a beat before the ball reaches the branches; skip it once that's long gone.
        await asyncio.sleep(max(0.0, hit_at - 0.2 - time.monotonic()))
        if self._shot_seq != seq or time.monotonic() > hit_at + 2.0 or self._banter_held() or self._practice_or_unseen():
            return
        self.state.record_lines(kept)
        self._air_exchange(list(zip(kept, audio)))
        self._write_transcript(kept, "flight", impact_seq=seq)
        log.info("%s tree call in flight, %.2fs after impact (the hit at %.2fs): %r", tag,
                 time.monotonic() - impact, hit_at - impact, kept[0].text if kept else "")

    async def _in_flight(
        self, shot, before: Hud, player: str | None, image: bytes | None, impact: float, factor: float,
        seq: int | None = None, fires0: int | None = None, stymie: bool = False,
    ) -> None:
        deadline = self.cfg.commentary.flight_deadline_seconds
        images = [image] if image and self.flight_llm.cfg.vision else []
        who, team = self._who(player)
        long_tee = (before.lie or "").lower() == "tee" and (self.current_par or 0) >= 4
        aim = None if fires0 is None or long_tee else self.gspro.aim_of(fires0, impact)   # (a long hole's map stops short)
        user, facts_text = flight_prompt(
            player=who, team=team, hole=self.state.current_hole,
            facts=flight_facts(shot, before.to_pin, factor, before.lie, before.elevation_yards, aim),
            recent_lines=self.state.recent_lines,
            speaker=self.speakers[0], has_image=bool(images), off_flag=self._off_flag(player, before.lie, before.to_pin, before.elevation_yards, aim, stymie),
        )
        budget = max(deadline - (time.monotonic() - impact), 0.1)
        try:
            lines = await asyncio.wait_for(
                self._generate("flight", self.flight_llm, user, images), timeout=budget)
        except asyncio.TimeoutError:
            self.stats["flight_late"] += 1
            log.info("Flight call missed the %.1fs deadline (%.1fs of it left when the call went out)",
                     deadline, budget)
            return
        elapsed = time.monotonic() - impact
        self.flight_latencies.append(elapsed)
        # No retry: there isn't time. A bad line is simply not said.
        good, _ = self._grounded(lines[:1], facts_text, mode="flight", seen_image=bool(images))
        if not good or (seq is not None and self._shot_seq != seq):   # another ball's in the air now
            return
        if seq is not None and self._tree_called == seq or \
                fires0 is not None and self._tree_hit_logged(fires0, impact, shot):   # the tree call has this shot
            return
        line = Line(speaker=self.speakers[0], text=good[0].text)
        self.state.record_lines([line])
        self.speech.say([line], interrupt=True, deadline=impact + self.cfg.tts.flight_audio_deadline_seconds)
        self._write_transcript([line], "flight", impact_seq=seq)

    async def handle_hole_change(self, event: HoleChangeEvent) -> None:
        if event.correction:
            await self._correct_hole(event)
            return
        previous = self.state.current_hole
        previous_course = self.current_course
        self.state.current_hole = event.current
        self._last_hud_shot = {}  # stroke counts start over on a new hole
        self._setup_screen_lies.clear()
        self._setup_lies.clear()   # a stroke's key (who, shot, distance) can recur on another hole
        self._setup_log_lies.clear()
        self._setup_slopes.clear()
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
            first_done = r.seq == 1
            r.seq += 1
            if self.hole_number and not r.start_hole_assumed:
                r.seq = self.hole_number - r.start_hole + 1  # a read hole number beats counting changes
            if self.current_par:
                r.par.setdefault(r.seq, self.current_par)
            log.info("Round %s: hole %s of %s", r.id, r.seq, r.holes)
            self._relabel_refiled(r, event.current)
            self._squabbit_kick()   # a round set up for more than the week's holes is asked on its first
            if first_done and r.seq == 2:
                self._drop_no_shows(r)
            self._score_hole_started(r)
            self._maybe_prep_asides(r)
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
            await self._wait_out_held_banter()
        if self.round and self._play_prepared_wrap(previous, self.round.seq - 1):
            self._wrapped_hole = None
            await self._tee_up(event.current)  # pre-made recap is recap-only; introduce the new hole
            return
        user, facts_text = segment_prompt(
            previous_hole=previous,
            current_hole=event.current,
            recap=self.state.hole_recap(previous, name=lambda p: self._display(p) or p),
            recent_lines=self.state.recent_lines,
            result=self._hole_score_facts(previous) + self._wrap_tally_facts() + self._roster_facts()
            + self._weather_facts(self.cfg.weather.between_holes_rate),
        )
        await self._speak("segment", self.segment_llm, user, facts_text + "\n" + "\n".join(self._format_facts()), [])

    async def _tee_up(self, current_hole: str) -> None:
        """Introduce the hole that's starting. Used after a pre-made recap, which is recap-only; the
        on-demand wrap already tees up the next hole itself."""
        if (r := self.round) and not r.ended:
            # The last hole's wrap is queued already: what's queued from here is this hole's, and a
            # quit on its tee drops it.
            self._hole_speech_mark = (r.id, r.seq, self.speech.exchange_mark())
        user, facts_text = hole_intro_prompt(
            hole=current_hole, recent_lines=self.state.recent_lines,
            extra=self._roster_facts() + self._weather_facts(self.cfg.weather.between_holes_rate))
        await self._speak("segment", self.segment_llm, user, facts_text, [], mode="round")
