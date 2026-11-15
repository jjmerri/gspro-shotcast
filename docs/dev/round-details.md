# Design: a slim rounds list, each round's details on demand

The deferred item in [stats-longevity.md](stats-longevity.md) ("past years' lists"): the stats page's
`summary-<context>.json` carries every round played, each with its scorecard and summary, and it is
loaded whole on every visit. This keeps every round in the list (no year cut-off, no "show older"
control) but moves what only a round's own page needs into the per-year data files the page already
reads a few pages at a time.

## Why

Measured on the live file (Nov 15, 2026: 121 rounds since Oct 24) with the 0.1.135 build:

| `summary-all.json` part   | Raw     | Gzipped |
|---------------------------|---------|---------|
| Whole file                | 508 KB  | 81 KB   |
| `rounds` list             | 187 KB  | 23 KB   |
| of which `summary`        | 69 KB   |         |
| of which `card`           | 37 KB   |         |
| Slim list (this design)   | 48 KB   | 5.3 KB  |
| Details moved out         | 140 KB  | 16.6 KB |

At 10 years (the bench's 13,284 rounds) the list was 20.3 MB of a 24.8 MB file (2.6 MB gzipped).
Scaled by size per round, the slim list would be about 5 MB raw, so the summary would be about a
third of its current size. This is an estimate; it will be measured on the bench when built (see
"Checks"). The list stays linear in rounds played, but at about 390 bytes a round it's no longer the
bulk of the page.

## The split

A rounds entry (`stats_web.rounds`, around line 529) keeps what the lists, the home page, the
course and player pages, the shots filter and the alerts read:

    id round session ended date started round_end course players format side side_players team
    holes played complete league tournament gross to_par net_to_par fir gir putts gimme_ft

These are read only by the round's own page (`renderRound`) and its share card (`shareCard`,
`teeText`, `ninePart`, `shotsUsed`, `roundSummary`, `puttText`, `mulliganTotal`):

    card summary tee tee_yards course_yards course_holes mulligans putt_split scramble sand
    penalties longest_drive

These were checked against every use in `stats_web.html`. The Records tab's `longest_drive` is the
records' own, not the round's.

## Where the details live

- A new table in each year's data file (`stats_site.build_data_db`):
  `pub_round (id INTEGER PRIMARY KEY, round INTEGER NOT NULL, json TEXT NOT NULL)`, one row per side
  (`id` is the rounds entry's id), indexed on `round`.
- A round's sides go in the year that holds its hole replays (its first hole's year), so
  `pickFiles(data, { round })` finds them the way `loadReplays` finds the replays. The file's
  `rounds` range in `data.json` takes in `pub_round` as well as `pub_shot` and `pub_replay`. A round
  without shots or replays still gets a row and a range.
- Most of the details are the same in every context: `card` and the rest depend only on the round
  and its own gimme radius. The summary doesn't:
  - `summary.vs[].season` (the player's season figure beside the round's) is the context's season
    number, and every new round moves it. It moves to the page (below), so the stored row holds only
    the round's own figures. That makes the existing "Decisions" note in stats-longevity.md true: the
    page works out the `vs` figures when it shows a round.
  - Strokes gained and the best and worst shots come from the context's own shots
    (`stats_web._collect_holes`, around line 1117). A round with a shot in another context (a
    practice-tagged putt in a league round, say) gets a different summary in each. The row keeps the
    "all" summary plus, under `summary_ctx`, the summary of each context where it differs. That's
    rare (none on the live file once `season` is taken out: 3 rounds differed, all only in it), so
    it costs next to nothing. The page uses `summary_ctx[state.ctx]` when present.
- The build takes the rows from the "all" summary's rounds (and the other contexts' summaries where
  they differ) before stripping them, and `page_files` writes them. `build` already writes the summaries before `page_files`, so no second pass over the
  database is needed.

### Rebuilding a year

A year's file is rebuilt only when its signature changes (`_year_sigs`). Card edits already change
it through the nights' signatures, but a stored summary also follows things outside those nights
(strokes gained baselines, a player's name, the usual gimme). So:

- Each year's signature also takes a hash of its rounds' `pub_round` JSON. The rows are computed on
  every build anyway; hashing them is cheap and exact.
- The page-year cache kind gets a layout number, so the first build with this change rebuilds every
  year once (each one is uploaded once; after that, a year with no changes is never re-sent).

## The page

- `loadRound(round)`: reads `SELECT id, json FROM pub_round WHERE round = ?` from the files
  `pickFiles` names, in the same pass as `loadReplays` (same files, same `onSite` retry). It keeps
  every side of the round in `state.details[id]`, so switching sides needs no fetch. This is cleared
  when the version changes, like `state.replays`.
- `renderRound` draws the header from the list entry straight away: who, course, format, score, to
  par, FIR, GIR and putts. It shows "Loading the scorecard…" in place of the scorecard and the
  detail cards, then renders again when the row arrives, as it already does for replays. When the
  row can't be read: "Couldn't load this round's scorecard" plus a retry link; the rest of the
  page still works.
- The "Share card" button appears once the details are in. `shareRound` must build the image
  synchronously from the tap (iOS), so it can't wait on a fetch.
- `roundSummary` fills each `vs[].season` from `state.data.players` for the current context
  (`avg_to_par_9`, `fir_pct`, `gir_pct`, `putts_per_hole`), the same numbers the build uses today.
  With no player row, there is no summary, as today.
- The local page (the tray's server) builds its site with the same `stats_site.build(local=True)`
  and serves its data files the same way, so it needs nothing of its own.

## Alerts

`alerts.py` finds eagles and aces by reading each new round's `card` in `summary-all.json`. Without
the card it would silently stop sending them. After a publish it will read the new rounds' rows
from `pub_round` in the site folder's data files (`v/<version>/data.json` names them; they're local
SQLite files). It reads only the rounds it hasn't alerted yet. A test covers an eagle still being
alerted.

## A page open across the publish

A page loaded before this version has the old script. It reloads the summary on a context switch or
refresh, and a slim list with no `card` would break its round page. Today nothing tells an old page
to reload: `latest.json` holds only the version and its data files. So:

- `latest.json` gets `"page": <number>` (bumped when the summary's shape changes), and the new page
  reloads itself when it differs from its own. This handles every later change.
- This one switch still needs a manual refresh from pages already open. That's the same as the first
  publish with data files.

## What doesn't change

- The other lists' sizes. At 1 year: `players` is 219 KB raw (its `index_history` grows one point a
  round), `courses` 40 KB and `highlights` 38 KB. They aren't part of this change; measure them on
  the bench with the rest.
- No migration: the page and its files are rebuilt together each version. Going back to 0.1.135
  rebuilds the old layout (its own cache kind).
- The cloud plan: details live in immutable, content-addressed per-year files, so past years upload
  once and are served from the CDN cache. That's the same shape the hosted version would use.

## Time filters (option 3, later)

The page already has time filters, but none on the rounds list:

- Records/highlights: `PERIODS` (3m, 6m, 9m, 1y, All time, and "This season" when a season start is
  set).
- Shots: from/to date pickers.
- Gapping: the last 12 months, worked out in the build.

If the list itself ever needs to shrink, the Rounds, course and player pages could reuse `PERIODS`
over the slim list with no new data.

## Checks

- Tests:
  - The summary's rounds have none of the moved fields.
  - Each round has one `pub_round` row per side, in its replays' year.
  - A round with a shot in another context shows that context's summary in it, as today.
  - The year's `rounds` range covers a round with no shots.
  - Editing a round rebuilds only its year.
  - An eagle is still alerted.
  - The page test drives a round page and a share card from the data file.
- Bench (`tools/scale_bench.py`) at 1 and 10 years: the summary's size raw and gzipped, the cold
  build, the build after a round, and how long a round page takes to open the first time.
- On the PC, before release: open a few rounds and share a card, on the local page and the published
  one.
