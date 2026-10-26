[← README](../README.md)

# Leagues, formats and handicaps

## League file

Optional. JSON or YAML (`league_file` in `config.yaml`); start from `data/league.example.json`.
Only `teams[].players[].name` is required. Everything except `schedule` and `pronunciations` is handed to the
commentators as context, so add any storylines, player `notes`, nicknames or stats you want them
to know (a file with just `league_name` and `storylines` is fine too). Fields the code itself reads:

| Field | Used for |
|---|---|
| `league_name` | How the booth names the league |
| `teams[].name`, `teams[].aliases` | Team names, and other spellings to match |
| `teams[].nickname` | What the booth calls the team, if set |
| `teams[].players[].name` | The roster (required) |
| `players[].gspro_name`, `players[].aliases` | Matching the name GSPro shows to a roster player |
| `players[].handicap` | 18-hole index, for net scores |
| `pronunciations` | Respellings for the voice only (`{"Szabo": "Sah-bo"}`); plurals and possessives follow, and the transcript keeps the real spelling |
| `handicap_allowances` | Per-format allowance override (`{"scramble": [0.35, 0.15], ...}`) |
| `schedule[]` | Per week: `week`, `course`, `format`, `end_date` (deadline), `date` (fallback match), `holes`/`hole_count`/`start_hole`, `tees`, `ctp_holes`, and `gspro_course` when GSPro's course name differs |
| `ctp_prize`, `week_winner_prize` | Prize amounts the booth can mention |
| `squabbit_invite`, `players[].squabbit_id` | Only with `squabbit.enabled: true` (live handicaps) |

A night counts as a league round only when the players are one registered team and tonight's
course matches an open `schedule` entry; otherwise it's practice. Players not on the roster
(guests, or everyone when there's no league file) are called by their GSPro name, and the early
opener over the course load includes them. `import-league` builds this file from one specific
spreadsheet layout (Schedule, Teams, Results and Standings sheets); most leagues will write the
file by hand.

## Rounds, formats and handicaps

- **League vs practice.** A night is treated as a league round only when a scheduled, in-date
  course is loaded with the right ball count and one registered team. Otherwise it is a
  practice round, and the booth never says points are on the line (opener and closer included).
  With `booking.enabled: true` the booth also reads the sim's SuperSaaS bookings, at startup (so a
  restart mid-round still knows) and as each round is set up, at GSPro's SELECT COURSE or setup
  screen, whichever it sees first: a round played
  in (or within `slack_minutes` of) a booking marked "Practice" is practice, even on the week's
  course with a full team. A "League Week N" booking doesn't pin the week (a group may play two in
  one block), so it, no booking, or SuperSaaS being unreachable leaves the matching above to decide.
  A `commentary.round_week` override still wins. With the stats page publishing, the next two weeks
  of bookings are also on its Schedule tab (see [tracking](tracking.md#stats-web-page)).
- **Opener.** When everyone is on shot 1 at even par, still the full hole away, and nothing has
  been recorded on the course today, the booth opens the broadcast. With the early opener on
  (`commentary.preround`), it reads the course and roster (league players and guests alike) off
  GSPro's setup screen, from the PLAYERS tab with no player dropdown open (checked four times a
  second, so a quick click on to another tab still catches the names), during the
  course load and airs the opener the instant the tee HUD confirms the same course and team.
  If the names were never on screen, the opener is made for the last roster read and airs at
  the tee only if the tee shows exactly those players.
- **Holes in the round** come from the setup screen too: the HOLES tab's FRONT 9 and BACK 9
  boxes (both is 18 from hole 1, one is that nine), sampled every quarter second so a quick
  visit just before Play still counts. A setup that never opens the HOLES tab is taken as
  GSPro's default, 18, but unconfirmed: the opener doesn't say how many holes. Without a setup read (the booth started later), the league schedule or
  `round_holes` is the guess. GSPro's log corrects any of these ("Skipping hole 1", the first
  score line, the hole layout at "Round Is Complete").
- **Par and stroke index** come from GSPro's own course files under `source.gspro_courses_dirs`,
  matched by name, so scoring never waits on OCR. Course altitude is in those files too.
- **Scores** come from GSPro's log: the score line after each ball holed or conceded is GSPro's
  own count for the hole. Its between-hole scorecard fills any hole the log didn't settle (and
  says which hole the round started on), then the strokes seen (the last putt holed, or one more
  for a conceded tap-in).
- **Wrap-up.** When every hole is scored and GSPro says the round is complete, or the last green goes quiet for
  `round_end_idle_seconds`, the booth talks through the round: net against handicap, fairways,
  greens in regulation, scrambling, sand saves, putts, penalties, and the team result.
- **Formats** come from the league file `schedule` (today's entry) or `commentary.round_format`:
  scramble, best ball, team sum, or alternate shot. One player per team in GSPro is a one-ball
  format, and the checker rejects any line that credits a shared shot to one partner. A scramble
  set in GSPro's own game mode overrides the schedule. The booth reads it from the setup screen's
  MATCH SETTINGS tab when that tab is opened before Play, and from GSPro's log after everyone's
  first tee shot otherwise (unless the tab showed SHAMBLE or ALTERNATE SHOT, which also share
  balls in GSPro's log). Every other mode, and a tab never opened, is stroke play: the schedule's
  format stands. The round becomes a scramble, with its teams from the picks (two players
  are one team), its shots stay out of individual stats, and the booth knows whose ball the team is
  playing. Other formats can't be read from GSPro, since leagues usually leave it on
  stroke play and score in their own app.
- **Handicaps.** `handicap` per player is an 18-hole index, halved for nine holes, then the
  format allowance (`handicap_allowances`). With `squabbit.enabled: true`, the booth signs in at
  round start and overwrites the file's handicaps with each player's live Squabbit profile index
  (read-only, best-effort). `python -m commentator squabbit --handicaps` prints them for a check.
- **Squabbit week cards.** `python -m commentator squabbit --cards` saves each league week's
  Squabbit card to `data/squabbit_cards.json` (beside the league file, gitignored): the tee's
  rating and slope, par and stroke index for that tee, and the format's handicap settings. A league
  round then gives strokes exactly as Squabbit does: a USGA course handicap where the format uses
  one, the format's percentages (one-ball shares summed before rounding), the max index and max
  hole score, and Squabbit's stroke index for the holes played. Re-run it when a new season's
  weeks are set up; cards saved for a different invite code are ignored.
  `python -m commentator squabbit --validate [--invite CODE]` recomputes every team's net on a
  season's leaderboards with these rules (2025: 108 of 108 match). Squabbit stays the source of
  truth for net results; GSPro's card can differ.
- **The league week.** With `squabbit.enabled: true`, the same round-start sign-in reads every
  week's Squabbit leaderboard. The opener hears what the other teams have posted for the week and
  the season standings (each week's places from the posted nets, ties sharing the places' points
  from `points_table` in the history file; a week counts once every team has posted or its
  deadline has passed). These replace the spreadsheet's standings when the fetch works. During the
  round the booth tracks each team's net the way Squabbit scores it, so the late-round stakes are
  on net (with what it takes over the remaining holes to tie or pass the week's best posted
  score), and the closer says where the net would place among the week's posted scores.
- **The week's closest to the pin** comes from the season database: the closest league tee shot
  that finished on the green on each CTP hole this week, from every round on the sim, in the
  opener, on the tee, when a ball finishes on the green (along with the group's earlier greens on
  that hole, and whether the mark came from an earlier round), and in the closer. Best ball and team sum weeks only: in
  scramble and alternate shot a player's next stroke is from the team's ball, so where their own
  tee shot finished isn't recorded.
