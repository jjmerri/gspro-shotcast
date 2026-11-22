import gzip
import hashlib
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


def test_a_round_changed_after_it_went_up_goes_again_as_the_next_revision(tmp_path, api):
    cfg, s, cl = _booth(tmp_path, api)
    u = _round(s, 1)
    _drain(cl, cfg)
    _change(s, u)
    assert _drain(cl, cfg).sent == [u] and [n for _, n, _ in api.puts] == [1, 2]


@pytest.mark.parametrize("answer", [
    httpx.Response(200, text="<html>ok</html>", headers={"content-type": "text/html"}),   # a proxy's page
    httpx.Response(200, json=["stored"]),
    httpx.Response(201, json={"status": "stored", "revision": 2, "current_revision": 2}),   # not this revision
    httpx.Response(201, json={"status": "stored", "revision": 1}),                           # (no latest named)
    httpx.Response(201, json={"status": "stored", "current_revision": 1}),                   # (nor which it's about)
    httpx.Response(200, json={"status": "stored", "revision": 1, "current_revision": 1}),   # stored is a 201
    httpx.Response(200, json={"status": "duplicate", "revision": 1, "current_revision": 0}),
    httpx.Response(200, json={"status": "duplicate"}),
    httpx.Response(200, json={"status": "fine", "revision": 1, "current_revision": 1}),
    httpx.Response(502, text="bad gateway"),
])
def test_only_the_clouds_own_word_for_this_revision_counts_as_sent(tmp_path, api, answer):
    cfg, s, cl = _booth(tmp_path, api)
    u = _round(s, 1)
    api.answers = [answer]
    out = _drain(cl, cfg)
    assert out.sent == [] and out.waiting == 1 and _counts(cfg) == {"waiting": 1}
    _retry_now(cfg)
    assert _drain(cl, cfg).sent == [u] and [n for _, n, _ in api.puts] == [1, 1]   # tried again, same revision


@pytest.mark.parametrize("status", ["duplicate", "superseded"])
def test_a_later_revision_on_the_cloud_holds_the_round_as_a_conflict_and_never_overwrites_it(tmp_path, api, status):
    cfg, s, cl = _booth(tmp_path, api)
    u = _round(s, 1)
    api.answers = [httpx.Response(200, json={"status": status, "revision": 1, "current_revision": 4})]
    out = _drain(cl, cfg)
    assert out.sent == [] and out.conflicts == [u]
    up = _upload(cfg, u)
    assert up["status"] == "conflict" and up["revision"] == 0 and up["frozen_body"] is not None   # its snapshot kept
    assert _counts(cfg) == {"conflict": 1}
    _retry_now(cfg)
    assert _drain(cl, cfg).sent == [] and [n for _, n, _ in api.puts] == [1]   # not sent as 5, nor again


def test_other_bytes_under_the_same_revision_hold_the_round(tmp_path, api):
    cfg, s, cl = _booth(tmp_path, api)
    u = _round(s, 1)
    api.rounds[u] = {1: b'{"someone": "else"}'}
    out = _drain(cl, cfg)
    assert out.conflicts == [u] and _upload(cfg, u)["status"] == "conflict"
    assert _drain(cl, cfg).sent == [] and len(api.puts) == 1


@pytest.mark.parametrize("own", [True, False])
def test_a_conflict_answer_never_counts_as_sent_even_naming_this_pcs_own_bytes(tmp_path, api, own):
    cfg, s, cl = _booth(tmp_path, api)
    u = _round(s, 1)
    _drain(cl, cfg)   # (revision 1)
    _change(s, u)
    api.answers = [httpx.Response(502)]
    _drain(cl, cfg)
    frozen = _upload(cfg, u)["frozen_body"]
    _retry_now(cfg)
    # (the cloud's checksum is of the JSON sent, not of its gzip bytes)
    sha = cloud_rounds.content_sha(frozen) if own else hashlib.sha256(b"other").hexdigest()
    assert cloud_rounds.content_sha(frozen) != hashlib.sha256(frozen).hexdigest()
    api.answers = [httpx.Response(409, json={"error": "revision_conflict", "revision": 2, "current_revision": 2,
                                             "sha256": sha})]
    out = _drain(cl, cfg)
    up = _upload(cfg, u)
    assert out.sent == [] and out.conflicts == [u]
    assert up["status"] == "conflict" and up["revision"] == 1 and up["frozen_body"] == frozen
    assert ("inconsistent" in up["error"]) == own
    _retry_now(cfg)
    assert _drain(cl, cfg).sent == [] and len(api.puts) == 3


def test_after_a_crash_before_the_ack_was_noted_the_same_revision_is_accepted(tmp_path, api):
    cfg, s, cl = _booth(tmp_path, api)
    u = _round(s, 1)
    api.answers = [httpx.Response(502)]   # the cloud stored it; this PC never heard (it crashed, say)
    _drain(cl, cfg)
    frozen = _upload(cfg, u)["frozen_body"]
    api.rounds[u] = {1: gzip.decompress(frozen)}   # (as the cloud keeps it: the JSON sent)
    _retry_now(cfg)
    assert _drain(cl, cfg).sent == [u]
    assert _upload(cfg, u)["revision"] == 1 and [n for _, n, _ in api.puts] == [1, 1]


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
    _change(s, changed)                                        # acknowledged, then changed here
    never = _round(s, 20)                                      # never tried
    s.db.commit()
    assert _counts(cfg) == {"sent": 1, "changed": 1, "never": 1}
    assert _counts(cfg, settle=LATER * 2) == {"settling": 3}   # just ended
    assert dict(cloud_rounds.outstanding(cfg.db_path, now=time.time() + LATER)) == {
        changed: "changed since it was sent", never: "never sent"}
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
    assert _drain(cl, cfg).unchanged == 1 and len(api.puts) == 1 and _counts(cfg) == {"parked": 1}
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
    s.db.commit()
    assert _drain(cl, cfg).sent == [u]
    b = api.puts[0][2]
    slots = {sl["slot_id"]: sl["name"] for sl in b["slots"]}
    assert sorted(slots.values()) == ["Ann", "Bo"]
    assert sorted(slots.get(sh["slot_id"], "") for sh in b["shots"]) == ["", "Ann", "Bo"]
    assert [c["slot_id"] for c in b["player_rounds"]] == [ann] and len(b["scorecards"]) == 1
    assert not any("player" in r for r in b["shots"] + b["player_rounds"])
    assert "players" not in b["scorecards"][0] and b["scorecards"][0]["side"] == "Ann & Bo"
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
    assert not {"gen", "finishes_gen", "uuid"} & set(b["round"]) and b["edit"]["left_out"] == [shot["uid"]]
    assert all("excluded" not in ln for ln in b["lines"])


def test_a_suspended_default_league_blocks_the_pc_but_one_rounds_league_only_holds_that_round(tmp_path, api):
    """The cloud says which: `league_suspended` is this PC's (nothing goes); `round_league_suspended`
    is the one round's league, so the rest of the queue isn't stuck behind it."""
    cfg, s, cl = _booth(tmp_path, api)
    a, b = _round(s, 1), _round(s, 100)
    api.answers = [httpx.Response(403, json={"error": "round_league_suspended", "message": "this round's league is suspended"})]
    out = _drain(cl, cfg)
    assert out.sent == [b] and out.waiting == 1 and _upload(cfg, a)["status"] == "queued"
    assert "suspended" in _upload(cfg, a)["error"] and _upload(cfg, a)["frozen_body"] is not None
    _retry_now(cfg)
    assert _drain(cl, cfg).sent == [a]   # (once the league is back)
    c = _round(s, 200)
    api.answers = [httpx.Response(403, json={"error": "league_suspended", "message": "this league is suspended"})]
    with pytest.raises(cloud_rounds.Blocked, match="suspended"):
        _drain(cl, cfg)
    assert _upload(cfg, c)["frozen_body"] is not None


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
        return json.loads(gzip.decompress(cloud_rounds.encode(big, 1)))
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
