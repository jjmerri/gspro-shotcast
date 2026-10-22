import json
import random
from pathlib import Path

import pytest

from commentator.config import Config
from commentator.engine import Engine
from commentator.grounding import check_line
from commentator.hud import Hud, OcrItem, OtherPlayer, Scorecard, parse_others
from commentator.league import League
from commentator.models import Shot
from commentator.round import Round, normalize_format
from commentator.state import ShotRecord

LEAGUE = League({
    "league_name": "Test League",
    "teams": [
        {"name": "Aces", "players": [{"name": "Mike", "gspro_name": "MikeD", "handicap": 12.0},
                                     {"name": "Sarah", "handicap": 24.0}]},
        {"name": "Duffers", "players": [{"name": "Tony", "handicap": 6.0}, {"name": "Priya", "handicap": 9.0}]},
    ],
    "schedule": [
        {"week": 3, "course": "Wynn", "format": "Alternate", "hole_count": 9, "start_hole": 1,
         "date": "2026-10-06", "end_date": "2099-01-01"},
        {"week": 4, "course": "Pebble Beach", "format": "Best Ball", "hole_count": 9, "start_hole": 1,
         "end_date": "2099-01-01"},
    ],
})

_t = iter(range(1, 10_000))


def rec(player, seq, par, number, before, after, club="7I", round_id=1):
    shot = Shot(ball_speed=100 if club != "PT" else 6, vla=15, hla=0, total_spin=5000, spin_axis=0, carry=150,
                club=club, timestamp=float(next(_t)))
    return ShotRecord(player, shot, f"hole {seq}", Hud(lie=after), [], round_id=round_id, hole_seq=seq, par=par,
                      lie_before=before, shot_number=number)


def test_roster_position_follows_setup_slot_order_not_the_filtered_roster():
    lg = League.load("data/league.example.json")
    a, b = "MikeD", "SarahK"
    # A guest occupies slot 0; GSPro still indexes the league players by their real slots, so the
    # captured slot order must keep MikeD at 1 (not 0 as the filtered 2-player roster would imply).
    r = Round(league=lg, gspro_players=[a, b], course="X", scheduled_format=None,
              slot_names=["Guest", a, b])
    assert r.roster_position(a) == 1 and r.roster_position(b) == 2
    # Without a captured slot order (a tee-time round), it falls back to roster order.
    r2 = Round(league=lg, gspro_players=[a, b], course="X", scheduled_format=None)
    assert r2.roster_position(a) == 0 and r2.roster_position(b) == 1


def test_formats_and_schedule():
    assert normalize_format("Team Sum") == normalize_format("aggregate") == "aggregate"
    assert normalize_format("Alt Shot") == "alternate" and normalize_format("best-ball") == "best_ball"
    assert LEAGUE.schedule_entry("2026-10-06")["course"] == "Wynn"  # date-only fallback
    assert LEAGUE.schedule_entry("2026-10-07") is None
    assert "schedule" not in LEAGUE.prompt_text()


def test_practice_round_doesnt_infer_a_team_format():
    # A lone league player practising (not a league round) is not playing alternate shot: no team
    # format is inferred from the one ball.
    r = Round(league=LEAGUE, gspro_players=["MikeD"], course="Augusta National", scheduled_format=None,
              holes=9, is_league=False)
    assert r.format is None and not r.format_warning
    assert r.format_facts() == []


def test_one_ball_per_team_is_alternate_shot_and_hides_names():
    r = Round(league=LEAGUE, gspro_players=["MikeD"], course="Wynn", scheduled_format=None, holes=9)
    assert r.format == "alternate"
    assert [s.label for s in r.sides] == ["Aces"]
    assert set(r.anonymous_players()) == {"Mike", "Sarah"}
    # 25% of the lower 9-hole course handicap plus 20% of the higher: 0.25*6 + 0.20*12 = 3.9.
    assert r.sides[0].strokes == 4
    # A scheduled best ball with one ball per team can only be alternate shot.
    r = Round(league=LEAGUE, gspro_players=["MikeD"], course=None, scheduled_format="best ball")
    assert r.format == "alternate" and r.format_warning
    # A scramble played on one GSPro player stays a scramble, still with no names.
    r = Round(league=LEAGUE, gspro_players=["MikeD"], course=None, scheduled_format="scramble")
    assert r.format == "scramble" and r.anonymous_players()


def test_two_balls_per_team_use_the_schedule():
    r = Round(league=LEAGUE, gspro_players=["MikeD", "Sarah"], course=None, scheduled_format="best ball")
    assert r.format == "best_ball" and not r.anonymous_players()
    assert {s.label: s.strokes for s in r.sides} == {"Mike": 5, "Sarah": 9}  # 75% of 6 and 12, .5 rounds up
    r = Round(league=LEAGUE, gspro_players=["MikeD", "Sarah"], course=None, scheduled_format="alternate shot")
    assert r.format is None and r.format_warning


def _three_hole_round(fmt="best ball"):
    r = Round(league=LEAGUE, gspro_players=["MikeD", "Sarah"], course="Wynn", scheduled_format=fmt, holes=3)
    card = Scorecard(holes=list(range(1, 19)), par={1: 4, 2: 3, 3: 5}, index={1: 5, 2: 9, 3: 1},
                     scores={"MIKED": {1: 4, 2: 5, 3: 5}, "SARAH": {1: 5, 2: 4, 3: 7}, "SCORECARD": {}})
    shots = [
        # Hole 1, par 4: fairway, green in two, two putts.
        rec("Mike", 1, 4, 1, "tee", "fairway", "DR"), rec("Mike", 1, 4, 2, "fairway", "green"),
        rec("Mike", 1, 4, 3, "green", "green", "PT"), rec("Mike", 1, 4, 4, "green", None, "PT"),
        # Hole 2, par 3: misses the green, three putts.
        rec("Mike", 2, 3, 1, "tee", "rough"), rec("Mike", 2, 3, 2, "rough", "green"),
        rec("Mike", 2, 3, 3, "green", "green", "PT"), rec("Mike", 2, 3, 4, "green", "green", "PT"),
        rec("Mike", 2, 3, 5, "green", None, "PT"),
        # Hole 3, par 5: wet second shot (a penalty stroke), one putt.
        rec("Mike", 3, 5, 1, "tee", "fairway", "DR"), rec("Mike", 3, 5, 2, "fairway", "water", "3W"),
        rec("Mike", 3, 5, 4, "fairway", "green"), rec("Mike", 3, 5, 5, "green", None, "PT"),
        rec("Mike", 1, 4, 1, "tee", "fairway", "DR", round_id=-1),  # another round: ignored
    ]
    for s in shots:
        s.round_id = r.id if s.round_id == 1 else s.round_id
    return r, card, shots


def test_scorecard_drives_scores_and_stats():
    r, card, shots = _three_hole_round()
    assert r.apply_scorecard(card) and r.complete()
    mike = r.side_for("Mike")
    s = r.side_stats(mike, shots)
    assert (s.gross, s.par, s.scores) == (14, 12, {1: 4, 2: 5, 3: 5})
    assert s.fir == (2, 2) and s.gir == (1, 3)
    # Two greens missed (holes 2, 3); the par-5 third was saved for par (an up-and-down), the par-3
    # second was a three-putt bogey, so one scramble of two. No greenside bunker played this round.
    assert s.scramble == (1, 2) and s.sand == (0, 0)
    assert (s.putts, s.putt_holes, s.one_putts, s.three_putts, s.penalties) == (6, 3, 1, 1, 1)
    assert s.longest_drive == 150


def test_player_stats_computes_the_same_from_one_players_shots():
    # Per-player stats mirror side_stats. Here the card row keys ("MIKED") don't match the shot player
    # ("Mike"), so it falls back to the shot-based score, which equals the card, so the stats match.
    r, card, shots = _three_hole_round()
    s = r.player_stats("Mike", shots)
    assert (s.gross, s.par) == (14, 12)
    assert s.fir == (2, 2) and s.gir == (1, 3)
    assert s.scramble == (1, 2) and s.sand == (0, 0)
    assert r.player_stats("Nobody", shots).holes_scored == 0   # a player with no shots gets nothing


def test_player_stats_reads_the_card_so_a_conceded_bogey_is_not_a_save():
    # Par 4 missed into greenside sand, out to 5 ft, tap-in conceded -> a real bogey 5. The conceded
    # putt leaves no shot record, so the shots alone imply a 4 (a false up-and-down). The scorecard has
    # the true 5 keyed by GSPro name, so player_stats scores a miss, not a save.
    r = Round(league=LEAGUE, gspro_players=["Mike"], course="X", scheduled_format=None, holes=1)
    r.par = {1: 4}
    r.card_scores = {"Mike": {1: 5}}
    r.scorecard_seen = True
    shots = [rec("Mike", 1, 4, 1, "tee", "rough", "DR"), rec("Mike", 1, 4, 2, "rough", "sand"),
             rec("Mike", 1, 4, 3, "sand", "green"), rec("Mike", 1, 4, 4, "green", None, "PT")]
    for s in shots:
        s.round_id = r.id
    st = r.player_stats("Mike", shots)
    assert st.scores[1] == 5                            # the card's bogey, not the shots' implied 4
    assert st.gir == (0, 1) and st.scramble == (0, 1)   # a missed green that was NOT saved for par


def test_player_stats_counts_a_conceded_stiff_as_a_gir():
    # Par 3, tee shot stiff inside the gimme, tap-in conceded (card 2). There's no green-putt record,
    # but the tee shot reached the green in regulation, so it's a GIR hit, not a miss.
    r = Round(league=LEAGUE, gspro_players=["Mike"], course="X", scheduled_format=None, holes=1)
    r.par = {1: 3}
    r.card_scores = {"Mike": {1: 2}}
    r.scorecard_seen = True
    shots = [rec("Mike", 1, 3, 1, "tee", "green", "7I")]   # tee shot to the green, conceded, no putt
    for s in shots:
        s.round_id = r.id
    st = r.player_stats("Mike", shots)
    assert st.scores[1] == 2 and st.gir == (1, 1)


async def test_round_end_records_per_player_stats(tmp_path):
    from commentator.models import RoundEndEvent

    e = _engine(tmp_path)
    e.cfg.commentary.closer = False   # return right after recording, no closer/speech
    r, card, shots = _three_hole_round()
    r.apply_scorecard(card)
    e.round = r
    e.state.shots = shots
    await e.handle_round_end(RoundEndEvent(reason="test"))
    rows = e.store.db.execute("SELECT player, gir_tries, fir_tries, fmt FROM player_rounds").fetchall()
    assert any(row["player"] == "Mike" and row["gir_tries"] == 3 and row["fir_tries"] == 2
               and row["fmt"] == "best_ball" for row in rows)


def test_side_stats_final_override_sets_the_last_hole_score():
    # The closer is pre-built before the last putt; final_override supplies the hypothetical last-hole
    # score so each branch's whole-round totals are right.
    r, card, shots = _three_hole_round()
    r.apply_scorecard(card)
    mike = r.side_for("Mike")
    base = r.side_stats(mike, shots)
    assert base.scores[3] == 5 and base.gross == 14
    bumped = r.side_stats(mike, shots, final_override=7)
    assert bumped.scores[3] == 7 and bumped.gross == 16  # only the last hole (seq 3) moves
    assert bumped.scores[1] == 4 and bumped.scores[2] == 5
    # The closer is pre-built with the ball on the last green and its putts still to come: the hole's
    # fairway and green are already known, and the branch's score says how many putts. A closer built
    # without them called the second fairway of the round the first.
    early = [x for x in shots if not (x.hole_seq == 3 and x.shot.club == "PT")]
    par_branch = r.side_stats(mike, early, final_override=5)
    assert (par_branch.fir, par_branch.gir, par_branch.scramble) == (base.fir, base.gir, base.scramble)
    assert (par_branch.putts, par_branch.putt_holes, par_branch.one_putts) == (base.putts, base.putt_holes, base.one_putts)
    three_putt = r.side_stats(mike, early, final_override=7)
    assert three_putt.putts == base.putts + 2 and three_putt.three_putts == base.three_putts + 1
    assert three_putt.scramble == (base.scramble[0] - 1, base.scramble[1])   # the par-5 no longer saved
    # Penalties and the longest drive come from full shots already struck on the overridden hole, so
    # they're preserved (the pre-built closer would otherwise under-report both). The fixture's only
    # penalty is on the last hole, so this is exactly the case that was being dropped.
    assert bumped.penalties == base.penalties == 1
    assert bumped.longest_drive == base.longest_drive == 150
    # closing_facts threads the per-side overrides through.
    facts = "\n".join(r.closing_facts(shots, {}, final_overrides={mike.key: 7}))
    assert "gross 16" in facts


def test_side_stats_counts_sand_saves_and_up_and_downs():
    r = Round(league=LEAGUE, gspro_players=["MikeD", "SarahK"], course="X", scheduled_format="best_ball", holes=3)
    mike = r.side_for("MikeD")
    # Hole 1 par 4: fairway, into a greenside bunker, out to the green, one putt -> par, a sand save.
    sand_out = rec("Mike", 1, 4, 3, "sand", "green")
    sand_out.hud.to_pin = "18 yards"            # greenside, not a fairway bunker
    # Hole 3 par 4: tee into a FAIRWAY bunker a long way out -> not a greenside sand save.
    fairway_bunker = rec("Mike", 3, 4, 2, "sand", "green")
    fairway_bunker.hud.to_pin = "95 yards"
    shots = [
        rec("Mike", 1, 4, 1, "tee", "fairway", "DR"), rec("Mike", 1, 4, 2, "fairway", "sand"),
        sand_out, rec("Mike", 1, 4, 4, "green", "green", "PT"),
        # Hole 2 par 4: green in regulation, two putts -> a GIR, no scramble/sand chance.
        rec("Mike", 2, 4, 1, "tee", "fairway", "DR"), rec("Mike", 2, 4, 2, "fairway", "green"),
        rec("Mike", 2, 4, 3, "green", "green", "PT"), rec("Mike", 2, 4, 4, "green", "green", "PT"),
        # Hole 3 par 4: drive, fairway bunker, green, two putts -> par but from a fairway bunker.
        rec("Mike", 3, 4, 1, "tee", "fairway", "DR"), fairway_bunker,
        rec("Mike", 3, 4, 3, "green", "green", "PT"), rec("Mike", 3, 4, 4, "green", "green", "PT"),
    ]
    for s in shots:
        s.round_id = r.id
    st = r.side_stats(mike, shots)
    assert st.sand == (1, 1)         # only hole 1's greenside bunker counts; the fairway bunker doesn't
    assert st.scramble[0] == 1       # hole 1 green missed but par saved
    assert st.gir[0] >= 1            # hole 2 (and maybe 3) found the green in regulation


def test_conceded_stiff_counts_as_a_gir():
    # An approach stuffed inside the gimme is auto-conceded with no putt, so there is no 'green' putt
    # record. The putt signal misses it, but the approach SETTLED on the green in regulation (its finish
    # lie), so it's a real GIR - and NOT an up-and-down (the green wasn't missed).
    r = Round(league=LEAGUE, gspro_players=["MikeD", "Sarah"], course="Wynn", scheduled_format="best ball", holes=1)
    card = Scorecard(holes=list(range(1, 19)), par={1: 4}, index={1: 5},
                     scores={"MIKED": {1: 3}, "SARAH": {1: 4}, "SCORECARD": {}})
    shots = [rec("Mike", 1, 4, 1, "tee", "fairway", "DR"), rec("Mike", 1, 4, 2, "fairway", "green", "9I")]
    for s in shots:
        s.round_id = r.id
    r.apply_scorecard(card)
    st = r.side_stats(r.side_for("Mike"), shots)
    assert st.scramble == (0, 0)   # no fabricated up-and-down: the green was hit, not scrambled
    assert st.gir == (1, 1)        # the regulation approach settled on the green -> a GIR


def test_conceded_chip_from_off_the_green_is_a_gir_miss():
    # Missed the green with the approach, then chipped to a conceded gimme (score par). The chip reaches
    # the green but at shot 3 on a par 4 - past regulation - so it's a GIR MISS (an up-and-down), not a
    # GIR, even though a later shot finished on the green.
    r = Round(league=LEAGUE, gspro_players=["MikeD", "Sarah"], course="Wynn", scheduled_format="best ball", holes=1)
    card = Scorecard(holes=list(range(1, 19)), par={1: 4}, index={1: 5},
                     scores={"MIKED": {1: 4}, "SARAH": {1: 5}, "SCORECARD": {}})
    shots = [rec("Mike", 1, 4, 1, "tee", "fairway", "DR"), rec("Mike", 1, 4, 2, "fairway", "rough", "9I"),
             rec("Mike", 1, 4, 3, "rough", "green", "SW")]
    for s in shots:
        s.round_id = r.id
    r.apply_scorecard(card)
    st = r.side_stats(r.side_for("Mike"), shots)
    assert st.gir == (0, 1)        # green reached only at shot 3, past regulation -> a miss


def test_missed_green_saved_with_a_fringe_putt_is_a_scramble():
    # PGA scrambling: every missed green is a chance, whatever the next stroke. Approach to the fringe,
    # putter from there (not a putt: it's off the green), one putt for par -> a scramble made.
    r = Round(league=LEAGUE, gspro_players=["MikeD", "Sarah"], course="Wynn", scheduled_format="best ball", holes=1)
    card = Scorecard(holes=list(range(1, 19)), par={1: 4}, index={1: 5},
                     scores={"MIKED": {1: 4}, "SARAH": {1: 5}, "SCORECARD": {}})
    shots = [rec("Mike", 1, 4, 1, "tee", "fairway", "DR"), rec("Mike", 1, 4, 2, "fairway", "fringe", "9I"),
             rec("Mike", 1, 4, 3, "fringe", "green", "PT"), rec("Mike", 1, 4, 4, "green", "green", "PT")]
    for s in shots:
        s.round_id = r.id
    r.apply_scorecard(card)
    st = r.side_stats(r.side_for("Mike"), shots)
    assert st.gir == (0, 1) and st.scramble == (1, 1) and st.putts == 1


def test_sand_save_is_up_and_down_from_the_bunker_whatever_the_score():
    # PGA: a greenside bunker, then holed in two strokes or fewer counting the bunker shot.
    r = Round(league=LEAGUE, gspro_players=["MikeD", "Sarah"], course="Wynn", scheduled_format="best ball", holes=3)
    bogey = rec("Mike", 1, 4, 4, "sand", "green", "SW")       # 4th from the bunker, one putt: bogey, saved
    par5 = rec("Mike", 2, 5, 2, "sand", "green", "SW")        # 2nd from the bunker, three to finish: par, not saved
    long = rec("Mike", 3, 4, 3, "sand", "rough", "SW")        # out long, then chipped in: saved
    for b in (bogey, par5, long):
        b.hud.to_pin = "15 yards"
    card = Scorecard(holes=list(range(1, 19)), par={1: 4, 2: 5, 3: 4}, index={1: 5, 2: 9, 3: 1},
                     scores={"MIKED": {1: 5, 2: 5, 3: 4}, "SARAH": {1: 5, 2: 6, 3: 5}, "SCORECARD": {}})
    shots = [
        rec("Mike", 1, 4, 1, "tee", "rough", "DR"), rec("Mike", 1, 4, 2, "rough", "rough", "7I"),
        rec("Mike", 1, 4, 3, "rough", "sand", "SW"), bogey, rec("Mike", 1, 4, 5, "green", "green", "PT"),
        rec("Mike", 2, 5, 1, "tee", "fairway", "DR"), par5, rec("Mike", 2, 5, 3, "green", "green", "PT"),
        rec("Mike", 2, 5, 4, "green", "green", "PT"), rec("Mike", 2, 5, 5, "green", "green", "PT"),
        rec("Mike", 3, 4, 1, "tee", "fairway", "DR"), rec("Mike", 3, 4, 2, "fairway", "sand", "9I"), long,
        rec("Mike", 3, 4, 4, "rough", None, "LW"),
    ]
    for s in shots:
        s.round_id = r.id
    r.apply_scorecard(card)
    assert r.side_stats(r.side_for("Mike"), shots).sand == (2, 3)


def test_holed_recovery_counts_as_a_scramble_and_sand_save():
    # The asymmetry fix: an up-and-down HOLED from the bunker leaves no putt record, but must still
    # count - and as a success - or the rate skews low by dropping only the successes (misses always
    # putt out). Keyed off the recovery shot, not a putt.
    r = Round(league=LEAGUE, gspro_players=["MikeD", "Sarah"], course="Wynn", scheduled_format="best ball", holes=3)
    holed = rec("Mike", 1, 4, 3, "sand", None, "SW")        # holed out of the bunker for par, no putt
    holed.hud.to_pin = "15 yards"
    missed = rec("Mike", 2, 4, 3, "sand", "green", "SW")     # out to the green, two putts, bogey
    missed.hud.to_pin = "15 yards"
    far_bunker = rec("Mike", 3, 4, 3, "sand", "green", "SW")  # a recovery, but a FAIRWAY bunker (90 yd)
    far_bunker.hud.to_pin = "90 yards"
    card = Scorecard(holes=list(range(1, 19)), par={1: 4, 2: 4, 3: 4}, index={1: 5, 2: 9, 3: 1},
                     scores={"MIKED": {1: 4, 2: 5, 3: 4}, "SARAH": {1: 5, 2: 6, 3: 5}, "SCORECARD": {}})
    shots = [
        rec("Mike", 1, 4, 1, "tee", "fairway", "DR"), rec("Mike", 1, 4, 2, "fairway", "sand"), holed,
        rec("Mike", 2, 4, 1, "tee", "fairway", "DR"), rec("Mike", 2, 4, 2, "fairway", "sand"), missed,
        rec("Mike", 2, 4, 4, "green", "green", "PT"), rec("Mike", 2, 4, 5, "green", "green", "PT"),
        rec("Mike", 3, 4, 1, "tee", "fairway", "DR"), rec("Mike", 3, 4, 2, "fairway", "sand"),
        far_bunker, rec("Mike", 3, 4, 4, "green", "green", "PT"),
    ]
    for s in shots:
        s.round_id = r.id
    r.apply_scorecard(card)
    st = r.side_stats(r.side_for("Mike"), shots)
    # Hole 1 saved (holed), hole 2 missed, hole 3 saved - all three are recoveries so all are scramble
    # chances. Only holes 1-2 are greenside bunkers; hole 3's 90-yd fairway bunker is a scramble but
    # not a sand save (the greenside <=30 yd gate, exercised here independent of shot number).
    assert st.scramble == (2, 3) and st.sand == (1, 2)


def test_closing_facts_judge_net_and_team_best_ball():
    r, card, shots = _three_hole_round()
    r.apply_scorecard(card)
    facts = "\n".join(r.closing_facts(shots, {"Mike": {"rounds": 2, "net_rounds": 2, "avg_net_to_par": 3.0, "best_net_to_par": -1}}))
    # 3 holes: Mike's 12 index is 2 course strokes, 85% of that rounds to 2.
    assert "Mike: gross 14 over 3 holes (2 over par, par 12); with 2 handicap strokes that's a net 12, net even par." in facts
    assert "Mike hit 2 of 2 fairways" in facts and "1 of 3 greens in regulation" in facts
    assert "one three-putt and one one-putt" in facts and "1 penalty stroke" in facts
    assert "1 double bogey (hole 2)" in facts
    # Season averages are per nine; a 3-hole round compares against a third of it.
    assert "Mike's average in 2 earlier league rounds (scaled to 3 holes): net 1 over par." in facts
    # Best ball net: strokes fall on the hardest holes; hole 3 is index 1.
    assert "Team Aces (best ball): net" in facts


def test_scorecard_corrects_an_assumed_back_nine_start():
    r = Round(league=LEAGUE, gspro_players=["MikeD"], course=None, scheduled_format=None)
    assert r.start_hole == 1 and r.start_hole_assumed
    r.apply_scorecard(Scorecard(holes=list(range(1, 19)), scores={"MIKED": {10: 4, 11: 5}}))
    assert r.start_hole == 10 and r.card_scores["MikeD"] == {1: 4, 2: 5}


def test_grounding_bans_partner_names_and_round_mode_skips_lie_checks():
    facts = "Aces: gross 44.\nAces hit 3 of 6 fairways."
    ban = [(["mike", "miked"], ["sarah"]), (["sarah"], ["mike", "miked"])]
    assert not check_line("Mike pured that one.", facts, "", banned_names=ban).ok
    assert check_line("The Aces will take that.", facts, "", banned_names=ban).ok
    assert check_line("Sarah and Mike are rolling.", facts, "", banned_names=ban).ok
    assert check_line("They found the fairway three times and it never mattered.", facts, "", mode="round").ok
    assert not check_line("A birdie on the last saved them.", facts, "", mode="round").ok


def test_parse_others_reads_rows_with_distances_only():
    items = [OcrItem("Wes", 113, 42, 47), OcrItem("400.3yds", 329, 42, 41), OcrItem(" 8.0yds", 472, 42, 39),
             OcrItem("E", 622, 40, 37), OcrItem("212.5", 100, 300, 40), OcrItem("CARRY (game)", 100, 340, 20)]
    assert parse_others(items) == [OtherPlayer("Wes", "400.3 yards", "E")]


def test_scorecard_ocr_on_real_frame():
    pytest.importorskip("rapidocr")
    frame = Path("data/screens/mgq_scorecard_after_h9_t0269.png")
    if not frame.exists():
        pytest.skip("GSPro frames not downloaded")
    from PIL import Image

    from commentator.config import ScreenConfig
    from commentator.screen import Screen

    hud = Screen(ScreenConfig()).read_hud(["scorecard_title", "scorecard"], Image.open(frame).convert("RGB"))
    assert hud.scorecard_open
    card = hud.scorecard
    assert card.holes == list(range(1, 19)) and card.par[3] == 5 and card.index[11] == 1
    # The player's name is under a camera inset in this video; the scores still line up.
    assert card.scores["ST"] == {1: 4, 2: 5, 3: 5, 4: 3, 5: 4, 6: 4, 7: 3, 8: 5, 9: 4}


def _engine(tmp_path):
    from commentator.sources.replay import ReplaySource
    from commentator.speech import SpeechQueue
    from commentator.tts.console_tts import ConsoleTTS
    from tests.test_core import FakeLLM

    cfg = Config(log_dir=str(tmp_path))
    llm = FakeLLM(cfg.llm.live)
    return Engine(cfg=cfg, league=LEAGUE, source=ReplaySource("data/sample_shots.jsonl"), live_llm=llm,
                  segment_llm=llm, speech=SpeechQueue(cfg, ConsoleTTS()), rng=random.Random(0))


def test_contrast_fact_league_vs_practice(tmp_path):
    from commentator.hud import Hud
    from commentator.models import Shot

    e = _engine(tmp_path)

    def putt(who, made, league):
        e.store.record_shot(player=who, hole="hole 1 par 4", shot=Shot(club="PT"),
                            before=Hud(lie="green", to_pin="8 feet", shot_number=2), after=Hud(lie="green"),
                            tags=[], carry_game_yards=None, total_yards=None, offline_yards=None, course="X",
                            to_pin_yards=8 / 3.0, gimme_yards=1.3, fmt="best_ball", attributed=True,
                            outcome="holed" if made else "missed", is_league=league)

    # League 8/10 (80%) vs practice 2/10 (20%): a 60-point gap, past the 20-point margin, 10 each clears
    # the 10-attempt gate (large enough that one putt can't manufacture the contrast).
    for m in [True] * 8 + [False] * 2:
        putt("Toby", m, league=True)
    for m in [True] * 2 + [False] * 8:
        putt("Toby", m, league=False)
    hud = Hud(lie="green", to_pin="8 feet", shot_number=2)
    out = e._contrast_facts("Toby", True, hud)
    assert len(out) == 1 and "league" in out[0] and "practice" in out[0]

    # A fresh player with thin slices (6 each, below the 10 gate) -> no contrast, even if they differ.
    for m in [True] * 5 + [False]:
        putt("Dana", m, league=True)
    for m in [True] + [False] * 5:
        putt("Dana", m, league=False)
    assert e._contrast_facts("Dana", True, hud) == []


def test_round_start_needs_everyone_on_the_first_tee(tmp_path):
    e = _engine(tmp_path)
    tee = Hud(player_text="MikeD E Shot 1 400.3 YDS", shot_number=1, score_to_par="E", to_pin="400.3 yards",
              others=[OtherPlayer("Sarah", "400.3 yards", "E")])
    assert e._is_round_start(tee)
    assert not e._is_round_start(Hud(**{**tee.__dict__, "shot_number": 2}))
    # Distance isn't checked (GSPro's elevation readout bleeds into the others region and misreads
    # it); the score is: an other who is level is fine, an other with a score has already played.
    assert e._is_round_start(Hud(**{**tee.__dict__, "others": [OtherPlayer("Sarah", "8.0 yards", "E")]}))
    assert not e._is_round_start(Hud(**{**tee.__dict__, "others": [OtherPlayer("Sarah", "300.0 yards", "+1")]}))
    # The back nine starts a round as well as the front; the hole number isn't consulted.
    e.hole_number = 10
    assert e._is_round_start(tee)
    # Shots recorded earlier today don't block a fresh tee: restarting the booth still opens.
    e.current_course = "Wynn"
    e.store.db.execute("INSERT INTO shots (session_id, ts, course, is_putt) VALUES (?, 0, 'Wynn', 0)", (e.store.session_id,))
    assert e._is_round_start(tee)
    # But a just-read scorecard with holes played means the booth started mid-round: no opener.
    import time as _t
    e._holes_played, e._holes_played_at = 3, _t.monotonic()
    assert not e._is_round_start(tee)


WYNN_CSV = "\n".join([
    "CourseName\tWynn Golf Club\t",
    "CourseLocation\tLas Vegas, NV\t",
    "Altitude(m)(ft)\t2080\t6824.064\t",
    "HOLE \t 1 \t 2 \t 3 \t 4 \t 5 \t 6 \t 7 \t 8 \t 9 \t OUT \t \t 10 \t 11 \t 12 \t 13 \t 14 \t 15 \t 16 \t 17 \t 18 \t IN \t TOTAL\t",
    "Par \t4\t3\t5\t4\t3\t4\t3\t5\t4\t35\t\t3\t5\t3\t5\t4\t4\t4\t4\t3\t35\t70\t",
    "Index \t13\t11\t1\t5\t17\t9\t15\t7\t3\t\t\t10\t2\t16\t12\t6\t18\t4\t14\t8\t",
    "Black\t393\t176\t471\t384\t134\t343\t144\t490\t424\t2959.0\t\t213\t540\t191\t476\t404\t329\t444\t361\t228\t0.0\t6145\t",
])


def test_course_card_reads_par_and_index_from_gspro_files(tmp_path):
    from commentator import course_card

    folder = tmp_path / "wynn_gsp"
    folder.mkdir()
    (folder / "wynn_gsp.unity3d.csv").write_text(WYNN_CSV, encoding="utf-8")
    card = course_card.load(tmp_path, "Wynn Golf Club")
    assert card and card.name == "Wynn Golf Club"
    assert sum(card.par.values()) == 70 and card.par[3] == 5 and card.par[18] == 3
    assert card.index[3] == 1 and card.index[12] == 16 and len(card.index) == 18
    assert card.altitude_ft == 2080 and card.yards["Black"][9] == 424
    # A name that matches nothing in the directory gives nothing, not a wrong course.
    assert course_card.load(tmp_path, "Pebble Beach") is None


DETAILS = ("Test Links|Someone|1010|A scenic place, links style.|Erin, WI|1|1|72|"
           "5|4|4|4|4|3|5|4|3|4|4|4|3|5|4|3|4|5|"
           "13|7|11|9|15|1|5|17|4|14|10|18|2|12|16|8|6|3|"
           "76.9|145|70.5|129|#$%^&*")


def test_course_card_from_coursedetails_and_matches_by_name_not_folder(tmp_path):
    from commentator import course_card

    # Folder name deliberately unlike the course name, to prove matching uses the file's name.
    folder = tmp_path / "zz7_gsp"
    folder.mkdir()
    (folder / "coursedetails.txt").write_text(DETAILS, encoding="utf-8")
    card = course_card.load(tmp_path, "Test Links")
    assert card and card.name == "Test Links"
    assert sum(card.par.values()) == 72 and card.par[1] == 5 and card.par[6] == 3
    assert len(card.index) == 18 and card.index[6] == 1 and card.altitude_ft == 1010


def test_course_card_takes_the_folder_gspro_logged_when_two_names_match(tmp_path):
    from commentator import course_card

    for folder, name in (("Alpine Valley_GSP", "Swiss Alpine Valley"),
                         ("AlpineValley_Lidar_Scene", "Alpine Valley - Meadows & Sugar Creek")):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "coursedetails.txt").write_text(DETAILS.replace("Test Links", name), encoding="utf-8")
    assert course_card.load(tmp_path, "ALPINE VALLEY", folder="alpinevalley_lidar_scene").name == \
        "Alpine Valley - Meadows & Sugar Creek"
    assert course_card.load(tmp_path, "ALPINE VALLEY", folder="alpine valley_gsp").name == "Swiss Alpine Valley"
    # A stale folder (the last course's) whose name doesn't match the read is ignored.
    (tmp_path / "pinevalley_v1_gsp").mkdir()
    (tmp_path / "pinevalley_v1_gsp" / "coursedetails.txt").write_text(DETAILS.replace("Test Links", "Pine Valley"),
                                                                       encoding="utf-8")
    assert "Alpine" in course_card.load(tmp_path, "ALPINE VALLEY", folder="pinevalley_v1_gsp").name
    # Nor does it beat a better match: the last course was Swiss, this one's full name was read.
    assert course_card.load(tmp_path, "Alpine Valley - Meadows & Sugar Creek", folder="alpine valley_gsp").name == \
        "Alpine Valley - Meadows & Sugar Creek"


def test_a_prebuilt_rounds_card_is_refound_once_gspro_logs_the_course(tmp_path):
    for folder, name in (("Alpine Valley_GSP", "Swiss Alpine Valley"),
                         ("AlpineValley_Lidar_Scene", "Alpine Valley - Meadows & Sugar Creek")):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "coursedetails.txt").write_text(DETAILS.replace("Test Links", name), encoding="utf-8")
    e = _log_engine(tmp_path)
    e.cfg.source.gspro_courses_dirs = [str(tmp_path)]
    r = Round(league=LEAGUE, gspro_players=["MikeD"], course="ALPINE VALLEY", scheduled_format=None)
    e.gspro.course = "alpine valley_gsp"     # still the last course's: the setup was read before the new one logged
    r.apply_course_card(e._load_course_card("ALPINE VALLEY"))
    assert r.course_card.name == "Swiss Alpine Valley"
    e.gspro.course = "alpinevalley_lidar_scene"
    e._refresh_course_card(r, "ALPINE VALLEY")
    assert r.course_card.name == "Alpine Valley - Meadows & Sugar Creek"


def test_league_history_at_a_course_needs_the_same_gspro_course_not_a_shared_name(tmp_path, monkeypatch):
    from commentator.history import History

    for folder, name in (("Alpine Valley_GSP", "Swiss Alpine Valley"),
                         ("AlpineValley_Lidar_Scene", "Alpine Valley - Meadows & Sugar Creek")):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "coursedetails.txt").write_text(DETAILS.replace("Test Links", name), encoding="utf-8")
    e = _engine(tmp_path)
    e.cfg.source.gspro_courses_dirs = [str(tmp_path)]
    h = History({"current_year": 2026, "seasons": [{"year": 2025, "complete": True, "teams": [
        {"label": "Alex/Jamie", "players": ["Alex Rivera", "Jamie Avery"], "rank": 3,
         "weeks": [{"week": 6, "course": "Swiss Alpine Valley", "format": "scramble", "place": 4, "tied": False}]}]}]})

    def facts(course):
        r = Round(league=LEAGUE, gspro_players=["MikeD"], course=course, scheduled_format=None)
        r.apply_course_card(e._load_course_card(course))
        return " ".join(h.team_facts("Alex and Jamie", ["Alex Rivera", "Jamie Avery"], None, r.course,
                                     same_course=e._course_test(r)))

    assert "Swiss Alpine Valley" not in facts("Alpine Valley - Meadows & Sugar Creek")
    assert "played Swiss Alpine Valley in 2025" in facts("Swiss Alpine Valley")
    # Swiss not installed: its week isn't put on the nearest installed namesake, nor through a
    # schedule alias for a near name.
    import shutil
    shutil.rmtree(tmp_path / "Alpine Valley_GSP")
    monkeypatch.setitem(e.league.data, "schedule", [{"week": 1, "course": "Alpine Valley",
                                  "gspro_course": "Alpine Valley - Meadows & Sugar Creek"}])
    assert "Swiss Alpine Valley" not in facts("Alpine Valley - Meadows & Sugar Creek")
    # A week played under the schedule's name for the course still counts.
    h.seasons[0]["teams"][0]["weeks"][0]["course"] = "Alpine Valley"
    assert "played Alpine Valley in 2025" in facts("Alpine Valley - Meadows & Sugar Creek")
    # No card for the round: by name, as before.
    assert "played Alpine Valley" in " ".join(
        h.team_facts("Alex and Jamie", ["Alex Rivera", "Jamie Avery"], None, "Alpine Valley"))


def test_a_settings_menu_over_the_tee_doesnt_rename_the_course(tmp_path):
    e = _log_engine(tmp_path)
    hud = Hud(player_text="MikeD E Shot 1 400 YDS", shot_number=1, score_to_par="E", course="GAME MENU")
    e.gspro.mode, e.gspro.course_display = "course", "Alpine Valley - Meadows & Sugar Creek"
    assert e._tee_course(hud) == "Alpine Valley - Meadows & Sugar Creek"
    hud.course = "ALPINE VALLEY"   # the HUD's own short name for the loaded course is kept
    assert e._tee_course(hud) == "ALPINE VALLEY"
    e.gspro.mode = "menu"          # no course loaded: the HUD read stands
    hud.course = "GAME MENU"
    assert e._tee_course(hud) == "GAME MENU"


def test_a_new_course_selection_forgets_the_last_courses_loaded_name():
    from commentator.sources.gspro_log import GsproState, LogEvent

    g = GsproState()
    g.apply(LogEvent("course", course="pinevalley_v1_gsp"))
    g.apply(LogEvent("course_display", display="Pine Valley"))
    g.apply(LogEvent("course", course="alpinevalley_lidar_scene"))
    assert g.course_display is None


def test_round_seeds_par_and_index_from_the_course_card():
    from commentator.course_card import CourseCard

    card = CourseCard(name="Wynn", par={10: 3, 11: 5, 12: 3}, index={10: 10, 11: 2, 12: 16})
    r = Round(league=LEAGUE, gspro_players=["MikeD", "Sarah"], course="Wynn", scheduled_format="best ball",
              holes=9, start_hole=10)
    r.apply_course_card(card)
    # Back-nine round: the first hole of the round is hole 10.
    assert r.par[1] == 3 and r.index[1] == 10 and r.par[2] == 5 and r.index[2] == 2


def test_a_late_start_keeps_only_the_holes_the_round_plays():
    from commentator.course_card import CourseCard

    card = CourseCard(name="Riviera", par={h: 4 for h in range(1, 19)} | {16: 3, 17: 5},
                      index={h: h for h in range(1, 19)})
    r = Round(league=LEAGUE, gspro_players=["MikeD"], course="Riviera", scheduled_format="individual", holes=18)
    r.apply_course_card(card)
    r.start_hole, r.start_hole_assumed = 16, False
    r.apply_course_card(card)
    r.set_holes(3)
    assert r.par == {1: 3, 2: 5, 3: 4} and sorted(r.index) == [1, 2, 3]
    assert {r.hole_number(q) for q in r.par} == {16, 17, 18}


def test_abandoned_round_does_not_block_a_new_start(tmp_path):
    import time

    e = _engine(tmp_path)
    tee = Hud(player_text="MikeD E Shot 1 400.3 YDS", shot_number=1, score_to_par="E", to_pin="400.3 yards",
              others=[OtherPlayer("Sarah", "400.3 yards", "E")])
    e.round = Round(league=LEAGUE, gspro_players=["MikeD", "Sarah"], course="Wynn", scheduled_format="best ball")
    assert not e.round.ended
    # A round still in progress and recently active: the next tee after an all-par hole is not a start.
    e._prev_activity = time.monotonic()
    e._holes_played, e._holes_played_at = 3, time.monotonic()
    assert not e._is_round_start(tee)
    # A just-read scorecard with no holes played means GSPro reset to a fresh round.
    e._holes_played, e._holes_played_at = 0, time.monotonic()
    assert e._is_round_start(tee)
    # Or a long quiet gap before this tee, i.e. the previous round was abandoned, not finished.
    e._holes_played, e._holes_played_at = 3, time.monotonic()
    e._prev_activity = time.monotonic() - e.cfg.commentary.round_restart_idle_seconds - 1
    assert e._is_round_start(tee)


async def test_alternate_shot_round_speaks_of_the_team(tmp_path):
    from commentator.models import RoundStartEvent

    e = _engine(tmp_path)
    e.current_par = 4
    e.current_course = "Wynn"  # week 3: alternate shot, one ball
    await e.handle_round_start(RoundStartEvent(hud=Hud(player_text="MikeD E Shot 1 400.3 YDS", shot_number=1,
                                                       score_to_par="E", to_pin="400.3 yards")))
    assert e.round.format == "alternate" and e.round.is_league
    opener = e.live_llm.prompts[-1]
    assert "THE ROUND IS ABOUT TO START" in opener and "Alternate shot" in opener
    assert e._display("Mike") == "Aces"
    assert e._who("Mike")[0] == "Aces (Mike and Sarah, one ball)"
    # The team is offered in the header, but naming an individual is allowed (not banned): forcing
    # team-only wording rejected a line on nearly every call and drove a costly retry.
    assert e._banned_names() == []


async def test_wrap_states_the_finished_hole_score_from_the_scorecard(tmp_path):
    from commentator.models import RoundStartEvent

    e = _engine(tmp_path)
    e.current_par = 4
    e.current_course = "Wynn"  # week 3: alternate shot, one ball
    await e.handle_round_start(RoundStartEvent(hud=Hud(player_text="MikeD E Shot 1 400.3 YDS", shot_number=1,
                                                       score_to_par="E", to_pin="400.3 yards")))
    r = e.round
    r.par[1] = 4
    assert r.apply_scorecard(Scorecard(holes=list(range(1, 19)), scores={"MikeD": {1: 5}}))
    r.seq = 2  # play has moved to hole 2, so hole 1 just finished
    facts = e._hole_score_facts("Hole 1, par 4, 400 yards")  # par comes from the hole's HUD label
    # From the scorecard (no banner needed): a 5 on a par 4 is a bogey, stated with the numbers so
    # the grounding check lets the wrap say "bogey" / "one over".
    assert facts and "made 5" in facts[0] and "par-4" in facts[0] and "1 over" in facts[0] and "bogey" in facts[0]


async def test_pre_made_wrap_branch_plays_for_the_scored_result(tmp_path):
    from commentator.models import Line, RoundStartEvent

    e = _engine(tmp_path)
    e.current_par = 4
    e.current_course = "Wynn"  # week 3: alternate shot, one ball
    await e.handle_round_start(RoundStartEvent(hud=Hud(player_text="MikeD E Shot 1 400.3 YDS", shot_number=1,
                                                       score_to_par="E", to_pin="400.3 yards")))
    r = e.round
    r.par[1] = 4
    r.apply_scorecard(Scorecard(holes=list(range(1, 19)), scores={"MikeD": {1: 5}}))
    r.seq = 2
    e.state.current_hole = "Hole 1, par 4, 400 yards"
    # Branches pre-voiced during the putt, keyed by the score the deciding side might come in on.
    e._wrap_branches = {"hole": e.state.current_hole, "side": r.sides[0].key, "by_score": {
        4: ([Line("pbp", "A tidy par to open.")], [b"clip"]),
        5: ([Line("pbp", "A bogey five to start.")], [b"clip"]),
    }}
    # The scorecard says 5 at seq 1, so the 5 branch plays and the on-demand wrap is skipped.
    assert e._play_prepared_wrap("Hole 1, par 4, 400 yards", 1) is True
    assert e._wrap_branches is None  # consumed
    assert e._wrapped_hole == "Hole 1, par 4, 400 yards"  # so the hole change won't repeat it
    # No branch for the scored result -> fall back to the on-demand wrap.
    e._wrap_branches = {"hole": e.state.current_hole, "side": r.sides[0].key,
                        "by_score": {3: ([Line("pbp", "A birdie!")], [b"clip"])}}
    assert e._play_prepared_wrap("Hole 1, par 4, 400 yards", 1) is False


async def test_wrap_fires_on_the_scorecard_not_only_the_hole_change(tmp_path):
    from commentator.models import Line, RoundStartEvent

    e = _engine(tmp_path)
    e.current_par = 4
    e.current_course = "Wynn"  # week 3: alternate shot, one ball
    await e.handle_round_start(RoundStartEvent(hud=Hud(player_text="MikeD E Shot 1 400.3 YDS", shot_number=1,
                                                       score_to_par="E", to_pin="400.3 yards")))
    r = e.round  # hole 1 in progress, seq 1
    r.par[1] = 4
    r.apply_scorecard(Scorecard(holes=list(range(1, 19)), scores={"MikeD": {1: 4}}))  # holed out for par
    e.state.current_hole = "par 4, 400 yards"
    e._wrap_branches = {"hole": "par 4, 400 yards", "side": r.sides[0].key,
                        "by_score": {4: ([Line("pbp", "Par.")], [b"clip"]), 5: ([Line("pbp", "Bogey.")], [b"clip"])}}
    e.speech.is_idle = lambda: True  # the opener queued above never plays here: don't wait it out
    # Scorecard posts the score while the hole is still "current" (seq 1), before any hole change.
    assert await e._wrap_after_last_putt("par 4, 400 yards", r.seq) is True
    assert e._wrapped_hole == "par 4, 400 yards"  # the hole change will now skip it


async def test_tee_order_assigns_gspro_indexes_when_setup_slots_missing_or_wrong(tmp_path):
    from commentator.models import RoundStartEvent

    e = _engine(tmp_path)
    e.cfg.source.gspro_log_truth = True
    e.current_par = 4
    e.current_course = "Pebble Beach"  # best ball, two balls
    tee = Hud(player_text="Sarah E Shot 1 400 YDS", shot_number=1, score_to_par="E", to_pin="400 yards",
              others=[OtherPlayer("MikeD", "400 yards", "E")])
    # No setup-screen read at all: the tee's top-to-bottom order (up first, then the others) is the index.
    await e.handle_round_start(RoundStartEvent(hud=tee))
    assert e.round.slot_names == ["Sarah", "MikeD"]
    assert e._gspro_idx("Sarah") == 0 and e._gspro_idx("Mike") == 1

    # Setup slots that disagree with the tee (a misread "Connor") give way to the tee order.
    r = e._build_round(["Toby", "Connor"], "Pebble Beach", {}, False, slot_names=["Toby", "Connor"])
    e._reconcile_slots(r, ["Sarah", "MikeD"])
    assert r.slot_names == ["Sarah", "MikeD"]
    # Setup slots that cover everyone on the tee are kept (they also place empty slots/guests exactly).
    r = e._build_round(["MikeD", "Sarah"], "Pebble Beach", {}, False, slot_names=["MikeD", None, "Sarah"])
    e._reconcile_slots(r, ["Sarah", "MikeD"])
    assert r.slot_names == ["MikeD", None, "Sarah"]


async def test_multiplayer_wrap_pre_builds_for_the_last_side_still_out(tmp_path):
    from commentator.models import RoundStartEvent, ScorecardEvent

    e = _engine(tmp_path)
    e.current_par = 4
    e.current_course = "Pebble Beach"  # week 4: best ball, both partners play their own ball
    tee = Hud(player_text="MikeD E Shot 1 400 YDS", shot_number=1, score_to_par="E", to_pin="400 yards",
              others=[OtherPlayer("Sarah", "400 yards", "E")])
    await e.handle_round_start(RoundStartEvent(hud=tee))
    r = e.round
    assert len(r.sides) == 2 and r.is_league  # two sides, both Aces: Mike and Sarah
    r.par[1] = 4
    e.state.current_hole = "par 4, 400 yards"
    mike = next(s for s in r.sides if "MikeD" in s.gspro)
    sarah = next(s for s in r.sides if "Sarah" in s.gspro)
    # Capture the pre-build call (skip the real model/TTS) to check the decision, not the generation.
    calls = []
    e._kick_wrap_branches = lambda side_key, scores, other: calls.append((side_key, scores, other))
    # Mike has holed out on hole 1, Sarah hasn't: the second-to-last is in, one side left to putt.
    await e.handle_scorecard(ScorecardEvent(card=Scorecard(holes=list(range(1, 19)),
                                                           scores={"MikeD": {1: 4}, "Sarah": {}})))
    assert calls, "the second-to-last holing out should trigger a pre-build"
    side_key, scores, other = calls[-1]
    assert side_key == sarah.key  # branched on the side still to putt
    assert set(scores) == {3, 4, 5, 6}  # par-1..par+2 for a par 4
    assert any("made 4" in f for f in other)  # Mike's known score is fixed in the facts
    assert mike.key != sarah.key
    assert r.card_score(mike, 1) == 4


def test_classify_round_separates_league_from_practice(tmp_path):
    e = _engine(tmp_path)
    # League rounds: a scheduled course, in date, the right ball count, one registered team.
    entry, practice = e._classify_round(["MikeD"], "Wynn")  # week 3: alternate shot, one ball
    assert practice is None and entry["week"] == 3
    entry, practice = e._classify_round(["MikeD", "Sarah"], "Pebble Beach")  # week 4: best ball, two partners
    assert practice is None and entry["week"] == 4
    # A course we aren't playing this year is practice, even on a scheduled date.
    _, practice = e._classify_round(["MikeD"], "Augusta National")
    assert practice and "schedule" in practice
    # Wrong ball count for the format: best ball shows two balls, not one.
    _, practice = e._classify_round(["MikeD"], "Pebble Beach")
    assert practice and "ball" in practice
    # The two players aren't partners on one team (Mike is an Ace, Tony a Duffer).
    _, practice = e._classify_round(["MikeD", "Tony"], "Pebble Beach")
    assert practice and "partners" in practice
    # A stranger who isn't in the league at all.
    _, practice = e._classify_round(["MikeD", "Rando"], "Pebble Beach")
    assert practice and "Rando" in practice


async def test_a_practice_booking_makes_a_league_looking_round_practice(tmp_path, monkeypatch, caplog):
    import logging
    from datetime import datetime

    import httpx

    from commentator import booking
    from commentator.booking import Bookings

    rows = [{"start": "2026-10-21T17:00", "finish": "2026-10-21T18:00", "field_1_r": "Practice", "deleted": False},
            {"start": "2026-10-21T19:00", "finish": "2026-10-21T21:00", "field_1_r": "League Week 1", "deleted": False},
            {"start": "2026-10-21T18:00", "finish": "2026-10-21T19:00", "field_1_r": "Practice", "deleted": True}]
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"bookings": rows})

    real = httpx.AsyncClient
    monkeypatch.setattr(booking.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    e = _engine(tmp_path)
    e.bookings = b = Bookings("GolfShed", 792418, "k", slack_minutes=30)
    with caplog.at_level(logging.INFO, logger="commentator.booking"):
        e._fetch_bookings("setup screen")      # in the background, as a setup screen comes up
        await e._bookings_ready()              # and a round classified now waits for it to land
    assert seen["auth"].startswith("Basic ") and len(b.bookings) == 2   # the deleted one is dropped
    assert "SuperSaaS bookings (setup screen) in " in caplog.text
    with caplog.at_level(logging.INFO, logger="commentator.booking"):
        await e._bookings_ready()              # fresh: no second fetch
        assert "(round start)" not in caplog.text
        e._bookings_fetched -= 601             # no setup screen seen in a while (preround off): fetch now
        await e._bookings_ready()
    assert "SuperSaaS bookings (round start) in " in caplog.text

    monkeypatch.setattr(b, "at", lambda: Bookings.at(b, datetime(2026, 10, 21, 18, 20)))   # just over a practice
    _, practice = e._classify_round(["MikeD"], "Wynn")
    assert practice and "booked for practice" in practice
    monkeypatch.setattr(b, "at", lambda: Bookings.at(b, datetime(2026, 10, 21, 18, 45)))   # nearer the league block
    entry, practice = e._classify_round(["MikeD"], "Wynn")
    assert practice is None and entry["week"] == 3
    monkeypatch.setattr(b, "at", lambda: Bookings.at(b, datetime(2026, 10, 21, 22, 0)))    # no booking: as before
    assert e._classify_round(["MikeD"], "Wynn")[1] is None
    e.cfg.commentary.round_week = 3                                     # a forced week beats the booking
    monkeypatch.setattr(b, "at", lambda: Bookings.at(b, datetime(2026, 10, 21, 17, 30)))
    assert e._classify_round(["MikeD"], "Wynn")[1] is None
    e.cfg.commentary.round_week = None

    # A league opener written before the practice booking was read doesn't air over the load.
    from commentator.engine import PendingOpening

    e._pending_opening = PendingOpening("Wynn", frozenset({"Aces"}), frozenset({"Mike"}), True, None, [],
                                        awaiting_load=True, roster_key=e._roster_key(["MikeD"]))
    assert not e._front_loaded_fits("Wynn", ["MikeD"])
    monkeypatch.setattr(b, "at", lambda: Bookings.at(b, datetime(2026, 10, 21, 22, 0)))
    assert e._front_loaded_fits("Wynn", ["MikeD"])

    # SuperSaaS unreachable: the last list is dropped, not trusted, so the schedule decides.
    def down(request):
        return httpx.Response(503)

    monkeypatch.setattr(booking.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(down), **kw))
    await b.refresh(datetime(2026, 10, 21, 17, 30))
    assert b.bookings is None and Bookings.at(b, datetime(2026, 10, 21, 17, 30)) is None


def test_classify_round_rejects_a_round_past_its_due_date(tmp_path):
    e = _engine(tmp_path)
    e.league = League({"teams": [{"name": "Aces", "players": [
        {"name": "Mike", "gspro_name": "MikeD"}, {"name": "Sarah"}]}],
        "schedule": [{"week": 1, "course": "Wynn", "format": "Alternate", "end_date": "2000-01-01"}]})
    _, practice = e._classify_round(["MikeD"], "Wynn")
    assert practice and "due by" in practice


async def test_practice_round_is_called_without_league_framing(tmp_path):
    from commentator.models import RoundStartEvent

    e = _engine(tmp_path)
    e.current_par = 4
    e.current_course = "Augusta National"  # not on this year's schedule
    await e.handle_round_start(RoundStartEvent(hud=Hud(player_text="MikeD E Shot 1 400 YDS", shot_number=1,
                                                       score_to_par="E", to_pin="400 yards")))
    assert e.round is not None and not e.round.is_league
    opener = e.live_llm.prompts[-1]
    assert "practice round" in opener.lower()  # the booth knows it's practice
    assert "judged on the net score" not in opener  # no handicap-league framing


def test_play_button_course_reads_the_setup_button():
    from commentator.engine import _play_button_course

    assert _play_button_course("Play Pebble Beach") == "Pebble Beach"
    assert _play_button_course("PLAY BALSAMS PANORAMA!") == "BALSAMS PANORAMA"  # real button text
    assert _play_button_course("PLAY  Wynn Golf Club") == "Wynn Golf Club"
    assert _play_button_course("Settings") is None   # a different screen/button
    assert _play_button_course("Play") is None        # the button before a course is chosen
    assert _play_button_course("") is None


def test_setup_slot_name_drops_the_empty_add_button():
    from commentator.engine import _setup_slot_name

    assert _setup_slot_name(["Alex"]) == "Alex"
    assert _setup_slot_name(["  Jamie "]) == "Jamie"
    assert _setup_slot_name(["Alex Rivera"]) == "Alex Rivera"   # first + last on one line is one name
    # An empty slot's centered "+" OCRs to a stray mark; none of these is a name.
    assert _setup_slot_name(["+"]) is None
    assert _setup_slot_name(["1"]) is None
    assert _setup_slot_name(["C"]) is None   # a lone letter clipped off the "+"
    assert _setup_slot_name(["上"]) is None
    assert _setup_slot_name([]) is None
    # An open name dropdown stacks several players over the slot; reject it rather than merge them.
    assert _setup_slot_name(["Alex", "Jamie", "Morgan"]) is None
    assert _setup_slot_name(["Alex Rivera", "Jamie Stone"]) is None


async def test_preround_opener_airs_during_the_load_and_the_tee_only_sets_up(tmp_path):
    from commentator.models import RoundStartEvent

    e = _engine(tmp_path)
    e.current_par = 4
    # Setup screen read, Play pressed: both Aces, Pebble Beach (week 4). The opener airs now, over the
    # load, and the round is kept pending for the tee.
    await e._prepare_speculative_round("Pebble Beach", ["MikeD", "Sarah"])
    assert e._pending_opening is not None and e._pending_opening.aired
    assert e._pending_opening.teams == frozenset({"Aces"}) and e._pending_opening.is_league
    aired_text = e._pending_opening.lines[0].text
    transcript_path = next(tmp_path.glob("transcript-*.txt"))
    assert aired_text in transcript_path.read_text()  # aired during the load, not at the tee
    prompts_before_tee = len(e.live_llm.prompts)

    # At the tee the same round is confirmed: it's set up, with no second opener and no model call.
    e.current_course = "Pebble Beach"
    tee = Hud(player_text="MikeD E Shot 1 400 YDS", shot_number=1, score_to_par="E", to_pin="400 yards",
              others=[OtherPlayer("Sarah", "400 yards", "E")])
    await e.handle_round_start(RoundStartEvent(hud=tee))
    assert e._pending_opening is None  # consumed
    assert e.round is not None and e.round.is_league and e.round.course == "Pebble Beach"
    assert len(e.live_llm.prompts) == prompts_before_tee  # no tee-time generation
    assert transcript_path.read_text().count(aired_text) == 1  # opener aired once, not twice


async def test_a_superseded_front_load_does_not_revive_the_pending_opener(tmp_path):
    # The double-air race: a front-load is still composing when the opener is committed (aired over the
    # load, or taken at the tee), which bumps _opener_gen. The in-flight compose must then bail at its
    # gen guard rather than overwrite _pending_opening with a fresh awaiting_load object that the tee
    # would air as a second opener.
    e = _engine(tmp_path)
    e.current_par = 4
    e._opener_gen = 8          # the committed opener bumped the gen past this compose's gen
    e._pending_opening = None
    await e._prepare_speculative_round("Pebble Beach", ["MikeD", "Sarah"], air=False, gen=7)
    assert e._pending_opening is None   # stale gen bailed; nothing revived to double-air at the tee
    # The load-screen recompose path (air=True) carries the gen too: a tee that superseded it mid-compose
    # bumps the gen, so this one bails before airing a second opener over the load.
    e._opener_gen = 8
    e._pending_opening = None
    await e._prepare_speculative_round("Pebble Beach", ["MikeD", "Sarah"], air=True, gen=7)
    assert e._pending_opening is None   # bailed before airing; no second opener


async def test_no_second_opener_when_an_occluded_tee_drops_a_partner(tmp_path):
    from commentator.models import RoundStartEvent

    e = _engine(tmp_path)
    e.current_par = 4
    # A league best-ball opener airs over the load for both Aces.
    await e._prepare_speculative_round("Pebble Beach", ["MikeD", "Sarah"])
    assert e._pending_opening is not None and e._pending_opening.aired
    prompts_before_tee = len(e.live_llm.prompts)

    # The tee HUD reads only MikeD (Sarah's row occluded, e.g. a window over GSPro), so it classifies
    # practice and the aired league opener doesn't match - but the tee roster is a subset of what aired,
    # so no second opener is generated (the aired one stands).
    e.current_course = "Pebble Beach"
    tee = Hud(player_text="MikeD E Shot 1 400 YDS", shot_number=1, score_to_par="E", to_pin="400 yards")
    await e.handle_round_start(RoundStartEvent(hud=tee))
    assert e._pending_opening is None
    assert len(e.live_llm.prompts) == prompts_before_tee  # no second opener at the tee


async def test_an_opener_aired_for_another_course_does_not_stand_in_at_the_tee(tmp_path):
    from commentator.models import RoundStartEvent

    e = _engine(tmp_path)
    e.current_par = 4
    # An opener airs over the Pebble Beach load, then the player backs out and tees off somewhere else.
    await e._prepare_speculative_round("Pebble Beach", ["MikeD", "Sarah"])
    assert e._pending_opening is not None and e._pending_opening.aired
    prompts_before_tee = len(e.live_llm.prompts)
    e.current_course = "Kapalua - Plantation Course"
    tee = Hud(player_text="MikeD E Shot 1 400 YDS", shot_number=1, score_to_par="E", to_pin="400 yards")
    await e.handle_round_start(RoundStartEvent(hud=tee))
    assert e._pending_opening is None
    assert len(e.live_llm.prompts) > prompts_before_tee  # this course gets its own opener


async def test_preround_opener_is_set_aside_when_the_tee_differs(tmp_path):
    from commentator.models import RoundStartEvent

    e = _engine(tmp_path)
    e.current_par = 4
    await e._prepare_speculative_round("Pebble Beach", ["MikeD", "Sarah"])  # Aces prepared
    assert e._pending_opening is not None
    prompts_before_tee = len(e.live_llm.prompts)

    # A different team is on the tee (the roster changed after we read the setup screen): the held
    # opener doesn't fit, so it's dropped and a fresh one is made at the tee, exactly as before.
    e.current_course = "Pebble Beach"
    tee = Hud(player_text="Tony E Shot 1 400 YDS", shot_number=1, score_to_par="E", to_pin="400 yards",
              others=[OtherPlayer("Priya", "400 yards", "E")])
    await e.handle_round_start(RoundStartEvent(hud=tee))
    assert e._pending_opening is None  # consumed
    assert e.round is not None and {t for t in e.round.teams} == {"Duffers"}
    assert len(e.live_llm.prompts) > prompts_before_tee  # a tee-time opener was generated


async def test_preround_opener_survives_the_gspro_course_alias(tmp_path):
    from commentator.models import RoundStartEvent

    e = _engine(tmp_path)
    e.current_par = 4
    # GSPro names this course "Balsams Panorama"; the league calls it Wentworth by the Sea.
    e.league = League({"teams": [{"name": "Aces", "players": [
        {"name": "Mike", "gspro_name": "MikeD"}, {"name": "Sarah"}]}],
        "schedule": [{"week": 7, "course": "Wentworth by the Sea", "gspro_course": "Balsams Panorama",
                      "format": "Best Ball", "hole_count": 9, "start_hole": 1, "end_date": "2099-01-01"}]})
    # The setup screen reads GSPro's name; the held opener should still say the league's name.
    await e._prepare_speculative_round("Balsams Panorama", ["MikeD", "Sarah"])
    assert e._pending_opening is not None
    assert e._pending_opening.round.course == "Wentworth by the Sea"
    prompts_before_tee = len(e.live_llm.prompts)

    # The HUD shows the course's real name, a different string, but the same week: the opener airs.
    e.current_course = "Wentworth By The Sea"
    tee = Hud(player_text="MikeD E Shot 1 400 YDS", shot_number=1, score_to_par="E", to_pin="400 yards",
              others=[OtherPlayer("Sarah", "400 yards", "E")])
    await e.handle_round_start(RoundStartEvent(hud=tee))
    assert e._pending_opening is None
    assert e.round is not None and e.round.course == "Wentworth by the Sea"
    assert len(e.live_llm.prompts) == prompts_before_tee  # aired the prepared opener, no tee-time call


async def test_preround_preloads_a_practice_opener_and_airs_it(tmp_path):
    from commentator.models import RoundStartEvent

    e = _engine(tmp_path)
    e.current_par = 4
    # Two balls on Wynn, which is alternate shot (one ball): a practice round, but still preloaded.
    await e._prepare_speculative_round("Wynn", ["MikeD", "Sarah"])
    assert e._pending_opening is not None and not e._pending_opening.is_league
    held = e._pending_opening.lines[0].text
    prompts_before_tee = len(e.live_llm.prompts)

    # The tee confirms the same practice round (same course, same players): the opener airs as-is.
    e.current_course = "Wynn"
    tee = Hud(player_text="MikeD E Shot 1 400 YDS", shot_number=1, score_to_par="E", to_pin="400 yards",
              others=[OtherPlayer("Sarah", "400 yards", "E")])
    await e.handle_round_start(RoundStartEvent(hud=tee))
    assert e._pending_opening is None
    assert e.round is not None and not e.round.is_league
    assert len(e.live_llm.prompts) == prompts_before_tee  # no tee-time generation
    assert held in next(tmp_path.glob("transcript-*.txt")).read_text()


async def test_preround_preloads_an_off_schedule_practice_opener(tmp_path):
    e = _engine(tmp_path)
    # A course that isn't on this year's schedule is a practice round; its opener still preloads.
    await e._prepare_speculative_round("Augusta National", ["MikeD", "Sarah"])
    assert e._pending_opening is not None and not e._pending_opening.is_league


async def test_setup_watcher_prepares_the_opener_when_the_screen_clears(tmp_path):
    import asyncio

    e = _engine(tmp_path)
    e.cfg.commentary.preround_poll_seconds = 0.01
    # The setup screen is up for two polls (course + roster), then clears when Play is pressed.
    # slot_3 is an empty "+" that OCR'd to junk (as seen live: 'nemory...'); it must be dropped, not
    # treated as a third player that would sink the classification.
    reads = [
        {"setup_course": "Play Pebble Beach", "setup_slot_1": "MikeD", "setup_slot_2": "Sarah", "setup_slot_3": "nemory..."},
        {"setup_course": "Play Pebble Beach", "setup_slot_1": "MikeD", "setup_slot_2": "Sarah", "setup_slot_3": "nemory..."},
        {"load_course": "Pebble Beach"},  # the load screen for the course that was set up
    ]

    class FakeScreen:
        def __init__(self):
            self.i = 0

        def grab_and_read(self, names):  # one poll: grab + gate regions; the read dict is the "frame"
            r = reads[min(self.i, len(reads) - 1)]
            self.i += 1
            return r, {n: r.get(n, "") for n in names}

        def read_region_lines(self, names, image):  # slots read from the same frame, one line each
            return {n: ([v] if (v := image.get(n, "").strip()) else []) for n in names}

        def read_region_lines_cached(self, names, image, cache):
            return self.read_region_lines(names, image), cache

    e.screen = FakeScreen()
    task = asyncio.create_task(e._watch_setup())
    for _ in range(200):
        await asyncio.sleep(0.01)
        if e._pending_opening is not None:
            break
    task.cancel()
    assert e._pending_opening is not None
    assert e._pending_opening.course == "Pebble Beach"
    assert e._pending_opening.teams == frozenset({"Aces"})


async def test_setup_watcher_airs_a_practice_opener_over_the_load(tmp_path):
    import asyncio

    e = _engine(tmp_path)
    e.cfg.commentary.preround_poll_seconds = 0.01
    # Wynn + these two is a practice round (not a scheduled league pairing). Practice airs over the load
    # screen exactly like league: both round types behave the same way.
    reads = [
        {"setup_course": "Play Wynn", "setup_slot_1": "MikeD", "setup_slot_2": "Sarah"},
        {"setup_course": "Play Wynn", "setup_slot_1": "MikeD", "setup_slot_2": "Sarah"},
        {"load_course": "Wynn"},  # the course's load screen: Play was pressed
    ]

    class FakeScreen:
        def __init__(self):
            self.i = 0

        def grab_and_read(self, names):
            r = reads[min(self.i, len(reads) - 1)]
            self.i += 1
            return r, {n: r.get(n, "") for n in names}

        def read_region_lines(self, names, image):
            return {n: ([v] if (v := image.get(n, "").strip()) else []) for n in names}

        def read_region_lines_cached(self, names, image, cache):
            return self.read_region_lines(names, image), cache

    e.screen = FakeScreen()
    task = asyncio.create_task(e._watch_setup())
    for _ in range(300):
        await asyncio.sleep(0.01)
        if e._pending_opening is not None and e._pending_opening.aired:
            break
    task.cancel()
    p = e._pending_opening
    assert p is not None and not p.is_league  # a practice round
    assert p.aired and not p.awaiting_load     # aired over the load, not held for the tee
    assert p.lines[0].text in next(tmp_path.glob("transcript-*.txt")).read_text()


async def test_setup_watcher_keeps_the_roster_across_a_settings_tab(tmp_path):
    import asyncio

    e = _engine(tmp_path)
    e.cfg.commentary.preround_poll_seconds = 0.01
    # Add players on the Players tab, switch to Match Settings (the Play button stays, but the slots
    # now read settings labels), then hit Play from there. The roster must survive.
    reads = [
        {"setup_course": "Play Pebble Beach", "setup_slot_1": "MikeD", "setup_slot_2": "Sarah"},
        {"setup_course": "Play Pebble Beach", "setup_slot_1": "MikeD", "setup_slot_2": "Sarah"},
        {"setup_course": "Play Pebble Beach", "setup_slot_1": "Stroke Play", "setup_slot_2": "18 Holes"},
        {"setup_course": "Play Pebble Beach", "setup_slot_1": "Handicap", "setup_slot_2": "Tees"},
        {"load_course": "Pebble Beach"},  # Play hit from the settings tab: the course's load screen
    ]

    class FakeScreen:
        def __init__(self):
            self.i = 0

        def grab_and_read(self, names):
            r = reads[min(self.i, len(reads) - 1)]
            self.i += 1
            return r, {n: r.get(n, "") for n in names}

        def read_region_lines(self, names, image):
            return {n: ([v] if (v := image.get(n, "").strip()) else []) for n in names}

        def read_region_lines_cached(self, names, image, cache):
            return self.read_region_lines(names, image), cache

    e.screen = FakeScreen()
    task = asyncio.create_task(e._watch_setup())
    for _ in range(300):
        await asyncio.sleep(0.01)
        if e._pending_opening is not None:
            break
    task.cancel()
    assert e._pending_opening is not None
    assert e._pending_opening.teams == frozenset({"Aces"})  # the settings-tab reads didn't clobber it


async def test_setup_watcher_ignores_a_popup_over_the_setup_screen(tmp_path):
    import asyncio

    e = _engine(tmp_path)
    e.cfg.commentary.preround_poll_seconds = 0.01
    # A window pops up over the setup screen: it hides the Play button and covers the slots, but it is
    # NOT the course's load screen. The opener must not fire on that; it fires only when the real load
    # screen appears.
    reads = [
        {"setup_course": "Play Pebble Beach", "setup_slot_1": "MikeD", "setup_slot_2": "Sarah"},
        {"setup_course": "Windows Update", "setup_slot_1": "Restart now", "load_course": "Windows Update"},
        {"setup_course": "Windows Update", "setup_slot_1": "Restart now", "load_course": "Notification"},
    ]

    class FakeScreen:
        def __init__(self):
            self.i = 0

        def grab_and_read(self, names):
            r = reads[min(self.i, len(reads) - 1)]
            self.i += 1
            return r, {n: r.get(n, "") for n in names}

        def read_region_lines(self, names, image):
            return {n: ([v] if (v := image.get(n, "").strip()) else []) for n in names}

        def read_region_lines_cached(self, names, image, cache):
            return self.read_region_lines(names, image), cache

    e.screen = FakeScreen()
    task = asyncio.create_task(e._watch_setup())
    for _ in range(60):
        await asyncio.sleep(0.01)
    task.cancel()
    # The opener may be front-loaded (composed, held) from the real setup read, but the popup never
    # looked like the course load screen, so nothing is AIRED over it.
    p = e._pending_opening
    assert p is None or (not p.aired and p.awaiting_load)


class _PixelSetupScreen:
    """A setup screen with pixel tones: each frame is a dict of region text plus "tones" (region ->
    Tone). Regions without a tone read as an empty "+" slot; the PLAYERS tab is dark unless given.
    Records every OCR'd region so tests can check which slots were (not) read."""

    def __init__(self, reads):
        self.reads, self.i, self.ocred = reads, 0, []

    def setup_frame(self):
        r = self.reads[min(self.i, len(self.reads) - 1)]
        self.i += 1
        return r

    def region_tones(self, names, image):
        t = image.get("tones", {})
        return {n: t.get(n, _EMPTY) for n in names}

    def read_region_lines_cached(self, names, image, cache):
        self.ocred.extend(names)
        return {n: ([v] if (v := str(image.get(n, "")).strip()) else []) for n in names}, cache


from commentator.screen import Tone  # noqa: E402

# Measured on the sim PC's GSPro screenshots (see engine._slot_state).
_TAB_ON, _TAB_OFF = Tone(242, 255, 0.9), Tone(28, 20, 0.0)
_PILL, _EMPTY, _LIST = Tone(121, 122, 0.01), Tone(32, 29, 0.0), Tone(200, 238, 0.59)


def _players_tab(course, names, tab=_TAB_ON, **extra_tones):
    """A PLAYERS-tab frame: `names` maps slot number -> name (each a filled pill)."""
    frame = {"setup_course": f"Play {course}", **{f"setup_slot_{i}": n for i, n in names.items()}}
    frame["tones"] = {"setup_players_tab": tab, **{f"setup_slot_{i}": _PILL for i in names}, **extra_tones}
    return frame


async def _run_setup_watch(e, reads, until, polls=300, screen=None):
    import asyncio

    e.cfg.commentary.preround_poll_seconds = 0.01
    e.screen = screen or _PixelSetupScreen(reads)
    task = asyncio.create_task(e._watch_setup())
    for _ in range(polls):
        await asyncio.sleep(0.01)
        if until(e._pending_opening):
            break
    task.cancel()
    return e._pending_opening


def _engine_with(tmp_path, league):
    from commentator.sources.replay import ReplaySource
    from commentator.speech import SpeechQueue
    from commentator.tts.console_tts import ConsoleTTS
    from tests.test_core import FakeLLM

    cfg = Config(log_dir=str(tmp_path))
    llm = FakeLLM(cfg.llm.live)
    return Engine(cfg=cfg, league=league, source=ReplaySource("data/sample_shots.jsonl"), live_llm=llm,
                  segment_llm=llm, speech=SpeechQueue(cfg, ConsoleTTS()), rng=random.Random(0))


async def test_setup_watcher_includes_a_guest_with_league_players(tmp_path):
    # Two league players plus a guest on the PLAYERS tab; slot 4 is an empty "+" whose OCR would be junk.
    frame = _players_tab("Augusta National", {1: "MikeD", 2: "Sarah", 3: "Guesty"})
    frame["setup_slot_4"] = "nemory..."
    e = _engine(tmp_path)
    scr = _PixelSetupScreen([frame, frame, {"load_course": "Augusta National"}])
    p = await _run_setup_watch(e, None, until=lambda p: p is not None and p.aired, screen=scr)
    assert p is not None and p.aired
    assert p.roster_key == frozenset({"Mike", "Sarah", "Guesty"})   # the guest is in, the empty slot isn't
    assert p.players == frozenset({"Mike", "Sarah"})                # league players, as the tee checks them
    assert "setup_slot_4" not in scr.ocred                          # an empty slot is never OCR'd


async def test_backing_out_after_an_opener_aired_reads_the_next_setup(tmp_path):
    # An opener airs over the Augusta load; the player backs out before the tee, sets up Pebble Beach
    # and presses Play. Pebble gets its own opener (the watch didn't stay parked on Augusta's).
    augusta = _players_tab("Augusta National", {1: "MikeD", 2: "Sarah"})
    pebble = _players_tab("Pebble Beach", {1: "MikeD", 2: "Sarah"})
    loading = {"load_course": "Augusta National"}
    e = _engine(tmp_path)
    scr = _PixelSetupScreen([augusta, augusta, loading, loading, {}, {}, pebble, pebble,
                             {"load_course": "Pebble Beach"}])
    aired = []

    def until(p):
        if p is not None and p.aired and p.course not in aired:
            aired.append(p.course)
        return "Pebble Beach" in aired
    p = await _run_setup_watch(e, None, screen=scr, until=until)
    assert aired == ["Augusta National", "Pebble Beach"] and p.aired


async def test_setup_watcher_fires_the_opener_with_no_league(tmp_path):
    from commentator.league import League

    frame = _players_tab("Pebble Beach", {1: "Alex", 2: "Jamie"})
    e = _engine_with(tmp_path, League({}))
    p = await _run_setup_watch(e, [frame, frame, {"load_course": "Pebble Beach"}],
                               until=lambda p: p is not None and p.aired)
    assert p is not None and p.aired and not p.is_league
    assert p.roster_key == frozenset({"Alex", "Jamie"})
    assert p.lines[0].text in next(tmp_path.glob("transcript-*.txt")).read_text()


async def test_a_setup_that_never_shows_the_players_tab_uses_the_last_roster_held_for_the_tee(tmp_path):
    from commentator.models import RoundStartEvent

    # An earlier setup read MikeD + a guest; this one goes straight from another tab to Play, so no
    # players are read. The opener is made for the last roster and held (not aired over the load).
    e = _engine(tmp_path)
    first = _players_tab("Augusta National", {1: "MikeD", 2: "Guesty"})
    await _run_setup_watch(e, [first, first], until=lambda p: p is not None, polls=100)
    e._pending_opening, e._opener_prepared_for = None, frozenset()
    holes_tab = _players_tab("Pebble Beach", {}, tab=_TAB_OFF)
    p = await _run_setup_watch(e, [holes_tab, {"load_course": "Pebble Beach"}],
                               until=lambda p: p is not None and p.course == "Pebble Beach")
    assert p is not None and p.course == "Pebble Beach" and p.guessed
    assert not p.aired and not p.awaiting_load and p.roster_key == frozenset({"Mike", "Guesty"})
    prompts = len(e.live_llm.prompts)

    # A different guest on the tee (same league players): the guess doesn't fit, so the tee makes its own.
    e.current_par, e.current_course = 4, "Pebble Beach"
    tee = Hud(player_text="MikeD E Shot 1 400 YDS", shot_number=1, score_to_par="E", to_pin="400 yards",
              others=[OtherPlayer("Other", "400 yards", "E")])
    await e.handle_round_start(RoundStartEvent(hud=tee))
    assert len(e.live_llm.prompts) > prompts

    # The same roster on the tee airs the held opener with no tee-time call.
    e2 = _engine(tmp_path / "b")
    e2._last_setup_roster = (["MikeD", "Guesty"], ["MikeD", "Guesty"])
    await _run_setup_watch(e2, [holes_tab, {"load_course": "Pebble Beach"}],
                           until=lambda p: p is not None and p.course == "Pebble Beach")
    prompts = len(e2.live_llm.prompts)
    e2.current_par, e2.current_course = 4, "Pebble Beach"
    tee = Hud(player_text="MikeD E Shot 1 400 YDS", shot_number=1, score_to_par="E", to_pin="400 yards",
              others=[OtherPlayer("Guesty", "400 yards", "E")])
    await e2.handle_round_start(RoundStartEvent(hud=tee))
    assert len(e2.live_llm.prompts) == prompts and e2._pending_opening is None


async def test_setup_watcher_skips_the_slots_when_the_players_tab_is_off(tmp_path):
    # Match Settings tab: the slot boxes land on lit settings widgets, but the PLAYERS tab is dark, so
    # the slots aren't even OCR'd (no labels as players, and no time spent reading them).
    frame = _players_tab("Pebble Beach", {1: "MikeD", 2: "Sarah"}, tab=_TAB_OFF)
    e = _engine(tmp_path)
    scr = _PixelSetupScreen([frame])
    p = await _run_setup_watch(e, None, until=lambda p: p is not None, polls=40, screen=scr)
    assert p is None
    assert not [n for n in scr.ocred if n.startswith("setup_slot")]


async def test_setup_watcher_ignores_frames_with_a_player_dropdown_open(tmp_path):
    # Slot 1's dropdown is open and its list spills over slot 5, where an entry (a league player!)
    # sits right where slot 5's name would. That frame must not count; once the list closes, the
    # real roster (MikeD + a guest) is read and front-loaded.
    dropdown = _players_tab("Augusta National", {1: "MikeD"}, setup_slot_5=_LIST)
    dropdown["setup_slot_5"] = "Sarah"
    picked = _players_tab("Augusta National", {1: "MikeD", 2: "Guesty"})
    e = _engine(tmp_path)
    p = await _run_setup_watch(e, [dropdown], until=lambda p: p is not None, polls=40)
    assert p is None and not e._opener_prepared_for   # nothing front-loaded off the open list
    p = await _run_setup_watch(e, [dropdown, picked, picked, {"load_course": "Augusta National"}],
                               until=lambda p: p is not None and p.aired)
    assert p is not None and p.aired
    assert p.roster_key == frozenset({"Mike", "Guesty"})


async def test_a_players_tab_shown_briefly_between_slow_polls_is_still_read(tmp_path):
    import time

    from PIL import Image

    # As seen live: the setup screen's first frame is mid fade-in (PLAYERS not lit yet), reading it
    # takes ~a second, and by the next poll the player has clicked on to MATCH SETTINGS. Only the quick
    # PLAYERS-tab sampler saw the names; the opener over the load must still be for them.
    fading = {"setup_course": "Play Pebble Beach", "tones": {"setup_players_tab": Tone(120, 120, 0.0)}}
    players = _players_tab("Pebble Beach", {1: "MikeD", 2: "Sarah"})
    match = _players_tab("Pebble Beach", {}, tab=_TAB_OFF, setup_match_tab=_TAB_ON)
    loading = {"load_course": "Pebble Beach"}

    class Timed(_PixelSetupScreen):
        def __init__(self):
            super().__init__([])
            self.t0, self.slow = time.monotonic(), True

        def setup_frame(self):
            t = time.monotonic() - self.t0
            return fading if t < 0.35 else players if t < 0.9 else match if t < 2.0 else loading

        def region(self, name):
            return name

        def grab(self, region):
            return Image.new("L", (4, 4), int(self.region_tones([region], self.setup_frame())[region].mean))

        def read_region_lines_cached(self, names, image, cache):
            if self.slow and image is fading:
                self.slow = False
                time.sleep(0.9)
            return super().read_region_lines_cached(names, image, cache)

    e = _engine(tmp_path)
    p = await _run_setup_watch(e, None, until=lambda p: p is not None and p.aired, polls=400, screen=Timed())
    assert p is not None and p.aired and p.roster_key == frozenset({"Mike", "Sarah"})


async def test_an_older_players_tab_sample_does_not_undo_a_newer_read(tmp_path):
    import time

    # The sampler caught MikeD + Sarah; the watch then read the PLAYERS tab itself with Guesty swapped
    # in, and the player clicked on to another tab. The older sample mustn't put Sarah back.
    old = _players_tab("Pebble Beach", {1: "MikeD", 2: "Sarah"})
    new = _players_tab("Pebble Beach", {1: "MikeD", 2: "Guesty"})
    other = _players_tab("Pebble Beach", {}, tab=_TAB_OFF)
    e = _engine(tmp_path)
    e._players_snap = (time.monotonic(), old, old["tones"])
    p = await _run_setup_watch(e, [new, new, other, other, {"load_course": "Pebble Beach"}],
                               until=lambda p: p is not None and p.aired)
    assert p is not None and p.aired and p.roster_key == frozenset({"Mike", "Guesty"})


async def test_a_players_tab_sample_from_another_course_is_not_used(tmp_path):
    import time

    # A sample from the Augusta setup, then a quick switch to Pebble Beach whose PLAYERS tab is never
    # seen: Augusta's players aren't taken as Pebble's.
    augusta = _players_tab("Augusta National", {1: "MikeD", 2: "Sarah"})
    other = _players_tab("Pebble Beach", {}, tab=_TAB_OFF)
    e = _engine(tmp_path)
    e._players_snap = (time.monotonic(), augusta, augusta["tones"])
    p = await _run_setup_watch(e, [other, other, {"load_course": "Pebble Beach"}],
                               until=lambda p: p is not None and p.aired, polls=150)
    assert p is None or not p.aired


def test_setup_slot_name_rejects_the_empty_slot_entry():
    from commentator.engine import _setup_slot_name, _slot_state

    assert _setup_slot_name(["Empty Slot"]) is None and _setup_slot_name(["Toby"]) == "Toby"
    assert [_slot_state(t) for t in (_PILL, _EMPTY, _LIST)] == ["filled", "empty", "list"]


async def test_setup_watcher_without_a_pixel_probe_trusts_league_players_only(tmp_path):
    # A screen that can't probe pixels falls back to the league roster alone: the guest is dropped.
    frame = {"setup_course": "Play Augusta National", "setup_slot_1": "MikeD", "setup_slot_2": "Sarah",
             "setup_slot_3": "Guesty"}

    class NoProbe(_PixelSetupScreen):
        setup_frame = None
        region_tones = None

        def grab_and_read(self, names):
            r = self.reads[min(self.i, len(self.reads) - 1)]
            self.i += 1
            return r, {n: r.get(n, "") for n in names}

    e = _engine(tmp_path)
    p = await _run_setup_watch(e, None, until=lambda p: p is not None, polls=200, screen=NoProbe([frame, frame]))
    assert p is not None
    assert p.roster_key == frozenset({"Mike", "Sarah"})


async def test_tee_opener_with_no_league_calls_players_by_gspro_name(tmp_path):
    from commentator.league import League
    from commentator.models import RoundStartEvent

    e = _engine_with(tmp_path, League({}))
    e.current_par = 4
    e.current_course = "Pebble Beach"
    tee = Hud(player_text="Alex E Shot 1 400 YDS", shot_number=1, score_to_par="E", to_pin="400 yards",
              others=[OtherPlayer("Jamie", "400 yards", "E")])
    await e.handle_round_start(RoundStartEvent(hud=tee))
    assert e.round is not None and not e.round.is_league
    assert next(tmp_path.glob("transcript-*.txt")).read_text().strip()   # an opener was spoken


def test_league_file_is_optional():
    from commentator.league import League

    lg = League.load(None)
    assert lg.players == [] and lg.match_player("Anyone") is None and lg.prompt_text() == "(none)"
    assert Config().league_file is None
    with pytest.raises(FileNotFoundError):
        League.load("data/no-such-league.json")   # a typo'd path is an error, not a silent no-league


def test_golf_news_parses_rss_and_cleans_html():
    from commentator.news import GolfNews

    rss = """<?xml version="1.0"?><rss version="2.0"><channel><title>ESPN Golf</title>
    <item><title>Scheffler wins the Masters</title>
      <description><![CDATA[<p>A  wire-to-wire win.</p>]]></description></item>
    <item><title>Rory misses the cut</title><description>A rough two days.</description></item>
    </channel></rss>"""
    items = GolfNews(["x"])._parse(rss, "espn.com")
    assert [h.title for h in items] == ["Scheffler wins the Masters", "Rory misses the cut"]
    assert items[0].summary == "A wire-to-wire win."  # tags stripped, whitespace collapsed
    assert "espn.com" in items[0].as_fact() and "Masters" in items[0].as_fact()


class _NewsLLM:
    """Answers the news call with a held, two-line aside."""

    def __init__(self, reply: str = "Fair enough, but a field like that needs a proper villain, Nigel.",
                 opening: str = "I read that Scheffler won by five, and nobody got close."):
        self.prompts: list[str] = []
        self.reply, self.opening = reply, opening
        self.provider, self.model = "fake", "fake"

    async def complete_json(self, system, user, schema, images=None):
        from commentator.llm.base import LLMResult
        self.prompts.append(user)
        lines = [{"speaker": "pbp", "text": self.opening},
                 {"speaker": "color", "text": self.reply}]
        return LLMResult(text=json.dumps({"lines": lines, "trigger": "big_drive",
                                          "tie_in": "Speaking of big hitters, Scheffler won by five."}),
                         input_tokens=100, output_tokens=20)


def _news_engine(tmp_path, llm):
    from commentator.news import GolfNews, Headline

    e = _engine(tmp_path)
    e.closer_llm = llm
    e.news = GolfNews(["x"])
    e.news.headlines = [Headline("Scheffler wins by five", "Scottie Scheffler won by five shots on Sunday.",
                                 "espn.com")]
    return e


async def test_news_asides_are_written_and_voiced_at_round_start(tmp_path):
    from types import SimpleNamespace

    llm = _NewsLLM()
    e = _news_engine(tmp_path, llm)
    r = SimpleNamespace(holes=9, seq=1, ended=False, is_league=False)
    e.round = r
    await e._prepare_news(r)
    assert "Scheffler won by five shots" in llm.prompts[0]   # the real story, fed in verbatim
    [bit] = e._news_bits
    assert bit.trigger == "big_drive" and bit.tie.text.startswith("Speaking of big hitters")
    assert len(bit.lines) == 2 and len(bit.audio) == 2


async def test_an_empty_reply_is_retried_then_the_aside_dropped(tmp_path):
    from types import SimpleNamespace

    llm = _NewsLLM(reply="Absolutely, Nigel.")
    e = _news_engine(tmp_path, llm)
    r = SimpleNamespace(holes=9, seq=1, ended=False, is_league=False)
    e.round = r
    await e._prepare_news(r)
    assert len(llm.prompts) == 2 and e._news_bits == []


async def test_a_news_line_too_long_to_air_whole_is_retried_not_cut_to_its_teaser(tmp_path):
    from types import SimpleNamespace

    # 10-21: cut back to its last full sentence under the word cap, Nigel's line aired as just
    # "Interesting bit of news this morning, Duke." with the story gone.
    llm = _NewsLLM(opening="Interesting bit of news this morning, Duke. Scottie Scheffler won by five shots on "
                           "Sunday and nobody else got close.")
    e = _news_engine(tmp_path, llm)
    e.cfg.commentary.aside_max_words = 18
    r = SimpleNamespace(holes=9, seq=1, ended=False, is_league=False)
    e.round = r
    await e._prepare_news(r)
    assert len(llm.prompts) == 2 and "at most 18 words" in llm.prompts[1] and e._news_bits == []


async def test_a_news_aside_gets_more_words_than_a_live_line(tmp_path):
    from types import SimpleNamespace

    # 23 words: over the 18-word live cap, which retried and dropped most news asides.
    llm = _NewsLLM(opening="Scottie Scheffler won by five shots on Sunday, Duke, and nobody else got close. "
                           "Interesting bit of news this morning, I must say.")
    e = _news_engine(tmp_path, llm)
    e.cfg.commentary.max_words_per_line = 18
    r = SimpleNamespace(holes=9, seq=1, ended=False, is_league=False)
    e.round = r
    await e._prepare_news(r)
    assert len(llm.prompts) == 1 and "up to 30 words" in llm.prompts[0]
    assert e._news_bits and e._news_bits[0].lines[0].text.endswith("I must say.")


async def test_a_news_story_isnt_told_again_to_anyone_who_heard_it(tmp_path):
    from types import SimpleNamespace

    def _round(*players):
        return SimpleNamespace(holes=9, seq=1, ended=False, is_league=False,
                               sides=[SimpleNamespace(players=[p]) for p in players])

    e = _news_engine(tmp_path, llm := _NewsLLM())
    e.cfg.news.banter_rate = 1.0
    e.speech.is_idle = lambda: True
    e.round = r = _round("Tony", "Sarah")
    await e._prepare_news(r)
    r.seq, e._shot_seq = 6, 1
    assert await e._news_after_shot({"seq": 1, "tags": [], "routine": True})
    assert e.store.news_heard(["Sarah"]) == {"Scheffler wins by five"}

    e.round = r = _round("Tony", "Mike")             # Tony heard it, so nobody in this round gets it
    await e._prepare_news(r)
    assert e._news_bits == []
    e.round = r = _round("Mike", "Dave")             # nobody here has heard it
    llm.opening, llm.reply = "Scheffler won on Sunday, by five shots.", "Nobody else got near him, Nigel."
    await e._prepare_news(r)
    assert [b.title for b in e._news_bits] == ["Scheffler wins by five"]
    r.seq, e._news_aired = 6, []                       # a new round starts with no asides aired
    r.sides.append(SimpleNamespace(players=["Sarah"]))   # named mid-round, and she's heard it
    assert not await e._news_after_shot({"seq": 1, "tags": [], "routine": True})
    assert e._news_bits == []


def test_a_held_aside_waits_for_its_shot_and_asides_are_spaced(tmp_path):
    from types import SimpleNamespace

    from commentator.engine import PreparedNews
    from commentator.models import Line

    e = _engine(tmp_path)
    e.rng = random.Random(0)
    e.cfg.news.banter_rate = 1.0
    held = PreparedNews("long", "big_drive", [Line("pbp", "a"), Line("color", "b")], [None, None],
                        Line("pbp", "tie"), None)
    plain = PreparedNews("plain", None, [Line("pbp", "c"), Line("color", "d")], [None, None])
    e._news_bits = [held, plain]
    e.round = SimpleNamespace(holes=9, seq=3, ended=False)
    routine = {"tags": [], "routine": True}
    assert e._news_pick({"tags": ["monster_carry"], "routine": False}) == (held, True)
    assert e._news_pick(routine) == (plain, False)
    assert e._news_pick({"tags": ["in_rough"], "routine": False}) is None   # neither routine nor its shot
    e._news_bits = [held]
    assert e._news_pick(routine) is None             # held for its shot early in the round...
    e.round.seq = 6
    assert e._news_pick(routine) == (held, False)    # ...and takes any quiet moment late on
    e._news_aired = [5]
    assert e._news_pick({"tags": ["monster_carry"], "routine": False}) is None   # too soon after the last
    e._news_aired, e.round.seq = [], 1
    assert e._news_pick({"tags": ["monster_carry"], "routine": False}) is None   # never the first hole
    e.round.seq = 8
    assert e._news_pick({"tags": ["monster_carry"], "routine": False}) is None   # nor the last two


def test_teaser_stories_are_skipped():
    from commentator.news import Headline

    teaser = Headline("How the U.S. won the Presidents Cup", "What were the keys to the comeback? Who stepped "
                      "up when needed? And what does it mean for the U.S. heading into the Ryder Cup in Ireland?", "espn.com")
    listicle = Headline("Top takeaways", "Here are some of the main takeaways from LIV Golf's bankruptcy "
                        "filings, including its plans for player compensation.", "espn.com")
    real = Headline("Zalatoris to have surgery", "Will Zalatoris said he will undergo spinal fusion surgery "
                    "next month and will miss all of 2027.", "espn.com")
    assert not teaser.substantive() and not listicle.substantive() and real.substantive()


def test_block_repeats_toggle(tmp_path):
    from commentator.models import Line

    e = _engine(tmp_path)
    e.state.record_lines([Line("pbp", "What a week it has been out on tour lately.")])
    dup = [Line("color", "What a week it has been out on tour lately.")]
    # On (default): the repeated phrasing is rejected.
    assert e.cfg.commentary.block_repeats
    good, problems = e._grounded(list(dup), "", mode="result")
    assert not good and problems
    # Off: the same line is allowed through.
    e.cfg.commentary.block_repeats = False
    good, problems = e._grounded(list(dup), "", mode="result")
    assert good and not problems


def test_spelled_out_numbers_in_the_notes_are_allowed():
    notes = "storylines: Shed Heads are five points off the lead."
    assert check_line("They're five points back.", "", notes).ok
    assert not check_line("They're six points back.", "", notes).ok


def _workbook(path, year, teams, standings, results, table):
    import datetime

    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Schedule"
    ws.append(["Week", "Course", "Format", "Holes", "Men's Tee", "End Date", "CTP"])
    ws.append([1, "Bethpage Black", "Scramble", "Front 9", "Red", datetime.datetime(year, 12, 6), "3, 8"])
    ws.append([2, "Riviera", "Alternate", "18 Holes", "Green", datetime.datetime(year, 12, 6), "14,16"])
    ws.append([None, "All weeks play with:"])
    ws.append([None, "Pins: Thursday\nGimme/Auto Putt: 4'", "Wind: No Wind"])
    t = wb.create_sheet("Teams")
    t.append(["Team Name", "Player Name", "Email"])
    for number, players in teams.items():
        for p in players:
            t.append([number, p, "someone@example.com"])
    r = wb.create_sheet("Results")
    r.append(["Week", "Player", "CTP", "Week Winner"])
    for row in results:
        r.append(row)
    s = wb.create_sheet("Standings")
    s.append(["Team"] + [f"Week {i}" for i in range(1, 13)] + ["Adjusted Final"])
    for label, cum in standings.items():
        s.append([label] + cum + [None] * (12 - len(cum)) + [None])
    s.append(["CTP - $20pp"])
    s.append(["Weekly Place", "Points awarded"])
    for i, pts in enumerate(table, 1):
        s.append([["1st", "2nd", "3rd", "4th"][i - 1], pts])
    wb.save(path)


def test_import_league_builds_league_and_history(tmp_path):
    pytest.importorskip("openpyxl")
    import json

    from commentator.history import History
    from commentator.league_import import place_for, run_import

    assert place_for(55, [60, 50, 40]) == (1, True) and place_for(40, [60, 50, 40]) == (3, False)
    teams = {"Team 1": ["Alex Rivera", "Jamie Avery"], "Team 2": ["Robin Delgado", "Robin Avery"]}
    _workbook(tmp_path / "2025.xlsx", 2025, teams, {"Alex/Jamie": [60, 105], "Robin D/Robin A": [45, 105]},
              [[1, "Alex", None, 35], [1, "Jamie", 20, 35], [2, "Robin", 20, None]], [60, 45])
    _workbook(tmp_path / "2026.xlsx", 2026, {"Team 1": ["Alex Rivera", "Jamie Avery"], "Team 2": ["Dana Rivera", "Casey Avery"]},
              {"Alex/Jamie": [], "Dana/Casey": []}, [], [70, 60])
    league_out, hist_out = tmp_path / "league.json", tmp_path / "history.json"
    league_out.write_text(json.dumps({"teams": [{"number": "Team 1", "players": [
        {"name": "Alex Rivera", "handicap": 14.2, "gspro_name": "AlexR", "notes": "built the booth"}]}],
        "storylines": ["kept"]}))
    league, hist = run_import(str(tmp_path / "2026.xlsx"), [str(tmp_path / "2025.xlsx")], str(league_out), str(hist_out))
    alex = league["teams"][0]["players"][0]
    assert (alex["handicap"], alex["gspro_name"], alex["notes"]) == (14.2, "AlexR", "built the booth")
    assert league["storylines"] == ["kept"] and league["ctp_prize"] == 20
    assert league["teams"][0]["name"] == "Alex and Jamie" and "Alex/Jamie" in league["teams"][0]["aliases"]
    assert "email" not in json.dumps(league).lower() and "example.com" not in hist_out.read_text()
    assert league["schedule"][1]["hole_count"] == 18 and league["schedule"][0]["ctp_holes"] == [3, 8]
    assert "Gimme/Auto Putt: 4'" in league["gspro_settings"]

    past = next(s for s in hist["seasons"] if s["year"] == 2025)
    alex_jamie = next(t for t in past["teams"] if t["label"] == "Alex/Jamie")
    assert [w["place"] for w in alex_jamie["weeks"]] == [1, 2] and alex_jamie["rank"] == 1 and alex_jamie["rank_tied"]
    # "Robin" is ambiguous in 2025, so that row's CTP isn't credited to either Robin.
    assert [a["player"] for a in past["awards"] if a["kind"] == "ctp"] == ["Jamie Avery"]

    h = History.load(hist_out)
    facts = " ".join(h.team_facts("Alex and Jamie", ["Alex Rivera", "Jamie Avery"], "scramble", "Bethpage Black"))
    assert "won the league in 2025, so they're the defending champions" in facts
    assert "1st at Bethpage Black in 2025" in facts and "Jamie Avery has won 1 closest-to-the-pin prize" in facts
    new = " ".join(h.team_facts("Dana and Casey", ["Dana Rivera", "Casey Avery"]))
    assert "new pairing" in new and "Dana Rivera is new to the league" in new
    assert h.standings_facts("Alex and Jamie", ["Alex Rivera", "Jamie Avery"]) == [
        "No 2026 league weeks have been recorded yet."]


def test_schedule_week_comes_from_the_course():
    league = League({"teams": [], "schedule": [
        {"week": 4, "course": "Sheep Ranch", "end_date": "2027-01-03"},
        {"week": 5, "course": "Pine Valley", "end_date": "2027-01-24"},
        {"week": 12, "course": "Sand Valley", "end_date": "2027-03-28"},
        {"week": 3, "course": "The Ancient Course", "gspro_course": "St Andrews Old", "end_date": "2027-01-03"},
    ]})
    assert league.schedule_entry("2026-12-01", "Sand Valley Golf Resort")["week"] == 12
    assert league.schedule_entry("2026-12-01", "Pine Valley GC")["week"] == 5
    assert league.schedule_entry("2026-12-01", "PINE VALLEV")["week"] == 5          # an OCR slip
    assert league.schedule_entry("2026-12-01", "ALPINE VALLEY") is None             # a different course
    assert league.schedule_entry("2026-12-01", "St Andrews Old")["week"] == 3
    assert league.schedule_entry("2026-12-01", "Wynn Golf Club") is None


def test_ctp_hole_is_flagged_and_tee_shot_finish_recorded(tmp_path):
    e = _engine(tmp_path)
    e.round = Round(league=LEAGUE, gspro_players=["MikeD", "Sarah"], course=None, scheduled_format="best ball",
                    start_hole=1, schedule={"ctp_holes": [3]})
    e.round.seq = 3
    assert "closest-to-the-pin" in e._ctp_facts(1)[0]
    facts = e._ctp_facts(2, Hud(lie="green", to_pin="14 feet 2 inches"), "Mike")
    assert "14 feet 2 inches" in facts[0]
    e._ctp_facts(2, Hud(lie="green", to_pin="31 feet"), "Sarah")
    assert "Closest to the pin today on hole 3: Mike, 14 feet 2 inches" in " ".join(e.round.closing_facts([], {}))
    e.round.seq = 2
    assert e._ctp_facts(1) == []


async def _log_round(tmp_path, holes=9):
    from commentator.models import RoundStartEvent

    e = _engine(tmp_path)
    e.cfg.source.gspro_log_truth = True
    e.cfg.commentary.closer = False
    e.cfg.commentary.opener = False
    e.cfg.commentary.round_holes = holes
    e.current_course = "Somewhere Else"   # off the league schedule: the configured hole count applies
    await e.handle_round_start(RoundStartEvent(hud=Hud(player_text="MikeD E Shot 1 400 YDS", shot_number=1,
                                                       score_to_par="E", to_pin="400 yards")))
    return e, e.round


def _queued(e):
    out = []
    while not e._events.empty():
        out.append(e._events.get_nowait())
    return out


async def test_range_keeps_the_booth_off(tmp_path):
    from commentator.models import Shot, ShotEvent

    e = _engine(tmp_path)
    e.cfg.source.gspro_log_truth = True
    e.gspro.mode = "range"
    tee = Hud(player_text="MikeD E Shot 1 400 YDS", shot_number=1, score_to_par="E", to_pin="400 yards")
    assert not e._is_round_start(tee)
    await e.handle_shot(ShotEvent(shot=Shot(club="7I", carry=150.0)))
    assert e.store.db.execute("SELECT COUNT(*) FROM shots").fetchone()[0] == 0
    assert e.state.shots == [] and e._shot_seq == 0   # never reached the reaction pipeline
    # Off the range (back to the menus, then a course), a fresh tee opens a round again.
    e.gspro.mode = "course"
    assert e._is_round_start(tee)


async def test_an_eighteen_counted_as_nine_plays_on_until_gspro_completes_it(tmp_path):
    from commentator.models import ScorecardEvent

    e, r = await _log_round(tmp_path, holes=9)
    assert r.holes == 9
    front = {"MikeD": {h: 4 for h in range(1, 10)}}
    await e.handle_scorecard(ScorecardEvent(card=Scorecard(holes=list(range(1, 19)), scores=front)))
    assert not r.ended and r.holes == 18   # GSPro hasn't said the round is complete
    full = {"MikeD": {h: 4 for h in range(1, 19)}}
    r.apply_scorecard(Scorecard(holes=list(range(1, 19)), scores=full))
    e.gspro.round_completes, e.gspro.active_holes = 1, 18
    e._check_log_round()
    assert r.log_complete and [ev.reason for ev in _queued(e)] == ["final scorecard"]


async def test_gspro_layout_shortens_an_overcounted_round(tmp_path):
    e, r = await _log_round(tmp_path, holes=18)
    r.apply_scorecard(Scorecard(holes=list(range(1, 19)), scores={"MikeD": {h: 4 for h in range(1, 10)}}))
    e.gspro.round_completes, e.gspro.active_holes = 1, 9
    e._check_log_round()
    assert r.holes == 9 and [ev.reason for ev in _queued(e)] == ["final scorecard"]


async def test_quitting_a_round_early_ends_it_quietly_and_a_new_tee_opens(tmp_path):
    e, r = await _log_round(tmp_path)
    r.apply_scorecard(Scorecard(holes=list(range(1, 19)), scores={"MikeD": {1: 4, 2: 5}}))
    r.seq = 3
    e.gspro.round_ends = 1   # "End Round Button Click" before "Round Is Complete"
    e._check_log_round()
    (ev,) = _queued(e)
    assert ev.abandoned
    await e.handle_round_end(ev)
    assert r.ended
    # The two holes played are kept for per-hole stats, marked incomplete; too short to count as a round.
    row = e.store.db.execute("SELECT complete, holes_played, net_to_par FROM rounds").fetchone()
    assert (row["complete"], row["holes_played"], row["net_to_par"]) == (0, 2, None)
    e.store.session_id += 1
    assert e.store.season_rounds("MikeD")["rounds"] == 0
    tee = Hud(player_text="MikeD E Shot 1 400 YDS", shot_number=1, score_to_par="E", to_pin="400 yards")
    assert e._is_round_start(tee)


async def test_a_front_nine_of_an_eighteen_counts_as_a_round(tmp_path):
    e, r = await _log_round(tmp_path, holes=18)
    pars = {h: 4 for h in range(1, 19)}
    r.apply_scorecard(Scorecard(holes=list(range(1, 19)), par=pars, scores={"MikeD": {h: 5 for h in range(1, 10)}}))
    r.sides[0].strokes = 0   # a scratch player: net is gross
    r.seq = 10   # on the 10th tee, nothing scored there yet
    e.gspro.round_ends = 1
    e._check_log_round()
    (ev,) = _queued(e)
    await e.handle_round_end(ev)
    row = e.store.db.execute("SELECT complete, holes, holes_played, gross, net_to_par FROM rounds").fetchone()
    assert tuple(row) == (0, 18, 9, 45, 9)
    e.store.session_id += 1
    season = e.store.season_rounds(r.sides[0].key)
    assert season["rounds"] == 1 and season["avg_net_to_par"] == 9


def test_season_net_is_per_nine_holes(tmp_path):
    from commentator.round import SideStats
    from commentator.store import Store

    s = Store(str(tmp_path / "s.db"))
    s.session_id = 1

    def put(holes, net, played=None, complete=True):
        n = played or holes
        st = SideStats(holes_scored=n, gross=4 * n + net, par=4 * n, scores={h: 4 for h in range(1, n + 1)})
        s.record_round(course="Pebble", fmt="stroke", side="Mike", team=None, holes=holes, stats=st, strokes=0,
                       net_to_par=net, hole_number=lambda k: k, hole_pars={h: 4 for h in range(1, n + 1)},
                       complete=complete)

    put(18, 4)                                   # an 18 at +4 is +2 a nine
    put(9, 0)
    put(18, 6, played=12, complete=False)        # quit after 12: +6 over 12 is +4.5 a nine
    put(9, -3, played=2, complete=False)         # quit after 2: not a round
    s.session_id = 2
    season = s.season_rounds("Mike")
    assert season["rounds"] == 3
    assert season["avg_net_to_par"] == pytest.approx((2 + 0 + 4.5) / 3) and season["best_net_to_par"] == 0


def test_season_line_counts_only_rounds_of_this_rounds_kind(tmp_path):
    from commentator.round import SideStats, _season_facts
    from commentator.store import Store

    s = Store(str(tmp_path / "s.db"))
    s.session_id = 1

    def put(gross, net, league, pars=9):
        st = SideStats(holes_scored=9, gross=gross, par=4 * pars, scores={h: 4 for h in range(1, 10)})
        s.record_round(course="Pebble", fmt="stroke", side="Josh", team=None, holes=9, stats=st, strokes=None,
                       net_to_par=net, hole_number=lambda k: k, hole_pars={h: 4 for h in range(1, pars + 1)},
                       is_league=league)

    put(35, -2, league=False)    # the one practice round a handicap happened to be loaded for
    put(39, None, league=False)
    put(37, None, league=False)
    put(36, None, league=False, pars=8)   # one par unread: 36 on a "par 32" isn't 4 over
    s.session_id = 2
    league = s.season_rounds("Josh", "league")
    assert _season_facts("Josh", league, 9, league=True) == ["Josh has no earlier league rounds recorded this season."]
    practice = _season_facts("Josh", s.season_rounds("Josh", "practice"), 9, league=False)
    assert practice[0] == "Josh's earlier practice rounds this season: 4."
    assert practice[1] == ("Josh's average gross score to par in the 3 with every par known: 1 over par; "
                           "best: 1 under par.")
    assert "net" not in " ".join(practice)


def _log_engine(tmp_path):
    e = _engine(tmp_path)
    e.cfg.source.gspro_log_truth = True
    e.cfg.commentary.opener = False
    return e


def test_no_round_starts_from_a_tee_left_on_screen_after_end_round(tmp_path):
    e = _log_engine(tmp_path)
    tee = Hud(player_text="MikeD E Shot 1 400 YDS", shot_number=1, score_to_par="E", to_pin="400 yards")
    e.gspro.mode = "menu"
    assert not e._is_round_start(tee)
    e.gspro.mode = "course"   # Play pressed: "Selected Course" logged
    assert e._is_round_start(tee)


def test_booth_started_mid_round_joins_it_without_an_opener(tmp_path):
    e = _log_engine(tmp_path)
    tee = Hud(player_text="MikeD E Shot 1 400 YDS", shot_number=1, score_to_par="E", to_pin="400 yards")
    e.gspro.mode, e.gspro.shots_since_course, e.gspro.first_hole, e.gspro.current_hole = "course", 30, 9, 12
    assert not e._is_round_start(tee)   # a level tee mid-round isn't a new round
    assert e._should_join() and not e._should_join()   # tried once


async def test_joining_takes_the_start_and_current_hole_from_the_log(tmp_path):
    from commentator.models import RoundStartEvent

    e = _log_engine(tmp_path)
    e.current_course = "Somewhere Else"
    g = e.gspro
    g.mode, g.shots_since_course, g.first_hole, g.current_hole = "course", 30, 9, 12   # began on 10, now on 13
    await e.handle_round_start(RoundStartEvent(hud=Hud(player_text="MikeD +1 Shot 2 150 YDS", shot_number=2),
                                               joined=True))
    r = e.round
    assert r and r.start_hole == 10 and not r.start_hole_assumed and r.seq == 4 and e.hole_number == 13


async def test_range_with_a_round_open_ends_it_quietly(tmp_path):
    e, r = await _log_round(tmp_path)
    e.gspro.mode = "range"
    e._check_log_round()
    (ev,) = _queued(e)
    assert ev.abandoned and ev.reason == "GSPro went to the range"


async def test_skipping_hole_one_means_a_back_nine_until_the_log_names_the_start(tmp_path):
    e2, r = await _log_round(tmp_path, holes=18)
    e2.gspro.starts_late = True
    e2._apply_log_holes(r)
    assert r.start_hole == 10 and r.start_hole_assumed and r.holes == 9
    e2.gspro.first_hole = 11   # first score line: hole 12 (custom holes 12-18)
    e2._apply_log_holes(r)
    assert r.start_hole == 12 and not r.start_hole_assumed and r.holes == 7


async def test_a_round_on_hole_one_keeps_its_count_and_confirms_the_start(tmp_path):
    e, r = await _log_round(tmp_path, holes=9)
    e.gspro.first_hole = 0
    e._apply_log_holes(r)
    assert r.start_hole == 1 and not r.start_hole_assumed and r.holes == 9


async def test_league_round_starts_from_the_schedule_and_the_log_corrects_it(tmp_path):
    from commentator.models import RoundStartEvent

    e = _log_engine(tmp_path)
    e.current_par = 4
    e.current_course = "Pebble Beach"   # scheduled: 9 holes from hole 1
    tee = Hud(player_text="MikeD E Shot 1 400 YDS", shot_number=1, score_to_par="E", to_pin="400 yards",
              others=[OtherPlayer("Sarah", "400 yards", "E")])
    await e.handle_round_start(RoundStartEvent(hud=tee))
    r = e.round
    assert r.is_league and (r.start_hole, r.holes, r.start_hole_assumed) == (1, 9, False)
    # GSPro skipped hole 1: assume the back nine until the first score line.
    e.gspro.starts_late = True
    e._apply_log_holes(r)
    assert (r.start_hole, r.holes, r.start_hole_assumed) == (10, 9, True)
    e.gspro.first_hole = 9
    e._apply_log_holes(r)
    assert (r.start_hole, r.holes, r.start_hole_assumed) == (10, 9, False)

    # Scheduled for the back nine but they teed off on 1: the log's first score line wins.
    r2 = e._build_round(["MikeD", "Sarah"], "Pebble Beach", {"hole_count": 9, "start_hole": 10}, True)
    e.gspro.starts_late, e.gspro.first_hole = False, 0
    e._apply_log_holes(r2)
    assert (r2.start_hole, r2.holes, r2.start_hole_assumed) == (1, 9, False)


def test_opener_introduces_players_by_full_name():
    from commentator.prompts import opener_prompt

    league = League({"teams": [{"name": "Aces", "players": [
        {"name": "Toby Hale", "aliases": ["Toby"]}, {"name": "Wes Brooks", "aliases": ["Wes"]}]}]})
    r = Round(league=league, gspro_players=["Toby", "Wes"], course="Wynn", scheduled_format=None, holes=9)
    facts = r.opening_facts({}, None, None)
    assert any(f.startswith("Playing: Toby Hale") for f in facts)
    assert any(f.startswith("Playing: Wes Brooks") for f in facts)
    prompt, _ = opener_prompt(facts=facts, recent_lines=[])
    assert "first and last name" in prompt


def test_ctp_prizes_are_counted_from_the_money_not_the_rows():
    from commentator.history import History

    seasons = [
        {"year": 2024, "payouts": [], "teams": [], "awards": [   # no payout line: $10 a pin, the smallest
            {"week": 1, "player": "Owen Pratt", "kind": "ctp", "amount": 10.0},
            {"week": 2, "player": "Owen Pratt", "kind": "ctp", "amount": 20.0},
            {"week": 4, "player": "David Hayes", "kind": "ctp", "amount": 0.0}]},
        {"year": 2025, "payouts": ["CTP - $20pp"], "teams": [], "awards": [
            {"week": 4, "player": "Owen Pratt", "kind": "ctp", "amount": 40.0},
            {"week": 5, "player": "Owen Pratt", "kind": "ctp", "amount": 20.0},
            {"week": 5, "player": "Owen Pratt", "kind": "week_win", "amount": 35.0}]},
    ]
    facts = History({"current_year": 2026, "seasons": seasons}).team_facts("Owen/David", ["Owen Pratt", "David Hayes"])
    assert "Owen Pratt has won 6 closest-to-the-pin prizes in league play (3 in 2024 and 3 in 2025)." in facts
    assert not any(f.startswith("David Hayes has won") for f in facts)


_BOX_ON, _BOX_OFF = Tone(221, 255, 0.82), Tone(73, 70, 0.0)   # measured on the sim PC's HOLES tab


def _holes_tab(course, front, back):
    return {"setup_course": f"Play {course}", "tones": {
        "setup_players_tab": _TAB_OFF, "setup_holes_tab": _TAB_ON,
        "setup_front9_box": _BOX_ON if front else _BOX_OFF, "setup_back9_box": _BOX_ON if back else _BOX_OFF}}


async def _setup_then_load(tmp_path, frames):
    from commentator.league import League

    e = _engine_with(tmp_path, League({}))
    course = "Beaver Creek"
    reads = [_players_tab(course, {1: "Alex", 2: "Jamie"})] + frames + [{"load_course": course}]
    p = await _run_setup_watch(e, reads, until=lambda p: p is not None and p.aired)
    assert p is not None and p.aired
    return e, p.round


async def test_setup_never_on_the_holes_tab_is_eighteen_holes(tmp_path):
    e, r = await _setup_then_load(tmp_path, [])
    assert (r.start_hole, r.holes, r.start_hole_assumed) == (1, 18, False)


async def test_setup_front_nine_only(tmp_path):
    e, r = await _setup_then_load(tmp_path, [_holes_tab("Beaver Creek", True, False)] * 2)
    assert (r.start_hole, r.holes) == (1, 9)


async def test_setup_back_nine_only(tmp_path):
    e, r = await _setup_then_load(tmp_path, [_holes_tab("Beaver Creek", False, True)] * 2)
    assert (r.start_hole, r.holes) == (10, 9)


async def test_setup_unchecking_a_nine_after_the_opener_was_composed_recomposes_it(tmp_path):
    course = "Beaver Creek"
    players = _players_tab(course, {1: "Alex", 2: "Jamie"})
    # The opener is composed for 18 on the PLAYERS tab, then the back nine is unchecked and Play pressed.
    e, r = await _setup_then_load(tmp_path, [players] * 5 + [_holes_tab(course, True, False)] * 5)
    assert (r.start_hole, r.holes) == (1, 9)


async def test_a_quick_holes_tab_visit_between_polls_is_caught_by_the_sampler(tmp_path):
    import time
    from PIL import Image

    # Premonition, 10-21: the back nine unchecked and Play pressed within a few seconds, all while the
    # watch was busy reading the PLAYERS tab, so it never saw HOLES and called the round 18 holes.
    course = "Beaver Creek"
    players, nines = _players_tab(course, {1: "Alex", 2: "Jamie"}), _holes_tab(course, True, False)
    loading = {"load_course": course}

    class Timed(_PixelSetupScreen):
        def __init__(self):
            super().__init__([])
            self.t0, self.slow = time.monotonic(), True

        def setup_frame(self):
            t = time.monotonic() - self.t0
            return players if t < 0.3 else nines if t < 1.5 else loading

        def region(self, name):
            return name

        def grab(self, region):
            return Image.new("L", (4, 4), int(self.region_tones([region], self.setup_frame())[region].mean))

        def read_region_lines_cached(self, names, image, cache):
            if self.slow and image is players:
                self.slow = False
                time.sleep(1.7)
            return super().read_region_lines_cached(names, image, cache)

    from commentator.league import League

    e = _engine_with(tmp_path, League({}))
    p = await _run_setup_watch(e, None, until=lambda p: p is not None and p.aired, polls=400, screen=Timed())
    assert p is not None and p.aired
    assert (p.round.start_hole, p.round.holes, p.round.holes_confirmed) == (1, 9, True)


async def test_a_holes_sample_from_an_earlier_setup_is_not_used(tmp_path):
    import time

    e, r = await _setup_then_load(tmp_path, [])
    assert r.holes == 18 and not r.holes_confirmed   # never on the HOLES tab: GSPro's default, unconfirmed
    e._pending_opening = None
    e._holes_snap = (time.monotonic() - 5, (10, 9))   # caught before this setup screen came up
    library = {"load_course": "COURSE LIBRARY"}
    p = await _run_setup_watch(e, [library, _players_tab("Beaver Creek", {1: "Alex", 2: "Jamie"}),
                                   {"load_course": "Beaver Creek"}],
                               until=lambda p: p is not None and p.aired)
    assert p is not None and (p.round.start_hole, p.round.holes) == (1, 18)


async def test_a_holes_sample_from_the_course_before_is_not_used(tmp_path):
    import time

    # Straight from course A's setup (HOLES sampled between polls) to course B's: that sample could be
    # A's, so B keeps GSPro's unconfirmed default.
    a = _players_tab("Augusta National", {1: "Alex", 2: "Jamie"})
    b = _players_tab("Beaver Creek", {1: "Alex", 2: "Jamie"})
    e = _engine(tmp_path)

    class Switch(_PixelSetupScreen):
        last = 0.0

        def setup_frame(self):
            frame = super().setup_frame()
            if frame is b and e._holes_snap is None:
                e._holes_snap = (self.last + 0.001, (10, 9))
            self.last = time.monotonic()
            return frame

    p = await _run_setup_watch(e, None, until=lambda p: p is not None and p.aired,
                               screen=Switch([a, a, b, b, {"load_course": "Beaver Creek"}]))
    assert p is not None and (p.round.start_hole, p.round.holes, p.round.holes_confirmed) == (1, 18, False)


def test_an_unconfirmed_hole_count_is_not_announced(tmp_path):
    e = _engine(tmp_path)
    e._setup_holes = (1, 18)   # the setup screen's default; its HOLES tab was never read
    r = e._build_round(["MikeD"], "Somewhere", {}, is_league=False)
    facts = " ".join(r.opening_facts({}, None, None))
    assert "Holes in this round" not in facts and "don't say a number of holes" in facts
    e._setup_holes_read = True
    r = e._build_round(["MikeD"], "Somewhere", {}, is_league=False)
    assert "Holes in this round: 18." in r.opening_facts({}, None, None)


def test_a_practice_round_with_half_a_team_says_who_isnt_here(tmp_path):
    e = _engine(tmp_path)
    solo = e._league_facts(e._build_round(["MikeD"], "Somewhere", {}, is_league=False))
    assert solo[0].startswith("Only Mike of Aces is playing this round; Sarah is not here.")
    both = e._league_facts(e._build_round(["MikeD", "Sarah"], "Somewhere", {}, is_league=False))
    assert not any(f.startswith("Only ") for f in both)


def test_a_round_with_no_league_players_drops_the_league_framing(tmp_path):
    e = _engine(tmp_path)
    guest = e._build_round(["David"], "Somewhere", {}, is_league=False)
    e._start_round(guest)
    _, opener = e._opener_io(guest)
    assert "plays in" in opener and "This is a practice round, not a league match" not in opener
    assert any("plays in" in f for f in e._roster_facts())
    member = e._build_round(["MikeD"], "Somewhere", {}, is_league=False)
    assert not e._no_members(member) and "This is a practice round" in e._opener_io(member)[1]


def test_a_round_takes_the_setup_holes_and_the_next_one_reads_its_own(tmp_path):
    e = _engine(tmp_path)
    e._setup_holes = (10, 9)
    r = e._build_round(["MikeD"], "Somewhere", {"hole_count": 18, "start_hole": 1}, is_league=False)
    assert (r.start_hole, r.holes) == (10, 9)   # the setup screen beats the schedule's guess
    e._start_round(r)
    assert e._setup_holes is None
    r2 = e._build_round(["MikeD"], "Somewhere", {"hole_count": 18, "start_hole": 1}, is_league=False)
    assert (r2.start_hole, r2.holes) == (1, 18)


def test_putts_are_strokes_from_the_green_with_any_club():
    # Par 4, as the PGA Tour counts putts: a wedge chipped off the green is a putt, a putt from the
    # fringe isn't. Putts = the wedge + the putt on the green + the conceded tap-in (card 6).
    r = Round(league=LEAGUE, gspro_players=["Mike"], course="X", scheduled_format=None, holes=1)
    r.par = {1: 4}
    r.card_scores = {"Mike": {1: 6}}
    r.scorecard_seen = True
    shots = [rec("Mike", 1, 4, 1, "tee", "fairway", "DR"), rec("Mike", 1, 4, 2, "fairway", "green"),
             rec("Mike", 1, 4, 3, "green", "fringe", "SW"), rec("Mike", 1, 4, 4, "fringe", "green", "PT"),
             rec("Mike", 1, 4, 5, "green", "green", "PT")]
    for s in shots:
        s.round_id = r.id
    st = r.player_stats("Mike", shots)
    assert st.putts == 3 and st.putt_holes == 1 and st.three_putts == 1
    assert r.side_stats(r.sides[0], shots).putts == 3


def test_putter_mode_overrides_the_launch_guess():
    chip = Shot(ball_speed=12, vla=None, club=None)
    assert chip.is_putt                       # the speed-and-launch guess
    chip.putter = False                       # GSPro: putter not selected
    assert not chip.is_putt
    fringe = Shot(ball_speed=25, vla=8, club="SW", putter=True)
    assert fringe.is_putt


def test_a_tee_shot_conceded_inside_the_gimme_is_a_one_putt_hole():
    r = Round(league=LEAGUE, gspro_players=["Mike"], course="X", scheduled_format=None, holes=1)
    r.par = {1: 3}
    r.card_scores = {"Mike": {1: 2}}
    r.scorecard_seen = True
    shots = [rec("Mike", 1, 3, 1, "tee", "green", "7I")]
    for s in shots:
        s.round_id = r.id
    st = r.player_stats("Mike", shots)
    assert st.putts == 1 and st.putt_holes == 1


def test_a_chip_in_is_a_zero_putt_hole():
    r = Round(league=LEAGUE, gspro_players=["Mike"], course="X", scheduled_format=None, holes=2)
    r.par = {1: 4, 2: 4}
    r.card_scores = {"Mike": {1: 3, 2: 4}}
    r.scorecard_seen = True
    shots = [rec("Mike", 1, 4, 1, "tee", "fairway", "DR"), rec("Mike", 1, 4, 2, "fairway", "rough", "8I"),
             rec("Mike", 1, 4, 3, "rough", "green", "SW"),   # chipped in
             rec("Mike", 2, 4, 1, "tee", "fairway", "DR"), rec("Mike", 2, 4, 2, "fairway", "green", "8I"),
             rec("Mike", 2, 4, 3, "green", "green", "PT"), rec("Mike", 2, 4, 4, "green", "green", "PT")]
    for s in shots:
        s.round_id = r.id
    st = r.player_stats("Mike", shots)
    assert st.putts == 2 and st.putt_holes == 2
    side = r.side_stats(r.sides[0], shots)
    assert side.putts == 2 and side.putt_holes == 2


def _solo(card, pars, holes=1):
    r = Round(league=LEAGUE, gspro_players=["Mike"], course="X", scheduled_format=None, holes=holes)
    r.par = pars
    r.card_scores = {"Mike": card}
    r.scorecard_seen = True
    return r


def _own(r, shots):
    for s in shots:
        s.round_id = r.id
    return shots


def test_a_mulligan_is_left_out_of_the_round_stats():
    # Par 4: a drive into the trees taken back, the re-hit in the fairway, on, two putts (card 4).
    r = _solo({1: 4}, {1: 4})
    bad = rec("Mike", 1, 4, 1, "tee", "rough", "DR")
    bad.mulligan = True
    shots = _own(r, [bad, rec("Mike", 1, 4, 1, "tee", "fairway", "DR"), rec("Mike", 1, 4, 2, "fairway", "green"),
                     rec("Mike", 1, 4, 3, "green", "green", "PT"), rec("Mike", 1, 4, 4, "green", "green", "PT")])
    for st in (r.player_stats("Mike", shots), r.side_stats(r.sides[0], shots)):
        assert st.fir == (1, 1) and st.gir == (1, 1) and st.putts == 2 and st.penalties == 0


def test_a_pick_up_is_not_putts_or_penalties():
    # Cabot Cliffs 11, par 4: four strokes into the trees, then picked up. GSPro scores it 8.
    r = _solo({1: 8}, {1: 4})
    shots = _own(r, [rec("Mike", 1, 4, n, "tee" if n == 1 else "rough", "rough", "DR" if n == 1 else "7I")
                     for n in (1, 2, 3, 4)])
    shots[-1].hud = Hud(lie="green")
    r.picked_up.add(("Mike", 1))
    st = r.player_stats("Mike", shots)
    assert st.gross == 8 and st.putts == 0 and st.putt_holes == 0 and st.penalties == 0
    assert any("picked up" in f for f in r._side_facts(r.sides[0], r.side_stats(r.sides[0], shots), None))


def test_a_greenside_bunker_just_past_thirty_yards_is_a_sand_save_chance():
    r = _solo({1: 4}, {1: 4})
    sand = rec("Mike", 1, 4, 3, "sand", "green", "SW")
    sand.hud = Hud(lie="green", to_pin="30.2 yards")
    shots = _own(r, [rec("Mike", 1, 4, 1, "tee", "fairway", "DR"), rec("Mike", 1, 4, 2, "fairway", "sand"),
                     sand, rec("Mike", 1, 4, 4, "green", "green", "PT")])
    assert r.player_stats("Mike", shots).sand == (1, 1)
    sand.hud = Hud(lie="green", to_pin="120 yards")      # a fairway bunker isn't a sand save
    assert r.player_stats("Mike", shots).sand == (0, 0)


def test_the_longest_drive_is_the_total_distance():
    r = _solo({1: 4}, {1: 4})
    drive = rec("Mike", 1, 4, 1, "tee", "fairway", "DR")
    drive.hud = Hud(lie="fairway", tiles={"carry (game)": "262 yds", "total length": "291 yds"})
    st = r.side_stats(r.sides[0], _own(r, [drive]))
    assert st.longest_drive == 291
    assert any("longest drive went 291 yards" in f for f in r._side_facts(r.sides[0], st, None))


async def test_a_putt_is_logged_as_the_putter_whatever_the_club_box_read(tmp_path):
    import time

    from commentator.sources.gspro_log import parse_events

    e = _engine(tmp_path)
    base, impact = e.gspro.fires, time.monotonic()
    for ev in parse_events("* ------- Shot Fired! ------- *\nLaunchExt - Putting Mode\n"):
        e.gspro.apply(ev)
    shot = Shot(club="DR", ball_speed=3.9)   # a late HUD read caught the next tee's driver
    await e._read_putter(shot, base, impact, wait=0.0)
    assert shot.club == "PT" and shot.is_putt


async def test_no_recap_on_a_hole_change_after_the_round_or_onto_another_course(tmp_path):
    from commentator.models import HoleChangeEvent

    e = _engine(tmp_path)
    e.cfg.commentary.segments_on_hole_change = True
    e.state.current_hole, e.current_course = "Hole 9 Par 4", "Sheep Ranch"
    r = Round(league=LEAGUE, gspro_players=["Mike"], course="Sheep Ranch", scheduled_format=None, holes=9)
    r.ended = True
    e.round = r
    before = len(e.live_llm.prompts)
    await e.handle_hole_change(HoleChangeEvent(previous="Hole 9 Par 4", current="Hole 1 Par 4", course="Sheep Ranch"))
    e.round = None
    await e.handle_hole_change(HoleChangeEvent(previous="Hole 1 Par 4", current="Hole 1 Par 5", course="Torrey South"))
    assert len(e.live_llm.prompts) == before


def test_practice_closer_judges_form_against_the_handicap_without_net():
    r = Round(league=LEAGUE, gspro_players=["MikeD"], course="Wynn", scheduled_format=None, holes=3, is_league=False)
    r.apply_scorecard(Scorecard(holes=list(range(1, 19)), par={1: 4, 2: 3, 3: 5}, index={1: 5, 2: 9, 3: 1},
                                scores={"MIKED": {1: 4, 2: 5, 3: 5}, "SCORECARD": {}}))
    facts = "\n".join(r.closing_facts([], {}))
    # A 12 index predicts +2 over 3 holes; Mike shot +2.
    assert "plays off a 12 handicap index, which predicts about 2 over par over 3 holes; 2 over par is right around" in facts
    assert "net 1" not in facts and "handicap strokes that's" not in facts


def test_meta_talk_and_bare_acknowledgements():
    from commentator.grounding import bare_acknowledgement, meta_talk
    assert meta_talk("We'll move on without inventing a story.")
    assert meta_talk("There's no shot-by-shot record to dissect.")
    assert meta_talk("That's not in the facts, Duke.")
    assert not meta_talk("A real flusher, right at the flag.")
    assert bare_acknowledgement("I did, Duke.") and bare_acknowledgement("Yes.")
    assert not bare_acknowledgement("I did, and he's never going to live down that hook.")


def test_note_keywords_need_the_note_not_just_a_word():
    from commentator.grounding import note_keywords, uses_note
    keys = note_keywords("Built this booth. An occasional light ribbing is fine, but don't lean on it.",
                         {"josh"})
    assert keys == {"built", "booth"}
    assert uses_note("Josh, the man who built this booth, steps up.", keys)
    assert not uses_note("Here in the booth, we liked that one.", keys)   # one word isn't the note
    assert note_keywords("Pat Miller's grandmother; they play together.", {"pat", "miller"}) == {"grandmother"}
    assert not uses_note("Best ball in the league tonight.", note_keywords("Best putter in the league", set()))


def _noted_league():
    return League({
        "league_name": "Test League",
        "teams": [
            {"name": "Aces", "players": [{"name": "Josh", "notes": "Built this booth."},
                                         {"name": "Sam"}]},
            {"name": "Duffers", "players": [{"name": "Tony"}, {"name": "Priya"}]},
        ],
    })


def test_a_note_is_capped_per_round(tmp_path):
    from commentator.models import Line
    e = _engine_with(tmp_path, _noted_league())
    e._start_round(Round(league=e.league, gspro_players=["Josh"], course="X", scheduled_format=None))
    line = Line("pbp", "Josh, who built this booth, steps up.")
    for _ in range(Engine.NOTE_USES_PER_ROUND):
        assert e._grounded([line], "Josh steps up.", mode="round")[0]
        e._write_transcript([line], kind="banter")
    good, problems = e._grounded([Line("color", "The booth builder himself, Josh.")], "Josh.", mode="round")
    assert not good and any("used enough" in p for p in problems)
    assert any("leave alone" in f and "Josh" in f for f in e._format_facts())
    # A new round resets the count.
    e._start_round(Round(league=e.league, gspro_players=["Josh"], course="X", scheduled_format=None))
    assert e._grounded([line], "Josh steps up.", mode="round")[0]


def test_roster_facts_name_an_absent_partner(tmp_path):
    e = _engine_with(tmp_path, _noted_league())
    # A league round with one ball for the team is alternate shot: the partner IS playing.
    e._start_round(Round(league=e.league, gspro_players=["Josh"], course="X", scheduled_format=None))
    assert not any("Not playing" in f for f in e._roster_facts())
    # Josh practising alone: Sam is not.
    e._start_round(Round(league=e.league, gspro_players=["Josh"], course="X", scheduled_format=None,
                         is_league=False))
    facts = " ".join(e._roster_facts())
    assert "Not playing this round: Sam" in facts and "never describe them hitting" in facts
    assert "Tony" not in facts   # only partners of someone playing
    e._start_round(Round(league=e.league, gspro_players=["Josh", "Sam"], course="X", scheduled_format=None))
    assert not any("Not playing" in f for f in e._roster_facts())


def test_club_delivery_never_on_back_to_back_reactions(tmp_path):
    e = _engine_with(tmp_path, _noted_league())

    class Zero:
        def random(self):
            return 0.0
    e.rng = Zero()
    e._delivery_last = False
    assert e._delivery_turn(True)
    e._delivery_last = True
    assert not e._delivery_turn(True) and not e._delivery_turn(False)


def test_the_wrap_says_which_holes_found_the_water():
    r, card, shots = _three_hole_round()
    r.apply_scorecard(card)
    shots[10].tags.append("in_water")     # hole 3's wet 3-wood
    mike = r.side_for("Mike")
    assert r.side_stats(mike, shots).water_holes == [3]
    facts = "\n".join(r.closing_facts(shots, {}))
    assert "found the water on hole 3" in facts


def test_a_hole_recap_names_a_putt_for_birdie():
    from commentator.state import SessionState

    st = SessionState()
    st.shots = [rec("Mike", 1, 4, 1, "tee", "fairway", "DR"), rec("Mike", 1, 4, 2, "fairway", "green"),
                rec("Mike", 1, 4, 3, "green", "green", "PT"), rec("Mike", 1, 4, 4, "green", None, "PT")]
    for s in st.shots:
        s.hole = "hole 1"
    recap = st.hole_recap("hole 1")
    assert recap[2].startswith("Mike: putt for birdie") and recap[3] == "Mike: putt"


def test_a_hole_recap_counts_the_conceded_tap_in_as_a_putt():
    from commentator.state import SessionState

    st = SessionState()
    # Drive, wedge, a missed 24-footer, then a 12-footer left inside the gimme: three putts on the card.
    st.shots = [rec("Mike", 1, 4, 1, "tee", "rough", "DR"), rec("Mike", 1, 4, 2, "rough", "green"),
                rec("Mike", 1, 4, 3, "green", "green", "PT"), rec("Mike", 1, 4, 4, "green", "green", "PT")]
    for s in st.shots:
        s.hole = "hole 1"
    st.shots[2].outcome, st.shots[3].outcome = "missed", "conceded"
    recap = st.hole_recap("hole 1")
    assert recap[2].endswith(", missed")
    assert recap[3] == "Mike: putt, stopped inside the gimme circle"
    assert recap[4] == "Mike: conceded tap-in (counts as a putt)" and len(recap) == 5
    st.shots[3].outcome = "holed"
    assert st.hole_recap("hole 1")[3].endswith(", holed") and len(st.hole_recap("hole 1")) == 4


def test_classify_round_calls_a_second_round_on_a_posted_week_practice(tmp_path, monkeypatch):
    e = _engine(tmp_path)
    monkeypatch.setattr(e.store, "league_round_courses", lambda team: [])
    entry, practice = e._classify_round(["MikeD", "Sarah"], "Pebble Beach")
    assert practice is None and entry["week"] == 4
    # The Aces already posted Pebble Beach this season: the same pair back on it is practice.
    monkeypatch.setattr(e.store, "league_round_courses", lambda team: ["Wynn", "Pebble Beach"])
    _, practice = e._classify_round(["MikeD", "Sarah"], "Pebble Beach")
    assert practice and "already played week 4" in practice


def test_store_counts_one_league_round_per_round_not_per_side(tmp_path):
    from types import SimpleNamespace

    from commentator.store import Store

    s = Store(str(tmp_path / "s.db"))
    stats = SimpleNamespace(gross=40, par=36, fir=(0, 0), gir=(0, 0), scramble=(0, 0), sand=(0, 0), putts=0,
                            putt_holes=0, penalties=0, longest_drive=None, scores={}, source="card", holes_scored=9)
    for side in ("Mike", "Sarah"):
        s.record_round(course="Wynn", fmt="best ball", side=side, team="Aces", holes=9, stats=stats, strokes=0,
                       net_to_par=0, hole_number=lambda k: k)
    s.record_round(course="Wynn", fmt="best ball", side="Mike", team="Aces", holes=9, stats=stats, strokes=0,
                   net_to_par=0, hole_number=lambda k: k, is_league=False)
    assert s.league_round_courses("Aces") == ["Wynn"]
    assert s.league_round_courses("Duffers") == []


def test_classify_round_calls_the_wrong_nines_practice(tmp_path):
    e = _engine(tmp_path)
    entry = next(w for w in e.league.data["schedule"] if w.get("week") == 4)
    entry.update(start_hole=10, hole_count=9, holes="Back 9")
    e._setup_holes = (1, 18)     # the setup screen's default: the HOLES tab was never opened
    _, practice = e._classify_round(["MikeD", "Sarah"], "Pebble Beach")
    assert practice == "set up for all 18, but week 4 is Back 9"
    e._setup_holes = (1, 9)
    _, practice = e._classify_round(["MikeD", "Sarah"], "Pebble Beach")
    assert practice == "set up for the front 9, but week 4 is Back 9"
    e._setup_holes = (10, 9)
    entry_, practice = e._classify_round(["MikeD", "Sarah"], "Pebble Beach")
    assert practice is None and entry_["week"] == 4
    e._setup_holes = None        # no setup screen seen (booth started at the tee): the schedule stands
    assert e._classify_round(["MikeD", "Sarah"], "Pebble Beach")[1] is None


def _match_tab(course, mode):
    return {"setup_course": f"Play {course}", "setup_game_mode": mode,
            "tones": {"setup_players_tab": _TAB_OFF, "setup_match_tab": _TAB_ON}}


async def test_setup_scramble_game_mode_makes_a_two_player_round_one_team(tmp_path):
    e, r = await _setup_then_load(tmp_path, [_match_tab("Beaver Creek", "SCRAMBLE")] * 2)
    assert r.format == "scramble" and [s.gspro for s in r.sides] == [["Alex", "Jamie"]]


async def test_setup_any_other_game_mode_leaves_the_format_alone(tmp_path):
    e, r = await _setup_then_load(tmp_path, [_match_tab("Beaver Creek", "STROKE PLAY")] * 2)
    assert r.format is None and len(r.sides) == 2


async def test_setup_shamble_stops_the_log_calling_it_a_scramble(tmp_path):
    e, r = await _setup_then_load(tmp_path, [_match_tab("Beaver Creek", "SHAMBLE")] * 2)
    e._start_round(r)
    e.gspro.teammates.add(frozenset((0, 1)))
    e._apply_log_corrections()
    assert r.no_scramble and r.format is None


def test_without_a_league_the_shot_goes_to_the_rounds_gspro_name(tmp_path):
    from commentator.league import League
    e = _engine(tmp_path)
    e.league = League.load(None)
    assert e._player("Alex E Shot 1 350 yds", None) is None          # no round yet
    e._start_round(e._build_round(["Alex", "Sam"], "Somewhere", {"hole_count": 9}, is_league=False))
    assert e._player("Alex E Shot 1 350 yds", None) == "Alex"
    assert e._player("SAM +1 Shot 2 120 yds", None) == "Sam"
    # After the tee GSPro adds the stroke ordinal and a bracketed score: "David 2nd (E) Shot 2".
    assert e._player("Alex 2nd (E) Shot 2 158.3 YDS", None) == "Alex"
    assert e._player("Sam 3rd (+5) Shot 3 4 FT 7", None) == "Sam"


async def test_a_round_ends_after_its_last_shot_is_recorded(tmp_path):
    import asyncio

    from commentator.models import RoundEndEvent
    e = _engine(tmp_path)
    e._start_round(e._build_round(["MikeD"], "Somewhere", {"hole_count": 9}, is_league=False))
    seen = []

    async def last_shot():
        await asyncio.sleep(0.2)
        seen.append(e.round.ended)
    e._shot_tasks.add(asyncio.create_task(last_shot()))
    await e.handle_round_end(RoundEndEvent(reason="final scorecard", abandoned=True))
    assert seen == [False] and e.round.ended


def test_a_reaction_links_to_its_own_shot(tmp_path):
    from commentator.models import Line
    e = _engine(tmp_path)
    e._shot_id = 99   # the next shot's handler has moved on
    e._write_transcript([Line(e.speakers[0], "Found the fairway.")], "shot", shot_id=7)
    assert [r[0] for r in e.store.db.execute("SELECT shot_id FROM lines")] == [7]


def test_log_scores_beat_the_scorecard_read_which_fills_the_gaps(caplog):
    r = Round(league=LEAGUE, gspro_players=["MikeD"], course="Pebble Beach", scheduled_format=None)
    r.holes, r.start_hole, r.start_hole_assumed = 3, 1, False
    r.par = {1: 4, 2: 4, 3: 4}
    r.set_log_scores("MikeD", {1: 6, 9: 6})              # hole 9 is outside the round: ignored
    assert not r.complete()
    r.apply_scorecard(Scorecard(holes=[1, 2, 3], scores={"MikeD": {1: 5, 2: 4}}))
    assert "the scorecard read 5 on hole 1" in caplog.text
    side = r.sides[0]
    assert [r.card_score(side, s) for s in (1, 2, 3)] == [6, 4, None]
    r.set_log_scores("MikeD", {1: 6, 3: 4})
    assert r.complete()
    st = r.side_stats(side, [])
    assert st.gross == 14 and st.source == "log"


def test_a_hole_with_no_card_score_counts_the_conceded_tap_in():
    r = Round(league=LEAGUE, gspro_players=["MikeD"], course="Pebble Beach", scheduled_format=None)
    r.holes, r.par = 1, {1: 4}
    a = rec("Mike", 1, 4, 1, "tee", None, club="DR", round_id=r.id)
    b = rec("Mike", 1, 4, 2, None, None, round_id=r.id)
    c = rec("Mike", 1, 4, 3, None, None, club="PT", round_id=r.id)
    c.outcome = "conceded"                              # the lies weren't read (a lefty's flipped HUD)
    assert r.side_stats(r.sides[0], [a, b, c]).scores == {1: 4}
    c.outcome = "holed"
    assert r.side_stats(r.sides[0], [a, b, c]).scores == {1: 3}
    c.outcome = None                                    # no outcome and no green lie: unknown
    assert r.side_stats(r.sides[0], [a, b, c]).scores == {}


async def test_log_scores_end_a_complete_round_without_waiting_for_end_round(tmp_path):
    from commentator.models import RoundStartEvent

    e = _engine(tmp_path)
    e.cfg.source.gspro_log_truth = True
    e.current_par = 4
    e.current_course = "Pebble Beach"
    tee = Hud(player_text="Sarah E Shot 1 400 YDS", shot_number=1, score_to_par="E", to_pin="400 yards",
              others=[OtherPlayer("MikeD", "400 yards", "E")])
    await e.handle_round_start(RoundStartEvent(hud=tee))
    r = e.round
    r.set_holes(2)
    g = e.gspro
    g.first_hole = 0
    g.hole_scores = {(0, 0): 5, (1, 0): 4, (0, 1): 3}
    while not e._events.empty():
        e._events.get_nowait()
    e._check_log_round()
    assert r.log_scores == {"Sarah": {1: 5, 2: 3}, "MikeD": {1: 4}}
    g.hole_scores.pop((0, 1))                             # a mulligan of the holed putt reopens it
    e._check_log_round()
    assert r.log_scores["Sarah"] == {1: 5}
    g.hole_scores[(0, 1)], g.hole_scores[(1, 1)] = 4, 6
    g.round_completes += 1                                # "Round Is Complete": no End Round yet
    e._check_log_round()
    assert r.log_complete and r.complete()
    assert e._events.get_nowait().reason == "final scorecard"
    # The finished round's last green, read as a setup before the sign-off, isn't a shot to call.
    assert e._after_round(Hud(shot_number=3)) and not e._after_round(Hud(shot_number=1))
    # Ended, and long past the quiet window: still quiet until GSPro logs a new shot.
    r.ended, e._round_ended_at, e._shots_at_round_end = True, -1e9, g.fires
    assert e._after_round(Hud(shot_number=4, to_pin="4349 feet 9 inches"))
    g.fires += 1
    assert not e._after_round(Hud(shot_number=4))
    # Quit in GSPro (End Round): even an opening tee still on screen is the quit round's, not a shot to call.
    g.mode = "menu"
    assert e._after_round(Hud(shot_number=1))
    g.mode = "course"                                     # a course picked again: its opening tee is live
    assert not e._after_round(Hud(shot_number=1))


async def test_an_ended_rounds_log_scores_survive_the_end_round_reset(tmp_path):
    from commentator.models import RoundStartEvent
    from commentator.sources.gspro_log import LogEvent

    e = _engine(tmp_path)
    e.cfg.source.gspro_log_truth = True
    e.current_par = 4
    e.current_course = "Pebble Beach"
    tee = Hud(player_text="Sarah E Shot 1 400 YDS", shot_number=1, score_to_par="E", to_pin="400 yards",
              others=[OtherPlayer("MikeD", "400 yards", "E")])
    await e.handle_round_start(RoundStartEvent(hud=tee))
    g = e.gspro
    g.first_hole = 0
    e._check_log_round()
    g.hole_scores = {(0, 0): 5}
    g.apply(LogEvent("end_round"))                        # applied before the booth looked
    assert g.hole_scores == {}
    e._check_log_round()
    e._check_log_round()                                  # polled again while the round end is handled
    assert e.round.log_scores == {"Sarah": {1: 5}}


def test_a_scramble_takes_the_logged_team_score_and_a_reopened_hole_forgets_its_card_read():
    r = Round(league=LEAGUE, gspro_players=["MikeD", "Sarah"], course="Pebble Beach", scheduled_format="scramble")
    r.holes, r.start_hole, r.start_hole_assumed = 2, 1, False
    side = r.side_for("Mike")
    assert r.format == "scramble" and set(side.gspro) == {"MikeD", "Sarah"}
    r.apply_scorecard(Scorecard(holes=[1, 2], scores={"MikeD": {1: 6, 2: 4}, "Sarah": {1: 5, 2: 4}}))
    r.set_log_scores("MikeD", {1: 6, 2: 4})               # only the finisher's index is logged
    assert r.card_score(side, 1) == 6                     # not the partner's lower card read
    assert r.player_stats("Sarah", []).scores == {1: 6, 2: 4}   # each partner scores the team's
    r.set_log_scores("MikeD", {1: 6})                     # hole 2's holed putt taken back
    assert r.card_score(side, 2) is None and not r.complete()   # the partner's card row went with it


def _last_hole_pair(tmp_path):
    e = _engine(tmp_path)
    r = Round(league=LEAGUE, gspro_players=["MikeD", "Sarah"], course="Wynn", scheduled_format="best ball")
    r.holes, r.start_hole, r.start_hole_assumed, r.seq = 2, 1, False, 2
    r.par = {1: 4, 2: 3}
    e.round = r
    kicks = []
    e._kick_closer_branches = lambda key, scores, fixed=None: kicks.append((key, scores, fixed))
    return e, r, kicks


def test_a_group_pre_builds_the_closer_once_one_side_is_left_on_the_last_hole(tmp_path):
    e, r, kicks = _last_hole_pair(tmp_path)
    r.set_log_scores("MikeD", {1: 5})
    r.set_log_scores("Sarah", {1: 4})
    e._kick_group_closer(r)
    assert kicks == []                                  # both still playing the last hole
    r.set_log_scores("MikeD", {1: 5, 2: 3})
    e._kick_group_closer(r)
    sarah = next(s for s in r.sides if "Sarah" in s.gspro)
    mike = next(s for s in r.sides if "MikeD" in s.gspro)
    assert kicks == [(sarah.key, [2, 3, 4, 5], {mike.key: 3})]


async def test_a_group_closer_plays_only_if_the_others_finished_as_written(tmp_path):
    from commentator.models import Line

    e, r, _ = _last_hole_pair(tmp_path)
    r.set_log_scores("MikeD", {1: 5, 2: 3})
    r.set_log_scores("Sarah", {1: 4, 2: 4})
    sarah = next(s for s in r.sides if "Sarah" in s.gspro)
    mike = next(s for s in r.sides if "MikeD" in s.gspro)
    played = []
    e._play_prepared_closer = lambda strokes, key: played.append((strokes, key)) or True
    branch = {"side": sarah.key, "by_score": {4: ([Line(e.speakers[0], "x")], [b""])}}

    async def run(fixed):
        played.clear()
        e._closer_branches = {**branch, "fixed": fixed}
        stats = {s.key: r.side_stats(s, []) for s in r.sides}
        await e._sign_off(r, stats)
        return list(played)

    assert await run({mike.key: 3}) == [(4, sarah.key)]
    assert await run({mike.key: 4}) == []        # Mike's hole changed after the branches were written


def test_a_scramble_pick_inside_the_gimme_circle_records_the_teams_conceded_putt(tmp_path):
    from commentator.hud import Hud
    from commentator.models import Shot
    from commentator.state import ShotRecord

    e = _engine(tmp_path)
    r = Round(league=LEAGUE, gspro_players=["MikeD", "Sarah"], course="Wynn", scheduled_format="scramble")
    e.round = r

    def putt(who):
        sid = e.store.record_shot(player=who, hole="Hole 2, par 4", shot=Shot(club="PT"),
                                  before=Hud(lie="green", shot_number=4), after=Hud(lie="green"), tags=[],
                                  carry_game_yards=None, total_yards=None, offline_yards=None, course="Wynn",
                                  to_pin_yards=4.0, gimme_yards=1.2, fmt="scramble", attributed=True,
                                  outcome="missed", is_league=False)
        return sid, ShotRecord(who, Shot(club="PT"), "Hole 2, par 4", Hud(), [], round_id=r.id,
                               hole_seq=2, shot_number=4, shot_id=sid, outcome="missed")

    (a, ra), (b, rb) = putt("MikeD"), putt("Sarah")
    e._fire_rows = {10: (a, ra, 0, 1), 11: (b, rb, 1, 1)}
    e.gspro.pick_gimmes.append((0, 1))           # GSPro picked MikeD's (first) putt, inside the circle
    e._apply_log_corrections()
    e._apply_log_corrections()                     # idempotent
    rows = e.store.db.execute("SELECT player, shot_number, outcome FROM shots ORDER BY id").fetchall()
    assert [tuple(x) for x in rows] == [("MikeD", 4, "conceded"), ("Sarah", 4, "missed"), ("MikeD", 5, "gimme")]
    e._record_result(a, "missed", "[late watch]", putt=True, rec=ra)   # a late watch can't undo it
    assert ra.outcome == "conceded"


def test_best_ball_team_gross_beside_each_partners_own_score(tmp_path):
    r = Round(league=LEAGUE, gspro_players=["MikeD", "SarahK"], course="X", scheduled_format="best_ball",
              holes=3, is_league=False)
    r.par = {1: 4, 2: 3, 3: 5}
    r.set_log_scores("MikeD", {1: 3, 2: 5})     # birdie, double
    r.set_log_scores("SarahK", {1: 5, 2: 3})    # bogey, par
    team, holes, own = r.team_gross("Aces")
    assert (team, holes) == (-1, 2)             # better ball: 3 then 3, against par 7
    assert {s.label: v for s, v in own} == {"Mike": 1, "Sarah": 1}

    eng = _engine(tmp_path)
    eng.round = r
    r.seq = 3
    r.set_log_scores("SarahK", {1: 5, 2: 3, 3: 4})   # Sarah's birdie already in on the hole in play
    facts = "\n".join(eng._team_score_facts("Mike"))
    assert "Team Aces best-ball score through 2 completed holes: -1" in facts
    assert "Mike on their own is +1; Sarah on their own is +1" in facts
    assert "never give the team number as one player's score" in facts
    assert "Sarah has already finished this hole in 4 (a birdie), so the team has 4 here unless Mike beats it." in facts


def test_team_gross_only_for_best_ball():
    r = Round(league=LEAGUE, gspro_players=["MikeD", "SarahK"], course="X", scheduled_format="scramble",
              holes=3, is_league=False)
    assert r.team_gross("Aces") is None
    solo = Round(league=LEAGUE, gspro_players=["MikeD"], course="X", scheduled_format="best_ball", holes=3)
    assert solo.team_gross("Aces") is None


def test_short_missed_putt_is_called_a_miss_not_good_touch():
    from commentator.engine import _short_miss_fact
    from commentator.hud import Hud

    fact = _short_miss_fact(Hud(to_pin="4 feet 1 inches", shot_number=4), par=4)
    assert "about 4 feet, and it MISSED: it was for par, so the hole goes down as a bogey" in fact
    assert "never as good touch" in fact
    assert _short_miss_fact(Hud(to_pin="31 feet 11 inches", shot_number=3), par=4) is None   # a lag


def test_setup_corrects_a_ball_called_on_the_green_that_rolled_off(tmp_path):
    from commentator.hud import Hud

    eng = _engine(tmp_path)
    eng.state.current_hole = "hole 1"
    called = rec("Josh", 1, 4, 2, "rough", "green")
    called.shot_id = 796
    eng.state.record_shot(called)
    setup = Hud(lie="rough", shot_number=3)
    facts = eng._finish_correction_facts(setup, "Josh")
    assert facts and "finished in the rough" in facts[0] and "If the booth said green" in facts[0]
    assert setup._lie_correction
    assert eng._finish_correction_facts(Hud(lie="rough", shot_number=3), "Josh") == []   # once only
    # Same family (OCR's rough vs deep rough), a penalty between, or a new hole: nothing to correct.
    other = rec("Dan", 1, 4, 2, "fairway", "rough")
    other.shot_id = 797
    eng.state.record_shot(other)
    assert eng._finish_correction_facts(Hud(lie="deep rough", shot_number=3), "Dan") == []
    assert eng._finish_correction_facts(Hud(lie="green", shot_number=4), "Dan") == []
    eng.state.current_hole = "hole 2"
    assert eng._finish_correction_facts(Hud(lie="green", shot_number=3), "Dan") == []
    # A scramble: the next shot can be from the partner's picked ball, so a different lie proves nothing.
    eng.state.current_hole = "hole 1"
    eng.round = Round(league=LEAGUE, gspro_players=["MikeD"], course="X", scheduled_format=None, holes=9,
                      is_league=False)
    eng.round.format = "scramble"
    picked = rec("Dan", 1, 4, 2, "fairway", "rough")
    picked.shot_id = 798
    eng.state.record_shot(picked)
    assert eng._finish_correction_facts(Hud(lie="fairway", shot_number=3), "Dan") == []


def test_round_sheet_waits_three_holes_then_gives_the_booth_the_numbers(tmp_path):
    from commentator.hud import Hud
    from commentator.models import Line

    eng = _engine(tmp_path)
    r = Round(league=LEAGUE, gspro_players=["MikeD"], course="X", scheduled_format=None, holes=9, is_league=False)
    r.par = {q: 4 for q in range(1, 10)}
    eng.round, eng.current_par = r, 4

    def hole(seq, approach_from, putts):   # an approach to the green, then putts (feet, outcome)
        a = rec("Mike", seq, 4, 2, "fairway", "green", round_id=r.id)
        a.to_pin_before = f"{approach_from} yards"
        eng.state.record_shot(a)
        for i, (feet, outcome) in enumerate(putts):
            p = rec("Mike", seq, 4, 3 + i, "green", "green", "PT", round_id=r.id)
            p.to_pin_before, p.outcome = f"{feet} feet", outcome
            eng.state.record_shot(p)

    hole(1, 150, [(4, "conceded")])
    hole(2, 140, [(9, "conceded")])
    putt = Hud(lie="green", to_pin="12 feet", shot_number=3)
    r.set_log_scores("MikeD", {1: 4, 2: 4})
    assert eng._round_form_facts("Mike", True, putt) == []          # two holes: too early
    hole(3, 130, [(18, "missed"), (2, "holed")])
    r.set_log_scores("MikeD", {1: 4, 2: 4, 3: 4})
    r.seq = 4
    (sheet,) = eng._round_form_facts("Mike", True, putt)
    assert sheet.startswith("Round so far for Mike")
    assert "Score: even par through 3 holes (3 pars)" in sheet
    assert "0-5 ft 1 of 2; 5-10 ft 0 of 1; 15-25 ft 0 of 1" in sheet
    assert "Birdie putts from inside 15 feet: 0 of 2 converted" in sheet
    assert ("from 150-250 yd 1 of 1 on the green, average 4 ft from the hole; "
            "from 100-150 yd 2 of 2 on the green, average 14 ft from the hole") in sheet
    assert "Greens in regulation: 3 of 3" in sheet
    assert "Already used" not in sheet
    # the same sheet whatever the shot: the booth picks what fits
    assert eng._round_form_facts("Mike", False, Hud(lie="tee", to_pin="400 yards", shot_number=1)) == [sheet]

    eng._write_transcript([Line("color", "Nothing today from inside ten feet for Mike, nought for three.")], "live")
    assert eng._round_form_facts("Mike", True, putt) == []          # just used: rests this hole and the next
    r.seq = 6
    (sheet,) = eng._round_form_facts("Mike", True, putt)
    assert "Already used this round: putting" in sheet
    assert eng.store.recent_form_mentions("Mike") == []             # today's don't count as a recent round
    eng._write_transcript([Line("color", "Sarah's driving has been excellent today.")], "live")
    assert [r[0] for r in eng.store.db.execute("SELECT player FROM form_mentions")] == ["Mike"]   # not credited to Mike
    eng._form_pending = (r.id, r.side_for("Mike").key, ["Mike"], r.seq)
    eng._write_transcript([Line("pbp", "Mike is up next."), Line("color", "Sarah's putting has been excellent today.")], "live")
    eng._write_transcript([Line("pbp", "Mike and Sarah: three of four fairways today, the pair of them.")], "live")
    assert eng.store.db.execute("SELECT COUNT(*) FROM form_mentions").fetchone()[0] == 1          # neither is Mike's
    eng._round_form_facts("Nobody", True, putt)                     # another player's setup clears the pending sheet
    eng._write_transcript([Line("color", "Mike's fairways so far: all of them.")], "live")
    assert eng.store.db.execute("SELECT COUNT(*) FROM form_mentions").fetchone()[0] == 1
    r.format = "scramble"
    (sheet,) = eng._round_form_facts("Mike", True, putt)
    assert "Approaches:" not in sheet                               # a partner's shot isn't this ball's finish
    del r.par[2]
    (sheet,) = eng._round_form_facts("Mike", True, putt)
    assert "Score:" not in sheet                                    # a missing par would read as a birdie-free +4


def test_form_topics_needs_a_stat_and_a_subject():
    from commentator.engine import form_topics

    assert form_topics("Finds the fairway, lovely.") == []
    assert form_topics("He's hit five of six fairways so far.") == ["fairways"]
    assert form_topics("Two three-putts today, he'll want this one close.") == ["three-putts", "putting"]
    assert form_topics("His approaches are averaging twelve feet from the hole.") == ["approach leaves"]


def test_length_norm_judges_a_shot_against_the_players_own_game(tmp_path):
    from commentator.hud import Hud
    from commentator.models import Shot
    from commentator.prompts import system_prompt

    eng = _engine(tmp_path)
    tee = Hud(lie="tee", to_pin="400 yards", shot_number=1)
    assert eng._length_norm_facts("Kathy", "DR", True, 400, 117) == []      # no sample yet: nothing to judge by
    for total in (100, 110, 105, 115, 95):
        eng.store.record_shot(player="Kathy", hole="Hole 1, par 4, 400 yards", shot=Shot(club="DR"), before=tee,
                              after=Hud(), tags=[], carry_game_yards=None, total_yards=total, offline_yards=None,
                              to_pin_yards=400)
    for total in (60, 62, 58, 61, 59):   # full 7-irons, the green out of reach
        eng.store.record_shot(player="Kathy", hole="Hole 2", shot=Shot(club="7I"), before=Hud(lie="fairway"),
                              after=Hud(), tags=[], carry_game_yards=None, total_yards=total, offline_yards=None,
                              to_pin_yards=150)
    eng.store.record_shot(player="Kathy", hole="Hole 2", shot=Shot(club="7I"), before=Hud(lie="fairway"),
                          after=Hud(), tags=[], carry_game_yards=None, total_yards=50, offline_yards=None,
                          to_pin_yards=55)   # a partial: the pin was in reach, not her full 7-iron
    for _ in range(2):   # mishits from well out still count: a full swing is judged by the pin, not the result
        eng.store.record_shot(player="Kathy", hole="Hole 2", shot=Shot(club="7I"), before=Hud(lie="fairway"),
                              after=Hud(), tags=[], carry_game_yards=None, total_yards=20, offline_yards=None,
                              to_pin_yards=150)
    assert eng._is_drive(tee) and not eng._is_drive(Hud(lie="tee", to_pin="140 yards", shot_number=1))
    assert eng._length_norm_facts("Kathy", "DR", True, 400, 112) == [
        "Kathy's drives usually go about 105 yards in total. This one was about that, a normal length for them: nothing to remark on."]
    assert eng._length_norm_facts("Kathy", "DR", True, 400, 80) == [
        "Kathy's drives usually go about 105 yards in total. This one went 25 yards shorter than that."]
    assert eng._length_norm_facts("Kathy", "7I", False, 150) == ["Kathy's full 7-iron shots usually go about 59 yards in total."]
    assert eng._length_norm_facts("Kathy", "7I", False, 30, 20) == []   # a bump-and-run isn't a short 7-iron
    assert eng._length_norm_facts("Kathy", None, False, 150) == []
    assert "Judge a player's distance against their own game" in system_prompt(eng.cfg, LEAGUE)


def test_in_round_prompts_know_the_day_and_time():
    from datetime import datetime

    from commentator.prompts import clock_fact, shot_prompt

    assert clock_fact(datetime(2026, 10, 20, 17, 31)) == (
        "Local time: Tuesday evening, 5:31 pm. Background only: a day or time-of-day remark now and then at most.")
    assert clock_fact(datetime(2026, 10, 24, 9, 5)).startswith("Local time: Saturday morning, 9:05 am.")
    _, facts_text = shot_prompt(player="Mike", team=None, facts=["Club: driver."], hole="Hole 1", recent_lines=[],
                                routine=True, has_images=False)
    assert "Local time:" in facts_text


def test_news_and_trivia_prompts_place_the_story_in_time_and_stick_to_the_article():
    from commentator.prompts import news_prompt, trivia_prompt

    user, facts = news_prompt(topic='"X" (via espn.com, published yesterday)')
    assert "Local time:" in facts and "not something happening now" in user
    user, _ = trivia_prompt(course="Riviera", title="Riviera Country Club", article="text")
    assert "a nickname isn't a namesake" in user
