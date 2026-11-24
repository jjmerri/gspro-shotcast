"""Starting a round: building it, its players, and the shots hit before it was known.

Part of Engine (commentator/engine/core.py), which is these parts together."""
import asyncio
import re
import time

from .common import PendingOpening, REST_WAIT_SECONDS, _gspro_name, log
from .. import perf_log
from ..facts import course_listing_facts
from ..health import RoundCounters
from ..hud import Hud, Scorecard
from ..league import course_match_score
from ..models import RoundStartEvent
from ..prompts import opener_prompt
from ..round import FORMAT_NAMES, Round, TEAM_BALL, name_key as _name_key, name_ratio as _name_ratio


class RoundStartMixin:
    """Starting a round: building it, its players, and the shots hit before it was known."""

    def _build_round(self, names: list[str], course: str | None, entry: dict, is_league: bool,
                     card_course: str | None = None, use_current_hole: bool = True,
                     slot_names: list[str] | None = None) -> Round:
        """A Round from a resolved roster, course, and schedule entry, with its course card applied.
        Shared by the tee path and the pre-round setup read. `card_course` overrides the name used to
        find the course-card folder (GSPro's own name), for a practice round with no schedule entry.
        `use_current_hole` is off for the pre-round build, where the HUD hole number is last round's
        (stale) rather than this round's start, so an unscheduled practice round's start stays unknown."""
        if self.round and self.round.ended:
            # The previous round is over: its lines (and their scores) must not colour this one.
            self.state.recent_lines.clear()
        c = self.cfg.commentary
        scheduled = (c.round_format or entry.get("format")) if is_league else None
        start = entry.get("start_hole") or (self.hole_number if use_current_hole and self.hole_number_read else None)
        holes = entry.get("hole_count") or c.round_holes
        confirmed = bool(entry.get("hole_count"))
        # What the setup screen showed beats the schedule's or config's guess; for a league round, an
        # unread HOLES tab only as far as it was judged by (_unread_holes).
        setup = self._setup_holes if self._setup_holes_read or not is_league else self._unread_holes()
        if setup:
            start, holes = setup
            confirmed = self._setup_holes_read or entry.get("hole_count") == holes
        r = Round(league=self.league, gspro_players=names, course=course, scheduled_format=scheduled,
                  holes=holes, start_hole=start, schedule=entry,
                  is_league=is_league, slot_names=slot_names)
        r.holes_confirmed = confirmed
        self._apply_setup_format(r)
        lookup = card_course or (self._card_name(entry) if entry.get("gspro_course") else None) or self._card_course(course)
        card = self._load_course_card(lookup)
        if not card_course:
            self._doubt_card_notes(card, course, lookup, entry or None)
        self._keep_course_tees(course, card)
        if card:
            r.apply_course_card(card)
            # Feed the course's own altitude to the carry model (unless config already set one).
            if card.altitude_ft is not None and course:
                self.carry.altitude.setdefault(course, card.altitude_ft)
            log.info("Course card for %r: %s, par %s over holes %s, altitude %s ft", course, card.name,
                     sum(card.par.values()), sorted(card.par), card.altitude_ft)
        if self.current_par:
            r.par.setdefault(1, self.current_par)
        return r

    async def _note_rest(self, shot_id: int, fire: int, impact: float, wait: float = REST_WAIT_SECONDS) -> None:
        """Store when GSPro's log had this shot's ball stop (wall clock), for its highlight clip to end on:
        the log says so whether GSPro's tiles stay up through the shot or hide until the next one."""
        deadline = time.monotonic() + wait
        while "rest_at" not in self.gspro.fire_info(fire) and time.monotonic() < deadline:
            await asyncio.sleep(0.25)
        at = self.gspro.fire_info(fire).get("rest_at")
        if at is not None and at >= impact:
            self.store.set_shot_rest(shot_id, time.time() - (time.monotonic() - at))

    def _bg(self, coro) -> None:
        """Run a coroutine detached, keeping a reference so it isn't GC'd and swallowing its result."""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(lambda t: (self._bg_tasks.discard(t), t.cancelled() or t.exception()))

    def _pick_round_voice(self, r: Round) -> None:
        """With voice fallbacks, pick the round's voice once: before an opener aired over the load,
        else as the round starts, so the whole round, opener included, has one voice."""
        if r.voice_from_seq is None:
            r.voice_from_seq = r.seq
            self.speech.start_round(max(r.holes - r.seq + 1, 1), len(r.roster))

    def _start_round(self, r: Round) -> None:
        if self.store.roll_session_if_idle():
            log.info("Booth idle for hours: round %s starts a new session (%s)", r.id, self.store.session_id)
            self.state.shots.clear()   # "today" starts over with the new session
        struck = self._struck_new_round()
        self.round = r
        perf_log.mark("round_start", round=r.id)
        r.loaded_folder = self.gspro.course if self._log_truth and self.gspro.mode == "course" else None
        self._squabbit_kick()   # a league round's question goes up; any other round ends the last one's
        self._round_started_at = time.monotonic()
        self._new_round_struck, self._range_exits_seen = None, self.gspro.range_exits
        self._card_join = False   # a round's on: a scorecard seen before it doesn't join another after it
        self._round_course_entries = self.gspro.course_entries   # its course, for _courses_at_round_end
        self._opening_fire = None
        if struck:
            self._adopt_opening_shots(r)
        self.state.recent_lines.words.reset()
        r.shot_floor = self.store.last_shot_id()
        if adopted := [v[0] for v in self._fire_rows.values() if v[0] is not None and v[1].round_id == r.id]:
            r.shot_floor = min(adopted) - 1   # the drives struck before it started are its own
        # Its record, from here on every shot's (a round a stopped booth left open, continued, keeps its own).
        r.uuid = self.store.begin_round(r.uuid, course=r.course, players=dict(r.roster), seats=r.slot_names,
                                        since_id=r.shot_floor)
        r.loaded_folder = r.loaded_folder or self.store.round_folder(r.uuid)   # (a round continued: its folder kept)
        self._note_tournament(r)   # a round joined mid-way already has its syncs in the log
        self._hc = RoundCounters(fires_at_start=self.gspro.fires, kinds_at_start=dict(self.gspro.kinds))
        if self.cfg.health.enabled and self.cfg.source.type != "replay":
            # The booth runs for days: what startup checked may have changed since (GSPro updated and
            # restarted, the monitor swapped), so check again as each round starts.
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None     # a round started outside the booth's loop (tests): nothing to check
            if loop:
                loop.create_task(asyncio.to_thread(self._health_checks, "round start"), name="health")
        self._setup_holes = self._opener_prepared_holes = None   # the next round reads its own setup
        self._setup_holes_read, self._holes_snap = False, None
        self._setup_format = self._opener_prepared_format = None
        self._setup_tints = self._opener_prepared_teams = None
        self._setup_mode_read, self._setup_course = False, None
        self._scramble_pairs = set()
        self._putted_on_last_hole = self._round_end_queued = False
        self._live_round_start()
        self._pick_round_voice(r)
        # Close the "fresh empty scorecard" window so this same tee can't re-trigger a start.
        self._holes_played_at = 0.0
        self._news_bits, self._news_aired, self._news_moment = [], [], None
        self._start_round_asides(r)
        # Per-round live state must not carry into a new round (a reused (idx, hole) would suppress a
        # real penalty call, a streak would continue, a stale call would be graded against a new putt).
        self._narrated_pen.clear()
        self._pen_called.clear()
        self._note_uses.clear()
        self._fir_streak.clear()
        self._last_hud_shot.clear()
        self._pending_finish.clear()
        for trail in self._carry_trail.values():   # no next stroke now: near-pin shots are still checked
            for _, key, elev, pin, args, _scramble in trail:
                if args:
                    self._check_carry(key, elev, pin, args)
        self._carry_trail.clear()
        self._last_row.clear()
        self._mulligans_called.clear()
        self._finish_seq.clear()
        self._pending_prediction.clear()
        self._calls.clear()

    def _opener_io(self, r: Round, first: str | None = None, preround: bool = False) -> tuple[str, str]:
        season = {s.key: self.store.season_rounds(s.key, "league" if r.is_league else "practice") for s in r.sides}
        # The opening hole comes from the round's start hole and the course card's par when we know it
        # (league rounds: Front/Back 9 from the schedule). For a pre-round practice build the start is
        # unknown, so leave the first hole out rather than claim the stale HUD hole; a tee-time opener
        # (no preload) still has the live HUD hole.
        if r.start_hole and not r.start_hole_assumed:
            par = r.par.get(1)   # r.par is keyed by the round's hole order, not the course's
            hole = f"hole {r.start_hole}" + (f", par {par}" if par else "")
        else:
            hole = None if preround else self.state.current_hole
        facts = (r.opening_facts(season, first_up=self._display(first), hole=hole)
                 + self._recall_opener_facts(r) + self._league_facts(r) + self._weather_facts())
        if self._no_members(r):   # the practice-round framing assumes league players
            facts = [f for f in facts if not f.startswith("This is a practice round, not a league match")]
        c = self.cfg.commentary
        return opener_prompt(facts=facts, recent_lines=self.state.recent_lines, max_words=c.opener_max_words,
                             max_lines=c.opener_max_lines, league=r.is_league,
                             course_notes=course_listing_facts(r.course_card))

    def _same_course(self, x: str | None, y: str | None) -> bool:
        a, b = self._canonical_course(x), self._canonical_course(y)
        return bool(a and b and (a.lower() == b.lower() or course_match_score(a, b) >= 0.6))

    def _setup_course_moved(self, x: str, y: str) -> bool:
        """Whether setup course `y` is another than `x` (as _maybe_front_load writes the opener again)."""
        return x != y and (not self._same_course(x, y) or self._other_course(x, y))

    def _other_course(self, x: str | None, y: str | None) -> bool:
        """Whether two names _same_course may take for one course are surely two (_names_differ), as
        given or by the league's names for them (a week's "Pinehurst" is its Pinehurst No. 2)."""
        return self._names_differ(x, y) or self._names_differ(self._canonical_course(x), self._canonical_course(y))

    def _names_differ(self, x: str | None, y: str | None) -> bool:
        """Whether two course names are surely two courses: other numbers
        ("Pinehurst No. 2" isn't "Pinehurst No. 4"), or installed courses each surely found in another GSPro
        folder ("Swiss Alpine Valley" isn't "Alpine Valley - Meadows & Sugar Creek"). A schedule week's own
        two names for its course (Wentworth by the Sea, GSPro's Balsams Panorama) are one."""
        from .. import course_card

        plain = course_card.plain_name
        a, b = plain(x or ""), plain(y or "")
        if not a or not b or a == b:
            return False
        alias: dict[str, str] = {}
        for e in self.league.data.get("schedule") or []:
            names = {plain(n) for n in (e.get("course"), e.get("gspro_course")) if n}
            if {a, b} <= names:
                return False
            if e.get("course") and e.get("gspro_course") and names & {a, b}:
                alias.update(dict.fromkeys(names & {a, b}, self._card_name(e)))
        na, nb = (set(re.findall(r"\d+", n)) for n in (a, b))
        if na and nb and na != nb:
            return True
        try:
            dirs = self.cfg.source.gspro_courses_dirs
            (fa, ua), (fb, ub) = (course_card.locate(dirs, alias.get(n, name)) for n, name in ((a, x), (b, y)))
        except Exception:
            return False
        # (a short name two courses share, "Alpine Valley", proves nothing)
        return bool(fa and fb and not ua and not ub and fa != fb)

    def _pending_mismatch(self, p: PendingOpening, is_league: bool, course: str | None,
                          names: list[str]) -> str | None:
        """Why a prepared opener (league or practice) doesn't fit this tee, or None when it does.
        Matched on: the same league-vs-practice call, the same course (by the league's canonical name,
        so the GSPro setup name and the HUD name agree), and the same set of league players."""
        if p.is_league != is_league:
            return f"prepared {'league' if p.is_league else 'practice'}, tee reads {'league' if is_league else 'practice'}"
        if not self._same_course(p.course, course) or self._other_course(p.course, course):
            return f"course {p.course!r} != tee {course!r}"
        tee_players = frozenset(pl for n in names if (pl := self.league.match_player(n)))
        if p.players != tee_players:
            return f"players {sorted(p.players)} != tee {sorted(tee_players)}"
        if not p.aired and p.colour_teams != self._setup_teams_key():
            return "written before the team colours changed"
        if p.guessed and p.roster_key != self._roster_key(names):
            return f"made for the last roster {sorted(p.roster_key)}, tee reads {sorted(self._roster_key(names))}"
        return None

    def _with_setup_players(self, names: list[str], course: str | None) -> tuple[list[str], list | None]:
        """The tee's names plus anyone the setup screen had that the tee didn't show (guests not up
        first), and the setup's slot order: when Play was pressed on this course recently and the tee's
        players are all on it. Taken once."""
        s, self._played_setup = self._played_setup, None
        seen, self._setup_seen = self._setup_seen, None
        if (owner := self._setup_course) and self._other_course(owner, course):
            # The tee is surely on another course than the setup read (its load screen was missed): the
            # setup's nines, game mode and team colours aren't this round's either.
            log.info("Tee on %r, not the setup's %r: its holes, game mode and team colours aren't used", course, owner)
            self._setup_holes, self._setup_holes_read, self._holes_snap = None, False, None
            self._setup_format, self._setup_mode_read, self._setup_tints = None, False, None
            self._setup_course = None
        if not s and seen and self._same_course(seen[0], course):
            s = seen
            log.info("Using the setup screen's players %s (its load screen wasn't read)", seen[1])
        if not s:
            return names, None
        s_course, roster, slots, at = s
        if (time.monotonic() - at > self.cfg.commentary.preround_ttl_seconds or not self._same_course(s_course, course)
                or self._other_course(s_course, course)):
            return names, None
        tee = self._roster_key(names)
        if not tee <= self._roster_key(roster):
            log.info("Setup roster %s doesn't cover the tee's %s: not used", roster, names)
            return names, None
        extra = [n for n in roster if not self._roster_key([n]) <= tee]
        if extra:
            log.info("From the setup screen, not on the tee: %s", extra)
        return names + extra, list(slots)

    def _guest_name(self, raw: str) -> str | None:
        """The round's GSPro name a player-box read is: the same name, then the same but for OCR's
        look-alike characters, then a letter or two off one player and no other. None when it's nobody's,
        or a name held as maybe another player (see _note_player)."""
        r = self.round
        if not r or r.ended or not raw:
            return None
        cf = raw.casefold()
        if g := next((g for g in r.roster if g.casefold() == cf), None):
            return g
        key = _name_key(raw)
        if cf in r.held or not key:
            return None
        same = [g for g in r.roster if _name_key(g) == key]
        if same:
            return same[0] if len(same) == 1 else None
        scored = sorted(((_name_ratio(g, raw), g) for g in r.roster), reverse=True)
        if scored and scored[0][0] >= 0.8 and (len(scored) == 1 or scored[1][0] < scored[0][0] - 0.1):
            return scored[0][1]
        return None

    def _note_player(self, hud: Hud) -> None:
        """Keep the round's players right from each setup's player box.
        - A first-tee setup for someone the round doesn't have (a guest who wasn't up first, the setup
          screen unread) adds them, so their shots and scores are theirs.
        - A name a letter or two off someone who's already teed off is held until it's read at another
          shot: a misread doesn't repeat there, another player (Mike, then Mikey) does. One that's a
          player's name with letters missing waits for the scorecard.
        - A guest added under a misread takes the spelling the reads settle on."""
        r = self.round
        if not r or r.ended:
            return
        raw = _gspro_name(hud.player_text or "")
        if len(raw) < 2 or not re.search(r"[^\W\d_]", raw):
            return
        first_tee = r.seq == 1 and hud.shot_number == 1 and hud.score_to_par == "E"
        at = (r.seq, hud.shot_number)
        match = self.league.match_player(hud.player_text)
        if held := r.held.get(raw.casefold()):
            # Read again at another shot: a player, not a misread. Unless it's someone's name with
            # letters missing (John for John B), which OCR can repeat: then only the card can add them.
            if held[2] != at and not self._shortened(r, raw):
                del r.held[raw.casefold()]
                self._add_to_round(r, held[0], match, teed=held[1])
            return
        g = next((g for g, p in r.roster.items() if match and p == match), None) or self._guest_name(raw)
        # A name a letter or two off someone who's teed off already (Mikey after Mike), or off more
        # than one player: maybe a misread, maybe another player.
        near = (g and not match and g in r.teed and _name_key(g) != _name_key(raw)) or (
            not g and any(_name_ratio(n, raw) >= 0.8 for n in r.roster))
        # GSPro's player menu opened since the last hole: a name close to a player's is likelier someone added.
        menu = any(r.seq - 1 <= seq <= r.seq for seq, _ in r.menus)
        fresh = near and menu
        newcomer = not first_tee and hud.shot_number == 1 and (
            (g in r.left) if g and not fresh else (fresh or not near))
        up, swapping = self.gspro.active_player, any(a[2] == r.seq for a in r.arrivals)
        if newcomer and menu and self._old_slot_up(r, len(r.arrivals) if swapping else 0) \
                and (not near or up >= len(r.slot_names)):
            # Teeing off in a slot the round has someone else in, since the menu opened: a player added as
            # another was removed. Who left shows once the hole's done.
            if r.is_league:
                log.info("%r joined league round %s partway: not added", raw, r.id)
                return
            name = raw if fresh or not g else g
            if any(a[0] == name for a in r.arrivals):
                return   # read again on a later tee: the hole and slot they first teed off in stand
            r.arrivals = [a for a in r.arrivals if a[2] == r.seq] + [(name, match, r.seq, up)]
            log.info("Round %s: %r teed off in GSPro slot %s (%r's) since the player menu opened: added once "
                     "who left is clear", r.id, name, up, r.slot_names[up] if up < len(r.slot_names) else None)
            return
        if newcomer and not swapping and self._new_slot_up(r):
            # Teeing off in a slot GSPro has and the round doesn't: someone added partway (or back).
            if r.is_league:
                log.info("%r joined league round %s partway: not added", raw, r.id)
                return
            last = next((n for n in reversed(r.slot_names) if n), None)
            # They've had the slot since the holes it finished in a row up to this one began (read
            # late, perhaps); from the first hole, they were there all along.
            scores, up, joined = self._log_view()[0], self.gspro.active_player, r.seq
            while joined > 1 and (up, joined - 1 + r.start_hole - 2) in scores:
                joined -= 1
            self._add_to_round(r, raw if fresh else (g or raw), match, after=last, joined=joined)
            return
        if g and not (first_tee and near):
            if first_tee and g not in r.teed:
                r.teed.append(g)
            if r.roster.get(g) is None and not r.is_league and not self.league.match_team(g):
                self._spelled(r, g, raw)
            return
        if not first_tee:
            return
        if r.is_league:
            # The lineup was a scheduled league match; someone else in it makes it practice, which
            # can't be redone mid-hole. The setup screen (always read) is what classifies it right.
            log.info("First-tee setup for %r, not in league round %s: not added", raw, r.id)
            return
        if near:
            r.held[raw.casefold()] = (raw, list(r.teed), at)
            r.teed.append(raw)   # a later one held or added goes after it, should it turn out a player
            log.info("First-tee setup for %r, close to %s in round %s: held until it's read again",
                     raw, [n for n in r.roster if _name_ratio(n, raw) >= 0.8], r.id)
            return
        self._add_to_round(r, raw, match)

    def _new_slot_up(self, r: Round) -> bool:
        """GSPro's log has the slot just past the last of the round's up: a player added to GSPro's round
        partway (it adds them as the last slot). One further on, or a slot the round hasn't named yet, waits
        for whoever's in it, so nobody's put in someone else's."""
        up = self.gspro.active_player
        return bool(self._log_rounds and r.slot_names and None not in r.slot_names and not r.slot_guess
                    and up == len(r.slot_names))

    def _old_slot_up(self, r: Round, pending: int) -> bool:
        """GSPro's log has one of the round's own slots up (or, with `pending` arrivals already seen this
        hole, the slots just past them): see _note_player's swap."""
        up = self.gspro.active_player
        return bool(self._log_rounds and r.slot_names and None not in r.slot_names and not r.slot_guess
                    and r.format not in TEAM_BALL and up is not None
                    and 0 <= up < len(r.slot_names) + pending)

    @staticmethod
    def _shortened(r: Round, raw: str) -> bool:
        """True when `raw` is a roster name with letters missing."""
        key = _name_key(raw)
        for n in r.roster:
            it = iter(_name_key(n))
            if len(_name_key(n)) > len(key) and all(ch in it for ch in key):
                return True
        return False

    def _drop_no_shows(self, r: Round) -> None:
        """The first hole's done, and everyone in a round plays it. A player the round has who didn't
        (no shot, no score, never set up on its tee, and GSPro's log never had their slot up) was never
        there - a setup slot misread - and leaves the round, so nothing waits on them to finish or
        talks about them. Nobody's shots at all means the names can't be trusted either way: kept."""
        shot = {x.player for x in self.state.shots if x.round_id == r.id}
        names = {g: {g, r.roster[g]} - {None} for g in r.roster}   # shots go by the league name
        if not any(shot & n for n in names.values()):
            return
        seen = self.gspro.players_seen if self._log_rounds else None
        for g in list(r.roster):
            if shot & names[g] or names[g] & set(r.teed) or r.card_scores.get(g) or r.log_scores.get(g):
                continue
            if seen is not None:
                # The log names players by slot: unless theirs is known and was never up, it can't say.
                if r.slot_guess or not r.slot_names or g not in r.slot_names or r.slot_names.index(g) in seen:
                    continue
            old = list(r.slot_names or [])
            if not r.remove_player(g):
                continue
            new = r.slot_names or []
            self.store.round_players(r.uuid, dict(r.roster), r.slot_names)
            for cache in (self._pending_finish, self._finish_seq, self._last_row):
                for k in [k for k in cache if (old[k] if k < len(old) else None) != (new[k] if k < len(new) else None)]:
                    del cache[k]
            log.info("Round %s: %r didn't play the first hole: not a player (players now %s)", r.id, g, list(r.roster))

    def _add_to_round(self, r: Round, name: str, match: str | None, **where) -> None:
        old = list(r.slot_names or [])
        if not r.add_player(name, **where):
            return
        new = r.slot_names or []
        self.store.round_players(r.uuid, dict(r.roster), r.slot_names)
        # An index that's someone else's now: its last shot isn't this one's to finish or correct.
        for cache in (self._pending_finish, self._finish_seq, self._last_row):
            for k in [k for k in cache if (old[k] if k < len(old) else None) != (new[k] if k < len(new) else None)]:
                del cache[k]
        log.info("Round %s: %r added (players now %s, GSPro order %s)", r.id, name, list(r.roster), r.slot_names)
        if self._log_rounds and not self._round_end_queued:
            self._sync_log_scores(r)   # the log's scores by slot: everyone after them moved down one
        if match:
            self._bg(self._apply_live_handicaps(self._round_players([name])))

    def _spelled(self, r: Round, g: str, raw: str) -> None:
        """Count a guest's name as read; one spelled another way (a misread when they were added) takes
        the spelling that's read three times and more than twice as often."""
        votes = r.spellings.setdefault(g, {})
        votes[raw] = votes.get(raw, 0) + 1
        best = max(votes, key=votes.get)
        if best == g or votes[best] < 3 or votes[best] <= 2 * votes.get(g, 0):
            return
        if any(n.casefold() == best.casefold() for n in r.roster if n != g) or not r.rename_player(g, best):
            return
        for x in self.state.shots:
            if x.round_id == r.id and x.player == g:
                x.player = best
        moved = self.store.rename_shots(g, best, after_id=r.shot_floor, record=r.uuid) if r.shot_floor is not None else 0
        log.info("Round %s: %r is read as %r: renamed (%s stored shots)", r.id, g, best, moved)

    def _admit_from_card(self, r: Round, card: Scorecard) -> bool:
        """Rows on GSPro's scorecard with scores the round has nobody for: players it missed (not up first
        on the first tee and that setup unread, or the booth started mid-round). Added in the card's row
        order, which is GSPro's slot order. Not into a league round (see _note_player). True if any."""
        if r.is_league:
            return False
        # Row order is slot order only if the read has every row: a missed row above would shift them.
        if not set(r.roster) <= {m for _, m in r.card_rows if m}:
            log.info("Round %s: scorecard read without all of %s: no players added from it", r.id, list(r.roster))
            return False
        seen = self.gspro.players_seen if self._log_rounds else ()
        slots = max(len(r.slot_names), max(seen, default=-1) + 1)
        if len(r.card_rows) < slots:
            log.info("Round %s: scorecard read %s rows of %s players: no players added from it",
                     r.id, len(r.card_rows), slots)
            return False
        added, above = False, None
        # Players the card has by name (not just a letter or two off): a row close to anyone else may be them.
        on_card = {m for n, m in r.card_rows if m and (_name_key(n) == _name_key(m) or (
            r.roster.get(m) and self.league.match_player(n) == r.roster[m]))}
        for n, g in list(r.card_rows):
            if g is not None:
                above = g
                continue
            # A row not added stops it: it may be someone in a slot, and rows below added after the
            # row above it would take that slot.
            unmatched = [x for x in r.roster if x not in on_card]
            if any(_name_ratio(x, n) >= 0.8 for x in unmatched):
                log.info("Round %s: scorecard row %r is close to %s, not on the card by name: not added "
                         "(nor any row below it)", r.id, n, unmatched)
                break
            if not (card.scores.get(n) and len(n) >= 2 and re.search(r"[^\W\d_]", n)):
                log.info("Round %s: scorecard row %r has no scores read: no rows below it added", r.id, n)
                break
            # The held name it is: the same, or else the one held name it's a letter or two off.
            same = [k for k in r.held if _name_key(k) == _name_key(n)] or [
                k for k in r.held if _name_ratio(k, n) >= 0.8]
            held = r.held.pop(same[0]) if len(same) == 1 else None
            name = held[0] if held else (n.title() if n.isupper() else n)
            before = len(r.roster)
            self._add_to_round(r, name, self.league.match_player(name), after=above, first=above is None)
            if len(r.roster) == before:
                break
            added, above = True, name
            on_card.add(name)
            log.info("Round %s: %r from the scorecard", r.id, name)
        return added

    def _reconcile_slots(self, r: Round, names: list[str]) -> None:
        """Make sure the round has a GSPro player-index order. The setup-screen slots are the source
        when they cover everyone on the first tee; when they're missing or disagree with the tee (a
        misread or skipped setup screen), the tee HUD's top-to-bottom order is used instead (the player
        up first, then the others list), so the GSPro log can still be attributed per player."""
        if not names:
            return
        tee = self._roster_key(names)
        if r.slot_names and tee <= self._roster_key([n for n in r.slot_names if n]):
            return
        log.info("GSPro player order from the first tee, top to bottom: %s (setup slots were %s)",
                 names, r.slot_names)
        r.slot_names = list(names)
        r.setup_slots = None   # the tee's order is a guess at GSPro's
        r.slot_guess = True    # on a green it isn't even the tee's: screen reads place each player
        self.store.round_players(r.uuid, dict(r.roster), r.slot_names)

    def _as_round_names(self, r: Round, slots: list[str | None],
                        pool: list[str] | None = None) -> list[str | None]:
        """Setup-screen slot names as the round spells its players (another read of a pill can differ
        by case or OCR), matched the way _roster_key matches them when only one of the round's names
        is that player; an exact or unmatched name is kept. The round's names are its slots', or `pool`."""
        mine = [g for g in (pool if pool is not None else r.slot_names or []) if g]
        by_key: dict = {}
        for g in mine:
            by_key.setdefault(self._roster_key([g]), []).append(g)

        def own(n: str | None) -> str | None:
            if not n or n in mine:
                return n
            match = by_key.get(self._roster_key([n]), [])
            return match[0] if len(match) == 1 else n
        return [own(n) for n in slots]

    def _take_matching_pending(self, is_league: bool, course: str | None,
                               names: list[str]) -> PendingOpening | None:
        p, self._pending_opening = self._pending_opening, None  # consumed either way
        self._opener_prepared_for = frozenset()  # next round front-loads fresh
        self._opener_gen += 1  # a front-load still composing must not revive _pending_opening post-tee
        if p is None:
            return None
        if why := self._pending_mismatch(p, is_league, course, names):
            log.info("Prepared opener set aside (%s); making the opener at the tee", why)
            return None
        if (age := time.monotonic() - p.created) > self.cfg.commentary.preround_ttl_seconds:
            log.info("Prepared opener set aside (%.0fs old); making the opener at the tee", age)
            return None
        return p

    async def handle_round_start(self, event: RoundStartEvent) -> None:
        self._round_handling += 1   # in a round for the round status while bookings and handicaps load
        try:
            await self._handle_round_start(event)
        finally:
            self._round_handling -= 1

    async def _handle_round_start(self, event: RoundStartEvent) -> None:
        hud, c = event.hud, self.cfg.commentary
        if event.joined:
            if self.round is not None and not self.round.ended:
                log.info("Round %s already joined: a second setup's join is set aside", self.round.id)
                return
            await self._join_round(hud)
            if hud.shot_number == 1:
                self._snap_hole(hud)
            return
        # The opener (aired over the course load) already introduces the opening hole and who's up, so
        # the live pre-shot on that first tee shot is redundant and lands late, just as the player is
        # ready to swing. Skip it for that one shot; the in-flight call still covers the shot.
        self._skip_opening_preshot = c.opener
        # GSPro's own name for the player (the text before the score badge), which the scorecard uses.
        raw = _gspro_name(hud.player_text or "")
        first = self.league.match_player(hud.player_text) or raw or None
        names = [n for n in [raw or first] + [o.name for o in hud.others] if n]
        course = self._tee_course(hud)
        names, slots = self._with_setup_players(names, course)
        await self._bookings_ready()
        entry, practice = self._classify_round(names, course)
        is_league = practice is None
        # Peek at an opener that already played over the load, before the match below consumes it. Only
        # one for this course, and recent, stands in for this round's: an opener that aired for a
        # course the player then backed out of (or long ago) doesn't.
        p = self._pending_opening
        aired_players = (p.players if p and p.aired and self._same_course(p.course, course)
                         and not self._other_course(p.course, course)
                         and time.monotonic() - p.created <= c.preround_ttl_seconds else None)
        # An opener prepared off the setup screen airs now, with no tee-time model or grounding wait,
        # when the tee confirms the same course and the same league team(s). Else it's made here.
        if (pending := self._take_matching_pending(is_league, course, names)) is not None:
            if practice:
                log.info("Practice, not a league round (%s)", practice)
            self._note_holes_only(pending.round, names, course, practice)
            if slots and pending.round.reorder_slots(self._as_round_names(pending.round, slots)):
                log.info("Round %s: the setup screen's players were reordered after the opener was written; "
                         "GSPro's slots are %s", pending.round.id, slots)
            self._reconcile_slots(pending.round, names)
            self._refresh_course_card(pending.round, course or pending.course)
            self._apply_setup_holes(pending.round)
            self._apply_setup_format(pending.round)
            self._apply_log_holes(pending.round)
            self._start_round(pending.round)
            self._note_player(hud)   # the first one up has teed off
            self._number_start_hole(pending.round, opening=True)
            if hud.shot_number == 1:
                self._snap_hole(hud)
            # League: net scoring. Practice: no strokes, but the closer judges form against the index.
            self._bg(self._apply_live_handicaps(self._round_players(names)))
            if pending.aired:
                log.info("Round %s started on %s (%s); opener already aired during the load",
                         pending.round.id, pending.round.course, "league" if is_league else "practice")
            else:
                log.info("Round %s started on %s (%s); prepared opener aired, no tee-time model call",
                         pending.round.id, pending.round.course, "league" if is_league else "practice")
                if c.opener:
                    self._air_lines("opener", pending.lines)
            return
        if is_league:
            await self._apply_live_handicaps(self._round_players(names))
            log.info("League round: course %r is week %s (%s)", course, entry.get("week"), entry.get("course"))
        else:
            # Off the critical path: practice only needs the index for the closer's read on form.
            self._bg(self._apply_live_handicaps(self._round_players(names)))
            log.info("Practice, not a league round (%s): calling the golf without league framing", practice)
        r = self._build_round(names, course, entry, is_league, slot_names=slots)
        self._note_holes_only(r, names, course, practice)
        self._reconcile_slots(r, names)
        self._apply_log_holes(r)
        self._start_round(r)
        self._note_player(hud)   # the first one up has teed off
        self._number_start_hole(r, opening=True)
        if hud.shot_number == 1:
            self._snap_hole(hud)
        log.info("Round %s started on %s: %s; format %s; start hole %s%s", r.id, r.course,
                 "; ".join(f"{s.label} [{', '.join(s.gspro)}] strokes {s.strokes}" for s in r.sides),
                 FORMAT_NAMES.get(r.format or "", "not set"), r.start_hole,
                 " (assumed)" if r.start_hole_assumed else "")
        if r.format_warning:
            log.warning("Round %s: %s", r.id, r.format_warning)
        if not c.opener:
            return
        # An opener already aired over the load and the tee's roster is a subset of what it covered (a
        # partner dropped by an occluded HUD read, not a different round): keep it, don't air a second.
        tee_players = frozenset(p for n in names if (p := self.league.match_player(n)))
        if aired_players is not None and tee_players <= aired_players:
            log.info("An opener already aired over the load for %s; the tee read %s (a subset) - not "
                     "airing a second", sorted(aired_players), sorted(tee_players))
            return
        user, facts_text = self._opener_io(r, first)
        await self._speak("opener", self.opener_llm, user, facts_text, [], mode="round",
                          max_words=self.cfg.commentary.opener_max_words)

    async def _join_round(self, hud: Hud) -> None:
        """Track a round the booth came up in the middle of: same roster and course as a fresh start,
        but the start hole and current hole come from GSPro's log, and there's no opener."""
        raw = _gspro_name(hud.player_text or "")
        names = [n for n in [raw or self.league.match_player(hud.player_text)] + [o.name for o in hud.others] if n]
        course = self._tee_course(hud)
        names, slots = self._with_setup_players(names, course)
        await self._bookings_ready()
        entry, practice = self._classify_round(names, course)
        is_league = practice is None
        if is_league:
            await self._apply_live_handicaps(self._round_players(names))
        else:
            self._bg(self._apply_live_handicaps(self._round_players(names)))
        r = self._build_round(names, course, entry, is_league, use_current_hole=False, slot_names=slots)
        self._note_holes_only(r, names, course, practice)
        g = self.gspro
        r.start_hole, r.start_hole_assumed = g.first_hole + 1, False   # the log's holes are 0-based
        if r.course_card:
            r.apply_course_card(r.course_card)
        current = g.current_hole
        # GSPro (3.59 at least) logs nothing on the next tee until its first shot, so a booth that comes
        # up there still sees the last hole: take the HUD's hole when it's the next one and someone has
        # finished the log's hole (scored or picked up). The group plays a hole together, so the HUD only
        # moves on once it's done; a scramble scores a hole under one partner, so not "everyone".
        if current is not None and hud.hole_number == current + 2 and any(
                h == current for _, h in (*g.hole_scores, *g._picked)):
            current += 1
        r.seq = max(1, current - g.first_hole + 1)
        if r.seq > r.holes:
            r.set_holes(max(r.seq, min(18, r.start_hole + 17) - r.start_hole + 1))
        self.hole_number, self.hole_number_read = current + 1, True
        self._reconcile_slots(r, names)
        self._start_round(r)
        self._number_start_hole(r, joined=True)
        self._note_player(hud)
        log.info("Round %s joined mid-round on %s (%s): %s; started on hole %s, now on hole %s (%s of %s); no opener",
                 r.id, r.course, "league" if is_league else "practice", ", ".join(s.label for s in r.sides),
                 r.start_hole, self.hole_number, r.seq, r.holes)
        self._maybe_prep_asides(r)   # joined past a nine's second hole: no hole change will start it

    def _adopt_opening_shots(self, r: Round) -> None:
        """A round started (or joined) after its opening drive came off a course's own range: that drive
        (and anything since) was recorded with no round open. They're this round's, on the holes GSPro's
        log put them, and their owners are settled against its roster like any of its shots. One still
        being followed when the round started is taken as it's recorded (_adopt_late_shot)."""
        g = self.gspro
        if (first := g.range_exit_fire) is None or g.first_hole is None:
            return
        self._opening_fire = (r.id, first)
        n = sum(self._adopt_row(r, fire) for fire in sorted(self._fire_rows) if fire >= first)
        if n:
            log.info("Round %s: %s shot(s) struck before it started, from its opening drive, are its own", r.id, n)

    def _adopt_late_shot(self, fire: int) -> None:
        r, o = self.round, self._opening_fire
        if o and r is not None and not r.ended and r.id == o[0] and fire >= o[1] and self._adopt_row(r, fire):
            log.info("Round %s: a shot struck before it started, recorded since, is its own", r.id)

    def _adopt_row(self, r: Round, fire: int) -> bool:
        g = self.gspro
        shot_id, rec, _, _ = self._fire_rows[fire]
        if rec.round_id not in (None, r.id):
            return False
        # A drive whose handler only got to it once the round was open is the round's already, but its
        # hole, par and course were read before the log left the range all the same.
        attached = rec.round_id == r.id
        hole = g.fire_info(fire).get("hole")
        if attached and shot_id is not None and shot_id in self._file_pins.values():
            hole = None   # GSPro's round file has filed it (_file_hole_check): its hole stands
        else:
            rec.round_id, rec.hole_seq = r.id, r.seq if hole is None or g.first_hole is None \
                else hole - g.first_hole + 1
        # Struck before the log left the range, it may carry the last round's hole and par: GSPro's log
        # says which hole it was (0-based), the round's card its par.
        label = None
        if hole is not None:
            par, m = r.par.get(rec.hole_seq), re.match(r"Hole (\d+)", rec.hole or "")
            if not rec.hole or (m and int(m[1]) != hole + 1) or (par is not None and rec.par != par):
                label = ", ".join([f"Hole {hole + 1}"] + ([f"par {par}"] if par else []))
                rec.hole, rec.par = label, par
        # Recorded as practice with no format: it's this round's (an unnamed one gets its format when the
        # owner reconciliation names it).
        name = rec.player if rec.player and rec.player != "unknown" else None
        shared = bool(name) and not r.solo(r.side_for(name)) and bool(
            r.format == "scramble" or r.no_scramble or g.team_scores)
        self.store.set_shot_round_class(shot_id, is_league=r.is_league, fmt=r.player_format(name) if name else None,
                                        attributed=bool(name) and not self._anonymous(name), shared_ball=shared,
                                        hole=label, course=r.course, is_tournament=r.is_tournament, record=r.uuid)
        if self._log_truth:
            self._owners_due.add(fire)
        return not attached
