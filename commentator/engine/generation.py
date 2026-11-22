"""Generating and checking what's said: the model calls, grounding, and the transcript.

Part of the Engine (commentator/engine/core.py), which is these parts together."""
import asyncio
import json
import re
import time
from dataclasses import replace
from pathlib import Path

from .common import (
    BoothCall, CLOSER_GREETING, CLOSER_PROMPTS, LIE_REACH_NOTE, REACH_COMFORT, REACH_SHARE, UNCACHED_KINDS,
    UNKNOWN_REACH_YARDS, WELCOME, _ON_AIR, form_topics, log,
)
from .. import perf_log
from ..facts import to_yards
from ..flight_pick import aim_spot, aimed_short, spot_words
from ..grounding import bare_acknowledgement, check_line, makes_call, meta_talk, ngrams, repeats, uses_note
from ..league import OnAir
from ..llm import LLMClient
from ..models import Line
from ..prompts import parse_lines, retry_prompt
from ..round import Round


class GenerationMixin:
    """Generating and checking what's said: the model calls, grounding, and the transcript."""

    @perf_log.timed("model")
    async def _complete(
        self, kind: str, llm: LLMClient, user: str, images: list[bytes], schema: dict | None = None,
        system: str | None = None,
    ) -> str:
        # `system` lets the opener/closer force their round's variant (league/practice): they compose
        # before (opener) or around the edge of (closer) `self.round` being the live round, so the
        # property can't be trusted to pick the right stakes framing for them.
        if not self.commentary_on:
            # Every model call funnels through here; with commentary off it answers "nothing to say",
            # so the opener, plans, reactions, wraps and closer all come out empty and are skipped.
            return "{}"
        started = time.monotonic()
        system, schema = system or self.system, schema or self.schema

        def keep(**outcome) -> None:
            self.prompt_log.record(kind, llm.provider, llm.model, system, user, schema, images, **outcome)

        try:
            no_cache = {"cache": False} if kind in UNCACHED_KINDS else {}
            if (on_air := _ON_AIR.get()) is None:
                on_air = self._on_air()
            user = on_air.spoken(user)
            result = await llm.complete_json(system, user, schema, images, **no_cache)
        except asyncio.CancelledError:
            # A deadline (the mid-flight call) or a newer shot gave up on it: still time it.
            log.info("%s call (%s:%s): cancelled after %.1fs", kind, llm.provider, llm.model,
                     time.monotonic() - started)
            keep(outcome="cancelled", latency_s=time.monotonic() - started)
            raise
        except Exception as e:
            log.warning("%s call (%s:%s): failed after %.1fs: %s", kind, llm.provider, llm.model,
                        time.monotonic() - started, e)
            keep(outcome="failed", error=str(e), latency_s=time.monotonic() - started)
            raise
        keep(text=result.text, latency_s=result.latency_seconds, input_tokens=result.input_tokens,
             output_tokens=result.output_tokens)
        self.usage.record(kind, llm, result)
        log.info("%s call (%s:%s): %.1fs, %s in / %s out tokens", kind, llm.provider, llm.model,
                 result.latency_seconds, result.input_tokens, result.output_tokens)
        if kind == "closer":
            self._capture_closer(llm, system, user, schema, result)
        return result.text

    def _capture_closer(self, llm: LLMClient, system: str, user: str, schema: dict, result) -> None:
        """Keep each closer call's exact prompt and reply in log_dir/closer-prompts.jsonl, so other
        models can be run on the same prompts (tools/closer_ab.py) to compare sign-offs."""
        try:
            entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "provider": llm.provider, "model": llm.model,
                     "system": system, "user": user, "schema": schema, "text": result.text,
                     "latency_s": round(result.latency_seconds, 2), "output_tokens": result.output_tokens}
            with open(Path(self.cfg.log_dir) / CLOSER_PROMPTS, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:   # a record for later comparison: never worth losing the sign-off over
            log.debug("Couldn't keep the closer prompt: %s", e)

    async def _generate(self, kind: str, llm: LLMClient, user: str, images: list[bytes],
                        system: str | None = None, max_words: int | None = None) -> list[Line]:
        raw = await self._complete(kind, llm, user, images, system=system)
        return parse_lines(raw, self.speakers, max_words or self.cfg.commentary.max_words_per_line,
                           self._persona_names)

    def _grounded(
        self, lines: list[Line], facts_text: str, mode: str = "result", seen_image: bool = False,
        on_air: OnAir | None = None,
    ) -> tuple[list[Line], list[str]]:
        good, problems = [], []
        if on_air is None:
            on_air = self._on_air()
        # Small models lift the prompt's example lines verbatim, and settle into catchphrases.
        # 5 words against the examples catches lifted lines; 6 against recent lines lets common
        # phrasing ("in the air with a") through while still stopping catchphrases.
        recent = [l.text for l in self.state.recent_lines]
        banned = self._banned_names() if mode not in ("round", "news") else []
        for line in lines:
            check = check_line(on_air.named(line.text), facts_text, self.league_text, mode=mode,
                               seen_image=seen_image, banned_names=banned)
            if (meta := meta_talk(line.text)):
                check.ok = False
                check.problems.append(f'"{meta}": talks about the booth\'s own rules or data, not the golf')
            if (own := self.cfg.personas.get(line.speaker)) and own.name and re.search(
                    rf"\b{re.escape(own.name)}\b", line.text, re.I):
                check.ok = False
                check.problems.append(f"{own.name} says his own name; he'd only name the other commentator")
            if (called := on_air.slip(line.text)):
                check.ok = False
                check.problems.append(f"calls a player by first name, but on air they go by {called[1]}: say {called[1]}")
            if (slip := self._missing_possessive(line.text)):
                check.ok = False
                check.problems.append(f'"{slip}" needs a possessive (\'s)')
            if bare_acknowledgement(line.text):
                check.ok = False
                check.problems.append("a bare acknowledgement adds nothing")
            if mode != "news" and (why := self._uncalled(line, mode == "round", good)):
                check.ok = False
                check.problems.append(why)
            for player in self._spent_notes():
                if uses_note(line.text, self._note_keys[player]):
                    check.ok = False
                    check.problems.append(f"{player}'s note has been used enough this round")
            if self.cfg.commentary.block_repeats:
                if phrase := repeats(line.text, [self.system_examples]) or repeats(
                    line.text, recent + [g.text for g in good], n=6
                ):
                    check.ok = False
                    check.problems.append(f'reuses the phrase "{phrase}" from earlier or the examples')
                # The opener and wrap-up are templated ("welcome to...", "the defending champions"), so
                # the season-wide repeat check only forces costly retries there; it guards shot lines.
                elif mode not in ("round", "news") and (said := ngrams(line.text, 6) & self._season_phrases):
                    check.ok = False
                    check.problems.append(f'"{" ".join(min(said))}" was already said this season')
            # Each line stands on its own: a valid line is kept even when a sibling was rejected. (An
            # earlier "drop the reply to a dropped line" rule was pulled: it couldn't tell a dependent
            # reply from a standalone line that just addresses the partner, and ate good commentary.)
            if check.ok:
                good.append(line)
                self.stats["lines_ok"] += 1
            else:
                problems += check.problems
                self.stats["lines_rejected"] += 1
                self.rejections.append((line.text, check.problems))
                log.info("Rejected line %r: %s", line.text, "; ".join(check.problems))
        return good, problems

    async def _compose(
        self, kind: str, llm: LLMClient, user: str, facts_text: str, images: list[bytes], mode: str = "result",
        system: str | None = None, must: str | None = None, max_words: int | None = None,
        about: Round | None = None,
    ) -> list[Line]:
        """Generate, ground, and retry a line without airing it. `_speak` airs the result now; the
        pre-round opener holds it until the tee confirms the round. `system` forces a round variant.
        `must` is a word the lines have to say (a mulligan the model would otherwise only hint at).
        An opener must lead with its welcome: with the welcome dropped, the replies that followed it
        aired as non-sequiturs ("Quite the pedigree." to nothing)."""
        seen = bool(images)

        # Both demand English words; in another language the prompt alone asks for them.
        english = self.cfg.commentary.language == "en"

        def needs(lines: list[Line]) -> list[str]:
            out = []
            if english and must and lines and not any(must in l.text.lower() for l in lines):
                out.append(f'none of the lines says "{must}": say it in so many words')
            if english and kind == "opener" and lines and not WELCOME.search(lines[0].text):
                out.append("the opener has to start with a line welcoming everyone to the broadcast")
            if english and kind == "closer" and lines and CLOSER_GREETING.match(lines[0].text):
                out.append("the sign-off can't open with a greeting like \"good afternoon\": the show is ending")
            return out

        # The names as said about this round (an early opener's isn't the live one yet).
        on_air = self._on_air(about)

        async def generate(prompt: str) -> list[Line]:
            token = _ON_AIR.set(on_air)
            try:
                return await self._generate(kind, llm, prompt, images, system=system, max_words=max_words)
            finally:
                _ON_AIR.reset(token)

        lines, problems = self._grounded(await generate(user), facts_text, mode, seen, on_air)
        problems = problems + needs(lines)
        if problems:
            log.info("%s rejected, retrying: %s", kind, "; ".join(problems))
            # One retry with the reasons; a second failure means those lines just aren't said.
            self.stats["retries"] += 1
            retried, still_bad = self._grounded(
                await generate(retry_prompt(user, problems)), facts_text, mode, seen, on_air
            )
            if retried and not (needs(retried) and not needs(lines)):
                lines = retried
            if still_bad or needs(lines):
                log.info("%s retry still had problems: %s", kind, "; ".join(still_bad + needs(lines)))
            if still_bad:
                self.stats["dropped_after_retry"] += len(still_bad)
        if kind == "opener":
            lines = lines[:self.cfg.commentary.opener_max_lines]   # it plays over a load that can be quick
        if english and kind == "closer" and lines and (m := CLOSER_GREETING.match(lines[0].text)):
            rest = lines[0].text[m.end():].strip()
            if rest:
                lines = [replace(lines[0], text=rest[0].upper() + rest[1:]), *lines[1:]]
            else:
                lines = lines[1:]
        return lines

    def _air_lines(self, kind: str, lines: list[Line], shot_id: int | None = None) -> None:
        if not lines:
            return
        self.state.record_lines(lines)
        self.speech.say(lines)
        self._write_transcript(lines, kind, shot_id)

    async def _speak(
        self, kind: str, llm: LLMClient, user: str, facts_text: str, images: list[bytes], mode: str = "result",
        system: str | None = None, must: str | None = None, shot_id: int | None = None,
        max_words: int | None = None,
    ) -> None:
        self._air_lines(kind, await self._compose(kind, llm, user, facts_text, images, mode, system=system,
                                                  must=must, max_words=max_words), shot_id)

    def _note_form_mentions(self, lines: list[Line]) -> None:
        """Which round-sheet angles a line just used, for the side the sheet last went to."""
        if not self._form_pending:
            return
        round_id, side_key, players, seq = self._form_pending
        if not self.round or self.round.id != round_id:
            return

        def names(side) -> set[str]:
            return ({n.split()[0].casefold() for n in side.players if n} | {side.label.casefold()}
                    | {nick.casefold() for n in side.players if (nick := self.league.nickname(n))})

        ours = next((x for x in self.round.sides if x.key == side_key), None)
        if ours is None:
            return
        mine = names(ours)
        others = set().union(*(names(x) for x in self.round.sides if x.key != side_key),
                             {n.split()[0].casefold() for n in self.league.player_info if n},
                             {nick.casefold() for nick in self.league.nicknamed().values()}) - mine

        def says(text: str, words: set[str]) -> bool:
            return any(re.search(rf"\b{re.escape(w)}\b", text, re.I) for w in words)

        # Each line's subject is whoever it names, else the line before's; only stat lines about the
        # side the sheet went to count (a line naming them and another about someone else don't add up).
        subject, topics = None, set()
        for line in lines:
            me, them = says(line.text, mine), says(line.text, others)
            subject = None if me and them else "ours" if me else "theirs" if them else subject
            if subject == "ours":
                topics |= set(form_topics(line.text))
        topics = sorted(topics)
        if not topics:
            return
        self._form_said.setdefault((round_id, side_key), []).append((seq, topics))
        self.store.record_form_mentions(players, topics)
        self._form_pending = None

    def _reach(self, player: str | None, lie: str | None) -> tuple[float, float]:
        """How far out this player can get to the green from this lie (their usual drive, REACH_SHARE over,
        less what the lie takes off a full swing), and the share of the carry the lie leaves."""
        norm = self.store.length_norm(player) if player and player != "unknown" else {}
        drive = norm.get("avg_total") if (norm.get("n") or 0) >= self.LENGTH_NORM_MIN_SHOTS else None
        reach = drive * REACH_SHARE if drive else UNKNOWN_REACH_YARDS
        share = self.carry.penalty((lie or "").lower() or None, reach)
        return reach * share, share

    def _off_flag(self, player: str | None, lie: str | None, to_pin: str | None, elevation_yards: float | None = None,
                  aim: tuple | None = None, stymie: bool = False) -> str | None:
        """Why the flag isn't this shot's target, for the flight lines (None when it is): the green's out of
        the player's reach (counting the lie), they clicked a spot short of it, or a tree blocks the way."""
        yards = to_yards(to_pin)
        long_tee = (lie or "").lower() == "tee" and (self.current_par or 0) >= 4
        if yards is None:
            return "This is a tee shot on a par 4 or 5, so the target is the fairway." if long_tee else None
        spot = None if long_tee else aim_spot(aim, yards)   # a long hole's map stops short of the green
        if spot and aimed_short(aim, yards):
            return f"The player has aimed at {spot_words(spot)}, short of the green."
        reach, share = self._reach(player, lie)
        if yards + max(elevation_yards or 0, 0) > reach:
            lie_note = f" from the {lie}" if share < LIE_REACH_NOTE else ""
            return f"The green is out of this player's reach{lie_note}, so the target is the fairway or a spot to lay up to."
        if stymie:
            return "A tree blocks the line to the pin, so the player may be playing around it or out to the fairway."
        return None

    def _reach_facts(self, player: str | None, lie: str | None, to_pin: str | None, aim: tuple | None,
                     finish_lie: str | None, elevation_yards: float | None = None) -> list[str]:
        """After the shot: whether the green was ever the target, so a layup, a way out of trouble or a
        shot the lie wouldn't let reach is judged by where it left them, and one that should have got
        there isn't excused."""
        yards = to_yards(to_pin)
        if not yards or (lie or "").lower() in ("tee", "green", "fringe", ""):
            return []
        if (finish_lie or "").lower() in ("green", "fringe"):
            return []   # it got there: nothing to excuse
        if aimed_short(aim, yards):
            return [f"The player aimed at {spot_words(aim_spot(aim, yards))}, short of the green: going at the "
                    f"flag was never the plan, so judge it by where it left them."]
        reach, share = self._reach(player, lie)
        if share >= LIE_REACH_NOTE:
            return []
        who = self._display(player) if player else "the player"
        takes = f"the {lie} takes about {round((1 - share) * 100)}% off the carry"
        yards += max(elevation_yards or 0, 0)   # as _off_flag: uphill plays longer
        if yards > reach:
            return [f"From the {lie} the green was out of {who}'s reach ({takes}): judge it by where it left them, "
                    f"not by missing the green."]
        if yards < reach * REACH_COMFORT:
            return [f"From the {lie} the green was within {who}'s reach even so ({takes})."]
        return []

    def _booth_call(self, line: Line, kind: str, shot_id: int | None, about: tuple[str | None, int, int] | None,
                    impact_seq: int | None = None, after: bool = False) -> BoothCall:
        """A call, tied to the stroke it's about: a setup line's, the player's at address; a mid-flight
        line's, the ball in the air; one reacting to a shot, that player's next; any other, the next
        stroke anyone hits."""
        seq, shots = self.round.seq, self.state.shots
        if about is not None:
            return BoothCall(line.speaker, seq, line.text, about[0], about[1], struck_after=about[2])
        if impact_seq is not None:
            return BoothCall(line.speaker, seq, line.text, None, len(shots), impact_seq=impact_seq, after=after)
        if shot_id is not None:
            return BoothCall(line.speaker, seq, line.text, None, len(shots), shot_id=shot_id,
                             after=kind != "flight")
        if kind == "reaction" and shots:   # reacting to the stroke just played
            return BoothCall(line.speaker, seq, line.text, shots[-1].player, len(shots),
                             struck_after=shots[-1].impact_seq)
        return BoothCall(line.speaker, seq, line.text, None, len(shots), struck_after=self._shot_seq)

    def _write_transcript(self, lines: list[Line], kind: str = "shot", shot_id: int | None = None,
                          about: tuple[str | None, int, int] | None = None, impact_seq: int | None = None,
                          after: bool = False) -> None:
        """What the lines' calls are about: `about`, (player, index into state.shots, impact count) when a
        setup line was written, for that player's next stroke; `impact_seq`, the stroke of that handler
        (or with `after`, the same player's next)."""
        # The handler's own shot id beats the shared one, which the next shot may have moved on.
        if shot_id is None and kind in ("shot", "flight"):
            shot_id = self._shot_id
        self.store.record_lines(kind, lines, shot_id)
        if kind == "flight" and (flight := getattr(self.state.recent_lines, "flight", None)) is not None:
            flight.extend(line.text for line in lines)
        self._note_form_mentions(lines)
        for line in lines:
            if self.round and not self.round.ended and makes_call(line.text):
                self._calls.append(self._booth_call(line, kind, shot_id, about, impact_seq, after))
            self._season_phrases |= ngrams(line.text, 6)
            for player, keys in self._note_keys.items():
                if uses_note(line.text, keys):
                    self._note_uses[player] = self._note_uses.get(player, 0) + 1
        stamp = time.strftime("%H:%M:%S")
        with self.transcript.open("a", encoding="utf-8") as f:
            for line in lines:
                name = self.cfg.personas[line.speaker].name
                f.write(f"{stamp} {name}: {line.text}\n")
