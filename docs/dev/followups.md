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

