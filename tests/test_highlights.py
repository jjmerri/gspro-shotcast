import json

from commentator import highlights as H
from test_record_clips import FakeS3, _stream

ROOT = "live/"
_id = iter(range(1, 10_000))


def shot(player, hole, n, ts, par=4, putt=False, lie="fairway", to_pin=None, finish=None, outcome=None,
         total=None, offline=None, hazard=None, fmt=None, picked=None, mulligan=0, wet=False):
    return {"id": next(_id), "player": player, "hole": f"Hole {hole}, par {par}, 400 yards", "shot_number": n,
            "ts": ts, "is_putt": int(putt), "lie_before": "green" if putt else lie, "to_pin_yards": to_pin,
            "finish_yards": finish, "outcome": outcome, "total_yards": total, "offline_yards": offline,
            "hazard_x": hazard, "fmt": fmt, "picked": picked, "mulligan": mulligan, "left_out": 0,
            "gimme_yards": 1.0, "shared_ball": 0, "tags": '["in_water"]' if wet else "[]"}


def test_picks_the_big_moments_and_one_bad_shot_a_player_in_order():
    shots = [
        shot("Ann", 1, 1, 10, lie="tee", total=250),
        shot("Ann", 1, 2, 20, to_pin=150, finish=1.5),                       # stuffed to 4.5 ft: 8
        shot("Ann", 1, 3, 30, putt=True, to_pin=1.5, outcome="holed"),
        shot("Bob", 1, 1, 40, lie="tee", total=240, hazard=1.0, wet=True),   # penalty: bad
        shot("Bob", 2, 1, 50, lie="tee", total=230, hazard=1.0, wet=True),   # another: only one a player
        shot("Bob", 2, 2, 60, putt=True, to_pin=12, outcome="holed"),       # 36 ft: 8
        shot("Ann", 3, 1, 70, lie="tee", total=260),
        shot("Ann", 3, 2, 80, putt=True, to_pin=1, outcome="missed"),       # missed from 3 ft: bad
        shot("Ann", 3, 3, 90, putt=True, to_pin=0.1, outcome="holed"),
        shot("Ann", 4, 1, 100, lie="tee", total=250, mulligan=1, hazard=1.0),   # a mulligan: never
    ]
    got = H.pick(shots)
    assert [(m["shot"]["ts"], m["bad"]) for m in got] == [(20, False), (40, True), (60, False), (80, True)]
    assert got[0]["what"] == "Ann stuffs it to 4 ft from 150 yd on 1 to set up birdie"
    assert got[2]["what"] == "Bob holes a 36 ft putt for eagle on 2"
    assert got[3]["what"] == "Ann misses from 3 ft on 3"


def test_three_putts_misses_off_line_and_long_penalty_drives_are_bad_ones():
    shots = [shot("Ann", 1, 1, 10, lie="tee", total=340, hazard=1.0),            # long, but wet: not a bomb
             shot("Ann", 1, 3, 15, to_pin=100),                                   # (the penalty: no stroke 2)
             shot("Ann", 2, 1, 20, lie="tee", total=260, hazard=1.0, wet=True),
             shot("Bob", 3, 2, 30, offline=60),
             shot("Cal", 4, 2, 40, putt=True, to_pin=10, outcome="conceded"),
             shot("Cal", 4, 3, 50, putt=True, to_pin=3, outcome="conceded"),
             shot("Cal", 4, 4, 55, putt=True, outcome="gimme")]
    assert H.pick(shots) == []   # no good ones to go with them: no more bad ones than good
    shots += [shot(p, h, 3, ts, putt=True, to_pin=10, outcome="holed") for p, h, ts in
              (("Ann", 5, 60), ("Bob", 6, 70), ("Cal", 7, 80))]   # 30 ft for par: big ones
    got = H.pick(shots)
    assert [(m["shot"]["ts"], m["bad"]) for m in got if m["bad"]] == [(10, True), (30, True), (40, True)]
    assert [m["what"] for m in got if m["bad"]][1:] == ["Bob sprays one 60 yd off line on 3", "Cal three-putts on 4"]
    assert [m["post"] for m in got if m["shot"]["ts"] == 40] == [H.POST_PUTT + 10]   # from the first putt


def test_a_dry_hazard_entry_isnt_a_penalty_and_a_scramble_putt_can_miss():
    shots = [shot("Ann", 1, 1, 10, lie="tee", total=340, hazard=1.0),   # logged an entry, played on: dry
             shot("Ann", 1, 2, 20, to_pin=100),
             shot("Bob", 2, 3, 30, putt=True, to_pin=1, outcome="missed", fmt="scramble")]
    assert [m["what"] for m in H.pick(shots)] == ["Ann bombs a 340 yd drive on 1", "Bob misses from 3 ft on 2"]


def test_solid_shots_fill_in_and_personal_bests_are_big():
    shots = [shot("Ann", 1, 1, 10, lie="tee", total=285),                     # in the fairway: solid
             shot("Ann", 1, 2, 20, to_pin=150, finish=7),                    # 21 ft from 150: solid
             shot("Ann", 1, 3, 30, putt=True, to_pin=3, outcome="holed"),   # 9 ft for birdie
             shot("Ann", 2, 5, 40, putt=True, to_pin=3, outcome="holed"),   # 9 ft for bogey: no
             shot("Bob", 3, 1, 50, lie="tee", total=270),                    # Bob's longest yet
             shot("Ann", 4, 4, 60, putt=True, to_pin=3, outcome="holed")]   # 9 ft for par: solid
    shots[0]["lie_after"], shots[1]["lie_after"] = "fairway", "green"
    said = lambda before: [m["what"] for m in H.pick(shots, before=before)]
    assert said({}) == ["Ann finds the green, 21 ft from 150 yd on 1 to set up birdie",
                        "Ann holes a 9 ft putt for birdie on 1", "Ann holes a 9 ft putt for par on 4"]   # two a hole
    pb = {"Bob": {"drive": (265, 40)}}
    assert said(pb)[-1] == "Bob's longest drive yet: 270 yd on 3"
    assert "Ann holes a 9 ft putt for par on 4" not in said(pb)   # Bob gets his turn first
    assert "Bob's longest drive yet: 270 yd on 3" not in said({"Bob": {"drive": (265, 3)}})   # too few to beat
    assert said({"Bob": {"drive": (265, 3)}})[-1] != "Bob's longest drive yet: 270 yd on 3"   # too few to beat
    assert said({"Bob": {"drive": (280, 40)}})[-1] == "Bob hits a 270 yd drive on 3, 10 yd off a personal best"
    assert not any("Bob" in x for x in said({"Bob": {"drive": (300, 40)}}))   # not that close
    shots[1]["lie_after"] = "sand"   # 21 ft away, but not on the green
    assert not any("finds the green" in x for x in said({}))
    del shots[:4]   # no pin distances to go on: a drive in the fairway still counts
    shots[0].update(total_yards=285, lie_after="fairway", finish_yards=None)
    assert "Bob splits the fairway, 285 yd on 3" in said({})
    chip = [shot("Dee", 6, 3, 80, to_pin=30, finish=1.8, wet=True)]
    assert H.pick(chip) == []   # in the water, however close: not a good one
    big = [shot("Eve", 7, 1, 90, lie="tee", total=340, to_pin=340, finish=7),
           shot("Eve", 8, 4, 100, putt=True, to_pin=3, outcome="holed")]
    big[0]["lie_after"] = "green"
    assert H.pick(big, most=1)[0]["what"].startswith("Eve bombs a 340 yd drive")
    ace = [shot("Cal", 5, 1, 70, lie="tee", total=340, outcome="holed")]
    assert H.pick(ace, before={"Cal": {"drive": (330, 40)}})[0]["what"] == "Hole in one! Cal on 5, Cal's longest drive yet"
    assert H.pick(ace, before={"Cal": {"drive": (330, 40)}})[0]["post"] == H.POST_BIG


def test_a_scramble_ball_not_played_isnt_the_longest_drive():
    shots = [shot("Ann", 1, 1, 10, lie="tee", total=340, fmt="scramble", picked=0),
             shot("Bob", 1, 1, 20, lie="tee", total=330, fmt="scramble", picked=1),
             shot("Bob", 2, 2, 30, putt=True, to_pin=12, outcome="holed", fmt="scramble")]
    assert [m["shot"]["ts"] for m in H.pick(shots)] == [20, 30]


def test_a_scramble_setup_takes_its_own_teams_score():
    shots = [shot(p, 1, 2, ts, to_pin=150, finish=4, fmt="scramble", picked=1) for p, ts in (("Ann", 10), ("Cal", 20))]
    shots += [shot("Ben", 1, 5, 30, putt=True, to_pin=0.3, outcome="holed", fmt="scramble"),     # Ann's side: bogey
              shot("Dan", 1, 3, 40, putt=True, to_pin=0.3, outcome="holed", fmt="scramble")]     # Cal's: birdie
    said = lambda sides: [m["what"] for m in H.pick(shots, sides=sides)]
    assert said({"Ann": 0, "Ben": 0, "Cal": 1, "Dan": 1}) == ["Ann stuffs it to 12 ft from 150 yd on 1",
                                                              "Cal stuffs it to 12 ft from 150 yd on 1 to set up birdie"]
    assert said({}) == ["Ann stuffs it to 12 ft from 150 yd on 1", "Cal stuffs it to 12 ft from 150 yd on 1"]


def test_a_scramble_doesnt_fill_a_hole_or_count_the_balls_not_played():
    shots = [shot(p, 5, 2, 10 + i, lie="rough", to_pin=30, finish=0.8, outcome="conceded", fmt="scramble",
                  picked=int(p == "Cal")) for i, p in enumerate(("Ann", "Bob", "Cal"))]
    shots += [shot("Ann", 6, 2, 50 + i, lie="rough", to_pin=40, finish=0.7, outcome="conceded", fmt="scramble",
                   picked=1) for i in range(3)]
    got = H.pick(shots)
    assert [m["shot"]["ts"] for m in got] == [12, 50, 51]   # the ball they played on 5; on 6 two, not three


def test_most_moments_and_the_cap():
    shots = [shot("Ann", h, 2, h * 10, putt=True, to_pin=15, outcome="holed") for h in range(1, 13)]
    assert len(H.pick(shots)) == H.MAX_MOMENTS
    old = [{k: v for k, v in x.items() if k not in ("picked", "hazard_x", "tags", "shared_ball", "gimme_yards")}
           for x in shots]   # an older database's shots
    assert len(H.pick(old)) == H.MAX_MOMENTS


def _reel(*ts):
    return {"label": "Pebble · Ann", "date": "2026-11-01", "ended": 1200, "league": False,
            "moments": [{"id": i, "ts": t, "player": "Ann", "hole": i, "what": f"m{i}", "bad": False, "post": 12.0}
                        for i, t in enumerate(ts, 1)]}


def test_a_reel_is_cut_from_the_stream_with_a_break_between_moments():
    s3 = FakeS3(_stream(60))   # 240 s from 1000
    assert H.sync(s3, "b", "", {7: _reel(1020, 1026, 1150)}, clock=lambda: 1300) == {"kept": 1, "added": 1, "removed": 0}
    pl = s3.objects[ROOT + "reels/7/reel.m3u8"].decode()
    # 20 s and 26 s in share one run (17-38 s: segments 4-9); 150 s in, the next (147-162 s: 36-40).
    assert pl.count("#EXT-X-DISCONTINUITY") == 1 and pl.endswith("#EXT-X-ENDLIST\n")
    assert [ln.split("/")[1] for ln in pl.splitlines() if ln.startswith("s1/")] == \
        [f"stream{i}.ts" for i in [*range(4, 10), *range(36, 41)]]
    assert s3.objects[ROOT + "reels/7/s1/stream36.ts"] == b"stream36.ts"
    reel = json.loads(s3.objects[ROOT + "reels.json"])["reels"]["7"]
    assert [m["at"] for m in reel["moments"]] == [3.0, 9.0, 29.0] and reel["duration"] == 44.0
    # Unchanged: nothing to do. Said differently, or made a league round: the same cut, its words redone.
    assert H.sync(s3, "b", "", {7: _reel(1020, 1026, 1150)}, clock=lambda: 1300)["added"] == 0
    renamed = _reel(1020, 1026, 1150)
    renamed["league"], renamed["moments"][0]["what"] = True, "new words"
    assert H.sync(s3, "b", "", {7: renamed}, clock=lambda: 1300)["added"] == 0
    reel = json.loads(s3.objects[ROOT + "reels.json"])["reels"]["7"]
    assert reel["league"] and reel["moments"][0] == {"at": 3.0, "id": 1, "player": "Ann", "hole": 1, "what": "new words", "bad": False}
    longer = _reel(1020, 1026, 1150)
    longer["moments"][2]["post"] = 20.0   # a longer cut: a new playlist
    assert H.sync(s3, "b", "", {7: longer}, clock=lambda: 1300)["added"] == 1
    # Gone off the page (or past its keep): deleted.
    assert H.sync(s3, "b", "", {}, clock=lambda: 1300) == {"kept": 0, "added": 0, "removed": 1}
    assert not [k for k in s3.objects if k.startswith(ROOT + "reels/")]


def test_a_reel_waits_for_its_stream_and_keeps():
    s3 = FakeS3(_stream(15, ended=False))   # 60 s so far: the last moment's not in yet
    assert H.sync(s3, "b", "", {7: _reel(1020, 1055)}, clock=lambda: 1100)["added"] == 0
    s3.objects.update(_stream(30, ended=False))
    assert H.sync(s3, "b", "", {7: _reel(1020, 1055)}, clock=lambda: 1200)["added"] == 1
    week = 8 * 86400
    assert not H.keep(_reel(1), 1200 + week, 7, None)
    assert H.keep({**_reel(1), "league": True}, 1200 + week, 7, "2026-03-01")
    assert not H.keep({**_reel(1), "league": True}, 1200, 7, "2026-12-01")


def test_the_build_finds_each_rounds_reel(tmp_path):
    import sqlite3
    from contextlib import closing

    from test_stats_site import _season
    from commentator import stats_site

    path, reels = _season(tmp_path), {}
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("INSERT INTO rounds (session_id, ended_at, date, course, side, holes, is_league)"
                   " VALUES (1, 30, '2026-10-17', 'Pebble', 'Ann', 9, 1)")
        db.execute("UPDATE shots SET is_putt = 1, lie_before = 'green', to_pin_yards = 10, outcome = 'holed',"
                   " hole = 'Hole ' || id || ', par 4, 400 yards', shot_number = 3 WHERE session_id = 1")
    stats_site.build(path, tmp_path / "site", reels=reels)
    assert list(reels) and all(r["league"] and len(r["moments"]) >= 2 for r in reels.values())
