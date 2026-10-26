# Shotcast privacy policy

Shotcast is free, open-source software. It has no accounts, no analytics and no telemetry, and the
project runs no servers: nothing is sent to the people who make Shotcast.

This program will not transfer any information to other networked systems unless specifically
requested by the user or the person installing or operating it. In detail:

**What stays on your PC.** Every shot, round, score and player name goes into the season database
(`season.db`) in Shotcast's folder (`%LOCALAPPDATA%\Shotcast` for the Windows app), with its logs,
backups and screenshots. The stats page is served on this PC only (`127.0.0.1`).

**Update checks (on by default, can be turned off).** The Windows app asks GitHub
(`api.github.com`) every few hours whether there is a newer release, and downloads it from GitHub
if there is. The request carries your IP address and the app's name, as any web request does, and
nothing about your rounds. GitHub's privacy statement applies:
https://docs.github.com/site-policy/privacy-policies/github-general-privacy-statement. Untick
"Check for updates automatically" in the installer, or in the tray menu, to turn it off; "Check
for updates" in the menu still works when you ask.

**Things you switch on.** These are off until you turn them on and give them your own account or
key. When on, they send this:

| Feature | Sent to | What |
|---|---|---|
| The AI booth (`commentary.enabled`) | OpenAI, Anthropic (and Google, if chosen for the voices) | Player names, shot and score data, the league file's context, screenshots of GSPro for the hazard look (`screen.send_images`), and each line to be voiced. Their API terms apply. A fully local setup (Ollama and Kokoro) sends nothing. |
| Jev minimap reads (`minimap.provider: jev`) | TypeSafe | A crop of GSPro's minimap |
| Sharing the stats page (`publish`) and S3 backups (`backup.s3`) | Your own Amazon S3 bucket | The stats page, and copies of the season database |
| The live stream (`live`) | Your own Amazon S3 bucket | Video of GSPro's screen and the PC's sound (the booth's voices too) while a round is on (or when started by hand from the tray), kept for 7 days (`live.keep_days`) so the page can replay whole rounds, with each round's course and players, then deleted (with `keep_days: 0`, deleted as it plays and when it stops). Its setup downloads OBS Studio from GitHub |
| Phone alerts (`notify`) | ntfy.sh (or your ntfy server) | Health messages (for example "screen read failing") |
| Squabbit handicaps (`squabbit`) | Squabbit (Google Firebase) | Your Squabbit login; it reads your league's handicaps |
| Bookings (`bookings`) | SuperSaaS | Your API key; it reads the sim's bookings. With publishing on, the next two weeks of them (times, type and the booker's name, never an email address) go on the stats page's Schedule tab, `schedule.json` in your bucket (`booking.schedule_days: 0` for none) |
| **Check** / **Send test** next to a key in the app's Settings | That key's service (OpenAI, Anthropic, Google, AWS, ntfy) | The key, to see if it works (only when you click it) |
| Golf news, live scores, course trivia, weather | The news feeds you list, ESPN, Wikipedia, Open-Meteo | Requests for headlines and scores, the course's name, the shed's location |

**Installing.** The Windows installer contains everything. Installing from source (`pip`)
downloads Python packages from PyPI and the spaCy language model and OCR models from
their publishers.

Questions: https://github.com/jjmerri/gspro-shotcast/issues
