import argparse
import asyncio
import time
import json
import logging
import sys
from collections import Counter
from pathlib import Path

from .config import Config, load_config


def _setup_logging(verbose: bool, log_dir: str | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_dir:
        # A file copy, so a session can be reviewed afterwards (or watched remotely).
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(Path(log_dir) / f"booth-{time.strftime('%Y-%m-%d')}.log", encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,  # a library (torch/kokoro) configures the root logger on import; without this
                     # basicConfig is a silent no-op and the file handler is never added
    )
    for noisy in ("httpx", "httpcore", "scapy.runtime"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # Kokoro's phonemizer warns on nearly every line about a word-count check that doesn't affect
    # audio, and resets its own level on each call, so a level change doesn't stick.
    logging.getLogger("phonemizer").disabled = True


def _overlay_wanted(cfg: Config) -> bool:
    return cfg.overlay.enabled and cfg.source.type != "replay"


def _build_engine(cfg: Config):
    from .engine import Engine
    from .league import League
    from .screen import Screen
    from .sources import build_source
    from .speech import SpeechQueue
    from .store import Store

    if not cfg.commentary.enabled and _overlay_wanted(cfg):
        # Starting off air but switchable on from the overlay: build the real clients so going on air
        # works, unless they can't be built (no API keys), in which case it's recording only.
        try:
            return _build_live_engine(cfg)
        except Exception as exc:
            logging.getLogger("commentator").warning(
                "Commentary clients unavailable (%s): recording only, the ON AIR overlay is off", exc)
            cfg.overlay.enabled = False
    if not cfg.commentary.enabled:
        # Recording only: no model or voice clients, so no API keys are needed. The engine never
        # calls these (see Engine._complete); they only fill the slots.
        from .llm.base import LLMClient
        from .tts.console_tts import ConsoleTTS

        class _Off(LLMClient):
            provider = "off"

            async def complete_json(self, system, user, schema, images=None):
                raise RuntimeError("commentary is off")

        off = _Off(cfg.llm.live.model_copy(update={"vision": False}))
        return Engine(
            cfg=cfg, league=League.load(cfg.league_file), source=build_source(cfg),
            live_llm=off, segment_llm=off, speech=SpeechQueue(cfg, ConsoleTTS()),
            screen=Screen(cfg.screen) if cfg.source.type != "replay" and cfg.screen.ocr else None,
            await_preshot=cfg.source.type == "replay",
            store=Store(cfg.db_path) if cfg.source.type != "replay" else None,
            health=_health(cfg),
        )
    return _build_live_engine(cfg)


def _build_live_engine(cfg: Config):
    from .engine import Engine
    from .league import League
    from .llm import build_llm
    from .screen import Screen
    from .sources import build_source
    from .speech import SpeechQueue
    from .store import Store
    from .tts import build_tts

    live = build_llm(cfg.llm.live)
    needs_screen = cfg.source.type != "replay" and (cfg.screen.ocr or cfg.screen.send_images)
    return Engine(
        cfg=cfg,
        league=League.load(cfg.league_file),
        source=build_source(cfg),
        live_llm=live,
        segment_llm=build_llm(cfg.llm.segment) if cfg.llm.segment else live,
        flight_llm=build_llm(cfg.llm.flight) if cfg.llm.flight else live,
        preshot_llm=build_llm(cfg.llm.preshot) if cfg.llm.preshot else live,
        round_llm=build_llm(cfg.round_llm),
        opener_llm=build_llm(cfg.opener_llm),
        closer_llm=build_llm(cfg.closer_llm),
        speech=SpeechQueue(cfg, build_tts(cfg.tts)),
        screen=Screen(cfg.screen) if needs_screen else None,
        await_preshot=cfg.source.type == "replay",
        # Replays are made-up shots; only live play goes into the season.
        store=Store(cfg.db_path) if cfg.source.type != "replay" else None,
        health=_health(cfg),
    )


def _health(cfg: Config):
    """Health checks persist next to the logs on a live source; a replay keeps them in memory."""
    from .health import Health
    from .notify import Notifier

    if cfg.source.type == "replay":
        return Health(None)
    health = Health(Path(cfg.log_dir) / "health.json")
    Notifier(cfg.notify).attach(health)
    return health


def cmd_notify_test(cfg: Config, args) -> None:
    """Send a test push to the phone (notify.provider and NTFY_TOPIC must be set)."""
    from .notify import Notifier

    n = Notifier(cfg.notify)
    if not n.enabled:
        sys.exit("Pushes are off: set NTFY_TOPIC in .env (and leave notify.provider at ntfy).")
    n.test()
    print(f"Sent a test push to {cfg.notify.server}/{n.topic[:4]}…")


def build_id() -> str:
    """The newest source timestamp in the package: which deployed build is actually running."""
    pkg = Path(__file__).resolve().parent
    mtimes = [p.stat().st_mtime for p in pkg.rglob("*.py")]
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(max(mtimes))) if mtimes else "unknown"


async def cmd_run(cfg: Config, args) -> None:
    if args.replay:
        cfg.source.type = "replay"
        cfg.source.replay_file = args.replay
    if args.console:
        cfg.tts.provider = "console"
    if args.no_commentary:
        cfg.commentary.enabled = False
    from .perf import tune

    tune(cfg.process_priority, cfg.max_cpu_cores)
    bid = build_id()
    logging.getLogger("commentator").info("Booth build: %s", bid)
    engine = _build_engine(cfg)
    if cfg.commentary.enabled:
        print(f"On air (build {bid}). Brain: {cfg.llm.live.provider}:{cfg.llm.live.model}  Voice: {cfg.tts.provider}  "
              f"Source: {cfg.source.type}. Ctrl+C to stop.", flush=True)
    else:
        print(f"Recording (build {bid}), commentary off: shots and rounds go to {cfg.db_path}. "
              f"Source: {cfg.source.type}. Ctrl+C to stop.", flush=True)
    if _overlay_wanted(cfg):
        from .overlay import OnAirOverlay

        loop = asyncio.get_running_loop()
        health = engine.health if cfg.health.enabled else None
        OnAirOverlay(cfg.overlay, is_on=lambda: engine.commentary_on,
                     toggle=lambda: loop.call_soon_threadsafe(
                         lambda: engine.set_commentary(not engine.commentary_on)),
                     health_status=health.status if health else None,
                     dismiss=health.dismiss if health else None).start()
    await engine.run()


async def cmd_sniff(cfg: Config, args) -> None:
    from .sources.gspro_sniffer import GSProSniffer

    sniffer = GSProSniffer(port=args.port or cfg.source.port, iface=cfg.source.iface)
    print("Listening for GSPro Open Connect traffic. Hit a shot.")
    async for direction, msg in sniffer.raw_messages():
        if not args.heartbeats and (msg.get("ShotDataOptions") or {}).get("IsHeartBeat"):
            continue
        print(f"--- {direction}\n{json.dumps(msg, indent=1)}", flush=True)


def _load_image(path: str | None):
    if not path:
        return None
    from PIL import Image

    return Image.open(path).convert("RGB")


def cmd_calibrate(cfg: Config, args) -> None:
    import time

    from .screen import Screen

    if args.delay and not args.image:
        for n in range(int(args.delay), 0, -1):
            print(f"Grabbing in {n}s... (bring the screen you want to the front)", end="\r", flush=True)
            time.sleep(1)
        print(" " * 60, end="\r")
    stem = args.out or "calibrate"
    screen = Screen(cfg.screen)
    base = _load_image(args.image)
    out_dir = Path(cfg.log_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    grid = out_dir / f"{stem}.png"
    screen.calibration_image(base).save(grid)
    # A clean copy too, so the regions can be read off the grid without the drawn boxes in the way.
    raw = out_dir / f"{stem}_raw.png"
    (base if base is not None else screen.grab()).save(raw)
    print(f"Saved {grid} (100px grid + current regions) and {raw} (clean). The grid labels give "
          f"pixel coords; divide by the image size for fractional regions.")


def cmd_fixture(cfg: Config, args) -> None:
    """Save a regression fixture from real frames and GSPro's log, or check them against the fixtures.
    Each screen kind's frame the booth saved (log_dir/fixture-frame[-kind].png) is used, or --image
    with --kind for one frame."""
    from . import fixtures
    from .league import League
    from .screen import Screen

    frames: dict[str, str] = {}
    if not args.no_screen:
        if args.image:
            frames[args.kind or "shot"] = args.image
        else:
            for kind in fixtures.KIND_REGIONS if args.kind is None else [args.kind]:
                stem = "fixture-frame" if kind == "shot" else f"fixture-frame-{kind}"
                if (path := Path(cfg.log_dir) / f"{stem}.png").exists():
                    frames[kind] = str(path)
        if not frames:
            print("No saved frame: the booth saves one on each round's first full shot and on the scorecard "
                  "and setup tabs (or pass --image).")
    text = None
    if not args.no_log:
        try:
            text = Path(args.log or cfg.source.gspro_log).read_bytes().decode("utf-8", "replace")
        except OSError as e:
            print(f"Can't read GSPro's log: {e}")
    version = next((ln.split(":", 1)[1].strip() for ln in (text or "").splitlines()[:400]
                    if ln.startswith("GSPro Version:")), None)
    screen = Screen(cfg.screen)
    if args.action == "save":
        if not args.name:
            sys.exit("fixture save needs a NAME (e.g. 3.1.6.20-3450x2160)")
        names = list(League.load(cfg.league_file).players)
        for kind, image in frames.items():
            meta = {"gspro_version": version}
            side = Path(image).with_suffix(".json")
            if side.exists():
                meta = {**meta, **{k: v for k, v in json.loads(side.read_text()).items() if k in ("gspro_version", "course")}}
            name = args.name if kind == "shot" else f"{args.name}-{kind}"
            out = fixtures.save_screen(_load_image(image), screen, name, meta, kind=kind)
            print(f"Screen fixture ({kind}): {out}")
            print(json.dumps(json.loads((out / "fixture.json").read_text())["expected"], indent=1))
        if text is not None:
            out = fixtures.save_log(text, args.name, names)
            kinds = Counter(e["kind"] for e in json.loads((out / "expected.json").read_text()))
            print(f"Log fixture: {out} ({sum(kinds.values())} events: {dict(kinds)})")
        print("Look over the files before committing them (log names and user paths are replaced).")
        return
    problems = []
    for kind, image in frames.items():
        problems += fixtures.check_screen(_load_image(image), screen, kind=kind)
    if text is not None:
        problems += fixtures.check_log(text)
    print("\n".join(problems) if problems else "Matches the fixtures.")
    if problems:
        sys.exit(1)


def cmd_screen_check(cfg: Config, args) -> None:
    import time

    from . import fixtures, screencheck
    from .screen import Screen

    if args.delay and not args.image:
        for n in range(int(args.delay), 0, -1):
            print(f"Checking in {n}s... (bring the GSPro screen you want to the front)", end="\r", flush=True)
            time.sleep(1)
        print(" " * 70, end="\r")
    screen = Screen(cfg.screen)
    img = _load_image(args.image)
    if img is None:
        img = screen.grab()
    print("Reading the screen (one full OCR pass, a few seconds)...")
    rep = screencheck.run(screen, img)
    print(screencheck.format_report(rep))
    out_dir = Path(cfg.log_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if rep.kind:
        pic = out_dir / "screen-check.png"
        marked = screencheck.annotate(img, screen, rep)
        marked.thumbnail((1920, 1920))
        marked.save(pic)
        print(f"Saved {pic}: green boxes read, red ones are off their panel, yellow is where --fix moves them.")
    if args.bundle and rep.kind:
        name = f"{rep.kind}-{img.width}x{img.height}-{time.strftime('%Y%m%d-%H%M%S')}"
        out = fixtures.save_screen(img, screen, name, {}, root=out_dir / "screen-check", kind=rep.kind)
        (out / "report.txt").write_text(screencheck.format_report(rep) + "\n", encoding="utf-8")
        print(f"Saved {out} (the boxes' crops and what they read). They show player names; look them over "
              "before attaching them to an issue.")
    if args.fix:
        if not rep.moves:
            print("Nothing to fix from this screen.")
            return
        path = Path(args.config)
        try:
            backup = screencheck.write_regions(path, rep.moves)
        except RuntimeError as exc:
            sys.exit(str(exc))
        print(f"Wrote {len(rep.moves)} region(s) to {path} under screen.regions"
              + (f" (old file kept as {backup})" if backup else "")
              + ". Restart the booth to use them, and run screen-check on the other GSPro screens too.")


def cmd_ocr_test(cfg: Config, args) -> None:
    from dataclasses import asdict

    from .hud import describe
    from .screen import Screen

    if not cfg.screen.regions:
        sys.exit("No screen.regions configured.")
    hud = Screen(cfg.screen).read_hud(list(cfg.screen.regions), _load_image(args.image))
    for key, value in asdict(hud).items():
        print(f"{key:14} {value!r}")
    print("\nAs facts:", " ".join(describe(hud)) or "(none)")


async def cmd_say(cfg: Config, args) -> None:
    from .models import Line
    from .speech import SpeechQueue
    from .tts import build_tts

    if args.voice:  # audition a voice without editing config
        args.speaker = args.speaker or "pbp"
        cfg.tts.openai_voices[args.speaker] = args.voice
        cfg.tts.google_voices[args.speaker] = args.voice
        cfg.tts.voices[args.speaker] = args.voice
        print(f"Using voice {args.voice!r} for {args.speaker}")
    speech = SpeechQueue(cfg, build_tts(cfg.tts))
    task = asyncio.create_task(speech.run())
    # No --speaker: each commentator says it in turn, so every configured voice is heard.
    speakers = [args.speaker] if args.speaker else list(cfg.personas)
    speech.say([Line(speaker=k, text=args.text) for k in speakers])
    while not speech.is_idle():
        await asyncio.sleep(0.1)
    task.cancel()


async def cmd_banter(cfg: Config, args) -> None:
    """Fetch a live golf headline and play the booth's between-shots banter about it."""
    import random

    from .league import League
    from .llm import build_llm
    from .news import GolfNews
    from .prompts import aside_room, news_prompt, news_schema, parse_lines, system_prompt
    from .speech import SpeechQueue
    from .tts import build_tts

    news = GolfNews(cfg.news.feeds, cfg.news.limit, max_age_days=cfg.news.max_age_days)
    await news.refresh()
    if not news.headlines:
        sys.exit("No headlines fetched (check the feeds and network).")
    topic = news.pick(random.Random()) if not args.headline else news.headlines[args.headline % len(news.headlines)]
    topic = topic or news.headlines[0]
    print(f"Headline: {topic.title}\n  {topic.summary}\n")

    league = League.load(cfg.league_file)
    llm = build_llm(cfg.closer_llm)
    user, _ = news_prompt(topic=topic.as_fact())
    user += aside_room(cfg.commentary.aside_max_words)
    res = await llm.complete_json(system_prompt(cfg, league), user, news_schema(list(cfg.personas)))
    lines = parse_lines(res.text, list(cfg.personas), cfg.commentary.aside_max_words)
    data = json.loads(res.text)
    print(f"Held for: {data.get('trigger')}" + (f"\n  tie-in: {data['tie_in']}" if data.get("tie_in") else ""))
    for ln in lines:
        print(f"  {cfg.personas[ln.speaker].name}: {ln.text}")

    speech = SpeechQueue(cfg, build_tts(cfg.tts))
    task = asyncio.create_task(speech.run())
    speech.say(lines)
    while not speech.is_idle():
        await asyncio.sleep(0.1)
    task.cancel()


def cmd_import_league(cfg: Config, args) -> None:
    from .league_import import run_import

    league, hist = run_import(args.current, args.history, args.league_out, args.history_out)
    print(f"{args.league_out}: {len(league['teams'])} teams, {len(league['schedule'])} weeks "
          "(handicaps, notes and GSPro names already in the file were kept)")
    for season in hist["seasons"]:
        played = [t for t in season["teams"] if t["weeks"]]
        top = min(played, key=lambda t: t["rank"])["label"] if played else "no weeks played yet"
        print(f"  {season['year']}: {season['team_count']} teams, {max((len(t['weeks']) for t in season['teams']), default=0)} "
              f"weeks recorded, {'champions' if season['complete'] else 'leading'}: {top}")
    missing = [p["name"] for t in league["teams"] for p in t["players"] if p.get("handicap") is None]
    if missing:
        print(f"No handicap yet for {len(missing)} players; add them in {args.league_out} for net scoring.")
    if cfg.league_file != args.league_out:
        print(f"Point config.yaml at it: league_file: {args.league_out}")


def cmd_squabbit(cfg: Config, args) -> None:
    from .squabbit import SquabbitClient, SquabbitError

    def redact(v):
        if isinstance(v, dict):
            return {k: ("<redacted>" if "mail" in k.lower() else redact(x)) for k, x in v.items()}
        if isinstance(v, list):
            return [redact(x) for x in v]
        return v

    def show(label: str, fields: dict) -> None:
        print(f"\n{label}")
        for k, v in sorted(fields.items()):
            if k == "_id":
                continue
            v = "<redacted>" if "mail" in k.lower() else redact(v)
            s = json.dumps(v, default=str)
            print(f"  {k}: {s if args.full else (s[:120] + '...' if len(s) > 120 else s)}")

    if args.user:
        try:
            with SquabbitClient() as sq:
                doc = sq.document(f"users/{args.user}")
        except SquabbitError as e:
            sys.exit(str(e))
        show(f"users/{args.user}", doc or {})
        return

    if args.handicaps:
        from .league import League

        lg = League.load(cfg.league_file)
        ids = [p["squabbit_id"] for t in lg.data.get("teams", []) for p in t.get("players", []) if p.get("squabbit_id")]
        try:
            with SquabbitClient() as sq:
                updated = lg.apply_handicaps(sq.handicaps(ids))
        except SquabbitError as e:
            sys.exit(str(e))
        print(f"Applied {len(updated)} live handicaps from Squabbit:")
        for name in lg.players:
            h = lg.handicap(name)
            print(f"  {name:18} {h if h is not None else '(not set in Squabbit)'}")
        return

    from .league import League

    invite = args.invite or League.load(cfg.league_file).squabbit_invite
    if not invite:
        sys.exit("No invite code: pass --invite or set squabbit_invite in the league file.")

    if args.cards or args.validate:
        from .handicap import validate_season
        from .league import cards_path

        try:
            with SquabbitClient() as sq:
                league = sq.league_by_invite(invite)
                if not league:
                    sys.exit(f"No league found for invite code {invite}.")
                if args.cards:
                    cards = sq.week_cards(league["_id"])
                    out = cards_path(cfg.league_file)
                    out.write_text(json.dumps({"invite": invite, "league": league.get("name"), "weeks": cards},
                                              indent=2), encoding="utf-8")
                    print(f"Wrote {len(cards)} week cards for {league.get('name')} to {out}:")
                    for c in cards:
                        hc = c["handicap"]
                        print(f"  week {c['week']}: {c['course']} ({c['tee']['name']} {c['tee']['rating']}/"
                              f"{c['tee']['slope']}), {c['format']}, {c['holes']} holes from {c['start_hole']}; "
                              f"{'course handicap' if hc['use_course_handicap'] else 'index'} x "
                              f"{hc['allowances'] or 'the default allowance'}"
                              f"{', max index ' + str(hc['max_index']) if hc['max_index'] else ''}")
                    return
                items = [{"t": t, "rounds": sq.tournament_rounds(t["_id"]), "lb": sq.leaderboard(t["_id"])}
                         for t in sq.tournaments(league["_id"])]
                results = validate_season(items, sq.league_rounds(league["_id"]))
        except SquabbitError as e:
            sys.exit(str(e))
        if not results:
            print(f"{league.get('name')}: no scored team results on the leaderboards yet.")
            return
        total = sum(len(v) for v in results.values())
        good = sum(ours == theirs for v in results.values() for _, _, ours, theirs in v)
        print(f"{league.get('name')}: {good} of {total} team nets match Squabbit's leaderboards")
        for fmt, rows in sorted(results.items()):
            print(f"  {fmt:10} {sum(o == t for _, _, o, t in rows)}/{len(rows)}")
            for week, team, ours, theirs in rows:
                if ours != theirs:
                    print(f"    {week}, {team}: ours {ours:+d}, Squabbit {theirs:+d}")
        return

    try:
        with SquabbitClient() as sq:
            league = sq.league_by_invite(invite)
            if not league:
                sys.exit(f"No league found for invite code {invite}.")
            print(f"League {league['_id']}: {league.get('name', '?')}")
            tours = sq.tournaments(league["_id"])
            cards = sq.scorecards(args.tournament) if args.tournament else []
            if args.out:
                detail = next((t for t in tours if t["_id"] == args.tournament), None)
                dump = {"league": redact(league),
                        "tournaments": [redact(t) for t in tours],
                        "tournament_detail": redact(detail) if detail else None,
                        "scorecards": [redact(c) for c in cards]}
                Path(args.out).write_text(json.dumps(dump, indent=2, default=str), encoding="utf-8")
                print(f"Wrote {args.out} ({len(tours)} tournaments, {len(cards)} scorecards; emails redacted)")
                return
            show("league fields", league)
            print(f"\n{len(tours)} tournaments:")
            for t in tours:
                print(f"  {t['_id']}: {t.get('name', '?')}")
            if args.tournament:
                show("tournament fields", next((t for t in tours if t["_id"] == args.tournament), {}))
                print(f"\n{len(cards)} scorecards in {args.tournament}:")
                if cards:
                    show("sample scorecard fields", cards[0])
    except SquabbitError as e:
        sys.exit(str(e))


async def cmd_check(cfg: Config, args) -> None:
    from .league import League
    from .llm import build_llm
    from .prompts import output_schema

    league = League.load(cfg.league_file)
    if cfg.league_file:
        print(f"League: {league.name}, {len(league.players)} players ({cfg.league_file})")
    else:
        print("League: none set (fine: every round is practice, players go by their GSPro names)")
    # Every model the booth can call, each tried once (roles sharing a model share the call).
    roles = {"live": cfg.llm.live, "segment": cfg.segment_llm, "flight": cfg.flight_llm,
             "preshot": cfg.preshot_llm, "opener": cfg.opener_llm, "closer": cfg.closer_llm}
    by_model: dict[tuple, tuple[list[str], object]] = {}
    for role, llm_cfg in roles.items():
        key = (llm_cfg.provider, llm_cfg.model, llm_cfg.base_url)
        by_model.setdefault(key, ([], llm_cfg))[0].append(role)
    failed = False
    for (provider, model, _), (names, llm_cfg) in by_model.items():
        label = f"{'/'.join(names)}: {provider}:{model}"
        try:
            llm = build_llm(llm_cfg)
            if provider == "ollama":
                models = await llm.list_models()
                if model not in models and f"{model}:latest" not in models:
                    print(f"{label} not pulled. Run: ollama pull {model}")
                    failed = True
                    continue
            result = await llm.complete_json(
                "Reply with JSON only.", 'Say "ready" as the play-by-play speaker.', output_schema(list(cfg.personas))
            )
            print(f"{label} OK in {result.latency_seconds:.1f}s -> {result.text[:120]}")
        except ModuleNotFoundError as e:
            failed = True
            print(f"{label} FAILED: package {e.name!r} is missing. Run: pip install -e \".[capture,cloud]\"")
        except Exception as e:
            failed = True
            print(f"{label} FAILED: {type(e).__name__}: {str(e)[:300]}")
    if failed:
        sys.exit("Some models failed; check the API keys in .env and the model names in config.yaml.")


async def cmd_eval(cfg: Config, args) -> None:
    """Replays a shot file several times with every shot commented and reports grounding failures."""
    import random
    import tempfile
    from collections import Counter

    from .engine import Engine
    from .league import League
    from .llm import build_llm
    from .sources.replay import ReplaySource
    from .speech import SpeechQueue
    from .tts.console_tts import ConsoleTTS

    if args.model:
        cfg.llm.live.model = args.model
        cfg.llm.segment = cfg.llm.flight = cfg.llm.preshot = None
    if args.vision:
        cfg.llm.live.vision = True
    cfg.tts.console_simulate_duration = False
    cfg.commentary.routine_comment_rate = 1.0
    cfg.commentary.preshot_rate = 1.0
    cfg.commentary.min_seconds_between_comments = 0
    cfg.commentary.max_speech_backlog = 1000
    cfg.log_dir = tempfile.mkdtemp(prefix="commentator-eval-")
    cfg.source.type = "replay"
    live = build_llm(cfg.llm.live)
    totals: Counter[str] = Counter()
    rejections = []
    flight_times: list[float] = []
    for i in range(args.runs):
        print(f"\n=== run {i + 1}/{args.runs}", flush=True)
        engine = Engine(
            cfg=cfg, league=League.load(cfg.league_file), source=ReplaySource(args.replay, speed=1e6),
            live_llm=live, segment_llm=build_llm(cfg.segment_llm) if cfg.llm.segment else live,
            flight_llm=build_llm(cfg.flight_llm) if cfg.llm.flight else live,
            preshot_llm=build_llm(cfg.preshot_llm) if cfg.llm.preshot else live,
            speech=SpeechQueue(cfg, ConsoleTTS()), rng=random.Random(i), await_preshot=True,
        )
        await engine.run()
        totals.update(engine.stats)
        rejections += engine.rejections
        flight_times += engine.flight_latencies
    checked = totals["lines_ok"] + totals["lines_rejected"]
    latencies = [json.loads(l)["latency_s"] for l in (Path(cfg.log_dir) / "usage.jsonl").read_text().splitlines()]
    print(f"\n=== {cfg.llm.live.provider}:{cfg.llm.live.model}, {args.runs} runs, "
          f"median call {sorted(latencies)[len(latencies) // 2]:.1f}s")
    print(f"lines checked: {checked}, rejected: {totals['lines_rejected']} "
          f"({totals['lines_rejected'] / max(checked, 1):.0%}), retries: {totals['retries']}, "
          f"still bad after retry (silenced): {totals['dropped_after_retry']}")
    if flight_times or totals["flight_late"]:
        print(f"in-flight calls: {len(flight_times)} on time (slowest {max(flight_times, default=0):.1f}s), "
              f"{totals['flight_late']} missed the {cfg.commentary.flight_deadline_seconds:.1f}s deadline")
    for text, problems in rejections:
        print(f"  REJECTED {text!r}: {'; '.join(problems)}")


def cmd_review(cfg: Config, args) -> None:
    """A session's log as a readable timeline, then one summary row per shot."""
    import re

    path = Path(cfg.log_dir) / f"booth-{args.date or time.strftime('%Y-%m-%d')}.log"
    if not path.exists():
        sys.exit(f"No log at {path}")
    shots: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "HTTP Request" in line:
            continue
        if not args.summary:
            print(line)
        if m := re.search(r"\[shot (\d+)\] (.*)", line):
            info = shots.setdefault(m.group(1), {})
            body = m.group(2)
            if body.startswith("detected"):
                info["time"] = line[:8]
                lag = re.search(r"([\d.]+)s after GSPconnect", body)
                info["feed lag"] = f"{lag.group(1)}s" if lag else "-"
                info["club"] = re.search(r"club (\S+),", body).group(1)
            elif body.startswith("projection"):
                info["projected"] = body.split("-> ")[1].split(";")[0]
            elif body.startswith("mid-flight call"):
                info["call"] = re.search(r"after detection", body) and re.search(r"([\d.]+)s after detection", body).group(1) + "s"
                info["audio"] = "ready" if "audio ready" in body else "voiced late"
            elif body.startswith("final reading"):
                info["final"] = re.search(r"lie (\S+)", body).group(1)
                info["landed"] = "yes" if "landed=True" in body else "NO"
            elif body.startswith("by "):
                info["player"] = body.split()[1].rstrip(":")
    if shots:
        cols = ["time", "player", "club", "feed lag", "projected", "call", "audio", "final", "landed"]
        print("\n" + "  ".join(f"{c:>11}" for c in ["shot"] + cols))
        for n, info in shots.items():
            print("  ".join(f"{v:>11}" for v in [n] + [str(info.get(c, "-")) for c in cols]))


def cmd_stats(cfg: Config, args) -> None:
    from .store import Store

    if not Path(cfg.db_path).exists():
        sys.exit(f"No season database at {cfg.db_path} yet.")
    rows = Store(cfg.db_path).leaderboard()
    print(f"{'player':12} {'nights':>6} {'shots':>6} {'longest drive':>14} {'avg drive':>10} {'fairways':>9}")
    for r in rows:
        fw = f"{r['fairway_pct']:.0f}%" if r["fairway_pct"] is not None else "-"
        print(f"{r['player']:12} {r['nights']:>6} {r['shots']:>6} {r['longest_drive'] or '-':>14} "
              f"{r['avg_drive'] or '-':>10} {fw:>9}")


def cmd_stats_web(cfg: Config, args) -> None:
    from .stats_web import serve

    path = Path(args.db or cfg.db_path)
    if not path.exists():
        sys.exit(f"No season database at {path}: copy the booth's logs/season.db there (or pass --db).")
    # A fixed folder, so a window closed without cleanup doesn't leave a copy behind each time.
    serve(path, port=args.port, open_browser=not args.no_browser,
          names={key: persona.name for key, persona in cfg.personas.items()},
          site=Path(cfg.log_dir) / "stats-web-site")


def cmd_backfill_log(cfg: Config, args) -> None:
    from .log_backfill import backfill

    path = Path(args.db or cfg.db_path)
    if not path.exists():
        sys.exit(f"No season database at {path}.")
    got = backfill(str(path), args.log, dry_run=args.dry_run, fix_starts=args.fix_starts)
    print(f"{'Would fill' if args.dry_run else 'Filled'}: {got['starts']} shot positions, {got['aims']} aims and "
          f"{got['hazards']} penalty-area entries ({got['matched']} of {got['fires']} logged shots matched); "
          f"{got['layouts']} hole layouts."
          + (f" Corrected {got['fixed']} positions." if args.fix_starts else ""))


def cmd_snap_minimap(cfg: Config, args) -> None:
    from .minimap import snapshot
    from .screen import Screen
    from .store import Store

    path = Path(args.db or cfg.db_path)
    if not path.exists():
        sys.exit(f"No season database at {path}.")
    screen = Screen(cfg.screen)
    base = _load_image(args.image)
    screen.detect_mirror(base)
    region = screen.region("minimap")
    image, ball, pin = snapshot(screen.crop(base, region) if base is not None else screen.grab(region))
    store = Store(str(path), new_session=False)
    sid = args.session
    if sid is None:
        row = store.db.execute("SELECT MAX(id) FROM sessions").fetchone()
        sid = row[0] if row else None
    store.record_hole_image(args.course, args.hole, image, ball, pin, session_id=sid, replace=True)
    print(f"Kept the minimap of {args.course} hole {args.hole} for session {sid} ({len(image) // 1024} KB); "
          + ("ball and pin found." if pin else "the ball and pin weren't found, so the replay won't use it."))


def cmd_publish(cfg: Config, args) -> None:
    from . import stats_site

    path = Path(args.db or cfg.db_path)
    if not path.exists():
        sys.exit(f"No season database at {path}.")
    site = Path(args.out or Path(cfg.log_dir) / "stats-site")
    names = {key: persona.name for key, persona in cfg.personas.items()}
    if args.build_only:
        version = stats_site.build(path, site, names)
        print(f"Built {version} in {site}")
        return
    if not cfg.publish.bucket:
        sys.exit("Set publish.bucket in config.yaml first (docs/tracking.md#sharing-the-stats-page).")
    try:
        version = stats_site.publish(path, site, cfg.publish.bucket, cfg.publish.prefix, cfg.publish.region, names)
    except ImportError:
        sys.exit('Publishing needs boto3: pip install -e ".[publish]"')
    print(f"Published {version} to s3://{cfg.publish.bucket}/{cfg.publish.prefix}")


def cmd_backup(cfg: Config, args) -> None:
    from . import backup

    if not Path(cfg.db_path).exists():
        sys.exit(f"No season database at {cfg.db_path}.")
    path = backup.run(cfg, force=args.force)
    print(f"Backed up to {path}" if path else f"Unchanged since the last copy in {backup.folder(cfg)}")


def cmd_restore(cfg: Config, args) -> None:
    from . import backup

    src = Path(args.file)
    if not src.exists():
        sys.exit(f"No backup at {src}.")
    try:
        saved = backup.restore(src, cfg.db_path, backup.folder(cfg), cfg.backup.daily, cfg.backup.weekly)
    except PermissionError:
        sys.exit("The season file is in use: stop the booth first.")
    print(f"Restored {src.name} as {cfg.db_path}" + (f" (the file it replaced is saved as {saved})" if saved else ""))


def cmd_cost(cfg: Config, args) -> None:
    from .usage import summarize

    print(summarize(cfg.log_dir, weeks=args.weeks, as_model=args.as_model))


def main() -> None:
    # Windows consoles default to a legacy code page; OCR'd text can hold anything.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="shotcast")
    parser.add_argument("-c", "--config", default="config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="Start the booth")
    p.add_argument("--replay", help="Replay a JSONL shot file instead of sniffing GSPro")
    p.add_argument("--console", action="store_true", help="Print lines instead of speaking them")
    p.add_argument("--no-commentary", action="store_true",
                   help="Record shots and rounds to the database without commentary (no API keys needed)")

    p = sub.add_parser("sniff", help="Print raw GSPro Open Connect messages")
    p.add_argument("--heartbeats", action="store_true")
    p.add_argument("--port", type=int, help="Override source.port")

    p = sub.add_parser("calibrate", help="Save a screenshot with the OCR regions drawn on it")
    p.add_argument("--image", help="Use a saved screenshot instead of the live screen")
    p.add_argument("--out", help="Output name stem (e.g. 'setup' -> logs/setup.png), default 'calibrate'")
    p.add_argument("--delay", type=int, default=0, help="Countdown seconds before grabbing, to bring a screen to the front")
    p = sub.add_parser("screen-check", help="Check what the booth reads from the GSPro screen that's up, "
                                            "and move boxes that are off their panels (--fix)")
    p.add_argument("--image", help="Use a saved screenshot instead of the live screen")
    p.add_argument("--delay", type=int, default=0, help="Countdown seconds before grabbing, to bring GSPro to the front")
    p.add_argument("--fix", action="store_true", help="Write boxes that read better moved to config.yaml (screen.regions)")
    p.add_argument("--bundle", action="store_true",
                   help="Also save the boxes' crops and the report under logs/screen-check/, for a bug report")
    p = sub.add_parser("ocr-test", help="Read the HUD once and print what was parsed")
    p.add_argument("--image", help="Use a saved screenshot instead of the live screen")

    p = sub.add_parser("fixture", help="Save a regression fixture (screen and GSPro log), or check against them")
    p.add_argument("action", choices=["save", "check"])
    p.add_argument("name", nargs="?", help="Fixture name for save, e.g. 3.1.6.20-3450x2160")
    p.add_argument("--image", help="A full-resolution frame (default: every log_dir/fixture-frame[-kind].png the booth saved)")
    p.add_argument("--kind", choices=["shot", "scorecard", "setup_players", "setup_holes", "setup_match"],
                   help="Screen kind: what --image shows, or the one saved frame to use (default: all saved)")
    p.add_argument("--log", help="GSPro's output_log.txt (default: source.gspro_log)")
    p.add_argument("--no-screen", action="store_true", help="Skip the screen")
    p.add_argument("--no-log", action="store_true", help="Skip the log")

    sub.add_parser("notify-test", help="Send a test health push to the phone (ntfy)")

    p = sub.add_parser("say", help="Speak a test line")
    p.add_argument("text")
    p.add_argument("--speaker", help="Just this commentator (pbp or color); default: each in turn")
    p.add_argument("--voice", help="Override the voice id just for this line (e.g. ash, echo, verse, onyx)")

    p = sub.add_parser("banter", help="Fetch a live golf headline and play the booth's banter about it")
    p.add_argument("--headline", type=int, help="Use the Nth fetched headline (default: random)")

    sub.add_parser("check", help="Check the league file and that the models respond")

    p = sub.add_parser("eval", help="Measure invented claims over repeated replays")
    p.add_argument("--replay", default="data/sample_shots.jsonl")
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--model", help="Override the model for every role in this eval")
    p.add_argument("--vision", action="store_true", help="Send screenshots (model must accept images)")

    sub.add_parser("stats", help="Season leaderboard from the shot database")

    p = sub.add_parser("stats-web", help="Browse the season database in a web page (read-only)")
    p.add_argument("--db", help="Database file (default: db_path, logs/season.db)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true", help="Don't open a browser tab")

    p = sub.add_parser("publish", help="Upload the stats page to S3 (publish: in config.yaml)")
    p.add_argument("--db", help="Database file (default: db_path, logs/season.db)")
    p.add_argument("--out", help="Where to build the site (default: logs/stats-site)")
    p.add_argument("--build-only", action="store_true", help="Build the site but don't upload it")

    p = sub.add_parser("backup", help="Back up the season database now (backup: in config.yaml)")
    p.add_argument("--force", action="store_true", help="Take a copy even if nothing changed")

    p = sub.add_parser("restore", help="Put a backup back as the season database (stop the booth first)")
    p.add_argument("file", help="A season-*.db.gz from the backup folder")

    p = sub.add_parser("backfill-log", help="Fill older shots' ball positions, aims and hole layouts from saved GSPro logs")
    p.add_argument("log", nargs="+", help="GSPro output_log.txt files")
    p.add_argument("--db", help="Database file (default: db_path, logs/season.db)")
    p.add_argument("--dry-run", action="store_true", help="Count what would be filled without changing the file")
    p.add_argument("--fix-starts", action="store_true",
                   help="Also correct stored ball positions the log disagrees with (group rounds recorded "
                        "before positions followed the shooter)")
    p = sub.add_parser("snap-minimap", help="Keep GSPro's minimap as a hole's replay background, replacing the "
                                            "one kept (the booth keeps one itself off each tee)")
    p.add_argument("--course", required=True, help="Course name, as the shots have it")
    p.add_argument("--hole", type=int, required=True, help="The course's hole number")
    p.add_argument("--session", type=int, help="Session it was taken in (default: the latest)")
    p.add_argument("--image", help="Use this full-screen screenshot instead of grabbing the screen")
    p.add_argument("--db", help="Database file (default: db_path, logs/season.db)")

    p = sub.add_parser("review", help="A session's log as a timeline plus a per-shot summary")
    p.add_argument("--date", help="YYYY-MM-DD, default today")
    p.add_argument("--summary", action="store_true", help="Only the per-shot table")

    p = sub.add_parser("import-league", help="Build the league file and history from the yearly spreadsheets")
    p.add_argument("current", help="This season's .xlsx")
    p.add_argument("history", nargs="*", help="Earlier seasons' .xlsx files")
    p.add_argument("--league-out", default="data/league.json")
    p.add_argument("--history-out", default="data/history.json")

    p = sub.add_parser("cost", help="Summarize LLM usage per night")
    p.add_argument("--weeks", type=int, default=12)
    p.add_argument("--as-model", help="Price every call as this Claude model, e.g. claude-sonnet-5-5")

    p = sub.add_parser("squabbit", help="Read this league's data from Squabbit (needs .env credentials)")
    p.add_argument("--invite", help="League invite code (defaults to squabbit_invite in the league file)")
    p.add_argument("--tournament", help="Also print a tournament's fields and a sample scorecard")
    p.add_argument("--full", action="store_true", help="Print full field values (no truncation)")
    p.add_argument("--out", help="Write fetched data (emails redacted) to this JSON file instead of printing")
    p.add_argument("--handicaps", action="store_true", help="Apply live handicaps to the league file and print them")
    p.add_argument("--user", help="Print a user's profile document by Squabbit id (to find the handicap field)")
    p.add_argument("--cards", action="store_true",
                   help="Save each week's course card (tee rating/slope, par, stroke index) and handicap "
                        "settings next to the league file, for the booth's net scoring")
    p.add_argument("--validate", action="store_true",
                   help="Recompute every team's net on the league's leaderboards and report how many match")

    args = parser.parse_args()
    _setup_logging(args.verbose, "logs" if args.command == "run" else None)
    from .dotenv import load_env

    # The booth's own .env (next to config.yaml), so its key isn't shared machine-wide.
    loaded = load_env(Path(__file__).resolve().parents[1] / ".env")
    if loaded:
        logging.info("Loaded %s from .env", ", ".join(loaded))
    cfg = load_config(args.config) if Path(args.config).exists() else Config()
    if not Path(args.config).exists():
        logging.warning("%s not found; using defaults", args.config)

    handlers = {
        "run": cmd_run, "sniff": cmd_sniff, "calibrate": cmd_calibrate, "ocr-test": cmd_ocr_test, "screen-check": cmd_screen_check, "fixture": cmd_fixture, "notify-test": cmd_notify_test,
        "say": cmd_say, "banter": cmd_banter, "check": cmd_check, "eval": cmd_eval, "stats": cmd_stats,
        "stats-web": cmd_stats_web,
        "publish": cmd_publish, "backup": cmd_backup, "restore": cmd_restore, "backfill-log": cmd_backfill_log, "snap-minimap": cmd_snap_minimap,
        "review": cmd_review,
        "cost": cmd_cost, "import-league": cmd_import_league, "squabbit": cmd_squabbit,
    }
    result = handlers[args.command](cfg, args)
    if asyncio.iscoroutine(result):
        try:
            asyncio.run(result)
        except KeyboardInterrupt:
            print("\nOff air.")


if __name__ == "__main__":
    main()
