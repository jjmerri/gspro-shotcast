"""Whether a round is the league's: its week, its course, and what its card says.

Part of Engine (commentator/engine/core.py), which is these parts together."""
import time

from .common import log
from ..facts import to_yards
from ..hud import Hud
from ..league import course_match_score
from ..round import FORMAT_NAMES, Round, TEAM_BALL, normalize_format


class LeagueRoundMixin:
    """Whether a round is the league's: its week, its course, and what its card says."""

    def _lone_team(self, names: list[str]) -> tuple[str | None, str | None]:
        """The single league team these HUD names belong to, or (None, reason) when they don't form
        one registered team. A one-ball format shows the team or one partner; a two-ball format shows
        both partners, who must be on the same team (Toby and Kyle are never a team)."""
        teams: set[str] = set()
        for n in names:
            player = self.league.match_player(n)
            team = self.league.player_team.get(player) if player else self.league.match_team(n)
            if not team:
                return None, f"{n!r} isn't a league player"
            teams.add(team)
        if len(teams) != 1:
            return None, "the players on the tee aren't partners on one team"
        return teams.pop(), None

    def _course_week(self, course: str | None, today: str | None = None) -> dict | None:
        """The schedule week on `course` (League.schedule_entry, of the weeks _course_matches), or None."""
        if not course:
            return None
        entry = self.league.schedule_entry(today or time.strftime("%Y-%m-%d"), course,
                                           fits=lambda e: self._course_matches(e, course))
        return entry if entry and self._course_matches(entry, course) else None

    def _course_matches(self, entry: dict, course: str | None) -> bool:
        """Whether a schedule week is on `course`: by either of its names, unless that's surely another
        course (_names_differ: a scheduled Pinehurst No. 2 isn't Pinehurst No. 4)."""
        if not course:
            return False
        from ..course_card import plain_name

        names = [n for n in (entry.get("course"), entry.get("gspro_course")) if n]
        if plain_name(course) in {plain_name(n) for n in names}:
            return True
        # (a generic alias, "Pinehurst", doesn't overrule the week's numbered "Pinehurst No. 2")
        return (any(course_match_score(n, course) >= 0.6 for n in names)
                and not any(self._names_differ(n, course) for n in names))

    def _classify_round(self, names: list[str], course: str | None,
                        holes: tuple[int, int] | tuple[()] | None = None) -> tuple[dict, str | None]:
        """Decide whether this opening tee is a league round. Returns (schedule entry, practice reason):
        a None reason means it's a league round on that entry; a string reason means practice (still
        commentated, just without league framing). A league round needs a scheduled course that's still
        in date, the right number of balls for the format (two, or one for alternate shot), those
        balls to be one registered team, and (when the setup screen was read) the week's nines. `round_week` in config forces a week when the course can't be
        read, bypassing only the course and date gates. Works the same for the tee's ball count and the
        setup screen's slots, since a player sets up exactly the balls they'll play (one for alternate).
        `holes` (start, count) stands in for the setup screen's; () leaves the holes unchecked."""
        c = self.cfg.commentary
        setup_holes = self._setup_holes if holes is None else holes
        if holes is None and setup_holes and not self._setup_holes_read:
            setup_holes = self._unread_holes()
        today = time.strftime("%Y-%m-%d")
        if not self.league.players:
            return {}, "no league roster configured"
        if not self.league.data.get("schedule"):
            return {}, None  # no schedule configured: treat any round as the round
        forced = self.league.week_entry(c.round_week) if c.round_week else None
        # A round in a practice booking is practice, even on this week's course with a full team.
        if not forced and self.bookings and (b := self.bookings.at()) and b.practice:
            return {}, f"the sim is booked for practice {b.start:%H:%M}-{b.finish:%H:%M} on SuperSaaS"
        entry = forced
        if not entry:
            # Course only: schedule_entry falls back to a date match, which would wrongly claim an
            # unscheduled course played on a scheduled day, so require the entry to match the course.
            entry = self._course_week(course, today)
            if not entry:
                return {}, f"{course!r} isn't on this year's schedule"
            end = entry.get("end_date")
            if end and str(end) < today:
                return {}, f"week {entry.get('week')} ({entry.get('course')}) was due by {end}"
        fmt = normalize_format(c.round_format or entry.get("format"))
        # Alternate shot: one GSPro profile for the team, or both partners in GSPro's alternate shot mode.
        expected = (1, 2) if fmt == "alternate" else (2,)
        if len(names) not in expected:
            return {}, (f"{len(names)} ball(s) on the tee, {FORMAT_NAMES.get(fmt or '', 'league play')} "
                        f"shows {' or '.join(map(str, expected))}")
        if fmt == "alternate" and len(names) == 2 and self._setup_mode_read and self._setup_format != "alternate":
            return {}, "two balls on an alternate shot week, but GSPro's game mode isn't alternate shot"
        team, bad = self._lone_team(names)
        if bad:
            return {}, bad
        if (not forced and setup_holes
                and (bad := self._wrong_holes(entry, *setup_holes))):
            return {}, bad
        if not forced and team:
            # A team posts one round per week: once it has a full league round on every week scheduled
            # on this course, another round here is practice.
            weeks = [e for e in self.league.data.get("schedule") or [] if self._course_matches(e, course)]
            played = [c for c in self.store.league_round_courses(team) if self._course_matches(entry, c)]
            if weeks and len(played) >= len(weeks):
                return {}, f"{team} already played week {entry.get('week')}'s league round on {entry.get('course')}"
        return entry, None

    def _unread_holes(self) -> tuple[int, int] | None:
        """The holes to judge a setup by when its HOLES tab wasn't read. GSPro's log skipping hole 1
        puts the round on the back nine (or wherever its first score was). Otherwise, GSPro's default
        all 18 when the booth saw a setup tab selected (so it would have seen HOLES opened), and nothing
        to check when it saw none: then it couldn't read the tabs at all (11-17: a back-nine league
        round set aside as practice over an 18 the booth only assumed)."""
        g = self.gspro
        start = None
        if self._log_rounds:
            start = g.first_hole + 1 if g.first_hole is not None else 10 if g.starts_late else None
        if start is not None and start != 1:
            return start, 19 - start
        if self._setup_tab_lit_at < self._setup_began_at:
            return None
        return self._setup_holes

    @staticmethod
    def _wrong_holes(entry: dict, start: int, count: int) -> str | None:
        """Why the holes picked on the setup screen (18 unless its HOLES tab said otherwise) aren't
        this week's league holes, or None when they match."""
        want_start, want_count = entry.get("start_hole"), entry.get("hole_count")
        if (want_start is None or want_start == start) and (want_count is None or want_count == count):
            return None
        names = {(1, 18): "all 18", (1, 9): "the front 9", (10, 9): "the back 9"}
        picked = names.get((start, count), f"{count} holes from hole {start}")
        week = entry.get("holes") or names.get((want_start or start, want_count or count), "other holes")
        return f"set up for {picked}, but week {entry.get('week')} is {week}"

    def _note_holes_only(self, r: Round, names: list[str], course: str | None, practice: str | None) -> None:
        """Keep the week a practice round would be but for the holes on the setup screen (see
        _league_after_quit)."""
        if practice and self._setup_holes:
            entry, why = self._classify_round(names, course, holes=())
            r.league_if_holes = entry if why is None and entry else None

    def _league_after_quit(self, r: Round) -> bool:
        """A practice round set up for the wrong holes (18 on a front-nine week) and quit after exactly
        the week's holes was that league round: it becomes one before it's recorded. True if it did."""
        entry = r.league_if_holes
        if r.is_league or r.is_tournament or not entry or r.start_hole_assumed:
            return False
        def scored(g: str) -> set[int]:
            return set(r.log_scores.get(g, {})) | set(r.card_scores.get(g, {}))
        # Who must have finished each hole: every ball, or in alternate shot each team's one ball (the
        # partner who holed out has the score; the practice round still has them on balls of their own).
        if normalize_format(self.cfg.commentary.round_format or entry.get("format")) == "alternate":
            groups: dict[str, list[str]] = {}
            for g, player in r.roster.items():
                team = self.league.player_team.get(player) if player else self.league.match_team(g)
                groups.setdefault(team or g, []).append(g)
            balls = list(groups.values())
        else:
            balls = [s.gspro for s in r.sides]
        holes = [set().union(*(scored(g) for g in ball)) for ball in balls]
        playing = [h for h in holes if h]
        if not playing:
            return False
        done = 0   # the holes every ball has finished, from the first: a gap isn't a finished round
        while done < r.seq and all(done + 1 in h for h in playing):
            done += 1
        if done != entry.get("hole_count") or r.start_hole != entry.get("start_hole"):
            return False
        # Anyone who played on past the week's holes wasn't stopping at them (only reaching the next tee is).
        if any(q > done for h in playing for q in h) or any(
                s.round_id == r.id and (s.hole_seq or 0) > done and not s.mulligan for s in self.state.shots):
            return False
        entry, why = self._classify_round(list(r.roster), r.course, holes=(r.start_hole, done))
        if why is not None or not entry:
            log.info("Round %s: quit after the week's holes but not a league round after all (%s)", r.id, why)
            return False
        r.make_league(entry, done, self.cfg.commentary.round_format)
        if r.shot_floor is not None:
            self.store.mark_league(r.shot_floor, record=r.uuid)
            # The league's format can change who a shot is credited to (one profile for an alternate
            # shot team is both partners): re-stamp the round's shots, gimmes included, the way they'd
            # have been recorded.
            anon = set(r.anonymous_players())
            for player in self.store.players_after(r.shot_floor, record=r.uuid):
                shared = not r.solo(r.side_for(player)) and (r.format == "scramble" or r.no_scramble
                                                             or self.gspro.team_scores)
                self.store.restamp_shots(r.shot_floor, player, fmt=r.player_format(player),
                                         attributed=player not in anon, shared_ball=shared, record=r.uuid)
        if r.format == "alternate":
            self._alternate_tee_finishes(r)
        log.info("Round %s: set up for %s holes but quit after week %s's %s: a league round after all (%s)",
                 r.id, self._setup_holes[1] if self._setup_holes else "more", entry.get("week"), done,
                 ", ".join(f"{s.label} strokes {s.strokes}" for s in r.sides))
        return True

    def _alternate_tee_finishes(self, r: Round) -> None:
        """Alternate shot's tee shots finish where the partner's second stroke starts: the practice
        round a promoted one began as treated them as two balls, so _ctp_facts never filled these in."""
        mine = [s for s in self.state.shots if s.round_id == r.id and not s.mulligan and s.hole_seq]
        for tee in (s for s in mine if s.shot_number == 1 and s.shot_id is not None):
            side = r.side_for(tee.player)
            nxt = next((s for s in mine if s.hole_seq == tee.hole_seq and s.shot_number == 2
                        and r.side_for(s.player) is side), None)
            if nxt and nxt.lie_before == "green" and (y := to_yards(nxt.to_pin_before)) is not None:
                self.store.fill_tee_finish(tee.shot_id, y)

    def _team_finishes(self, r: Round) -> None:
        """A shared ball's full shot finished where the team's next stroke starts. GSPro's log has no
        position for it (at a tee it still shows the last hole), so the per-player finish never comes:
        alternate shot's ball is the partner's next, a scramble's is the picked ball's (the lifted
        ones' rest is never shown). A penalty makes the next stroke two on, so it's left alone."""
        mine = [s for s in self.state.shots if s.round_id == r.id and not s.mulligan and s.hole_seq]
        for s in mine:
            if s.shot_id is None or s.shot_id in self._team_finished or s.shot.is_putt or s.shot_number is None:
                continue
            side = r.side_for(s.player)
            fmt = r.player_format(s.player)
            if side is None or fmt not in TEAM_BALL or (fmt == "scramble" and not s.picked):
                continue
            nxt = next((x for x in mine if x.hole_seq == s.hole_seq and x.shot_number == s.shot_number + 1
                        and r.side_for(x.player) is side), None)
            if nxt is not None and (y := to_yards(nxt.to_pin_before)) is not None:
                self._team_finished.add(s.shot_id)
                # Alternate shot's is the only finish there is (one from before the round was
                # alternate chained the player's own strokes); a scramble's pick screen may have one.
                self.store.fill_finish(s.shot_id, y, replace=fmt == "alternate")

    def _canonical_course(self, course: str | None) -> str | None:
        """The league's name for a course when it's on the schedule, else the name as given. GSPro
        names some courses differently (Balsams Panorama = Wentworth by the Sea); this maps both the
        setup/load name and the HUD name to one string so a prepared opener matches its tee."""
        if not course:
            return course
        entry = self._course_week(course)
        return entry.get("course") if entry else course

    def _card_course(self, course: str | None) -> str | None:
        """The name to find a setup-read course's card folder by: see _card_name, else the name as read."""
        sched = self._course_week(course)
        return (self._card_name(sched) if sched else None) or course

    def _doubt_card_notes(self, card, course: str | None, lookup: str | None, entry: dict | None = None) -> None:
        """Mark a card found by a schedule week's name (`lookup`; see _card_course, _card_name) rather than
        the course as read unsure, so its scorecard stands but its listing's notes aren't said, unless the
        name as read is exactly one of that week's names (GSPro's Balsams Panorama for Wentworth) or
        finds that same card, surely, itself. "Swiss Alpine Valley" isn't the scheduled "Alpine Valley -
        Meadows & Sugar Creek". Off air the notes aren't read and that search isn't run: the card is
        unsure, which only withholds the notes if commentary comes on mid-round."""
        from .. import course_card

        if not card or not lookup or lookup == course:
            return
        if not course:   # nothing read to corroborate the week's name
            card.name_unsure = True
            return
        if entry is None:
            entry = self._course_week(course) or {}
        plain = course_card.plain_name
        if plain(course) in {plain(entry.get("course") or ""), plain(entry.get("gspro_course") or "")}:
            return
        if not self.commentary_on:
            card.name_unsure = True
            return
        try:
            path, unsure = course_card.locate(self.cfg.source.gspro_courses_dirs, course,
                                              self.gspro.course if self._log_truth else None)
            sure = not unsure and path is not None and path == card.path
        except Exception:
            sure = False
        if not sure:
            card.name_unsure = True

    def _card_name(self, entry: dict) -> str | None:
        """The name a schedule week's course card is found by. GSPro lists Wentworth by the Sea as
        Balsams Panorama (the schedule's gspro_course): the league copies Wentworth's files into GSPro's
        Balsams folder, and they still name the course Wentworth. So the league's name, when an
        installed course is exactly that; else GSPro's."""
        from .. import course_card

        course, listed = entry.get("course"), entry.get("gspro_course")
        if listed and course and course_card.identify(self.cfg.source.gspro_courses_dirs, course):
            return course
        return listed or course

    def _course_test(self, r: Round):
        """Whether a course name (a past league week's) is this round's course, or None to compare
        names. With the round's GSPro course known, the name must be that installed course's own
        (or its schedule alias): "Alpine Valley" is in "Swiss Alpine Valley" by name, but the
        two are different courses, and a course that isn't installed isn't its nearest namesake."""
        mine = r.course_card.folder if r.course_card else None
        if not mine:
            return None
        from .. import course_card

        dirs = self.cfg.source.gspro_courses_dirs
        plain = course_card.plain_name
        # The schedule's own name for a GSPro course (Wentworth by the Sea is Balsams Panorama), but only
        # by exact name: a near one would turn "Swiss Alpine Valley" into "Alpine Valley".
        alias = {plain(e["course"]): self._card_name(e) for e in self.league.data.get("schedule") or []
                 if e.get("course") and e.get("gspro_course")}
        return lambda name: (course_card.identify(dirs, alias.get(plain(name), name)) or "").lower() == mine.lower()

    def _tee_course(self, hud: Hud) -> str | None:
        """The course a round starting at this tee is on. The HUD's read is the short name players
        know, but it's OCR of whatever is on screen (an open settings menu reads "GAME MENU"); GSPro's
        log names the course it loaded, so a HUD read that isn't that course gives way to it."""
        read = self.current_course or hud.course
        g = self.gspro
        logged = g.course_display if self._log_truth and g.mode == "course" else None
        if not logged or (read and self._same_course(read, logged) and not self._other_course(read, logged)):
            return read
        if read:
            log.info("Tee course: HUD read %r, but GSPro loaded %r; using GSPro's", read, logged)
        return logged

    def _refresh_course_card(self, r: Round, course: str | None) -> None:
        """Re-find a pre-built round's card now that GSPro's log has surely named the selected folder:
        the setup screen can be read before it does, and a name two courses share took the wrong one."""
        lookup = self._card_course(course)
        card = self._load_course_card(lookup)
        self._doubt_card_notes(card, course, lookup)
        old = r.course_card
        moved = card is not None and old is not None and (card.folder or "").lower() != (old.folder or "").lower()
        logged = self.gspro.course if self._log_truth else None
        if (moved and card.name_unsure and not old.name_unsure and self._same_course(course, old.name)
                and not (logged and card.folder and logged.lower() == card.folder.lower())):
            # A short name two courses share ("Alpine Valley") doesn't unseat the card the setup surely found.
            card = old
        # (under the round's own name too: its handicap differential looks the tees up by it)
        for name in dict.fromkeys(n for n in (course, r.course) if n):
            self._keep_course_tees(name, card)
        if card is old:
            return
        if card and (old is None or card.name != old.name or moved or (old.name_unsure and not card.name_unsure)):
            log.info("Course card for %r is %s, not %s, now GSPro's log names the course", course, card.name,
                     r.course_card.name if r.course_card else "none")
            r.apply_course_card(card)
