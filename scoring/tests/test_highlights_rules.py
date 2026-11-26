"""The shared highlight rules on plain dicts (shotcast_scoring.highlights): what Shotcast Cloud picks a
round's reel with. The app's own tests check the same rules pick as its highlights.py does."""
from shotcast_scoring import highlights as H


def shot(i, player, hole, n, ts, **kw):
    base = {"id": i, "player": player, "hole": f"Hole {hole}, par 4", "shot_number": n, "ts": ts,
            "session_id": 1, "gimme_yards": 1.0, "tags": "[]"}
    return base | kw


ROUND = [
    shot(1, "a", 1, 1, 100.0, lie_before="tee", total_yards=250),
    shot(2, "a", 1, 2, 130.0, lie_before="fairway", to_pin_yards=150, finish_yards=1.5),   # stuffed: 8
    shot(3, "a", 1, 3, 160.0, is_putt=1, lie_before="green", to_pin_yards=1.5, outcome="holed"),
    shot(4, "b", 2, 1, 200.0, lie_before="tee", total_yards=240),
    shot(5, "b", 2, 2, 230.0, is_putt=1, lie_before="green", to_pin_yards=12, outcome="holed"),  # 36 ft, eagle
]


def test_a_rounds_moments_are_picked_and_named_by_whatever_names_the_player():
    got = H.pick(ROUND)
    assert [(m["shot"]["id"], m["what"], m["bad"]) for m in got] == [
        (2, "a stuffs it to 4 ft from 150 yd on 1 to set up birdie", False),
        (3, "a holes a 4 ft putt for birdie on 1", False),
        (5, "b holes a 36 ft putt for eagle on 2", False),
    ]
    assert [H.entry(m)["hole"] for m in got] == [1, 1, 2]


def test_a_clip_ends_by_the_booths_lines_and_the_streams_timing_is_a_parameter():
    rows = [(1, 2, 131.0, "shot", "What a shot " * 5, None, None), (1, None, 150.0, "banter", "So anyway", None, None)]
    talk = H.talk(rows, ROUND)
    stuffed = next(m for m in H.pick(ROUND, talk=talk) if m["shot"]["id"] == 2)
    assert stuffed["stop"] is not None and stuffed["post"] <= stuffed["stop"]
    later = H.Timing(shot_shows=7.0, talk_lag=8.0)
    slow = next(m for m in H.pick(ROUND, talk=H.talk(rows, ROUND, timing=later), timing=later) if m["shot"]["id"] == 2)
    assert slow["post"] != stuffed["post"]


def test_a_line_never_aired_is_dropped_or_placed_by_its_words():
    rows = [(1, 2, 131.0, "shot", "Lovely", 132.0, 1.0), (1, 2, 140.0, "reaction", "And again " * 8, None, None)]
    dropped = H.talk(rows, ROUND)[0][1]
    estimated = H.talk(rows, ROUND, unaired="estimate")[0][1]
    assert len(dropped) == 1 and len(estimated) == 2
