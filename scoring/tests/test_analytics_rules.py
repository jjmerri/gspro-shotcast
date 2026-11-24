"""shotcast_scoring.analytics and handicap_index from plain mappings, as Shotcast Cloud calls them (the
booth's own tests cover them through its season file)."""

import random

import pytest

from shotcast_scoring import analytics as A
from shotcast_scoring import handicap_index as H


def stroke(n, lie, yards, outcome=None, **over):
    return {"id": over.pop("id", n), "player": "Ann", "session_id": 1, "date": "2026-11-20", "course": "Links",
            "hole": "Hole 1, par 4, 400 yards", "shot_number": n, "lie_before": lie, "is_putt": int(lie == "green"),
            "to_pin_yards": yards, "outcome": outcome, "gimme_yards": None, "finish_yards": None, "left_out": 0,
            "is_league": 1, **over}


def test_strokes_gained_with_the_usual_gimme_is_the_strokes_gained_with_none_plus_usual_gain():
    """The cloud keeps a round's strokes gained before it knows the league's usual gimme circle."""
    rng = random.Random(7)
    for _ in range(300):
        rows, n = [], 0
        for hole in range(1, rng.randint(2, 6)):
            label = f"Hole {hole}, par 4, 400 yards"
            for k, (lie, yards) in enumerate([("tee", 400), ("fairway", rng.uniform(60, 180)), ("green", rng.uniform(1, 12))], 1):
                n += 1
                rows.append(stroke(k, lie, yards, id=n, hole=label, gimme_yards=rng.choice([None, 1.0, 1.5])))
            n += 1
            rows.append(stroke(4, "green", 0.5, "gimme", id=n, hole=label, gimme_yards=rng.choice([None, 1.0, 2.0])))
        holes = A.holes_of(rows)
        usual = rng.choice([None, 2.5, 3.0, 6.0])
        want = {s["shot_id"]: s["sg"] for s in A.strokes_gained(holes, usual)}
        got = {s["shot_id"]: s["sg"] + A.usual_gain(s["gimme_r"], usual) for s in A.strokes_gained(holes)}
        assert got.keys() == want.keys()
        assert all(got[k] == pytest.approx(want[k], abs=1e-12) for k in want)


def test_a_holes_strokes_gained_and_recovery_from_plain_mappings():
    holes = A.holes_of([stroke(1, "tee", 400), stroke(2, "rough", 150), stroke(3, "sand", 20, finish_yards=2.0),
                        stroke(4, "green", 2.0, "holed")])
    [h] = holes
    assert (h["complete"], h["score"], h["par"], h["hole"]) == (True, 4, 4, 1)
    sg = A.strokes_gained(holes)
    assert [s["category"] for s in sg] == ["off_tee", "approach", "around_green", "putting"]
    assert sum(s["sg"] for s in sg) == pytest.approx(A.expected("tee", 400) - 4)
    rec = A.recovery(holes)["Ann"]
    assert [(r["lie"], r["band"], r["green_pct"]) for r in rec] == [("rough", "long", 0.0), ("sand", "greenside", 100.0)]


def test_a_clubs_gapping_from_plain_swings():
    swings = [{"id": i, "club": "7I", "carry": 150 + i, "game": 0, "total_yards": 160 + i, "ball_speed": 110.0,
               "club_speed": 80.0, "launch_angle": 17.0, "total_spin": 6500.0, "spin_axis": 4.0,
               "start_direction": 1.0, "handed": "R", "lie_before": "fairway"} for i in range(6)]
    [c] = A.gapping_from(swings, {0: 3.0})
    assert (c["club"], c["n"], c["carry"], c["side_logged"], c["miss"]) == ("7I", 6, 152.5, 1, "starts straight, fade")
    assert c["ellipse"] is not None and len(c["landing"]) == 6


def test_a_handicap_index_from_plain_cards():
    tee = {"tee": "White", "rating": 70.0, "slope": 125, "yards": {h: 380 for h in range(1, 19)},
           "par": {h: 4 for h in range(1, 19)}, "index": {h: h for h in range(1, 19)}}
    assert H.pick_tee([tee, {**tee, "tee": "Blue", "yards": {h: 420 for h in range(1, 19)}}],
                      {1: [419.0], 2: [421.0]}, [1, 2])["tee"] == "Blue"
    cards = [("Ann", "Links", f"2026-11-{d:02}", {h: 5 for h in range(1, 19)}, {h: 4 for h in range(1, 19)}, tee, 0)
             for d in range(1, 5)]
    ann = H._index_of(cards)["Ann"]
    assert ann["rounds"] == 4 and ann["index"] == pytest.approx(H.index_from([h["differential"] for h in ann["history"]]))
    assert ann["history"][0]["differential"] == round((90 - 70.0) * 113 / 125, 1)
