"""The stats page's live chat, booth side (docs/tracking.md#live-chat).

Viewers post to the site stack's chat function (tools/aws/stats-site.yaml, Chat), which keeps their
messages in a DynamoDB table for a few hours. The booth reads them from the same table, with the publish
keys: it heartbeats whether it's on air (the page shows the chat, and the function takes posts, only
while it is), reads the messages since the last it saw, and marks the ones it answered on air.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class ChatMessage:
    id: str        # the table's sort key: "<ms since epoch, 13 digits>-<6 hex>", in posting order
    name: str
    text: str
    at: float      # epoch seconds


def cursor_at(epoch: float) -> str:
    """A sort key just before any message posted at or after `epoch`."""
    return f"{int(epoch * 1000):013d}-"


class ChatTable:
    """The chat's table, through boto3 (blocking: call it off the event loop)."""

    def __init__(self, table: str, region: str | None = None, client=None):
        self.table, self.region, self._client = table, region, client

    def _db(self):
        if self._client is None:
            import boto3

            self._client = boto3.client("dynamodb", region_name=self.region) if self.region else boto3.client("dynamodb")
        return self._client

    def set_on_air(self, on: bool) -> None:
        self._db().put_item(TableName=self.table, Item={
            "pk": {"S": "booth"}, "sk": {"S": "state"}, "on": {"BOOL": on}, "at": {"N": str(int(time.time()))},
        })

    def since(self, after: str, limit: int = 50) -> list[ChatMessage]:
        """Messages posted after the sort key `after`, oldest first; the newest `limit` of them if more."""
        rows = self._db().query(
            TableName=self.table, KeyConditionExpression="pk = :p AND sk > :s",
            ExpressionAttributeValues={":p": {"S": "msg"}, ":s": {"S": after}},
            ScanIndexForward=False, Limit=limit,
        ).get("Items", [])
        out = []
        for row in reversed(rows):
            try:
                sk = row["sk"]["S"]
                out.append(ChatMessage(sk, row["name"]["S"], row["text"]["S"], int(sk.split("-")[0]) / 1000))
            except (KeyError, ValueError):
                log.debug("Chat: skipped a malformed row %r", row)
        return out

    def mark_aired(self, message_id: str) -> None:
        self._db().update_item(
            TableName=self.table, Key={"pk": {"S": "msg"}, "sk": {"S": message_id}},
            UpdateExpression="SET aired = :t", ConditionExpression="attribute_exists(pk)",
            ExpressionAttributeValues={":t": {"BOOL": True}},
        )
