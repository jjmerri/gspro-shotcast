[← README](../README.md)

# The booth

## How a shot sounds

1. **Pre-shot.** When the player box settles on a new shot, three calls fire at once: a spoken
   setup line (`live`), a hazard read off the minimap (`preshot`, vision; or Jev with
   `minimap.provider: jev`, which runs first and also hands its hazards and yardages to the
   setup line and branches as facts; on a putt, `putt_read.enabled` instead reads the break and
   slope from the drift of GSPro's green-grid dots over a quick burst of frames), and the full set of
   pre-written branches (`flight`): a mid-flight line for on-target / short / long / left /
   right / toward-water, and a resting-lie reaction for every finish (fairway, green, each
   rough severity, sand, pine straw, cart path, structure, water). A putt gets holed / short /
   past instead, plus a "gimme" line from `commentary.gimme_lag_feet` (30 ft) or longer, which
   always plays when the lag stops inside the gimme circle. From hole
   `commentary.round_form_after_holes` (3; 0 turns it off) the setup line also gets the round so far
   as a stat sheet: putts holed by length, approach leaves by distance, greens, fairways, driving,
   scrambling, birdie looks, each beside the player's season figure. It's optional colour; the booth
   picks a pattern if one is worth saying. Once it uses one, the sheet rests for two holes, and
   it's told which angles it already used this round and leaned on in the player's last few rounds
   (`form_mentions` in the database), so it doesn't settle into a refrain. Length is judged only
   against the player's own game: once a player has 5 drives (tee shots with 250+ yards to the pin)
   or 5 full swings with a club in the season, the setup and the reaction get their usual length and
   how this shot compared. Without that, the booth is told not to call a shot short or long at all.
2. **Impact.** No model call. Code projects carry against the pin and the drift from start line
   and spin axis. The launch monitor's carry is first scaled to what GSPro will allow: a course
   baseline (altitude and conditions, learned from tee and fairway shots) times a per-lie
   penalty (learned per lie, carry band, and launch band from your own shots). Pin elevation
   from the player-box arrow adjusts the distance it plays; wind is ignored. Keep GSPro's
   CARRY (raw) and CARRY (game) tiles visible so the model keeps learning. The code then picks
   the matching flight branch and speaks it, cutting off anything still playing. A ball headed
   for water gets the in-flight tease the instant the log shows it is wet.
3. **Landing.** The HUD is polled from impact (`screen.follow_poll_seconds`): CARRY (game)
   counts up and freezes at touchdown, Total Length counts until the ball stops, then GSPro
   moves the player box to the next shot. The last reading that still belonged to this shot
   gives the reaction its lie and numbers, so the next player's lie is never credited to this
   shot. From touchdown on, the HUD is grabbed without OCR every poll, and the first grab whose
   tiles haven't changed since the last (the ball has stopped) is the one read, so GSPro moving
   on a second later doesn't matter. If the box moves on before the ball is seen at rest, the
   lie isn't named (except the green); the next setup and GSPro's log record where it stopped.
   A shot that GSPro's log says ended the hole (holed, or stopped inside the gimme circle) stops
   the follow there, so a chip that ends the hole is called without waiting to see it at rest.
   GSPro also logs the end of every shot's roll (`Roll dist ft`): the first read after that line is
   taken as at rest, so a short or low ball the screen never shows landing doesn't wait out the cap.
   A ball that GSPro's log says hit foliage in flight (`Leaves hitTime`) gets a reaction fact. GSPro
   doesn't say whether it was a tree or a bush, so `flight.py` models the height at the first hit from
   the launch numbers: under 10 ft is a bush or low branches, 10-20 ft a big bush or a tree's low
   branches, 20-30 ft the branches of a tree, higher is high up in a tree, and a hit after the modelled
   landing is the ball running into the trees as it came down. A first bounce off wood (`Using Material
   TVGwood`) is taken as a tree trunk or branch, rising or falling, unless the ball met it at ground
   level: GSPro's carry (which ends at that first contact) at least 70% of the full carry, about where
   it would have landed anyway, is a bridge, steps or a wall. Leaves on the same shot, or no carry read,
   keep it a tree. Its shortened carry isn't learned from (`shots.tree_hits`).
   GSPro logs foliage hits at launch, each with its time into the flight, so a hit that cost at least
   15% of the ball's speed is also called in flight, timed to the moment the ball reaches the
   branches (a stock line, no model call). That call replaces the planned flight line. A trunk isn't
   called in flight: only the carry, read once the ball stops, tells it from a bridge.

**Voices.** Lines are voiced as soon as they're written, several at once, and played in order. A
cloud voice streams: a line starts playing once `tts.stream_prebuffer_seconds` (0.3) of audio has
arrived (under a second) while the rest downloads; the stream comes in uneven bursts, and the head
start keeps the voice from breaking up. Cloud voice requests sometimes stall and send nothing at all, so one with no audio after
`tts.first_audio_timeout_seconds` (1.0) gets `tts.retries` (2) more sent alongside it, and the first
to start is played. With `tts.stream: false` whole clips are fetched instead, and a request still
unanswered after `tts.hedge_after_seconds` (2.5) gets another sent alongside it.
Consecutive lines get a short pause, `tts.turn_gap_seconds` (0.4) when the other commentator
takes over and `tts.line_gap_seconds` (0.15) when the same one carries on, so an exchange doesn't
sound rapid-fire. It only tops up the silence since the last line ended: a line after a longer
quiet spell (a mid-flight call) starts at once, and an interrupt skips it.

Only what airs is voiced (`tts.prevoice: false`, the default): the flight call is voiced at impact,
the result call when the ball stops, and the wrap and asides as they play. Their text is
still written ahead, so there's no model wait. `tts.prevoice: true` voices every possible flight and
result call at setup, and every wrap branch, so they play with no voicing wait, but
most of that audio is never played (about 80% of the voice bill). A local voice (Kokoro) always
voices ahead, since it's free and slow on CPU. Every line has a deadline (a flight call
must start within `tts.flight_audio_deadline_seconds` of impact, any other on-demand line within
`tts.live_deadline_seconds`); one that misses it is dropped, along with the replies to it, never
played late.

Every line passes a checker (`grounding.py`) before it is spoken: numbers must come from the
facts, nothing may claim where a ball still in the air will land, and course features need a
screenshot to back them up.

## Asides: news, scores and course trivia

On an ordinary shot the booth would otherwise pass on, it sometimes drifts into a two-line side
conversation instead. Each one is written ahead by the closer's model (the richest one), so it plays
with no model wait. They all share one budget: at most one every three holes, never
on the first hole or the last two, and only after an ordinary shot, so the round's own calls come first.

- **Live scores** (`scores`): a tour leaderboard and your local teams. Which holes get a check-in is
  decided at round start, and only if something is live, just finished or on later tonight. The scores
  themselves are fetched as that hole starts and the check-in is written then, so it's minutes old on
  air; one that hasn't found a quiet moment within `max_age_minutes` is dropped. If another aside
  aired too recently for it to play, the check-in moves to the first hole it can. A game the booth
  already mentioned comes back only as an update, once its score has moved.
- **Course trivia** (`trivia`): a bit of the real course's history from its Wikipedia article. A story
  about one hole (the Road Hole, the island green) waits for that hole, even late in the round. What
  aired is remembered, so a course played again gets different stories.
- **Golf news** (`news`): a real headline from the feed, told as of when it came out ("over the
  weekend"), not as breaking news. Stories older than `news.max_age_days` (3) are left out. A story
  that suits a moment in a round — a long drive, a ball in trouble, a birdie just posted, the turn —
  is held back and played on that moment, opening with a line that bridges into it instead of its
  own. Prep waits a few seconds for the round start to settle, and stops if the round is replaced,
  so a false start costs nothing.

With more than one on, they're one bucket: when an ordinary shot gets an aside (`news.banter_rate`),
it's picked at random from those ready. Only as many are written as the round has room for — on an
18 with all three on, that is one news aside, two trivia and two check-ins — so the booth isn't
paying its dearest model for asides nobody will hear.

## Models and voices

The booth itself always runs on the sim PC; the models and voices it calls can be cloud APIs
(the default) or run on the PC itself. Everything swappable lives behind one config setting, so
the same code runs fully cloud, hybrid, or fully local. `config.example.yaml` ships a **per-call
model mix**: each kind of call gets the model that fits its latency and quality budget, and it
needs an OpenAI and an Anthropic API key (an Anthropic key alone works too; see
[Choosing a setup](#choosing-a-setup)). Audio generation (TTS), not the text calls, is the
real cost.

| Call | Default model | Why |
|---|---|---|
| `live` (reactions, segments) | `gpt-6-luna`, effort none, service_tier fast | Matches Sonnet speed/quality here for a fraction of the cost |
| `flight` (pre-built flight + result branches) | `gpt-6-luna`, effort none, fast, max_tokens 900 | One reply holds ~7 flight + ~10 result lines; needs room so the JSON never truncates |
| `preshot` (hazard read, vision) | `claude-haiku-5-5`, vision, max_tokens 900 | Reads the hole minimap for hazards; cheaper and better at it than 4.5 |
| `minimap` (optional, `provider: jev`) | TypeSafe `jev-latest` (+ `jev-preview` to find the ball and pin) | Reads the minimap in ~0.3 s: water, red-lined penalty areas with no water in them, out of bounds and bunkers left/right of the line to the pin, water to carry, front/back of the green, and bunkers measured off the map's scale (code finds sand-coloured patches near the line, Jev confirms each is a bunker, not a beach or roof), trees near the line and any tree right in the way, which side of the green the pin is on, and doglegs from the tee. Answers below `threshold` (bunkers: `sand_threshold`) are dropped. A side's trouble flight line may then name that side's hazard, and plays only when that hazard is in play. Needs `.[jev]` and `TYPESAFE_API_KEY`. Falls back to `preshot` |
| `opener` | `claude-sonnet-5-5` | Raced against the course load, so a fast model. At most `opener_max_lines` (3) lines of up to `opener_max_words` (25) words |
| `closer` | `claude-opus-5-5`, max_tokens 2000 | One call at round end, once the final score is known; it's written while the last hole's wrap airs, so most of its latency is hidden; use the richest model. Its lines may run to `closer_max_words` (40) rather than a live call's 18 |
| Voices (`tts`) | OpenAI `gpt-4o-mini-tts` (`fable` / `ballad`) | The dominant cost; see [Cost](#cost) |

### Choosing a setup

Four setups, from best to cheapest. Each is a few `provider`/`model` lines in `config.yaml`, so
you can start with one and switch later.

| Setup | Keys in `.env` | Per round | Trade-offs |
|---|---|---|---|
| **Quality and speed** (what the author runs): the default mix above plus `minimap.provider: jev` | OpenAI, Anthropic, TypeSafe | ~$0.40-0.60 plus Jev | Fastest calls and the most expressive voices. Jev reads the minimap in ~0.3 s instead of ~4 s, and adds water carries, green front/back and bunker yardages for sharper setup lines. Three accounts and the highest bill; `service_tier: fast` roughly doubles the (small) GPT token cost for steadier latency |
| **Default** (`config.example.yaml` as shipped) | OpenAI, Anthropic | ~$0.40-0.60 | Same calls and voices; Claude reads hazards off a screenshot, slower and less detailed than Jev |
| **One key: Claude + local voices** (the commented "one key" block, plus `tts.provider: kokoro`) | Anthropic | text calls only | One account, and no voice bill, which is most of the default's cost. Kokoro voices are plainer (no delivery direction) and run on the PC's CPU next to GSPro. Claude Haiku on the fast calls kept every deadline in a scripted test round (flight branches ~2-3 s, mid-flight lines ~1 s); it hasn't had the default's live mileage |
| **Fully local**: Ollama (`qwen3:8b`) + Kokoro | none | $0 | Nothing leaves the PC, but slower and plainer on a typical sim PC; a vision model for the hazard read needs ~8-10 GB of free VRAM next to GSPro, or go without it |

The voices are where the money goes, so the "one key" setup is the cheap cloud option, and it
is the least to sign up for. Keeping OpenAI for voices only (Claude for the text) works too: put
just the `tts` block back on `openai`.

**Google voices** (`tts.provider: google`, `GOOGLE_TTS_API_KEY`) are another cloud option: Chirp 3
HD voices (`tts.google_voices`; British Iapetus and American Orus by default), streamed with about
0.2 s to first audio. Google bills $30 per million characters after the first million each month.
A two-player nine-hole round airs about 14,300 characters (a solo one about 8,300) with only what
airs voiced, so the free million covers about 70 two-player rounds a month (about 120 solo), then
about $0.43 a two-player round. `tts.prevoice: true` voices about 64,000 characters a round, so
the free tier lasts about 15 rounds.
There's no delivery direction, so `openai_instructions` don't apply. Create a Google Cloud project
with billing on, enable the Cloud Text-to-Speech API, and make an API key restricted to it.

## Cost

With the default cloud setup, expect roughly (measured on real rounds):

| Round | Voices | Text models | Total |
|---|---|---|---|
| 18 holes, 1 player | ~$0.25 | ~$0.15 | **~$0.40** |
| 18 holes, 2 players | ~$0.40 | ~$0.20 | **~$0.60** |

Voices scale with the number of shots, so higher scores mean more cost. Only what airs is voiced;
with `tts.prevoice: true` every possible call is voiced ahead and the voice column is about four times
higher (the voice figures above are scaled from rounds measured that way). These are estimates from the booth's own
logging, at the providers' prices when it was written; check current pricing.

Keeping spend in check:

- **Set a monthly spending limit** in each provider's console (OpenAI, Anthropic) before you start.
  A bug or a booth left running then can't run up a surprise bill.
- **Click the ON AIR badge** (or `run --no-commentary`) when you don't want commentary: off air
  makes no model or voice calls, and shots are still recorded.
- **Voice on the PC**: Kokoro voices cost nothing per line, which removes most of the bill
  (see [Choosing a setup](#choosing-a-setup)); Ollama for the text too makes it $0.
- **See what you actually spent.** `logs/usage.jsonl` gets a row for every model call (Jev's minimap
  requests too, as provider `typesafe`), every voice request (`kind: tts`: time to first audio, audio length, outcome, estimated cost) and every line
  aired or dropped (`kind: line`: how long it waited to start). `cost` totals them per night: text
  cost by provider, voice cost, minutes voiced vs aired, voice requests that never answered, time to
  first audio and line wait (median/90th percentile), and lines dropped. The booth log also ends each
  session with an "Audio this session" line.

```
python -m commentator cost                          # spend and voice latency per night
python -m commentator cost --as-model claude-sonnet-5-5   # the text calls repriced on a Claude model
```

## Privacy

The full list of what Shotcast sends anywhere is in the [privacy policy](../PRIVACY.md).
With the default cloud setup, the booth sends to OpenAI and Anthropic what the commentators need
to talk: player names, shot and score data, and the league file's context (notes, storylines).
The pre-shot hazard look also sends a screenshot of GSPro to the vision model (Claude by default;
turn it off with `screen.send_images: false`). With `minimap.provider: jev`, a crop of the minimap
goes to TypeSafe's Jev instead. Every spoken line's text goes to OpenAI to be
voiced (unless the voices run on the PC with Kokoro). Nothing else leaves the PC: the shot database, transcripts and logs stay in `logs/`. The
fully local setup (Ollama and Kokoro) sends nothing at all. Optional features that reach out are
off by default: Squabbit handicaps (`squabbit.enabled`) and golf news (`news.enabled`), live scores (`scores.enabled`: ESPN's public scoreboards
for a tour leaderboard and your local teams), course trivia (`trivia.enabled`: the course's name to
Wikipedia), and the local weather (`weather.enabled`), which sends only the shed's location (looked up
once from `weather.location`) to Open-Meteo every 30 minutes. The weather is colour for
the opener, the closer and the odd between-holes line; the booth also knows the day and time.

## ON AIR badge

While the booth runs, a small "ON AIR" badge with a pulsing red dot sits over
GSPro, in the top strip between the wind readout and the hole box. Click it to go off air: the
booth stops talking at once and makes no model or voice calls, but keeps recording every shot and
round. Click again to go back on air, mid-round included. It changes the running booth only;
`commentary.enabled` still decides how the booth starts (starting off air, the badge can still put
it on air if the API keys are set). It never takes focus from GSPro and stays on top of it. Move,
resize or hide it with the `overlay` section of the config. GSPro must be windowed or borderless,
not exclusive fullscreen, for anything to draw over it.
