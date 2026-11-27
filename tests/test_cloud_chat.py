"""The cloud's live chat, booth side (commentator/cloud_chat.py): what members watching this bay's
stream say, through the calls the booth makes of the site stack's chat table."""
from types import SimpleNamespace

import pytest

from commentator.chat import cursor_at
from commentator.cloud_chat import CloudChat, message_id, number_of


class FakeCloud:
    def __init__(self):
        self.messages, self.aired, self.asked = [], [], []

    def request(self, method, path, body=None, params=None, **kw):
        self.asked.append((method, path, params))
        if method == "GET":
            after = (params or {}).get("after", 0)
            rows = [m for m in self.messages if m["id"] > after][:50]
            return SimpleNamespace(status_code=200, json=lambda: {"messages": rows})
        self.aired += body["ids"]
        return SimpleNamespace(status_code=204, json=lambda: None)


def _stream(cloud, sid="s1"):
    return SimpleNamespace(stream_id=sid, cloud_of=lambda: cloud)


def _msg(n, at, name="Kim", text="Nice shot"):
    return {"id": n, "name": name, "text": text, "at": at, "stream_at": None}


def test_the_booth_reads_its_streams_chat_after_its_cursor_and_marks_what_it_aired():
    cloud = FakeCloud()
    chat = CloudChat(_stream(cloud))
    chat.set_on_air(True)   # (nothing to say: the cloud's chat is open while the stream's live)
    cloud.messages = [_msg(1, "2026-09-21T19:00:00Z"), _msg(2, "2026-09-21T19:00:05+00:00", "Pat", "Go!")]
    got = chat.since(cursor_at(0))
    assert [(m.name, m.text, m.at) for m in got] == [("Kim", "Nice shot", 1790017200.0), ("Pat", "Go!", 1790017205.0)]
    assert got[0].id < got[1].id and number_of(got[1].id) == 2
    # A cursor at a time passes what was said before it, as the table's do.
    assert [m.text for m in chat.since(cursor_at(1790017203.0))] == ["Go!"]
    # Read on from the last it had (asked for after 2, not from the start).
    cloud.messages.append(_msg(3, "2026-09-21T19:00:09Z", text="Again"))
    assert [m.text for m in chat.since(got[1].id)] == ["Again"]
    assert cloud.asked[-1] == ("GET", "/api/streams/s1/chat", {"after": 2})
    chat.mark_aired(got[0].id)
    assert cloud.aired == [1]


def test_no_stream_no_chat_a_new_stream_its_own_and_a_failed_read_raises():
    cloud = FakeCloud()
    stream = _stream(cloud, None)
    chat = CloudChat(stream)
    assert chat.since(cursor_at(0)) == [] and cloud.asked == []
    stream.stream_id = "s1"
    cloud.messages = [_msg(5, "2026-09-21T19:00:00Z")]
    assert len(chat.since(cursor_at(0))) == 1
    stream.stream_id = "s2"   # (OBS restarted: a new stream, read from its start)
    cloud.messages = [_msg(6, "2026-09-21T19:10:00Z", text="New")]
    assert [m.text for m in chat.since(cursor_at(0))] == ["New"]
    assert cloud.asked[-1][2] == {"after": 0}
    cloud.request = lambda *a, **kw: SimpleNamespace(status_code=503, json=lambda: None)
    with pytest.raises(RuntimeError):
        chat.since(cursor_at(0))
    assert message_id(1.5, 255) == "0000000001500-0000000000ff"


def test_messages_whose_times_run_against_their_order_are_read_once_in_order():
    cloud = FakeCloud()
    chat = CloudChat(_stream(cloud))
    # (two posts at once: the later row stamped a few ms earlier)
    cloud.messages = [_msg(10, "2026-09-21T19:00:00.020Z", text="B"), _msg(11, "2026-09-21T19:00:00.005Z", text="A")]
    got = chat.since(cursor_at(0))
    assert [m.text for m in got] == ["B", "A"] and got[0].id < got[1].id
    assert chat.since(got[-1].id) == []   # (the booth's cursor past both: neither again)


def test_a_messages_age_is_on_the_clouds_clock_and_names_are_tidied():
    cloud = FakeCloud()
    get = cloud.request

    def dated(method, path, **kw):
        r = get(method, path, **kw)
        r.headers = {"date": "Mon, 21 Sep 2026 19:00:30 GMT"}
        return r
    cloud.request = dated
    cloud.messages = [_msg(1, "2026-09-21T19:00:00Z", name="  Kim\n  Roster " + "x" * 60)]
    [m] = CloudChat(_stream(cloud)).since(cursor_at(0))
    assert m.age == 30.0
    assert m.name == ("Kim Roster " + "x" * 60)[:40]


def test_a_paired_booths_chat_model_that_cant_be_built_turns_the_chat_off_not_the_booth(monkeypatch):
    from commentator import cloud
    from commentator import __main__ as main
    from commentator.config import Config

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = Config()
    cfg.chat.enabled = True
    monkeypatch.setattr(cloud, "startup", SimpleNamespace(problem=None, rec={}, bound=()))
    assert main._chat_llm(cfg) is None and cfg.chat.enabled is False
    # Unpaired, as before: the owner turned the chat on, and its model is required.
    cfg.chat.enabled = True
    monkeypatch.setattr(cloud, "startup", None)
    with pytest.raises(Exception):
        main._chat_llm(cfg)
    # With its key, it's built, paired or not (0.1.169 named build_llm without importing it: no commentary).
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    assert main._chat_llm(cfg) is not None
    monkeypatch.setattr(cloud, "startup", SimpleNamespace(problem=None, rec={}, bound=()))
    assert main._chat_llm(cfg) is not None and cfg.chat.enabled is True
