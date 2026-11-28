"""A league week's recap on the stats page: once a week is final, the booth's two voices run through
how it went (each team's result, the standings) over a few of each team's moments from its round's
highlight reel (highlights.py).

The video is the reels' own segments, copied under live/recaps/<key>/ and played muted; the
narration is a clip per line, which the page plays over it at the times in its timeline. A build's
playlist and timeline are named for it, and the index (put last) names the build, so a page never
gets one build's video with another's lines:

    live/recaps.json                    the index: key -> {week, course, end_date, built, ...}
    live/recaps/<key>/recap-<built>.json  the timeline: intro and outro lines over cards, each clip's lines
    live/recaps/<key>/recap-<built>.m3u8  the clips, a discontinuity between each
    live/recaps/<key>/<session>/<seg>   the segments, copied from the reels
    live/recaps/<key>/a/<sha1>.wav      each line's audio, named for its content (cached for good)

The engine builds it (Engine._build_recap): this module finds the moments, times the lines and
publishes. How the week's told, which clips play and the request for the lines (the week's facts,
race, choose, prompt, schema, script and the constants they share) are shotcast_scoring.recap's,
shared with Shotcast Cloud, and imported here as they always were.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import tempfile
import time
import wave
from pathlib import Path
from typing import Any

import numpy as np

from shotcast_scoring.recap import (BOARD_ROWS, CARD_LEAD, CARD_WORDS, CATCH_UP_WORDS, CLIP_MAX, CLIP_MIN,
                                    CLIP_SECONDS, CLIP_WORDS, INTRO_SECONDS, LEAD, LINE_GAP, LINE_START,
                                    OUTRO_SECONDS, PER_TEAM, RECAP_SECONDS, RETIRE_SECONDS, TAIL, _num, board,
                                    choose, prompt, race, schema, script, standing, turn_moments, week_facts)

from .highlights import _get, playlist
from .live_stream import TYPES

log = logging.getLogger(__name__)

__all__ = ["BOARD_ROWS", "BUILD", "CARD_LEAD", "CARD_WORDS", "CATCH_UP_WORDS", "CLIP_MAX", "CLIP_MIN", "CLIP_SECONDS",
           "CLIP_WORDS", "INTRO_SECONDS", "LEAD", "LINE_GAP", "LINE_START", "OUTRO_SECONDS", "PER_TEAM",
           "RECAP_SECONDS", "RETIRE_SECONDS", "TAIL", "board", "burn_boards", "burn_cards", "choose", "cut", "digest",
           "footage", "key_of", "load", "parts", "prompt", "publish", "race", "reel_playlist", "schema", "script",
           "standing", "timeline", "turn_moments", "wav", "week_facts"]

BUILD = 5               # how a recap's made: a change to it builds every recap again


def key_of(facts: dict) -> str:
    return f"{facts.get('end_date') or 'week'}-w{facts['week']}"


def footage(reels: dict[str, dict], team_reel: dict[str, str], players: dict[str, list[str]]) -> dict[str, dict]:
    """Each team's moments from its round's reel (`reels`: reels.json's; `team_reel`: team -> reel
    key): team -> {key, moments, ats}. A reel two teams share (one round, a side each) gives each the
    shots its players hit; one of a single team's, all of them (a name the league file spells
    differently still counts) but another team's players' (a round shared that the other team has
    since played again). `ats`: where every moment of the reel starts, for where a clip has to end.
    `finish`: the round's last shot, cut on its own (highlights._finish), when it's the team's."""
    shared = {k for k in team_reel.values() if list(team_reel.values()).count(k) > 1}
    out: dict[str, dict] = {}
    for team, key in team_reel.items():
        reel = reels.get(key)
        if not isinstance(reel, dict) or not reel.get("moments"):
            continue
        names = {p.casefold() for p in players.get(team, [])}
        others = {p.casefold() for t, ps in players.items() if t != team for p in ps} - names
        moments = [m for m in reel["moments"] if isinstance(m, dict) and isinstance(m.get("at"), (int, float))
                   and (who := str(m.get("player") or "").casefold()) not in others
                   and (key not in shared or who in names)]
        f = reel.get("finish")
        if not (isinstance(f, dict) and isinstance(f.get("at"), (int, float))
                and (who := str(f.get("player") or "").casefold()) not in others and (key not in shared or who in names)):
            f = None
        if moments or f:
            out[team] = {"key": key, "moments": moments, "cuts": reel.get("cuts"), "ats": sorted(m["at"] for m in reel["moments"]
                                                                       if isinstance(m.get("at"), (int, float))),
                         **({"finish": f} if f else {})}
    return out


def digest(facts: dict, footage: dict[str, dict], turns: dict[str, list[dict]] | None = None) -> str:
    """What a recap is built from: the week's numbers and the moments it could show (where each is
    in its reel, and what it is; `turns`: turn_moments'). A change to any (Squabbit's net replacing
    the sim's, a team's reel cut late or cut again) rebuilds it, as does a change to how it's made or drawn."""
    from . import burn

    body = {"v": [BUILD, burn.LOOK, burn.CARD_LOOK], "rows": [[r["team"], r["net"]] for r in facts["rows"]],
            "holes": {t: {str(h): v for h, v in hs.items()} for t, hs in sorted((facts.get("holes") or {}).items())},
            "standings": [[s["team"], s["points"]] for s in facts["standings"]],
            "footage": {t: [f["key"], f.get("cuts"), [[m.get(k) for k in ("id", "at", "what", "level", "bad", "player", "hole")]
                                                      for m in f["moments"]],
                            [(f.get("finish") or {}).get(k) for k in ("id", "at", "what", "player", "hole", "cut")]]
                        for t, f in sorted(footage.items())},
            "turns": {t: [[m["id"], m["what"]] for m in ms] for t, ms in sorted((turns or {}).items())}}
    return hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]


# ---- the video and its timeline --------------------------------------------------------------

def parts(m3u8: str) -> list[list[tuple[str, float]]]:
    """A reel playlist's runs of segments, (uri, seconds), split at each discontinuity."""
    out: list[list[tuple[str, float]]] = [[]]
    dur = None
    for raw in m3u8.splitlines():
        s = raw.strip()
        if s.startswith("#EXTINF:"):
            try:
                dur = float(s[8:].split(",", 1)[0])
            except ValueError:
                dur = None
        elif s == "#EXT-X-DISCONTINUITY":
            if out[-1]:
                out.append([])
        elif s and not s.startswith("#") and dur is not None:
            out[-1].append((s, dur))
            dur = None
    return [p for p in out if p]


def cut(clip: dict, reel: list[list[tuple[str, float]]], ats: list[float], spoken: float) -> tuple[list[tuple[str, float]], float] | None:
    """A clip's segments of its reel, and how far into the first its moment's mark (`at` less LEAD)
    is: from just before the moment for long enough to hear its `spoken` seconds of lines, but not
    into the reel's next moment or past the end of the moment's run. None if the reel doesn't reach it."""
    start, run = 0.0, None
    for p in reel:
        length = sum(d for _, d in p)
        if start <= clip["at"] < start + length:
            run = (start, p)
            break
        start += length
    if run is None:
        return None
    first, segs = run
    want = max(first, clip["at"] - LEAD)
    nxt = min([a for a in ats if a > clip["at"] + 0.5], default=float("inf"))
    end = min(first + sum(d for _, d in segs), nxt - LEAD, want + CLIP_MAX)
    end = max(min(end, want + max(CLIP_MIN, LINE_START + spoken + TAIL)), want + 0.1)
    out, t, skew = [], first, None
    for uri, d in segs:
        if t + d > want and t < end:
            skew = want - t if skew is None else skew
            out.append((uri, d))
        t += d
    return (out, skew) if out else None


def timeline(facts: dict, clips: list[dict], lines: dict, voices: dict[tuple, tuple[float, str]],
             cuts: list[tuple[list[tuple[str, float]], float] | None]) -> tuple[dict, list[list[tuple[str, float]]], list[str]]:
    """recap.json's body (bar its key), the playlist's parts and where each part's segments are (under
    live/: a reel's folder, or a clip's own `src`, s for one cut from its stream). `lines`: script()'s;
    `voices`: each voiced line's (seconds, audio file) by ("intro", n), ("clip", i, n), ("outro", n);
    `cuts`: each clip's cut() (a clip that's None, or has no voiced lines, is left out)."""
    def voiced(where: tuple, ls) -> list[dict]:
        return [{"speaker": s, "text": t, "seconds": round(voices[(*where, n)][0], 2), "audio": voices[(*where, n)][1]}
                for n, (s, t) in enumerate(ls) if (*where, n) in voices]

    by_team = {r["team"]: r for r in facts["rows"]}
    story = race(facts)
    out_clips, out_parts, sources, offset = [], [], [], 0.0
    for i, (c, cu) in enumerate(zip(clips, cuts)):
        said = voiced(("clip", i), lines["clips"].get(i, []))
        if cu is None or not said:
            continue
        segs, skew = cu
        at = offset + skew + LINE_START
        for line in said:
            line["at"] = round(at, 2)
            at += line["seconds"] + LINE_GAP
        r = by_team.get(c["team"], {})
        out_clips.append({"at": round(offset, 2), "team": r.get("name", c["team"]), "player": c.get("player"),
                          "hole": c.get("hole"), "what": c.get("what"), "bad": bool(c.get("bad")),
                          "lines": said, "src": [c["key"], c.get("id")]})
        h = _num(c.get("hole"))
        if story and h in story["holes"] and story["holes"].index(h) > 0:   # (up all through, as on TV)
            out_clips[-1]["board"] = board(story, story["holes"].index(h) - 1, c["team"], by_team)
        out_parts.append(segs)
        sources.append(c.get("src") or f"reels/{c['key']}")
        offset += sum(d for _, d in segs)
    body = {"week": facts["week"], "course": facts.get("course"), "end_date": facts.get("end_date"),
            "rows": [{k: r[k] for k in ("name", "players", "net", "place", "tied", "points")} for r in facts["rows"]],
            "missed": facts.get("missed") or [],
            "standings": [{k: s[k] for k in ("name", "points", "place", "was")} for s in facts["standings"]],
            "intro": voiced(("intro",), lines["intro"]), "clips": out_clips,
            "outro": voiced(("outro",), lines["outro"]), "duration": round(offset, 1)}
    return body, out_parts, sources


def wav(samples: np.ndarray, rate: int) -> bytes:
    pcm = (np.clip(np.asarray(samples, dtype="float32"), -1.0, 1.0) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


# ---- S3 ------------------------------------------------------------------------------------

def _root(prefix: str) -> str:
    return (prefix.strip("/") + "/" if prefix.strip("/") else "") + "live/"


def load(client: Any, bucket: str, prefix: str) -> tuple[dict[str, dict], dict[str, dict]]:
    """reels.json's reels and recaps.json's recaps, as they are up there."""
    root = _root(prefix)
    out = []
    for name, field in (("reels.json", "reels"), ("recaps.json", "recaps")):
        raw = _get(client, bucket, root + name)
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = {}
        got = body.get(field) if isinstance(body, dict) else None
        out.append({k: v for k, v in (got or {}).items() if isinstance(v, dict)})
    return out[0], out[1]


def reel_playlist(client: Any, bucket: str, prefix: str, key: str, finish: bool = False) -> list[list[tuple[str, float]]]:
    """A reel's playlist parts, or (`finish`) its round's finish's, its uris from the reel's folder."""
    name = "finish/finish.m3u8" if finish else "reel.m3u8"
    raw = _get(client, bucket, f"{_root(prefix)}reels/{key}/{name}")
    got = parts(raw.decode("utf-8", "replace")) if raw else []
    return [[(f"finish/{u}", d) for u, d in p] for p in got] if finish else got


def _keys(client: Any, bucket: str, prefix: str) -> list[str]:
    return [o["Key"] for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix)
            for o in page.get("Contents", [])]


def _delete(client: Any, bucket: str, keys: list[str]) -> None:
    for i in range(0, len(keys), 1000):
        client.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in keys[i:i + 1000]]})


def burn_boards(client: Any, bucket: str, prefix: str, key: str, body: dict,
                video: list[list[tuple[str, float]]], sources: list[str]) -> tuple[list[list[tuple[str, float]]], dict[str, bytes]]:
    """Each clip with a board (timeline's) drawn into its video, so it's there full screen: its part's
    segments made again under b/ (named for what's in them: one already up isn't made again), and the
    clip marked `burned` (the page draws no board over it). Returns the playlist's parts and the
    segments made, by uri. Without ffmpeg, or for a clip it fails on, the page draws the board."""
    from . import burn

    ff = burn.ffmpeg()
    if not ff:
        if any(c.get("board") for c in body["clips"]):
            log.warning("League recap: no ffmpeg, so its leaderboard isn't in the video (pip install imageio-ffmpeg)")
        return video, {}
    root, base = _root(prefix), f"{_root(prefix)}recaps/{key}/"
    there = set(_keys(client, bucket, base + "b/"))
    out, made = [], {}
    for c, part, src in zip(body["clips"], video, sources):
        if not c.get("board"):
            out.append(part)
            continue
        tag = hashlib.sha1(json.dumps([burn.LOOK, src.removeprefix("reels/"), [u for u, _ in part], c["board"]], sort_keys=True).encode()).hexdigest()[:16]
        names = [f"b/{tag}-{n}.ts" for n in range(len(part))]
        if not all(base + n in there for n in names):
            try:
                segs = [client.get_object(Bucket=bucket, Key=f"{root}{src}/{u}")["Body"].read() for u, _ in part]
                made.update(zip(names, burn.clip(ff, segs, c["board"])))
            except Exception as e:
                log.warning("League recap: couldn't draw the leaderboard into the clip at %ss (%s)", c["at"], e)
                out.append(part)
                continue
        c["burned"] = True
        out.append([(n, d) for n, (_, d) in zip(names, part)])
    return out, made


def burn_cards(client: Any, bucket: str, prefix: str, key: str, body: dict, video: list[list[tuple[str, float]]],
               sources: list[str]) -> tuple[list[list[tuple[str, float]]], list[str], dict[str, bytes]]:
    """The intro's results and the outro's standings made video (burn.card) at the recap's start and
    end, so it's one video, seekable through and full screen: each a clip of the body's (`card`:
    "intro"/"outro", its lines at their times, the clips after it moved on), the body's intro and outro
    left empty (`cards`: True). Under b/, named for what's in them (one already up isn't made again).
    Returns the playlist's parts, their sources and the segments made, by uri. Without ffmpeg, or if
    it fails, all as it was: the page shows the cards over the player."""
    from . import burn

    ff = burn.ffmpeg()
    if not ff or not video or not (body["intro"] or body["outro"]):
        return video, sources, {}
    root, base = _root(prefix), f"{_root(prefix)}recaps/{key}/"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp, "first.ts")
            first.write_bytes(client.get_object(Bucket=bucket, Key=f"{root}{sources[0]}/{video[0][0][0]}")["Body"].read())
            like = burn.probe(ff, first)
        there = set(_keys(client, bucket, base + "b/"))
        made, cards = {}, {}
        for kind in ("intro", "outro"):
            lines = body[kind]
            if not lines:
                continue
            at, timed = CARD_LEAD, []
            for line in lines:
                timed.append({**line, "at": round(at, 2)})
                at += line["seconds"] + LINE_GAP
            seconds = round(at - LINE_GAP + TAIL, 2)
            what = {"week": body["week"], "course": body.get("course"), "rows": body["rows"], "standings": body["standings"]}
            tag = hashlib.sha1(json.dumps([burn.CARD_LOOK, kind, what, seconds, like], sort_keys=True).encode()).hexdigest()[:16]
            plan = burn.card_plan(seconds)
            names = [f"b/{kind}-{tag}-{n}.ts" for n in range(len(plan))]
            if not all(base + n in there for n in names):
                segs = burn.card(ff, burn.card_png(kind, body, like["width"], like["height"]), seconds, like)
                made.update(zip(names, (b for b, _ in segs)))
            cards[kind] = ({"at": 0.0, "card": kind, "team": None, "player": None, "hole": None, "what": None,
                            "bad": False, "lines": timed}, list(zip(names, plan)))
    except Exception as e:
        log.warning("League recap: couldn't make its opening and closing cards into video (%s); the page shows them", e)
        return video, sources, {}
    clips = [{**c, "lines": [dict(ln) for ln in c["lines"]]} for c in body["clips"]]
    video, sources = list(video), list(sources)
    if "intro" in cards:
        shift = sum(d for _, d in cards["intro"][1])
        for c in clips:
            c["at"] = round(c["at"] + shift, 2)
            for line in c["lines"]:
                line["at"] = round(line["at"] + shift, 2)
        clips.insert(0, cards["intro"][0])
        video.insert(0, cards["intro"][1])
        sources.insert(0, "card")
    if "outro" in cards:
        end = sum(d for p in video for _, d in p)
        c = cards["outro"][0]
        c["at"] = round(end, 2)
        for line in c["lines"]:
            line["at"] = round(line["at"] + end, 2)
        clips.append(c)
        video.append(cards["outro"][1])
        sources.append("card")
    body.update(clips=clips, cards=True, duration=round(sum(d for p in video for _, d in p), 1),
                **{k: [] for k in cards})
    return video, sources, made


def publish(client: Any, bucket: str, prefix: str, key: str, body: dict, video: list[list[tuple[str, float]]],
            sources: list[str], audio: list[tuple[str, bytes]], digest_: str, season_from: str | None = None,
            clock=time.time, made: dict[str, bytes] | None = None) -> dict:
    """Put a recap up: its clips' segments copied from their reels or streams (`sources`: timeline's;
    a reel's segments go when it's cut again, a stream's past its keep), its playlist, lines' audio, timeline and an entry in
    recaps.json; then the recaps of past seasons go. An earlier build's leftovers are retired (listed
    in the entry) and go at a build RETIRE_SECONDS on, a page that loaded it still playing them till
    then. `made`: segments made for it (burn_boards'), by uri, put up instead of copied. Returns the
    index entry."""
    root = _root(prefix)
    base = f"{root}recaps/{key}/"
    old = set(_keys(client, bucket, base))
    new = set()
    for part, src in zip(video, sources):
        for uri, _ in part:
            k = base + uri
            if k not in old and k not in new and made and uri in made:
                client.put_object(Bucket=bucket, Key=k, Body=made[uri], CacheControl="public, max-age=31536000, immutable",
                                  ContentType=TYPES[".ts"])
            elif k not in old and k not in new:
                client.copy_object(Bucket=bucket, Key=k, CopySource={"Bucket": bucket, "Key": f"{root}{src}/{uri}"},
                                   MetadataDirective="REPLACE", CacheControl="public, max-age=31536000, immutable",
                                   ContentType=TYPES.get("." + uri.rsplit(".", 1)[-1], "application/octet-stream"))
            new.add(k)
    for name, data in audio:
        client.put_object(Bucket=bucket, Key=base + name, Body=data, ContentType="audio/wav",
                          CacheControl="public, max-age=31536000, immutable")
        new.add(base + name)
    built = round(clock())
    name = f"{base}recap-{built}"
    client.put_object(Bucket=bucket, Key=name + ".m3u8", Body=playlist(video), CacheControl="no-cache",
                      ContentType=TYPES[".m3u8"])
    client.put_object(Bucket=bucket, Key=name + ".json", Body=json.dumps({**body, "key": key, "built": built}).encode(),
                      CacheControl="no-cache", ContentType=TYPES[".json"])
    new |= {name + ".m3u8", name + ".json"}
    _, index = load(client, bucket, prefix)
    retired = {}
    for k in old - new:
        retired[k[len(base):]] = ((index.get(key) or {}).get("retired") or {}).get(k[len(base):], built)
    # Every recap's leftovers retired long enough go now (a week not built again has no publish of its own).
    for k, v in index.items():
        if not isinstance(v, dict) or not isinstance(v.get("retired"), dict):
            continue
        here = f"{root}recaps/{k}/"
        gone_now = [here + n for n, when in v["retired"].items() if built - when >= RETIRE_SECONDS]
        if k == key:
            gone_now = [g for g in gone_now if g not in new]
            retired = {n: w for n, w in retired.items() if built - w < RETIRE_SECONDS}
        else:
            v["retired"] = {n: w for n, w in v["retired"].items() if built - w < RETIRE_SECONDS}
            if not v["retired"]:
                del v["retired"]
        _delete(client, bucket, sorted(gone_now))
    entry = {"week": body["week"], "course": body.get("course"), "end_date": body.get("end_date"), "built": built,
             "duration": body["duration"], "clips": sum(1 for c in body["clips"] if not c.get("card")), "hash": digest_,
             "winner": body["rows"][0]["name"] if body["rows"] else None}
    if retired:
        entry["retired"] = retired
    index[key] = entry
    gone = [k for k, v in index.items() if season_from and str(v.get("end_date") or "") < season_from]
    for k in gone:
        del index[k]
        _delete(client, bucket, _keys(client, bucket, f"{root}recaps/{k}/"))
    client.put_object(Bucket=bucket, Key=root + "recaps.json", Body=json.dumps({"recaps": index}).encode(),
                      CacheControl="no-cache", ContentType=TYPES[".json"])
    return entry
