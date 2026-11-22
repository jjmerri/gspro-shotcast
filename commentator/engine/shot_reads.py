"""What's read around a shot: carry, the finish, trees, the lie, and which hole it was on.

Part of Engine (commentator/engine/core.py), which is these parts together."""
import asyncio
import bisect
import re
import time

from .common import _gspro_name, _setup_key, log
from .. import round_file
from ..carry_model import correct_for_elevation
from ..facts import to_yards
from ..flight import point_at
from ..hud import Hud
from ..models import Line, Shot
from ..sources.gspro_log import MATERIAL_TO_LIE, M_TO_YD


class ShotReadsMixin:
    """What's read around a shot: carry, the finish, trees, the lie, and which hole it was on."""

    def _hook_penalty_call(self, lines: list[Line], info: dict) -> None:
        """The shot's penalty (_penalty_shot_facts) counts as told once a reaction line saying it is on
        air: one dropped or never voiced leaves it for the next setup's line."""
        idx, hole = info.get("player"), info.get("hole")
        said = next((l for l in lines if re.search(r"penalt|out of bounds|\bOB\b|lost ball|\bdrop", l.text, re.I)), None)
        if idx is None or hole is None or said is None:
            return

        def told(key=(idx, hole), n=self.gspro.penalties_on(idx, hole)) -> None:
            self._pen_called[key] = max(self._pen_called.get(key, 0), n)
        said._on_air = told

    def _trail_carry(self, player: str | None, entry: tuple) -> None:
        """Add a stroke to the player's trail in shot order, and teach the carry model from every full
        shot that now has its next stroke from the same ball; the rest only get the near-pin rule."""
        trail = self._carry_trail.setdefault(player or "unknown", [])
        bisect.insort(trail, list(entry), key=lambda e: e[0])
        # Every shot whose next stroke is in now learns from it. Strokes stay a while after (learned or
        # not) as a slower handler for the stroke before may still need them; a shot whose next stroke
        # never shows (a mulligan, a penalty, an unread stroke number) gets the fallback once it ages out.
        for e, nxt in zip(trail, trail[1:]):
            if e[4] and self._next_stroke(e[1], nxt[1]):
                self._learn_carry(*e[1:4], e[4], *(nxt[1:3] if self._own_ball_next(e[5], nxt[5]) else ()))
                e[4] = None
        while len(trail) > self.CARRY_TRAIL:
            _, key, elev, pin, args, _other = trail.pop(0)
            if args:
                self._learn_carry(key, elev, pin, args)

    CARRY_TRAIL = 4

    @staticmethod
    def _own_ball_next(ball: tuple, next_ball: tuple) -> bool:
        """The next stroke was played from this shot's ball. Each is (shared-ball format?, balls GSPro
        has dropped for the player so far, None if unknown). Either stroke may be the first to show a
        team format, so a shared one needs both counts known and equal: no partner's ball was copied
        onto the player in between (their own was the one picked)."""
        if not ball[0] and not next_ball[0]:
            return True
        return ball[1] is not None and ball[1] == next_ball[1]

    @staticmethod
    def _next_stroke(key: tuple, next_key: tuple | None) -> bool:
        """`next_key` (round, hole, shot number) is the stroke right after `key`'s."""
        return next_key is not None and key[0] == next_key[0] and key[1] == next_key[1] \
            and key[2] is not None and next_key[2] == key[2] + 1

    def _learn_carry(self, key: tuple, elevation: float | None, pin_yards: float | None, args: tuple,
                     next_key: tuple | None = None, next_elevation: float | None = None) -> None:
        """Teach the carry model one shot, its CARRY (game) put level with where it was hit. The next
        stroke (same round and hole, one shot on) says how high the ball came to rest; without it,
        only a shot that landed near the pin can be leveled (see correct_for_elevation)."""
        lie, carry, game, course, vla, base = args
        rest_rise = None
        if elevation is not None and next_elevation is not None and self._next_stroke(key, next_key):
            rest_rise = elevation - next_elevation
        level = correct_for_elevation(game, elevation, pin_yards, carry, rest_rise)
        if level is not None:
            self.carry.observe(lie, carry, level, course, vla, base)

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
        is used rather than live state because the wait for the ball at rest has since advanced the log another shot."""
        if idx is None or idx not in self._pending_finish:
            return
        prev_id, before = self._pending_finish.pop(idx)
        after = self._idx_snap(after_snap, idx)
        if after["dropped"] > before["dropped"]:
            return   # a scramble picked another ball: this one's resting spot was never logged
        if before["hole"] is None:
            return   # the shot's hole was unknown (log not tailed yet); can't compare, so don't guess
        hud, logged = before.get("hud_yards"), before["to_pin_m"]
        if hud is not None and logged is not None and abs(logged * M_TO_YD - hud) > max(5.0, 0.2 * hud):
            # The log hadn't caught up to this shot: on a new tee it can still show the last hole, ball
            # by the cup, so that hole's hole-out landing next would read as this shot holing out.
            return
        if after["hole"] != before["hole"]:              # the shot ended the hole
            if after["outs"] > before["outs"]:
                self.store.set_shot_result(prev_id, finish_yards=0.0)                  # holed
            elif after["gim"] > before["gim"]:
                self.store.set_shot_result(prev_id, finish_yards=self._gimme_left(prev_id)
                                           or self.gspro.gimme_of(idx))   # conceded
            return
        if after["pen"] > before["pen"]:   # same hole, a penalty on this shot: the next setup is the drop
            return
        if after["to_pin_m"] is not None:
            self._log_finish[prev_id] = after["to_pin_m"] * M_TO_YD
            self.store.set_shot_result(prev_id, finish_yards=after["to_pin_m"] * M_TO_YD)

    async def _log_shooter(self, fires0: int, impact: float, window: float,
                           poll: float = 0.1) -> tuple[int, int] | None:
        """The slot GSPro's log credits the shot that hit at `impact` to (its shooter's score line, logged
        as the ball stops), with the shot's "Shot Fired" number, waited on up to `window` after `impact`;
        None with no log to go by."""
        if not self._log_truth or self.gspro.mode != "course":
            return None
        deadline = impact + window
        while True:
            fire = self.gspro.fire_of(fires0, impact)
            if (p := self.gspro.fire_info(fire).get("player")) is not None:
                return p, fire
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(poll)

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

    async def _kick_facts(self, fire: int | None, lie: str | None, is_putt: bool, wait: float = 0.5,
                          wood_tree: bool = False) -> list[str]:
        """A fact when the ball came down on the cart path (or a man-made structure) and bounced on to
        rest somewhere else. GSPro logs the landing surface as the ball settles, so give it a moment."""
        if is_putt or fire is None or not lie:
            return []
        deadline = time.monotonic() + wait
        while "landed" not in self.gspro.fire_info(fire) and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        landed = MATERIAL_TO_LIE.get(self.gspro.fire_info(fire).get("landed") or "")
        if landed not in ("cart path", "structure") or landed == lie.lower() or wood_tree:
            return []   # wood that was a tree is _tree_facts' to call
        what = "the cart path" if landed == "cart path" else "something man-made (a bridge, steps or a wall)"
        return [f"The ball landed on {what} and kicked off it, finishing in the {lie}. That bounce is "
                f"worth a word."]

    async def _penalty_shot_facts(self, fire: int | None, shot: Shot, tags: list[str],
                                  wait: float = 0.6, track: round_file.Track | None = None
                                  ) -> tuple[list[str], str | None]:
        """The shot's own penalty, when GSPro scored it as the ball stopped: 4.0 logs a dry penalty (out
        of bounds, a ball that ran into a penalty area) right after the shooter's score line, where 3.1
        waited for the next player, so the shot's call can say it. A splash is the water call's to make.
        Returns (facts, tag): "out_of_bounds", or "in_hazard" for a ball GSPro saw run into a penalty
        area. Told here, the next setup's penalty fact only counts the stroke (_penalty_facts_from_truth).
        With the shot's stroke from GSPro's round file (`track`), its penalty entry says it with no wait."""
        if shot.is_putt or "in_water" in tags or (fire is None and track is None):
            return [], None
        info = self.gspro.fire_info(fire) if fire is not None else {}
        if track is not None and track.penalty_kind:
            # (the file says water, not how the ball got there: a splash or a roll in; the log's count stands)
            info = {**info, "penalty_strokes": info.get("penalty_strokes") or 1,
                    "hazard": (info.get("hazard") or "file") if track.penalty_kind == "water" else False}
        if "rest_at" in info and "penalty_strokes" not in info:
            deadline = info["rest_at"] + wait   # it comes with the rest: no wait once that's past
            while "penalty_strokes" not in info and time.monotonic() < deadline:
                await asyncio.sleep(0.1)
                info = self.gspro.fire_info(fire)
        n = info.get("penalty_strokes")
        if not n:
            return [], None
        strokes = f"{'a' if n == 1 else n} penalty stroke{'s' if n > 1 else ''}"
        if info.get("hazard") == "file":
            return [f"The ball finished in a penalty area: GSPro has added {strokes}. That's worth a word."], \
                "in_hazard"
        if info.get("hazard"):
            return [f"The ball landed dry and then ran on into the penalty area: GSPro has added {strokes}. "
                    f"That's worth a word."], "in_hazard"
        return [f"GSPro has called this ball out of bounds: {strokes}, and the next shot is played again "
                f"from where this one was hit. That's worth a word: call it out of bounds."], "out_of_bounds"

    def _tree_facts(self, fire: int | None, shot: Shot, impact: float, tag: str = "",
                    wood_tree: bool = False) -> list[str]:
        """Facts when GSPro's log had the ball hit a tree: foliage in flight, or the wood of a trunk or
        branch (`wood_tree`, see GsproState.tree_wood). GSPro 3.59+ logs a bush as one; otherwise the
        height there (modelled from the launch numbers) tells a tree from a bush."""
        if shot.is_putt:
            return []
        info = self.gspro.fire_info(fire)
        hits = info.get("leaves") or []
        facts = self._foliage_fact(hits, info, shot, impact, tag) if hits else []
        if wood_tree:
            log.info("%s hit wood short of its carry (descent angle %s): a tree, not a structure", tag,
                     info.get("descent"))
            facts.append(f"The ball {'then ' if hits else ''}struck the wood of a tree (the trunk or a thick "
                         f"branch) and bounced off it, so a tree, not the swing, cut this shot short. "
                         f"Worth a word. It was a tree: never call it a bridge, a wall or a cart path.")
        return facts

    def _foliage_fact(self, hits: list, info: dict, shot: Shot, impact: float, tag: str) -> list[str]:
        t, _, _, _ = hits[0]
        lost = hits[-1][3]
        # Only the first hit's height is modelled: the ball flew on slowed after it.
        p = point_at(shot.ball_speed, shot.vla, shot.total_spin, shot.spin_axis, self._raw_yards(shot), t)
        log.info("%s hit foliage %s time(s) (logged %.1fs after impact): first at %.2fs into the flight, "
                 "~%s ft up and %s yd out by the model, %.0f%% of its speed lost%s", tag, len(hits),
                 info["leaves_at"] - impact if "leaves_at" in info else float("nan"), t,
                 f"{p.height_ft:.0f}" if p else "?", f"{p.out_yd:.0f}" if p else "?", lost,
                 " (past the modelled landing)" if p and p.landed else "")
        times = "once" if len(hits) == 1 else f"{len(hits)} times"
        if info.get("bush"):
            where = "a bush" + (f" about {p.out_yd:.0f} yards out" if p and not p.landed else "")
            return [f"The ball hit foliage {times} in flight: {where} (GSPro itself says it was a bush, not a "
                    f"tree). It cost the ball about {lost:.0f}% of its speed, so the bush, not the swing, cut "
                    f"this shot short. Worth a word. Call it a bush, never a tree or a branch; any distance "
                    f"is an estimate, keep it loose."]
        if p is None:
            where = "somewhere along the way"
        elif p.landed or p.height_ft < 10:
            where = (f"low to the ground about {p.out_yd:.0f} yards out, so a bush or the lowest branches "
                     "of a tree" if not p.landed else
                     f"as it came down around {p.out_yd:.0f} yards out, into the trees or bushes there")
        elif p.height_ft < 20:
            # A big bush reads ~11 ft by the model (a test on the sim): this band can't tell them apart.
            where = (f"about {p.height_ft:.0f} feet up and {p.out_yd:.0f} yards out, so a big bush or the low "
                     "branches of a tree")
        elif p.height_ft < 30:
            where = f"in the branches of a tree, roughly {p.height_ft:.0f} feet up and {p.out_yd:.0f} yards out"
        else:
            where = f"high up in a tree, roughly {p.height_ft:.0f} feet up and {p.out_yd:.0f} yards out"
        return [f"The ball hit foliage {times} in flight: {where}, by our estimate. It cost the ball about "
                f"{lost:.0f}% of its speed, so the trees, not the swing, cut this shot short. Worth a word "
                f"(an unlucky branch, a tree that jumped out). The height and distance are estimates: keep "
                f"them loose, never exact."]

    def _setup_at_impact(self, at: float) -> Hud | None:
        """This stroke's setup read, taken at impact: the last setup, when no ball was struck since it,
        it was read before `at`, and GSPro's log (when it names one) still has the same player up."""
        if self._last_setup is None:
            return None
        hud, seq, active, read_at = self._last_setup
        if seq != self._shot_seq or read_at > at:
            return None
        now = self.gspro.active_player
        if active is not None and now is not None and active != now:
            return None
        return hud

    def _keep_impact_misread(self, before: Hud, is_putt: bool, tag: str = "") -> None:
        """Leave the boxes behind for a problem report when the impact read came up wrong: the player
        box hidden or unreadable, a name that's nobody's, or no shot number or distance in it."""
        if before.crops is None:
            return
        if not before.player_text:
            reason = "impact-empty"
        elif not self._player(before.player_text):
            reason = "impact-junk-player"
        elif before.shot_number is None:
            reason = "impact-no-shot"
        elif before.to_pin is None:
            reason = "impact-no-pin"
        else:
            return
        self._keep_report_frame(reason, crops=before.crops, shot=tag, hud_text=before.player_text,
                                shot_number=before.shot_number, to_pin=before.to_pin, putt=is_putt,
                                read_seconds=self.screen.last_read_seconds if self.screen else None)

    def _fill_from_setup(self, before: Hud, setup: Hud | None, tag: str = "") -> bool:
        """The impact read missed the player box (GSPro can hide it for the flight before a slow read
        grabs it): who hit this, which stroke, and from where come from the setup read for it. A read
        with no shot number missed it too: OCR of the flight's backdrop ('诚', '5') isn't a player box,
        and a stroke left unnumbered breaks its hole's replay. True when the setup read was used."""
        if setup is None or not setup.player_text or (before.player_text and before.shot_number is not None):
            return False
        log.info("%s the impact read missed the player box%s: %r shot %s from the setup read", tag,
                 f" (read {before.player_text!r})" if before.player_text else "",
                 setup.player_text, setup.shot_number)
        # GSPro's own number: _count_added_penalty adds what it left out, once, to this read.
        before.player_text, before.shot_number = setup.player_text, setup.gspro_shot or setup.shot_number
        before.to_pin, before.score_to_par = setup.to_pin, setup.score_to_par
        if before.elevation_yards is None:
            before.elevation_yards = setup.elevation_yards
        if not before.lie:
            before.lie = setup.lie
        if not before.handed:
            before.handed = setup.handed
        return True

    def _number_from_log(self, before: Hud, player: str | None, impact_snap: tuple, tag: str = "") -> None:
        """No shot number from the screen at impact or the setup read for it (a quick chip, hit while the
        setup read was still retrying): GSPro's log counted this player's stroke at impact. A stroke
        left unnumbered breaks its hole's replay line. Not in a scramble: its HUD counts the team's. Only
        when the log had the player on this hole: it moves to a new hole only once its tee shot is logged,
        so at a tee it still counts the hole before."""
        r = self.round
        if not self._log_truth or (r and r.format == "scramble" and not r.no_scramble):
            return
        if (idx := self._gspro_idx(player)) is None or (n := impact_snap[6].get(idx)) is None:
            return
        if self.hole_number is None or (hole := impact_snap[1].get(idx)) is None or hole + 1 != self.hole_number:
            return
        if self._log_hole_finished(idx, hole, impact_snap):
            return   # the log's count is the finished hole's: this is the next tee, the booth not there yet
        log.info("%s shot %s from GSPro's log (unread on screen)", tag, n)
        before.shot_number = n

    def _finished_holes(self) -> frozenset:
        """The (player, 0-based hole)s GSPro's log has finished: scored or picked up, and in a scramble
        each teammate's too (the log scores the team's hole on one of them; a log's teams before the
        round is known are taken as a scramble's)."""
        g, r = self.gspro, self.round
        done = {*g.hole_scores, *g._picked}
        teams: list[set[int]] = []   # the log's pairs joined into teams (A-B and A-C: one team)
        # (Not a shamble's: its partners share the drive, then each finish with their own ball.)
        for pair in g.teammates if r is None or (r.format == "scramble" and not r.no_scramble) else ():
            hit = [t for t in teams if t & pair]
            teams = [t for t in teams if t not in hit] + [set(pair).union(*hit)]
        return frozenset(done | {(j, h) for i, h in done for t in teams if i in t for j in t})

    def _log_hole_finished(self, idx: int, hole: int, impact_snap: tuple) -> bool:
        """Whether GSPro's log had the player done with this (0-based) hole at impact: scored, or picked
        up. Its hole moves on only with the next tee shot, so until then its stroke count is the
        finished hole's. (At impact: a stroke that holes out is scored before its HUD read is done.)"""
        return (idx, hole) in (impact_snap[10] if len(impact_snap) > 10 else self._finished_holes())

    def _misread_shot_one(self, before: Hud, player: str | None, impact_snap: tuple, lies0: dict,
                          hole_label: str | None, tag: str = "") -> tuple[str, str | None] | None:
        """An impact read of "shot 1" away from the tee, where GSPro's log (as it stood at impact) has
        the player further on this hole: the strip's boxed digit misread (Dan's third at Sand Valley's
        2nd, a lob wedge from 19.6 yards, read "1", then recorded off the tee and called "for eagle").
        The log's count wins; in a scramble, the stroke after the team's picked ball."""
        if before.shot_number != 1 or self._at_tee_length(before, hole_label) is not False:
            return
        idx = self._gspro_idx(player)
        if (not self._log_truth or idx is None or self.hole_number is None
                or (hole := impact_snap[1].get(idx)) is None or hole + 1 != self.hole_number):
            return
        if self._log_hole_finished(idx, hole, impact_snap):
            # Steve at Brasstown's 17th, its banner's length unread so the booth was still on 16: GSPro
            # had scored his 16th, so its "stroke 5" there wasn't this one. The HUD's 1 is the tee shot.
            return
        r = self.round
        if r and r.format == "scramble" and not r.no_scramble:
            pick, stroke = impact_snap[9] if len(impact_snap) > 9 else (None, None)
            ours = pick and (pick[0] == idx or frozenset((pick[0], idx)) in self.gspro.teammates)   # not a rival team's
            n = (stroke or 0) + 1 if ours and pick[1] == hole else 0
        else:
            n = impact_snap[6].get(idx) or 0
        if n <= 1:
            return
        log.info("%s shot %s, not the HUD's 1: %s from the pin isn't the tee", tag, n, before.to_pin)
        before.shot_number = n
        if (before.lie or "").lower() == "tee":
            fallback = MATERIAL_TO_LIE.get(lies0.get(idx)) or before.lie
            before.lie = self._file_rest_lie(player, n) or fallback
            if before.lie != fallback:
                return before.lie, fallback   # (GSPro's file's: the lie without it, should it be another round's)
        return None

    def _use_setup_lie(self, before: Hud, tag: str = "") -> None:
        """The lie this stroke's setup read, while the player stood over the ball, over the impact
        read's: that comes ~3 s after the swing, when the lie box may show where the ball landed."""
        if (setup_lie := self._setup_lies.get(_setup_key(before))) and setup_lie != before.lie:
            log.info("%s lie %s at setup (the impact read, after the swing, said %s)", tag, setup_lie, before.lie)
            before.lie = setup_lie
        elif not before.lie and (log_lie := self._setup_log_lies.get(_setup_key(before))):
            log.info("%s lie %s from the GSPro log at setup (unread on screen)", tag, log_lie)
            before.lie = log_lie
        # The slope box is read only at setup (the impact read skips it): carry it over for the record.
        if (slope := self._setup_slopes.get(_setup_key(before))) and before.slope_updown is None \
                and not before.slope_side:
            before.slope_updown, before.slope_side, before.slope_side_degrees = slope

    def _set_up_in_yards(self, before: Hud) -> bool:
        """Whether this stroke was set up off the green: GSPro shows the distance in yards there and in
        feet on the green. Only that way round: switching to putt mode off the green shows feet too."""
        key = _setup_key(before)
        return bool(key and ("yard" in key[2] or self._setup_yards.get(key[:2])))

    def _off_green_lie(self, before: Hud) -> str:
        """The lie for a stroke set up off the green that the reads put on it: the HUD's, when it's a
        lie GSPro played it from (no rough or sand without GSPro's penalty for it), else the fringe."""
        return before.lie if before.lie not in (None, "", "green", *self._PENALIZING_LIES) else "fringe"

    @staticmethod
    def _at_tee_length(before: Hud, hole_label: str | None) -> bool | None:
        """Whether the distance to the pin is the hole's full length (a ball on the tee), or None when
        either distance is unread."""
        m = re.search(r"\d+(?:\.\d+)? (?:yards|metres|meters)", hole_label or "")
        yd, length = to_yards(before.to_pin), to_yards(m.group(0)) if m else None
        if yd is None or not length:
            return None
        return abs(yd - length) <= 10

    def _played_from(self, before: Hud, shot: Shot, info: dict, material: str | None,
                     hole_label: str | None = None, file_lie: str | None = None) -> str | None:
        """The lie this shot was really struck from, for the record. GSPro's round file says it outright
        (`file_lie`, the stroke's StartingSurface).
        Without it, the HUD's lie read and the log's material (where the previous shot LANDED, not where
        it stopped) both misread: a ball that landed on the green and rolled into a bunker, a bunker ball
        shown as rough. GSPro's own word wins: the first stroke on a hole is off the tee, and a shot struck
        from a surface that costs it speed or spin is logged with that surface. When this GSPro logs
        those and logged none for this shot, it wasn't played from rough or sand, whatever the other
        reads said."""
        if file_lie:
            return file_lie
        if before.shot_number == 1:
            return "tee"
        if (before.lie or "").lower() == "tee" and not shot.is_putt and self._at_tee_length(before, hole_label):
            return "tee"   # a re-tee (stroke and distance): the HUD's Tee, at the hole's full length
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

    def _banner_past(self, hud: Hud, hole: int) -> bool:
        """Whether a banner is plainly a later hole than `hole`: its own number says so, or the course
        card's par for `hole` isn't the banner's."""
        if hud.hole_number:
            return hud.hole_number > hole
        card = self.round and self.round.course_card
        return bool(hud.par and card and card.par and card.par.get(hole) not in (None, hud.par))

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
        raw = _gspro_name(hud.player_text or "")
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
