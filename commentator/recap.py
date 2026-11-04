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

The engine builds it (Engine._build_recap): this module picks the moments, writes the request
for the lines, times them and publishes.
"""

from __future__ import annotations

import hashlib
import io
import json
import time
import wave
from typing import Any

import numpy as np

from .board import Board, place, place_points
from .highlights import _get, playlist
from .live_stream import TYPES

RECAP_SECONDS = 180.0   # the whole recap, cards and clips
INTRO_SECONDS = 22.0    # what the intro and outro take, for picking how many clips fit
OUTRO_SECONDS = 22.0
CLIP_SECONDS = 14.0     # what a clip takes, for picking how many fit
PER_TEAM = 3            # moments a leading team (or a big mover) can get; any other, two
LEAD = 1.0              # a clip starts this long before the moment's mark in its reel (`at`, a second before the shot)
LINE_START = 1.5        # its first line this long after that, as the swing comes
LINE_GAP = 0.3
TAIL = 1.5              # the clip runs this long past its last line
CLIP_MIN = 10.0         # long enough to see where the ball went
CLIP_MAX = 25.0
CLIP_WORDS = 26         # a clip's lines, together
CARD_WORDS = 35         # an intro or outro line
RETIRE_SECONDS = 3600.0  # a rebuild's leftovers stay this long (a player open on the last build still plays them)


def _net(n: int) -> str:
    return "E" if n == 0 else f"{n:+d}"


def week_facts(board: Board, week: int, today: str) -> dict[str, Any]:
    """The week's result and the standings it leaves, by the league file's team names (`team`) with
    their display names: rows best first {team, name, players, net, place, tied, points}, standings
    {team, name, points, place, was} (`was`: the place before this week; None for a first week)."""
    lg = board.league
    entry = lg.week_entry(week) or {}
    posted = board.posted(week)
    nets = [e.net for e in posted]
    rows = []
    for e in posted:
        pos, tied = place(e.net, nets)
        pts = place_points(pos, nets.count(e.net), board.points_table) if board.points_table else None
        rows.append({"team": e.team, "name": lg.team_name(e.team), "players": lg.team_players.get(e.team, []),
                     "net": e.net, "place": pos, "tied": tied, "points": None if pts is None else round(pts, 2)})
    table, _ = board.standings(today)
    before, _ = Board({w: v for w, v in board.weeks.items() if w != week}, lg, board.points_table).standings(today)
    was = {t: pl for t, _, pl in before}
    standings = [{"team": t, "name": lg.team_name(t), "points": round(p, 2), "place": pl, "was": was.get(t)}
                 for t, p, pl in table]
    return {"week": week, "course": entry.get("course"), "end_date": entry.get("end_date"), "rows": rows,
            "standings": standings, "missed": [lg.team_name(e.team) for e in board.weeks.get(week, []) if e.net is None]}


def key_of(facts: dict) -> str:
    return f"{facts.get('end_date') or 'week'}-w{facts['week']}"


def footage(reels: dict[str, dict], team_reel: dict[str, str], players: dict[str, list[str]]) -> dict[str, dict]:
    """Each team's moments from its round's reel (`reels`: reels.json's; `team_reel`: team -> reel
    key): team -> {key, moments, ats}. A reel two teams share (one round, a side each) gives each the
    shots its players hit; one of a single team's, all of them (a name the league file spells
    differently still counts) but another team's players' (a round shared that the other team has
    since played again). `ats`: where every moment of the reel starts, for where a clip has to end."""
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
        if moments:
            out[team] = {"key": key, "moments": moments, "cuts": reel.get("cuts"), "ats": sorted(m["at"] for m in reel["moments"]
                                                                       if isinstance(m.get("at"), (int, float)))}
    return out


def _ranked(moments: list[dict]) -> list[dict]:
    """A team's moments, the best first: the good by how big (a reel from before levels counts each
    the same), then the bad."""
    return sorted(moments, key=lambda m: (bool(m.get("bad")), -(m.get("level") or 1), m["at"]))


def choose(facts: dict, footage: dict[str, dict], budget: float = RECAP_SECONDS) -> list[dict]:
    """The recap's clips in the order they play: the teams from last place to the winner, each team's
    in the order they were hit. Every team with footage gets its best moment (a bad one only when it
    has nothing good) while they fit; then, a round at a time, another good one, the leaders and big
    movers first, up to PER_TEAM for them and two for the rest. Each: {team, key, **moment}."""
    order = [r["team"] for r in facts["rows"] if r["team"] in footage]
    if not order:
        return []
    place_of = {r["team"]: r["place"] for r in facts["rows"]}
    moved = {s["team"]: (s["was"] - s["place"]) if s.get("was") else 0 for s in facts.get("standings") or []}
    lead = {s["team"] for s in facts.get("standings") or [] if s["place"] <= 2}
    cap = {t: PER_TEAM if (t in lead or place_of[t] == 1 or moved.get(t, 0) >= 2) else 2 for t in order}
    ranked = {t: _ranked(footage[t]["moments"]) for t in order}
    room = budget - INTRO_SECONDS - OUTRO_SECONDS
    picked: dict[str, list[dict]] = {t: [] for t in order}
    for t in order:
        if room >= CLIP_SECONDS:
            picked[t].append(ranked[t][0])
            room -= CLIP_SECONDS
    priority = sorted(order, key=lambda t: place_of[t] - 2 * max(0, moved.get(t, 0)))
    while room >= CLIP_SECONDS:
        added = False
        for t in priority:
            more = [m for m in ranked[t] if m not in picked[t] and not m.get("bad")]
            if more and len(picked[t]) < cap[t] and room >= CLIP_SECONDS:
                picked[t].append(more[0])
                room -= CLIP_SECONDS
                added = True
        if not added:
            break
    return [{"team": t, "key": footage[t]["key"], **m}
            for t in reversed(order) for m in sorted(picked[t], key=lambda m: m["at"])]


def digest(facts: dict, footage: dict[str, dict]) -> str:
    """What a recap is built from: the week's numbers and the moments it could show (where each is
    in its reel, and what it is). A change to either (Squabbit's net replacing the sim's, a team's
    reel cut late or cut again) rebuilds it."""
    body = {"rows": [[r["team"], r["net"]] for r in facts["rows"]],
            "standings": [[s["team"], s["points"]] for s in facts["standings"]],
            "footage": {t: [f["key"], f.get("cuts"), [[m.get(k) for k in ("id", "at", "what", "level", "bad", "player", "hole")]
                                                      for m in f["moments"]]] for t, f in sorted(footage.items())}}
    return hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]


# ---- the lines -----------------------------------------------------------------------------

def schema(speakers: list[str]) -> dict[str, Any]:
    line = {"type": "object", "properties": {"speaker": {"type": "string", "enum": speakers}, "text": {"type": "string"}},
            "required": ["speaker", "text"], "additionalProperties": False}
    lines = {"type": "array", "items": line}
    return {"type": "object", "properties": {
        "intro": lines,
        "clips": {"type": "array", "items": {"type": "object", "properties": {"clip": {"type": "integer"}, "lines": lines},
                                             "required": ["clip", "lines"], "additionalProperties": False}},
        "outro": lines}, "required": ["intro", "clips", "outro"], "additionalProperties": False}


def prompt(facts: dict, clips: list[dict], names: dict[str, str]) -> tuple[str, str]:
    """The request for the recap's lines, and its facts (what the lines are checked against).
    `names`: speaker key -> commentator's name."""
    out = [f"League week {facts['week']}" + (f" at {facts['course']}" if facts.get("course") else "") + " is final."]
    out.append("The week's results, best first (team net to par" + (", points earned" if any(
        r["points"] is not None for r in facts["rows"]) else "") + "):")
    for r in facts["rows"]:
        pts = f", {r['points']:g} points" if r["points"] is not None else ""
        out.append(f"- {'T' if r['tied'] else ''}{r['place']}. {r['name']} ({' & '.join(r['players'])}): "
                   f"{_net(r['net'])}{pts}")
    if facts.get("missed"):
        out.append("No score posted: " + ", ".join(facts["missed"]) + ".")
    if facts.get("standings"):
        out.append("The season standings after it:")
        for s in facts["standings"]:
            move = ""
            if s.get("was"):
                d = s["was"] - s["place"]
                move = f" (up {d} from {s['was']})" if d > 0 else f" (down {-d} from {s['was']})" if d < 0 else " (same place)"
            out.append(f"- {s['place']}. {s['name']}: {s['points']:g} points{move}")
    out.append("The clips, in the order they play:")
    by_team = {r["team"]: r["name"] for r in facts["rows"]}
    for i, c in enumerate(clips):
        out.append(f"- clip {i}: {by_team.get(c['team'], c['team'])}, {c.get('player') or 'a player'}"
                   + (f" on hole {c['hole']}" if c.get("hole") else "") + f": {c.get('what') or 'a shot'}"
                   + (" (a rough moment)" if c.get("bad") else ""))
    facts_text = "\n".join(out)
    who = " and ".join(names.values()) or "the booth"
    ask = (
        f"\n\nWrite the week's recap package, voiced by {who}, as they would on a highlights show the day "
        "after. It plays as a short video: an intro over the results, then the clips, then an outro over "
        "the standings.\n"
        f"- intro: two or three lines, at most {CARD_WORDS} words each: the week, the course, who won it "
        "and by what, and how the field finished.\n"
        f"- clips: for every clip listed, by its number, one or two lines, at most {CLIP_WORDS} words "
        "between them. They play as the shot is hit, so call it the way the clip says it went, and tie it "
        "to that team's week. Go through the teams as the clips run, last place to the winner.\n"
        f"- outro: two or three lines, at most {CARD_WORDS} words each, on the season standings now: who "
        "leads, who moved, what's ahead.\n"
        "Trade lines between the commentators, as they do live. Use only the facts here: no other scores, "
        "points or shots. Say nets the way golf does (\"four under\", \"even par\").")
    return facts_text + ask, facts_text


def script(raw: str, clips: list[dict], speakers: list[str]) -> tuple[dict, list[str]]:
    """The model's lines, {intro, clips: {n: lines}, outro} as [(speaker, text)], and what's wrong with
    them: no intro or outro, a clip with no lines, or one with too many words."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}

    def lines(items) -> list[tuple[str, str]]:
        return [(str(i["speaker"]), " ".join(str(i.get("text") or "").split())) for i in items or []
                if isinstance(i, dict) and i.get("speaker") in speakers and str(i.get("text") or "").strip()]

    out = {"intro": lines(data.get("intro")), "outro": lines(data.get("outro")), "clips": {}}
    for c in data.get("clips") or []:
        if isinstance(c, dict) and isinstance(c.get("clip"), int) and 0 <= c["clip"] < len(clips):
            out["clips"][c["clip"]] = lines(c.get("lines"))[:2]
    problems = []
    if not out["intro"]:
        problems.append("write the intro")
    if not out["outro"]:
        problems.append("write the outro")
    if missing := [str(i) for i in range(len(clips)) if not out["clips"].get(i)]:
        problems.append("write lines for clip " + ", ".join(missing))
    if long := [str(i) for i, ls in out["clips"].items() if sum(len(t.split()) for _, t in ls) > CLIP_WORDS + 4]:
        problems.append(f"keep each clip's lines to {CLIP_WORDS} words between them (clip {', '.join(long)})")
    if long := [t[:30] for _, t in out["intro"] + out["outro"] if len(t.split()) > CARD_WORDS + 5]:
        problems.append(f"keep each intro and outro line to {CARD_WORDS} words")
    return out, problems


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
    """recap.json's body (bar its key), the playlist's parts and each part's reel. `lines`: script()'s;
    `voices`: each voiced line's (seconds, audio file) by ("intro", n), ("clip", i, n), ("outro", n);
    `cuts`: each clip's cut() (a clip that's None, or has no voiced lines, is left out)."""
    def voiced(where: tuple, ls) -> list[dict]:
        return [{"speaker": s, "text": t, "seconds": round(voices[(*where, n)][0], 2), "audio": voices[(*where, n)][1]}
                for n, (s, t) in enumerate(ls) if (*where, n) in voices]

    by_team = {r["team"]: r for r in facts["rows"]}
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
        out_parts.append(segs)
        sources.append(c["key"])
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


def reel_playlist(client: Any, bucket: str, prefix: str, key: str) -> list[list[tuple[str, float]]]:
    raw = _get(client, bucket, f"{_root(prefix)}reels/{key}/reel.m3u8")
    return parts(raw.decode("utf-8", "replace")) if raw else []


def _keys(client: Any, bucket: str, prefix: str) -> list[str]:
    return [o["Key"] for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix)
            for o in page.get("Contents", [])]


def _delete(client: Any, bucket: str, keys: list[str]) -> None:
    for i in range(0, len(keys), 1000):
        client.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in keys[i:i + 1000]]})


def publish(client: Any, bucket: str, prefix: str, key: str, body: dict, video: list[list[tuple[str, float]]],
            sources: list[str], audio: list[tuple[str, bytes]], digest_: str, season_from: str | None = None,
            clock=time.time) -> dict:
    """Put a recap up: its clips' segments copied from their reels (`sources`: each part's reel key;
    a reel's segments go when it's cut again), its playlist, lines' audio, timeline and an entry in
    recaps.json; then the recaps of past seasons go. An earlier build's leftovers are retired (listed
    in the entry) and go at a build RETIRE_SECONDS on, a page that loaded it still playing them till
    then. Returns the index entry."""
    root = _root(prefix)
    base = f"{root}recaps/{key}/"
    old = set(_keys(client, bucket, base))
    new = set()
    for part, src in zip(video, sources):
        for uri, _ in part:
            k = base + uri
            if k not in old and k not in new:
                client.copy_object(Bucket=bucket, Key=k, CopySource={"Bucket": bucket, "Key": f"{root}reels/{src}/{uri}"},
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
             "duration": body["duration"], "clips": len(body["clips"]), "hash": digest_,
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
