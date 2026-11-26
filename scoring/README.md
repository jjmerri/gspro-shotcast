# shotcast-scoring

The league scoring rules Shotcast uses: handicap strokes per side and per hole, a team's net to par
hole by hole under its format, and a league's week places, points, flights and season standings.

It's shared: the Shotcast app (this repo's `commentator`) and the Shotcast Cloud server both score
with it, so there's one copy of the rules. The app ships it inside its own install (pyproject.toml's
package discovery); the server installs it from a tag:

    shotcast-scoring @ git+https://github.com/jjmerri/gspro-shotcast@<tag>#subdirectory=scoring

Don't install both the app and this package's wheel into one environment: both provide the top-level
`shotcast_scoring` and would conflict. The app bundles its own copy.

Keep it pure: plain data in, plain data out. No I/O, no sqlite, no imports from the app, no
third-party dependencies, no Squabbit payload parsing (the app turns Squabbit's documents into the
plain data these functions take).

A rule change is a change for both: bump `version` in pyproject.toml, tag the repo, and move the
server's pin to the new tag.

Tests: `python -m pytest scoring/tests` (the app's own `pytest` run collects them too).

- `shotcast_scoring.handicap`: strokes from handicap indexes by a week card's settings, their
  allocation to holes, the max hole score, and `team_holes` (a team's net to par per hole).
- `shotcast_scoring.round`: one round put together from those: a side's strokes from its players'
  indexes (`strokes`), the holes they fall on (`hole_strokes`), and a league team's net per hole from
  its sides (`team_holes`, `team_net`), with a league's names for the formats (`normalize_format`) and its
  allowances (`allowances`, the defaults when it sets none). The booth's live round and the
  server's stored rounds both score with it.
- `shotcast_scoring.excitement`, `shotcast_scoring.highlights`: a round's highlight moments, picked
  as the app's reels pick them (`pick`, `finish`, the booth's lines placed in time by `talk`), with the
  stream's timing a parameter (`Timing`; `APP`, the app's). The app's `highlights.py` and `interest.py`
  keep their own copies for now; `tests/test_highlights_parity.py` runs the app's highlights tests
  against both, so they stay the same.
- `shotcast_scoring.board`: `Board` (week places, points, standings, flights, the stats page's league
  cards) over a `LeagueView` (the app's `League`, or the plain `LeagueData`).

Callers must pass clean data; the package does not validate. It is the app's scoring code as it has
always run, taking what the app has always passed and failing the way it always has. Shotcast Cloud
validates its input (pydantic) before calling it. The few rules new with the package (in
`team_holes`: `complete_sides`, which holes it leaves out, and its two ValueErrors) are in its
docstring; changing what it skips or refuses is a rule change. The other is in Board: a team is in a
week once (its best posted net, else one unposted entry), so a team listed twice never places or
scores twice (a tie keeps its first row). A Board is a snapshot of its inputs (it copies the entries it
keeps): later changes to the caller's week dict, lists or entries aren't seen, so build a new Board
instead.

Build standings the way the app does: `Board(weeks, league, points_table).with_flights(played)` (or
`.with_sim(results, played)`). A Board doesn't resolve flights when it's built; with_flights and
with_sim put each team's week in the flight it played in, else the one it's in now. How the server
handles past flight assignments is a phase 2 design question.
