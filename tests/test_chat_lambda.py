"""The live chat's function (tools/aws/stats-site.yaml, Chat), run from the template itself against
an in-memory table."""
import itertools
import json
import re
import sys
import textwrap
import types
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).resolve().parents[1] / "tools" / "aws" / "stats-site.yaml"
SECRET = "stack-uuid"
NOW = 1_700_000_003.25   # the clock, early in a rate window, so none turns over mid-test


class ConditionFailed(Exception):
    pass


class FakeTable:
    def __init__(self):
        self.items: dict[tuple[str, str], dict] = {}
        self.exceptions = types.SimpleNamespace(ConditionalCheckFailedException=ConditionFailed)

    def get_item(self, TableName, Key):
        item = self.items.get((Key["pk"]["S"], Key["sk"]["S"]))
        return {"Item": item} if item else {}

    def put_item(self, TableName, Item):
        self.items[(Item["pk"]["S"], Item["sk"]["S"])] = Item

    def update_item(self, TableName, Key, ExpressionAttributeValues, **_):
        k = (Key["pk"]["S"], Key["sk"]["S"])
        item = self.items.setdefault(k, dict(Key))
        n = int(item.get("n", {"N": "0"})["N"])
        if n >= int(ExpressionAttributeValues[":limit"]["N"]):
            raise ConditionFailed()
        item["n"] = {"N": str(n + 1)}

    def query(self, TableName, ExpressionAttributeValues, ScanIndexForward, Limit, **_):
        pk, after = ExpressionAttributeValues[":p"]["S"], ExpressionAttributeValues[":s"]["S"]
        rows = sorted((v for (p, s), v in self.items.items() if p == pk and s > after), key=lambda i: i["sk"]["S"],
                      reverse=not ScanIndexForward)
        return {"Items": rows[:Limit]}


@pytest.fixture
def chat(monkeypatch):
    code = re.search(r"\n  Chat:\n.*?ZipFile: \|\n(.*?)\n\n  ChatUrl:", TEMPLATE.read_text(), re.S).group(1)
    table = FakeTable()
    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda name: table))
    monkeypatch.setenv("TABLE", "chat")
    monkeypatch.setenv("ORIGIN_SECRET", SECRET)
    monkeypatch.setenv("KEEP_HOURS", "6")
    mod = types.ModuleType("chat_fn")
    exec(compile(textwrap.dedent(code), "chat_fn", "exec"), mod.__dict__)
    ticks = itertools.count()
    mod.time = types.SimpleNamespace(time=lambda: NOW + next(ticks) / 1000)   # a millisecond a read
    mod.table = table
    return mod


def call(fn, method="GET", body=None, query=None, ip="203.0.113.5", secret=SECRET, path="/chat/messages"):
    event = {"rawPath": path, "requestContext": {"http": {"method": method, "sourceIp": "10.0.0.1"}},
             "headers": {"x-chat-origin": secret, "cloudfront-viewer-address": f"{ip}:51234"},
             "queryStringParameters": query, "body": json.dumps(body) if body is not None else None}
    r = fn.handler(event, None)
    return r["statusCode"], json.loads(r["body"])


def booth(fn, on=True, ago=0, since=None):
    item = {"pk": {"S": "booth"}, "sk": {"S": "state"}, "on": {"BOOL": on}, "at": {"N": str(int(NOW) - ago)}}
    if since is not None:
        item["since"] = {"S": since}
    fn.table.put_item("chat", item)


def test_a_new_round_starts_with_an_empty_chat(chat):
    booth(chat)
    paul = call(chat, "POST", {"name": "Paul", "text": "last round"})[1]["message"]["id"]
    opened = f"{int(paul[:13]) + 1:013d}-"                     # the next round's chat opens
    booth(chat, since=opened)
    assert call(chat)[1]["messages"] == []
    call(chat, "POST", {"name": "Corey", "text": "this round"}, ip="203.0.113.9")
    assert [m["text"] for m in call(chat)[1]["messages"]] == ["this round"]
    booth(chat, since="junk")                                  # an unreadable stamp: the last few hours
    assert [m["text"] for m in call(chat)[1]["messages"]] == ["last round", "this round"]


def test_the_chat_is_hidden_and_closed_while_the_booth_is_off(chat):
    assert call(chat) == (200, {"on": False, "messages": []})
    assert call(chat, "POST", {"name": "Paul", "text": "hi"})[0] == 409
    booth(chat, ago=200)                                   # a booth that stopped heartbeating
    assert call(chat)[1]["on"] is False
    booth(chat, on=False)
    assert call(chat)[1]["on"] is False


def test_a_message_posted_on_air_is_cleaned_and_read_back_in_order(chat):
    booth(chat)
    status, out = call(chat, "POST", {"name": "  Paul\u202e  ", "text": "Nice   putt\n Corey!" + "x" * 300})
    assert status == 200 and out["message"]["name"] == "Paul"
    assert out["message"]["text"].startswith("Nice putt Corey!") and len(out["message"]["text"]) == 200
    call(chat, "POST", {"name": "Frankie", "text": "lol"}, ip="203.0.113.9")
    status, got = call(chat)
    assert status == 200 and got["on"] is True
    assert [m["name"] for m in got["messages"]] == ["Paul", "Frankie"]
    assert [m["name"] for m in call(chat, query={"after": got["messages"][0]["id"]})[1]["messages"]] == ["Frankie"]


def test_the_chat_turns_away_bad_posts_floods_and_requests_around_cloudfront(chat):
    booth(chat)
    assert call(chat, "POST", {"name": "", "text": "hi"})[0] == 400
    assert call(chat, "POST", {"name": "Paul", "text": " \n "})[0] == 400
    assert call(chat, "POST", ["not", "an", "object"])[0] == 400
    assert [call(chat, "POST", {"name": "Paul", "text": "hi"})[0] for _ in range(3)] == [200, 200, 429]
    assert call(chat, "POST", {"name": "Corey", "text": "hi"}, ip="198.51.100.7")[0] == 200   # per viewer
    assert call(chat, secret="guess")[0] == 403
    assert call(chat, path="/chat/other")[0] == 404


def test_what_the_viewer_was_watching_is_kept_for_the_booth_only_when_its_well_formed(chat):
    booth(chat)
    sent = [{"sess": "20261103-2010", "seg": "stream12.ts", "off": 2.5},
            {"sess": "20261103-2010", "behind": 9},
            {"sess": "20261103-2010", "seg": "../x.ts", "off": 1},
            {"sess": "20261103-2010", "seg": "stream12.ts", "off": float("nan")},
            {"sess": "bad session", "behind": 9}, "junk", None]
    for i, watch in enumerate(sent):
        assert call(chat, "POST", {"name": "Paul", "text": f"m{i}", "watch": watch}, ip=f"203.0.113.{i}")[0] == 200
    rows = sorted((v for (p, _), v in chat.table.items.items() if p == "msg"), key=lambda i: i["text"]["S"])
    kept = [{k: next(iter(v[k].values())) for k in ("sess", "seg", "off", "behind") if k in v} for v in rows]
    assert kept == [{"sess": "20261103-2010", "seg": "stream12.ts", "off": "2.50"},
                    {"sess": "20261103-2010", "behind": "9.00"}, {}, {}, {}, {}, {}]
    _, body = call(chat)
    assert all(set(m) == {"id", "name", "text", "at", "aired"} for m in body["messages"])   # not shown to viewers
