"""The facts a pre-shot line can use: the player's season, form, recall, the field and the stakes.

Part of the Engine (commentator/engine/core.py), which is these parts together."""
import datetime
from fractions import Fraction
from typing import Any

from .common import _lie_family, _lie_prep, _misread_putt, _rate_words, _week_gap_fact, log
from .. import recall
from ..facts import SCORE_NAMES, _fmt_to_par, _to_par_value, club_name, ordinal, putt_count_fact, to_yards
from ..hud import Hud
from ..round import Round, TEAM_BALL, to_par
from ..signature_holes import signature_hole
from ..state import ShotRecord


class FactsMixin:
    """The facts a pre-shot line can use: the player's season, form, recall, the field and the stakes."""

    def _replay_facts(self, player: str | None, rec: ShotRecord) -> list[str]:
        """For a re-hit whose mulligan only showed up in GSPro's log (a take-back puts the HUD back
        exactly as it was, so no new setup is seen and the setup-time call can't fire): tell the
        reaction this swing replaced the last one."""
        if not player:
            return []
        mine = [x for x in self.state.shots if x.player == player]
        if len(mine) < 2 or mine[-1] is not rec:
            return []
        prev = mine[-2]
        if not prev.mulligan or id(prev) in self._mulligans_called or prev.hole != rec.hole \
                or prev.round_id != rec.round_id:
            return []
        self._mulligans_called.add(id(prev))
        who = self._display(player)
        club = club_name(prev.shot.club)
        log.info("Mulligan: %s replayed stroke %s (from GSPro's log)", who, rec.shot_number)
        if _misread_putt(prev):
            return [f"This stroke replaced a 'putt' that barely moved the ball: almost certainly the launch "
                    f"monitor misreading {who} nudging the ball into position, so GSPro took it back. Call it a "
                    f"misread by the machine, not a mulligan on a bad putt, then judge this putt on its own."]
        return [f"This swing was a mulligan: {who} took back the previous one"
                f"{f' (the {club})' if club else ''} and replayed the stroke from the same spot. Mention "
                f"the mulligan and judge this result on its own, but don't call it final or \"the one that "
                f"counts\": another mulligan can still follow."]

    def _altitude_facts(self) -> list[str]:
        """The thin air at a high course, for a drive. The altitude is the config's for the course or
        the one GSPro logs as the ball bounces (what its physics plays at), never the course file's:
        a round can play at the global setting instead. The round's course (checked against GSPro's log
        as it started) beats the screen's read, which can keep the last course's name."""
        r = self.round
        course = (r.course if r is not None and not r.ended else None) or self.current_course
        alt = self.cfg.commentary.course_altitude_ft.get(course or "", self.gspro.altitude_ft)
        if alt is None or alt < self.cfg.commentary.altitude_fact_min_ft:
            return []
        return [f"The course plays at {alt:,.0f} feet of altitude: the ball flies farther in the thin air. "
                f"Don't put a number on how much farther; bring it up now and then, not on every drive."]

    def _bunker_facts(self, player: str | None) -> list[str]:
        """A recurring theme: how often this player has finished in a bunker this round (taken-back
        swings excluded), once it has happened at least twice. Fed only where a bunker is on topic
        (a shot into or out of the sand, the wrap of a hole that had one), so it airs now and then."""
        r = self.round
        if not r or r.ended or not player or self._anonymous(player):
            return []
        seqs = [x.hole_seq for x in self.state.shots
                if x.player == player and x.round_id == r.id and not x.mulligan
                and (x.hud.lie or "").lower() in ("sand", "bunker")]
        if len(seqs) < 2:
            return []
        holes = sorted({h for q in seqs if q and (h := r.hole_number(q))})
        where = f" (holes {', '.join(map(str, holes))})" if len(holes) > 1 else ""
        theme = " a recurring theme of the round" if len(seqs) >= 3 else ""
        return [f"{self._display(player)} has found a bunker {len(seqs)} times this round{where}:{theme or ' worth a mention'}"
                f"; bring it up now and then, not on every shot."]

    def _replayed_swing(self, player: str | None, hud: Hud) -> ShotRecord | None:
        """This player's last swing when this setup is for the same stroke, on the same hole, from the
        same spot and lie: the swing was taken back (a mulligan), or the HUD hasn't moved on from it yet."""
        if not player or not hud.shot_number:
            return None
        r = self.round if self.round and not self.round.ended else None
        last = next((x for x in reversed(self.state.shots) if x.player == player), None)
        if last is None or last.shot_number != hud.shot_number:
            return None
        same_hole = (last.round_id == r.id and last.hole_seq == r.seq) if r else last.hole == self.state.current_hole
        # Where the swing was played from, not where it finished.
        here, there = to_yards(hud.to_pin), to_yards(last.to_pin_before or last.hud.to_pin)
        if not same_hole or here is None or there is None or abs(here - there) > max(1.0 if there < 30 else 3.0, there * 0.05):
            return None
        # A replay is from the same lie. Right after a scramble pick the HUD can still show the tee's
        # stroke and distance while the lie already reads the picked ball's (fairway, sand): not a mulligan.
        was, now = (last.lie_before or "").lower(), (hud.lie or "").lower()
        if was and now and was != now:
            return None
        # A scramble's last hitter: GSPro shows their box at the same spot while the team picks a ball
        # (four false mulligans called on Bethpage, 11-07). With every teammate's stroke played, the
        # HUD can't tell a replay from the pick; GSPro's log still flags a real one.
        if r and r.player_format(player) == "scramble" and (side := r.side_for(player)) and len(side.players) > 1:
            played = {x.player for x in self.state.shots if x.round_id == r.id and x.hole_seq == r.seq
                      and x.shot_number == hud.shot_number and not x.mulligan}
            if set(side.players) <= played:
                return None
        return last

    def _mulligan_facts(self, player: str | None, hud: Hud) -> list[str]:
        """A fact when the player is replaying their last swing (a mulligan). GSPro puts the ball back
        and the HUD back to the same stroke number, so a setup for the same stroke, from the same spot,
        as this player's last swing on this hole means that swing was taken back. Read off the HUD at
        the re-hit's setup because GSPro's log often says nothing about a mulligan until the re-hit has
        been played. Marks the HUD so the setup line is always voiced."""
        last = self._replayed_swing(player, hud)
        if last is None or id(last) in self._mulligans_called or last.outcome in ("holed", "conceded"):
            return []
        self._mulligans_called.add(id(last))
        hud._mulligan = True
        who = self._display(player)
        club = club_name(last.shot.club)
        lie = (last.hud.lie or "").lower()
        went = f" (the {club}" if club else " (the last one"
        went += f" that finished in the {lie})" if lie and lie not in ("green", "fairway") else ")"
        log.info("Mulligan: %s replaying stroke %s from %s", who, hud.shot_number, hud.to_pin)
        if _misread_putt(last):
            return [f"{who} is replaying stroke {hud.shot_number}: the last 'putt' barely moved the ball, so it "
                    f"was almost certainly the launch monitor misreading {who} nudging the ball into position, "
                    f"not a real stroke. Call it a misread and have a laugh at the machine, not a mulligan on a "
                    f"bad putt."]
        return [f"{who} is taking a mulligan: the last swing{went} was taken back, and {who} is replaying "
                f"stroke {hud.shot_number} from the same spot{f', {hud.to_pin} away' if hud.to_pin else ''} "
                f"(not from where the taken-back swing finished). Mention the mulligan."]

    def _putt_count_facts(self, player: str | None, is_putt: bool, hud: Hud) -> list[str]:
        """Which putt on the hole this is, counted from this player's own ball (strokes from the green,
        taken-back swings left out, including the one this setup replays before GSPro's log flags it).
        Not in a one-ball team format, where a partner's putts count too."""
        r = self.round
        if not is_putt or not player or not r or r.ended or r.format in TEAM_BALL:
            return []
        replayed = self._replayed_swing(player, hud)
        mine = [x for x in self._round_shots(player)
                if x.hole_seq == r.seq and not x.mulligan and x is not replayed]
        # Only when every stroke before this one is on record (a booth that joined mid-hole, a missed
        # read, or a penalty stroke would leave a gap and miscount).
        played = [x.shot_number for x in mine]
        if not hud.shot_number or sorted(played, key=lambda s: s or 0) != list(range(1, hud.shot_number)) \
                or any(not x.lie_before for x in mine):
            return []
        n = 1 + sum(1 for x in mine if (x.lie_before or "").lower() == "green")
        return [f] if (f := putt_count_fact(self._display(player), n)) else []

    def _penalty_facts(self, player: str | None, shot_number: int | None, hud: Hud | None = None) -> list[str]:
        """A fact when a penalty stroke was taken, so the booth counts it instead of swings.

        GSPro folds a penalty into the stroke count without playing a shot for it, so the shot
        number jumps (2 -> 4 after a water ball and drop). Without this the model counts physical
        swings and calls the fourth shot a third. The GSPro log counts penalties exactly; when it's
        on we use it, else we infer the penalty from the HUD shot number jumping. With the setup's
        `hud`, the log's penalty is only counted as told once a line carrying it airs (see
        _penalty_facts_from_truth).
        """
        idx = self._gspro_idx(player)
        if shot_number == 1:
            # A hole's first stroke follows no penalty. On the tee the log can still hold the player's
            # last hole (it moves on at their first shot here), so don't read that hole's count.
            self._last_hud_shot[player or "unknown"] = 1
            return []
        if idx is not None and self.gspro.current_hole is not None:
            return self._penalty_facts_from_truth(idx, hud)
        if not shot_number:
            return []
        key = player or "unknown"
        last = self._last_hud_shot.get(key)
        self._last_hud_shot[key] = shot_number
        if last is None or shot_number <= last + 1:
            return []
        penalties = shot_number - last - 1
        return [f"A penalty stroke was taken (the stroke count jumped from {last} to {shot_number}: "
                f"{penalties} penalty stroke{'s' if penalties > 1 else ''} for a ball in the water or out "
                f"of bounds, then a drop). Counting the penalty, this is stroke {shot_number} on the hole: "
                f"call it the {ordinal(shot_number)}, not an earlier number."]

    @property
    def system(self) -> str:
        """The system prompt for the current round type: the practice variant (no stakes on the line)
        unless a real league round is in progress. Defaults to practice when no round is live, so the
        booth never invents league stakes before one is confirmed."""
        r = self.round
        return self._system_of(r) if (r is not None and not r.ended) else self._system_practice

    def _system_of(self, r) -> str:
        """The round's own system prompt variant: league, tournament or practice."""
        return self._system_league if r.is_league else self._system_tournament if r.is_tournament else self._system_practice

    def _note_tournament(self, r) -> None:
        """GSPro is reporting round `r` (the one being played, or just ended) to the Simulator Golf Tour:
        it's a tournament round (a league one stays league, its schedule decides that), and so are its
        shots so far. GSPro's first sync comes just after the round's first stroke; once the round's
        over, the log has the ended round's until the next course is entered."""
        g = self.gspro
        if r is None or r.is_league or r.is_tournament or not (g.sgt or (g.mode != "course" and g.last_sgt)):
            return
        r.is_tournament = True
        n = self.store.mark_tournament(r.shot_floor, record=r.uuid) if r.shot_floor is not None else 0
        log.info("Round %s is a tournament round: GSPro is syncing it to the Simulator Golf Tour (%d shots so far marked)",
                 r.id, n)

    def _round_ctx(self) -> str:
        """The current round's stat context: 'league' under a live league round, else 'practice'. Used
        to slice a player's season stats to the kind of round they're in (the 'under pressure' view).
        Honours `ended` so post-round facts don't read the league slice while shots write practice."""
        r = self.round
        return "league" if (r is not None and not r.ended and r.is_league) else "practice"

    @staticmethod
    def _ctx_phrase(ctx: str | None) -> str:
        """How a stat's context reads: the current round's kind, or 'this season' for the lifetime slice."""
        return {"league": "in league play", "practice": "outside league play"}.get(ctx, "this season")

    def _ctx_stat(self, query, thin) -> tuple[dict, str | None]:
        """A stat sliced to the current round's context (league/practice), falling back to the lifetime
        slice when that slice is thin. `query(ctx)` runs the store query for a context; `thin(row)` says
        the row is too small to trust. Returns (row, used_ctx), used_ctx None meaning the lifetime slice."""
        ctx = self._round_ctx()
        row = query(ctx)
        if thin(row):
            return query(None), None
        return row, ctx

    def _profile_facts(self, player: str | None, is_putt: bool, hud: Hud) -> list[str]:
        """Season profile colour for this player as grounded facts: how close they get from this
        range, how they putt from this distance, and (on the tee) their go-to club and typical miss.
        Each has a minimum sample so a thin database doesn't produce confident nonsense. The marquee
        rates (putting, driving accuracy) prefer the current context (league/practice) and fall back to
        the lifetime slice when that context is thin; a league-vs-practice contrast rides along too."""
        if not player or player == "unknown":
            return []
        who = self._display(player)
        ctx = self._round_ctx()
        facts: list[str] = []
        dist = to_yards(hud.to_pin)
        if is_putt and dist is not None:
            feet = dist * 3
            pm, used = self._ctx_stat(lambda c: self.store.putting_make_rate(player, feet, context=c),
                                      lambda d: (d.get("attempts") or 0) < 4)
            if (pm.get("attempts") or 0) >= 4:
                facts.append(f"{who} has made {pm['made'] or 0} of {pm['attempts']} putts from around "
                             f"{feet:.0f} feet {self._ctx_phrase(used)}.")
            # On a long lag the danger is the three-putt, which is what a broadcast leans on from here.
            if feet >= 25:
                tp, tused = self._ctx_stat(lambda c: self.store.three_putt_rate(player, feet, context=c),
                                           lambda d: (d.get("holes") or 0) < 4)
                if (tp.get("holes") or 0) >= 4 and tp.get("three_putts") is not None:
                    pct = round(100 * tp["three_putts"] / tp["holes"])
                    facts.append(f"{who} three-putts from around {feet:.0f} feet {pct}% of the time "
                                 f"{self._ctx_phrase(tused)} ({tp['three_putts']} of {tp['holes']}).")
        elif dist is not None:
            # A shot from trouble gets the lie-specific recovery stat (greenside sand, rough); any other
            # full shot gets the general approach-proximity one. One proximity fact either way, no spam.
            lie = (hud.lie or "").lower()
            thin_n = lambda d: (d.get("n") or 0) < 4
            if lie in ("sand", "bunker") and dist <= 30:
                rp, rused = self._ctx_stat(
                    lambda c: self.store.recovery_proximity(player, ("sand", "bunker"), max_to_pin=30, context=c), thin_n)
                if (rp.get("n") or 0) >= 4 and rp.get("avg_finish_yards") is not None:
                    facts.append(f"From greenside sand, {who} usually finishes about "
                                 f"{rp['avg_finish_yards'] * 3:.0f} feet from the pin {self._ctx_phrase(rused)}.")
            elif lie == "rough" and dist <= 120:
                rp, rused = self._ctx_stat(
                    lambda c: self.store.recovery_proximity(player, ("rough",), max_to_pin=120, context=c), thin_n)
                if (rp.get("n") or 0) >= 4 and rp.get("avg_finish_yards") is not None:
                    facts.append(f"From the rough, {who} usually finishes about "
                                 f"{rp['avg_finish_yards'] * 3:.0f} feet from the pin {self._ctx_phrase(rused)}.")
            else:
                ap, aused = self._ctx_stat(lambda c: self.store.approach_proximity(player, dist, context=c), thin_n)
                if (ap.get("n") or 0) >= 4 and ap.get("avg_finish_yards") is not None:
                    facts.append(f"From around {dist:.0f} yards, {who} usually finishes about "
                                 f"{ap['avg_finish_yards'] * 3:.0f} feet from the pin {self._ctx_phrase(aused)}.")
        if hud.shot_number == 1:
            # Driving accuracy only on a par 4/5 tee (a driver on a par 3 isn't a fairway chance, and
            # the shots table has no par to filter the query itself).
            if not is_putt and (self.current_par or 4) >= 4:
                fw, used = self._ctx_stat(lambda c: self.store.fairways_hit(player, context=c),
                                          lambda d: (d.get("tries") or 0) < 6)
                if (fw.get("tries") or 0) >= 6:
                    facts.append(f"{who} finds the fairway off the tee "
                                 f"{round(100 * (fw['hit'] or 0) / fw['tries'])}% of the time {self._ctx_phrase(used)}.")
            prof = self.store.player_profile(player)
            if (prof.get("favorite_club_n") or 0) >= 6 and prof.get("favorite_club"):
                facts.append(f"{who}'s most-used club this season is the {club_name(prof['favorite_club'])}.")
            bias = prof.get("avg_start_dir")
            if bias is not None and (prof.get("aimed") or 0) >= 8 and abs(bias) >= 2:
                facts.append(f"{who} tends to start the ball {'right' if bias > 0 else 'left'} of the aim.")
        # Season colour (scrambling, GIR, birdie rate, putts a hole...) widens the palette so the booth
        # isn't on the same beat every shot. On the tee always, and about half of other shots; the
        # selectivity instruction picks one angle, and each stat is sliced to the round's context.
        if hud.shot_number == 1 or self.rng.random() < 0.5:
            facts += self._player_season_facts(player, ctx)
        facts += self._contrast_facts(player, is_putt, hud)   # at most one league-vs-practice contrast
        return facts

    def _contrast_facts(self, player: str, is_putt: bool, hud: Hud) -> list[str]:
        """At most ONE league-vs-practice contrast for a marquee stat, so the booth can note a player
        who steps up when it counts (or the reverse). The sample gates are DELIBERATELY larger than the
        single-rate gates and the margins wider: a difference needs a real sample behind it, since one
        made putt out of a tiny sample can swing a rate 25 points. Both slices clear the gate and differ
        past the margin, else nothing - a contrast that isn't really there would mislead."""
        who = self._display(player)

        def pct(d, hit, tries):
            return round(100 * (d.get(hit) or 0) / d[tries]) if (d.get(tries) or 0) else None

        if is_putt and (dist := to_yards(hud.to_pin)) is not None:
            feet = dist * 3
            lg = self.store.putting_make_rate(player, feet, context="league")
            pr = self.store.putting_make_rate(player, feet, context="practice")
            if (lg.get("attempts") or 0) >= 10 and (pr.get("attempts") or 0) >= 10:
                a, b = pct(lg, "made", "attempts"), pct(pr, "made", "attempts")
                if abs(a - b) >= 25:   # wide margin: one made putt (~10pp at n=10) can't conjure the gap
                    tail = "he steps up when it counts" if a > b else "sharper in practice than when it counts"
                    return [f"{who} makes {a}% of these in league but {b}% in practice - {tail}."]
            return []
        if hud.shot_number == 1 and not is_putt:
            # Driving accuracy, then the round-level rates (GIR, scrambling); first marquee gap wins.
            if (self.current_par or 4) >= 4:
                lg, pr = self.store.fairways_hit(player, context="league"), self.store.fairways_hit(player, context="practice")
                if (lg.get("tries") or 0) >= 12 and (pr.get("tries") or 0) >= 12:
                    a, b = pct(lg, "hit", "tries"), pct(pr, "hit", "tries")
                    if abs(a - b) >= 15:
                        where = "practice rounds" if b > a else "league play"
                        return [f"{who} finds more fairways in {where} ({max(a, b)}%) than the other ({min(a, b)}%)."]
            lg = self.store.player_broadcast_stats(player, context="league")
            pr = self.store.player_broadcast_stats(player, context="practice")
            for hit, tries, min_t, label in (("gir_hit", "gir_tries", 18, "greens in regulation"),
                                             ("scr_hit", "scr_tries", 12, "up-and-downs")):
                if (lg.get(tries) or 0) >= min_t and (pr.get(tries) or 0) >= min_t:
                    a, b = pct(lg, hit, tries), pct(pr, hit, tries)
                    if abs(a - b) >= 15:
                        tail = "a league gear" if a > b else "it slips when it counts"
                        return [f"{who}: {label} {a}% in league vs {b}% in practice - {tail}."]
        return []

    def _player_season_facts(self, player: str | None, ctx: str | None = None) -> list[str]:
        """Per-individual-player season rates a broadcast leans on - scrambling, sand saves, greens in
        regulation, plus the scores-vs-par stats (birdie rate, bounce-back, par-type scoring, closing
        stretch, putts per hole) - each gated on a real sample, and sliced to the round's context
        (league/practice) with a lifetime fallback when that slice is thin. The per-team version is
        _team_season_facts + _team_hole_stats; this is the same for one player's own ball, counting
        individual-ball rounds only (gated in the store). Noun-phrased so it reads as a player or a team."""
        if not player or player == "unknown":
            return []
        who = self._display(player)
        bs = self.store.player_broadcast_stats(player, context=ctx)
        hs = self.store.player_hole_stats(player, context=ctx)
        used = ctx
        # Thin in this context (too few rounds of this kind): fall back to the lifetime slice so the
        # player still has season colour, labelled "this season" rather than the context.
        if (bs.get("gir_tries") or 0) < 18 and (bs.get("scr_tries") or 0) < 6 and (hs.get("holes") or 0) < 18:
            bs = self.store.player_broadcast_stats(player)
            hs = self.store.player_hole_stats(player)
            used = None
        when = self._ctx_phrase(used)
        facts: list[str] = []
        if (bs.get("scr_tries") or 0) >= 6:
            facts.append(f"{who}: up and down on {round(100 * (bs['scr_hit'] or 0) / bs['scr_tries'])}% of missed "
                         f"greens {when} ({bs['scr_hit'] or 0} of {bs['scr_tries']}).")
        if (bs.get("sand_tries") or 0) >= 4:
            facts.append(f"{who}: par saved from greenside sand {round(100 * (bs['sand_hit'] or 0) / bs['sand_tries'])}% "
                         f"of the time {when} ({bs['sand_hit'] or 0} of {bs['sand_tries']}).")
        if (bs.get("gir_tries") or 0) >= 18:
            facts.append(f"{who}: {round(100 * (bs['gir_hit'] or 0) / bs['gir_tries'])}% of greens hit in "
                         f"regulation {when}.")
        if (bs.get("putt_holes") or 0) >= 18:
            facts.append(f"{who}: averages {bs['putts'] / bs['putt_holes']:.1f} putts a hole {when}.")
        if (hs.get("holes") or 0) >= 18:
            facts.append(f"{who}: birdie or better on {round(100 * (hs['birdies'] or 0) / hs['holes'])}% of holes {when}.")
        if (hs.get("bounce_tries") or 0) >= 5:
            facts.append(f"{who}: answers a bogey with a birdie {round(100 * (hs['bounce_hit'] or 0) / hs['bounce_tries'])}% "
                         f"of the time {when}.")
        # Par-type and closing-stretch scoring are NOT added here: in an individual-ball format each
        # player is already their own Side, so _hole_type_fact and _closing_fact (team-level, keyed by
        # the player's side) fire those exact lines in this same tee fact list. player_hole_stats still
        # exists for them, but surfacing here would duplicate verbatim.
        return facts

    def _use_own_scores(self, hud: Hud, player: str | None) -> None:
        """Round scores to par from our own tally of finished holes in place of the HUD's (an OCR read
        that can miss or garble), for this player and the others on screen; the HUD's where we can't."""
        r = self.round
        if r is None or r.ended:
            return
        if (own := r.running_to_par(player, self.state.shots)) is not None:
            hud.score_to_par = "E" if own == 0 else f"{own:+d}"
        for other in hud.others:
            if (own := r.running_to_par(self._player(other.name), self.state.shots)) is not None:
                other.score_to_par = "E" if own == 0 else f"{own:+d}"

    def _stakes_facts(self, hud: Hud, player: str | None) -> list[str]:
        """The match situation late in a league round: holes left and where this player stands against
        the field on the card, so the booth can put weight on a shot that's actually for something."""
        r = self.round
        if r is None or not r.is_league or not player:
            return []
        holes_left = r.holes - r.seq + 1   # includes the hole being played
        if 1 <= holes_left <= 4 and (net := self._net_stakes(r, player, holes_left)):
            return net
        me = _to_par_value(hud.score_to_par)
        if me is None or holes_left > 4 or holes_left < 1:
            return []
        field = [(self._display(player), me)]
        field += [(o.name, _to_par_value(o.score_to_par)) for o in hud.others
                  if _to_par_value(o.score_to_par) is not None]
        if len(field) < 2:
            return []
        low = min(v for _, v in field)
        who = self._display(player)
        facts = [f"{holes_left} hole{'s' if holes_left != 1 else ''} to play."]
        if me == low:
            shared = sum(1 for _, v in field if v == low) > 1
            verb = "shares the lead" if shared else "leads"
            facts.append(f"{who} {verb} on the card at {_fmt_to_par(me)}.")
        else:
            leader = next(n for n, v in field if v == low)
            facts.append(f"{who} is {_fmt_to_par(me)}, {me - low} back of {leader}.")
        if (cf := self._closing_fact(r.side_for(player))):
            facts.append(cf)
        return facts

    FORM_PUTT_BANDS = ((0, 5), (5, 10), (10, 15), (15, 25), (25, None))     # feet
    FORM_APPROACH_BANDS = ((50, 100), (100, 150), (150, 250))               # yards
    FORM_BIRDIE_LOOK_FEET = 15

    def _round_form_facts(self, player: str | None, is_putt: bool, hud: Hud) -> list[str]:
        """The round so far as a stat sheet, from `round_form_after_holes` finished holes on: putting by
        distance, approach leaves by distance, greens, fairways, driving, scrambling, birdie looks,
        each beside the player's season number. The booth finds the pattern, if there is one; the sheet
        only says what happened. It's held back for two holes after the booth uses it, and comes with the
        angles already used this round and in recent rounds, so the booth doesn't settle into a refrain."""
        self._form_pending = None   # a line is only credited to the sheet its own setup was given
        r, after = self.round, self.cfg.commentary.round_form_after_holes
        side = r.side_for(player) if r and not r.ended and player and after > 0 else None
        if side is None:
            return []
        done = {q for q in range(1, r.holes + 1) if r.card_score(side, q) is not None}
        if len(done) < after:
            return []
        said = self._form_said.get((r.id, side.key), [])
        if any(r.seq - seq < 2 for seq, _ in said):
            return []
        recs = sorted((x for x in self.state.shots if x.round_id == r.id and not x.mulligan
                       and x.hole_seq in done and (x.player in side.players or x.player in side.gspro)),
                      key=lambda x: x.shot.timestamp or 0)
        stats = r.side_stats(side, recs)
        name = side.players[0] if len(side.players) == 1 else None   # season numbers only for one player

        def feet(x) -> float | None:
            y = to_yards(x.to_pin_before)
            return None if y is None else y * 3

        def rate(a, b) -> str:
            return f" (season {round(100 * a / b)}%)" if b else ""

        def band(lo, hi, unit) -> str:
            return f"{lo}-{hi} {unit}" if hi is not None else f"over {lo} {unit}"

        rows = []
        to_par = stats.gross - sum(r.par[q] for q in done) if all(r.par.get(q) for q in done) else None
        counts = {}
        for q in done:
            if (sc := stats.scores.get(q)) is not None and r.par.get(q):
                d = sc - r.par[q]
                key = "eagles or better" if d <= -2 else "birdies" if d == -1 else "pars" if d == 0 \
                    else "bogeys" if d == 1 else "doubles or worse"
                counts[key] = counts.get(key, 0) + 1
        if to_par is not None:
            rows.append(f"Score: {_fmt_to_par(to_par)} through {len(done)} holes ("
                        + ", ".join(f"{n} {k}" for k, n in counts.items()) + ").")

        putts = [x for x in recs if x.shot.is_putt and x.outcome in ("holed", "missed", "conceded")
                 and feet(x) is not None]
        cells = []
        for lo, hi in self.FORM_PUTT_BANDS:
            got = [x for x in putts if lo < feet(x) <= (hi if hi is not None else 1e9)]
            if not got:
                continue
            base = self.store.putt_band_rate(name, lo, hi) if name else {}
            season = rate(base.get("made") or 0, base["attempts"]) if (base.get("attempts") or 0) >= 10 else ""
            cells.append(f"{band(lo, hi, 'ft')} {sum(x.outcome == 'holed' for x in got)} of {len(got)}{season}")
        if cells:
            rows.append("Putts holed by length: " + "; ".join(cells) + ".")
        if stats.putt_holes:
            rows.append(f"Putting: {stats.putts} putts on {stats.putt_holes} holes, {stats.one_putts} one-putts, "
                        f"{stats.three_putts} three-putts.")
        looks = made_looks = 0
        for q in done:
            first = next((x for x in recs if x.hole_seq == q and x.lie_before == "green"), None)
            hole_par = r.par.get(q)
            if first and hole_par and first.shot_number and first.shot_number <= hole_par - 1 \
                    and (f := feet(first)) is not None and f <= self.FORM_BIRDIE_LOOK_FEET:
                looks += 1
                made_looks += stats.scores.get(q, 99) <= hole_par - 1
        if looks:
            rows.append(f"Birdie putts from inside {self.FORM_BIRDIE_LOOK_FEET} feet: {made_looks} of {looks} converted.")

        leaves: dict[tuple, list[float | None]] = {}
        # In a scramble every partner hits each stroke and the next record is usually a partner's shot,
        # not this ball's next one, so a finish can't be paired with its approach.
        for i, x in enumerate(recs if r.format != "scramble" else []):
            if x.shot.is_putt or (y := to_yards(x.to_pin_before)) is None \
                    or (x.shot_number == 1 and (x.par or r.par.get(x.hole_seq) or 0) >= 4):
                continue
            for lo, hi in self.FORM_APPROACH_BANDS:
                if lo <= y < (hi if hi is not None else 1e9):
                    nxt = recs[i + 1] if i + 1 < len(recs) else None
                    if x.outcome in ("holed", "conceded"):
                        leaves.setdefault((lo, hi), []).append(0.0 if x.outcome == "holed" else 3.0)
                    elif nxt and nxt.hole_seq == x.hole_seq and nxt.lie_before == "green" \
                            and (f := feet(nxt)) is not None:
                        leaves.setdefault((lo, hi), []).append(f)
                    else:
                        leaves.setdefault((lo, hi), []).append(None)
        cells = []
        for (lo, hi), got in leaves.items():
            on = [f for f in got if f is not None]
            base = self.store.approach_leave(name, lo, hi) if name else {}
            season = f", season {base['avg_finish_yards'] * 3:.0f} ft" if (base.get("n") or 0) >= 10 else ""
            avg = f", average {sum(on) / len(on):.0f} ft from the hole{season}" if on else ""
            cells.append(f"from {band(lo, hi, 'yd')} {len(on)} of {len(got)} on the green{avg}")
        if cells:
            rows.append("Approaches: " + "; ".join(cells) + ".")

        season = self.store.player_broadcast_stats(name) if name else {}
        hit, tries = stats.gir
        if tries:
            rows.append(f"Greens in regulation: {hit} of {tries}"
                        f"{rate(season.get('gir_hit') or 0, season['gir_tries']) if (season.get('gir_tries') or 0) >= 18 else ''}.")
        hit, tries = stats.fir
        if tries:
            fw = self.store.fairways_hit(name) if name else {}
            rows.append(f"Fairways: {hit} of {tries}"
                        f"{rate(fw.get('hit') or 0, fw['tries']) if (fw.get('tries') or 0) >= 12 else ''}.")
        tees = [x for x in recs if x.shot_number == 1 and (x.par or r.par.get(x.hole_seq) or 0) >= 4]
        lengths = list(self.store.shot_lengths([x.shot_id for x in tees]).values())
        if lengths:
            rows.append(f"Drives on par 4s and 5s: average {sum(lengths) / len(lengths):.0f} yards over {len(lengths)}"
                        + (f", longest {stats.longest_drive:.0f}" if stats.longest_drive else "") + ".")
        hit, tries = stats.scramble
        if tries:
            rows.append(f"Up and down after missing the green: {hit} of {tries}"
                        f"{rate(season.get('scr_hit') or 0, season['scr_tries']) if (season.get('scr_tries') or 0) >= 10 else ''}.")
        hit, tries = stats.sand
        if tries:
            rows.append(f"Sand saves: {hit} of {tries}.")
        if stats.penalties:
            rows.append(f"Penalty strokes: {stats.penalties}.")
        if len(rows) <= 1:
            return []
        self._form_pending = (r.id, side.key, list(side.players), r.seq)

        used_now = sorted({t for _, topics in said for t in topics})
        recent = [] if name is None else self.store.recent_form_mentions(name)
        guide = ("Optional colour, not a script: most shots need none of it. If something here is genuinely "
                 "telling (a strength or weakness today, a contrast with the season, something that matters for "
                 "this shot) you may work in one point, in your own words. A count of one or two is not a "
                 "pattern. Season figures are in brackets.")
        if used_now:
            guide += f" Already used this round: {', '.join(used_now)}; find a different angle or skip it."
        if recent:
            guide += f" Angles the booth leaned on for {side.label} in recent rounds: {', '.join(recent)}; prefer something fresher."
        return [f"Round so far for {side.label} (completed holes only). {guide}\n" + "\n".join("  " + x for x in rows)]

    LENGTH_NORM_MIN_SHOTS = 5

    @staticmethod
    def _is_drive(hud: Hud) -> bool:
        """A tee shot on a hole too long to reach (as the season's drive norm counts them)."""
        on_tee = hud.shot_number == 1 or (hud.lie or "").lower() == "tee"
        return on_tee and (to_yards(hud.to_pin) or 0) >= 250

    def _length_norm_facts(self, player: str | None, club: str | None, tee: bool, to_pin: float | None,
                           total: float | None = None, lie: str | None = None) -> list[str]:
        """The player's usual length (drives off a par-4/5 tee, else this club's full swings), so the
        booth judges a shot against this player's game, not a long hitter's. With `total` (after the
        shot), how this one compared. Nothing until there's a sample; the prompt then says to leave
        length alone. A club's norm is off the tee or fairway only, so it says nothing about a swing
        from any other `lie` (the rough or sand takes distance off)."""
        if not player or player == "unknown" or self._anonymous(player):
            return []
        if not tee and (lie or "").lower() not in ("tee", "fairway"):
            return []
        who = self._display(player)
        if tee:
            norm, what = self.store.length_norm(player), "drives"
        elif club:
            norm, what = self.store.length_norm(player, club), f"full {club_name(club)} shots"
        else:
            return []
        if (norm.get("n") or 0) < self.LENGTH_NORM_MIN_SHOTS or not norm.get("avg_total"):
            return []
        avg = norm["avg_total"]
        if not tee and (to_pin is None or to_pin <= avg * 1.15):
            return []   # a partial or a shot at the green: a full swing's length says nothing about it
        fact = f"{who}'s {what} usually go about {avg:.0f} yards in total."
        if total:
            d = total - avg
            fact += (" This one was about that, a normal length for them: nothing to remark on." if abs(d) <= max(10, 0.1 * avg)
                     else f" This one went {abs(d):.0f} yards {'further' if d > 0 else 'shorter'} than that.")
        return [fact]

    def _finish_correction_facts(self, hud: Hud, player: str | None) -> list[str]:
        """The setup screen shows where the last ball really stopped. The result call reads the lie while
        the ball may still be rolling (a read takes over a second), so a ball that trickled off the green
        got called on it. When the same ball's next setup shows a different lie, own it in a few words."""
        if self.round and not self.round.ended and self.round.format == "scramble":
            return []   # the next shot may be from a partner's picked ball, not this one
        recs = self.state.player_shots(player or "unknown")
        last = recs[-1] if recs else None
        if (last is None or last.shot.is_putt or last.mulligan or last.shot_id in self._lie_corrected
                or last.hole != self.state.current_hole or not hud.shot_number or not last.shot_number
                or hud.shot_number != last.shot_number + 1):
            return []
        called, now = (last.hud.lie or "").lower(), (hud.lie or "").lower()
        if not called or not now or now == "tee" or _lie_family(called) == _lie_family(now):
            return []
        self._lie_corrected.add(last.shot_id)
        hud._lie_correction = True
        log.info("Last call had shot %s finishing on the %s; the setup shows %s", last.shot_id, called, now)
        return [f"Correction: the last shot's call read the ball {_lie_prep(called)} the {called} while it was "
                f"still moving, but it finished {_lie_prep(now)} the {now}. If the booth said {called}, set it "
                f"straight in a few words (it trickled off into the {now}, say) before the setup."]

    def _team_score_facts(self, player: str | None) -> list[str]:
        """A scramble's one shared score; in best ball, the team's running gross score beside each partner's
        own. GSPro's HUD score is the individual's, and with no team number the booth credited a partner's or team's figure to one
        player ("keeps Derek at +1" when +1 was the team's)."""
        r = self.round
        side = r.side_for(player) if r and not r.ended and player else None
        if side and len(side.players) > 1 and r.format == "scramble" and not r.no_scramble:
            # One ball, one score: "he stays at plus two" and "Derek has 23 feet for par to keep the round
            # even" credited Dan and Derek's shared scramble score to Derek (Kiawah, 11-11).
            who = self._display(player)
            return [f"This is a scramble: {side.label} play one ball and share one score. The round score and "
                    f"what this stroke is for (par, birdie) are the team's, not {who}'s own: say the team or "
                    f"they stay at that number, never \"{who} stays at\" or \"his round\". Each partner plays "
                    f"the stroke from the picked spot, so one partner's miss isn't the hole's result while the "
                    f"other still has that stroke to play."]
        tg = r.team_gross(side.team) if side and side.team else None
        if tg is None:
            return []
        team_par, holes, own = tg
        mine = next(v for s, v in own if s is side)
        partners = [(s, v) for s, v in own if s is not side]
        if holes:
            parts = "; ".join(f"{s.label} on their own is {_fmt_to_par(v)}"
                              for s, v in [(side, mine)] + partners)
            facts = [f"Team {side.team} best-ball score through {holes} completed "
                     f"hole{'s' if holes != 1 else ''}: {_fmt_to_par(team_par)} "
                     f"(the better ball each hole). {parts}. The player's round score in the other facts is "
                     f"their own, not the team's: never give the team number as one player's score, or one "
                     f"player's as the team's."]
        else:
            facts = []
        par = r.par.get(r.seq)
        for s, _ in partners:
            if (sc := r.card_score(s, r.seq)) is not None and par:
                name = SCORE_NAMES.get(sc - par)
                facts.append(f"{s.label} has already finished this hole in {sc}"
                             + (f" ({name})" if name else "")
                             + f", so the team has {sc} here unless {side.label} beats it.")
        return facts

    def _prediction_facts(self, player: str | None, is_putt: bool, hud: Hud) -> list[str]:
        """A commentator calls whether this putt drops (from the player's make-rate, or distance when
        there's no data), to be graded against the result. Puts the commentators in the game and gives
        a running 'how good are their calls' stat. Only for putts worth a call, not tap-ins or lags."""
        if not is_putt or not player or player == "unknown" or len(self.speakers) == 0:
            return []
        dist = to_yards(hud.to_pin)
        if dist is None:
            return []
        feet = dist * 3
        if feet < 3 or feet > 25:
            return []
        # About one putt in three gets a call, never two in a row, likeliest on a 6-15 foot look
        # at birdie or par: calling every putt from 3 to 25 feet wore thin within a nine.
        rested, self._pred_rested = self._pred_rested, True
        par = self.current_par
        scoring = bool(hud.shot_number and par and hud.shot_number - par <= 0)
        chance = 0.6 if scoring and 6 <= feet <= 15 else 0.25
        if not rested or self.rng.random() >= chance:
            return []
        self._pred_rested = False
        # Call it drop or slide by in proportion to how often it drops (the player's rate at this
        # length, else a typical amateur's), so the calls vary instead of always "slides by".
        pm = self.store.putting_make_rate(player, feet)
        if (pm.get("attempts") or 0) >= 4 and pm.get("made") is not None:
            p_make = pm["made"] / pm["attempts"]
        else:
            p_make = min(0.9, 2.0 / feet ** 0.8)   # ~0.8 at 3 ft, 0.5 at 6, 0.3 at 10, 0.15 at 25
        predicted_make = self.rng.random() < p_make
        commentator = self.speakers[self._pred_turn % len(self.speakers)]
        self._pred_turn += 1
        # Stash the call on this setup's HUD; handle_preshot commits it only if the line actually airs,
        # so an unspoken call is never written to the season prediction record.
        hud._prediction = (commentator, predicted_make, player)
        name = self.cfg.personas[commentator].name
        facts = [f"{name} is calling this putt to {'drop' if predicted_make else 'slide by'}; have "
                 f"{name} make that prediction out loud before the stroke."]
        rec = self.store.prediction_record(commentator)
        if rec["total"] >= 5:
            facts.append(f"{name}'s calls this season have been right {_rate_words(rec['hits'], rec['total'])} "
                         f"({round(100 * rec['hits'] / rec['total'])}%). If it's mentioned, say it that way, "
                         "never as a count of calls.")
        return facts

    def _net_stakes(self, r: Round, player: str, holes_left: int) -> list[str]:
        """Late in a league round, the match on net: this team, the other teams in the group, and the
        best score other teams have posted for the week. [] when the net isn't known (falls back to the
        card's gross)."""
        side = r.side_for(player)
        team = side and (side.team or side.key)
        tn = r.team_net(team) if team else None
        if not tn:
            return []
        facts = [f"{holes_left} hole{'s' if holes_left != 1 else ''} to play.",
                 f"{self.league.team_name(team)} are net {to_par(tn[0])} through {tn[1]} holes."]
        for t in r.teams:
            if t and t != team and (o := r.team_net(t)):
                facts.append(f"{self.league.team_name(t)} are net {to_par(o[0])} through {o[1]} holes.")
        if (others := self._week_others(r)):
            best = self.league.team_name(others[0].team)
            facts.append(f"The best week {r.schedule.get('week')} score posted on Squabbit: "
                         f"{best} at net {to_par(others[0].net)}.")
            if tn[1] + holes_left == r.holes:   # the card is current, so "the last N holes" is exact
                facts.append(_week_gap_fact(self.league.team_name(team), tn[0], best, others[0].net, holes_left))
        if (cf := self._closing_fact(side)):
            facts.append(cf)
        return facts

    def _streak_facts(self, player: str | None) -> list[str]:
        """On the tee: a run the card shows but the booth can't see in a score total (two birdies in a
        row, a stretch without a dropped shot, a run of bogeys)."""
        r = self.round
        side = r.side_for(player) if r and not r.ended and player else None
        if side is None:
            return []
        diffs = []
        for q in range(r.seq - 1, 0, -1):
            if (sc := r.card_score(side, q)) is None or not r.par.get(q):
                break
            diffs.append(sc - r.par[q])

        def run(test) -> int:
            return next((i for i, d in enumerate(diffs) if not test(d)), len(diffs))

        who = side.players[0] if len(side.players) == 1 else side.label

        if (n := run(lambda d: d <= -1)) >= 2:
            return [f"{who} has made birdie or better on each of the last {n} holes: {n} in a row."]
        if (n := run(lambda d: d >= 1)) >= 3:
            return [f"{who} has dropped a shot on each of the last {n} holes: {n} in a row."]
        # A clean card from the first hole shows in the score line already; this is a run after a slip.
        if 5 <= (n := run(lambda d: d <= 0)) < len(diffs):
            return [f"{who} hasn't dropped a shot in the last {n} holes."]
        return []

    def _signature_facts(self, player: str | None) -> list[str]:
        """On the first tee shot of a famous hole on a replica of a real course: what the real hole is
        known for. Only for the first player to tee off there (a rebuilt setup for them keeps it), so
        the rest of the group don't hear it again."""
        r = self.round
        if r is None or (note := signature_hole(self._course_names(r), self.hole_number)) is None:
            return []
        told = self._signature_told
        if told and told[:2] == (r.id, self.hole_number) and told[2] != player:
            return []
        self._signature_told = (r.id, self.hole_number, player)
        return [f"Famous hole: this is GSPro's version of {note} Worth a line if it fits the moment; "
                "tell only what this says."]

    # Hole-history facts a round may get: one per this many holes (3 in 18), at least this far apart.
    RECALL_HOLES_PER_FACT = 6
    RECALL_HOLE_GAP = 3
    RECALL_MOMENT_CHANCE = 0.4   # each fitting moment's chance at the last round's standout stat

    def _recall_state(self, r: Round) -> dict[str, Any]:
        if self._recall["round"] != r.id:
            self._recall = {"round": r.id, "rows": {}, "holes": {}, "moment": {}, "moment_told": set(),
                            "field": {}, "field_avgs": None}
        return self._recall

    def _recall_rows(self, r: Round, side) -> list[dict]:
        """A side's earlier rounds (Store.side_rounds), read once a round: this one isn't stored until
        it ends. Filed as record_round files them (a one-person scramble team under the player)."""
        rows = self._recall_state(r)["rows"]
        key = side.players[0] if r.solo(side) else side.key
        if key not in rows:
            rows[key] = self.store.side_rounds(key)
        return rows[key]

    def _recall_opener_facts(self, r: Round) -> list[str]:
        """Each side's last round (when, where, the score) plus one more: what stood out about the
        round as a whole, else their record on this course (a league round has the team's already)."""
        today = datetime.date.today()
        course = self.store.course_name(r.course)
        facts: list[str] = []
        for side in r.sides:
            rows = self._recall_rows(r, side)
            if not (last := recall.last_round_fact(side.label, rows, today)):
                continue
            facts.append(last)
            st = recall.standout(side.label, rows)
            if st and st.moment == "score":
                facts.append(st.text)
            elif not r.is_league and (cf := recall.course_fact(
                    side.label, rows, course, today,
                    self.store.player_team_rounds(side.players[0]) if len(side.players) == 1 else ())):
                facts.append(cf)
        return facts

    def _recall_moment_facts(self, player: str | None, is_putt: bool, hud: Hud) -> list[str]:
        """The last round's standout putting, driving or greens stat, once a round, at a moment it fits
        (a putt, a par 4 or 5's tee shot, an approach) from the second hole on: the opener has the first."""
        r = self.round
        side = r.side_for(player) if r and not r.ended and player else None
        if side is None or r.seq < 2:
            return []
        state = self._recall_state(r)
        shot = (r.seq, player, hud.shot_number)
        if shot in state["moment"]:   # a rebuilt setup for the same shot gets the same decision
            return state["moment"][shot]
        facts: list[str] = []
        if side.key not in state["moment_told"]:   # once a round
            st = recall.standout(side.label, self._recall_rows(r, side))
            fits = {"putt": is_putt,
                    "tee": hud.shot_number == 1 and not is_putt and (self.current_par or 4) >= 4,
                    "approach": (not is_putt and (hud.shot_number or 0) >= 2
                                 and (to_yards(hud.to_pin) or 999) <= 200)}
            if st is not None and fits.get(st.moment) and self.rng.random() < self.RECALL_MOMENT_CHANCE:
                facts = [st.text]
                state["moment_told"].add(side.key)
        state["moment"][shot] = facts
        return facts

    def _hole_recall_facts(self, player: str | None) -> list[str]:
        """On a hole's first tee shot: this side's pattern on the hole in earlier rounds here, when it
        has a clear one. Rationed: one hole per RECALL_HOLES_PER_FACT, RECALL_HOLE_GAP holes apart, one
        side a hole (a rebuilt setup for the same player keeps it)."""
        r = self.round
        side = r.side_for(player) if r and not r.ended and player else None
        if side is None or (hole := r.hole_number(r.seq)) is None:
            return []
        told = self._recall_state(r)["holes"]
        if r.seq in told:
            who, facts = told[r.seq]
            return facts if who == player else []
        if (len(told) >= max(1, r.holes // self.RECALL_HOLES_PER_FACT)
                or any(abs(r.seq - q) < self.RECALL_HOLE_GAP for q in told)):
            return []
        fact = recall.hole_fact(side.label, self._recall_rows(r, side), self.store.course_name(r.course), hole)
        if not fact:
            return []
        told[r.seq] = (player, [fact])
        return [fact]

    def _hole_field_facts(self, player: str | None) -> list[str]:
        """On the first shot of a hole: the hole's difficulty (stroke index), how the league has
        scored it this season, and whether this player gets a handicap stroke here (so the booth can
        talk raw and net). All from the course card + season rounds, no OCR."""
        r = self.round
        if r is None or not r.is_league:
            return []
        hole, par, sidx = r.hole_number(r.seq), r.par.get(r.seq), r.index.get(r.seq)
        if hole is None or par is None:
            return []
        facts: list[str] = []
        if sidx:
            hard = " (one of the toughest on the card)" if sidx <= 4 else \
                   " (one of the easiest)" if sidx >= 15 else ""
            facts.append(f"This hole is stroke index {sidx}{hard}.")
        facts += self._field_scoring_facts(r, player)
        # Use the round's own per-hole allocation (it applies the holes/18 scaling and the format's
        # handicap allowance), so the net the booth states matches how the round is actually scored.
        side = r.side_for(player)
        strokes = side.hole_strokes.get(r.seq, 0) if side else 0
        if strokes > 0:
            facts.append(f"{self._display(player)} gets {strokes} handicap stroke"
                         f"{'s' if strokes > 1 else ''} here, so a {par} plays to net {par - strokes}.")
        if (ht := self._hole_type_fact(side, par)):
            facts.append(ht)
        if side and r.seq > 1 and (tn := r.team_net(side.team or side.key)):
            facts.append(f"{self.league.team_name(side.team or side.key)} are net {to_par(tn[0])} through "
                         f"{tn[1]} hole{'s' if tn[1] != 1 else ''}.")
            if (others := self._week_others(r)):
                facts.append(f"The best week {r.schedule.get('week')} score posted on Squabbit is "
                             f"{self.league.team_name(others[0].team)} at net {to_par(others[0].net)}.")
        return facts

    FIELD_MIN_SCORES = 2       # league cards on a hole before its scoring is worth a word
    FIELD_HOLES_PER_ROUND = 3  # so it stays a story on the holes that stand out, not a fixture

    def _field_scoring_facts(self, r: Round, player: str | None) -> list[str]:
        """How the league has scored this hole, offered only when the hole stands out (it plays half a
        stroke or more over par, it's the hardest or easiest of the round's holes, or nobody has birdied it in
        four or more tries), on at most FIELD_HOLES_PER_ROUND holes a round. Kept per hole for the
        player it was first offered to, so a rebuilt setup keeps it and a partner doesn't repeat it."""
        state = self._recall_state(r)
        told = state["field"]
        if r.seq in told:
            who, facts = told[r.seq]
            return facts if who == player else []
        if len(told) >= self.FIELD_HOLES_PER_ROUND:
            return []
        # This round's holes, read once (other cards don't change mid-round) - again when the start hole
        # moves or a hole's par is learned during play.
        key = (r.start_hole, tuple(sorted(r.par.items())))
        if state["field_avgs"] is None or state["field_avgs"][0] != key:
            avgs: dict[int, tuple[Fraction, list[int]]] = {}   # exact, so tied holes compare equal
            for seq, p in r.par.items():
                if (h := r.hole_number(seq)) is not None and p and \
                        len(sc := self.store.hole_scoring(r.course, h, context="league")) >= self.FIELD_MIN_SCORES:
                    avgs[seq] = (Fraction(sum(sc) - p * len(sc), len(sc)), sc)
            state["field_avgs"] = (key, avgs)
        avgs = state["field_avgs"][1]
        if r.seq not in avgs:
            return []
        rel, scores = avgs[r.seq]
        par = r.par[r.seq]
        under = sum(1 for s in scores if s < par)
        rels = [v for v, _ in avgs.values()]
        ranked = len(rels) >= 3
        hardest = ranked and rel == max(rels) and rels.count(rel) == 1
        easiest = ranked and rel == min(rels) and rels.count(rel) == 1
        if not (rel >= 0.5 or hardest or easiest or (under == 0 and len(scores) >= 4)):
            return []
        n = len(scores)
        facts = [f"The league has played this hole {n} time{'s' if n != 1 else ''} this season, averaging "
                 f"{float(rel) + par:.1f} ({float(rel):+.1f} to par); {under} {'was' if under == 1 else 'were'} under par."]
        if hardest:
            facts.append("By the league's scores it is the hardest of the holes in this round.")
        elif easiest:
            facts.append("By the league's scores it is the easiest of the holes in this round.")
        if (low := min(scores)) < par:
            facts.append(f"The low score on this hole this season is a {low}.")
        told[r.seq] = (player, facts)
        return facts

    def _team_season_facts(self, side) -> list[str]:
        """Season team stats a broadcast leans on - scrambling, sand saves, GIR, birdie rate, bounce-back,
        course history - each gated on a real sample so a thin season can't produce confident numbers.
        Noun-phrased so they read right whether the side is a team or a single player, and the caller
        picks among them (so it isn't always the first one that airs)."""
        r = self.round
        if side is None or r is None:
            return []
        who = side.label
        facts: list[str] = []
        bs = self.store.team_broadcast_stats(side.key)
        if (bs.get("scr_tries") or 0) >= 6:
            facts.append(f"{who}: up and down on {round(100 * (bs['scr_hit'] or 0) / bs['scr_tries'])}% of missed "
                         f"greens this season ({bs['scr_hit'] or 0} of {bs['scr_tries']}).")
        if (bs.get("sand_tries") or 0) >= 4:
            facts.append(f"{who}: par saved from greenside sand {round(100 * (bs['sand_hit'] or 0) / bs['sand_tries'])}% "
                         f"of the time this season ({bs['sand_hit'] or 0} of {bs['sand_tries']}).")
        if (bs.get("gir_tries") or 0) >= 18:
            facts.append(f"{who}: {round(100 * (bs['gir_hit'] or 0) / bs['gir_tries'])}% of greens hit in regulation this season.")
        hs = self.store.team_hole_stats(side.key)
        if (hs.get("holes") or 0) >= 18:
            facts.append(f"{who}: birdie or better on {round(100 * (hs['birdies'] or 0) / hs['holes'])}% of holes this season.")
        if (hs.get("bounce_tries") or 0) >= 5:
            facts.append(f"{who}: a bogey answered with a birdie {round(100 * (hs['bounce_hit'] or 0) / hs['bounce_tries'])}% "
                         f"of the time this season.")
        ch = self.store.team_course_history(side.key, r.course)
        if (ch.get("rounds") or 0) >= 2 and ch.get("avg_net_to_par") is not None:
            scale = r.holes / 9   # stored per nine holes; put on this round's length
            facts.append(f"{who} on this course: {round(ch['avg_net_to_par'] * scale):+d} net over {ch['rounds']} "
                         f"rounds this season{'' if r.holes == 9 else f', scaled to {r.holes} holes'} "
                         f"(best: {round(ch['best_net_to_par']):+d} net over nine holes).")
        return facts

    def _hole_type_fact(self, side, par: int | None) -> str | None:
        """How this side scores on holes of THIS par (3/4/5) this season, for the hole intro."""
        if side is None or par not in (3, 4, 5):
            return None
        s, n = self.store.team_hole_stats(side.key).get(f"par{par}", (0, 0))
        return f"{side.label}: {(round(s / n, 1) or 0.0):+.1f} to par on the par {par}s this season." if n >= 4 else None

    def _closing_fact(self, side) -> str | None:
        """A side's scoring over the closing stretch this season, for the late-round stakes."""
        if side is None:
            return None
        hs = self.store.team_hole_stats(side.key)
        n = hs.get("closing_holes") or 0
        return (f"{side.label}: {(round(hs['closing_to_par'] / n, 1) or 0.0):+.1f} to par on each of their closing holes "
                f"this season.") if n >= 6 else None
