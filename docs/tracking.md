[← README](../README.md)

# Shot tracking and stats

## Where the data comes from

A launch monitor says how the ball was struck. It doesn't say where it finished, who hit it, or
what GSPro scored. Shotcast puts three feeds together (see `commentator/sources/`):

1. **Shot feed** (`source.type`): one row per swing, with the ball data. See
   [Launch monitors](#launch-monitors) for the options.
2. **GSPro's own log** (`source.gspro_log_truth`, on by default). Tails GSPro's Unity log,
   `output_log.txt`, for what OCR could only guess at: strokes, penalties, the lie a shot was
   played from and where it came to rest, distance to the pin, putt or full shot, hole-outs,
   gimmes, course and par, and when a round starts, ends or is quit. On the driving range nothing
   is recorded until a round is set up. Replays (`run --replay`, `eval`) ignore it, since their
   shots never happened in GSPro.
3. **Screen OCR** (`screen.ocr`, on by default). RapidOCR/ONNX on the CPU reads GSPro's HUD: who
   is up, the club, the carry/total/offline tiles, the minimap and the slope. Nothing leaves the
   PC (with the booth on, the pre-shot hazard look also sends a screenshot to the vision model).

Player names are matched against the league file (each player's `gspro_name` and `aliases`),
tolerating small OCR errors; without one, players are recorded under their GSPro names.

## Launch monitors

Shotcast records what GSPro plays, so it works with any launch monitor GSPro supports. What the
monitor changes is how much of the swing gets recorded:

| Recorded | With a launch monitor feed (`connector_log`, `gspro_sniffer`) | From GSPro's log only (`gspro_log`) |
|---|---|---|
| Ball speed, launch angle, start direction, spin, spin axis, carry | Yes | Yes |
| Club speed | If the monitor measures it | No |
| Club path, face, attack angle, strike location (kept in the shot's `raw` data; the booth talks about them) | If the monitor measures them (EYE XO2: with club markers) | No |
| Club | From the HUD either way | From the HUD |
| Player, hole, lie before and after, distance to the pin and where it finished, putts, strokes, penalties, scores, fairways, greens, scrambling | From GSPro's log and screen, the same for every monitor | Same |

The shot feeds:

- **`connector_log`** (default): tails `ConnectDebug.txt`, where GSPro's GSPconnect logs every
  message from the launch monitor. Built and tested with a **Uneekor EYE XO2**. Other monitors
  that connect through GSPconnect likely log there too, but that's untested. If the file isn't
  there at all, Shotcast uses `gspro_log` instead. If it's there but GSPconnect has gone quiet
  (it writes a heartbeat about every second while running, so 30 s of silence means it isn't
  the feed, e.g. a leftover file from an old setup or another connector in use), shots are taken
  from GSPro's log until GSPconnect speaks again. The booth log notes each switch.
- **`gspro_log`**: reads each shot from GSPro's own log, which records the ball data GSPro
  played (`LaunchExt Vars: sp 138.37, el 15.21, ...`) whatever sent it. Its numbers matched the
  Uneekor feed shot for shot, but it hasn't been run with other monitors yet. Use it if your
  monitor's shots don't show up with `connector_log`. Carry is read as yards and speed as mph
  (checked with GSPro set to yards; metric is untested).
- **`gspro_sniffer`**: captures the Open Connect traffic on GSPro's port 921, for third-party
  connectors. Gets whatever club data the connector sends. Needs Npcap with loopback support and
  an Administrator terminal.
- **`replay`**: an offline `.jsonl` of shots, for testing.

To check a feed, run `python -m commentator run --no-commentary`, hit a shot, and look for
`Connector shot:` or `GSPro log shot:` in `logs/booth-<date>.log`. Missing club data is stored
empty and the stats that need it skip those shots.

## Shot and round database

GSPro shows a scorecard and forgets it. The booth records every round into
`logs/season.db` (SQLite, set by `db_path`): one file, no server, kept across nights. Open it
in any SQLite viewer (DB Browser for SQLite, DBeaver, Datasette) or from Python, Excel or a
notebook to browse, chart or export.

What's recorded, and from where:

| Table | One row per | What's in it |
|---|---|---|
| `shots` | swing | Player, course, hole, shot number, club; launch monitor ball speed, launch angle, start direction, spin, spin axis, club speed, carry; lie before and after, slope, pin elevation, distance to the pin before and where it finished; GSPro's carry/total/offline; putt or full shot; a putt's result (holed, conceded, missed), or `holed` on a full shot that went in (a stroke the same player hits again on the same hole is never left `holed`: a putt becomes `missed`, a full shot loses its outcome); league or practice; `mulligan = 1` on a stroke GSPro took back. A conceded tap-in gets its own putt row (`outcome = 'gimme'`, no ball data), so every putt on the card is here |
| `rounds` | side per round (a player, or the team in a one-ball format) | Course, format, holes, gross, net to par, hole-by-hole scores and pars, fairways, greens in regulation, scrambling, sand saves, putts, penalties, longest drive (total distance); complete or quit early |
| `player_rounds` | player per round | The same per-player stats and scores, for team formats where the side is the team |
| `lines` | spoken line | Every line the booth said, tied to its shot |
| `predictions` | commentator call | The booth's make/miss putt calls and whether they were right |
| `sessions` | night | When the booth ran |

Scores, strokes, penalties, lies and distances come from GSPro's own log, so they're GSPro's
numbers, not OCR guesses; ball and club data come from the shot feed. Whether a stroke
is a putt (`is_putt`) is GSPro's own call, from whether the putter was selected, so a chip from
the green is a full shot and a putt from the fringe is a putt. The round's putt count, though,
follows the PGA Tour: every stroke played from the green counts, whatever the club (plus conceded
tap-ins), so a wedge chipped off the green is a putt and a putt from the fringe isn't. Scrambling
and sand saves follow the PGA Tour too: every green missed in regulation is a scramble chance,
saved by par or better; a greenside bunker is a sand-save chance, saved by holing out in two
strokes or fewer counting the bunker shot, whatever the score. The lie a shot was played from
(`lie_before`) is GSPro's too: the first stroke on a hole is `tee`, and GSPro logs the surface
when a shot is struck from rough or sand; the next shot's lie then corrects where the previous
one came to rest (`lie_after`).

Mulligans and pick-ups follow GSPro's scoring. A mulligan's stroke stays in `shots`, flagged
`mulligan = 1`, and is left out of every score and stat (the re-hit takes its shot number); its
club and ball data are still a real swing. With commentary on, the booth calls the mulligan
when the player sets up to replay the stroke. Mulligans count in every format where each player
has their own ball (best ball included); in scramble and alternate shot only GSPro's explicit
take-back is used. A hole picked up is scored at GSPro's maximum, and
those capped strokes are not counted as putts or penalties. Driving-range
shots are not recorded, only rounds. Practice rounds are recorded and flagged
(`is_league = 0`), so stats can be split league vs practice. On an alternate-shot round the log
can't say which partner hit, so those shots are kept (`attributed = 0`) but left out of
per-player stats. Replays and evals use an in-memory DB and never touch the file.

Back the file up (`logs/season.db`); it's the only copy of your rounds.

## Stats web page

`python -m commentator stats-web` opens a local page (http://127.0.0.1:8765)
for browsing the database. Its tabs:

- **Players**: the season table, plus per player strokes gained per nine (off the tee, approach,
  around the green, putting), night-by-night trends, club gapping with each club's typical flight
  (from full swings only, so chips don't shrink a wedge), approach by club, putting by length, and
  recovery from rough and sand.
- **Head-to-head**, **Rounds** (hole-by-hole cards, and a link to every shot of the round), **Courses** (each hole's scoring average,
  hardest and easiest, best nine).
- **Shots**: every shot, filterable by player, course, club, lie and a date range (or one round,
  from the Rounds tab), with its launch data and what the booth said about it. A shot's round is
  the round that ended next after it on its course, so a round the booth restarted during keeps
  all its shots; shots from a round that never ended (or was quit before a hole was done) have none.
- **Broadcasts**: a night's full transcript, in order.
- **Highlights**: records, aces, eagles, hole-outs, birdie streaks, bounce-backs, blow-ups, and
  top-five drives, approaches, chips and putts. An approach is a full shot from 51-250 yd that isn't
  a par-4/5 tee shot (a par-3 tee shot counts); a chip is from 50 yd and in, off the green.
- **Booth**: lines and words per commentator, signature phrases, and putt-call accuracy by night.

The **player picker** at the top (Everyone, or one name) narrows every tab to that player:
their row on Players, them on the left of Head-to-head, only rounds they played in (team rounds
too), their own hole-by-hole averages on Courses next to everyone's, only their shots, only nights
they were on air, and their own records and highlights. The browser remembers the pick. The
address bar always holds the view (tab, player, league/practice, and the Shots filters, e.g.
`#shots?player=Ann&club=DR`), so **Copy link** gives a link that opens exactly that. A player can
bookmark `#rounds?player=Their+Name`. Rounds that ended since this browser's last visit are tagged
**new**, with a count in the header. On a phone, the tabs wrap and wide tables scroll sideways.

Strokes gained is measured against an approximate PGA Tour baseline (Broadie-style expected
strokes by lie and distance), so negative numbers are normal; it's for comparing players and
nights. Click a column header to sort a table. It filters league, practice or all rounds (the filter and the league/practice tags only show once the database has a league round). It's the same static page that
[gets published](#sharing-the-stats-page): the local server opens the database read-only, builds
the page's files into `logs/stats-web-site` (again whenever the database changes) and serves them. It
follows the booth's own stat rules: mulligans are left out, and scoring, GIR, scrambling, sand and putting
come from individual-ball rounds. To look at
the season on another machine, copy the booth PC's `logs/season.db` into this checkout's `logs/`
(or pass `--db PATH`). `logs/` and `*.db` files are gitignored, so the copy is never committed.

Rounds quit early are stored too, marked incomplete with the holes played. Their holes count
toward per-hole and per-shot stats (birdies, GIR, scrambling); in round totals (rounds played,
average and best net), only those with nine or more holes finished count. Net scores are
compared per nine holes, so a 9 and an 18 line up; the commentary scales them to the round
being played.

The booth uses it too: season bests and streaks in the commentary, the per-lie carry factors
(rebuilt at startup), and `python -m commentator stats` (a season leaderboard). Some starting
queries:

```sql
-- Average drive and carry by player
SELECT player, COUNT(*) AS drives, ROUND(AVG(total_yards)) AS avg_total, ROUND(AVG(carry_game_yards)) AS avg_carry
FROM shots WHERE lie_before = 'tee' AND club = 'DR' AND NOT mulligan GROUP BY player;

-- Every round's score, newest first
SELECT date, course, side, holes_played, gross, net_to_par, putts FROM rounds ORDER BY ended_at DESC;

-- One player's carry by club (club gapping)
SELECT club, COUNT(*) AS shots, ROUND(AVG(carry_game_yards)) AS avg_carry, ROUND(AVG(ball_speed)) AS ball_speed
FROM shots WHERE player = 'Toby Hale' AND NOT is_putt AND club IS NOT NULL
GROUP BY club ORDER BY avg_carry DESC;
```

## Sharing the stats page

The stats page can go online for the league behind one shared username and password, updated on
its own after every round. It's a static site in a private S3 bucket, served by CloudFront; a
CloudFront Function checks the password before anything is served, and the booth PC uploads with
keys that can only write to that bucket. For a league it costs pennies a month or nothing (S3
uploads are free to send in, and CloudFront's free tier is 1 TB out a month).

What gets published (`python -m commentator publish --build-only` builds it into `logs/stats-site`
to look at):

- `index.html` and the browser SQLite library ([sql.js-httpvfs](https://github.com/phiresky/sql.js-httpvfs),
  vendored in `commentator/stats_vendor`).
- `v/<version>/summary-{all,league,practice}.json`: the season tables, strokes gained, cards,
  highlights and booth stats, computed on the PC.
- `v/<version>/player/<all|league|practice>/<key>.json`: one player's courses, highlights and
  records, fetched when they're picked (a few KB each).
- `v/<version>/stats.db`: the shots and broadcast transcripts. The Shots and Broadcasts tabs query it
  in the browser, which reads only the parts of the file a query needs, so a season of 90,000 shots
  (a 90 MB file) still opens the Shots tab with about half a megabyte.
- `latest.json`, uploaded last: which version the page reads. The two newest versions are kept, so
  a page that's open during a publish keeps working; one left open longer moves to the newest
  version the next time it looks up shots or a broadcast (refresh for the newest season tables).

It holds every player's shots, scores and the booth's lines, and nothing else from the PC (no
config, keys or logs).

### Setting it up

You need an AWS account and the [AWS CLI](https://aws.amazon.com/cli/) signed in as an admin, on
any computer. The template, [`tools/aws/stats-site.yaml`](../tools/aws/stats-site.yaml), makes the
bucket, the CloudFront distribution, the password check and an upload-only IAM user.

1. Create the stack (pick the password you'll give the league; 8+ characters, letters, digits and
   `._~!@#%^*+=-`):

   ```sh
   aws cloudformation deploy --stack-name shotcast-stats --template-file tools/aws/stats-site.yaml \
     --capabilities CAPABILITY_IAM --parameter-overrides SiteUser=league SitePassword='...'
   aws cloudformation describe-stacks --stack-name shotcast-stats --query "Stacks[0].Outputs"
   ```

   The outputs are the page's URL, the bucket, the region and the upload user. CloudFront takes a
   few minutes to come up.
2. Make the upload user's keys and put them in the booth PC's `.env`:

   ```sh
   aws iam create-access-key --user-name <UploadUser>
   ```

   ```
   AWS_ACCESS_KEY_ID=...
   AWS_SECRET_ACCESS_KEY=...
   ```
3. On the booth PC, install the uploader (`.venv\Scripts\python.exe -m pip install -e ".[publish]"`)
   and turn it on in `config.yaml`:

   ```yaml
   publish:
     enabled: true
     bucket: <BucketName>
     region: <Region>
   ```
4. Publish once by hand to check it: `.venv\Scripts\python.exe -m commentator publish`. Open the
   URL, sign in, and click into the Shots and Broadcasts tabs.

From then on the booth publishes after each round as soon as the sign-off has aired (if it never
finishes, `publish.delay_seconds`, 3 minutes, after the round ends instead), in its own process so
it never holds up the next round. A failed upload
is in the log and, with health checks on, raised as a health warning (a phone push too, if
[phone pushes](setup.md#phone-pushes) are set up). Replays never publish.

To change the password, run the `deploy` command again with the new one. To take it all down, empty
the bucket (`aws s3 rm s3://<BucketName> --recursive`), delete the upload user's access key, and
`aws cloudformation delete-stack --stack-name shotcast-stats`.

## Recording only

To keep the database without the booth talking, set
`commentary.enabled: false` or run `python -m commentator run --no-commentary`. Shots and rounds
are read from GSPro and recorded exactly as with commentary on, but nothing calls a model or a
voice, so no API keys are needed. The league file is optional too: without one, players are
recorded under their GSPro names and every round is practice.
