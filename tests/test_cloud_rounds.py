import shutil
import time

import httpx
import pytest

from commentator import cloud, cloud_rounds
from commentator.store import Store
from tests.test_cloud import API, FakeAPI, mock_http, paired_cfg
from tests.test_round_record import _hit, _said



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
    """One look by a run that waits for nothing (no ceiling): every finished round goes."""
    return cloud_rounds.drain(cl, cfg.db_path, **({"settle": 0} | kw))


def _upload(cfg, u):
    db = cloud_rounds.connect(cfg.db_path)
    try:
        return dict(db.execute("SELECT * FROM cloud_uploads WHERE uuid = ?", (u,)).fetchone())
    finally:
        db.close()


def _run(monkeypatch, at=1000.0):
    """One run of the uploader, and its clock (elapsed seconds), moved by hand."""
    clock = [at]
    monkeypatch.setattr(cloud_rounds, "_monotonic", lambda: clock[0])
    return cloud_rounds.Pace(), clock


def _counts(cfg, **kw):
    """status(): the kinds that aren't 0."""
    return {k: n for k, n in cloud_rounds.status(cfg.db_path, **kw).items() if n}


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
    assert _counts(cfg) == {"sent": 1} and cloud_rounds.behind(cfg.db_path) == 0
    assert cloud_rounds.outstanding(cfg.db_path) == []


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
    u, after = _round(s, 1), _round(s, 100)
    api.answers = [answer]
    out = _drain(cl, cfg)
    # Not an answer the API wrote: nothing more is tried this time (the next round would get the same).
    assert out.sent == [] and out.waiting == 1 and len(api.puts) == 1 and _counts(cfg) == {"never": 2}
    assert _upload(cfg, u)["tries"] == 1 and _upload(cfg, u)["error"]
    assert _drain(cl, cfg).sent == [u, after]


def test_after_a_crash_before_the_answer_was_noted_the_round_is_sent_again_and_the_cloud_says_it_has_it(tmp_path, api):
    """The cloud stored it and this PC never heard. Sent again as it is, it's a `duplicate`; changed
    here meanwhile, it's `round_final` (the cloud keeps what it first got): done either way."""
    cfg, s, cl = _booth(tmp_path, api)
    same, changed = _round(s, 1), _round(s, 100)
    api.answers = [httpx.Response(502)]
    _drain(cl, cfg)
    api.answers = [httpx.Response(200, json={"status": "duplicate"}),
                   httpx.Response(409, json={"error": "round_final", "action": "fix", "sha256": "0" * 64})]
    assert _drain(cl, cfg).sent == [same, changed]
    assert _upload(cfg, same)["revision"] == _upload(cfg, changed)["revision"] == 1
    assert _drain(cl, cfg).sent == [] and len(api.puts) == 3 and cloud_rounds.behind(cfg.db_path) == 0


def test_a_round_the_cloud_already_has_otherwise_is_done_here_not_a_problem(tmp_path, api):
    """`round_final`: the cloud has the round from an earlier build of its body (a queue lost and built
    again, say). A round is sent once, so there's nothing more to send: not waiting, not red."""
    cfg, s, cl = _booth(tmp_path, api)
    u = _round(s, 1)
    api.rounds[u] = b'{"an": "earlier build"}'
    out = _drain(cl, cfg)
    assert out.sent == [u] and out.waiting == 0
    assert _counts(cfg) == {"sent": 1} and cloud_rounds.behind(cfg.db_path) == 0
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
        cloud_rounds.drain(cl, path, settle=0)
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


def test_a_round_the_cloud_refuses_is_tried_again_later_and_doesnt_hold_up_the_next(tmp_path, api, monkeypatch):
    """The API's own refusal of one round (another bay's, too big, not a body it reads, a code this
    app doesn't know): its error is kept and shown, it waits a day for each next try (every try is
    charged to this PC's daily allowance), and the rounds after it go meanwhile."""
    cfg, s, cl = _booth(tmp_path, api)
    refused, good = _round(s, 1), _round(s, 100)
    pace, clock = _run(monkeypatch)

    def look():
        return cloud_rounds.drain(cl, cfg.db_path, pace, settle=0)
    api.answers = [httpx.Response(409, json={"error": "round_owned_elsewhere", "message": "another bay's"})]
    out = look()
    assert out.sent == [good] and out.waiting == 1 and _counts(cfg) == {"sent": 1, "never": 1}
    [(uuid, what)] = cloud_rounds.outstanding(cfg.db_path)
    assert uuid == refused and what.startswith("not sent yet (") and "409" in what and "another bay's" in what
    assert cloud_rounds.behind(cfg.db_path) == 1
    assert look().sent == [] and len(api.puts) == 2              # (not before its wait is over)
    clock[0] += cloud_rounds.RETRY_MAX_S
    assert look().sent == [] and len(api.puts) == 2              # (nor after the longest wait of a network failure)
    clock[0] += cloud_rounds.REFUSED_S
    api.answers = [httpx.Response(422, json={"error": "something_new", "message": "no"})]
    assert look().waiting == 1 and _upload(cfg, refused)["tries"] == 2
    clock[0] += cloud_rounds.REFUSED_S                           # (put right on Shotcast Cloud meanwhile)
    assert look().sent == [refused] and cloud_rounds.outstanding(cfg.db_path) == []


@pytest.mark.parametrize("answer", [
    httpx.Response(422, json={"error": "unsupported_format", "action": "retry", "message": "not yet"}),
    httpx.Response(503, json={"error": "busy"}),
    httpx.Response(401, json={"error": "access_token_invalid"}),
    httpx.Response(404, json={"error": "not_found"}),
    httpx.Response(422, json={"message": "no code"}),
])
def test_an_answer_that_isnt_a_no_to_this_one_round_stops_the_drain_before_the_next(tmp_path, api, answer):
    """The API behind this app, busy, or answering something that isn't about the round: the next
    round would get the same, so it isn't sent (each try is charged)."""
    cfg, s, cl = _booth(tmp_path, api)
    first, _ = _round(s, 1), _round(s, 100)
    api.answers = [answer] * 3   # (a 401 is tried once more with a fresh token)
    out = _drain(cl, cfg)
    assert out.sent == [] and out.waiting == 1 and {p[0] for p in api.puts} == {first}


def test_another_writer_for_the_bay_blocks_every_upload(tmp_path, api):
    cfg, s, cl = _booth(tmp_path, api)
    _round(s, 1)
    api.answers = [httpx.Response(409, json={"error": "not_bay_writer"})]
    with pytest.raises(cloud_rounds.Blocked):
        _drain(cl, cfg)
    assert _drain(cl, cfg).sent   # (sent once allowed)


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
    assert dict(cloud_rounds.outstanding(cfg.db_path)) == {never: "not sent yet"}
    assert sent not in dict(cloud_rounds.outstanding(cfg.db_path))


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
    assert row["revision"] == 0 and row["tries"] == 1 and "couldn't reach" in row["error"]
    assert cloud_rounds.behind(cfg.db_path) == 1   # (what upload health goes by)
    mock_http(monkeypatch, api)
    _drain(cl, cfg)
    assert u in api.rounds and cloud_rounds.behind(cfg.db_path) == 0


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
    assert _counts(cfg) == {"sent": 1} and cloud_rounds.behind(cfg.db_path) == 0
    assert cloud_rounds.outstanding(cfg.db_path) == []
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
                                    httpx.Response(422, json={"error": 7}),                    # (no code: not the API's own)
                                    httpx.Response(422, json={"detail": "not a round"}),
                                    httpx.Response(400, json={"detail": "bad request"}),
                                    httpx.Response(503, json={"error": "busy"})])
def test_an_answer_that_isnt_the_apis_own_refusal_of_the_round_is_tried_again(tmp_path, api, answer):
    cfg, s, cl = _booth(tmp_path, api)
    u = _round(s, 1)
    api.answers = [answer]
    out = _drain(cl, cfg)
    assert out.sent == [] and out.waiting == 1 and _upload(cfg, u)["tries"] == 1
    assert _drain(cl, cfg).sent == [u] and _counts(cfg) == {"sent": 1}


def test_the_upload_command_fails_while_a_round_is_refused_and_says_why(tmp_path, api, monkeypatch, capsys):
    cfg, s, cl = _booth(tmp_path, api)
    u = _round(s, 1)
    monkeypatch.setattr(cloud_rounds, "CEILING_S", 0.0)   # (no waiting out the quiet here)
    s.db.close()
    api.answers = [httpx.Response(422, json={"error": "invalid_body", "message": "not a round"})]
    with pytest.raises(SystemExit, match="1 round.*still aren't on Shotcast Cloud") as e:
        cloud.main(["upload"])
    assert "Sent 0 round(s); 1 to retry" in capsys.readouterr().out and u in str(e.value) and "not a round" in str(e.value)
    cloud.main(["upload"])                                # (put right on Shotcast Cloud: another run sends it)
    assert "Sent 1" in capsys.readouterr().out and u in api.rounds


# ---- review round 8 ---------------------------------------------------------------------------

def test_a_429_holds_the_whole_queue_for_as_long_as_the_api_said(tmp_path, api, monkeypatch):
    cfg, s, cl = _booth(tmp_path, api)
    a, b = _round(s, 1), _round(s, 100)
    pace, clock = _run(monkeypatch)

    def look():
        return cloud_rounds.drain(cl, cfg.db_path, pace, settle=0)
    api.answers = [httpx.Response(429, headers={"Retry-After": "600"}, json={"error": "rate_limited", "message": "too many requests"})]
    out = look()
    assert out.sent == [] and out.waiting == 1 and len(api.puts) == 1
    clock[0] += 599
    out = look()                                                 # the other round waits too: the limit is the PC's
    assert out.sent == [] and out.waiting == 2 and len(api.puts) == 1
    clock[0] += 2
    out = look()
    assert out.sent == [a, b] and len(api.puts) == 3


@pytest.mark.parametrize("header, wait", [({}, cloud_rounds.THROTTLE_DEFAULT_S),
                                          ({"Retry-After": "soon"}, cloud_rounds.THROTTLE_DEFAULT_S),
                                          ({"Retry-After": "nan"}, cloud_rounds.THROTTLE_DEFAULT_S),
                                          ({"Retry-After": "1e12"}, cloud_rounds.THROTTLE_MAX_S),
                                          ({"Retry-After": "-5"}, 1.0)])
def test_a_429_with_no_time_given_waits_a_while_and_never_longer_than_a_day(tmp_path, api, monkeypatch, header, wait):
    cfg, s, cl = _booth(tmp_path, api)
    _round(s, 1)
    pace, clock = _run(monkeypatch)
    api.answers = [httpx.Response(429, headers=header, json={"error": "rate_limited", "message": "too many requests"})]
    cloud_rounds.drain(cl, cfg.db_path, pace, settle=0)
    assert pace.held() == wait
    clock[0] += wait
    assert pace.held() == 0


def test_a_wait_the_api_gives_late_in_a_long_batch_counts_from_when_it_came(tmp_path, api, monkeypatch):
    cfg, s, cl = _booth(tmp_path, api)
    a, b = _round(s, 1), _round(s, 100)
    pace, clock = _run(monkeypatch)
    real = cl.request

    def slow(method, path, *args, **kw):   # (each upload takes ten minutes)
        clock[0] += 600
        return real(method, path, *args, **kw)
    cl.request = slow
    api.answers = [httpx.Response(201, json={"status": "stored", "revision": 1, "current_revision": 1}),
                   httpx.Response(429, headers={"Retry-After": "3600"}, json={"error": "rate_limited", "message": "too many requests"})]
    out = cloud_rounds.drain(cl, cfg.db_path, pace, settle=0)
    assert out.sent == [a] and out.waiting == 1
    assert pace.held() == 3600   # (an hour from the answer, twenty minutes into the batch)
    del b


def test_the_time_of_day_has_no_part_in_when_a_round_goes(tmp_path, api, monkeypatch):
    """The PC's clock put right, or set wrong, by years either way: a round's quiet, a retry and a
    wait the API gave are all counted in elapsed seconds, so none of them moves."""
    import types

    cfg, s, cl = _booth(tmp_path, api)
    u = _round(s, 1)
    pace, clock = _run(monkeypatch)
    day = [time.time()]
    monkeypatch.setattr(cloud_rounds, "time", types.SimpleNamespace(time=lambda: day[0]))

    def look():
        return cloud_rounds.drain(cl, cfg.db_path, pace)
    assert look().settling == 1
    day[0] += 10 ** 9                                   # years ahead: not done any sooner
    clock[0] += cloud_rounds.QUIET_S - 1
    assert look().settling == 1
    day[0] -= 2 * 10 ** 9                               # and years back: not held any longer
    clock[0] += 1
    api.answers = [httpx.Response(502)]
    assert look().waiting == 1 and len(api.puts) == 1
    day[0] += 10 ** 9
    clock[0] += cloud_rounds.RETRY_BASE_S - 1
    assert look().sent == [] and len(api.puts) == 1     # (its retry isn't due yet)
    day[0] -= 10 ** 9
    clock[0] += 1
    assert look().sent == [u]


def test_another_run_counts_afresh(tmp_path, api, monkeypatch):
    """Nothing about the waits is kept on disk: a booth that restarts watches a finished round for
    its quiet again, and holds no retry over."""
    cfg, s, cl = _booth(tmp_path, api)
    u = _round(s, 1)
    pace, clock = _run(monkeypatch)
    assert cloud_rounds.drain(cl, cfg.db_path, pace).settling == 1
    clock[0] += cloud_rounds.QUIET_S - 1
    again = cloud_rounds.Pace()                         # (the booth restarted)
    clock[0] += 1
    assert cloud_rounds.drain(cl, cfg.db_path, again).settling == 1
    clock[0] += cloud_rounds.QUIET_S
    api.answers = [httpx.Response(502)]
    assert cloud_rounds.drain(cl, cfg.db_path, again).waiting == 1
    assert cloud_rounds.drain(cl, cfg.db_path, again).sent == []          # (waiting for its retry)
    restarted = cloud_rounds.Pace()                     # (restarted again: no retry held over, its quiet anew)
    assert cloud_rounds.drain(cl, cfg.db_path, restarted).settling == 1
    clock[0] += cloud_rounds.QUIET_S
    assert cloud_rounds.drain(cl, cfg.db_path, restarted).sent == [u]


def test_a_round_that_has_waited_for_weeks_still_gets_its_next_try(tmp_path, api, monkeypatch):
    cfg, s, cl = _booth(tmp_path, api)
    u = _round(s, 1)
    pace, clock = _run(monkeypatch)
    api.answers = [httpx.Response(502)]
    cloud_rounds.drain(cl, cfg.db_path, pace, settle=0)
    db = cloud_rounds.connect(cfg.db_path)
    db.execute("UPDATE cloud_uploads SET tries = 5000")
    db.close()
    clock[0] += cloud_rounds.RETRY_BASE_S
    api.answers = [httpx.Response(502)]
    assert cloud_rounds.drain(cl, cfg.db_path, pace, settle=0).waiting == 1   # (no overflow working out the wait)
    assert pace.retry[u] - clock[0] == cloud_rounds.RETRY_MAX_S


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


def test_a_round_goes_once_what_it_would_send_has_stood_still_or_at_the_ceiling(tmp_path, api, monkeypatch):
    """Done means the booth has stopped adding to it: no shot or line added and no shot's data changed
    for QUIET_S, or CEILING_S have passed since the uploader first saw it over. A line being spoken
    (its air time filled in) isn't a change: the round goes once it's known what was said."""
    cfg, s, cl = _booth(tmp_path, api)
    pace, clock = _run(monkeypatch)
    u = _round(s, 1)
    ended = s.db.execute("SELECT ended_at FROM round_records WHERE uuid = ?", (u,)).fetchone()[0]
    seen = clock[0]

    def at(seconds):
        clock[0] = seen + seconds
        return cloud_rounds.drain(cl, cfg.db_path, pace)
    out = at(0)
    assert out.sent == [] and out.settling == 1             # watched from now
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
    seen = clock[0]
    assert at(1).sent == []
    s.db.execute("UPDATE shots SET total_yards = 251.5 WHERE record_uuid = ?", (w,))   # (GSPro's round file, late)
    s.db.commit()
    assert at(1 + cloud_rounds.QUIET_S).sent == []
    assert at(1 + 2 * cloud_rounds.QUIET_S).sent == [w] and api.puts[-1][2]["shots"][0]["total_yards"] == 251.5
    # One that never stops changing still goes at the ceiling.
    v = _round(s, 600)
    seen = clock[0]

    def more(n):   # (the booth still adding to it)
        s.db.execute("UPDATE shots SET offline_yards = ? WHERE record_uuid = ?", (float(n), v))
        s.db.commit()
    for t in range(0, int(cloud_rounds.CEILING_S), 20):
        more(t)
        assert at(t).sent == []
    more(999)
    assert at(cloud_rounds.CEILING_S).sent == [v]
    # A round taken up again and finished again is watched anew.
    x = _round(s, 900)
    seen = clock[0]
    assert at(0).settling == 1
    s.db.execute("UPDATE round_records SET state = 'in_progress' WHERE uuid = ?", (x,))
    s.db.commit()
    assert at(cloud_rounds.CEILING_S).sent == [] and x not in pace.seen
    s.db.execute("UPDATE round_records SET state = 'complete' WHERE uuid = ?", (x,))
    s.db.commit()
    assert at(cloud_rounds.CEILING_S + 1).settling == 1
    assert at(cloud_rounds.CEILING_S + 1 + cloud_rounds.QUIET_S).sent == [x]


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


def test_a_change_seen_late_in_a_long_batch_starts_its_quiet_then(tmp_path, api, monkeypatch):
    cfg, s, cl = _booth(tmp_path, api)
    a, b = _round(s, 1), _round(s, 100)
    pace, clock = _run(monkeypatch)
    assert cloud_rounds.drain(cl, cfg.db_path, pace).settling == 2                 # both seen; quiet from here
    real = cl.request

    def slow(method, path, *args, **kw):   # (the first upload takes forty seconds, and the second round changes in them)
        clock[0] += 40
        s.db.execute("UPDATE shots SET total_yards = 199.0 WHERE record_uuid = ?", (b,))
        s.db.commit()
        return real(method, path, *args, **kw)
    cl.request = slow
    clock[0] += cloud_rounds.QUIET_S
    start = clock[0]
    assert cloud_rounds.drain(cl, cfg.db_path, pace).sent == [a]
    cl.request = real
    assert pace.stood[b][1] == start + 40                                           # when it was seen, not the batch's start
    clock[0] = start + 40 + cloud_rounds.QUIET_S - 1
    assert cloud_rounds.drain(cl, cfg.db_path, pace).sent == []
    clock[0] += 1
    assert cloud_rounds.drain(cl, cfg.db_path, pace).sent == [b]


# ---- review round 11 (three reviewers) -----------------------------------------------------------

def test_a_round_taken_up_again_before_the_cloud_had_it_goes_as_it_finally_ended(tmp_path, api):
    """Nothing is kept of a try that failed: played on and over again, the round is built as it is."""
    cfg, s, cl = _booth(tmp_path, api)
    u = _round(s, 1)
    api.answers = [httpx.Response(502)]
    assert _drain(cl, cfg).waiting == 1
    s.db.execute("UPDATE round_records SET state = 'in_progress', ended_at = NULL WHERE uuid = ?", (u,))
    s.db.commit()
    assert _drain(cl, cfg).sent == [] and len(api.puts) == 1
    s.db.execute("UPDATE shots SET total_yards = 300.0 WHERE record_uuid = ?", (u,))   # (played on)
    s.end_round(u, complete=True)
    assert _drain(cl, cfg).sent == [u] and api.puts[-1][2]["shots"][0]["total_yards"] == 300.0


def test_a_round_that_changes_while_it_waits_starts_its_quiet_again(tmp_path, api, monkeypatch):
    cfg, s, cl = _booth(tmp_path, api)
    u = _round(s, 1)
    pace, clock = _run(monkeypatch)
    assert cloud_rounds.drain(cl, cfg.db_path, pace).settling == 1
    clock[0] += cloud_rounds.QUIET_S - 1
    s.db.execute("UPDATE shots SET total_yards = 123.0 WHERE record_uuid = ?", (u,))
    s.db.commit()
    assert cloud_rounds.drain(cl, cfg.db_path, pace).settling == 1
    clock[0] += cloud_rounds.QUIET_S - 1
    assert cloud_rounds.drain(cl, cfg.db_path, pace).settling == 1
    clock[0] += 1
    assert cloud_rounds.drain(cl, cfg.db_path, pace).sent == [u] and api.puts[-1][2]["shots"][0]["total_yards"] == 123.0


def test_one_round_that_cant_be_read_doesnt_hold_up_the_others(tmp_path, api, monkeypatch):
    cfg, s, cl = _booth(tmp_path, api)
    bad, good = _round(s, 1), _round(s, 100)
    real = cloud_rounds.body

    def body(db, uuid):
        if uuid == bad:
            raise ValueError("a row that can't be read")
        return real(db, uuid)
    monkeypatch.setattr(cloud_rounds, "body", body)
    out = _drain(cl, cfg)
    assert out.sent == [good] and out.settling == 0 and out.waiting == 0   # (so `cloud upload` doesn't wait for it)
    assert cloud_rounds.behind(cfg.db_path) == 1
    monkeypatch.setattr(cloud_rounds, "_signature", lambda b: (_ for _ in ()).throw(TypeError("a value that can't be sent")))
    late = _round(s, 200)
    out = _drain(cl, cfg)                                   # (failing at any step of the build: the same)
    assert out.sent == [] and out.settling == 0 and late not in api.rounds and cloud_rounds.behind(cfg.db_path) == 2
    assert cloud_rounds._holes('{"\u00b2": 4, "7": 3, "07": 5}') == {"7": 3}   # (a digit that isn't a number: dropped)


def test_a_line_longer_than_the_cloud_takes_is_cut_so_the_round_still_goes(tmp_path, api):
    cfg, s, cl = _booth(tmp_path, api)
    u = _round(s, 1)
    s.db.execute("INSERT INTO lines (session_id, ts, kind, speaker, text, record_uuid) VALUES (?, 5, 'segment', 'A', ?, ?)",
                 (s.session_id, "x" * (cloud_rounds.MAX_TEXT + 500), u))
    s.db.commit()
    assert _drain(cl, cfg).sent == [u]
    assert len(api.puts[0][2]["lines"][-1]["text"]) == cloud_rounds.MAX_TEXT


def test_the_upload_command_keeps_waiting_for_a_round_that_changed_as_it_was_built(tmp_path, api, monkeypatch):
    cfg, s, cl = _booth(tmp_path, api)
    u = _round(s, 1)
    pace, clock = _run(monkeypatch)
    assert cloud_rounds.drain(cl, cfg.db_path, pace).settling == 1
    clock[0] += cloud_rounds.QUIET_S
    real = cloud_rounds._watch

    def watch(db, pace, rows):   # (the round changes between the look and its bytes being built)
        bad = real(db, pace, rows)
        s.db.execute("UPDATE shots SET total_yards = 77.0 WHERE record_uuid = ?", (u,))
        s.db.commit()
        return bad
    monkeypatch.setattr(cloud_rounds, "_watch", watch)
    out = cloud_rounds.drain(cl, cfg.db_path, pace)
    assert out.sent == [] and out.settling == 1


def test_the_upload_command_doesnt_wait_for_a_round_that_cant_be_read(api, monkeypatch, capsys):
    from commentator import cloud_rounds
    from tests.test_cloud import _pair_cli, _unsent_round

    prof = _pair_cli(api, monkeypatch)
    u = _unsent_round(prof)

    def body(db, uuid):
        raise ValueError("a row that can't be read")
    monkeypatch.setattr(cloud_rounds, "body", body)
    monkeypatch.setattr(cloud.time, "sleep", lambda s: pytest.fail("it waited"))
    with pytest.raises(SystemExit, match="1 round.*still aren't on Shotcast Cloud") as e:
        cloud.main(["upload"])
    assert u in str(e.value)


def test_a_player_who_joined_between_the_look_and_the_build_still_goes_with_a_hash(tmp_path, api, monkeypatch):
    """Codes are made as a finished round is first looked at; a slot that appears after that and
    before its bytes are built gets its code at the build, so no slot is sent without its hash."""
    cfg = paired_cfg(tmp_path, api)
    s = Store(cfg.db_path)
    u = _round(s, 1)
    watch = cloud_rounds._watch

    def late(db, pace, rows):
        bad = watch(db, pace, rows)
        s.db.execute("INSERT INTO round_slots (slot_id, round_uuid, name, matched_by) VALUES ('late-slot', ?, 'Zed', 'legacy')", (u,))
        s.db.commit()
        return bad

    monkeypatch.setattr(cloud_rounds, "_watch", late)
    cloud_rounds.drain(cloud.Cloud(cloud.store()), cfg.db_path, settle=0)
    [(_, _, sent)] = api.puts
    assert len(sent["slots"]) == 2 and all(len(sl["claim_hash"]) == 64 for sl in sent["slots"])


def test_a_slot_with_no_code_yet_isnt_built_into_the_rounds_bytes(tmp_path, api, monkeypatch):
    """A player who joins in the instant between the codes being made and the body being read: the
    round isn't built without their hash; the next look makes it and the round goes whole."""
    cfg = paired_cfg(tmp_path, api)
    s = Store(cfg.db_path)
    u = _round(s, 1)
    made, calls = cloud_rounds.claim_codes, []

    def then_a_slot(db, uuid):
        out = made(db, uuid)
        calls.append(uuid)
        if len(calls) == 2:   # (the one in the build, after the look's)
            s.db.execute("INSERT INTO round_slots (slot_id, round_uuid, name, matched_by) "
                         "VALUES ('late-slot', ?, 'Zed', 'legacy')", (u,))
            s.db.commit()
        return out

    monkeypatch.setattr(cloud_rounds, "claim_codes", then_a_slot)
    cl = cloud.Cloud(cloud.store())
    cloud_rounds.drain(cl, cfg.db_path, settle=0)
    assert api.puts == []
    cloud_rounds.drain(cl, cfg.db_path, settle=0)
    [(_, _, sent)] = api.puts
    assert len(sent["slots"]) == 2 and all(len(sl["claim_hash"]) == 64 for sl in sent["slots"])


def test_a_round_goes_with_its_courses_card_as_gspros_files_have_it(tmp_path, api):
    """Par and stroke index by hole and each tee's rating: what a league's results in the cloud put
    a player's strokes on the holes by. The folder only when GSPro's round file named it."""
    from commentator.course_card import CourseCard

    cfg, s, cl = _booth(tmp_path, api)
    s.record_course_tees("Pebble", CourseCard(
        name="Pebble Beach", par={1: 4, 2: 5}, index={1: 7, 2: 1},
        yards={"Blue": {1: 380, 2: 510}, "Red": {1: 300, 2: 420}}, ratings={"Blue": (72.1, 131.0)}))
    known, plain, other = _round(s, 1), _round(s, 100), s.begin_round("r3", course="Elsewhere", players={"Ann": None})
    _hit(s, "Ann", 200)
    s.end_round(other, complete=True)
    s.db.execute("INSERT INTO round_folders VALUES (?, 'pebble_gsp')", (known,))
    s.db.commit()
    assert sorted(_drain(cl, cfg).sent) == sorted([known, plain, other])
    card = {p[0]: p[2]["course_card"] for p in api.puts}
    assert card[known] == {
        "folder": "pebble_gsp", "par": {"1": 4, "2": 5}, "index": {"1": 7, "2": 1},
        "tees": [{"name": "Blue", "rating": 72.1, "slope": 131.0}, {"name": "Red", "rating": None, "slope": None}]}
    assert card[plain] == card[known] | {"folder": None}
    assert card[other] is None   # (a course whose files gave no card)
