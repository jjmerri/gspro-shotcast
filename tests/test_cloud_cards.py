"""A venue bay's claim cards (commentator/cloud_cards.py): a code a player, kept with the round and sent
as its hash; cards up as a round ends, showing who has claimed; down when they should be."""
import asyncio
import hashlib
import json

import pytest

from commentator import cloud, cloud_booth, cloud_cards, cloud_rounds
from commentator.store import Store
from tests.test_cloud import API, FakeAPI, mock_http, paired_cfg
from tests.test_round_record import _hit


@pytest.fixture
def api(monkeypatch):
    fake = FakeAPI()
    fake.claim_cards = True
    mock_http(monkeypatch, fake)
    return fake


def _play(s, name, players=("Ann", "Bob"), start=1):
    u = s.begin_round(name, course="Pebble", players={p: None for p in players})
    for i, p in enumerate(players):
        _hit(s, p, start + i)
    s.db.commit()
    return u


def _end(s, u):
    s.end_round(u, complete=True)
    s.db.commit()


def _cards(cfg, api):
    """The cards' loop for a paired booth, and the windows it put up."""
    up = cloud_booth.Uploads(cfg, cloud.locate())
    windows = []
    return cloud_cards.Cards(cfg.db_path, API, up._cloud, window=lambda shown, where: windows.append(shown)), windows


def _send(cfg):
    out = cloud_rounds.drain(cloud.Cloud(cloud.store()), cfg.db_path, settle=0)
    assert not out.parked
    return out


def _code(url):
    assert url.startswith(f"{API}/claim/")
    return url.rsplit("/", 1)[1]


# ---- the codes ---------------------------------------------------------------------------------

def test_a_players_code_is_made_once_and_its_hash_goes_with_the_round(tmp_path, api):
    cfg = paired_cfg(tmp_path, api)
    s = Store(cfg.db_path)
    u = _play(s, "r1")
    _end(s, u)
    db = cloud_rounds.connect(cfg.db_path)
    codes = cloud_rounds.claim_codes(db, u)
    assert len(codes) == 2 and len(set(codes.values())) == 2
    assert all(len(c) == 22 and c.isascii() for c in codes.values())   # (the cloud's code: 22 base64url characters)
    assert cloud_rounds.claim_codes(db, u) == codes                    # kept: asked again, the same
    db.close()
    assert _send(cfg).sent == [u]
    sent = {sl["slot_id"]: sl["claim_hash"] for sl in api.puts[0][2]["slots"]}
    assert sent == {slot: hashlib.sha256(code.encode()).hexdigest() for slot, code in codes.items()}
    assert not any(code in json.dumps(api.puts[0][2]) for code in codes.values())   # the code itself never goes


def test_a_round_nobody_asked_codes_for_still_goes_with_hashes_and_they_dont_restart_its_quiet(tmp_path, api):
    """The uploader makes the codes before it first reads a finished round, so the hashes aren't a
    change it sees later: the round goes at its first quiet, with them."""
    cfg = paired_cfg(tmp_path, api)
    s = Store(cfg.db_path)
    u = _play(s, "r1")
    _end(s, u)
    pace = cloud_rounds.Pace()
    cl = cloud.Cloud(cloud.store())
    cloud_rounds.drain(cl, cfg.db_path, pace, settle=0)
    assert [p[0] for p in api.puts] == [u] and all("claim_hash" in sl for sl in api.puts[0][2]["slots"])
    db = cloud_rounds.connect(cfg.db_path)
    assert {sl["claim_hash"] for sl in api.puts[0][2]["slots"]} == {
        cloud_rounds.claim_hash(c) for c in cloud_rounds.claim_codes(db, u).values()}
    db.close()


def test_a_qr_is_square_and_differs_by_code():
    a, b = (cloud_cards.matrix(cloud_cards.claim_url("https://shotcast.example", c * 22)) for c in "AB")
    assert len(a) >= 21 and all(len(row) == len(a) for row in a) and a != b
    assert cloud_cards.claim_url("https://shotcast.example/", "x") == "https://shotcast.example/claim/x"


# ---- when the cards show -----------------------------------------------------------------------

def test_a_bay_that_isnt_a_venues_shows_no_cards_and_asks_nothing_more(tmp_path, api):
    api.claim_cards = False
    cfg = paired_cfg(tmp_path, api)
    s = Store(cfg.db_path)
    cards, windows = _cards(cfg, api)
    cards.look(0)
    u = _play(s, "r1")
    _end(s, u)
    api.calls.clear()
    cards.look(2)
    cards.look(500)
    assert windows == [] and api.calls == []


def test_an_api_that_cant_say_is_asked_again_later_and_shows_nothing_meanwhile(tmp_path, api):
    cfg = paired_cfg(tmp_path, api)
    cards, windows = _cards(cfg, api)
    api.minimum = "9.9.9"   # (every call refused)
    cards.look(0)
    api.minimum = None
    cards.look(10)
    assert cards.wanted is None
    cards.look(cloud_cards.ASK_S)
    assert cards.wanted is True and windows == []


def test_a_round_over_before_the_booth_started_gets_no_cards_and_one_that_ends_now_does(tmp_path, api):
    cfg = paired_cfg(tmp_path, api)
    s = Store(cfg.db_path)
    _end(s, _play(s, "old"))
    cards, windows = _cards(cfg, api)
    cards.look(0)
    assert windows == []
    u = _play(s, "r1", start=50)
    cards.look(2)
    assert windows == []                      # in progress: nothing yet
    _end(s, u)
    cards.look(4)
    [shown] = windows
    assert shown.uuid == u and sorted(c.name for c in shown.cards) == ["Ann", "Bob"]
    db = cloud_rounds.connect(cfg.db_path)
    assert sorted(_code(c.url) for c in shown.cards) == sorted(cloud_rounds.claim_codes(db, u).values())
    db.close()
    cards.look(6)
    assert len(windows) == 1 and not shown.down.is_set()   # still the same cards


def test_the_cards_show_who_has_claimed_once_the_round_is_there(tmp_path, api):
    cfg = paired_cfg(tmp_path, api)
    s = Store(cfg.db_path)
    cards, windows = _cards(cfg, api)
    cards.look(0)
    u = _play(s, "r1")
    _end(s, u)
    cards.look(2)
    [shown] = windows
    assert [c.claimed for c in shown.cards] == [False, False]   # (the round isn't there yet: 404, and it waits)
    _send(cfg)
    first = shown.cards[0].slot_id
    api.claimed[(u, first)] = "Ann R."
    cards.look(3)                                               # (asked every POLL_S, not every look)
    assert [c.claimed for c in shown.cards] == [False, False]
    cards.look(2 + cloud_cards.POLL_S)
    assert [(c.claimed, c.by) for c in shown.snapshot()] == [(True, "Ann R."), (False, None)]
    del api.claimed[(u, first)]                                   # staff undid it: the card goes back
    cards.look(2 + 2 * cloud_cards.POLL_S)
    assert [(c.claimed, c.by) for c in shown.snapshot()] == [(False, None), (False, None)]
    assert not shown.down.is_set()
    # The sent body's hashes are the shown codes': what's scanned is what the cloud was told.
    assert {sl["claim_hash"] for sl in api.puts[0][2]["slots"]} == {
        cloud_rounds.claim_hash(_code(c.url)) for c in shown.cards}


def test_the_cards_come_down_a_while_after_everyone_has_claimed(tmp_path, api):
    cfg = paired_cfg(tmp_path, api)
    s = Store(cfg.db_path)
    cards, windows = _cards(cfg, api)
    cards.look(0)
    u = _play(s, "r1")
    _end(s, u)
    cards.look(2)
    [shown] = windows
    _send(cfg)
    for c in shown.cards:
        api.claimed[(u, c.slot_id)] = "Someone"
    cards.look(10)
    assert shown.all_claimed_at == 10 and not shown.down.is_set()
    cards.look(10 + cloud_cards.CLAIMED_S - 1)
    assert not shown.down.is_set()
    cards.look(10 + cloud_cards.CLAIMED_S)
    assert shown.down.is_set() and cards.shown is None


@pytest.mark.parametrize("why", ["closed", "too long", "next round"])
def test_the_cards_come_down_when_closed_after_a_while_or_when_a_round_is_played_again(tmp_path, api, why):
    cfg = paired_cfg(tmp_path, api)
    s = Store(cfg.db_path)
    cards, windows = _cards(cfg, api)
    cards.look(0)
    u = _play(s, "r1")
    _end(s, u)
    cards.look(2)
    [shown] = windows
    at = 4
    if why == "closed":
        shown.closed.set()
    elif why == "too long":
        cards.look(2 + cloud_cards.SHOW_S - 1)
        assert not shown.down.is_set()
        at = 2 + cloud_cards.SHOW_S
    else:
        _play(s, "r2", start=50)
    cards.look(at)
    assert shown.down.is_set() and cards.shown is None and len(windows) == 1
    api.calls.clear()
    cards.look(at + 100)
    assert not [c for c in api.calls if c[2].endswith("/claims")]   # nothing more is asked about it


def test_the_next_rounds_cards_replace_the_last(tmp_path, api):
    cfg = paired_cfg(tmp_path, api)
    s = Store(cfg.db_path)
    cards, windows = _cards(cfg, api)
    cards.look(0)
    first = _play(s, "r1")
    _end(s, first)
    cards.look(2)
    second = _play(s, "r2", players=("Cat",), start=50)
    cards.look(4)
    _end(s, second)
    cards.look(6)
    assert [w.uuid for w in windows] == [first, second] and windows[0].down.is_set()
    assert [c.name for c in windows[1].cards] == ["Cat"] and not windows[1].down.is_set()


def test_what_the_cloud_says_about_claims_is_only_taken_as_a_tick_and_a_short_name():
    shown = cloud_cards.Shown("u", [cloud_cards.Card("a", "Ann", "x"), cloud_cards.Card("b", "Bob", "y")], 0)
    for junk in (None, {"a": 1}, ["a", 3], [{"slot_id": "zz", "claimed": True}]):
        cloud_cards.apply(shown, junk)
    assert not any(c.claimed for c in shown.cards)
    cloud_cards.apply(shown, [{"slot_id": "a", "claimed": "yes", "name": "X"},
                              {"slot_id": "b", "claimed": True, "name": "N" * 500}])
    assert [(c.claimed, c.by) for c in shown.cards] == [(False, None), (True, "N" * 40)]
    cloud_cards.apply(shown, [{"slot_id": "b", "claimed": True, "name": 7}])
    assert (shown.cards[1].claimed, shown.cards[1].by) == (True, None)


# ---- with the booth ----------------------------------------------------------------------------

def test_the_cards_loop_runs_beside_the_upload_loop_and_ends_with_it(tmp_path, api, monkeypatch):
    monkeypatch.setattr(cloud_booth, "DRAIN_S", 0.01)
    monkeypatch.setattr(cloud_cards, "LOOK_S", 0.01)
    monkeypatch.setattr(cloud_rounds, "CEILING_S", 0.0)
    windows = []
    monkeypatch.setattr(cloud_cards, "Window", lambda shown, where: type("W", (), {
        "show": lambda self: windows.append((shown, where))})())
    cfg = paired_cfg(tmp_path, api)
    s = Store(cfg.db_path)
    where = lambda: (0, 0, 1920, 1080)   # noqa: E731

    async def go():
        task = cloud_booth.start(cfg, cloud.locate(), where=where)
        await asyncio.sleep(0.1)          # (its first look: nothing over yet)
        u = _play(s, "r1")
        _end(s, u)
        for _ in range(500):
            if windows and u in api.rounds:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return u

    u = asyncio.run(go())
    [(shown, got)] = windows
    assert shown.uuid == u and got is where and u in api.rounds
    assert shown.down.is_set()   # the booth stopping takes them down
