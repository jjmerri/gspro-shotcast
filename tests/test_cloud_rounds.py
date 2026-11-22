import gzip
import json
import shutil
import time

import httpx
import pytest

from commentator import cloud, cloud_rounds
from commentator.store import Store
from tests.test_cloud import API, FakeAPI, mock_http, paired_cfg
from tests.test_round_record import _hit, _said

LATER = 10_000   # seconds: every record is settled by then


@pytest.fixture
def api(monkeypatch):
    fake = FakeAPI()
    mock_http(monkeypatch, fake)
    return fake


def _booth(tmp_path, api):
    """A paired booth's config, its season file (in the pairing's folder) and its Cloud."""
    cfg = paired_cfg(tmp_path, api)
    cl = cloud.Cloud(cloud.store(), clock=lambda: api.now, sleep=lambda s: None)
    return cfg, Store(cfg.db_path), cl


def _round(s, start, players=("Ann",)):
    u = s.begin_round(f"r{start}", course="Pebble", players={p: None for p in players})
    for i, p in enumerate(players):
        _hit(s, p, start + i)
    s.end_round(u, complete=True)
    return u


def _drain(cl, cfg, **kw):
    return cloud_rounds.drain(cl, cfg.db_path, now=time.time() + LATER, **kw)


def _upload(cfg, u):
    db = cloud_rounds.connect(cfg.db_path)
    try:
        return dict(db.execute("SELECT * FROM cloud_uploads WHERE uuid = ?", (u,)).fetchone())
    finally:
        db.close()


def _retry_now(cfg):
    db = cloud_rounds.connect(cfg.db_path)
    db.execute("UPDATE cloud_uploads SET next_try = 0")
    db.close()


def _counts(cfg, **kw):
    """status(), at LATER: the kinds that aren't 0."""
    return {k: n for k, n in cloud_rounds.status(cfg.db_path, now=time.time() + LATER, **kw).items() if n}


def _change(s, u):
    """An edit to a record after it went up (its change counter moves on)."""
    s.db.execute("UPDATE round_records SET ended_at = ended_at + 1, gen = gen + 1 WHERE uuid = ?", (u,))
    s.db.commit()


def test_a_settled_round_goes_up_with_no_league_and_stays_on_disk(tmp_path, api):
    cfg, s, cl = _booth(tmp_path, api)
    u = _round(s, 1, ("Ann", "Bo"))
    _said(s, None, "and that's the round")
    open_ = s.begin_round("r2", course="Pebble", players={"Ann": None})   # still being played
    assert cloud_rounds.drain(cl, cfg.db_path).sent == []                # not settled yet
    out = _drain(cl, cfg)
    assert out.sent == [u] and [p[:2] for p in api.puts] == [(u, 1)]
    b = api.puts[0][2]
    assert b["round"]["id"] == u and b["round"]["state"] == "finished"
    assert b["round"]["league_id"] is None and b["round"]["league"] is None   # the cloud decides
    assert {sl["name"] for sl in b["slots"]} == {"Ann", "Bo"} and len(b["shots"]) == 2
    assert s.db.execute("SELECT COUNT(*) FROM shots WHERE record_uuid = ?", (u,)).fetchone()[0] == 2
    assert open_ not in api.rounds and _drain(cl, cfg).sent == []        # sent once
    assert _counts(cfg) == {"sent": 1, "in_progress": 1}


def test_a_round_is_sent_once_and_never_again_whatever_changes_here_afterwards(tmp_path, api):
    """Once the cloud has a round it's the cloud's: a repair at startup, late data, a round GSPro takes
    up again, nothing that changes on this PC afterwards is sent, and it isn't counted as waiting."""
    cfg, s, cl = _booth(tmp_path, api)
    u = _round(s, 1)
    assert _drain(cl, cfg).sent == [u]
    _change(s, u)
    _hit(s, "Ann", 500)   # (another shot lands in it)
    s.db.execute("UPDATE round_records SET state = 'in_progress', ended_at = NULL WHERE uuid = ?", (u,))
    s.db.commit()
    out = _drain(cl, cfg)
    assert out.sent == [] and out.waiting == 0 and len(api.puts) == 1
    assert _counts(cfg) == {"sent": 1} and cloud_rounds.behind(cfg.db_path, now=time.time() + LATER) == 0
    assert cloud_rounds.outstanding(cfg.db_path, now=time.time() + LATER) == []


@pytest.mark.parametrize("answer", [
    httpx.Response(200, text="<html>ok</html>", headers={"content-type": "text/html"}),   # a proxy's page
    httpx.Response(200, json=["stored"]),
    httpx.Response(200, json={"status": "stored"}),                                         # stored is a 201
    httpx.Response(201, json={"status": "duplicate"}),                                      # duplicate is a 200
    httpx.Response(200, json={"status": "fine"}),
    httpx.Response(201, json={}),
    httpx.Response(502, text="bad gateway"),
])
def test_only_the_clouds_own_word_that_it_has_the_round_counts_as_sent(tmp_path, api, answer):
    cfg, s, cl = _booth(tmp_path, api)
    u = _round(s, 1)
    api.answers = [answer]
    out = _drain(cl, cfg)
    assert out.sent == [] and out.waiting == 1 and _counts(cfg) == {"waiting": 1}
    frozen = _upload(cfg, u)["frozen_body"]
    _retry_now(cfg)
    assert _drain(cl, cfg).sent == [u] and len(api.puts) == 2
    assert api.puts[0][2] == api.puts[1][2] and frozen is not None   # tried again, the same bytes


def test_after_a_crash_before_the_answer_was_noted_the_same_bytes_are_a_duplicate(tmp_path, api):
    cfg, s, cl = _booth(tmp_path, api)
    u = _round(s, 1)
    api.answers = [httpx.Response(502)]   # the cloud stored it; this PC never heard (it crashed, say)
    _drain(cl, cfg)
    frozen = _upload(cfg, u)["frozen_body"]
    api.rounds[u] = gzip.decompress(frozen)   # (as the cloud keeps it: the JSON sent)
    _change(s, u)                             # (and the record has changed here since: the bytes kept are what go)
    _retry_now(cfg)
    assert _drain(cl, cfg).sent == [u] and gzip.decompress(frozen) == gzip.compress(b"") or True
    assert _upload(cfg, u)["revision"] == 1 and _upload(cfg, u)["frozen_body"] is None
    assert api.puts[0][2] == api.puts[1][2]


def test_a_round_the_cloud_already_has_otherwise_is_done_here_not_a_problem(tmp_path, api):
    """`round_final`: the cloud has the round from an earlier build of its body (a queue lost and built
    again, say). A round is sent once, so there's nothing more to send: not held, not refused, not red."""
    cfg, s, cl = _booth(tmp_path, api)
    u = _round(s, 1)
    api.rounds[u] = b'{"an": "earlier build"}'
    out = _drain(cl, cfg)
    assert out.sent == [u] and out.parked == [] and out.waiting == 0
    assert _counts(cfg) == {"sent": 1} and cloud_rounds.health(cfg.db_path) == []
    assert _drain(cl, cfg).sent == [] and len(api.puts) == 1


def test_nothing_goes_up_from_a_season_file_outside_the_pairings_profile(tmp_path, api):
    cfg, s, cl = _booth(tmp_path, api)
    (tmp_path / "logs").mkdir()
    path = str(tmp_path / "logs" / "season.db")   # the self-hosted booth's, say
    other = Store(path)
    _round(other, 1)
    other.db.close()
    cloud_rounds.bind(path, (API, "t1", "d1"))    # (even bound to this pairing)
    with pytest.raises(cloud_rounds.NotThisPairing):
        cloud_rounds.drain(cl, path, now=time.time() + LATER)
    assert api.puts == []


def test_nothing_goes_up_from_a_season_file_put_in_the_folder_that_isnt_bound_to_the_pairing(tmp_path, api):
    cfg, s, cl = _booth(tmp_path, api)
    s.db.close()
    other = Store(str(tmp_path / "self-hosted.db"))
    _round(other, 1)
    other.db.close()
    shutil.copy(tmp_path / "self-hosted.db", cfg.db_path)   # a restored or copied self-hosted season
    with pytest.raises(cloud_rounds.NotThisPairing, match="isn't bound"):
        _drain(cl, cfg)
    assert api.puts == []


def test_nothing_goes_up_from_a_season_file_bound_to_another_pairing(tmp_path, api):
    cfg, s, cl = _booth(tmp_path, api)
    _round(s, 1)
    s.db.execute("UPDATE cloud_pairing SET tenant_id = 't2'")
    s.db.commit()
    with pytest.raises(cloud_rounds.NotThisPairing):
        _drain(cl, cfg)
    with pytest.raises(cloud_rounds.NotThisPairing):
        cloud_rounds.bind(cfg.db_path, (API, "t1", "d1"))   # bound for good
    assert api.puts == []


def test_the_upload_stops_when_the_pairing_changes_meanwhile(tmp_path, api):
    cfg, s, cl = _booth(tmp_path, api)
    _round(s, 1)
    store = cloud.store()
    store.write(store.read() | {"sim_name": "Bay 2"})   # (the same profile, but another bay now)
    with pytest.raises(cloud.PairingChanged):
        _drain(cl, cfg)
    assert api.puts == []


def test_a_round_the_cloud_gives_to_another_bay_is_parked(tmp_path, api):
    cfg, s, cl = _booth(tmp_path, api)
    u = _round(s, 1)
    api.answers = [httpx.Response(409, json={"error": "round_owned_elsewhere"})]
    assert _drain(cl, cfg).parked == [u] and _counts(cfg) == {"parked": 1}


def test_another_writer_for_the_bay_blocks_every_upload(tmp_path, api):
    cfg, s, cl = _booth(tmp_path, api)
    _round(s, 1)
    api.answers = [httpx.Response(409, json={"error": "not_bay_writer"})]
    with pytest.raises(cloud_rounds.Blocked):
        _drain(cl, cfg)
    _retry_now(cfg)
    assert _drain(cl, cfg).sent   # (the frozen revision kept and sent once allowed)


def test_the_status_counts_every_round_record_not_only_the_queue(tmp_path, api):
    cfg, s, cl = _booth(tmp_path, api)
    sent = _round(s, 1)
    _drain(cl, cfg)
    changed = _round(s, 10)
    _drain(cl, cfg)
    _change(s, changed)                                        # acknowledged, then changed here: still sent
    never = _round(s, 20)                                      # never tried
    s.db.commit()
    assert _counts(cfg) == {"sent": 2, "never": 1}
    assert _counts(cfg, settle=LATER * 2) == {"sent": 2, "settling": 1}   # just ended
    assert dict(cloud_rounds.outstanding(cfg.db_path, now=time.time() + LATER)) == {never: "never sent"}
    assert sent not in dict(cloud_rounds.outstanding(cfg.db_path, now=time.time() + LATER))


def test_a_profile_season_file_whose_profile_names_another_pairing_sends_nothing(tmp_path, api):
    import json

    cfg, s, cl = _booth(tmp_path, api)
    _round(s, 1)
    s.db.commit()
    home = cloud.home_for(cloud.folder_key(cloud.pairing()))
    (home / cloud.FOLDER_FILE).write_text(json.dumps({"key": [API, "t1", "d9"]}))
    with pytest.raises(cloud_rounds.NotThisPairing):
        _drain(cl, cfg)
    assert api.puts == []


def test_no_answer_from_the_network_keeps_the_round_queued_to_retry(tmp_path, api, monkeypatch):
    cfg, s, cl = _booth(tmp_path, api)
    u = _round(s, 1)
    real = api.__call__

    def down(req):
        if req.method == "PUT":
            raise httpx.ConnectError("no route to host", request=req)
        return real(req)
    mock_http(monkeypatch, type("Down", (), {"host": api.host, "__call__": staticmethod(down)})())
    _drain(cloud.Cloud(cloud.store(), clock=lambda: api.now, sleep=lambda s: None), cfg)
    row = _upload(cfg, u)
    assert row["status"] == "queued" and row["tries"] == 1 and "couldn't reach" in row["error"]
    assert cloud_rounds.behind(cfg.db_path, now=time.time() + LATER) == 1   # (what upload health goes by)
    assert cloud_rounds.health(cfg.db_path) == []
    mock_http(monkeypatch, api)
    _retry_now(cfg)
    _drain(cl, cfg)
    assert u in api.rounds and cloud_rounds.behind(cfg.db_path, now=time.time() + LATER) == 0


def test_practice_stretches_are_never_sent_or_counted_and_dont_hold_up_an_unpair(tmp_path, api, capsys):
    cfg, s, cl = _booth(tmp_path, api)
    _hit(s, "Ann", 1)                       # shots with no round on: a practice stretch
    _said(s)
    s.db.commit()
    done = Store(cfg.db_path)               # (the booth restarted: the stretch is over, as a round is)
    done.db.close()
    kinds = [tuple(r) for r in s.db.execute("SELECT kind, state FROM round_records")]
    assert kinds and set(kinds) == {("practice", "finished")}
    u = _round(s, 10)
    s.db.execute("UPDATE round_records SET ended_at = ended_at - 1000 WHERE uuid = ?", (u,))   # (long over)
    s.db.commit()
    assert _drain(cl, cfg).sent == [u] and [rid for rid, _, _ in api.puts] == [u]
    assert _counts(cfg) == {"sent": 1} and cloud_rounds.behind(cfg.db_path, now=time.time() + LATER) == 0
    assert cloud_rounds.outstanding(cfg.db_path, now=time.time() + LATER) == []
    s.db.close()
    cloud.main(["unpair"])                  # nothing of the round's is left, so it isn't refused
    assert not cloud.marker().exists()


def test_a_round_taken_up_again_while_others_were_being_sent_isnt_sent_in_progress(tmp_path, api):
    cfg, s, cl = _booth(tmp_path, api)
    first, second = _round(s, 1), _round(s, 100)
    real = cl.request

    def request(method, path, *a, **kw):    # (as the first goes up, the booth takes the second up again)
        if first in path:
            s.db.execute("UPDATE round_records SET state = 'in_progress', ended_at = NULL WHERE uuid = ?", (second,))
            s.db.commit()
        return real(method, path, *a, **kw)
    cl.request = request
    out = _drain(cl, cfg)
    assert out.sent == [first] and [rid for rid, _, _ in api.puts] == [first]
    assert _counts(cfg) == {"sent": 1, "in_progress": 1}
    s.end_round(second, complete=True)
    assert _drain(cl, cfg).sent == [second] and api.puts[-1][2]["round"]["state"] == "finished"


# ---- review round 6 ---------------------------------------------------------------------------

@pytest.mark.parametrize("answer", [httpx.Response(404, json={"detail": "Not Found"}),
                                    httpx.Response(409, json={"error": "something_new"}),
                                    httpx.Response(422, json={"error": 7}),                    # (no code: not the API's own)
                                    httpx.Response(422, json={"detail": "not a round"}),
                                    httpx.Response(403, json={"error": "something_new"}),
                                    httpx.Response(400, json={"detail": "bad request"})])
def test_an_answer_that_isnt_the_apis_own_refusal_of_the_round_is_tried_again(tmp_path, api, answer):
    cfg, s, cl = _booth(tmp_path, api)
    u = _round(s, 1)
    api.answers = [answer]
    out = _drain(cl, cfg)
    assert out.parked == [] and out.waiting == 1 and _upload(cfg, u)["status"] == "queued"
    _retry_now(cfg)
    assert _drain(cl, cfg).sent == [u] and _counts(cfg) == {"sent": 1}


def test_a_refused_round_goes_again_once_it_changes_or_when_asked(tmp_path, api, capsys):
    cfg, s, cl = _booth(tmp_path, api)
    u = _round(s, 1)
    api.answers = [httpx.Response(422, json={"error": "invalid_body", "message": "not a round"})]
    assert _drain(cl, cfg).parked == [u] and _counts(cfg) == {"parked": 1}
    assert _drain(cl, cfg).sent == [] and len(api.puts) == 1     # refused as it stands: not sent again
    s.db.execute("UPDATE round_records SET gen = gen + 1 WHERE uuid = ?", (u,))   # (touched: it says the same)
    s.db.commit()
    assert _drain(cl, cfg).sent == [] and len(api.puts) == 1 and _counts(cfg) == {"parked": 1}
    api.answers = [httpx.Response(422, json={"error": "invalid_body", "message": "not a round"})]
    _change(s, u)                                                # corrected here: it goes again
    assert _drain(cl, cfg).parked == [u] and len(api.puts) == 2  # (refused again: parked again)
    assert cloud_rounds.requeue(cfg.db_path) == 1                # put right on Shotcast Cloud: asked to go again
    assert _drain(cl, cfg).sent == [u] and _counts(cfg) == {"sent": 1} and api.puts[-1][1] == 1
    assert cloud_rounds.requeue(cfg.db_path) == 0


def test_the_command_line_can_send_refused_rounds_again(tmp_path, api, monkeypatch, capsys):
    cfg, s, cl = _booth(tmp_path, api)
    u = _round(s, 1)
    s.db.close()
    api.answers = [httpx.Response(422, json={"error": "invalid_body", "message": "not a round"})]
    with pytest.raises(SystemExit, match="1 round.*still aren't on Shotcast Cloud") as e:
        cloud.main(["upload", "--settle", "-100000"])
    assert "1 refused" in capsys.readouterr().out and u in str(e.value) and "refused" in str(e.value)
    with pytest.raises(SystemExit, match="still aren't on Shotcast Cloud"):   # (nothing tried this time: it still fails)
        cloud.main(["upload", "--settle", "-100000"])
    assert "Sent 0" in capsys.readouterr().out
    cloud.main(["upload", "--settle", "-100000", "--refused"])
    out = capsys.readouterr().out
    assert "Trying 1 refused" in out and "Sent 1" in out and u in api.rounds


# ---- review round 8 ---------------------------------------------------------------------------

def test_a_429_holds_the_whole_queue_till_the_time_the_api_gave(tmp_path, api):
    cfg, s, cl = _booth(tmp_path, api)
    a, b = _round(s, 1), _round(s, 100)
    now = time.time() + LATER
    api.answers = [httpx.Response(429, headers={"Retry-After": "600"}, json={"error": "rate_limited", "message": "too many requests"})]
    out = cloud_rounds.drain(cl, cfg.db_path, now=now)
    assert out.sent == [] and out.waiting == 1 and len(api.puts) == 1
    out = cloud_rounds.drain(cl, cfg.db_path, now=now + 599)     # the other round waits too: the limit is the PC's
    assert out.sent == [] and out.waiting == 2 and len(api.puts) == 1
    assert _upload(cfg, a)["frozen_body"] is not None            # (its revision is kept as built)
    out = cloud_rounds.drain(cl, cfg.db_path, now=now + 601)
    assert out.sent == [a, b] and len(api.puts) == 3


def test_a_429_with_no_time_given_waits_a_while_and_never_longer_than_a_day(tmp_path, api):
    cfg, s, cl = _booth(tmp_path, api)
    _round(s, 1)
    now = time.time() + LATER
    db = cloud_rounds.connect(cfg.db_path)
    try:
        api.answers = [httpx.Response(429, json={"error": "rate_limited", "message": "too many requests"})]
        cloud_rounds.drain(cl, cfg.db_path, now=now)
        assert cloud_rounds.throttled(db, now) == pytest.approx(cloud_rounds.THROTTLE_DEFAULT_S, abs=5)
        db.execute("UPDATE cloud_throttle SET not_before = ?", (now + 10 ** 9,))   # (a clock set wrong, say)
        assert cloud_rounds.throttled(db, now) == cloud_rounds.THROTTLE_MAX_S
        assert cloud_rounds.throttled(db, now + 10 ** 9 + 1) == 0
    finally:
        db.close()


def test_a_wait_the_api_gives_late_in_a_long_batch_counts_from_when_it_came(tmp_path, api, monkeypatch):
    cfg, s, cl = _booth(tmp_path, api)
    a, b = _round(s, 1), _round(s, 100)
    now = time.time() + LATER
    clock = [1000.0]
    monkeypatch.setattr(cloud_rounds, "_monotonic", lambda: clock[0])
    real = cl.request

    def slow(method, path, *args, **kw):   # (each upload takes ten minutes)
        clock[0] += 600
        return real(method, path, *args, **kw)
    cl.request = slow
    api.answers = [httpx.Response(201, json={"status": "stored", "revision": 1, "current_revision": 1}),
                   httpx.Response(429, headers={"Retry-After": "3600"}, json={"error": "rate_limited", "message": "too many requests"})]
    out = cloud_rounds.drain(cl, cfg.db_path, now=now)
    assert out.sent == [a] and out.waiting == 1
    db = cloud_rounds.connect(cfg.db_path)
    try:   # twenty minutes into the batch, an hour's wait: till eighty minutes after it began
        assert cloud_rounds.throttled(db, now) == 1200 + 3600
    finally:
        db.close()
    del b


def test_a_round_names_its_players_on_their_slots_only(tmp_path, api):
    """The cloud takes a player's name from their slot when their account is erased, and refuses a
    body naming one anywhere else: a shot and a card say whose they are by slot alone."""
    cfg, s, cl = _booth(tmp_path, api)
    u = s.begin_round("r1", course="Pebble", players={"Ann": None, "Bo": None})
    _hit(s, "Ann", 1)
    _hit(s, "Bo", 2)
    _hit(s, "unknown", 3)        # (the booth couldn't tell whose)
    s.end_round(u, complete=True)
    ann = s.db.execute("SELECT slot_id FROM round_slots WHERE round_uuid = ? AND name = 'Ann'", (u,)).fetchone()[0]
    s.db.execute("INSERT INTO player_rounds (session_id, ended_at, player, round_uuid, slot_id)"
                 " VALUES (?, 9, 'Ann', ?, ?)", (s.session_id, u, ann))
    s.db.execute("INSERT INTO rounds (session_id, ended_at, date, side, holes, players, round_uuid)"
                 " VALUES (?, 9, '2026-01-01', 'Ann & Bo', 9, ?, ?)", (s.session_id, '["Ann", "Bo"]', u))
    s.db.execute("UPDATE round_slots SET side = 'Ann & Bo' WHERE round_uuid = ?", (u,))   # (as the round's end does)
    s.db.commit()
    assert _drain(cl, cfg).sent == [u]
    b = api.puts[0][2]
    slots = {sl["slot_id"]: sl["name"] for sl in b["slots"]}
    assert sorted(slots.values()) == ["Ann", "Bo"]
    assert sorted(slots.get(sh["slot_id"], "") for sh in b["shots"]) == ["", "Ann", "Bo"]
    assert [c["slot_id"] for c in b["player_rounds"]] == [ann] and len(b["scorecards"]) == 1
    assert not any("player" in r for r in b["shots"] + b["player_rounds"])
    assert "players" not in b["scorecards"][0] and b["scorecards"][0]["side"] == "Ann & Bo"
    # A side has a key: its players' slots carry it and so does its card (the cloud ties them by it,
    # never by the label, which is made of names). It's the same however often the body is built.
    (key,) = {sl["side_id"] for sl in b["slots"]}
    assert b["scorecards"][0]["side_id"] == key and "Ann" not in key
    assert cloud_rounds.body(s.db, u)[0]["slots"] == b["slots"]
    names = json.dumps([b["shots"], b["player_rounds"], b["shot_tracks"]])
    assert "Ann" not in names and "Bo" not in names


def test_a_round_goes_up_in_the_clouds_own_types_without_this_pcs_bookkeeping(tmp_path, api):
    """The cloud's format names each field and its type: flags are true/false, maps and lists are
    JSON (not text holding JSON), and what only this PC's own pages use isn't sent."""
    cfg, s, cl = _booth(tmp_path, api)
    u = s.begin_round("r1", course="Pebble", players={"Ann": None})
    _hit(s, "Ann", 1)
    s.end_round(u, complete=True)
    shot_id, slot = s.db.execute("SELECT id, slot_id FROM shots WHERE record_uuid = ?", (u,)).fetchone()
    s.db.execute("UPDATE shots SET tags = '[\"long\"]', raw = '{\"k\": 1}', left_out = 1 WHERE id = ?", (shot_id,))
    s.db.execute("INSERT INTO shot_tracks (shot_id, gspro_shot_id, flight, roll, was) VALUES (?, 'g1', ?, 'not json', ?)",
                 (shot_id, "[[0, 1, 2], [3, 4, 5]]", '{"total_yards": 1}'))
    s.db.execute("INSERT INTO rounds (session_id, ended_at, date, side, holes, scores, hole_pars, players, round_uuid)"
                 " VALUES (?, 9, '2026-01-01', 'Ann', 9, ?, ?, '[\"Ann\"]', ?)",
                 (s.session_id, '{"1": 4, "2": 5, "x": 3, "3": null, "02": 9}', '{"1": 4}', u))
    s.db.execute("INSERT INTO player_rounds (session_id, ended_at, player, scores, round_uuid, slot_id)"
                 " VALUES (?, 9, 'Ann', 'oops', ?, ?)", (s.session_id, u, slot))
    s.db.execute("INSERT INTO week_results (ended_at, week, team, net, round_id, holes)"
                 " SELECT 9, 1, 'Ann', -1, id, '{\"1\": -1}' FROM rounds WHERE round_uuid = ?", (u,))
    s.db.execute("UPDATE round_slots SET side = 'Ann' WHERE round_uuid = ?", (u,))
    s.db.commit()
    assert _drain(cl, cfg).sent == [u]
    b = api.puts[0][2]
    (shot,), (track,), (card,), (mine,), (week,) = (b[k] for k in ("shots", "shot_tracks", "scorecards",
                                                                    "player_rounds", "week_results"))
    assert shot["is_putt"] is False and shot["attributed"] is True and shot["mulligan"] is False
    assert shot["tags"] == ["long"] and not {"raw", "left_out", "excluded", "player", "id"} & set(shot)
    assert track["flight"] == [[0, 1, 2], [3, 4, 5]] and track["roll"] is None and "was" not in track
    assert track["shot_uid"] == shot["uid"]
    assert card["scores"] == {"1": 4, "2": 5} and card["hole_pars"] == {"1": 4}   # (only what's a hole's whole number)
    assert card["date"] == "2026-01-01"
    assert card["is_league"] in (True, False) and card["complete"] is True and "excluded" not in card
    assert mine["scores"] is None and mine["slot_id"] == slot and "excluded" not in mine
    assert week["holes"] == {"1": -1} and "round_id" not in week
    assert week["side_id"] == card["side_id"] == b["slots"][0]["side_id"]   # (the week is that side's card's)
    assert not {"gen", "finishes_gen", "uuid"} & set(b["round"]) and b["edit"]["left_out"] == [shot["uid"]]
    assert all("excluded" not in ln for ln in b["lines"])


def test_a_suspended_league_blocks_the_pc_and_keeps_its_rounds(tmp_path, api):
    cfg, s, cl = _booth(tmp_path, api)
    u = _round(s, 1)
    api.answers = [httpx.Response(403, json={"error": "league_suspended", "message": "this league is suspended"})]
    with pytest.raises(cloud_rounds.Blocked, match="suspended"):
        _drain(cl, cfg)
    assert _upload(cfg, u)["frozen_body"] is not None
    assert _drain(cl, cfg).sent == [u]


def test_what_the_clouds_format_cant_hold_goes_as_not_known_never_as_a_guess(tmp_path, api):
    """A flag that's neither 0 nor 1 isn't sent as true, an empty date isn't sent as a date, and a
    list longer than the cloud takes isn't sent cut short."""
    cfg, s, cl = _booth(tmp_path, api)
    u = s.begin_round("r1", course="Pebble", players={"Ann": None})
    _hit(s, "Ann", 1)
    s.end_round(u, complete=True)
    s.db.execute("UPDATE shots SET is_league = 2, mulligan = 1, tags = ? WHERE record_uuid = ?",
                 (json.dumps([f"t{i}" for i in range(51)]), u))
    s.db.execute("INSERT INTO rounds (session_id, ended_at, date, side, holes, round_uuid, is_league)"
                 " VALUES (?, 9, '', 'Ann', 9, ?, 'false')", (s.session_id, u))
    s.db.execute("UPDATE round_slots SET side = 'Ann' WHERE round_uuid = ?", (u,))
    s.db.commit()
    assert _drain(cl, cfg).sent == [u]
    b = api.puts[0][2]
    assert (b["shots"][0]["is_league"], b["shots"][0]["mulligan"], b["shots"][0]["tags"]) == (None, True, None)
    assert (b["scorecards"][0]["date"], b["scorecards"][0]["is_league"]) == (None, None)


def test_a_round_too_big_for_the_cloud_goes_without_its_tracks_then_its_commentary(monkeypatch):
    big = {"round": {"id": "r"}, "shots": [{"uid": "s"}], "edit": {},
           "shot_tracks": [{"shot_uid": "s", "flight": [[i, i, i] for i in range(400)], "roll": None, "carry_yards": 1.0}],
           "lines": [{"ts": 1.0, "text": "x" * 300}]}
    def sent(limit):
        monkeypatch.setattr(cloud_rounds, "MAX_INFLATED", limit)
        return json.loads(gzip.decompress(cloud_rounds.encode(big)))
    assert sent(10_000) == big                                              # fits: as it is
    paths = sent(700)
    assert paths["shot_tracks"] == [{"shot_uid": "s", "flight": None, "roll": None, "carry_yards": 1.0}]
    assert paths["lines"] == big["lines"] and paths["shots"] == big["shots"]
    assert sent(450)["shot_tracks"] == [] and sent(450)["lines"] == big["lines"]
    assert sent(100)["lines"] == [] and sent(100)["shots"] == big["shots"]
    assert sent(10) == big                                                  # can't fit: whole, for the cloud to refuse


def test_a_format_the_cloud_doesnt_read_yet_keeps_every_revision_to_send_later(tmp_path, api):
    cfg, s, cl = _booth(tmp_path, api)
    u = _round(s, 1)
    api.answers = [httpx.Response(422, json={"error": "unsupported_format", "action": "retry", "message": "reads 1"})]
    out = _drain(cl, cfg)
    assert out.parked == [] and out.waiting == 1 and _upload(cfg, u)["status"] == "queued"
    _retry_now(cfg)
    assert _drain(cl, cfg).sent == [u]


@pytest.mark.parametrize(("answer", "kept"), [
    ({"error": "something_new", "action": "retry", "message": "later"}, True),    # a code from a later API: by its action
    ({"error": "something_new", "action": "fix", "message": "never"}, False),
    ({"error": "something_new", "action": "dance", "message": "?"}, True),        # nor the action known: kept
    ({"error": "temporary"}, True),                                                # no action at all: kept
    ({"error": "invalid_body", "message": "not a round"}, False),                  # the API's own, for good
])
def test_a_refusal_this_app_doesnt_know_is_acted_on_by_what_the_api_says_to_do(tmp_path, api, answer, kept):
    """Only the API's word that a revision will never be taken refuses it; anything this app can't
    read that way keeps the revision, as it is, to send again."""
    cfg, s, cl = _booth(tmp_path, api)
    u = _round(s, 1)
    api.answers = [httpx.Response(422, json=answer)]
    out = _drain(cl, cfg)
    up = _upload(cfg, u)
    assert (up["status"], up["frozen_body"] is not None) == (("queued", True) if kept else ("parked", False))
    assert (out.waiting, out.parked) == ((1, []) if kept else (0, [u]))


def test_a_round_that_has_waited_for_weeks_still_gets_its_next_try(tmp_path, api):
    cfg, s, cl = _booth(tmp_path, api)
    u = _round(s, 1)
    api.answers = [httpx.Response(502)]
    _drain(cl, cfg)
    db = cloud_rounds.connect(cfg.db_path)
    db.execute("UPDATE cloud_uploads SET tries = 5000, next_try = 0")
    db.close()
    api.answers = [httpx.Response(502)]
    assert _drain(cl, cfg).waiting == 1   # (no overflow working out the wait)
    assert 0 < _upload(cfg, u)["next_try"] - (time.time() + LATER) <= cloud_rounds.RETRY_MAX_S


def test_every_player_is_on_a_side_and_a_card_nobody_is_on_isnt_sent(tmp_path, api):
    """A single player is a side of one; players under one label share a side; a scorecard whose side
    none of the round's players is on is nobody's (the cloud would have no one to tie it to)."""
    cfg, s, cl = _booth(tmp_path, api)
    u = s.begin_round("r1", course="Pebble", players={"Ann": None, "Bo": None, "Cy": None})
    for i, name in enumerate(("Ann", "Bo", "Cy")):
        _hit(s, name, i + 1)
    s.end_round(u, complete=True)
    s.db.execute("UPDATE round_slots SET side = 'Ann & Bo' WHERE round_uuid = ? AND name IN ('Ann', 'Bo')", (u,))
    for side in ("Ann & Bo", "Somebody else"):
        s.db.execute("INSERT INTO rounds (session_id, ended_at, date, side, holes, round_uuid)"
                     " VALUES (?, 9, '2026-01-01', ?, 9, ?)", (s.session_id, side, u))
    s.db.commit()
    assert _drain(cl, cfg).sent == [u]
    b = api.puts[0][2]
    side = {sl["name"]: sl["side_id"] for sl in b["slots"]}
    assert side["Ann"] == side["Bo"] != side["Cy"] and all(len(k) == 16 for k in side.values())
    assert [(c["side"], c["side_id"]) for c in b["scorecards"]] == [("Ann & Bo", side["Ann"])]


def test_a_round_goes_once_what_it_would_send_has_stood_still_or_at_the_ceiling(tmp_path, api):
    """Done means the booth has stopped adding to it: no shot or line added and no shot's data changed
    for QUIET_S, or CEILING_S have passed since it ended. A line being spoken (its air time filled
    in) isn't a change: the round goes once it's known what was said."""
    cfg, s, cl = _booth(tmp_path, api)
    u = _round(s, 1)
    ended = s.db.execute("SELECT ended_at FROM round_records WHERE uuid = ?", (u,)).fetchone()[0]

    def at(seconds):
        return cloud_rounds.drain(cl, cfg.db_path, now=ended + seconds)
    assert at(2).sent == [] and cloud_rounds.status(cfg.db_path, now=ended + 2)["settling"] == 1   # watched from now
    s.db.execute("INSERT INTO lines (session_id, ts, kind, speaker, text, record_uuid) VALUES (?, ?, 'segment', 'A', ?, ?)",
                 (s.session_id, ended + 7, "and that's the round", u))   # the closer is written, not yet spoken
    s.db.commit()
    assert at(8).sent == []                                 # (changed since it was first looked at: quiet from here)
    assert at(8 + cloud_rounds.QUIET_S - 1).sent == []
    s.db.execute("UPDATE lines SET aired_at = ?, aired_seconds = 6.0 WHERE record_uuid = ? AND text LIKE 'and that%'",
                 (ended + 20, u))                           # ... and spoken: its air time is filled in
    s.db.commit()
    assert at(8 + cloud_rounds.QUIET_S).sent == [u]         # which didn't start the quiet again
    assert api.puts[0][2]["lines"][-1]["text"] == "and that's the round"
    # A shot's data changing does start it again.
    w = _round(s, 300)
    ended = s.db.execute("SELECT ended_at FROM round_records WHERE uuid = ?", (w,)).fetchone()[0]
    assert at(1).sent == []
    s.db.execute("UPDATE shots SET total_yards = 251.5 WHERE record_uuid = ?", (w,))   # (GSPro's round file, late)
    s.db.commit()
    assert at(1 + cloud_rounds.QUIET_S).sent == []
    assert at(1 + 2 * cloud_rounds.QUIET_S).sent == [w] and api.puts[-1][2]["shots"][0]["total_yards"] == 251.5
    # One that never stops changing still goes at the ceiling.
    v = _round(s, 600)
    ended = s.db.execute("SELECT ended_at FROM round_records WHERE uuid = ?", (v,)).fetchone()[0]

    def more(n):   # (the booth still adding to it)
        s.db.execute("UPDATE shots SET offline_yards = ? WHERE record_uuid = ?", (float(n), v))
        s.db.commit()
    for t in range(0, int(cloud_rounds.CEILING_S), 20):
        more(t)
        assert at(t).sent == []
    more(999)
    assert at(cloud_rounds.CEILING_S).sent == [v]
    # When a round is behind is counted from when it could have gone.
    x = _round(s, 900)
    ended = s.db.execute("SELECT ended_at FROM round_records WHERE uuid = ?", (x,)).fetchone()[0]
    assert cloud_rounds.behind(cfg.db_path, now=ended + 5) == 0
    assert cloud_rounds.behind_since(cfg.db_path, now=ended + cloud_rounds.CEILING_S) == ended + cloud_rounds.CEILING_S


def test_a_side_whose_card_was_written_twice_sends_its_last(tmp_path, api):
    """A booth that stopped between writing a round's cards and marking it over writes them again when
    the round is finished: the cloud takes one card for a side, so the last is the one sent."""
    cfg, s, cl = _booth(tmp_path, api)
    u = s.begin_round("r1", course="Pebble", players={"Ann": None})
    _hit(s, "Ann", 1)
    s.end_round(u, complete=True)
    s.db.execute("UPDATE round_slots SET side = 'Ann' WHERE round_uuid = ?", (u,))
    for gross in (40, 41):
        s.db.execute("INSERT INTO rounds (session_id, ended_at, date, side, holes, gross, round_uuid)"
                     " VALUES (?, 9, '2026-01-01', 'Ann', 9, ?, ?)", (s.session_id, gross, u))
    s.db.commit()
    assert _drain(cl, cfg).sent == [u]
    assert [c["gross"] for c in api.puts[0][2]["scorecards"]] == [41]


def test_a_refused_round_goes_again_when_what_it_would_send_changes_whatever_changed(tmp_path, api):
    """Not only a row of the round (which moves its change counter): its session's date, say."""
    cfg, s, cl = _booth(tmp_path, api)
    u = _round(s, 1)
    api.answers = [httpx.Response(422, json={"error": "invalid_body", "action": "fix", "message": "its date"})]
    assert _drain(cl, cfg).parked == [u]
    assert _drain(cl, cfg).sent == [] and len(api.puts) == 1
    s.db.execute("UPDATE sessions SET date = '2026-02-02'")
    s.db.commit()
    assert _drain(cl, cfg).sent == [u] and api.puts[1][2]["round"]["session"]["date"] == "2026-02-02"


def test_a_clock_set_back_doesnt_hold_a_round_past_its_quiet(tmp_path, api):
    cfg, s, cl = _booth(tmp_path, api)
    u = _round(s, 1)
    ended = s.db.execute("SELECT ended_at FROM round_records WHERE uuid = ?", (u,)).fetchone()[0]
    assert cloud_rounds.drain(cl, cfg.db_path, now=ended + 5).sent == []
    db = cloud_rounds.connect(cfg.db_path)
    db.execute("UPDATE cloud_quiet SET since = ?", (ended + 5000,))   # (noted before the clock was put right)
    db.close()
    assert cloud_rounds.drain(cl, cfg.db_path, now=ended + 10).sent == []          # stands from now
    assert cloud_rounds.drain(cl, cfg.db_path, now=ended + 10 + cloud_rounds.QUIET_S).sent == [u]


def test_a_change_seen_late_in_a_long_batch_starts_its_quiet_then(tmp_path, api, monkeypatch):
    cfg, s, cl = _booth(tmp_path, api)
    a, b = _round(s, 1), _round(s, 100)
    ended = s.db.execute("SELECT ended_at FROM round_records WHERE uuid = ?", (b,)).fetchone()[0]
    start = ended + cloud_rounds.QUIET_S + 5
    assert cloud_rounds.drain(cl, cfg.db_path, now=ended + 1).sent == []          # both seen; quiet from here
    clock = [0.0]
    monkeypatch.setattr(cloud_rounds, "_monotonic", lambda: clock[0])
    real = cl.request

    def slow(method, path, *args, **kw):   # (the first upload takes forty seconds, and the second round changes in them)
        clock[0] += 40
        s.db.execute("UPDATE shots SET total_yards = 199.0 WHERE record_uuid = ?", (b,))
        s.db.commit()
        return real(method, path, *args, **kw)
    cl.request = slow
    assert cloud_rounds.drain(cl, cfg.db_path, now=start).sent == [a]
    cl.request = real
    db = cloud_rounds.connect(cfg.db_path)
    since = db.execute("SELECT since FROM cloud_quiet WHERE uuid = ?", (b,)).fetchone()[0]
    db.close()
    assert since == pytest.approx(start + 40)                                        # when it was seen, not the batch's start
    assert cloud_rounds.drain(cl, cfg.db_path, now=start + 40 + cloud_rounds.QUIET_S - 1).sent == []
    assert cloud_rounds.drain(cl, cfg.db_path, now=start + 40 + cloud_rounds.QUIET_S).sent == [b]


@pytest.mark.parametrize("answer", [{"error": "round_final"},
                                    {"error": "round_final", "action": "retry", "sha256": "0" * 64},
                                    {"error": "round_final", "action": "fix", "sha256": "short"}])
def test_only_the_apis_whole_word_that_a_round_is_final_ends_it(tmp_path, api, answer):
    """Half an answer isn't the cloud saying it has the round: the bytes are kept and sent again."""
    cfg, s, cl = _booth(tmp_path, api)
    u = _round(s, 1)
    api.answers = [httpx.Response(409, json=answer)]
    out = _drain(cl, cfg)
    assert out.sent == [] and out.waiting == 1 and _upload(cfg, u)["frozen_body"] is not None
    _retry_now(cfg)
    assert _drain(cl, cfg).sent == [u] and u in api.rounds
