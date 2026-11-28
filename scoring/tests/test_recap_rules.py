"""The shared recap rules on plain data (shotcast_scoring.recap, .grounding and highlights.turn_shot):
what Shotcast Cloud tells a league week with. The app's own recap tests (tests/test_recap.py) run
the same code through commentator.recap, which imports it."""
import json

from shotcast_scoring import highlights as H
from shotcast_scoring import recap as R
from shotcast_scoring.board import Board, Entry, LeagueData
from shotcast_scoring.grounding import check_line, meta_talk

LEAGUE = LeagueData(
    team_players={"t1": ["Ann", "Bo"], "t2": ["Cy", "Di"], "t3": ["Ed"]},
    schedule=[{"week": 1, "course": "Links", "end_date": "2026-11-15"},
              {"week": 2, "course": "Pebble", "end_date": "2026-11-22"}],
    team_names={"t1": "Aces", "t2": "Birdies", "t3": "Pars"},
)


def _facts():
    board = Board({1: [Entry("t1", 2), Entry("t2", -1), Entry("t3", 0)],
                   2: [Entry("t1", -2), Entry("t2", -1), Entry("t3", None)]}, LEAGUE, [10.0, 6.0, 3.0])
    facts = R.week_facts(board, 2, "2026-11-23")
    facts["holes"] = {"t1": {1: 0, 2: -1, 3: -1}, "t2": {1: -1, 2: 0, 3: 0}}
    return facts


def test_a_week_is_read_off_a_board_of_plain_league_data():
    facts = _facts()
    assert (facts["week"], facts["course"], facts["end_date"]) == (2, "Pebble", "2026-11-22")
    assert [(r["team"], r["name"], r["players"], r["net"], r["place"], r["points"]) for r in facts["rows"]] == \
        [("t1", "Aces", ["Ann", "Bo"], -2, 1, 10.0), ("t2", "Birdies", ["Cy", "Di"], -1, 2, 6.0)]
    # The standings after it, with each team's place before it; who didn't post.
    assert [(s["name"], s["points"], s["place"], s["was"]) for s in facts["standings"]] == \
        [("Birdies", 16.0, 1, 1), ("Aces", 13.0, 2, 3), ("Pars", 6.0, 3, 2)]
    assert facts["missed"] == ["Pars"]


def test_the_week_told_as_one_tournament_picks_and_orders_the_clips():
    facts = _facts()
    story = R.race(facts)
    assert [e["text"] for e in story["events"]] == [
        "Birdies lead after hole 1 at -1", "Tied for the lead after hole 2 at -1: Aces, Birdies",
        "Aces pull clear alone after hole 3 at -2 (falling back: Birdies)", "Aces won it on the last hole"]
    assert R.standing(story, "t1", 1) == "-1, tied for the lead"
    assert R.board(story, 2, "t2", {r["team"]: r for r in facts["rows"]})["rows"] == [
        {"place": 1, "tied": False, "name": "Aces", "net": -2},
        {"place": 2, "tied": False, "name": "Birdies", "net": -1, "on": True}]
    moment = {"id": 1, "player": "Ann", "at": 5.0, "level": 7, "bad": False, "what": "Ann holes a 20 ft putt on 3",
              "hole": 3}
    other = {"id": 2, "player": "Cy", "at": 9.0, "level": 6, "bad": False, "what": "Cy chips close on 1", "hole": 1}
    clips = R.choose(facts, {"t1": {"key": "r1", "moments": [moment]}, "t2": {"key": "r2", "moments": [other]}})
    # In hole order across the field, the turns no clip showed told on the next one.
    assert [(c["team"], c["hole"]) for c in clips] == [("t2", 1), ("t1", 3)]
    assert clips[1]["since"] == ["Tied for the lead after hole 2 at -1: Aces, Birdies"]
    user, facts_text = R.prompt(facts, clips, {"pbp": "Nigel", "color": "Duke"})
    assert "- 1. Aces (Ann & Bo): -2, 10 points" in facts_text and "No score posted: Pars." in facts_text
    assert user.startswith(facts_text) and "voiced by Nigel and Duke" in user


def test_the_models_lines_are_read_and_whats_wrong_with_them_said():
    clips = [{"team": "t1"}, {"team": "t2", "since": ["x"]}]
    assert R.schema(["pbp", "color"])["required"] == ["intro", "clips", "outro"]
    raw = json.dumps({"intro": [{"speaker": "pbp", "text": " Week two  at Pebble. "}],
                      "clips": [{"clip": 0, "lines": [{"speaker": "color", "text": "word " * 31},
                                                      {"speaker": "nobody", "text": "dropped"}]}],
                      "outro": []})
    lines, problems = R.script(raw, clips, ["pbp", "color"])
    assert lines["intro"] == [("pbp", "Week two at Pebble.")] and len(lines["clips"][0]) == 1
    assert problems == ["write the outro", "write lines for clip 1",
                        "keep each clip's lines to 26 words between them, 42 catching up off camera (clip 0)"]
    assert R.script("not json", clips, ["pbp"])[1][0] == "write the intro"


def test_a_recaps_line_is_held_to_its_facts():
    facts = "- 1. Aces (Ann & Bo): -2, 10 points\n- 2. Birdies: -1, 6 points"
    assert check_line("Aces win it at two under, ten points in the bag.", facts, "", mode="news").ok
    bad = check_line("Aces take 12 points and three putts on the last.", facts, "", mode="news")
    assert bad.problems == ['the number "12" is not in the facts', '"three" is a count the facts don\'t give']
    assert meta_talk("Nothing on record for the Pars this week.") == "nothing on record"


def test_the_shot_a_lead_turned_on_from_a_rounds_plain_shots():
    shots = [{"id": 1, "player": "Ann", "hole": "Hole 3, par 4", "shot_number": 1, "ts": 100.0, "lie_before": "tee"},
             {"id": 2, "player": "Ann", "hole": "Hole 3, par 4", "shot_number": 2, "ts": 130.0, "to_pin_yards": 140,
              "finish_yards": 7.0},
             {"id": 3, "player": "Ann", "hole": "Hole 3, par 4", "shot_number": 3, "ts": 160.0, "is_putt": 1,
              "to_pin_yards": 7.0, "outcome": "holed"}]
    made = H.turn_shot(shots, 3, "made")
    assert (made["id"], made["what"], made["bad"], made["post"]) == (3, "Ann holes a 21 ft putt on 3", False, H.TURN_POST)
    assert H.turn_shot(shots, 3, "setup")["what"] == "Ann hits it to 21 ft from 140 yd on 3"
    assert H.turn_shot(shots, 4, "miss") is None
