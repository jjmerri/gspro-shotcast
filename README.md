# GSPro Shotcast

[![tests](https://github.com/jjmerri/gspro-shotcast/actions/workflows/tests.yml/badge.svg)](https://github.com/jjmerri/gspro-shotcast/actions/workflows/tests.yml)

Shot tracking, season stats and an AI broadcast booth for **GSPro 3.1.x** simulator golf.

- **Shot tracking.** GSPro shows a scorecard and forgets it. Shotcast keeps every swing: launch
  data, where it finished, what it scored, plus every round's card and stats (fairways, greens,
  putts, scrambling). It all goes into one local SQLite file. No cloud services, accounts or API
  keys needed.
- **Stats web page.** Strokes gained, club gapping, approach and putting by distance, scorecards,
  every shot, and season highlights, in a local page over that file. Optionally published online
  after every round, behind a password, so the whole league can check their numbers from a phone.
- **The booth.** Two AI commentators call your round live through the speakers. They know the
  launch numbers, the course, your league's standings and storylines, and what you did last
  week. Bring your own league: put your roster, schedule and storylines in a league file and the
  booth calls your players by name.

Use the tracking on its own, or turn the booth on for the full broadcast.

https://github.com/user-attachments/assets/a3582249-23be-48c9-bf14-c5d1197671f8

*The booth calling a hole at Kapalua (sound on): the opener during course load, calls in flight,
a mulligan, and the hole wrap-up.*

## Shot tracking and stats

![Players: season table, strokes gained, night-by-night trends, club gapping](docs/screenshots/players.png)

| | |
|---|---|
| ![Rounds: hole-by-hole scorecards](docs/screenshots/rounds.png) | ![Highlights: season records and top shots](docs/screenshots/highlights.png) |
| ![Shots: every shot, filterable](docs/screenshots/shots.png) | ![Broadcasts: a night's commentary, shot by shot](docs/screenshots/broadcasts.png) |

*(Sample data, player names changed.)* Every swing goes into `logs/season.db` with its launch data,
where it finished and what it scored, plus each round's card and stats. The stats page shows
strokes gained, club gapping, approach and putting by distance, scorecards, every shot, and
season highlights. Works with any launch monitor GSPro supports; see
[what's recorded with yours](docs/tracking.md#launch-monitors).

**Share it with your league.** The booth can publish the stats page as a password-protected
website (your own AWS account: S3 behind CloudFront, pennies a month or free) and update it on its
own after every round. Each player can pick themselves to see just their rounds, shots and records,
and send a link to exactly what they're looking at. Setup is one CloudFormation stack; see
[Sharing the stats page](docs/tracking.md#sharing-the-stats-page).

## The booth

Two voices call the action: **Nigel** on play-by-play, a calm British delivery, and **Duke** on
color, an ex mini-tour grinder who reads the launch numbers like a coach. They call the shot
while it's in the air, react to where it finished, read putts, open and close the round, and
track your league's standings, closest-to-the-pin and net scores. About $1-2 per 18-hole round
with the default cloud models, or $0 fully local.

Excerpts from real league nights (names changed). The opener:

> **Nigel:** Good evening, and welcome to the Tuesday Night Sim League, week six, the back nine at Cabot Cliffs.<br>
> **Nigel:** Two titles in a row for that pair, and both have a habit of winning closest-to-the-pin prizes.<br>
> **Duke:** Holes 14 and 16 offer those tonight, so keep an eye out.

An approach, called in the air and then where it finished, and the putt:

> **Nigel:** A smooth strike, holding its line toward the flag. *(ball in the air)*<br>
> **Nigel:** Ray's pitching wedge lands ten feet away.<br>
> **Duke:** There's the answer to those right starts, Nigel. That one began right and worked gently back.<br>
> **Nigel:** Ray's putt slides by, just as called, and runs past the cup.

Water, then a mulligan:

> **Nigel:** Bending right, the water down the right is coming into play. *(ball in the air)*<br>
> **Nigel:** That's found the water, a costly penalty on this par five.<br>
> **Nigel:** Marcus replays stroke one after the mulligan, sending it straight at the target.<br>
> **Nigel:** Marcus catches this three-wood cleanly, and it carries 255 before rolling out to 269 in the fairway.<br>
> **Duke:** Five fairways running, Nigel. Much tidier than that driver that tried to escape right.

A putt call, and the wrap-up:

> **Nigel:** Marcus for birdie to reach plus two; uphill from eleven feet.<br>
> **Duke:** I'm calling this one to slide by, Nigel.<br>
> **Nigel:** Marcus leaves the uphill birdie putt short; the tap-in remains.<br>
> ...<br>
> **Nigel:** That's the night at Cabot Cliffs, Duke, and Toby was the steady hand throughout.<br>
> **Duke:** Only two fairways found, Nigel, yet he kept scrambling to greens.<br>
> **Nigel:** And Marcus's tee shot on sixteen, inside ten feet, is in the closest-to-the-pin chase this week.<br>
> **Duke:** Toby and Marcus post net three under. We'll see if it holds. Goodnight, Nigel.

```
Launch monitor ─► GSPconnect or GSPro log ─► ball data ──────────┐
GSPro output_log.txt ───────────► strokes/lie/pin/penalties ─────┤
Screen (framebuffer) ─► OCR ────► who's up, HUD tiles, minimap ──┼─► engine ─► LLM ─► TTS ─► speakers
GSPro course files ─────────────► par, stroke index, yardage ────┤          │
League file + Squabbit ─────────► standings, handicaps, stories ─┘          └─► logs/transcript, logs/usage.jsonl, logs/season.db
```

## Requirements

- A Windows PC running **GSPro 3.1.x**, windowed or borderless (not exclusive fullscreen).
  Shotcast runs on the same PC. It reads GSPro's screen; checked on 16:10 (3450×2160) and 16:9
  (1920×1080) screens, and `screen-check --fix` fits it to others.
- Any launch monitor GSPro supports. Built and tested with a **Uneekor EYE XO2**; others record
  the same shots and scores, with club data only when the monitor sends it
  ([Launch monitors](docs/tracking.md#launch-monitors)).
- **Python 3.12 or 3.13.**
- For the booth only: speakers, and an **OpenAI API key** and an **Anthropic API key** for the
  default setup ([Cost](docs/booth.md#cost)). An Anthropic key alone works with voices on the PC,
  and fully local models and voices need no keys ([Choosing a setup](docs/booth.md#choosing-a-setup)).

## Tested on, and known limits

Shotcast has run a season of weekly league nights on one setup: a Windows PC with GSPro 3.1.x
on a 3450×2160 (16:10) screen and a Uneekor EYE XO2 through GSPconnect. Anything
beyond that is supported on paper but not yet run live:

- **Other launch monitors.** Shots should come through GSPconnect or GSPro's own log for any
  monitor, but only the EYE XO2 has been used ([Launch monitors](docs/tracking.md#launch-monitors)).
- **Other screen sizes.** At 1920×1080 (16:9) the setup screen and the tee HUD have been checked
  live; the shot data tiles, the other lies, the scorecard and the ON AIR badge have not.
  Ultrawide and 4:3 screens are untested. Run `screen-check` first on any screen.
- **GSPro must stay visible.** The screen is read live, so GSPro has to be on screen and not
  covered. A GSPro window smaller than the screen is only tested on saved screenshots.
- **English and yards.** The screen and log reads expect GSPro's English labels, yards and mph.
  Metres or another language will likely misread.
- **GSPro updates.** Built against 3.1.x. A release that moves the HUD or changes the log can
  break reads until the code catches up; `screen-check` shows which boxes stopped reading.
- **Windows only** for tracking and the booth. The stats page runs anywhere with Python.

It's a personal project shared as is. Issues are welcome, but fixes for other setups aren't
promised.

## Quick start

1. On GitHub, **Code → Download ZIP**, and unzip it somewhere it can stay (for example
   `Documents\gspro-shotcast`). Or `git clone https://github.com/jjmerri/gspro-shotcast.git`.
2. Double-click **`install.bat`**. It finds Python 3.12 or 3.13 (or offers to install 3.13),
   asks whether you want **shot tracking only** (no API keys, nothing leaves the PC) or
   **tracking plus the booth**, installs everything into the folder, creates `config.yaml` and
   `.env`, asks for the booth's API keys, and offers desktop shortcuts. Re-run it any time, for
   example to add the booth later; it never overwrites your settings.
3. Start GSPro, get to a hole, and double-click **`screen-check.bat`** (bring GSPro to the front
   during the countdown). Every line should say OK: the screen read tells Shotcast who's up. If
   some say FIX, run `screen-check.bat --fix` from a command prompt in the folder; see
   [Checking the screen read](docs/setup.md#checking-the-screen-read).
4. Double-click **`start-booth.bat`** (the Shotcast shortcut) and play. Every shot and round goes
   into `logs\season.db`; with the booth on, click the ON AIR badge over GSPro to go off air
   (recording continues) and back. If shots don't show up, see
   [Launch monitors](docs/tracking.md#launch-monitors).
5. **`stats.bat`** (Shotcast Stats) opens the stats page. To browse on another computer (Mac
   too), copy `logs/season.db` into a checkout there and run `python -m commentator stats-web`;
   it only needs Python and `pip install -e .`. To put it online for the league behind a password,
   updated after every round, see [Sharing the stats page](docs/tracking.md#sharing-the-stats-page).

**`update.bat`** updates to the latest version (`git pull`, or a fresh download if you used the
ZIP) and keeps your `config.yaml`, `.env`, league file and `logs`.

No league file is needed; players are recorded and called by their GSPro names. Add one later to
split league and practice rounds and get standings, net scores and storylines
([Leagues](docs/leagues.md)). To install by hand instead, see
[Setup](docs/setup.md#setup-fresh-machine).

## Documentation

| | |
|---|---|
| [Shot tracking and stats](docs/tracking.md) | Where the data comes from, launch monitors, the database tables, the stats page, example queries |
| [The booth](docs/booth.md) | How a shot is called, models and voices, cost, privacy, the ON AIR badge |
| [Leagues, formats and handicaps](docs/leagues.md) | The league file, league vs practice, formats, handicaps, Squabbit, closest to the pin |
| [Setup, operation and development](docs/setup.md) | Full setup, bring-up, troubleshooting, health checks, phone pushes, commands, tests and tools |
| [DEPLOY.md](DEPLOY.md) | Installing and updating on the sim PC, and where every file and log lives |
| [ARCHITECTURE.md](ARCHITECTURE.md) | The module map and data model, for working on the code |
