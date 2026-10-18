# Architecture

A map of the codebase for someone (or an agent) picking it up. Read this with
[How a shot sounds](docs/booth.md#how-a-shot-sounds) open; this adds the module layout and the invariants that are easy
to break.

## The shape of it

Three input feeds get reconciled into one live game state, the engine turns each event into
facts, a model phrases the facts, a checker guards the phrasing, and TTS speaks it. All the
counting and arithmetic happen in code: the model is handed statements ("hit 3 of 6 fairways",
"carries 268 to a pin 4 down"), never raw data to tally or interpret, because that is where
small models invent things.

```
sources/  ─►  engine.py  ─►  facts.py  ─►  prompts.py ─► LLM ─► grounding.py ─► speech.py ─► TTS
(3 feeds)     (orchestrate   (statements   (compose)            (reject bad     (queue +
              + project)      from state)                        lines)          play)
                   │
                   └─► store.py (season.db), round.py (scorecard/format/handicap), usage.py (cost)
```

## Modules

### Core pipeline
- **`__main__.py`** CLI entry and subcommands (`run`, `sniff`, `calibrate`, `ocr-test`, `say`,
  `banter`, `check`, `eval`, `review`, `stats`, `stats-web`, `import-league`, `cost`, `squabbit`,
  `fixture`, `notify-test`).
- **`engine.py`** the orchestrator, and by far the largest file. Owns the live loop: the
  three-way pre-shot split, the impact carry/offline projection, follow-to-rest, reactions,
  water handling, the round lifecycle (opener/closer), and speech scheduling. Most behavior
  questions start here.
- **`config.py`** the Pydantic config schema and loader. The source of truth for every setting
  and its default (including the GSPro/connector log paths and course dirs).
- **`facts.py`** turns launch data and state into plain statements so the model never interprets
  numbers or counts.
- **`prompts.py`** the system prompts (league and practice variants), the line and reaction
  pickers, and the result-outcome vocabulary.
- **`flight_pick.py`** chooses which pre-written branch to fire: the flight line for the
  trajectory, and the resting-lie reaction per material (fairway, green, semi/regular/deep
  rough, sand, pine straw, cart path, structure, water).
- **`minimap.py`** the optional Jev hazard read (`minimap.provider: jev`). Jev boxes the ball and
  pin, code splits the minimap along the line between them, and Jev says what each half holds
  (open water). Red penalty and white out-of-bounds lines are measured by code instead: each
  long red or white run is placed against the ball-to-pin line, its closest approach turned into
  yards by the map's scale, and its hazard side found (beyond a white line; on a red line's
  side with less grass, so a cliff edge that crosses the line of play is called on the right
  side). Lines more than 60 yards off aren't part of the hole, and the ground past an OB line
  is greyed before Jev looks for water. Jev also
  reads the yardage numbers as digit choices. Bunkers carry no number, so code finds
  sand-coloured patches near the line, Jev confirms each is a bunker cut into grass (not a beach,
  shoreline or roof), and the map's scale (ball-to-pin pixels over the yards to the pin) gives their
  distance; overlapping pieces of one bunker merge. Trees the same way (textured canopies near
  the line, Jev confirms; a lone tree gets one distance, a stand a range), plus a stymie when the line to the pin runs through one within 60 yards.
  On an approach Jev says which side of the green the pin is on (front/back comes from GSPro's
  front and back numbers); from the tee a dogleg shows in GSPro's aim dot (found as a pale disc,
  confirmed by Jev on a close-up) sitting 10°+ off the ball-to-pin line, or failing that in Jev
  saying the fairway bows off that line. On a long tee shot whose map stops short of the green, the
  only dot is the aim point: Jev reads its number on a close-up, and when that's well short of the
  to-pin, sides and distances are measured to it and the pin and dogleg reads are skipped. Only answers above `threshold` become facts and
  hazards; a late or failed read falls back to the `preshot` vision call. Each side's main hazard
  (`FlightPlan.named`: water, then a bunker where the shot likely lands, then out of bounds) may be
  named by that side's trouble flight line, which `pick_line` plays only when that hazard is in play
  (for a measured line, only when the miss reaches the line where the ball comes down).
- **`putt_read.py`** the optional putt-break read (`putt_read.enabled`). GSPro's green grid has
  white dots creeping slowly downhill along its lines; a still frame says nothing, so the booth
  grabs ~20 frames of the strip between the ball and the hole a few hundredths of a second apart,
  matches each dot to itself in the next frame (mutual nearest neighbour; a dot wrapping back across
  its cell is too far to match), and averages the drift. Sideways drift on the cross lines is the
  break, read in thirds up the screen so a double breaker shows; drift toward or away from the golfer
  is uphill or downhill. Gentle drift can't be told from a flat putt, so it says nothing. Started at
  setup when the distance is in feet; bounded by `putt_read.timeout_seconds`. The HUD's measured
  rise or fall (from 3 inches, spoken in inches and feet) replaces the grid's uphill/downhill.
- **`grounding.py`** the checker. Rejects any line whose numbers are not in the facts, that
  claims where an airborne ball lands, or that names a course feature without a screenshot.
- **`interest.py`** scores how notable a shot is and whether to comment at all.

### Shot and game state
- **`models.py`** the shot and event types: `Shot` (ball/club data, `is_putt`), `ShotEvent`,
  `PreShotEvent`, `HoleChangeEvent`, `RoundStartEvent`, `ScorecardEvent`, `RoundEndEvent`.
- **`hud.py`** parses OCR of the GSPro 3.1.x HUD (player box, hole box, tile panels, minimap lie
  and slope, club box) into structured fields.
- **`screen.py`** screen capture and OCR (RapidOCR/ONNX, CPU). Region reads, and the per-slot
  hash cache (`read_region_lines_cached`) that re-OCRs only changed regions. Regions are fractions
  of the screen; the HUD defaults carry the aspect they were measured at (16:10) and the edge their
  panel is pinned to, and `Region.to_pixels` rescales them for other screen shapes, because GSPro
  sizes its HUD to the screen width. The ON AIR badge (`overlay.badge_geometry`) does the same.
- **`carry_model.py`** learns how far GSPro actually lets a shot carry relative to the launch
  monitor: a course baseline and a per-lie penalty, learned from your own shots.
- **`course_card.py`** reads a GSPro course's static card (par, stroke index, yardages,
  altitude) from its `.unity3d.csv` / `coursedetails.txt`.
- **`round.py`** a league round: who is in it, the format, handicaps, hole scores, and the
  derived numbers (net, fairways, GIR, putts, penalties). Opener/closer trigger logic.
- **`store.py`** the SQLite season history (`season.db`): records every shot and line, serves
  season bests, rebuilds the per-lie carry factors at startup, backs `stats`. In-memory for
  replays/evals so fake shots never persist.
- **`stats_web.py`** / **`stats_web.html`** / **`season_analytics.py`** the `stats-web` viewer: a
  localhost-only server that opens `season.db` read-only (its own connection;
  `Store` would insert a session row) and a single-page UI. `season_analytics.py` holds the
  heavier season reads: strokes gained, gapping, recovery, trends, courses, highlights, booth
  stats, the shot log and broadcast transcripts. The booth itself doesn't import them.
- **`stats_site.py`** builds the stats page as a static site (per-context summary JSON plus a small
  `stats.db` of shots and transcripts that the page queries in the browser with the vendored
  sql.js-httpvfs over HTTP range requests) and uploads it to S3 (`publish`; the booth runs it in a
  subprocess after each round). `stats-web` serves the same site locally.
  `tools/aws/stats-site.yaml` is the S3 + CloudFront + password stack.
- **`state.py`** small in-session state (`ShotRecord`, `SessionState`).
- **`league.py`** the league file model (teams, players, `gspro_name`/`aliases`, handicaps,
  schedule, storylines) and the course-name matcher.
- **`history.py`** earlier-season standings as facts, built by `import-league`.
- **`league_import.py`** builds `league.json` and history from the yearly spreadsheets.

### External data
- **`squabbit.py`** reads live handicaps and leaderboards from Squabbit at round start (read-only, best-effort),
  and the season's week cards, rounds and leaderboards for `squabbit --cards`/`--validate`.
- **`handicap.py`** Squabbit's handicap rules (course handicap, allowances, caps, stroke
  allocation), the week card built from a Squabbit tournament, and the season validation that
  recomputes leaderboard nets. `round.py` uses it, with the week card when one is saved.
- **`board.py`** the week's posted results and the season standings from Squabbit's leaderboards
  (places from the nets, tied places splitting their points), read with the handicaps at round
  start. The engine's week facts: the week so far, standings, the closer's projected place, the
  net stakes late in a round, and the week's closest to the pin (`Store.week_ctp`).
- **`news.py`** fetches real golf headlines (RSS/Atom) for between-shots banter, because the
  model's own news is stale. Teasers with no facts in them are skipped. At round start the engine
  has the closer's model write and voice `news.per_nine` two-line asides per nine holes; each is
  held for a quiet shot, or for the kind of shot its story is about (a long drive, trouble), with a
  bridging first line for that moment. At most one every three holes, never on the first or last two.

### Output
- **`speech.py`** the speech queue: ordering, backlog limits, barge-in (cutting off a line in
  progress), and audio playback. On a cloud voice it voices queued lines in parallel and plays
  them in order, hedges a slow request with a second one, and drops a line that misses its
  deadline (and the rest of its exchange) instead of playing it late.
- **`overlay.py`** the clickable ON AIR / OFF AIR badge over GSPro: a Tk window on its own
  thread, never focused, re-asserted topmost every second. A click calls `Engine.set_commentary`
  on the booth's loop (off air mutes and cuts speech and skips model calls; recording carries on).
  Starting it makes the whole booth process per-monitor DPI-aware, so under Windows display scaling
  everything (badge placement, screen capture) works in real pixels rather than scaled ones.
- **`usage.py`** per-call token logging to `usage.jsonl`, the cost rollups, and the per-session voice (audio) cost line.
- **`perf.py`** process-priority / core-cap helpers so OCR bursts do not starve GSPro.
- **`winshot.py`** occlusion-proof window capture (PrintWindow) for `screen.capture: window`, when a
  window sits over GSPro. Off by default; the default is a plain monitor grab.
- **`dotenv.py`** a tiny `.env` loader (no dependency).

### `llm/` and `tts/` (one file per provider, swapped by config)
- `llm/`: `base.py`, `ollama_client.py`, `anthropic_client.py`, `openai_client.py`.
- `tts/`: `base.py`, `console_tts.py`, `kokoro_tts.py`, `openai_tts.py`.

### `sources/` (the three feeds, see [docs/tracking.md](docs/tracking.md#where-the-data-comes-from))
- `base.py` the `ShotSource` ABC. `__init__.py` picks the source from `source.type`.
- `connector_log.py` tails GSPconnect's `ConnectDebug.txt` for the shot feed (the default).
- `gspro_log_shots.py` the shot feed from GSPro's own log (`LaunchExt Vars` lines): any launch
  monitor, ball data only. `connector_log` falls back to it when `ConnectDebug.txt` is missing.
- `gspro_sniffer.py` sniffs GSPro Open Connect loopback (scapy, needs Npcap + admin).
- `gspro_log.py` tails GSPro's `output_log.txt` into a live `GsproState` game-truth model.
- `replay.py` replays a `.jsonl` shot feed offline. `stream.py` is shared async plumbing.

## Invariants that are easy to break

These were each the cause of a real bug. Preserve them.

- **Follow-to-rest reads the resting lie, not the landing lie.** `engine._follow_shot` polls
  only the `tiles` and `lie` regions (not the slow full-HUD read) until the ball stops. "Moved
  on to the next player" is detected when the carry and total tiles **both** parse to `None`
  (the next player's `--.--` address), not when a tile is merely blank: `--.--` is a kept tile
  value and never reads as empty. The last reading that still belonged to this shot is what the
  reaction uses, so the next player's lie is never credited to this shot.
- **`impact_snap` is a 7-tuple.** Index 6 is the per-player shot number captured at impact. Any
  code that unpacks `impact_snap` must unpack all 7 (this caused a crash when one reader still
  expected 6). The putt `base_shot` comes from `impact_snap[6]`, not a live read.
- **Unknown putt outcomes are not graded.** `_wait_putt_result` returns `"unknown"` on timeout
  (vs `"missed"` on a real stroke advance). An `"unknown"` outcome writes no result and grades
  no prediction, and its reaction is suppressed, so a holed putt that timed out does not get a
  "missed" reaction.
- **OCR is genuinely slow; do not add concurrency to beat it.** A full HUD read is ~2.6-2.8s,
  dominated by the player-box region (~1.4s). More OCR threads oversubscribe the CPU and make it
  slower, and starve GSPro. This is why follow polls a narrow region set.
- **League vs practice gates the prompt.** The `system` prompt is league-only when a league
  round is actually in progress (`r is not None and not r.ended and r.is_league`), else practice.
  The closer runs when `r.ended` is true, so it passes its variant explicitly rather than
  reading the property. Practice must never claim points are on the line.
- **Water is learned from the log, fast.** `GsproState.wet(player, since=)` and
  `_shot_wet(player, since=impact)` must be queried with the impact timestamp, or a stale wet
  flag from the previous ball fires the splash a shot late.
- **A log shot belongs to the score line after it, not the last select.** GSPro hands the clock
  over with "Next Player" and no new select block (and the round's first shot has none), so the
  player last selected is often not the one swinging. `GsproState` holds each "Shot Fired", and
  any "Hole out" or splash during it, unowned until the shooter's own score line credits it.
  Crediting by the last select recorded partners' holed putts as misses and flagged the wrong
  swing as a mulligan.
- **A stroke played on isn't holed.** At the next tee GSPro's log can still show the last hole's
  hole-out, which once marked two tee shots `holed`. `Store` checks both ways (when the next
  stroke is recorded, and when a late `holed` result arrives): if the same player's next stroke
  is on the same hole with a higher stroke number, the putt becomes `missed` and a full shot
  loses its outcome. A mulligan's re-hit keeps the stroke number, so it doesn't count.
- **Every full shot gets a finishing distance.** The booth sets `finish_yards` from GSPro's log
  at the player's next shot. It used to skip that whenever the screen follow hadn't seen
  touchdown, which lost about half of them. As a safety net, `Store` also fills a missing
  finish from the same player's very next stroke on the hole (its `to_pin_yards`, or the gimme
  radius), and backfills old rows at startup. A penalty in between leaves the finish empty,
  because the next stroke was played from a drop. A full shot that holed out gets 0.
- **Replays and evals must not write the season DB.** They use an in-memory database. If you add
  a new persistence path, keep that separation or fake shots pollute `season.db`.

## Tests

`tests/` (400, run with `pytest`; CI in `.github/workflows/tests.yml`): `test_core.py` (engine,
HUD, follow, OCR cache),
`test_round.py` (rounds, formats, handicaps, setup watcher), `test_handicap.py` (Squabbit's
handicap rules and week cards), `test_board.py` (week results, standings, net and CTP facts), `test_gspro_log.py` (the log
reader), `test_model_switch.py`, `test_connector.py`, `test_replay.py`, `test_minimap.py`,
`test_putt_read.py`, `test_gspro_log_shots.py`. They cover logic, not
real GSPro/Uneekor traffic or live screen layouts. `tools/` holds the offline harnesses, an OCR
thread benchmark and screen-calibration helpers.
