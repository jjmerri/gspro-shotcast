"""Who is playing and what's been called: names on air, the calls made, score facts, the hole's wrap and closest to the pin.

Part of the Engine (commentator/engine/core.py), which is these parts together."""
import asyncio
import re
from collections.abc import Iterable

from .common import BoothCall, _OWNED, _gspro_name, _wrap_contradicts_finish, log
from ..facts import PUTT_FOR, ctp_text as _ctp_text, club_name, ctp_feet, to_yards
from ..grounding import call_words, cites_call, credited_words, makes_call
from ..hud import Hud
from ..league import OnAir
from ..models import Line
from ..prompts import wrap_recap_prompt
from ..round import Round, TEAM_BALL, _join as _join_names, to_par
from ..state import ShotRecord


class OnAirMixin:
    """Who is playing and what's been called: names on air, the calls made, score facts, the hole's wrap and closest to the pin."""

    @staticmethod
    def _raw_yards(shot) -> float:
        return (shot.carry or 0) * (1 if (shot.units or "yards").lower().startswith("y") else 1.0936)

    def _player(self, *texts: str | None) -> str | None:
        for text in texts:
            if name := self.league.match_player(text):
                return name
        # Not on the league roster (no league file, or a guest): the round's own GSPro names.
        r = self.round
        if r and not r.ended and texts and texts[0]:
            raw = _gspro_name(texts[0])
            if name := self._guest_name(raw):
                if name.casefold() == raw.casefold():
                    return name
                # A misread: whoever the read of the name itself would be (a league player's league name).
                return self.league.match_player(texts[0].replace(raw, name, 1)) or name
        return next((t for t in texts[1:] if t), None)

    def _anonymous(self, player: str | None) -> bool:
        """Partners sharing one ball (alternate shot): GSPro's name on it isn't who hit it."""
        r = self.round
        return bool(r and not r.ended and player and player in r.anonymous_players())

    def _on_air(self, about: Round | None = None) -> OnAir:
        """Who goes by a nickname, against everyone the booth might name: the roster, the commentators,
        and the guests of the round being talked about (`about`, an opener's before it's the live one)
        and the live or just-ended one (a closer's). A first name is shared only with those rounds'
        players and the commentators (with no round, the whole roster)."""
        guests: set[str] = set()
        present: set[str] = set()
        rounds = [r for r in (about, self.round) if getattr(r, "roster", None)]
        if self.league.nicknamed():
            for r in rounds:
                guests |= {g for g, p in r.roster.items() if p is None}
                present |= {p or g for g, p in r.roster.items()}
        key = (tuple(sorted(guests)), tuple(sorted(present)))
        if self._on_air_cache is None or self._on_air_cache[0] != key:
            personas = [p.name for p in self.cfg.personas.values() if p.name]
            self._on_air_cache = (key, OnAir(self.league.nicknamed(list(key[0])),
                                             [*self.league.players, *key[0], *personas],
                                             [*key[1], *personas] if rounds else None))
        return self._on_air_cache[1]

    def _display(self, player: str | None) -> str | None:
        """How the booth refers to whoever hit: the team, when the hitter can't be known."""
        if self._anonymous(player):
            return self.round.side_for(player).label
        return player

    def _who(self, player: str | None) -> tuple[str | None, str | None]:
        """(player, team) for a prompt header."""
        if self._anonymous(player):
            return self.round.describe_side(self.round.side_for(player)), None
        team = self.league.player_team.get(player or "")
        # A team name like "Josh and Corey" on a solo round's every call kept Corey in the booth's mouth.
        if team and any(p in self.league.team_players.get(team, []) for p in self._absent_partners()):
            team = None
        r = self.round
        if r and not r.ended and not r.is_league and (r.format == "scramble" or r.format == "alternate" and r.colour_teams):
            # A practice scramble's teams are GSPro's, not the league's: with Josh's league team on
            # Corey's calls, a scramble Corey played with Frankie aired "Josh and Corey remain even par".
            # Alternate shot paired by the team colours likewise.
            side = None if r.teams_guessed() else r.side_for(player)
            team = side.label if side and len(side.players) > 1 else None
        return player, team

    def _absent_partners(self) -> list[str]:
        """Teammates of someone playing who aren't in this round. A one-ball team's side lists both
        partners, so a league alternate shot or scramble has none; a practice scramble with Josh alone
        has Corey."""
        r = self.round
        if not r or r.ended:
            return []
        playing = self._playing(r)
        teams = {self.league.player_team.get(p) for p in playing} - {None}
        return [p for t in teams for p in self.league.team_players.get(t, []) if p not in playing]

    @staticmethod
    def _playing(r: Round) -> set[str]:
        return {p for side in r.sides for p in side.players} if r.format in TEAM_BALL else \
            {p for p in r.roster.values() if p}

    def _delivery_turn(self, notable: bool) -> bool:
        """Whether this reaction gets club-delivery facts: never two in a row."""
        return not self._delivery_last and self.rng.random() < (0.35 if notable else 0.08)

    NOTE_USES_PER_ROUND = 2

    def _spent_notes(self) -> list[str]:
        return [p for p, n in self._note_uses.items() if n >= self.NOTE_USES_PER_ROUND]

    def _format_facts(self, stroke: int | None = None, setup: bool = False, player: str | None = None) -> list[str]:
        if not self.round or self.round.ended:
            return []
        # Who leads, every time: given only each player's own score, the booth worked out the margin
        # itself and got it wrong ("one behind" for the leader).
        standing = self.round.standing_fact(self.round.seq - 1)
        return (self.round.format_facts() + ([standing] if standing else []) + self._pick_facts(stroke, setup, player)
                + self._roster_facts() + self._call_facts())

    def _calls_made(self, whole_round: bool = False) -> list[BoothCall]:
        """The booth's calls this round: on this hole or the last one, or the whole round's."""
        seq = self.round.seq if self.round else 0
        return [c for c in self._calls if whole_round or c.seq >= seq - 1]

    def _same_ball(self, player: str, other: str | None) -> bool:
        """Whether `other`'s stroke plays `player`'s ball: theirs, or a partner's in alternate shot."""
        if other == player:
            return True
        r = self.round
        return bool(r and other and r.player_format(player) == "alternate"
                    and (side := r.side_for(player)) is not None and side is r.side_for(other))

    def _strokes_after(self, player: str | None, seq: int | None, index: int) -> list[ShotRecord]:
        """`player`'s ball's strokes (a partner's in alternate shot) (anyone's, with None) struck after impact `seq`, earliest first. Handlers can
        record out of order, so it goes by impact order; a stroke with no impact number, by its place
        after `index` in the session's shots."""
        later = [x for i, x in enumerate(self.state.shots) if (player is None or self._same_ball(player, x.player)) and (
            x.impact_seq > seq if x.impact_seq is not None and seq is not None else i > index)]
        return sorted(later, key=lambda x: (x.impact_seq is None, x.impact_seq or 0))

    def _call_shot(self, call: BoothCall) -> ShotRecord | None:
        """The stroke a call is about, once it's been hit (a mulligan included: it's still the stroke the
        call was about). A next stroke is looked up afresh each time, so one recorded late takes its place."""
        if call.stroke is not None:
            return call.stroke
        shots = self.state.shots
        if call.shot_id is None and call.impact_seq is None:
            return next(iter(self._strokes_after(call.player, call.struck_after, call.start - 1)), None)
        at = next((i for i, x in enumerate(shots) if (call.shot_id is not None and x.shot_id == call.shot_id)
                   or (call.impact_seq is not None and x.impact_seq == call.impact_seq)), None)
        if at is None:
            return None   # that stroke isn't recorded yet
        if call.after:
            return next(iter(self._strokes_after(shots[at].player, shots[at].impact_seq, at)), None)
        call.stroke = shots[at]
        return call.stroke

    def _hitter(self, player: str) -> str:
        """Who a call's stroke is credited to: the team for a shared ball nobody can attribute, even
        once the round has ended (the sign-off's calls are still that round's strokes)."""
        r = self.round
        side = r.side_for(player) if r else None
        return side.label if side and set(side.players) & set(r.anonymous_players()) else player

    def _call_outcome(self, call: BoothCall) -> str:
        """What a call was about and how it came out, or that it's still to be decided."""
        rec = self._call_shot(call)
        if rec is None:
            if not call.player:
                return "about a stroke not hit or not recorded yet: still undecided"
            return f"about {self._hitter(call.player)}'s next stroke: not hit yet, still undecided"
        who = self._hitter(rec.player)
        if rec.mulligan:
            return (f"about {who}'s stroke{f' {rec.shot_number}' if rec.shot_number else ''}: GSPro took it back "
                    "as a mulligan, so the call was never decided: don't judge it")
        if rec.shot.is_putt:
            stake = PUTT_FOR.get(rec.shot_number - rec.par) if rec.shot_number and rec.par else None
            what = f"{who}'s putt" + (f" for {stake}" if stake else "")
            outcome = rec.outcome
            if outcome is None and any(x.hole == rec.hole and not x.mulligan for x in self._strokes_after(
                    rec.player, rec.impact_seq, self.state.shots.index(rec))):
                outcome = "missed"
            if outcome is None:
                return f"about {what}: struck, result not known yet, still undecided"
            how = {"holed": "holed", "missed": "missed",
                   "conceded": "missed, stopped inside the gimme circle"}.get(outcome, outcome)
        else:
            what = f"{who}'s {club_name(rec.shot.club) or 'shot'}" + (
                f" (stroke {rec.shot_number})" if rec.shot_number else "")
            how = "holed" if rec.outcome == "holed" else f"finished in the {rec.hud.lie}" if rec.hud.lie else None
            if how is None:
                return f"about {what}: struck, result not known yet, still undecided"
        return f"about {what}: {how}. Settled: judge the call only by that, never hold it to a later shot"

    def _call_facts(self, whole_round: bool = False) -> list[str]:
        calls = self._calls_made(whole_round)
        where = "this round" if whole_round else "on this hole or the last"
        if not calls:
            return [f"Nobody in the booth made a call out loud {where}: never say anyone predicted or "
                    "called anything."] if whole_round else []
        said = "; ".join(f'{self.cfg.personas[c.speaker].name or c.speaker}: "{c.text}" ({self._call_outcome(c)})'
                         for c in (calls if whole_round else calls[-4:]))
        return [f"The calls the booth made out loud {where}, the only ones to hold anyone to, each with the "
                f"stroke it was about and how that came out (a call came true only if that outcome bears it "
                f"out): {said}"]

    def _uncalled(self, line: Line, whole_round: bool, earlier: Iterable[Line] = ()) -> str | None:
        """Why a line holding a commentator to a call nobody made is wrong, or None. `earlier`: the
        lines kept so far in the same response, whose calls count too."""
        if not self.round:
            return None
        whose = cites_call(line.text)
        credited = credited_words(line.text) if whose is None else set()
        if whose is None and not credited:
            return None
        others = [k for k in self.speakers if k != line.speaker]
        calls, earlier = self._calls_made(whole_round), list(earlier)

        def called(who: set[str]) -> bool:
            return any(c.speaker in who for c in calls) or any(l.speaker in who and makes_call(l.text) for l in earlier)

        if whose is None:
            # "You called for simple" from the one who made that call ("I'm backing him to keep this one
            # simple") when the other voice made none: it gives his own call away. Only when the credited
            # words echo his call, so "you called that a pull" quoting a description still stands.
            mine = set().union(*(call_words(c.text) for c in calls if c.speaker == line.speaker),
                               *(call_words(l.text) for l in earlier if l.speaker == line.speaker and makes_call(l.text)))
            if others and credited & mine and not called(set(others)):
                return ("credits the other commentator with a call the speaker made himself: only he made a "
                        "call" + (" this round" if whole_round else " on this hole") + ", so it's \"I called\", "
                        "not \"you called\"")
            return None
        if whose == "you" and not others:
            return None
        who = {line.speaker} if whose == "me" else set(others) if whose == "you" else set(self.speakers)
        if called(who):
            return None
        return ("holds a commentator to a prediction nobody made" + (" this round" if whole_round else " on this hole")
                + ": only a line that said what would happen (\"I think this slides by\") is a prediction")

    def _roster_facts(self, watching: Iterable[str] = ()) -> list[str]:
        """Who isn't here: a partner of someone playing is left out entirely (a solo practice round had
        "a handy look for <absent partner> now", and asides about the partner's absence call after call). And any player
        note already used enough this round, so the model doesn't keep reaching for it. `watching`: who's
        most likely in the live chat, whom a take on the chat may talk to as a viewer."""
        r, watching = self.round, set(watching)
        facts: list[str] = self._outsider_facts(r) if r and not r.ended else []
        if absent := self._absent_partners():
            facts.append(f"Not playing this round: {', '.join(absent)}. Leave them out of the commentary"
                         + (f", except {_join_names(here)} in the chat, as a viewer watching."
                            if (here := [p for p in absent if p in watching]) else "."))
        if spent := self._spent_notes():
            facts.append(f"Already used enough today, leave alone: the league notes about {', '.join(spent)}.")
        return facts

    _HOLE_TERMS = {-3: "albatross", -2: "eagle", -1: "birdie", 0: "par", 1: "bogey",
                   2: "double bogey", 3: "triple bogey"}

    def _score_fact(self, side, strokes: int, par: int, picked: bool = False) -> str:
        """One line stating a side's hole score with the numbers, so the wrap can say 'bogey'/'one
        over' without the grounding check rejecting it. A pick-up says so: the score is the cap the
        game gave, not strokes played."""
        diff = strokes - par
        term = self._HOLE_TERMS.get(diff)
        margin = "even" if diff == 0 else f"{abs(diff)} {'over' if diff > 0 else 'under'}"
        return (f"Score on the finished hole: {side.label} made {strokes} on the par-{par} hole, "
                f"{margin} par" + (f" (a {term})" if term else "")
                + (": they picked up, and the game scored the hole at its maximum" if picked else "") + ".")

    def _round_total_facts(self, through: int, extra: tuple[str, int, int, int] | None = None) -> list[str]:
        """Each side's round score to par after hole `through`, beside the hole's own: given only the
        hole ("2 under par" for an eagle), the wrap called it the round ("moving two under overall" at
        three under). `extra` = (side key, seq, strokes, par), a pre-built branch's score for the hole."""
        r = self.round
        if not r or r.ended or through < 1:
            return []
        facts = []
        for side in r.sides:
            hyp = extra[1:] if extra and extra[0] == side.key else None
            tally = r.par_type_to_par(side, through, self.state.shots, hyp)
            if tally:
                total = sum(v for v, _ in tally.values())
                facts.append(f"Round score after the finished hole: {side.label} is {to_par(total)} through "
                             f"{through} hole{'s' if through > 1 else ''} (the hole score is for that hole alone).")
        return facts

    def _hole_score_facts(self, finished_hole: str | None) -> list[str]:
        """How each side scored on the hole that just finished, from the scorecard. This is the
        reliable par/bogey signal even when GSPro's result banner never shows. The par comes from the
        hole's own HUD label (e.g. 'par 4'), not r.par, which is seeded from a course card that can
        mismatch the holes actually being played (that mismatch called a par a bogey). Skipped when
        the scorecard hasn't caught up to the finished hole yet (better no claim than the wrong one)."""
        r = self.round
        m = re.search(r"par (\d)", finished_hole or "", re.IGNORECASE)
        if not r or r.ended or r.seq <= 1 or not m:
            return []
        par = int(m.group(1))
        self._apply_log_corrections()
        return [self._score_fact(side, s, par, picked=r.was_picked_up([*side.gspro, *side.players], r.seq - 1))
                for side in r.sides if (s := r.card_score(side, r.seq - 1)) is not None] \
            + self._round_total_facts(r.seq - 1)

    def _hole_penalty_facts(self, seq: int) -> list[str]:
        """Penalty strokes each player took on hole `seq` of the round, from GSPro's log, so the wrap
        counts them instead of explaining the extra stroke as a missed putt. Left out of a scramble,
        where a penalty on a ball that wasn't picked never reaches the team score."""
        r = self.round
        if not r or r.ended or (r.format == "scramble" and not r.no_scramble):
            return []
        holes = {(rec.player, idx): hole for _, rec, idx, hole in self._fire_rows.values()
                 if rec.round_id == r.id and rec.hole_seq == seq and idx is not None}
        facts = []
        for (player, idx), hole in holes.items():
            if n := self.gspro.penalties_on(idx, hole):
                facts.append(f"Penalty on the finished hole: {self._display(player) or player} took {n} penalty "
                             f"stroke{'s' if n > 1 else ''} (a ball in the water, out of bounds or lost), counted "
                             f"in the hole score. Those strokes were penalties, not extra shots or putts.")
        return facts

    def _par_type_facts(self, through: int, extra: tuple[str, int, int, int] | None = None) -> list[str]:
        """Each side's score to par on the par 3s, 4s and 5s through hole `through`, so a wrap that
        wants the tally has the real numbers (the model added one up itself and got it wrong).
        `extra` = (side key, seq, strokes, par): a pre-built branch's hypothetical score for a hole
        the card doesn't have yet."""
        r = self.round
        if not r or r.ended or through < 2:
            return []
        facts = []
        for side in r.sides:
            hyp = extra[1:] if extra and extra[0] == side.key else None
            tally = r.par_type_to_par(side, through, self.state.shots, hyp)
            if not tally:
                continue
            parts = []
            for par in sorted(tally):
                to_par, holes = tally[par]
                margin = "even" if to_par == 0 else f"{abs(to_par)} {'over' if to_par > 0 else 'under'}"
                parts.append(f"{margin} on the par {par}s ({holes} played)")
            facts.append(f"Score by hole type through {through} holes: {side.label} is {', '.join(parts)}.")
        return facts

    def _wrap_tally_facts(self) -> list[str]:
        """The on-demand wrap's penalty and par-type facts for the hole just finished (the scorecard
        has moved the round on to the next hole, so that's seq - 1)."""
        r = self.round
        if not r or r.ended or r.seq <= 1:
            return []
        standing = r.standing_fact(r.seq - 1)
        return (self._hole_penalty_facts(r.seq - 1) + self._par_type_facts(r.seq - 1)
                + r.lead_change_facts(r.seq - 1) + ([standing] if standing else []))

    def _prevoicing(self) -> bool:
        return getattr(self.speech, "prevoicing", True)

    async def _voice_ahead(self, lines: list[Line]) -> tuple[list[Line], list]:
        """Voice an exchange that may never air (a wrap/closer branch, an aside) now, or with
        tts.prevoice off on a cloud voice, keep just the text for _air_exchange to voice as it plays."""
        if self._prevoicing():
            return await self.speech.prepare_exchange(lines)
        return list(lines), [None] * len(lines)

    def _air_exchange(self, items: list[tuple[Line, object]], banter: bool = False, hold: bool = False) -> None:
        """Play an exchange from _voice_ahead: its clips if they were voiced ahead, else voiced now
        (streamed, line by line) the same way a live line is. `banter` (an aside) isn't cut off by a
        routine shot (_banter_on); `hold`ing it, by no shot at all (_banter_held)."""
        extra = {"banter": True, "hold": hold} if banter else {}
        if self._prevoicing():
            self.speech.say_prepared_sequence(items, **extra)
        else:
            self.speech.say([line for line, _ in items], interrupt=True, **extra)

    def _banter_on(self) -> bool:
        """An aside is on air: only a big moment cuts it off; a routine shot's calls wait or skip."""
        return self.speech is not None and getattr(self.speech, "banter_airing", lambda: False)()

    HELD_BANTER_WAIT = 60.0   # seconds the wrap waits for a chat take on air (two 45-word lines run ~40)

    async def _wait_out_held_banter(self) -> None:
        """Let a chat take on air finish before the wrap, which cuts off what's playing (bounded)."""
        if self._banter_held():
            await self.speech.wait_banter(self.HELD_BANTER_WAIT)

    def _banter_held(self) -> bool:
        """The aside on air is one no shot cuts off (the booth talking to the chat): even a big moment
        waits for it."""
        return self.speech is not None and getattr(self.speech, "banter_held", lambda: False)()

    def _kick_wrap_branches(self, side_key: str, scores: list[int], other_facts: list[str],
                            others: dict[str, int] | None = None) -> None:
        """(Re)start the background pre-build of the hole wrap, branched by the one side still to
        finish (`side_key`) over its possible `scores`, with the sides already in fixed in
        `other_facts` (their scores by side key in `others`, checked again before it airs)."""
        if self._wrap_task and not self._wrap_task.done():
            self._wrap_task.cancel()
        self._wrap_task = asyncio.create_task(
            self._prepare_wrap_branches(side_key, scores, other_facts, others or {}))
        self._wrap_task.add_done_callback(lambda t: t.cancelled() or t.exception())

    async def _prepare_wrap_branches(self, side_key: str, scores: list[int], other_facts: list[str],
                                     others: dict[str, int] | None = None) -> None:
        """Pre-generate and pre-voice the hole wrap for each score the deciding side might finish on,
        so the recap plays the instant the scorecard posts the result, no model call, retry or banner.
        The deciding side is the last one still to hole out: solo that's the only player (fired when
        the ball reaches the green); in a group it's whoever is left once the second-to-last holes
        out, with the others' scores already fixed in `other_facts`."""
        r = self.round
        par = self.current_par
        side = next((s for s in (r.sides if r else []) if s.key == side_key), None)
        if not r or r.ended or par is None or side is None or self.segment_llm is None or not scores:
            return
        finished = self.state.current_hole
        recap = self.state.hole_recap(finished, name=lambda p: self._display(p) or p)
        sand_here = [x.player for x in self.state.shots if x.round_id == r.id and x.hole_seq == r.seq
                     and not x.mulligan and (x.hud.lie or "").lower() in ("sand", "bunker")]
        theme = [f for p in dict.fromkeys(sand_here) for f in self._bunker_facts(p)]
        roster = self._roster_facts()

        async def gen(strokes):
            # "round" mode: a recap spans several lies, so skip the single-finishing-lie check that
            # would otherwise reject "found the green, then the rough".
            result = (other_facts + [self._score_fact(side, strokes, par)]
                      + self._round_total_facts(r.seq, (side.key, r.seq, strokes, par)) + self._hole_penalty_facts(r.seq)
                      + self._par_type_facts(r.seq, (side.key, r.seq, strokes, par))
                      + r.lead_change_facts(r.seq, (side.key, r.seq, strokes))
                      + [f for f in [r.standing_fact(r.seq, (side.key, r.seq, strokes))] if f] + theme + roster)
            user, facts = wrap_recap_prompt(finished_hole=finished, result=result,
                                            recap=recap, recent_lines=self.state.recent_lines)
            return self._grounded(await self._generate("segment", self.segment_llm, user, []), facts, "round", False)[0]

        # Generate the branches' text in parallel (network-bound); the final-score branch is then
        # ready sooner, in time to fire on the scorecard rather than only at the hole change.
        drafts = await asyncio.gather(*(gen(s) for s in scores), return_exceptions=True)
        written: dict[int, list] = {}
        for strokes, lines in zip(scores, drafts):
            if isinstance(lines, Exception):
                log.debug("Wrap branch for %s strokes failed: %s", strokes, lines)
            elif lines:
                written[strokes] = lines
        # On a network voice, voice every branch (and every line in it) at once; a local CPU voice goes
        # one line at a time. A branch whose opening line couldn't be voiced is left out, so the
        # on-demand fallback covers it. With tts.prevoice off (cloud), only the text is kept and the
        # branch that airs is voiced as it plays.
        if getattr(self.speech.tts, "concurrent", False):
            voiced = await asyncio.gather(*(self._voice_ahead(l) for l in written.values()))
        else:
            voiced = [await self._voice_ahead(l) for l in written.values()]
        branches: dict[int, tuple] = {k: v for k, v in zip(written, voiced) if v[0]}
        if self.state.current_hole == finished:  # a new hole hasn't started while we were working
            self._wrap_branches = ({"hole": finished, "side": side_key, "by_score": branches,
                                    "others": dict(others or {})} if branches else None)
            if branches:
                log.info("Wrap: branches ready for %s (%s) on %s", sorted(branches), side.label, finished)

    def _play_prepared_wrap(self, finished_hole, finished_seq: int, last_chance: bool = True) -> bool:
        """Play the pre-made wrap branch for the score the deciding side actually came in on (from the
        scorecard at `finished_seq`). Returns False (fall back to the on-demand wrap) when there's no
        branch for that score, or the scorecard hasn't posted it yet. A branch that tells how the hole
        was finished is checked against GSPro's log; with the result not in yet and a `last_chance`
        still to come (the hole change), it's kept for then rather than aired on a guess."""
        wb = self._wrap_branches
        r = self.round
        side = next((s for s in (r.sides if r else []) if s.key == wb["side"]), None) if wb else None
        if not wb or wb["hole"] != finished_hole or side is None:
            return False
        # The other sides' scores were written into every branch: a later correction (a stroke moved to
        # another player by GSPro's round file) makes them all wrong, and so does a side still out.
        others = {s.key: r.card_score(s, finished_seq) for s in r.sides
                  if s.key != side.key and r.plays(s, finished_seq)}
        if others != wb.get("others", {}):
            log.info("Wrap: dropped the pre-made branches (the other scores are now %s, written for %s)",
                     others, wb.get("others", {}))
            self._wrap_branches = None
            return False
        strokes = r.card_score(side, finished_seq)
        branch = wb["by_score"].get(strokes) if strokes is not None else None
        if not branch:
            return False
        finish = self._hole_finish(finished_seq)
        if _wrap_contradicts_finish(branch[0], finish, shared=any(len(s.players) > 1 for s in r.sides)):
            if finish is None and not last_chance:
                return False
            # Written before the last putt, it guessed the finish (wrong, or with no result to check it
            # against); the on-demand wrap sees the whole hole.
            log.info("Wrap: dropped the pre-made branch for %s strokes (the hole ended %s): %r",
                     strokes, finish or "unknown", " ".join(line.text for line in branch[0]))
            self._wrap_branches = None
            return False
        self._wrap_branches = None
        self._wrapped_hole = finished_hole
        lines, audio = branch
        self.state.record_lines(lines)
        self._air_exchange(list(zip(lines, audio)))
        self._write_transcript(lines, "segment")
        log.info("Wrap: pre-made branch played for %s strokes, no model call", strokes)
        return True

    def _hole_finish(self, seq: int) -> str | None:
        """How hole `seq` was finished, from GSPro's log: "holed" (every side's last putt went in),
        "holed_out" (every side holed a full shot), "conceded" (every side's was inside the gimme
        circle), "mixed", or None while any side's
        isn't known. A wrap can talk about any side, so its finish is checked against them all."""
        r = self.round
        if not r:
            return None
        finishes = set()
        for side in r.sides:
            if not r.plays(side, seq):
                continue
            shots = [x for x in self.state.shots if x.round_id == r.id and x.hole_seq == seq
                     and not x.mulligan and r.side_for(x.player) is side]
            holed = [x for x in shots if x.outcome == "holed"]
            if holed:
                last = holed[-1].shot
                # GSPro's own call wins (a chip off the green isn't a putt); the lie only fills a gap.
                putt = last.putter if last.putter is not None else (
                    last.is_putt or (holed[-1].lie_before or "").lower() == "green")
                finishes.add("holed" if putt else "holed_out")
            else:
                finishes.add("conceded" if any(x.outcome == "conceded" for x in shots) else None)
        if None in finishes or not finishes:
            return None
        return finishes.pop() if len(finishes) == 1 else "mixed"

    def _missing_possessive(self, text: str) -> str | None:
        """A player's name run straight into what's theirs ("Josh round in 42" for "Josh's round")."""
        r = self.round
        names = {n for s in getattr(r, "sides", None) or [] for p in s.players
                 for n in (p, p.split()[0], self.league.nickname(p)) if n}
        for name in names:
            if m := re.search(rf"\b{re.escape(name)} {_OWNED}\b", text, re.I):
                return m.group(0)
        return None

    def _banned_names(self) -> list[tuple[list[str], list[str]]]:
        # Naming an individual player is allowed in every format (a scramble can credit a person, a
        # team format can talk about the team; both read fine). Forcing team-only wording rejected a
        # line on nearly every call and triggered a full model retry, which was the biggest latency
        # hog, and it was plain wrong when one player is on the ball solo. So nothing is banned.
        return []

    def _ctp_facts(self, shot_number: int | None, hud: Hud | None = None, player: str | None = None) -> list[str]:
        """Closest-to-the-pin holes: flagged on the tee, and the tee shot's finish recorded from the
        same player's next setup (the player box then shows how far the ball is from the hole)."""
        r = self.round
        if not r or r.ended or r.hole_number(r.seq) not in r.ctp_holes:
            return []
        prize = self.league.data.get("ctp_prize")
        hole = r.hole_number(r.seq)
        if shot_number == 1:
            facts = ["This is one of the week's closest-to-the-pin holes: the closest tee shot across the whole "
                     "league this week wins" + (f" ${prize:g}." if prize else " the prize.")]
            if (lead := self._ctp_leader(r, hole)):
                facts.append(f"The one to beat this week: {lead[0]}, {lead[1]} from the hole.")
                facts += self._ctp_mark_source(lead)
            facts += self._ctp_group_facts(r, exclude=self._display(player) if player else None)
            return facts
        if shot_number == 2 and hud and hud.lie == "green" and hud.to_pin and player \
                and not getattr(hud, "_wrong_green", False):
            sq = self._sq
            # Whose tee shot the way the league week has it: a practice round only for its holes has
            # GSPro's teams, not the league's (see _squabbit_view).
            who, tee = self._ctp_tee_shot(self._squabbit_view(r, sq) if sq and sq["round"] == r.id else r, player)
            lead = self._ctp_leader(r, hole, exclude=who)
            group = self._ctp_group_facts(r, exclude=who)
            mine = to_yards(hud.to_pin)
            dist = ctp_feet(hud.to_pin)
            r.ctp_results.setdefault(r.seq, {})[who] = hud.to_pin   # unrounded, to rank against
            r.ctp_tees[(r.seq, who)] = tee.shot_id if tee is not None else None
            self._squabbit_kick()
            if tee is not None and r.format == "alternate" and mine is not None:
                self.store.fill_tee_finish(tee.shot_id, mine)   # the shared ball has no log finish
            facts = [f"{who}'s tee shot on this closest-to-the-pin hole finished on the green, {dist} from the hole."]
            if lead and mine is not None:
                facts.append(f"That's the new closest this week, inside {lead[0]}'s {lead[1]}." if mine < lead[2]
                             else f"{lead[0]}'s {lead[1]} is still the closest this week.")
                facts += self._ctp_mark_source(lead)
            elif not lead and r.is_league:
                facts.append("That's the closest tee shot on this hole this week so far.")
            return facts + group
        return []

    def _ctp_tee_shot(self, r: Round, player: str) -> tuple[str, ShotRecord | None]:
        """Whose tee shot this hole's second stroke is played after, and its record. Each ball's own in
        best ball; alternate shot's is the partner who teed off (the team, when one GSPro profile hides
        who); a scramble's is the ball the team picked, or the team until the pick is known."""
        side = r.side_for(player)
        if r.format not in TEAM_BALL or side is None:
            mine = [s for s in self.state.shots if s.round_id == r.id and s.hole_seq == r.seq
                    and s.shot_number == 1 and not s.mulligan and s.player == player]
            return self._display(player), (mine[-1] if mine else None)
        tees = [s for s in self.state.shots if s.round_id == r.id and s.hole_seq == r.seq and s.shot_number == 1
                and not s.mulligan and r.side_for(s.player) is side]
        if r.format == "scramble":
            tees = [s for s in tees if s.picked]
        if len(tees) == 1 or (tees and r.format == "alternate"):
            return self._display(tees[-1].player), tees[-1]
        return side.label, None

    @staticmethod
    def _ctp_mark_source(lead: tuple) -> list[str]:
        # Without this the model guesses when the mark was set ("Dan's had the tape out all night").
        return [f"{lead[0]}'s mark came from an earlier round this week, not this one."] if lead[3] else []

    def _ctp_group_facts(self, r: Round, exclude: str | None = None) -> list[str]:
        """This round's tee shots already on the green on this hole, so a line about a playing partner's
        shot has something true to say (and isn't made up)."""
        return [f"Earlier on this hole in this round, {who}'s tee shot finished on the green, {ctp_feet(dist)} from the hole."
                for who, dist in (r.ctp_results.get(r.seq) or {}).items() if who != exclude]

    def _ctp_leader(self, r: Round, hole: int | None,
                    exclude: str | None = None) -> tuple[str, str, float, bool] | None:
        """The closest tee shot on the green on this hole this week (who, distance text, yards, whether it
        came from an earlier round): earlier rounds from the season store (everything recorded before
        this round started), and this round's own greens as the HUD read them."""
        if hole is None:
            return None
        options = []
        if (db := self._week_ctp(r, before_id=r.shot_floor).get(hole)):
            options.append((db[0], _ctp_text(db[1]), db[1], True))
        for who, dist in (r.ctp_results.get(r.seq) or {}).items():
            if who != exclude and (y := to_yards(dist)) is not None:
                options.append((who, ctp_feet(dist), y, False))
        return min(options, key=lambda o: o[2]) if options else None
