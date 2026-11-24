"""The shot: reading the screen at impact and handling the shot itself.

Part of Engine (commentator/engine/core.py), which is these parts together."""
import asyncio
import contextlib
import math
import sqlite3
import time
from dataclasses import replace

from .common import (
    BANTER_WAIT_SECONDS, FLIGHT_CALL_LATE_SECONDS, SHORT_GAME_YARDS, _CUP_FACTS, _conceded_hole_fact, _feet_text,
    _great_shot_fact, _hole_key, _short_miss_fact, _tile_yards, file_putt_outcome, log,
)
from .. import carry_estimate, club_fields, round_file
from ..facts import (
    SCORE_NAMES, WRONG_GREEN_YARDS, club_delivery_facts, club_delivery_notable, club_name, gimme_edge,
    green_chip_fact, history_facts, is_chip, lie_cost_fact, moment_facts, shot_facts, to_yards, wrong_green_fact,
    wrong_green_yards,
)
from ..flight_pick import aim_turn, pick_line, pick_reaction, project
from ..hud import Hud, deck_club, describe
from ..interest import (
    LEAGUE_BEST_DRIVE_LEVEL, SEASON_BEST_DRIVE_LEVEL, approach_gimme_level, holed_out_level, holed_putt_score,
    putt_to_par, score_shot, short_game_gimme_level, stuffed_level,
)
from ..models import Line, ShotEvent
from ..prompts import ANGLES, PRACTICE_ANGLES, shot_prompt
from ..state import ShotRecord
from ..store import is_drive
from ..sources.gspro_log import MATERIAL_TO_LIE, M_TO_YD


class ShotMixin:
    """The shot: reading the screen at impact and handling the shot itself."""

    def _impact_state(self) -> tuple:
        # Snapshot this player's per-index log state at impact, BEFORE the wait for the ball at rest advances the log to
        # the next shot's setup. Used to resolve the previous shot's finishing distance (this shot's
        # setup distance is where the previous ball came to rest) and to baseline this putt's outcome.
        gs = self.gspro
        impact_snap = (dict(gs.to_pin_by), dict(gs.hole_by), dict(gs.hole_outs_by),
                       dict(gs.gimmies_by), dict(gs._penalties), gs.current_hole,
                       {i: gs.shot_number(i) for i in range(8)},  # stroke count AT IMPACT, before the
                       #                                            slow read, so a putt-miss shows the
                       #                                            instant the next state line advances it
                       dict(gs.dropped_by),   # scramble balls not picked, to leave their finish alone
                       gs.added_count,        # penalties the booth had added by then
                       (gs.pick, gs.pick_stroke()),   # the scramble's pick: the team ball being played
                       self._finished_holes())   # (player, hole)s finished by then
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
        # once the ball is struck). The club box: with local-CPU TTS (Kokoro) it's skipped (no club
        # then), to keep OCR off the synth's back; with cloud audio nothing contends for the cores.
        regions = ["player_name", "player_box", "lie", "tiles", "tiles_left"]
        if self.cfg.tts.provider != "kokoro":
            regions.append("club")
        # Every box grabbed at once, before any OCR: one by one, the club box (gone soon after the
        # swing) was grabbed seconds late on a slow read, and the lie could be the next player's.
        before, impact_image = await asyncio.gather(
            self.screen.read_hud_async(regions, at_once=True) if ocr else asyncio.sleep(0, Hud()),
            self.screen.snapshot_jpeg() if wants_image else asyncio.sleep(0, None),
        )
        return before, impact_image

    def _start_hud_buffer(self) -> None:
        sc = self.cfg.screen
        if (not self.screen or not sc.ocr or not sc.impact_buffer or sc.capture != "monitor"
                or self.cfg.source.type == "replay" or not callable(getattr(self.screen, "grab_boxes", None))):
            return
        from ..screen import HudBuffer

        self._hud_buffer = HudBuffer(self.screen)
        self._hud_buffer.start(lambda: self.round is not None and not self.round.ended)

    def _keep_buffer_frames(self, event: ShotEvent, at: float) -> None:
        """Keep the HUD buffer's frames from before this shot (and after the last one) with it, at
        impact or as it arrives: the buffer moves on (the next player's box, frames aged out) while the
        shot waits behind other work or for its ball to stop."""
        if self._hud_buffer is None or event.buffer_frames is not None:
            return
        since, self._last_impact_at = self._last_impact_at, max(at, self._last_impact_at or at)
        event.buffer_frames = self._hud_buffer.before(at, since)

    async def _fill_from_buffer(self, before: Hud, frames: list | None, tag: str = "") -> bool:
        """The impact read missed the player box and there's no setup read for this stroke (or it missed
        the club box): the newest address frame from the HUD buffer that shows them. True when it named
        the shot."""
        if self._hud_buffer is None:
            return False
        named = bool(before.player_text and before.shot_number is not None)
        if named and (before.club or self.cfg.tts.provider == "kokoro"):
            return False
        if not frames:
            return False
        started = time.monotonic()
        hud = await asyncio.to_thread(self.screen.read_buffered, frames,
                                      lambda h: self._player(h.player_text) is not None, want_club=named)
        if hud is None:
            if not named:
                log.info("%s no address frame in the HUD buffer shows the player box (%d frames)", tag, len(frames))
            return False
        if named:
            if hud.club and self._player(hud.player_text) == self._player(before.player_text):
                log.info("%s club %s from an address frame (the impact read missed the club box)", tag, hud.club)
                before.club = hud.club
            return False
        log.info("%s the impact read missed the player box%s: %r shot %s, club %s, lie %s from an address frame "
                 "(%.2fs)", tag, f" (read {before.player_text!r})" if before.player_text else "", hud.player_text,
                 hud.shot_number, hud.club, hud.lie, time.monotonic() - started)
        before.player_text, before.shot_number = hud.player_text, hud.shot_number
        before.to_pin, before.score_to_par = hud.to_pin, hud.score_to_par
        if before.elevation_yards is None:
            before.elevation_yards = hud.elevation_yards
        before.lie = before.lie or hud.lie
        before.club = before.club or hud.club
        before.handed = before.handed or hud.handed
        return True

    def _impact_hole(self) -> tuple:
        """(hole, par, round, the round's hole number) the shot was struck on: a hole change while
        its handler is still reading mustn't move it to the next hole."""
        r = self.round if self.round and not self.round.ended else None
        return self.state.current_hole, self.current_par, r, r.seq if r else None

    def _capture_impact(self, event: ShotEvent) -> None:
        self._struck_at = time.time()   # even a swing whose ball data never comes is someone playing
        event.impact_at = time.monotonic()
        event.impact_state = (self._impact_state(), *self._impact_hole())
        event.setup_hud = self._setup_at_impact(event.impact_at)
        self._keep_buffer_frames(event, event.impact_at)
        # The ball's in the air now, though the shot comes later: the setup's moment and its pending
        # lines are past (handle_shot reuses this count).
        self._cancel_preshot()
        self._shot_seq += 1
        event.impact_seq = self._shot_seq
        if self.speech is not None:
            self.speech.drop_queued()
        # Read the screen now, while it still shows this shot's player, lie and tiles.
        if self.screen is not None and event.hud_hint is None:
            event.impact_read = asyncio.ensure_future(self._read_impact_hud("[held shot]"))

    async def handle_shot(self, event: ShotEvent) -> None:
        self._struck_at = time.time()
        if self._on_range() and not await self._course_shot_off_own_range(event):
            log.info("Range shot ignored (club %s, carry %.1f)", event.shot.club, event.shot.carry or 0)
            return
        # A shot the feed held back (GSPconnect skipped it) carries its impact time and state.
        impact = event.impact_at or time.monotonic()
        from_buffer = False
        held = event.impact_state
        impact_snap, fires0, setups0, lies0 = held[0] if held else self._impact_state()
        # The reaction runs in the background and may finish after the hole has changed; pin the
        # hole and par to this shot so a late reaction still talks about the right hole.
        shot_hole, shot_par, shot_round, round_hole = held[1:] if held else self._impact_hole()
        if self._practice and self.screen is not None and self.cfg.screen.ocr and event.hud_hint is None:
            # This swing's own impact read has the last word: a round's player box ends practice. Taken
            # after the impact state above (a slow read mustn't age it), and kept on the event, so the
            # read further on is this one.
            if getattr(event, "impact_read", None) is None:
                event.impact_read = asyncio.ensure_future(self._read_impact_hud("[practice check]"))
            hud0 = (await event.impact_read)[0]
            self._note_practice(hud0.player_text, hud0.shot_number)
        if self._practice:
            log.info("Practice-mode shot ignored (club %s, carry %.1f)", event.shot.club, event.shot.carry or 0)
            return
        if not held and not await self._gspro_played(fires0, impact):
            log.info("Shot ignored: GSPro never played it (struck in the menus or while a course loads; "
                     "club %s, ball %.1f mph, carry %.1f)", event.shot.club, event.shot.ball_speed or 0,
                     event.shot.carry or 0)
            return
        self._last_activity = time.monotonic()
        self._live_shot()
        # Counted at impact (_capture_impact) if the feed held it; superseded if a ball was struck since,
        # and then it mustn't cancel or clear that newer shot's work.
        counted = held is not None and event.impact_seq is not None
        superseded = counted and event.impact_seq != self._shot_seq
        if not superseded:
            self._cancel_preshot()
        self._shot_id = None  # set once this shot is recorded; mid-flight lines come before that
        self._apply_log_corrections()
        shot = event.shot
        if not counted:
            if event.setup_hud is None:   # (a shot held on the range kept its own)
                event.setup_hud = self._setup_at_impact(event.received_at or impact)
            self._shot_seq += 1
        my_seq = event.impact_seq if counted else self._shot_seq   # this shot's order, to keep concurrent handlers from reordering finish
        event.seq = my_seq
        tag = f"[shot {my_seq}]"
        if self.speech is not None and not superseded:
            self.speech.drop_queued()   # the ball's in the air: the last shot's calls are old news
        banter = self._banter_on()   # an aside plays on: no mid-flight calls over it
        if banter:
            log.info("%s an aside is on air: no mid-flight call", tag)
        landed_already = event.landed or (held is not None
                                          and time.monotonic() - impact > FLIGHT_CALL_LATE_SECONDS)
        if landed_already:
            log.info("%s arrived %.1fs after impact%s: the ball is down, no mid-flight call", tag,
                     time.monotonic() - impact, " (from GSPro's shot files)" if event.landed else "")
        lag = f", {time.time() - event.logged_at:.2f}s after GSPconnect logged it" if event.logged_at else ""
        log.info("%s detected%s: club %s, ball %.1f mph, launch %s, start %.1f, spin %s @ axis %.1f, carry %.1f %s, putt=%s",
                 tag, lag, shot.club, shot.ball_speed or 0,
                 "none" if shot.vla is None else f"{shot.vla:.1f}", shot.hla or 0,
                 round(shot.total_spin) if shot.total_spin else None, shot.spin_axis or 0, shot.carry or 0,
                 shot.units, shot.is_putt)
        use_screen = self.screen is not None and event.hud_hint is None
        ocr = use_screen and self.cfg.screen.ocr
        if ocr and not shot.is_putt and my_seq == self._shot_seq:
            self._tiles_wanted = my_seq
        # A monitor that leaves the carry to GSPro (springbok's connector, other Open Connect bridges):
        # GSPro's no-penalty carry estimated from the ball numbers, for the mid-flight call only.
        estimated = None if shot.carry or shot.is_putt else carry_estimate.estimate(shot)
        flight_carry = shot.carry or estimated or 0
        if estimated:
            log.info("%s no carry from the monitor: %.0f yd estimated from the ball numbers, for the "
                     "mid-flight call", tag, estimated)

        # Play the pre-voiced flight line immediately, from the setup plan and the launch data, before
        # the (slow, under shot-animation load) impact OCR read. The line was voiced for this moment;
        # it must not wait on OCR. The impact read below still runs, for the reaction and learning.
        c = self.cfg.commentary
        if c.in_flight and not shot.is_putt and self.speech is not None and self.commentary_on and not banter \
                and not landed_already:
            tree_call = asyncio.create_task(self._tree_flight_call(fires0, impact, shot, my_seq, tag))
            tree_call.add_done_callback(lambda t: t.cancelled() or t.exception())
        flight_played = False
        plan = shot_plan = self.plan  # shot_plan survives the one-swing nulling below, for the reaction
        shot_plan_made_at = self._plan_made_at  # snapshot: the next player's pre-shot can rebind the live one
        if c.in_flight and not banter and not landed_already and not shot.is_putt \
                and flight_carry >= c.min_flight_carry and plan and time.monotonic() - self._plan_made_at < 180 and not self._tree_hit_logged(fires0, impact, shot):
            carry0 = estimated or self._raw_yards(shot)
            factor0 = self.carry.factor(plan.lie, carry0, self.current_course, shot.vla, "gspro" if estimated else "lm")
            picked = pick_line(plan, replace(shot, carry=carry0, units="Yards"), plan.to_pin, factor0, plan.elevation_yards,
                               self.gspro.aim_of(fires0, impact))
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
                self._write_transcript([line], "flight", impact_seq=my_seq)
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
            self._note_practice(before.player_text, before.shot_number)
            if self._practice:   # the first swing in practice mode, struck before a HUD poll saw it
                log.info("%s practice-mode shot ignored (club %s, carry %.1f)", tag, shot.club, shot.carry or 0)
                return
            self._keep_impact_misread(before, shot.is_putt, tag)
            shown = bool(before.player_text)
            if self._fill_from_setup(before, event.setup_hud, tag):
                shown = False
            if await self._fill_from_buffer(before, event.buffer_frames, tag):
                shown, from_buffer = False, True
            self._use_setup_lie(before, tag)
        else:
            shown = bool(before.player_text)
        player = self._player(before.player_text, event.player_hint)
        name_source = ("screen" if shown else "address frame" if from_buffer else "setup read") if player else "none yet"
        if not player and (player := self._solo_name()):
            name_source = "the round's only player"
        log.info("%s player %s (%s)", tag, player or "unknown", name_source)
        self._add_rehit_penalty(before, player)   # (the wet shot may have been recorded only after the setup)
        self._snap_added_penalty(impact_snap, before, player)
        self._count_added_penalty(before, player, tag)
        if use_screen and before.shot_number is None:
            self._number_from_log(before, player, impact_snap, tag)
        shot_one = self._misread_shot_one(before, player, impact_snap, lies0, shot_hole, tag) if use_screen else None
        # GSPro's own club box beats guessing the club from loft.
        before.club = deck_club(before.club, before.shot_number, before.lie)
        if before.club:
            shot = replace(shot, club=before.club)
        elif (club := deck_club(shot.club, before.shot_number, before.lie)) != shot.club:
            shot = replace(shot, club=club)
        handed = before.handed or shot.handed
        if player and handed:
            self.handed[player] = handed
        unit = "metres" if (before.to_pin or "").endswith("metres") else "yards"

        # GSPro's own no-penalty carry beats the launch monitor's number when it's on screen.
        raw_now = _tile_yards(before.tiles, "carry (raw)", unit)
        game_now = _tile_yards(before.tiles, "carry (game)", unit)
        if raw_now and (game_now is None or game_now < raw_now * 0.5):  # fresh, not last shot's tiles
            base, base_carry, source = "gspro", raw_now, "GSPro raw"
        elif estimated:   # (estimated as GSPro's own: its course level is GSPro's)
            base, base_carry, source = "gspro", estimated, "an estimate"
        else:
            base, base_carry, source = "lm", self._raw_yards(shot), "launch monitor"
        projected = replace(shot, carry=base_carry, units="Yards")
        factor = self.carry.factor(before.lie, base_carry, self.current_course, shot.vla, base)
        if use_screen:
            log.info("%s impact HUD (read %.2fs): %r shot %s, %s to pin, lie %s, club %s, elevation %s; "
                     "carry %.1f yd from %s x %.3f for the lie", tag, self.screen.last_read_seconds if self.screen else 0,
                     before.player_text, before.shot_number, before.to_pin, before.lie, before.club,
                     before.elevation_yards, base_carry, source, factor)
        if self.screen and self.cfg.screen.save_review_images:
            await asyncio.to_thread(self._save_review_image, tag, "impact")

        flight_task = None
        plan, self.plan = self.plan, None  # a plan is good for one swing
        # Only the fallback here now: the fresh-plan line already played above, before this read.
        if not flight_played and not banter and not landed_already and c.in_flight and not shot.is_putt and flight_carry >= c.min_flight_carry:
            fresh = plan and time.monotonic() - self._plan_made_at < 180 and plan.player in (None, player)
            aim = self.gspro.aim_of(fires0, impact)
            picked = pick_line(plan, projected, before.to_pin, factor, before.elevation_yards, aim) if fresh else None
            if fresh and self._shot_wet(plan.player, since=impact) and plan.lines.get("water"):
                picked = ("water", plan.lines["water"])  # the log already says it's wet: tease the water
            proj = project(projected, before.to_pin or (plan.to_pin if plan else None), plan.hazards if plan else [],
                           factor, before.elevation_yards, aim)
            if proj:
                pin = to_yards(before.to_pin)
                log.info("%s projection: carry %.0f yd vs pin %s (elevation %s), drift %+.0f yd%s -> %s; plan %s",
                         tag, proj.carry, f"{pin:.0f} yd" if pin else "unknown", before.elevation_yards,
                         proj.lateral, f" (aimed {math.degrees(t):+.0f} deg)" if (t := aim_turn(aim)) else "",
                         proj.outcome, "ready" if fresh else ("stale" if plan else "none"))
            if self._tree_called == my_seq or self._tree_hit_logged(fires0, impact, shot):
                pass   # a tree hit is called (or about to be): the plan's line and a fresh one no longer fit
            elif picked and self._shot_seq == my_seq:   # not if another ball was struck during the impact read
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
                self._write_transcript([line], "flight", impact_seq=my_seq)
                self.flight_latencies.append(time.monotonic() - impact)
            elif self._shot_seq == my_seq:
                flight_task = asyncio.create_task(
                    self._in_flight(projected, before, player, impact_image, impact, factor, my_seq, fires0,
                                    stymie=self._stymie_seq == my_seq - 1))

        # A ball in the water is a penalty with no resting lie to wait for, so SAY its splash NOW, off
        # the GSPro log's Hit_water (ingested by this point, after the impact read), instead of behind
        # the wait for GSPro's round file. It does NOT return: the handler still waits for it and records the
        # shot below (so the fairway streak resets and the stroke is counted); `splash_said` then skips
        # the reaction section. Falls through to the reaction-path water check if not wet yet here.
        splash_said = False
        if c.pre_reactions and not banter and not shot.is_putt and shot_plan and shot_plan.reactions.get("water") \
                and time.monotonic() - shot_plan_made_at < 180 and shot_plan.player in (None, player) \
                and self._shot_seq == my_seq and self._shot_wet(player, since=impact):
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
            self._write_transcript([line], "reaction", impact_seq=my_seq, after=True)
            self.stats["reaction_planned"] = self.stats.get("reaction_planned", 0) + 1
            splash_said = True
            log.info("%s water splash reaction (early, off Hit_water) %.1fs after impact: %r", tag,
                     time.monotonic() - impact, text)

        await self._read_putter(shot, fires0, impact, tag)
        # GSPro's log says a full shot ended the hole (holed, or stopped inside the gimme circle) a second
        # or two after the ball stops.
        end_idx = self._gspro_idx(player) if self._log_truth and not shot.is_putt else None

        def hole_over() -> bool:
            return end_idx is not None and (
                self.gspro.hole_outs_of(end_idx) > impact_snap[2].get(end_idx, 0)
                or self.gspro.gimmies_of(end_idx) > impact_snap[3].get(end_idx, 0))

        # Where the ball stopped is GSPro's round file's (the screen isn't followed). Its stroke is one first
        # read after the swing (a shot with no impact time: after the feed delivered it).
        file_track, file_timing = None, {}
        file_since = event.impact_at or event.received_at or impact
        if event.hud_hint is None and not (event.landed and event.round_shot_id is None):
            # (no screen needed: the file is read with OCR off too. A shot the feed had from GSPro.db's
            # LastShot once its round file went unwritten, practice, isn't waited on.)
            after, landed, file_track, file_timing = await self._wait_for_file(
                before, tag, shot.is_putt, player=player, ball_speed=shot.ball_speed,
                impact=impact, unit=unit, seq=my_seq, since=file_since, known=event.round_shot_id,
                shot_round=shot_round)
        else:   # a replay's shot, its resting read with it; or LastShot's
            after, landed = Hud(), event.hud_hint is not None
        if use_screen:
            log.info("%s final reading %.1fs after impact (landed=%s%s): lie %s, carry %s, total %s, offline %s", tag,
                     time.monotonic() - impact, landed, ", GSPro's round file" if file_track else "", after.lie,
                     after.tiles.get("carry (game)"), after.tiles.get("total length"), after.tiles.get("offline (raw)"))
        if file_track is not None and not shot.is_putt and file_timing:
            self._bg(self._log_file_latency(tag, file_timing, fires0, impact))
        file_ours, screen_before = file_track is None or self._file_ours(file_track, shot_round, tag), before
        if file_track is not None and file_ours and (n := self._file_stroke(file_track, player)) != before.shot_number:
            # GSPro's count (plus a penalty the booth counted that GSPro left out), before anything (the
            # opening tee shot, a drive, a streak) is worked out from it
            log.info("%s stroke %s on GSPro's count (its round file; the screen read %s)", tag, n, before.shot_number)
            before = replace(before, shot_number=n,
                             gspro_shot=file_track.shot_number if n != file_track.shot_number else None)
        if shot_round is None and file_track is not None and file_ours and (cur := self.round) and not cur.ended \
                and cur.seq == 1 and self._round_started_at >= impact and not self._file_ours(file_track, cur, tag):
            before, file_ours = screen_before, False   # the round that may adopt it isn't GSPro's: the screen's count
        # (the screen's tiles, offline and raw carry, are read at the next setup: _apply_late_tiles)
        shown_before = self._shown_tiles
        if not held and self._fire_log_followed() and not await self._gspro_fired(fires0, impact, event):
            log.warning("%s dropped: GSPro never played it (no \"Shot Fired\" in its log; club %s, ball %.1f mph, "
                        "carry %.1f): not recorded or called", tag, shot.club, shot.ball_speed or 0, shot.carry or 0)
            return
        try:
            hc = self._hc   # (this round's: another may have started by the time the tiles are read)
            # No screen read after the swing: the impact read's tiles count, and the next setup's
            # read takes back any it missed (_apply_late_tiles).
            missed = self._health_shot(before, Hud(), shot.is_putt, ocr)
        except Exception:
            missed = set()
            log.exception("Health shot count failed")
        hud = event.hud_hint or Hud(
            player_text=before.player_text, shot_number=before.shot_number, to_pin=before.to_pin,
            score_to_par=before.score_to_par, lie=after.lie, tiles=after.tiles,
        )
        player = player or self._player(hud.player_text, event.player_hint)
        # The opening tee shot can be struck before the round starts: the round is only known from the
        # setup read that follows the swing (a round quit and restarted, the first player swinging fast).
        # With no setup read before the swing (straight on from the last round), GSPro's log says it
        # was the first stroke on the hole the round started on.
        opening = before.shot_number == 1
        if before.shot_number is None and (info := self.gspro.fire_info(self.gspro.fire_of(fires0, impact))):
            opening = info.get("score") == 1 and info.get("hole") is not None and info["hole"] == self.gspro.first_hole
        if shot_round is None and opening and (cur := self.round) and not cur.ended \
                and cur.seq == 1 and self._round_started_at >= impact:
            shot_round, round_hole = cur, cur.seq
            if _hole_key(shot_hole) == _hole_key(self.state.current_hole):
                shot_hole = self.state.current_hole   # the opening hole's label once the round numbered it
            log.info("%s struck just before round %s started: it's the round's opening tee shot", tag, cur.id)
        unit = "metres" if (hud.to_pin or before.to_pin or "").endswith("metres") else "yards"

        def tile_yards(name: str) -> float | None:
            return _tile_yards(hud.tiles, name, unit)

        # Values read before the ball was seen to stop are partial; they'd teach the model nonsense.
        carry_game = None if shot.is_putt or not landed else tile_yards("carry (game)")
        carry_flat = None if shot.is_putt or not landed else tile_yards("carry (raw)")
        if carry_flat is None and file_track is not None and carry_game and source == "GSPro raw":
            carry_flat = raw_now   # no screen read at rest: the fresh one read at impact
        if self.gspro.altitude_ft is not None and self.current_course \
                and self.current_course not in self.cfg.commentary.course_altitude_ft:
            self.carry.altitude[self.current_course] = self.gspro.altitude_ft   # beats the course file's
        # A team format where GSPro copies balls between teammates: the next stroke may be a partner's
        # ball, unless GSPro dropped none of this player's balls in between (their own was picked).
        # (never for a lone player in a scramble: nobody else's ball can be theirs)
        shared = not (shot_round and shot_round.solo(shot_round.side_for(player))) and bool(
            shot_round and (shot_round.format == "scramble" or shot_round.no_scramble) or self.gspro.team_scores)
        unnamed = not player or player == "unknown" or self._anonymous(player)
        if not player or player == "unknown":
            self._keep_report_frame("no-player", shot=before.shot_number, hud_text=hud.player_text)
        carry_idx = None if unnamed else self._gspro_idx(player)
        dropped = None if carry_idx is None else impact_snap[7].get(carry_idx, 0)
        check_vla = shot.vla   # (the monitor's, before any club's fields are filtered)
        if flight_task:
            try:
                await flight_task  # bounded by the flight deadline; keeps the call ahead of the reaction
            except Exception:   # a failed call mustn't cost the shot its record
                log.warning("%s mid-flight call failed", tag, exc_info=True)
        if file_track is None and not ocr and self._round_watch is not None and not shot.is_putt:
            # (no follow: a shot fed in flight may have come to rest in GSPro's file since)
            file_track = self._file_track_of(player, shot.ball_speed, file_since, claim=my_seq,
                                             known=event.round_shot_id)
            if file_track is not None:   # its resting read and stroke number, as an early find's
                hud, landed = self._file_after(hud, file_track, unit), True
                after = self._file_after(after, file_track, unit)
                carry_game = tile_yards("carry (game)")
                file_ours = self._file_ours(file_track, shot_round, tag)
                if file_ours and (n := self._file_stroke(file_track, player)) != before.shot_number:
                    log.info("%s stroke %s on GSPro's count (its round file; the screen read %s)", tag, n,
                             before.shot_number)
                    before = replace(before, shot_number=n,
                                     gspro_shot=file_track.shot_number if n != file_track.shot_number else None)
                    hud = replace(hud, shot_number=n)
        if file_track is not None and file_ours and before.shot_number != screen_before.shot_number \
                and not self._file_ours(file_track, shot_round, tag):   # GSPro's round tied to another since
            file_ours = False
            before = replace(before, shot_number=screen_before.shot_number, gspro_shot=screen_before.gspro_shot)
            hud = replace(hud, shot_number=screen_before.shot_number)
        # GSPro's club for the stroke fills one the screen read none for (round_file.club_fix), before the
        # drive records and the club's monitor fields are worked out from it.
        screen_club, file_putt = None, False
        if file_track is not None and file_ours:   # (as the file has it now: a hole-out since takes its club)
            file_track = self._file_track_of(player, None, impact, file_track.shot_id) or file_track
        # Not live on a stroke at rest on the green (or anywhere unknown): the hole may yet end on it (a
        # gimme, holed), GSPro's club then the putter its hole-out selects. The stroke's pin fills it.
        if file_track is not None and file_ours and file_track.lie_after not in (None, "green") \
                and (club := round_file.club_fix(file_track, shot.club)) is not None:
            log.info("%s club %s -> %s from GSPro's round file", tag, shot.club or "unread", club)
            screen_club, shot = (shot.club, shot.putter, shot.is_putt), replace(shot, club=club)
        elif file_track is not None and file_ours and not shot.club and shot.putter is None \
                and not self.gspro.logs_launch_mode and file_track.putt is not None \
                and file_track.putt != shot.is_putt:
            # (no club to say, nor GSPro's log: its file's call at impact over the speed-and-launch guess)
            log.info("%s GSPro's round file has it %s", tag, "a putt" if file_track.putt else "a full shot")
            shot, file_putt = replace(shot, putter=file_track.putt), True

        prior = self.state.player_shots(player or "unknown")
        score, tags = score_shot(shot, prior, hud, to_yards(before.to_pin))
        scored = (score, list(tags), hud)   # (with the club as it stands: a fill taken back below rescores it)
        # The player's own longest/avg drive is a lifetime personal best (a long drive is a long drive,
        # league or practice). The LEAGUE record, though, must be league-only: a huge practice drive is
        # not "the league's best drive this season", so that bar reads from the league slice.
        season = self.store.season_stats(player) if player else {}
        season_facts = []
        # Drives are measured by total distance (carry and roll), as the season records are.
        drive = (tile_yards("total length") if landed else None) or carry_game or shot.carry
        # Only a drive off the tee counts against the driving records, never a putt or a fairway driver.
        tee_drive = is_drive(shot.club, shot.is_putt, before.shot_number, before.lie, to_yards(before.to_pin))
        score_before_drive = score
        if tee_drive and drive and season.get("longest_drive"):
            # Earlier weeks are the record, but a longer drive earlier today already broke it: every
            # drive past last week's best was "a personal best" all night, the shorter ones included.
            today = self.store.today_longest_drive(player)
            if drive > season["longest_drive"] and today and drive <= today:
                season_facts.append(f"Not a personal best: {player} hit a longer drive earlier today "
                                    f"({today:.0f} yards).")
            elif drive > season["longest_drive"]:
                tags.append("season_best_drive")
                score = max(score + 0.5, c.big_moment_threshold)
                beaten = (f"today's earlier best ({today:.0f} yards)" if today and today > season["longest_drive"]
                          else f"{player}'s longest drive from earlier weeks ({season['longest_drive']:.0f} yards)")
                season_facts.append(f"That's a new personal best: longer than {beaten}. Excitement "
                                    f"{SEASON_BEST_DRIVE_LEVEL} out of 10.")
            elif shot_round is not None and shot_round.is_league \
                    and (best := max(filter(None, (self.store.season_longest_drive(context="league"),
                                                   self.store.today_longest_league_drive())),
                                     key=lambda b: b[1], default=None)) and drive > best[1]:
                tags.append("league_best_drive")
                score = max(score + 0.6, c.big_moment_threshold)
                season_facts.append(f"That's the longest drive in the league this season, past {best[0]}'s "
                                    f"{best[1]:.0f} yards. Excitement {LEAGUE_BEST_DRIVE_LEVEL} out of 10.")
        if tee_drive and season.get("avg_drive"):
            season_facts.append(f"{player}'s average drive in earlier weeks: {season['avg_drive']:.0f} yards.")
        drive_facts, drive_bump = list(season_facts), score - score_before_drive   # (all of them, so far)
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
        # Read before the lie fix-up below, which can recast a green set up in yards as the fringe.
        off_wrong_green = None if shot.is_putt else wrong_green_yards(before.lie, before.to_pin)
        info, material = self.gspro.fire_info(fire), MATERIAL_TO_LIE.get(lies0.get(idx)) if idx is not None else None
        file_lie = file_track.lie_before if file_track is not None and file_ours else None
        # The setup lie GSPro's file gave, from a round that isn't this one's: back to the screen's.
        unfiled = self._unfiled_setup(before, screen_before, shot_one)
        if file_track is not None and not file_ours and unfiled.lie != before.lie:
            log.info("%s set up on %s after all (not GSPro's round file's %s)", tag, unfiled.lie, before.lie)
            before = unfiled

        def played(b: Hud, file_lie: str | None) -> tuple[str | None, bool]:
            p = self._played_from(b, shot, info, material, shot_hole, file_lie)
            return (self._off_green_lie(b), True) if p == "green" and self._set_up_in_yards(b) else (p, False)

        played_from, in_yards = played(before, file_lie)
        if in_yards:
            log.info("%s set up in yards: off the green, recorded as %s", tag, played_from)
        # Where it was played from without GSPro's round file, should that round turn out another's.
        # (recorded with it: GSPro's goes on with the stroke's pin, which keeps this one to go back to)
        unfiled = replace(self._unfiled_setup(before, screen_before, shot_one), shot_number=screen_before.shot_number)
        screen_played = played(unfiled, None)[0] if file_lie else None
        # The HUD's Tee recast only for want of a distance to check it by may still be a re-tee: keep its driver.
        retee = (before.lie or "").lower() == "tee" and self._at_tee_length(before, shot_hole) is None
        if played_from != before.lie:
            log.info("%s played from %s (the HUD read %s)", tag, played_from, before.lie)
            before = replace(before, lie=played_from)
        deck_from = shot.club
        if not retee and (club := deck_club(shot.club, before.shot_number, before.lie)) != shot.club:
            shot = replace(shot, club=club)   # off the deck (from the feed's club, or a stale "tee" read)
        if not shot.is_putt and "in_water" not in tags and self._shot_wet(player, since=impact):
            tags = [*tags, "in_water"]   # the GSPro log saw the splash: the wrap can say where it went
        if file_track is not None:   # a penalty or drop GSPro wrote since it was taken
            file_track = self._file_track_of(player, None, impact, file_track.shot_id) or file_track
            if file_track.penalty and not shot.is_putt:   # where it stopped isn't where play goes on
                hud = self._file_after(hud, file_track, unit)
        penalty_facts, penalty_tag = await self._penalty_shot_facts(fire, shot, tags, track=file_track)
        if penalty_tag and penalty_tag not in tags:
            tags = [*tags, penalty_tag]
        r = shot_round
        if r and player:
            player = r.current_name(player)   # a guest renamed while this shot was in flight
        relie = bool(file_lie) and screen_played != before.lie
        if file_track is not None and file_ours and (before.shot_number != screen_before.shot_number
                                                     or screen_club is not None or file_putt or relie) \
                and not self._file_ours(file_track, shot_round, tag):   # GSPro's round tied to another since
            file_ours = False
            if relie:   # (not the lie its stroke started from either)
                log.info("%s played from %s after all (not GSPro's round file's %s)", tag, screen_played, before.lie)
                before = replace(before, lie=screen_played)
                if screen_club is None and not retee:
                    shot = replace(shot, club=deck_club(deck_from, screen_before.shot_number, before.lie))
            before = replace(before, shot_number=screen_before.shot_number, gspro_shot=screen_before.gspro_shot)
            hud = replace(hud, shot_number=screen_before.shot_number)
        fire0 = self.gspro.fire_of(fires0, impact)
        # GSPro's carry ends where the ball first met something: wood well short of the full carry stood up.
        wood_tree = self.gspro.tree_wood(fire0, carry_game, carry_flat or self._raw_yards(shot))
        tree_hits = len(self.gspro.fire_info(fire0).get("leaves") or ()) + wood_tree
        if screen_club is not None and file_ours:   # (a hole-out copy may have come in since)
            file_track = self._file_track_of(player, None, impact, file_track.shot_id) or file_track

        def take_back() -> None:
            """The club GSPro's round file filled (club_fix) back to the screen's, or the putter GSPro's log
            called only after the fill: the stroke turned out another round's, or one the hole has ended
            on since (its hole-out selects the putter), and what that club scored it (a chip as a putt,
            say) or the driving records it set go with it."""
            nonlocal shot, screen_club, file_putt, tags, score, season_facts
            if screen_club is None:   # (the file's putt or not, with no club to say: the guess again)
                club = shot.club
                log.info("%s GSPro's round file's putt call taken back", tag)
                shot = replace(shot, putter=None)
            else:
                club = "PT" if screen_club[1] is None and shot.putter \
                    else screen_club[0] if retee else deck_club(screen_club[0], before.shot_number, before.lie)
                log.info("%s club %s -> %s: back to the screen's", tag, shot.club, club or "unread")
                shot = replace(shot, club=club)
            screen_club, file_putt = None, False
            rescore, retags = score_shot(shot, prior, scored[2],
                                         to_yards(screen_before.to_pin if not file_ours else before.to_pin))
            tags = [*retags, *(t for t in tags if t not in scored[1] and t not in retags)]
            score += rescore - scored[0]
            if tee_drive and not is_drive(club, shot.is_putt, before.shot_number, before.lie,
                                          to_yards(before.to_pin)):   # its driving records were the file club's
                tags = [t for t in tags if t not in ("season_best_drive", "league_best_drive")]
                season_facts = [f for f in season_facts if f not in drive_facts]
                score -= drive_bump

        if screen_club is not None and (not file_ours or round_file.club_fix(file_track, screen_club[0]) is None) \
                or file_putt and not file_ours:
            take_back()
        # The player and club are final here: drop the club fields this player's monitor doesn't measure
        # with this club (zeros it wrote for them), before the shot is recorded or talked about. Not on a
        # shared ball: the name on it isn't who hit it.
        measured = shot   # (what's stored under a filled club: the stroke's pin filters it, reversibly)
        if (seen := getattr(self.store, "club_fields_seen", None)) and not self._anonymous(player):
            shot = club_fields.apply(player, shot, seen)
        # A standout club delivery (big angle of attack, a real shape, a flush or a clear mishit) is
        # worth a word even when the result was ordinary, so it crosses the interest threshold and
        # gets a live reaction carrying the club facts below. Only for players with the markers.
        # Club delivery is spice: a notable one now and then, an ordinary one rarely, never on two
        # reactions in a row (every shot's face-to-path got old fast).
        notable_delivery = club_delivery_notable(shot) and not is_chip(shot, before.to_pin)
        use_delivery = self._delivery_turn(notable_delivery)
        if notable_delivery and use_delivery:
            score += 0.5
            tags.append("club_notable")
        rec = ShotRecord(
            player or "unknown", shot, shot_hole, hud, tags,
            round_id=r.id if r else None, hole_seq=round_hole, par=shot_par,
            lie_before=before.lie, shot_number=before.shot_number, to_pin_before=before.to_pin,
            carry_game=carry_game, impact_seq=my_seq, impact_at=file_since,
        )
        if idx is not None and (log_hole := impact_snap[1].get(idx)) is not None \
                and not self._log_hole_finished(idx, log_hole, impact_snap):
            rec.log_hole = log_hole + 1   # (a finished one is the log's until the next tee shot: not this one's)
        if idx is not None and (start_m := impact_snap[0].get(idx)) is not None:
            # Where this stroke was struck from (GSPro's distance at impact, in metres, to the inch).
            rec.gimme_edge = gimme_edge(start_m * M_TO_YD, self.gspro.gimme_of(idx))
        self.state.record_shot(rec)
        if r and r.seq >= r.holes and before.lie == "green":
            self._putted_on_last_hole = True
        # Per-player stats from GSPro's log, attached to the player via their GSPro index (None on an
        # alternate-shot round, where the ball is shared and the log can't say which partner hit, so
        # those shots aren't attributed). finish_yards (how close the ball got) is set now for full
        # shots; a putt's finishing distance and holed/conceded outcome are filled in below. (idx was
        # resolved above for the closest-approach record.)
        fmt = r.player_format(player) if r else None
        # Attribution is independent of the GSPro log: a shot is attributed to its player unless the
        # ball is shared (any one-ball team format, via _anonymous), where GSPro's name isn't who hit.
        # The log-only fields (finish/gimme distance) are gated separately on idx, so turning the log
        # off doesn't silently exclude every row from the stats.
        attributed = bool(player) and player != "unknown" and not self._anonymous(player)
        # The impact snapshot is where this player's ball sat going into this shot = the previous
        # shot's resting place, so it resolves the previous full shot's finishing distance (GSPro
        # doesn't log a shot's finish until the following shot is set up, and the wait for the ball at rest has since
        # advanced the live state, so a snapshot taken at THIS impact is the reliable source). Guarded
        # by shot order: a slow earlier handler (a long wait on GSPro's round file) must not resolve/stash after a
        # later shot already did, which would scramble the "next setup = this rest" pairing.
        in_order = idx is not None and my_seq > self._finish_seq.get(idx, 0)
        # Alternate shot: a player's next stroke is the partner's ball after theirs (often on the next
        # hole), so their own last shot's rest isn't where it was played from. _team_finishes has it.
        alternate = fmt == "alternate"
        if in_order:
            self._finish_seq[idx] = my_seq
            if alternate:
                self._pending_finish.pop(idx, None)
                self._last_row.pop(idx, None)
            else:
                self._resolve_pending_finish(idx, impact_snap)
                self._correct_last_finish(idx, impact_snap, before.lie, shot_hole, before.shot_number, fmt)
        if r is not None and not r.ended:   # a shot of no round (or an ended one) says nothing of the next
            self._note_tournament(r)
        # The round's course, fixed at its start: the hole box's read each hole can be cut short.
        shot_course = (r.course if r is not None else None) or self.current_course
        # The screen's club, and putt or not, with its fields unfiltered: GSPro's go on with the stroke's
        # pin, which keeps the screen's to go back to. GSPro's log's call, when it came only after the club
        # was filled: a putter it called is the putter's.
        # The file's putt call with no club to go on is its pin's to make, reversibly: the guess here.
        stored = replace(shot, putter=None) if file_putt else shot
        if screen_club is not None and screen_club[1] is None and shot.putter:
            stored = replace(shot, club="PT")
        elif screen_club is not None:
            late = screen_club[1] is None and shot.putter is not None
            stored = replace(measured, club=screen_club[0], putter=shot.putter if late else screen_club[2])
        shot_id = self.store.record_shot(
            player=player, hole=shot_hole, shot=stored,
            before=replace(before, lie=screen_played) if file_lie else before, after=hud, tags=tags,
            carry_game_yards=carry_game, total_yards=tile_yards("total length"), offline_yards=tile_yards("offline (raw)"),
            course=shot_course, carry_raw_tile_yards=carry_flat,
            to_pin_yards=to_yards(before.to_pin),
            finish_yards=None,   # filled in on this player's next shot (see _resolve_pending_finish)
            gimme_yards=self.gspro.gimme_of(idx), fmt=fmt, attributed=attributed,
            is_league=bool(r and r.is_league), shared_ball=shared, is_tournament=bool(r and r.is_tournament),
            # where GSPro's log had the ball for this shot, and the player's minimap aim, for the replay
            start=self.gspro.fire_info(fire).get("start"), aim=self.gspro.fire_info(fire).get("aim"),
            handed=before.handed or shot.handed or self.handed.get(player or ""), tree_hits=tree_hits or None,
            record=r.uuid if r is not None else None,   # (its round's, even one ended or replaced since impact)
        )
        rec.shot_id = shot_id
        self._shots_unrecorded.pop(id(event), None)
        if file_track is not None and shot_id is not None:
            # (a penalty's ball has no resting total: none is taken from the screen either)
            self._file_shot_ids[shot_id] = (file_track.carry_yards is not None,
                                            file_track.total_yards is not None or file_track.penalty)
            while len(self._file_shot_ids) > 64:
                del self._file_shot_ids[next(iter(self._file_shot_ids))]
        # Another round's or course's: its rest stands (it's this swing's), but not GSPro's numbering of it.
        if file_track is not None and shot_id is not None \
                and file_ours and self._file_ours(file_track, shot_round, tag, shot_course):
            if before.shot_number != screen_before.shot_number:
                try:
                    self._keep_screen_stroke(rec, file_track, screen_before.shot_number)
                except sqlite3.Error as e:
                    log.warning("%s couldn't keep the screen's stroke number: %s", tag, e)
            self._pin_file_track(file_track, shot_id)
            self._round_file_kick = True   # the store takes GSPro's stroke now, not at the next look
            self._file_nudge.set()
            moved = None
            try:
                moved = self._file_hole_check(rec, file_track, shot_round, round_hole, shot_hole, tag)
            except sqlite3.Error as e:
                log.warning("%s couldn't move the shot to GSPro's hole: %s", tag, e)
            if moved is not None:   # it's called on GSPro's hole and stroke
                shot_hole, shot_par = moved[0], moved[1] or shot_par
                if moved[2] and moved[2] != before.shot_number:
                    before = replace(before, shot_number=moved[2])
        # The carry check reads the shot's record (its lie, hole, stroke and game carry) when it's made, so
        # what GSPro's round file or a late read corrects on it since counts.
        check = None
        if tree_hits and not shot.is_putt:
            log.info("%s hit a tree %s time(s) in flight: its carry isn't checked", tag, tree_hits)
        elif not shot.is_putt:
            check = (("gspro", carry_flat) if carry_flat else ("lm", self._raw_yards(shot))) + (shot_course, check_vla)
        if self._log_truth and fire is not None and shot_id is not None:
            self._bg(self._note_rest(shot_id, fire, impact))
        if ocr and not shot.is_putt and shot_id is not None:
            self._tiles_due[my_seq] = (shot_id, rec, shown_before, unit, hc, missed)
            while len(self._tiles_due) > 4:
                del self._tiles_due[next(iter(self._tiles_due))]
            if (late_raw := self._apply_late_tiles(my_seq)) and check and check[0] == "lm":
                check = ("gspro", late_raw) + check[2:]
        # Into the trail after its buffered tiles, so a next stroke already in checks it with them.
        self._trail_carry(player, (my_seq, rec, before.elevation_yards, to_yards(before.to_pin), check,
                                   (shared or unnamed, dropped)))
        for m in self._live_mirrors():
            m.shot()   # a stream with no shots on it isn't kept
        # Not gated on the screen seeing touchdown: the finish comes from GSPro's log at the next shot,
        # and a water or OB ball is caught there by its penalty. (Gating on it lost half the finishes.)
        if in_order and not shot.is_putt and not alternate:
            self._pending_finish[idx] = (shot_id, {**self._idx_snap(impact_snap, idx),
                                                   "hud_yards": to_yards(before.to_pin)})
        if in_order and not alternate:
            snap = self._idx_snap(impact_snap, idx)
            self._last_row[idx] = (shot_id, rec, snap["hole"], snap["pen"], snap["dropped"])
        if r is not None and rec.round_id == r.id:
            r.name_sources[name_source] = r.name_sources.get(name_source, 0) + 1
            r.box_hidden += use_screen and not shown
            r.physics.update(self.gspro.fire_info(fire).get("physics") or {})
        if fire is not None:
            self._fire_rows[fire] = (shot_id, rec, idx, impact_snap[1].get(idx) if idx is not None else None)
            self._row_source[fire] = name_source
            self._adopt_late_shot(fire)
            if r is not None and self._log_truth:
                self._owners_due.add(fire)
            self._forget_fires(fire - 60)
            if not self.gspro.fire_info(fire).get("start"):
                self._starts_due.add(fire)
            else:
                self._totals_due = True
            self._apply_log_corrections()   # a mulligan logged before this re-hit was recorded
        replay_facts = self._replay_facts(player, rec)
        must = ("misread" if "misread" in replay_facts[0] else "mulligan") if replay_facts else None
        if off_wrong_green is not None:
            score = max(score, c.interest_threshold)   # a shot off another hole's green gets its word
        chip_off_green = not shot.is_putt and (played_from or "").lower() == "green" and off_wrong_green is None
        if chip_off_green:
            score = max(score, c.interest_threshold)   # a chip off the putting surface gets its word
            tags = [*tags, "green_chip"]
        if replay_facts:
            score = max(score, c.interest_threshold)   # the mulligan gets its word
        if kick := await self._kick_facts(fire, hud.lie, shot.is_putt, wood_tree=wood_tree):
            score = max(score, c.interest_threshold)   # a bounce off the cart path gets its word
            replay_facts = replay_facts + kick
        if tree := self._tree_facts(fire, shot, impact, tag, wood_tree):
            score = max(score, c.interest_threshold)   # a ball knocked down by a tree gets its word
            replay_facts = replay_facts + tree
        if penalty_facts:
            score = max(score, c.interest_threshold)   # out of bounds gets its word
            replay_facts = replay_facts + penalty_facts
        self._shot_id = shot_id   # for record_lines linkage; the local guards against a concurrent task
        log.info("%s by %s: interest %.2f %s", tag, player or "unknown", score, tags)

        # Solo: the ball reaching the green on a full shot is the cue to pre-build the wrap (one-,
        # two- or three-putt finish), ready well before the putts end. In a group the trigger is the
        # scorecard instead (the second-to-last side holing out), handled in handle_scorecard.
        if r and len(r.sides) == 1 and not shot.is_putt and (hud.lie or "").lower() == "green" and before.shot_number:
            n = before.shot_number
            self._kick_wrap_branches(r.sides[0].key, [n + 1, n + 2, n + 3], [])

        # Resolve a putt's result once, for EVERY putt, so the outcome, finishing distance and the
        # prediction grade are never skipped (a notable putt, a too-soon one, or a failed dice roll all
        # used to skip this). Prefer GSPro's log (holed vs conceded); the banner is the fallback, and
        # when it's the only signal an absent banner is 'unknown', not a miss, so a call isn't misgraded.
        holed = None
        close_feet = None
        long_gimme = False
        gimme_level = gimme_feet = None
        # (a putt the feed took from GSPro's round file: its roll, exactly)
        file_roll = file_track.total_yards if shot.is_putt and file_track is not None \
            and file_track.shot_id == event.round_shot_id else None
        roll_yards = file_roll if file_roll is not None else tile_yards("total length")
        outcome = full_result = None
        wrong_green = None
        # A putt the feed took from GSPro's round file, which has the hole ended on it: a definitive drop.
        log_drop = False   # the putt's result from the log's score line for its fire (no name for the slot)
        file_drop = shot.is_putt and file_track is not None and file_track.shot_id == event.round_shot_id \
            and (file_track.dropped or file_track.picked_up)
        if shot.is_putt and ((self.screen is not None and ocr) or idx is not None or file_drop
                             or self._round_watch is not None or (self._log_truth and self.gspro.mode == "course")):
            base_outs, base_gim = impact_snap[2].get(idx, 0), impact_snap[3].get(idx, 0)
            if file_drop:   # GSPro's file has it: no waiting on the log
                outcome = file_putt_outcome(file_track)
                self._record_result(shot_id, outcome, tag, putt=True, rec=rec, left_yards=file_track.finish_yards)
                holed = outcome == "holed"
                log.info("%s putt outcome from GSPro's round file: %s", tag, outcome)
                if holed:
                    score = holed_putt_score(score, before, tags, c.interest_threshold, c.big_moment_threshold,
                                             putt_to_par(before.shot_number, shot_par))
            elif idx is not None:
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
                                                       base_setup=base_setup, fire=fire)
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
                                               base_hole=base_hole, base_setup=base_setup, rec=rec, fire=fire))
                elif self._record_result(shot_id, outcome, tag, putt=True, rec=rec) is False:
                    outcome = None   # (not a putt after all: nothing to grade a call on)
                else:
                    holed = outcome == "holed"
                    log.info("%s putt outcome from GSPro log: %s (%.1fs after impact)", tag, outcome,
                             time.monotonic() - impact)
                    if holed:
                        score = holed_putt_score(score, before, tags, c.interest_threshold,
                                                 c.big_moment_threshold,
                                                 putt_to_par(before.shot_number, shot_par))
            else:
                # No log attribution (practice/range mode, or an unmapped multiplayer slot): GSPro's round
                # file, when it has this putt (player, ball speed, after impact) ending the hole, says holed
                # or conceded. Else fall back to the on-screen result banner. It only says the hole ENDED -
                # a conceded miss inside the gimme circle ends it too - so it's a holed HINT, not a
                # definitive drop; no outcome written.
                # (looked at again after the banner: a putt still rolling at its first look is written
                # at rest, before the banner shows)
                # With no screen to read, the file alone, waited on while the ball may still be rolling
                # (in a round, once a live GSPro has written the file: practice putts aren't in it).
                screen = self.screen is not None and ocr
                putt_track = await self._file_hole_end(
                    player, shot.ball_speed, file_since, my_seq,
                    wait=c.hole_result_window_seconds if not screen and self._file_live() else 0.0)
                window = c.hole_result_window_seconds
                # The banner is up for ~2s only: watched for while the log's credit is waited on, so a
                # stroke the log never credits still has it (one it does goes by its score line).
                banner = asyncio.create_task(self._hole_result(window)) if putt_track is None and screen else None
                if putt_track is None and (credit := await self._log_shooter(fires0, impact, window)):
                    # GSPro's log credited this stroke to a slot it can't name: its score line, not the banner.
                    shooter, fire = credit
                    outcome = await self._wait_putt_result(
                        shooter, impact_snap[2].get(shooter, 0), impact_snap[3].get(shooter, 0),
                        impact_snap[6].get(shooter, self.gspro.shot_number(shooter)),
                        max(0.5, impact + c.hole_result_window_seconds - time.monotonic()),
                        base_hole=impact_snap[1].get(shooter), base_setup=setups0.get(shooter, 0), fire=fire)
                    if outcome != "unknown" and self._record_result(shot_id, outcome, tag, putt=True,
                                                                     rec=rec) is False:
                        outcome = None   # (not a putt after all: nothing to grade a call on)
                    elif outcome != "unknown":
                        log_drop = True
                        holed = outcome == "holed"
                        log.info("%s putt outcome from GSPro's log (its slot %s's score line): %s", tag, shooter,
                                 outcome)
                        if holed:
                            score = holed_putt_score(score, before, tags, c.interest_threshold,
                                                     c.big_moment_threshold, putt_to_par(before.shot_number, shot_par))
                if banner is not None and log_drop:
                    banner.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await banner
                elif banner is not None:   # the banner only says the hole ended
                    self.last_hole_result = await banner
                    holed = True if self.last_hole_result else None
                    putt_track = await self._file_hole_end(player, shot.ball_speed, file_since, my_seq)
                if putt_track is not None:
                    file_drop = True
                    outcome = file_putt_outcome(putt_track)
                    self._record_result(shot_id, outcome, tag, putt=True, rec=rec,
                                        left_yards=putt_track.finish_yards)
                    holed = outcome == "holed"
                    log.info("%s putt outcome from GSPro's round file: %s", tag, outcome)
                    if holed:
                        score = holed_putt_score(score, before, tags, c.interest_threshold,
                                                 c.big_moment_threshold, putt_to_par(before.shot_number, shot_par))
                elif log_drop:
                    pass
                elif not screen and self._file_live():
                    # Still rolling, maybe (a long lag): the record gets its end once GSPro writes it.
                    self._bg(self._late_file_end(shot_id, tag, rec, player, shot.ball_speed, file_since, my_seq,
                                                 putt=True))
                elif screen and self.last_hole_result:
                    log.info("%s hole result banner: %s (%.1fs after impact)", tag,
                             self.last_hole_result, time.monotonic() - impact)
            # Grade this player's pre-putt call, only when the log could attribute the result (the
            # banner can't distinguish a drop from a conceded miss).
            pending_prediction = self._pending_prediction.pop(player, None)
            if pending_prediction and (idx is not None or file_drop or log_drop) \
                    and outcome not in (None, "unknown") \
                    and pending_prediction[2] == shot_hole:
                commentator, predicted_make, _ = pending_prediction
                made = outcome == "holed"
                self.store.record_prediction(commentator, "putt", "make" if predicted_make else "miss",
                                             "made" if made else "missed", predicted_make == made)
            # The impact read caught the ball mid-roll; the banner window has let it settle, so re-read
            # the roll and keep whichever travelled further.
            if ocr and file_roll is None:
                settled = _tile_yards((await self.screen.read_hud_async(["tiles"])).tiles, "total length", unit)
                if settled is not None and (roll_yards is None or settled > roll_yards):
                    roll_yards = settled

        elif not shot.is_putt and file_track is None and not (idx is not None and self._log_truth) \
                and self._file_live():
            # No log slot, and the ball not yet at rest in GSPro's round file (fed in flight with no screen,
            # or its stroke didn't come while it was waited for): the record still gets a hole-out or a gimme from it.
            self._bg(self._late_file_end(shot_id, tag, rec, player, shot.ball_speed, file_since, my_seq))
        elif not shot.is_putt and (idx is not None and self._log_truth or file_track is not None):
            # A full shot can end the hole: holed out, or stopped inside the gimme circle (the putt is
            # conceded, never struck). The log says so a second or two after the ball stops, so for a
            # ball that finished on or near the green, wait briefly: that's a moment worth a live call,
            # not the pre-written "on the green" line. Otherwise (or if the log is slow) keep watching
            # in the background so the record still gets it.
            # (GSPro's round file alone, with no log index for the player: only what the file says)
            logged = idx is not None and self._log_truth
            base = (impact_snap[2].get(idx, 0), impact_snap[3].get(idx, 0),
                    impact_snap[6].get(idx, self.gspro.shot_number(idx))) if logged else (0, 0, 0)
            near = (hud.lie or "").lower() in ("", "green", "fringe")
            base_hole = impact_snap[1].get(idx)
            base_setup = setups0.get(idx, 0)
            # GSPro writes a holed stroke to its round file as the ball drops (BallInTheHole), but a short
            # chip or splash can still reach here without it (no watch of the file, or the stroke not yet
            # read), so it counts as near without `landed`, with a longer window: the hole-out reaches the
            # log a few seconds later. A chip that stays out resolves
            # as soon as GSPro sets the player up again, so the wait costs a miss almost nothing.
            short = (start_yards := to_yards(before.to_pin)) is not None and start_yards <= SHORT_GAME_YARDS
            watch = logged and near and (landed or short or hole_over())

            async def finish_yards(window: float) -> float | None:
                # GSPro's round file has where the ball stopped, with no wait on the log.
                if file_track is not None and file_track.finish_yards is not None:
                    return file_track.finish_yards
                if not logged:
                    return None
                return await self._wait_finish_yards(idx, impact_snap[0].get(idx), window)
            if file_track is not None:
                if not file_track.dropped and not file_track.picked_up:
                    await self._file_settle()   # (its drop may be the next rewrite's)
                file_track = self._file_track_of(player, None, impact, file_track.shot_id) or file_track
            if file_track is not None and file_track.picked_up:
                full_result = None   # the hole conceded at the maximum where the ball lay: not in
                log.info("%s full shot picked up short (GSPro round file)", tag)
            elif file_track is not None and file_track.dropped:
                # The ball dropped in GSPro's round file (holed, or inside the gimme circle).
                full_result = "holed" if file_track.in_cup else "conceded"
            else:
                full_result = await self._wait_putt_result(idx, *base, 1.5 if landed else 5.0,
                                                           base_hole=base_hole, base_setup=base_setup, fire=fire) \
                    if watch else "unknown"
            if full_result in ("holed", "conceded"):
                self._record_result(shot_id, full_result, tag, putt=False, rec=rec,
                                    left_yards=file_track.finish_yards if file_track is not None else None)
                log.info("%s full shot %s (GSPro %s, %.1fs after impact)", tag,
                         "holed out" if full_result == "holed" else "finished inside the gimme circle",
                         "round file" if file_track is not None and file_track.dropped else "log",
                         time.monotonic() - impact)
                score = max(score, c.big_moment_threshold if full_result == "holed" else c.interest_threshold)
            elif full_result == "unknown" and logged:
                self._bg(self._late_result(shot_id, idx, *base, tag, False, rec=rec, base_hole=base_hole,
                                           base_setup=base_setup, fire=fire))
            elif full_result == "unknown" and file_track is not None:
                self._file_end_due(file_track.shot_id, shot_id, tag, rec, False)   # (no log to say: the file may)
            picked_short = file_track is not None and file_track.picked_up
            if full_result == "conceded" and start_yards:
                # GSPro logs where the ball stopped (tdist) the line before the gimmie, so it's in by now:
                # a foot away nearly dropped, four feet is just a gimme.
                finish = await finish_yards(0.5)
                gimme_feet = finish * 3 if finish is not None else None
                if start_yards >= c.close_approach_from_yards:
                    long_gimme = True
                    gimme_level = approach_gimme_level(start_yards, gimme_feet)
                else:
                    # A chip or pitch to tap-in range. The log's "conceded" is proof enough: a short
                    # chip often ends the hole before its touchdown is seen.
                    gimme_level = short_game_gimme_level(start_yards, before.lie, gimme_feet)
                if gimme_level:
                    score = max(score, c.big_moment_threshold)
                    log.info("%s to the gimme from %.0f yards (%s), finished %s feet: excitement %s", tag,
                             start_yards, before.lie, "?" if gimme_feet is None else f"{gimme_feet:.1f}",
                             gimme_level)
            elif picked_short:
                pass   # (the hole picked up after it: the next setup is another hole's, no distance to read)
            elif near and landed and start_yards and start_yards >= c.close_approach_from_yards \
                    and full_result != "holed":
                finish = await finish_yards(1.5)
                if finish is not None and finish * 3 <= c.close_approach_feet:
                    close_feet = finish * 3
                    score = max(score, c.big_moment_threshold)
                    log.info("%s stuffed it: %.0f feet from the hole from %.0f yards (GSPro log)",
                             tag, close_feet, start_yards)
            if (hud.lie or "").lower() == "green" and close_feet is None and not picked_short \
                    and full_result not in ("holed", "conceded"):
                # On a green, but whose? GSPro's finishing distance says (a drive "found the green" 322
                # yards from the pin, then had "a putt for albatross").
                finish = await finish_yards(1.0)
                if finish is not None and finish > WRONG_GREEN_YARDS:
                    wrong_green = finish
                    score = max(score, c.interest_threshold)
                    if "found_green" in tags:
                        tags.remove("found_green")
                    tags.append("wrong_green")
                    log.info("%s finished on the wrong green, %.0f yards from the pin", tag, finish)

        # How the ball met the cup counts toward today's tally even when no reaction airs below.
        rec.cup = self.gspro.cup_of(fire)
        # The early splash (above) already aired and was the call for this shot; the shot has now been
        # recorded, so skip every further reaction.
        if splash_said:
            return
        if self._shot_seq != my_seq:
            # Another ball was struck while this one was followed to rest (a quick mulligan, the next
            # player): its call would land on top of the new shot, about one already in the past.
            log.info("%s reaction skipped: a newer shot was hit %.1fs after this one", tag, time.monotonic() - impact)
            return
        if self._banter_on():
            # An aside is long and paid for: only a big moment cuts it off (and not a held one, the
            # booth talking to the chat); a routine call waits for it.
            if score >= c.big_moment_threshold and not self._banter_held():
                log.info("%s a big moment (excitement %s): the call cuts the aside off", tag, score)
            else:
                waited = time.monotonic()
                aired_out = await self.speech.wait_banter(BANTER_WAIT_SECONDS)
                log.info("%s the call waited %.1fs for the aside to finish", tag, time.monotonic() - waited)
                if not aired_out:
                    log.info("%s reaction skipped: something else cut the aside off and took the air", tag)
                    return
                if self._shot_seq != my_seq:
                    log.info("%s reaction skipped: a newer shot was hit while the aside played", tag)
                    return
                if self._banter_held():
                    log.info("%s reaction skipped: the chat take held the air past the wait", tag)
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
            self._write_transcript([line], "reaction", impact_seq=my_seq, after=True)
            self.stats["reaction_planned"] = self.stats.get("reaction_planned", 0) + 1
            log.info("%s water splash reaction from the plan, audio %s, %.1fs after impact: %r", tag,
                     "ready" if audio is not None else "voiced now", time.monotonic() - impact, text)
            return

        notable = score >= c.interest_threshold
        # An ordinary full shot is a quiet moment a prepared golf-world aside can follow, once its call
        # (if any) is done. Not a putt: the next putt, or the hole's wrap, is right behind it.
        self._news_moment = {"seq": my_seq, "tags": [*tags, *self._round_moment_tags(r)],
                             "routine": not notable and not shot.is_putt}
        too_soon = time.time() - self.state.last_comment_at < c.min_seconds_between_comments
        # A makeable putt always gets a word (make or miss), and so does a long lag that stopped in the
        # gimme circle (its pre-voiced "gimme" line), skipping the routine dice but still yielding to
        # too_soon so it never talks over the last line.
        lag_gimme = bool(shot.is_putt and outcome == "conceded" and shot_plan
                         and shot_plan.player in (None, player) and shot_plan.reactions.get("gimme"))
        # A ball that lipped out or in, or hit the flagstick, always gets a word too.
        cup = rec.cup
        if cup:
            log.info("%s cup: %s (crossed %s mm from the centre)", tag, cup, self.gspro.fire_info(fire).get("cup_mm"))
        cup_putt = bool(shot.is_putt and cup in ("lip_out", "lip_in") and shot_plan
                        and shot_plan.player in (None, player) and shot_plan.reactions.get(cup))
        if not notable:
            if too_soon:
                return
            if self.rng.random() > c.routine_comment_rate and "makeable_putt" not in tags and not lag_gimme \
                    and not cup:
                return

        # Routine shots take the pre-written result line (voiced at setup, played now with no model
        # call), using the putt result resolved above. Notable moments still get the model reaction -
        # EXCEPT a holed putt, which always takes its instant pre-written "holed" line: a made putt is
        # the worst thing to react to late or miss, and a live call here can lag or drop.
        # An "unknown" putt (no log signal in the window) must NOT take the pre-written line: with no
        # result we can't say short vs holed, so fall through to the grounded live reaction instead.
        if c.pre_reactions and outcome != "unknown" \
                and (not notable or (shot.is_putt and holed) or lag_gimme or cup_putt) \
                and shot_plan \
                and time.monotonic() - shot_plan_made_at < 180 \
                and shot_plan.player in (None, player) and shot_plan.is_putt == shot.is_putt:
            picked = pick_reaction(shot_plan, after_lie=hud.lie, start_yards=to_yards(before.to_pin),
                                   roll_yards=roll_yards, holed=holed, conceded=outcome == "conceded", cup=cup)
            if picked:
                outcome, text = picked
                audio = self._play_reaction(shot_plan, outcome, text, impact_seq=my_seq)
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
            elif (ends := _conceded_hole_fact(before, shot_par)):
                result.append(ends)
        elif shot.is_putt and outcome == "holed":
            result = ["The putt went in the hole: holed."]
        elif shot.is_putt and outcome == "missed":
            result = ["The putt missed: it did not go in."]
        elif shot.is_putt:
            # No log result ("it drops for par" was guessed for a miss): say nothing either way.
            result = ["Whether the putt went in is not known: don't say it dropped, went in or missed. "
                      "Talk about the stroke, its pace or its line."]
        if full_result == "holed":
            start = to_yards(before.to_pin)
            chip = start is not None and start < c.close_approach_from_yards
            result = ["The ball went in the hole: holed out without a putt.",
                      _great_shot_fact(start, "went in the hole", before.shot_number, shot_par,
                                       lie=before.lie if chip else None,
                                       level=holed_out_level(start, before.shot_number, shot_par,
                                                             c.close_approach_from_yards))]
        elif full_result == "conceded":
            gimme = self.gspro.gimme_of(idx)
            circle = f"{gimme * 3:.0f}-foot " if gimme else ""
            where = f"finished {_feet_text(gimme_feet)} from the hole, inside the {circle}gimme circle" \
                if gimme_feet is not None else f"finished inside the {circle}gimme circle"
            result = [f"The ball stopped inside the {circle}gimme circle, right by the hole: the putt is "
                      f"conceded, so the hole is over without a putt being struck."]
            if gimme_level:
                strokes = before.shot_number + 1 if before.shot_number else None
                result.append(_great_shot_fact(to_yards(before.to_pin), where, strokes, shot_par,
                                               lie=None if long_gimme else before.lie, level=gimme_level))
            elif gimme_feet is not None:
                result.append(f"It {where}.")
        if cup and (fact := _CUP_FACTS.get((shot.is_putt, cup))):
            result = result + [fact]
        info = self.gspro.fire_info(fire)
        if not shot.is_putt and (fact := lie_cost_fact(MATERIAL_TO_LIE.get(info.get("penalty") or "", "rough"),
                                                       info.get("lie_cost"))):
            result = result + [fact]
        # On the green: say what the next putt is for, so "ten feet for birdie" is a fact, not a guess
        # (a par 5 in two is an eagle putt), and on in three on a par 4 it's a par putt, not a birdie one.
        next_putt = None
        if before.shot_number and shot_par and (name := SCORE_NAMES.get(before.shot_number + 1 - shot_par)):
            next_putt = (f"The ball is on the green in {before.shot_number}: the next putt "
                         f"is for {name.split(' ', 1)[1]}.")
        picked = file_track is not None and file_track.picked_up
        if close_feet is not None:
            result = result + [_great_shot_fact(to_yards(before.to_pin),
                                                f"finished {close_feet:.0f} feet from the hole",
                                                level=stuffed_level(close_feet))]
        elif not shot.is_putt and picked:
            # (the next setup is another hole's: no distance left to wait for)
            result = result + ["The ball did not go in. The player has picked up after this shot: the hole "
                               "is over, scored at the maximum the game allows."]
        elif not shot.is_putt and full_result not in ("holed", "conceded"):
            # GSPro's own finishing distance (logged as the ball settles), so the model never reads the
            # starting distance as what's left: "still 102 yards" after a pitch to 8 yards.
            left = file_track.finish_yards if file_track is not None and not file_track.penalty else None
            if left is None and idx is not None and self._log_truth:
                left = await self._wait_finish_yards(idx, impact_snap[0].get(idx), 1.0)
            if left is not None and wrong_green is None and left > WRONG_GREEN_YARDS \
                    and (hud.lie or "").lower() == "green":
                wrong_green = left   # the tdist came too late for the check above: still no birdie putt
                if "found_green" in tags:
                    tags.remove("found_green")
                tags.append("wrong_green")
            if left is not None:
                on_green = (hud.lie or "").lower() in ("green", "fringe")
                where = f"{left * 3:.0f} feet" if on_green or left < 10 else f"{left:.0f} yards"
                result = result + [f"After this shot the ball is {where} from the pin."]
            else:
                result = result + ["How far the ball is from the pin now is not known: don't state a distance "
                                   "left, and don't repeat the starting distance as if it were."]
        if wrong_green is not None:
            result = [wrong_green_fact(wrong_green)]
        elif next_putt and not shot.is_putt and not picked and full_result not in ("holed", "conceded") and \
                (close_feet is not None or (hud.lie or "").lower() == "green"):
            result = result + [next_putt]
        if off_wrong_green is not None:
            result = [wrong_green_fact(off_wrong_green, played_off=True)] + result
        if chip_off_green:
            result = [green_chip_fact(club_name(shot.club))] + result
        # No in-the-moment "distance from the pin": the only estimate available (start distance minus
        # how far the ball travelled, from a tile that can read stale) was unreliable enough to call a
        # ball in a greenside bunker "three yards, beautifully done". The lie (describe) is read
        # accurately; crediting a stuffed shot belongs on the next setup's exact distance instead.
        chip = is_chip(shot, before.to_pin)
        flight = shot_facts(shot, chip=chip)
        if "mishit" in tags:
            flight.append("The strike was a mishit: well short of what this club normally goes.")
        if "carry (game)" in hud.tiles and not shot.is_putt:
            # GSPro's carry accounts for elevation and conditions; two different carries confuse the model.
            flight = [f for f in flight if not f.startswith("Carry:")]
        # Club delivery (angle of attack, face, path) is colour, not every-shot material - on every
        # shot it gets repetitive. Feed it only when the delivery is actually standout, or now and then
        # on an ordinary one, so it stays a spice.
        club = club_delivery_facts(shot) if use_delivery and not chip else []
        self._delivery_last = bool(club)
        if not shot.is_putt:
            if self._is_drive(before):
                club += self._altitude_facts()
            club += self._length_norm_facts(
                player, shot.club, self._is_drive(before), to_yards(before.to_pin),
                tile_yards("total length") if landed and "total length" in hud.tiles else None,
                lie=before.lie)
            club += self._reach_facts(player, before.lie, before.to_pin, self.gspro.aim_of(fires0, impact),
                                      "green" if full_result in ("holed", "conceded") else hud.lie,
                                      before.elevation_yards)
        sand_theme = (self._bunker_facts(player) if (hud.lie or "").lower() in ("sand", "bunker")
                      and not shot.is_putt else [])
        facts = (lead + replay_facts + flight + club + result + sand_theme
                 + history_facts(self._display(player), prior, chip=chip)
                 + moment_facts(self._display(player), [x for x in prior if r and x.round_id == r.id], rec)
                 + season_facts
                 + self._format_facts(before.shot_number, player=player) + (self._ctp_facts(1) if hud.shot_number == 1 else []))
        who, team = self._who(player)
        user, facts_text = shot_prompt(
            player=who,
            team=team,
            facts=facts,
            hole=shot_hole,
            recent_lines=self.state.recent_lines,
            routine=not notable,
            has_images=False,
            angle=self.rng.choice(ANGLES if not self.round or self.round.is_league else PRACTICE_ANGLES),
            big=score >= c.big_moment_threshold,
        )
        lines = await self._compose("shot", self.live_llm, user, facts_text, [], must=must)
        if self._shot_seq != my_seq:
            log.info("%s reaction dropped: a newer shot was hit while it was written", tag)
            return
        if lines and self._banter_held():
            # A held aside ran past the wait: the call goes on after it, not over it.
            await self.speech.wait_banter(BANTER_WAIT_SECONDS)
            if self._shot_seq != my_seq or self._banter_held():
                log.info("%s reaction dropped: the chat take held the air", tag)
                return
        if lines and self._banter_on():
            # Only a big moment (or an aside past its wait) gets here with the aside still on air.
            self.speech.interrupt()
        if penalty_facts and lines:
            self._hook_penalty_call(lines, self.gspro.fire_info(fire))
        if score >= c.big_moment_threshold:
            for line in lines:
                line._excited = True   # a voice with a big-moment style uses it (tts.azure_styles)
        self._air_lines("shot", lines, shot_id)
