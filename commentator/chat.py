"""The stats page's live chat, booth side (docs/tracking.md#live-chat).

Viewers post to the site stack's chat function (tools/aws/stats-site.yaml, Chat), which keeps their
messages in a DynamoDB table for a few hours. The booth reads them from the same table, with the publish
keys: it heartbeats whether it's on air (the page shows the chat, and the function takes posts, only
while it is), reads the messages since the last it saw, and marks the ones it answered on air.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class ChatMessage:
    id: str        # the table's sort key: "<ms since epoch, 13 digits>-<6 hex>", in posting order
    name: str
    text: str
    at: float      # epoch seconds
    age: float | None = None   # seconds old when read, on the table's own clock (None: unknown)
    # What the viewer was watching as they sent it (the page says, when its player can): the stream
    # session, and its segment and how far into it, or how far behind the live edge it was.
    sess: str | None = None
    seg: str | None = None
    off: float | None = None
    behind: float | None = None
    watched: float | None = None   # the booth's: the moment on their screen (the PC's clock)
    shot: int | None = None    # the booth's: the shot they were watching (shots.id), once it's worked out
    upcoming: str | None = None   # the booth's: who was over the ball on their screen after that shot


def cursor_at(epoch: float) -> str:
    """A sort key just before any message posted at or after `epoch`."""
    return f"{int(epoch * 1000):013d}-"


class ChatTable:
    """The chat's table, through boto3 (blocking: call it off the event loop)."""

    def __init__(self, table: str, region: str | None = None, client=None):
        self.table, self.region, self._client = table, region, client

    def _db(self):
        if self._client is None:
            from . import backend

            self._client = backend.dynamodb(self.region)
        return self._client

    def set_on_air(self, on: bool, since: str | None = None) -> None:
        """`since`: the round's chat opened there (a cursor_at key); the page shows only what came after."""
        item = {"pk": {"S": "booth"}, "sk": {"S": "state"}, "on": {"BOOL": on}, "at": {"N": str(int(time.time()))}}
        if since:
            item["since"] = {"S": since}
        self._db().put_item(TableName=self.table, Item=item)

    def since(self, after: str, limit: int = 50) -> list[ChatMessage]:
        """Messages posted after the sort key `after`, oldest first; the newest `limit` of them if more."""
        resp = self._db().query(
            TableName=self.table, KeyConditionExpression="pk = :p AND sk > :s",
            ExpressionAttributeValues={":p": {"S": "msg"}, ":s": {"S": after}},
            ScanIndexForward=False, Limit=limit,
        )
        server_now = _server_time(resp)
        out = []
        for row in reversed(resp.get("Items", [])):
            try:
                sk = row["sk"]["S"]
                at = int(sk.split("-")[0]) / 1000
                age = None if server_now is None else max(0.0, server_now - at)
                num = lambda k: float(row[k]["N"]) if k in row else None  # noqa: E731
                out.append(ChatMessage(sk, row["name"]["S"], row["text"]["S"], at, age,
                                       row.get("sess", {}).get("S"), row.get("seg", {}).get("S"),
                                       num("off"), num("behind")))
            except (KeyError, ValueError):
                log.debug("Chat: skipped a malformed row %r", row)
        return out

    def mark_aired(self, message_id: str) -> None:
        self._db().update_item(
            TableName=self.table, Key={"pk": {"S": "msg"}, "sk": {"S": message_id}},
            UpdateExpression="SET aired = :t", ConditionExpression="attribute_exists(pk)",
            ExpressionAttributeValues={":t": {"BOOL": True}},
        )

    # ---- alerts (push.py): the page's subscriptions, which the chat function keeps here ----------

    def set_push_key(self, public: str, topics: list[str] | None = None) -> None:
        """The booth's public push key, which the page subscribes with (GET /chat/push), and the kinds
        of alert it sends (`topics`), the only ones the page offers."""
        item = {"pk": {"S": "push"}, "sk": {"S": "key"}, "public": {"S": public}}
        if topics:
            item["topics"] = {"SS": list(topics)}
        self._db().put_item(TableName=self.table, Item=item)

    def push_subscriptions(self) -> list:
        from .push import Subscription

        out, start = [], None
        while True:
            args = {"TableName": self.table, "KeyConditionExpression": "pk = :p AND begins_with(sk, :s)",
                    "ExpressionAttributeValues": {":p": {"S": "push"}, ":s": {"S": "sub#"}}}
            if start:
                args["ExclusiveStartKey"] = start
            resp = self._db().query(**args)
            for row in resp.get("Items", []):
                try:
                    out.append(Subscription(row["sk"]["S"], row["endpoint"]["S"], row["p256dh"]["S"],
                                            row["auth"]["S"], frozenset(row.get("topics", {}).get("SS", []))))
                except KeyError:
                    log.debug("Alerts: skipped a malformed subscription %r", row.get("sk"))
            start = resp.get("LastEvaluatedKey")
            if not start:
                return out

    def drop_push_subscription(self, sub_id: str) -> None:
        self._db().delete_item(TableName=self.table, Key={"pk": {"S": "push"}, "sk": {"S": sub_id}})


def _server_time(resp) -> float | None:
    """AWS's clock when it answered (its Date header, to the second): the messages' stamps are on it,
    the PC's may not be."""
    try:
        from email.utils import parsedate_to_datetime

        return parsedate_to_datetime(resp["ResponseMetadata"]["HTTPHeaders"]["date"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return None


# Always kept off the air, whatever the moderation: a link or an ad has nothing for the booth.
_LINK = re.compile(r"https?://|www\.|\b[\w-]+\.(?:com|net|org|biz|info|io|co|xyz|ru|shop|store|link|ly|gg|tv|me)\b",
                   re.IGNORECASE)
# Swearing, starred out or not: kept off the air under standard and strict moderation.
# Whole words only, so a name or a word that starts the same ("Dickinson", "shitake") isn't caught.
_SWEAR = re.compile(r"\b(?:f[\W_]*[u*@][\W_]*[c*][\W_]*k(?:s|ed|er|ers|in|ing)?|f\*{2,}(?:ing|ed|er|in)?|motherf\w*|"
                    r"sh[i*!1]t(?:s|ty|ter|head|heads|show)?|c[u*]nts?|b[i*!1]tch(?:es|y|ing)?|ass[\W_]*holes?|"
                    r"dickheads?|puss(?:y|ies)(?![\W_]*willow)|bastards?|wank(?:s|er|ers|ing)?|twats?)(?![\w*])",
                    re.IGNORECASE)


def screened_out(text: str, moderation: str) -> str | None:
    """Why a message can't go to the booth at this moderation level, or None if it can. The model
    applies the rest of the level (abuse, put-downs) to what's left."""
    if _LINK.search(text):
        return "a link"
    if moderation != "off" and _SWEAR.search(text):
        return "swearing"
    return None


# What tells a listener the booth has turned to the chat, rather than to something said on the course.
_LEAD_IN = re.compile(r"\b(?:chat|chatroom|viewers?|stream|inbox|messages? (?:in|from)|"
                      r"(?:folks|people|those|everyone|friends) (?:at home|watching|tuning in)|"
                      r"watching (?:at home|along|on the stream|online))\b", re.IGNORECASE)
# A sentence ends at . ! or ? (and any closing quote) and a space - not inside "Mr. Smith" or "1.5".
_SENTENCE_END = re.compile(r"(?<!\bMr\.)(?<!\bMrs\.)(?<!\bMs\.)(?<!\bDr\.)(?<!\bSt\.)(?<=[.!?])[\"'\u201d\u2019)]*\s+")


def chat_lead_in_problems(lines) -> list[str]:
    """A take on the chat must open by turning to it: two lines out of nowhere sound like someone on
    the course said it."""
    if lines and not _LEAD_IN.search(_SENTENCE_END.split(lines[0].text, maxsplit=1)[0]):
        return ["the first line has to open by turning to the live chat (say it's the chat, or viewers, "
                "in its first sentence) before getting into what they're saying"]
    return []


# The booth's map read lists the trouble it's sure of, not all there is: a take mustn't tell a viewer
# their warning is about trouble that isn't there ("Dave's watching the wrong side of the hole").
_TROUBLE = r"(?:water|ob|o\.b\.|out[- ]of[- ]bounds|bunkers?|sand|hazards?|trouble|penalty areas?|red stakes|white stakes)"
_DENIAL = re.compile(
    # Someone watching or worried about the wrong side, not a ball that missed on the wrong side.
    r"\b(?:watching|looking at|looking|worried about|eyeing|eyes on|on about|got|has) the wrong "
    r"(?:side|hazard|trouble|hole)\b|"
    # "No trouble clearing it" and "no trouble for Frankie" are about the shot, not the hole; only
    # modifiers may come between, so "no escaping the water" or "no shortage of water" agree it's there.
    rf"\b(?:no|isn't any|aren't any|there's no|there is no|nothing like)\s+"
    r"(?:(?:real|actual|big|serious|much|more|other|such|extra|hidden|greenside|fairway|left|right|"
    rf"left-side|right-side)\s+){{0,2}}{_TROUBLE}(?!\w)"
    r"(?!(?<=trouble)\s+(?:\w+ing|for|with)\b)|"
    # Within the sentence: a period inside "O.B." doesn't end it.
    rf"\b{_TROUBLE}(?!\w)(?:[^.!?]|\.(?=\S)){{0,40}}?\b(?:isn't there|is not there|aren't there|doesn't exist|isn't in play|"
    r"not in play|doesn't come into play|nowhere to be (?:seen|found))\b", re.IGNORECASE)


def chat_hazard_denial_problems(lines) -> list[str]:
    """A take that says trouble a viewer warned about isn't on the hole: the booth can't know that."""
    if any(_DENIAL.search(line.text) for line in lines):
        return ["don't say trouble a viewer mentioned isn't there or that they're watching the wrong side: the "
                "booth's map only lists what it's sure of, and there may be more on the hole"]
    return []
