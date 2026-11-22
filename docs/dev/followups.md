# Follow-ups

Deferred work, newest last. Take an entry out when it's done.

## Retire the setup-screen slot order once the round file names every slot

Player slots are now named from GSPro's log plus `currentRound.dat` (launch numbers tie a credited
shot to the file's `PlayerName`; `_learn_file_slots` in `commentator/engine/identity.py`). The setup screen's
player order (`setup_slots`) and the HUD-name slot reads (`Round.slot_read`) are kept as the fallback
for the first shot of a round, before the file has a ball to match, and for GSPro 3.59, which logs no
`LaunchExt Vars`. Only GSPro 4.x needs support, so 3.59 no longer blocks this.

**Needs verification:** more multiplayer rounds on 0.1.151+. Evan and Jason's two-player round
(Dinosaur Mountain, 2026-11-19 evening) passed: `confirms the slots of ['Evan', 'Jason']` right after
the second shot, and no `GSPro's round file places the players` lines (the setup screen's order was
right). Still to see: three or four players, and a round where the screen's order is wrong. (The
round's `shots named by:` line counts only each shot's first naming source, so it says `screen` even
when the file confirmed it.) If the file always names every slot by the second shot, remove the screen-read slot
learning (`slot_read`'s read counts, `_recheck_slots`) and keep the setup screen only for format and
nines. The pre-shot "who's up" read stays: GSPro never logs it.
A shot the screen misnamed is moved to the file's player once its score line and the file agree
(`_file_rename`), but the live call already aired (and its prediction was graded) under the screen's
name: the shooter isn't in the log until the ball stops.

## Drop the screen's setup lie once the round file covers every setup

The lie a player sets up on comes from `currentRound.dat` (`Engine._file_rest_lie`: the player's last
stroke's EndingSurface), which outranks the screen's word. The screen read
(and `_reread_setup_lie`'s retries) and the log's first-bounce material (`GsproState.lie_by`,
`_setup_log_lies`) remain as fallbacks for a team ball (scramble, alternate shot), a penalty or drop,
the first setup before the watcher has the stroke, a ball that ended on the cart path (free relief may
have moved it; the stroke's own StartingSurface is recorded once it's in the file), and an EndingSurface
code that names no lie. `round_file.SURFACES` maps every code GSPro has: the index into its
`Trajectory.PhyMatList` (0-22, from the game code), semi rough (12) and pine straw (16) included.
Only 0 (the ball's own material) and 19 (`none`: GSPro found
no surface under the ball) fall back to the screen. Codes 6 (earth), 7 (dropzone), 13 (stone) and
17 (leaves) take plain words the booth had none for (dirt, drop zone, stone, leaves); dirt, stone and
leaves have pre-written result lines (`flight_pick._LIE_REACTION`), a drop zone is left to the model.
Only 1, 2, 3, 5, 11 and 18 have been seen in a real file (the 2026-11-18 rounds in
`tests/fixtures/round_files/`).

**Needs verification:** which setups still take the screen's lie. Every off-the-tee setup now logs its
lie source (`Setup lie ... from GSPro's round file (the screen agreed | the screen read ...)`, `... from
the screen (GSPro's round file: <why not>)`, `... from GSPro's log`), and the round ends with a
`Round N: M setup lies from: ...` count. Offline, Evan and Jason's stroke-play round (2026-11-19) had
the file agree with the screen on all 121 setups it covered; the 8 it didn't were after a conceded
tap-in (holed). It had out-of-bounds and water re-tees but no drop, cart path or team ball. Still to
check: one round with drops and cart-path finishes, and one scramble or alternate-shot round (team
balls, which only those formats have). If the gaps are only team balls, map those from the file too (the pick's stroke) and
drop the screen read and the log's `lie_by` fallback.

## Drop the scorecard and result-banner OCR once the log names every player

Scores come from GSPro's log (`Round.log_scores`). The between-hole scorecard OCR is skipped while
`Engine._log_scores_cover` holds (a followed log, every player of the live round in a slot the log
can be tied to with a score on every earlier hole, and every hole's par and stroke index known);
a score line on the hole in play queues `ScorecardEvent(card=None)` (`_note_log_scores`) for the
wrap. A putt the log credits to a slot the booth can't name takes its result from that slot's score
line (`_log_shooter`). A pick-up's score is final on the conceding stroke GSPro logs with the capped score, so the card
isn't read after one (it was, three times, in Evan and Jason's round on 2026-11-19, waiting for the
next tee shot to settle it). The OCR fallbacks left: the scorecard while a slot is unnamed, a score line is missing, the
course card has no stroke index, a player the
round doesn't have is in the log (`_admit_from_card`), or the first hole is assumed; the idle card
read with no round (mid-round join, `_note_idle_card`); and the result banner (`_hole_result`) for a
putt the log never credits (GSPro 3.59 practice, no log). Once the slot naming (see "Retire the
setup-screen slot order") covers every round, check booth logs for `Round ... scorecard:` lines in
covered rounds and remove the in-round card read and the banner.

## Find why the round file's club disagrees with the screen's

The club read off the screen before the shot is the club hit. GSPro's `ClubIndex` only fills one the
screen read none for (`round_file.club_fix`, applied by `Store._apply_track`, and live in
`handle_shot` except on a stroke at rest on the green, which the hole may yet end on), and never on a
holed or conceded stroke (`round_file.file_club`; one it filled before the hole ended on the stroke
goes back). A shot left with no club, read or filled, takes GSPro's `isPutt` (its putting mode at
impact) for putt or not, reversibly with the stroke's pin. GSPro writes
`ClubIndex = mGC.clubsel.clubSelection` when it records the stroke at rest, not at impact
(decompiled `PlayerShotHistory.cs:79`), and sets the selection to the putter (26) on a hole-out, so a
chip-in reads PT (the 2026-11-18 scramble's hole 4: Corey's lob-wedge chip-in). So the file can't
replace the club box OCR. Against the booth's club on 2026-11-18 (its `impact HUD ... club` line,
logged after `hud.deck_club` turns a driver read off the deck into a 3-wood) the file matched 28 of 28
strokes in a single-player round but 49 of 52 in a scramble: the chip-in, and hole 7's second shots
(both players, off the fairway, 124.9 and 150.7 mph), which the booth has as 3-woods and the file as
the driver. Evan and Jason's stroke-play round (2026-11-19) matched on 165 of 167 strokes; the two
misses were holed chips the file has as PT. Whether the screen itself showed the driver there (the deck rule's case: newer GSPro
shows the driver for a long shot off the deck) or a 3-wood is unverified, as is why the file has a
3-wood off the deck in the single-player round (holes 4 and 7, from the fairway and the rough). The index-to-club table
(`round_file.CLUBS`) is GSPro 4.x's own (`ClubSelect`'s DR, W2-W7, H2-H7, I1-I9, PW, GW, SW, LW, PT,
None). Keep the raw club box read for a few rounds (before the deck rule) to settle it, and find what
moves the selection between impact and rest, before trusting the file's club anywhere the screen
read one.

## Check the scramble pick against a real scramble round file

The pick screen's OCR is gone: a scramble's unpicked balls take their finish and lie from
`currentRound.dat` (`Engine._file_pick_cards`). One real scramble file has been seen (2026-11-18, in
`tests/fixtures/round_files/`): every teammate's ball is there, and a hole-out adds a copy of the
holing ball for each teammate (`gspro_files.holed_copies`). It can't say when each was written, so check
that GSPro writes every teammate's ball before the pick screen shows. A ball missing when the screen
goes down gets no finish. On 3.59 (no `LaunchExt`), the tie relies on `_file_pins` only.
