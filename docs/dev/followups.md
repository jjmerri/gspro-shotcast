# Follow-ups

Deferred work, newest last. Take an entry out when it's done.

## Remove the OCR ball follow once currentRound.dat has proven itself

OCR ball-stop/follow detection is kept as a fallback behind currentRound.dat. After a session or two,
compare the new INFO timing logs (file vs log vs OCR rest). If the file is never later than OCR, remove
the OCR ball follow.

The lines to compare (from `commentator/engine.py`, one set per full shot):

- `<tag> rest latency: GSPro's round file X.XXs after impact; its log Y.YYs (+/-Z.ZZs file vs log); the
  screen W.WWs (+/-V.VVs file vs screen)`. This is the main one: the file should never be behind. The
  screen part says `stopped by the file` when the file ended the follow first, and `none (the follow
  ended at ...: <why>)` when the follow stopped for some other reason (the log's rest, the hole's end,
  its time cap), which gives no screen timing.
- `<tag> ball at rest in GSPro's round file X.XXs after impact ...`: the file ended the follow (or came
  within the grace window after it).
- `<tag> no stroke in GSPro's round file 0.30s after the follow ended (...): the screen's read stands`:
  the file missed the grace window. Any of these is a reason to keep the OCR follow.
- At startup, `Watching ...currentRound.dat with change notifications`. If it says `No change
  notifications for ...: checking it every 1 s`, or later `Lost change notifications ...`, the timings
  above are not the fast path's.


## Retire the setup-screen slot order once the round file names every slot

Player slots are now named from GSPro's log plus `currentRound.dat` (launch numbers tie a credited
shot to the file's `PlayerName`; `_learn_file_slots` in `commentator/engine.py`). The setup screen's
player order (`setup_slots`) and the HUD-name slot reads (`Round.slot_read`) are kept as the fallback
for the first shot of a round, before the file has a ball to match, and for GSPro 3.59, which logs no
`LaunchExt Vars`. Once 3.59 support is dropped, check the booth logs for `GSPro's round file places the players`
lines and, if the file always names every slot by the second shot, remove the screen-read slot
learning (`slot_read`'s read counts, `_recheck_slots`) and keep the setup screen only for format and
nines. The pre-shot "who's up" read stays: GSPro never logs it.

## Drop the screen's setup lie once the round file covers every setup

The lie a player sets up on comes from `currentRound.dat` (`Engine._file_rest_lie`: the player's last
stroke's EndingSurface), with the screen's finer word kept (`round_file.finer_lie`). The screen read
(and `_reread_setup_lie`'s retries) and the log's first-bounce material (`GsproState.lie_by`,
`_setup_log_lies`) remain as fallbacks for a team ball (scramble, alternate shot), a penalty or drop,
the first setup before the watcher has the stroke, and an unknown EndingSurface code (only 1, 2, 3, 4,
5, 11 and 18 are mapped in `round_file.SURFACES`). Check the booth logs for `Setup lie ... from GSPro's
round file` against setups without it; if the gaps are only team balls, map those from the file too
(the pick's stroke) and drop the log's `lie_by` fallback.

## Drop the scorecard and result-banner OCR once the log names every player

Scores come from GSPro's log (`Round.log_scores`). The between-hole scorecard OCR is skipped while
`Engine._log_scores_cover` holds (a followed log, every player of the live round in a slot the log
can be tied to with a score on every earlier hole, and every hole's par and stroke index known);
a score line on the hole in play queues `ScorecardEvent(card=None)` (`_note_log_scores`) for the
wrap. A putt the log credits to a slot the booth can't name takes its result from that slot's score
line (`_log_shooter`). The OCR fallbacks left: the scorecard while a slot is unnamed, a score line is missing, the
course card has no stroke index, a player the
round doesn't have is in the log (`_admit_from_card`), or the first hole is assumed; the idle card
read with no round (mid-round join, `_note_idle_card`); and the result banner (`_hole_result`) for a
putt the log never credits (GSPro 3.59 practice, no log). Once the slot naming (see "Retire the
setup-screen slot order") covers every round, check booth logs for `Round ... scorecard:` lines in
covered rounds and remove the in-round card read and the banner.

## Check the round file's club against the screen's, then trust it outright

GSPro's `ClubIndex` corrects the club the screen read before the shot (`round_file.club_fix`, applied
live in `handle_shot` and by `Store._apply_track`). The index-to-club table (`round_file.CLUBS`) is
GSPro 4.x's own (`ClubSelect`'s DR, W2-W7, H2-H7, I1-I9, PW, GW, SW, LW, PT, None) and matched one real
4.x round (driver off tees, putter on greens, 5-wood and wedges where expected); it hasn't been checked
on 3.59. Count the `club ... from GSPro's round file` log lines over a few nights: if the screen's
club is only ever the one corrected, the club box OCR can be kept just for the pre-shot call (the
only place the file can't help), and the driver-off-the-deck rule (`hud.deck_club`) rechecked
against what the file records there.

## Check the scramble pick against a real scramble round file

The pick screen's OCR is gone: a scramble's unpicked balls take their finish and lie from
`currentRound.dat` (`Engine._file_pick_cards`). No real scramble file has been seen yet, so check
that GSPro writes every teammate's ball before the pick screen shows. A ball missing when the screen
goes down gets no finish. On 3.59 (no `LaunchExt`), the tie relies on `_file_pins` only.
