[← README](../README.md)

# Setup, operation and development

## Setup (fresh machine)

`install.bat` does steps 1, 3, 5 and 7 for you (see the [README](../README.md#quick-start)). By
hand:

1. **Python 3.12 or 3.13.**
2. For the `gspro_sniffer` source only: **Npcap** with "Support loopback traffic" ticked
   (https://npcap.com). Not needed for the default `connector_log` source.
3. Clone and install:
   ```
   git clone https://github.com/jjmerri/gspro-shotcast.git
   cd gspro-shotcast
   py -3.13 -m venv .venv
   .venv\Scripts\activate
   pip install -e ".[capture,cloud]"      # add ".[local]" for Kokoro voices (Ollama needs nothing extra)
   ```
4. For local voices only, download Kokoro files into `models/`:
   - https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx
   - https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin
5. `copy config.example.yaml config.yaml` and edit it.
6. **Your league (optional).** Without a league file the booth still calls every round, by the
   names on GSPro's setup screen, as practice (no points or standings talk). To add league
   features, `copy data\league.example.json data\league.json`, replace the example teams,
   players, schedule and storylines with your own, and set `league_file: data/league.json` in
   `config.yaml`. Only `teams[].players[].name` is required; everything else is free-form
   context the commentators use. Give each player a `gspro_name` (and `aliases`) so the booth can
   match the name GSPro shows to your roster.
7. `copy .env.example .env` and fill in the API keys you use. `.env` is never committed. The
   default needs OpenAI and Anthropic keys; with only an Anthropic key, use the "one key" blocks in
   `config.yaml` and Kokoro voices (steps 3-4). [Choosing a setup](booth.md#choosing-a-setup)
   compares the options.

Your `config.yaml`, `.env`, `data/league.json` and `logs/` are gitignored, so `git pull` (or
`update.bat`) updates the code without touching your league or settings. See [DEPLOY.md](../DEPLOY.md) for updating
and running on the sim PC (`start-booth.bat`, `screen-check.bat`).

## Bring-up, one step at a time

```
python -m commentator check                     # league loads, models answer
python -m commentator say "Testing, one two."   # voices work
python -m commentator run --replay data/sample_shots.jsonl   # whole pipeline, fake shots
```

Then check the screen read with `screen-check` (below; `screen.ocr` is on by default), and go live
with `python -m commentator run`.

## Checking the screen read

`python -m commentator screen-check` reads whatever GSPro screen is up (a hole, the scorecard, or
the setup screen's Players, Match Settings or Holes tab) and lists what the booth got from it:

```
Screen 1920x1080: a hole (the HUD)
  OK   player                 Alex E Shot 1 391.5 YDS 27.1YDS
  OK   par / length           par 4, 392 yards
  FIX  lie                    (nothing read)  -> with moved boxes: tee
  --   data tiles             (nothing read)  (blank until a shot is hit on this hole)
```

It also finds each panel by the labels GSPro draws on it ("PAR 4", "CARRY (game)", "CLUB",
"MATCH SETTINGS", ...) and works out where the boxes belong on your screen. A `FIX` line is a
read that comes back once the boxes are moved there; `screen-check --fix` writes those boxes to
`screen.regions` in `config.yaml` (the old file is kept as `config.yaml.bak`). Run it on a hole
and on each setup tab, since each screen places its own boxes. `logs/screen-check.png` shows the
boxes: green read, red are off their panel, yellow is where `--fix` puts them.

Use `--delay 10` to switch to GSPro before the grab, and `--image <png>` to check a saved
screenshot. `--bundle` saves the boxes' crops and the report under `logs/screen-check/` to attach
to an issue; they show player names, so look them over first.

## Troubleshooting

- **HUD misreads (wrong lie, missing tiles, wrong player).** The built-in OCR boxes fit GSPro's
  default HUD and adjust to the screen's shape (checked on 16:10 and 16:9). Run `screen-check`
  on a hole, and `screen-check --fix` if it offers to move boxes. To place one by hand, run
  `calibrate` and open `logs/calibrate.png` (a pixel grid with each box drawn), then set it under
  `screen.regions` in `config.yaml` (fractions of the screen; only list the ones you change) and
  confirm with `ocr-test`. A region you list is used exactly as written.
- **Players not picked up from the setup screen.** Those boxes adjust to the screen's shape too
  (checked on 16:10 and 16:9). Run `screen-check` with the Players tab up (and the Match
  Settings and Holes tabs), or `calibrate --out setup` to place the `setup_*` regions by hand.
- **Nothing reads at all, or the ON AIR badge doesn't show.** GSPro must be windowed or
  borderless, not exclusive fullscreen, and on the primary monitor (`screen.monitor`). A window
  over the HUD (the booth's own console, a notification, the taskbar) is read as part of it.
- **Silence, or errors in the log about the model or voice.** Check the keys in `.env`
  (`check` tries the text models, `say` the voices). A bad key or an outage makes the booth go
  quiet, but it keeps recording shots.
- **No shots at all.** The default source follows GSPconnect's `ConnectDebug.txt`; check its path
  (`source.connector_log` in `config.yaml`). If your monitor's shots never show up there, set
  `source.type: gspro_log` (any monitor, ball data only) or `gspro_sniffer` (Npcap, run as
  Administrator). See [Launch monitors](tracking.md#launch-monitors).

## Health checks

The booth reads GSPro's log lines and its HUD, so a GSPro update, a new monitor
or a re-picked data tile can quietly break it. Checks run at startup and at every round start: the
files it follows exist, and GSPro's version and the screen size match what it last ran against.
After every round (quit rounds included) it checks the round's reads:
- GSPro's log logged the shots, with every line kind the booth uses and no reworded lines.
- The HUD's shot number, distance to the pin and lie were read at most setups.
- The required data tiles (`health.required_tiles`) were read on most full shots.

A tile that never reads, or a log that logs no shots, is flagged mid-round, without waiting for the
round to end. Anything off puts a **"!" chip** beside the ON AIR badge: amber for a warning, red for
a failure. Hover it for the list; right-click it to dismiss it until something changes. A version or
screen change shows as a warning until a round passes every check. Each change is logged
(`HEALTH ...`) and saved to `logs/health.json`. Each round's report card goes to the booth log
(`Health checkup, round ...`) and to `logs/health-history.jsonl`.

## Phone pushes

Health issues can also buzz your phone through [ntfy](https://ntfy.sh) (free, no
account). Install the ntfy app, subscribe to a long, random topic name (anyone who knows it can read
the pushes), and put it in `.env` as `NTFY_TOPIC=...`. Restart the booth, then run `notify-test` to
check. Issues found together arrive as one push ("fail" ones at high priority), the same issue isn't
pushed again for 12 hours, and a "resolved" push follows once they clear (`notify:` in config.yaml).

## Commands

`python -m commentator <cmd>`, or `shotcast <cmd>` with the venv active:

| Command | What it does |
|---|---|
| `run` | Start the booth (live, or `--replay <file>`; `--no-commentary` records without talking) |
| `sniff` | Print raw GSPro Open Connect messages (needs Npcap + Administrator) |
| `screen-check` | Check what the booth reads from the GSPro screen that's up; `--fix` moves boxes that are off their panels |
| `calibrate` | Save a screenshot with the OCR regions drawn on it |
| `ocr-test` | Read the HUD once and print what was parsed |
| `fixture check` / `fixture save <name>` | Compare GSPro's log and screen to the regression fixtures, or save new ones |
| `notify-test` | Send a test health push to your phone (needs `NTFY_TOPIC` in `.env`) |
| `say "<line>"` | Speak a test line in each commentator's voice (`--speaker` for one) |
| `banter` | Fetch a live golf headline and play the booth's aside about it (as written at round start) |
| `check` | Verify the league file loads and every configured model responds |
| `eval` | Measure invented claims over repeated replays |
| `review <date>` | A session's log as a timeline plus a per-shot summary |
| `stats` | Season leaderboard from the shot database |
| `stats-web` | Browse the season database in a local web page (`--db`, `--port`, `--no-browser`) |
| `publish` | Build the stats page and upload it to S3 ([Sharing the stats page](tracking.md#sharing-the-stats-page); `--build-only` just builds it) |
| `backup` | Back up the season database now (`--force` even if unchanged; [Backups](tracking.md#backups)) |
| `restore <file>` | Put a backup back as the season database (stop the booth first) |
| `import-league` | Build the league file and history from the yearly spreadsheets |
| `cost` | Text-model calls and estimated cost per night (voice cost is in the booth log) |
| `squabbit` | Read this league's data from Squabbit (needs `.env` credentials) |

## Tests

```
pip install -e ".[cloud,dev]"
pytest
```

They run in parallel across the CPU's cores (pytest-xdist); `pytest -n0` runs them serially, which
starts quicker for a single test. GitHub Actions runs them on every push (Linux on Python 3.12 and
3.13, and Windows). Tests that need the third-party GSPro screenshots (see `data/screens/SOURCES.md`)
or the OCR engine skip when those aren't present. About 400 tests cover the engine, HUD parsing, rounds and handicaps, the GSPro
log reader, the season database and stats page, the OCR cache, and model switching. They do not
cover real GSPro/Uneekor traffic or live screen layouts; use the bring-up steps for those.

`tools/` holds developer harnesses, none needed to run the booth:

| Script | What it does |
|---|---|
| `simulate.py` | Drives the whole engine with scripted shots and HUD frames and reports the timeline and any rule it broke (`--real` uses a real model) |
| `round_demo.py` | Runs made-up rounds (opener, a few shots, wrap-up) through your configured models and prints what the booth says; costs a few cents |
| `db_replay.py` | Replays a real round from a copy of the season DB (shots, putt results, finishing distances) through your configured models and writes the transcript, to compare prompt changes on the same golf; costs a few cents a run |
| `replay_connector.py` | Replays a GSPconnect message stream (writing and rotating `ConnectDebug.txt` the way GSPconnect does) through the shot source, and checks every shot is detected once |
| `fake_gspro.py` | A stand-in GSPro Open Connect server, for testing the sniffer source |
| `capture_screen.py` | Grabs a full screenshot with a pixel grid, for placing OCR regions on GSPro's setup and course-load screens |
| `list_windows.py` | Lists window titles (Windows), to find GSPro's exact `screen.window_title` |
| `ocr_bench.py` | Times the OCR engine at different thread counts, to pick `screen.ocr_threads` |

## Regression fixtures

`tests/fixtures/` holds real, redacted GSPro rounds and screens the booth
is known to read right:
- logs: only the parser's lines, with user paths and player names replaced;
- screens: the HUD regions the booth reads (player box, hole box, data tiles, lie, club).

The tests re-read them. After a GSPro update, run `fixture check`: it compares GSPro's last round
and the latest frame against the fixtures, and lists any line kind, HUD field or tile that no
longer reads. The booth saves `logs/fixture-frame.png` on each round's first full shot, and
`logs/fixture-frame-<kind>.png` when it reads the scorecard or the setup screen's PLAYERS or HOLES
tab. Once a new version is confirmed, `fixture save <name>` turns those frames and the last round
into new fixtures (`--kind` picks one screen).
Look the files over before committing them.
