"""A shot's result once it's known: corrections from the log and the round file, pick-ups, and what's recorded.

Part of Engine (commentator/engine/core.py), which is these parts together."""
import asyncio
import math
import re
import time
from dataclasses import replace
from pathlib import Path

from .common import (
    FILE_SETTLE_S, FILE_WAIT_MAX_S, FILE_WAIT_POLL_S, PICK_SCREEN_SECONDS, _hole_key, _tile_values, gspro_ui,
    log,
)
from .. import round_file
from ..hud import Hud
from ..models import HoleChangeEvent
from ..round import TEAM_BALL
from ..sources import gspro_files
from ..state import ShotRecord
from ..sources.club_feeds import SAME_BALL_MPH
from ..sources.gspro_log import M_TO_YD


class ResultsMixin:
    """A shot's result once it's known: corrections from the log and the round file, pick-ups, and what's recorded."""

    async def _correct_hole(self, event: HoleChangeEvent) -> None:
        """Puts the round back on the right hole without a recap: undoes a misread hole change, or moves
        to the hole GSPro's log names. Shots already recorded on the wrong hole move with it."""
        wrong, r = self.state.current_hole, self.round
        self.state.current_hole = event.current
        if new_par := re.search(r"par (\d)", event.current, re.IGNORECASE):
            self.current_par = int(new_par.group(1))
        log.info("Hole corrected (%s): %s -> %s", event.correction, wrong, event.current)
        if not r or r.ended:
            return
        old_seq = r.seq
        if not r.start_hole_assumed and (m := re.match(r"Hole (\d+)", event.current)):
            new_seq = int(m.group(1)) - r.start_hole + 1
        elif event.correction == "revert":
            new_seq = old_seq - 1   # the misread counted one hole
        else:
            return
        if not 1 <= new_seq <= r.holes or new_seq == old_seq:
            return
        await self._drain_shot_tasks(self.cfg.commentary.wrap_wait_seconds)   # shots in flight record first
        r.seq = new_seq
        for seq in [s for s in r.par if s > new_seq]:
            del r.par[seq]   # set by the wrong count; set again when play gets there
        # The hole's par: the label's, else the card's for its number, else what the round had. Not the
        # hole it's corrected from (a label with its par unread).
        card, hole_n = r.course_card, re.match(r"Hole (\d+)", event.current)
        par = (int(new_par.group(1)) if new_par
               else card.par.get(int(hole_n.group(1))) if hole_n and card and card.par else None) \
            or r.par.get(new_seq)
        self.current_par = par or None   # unknown, not the hole it's corrected from
        if par:
            r.par[new_seq] = par
        moved = [x for x in self.state.shots if x.round_id == r.id and x.hole_seq == old_seq and x.hole == wrong]
        kept = []
        # GSPro's round file has the final say on a shot it has: it stays on another hole, or moves to this.
        on = self._file_holes() if (m := re.match(r"Hole (\d+)", event.current)) else {}
        filed = [x for x in moved if x.shot_id in on]
        moved = [x for x in moved if x.shot_id not in on]
        if new_seq > old_seq and (m := re.match(r"Hole (\d+)", event.current)):
            # Shots GSPro's log had on an earlier hole at impact were played there: only the new hole's
            # move (Steve's hole 16 was filed under 17 along with his tee shot there).
            new_hole, g = int(m.group(1)), self.gspro
            fires = {id(rec): f for f, (_, rec, _, _) in self._fire_rows.items()}

            def played_on(x: ShotRecord) -> int | None:
                # The hole its own score line credited (a tee shot is the log's once that's in), else
                # the hole the log had the player playing at impact.
                if (f := fires.get(id(x))) is not None and (h := g.fire_info(f).get("hole")) is not None:
                    return h + 1
                return x.log_hole
            kept = [x for x in moved if (h := played_on(x)) is not None and h < new_hole]
        if m := re.match(r"Hole (\d+)", event.current):
            kept += [x for x in filed if on[x.shot_id] != int(m.group(1))]
            moved += [x for x in filed if on[x.shot_id] == int(m.group(1))]
        moved = [x for x in moved if not any(x is k for k in kept)]
        for x in moved:
            x.hole_seq, x.hole = new_seq, event.current
            x.par = par or None   # the hole's, not the one it was filed under
        # A pickup goes with its player's shots, unless some of theirs stayed (it may be that hole's).
        tee = self._tee_concedes.get(r.id, {})   # (conceded on the tee: that hole's, whatever the shots)
        given = set(self._picked_fires.values()) - {None}
        stayed = {x.player for x in kept}
        for name in {x.player for x in moved}:
            if (name, old_seq) not in r.picked_up:
                continue
            if name not in stayed and (r.current_name(name), old_seq) not in tee:
                r.picked_up.discard((name, old_seq))
                r.picked_up.add((name, new_seq))
            elif any(x.player == name and x.shot_id in given for x in moved):
                r.picked_up.add((name, new_seq))   # a pick-up given to a shot that moved: that hole's
                if (r.current_name(name), old_seq) not in tee \
                        and not any(x.player == name and x.shot_id in given for x in kept):
                    r.picked_up.discard((name, old_seq))
        ids = [x.shot_id for x in moved if x.shot_id is not None]
        if wrong and ids:
            keep = [i for x in kept if x.shot_id is not None
                    for i in (x.shot_id, self._gimme_ids.get(x.shot_id)) if i is not None]
            self.store.relabel_hole(wrong, event.current, min(ids), keep)
        log.info("Round %s: hole %s of %s (was %s); %s shots moved", r.id, r.seq, r.holes, old_seq, len(moved))

    def _sync_scramble(self) -> None:
        """GSPro's log shows a scramble once it copies the first picked ball onto a teammate (after
        everyone's first tee shot). From then the round is a scramble: the format, the teams the copies
        tied together, and every shot already recorded this round."""
        g, r = self.gspro, self.round
        if not g.teammates or r is None or r.ended or r.no_scramble or self._scramble_pairs == g.teammates:
            return
        self._scramble_pairs = set(g.teammates)
        groups: list[set[int]] = []
        for pair in g.teammates:
            hit = [grp for grp in groups if grp & pair]
            merged = set(pair).union(*hit)
            groups = [grp for grp in groups if grp not in hit] + [merged]
        order = r.slot_names or (list(r.roster) if len(r.roster) == len(g.players_seen) else [])
        names = [n for grp in groups if len(n := [order[i] for i in sorted(grp) if i < len(order) and order[i]]) > 1]
        if r.colour_teams:
            # The setup's team colours stand for whoever the log hasn't tied to anyone yet (each team's
            # first copy comes as it picks): not having picked isn't being alone.
            # A log pair inside one colour team only confirms part of it: the rest of that colour join it.
            tied = {n for grp in names for n in grp}
            colours = [[n for n in cg if n in r.roster] for cg in r.colour_teams]
            for grp in names:
                home = next((cg for cg in colours if set(grp) <= set(cg)), None)
                if home is not None:
                    grp += [n for n in home if n not in tied]
                    tied.update(home)
            names += [k for cg in colours if len(k := [n for n in cg if n not in tied]) > 1]
        was = r.format
        if r.play_scramble(names):
            # Every time the teams change: a pair found later had its earlier shots filed as solo, and a
            # player the setup grouped by league team may turn out to be alone (their shots are their own).
            mine = [x for x in self.state.shots if x.round_id == r.id and x.shot_id is not None]
            ids = [x.shot_id for x in mine if r.player_format(x.player) == "scramble"]
            self.store.set_format(ids, "scramble")
            self.store.set_format([x.shot_id for x in mine if r.solo(r.side_for(x.player))], None)
            log.info("GSPro's log shows a scramble (was %s): teams %s; %s earlier shots relabelled",
                     was, [s.label for s in r.sides], len(ids))

    def _pick_facts(self, stroke: int | None = None, setup: bool = False, player: str | None = None) -> list[str]:
        """Whose ball the team is playing, from GSPro's last scramble pick on this hole. Given the stroke
        being set up or reacted to, it says which stroke that pick was: a reaction to the last hitter
        once read the previous stroke's pick as this one's ("the team has chosen Dan's ball"). Given the
        player, only their own side's pick: a lone player in the scramble picks nothing, and another
        team's pick isn't theirs ("Josh gives it a gentle stroke, but Kathy's ball is the team's choice")."""
        g, r = self.gspro, self.round
        if not r or r.ended or r.format != "scramble":
            return []
        side = r.side_for(player) if player else None
        if side is not None and len(side.gspro) == 1 and len(side.players) <= 1:
            who = self._display(player)
            return [f"{who} is playing alone, on {who}'s own ball every stroke: no team, no pick of a ball, "
                    "and nobody else's ball has anything to do with this shot."]
        undecided = (f"Whose stroke-{stroke} ball the team takes isn't decided until everyone has hit and "
                     f"GSPro picks: don't say or guess which ball they chose.") if stroke and not setup else None
        if not g.pick:
            return [undecided] if undecided else []
        idx, hole = g.pick
        order = r.slot_names or list(r.roster)
        if hole != g.current_hole or idx >= len(order) or not order[idx]:
            return [undecided] if undecided else []
        if side is not None and r.side_for(order[idx]) is not side:
            return [undecided] if undecided else []
        name = r.league.match_player(order[idx]) or order[idx]
        n = g.pick_stroke()
        if not stroke or not n:
            return [f"The team picked {name}'s ball from the last round of shots; everyone plays from there."]
        if setup:
            return [f"Stroke {stroke} is played from {name}'s ball, the team's pick from stroke {n}."] \
                if n == stroke - 1 else []
        if n > stroke:
            # A pick from a later stroke than this one is the last hole's: the log moves to a new hole only
            # once its tee shot is in ("They've picked Josh's ball" off the tee, Corey still to hit).
            return [undecided]
        if n == stroke:
            return [f"The team picked {name}'s ball from the stroke-{stroke} shots; stroke {stroke + 1} is "
                    f"played from there."]
        return [f"Stroke {stroke} was played from {name}'s ball (the team's pick from stroke {n}).", undecided]

    def _correct_last_finish(self, idx: int, snap: tuple, lie: str | None, hole_label: str | None = None,
                             shot_number: int | None = None, fmt: str | None = None) -> None:
        """Where the player's previous shot came to rest is where this one was played from. Corrects
        that shot's resting lie (the HUD's read at rest misses, e.g. a tee shot onto the green read as
        fairway) when it's the same hole, no penalty came between (a drop), and it wasn't taken back."""
        prev = self._last_row.get(idx)
        if not prev or not lie or lie == "tee":
            return
        shot_id, rec, hole, pen, dropped = prev
        now = self._idx_snap(snap, idx)
        same = hole is not None and now["hole"] == hole and now["pen"] == pen
        # GSPro's log moves to a new hole only once its tee shot is logged, so a tee shot's snapshot
        # still names the hole before. The HUD's strokes (1, then 2: no penalty between) on the same
        # hole say it's this ball. In a scramble the second shot may be a partner's ball: only when the
        # log counts the balls GSPro dropped, and none of this player's was (their own was picked).
        tee_then_second = (rec.shot_number == 1 and shot_number == 2 and hole_label is not None
                           and _hole_key(rec.hole) == _hole_key(hole_label) and now["pen"] == 0
                           and (fmt != "scramble" or len(snap) > 7))
        if rec.mulligan or not (same or tee_then_second) or now["dropped"] != dropped:
            return   # (a scramble that picked another ball: this shot is played from the team's ball)
        if (rec.hud.lie or "").lower() != lie and (kept := self.store.set_lie_after(shot_id, lie)) \
                and (rec.hud.lie or "").lower() != kept.lower():
            log.info("Shot %s finished on the %s (the HUD read %s)", shot_id, kept, rec.hud.lie)
            rec.hud = replace(rec.hud, lie=kept)

    def _apply_late_tiles(self, seq: int) -> None:
        """A shot's tiles read at the next setup (GSPro hid them while the ball was followed), once both
        the read and the shot's record are in. No swing came between the shot and the read, so GSPro's
        tiles are this shot's; unless they're exactly the shot before's, when this one's never came up.
        (The launch monitor's carry is no check: GSPro's can be well off it.)"""
        if seq not in self._tiles_due or seq not in self._late_tiles:
            return
        (shot_id, rec, before, unit, hc, missed), tiles = self._tiles_due.pop(seq), self._late_tiles.pop(seq)
        game, raw, total, offline = values = _tile_values(tiles, unit)
        # Only the fields both reads have.
        both = [(a, b) for a, b in zip(values, before or ()) if a is not None and b is not None]
        if both and all(a == b for a, b in both):
            log.info("[shot %s] tiles at the next setup (carry %s, total %s) are the shot before's: not kept",
                     seq, game, total)
            return
        self._shown_tiles = values
        # GSPro's round file's carry and total, live, stand: the screen adds only what the file hadn't.
        file_carry, file_total = self._file_shot_ids.pop(shot_id, (False, False))
        # A penalty GSPro's file has for it since (matched after the swing): no resting total.
        file_total = file_total or any(t.penalty for t in self._file_tracks if self._file_pins.get(t.shot_id) == shot_id)
        read = set(tiles)   # what the screen showed, for health, whichever of it the shot takes
        tiles = {k: v for k, v in tiles.items()
                 if not (k == "carry (game)" and file_carry or k == "total length" and file_total)}
        self.store.set_shot_tiles(shot_id, carry_game_yards=None if file_carry else game, carry_raw_tile_yards=raw,
                                  total_yards=None if file_total else total, offline_yards=offline)
        if not file_carry:
            rec.carry_game = game if game is not None else rec.carry_game   # for the hole recap
        rec.hud = replace(rec.hud, tiles={**rec.hud.tiles, **tiles})
        log.info("[shot %s] tiles read at the next setup (hidden in flight): carry %s, total %s, offline %s",
                 seq, game, total, offline)
        if hc is self._hc and self.cfg.health.enabled:
            hc.tiles_seen |= read
            for t in self.cfg.health.required_tiles:
                if t in read and t in missed and hc.tiles_missing.get(t):
                    hc.tiles_missing[t] -= 1
            if hc.full_shots >= 3 and set(self.cfg.health.required_tiles) <= hc.tiles_seen:
                self.health.clear("round.tiles_absent", "tiles read")

    def _fill_log_totals(self) -> None:
        """Totals from GSPro's logged positions for shots whose TOTAL LENGTH tile wasn't read."""
        try:
            if n := self.store.fill_log_totals():
                log.info("Filled the total of %d shot(s) from GSPro's logged positions", n)
        except Exception:
            log.exception("Filling totals from the log failed")

    def _apply_log_corrections(self) -> None:
        """Apply what GSPro's log has since said about recorded shots: strokes it took back
        (mulligans), flagged so they leave the stats, and holes picked up (scored at the maximum)."""
        g, r = self.gspro, self.round
        self._learn_file_slots()
        self._sync_scramble()
        # A score line that merely repeats is a mulligan when each player has their own ball and score,
        # and in a scramble, where each player hits once per stroke (the tracker leaves out the lines a
        # penalty on a ball that wasn't picked puts a stroke ahead). Alternate shot's partners take turns
        # on one score, which repeats for other reasons. Best ball is still one ball each.
        team = r is not None and r.format == "alternate"
        # A shamble selects drives too, but its partners then play their own balls: only a scramble's
        # strokes are each one partner's ball played on and the rest lifted.
        scramble = r is not None and r.format == "scramble" and not r.no_scramble
        for fire in sorted(self._starts_due):
            if fire not in self._fire_rows:
                self._starts_due.discard(fire)
            elif start := g.fire_info(fire).get("start"):
                self._starts_due.discard(fire)
                self.store.set_shot_position(self._fire_rows[fire][0], start)
                self._totals_due = True
        for fire, row in self._fire_rows.items():
            if (hole := g.fire_info(fire).get("hole")) is not None:
                row[1].log_hole = hole + 1   # its score line's: kept on the record past the log's memory
            if fire not in self._hazards_done and (entry := g.fire_info(fire).get("hazard")):
                self._hazards_done.add(fire)
                self.store.set_shot_hazard(row[0], entry)
            if (scramble and row[1].round_id == r.id and fire not in self._picks_done
                    and (picked := g.fire_info(fire).get("picked")) is not None):
                self._picks_done.add(fire)
                self.store.set_picked(row[0], picked)
                row[1].picked = picked
        if r is not None and r.format in TEAM_BALL:
            self._team_finishes(r)
        if self._pick_cards_due or self._pick_cards_kept:
            self._apply_pick_cards()
        for fire in sorted(self._owners_due):
            owner = g.fire_info(fire).get("player")
            if fire not in self._fire_rows or owner is not None or fire < g.fires - 20:
                self._owners_due.discard(fire)
            if fire in self._fire_rows and owner is not None and r is not None and not r.ended \
                    and self._fire_rows[fire][1].round_id == r.id:
                self._own_shot(fire, owner)
        if r is not None and not r.ended and self._log_truth:
            self._name_unnamed()
            self._settle_outcomes()
        for fire, kind in list(g.mulligan_fires.items()):
            if fire in self._mulligans_done or fire not in self._fire_rows:
                continue
            self._mulligans_done.add(fire)
            shot_id, rec, idx, hole = self._fire_rows[fire]
            # A player the team colours left alone plays their own ball and score (one profile alone in
            # alternate shot is more likely partners sharing it).
            lone = r is not None and bool(r.colour_teams) and r.solo(r.side_for(rec.player))
            if kind == "repeat" and team and not lone:
                continue
            self._totals_due = True
            rec.mulligan = True
            self.store.set_mulligan(shot_id)
            # No conceded-putt row to flag with it: a shot that ends in the gimme circle ends the hole,
            # and GSPro offers no shot options (so no mulligan) after that.
            # The re-hit replaces it: the same stroke, from the same spot.
            f2, nxt = next(((f, v) for f, v in sorted(self._fire_rows.items()) if f > fire and v[2] == idx),
                           (None, None)) if idx is not None else (None, None)   # an unnamed shot's re-hit can't be told from anyone's
            # On the hole GSPro's log credited each to: the hole read at impact is the last one's until the
            # new hole's first score line, so a tee shot and its re-hit can be read on different holes.
            if nxt and g.fire_info(f2).get("hole", nxt[3]) == g.fire_info(fire).get("hole", hole) \
                    and nxt[1].round_id == rec.round_id:
                nxt[1].shot_number, nxt[1].lie_before = rec.shot_number, rec.lie_before
                self.store.set_shot_start(nxt[0], rec.shot_number, rec.lie_before, replaced=shot_id)
            log.info("Shot %s was a mulligan (GSPro took it back): left out of the stats", shot_id)
        if self._totals_due:   # once the positions and mulligans above are all in
            self._totals_due = False
            self._fill_log_totals()
        while self._pickups_seen < len(g.pickups):
            self._marks_due.append(("pickup", *g.pickups[self._pickups_seen]))
            self._pickups_seen += 1
        while self._wrong_holes_seen < len(g.wrong_holes):
            idx, hole = g.wrong_holes[self._wrong_holes_seen]
            self._wrong_holes_seen += 1
            named = self._log_name(idx)
            log.warning("GSPro scored a shot by player %s (%s) on the wrong hole and corrected it on hole %s: "
                        "the round's scores there may be off", idx, named[1] if named else "unnamed", hole + 1)
        while self._pick_gimmes_seen < len(g.pick_gimmes):
            if r and r.format == "scramble":
                self._marks_due.append(("pick", *g.pick_gimmes[self._pick_gimmes_seen]))
            self._pick_gimmes_seen += 1
        for mark in list(self._marks_due):
            kind, idx, hole = mark
            live = kind == "pick"
            at, row = next(((f, v) for f, v in sorted(self._fire_rows.items(), reverse=True)
                            if v[2] == idx and v[3] == hole and not (live and v[1].mulligan)
                            and r is not None and v[1].round_id == r.id), (-1, None))
            if any(f > at and not (live and v[1].mulligan) and r is not None and v[1].round_id == r.id
                   and ((f in self._unnamed and v[1].player == "unknown")
                        or (v[2] is None and r is not None and r.slot_guess and v[1].player != "unknown"
                            and self._row_source.get(f) in ("screen", "setup read", "address frame")))
                   and self._fire_meta(f) == (idx, hole)
                   for f, v in self._fire_rows.items()):
                continue   # the shot it's about has no name (or, named by the screen, no slot) yet: settled here once it has
            self._marks_due.remove(mark)
            if kind == "pickup":
                if r and row and row[1].round_id == r.id and row[1].hole_seq:
                    r.picked_up.add((row[1].player, row[1].hole_seq))
                    self._picked_fires[at] = row[0]
                    log.info("%s picked up on hole %s of the round: scored at the maximum", row[1].player,
                             row[1].hole_seq)
            # A scramble pick inside the gimme circle concedes the picked ball's stroke. Its player usually
            # hit before a teammate, so that stroke's watch had already settled (as a miss) when GSPro
            # picked it, and the team's conceded putt went unrecorded.
            elif r and row and row[0] is not None and row[1].round_id == r.id and row[1].outcome in (None, "missed"):
                self._record_result(row[0], "conceded", f"[shot {row[0]}] (scramble pick)",
                                    putt=row[1].shot.is_putt, rec=row[1], from_log=True)

    def _record_result(self, shot_id: int | None, outcome: str, tag: str, putt: bool,
                       rec: ShotRecord | None = None, from_log: bool = False,
                       left_yards: float | None = None) -> bool | None:
        """Store how a stroke ended (False: a miss for a stroke GSPro's round file has made a full shot
        since, not recorded). A putt keeps its outcome, and so does a full shot that holed out;
        any stroke that ended inside the gimme circle also gets a row for the conceded putt GSPro
        counts but nobody hits, so putting stats (putts per hole, three-putts) see every putt on the
        card. A full shot that ended inside the circle finished on the green, whatever the HUD read."""
        if shot_id in self._conceded_ids:
            return   # a scramble pick already conceded it; a later watch can't undo or repeat that
        if shot_id in self._void_watch and not from_log:
            return   # its watch is on the player it was wrongly given to; _settle_outcomes grades it
        if shot_id is not None and shot_id not in self._before_result:
            self._before_result[shot_id] = (*self.store.finish_of(shot_id), rec is not None, rec and rec.hud.lie)
            while len(self._before_result) > 100:
                del self._before_result[next(iter(self._before_result))]
        # (the store's call once GSPro's round file has given one, else the live record's, else the store's)
        if outcome == "missed" and (stored := self.store.shot_is_putt(shot_id, tracked=True)) is None:
            stored = bool(rec.shot.is_putt) if rec is not None else self.store.shot_is_putt(shot_id)
        if outcome == "missed" and stored is False:
            return False   # (a watch started on a guessed putt GSPro's round file has made a full shot since)
        screen_putt = None
        if outcome in ("holed", "conceded") and (screen_putt := self.store.screen_putt(shot_id)) is None:
            # (putt or not as the record has it now, else the store: GSPro's round file may have called it
            # since the watch started)
            if (now := self.store.shot_is_putt(shot_id, tracked=True)) is not None:
                if rec is not None:
                    rec.shot = replace(rec.shot, putter=now)
            else:
                now = rec.shot.is_putt if rec is not None else self.store.shot_is_putt(shot_id)
            putt = putt if now is None else now
        elif outcome in ("holed", "conceded"):
            # (a club the store filled from GSPro's file since, which a stroke the hole ended on can't have:
            # putt or not as it was before, as the store has it once it takes the club back and
            # _sync_file_clubs follows)
            putt = screen_putt
            if rec is not None:
                rec.shot = replace(rec.shot, putter=putt)
        if outcome in ("holed", "conceded"):
            # (a miss settled while a putter the file filled made it a putt: a scramble's teammate played on)
            self.store.clear_miss(shot_id, full=screen_putt is False)
        if rec is not None:
            rec.outcome = outcome
        # (a watch started on a full shot GSPro's round file has since made a putt: stored as a putt's)
        if putt or outcome == "holed" or (rec is not None and rec.shot.is_putt):
            self.store.set_shot_result(shot_id, outcome=outcome)
        if outcome == "conceded" and shot_id is not None:
            self._conceded_ids.add(shot_id)
            left = left_yards if left_yards is not None else self._gimme_left(shot_id)   # (the file's, else the log's)
            if (gimme := self.store.record_gimme(shot_id, left)) is not None:
                self._gimme_ids[shot_id] = gimme
            log.info("%s conceded putt recorded", tag)
            if not putt:
                self.store.set_lie_after(shot_id, "green")
                if rec is not None and rec.hud.lie != "green":
                    rec.hud = replace(rec.hud, lie="green")
        return True

    def _gimme_left(self, shot_id: int | None) -> float | None:
        """How far from the hole (yards) GSPro logged the ball this shot left inside the gimme circle."""
        fire = next((f for f, v in self._fire_rows.items() if v[0] == shot_id), None) if shot_id else None
        m = self.gspro.fire_info(fire).get("left") if fire is not None else None
        return m * M_TO_YD if m is not None else None

    async def _late_result(self, shot_id: int | None, idx: int, base_outs: int, base_gim: int, base_shot: int,
                           tag: str, putt: bool, prediction: tuple | None = None, hole: str | None = None,
                           window: float = 60.0, rec: ShotRecord | None = None,
                           base_hole: int | None = None, base_setup: int | None = None,
                           fire: int | None = None) -> None:
        """Background: keep watching the log for how a stroke ended after commentary has moved on. A
        long missed putt only shows in the log when the next one is set up, well past the reaction
        window; a full shot can come to rest inside the gimme circle. Gives up after `window`."""
        try:
            outcome = await self._wait_putt_result(idx, base_outs, base_gim, base_shot, window, poll=0.5,
                                                   base_hole=base_hole, base_setup=base_setup, fire=fire)
            if outcome == "unknown" or (not putt and outcome == "missed"):
                return
            if self._record_result(shot_id, outcome, tag, putt, rec) is False:
                return
            if putt:
                log.info("%s putt outcome from GSPro log (late): %s", tag, outcome)
            if prediction and prediction[2] == hole:
                commentator, predicted_make, _ = prediction
                made = outcome == "holed"
                self.store.record_prediction(commentator, "putt", "make" if predicted_make else "miss",
                                             "made" if made else "missed", predicted_make == made)
        except Exception:
            log.exception("%s late result watch failed", tag)

    async def _wait_finish_yards(self, idx: int, at_impact: float | None, window: float,
                                 poll: float = 0.1) -> float | None:
        """Where the ball came to rest, in yards from the pin, from GSPro's `Logging tdist` line
        (written as the ball settles, just after the score line). None if it doesn't change from the
        impact value within the window, so a stale distance is never taken for this shot's. Nor is a
        scramble's pick copied onto the player (their teammate's ball, now theirs to play): Derek's
        nine-iron, 34 yards out, "leaves fourteen feet", Dan's ball (Bethpage, 11-16)."""
        deadline = time.monotonic() + window
        while True:
            now = self.gspro.to_pin_by.get(idx)
            if now is not None and now != at_impact and self.gspro.to_pin_from.get(idx, idx) == idx:
                return self.gspro.to_pin_of(idx)
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(poll)

    async def _wait_putt_result(self, idx: int, base_outs: int, base_gim: int, base_shot: int,
                                window: float, poll: float = 0.2, base_hole: int | None = None,
                                base_setup: int | None = None, setup_grace: float = 0.3,
                                no_setup_after: float = 2.0, fire: int | None = None) -> str:
        """Resolve a stroke from the GSPro log, no banner wait: 'holed' (a hole-out registered),
        'conceded' (a gimmie registered), or 'missed'. Returns 'unknown' when the window closes without
        a result, so the caller doesn't claim a definitive miss it never saw.

        `base_setup` is the player's setup count at impact. GSPro sets the shooter up again the moment
        the ball stops, and logs any gimmie right after that setup, so that first setup settles THIS
        stroke: a gimmie with it is this stroke's, none means it missed (after `setup_grace`, as the
        gimmie line can land a read later). Anything after belongs to a later stroke. Going by the
        stroke count alone got both wrong: a putt's own score line advances it a read before its gimmie
        lands (a conceded putt stored as a miss), and a chip whose score line didn't move (GSPro took
        it back) kept watching and claimed the next putt's gimmie. Without a setup count (or if no
        setup follows within `no_setup_after` of the count advancing) the advance is the miss signal.
        `base_hole` is the player's hole in the log at impact. On a tee shot the log can still be on
        the last hole (stroke 6 there), and the new hole's count restarts below that, so the player's
        hole moving on counts as the stroke being taken as well; without it a tee shot's watch ran on
        and claimed the gimme the player's next stroke earned. `fire` is this stroke's "Shot Fired"
        number: once the player has hit again, anything after is a later stroke's. In a scramble a ball
        that isn't picked gets no setup of its own, so without this a chip's watch ran on and claimed
        the gimme its player's putt earned a minute later."""
        g = self.gspro
        deadline = time.monotonic() + window
        setups = getattr(g, "setups_by", None) if base_setup is not None else None
        settled_at = advanced_at = None
        while True:
            now = time.monotonic()
            if fire is not None and g.fired_since(idx, fire):
                return "missed"
            if g.hole_outs_of(idx) > base_outs:
                return "holed"
            if setups is None or setups.get(idx, 0) > base_setup:
                if g.gimmies_of(idx) > base_gim:
                    return "conceded"
                if setups is not None:
                    settled_at = settled_at or now
                    if now - settled_at >= setup_grace:
                        return "missed"
            if g.shot_number(idx) > base_shot:  # stroke advanced: this stroke counted, next one is up
                if setups is None:
                    return "missed"
                advanced_at = advanced_at or now
                if now - advanced_at >= no_setup_after:
                    return "missed"
            if base_hole is not None and g.hole_by.get(idx, base_hole) != base_hole and g.shot_number(idx) > 1:
                return "missed"
            if now >= deadline:
                return "unknown"  # window passed with no log signal: not a definitive miss
            await asyncio.sleep(poll)

    async def _wait_for_file(self, before: Hud, tag: str, is_putt: bool, *, player: str | None,
                             ball_speed: float | None, impact: float, unit: str, seq: int | None = None,
                             since: float | None = None, known: str | None = None, shot_round=...
                             ) -> tuple[Hud, bool, round_file.Track | None, dict]:
        """Where the ball stopped, from GSPro's round file: GSPro writes each stroke there (every course
        mode, practice too) in the frame it has the ball at rest. The stroke with this shot's ball speed
        (and player) is waited for until _file_wait_over says it won't come (a ball struck since, the
        round ended); its lie, carry and total are the resting read. The screen isn't followed.

        Returns (the resting read, whether the ball was had at rest, the file's stroke or None, timings
        for the latency log). A putt isn't waited on (its result is the log's and the banner's): `before`,
        with the stroke the feed took it from, if any. No stroke: an empty read; the next setup's read of
        the tiles still gives the shot its numbers (_apply_late_tiles). Nor is anything waited for with no
        watch of the file running (the engine's, with GSPro's log followed, or the feed's, from before any swing: one
        begun after it would take this stroke for an old one), or from a GSPro before 3.59 (no such file)."""
        w = self._round_watch
        if w is None and isinstance(getattr(getattr(self.source, "gspro_log", self.source), "files", None),
                                    gspro_files.GsproFiles):
            w = self._round_watch_of()   # the feed's, watching since before the swing
        if w is None:
            return (before if is_putt else Hud()), False, None, {}
        if known is not None:   # the feed has read the file since this engine last looked, maybe
            await self._refresh_round_file()
        if is_putt:
            track = None
            if known is not None:   # a putt the feed took from the file: its stroke, unwaited
                track = self._file_track_of(player, ball_speed, since or impact, claim=seq, known=known)
            return before, False, track, {}
        if self.gspro.version and gspro_ui(self.gspro.version) == "classic":
            return Hud(), False, None, {}
        timing: dict = {}
        started = time.monotonic()
        seq0, ends0 = (seq if seq is not None else self._shot_seq), self.gspro.round_ends
        # the booth round it was struck in (`shot_round`, from impact; else the one on now, as _impact_hole)
        round0 = shot_round if shot_round is not ... else self.round if self.round and not self.round.ended else None
        round_then = self.round
        had_file = w is not None and w.entries is not None
        track, stop = None, None
        if ball_speed is None:
            stop = "it has no ball speed to know its stroke by"
        while stop is None:
            changed = self._file_changed   # taken before the look, so a write in between still wakes it
            if (track := self._file_track_of(player, ball_speed, since or impact, claim=seq,
                                             known=known)) is not None:
                break
            if (stop := self._file_wait_over(seq0, ends0, round0, started, round_then, had_file)) is not None:
                break
            if known is not None and any(isinstance(x, dict) and str(x.get("ShotID")) == known
                                         for x in self._file_entries):
                stop = "the stroke the feed took it from isn't one to take"   # (no write will change that)
                break
            if known is None and any(
                    (w.seen_at.get(t.shot_id) or -math.inf) >= (since or impact)
                    and abs(t.ball_speed - ball_speed) <= SAME_BALL_MPH for t in self._file_tracks):
                stop = "the stroke with its ball speed isn't this player's"   # (a name it can't match)
                break
            try:   # (woken by a write; polled too, for a file not yet there and the stops above)
                await asyncio.wait_for(changed.wait(), FILE_WAIT_POLL_S)
            except asyncio.TimeoutError:
                pass
            await self._refresh_round_file()
        if track is None:
            log.warning("%s no stroke in GSPro's round file %.1fs after impact, and %s: no resting read",
                        tag, time.monotonic() - impact, stop)
            return Hud(), False, None, timing
        timing["file"] = w.seen_at.get(track.shot_id, time.monotonic())
        # A stroke's penalty or drop is its own entry, written just after it: give it time to land.
        if (wait := FILE_SETTLE_S - (time.time() - w.written)) > 0:
            await asyncio.sleep(wait)
            await self._refresh_round_file()
            track = self._file_track_of(player, ball_speed, impact, track.shot_id) or track
        end = track.penalty_kind or ("holed" if track.in_cup else "dropped" if track.dropped
                                     else "picked up" if track.picked_up else "")
        log.info("%s ball at rest in GSPro's round file %.2fs after impact: lie %s, carry %s, total %s, "
                 "%s yd to the pin%s", tag, timing["file"] - impact, track.lie_after, track.carry_yards,
                 track.total_yards, track.finish_yards, f", {end}" if end else "")
        return self._file_after(Hud(), track, unit), True, track, timing

    def _file_wait_over(self, seq: int, round_ends: int, r, started: float, then=None,
                        had_file: bool = True) -> str | None:
        """Why a stroke not yet in GSPro's round file won't come for shot `seq` (struck in booth round
        `r`, its wait begun at `started` with `then` the booth's round), else None: a ball struck after
        it (its stroke would be the next), GSPro's round or the booth's ended (quit to the menu: GSPro
        writes no more of it; struck in none, the round started since, that may adopt it), the file gone
        (`had_file`: there when the wait began; GSPro never deletes it, but writes it first at a first
        stroke), or (GSPro hung, say) FILE_WAIT_MAX_S, far past any ball's roll."""
        if self._shot_seq > seq:
            return "another ball was struck"
        if self.gspro.round_ends > round_ends:
            return "GSPro's round ended"
        if r is not None and (r.ended or r.closing or self.round is not r):
            return "the booth's round ended"
        if r is None and (cur := self.round) is not None and cur is not then and (cur.ended or cur.closing):
            return "the booth's round ended"
        w = self._round_watch
        if had_file and (w is None or not w.path.exists()):
            return "the round file is gone"
        if time.monotonic() - started >= FILE_WAIT_MAX_S:
            return f"{FILE_WAIT_MAX_S:.0f}s passed"
        return None

    async def _log_file_latency(self, tag: str, timing: dict, fires0: int, impact: float) -> None:
        """When GSPro's round file had the shot at rest, next to its log's rest (the shooter's score line):
        for checking the latency on the sim PC."""
        def rest_at() -> float | None:
            return self.gspro.fire_info(self.gspro.fire_of(fires0, impact)).get("rest_at")
        deadline = time.monotonic() + 3.0
        while (logged := rest_at()) is None and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        f = timing["file"]
        log.info("%s rest latency: GSPro's round file %.2fs after impact; its log %s", tag, f - impact,
                 "none in 3s" if logged is None else f"{logged - impact:.2f}s ({f - logged:+.2f}s file vs log)")

    @staticmethod
    def _file_after(base: Hud, t: round_file.Track, unit: str) -> Hud:
        """The resting read from GSPro's round file: its lie, carry and total in the HUD's unit over
        `base`'s tiles."""
        tiles = dict(base.tiles)
        if t.penalty:
            tiles.pop("total length", None)   # a penalty's ball has no resting total (round_file.Track)
        for name, yd in (("carry (game)", t.carry_yards), ("total length", t.total_yards)):
            if yd is not None:
                tiles[name] = f"{yd if unit != 'metres' else yd / M_TO_YD:.1f}"
        return replace(base, lie=None if t.penalty else t.lie_after, tiles=tiles)

    async def _watch_picks(self) -> None:
        """A scramble's pick (GSPro logs "SelectShotToUse") is made from every ball the team hit, and GSPro's
        log has where only the picked one lies. GSPro's round file has them all (it writes each teammate's
        ball as it stops): each ball's yards to the pin and lie are taken from it while the pick is up."""
        seen = self.gspro.pick_screens
        while True:
            await asyncio.sleep(0.25)
            g = self.gspro
            if g.pick_screens == seen:
                continue
            seen, balls = g.pick_screens, dict(g.pick_balls)
            if len(balls) < 2:
                continue
            deadline = time.monotonic() + PICK_SCREEN_SECONDS
            while len(cards := self._file_pick_cards(balls)) < len(balls) and g.pick_screen_up \
                    and g.pick_screens == seen and time.monotonic() < deadline:
                await asyncio.sleep(0.25)   # (the file has each ball by now, bar a slow write)
            log.info("Pick from GSPro's round file: %s", ", ".join(
                f"{c.name} " + ("penalty" if c.penalty else f"{c.yards:.0f} yd {c.lie or 'unknown lie'}")
                for c in cards.values()) or "no ball in it")
            names = {idx: (self._log_name(idx) or (None,))[0] for idx in balls}
            self._pick_cards_due.append((balls, names, cards, time.monotonic()))   # (a ball missing: retried)
            self._apply_pick_cards()   # a CTP hole's tee shots are asked about now, not after the pick

    def _file_pick_cards(self, balls: dict[int, int]) -> dict[int, round_file.PickBall]:
        """Each ball of a pick (GSPro index -> its "Shot Fired") that GSPro's round file has: its player's
        name in the file, yards to the pin and lie where it stopped. A ball a penalty followed (out of
        bounds, played again) has no finish. The file's stroke is
        the one with the ball's launch numbers (LaunchExt Vars), else the one the live follow tied to its
        shot."""
        tracks = {t.shot_id: t for t in self._file_tracks}
        pinned = {sid: gid for gid, sid in self._file_pins.items()}
        out = {}
        for idx, fire in balls.items():
            t = None
            if launch := self.gspro.fire_info(fire).get("launch"):
                ids = {str(e["ShotID"]) for e in round_file.ball_entries(launch, self._file_entries)
                       if e.get("ShotID") is not None} & tracks.keys()
                t = tracks.get(next(iter(ids))) if len(ids) == 1 else None
            if t is None and (row := self._fire_rows.get(fire)) is not None:
                t = tracks.get(pinned.get(row[0]))
            if t is None:
                continue
            if t.penalty:
                out[idx] = round_file.PickBall(t.player, None, None, penalty=True)
            elif t.finish_yards is not None:   # unrounded: a CTP mark is to the inch
                holed = t.in_cup or (t.holed and not t.to_pin_m)   # (as finish_yards has it)
                out[idx] = round_file.PickBall(t.player, 0.0 if holed else t.to_pin_m * M_TO_YD, t.lie_after)
        return out

    def _apply_pick_cards(self) -> None:
        """Each pick card's yards and lie onto its ball's row (cards are by GSPro index, as the balls)."""
        self._recheck_pick_penalties()
        for entry in list(self._pick_cards_due):
            balls, gspro_names, cards, at = entry
            late = time.monotonic() - at > 60
            # Read again each time: a ball the file has only now (or its row), or a penalty written after it.
            cards.update(self._file_pick_cards(balls))
            if any(f not in self._fire_rows for f in balls.values()):
                if late:
                    self._pick_cards_due.remove(entry)
                continue
            names = {idx: gspro_names.get(idx) or self._fire_rows[f][1].player for idx, f in balls.items()}
            pairs = [(idx, card) for idx, card in cards.items() if idx in balls]
            # Whose ball each card is: the player GSPro's round file has it under, else the log's name for
            # its slot (the shot may not be named yet).
            r = self.round
            self._pick_ctp([(self._fire_rows[balls[idx]][1],
                             (r is not None and self._file_roster_key(r, card.name)) or names[idx], card)
                            for idx, card in pairs])
            # The log names the pick soon after: wait for it (a while), so the picked ball keeps its lie; and
            # for a ball not yet matched in the file.
            if (any(self._fire_rows[f][1].picked is None for f in balls.values()) or len(cards) < len(balls)) \
                    and not late:
                continue
            self._pick_cards_due.remove(entry)
            filled: dict[int, tuple] = {}   # (finish, lie, the lie it replaced) each ball was given
            self._pick_cards_kept.append((balls, cards, at, filled))
            for idx, card in pairs:
                shot_id, rec = self._fire_rows[balls[idx]][:2]
                if rec.mulligan:
                    continue
                if card.penalty:
                    # A penalty followed (out of bounds, lost): played again, so it didn't finish here.
                    log.info("Pick ball %s (%s): shot %s took a penalty, no finish", card.name, names[idx], shot_id)
                    continue
                yards = round(card.yards, 1)
                filled[idx] = (yards if self.store.fill_finish(shot_id, yards) else None, None, None)
                # The picked ball's lie is GSPro's log's (the team plays on from it); the file's only
                # stands in for it when there's none, and is the only word on the balls lifted. Until the
                # log says which was picked, a ball keeps the lie it has. A saved "tee" is no lie at all: the
                # stale hit-from read (Corey's opening drive at Bethpage).
                saved = self.store.finish_of(shot_id)[0]
                if card.lie and (rec.picked is False or not saved or saved.lower() == "tee"):
                    if (lie := self.store.set_lie_after(shot_id, card.lie)) and lie != saved:
                        filled[idx] = (filled[idx][0], lie, saved)
                log.info("Pick ball %s (%s): shot %s finished %.0f yd, %s", card.name, names[idx], shot_id,
                         card.yards, card.lie)

    def _recheck_pick_penalties(self) -> None:
        """A written pick's ball GSPro's round file has a penalty for only now (its entry read after the
        pick was): the finish written for it is taken back, and its CTP mark."""
        now = time.monotonic()
        for entry in list(self._pick_cards_kept):
            balls, cards, at, filled = entry
            if now - at > 60:   # (a last look at the file first)
                self._pick_cards_kept.remove(entry)
            for idx, card in self._file_pick_cards(balls).items():
                if not card.penalty or (cards.get(idx) and cards[idx].penalty) or balls[idx] not in self._fire_rows:
                    continue
                cards[idx] = card
                shot_id, rec = self._fire_rows[balls[idx]][:2]
                log.info("Pick ball %s: shot %s took a penalty after all, no finish", card.name, shot_id)
                if (wrote := filled.pop(idx, None)) is not None:
                    self.store.drop_finish(shot_id, *wrote)
                r = self.round
                who = (r is not None and self._file_roster_key(r, card.name)) or rec.player
                self._pick_ctp([(rec, who, card)])

    def _pick_ctp(self, balls: list[tuple]) -> None:
        """A scramble's tee shots on a closest-to-the-pin hole, from each ball's finish in GSPro's round
        file: whichever ball the team then plays, these are the marks (see _ctp_marks). `balls`: (shot
        record, its player, its card)."""
        from ..squabbit_live import feet_text

        r = self.round
        if r is None or r.ended or r.format != "scramble":
            return
        tees = [(rec, r.current_name(who), card) for rec, who, card in balls
                if who and who != "unknown"
                and (card.lie is not None or card.penalty)   # no finish: not known to be off the green
                and rec.round_id == r.id and rec.shot_number == 1 and not rec.mulligan
                and rec.hole_seq is not None and r.hole_number(rec.hole_seq) in r.ctp_holes]
        if not tees:
            return
        seq = tees[0][0].hole_seq
        tees = [(rec.shot_id, who, card) for rec, who, card in tees if rec.hole_seq == seq]
        got = {who: (card.yards if card.lie == "green" and not card.penalty else None, sid) for sid, who, card in tees}
        # One team's balls: the other teams' on this hole stay as they are.
        was = r.ctp_picks.get(seq, {})
        now = {p: y for p, y in was.items() if p not in got} | got
        if now != was:
            r.ctp_picks[seq] = now
            log.info("Scramble CTP from GSPro's round file, %s: %s", r.hole_name(seq),
                     ", ".join(f"{p} {feet_text(y)}" for p, (y, _) in got.items() if y is not None)
                     or "no tee shot on the green")
            self._squabbit_kick()

    def _save_review_image(self, tag: str, moment: str) -> None:
        folder = Path(self.cfg.log_dir) / "shots" / time.strftime("%Y-%m-%d")
        folder.mkdir(parents=True, exist_ok=True)
        try:
            self.screen.save_jpeg(folder / f"{time.strftime('%H%M%S')}-{tag.strip('[]').replace(' ', '')}-{moment}.jpg")
        except Exception:
            log.exception("Couldn't save review image")
