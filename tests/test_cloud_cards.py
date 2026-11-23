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
    return cloud_cards.Cards(cfg.db_path, API, up._cloud, tuple(up.identity[:3]),
                             window=lambda shown, where: windows.append(shown)), windows


def _step(cards, now):
    """One turn of both of the cards' loops."""
    cards.look(now)
    cards.ask(now)


def _send(cfg):
    out = cloud_rounds.drain(cloud.Cloud(cloud.store()), cfg.db_path, settle=0)
    assert not out.parked
    return out


def _linked(url):
    """The (name, code) pairs a QR's link carries after its `#`, as the site's page reads them."""
    from urllib.parse import parse_qsl

    page, _, rest = url.partition("#")
    assert page == f"{API}/claim/"
    return [(name, code) for code, name in parse_qsl(rest, strict_parsing=True)]


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


def test_a_round_nobody_asked_codes_for_still_goes_with_hashes_and_they_dont_restart_its_quiet(tmp_path, api,
                                                                                                monkeypatch):
    """The uploader makes the codes before it first reads a finished round, so the hashes aren't a
    change it sees later: the round goes at its first quiet, with them."""
    cfg = paired_cfg(tmp_path, api)
    s = Store(cfg.db_path)
    u = _play(s, "r1")
    _end(s, u)
    clock = [1000.0]
    monkeypatch.setattr(cloud_rounds, "_monotonic", lambda: clock[0])
    pace, cl = cloud_rounds.Pace(), cloud.Cloud(cloud.store())
    cloud_rounds.drain(cl, cfg.db_path, pace)             # first seen over
    clock[0] += cloud_rounds.QUIET_S - 1
    cloud_rounds.drain(cl, cfg.db_path, pace)
    assert api.puts == []                                 # not quiet long enough yet
    clock[0] += 1
    cloud_rounds.drain(cl, cfg.db_path, pace)             # quiet since it was first seen: the codes changed nothing
    assert [p[0] for p in api.puts] == [u]
    db = cloud_rounds.connect(cfg.db_path)
    assert {sl["claim_hash"] for sl in api.puts[0][2]["slots"]} == {
        cloud_rounds.claim_hash(c) for c in cloud_rounds.claim_codes(db, u).values()}
    db.close()


def _read(link):
    """A link's (code, name) pairs, as the site's page reads them."""
    from urllib.parse import parse_qsl

    assert link.count("#") == 1
    return parse_qsl(link.split("#")[1], strict_parsing=True, keep_blank_values=True)


def test_the_one_link_carries_every_players_name_and_code_after_its_hash():
    link = cloud_cards.claim_link("https://shotcast.example/", [("Ann", "a" * 22), ("B&C=#2/é", "b" * 22)])
    assert link.startswith("https://shotcast.example/claim/#")
    assert _read(link) == [("a" * 22, "Ann"), ("b" * 22, "B&C=#2/é")]
    [(_, name)] = _read(cloud_cards.claim_link("https://shotcast.example", [("N" * 200, "c" * 22)]))
    assert name == "N" * cloud_cards.NAME_BYTES   # only enough of a name to pick it by


@pytest.mark.parametrize("api", ["https://app.shotcast.example", "https://" + "venue-bookings." * 3 + "shotcast.example"])
@pytest.mark.parametrize("name", ["Александр Иванович", "山田太郎山田太郎山田太郎山田太郎", "😀" * 16, "A" * 40, "",
                                  "Christopher Alexander Wilson"])
def test_a_full_bay_of_any_names_still_makes_a_code_coarse_enough_to_scan(name, api):
    """Names are measured as the link writes them (an accented letter is six characters there, an
    emoji twelve), and cut further when the link would be too long (a long site address, many
    players): every player's code is always in it, and the code it makes is drawn at four pixels a
    module or more on a 1080p screen, and on a small one."""
    players = [(label, chr(65 + i) * 22) for i, label in enumerate(cloud_cards.labels([name] * cloud_cards.MAX_PLAYERS))]
    link = cloud_cards.claim_link(api, players)
    assert len(link) <= cloud_cards.LINK_BYTES
    assert [code for code, _ in _read(link)] == [code for _, code in players]
    modules = len(cloud_cards.matrix(link))
    for height in (1080, 768, 480):
        side = cloud_cards.code_side(height, modules)
        assert side // (modules + 2 * cloud_cards.QUIET) >= cloud_cards.MODULE_PX and side <= height * 0.9
    shown = [n for _, n in _read(link)]
    assert len(set(shown)) == len(shown) or not any(shown)   # told apart, or left to the page's "Player N"


PAGE_NAME = 64   # what shotcast-cloud's claim.js shows of a name (its slice)


def _link_names(names):
    got = cloud_cards.labels(names)
    link = cloud_cards.claim_link("https://app.shotcast.example", [(n, chr(97 + i) * 22) for i, n in enumerate(got)])
    return [n for _, n in _read(link)]


def test_every_seat_is_numbered_so_no_two_players_read_the_same():
    assert cloud_cards.labels(["Mike"]) == ["Mike"] and cloud_cards.labels([""]) == ["Player"]   # alone: no number
    assert cloud_cards.labels(["  Ann   Lee ", "Bob\x07"]) == ["Ann Lee (1)", "Bob (2)"]          # tidied
    assert cloud_cards.labels(["山田\u3000太郎", "Ann\tLee"]) == ["山田 太郎 (1)", "Ann Lee (2)"]   # any space is a space
    # No room for any of a name: nothing, and the page says "Player 3".
    assert cloud_cards._in_link("김민수 (3)", 3, 6) == "" and cloud_cards._in_link("Mike (3)", 3, 2) == "Mi (3)"
    assert cloud_cards.labels(["Mike", "Ann", "Mike", " "]) == ["Mike (1)", "Ann (2)", "Mike (3)", "Player (4)"]
    # Whatever was typed, and however much of a name the link has room for, the number tells them apart.
    for names in (["Mike", "Mike", "Mike (2)"], ["Charlie", "Charlie Brown"], ["Jos\u00e9", "Jose\u0301"],
                  ["Christopher Alexander Wilson East", "Christopher Alexander Wilson West"], ["Mike"] * 12):
        shown = _link_names(names)
        assert len(set(shown)) == len(names) and [n.rsplit(" ", 1)[1] for n in shown] == [
            f"({i})" for i in range(1, len(names) + 1)], shown


def test_a_players_name_keeps_its_room_in_the_link_whatever_it_is_written_in():
    """The seat's number isn't counted against the name's room: Korean players are still called by
    their whole names on the phone, where they pick, and a full bay still has names."""
    names = ["김민수", "김민준", "이서연", "박지훈"]
    assert _link_names(names) == [f"{n} ({i})" for i, n in enumerate(names, 1)]
    last = _link_names(["Mike"] * 9 + ["Bartholomew-Maximilian-Featherstonehaugh"])[-1]
    assert last.endswith(" (10)") and len(last) <= PAGE_NAME   # the page shows it whole
    assert _link_names(["Mike"] * 12) == [f"Mike ({i})" for i in range(1, 13)]


def test_a_player_who_joins_as_the_codes_are_made_keeps_the_card_waiting_for_them(tmp_path, api, monkeypatch):
    cfg = paired_cfg(tmp_path, api)
    s = Store(cfg.db_path)
    cards, windows = _cards(cfg, api)
    _step(cards, 0)
    u = _end(s, _play(s, "r1")) or "r1"
    made = cloud_rounds.claim_codes

    def then_a_slot(db, uuid):
        out = made(db, uuid)
        s.db.execute("INSERT OR IGNORE INTO round_slots (slot_id, round_uuid, name, matched_by) "
                     "VALUES ('late-slot', ?, 'Zed', 'legacy')", (u,))
        s.db.commit()
        return out

    monkeypatch.setattr(cloud_rounds, "claim_codes", then_a_slot)
    with pytest.raises(RuntimeError):
        cards.look(2)
    assert windows == [] and cards.pending == (u, 2)
    monkeypatch.setattr(cloud_rounds, "claim_codes", made)
    _step(cards, 4)
    assert sorted(c.name[:3] for c in windows[0].cards) == ["Ann", "Bob", "Zed"]   # (seat order isn't set here)


def test_a_qr_is_square_differs_by_link_and_stays_scannable_with_a_full_bay():
    a, b = (cloud_cards.matrix(cloud_cards.claim_link("https://shotcast.example", [("Ann", c * 22)])) for c in "AB")
    assert len(a) >= 21 and all(len(row) == len(a) for row in a) and a != b
    full = cloud_cards.claim_link("https://app.shotcast.example", [
        (label, "A1b2C3d4E5f6G7h8I9j0Kl") for label in cloud_cards.labels(["Bartholomew Longname"] * 8)])
    assert len(cloud_cards.matrix(full)) <= 89   # (eight players, numbered: within the largest code drawn)


# ---- when the cards show -----------------------------------------------------------------------

def test_a_bay_that_isnt_a_venues_shows_no_cards_and_is_asked_only_as_a_round_ends(tmp_path, api):
    api.claim_cards = False
    cfg = paired_cfg(tmp_path, api)
    s = Store(cfg.db_path)
    cards, windows = _cards(cfg, api)
    api.calls.clear()
    _step(cards, 0)
    u = _play(s, "r1")
    _step(cards, 2)
    assert api.calls == []                      # nothing is asked while nothing has ended
    _end(s, u)
    _step(cards, 4)
    asked = lambda: [c[2] for c in api.calls if c[2] != "/api/devices/refresh"]   # noqa: E731
    assert asked() == ["/api/devices/me"] and windows == [] and cards.pending is None
    _step(cards, 500)
    assert asked() == ["/api/devices/me"]
    # The tenant becomes a venue while the booth runs: the next round that ends has cards.
    api.claim_cards = True
    u2 = _play(s, "r2", start=50)
    _step(cards, 502)
    _end(s, u2)
    _step(cards, 504)
    assert [w.uuid for w in windows] == [u2]


def test_a_round_that_ends_while_the_cloud_cant_be_asked_gets_its_cards_once_it_can(tmp_path, api):
    cfg = paired_cfg(tmp_path, api)
    s = Store(cfg.db_path)
    cards, windows = _cards(cfg, api)
    _step(cards, 0)
    api.minimum = "9.9.9"   # (every call refused)
    u = _play(s, "r1")
    _end(s, u)
    _step(cards, 2)
    api.minimum = None
    _step(cards, 10)
    assert windows == [] and cards.pending == (u, 2)   # asked again only after ASK_S
    _step(cards, 2 + cloud_cards.ASK_S)
    [shown] = windows
    assert shown.uuid == u and shown.since == 2        # (its ten minutes count from when it ended)


def test_cards_never_asked_for_in_time_are_dropped(tmp_path, api):
    cfg = paired_cfg(tmp_path, api)
    s = Store(cfg.db_path)
    cards, windows = _cards(cfg, api)
    _step(cards, 0)
    api.minimum = "9.9.9"
    _end(s, _play(s, "r1"))
    _step(cards, 2)
    api.minimum = None
    _step(cards, 2 + cloud_cards.SHOW_S)
    assert windows == [] and cards.pending is None


def test_a_busy_season_file_as_a_round_ends_doesnt_lose_its_cards(tmp_path, api, monkeypatch):
    cfg = paired_cfg(tmp_path, api)
    s = Store(cfg.db_path)
    cards, windows = _cards(cfg, api)
    _step(cards, 0)
    u = _play(s, "r1")
    _end(s, u)
    real = cloud_cards.cards_for
    monkeypatch.setattr(cloud_cards, "cards_for", lambda *a: (_ for _ in ()).throw(RuntimeError("database is locked")))
    with pytest.raises(RuntimeError):
        cards.look(2)
    assert windows == []
    monkeypatch.setattr(cloud_cards, "cards_for", real)
    _step(cards, 4)
    assert [w.uuid for w in windows] == [u]


def test_a_season_file_that_cant_be_read_takes_the_cards_down(tmp_path, api, monkeypatch):
    cfg = paired_cfg(tmp_path, api)
    s = Store(cfg.db_path)
    cards, windows = _cards(cfg, api)
    _step(cards, 0)
    _end(s, _play(s, "r1"))
    _step(cards, 2)
    [shown] = windows
    monkeypatch.setattr(cloud_cards, "state", lambda db: (_ for _ in ()).throw(RuntimeError("disk I/O error")))
    with pytest.raises(RuntimeError):
        cards.look(4)
    assert shown.down.is_set() and cards.shown is None


def test_a_season_file_that_isnt_the_pairings_shows_no_codes_and_ends_the_loop(tmp_path, api):
    cfg = paired_cfg(tmp_path, api)
    s = Store(cfg.db_path)
    cards, windows = _cards(cfg, api)
    _step(cards, 0)
    _end(s, _play(s, "r1"))
    _step(cards, 2)
    [shown] = windows
    s.db.execute("UPDATE cloud_pairing SET device_id = 'another'")
    s.db.commit()
    _end(s, _play(s, "r2", start=50))
    asyncio.run(asyncio.wait_for(cards.run(), 5))   # (it ends by itself)
    assert shown.down.is_set() and len(windows) == 1


def test_practice_starting_takes_the_last_groups_cards_down(tmp_path, api):
    cfg = paired_cfg(tmp_path, api)
    s = Store(cfg.db_path)
    cards, windows = _cards(cfg, api)
    _step(cards, 0)
    _end(s, _play(s, "r1"))
    _step(cards, 2)
    [shown] = windows
    _hit(s, "Zed", 90)   # (no round on: a practice stretch)
    s.db.commit()
    assert s.db.execute("SELECT count(*) FROM round_records WHERE kind = 'practice' AND state = 'in_progress'"
                        ).fetchone()[0] == 1
    _step(cards, 4)
    assert shown.down.is_set() and cards.shown is None


def test_a_cloud_that_doesnt_answer_about_claims_never_keeps_cards_up(tmp_path, api, monkeypatch):
    """Who has claimed is asked apart from the look at the season file: with that asking failing
    (or still waiting on the network), the next round starting still takes the cards down."""
    cfg = paired_cfg(tmp_path, api)
    s = Store(cfg.db_path)
    cards, windows = _cards(cfg, api)
    _step(cards, 0)
    _end(s, _play(s, "r1"))
    _step(cards, 2)
    [shown] = windows
    api.minimum = "9.9.9"
    cards.ask(10)            # (refused: nothing changes, nothing raised)
    _play(s, "r2", start=50)
    cards.look(12)           # the look alone, with no answer from the cloud
    assert shown.down.is_set()


def test_the_cards_come_down_for_the_next_group_while_the_cloud_is_still_being_asked(tmp_path, api, monkeypatch):
    """With the real loops: the cloud's answer about claims is held, the next round starts, and the
    cards are down before that answer ever comes."""
    import threading

    monkeypatch.setattr(cloud_cards, "LOOK_S", 0.01)
    cfg = paired_cfg(tmp_path, api)
    s = Store(cfg.db_path)
    cards, windows = _cards(cfg, api)
    asking, release = threading.Event(), threading.Event()

    def held(now):
        if cards.shown is not None:
            asking.set()
            release.wait(10)

    cards.ask = held

    async def go():
        task = asyncio.create_task(cards.run())
        await asyncio.sleep(0.1)
        _end(s, _play(s, "r1"))
        for _ in range(500):
            if windows and asking.is_set():
                break
            await asyncio.sleep(0.01)
        _play(s, "r2", start=50)
        for _ in range(500):
            if windows[0].down.is_set():
                break
            await asyncio.sleep(0.01)
        down = windows[0].down.is_set() and not release.is_set()
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return down

    assert asyncio.run(go())


def test_someone_playing_before_the_cloud_answers_gets_no_cards_of_the_last_group(tmp_path, api):
    cfg = paired_cfg(tmp_path, api)
    s = Store(cfg.db_path)
    cards, windows = _cards(cfg, api)
    _step(cards, 0)
    _end(s, _play(s, "r1"))
    me = cards._wanted

    def slow(now):   # (the next group starts hitting while the cloud is being asked)
        _hit(s, "Zed", 90)
        s.db.commit()
        return me(now)

    cards._wanted = slow
    _step(cards, 2)
    assert windows == [] and cards.pending is None


def test_a_yes_is_kept_for_the_round_and_an_answer_that_doesnt_say_is_asked_again(tmp_path, api, monkeypatch):
    cfg = paired_cfg(tmp_path, api)
    s = Store(cfg.db_path)
    cards, windows = _cards(cfg, api)
    _step(cards, 0)
    u = _end(s, _play(s, "r1")) or "r1"
    api.claim_cards = "yes"          # (not a yes or a no)
    _step(cards, 2)
    assert windows == [] and cards.pending == (u, 2)
    api.claim_cards = True
    real = cloud_cards.cards_for
    monkeypatch.setattr(cloud_cards, "cards_for", lambda *a: (_ for _ in ()).throw(RuntimeError("database is locked")))
    with pytest.raises(RuntimeError):
        cards.look(2 + cloud_cards.ASK_S)
    monkeypatch.setattr(cloud_cards, "cards_for", real)
    api.calls.clear()
    _step(cards, 4 + cloud_cards.ASK_S)
    assert [w.uuid for w in windows] == [u]
    assert not [c for c in api.calls if c[2] == "/api/devices/me"]   # the yes wasn't asked for twice


def test_a_round_found_over_after_the_file_went_unread_gets_no_cards(tmp_path, api):
    cfg = paired_cfg(tmp_path, api)
    s = Store(cfg.db_path)
    cards, windows = _cards(cfg, api)
    _step(cards, 0)
    _end(s, _play(s, "r1"))
    _step(cards, cloud_cards.STALE_S + 1)   # (no look could read it in between: it ended nobody knows when)
    assert windows == [] and cards.pending is None


def test_the_claims_asking_ending_takes_the_cards_down_with_it(tmp_path, api, monkeypatch):
    monkeypatch.setattr(cloud_cards, "LOOK_S", 0.01)
    cfg = paired_cfg(tmp_path, api)
    s = Store(cfg.db_path)
    cards, windows = _cards(cfg, api)

    def gone(now):
        if cards.shown is not None:
            raise cloud.PairingChanged("this PC's Shotcast Cloud pairing changed")

    cards.ask = gone

    async def go():
        task = asyncio.create_task(cards.run())
        await asyncio.sleep(0.1)
        _end(s, _play(s, "r1"))
        await asyncio.wait_for(task, 10)   # (it ends by itself)

    asyncio.run(go())
    assert len(windows) == 1 and windows[0].down.is_set()


def test_someone_who_played_and_stopped_before_the_cloud_answered_still_means_no_card(tmp_path, api):
    cfg = paired_cfg(tmp_path, api)
    s = Store(cfg.db_path)
    cards, windows = _cards(cfg, api)
    _step(cards, 0)
    _end(s, _play(s, "r1"))
    me = cards._wanted

    def slow(now):   # (the next group starts a round and gives it up while the cloud is being asked)
        _end(s, _play(s, "r2", start=50))
        return me(now)

    cards._wanted = slow
    _step(cards, 2)
    assert windows == [] and cards.pending is None


def test_a_late_line_of_commentary_isnt_someone_playing_but_their_first_shot_is(tmp_path, api):
    cfg = paired_cfg(tmp_path, api)
    s = Store(cfg.db_path)
    cards, windows = _cards(cfg, api)
    _step(cards, 0)
    _end(s, _play(s, "r1"))
    _step(cards, 2)
    [shown] = windows
    # A practice record with no shot in it (where a line said after the round was filed).
    row = dict(s.db.execute("SELECT * FROM round_records LIMIT 1").fetchone()) | {
        "uuid": "late", "kind": "practice", "state": "in_progress", "ended_at": None}
    s.db.execute(f"INSERT INTO round_records ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", list(row.values()))
    s.db.commit()
    _step(cards, 4)
    assert not shown.down.is_set()
    s.db.execute("UPDATE shots SET record_uuid = 'late' WHERE id = (SELECT max(id) FROM shots)")
    s.db.commit()
    _step(cards, 6)
    assert shown.down.is_set()


def test_a_round_found_over_too_late_for_a_card_still_takes_the_last_one_down(tmp_path, api):
    cfg = paired_cfg(tmp_path, api)
    s = Store(cfg.db_path)
    cards, windows = _cards(cfg, api)
    _step(cards, 0)
    _end(s, _play(s, "r1"))
    _step(cards, 2)
    [shown] = windows
    _end(s, _play(s, "r2", start=50))        # (played and ended while the file went unread)
    _step(cards, 4 + cloud_cards.STALE_S)
    assert shown.down.is_set() and len(windows) == 1


def test_a_round_found_over_too_late_also_cancels_a_card_still_waiting_to_go_up(tmp_path, api):
    cfg = paired_cfg(tmp_path, api)
    s = Store(cfg.db_path)
    cards, windows = _cards(cfg, api)
    _step(cards, 0)
    api.minimum = "9.9.9"                    # (the cloud can't be asked: the first round's card waits)
    first = _end(s, _play(s, "r1")) or "r1"
    _step(cards, 2)
    assert cards.pending == (first, 2)
    _end(s, _play(s, "r2", start=50))        # (another group played and finished while the file went unread)
    api.minimum = None
    _step(cards, 4 + cloud_cards.ASK_S)
    assert windows == [] and cards.pending is None


def test_a_late_line_filed_while_the_cloud_is_asked_doesnt_cost_the_round_its_card(tmp_path, api):
    cfg = paired_cfg(tmp_path, api)
    s = Store(cfg.db_path)
    cards, windows = _cards(cfg, api)
    _step(cards, 0)
    u = _end(s, _play(s, "r1")) or "r1"
    me = cards._wanted

    def slow(now):   # (a practice record with no shot appears meanwhile: nobody played)
        row = dict(s.db.execute("SELECT * FROM round_records LIMIT 1").fetchone()) | {
            "uuid": "late", "kind": "practice", "state": "in_progress", "ended_at": None}
        s.db.execute(f"INSERT INTO round_records ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                     list(row.values()))
        s.db.commit()
        return me(now)

    cards._wanted = slow
    _step(cards, 2)
    assert [w.uuid for w in windows] == [u]


def test_a_window_that_went_away_by_itself_is_seen_as_down(tmp_path, api):
    cfg = paired_cfg(tmp_path, api)
    s = Store(cfg.db_path)
    cards, windows = _cards(cfg, api)
    _step(cards, 0)
    _end(s, _play(s, "r1"))
    _step(cards, 2)
    windows[0].closed.set()                  # (as Window._run does on its way out, however it went)
    _step(cards, 4)
    assert cards.shown is None


def test_a_booth_that_is_stopping_puts_no_cards_up(tmp_path, api):
    cfg = paired_cfg(tmp_path, api)
    s = Store(cfg.db_path)
    cards, windows = _cards(cfg, api)
    _step(cards, 0)
    _end(s, _play(s, "r1"))
    cards.stopped.set()      # (as run() does on its way out, with a look still in its thread)
    cards.look(2)
    assert windows == [] and cards.shown is None


def test_a_round_over_before_the_booth_started_gets_no_cards_and_one_that_ends_now_does(tmp_path, api):
    cfg = paired_cfg(tmp_path, api)
    s = Store(cfg.db_path)
    _end(s, _play(s, "old"))
    cards, windows = _cards(cfg, api)
    _step(cards, 0)
    assert windows == []
    u = _play(s, "r1", start=50)
    _step(cards, 2)
    assert windows == []                      # in progress: nothing yet
    _end(s, u)
    _step(cards, 4)
    [shown] = windows
    assert shown.uuid == u and sorted(c.name[:3] for c in shown.cards) == ["Ann", "Bob"]
    assert sorted(c.name[3:] for c in shown.cards) == [" (1)", " (2)"]
    db = cloud_rounds.connect(cfg.db_path)
    codes = cloud_rounds.claim_codes(db, u)
    assert sorted(_linked(shown.url)) == sorted((c.name, codes[c.slot_id]) for c in shown.cards)   # one link, both players
    assert sorted(c.code for c in shown.cards) == sorted(codes.values())
    db.close()
    _step(cards, 6)
    assert len(windows) == 1 and not shown.down.is_set()   # still the same cards


def test_the_cards_show_who_has_claimed_once_the_round_is_there(tmp_path, api):
    cfg = paired_cfg(tmp_path, api)
    s = Store(cfg.db_path)
    cards, windows = _cards(cfg, api)
    _step(cards, 0)
    u = _play(s, "r1")
    _end(s, u)
    _step(cards, 2)
    [shown] = windows
    assert [c.claimed for c in shown.cards] == [False, False]   # (the round isn't there yet: 404, and it waits)
    _send(cfg)
    first = shown.cards[0].slot_id
    api.claimed[(u, first)] = "Ann R."
    _step(cards, 3)                                               # (asked every POLL_S, not every look)
    assert [c.claimed for c in shown.cards] == [False, False]
    _step(cards, 2 + cloud_cards.POLL_S)
    assert [(c.claimed, c.by) for c in shown.snapshot()] == [(True, "Ann R."), (False, None)]
    del api.claimed[(u, first)]                                   # staff undid it: the card goes back
    _step(cards, 2 + 2 * cloud_cards.POLL_S)
    assert [(c.claimed, c.by) for c in shown.snapshot()] == [(False, None), (False, None)]
    assert not shown.down.is_set()
    # The sent body's hashes are the shown codes': what's scanned is what the cloud was told.
    assert {sl["claim_hash"] for sl in api.puts[0][2]["slots"]} == {
        cloud_rounds.claim_hash(code) for _, code in _linked(shown.url)}


def test_the_cards_come_down_a_while_after_everyone_has_claimed(tmp_path, api):
    cfg = paired_cfg(tmp_path, api)
    s = Store(cfg.db_path)
    cards, windows = _cards(cfg, api)
    _step(cards, 0)
    u = _play(s, "r1")
    _end(s, u)
    _step(cards, 2)
    [shown] = windows
    _send(cfg)
    for c in shown.cards:
        api.claimed[(u, c.slot_id)] = "Someone"
    _step(cards, 10)
    assert shown.all_claimed_at == 10 and not shown.down.is_set()
    _step(cards, 10 + cloud_cards.CLAIMED_S - 1)
    assert not shown.down.is_set()
    _step(cards, 10 + cloud_cards.CLAIMED_S)
    assert shown.down.is_set() and cards.shown is None


@pytest.mark.parametrize("why", ["closed", "too long", "next round"])
def test_the_cards_come_down_when_closed_after_a_while_or_when_a_round_is_played_again(tmp_path, api, why):
    cfg = paired_cfg(tmp_path, api)
    s = Store(cfg.db_path)
    cards, windows = _cards(cfg, api)
    _step(cards, 0)
    u = _play(s, "r1")
    _end(s, u)
    _step(cards, 2)
    [shown] = windows
    at = 4
    if why == "closed":
        shown.closed.set()
    elif why == "too long":
        _step(cards, 2 + cloud_cards.SHOW_S - 1)
        assert not shown.down.is_set()
        at = 2 + cloud_cards.SHOW_S
    else:
        _play(s, "r2", start=50)
    _step(cards, at)
    assert shown.down.is_set() and cards.shown is None and len(windows) == 1
    api.calls.clear()
    _step(cards, at + 100)
    assert not [c for c in api.calls if c[2].endswith("/claims")]   # nothing more is asked about it


def test_the_next_rounds_cards_replace_the_last(tmp_path, api):
    cfg = paired_cfg(tmp_path, api)
    s = Store(cfg.db_path)
    cards, windows = _cards(cfg, api)
    _step(cards, 0)
    first = _play(s, "r1")
    _end(s, first)
    _step(cards, 2)
    second = _play(s, "r2", players=("Cat",), start=50)
    _step(cards, 4)
    _end(s, second)
    _step(cards, 6)
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
    cloud_cards.apply(shown, [{"slot_id": "b", "claimed": True, "name": " Mi\ud800ke\x00 R.\n"}])
    assert shown.cards[1].by == "Mike R."   # (only what can be drawn)
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
