import asyncio
import io
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from commentator.config import Config
from commentator.engine import Engine
from commentator.flight_pick import Hazard
from commentator.hud import Hud
from commentator.league import League
from commentator.llm.base import LLMClient, LLMResult
from commentator.minimap import JevMinimap, MinimapRead, Past, Sand, Tree, fit, half

pytest.importorskip("typesafe_sdk")


def test_half_greys_the_other_side_of_the_ball_to_pin_line():
    img = Image.new("RGB", (300, 300), (0, 0, 0))
    # Ball at the bottom middle, pin straight up: left of the line is x < 150.
    left = half(img, (0.5, 0.9), (0.5, 0.1), "left")
    right = half(img, (0.5, 0.9), (0.5, 0.1), "right")
    assert left.getpixel((50, 150)) == (0, 0, 0) and left.getpixel((250, 150)) == (128, 128, 128)
    assert right.getpixel((250, 150)) == (0, 0, 0) and right.getpixel((50, 150)) == (128, 128, 128)
    # A hole bending right: a point right of the straight-up line but left of the bent one is left.
    bent = half(img, (0.5, 0.9), (0.9, 0.1), "left")
    assert bent.getpixel((200, 150)) == (0, 0, 0)


def test_fit_scales_into_jevs_limits():
    assert min(fit(Image.new("RGB", (170, 310))).size) >= 256
    assert max(fit(Image.new("RGB", (400, 3000))).size) <= 1568
    assert fit(Image.new("RGB", (458, 836))).size == (458, 836)


def test_read_facts_respect_threshold_and_the_pin_distance():
    r = MinimapRead(threshold=0.7, sides=True,
                    seen={"water_ahead": 0.9, "carry_shown": 0.9, "water_left": 0.95, "water_right": 0.55,
                          "ob_right": 0.8, "bunker_left": 0.3, "front_shown": 0.9, "back_shown": 0.9},
                    numbers={"carry": 142, "front": 150, "back": 172})
    facts = r.facts(160)
    assert "Hole layout: water in front, about 142 yards to carry it." in facts
    assert "Hole layout: water down the left." in facts
    assert "Hole layout: out of bounds down the right." in facts
    assert not any("water down the right" in f or "bunkers" in f for f in facts)  # under the threshold
    assert "Green: 150 yards to the front edge, 172 to the back." in facts
    # A "front" number past the pin is a misread (the island's pin number came back as the back edge).
    assert not any(f.startswith("Green") for f in r.facts(120))
    assert r.hazards(160) == [Hazard("water", "left"), Hazard("out of bounds", "right"),
                              Hazard("water", "short", to_yards=142)]
    # No sides without a trusted ball/pin split; an island green isn't also "water down the left".
    island = MinimapRead(threshold=0.7, sides=True, seen={"island": 0.9, "water_left": 0.9})
    assert island.facts() == ["Hole layout: an island green, water all around it."]
    assert MinimapRead(threshold=0.7, seen={"water_left": 0.9}).facts() == []


def _box(x, y, conf):
    return SimpleNamespace(type="box", confidence=conf,
                           box=SimpleNamespace(left=x - 0.02, right=x + 0.02, top=y - 0.02, bottom=y + 0.02))


class FakeJev:
    """Answers by question name, like the real client; records which calls were made."""

    def __init__(self, box_conf=0.9, pin_y=0.2, choices=None, aim=None):
        self.box_conf, self.pin_y, self.calls = box_conf, pin_y, []
        self.choices = choices or {}
        self.aim = aim   # the number beside the pin box's dot, when it's really the aim point

    async def system_one(self, state, questions, model=None, extra_headers=None):
        self.calls.append((model, sorted(state), sorted(questions)))
        assert extra_headers == {"TypeSafe-Danger-Zone-Images": "true"}
        for img in state.values():
            assert min(Image.open(io.BytesIO(img.data)).size) >= 256
        answers = {}
        for k in questions:
            if k == "ball":
                answers[k] = _box(0.5, 0.9, self.box_conf)
            elif k == "pin":
                answers[k] = _box(0.5, self.pin_y, self.box_conf)
            elif k in ("aim_h", "aim_r"):
                n = f"{self.aim or 0:03d}"
                answers[k] = SimpleNamespace(type="choice", choice=n[0] if k == "aim_h" else n[1:],
                                             confidence=0.95 if self.aim else 0.3)
            elif k == "aim_label":
                answers[k] = SimpleNamespace(type="noul", noul=0.95 if self.aim else 0.1)
            elif k in ("fairway", "pin_side"):
                choice, conf = self.choices.get(k, ("along" if k == "fairway" else "center", 0.5))
                answers[k] = SimpleNamespace(type="choice", choice=choice, confidence=conf)
            elif k.endswith("_h"):
                answers[k] = SimpleNamespace(type="choice", choice="1", confidence=0.9)
            elif k.endswith("_r"):
                answers[k] = SimpleNamespace(type="choice", choice={"carry_r": "42", "front_r": "50"}.get(k, "72"),
                                             confidence=0.9)
            else:
                answers[k] = SimpleNamespace(type="noul", noul=0.9 if k in ("water_ahead", "carry_shown", "water_left",
                                                                            "front_shown", "back_shown") else 0.1)
        return SimpleNamespace(answers=answers)


async def test_jev_read_splits_the_map_and_reads_numbers():
    fake = FakeJev()
    r = await JevMinimap(Config().minimap, client=fake).read(Image.new("RGB", (170, 310)))
    assert r.sides and len(fake.calls) == 3
    assert fake.calls[0][0] == "jev-preview" and fake.calls[0][2] == ["ball", "pin"]
    assert fake.calls[2][1] == ["aimed", "left", "right"]   # no to_pin: no patches, no green close-up
    assert r.facts(160) == ["Hole layout: water in front, about 142 yards to carry it.",
                            "Hole layout: water down the left.",
                            "Green: 150 yards to the front edge, 172 to the back."]


async def test_each_jev_request_is_logged_with_its_tokens_and_cost(tmp_path):
    from commentator.usage import UsageLog, summarize

    class BilledJev(FakeJev):
        async def system_one(self, *a, **kw):
            resp = await super().system_one(*a, **kw)
            return SimpleNamespace(answers=resp.answers, model="jev-1.14",
                                   usage=SimpleNamespace(input_tokens=2_000, output_tokens=12))

    fake = BilledJev()
    await JevMinimap(Config().minimap, client=fake, usage=UsageLog(str(tmp_path))).read(Image.new("RGB", (170, 310)))
    rows = [json.loads(l) for l in (tmp_path / "usage.jsonl").read_text().splitlines()]
    assert len(rows) == len(fake.calls) == 3
    assert {(r["kind"], r["provider"], r["model"], r["input_tokens"]) for r in rows} == {
        ("minimap", "typesafe", "jev-1.14", 2_000)}
    assert all(r["est_cost_usd"] == pytest.approx(2_000 * 0.042 / 1e6) for r in rows)  # output is free
    assert "typesafe $0.00" in summarize(str(tmp_path))


async def test_jev_read_skips_sides_without_a_confident_ball_and_pin():
    fake = FakeJev(box_conf=0.3)
    r = await JevMinimap(Config().minimap, client=fake).read(Image.new("RGB", (300, 500)))
    assert not r.sides and len(fake.calls) == 2
    assert "Hole layout: water down the left." not in r.facts(160)
    # A pin below the ball means the boxes are wrong; no split either.
    r = await JevMinimap(Config().minimap, client=FakeJev(pin_y=0.95)).read(Image.new("RGB", (300, 500)))
    assert not r.sides


class RecordingLLM(LLMClient):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.users: list[str] = []

    async def complete_json(self, system, user, schema, images=None):
        self.users.append(user)
        if "HAZARDS" in user:
            return LLMResult(text=json.dumps({"hazards": [{"kind": "bunker", "side": "right"}]}))
        return LLMResult(text=json.dumps({"lines": []}))


def _engine(tmp_path):
    cfg = Config(log_dir=str(tmp_path))
    cfg.minimap.timeout_seconds = 0.2
    llm = RecordingLLM(cfg.llm.live)
    return Engine(cfg=cfg, league=League.load("data/league.example.json"), source=None,
                  live_llm=llm, segment_llm=llm, speech=None), llm


async def test_preshot_uses_jev_facts_and_hazards_instead_of_the_vision_call(tmp_path):
    engine, llm = _engine(tmp_path)

    async def jev():
        return MinimapRead(threshold=0.7, sides=True, seen={"water_left": 0.9})

    hud = Hud(player_text="Tony", shot_number=2, to_pin="160 yards", lie="fairway")
    raws, facts_text = await engine._preshot_model(hud, None, [b"jpeg"], asyncio.create_task(jev()))
    assert "Hole layout: water down the left." in facts_text
    assert json.loads(raws["hazards"])["hazards"][0] == {"kind": "water", "side": "left",
                                                         "from_yards": None, "to_yards": None, "edge": [], "off_yards": None,
                                                         "wide_yards": None}
    assert not any("HAZARDS" in u for u in llm.users)


async def test_preshot_falls_back_to_the_vision_call_when_jev_is_late(tmp_path):
    engine, llm = _engine(tmp_path)

    async def slow():
        await asyncio.sleep(5)

    hud = Hud(player_text="Tony", shot_number=2, to_pin="160 yards", lie="fairway")
    raws, facts_text = await engine._preshot_model(hud, None, [b"jpeg"], asyncio.create_task(slow()))
    assert "Hole layout" not in facts_text
    assert json.loads(raws["hazards"])["hazards"] == [{"kind": "bunker", "side": "right"}]


async def test_preshot_cancels_the_minimap_read_on_a_putt(tmp_path):
    engine, _ = _engine(tmp_path)
    task = asyncio.create_task(asyncio.sleep(5))
    hud = Hud(player_text="Tony", shot_number=3, to_pin="12 feet", lie="green")
    raws, _ = await engine._preshot_model(hud, None, [], task)
    await asyncio.sleep(0)
    assert task.cancelled() and "hazards" not in raws


def _course(blobs):
    """A green map with tan sand blobs, each (x0, y0, x1, y1) in pixels."""
    from PIL import ImageDraw

    img = Image.new("RGB", (460, 836), (80, 105, 40))
    d = ImageDraw.Draw(img)
    for box in blobs:
        d.ellipse(box, fill=(168, 136, 92))
    return img


def test_sand_patches_measures_bunkers_near_the_line():
    from commentator.minimap import sand_patches

    # Ball at the bottom, pin 600 px straight up: the map is 600 px per to-pin.
    img = _course([(250, 400, 290, 440),      # right, ~20 px off the line, 312..352 px out
                   (150, 600, 200, 630),      # left, ~30 px off, 122..152 px out
                   (10, 300, 40, 330),        # far left: outside the corridor (roofs live out here)
                   (215, 740, 245, 765)])     # under the ball: the bunker it's sitting in
    ball, pin = (230 / 460, 752 / 836), (230 / 460, 152 / 836)
    found = sand_patches(img, ball, pin, corridor=0.15)
    assert [p[0] for p in found] == ["right", "left"]   # nearest the line first
    (_, near_r, far_r, *_), (_, near_l, far_l, *_) = found
    assert 0.19 < near_l < far_l < 0.3 and 0.51 < near_r < far_r < 0.6
    assert all(p[4].size == (300, 300) for p in found)


def test_measured_bunkers_become_facts_hazards_and_named_trouble():
    from commentator.minimap import Sand

    r = MinimapRead(threshold=0.7, sides=True, to_pin_hint=400,
                    seen={"bunker_left": 0.9, "bunker_right": 0.9, "water_right": 0.9},
                    sand=[Sand("left", 0.6, 0.66, 0.92), Sand("right", 0.7, 0.75, 0.5)])
    facts = r.facts(410)
    # The log's 410 sets the yards, not the hint.
    assert "Hole layout: a bunker left, about 245 to 270 yards out." in facts
    assert "Hole layout: a bunker (or bunkers) left of the line to the pin." not in facts
    # The unsure patch on the right stays out; the half-map bunker read still says there's sand there.
    assert "Hole layout: a bunker (or bunkers) right of the line to the pin." in facts
    assert Hazard("bunker", "left", 246, 271) in r.hazards(410)
    assert r.named(410) == {"left": "bunker", "right": "water"}   # water outranks sand


def test_named_trouble_line_plays_only_when_its_hazard_is_in_play():
    from commentator.flight_pick import FlightPlan, pick_line
    from commentator.models import Shot

    plan = FlightPlan(player=None, to_pin="420 yards", hazards=[Hazard("bunker", "left", 240, 265)],
                      lines={"left_trouble": "Bending left, the bunker in play.", "left": "Pulled left."},
                      named={"left": "bunker"})
    hook = dict(hla=-4.5, spin_axis=-18)
    assert pick_line(plan, Shot(club="DR", carry=250, **hook), "420 yards")[0] == "left_trouble"
    assert pick_line(plan, Shot(club="5I", carry=180, **hook), "420 yards")[0] == "left"
    # Water down the left (no distance) is in play for both, but the line names the bunker.
    plan.hazards.append(Hazard("water", "left"))
    assert pick_line(plan, Shot(club="5I", carry=180, **hook), "420 yards")[0] == "left"


def test_flight_lines_name_only_their_sides_hazard(tmp_path):
    from commentator.engine import _names_other_scenery

    assert not _names_other_scenery("Bending left, that bunker in play.", "bunker")
    assert _names_other_scenery("Bending left, the water in play.", "bunker")
    assert _names_other_scenery("Pulled left toward the bunker.", None)
    assert not _names_other_scenery("Pulled left, trouble in play.", None)
    engine, _ = _engine(tmp_path)
    facts = "Hole layout: a bunker left, about 245 to 270 yards out.\nHole layout: water down the right."
    plan = engine._build_plan({"named": {"left": "bunker"}, "flight_lines": {
        "left_trouble": "Hooking left, bringing the bunker into play.",
        "left": "Pulled left, toward the bunker.",
        "right_trouble": "Leaking right toward the water."}}, None, "400 yards", facts, seen_image=False)
    assert plan.named == {"left": "bunker"} and set(plan.lines) == {"left_trouble"}


def test_branch_prompt_lets_named_sides_name_their_hazard():
    from commentator.prompts import preshot_branch_prompt

    user, _ = preshot_branch_prompt(player="Tony", team=None, hole=4, facts=[], recent_lines=[], speaker="pbp",
                                    named={"left": "water"})
    assert "left_trouble names the water down the left" in user
    assert "right_trouble stays generic" in user
    plain, _ = preshot_branch_prompt(player="Tony", team=None, hole=4, facts=[], recent_lines=[], speaker="pbp")
    assert "NEVER a specific" in plain


async def test_jev_read_confirms_and_measures_sand():
    fake = FakeJev()
    img = _course([(250, 400, 290, 440), (150, 600, 200, 630)])
    r = await JevMinimap(Config().minimap, client=fake).read(img, to_pin=400)
    assert sorted(fake.calls[2][1]) == ["aimed", "left", "patch0", "patch1", "right"]
    assert {b.side for b in r.sand} == {"left", "right"}


def test_split_bunker_pieces_merge_into_one():
    # A tree split one greenside bunker into three pieces (Pebble 18); a separate bunker on the
    # other side and a distant one on the same side stay apart.
    r = MinimapRead(threshold=0.7, sides=True, to_pin_hint=152, sand=[
        Sand("right", 0.82, 0.86, 0.95), Sand("right", 0.85, 0.87, 0.96), Sand("right", 0.88, 0.92, 0.94),
        Sand("left", 0.84, 0.88, 0.9), Sand("right", 0.30, 0.35, 0.9), Sand("right", 0.5, 0.6, 0.5)])
    bunkers = [f for f in r.facts(152) if "a bunker" in f]
    assert bunkers == ["Hole layout: a bunker right, about 45 to 55 yards out.",
                       "Hole layout: a bunker right, about 125 to 140 yards out.",
                       "Hole layout: a bunker left, about 130 to 135 yards out."]


def _canopy(img, box, seed=1):
    """Paint a leafy (textured dark-green) ellipse, as GSPro draws a tree from above."""
    import random

    from PIL import ImageDraw

    rnd = random.Random(seed)
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).ellipse(box, fill=255)
    leaves = Image.new("RGB", img.size)
    leaves.putdata([(30, 70, 20) if rnd.random() < 0.5 else (75, 125, 45) for _ in range(img.width * img.height)])
    img.paste(leaves, (0, 0), mask)
    return img


def test_tree_patches_finds_trees_and_a_stymie():
    from commentator.minimap import tree_patches

    # Ball at (230, 760), pin 600 px straight up at (230, 160): 1 px = 0.25 yd at 150 to the pin.
    img = _course([])
    _canopy(img, (210, 650, 250, 690))             # on the line, 70..110 px out: ~18-28 yd, in the way
    _canopy(img, (150, 300, 200, 360), seed=2)     # left, 30-80 px off (7-20 yd), ~100-115 yd out
    _canopy(img, (20, 400, 60, 440), seed=3)       # 170+ px off the line (40+ yd): outside the corridor
    trees = tree_patches(img, (0.5, 760 / 836), (0.5, 160 / 836), to_pin=150)
    assert len(trees) == 2
    blocker, left = trees
    assert blocker[5] is not None and 60 <= blocker[5] * 600 <= 80
    assert left[0] == "left" and left[5] is None and 95 <= left[1] * 150 <= 105


def test_tree_facts_stymie_dogleg_and_pin_spot():
    from commentator.minimap import Tree

    r = MinimapRead(threshold=0.7, sides=True, to_pin_hint=152,
                    seen={"front_shown": 0.9, "back_shown": 0.9}, numbers={"front": 141, "back": 168},
                    trees=[Tree("right", 0.1, 0.2, 0.95, blocks=0.12), Tree("left", 0.5, 0.8, 0.9),
                           Tree("left", 0.3, 0.4, 0.6)],
                    choices={"pin_side": ("right", 0.9), "fairway": ("right", 0.96)})
    facts = r.facts(152)
    assert "Hole layout: a tree stands right on the line to the pin, about 20 yards ahead." in facts
    assert r.stymied(152)
    assert not MinimapRead(threshold=0.7, trees=[Tree("left", 0.5, 0.8, 0.9)]).stymied(152)
    assert "Hole layout: trees close to the left of the line, about 80 to 120 yards out." in facts
    assert not any("40 to 60" in f for f in facts)                 # Jev wasn't sure of that one
    assert not any("dogleg" in f for f in facts)                   # an approach: no dogleg talk
    assert "Pin: on the right side of the green, middle front to back." in facts
    # From the tee the fairway bowing right of the straight line is a dogleg left.
    assert "Hole shape: a dogleg left, the fairway bending left toward the green." in r.facts(420, "tee")
    assert not any("dogleg" in f for f in r.facts(420, "rough"))   # off the tee the fairway is always off-line
    unsure = MinimapRead(threshold=0.7, sides=True, choices={"pin_side": ("left", 0.6), "fairway": ("left", 0.7)})
    assert not any(f.startswith(("Pin", "Hole shape")) for f in unsure.facts(420, "tee"))
    # A lone tree gets one distance, the middle of its patch; a stand keeps its range.
    lone = MinimapRead(threshold=0.7, sides=True, trees=[Tree("right", 0.77, 1.09, 0.94, single=0.74),
                                                         Tree("left", 0.4, 0.8, 0.95, single=0.6)])
    assert "Hole layout: a tree right of the line, about 140 yards out." in lone.facts(152)
    assert "Hole layout: trees close to the left of the line, about 60 to 120 yards out." in lone.facts(152)
    # A lone tree in the way is placed at its trunk (just short of the canopy's far edge) and isn't
    # listed again beside the line.
    lone_in_way = MinimapRead(threshold=0.7, sides=True,
                              trees=[Tree("right", 0.02, 0.14, 0.9, blocks=0.04, single=0.88)]).facts(285)
    assert lone_in_way == ["Hole layout: a tree stands right on the line to the pin, about 30 yards ahead."]
    # Its patch can run back toward the ball (Pebble 18, the tree 60 yards out read as 13 to 70).
    tail = MinimapRead(threshold=0.7, sides=True,
                       trees=[Tree("right", 0.04, 0.22, 0.93, blocks=0.13, single=0.91)]).facts(314)
    assert tail == ["Hole layout: a tree stands right on the line to the pin, about 60 yards ahead."]
    back = MinimapRead(threshold=0.7, seen={"front_shown": 0.9, "back_shown": 0.9}, numbers={"front": 12, "back": 47})
    assert "Pin: toward the back of the green." in back.facts(37)


async def test_jev_read_asks_the_pin_side_only_on_an_approach():
    fake = FakeJev(choices={"pin_side": ("left", 0.9)})
    r = await JevMinimap(Config().minimap, client=fake).read(_course([]), to_pin=150)
    assert "green" in fake.calls[2][1] and "pin_side" in fake.calls[2][2]
    assert "Pin: on the front left of the green." in r.facts(150)   # the fake's green is 150 to 172
    far = FakeJev()
    await JevMinimap(Config().minimap, client=far).read(_course([]), to_pin=450)
    assert "green" not in far.calls[2][1]


def test_a_bunker_where_the_shot_lands_outranks_out_of_bounds():
    # Desert Canyon par 3: OB right and a bunker right 125-140 short of a 143 pin.
    r = MinimapRead(threshold=0.7, sides=True, to_pin_hint=143, seen={"ob_right": 0.95, "ob_left": 0.9},
                    sand=[Sand("right", 125 / 143, 140 / 143, 0.96), Sand("left", 30 / 143, 40 / 143, 0.9)])
    assert r.named(143) == {"right": "bunker", "left": "out of bounds"}   # a bunker at 30 isn't where it lands
    # From a 480 tee a bunker at drive distance counts; one by the green doesn't.
    tee = MinimapRead(threshold=0.7, sides=True, to_pin_hint=480, seen={"ob_right": 0.95, "ob_left": 0.95},
                      sand=[Sand("right", 260 / 480, 280 / 480, 0.9), Sand("left", 460 / 480, 475 / 480, 0.9)])
    assert tee.named(480) == {"right": "bunker", "left": "out of bounds"}


async def test_an_aim_point_dot_short_of_the_pin_sets_the_scale():
    from commentator.minimap import Tree

    # A long tee shot whose map stops short of the green: the dot is the 201-yard aim point.
    fake = FakeJev(aim=201, choices={"fairway": ("right", 0.95)})
    r = await JevMinimap(Config().minimap, client=fake).read(_course([]), to_pin=365)
    assert r.aim == 201
    assert any(c[1] == ["spot"] for c in fake.calls)   # a call of its own
    r.trees = [Tree("left", 0.64, 0.72, 0.94), Tree("right", 0.1, 0.2, 0.95, blocks=0.15)]
    facts = r.facts(365, "tee")
    assert "Hole layout: trees close to the left of the line, about 130 to 140 yards out." in facts
    assert "Hole layout: a tree stands right on the target line, about 30 yards ahead." in facts
    assert not any(f.startswith(("Hole shape", "Pin")) for f in facts)   # the pin isn't on the map
    # The pin's own dot (or no number) keeps the to-pin scale; short shots never ask.
    r = await JevMinimap(Config().minimap, client=FakeJev(aim=350)).read(_course([]), to_pin=365)
    assert r.aim is None
    fake = FakeJev(aim=100)
    r = await JevMinimap(Config().minimap, client=fake).read(_course([]), to_pin=150)
    assert r.aim is None and not any("spot" in c[1] for c in fake.calls)


def test_aim_dots_and_the_turn_off_the_tee_make_a_dogleg():
    from PIL import ImageDraw

    from commentator.minimap import _turn, aim_dots

    img = Image.new("RGB", (459, 834), (60, 100, 50))
    d = ImageDraw.Draw(img)
    d.ellipse((217, 134, 233, 150), fill="white")            # the pin's dot, bigger
    d.ellipse((114, 330, 126, 342), fill=(225, 225, 225))    # the aim dot, up and left
    d.rectangle((350, 400, 380, 410), fill="white")          # a long roof, not a dot
    ball, pin = (0.49, 0.90), (0.49, 0.17)
    dots = aim_dots(img, ball, pin)
    assert len(dots) == 1 and abs(dots[0][0] - 120 / 459) < 0.01
    assert _turn(ball, pin, dots[0], 459, 834) < -10           # the fairway heads off left
    r = MinimapRead(threshold=0.7, sides=True, aim_turn=-14, choices={"fairway": ("along", 0.9)})
    assert "Hole shape: a dogleg right, the fairway bending right toward the green." in r.facts(373, "tee")
    assert not any("dogleg" in f for f in MinimapRead(threshold=0.7, sides=True, aim_turn=-4).facts(373, "tee"))
    assert not any("dogleg" in f for f in r.facts(373, "fairway"))


async def test_jev_read_confirms_the_aim_dot_in_its_own_call():
    from PIL import ImageDraw

    img = _course([])
    w, h = img.size
    ImageDraw.Draw(img).ellipse((round(0.2 * w) - 6, round(0.5 * h) - 6, round(0.2 * w) + 6, round(0.5 * h) + 6),
                                fill=(225, 225, 225))
    fake = FakeJev()
    r = await JevMinimap(Config().minimap, client=fake).read(img, to_pin=400)
    spot_call = next(c for c in fake.calls if "spot" in c[1])
    assert "dot0" in spot_call[1] and "dot0" in spot_call[2]
    assert r.aim_turn is None    # the fake says 0.1: not the aim marker


def test_a_red_line_without_water_is_a_penalty_area():
    r = MinimapRead(threshold=0.7, sides=True, to_pin_hint=400,
                    seen={"penalty_left": 0.98, "water_left": 0.1, "penalty_right": 0.95, "water_right": 0.9})
    facts = r.facts(400)
    assert "Hole layout: a penalty area (red stakes) down the left." in facts
    assert "Hole layout: water down the right." in facts
    assert not any("penalty area" in f and "right" in f for f in facts)
    assert Hazard("penalty area", "left") in r.hazards(400)
    assert r.named(400) == {"left": "penalty area", "right": "water"}


def test_side_hazards_need_a_big_miss_and_par4_tee_shots_have_no_length_call():
    from commentator.flight_pick import FlightPlan, pick_line
    from commentator.models import Shot

    lines = {o: o for o in ("right_trouble", "right", "short", "on_target", "left")}
    plan = FlightPlan(player=None, to_pin="526 yards", hazards=[Hazard("water", "right")], lines=lines,
                      named={"right": "water"})
    # A driver bending gently right into the fairway (Cabot Cliffs) isn't near the water.
    fade = Shot(club="DR", carry=216, hla=-2.6, spin_axis=14.7, total_spin=4211)
    assert pick_line(plan, fade, "526 yards")[0] == "right"
    slice_ = Shot(club="DR", carry=240, hla=4.5, spin_axis=18)
    assert pick_line(plan, slice_, "526 yards")[0] == "right_trouble"
    # A 3-wood laid back off the tee of a 273-yard par 4 isn't "short".
    plan = FlightPlan(player=None, to_pin="273 yards", lines=lines)
    layup = Shot(club="3W", carry=234, hla=0.0, spin_axis=0)
    assert pick_line(plan, layup, "273 yards")[0] == "short"
    plan.lengthless = True
    assert pick_line(plan, layup, "273 yards")[0] == "on_target"


def _lined_map():
    """A 459x834 map, ball bottom middle and pin straight up: fairway grass left of x 300, a red
    penalty line at x 300 with rock beyond it, and a white out-of-bounds line far left at x 40."""
    from PIL import ImageDraw

    img = Image.new("RGB", (459, 834), (100, 140, 70))           # grass
    d = ImageDraw.Draw(img)
    d.rectangle((300, 0, 458, 833), fill=(90, 80, 70))            # rock right of the red line
    d.line([(300, 100), (300, 760)], fill=(210, 10, 10), width=6)
    d.line([(40, 0), (40, 833)], fill=(255, 255, 255), width=4)
    d.text((200, 400), "186", fill=(255, 255, 255))               # a yardage number isn't a line
    d.ellipse((220, 744, 232, 756), fill=(220, 20, 20))           # the ball's red dot isn't one either
    return img, (226 / 459, 0.9), (226 / 459, 0.15)


def test_red_and_white_lines_are_measured_off_the_line_of_play():
    from commentator.minimap import boundary_lines

    img, ball, pin = _lined_map()
    lines = {(b.kind, b.side): b for b in boundary_lines(img, ball, pin)}
    assert set(lines) == {("penalty area", "right"), ("out of bounds", "left")}
    # 625 px ball to pin is 300 yards: the red line is 74 px (~36 yd) right, the white 186 px (~89 yd) left.
    assert abs(lines["penalty area", "right"].nearest() * 300 - 36) < 4
    assert abs(lines["out of bounds", "left"].nearest() * 300 - 89) < 4
    r = MinimapRead(threshold=0.7, sides=True, to_pin_hint=300, lines=list(lines.values()))
    facts = r.facts(300)
    assert any(f.startswith("Hole layout: a penalty area (red stakes) down the right, about 35 yards off") for f in facts)
    assert not any("out of bounds" in f for f in facts)   # 90 yards off the line isn't part of the hole
    pen = next(h for h in r.hazards(300) if h.kind == "penalty area")
    assert pen.side == "right" and pen.edge and min(off for _, off in pen.edge) >= 30
    # The ground past the out-of-bounds line is greyed before Jev looks for water in it.
    inside = _out_of_play_greyed(img, ball, pin)
    assert inside.getpixel((10, 400)) == (128, 128, 128) and inside.getpixel((150, 400)) == (100, 140, 70)


def test_only_ground_cut_off_by_a_white_line_is_greyed_as_out_of_bounds():
    from PIL import ImageDraw

    # GSPro's dark frame above and below the map, a white line across the top-left corner only, and
    # a white-outlined block of houses with a lake beside it: just the corner and the block are out.
    img = Image.new("RGB", (459, 834), (100, 140, 70))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, 458, 80), fill=(5, 25, 24))
    d.rectangle((0, 815, 458, 833), fill=(5, 25, 24))
    d.line([(0, 300), (150, 81)], fill=(255, 255, 255), width=4)
    d.rectangle((20, 450, 140, 600), outline=(255, 255, 255), width=4)
    d.rectangle((20, 610, 140, 760), fill=(170, 200, 210))           # the lake, in play
    ball, pin = (226 / 459, 0.9), (226 / 459, 0.15)
    out = _out_of_play_greyed(img, ball, pin)
    assert out.getpixel((20, 150)) == (128, 128, 128)               # the cut-off corner
    assert out.getpixel((80, 525)) == (128, 128, 128)               # inside the houses' boundary
    assert out.getpixel((20, 700)) == (170, 200, 210)               # the lake below the corner stays
    assert out.getpixel((300, 400)) == (100, 140, 70)
    assert out.getpixel((200, 40)) == (5, 25, 24)                   # the frame itself isn't touched


def test_a_red_line_whose_hazard_spans_the_line_of_play_is_on_the_bare_side():
    from PIL import ImageDraw

    from commentator.minimap import boundary_lines

    # A dogleg left round a cliff: the fairway is left of a red line that sits LEFT of the straight
    # line to the pin, and the rock (the hazard) is to its right, under the line of play.
    img = Image.new("RGB", (459, 834), (90, 80, 70))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, 170, 833), fill=(100, 140, 70))
    d.line([(170, 100), (170, 760)], fill=(210, 10, 10), width=6)
    lines = boundary_lines(img, (226 / 459, 0.9), (226 / 459, 0.15))
    assert [(b.kind, b.side) for b in lines] == [("penalty area", "right")]
    assert lines[0].nearest() == 0
    r = MinimapRead(threshold=0.7, sides=True, to_pin_hint=300, lines=lines)
    assert any("down the right, reaching the line to the pin" in f for f in r.facts(300))


def test_a_measured_side_hazard_is_in_play_only_for_a_miss_that_reaches_it():
    from commentator.flight_pick import _in_play

    far = Hazard("out of bounds", "right", edge=((200, 90.0), (250, 85.0), (300, 80.0)))
    assert not _in_play(far, 235, 57)        # Cabot 11: 57 yards right, the stakes 85 out
    assert _in_play(far, 235, 84)
    near = Hazard("penalty area", "right", edge=((100, 40.0), (250, 0.0)))
    assert _in_play(near, 240, 15)           # the line runs over it where the ball lands
    assert not _in_play(near, 120, 15)       # short of where it crosses, it's 40 off
    assert not _in_play(Hazard("water", "left", edge=((80, 5.0),)), 240, -30)   # nowhere near the landing


async def test_a_measured_edge_reaches_the_flight_pick_through_the_preshot_plan(tmp_path):
    from commentator.flight_pick import _in_play
    from commentator.minimap import Boundary

    engine, _ = _engine(tmp_path)
    # Red stakes 40 yards right of a 300-yard line, the whole way.
    stakes = Boundary("penalty area", "right", [(a / 20, 40 / 300) for a in range(1, 21)])

    async def jev():
        return MinimapRead(threshold=0.7, sides=True, lines=[stakes])

    hud = Hud(player_text="Tony", shot_number=1, to_pin="300 yards", lie="tee")
    raws, _ = await engine._preshot_model(hud, None, [b"jpeg"], asyncio.create_task(jev()))
    plan = engine._build_plan(json.loads(raws["hazards"]), None, "300 yards", "", seen_image=False)
    [pen] = plan.hazards
    assert pen.kind == "penalty area" and pen.edge and all(off == 40 for _, off in pen.edge)
    assert not _in_play(pen, 240, 20) and _in_play(pen, 240, 38)
    # A malformed edge from the JSON is dropped rather than crashing the plan.
    bad = engine._build_plan({"hazards": [{"kind": "water", "side": "left", "edge": [[1], "x", [2, None]]}]},
                             None, "300 yards", "", seen_image=False)
    assert bad.hazards[0].edge == ()


def test_patches_searched_for_an_aim_point_are_kept_on_the_scale_the_read_settles_on():
    # Bunkers and trees are found on a wide search (the map may stop at a near aim point) and only
    # those within the corridor on the final scale are kept.
    sand = [Sand("right", 0.8, 0.85, 0.95, off=0.2)]             # 0.2 of the ball-to-dot distance off the line
    trees = [Tree("left", 0.2, 0.3, 0.95, off=0.05, blocks=0.3)]
    pin = MinimapRead(threshold=0.7, sides=True, sand=sand, trees=trees)
    # Measured against a 300-yard pin, the bunker is 60 yards off (outside 35) and the trees block
    # 90 yards out (past 60): no bunker, and trees but no stymie.
    assert not pin._sand(300) and pin._trees(300)[0].blocks is None
    aimed = MinimapRead(threshold=0.7, sides=True, sand=sand, trees=trees, aim=150)
    # Against a 150-yard aim point: the bunker is 30 off and the trees block at 45.
    assert aimed._sand(300) and aimed._trees(300)[0].blocks == 0.3
    assert trees[0].blocks == 0.3                                   # the read itself isn't changed


def test_a_red_line_running_across_the_hole_gets_no_left_or_right_side():
    from PIL import ImageDraw

    from commentator.minimap import boundary_lines

    # Gorse across the fairway 150 yards out (a 300-yard hole): grass short of the line, scrub beyond.
    img = Image.new("RGB", (459, 834), (100, 140, 70))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 100, 458, 440), fill=(90, 80, 70))
    d.line([(0, 440), (458, 440)], fill=(210, 10, 10), width=6)
    assert boundary_lines(img, (226 / 459, 0.9), (226 / 459, 0.15)) == []


def test_sand_behind_the_ball_is_not_a_bunker_ahead():
    from PIL import ImageDraw

    from commentator.minimap import sand_patches

    img = Image.new("RGB", (459, 834), (100, 140, 70))
    d = ImageDraw.Draw(img)
    d.ellipse((150, 790, 200, 815), fill=(215, 195, 150))      # behind the ball (ball at y 750)
    d.ellipse((240, 600, 290, 630), fill=(215, 195, 150))      # ahead and right
    found = sand_patches(img, (226 / 459, 0.9), (226 / 459, 0.15), 0.3)
    assert [p[0] for p in found] == ["right"]


def test_a_bunker_beside_the_ball_is_measured_only_over_its_part_ahead():
    from PIL import ImageDraw

    from commentator.minimap import sand_patches

    img = Image.new("RGB", (459, 834), (100, 140, 70))
    ImageDraw.Draw(img).rectangle((245, 725, 270, 815), fill=(215, 195, 150))   # ball at y 750
    (_, near, far, _, _, _), = sand_patches(img, (226 / 459, 0.9), (226 / 459, 0.15), 0.3)
    assert far * 625 < 55   # its far corner ahead is ~51 px off, not the ~78 px of the one behind
    assert near * 625 < 25


def test_a_bunker_off_line_ahead_is_not_let_in_by_its_tail_behind_the_ball():
    from PIL import ImageDraw

    from commentator.minimap import sand_patches

    img = Image.new("RGB", (459, 834), (100, 140, 70))
    d = ImageDraw.Draw(img)
    d.rectangle((235, 760, 250, 800), fill=(215, 195, 150))   # close to the line, behind the ball
    d.rectangle((235, 760, 420, 775), fill=(215, 195, 150))
    d.rectangle((400, 600, 420, 775), fill=(215, 195, 150))   # far right, ahead
    assert sand_patches(img, (226 / 459, 0.9), (226 / 459, 0.15), 0.1) == []


def test_a_white_line_across_the_hole_beyond_the_green_greys_the_ground_past_it():
    from PIL import ImageDraw

    from commentator.minimap import boundary_lines

    img = Image.new("RGB", (459, 834), (100, 140, 70))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 150, 458, 230), fill=(40, 70, 140))        # water past the line
    d.line((0, 240, 458, 240), fill=(255, 255, 255), width=3)
    ball, pin = (226 / 459, 0.9), (226 / 459, 0.4)
    lines = boundary_lines(img, ball, pin)
    assert not any(b.kind == "out of bounds" and b.side in ("left", "right") for b in lines)
    assert _out_of_play_greyed(img, ball, pin).getpixel((226, 190)) == (128, 128, 128)


def test_white_outlined_ground_the_straight_line_cuts_across_is_still_out():
    from PIL import ImageDraw

    img = Image.new("RGB", (459, 834), (100, 140, 70))
    d = ImageDraw.Draw(img)
    d.rectangle((150, 350, 300, 550), fill=(30, 60, 130), outline=(255, 255, 255), width=3)
    ball, pin = (226 / 459, 0.9), (226 / 459, 0.2)
    grey = _out_of_play_greyed(img, ball, pin)
    assert grey.getpixel((226, 450)) == (128, 128, 128)
    assert grey.getpixel((226, 700)) == (100, 140, 70)
    assert grey.getpixel((226, 250)) == (100, 140, 70)


def test_sand_and_trees_past_a_white_line_are_left_out():
    from PIL import ImageDraw

    from commentator.minimap import out_of_play, sand_patches, tree_patches

    img = Image.new("RGB", (459, 834), (100, 140, 70))
    d = ImageDraw.Draw(img)
    d.ellipse((270, 450, 310, 480), fill=(215, 195, 150))        # sand past the line
    for y in range(500, 560, 4):                                 # a speckled canopy past it too
        for x in range(285, 335, 4):
            d.rectangle((x, y, x + 1, y + 1), fill=(20, 60, 15) if (x + y) % 8 else (60, 110, 40))
    d.line((250, 0, 250, 833), fill=(255, 255, 255), width=3)
    ball, pin = (226 / 459, 0.9), (226 / 459, 0.2)
    assert sand_patches(img, ball, pin, 0.3)                      # found without the mask...
    out = out_of_play(img, ball, pin)
    assert sand_patches(img, ball, pin, 0.3, out) == []           # ...and dropped with it
    canopy = [t for t in tree_patches(img, ball, pin, 160) if t[3] > 0.08]   # not the line's own edge
    assert canopy
    assert not [t for t in tree_patches(img, ball, pin, 160, out) if t[3] > 0.08]


async def test_a_bunker_off_the_line_is_in_play_only_for_a_miss_that_reaches_it(tmp_path):
    from commentator.flight_pick import FlightPlan, pick_line
    from commentator.models import Shot

    engine, _ = _engine(tmp_path)
    # A bunker 240-260 yards out on a 400-yard hole, its near edge 32 yards left of the line.
    sand = [Sand("left", 0.6, 0.65, 0.95, off=0.08, wide=0.12)]

    async def jev():
        return MinimapRead(threshold=0.7, sides=True, sand=sand)

    hud = Hud(player_text="Tony", shot_number=1, to_pin="400 yards", lie="tee")
    raws, _ = await engine._preshot_model(hud, None, [b"jpeg"], asyncio.create_task(jev()))
    plan = engine._build_plan(json.loads(raws["hazards"]), None, "400 yards", "", seen_image=False)
    [bunker] = plan.hazards
    assert (bunker.off_yards, bunker.wide_yards) == (32, 48)
    plan = FlightPlan(player=None, to_pin="400 yards", hazards=plan.hazards,
                      lines={"left_trouble": "That bunker comes into play.", "left": "Pulled left."},
                      named={"left": "bunker"})
    # A 250-yard drive ~17 yards left doesn't get out to it; ~30 yards left does.
    assert pick_line(plan, Shot(club="DR", carry=250, hla=-4), "400 yards")[0] == "left"
    assert pick_line(plan, Shot(club="DR", carry=250, hla=-7), "400 yards")[0] == "left_trouble"
    # ...and a shank 86 yards left is well past it.
    assert pick_line(plan, Shot(club="DR", carry=250, hla=-20), "400 yards")[0] == "left"


def test_merged_bunker_pieces_keep_the_nearest_distance_off_the_line():
    r = MinimapRead(threshold=0.7, sides=True, sand=[
        Sand("right", 0.80, 0.85, 0.95, off=0.11, wide=0.15), Sand("right", 0.84, 0.90, 0.95, off=0.10, wide=0.13)])
    [b] = r._sand(300)
    assert (b.off, b.wide) == (0.10, 0.15)
    [h] = r.hazards(300)
    assert (h.off_yards, h.wide_yards) == (30, 45)


def test_strategy_says_where_the_miss_is():
    # One bunker short of the green on a 150-yard approach, nothing read long: short is the miss to
    # avoid. The blobs are counted, so one is "a bunker": calling it "bunkers" let a line invent the
    # rest of them, since the checker takes a plural in the facts as backing a plural on air.
    r = MinimapRead(threshold=0.7, sides=True, seen={"front_shown": 0.9, "back_shown": 0.9},
                    numbers={"front": 140, "back": 165}, sand=[Sand("left", 125 / 150, 138 / 150, 0.95, off=0.02)])
    assert r.strategy(150, "fairway") == ("Strategy: a bunker short of the green, so coming up short is the miss "
                                          "to avoid; nothing read past the green.")
    two = MinimapRead(threshold=0.7, sides=True, seen={"front_shown": 0.9, "back_shown": 0.9},
                      numbers={"front": 140, "back": 165},
                      sand=[Sand("left", 125 / 150, 138 / 150, 0.95, off=0.02),
                            Sand("right", 120 / 150, 132 / 150, 0.95, off=0.04)])
    assert two.strategy(150, "fairway").startswith("Strategy: bunkers short of the green")
    # Water past the green (a red line beyond the pin with water behind it) is worse than the sand short.
    r.past = [Past("penalty area", [(1.15, 0.02), (1.3, 0.4)], water=True)]
    assert r.strategy(150, "fairway") == ("Strategy: water past the green and a bunker short of the green, so going "
                                          "long is the miss to avoid; coming up short finds sand at worst.")
    r.past = [Past("penalty area", [(1.5, 0.0)], water=True)]   # 75 yards past: not the green's trouble
    assert "past the green" not in r.strategy(150, "fairway").split(";")[0]
    # Water in front.
    assert "water short of the green" in MinimapRead(threshold=0.7, sides=True, seen={"water_ahead": 0.9}).strategy(120)
    # Off a long tee, out of bounds down one side only: the other side is the safe miss.
    tee = MinimapRead(threshold=0.7, sides=True, seen={"ob_left": 0.9})
    assert tee.strategy(400, "tee") == "Strategy: out of bounds down the left, so the right side is the safer miss off the tee."
    assert tee.strategy(400, "fairway") is None   # a long second isn't a tee shot or an approach
    # Nothing by the green, a putt, or no trusted sides: nothing to say.
    far = MinimapRead(threshold=0.7, sides=True, sand=[Sand("right", 0.2, 0.3, 0.95, off=0.02)])
    assert far.strategy(150, "fairway") is None
    assert r.strategy(150, "green") is None
    assert MinimapRead(threshold=0.7, seen={"water_ahead": 0.9}).strategy(120) is None


def test_past_trouble_finds_water_beyond_the_green_on_a_real_map():
    # Doral's 18th from 42 yards: a red line past the green with the lake behind it; the red line down
    # the left behind the ball isn't past the pin, and nothing white crosses beyond it.
    from PIL import Image
    from commentator.minimap import past_trouble
    img = Image.open("tests/fixtures/minimap_water_long.jpg")
    found = past_trouble(img, (0.495, 0.765), (0.5, 0.305))
    assert [(p.kind, p.water) for p in found] == [("penalty area", True)]
    assert 0 < found[0].nearest(42, 20) < 15


def _out_of_play_greyed(img, ball, pin):
    """The map as the Jev read sees it: the ground out of play greyed out."""
    from commentator.minimap import _greyed, out_of_play

    return _greyed(img, out_of_play(img, ball, pin))
