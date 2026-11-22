"""Guard rules: turn folded task state into alerts for the user (dashboard) and the agent (every mf call).

Levels: crit (must stop or fix) > ask (needs the user) > warn > info.
"""

from __future__ import annotations

import subprocess
import time

from myflow.core import (LANG_ZH, MS_TERMINAL, PHASES, VALID_VERDICTS, audit_chain, file_hash, flatten, goalposts,
                         lang_rule)

LEVELS = ["crit", "ask", "warn", "info"]
PHASE_HOURS = {"spec": 4, "survey": 4, "plan": 3}
ORCHESTRATOR_ONLY = ("gate_unasked", "no_report")  # only the orchestrator talks to the user


def _age(sec: float) -> str:
    if sec < 90:
        return f"{int(sec)}s"
    if sec < 5400:
        return f"{int(sec / 60)}m"
    return f"{sec / 3600:.1f}h"


_EVIDENCE = {}  # (task dir, last event seq) -> (computed at, evidence): one scan per task per process / 30 s
WORKED_WT = ("_bootstrap", "_ms")  # mf-managed worktrees (_baseline, _verify-*, _against-*, _ms-check) see no agent work


def work_evidence(st, limit: int = 400):
    """Newest sign of work that does not depend on what the agent chose to record: commits and uncommitted
    changes in the worktrees agents work in, and documents agents write in the task dir (SPEC, notes, cards).

    Returns {"t", "where", "n"} (n = uncommitted files) or None. Runs git (lock-free), so callers only ask when
    the record alone looks bad (no mf calls / no progress for a while)."""
    key = (str(st.dir), st.events[-1].get("seq") if st.events else 0)
    hit = _EVIDENCE.get(key)
    if hit and time.time() - hit[0] < 30:
        return hit[1]
    best = None

    def see(t, where, n=0):
        nonlocal best
        if t and (best is None or t > best["t"]):
            best = {"t": t, "where": where, "n": n}

    from myflow import gitops
    root = st.dir / "wt"
    for wt in sorted(root.iterdir()) if root.is_dir() else []:
        if (wt.name.startswith("_") and wt.name not in WORKED_WT) or not (wt / ".git").exists():
            continue
        try:
            see(float(gitops.git(["log", "-1", "--format=%ct"], wt) or 0), f"wt/{wt.name}（提交）")
            # --no-optional-locks: never take index.lock from under an agent committing in this worktree
            out = subprocess.run(["git", "--no-optional-locks", "status", "--porcelain", "-z", "--untracked-files=normal"],
                                 cwd=str(wt), capture_output=True, text=True, timeout=20).stdout
        except Exception:  # a broken or hung worktree is no evidence either way
            continue
        files, items = [], iter(out.split("\0"))
        for e in items:
            if len(e) > 3:
                files.append(e[3:])
                if e[0] in "RC":  # renames / copies carry their source as the next entry
                    next(items, None)
        files = [f for f in files if not gitops.match(f + "x" if f.endswith("/") else f, gitops.DEFAULT_IGNORE)]  # "dir/" = untracked dir
        mt = []
        for f in files[:limit]:
            try:
                mt.append((wt / f).stat().st_mtime)
            except OSError:  # deleted files
                pass
        if mt:
            see(max(mt), f"wt/{wt.name}", len(files))
    docs = [p for p in st.dir.glob("*.md") if p.name != "STATUS.md"]  # STATUS.md is rendered by mf
    for sub in ("notes", "milestones"):
        if (st.dir / sub).is_dir():
            docs += [p for p in (st.dir / sub).rglob("*") if p.is_file()]
    for p in docs[:limit]:
        try:
            see(p.stat().st_mtime, str(p.relative_to(st.dir)))
        except OSError:
            pass
    _EVIDENCE[key] = (time.time(), best)
    return best


def alerts(st, now=None) -> list:
    now = now or time.time()
    t, b = st.task, st.task["budget"]
    out = []

    def add(level, code, msg):
        out.append({"level": level, "code": code, "msg": msg})

    if (st.dir / "events.jsonl").exists():
        broken = audit_chain(st.dir)
        if broken:
            add("crit", "tamper", f"事件日志哈希链断裂（{len(broken)} 处）：日志被手工修改过，数字不可信。运行 mf audit 查看")
    pi = PHASES.index(st.phase) if st.phase in PHASES else 0

    # -- alignment and gates (drift starts here)
    if pi > 0 and not st.gate_ok("spec", 1):
        add("crit", "spec_unconfirmed", f"需求未经用户确认就进入了 {st.phase} 阶段")
    g = st.gates.get("spec") or {}
    if g.get("state") == "approved" and g.get("spec_hash"):
        cur = file_hash(st.dir / "SPEC.md")
        if cur and cur != g["spec_hash"]:
            add("warn", "spec_changed", "SPEC.md 在用户确认后被修改：确认的内容与现在不一致")
    rule = lang_rule(t)
    wrong = [k for k in ("git", "doc") if rule and (t[k].get("lang") or "") != rule[1]]
    if wrong:
        add("warn", "lang_rule", f"规则：推送到 {rule[0]} 的文档、提交信息、PR 描述用{LANG_ZH.get(rule[1], rule[1])}，"
                                 f"但 {'、'.join(k + '.lang' for k in wrong)} 不是 {rule[1]}：mf set {st.id} "
                                 + " ".join(f"{k}.lang={rule[1]}" for k in wrong))
    dl = st.delegated(now)
    if dl:
        add("info", "delegated", f"用户授权至 {time.strftime('%m-%d %H:%M', time.localtime(dl['until']))}：需要确认的问题按默认值"
                                 f"（推荐项）决定，已代决 {len(st.default_decisions())} 项（看板“按默认值代你决定”）")
    for pg in st.pending_gates():
        wait = now - (pg.get("t") or now)
        if pg.get("asked_t"):
            how = f"（{_age(now - pg['asked_t'])} 前已经通过 {pg.get('asked_via') or 'brief'} 问过你）"
        else:
            how = f"（mf 已挂起 {_age(wait)}，agent 尚未转达给你）"
        add("ask", "gate", f"等待你确认【{pg['key']}】{how}：{pg.get('brief', '')[:160]}")
        if dl:  # the user is away and said: decide by the defaults
            add("crit" if wait > 1800 else "warn", "gate_unasked",
                f"【{pg['key']}】需要确认：用户授权期内由编排者按默认值决定（仅编排者处理）：mf decide {st.id} --gate {pg['key']} "
                "--default \"<选了哪个默认值、为什么>\"")
        elif not pg.get("asked_t"):  # opened by mf (push / protected tests): nobody has actually asked the user yet
            add("crit" if wait > 1800 else "warn", "gate_unasked",
                f"【{pg['key']}】需要用户确认，但编排者 {_age(wait)} 还没问用户（仅编排者处理）：现在就在对话里问用户，"
                f"不要攒到后面；问完运行 mf asked {st.id} {pg['key']}")
    for s in st.unacked():
        age = now - s["t"]
        lvl = "warn" if age > 600 else "info"
        add(lvl, "steer", f"你的留言 #{s['n']} 已 {_age(age)} 未被 agent 处理：{s['text'][:80]}")
    if st.stop:
        add("info", "stop", f"已请求停止（{st.stop['by']}）：{st.stop['reason']} → 应进入集成/报告")

    # -- contract integrity (anti-cheating)
    if st.freeze and not st.contract_ok:
        add("crit", "contract", "冻结后 task.json 的度量契约（命令/解析/环境/保护文件/基线）被改动，基线已失效：需重新 mf freeze")
    if st.freeze:
        expected = dict(st.freeze.get("goalposts") or {})
        for c in st.changes:
            if c["t"] >= st.freeze["t"]:
                for k, v in flatten(c["key"], c["new"]).items():
                    if k in expected or any(k.startswith(e + ".") for e in expected):
                        expected[k] = v
        current = goalposts(t)
        for key, old in expected.items():
            if key in current and current[key] != old:
                add("warn", "goalpost_edit", f"task.json 的 {key} 被直接修改（{old} → {current[key]}），未经 mf set 记录")
    for c in st.changes:
        if c["key"] in ("metric.target", "goal", "metric.min_improve") and st.freeze and c["t"] > st.freeze["t"]:
            lvl = "warn" if c["by"] != "user" else "info"
            add(lvl, "goalpost", f"目标被修改（{c['by']}）：{c['key']} {c['old']} → {c['new']}  {c['why']}")
    bad = [a for a in st.attempts if a.get("status") in ("oracle_modified", "scope_violation")]
    if bad:
        last = bad[-1]
        live = any((st.lanes.get(a.get("lane")) or {}).get("status") == "active" for a in bad)
        add("warn" if live else "info", "oracle",
            f"{len(bad)} 次尝试改动了受保护/范围外文件（最近 A{last.get('n')} [{last.get('lane')}]：{last.get('status')}）")

    # -- budget and stall (anti-stuck)
    used, mx = len(st.attempts), int(b.get("max_attempts") or 0)
    if mx:
        if used >= mx:
            add("crit", "budget", f"尝试次数预算用尽 {used}/{mx}：停止新尝试，进入集成/报告")
        elif used >= 0.8 * mx:
            add("warn", "budget", f"尝试次数已用 {used}/{mx}")
    mh = float(b.get("max_hours") or 0)
    if mh and st.freeze:
        if st.hours >= mh:
            add("crit", "time", f"时间预算用尽 {st.hours:.1f}h/{mh:g}h")
        elif st.hours >= 0.8 * mh:
            add("warn", "time", f"时间已用 {st.hours:.1f}h/{mh:g}h")
    mni = int(b.get("max_no_improve") or 0)
    if mni and st.all_stalled:
        add("crit", "stall", f"所有活跃 lane 都已连续 ≥{mni} 次完整尝试没有 ≥{t['metric']['min_improve']:.0%} 的提升："
                             "停止、彻底换方向或延长预算（需你决定）")
    for ln in st.lanes.values():
        if ln["status"] != "active":
            continue
        if mni and ln["stalled"] and not st.all_stalled:
            add("warn", "stall", f"lane {ln['name']} 连续 {ln['no_improve']} 次无提升（上限 {mni}）：该线 try 已被拒绝，换方向或停掉")
        elif mni and ln["no_improve"] >= max(3, int(0.6 * mni)):
            add("warn", "stall", f"lane {ln['name']} 连续 {ln['no_improve']}/{mni} 次无提升：当前方向可能到顶")
        if ln.get("fail_streak", 0) >= int(b.get("max_fail_streak") or 4):
            add("warn", "fail_streak", f"lane {ln['name']} 连续失败 {ln['fail_streak']} 次：回到最佳版本、换思路")
        tos = [a for a in ln["attempts"][-2:] if a.get("status") == "timeout"]
        if len(tos) == 2:
            add("warn", "timeouts", f"lane {ln['name']} 连续 2 次超时（可能死循环/hang）")
    ph = PHASE_HOURS.get(st.phase)
    if ph and st.phase_hist and not st.done():
        t0 = st.phase_hist[-1]["t"]
        if now - t0 > ph * 3600:  # pace only; whether it is spinning is decided below from evidence
            moved = [x for x in st.progress_ts if x > t0]
            tail = f"期间 {len(moved)} 次进展，最近 {_age(now - moved[-1])} 前" if moved else "期间没有进展记录"
            add("info", "phase_long", f"已在 {st.phase} 阶段 {_age(now - t0)}（建议 ≤{ph}h），{tail}")
    # -- reporting: recorded progress the user has not been told about (mf only sees task-level mf brief)
    unreported = [x for x in st.progress_ts if x > (st.last_report_t or 0)]
    rm = float(b.get("report_min") or 60) * 60
    if unreported and now - unreported[0] > rm and not st.done():
        last = f"上次汇报在 {_age(now - st.last_report_t)} 前" if st.last_report_t else "还从没汇报过"
        add("warn", "no_report", f"有 {len(unreported)} 次进展没向你汇报（最早一次已过 {_age(now - unreported[0])}，{last}）："
                                 f"不是空转，是没汇报（仅编排者处理）：在对话里告诉用户，并 mf brief {st.id} "
                                 f"\"做完了什么 / 下一步 / 风险\"")

    # -- is it moving? recorded progress first, then real file changes; never the agent's own prose
    hb = float(b.get("heartbeat_min") or 30) * 60
    spin = float(b.get("spin_min") or 120) * 60
    if not st.done():
        idle = now - st.last_agent_t
        waiting = False  # a command still inside its timeout window explains the silence
        for rid, r in st.runs_open.items():
            lim = (r.get("timeout") or 0) + 300
            if r.get("timeout") and now - r["t"] > lim:
                add("warn", "run_lost", f"运行 {rid} 已 {_age(now - r['t'])} 无结束记录（mf 进程可能被强杀）")
            else:
                waiting = True
        asked = sorted((g for g in st.pending_gates() if g.get("asked_t")), key=lambda g: g["asked_t"])
        quiet = now - (st.progress_ts[-1] if st.progress_ts else st.created_t)
        ev = work_evidence(st) if not waiting and (idle > hb or quiet > spin) else None
        seen = f"{ev['where']} {_age(now - ev['t'])} 前" if ev else ""
        files = f"（{ev['n']} 个未提交文件）" if ev and ev["n"] else ""
        if waiting:
            pass
        elif asked and idle > hb:
            g = asked[0]
            add("info", "await_user", f"agent 在等你回复【{g['key']}】（{_age(now - g['asked_t'])} 前问的），最后一次活动在 "
                                      f"{_age(idle)} 前；如果你已经回复过，说明 agent 没记录你的答复或已经停止")
        elif idle > hb and ev and now - ev["t"] < hb:
            add("warn", "no_record", f"agent {_age(idle)} 没调用 mf，但 {seen}还有改动{files}：在干活，只是没记录"
                                     f"（mf ping 说明在做什么，做成一步就 mf note --kind finding）")
        elif idle > hb:
            also = f"；最近的文件改动在 {seen}" if ev else "；也没有文件改动"
            add("crit" if idle > 3 * hb else "warn", "stale",
                f"agent {_age(idle)} 无任何活动（没有 mf 调用、没有进展记录{also}）：可能已卡死或退出")
        elif quiet > spin and ev and now - ev["t"] < spin:
            add("info", "no_record", f"已 {_age(quiet)} 没有进展记录，但 {seen}还有改动{files}：在干活，阶段性结果还没记录")
        elif quiet > spin:
            also = f"最近的文件改动在 {seen}" if ev else "也找不到任何文件改动"
            add("warn", "spin", f"agent 还在调用 mf（最近 {_age(idle)} 前），但已 {_age(quiet)} 没有任何进展记录，{also}："
                                f"可能在原地打转（同一件事反复试？卡在某个问题上？）")

    # -- honesty of numbers (incremental verifies compare two milestones; the lane's claim is vs the frozen base)
    noise_floor = 0.05
    cum_v = [v for v in st.verifies if not v.get("against")]
    for v in cum_v:
        if v.get("verdict") in VALID_VERDICTS and v.get("claim") and v.get("speedup"):
            tol = max(noise_floor, 3 * (v.get("noise") or 0))
            if v["claim"] > v["speedup"] * (1 + tol):
                add("warn", "overclaim", f"A{v.get('attempt')} [{v.get('lane')}] 自测 {v['claim']:.3f}× 未复现：复核 {v['speedup']:.3f}×")
        if v.get("regressed"):
            add("warn", "case_regress", f"A{v.get('attempt')} 复核：{len(v['regressed'])} 个 case 变慢超阈值（{', '.join(v['regressed'][:3])}…）")
    counts = {}
    for v in st.verifies:  # per (code, what it was compared against): re-running until one passes is shopping
        key = (v.get("commit") or v.get("attempt"), v.get("against_ms") or v.get("against"))
        counts.setdefault(key, []).append((v.get("attempt"), v.get("verdict")))
    for (_, vs), runs in counts.items():
        if len(runs) >= 3:
            what = f"（对 {vs}）" if vs else ""
            ns = "/".join(sorted({f"A{n}" for n, _ in runs}))
            add("warn", "verify_shopping", f"{ns}{what} 同一份代码被复核 {len(runs)} 次（{'/'.join(str(x) for _, x in runs)}）：警惕挑结果")
    bs = st.best_self
    if bs:
        verified = {v.get("attempt") for v in cum_v}
        later = sum(1 for a in st.attempts if a["seq"] > bs["seq"])
        bv = st.best_verified["speedup"] if st.best_verified else 1.0
        if bs.get("n") not in verified and bs["speedup"] > bv * 1.03 and later >= 3:
            add("info", "unverified", f"最佳自测 A{bs.get('n')} ({bs['speedup']:.3f}×) 尚未复核（之后已有 {later} 次尝试）")

    # -- reachability
    if st.ceiling and st.ceiling.get("value") and st.ceiling["value"] < st.target:
        add("ask", "ceiling", f"目标 {st.target:g}× 高于上限估计 {st.ceiling['value']:g}×（{st.ceiling['basis'][:80]}）：要调整目标或接受部分达成吗？")
    if st.best_verified and st.best_verified["speedup"] >= st.target and st.phase in ("implement", "verify"):
        add("info", "target_met", f"已复核达到目标 {st.best_verified['speedup']:.3f}× ≥ {st.target:g}×：可进入集成/报告")
    if st.corrupt:
        add("crit", "corrupt", f"events.jsonl 有 {st.corrupt} 行无法解析")

    # -- milestones: is the work going where it should?
    for d in direction(st, now):
        if d["level"] in ("warn", "ask") or (d["level"] == "info" and d["msg"]):
            add(d["level"], f"ms_{d['code']}", f"[方向·{d['name']}] {d['msg']}")
    out.sort(key=lambda a: LEVELS.index(a["level"]))
    return out


def direction(st, now=None) -> list:
    """Milestone-level direction signals (docs/DESIGN-v2-milestones.md §8.4); [] when there are no milestones.

    Each item: {code, name, level: ok|info|warn|ask, msg}. Computed only from events, never from agent prose."""
    if not st.milestones:
        return []
    now = now or time.time()
    t = st.task
    mi = float(t["metric"].get("min_improve", 0.02))
    closed, om = st.closed_ms, st.open_ms
    out = []

    def sig(code, name, level, msg=""):
        out.append({"code": code, "name": name, "level": level, "msg": msg})

    recent = closed[-2:]
    if len(recent) == 2 and not any(m.get("goals") for m in recent):
        sig("goal", "目标对齐", "warn", f"最近两个里程碑（{recent[0]['id']}、{recent[1]['id']}）都没写服务于哪个目标（ms plan --goal）")
    else:
        sig("goal", "目标对齐", "ok")

    broken, changed = [], []
    for prev, cur in zip(closed, closed[1:]):
        promised = (prev["close"].get("next") or [None])[0]
        if promised and promised not in MS_TERMINAL and promised != cur["id"]:
            why = cur["close"].get("why") or cur.get("why")
            (changed if why else broken).append((prev["id"], promised, cur["id"], why))
    nxt = st.next_ms()
    if broken:
        p, promised, done, _ = broken[-1]
        sig("promise", "承诺兑现", "warn", f"{p} 推送时承诺下一步做 {promised}，实际完成的是 {done}，且没写原因（ms close --why）")
    elif isinstance(nxt, dict) and nxt.get("status") == "dropped":
        sig("promise", "承诺兑现", "info", f"{st.last_ms['id']} 承诺的 {nxt['id']} 已放弃：{(nxt.get('drop') or {}).get('reason', '')}")
    elif changed:
        p, promised, done, why = changed[-1]
        sig("promise", "承诺兑现", "info", f"{p} 承诺 {promised}，实际做了 {done}：{why}")
    else:
        sig("promise", "承诺兑现", "ok")

    ratios = []
    for m in closed:
        inc = (m.get("prep") or {}).get("inc")
        if m.get("type") == "perf" and inc and (m.get("expect") or 0) > 1:
            ratios.append((m["id"], (inc["speedup"] - 1) / (m["expect"] - 1)))
    if len(ratios) >= 2 and all(r < 0.5 for _, r in ratios[-2:]):
        sig("predict", "预测偏差", "warn", f"{ratios[-2][0]}、{ratios[-1][0]} 实测收益不到预期的一半（{ratios[-2][1]:.0%}、{ratios[-1][1]:.0%}）："
                                       "对瓶颈的判断可能错了，先做一个分析里程碑")
    else:
        sig("predict", "预测偏差", "ok", f"最近 实测/预期 {ratios[-1][1]:.0%}" if ratios else "")

    mx = int(t["milestone"].get("max_lines") or 0)
    lines = ((om or {}).get("prep", {}).get("size") or {}).get("lines", 0)
    if om and mx and lines > mx:
        why = om["prep"].get("size_why")
        sig("size", "规模", "info" if why else "warn", f"{om['id']} 改动 {lines} 行 > {mx}" + (f"（原因：{why}）" if why else "：该拆了"))
    else:
        sig("size", "规模", "ok")

    # quality = the entry document stays readable (user request 11-22: easy to understand, not long)
    entry = t["doc"].get("entry")
    doc = st.dir / "wt" / "_ms" / entry if entry else None
    if doc and doc.is_file():
        from myflow import ms
        n, cap = ms.doc_core_lines(doc.read_text(encoding="utf-8", errors="replace")), ms.doc_budget(st)
        if n > cap:
            sig("quality", "质量", "warn", f"入口文档 {n} 行（不含 mf 生成块）> 预算 {cap}：旧里程碑小节收成一行摘要 + 提交链接，"
                                       "细节移到深度参考")
        else:
            sig("quality", "质量", "ok", f"入口文档 {n}/{cap} 行")
    else:
        sig("quality", "质量", "ok")

    perf = sorted((m for m in st.milestones.values() if m.get("type") == "perf" and m["status"] in ("done", "dropped")),
                  key=lambda m: (m.get("close") or m.get("drop") or {}).get("t", 0))

    def marginal(m):
        inc = (m.get("prep") or {}).get("inc")
        if m["status"] == "dropped" or not inc:
            return True
        return inc["speedup"] < 1 + 2 * max(mi, 2 * (inc.get("noise") or 0))

    best = max(((m.get("prep") or {}).get("cum") or {}).get("speedup") or 0 for m in closed) if closed else 0
    if len(perf) >= 2 and all(marginal(m) for m in perf[-2:]) and best < st.target:
        sig("plateau", "收益曲线", "ask", f"最近两个性能里程碑（{perf[-2]['id']}、{perf[-1]['id']}）收益都很小或被放弃，"
                                       f"累计 {best:.3f}× 未达目标 {st.target:g}×：到顶了吗？换方向、调目标还是停？")
    else:
        sig("plateau", "收益曲线", "ok")

    cur = om or (nxt if isinstance(nxt, dict) and nxt.get("status") in ("planned", "closing") else None)
    if cur and cur.get("est_h"):
        start = max(cur.get("t_plan") or 0, st.last_ms["close"]["t"] if st.last_ms else 0)
        spent = (now - start) / 3600
        if spent > 2 * float(cur["est_h"]):
            sig("slow", "时间", "warn", f"{cur['id']} 已用 {spent:.1f}h，预计 {cur['est_h']:g}h（超过 2 倍）：是否在原地打转？")
        else:
            sig("slow", "时间", "ok")

    if (t["git"].get("url") or "").strip():
        unp = [m["id"] for m in closed if not m.get("pushed")]
        if unp:
            waiting = any(k.startswith("push:") and g.get("state") == "pending" for k, g in st.gates.items())
            sig("unpushed", "未推送", "info" if waiting else "warn",
                f"{', '.join(unp)} 已收尾未推送" + ("：等你确认推送目标" if waiting else f"：mf ms push {st.id} 重试"))
        else:
            sig("unpushed", "未推送", "ok")
    return out


def health(al: list) -> str:
    levels = {a["level"] for a in al}
    if "crit" in levels:
        return "crit"
    if "warn" in levels:
        return "warn"
    return "ok"


def needs_you(al: list) -> list:
    return [a for a in al if a["level"] == "ask" or a["code"] in ("stall", "spec_unconfirmed", "tamper", "contract", "stale")
            and a["level"] == "crit"]


def refusal(st, kind: str = "try", lane=None):
    """Reason to refuse starting new work, or None. Agents see this as exit code 3."""
    b = st.task["budget"]
    if kind == "try" and st.stop:
        return f"已请求停止：{st.stop['reason']}（进入集成/报告）"
    if not st.freeze:
        return "尚未冻结基线：先 mf freeze"
    if not st.contract_ok:
        return "冻结后契约被改动：先 mf freeze 重新冻结"
    if not st.gate_ok("spec", 1):
        return "需求尚未经用户确认（spec gate）"
    if kind == "try":
        if b.get("max_attempts") and len(st.attempts) >= int(b["max_attempts"]):
            return f"尝试次数预算用尽 {len(st.attempts)}/{b['max_attempts']}"
        if b.get("max_hours") and st.hours >= float(b["max_hours"]):
            return f"时间预算用尽 {st.hours:.1f}h"
        ln = st.lanes.get(lane) if lane else None
        if ln and ln.get("stalled"):
            return (f"lane {lane} 连续 {ln['no_improve']} 次无提升（stall）：换方向（新 lane）或停掉本线；"
                    "继续本线需用户同意后 mf budget --no-improve +N")
    return None
