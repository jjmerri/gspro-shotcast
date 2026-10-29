"""Record shots kept for good. A stream is kept for a week (live_stream.py), and a shot's Watch button
plays it from there; the shots holding a record (the season's and each player's, for every range on
the Records tab) are copied out of their stream into a clip of their own, which stays as long as
the shot holds a record. Run after each publish, with the shots that build found:

    live/clips.json                {"clips": {"<shot id>": {"at": seconds into the clip, "duration",
                                   "player", "course", "hole"}}}
    live/clips/<shot id>/clip.m3u8 the clip's playlist, and its segments, copied from live/s/<session>/

A shot whose stream is gone (over a week old, or never streamed) gets no clip; one whose stream hasn't
reached the end of its clip yet (still uploading) gets it on the next publish.
"""
from __future__ import annotations

import json
import time
from typing import Any

from .live_stream import TYPES, segment_durations, vod_playlist

PRE, POST = 3.0, 20.0   # seconds of the stream kept around the shot (it shows ~4 s after its ts)
WATCH_LEAD = 1.0        # the page's stream links start a second before a shot: the same here


def _get(client: Any, bucket: str, key: str) -> bytes | None:
    try:
        return client.get_object(Bucket=bucket, Key=key)["Body"].read()
    except client.exceptions.NoSuchKey:
        return None


def _segments(text: str) -> tuple[list[tuple[str, float]], bool]:
    """A playlist's segments in order, with their durations, and whether it's closed."""
    return list(segment_durations(text).items()), "#EXT-X-ENDLIST" in text


def sync(client: Any, bucket: str, prefix: str, shots: dict[int, dict], clock=time.time) -> dict[str, int]:
    """Clip each of `shots` (id -> its record: ts, player, course, hole) that has no clip yet and is
    still in a kept stream, and delete the clips of shots no longer holding a record."""
    root = (prefix.strip("/") + "/" if prefix.strip("/") else "") + "live/"
    raw = _get(client, bucket, root + "recordings.json")
    recordings = [r for r in (json.loads(raw).get("recordings") or [] if raw else []) if isinstance(r, dict)]
    saved = _get(client, bucket, root + "clips.json")
    index = {k: v for k, v in ((json.loads(saved).get("clips") or {}) if saved else {}).items() if isinstance(v, dict)}
    wanted = {str(k): v for k, v in shots.items()}
    now, playlists, added = clock(), {}, 0
    for key, shot in wanted.items():
        ts = shot.get("ts")
        if key in index or not ts:
            continue
        rec = next((r for r in recordings if r.get("session") and r.get("since") is not None
                    and r["since"] <= ts <= (r.get("ended") or now) + 1), None)
        if rec is None:
            continue
        session = rec["session"]
        if session not in playlists:
            text = _get(client, bucket, f"{root}s/{session}/full.m3u8")
            playlists[session] = _segments(text.decode("utf-8", "replace")) if text else ([], False)
        track, ended = playlists[session]
        t, start, first, pick = ts - rec["since"], 0.0, None, []
        for name, dur in track:
            if start + dur > t - PRE and start < t + POST:
                first = start if first is None else first
                pick.append((name, dur))
            start += dur
        if not pick or (start < t + POST and not ended):
            continue   # not streamed yet, or not all of it: the next publish
        for name, _ in pick:
            client.copy_object(Bucket=bucket, Key=f"{root}clips/{key}/{name}",
                               CopySource={"Bucket": bucket, "Key": f"{root}s/{session}/{name}"},
                               MetadataDirective="REPLACE", CacheControl="public, max-age=31536000, immutable",
                               ContentType=TYPES.get("." + name.rsplit(".", 1)[-1], "application/octet-stream"))
        client.put_object(Bucket=bucket, Key=f"{root}clips/{key}/clip.m3u8", Body=vod_playlist(pick, ended=True),
                          CacheControl="public, max-age=31536000, immutable", ContentType=TYPES[".m3u8"])
        index[key] = {"at": round(max(0.0, t - first - WATCH_LEAD), 1), "duration": round(sum(d for _, d in pick), 1),
                      **{k: shot.get(k) for k in ("player", "course", "hole")}}
        added += 1
    gone = [k for k in index if k not in wanted]
    for k in gone:
        del index[k]
    stale = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=root + "clips/"):
        stale += [o["Key"] for o in page.get("Contents", []) if o["Key"][len(root) + 6:].split("/", 1)[0] not in index]
    if added or gone or saved is None:
        client.put_object(Bucket=bucket, Key=root + "clips.json", Body=json.dumps({"clips": index}).encode(),
                          CacheControl="no-cache", ContentType=TYPES[".json"])
    for i in range(0, len(stale), 1000):
        client.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in stale[i:i + 1000]]})
    return {"kept": len(index), "added": added, "removed": len(gone)}
