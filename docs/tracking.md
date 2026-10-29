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
   GSPro's mirrored (lefty) HUD, with the minimap bottom-left and the club box bottom-right, is
   a per-player setting; each read checks which side the slope bar is on and swaps the lie, club,
   minimap and slope regions to match.

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
  from GSPro's log until GSPconnect speaks again. The booth log notes each switch. GSPconnect
  can also skip a single shot while it's running; a shot in GSPro's log that GSPconnect hasn't
  sent within two seconds (matched by ball speed) is taken from GSPro's log, without club data
  ("GSPconnect skipped a shot" in the booth log).
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
| `shots` | swing | Player, course, hole, shot number, club; launch monitor ball speed, launch angle, start direction, spin, spin axis, club speed, carry; club path, attack angle, face to target, dynamic loft and strike location on the face, when the monitor reported club data (NULL otherwise); lie before and after, slope, pin elevation, distance to the pin before and where it finished; GSPro's carry/total/offline; putt or full shot; a putt's result (holed, conceded, missed), or `holed` on a full shot that went in (a stroke the same player hits again on the same hole is never left `holed`: a putt becomes `missed`, a full shot loses its outcome); where GSPro had the ball before it (`start_x/y/z`, world metres, from the log's setup block; NULL on a tee shot, which has none) and the player's minimap aim click, if they moved it (`aim_map_x/y`); league or practice; `mulligan = 1` on a stroke GSPro took back; `left_out = 1` on a stroke left out of the stats (see below). `handed`: the player's hand, `RH` or `LH`. `tree_hits`: how many times GSPro's log had the ball hit a tree in flight, foliage or a trunk (NULL for none); such a shot's carry is left out of the carry model. A conceded tap-in gets its own putt row (`outcome = 'gimme'`, no ball data), so every putt on the card is here |
| `hole_layouts` | hole of a completed round | Each played hole's tee, pin (as set that round) and GSPro's aim waypoint, in world metres, from the hole list GSPro logs when a round is complete; a round quit early logs none, so its holes are filled from the course's GKD file (see Rounds below) |
| `hole_images` | course and hole | GSPro's minimap off the tee (JPEG, title bar trimmed), with where the ball and pin sit on it: the background of every replay of the hole. Kept at the first tee setup on a hole that has none yet, on or off air, once the round has started (an opening tee is snapped as the round starts, under its course and start hole) |
| `course_tees` | course and tee | Each tee's course rating, slope and par from the course's `coursedetails.txt`, kept whenever a course loads; used for the handicap index |
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
has their own ball (best ball included) and in scramble, where a player's re-hit logs the same
score as their last stroke (a penalty on a ball the team didn't pick logs a new one, so isn't
mistaken for it); in alternate shot only GSPro's explicit take-back is used. The re-hit starts
where the stroke it replaced did. In a scramble, a tee ball whose rest the screen never read still
counts toward fairways hit: the team's second stroke is played from the drive it picked, so that
stroke's lie says where it finished. A hole picked up is scored at GSPro's maximum, and
those capped strokes are not counted as putts or penalties. Driving-range
shots are not recorded, only rounds. Practice rounds are recorded and flagged
(`is_league = 0`), so stats can be split league vs practice. On an alternate-shot round the log
can't say which partner hit, so those shots are kept (`attributed = 0`) but left out of
per-player stats. Replays and evals use an in-memory DB and never touch the file.

**Leaving a shot out.** A bad read or a practice swing can be left out of the stats: open the
shot on the Shots tab of the local stats page (the tray's Open stats) and click "Leave this shot
out of the stats". It sets `shots.left_out = 1`: the stroke stays on the card, in the hole's score
and in the replay, but drops out of every average, record, gapping table, highlight and strokes
gained. Click again to count it. The published page is read-only and shows the tag; the change goes
out with the next publish. Only the local page accepts the change (from this PC, JSON requests only).

Shots recorded before the booth stored positions can get them from a saved GSPro log:
`python -m commentator backfill-log <output_log.txt>... [--db logs/season.db] [--dry-run] [--fix-starts]`. The log
has no timestamps, so its shots are matched to the file's in order, on course, hole and distance to
the pin; a stretch of the log that mostly doesn't match (a round the booth never recorded) is
skipped, and only shots with no position yet are filled. `--fix-starts` also corrects positions
already stored that the log puts elsewhere: group rounds recorded before 10/24 have each shot starting
where the previous player's ball stopped (GSPro logs a ball's spot right after its shot, for the
player who hit it). GSPro starts `output_log.txt` over at each
launch, so only the sessions since GSPro was last started can be backfilled. Stop the booth first and
back the file up.

A hole replay plays the shots in turn (every player's nth shot together), each line drawing out
behind a ball, and ends on the full drawing; ↻ Play again replays it. Each player keeps one colour
for the whole round, and "Only <name>" hides the others' shots (the player picked at the top, else
the round row's player) until "Show everyone". A ball lost to a penalty area (water, OB) runs to
where GSPro logged it going in (`shots.hazard_*`) and ends in a red ✕; the drop is the next stroke's
start. GSPro logs that entry point even for a ball that rolls in with no splash line, but also, once,
for a ball that finished dry, so it is drawn only when a penalty stroke follows. `backfill-log` fills
it (and a scramble's picks) in for shots recorded before it was stored. It's drawn over GSPro's minimap
of the hole, turned and scaled so the map's ball sits on
the tee and its pin on the pin (the ball's dot is the colour of the player's team, whatever was
picked at setup; a map stored before this was found is fixed when the booth next opens the season).
In a scramble, every partner's shot is drawn from the team's ball, but only the one the team played
on has a line to where it finished. GSPro's log names that ball after every stroke ("Selecting shot
for player"), and the putt that holes is the team's too; the booth stores it as `shots.picked` (1 the
team's ball, 0 a ball lifted). A stroke without it, recorded before 10/26 or with no log, is worked
out from where the next stroke started. Under the map, each scramble partner shows the shots of
theirs the team used, plus the gimme (GSPro gives it to the partner whose ball was picked inside the
circle) and any penalty stroke their picked ball cost (a gap in the team's stroke numbers after it),
so the partners' counts add up to the hole's score. An open scramble round sums these per partner in
a "Shots used" line under the scorecard, noting holes with no shot data. The map is the same every round, so the booth keeps one per course and
hole, at the first tee setup on a hole that has none, and every round there uses it, older ones too
(marked as from another round, since its tee, pin dot and yardages may differ a little). To replace a
hole's map with the one on screen now: `python -m commentator snap-minimap --course "Kiawah Ocean Course" --hole 10
[--session N] [--image screenshot.png]` (run it on the sim PC's desktop session, not over SSH).

Each full swing's **shape** (draw, fade, pull hook, push slice...) is worked out from its start
direction and spin axis, and shown in the Shots tab and as the curve of its line in the replay.
Names follow the player's hand, which is stored with each shot (`shots.handed`, as GSPro has it set):
a lefty's ball curving right is a draw. A shot with no hand stored is named as a right-hander's.

### Handicap index

The booth keeps its own handicap index per player, by the World Handicap System: each round's
score differential is (113 / slope) × (adjusted gross − course rating), with each hole capped at
net double bogey (par + 5 until the player has an index); the index is the average of the best
differentials among the last 20 (best 1 of 3 up to best 8 of 20, with the WHS adjustments for
fewer than 6), to 0.1, capped at 54, once the player has 54 holes in. A round of 9 to 17 holes
counts as an 18 using the player's expected score for the holes not played (0.52 × index + 1.2 a
nine), or scaled up to 18 before there's an index. Scramble and
alternate-shot rounds and rounds under nine holes don't count (a player alone on a one-person
scramble team plays their own ball, so that's recorded as an ordinary individual round). The tee is the one whose yardages
match the round's logged tee-to-pin distances, else the one whose length fits the score; a course with no
ratings uses par and slope 113. Sim scoring runs low, so plus indexes are common.

When the league file has no handicap for a player, the booth uses this index for net scores and
commentary. A Squabbit handicap, when there is one, is used in the commentary and never stored.

Back the file up (`logs/season.db`); it's the only copy of your rounds.

## Stats web page

`python -m commentator stats-web` opens a local page (http://127.0.0.1:8765)
for browsing the database. Its tabs:

- **Players**: the season table, plus per player strokes gained per nine (off the tee, approach,
  around the green, putting), night-by-night trends, club gapping with each club's typical flight
  (from full swings only, so chips don't shrink a wedge), approach by club, putting by length, and
  recovery from rough and sand, and the handicap index with its trend.
- **Head-to-head**, **Rounds** (hole-by-hole cards with Out and In for each nine, and a link to every
  shot of the round; a practice round is listed under its players, a league round under its side; a hole
  number that's a link draws the hole from above, tee at the bottom: each player's shots from where
  GSPro had the ball, dashed lines to where they aimed, and the pin; when the round was quit before
  GSPro logged its tees and pins, they're taken from the course's GKD file (the pin day the shots'
  distances match, the tee box the tee shot's yardage matches), else the pin is estimated from those
  distances; a one-player round of nine
  holes or more gets a summary: score per nine, fairways, greens and putts against the player's
  season, strokes gained by category, best and worst shot, best and worst hole; **Share card**
  saves the card as an image, or opens the phone's share sheet), **Courses** (each hole's scoring average,
  hardest and easiest, best nine).
- **Shots**: every shot, filterable by player, course, club, lie and a date range (or one round,
  from the Rounds tab), with its launch data and what the booth said about it; each row has its own
  ▶ Watch and 🔗 share buttons. A shot's round is
  the round that ended next after it on its course, so a round the booth restarted during keeps
  all its shots; shots from a round that never ended (or was quit before a hole was done) have none.
- **Highlights**: records, aces, eagles, hole-outs, birdie streaks, bounce-backs, blow-ups, and
  top-five drives, approaches, chips and putts. An approach is a full shot from 51-250 yd that isn't
  a par-4/5 tee shot (a par-3 tee shot counts); a chip is from 50 yd and in, off the green. Pick the
  last 3, 6 or 9 months, the last year (the default) or all time, counted back from when the page was built.
- **Broadcasts**: a night's full transcript, in order.
- **Booth**: lines and words per commentator, signature phrases, and putt-call accuracy by night.

Broadcasts and Booth are hidden until the booth has said something (stats only, commentary off).
Three more tabs are only on the [published page](#sharing-the-stats-page), since the booth uploads
what they show straight to the site. A **Live** tab plays the sim's screen while a round is on, if the
[live stream](#live-stream) is set up (a red dot on the tab means it's on air; between rounds it
says off air). Turning `live.enabled` off takes the tab away when the booth next starts.
**Round replays** plays each stream of the last week from the start, with a link to where each
round in it begins (course and players); a stream still on air can be watched from its start too.
**Schedule** lists the sim's SuperSaaS bookings for the next two weeks (time, the booker's name
and the booking type), when [bookings](leagues.md) are on (`booking.schedule_days`, 0 for none);
the booth refreshes it every 30 minutes while it runs.

The **player picker** at the top (Everyone, or one name) narrows every tab to that player:
their row on Players, them on the left of Head-to-head, only rounds they played in (team rounds
too), their own hole-by-hole averages on Courses next to everyone's, only their shots, only nights
they were on air, and their own records and highlights. The browser remembers the pick. The
address bar always holds the view (tab, player, league/practice, the Shots filters and open shot,
the open round and hole, the course, the head-to-head pair, e.g. `#shots?player=Ann&club=DR`), so
**Share link** gives a link that opens exactly that. Each round, hole replay and shot (on Rounds,
Shots and Highlights) has its own share button (🔗, or **Share link**): a phone opens its share
sheet, a computer copies the link. A link to one shot (`#shots?shot=1234`) lists the round it was
in with that shot open; a link to a round (`#rounds?round=45&hole=3`) opens its scorecard and that
hole's replay. While a round's stream is still kept (a week), its rounds, holes and shots also get
**▶ Watch**, which opens **Round replays** about a second before that swing (the back button returns).
A player can bookmark `#rounds?player=Their+Name`. Rounds that ended since this browser's last visit are tagged
**new**, with a count in the header. On a phone, the tabs wrap and wide tables scroll sideways.

Strokes gained is measured against an approximate PGA Tour baseline (Broadie-style expected
strokes by lie and distance), so negative numbers are normal; it's for comparing players and
nights. Gimmes aren't assumed to be any size: each round's gimme circle is the radius GSPro logged
for most of its shots, and the usual one is the radius most league rounds were played with (each
round counted once). A round with a different circle is tagged on Rounds (e.g. **6 ft gimmes**),
putting by length starts at the usual circle, and in strokes gained a stroke that stops in a bigger
circle than usual is charged for the longer putt it was spared (holing out from 2/3 of the radius,
beyond the usual circle's), so a generous circle doesn't inflate it. Click a column header to sort a table. It filters league, practice or all rounds (the filter and the league/practice tags only show once the database has a league round). It's the same static page that
[gets published](#sharing-the-stats-page): the local server opens the database read-only, builds
the page's files into `logs/stats-web-site` (again whenever the database changes) and serves them. It
follows the booth's own stat rules: mulligans are left out, and scoring, GIR, scrambling, sand and putting
come from individual-ball rounds. To look at
the season on another machine, copy the booth PC's `logs/season.db` into this checkout's `logs/`
(or pass `--db PATH`). `logs/` and `*.db` files are gitignored, so the copy is never committed.

Rounds quit early are stored too, marked incomplete with the holes played. Their holes count
toward per-hole and per-shot stats (birdies, GIR, scrambling); in round totals (rounds played,
average and best net), only those with nine or more holes finished count. A quit that far in also
earns a sign-off, over the holes actually played; a shorter one ends without a closer. Net scores
are compared per nine holes, so a 9 and an 18 line up; the commentary scales them to the round
being played. A game shorter than nine holes (a one-hole 19th, a 3-hole round) is left out of round
totals and isn't measured against them, since per nine a birdie on one hole would read as -9. A
round's course is stored under the name GSPro gives it (the scorecard reads it in capitals).

Every generated line over its word cap is cut to its last full sentence, or dropped if it has
none; the booth log says which (`Cut a N-word line` / `Dropped a N-word line`), with the full text.

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

**Your own domain (optional).** Request a certificate for the domain and its `www` name in
`us-east-1` (CloudFront only uses certificates from there) and add its validation records at your
DNS host:

```sh
aws acm request-certificate --region us-east-1 --domain-name example.com \
  --subject-alternative-names www.example.com --validation-method DNS
aws acm describe-certificate --region us-east-1 --certificate-arn <arn> \
  --query "Certificate.DomainValidationOptions[].ResourceRecord"
```

Once it shows `ISSUED`, run the `deploy` command again with
`DomainName=example.com CertificateArn=<arn>`. Then point `example.com` and `www.example.com` with a
CNAME at the `DistributionDomain` output. On Cloudflare, set both records to DNS only (grey cloud),
not proxied. Every other address, `www` and the `cloudfront.net` one included, redirects to the
domain.

To change the password, run the `deploy` command again with the new one. To take it all down, empty
the bucket (`aws s3 rm s3://<BucketName> --recursive`), delete the upload user's access key, and
`aws cloudformation delete-stack --stack-name shotcast-stats`.

### Live stream

The published page's **Live** tab can show the GSPro screen and sound (the booth's voices too)
while a round is being played, behind the same password. OBS on the GSPro PC writes the stream as
[HLS](https://en.wikipedia.org/wiki/HTTP_Live_Streaming), a playlist and 4-second video files, into
the Shotcast folder; the booth uploads each file to the bucket's `live/` folder as it appears, and
the page plays it with
[hls.js](https://github.com/video-dev/hls.js) (vendored, like the SQLite library). Viewers are
about 20 seconds behind, and the player (live or a replay) starts muted, with a button to turn the
sound on. Any number of people can watch at once.
Each stream is kept for a week (`live.keep_days`) for the **Round replays** tab, with a second
playlist (`full.m3u8`) of every file from the start and `live/recordings.json` listing the streams
and where each round in them starts. A stream under a minute isn't kept. Streams past a week are
deleted as the next one starts or stops, or when the booth starts, and a lifecycle rule in the stack
deletes anything under `live/s/` after 8 days (stacks made before 0.1.16 had 1 day: update the stack,
or kept streams vanish after a day). A stream cut off by a crash is closed and listed when the booth
next starts. With `live.keep_days: 0` nothing is kept: each file goes from the bucket once it drops
off the playlist, and the rest when the stream stops. Either way the PC's `live\` folder is emptied
when a stream stops.

The booth starts OBS's output when a round starts and stops it 30 seconds after the sign-off
(`live.stop_after_seconds`), over OBS's WebSocket server; a round quit early stops it the same way,
30 seconds after its sign-off or after the quit if it's too short for one. The tray's
**Start/Stop the live stream** does it by hand. It also stops:

- **OFF AIR** on the badge stops it, and no round starts it until **ON AIR**, which starts it again
  mid-round.
- **Idle**: after 20 minutes with no shots (`live.idle_minutes`; 0 for never), a round left on a hole
  or a stream started by hand and forgotten. The round's next shot starts it again.
- **Quit** in the tray stops OBS's output and takes the page off air at once. A booth that crashed
  or was killed can't: while live it refreshes `live/status.json` every 30 seconds, and the page
  shows off air within about 2 minutes of that stopping, rather than a frozen player. A booth
  starting goes off air, and clears `live/` of everything but the week's kept streams.

With `live.with_rounds: false` none of this follows rounds or the badge (only the idle stop
applies): the stream is started and stopped by hand.

Use OBS's hardware encoder (NVENC on NVIDIA cards) so encoding stays off the CPU
GSPro needs.

Cost: S3 charges for each upload, three every 4 seconds, about a cent and a half an hour on air
(about $4 for a season of 144 rounds). A week kept is about 1.2 GB an hour streamed: two 18-hole
rounds a day is about 20 GB, about 50 cents a month. Watching (live or kept) is within CloudFront's
free 1 TB a month: at 2.5 Mbps one viewer-hour is about 1.3 GB, so about 800 viewer-hours a month,
counting every viewer; past it, about 11 cents a viewer-hour.

Setting it up on the GSPro PC (with the [stats page](#setting-it-up) already publishing): in the
tray, **Set up the live stream...** (once it's on, **More → Set up the live stream again
(OBS)...**). It downloads OBS Studio 33.0.0's portable build (224 MB, checked against its
published checksum) into the Shotcast folder's `obs\` (`%LOCALAPPDATA%\Shotcast\obs`),
apart from any other OBS, and sets it up:

- its WebSocket server on port 4455, with a password it puts in `.env` (`OBS_WEBSOCKET_PASSWORD`)
- a **Game Capture** of GSPro filling a canvas the screen's shape, up to 1920 wide (1920x1080 on a
  16:9 screen), streamed up to 1280 wide (1280x720 on 16:9), 30 FPS; **Desktop
  Audio** carries GSPro's sound and the booth's voices (a microphone, if there is one, is muted)
- its recording output as the stream: **Custom Output (FFmpeg)** to the Shotcast folder's
  `live\stream.m3u8`, container **hls** (`hls_time=4 hls_list_size=5 hls_flags=delete_segments+temp_file`),
  2500 Kbps with a keyframe every 4 seconds on **h264_nvenc** (an NVIDIA card; else libx264), aac
  at 128 Kbps
- `live.enabled: true` in `config.yaml`, then it restarts the booth

Then try **Start the live stream** in the tray, open the page's Live tab, and stop it. Running the
setup again keeps OBS's scenes and other settings. OBS has to be closed for it (right-click its
icon by the clock, **Exit**). If Windows Firewall asks whether OBS Studio may use public and
private networks, choose **Cancel**: the booth reaches OBS's WebSocket on the same PC, which the
firewall doesn't block, and the stream leaves as the booth's uploads.

With your own OBS instead, set the same up in it by hand, put its WebSocket password in `.env` and
point `live.obs_path` at its `obs64.exe` if it's not in `C:\Program Files\obs-studio`.

The booth starts OBS minimized to the tray when it starts, if it isn't running (`live.launch_obs`),
clearing OBS's crash flag first so it doesn't stop at its Safe Mode question after a power cut.
With a `publish.prefix`, the stream goes under `<prefix>/live/`: change the stack's lifecycle rule
to match.

## Backups

The season file is the only copy of every round, so the booth backs it up: when it starts and after
each round, but only if something changed since the last copy. Copies are gzipped SQLite files
(`season-YYYYMMDD-HHMMSS.db.gz`, about half the database's size; the minimap images don't compress), taken with SQLite's online
backup and integrity-checked before they're kept. A failure shows as a health warning on the ON AIR
badge.

It keeps the newest copy from each of the last 14 days that had one, then one a week for the 4
weeks before those. Days without golf take no copies, so a break doesn't age the history out.

```yaml
backup:
  enabled: true
  dir: null          # default logs/backups; better: a OneDrive/Dropbox folder, NAS or other drive
  daily: 14
  weekly: 4
  s3: false          # also upload to publish.bucket under s3_prefix
  s3_prefix: backups/ # must stay under backups/, the only prefix the site won't serve
```

By default the copies sit on the same drive as the database, which covers a corrupted or deleted
file but not a dead drive. Point `dir` at a synced folder or another drive to cover that too.

`s3: true` uploads each copy to the stats bucket under `backups/` (pruned the same way) with the
publish keys; a failed upload is retried at the next backup. Deploy the stats stack from this repo
first: its bucket policy keeps CloudFront from serving `backups/`, which would otherwise be readable
by anyone with the site password. The upload keys can't download, so restoring from S3 needs your
own AWS credentials (`aws s3 cp s3://<BucketName>/backups/<file> .`).

`python -m commentator backup` takes a copy now. To restore, stop the booth and run
`python -m commentator restore logs/backups/season-....db.gz`; it checks the copy, saves the
current file (even a corrupted one) as `pre-restore-*.db.gz` in the backup folder, then swaps it in.

## Recording only

To keep the database without the booth talking, set
`commentary.enabled: false` or run `python -m commentator run --no-commentary`. Shots and rounds
are read from GSPro and recorded exactly as with commentary on, but nothing calls a model or a
voice, so no API keys are needed. The league file is optional too: without one, players are
recorded under their GSPro names and every round is practice.

Off air (from the config or the ON AIR badge) only stops model and voice calls, and the screen reads
that only feed them (the paid Jev minimap read, putt breaks, headlines, weather, scores, trivia).
Shots, rounds, hole replays and their minimap images, publishing, backups and health checks carry on.

If commentary is on but its API keys are missing, the booth still starts and records, off air, with
a warning on the ON AIR badge saying which key is missing. With commentary off and no keys, the badge
isn't shown, since it couldn't go on air.
