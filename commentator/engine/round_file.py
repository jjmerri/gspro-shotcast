"""GSPro's round file: watching it, and matching its strokes, names and clubs to the booth's shots.

Part of the Engine (commentator/engine/core.py), which is these parts together."""
import asyncio
import json
import math
import os
import re
import sqlite3
import time
from dataclasses import replace
from pathlib import Path

from .common import (
    FILE_END_DUE_S, FILE_LATE_END_S, FILE_SETTLE_S, ROUND_FILE_CHECK_S, ROUND_FILE_RETRY_S, SCREEN_COVERED_S,
    SCREEN_COVER_CHECK_S, _setup_key, log,
)
from .. import round_file
from ..config import ROUND_STATUS, ROUND_STATUS_CHECK_S, ROUND_STATUS_EVERY_S
from ..hud import Hud
from ..models import HoleChangeEvent
from ..sources import gspro_files
from ..state import ShotRecord
from ..sources.club_feeds import SAME_BALL_MPH


class RoundFileMixin:
    """GSPro's round file: watching it, and matching its strokes, names and clubs to the booth's shots."""

    def _in_round(self) -> bool:
        """In a round, or still finishing one (the sign-off, stopping the stream): a restart now would cut
        into it."""
        r = self.round
        stopping = self._live_stop_task
        return bool((r is not None and not r.ended) or self._round_handling
                    or (self.speech is not None and not self.speech.is_idle())
                    or (stopping is not None and not stopping.done()))

    async def _round_status_loop(self) -> None:
        """Keeps log_dir/ROUND_STATUS current, so the tray knows when a restart would interrupt nothing."""
        path = Path(self.cfg.log_dir) / ROUND_STATUS

        def write(status: dict) -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(status), encoding="utf-8")
            os.replace(tmp, path)
        # Checked often and written the moment it changes, so a round that just started reads as one;
        # rewritten every ROUND_STATUS_EVERY_S anyway, which tells the tray the booth is still there.
        # A ball struck is written at once too: the tray mustn't install over play that just resumed.
        written, at = None, 0.0
        while True:
            now = (self._in_round(), self._struck_at)
            if now != written or time.monotonic() - at >= ROUND_STATUS_EVERY_S:
                try:
                    await asyncio.to_thread(write, {"round": now[0], "updated": time.time(), "struck": now[1]})
                    written, at = now, time.monotonic()
                except Exception as e:
                    log.debug("Couldn't write the round status: %s", e)
                if self.live_mirror is not None:
                    try:
                        r = self.round
                        league = r is not None and not r.ended and r.is_league
                        self.live_mirror.league(r.schedule.get("end_date") if league else None)
                        self.live_mirror.week(self._week_panel())
                    except Exception as e:
                        log.debug("Couldn't work out the week board: %s", e)
            await asyncio.sleep(ROUND_STATUS_CHECK_S)

    async def _screen_cover_loop(self) -> None:
        while True:
            await asyncio.sleep(SCREEN_COVER_CHECK_S)
            self._check_screen_cover()

    def _check_screen_cover(self) -> None:
        """A program in front of all of GSPro for minutes (TabTip's handwriting canvas once did it for a
        whole afternoon) leaves the booth reading black: no names, holes or scores. That's raised as a
        health problem, pushed to the phone, so someone closes it; cleared once GSPro is read again."""
        got = self.screen.covered_for() if self.screen is not None else None
        if got and got[0] >= SCREEN_COVERED_S:
            self.health.raise_issue(
                "screen.covered", "fail",
                f"{got[1]} has been covering GSPro for over {round(SCREEN_COVERED_S / 60)} minutes, so the booth "
                f"can't read names, holes or scores. Close it (or, if GSPro shows through it, add it to "
                f"screen.show_through).")
        elif self.health.get("screen.covered") is not None:
            self.health.clear("screen.covered", "GSPro is being read again" if not got else "a different cover")

    def _round_folder(self) -> str:
        return gspro_files.folder_of(self._follower.path if self._follower else self.cfg.source.gspro_log)

    def _round_watch_of(self) -> gspro_files.RoundFileWatch:
        """The one watch of GSPro's round file: the shot feed's when it reads GSPro's files (it moves it
        with the log it follows), else the engine's own."""
        if self._round_watch is None:
            files = getattr(getattr(self.source, "gspro_log", self.source), "files", None)
            if isinstance(files, gspro_files.GsproFiles):
                self._round_watch, self._round_watch_shared = files.watch, True
            else:
                self._round_watch = gspro_files.RoundFileWatch(self._round_folder())
        return self._round_watch

    async def _refresh_round_file(self) -> bool:
        """Look at the round file (a stat; parsed only when it changed). True when there's a new read: its
        strokes are then in _file_tracks, and anything waiting on _file_changed is woken."""
        w = self._round_watch_of()
        if not self._round_watch_shared:
            w.move(self._round_folder())
        try:
            await asyncio.to_thread(w.poll)
        except Exception:
            log.exception("Couldn't read GSPro's round file")
            return False
        if w.version == self._file_version:
            return False
        self._file_version = w.version
        entries = w.entries if isinstance(w.entries, list) else []
        self._file_entries = entries
        self._file_tracks = round_file.tracks(entries)
        woken, self._file_changed = self._file_changed, asyncio.Event()
        woken.set()
        return True

    def _file_names_fit(self, gspro_name: str, player: str | None) -> bool:
        return round_file.names_fit(gspro_name, player) or (
            player is not None and self._file_player(gspro_name) == player)

    def _file_rest_lie(self, player: str | None, shot_number: int | None) -> str | None:
        return self._file_rest_lie_why(player, shot_number)[0]

    def _file_rest_lie_why(self, player: str | None, shot_number: int | None) -> tuple[str | None, str]:
        """Where GSPro's round file has `player`'s ball sitting before their stroke `shot_number` (GSPro's
        own number, as its HUD and log have it: no penalty the booth added) on the hole in play: their last
        stroke's EndingSurface, as a booth lie. None when the file can't say:
        no such stroke yet, a penalty or a holed ball (play goes on from elsewhere), the cart path (free
        relief may have moved it: the stroke's own StartingSurface says where from), a code with no
        name (round_file.SURFACES), a team ball (scramble, alternate shot, or a shamble's picked drive:
        GSPro copies balls between partners), or another round's file. With it, why there's none, for
        the setup's lie-source line."""
        r = self.round
        if not self._file_live():
            return None, "not being written"
        if not player or player == "unknown" or self._anonymous(player):
            return None, "player unnamed"
        if not shot_number or shot_number < 2 or self.hole_number is None or r is None or r.ended:
            return None, "no stroke to ask about"
        if r.format in ("scramble", "alternate") or r.no_scramble:
            return None, "team ball"
        def theirs(name: str) -> bool:   # (the round's one player it names: not Kyle's for Kyle Martin)
            k = self._file_roster_key(r, name)
            return k is not None and player in (k, r.roster.get(k)) and self._file_names_fit(name, player)
        t = next((t for t in reversed(self._file_tracks) if t.hole == self.hole_number
                  and t.shot_number == shot_number - 1 and theirs(t.player)), None)
        if t is None:
            return None, "no previous stroke yet"
        why = ("penalty" if t.penalty else "dropped" if t.dropped else "picked up" if t.picked_up
               else "holed" if t.in_cup else "no lie code" if not t.lie_after
               else "cart path" if t.lie_after == "cart path" else None)
        if why:
            return None, why
        try:
            ours = self._file_round_test(r)(t, r.course or self.current_course)
        except sqlite3.Error as e:
            log.warning("Couldn't check GSPro's round file stroke is this round's: %s", e)
            return None, "unchecked"
        return (t.lie_after, "") if ours else (None, "another round's")

    def _file_live(self) -> bool:
        """A round on, and a live GSPro writing its round file (this session): a stroke of it will be there."""
        w = self._round_watch
        return w is not None and w.entries is not None and w.written >= self._booth_wall0 \
            and self.round is not None and not self.round.ended

    async def _file_settle(self) -> bool:
        """A stroke's penalty or drop is its own entry, maybe a rewrite after the stroke's: when the file
        was written under FILE_SETTLE_S ago, wait that out and read it again (True)."""
        w = self._round_watch
        if w is None or not w.written or (settle := FILE_SETTLE_S - (time.time() - w.written)) <= 0:
            return False
        await asyncio.sleep(min(settle, FILE_SETTLE_S))   # (a clock skewed from the file's)
        await self._refresh_round_file()
        return True

    async def _late_file_end(self, shot_id: int | None, tag: str, rec: ShotRecord | None, player: str | None,
                             ball_speed: float | None, since: float, seq: int | None, putt: bool = False) -> None:
        """A stroke's hole-out or gimme, from GSPro's round file once it has the ball at rest (or, its
        drop written later, a scramble's pick: once the watcher sees it, _settle_file_ends)."""
        t = await self._file_stroke_at(player, ball_speed, since, seq, wait=FILE_LATE_END_S)
        if t is not None:
            self._file_end_due(t.shot_id, shot_id, tag, rec, putt)
            self._settle_file_ends()

    def _file_end_due(self, gid: str, shot_id: int | None, tag: str, rec: ShotRecord | None, putt: bool) -> None:
        self._file_ends_due[gid] = (shot_id, tag, rec, putt, time.monotonic() + FILE_END_DUE_S)
        while len(self._file_ends_due) > 64:
            del self._file_ends_due[next(iter(self._file_ends_due))]

    def _settle_file_ends(self) -> None:
        """Record the hole-out or gimme of each stroke due one (_file_ends_due) that GSPro's round file
        now has dropped; forget those past their time."""
        if not self._file_ends_due:
            return
        now, tracks = time.monotonic(), {t.shot_id: t for t in self._file_tracks}
        for gid, (shot_id, tag, rec, putt, until) in list(self._file_ends_due.items()):
            if (t := tracks.get(gid)) is not None and t.picked_up:
                del self._file_ends_due[gid]   # (the hole conceded at the maximum: the ball didn't drop)
                if putt:
                    self._record_result(shot_id, "missed", tag, putt=True, rec=rec)
                log.info("%s %s picked up short (GSPro round file, after the reaction)", tag,
                         "putt" if putt else "full shot")
            elif t is not None and t.dropped:
                del self._file_ends_due[gid]
                outcome = "holed" if t.in_cup else "conceded"
                self._record_result(shot_id, outcome, tag, putt=putt, rec=rec, left_yards=t.finish_yards)
                log.info("%s %s %s (GSPro round file, after the reaction)", tag, "putt" if putt else "full shot",
                         ("holed" if putt else "holed out") if t.in_cup else "finished inside the gimme circle")
            elif now >= until:
                del self._file_ends_due[gid]

    async def _file_hole_end(self, player: str | None, ball_speed: float | None, since: float,
                             seq: int | None, wait: float = 0.0) -> round_file.Track | None:
        """The stroke struck at `since` (player, ball speed) in GSPro's round file, when it ended the hole
        there (holed, stopped inside the gimme circle, or picked up short), else None. `wait`: up to how
        long (seconds) to wait for GSPro to write it (at rest)."""
        t = await self._file_stroke_at(player, ball_speed, since, seq, wait)
        return t if t is not None and (t.dropped or t.picked_up) else None

    async def _file_stroke_at(self, player: str | None, ball_speed: float | None, since: float,
                              seq: int | None, wait: float = 0.0) -> round_file.Track | None:
        """The stroke struck at `since` (player, ball speed) in GSPro's round file, waited for up to `wait`
        seconds, with its drop if GSPro wrote that just after (_file_settle)."""
        if self._round_watch is None:
            return None
        deadline = time.monotonic() + wait
        while True:
            changed = self._file_changed
            await self._refresh_round_file()
            t = self._file_track_of(player, ball_speed, since, claim=seq)
            if t is not None and not t.dropped and not t.picked_up and await self._file_settle():
                t = self._file_track_of(player, ball_speed, since, t.shot_id) or t
            if t is not None or (left := deadline - time.monotonic()) <= 0:
                return t
            try:   # (the watcher's wake, else a look of its own: _refresh_round_file is a stat)
                await asyncio.wait_for(changed.wait(), min(left, 0.1))
            except asyncio.TimeoutError:
                pass

    def _file_player(self, gspro_name: str) -> str | None:
        """The league player GSPro's round file names: one of their names exactly, else the roster's
        tolerant match when GSPro's name fits one of theirs word for word (round_file.names_fit: not
        "Alex Jones" for Alex Smith, known as "Alex", nor "Daniel" for Dan). GSPro's names are typed,
        not read off the screen."""
        if self.league is None:
            return None
        if p := self.league.exact_player(gspro_name):
            return p
        p = self.league.match_player(gspro_name)
        names = [p, *self.league.names_of(p)] if p else []
        full = [n for n in names if len(n.split()) > 1]
        if p and any(round_file.names_fit(gspro_name, n) for n in names) and (
                len(gspro_name.split()) < 2 or not full or any(round_file.names_fit(gspro_name, n) for n in full)):
            return p   # (a whole first name of theirs: "Daniel" isn't Dan Smith, known as "Dan")
        if len(gspro_name.split()) > 1:   # ("Alex J."): the one player whose full name it fits
            fit = {q for q in self.league.players for n in (q, *self.league.names_of(q))
                   if len(n.split()) > 1 and round_file.names_fit(gspro_name, n)}
            if len(fit) == 1:
                return fit.pop()
        return None

    def _file_track_of(self, player: str | None, ball_speed: float | None, impact: float,
                       shot_id: str | None = None, claim: int | None = None,
                       known: str | None = None) -> round_file.Track | None:
        """The stroke GSPro's round file has for the shot struck at `impact` (monotonic): the earliest read
        after it with the shot's ball speed (SAME_BALL_MPH) and, with the player known, theirs, that no
        other shot (`claim`: this one's impact count) has taken. `shot_id` gets the one already found
        again, as the file last had it (a penalty or drop written after it). `known`: the ShotID the shot
        feed took the shot from (it's that one, however soon it was read, if the names fit)."""
        w = self._round_watch
        if w is None:
            return None
        if shot_id is not None:
            return next((t for t in self._file_tracks if t.shot_id == shot_id), None)
        if ball_speed is None:
            return None
        named = bool(player) and player != "unknown" and not self._anonymous(player)
        for t in self._file_tracks:
            if known is not None:
                if t.shot_id != known:
                    continue
            else:
                seen = w.seen_at.get(t.shot_id)
                if seen is None or seen < impact or abs(t.ball_speed - ball_speed) > SAME_BALL_MPH:
                    continue
            if named and not self._file_names_fit(t.player, player):   # another's stroke: not this shot's
                continue
            if self._file_claims.get(t.shot_id, claim) != claim:
                continue
            if claim is not None:
                self._file_claims[t.shot_id] = claim
                while len(self._file_claims) > 64:
                    del self._file_claims[next(iter(self._file_claims))]
            return t
        return None

    async def _round_file_loop(self) -> None:
        """Watches GSPro's own record of the round in play (currentRound.dat, 3.59+; see round_file.py): read
        the moment Windows says GSPro wrote it (RoundFileWatch.notify), else on a stat every
        gspro_files.WATCH_BACKUP_S (parsed only when it changed). The shot handler takes the
        ball at rest from it (_wait_for_file). Applies it to the stored shots when it changes, as soon
        as a shot it has is recorded (_round_file_kick), and again every ROUND_FILE_CHECK_S for
        ROUND_FILE_RETRY_S after (till the booth records a shot, its stroke can be matched to an earlier
        round's), then moves any shot the booth filed under another hole or stroke number than GSPro's
        (_refile_from_round_file). Older GSPro writes none. It's applied whole each time: which round the
        file is comes from all its strokes."""
        w = self._round_watch_of()
        if not self._round_watch_shared:
            w.move(self._round_folder())
        loop, nudge = asyncio.get_running_loop(), self._file_nudge

        def notified() -> None:   # (on the notifier's thread)
            try:
                loop.call_soon_threadsafe(nudge.set)
            except RuntimeError:   # the loop has closed
                pass
        await asyncio.to_thread(w.notify, notified)
        nudge.set()   # the first read now: what's there is history before any shot is struck
        try:
            await self._watch_round_file(w, nudge)
        finally:
            w.unnotify()

    async def _watch_round_file(self, w: gspro_files.RoundFileWatch, nudge: asyncio.Event) -> None:
        gone, pending, pending_since, tried_at, applied = set(), [], None, 0.0, None

        def same_player(gspro_name: str, player: str | None) -> bool:
            return player is not None and self._file_player(gspro_name) == player

        def name_of(gspro_name: str) -> str:
            return self._file_player(gspro_name) or gspro_name
        while True:
            # (a stroke waiting on a shot is tried again every ROUND_FILE_CHECK_S, a multiple of this)
            try:
                await asyncio.wait_for(nudge.wait(), gspro_files.WATCH_BACKUP_S)
            except TimeoutError:
                pass
            nudge.clear()
            await self._refresh_round_file()
            w = self._round_watch
            if w is None or w.entries is None:
                continue
            now = time.monotonic()
            # (a read the live follow made counts too: what matters is whether this version was applied)
            changed = self._file_version != applied
            if changed:
                gone = round_file.untracked(self._file_entries, self._file_tracks)
                self._settle_file_ends()
            elif self._round_file_kick:
                pass
            elif pending_since is None or now - pending_since >= ROUND_FILE_RETRY_S or now - tried_at < ROUND_FILE_CHECK_S:
                continue
            self._round_file_kick, tried_at, tracks = False, now, self._file_tracks
            try:
                written, left = self.store.apply_round_file(tracks, now=w.written, same_player=same_player,
                                                            rewrite=changed, gone=gone, name_of=name_of,
                                                            pinned=self._file_pins, rounds=self._file_round_ids(),
                                                            folders=self._round_file_folders())
                self._drop_refused_pins(tracks)
            except sqlite3.Error as e:
                log.warning("Couldn't apply GSPro's round file: %s", e)
                continue
            applied = self._file_version
            if changed:
                log.info("GSPro's round file: %d strokes, %d applied, %d without a shot yet",
                         len(tracks), written, len(left))
                pending_since = now
            elif len(left) < len(pending):
                log.info("GSPro's round file: %d more strokes applied", len(pending) - len(left))
            pending = left
            if left:
                try:
                    if self._refile_from_round_file(left):
                        self.store.apply_round_file(tracks, now=w.written, same_player=same_player,
                                                    rewrite=False, gone=gone, name_of=name_of,
                                                    pinned=self._file_pins, rounds=self._file_round_ids(),
                                                    folders=self._round_file_folders())
                        self._drop_refused_pins(tracks)
                except sqlite3.Error as e:
                    log.warning("Couldn't refile shots from GSPro's round file: %s", e)
            if changed and self._pick_cards_kept:   # a penalty written after a pick, with no swing since
                try:
                    self._recheck_pick_penalties()
                except Exception:
                    log.exception("Couldn't recheck the picks against GSPro's round file")
            try:
                self._sync_file_names()
                self._sync_file_clubs()
            except sqlite3.Error as e:
                log.debug("Couldn't read the names GSPro's round file gave: %s", e)
            try:
                self._learn_file_slots()
            except Exception:
                log.exception("Couldn't place the players in GSPro's slots from its round file")

    def _sync_file_names(self) -> None:
        """The live round's shots the booth recorded blind take the player, hole and stroke number the store
        gave them from GSPro's round file (Store._blind_named); they go back as they were if that match
        is taken back."""
        r = self.round
        if r is None:
            return
        recs = [x for x in self.state.shots if x.round_id == r.id and x.shot_id is not None
                and (x.player == "unknown" or x.shot_id in self._file_named)]
        if not recs:
            return
        ids = [x.shot_id for x in recs]
        rows = {i: (p, h, n) for i, p, h, n in self.store.db.execute(
            f"SELECT id, player, hole, shot_number FROM shots WHERE id IN ({', '.join('?' * len(ids))})", ids)}
        for x in recs:
            row = p, hole, n = rows.get(x.shot_id, (None, None, None))
            named = bool(p) and p != "unknown"
            was = self._file_named.get(x.shot_id)
            if named and (x.player == "unknown" or was is not None and was[1] != row):
                if was is None:
                    was = ((x.hole, x.hole_seq, x.shot_number, x.par), row)
                else:   # the store matched it to another stroke since: from where it was recorded blind
                    x.hole, x.hole_seq, x.shot_number, x.par = was[0]
                    if p != x.player:
                        self._unslot(x.shot_id)
                self._file_named[x.shot_id] = (was[0], row)
                x.player = p
                if hole and (m := re.match(r"Hole (\d+)", hole)) and not re.match(rf"Hole {m.group(1)}\b", x.hole or "") \
                        and not r.start_hole_assumed and 1 <= (seq := int(m.group(1)) - r.start_hole + 1) <= r.holes:
                    # (the hole's own label, as its other shots have it, for the recap to find it by)
                    x.hole = next((y.hole for y in self.state.shots if y.round_id == r.id and y.hole_seq == seq
                                   and y.hole and re.match(rf"Hole {m.group(1)}\b", y.hole)), hole)
                    x.hole_seq = seq
                    x.par = next((y.par for y in self.state.shots if y.round_id == r.id and y.hole_seq == seq
                                  and y is not x and y.par), None) \
                        or (int(pm.group(1)) if (pm := re.search(r"\bpar (\d)", x.hole)) else None)
                x.shot_number = n if n is not None else x.shot_number
                log.info("Shot %s is %s's (GSPro's round file): %s, stroke %s", x.shot_id, p, x.hole, x.shot_number)
            elif was is not None and not named:
                x.player = "unknown"
                x.hole, x.hole_seq, x.shot_number, x.par = self._file_named.pop(x.shot_id)[0]
                x.shot_number = n if n is not None else x.shot_number   # (as stored: _unrefile's, maybe)
                self._unslot(x.shot_id)

    def _unfiled_setup(self, before: Hud, screen_before: Hud, shot_one: tuple | None = None) -> Hud:
        """The setup as the screen read it: the lie GSPro's round file overrode at setup (or for a
        misread shot 1: `shot_one`, its file lie and the one without it), put back."""
        if shot_one is not None and before.lie == shot_one[0]:
            return replace(before, lie=shot_one[1])
        k = _setup_key(screen_before)
        if k in self._setup_screen_lies and before.lie == self._setup_lies.get(k):
            return replace(before, lie=self._setup_screen_lies[k])
        return before

    def _sync_file_clubs(self) -> None:
        """The live round's shots take the club (and putt or not) and the lie played from the store has for
        them: GSPro's round file can correct one after the shot was recorded, or take its correction back,
        and the round's stats (its longest drive) are worked out from these. A shot it makes a putt is
        graded as one (a miss the log showed, which a full shot isn't given)."""
        r = self.round
        if r is None:
            return
        recs = {x.shot_id: x for x in self.state.shots if x.round_id == r.id and x.shot_id is not None}
        if not recs:
            return
        ids = list(recs)
        fires = {v[0]: f for f, v in self._fire_rows.items() if v[2] is not None}
        for i, club, putt, lie, outcome in self.store.db.execute(
                f"SELECT id, club, is_putt, lie_before, outcome FROM shots WHERE id IN ({', '.join('?' * len(ids))})",
                ids).fetchall():
            x = recs[i]
            if x.outcome in ("holed", "conceded") and (screen_putt := self.store.screen_putt(i)) is not None:
                putt = screen_putt   # (the hole ended on it: putt or not as before the file's club, _record_result)
            if club != x.shot.club or bool(putt) != x.shot.is_putt:
                was_putt = x.shot.is_putt
                x.shot = replace(x.shot, club=club, putter=bool(putt))
                if not putt and was_putt and x.outcome == "missed":   # (the store's taken back with the putt)
                    x.outcome = None
                elif putt and not was_putt and x.outcome is None and outcome == "missed":
                    x.outcome = outcome   # (the store's, back with the putt: _release_track)
                elif putt and not was_putt and x.outcome not in (None, "holed"):
                    # (a putt's result is stored, as a full shot's concession isn't: its conceded putt is)
                    self.store.set_shot_result(i, outcome=x.outcome)
                elif putt and not was_putt and x.outcome is None and (f := fires.get(i)) is not None:
                    self._outcomes_due.add(f)
                    self._resettle.add(f)
            if lie != x.lie_before:
                x.lie_before = lie

    def _unslot(self, shot_id: int) -> None:
        """A shot GSPro's round file named is no longer that player's: the log slot taken when the log
        agreed goes, and it waits again for the log to agree with whoever it's matched to now."""
        for fire, (sid, rec, owner, hole) in list(self._fire_rows.items()):
            if sid == shot_id and owner is not None:
                self._fire_rows[fire] = (sid, rec, None, hole)
                self._outcomes_due.discard(fire)
                self._unnamed.add(fire)

    def _file_holes(self) -> dict[int, int]:
        """The hole (GSPro's number) of each stored shot GSPro's round file has a stroke for."""
        if not self._file_tracks:
            return {}
        hole = {t.shot_id: t.hole for t in self._file_tracks}
        # The live pins (the store may not have applied them yet) over the store's matches they replace.
        pins = {gid: sid for gid, sid in self._file_pins.items() if gid in hole}
        try:
            stored = self.store.tracked_shots(list(hole))
        except sqlite3.Error as e:
            log.debug("Couldn't read the round file's shots: %s", e)
            stored = {}
        pinned_rows = set(pins.values())
        out = {sid: hole[gid] for gid, sid in stored.items() if gid not in pins and sid not in pinned_rows}
        out.update({sid: hole[gid] for gid, sid in pins.items()})
        return out

    def _file_stroke(self, t: round_file.Track, player: str | None) -> int:
        """The booth's stroke number for a stroke in GSPro's round file: GSPro's, plus any penalty the booth
        counted on that hole that GSPro left out (_count_added_penalty)."""
        idx = self._gspro_idx(player) if self._log_truth and player else None
        return t.shot_number + self.gspro.stroke_shift(idx, t.shot_number, t.hole - 1)

    def _file_seq(self, t: round_file.Track) -> int | None:
        """The live round's hole (its count from 1) GSPro's round file has a stroke on, when the round's
        first hole is known."""
        r = self.round
        if r is None or r.ended or r.start_hole_assumed or not r.start_hole:
            return None
        seq = t.hole - r.start_hole + 1
        return seq if 1 <= seq <= r.holes else None

    def _file_label(self, t: round_file.Track, seq: int) -> tuple[str, int | None]:
        """The hole label and par for a stroke GSPro's round file has on the live round's hole `seq`: the
        label the booth gave that hole's shots, else "Hole N, par P"."""
        r = self.round
        other = next((y for y in self.state.shots if y.round_id == r.id and y.hole_seq == seq and y.hole
                      and re.match(rf"Hole {t.hole}\b", y.hole)), None)
        label = other.hole if other is not None else None
        if label is None and self.state.current_hole and re.match(rf"Hole {t.hole}\b", self.state.current_hole):
            label = self.state.current_hole
        par = t.par or r.par.get(seq) or (other.par if other is not None else None) or (
            r.course_card.par.get(t.hole) if r.course_card and r.course_card.par else None)
        return label or f"Hole {t.hole}" + (f", par {par}" if par else ""), par

    def _file_hole_check(self, rec: ShotRecord, t: round_file.Track, r, round_hole: int | None, shot_hole: str | None,
                         tag: str) -> tuple[str, int | None, int | None] | None:
        """A shot just recorded that GSPro's round file has on another hole of the live round than the
        booth's count: with the group still on it (the stroke didn't end the hole, it's the newest shot,
        and the round hasn't moved since impact) the round's hole is corrected (_correct_hole moves the
        hole's shots with it); otherwise only this shot moves. The shot's hole label, par and stroke
        number, when they're not what it was recorded with (for calling it)."""
        if r is None or r is not self.round or rec.shot_id is None:
            return None
        seq = self._file_seq(t)
        if seq is None:
            return None
        if seq == round_hole:   # the round's hole is right; the shot may still lack (or misname) it
            if not (rec.hole and re.match(rf"Hole {t.hole}\b", rec.hole)):
                self._refile_shot(rec, t, seq)
                return rec.hole, rec.par, rec.shot_number
            return None
        label, par = self._file_label(t, seq)
        # The latest ball struck (a handler finishing late must not take the group back a hole).
        newest = rec.impact_seq is not None and rec.impact_seq == self._shot_seq
        # The next hole's banner seen, held back while the ball was followed: it moves the group on (the
        # hole change, its recap and all); the shot is filed there now.
        banner = seq == round_hole + 1 and self._hole_watch is not None and self._hole_watch.pending
        if not t.dropped and not t.picked_up and r.seq == round_hole and shot_hole \
                and shot_hole == self.state.current_hole and newest and not banner:
            log.info("%s GSPro's round file has this stroke on hole %s (round hole %s), not %s: correcting the hole",
                     tag, t.hole, seq, shot_hole)
            self.hole_number, self.hole_number_read = t.hole, True
            # (its banner, when read after this, isn't the next hole; moved back, it was read already)
            self._file_moved_to = t.hole if seq > round_hole else None
            self._events.put_nowait(HoleChangeEvent(previous=None, current=label, correction="round file"))
            return label, par, self._file_stroke(t, rec.player)
        self._refile_shot(rec, t, seq)
        return rec.hole, rec.par, rec.shot_number

    def _refile_shot(self, x: ShotRecord, t: round_file.Track, seq: int) -> None:
        """Moves a recorded shot to the hole and stroke number GSPro's round file has it on, in memory and in
        the store (its conceded tap-in's row with it). Round scores and pickups are left alone: GSPro's
        log has the scores, and a concession writes no stroke."""
        label, par = self._file_label(t, seq)
        n = self._file_stroke(t, x.player)
        log.info("Shot %s (%s) moved to hole %s stroke %s by GSPro's round file (was %s stroke %s)", x.shot_id,
                 x.player, t.hole, n, x.hole, x.shot_number)
        if x.shot_id is not None:   # (for _unrefile, should the stroke turn out not to be this shot's)
            if (u := self._file_unrefile.get(t.shot_id)) is not None and u[0] is not x:
                self._unrefile(t.shot_id)
                u = None
            if u is None:
                ids = [x.shot_id] + ([g] if (g := self._gimme_ids.get(x.shot_id)) is not None else [])
                u = (x, (x.hole_seq, x.hole, x.shot_number, x.par), None, self.store.shot_holes(ids))
            self._file_unrefile[t.shot_id] = (x, u[1], (seq, n, par, t.hole), u[3])
            while len(self._file_unrefile) > 256:
                del self._file_unrefile[next(iter(self._file_unrefile))]
        x.hole_seq, x.hole, x.shot_number, x.par = seq, label, n, par
        if x.shot_id is not None:
            self._file_refiled.add(x.shot_id)
            self.store.set_shot_hole([x.shot_id], label, n)
            if (gimme := self._gimme_ids.get(x.shot_id)) is not None:
                self.store.set_shot_hole([gimme], label, n + 1)

    def _relabel_refiled(self, r, label: str) -> None:
        """The hole's banner, read now: shots GSPro's round file put on it before take its full label
        (the recap finds a hole's shots by it)."""
        if not self._file_refiled or not (m := re.match(r"Hole (\d+)\b", label)):
            return
        moved = [x for x in self.state.shots if x.shot_id in self._file_refiled and x.round_id == r.id
                 and x.hole_seq == r.seq and x.hole != label and re.match(rf"Hole {m.group(1)}\b", x.hole or "")]
        for x in moved:
            x.hole = label
            self._file_refiled.discard(x.shot_id)
        ids = [x.shot_id for x in moved] + [g for x in moved if (g := self._gimme_ids.get(x.shot_id)) is not None]
        try:
            self.store.set_shot_hole(ids, label)
        except sqlite3.Error as e:
            log.warning("Couldn't relabel the shots GSPro's round file put on %s: %s", label, e)

    def _refile_from_round_file(self, pending: list[round_file.Track]) -> bool:
        """Strokes in GSPro's round file no recorded shot matched (store.apply_round_file matches on the hole
        and stroke number): a shot of the live round with the stroke's player and ball speed, between the
        shots that player's strokes before and after it went to, filed under another hole or stroke number,
        is moved to GSPro's (its Hole and HoleShot are GSPro's own count) and pinned to the stroke (its
        number may keep a penalty GSPro's leaves out). True if any was found. Only a file
        written since the booth started, and only strokes first read since the round started: an earlier
        round's file must not relabel this one's shots."""
        r, w = self.round, self._round_watch
        if r is None or r.ended or not pending or w is None or w.written < self._booth_wall0:
            return False
        tracks = self._file_tracks
        claimed = self.store.tracked_shots(t.shot_id for t in tracks)
        taken = set(claimed.values())
        mine = [x for x in self.state.shots if x.round_id == r.id and x.shot_id is not None and not x.mulligan
                and x.shot_id not in taken and x.shot.ball_speed is not None]
        # Shots in the order they were struck (handlers record out of order: a row id isn't).
        struck = sorted((x for x in self.state.shots if x.round_id == r.id and x.shot_id is not None),
                        key=lambda x: (x.impact_seq is None, x.impact_seq or 0, x.shot_id))
        order = {x.shot_id: i for i, x in enumerate(struck)}
        fits_round = self._file_round_test(r)
        try:
            course_of = self.store.shot_courses(x.shot_id for x in mine)
        except sqlite3.Error as e:
            log.debug("Couldn't read the shots' courses: %s", e)
            return False
        moved = False
        for t in pending:
            seq = self._file_seq(t)
            if seq is None or w.seen_at.get(t.shot_id, -math.inf) < self._round_started_at or not fits_round(t):
                continue
            theirs = [u for u in tracks if u.player == t.player]
            at = next((i for i, u in enumerate(theirs) if u.shot_id == t.shot_id), None)
            if at is None:
                continue
            lo = max((order[claimed[u.shot_id]] for u in theirs[:at] if claimed.get(u.shot_id) in order), default=-1)
            hi = min((order[claimed[u.shot_id]] for u in theirs[at + 1:] if claimed.get(u.shot_id) in order),
                     default=math.inf)
            # (a hole either side at most: the booth's count is off by one when it's off)
            # (not one struck after GSPro had written the stroke: it's a later one)
            fits = [x for x in mine if lo < order[x.shot_id] < hi and self._file_names_fit(t.player, x.player)
                    and (x.impact_at is None or x.impact_at <= w.seen_at.get(t.shot_id, math.inf))
                    and (t.putt is None or bool(x.shot.is_putt) == t.putt) and x.hole_seq is not None and abs(x.hole_seq - seq) <= 1
                    and abs(x.shot.ball_speed - t.ball_speed) <= max(SAME_BALL_MPH, 0.01 * t.ball_speed)
                    and fits_round(t, course_of.get(x.shot_id))]
            if not fits:
                continue
            # The player's earliest that fits: strokes and shots go in the same order (the next stroke's
            # bounds start after it, so two at one speed never cross).
            x = min(fits, key=lambda x: order[x.shot_id])
            mine.remove(x)
            claimed[t.shot_id] = x.shot_id
            if (x.hole_seq, x.shot_number) != (seq, self._file_stroke(t, x.player)) \
                    or not re.match(rf"Hole {t.hole}\b", x.hole or ""):
                self._refile_shot(x, t, seq)
            self._pin_file_track(t, x.shot_id)
            moved = True
        return moved

    def _file_round_test(self, r):
        """Whether a stroke in GSPro's round file can be round `r`'s (a shot of it stored on `course`): not
        when GSPro's own round (RoundID) isn't the one strokes already pinned to `r`'s shots were of, nor
        when its course key is another course than `r`'s or the shot's (GSPro started another round before
        the booth closed this one): another folder than the round's GSPro course folder, when that's known
        (_round_course_folder: GSPro lists Wentworth by the Sea, in its balsams_gsp folder, as Balsams
        Panorama), else a course the booth knows by another name (round_file.other_course)."""
        rounds = self._file_round_ids().get(r.uuid) if r is not None else None
        folder = self._round_course_folder(r)
        courses: list[str] | None = None
        known: dict[tuple, bool] = {}

        def other_course(key: str, course: str | None) -> bool:
            nonlocal courses
            if not course:
                return False
            if (key, course) not in known:
                if courses is None:
                    courses = self.store.courses_played()
                known[key, course] = round_file.other_course(key, course, courses)
            return known[key, course]

        def fits(t: round_file.Track, course: str | None = None) -> bool:
            if rounds and t.round_id is not None and t.round_id not in rounds:
                return False
            key = t.course_key
            if key and folder:   # (the round's shots are all on its folder's course, whatever they're named)
                return round_file.folder_is(key, folder)
            return not key or not (other_course(key, r.course if r is not None else None)
                                   or other_course(key, course))
        return fits

    def _round_course_folder(self, r) -> str | None:
        """Round `r`'s GSPro course folder, when the booth is sure of it: the folder GSPro's log said it
        had loaded when the round started, else its course card's, found surely by name (or by the
        schedule's own name for the course).
        None (go by the course's name) when a near miss or a name two courses share found the card, or
        when GSPro found another course's files in that folder (a copy: Wentworth by the Sea in
        balsams_gsp), whose strokes it keys by that other course."""
        if r is not None and r.loaded_folder:   # (as the round started: a later load is the next round's)
            folder = r.loaded_folder
        else:
            card = r.course_card if r is not None else None
            if card is None or not card.folder or card.name_unsure:
                return None
            folder = card.folder
        return None if folder.lower() in self.gspro.mismatched_folders else folder

    def _round_file_folders(self) -> dict[str, str | None]:
        """The live round's record (uuid) -> its GSPro course folder, when sure of it (_round_course_folder),
        for store.apply_round_file; -> None to forget one kept for it that GSPro found another course's
        files in (a round continued from before that was known)."""
        r = self.round
        if r is None:
            return {}
        if folder := self._round_course_folder(r):
            return {r.uuid: folder}
        card = r.course_card
        if {(r.loaded_folder or "").lower(), ((card.folder if card else None) or "").lower()} \
                & self.gspro.mismatched_folders:
            return {r.uuid: None}
        return {}

    def _file_ours(self, t: round_file.Track, r, tag: str, course: str | None = None) -> bool:
        """Whether the stroke GSPro's round file has for a shot of booth round `r` (stored on `course`;
        else the round's, or the course on screen) is that round's to number and tie to: not when GSPro
        is on to another round or course the booth hasn't caught up with (_file_round_test)."""
        try:
            ours = self._file_round_test(r)(t, course or (r.course if r is not None else None) or self.current_course)
        except sqlite3.Error as e:
            log.warning("%s couldn't check GSPro's stroke is this round's: %s", tag, e)
            return False
        if not ours:
            log.info("%s GSPro's round file stroke is of another round or course (%s, round %s): not tied to it",
                     tag, t.course_key, t.round_id)
        return ours

    def _pin_file_track(self, t: round_file.Track, shot_id: int) -> None:
        """Tie a stroke in GSPro's round file to the stored shot it is (store.apply_round_file's `pinned`)."""
        if (u := self._file_unrefile.get(t.shot_id)) is not None and u[0].shot_id != shot_id:
            self._unrefile(t.shot_id)   # another shot was moved for it before this one was found to be it
        self._file_pins[t.shot_id] = shot_id
        if t.round_id is not None:
            self._file_pin_rounds[t.shot_id] = t.round_id
        while len(self._file_pins) > 256:
            self._file_pin_rounds.pop(gid := next(iter(self._file_pins)), None)
            self._file_unrefile.pop(gid, None)
            del self._file_pins[gid]

    def _file_round_ids(self) -> dict[str, set]:
        """GSPro's RoundIDs of the strokes pinned to each booth round's shots (its record's uuid -> them), as
        the shots are stored now (an opening drive a round adopted after it was pinned counts), whatever
        file GSPro has now: store.apply_round_file's `rounds`."""
        sids = {self._file_pins[g]: rid for g, rid in self._file_pin_rounds.items() if g in self._file_pins}
        out: dict[str, set] = {}
        for sid, uuid in self.store.shot_records(sids).items():
            if uuid:
                out.setdefault(uuid, set()).add(sids[sid])
        return out

    def _drop_refused_pins(self, tracks: list[round_file.Track]) -> None:
        """Forget the live pins the store didn't take (another course or GSPro round, known since): they
        no longer say which shot a stroke is, nor which GSPro round the shot's is."""
        ids = [t.shot_id for t in tracks if t.shot_id in self._file_pins]
        stored = self.store.tracked_shots(ids)
        for gid in ids:
            if stored.get(gid) != self._file_pins[gid]:
                log.info("GSPro's stroke %s isn't shot %s's after all: not tied to it", gid, self._file_pins[gid])
                del self._file_pins[gid]
                self._file_pin_rounds.pop(gid, None)
                self._unrefile(gid)

    def _keep_screen_stroke(self, x: ShotRecord, t: round_file.Track, n: int | None) -> None:
        """A shot recorded on GSPro's stroke number, not the screen's (`n`): should the stroke turn out not to
        be this shot's, _unrefile puts the screen's back (a refile after keeps it as where it was)."""
        if (u := self._file_unrefile.get(t.shot_id)) is not None and u[0] is not x:
            self._unrefile(t.shot_id)   # another shot was moved for it before this one was found to be it
        self._file_unrefile[t.shot_id] = (x, (x.hole_seq, x.hole, n, x.par), (x.hole_seq, x.shot_number, x.par, t.hole),
                                          {i: (h, n) for i, (h, _) in self.store.shot_holes([x.shot_id]).items()})

    def _unrefile(self, gid: str) -> None:
        """A shot _refile_shot moved for GSPro's stroke `gid`, found since not to be it: back where it (and its
        conceded tap-in) was, unless it has moved since."""
        x, was, moved, rows = self._file_unrefile.pop(gid, (None, None, None, None))
        if x is None or (x.hole_seq, x.shot_number, x.par) != moved[:3] \
                or not re.match(rf"Hole {moved[3]}\b", x.hole or ""):
            return
        x.hole_seq, x.hole, x.shot_number, x.par = was
        self._file_refiled.discard(x.shot_id)
        if (g := self._gimme_ids.get(x.shot_id)) is not None and g not in rows:   # (conceded since)
            rows = {**rows, g: (x.hole, x.shot_number + 1 if x.shot_number is not None else None)}
        self.store.put_shot_holes(rows)
        log.info("Shot %s back on %s stroke %s", x.shot_id, x.hole, x.shot_number)
