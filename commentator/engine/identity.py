"""Whose shot it is: GSPro's slots, names from the round file and the log, and each shot's row.

Part of Engine (commentator/engine/core.py), which is these parts together."""
import os
import shutil
import time
from dataclasses import replace
from pathlib import Path

from .common import GSPRO_LOG_KEEP_BYTES, KEEP_GSPRO_LOGS, STRATEGY_CHANCE, log
from .. import round_file
from ..facts import ordinal, to_yards
from ..hud import Hud
from ..minimap import MinimapRead
from ..round import Round, TEAM_BALL
from ..sources import gspro_files
from ..state import ShotRecord


class IdentityMixin:
    """Whose shot it is: GSPro's slots, names from the round file and the log, and each shot's row."""

    def _gspro_idx(self, player: str | None, scores: bool = False) -> int | None:
        """The GSPro player index for the name being commentated, for reading the log's per-player
        data (lie, distance, strokes, penalties). GSPro indexes players by setup slot, so the index is
        the slot position (slot 1 = 0). None when the log can't be safely attributed: not enabled, no
        round, alternate shot (shared ball) unless it's only for the `scores` (a hole's score is the
        team's, whichever partner's line carries it), or a MULTIPLAYER round not built from the setup
        slots (a tee-time roster is in honour order, so its position isn't GSPro's index)."""
        r = self.round
        if not self._log_truth or r is None or r.ended:
            return None
        # A player the team colours left alone beside alternate-shot pairs plays their own ball.
        if r.format == "alternate" and not scores and not (r.colour_teams and r.solo(r.side_for(player))):
            return None
        if r.slot_names:                       # captured setup slots: the trustworthy name -> index map
            name = r.current_name(player) if player else player   # a shot in flight may hold a pre-rename name
            # Teed after a name held on the first hole: their index waits on whether that's a player.
            if r.seq == 1 and r.held:
                g = name if name in r.roster else next((g for g, p in r.roster.items() if p and p == name), None)
                held = {h[0] for h in r.held.values()}
                if g in r.teed and any(n in held for n in r.teed[:r.teed.index(g)]):
                    return None
            pos = r.roster_position(name)
            return pos if pos is None or not r.slot_guess or self._slot_placed(pos) else None
        if len(r.roster) <= 1 and len(self._round_seen()) <= 1:
            return 0                           # genuinely one player (the log shows one too): index 0
        return None                            # no slot order and more than one player: don't attribute

    def _slot_placed(self, pos: int) -> bool:
        """In a guessed slot order, whether screen reads have put the player at `pos` there: their own
        reads, or every other player's when the log has no more players than the order names."""
        r = self.round
        g = r.slot_names[pos]
        if r.slot_read(g) == pos:
            return True
        others = [(i, n) for i, n in enumerate(r.slot_names) if n and i != pos]
        return None not in r.slot_names and self._log_view()[2] <= set(range(len(r.slot_names))) \
            and all(r.slot_read(n) == i for i, n in others)

    def _read_slot(self, r: Round, owner: int, player: str) -> None:
        """A screen read named `player` on a shot GSPro's log gave slot `owner`: with a guessed slot order,
        that places them. When the reads move anyone, GSPro's log is re-read under the new order (its
        scores were filed under whoever the guess put in each slot)."""
        name = r.current_name(player)
        g = next((n for n in (r.slot_names or []) if n and (self._player(n) or n) == name), None)
        if g is None:
            return
        was = list(r.slot_names)
        r.note_slot_read(g, owner)
        r.learn_slots()
        if r.slot_names != was:
            self.store.round_players(r.uuid, dict(r.roster), r.slot_names)
        if self._recheck_slots(r, was):
            self._sync_log_scores(r)   # renames the shots it can, too

    def _recheck_slots(self, r: Round, was: list[str | None]) -> bool:
        """In a guessed slot order, undo what GSPro's log was read into under placements that no longer
        hold: the order `was` changed, or a player's slot changed or was lost (reads now conflict, or the
        log showed a slot that leaves the last one open). Whether anything was undone."""
        if r is not self.round or r.ended or not r.slot_guess:
            return False
        order = list(r.slot_names or [])
        now = {n: self._gspro_idx(n, scores=True) for n in order if n}
        held = r.slot_held
        changed = {n for n, i in held.items() if now.get(n) != i}
        r.slot_held = {n: i for n, i in now.items() if i is not None}
        if order == was and not changed:
            return False
        if order != was:
            log.info("Round %s: screen reads place the players in GSPro's slots %s (the guess was %s)",
                     r.id, order, was)
        else:
            log.info("Round %s: GSPro's slots for %s are no longer placed", r.id, sorted(changed))
        r.log_doubt.clear()   # doubts about the old order's slots
        for n in was:
            if n and (n in changed or n not in order or order.index(n) != was.index(n)):
                r.log_scores.pop(n, None)
        moved = {i for i in range(max(len(was), len(order)))
                 if (was[i] if i < len(was) else None) != (order[i] if i < len(order) else None)}
        moved |= {held[n] for n in changed}
        # (a shot GSPro's log has its slot's own player hitting keeps its finish there: followed on its
        # own line all along, whatever name it goes by)
        own = {sid: self._fire_meta(f)[0] for f, (sid, *_) in self._fire_rows.items() if sid is not None}
        for cache in (self._pending_finish, self._last_row):
            for i in moved:
                if (v := cache.get(i)) is None or v[0] is None or own.get(v[0]) != i:
                    cache.pop(i, None)
        for fire, (shot_id, rec, slot, hole) in list(self._fire_rows.items()):
            if rec.round_id != r.id or slot not in moved or rec.player == "unknown":
                continue
            if self._row_source.get(fire) in ("screen", "setup read", "address frame"):
                if (own := self._fire_meta(fire)[0]) is None or own == slot:
                    continue   # followed on its own line of the log all along (or there's no telling): it stands
                self._requeue_marks(r, fire, shot_id, rec, slot, hole)
                # Its result was followed on the slot the old order gave its name: graded again on its own.
                self._void_result(shot_id, rec)
                self._resettle.add(fire)
                self._fire_rows[fire] = (shot_id, rec, None, None)   # _follow_placed gives it its slot
            else:   # named from the log's slot under the old order: the slot's right, the name may not be
                self._renames.add(fire)
        for fire, (_, rec, slot, _) in self._slot_archive.items():
            if rec.round_id == r.id and slot in moved and rec.player != "unknown":
                self._renames.add(fire)
        self._scramble_pairs = set()   # the log's teams are by slot: re-read them under the new order
        return True

    def _requeue_marks(self, r: Round, fire: int, shot_id: int | None, rec: ShotRecord, slot: int,
                       hole: int | None) -> None:
        """A shot followed on the wrong slot: a pick-up it was given goes back to the log's marks, given
        again once it's followed; so does a scramble pick's concession, which voiding its result clears."""
        if self._picked(fire, shot_id):
            self._picked_fires.pop(fire)
            self._drop_pickup(r, rec.player, rec.hole_seq)
            if ("pickup", slot, hole) not in self._marks_due:
                self._marks_due.append(("pickup", slot, hole))
        if shot_id in self._conceded_ids and (slot, hole) in self.gspro.pick_gimmes[:self._pick_gimmes_seen] \
                and ("pick", slot, hole) not in self._marks_due:
            self._marks_due.append(("pick", slot, hole))

    def _file_roster_key(self, r: Round, name: str) -> str | None:
        """The round's GSPro name for a name GSPro's round file has (typed, not read off the screen): the
        same, the one it fits word for word (round_file.names_fit), else the one the league player it
        names goes by. None when no single one does."""
        if name in r.roster:
            return name
        for fits in ([g for g in r.roster if g.casefold() == name.casefold()],
                     [g for g in r.roster if round_file.names_fit(name, g) or round_file.names_fit(g, name)],
                     [g for g in r.roster if (p := self._file_player(name)) and r.roster.get(g) == p]):
            if len(fits) == 1:
                return fits[0]
            if fits:
                return None
        return None

    def _learn_file_slots(self) -> None:
        """Who's in each of GSPro's slots, from GSPro's own records: each of the round's shots the log
        credited to a slot (its shooter's score line, once the ball stopped) is tied by its launch
        numbers to the round file's stroke, which names the player (round_file.slot_players; GSPro 4.x,
        which logs the numbers). Once known, a slot's name is taken over the setup screen's and the
        screen reads' (Round.file_slots, Round.slot_read): an order they got wrong is re-read as a
        guess, its players placed where the file says, and the log's attributions under it are undone
        (_recheck_slots). Until then, the screen's stand."""
        r, g = self.round, self.gspro
        if r is None or r.ended or not self._log_truth:
            return
        if self._file_slots_from is None or self._file_slots_from[0] != r.id:
            self._file_credits, self._file_slots_from = {}, (r.id, -1, None)
        ours = {f for f, v in self._fire_rows.items() if v[1].round_id == r.id}
        for fire in range(max(1, g.fires - 39), g.fires + 1):
            if fire in self._file_credits or (fire <= self._hc.fires_at_start and fire not in ours):
                continue   # (an earlier round's, unless the round adopted it)
            info = g.fire_info(fire)
            if info.get("player") is not None and info.get("launch"):
                self._file_credits[fire] = (info["player"], info["launch"])
        key = (r.id, len(self._file_credits), self._file_version)
        if key == self._file_slots_from or not self._file_credits:
            return
        self._file_slots_from = key
        slots: dict[str, int] = {}
        clash: set[str] = set()
        for idx, name in round_file.slot_players(self._file_credits.values(), self._file_entries).items():
            if (k := self._file_roster_key(r, name)) is not None:
                if k in slots:
                    clash.add(k)
                slots[k] = idx
        slots = {k: i for k, i in slots.items() if k not in clash}
        if slots == r.file_slots:
            self._own_misread(r)   # (a ball the file has only now)
            return
        r.file_slots = slots
        was = list(r.slot_names or [])
        order = list(was)

        def seat(k: str) -> None:
            if None in order:
                order[order.index(None)] = k
            else:
                order.append(k)
        if not order:
            if len(r.roster) < 2:
                return   # (one player: GSPro's index 0 already)
            order = [None] * (max(slots.values()) + 1)
            for k, i in slots.items():
                order[i] = k
            for k in r.roster:   # the rest where there's room: a guess, till the file places them too
                if k not in order:
                    seat(k)
            r.slot_guess = True
        elif any(i >= len(order) or order[i] != k for k, i in slots.items()):
            if not r.slot_guess:
                log.info("Round %s: GSPro's round file has %s in its slots, not as the setup screen's %s: "
                         "going by the file", r.id, slots, order)
                # What the setup order placed is what _recheck_slots undoes where it no longer holds (a
                # player left where the setup had them, but no longer known to be there, too).
                r.slot_held = {**{k: i for i, k in enumerate(was) if k}, **r.slot_held}
                r.slot_guess, r.setup_slots = True, None
            for k in slots:
                if k not in order:
                    seat(k)
        elif not r.slot_guess:
            log.info("Round %s: GSPro's round file confirms the slots of %s", r.id, sorted(slots))
            self._own_misread(r)
            return
        r.slot_names = order
        r.learn_slots()
        if r.slot_names != was:
            log.info("Round %s: GSPro's round file places the players in GSPro's slots %s (was %s)",
                     r.id, r.slot_names, was)
            self.store.round_players(r.uuid, dict(r.roster), r.slot_names)
        if self._recheck_slots(r, was):
            self._sync_log_scores(r)
        self._own_misread(r)

    def _own_misread(self, r: Round) -> None:
        """Shots the screen named for someone else in a slot GSPro's round file has just named, whose
        score line came in before the file was matched: the slot's player's (_own_shot). Those it named
        right, with no slot yet (or followed on the one an old order gave the name), are graded on it."""
        self._follow_placed(r)
        for fire, (owner, _) in list(self._fire_log.items()):
            row = self._fire_rows.get(fire)
            if row is None or row[1].round_id != r.id or fire in self._renames or owner not in r.file_slots.values() \
                    or self._row_source.get(fire) not in ("screen", "setup read", "address frame"):
                continue
            if (known := self._log_name(owner)) is not None and r.file_slots.get(known[0]) == owner \
                    and r.current_name(row[1].player) != known[1] and not self._anonymous(known[1]) \
                    and self._file_hit_by(fire, known[0]):
                self._file_rename(fire, owner, known[1])

    def _file_hit_by(self, fire: int, g: str) -> bool:
        """Whether GSPro's round file has this fire's own ball (its launch numbers) under round player `g`:
        the slot's player as the file named it may since have moved (a player who left closes it up)."""
        launch = self.gspro.fire_info(fire).get("launch")
        if not launch or self.round is None:
            return False
        names = {str(e["PlayerName"]) for e in round_file.ball_entries(launch, self._file_entries)
                 if e.get("PlayerName")}
        return len(names) == 1 and self._file_roster_key(self.round, next(iter(names))) == g

    def _file_rename(self, fire: int, owner: int, name: str) -> None:
        """GSPro's own records name the slot that hit a shot (_learn_file_slots): the screen misread who
        was up, so the shot is `name`'s."""
        shot_id, rec, _, _ = self._fire_rows[fire]
        was = rec.player
        log.info("Shot %s: the screen said %s hit it, GSPro's log and round file have %s in slot %d: it's "
                 "theirs", shot_id, rec.player, name, owner + 1)
        # Its finish and result were followed on the misread player's slot: void them, so the next
        # shot from that slot isn't taken as where this ball went, and grade it on its own slot. Ones
        # followed on its own slot already (a misread setup order put the wrong name on the right slot)
        # stand, and go with the shot (its conceded putt too: _rename_remapped).
        _, _, slot, hole = self._fire_rows[fire]
        regrade = slot != owner
        if regrade:
            if slot is not None and self.round is not None:
                self._requeue_marks(self.round, fire, shot_id, rec, slot, hole)
            for cache in (self._pending_finish, self._last_row):
                for k in [k for k, v in cache.items() if v is not None and v[0] == shot_id]:
                    del cache[k]
                # Its owner's earlier swing isn't their last any more: their next setup is where this one stopped.
                # (by swing, not row: a slow handler can store an earlier swing's row after a later one's)
                if (prev := cache.get(owner)) is not None and prev[0] is not None and shot_id is not None:
                    pf = next((f for f, v in self._fire_rows.items() if v[0] == prev[0]), None)
                    if (pf < fire) if pf is not None else prev[0] < shot_id:
                        del cache[owner]
            self._void_result(shot_id, rec)
        self._fire_rows[fire] = (shot_id, rec, owner, self.gspro.fire_info(fire).get("hole"))
        self._renames.add(fire)
        self._rename_remapped(self.round)
        if shot_id is not None and rec.player != was and self._anonymous(was) != self._anonymous(rec.player):
            # (a shared profile's shot, the file's shooter's own: it counts in their stats, or the reverse)
            gimme = self._gimme_ids.get(shot_id) if shot_id in self._conceded_ids else None
            self.store.set_attributed([shot_id] + ([gimme] if gimme is not None else []),
                                      not self._anonymous(rec.player))
        if shot_id is not None and rec.player != was:
            # Stored as the misread player's next stroke, it unholed their hole-out: that stands again.
            for x in self.state.shots:
                if x.round_id == rec.round_id and x.player == was and x.hole_seq == rec.hole_seq \
                        and x.outcome == "holed" and x.shot_id is not None and x.shot_id < shot_id:
                    self.store.set_shot_result(x.shot_id, outcome="holed")
        if regrade:
            self._outcomes_due.add(fire)
            self._resettle.add(fire)   # graded however long ago, while the log still has it

    def _forget_fires(self, upto: int) -> None:
        """Let the recorded rows of fires `upto` and before go (keeping a guessed slot order's log-named
        ones for the round, for a later re-map)."""
        r = self.round
        for old in [f for f in self._fire_rows if f <= upto]:
            gone = self._fire_rows.pop(old)
            self._fire_log.pop(old, None)
            self._resettle.discard(old)
            if r is not None and r.slot_guess and gone[1].round_id == r.id and gone[1].player != "unknown" \
                    and gone[2] is not None and self._row_source.get(old) not in ("screen", "setup read", "address frame"):
                self._slot_archive[old] = gone
            self._row_source.pop(old, None)
        if r is None or any(v[1].round_id != r.id for v in self._slot_archive.values()):
            self._slot_archive = {f: v for f, v in self._slot_archive.items() if r and v[1].round_id == r.id}

    def _rename_remapped(self, r: Round) -> None:
        """Shots named from GSPro's log by a slot order since changed: once the slot's player is known
        again, the shot (its result, conceded putt and pick-up with it) is theirs."""
        dropped: list[tuple[str, int | None]] = []
        for fire in sorted(self._renames):
            row = self._fire_rows.get(fire) or self._slot_archive.get(fire)
            if row is None or row[1].round_id != r.id or row[1].player == "unknown" or row[2] is None:
                self._renames.discard(fire)
                continue
            shot_id, rec, slot, _ = row
            if (known := self._log_name(slot)) is None:
                continue
            self._renames.discard(fire)
            old, new = rec.player, known[1]
            if r.current_name(old) == new:
                continue
            rec.player = new
            shared = not r.solo(r.side_for(new)) and (r.format == "scramble" or r.no_scramble or self.gspro.team_scores)
            if shot_id is not None:
                self.store.move_shot_player(shot_id, new, fmt=r.player_format(new), shared_ball=shared,
                                            gimme_id=self._gimme_ids.get(shot_id) if shot_id in self._conceded_ids
                                            else None)
            if self._picked(fire, shot_id):
                dropped.append((old, rec.hole_seq))
                r.picked_up.add((new, rec.hole_seq))
            self._totals_due = True
            log.info("Shot %s went to %s by GSPro's slot %d under the old slot order: it's %s's", shot_id, old,
                     slot + 1, new)
        for old, seq in dropped:   # after them all: two players' shots on one hole may have swapped
            self._drop_pickup(r, old, seq)

    def _picked(self, fire: int, shot_id: int | None) -> bool:
        """Whether a GSPro pick-up was given to this fire's row."""
        return fire in self._picked_fires and self._picked_fires[fire] == shot_id

    def _drop_pickup(self, r: Round, name: str, seq: int | None) -> None:
        """Take back `name`'s pick-up on hole `seq`, unless another row a pick-up was given to is still theirs."""
        for f, sid in self._picked_fires.items():
            row = self._fire_rows.get(f) or self._slot_archive.get(f)
            if row and row[0] == sid and row[1].round_id == r.id and row[1].player == name and row[1].hole_seq == seq:
                return
        r.picked_up.discard((name, seq))

    def _follow_placed(self, r: Round, skip: int | None = None) -> None:
        """Shots the screen named before screen reads placed their player in a guessed slot order: GSPro's
        log now says whose slot they were, so their results are followed from it too."""
        for fire, (shot_id, rec, slot, _) in list(self._fire_rows.items()):
            if fire == skip or slot is not None or rec.round_id != r.id or rec.player == "unknown" \
                    or self._row_source.get(fire) not in ("screen", "setup read", "address frame"):
                continue
            owner, hole = self._fire_meta(fire)
            if owner is not None and (known := self._log_name(owner)) is not None \
                    and r.current_name(rec.player) == known[1]:
                self._fire_rows[fire] = (shot_id, rec, owner, hole)
                self._outcomes_due.add(fire)

    def _log_name(self, idx: int | None) -> tuple[str, str] | None:
        """(GSPro name, booth name) of the player in GSPro slot `idx`, when the round maps the log's
        index to a name both ways (the same guards as `_gspro_idx`)."""
        r = self.round
        if idx is None or r is None or r.ended:
            return None
        order = r.slot_names or list(r.roster)
        if not 0 <= idx < len(order) or not order[idx]:
            return None
        g = order[idx]
        name = self._player(g) or g   # a league player goes by their league name, as the screen's read does
        return (g, name) if self._gspro_idx(name) == idx else None

    def _solo_name(self) -> str | None:
        """The round's only player, when GSPro's log agrees there's only one: whoever hit, it was them."""
        r = self.round
        if not self._log_truth or r is None or r.ended or len(r.roster) != 1 or len(self._round_seen()) > 1:
            return None
        known = self._log_name(0)
        return known[1] if known else None

    def _round_seen(self) -> set:
        """The GSPro players the round has had: those before the booth started too, and while the round
        GSPro ended is closing, its own (not the next round's, whatever GSPro is on)."""
        g, r = self.gspro, self.round
        if r is not None and not r.ended and (g.round_ends != self._log_round_ends or self._log_ended_round == r.id):
            return g.last_players_seen
        return g.players_seen | g.primed_seen

    def _log_view(self) -> tuple[dict, dict, set, set]:
        """GSPro's log's (finished hole scores, penalties, players seen, holes picked up) for the round:
        the live ones, or once GSPro has ended the round (and reset them for the next), the ended round's:
        in the menus, or while the round GSPro ended is still closing (GSPro may be on the next already)."""
        g, r = self.gspro, self.round
        closing = r is not None and not r.ended \
            and (g.round_ends != self._log_round_ends or self._log_ended_round == r.id)
        if g.mode == "menu" or closing:
            return g.last_hole_scores, g.last_penalties, g.last_players_seen, g.last_picked
        return g.hole_scores, g._penalties, g.players_seen | g.primed_seen, g._picked

    def _setup_slot(self, idx: int, g: str) -> bool:
        """Whether GSPro's setup screen showed `g` in slot `idx` (and the round still has them there)."""
        r = self.round
        return bool(r.setup_slots) and idx < len(r.setup_slots) and r.setup_slots[idx] == g \
            and idx not in r.log_doubt

    def _slot_trust(self, owner: int, g: str) -> str | None:
        """Why GSPro's log index `owner` is known to be `g`, or None:
        - GSPro's round file names them in that slot (_learn_file_slots), or another player there;
        - two screen reads named the player the log had up there;
        - the setup screen showed them in that slot, and no read has said otherwise;
        - every other player the log has seen is known, and the round, its slots and the log all
          have the same number of players: the one slot left is theirs (a round's only player too)."""
        r = self.round
        if r.file_slots.get(g) == owner:
            return "GSPro's round file names the slot"
        if owner in r.file_slots.values() or g in r.file_slots:
            return None
        if r.log_agree.get((owner, g), 0) >= 2:
            return "screen reads agreed"
        if r.slot_guess and r.slot_read(g) == owner:
            return "screen reads placed them"
        if owner in r.log_doubt:
            return None
        if self._setup_slot(owner, g):
            return "the setup screen's slots"
        seen = self._log_view()[2] | {owner}
        named = [n for n in (r.slot_names or list(r.roster)) if n]
        if len(named) != len(seen) or len(seen) != len(r.roster):
            return None
        for i in seen - {owner}:
            k = self._log_name(i)
            if k is None or i in r.log_doubt or not (r.log_agree.get((i, k[0]), 0) >= 2 or self._setup_slot(i, k[0])
                                                    or r.file_slots.get(k[0]) == i
                                                    or (r.slot_guess and r.slot_read(k[0]) == i)):
                return None
        return "every other slot is known" if len(seen) > 1 else "the round's only player"

    def _own_shot(self, fire: int, owner: int) -> None:
        """GSPro's log has said whose shot a recorded row was (its shooter's score line, after the ball
        stopped). The screen's name stands whenever it was read: each read that matches the log confirms
        that slot, one that doesn't unconfirms it. A shot recorded with no name (GSPro hid it for the
        shot) waits in `_unnamed` for `_name_unnamed` to work out whose it was."""
        shot_id, rec, _, _ = self._fire_rows[fire]
        r = self.round
        self._fire_log[fire] = (owner, self.gspro.fire_info(fire).get("hole"))
        if self._row_source.get(fire) == "the round's only player" and owner != 0:
            self._unassign_row(fire, owner)   # there's a second player after all, and it was theirs
        # (one GSPro's round file named with no slot yet waits there too, for the log to agree)
        if rec.player == "unknown" or (shot_id in self._file_named and self._fire_rows[fire][2] is None):
            if shot_id is not None:
                # This player's earlier shot isn't their last any more: their next setup is where THIS
                # ball stopped, not that one's.
                for cache in (self._pending_finish, self._last_row):
                    if (prev := cache.get(owner)) is not None and prev[0] is not None and prev[0] < shot_id:
                        del cache[owner]
            self._unnamed.add(fire)
            return
        if r.slot_guess and self._row_source.get(fire) in ("screen", "setup read", "address frame"):
            self._read_slot(r, owner, rec.player)
            self._follow_placed(r, skip=fire)
        if self._row_source.get(fire) not in ("screen", "setup read", "address frame") or (known := self._log_name(owner)) is None:
            return   # only a read of the screen confirms the log's slots, not a name the log gave
        g, name = known
        if r.current_name(rec.player) == name:
            r.log_agree[(owner, g)] = r.log_agree.get((owner, g), 0) + 1
            if self._fire_rows[fire][2] is None:
                # Named before the round was open (struck off a course's range): with no slot then, its
                # log corrections and result were never followed. The log agrees whose it is.
                self._fire_rows[fire] = (shot_id, rec, owner, self.gspro.fire_info(fire).get("hole"))
                self._outcomes_due.add(fire)
        elif r.file_slots.get(g) == owner and not self._anonymous(name) and self._file_hit_by(fire, g):
            self._file_rename(fire, owner, name)
        else:
            for k in [k for k in r.log_agree if k[0] == owner]:
                del r.log_agree[k]
            r.log_doubt.add(owner)
            r.log_disagreed += 1
            log.info("Shot %s: the screen said %s hit it, GSPro's log has %s in slot %d: keeping the "
                     "screen's, and not naming that slot's shots from the log", shot_id, rec.player, name, owner + 1)

    def _name_unnamed(self) -> None:
        """Name the shots recorded with no name, as soon as there's good reason (each also confirms the
        slot): GSPro's log's shooter, when their slot is known (see _slot_trust); the only player still
        on a hole everyone else had finished when it was hit; the one player a finished hole's strokes
        on the log are short for, by just the unnamed shots on it. One GSPro's round file named
        (_sync_file_names) waits for the log to agree, then is graded on that player's line."""
        g, r = self.gspro, self.round
        for fire in sorted(self._unnamed):
            row = self._fire_rows.get(fire)
            if row is None or row[1].round_id != r.id or fire < g.fires - 30 \
                    or (row[1].player != "unknown" and row[0] not in self._file_named):
                self._unnamed.discard(fire)
                continue
            owner = g.fire_info(fire).get("player")
            if (known := self._log_name(owner)) is None or self._anonymous(known[1]):
                continue
            if row[1].player != "unknown":
                if known[1] == row[1].player:
                    self._unnamed.discard(fire)
                    self._fire_rows[fire] = (row[0], row[1], owner, g.fire_info(fire).get("hole"))
                    self._outcomes_due.add(fire)
                continue
            if why := self._slot_trust(owner, known[0]):
                self._assign_row(fire, owner, known[1], f"GSPro's log ({why})")
            elif self._last_on_hole(fire, known[1]):
                self._assign_row(fire, owner, known[1], "the last player on the hole")
                r.log_agree[(owner, known[0])] = r.log_agree.get((owner, known[0]), 0) + 1
        self._reconcile_strokes()

    def _last_on_hole(self, fire: int, name: str) -> bool:
        """Whether every other player had finished the hole (the screen named their last stroke, and it
        holed or was conceded) before this shot was hit on it, leaving `name` the only one who could."""
        r = self.round
        hole = self.gspro.fire_info(fire).get("hole")
        if hole is None or r.format in TEAM_BALL or len(r.roster) != len(self._log_view()[2]):
            return False
        names = {self._player(g) or g for g in r.roster}
        done = {r.current_name(rec.player) for f, (_, rec, _, h) in self._fire_rows.items()
                if f < fire and h == hole and rec.round_id == r.id and not rec.mulligan
                and rec.outcome in ("holed", "conceded") and self._row_source.get(f) in ("screen", "setup read", "address frame")}
        return names - done == {name}

    def _reconcile_strokes(self) -> None:
        """A hole every player has finished on GSPro's log, with shots on it no one's name was read for:
        when exactly one player's strokes (penalties aside) are short of their score, by just that many
        shots, and everyone else's add up, they're that player's."""
        g, r = self.gspro, self.round
        if r.format in TEAM_BALL or g.team_scores or len(r.roster) < 2:
            return   # one player's shots are named by _slot_trust; a shared ball's strokes aren't one player's
        scores, pens, seen, picked = self._log_view()
        unnamed: dict[int, list[int]] = {}
        for fire in self._unnamed:
            if (hole := g.fire_info(fire).get("hole")) is not None and (fire in self._fire_rows) \
                    and self._fire_rows[fire][1].player == "unknown":
                unnamed.setdefault(hole, []).append(fire)
        names = {self._player(n) or n: self._gspro_idx(self._player(n) or n) for n in r.roster}
        if None in names.values() or len(seen) != len(names):
            return
        for hole, fires in unnamed.items():
            if (r.id, hole) in self._reconciled_holes or any((i, hole) not in scores or (i, hole) in picked
                                                     for i in names.values()):
                continue
            live = [f for f in fires if not self._fire_rows[f][1].mulligan]
            holed = any(g.fire_info(f).get("holed") for f in live)
            short: list[str] = []
            for name, idx in names.items():
                rows = [rec for f, (_, rec, _, _) in self._fire_rows.items()
                        if g.fire_info(f).get("hole") == hole and rec.round_id == r.id and not rec.mulligan
                        and rec.player != "unknown" and r.current_name(rec.player) == name]
                # GSPro counts the conceded putt nobody hits: on the row that ended inside the circle,
                # or, when that's not on the record yet, still to account for.
                ended = any(rec.outcome in ("holed", "conceded") for rec in rows)
                gap = scores[(idx, hole)] - pens.get((idx, hole), 0) - len(rows) - any(
                    rec.outcome == "conceded" for rec in rows)
                if live and gap == len(live) + (not ended and not holed):
                    short.append(name)
                elif gap not in (0, 1) or (gap == 1 and ended):
                    short = []
                    break
            if len(short) != 1:
                continue
            name = short[0]
            if any((o := g.fire_info(f).get("player")) is not None and o != names[name] for f in live):
                continue   # the log says one of them was someone else's: something else is off
            self._reconciled_holes.add((r.id, hole))
            for f in live:
                self._assign_row(f, names[name], name, "the hole's strokes on GSPro's log")

    def _assign_row(self, fire: int, owner: int, name: str, why: str) -> None:
        """Name a shot recorded with no name: its row, the stats it counts in, and its index for the log
        corrections that follow (mulligans, pickups, scramble picks)."""
        shot_id, rec, _, _ = self._fire_rows[fire]
        r = self.round
        self._unnamed.discard(fire)
        rec.player = name
        shared = not r.solo(r.side_for(name)) and (r.format == "scramble" or r.no_scramble or self.gspro.team_scores)
        self.store.set_shot_player(shot_id, name, attributed=True, fmt=r.player_format(name), shared_ball=shared)
        self._fire_rows[fire] = (shot_id, rec, owner, self.gspro.fire_info(fire).get("hole"))
        self._row_source[fire] = why
        if r.name_sources.get("none yet"):
            r.name_sources["none yet"] -= 1
        r.name_sources[why.split(" (")[0]] = r.name_sources.get(why.split(" (")[0], 0) + 1
        self._totals_due = True
        self._outcomes_due.add(fire)
        log.info("Shot %s had no name on screen: %s's, by %s", shot_id, name, why)

    def _unassign_row(self, fire: int, owner: int, why: str | None = None) -> None:
        """Take back the name a shot was given (as the round's only player, or `why`), when GSPro's log
        says another slot hit it: it's unnamed until _name_unnamed works out whose it was."""
        shot_id, rec, _, _ = self._fire_rows[fire]
        self._fire_rows[fire] = (shot_id, rec, None, None)   # not the slot it was given to's any more
        r = self.round
        if why:
            log.info("Shot %s went to %s by GSPro's slot %d, but %s: unnamed until its player is known",
                     shot_id, rec.player, owner + 1, why)
        else:
            log.info("Shot %s went to %s as the round's only player, but GSPro's log has slot %d hitting it: "
                     "unnamed until its player is known", shot_id, rec.player, owner + 1)
        for cache in (self._pending_finish, self._last_row):
            for k in [k for k, v in cache.items() if v is not None and v[0] == shot_id]:
                del cache[k]
        self._void_result(shot_id, rec)
        rec.player = "unknown"
        self.store.set_shot_player(shot_id, "unknown", attributed=False, fmt=None, shared_ball=False)
        self._row_source[fire] = "none yet"
        if r.name_sources.get("the round's only player"):
            r.name_sources["the round's only player"] -= 1
        r.name_sources["none yet"] = r.name_sources.get("none yet", 0) + 1
        self._totals_due = True

    def _void_result(self, shot_id: int | None, rec: ShotRecord) -> None:
        """Take back the result a shot was given on the wrong player's line of GSPro's log (and its
        conceded putt): its watch is void, and _settle_outcomes grades it on its own slot."""
        if shot_id is not None:
            self._void_watch.add(shot_id)
            if rec.outcome is not None:
                before = self._before_result.pop(shot_id, None)
                self.store.clear_shot_result(shot_id, rec.outcome, finish=before and before[:2],
                                             gimme_id=self._gimme_ids.pop(shot_id, None))
                if before and before[2]:
                    rec.hud = replace(rec.hud, lie=before[3])
                self._conceded_ids.discard(shot_id)
        rec.outcome = None

    def _fire_meta(self, fire: int) -> tuple[int | None, int | None]:
        """(GSPro slot, hole) of a fire, from GSPro's log or, once that's let it go, as it was owned."""
        info = self.gspro.fire_info(fire)
        if info.get("player") is not None:
            return info.get("player"), info.get("hole")
        return self._fire_log.get(fire, (None, None))

    def _count_lie_source(self, hud: Hud) -> None:
        """Count a taken off-the-tee setup's lie source (set at setup, `_emit_setup`) in the round's tally."""
        if (r := self.round) is None or r.ended or not (src := getattr(hud, "_lie_source", None)):
            return
        label = src[0] if src[0] == "round file" else f"{src[0]} ({src[1]})"
        r.lie_sources[label] = r.lie_sources.get(label, 0) + 1

    def _log_lie_summary(self, r: Round) -> None:
        """One line on where the round's off-the-tee setups got their lies (GSPro's round file, else why not)."""
        counts = ", ".join(f"{n} {k}" for k, n in sorted(r.lie_sources.items(), key=lambda kv: -kv[1]) if n)
        log.info("Round %s: %d setup lies from: %s", r.id, sum(r.lie_sources.values()), counts or "nothing")

    def _log_name_summary(self, r: Round) -> None:
        """One line on how the round's shots got their players' names, for a report to start from."""
        counts = ", ".join(f"{n} {k}" for k, n in sorted(r.name_sources.items(), key=lambda kv: -kv[1]) if n)
        total = sum(r.name_sources.values())
        log.info("Round %s: %d shots named by: %s; player box hidden at impact on %d of %d; the screen and "
                 "GSPro's log disagreed on %d", r.id, total, counts or "nothing", r.box_hidden, total,
                 r.log_disagreed)

    def _keep_gspro_log(self) -> None:
        """Copy GSPro's log into the booth's logs at round end: GSPro starts it afresh each time it opens,
        so the round's lines are gone if it's restarted before a report is sent."""
        try:
            src = Path(getattr(self._follower, "path", None) or self.cfg.source.gspro_log).expanduser()
            if not src.is_file():
                return
            log_dir = Path(self.cfg.log_dir)
            with open(src, "rb") as f, open(log_dir / f"gspro-{time.strftime('%Y-%m-%d-%H%M%S')}.log", "wb") as out:
                f.seek(max(0, os.path.getsize(src) - GSPRO_LOG_KEEP_BYTES))
                shutil.copyfileobj(f, out)
            for old in sorted(log_dir.glob("gspro-*.log"))[:-KEEP_GSPRO_LOGS]:
                old.unlink(missing_ok=True)
        except OSError:
            log.exception("Keeping a copy of GSPro's log failed")

    @staticmethod
    def _round_file_names(r: Round) -> list[str]:
        """The names GSPro's round file may give the round's players: as read on the tee, and every name
        the league knows each one by (a misread on the tee still maps to the player)."""
        return [*r.roster, *(n for p in r.roster.values() if p for n in r.league.names_for(p))]

    def _keep_round_file(self, r: Round, started: float, players: list[str], speeds: list[float],
                         courses: list[str] = (), shot_ids: list[str] = (), folder: str | None = None) -> None:
        """Copy GSPro's round file (currentRound.dat) into the booth's logs at round end, for looking into
        a round later: GSPro overwrites it when the next round starts. `players`, `speeds`, `courses`,
        `shot_ids` and `folder` (see round_file.archive) are taken on the event loop (this runs in a thread)."""
        try:
            log_path = self._follower.path if self._follower else self.cfg.source.gspro_log
            src = Path(gspro_files.folder_of(os.path.expanduser(log_path))) / gspro_files.ROUND_FILE
            if out := round_file.archive(src, self.cfg.log_dir, started, r.course, players, speeds,
                                         courses=courses, shot_ids=shot_ids, folder=folder):
                log.info("Round %s: kept GSPro's round file as %s", r.id, out.name)
        except OSError as e:
            log.warning("Keeping a copy of GSPro's round file failed: %s", e)

    def _settle_outcomes(self) -> None:
        """How the shots named after the fact ended, from GSPro's log: holed; a putt the player hit again
        on the same hole missed; their last stroke on a hole that ended without holing was conceded."""
        g, r = self.gspro, self.round
        scores, _, _, picked = self._log_view()
        for fire in sorted(self._outcomes_due):
            shot_id, rec, owner, hole = self._fire_rows.get(fire, (None, None, None, None))
            if rec is None or rec.round_id != r.id or rec.outcome is not None or rec.mulligan \
                    or (fire < g.fires - 30 and (fire not in self._resettle or not g.fire_info(fire))):
                self._outcomes_due.discard(fire)
                self._resettle.discard(fire)
                continue
            putt, tag = rec.shot.is_putt, f"[shot {shot_id}]"
            later = min((n for n, info in g._fire_info.items() if n > fire and info.get("player") == owner),
                        default=None)
            if g.fire_info(fire).get("holed"):
                self._record_result(shot_id, "holed", tag, putt=putt, rec=rec, from_log=True)
            elif later is not None and g.fire_info(later).get("hole") == hole:
                if putt:
                    self._record_result(shot_id, "missed", tag, putt=True, rec=rec, from_log=True)
            elif (owner, hole) in scores and (owner, hole) not in picked and r.format not in TEAM_BALL \
                    and (not g.team_scores or r.no_scramble):   # a shamble's balls are each player's own
                self._record_result(shot_id, "conceded", tag, putt=putt, rec=rec, from_log=True)
            elif later is None:
                continue   # the hole's still going
            self._outcomes_due.discard(fire)
            self._resettle.discard(fire)

    def _shot_wet(self, player: str | None, since: float | None = None) -> bool:
        """True when the GSPro log's Hit_water says this player's current shot went in the water. Used
        for the in-flight tease and the instant splash reaction. False when the log can't be attributed
        to a player (idx None) - don't fall through to wet(None), which would borrow the active player's
        wet state and mis-credit the splash to the wrong player. `since` (a monotonic time) gates the
        in-flight tease to a splash seen after this shot's impact, so a stale earlier one can't fire it."""
        gs = getattr(self, "gspro", None)
        idx = self._gspro_idx(player)
        if gs is None or idx is None:
            return False
        return gs.wet(idx, since=since)

    def _round_shots(self, player: str | None) -> list[ShotRecord]:
        """This player's recorded shots in the current round (the round's running tallies)."""
        r = self.round
        return [x for x in self.state.player_shots(player or "unknown") if r and x.round_id == r.id]

    def _truth_context_facts(self, is_putt: bool, idx: int | None) -> list[str]:
        """Gimme-circle context from the GSPro log for this player.

        The league concedes putts inside a set radius (the log's gimme distance, nominally four feet),
        so a ball inside the circle is auto-conceded and never actually putted. A putt the player is
        over is therefore outside the circle; the one worth a word is a short one left just beyond it.
        """
        g = self.gspro
        to_pin, gimme = g.to_pin_of(idx), g.gimme_of(idx)
        if idx is None or not (is_putt and to_pin is not None and gimme):
            return []
        gimme_ft, dist_ft = gimme * 3, to_pin * 3
        if dist_ft <= gimme_ft + 3:
            return [f"This putt sits just outside the {gimme_ft:.0f}-foot gimme circle, about "
                    f"{dist_ft:.0f} feet; it was not conceded, so it has to be holed."]
        return []

    def _penalty_facts_from_truth(self, idx: int, hud: Hud | None = None) -> list[str]:
        """The penalty fact from GSPro's own stroke count, told once per new penalty on a hole.

        GSPro 3.1 scores a dry penalty (out of bounds, a ball that ran into a penalty area, a lost ball)
        only as it moves on to the next player, after the shot's own call, so the next setup is the
        first place the booth can say it (4.0 scores it as the ball stops, and the shot's call says it:
        _penalty_shot_facts). With a setup `hud` and no splash or penalty already called, that
        setup's line is marked to always air and the penalty only counts as told once a line carrying
        it airs: a dropped or cancelled setup line leaves it for the next one."""
        hole = self.gspro.hole_by.get(idx, self.gspro.current_hole)   # this player's hole, not the global one
        penalties = self.gspro.penalties(idx)
        if penalties <= self._narrated_pen.get((idx, hole), 0):
            return []
        n = self.gspro.shot_number(idx)
        r = self.round
        if hud is not None and hud.shot_number and n != hud.shot_number and r and not r.ended \
                and r.format == "scramble" and not r.no_scramble:
            # The log counts the player's own ball; the team plays the picked one. Derek's ball was in the
            # water, Dan's on the green, and "Derek's fourth is for birdie" aired over stroke 3 (Kiawah, 11-11).
            return []
        facts = [f"A penalty stroke was taken (a ball in the water or out of bounds, then a drop); "
                 f"{penalties} penalty stroke{'s' if penalties > 1 else ''} on this hole so far. Counting "
                 f"the penalty, this is stroke {n} on the hole: call it the {ordinal(n)}, not an earlier number."]
        # GSPro logs a ball crossing into a penalty area; with no splash, it landed dry and ran in.
        fire, last = next(((f, v) for f, v in sorted(self._fire_rows.items(), reverse=True) if v[2] == idx),
                          (None, None))
        if last and last[3] == hole and self.gspro.fire_info(fire).get("hazard") and "in_water" not in last[1].tags:
            facts.append("The ball that cost the penalty landed dry and then ran on into the penalty area.")
        # The shot's own call told it: the splash, or (4.0, which scores a dry penalty at once) a line
        # about the ball out of bounds or run into the penalty area that aired (_penalty_shot_facts).
        splashed = bool(last and last[3] == hole and "in_water" in last[1].tags) \
            or self._pen_called.get((idx, hole), 0) >= penalties
        if hud is None or splashed or not self.cfg.commentary.preshot:
            self._narrated_pen[(idx, hole)] = penalties   # the splash call told it (or no setup line will)
        else:
            hud._penalty = ((idx, hole), penalties, True)
            # "...: say so" aired as "one penalty already unmentioned" (Bethpage, 11-16).
            facts.append("Nobody has called this penalty yet, so this line must tell the listener about it "
                         "(that a penalty stroke was taken). Never say it went unmentioned.")
        return facts

    def _strategy_facts(self, minimap: "MinimapRead | None", hud: Hud) -> list[str]:
        """The minimap's read on where to miss, offered at most once a hole and not every hole, so the
        booth plays the course now and then ("with those bunkers short, he can't come up short") rather
        than on every swing."""
        hole = self.state.current_hole
        if minimap is None or hole is None or self._strategy_hole == hole:
            return []
        if not (line := minimap.strategy(to_yards(hud.to_pin), hud.lie)) or self.rng.random() >= STRATEGY_CHANCE:
            return []
        self._strategy_hole = hole
        return [line]

    def _add_rehit_penalty(self, hud: Hud, player: str | None) -> tuple[int, int] | None:
        """GSPro 4.0 scores no penalty stroke when a ball in the water is played again from where it was
        hit (a re-tee): the next stroke is numbered one on, as if the wet ball were in play. Seen at that
        stroke's setup, the player's last stroke on the hole went wet (or out of bounds), this is the
        stroke after it, from the same spot, and GSPro's log scored nothing between: add the stroke.
        Returns the (index, hole) it was added on."""
        g = self.gspro
        idx = self._gspro_idx(player)
        r = self.round
        if idx is None or r is None or not hud.shot_number or hud.shot_number < 2 or g.team_scores \
                or (r.format == "scramble" and not r.no_scramble):
            return None
        fire, row = next(((f, v) for f, v in sorted(self._fire_rows.items(), reverse=True) if v[2] == idx),
                         (None, None))
        if row is None:
            return None
        shot_id, rec, _, _ = row
        hole = g.fire_info(fire).get("hole")   # as its score line credited it (a tee shot's impact had the hole before)
        own = hud.gspro_shot or hud.shot_number
        if (shot_id is None or shot_id in self._rehit_penalised or rec.mulligan or rec.shot.is_putt
                or rec.round_id != r.id or hole is None or g.hole_by.get(idx) != hole
                or rec.shot_number is None or own + g.stroke_shift(idx, own) != rec.shot_number + 1):
            return None   # (the record counts any penalty added before: compare in its numbering)
        info = g.fire_info(fire)
        wet = "in_water" in rec.tags or "out_of_bounds" in rec.tags or g.wet(idx)
        if not wet or info.get("penalty_strokes") or info.get("score") is None \
                or g.strokes_before(idx) != info["score"]:
            return None
        here, there = to_yards(hud.to_pin), to_yards(rec.to_pin_before or rec.hud.to_pin)
        if here is None or there is None or abs(here - there) > max(3.0, there * 0.02):
            return None   # played from somewhere else: a drop, not the same spot again
        self._rehit_penalised.add(shot_id)
        g.add_penalty(idx, hole)
        log.info("Re-hit from where shot %s (%r's stroke %s, %s) was played, %s from the pin: GSPro scored no "
                 "penalty, so the booth adds one (stroke %d, not %d)", shot_id, self._display(player),
                 rec.shot_number, "in the water" if "in_water" in rec.tags or g.wet(idx) else "out of bounds",
                 hud.to_pin, rec.shot_number + 2, rec.shot_number + 1)
        return idx, hole

    def _snap_added_penalty(self, snap: tuple, before: Hud, player: str | None) -> None:
        """A penalty owed before this stroke that the booth added after its impact snapshot was taken
        (its setup, or this impact, only got to it then): the snapshot that settles the wet shot's
        finish and baselines this stroke counts it too."""
        idx = self._gspro_idx(player)
        if idx is None or len(snap) < 9:
            return
        g = self.gspro
        # With no number on screen, the log's own count at impact (which _number_from_log goes on to use).
        stroke = before.gspro_shot or before.shot_number or snap[6].get(idx)
        if n := g.added_since(idx, stroke, snap[8]):
            key = (idx, snap[1].get(idx, snap[5]))
            snap[4][key] = snap[4].get(key, 0) + n
            if idx in snap[6]:
                snap[6][idx] += n

    def _count_added_penalty(self, hud: Hud, player: str | None, tag: str = "") -> None:
        """A penalty GSPro left out (_add_rehit_penalty) on the HUD's stroke number for the rest of the
        hole, so the record, the replay check and the calls all count it. GSPro's own number is kept on
        the read, which makes this safe to run on it again."""
        if not hud.shot_number:
            return
        own = hud.gspro_shot or hud.shot_number
        n = self.gspro.stroke_shift(self._gspro_idx(player), own)
        hud.gspro_shot, hud.shot_number = own if n else None, own + n   # (a mulligan can take it back)
        if n and tag:
            log.info("%s stroke %d, not GSPro's %d: the booth counted a penalty GSPro didn't", tag, own + n, own)

    def _fix_stale_pick_hud(self, hud: Hud, idx: int | None) -> None:
        """Right after a scramble pick GSPro's HUD can still show "Shot 1" and the tee distance (Derek
        "has 437 yards" for a stroke 2 from 262). The log has the team ball by then: trust it."""
        g = self.gspro
        # (this team's: another side's hole-out or pick-up leaves this one playing on)
        team = {idx}
        while grown := {m for pair in g.teammates if pair & team for m in pair} - team:
            team |= grown
        if (idx is not None and hud.shot_number == 1 and g.team_scores and g.pick and g.pick[0] in team
                and g.pick[1] == g.current_hole and g.hole_by.get(idx) == g.current_hole
                and not any(h == g.current_hole and i in team for i, h in g.hole_scores)   # not the next tee
                and (n := (g.pick_stroke() or 0) + 1) > 1 and (tp := g.to_pin_of(idx)) is not None):
            # The team's ball sets the stroke: this player's own count can carry a penalty on a ball
            # the team didn't pick.
            log.info("Stale HUD after a scramble pick: shot 1 %s -> stroke %d, %.1f yards", hud.to_pin, n, tp)
            hud.shot_number, hud.to_pin = n, f"{tp:.1f} yards"
            source = "screen"
            if (hud.lie or "").lower() == "tee":
                hud.lie, source = g.lie_of(idx), "GSPro's log"
            hud._lie_source = (source if hud.lie else "none", "team ball")   # (a tee setup had none)
