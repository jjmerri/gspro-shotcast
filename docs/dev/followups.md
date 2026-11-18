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
