"""The GSPro output-log parser and state reducer, exercised against a real captured excerpt.

tests/data/gspro_output_log_sample.txt is a trimmed slice of a genuine output_log.txt: a course
load, the 18-hole par layout, and several holes of play including a water-ball penalty and a
hole-out.
"""

from commentator.sources.gspro_log import (
    GsproLogFollower, GsproState, LogEvent, _split_trailing, aim_offset, build_state, parse_events,
)

SAMPLE = "tests/data/gspro_output_log_sample.txt"


def _text():
    return open(SAMPLE, errors="replace").read()


def test_parser_ignores_unity_noise_and_extracts_events():
    kinds = {e.kind for e in parse_events(_text())}
    # The signal kinds we rely on are all present; the "(Filename:)" / Spline noise is dropped.
    assert {"state", "shot_fired", "material", "tdist", "course", "layout", "hole_out"} <= kinds


def test_course_and_par_layout_are_read():
    s = build_state(_text())
    assert s.course == "paynes_valley_gsp"
    assert len(s.pars) == 18 and all(3 <= p <= 5 for p in s.pars)


def test_hole_out_counter_advances_on_a_holed_shot():
    assert build_state(_text()).hole_outs >= 1


def test_lie_and_distance_track_the_latest_shot():
    s = build_state(_text())
    assert s.lie in {"fairway", "green", "rough", "sand", "water", "tee", "deeprough", "semirough", "fringe"}
    assert s.to_pin is not None and s.to_pin >= 0


def test_a_water_ball_is_counted_as_a_penalty():
    # In the fixture, player 0 finds the water on hole index 3: Previous Score steps 1 -> 2 with no
    # shot fired between, so one penalty is counted and the stroke count reflects it.
    s = build_state(_text())
    assert s.penalties(0) >= 1 or any(s._penalties.values())


def _events_to_state(evs):
    s = GsproState()
    for e in evs:
        s.apply(e)
    return s


def test_penalty_rule_distinguishes_strokes_gimmies_and_penalties():
    # Hole 7 (index 6), player 0: tee shot (score 1), approach into water (score 2 then a penalty
    # bump to 3), recovery (4), then a conceded gimmie (5). Only the water bump is a penalty.
    evs = [
        LogEvent("state", player=0, hole=6, score=0),
        LogEvent("shot_fired"),
        LogEvent("state", player=0, hole=6, score=1),   # tee shot: real stroke
        LogEvent("shot_fired"),
        LogEvent("material", surface="water"),
        LogEvent("state", player=0, hole=6, score=2),   # approach landed, scored
        LogEvent("state", player=0, hole=6, score=3),   # penalty bump, no shot between
        LogEvent("shot_fired"),
        LogEvent("state", player=0, hole=6, score=4),   # recovery: real stroke
        LogEvent("gimmie"),
        LogEvent("state", player=0, hole=6, score=5),   # conceded putt, not a penalty
    ]
    s = _events_to_state(evs)
    assert s.penalties(0) == 1
    assert s.strokes_before(0) == 5
    assert s.shot_number(0) == 6


def test_counts_are_per_player_and_per_hole():
    evs = [
        LogEvent("state", player=0, hole=2, score=0),
        LogEvent("shot_fired"),
        LogEvent("state", player=0, hole=2, score=1),
        LogEvent("state", player=1, hole=2, score=0),   # a different player, same hole
    ]
    s = _events_to_state(evs)
    assert s.strokes_before(0) == 1
    assert s.strokes_before(1) == 0
    # Advancing to a new hole resets the active player's count.
    s.apply(LogEvent("state", player=0, hole=3, score=0))
    assert s.strokes_before(0) == 0


def test_per_index_lie_and_distance_stay_separate_per_player():
    # The crux of 2-ball attribution: each player's setup block (material -> state -> tdist) names its
    # player, so one player's data never clobbers the other's (the old global 'latest' bug).
    s = GsproState()
    s.apply(LogEvent("material", surface="fairway"))
    s.apply(LogEvent("state", player=0, hole=0, score=1))
    s.apply(LogEvent("tdist", to_pin=150.0, gimmie=1.3))
    s.apply(LogEvent("material", surface="sand"))
    s.apply(LogEvent("state", player=1, hole=0, score=1))
    s.apply(LogEvent("tdist", to_pin=40.0, gimmie=1.3))
    # tdist is metres; the accessors return yards (so a stale cross-attribution can't happen).
    assert s.lie_of(0) == "fairway" and abs(s.to_pin_of(0) - 150.0 * 1.0936133) < 0.1
    assert s.lie_of(1) == "sand" and abs(s.to_pin_of(1) - 40.0 * 1.0936133) < 0.1


def test_two_ball_interleaving_does_not_manufacture_a_penalty():
    # The partner's state lines interleave with this player's; the penalty flags are per index, so a
    # normal second stroke is never misread as a water-drop penalty.
    s = GsproState()
    s.apply(LogEvent("select", player=0)); s.apply(LogEvent("shot_fired"))
    s.apply(LogEvent("state", player=0, hole=0, score=1))
    s.apply(LogEvent("select", player=1)); s.apply(LogEvent("shot_fired"))
    s.apply(LogEvent("state", player=1, hole=0, score=1))
    s.apply(LogEvent("select", player=0)); s.apply(LogEvent("shot_fired"))
    s.apply(LogEvent("state", player=0, hole=0, score=2))   # P0's real 2nd stroke, not a penalty
    assert s.penalties(0) == 0 and s.penalties(1) == 0
    assert s.strokes_before(0) == 2 and s.strokes_before(1) == 1


def test_strokes_use_each_players_own_hole_not_the_global_one():
    # P0 moves to the next hole while P1 is still on the previous one; the partner's state line reverts
    # the global current_hole, but each player's count is read against their own hole.
    s = GsproState()
    s.apply(LogEvent("select", player=0)); s.apply(LogEvent("shot_fired"))
    s.apply(LogEvent("state", player=0, hole=0, score=3))
    s.apply(LogEvent("state", player=0, hole=1, score=0))   # P0 tees the next hole
    s.apply(LogEvent("state", player=1, hole=0, score=2))   # P1 still on the previous hole
    assert s.shot_number(0) == 1          # P0 is on a fresh hole, not hole 0's total + 1
    assert s.strokes_before(1) == 2


def test_a_penalty_drops_the_stale_lie_so_the_drop_shot_falls_back_to_ocr():
    s = GsproState()
    s.apply(LogEvent("material", surface="water"))
    s.apply(LogEvent("state", player=0, hole=0, score=1))
    s.apply(LogEvent("state", player=0, hole=0, score=2))   # penalty bump, no shot between
    assert s.penalties(0) == 1
    assert s.lie_of(0) is None        # 'water' forgotten; the drop's lie isn't logged


def test_multiplayer_flag_tracks_distinct_players_and_resets_on_a_new_course():
    s = GsproState()
    assert not s.multiplayer
    s.apply(LogEvent("state", player=0, hole=0, score=0))
    assert not s.multiplayer                       # one player so far
    s.apply(LogEvent("select", player=1))
    assert s.multiplayer                           # a second player took the clock
    s.apply(LogEvent("course", course="other_gsp"))
    assert not s.multiplayer                        # a new round clears the set


def test_hole_out_and_gimmie_each_have_their_own_counter():
    s = GsproState()
    for e in [LogEvent("gimmie"), LogEvent("hole_out"), LogEvent("gimmie")]:
        s.apply(e)
    assert s.gimmies == 2 and s.hole_outs == 1


def test_split_trailing_holds_a_partial_line():
    assert _split_trailing("a\nb\n") == ("", "a\nb\n")
    assert _split_trailing("a\nb") == ("b", "a\n")      # partial "b" carried over
    assert _split_trailing("abc") == ("abc", "")         # nothing complete yet


def test_follower_reads_only_appended_bytes_and_resets_on_truncation(tmp_path):
    p = tmp_path / "output_log.txt"
    p.write_text("old history we should skip\n")
    f = GsproLogFollower(str(p), GsproState())
    f._offset = p.stat().st_size                         # run() starts at EOF; emulate that
    # New shot lines appended after we started are parsed.
    with p.open("a") as fh:
        fh.write("ActivePlayer: 0  currentHole: 1  strokes: 1  Previous Score: 2\n")
        fh.write("Using Material TVGsand\n")
    for ev in parse_events(f._read_new()):
        f.state.apply(ev)
    assert f.state.strokes_before(0) == 3 and f.state.lie == "sand"   # 1 stroke added to 2
    # A GSPro restart rewrites the file smaller; the follower resets and reads from the top.
    p.write_text("ActivePlayer: 1  currentHole: 0  strokes: 1  Previous Score: 1\n")
    for ev in parse_events(f._read_new()):
        f.state.apply(ev)
    assert f.state.strokes_before(1) == 2 and f.state.current_hole == 0


def test_hit_water_sets_and_clears_the_wet_flag():
    # The splash is logged (Hit_water, and "| material water |" on the bounce line) the instant the
    # ball goes in, before the penalty score bump. It drives the booth's instant water reaction.
    s = GsproState()
    for ev in parse_events(
        "ActivePlayer: 0  currentHole: 1  strokes: 1  Previous Score: 0\n"
        "Selecting shot for player: 0\n"
        " * ------- Shot Fired! ------- *\n"
        "magnitude: 5.8 | yForce 3.2 | material water | SoundMultiplier 0.1\n"
        "Hit_water\n"
    ):
        s.apply(ev)
    assert s.wet(0) is True        # seen the instant the ball goes in
    for ev in parse_events("ActivePlayer: 0  currentHole: 1  strokes: 1  Previous Score: 1\n"):
        s.apply(ev)                # the shooter's score line credits the splash
    assert s.wet(0) is True
    assert s.wet(1) is False       # never attributed to the partner
    # The next shot (from the drop) is dry: the flag clears when that player fires again.
    for ev in parse_events(" * ------- Shot Fired! ------- *\n"):
        s.apply(ev)
    assert s.wet(0) is False


def test_older_tvgwater_material_also_sets_the_wet_flag():
    # Older GSPro flags a splash only as `Using Material TVGwater` (no Hit_water line); it must still
    # set the wet flag, so the water reaction works across GSPro versions.
    s = GsproState()
    for ev in parse_events(
        "ActivePlayer: 0  currentHole: 1  strokes: 1  Previous Score: 0\n"
        "Selecting shot for player: 0\n"
        " * ------- Shot Fired! ------- *\n"
        "Using Material TVGwater\n"
    ):
        s.apply(ev)
    assert s.wet(0) is True


def test_wet_since_gates_a_stale_splash():
    # The in-flight tease passes `since` (this shot's impact time) so a splash from an EARLIER shot,
    # not yet cleared, can't tease the current (dry) shot while it is still in the air.
    s = GsproState()
    for ev in parse_events(
        "ActivePlayer: 0  currentHole: 1  strokes: 1  Previous Score: 0\n"
        "Selecting shot for player: 0\n"
        " * ------- Shot Fired! ------- *\n"
        "Hit_water\n"
    ):
        s.apply(ev)
    stamp = s._unowned_wet
    assert s.wet(0) is True                       # no `since`: still wet
    assert s.wet(0, since=stamp - 1.0) is True     # splash is at/after the gate
    assert s.wet(0, since=stamp + 1.0) is False    # splash predates the gate: stale, don't fire


def test_wet_flag_clears_on_new_hole_and_same_course_restart():
    def wet_after(tail: str) -> bool:
        s = GsproState()
        for ev in parse_events(
            "ActivePlayer: 0  currentHole: 1  strokes: 1  Previous Score: 1\n"
            "Selecting shot for player: 0\n"
            " * ------- Shot Fired! ------- *\n"
            "Hit_water\n" + tail
        ):
            s.apply(ev)
        return s.wet(0)

    # Moving to the next hole ends the previous hole's splash.
    assert wet_after("ActivePlayer: 0  currentHole: 1  strokes: 1  Previous Score: 2\n"
                     "ActivePlayer: 0  currentHole: 2  strokes: 1  Previous Score: 0\n") is False
    # Replaying the same hole (score decreases) starts dry too.
    assert wet_after("ActivePlayer: 0  currentHole: 1  strokes: 1  Previous Score: 0\n") is False


def test_splash_before_the_first_state_line_is_not_cleared():
    # The follower starts at EOF, so the first state line it sees for a player can arrive AFTER the
    # splash (active_player is set by the preceding "Selecting shot for player" line). That first state
    # line, where hole_by still defaults to None, must not be read as a hole change that wipes the flag.
    s = GsproState()
    for ev in parse_events(
        "Selecting shot for player: 0\n"
        " * ------- Shot Fired! ------- *\n"
        "Hit_water\n"
        "ActivePlayer: 0  currentHole: 2  strokes: 1  Previous Score: 0\n"
    ):
        s.apply(ev)
    assert s.wet(0) is True


def test_new_surface_materials_map_to_their_lies():
    from commentator.sources.gspro_log import MATERIAL_TO_LIE

    assert MATERIAL_TO_LIE["concrete"] == "cart path"
    assert MATERIAL_TO_LIE["wood"] == "structure"
    assert MATERIAL_TO_LIE["timber"] == "structure"
    assert MATERIAL_TO_LIE["pinestraw"] == "pine straw"
    # Read end to end: a material event followed by the player's setup block attaches the lie.
    s = GsproState()
    for ev in parse_events(
        "Using Material TVGconcrete\n"
        "ActivePlayer: 0  currentHole: 2  strokes: 1  Previous Score: 0\n"
    ):
        s.apply(ev)
    assert s.lie_of(0) == "cart path"


def test_range_round_end_and_completion_are_tracked():
    s = GsproState()
    for ev in parse_events(
        "ResetCriticalRoundVaraibles\nGreenKeeper Course Parse\nGSPro Multi Range\n"
        "RangeLevelLoaded()\nLoading course Practice\n"
    ):
        s.apply(ev)
    assert s.mode == "range"
    for ev in parse_events("End Round Button Click\nSelected Course: riviera_gsp\nLoading course Riviera\n"):
        s.apply(ev)
    assert s.mode == "course" and s.round_ends == 1
    for ev in parse_events(
        "ActivePlayer: 0  currentHole: 3  strokes: 1  Previous Score: 2\n"
        "Round Is Complete\n"
        '[{"active":false,"played":false,"par":5},{"active":true,"played":true,"par":4},'
        '{"active":true,"played":true,"par":3}]\n'
    ):
        s.apply(ev)
    assert s.round_completes == 1 and s.active_holes == 2 and s.pars == [5, 4, 3]
    for ev in parse_events("End Round Button Click\n"):
        s.apply(ev)
    # Leaving the round resets the per-round state, so a new round on the same course starts clean.
    assert s.mode == "menu" and s.round_ends == 2 and s.strokes_before(0) == 0


def test_follower_learns_from_the_backlog_that_gspro_is_on_the_range(tmp_path):
    path = tmp_path / "output_log.txt"
    path.write_text("Selected Course: riviera_gsp\nEnd Round Button Click\nGSPro Multi Range\nRangeLevelLoaded()\n")
    f = GsproLogFollower(str(path))
    f._prime()
    assert f.state.mode == "range" and f._offset == path.stat().st_size
    assert f.state.round_ends == 0   # the backlog's history isn't replayed, only where GSPro is now


def test_follower_learns_from_the_backlog_whether_play_has_begun(tmp_path):
    path = tmp_path / "output_log.txt"
    path.write_text("Selected Course: riviera_gsp\nLoading course Riviera\n")
    f = GsproLogFollower(str(path))
    f._prime()
    assert f.state.mode == "course" and f.state.shots_since_course == 0   # still on the opening tee
    path.write_text("Selected Course: riviera_gsp\n"
                    " * ------- Shot Fired! ------- *\nActivePlayer: 0  currentHole: 9  strokes: 1  Previous Score: 0\n"
                    " * ------- Shot Fired! ------- *\nActivePlayer: 0  currentHole: 11  strokes: 1  Previous Score: 0\n")
    f = GsproLogFollower(str(path))
    f._prime()
    assert f.state.shots_since_course == 2 and f.state.first_hole == 9 and f.state.current_hole == 11
    path.write_text(path.read_text() + "End Round Button Click\n")
    f = GsproLogFollower(str(path))
    f._prime()
    assert f.state.mode == "menu" and f.state.shots_since_course == 0


def test_skipping_hole_one_marks_a_late_start_until_the_next_round():
    s = GsproState()
    for ev in parse_events("Selected Course: paynes_valley_gsp\nLoading course Paynes Valley\nSkipping hole 1\n"):
        s.apply(ev)
    assert s.starts_late
    for ev in parse_events("End Round Button Click\nSelected Course: paynes_valley_gsp\n"):
        s.apply(ev)
    assert not s.starts_late


def test_the_altitude_gspro_plays_at_is_read_from_a_bounce_until_the_course_changes():
    s = GsproState()
    bounce = ("magnitude: 10.34195 | yForce 4.88229 | material TVGfairway | SoundMultiplier 0.3370993 | "
              "SoundForceScale 0.6741985 | elevation 2080\r\n")
    for ev in parse_events("Selected Course: wynn_gsp\n" + bounce):
        s.apply(ev)
    assert s.altitude_ft == 2080.0
    for ev in parse_events("Selected Course: cabotcliffs_gsp\n"):
        s.apply(ev)
    assert s.altitude_ft is None


def test_putting_mode_is_read_per_shot_fired():
    import time
    text = ("* ------- Shot Fired! ------- *\nLaunchExt Vars: sp 3.42, el 0\nLaunchExt - Putting Mode\n"
            "* ------- Shot Fired! ------- *\nLaunchExt - Regular Mode\n")
    s = GsproState()
    assert not s.logs_launch_mode
    impact = time.monotonic()
    base = s.fires
    assert s.putting_mode(base, impact) is None          # not logged yet
    evs = parse_events(text)
    assert [e.putting for e in evs if e.kind == "launch_mode"] == [True, False]
    for e in evs[:2]:
        s.apply(e)
    assert s.logs_launch_mode and s.putting_mode(base, impact) is True    # the fire after impact
    assert s.putting_mode(s.fires, impact) is True       # already tailed when impact was snapshotted
    for e in evs[2:]:
        s.apply(e)
    s._fire_at[1] -= 20                                   # the putt was 20 s ago
    assert s.putting_mode(1, time.monotonic()) is False  # so the shot after it, played as a full shot


def _replay(text):
    s = GsproState()
    for e in parse_events(text):
        s.apply(e)
    return s


FIRE = "Selecting shot for player: 0\n* ------- Shot Fired! ------- *\n"


def _line(hole, strokes, prev, player=0):
    return f"ActivePlayer: {player}  currentHole: {hole}  strokes: {strokes}  Previous Score: {prev}\n"


def test_the_score_line_adds_its_strokes_so_the_next_stroke_is_right():
    # Captured order: the tee shot settles, then "strokes: 1  Previous Score: 0". The player has 1, so
    # the next stroke is the 2nd (reading Previous Score alone left every stroke number one behind).
    s = _replay(FIRE + _line(4, 1, 0))
    assert s.strokes_before(0) == 1 and s.shot_number(0) == 2


def test_a_mulligan_with_a_negative_line_is_flagged_and_its_penalty_dropped():
    # Real log (Paynes Valley): tee shot onto the cart path, the OB penalty, then both taken back.
    s = _replay(FIRE + _line(8, 1, 0) + _line(8, 1, 1) + _line(8, -2, 2) + FIRE + _line(8, 1, 0))
    assert s.mulligan_fires == {1: "undo"} and s.penalties(0) == 0 and s.shot_number(0) == 2 and not s.pickups


def test_a_mulligan_with_no_negative_line_is_flagged_from_the_repeated_score():
    # Real log (Cabot Cliffs 12): the tee shot, then the re-hit's line repeats "Previous Score: 0".
    s = _replay(FIRE + _line(11, 1, 0) + FIRE + _line(11, 1, 0) + FIRE + _line(11, 1, 1))
    assert s.mulligan_fires == {1: "repeat"} and s.shot_number(0) == 3


def test_a_pick_up_caps_the_hole_without_counting_penalties():
    # Real log (Cabot Cliffs 11, par 4): four strokes, then GSPro takes them back, writes 7 and adds
    # the conceding stroke: 8, a quad bogey. Not seven penalty strokes.
    text = FIRE + _line(10, 1, 0) + "".join(FIRE + _line(10, 1, n) for n in (1, 2, 3))
    s = _replay(text + _line(10, -4, 4) + _line(10, 7, 0) + _line(10, 1, 7))
    assert s.strokes_before(0) == 8 and s.penalties(0) == 0
    assert s.pickups == [(0, 10)] and s.picked_up(0, 10) and not s.mulligan_fires


def test_penalty_surface_is_kept_per_shot():
    import time
    s = _replay(FIRE + "LaunchExt - Regular Mode\nPenalty Surface TVGsand, Spin71%, Speed 78%, VLA 100%\n"
                + _line(1, 1, 1))
    assert s.fire_info(s.fire_of(s.fires, time.monotonic())) == {"player": 0, "hole": 1, "putting": False, "penalty": "sand",
                                                                  "lie_cost": (71, 78)}


def test_a_repeated_score_in_a_team_format_is_a_mulligan_only_when_it_repeats_the_players_last_shot():
    # Real log (Bethpage 4, scramble): Dan tees off twice before Derek; the first was taken back.
    text = "Standard Teammate Result Copy\n" + FIRE + _line(3, 1, 0) + FIRE + _line(3, 1, 0)
    assert _replay(text).mulligan_fires == {1: "repeat"}
    # Kiawah 12: Dan's ball found a penalty area (a line a stroke ahead, with no shot), Derek's was
    # picked, and Dan's next stroke repeats the penalty's line. Not a do-over.
    text = ("Standard Teammate Result Copy\n" + FIRE + _line(11, 1, 1, player=1) + _line(11, 1, 2, player=1)
            + "Selecting shot for player: 0\n" + FIRE + _line(11, 1, 2, player=0) + FIRE + _line(11, 1, 2, player=1))
    assert not _replay(text).mulligan_fires


# GSPro hands the clock over with "Next Player" and no new select block, so the player last selected
# isn't always the one swinging; the shooter's own score line follows each shot. (Paynes Valley, 2-ball)
NEXT = "Putt Out Mode\nNext Player\n"


def test_next_player_shots_and_hole_outs_go_to_the_shooter():
    s = _replay(
        _line(2, 1, 0, player=0) + "Selecting shot for player: 0\n" + NEXT
        + "* ------- Shot Fired! ------- *\nHole out !!! \n"      # player 1 holes out
        + _line(2, 1, 2, player=1)
    )
    assert s.hole_outs_of(1) == 1 and s.hole_outs_of(0) == 0
    assert s.fire_info(s.fires)["player"] == 1


def test_a_mulligan_after_a_handover_flags_the_shooters_own_swing():
    # Wes putts out (gimme) after Toby was last selected; Toby tees off, takes a mulligan (the repeated
    # score line) and re-hits. The mulligan is Toby's tee shot, not Wes's putt.
    s = _replay(
        _line(2, 1, 2, player=0) + "Selecting shot for player: 0\n" + NEXT
        + "* ------- Shot Fired! ------- *\n"                             # 1: Wes's putt
        + _line(2, 1, 2, player=1) + "Selecting shot for player: 1\n"
        + "Select Shot - active user ball is within Gimmie distance\n" + _line(2, 1, 3, player=1)
        + "* ------- Shot Fired! ------- *\n"                             # 2: Toby's tee shot
        + _line(3, 1, 0, player=0) + "Selecting shot for player: 0\n"
        + "* ------- Shot Fired! ------- *\n"                             # 3: the re-hit
        + _line(3, 1, 0, player=0)
    )
    assert s.mulligan_fires == {2: "repeat"}
    assert s.penalties(1) == 0 and s.penalties(0) == 0


def test_the_rounds_first_shot_is_credited_to_its_shooter():
    # The opening tee shot comes before any select block; its mulligan must still be found.
    s = _replay("* ------- Shot Fired! ------- *\n" + _line(0, 1, 0)
                + "Selecting shot for player: 0\n* ------- Shot Fired! ------- *\n" + _line(0, 1, 0))
    assert s.mulligan_fires == {1: "repeat"}


def test_material_records_where_each_shot_landed():
    s = GsproState()
    for line in ["ActivePlayer: 0  currentHole: 12  strokes: 1  Previous Score: 0",
                 " * ------- Shot Fired! ------- *", "LaunchExt - Regular Mode", "Using Material TVGconcrete",
                 "ActivePlayer: 0  currentHole: 12  strokes: 1  Previous Score: 1"]:
        for ev in parse_events(line):
            s.apply(ev)
    assert s.fire_info(s.fires)["landed"] == "concrete"


def test_a_kick_off_the_cart_path_is_a_fact():
    import asyncio
    from types import SimpleNamespace
    from commentator.engine import Engine

    s = GsproState()
    for line in [" * ------- Shot Fired! ------- *", "Using Material TVGconcrete"]:
        for ev in parse_events(line):
            s.apply(ev)
    me = SimpleNamespace(gspro=s)
    facts = asyncio.run(Engine._kick_facts(me, s.fires, "rough", False, wait=0))
    assert facts and "cart path" in facts[0] and "rough" in facts[0]
    assert asyncio.run(Engine._kick_facts(me, s.fires, "cart path", False, wait=0)) == []
    assert asyncio.run(Engine._kick_facts(me, s.fires, "rough", True, wait=0)) == []


def test_a_tree_hit_is_logged_on_its_shot_and_called_by_its_height():
    from types import SimpleNamespace
    from commentator.engine import Engine
    from commentator.models import Shot

    # Dan's approach on Tobacco Road 9, then a topped ball that ran into a bush. The roll line can
    # come glued to a garbled one; it still marks the ball at rest.
    s = GsproState()
    for line in [" * ------- Shot Fired! ------- *", "LaunchExt - Regular Mode",
                 " ** Leaves hitTime, 1.53 , HitCount 1, Penalty 33.9% , Accumulated penalty 33.9% ",
                 "Using Material TVGdeeprough", "LM Type \ua212Roll dist ft9.689",
                 " * ------- Shot Fired! ------- *", " * Leaves pas thru at hit 1",
                 " ** Leaves hitTime, 1.84 , HitCount 1, Penalty 18.2% , Accumulated penalty 18.2% ",
                 " ** Leaves hitTime, 2.10 , HitCount 2, Penalty 10.0% , Accumulated penalty 26.4% "]:
        for ev in parse_events(line):
            s.apply(ev)
    first, second = s.fire_info(s.fires - 1), s.fire_info(s.fires)
    assert first["leaves"] == [(1.53, 1, 33.9, 33.9)] and "rest_at" in first
    assert len(second["leaves"]) == 2 and second["leaves_pass"] == 1 and "rest_at" not in second

    me = SimpleNamespace(gspro=s, _raw_yards=Engine._raw_yards)
    me._foliage_fact = lambda *a: Engine._foliage_fact(me, *a)
    approach = Shot(ball_speed=112.15, vla=18.43, total_spin=2992, spin_axis=-1.69, carry=171.77)
    fact = Engine._tree_facts(me, s.fires - 1, approach, 0.0)[0]
    assert "high up in a tree" in fact and "once" in fact and "34%" in fact
    topped = Shot(ball_speed=122, vla=-5.9, total_spin=1500, spin_axis=0, carry=1)
    fact = Engine._tree_facts(me, s.fires, topped, 0.0)[0]
    assert "as it came down" in fact and "2 times" in fact and "26%" in fact
    assert Engine._tree_facts(me, s.fires, Shot(putter=True), 0.0) == []



def test_the_cup_lines_say_how_each_ball_met_the_cup():
    # From the sim (Josh, Owl's Nest): a lip-out on 8, a putt holed dead centre, one that caught the
    # edge on the way in, one that burned the edge with too much pace, and a chip off the flagstick.
    fired = " * ------- Shot Fired! ------- *"
    s = GsproState()
    shots = [
        ["LaunchExt - Putting Mode", "Distance to cup = 46mm, time:281763.3", "Cup capture Lip Out", "Roll dist ft37.991"],
        ["LaunchExt - Putting Mode", "Distance to cup = 12mm, time:2522.16", "Hole out !!! ",
         "Lip In Launched. Source = 2", "Roll dist ft11.431"],
        ["LaunchExt - Putting Mode", "Distance to cup = 47mm, time:2522.16", "Hole out !!! ",
         "Lip In Launched. Source = 2", "Roll dist ft4.8"],
        ["LaunchExt - Putting Mode", "Distance to cup = 52mm, time:1.0",
         "Cup capture no action. Ball speed to high or Lip out active", "Roll dist ft11.677"],
        ["LaunchExt - Regular Mode", "Cup hit off Center 1330.143", "Distance to cup = 28mm, time:263091.3",
         "Flag hit & no capture, time:263091.3", "Roll dist ft48.470"],
        ["LaunchExt - Regular Mode", "Cup hit off Center 1539.6", "Roll dist ft20.0"],
    ]
    for shot in shots:
        for line in [fired, *shot]:
            for ev in parse_events(line):
                s.apply(ev)
    first = s.fires - len(shots) + 1
    assert [s.cup_of(first + i) for i in range(len(shots))] == \
        ["lip_out", None, "lip_in", "lip_out", "flagstick", None]
    assert s.cup_of(None) is None


def test_wood_is_a_tree_unless_met_at_ground_level_where_the_ball_came_down():
    import asyncio
    from types import SimpleNamespace
    from commentator.engine import Engine
    from commentator.models import Shot

    def bounce(descent):
        return (f"Force 19.23|| SurfMul 1.41|| BallSpeed 38.32|| AngleSpeedBounce 0.62|| descent angle {descent}"
                f"|| ExitAngle 45.7")

    # From the sim: a trunk; leaves then wood coming down; wood met where the ball came down.
    s = GsproState()
    for line in [" * ------- Shot Fired! ------- *", bounce("0.0"), "Using Material TVGwood",
                 " * ------- Shot Fired! ------- *",
                 " ** Leaves hitTime, 1.05 , HitCount 1, Penalty 11.4% , Accumulated penalty 11.4% ",
                 bounce("36.8"), "Using Material TVGwood",
                 " * ------- Shot Fired! ------- *", bounce("0.0"), "Using Material TVGwood"]:
        for ev in parse_events(line):
            s.apply(ev)
    trunk, leafy, bridge = s.fires - 2, s.fires - 1, s.fires
    assert s.fire_info(trunk)["descent"] == 0.0
    assert s.tree_wood(trunk, 9.2, 53.1) and s.tree_wood(trunk, 1.8, 20.5)   # a trunk, rising or at its base
    assert s.tree_wood(leafy, 42.5, 71.8) and s.tree_wood(leafy)
    assert not s.tree_wood(bridge, 86.9, 97.8)   # descent 0.0 too, but met where it came down: steps
    assert s.tree_wood(trunk) and s.tree_wood(bridge, None, 97.8)   # no carry read: wood is taken as a tree

    me = SimpleNamespace(gspro=s, _raw_yards=Engine._raw_yards)
    me._foliage_fact = lambda *a: Engine._foliage_fact(me, *a)
    shot = Shot(ball_speed=54.8, vla=36.4, total_spin=8499, spin_axis=-6.1, carry=53.1)
    assert asyncio.run(Engine._kick_facts(me, trunk, "rough", False, wait=0, wood_tree=True)) == []
    assert "bridge" in asyncio.run(Engine._kick_facts(me, bridge, "rough", False, wait=0, wood_tree=False))[0]
    fact = Engine._tree_facts(me, trunk, shot, 0.0, "", True)
    assert len(fact) == 1 and "wood of a tree" in fact[0]
    facts = Engine._tree_facts(me, leafy, shot, 0.0, "", True)
    assert len(facts) == 2 and "then struck the wood" in facts[1]
    assert Engine._tree_facts(me, bridge, shot, 0.0) == []


SCRAMBLE = "tests/data/gspro_scramble_sample.txt"


def test_a_scramble_pick_is_read_and_the_dropped_ball_takes_the_picked_one():
    # A real two-player scramble hole: player 1's drive (fairway) was picked over player 0's (deep
    # rough), then player 0's approach (green) over player 1's (fairway), then a putt inside the gimme.
    s = GsproState()
    text = open(SCRAMBLE).read().splitlines(keepends=True)
    for ev in parse_events("".join(text[:24])):    # through the first pick
        s.apply(ev)
    assert s.team_scores and s.pick == (1, 0) and s.teammates == {frozenset((0, 1))}
    assert s.pick_stroke() == 1
    assert s.dropped_by == {0: 1}
    assert s.lie_of(0) == "fairway" and s.to_pin_by[0] == s.to_pin_by[1] == 125.4661
    for ev in parse_events("".join(text[24:])):
        s.apply(ev)
    assert s.pick == (0, 0) and s.dropped_by == {0: 1, 1: 2}
    # Each stroke's ball the team played on, the last one the putt GSPro picked inside the circle.
    assert {n: s.fire_info(n).get("picked") for n in range(1, 7)} == {
        1: False, 2: True, 3: True, 4: False, 5: True, 6: False}
    # The team's final score copied onto the partner after the gimme is not a penalty stroke.
    assert s.penalties(1) == 0 and not s._penalties


def test_one_teams_finish_doesnt_hide_another_teams_penalty():
    s = GsproState()
    s.team_scores, s.teammates = True, {frozenset((0, 1)), frozenset((2, 3))}
    s.hole_scores[(0, 0)] = 4   # team 0-1 is done on the hole
    for ev in [LogEvent("state", player=2, hole=0, score=2, strokes=1),
               LogEvent("state", player=2, hole=0, score=3, strokes=1),   # water: a bump with no shot
               LogEvent("state", player=1, hole=0, score=4, strokes=1)]:  # the partner's final-score copy
        s.apply(ev)
    assert s.penalties(2) == 1 and s.penalties(1) == 0


def test_in_a_scramble_the_putt_that_holed_is_the_teams_and_the_partners_miss_is_not():
    s = GsproState()
    for ev in [LogEvent("shot_fired"), LogEvent("state", player=0, hole=0, score=1, strokes=1),
               LogEvent("shot_fired"), LogEvent("state", player=1, hole=0, score=1, strokes=1),
               LogEvent("select", player=1), LogEvent("team_copy", player=0),
               LogEvent("shot_fired"), LogEvent("state", player=0, hole=0, score=2, strokes=1),
               LogEvent("shot_fired"), LogEvent("hole_out"), LogEvent("state", player=1, hole=0, score=2, strokes=1)]:
        s.apply(ev)
    assert [s.fire_info(n).get("picked") for n in range(1, 5)] == [False, True, False, True]


def test_a_solo_hole_out_is_not_a_pick():
    s = GsproState()
    for ev in [LogEvent("shot_fired"), LogEvent("hole_out"), LogEvent("state", player=0, hole=0, score=1, strokes=1)]:
        s.apply(ev)
    assert "picked" not in s.fire_info(1)


def test_a_solo_select_line_is_not_a_scramble():
    s = _replay("Selecting shot for player: 0\nLogging tdist: 100 and ActiveGameGimmieDistance: 1.22\n"
                + FIRE + _line(0, 1, 0))
    assert not s.team_scores and s.pick is None and not s.dropped_by


def _round_scores(path):
    """Each ended round's finished-hole scores, (course, {(index, 1-based hole): strokes})."""
    s, out = GsproState(), []
    for ev in parse_events(open(path, errors="replace").read()):
        s.apply(ev)
        if ev.kind == "end_round":
            out.append((s.course, {(i, h + 1): v for (i, h), v in s.last_hole_scores.items()}))
    return out


def test_finished_holes_are_scored_from_the_log_as_gspro_counts_them():
    # Real rounds (trimmed to the lines the parser reads), checked against GSPro's own final cards.
    # The scorecard OCR missed a cell on each of these: the conceded 6 on Riviera's 16 (and the
    # picked-up 9 on 17), and the 6 on Cabot's 11.
    riviera, pine, cabot = _round_scores("tests/data/gspro_log_hole_scores.txt")
    assert riviera[0] == "riviera_gsp"
    assert [riviera[1][(1, h)] for h in (16, 17, 18)] == [6, 9, 8]   # a gimmie, a pick-up, a gimmie
    assert [riviera[1][(0, h)] for h in (16, 17, 18)] == [4, 6, 8]
    # A scramble: the team score once the ball drops (not the teammate-check lines after it), on the
    # index whose line followed; the round takes the side's score from either partner.
    team = {h: v for (_, h), v in pine[1].items()}
    assert sorted(team) == list(range(1, 10)) and len(pine[1]) == 9 and sum(team.values()) == 36
    # Best ball, mulligans re-hit with the score line repeated.
    assert [cabot[1][(0, h)] for h in range(10, 19)] == [4, 6, 3, 4, 4, 6, 4, 4, 9]
    assert sum(v for (i, _), v in cabot[1].items() if i == 1) == 39


def test_a_stroke_taken_back_reopens_a_finished_hole():
    s = GsproState()
    for ev in [LogEvent("state", player=0, hole=0, score=0, strokes=0), LogEvent("shot_fired"),
               LogEvent("state", player=0, hole=0, score=1, strokes=1), LogEvent("shot_fired"),
               LogEvent("hole_out"), LogEvent("state", player=0, hole=0, score=2, strokes=1)]:
        s.apply(ev)
    assert s.hole_scores == {(0, 0): 2}
    s.apply(LogEvent("state", player=0, hole=0, score=1, strokes=-1))   # a mulligan of the holed putt
    assert s.hole_scores == {}
    for ev in [LogEvent("shot_fired"), LogEvent("state", player=0, hole=0, score=2, strokes=1),
               LogEvent("gimmie"), LogEvent("state", player=0, hole=0, score=3, strokes=1)]:
        s.apply(ev)
    assert s.hole_scores == {(0, 0): 3}
    s.apply(LogEvent("end_round"))
    assert s.hole_scores == {} and s.last_hole_scores == {(0, 0): 3}


def test_a_pick_up_on_the_last_hole_is_settled_when_the_round_completes():
    s = GsproState()
    for ev in [LogEvent("state", player=0, hole=8, score=0, strokes=0)] + [
            e for n in range(1, 9) for e in (LogEvent("shot_fired"), LogEvent("state", player=0, hole=8, score=n, strokes=1))] + [
            LogEvent("state", player=0, hole=8, score=0, strokes=-8), LogEvent("state", player=0, hole=8, score=8, strokes=8),
            LogEvent("state", player=0, hole=8, score=9, strokes=1)]:
        s.apply(ev)
    assert s.picked_up(0, 8) and s.hole_scores == {}
    s.apply(LogEvent("round_complete"))
    assert s.hole_scores == {(0, 8): 9}


def test_a_holed_shot_replayed_with_the_score_repeated_reopens_the_hole():
    s = GsproState()
    for ev in [LogEvent("state", player=0, hole=0, score=0, strokes=0), LogEvent("shot_fired"),
               LogEvent("state", player=0, hole=0, score=1, strokes=1), LogEvent("shot_fired"),
               LogEvent("hole_out"), LogEvent("state", player=0, hole=0, score=2, strokes=1),
               LogEvent("shot_fired"), LogEvent("state", player=0, hole=0, score=2, strokes=1)]:
        s.apply(ev)
    assert s.hole_scores == {}


def test_a_hole_finished_before_the_booth_started_can_still_reopen(tmp_path):
    path = tmp_path / "output_log.txt"
    path.write_text("Selected Course: x_gsp\nActivePlayer: 0  currentHole: 0  strokes: 0  Previous Score: 0\n"
                    " * ------- Shot Fired! ------- *\nActivePlayer: 0  currentHole: 0  strokes: 1  Previous Score: 0\n"
                    " * ------- Shot Fired! ------- *\nHole out !!! \n"
                    "ActivePlayer: 0  currentHole: 0  strokes: 1  Previous Score: 1\n")
    f = GsproLogFollower(str(path))
    f._prime()
    assert f.state.hole_scores == {(0, 0): 2}
    for ev in [LogEvent("shot_fired"), LogEvent("state", player=0, hole=0, score=2, strokes=1)]:
        f.state.apply(ev)                                  # replayed with the score repeated
    assert f.state.hole_scores == {}
    f._prime()
    f.state.apply(LogEvent("state", player=0, hole=0, score=1, strokes=-1))   # or taken back explicitly
    assert f.state.hole_scores == {}


def test_a_scramble_pick_inside_the_gimme_circle_concedes_the_picked_balls_stroke():
    # Pine Valley, 10/20: both partners putt, then GSPro picks player 0's ball (the first putt), which
    # sits inside the circle. That putt's own watch had long since settled as a miss.
    s = _replay(
        _line(1, 1, 3, player=0) + "Putt Out Mode\n* ------- Shot Fired! ------- *\n"
        + _line(1, 1, 3, player=1)
        + "Selecting shot for player: 0\n"
        "Logging tdist: 0.2899483 and ActiveGameGimmieDistance: 1.22\n"
        "Select Shot - active user ball is within Gimmie distance\n"
        + _line(1, 1, 4, player=0)
        + "SelectShotCopyScoreToTeammates = true\nSelect Shot - Marking teamats as gimmie distance\n"
        + _line(1, 1, 4, player=1)
    )
    assert s.pick_gimmes == [(0, 1)]


def test_an_ordinary_gimmie_is_not_a_pick():
    s = _replay(FIRE + _line(1, 1, 3) + "Logging tdist: 0.5 and ActiveGameGimmieDistance: 1.22\n"
                "Select Shot - active user ball is within Gimmie distance\n" + _line(1, 1, 4))
    assert s.gimmies_by == {0: 1} and s.pick_gimmes == []


# ---- ball positions, aim clicks and the hole layout ------------------------------------------

SHOT = "* ------- Shot Fired! ------- *\n"
LAYOUT = ('[{"active":true,"played":true,"par":4,"pinPos":{"x":10.0,"y":1.0,"z":300.0},'
          '"teePos":{"x":0.0,"y":2.0,"z":0.0},"ShotWPPos0":{"x":5.0,"y":0.0,"z":200.0}},'
          '{"active":false,"played":false,"par":3,"pinPos":{"x":0.0,"y":0.0,"z":0.0},'
          '"teePos":{"x":50.0,"y":2.0,"z":0.0},"ShotWPPos0":{"x":0.0,"y":0.0,"z":0.0}}]\n')


def test_each_shot_takes_the_position_and_aim_logged_since_the_last():
    s = _replay(
        "(12.5, 100.0, 0.0)\n" + SHOT + _line(0, 1, 0)          # the tee shot: aimed, but no setup block
        + "Selecting shot for player: 0\nData for selected player: X 5.5 Y 1.25 Z 210.0\n"
        + "Logging tdist: 90.1 and ActiveGameGimmieDistance: 1.22\n" + SHOT + _line(0, 1, 1)
        + SHOT + _line(0, 1, 2)                                  # a stroke with no fresh setup block
        + "Data for selected player: X 9.0 Y 1.0 Z 299.0\nHole out !!! \n" + _line(0, 1, 3)
        + "(0.0, 50.0, 0.0)\n" + SHOT + _line(1, 1, 0) + "Round Is Complete\n" + LAYOUT)
    assert [s.fire_info(n).get("start") for n in (1, 2, 3, 4)] == [None, (5.5, 1.25, 210.0), None, None]
    assert [s.fire_info(n).get("aim") for n in (1, 2, 3, 4)] == [(12.5, 100.0), None, None, (0.0, 50.0)]
    assert s.layouts == 1 and s.layout_holes[0] == {
        "par": 4, "played": True, "tee": (0.0, 2.0, 0.0), "pin": (10.0, 1.0, 300.0), "waypoint": (5.0, 0.0, 200.0)}
    assert s.layout_holes[1]["pin"] is None and not s.layout_holes[1]["played"]


def _rest(player, x, z, tdist):
    return (f"Selecting shot for player: {player}\nData for selected player: X {x} Y 1.0 Z {z}\n"
            f"Logging tdist: {tdist} and ActiveGameGimmieDistance: 1.22\n")


def test_in_a_group_each_shot_starts_where_its_own_players_ball_stopped():
    # Paynes Valley, 10/23, three players: GSPro logs where the shooter's ball stopped right after their
    # shot, so the block before a fire is usually someone else's ball.
    s = _replay(
        SHOT + _line(0, 1, 0, player=1) + _rest(1, 864.9, 237.2, 99.7)       # 1: Corey drives
        + SHOT + _line(0, 1, 0, player=2) + _rest(2, 944.6, 207.9, 113.8)    # 2: Paul drives
        + SHOT + _line(0, 1, 0, player=0) + _rest(0, 909.7, 239.5, 115.3)    # 3: Josh drives
        + "Next Player\n" + SHOT + _line(0, 1, 1, player=0) + _rest(0, 849.0, 136.9, 6.0)   # 4: Josh
        + "Next Player\n" + SHOT + _line(0, 1, 1, player=2) + _rest(2, 847.4, 151.0, 14.8)  # 5: Paul
        + SHOT + _line(1, 1, 0, player=0) + _line(1, -1, 1, player=0) + _rest(0, 868.2, 510.3, 7.1)  # 6: taken back
        + SHOT + _line(1, 1, 0, player=0))                                   # 7: the mulligan's re-hit
    starts = [s.fire_info(n).get("start") for n in range(1, 8)]
    assert starts == [None, None, None, (909.7, 1.0, 239.5), (944.6, 1.0, 207.9), None, (868.2, 1.0, 510.3)]
    assert s.fire_info(5).get("to_pin") == 113.8 and "to_pin" not in s.fire_info(1)


def test_aim_offset_scales_the_minimap_to_the_distance():
    assert aim_offset(0.0, -338.0, 150.0) == (0.0, 0.0)          # on the ball
    along, right = aim_offset(67.7, 339.0, 200.0)                # at the pin, a tenth of the span right
    assert abs(along - 200.0) < 1e-9 and abs(right - 20.0) < 1e-9
