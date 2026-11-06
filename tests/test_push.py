"""Phone alerts: web push encryption and signing (push.py), and what the booth alerts (alerts.py)."""
import json

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from commentator import alerts, push


def test_encryption_matches_the_rfc_8291_example():
    as_private = ec.derive_private_key(
        int.from_bytes(push.unb64u("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"), ec.SECP256R1())
    out = push.encrypt(push.unb64u("V2hlbiBJIGdyb3cgdXAsIEkgd2FudCB0byBiZSBhIHdhdGVybWVsb24"),
                       "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
                       "BTBZMqHH6r4Tts7J_aSIgg", as_private, push.unb64u("DGv6ra1nlYgDCS1FRnbzlw"))
    header = push.unb64u("DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIg"
                         "Dll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8")
    body = push.unb64u("8pfeW0KbunFT06SuDKoJH9Ql87S1QUrdirN6GcG7sFz1y1sqLgVi1VhjVkHsUoEsbI_0LpXMuGvnzQ")
    assert out == header + body


def test_the_key_is_kept_and_the_vapid_token_verifies(tmp_path):
    key = push.load_key(tmp_path)
    assert push.public_key(push.load_key(tmp_path)) == push.public_key(key)   # made once
    auth = push.vapid(key, "https://web.push.apple.com/abc", now=1000)
    token, k = auth.removeprefix("vapid t=").split(", k=")
    assert k == push.public_key(key)
    head, claims, sig = token.split(".")
    assert json.loads(push.unb64u(claims)) == {"aud": "https://web.push.apple.com", "exp": 1000 + 12 * 3600,
                                               "sub": push.CONTACT}
    raw = push.unb64u(sig)
    key.public_key().verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
                            f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))


class FakeHttp:
    def __init__(self, status):
        self.status, self.posts = status, []

    def post(self, url, content, headers):
        self.posts.append((url, headers))
        return type("R", (), {"status_code": self.status.get(url, 201), "text": ""})()


def _sub(name, topics):
    browser = ec.generate_private_key(ec.SECP256R1())
    return push.Subscription(f"sub#{name}", f"https://fcm.googleapis.com/{name}", push.b64u(push._point(browser.public_key())),
                             push.b64u(b"0123456789abcdef"), frozenset(topics))


def test_send_goes_only_to_phones_that_asked_and_reports_the_gone_ones(tmp_path):
    subs = [_sub("a", {"round"}), _sub("b", {"live"}), _sub("c", {"round", "live"})]
    http = FakeHttp({"https://fcm.googleapis.com/c": 410})
    sent, gone = push.send(push.Alert("round", "t", "b", "#x"), subs, push.load_key(tmp_path), client=http)
    assert (sent, gone) == (1, ["sub#c"])
    assert [u for u, _ in http.posts] == ["https://fcm.googleapis.com/a", "https://fcm.googleapis.com/c"]
    assert http.posts[0][1]["Content-Encoding"] == "aes128gcm"


def _row(rnd, ended, side, gross, to_par, card=(), league=True):
    return {"id": rnd, "round": rnd, "ended": ended, "course": "Sheep Ranch", "side": side, "players": [side],
            "league": league, "tournament": False, "gross": gross, "to_par": to_par,
            "card": [{"hole": h, "par": p, "score": s} for h, p, s in card]}


def _records(drive):
    return {"records": {"all": {"longest_drive": drive, "longest_putt": None, "approaches": []}}}


def _check(rows, state, now, drive=None, anywhere=None):
    """diff() on a build: its rounds, the league's longest drive and the longest anywhere (practice too)."""
    return alerts.diff({"rounds": rows, **_records(anywhere or drive)}, state, now=now, league=_records(drive))


def test_the_first_check_alerts_nothing_and_later_ones_only_whats_new():
    old = [_row(1, 100, "Ann", 36, 0, [(1, 4, 2)])]
    drive = {"id": 5, "ts": 90, "player": "Ann", "v": 300.4, "course": "Sheep Ranch", "hole": 1}
    sent, state = _check(old, {}, 200, drive)
    assert sent == []                                     # what was there before alerts were set up
    rows = old + [_row(2, 5000, "Ann", 34, -2, [(3, 3, 1)]), _row(2, 5000, "Bob", 38, 2),
                  _row(3, 5000, "Cal", 40, 4, [(5, 5, 3)], league=False)]
    big = {"id": 9, "ts": 4900, "player": "Bob", "v": 321.0, "course": "Sheep Ranch", "hole": 4}
    sent, state = _check(rows, state, 5100, big)
    got = [(a.topic, a.title, a.body) for a in sent]
    assert ("round", "⛳ League round posted: Sheep Ranch", "Ann 34 (-2) · Bob 38 (+2)") in got
    assert ("record", "🎯 Hole in one! Ann", "Hole 3 (par 3) at Sheep Ranch") in got
    assert ("record", "🦅 Eagle! Cal", "Hole 5 (par 5) at Sheep Ranch") in got      # practice round: no "posted"
    assert ("record", "🏆 New league record: Longest drive", "Bob: 321 yd, Sheep Ranch, hole 4 (was 300 yd)") in got
    assert len(got) == 4
    assert _check(rows, state, 5200, big)[0] == []   # nothing twice


def test_a_phone_can_hear_only_when_a_league_or_tournament_round_goes_live(tmp_path, monkeypatch):
    a = alerts.Alerts(tmp_path, table=None)
    sent = []
    monkeypatch.setattr(a, "send", lambda got: sent.append([x.topic for x in got]))
    clock = [1e9]
    monkeypatch.setattr(alerts.time, "time", lambda: clock[0])
    a.live("Ann", "Sheep Ranch")                 # practice
    a.live("Ann", "Sheep Ranch", league=True)   # league, an hour on: the any-round phones already heard
    clock[0] += 3600
    a.live("Ann", "Sheep Ranch", league=True)
    a.live("Ann", "Sheep Ranch", league=True)
    clock[0] += alerts.LIVE_GAP
    a.live("Ann", "Sheep Ranch", league=True)
    assert sent == [["live"], ["liveleague"], ["live", "liveleague"]]


def test_only_league_shots_are_league_records():
    rows = [_row(1, 100, "Ann", 36, 0)]
    league = {"id": 5, "ts": 90, "player": "Ann", "v": 280.0}
    _, state = _check(rows, {}, 200, league)
    practice = {"id": 6, "ts": 4900, "player": "Bob", "v": 350.0}
    assert _check(rows, state, 5000, league, anywhere=practice)[0] == []
    better = {"id": 7, "ts": 5100, "player": "Cal", "v": 300.0}
    sent, _ = _check(rows, state, 5200, better, anywhere=practice)
    assert [(a.title, a.body, a.url) for a in sent] == [
        ("🏆 New league record: Longest drive", "Cal: 300 yd (was 280 yd)", "#highlights?player=&ctx=league&period=all")]
    _, state = _check(rows, state, 5200, better)
    assert _check(rows, state, 5300, {**league, "ts": 5250})[0] == []   # the 300 taken out: 280 isn't new


def test_an_old_round_or_record_coming_back_isnt_news():
    rows = [_row(1, 100, "Ann", 36, 0)]
    _, state = _check(rows, {}, 200)
    _, state = _check(rows, state, 90000)
    back = rows + [_row(7, 150, "Bob", 30, -6, [(2, 4, 2)])]   # put back in the stats, played long ago
    old_drive = {"id": 3, "ts": 120, "player": "Bob", "v": 310.0}
    assert _check(back, state, 90100, old_drive)[0] == []
