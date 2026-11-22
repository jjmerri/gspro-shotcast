"""The course card, tee boxes, handicaps and the board for the round.

Part of Engine (commentator/engine/core.py), which is these parts together."""
import asyncio
import threading
import time
from collections.abc import Callable
from typing import Any

from .common import BOARD_REREAD_SECONDS, BOARD_WAIT_SECONDS, LAYOUT_WAIT_SECONDS, _own_handicaps, _own_handicaps_from, log
from ..board import Board


class CourseMixin:
    """The course card, tee boxes, handicaps and the board for the round."""

    def _load_course_card(self, course: str | None):
        """GSPro's static card (par, stroke index) for this course, best-effort."""
        try:
            from .. import course_card

            # GSPro's log names the selected course's folder, which settles a name two courses share.
            folder = self.gspro.course if self._log_truth else None
            return course_card.load(self.cfg.source.gspro_courses_dirs, course, folder=folder,
                                    notes=self.commentary_on)
        except Exception as e:
            log.warning("Course card load failed (%s); par and index come from the scorecard", e)
            return None

    def _keep_course_tees(self, course: str | None, card) -> None:
        """Keep the course's tee ratings and yardages for our own handicap index (best-effort)."""
        if card is not None and course:
            try:
                self.store.record_course_tees(course, card)
            except Exception as e:
                log.warning("Couldn't keep %s's tees (%s)", course, e)

    def _fill_course_layouts(self, since: float | None = None) -> int:
        """Take the tee and pin of each hole of a round GSPro logged no layout for (one quit early; on
        3.59 any round) from the course file, for its hole replays (see course_layout), then name the
        layouts' tee boxes. Best-effort. Returns the holes filled."""
        filled = 0
        try:
            from .. import course_layout

            filled = course_layout.fill_missing(self.store.db, self.cfg.source.gspro_courses_dirs, since=since)
        except Exception as e:
            log.warning("Couldn't fill hole layouts from the course files (%s)", e)
        self._name_tee_boxes()
        return filled

    async def _fill_course_layouts_later(self) -> None:
        """After a round played to the end: GSPro 3.1 logs its layout as it completes, 3.59 doesn't.
        Give the log's a moment to be read, then fill whatever holes still have none, and publish them."""
        await asyncio.sleep(LAYOUT_WAIT_SECONDS)
        if self._fill_course_layouts(since=time.time() - 12 * 3600):
            self._schedule_publish(after_speech=True)

    def _name_tee_boxes(self) -> None:
        """Name the tee box (Black, Blue, ...) of the stored layouts not named yet. Best-effort."""
        try:
            from .. import course_layout

            course_layout.name_tee_boxes(self.store.db, self.cfg.source.gspro_courses_dirs)
        except Exception as e:
            log.warning("Couldn't name the tee boxes from the course files (%s)", e)

    def _refresh_own_handicaps(self, backfill: bool = False) -> None:
        """Work out each player's own handicap index from the rounds on record, for anyone the league
        has no handicap for. `backfill` first reads the tees of courses played before they were kept.
        The working out (seconds, after years of rounds) runs in a thread on a connection of its own, so
        the booth carries on meanwhile and the indexes land when it's done; one asked for while another
        runs follows it."""
        try:
            from .. import course_card

            if backfill:
                # By name alone: the folder GSPro's log names is tonight's course, not these.
                for course in self.store.courses_without_tees():
                    self._keep_course_tees(course, course_card.load(self.cfg.source.gspro_courses_dirs, course,
                                                                    notes=False))
            path = self.store.db.execute("PRAGMA main.database_list").fetchone()[2]
        except Exception as e:
            log.warning("Own handicap indexes failed (%s)", e)
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None or not path or self.store.db.in_transaction:   # (a thread's connection wouldn't see it)
            self._own_handicaps_inline += 1
            self._own_handicaps_wanted = False   # (one asked for before is older than this)
            self._set_own_handicaps(_own_handicaps, self.store.db)
            return
        self._own_handicaps_wanted = True
        if self._own_handicaps_task is None or self._own_handicaps_task.done():
            self._own_handicaps_task = loop.create_task(self._own_handicaps_in_thread(path), name="own-handicaps")

    async def _own_handicaps_in_thread(self, path: str) -> None:
        loop = asyncio.get_running_loop()
        while self._own_handicaps_wanted:
            self._own_handicaps_wanted = False
            inline = self._own_handicaps_inline
            done: asyncio.Future = loop.create_future()

            def work() -> None:
                # A daemon thread (not asyncio.to_thread), so quitting mid-way doesn't wait for it.
                try:
                    out: tuple = (_own_handicaps_from(path), None)
                except Exception as e:
                    out = (None, e)
                try:
                    loop.call_soon_threadsafe(lambda: done.done() or done.set_result(out))
                except RuntimeError:   # the loop has closed: the booth has quit
                    pass

            threading.Thread(target=work, name="own-handicaps", daemon=True).start()
            own, error = await done
            if error is not None:
                log.warning("Own handicap indexes failed (%s)", error)
            elif inline == self._own_handicaps_inline:   # (else one worked out inline since is newer)
                self._set_own_handicaps(lambda: own)

    def _set_own_handicaps(self, compute: Callable[..., dict], *args: Any) -> None:
        try:
            self.league.own_indexes = compute(*args)
        except Exception as e:
            log.warning("Own handicap indexes failed (%s)", e)
            return
        if self.league.own_indexes:
            log.info("Own handicap indexes: %s", ", ".join(f"{p} {i}" for p, i in sorted(self.league.own_indexes.items())))

    def _round_players(self, names: list[str]) -> list[str]:
        """Roster players in this round: every player on a team that has a GSPro name here.
        Both partners, so a one-ball team's net still has both handicaps."""
        teams = set()
        for n in names:
            player = self.league.match_player(n)
            if player:
                teams.add(self.league.player_team.get(player))
            team = self.league.match_team(n)
            if team:
                teams.add(team)
        return [p for t in teams if t for p in self.league.team_players.get(t, [])]

    async def _apply_live_handicaps(self, players: list[str]) -> None:
        """Refresh the current round's players' handicaps from Squabbit. Best-effort: on any
        failure the file's handicaps stand, so a slow or offline Squabbit never holds up the opener."""
        sq = self.cfg.squabbit
        if not sq.enabled or not self.league.squabbit_invite:
            return
        self._squabbit_kick()
        # The week's leaderboards load alongside, in their own task: a slow read never costs the handicaps.
        self._reread_board()
        ids = [i for p in players if (i := self.league.player_info.get(p, {}).get("squabbit_id"))]
        if ids:
            try:
                from ..squabbit import SquabbitClient

                def fetch() -> dict:
                    with SquabbitClient() as client:
                        return client.handicaps(ids)

                by_id = await asyncio.wait_for(asyncio.to_thread(fetch), timeout=sq.timeout_seconds)
                updated = self.league.apply_handicaps(by_id)
                log.info("Squabbit handicaps applied to %d of %d round players: %s",
                         len(updated), len(players), ", ".join(sorted(updated)))
            except Exception as e:
                log.warning("Squabbit handicap fetch failed (%s); using the league file's handicaps", e)
        # Give the leaderboards a moment more so the opener can use them (they usually land with the
        # handicaps); after that they fill in for the rest of the round.
        await asyncio.wait({self._board_task}, timeout=BOARD_WAIT_SECONDS)

    def _reread_board(self) -> None:
        """Read Squabbit's leaderboards again (_refresh_board). One read at a time: a call while one's
        out reads again after it, since that one may have started before what's new went in."""
        self._board_again = True
        if self._board_task is None or self._board_task.done():
            self._board_task = asyncio.create_task(self._board_reads())

    async def _board_reads(self) -> None:
        while self._board_again:
            self._board_again = False
            await self._refresh_board()

    async def _reread_board_later(self) -> None:
        await asyncio.sleep(BOARD_REREAD_SECONDS)
        self._reread_board()

    async def _refresh_board(self) -> None:
        """Read every league week's Squabbit leaderboard into self.board. Best-effort: on failure the
        last board read (or the history file's standings) stands."""
        try:
            from ..squabbit import SquabbitClient

            def fetch() -> list | None:
                with SquabbitClient() as client:
                    league = client.league_by_invite(self.league.squabbit_invite)
                    return [(t, client.leaderboard(t["_id"])) for t in client.tournaments(league["_id"])] \
                        if league else None

            # A read that's taken too long is dropped, but this waits out its thread (it can't be stopped)
            # so a read queued behind it (_board_reads) never runs alongside it.
            work = asyncio.ensure_future(asyncio.to_thread(fetch))
            try:
                items = await asyncio.wait_for(asyncio.shield(work), timeout=self.cfg.squabbit.timeout_seconds)
            except asyncio.TimeoutError:
                await asyncio.wait({work})
                work.cancelled() or work.exception()   # its own failure, if any, is the timeout's now
                raise
            if items is None:
                return
            season = self.history.current() or {}
            self.board = Board.from_squabbit(items, self.league, season.get("points_table"))
            self._publish_league_page()
            log.info("Squabbit leaderboards: %s", "; ".join(
                f"week {w} {len(self.board.posted(w))}/{self.board.team_count(w)} posted"
                for w in sorted(self.board.weeks) if self.board.posted(w)) or "no week posted yet")
        except Exception as e:
            log.warning("Squabbit leaderboard fetch failed (%s)", e)
