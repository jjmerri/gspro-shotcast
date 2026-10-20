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
   shot. The resting-lie reaction fires as soon as the lie is confirmed.

**Voices.** Lines are voiced as soon as they're written, several at once, and played in order.
A cloud voice request still unanswered after `tts.hedge_after_seconds` (2.5) gets a second one
sent alongside it, and the first to answer is used. Every line has a deadline (a flight call
must start within `tts.flight_audio_deadline_seconds` of impact, any other on-demand line within
`tts.live_deadline_seconds`); one that misses it is dropped, along with the replies to it, never
played late.

Every line passes a checker (`grounding.py`) before it is spoken: numbers must come from the
facts, nothing may claim where a ball still in the air will land, and course features need a
screenshot to back them up.

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
| `opener` | `claude-sonnet-5-5` | Raced against the course load, so a fast model |
| `closer` | `claude-opus-5-5`, max_tokens 2000 | Pre-fetched on the last hole (solo: once the ball is on the green; a group: once only one side is left to finish), branched by the final score, so latency is hidden; use the richest model |
| Voices (`tts`) | OpenAI `gpt-4o-mini-tts` (`fable` / `ballad`) | The dominant cost; see [Cost](#cost) |

### Choosing a setup

Four setups, from best to cheapest. Each is a few `provider`/`model` lines in `config.yaml`, so
you can start with one and switch later.

| Setup | Keys in `.env` | Per round | Trade-offs |
|---|---|---|---|
| **Quality and speed** (what the author runs): the default mix above plus `minimap.provider: jev` | OpenAI, Anthropic, TypeSafe | ~$1.25-2 plus Jev | Fastest calls and the most expressive voices. Jev reads the minimap in ~0.3 s instead of ~4 s, and adds water carries, green front/back and bunker yardages for sharper setup lines. Three accounts and the highest bill; `service_tier: fast` roughly doubles the (small) GPT token cost for steadier latency |
| **Default** (`config.example.yaml` as shipped) | OpenAI, Anthropic | ~$1.25-2 | Same calls and voices; Claude reads hazards off a screenshot, slower and less detailed than Jev |
| **One key: Claude + local voices** (the commented "one key" block, plus `tts.provider: kokoro`) | Anthropic | text calls only | One account, and no voice bill, which is most of the default's cost. Kokoro voices are plainer (no delivery direction) and run on the PC's CPU next to GSPro. Claude Haiku on the fast calls kept every deadline in a scripted test round (flight branches ~2-3 s, mid-flight lines ~1 s); it hasn't had the default's live mileage |
| **Fully local**: Ollama (`qwen3:8b`) + Kokoro | none | $0 | Nothing leaves the PC, but slower and plainer on a typical sim PC; a vision model for the hazard read needs ~8-10 GB of free VRAM next to GSPro, or go without it |

The voices are where the money goes, so the "one key" setup is the cheap cloud option, and it
is the least to sign up for. Keeping OpenAI for voices only (Claude for the text) works too: put
just the `tts` block back on `openai`.

## Cost

With the default cloud setup, expect roughly (measured on real rounds):

| Round | Voices | Text models | Total |
|---|---|---|---|
| 18 holes, 1 player | ~$1.00 | ~$0.15 | **~$1.25** |
| 18 holes, 2 players | ~$1.80 | ~$0.20 | **~$2.00** |

Voices are most of it, and they scale with the number of shots. Each shot's possible outcomes
are voiced before the ball lands so the call is instant, and about 70% of that audio is never
played. Higher scores mean more shots and more cost. These are estimates from the booth's own
logging, at the providers' prices when it was written; check current pricing.

Keeping spend in check:

- **Set a monthly spending limit** in each provider's console (OpenAI, Anthropic) before you start.
  A bug or a booth left running then can't run up a surprise bill.
- **Click the ON AIR badge** (or `run --no-commentary`) when you don't want commentary: off air
  makes no model or voice calls, and shots are still recorded.
- **Voice on the PC**: Kokoro voices cost nothing per line, which removes most of the bill
  (see [Choosing a setup](#choosing-a-setup)); Ollama for the text too makes it $0.
- **See what you actually spent.** The booth log ends each session with an "Audio this session"
  line (voice minutes and estimated cost), and `cost` totals the text-model calls per night:

```
python -m commentator cost                          # text-model usage per night
python -m commentator cost --as-model claude-sonnet-5-5   # the same calls, repriced on a Claude model
```

## Privacy

With the default cloud setup, the booth sends to OpenAI and Anthropic what the commentators need
to talk: player names, shot and score data, and the league file's context (notes, storylines).
The pre-shot hazard look also sends a screenshot of GSPro to the vision model (Claude by default;
turn it off with `screen.send_images: false`). With `minimap.provider: jev`, a crop of the minimap
goes to TypeSafe's Jev instead. Every spoken line's text goes to OpenAI to be
voiced (unless the voices run on the PC with Kokoro). Nothing else leaves the PC: the shot database, transcripts and logs stay in `logs/`. The
fully local setup (Ollama and Kokoro) sends nothing at all. Optional features that reach out are
off by default: Squabbit handicaps (`squabbit.enabled`) and golf news (`news.enabled`).

## ON AIR badge

While the booth runs, a small "ON AIR" badge with a pulsing red dot sits over
GSPro, in the top strip between the wind readout and the hole box. Click it to go off air: the
booth stops talking at once and makes no model or voice calls, but keeps recording every shot and
round. Click again to go back on air, mid-round included. It changes the running booth only;
`commentary.enabled` still decides how the booth starts (starting off air, the badge can still put
it on air if the API keys are set). It never takes focus from GSPro and stays on top of it. Move,
resize or hide it with the `overlay` section of the config. GSPro must be windowed or borderless,
not exclusive fullscreen, for anything to draw over it.
