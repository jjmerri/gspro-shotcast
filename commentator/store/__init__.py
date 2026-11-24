"""Season history in SQLite: every shot and every line, kept across league nights.

One file, no server. Open it with any SQLite viewer (e.g. DB Browser for SQLite) to browse or
export. Replays and evals use an in-memory database so made-up shots never reach the season.

The season's SQLite file. `Store` (core.py) is put together from one part per job (the other files
here); common.py holds what they share. Everything the old single store.py defined is still
importable from here under the same name."""
from .common import (  # noqa: F401
    CLOSER_SECONDS, CLUB_COLS, COUNTS_AS_ROUND_HOLES, DRIVE_MIN_TO_PIN,
    DRIVE_SQL, DROP_CARRY_TRIGGERS, FORMAT, HAZARD_COLS, INDEXES, POSITION_COLS, SCHEMA, SeasonCopyFailed, UNKNOWN_CLUB_DRIVE_SQL,
    _COUNTS_AS_ROUND, _DRIVE, _GROSS_PER_NINE, _NET_PER_NINE,
    _PARS_COMPLETE, _TEE_SHOT, _club_values, _ctx_sql, _median, _position_values, is_drive, log,
    planner_counts_stale,
)
from .core import Store  # noqa: F401
