"""Core of myflow: paths, registry, task contract, hash-chained event log and state folding.

Everything shown to humans (status, dashboard, report) is folded from events.jsonl, so summaries are
generated from facts instead of being hand-written by agents.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import math
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
PHASES = ["spec", "bootstrap", "freeze", "survey", "plan", "implement", "verify", "integrate", "report", "done"]
PHASE_ZH = {
    "spec": "对齐需求", "bootstrap": "功能版", "freeze": "冻结基线", "survey": "调研", "plan": "评估规划",
    "implement": "并行实现", "verify": "独立复核", "integrate": "集成", "report": "报告沉淀", "done": "完成",
}
LOOP_PHASES = ("plan", "implement", "verify", "integrate")
# what counts as moving forward (vs. only being alive) and as telling the user (mf can only see what is recorded)
PROGRESS_KINDS = ("attempt", "verify", "freeze", "lane.new", "lane.set", "hyp.add", "hyp.set", "ceiling", "ms.plan",
                  "ms.bootstrap", "ms.prep", "ms.role", "ms.close", "ms.push", "oracle.accept", "phase")
PROGRESS_NOTES = ("finding", "survey", "plan", "summary", "deadend", "lesson")
PASS = "pass"
VALID_VERDICTS = ("improved", "no_gain", "regression")

DEFAULTS = {
    "id": "",
    "title": "",
    "goal": "",
    "repo": "",
    "base": "",
    "parent": None,
    "metric": {
        "name": "latency_us",
        "direction": "min",  # min: lower is better (latency); max: higher is better (throughput)
        "aggregate": "geomean",  # geomean of per-case speedups, or "sum" = total-time speedup
        "target": 1.2,
        "min_improve": 0.02,
        "weights": {},
    },
    "commands": {"setup": "", "correctness": "", "bench": "", "bench_quick": "", "verify_extra": "", "gate": ""},
    "parse": {"mode": "mf"},
    "env": {},
    "scope": {"allow": [], "protect": []},
    "gpu": {"pool": "auto", "max_use": 5, "max_vram": 20, "gate": "strict", "env": "HIP_VISIBLE_DEVICES", "wait_s": 3600},
    "budget": {
        "max_attempts": 40,
        "max_hours": 24.0,
        "max_no_improve": 10,
        "max_fail_streak": 4,
        "heartbeat_min": 30,
        "report_min": 60,  # recorded progress not yet reported to the user (task-level mf brief) for longer -> warn
        "spin_min": 120,  # agent alive but no recorded progress and no file changes for longer -> "spinning"
        "timeout_s": 1800,
    },
    "freeze": {"repeats": 3},
    "verify": {"pairs": 4, "clean": "fd", "max_case_regress": 0.02},
    "gates": {"spec": "ask", "plan": "ask", "integrate": "notify", "report": "notify"},
    "survey": {"repos": []},
    # milestones (docs/DESIGN-v2-milestones.md): url is pinned by the user's first push approval
    "git": {"url": "", "branch": "", "push": "first-ask", "lang": "en", "sign": False},
    "milestone": {"max_lines": 800, "max_hours": 24},
    # max_lines: entry document budget without mf's generated blocks (user request 11-22: easy to read, not long)
    "doc": {"entry": "", "paths": [], "lang": "zh", "max_lines": 150, "max_line_chars": 120},
    "tests": {"paths": []},
}
MS_TERMINAL = ("done", "report", "stop", "await-user")
# What gets published (docs, milestone commit messages, PR descriptions) must be in this language when the task's
# repository (local dir name or git.url) is one of these. User rule 2026-11-21: development pushed to pyhip is in Chinese.
LANG_RULES = {"pyhip": "zh"}
LANG_ZH = {"zh": "中文", "en": "英文"}


def lang_rule(task: dict):
    """(repo name, required language) if a language rule covers where this task publishes, else None."""
    url = ((task.get("git") or {}).get("url") or "").strip().rstrip("/")
    for name in (Path(task.get("repo") or "").name, re.sub(r"\.git$", "", url.split("/")[-1]) if url else ""):
        if name and name.lower() in LANG_RULES:
            return name, LANG_RULES[name.lower()]
    return None


def cjk_share(text: str) -> float:
    """Share of CJK characters among letters, ignoring code blocks and `code` (identifiers are ASCII anyway)."""
    prose = re.sub(r"`[^`\n]*`", " ", re.sub(r"```.*?```", " ", text, flags=re.S))
    cjk = len(re.findall(r"[\u3400-\u9fff]", prose))
    latin = len(re.findall(r"[A-Za-z]", prose))
    return cjk / max(1, cjk + latin)
# Changing any of these after `mf freeze` invalidates the baseline (they define what is measured).
REFREEZE_KEYS = ["repo", "base", "commands", "parse", "env", "scope.protect",
                 "metric.direction", "metric.aggregate", "metric.weights"]
# Changing these is legal but always surfaced, because it moves the goalposts.
GOALPOST_KEYS = ["goal", "metric.target", "metric.min_improve", "budget", "scope.allow", "gates"]


class MfError(Exception):
    """User-facing error: printed without a traceback."""


class MfRefused(MfError):
    """A guard refused the action (budget exhausted, gate pending, stop requested...)."""


def config_path() -> Path:
    """Persistent settings (data locations), so they do not depend on each agent shell's environment."""
    if os.environ.get("MYFLOW_CONFIG"):
        return Path(os.environ["MYFLOW_CONFIG"]).expanduser()
    return Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "myflow" / "config.json"


# setting -> environment variable that overrides it; the config file comes next, then the default
CONFIG_KEYS = {"home": "MYFLOW_HOME", "tasks_dir": "MYFLOW_TASKS_DIR", "lock_dir": "MYFLOW_LOCK_DIR", "kb": "MYFLOW_KB"}


def load_config() -> dict:
    try:
        cfg = json.loads(config_path().read_text(encoding="utf-8"))
        return cfg if isinstance(cfg, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def setting(key: str) -> tuple:
    """(path, source) of a data-location setting: environment > config file > default."""
    env = CONFIG_KEYS[key]
    if os.environ.get(env):
        return Path(os.environ[env]).expanduser(), f"环境变量 {env}"
    cfg = load_config().get(key)
    if cfg:
        return Path(cfg).expanduser(), f"配置文件 {config_path()}"
    default = {"home": lambda: Path.home() / ".myflow", "tasks_dir": lambda: home() / "tasks",
               "lock_dir": lambda: home() / "locks", "kb": lambda: PKG_ROOT / "knowledge"}[key]()
    return default, "默认"


def home() -> Path:
    return setting("home")[0]


def tasks_root() -> Path:
    return setting("tasks_dir")[0]


def lock_dir() -> Path:
    p = setting("lock_dir")[0]
    p.mkdir(parents=True, exist_ok=True)
    return p


@contextmanager
def flocked(path: Path, mode: int = fcntl.LOCK_EX):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as fh:
        fcntl.flock(fh, mode)
        try:
            yield fh
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------- registry


def registry() -> dict:
    try:
        return json.loads((home() / "registry.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def register(task_id: str, task_dir: Path) -> None:
    with flocked(home() / ".registry.lock"):
        reg = registry()
        reg[task_id] = {"dir": str(task_dir), "t": time.time()}
        atomic_write(home() / "registry.json", json.dumps(reg, indent=1, ensure_ascii=False))


def task_dir_of(task_ref: str) -> Path:
    reg = registry()
    if task_ref in reg and (Path(reg[task_ref]["dir"]) / "task.json").is_file():
        return Path(reg[task_ref]["dir"])
    p = Path(task_ref).expanduser()
    if (p / "task.json").is_file():
        return p.resolve()
    known = ", ".join(sorted(reg)) or "(none)"
    raise MfError(f"unknown task '{task_ref}'. Known tasks: {known}")


def all_task_dirs() -> list:
    out = []
    for _tid, info in sorted(registry().items()):
        d = Path(info["dir"])
        if (d / "task.json").is_file():
            out.append(d)
    return out


# ---------------------------------------------------------------- task contract


def deep_merge(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict) and k not in ("weights", "env"):
            deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def load_task(tdir: Path) -> dict:
    raw = json.loads((tdir / "task.json").read_text(encoding="utf-8"))
    return deep_merge(copy.deepcopy(DEFAULTS), raw)


def save_task(tdir: Path, task: dict) -> None:
    atomic_write(tdir / "task.json", json.dumps(task, indent=2, ensure_ascii=False) + "\n")


def get_key(d: dict, dotted: str):
    cur = d
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def set_key(d: dict, dotted: str, value) -> None:
    parts = dotted.split(".")
    cur = d
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


def sha16(data) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()[:16]


def contract_hash(task: dict) -> str:
    return sha16(json.dumps({k: get_key(task, k) for k in REFREEZE_KEYS}, sort_keys=True, ensure_ascii=False))


def flatten(prefix: str, value) -> dict:
    if isinstance(value, dict) and value and prefix not in ("metric.weights",):
        out = {}
        for k, v in value.items():
            out.update(flatten(f"{prefix}.{k}" if prefix else k, v))
        return out
    return {prefix: value}


def goalposts(task: dict) -> dict:
    out = {}
    for k in GOALPOST_KEYS:
        out.update(flatten(k, get_key(task, k)))
    return out


def file_hash(path: Path):
    try:
        return sha16(path.read_bytes())
    except FileNotFoundError:
        return None


# ---------------------------------------------------------------- event log


def _last_line(path: Path):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            pos = size
            while pos > 0:
                pos = max(0, pos - (1 << 16))
                f.seek(pos)
                lines = f.read(size - pos).rstrip(b"\n").split(b"\n")
                if len(lines) > 1 or pos == 0:
                    return lines[-1] or None
    except FileNotFoundError:
        pass
    return None


def append_event(tdir: Path, kind: str, title: str = "", /, lane=None, by: str = "agent", counter=None, **data) -> dict:
    """Append one event under an exclusive lock; each line carries the hash of the previous line.

    `counter` assigns data["n"] = 1 + number of earlier events of that kind (atomic with the append).
    """
    path = tdir / "events.jsonl"
    with flocked(tdir / ".events.lock"):
        last = _last_line(path)
        if counter:
            needle = f'"kind":"{counter}"'
            n = 0
            if path.exists():
                with open(path, encoding="utf-8") as f:
                    n = sum(1 for line in f if needle in line)
            data["n"] = n + 1
        ev = {"seq": (json.loads(last)["seq"] + 1) if last else 1, "t": round(time.time(), 3), "kind": kind, "by": by}
        if lane:
            ev["lane"] = lane
        if title:
            ev["title"] = title
        if data:
            ev["data"] = data
        ev["prev"] = sha16(last) if last else "0" * 16
        head = tdir / ".events.head"
        if last and head.exists():
            seen = head.read_text().strip()
            if seen != f"{json.loads(last).get('seq')} {sha16(last)}":
                # the last line was edited by hand: chaining onto it must not erase that evidence
                ev["head_mismatch"] = seen
        line = json.dumps(ev, ensure_ascii=False, separators=(",", ":"))
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        (tdir / ".events.head").write_text(f"{ev['seq']} {sha16(line)}\n")
    return ev


def read_events(tdir: Path) -> list:
    path = tdir / "events.jsonl"
    if not path.exists():
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    out.append({"seq": -1, "t": 0, "kind": "corrupt", "title": line[:80]})
    return out


def audit_chain(tdir: Path) -> list:
    problems = []
    prev_line = None
    with open(tdir / "events.jsonl", "rb") as f:
        for i, raw in enumerate(f, 1):
            raw = raw.rstrip(b"\n")
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                problems.append(f"line {i}: not JSON")
                prev_line = raw
                continue
            want = sha16(prev_line) if prev_line is not None else "0" * 16
            if ev.get("prev") != want:
                problems.append(f"line {i} (seq {ev.get('seq')}): hash chain broken (edited or reordered)")
            if ev.get("seq") != i:
                problems.append(f"line {i}: seq {ev.get('seq')} != {i}")
            if ev.get("head_mismatch"):
                problems.append(f"line {i} (seq {ev.get('seq')}): appended after the previous line stopped matching "
                                f".events.head ({ev['head_mismatch']}): that line was edited by hand")
            prev_line = raw
    head = tdir / ".events.head"
    if prev_line is not None and head.exists():
        parts = head.read_text().split()
        try:
            want_seq = int(parts[0])
        except (IndexError, ValueError):
            want_seq = -1
        if len(parts) != 2 or parts[1] != sha16(prev_line) or want_seq != json.loads(prev_line).get("seq"):
            problems.append("last event does not match .events.head (edited, truncated or appended by hand)")
    return problems


# ---------------------------------------------------------------- state folding


def geomean(values):
    vals = [v for v in values if v and v > 0]
    return math.exp(sum(math.log(v) for v in vals) / len(vals)) if vals else None


class State:
    """Everything known about one task, folded from task.json + events.jsonl."""

    def __init__(self, tdir: Path):
        self.dir = Path(tdir)
        self.task = load_task(self.dir)
        self.id = self.task.get("id") or self.dir.name
        self.events = read_events(self.dir)
        self.phase, self.round = "spec", 1
        self.phase_hist = []
        self.lanes, self.hyps, self.steers, self.gates, self.runs_open = {}, {}, {}, {}, {}
        self.attempts, self.verifies, self.decisions, self.briefs, self.notes, self.changes = [], [], [], [], [], []
        self.freeze = self.ceiling = self.stop = None
        self.milestones, self.pushgates = {}, {}
        self.revoked = []  # closed milestones taken back (mf ms revoke); their ids can be planned again
        self.oracle_accepts = []
        self.bootstrap = None
        self.delegation = None  # user: "decide by the defaults until <time>" (mf decide --delegate)
        self.checks = {}  # correctness passes in clean worktrees, by code+tests key (user request 11-22: run once)
        self.progress_ts, self.last_report_t = [], None
        self.created_t = self.events[0]["t"] if self.events else time.time()
        self.last_t = self.events[-1]["t"] if self.events else self.created_t
        worker = [e["t"] for e in self.events if e.get("by") != "user"]  # user messages are not agent liveness
        self.last_agent_t = worker[-1] if worker else self.created_t
        self.corrupt = 0
        self.now = None
        for ev in self.events:
            self._apply(ev)
        self._derive()

    # -- event application
    def _apply(self, ev: dict) -> None:
        k, d, lane = ev.get("kind"), ev.get("data", {}), ev.get("lane")
        if k == "corrupt":
            self.corrupt += 1
        elif k == "phase":
            self.phase, self.round = d["phase"], d.get("round", self.round)
            self.phase_hist.append({"phase": self.phase, "round": self.round, "t": ev["t"], "brief": ev.get("title", "")})
        elif k == "brief":
            item = {"t": ev["t"], "phase": d.get("phase", self.phase), "round": d.get("round", self.round),
                    "text": ev.get("title", ""), "lane": lane, "ask": d.get("ask", False), "by": ev["by"], "seq": ev["seq"]}
            self.briefs.append(item)
            if item["ask"] and not lane:
                # a question the orchestrator writes is asked when written; one opened by mf itself (push, oracle)
                # still has to be relayed to the user by the orchestrator (mf brief --gate)
                self.gates[gate_key(item["phase"], item["round"])] = {
                    "state": "pending", "brief": item["text"], "t": ev["t"], "phase": item["phase"],
                    "asked_t": ev["t"] if ev["by"] != "mf" else None}
        elif k == "gate.asked":
            g = self.gates.get(d.get("gate"))
            if g:
                g.update(asked_t=ev["t"], asked_via=d.get("via", ""))
        elif k == "check":
            self.checks[d.get("key")] = dict(d, t=ev["t"])
        elif k == "delegate":
            self.delegation = {"t": ev["t"], "until": d.get("until") or 0, "quote": d.get("quote", ""),
                               "text": ev.get("title", "")}
        elif k == "decide":
            item = {"t": ev["t"], "text": ev.get("title", ""), "by": ev["by"], "gate": d.get("gate"),
                    "quote": d.get("quote", ""), "spec_hash": d.get("spec_hash")}
            self.decisions.append(item)
            if d.get("push_key"):  # the push target was part of the requirements the user approved
                self.gates[d["push_key"]] = {"phase": "push", "brief": "推送目标随需求一起确认", "state": "approved",
                                             "decision": item["text"], "by": ev["by"], "t": ev["t"], "t_dec": ev["t"],
                                             "asked_t": ev["t"], "via": "spec"}
            if item["gate"]:
                g = self.gates.setdefault(item["gate"], {"phase": item["gate"].split("@")[0], "brief": ""})
                g.update(state="approved", decision=item["text"], by=ev["by"], t_dec=ev["t"],
                         spec_hash=item["spec_hash"])
        elif k == "freeze":
            self.freeze = dict(d, t=ev["t"], seq=ev["seq"])
        elif k == "lane.new":
            self.lanes[lane] = {"name": lane, "direction": ev.get("title", ""), "hyp": d.get("hyp"),
                                "status": "active", "reason": "", "t_new": ev["t"], "t_last": ev["t"],
                                "round": d.get("round", self.round), "wt": d.get("wt"), "branch": d.get("branch"),
                                "start": d.get("start"), "attempts": [], "merge": d.get("merge")}
        elif k == "lane.set" and lane in self.lanes:
            self.lanes[lane].update(status=d.get("status", "active"), reason=ev.get("title", ""))
        elif k == "attempt":
            a = dict(d, t=ev["t"], lane=lane, seq=ev["seq"])
            self.attempts.append(a)
            if lane in self.lanes:
                self.lanes[lane]["attempts"].append(a)
        elif k == "verify":
            self.verifies.append(dict(d, t=ev["t"], lane=lane, seq=ev["seq"], by=ev["by"]))
        elif k == "hyp.add":
            self.hyps[f"H{d['n']}"] = {"id": f"H{d['n']}", "text": ev.get("title", ""), "predict": d.get("predict", ""),
                                       "test": d.get("test", ""), "status": "open", "evidence": "", "lane": lane,
                                       "t": ev["t"]}
        elif k == "hyp.set" and d.get("id") in self.hyps:
            self.hyps[d["id"]].update(status=d.get("status", "open"), evidence=ev.get("title", ""), t_set=ev["t"])
        elif k == "ceiling":
            self.ceiling = {"value": d.get("value"), "basis": ev.get("title", ""), "t": ev["t"]}
        elif k == "note":
            self.notes.append({"t": ev["t"], "title": ev.get("title", ""), "lane": lane, "kind": d.get("kind", "info"),
                               "file": d.get("file"), "by": ev["by"]})
        elif k == "steer":
            self.steers[d["n"]] = {"n": d["n"], "text": ev.get("title", ""), "lane": lane, "t": ev["t"],
                                   "acked": False, "ack": "", "by": ev["by"]}
        elif k == "ack":
            s = self.steers.get(d.get("n"))
            if s:
                s.update(acked=True, ack=ev.get("title", ""), t_ack=ev["t"])
        elif k == "run.start":
            self.runs_open[d["run"]] = {"t": ev["t"], "what": ev.get("title", ""), "lane": lane,
                                        "timeout": d.get("timeout")}
        elif k == "run.end":
            self.runs_open.pop(d.get("run"), None)
        elif k == "contract.change":
            self.changes.append({"t": ev["t"], "key": d.get("key"), "old": d.get("old"), "new": d.get("new"),
                                 "by": ev["by"], "refreeze": d.get("refreeze", False), "why": ev.get("title", "")})
        elif k == "stop":
            self.stop = {"t": ev["t"], "reason": ev.get("title", ""), "by": ev["by"]}
        elif k == "gate.clear":  # a pending gate that no longer applies (superseded content, closed or dropped milestone)
            g = self.gates.get(d.get("gate"))
            if g and g.get("state") == "pending":
                g["state"] = "superseded"
        elif (k or "").startswith("ms.") or k == "oracle.accept":
            self._apply_ms(k, d, ev)
        if k in ("ping", "brief", "attempt", "verify", "run.start", "phase", "lane.new", "ms.plan", "ms.prep",
                 "ms.role", "ms.close") and ev.get("title"):
            who = f"[{lane}] " if lane else ""
            self.now = {"t": ev["t"], "text": f"{who}{k}: {ev['title']}"}
        if (k in PROGRESS_KINDS and not (k == "phase" and len(self.phase_hist) == 1)) or \
                (k == "note" and d.get("kind") in PROGRESS_NOTES):  # the task's first phase is its creation, not progress
            self.progress_ts.append(ev["t"])
        if k == "brief" and ev.get("by") == "agent" and not lane:  # the orchestrator telling the user (lane briefs are not)
            self.last_report_t = ev["t"]
        if lane in self.lanes:
            self.lanes[lane]["t_last"] = ev["t"]

    def _apply_ms(self, k: str, d: dict, ev: dict) -> None:
        if k == "ms.bootstrap":
            self.bootstrap = dict(d, t=ev["t"])
            return
        if k == "ms.pushgate":
            self.pushgates[d["key"]] = dict(d, t=ev["t"])
            return
        if k == "oracle.accept":
            self.oracle_accepts.append(dict(d, t=ev["t"], by=ev["by"], why=ev.get("title", "")))
            return
        if k == "ms.push":
            for mid in d.get("ids") or []:
                if mid in self.milestones:
                    self.milestones[mid]["push"] = dict(d, t=ev["t"])
                    self.milestones[mid]["pushed"] = bool(d.get("ok"))
            return
        mid = d.get("id")
        if not mid:
            return
        m = self.milestones.get(mid)
        if k == "ms.revoke":
            if m:
                m.update(status="revoked", revoke=dict(d, t=ev["t"], why=ev.get("title", "")))
                self.revoked.append(dict(d, t=ev["t"], why=ev.get("title", ""), title=m.get("title", ""),
                                         close=m.get("close"), push=m.get("push") if m.get("pushed") else None))
            return
        if m is None or (k == "ms.plan" and m["status"] == "revoked"):  # a revoked id starts over, same place
            m = self.milestones[mid] = {"id": mid, "title": "", "type": "perf", "status": "planned", "goals": [],
                                        "commitment": "committed", "plans": [], "roles": {}, "role_log": [],
                                        "t_plan": ev["t"], "pushed": False}
        if k == "ms.plan":
            m["title"] = ev.get("title") or m["title"]
            for key in ("type", "goals", "accept", "expect", "basis", "scope", "est_h", "why", "round"):
                if d.get(key) not in (None, "", []):
                    m[key] = d[key]
            m["commitment"] = d.get("commitment", m["commitment"])
            m["plans"].append(dict(d, t=ev["t"], title=ev.get("title", "")))
        elif k == "ms.prep":  # append_event() stores `lane` at the event level, not in data
            m.update(status="closing", prep=dict(d, t=ev["t"], lane=ev.get("lane")), roles={}, checkpoint=d.get("tree"))
        elif k == "ms.role":
            rec = dict(d, t=ev["t"], by=ev["by"], summary=ev.get("title", ""))
            m["roles"][d.get("role")] = rec
            m["role_log"].append(rec)
            if d.get("tree"):
                m["checkpoint"] = d["tree"]  # latest recorded state of the integration worktree
        elif k == "ms.close":
            m.update(status="done", close=dict(d, t=ev["t"]))
        elif k == "ms.drop":
            m.update(status="dropped", drop={"reason": ev.get("title", ""), "t": ev["t"]})

    # -- derived facts
    def _derive(self) -> None:
        t = self.task
        min_imp = float(t["metric"].get("min_improve", 0.02))
        # incremental verifies (`--against` a milestone) measure M_prev -> candidate, not frozen base -> candidate;
        # verifies from an earlier freeze were measured against another baseline (re-freeze = new epoch)
        valid = [v for v in self.verifies if v.get("verdict") in VALID_VERDICTS and v.get("speedup")
                 and not v.get("against") and self.same_epoch(v)]
        latest_v = {v.get("attempt"): v for v in valid}  # later verifies of the same attempt win

        def effective(a):  # what we believe now: a verified number overrides the lane's own measurement
            if a.get("n") in latest_v:
                return latest_v[a["n"]]["speedup"]
            return a.get("speedup") if a.get("status") == PASS else None

        def streak(attempts):
            best, n = 1.0, 0
            for a in attempts:
                s = effective(a)
                if s and s >= best * (1 + min_imp):
                    best, n = s, 0
                else:
                    n += 1
            return n

        full = [a for a in self.attempts if a.get("kind", "full") == "full"]
        passing = [a for a in full if a.get("status") == PASS and a.get("speedup")]
        self.best_self = max(passing, key=lambda a: a["speedup"]) if passing else None
        self.no_improve = streak(full)
        self.best_verified = max(valid, key=lambda v: v["speedup"]) if valid else None
        max_ni = int(t["budget"].get("max_no_improve") or 0)
        for ln in self.lanes.values():
            fs = 0
            for a in reversed(ln["attempts"]):
                if a.get("status") == PASS:
                    break
                fs += 1
            ln["fail_streak"] = fs
            lfull = [a for a in ln["attempts"] if a.get("kind", "full") == "full"]
            ln["no_improve"] = streak(lfull)
            lp = [a for a in lfull if a.get("status") == PASS and a.get("speedup")]
            ln["best_self"] = max(lp, key=lambda a: a["speedup"]) if lp else None
            lv = [v for v in valid if v.get("lane") == ln["name"]]
            ln["best_verified"] = max(lv, key=lambda v: v["speedup"]) if lv else None
            ln["stalled"] = bool(max_ni) and ln["status"] == "active" and ln["no_improve"] >= max_ni
        active = [ln for ln in self.lanes.values() if ln["status"] == "active" and ln["attempts"]]
        self.worst_lane = max(active, key=lambda ln: ln["no_improve"]) if active else None
        self.all_stalled = bool(active) and all(ln["stalled"] for ln in active)
        start = self.freeze["t"] if self.freeze else self.created_t
        self.hours = (time.time() - start) / 3600.0
        self.contract_ok = (self.freeze is None) or (self.freeze.get("contract") == contract_hash(t))
        self.closed_ms = sorted((m for m in self.milestones.values() if m["status"] == "done"),
                                key=lambda m: m["close"]["t"])
        self.last_ms = self.closed_ms[-1] if self.closed_ms else None
        self.ms_commits = {m["close"]["commit"]: m["id"] for m in self.closed_ms}
        self.open_ms = next((m for m in self.milestones.values() if m["status"] == "closing"), None)

    # -- milestone helpers
    def same_epoch(self, v: dict) -> bool:
        """Was this verify measured under the current freeze? (records without the field predate epochs)"""
        cur = (self.freeze or {}).get("seq")
        return v.get("freeze_seq", cur) == cur

    def ms_commit(self, mid: str):
        m = self.milestones.get(mid)
        return m["close"]["commit"] if m and m["status"] == "done" else None

    @property
    def last_ms_commit(self):
        return self.last_ms["close"]["commit"] if self.last_ms else None

    def next_ms(self):
        """What the last push promised next: a milestone dict, a terminal state string, or None."""
        if self.last_ms:
            nxt = self.last_ms["close"].get("next") or []
            if nxt:
                first = nxt[0]
                return first if first in MS_TERMINAL else self.milestones.get(first)
        planned = [m for m in self.milestones.values() if m["status"] in ("planned", "closing")
                   and m.get("commitment") == "committed"]
        return planned[0] if planned else None

    # -- helpers
    @property
    def target(self) -> float:
        return float(self.task["metric"].get("target") or 1.0)

    def progress(self):
        bv = self.best_verified["speedup"] if self.best_verified else None
        if bv is None or self.target <= 1.0:
            return None
        return max(0.0, min(1.0, (bv - 1.0) / (self.target - 1.0)))

    def gate_mode(self, phase: str) -> str:
        return (self.task.get("gates") or {}).get(phase, "notify")

    def gate_ok(self, phase: str, rnd=None) -> bool:
        if self.gate_mode(phase) != "ask":
            return True
        g = self.gates.get(gate_key(phase, rnd if rnd is not None else self.round))
        return bool(g and g.get("state") == "approved")

    def pending_gates(self) -> list:
        return [dict(g, key=k) for k, g in self.gates.items() if g.get("state") == "pending"]

    def delegated(self, now=None):
        """The user's standing delegation (mf decide --delegate) if still in force: questions are decided by the defaults."""
        d = self.delegation
        return d if d and (now if now is not None else time.time()) < d["until"] else None

    def default_decisions(self) -> list:
        """Decisions taken by the defaults under the latest delegation: what the user should look over."""
        start = (self.delegation or {}).get("t")
        return [x for x in self.decisions if x["by"] == "default" and start and x["t"] >= start]

    def unacked(self, lane=None) -> list:
        out = []
        for s in sorted(self.steers.values(), key=lambda s: s["n"]):
            if s["acked"]:
                continue
            if lane is None or s["lane"] in (None, lane):
                out.append(s)
        return out

    def attempt(self, n: int):
        for a in self.attempts:
            if a.get("n") == n:
                return a
        return None

    def done(self) -> bool:
        return self.phase == "done"


def gate_key(phase: str, rnd: int) -> str:
    return f"{phase}@R{rnd}" if phase in LOOP_PHASES else phase


def load_state(task_ref) -> State:
    tdir = task_ref if isinstance(task_ref, Path) else task_dir_of(str(task_ref))
    return State(tdir)
