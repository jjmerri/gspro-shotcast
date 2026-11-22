"""Posting the round to Squabbit as it's played.

Part of Engine (commentator/engine/core.py), which is these parts together."""
import asyncio
from dataclasses import replace
from pathlib import Path

from .common import log
from ..round import FORMAT_NAMES, Round


class SquabbitMixin:
    """Posting the round to Squabbit as it's played."""

    def _squabbit_live_on(self) -> bool:
        sq = self.cfg.squabbit
        return bool(sq.enabled and sq.live_updates and self.league.squabbit_invite
                    and self.cfg.source.type != "replay")

    def _squabbit_uids(self) -> dict[str, str]:
        """Roster player -> Squabbit user id."""
        return {p: i for p, info in self.league.player_info.items() if (i := (info or {}).get("squabbit_id"))}

    def _squabbit_week(self, r: Round) -> tuple[dict, int] | None:
        """The league week this round goes on Squabbit as, and how many of its holes come before the
        week's first (0 for a league round). A practice round only for its holes (set up for 18 on a
        nine-hole week) counts too while the sim's booked for league: from the tee of the week's first
        hole, so a back nine week's warm-up front nine is left out."""
        if r.is_league:
            entry = r.schedule
        else:
            entry = r.league_if_holes
            b = self.bookings.at() if self.bookings else None
            if not entry or b is None or not b.league:
                return None
        if not entry.get("week") or r.start_hole_assumed and not r.is_league:
            return None
        # A league round a restarted booth joined on 18 holes (the setup screen's gone) has one too.
        offset = 0 if r.start_hole_assumed else (entry.get("start_hole") or r.start_hole) - r.start_hole
        if offset < 0 or r.seq <= offset or (
                not r.is_league and offset + (entry.get("hole_count") or r.holes) > r.holes):
            return None
        return entry, offset

    def _squabbit_view(self, r: Round, s: dict) -> Round:
        """The round as its league week scores it: a practice round only for its holes takes the week's
        teams and format (one profile for an alternate shot team is both partners)."""
        return r if r.is_league else r.league_view(s["entry"], self.cfg.commentary.round_format)

    def _squabbit_kick(self) -> None:
        """A league round's scores or CTP marks may have changed (or a round set up for more than the
        week's holes reached its first, see _squabbit_week): the first time, find the team's card and
        ask on screen; once they've said yes, post whatever's new (in the background)."""
        r = self.round
        s = self._sq
        if r is not None and s is not None and s["round"] != r.id:
            # Another round (league or not): the last one's question comes down and its writer stops
            # before this round's can start.
            if s["status"] == "asking" and s["question"] is not None:
                s["question"].close()
            if s["ctp_q"] is not None:
                s["ctp_q"][1].close()
            if s.get("league_q") is not None:
                s["league_q"].close()
            s["status"] = "off"
            self._sq = s = None
        if not r or r.ended or r.is_tournament or not self._squabbit_live_on():
            return
        if s is None:
            if (week := self._squabbit_week(r)) is None:
                return   # not placed yet (or not up to the week's holes); a later kick tries again
            entry, offset = week
            if not any(side.team for side in self._squabbit_view(r, {"entry": entry}).sides):
                return
            self._sq = {"round": r.id, "status": "finding", "targets": [], "question": None,
                        "dirty": False, "task": None, "last": None, "seen": {},
                        "ctp_q": None, "ctp_queue": [], "ctp_asked": set(), "league_q": None,
                        "entry": entry, "offset": offset}
            self._bg(self._squabbit_offer(r, self._sq))
            return
        if s["status"] == "on":
            s["dirty"] = True
            if s["task"] is None or s["task"].done():
                s["task"] = asyncio.create_task(self._squabbit_push(r, s))

    def _ctp_marks(self, r: Round) -> list:
        """This round's closest-to-the-pin tee shots on the green (squabbit_live.Mark), each with why
        the booth isn't sure of it, if it isn't: the screen's distance against GSPro's log, and a tee
        shot conceded inside the gimme (on the green, or the fringe?). A scramble's come from the pick
        screen, each ball its own player's whichever the team plays, and are always asked about."""
        from ..facts import ctp_text, to_yards
        from ..squabbit_live import Mark, feet_text

        marks = []
        scramble = r.format == "scramble"
        # A scramble team's next setup is the picked ball's (or the team's, before the log names the
        # pick): its tee shots are read off the pick screen instead. A one-person side plays its own.
        for seq, by_who in r.ctp_results.items():
            for who, text in by_who.items():
                if r.player_format(who) == "scramble":
                    continue
                seen = to_yards(text)
                logged = self._log_finish.get(r.ctp_tees.get((seq, who)))
                if logged is not None and (seen is None or abs(seen - logged) * 3 > max(1.0, logged * 0.3)):
                    marks.append(Mark(seq, who, logged, f"The screen read {text}, but GSPro's log has it "
                                                        f"{ctp_text(logged)} from the hole."))
                elif seen is not None:
                    marks.append(Mark(seq, who, seen))
        shared = set(r.anonymous_players())   # one profile's ball: whoever hit it, it's no one's mark
        tees = {s.shot_id: s for s in self.state.shots
                if s.round_id == r.id and s.player not in shared and s.shot_number == 1 and not s.mulligan
                and s.shot_id is not None and s.hole_seq is not None and r.hole_number(s.hole_seq) in r.ctp_holes}
        # Tee shots that ended the hole (an ace, or inside the gimme) leave no second-stroke setup to read.
        # A scramble ball on the pick screen has its mark from there (the screen's name may be an alias),
        # unless the shot was taken back since (a mulligan).
        withdrawn = {s.shot_id for s in self.state.shots if s.round_id == r.id and s.mulligan}
        picks = {(seq, who): (yards, sid) for seq, by in r.ctp_picks.items()
                 for who, (yards, sid) in by.items() if sid not in withdrawn}
        player_of = lambda n: self.league.match_player(n) or n
        carded = {(seq, player_of(p)) for seq, p in picks}
        enders = {i: s for i, s in tees.items() if s.outcome in ("holed", "conceded")
                  and (not scramble or (s.picked is not False and (s.hole_seq, player_of(s.player)) not in carded))}
        sure_yards = self.cfg.squabbit.ctp_sure_gimme_feet / 3
        finished = self.store.tee_finishes(list(enders))
        for sid, shot in enders.items():
            if sid not in finished and shot.outcome != "conceded":
                continue
            yards = finished.get(sid)   # a gimme GSPro gave no distance or radius for: they're asked
            if shot.outcome == "holed":
                marks.append(Mark(shot.hole_seq, shot.player, 0.0))
            elif (left := self._gimme_left(sid)) is None:
                marks.append(Mark(shot.hole_seq, shot.player, yards, "It was conceded inside the gimme, but "
                                  "GSPro didn't say how close. Is it on the green, and how far from the hole?"))
            elif left > sure_yards:
                marks.append(Mark(shot.hole_seq, shot.player, left, f"It was conceded inside the gimme, "
                                  f"{ctp_text(left)} from the hole. Is it on the green?"))
            else:
                marks.append(Mark(shot.hole_seq, shot.player, left))
        for (seq, who), (yards, sid) in picks.items():
            if who not in shared and yards is not None:
                marks.append(Mark(seq, who, yards, f"The pick screen has it on the green, "
                                                   f"{feet_text(yards)} from the hole.", sid))
        return marks

    def _popup_where(self):
        return self.screen.monitor_rect if self.screen is not None and self.cfg.screen.follow_window else None

    async def _squabbit_offer(self, r: Round, s: dict) -> None:
        from ..popup import Question
        from ..squabbit import SquabbitClient
        from ..squabbit_live import LiveSync

        week = int(s["entry"]["week"])
        teams = sorted({side.team for side in self._squabbit_view(r, s).sides if side.team})
        uid_of = self._squabbit_uids()
        if self._sq_live is None:
            self._sq_live = LiveSync(Path(self.cfg.db_path).with_name("squabbit_live.json"))
        live = self._sq_live

        def find() -> list:
            found = []
            with SquabbitClient() as client:
                league = client.league_by_invite(self.league.squabbit_invite)
                if not league:
                    return found
                for team in teams:
                    players = self.league.team_players.get(team, [])
                    uids = [uid_of[p] for p in players if p in uid_of]
                    if not players or len(uids) != len(players):
                        log.info("Squabbit live: %s has a player with no Squabbit id", team)
                        continue
                    if (t := LiveSync.find(client, league["_id"], week, team, uids)) is not None:
                        found.append((t, live.others(client, t)))
            return found

        try:
            # After any earlier round's writer: what it's committing is the booth's, not someone else's.
            async with self._sq_lock:
                if s is not self._sq:
                    return
                found = await asyncio.wait_for(asyncio.to_thread(find), timeout=self.cfg.squabbit.timeout_seconds)
        except Exception as e:
            log.warning("Squabbit live: couldn't read week %s's cards (%s); not posting this round", week, e)
            s["status"] = "off"
            return
        targets = []
        declined = False
        for t, others in found:
            if others:
                log.info("Squabbit live: %s's week %s card already has entries the booth didn't make (%s); "
                         "leaving it to them", t.team, week, "; ".join(others))
            elif live.answer(t) is not False:
                targets.append(t)
            else:
                declined = True
        if s is not self._sq or r.ended or not targets:
            s["status"] = "off"
            if declined and not targets:
                # Said no thanks to this week's card already (an earlier start of this round, or a
                # league round they're replaying for practice): is this one even league?
                self._ask_league(r, s)
            return
        s["targets"] = targets
        if not r.is_league and not r.ctp_holes:
            # Played as practice for its holes: the week's closest-to-the-pin tee shots are still read.
            r.ctp_holes = [int(h) for h in s["entry"].get("ctp_holes") or []]
        if all(live.answer(t) for t in targets):   # said yes before the booth restarted
            log.info("Squabbit live: posting %s's week %s round (said yes earlier)", " and ".join(teams), week)
            s["status"] = "on"
            self._squabbit_kick()
            return
        s["status"] = "asking"
        loop = asyncio.get_running_loop()
        who = " and ".join(t.team for t in targets)
        s["question"] = Question(
            "Update Squabbit as you play?",
            f"Week {week}, {who}: each hole's score goes on your Squabbit card as you finish it, and "
            "closest-to-the-pin tee shots too. Nothing is entered over anything already on your card.",
            "Yes, update Squabbit", "No thanks",
            on_answer=lambda yes: loop.call_soon_threadsafe(self._squabbit_answer, s, yes),
            where=self._popup_where(),
        ).show()

    def _squabbit_answer(self, s: dict, yes: bool) -> None:
        if s is not self._sq or s["status"] != "asking":
            return
        s["question"] = None
        for t in s["targets"]:
            self._sq_live.set_answer(t, yes)
        s["status"] = "on" if yes else "off"
        log.info("Squabbit live: %s", "yes, posting the round as it's played" if yes else "no thanks")
        if not yes:
            self._ask_league(self.round, s)
        if yes:
            r = self.round
            if r is not None and r.id == s["round"] and r.ended:
                s["dirty"] = True
                s["task"] = asyncio.create_task(self._squabbit_push(r, s))
            else:
                self._squabbit_kick()

    def _ask_league(self, r: Round | None, s: dict) -> None:
        """Squabbit turned down for a league round: ask whether it's a league round at all (a team
        practising on another week's course after their league round reads as that week's)."""
        from ..popup import Question

        if (r is None or s is not self._sq or r.id != s["round"] or r.ended or not r.is_league
                or s.get("league_q") is not None):
            return
        loop = asyncio.get_running_loop()
        teams = " and ".join(sorted({side.team for side in r.sides if side.team})) or "this team"
        s["league_q"] = Question(
            "Is this a league round?",
            f"The booth has this as {teams}'s week {s['entry'].get('week')} league round on "
            f"{r.course or 'this course'}. If you're only practising, it's kept as practice: out of the "
            "league's standings and league stats.",
            "Yes, it's league", "No, practice",
            on_answer=lambda yes: loop.call_soon_threadsafe(self._league_answer, s, yes),
            where=self._popup_where(),
        ).show()
        log.info("Asking on screen whether round %s is a league round", r.id)

    def _league_answer(self, s: dict, yes: bool) -> None:
        if s is not self._sq or s.get("league_q") is None:
            return
        s["league_q"] = None
        r = self.round
        if yes or r is None or r.id != s["round"] or r.ended or not r.is_league:
            log.info("Round %s: %s", s["round"], "a league round, the players say" if yes else
                     "not a league round, but it's over (or changed) already: left as it is")
            return
        r.make_practice()
        # GSPro's own format, as a practice round started this way would have: the setup screen's, then
        # the scramble its log shows.
        self._play_setup_format(r)
        self._scramble_pairs = set()
        self._sync_scramble()
        n = self._restamp_practice(r)
        log.info("Round %s: not a league round, the players say: practice from here (%d shots so far "
                 "marked); format %s", r.id, n, FORMAT_NAMES.get(r.format or "", "not set"))
        self._bg(self._restamp_practice_landed(r))

    def _restamp_practice(self, r: Round) -> int:
        """A round made practice: its shots so far filed under the format it is now (mark_practice).
        How many were league shots."""
        if r.shot_floor is None:
            return 0
        n = self.store.mark_practice(r.shot_floor, record=r.uuid)
        anon = set(r.anonymous_players())
        for player in self.store.players_after(r.shot_floor, record=r.uuid):
            shared = not r.solo(r.side_for(player)) and (r.format == "scramble" or r.no_scramble
                                                         or self.gspro.team_scores)
            self.store.restamp_shots(r.shot_floor, player, fmt=r.player_format(player),
                                     attributed=player not in anon, shared_ball=shared, record=r.uuid)
        return n

    async def _restamp_practice_landed(self, r: Round) -> None:
        # A shot in flight as they answered took the league format before it landed: file it again.
        await self._drain_shot_tasks(self.cfg.commentary.wrap_wait_seconds)
        if not r.is_league:
            self._restamp_practice(r)

    async def _squabbit_push(self, r: Round, s: dict) -> None:
        """Posts what's new until nothing is: a kick while a post is out just goes round again."""
        async with self._sq_lock:
            await self._squabbit_push_locked(r, s)

    async def _squabbit_push_locked(self, r: Round, s: dict) -> None:
        from ..squabbit import SquabbitClient
        from ..squabbit_live import Blocked, team_ctp, team_scores

        live = self._sq_live
        while s["dirty"] and s["status"] == "on" and s["targets"] and s is self._sq:
            s["dirty"] = False
            uid_of = self._squabbit_uids()
            shared = {side.label for side in r.sides if len(side.players) > 1}   # a team's ball: no one's mark
            found = [m for m in self._ctp_marks(r) if m.who not in shared]
            view = self._squabbit_view(r, s)
            off = s["offset"]
            jobs = []
            for t in s["targets"]:
                if r.hole_number(1 + off) is None:
                    continue   # which nine isn't confirmed yet: the next kick posts it
                if r.hole_number(1 + off) != t.start_hole:   # GSPro's log moved the round to the other nine
                    if t.scorecard_id not in s.setdefault("wrong_nine", set()):
                        s["wrong_nine"].add(t.scorecard_id)
                        log.warning("Squabbit live: this round's week holes start on hole %s, %s's week card on "
                                    "%s; not posting it", r.hole_number(1 + off), t.team, t.start_hole)
                    continue
                # The round's holes as the week card's: only the week's (a front nine week set up for 18
                # stops at 9; a back nine week's warm-up front nine isn't on it).
                week_seq = lambda seq: seq - off if 1 <= seq - off <= t.num_holes else None
                scores: dict[str, dict[int, int | None]] = {
                    uid: {q: v for seq, v in by.items() if (q := week_seq(seq)) is not None}
                    for uid, by in team_scores(view, t.team, uid_of).items()}
                scores = {uid: by for uid, by in scores.items() if by}
                marks: dict[tuple[int, str], float] = {}

                def answered(m):
                    """The mark at the distance the players confirmed, so a scramble team's closest is
                    picked by it (a corrected ball can lose to a teammate's)."""
                    if m.sure or (q := week_seq(m.seq)) is None:
                        return m
                    if not (uid := uid_of.get(self.league.match_player(m.who) or "")):
                        return m
                    asked, yards = live.ctp_answer(live.ctp_key(t, q, uid, m.shot))
                    return replace(m, yards=yards) if asked and yards is not None else m

                week_found = [answered(m) for m in found]
                for (seq, uid), m in team_ctp(view, t.team, uid_of, self.league.match_player, week_found).items():
                    if (seq := week_seq(seq)) is None:
                        continue
                    m = replace(m, seq=seq)
                    # Only where the week's event has a contest, and once the player's done the hole (GSPro's
                    # log has the tee shot's finish by then, and a mulligan's been taken). A scramble's,
                    # off the pick screen, are asked about as soon as it's read.
                    done = r.ended or scores.get(uid, {}).get(seq) is not None
                    if t.hole(seq) not in t.ctp or not (done or view.player_format(m.who) == "scramble"):
                        continue
                    if m.sure:
                        if done:
                            marks[(seq, uid)] = m.yards
                        continue
                    key = live.ctp_key(t, seq, uid, m.shot)
                    asked, yards = live.ctp_answer(key)
                    if not asked:
                        self._squabbit_ask_ctp(s, t, key, m)
                    elif yards is not None and done:
                        marks[(seq, uid)] = yards
                # Only a score this round had and then lost is taken back (a mulligan on the green): a round
                # rebuilt after a restart hasn't lost what it hasn't recovered yet.
                seen = s["seen"].setdefault(t.scorecard_id, set())
                seen |= {(uid, q) for uid, by in scores.items() for q in by}
                for uid, seqs in live.posted(t).items():
                    for seq in seqs - set(scores.get(uid, {})):
                        if (uid, seq) in seen:
                            scores.setdefault(uid, {})[seq] = None
                jobs.append((t, scores, marks))
            snapshot = [(t.scorecard_id, scores, sorted(marks.items())) for t, scores, marks in jobs]
            if snapshot == s["last"]:
                continue
            s["last"] = None   # Squabbit may change below even if some cards fail; set again only when all go in

            def push() -> tuple[list[str], list[tuple], set[str]]:
                """What went in, the cards someone else has entered on, and the cards that failed."""
                lines, blocked, failed = [], [], set()
                with SquabbitClient() as client:
                    for t, scores, marks in jobs:
                        try:
                            lines += [f"{t.team}: {x}" for x in live.push(client, t, scores, marks)]
                        except Blocked as e:
                            blocked.append((t, str(e)))
                        except Exception as e:
                            log.warning("Squabbit live: %s's post failed (%s); trying again on the next hole",
                                        t.team, e)
                            failed.add(t.scorecard_id)
                return lines, blocked, failed

            # No timeout here: a thread can't be stopped, and a second writer mustn't start while this one
            # might still post older scores. Each request has its own (httpx's), so it does end.
            try:
                lines, blocked, failed = await asyncio.to_thread(push)
            except Exception as e:
                log.warning("Squabbit live: post failed (%s); trying again on the next hole", e)
                continue
            if not failed:
                s["last"] = snapshot
            if lines:
                log.info("Squabbit live: posted %s", "; ".join(lines))
            for t, why in blocked:
                log.warning("Squabbit live: someone else has entered on %s's card (%s); the booth stops "
                            "posting it", t.team, why)
                s["targets"] = [x for x in s["targets"] if x is not t]
                s["last"] = None
            if not s["targets"]:
                s["status"] = "off"
                if s["ctp_q"] is not None:
                    s["ctp_q"][1].close()
                    s["ctp_q"] = None

    def _squabbit_ask_ctp(self, s: dict, t, key: str, m) -> None:
        """Queues an on-screen check of a mark the booth isn't sure of: one box at a time."""
        if key in s["ctp_asked"]:
            return
        s["ctp_asked"].add(key)
        s["ctp_queue"].append((t, key, m))
        self._squabbit_next_ctp(s)

    def _squabbit_next_ctp(self, s: dict) -> None:
        from ..popup import Entry
        from ..squabbit_live import feet_text, parse_feet

        if s["ctp_q"] is not None or not s["ctp_queue"] or s is not self._sq:
            return
        t, key, m = s["ctp_queue"].pop(0)
        loop = asyncio.get_running_loop()
        s["ctp_q"] = (key, Entry(
            f"{self._display(m.who)}'s closest to the pin, hole {t.hole(m.seq)}",
            f"{m.why} Fix the distance if it's off, then post it to Squabbit.",
            feet_text(m.yards) if m.yards is not None else "", "Post it", "I'll enter it myself",
            on_answer=lambda text: loop.call_soon_threadsafe(self._squabbit_ctp_answer, s, key, text),
            valid=lambda text: parse_feet(text) is not None,
            where=self._popup_where(),
        ).show())

    def _squabbit_ctp_answer(self, s: dict, key: str, text: str | None) -> None:
        from ..squabbit_live import parse_feet

        if s is not self._sq or s["ctp_q"] is None or s["ctp_q"][0] != key:
            return
        s["ctp_q"] = None
        yards = parse_feet(text) if text is not None else None
        log.info("Squabbit live: CTP %s %s", key, f"confirmed at {text}" if yards is not None else
                 "left for the players to enter")
        self._squabbit_next_ctp(s)
        r = self.round if self.round is not None and self.round.id == s["round"] else None

        self._sq_live.set_ctp_answer(key, yards)
        if r is not None and s["status"] == "on":
            s["dirty"] = True
            # A new task, not the one out now: that one may already be past its last look at `dirty`.
            s["task"] = asyncio.create_task(self._squabbit_push(r, s))

    def _squabbit_round_end(self, r: Round) -> None:
        """The round's over: an unanswered question comes down, and a yes gets its last holes posted."""
        s = self._sq
        if not s or s["round"] != r.id:
            return
        if s.get("league_q") is not None:
            s["league_q"].close()
            s["league_q"] = None
            log.info("Round %s ended before anyone said whether it's league; left as league", r.id)
        if s["status"] == "asking":
            if s["question"] is not None:
                s["question"].close()
            s["question"] = None
            s["status"] = "off"
            log.info("Squabbit live: the round ended before anyone answered; not posting it")
        elif s["status"] == "on":
            s["dirty"] = True
            if s["task"] is None or s["task"].done():
                s["task"] = asyncio.create_task(self._squabbit_push(r, s))
