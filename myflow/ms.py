"""Milestones (v2 phase 1): plan card -> prep (candidate, incremental ABBA, integration worktree) -> three
independent roles (tests / writer / cold-read verifier) -> close (final commit, correctness on it, tag, push).

Design: docs/DESIGN-v2-milestones.md. Invariants enforced here:
- the pushed commit is the accepted one: its runtime tree (everything except doc and protected-test files, as
  defined by the globs pinned at prep) equals the candidate's, whose incremental ABBA vs the previous milestone was
  measured at prep under the current freeze; correctness re-runs on that exact commit before any ref moves;
- each role may change only its own files, every role record leaves a checkpoint tree (who changed what is
  provable), three roles = three sessions; changes to protected tests need the user's approval;
- pushes go only to the URL + branch the user approved (the gate key hashes them): fast-forward only, atomic, and
  only the recorded milestone commit is published.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
import urllib.parse
from pathlib import Path, PurePosixPath

from myflow import cli, guard, render
from myflow.core import (LANG_ZH, MS_TERMINAL, PASS, PKG_ROOT, VALID_VERDICTS, MfError, MfRefused, State, append_event,
                         atomic_write, cjk_share, file_hash, gate_key, lang_rule, sha16, task_dir_of)
from myflow.gitops import (DEFAULT_IGNORE, commit_tree, delete_tag, ensure_worktree, git, github_web, is_ancestor,
                           ls_tree, make_tag, match, merge, numstat, push_atomic, ref_sha, remote_head, remote_tag,
                           reset_detached, rev_parse, runtime_tree_hash, snapshot, stage_worktree, tree_diff_names,
                           uncommitted_files, update_branch, valid_branch, write_tree)

MS_RE = re.compile(r"M\d{1,3}[a-z]?$")
TYPES = ("feature", "perf", "analysis", "maint")
TYPE_ZH = {"feature": "功能", "perf": "性能", "analysis": "分析", "maint": "维护"}
ROLE_ZH = {"review": "复核", "tests": "测试负责人", "writer": "文档作者", "verifier": "冷读验证者"}  # the last three: history only
# the entry document is a design document a tech lead reads in 10 minutes (user request 11-22): its budget without
# mf's generated blocks, one thing per line (link targets not counted), and no process traces in it
DOC_MAX_LINES = 150
DOC_MAX_LINE_CHARS = 120
DOC_TRACES = (("runs/ 日志路径", r"\bruns/"), ("notes/ 笔记路径", r"\bnotes/"),
              ("尝试 / 复核编号（A7、V3）", r"(?<![A-Za-z0-9_/.#-])[AV]\d{1,3}(?![A-Za-z0-9_.])"),
              ("history 文档", r"\bhistory\.md\b"), ("“用户 MM-DD”式的过程记录", r"用户 ?\d{1,2}-\d{1,2}"))
DOC_EXT = (".md", ".rst")  # only formats that never execute; .txt can be requirements.txt / CMakeLists.txt
CARD_MAX_LINES = 60
PLACEHOLDER = "«"  # card skeleton placeholders; the writer must replace all of them
CJK_MIN = 0.15  # below this share of CJK letters (code and `identifiers` excluded) a text is not written in Chinese


# ---------------------------------------------------------------- paths, names, pins


def int_branch(st: State) -> str:
    return f"mf/{st.id}/_ms"  # lane names cannot start with "_", so no lane worktree can ever check this out


def remote_branch(st: State) -> str:
    return st.task["git"].get("branch") or f"mf/{st.id}"


def tag_name(st: State, mid: str) -> str:
    return f"{st.id}-{mid}"


def doc_paths(st: State) -> list:
    d = st.task["doc"]
    paths = list(d.get("paths") or [])
    if d.get("entry") and not match(d["entry"], paths):
        paths.append(d["entry"])
    return paths


def tests_paths(st: State) -> list:
    return list(st.task["tests"].get("paths") or [])


def pins_now(st: State) -> dict:
    """What decides the runtime tree and the review's files. Pinned at prep; role and close use the pinned copy."""
    t = st.task
    return {"doc": doc_paths(st), "tests": tests_paths(st), "protect": list(t["scope"]["protect"] or []),
            "bench": " ".join(t["commands"].get(k) or "" for k in ("bench", "bench_quick"))}


def doc_budget(st: State) -> int:
    return int(st.task["doc"].get("max_lines") or DOC_MAX_LINES)


def _reader_text(text: str) -> str:
    """An entry document as its reader sees it: without mf's generated blocks and without HTML comments (the template's
    writing hints are comments; GitHub does not show them)."""
    text = GEN_BLOCK.sub(r"\1\3", text)
    comment = r"<!--(?:(?!-->).)*-->"  # never runs past its own end
    text = re.sub(r"^[ \t]*(?:%s[ \t]*)+(?:\n|$)" % comment, "", text, flags=re.S | re.M)
    return re.sub(comment, "", text, flags=re.S)


def doc_core_lines(text: str) -> int:
    """Lines of an entry document that people have to read, without mf's generated blocks."""
    return len(_reader_text(text).splitlines())


def doc_problems(st: State, text: str) -> list:
    """What makes the entry document longer or harder to read than a design document should be: over the line budget,
    lines that say more than one thing, and process traces (they belong in commit messages and mf's report)."""
    core = _reader_text(text)
    lines, cap = core.splitlines(), doc_budget(st)
    width = int(st.task["doc"].get("max_line_chars") or DOC_MAX_LINE_CHARS)
    out = [f"{len(lines)} 行（不含 mf 生成块）> 预算 {cap}"] if len(lines) > cap else []
    seen = [len(re.sub(r"\]\([^)]*\)", "]", ln)) for ln in lines]  # a link's target is not read
    long_ = [n for n in seen if n > width]
    if long_:
        out.append(f"{len(long_)} 行超过 {width} 字（最长 {max(long_)}；一行只说一件事，链接地址不计）")
    traces = [f"{label} ×{n}" for label, pat in DOC_TRACES for n in [len(re.findall(pat, core))] if n]
    if traces:
        out.append("过程痕迹：" + "、".join(traces) + "（放提交信息或 mf 报告，设计文档只写当前设计）")
    return out


def _bench_ref(path: str, pins: dict) -> bool:
    return bool(pins["bench"].strip()) and (path in pins["bench"] or Path(path).name in pins["bench"])


def is_doc(path: str, pins: dict) -> bool:
    """Documentation never executes: doc globs AND .md/.rst (so `doc.paths=["**"]` is harmless)."""
    return match(path, pins["doc"]) and path.lower().endswith(DOC_EXT)


def is_test(path: str, pins: dict) -> bool:
    """Protected tests only (lanes cannot touch them; the tests role needs the user's approval); never the bench."""
    return match(path, pins["tests"]) and match(path, pins["protect"]) and not _bench_ref(path, pins)


def non_runtime(pins: dict):
    return lambda p: is_doc(p, pins) or is_test(p, pins)


def ms_dir(st: State, mid: str) -> Path:
    p = st.dir / "milestones" / mid
    p.mkdir(parents=True, exist_ok=True)
    return p


def _repo(st: State) -> Path:
    return Path(st.task["repo"])


def _ignore(st: State) -> list:
    return DEFAULT_IGNORE + list(st.task["scope"].get("ignore") or [])


def _sign_cfg(st: State) -> tuple:
    return () if st.task["git"].get("sign") else ("-c", "commit.gpgsign=false", "-c", "tag.gpgSign=false")


def _pipeline_base(st: State, candidate: str, parent: str, what: str):
    """None when `candidate` already builds on the last milestone; else the previous milestone whose *candidate* it grew
    from while that milestone was being closed (pipelining, user request 11-22). Refuses anything else."""
    repo = _repo(st)
    if is_ancestor(repo, parent, candidate):
        return None
    last = st.closed_ms[-1] if st.closed_ms else None
    pc = ((last or {}).get("prep") or {}).get("candidate")
    if last and pc and is_ancestor(repo, pc, candidate):
        return last
    raise MfRefused(f"{what} 既不是从 {prev_label(st)}（{parent[:10]}）出发的，也不是从它收尾时的候选出发的：先叠加到当前 tip，例如 "
                    f"`mf lane new {st.id} <名字> --from {st.last_ms['id'] if st.last_ms else 'base'} --merge <lane>`")


def _merge_close(st: State, candidate: str, parent: str, last: dict, mid: str) -> str:
    """Bring the previous milestone's closing changes (documents and tests only) into a candidate that grew from that
    milestone's prep candidate, and check that the code is unchanged, so measurements of the candidate stay valid.

    A close commit is a squash onto the milestone before it, so a plain merge would use an older merge base and see
    the code change twice. The closing changes are therefore merged as a delta on top of the prep candidate, and the
    result is recorded with the close commit as second parent."""
    repo = _repo(st)
    pc = last["prep"]["candidate"]
    wt = st.dir / "wt" / "_merge"
    ensure_worktree(repo, wt, candidate)
    reset_detached(wt, candidate, "fd")
    close_tree = git(["rev-parse", f"{parent}^{{tree}}"], repo)
    delta = commit_tree(wt, close_tree, pc, f"mf {st.id}: {last['id']} 收尾时的文档 / 用例")
    try:
        merge(wt, delta, f"mf {st.id}: 合并 {last['id']} 的收尾提交（文档 / 用例）")
    except MfError as e:
        try:
            git(["merge", "--abort"], wt)
        except MfError:
            pass
        raise MfRefused(f"自动合并 {last['id']} 的收尾提交时冲突（下一个里程碑和复核改了同一个文档 / 用例文件）：在实现者的 worktree 里 "
                        f"合并 {parent[:12]} 的文档 / 用例改动并提交，再 prep。\n{str(e)[:600]}")
    merged = commit_tree(wt, git(["rev-parse", "HEAD^{tree}"], wt), candidate,
                         f"mf {st.id}: 合并 {last['id']} 的收尾提交（文档 / 用例）到 {mid} 的候选", also=[parent])
    pins = pins_now(st)
    if runtime_tree_hash(repo, merged, non_runtime(pins)) != runtime_tree_hash(repo, candidate, non_runtime(pins)):
        raise MfRefused(f"合并 {last['id']} 的收尾提交后运行时代码变了（收尾只应改文档 / 用例）：先查明再 prep")
    print(f"[mf] 流水线：候选是在 {last['id']} 收尾期间开始的，已自动合并 {last['id']} 的收尾提交（只多了文档 / 用例）→ {merged[:10]}",
          flush=True)
    return merged


def _check_id(mid: str) -> None:
    if not MS_RE.match(mid or ""):
        raise MfError("milestone id: M<number>[letter], e.g. M3 or M5a")


def _check_entry(entry: str) -> None:
    p = PurePosixPath(entry)
    if p.is_absolute() or ".." in p.parts or not entry.lower().endswith(DOC_EXT):
        raise MfError(f"doc.entry={entry!r}: must be a relative path inside the repo to a .md/.rst file")


def _get(st: State, mid: str) -> dict:
    _check_id(mid)
    m = st.milestones.get(mid)
    if not m:
        raise MfError(f"unknown milestone {mid}: plan it first (mf ms plan {st.id} {mid} --title ...)")
    return m


def parent_commit(st: State) -> str:
    if st.last_ms_commit:
        return st.last_ms_commit
    if st.freeze:
        return st.freeze["base"]
    if st.bootstrap:
        return st.bootstrap["start"]
    raise MfError(f"nothing to build on: mf freeze {st.id} first, or mf ms bootstrap {st.id} for a new operator")


def prev_label(st: State) -> str:
    if st.last_ms:
        return st.last_ms["id"]
    return "冻结基线" if st.freeze else "bootstrap 起点"


def plan_line(m: dict) -> str:
    s = f"{m['id']}[{TYPE_ZH.get(m.get('type'), m.get('type'))}] {m.get('title', '')}"
    if m.get("expect"):
        s += f" · 预期 {m['expect']:g}×"
    if m.get("basis"):
        s += f"（依据：{m['basis']}）"
    if m.get("accept"):
        s += f" · 验收：{m['accept']}"
    if m.get("est_h"):
        s += f" · 约 {m['est_h']:g}h"
    if m.get("commitment") == "tentative":
        s += " · 暂定"
    return s


def _pick_verify(st: State, attempt, against=None):
    """The verify to use for (attempt's commit, comparison), from the current freeze. Disagreeing repeats = shopping;
    keyed on the commit, so re-running `mf try` on the same code does not reset the history. `attempt` may be an
    attempt number or a commit (an integration lane's head, which needs no `mf try`)."""
    at = {"commit": attempt} if isinstance(attempt, str) else (st.attempt(attempt) or {})
    vs = [v for v in st.verifies if v.get("commit") == at.get("commit") and v.get("verdict") in VALID_VERDICTS
          and v.get("speedup") and (v.get("against") or None) == against and st.same_epoch(v)]
    if not vs:
        return None, None
    verdicts = [v["verdict"] for v in vs]
    if len(set(verdicts)) > 1:
        return vs[-1], (f"提交 {str(at.get('commit'))[:10]} 的 {len(vs)} 次{'增量' if against else ''}复核结论不一致"
                        f"（{'/'.join(verdicts)}）：不能挑结果。交给用户决定；用户认可时加 --verify-why \"原因\"（会写进卡片）")
    return vs[-1], None


def _vsum(v) -> dict:
    if not v:
        return None
    return {"v": v.get("n"), "speedup": v.get("speedup"), "verdict": v.get("verdict"), "noise": v.get("noise"),
            "pairs": v.get("pairs"), "gpu": v.get("gpu"), "regressed": v.get("regressed") or [],
            "against_ms": v.get("against_ms")}


def _size(st: State, parent: str, candidate: str, pins: dict) -> dict:
    excluded = non_runtime(pins)
    rows = [r for r in numstat(_repo(st), parent, candidate) if not excluded(r[2])]
    return {"lines": sum(r[0] + r[1] for r in rows), "files": len(rows), "names": [r[2] for r in rows][:20]}


def check_key(st: State, treeish: str) -> str:
    """What decides a correctness result: code + tests (not documents) and the commands and environment that run them.
    User request 11-22: the same code and tests are checked once (only runs by mf itself in a clean worktree count)."""
    pins, t = pins_now(st), st.task
    how = "|".join(str(t["commands"].get(k) or "") for k in ("setup", "correctness", "verify_extra"))
    how += "|" + json.dumps(t.get("env") or {}, sort_keys=True)
    return sha16(runtime_tree_hash(_repo(st), treeish, lambda f: is_doc(f, pins)) + "|" + how)


def reuse_check(st: State, treeish: str):
    hit = st.checks.get(check_key(st, treeish))
    if hit:
        print(f"[mf] 功能用例：同一份代码与用例已在 {hit.get('what')}（{str(hit.get('commit', ''))[:10]}）通过，复用 {hit.get('log')}",
              flush=True)
    return hit


def record_check(st: State, commit: str, what: str, log) -> None:
    append_event(st.dir, "check", f"{what}: {commit[:10]}", by="mf", key=check_key(st, commit), commit=commit, what=what,
                 log=str(log))


def _correctness_at(st: State, commit: str, what: str) -> dict:
    """Frozen correctness (+ verify_extra) on exactly `commit`, in a clean worktree; reused when the same code and
    tests already passed that way."""
    hit = reuse_check(st, commit)
    if hit:
        return {"rc": 0, "log": hit.get("log"), "reused": hit.get("what")}
    repo, t = _repo(st), st.task
    wt = st.dir / "wt" / "_ms-check"
    ensure_worktree(repo, wt, commit)
    reset_detached(wt, commit, "fd")
    run_dir = cli._run_dir(st, "_ms", what)
    ctx = cli._ctx(st, "_ms-check", wt, run_dir)
    timeout = cli._timeout(st)
    append_event(st.dir, "run.start", f"ms {what}: correctness on {commit[:10]}", by="mf", run=run_dir.name,
                 timeout=timeout * 3)
    try:
        if t["commands"].get("setup"):
            r = cli.run_cmd(cli.expand(t["commands"]["setup"], ctx), wt, cli.build_env(t, ctx), run_dir / "setup" / "out.log",
                            timeout)
            if r["rc"] != 0:
                return r
        res = cli._correctness(st, ctx, wt, run_dir / "correctness", timeout)
        if res["rc"] == 0 and t["commands"].get("verify_extra"):
            extra = cli._correctness(st, ctx, wt, run_dir / "verify_extra", timeout, key="verify_extra")
            if extra["rc"] != 0:
                return extra
        if res["rc"] == 0:
            record_check(st, commit, f"{what} {commit[:10]}", res["log"])
        return res
    finally:
        append_event(st.dir, "run.end", "", by="mf", run=run_dir.name)


TEST_PRINCIPLE = ("用例分三类：基本功能测试（简短、数量少，覆盖主路径，每次改动先跑）、性能测试（bench，每个分档至少一个 case）、"
                  "其他覆盖测试（只测前两类覆盖不了的，写明原因）；前两类能覆盖的不再加，但不缺失覆盖")


def _oracle_change(st: State, m: dict, tree: str):
    """Protected files (tests, bench) that existed before this milestone and were changed or deleted in it,
    the key binding their content, and a one-line summary.

    User rule 11-22: such changes need no approval; the closing role states why they keep the test principle
    (--tests-why, published in the commit message) and only a violation goes to the user. New files need nothing."""
    repo, pins, parent = _repo(st), m["prep"]["pins"], m["prep"]["parent"]
    files = [f for f in tree_diff_names(repo, parent, tree) if match(f, pins["protect"])]
    if files:
        before = {p for p, _ in ls_tree(repo, parent)}
        files = [f for f in files if f in before]
    if not files:
        return [], None, ""
    blobs = dict(ls_tree(repo, tree))
    key = f"tests@{m['id']}:" + sha16(",".join(f"{f}={blobs.get(f, 'deleted')}" for f in sorted(files)))[:8]
    stat = {r[2]: r for r in numstat(repo, parent, tree)}
    summary = ", ".join(f"{f} +{stat[f][0]}/-{stat[f][1]}" if f in stat else f"{f}（删除）" for f in files[:8])
    return files, key, summary


def _tests_attested(m: dict, key) -> dict:
    """The latest role record that explained exactly this content of the changed pre-existing tests."""
    recs = [r for r in (m.get("roles") or {}).values() if r and r.get("tests_key") == key]
    return max(recs, key=lambda r: r["t"]) if key and recs else {}


# ---------------------------------------------------------------- texts


def _zh(st: State) -> bool:
    """Language of what gets committed and pushed (card, mf's lines in the commit message): task.json git.lang."""
    return (st.task["git"].get("lang") or "en") == "zh"


VERDICT_ZH = {"improved": "提升", "no_gain": "无提升", "regression": "变慢"}
COMMIT_ZH = {"committed": "承诺", "tentative": "暂定"}


def _card_skeleton(st: State, m: dict, prep: dict) -> str:
    """The commit card answers the user's three questions: what was done, did it deviate, what is covered."""
    zh = _zh(st)
    L = {"what": "做了什么", "why": "为什么", "dev": "与计划的差异", "cov": "覆盖", "known": "已知问题",
         "perf": "性能（mf 生成）", "next": "下一步"} if zh else \
        {"what": "What", "why": "Why", "dev": "Deviation from plan", "cov": "Coverage", "known": "Known issues",
         "perf": "Perf (by mf)", "next": "Next"}
    P = ({"what": "«≤3 行：改了什么，用评审能核对的说法»",
          "why": "«≤3 行：需求或瓶颈 → 改法 → 为何正确 / 为何更快；数值是否逐位不变，否则给误差界»",
          "dev": "«无 | 计划是什么 → 实际怎么做 → 原因（含与上游 / 参照实现的差异）»",
          "cov": "«本里程碑新增 / 改动的逻辑由哪些用例覆盖（基本功能 / 性能 / 其他）；全表见入口文档的测试覆盖一节»",
          "known": "«无 | 一行一条»",
          "next": "（收尾时由 mf 按计划卡生成，这里写的会被替换）"} if zh else
         {"what": "«≤3 lines: what changed, in terms a reviewer can check»",
          "why": "«≤3 lines: need or bottleneck → change → why correct/faster; numerics bit-exact or the error bound»",
          "dev": "«none | planned → done instead → why (including differences from upstream/reference)»",
          "cov": "«which tests cover the logic added/changed here (basic / performance / other); the full table is in the entry doc»",
          "known": "«none | one per line»",
          "next": "(generated by mf at close from the plan cards; anything written here is replaced)"})
    perf = perf_line(st, prep) or ("无性能主张" if zh else "no performance claim")
    kind = TYPE_ZH.get(m.get("type"), m.get("type")) if zh else m.get("type")
    return "\n".join([f"### {m['id']} · {m.get('title', '')} · {kind}"]
                     + [f"**{L[k]}**: {P[k]}" for k in ("what", "why", "dev", "cov", "known")]
                     + [f"**{L['perf']}**: {perf}", f"**{L['next']}**: {P['next']}", ""])


NEXT_RE = re.compile(r"^\*\*(?:Next|下一步)\*\*.*?(?=^\*\*|^```|\Z)", re.M | re.S)


def next_block(st: State, m: dict, nxt: list) -> str:
    """The card's Next section, built at close from the plan cards: a writer's text can predate a re-plan."""
    zh = _zh(st)
    terms = ({"done": "完成", "report": "写报告", "stop": "停止", "await-user": "等用户决定"} if zh else
             {"done": "done", "report": "write the report", "stop": "stop", "await-user": "wait for the user's decision"})
    items = []
    for x in nxt:
        if x in MS_TERMINAL:
            items.append(terms[x])
            continue
        n = st.milestones[x]
        c = n.get("commitment", "committed")
        if zh:
            s = f"{x}（{COMMIT_ZH.get(c, c)}，{TYPE_ZH.get(n.get('type'), n.get('type'))}）：{n.get('title', '')}"
            if n.get("expect"):
                s += f"；预期相对 {m['id']} {n['expect']:g}×"
            s += "".join(f"；{label}：{n[key]}" for key, label in (("accept", "验收"), ("basis", "依据")) if n.get(key))
            s += f"；约 {n['est_h']:g} 小时" if n.get("est_h") else ""
        else:
            s = f"{x} ({c}, {n.get('type')}): {n.get('title', '')}"
            if n.get("expect"):
                s += f"; expected {n['expect']:g}x vs {m['id']}"
            s += "".join(f"; {key}: {n[key]}" for key in ("accept", "basis") if n.get(key))
            s += f"; ~{n['est_h']:g} h" if n.get("est_h") else ""
        items.append(s)
    later = [x for x in st.milestones.values() if x["status"] == "planned" and x["id"] not in nxt]
    if later and not any(x in MS_TERMINAL for x in nxt):
        if zh:
            items.append("之后：" + "；".join(f"{x['id']}（{COMMIT_ZH.get(x.get('commitment', 'committed'))}）："
                                            f"{x.get('title', '')}" for x in later))
        else:
            items.append("then " + "; ".join(f"{x['id']} ({x.get('commitment', 'committed')}): {x.get('title', '')}"
                                             for x in later))
    head = "**下一步**（mf 收尾时按计划卡生成）：" if zh else "**Next** (by mf, from the plan cards at close):"
    return "\n".join([head] + [f"- {x}" for x in items])


def card_for_commit(st: State, m: dict, card: str, nxt: list) -> str:
    block = next_block(st, m, nxt) + "\n"
    if NEXT_RE.search(card):
        return NEXT_RE.sub(lambda _: block, card, count=1).strip()
    return (card.rstrip() + "\n" + block).strip()


def perf_line(st: State, prep: dict) -> str:
    zh, inc, cum = _zh(st), prep.get("inc"), prep.get("cum")

    def how(x, verdict=True):
        v = (VERDICT_ZH.get(x["verdict"], x["verdict"]) if zh else x["verdict"]) if verdict else ""
        gpu = render.gpu_txt(x.get("gpu"))
        if zh:
            return f"{v + '，' if v else ''}V{x['v']}，同卡 ABBA {x['pairs']} 对，{gpu}"
        return f"{v + ', ' if v else ''}V{x['v']}, ABBA {x['pairs']} pairs, {gpu}"

    if inc and cum and inc.get("v") == cum.get("v"):
        if zh:
            return f"相对冻结基线 {render.fx(cum['speedup'])}（{how(cum)}；冻结后第一个里程碑：增量 = 累计）"
        return f"vs frozen base {render.fx(cum['speedup'])} ({how(cum)}; first milestone after freeze: incremental = cumulative)"
    parts = []
    if inc:
        against = inc.get("against_ms") or ("上一里程碑" if zh else "previous")
        parts.append(f"相对 {against} 增量 {render.fx(inc['speedup'])}（{how(inc)}）" if zh else
                     f"incremental vs {against} {render.fx(inc['speedup'])} ({how(inc)})")
    if cum:
        parts.append(f"相对冻结基线累计 {render.fx(cum['speedup'])}（{how(cum, False)}）" if zh else
                     f"cumulative vs frozen base {render.fx(cum['speedup'])} ({how(cum, False)})")
    return ("；" if zh else "; ").join(parts)


def entry_url(st: State, mid: str) -> str:
    """The entry document as GitHub renders it at this milestone's tag (the commit message itself is plain text)."""
    entry = st.task["doc"].get("entry")
    if not entry:
        return ""
    web = github_web(st.task["git"].get("url"))
    return f"{web}/blob/{tag_name(st, mid)}/{urllib.parse.quote(entry, safe='/')}" if web else entry


def commit_message(st: State, m: dict, nxt: list) -> str:
    zh, prep = _zh(st), m["prep"]
    card_p = ms_dir(st, m["id"]) / "card.md"
    # GitHub shows commit messages as plain text: the card (Markdown) is flattened, the rendered version is the entry doc
    card = render.md_text(card_for_commit(st, m, card_p.read_text(encoding="utf-8").strip() if card_p.is_file() else "", nxt))
    rv = m["roles"].get("review") or {}
    terms = {"done": "完成", "report": "写报告", "stop": "停止", "await-user": "等用户决定"}
    nxt_txt = ("；" if zh else "; ").join(
        (terms.get(x, x) if zh else x) if x in MS_TERMINAL else
        f"{x} {st.milestones[x].get('title', '')}" + (f"（{'承诺' if i == 0 else '暂定'}）" if zh else
                                                     f" ({'committed' if i == 0 else 'tentative'})")
        for i, x in enumerate(nxt))
    size = prep.get("size") or {}
    lines = [f"[{m['id']}] {m.get('title', '')}"[:100], "", card, ""]
    url = entry_url(st, m["id"])
    if url:
        lines += [f"入口文档（渲染版的设计说明：总体设计、关键决策、性能与精度、测试覆盖）：{url}" if zh else
                  f"Entry document (rendered design document: design, key decisions, performance and accuracy, "
                  f"test coverage): {url}", ""]
    tests = max((r for r in m["roles"].values() if r and r.get("tests_files")), key=lambda r: r["t"], default=None)
    if tests:
        lines.append(f"改动已有用例：{', '.join(tests['tests_files'][:6])}；说明（复核）：{tests['tests_why']}" if zh else
                     f"Changed existing tests: {', '.join(tests['tests_files'][:6])}; why (review): {tests['tests_why']}")
    if perf_line(st, prep):
        lines.append(f"性能：{perf_line(st, prep)}" if zh else f"Perf: {perf_line(st, prep)}")
    notes = (("size_why", "超出规模预算", "over size budget"), ("regress_why", "接受的变慢", "accepted regressions"),
             ("verify_why", "接受不一致的复核", "disagreeing verifies accepted"))
    for key, label_zh, label_en in notes:
        if prep.get(key):
            lines.append(f"说明：{label_zh}：{prep[key]}" if zh else f"Note: {label_en}: {prep[key]}")
    if zh:
        lines.append(f"规模：{size.get('lines', 0)} 行，{size.get('files', 0)} 个文件（不含文档 / 用例）")
        lines.append(f"复核：{rv.get('session', '—')}")
        lines.append(f"下一步：{nxt_txt}")
    else:
        lines.append(f"Size: {size.get('lines', 0)} lines in {size.get('files', 0)} files (excluding docs/tests)")
        lines.append(f"Review: {rv.get('session', '—')}")
        lines.append(f"Next: {nxt_txt}")
    # machine-readable trailer (identifiers, not prose)
    lines.append(f"mf: task={st.id} milestone={m['id']}" + (f" attempt=A{prep['attempt']}" if prep.get("attempt") else "")
                 + (f" accept=V{prep['inc']['v']}" if prep.get("inc") else "") + f" runtime-tree={prep['runtime_tree']}")
    return "\n".join(lines).strip() + "\n"


def role_prompts(st: State, m: dict) -> str:
    prep, mid = m["prep"], m["id"]
    wt, pins = prep["wt"], prep["pins"]
    d = ms_dir(st, mid)
    entry = st.task["doc"].get("entry")
    entry_p = (f"{wt}/{entry}（给 tech lead 的设计文档，标准见 skill myflow-review：不含 mf 生成块 ≤{doc_budget(st)} 行）"
               if entry else "（未设置 doc.entry：只写卡片）")
    tp = (", ".join(pins["tests"]) + "（且在 scope.protect 内、不是 bench）") if pins["tests"] else "（未设置 tests.paths：不能改用例）"
    dp = (", ".join(pins["doc"]) + "（只限 .md / .rst）") if pins["doc"] else "（未设置 doc.paths：只写卡片）"
    mf, tid = cli.MF, st.id
    diff = f"git -C {wt} diff {prep['parent'][:12]} --stat"
    lane = prep.get("lane")
    merged = [x["lane"] for x in ((st.lanes.get(lane) or {}).get("merge") or [])] if lane else []
    reports = [st.dir / "notes" / f"{x}-report.md" for x in ([lane] if lane else [mid]) + merged]
    have = [str(p) for p in reports if p.is_file()]
    report = "、".join(have) if have else f"{reports[0]}（没有：直接读代码）"
    return "\n".join([
        f"收尾 {mid} 已就绪：集成 worktree {wt}（候选 {prep['candidate'][:10]}，运行时树 {prep['runtime_tree']}）。",
        "派 1 个独立的复核子 agent（不是实现者的会话），只做一次；下一个里程碑的实现者现在就可以开工（流水线：下一个 prep 时 mf 自动合并本次收尾）：",
        f"子 agent 模型：{cli.SUBAGENT_MODEL}",
        f"prompt：使用 skill myflow-review。任务 {tid} 里程碑 {mid}：对照改动（{diff}）、{wt} 里的代码与用例、"
        f"实现者报告 {report}、`{mf} status {tid} {mid}` 的数据（计划卡、验收数字），更新入口文档 {entry_p}"
        "（只改受影响的节，写成现在的设计；本里程碑改了什么、为什么、与计划的差异写在卡片里），核对基线与对照的来源、"
        "测试覆盖表（按基本功能 / 性能 / 其他三类写覆盖了什么，其他类写明前两类为何覆盖不了，及未覆盖 / 跳过清单），"
        f"按测试原则补上本里程碑新增或改动的逻辑缺的用例，把卡片 {d / 'card.md'} 的 «…» 换成实际内容；能改 {dp}，以及 {tp}。"
        "发现与代码不符直接改，不打回。只跑你新增 / 改动的用例文件（完整功能用例由 mf 收尾时在最终提交上跑），然后运行："
        f"{mf} ms review {tid} {mid} --session <你的会话名> --card {d / 'card.md'} -m \"<一句话>\"（改动或删除了已有用例时加 --tests-why）；"
        "输出里若有「需要用户确认【…】」，原样放在给编排者的结果最前面",
        f"最后：{mf} ms close {tid} {mid} --next <已计划的下一个里程碑 id，或 done|report|stop|await-user>",
        "编排者：子 agent 带回「需要用户确认」时当回合问用户，并 mf brief <任务> --gate <key>；复核完成后 mf brief 向用户汇报",
    ])


def plan_follows_promise(st: State) -> tuple:
    """(True, why) when the current plan is what the last pushed milestone promised as its next step: the user has
    already seen it in that commit (user rule 11-22: ask about a plan only when it deviates or the goal moved)."""
    if not st.closed_ms:
        return False, "还没有推送过承诺的下一步（第一次计划要问用户）"
    last = st.closed_ms[-1]
    close = last.get("close") or {}
    nxt = (close.get("next") or [None])[0]
    if not nxt or nxt in MS_TERMINAL or nxt not in st.milestones:
        return False, f"{last['id']} 没有承诺下一个里程碑"
    m, t0 = st.milestones[nxt], close.get("t") or 0
    if m["status"] in ("dropped", "revoked"):
        return False, f"承诺的 {nxt} 已放弃"
    moved = [p for p in m.get("plans") or [] if p["t"] > t0 and p.get("why")]
    if moved:
        return False, f"{nxt} 在 {last['id']} 推送后改过计划（{moved[-1]['why'][:80]}）"
    goal = [c for c in st.changes if c["t"] > t0 and c["key"] in ("goal", "metric.target")]
    if goal:
        return False, f"{last['id']} 推送后改过目标（{goal[-1]['key']}）"
    return True, f"计划与 {last['id']} 推送时承诺的下一步一致（{nxt}）"


def ensure_plan_gate(st: State, rnd: int) -> bool:
    """The round's plan gate is approved, or the plan follows the pushed promise and mf records that it only notifies."""
    if st.gate_ok("plan", rnd):
        return True
    ok, why = plan_follows_promise(st)
    if not ok:
        return False
    append_event(st.dir, "decide", f"{why}：按用户规则（11-22）只通知，不再单独确认", by="mf", gate=gate_key("plan", rnd))
    print(f"[mf] {why}：计划只通知、不再问用户（换方向或改目标时才问）", file=sys.stderr)
    return True


def cmd_plan(a):
    tdir = task_dir_of(a.task)
    st = State(tdir)
    _check_id(a.id)
    m = st.milestones.get(a.id)
    if m and m["status"] in ("done", "dropped"):
        raise MfError(f"{a.id} is already {m['status']}; plan a new id")
    if m and m["status"] == "closing":
        raise MfRefused(f"{a.id} 正在收尾，计划卡不能再改（类型、验收会影响收尾要求）：先 mf ms drop，再用新编号计划")
    if not m and not a.title:
        raise MfError("--title is required for a new milestone")
    mtype = a.type or (m or {}).get("type") or "perf"
    if mtype == "perf" and a.expect is None and not (m or {}).get("expect"):
        print("[mf] 提示：性能型里程碑请给出 --expect（预期相对上一里程碑的加速，如 1.10）和 --basis（依据）；"
              "方向检查会拿它对照实测", file=sys.stderr)
    data = {"id": a.id, "type": mtype, "goals": a.goal or [], "accept": a.accept, "expect": a.expect, "basis": a.basis,
            "scope": a.scope, "est_h": a.hours, "why": a.why, "round": st.round,
            "commitment": "tentative" if a.tentative else "committed"}
    append_event(tdir, "ms.plan", a.title or m["title"], by="agent",
                 **{k: v for k, v in data.items() if v not in (None, "", [])})
    if getattr(a, "ceiling", None):
        append_event(tdir, "ceiling", a.basis or f"{a.id} 计划时的估计", by="agent", value=a.ceiling)
    st = State(tdir)
    print("计划卡 " + plan_line(st.milestones[a.id]))
    if st.freeze and mtype == "perf":  # rounds are performance rounds; a docs-only milestone does not start one
        cli.auto_phase(st, "plan", f"计划 {a.id}")
        st = State(tdir)
    if a.ask and st.freeze and ensure_plan_gate(st, st.round):
        print(f"gate【{gate_key('plan', st.round)}】不需要问用户：计划与上次推送的承诺一致（看板与提交里已可见）")
    elif a.ask:
        # before the freeze this plans the functional version: it must not pre-approve the R1 (performance) plan gate
        phase = "plan" if st.freeze else "bootstrap"
        key = gate_key(phase, st.round)
        mine = [x for x in st.milestones.values() if x["status"] == "planned" and x.get("round") == st.round]
        text = f"R{st.round} 计划：" if st.freeze else "功能版计划："
        text += "；".join(plan_line(x) for x in mine)
        append_event(tdir, "brief", text[:600], by="agent", phase=phase, round=st.round, ask=True)
        print(f"gate【{key}】已挂起：把计划卡给用户看并等待回复；回复后 "
              f"{cli.MF} decide {st.id} --gate {key} \"<用户原话>\"")
    cli._after(tdir)


def cmd_bootstrap(a):
    tdir = task_dir_of(a.task)
    st = State(tdir)
    if st.freeze:
        raise MfError("already frozen: bootstrap is for a new operator before the first freeze")
    if not st.gate_ok("spec", 1):
        raise MfRefused("需求尚未经用户确认（spec gate）：先对齐需求再做功能版")
    repo = _repo(st)
    wt = st.dir / "wt" / "_bootstrap"
    if st.phase == "spec":  # building the functional version is its own phase, not "still aligning requirements"
        append_event(tdir, "phase", "开始做功能版（bootstrap）", by="mf", phase="bootstrap", round=st.round)
    if st.bootstrap and (wt / ".git").exists():
        print(f"bootstrap worktree exists: {wt} (from {st.bootstrap['start'][:10]})")
        return
    start = rev_parse(repo, a.start or st.task.get("base") or "HEAD")
    branch = f"mf/{st.id}/_bootstrap"
    ensure_worktree(repo, wt, start, branch)
    append_event(tdir, "ms.bootstrap", f"bootstrap worktree from {start[:10]}", by="agent", wt=str(wt), start=start,
                 branch=branch)
    print(f"bootstrap worktree: {wt} (branch {branch}, from {start[:10]})")
    print(f"next: 在这里做出功能正确版 + 用例 + bench + 文档骨架 → {cli.MF} ms plan {st.id} M0 --type feature --title ... "
          f"→ {cli.MF} ms prep {st.id} M0 --bootstrap → 三个角色 → {cli.MF} ms close {st.id} M0 --next ... → {cli.MF} freeze {st.id}")
    cli._after(tdir)


def cmd_prep(a):
    tdir = task_dir_of(a.task)
    st = State(tdir)
    m = _get(st, a.id)
    if m["status"] != "planned":
        raise MfError(f"{a.id} is {m['status']} (prep needs a planned milestone; mf ms drop abandons a closing one)")
    if st.open_ms:
        raise MfRefused(f"{st.open_ms['id']} is still closing: close or drop it first (one milestone at a time)")
    repo, t = _repo(st), st.task
    pins = pins_now(st)
    if t["doc"].get("entry"):
        _check_entry(t["doc"]["entry"])
    parent = parent_commit(st)
    inc = cum = func = None
    attempt = lane = None
    if a.bootstrap:
        if st.freeze:
            raise MfError("--bootstrap is only for the first milestone of a new operator, before freeze")
        if not st.bootstrap:
            raise MfError(f"no bootstrap worktree: mf ms bootstrap {st.id}")
        if m["type"] != "feature":
            raise MfRefused(f"bootstrap 里程碑必须是功能型：mf ms drop {st.id} {a.id} 后用 --type feature 重新计划")
        if not st.gate_ok("spec", 1):
            raise MfRefused("需求尚未经用户确认（spec gate）：先对齐需求再做功能版")
        bwt = Path(st.bootstrap["wt"])
        candidate = snapshot(bwt, [], _ignore(st), f"mf {st.id}/bootstrap: {a.id} candidate")
        last = _pipeline_base(st, candidate, parent, f"bootstrap 候选 {candidate[:10]}")
        if last:
            candidate = _merge_close(st, candidate, parent, last, a.id)
            git(["merge", "-q", "--ff-only", candidate], bwt)  # the next milestone continues from the merged commit
        res = _correctness_at(st, candidate, "prep")
        if res["rc"] != 0:
            raise MfRefused(f"功能用例在 bootstrap 候选上未通过（rc={res['rc']}）：{res['log']}\n{cli.tail(res['log'])}")
        func = {"rc": 0, "log": res["log"]}
    else:
        why = guard.refusal(st, "verify")  # frozen, contract intact, spec confirmed
        if why:
            raise MfRefused(why)
    if a.lane and not a.bootstrap:
        ln = cli._lane(st, a.lane)
        lane = a.lane
        if a.attempt:
            at = st.attempt(a.attempt)
            if not at or at.get("lane") != lane:
                raise MfError(f"A{a.attempt} is not an attempt of lane {lane}")
        else:
            bv = ln["best_verified"]
            at = st.attempt(bv["attempt"]) if bv else ln["best_self"]
            if not at:  # an integration lane (merges only) needs no `mf try`: its head is the candidate
                wt_l = Path(ln["wt"])
                if uncommitted_files(wt_l):
                    raise MfRefused(f"lane {lane} 有未提交的改动：先提交或 mf try，再 prep")
                head = git(["rev-parse", "HEAD"], wt_l)
                if head == ln.get("start"):
                    raise MfError(f"lane {lane} has no changes and no passing attempt")
                at = {"n": None, "commit": head, "status": PASS, "lane": lane}
        if at.get("status") != PASS:
            raise MfRefused(f"A{at.get('n')} did not pass ({at.get('status')})")
        candidate, attempt = at["commit"], at.get("n")
        last_pipe = _pipeline_base(st, candidate, parent, f"A{attempt}" if attempt else f"lane {lane} 的头部")
        # user request 11-22: prep measures what is missing itself (no separate verify / `mf try` first); when both
        # numbers are missing, one three-arm run (frozen base / previous milestone / candidate) gives both
        need_inc = parent != st.freeze["base"]
        cumv, problem = _pick_verify(st, candidate)
        if problem and not a.verify_why:
            raise MfRefused(problem)
        incv = cumv if not need_inc else None
        if need_inc and m["type"] in ("perf", "maint"):
            incv, problem = _pick_verify(st, candidate, against=parent)
            if problem and not a.verify_why:
                raise MfRefused(problem)
        if m["type"] in ("perf", "maint") and not (cumv and incv):
            prev_ms = st.last_ms["id"] if st.last_ms else None
            kw = dict(pairs=a.pairs, gpu=a.gpu, timeout=a.timeout)
            if not cumv and need_inc:
                print(f"[mf] 验收：一次三方交替测量（冻结基线 / {prev_label(st)} / 候选），同时得到累计与增量 …", flush=True)
                cli.verify_commit(st, lane, at, against=parent, against_ms=prev_ms, both=True, **kw)
            elif not cumv:
                print("[mf] 验收：候选对冻结基线做同卡 ABBA …", flush=True)
                cli.verify_commit(st, lane, at, **kw)
            else:
                print(f"[mf] 增量验收：候选对 {prev_label(st)} 做同卡 ABBA …", flush=True)
                cli.verify_commit(st, lane, at, against=parent, against_ms=prev_ms, **kw)
            st = State(tdir)
            m = st.milestones[a.id]
            cumv, _ = _pick_verify(st, candidate)
            incv = cumv if not need_inc else _pick_verify(st, candidate, against=parent)[0]
            if not cumv:
                raise MfRefused("验收没有得到有效结果（见上面的 verify 输出）")
        cum, inc = _vsum(cumv), _vsum(incv)
        if last_pipe:  # measured on the lane commit; the merged commit has the same runtime tree
            candidate = _merge_close(st, candidate, parent, last_pipe, a.id)
        if inc and not inc.get("against_ms"):
            inc["against_ms"] = st.last_ms["id"] if st.last_ms else "base"
        if m["type"] in ("perf", "maint"):
            if not inc:
                raise MfRefused(f"增量验收没有得到有效结果（见上面的 verify 输出）：{a.id} 不进入收尾")
            if m["type"] == "perf" and inc["verdict"] != "improved":
                raise MfRefused(f"增量验收未通过：A{attempt} 对 {prev_label(st)} {render.fx(inc['speedup'])}（{inc['verdict']}，V{inc['v']}）。"
                                "性能型里程碑要求 improved：换候选、改为其他类型，或 mf ms drop")
            if m["type"] == "maint" and inc["verdict"] == "regression":
                raise MfRefused(f"维护型里程碑不能变慢：对 {prev_label(st)} {render.fx(inc['speedup'])}（V{inc['v']}）")
            if inc["regressed"] and not a.regress_why:
                raise MfRefused(f"有 case 相对 {prev_label(st)} 变慢超阈值：{', '.join(inc['regressed'][:6])}。"
                                "确认可接受时加 --regress-why \"原因\"（会写进卡片）")
    elif not a.bootstrap:
        if m["type"] not in ("analysis", "maint"):
            raise MfError("--lane (or --bootstrap) is required for feature/perf milestones; "
                          "analysis/maint milestones without --lane change only docs/tests")
        candidate = parent
    excluded = non_runtime(pins)
    if pins["tests"]:
        under = [p for p, _ in ls_tree(repo, candidate) if match(p, pins["tests"])]
        loose = [p for p in under if not match(p, pins["protect"])]
        if loose or not under:
            print(f"[mf] ⚠ tests.paths 下{'有不受 scope.protect 保护的文件：' + ', '.join(loose[:5]) if loose else '目前没有文件'}。"
                  "测试负责人只能改受保护的用例，新增的用例文件也必须匹配 scope.protect"
                  "（建议 protect 用目录通配，例如 tests/ops/<op>/**），否则收尾会拒绝", file=sys.stderr)
    rt = runtime_tree_hash(repo, candidate, excluded)
    size = _size(st, parent, candidate, pins)
    mx = int(t["milestone"].get("max_lines") or 0)
    if mx and size["lines"] > mx and not a.size_why:
        raise MfRefused(f"{a.id} 改动 {size['lines']} 行（不含文档/用例）> 上限 {mx}：该拆了。"
                        "确需一次提交时加 --size-why \"原因\"（会显示在卡片上）")
    wt = st.dir / "wt" / "_ms"
    ensure_worktree(repo, wt, candidate)
    reset_detached(wt, candidate, "fd")
    br = int_branch(st)
    cur = ref_sha(repo, f"refs/heads/{br}")
    if cur is None:
        update_branch(repo, br, parent)
    elif cur != parent:
        raise MfError(f"integration branch {br} is at {cur[:10]}, expected {parent[:10]} (the last milestone); moved by hand?")
    prep = {"id": a.id, "lane": lane, "attempt": attempt, "candidate": candidate, "parent": parent,
            "runtime_tree": rt, "pins": pins, "size": size, "size_why": a.size_why, "regress_why": a.regress_why,
            "verify_why": a.verify_why, "inc": inc, "cum": cum, "wt": str(wt), "bootstrap": bool(a.bootstrap),
            "func": func, "freeze_seq": (st.freeze or {}).get("seq")}
    entry = t["doc"].get("entry")
    if entry:
        p = wt / entry
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            tpl = (PKG_ROOT / "templates" / "ENTRY.md").read_text(encoding="utf-8")
            p.write_text(tpl.replace("{{title}}", t.get("title") or st.id), encoding="utf-8")
        _fill_entry(st, p, current=dict(m, prep=prep, status="closing"))
    stage_worktree(wt, _ignore(st))
    prep["tree"] = write_tree(wt)
    mine = tree_diff_names(repo, candidate, prep["tree"])
    if any(not is_doc(f, pins) for f in mine):  # mf itself only writes the entry document
        raise MfError(f"unexpected files in the integration worktree after prep: {mine}")
    d = ms_dir(st, a.id)
    if not (d / "card.md").exists():
        atomic_write(d / "card.md", _card_skeleton(st, m, prep))
    who = (f"A{attempt} [{lane}]" if attempt else f"[{lane}] 头部 {candidate[:10]}" if lane else
           "bootstrap" if a.bootstrap else "仅文档/用例")
    append_event(tdir, "ms.prep", f"{a.id} 收尾开始：候选 {who}", lane=lane, by="mf",
                 **{k: v for k, v in prep.items() if k != "lane"})
    st = State(tdir)
    m = st.milestones[a.id]
    print(f"{a.id} prep ok · 候选 {who} {candidate[:10]} · 父 {prev_label(st)} {parent[:10]} · 改动 {size['lines']} 行/{size['files']} 文件")
    if perf_line(st, prep):
        print(f"  {perf_line(st, prep)}")
    print(role_prompts(st, m))
    if st.freeze:
        cli.auto_phase(st, "integrate", f"收尾 {a.id}")
    cli._after(tdir)


# ---------------------------------------------------------------- roles


def _gate_missing(st: State, key: str) -> bool:
    """Nobody is being (or was successfully) asked about `key`: never opened, or opened and later superseded."""
    return (st.gates.get(key) or {}).get("state") not in ("pending", "approved")


def _open_gate(st: State, key: str, text: str) -> None:
    if _gate_missing(st, key):
        append_event(st.dir, "brief", text[:600], by="mf", phase=key, round=st.round, ask=True)


def relay_hint(st: State, key: str) -> str:
    """mf only records a gate; a person has to be asked. Printed wherever mf opens one."""
    return (f"mf 不会自己问用户：编排者现在就在对话里问（不要攒到后面），问完 {cli.MF} brief {st.id} --gate {key} \"<问了什么>\"；"
            f"用户答复后 {cli.MF} decide {st.id} --gate {key} \"<用户原话>\"。子 agent：把这段原样带回给编排者")


def _clear_gates(st: State, prefix: str, keep=None, why: str = "") -> None:
    """Retire pending gates that no longer apply, so they stop asking the user."""
    for key, g in list(st.gates.items()):
        if key.startswith(prefix) and key != keep and g.get("state") == "pending":
            append_event(st.dir, "gate.clear", why, by="mf", gate=key)


def cmd_review(a):
    """The closing review (user rule 11-21): one independent sub-agent writes / checks the entry document section,
    the coverage table, missing tests and the card, and records once. It may change docs and tests, never code."""
    tdir = task_dir_of(a.task)
    st = State(tdir)
    m = _get(st, a.id)
    if m["status"] != "closing":
        raise MfError(f"{a.id} is not being closed (mf ms prep {st.id} {a.id} first)")
    repo, prep = _repo(st), m["prep"]
    pins, wt = prep["pins"], Path(prep["wt"])
    stage_worktree(wt, _ignore(st))
    tree = write_tree(wt)
    changed = tree_diff_names(repo, m.get("checkpoint") or prep["tree"], tree)
    benchy = [f for f in changed if match(f, pins["protect"]) and _bench_ref(f, pins)]
    if benchy:
        raise MfRefused(f"改到了 bench / 性能用例（{', '.join(benchy)}）：这会改变度量，需要用户批准并重新冻结基线，"
                        "不能在收尾里做。先撤销，改为单独和用户确认")
    bad = [f for f in changed if not (is_doc(f, pins) or is_test(f, pins))]
    if bad:
        raise MfRefused(f"复核只能改 doc.paths（{', '.join(pins['doc']) or '未设置'}）里的 .md / .rst 文档，以及 tests.paths"
                        f"（{', '.join(pins['tests']) or '未设置'}）里受 scope.protect 保护的用例文件（bench 除外），"
                        f"但上次记录之后改了：{', '.join(bad[:8])}。在 {wt} 里撤销这些改动"
                        f"（git -C {wt} checkout HEAD -- <文件>，新文件直接删除）后再记录")
    data = {"id": a.id, "role": "review", "result": "done", "session": a.session, "tree": tree, "changed": changed}
    files, tkey, tsum = _oracle_change(st, m, tree)
    if files:
        if not (a.tests_why or "").strip():
            raise MfRefused(f"本里程碑改动或删除了已有的用例 / bench：{tsum}。不需要用户批准，但要说明为什么符合测试原则"
                            f"（{TEST_PRINCIPLE}）：加 --tests-why \"…\"（会写进提交信息）。若这个改动违反原则（放宽容差、"
                            "删掉仍需要的覆盖、加与已有用例重复的用例……），不要这样改，交编排者问用户")
        data.update(tests_key=tkey, tests_files=files, tests_why=a.tests_why.strip()[:600])
    d = ms_dir(st, a.id)
    card = Path(a.card).expanduser().resolve() if a.card else d / "card.md"
    if not card.is_file() or not card.read_text(encoding="utf-8").strip():
        raise MfRefused(f"卡片 {card} 不存在或为空")
    if card != (d / "card.md").resolve():
        shutil.copy2(card, d / "card.md")
    text = (d / "card.md").read_text(encoding="utf-8")
    if PLACEHOLDER in text:
        raise MfRefused(f"卡片里还有 {text.count(PLACEHOLDER)} 处 «…» 占位符没填：{d / 'card.md'}")
    entry = st.task["doc"].get("entry")
    entry_text = (wt / entry).read_text(encoding="utf-8") if entry and (wt / entry).is_file() else ""
    rule = lang_rule(st.task)
    if rule and rule[1] == "zh":  # the card goes into the pushed commit message; the entry doc is published too
        docs = [("卡片", text)] + ([("入口文档", entry_text)] if entry_text else [])
        wrong = [what for what, body in docs if cjk_share(body) < CJK_MIN]
        if wrong:
            raise MfRefused(f"规则：推送到 {rule[0]} 的文档、提交信息、PR 描述一律用中文，但{'、'.join(wrong)}"
                            "看起来不是中文（代码与标识符可以保留原文）：改成中文后再记录")
    n = len(text.strip().splitlines())
    if n > CARD_MAX_LINES:
        print(f"[mf] ⚠ 卡片 {n} 行 > {CARD_MAX_LINES}：卡片要一屏读完，细节放入口文档或深度参考", file=sys.stderr)
    for p in (doc_problems(st, entry_text) if entry_text else []):
        print(f"[mf] ⚠ 入口文档：{p}。按设计文档标准精简（skill myflow-review）", file=sys.stderr)
    data.update(card=str(d / "card.md"), card_hash=file_hash(d / "card.md"))
    append_event(tdir, "ms.role", (a.summary or "")[:300], by="agent", **data)
    print(f"{a.id} 复核已记录（会话 {a.session}；改动 {len(changed)} 个文件）")
    if data.get("tests_files"):
        print(f"  已有用例 / bench 的改动已说明（写进提交信息）：{tsum}")
    st = State(tdir)
    missing = _close_blockers(st, st.milestones[a.id])
    print("  可以收尾：" + f"{cli.MF} ms close {st.id} {a.id} --next <M? | done|report|stop|await-user>" if not missing
          else "  收尾前还差：" + "；".join(missing))
    cli._after(tdir)


GEN_BLOCK = re.compile(r"(<!-- mf:(\w+) -->).*?(<!-- /mf:\2 -->)", re.S)


def _blocks_only(st: State, a, b) -> bool:
    """Trees a and b differ only inside mf's generated blocks of the entry document (mf data, not prose)."""
    entry = st.task["doc"].get("entry")
    if not (a and b and entry):
        return False
    repo = _repo(st)
    if tree_diff_names(repo, a, b) != [entry]:
        return False
    try:
        old, new = (git(["show", f"{t}:{entry}"], repo) for t in (a, b))
    except MfError:
        return False
    return GEN_BLOCK.sub(r"\1\3", old) == GEN_BLOCK.sub(r"\1\3", new)


def _close_blockers(st: State, m: dict, tree=None) -> list:
    out, d = [], ms_dir(st, m["id"])
    rv = m["roles"].get("review")
    if not rv:
        out.append("复核记录（mf ms review … --session <会话> --card …）")
    elif tree and rv.get("tree") != tree and not _blocks_only(st, rv.get("tree"), tree):
        out.append("复核记录之后 worktree 又有改动：让复核重新记录，或撤销")
    if rv and file_hash(d / "card.md") != rv.get("card_hash"):
        out.append("卡片在复核记录之后被改过：让复核重新记录")
    files, key, _ = _oracle_change(st, m, tree or m.get("checkpoint") or m["prep"]["tree"])
    if files and not _tests_attested(m, key):
        out.append(f"已有用例 / bench 的改动（{', '.join(files[:4])}）没有说明：复核记录时加 --tests-why")
    return out


# ---------------------------------------------------------------- close / push


def cmd_close(a):
    tdir = task_dir_of(a.task)
    st = State(tdir)
    m = _get(st, a.id)
    if m["status"] != "closing":
        raise MfError(f"{a.id} is {m['status']}: mf ms prep {st.id} {a.id} first")
    rule = lang_rule(st.task)
    wrong = [k for k in ("git", "doc") if rule and (st.task[k].get("lang") or "") != rule[1]]
    if wrong:
        raise MfRefused(f"规则：推送到 {rule[0]} 的文档、提交信息、PR 描述一律用{LANG_ZH.get(rule[1], rule[1])}："
                        f"先 mf set {st.id} " + " ".join(f"{k}.lang={rule[1]}" for k in wrong) + "，卡片也要用该语言写")
    if st.freeze and not st.contract_ok:
        raise MfRefused("冻结后度量契约被改动：先和用户确认并 mf freeze，再重新 prep")
    prep = m["prep"]
    if prep.get("freeze_seq") != (st.freeze or {}).get("seq"):
        raise MfRefused(f"prep 之后重新冻结过：验收数字属于旧基线。mf ms drop {st.id} {a.id} 后用新编号重新计划并 prep")
    nxt = [x.strip() for item in a.next for x in item.split(",") if x.strip()]
    if not nxt:
        raise MfError("--next is required: a planned milestone id, or done|report|stop|await-user")
    for x in nxt:
        if x in MS_TERMINAL:
            if len(nxt) > 1:
                raise MfError(f"terminal next '{x}' must be the only --next")
        elif x == a.id or x not in st.milestones or st.milestones[x]["status"] != "planned":
            raise MfRefused(f"--next {x}: 必须是已计划、未开始的里程碑（mf ms plan {st.id} {x} --title ... --expect ... --basis ...），"
                            "或 done|report|stop|await-user")
    repo, pins, wt = _repo(st), prep["pins"], Path(prep["wt"])
    stage_worktree(wt, _ignore(st))
    tree = write_tree(wt)
    blockers = _close_blockers(st, m, tree)
    if blockers:
        raise MfRefused(f"{a.id} 还不能收尾：" + "；".join(blockers))
    card = (ms_dir(st, a.id) / "card.md").read_text(encoding="utf-8")
    if PLACEHOLDER in card:
        raise MfRefused("卡片里还有 «…» 占位符")
    entry = st.task["doc"].get("entry")
    if entry and (wt / entry).is_file() and _fill_entry(st, wt / entry, current=m, nxt=nxt):
        # mf's own blocks (roadmap, summary) show the plan as of this close, not as of prep
        stage_worktree(wt, _ignore(st))
        tree = write_tree(wt)
    rt = runtime_tree_hash(repo, tree, non_runtime(pins))
    if rt != prep["runtime_tree"]:
        raise MfRefused("运行时树与验收时的候选不同（文档 / 受保护用例之外有改动）：验收结果不再适用。"
                        f"撤销这些改动，或 mf ms drop {st.id} {a.id} 后用新编号重新计划并 prep")
    parent, br = prep["parent"], int_branch(st)
    if ref_sha(repo, f"refs/heads/{br}") != parent:
        raise MfError(f"integration branch {br} moved since prep (expected {parent[:10]})")
    tag = tag_name(st, a.id)
    if ref_sha(repo, f"refs/tags/{tag}"):
        raise MfError(f"tag {tag} already exists in {repo}; delete it or use another milestone id")
    msg = commit_message(st, m, nxt)
    cfg = _sign_cfg(st)
    commit = commit_tree(wt, tree, parent, msg, cfg)  # an unreferenced object until the branch moves
    res = _correctness_at(st, commit, "close")
    if res["rc"] != 0:
        raise MfRefused(f"最终提交上的功能用例未通过（rc={res['rc']}）：{res['log']}\n{cli.tail(res['log'])}")
    files, key, summary = _oracle_change(st, m, tree)
    d = ms_dir(st, a.id)
    make_tag(wt, tag, commit, msg.splitlines()[0], cfg)  # tag first: a failure here leaves nothing to undo
    try:
        update_branch(repo, br, commit, parent)
    except MfError:
        delete_tag(repo, tag)
        raise
    try:
        atomic_write(d / "commit-msg.txt", msg)
        if files:
            rec = _tests_attested(m, key)
            append_event(tdir, "oracle.accept", f"{a.id}：{summary}", by="mf", id=a.id, gate=key, paths=files,
                         approved_by=f"{rec.get('role')}:{rec.get('session')}", tests_why=rec.get("tests_why"),
                         blobs={p: b for p, b in ls_tree(repo, tree) if p in files}, commit=commit)
        append_event(tdir, "ms.close", f"{a.id} {m.get('title', '')}", by="mf", id=a.id, commit=commit, tag=tag,
                     parent=parent, runtime_tree=rt, tree=tree, next=nxt, why=a.why, correctness_log=res["log"],
                     msg=str(d / "commit-msg.txt"), oracle=files)
    except Exception:
        update_branch(repo, br, parent, commit)
        delete_tag(repo, tag)
        raise
    print(f"✅ {a.id} 已收尾：{commit[:10]}（分支 {br}，tag {tag}）")
    st = State(tdir)
    if nxt[0] in ("report", "done") and st.freeze:
        cli.auto_phase(st, "report", f"{a.id} 收尾：{nxt[0]}")
        st = State(tdir)
    moved = False
    if prep.get("bootstrap") and st.bootstrap and not st.freeze:
        bwt = Path(st.bootstrap["wt"])
        if git(["rev-parse", "HEAD"], bwt) == prep["candidate"] and not uncommitted_files(bwt):
            git(["reset", "-q", "--hard", commit], bwt)  # nobody started the next milestone yet: catch up
        else:  # pipelining: the next milestone already started there during the review; never throw that away
            moved = True
            print(f"  bootstrap worktree 里下一个里程碑已经开工：不重置；它 prep 时 mf 会自动合并 {a.id} 的收尾提交（只多了文档 / 用例）")
    _clear_gates(st, f"oracle@{a.id}:", why=f"{a.id} 已收尾")
    st = State(tdir)
    print(push_pending(st, strict=False))  # the milestone is closed: a push problem is reported, not an error
    if prep.get("bootstrap") and not st.freeze:
        print(f"next: 继续在 bootstrap worktree（{'下一个里程碑已在进行' if moved else f'已对齐到 {a.id}'}）做下一个功能里程碑"
              f"（mf ms prep {st.id} M? --bootstrap），或 {cli.MF} freeze {st.id}（默认以最新的里程碑为冻结基线）")
    cli._after(tdir)


def spec_push_key(st: State):
    """The push gate that approving the requirements also approves (user rule 11-22: one question, not two).
    None when there is no valid target yet, or the user wants to be asked at every push."""
    url = (st.task["git"].get("url") or "").strip()
    if (not url or (st.task["git"].get("push") or "first-ask") != "first-ask" or url.startswith("-")
            or any(ch.isspace() for ch in url) or not valid_branch(remote_branch(st))):
        return None
    return push_gate_key(st)


def push_gate_key(st: State, mid=None) -> str:
    key = "push:" + sha16(f"{st.task['git'].get('url', '').strip()}|{remote_branch(st)}")[:8]
    return f"{key}@{mid}" if mid else key


def push_pending(st: State, strict: bool = True) -> str:
    url = (st.task["git"].get("url") or "").strip()
    if not url:
        return f"未配置 git.url：里程碑只在本地提交 + tag（mf set {st.id} git.url=<你的 fork> 后 mf ms push {st.id}）"
    br, mode = remote_branch(st), st.task["git"].get("push") or "first-ask"
    bad = (f"git.url={url!r} is not a repository URL" if url.startswith("-") or any(ch.isspace() for ch in url)
           else None if valid_branch(br) else f"git.branch={br!r} is not a valid branch name")
    if bad:
        if strict:
            raise MfError(bad)
        return f"✖ 没有推送：{bad}（修正后 mf ms push {st.id}）"
    if st.freeze and not st.contract_ok:
        raise MfRefused("冻结后度量契约被改动：先和用户确认并 mf freeze，再推送")
    pending = [m for m in st.closed_ms if not m.get("pushed")]
    if not pending:
        return "已完成的里程碑都已推送"
    ids = [m["id"] for m in pending]
    key = push_gate_key(st, ids[-1] if mode == "ask" else None)
    if mode != "auto" and (st.gates.get(key) or {}).get("state") != "approved":
        if _gate_missing(st, key):
            # another target or batch: the old question no longer applies and must stop asking the user
            _clear_gates(st, "push:", keep=key, why="推送目标或批次变了：旧的推送确认作废")
            append_event(st.dir, "ms.pushgate", f"推送目标 {url} · {br}", by="mf", key=key, url=url, branch=br, ids=ids)
            later = "之后每次推送都会再问" if mode == "ask" else "之后推到同一目标不再询问；目标变了会重新问"
            append_event(st.dir, "brief", f"请确认推送目标：{url} 分支 {br}（含 tag {', '.join(tag_name(st, i) for i in ids)}）。{later}。",
                         by="mf", phase=key, round=st.round, ask=True)
        return (f"⏸ 需要用户确认推送目标【{key}】：{url} · 分支 {br}。{relay_hint(st, key)}；批准后 {cli.MF} ms push {st.id}")
    repo, last = _repo(st), st.last_ms_commit
    local = ref_sha(repo, f"refs/heads/{int_branch(st)}")
    tags = [m["close"]["tag"] for m in st.closed_ms]
    leases = []
    if local != last:
        ok, out = False, f"集成分支 {int_branch(st)} 不在最后一个里程碑 {last[:10]} 上（现在 {str(local)[:10]}）：被移动过，拒绝推送"
    else:
        try:
            remote = remote_head(repo, url, br)
            leases, why = _replace_leases(st, repo, url, br, remote, last, tags)
            if why:
                ok, out = False, why
            elif leases:  # overwriting revoked milestones on the remote: always the user's call
                rkey = "push-replace:" + sha16(f"{url}|{br}|{last}|" + ",".join(f"{r}={o}" for r, o in leases))[:8]
                if (st.gates.get(rkey) or {}).get("state") != "approved":
                    gone = ", ".join(f"{r.split('/', 2)[-1]}（{o[:10]}）" for r, o in leases)
                    _open_gate(st, rkey, f"请确认覆盖远端：{url} 分支 {br} 上已撤销的内容（{gone}）将被新的 {', '.join(ids)} "
                                         f"（{last[:10]}）替换，旧提交从该分支消失；只在远端仍是这些对象时才会覆盖。")
                    return (f"⏸ 覆盖远端需要用户批准【{rkey}】：{gone} → {last[:10]}。{relay_hint(st, rkey)}；"
                            f"批准后 {cli.MF} ms push {st.id}")
                ok, out = push_atomic(repo, url, [f"{last}:refs/heads/{br}"] + [f"refs/tags/{x}:refs/tags/{x}" for x in tags],
                                      leases)
            else:  # publish the recorded milestone commit itself, never whatever a ref points to now
                ok, out = push_atomic(repo, url, [f"{last}:refs/heads/{br}"] + [f"refs/tags/{x}:refs/tags/{x}" for x in tags])
        except MfError as e:
            ok, out = False, str(e)
    append_event(st.dir, "ms.push", f"{'推送' if ok else '推送失败'} {', '.join(ids)} → {br}", by="mf", ok=ok, url=url,
                 branch=br, ids=ids, tags=tags, commit=last, replaced=[r for r, _ in leases] or None,
                 error=None if ok else out[-800:])
    if not ok:
        return f"✖ 推送失败（{', '.join(ids)}）：{out[-400:]}\n  修好后重试：{cli.MF} ms push {st.id}"
    _clear_gates(State(st.dir), "push", why="已推送：其余的推送确认不再需要")
    web = github_web(url)
    link = f"\n  GitHub：{web}/commits/{br} · {web}/tree/{tag_name(st, ids[-1])}" if web else ""
    return f"⬆ 已推送 {', '.join(ids)} → {url} · {br}（tag 原子推送{'；覆盖了已撤销的内容' if leases else ''}）{link}"


def _replace_leases(st: State, repo, url: str, br: str, remote, last: str, tags: list) -> tuple:
    """Remote refs this push must overwrite, as (ref, expected object) leases, or ([], why) when it may not.

    Only content mf itself pushed and later revoked can be overwritten: the branch while it holds a revoked
    milestone's pushed commit, a tag while it holds the tag object recorded when it was revoked."""
    leases = []
    if remote and remote != last and not is_ancestor(repo, remote, last):
        rv = next((r for r in reversed(st.revoked) if (r.get("push") or {}).get("commit") == remote
                   and r["push"].get("url") == url and r["push"].get("branch") == br), None)
        if not rv:
            return [], f"远端分支 {br} 有本地没有的提交（{remote[:10]}）：拒绝非快进推送，先查明是谁推的"
        leases.append((f"refs/heads/{br}", remote))
    revoked_tags = {r["tag"]: r.get("tag_obj") for r in st.revoked if r.get("push")}
    for tag in tags:
        theirs = remote_tag(repo, url, tag)
        mine = git(["rev-parse", "--verify", "--quiet", f"refs/tags/{tag}"], repo, check=False) or None
        if theirs and theirs != mine:
            if revoked_tags.get(tag) != theirs:
                return [], f"远端 tag {tag} 指向别的对象（{theirs[:10]}），不是 mf 推送后又撤销的那个：拒绝覆盖"
            leases.append((f"refs/tags/{tag}", theirs))
    return leases, None


def cmd_push(a):
    tdir = task_dir_of(a.task)
    print(push_pending(State(tdir)))
    cli._after(tdir)


def cmd_drop(a):
    tdir = task_dir_of(a.task)
    st = State(tdir)
    m = _get(st, a.id)
    if m["status"] in ("done", "dropped", "revoked"):
        raise MfError(f"{a.id} is already {m['status']}" + ("（已收尾的用 mf ms revoke）" if m["status"] == "done" else ""))
    append_event(tdir, "ms.drop", " ".join(a.reason)[:300], by="agent", id=a.id)
    _clear_gates(State(tdir), f"oracle@{a.id}:", why=f"{a.id} 已放弃")
    if m["status"] == "closing":
        reset_detached(Path(m["prep"]["wt"]), m["prep"]["parent"], "fd")
        for f in ("card.md", "qa.md", "coverage.md"):
            p = ms_dir(st, a.id) / f
            if p.exists():
                p.rename(p.with_name(f"{p.stem}.dropped-{int(time.time())}{p.suffix}"))
    print(f"{a.id} dropped（编号不复用：用新编号重新计划）")
    cli._after(tdir)


def cmd_revoke(a):
    """Take back the latest closed milestone (it went the wrong way): the integration branch returns to its parent,
    its local tag goes, and the id can be planned again. A pushed copy is replaced by the next push, with the user's
    approval and only while the remote still holds exactly the revoked commit and tag (force-with-lease)."""
    tdir = task_dir_of(a.task)
    st = State(tdir)
    m = _get(st, a.id)
    if m["status"] != "done":
        raise MfError(f"{a.id} is {m['status']}: only a closed milestone can be revoked (while closing: mf ms drop)")
    if st.last_ms["id"] != a.id:
        raise MfRefused(f"只能撤销最近收尾的里程碑（现在是 {st.last_ms['id']}）：后面的里程碑建在它上面，先撤销后面的")
    if st.open_ms:
        raise MfRefused(f"{st.open_ms['id']} 正在收尾：先收尾或 mf ms drop")
    close = m["close"]
    if st.freeze and st.freeze["t"] > close["t"]:
        raise MfRefused("冻结基线是在这个里程碑之后建立的：撤销会让基线和复核数字失效。先和用户确认，再撤销并重新冻结")
    repo, br = _repo(st), int_branch(st)
    if ref_sha(repo, f"refs/heads/{br}") != close["commit"]:
        raise MfError(f"integration branch {br} is not at {a.id} ({close['commit'][:10]}): moved outside mf")
    bwt = Path(st.bootstrap["wt"]) if st.bootstrap and m["prep"].get("bootstrap") and not st.freeze else None
    if bwt and (bwt / ".git").exists():
        dirty = [f for f in uncommitted_files(bwt) if not match(f, _ignore(st))]
        if dirty and not a.discard:
            raise MfRefused(f"bootstrap worktree 有未提交的改动（{', '.join(dirty[:5])}）：撤销会把它重置到 {close['parent'][:10]}。"
                            "先提交或保存，确认可以丢弃时加 --discard")
    tag = close["tag"]
    tag_obj = git(["rev-parse", "--verify", "--quiet", f"refs/tags/{tag}"], repo, check=False) or None
    update_branch(repo, br, close["parent"], close["commit"])
    try:
        append_event(tdir, "ms.revoke", a.why, by="agent", id=a.id, commit=close["commit"], parent=close["parent"],
                     tag=tag, tag_obj=tag_obj)
    except Exception:
        update_branch(repo, br, close["commit"], close["parent"])
        raise
    delete_tag(repo, tag)  # the tag name is reused when the id closes again
    if bwt and (bwt / ".git").exists():
        git(["reset", "-q", "--hard", close["parent"]], bwt)
    d = ms_dir(st, a.id)
    old = [p for p in d.iterdir() if p.is_file()]
    if old:  # a new card / Q&A must start from scratch, not from the revoked one
        arch = d / f"revoked-{int(time.time())}"
        arch.mkdir()
        for p in old:
            p.rename(arch / p.name)
    pushed = m.get("pushed")
    print(f"↩ {a.id} 已撤销：集成分支回到 {close['parent'][:10]}，本地 tag {tag} 已删除"
          + (f"，bootstrap worktree 已重置" if bwt else "") + f"；旧卡片等归档在 {d.name}/revoked-*")
    print(f"  重新计划同一编号：{cli.MF} ms plan {st.id} {a.id} --type {m.get('type')} --title ...")
    if pushed:
        print(f"  远端仍是旧的 {a.id}（{close['commit'][:10]}）：新的 {a.id} 收尾后推送会要求你批准覆盖（仅当远端仍是这个提交和 tag）")
    cli._after(tdir)


def show_text(st: State, mid: str) -> str:
    """One milestone (`mf status <task> M3`): plan, candidate, numbers, review, and the card as committed."""
    m = _get(st, mid)
    out = [render.ms_text(st, m)]
    d = ms_dir(st, mid)
    # a closed milestone: show what was committed (and pushed), whose Next section mf generated at close
    src = d / "commit-msg.txt" if m["status"] == "done" and (d / "commit-msg.txt").is_file() else d / "card.md"
    if src.is_file():
        out.append("\n" + src.read_text(encoding="utf-8").strip())
    return "\n".join(out)


# ---------------------------------------------------------------- entry document


def _fill_entry(st: State, path: Path, current=None, nxt=None) -> bool:
    text = path.read_text(encoding="utf-8")
    new = render.fill_blocks(text, render.entry_blocks(st, current, nxt))
    if new != text:
        path.write_text(new, encoding="utf-8")
        return True
    return False
