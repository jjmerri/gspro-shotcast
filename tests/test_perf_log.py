import asyncio
import json
import time
from datetime import date

from commentator import perf_log


def test_work_is_timed_sync_and_async_and_handed_over_once():
    perf_log._take_work()

    @perf_log.timed("ocr")
    def read():
        return "ok"

    @perf_log.timed("model")
    async def call():
        return 2

    assert read() == "ok" and asyncio.run(call()) == 2
    read()
    work = perf_log._take_work()
    assert work["ocr"][0] == 2 and work["model"][0] == 1
    assert perf_log._take_work() == {}


def test_gpu_takes_each_process_busiest_engine_and_the_pc_busiest_engine():
    a3d, b3d, enc = "luid_0x1_phys_0_eng_0_engtype_3D", "luid_0x2_phys_0_eng_0_engtype_3D", "luid_0x1_phys_0_eng_4_engtype_VideoEncode"
    per = {10: {a3d: 40.0, enc: 10.0}, 20: {a3d: 30.0}, 30: {b3d: 50.0}, 40: {enc: 45.0}}
    assert perf_log.gpu_busiest(per, {10}) == 40.0 and perf_log.gpu_busiest(per, {30}) == 50.0
    assert perf_log.gpu_busiest(per) == 70.0               # the two adapters' 3D engines aren't added
    assert perf_log.gpu_busiest(per, {30, 40}) == 50.0     # nor one process's 3D to another's encode


def test_work_still_going_counts_its_seconds_in_each_line_and_finishes_once(monkeypatch):
    perf_log._take_work()
    clock = iter([0.0, 3.0, 5.0, 6.0])
    monkeypatch.setattr(perf_log.time, "perf_counter", lambda: next(clock))
    with perf_log.work("publish"):
        assert perf_log._take_work() == {"publish": [0, 3.0, 0.0]}
    assert perf_log._take_work() == {"publish": [1, 2.0, 5.0]}


def test_work_at_a_round_edge_lands_in_the_right_round_and_a_restart_closes_it(tmp_path, monkeypatch):
    sampler = perf_log.Sampler(tmp_path)
    monkeypatch.setattr(perf_log, "_sampler", sampler)
    perf_log._take_work()
    sample = {"cpu": 10, "booth": {"cpu": 1, "mb": 1, "n": 1}}
    perf_log.note("ocr", 1.0)
    perf_log.mark("round_start", round=1)     # before the round: not the round's
    sampler.write(sample)
    perf_log.note("model", 2.0)
    perf_log.mark("round_end", round=1)       # the round's last seconds: the round's
    perf_log.mark("round_start", round=2)
    sampler.write(sample)
    sampler.write({"mark": "session_start"})  # the booth restarted mid-round
    sampler.write({**sample, "cpu": 99})
    sampler.flush()
    text = "\n".join(perf_log.summarize(tmp_path))
    assert "Round 1: " in text and "model 1x 2s" in text and "ocr" not in text
    assert "Round 2 (the booth stopped)" in text and "99" not in text


def test_a_sampler_that_wont_start_leaves_the_booth_running(tmp_path, monkeypatch):
    monkeypatch.setattr(perf_log, "_sampler", None)
    monkeypatch.setattr(perf_log.Sampler, "start", lambda self: (_ for _ in ()).throw(RuntimeError("no")))
    assert perf_log.start(tmp_path) is None and perf_log._sampler is None


def test_summary_is_per_round_with_work_and_busiest_moments(tmp_path):
    rows = [{"t": "2026-11-20T10:00:00", "mark": "round_start", "round": 150},
            *({"t": f"2026-11-20T10:00:{i:02d}", "cpu": 20 + i, "mem": 50,
               "booth": {"cpu": i, "mb": 400, "n": 2}, "gspro": {"cpu": 15, "mb": 3000},
               "gpu": {"all": 70, "gspro": 65, "booth": 1}, "work": {"ocr": [2, 0.5, 0.3]}, "self_ms": 0.3}
              for i in range(1, 11)),
            {"t": "2026-11-20T10:01:00", "mark": "round_end", "round": 150},
            {"t": "2026-11-20T10:02:00", "cpu": 99, "booth": {"cpu": 99, "mb": 1, "n": 1}}]   # between rounds
    (tmp_path / "perf-2026-11-20.jsonl").write_text("\n".join(map(json.dumps, rows)) + "\nnot json\n")
    text = "\n".join(perf_log.summarize(tmp_path))
    assert "Round 150: 11-20 10:00 to 10:00, 10 samples" in text
    assert "GSPro CPU avg 15%" in text and "Booth CPU avg 6% p95 10% max 10%" in text and "99" not in text
    assert "GPU all avg 70%" in text and "gspro avg 65%" in text
    assert "ocr 20x 5s (longest 0.3s)" in text and "10:00:10 booth 10% GSPro 15% (ocr 0.5s)" in text


def test_old_days_are_pruned(tmp_path):
    for day in ("2026-11-01", "2026-11-19", "notes"):
        (tmp_path / f"perf-{day}.jsonl").write_text("")
    perf_log.prune(tmp_path, date(2026, 11, 20))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["perf-2026-11-19.jsonl", "perf-notes.jsonl"]


def test_a_failing_sample_is_skipped_and_stop_writes_whats_queued(tmp_path, monkeypatch):
    monkeypatch.setattr(perf_log, "_sampler", None)
    monkeypatch.setattr(perf_log.Sampler, "sample", lambda self: 1 / 0)
    sampler = perf_log.start(tmp_path, interval=0.01)
    time.sleep(0.1)
    perf_log.mark("round_end", round=4)
    perf_log.stop()
    rows = [json.loads(line) for p in tmp_path.glob("perf-*.jsonl") for line in p.read_text().splitlines()]
    assert not sampler._thread.is_alive() and rows[0]["mark"] == "session_start"
    assert sum(1 for r in rows if r.get("failed")) >= 2 and any(r.get("mark") == "round_end" for r in rows)


def test_obs_and_its_helpers_are_obs_whoever_started_it(tmp_path, monkeypatch):
    class Proc:
        def __init__(self, pid, born=4e9):
            self.pid, self.born = pid, born

        def create_time(self):
            return self.born

    sampler = perf_log.Sampler(tmp_path)
    me = sampler.me.pid
    sampler.watched = {"obs": Proc(200)}
    sampler.procs = {pid: Proc(pid) for pid in (101, 102, 200, 201, 202, 301)}
    sampler.procs[103] = Proc(103, born=1.0)   # older than the booth: its parent's pid was reused
    tree = {me: 1, 101: me, 102: 101, 103: me, 200: me, 201: 200, 202: 201, 300: 1, 301: 300}   # 300: OBS not ours
    monkeypatch.setattr(sampler.psutil, "_ppid_map", lambda: tree)
    sampler._refresh_tree()
    assert {k: sorted(p.pid for p in v) for k, v in sampler.tree.items()} == {"booth": [101, 102], "obs": [201, 202]}
    sampler.watched = {"obs": Proc(300)}
    sampler._refresh_tree()
    assert sorted(p.pid for p in sampler.tree["obs"]) == [301]
    assert sorted(p.pid for p in sampler.tree["booth"]) == [101, 102, 200, 201, 202]   # 200 isn't OBS now: the booth's
