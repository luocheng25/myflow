"""`mf` command line. Every repeated step of the flow is one command, so all lanes do it the same way.

Exit codes: 0 ok · 2 error · 3 refused by a guard (budget / gate / stop) · guard: 1 warn, 2 crit.
"""

from __future__ import annotations

import argparse
import ast
import fcntl
import json
import os
import random
import re
import shlex
import shutil
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from myflow import __version__, guard, kb, render
from myflow.core import (DEFAULTS, LANG_ZH, PASS, PHASES, PKG_ROOT, REFREEZE_KEYS, LOOP_PHASES, MfError, MfRefused, State,
                         append_event, atomic_write, audit_chain, contract_hash, file_hash, gate_key, get_key,
                         goalposts, home, lang_rule, register, registry, save_task, set_key, task_dir_of,
                         CONFIG_KEYS, config_path, load_config, setting, tasks_root)
from myflow.gitops import (DEFAULT_IGNORE, changed_files, diffstat, ensure_worktree, git, is_ancestor, is_repo, merge,
                           protect_problems, reset_detached, rev_parse, scope_violations, snapshot, tree_blobs,
                           write_patch)
from myflow.runner import (Lease, abba_order, analyze_abba, build_env, combine_runs, expand, gate_passes,
                           gate_snapshot, install_signal_handlers, lease_holders, parse_metrics, parse_pool, run_cmd,
                           smi_snapshot, speedup, tail)

MF = str(PKG_ROOT / "bin" / "mf")
NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{1,63}$")
LANE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")


# ---------------------------------------------------------------- helpers


def _st(ref) -> State:
    return State(ref if isinstance(ref, Path) else task_dir_of(ref))


def _lane(st: State, name: str) -> dict:
    if name not in st.lanes:
        raise MfError(f"unknown lane '{name}' in {st.id}; lanes: {', '.join(st.lanes) or '(none)'} (mf lane new)")
    return st.lanes[name]


def _run_dir(st: State, lane: str, kind: str) -> Path:
    d = st.dir / "runs" / f"{time.strftime('%Y%m%d-%H%M%S')}-{lane}-{kind}-{os.urandom(2).hex()}"
    d.mkdir(parents=True)
    return d


def _ctx(st: State, lane: str, wt, run_dir, seed=0, gpu=None) -> dict:
    lane_dir = st.dir / "lanes" / lane
    for sub in ("tmp", "cache"):
        (lane_dir / sub).mkdir(parents=True, exist_ok=True)
    return {"wt": str(wt), "lane": lane, "task": st.id, "run": str(run_dir), "gpu": gpu, "seed": seed,
            "lane_dir": str(lane_dir), "task_dir": str(st.dir), "repo": st.task["repo"],
            "base": (st.freeze or {}).get("base") or st.task.get("base")}


@contextmanager
def _lane_lock(st: State, name: str):
    """One mf try / verify at a time per lane: refuse instead of letting two agents clobber one worktree."""
    path = st.dir / "lanes" / f".{name}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise MfRefused(f"another mf command is already running on {name} (one try/verify per lane at a time)") from None
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _timeout(st: State, a=None) -> int:
    return int(getattr(a, "timeout", None) or st.task["budget"].get("timeout_s") or 1800)


def _pool(st: State):
    return parse_pool(st.task["gpu"].get("pool"))


def _who(st: State, lane: str, what: str) -> str:
    return f"{st.id}/{lane}/{what}"


def _resolve_commit(st: State, ref: str) -> str:
    """A milestone id (M3) or any git revision."""
    if ref and re.match(r"M\d{1,3}[a-z]?$", ref):
        c = st.ms_commit(ref)
        m = st.milestones.get(ref) or {}
        if not c and m.get("status") == "closing":  # pipelining: build on the candidate while it is reviewed
            c = m["prep"]["candidate"]
        if not c:
            raise MfError(f"milestone {ref} is neither closed nor closing (mf ms show {st.id})")
        return c
    return rev_parse(Path(st.task["repo"]), ref)


def _scope_base(st: State, ln: dict) -> str:
    """Scope/oracle checks are relative to a trusted start: the newest closed milestone (created only by mf ms close)
    that the lane's start descends from, else the frozen base. Every change after it is checked. Without this,
    docs/tests legitimately added by a milestone would flag every later attempt."""
    start = ln.get("start")
    if start:
        repo = Path(st.task["repo"])
        for m in reversed(st.closed_ms):
            c = m["close"]["commit"]
            if c == start or is_ancestor(repo, c, start):
                return c
    return st.freeze["base"]


def _oracle_map(st: State, sbase: str) -> dict:
    globs = st.task["scope"]["protect"]
    if not globs or sbase == st.freeze["base"]:
        return st.freeze["protect"]
    return tree_blobs(Path(st.task["repo"]), sbase, globs)


def _cases_line(per_case: dict) -> str:
    items = sorted(per_case.items(), key=lambda kv: kv[1])
    if len(items) > 6:
        items = items[:3] + [("…", None)] + items[-3:]
    return " | ".join(f"{c} {render.fx(s)}" if s else c for c, s in items)


def _after(tdir: Path, lane=None) -> None:
    """Re-render views, then show the agent everything it must react to (alerts, user messages, gates)."""
    st = State(tdir)
    try:
        render.render_task(st)
        render.render_index()
    except Exception as e:  # views must never break the command that produced the facts
        print(f"[mf] render warning: {e}", file=sys.stderr)
    al = [a for a in guard.alerts(st) if (a["level"] in ("crit", "ask", "warn") or a["code"] in ("stop", "target_met"))
          and not (lane and a["code"] in guard.ORCHESTRATOR_ONLY)]  # a lane agent cannot talk to the user
    steers = st.unacked(lane)
    if not al and not steers:
        return
    print("──── mf 提醒（每次 mf 调用都会显示，处理后消失）────")
    for a in al[:8]:
        print(f"{render.ICON[a['level']]} {a['level']:<4} {a['msg']}")
    for s in steers:
        who = f"lane {s['lane']}" if s["lane"] else "全体"
        print(f"📨 用户留言 #{s['n']}（{render.age(time.time() - s['t'])} 前，{who}）：{s['text']}")
        print(f"   处理后回执：{MF} ack {st.id} {s['n']} \"<如何处理>\"")


def _parse_value(v: str):
    try:
        return json.loads(v)
    except json.JSONDecodeError:
        return v


def _set(tdir: Path, pairs: list, why: str, by: str) -> list:
    st = State(tdir)
    task = json.loads((tdir / "task.json").read_text(encoding="utf-8"))
    changed = []
    for key, value in pairs:
        old = get_key(st.task, key)
        if old == value:
            continue
        set_key(task, key, value)
        refreeze = bool(st.freeze) and any(key == k or key.startswith(k + ".") or k.startswith(key + ".")
                                           for k in REFREEZE_KEYS)
        append_event(tdir, "contract.change", why, by=by, key=key, old=old, new=value, refreeze=refreeze)
        changed.append((key, old, value, refreeze))
    save_task(tdir, task)
    return changed


# ---------------------------------------------------------------- task lifecycle


def cmd_new(a):
    if not NAME_RE.match(a.id):
        raise MfError("task id: lowercase letters/digits/._- (2-64 chars)")
    if a.id in registry():
        raise MfError(f"task '{a.id}' already exists at {registry()[a.id]['dir']}")
    repo = Path(a.repo).expanduser().resolve()
    if not is_repo(repo):
        raise MfError(f"{repo} is not a git repository")
    tdir = Path(a.dir).expanduser().resolve() if a.dir else tasks_root() / a.id
    if tdir.exists() and any(tdir.iterdir()):
        raise MfError(f"{tdir} exists and is not empty")
    task = json.loads(json.dumps(DEFAULTS))
    if a.from_file:
        tpl = json.loads(Path(a.from_file).read_text(encoding="utf-8"))
        tpl = {k: v for k, v in tpl.items() if not k.startswith("_")}
        from myflow.core import deep_merge
        deep_merge(task, tpl)
    task.update(id=a.id, title=a.title or task.get("title") or a.id, goal=a.goal or task.get("goal", ""),
                repo=str(repo), parent=a.parent)
    if a.target:
        task["metric"]["target"] = a.target
    rule = lang_rule(task)
    if rule:  # what this task publishes there must be in that language
        task["git"]["lang"] = task["doc"]["lang"] = rule[1]
    for sub in ("runs", "notes", "lanes", "tools", "wt"):
        (tdir / sub).mkdir(parents=True, exist_ok=True)
    save_task(tdir, task)
    spec = (PKG_ROOT / "templates" / "SPEC.md").read_text(encoding="utf-8")
    for k in ("id", "title", "goal", "repo"):
        spec = spec.replace("{{" + k + "}}", str(task[k]))
    atomic_write(tdir / "SPEC.md", spec)
    register(a.id, tdir)
    append_event(tdir, "task.new", task["title"], by="agent", repo=str(repo), parent=a.parent)
    append_event(tdir, "phase", "创建任务", by="mf", phase="spec", round=1)
    print(f"created task {a.id}: {tdir}")
    if rule:
        print(f"语言规则：在 {rule[0]} 开发 / 推送到 {rule[0]} 时，文档、提交信息、PR 描述一律用{LANG_ZH.get(rule[1], rule[1])}"
              f"（已设 git.lang = doc.lang = {rule[1]}）")
    print(f"next: 1) 填 {tdir}/task.json（commands/parse/scope/metric）或用 `mf set`  2) 写 SPEC.md（尤其第 7、8 节）\n"
          f"      3) {MF} brief {a.id} --ask \"<一句话复述目标+关键假设+待确认问题>\"  4) 等用户回复后 {MF} decide {a.id} --gate spec \"<用户原话>\"")
    _after(tdir)


def cmd_set(a):
    tdir = task_dir_of(a.task)
    pairs = []
    for item in a.pairs:
        if "=" not in item:
            raise MfError(f"expected key=value, got {item!r}")
        k, v = item.split("=", 1)
        pairs.append((k.strip(), _parse_value(v)))
    for key, old, new, refreeze in _set(tdir, pairs, a.why or "", a.by):
        print(f"{key}: {json.dumps(old, ensure_ascii=False)} -> {json.dumps(new, ensure_ascii=False)}"
              + ("   ⚠ 已冻结：度量契约变了，需重新 mf freeze" if refreeze else ""))
    _after(tdir)


def cmd_budget(a):
    tdir = task_dir_of(a.task)
    st = State(tdir)
    pairs = []
    for flag, key in (("attempts", "max_attempts"), ("hours", "max_hours"), ("no_improve", "max_no_improve")):
        v = getattr(a, flag)
        if v is None:
            continue
        cur = st.task["budget"].get(key) or 0
        new = cur + float(v[1:]) if str(v).startswith("+") else float(v)
        pairs.append((f"budget.{key}", int(new) if key != "max_hours" else new))
    for key, old, new, _ in _set(tdir, pairs, a.why or "", a.by):
        print(f"{key}: {old} -> {new}")
    _after(tdir)


def cmd_brief(a):
    tdir = task_dir_of(a.task)
    st = State(tdir)
    text = " ".join(a.text).strip()
    if len(text) > 600:
        print("[mf] brief 超过 600 字已截断：brief 是给人一眼看的，细节请写文件并用 mf note --file", file=sys.stderr)
        text = text[:600] + "…"
    if a.ask and a.lane:
        raise MfError("--ask is task-level (gates belong to the orchestrator), drop --lane")
    phase = a.phase or st.phase
    if a.ask and phase == "plan" and st.freeze:
        from myflow import ms
        if ms.ensure_plan_gate(st, st.round):  # user rule 11-22: a plan that follows the pushed promise is only shown
            append_event(tdir, "brief", text, lane=a.lane, by="agent", phase=phase, round=st.round, ask=False)
            print(f"gate【{gate_key(phase, st.round)}】不需要问用户：计划与上次推送的承诺一致；brief 已作为通知记录")
            _after(tdir, a.lane)
            return
    if a.ask and phase == "spec":  # user rule 11-22: the push target is confirmed together with the requirements
        from myflow import ms
        key = ms.spec_push_key(st)
        if key:
            text += f"（推送目标：{st.task['git']['url']} 分支 {ms.remote_branch(st)}，含里程碑 tag；批准需求即同时批准推送到这里）"
        else:
            print("[mf] ⚠ git.url / git.branch 未设置：先 mf set 推送目标并写进 SPEC，批准需求时一并确认；"
                  "否则首次推送还要单独问用户一次", file=sys.stderr)
    append_event(tdir, "brief", text, lane=a.lane, by="agent", phase=phase, round=st.round, ask=a.ask)
    if a.ask:
        print(f"gate【{gate_key(phase, st.round)}】已挂起：请向用户展示这段 brief 并等待回复；"
              f"回复后：{MF} decide {st.id} --gate {gate_key(phase, st.round)} \"<用户原话>\"")
    _after(tdir, a.lane)


def cmd_decide(a):
    tdir = task_dir_of(a.task)
    st = State(tdir)
    gate = a.gate
    if gate == "auto":
        pend = st.pending_gates()
        if len(pend) != 1:
            raise MfError(f"--gate auto needs exactly one pending gate; pending: {[g['key'] for g in pend]}")
        gate = pend[0]["key"]
    elif gate and gate in PHASES:
        gate = gate_key(gate, st.round)
    extra = {}
    if gate == "spec":
        from myflow import ms
        extra["spec_hash"] = file_hash(tdir / "SPEC.md")
        extra["push_key"] = ms.spec_push_key(st)
    quote = a.quote or ""
    if a.default:
        dl = st.delegated()
        if not dl:
            raise MfRefused("没有生效的用户授权（mf delegate）：这件事要先问用户")
        by, quote = "default", dl["quote"]
        extra["delegation_t"] = dl["t"]
    else:
        by = a.by or ("user" if gate else "agent")
    append_event(tdir, "decide", " ".join(a.text).strip(), by=by, gate=gate, quote=quote, **extra)
    print(f"decision recorded{(' for gate ' + gate) if gate else ''} (by {by})")
    _after(tdir)


def cmd_delegate(a):
    """The user lets the orchestrator decide by the defaults for a while; every such decision stays listed for review."""
    tdir = task_dir_of(a.task)
    st = State(tdir)
    if a.off:
        append_event(tdir, "delegate", "授权结束", by="user", until=0)
        print(f"授权已结束：之后需要确认的问题照常问用户（期间按默认值决定的 {len(st.default_decisions())} 项见 mf show）")
    else:
        if not a.quote or not a.hours or a.hours <= 0:
            raise MfError("--hours H and --quote \"<the user's exact words>\" are required")
        until = time.time() + a.hours * 3600
        append_event(tdir, "delegate", f"用户授权 {a.hours:g} 小时内按默认值决定", by="user", until=until, quote=a.quote)
        print(f"授权至 {time.strftime('%m-%d %H:%M', time.localtime(until))}：期间需要确认的问题由编排者按默认值（推荐项）决定，"
              f"用 {MF} decide {st.id} [--gate <key>] --default \"<选了什么、为什么>\"；这些决定会单独列出，供用户回来过目")
    _after(tdir)


def cmd_asked(a):
    """The orchestrator relayed a pending gate to the user (chat / form): from now on it is the user's turn."""
    tdir = task_dir_of(a.task)
    st = State(tdir)
    for key in a.gates:
        g = st.gates.get(key)
        if not g or g.get("state") != "pending":
            raise MfError(f"no pending gate '{key}'; pending: {[x['key'] for x in st.pending_gates()] or 'none'}")
        append_event(tdir, "gate.asked", "", by="agent", gate=key, via=a.via)
        print(f"gate {key}: asked the user via {a.via}; after the reply: {MF} decide {st.id} --gate {key} \"<user's words>\"")
    _after(tdir)


def auto_phase(st: State, phase: str, why: str) -> None:
    """Phases follow the actions (user request 11-22); `mf phase` is only needed to override or to finish.
    Planning a performance milestone after a round was integrated starts the next round. Gates still apply."""
    if phase not in PHASES or st.phase not in PHASES:
        return
    cur, new, rnd = PHASES.index(st.phase), PHASES.index(phase), st.round
    if new <= cur:
        if not (phase == "plan" and st.phase in ("integrate", "report")):
            return
        rnd += 1
    from myflow import ms
    for p in PHASES[cur:new]:
        r = 1 if p == "spec" else st.round
        if st.gate_mode(p) == "ask" and not st.gate_ok(p, r) and not (p == "plan" and ms.ensure_plan_gate(st, r)):
            return  # the gate decides; the action reports what is missing
    append_event(st.dir, "phase", why, by="mf", phase=phase, round=rnd)


def cmd_phase(a):
    tdir = task_dir_of(a.task)
    st = State(tdir)
    new = a.phase
    if new not in PHASES:
        raise MfError(f"phase must be one of {PHASES}")
    cur_i, new_i = PHASES.index(st.phase), PHASES.index(new)
    rnd = st.round
    if new == "plan" and st.phase in LOOP_PHASES[1:]:
        rnd += 1
    if new_i > cur_i:
        for p in PHASES[cur_i:new_i]:
            r = 1 if p == "spec" else st.round
            if st.gate_mode(p) == "ask" and not st.gate_ok(p, r):
                raise MfRefused(f"阶段 {p} 需要用户确认后才能离开：先 `mf brief {st.id} --ask \"...\"`，"
                                f"等用户回复后 `mf decide {st.id} --gate {gate_key(p, r)} \"...\"`")
    brief = " ".join(a.brief or []).strip()
    append_event(tdir, "phase", brief, by="agent", phase=new, round=rnd)
    if brief:
        append_event(tdir, "brief", brief[:600], by="agent", phase=new, round=rnd, ask=False)
    print(f"{st.id}: {st.phase} -> {new} (R{rnd})")
    _after(tdir)


def cmd_stop(a):
    tdir = task_dir_of(a.task)
    append_event(tdir, "stop", " ".join(a.reason), by=a.by)
    print("stop requested: mf try will refuse; orchestrator should integrate + report")
    _after(tdir)


# ---------------------------------------------------------------- measurement


def _bench(st: State, ctx: dict, cmd_key: str, lease: Lease, out_dir: Path, timeout: int) -> dict:
    """One frozen bench invocation with idle gates around it. Raw log + parsed metrics stay in out_dir."""
    t = st.task
    cmd = t["commands"].get(cmd_key)
    if not cmd:
        raise MfError(f"commands.{cmd_key} is empty")
    ctx = dict(ctx, gpu=lease.gpu, run=str(out_dir))
    out_dir.mkdir(parents=True, exist_ok=True)
    env = build_env(t, ctx)
    before = gate_snapshot(lease.gpu, t["gpu"])
    hook_rc = None
    if t["commands"].get("gate"):
        hook_rc = run_cmd(expand(t["commands"]["gate"], ctx), ctx["wt"], env, out_dir / "gate.log", 120)["rc"]
    res = run_cmd(expand(cmd, ctx), ctx["wt"], env, out_dir / "out.log", timeout, purge=True)
    after = gate_snapshot(lease.gpu, t["gpu"], after=True)
    res.update(gpu=lease.gpu, gate_before=before, gate_after=after, gate_hook_rc=hook_rc,
               gate_ok=gate_passes(before) and gate_passes(after) and hook_rc in (None, 0))
    try:
        text = Path(res["log"]).read_text(encoding="utf-8", errors="replace")
        res["metrics"], samples = parse_metrics(text, t["parse"], ctx)
        atomic_write(out_dir / "mf_parsed.json", json.dumps({"metrics": res["metrics"], "samples": samples}, indent=1))
    except (OSError, ValueError, KeyError, MfError) as e:
        res["metrics"], res["parse_error"] = {}, str(e)
    return res


def _slim(res: dict) -> dict:
    keep = ("rc", "dur", "timeout", "log", "gpu", "gate_before", "gate_after", "gate_ok", "gate_hook_rc", "parse_error")
    return {k: res[k] for k in keep if k in res}


def _correctness(st: State, ctx: dict, wt, out: Path, timeout: int, key: str = "correctness") -> dict:
    cmd = st.task["commands"].get(key)
    if not cmd:
        raise MfError(f"commands.{key} is empty: correctness must be defined before any measurement")
    with Lease(_pool(st), exclusive=False, who=_who(st, ctx["lane"], key), gpu_cfg=st.task["gpu"],
               wait_s=st.task["gpu"].get("wait_s", 3600)) as L:
        c = dict(ctx, gpu=L.gpu)
        res = run_cmd(expand(cmd, c), wt, build_env(st.task, c), out / "out.log", timeout, purge=True)
        res["gpu"] = L.gpu
    return res


def cmd_freeze(a):
    tdir = task_dir_of(a.task)
    st = State(tdir)
    t = st.task
    for key in ("correctness", "bench"):
        if not t["commands"].get(key):
            raise MfError(f"commands.{key} is empty; set it first (mf set {st.id} commands.{key}='...')")
    if not st.gate_ok("spec", 1):
        print("[mf] 注意：spec 尚未经用户确认；冻结可以先做（便于用真实基线讨论目标），但 try 会被拒绝", file=sys.stderr)
    repo = Path(t["repo"])
    # bootstrap (new operator): the first freeze defaults to the latest milestone (M0), not the repo HEAD
    ref = a.base or (st.last_ms_commit if not st.freeze else None) or t.get("base") or "HEAD"
    base = _resolve_commit(st, ref) if re.match(r"M\d{1,3}[a-z]?$", ref) else rev_parse(repo, ref)
    if t.get("base") != base:
        _set(tdir, [("base", base)], "freeze 解析基线提交", "mf")
        st = State(tdir)
        t = st.task
    wt = tdir / "wt" / "_baseline"
    ensure_worktree(repo, wt, base)
    reset_detached(wt, base, "fd")
    protect = tree_blobs(repo, base, t["scope"]["protect"]) if t["scope"]["protect"] else {}
    if not t["scope"]["protect"]:
        print("[mf] ⚠ scope.protect 为空：无法检测 lane 改测试/bench（强烈建议把正确性测试和 bench 脚本列进去）", file=sys.stderr)
    elif not protect:
        raise MfError("scope.protect matched no files at the base commit; check the globs")
    run_dir = _run_dir(st, "_baseline", "freeze")
    timeout = _timeout(st, a)
    ctx = _ctx(st, "_baseline", wt, run_dir)
    ctx["base"] = base
    append_event(tdir, "run.start", "freeze baseline", by="mf", run=run_dir.name, timeout=timeout * (a.repeats + 2))
    try:
        if t["commands"].get("setup"):
            r = run_cmd(expand(t["commands"]["setup"], ctx), wt, build_env(t, ctx), run_dir / "setup" / "out.log", timeout)
            if r["rc"] != 0:
                raise MfError(f"setup failed on baseline (rc={r['rc']}): {r['log']}\n{tail(r['log'])}")
        c = _correctness(st, ctx, wt, run_dir / "correctness", timeout)
        if c["rc"] != 0:
            raise MfError(f"correctness fails on the BASELINE (rc={c['rc']}); fix the command first: {c['log']}\n{tail(c['log'])}")
        runs, quick = [], None
        with Lease(_pool(st), exclusive=True, who=_who(st, "_baseline", "freeze"), gpu_cfg=t["gpu"], prefer=a.gpu,
                   wait_s=t["gpu"].get("wait_s", 3600)) as L:
            for i in range(a.repeats):
                b = _bench(st, ctx, "bench", L, run_dir / f"bench{i + 1}", timeout)
                if b["rc"] != 0 or b["timeout"]:
                    raise MfError(f"bench failed on baseline (rc={b['rc']}): {b['log']}\n{tail(b['log'])}")
                if not b["gate_ok"]:
                    raise MfError(f"GPU{L.gpu} not idle during baseline run {i + 1} "
                                  f"(before={b['gate_before']}, after={b['gate_after']}); raw kept in {run_dir}")
                if not b["metrics"]:
                    raise MfError(f"no metrics parsed from {b['log']} (parse={t['parse']}).\n{tail(b['log'])}")
                runs.append(b["metrics"])
            if t["commands"].get("bench_quick"):
                q = _bench(st, ctx, "bench_quick", L, run_dir / "quick", timeout)
                quick = q["metrics"] if q["rc"] == 0 and q["gate_ok"] else None
            gpu = L.gpu
    finally:
        append_event(tdir, "run.end", "", by="mf", run=run_dir.name)
    med, noise = combine_runs(runs)
    append_event(tdir, "freeze", f"基线 {base[:10]}：{len(med)} cases，噪声 {render.pct(noise)}", by="mf",
                 base=base, protect=protect, contract=contract_hash(t), goalposts=goalposts(t), baseline=med,
                 baseline_quick=quick, noise=noise, runs=runs, gpu=gpu, repeats=a.repeats, run=str(run_dir),
                 correctness_log=c["log"])
    st = State(tdir)
    if st.phase in ("spec", "bootstrap") and st.gate_ok("spec", 1):
        append_event(tdir, "phase", "基线已冻结", by="mf", phase="freeze", round=1)
    print(f"frozen {st.id} @ {base[:10]} on {render.gpu_txt(gpu)}: {len(med)} cases, noise {render.pct(noise)}")
    for c_, v in list(med.items())[:12]:
        print(f"  {render.pad(c_, 40)} {v:g} {t['metric'].get('name', '')}")
    _after(tdir)


def cmd_lane_new(a):
    tdir = task_dir_of(a.task)
    st = State(tdir)
    if not LANE_RE.match(a.lane) or a.lane.startswith("_"):
        raise MfError("lane name: letters/digits/_- (max 32), not starting with _")
    if a.lane in st.lanes:
        raise MfError(f"lane {a.lane} exists (mf lane set to change status)")
    if not st.freeze:
        raise MfRefused("freeze the baseline first (mf freeze)")
    repo, base = Path(st.task["repo"]), st.freeze["base"]
    # the next work builds on the latest milestone; while one is being closed, on its candidate (pipelining)
    start = ((st.open_ms or {}).get("prep") or {}).get("candidate") or st.last_ms_commit or base
    if a.start == "base":
        start = base
    elif a.start is not None:
        if a.start in st.lanes:
            ln = st.lanes[a.start]
            pick = ln["best_verified"] or ln["best_self"]
            start = pick["commit"] if pick else git(["rev-parse", "HEAD"], ln["wt"])
        else:
            start = _resolve_commit(st, a.start)
    wt = tdir / "wt" / a.lane
    branch = f"mf/{st.id}/{a.lane}"
    ensure_worktree(repo, wt, start, branch)
    merged = []
    for item in (a.merge or "").split(","):
        item = item.strip()
        if not item:
            continue
        name, _, n = item.partition("@")
        src = _lane(st, name)
        if n:
            at = st.attempt(int(n))
            if not at or at.get("lane") != name:
                raise MfError(f"A{n} is not an attempt of lane {name}")
            commit = at["commit"]
        else:
            pick = src["best_verified"] or src["best_self"]
            if not pick:
                raise MfError(f"lane {name} has no passing attempt to merge")
            commit = pick["commit"]
        merge(wt, commit, f"mf {st.id}/{a.lane}: merge {item}")
        merged.append({"lane": name, "commit": commit})
    if st.task["commands"].get("setup"):
        run_dir = _run_dir(st, a.lane, "setup")
        ctx = _ctx(st, a.lane, wt, run_dir)
        r = run_cmd(expand(st.task["commands"]["setup"], ctx), wt, build_env(st.task, ctx), run_dir / "out.log",
                    _timeout(st))
        if r["rc"] != 0:
            print(f"[mf] ⚠ setup failed in lane worktree (rc={r['rc']}): {r['log']}", file=sys.stderr)
    brief = _write_brief(st, a.lane, a.direction, a.hyp, wt)
    append_event(tdir, "lane.new", a.direction, lane=a.lane, by="agent", hyp=a.hyp, round=st.round, wt=str(wt),
                 branch=branch, start=start, merge=merged, brief=str(brief))
    if st.freeze:
        auto_phase(State(tdir), "implement", f"lane {a.lane}")
    print(f"lane {a.lane}: worktree {wt} (branch {branch}, from {start[:10]})")
    print(f"brief: {brief}")
    print(f"subagent prompt: 使用 skill myflow-lane。先完整阅读 {brief} 再开始；所有尝试必须用 `{MF} try {st.id} --lane {a.lane} ...` 提交。")
    _after(tdir)


def _write_brief(st: State, lane: str, direction: str, hyp, wt: Path) -> Path:
    t, b = st.task, st.task["budget"]
    h = st.hyps.get(hyp) if hyp else None
    tools = _tool_index(st) or "（暂无）"
    repl = {
        "task": st.id, "lane": lane, "direction": direction, "goal": t.get("goal", ""), "target": f"{st.target:g}",
        "hyp": f"{hyp}: {h['text']}（预测 {h['predict']}）" if h else (hyp or "（无）"), "wt": str(wt), "mf": MF,
        "task_dir": str(st.dir), "allow": ", ".join(t["scope"]["allow"]) or "（不限，protect 除外）",
        "protect": ", ".join(t["scope"]["protect"]) or "（未设置）", "correctness": t["commands"]["correctness"],
        "bench": t["commands"]["bench"], "bench_quick": t["commands"].get("bench_quick") or "（无）",
        "max_fail_streak": str(b.get("max_fail_streak")), "max_attempts": str(b.get("max_attempts")),
        "max_no_improve": str(b.get("max_no_improve")), "tools": tools, "kb": str(kb.kb_dir() / "INDEX.md"),
        "spec": str(st.dir / "SPEC.md"), "survey": str(st.dir / "survey.md"), "plan": str(st.dir / "plan.md"),
    }
    text = (PKG_ROOT / "templates" / "lane_brief.md").read_text(encoding="utf-8")
    for k, v in repl.items():
        text = text.replace("{{" + k + "}}", v)
    path = st.dir / "lanes" / lane / "brief.md"
    atomic_write(path, text)
    return path


def cmd_lane_set(a):
    tdir = task_dir_of(a.task)
    st = State(tdir)
    _lane(st, a.lane)
    if a.status in ("abandoned", "blocked", "done") and not a.reason:
        raise MfError("--reason is required (why stopped / what evidence); it becomes the lane's epitaph")
    append_event(tdir, "lane.set", " ".join(a.reason or []), lane=a.lane, by="agent", status=a.status)
    print(f"lane {a.lane}: {a.status}")
    _after(tdir, a.lane)


def cmd_try(a):
    tdir = task_dir_of(a.task)
    st = State(tdir)
    ln = _lane(st, a.lane)
    t = st.task
    why = guard.refusal(st, "try", a.lane)
    if why and not a.force:
        raise MfRefused(why)
    if why and a.force:
        append_event(tdir, "note", f"强制越过守卫：{why}", lane=a.lane, by="agent", kind="override")
    from myflow import ms
    if not ms.ensure_plan_gate(st, ln["round"]):
        why = ms.plan_follows_promise(st)[1]
        raise MfRefused(f"R{ln['round']} 的计划需要用户确认（gate plan@R{ln['round']}；{why}）：编排者先 "
                        f"`mf ms plan {st.id} M<k> … --ask`，用户同意后 `mf decide {st.id} --gate plan@R{ln['round']} \"...\"`")
    if ln["status"] != "active":
        raise MfRefused(f"lane {a.lane} is {ln['status']}; reactivate with mf lane set --status active")
    kind = "nobench" if a.no_bench else ("quick" if a.quick else "full")
    if kind == "quick" and not t["commands"].get("bench_quick"):
        raise MfError("commands.bench_quick is not defined")
    with _lane_lock(st, a.lane):
        return _try_locked(a, st, ln, kind)


def _try_locked(a, st: State, ln: dict, kind: str):
    tdir, t = st.dir, st.task
    wt = Path(ln["wt"])
    sbase = _scope_base(st, ln)
    ignore = DEFAULT_IGNORE + list(t["scope"].get("ignore") or [])
    run_dir = _run_dir(st, a.lane, "try")
    timeout = _timeout(st, a)
    oracle = protect_problems(wt, _oracle_map(st, sbase), t["scope"]["protect"]) if t["scope"]["protect"] else []
    scope = scope_violations(changed_files(wt, sbase), t["scope"]["allow"], t["scope"]["protect"], ignore)
    commit = snapshot(wt, t["scope"]["allow"], ignore, f"mf {st.id}/{a.lane}: {a.idea[:100]}", t["scope"]["protect"])
    diff = diffstat(wt, sbase, commit)
    write_patch(wt, sbase, commit, run_dir / "diff.patch")
    data = {"idea": a.idea, "hyp": a.hyp, "kind": kind, "commit": commit, "diff": diff, "note": a.note or "",
            "run": str(run_dir), "round": ln["round"]}
    status = PASS
    if oracle:
        status, data["problems"] = "oracle_modified", oracle
    elif scope:
        status, data["problems"] = "scope_violation", scope
    else:
        ctx = _ctx(st, a.lane, wt, run_dir)
        append_event(tdir, "run.start", f"try {a.lane}: {a.idea[:80]}", lane=a.lane, by="mf", run=run_dir.name,
                     timeout=timeout * 2)
        try:
            c = _correctness(st, ctx, wt, run_dir / "correctness", timeout)
            data["correctness"] = _slim(c)
            if c["timeout"]:
                status = "timeout"
            elif c["rc"] != 0:
                status = "fail_correctness"
            elif kind != "nobench":
                key = "bench_quick" if kind == "quick" else "bench"
                with Lease(_pool(st), exclusive=True, who=_who(st, a.lane, "bench"), gpu_cfg=t["gpu"],
                           wait_s=t["gpu"].get("wait_s", 3600)) as L:
                    b = _bench(st, ctx, key, L, run_dir / "bench", timeout)
                data["bench"] = _slim(b)
                if b["timeout"]:
                    status = "timeout"
                elif b["rc"] != 0:
                    status = "bench_error"
                elif not b["gate_ok"]:
                    status = "gate_failed"
                elif not b["metrics"]:
                    status, data["problems"] = "bench_error", [b.get("parse_error") or "no metrics parsed"]
                else:
                    data["metrics"] = b["metrics"]
                    ref = st.freeze["baseline"] if kind == "full" else (st.freeze.get("baseline_quick") or {})
                    agg, per, missing = speedup(ref, b["metrics"], t["metric"])
                    data.update(speedup=agg, per_case=per)
                    if missing:
                        status, data["problems"] = "incomplete", [f"missing cases: {', '.join(missing[:10])}"]
        finally:
            append_event(tdir, "run.end", "", lane=a.lane, by="mf", run=run_dir.name)
    ev = append_event(tdir, "attempt", a.idea[:200], lane=a.lane, by="mf", counter="attempt", status=status, **data)
    n = ev["data"]["n"]
    st = State(tdir)
    ok = status == PASS
    head = f"A{n} [{a.lane}] {render.STATUS_ZH.get(status, status)} {'✓' if ok else '✗'}"
    if ok and data.get("speedup"):
        best = st.best_self
        tag = "（新的自测最佳）" if best and best.get("n") == n else ""
        head += f"  自测 {render.fx(data['speedup'])}{tag}（对冻结基线、未配对；正式数字需 mf verify）"
    print(head)
    for key in ("correctness", "bench"):
        r = data.get(key)
        if r:
            g = r.get("gate_before")
            print(f"  {key}: rc={r['rc']} {r['dur']}s {render.gpu_txt(r.get('gpu'))}"
                  + (f" 门禁 {render._gate_txt(g)}/{render._gate_txt(r.get('gate_after'))}" if g else "")
                  + f"  log: {r['log']}")
            if r["rc"] != 0 or r.get("timeout"):
                print("  ---- tail ----\n" + "\n".join("  " + x for x in tail(r["log"], 12).splitlines()))
    if data.get("problems"):
        print("  问题: " + "; ".join(data["problems"][:8]))
    if data.get("per_case"):
        print(f"  cases: {_cases_line(data['per_case'])}")
    print(f"  commit {commit[:10]} +{diff['ins']}/-{diff['del']} {diff['files']} files · run {run_dir}")
    b = t["budget"]
    ln = st.lanes[a.lane]
    print(f"  预算: 尝试 {len(st.attempts)}/{b.get('max_attempts')} · 本线连续无提升 {ln['no_improve']}/{b.get('max_no_improve')}"
          f" · 本线连败 {ln['fail_streak']}/{b.get('max_fail_streak')}")
    _after(tdir, a.lane)


def cmd_verify(a):
    tdir = task_dir_of(a.task)
    st = State(tdir)
    ln = _lane(st, a.lane)
    why = guard.refusal(st, "verify")
    if why:
        raise MfRefused(why)
    if a.attempt:
        at = st.attempt(a.attempt)
        if not at or at.get("lane") != a.lane:
            raise MfError(f"A{a.attempt} is not an attempt of lane {a.lane}")
    else:
        at = ln["best_self"]
        if not at:
            raise MfError(f"lane {a.lane} has no passing full attempt; pass --attempt N")
    commit, base = at["commit"], st.freeze["base"]
    against = _resolve_commit(st, a.against) if getattr(a, "against", None) else None
    if against and against == base:
        against = None  # the frozen base is what a plain verify compares against anyway
    with _lane_lock(st, f"_verify-{a.lane}"):
        return _verify_locked(a, st, ln, at, commit, base, against=against,
                              against_ms=a.against if against and re.match(r"M\d", a.against or "") else None)


def _verify_locked(a, st: State, ln: dict, at: dict, commit: str, base: str, against=None, against_ms=None):
    tdir, t = st.dir, st.task
    pairs = a.pairs or int(t["verify"].get("pairs", 4))
    timeout = _timeout(st, a)
    run_dir = _run_dir(st, a.lane, "verify")
    repo = Path(t["repo"])
    vname = f"_verify-{a.lane}"  # one clean worktree per lane, so parallel verifies never share files
    wt_v = st.dir / "wt" / vname
    ensure_worktree(repo, wt_v, commit)
    reset_detached(wt_v, commit, t["verify"].get("clean", "fd"))
    if against:  # incremental: previous milestone (A) vs candidate (B), same GPU, same protocol
        aname = f"_against-{a.lane}"
        wt_a = st.dir / "wt" / aname
        ensure_worktree(repo, wt_a, against)
        reset_detached(wt_a, against, t["verify"].get("clean", "fd"))
        shutil.rmtree(st.dir / "lanes" / aname, ignore_errors=True)
    else:
        aname, wt_a = "_baseline", st.dir / "wt" / "_baseline"
        ensure_worktree(repo, wt_a, base)
        if git(["rev-parse", "HEAD"], wt_a) != base or git(["status", "--porcelain"], wt_a):
            reset_detached(wt_a, base, "fd")  # nobody edits the baseline; only repair it when it drifted
    shutil.rmtree(st.dir / "lanes" / vname, ignore_errors=True)
    seed = random.randint(1, 2 ** 31 - 1)
    ctx_b = _ctx(st, vname, wt_v, run_dir, seed=seed)
    ctx_a = _ctx(st, aname, wt_a, run_dir, seed=seed)
    ignore = DEFAULT_IGNORE + list(t["scope"].get("ignore") or [])
    data = {"attempt": at.get("n"), "commit": commit, "pairs": pairs, "seed": seed, "claim": at.get("speedup"),
            "run": str(run_dir), "base": base, "freeze_seq": st.freeze.get("seq")}
    if against:
        data.update(against=against, against_ms=against_ms)
    verdict, reason = None, ""
    sbase = _scope_base(st, ln)
    problems = protect_problems(wt_v, _oracle_map(st, sbase), t["scope"]["protect"]) if t["scope"]["protect"] else []
    if problems:
        verdict, reason = "oracle_modified", "; ".join(problems[:5])
    else:
        sc = scope_violations(changed_files(wt_v, sbase), t["scope"]["allow"], t["scope"]["protect"], ignore)
        if sc:
            verdict, reason = "scope_violation", "; ".join(sc[:5])
    append_event(tdir, "run.start", f"verify {a.lane} A{at.get('n')}", lane=a.lane, by="mf", run=run_dir.name,
                 timeout=timeout * (2 * pairs + 3))
    try:
        if verdict is None and t["commands"].get("setup"):
            for wt_s, ctx_s in ([(wt_v, ctx_b), (wt_a, ctx_a)] if against else [(wt_v, ctx_b)]):
                r = run_cmd(expand(t["commands"]["setup"], ctx_s), wt_s, build_env(t, ctx_s),
                            run_dir / f"setup-{ctx_s['lane']}" / "out.log", timeout)
                if r["rc"] != 0:
                    verdict, reason = "error", f"setup failed rc={r['rc']} in {wt_s.name}"
                    break
        if verdict is None:
            verdict, reason = _checked(st, a.lane, commit, ctx_b, wt_v, run_dir, timeout, seed, data)
        if verdict is None:
            a_runs, b_runs, arms = [], [], []
            fz_gpu = st.freeze.get("gpu")
            with Lease(_pool(st), exclusive=True, who=_who(st, a.lane, "verify"), gpu_cfg=t["gpu"],
                       prefer=a.gpu if a.gpu is not None else fz_gpu, wait_s=t["gpu"].get("wait_s", 3600)) as L:
                data["gpu"] = L.gpu
                for i, arm in enumerate(abba_order(pairs)):
                    ctx = ctx_a if arm == "A" else ctx_b
                    r = _bench(st, ctx, "bench", L, run_dir / f"{i + 1:02d}{arm}", timeout)
                    arms.append({"arm": arm, "log": r["log"], "metrics": r["metrics"], "gate_ok": r["gate_ok"],
                                 "gate_before": r["gate_before"], "gate_after": r["gate_after"], "rc": r["rc"]})
                    if r["rc"] != 0 or r["timeout"]:
                        verdict, reason = ("timeout" if r["timeout"] else "error"), f"bench {arm} rc={r['rc']} ({r['log']})"
                        break
                    if not r["gate_ok"]:
                        verdict, reason = "gate_failed", f"GPU{L.gpu} busy at run {i + 1}{arm}: {r['gate_before']} / {r['gate_after']}"
                        break
                    (a_runs if arm == "A" else b_runs).append(r["metrics"])
            atomic_write(run_dir / "abba.json", json.dumps({"order": abba_order(pairs), "arms": arms}, indent=1))
            if verdict is None:
                verdict, reason = _judge(analyze_abba(a_runs, b_runs, t["metric"]), t, data, against)
    finally:
        append_event(tdir, "run.end", "", lane=a.lane, by="mf", run=run_dir.name)
    _record_verify(st, a.lane, at, data, verdict, reason, against, against_ms, run_dir)
    _after(tdir, a.lane)


def _checked(st: State, lane, commit: str, ctx: dict, wt, run_dir: Path, timeout: int, seed, data: dict):
    """Correctness (+ verify_extra) for a verify in its clean worktree, reused when the same code and tests already
    passed that way (user request 11-22). Returns (verdict, reason): (None, "") when it passed."""
    from myflow import ms
    hit = ms.reuse_check(st, commit)
    if hit:
        data["correctness"] = {"rc": 0, "log": hit.get("log"), "reused": hit.get("what")}
        return None, ""
    c = _correctness(st, ctx, wt, run_dir / "correctness", timeout)
    data["correctness"] = _slim(c)
    if c["rc"] != 0:
        return "fail_correctness", f"correctness rc={c['rc']} (fresh seed {seed})"
    if st.task["commands"].get("verify_extra"):
        c = _correctness(st, ctx, wt, run_dir / "verify_extra", timeout, key="verify_extra")
        data["verify_extra"] = _slim(c)
        if c["rc"] != 0:
            return "fail_correctness", f"verify_extra rc={c['rc']}"
    ms.record_check(st, commit, f"verify {lane}", data["correctness"].get("log"))
    return None, ""


def _judge(res: dict, t: dict, data: dict, against) -> tuple:
    """Verdict of one comparison from its paired runs; fills speedup, noise, per-case numbers into `data`."""
    sp, noise = res["speedup"], res["noise"] or 0.0
    mi = float(t["metric"].get("min_improve", 0.02))
    data.update(speedup=sp, noise=noise, missing=res["missing"],
                per_case={c: {k: (round(v, 6) if isinstance(v, float) else v) for k, v in d.items()}
                          for c, d in res["per_case"].items()})
    verdict, reason = None, ""
    if res["missing"] or sp is None:
        verdict, reason = "error", f"missing cases: {', '.join(res['missing'][:8])}"
    elif sp >= 1 + max(mi, 2 * noise):
        verdict = "improved"
    elif sp <= 1 - max(0.01, 2 * noise):
        verdict = "regression"
    else:
        verdict, reason = "no_gain", f"|Δ| < max(min_improve {mi:.0%}, 2×noise {2 * noise:.1%})"
    mcr = float(t["verify"].get("max_case_regress", 0.02))
    data["regressed"] = [c for c, d in res["per_case"].items() if d["speedup"] < 1 - max(mcr, 2 * d["noise"])]
    if data.get("claim") and sp and not against:  # the claim is vs the frozen base, not vs a milestone
        data["mismatch"] = data["claim"] > sp * (1 + max(0.05, 3 * noise))
    return verdict, reason


def _record_verify(st: State, lane, at: dict, data: dict, verdict, reason, against, against_ms, run_dir: Path):
    vs = f" vs {against_ms or against[:9]}" if against else ""
    ev = append_event(st.dir, "verify", f"A{at.get('n')}{vs} {verdict}", lane=lane, by="mf", counter="verify",
                      verdict=verdict, reason=reason, **data)
    what = f"增量（对 {against_ms or against[:9]}）" if against else "复核"
    how = f"三方交替 {data.get('pairs')} 轮" if data.get("arms") == 3 else f"ABBA {data.get('pairs')} 对"
    print(f"V{ev['data']['n']} A{at.get('n')} [{lane}] {render.STATUS_ZH.get(verdict, verdict)}  "
          f"{what} {render.fx(data.get('speedup'))}（{how}，{render.gpu_txt(data.get('gpu'))}，噪声 {render.pct(data.get('noise'))}）"
          + ("" if against else f"  自测 {render.fx(data.get('claim'))}{'  ⚠ 自测未复现' if data.get('mismatch') else ''}"))
    if reason:
        print(f"  {reason}")
    if data.get("per_case"):
        print(f"  cases: {_cases_line({c: d['speedup'] for c, d in data['per_case'].items()})}")
    if data.get("regressed"):
        print(f"  ⚠ 变慢超阈值的 case: {', '.join(data['regressed'][:8])}")
    print(f"  run {run_dir}")


def _verify3_locked(a, st: State, ln: dict, at: dict, commit: str, base: str, prev: str, prev_ms):
    """One interleaved run when a milestone needs both numbers (user request 11-22): frozen base (A), previous
    milestone (P) and candidate (B) on the same GPU, A P B / B P A rounds; records the cumulative (vs base) and the
    incremental (vs previous) verify from the same runs. Costs ~1.5 ABBA instead of two."""
    tdir, t = st.dir, st.task
    pairs = a.pairs or int(t["verify"].get("pairs", 4))
    timeout = _timeout(st, a)
    run_dir = _run_dir(st, a.lane, "verify3")
    repo, clean = Path(t["repo"]), t["verify"].get("clean", "fd")
    names = {"B": f"_verify-{a.lane}", "P": f"_against-{a.lane}", "A": "_baseline"}
    wts = {k: st.dir / "wt" / n for k, n in names.items()}
    for k, c in (("B", commit), ("P", prev)):
        ensure_worktree(repo, wts[k], c)
        reset_detached(wts[k], c, clean)
        shutil.rmtree(st.dir / "lanes" / names[k], ignore_errors=True)
    ensure_worktree(repo, wts["A"], base)
    if git(["rev-parse", "HEAD"], wts["A"]) != base or git(["status", "--porcelain"], wts["A"]):
        reset_detached(wts["A"], base, "fd")  # nobody edits the baseline; only repair it when it drifted
    seed = random.randint(1, 2 ** 31 - 1)
    ctx = {k: _ctx(st, names[k], wts[k], run_dir, seed=seed) for k in names}
    ignore = DEFAULT_IGNORE + list(t["scope"].get("ignore") or [])
    common = {"attempt": at.get("n"), "commit": commit, "pairs": pairs, "seed": seed, "claim": at.get("speedup"),
              "run": str(run_dir), "base": base, "freeze_seq": st.freeze.get("seq"), "arms": 3}
    verdict, reason = None, ""
    sbase = _scope_base(st, ln)
    problems = protect_problems(wts["B"], _oracle_map(st, sbase), t["scope"]["protect"]) if t["scope"]["protect"] else []
    if problems:
        verdict, reason = "oracle_modified", "; ".join(problems[:5])
    else:
        sc = scope_violations(changed_files(wts["B"], sbase), t["scope"]["allow"], t["scope"]["protect"], ignore)
        if sc:
            verdict, reason = "scope_violation", "; ".join(sc[:5])
    order = [x for i in range(pairs) for x in (("A", "P", "B") if i % 2 == 0 else ("B", "P", "A"))]
    runs, arms = {"A": [], "P": [], "B": []}, []
    append_event(tdir, "run.start", f"verify3 {a.lane} A{at.get('n')}", lane=a.lane, by="mf", run=run_dir.name,
                 timeout=timeout * (len(order) + 4))
    try:
        if verdict is None and t["commands"].get("setup"):
            for k in ("B", "P", "A"):
                r = run_cmd(expand(t["commands"]["setup"], ctx[k]), wts[k], build_env(t, ctx[k]),
                            run_dir / f"setup-{names[k]}" / "out.log", timeout)
                if r["rc"] != 0:
                    verdict, reason = "error", f"setup failed rc={r['rc']} in {names[k]}"
                    break
        if verdict is None:
            verdict, reason = _checked(st, a.lane, commit, ctx["B"], wts["B"], run_dir, timeout, seed, common)
        if verdict is None:
            with Lease(_pool(st), exclusive=True, who=_who(st, a.lane, "verify"), gpu_cfg=t["gpu"],
                       prefer=a.gpu if a.gpu is not None else st.freeze.get("gpu"), wait_s=t["gpu"].get("wait_s", 3600)) as L:
                common["gpu"] = L.gpu
                for i, arm in enumerate(order):
                    r = _bench(st, ctx[arm], "bench", L, run_dir / f"{i + 1:02d}{arm}", timeout)
                    arms.append({"arm": arm, "log": r["log"], "metrics": r["metrics"], "gate_ok": r["gate_ok"],
                                 "gate_before": r["gate_before"], "gate_after": r["gate_after"], "rc": r["rc"]})
                    if r["rc"] != 0 or r["timeout"]:
                        verdict, reason = ("timeout" if r["timeout"] else "error"), f"bench {arm} rc={r['rc']} ({r['log']})"
                        break
                    if not r["gate_ok"]:
                        verdict, reason = "gate_failed", f"GPU{L.gpu} busy at run {i + 1}{arm}: {r['gate_before']} / {r['gate_after']}"
                        break
                    runs[arm].append(r["metrics"])
            atomic_write(run_dir / "abba.json", json.dumps({"order": order, "arms": arms}, indent=1))
    finally:
        append_event(tdir, "run.end", "", lane=a.lane, by="mf", run=run_dir.name)
    for against, against_ms, ref in ((None, None, "A"), (prev, prev_ms, "P")):
        data = dict(common, **({"against": against, "against_ms": against_ms} if against else {}))
        v, why = verdict, reason
        if v is None:
            v, why = _judge(analyze_abba(runs[ref], runs["B"], t["metric"]), t, data, against)
        _record_verify(st, a.lane, at, data, v, why, against, against_ms, run_dir)
    _after(tdir, a.lane)


def verify_commit(st: State, lane: str, at: dict, against=None, against_ms=None, both=False, pairs=None, gpu=None,
                  timeout=None) -> None:
    """Verify a candidate for `mf ms prep` (an attempt, or an integration lane's head that never ran `mf try`)."""
    ln = _lane(st, lane)
    a = argparse.Namespace(task=st.id, lane=lane, attempt=at.get("n"), pairs=pairs, gpu=gpu, timeout=timeout)
    with _lane_lock(st, f"_verify-{lane}"):
        if both:
            return _verify3_locked(a, st, ln, at, at["commit"], st.freeze["base"], against, against_ms)
        return _verify_locked(a, st, ln, at, at["commit"], st.freeze["base"], against=against, against_ms=against_ms)


def cmd_run(a):
    """Generic logged command (profiling, diagnostics) with the same env/templating/lease/timeout rules."""
    tdir = task_dir_of(a.task)
    st = State(tdir)
    parts = list(a.cmd or [])
    if parts and parts[0] == "--":
        parts = parts[1:]
    # one argument is a shell string ("cd x && make"); several are an argv whose boundaries must survive `bash -c`
    # (joining them with spaces turned `bash -lc "cd W && pytest"` into `bash -lc cd W && pytest`)
    cmd = (parts[0] if len(parts) == 1 else shlex.join(parts)).strip() if parts else ""
    if not cmd:
        raise MfError("usage: mf run <task> [--lane L] -- <command>")
    lane = a.lane or "_task"
    wt = Path(_lane(st, a.lane)["wt"]) if a.lane else Path(st.task["repo"])
    run_dir = _run_dir(st, lane, "run")
    timeout = _timeout(st, a)
    pool = None if a.gpu == "none" else ([int(a.gpu)] if a.gpu not in (None, "auto") else _pool(st))
    append_event(tdir, "run.start", f"run: {cmd[:80]}", lane=a.lane, by="mf", run=run_dir.name, timeout=timeout)
    try:
        with Lease(pool, exclusive=a.exclusive or a.machine_exclusive, machine="excl" if a.machine_exclusive else "shared",
                   who=_who(st, lane, "run"), gpu_cfg=st.task["gpu"], wait_s=st.task["gpu"].get("wait_s", 3600)) as L:
            ctx = _ctx(st, lane, wt, run_dir, gpu=L.gpu)
            r = run_cmd(expand(cmd, ctx), wt, build_env(st.task, ctx), run_dir / "out.log", timeout, purge=bool(a.lane))
    finally:
        append_event(tdir, "run.end", "", lane=a.lane, by="mf", run=run_dir.name)
    append_event(tdir, "note", a.title or f"run: {cmd[:100]}", lane=a.lane, by="mf", kind="run",
                 file=r["log"], rc=r["rc"], dur=r["dur"], gpu=L.gpu)
    print(f"rc={r['rc']} {r['dur']}s {render.gpu_txt(L.gpu)}{' TIMEOUT' if r['timeout'] else ''}  log: {r['log']}")
    print(tail(r["log"], a.tail))
    _after(tdir, a.lane)
    return 0 if r["rc"] == 0 else 1


# ---------------------------------------------------------------- ledger


def cmd_hyp(a):
    tdir = task_dir_of(a.task)
    st = State(tdir)
    if a.action == "add":
        ev = append_event(tdir, "hyp.add", " ".join(a.args), lane=a.lane, by="agent", counter="hyp.add",
                          predict=a.predict or "", test=a.test or "")
        print(f"H{ev['data']['n']}: {' '.join(a.args)}")
    else:
        if len(a.args) < 2:
            raise MfError("usage: mf hyp <task> set H1 confirmed|refuted|open|dropped --evidence '...'")
        hid, status = a.args[0], a.args[1]
        if hid not in st.hyps:
            raise MfError(f"unknown hypothesis {hid}")
        if status not in ("confirmed", "refuted", "open", "dropped"):
            raise MfError("status: confirmed|refuted|open|dropped")
        if status in ("confirmed", "refuted") and not a.evidence:
            raise MfError("--evidence is required (attempt/verify id, file, or measurement)")
        append_event(tdir, "hyp.set", a.evidence or "", lane=a.lane, by="agent", id=hid, status=status)
        print(f"{hid}: {status}")
    _after(tdir, a.lane)


def cmd_ceiling(a):
    tdir = task_dir_of(a.task)
    append_event(tdir, "ceiling", " ".join(a.basis), by="agent", value=a.value)
    print(f"ceiling estimate {a.value:g}× recorded")
    _after(tdir)


def cmd_note(a):
    tdir = task_dir_of(a.task)
    f = None
    if a.file:
        src = Path(a.file).expanduser().resolve()
        if not src.is_file():
            raise MfError(f"{src} not found")
        if tdir.resolve() in src.parents:
            f = str(src)
        else:
            dst = tdir / "notes" / f"{time.strftime('%m%d-%H%M%S')}-{src.name}"
            shutil.copy2(src, dst)
            f = str(dst)
    append_event(tdir, "note", " ".join(a.title)[:300], lane=a.lane, by="agent", kind=a.kind, file=f)
    if a.kind == "survey":
        auto_phase(State(tdir), "survey", "调研")
    print("note recorded" + (f": {f}" if f else ""))
    _after(tdir, a.lane)


def cmd_ping(a):
    """Cheap liveness + 'what I am doing now' line for long stretches without other mf calls."""
    tdir = task_dir_of(a.task)
    append_event(tdir, "ping", " ".join(a.text)[:200], lane=a.lane, by="agent")
    _after(tdir, a.lane)


# alerts the orchestrator itself must act on while sub-agents work in the background
AWAIT_CODES = ("gate_unasked", "no_report", "spin", "stale", "run_lost")


def _needs_orchestrator(st: State) -> list:
    out = [f"{render.ICON[x['level']]} {x['msg']}" for x in guard.alerts(st)
           if x["level"] == "crit" or (x["code"] in AWAIT_CODES and x["level"] == "warn")
           or (x["code"] == "steer" and x["level"] == "warn")]  # a lane agent has not picked up its message
    out += [f"📨 用户留言 #{s['n']}：{s['text'][:200]}（处理后 {MF} ack {st.id} {s['n']} \"<如何处理>\"）"
            for s in st.unacked() if not s["lane"]]
    return out


def cmd_await(a):
    """Block until the orchestrator has to act, or --max-min passes. Records nothing (not agent activity).

    Run it as a background command while sub-agents work: it returns when a gate needs relaying, the user left a
    message, recorded progress is due to be reported, or an agent looks stuck / spinning."""
    tdir = task_dir_of(a.task)
    end = time.time() + a.max_min * 60
    while True:
        st = State(tdir)
        if st.done():
            print(f"mf await {st.id}: 任务已完成")
            return 0
        why = _needs_orchestrator(st)
        if why:
            print(f"mf await {st.id}: 需要编排者处理（{len(why)} 项）——处理后再启动 mf await")
            print("\n".join(f"  {w}" for w in why))
            return 0
        if time.time() >= end:
            last = f"{render.age(time.time() - st.last_report_t)} 前" if st.last_report_t else "从未"
            print(f"mf await {st.id}: {a.max_min:g} 分钟内没有需要编排者处理的事（最后活动 "
                  f"{render.age(time.time() - st.last_agent_t)} 前，最后向用户汇报 {last}）")
            return 0
        time.sleep(max(0.05, min(a.poll_s, end - time.time())))


def cmd_steer(a):
    tdir = task_dir_of(a.task)
    ev = append_event(tdir, "steer", " ".join(a.text)[:2000], lane=a.lane, by=a.by, counter="steer")
    print(f"steer #{ev['data']['n']} queued: agents see it on their next mf call")
    render.render_task(State(tdir))


def cmd_ack(a):
    tdir = task_dir_of(a.task)
    st = State(tdir)
    if a.n not in st.steers:
        raise MfError(f"no steer #{a.n}")
    append_event(tdir, "ack", " ".join(a.how), lane=a.lane, by="agent", n=a.n)
    print(f"steer #{a.n} acknowledged")
    _after(tdir, a.lane)


# ---------------------------------------------------------------- views


def cmd_status(a):
    states = [State(d) for d in _dirs()]
    if a.json:
        print(json.dumps([render.summary(s) for s in states], ensure_ascii=False, indent=1, default=str))
        return
    print(render.term_overview(states))
    try:
        p = render.render_index()
        print(f"\ndashboard: {p}   (live: mf serve)")
    except OSError:
        pass


def _dirs():
    from myflow.core import all_task_dirs
    return all_task_dirs()


def cmd_show(a):
    st = _st(a.task)
    if a.json:
        print(render.dump_json(st))
    elif a.attempt:
        print(render.text_attempt(st, a.attempt))
    elif a.lane:
        ln = _lane(st, a.lane)
        print(f"lane {ln['name']} · {ln['status']} {ln['reason']}\n  方向: {ln['direction']}\n  worktree: {ln['wt']}")
        for at in ln["attempts"]:
            print(f"  A{at.get('n')} {render.STATUS_ZH.get(at.get('status'), at.get('status'))} "
                  f"{render.fx(at.get('speedup'))} {at.get('idea', '')[:80]}")
    else:
        print(render.status_md(st))
    _after(st.dir, a.lane)


def cmd_tail(a):
    st = _st(a.task)
    for e in st.events[-a.n:]:
        print(f"{e.get('seq'):>4} {render.hm(e.get('t'))} {e.get('kind'):<15} {e.get('lane', '') or '':<10} "
              f"{e.get('by', ''):<5} {e.get('title', '')[:110]}")


def cmd_watch(a):
    try:
        while True:
            out = render.term_overview([State(d) for d in _dirs()]) if not a.task else render.status_md(_st(a.task))
            sys.stdout.write("\033[2J\033[H" + out + "\n")
            sys.stdout.flush()
            time.sleep(a.interval)
    except KeyboardInterrupt:
        return 0


def cmd_render(a):
    dirs = [task_dir_of(a.task)] if a.task else _dirs()
    for d in dirs:
        render.render_task(State(d))
        print(d / "dashboard.html")
    print(render.render_index())


def cmd_serve(a):
    from myflow.serve import serve
    serve(a.host, a.port)


def cmd_guard(a):
    st = _st(a.task)
    al = guard.alerts(st)
    for x in al:
        print(f"{render.ICON[x['level']]} {x['level']:<4} {x['code']:<16} {x['msg']}")
    if not al:
        print("● ok")
    h = guard.health(al)
    return 2 if h == "crit" else (1 if h == "warn" or any(x["level"] == "ask" for x in al) else 0)


def cmd_audit(a):
    """Re-derive every recorded number from raw logs; any mismatch means the record was edited."""
    st = _st(a.task)
    problems = audit_chain(st.dir)
    t = st.task

    def reparse(log):
        text = Path(log).read_text(encoding="utf-8", errors="replace")
        return parse_metrics(text, t["parse"], {"run": str(Path(log).parent)})[0]

    def same(x, y):
        return set(x) == set(y) and all(abs(x[k] - y[k]) <= 1e-9 * max(1.0, abs(y[k])) for k in x)

    if st.freeze:
        runs = sorted(Path(st.freeze["run"]).glob("bench*/out.log"))
        parsed = [reparse(p) for p in runs]
        if len(parsed) != len(st.freeze.get("runs", [])) or not all(same(p, r) for p, r in zip(parsed, st.freeze["runs"])):
            problems.append("freeze: baseline runs do not match raw logs")
        elif not same(combine_runs(parsed)[0], st.freeze["baseline"]):
            problems.append("freeze: baseline medians do not match raw logs")
    for at in st.attempts:
        b = at.get("bench") or {}
        if at.get("status") == PASS and b.get("log"):
            if not Path(b["log"]).exists():
                problems.append(f"A{at.get('n')}: raw log missing {b['log']}")
            elif not same(reparse(b["log"]), at.get("metrics", {})):
                problems.append(f"A{at.get('n')}: metrics do not match raw log")
            elif at.get("kind", "full") == "full" and st.freeze:
                sp = speedup(st.freeze["baseline"], at["metrics"], t["metric"])[0]
                if sp and at.get("speedup") and abs(sp - at["speedup"]) > 1e-9:
                    problems.append(f"A{at.get('n')}: speedup {at['speedup']} != recomputed {sp}")
    for v in st.verifies:
        if v.get("verdict") not in ("improved", "no_gain", "regression"):
            continue
        p = Path(v["run"]) / "abba.json"
        if not p.exists():
            problems.append(f"V{v.get('n')}: abba.json missing")
            continue
        arms = json.loads(p.read_text())["arms"]
        A = [reparse(x["log"]) for x in arms if x["arm"] == "A"]
        B = [reparse(x["log"]) for x in arms if x["arm"] == "B"]
        sp = analyze_abba(A, B, t["metric"])["speedup"]
        if sp is None or abs(sp - v["speedup"]) > 1e-6:
            problems.append(f"V{v.get('n')}: verified speedup {v.get('speedup')} != recomputed {sp}")
    for x in problems:
        print(f"✖ {x}")
    print("audit ok: chain intact, all numbers re-derived from raw logs" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


def cmd_gpu(a):
    snap = smi_snapshot() or {}
    holders = lease_holders()
    gpus = sorted(set(snap) | {h["gpu"] for h in holders})
    if not gpus:
        print("no GPU telemetry (rocm-smi missing?) and no leases")
    for g in gpus:
        s = snap.get(g, {})
        hs = [h for h in holders if h["gpu"] == g]
        who = "; ".join(f"{'EXCL' if h['exclusive'] else 'shared'} {h['who']} pid{h['pid']} {render.age(time.time() - (h['since'] or time.time()))}"
                        for h in hs)
        print(f"GPU{g}: use {s.get('use', '?')}% vram {s.get('vram', '?')}%  {who or 'free'}")


def cmd_report(a):
    tdir = task_dir_of(a.task)
    st = State(tdir)
    t = st.task
    cands = [v for v in st.verifies if v.get("verdict") in ("improved", "no_gain", "regression") and not v.get("against")
             and st.same_epoch(v)]
    if a.lane:
        cands = [v for v in cands if v.get("lane") == a.lane]
    final = max(cands, key=lambda v: v["speedup"]) if cands else None
    L = [f"# 报告：{st.id} — {t.get('title', '')}", "",
         f"- 目标：{t.get('goal', '')}（目标 {st.target:g}×，指标 {t['metric'].get('name')}，{t['metric'].get('direction')}）",
         f"- 基线：`{(st.freeze or {}).get('base', '')}` · 生成：{render.hm(time.time())} · 本报告所有数字来自 mf 记录（mf audit 可复核）", ""]
    summ = [n for n in st.notes if n["kind"] == "summary"]
    if summ:
        L += ["## 摘要（agent）", *[f"- {n['title']}" for n in summ[-3:]], ""]
    L.append("## 最终结果（独立复核）")
    if final:
        L += [f"**{render.fx(final['speedup'])}**（A{final.get('attempt')} [{final.get('lane')}]，提交 `{final['commit'][:12]}`，"
              f"ABBA {final.get('pairs')} 对，{render.gpu_txt(final.get('gpu'))}，噪声 {render.pct(final.get('noise'))}，结论 {final.get('verdict')}）", "",
              "| case | 基线 | 新 | 加速 | 噪声 |", "|---|---|---|---|---|"]
        for c, d in sorted(final.get("per_case", {}).items()):
            L.append(f"| {c} | {d.get('a'):g} | {d.get('b'):g} | {render.fx(d.get('speedup'))} | {render.pct(d.get('noise'))} |")
        patch = tdir / "final.patch"
        write_patch(Path(t["repo"]), st.freeze["base"], final["commit"], patch)
        L += ["", f"补丁：`{patch}`（git diff 基线..最终提交）", ""]
    else:
        L += ["没有通过复核的结果。", ""]
    if st.milestones:
        L += ["## 里程碑", render.roadmap_md(st), ""]
    if st.lanes:
        L += ["## Lanes", "| lane | 方向 | 结局 | 尝试 | 最佳复核 |", "|---|---|---|---|---|"]
        for ln in st.lanes.values():
            L.append(f"| {ln['name']} | {ln['direction']} | {ln['status']} {ln['reason']} | {len(ln['attempts'])} | "
                     f"{render.fx(ln['best_verified'] and ln['best_verified']['speedup'])} |")
        L.append("")
    if st.hyps:
        L += ["## 假设", *[f"- {h['id']} [{h['status']}] {h['text']} — 预测 {h['predict']}；证据 {h['evidence']}" for h in st.hyps.values()], ""]
    dead = [ln for ln in st.lanes.values() if ln["status"] in ("abandoned", "blocked")]
    dn = [n for n in st.notes if n["kind"] in ("deadend", "lesson")]
    if dead or dn:
        L += ["## 死路与经验", *[f"- lane {ln['name']}（{ln['status']}）：{ln['reason']}" for ln in dead],
              *[f"- [{n['kind']}] {n['title']}" for n in dn], ""]
    if st.decisions or st.changes:
        L += ["## 决策与目标变更", *[f"- {render.hm(d['t'])}（{d['by']}）{d['gate'] or ''} {d['text']}" for d in st.decisions],
              *[f"- {render.hm(c['t'])}（{c['by']}）{c['key']}: {c['old']} → {c['new']} {c['why']}" for c in st.changes], ""]
    b = t["budget"]
    L += ["## 预算", f"尝试 {len(st.attempts)}/{b.get('max_attempts')} · 复核 {len(st.verifies)} 次 · 用时 {st.hours:.1f}h", ""]
    al = [x for x in guard.alerts(st) if x["level"] in ("crit", "warn", "ask")]
    if al:
        L += ["## 未解决的预警", *[f"- {render.ICON[x['level']]} {x['msg']}" for x in al], ""]
    L += ["## 复现", f"- 正确性：`{t['commands']['correctness']}`", f"- bench：`{t['commands']['bench']}`",
          f"- 复核：`{MF} verify {st.id} --lane {final.get('lane') if final else '<lane>'} --attempt {final.get('attempt') if final else 'N'}`", ""]
    path = tdir / "REPORT.md"
    atomic_write(path, "\n".join(L))
    append_event(tdir, "note", "生成报告 REPORT.md", by="mf", kind="report", file=str(path))
    print(path)
    _after(tdir)


# ---------------------------------------------------------------- knowledge, tools, install


def cmd_kb(a):
    if a.action == "index":
        print(kb.index())
    elif a.action == "lint":
        probs = kb.lint()
        print("\n".join(probs) or "kb ok")
        return 1 if any("(warn)" not in p for p in probs) else 0
    elif a.action == "search":
        print("\n".join(kb.search(a.args)) or "no match")
    elif a.action == "new":
        if not a.args:
            raise MfError("usage: mf kb new <name> --kind technique|pitfall|dead-end|measurement")
        print(kb.new(a.args[0], a.kind))


def _doc_line(p: Path) -> str:
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if p.suffix == ".py":
        try:
            doc = ast.get_docstring(ast.parse(text))
            if doc:
                return doc.strip().splitlines()[0]
        except SyntaxError:
            pass
    for line in text.splitlines()[:10]:
        if line.startswith("#") and not line.startswith("#!"):
            return line.lstrip("# ").strip()
    return ""


def _tool_index(st: State = None) -> str:
    rows = []
    dirs = ([st.dir / "tools"] if st else []) + [PKG_ROOT / "tools"]
    for d in dirs:
        if d.is_dir():
            for p in sorted(d.rglob("*")):
                if p.is_file() and p.suffix in (".py", ".sh"):
                    rows.append(f"- `{p}` — {_doc_line(p) or '（缺 docstring：第一行写用途+用法）'}")
    return "\n".join(rows)


def cmd_tools(a):
    print(_tool_index(_st(a.task) if a.task else None) or "no tools yet (put reusable scripts in <task>/tools/ with a one-line docstring)")


def cmd_config(a):
    """Show or set where myflow keeps its data (persistent; an environment variable still wins)."""
    cfg, changed = load_config(), False
    before = {k: setting(k)[0] for k in CONFIG_KEYS}
    for item in a.items or []:
        key, sep, value = item.partition("=")
        if key not in CONFIG_KEYS or not sep:
            raise MfError(f"use key=value with key in {', '.join(CONFIG_KEYS)}")
        cfg[key] = str(Path(value).expanduser().resolve()) if value else None
        changed = True
    for key in a.unset or []:
        if key not in CONFIG_KEYS:
            raise MfError(f"unknown key {key}: {', '.join(CONFIG_KEYS)}")
        cfg.pop(key, None)
        changed = True
    if changed:
        cfg = {k: v for k, v in cfg.items() if v}
        atomic_write(config_path(), json.dumps(cfg, indent=1, ensure_ascii=False) + "\n")
        old_home, new_home = before["home"], setting("home")[0]
        if new_home != old_home:  # the task list moves with home; task directories stay where they are
            old = json.loads((old_home / "registry.json").read_text()) if (old_home / "registry.json").is_file() else {}
            moved = [tid for tid in old if tid not in registry()]
            for tid in moved:
                register(tid, Path(old[tid]["dir"]))
            print(f"home 改到 {new_home}：登记了原有的 {len(moved)} 个任务（任务目录不移动）；运行中的任务请先停下再改")
    for key, env in CONFIG_KEYS.items():
        value, source = setting(key)
        print(f"{key:<10} {value}  （{source}；环境变量 {env} 优先）")
    print(f"配置文件：{config_path()}（环境变量 MYFLOW_CONFIG 可指定别的位置）")
    print("tasks_dir 只影响之后 mf new 的任务；单个任务也可以 mf new <id> --dir <目录>")


def cmd_install_skills(a):
    targets = []
    if a.user:
        targets += [Path.home() / ".copilot" / "skills", Path.home() / ".claude" / "skills"]
    if a.project:
        proj = Path(a.project).expanduser().resolve()
        targets.append(proj / ".github" / "skills")
        if (proj / ".claude").is_dir():
            targets.append(proj / ".claude" / "skills")
    targets += [Path(x).expanduser() for x in (a.target or [])]
    if not targets:
        raise MfError("choose --user, --project PATH or --target DIR")
    for tgt in targets:
        tgt.mkdir(parents=True, exist_ok=True)
        for sk in sorted((PKG_ROOT / "skills").iterdir()):
            if not (sk / "SKILL.md").is_file():
                continue
            dst = tgt / sk.name
            if dst.is_symlink() and dst.resolve() == sk.resolve():
                print(f"= {dst}")
                continue
            if dst.exists() or dst.is_symlink():
                print(f"! {dst} exists, skipped")
                continue
            if a.copy:
                shutil.copytree(sk, dst)
            else:
                dst.symlink_to(sk)
            print(f"+ {dst}")
    if a.project:
        excl = Path(git(["rev-parse", "--git-common-dir"], proj)) / "info" / "exclude"
        if not excl.is_absolute():
            excl = proj / excl
        lines = excl.read_text().splitlines() if excl.exists() else []
        add = [f"/.github/skills/{sk.name}" for sk in (PKG_ROOT / "skills").iterdir() if f"/.github/skills/{sk.name}" not in lines]
        if add:
            excl.parent.mkdir(parents=True, exist_ok=True)
            excl.write_text("\n".join(lines + ["# myflow skills (symlinks)"] + add) + "\n")
            print(f"+ {excl}: ignore installed skill links")


def cmd_selftest(a):
    import unittest
    suite = unittest.defaultTestLoader.discover(str(PKG_ROOT / "tests"), pattern="test_*.py")
    res = unittest.TextTestRunner(verbosity=1 if not a.verbose else 2).run(suite)
    return 0 if res.wasSuccessful() else 1


def _ms_dispatch(a):
    from myflow import ms
    return getattr(ms, f"cmd_{a.ms_cmd}")(a)


def _doc_dispatch(a):
    from myflow import ms
    return getattr(ms, f"cmd_doc_{a.doc_cmd}")(a)


# ---------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mf", description="myflow: visible, verifiable, budgeted kernel optimization")
    p.add_argument("--version", action="version", version=f"myflow {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def cmd(name, fn, help_):
        sp = sub.add_parser(name, help=help_, description=help_)
        sp.set_defaults(func=fn)
        return sp

    s = cmd("new", cmd_new, "create a task (contract + SPEC.md + event log)")
    s.add_argument("id")
    s.add_argument("--repo", required=True)
    s.add_argument("--title")
    s.add_argument("--goal")
    s.add_argument("--target", type=float)
    s.add_argument("--dir")
    s.add_argument("--parent")
    s.add_argument("--from", dest="from_file", help="task.json template to start from")

    s = cmd("set", cmd_set, "change task.json fields (logged; contract keys after freeze require re-freeze)")
    s.add_argument("task")
    s.add_argument("pairs", nargs="+", metavar="key=value")
    s.add_argument("--why")
    s.add_argument("--by", default="agent", choices=["agent", "user"])

    s = cmd("budget", cmd_budget, "extend budgets, e.g. --attempts +10 (logged)")
    s.add_argument("task")
    s.add_argument("--attempts")
    s.add_argument("--hours")
    s.add_argument("--no-improve", dest="no_improve")
    s.add_argument("--why")
    s.add_argument("--by", default="agent", choices=["agent", "user"])

    s = cmd("brief", cmd_brief, "short checkpoint summary (<=600 chars); --ask opens a user gate")
    s.add_argument("task")
    s.add_argument("text", nargs="+")
    s.add_argument("--ask", action="store_true")
    s.add_argument("--lane")
    s.add_argument("--phase")

    s = cmd("decide", cmd_decide, "record a decision; --gate KEY|auto resolves a pending gate")
    s.add_argument("task")
    s.add_argument("text", nargs="+")
    s.add_argument("--gate")
    s.add_argument("--quote", help="the user's exact words")
    s.add_argument("--by", choices=["agent", "user"])
    s.add_argument("--default", action="store_true",
                   help="decided by the default (recommended) option under the user's delegation (mf delegate)")

    s = cmd("delegate", cmd_delegate, "the user lets the orchestrator decide by the defaults for H hours")
    s.add_argument("task")
    s.add_argument("--hours", type=float)
    s.add_argument("--quote", help="the user's exact words")
    s.add_argument("--off", action="store_true")

    s = cmd("asked", cmd_asked, "record that a pending gate was actually put to the user (orchestrator)")
    s.add_argument("task")
    s.add_argument("gates", nargs="+", metavar="gate")
    s.add_argument("--via", default="chat", help="chat | form | web ...")

    s = cmd("phase", cmd_phase, f"move to a phase: {' '.join(PHASES)}")
    s.add_argument("task")
    s.add_argument("phase")
    s.add_argument("--brief", nargs="+")

    s = cmd("stop", cmd_stop, "request stop: no more attempts, go integrate/report")
    s.add_argument("task")
    s.add_argument("reason", nargs="+")
    s.add_argument("--by", default="user", choices=["agent", "user"])

    s = cmd("freeze", cmd_freeze, "freeze baseline: base commit, oracle hashes, baseline metrics + noise")
    s.add_argument("task")
    s.add_argument("--repeats", type=int, default=None)
    s.add_argument("--base")
    s.add_argument("--gpu", type=int)
    s.add_argument("--timeout", type=int)

    lane = cmd("lane", lambda a: a.lane_func(a), "manage lanes (parallel implementation lines)")
    lsub = lane.add_subparsers(dest="lane_cmd", required=True)
    s = lsub.add_parser("new", help="worktree + branch + brief for a new lane")
    s.set_defaults(lane_func=cmd_lane_new)
    s.add_argument("task")
    s.add_argument("lane")
    s.add_argument("--direction", required=True)
    s.add_argument("--hyp")
    s.add_argument("--from", dest="start", help="base (default) | <lane> (its best) | <commit>")
    s.add_argument("--merge", help="integration: L1,L2@7 (best or given attempt of each lane)")
    s = lsub.add_parser("set", help="set lane status")
    s.set_defaults(lane_func=cmd_lane_set)
    s.add_argument("task")
    s.add_argument("lane")
    s.add_argument("--status", required=True, choices=["active", "paused", "done", "abandoned", "blocked"])
    s.add_argument("--reason", nargs="+")

    s = cmd("try", cmd_try, "snapshot lane worktree, run frozen correctness + bench, record attempt")
    s.add_argument("task")
    s.add_argument("--lane", required=True)
    s.add_argument("--idea", required=True)
    s.add_argument("--hyp")
    s.add_argument("--note")
    s.add_argument("--quick", action="store_true", help="use commands.bench_quick (not counted toward best)")
    s.add_argument("--no-bench", action="store_true", help="correctness only")
    s.add_argument("--force", action="store_true", help="override budget guard (logged and shown to the user)")
    s.add_argument("--timeout", type=int)

    s = cmd("verify", cmd_verify, "independent re-measure: clean worktree, fresh seed, ABBA vs frozen baseline")
    s.add_argument("task")
    s.add_argument("--lane", required=True)
    s.add_argument("--attempt", type=int)
    s.add_argument("--pairs", type=int)
    s.add_argument("--gpu", type=int)
    s.add_argument("--timeout", type=int)
    s.add_argument("--against", help="incremental ABBA vs a milestone (M3) or commit instead of the frozen base")

    ms = cmd("ms", _ms_dispatch, "milestones: plan → prep → role (review) → close (tag + push); "
                                 "show · push · drop · revoke · bootstrap")
    msub = ms.add_subparsers(dest="ms_cmd", required=True)
    s = msub.add_parser("plan", help="plan card (≤10 lines); --ask opens this round's plan gate (before the freeze: the bootstrap gate)")
    s.add_argument("task")
    s.add_argument("id", help="M1, M2, M5a ...")
    s.add_argument("--title")
    s.add_argument("--type", choices=["feature", "perf", "analysis", "maint"])
    s.add_argument("--goal", action="append", help="SPEC goal it serves (repeatable)")
    s.add_argument("--accept", help="acceptance criteria")
    s.add_argument("--expect", type=float, help="expected speedup vs the previous milestone, e.g. 1.10")
    s.add_argument("--basis", help="why we expect it: ceiling / ATT / Amdahl ...")
    s.add_argument("--scope", help="modules / files it will touch")
    s.add_argument("--hours", type=float, help="estimated effort")
    s.add_argument("--tentative", action="store_true")
    s.add_argument("--why", help="why this differs from what the last push promised")
    s.add_argument("--ask", action="store_true")
    s = msub.add_parser("bootstrap", help="new operator: a worktree to build the functional M0 before freeze")
    s.add_argument("task")
    s.add_argument("--from", dest="start")
    s = msub.add_parser("prep", help="pick the candidate, incremental ABBA vs the last milestone, integration worktree, role prompts")
    s.add_argument("task")
    s.add_argument("id")
    s.add_argument("--lane")
    s.add_argument("--attempt", type=int)
    s.add_argument("--bootstrap", action="store_true", help="candidate = the bootstrap worktree (M0 of a new operator)")
    s.add_argument("--size-why", help="reason to exceed milestone.max_lines (shown on the card)")
    s.add_argument("--regress-why", help="reason to accept cases that got slower than the previous milestone")
    s.add_argument("--verify-why", help="user-approved reason to use the latest of disagreeing repeated verifies")
    s.add_argument("--pairs", type=int)
    s.add_argument("--gpu", type=int)
    s.add_argument("--timeout", type=int)
    s = msub.add_parser("role", help="record the closing review (docs, coverage, missing tests, card); never code")
    s.add_argument("task")
    s.add_argument("id")
    s.add_argument("--role", default="review", choices=["review"], help="(kept for old prompts; the only role)")
    s.add_argument("--result", default="done", choices=["done"], help="(kept for old prompts)")
    s.add_argument("--session", required=True, help="the review sub-agent's session / name")
    s.add_argument("--card", help="milestone card file (default: milestones/<M>/card.md)")
    s.add_argument("--tests-why", help="why changing or deleting existing tests/bench keeps the test principle "
                                       "(required then; goes into the commit message)")
    s.add_argument("-m", "--summary", default="", help="one-line conclusion of the review")
    s = msub.add_parser("close", help="final commit (runtime tree = accepted candidate), correctness on it, tag, push")
    s.add_argument("task")
    s.add_argument("id")
    s.add_argument("--next", action="append", required=True,
                   help="planned milestone id(s), first = committed; or done|report|stop|await-user")
    s.add_argument("--why", help="why the plan changed vs the last promise")
    s = msub.add_parser("push", help="push closed milestones (branch + tags, atomic, fast-forward only)")
    s.add_argument("task")
    s = msub.add_parser("drop", help="abandon a planned or closing milestone")
    s.add_argument("task")
    s.add_argument("id")
    s.add_argument("reason", nargs="+")
    s = msub.add_parser("revoke", help="take back the latest closed milestone; the same id can be planned again")
    s.add_argument("task")
    s.add_argument("id")
    s.add_argument("--why", required=True, help="why it is taken back (shown on the dashboard)")
    s.add_argument("--discard", action="store_true", help="reset the bootstrap worktree even if it has uncommitted changes")
    s = msub.add_parser("show", help="roadmap, or one milestone card")
    s.add_argument("task")
    s.add_argument("id", nargs="?")

    doc = cmd("doc", _doc_dispatch, "entry document: render the mf-generated blocks")
    dsub = doc.add_subparsers(dest="doc_cmd", required=True)
    s = dsub.add_parser("render", help="fill <!-- mf:summary|roadmap|perf --> blocks")
    s.add_argument("task")
    s.add_argument("--file")

    s = cmd("run", cmd_run, "run any command logged into the task (profiling: --machine-exclusive)")
    s.add_argument("task")
    s.add_argument("--lane")
    s.add_argument("--gpu", help="auto | none | N")
    s.add_argument("--exclusive", action="store_true")
    s.add_argument("--machine-exclusive", action="store_true", help="for PMC etc. that perturb every GPU")
    s.add_argument("--timeout", type=int)
    s.add_argument("--title")
    s.add_argument("--tail", type=int, default=20)
    s.add_argument("cmd", nargs="*", help="the command, after --")

    s = cmd("hyp", cmd_hyp, "hypothesis ledger: add 'text' --predict ... | set H1 confirmed --evidence ...")
    s.add_argument("task")
    s.add_argument("action", choices=["add", "set"])
    s.add_argument("args", nargs="+")
    s.add_argument("--predict")
    s.add_argument("--test")
    s.add_argument("--evidence")
    s.add_argument("--lane")

    s = cmd("ceiling", cmd_ceiling, "record an upper-bound speedup estimate (roofline/Amdahl) and its basis")
    s.add_argument("task")
    s.add_argument("value", type=float)
    s.add_argument("basis", nargs="+")

    s = cmd("note", cmd_note, "attach a detail note/file (L2/L3)")
    s.add_argument("task")
    s.add_argument("title", nargs="+")
    s.add_argument("--lane")
    s.add_argument("--kind", default="info", choices=["info", "finding", "lesson", "deadend", "summary", "survey", "plan"])
    s.add_argument("--file")

    s = cmd("ping", cmd_ping, "say what you are doing now (liveness; shown as 最近动作)")
    s.add_argument("task")
    s.add_argument("text", nargs="+")
    s.add_argument("--lane")

    s = cmd("await", cmd_await, "orchestrator: block (in the background) until something needs you, or --max-min passes")
    s.add_argument("task")
    s.add_argument("--max-min", type=float, default=180, help="return anyway after this many minutes")
    s.add_argument("--poll-s", type=float, default=60, help="seconds between checks")

    s = cmd("steer", cmd_steer, "leave a message for the agents (shown on their next mf call)")
    s.add_argument("task")
    s.add_argument("text", nargs="+")
    s.add_argument("--lane")
    s.add_argument("--by", default="user", choices=["agent", "user"])

    s = cmd("ack", cmd_ack, "acknowledge a steer message and say how it was handled")
    s.add_argument("task")
    s.add_argument("n", type=int)
    s.add_argument("how", nargs="+")
    s.add_argument("--lane")

    s = cmd("status", cmd_status, "L0 overview of all tasks")
    s.add_argument("--json", action="store_true")

    s = cmd("show", cmd_show, "L1 task view, or L2 with --attempt / --lane")
    s.add_argument("task")
    s.add_argument("--attempt", type=int)
    s.add_argument("--lane")
    s.add_argument("--json", action="store_true")

    s = cmd("tail", cmd_tail, "recent events")
    s.add_argument("task")
    s.add_argument("-n", type=int, default=25)

    s = cmd("watch", cmd_watch, "refreshing terminal view")
    s.add_argument("task", nargs="?")
    s.add_argument("--interval", type=float, default=10)

    s = cmd("render", cmd_render, "regenerate STATUS.md / dashboard.html / index.html")
    s.add_argument("task", nargs="?")

    s = cmd("serve", cmd_serve, "local web dashboard with steer/approve forms")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)

    s = cmd("guard", cmd_guard, "print alerts; exit 0 ok / 1 warn|ask / 2 crit")
    s.add_argument("task")

    s = cmd("audit", cmd_audit, "verify hash chain and re-derive all numbers from raw logs")
    s.add_argument("task")

    cmd("gpu", cmd_gpu, "GPU telemetry and myflow leases")

    s = cmd("report", cmd_report, "generate REPORT.md + final.patch from verified records")
    s.add_argument("task")
    s.add_argument("--lane")

    s = cmd("kb", cmd_kb, "knowledge cards: index | lint | search WORDS | new NAME --kind K")
    s.add_argument("action", choices=["index", "lint", "search", "new"])
    s.add_argument("args", nargs="*")
    s.add_argument("--kind", default="technique")

    s = cmd("tools", cmd_tools, "list reusable scripts (task tools/ and global tools/)")
    s.add_argument("task", nargs="?")

    s = cmd("config", cmd_config, "show / set data locations: home (registry, overview), tasks_dir (new tasks), lock_dir")
    s.add_argument("items", nargs="*", metavar="key=value")
    s.add_argument("--unset", action="append", metavar="key")

    s = cmd("install-skills", cmd_install_skills, "symlink myflow skills into agent skill folders")
    s.add_argument("--user", action="store_true", help="~/.copilot/skills and ~/.claude/skills")
    s.add_argument("--project", help="<repo>/.github/skills (+ .claude/skills if present)")
    s.add_argument("--target", action="append")
    s.add_argument("--copy", action="store_true")

    s = cmd("selftest", cmd_selftest, "run myflow's own tests (CPU only)")
    s.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv=None) -> int:
    install_signal_handlers()
    argv = list(sys.argv[1:] if argv is None else argv)
    tail = None
    if argv and argv[0] == "run" and "--" in argv:  # `mf run <task> [opts] -- <cmd...>`: options stay options
        i = argv.index("--")
        argv, tail = argv[:i], argv[i + 1:]
    args = build_parser().parse_args(argv)
    if tail is not None:
        args.cmd = tail
    if getattr(args, "cmd", None) == "freeze" and args.repeats is None:
        args.repeats = int(State(task_dir_of(args.task)).task["freeze"].get("repeats", 3))
    try:
        return args.func(args) or 0
    except BrokenPipeError:  # e.g. `mf status | head`
        try:
            sys.stdout = open(os.devnull, "w")
        except OSError:
            pass
        return 0
    except MfRefused as e:
        print(f"[mf] REFUSED: {e}", file=sys.stderr)
        if getattr(args, "task", None):
            try:
                _after(task_dir_of(args.task), getattr(args, "lane", None))
            except MfError:
                pass
        return 3
    except MfError as e:
        print(f"[mf] error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
