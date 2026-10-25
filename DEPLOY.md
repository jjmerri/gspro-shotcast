# Deploy and operate on the sim PC

Shotcast runs on the Windows PC that runs GSPro, installed from
[Releases](https://github.com/jjmerri/gspro-shotcast/releases) (see the
[README](README.md#the-windows-app-easiest)). To work on the code, see
[Setup and development](docs/setup.md).

Your own machine details (host, user, SSH key, paths) belong in `DEPLOY.local.md`, which is
gitignored. Keep personal notes there, not here.

## The Windows app

The installer puts a private Python with Shotcast in `%LOCALAPPDATA%\Programs\Shotcast` and runs
`shotcast tray`, which keeps the booth running (restarting it after a crash, up to 3 times in 10
minutes), serves the stats page on this PC and offers updates. Everything else (config.yaml, .env,
the league file, `logs\` with season.db, backups and the booth, tray and `booth-errors.log` logs)
lives in `%LOCALAPPDATA%\Shotcast`.

**Updating.** The tray offers new releases (**Check for updates** / **Restart to update**); it
never installs one on its own. An update closes the booth, installs, and starts Shotcast again, so
don't update mid-round.

**Publishing the stats page** (optional): with sharing set up in **Settings**, the booth uploads
the page right after each round's sign-off
([Sharing the stats page](docs/tracking.md#sharing-the-stats-page)).

Don't run two booths: if another one is running (say, from a source checkout), the tray doesn't
start its own and says so; **Restart the booth** in its menu stops the other and starts its own.

## Releases

Releases are built by `.github/workflows/windows-app.yml` when a `v*` tag is pushed (the tag is
the version); running the workflow by hand makes a test build in the run's artifacts. To build
locally on Windows with Inno Setup 6: `powershell -ExecutionPolicy Bypass -File tools\windows\build.ps1 -Version 0.2.0`.

## Remote administration (optional)

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
%LOCALAPPDATA%\Programs\Shotcast\   the app: its own Python, Shotcast, shotcast.exe (replaced by updates)
%LOCALAPPDATA%\Shotcast\             your files (kept by updates and uninstall)
  config.yaml            your config. Secrets are NOT here
  .env                   secrets only
  data\
    league.json          your league (teams, players, aliases, handicaps, schedule, stories)
    history.json         earlier seasons, from import-league (optional)
  logs\                  everything the booth writes (below)
  updates\               downloaded installers and the last update's install log
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

Most settings are in the tray's **Settings** window; the rest are in `config.yaml` (**Settings →
Advanced**).

- **`config.yaml`** is per machine. Keep a `config.yaml.bak` before big edits as a rollback point.
- **`.env` holds only secrets** (the Settings window's API keys go here). Keys the booth reads:
  - `ANTHROPIC_API_KEY` for Claude calls.
  - `OPENAI_API_KEY` for GPT models and the OpenAI voices.
  - `GOOGLE_TTS_API_KEY` for the Google voices (`tts.provider: google`).
  - `SQUABBIT_EMAIL` and `SQUABBIT_PASSWORD` for the live-handicap pull (only if
    `squabbit.enabled: true`).

## Quick checks

From the tray's **More → Command prompt** (or `%LOCALAPPDATA%\Programs\Shotcast\Scripts\shotcast.exe`
from `%LOCALAPPDATA%\Shotcast`):

```
shotcast check                     # league loads, models answer
type logs\transcript-<today>.txt   # what was said tonight
shotcast cost                      # tonight's spend
```

In `cmd`, read the end of a log with PowerShell `Get-Content ... | Select-Object -Last 20`, since
`cmd` has no `tail`.
