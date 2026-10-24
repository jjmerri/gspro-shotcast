# Deploy and operate on the sim PC

The booth runs on the Windows PC that runs GSPro. You can develop on the same PC or on another
machine; either way, the sim PC should be a **git clone** of this repo so updates are a `git pull`.

Your own machine details (host, user, SSH key, paths) belong in `DEPLOY.local.md`, which is
gitignored. Keep personal notes there, not here.

## The Windows app

The installer from [Releases](https://github.com/jjmerri/gspro-shotcast/releases) is the simplest
setup for a sim PC that just plays: see the [README](README.md#the-windows-app-easiest). It puts a
private Python with Shotcast in `%LOCALAPPDATA%\Programs\Shotcast` and runs `shotcast tray`, which
keeps the booth running (restarting it after a crash, up to 3 times in 10 minutes), serves the
stats page on this PC and offers updates. Everything else (config.yaml, .env, the league file,
`logs\` with season.db, backups and the booth, tray and `booth-errors.log` logs) lives in
`%LOCALAPPDATA%\Shotcast`. The rest of this page is for a git clone.

Don't run both: if another booth is running (start-booth.bat or a scheduled task), the tray
doesn't start one and says so; **Restart the booth** in its menu stops the other and starts its own.

Releases are built by `.github/workflows/windows-app.yml` when a `v*` tag is pushed (the tag is
the version); running the workflow by hand makes a test build in the run's artifacts. To build
locally on Windows with Inno Setup 6: `powershell -ExecutionPolicy Bypass -File tools\windows\build.ps1 -Version 0.2.0`.

## Install

`install.bat` (double-click it in a clone or an unzipped download) does the install below and
asks for the keys; see the [README](README.md#quick-start). By hand, from a terminal on the sim PC:

```
git clone https://github.com/jjmerri/gspro-shotcast.git
cd gspro-shotcast
py -3.13 -m venv .venv
.venv\Scripts\pip install -e ".[capture,cloud]"
copy config.example.yaml config.yaml
copy .env.example .env
```

Then:

1. Fill in `.env` with the keys you use (see [Config and secrets](#config-and-secrets)).
2. Optional, for league features: `copy data\league.example.json data\league.json` and fill in
   your league. Only `teams[].players[].name` is required; add `gspro_name` and `aliases` so the
   booth can match GSPro's player names to your roster. Then set `league_file: data/league.json`
   in `config.yaml`. Without a league file every round is commentated as practice.
3. Edit `config.yaml` if needed. The example uses the cloud models and voices (both API keys in
   `.env`) and GSPconnect's log as the shot source; see [docs/booth.md](docs/booth.md#models-and-voices)
   for local models and voices and [docs/tracking.md](docs/tracking.md#launch-monitors) for other
   launch monitors.
4. Bring it up step by step: `check`, `say "Testing"`, `run --replay data/sample_shots.jsonl`,
   then check the screen read with `screen-check.bat` (on a hole and on each setup tab).

`config.yaml`, `.env`, `data/league.json`, `data/history.json` and `logs/` are all gitignored, so
`git pull` never touches your league or your settings.

## Update

`update.bat` does this (or downloads the latest ZIP when the folder isn't a git clone). By hand:

```
cd gspro-shotcast
git pull
.venv\Scripts\pip install -e ".[capture,cloud]"     # only needed if pyproject.toml changed
.venv\Scripts\python.exe -m compileall -q commentator  # quick syntax check
```

**An update does nothing until the booth is restarted.** Python loads modules once at launch,
so changed code has no effect on a running booth. Relaunch `start-booth.bat`: it closes the
running booth (and its window) and starts a fresh one. Don't update mid-round.

When `config.example.yaml` changes, diff it against your `config.yaml` for new settings.

## Running the booth

Batch files in the repo root (`install.bat` and `update.bat` are under [Install](#install) and
[Update](#update)):

- **`start-booth.bat`** launches the booth minimized in a window titled "Golf Booth". Double-click
  it for a league night; close that window to stop. Running it again restarts the booth: it closes
  any booth already running, window and all, before starting the new one.
- **`screen-check.bat`** counts down 8 s (bring GSPro to the front), then runs `screen-check` into
  `logs\screen-check.txt` and shows it. Use it to confirm the screen read still lines up after a
  GSPro update or a resolution change; `screen-check.bat --fix` writes moved boxes to `config.yaml`.
- **`stats.bat`** opens the stats page over `logs\season.db`; close its window to stop it.
- **Publishing the stats page** (optional): with `publish.enabled` and the AWS keys in `.env`, the
  booth uploads the page right after each round's sign-off
  ([Sharing the stats page](docs/tracking.md#sharing-the-stats-page)); install the uploader once with
  `.venv\Scripts\pip install -e ".[publish]"`.

### Remote administration (optional)

If you manage the PC over SSH (Windows OpenSSH server):

- SSH sessions run Windows `cmd`, so quote-heavy `python -c` one-liners get mangled. Ship a script
  file and run it instead.
- Screen capture (`screen-check`, `calibrate`, `ocr-test`) does **not** work over a plain SSH
  session, because the capture APIs need an interactive desktop. Run it from a logged-in session,
  or from a scheduled task created with `schtasks /it`.
- The PC may be asleep and SSH may time out. Retry or wake it before assuming something failed.

## What comes from where

The booth reads GSPro and the launch monitor out of files GSPro and GSPconnect already write.
It only reads these; it never writes to them. Paths are GSPro's defaults; change them in
`config.yaml` under `source:` if your install differs.

| Source | Default path | Gives the booth |
|---|---|---|
| GSPconnect debug log (`source.type: connector_log`) | `C:\GSProV1\Core\GSPC\ConnectDebug.txt` | Ball and club data per shot from GSPconnect's built-in connectors (e.g. Uneekor) |
| GSPro Unity log (`source.gspro_log_truth: true`) | `%USERPROFILE%\AppData\LocalLow\GSPro\GSPro\output_log.txt` | Authoritative strokes, penalties, resting-lie material, distance to pin, hole-outs, concessions, course and par |
| GSPro course files (`source.gspro_courses_dirs`) | `C:\GSProV1\Courses` and `C:\GSProV1\Core\GSP\Courses` | Par, stroke index, tee yardages, altitude |
| Screen (OCR + vision) | live framebuffer (`screen.capture: monitor`) | Who is up, HUD carry/total tiles, minimap lie and slope, the hole image for the hazard read |
| GSPro's own DB | `%USERPROFILE%\AppData\LocalLow\GSPro\GSPro\GSPro.db` | GSPro's internal store. The booth does not read or write this; listed only so it is not mistaken for ours |

The alternate `gspro_sniffer` source reads the same shot data off GSPro's Open Connect loopback
socket (port 921) with scapy. It needs Npcap with loopback support and an Administrator terminal,
and is the option for third-party connectors that don't go through GSPconnect's debug log.

## Files on the PC

```
gspro-shotcast\
  install.bat            first install (re-run to add the booth or new keys)
  update.bat             update to the latest version, keeping your settings
  start-booth.bat        launch the booth (minimized)
  stats.bat              open the stats page
  screen-check.bat       screen-check into logs\screen-check.txt
  config.yaml            your config (gitignored). Secrets are NOT here
  config.example.yaml    the template, kept in sync with the code
  .env                   secrets only (gitignored)
  .env.example           the template for .env
  .venv\                 the Python environment with all deps
  commentator\           the package
  data\
    league.json          your league (teams, players, aliases, handicaps, schedule, stories; gitignored)
    history.json         earlier seasons, from import-league (optional; gitignored)
  models\                Kokoro voice files (only for local TTS)
  logs\                  everything the booth writes (below)
```

`logs\` holds, per day:

- `booth-YYYY-MM-DD.log` the booth's own debug log (OCR reads, timings, decisions).
- `transcript-YYYY-MM-DD.txt` every line spoken, timestamped.
- `usage.jsonl` per-call LLM token counts, per-request voice timing and audio, per-line wait, with estimated costs, for `cost`.
- `season.db` the SQLite shot and round database (every shot, round, scorecard and spoken line
  across nights; see [docs/tracking.md](docs/tracking.md)). Back this up: it's the only copy.
- `calibrate.png`, `setup*.png`, `load*.png` and similar: debug screenshots.
- `shots\` per-shot debug snapshots when `screen.save_review_images` is on (off by default;
  it is a lot of disk).

## Config and secrets

- **`config.yaml`** is gitignored and per machine. A dev machine and the sim PC usually differ
  (a dev box might use `source.type: replay` and console TTS). Keep a `config.yaml.bak` before
  big edits as a rollback point.
- **`.env` holds only secrets** and is gitignored. Start from `.env.example`. Keys the booth reads:
  - `ANTHROPIC_API_KEY` for Claude calls.
  - `OPENAI_API_KEY` for GPT models and the OpenAI voices.
  - `GOOGLE_TTS_API_KEY` for the Google voices (`tts.provider: google`).
  - `SQUABBIT_EMAIL` and `SQUABBIT_PASSWORD` for the live-handicap pull (only if
    `squabbit.enabled: true`).

## Quick checks

```
.venv\Scripts\python.exe -m commentator check   # league loads, models answer
type logs\transcript-<today>.txt                # what was said tonight
.venv\Scripts\python.exe -m commentator cost    # tonight's spend
```

Tests take a minute or two on Windows. In `cmd`, read the end of the
output with PowerShell `Get-Content ... | Select-Object -Last 20`, since `cmd` has no `tail`.
