"""`mf revive`: restart a task's orchestrator in GitHub Copilot CLI when it stopped without finishing or asking.

The orchestrator runs inside a chat session (e.g. VS Code Copilot Chat). When that process dies - the VS Code
extension host runs out of memory, the window is closed - the work stops silently: nothing is done, nobody is
asked. A watcher (`mf revive start <task>`, a detached process) checks the listed tasks every few minutes and
starts `copilot -p` (non-interactive, tools allowed, `git push` denied: mf pushes) only when the task

- is not done or stopped and no question to the user is pending (asked gate), no mf run is open,
- has no orchestrator the watcher started that is still alive, and
- shows no activity for --idle-min minutes: mf events, worktree changes, a VS Code chat transcript that mentions
  the task, the CLI session; --crash-grace-min suffices when a VS Code extension host crashed after the last one.

Each task keeps one CLI session (fixed id), so later launches resume it. Launches are capped per day, and the
watcher halts a task (and tells the user via mf brief) after launches that recorded no progress.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import time
import uuid
from pathlib import Path

from myflow import guard
from myflow.core import PKG_ROOT, MfError, State, atomic_write, home, task_dir_of

MF = str(PKG_ROOT / "bin" / "mf")
DAY = 86400.0
RUN_MAX_S = 12 * 3600  # an open mf run without a timeout stops explaining the silence after this
SHIM_MARK = "github.copilot-chat"  # the VS Code shim on PATH asks before installing; use the real binary
INSTALL_DOC = "https://docs.github.com/en/copilot/how-tos/set-up/install-copilot-cli"
_children = []  # launched CLIs, polled so that finished ones are reaped
DEFAULTS = {"idle_min": 60.0, "crash_grace_min": 10.0, "stall_min": 180.0, "interval_min": 5.0, "max_launches": 6,
            "max_idle_launches": 2, "model": "claude-opus-5.5", "effort": "max", "copilot": ""}

PROMPT = """使用 skill myflow-kernel-opt。你是 myflow 任务 {task} 的编排者，现在接手继续（任务目录 {tdir}）。
上一个编排会话停了：{reason}。任务没有完成，也没有在等用户回答。
1. 先弄清现状，不要重复已经做完的事：运行 `{mf} status {task}`，读 {tdir}/STATUS.md 和 notes/ 下最新的几个文件{transcript}。
   检查还在跑的作业（`ps -ef`、`{mf} status` 里的运行记录）和做了一半的工作（各 worktree 的 `git status`、/tmp 下的中间产物），再决定续做还是重做。
2. 按 skill 的流程继续推进，直到任务完成（生成报告、等用户确认），或必须由用户决定（`{mf} brief {task} --ask "…"` 记录后结束本次运行）。
   `{mf} status` 显示用户的委托仍有效时，按默认值代决并用 `{mf} decide` 记录。
3. 你在 GitHub Copilot CLI 的非交互模式下运行，没有人看着：进展用 `{mf} brief` 汇报；派子 agent 用你的子 agent 工具，模型 {model}、推理强度 {effort}；
   不要直接 git push（mf 负责推送）。
4. 回复和文档用中文；代码、命令、路径保持原文。"""


def state_dir() -> Path:
    d = home() / "revive"
    d.mkdir(parents=True, exist_ok=True)
    return d


def vscode_root() -> Path:
    return Path(os.environ.get("MF_VSCODE_SERVER_DIR", "~/.vscode-server")).expanduser()


def copilot_home() -> Path:
    return Path(os.environ.get("COPILOT_HOME", "~/.copilot")).expanduser()


def session_id(task_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"myflow-revive/{task_id}"))


def _log(msg: str) -> None:
    with open(state_dir() / "watch.log", "a", encoding="utf-8") as fh:
        fh.write(f"{time.strftime('%m-%d %H:%M:%S')} {msg}\n")


def _load(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _rec_path(task_id: str) -> Path:
    return state_dir() / f"{task_id}.json"


def load_rec(task_id: str) -> dict:
    rec = _load(_rec_path(task_id), {})
    rec.setdefault("launches", [])
    return rec


def save_rec(task_id: str, rec: dict) -> None:
    atomic_write(_rec_path(task_id), json.dumps(rec, indent=1, ensure_ascii=False))


def pid_alive(pid, needle: str = "") -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        cmd = Path(f"/proc/{int(pid)}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except (OSError, ValueError):
        return False
    return needle in cmd


# ---------------------------------------------------------------- activity outside mf


_EXIT_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\.\d+ .*Extension Host Process exited with code: (\S+), "
                      r"signal: (\S+?)\.?$")


def exthost_crashes(since: float) -> list:
    """Times after `since` at which a VS Code server extension host exited abnormally (remoteagent.log)."""
    out = []
    for log in vscode_root().glob("data/logs/*/remoteagent.log"):
        try:
            if log.stat().st_mtime < since:
                continue
            with open(log, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    m = _EXIT_RE.match(line.rstrip())
                    if not m or (m.group(2) == "0" and m.group(3) == "null"):  # a closed window exits cleanly
                        continue
                    t = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
                    if t > since:
                        out.append(t)
        except OSError:
            continue
    return sorted(out)


def vscode_transcript(task_id: str, horizon_s: float = 7 * DAY, tail_bytes: int = 4 << 20):
    """(path, mtime) of the newest VS Code Copilot Chat transcript that mentions the task near its end, or None."""
    now, files = time.time(), []
    for p in vscode_root().glob("data/User/workspaceStorage/*/GitHub.copilot-chat/transcripts/*.jsonl"):
        try:
            m = p.stat().st_mtime
        except OSError:
            continue
        if now - m < horizon_s:
            files.append((m, p))
    needle = task_id.encode()
    for m, p in sorted(files, reverse=True):
        try:
            with open(p, "rb") as fh:
                fh.seek(max(0, p.stat().st_size - tail_bytes))
                if needle in fh.read():
                    return p, m
        except OSError:
            continue
    return None


def cli_activity(task_id: str) -> float:
    paths = [copilot_home() / "session-state" / session_id(task_id) / "events.jsonl", state_dir() / f"{task_id}.log"]
    return max((p.stat().st_mtime for p in paths if p.exists()), default=0.0)


# ---------------------------------------------------------------- the decision


def decide(st, now: float, cfg: dict, cli_alive: bool = False, other_t: float = 0.0, crashes=(), launches=()):
    """('skip' | 'resume' | 'kill' | 'halt', reason) for one task.

    `other_t`: newest activity mf does not record (worktree changes, VS Code transcript, CLI session);
    `crashes`: VS Code extension host crash times; `launches`: earlier launches ({"t", "progress"})."""
    if st.done():
        return "skip", "任务已完成"
    if st.stop:
        return "skip", "用户已要求停止（mf steer --stop）"
    asked = [g for g in st.pending_gates() if g.get("asked_t")]
    if asked:
        return "skip", f"在等用户回复【{asked[0]['key']}】"
    for rid, r in st.runs_open.items():
        if now - r["t"] <= (r.get("timeout") or RUN_MAX_S) + 300:
            return "skip", f"mf run {rid} 还在跑"
    last = max(st.last_agent_t, other_t)
    idle_min = (now - last) / 60
    if cli_alive:
        if idle_min > cfg["stall_min"]:
            return "kill", f"CLI 编排者 {idle_min:.0f} 分钟没有任何活动"
        return "skip", "CLI 编排者在跑"
    crashed = any(t > last for t in crashes)
    if idle_min < (cfg["crash_grace_min"] if crashed else cfg["idle_min"]):
        return "skip", f"最后活动在 {idle_min:.0f} 分钟前"
    if sum(1 for x in launches if now - x["t"] < DAY) >= cfg["max_launches"]:
        return "halt", f"24 小时内已续跑 {cfg['max_launches']} 次"
    counts = [x["progress"] for x in launches] + [len(st.progress_ts)]
    idle_runs = 0
    while idle_runs < len(launches) and counts[-1 - idle_runs] == counts[-2 - idle_runs]:
        idle_runs += 1
    if idle_runs >= cfg["max_idle_launches"]:
        return "halt", f"连续 {idle_runs} 次续跑都没有记录到进展"
    why = "VS Code 扩展宿主崩溃（多半是内存耗尽）" if crashed else "没有编排者在跑"
    return "resume", f"{why}，最后活动在 {idle_min:.0f} 分钟前"


# ---------------------------------------------------------------- Copilot CLI


def find_copilot(path: str = "") -> str:
    if path:
        return path if os.access(path, os.X_OK) else ""
    dirs = [d for d in os.environ.get("PATH", "").split(os.pathsep) if d and SHIM_MARK not in d]
    for d in dirs + ["/usr/local/bin", str(Path("~/.local/bin").expanduser())]:
        p = Path(d) / "copilot"
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return ""


def install_copilot() -> str:
    """Install the CLI the way the VS Code `copilot` shim offers to: npm, brew, then the official install script."""
    has = shutil.which
    tries = []
    if has("npm"):
        tries.append(["npm", "install", "-g", "@github/copilot"])
    if has("brew"):
        tries.append(["brew", "install", "copilot-cli"])
    if has("curl"):
        tries.append(["bash", "-c", "curl -fsSL https://gh.io/copilot-install | bash"])
    elif has("wget"):
        tries.append(["bash", "-c", "wget -qO- https://gh.io/copilot-install | bash"])
    for cmd in tries:
        _log(f"installing GitHub Copilot CLI: {' '.join(cmd)}")
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True)
        _log(f"  rc={r.returncode} {(r.stdout + r.stderr).strip()[-400:]}")
        path = find_copilot()
        if r.returncode == 0 and path:
            return path
    return ""


def _clean_env() -> dict:
    # started from a VS Code terminal: do not let the CLI attach to an IDE that may be gone
    return {k: v for k, v in os.environ.items() if not k.startswith(("VSCODE_", "TERM_PROGRAM"))}


def launch(st, cfg: dict, reason: str, transcript=None) -> int:
    copilot = find_copilot(cfg.get("copilot", "")) or install_copilot()
    if not copilot:
        raise MfError(f"GitHub Copilot CLI not found and the install failed ({INSTALL_DOC})")
    tr = (f"；VS Code 里上一个编排会话的记录在 {transcript}（JSONL，很大：只用 `tail -n 300 <文件> | cut -c1-400` "
          "看末尾，了解停下前在做什么）") if transcript else ""
    prompt = PROMPT.format(task=st.id, tdir=st.dir, reason=reason, mf=MF, transcript=tr, model=cfg["model"],
                           effort=cfg["effort"])
    repo = Path(st.task.get("repo") or st.dir)
    logs = state_dir() / "copilot-logs"
    logs.mkdir(exist_ok=True)
    cmd = [copilot, "-p", prompt, "--session-id", session_id(st.id), "--model", cfg["model"],
           "--reasoning-effort", cfg["effort"], "--allow-all-tools", "--allow-all-paths", "--deny-tool", "shell(git push)",
           "--no-ask-user", "-C", str(repo), "--log-dir", str(logs)]
    log = open(state_dir() / f"{st.id}.log", "a", encoding="utf-8")
    log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} launch: {reason}\n")
    log.flush()
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, cwd=str(repo),
                         env=_clean_env(), start_new_session=True)
    log.close()
    _children.append(p)
    return p.pid


def stop_pid(pid, grace_s: float = 30) -> None:
    try:
        os.killpg(int(pid), signal.SIGTERM)
    except (OSError, ValueError):
        return
    end = time.time() + grace_s
    while time.time() < end and pid_alive(pid):
        time.sleep(1)
    if pid_alive(pid):
        try:
            os.killpg(int(pid), signal.SIGKILL)
        except OSError:
            pass


def _brief(task_id: str, text: str) -> None:
    subprocess.run([MF, "brief", task_id, text[:600]], stdin=subprocess.DEVNULL, capture_output=True)


# ---------------------------------------------------------------- one check, the watch loop


def look(task_id: str, cfg: dict, now=None) -> dict:
    """Everything one decision needs; also what `status` / `check` print."""
    st = State(task_dir_of(task_id))
    now = time.time() if now is None else now
    rec = load_rec(st.id)
    alive = pid_alive(rec.get("pid"), "copilot")
    tr = vscode_transcript(st.id)
    ev = guard.work_evidence(st)
    other = max(ev["t"] if ev else 0.0, tr[1] if tr else 0.0, cli_activity(st.id))
    crashes = exthost_crashes(now - 2 * DAY)
    action, why = decide(st, now, cfg, alive, other, crashes, rec["launches"])
    if rec.get("halted"):
        action, why = "halted", rec["halted"]["why"]
    return {"st": st, "rec": rec, "alive": alive, "transcript": tr, "action": action, "why": why}


def check_once(task_id: str, cfg: dict, seen: dict) -> None:
    v = look(task_id, cfg)
    st, rec, action, why = v["st"], v["rec"], v["action"], v["why"]
    if seen.get(st.id) != (action, why):
        _log(f"{st.id}: {action} - {why}")
        seen[st.id] = (action, why)
    if action == "resume":
        pid = launch(st, cfg, why, v["transcript"][0] if v["transcript"] else None)
        rec["pid"] = pid
        rec["launches"].append({"t": time.time(), "pid": pid, "progress": len(st.progress_ts), "reason": why})
        save_rec(st.id, rec)
        _log(f"{st.id}: Copilot CLI started (pid {pid}, session {session_id(st.id)})")
        _brief(st.id, f"自动续跑：{why}，已由 GitHub Copilot CLI 接手（pid {pid}，会话 {session_id(st.id)}，"
                      f"日志 {state_dir() / (st.id + '.log')}）。要在 VS Code 里接管，先运行 {MF} revive stop --kill")
    elif action == "kill":
        stop_pid(rec.get("pid"))
        _log(f"{st.id}: stopped the stalled Copilot CLI (pid {rec.get('pid')}); it is restarted on the next check")
    elif action == "halt":
        rec["halted"] = {"t": time.time(), "why": why}
        save_rec(st.id, rec)
        _brief(st.id, f"自动续跑已停止：{why}。请人工查看（日志 {state_dir() / (st.id + '.log')}）；"
                      f"处理后用 {MF} revive start {st.id} 重新启用")


def watch(tasks: list, cfg: dict) -> int:
    _log(f"watcher started (pid {os.getpid()}): {', '.join(tasks)} {json.dumps(cfg, ensure_ascii=False)}")
    seen = {}
    while True:
        _children[:] = [p for p in _children if p.poll() is None]
        for t in tasks:
            try:
                check_once(t, cfg, seen)
            except Exception as e:  # one bad check must not end the watch
                _log(f"{t}: check failed: {e!r}")
        time.sleep(cfg["interval_min"] * 60)


# ---------------------------------------------------------------- commands


def _watcher() -> dict:
    w = _load(state_dir() / "watcher.json", {})
    w["alive"] = pid_alive(w.get("pid"), "revive")
    return w


def _cfg(a) -> dict:
    cfg = dict(DEFAULTS)
    for k in DEFAULTS:
        v = getattr(a, k, None)
        if v is not None:
            cfg[k] = v
    return cfg


def cmd(a) -> int:
    if a.action == "run":
        return watch([State(task_dir_of(t)).id for t in a.tasks], _cfg(a))
    w = _watcher()
    if a.action == "start":
        if not a.tasks:
            raise MfError("mf revive start <task> [<task> ...]")
        if w["alive"]:
            raise MfError(f"the watcher is already running (pid {w['pid']}, tasks {', '.join(w['tasks'])}): "
                          f"mf revive stop first")
        tasks = [State(task_dir_of(t)).id for t in a.tasks]
        cfg = _cfg(a)
        if not (find_copilot(cfg["copilot"]) or install_copilot()):
            raise MfError(f"GitHub Copilot CLI not found and the install failed ({INSTALL_DOC})")
        for t in tasks:
            rec = load_rec(t)
            if rec.pop("halted", None):
                save_rec(t, rec)
        args = [MF, "revive", "run", *tasks] + [f"--{k.replace('_', '-')}={v}" for k, v in cfg.items() if v != ""]
        with open(state_dir() / "watch.log", "a", encoding="utf-8") as log:
            p = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                 start_new_session=True)
        atomic_write(state_dir() / "watcher.json", json.dumps({"pid": p.pid, "tasks": tasks, "cfg": cfg,
                                                                "t": time.time()}, ensure_ascii=False))
        print(f"revive watcher started (pid {p.pid}) for {', '.join(tasks)}; log {state_dir() / 'watch.log'}")
        return 0
    if a.action == "stop":
        if w["alive"]:
            stop_pid(w["pid"], grace_s=5)
            print(f"watcher (pid {w['pid']}) stopped")
        else:
            print("the watcher is not running")
        for t in (a.tasks or w.get("tasks") or []):
            rec = load_rec(t)
            if a.kill and pid_alive(rec.get("pid"), "copilot"):
                stop_pid(rec["pid"])
                print(f"{t}: Copilot CLI orchestrator (pid {rec['pid']}) stopped")
        return 0
    # status / check
    if w.get("pid"):
        state = "running" if w["alive"] else "NOT running"
        print(f"watcher: {state} (pid {w['pid']}, since {time.strftime('%m-%d %H:%M', time.localtime(w['t']))}, "
              f"tasks {', '.join(w['tasks'])}); log {state_dir() / 'watch.log'}")
    else:
        print("watcher: never started (mf revive start <task>)")
    cfg = dict(w.get("cfg") or DEFAULTS)
    for t in (a.tasks or w.get("tasks") or []):
        v = look(t, cfg)
        rec, now = v["rec"], time.time()
        n = sum(1 for x in rec["launches"] if now - x["t"] < DAY)
        cli = f"pid {rec['pid']} {'alive' if v['alive'] else 'exited'}" if rec.get("pid") else "never launched"
        print(f"{v['st'].id}: {v['action']} - {v['why']}")
        print(f"  Copilot CLI: {cli}; launches in 24 h: {n}/{cfg['max_launches']}; session {session_id(v['st'].id)} "
              f"(copilot --resume {session_id(v['st'].id)} to look in); log {state_dir() / (v['st'].id + '.log')}")
        if v["transcript"]:
            print(f"  VS Code transcript: {v['transcript'][0]} "
                  f"(written {(now - v['transcript'][1]) / 60:.0f} min ago)")
    return 0


def add_parser(cmd_factory) -> None:
    s = cmd_factory("revive", lambda a: cmd(a),
                    "restart a task's orchestrator in Copilot CLI when it stops without finishing or asking: "
                    "start | stop | status | check")
    s.add_argument("action", choices=["start", "stop", "status", "check", "run"])
    s.add_argument("tasks", nargs="*")
    s.add_argument("--idle-min", type=float, help=f"no activity for this long (default {DEFAULTS['idle_min']:g})")
    s.add_argument("--crash-grace-min", type=float,
                   help=f"after a VS Code extension host crash (default {DEFAULTS['crash_grace_min']:g})")
    s.add_argument("--stall-min", type=float,
                   help=f"stop a launched CLI that is silent this long (default {DEFAULTS['stall_min']:g})")
    s.add_argument("--interval-min", type=float, help=f"between checks (default {DEFAULTS['interval_min']:g})")
    s.add_argument("--max-launches", type=int, help=f"per task per 24 h (default {DEFAULTS['max_launches']})")
    s.add_argument("--max-idle-launches", type=int,
                   help=f"halt after this many launches without progress (default {DEFAULTS['max_idle_launches']})")
    s.add_argument("--model", help=f"default {DEFAULTS['model']}")
    s.add_argument("--effort", help=f"reasoning effort (default {DEFAULTS['effort']})")
    s.add_argument("--copilot", help="path of the copilot binary (default: found on PATH, installed if missing)")
    s.add_argument("--kill", action="store_true", help="stop: also stop the Copilot CLI orchestrators")
