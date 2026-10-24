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

Every swing goes into `logs/season.db` with its launch data,
where it finished and what it scored, plus each round's card and stats. The stats page shows
strokes gained, club gapping, approach and putting by distance, scorecards, every shot, and
season highlights. Works with any launch monitor GSPro supports; see
[what's recorded with yours](docs/tracking.md#launch-monitors). Lefties who use GSPro's mirrored
HUD are read too: the layout is detected per player, shot by shot.

**Share it with your league.** The booth can publish the stats page as a password-protected
website (your own AWS account: S3 behind CloudFront, pennies a month or free) and update it on its
own after every round. Each player can pick themselves to see just their rounds, shots and records,
and send a link to exactly what they're looking at. It can live on your own domain if you have
one. Setup is one CloudFormation stack; see
[Sharing the stats page](docs/tracking.md#sharing-the-stats-page).

## The booth

Two voices call the action: **Nigel** on play-by-play, a calm British delivery, and **Duke** on
color, an ex mini-tour grinder who reads the launch numbers like a coach. They call the shot
while it's in the air, react to where it finished, read putts, open and close the round, and
track your league's standings, closest-to-the-pin and net scores. Each player is judged against
their own game, not the longest hitter in the room: a drive is "short" only by that player's
usual numbers. From a few holes in, the booth also gets a round-so-far stat sheet and picks out
whatever pattern stands out tonight. About $0.40-0.60 per 18-hole round with the default cloud models,
or $0 fully local.

Between shots they can drop in an aside, kept occasional so the golf stays first: a recent golf
headline, a bit of history about the course you're playing, or a live score check. Name your
teams and the booth keeps an eye on their games: "Last I saw, the Bruins were up two to one, late
in the second." It also follows the tour leaderboard while an event is on, and checks back later
in the round with an update rather than a repeat. The booth knows the time of day too and, given
a location, the weather outside; see [Asides](docs/booth.md#asides-news-scores-and-course-trivia).

If your league scores in [Squabbit](https://squabbit.com), the booth can sign in (read-only) to
pull each player's live handicap, give strokes exactly as Squabbit does, and know what the other
teams have posted this week and where the season standings sit; see
[Leagues, formats and handicaps](docs/leagues.md#rounds-formats-and-handicaps).

If the sim is booked through [SuperSaaS](https://www.supersaas.com), the booth can also read its
bookings (optional, off by default, `booking:` in `config.yaml`, an API key in `.env`). A round
played in a booking marked "Practice" is called as practice, even on this week's course with a full
team; any other booking, or none, leaves the usual league matching to decide.

Excerpts from real league nights. The opener:

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
- **Python 3.12 or 3.13**, for the ZIP or git install only (the Windows app brings its own).
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

## Quick start

### The Windows app (easiest)

1. Download **`Shotcast-Setup-<version>.exe`** from the latest
   [release](https://github.com/jjmerri/gspro-shotcast/releases/latest) and run it. It installs
   for your Windows user only (no administrator prompt) and brings its own Python. The installer
   isn't code-signed yet, so Windows SmartScreen may say "Windows protected your PC": choose
   **More info → Run anyway**. Each release lists the installer's SHA-256 next to it (see the
   [code signing policy](#code-signing-policy)). The installer finds GSPro (usually `C:\GSProV1`)
   and asks for its folder if it can't.
2. Shotcast starts in the **tray** (the golf ball by the clock; it may be under the ^ arrow) and,
   unless you untick it, every time Windows starts. It records every shot and round without any
   API keys. Click the icon for the **stats page**; right-click it for the menu:
   - **Check the screen read...**: start GSPro, get to a hole, choose this and bring GSPro to the
     front. Every line should say OK (see [Checking the screen read](docs/setup.md#checking-the-screen-read)).
   - **Turn the AI booth on**: asks for the OpenAI and Anthropic keys the first time.
   - **Settings...**: the AI booth, voices and ON AIR badge; API keys (each with a **Check**); the
     GSPro folder; the league file, Squabbit and SuperSaaS; sharing the stats page, S3 backups and
     phone alerts. Everything else is in `config.yaml` (**Settings → Advanced**).
   - **More**: open the Shotcast folder or the logs, edit `config.yaml` or `.env` by hand, and the
     command prompt.
   - **Warnings** (when there are any), **Restart the booth**, **Back up now** (the menu shows the
     last backup),
     **Check for updates** / **Restart to update**, **Check for updates automatically**,
     **Start with Windows**, **Quit**.

   The icon's light is green when everything's fine, orange with warnings, red when the booth
   isn't running. Updates are checked every few hours (unless you untick it; see the
   [privacy policy](PRIVACY.md)) and offered in the menu, never installed on their own (not
   mid-round). Your settings, season database and backups are in
   `%LOCALAPPDATA%\Shotcast`, which updating and uninstalling leave alone.
3. **Where things are.** The docs give paths from the Shotcast folder (`%LOCALAPPDATA%\Shotcast`;
   **More → Open the Shotcast folder**): `logs\season.db`, `config.yaml`, `.env`, and a league
   file you add as `data\league.json`. The docs' `.bat` files are the ZIP install's; the tray
   menu does their jobs.
4. **Commands** such as `publish`, `import-league` and `restore` run from **More → Command
   prompt**: type `shotcast <command>` there (`shotcast --help` lists them; they're the
   `python -m commentator <command>` in the docs).
5. **Uninstall** from Windows Settings → Apps → Shotcast. It removes the app and its "Start with
   Windows" entry and keeps `%LOCALAPPDATA%\Shotcast`; delete that folder too to remove your
   settings and season.

Already running Shotcast from the ZIP or git? See
[Moving to the Windows app](DEPLOY.md#moving-to-the-windows-app) to bring your season and settings
across.

### From the ZIP or git

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

## Code signing policy

Windows releases are built from this repository's source by GitHub Actions
([windows-app.yml](.github/workflows/windows-app.yml)); nothing is built on a personal machine.

Free code signing provided by [SignPath.io](https://about.signpath.io), certificate by
[SignPath Foundation](https://signpath.org) (applied for; until it's granted, releases are
unsigned and each lists its installer's SHA-256).

- Committers and reviewers: [jjmerri](https://github.com/jjmerri) (changes from anyone else come
  as pull requests and are reviewed before merging)
- Approvers (each signed release): [jjmerri](https://github.com/jjmerri)

Privacy: this program will not transfer any information to other networked systems unless
specifically requested by the user or the person installing or operating it. See the
[privacy policy](PRIVACY.md) for exactly what each optional feature sends, and where.

## Documentation

| | |
|---|---|
| [Shot tracking and stats](docs/tracking.md) | Where the data comes from, launch monitors, the database tables, the stats page, example queries |
| [The booth](docs/booth.md) | How a shot is called, models and voices, cost, privacy, the ON AIR badge |
| [Leagues, formats and handicaps](docs/leagues.md) | The league file, league vs practice (and SuperSaaS bookings), formats, handicaps, Squabbit, closest to the pin |
| [Setup, operation and development](docs/setup.md) | Full setup, bring-up, troubleshooting, health checks, phone pushes, commands, tests and tools |
| [DEPLOY.md](DEPLOY.md) | Installing and updating on the sim PC, and where every file and log lives |
| [Privacy policy](PRIVACY.md) | What stays on the PC, and what each optional feature sends where |
| [ARCHITECTURE.md](ARCHITECTURE.md) | The module map and data model, for working on the code |
