"""Running frozen commands: templating, timeouts that kill the whole process group, GPU leases,
idle gates, metric parsing, speedup aggregation and ABBA analysis."""

from __future__ import annotations

import fcntl
import json
import math
import os
import re
import shutil
import signal
import statistics
import subprocess
import sys
import time
from pathlib import Path

from myflow.core import MfError, geomean, lock_dir

TEMPLATE_KEYS = ("wt", "lane", "task", "run", "gpu", "seed", "lane_dir", "task_dir", "repo", "base")
_TPL = re.compile(r"\{(" + "|".join(TEMPLATE_KEYS) + r")\}")
_CHILDREN = set()


def expand(s: str, ctx: dict) -> str:
    """Replace only known {keys}; shell braces such as ${VAR} or awk '{print}' stay untouched."""
    return _TPL.sub(lambda m: str(ctx[m.group(1)]) if ctx.get(m.group(1)) is not None else m.group(0), s or "")


def build_env(task: dict, ctx: dict) -> dict:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"  # never measure a stale .pyc (same-second, same-size edits look fresh)
    for k, v in (task.get("env") or {}).items():
        env[k] = expand(str(v), ctx)
    env.update({"MF_TASK": str(ctx.get("task", "")), "MF_LANE": str(ctx.get("lane", "")),
                "MF_RUN_DIR": str(ctx.get("run", "")), "MF_SEED": str(ctx.get("seed", 0)),
                "MF_WT": str(ctx.get("wt", ""))})
    gpu_env = task["gpu"].get("env") or "HIP_VISIBLE_DEVICES"
    if ctx.get("gpu") is not None:
        env[gpu_env] = ",".join(map(str, ctx["gpus"])) if ctx.get("gpus") else str(ctx["gpu"])
        if gpu_env != "CUDA_VISIBLE_DEVICES":
            env.pop("CUDA_VISIBLE_DEVICES", None)  # HIP composes both variables; keep a single source of truth
    return env


# ---------------------------------------------------------------- processes


def _kill_group(p: subprocess.Popen, grace: float = 15.0) -> None:
    try:
        os.killpg(p.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        p.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        p.wait()


def _on_signal(signum, _frame):
    for p in list(_CHILDREN):
        _kill_group(p, grace=5)
    raise SystemExit(128 + signum)


def install_signal_handlers() -> None:
    """If mf itself is killed (agent tool timeout, Ctrl-C), take the GPU job down with it."""
    for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        try:
            signal.signal(s, _on_signal)
        except (ValueError, OSError):
            pass


def purge_pycache(root) -> int:
    """Delete __pycache__ dirs under a worktree so the measured code is always the checked-out source."""
    n = 0
    for dirpath, dirnames, _ in os.walk(str(root)):
        if ".git" in dirnames:
            dirnames.remove(".git")
        if "__pycache__" in dirnames:
            shutil.rmtree(os.path.join(dirpath, "__pycache__"), ignore_errors=True)
            dirnames.remove("__pycache__")
            n += 1
    return n


def run_cmd(cmd: str, cwd, env: dict, log_path: Path, timeout, purge: bool = False) -> dict:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if purge:
        purge_pycache(cwd)
    t0 = time.time()
    timed_out = False
    with open(log_path, "w", encoding="utf-8", errors="replace") as log:
        log.write(f"$ {cmd}\n# cwd={cwd} gpu={env.get('HIP_VISIBLE_DEVICES', '-')}\n")
        log.flush()
        p = subprocess.Popen(["bash", "-c", cmd], cwd=str(cwd), env=env, stdout=log, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True)
        _CHILDREN.add(p)
        try:
            rc = p.wait(timeout=timeout or None)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(p)
            rc = -9
        finally:
            _CHILDREN.discard(p)
    dur = time.time() - t0
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(f"\n# rc={rc} dur={dur:.1f}s{' TIMEOUT (process group killed)' if timed_out else ''}\n")
    return {"rc": rc, "dur": round(dur, 2), "timeout": timed_out, "log": str(log_path)}


def tail(path, n: int = 15) -> str:
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return ""
    return "\n".join(lines[-n:])


# ---------------------------------------------------------------- GPUs


def smi_snapshot(timeout: float = 20):
    """{gpu_index: {"use": %, "vram": %}} from rocm-smi, or None when telemetry is unavailable.

    Unknown is never treated as idle. Index = rocm-smi card index (matches HIP order on typical MI300 nodes).
    """
    cmd = os.environ.get("MYFLOW_SMI_CMD", "rocm-smi --showuse --showmemuse --json")
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        data = json.loads(r.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None
    out = {}
    for card, v in data.items():
        m = re.match(r"card(\d+)$", card)
        if not m or not isinstance(v, dict):
            continue

        def num(key, v=v):
            try:
                return float(v.get(key))
            except (TypeError, ValueError):
                return None

        out[int(m.group(1))] = {"use": num("GPU use (%)"), "vram": num("GPU Memory Allocated (VRAM%)")}
    return out or None


def parse_pool(spec):
    if spec in (None, "", "none", "cpu"):
        return None
    if spec == "auto":
        snap = smi_snapshot()
        if snap:
            return sorted(snap)
        raise MfError('gpu.pool="auto" but rocm-smi is unavailable; set gpu.pool (e.g. "0-7") or "none"')
    out = []
    for part in str(spec).split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        elif part:
            out.append(int(part))
    return out


def _flock_wait(fh, mode, deadline: float, what: str) -> None:
    told = False
    while True:
        try:
            fcntl.flock(fh, mode | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.time() > deadline:
                raise MfError(f"timed out waiting for {what}") from None
            if not told:
                print(f"[mf] waiting for {what} ...", file=sys.stderr, flush=True)
                told = True
            time.sleep(2)


class Lease:
    """flock-based GPU lease. exclusive=True for timing, False for correctness (shared).

    machine="shared" for ordinary jobs; "excl" for profilers that perturb every GPU (PMC disables PTL
    and locks clocks machine-wide), so they wait for running timings and block new ones.
    gpu_cfg["count"] = N > 1 (multi-GPU jobs such as TP under torchrun): N distinct GPUs, all or nothing, always
    exclusive; .gpu stays the first (lowest) one, .gpus lists all of them.
    Locks are released by the kernel when the process dies, so leases can never go stale.
    """

    def __init__(self, pool, exclusive=True, machine="shared", wait_s=3600, who="", prefer=None, gpu_cfg=None):
        self.pool, self.exclusive, self.machine, self.wait_s = pool, exclusive, machine, wait_s
        self.who, self.prefer, self.cfg = who, prefer, gpu_cfg or {}
        self.count = self.cfg.get("count", 1)
        if isinstance(self.count, bool) or not isinstance(self.count, int) or self.count < 1:
            raise MfError(f"gpu.count must be a positive integer, got {self.count!r}")
        if self.count > 1:
            self.exclusive = True  # the ranks spin on each other: every GPU of the job must be whole
        self.gpu, self.gpus, self._fhs, self._holders, self._m = None, [], [], [], None

    def _order(self) -> list:
        pool = list(self.pool)
        snap = smi_snapshot() or {}
        max_use, max_vram = self.cfg.get("max_use", 5), self.cfg.get("max_vram", 20)

        def score(g):
            s = snap.get(g) or {}
            idle = s.get("use") is not None and s["use"] <= max_use and (s.get("vram") or 0) <= max_vram
            return (0 if g == self.prefer else 1, 0 if idle else 1, pool.index(g))

        return sorted(pool, key=score)

    def __enter__(self):
        if self.count > 1 and self.pool is not None and self.count > len(set(self.pool)):
            raise MfError(f"gpu.count={self.count} but pool {self.pool} has only {len(set(self.pool))} GPUs")
        deadline = time.time() + self.wait_s
        self._m = open(lock_dir() / "machine.lock", "a+")
        mmode = fcntl.LOCK_EX if self.machine == "excl" else fcntl.LOCK_SH
        _flock_wait(self._m, mmode, deadline, "the machine lock (a machine-exclusive job such as PMC is running)")
        if self.pool is None:
            return self
        if self.count > 1:
            return self._enter_many(deadline)
        mode = fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH
        told = False
        while True:
            for g in self._order():
                fh = open(lock_dir() / f"gpu{g}.lock", "a+")
                try:
                    fcntl.flock(fh, mode | fcntl.LOCK_NB)
                except BlockingIOError:
                    fh.close()
                    continue
                self._hold([(g, fh)])
                return self
            if time.time() > deadline:
                raise MfError(f"no GPU in pool {self.pool} became free within {self.wait_s}s (see: mf status)")
            if not told:
                print(f"[mf] all GPUs in pool {self.pool} are leased; waiting ...", file=sys.stderr, flush=True)
                told = True
            time.sleep(3)

    def _enter_many(self, deadline):
        """The best `count` GPUs by _order() (those mf records as leased go last), locked in ascending order without
        waiting; any miss releases them all before retrying, so this never deadlocks with other leases."""
        told = False
        while True:
            taken = {h["gpu"] for h in lease_holders()}
            order = list(dict.fromkeys(self._order()))
            pick = sorted(([g for g in order if g not in taken] + [g for g in order if g in taken])[:self.count])
            held = []
            for g in pick:
                fh = open(lock_dir() / f"gpu{g}.lock", "a+")
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    fh.close()
                    break
                held.append((g, fh))
            if len(held) == self.count:
                self._hold(held)
                return self
            for _, fh in held:
                fcntl.flock(fh, fcntl.LOCK_UN)
                fh.close()
            if time.time() > deadline:
                self.__exit__()
                raise MfError(f"{self.count} GPUs of pool {self.pool} were not free together within {self.wait_s}s "
                              "(see: mf status)")
            if not told:
                print(f"[mf] waiting for {self.count} GPUs of pool {self.pool} to be free together ...",
                      file=sys.stderr, flush=True)
                told = True
            time.sleep(3)

    def _hold(self, held: list) -> None:
        """Keep the locks of [(gpu, fh)] and write one holder record per GPU for `mf status`."""
        self.gpus, self._fhs = [g for g, _ in held], [fh for _, fh in held]
        self.gpu = self.gpus[0]
        info = {"pid": os.getpid(), "who": self.who, "since": time.time(), "exclusive": self.exclusive}
        if len(self.gpus) > 1:
            info["gpus"] = self.gpus
        for g in self.gpus:
            p = lock_dir() / f"gpu{g}.{'x' if self.exclusive else 's'}.{os.getpid()}.json"
            p.write_text(json.dumps(info))
            self._holders.append(p)

    def gpu_fields(self) -> dict:
        """Fields for ctx and records: "gpu" is the first GPU (int) as before; "gpus" only for multi-GPU leases."""
        return {"gpu": self.gpu, "gpus": list(self.gpus)} if len(self.gpus) > 1 else {"gpu": self.gpu}

    def __exit__(self, *_exc):
        for p in self._holders:
            p.unlink(missing_ok=True)
        for fh in self._fhs + [self._m]:
            if fh:
                fcntl.flock(fh, fcntl.LOCK_UN)
                fh.close()
        self._holders, self._fhs, self._m = [], [], None
        return False


def lease_holders() -> list:
    out = []
    for f in sorted(lock_dir().glob("gpu*.json")):
        m = re.match(r"gpu(\d+)\.([xs])\.(\d+)\.json$", f.name)
        if not m:
            continue
        pid = int(m.group(3))
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            f.unlink(missing_ok=True)
            continue
        except PermissionError:
            pass
        try:
            info = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            info = {}
        out.append({"gpu": int(m.group(1)), "exclusive": m.group(2) == "x", "pid": pid,
                    "who": info.get("who", ""), "since": info.get("since"), "gpus": info.get("gpus")})
    return out


def gate_snapshot(gpu, cfg: dict, after: bool = False) -> dict:
    """Idle check of one GPU, or of all GPUs of a multi-GPU lease (list). ok: True idle / False busy / None unknown."""
    mode = cfg.get("gate", "strict")
    if gpu is None or mode == "off":
        return {"ok": True, "mode": mode, "detail": "cpu" if gpu is None else "gate off"}
    if isinstance(gpu, (list, tuple)):
        return _gate_all(list(gpu), cfg, mode, after)
    snap = None
    for i in range(3 if after else 1):  # our own job may still be draining right after it exits
        if i:
            time.sleep(1.5)
        snap = (smi_snapshot() or {}).get(gpu)
        if not snap or snap.get("use") is None:
            break
        if snap["use"] <= cfg.get("max_use", 5) and (snap.get("vram") or 0) <= cfg.get("max_vram", 20):
            break
    if not snap or snap.get("use") is None:
        return {"ok": None, "mode": mode, "detail": "telemetry unavailable"}
    ok = snap["use"] <= cfg.get("max_use", 5) and (snap.get("vram") or 0) <= cfg.get("max_vram", 20)
    return {"ok": ok, "mode": mode, "use": snap["use"], "vram": snap.get("vram")}


def _gate_all(gpus: list, cfg: dict, mode: str, after: bool) -> dict:
    """Busy if any GPU is busy, else unknown if any is unknown; use/vram = the worst one, "gpus" = each one."""

    def idle(s):
        return s["use"] <= cfg.get("max_use", 5) and (s.get("vram") or 0) <= cfg.get("max_vram", 20)

    per = []
    for i in range(3 if after else 1):  # our own job may still be draining right after it exits
        if i:
            time.sleep(1.5)
        snap = smi_snapshot() or {}
        per = [{"gpu": g, "use": (snap.get(g) or {}).get("use"), "vram": (snap.get(g) or {}).get("vram")}
               for g in gpus]
        if any(p["use"] is None for p in per) or all(idle(p) for p in per):
            break
    known = [p for p in per if p["use"] is not None]
    ok = False if any(not idle(p) for p in known) else (True if len(known) == len(per) else None)
    out = {"ok": ok, "mode": mode}
    if known:
        out.update(use=max(p["use"] for p in known), vram=max(p["vram"] or 0 for p in known))
    if ok is None:
        out["detail"] = "telemetry unavailable"
    out["gpus"] = per
    return out


def gate_passes(snap: dict) -> bool:
    return snap.get("ok") is True or snap.get("mode") in ("warn", "off")


# ---------------------------------------------------------------- metrics


_MF_LINE = re.compile(r"^\s*MF_METRIC\s+(.*)$", re.M)
_KV = re.compile(r"(\w+)=(\"[^\"]*\"|\S+)")


def parse_metrics(text: str, cfg: dict, ctx: dict):
    """Return ({case: median value}, {case: [samples]}). Repeated cases in one run -> median."""
    mode = (cfg or {}).get("mode", "mf")
    vals = {}
    if mode == "mf":
        for m in _MF_LINE.finditer(text):
            kv = {k: v.strip('"') for k, v in _KV.findall(m.group(1))}
            if "value" in kv:
                vals.setdefault(kv.get("case", "default"), []).append(float(kv["value"]))
    elif mode == "regex":
        for m in re.compile(cfg["pattern"], re.M).finditer(text):
            gd = m.groupdict()
            vals.setdefault(gd.get("case") or "default", []).append(float(gd["value"]))
    elif mode == "json":
        path = Path(expand(cfg.get("path", "{run}/metrics.json"), ctx))
        data = json.loads(path.read_text())
        items = data.items() if isinstance(data, dict) else ((d["case"], d["value"]) for d in data)
        for case, v in items:
            vals.setdefault(str(case), []).extend(float(x) for x in (v if isinstance(v, list) else [v]))
    else:
        raise MfError(f"unknown parse.mode {mode!r} (mf|regex|json)")
    vals = {c: [x for x in v if math.isfinite(x)] for c, v in vals.items()}
    return {c: statistics.median(v) for c, v in vals.items() if v}, vals


_REF_LINE = re.compile(r"^\s*MF_REF\s+(.*)$", re.M)
_TO_US = {"ns": 1e-3, "us": 1.0, "ms": 1e3, "s": 1e6}
PERF_EXTRACT = ("tbps", "tflops", "ref_tbps", "ref_tflops")


def parse_perf(text: str, cfg: dict) -> dict:
    """Throughput of one bench run, for display only (never the metric): {case: {field: value}}.
    us / tbps / tflops: the measured implementation (MF_METRIC with bytes= / flops= per call, or perf.extract regexes);
    ref_impl / ref_us / ref_tbps / ref_tflops: the first reference implementation of MF_REF lines (same run, same
    protocol); ratio = us / ref_us within the run. Repeated lines -> median."""
    vals, impl, first = {}, {}, None

    def add(case, field, v):
        if v is not None and math.isfinite(v):
            vals.setdefault(case, {}).setdefault(field, []).append(v)

    def us_of(kv):
        try:
            return float(kv["value"]) * _TO_US[kv.get("unit", "us")]
        except (KeyError, ValueError):
            return None

    def rate(kv, key, us):  # bytes or flops per call over µs -> TB/s or TFLOPS
        try:
            return float(kv[key]) / us / 1e6 if us and key in kv else None
        except ValueError:
            return None

    for line_re, prefix in ((_MF_LINE, ""), (_REF_LINE, "ref_")):
        for m in line_re.finditer(text):
            kv = {k: v.strip('"') for k, v in _KV.findall(m.group(1))}
            case = kv.get("case", "default")
            if prefix:
                first = first or kv.get("impl", "ref")
                if kv.get("impl", "ref") != first:
                    continue
                impl[case] = first
            us = us_of(kv)
            add(case, prefix + "us", us)
            add(case, prefix + "tbps", rate(kv, "bytes", us))
            add(case, prefix + "tflops", rate(kv, "flops", us))
    for field, pattern in ((cfg or {}).get("extract") or {}).items():
        if field not in PERF_EXTRACT:
            raise MfError(f"perf.extract.{field}: unknown field (one of {', '.join(PERF_EXTRACT)})")
        for m in re.compile(pattern, re.M).finditer(text):
            gd = m.groupdict()
            add(gd.get("case") or "default", field, float(gd["value"]))
    out = {c: {f: statistics.median(v) for f, v in d.items()} for c, d in vals.items()}
    for c, d in out.items():
        if c in impl:
            d["ref_impl"] = impl[c]
        if d.get("us") and d.get("ref_us"):
            d["ratio"] = d["us"] / d["ref_us"]
    return out


def combine_perf(runs: list) -> dict:
    """Per case, the median over runs of each field (ratio: of the in-run ratios, i.e. paired in the same process)."""
    out = {}
    for r in runs:
        for c, d in (r or {}).items():
            for f, v in d.items():
                out.setdefault(c, {}).setdefault(f, []).append(v)
    return {c: {f: (v[0] if isinstance(v[0], str) else statistics.median(v)) for f, v in d.items()} for c, d in out.items()}


def _w(weights: dict, case: str) -> float:
    return float(weights.get(case, 1.0)) if weights else 1.0


def aggregate(per_case: dict, base: dict, cand: dict, direction: str, how: str, weights: dict):
    if not per_case:
        return None
    if how == "sum":
        cs = list(per_case)
        num = sum(_w(weights, c) * (base[c] if direction == "min" else cand[c]) for c in cs)
        den = sum(_w(weights, c) * (cand[c] if direction == "min" else base[c]) for c in cs)
        return num / den if den else None
    if weights:
        tot = sum(_w(weights, c) for c in per_case)
        return math.exp(sum(_w(weights, c) * math.log(s) for c, s in per_case.items()) / tot)
    return geomean(per_case.values())


def speedup(base: dict, cand: dict, metric: dict):
    """Speedup vs frozen baseline (>1 is better regardless of direction). Returns (agg, per_case, missing)."""
    direction = metric.get("direction", "min")
    per = {}
    for c, b in base.items():
        x = cand.get(c)
        if x and x > 0 and b > 0:
            per[c] = b / x if direction == "min" else x / b
    agg = aggregate(per, base, cand, direction, metric.get("aggregate", "geomean"), metric.get("weights") or {})
    return agg, per, sorted(set(base) - set(per))


def spread(values) -> float:
    """Relative half-range: (max - min) / (2 * median)."""
    med = statistics.median(values)
    return (max(values) - min(values)) / (2 * med) if med else 0.0


def combine_runs(runs: list) -> tuple:
    """Median per case over repeated runs + noise (median over cases of each case's relative half-range)."""
    cases = set.intersection(*(set(r) for r in runs)) if runs else set()
    med = {c: statistics.median(r[c] for r in runs) for c in sorted(cases)}
    noise = statistics.median(spread([r[c] for r in runs]) for c in cases) if cases and len(runs) > 1 else None
    return med, noise


def abba_order(pairs: int) -> list:
    seq = []
    for i in range(pairs):
        seq += ["A", "B"] if i % 2 == 0 else ["B", "A"]
    return seq


def analyze_abba(a_runs: list, b_runs: list, metric: dict) -> dict:
    """A = frozen baseline, B = candidate, paired by index on the same GPU."""
    direction = metric.get("direction", "min")
    cases = sorted(set.intersection(*(set(m) for m in a_runs + b_runs))) if a_runs and b_runs else []
    per_case = {}
    for c in cases:
        ratios = [(a[c] / b[c]) if direction == "min" else (b[c] / a[c]) for a, b in zip(a_runs, b_runs)]
        per_case[c] = {"speedup": statistics.median(ratios), "ratios": [round(r, 4) for r in ratios],
                       "a": statistics.median(a[c] for a in a_runs), "b": statistics.median(b[c] for b in b_runs),
                       "noise": spread([a[c] for a in a_runs]) if len(a_runs) > 1 else 0.0}
    sp = {c: v["speedup"] for c, v in per_case.items()}
    agg = aggregate(sp, {c: v["a"] for c, v in per_case.items()}, {c: v["b"] for c, v in per_case.items()},
                    direction, metric.get("aggregate", "geomean"), metric.get("weights") or {})
    noise = statistics.median(v["noise"] for v in per_case.values()) if per_case else None
    base_cases = set(a_runs[0]) if a_runs else set()
    return {"speedup": agg, "per_case": per_case, "noise": noise, "missing": sorted(base_cases - set(cases))}
