"""Renderers. One source of truth (events) -> three generated views:

L0 overview   `mf status` / index.html          one line per task: phase, health, best verified, budget
L1 task       `mf status <task>` / STATUS.md / dashboard  stepper, perf chart, lanes, briefs, alerts, hypotheses
L2 attempt    `mf status <task> A<n>` / <details>  per-case numbers, commit, correctness, gates, links
L3 raw        runs/*/out.log, diff.patch, events.jsonl
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

from myflow import guard
from myflow.core import (MS_TERMINAL, PASS, PHASE_ZH, PHASES, VALID_VERDICTS, MfError, State, all_task_dirs,
                         atomic_write, home)
from myflow.gitops import github_web, is_ancestor
from myflow.runner import combine_perf, parse_perf

ICON = {"crit": "✖", "warn": "⚠", "ask": "?", "info": "·", "ok": "●"}
LANE_COLORS = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e", "#17becf", "#8c564b", "#e377c2",
               "#7f7f7f", "#bcbd22"]
STATUS_ZH = {"pass": "通过", "fail_correctness": "正确性失败", "fail_setup": "构建失败", "timeout": "超时",
             "scope_violation": "越界改动", "oracle_modified": "改了oracle", "gate_failed": "门禁失败",
             "bench_error": "bench出错", "incomplete": "case缺失", "improved": "有提升", "no_gain": "无显著变化",
             "regression": "变慢", "error": "出错"}


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


# ---------------------------------------------------------------- markdown (stdlib only)
# Cards, notes and entry documents are Markdown. Commit messages are shown as plain text by GitHub (md_text);
# mf serve / dashboards render them (md_html). Only the common subset is supported; HTML in the source is escaped.

_MD_DETAILS = re.compile(r"^\s*(<details>)?\s*(?:<summary>(.*?)</summary>)?\s*$")
_MD_DETAILS_END = re.compile(r"^\s*</details>\s*$")
_MD_FENCE = re.compile(r"^\s*(```|~~~)")
_MD_LIST = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_MD_HEAD = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_MD_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
_MD_COMMENT = re.compile(r"<!--.*?-->", re.S)


def _md_inline(s: str) -> str:
    """Inline Markdown on raw text: code spans, links, bare URLs, **bold**. Everything else is escaped.
    (`__x__` is not bold: it would mangle identifiers such as __init__.py.)"""
    keep = []

    def stash(h: str) -> str:
        keep.append(h)
        return f"\x00{len(keep) - 1}\x00"

    def unstash(x: str) -> str:
        return re.sub(r"\x00(\d+)\x00", lambda m: keep[int(m.group(1))], x)

    def bold(x: str) -> str:
        return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", x)

    s = re.sub(r"`([^`]+)`", lambda m: stash(f"<code>{esc(m.group(1))}</code>"), s)

    def link(m):
        text, url = m.group(1), m.group(2).strip()
        if re.match(r"^(https?://|#|\.{0,2}/|[\w.%-]+(/|\.|#|$))", url) and not url.lower().startswith("javascript:"):
            return stash(f"<a href='{esc(url)}'>{unstash(bold(esc(text)))}</a>")
        return stash(unstash(esc(m.group(0))))

    s = re.sub(r"!?\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)", link, s)
    s = re.sub(r"https?://[^\s<>()（）\x00]+[^\s<>()（）.,;:!?，。；：！？\x00]",
               lambda m: stash(f"<a href='{esc(m.group(0))}'>{esc(m.group(0))}</a>"), s)
    return unstash(bold(esc(s)))


def _slug(text: str, seen: dict) -> str:
    """GitHub-style heading anchor, so in-document links (#…) work the same locally."""
    plain = re.sub(r"[`*_\[\]]|\(([^)]*)\)", "", text) if "](" in text else re.sub(r"[`*]", "", text)
    s = re.sub(r"[^\w\- ]", "", plain.lower()).replace(" ", "-")
    n = seen.get(s, 0)
    seen[s] = n + 1
    return s if n == 0 else f"{s}-{n}"


def md_html(text: str) -> str:
    lines = _MD_COMMENT.sub("", text or "").splitlines()
    out, para, seen, i = [], [], {}, 0

    def flush():
        if para:
            out.append("<p>" + "<br>".join(_md_inline(x) for x in para) + "</p>")
            para.clear()

    def cells(row: str) -> list:
        row = row.strip()
        row = row[1:] if row.startswith("|") else row
        row = row[:-1] if row.endswith("|") and not row.endswith("\\|") else row
        return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", row)]

    while i < len(lines):
        ln = lines[i]
        if _MD_FENCE.match(ln):
            flush()
            fence, body = ln.strip()[:3], []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(fence):
                body.append(lines[i])
                i += 1
            out.append("<pre><code>" + esc("\n".join(body)) + "</code></pre>")
            i += 1
            continue
        if not ln.strip():
            flush()
            i += 1
            continue
        d = _MD_DETAILS.match(ln)
        if d and (d.group(1) or d.group(2) is not None):  # the only HTML let through: a fold on its own lines
            flush()
            out.append(("<details>" if d.group(1) else "") + (f"<summary>{_md_inline(d.group(2))}</summary>"
                                                              if d.group(2) is not None else ""))
            i += 1
            continue
        if _MD_DETAILS_END.match(ln):
            flush()
            out.append("</details>")
            i += 1
            continue
        h = _MD_HEAD.match(ln)
        if h:
            flush()
            n = len(h.group(1))
            out.append(f"<h{n} id='{esc(_slug(h.group(2), seen))}'>{_md_inline(h.group(2))}</h{n}>")
            i += 1
            continue
        if re.match(r"^\s*([-*_])(\s*\1){2,}\s*$", ln):
            flush()
            out.append("<hr>")
            i += 1
            continue
        if "|" in ln and i + 1 < len(lines) and _MD_TABLE_SEP.match(lines[i + 1]):
            flush()
            rows = ["<tr>" + "".join(f"<th>{_md_inline(c)}</th>" for c in cells(ln)) + "</tr>"]
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append("<tr>" + "".join(f"<td>{_md_inline(c)}</td>" for c in cells(lines[i])) + "</tr>")
                i += 1
            out.append("<table>" + "".join(rows) + "</table>")
            continue
        if ln.lstrip().startswith(">"):
            flush()
            quote = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                quote.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            out.append("<blockquote>" + md_html("\n".join(quote)) + "</blockquote>")
            continue
        if _MD_LIST.match(ln):
            flush()
            stack = []  # (indent, tag)
            while i < len(lines) and lines[i].strip():
                m = _MD_LIST.match(lines[i])
                if not m:
                    if lines[i][:1].isspace() and out[-1].endswith("</li>"):  # indented: continues the item
                        out[-1] = out[-1][:-5] + "<br>" + _md_inline(lines[i].strip()) + "</li>"
                        i += 1
                        continue
                    break  # an unindented line (the next "**label**:" of a card) ends the list
                ind, tag = len(m.group(1).expandtabs(4)), "ol" if m.group(2)[0].isdigit() else "ul"
                while stack and ind < stack[-1][0]:
                    out.append(f"</{stack.pop()[1]}>")
                if stack and ind == stack[-1][0] and tag != stack[-1][1]:
                    out.append(f"</{stack.pop()[1]}>")
                if not stack or ind > stack[-1][0]:
                    out.append(f"<{tag}>")
                    stack.append((ind, tag))
                out.append(f"<li>{_md_inline(m.group(3))}</li>")
                i += 1
            while stack:
                out.append(f"</{stack.pop()[1]}>")
            continue
        para.append(ln.strip())
        i += 1
    flush()
    return "<div class='md'>" + "\n".join(out) + "</div>"


def md_text(text: str) -> str:
    """Markdown -> plain text for places that show it raw (git commit messages on GitHub)."""
    out, in_fence = [], False
    for ln in _MD_COMMENT.sub("", text or "").splitlines():
        if _MD_FENCE.match(ln):
            in_fence = not in_fence
            continue
        if in_fence:
            out.append("    " + ln)
            continue
        if _MD_TABLE_SEP.match(ln):
            continue
        h = _MD_HEAD.match(ln)
        if h:
            ln = h.group(2)
        ln = re.sub(r"^(\s*)>\s?", r"\1", ln)
        ln = re.sub(r"!?\[([^\]]*)\]\((https?://[^)\s]+)\)", r"\1 <\2>", ln)
        ln = re.sub(r"!?\[([^\]]*)\]\(([^)\s]+)\)", r"\1（\2）", ln)
        ln = re.sub(r"\*\*(.+?)\*\*", r"\1", ln)
        out.append(ln.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def fx(v, nd=3) -> str:
    return f"{v:.{nd}f}×" if isinstance(v, (int, float)) else "—"


def age(sec: float) -> str:
    sec = max(0, sec)
    if sec < 90:
        return f"{int(sec)}s"
    if sec < 5400:
        return f"{int(sec / 60)}m"
    if sec < 3 * 86400:
        return f"{sec / 3600:.1f}h"
    return f"{sec / 86400:.0f}d"


def ago(sec) -> str:
    """'12m 前', or '从未' when it never happened (None)."""
    return "从未" if sec is None else f"{age(sec)} 前"


def pct(x) -> str:
    x = (x or 0) * 100
    return f"±{x:.2f}%" if x < 1 else f"±{x:.1f}%"


def gpu_txt(g) -> str:
    return "CPU" if g is None or g == "-" else f"GPU{g}"


def hm(t) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(t)) if t else "—"


def bar(frac, width=8) -> str:
    if frac is None:
        return "░" * width
    n = int(round(frac * width))
    return "█" * n + "░" * (width - n)


def dw(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def pad(s: str, w: int) -> str:
    s = str(s)
    while dw(s) > w:
        s = s[:-2] + "…" if len(s) > 1 else s
        if dw(s) <= w:
            break
    return s + " " * max(0, w - dw(s))


def stall_txt(st: State) -> str:
    w = st.worst_lane
    mx = st.task["budget"].get("max_no_improve")
    return f"最停滞 lane {w['name']} {w['no_improve']}/{mx}" if w else f"无提升上限 {mx}/lane"


def lane_color(st: State, lane) -> str:
    names = list(st.lanes)
    return LANE_COLORS[names.index(lane) % len(LANE_COLORS)] if lane in names else "#555"


def shown_phases(st: State) -> list:
    """Phases to draw: the bootstrap step only exists for new operators."""
    used = st.bootstrap or st.phase == "bootstrap" or any(h["phase"] == "bootstrap" for h in st.phase_hist)
    return [p for p in PHASES if p != "bootstrap" or used]


def phase_label(st: State) -> str:
    ps = shown_phases(st)
    i = ps.index(st.phase) + 1 if st.phase in ps else 0
    r = f"R{st.round} " if st.round > 1 or st.phase in ("plan", "implement", "verify", "integrate") else ""
    return f"{r}{st.phase} {i}/{len(ps) - 1}"


def summary(st: State, now=None) -> dict:
    now = now or time.time()
    al = guard.alerts(st, now)
    return {"id": st.id, "title": st.task.get("title", ""), "phase": phase_label(st), "health": guard.health(al),
            "alerts": al, "needs": guard.needs_you(al),
            "best_v": st.best_verified["speedup"] if st.best_verified else None,
            "best_s": st.best_self["speedup"] if st.best_self else None, "target": st.target,
            "progress": st.progress(), "attempts": len(st.attempts),
            "max_attempts": st.task["budget"].get("max_attempts"),
            "lanes_active": sum(1 for x in st.lanes.values() if x["status"] == "active"), "lanes": len(st.lanes),
            "last": now - st.last_agent_t, "parent": st.task.get("parent"),
            "last_report": now - st.last_report_t if st.last_report_t else None,
            "now": (st.now or {}).get("text", ""),
            "ms_done": len(st.closed_ms), "ms_total": sum(1 for m in st.milestones.values() if m["status"] != "dropped"),
            "ms_next": next_label(st)}


# ---------------------------------------------------------------- milestones (v2)

TYPE_ZH = {"feature": "功能", "perf": "性能", "analysis": "分析", "maint": "维护"}


def next_label(st: State, exclude=None) -> str:
    n = st.next_ms()
    if exclude and isinstance(n, dict) and n.get("id") == exclude:
        n = next((m for m in st.milestones.values() if m["status"] == "planned" and m["id"] != exclude
                  and m.get("commitment") == "committed"), None)
    if n is None:
        return ""
    if isinstance(n, str):
        return {"done": "完成", "report": "写报告", "stop": "停止", "await-user": "等你决定"}.get(n, n)
    return f"{n['id']} {n.get('title', '')}"


def next_plan(st: State, nxt: list) -> str:
    """The upcoming work a closing milestone promises (its --next), then what else is planned, from the plan cards."""
    terms = {"done": "完成", "report": "写报告", "stop": "停止", "await-user": "等你决定"}
    out = [terms.get(x, x) if x in MS_TERMINAL else f"{x} {st.milestones[x].get('title', '')}" for x in nxt]
    later = [m for m in st.milestones.values() if m["status"] == "planned" and m["id"] not in nxt]
    if later and not any(x in MS_TERMINAL for x in nxt):
        out.append("之后：" + "；".join(f"{m['id']} {m.get('title', '')}"
                                     f"（{'承诺' if m.get('commitment') == 'committed' else '暂定'}）" for m in later))
    return "；".join(out)


def _verify_n(st: State, n):
    for v in st.verifies:
        if v.get("n") == n:
            return v
    return None


def ms_status(st: State, m: dict, current=None) -> str:
    s = m["status"]
    if s == "done":
        if not (st.task["git"].get("url") or "").strip():
            return "✅"
        return "✅ 已推送" if m.get("pushed") else "✅ 未推送"
    if s == "closing":
        return "✅ 本提交" if current and current.get("id") == m["id"] else "🔄 收尾中"
    if s == "dropped":
        return "✖ 放弃"
    if s == "revoked":
        return "↩ 已撤销"
    return "⏳ 承诺" if m.get("commitment") == "committed" else "· 暂定"


def _ms_cells(st: State, m: dict, current=None) -> dict:
    prep = m.get("prep") or {}
    inc, cum = prep.get("inc"), prep.get("cum")
    inc_txt = "—"
    if inc and not (cum and inc.get("v") == cum.get("v")):
        inc_txt = f"{fx(inc['speedup'])} 对 {inc.get('against_ms')}"
    elif inc:
        inc_txt = f"{fx(inc['speedup'])} 对基线"
    if m.get("expect"):
        inc_txt += f"（预期 {m['expect']:g}×）"
    roles = m.get("roles") or {}
    if not (roles.get("verifier") or roles.get("tests") or roles.get("writer")):  # the review (or nothing yet)
        rv = roles.get("review")
        cold, pending = ("✓" if rv else "—"), not rv
        # close re-runs the frozen correctness on the final commit and keeps the commit only if it passes
        tests = "✓" if m.get("close") or (current and current.get("id") == m["id"] and rv) else "—"
    else:  # milestones closed with the earlier three roles
        v, tr = roles.get("verifier"), roles.get("tests")
        cold = "—" if not v else ("冷读✓" if v.get("result") == "pass" else "冷读✗") + \
            (f" {v['minutes']:g}分" if v.get("minutes") else "")
        tests, pending = ("—" if not tr else ("✓" if tr.get("result") == "pass" else "✗")), not v
    if current and current.get("id") == m["id"] and pending:
        cold = tests = "本提交核对"  # rendered at prep, before the roles ran; the commit message carries the results
    tag = (m.get("close") or {}).get("tag", "")
    commit = (m.get("close") or {}).get("commit", "")
    return {"id": m["id"], "status": ms_status(st, m, current), "type": TYPE_ZH.get(m.get("type"), m.get("type", "")),
            "title": m.get("title", ""), "inc": inc_txt, "cum": fx(cum["speedup"]) if cum else "—", "cold": cold,
            "tests": tests, "tag": tag, "commit": commit,
            "lines": (prep.get("size") or {}).get("lines")}


def roadmap_md(st: State, current=None, nxt=None) -> str:
    web = github_web(st.task["git"].get("url"))
    rows = ["| 里程碑 | 状态 | 类型 | 标题 | 增量（预期） | 累计 | 用例 | 复核 | 提交 |", "|---|---|---|---|---|---|---|---|---|"]
    for m in st.milestones.values():
        if current and m["id"] == current.get("id"):
            m = current  # rendered at prep: show the milestone this commit will close
        c = _ms_cells(st, m, current)
        ref = c["tag"] or ("本提交" if c["status"].endswith("本提交") else "")
        if web and c["tag"] and m.get("pushed"):
            ref = f"[{c['tag']}]({web}/commit/{c['commit']})"
        rows.append(f"| {c['id']} | {c['status']} | {c['type']} | {_md_cell(c['title'])} | {c['inc']} | {c['cum']} | "
                    f"{c['tests']} | {c['cold']} | {ref} |")
    if nxt:  # rendered at close: what this commit promises
        rows += ["", f"下一步（本提交的承诺）：{next_plan(st, nxt)}"]
    else:
        label = next_label(st, exclude=(current or {}).get("id"))
        if label:
            rows += ["", f"下一步（{'计划' if current else '上次推送的承诺'}）：{label}"]
    return "\n".join(rows)


def roadmap_lines(st: State, current=None, nxt=None) -> str:
    """The milestone list of the entry document (user request 11-22: a design document, not a process log): one line
    per milestone; the commit holds what changed and why."""
    web = github_web(st.task["git"].get("url"))
    rows = []
    for m in st.milestones.values():
        if current and m["id"] == current.get("id"):
            m = current  # rendered at prep: show the milestone this commit will close
        c = _ms_cells(st, m, current)
        title = " ".join(c["title"].split())
        parts = [f"{c['id']} {c['status']}", c["type"], title if len(title) <= 40 else title[:39] + "…"]
        if (m.get("prep") or {}).get("inc"):
            parts.append(c["inc"])
        elif m.get("expect"):
            parts.append(f"预期 {m['expect']:g}×")
        if web and c["tag"] and m.get("pushed"):
            parts.append(f"[提交]({web}/commit/{c['commit']})")
        elif c["tag"]:
            parts.append(f"tag `{c['tag']}`")
        rows.append("- " + " · ".join(parts))
    if nxt:  # rendered at close: what this commit promises
        rows += ["", f"下一步（本提交的承诺）：{next_plan(st, nxt)}"]
    else:
        label = next_label(st, exclude=(current or {}).get("id"))
        if label:
            rows += ["", f"下一步（{'计划' if current else '上次推送的承诺'}）：{label}"]
    return "\n".join(rows)


def roadmap_text(st: State) -> str:
    if not st.milestones:
        return f"{st.id}: 还没有里程碑（mf ms plan {st.id} M1 --title ...）"
    L = [f"{st.id} 路线图（{len(st.closed_ms)} 个已完成）"]
    for m in st.milestones.values():
        c = _ms_cells(st, m)
        L.append(f"  {pad(c['id'], 5)} {pad(c['status'], 10)} {pad(c['type'], 4)} {pad(c['title'], 40)} "
                 f"增量 {pad(c['inc'], 28)} 累计 {pad(c['cum'], 8)} 复核 {pad(c['cold'], 9)} {c['tag']}")
    nxt = next_label(st)
    if nxt:
        L.append(f"  下一步：{nxt}")
    for r in st.revoked:
        L.append(f"  ↩ 已撤销 {r['id']} {str((r.get('close') or {}).get('commit', ''))[:10]}（{hm(r['t'])}）：{r.get('why', '')[:100]}")
    dirs = guard.direction(st)
    L.append("  方向检查：" + " ｜ ".join(f"{'✓' if d['level'] == 'ok' else ICON.get(d['level'], '·')} {d['name']}" for d in dirs))
    L += [f"    {ICON.get(d['level'], '·')} {d['name']}：{d['msg']}" for d in dirs if d["level"] != "ok"]
    return "\n".join(L)


def ms_text(st: State, m: dict) -> str:
    prep, close = m.get("prep") or {}, m.get("close") or {}
    L = [f"{m['id']} · {m.get('title', '')} · {TYPE_ZH.get(m.get('type'), m.get('type'))} · {ms_status(st, m)}",
         f"  计划：验收 {m.get('accept') or '—'} · 预期 {m.get('expect') or '—'}× · 依据 {m.get('basis') or '—'} · "
         f"目标 {', '.join(m.get('goals') or []) or '—'} · 约 {m.get('est_h') or '—'}h"]
    if prep:
        who = (f"A{prep['attempt']} [{prep.get('lane')}]" if prep.get("attempt") else f"[{prep['lane']}] 头部" if prep.get("lane")
                   else "bootstrap" if prep.get("bootstrap") else "仅文档/用例")
        L.append(f"  候选 {who} {prep['candidate'][:10]} · 父 {prep['parent'][:10]} · 运行时树 {prep['runtime_tree']} · "
                 f"改动 {(prep.get('size') or {}).get('lines', 0)} 行" + (f"（超预算：{prep['size_why']}）" if prep.get("size_why") else ""))
        for k, label in (("regress_why", "接受回退"), ("verify_why", "接受不一致的复核")):
            if prep.get(k):
                L.append(f"  {label}：{prep[k]}")
        for k, label in (("inc", "增量"), ("cum", "累计")):
            x = prep.get(k)
            if x:
                L.append(f"  {label} {fx(x['speedup'])} {x['verdict']} V{x['v']} ABBA {x['pairs']} 对 {gpu_txt(x.get('gpu'))}")
    for r in ("tests", "writer", "verifier", "review"):
        rec = (m.get("roles") or {}).get(r)
        if rec:
            L.append(f"  {r}: {rec.get('result')} · 会话 {rec.get('session')} · 改 {len(rec.get('changed') or [])} 个文件"
                     + (f" · {rec['minutes']:g} 分钟" if rec.get("minutes") else "") + f" · {rec.get('summary', '')}")
    if close:
        L.append(f"  提交 {close['commit'][:10]} tag {close['tag']} · 下一步 {', '.join(close.get('next') or [])}"
                 + (f" · 变更原因 {close['why']}" if close.get("why") else ""))
    if m.get("drop"):
        L.append(f"  放弃：{m['drop']['reason']}")
    return "\n".join(L)


def entry_blocks(st: State, current=None, nxt=None) -> dict:
    """Generated blocks of the entry document: numbers come only from mf records. nxt: the --next of a closing milestone."""
    done = list(st.closed_ms) + ([current] if current and current.get("status") == "closing" else [])
    seq = (st.freeze or {}).get("seq")  # a speedup against an earlier freeze is not against this base
    cums = [(m.get("prep") or {}).get("cum") for m in done if (m.get("prep") or {}).get("freeze_seq") in (None, seq)]
    best = max((c["speedup"] for c in cums if c), default=None)
    label = next_plan(st, nxt) if nxt else next_label(st, exclude=(current or {}).get("id"))
    n_done = len(st.closed_ms) + (1 if current else 0)
    summary_txt = (f"> 由 mf 生成（{hm(time.time())}）· 目标：{st.task.get('goal', '')}（{st.target:g}× 相对冻结基线）· "
                   f"已完成 {n_done} 个里程碑" + (f"（含本提交 {current['id']}）" if current else "")
                   + f" · 最佳累计 {fx(best)}" + (f" · 下一步：{label}" if label else ""))
    perf = ["（尚无复核数据）"]
    latest = next((m for m in reversed(done) if (m.get("prep") or {}).get("cum")), None)
    fz = st.freeze or {}
    lc = ((latest or {}).get("close") or {}).get("commit")
    if (latest and fz and lc and latest["prep"].get("freeze_seq") not in (None, fz.get("seq"))
            and (lc == fz.get("base") or is_ancestor(Path(st.task["repo"]), lc, fz.get("base")))):
        # re-frozen after the latest perf milestone (the measurement changed, e.g. a finer bench): its verify used
        # the old bench; the freeze runs are the current code on the new bench
        cases = case_order(fz.get("baseline") or {})
        heads, extra, legend = perf_cols(st, cases, {}, freeze_perf(st), ("基线", "当前"))
        perf = [f"最近的性能里程碑 {latest['id']}：相对当时的冻结基线 {fx(latest['prep']['cum'].get('speedup'))}"
                f"（mf 复核 V{latest['prep']['cum'].get('v')}，旧 bench）。之后 bench 改了（重新冻结于 `{fz['base'][:10]}`，"
                f"含 {latest['id']} 的代码）：下表是当前代码在新 bench 上的逐 case 数据。", "",
                f"<details><summary>逐 case（{len(cases)} 个，mf 冻结 {fz.get('repeats') or '—'} 次中位，当前代码）</summary>", "",
                "| case | 当前 µs |" + "".join(f" {h} |" for h in heads), "|---" * (2 + len(heads)) + "|"]
        perf += [f"| {c} | {_num(fz['baseline'][c])} |" + "".join(f" {x} |" for x in extra[c]) for c in cases]
        perf += (["", legend] if legend else []) + ["", "</details>"]
        latest = None
    if latest:
        cum = _verify_n(st, latest["prep"]["cum"]["v"]) or {}
        inc_s = latest["prep"].get("inc") or {}
        inc = _verify_n(st, inc_s.get("v")) if inc_s.get("v") != latest["prep"]["cum"]["v"] else None
        vs = f"，相对 {inc_s.get('against_ms') or '上一里程碑'} {fx(inc_s.get('speedup'))}" if inc else ""
        cases = [(c, cum["per_case"][c]) for c in case_order(cum.get("per_case") or {})]
        pa, pb = verify_perf(st, cum)
        heads, extra, legend = perf_cols(st, [c for c, _ in cases], pa, pb, ("基线", "当前"))
        # the per-case numbers stay verifiable but folded: the design document states the conclusion per tier
        perf = [f"最近的性能里程碑 {latest['id']}：相对冻结基线 {fx(cum.get('speedup'))}{vs}（mf 复核 V{cum.get('n')}："
                f"同卡 ABBA {cum.get('pairs')} 对，{gpu_txt(cum.get('gpu'))}，噪声 {pct(cum.get('noise'))}）。", "",
                f"<details><summary>逐 case（{len(cases)} 个，mf 生成）</summary>", "",
                "| case | 基线 | 当前 | 累计加速 | 增量加速 |" + "".join(f" {h} |" for h in heads),
                "|---" * (5 + len(heads)) + "|"]
        for c, d in cases:
            ic = ((inc or {}).get("per_case") or {}).get(c, {}).get("speedup")
            perf.append(f"| {c} | {_num(d.get('a'))} | {_num(d.get('b'))} | {fx(d.get('speedup'))} | {fx(ic) if inc else '—'} |"
                        + "".join(f" {x} |" for x in extra[c]))
        perf += (["", legend] if legend else []) + ["", "</details>"]
    src = baseline_source(st)
    if src:
        perf = [src, ""] + perf
    return {"summary": summary_txt, "roadmap": roadmap_lines(st, current, nxt), "perf": "\n".join(perf)}


def baseline_source(st: State) -> str:
    """Where the frozen baseline numbers come from, from mf's own records (the comparison reference is hand-written)."""
    fz = st.freeze or {}
    if not fz:
        return ""
    base = str(fz.get("base", ""))
    owner = next((m for m in st.closed_ms if (m.get("close") or {}).get("commit") == base), None)
    who = f"（里程碑 {owner['id']}，tag `{owner['close']['tag']}`）" if owner else ""
    bench = " ".join(str(st.task["commands"].get("bench") or "").split())
    bench = bench if len(bench) <= 160 else bench[:157] + "…"
    return (f"基线来源（mf 记录）：{hm(fz.get('t'))} 冻结于提交 `{base[:10]}`{who}，bench 跑 {fz.get('repeats') or '—'} 次取中位数，"
            f"{gpu_txt(fz.get('gpu'))}，噪声 {pct(fz.get('noise'))}；bench 命令 `{bench}`。"
            "对照实现与精度参考的来源见本节手写部分。")


def _num(x) -> str:
    return f"{x:g}" if isinstance(x, (int, float)) else "—"


def case_order(cases) -> list:
    """Cases in natural order (tp2_m1, tp2_m16, tp2_m128 ...), not as strings."""
    return sorted(cases, key=lambda c: [(0, int(t), "") if t.isdigit() else (1, 0, t) for t in re.split(r"(\d+)", c)])


def sweep_gaps(cases) -> list:
    """Size series in case names (the last number: tp2_m1, tp2_m16 ...) that skip powers of two between their
    smallest and largest size. Performance data is the main deliverable (user request 11-23): a bench sweeps its main
    size dimension in powers of two, e.g. batch 1, 2, 4 ... 32768."""
    groups = {}
    for c in cases:
        parts = re.split(r"(\d+)", c)
        nums = [i for i, s in enumerate(parts) if s.isdigit()]
        if nums:
            i = nums[-1]
            groups.setdefault("".join(parts[:i]) + "*" + "".join(parts[i + 1:]), set()).add(int(parts[i]))
    out = []
    for key, sizes in sorted(groups.items()):
        lo, hi = min(sizes), max(sizes)
        if len(sizes) < 2 or lo < 1:
            continue
        miss = [1 << k for k in range(hi.bit_length()) if lo <= 1 << k <= hi and 1 << k not in sizes]
        if miss:
            out.append(f"{key} 缺 {', '.join(map(str, miss[:8]))}{' …' if len(miss) > 8 else ''}")
    return out


# ---------------------------------------------------------------- throughput (display only, from raw logs)

_PERF_CACHE: dict = {}


def _log_perf(st: State, log) -> dict:
    p = Path(str(log))
    cfg = st.task.get("perf") or {}
    try:
        key = (str(p), p.stat().st_mtime_ns, json.dumps(cfg, sort_keys=True))
    except OSError:
        return {}
    if key not in _PERF_CACHE:
        try:
            _PERF_CACHE[key] = parse_perf(p.read_text(encoding="utf-8", errors="replace"), cfg)
        except (OSError, ValueError, re.error, MfError):
            _PERF_CACHE[key] = {}
    return _PERF_CACHE[key]


def perf_of(st: State, logs) -> dict:
    """Per-case throughput over the raw bench logs of one implementation (median over runs)."""
    return combine_perf([_log_perf(st, x) for x in logs or []])


def verify_perf(st: State, v: dict) -> tuple:
    """(reference arm, candidate) throughput of a verify, re-derived from the raw logs of its paired runs."""
    try:
        arms = json.loads((Path(v["run"]) / "abba.json").read_text())["arms"]
    except (OSError, ValueError, KeyError, TypeError):
        return {}, {}
    ref = "P" if v.get("arms") == 3 and v.get("against") else "A"
    return tuple(perf_of(st, [x["log"] for x in arms if x.get("arm") == k]) for k in (ref, "B"))


def freeze_perf(st: State) -> dict:
    run = (st.freeze or {}).get("run")
    return perf_of(st, sorted(Path(run).glob("bench*/out.log"))) if run else {}


def _rate(x) -> str:
    return f"{x:.2f}" if x < 10 else f"{x:.1f}" if x < 100 else f"{x:.0f}"


def tput(x, peak=None) -> str:
    """Throughput with its share of the device peak, e.g. '1.70 (32.1%)'."""
    if not isinstance(x, (int, float)):
        return "—"
    return _rate(x) + (f" ({x / peak * 100:.1f}%)" if peak else "")


def perf_cols(st: State, cases, base: dict, new: dict, labels=("基线", "新")) -> tuple:
    """Extra table columns for every implementation compared: the reference's time and ratio, then TB/s and TFLOPS
    of the baseline, the candidate and the reference. Returns (headers, {case: cells}, legend); only columns some
    case has data for (benches print bytes= / flops= / MF_REF, or perf.extract reads them)."""
    pk = st.task.get("perf") or {}
    tb, tf = pk.get("peak_tbps"), pk.get("peak_tflops")
    b, n = labels
    spec = [("对照 µs", lambda c: new.get(c, {}).get("ref_us"), lambda x: f"{x:.2f}"),
            (f"{n}/对照", lambda c: new.get(c, {}).get("ratio"), lambda x: f"{x:.3f}"),
            (f"{b} TB/s", lambda c: base.get(c, {}).get("tbps"), lambda x: tput(x, tb)),
            (f"{n} TB/s", lambda c: new.get(c, {}).get("tbps"), lambda x: tput(x, tb)),
            ("对照 TB/s", lambda c: new.get(c, {}).get("ref_tbps"), lambda x: tput(x, tb)),
            (f"{b} TFLOPS", lambda c: base.get(c, {}).get("tflops"), lambda x: tput(x, tf)),
            (f"{n} TFLOPS", lambda c: new.get(c, {}).get("tflops"), lambda x: tput(x, tf)),
            ("对照 TFLOPS", lambda c: new.get(c, {}).get("ref_tflops"), lambda x: tput(x, tf))]
    spec = [s for s in spec if any(isinstance(s[1](c), (int, float)) for c in cases)]
    cells = {c: [fmt(get(c)) if isinstance(get(c), (int, float)) else "—" for _, get, fmt in spec] for c in cases}
    if not spec:
        return [], cells, ""
    impl = next((d["ref_impl"] for d in new.values() if d.get("ref_impl")), None)
    peaks = "、".join(x for x in (f"带宽 {tb:g} TB/s" if tb else "", f"算力 {tf:g} TFLOPS" if tf else "") if x)
    legend = ("TB/s、TFLOPS = bench 给出的每次调用数据量 / 运算量 ÷ 中位耗时" + (f"，括号内占峰值（{peaks}）" if peaks else "")
              + (f"；对照 = {impl}（MF_REF，同一 bench 进程、同一协议）" if impl else ""))
    return [s[0] for s in spec], cells, legend


def perf_line(st: State, perf: dict, limit: int = 6) -> str:
    """Console view of one implementation's throughput: the cases slowest against the reference first."""
    pk = st.task.get("perf") or {}
    items = sorted(perf.items(), key=lambda kv: (-(kv[1].get("ratio") or 0), kv[0]))[:limit]
    parts = []
    for c, d in items:
        x = [c] + ([f"{d['ratio']:.3f}×对照"] if d.get("ratio") else [])
        x += [f"{tput(d[k], pk.get(p))} {u}" for k, p, u in (("tbps", "peak_tbps", "TB/s"), ("tflops", "peak_tflops", "TFLOPS"))
              if isinstance(d.get(k), (int, float))]
        if len(x) > 1:
            parts.append(" ".join(x))
    return " | ".join(parts)


def fill_blocks(text: str, blocks: dict) -> str:
    import re
    for name, body in blocks.items():
        pat = re.compile(r"(<!-- mf:%s -->)(.*?)(<!-- /mf:%s -->)" % (name, name), re.S)
        text = pat.sub(lambda m_: f"{m_.group(1)}\n{body}\n{m_.group(3)}", text)
    return text


# ---------------------------------------------------------------- terminal (L0)


def _c(code: str, s: str) -> str:
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return s
    return f"\033[{code}m{s}\033[0m"


HCOLOR = {"crit": "31", "warn": "33", "ok": "32", "ask": "34"}


def term_overview(states: list) -> str:
    now = time.time()
    rows = [summary(st, now) for st in states]
    order = {r["id"]: i for i, r in enumerate(rows)}
    rows.sort(key=lambda r: (order.get(r["parent"], order[r["id"]]), r["parent"] is not None, order[r["id"]]))
    head = f"myflow · {time.strftime('%Y-%m-%d %H:%M')} · {len(rows)} task(s)"
    needs = sum(len(r["needs"]) for r in rows)
    if needs:
        head += _c("34", f" · {needs} item(s) need you")
    cols = [("", 1), ("TASK", 18), ("PHASE", 16), ("BEST✔", 8), ("SELF", 8), ("TARGET", 7), ("PROGRESS", 13),
            ("TRIES", 7), ("LANES", 5), ("MS", 5), ("LAST", 5), ("TOP ALERT", 48)]
    lines = [head, " ".join(pad(n, w) for n, w in cols)]
    for r in rows:
        top = r["needs"][0] if r["needs"] else (r["alerts"][0] if r["alerts"] else None)
        alert = f"{ICON[top['level']]} {top['msg']}" if top else ""
        prog = f"{bar(r['progress'])} {int(r['progress'] * 100)}%" if r["progress"] is not None else bar(None)
        name = ("└ " if r["parent"] else "") + r["id"]
        ms = f"{r['ms_done']}/{r['ms_total']}" if r["ms_total"] else "—"
        cells = [ICON["ok"], name, r["phase"], fx(r["best_v"]), fx(r["best_s"]), f"{r['target']:g}×", prog,
                 f"{r['attempts']}/{r['max_attempts']}", f"{r['lanes_active']}/{r['lanes']}", ms, age(r["last"]), alert]
        txt = " ".join(pad(c, w) for c, (_, w) in zip(cells, cols))
        lines.append(_c(HCOLOR[r["health"]], txt[:1]) + txt[1:])
        if r["now"]:
            lines.append(_c("2", f"    ↳ {r['now'][:100]} · 最后向你汇报 {ago(r['last_report'])}"))
        if r["ms_next"]:
            lines.append(_c("2", f"    ⏭ 下一步：{r['ms_next'][:100]}"))
    if not rows:
        lines.append("(no tasks yet: mf new <id> --repo <path>)")
    return "\n".join(lines)


# ---------------------------------------------------------------- markdown (L1) and attempt text (L2)


def _md_cell(s) -> str:
    return str(s if s is not None else "").replace("|", "\\|").replace("\n", " ")


def _by(who: str) -> str:
    return "按默认值·用户授权" if who == "default" else who


def defaults_lines(st: State) -> list:
    """Decisions taken by the defaults while the user was away (mf decide --delegate): listed until the next delegation."""
    dl, items = st.delegation, st.default_decisions()
    if not dl or not (items or st.delegated()):
        return []
    end = "进行中，" if st.delegated() else "已结束，"
    head = (f"按默认值代你决定（授权 {hm(dl['t'])} 起至 {hm(dl['until']) if dl['until'] else '—'}，{end}"
            f"原话：{dl['quote'][:60]}）")
    return [head] + [f"{hm(d['t'])} {('[' + d['gate'] + '] ') if d['gate'] else ''}{d['text']}" for d in items] + \
        ([] if items else ["（暂无）"])


def status_md(st: State) -> str:
    now = time.time()
    s = summary(st, now)
    t = st.task
    L = [f"# {st.id} · {t.get('title', '')}",
         f"> 目标：{t.get('goal', '')}  ",
         f"> 阶段：{phase_label(st)}（{PHASE_ZH.get(st.phase, '')}）｜ 健康：{ICON[s['health']]} {s['health']} ｜ "
         f"更新：{hm(now)} ｜ 最后活动：{age(s['last'])} 前 ｜ 最后向你汇报：{ago(s['last_report'])}  ",
         f"> 最近动作：{s['now'][:160]}", ""]
    if st.milestones:
        L += ["## 路线图（已完成的里程碑 = 已提交 + tag；数字由 mf 产生）", roadmap_md(st), ""]
        dirs = guard.direction(st, now)
        if dirs:
            L.append("方向检查：" + " ｜ ".join(f"{'✓' if d['level'] == 'ok' else ICON.get(d['level'], '·')} {d['name']}"
                                             + ("" if d["level"] == "ok" else f"：{d['msg']}") for d in dirs))
            L.append("")
    if s["alerts"]:
        L.append("## 需要关注")
        L += [f"- {ICON[a['level']]} **{a['level']}** {a['msg']}" for a in s["alerts"] if a["level"] != "info"]
        L += [f"- · {a['msg']}" for a in s["alerts"] if a["level"] == "info"]
        L.append("")
    dfl = defaults_lines(st)
    if dfl:
        L += [f"## {dfl[0]}"] + [f"- {x}" for x in dfl[1:]] + [""]
    fz = st.freeze or {}
    base = f"{len(fz.get('baseline', {}))} cases {pct(fz.get('noise'))} · {gpu_txt(fz.get('gpu'))} · " \
           f"{str(fz.get('base', ''))[:9]}" if fz else "未冻结"
    bv, bs = st.best_verified, st.best_self
    bv_who = f"A{bv.get('attempt')} {bv.get('lane')}" if bv else ""
    bs_who = f"A{bs.get('n')} {bs.get('lane')}" if bs else ""
    ceil = f"{st.ceiling['value']:g}×" if st.ceiling else "—"
    prog = f"{int(s['progress'] * 100)}%" if s["progress"] is not None else "—"
    L += ["## 性能（数字均由 mf 脚本产生）", "| 基线 | 最佳·已复核 | 最佳·自测(未配对) | 目标 | 上限估计 | 进度 |",
          "|---|---|---|---|---|---|",
          f"| {base} | **{fx(bv and bv['speedup'])}** {bv_who} | {fx(bs and bs['speedup'])} {bs_who} | {st.target:g}× | "
          f"{ceil} | {prog} |", ""]
    if bv:
        cs = case_order(bv.get("per_case") or {})
        heads, extra, legend = perf_cols(st, cs, *verify_perf(st, bv))
        if heads:
            L += [f"逐 case（最佳复核 V{bv.get('n')}，耗时 µs）", "",
                  "| case | 基线 | 新 | 加速 |" + "".join(f" {h} |" for h in heads), "|---" * (4 + len(heads)) + "|"]
            for c in cs:
                d = bv["per_case"][c]
                L.append(f"| {c} | {_num(d.get('a'))} | {_num(d.get('b'))} | {fx(d.get('speedup'))} |"
                         + "".join(f" {x} |" for x in extra[c]))
            L += ["", legend, ""]
    b = t["budget"]
    L.append(f"预算：尝试 {len(st.attempts)}/{b.get('max_attempts')} · 时间 {st.hours:.1f}/{b.get('max_hours')}h · "
             f"{stall_txt(st)}")
    L.append("")
    if st.briefs:
        L.append("## 最近 brief（agent 的阶段小结，新→旧）")
        for br in st.briefs[-6:][::-1]:
            ask = " **[待确认]**" if br["ask"] and (st.gates.get(_gk(br)) or {}).get("state") == "pending" else ""
            L.append(f"- {hm(br['t'])} [{br['phase']}{'/' + br['lane'] if br['lane'] else ''}]{ask} {br['text']}")
        L.append("")
    if st.lanes:
        L += ["## Lanes", "| lane | 方向 | 状态 | 尝试(通过) | 无提升 | 最佳自测 | 最佳复核 | 最后活动 |", "|---|---|---|---|---|---|---|---|"]
        for ln in st.lanes.values():
            n_ok = sum(1 for a in ln["attempts"] if a.get("status") == PASS)
            L.append(f"| {ln['name']} | {_md_cell(ln['direction'])} | {ln['status']}{(' — ' + _md_cell(ln['reason'])) if ln['reason'] else ''} | "
                     f"{len(ln['attempts'])}({n_ok}) | {ln['no_improve']} | {fx(ln['best_self'] and ln['best_self']['speedup'])} | "
                     f"{fx(ln['best_verified'] and ln['best_verified']['speedup'])} | {age(now - ln['t_last'])} |")
        L.append("")
    if st.attempts:
        vmap = {}
        for v in st.verifies:
            if not v.get("against"):  # this column is "vs frozen base"; incremental verifies live on the roadmap
                vmap[v.get("attempt")] = v
        L += ["## 最近尝试（新→旧）", "| # | lane | 想法 | 结果 | 自测 | 复核 | 提交 |", "|---|---|---|---|---|---|---|"]
        for a in st.attempts[-12:][::-1]:
            v = vmap.get(a.get("n"))
            vtxt = f"{fx(v.get('speedup'))} {STATUS_ZH.get(v.get('verdict'), v.get('verdict'))}" if v else ""
            L.append(f"| A{a.get('n')} | {a.get('lane')} | {_md_cell(a.get('idea', ''))[:70]} | "
                     f"{STATUS_ZH.get(a.get('status'), a.get('status'))} | {fx(a.get('speedup'))} | {vtxt} | "
                     f"{str(a.get('commit', ''))[:9]} |")
        L.append("")
    if st.hyps:
        L += ["## 假设台账", "| id | 假设 | 预测 | 状态 | 证据 |", "|---|---|---|---|---|"]
        for h in st.hyps.values():
            L.append(f"| {h['id']} | {_md_cell(h['text'])} | {_md_cell(h['predict'])} | {h['status']} | {_md_cell(h['evidence'])} |")
        L.append("")
    if st.decisions or st.changes:
        L.append("## 决策与契约变更")
        for d in st.decisions[-8:]:
            L.append(f"- {hm(d['t'])} 决策（{_by(d['by'])}）{('[' + d['gate'] + '] ') if d['gate'] else ''}{d['text']}")
        for c in st.changes[-8:]:
            L.append(f"- {hm(c['t'])} 变更（{c['by']}）`{c['key']}`: {c['old']} → {c['new']} {c['why']}")
        L.append("")
    L.append("详情：`dashboard.html`（或 `mf serve`）· L2：`mf status <task> A<n>` · 原始事件：`events.jsonl`")
    return "\n".join(L) + "\n"


def _gk(br) -> str:
    from myflow.core import gate_key
    return gate_key(br["phase"], br["round"])


def text_attempt(st: State, n: int) -> str:
    a = st.attempt(n)
    if not a:
        return f"no attempt A{n}"
    L = [f"A{n} [{a.get('lane')}] {a.get('kind', 'full')} · {STATUS_ZH.get(a.get('status'), a.get('status'))} · {hm(a['t'])}",
         f"  想法: {a.get('idea', '')}" + (f"   假设: {a['hyp']}" if a.get("hyp") else ""),
         f"  提交: {a.get('commit', '')}  改动: +{(a.get('diff') or {}).get('ins', 0)}/-{(a.get('diff') or {}).get('del', 0)} "
         f"{(a.get('diff') or {}).get('files', 0)} files {', '.join((a.get('diff') or {}).get('names', [])[:6])}"]
    for key in ("correctness", "bench"):
        r = a.get(key)
        if r:
            g = r.get("gate_before")
            gtxt = f" gate before/after={_gate_txt(g)}/{_gate_txt(r.get('gate_after'))}" if g else ""
            L.append(f"  {key}: rc={r.get('rc')} {r.get('dur')}s {gpu_txt(r.get('gpu'))}{gtxt} log={_rel(st, r.get('log'))}")
    if a.get("problems"):
        L.append("  问题: " + "; ".join(a["problems"][:8]))
    if a.get("per_case"):
        pb = perf_of(st, [(a.get("bench") or {}).get("log")] if (a.get("bench") or {}).get("log") else [])
        L.append(f"  自测加速比（对冻结基线，未配对）: {fx(a.get('speedup'))}")
        for c, s in sorted(a["per_case"].items(), key=lambda kv: kv[1]):
            bits = perf_line(st, {c: pb[c]}) if c in pb else ""
            L.append(f"    {pad(c, 40)} {fx(s)}  ({a.get('metrics', {}).get(c)} vs base {st.freeze['baseline'].get(c) if st.freeze else '?'})"
                     + (f"  {bits[len(c) + 1:]}" if bits else ""))
    for v in [v for v in st.verifies if v.get("attempt") == n]:
        what = f"增量 V{v.get('n')}（对 {v.get('against_ms') or str(v.get('against'))[:9]}）" if v.get("against") else f"复核 V{v.get('n')}"
        L.append(f"  {what}: {STATUS_ZH.get(v.get('verdict'), v.get('verdict'))} {fx(v.get('speedup'))} "
                 f"噪声{pct(v.get('noise'))} ABBA {v.get('pairs')} 对 {gpu_txt(v.get('gpu'))} {v.get('reason', '')}")
    if a.get("note"):
        L.append(f"  备注: {a['note']}")
    L.append(f"  运行目录: {_rel(st, a.get('run'))}")
    return "\n".join(L)


def _gate_txt(g) -> str:
    if not g:
        return "-"
    if g.get("ok") is None:
        return "?"
    return ("ok" if g["ok"] else "BUSY") + (f"({g.get('use'):.0f}%)" if isinstance(g.get("use"), float) else "")


def _rel(st: State, p) -> str:
    if not p:
        return ""
    try:
        return str(Path(p).resolve().relative_to(st.dir.resolve()))
    except ValueError:
        return str(p)


# ---------------------------------------------------------------- HTML


CSS = """
body{font:13px/1.45 -apple-system,Segoe UI,Helvetica,Arial,'PingFang SC','Noto Sans CJK SC',sans-serif;margin:0;color:#1f2328;background:#f6f8fa}
header{background:#24292f;color:#fff;padding:8px 16px;display:flex;gap:16px;align-items:center}
header a{color:#fff} main{padding:12px 16px;max-width:1280px}
.card{background:#fff;border:1px solid #d0d7de;border-radius:6px;padding:10px 12px;margin:10px 0}
h2{font-size:14px;margin:0 0 8px} table{border-collapse:collapse;width:100%} td,th{border-bottom:1px solid #eaeef2;padding:3px 6px;text-align:left;vertical-align:top}
th{color:#57606a;font-weight:600;background:#f6f8fa} .num{text-align:right;font-variant-numeric:tabular-nums}
.crit{color:#cf222e}.warn{color:#9a6700}.ok{color:#1a7f37}.ask{color:#0969da}.info{color:#57606a}
.dot{font-size:16px} .muted{color:#57606a} .big{font-size:22px;font-weight:700}
.kpis{display:grid;grid-template-columns:repeat(6,1fr);gap:8px} .kpi{background:#fff;border:1px solid #d0d7de;border-radius:6px;padding:8px}
.kpi .l{color:#57606a;font-size:12px} .steps{display:flex;gap:4px;flex-wrap:wrap}
.step{padding:3px 8px;border-radius:12px;background:#eaeef2;color:#57606a;font-size:12px}
.step.done{background:#dafbe1;color:#1a7f37}.step.cur{background:#0969da;color:#fff;font-weight:600}
.needs{border-left:4px solid #0969da}.needs.crit{border-left-color:#cf222e}
.pbar{display:inline-block;width:90px;height:8px;background:#eaeef2;border-radius:4px;vertical-align:middle}
.pbar i{display:block;height:8px;background:#2da44e;border-radius:4px}
details>summary{cursor:pointer} code,pre{font:12px ui-monospace,Menlo,Consolas,monospace} pre{white-space:pre-wrap;margin:4px 0}
.md{font-size:14px;line-height:1.6} .md h1,.md h2,.md h3,.md h4{margin:14px 0 6px} .md h1{font-size:20px} .md h2{font-size:17px}
.md h3{font-size:15px} .md p{margin:6px 0} .md pre{background:#f6f8fa;border:1px solid #eaeef2;border-radius:6px;padding:8px}
.md code{background:#eff1f3;border-radius:4px;padding:0 3px} .md pre code{background:none;padding:0}
.md table{width:auto;margin:6px 0} .md td,.md th{border:1px solid #d0d7de;padding:3px 8px}
.md blockquote{margin:6px 0;padding:0 10px;border-left:4px solid #d0d7de;color:#57606a} .md ul,.md ol{margin:4px 0;padding-left:22px}
.tag{display:inline-block;padding:0 6px;border-radius:10px;font-size:11px;background:#eaeef2}
.tag.pass,.tag.improved{background:#dafbe1;color:#1a7f37}.tag.regression,.tag.fail_correctness,.tag.oracle_modified,.tag.scope_violation,.tag.timeout{background:#ffebe9;color:#cf222e}
textarea{width:100%;font:13px inherit} button{cursor:pointer}
"""

JS = """
const fmt=s=>s<90?Math.floor(s)+'s':s<5400?Math.floor(s/60)+'m':s<259200?(s/3600).toFixed(1)+'h':Math.floor(s/86400)+'d';
function tick(){const now=Date.now()/1000;document.querySelectorAll('[data-t]').forEach(e=>{const a=now-parseFloat(e.dataset.t);
e.textContent=fmt(a);const w=parseFloat(e.dataset.warn||0);e.className=w&&a>3*w?'crit':w&&a>w?'warn':'';});}
tick();setInterval(tick,5000);
const ar=document.getElementById('autoref');if(ar){ar.checked=localStorage.getItem('mf_ar')!=='0';
ar.onchange=()=>localStorage.setItem('mf_ar',ar.checked?'1':'0');}
setInterval(()=>{if(ar&&!ar.checked)return;if(document.querySelector('details[open]'))return;
const a=document.activeElement;if(a&&/TEXTAREA|INPUT/.test(a.tagName))return;location.reload();},30000);
"""


def _page(title: str, body: str, nav: str = "") -> str:
    return (f"<!doctype html><html lang='zh'><head><meta charset='utf-8'><title>{esc(title)}</title>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'><style>{CSS}</style></head><body>"
            f"<header><b>myflow</b>{nav}<span style='margin-left:auto' class='muted'>生成于 {hm(time.time())} · "
            f"<label><input type='checkbox' id='autoref'> 自动刷新</label></span></header><main>{body}</main>"
            f"<script>{JS}</script></body></html>")


def _hdot(h: str) -> str:
    return f"<span class='dot {h}' title='{h}'>●</span>"


def _pbar(frac) -> str:
    if frac is None:
        return "<span class='muted'>—</span>"
    return f"<span class='pbar'><i style='width:{int(frac * 100)}%'></i></span> {int(frac * 100)}%"


def _age_span(t, warn_min=None) -> str:
    w = f" data-warn='{float(warn_min) * 60:.0f}'" if warn_min else ""
    return f"<span data-t='{t}'{w}>{age(time.time() - t)}</span>"


def _report_span(st: State, warn_min=None) -> str:
    return f"{_age_span(st.last_report_t, warn_min)} 前" if st.last_report_t else "<span class='muted'>从未</span>"


def svg_chart(st: State, w: int = 900, h: int = 260) -> str:
    pts = [a for a in st.attempts if a.get("kind", "full") == "full"]
    cum_v = [v for v in st.verifies if not v.get("against")]  # the y axis is "vs frozen base"
    if not pts and not cum_v:
        return "<p class='muted'>还没有完整的尝试。</p>"
    ys = [a["speedup"] for a in pts if a.get("speedup")] + [v["speedup"] for v in cum_v if v.get("speedup")]
    marks = [1.0, st.target] + ([st.ceiling["value"]] if st.ceiling and st.ceiling.get("value") else [])
    lo = min(ys + marks + [0.9]) * 0.97
    hi = max(ys + marks) * 1.06
    n_max = max([a.get("n", 0) for a in st.attempts] + [1])
    L, R, T, B = 48, 16, 12, 26

    def X(n):
        return L + (n - 0.5) / n_max * (w - L - R)

    def Y(v):
        return T + (hi - v) / (hi - lo) * (h - T - B)

    out = [f"<svg viewBox='0 0 {w} {h}' width='100%' style='max-width:{w}px;background:#fff'>"]
    steps = 5
    for i in range(steps + 1):
        v = lo + (hi - lo) * i / steps
        out.append(f"<line x1='{L}' x2='{w - R}' y1='{Y(v):.1f}' y2='{Y(v):.1f}' stroke='#f0f0f0'/>"
                   f"<text x='{L - 4}' y='{Y(v) + 4:.1f}' font-size='10' text-anchor='end' fill='#888'>{v:.2f}</text>")
    for v, color, label in [(1.0, "#888", "baseline"), (st.target, "#1a7f37", f"target {st.target:g}×")] + (
            [(st.ceiling["value"], "#cf222e", f"ceiling {st.ceiling['value']:g}×")] if st.ceiling and st.ceiling.get("value") else []):
        out.append(f"<line x1='{L}' x2='{w - R}' y1='{Y(v):.1f}' y2='{Y(v):.1f}' stroke='{color}' stroke-dasharray='5,4'/>"
                   f"<text x='{w - R - 2}' y='{Y(v) - 3:.1f}' font-size='10' text-anchor='end' fill='{color}'>{label}</text>")
    last_round = None
    for a in st.attempts:
        r = a.get("round", 1)
        if last_round is not None and r != last_round:
            x = X(a.get("n", 1)) - (w - L - R) / n_max / 2
            out.append(f"<line x1='{x:.1f}' x2='{x:.1f}' y1='{T}' y2='{h - B}' stroke='#bbb' stroke-dasharray='2,3'/>"
                       f"<text x='{x + 3:.1f}' y='{T + 10}' font-size='10' fill='#888'>R{r}</text>")
        last_round = r
    latest_v = {v.get("attempt"): v for v in cum_v if v.get("verdict") in VALID_VERDICTS and v.get("speedup")}
    best, path = None, []
    for a in pts:  # trusted best-so-far: a verified number overrides the lane's own measurement
        s = latest_v[a["n"]]["speedup"] if a.get("n") in latest_v else (
            a["speedup"] if a.get("status") == PASS and a.get("speedup") else None)
        if s and (best is None or s > best):
            best = s
        if best is not None:
            path.append(f"{X(a['n']):.1f},{Y(best):.1f}")
    if len(path) > 1:
        out.append(f"<polyline points='{' '.join(path)}' fill='none' stroke='#999' stroke-width='1' stroke-dasharray='3,2'/>")
    for a in st.attempts:
        x, col = X(a.get("n", 1)), lane_color(st, a.get("lane"))
        tip = esc(f"A{a.get('n')} [{a.get('lane')}] {a.get('idea', '')} → {STATUS_ZH.get(a.get('status'), a.get('status'))} {fx(a.get('speedup'))}")
        if a.get("status") == PASS and a.get("speedup") and a.get("kind", "full") == "full":
            out.append(f"<a href='#A{a.get('n')}'><circle cx='{x:.1f}' cy='{Y(a['speedup']):.1f}' r='4' fill='none' "
                       f"stroke='{col}' stroke-width='2'><title>{tip}</title></circle></a>")
        elif a.get("kind", "full") != "full":
            out.append(f"<a href='#A{a.get('n')}'><circle cx='{x:.1f}' cy='{h - B - 4}' r='2' fill='{col}'><title>{tip}（quick，不计入曲线）</title></circle></a>")
        else:
            out.append(f"<a href='#A{a.get('n')}'><text x='{x:.1f}' y='{h - B - 2}' font-size='11' text-anchor='middle' "
                       f"fill='#cf222e'>×<title>{tip}</title></text></a>")
    vcol = {"improved": "#1a7f37", "no_gain": "#888", "regression": "#cf222e"}
    for v in cum_v:
        if not v.get("speedup") or not v.get("attempt"):
            continue
        x, y = X(v["attempt"]), Y(v["speedup"])
        tip = esc(f"复核 V{v.get('n')} of A{v.get('attempt')}: {fx(v['speedup'])} {v.get('verdict')} (自测 {fx(v.get('claim'))})")
        out.append(f"<path d='M{x:.1f},{y - 6:.1f} L{x + 6:.1f},{y:.1f} L{x:.1f},{y + 6:.1f} L{x - 6:.1f},{y:.1f} Z' "
                   f"fill='{vcol.get(v.get('verdict'), '#888')}'><title>{tip}</title></path>")
        if v.get("claim"):
            out.append(f"<line x1='{x:.1f}' x2='{x:.1f}' y1='{Y(v['claim']):.1f}' y2='{y:.1f}' stroke='#cf222e' "
                       f"stroke-width='1' opacity='.5'/>")
    out.append(f"<text x='{L}' y='{h - 6}' font-size='10' fill='#888'>attempt # →（○ 自测通过，◆ 复核，× 失败，虚线 = 可信最佳轨迹（有复核取复核值），红竖线 = 自测与复核之差）</text>")
    out.append("</svg>")
    legend = " ".join(f"<span style='color:{lane_color(st, n)}'>■ {esc(n)}</span>" for n in st.lanes)
    return "".join(out) + f"<div class='muted'>{legend}</div>"


def _stepper(st: State) -> str:
    seen = {p["phase"]: p for p in st.phase_hist}
    ps = shown_phases(st)
    cur = ps.index(st.phase) if st.phase in ps else 0
    cells = []
    for i, p in enumerate(ps):
        visited = p in seen or (p == "freeze" and st.freeze)
        cls = "cur" if i == cur else ("done" if i < cur else "")
        when = f" <span class='muted' style='color:inherit;opacity:.7'>{hm(seen[p]['t'])[6:]}</span>" if p in seen else ""
        skipped = " (跳过)" if i < cur and not visited and p != "spec" else ""
        rnd = f" R{st.round}" if i == cur and p in ("plan", "implement", "verify", "integrate") else ""
        cells.append(f"<span class='step {cls}' title='{esc(p)}'>{i + 1}. {esc(PHASE_ZH[p])}{rnd}{skipped}{when}</span>")
    return "<div class='steps'>" + "".join(cells) + "</div>"


def _per_case_table(st: State, a: dict) -> str:
    pc = a.get("per_case") or {}
    if not pc:
        return ""
    base = (st.freeze or {}).get("baseline", {})
    log = (a.get("bench") or {}).get("log")
    cs = [c for c, _ in sorted(pc.items(), key=lambda kv: kv[1])]
    heads, extra, legend = perf_cols(st, cs, freeze_perf(st), perf_of(st, [log] if log else []), ("基线", "本次"))
    rows = "".join(f"<tr><td>{esc(c)}</td><td class='num'>{base.get(c, '')}</td><td class='num'>{(a.get('metrics') or {}).get(c, '')}</td>"
                   f"<td class='num'>{fx(pc[c])}</td>" + "".join(f"<td class='num'>{esc(x)}</td>" for x in extra[c]) + "</tr>"
                   for c in cs)
    hs = "".join(f"<th class='num'>{esc(h)}</th>" for h in heads)
    return (f"<table><tr><th>case</th><th class='num'>基线</th><th class='num'>本次</th><th class='num'>自测加速</th>{hs}</tr>"
            f"{rows}</table>" + (f"<div class='muted'>{esc(legend)}</div>" if legend else ""))


def _link(st: State, p, label=None) -> str:
    if not p:
        return ""
    rel = _rel(st, p)
    return f"<a href='{esc(rel)}'>{esc(label or Path(rel).name)}</a>"


def _attempt_details(st: State, a: dict) -> str:
    d = a.get("diff") or {}
    parts = [f"<div>想法：{esc(a.get('idea', ''))} {('· 假设 ' + esc(a['hyp'])) if a.get('hyp') else ''}</div>",
             f"<div>提交 <code>{esc(str(a.get('commit', ''))[:12])}</code> · +{d.get('ins', 0)}/-{d.get('del', 0)} · "
             f"{esc(', '.join(d.get('names', [])[:8]))}</div>"]
    for key in ("correctness", "bench"):
        r = a.get(key)
        if r:
            g = r.get("gate_before")
            parts.append(f"<div>{key}: rc={r.get('rc')} · {r.get('dur')}s · {gpu_txt(r.get('gpu'))}"
                         f"{(' · 门禁 ' + _gate_txt(g) + '/' + _gate_txt(r.get('gate_after'))) if g else ''} · {_link(st, r.get('log'), 'log')}</div>")
    if a.get("problems"):
        parts.append("<div class='crit'>问题：" + esc("; ".join(a["problems"][:10])) + "</div>")
    if a.get("note"):
        parts.append(f"<div>备注：{esc(a['note'])}</div>")
    parts.append(_per_case_table(st, a))
    run = a.get("run")
    if run:
        parts.append(f"<div class='muted'>运行目录 {_link(st, run, _rel(st, run))} · {_link(st, Path(run) / 'diff.patch', 'diff.patch')}</div>")
    return "".join(parts)


def html_task(st: State, served: bool = False) -> str:
    now = time.time()
    s = summary(st, now)
    t = st.task
    nav = f" · <a href='{'/' if served else esc(str(home() / 'index.html'))}'>全部任务</a> · {esc(st.id)}"
    body = [f"<div class='card'><div class='big'>{_hdot(s['health'])} {esc(st.id)} <span class='muted' style='font-size:14px'>"
            f"{esc(t.get('title', ''))}</span></div><div>{esc(t.get('goal', ''))}</div><div class='muted'>repo {esc(t.get('repo'))} @ "
            f"{esc(str((st.freeze or {}).get('base', t.get('base', '')))[:12])} · 最后活动 "
            f"{_age_span(st.last_agent_t, t['budget'].get('heartbeat_min'))} 前 · 最后向你汇报 "
            f"{_report_span(st, t['budget'].get('report_min'))} · 当前阶段 {esc(phase_label(st))}</div>"
            f"<div class='muted'>↳ 最近动作：{esc((st.now or {}).get('text', '')[:160])}</div>"
            f"<div style='margin-top:6px'>{_stepper(st)}</div></div>"]
    if st.milestones:
        body.append(_html_roadmap(st, served))
    needs = [a for a in s["alerts"] if a["level"] in ("ask", "crit")]
    if needs or st.pending_gates():
        items = "".join(f"<li class='{a['level']}'>{ICON[a['level']]} {esc(a['msg'])}</li>" for a in needs)
        forms = ""
        if served:
            for g in st.pending_gates():
                forms += (f"<form method='post' action='decide'><input type='hidden' name='gate' value='{esc(g['key'])}'>"
                          f"<div><b>{esc(g['key'])}</b>：{esc(g.get('brief', ''))}</div>"
                          f"<textarea name='text' rows='2' placeholder='你的决定/补充（会作为 user 决策记录）'></textarea>"
                          f"<button>批准并记录</button></form>")
        cls = "crit" if any(a["level"] == "crit" for a in needs) else ""
        body.append(f"<div class='card needs {cls}'><h2>需要你处理</h2><ul>{items}</ul>{forms}</div>")
    dfl = defaults_lines(st)
    if dfl:
        body.append(f"<div class='card'><h2>{esc(dfl[0])}</h2><ul>" + "".join(f"<li>{esc(x)}</li>" for x in dfl[1:])
                    + "</ul></div>")
    fz = st.freeze or {}
    bv, bs = st.best_verified, st.best_self
    b = t["budget"]
    kp = [("基线", f"{len(fz.get('baseline', {}))} cases" if fz else "未冻结",
           f"噪声 {pct(fz.get('noise'))} · {gpu_txt(fz.get('gpu'))}" if fz else ""),
          ("最佳·已复核", fx(bv and bv["speedup"]), f"A{bv.get('attempt')} {bv.get('lane')} · ABBA {bv.get('pairs')}对" if bv else "尚无复核"),
          ("最佳·自测", fx(bs and bs["speedup"]), f"A{bs.get('n')} {bs.get('lane')} · 未配对" if bs else ""),
          ("目标", f"{st.target:g}×", _pbar(s["progress"])),
          ("上限估计", f"{st.ceiling['value']:g}×" if st.ceiling else "—", esc(st.ceiling["basis"][:40]) if st.ceiling else "plan 阶段给出"),
          ("预算", f"{len(st.attempts)}/{b.get('max_attempts')}", f"{st.hours:.1f}/{b.get('max_hours')}h · {stall_txt(st)}")]
    body.append("<div class='kpis'>" + "".join(f"<div class='kpi'><div class='l'>{esc(k)}</div><div class='big'>{v}</div>"
                                               f"<div class='muted'>{sub}</div></div>" for k, v, sub in kp) + "</div>")
    body.append(f"<div class='card'><h2>性能轨迹</h2>{svg_chart(st)}</div>")
    other = [a for a in s["alerts"] if a["level"] not in ("ask", "crit")]
    if other:
        body.append("<div class='card'><h2>预警</h2><ul>" + "".join(
            f"<li class='{a['level']}'>{ICON[a['level']]} {esc(a['msg'])}</li>" for a in other) + "</ul></div>")
    if st.briefs:
        def _ask_tag(br):
            if not br["ask"]:
                return ""
            g = st.gates.get(_gk(br)) or {}
            return "<b class=ok>[已确认]</b> " if g.get("state") == "approved" else "<b class=ask>[待确认]</b> "
        rows = "".join(f"<tr><td style='width:90px'>{hm(br['t'])}</td><td style='width:110px'>{esc(br['phase'])}{('/' + esc(br['lane'])) if br['lane'] else ''}</td>"
                       f"<td>{_ask_tag(br)}{esc(br['text'])}</td></tr>" for br in st.briefs[-8:][::-1])
        body.append(f"<div class='card'><h2>阶段小结（brief，新→旧）</h2><table>{rows}</table></div>")
    if st.lanes:
        rows = ""
        for ln in st.lanes.values():
            n_ok = sum(1 for a in ln["attempts"] if a.get("status") == PASS)
            rows += (f"<tr><td style='color:{lane_color(st, ln['name'])}'><b>{esc(ln['name'])}</b></td><td>{esc(ln['direction'])}</td>"
                     f"<td>{esc(ln.get('hyp') or '')}</td><td>{esc(ln['status'])}{(' — ' + esc(ln['reason'])) if ln['reason'] else ''}</td>"
                     f"<td class='num'>{len(ln['attempts'])} ({n_ok})</td><td class='num{' warn' if ln['stalled'] else ''}'>{ln['no_improve']}</td>"
                     f"<td class='num'>{fx(ln['best_self'] and ln['best_self']['speedup'])}</td>"
                     f"<td class='num'>{fx(ln['best_verified'] and ln['best_verified']['speedup'])}</td>"
                     f"<td>{_age_span(ln['t_last'], t['budget'].get('heartbeat_min') if ln['status'] == 'active' else None)}</td>"
                     f"<td class='muted'>{esc(ln.get('wt') or '')}</td></tr>")
        body.append("<div class='card'><h2>Lanes（并行实现线）</h2><table><tr><th>lane</th><th>方向</th><th>假设</th><th>状态</th>"
                    "<th class='num'>尝试(通过)</th><th class='num'>无提升</th><th class='num'>最佳自测</th><th class='num'>最佳复核</th><th>最后活动</th><th>worktree</th></tr>"
                    f"{rows}</table></div>")
    if st.attempts:
        vmap = {}
        for v in st.verifies:
            vmap.setdefault(v.get("attempt"), []).append(v)
        rows = ""
        for a in st.attempts[::-1]:
            vs = vmap.get(a.get("n"), [])
            vtxt = " ".join(f"<span class='tag {esc(v.get('verdict'))}'>{'增量 ' if v.get('against') else ''}{fx(v.get('speedup'))} "
                            f"{esc(STATUS_ZH.get(v.get('verdict'), v.get('verdict')))}</span>" for v in vs)
            rows += (f"<tr id='A{a.get('n')}'><td><details><summary><b>A{a.get('n')}</b> {hm(a['t'])} "
                     f"<span style='color:{lane_color(st, a.get('lane'))}'>[{esc(a.get('lane'))}]</span> "
                     f"<span class='tag {esc(a.get('status'))}'>{esc(STATUS_ZH.get(a.get('status'), a.get('status')))}</span> "
                     f"{'<span class=tag>quick</span> ' if a.get('kind', 'full') != 'full' else ''}"
                     f"自测 <b>{fx(a.get('speedup'))}</b> {vtxt} — {esc(a.get('idea', ''))[:120]}</summary>"
                     f"{_attempt_details(st, a)}</details></td></tr>")
        body.append(f"<div class='card'><h2>尝试（新→旧，点击展开 L2 细节）</h2><table>{rows}</table></div>")
    if st.verifies:
        rows = "".join(f"<tr><td>V{v.get('n')}</td><td>{hm(v['t'])}</td><td>A{v.get('attempt')} [{esc(v.get('lane'))}]"
                       f"{(' <b>增量</b> 对 ' + esc(v.get('against_ms') or str(v.get('against'))[:9])) if v.get('against') else ''}</td>"
                       f"<td><span class='tag {esc(v.get('verdict'))}'>{esc(STATUS_ZH.get(v.get('verdict'), v.get('verdict')))}</span></td>"
                       f"<td class='num'><b>{fx(v.get('speedup'))}</b></td><td class='num'>{'' if v.get('against') else fx(v.get('claim'))}</td>"
                       f"<td class='num'>{pct(v.get('noise'))}</td><td>{v.get('pairs')}对 {gpu_txt(v.get('gpu'))}</td>"
                       f"<td>{esc(v.get('reason', ''))} {_link(st, v.get('run'), 'run')}</td></tr>" for v in st.verifies[::-1])
        body.append("<div class='card'><h2>独立复核（干净 worktree + 同卡 ABBA；默认对照冻结基线，“增量”对照上一里程碑）</h2><table><tr><th>#</th><th>时间</th>"
                    "<th>对象</th><th>结论</th><th class='num'>复核</th><th class='num'>自测</th><th class='num'>噪声</th><th>方式</th>"
                    f"<th>说明</th></tr>{rows}</table></div>")
    if st.hyps:
        rows = "".join(f"<tr><td>{esc(h['id'])}</td><td>{esc(h['text'])}</td><td>{esc(h['predict'])}</td><td>{esc(h['status'])}</td>"
                       f"<td>{esc(h['evidence'])}</td></tr>" for h in st.hyps.values())
        body.append(f"<div class='card'><h2>假设台账</h2><table><tr><th>id</th><th>假设</th><th>预测</th><th>状态</th><th>证据</th></tr>{rows}</table></div>")
    if st.decisions or st.changes or st.notes:
        items = [f"<li>{hm(d['t'])} <b>决策</b>（{esc(_by(d['by']))}）{esc('[' + d['gate'] + '] ' if d['gate'] else '')}{esc(d['text'])}</li>" for d in st.decisions]
        items += [f"<li>{hm(c['t'])} <b>变更</b>（{esc(c['by'])}）<code>{esc(c['key'])}</code>: {esc(c['old'])} → {esc(c['new'])} {esc(c['why'])}</li>" for c in st.changes]
        items += [f"<li>{hm(n['t'])} <b>笔记</b>[{esc(n['kind'])}]{('[' + esc(n['lane']) + ']') if n['lane'] else ''} {esc(n['title'])} {_link(st, n['file'])}</li>" for n in st.notes]
        body.append("<div class='card'><h2>决策 · 契约变更 · 笔记</h2><ul>" + "".join(items) + "</ul></div>")
    if served:
        body.append("<div class='card'><h2>给 agent 留言（steer）</h2><form method='post' action='steer'>"
                    "<textarea name='text' rows='2' placeholder='例如：不要再动 prepare kernel，集中在 union'></textarea>"
                    "<input name='lane' placeholder='lane（可空=全体）' size='12'> <button>发送</button></form>"
                    "<div class='muted'>agent 下一次调用任何 mf 命令时都会看到这条留言，并需 mf note --ack 回执。</div></div>")
    ev_rows = "".join(f"<tr><td>{e.get('seq')}</td><td>{hm(e.get('t'))}</td><td>{esc(e.get('kind'))}</td><td>{esc(e.get('lane', ''))}</td>"
                      f"<td>{esc(e.get('by'))}</td><td>{esc(e.get('title', ''))[:160]}</td></tr>" for e in st.events[-150:][::-1])
    body.append(f"<div class='card'><details><summary><b>事件流（L3，最近 150 条）</b> · <a href='events.jsonl'>events.jsonl</a> · "
                f"<a href='task.json'>task.json</a> · <a href='STATUS.md'>STATUS.md</a></summary><table>{ev_rows}</table></details></div>")
    return _page(f"{st.id} · myflow", "".join(body), nav)


def entry_links(st: State, web=None) -> str:
    """Where to read the entry document rendered: the local integration worktree and the last pushed tag on GitHub."""
    entry = st.task["doc"].get("entry")
    if not entry:
        return ""
    links = []
    local = st.dir / "wt" / "_ms" / entry
    if local.is_file():
        links.append(_link(st, local, "本地（mf serve 渲染）"))
    pushed = [m for m in st.closed_ms if m.get("pushed")]
    if web and pushed:
        tag = pushed[-1]["close"]["tag"]
        links.append(f"<a href='{web}/blob/{esc(tag)}/{esc(entry)}'>GitHub（{esc(tag)}）</a>")
    return f"<div>入口文档 <code>{esc(entry)}</code>：{' · '.join(links) or '（尚未生成）'}</div>" if links else ""


def _html_roadmap(st: State, served: bool) -> str:
    web = github_web(st.task["git"].get("url"))
    chips = []
    for m in st.milestones.values():
        cls = {"done": "done", "closing": "cur"}.get(m["status"], "")
        style = " style='text-decoration:line-through'" if m["status"] == "dropped" else ""
        chips.append(f"<span class='step {cls}'{style} title='{esc(m.get('title', ''))}'>{esc(m['id'])} "
                     f"{esc(ms_status(st, m).split(' ')[0])} {esc(TYPE_ZH.get(m.get('type'), ''))}</span>")
    nxt = next_label(st)
    dirs = guard.direction(st)
    dchips = "".join(f"<span class='tag' title='{esc(d['msg'])}'><span class='{d['level']}'>"
                     f"{'✓' if d['level'] == 'ok' else ICON.get(d['level'], '·')}</span> {esc(d['name'])}"
                     f"{'' if d['level'] == 'ok' else '：' + esc(d['msg'][:80])}</span> " for d in dirs)
    rows, prev_ref = "", (st.freeze or {}).get("base")
    for m in st.milestones.values():
        c = _ms_cells(st, m)
        prep, close = m.get("prep") or {}, m.get("close") or {}
        links = []
        d = st.dir / "milestones" / m["id"]
        for f, label in (("card.md", "卡片"), ("qa.md", "冷读问答"), ("commit-msg.txt", "提交信息")):
            if (d / f).is_file():
                links.append(_link(st, d / f, label))
        if web and close and m.get("pushed"):
            links.append(f"<a href='{web}/commit/{close['commit']}'>GitHub 提交</a>")
            if prev_ref:
                links.append(f"<a href='{web}/compare/{prev_ref}...{close['tag']}'>本里程碑 diff</a>")
        if close:
            prev_ref = close["tag"]
        roles = "".join(f"<li>{esc(r)}：{esc(rec.get('result'))} · 会话 <code>{esc(rec.get('session'))}</code> · "
                        f"改 {len(rec.get('changed') or [])} 个文件{(' · ' + str(rec['minutes']) + ' 分钟') if rec.get('minutes') else ''} · "
                        f"{esc(rec.get('summary', ''))}</li>" for r, rec in (m.get("roles") or {}).items())
        card = (d / "card.md").read_text(encoding="utf-8") if (d / "card.md").is_file() else ""
        plan = (f"验收：{esc(m.get('accept') or '—')} · 预期：{esc(m.get('expect') or '—')}× · 依据：{esc(m.get('basis') or '—')} · "
                f"目标：{esc(', '.join(m.get('goals') or []) or '—')} · 约 {esc(m.get('est_h') or '—')}h")
        if m.get("why"):
            plan += f" · 计划变更原因：{esc(m['why'])}"
        cand = ""
        if prep:
            who = (f"A{prep['attempt']} [{prep.get('lane')}]" if prep.get("attempt") else f"[{prep['lane']}] 头部" if prep.get("lane")
                   else "bootstrap" if prep.get("bootstrap") else "仅文档/用例")
            cand = (f"<div class='muted'>候选 {esc(who)} <code>{esc(prep['candidate'][:10])}</code> · 父 <code>{esc(prep['parent'][:10])}</code>"
                    f" · 运行时树 <code>{esc(prep['runtime_tree'])}</code> · 改动 {c['lines'] or 0} 行"
                    f"{(' · 超预算：' + esc(prep['size_why'])) if prep.get('size_why') else ''}"
                    f"{(' · 接受回退：' + esc(prep['regress_why'])) if prep.get('regress_why') else ''}"
                    f"{(' · 接受不一致的复核：' + esc(prep['verify_why'])) if prep.get('verify_why') else ''}</div>")
        rows += (f"<tr id='{esc(m['id'])}'><td><details><summary><b>{esc(m['id'])}</b> {esc(c['status'])} · {esc(c['type'])} · "
                 f"{esc(c['title'])} · 增量 <b>{esc(c['inc'])}</b> · 累计 {esc(c['cum'])} · 用例 {esc(c['tests'])} · 复核 {esc(c['cold'])}"
                 f"{(' · ' + esc(c['tag'])) if c['tag'] else ''}</summary><div>{plan}</div>{cand}"
                 f"{('<ul>' + roles + '</ul>') if roles else ''}"
                 f"{md_html(card) if card else ''}<div>{' · '.join(links)}</div></details></td></tr>")
    return (f"<div class='card'><h2>路线图（里程碑 = 一个可评审的完整改动；✅ = 已提交 + tag）</h2>"
            f"{entry_links(st, web)}"
            f"<div class='steps'>{'<span class=muted>→</span>'.join(chips)}</div>"
            f"{('<div style=margin-top:6px><b>下一步</b>（上次推送的承诺）：' + esc(nxt) + '</div>') if nxt else ''}"
            f"{('<div style=margin-top:6px><b>方向检查</b> ' + dchips + '</div>') if dchips else ''}"
            f"<table style='margin-top:6px'>{rows}</table></div>")


def html_index(states: list, served: bool = False) -> str:
    now = time.time()
    rows = ""
    for st in states:
        s = summary(st, now)
        href = f"/t/{esc(st.id)}/" if served else esc(str(st.dir / "dashboard.html"))
        top = (s["needs"] or s["alerts"] or [None])[0]
        alert = f"<span class='{top['level']}'>{ICON[top['level']]} {esc(top['msg'][:90])}</span>" if top else ""
        nneed = len(s["needs"])
        rows += (f"<tr><td>{_hdot(s['health'])}</td><td>{'└ ' if s['parent'] else ''}<a href='{href}'><b>{esc(st.id)}</b></a>"
                 f"<div class='muted'>{esc(s['title'])}</div><div class='muted'>↳ {esc(s['now'][:90])}</div></td><td>{esc(s['phase'])}</td>"
                 f"<td class='num'><b>{fx(s['best_v'])}</b></td><td class='num muted'>{fx(s['best_s'])}</td>"
                 f"<td class='num'>{s['target']:g}×</td><td>{_pbar(s['progress'])}</td>"
                 f"<td class='num'>{s['attempts']}/{s['max_attempts']}</td><td class='num'>{s['lanes_active']}/{s['lanes']}</td>"
                 f"<td>{(str(s['ms_done']) + '/' + str(s['ms_total'])) if s['ms_total'] else '—'}"
                 f"{('<div class=muted>⏭ ' + esc(s['ms_next'][:60]) + '</div>') if s['ms_next'] else ''}</td>"
                 f"<td>{_age_span(st.last_agent_t, st.task['budget'].get('heartbeat_min') if not st.done() else None)}"
                 f"<div class='muted'>汇报 {_report_span(st, st.task['budget'].get('report_min') if not st.done() else None)}</div></td>"
                 f"<td>{('<b class=ask>' + str(nneed) + ' 项</b>') if nneed else ''}</td><td>{alert}</td></tr>")
    if not rows:
        rows = "<tr><td colspan='13' class='muted'>还没有任务：mf new &lt;id&gt; --repo &lt;path&gt;</td></tr>"
    table = ("<div class='card'><h2>全部任务（L0）</h2><table><tr><th></th><th>任务</th><th>阶段</th><th class='num'>最佳·复核</th>"
             "<th class='num'>自测</th><th class='num'>目标</th><th>进度</th><th class='num'>尝试</th><th class='num'>lanes</th>"
             f"<th>里程碑 · 下一步</th><th>最后活动</th><th>需要你</th><th>首要预警</th></tr>{rows}</table></div>")
    legend = ("<div class='muted'>● 绿=正常 黄=预警 红=必须处理 · 最佳·复核 = 干净 worktree 里 ABBA 同卡对照冻结基线的结果；"
              "自测 = lane 自己的未配对测量（仅供参考）</div>")
    return _page("myflow · 全部任务", table + legend)


def render_task(st: State) -> None:
    atomic_write(st.dir / "STATUS.md", status_md(st))
    atomic_write(st.dir / "dashboard.html", html_task(st))


def overview_md(states: list) -> str:
    now = time.time()
    L = [f"# myflow 总览（{hm(now)}）", "",
         "| | 任务 | 阶段 | 最佳·复核 | 自测 | 目标 | 进度 | 尝试 | lanes | 里程碑 · 下一步 | 最后活动 | 首要预警 |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    needs = []
    for st in states:
        s = summary(st, now)
        top = (s["needs"] or s["alerts"] or [None])[0]
        prog = f"{int(s['progress'] * 100)}%" if s["progress"] is not None else "—"
        ms = (f"{s['ms_done']}/{s['ms_total']}" + (f"<br>⏭ {_md_cell(s['ms_next'][:40])}" if s["ms_next"] else "")) \
            if s["ms_total"] else "—"
        L.append(f"| {ICON[s['health']] if s['health'] != 'ok' else '🟢'} | {'└ ' if s['parent'] else ''}[{st.id}]({st.dir / 'STATUS.md'})"
                 f"<br>↳ {_md_cell(s['now'][:60])} | "
                 f"{s['phase']} | **{fx(s['best_v'])}** | {fx(s['best_s'])} | {s['target']:g}× | {prog} | "
                 f"{s['attempts']}/{s['max_attempts']} | {s['lanes_active']}/{s['lanes']} | {ms} | {age(s['last'])} | "
                 f"{_md_cell(top['msg'][:80]) if top else ''} |")
        needs += [f"- **{st.id}** {ICON[a['level']]} {a['msg']}" for a in s["needs"]]
    if needs:
        L += ["", "## 需要你处理", *needs]
    L += ["", "健康：🟢 正常 ⚠ 预警 ✖ 必须处理 · 最佳·复核 = 干净 worktree + 同卡 ABBA；自测 = lane 未配对测量。"
               "详情：各任务 STATUS.md / dashboard.html，或 `mf serve`。"]
    return "\n".join(L) + "\n"


def render_index() -> Path:
    states = []
    for d in all_task_dirs():
        try:
            states.append(State(d))
        except (OSError, ValueError, KeyError) as e:
            print(f"[mf] skip {d}: {e}", file=sys.stderr)
    path = home() / "index.html"
    atomic_write(path, html_index(states))
    atomic_write(home() / "STATUS.md", overview_md(states))
    return path


def dump_json(st: State) -> str:
    s = summary(st)
    return json.dumps(s, ensure_ascii=False, indent=1, default=str)
