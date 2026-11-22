"""The pre-shot: the setup line, the plan for the shot's flight, and the reactions prepared for it.

Part of Engine (commentator/engine/core.py), which is these parts together."""
import asyncio
import json
import re
import time
from typing import Any

from .common import PRESHOT_IDLE_WAIT_SECONDS, _names_other_scenery, _setup_key, log
from ..facts import (
    CHIP_YARDS, PUTT_COUNT_FACT, PUTT_FOR_FACT, _PUTT_WORDS, gimme_edge, history_facts, moment_facts, names_club,
    preshot_facts, putt_elevation_inches, to_yards, wrong_conceded_score, wrong_green_yards, wrong_holed_putt,
    wrong_putt_score,
)
from ..flight_pick import FlightPlan, Hazard
from ..grounding import repeats, wrong_round_score
from ..hud import Hud
from ..interest import REPLY_PUTT_LEVEL, holed_putt_level, putt_to_par
from ..minimap import MinimapRead
from ..models import Line, PreShotEvent
from ..prompts import (
    FLIGHT_OUTCOMES, parse_lines, preshot_branch_prompt, preshot_branch_schema, preshot_hazard_prompt,
    preshot_hazard_schema, preshot_line_prompt, preshot_line_schema, reaction_outcomes,
)
from ..putt_read import PuttRead


class PreshotMixin:
    """The pre-shot: the setup line, the plan for the shot's flight, and the reactions prepared for it."""

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
        self._fix_stale_pick_hud(hud, idx)
        # The log's material is where the ball first LANDED, not where it came to rest (a drive that
        # kicked off the cart path into the rough logs "concrete"; across a season's log, a fairway
        # landing was played from the rough about 1 time in 12), so the HUD's resting lie wins and the
        # log only fills in a lie OCR missed. Never on the tee (the HUD's shot 1, or a tee lie), where
        # the log can still hold the previous hole's ball: its "green" made a par-3 tee shot a putt.
        on_tee = hud.shot_number == 1 or (hud.lie or "").lower() == "tee"
        if idx is not None and not on_tee and not hud.lie and g.shot_number(idx) > 1 and (lie := g.lie_of(idx)):
            hud.lie = lie
            if (src := getattr(hud, "_lie_source", None)) and src[0] == "none":
                log.info("Setup lie %s from GSPro's log (its landing material; the round file: %s)", lie, src[1])
                hud._lie_source = ("GSPro's log", src[1])
            if key := _setup_key(hud):   # for the impact too, whose own read may miss it again
                self._setup_log_lies[key] = lie
                while len(self._setup_log_lies) > 8:
                    del self._setup_log_lies[next(iter(self._setup_log_lies))]
        on_green = (hud.lie or "").lower() == "green"
        # A ball on another hole's green is no putt: it'll be chipped or hit back toward this hole.
        is_putt = on_green and not wrong_green_yards(hud.lie, hud.to_pin)
        if on_green and not is_putt:
            hud._wrong_green = True
            log.info("Wrong green: %r is on a green %s from this hole's pin", self._display(player), hud.to_pin)
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
            if idx is not None and not on_green and (tp := g.to_pin_of(idx)) is not None:
                # Trust the log's measured distance, but if it disagrees wildly with what's on screen
                # for the player at address, keep the OCR reading: a big gap means the per-player slot
                # was mis-attributed (grabbing the partner's distance in a multiplayer round).
                ocr_pin = to_yards(hud.to_pin)
                if ocr_pin is None or abs(tp - ocr_pin) <= 20:
                    hud.to_pin = f"{tp:.1f} yards"
                else:
                    # On a tee the log can still hold the last hole's ball, and mid-hole the last spot
                    # (Evan's 302.1 yards after his 3-wood from there): expected, not a warning.
                    prev = self.state.player_shots(player or "unknown")
                    stale = on_tee or bool(prev) and (was := to_yards(prev[-1].to_pin_before)) is not None \
                        and abs(was - tp) <= 0.5
                    (log.debug if on_tee else log.info if stale else log.warning)(
                        "Log to_pin %.1f yd diverges from HUD %.1f yd for idx %s (%r); keeping HUD (%s)", tp,
                        ocr_pin, idx, self._display(player),
                        "the log still has the last spot" if stale else "likely a mis-attributed slot")
        minimap = await self._await_minimap(minimap_task, is_putt)
        if minimap and (entry := getattr(hud, "_chat_setup", None)) and (trouble := [
                f.removeprefix("Hole layout: ").rstrip(".") for f in minimap.facts(to_yards(hud.to_pin), hud.lie)
                if f.startswith("Hole layout: ")][:4]):
            entry[1] += "; trouble: " + "; ".join(trouble)   # what a viewer's warning may be about
        if minimap and minimap.stymied(to_yards(hud.to_pin)):
            hud._stymie = True   # a tree on the line always gets its setup line
            self._stymie_seq = self._shot_seq   # (and the live flight call, at impact, still knows it)
        if is_putt and putt_task is None and self.putt_reader:
            putt_task = self._start_putt_read()   # a putt the distance didn't give away (metres)
        putt = await self._await_putt(putt_task, is_putt)
        self._use_own_scores(hud, player)
        facts = (preshot_facts(hud, self.current_par, self.handed.get(player or ""))
                 + self._mulligan_facts(player, hud) + self._putt_count_facts(player, is_putt, hud)
                 + self._penalty_facts(player, hud.shot_number, hud) + self._truth_context_facts(is_putt, idx)
                 + history_facts(self._display(player), self.state.player_shots(player or "unknown"),
                                 chip=not is_putt and (to_yards(hud.to_pin) or 99) <= CHIP_YARDS)
                 + moment_facts(self._display(player), self._round_shots(player), putt=is_putt,
                                at_edge=idx is not None and gimme_edge(self.gspro.to_pin_of(idx), self.gspro.gimme_of(idx)))
                 + self._format_facts(hud.shot_number, setup=True, player=player) + self._ctp_facts(hud.shot_number, hud, player)
                 + self._profile_facts(player, is_putt, hud) + self._prediction_facts(player, is_putt, hud)
                 + self._stakes_facts(hud, player) + self._team_score_facts(player)
                 + self._finish_correction_facts(hud, player)
                 + self._round_form_facts(player, is_putt, hud) + self._recall_moment_facts(player, is_putt, hud)
                 + ([] if is_putt else self._length_norm_facts(player, hud.club, self._is_drive(hud),
                                                                       to_yards(hud.to_pin), lie=hud.lie))
                 + (self._bunker_facts(player) if (hud.lie or "").lower() in ("sand", "bunker")
                    and self.rng.random() < 0.5 else [])
                 + (self._hole_field_facts(player) + self._streak_facts(player) + self._signature_facts(player)
                    + self._hole_recall_facts(player)
                    if hud.shot_number == 1 else [])
                 + (minimap.facts(to_yards(hud.to_pin), hud.lie) if minimap else [])
                 + (["The tree on the line to the pin is the story of this shot: say how it blocks the way."]
                    if getattr(hud, "_stymie", False) else [])
                 + self._strategy_facts(minimap, hud)
                 + (putt.facts(self.cfg.putt_read, slope=putt_elevation_inches(hud) is None) if putt else []))
        who, team = self._who(player)
        second = self.speakers[1] if len(self.speakers) > 1 else None
        lag_feet = self.cfg.commentary.gimme_lag_feet
        lag = is_putt and lag_feet > 0 and (to_yards(hud.to_pin) or 0) * 3 >= lag_feet
        putt_feet = (to_yards(hud.to_pin) or 0) * 3 if is_putt else None
        to_par = putt_to_par(hud.shot_number, self.current_par) if is_putt else None
        # A big putt that drops (a long one, or any for eagle) gets the colour voice's reply too, so
        # the booth reacts together.
        reply = bool(is_putt and second and (holed_putt_level(putt_feet, to_par) or 0) >= REPLY_PUTT_LEVEL)
        line_user, facts_text = preshot_line_prompt(
            player=who, team=team, hole=self.state.current_hole, facts=facts,
            recent_lines=self.state.recent_lines, speaker=self.speakers[0], second=second, is_putt=is_putt,
        )
        # Names, not keys: the branch values are bare strings, and a quoted key ("pbp") gets echoed as the line.
        name = lambda key: self.cfg.personas[key].name if key in self.cfg.personas else key  # noqa: E731
        branch_user, _ = preshot_branch_prompt(
            player=who, team=team, hole=self.state.current_hole, facts=facts,
            recent_lines=self.state.recent_lines, speaker=name(self.speakers[0]),
            second=name(second) if second else None, is_putt=is_putt,
            named=minimap.named(to_yards(hud.to_pin)) if minimap else None, lag=lag,
            putt_feet=putt_feet, reply=reply, putt_to_par=to_par, off_flag=self._off_flag(player, hud.lie, hud.to_pin, hud.elevation_yards, self.gspro.pending_aim,
                                   getattr(hud, "_stymie", False)),
        )
        # Three concurrent calls: the spoken line (live_llm, text) and the branches (flight_llm, text)
        # always run; the hazard read (preshot_llm, vision) runs only for a full shot with an image to
        # read. A lean fact set (lie + pin) keeps the vision call fast. Order the dict so the swing-
        # critical line is first in the gather for the clearest failure logging.
        # Roll for the setup line before writing it: a line that won't air is a wasted call. The
        # branches are always written (impact needs them whether or not a setup line is spoken).
        hud._line_turn = self.rng.random() <= self._setup_line_rate(hud, is_putt)
        calls: dict[str, Any] = {}
        if hud._line_turn and self.cfg.commentary.preshot:
            calls["line"] = self._complete("preshot-line", self.live_llm, line_user, [],
                                           preshot_line_schema(self.speakers))
        calls["branches"] = self._complete("preshot-branches", self.flight_llm, branch_user, [],
                                           preshot_branch_schema(self.speakers, is_putt, lag, reply))
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

    def _partner_to_play(self, player: str | None, stroke: int | None) -> str | None:
        """In a scramble, a partner who hasn't yet hit the stroke before `stroke` on this hole, when
        `player` has: the team's stroke `stroke` can't be set up before everyone's played the last."""
        r = self.round
        if not r or r.ended or r.format != "scramble" or not player or not stroke or stroke < 2:
            return None
        side = r.side_for(player)
        if side is None or len(side.players) < 2 or len(set(side.gspro)) < 2:
            return None   # (partners sharing one GSPro profile: their turns can't be told apart)
        played = {x.player for x in self.state.shots if x.round_id == r.id and x.hole_seq == r.seq
                  and not x.mulligan and x.shot_number == stroke - 1}
        if player not in played:
            return None   # this player's own count is off: no grounds to say anyone's still to play
        return next((p for p in side.players if p != player and p not in played), None)

    def _setup_line_rate(self, hud: Hud, is_putt: bool) -> float:
        """The chance this setup gets a spoken line. Weighted toward the moments worth setting up:
        every putt (a long lag and a close scoring look both deserve a word) and a short scoring
        approach; routine full shots keep the baseline rate."""
        rate = self.cfg.commentary.preshot_rate
        pin_yd = to_yards(hud.to_pin)
        if is_putt or (pin_yd is not None and pin_yd <= 40):
            rate = max(rate, 0.9)
        if any(getattr(hud, flag, False) for flag in ("_mulligan", "_lie_correction", "_stymie", "_wrong_green")) \
                or (getattr(hud, "_penalty", None) or (0, 0, False))[2]:
            # A mulligan, a wrong call to set straight, a tree in the way, the wrong green or a penalty
            # not yet called (no splash) always gets its word.
            rate = 1.0
        return rate

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
        except asyncio.CancelledError:
            if task.cancelled() and not asyncio.current_task().cancelling():
                return None   # the read was stopped (off air), not this setup
            raise
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
        call_at = (len(self.state.shots), self._shot_seq)   # a setup line's call: this player's next stroke
        images = [event.image] if event.image else []
        # The call was usually started at setup (overlapping the HUD read); await that. Replay and
        # tests post no task, so fall back to making it here.
        if event.model_task is not None:
            raws, facts_text = await event.model_task   # (its setup's lie source counted at dispatch)
        else:
            raws, facts_text = await self._preshot_model(event.hud, player, images)
            self._count_lie_source(event.hud)
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
        is_putt = (event.hud.lie or "").lower() == "green" and not getattr(event.hud, "_wrong_green", False)
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

        # A busy booth defers a putt or short setup line until idle; it never talks over a live call.
        rate = self._setup_line_rate(event.hud, is_putt)
        pin_yd = to_yards(event.hud.to_pin)
        correction = getattr(event.hud, "_lie_correction", False)
        stymie = getattr(event.hud, "_stymie", False)
        # The opening tee shot skips the spoken setup line (the opener just set the round up, and a
        # line here would land late), but still pre-voices the flight and result branches below so the
        # first shot's calls are ready at impact like every other shot's.
        skip_setup_line = self._skip_opening_preshot
        self._skip_opening_preshot = False
        def setup_lines() -> list:
            lines = parse_lines(json.dumps({"lines": data.get("lines") or []}), self.speakers, c.max_words_per_line, self._persona_names)
            # No retry: a setup line that needs a second attempt has usually missed its moment.
            good, problems = self._grounded(lines, facts_text, mode="preshot", seen_image=False)  # the line call is text-only
            if not good:
                log.info("Setup line dropped: %s", "; ".join(problems) or "the model wrote none")
            elif (pen := getattr(event.hud, "_penalty", None)) is not None:
                # Told once the line saying it is actually on air (hooked before any voicing can drop
                # lines): one queued and then dropped, or never voiced, leaves it for the next setup.
                def told(key=pen[0], n=pen[1]) -> None:
                    self._narrated_pen[key] = max(self._narrated_pen.get(key, 0), n)
                said = next((l for l in good if re.search(r"penalt|out of bounds|\bOB\b|lost ball|\bdrop", l.text, re.I)),
                            good[-1])
                said._on_air = told
            return good

        def aired(good: list) -> None:
            self.state.record_lines(good)
            self._write_transcript(good, "preshot", about=(player, *call_at))
            # The pre-shot (incl. any commentator call) aired; make its prediction gradeable, keyed
            # by player and stamped with the hole so a stale call (its putt produced no shot event)
            # is discarded at grading rather than graded against a later hole's putt.
            if (pred := getattr(event.hud, "_prediction", None)) is not None:
                self._pending_prediction[pred[2]] = (pred[0], pred[1], self.state.current_hole)

        spoken = not skip_setup_line and c.preshot
        turn = getattr(event.hud, "_line_turn", None)
        if turn is None:   # results supplied without _preshot_model (replay, tests): roll here
            turn = self.rng.random() <= rate
        waiting = self._partner_to_play(player, event.hud.shot_number)
        if spoken and waiting:
            # GSPro flashes the hitter's next ball before the partner's turn (Josh's deep-rough 26 yards
            # shown before Corey teed off): the team's next stroke isn't set up until the pick.
            log.info("Setup line skipped: %s still to play stroke %s", waiting, event.hud.shot_number - 1)
        elif spoken and not turn:
            log.info("Setup line skipped: not this shot's turn (rate %.0f%%)", rate * 100)
        elif spoken and (good := setup_lines()):
            if self.speech.is_idle():
                self.speech.say(good)
                aired(good)
            elif is_putt or correction or stymie or (pin_yd is not None and pin_yd <= 40) \
                    or (getattr(event.hud, "_penalty", None) or (0, 0, False))[2]:
                task = asyncio.create_task(self._air_setup_when_idle(good, self._shot_seq, PRESHOT_IDLE_WAIT_SECONDS, aired))
                task.add_done_callback(lambda t: t.cancelled() or t.exception())
            else:
                log.info("Setup line dropped: the booth was talking")

        # Then voice the mid-flight lines while the player sets up. Voicing takes over a second on
        # some CPUs, which would land a wedge call after the ball does. Likeliest outcomes first;
        # whatever isn't ready at impact is voiced then instead.
        order = ("on_target", "left", "right", "short", "long", "coming_back", "layup", "left_trouble", "right_trouble")
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
            async def voice_into(target: dict, key: str, text: str, speaker: str) -> None:
                if (audio := await self.speech.prepare(Line(speaker, text))) is not None:
                    target[key] = audio
            await asyncio.gather(
                *(voice_into(plan.audio, o, plan.lines[o], self.speakers[0]) for o in outcomes),
                *(voice_into(plan.reaction_audio, o, plan.reactions[o], self._reaction_speaker(o))
                  for o in plan.reactions),
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
            if line and not is_putt and (club := names_club(line[0].text)):
                log.info("Dropped the %s flight line (names a club, %r, before it's known): %r", outcome, club, line[0].text)
                continue
            if outcome == "water":
                # The water tease names the water, which the unseen-scenery grounding would reject.
                # It's only ever played when the GSPro log confirms the ball is wet, so the water is a
                # fact, not a guess; guard only against an invented number (as the reactions do).
                if line and not re.search(r"\d", line[0].text) and not self._on_air().slip(line[0].text):
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

    def _play_reaction(self, plan: FlightPlan, outcome: str, text: str, impact_seq: int | None = None) -> object:
        """Air a pre-written result line, with the colour reply after it on a long holed putt. Returns
        the main line's audio (None when it's voiced now)."""
        line = Line(speaker=self.speakers[0], text=text)
        lines = [line]
        audio = plan.reaction_audio.get(outcome)
        reply = plan.reactions.get("holed_reply") if outcome in ("holed", "lip_in") else None
        if reply:
            lines.append(Line(speaker=self._reaction_speaker("holed_reply"), text=reply))
            reply_audio = plan.reaction_audio.get("holed_reply")
            # A sequence skips a clip with no audio, so voice both now unless both are ready.
            if audio is not None and reply_audio is not None:
                self.speech.say_prepared_sequence([(line, audio), (lines[1], reply_audio)])
            else:
                self.speech.say(lines, interrupt=True)
        elif audio is not None:
            self.speech.say_prepared(line, audio)
        else:
            self.speech.say([line], interrupt=True)
        self.state.record_lines(lines)
        self._write_transcript(lines, "reaction", impact_seq=impact_seq, after=True)
        return audio

    def _reaction_speaker(self, outcome: str) -> str:
        """The voice of a pre-written result line: the play-by-play's, except the colour reply to a holed putt."""
        return self.speakers[1] if outcome == "holed_reply" and len(self.speakers) > 1 else self.speakers[0]

    def _build_reactions(self, data: dict, is_putt: bool, facts_text: str = "") -> dict[str, str]:
        """The pre-written result lines. Each is written for one known outcome and is played only
        when that outcome happens, so the usual result-grounding (which would reject "found the
        green" before the ball lands) doesn't apply; only invented numbers and reused phrasing do."""
        out: dict[str, str] = {}
        recent = [l.text for l in self.state.recent_lines]
        # (this putt's score, the comeback's): a holed reaction may only name the first, a miss or a
        # conceded lag only the second ("the comeback putt for par" when the comeback was for bogey).
        putt_for = m.groups() if is_putt and (m := PUTT_FOR_FACT.search(facts_text)) else None
        # Which N-putt holing it makes (and the conceded tap-in after a lag), when the facts count them.
        putts = (m.group(1) if is_putt and (m := PUTT_COUNT_FACT.search(facts_text)) else None)
        next_putts = _PUTT_WORDS[_PUTT_WORDS.index(putts) + 1] if putts in _PUTT_WORDS[:-1] else None
        for outcome, text in (data.get("reaction_lines") or {}).items():
            if outcome not in reaction_outcomes(is_putt, lag=True) + (("holed_reply",) if is_putt else ()) \
                    or not text:
                continue
            parsed = parse_lines(json.dumps({"lines": [{"speaker": self._reaction_speaker(outcome), "text": text}]}),
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
            said = re.findall(r"\d+(?:\.\d+|,\d{1,2}(?!\d))?", body)   # "7,5": a decimal comma
            if any(n.replace(",", ".") not in known for n in said):
                log.info("Dropped the pre-written %s reaction (quotes a number not in the facts): %r", outcome, body)
                continue
            if phrase := repeats(body, [self.system_examples]) or repeats(body, recent):
                log.info("Dropped the pre-written %s reaction (reuses %r): %r", outcome, phrase, body)
                continue
            if not is_putt and (club := names_club(body)):
                log.info("Dropped the pre-written %s reaction (names a club, %r, before it's known): %r", outcome, club, body)
                continue
            if putt_for and (named := wrong_putt_score(body, *putt_for, holed=outcome in ("holed", "lip_in", "holed_reply"))):
                log.info("Dropped the pre-written %s reaction (calls a putt for %s): %r", outcome, named, body)
                continue
            if outcome in ("holed", "lip_in", "holed_reply") \
                    and (named := wrong_holed_putt(body, putt_for[0] if putt_for else None, putts)):
                log.info("Dropped the pre-written %s reaction (holing it makes %s, not %s): %r", outcome,
                         " / ".join(x for x in (putt_for and putt_for[0], putts and f"a {putts}-putt") if x),
                         named, body)
                continue
            if outcome == "gimme" and (named := (putt_for and wrong_conceded_score(body, *putt_for))
                                       or wrong_holed_putt(body, putt_for[1] if putt_for else None, next_putts)):
                log.info("Dropped the pre-written gimme reaction (the hole is %s, not %s): %r",
                         " / ".join(x for x in (putt_for and putt_for[1], next_putts and f"a {next_putts}-putt") if x),
                         named, body)
                continue
            if called := self._on_air().slip(body):
                log.info("Dropped the pre-written %s reaction (calls %s %s): %r", outcome, called[1], called[0], body)
                continue
            if said := wrong_round_score(body, facts_text):
                log.info("Dropped the pre-written %s reaction (round score %r isn't in the facts): %r", outcome, said, body)
                continue
            out[outcome] = body
        return out
