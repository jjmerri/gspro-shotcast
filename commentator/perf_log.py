"""What the booth costs the PC while GSPro runs, over time, for a problem report.

A background thread wakes every few seconds and writes one line to log_dir/perf-YYYY-MM-DD.jsonl:
the whole PC's CPU and memory, the booth's (with its child processes: the stats publish, ffmpeg),
GSPro's and OBS's, the GPU each uses (Windows, less often: its counters are the dearest read), and
how long the booth spent on each kind of heavy work since the last line (`work`), so a spike in
GSPro's frame rate can be lined up with what the booth was doing. Rounds start and end with a
`mark` line. Each line also says what the sampler itself cost.

Cheap by design: a handful of known processes' counters per tick, the process list walked only to
find GSPro/OBS again when they're gone, the process tree every half minute, below-normal priority (the booth's), nothing written per
piece of work. Never raises into the booth.
"""

from __future__ import annotations

import contextlib
import functools
import inspect
import json
import logging
import os
import sys
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

PREFIX = "perf-"
KEEP_DAYS = 7
INTERVAL_S = 5.0
GPU_EVERY_S = 30.0
FIND_EVERY_S = 60.0       # walking the process list (~10-30 ms): GSPro/OBS when gone
TREE_EVERY_S = 30.0       # one snapshot of every process's parent: who's under the booth, OBS, GSPro
WATCHED = {"gspro": "gspro.exe", "obs": "obs64.exe"}

_lock = threading.Lock()
_emit = threading.Lock()   # handing work over and writing the line it goes on: one step, so a round's work stays in it
_work: dict[str, list[float]] = {}   # kind -> [count, seconds, longest]
_active: dict[int, list] = {}        # work still going: id -> [kind, started, counted up to]
_sampler: "Sampler | None" = None


def note(kind: str, seconds: float) -> None:
    with _lock:
        w = _work.setdefault(kind, [0, 0.0, 0.0])
        w[0] += 1
        w[1] += seconds
        w[2] = max(w[2], seconds)


@contextlib.contextmanager
def work(kind: str):
    """Time a piece of the booth's heavy work (wall clock: a model call's wait counts too). Its seconds
    go to the line they were spent in (a 20 s publish shows in each line it ran through); its count and
    length to the line it ends in."""
    token = object()
    with _lock:   # every clock read under the lock, so a line's cut and the work's ends stay in order
        started = time.perf_counter()
        _active[id(token)] = [kind, started, started]
    try:
        yield
    finally:
        with _lock:
            now = time.perf_counter()
            _, _, counted = _active.pop(id(token))
            w = _work.setdefault(kind, [0, 0.0, 0.0])
            w[0] += 1
            w[1] += now - counted
            w[2] = max(w[2], now - started)


def timed(kind: str):
    """`work` round a whole function, sync or async."""
    def wrap(fn):
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def ainner(*a, **kw):
                with work(kind):
                    return await fn(*a, **kw)
            return ainner

        @functools.wraps(fn)
        def inner(*a, **kw):
            with work(kind):
                return fn(*a, **kw)
        return inner
    return wrap


def _take_work() -> dict[str, list[float]]:
    """The work since the last call: [finished count, seconds spent (unfinished work's too), longest finished]."""
    global _work
    with _lock:
        now = time.perf_counter()
        for a in _active.values():
            w = _work.setdefault(a[0], [0, 0.0, 0.0])
            w[1] += now - a[2]
            a[2] = now
        taken, _work = _work, {}
    return {k: [int(v[0]), round(v[1], 3), round(v[2], 3)] for k, v in taken.items() if v[0] or v[1] >= 0.0005}


def mark(event: str, **fields) -> None:
    """A line of its own, now: a round's start or end. It takes the work done since the last line, so
    a round's last seconds of work end up in it (round_end) and work from before it stays out
    (round_start)."""
    if (sampler := _sampler) is None:
        return
    with _emit:
        row = {"mark": event, **fields}
        if work_done := _take_work():
            row["work"] = work_done
        sampler.write(row)   # queued: the sampler's thread does the file work, straight away
    sampler.wake()


def start(log_dir: str | Path, interval: float = INTERVAL_S) -> "Sampler | None":
    global _sampler
    if _sampler is not None:
        return _sampler
    try:
        import psutil  # noqa: F401
    except ImportError:
        log.info("Perf log off: psutil isn't installed")
        return None
    try:
        sampler = Sampler(Path(log_dir), interval)
        sampler.write({"mark": "session_start"})
        sampler.start()
    except Exception:
        log.exception("Perf log off: the sampler wouldn't start")
        return None
    _sampler = sampler
    return sampler


def stop() -> None:
    global _sampler
    if (sampler := _sampler) is not None:
        _sampler = None
        sampler.stop()


class Sampler:
    def __init__(self, log_dir: Path, interval: float = INTERVAL_S) -> None:
        import psutil

        self.psutil = psutil
        self.log_dir = log_dir
        self.interval = interval
        self.cores = psutil.cpu_count() or 1
        self.me = psutil.Process()
        self.procs: dict[int, object] = {}   # pid -> Process, kept so cpu_percent has a last reading
        self.watched: dict[str, object] = {}
        self.found_at = 0.0
        self.tree: dict[str, list] = {}   # "booth"/watched key -> the processes under it, as of tree_at
        self.tree_at = -TREE_EVERY_S
        self.gpu = _Gpu() if sys.platform == "win32" else None
        self.gpu_at = 0.0
        self.day: date | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()   # a mark queued: write it now, not at the next sample
        self._thread = threading.Thread(target=self._run, name="perf-log", daemon=True)
        self._queue: deque[str] = deque()

    def start(self) -> None:
        self._thread.start()

    def stop(self, wait: float = 2.0) -> None:
        """Stop, and give the thread `wait` seconds to write what's queued."""
        self._stop.set()
        self._wake.set()
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(wait)

    def _run(self) -> None:
        failed_logged = False
        with contextlib.suppress(Exception):
            self.psutil.cpu_percent(None)
        self.flush()
        due = time.monotonic() + self.interval
        while True:
            self._wake.wait(max(0.0, due - time.monotonic()))
            self._wake.clear()
            self.flush()
            if self._stop.is_set():
                break
            if time.monotonic() < due:
                continue
            due = time.monotonic() + self.interval   # from now: a late sample's next still spans an interval
            try:
                cost = time.thread_time()
                row = self.sample()
                row["self_ms"] = round((time.thread_time() - cost) * 1000, 1)
            except Exception:
                if not failed_logged:   # once: a fault that keeps happening would fill the log
                    failed_logged = True
                    log.exception("Perf sample failed (skipped; later ones are tried)")
                row = {"failed": True}
            with _emit:
                if work_done := _take_work():
                    row["work"] = work_done
                self.write(row)
            self.flush()
        self.flush()

    def _cpu(self, p) -> float:
        """% of the whole PC (all cores), as the system figure is."""
        key = p.pid
        if key not in self.procs:
            self.procs[key] = p
            p.cpu_percent(None)
            return 0.0
        return self.procs[key].cpu_percent(None) / self.cores

    def _usage(self, procs: list, seen: dict) -> dict:
        """CPU and memory of `procs`, each read once a tick (`seen`): a reading resets cpu_percent."""
        cpu = mb = 0.0
        for p in procs:
            if p.pid not in seen:
                try:
                    seen[p.pid] = (self._cpu(p), p.memory_info().rss / 2**20)
                except (self.psutil.NoSuchProcess, self.psutil.AccessDenied):
                    self.procs.pop(p.pid, None)
                    seen[p.pid] = (0.0, 0.0)
            cpu += seen[p.pid][0]
            mb += seen[p.pid][1]
        return {"cpu": round(cpu, 1), "mb": round(mb)}

    def _refresh_tree(self) -> None:
        """Who's under the booth and under each watched process (OBS's helpers are OBS's, whoever
        started it), from one snapshot of every process's parent."""
        ps = self.psutil
        # What children() uses: one snapshot, not one per process. If it fails the last tree stands
        # (psutil's per-process fallback would walk every process once per process).
        parents = ps._ppid_map()
        kids: dict[int, list[int]] = {}
        for pid, ppid in parents.items():
            if pid != ppid:
                kids.setdefault(ppid, []).append(pid)

        def under(root) -> list:
            # Windows keeps a dead parent's pid on its children: one started before its "parent" is
            # under whoever had that pid before, not under this one.
            out, stack, seen = [], [root], {root.pid}
            while stack:
                parent = stack.pop()
                born = self._born(parent)
                for c in kids.get(parent.pid, ()):
                    if c in seen or (p := self._proc(c)) is None or self._born(p) < born:
                        continue
                    seen.add(c)
                    out.append(p)
                    stack.append(p)
            return out

        tree = {key: under(p) for key, p in self.watched.items()}
        taken = {p.pid for p in self.watched.values()}.union(*({p.pid for p in v} for v in tree.values()))
        tree["booth"] = [p for p in under(self.me) if p.pid not in taken]
        self.tree = tree

    def _proc(self, pid: int):
        if (p := self.procs.get(pid)) is not None:
            return p
        try:
            return self.psutil.Process(pid)
        except self.psutil.Error:
            return None

    def _born(self, p) -> float:
        try:
            return p.create_time()
        except (self.psutil.Error, AttributeError):
            return 0.0

    def _find_watched(self) -> bool:
        """Look for GSPro/OBS when gone (once a minute); True when one turned up."""
        missing = {k: n for k, n in WATCHED.items() if k not in self.watched}
        if not missing or time.monotonic() - self.found_at < FIND_EVERY_S:
            return False
        self.found_at = time.monotonic()
        found = False
        for p in self.psutil.process_iter(["name"]):
            name = (p.info.get("name") or "").lower()
            for key, want in missing.items():
                if name == want and key not in self.watched:
                    self.watched[key] = p
                    found = True
        return found

    def sample(self) -> dict:
        ps = self.psutil
        row: dict = {"cpu": round(ps.cpu_percent(None), 1), "mem": round(ps.virtual_memory().percent, 1)}
        changed = self._find_watched()
        for key, p in list(self.watched.items()):
            if not p.is_running():
                self.watched.pop(key)
                self.procs.pop(p.pid, None)
                changed = True
        if changed or time.monotonic() - self.tree_at >= TREE_EVERY_S:
            self.tree_at = time.monotonic()
            try:
                self._refresh_tree()
            except Exception:
                log.debug("Perf log: couldn't look at the process tree; the last one stands", exc_info=True)
        self.tree = {key: [p for p in procs if p.is_running()] for key, procs in self.tree.items()}
        booth = [self.me, *self.tree.get("booth", ())]
        groups = {key: [p, *self.tree.get(key, ())] for key, p in self.watched.items()}
        seen: dict = {}
        row["booth"] = {**self._usage(booth, seen), "n": len(booth)}
        for key, procs in groups.items():
            row[key] = self._usage(procs, seen)
        for pid in set(self.procs) - set(seen):
            self.procs.pop(pid)
        if self.gpu is not None and time.monotonic() - self.gpu_at >= GPU_EVERY_S:
            self.gpu_at = time.monotonic()
            per = self.gpu.read()
            if per is not None:
                gpu = {"all": gpu_busiest(per), "booth": gpu_busiest(per, {p.pid for p in booth})}
                for key, procs in groups.items():
                    gpu[key] = gpu_busiest(per, {p.pid for p in procs})
                row["gpu"] = gpu
        return row

    def wake(self) -> None:
        self._wake.set()

    def write(self, row: dict) -> None:
        """Queue a line, in order; `flush` (the sampler's thread) writes it."""
        now = datetime.now()
        self._queue.append(json.dumps({"t": now.isoformat(timespec="seconds"), **row}, separators=(",", ":")))

    def flush(self) -> None:
        while self._queue:
            line = self._queue.popleft()
            day = date.fromisoformat(line[6:16])   # {"t":"YYYY-MM-DD...
            try:
                if day != self.day:
                    self.day = day
                    prune(self.log_dir, day)
                self.log_dir.mkdir(parents=True, exist_ok=True)
                with open(self.log_dir / f"{PREFIX}{day:%Y-%m-%d}.jsonl", "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                log.debug("Perf log write failed", exc_info=True)


def prune(log_dir: Path, today: date) -> None:
    cutoff = today - timedelta(days=KEEP_DAYS)
    for p in log_dir.glob(f"{PREFIX}*.jsonl"):
        try:
            if date.fromisoformat(p.stem[len(PREFIX):]) < cutoff:
                p.unlink()
        except (ValueError, OSError):
            pass


class _Gpu:
    r"""Per-process GPU use from Windows' "GPU Engine" counters (what Task Manager shows): each engine
    instance is named pid_N_luid_..._phys_P_eng_E_engtype_X, one per process per engine. Read as
    pid -> {engine: %}, averaged since the last read. Off for good after the first failure."""

    PATH = r"\GPU Engine(*)\Utilization Percentage"

    def __init__(self) -> None:
        self.query = self.counter = None
        self.ok = True

    def _open(self) -> None:
        import ctypes
        from ctypes import wintypes

        self.pdh = ctypes.windll.pdh
        self.query, self.counter = wintypes.HANDLE(), wintypes.HANDLE()
        if self.pdh.PdhOpenQueryW(None, None, ctypes.byref(self.query)):
            raise OSError("PdhOpenQuery failed")
        if self.pdh.PdhAddEnglishCounterW(self.query, self.PATH, None, ctypes.byref(self.counter)):
            raise OSError("PdhAddEnglishCounter failed (no GPU Engine counters?)")
        self.pdh.PdhCollectQueryData(self.query)

    def read(self) -> dict[int, dict[str, float]] | None:
        if not self.ok:
            return None
        try:
            if self.query is None:
                self._open()
                return None   # a rate needs two collections
            return self._read()
        except Exception as e:
            self.ok = False
            log.info("Perf log: no GPU figures (%s)", e)
            return None

    def _read(self) -> dict[int, dict[str, float]]:
        import ctypes
        from ctypes import wintypes

        Item = _pdh_item()

        if self.pdh.PdhCollectQueryData(self.query):
            raise OSError("PdhCollectQueryData failed")
        size, count = wintypes.DWORD(0), wintypes.DWORD(0)
        fmt_double, more_data = 0x200, 0x800007D2
        rc = self.pdh.PdhGetFormattedCounterArrayW(self.counter, fmt_double, ctypes.byref(size), ctypes.byref(count),
                                                   None) & 0xFFFFFFFF   # a PDH_STATUS comes back as a signed int
        if rc == 0:
            return {}   # no engine in use at all
        if rc != more_data:
            raise OSError(f"PdhGetFormattedCounterArray said {rc:#x}")
        buf = (ctypes.c_byte * size.value)()
        if self.pdh.PdhGetFormattedCounterArrayW(self.counter, fmt_double, ctypes.byref(size), ctypes.byref(count),
                                                 buf):
            raise OSError("PdhGetFormattedCounterArray failed")
        items = ctypes.cast(buf, Item.Pointer)
        per: dict[int, dict[str, float]] = {}
        for i in range(count.value):
            name, value = items[i].szName or "", items[i].FmtValue.doubleValue
            if items[i].FmtValue.CStatus not in (0, 1) or not name.startswith("pid_"):
                continue
            parts = name.split("_", 2)
            try:
                pid, engine = int(parts[1]), parts[2]   # engine: luid_..._phys_P_eng_E_engtype_X
            except (IndexError, ValueError):
                continue
            engines = per.setdefault(pid, {})
            engines[engine] = engines.get(engine, 0.0) + value
        return per


@functools.cache
def _pdh_item():
    """PDH_FMT_COUNTERVALUE_ITEM_W, made once: ctypes keeps every POINTER() type it's asked for."""
    import ctypes
    from ctypes import wintypes

    class Value(ctypes.Structure):
        _fields_ = [("CStatus", wintypes.DWORD), ("doubleValue", ctypes.c_double)]

    class Item(ctypes.Structure):
        _fields_ = [("szName", wintypes.LPWSTR), ("FmtValue", Value)]

    Item.Pointer = ctypes.POINTER(Item)
    return Item


def gpu_busiest(per: dict[int, dict[str, float]], pids: set[int] | None = None) -> float:
    """How busy `pids` (all processes when None) keep the GPU: each engine's use summed over them, the
    busiest engine's (what Task Manager shows)."""
    total: dict[str, float] = {}
    for pid, engines in per.items():
        if pids is None or pid in pids:
            for engine, v in engines.items():
                total[engine] = total.get(engine, 0.0) + v
    return round(min(100.0, max(total.values(), default=0.0)), 1)


# ---- a problem report's reading of it -------------------------------------------------------

def _pct(values: list[float], q: float) -> float:
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))] if values else 0.0


def _rows(paths: list[Path]):
    """The log's lines, read as they're needed (a week of them is ~100k)."""
    for p in paths:
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict):
                        yield row
        except OSError:
            pass


def _spans(rows):
    """Each round's samples (round_start..round_end), oldest first; samples outside a round dropped.
    A round_end's work (its last seconds) joins the round as a line of its own, with no sample."""
    cur, label = None, ""
    for r in rows:
        mark = r.get("mark")
        if mark == "round_start":
            if cur:
                yield label + " (cut short)", cur
            cur, label = [], f"Round {r.get('round', '?')}"
        elif mark == "round_end":
            if cur is not None:
                if r.get("work"):
                    cur.append({"t": r.get("t", ""), "work": r["work"]})
                if cur:
                    yield label, cur
            cur = None
        elif mark == "session_start":
            if cur:
                yield label + " (the booth stopped)", cur
            cur = None
        elif cur is not None and mark is None and "t" in r:
            cur.append(r)
    if cur:
        yield label + " (still going, or the booth stopped)", cur


def _stat(rows: list[dict], key: str, field: str = "cpu") -> str | None:
    vals = [r[key][field] for r in rows if isinstance(r.get(key), dict) and field in r[key]]
    if not vals:
        return None
    return f"avg {sum(vals) / len(vals):.0f}% p95 {_pct(vals, 0.95):.0f}% max {max(vals):.0f}%"


def summarize(log_dir: Path, rounds: int = 5) -> list[str]:
    paths = sorted(log_dir.glob(f"{PREFIX}*.jsonl"))
    if not paths:
        return ["Perf log: none"]
    spans = deque(_spans(_rows(paths)), maxlen=rounds)
    if not spans:
        return [f"Perf log: {len(paths)} file(s), no round in them"]
    lines = [f"Perf log: {len(paths)} file(s); CPU is % of the whole PC, every {INTERVAL_S:.0f} s; GPU every "
             f"{GPU_EVERY_S:.0f} s"]
    for label, rows in spans:
        lines.append(f"{label}: {rows[0]['t'][5:16].replace('T', ' ')} to {rows[-1]['t'][11:16]}, "
                     f"{sum(1 for r in rows if 'cpu' in r)} samples")
        sys_cpu = [r["cpu"] for r in rows if "cpu" in r]
        if sys_cpu:
            lines.append(f"  PC CPU avg {sum(sys_cpu) / len(sys_cpu):.0f}% p95 {_pct(sys_cpu, 0.95):.0f}% "
                         f"max {max(sys_cpu):.0f}%; memory up to {max(r.get('mem', 0) for r in rows):.0f}%")
        for key, name in (("gspro", "GSPro"), ("booth", "Booth"), ("obs", "OBS")):
            if stat := _stat(rows, key):
                mb = max(r[key].get("mb", 0) for r in rows if isinstance(r.get(key), dict))
                lines.append(f"  {name} CPU {stat}; memory up to {mb / 1024:.1f} GB")
        gpu = [r["gpu"] for r in rows if isinstance(r.get("gpu"), dict)]
        if gpu:
            parts = [f"{k} avg {sum(g.get(k, 0) for g in gpu) / len(gpu):.0f}% max {max(g.get(k, 0) for g in gpu):.0f}%"
                     for k in ("all", "gspro", "booth", "obs") if any(k in g for g in gpu)]
            lines.append("  GPU " + "; ".join(parts))
        totals: dict[str, list[float]] = {}
        for r in rows:
            for kind, (n, secs, longest) in (r.get("work") or {}).items():
                t = totals.setdefault(kind, [0, 0.0, 0.0])
                t[0] += n
                t[1] += secs
                t[2] = max(t[2], longest)
        if totals:
            lines.append("  Booth work: " + ", ".join(
                f"{k} {n:.0f}x {secs:.0f}s (longest {longest:.1f}s)"
                for k, (n, secs, longest) in sorted(totals.items(), key=lambda kv: -kv[1][1])))
        busiest = sorted((r for r in rows if isinstance(r.get("booth"), dict)), key=lambda r: -r["booth"]["cpu"])[:3]
        if busiest and busiest[0]["booth"]["cpu"] > 0:
            lines.append("  Booth's busiest moments: " + "; ".join(
                f"{r['t'][11:19]} booth {r['booth']['cpu']:.0f}% GSPro {r.get('gspro', {}).get('cpu', 0):.0f}%"
                + (f" ({', '.join(f'{k} {v[1]:.1f}s' for k, v in r['work'].items())})" if r.get("work") else "")
                for r in busiest))
        costs = [r["self_ms"] for r in rows if "self_ms" in r]
        if costs:
            lines.append(f"  Sampler's own cost: avg {sum(costs) / len(costs):.1f} ms CPU a sample, max {max(costs):.0f} ms")
    return lines
