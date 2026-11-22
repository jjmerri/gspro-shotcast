import hashlib
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
    httpx.Response(201, json={"status": "stored", "revision": 2}),                         # not this revision
    httpx.Response(200, json={"status": "stored", "revision": 1}),                         # stored is a 201
    httpx.Response(200, json={"status": "duplicate", "revision": 0}),
    httpx.Response(200, json={"status": "duplicate"}),
    httpx.Response(200, json={"status": "fine", "revision": 1}),
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
    api.answers = [httpx.Response(200, json={"status": status, "revision": 4})]
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
    api.rounds[u] = {1: b"someone else's"}
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
    sha = hashlib.sha256(frozen if own else b"other").hexdigest()
    api.answers = [httpx.Response(409, json={"error": "revision_conflict", "revision": 2, "sha256": sha})]
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
    api.rounds[u] = {1: frozen}
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


def test_a_practice_stretch_left_idle_is_ended_and_goes_up_without_another_shot(tmp_path, api):
    from commentator.round_record import PRACTICE_IDLE_SECONDS

    cfg, s, cl = _booth(tmp_path, api)
    t0 = time.time()
    _hit(s, "Ann", t0)                      # on the range: a practice stretch, in progress
    _hit(s, "Ann", t0 + 60)
    s.db.commit()
    [(u, kind, state)] = s.db.execute("SELECT uuid, kind, state FROM round_records").fetchall()
    assert (kind, state) == ("practice", "in_progress")
    soon = t0 + 60 + PRACTICE_IDLE_SECONDS - 1
    assert cloud_rounds.drain(cl, cfg.db_path, now=soon).sent == [] and api.puts == []
    assert s.db.execute("SELECT state FROM round_records").fetchone()[0] == "in_progress"   # (it may get a shot yet)
    later = t0 + 60 + PRACTICE_IDLE_SECONDS + 1
    assert cloud_rounds.drain(cl, cfg.db_path, now=later, settle=0).sent == [u]
    assert tuple(s.db.execute("SELECT state, ended_at FROM round_records WHERE uuid = ?", (u,)).fetchone()) \
        == ("finished", t0 + 60)            # (ended at its last row, as the booth ends it)
    assert [(rid, n, len(b["shots"])) for rid, n, b in api.puts] == [(u, 1, 2)]
    _hit(s, "Ann", later + 5)               # the booth, still running, finds it ended: a new stretch
    s.db.commit()
    rows = s.db.execute("SELECT uuid, state FROM round_records ORDER BY started_at").fetchall()
    assert [tuple(r) for r in rows] == [(u, "finished"), (rows[1][0], "in_progress")] and rows[1][0] != u
    assert s.db.execute("SELECT COUNT(*) FROM shots WHERE record_uuid = ?", (u,)).fetchone()[0] == 2


def test_ending_idle_practice_leaves_a_round_being_played_and_a_stretch_that_just_got_a_shot(tmp_path, api):
    from commentator.round_record import PRACTICE_IDLE_SECONDS

    cfg, s, cl = _booth(tmp_path, api)
    t0 = time.time()
    _hit(s, "Ann", t0)
    _said(s)                                # (said just now: time.time(), its last row)
    s.db.commit()
    db = cloud_rounds.connect(cfg.db_path)
    try:
        assert cloud_rounds.settle_idle(db, time.time() + PRACTICE_IDLE_SECONDS - 5) == 0
        u = s.begin_round("r1", course="Pebble", players={"Ann": None})   # (ends the stretch itself)
        _hit(s, "Ann", t0 + 10)
        s.db.commit()
        assert cloud_rounds.settle_idle(db, t0 + 10 * PRACTICE_IDLE_SECONDS) == 0
        assert s.db.execute("SELECT state FROM round_records WHERE uuid = ?", (u,)).fetchone()[0] == "in_progress"
    finally:
        db.close()


# ---- review round 6 ---------------------------------------------------------------------------

@pytest.mark.parametrize("answer", [httpx.Response(404, json={"detail": "Not Found"}),
                                    httpx.Response(409, json={"error": "something_new"}),
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
    api.answers = [httpx.Response(422, json={"detail": "not a round"})]
    assert _drain(cl, cfg).parked == [u] and _counts(cfg) == {"parked": 1}
    assert _drain(cl, cfg).sent == [] and len(api.puts) == 1     # refused as it stands: not sent again
    s.db.execute("UPDATE round_records SET gen = gen + 1 WHERE uuid = ?", (u,))   # (touched: it says the same)
    s.db.commit()
    assert _drain(cl, cfg).unchanged == 1 and len(api.puts) == 1 and _counts(cfg) == {"parked": 1}
    api.answers = [httpx.Response(422, json={"detail": "not a round"})]
    _change(s, u)                                                # corrected here: it goes again
    assert _drain(cl, cfg).parked == [u] and len(api.puts) == 2  # (refused again: parked again)
    assert cloud_rounds.requeue(cfg.db_path) == 1                # put right on Shotcast Cloud: asked to go again
    assert _drain(cl, cfg).sent == [u] and _counts(cfg) == {"sent": 1} and api.puts[-1][1] == 1
    assert cloud_rounds.requeue(cfg.db_path) == 0


def test_the_command_line_can_send_refused_rounds_again(tmp_path, api, monkeypatch, capsys):
    cfg, s, cl = _booth(tmp_path, api)
    u = _round(s, 1)
    s.db.close()
    api.answers = [httpx.Response(422, json={"detail": "not a round"})]
    with pytest.raises(SystemExit, match="1 round.*still aren't on Shotcast Cloud") as e:
        cloud.main(["upload", "--settle", "-100000"])
    assert "1 refused" in capsys.readouterr().out and u in str(e.value) and "refused" in str(e.value)
    with pytest.raises(SystemExit, match="still aren't on Shotcast Cloud"):   # (nothing tried this time: it still fails)
        cloud.main(["upload", "--settle", "-100000"])
    assert "Sent 0" in capsys.readouterr().out
    cloud.main(["upload", "--settle", "-100000", "--refused"])
    out = capsys.readouterr().out
    assert "Trying 1 refused" in out and "Sent 1" in out and u in api.rounds
