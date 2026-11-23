"""Milestone (v2 phase 1) self-tests, CPU only: plan -> prep -> roles -> close -> push, bootstrap, direction."""

from __future__ import annotations

import os
import subprocess
import time
import unittest

from test_myflow import ROOT, Base, mf

from myflow import cli, guard, ms, render
from myflow.core import State, append_event, task_dir_of


def git(*args, cwd=None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


class MilestoneBase(Base):
    def setUp(self):
        super().setUp()
        self.remote = self.tmp / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)], check=True)

    def configure(self, tid="t1", entry="docs/ENTRY.md"):
        self.ok("set", tid, f"doc.entry={entry}", 'tests.paths=["test_kernel.py","tests/**"]',
                f"git.url={self.remote}", "git.branch=feat/t1")

    def roles(self, tid, mid, tdir, wt, doc_line="设计说明", entry="docs/ENTRY.md"):
        """The closing review, recorded the way an independent sub-agent would."""
        d = tdir / "milestones" / mid
        p = wt / entry
        p.write_text(p.read_text() + f"\n{doc_line}\n")
        (d / "card.md").write_text(f"### {mid}\n**What**: raise SPEED\n**Why**: toy\n")
        self.ok("ms", "review", tid, mid, "--session", f"review-{mid}", "--card", d / "card.md", "-m", "doc updated")

    def milestone1(self):
        """M1 = lane L1 at SPEED 1.3, closed and pushed. Returns (tdir, integration worktree, M1 commit)."""
        tdir = self.ready()
        self.configure()
        self.edit(tdir, "L1", r"^SPEED = .*", "SPEED = 1.3")
        self.ok("try", "t1", "--lane", "L1", "--idea", "1.3")
        self.verify("L1")
        self.ok("ms", "plan", "t1", "M1", "--title", "speed 1.3", "--type", "perf", "--expect", "1.3", "--basis", "toy",
                "--goal", "G1")
        self.ok("ms", "plan", "t1", "M2", "--title", "speed 1.6", "--type", "perf", "--expect", "1.2", "--goal", "G1")
        self.ok("ms", "prep", "t1", "M1", "--lane", "L1")
        wt = tdir / "wt" / "_ms"
        self.roles("t1", "M1", tdir, wt)
        self.ok("ms", "close", "t1", "M1", "--next", "M2")
        key = next(g["key"] for g in self.st().pending_gates() if g["key"].startswith("push:"))
        self.ok("decide", "t1", "--gate", key, "推吧", "--quote", "推吧")
        self.ok("ms", "push", "t1")
        return tdir, wt, self.st().milestones["M1"]["close"]["commit"]

    def remote_head(self):
        return git("--git-dir", str(self.remote), "rev-parse", "feat/t1")


class MilestoneTest(MilestoneBase):
    def test_milestone_flow_push_and_incremental_acceptance(self):
        tdir = self.ready()
        self.configure()
        self.edit(tdir, "L1", r"^SPEED = .*", "SPEED = 1.3")
        self.ok("try", "t1", "--lane", "L1", "--idea", "1.3")
        self.verify("L1")
        self.ok("ms", "plan", "t1", "M1", "--title", "speed 1.3", "--type", "perf", "--expect", "1.3", "--basis", "toy",
                "--goal", "G1", "--ceiling", "2")
        self.assertEqual(self.st().ceiling["value"], 2.0)
        self.ok("ms", "plan", "t1", "M2", "--title", "speed 1.6", "--type", "perf", "--expect", "1.2", "--goal", "G1")
        out = self.ok("ms", "prep", "t1", "M1", "--lane", "L1")
        self.assertIn("myflow-review", out)
        wt = tdir / "wt" / "_ms"
        entry = (wt / "docs" / "ENTRY.md").read_text()
        self.assertIn("由 mf 生成", entry)
        self.assertIn("- M1 ✅ 本提交 · 性能 · speed 1.3", entry)  # one line per milestone: the commit has the details
        rc, out = mf("ms", "review", "t1", "M1", "--session", "w0")
        self.assertEqual(rc, 3, out)  # the card skeleton still has placeholders
        self.assertIn("占位符", out)
        self.roles("t1", "M1", tdir, wt)
        rc, out = mf("ms", "close", "t1", "M1", "--next", "M9")
        self.assertEqual(rc, 3, out)
        rc, out = mf("ms", "close", "t1", "M1", "--next", "")
        self.assertEqual(rc, 2, out)
        card = tdir / "milestones" / "M1" / "card.md"
        good = card.read_text()
        card.write_text(good + "9.99x faster!\n")
        rc, out = mf("ms", "close", "t1", "M1", "--next", "M2")
        self.assertEqual(rc, 3, out)  # the pushed card must be the one the writer recorded
        self.assertIn("卡片在复核记录之后被改过", out)
        card.write_text(good)
        out = self.ok("ms", "close", "t1", "M1", "--next", "M2")
        self.assertIn("需要用户确认推送目标", out)
        self.assertIn("mf 不会自己问用户", out)  # mf records the gate; the orchestrator must relay it
        st = self.st()
        m1 = st.milestones["M1"]
        self.assertEqual(m1["status"], "done")
        self.assertFalse(m1["pushed"])
        self.assertIn("unpushed", {d["code"] for d in guard.direction(st) if d["level"] != "ok"})
        key = next(g["key"] for g in st.pending_gates() if g["key"].startswith("push:"))
        self.ok("decide", "t1", "--gate", key, "推吧", "--quote", "推吧")
        self.assertIn("已推送 M1", self.ok("ms", "push", "t1"))
        c1 = m1["close"]["commit"]
        self.assertEqual(self.remote_head(), c1)
        self.assertEqual(git("--git-dir", str(self.remote), "rev-parse", "t1-M1^{commit}"), c1)
        msg = git("log", "-1", "--format=%B", c1, cwd=self.repo)
        for needle in ("[M1] speed 1.3", "Perf: vs frozen base", "runtime-tree=", "Next: M2"):
            self.assertIn(needle, msg)
        doc1 = git("show", f"{c1}:docs/ENTRY.md", cwd=self.repo)
        self.assertIn("设计说明", doc1)
        self.assertIn("基线来源（mf 记录）", doc1)  # user rule 11-22: where the baseline numbers come from
        self.assertIn("最近的性能里程碑 M1：相对冻结基线 1.", doc1)  # the conclusion first, the per-case table folded
        self.assertIn("<details><summary>逐 case（3 个，mf 生成）</summary>", doc1)
        for f in ("STATUS.md", "dashboard.html"):
            self.assertIn("路线图", (tdir / f).read_text())

        # a lane named "ms" cannot move the integration branch or get its commits published
        self.ok("lane", "new", "t1", "ms", "--direction", "name clash")
        self.edit(tdir, "ms", r"^SPEED = .*", "SPEED = 9.9")
        self.ok("try", "t1", "--lane", "ms", "--idea", "unaccepted")
        self.ok("ms", "push", "t1")
        self.assertEqual(self.remote_head(), c1)
        self.assertEqual(git("rev-parse", "mf/t1/_ms", cwd=self.repo), c1)

        # the next round builds on M1; docs added by M1 do not count as scope violations
        self.ok("lane", "new", "t1", "L2", "--direction", "more speed")
        self.assertEqual(self.st().lanes["L2"]["start"], c1)
        self.ok("try", "t1", "--lane", "L2", "--idea", "no-op: inherits M1's gain")
        n_noop = self.st().attempts[-1]["n"]
        self.assertEqual(self.st().attempt(n_noop)["status"], "pass")
        self.verify("L2", n_noop)
        self.assertEqual(self.st().verifies[-1]["verdict"], "improved")  # vs the frozen base it looks like a gain ...
        rc, out = mf("ms", "prep", "t1", "M2", "--lane", "L2", "--attempt", n_noop)
        self.assertEqual(rc, 3, out)  # ... but vs M1 it is nothing
        self.assertIn("增量验收未通过", out)
        self.edit(tdir, "L2", r"^SPEED = .*", "SPEED = 1.6")
        self.ok("try", "t1", "--lane", "L2", "--idea", "1.6")
        n = self.st().attempts[-1]["n"]
        self.verify("L2", n)
        self.ok("set", "t1", 'tests.paths=["**"]', 'doc.paths=["**"]')  # broad globs must stay harmless
        self.ok("ms", "prep", "t1", "M2", "--lane", "L2", "--attempt", n)
        inc = self.st().milestones["M2"]["prep"]["inc"]
        self.assertEqual(inc["verdict"], "improved")
        self.assertAlmostEqual(inc["speedup"], 1.6 / 1.3, delta=0.05)

        # the review may only touch documents and protected tests, never runtime code
        d = tdir / "milestones" / "M2"
        (d / "card.md").write_text("### M2\n**What**: 1.6\n")
        k = wt / "kernel.py"
        orig = k.read_text()
        k.write_text(orig + "SPEED = 0.5\n")
        rc, out = mf("ms", "review", "t1", "M2", "--session", "r2", "--card", d / "card.md")
        self.assertEqual(rc, 3, out)
        self.assertIn("只能改", out)
        k.write_text(orig)
        (wt / "tuning.txt").write_text("0.3\n")  # .txt may be runtime data, never "documentation"
        rc, out = mf("ms", "review", "t1", "M2", "--session", "r2", "--card", d / "card.md")
        self.assertEqual(rc, 3, out)
        (wt / "tuning.txt").unlink()
        tk = wt / "test_kernel.py"
        base_tk = tk.read_text()
        tk.write_text(base_tk + "assert kernel.run([1]) == [2]\n")
        rc, out = mf("ms", "review", "t1", "M2", "--session", "r2", "--card", d / "card.md")
        self.assertEqual(rc, 3, out)  # user rule 11-22: no approval needed, but the reason has to be written down
        self.assertIn("--tests-why", out)
        self.ok("ms", "review", "t1", "M2", "--session", "r2", "--card", d / "card.md", "--tests-why", "加 run([1])")
        tk.write_text(base_tk + "assert kernel.run([0]) == [0]\n")  # changed again: a new reason for the new content
        rc, out = mf("ms", "review", "t1", "M2", "--session", "r2", "--card", d / "card.md")
        self.assertEqual(rc, 3, out)
        e = wt / "docs" / "ENTRY.md"
        e.write_text(e.read_text() + "第二轮\n")
        out = self.ok("ms", "review", "t1", "M2", "--session", "r2", "--card", d / "card.md",
                      "--tests-why", "只加断言 run([0])，没有放宽容差")
        self.assertIn("已说明", out)
        self.assertEqual([g for g in self.st().pending_gates() if g["key"].startswith(("oracle@", "tests@"))], [])
        e.write_text(e.read_text() + "late edit\n")
        rc, out = mf("ms", "close", "t1", "M2", "--next", "done")
        self.assertEqual(rc, 3, out)
        self.assertIn("复核记录之后 worktree 又有改动", out)  # edited after the review recorded
        e.write_text(e.read_text().replace("late edit\n", ""))
        out = self.ok("ms", "close", "t1", "M2", "--next", "done")
        self.assertIn("已推送 M2", out)  # same approved target: no second question
        self.assertIn("Changed existing tests: test_kernel.py; why (review): 只加断言 run([0])，没有放宽容差",
                      git("log", "-1", "--format=%B", self.st().milestones["M2"]["close"]["commit"], cwd=self.repo))
        st = self.st()
        c2 = st.milestones["M2"]["close"]["commit"]
        self.assertEqual(self.remote_head(), c2)
        self.assertEqual(git("rev-parse", f"{c2}^", cwd=self.repo), c1)
        self.assertEqual(st.oracle_accepts[-1]["paths"], ["test_kernel.py"])
        self.assertEqual([g["key"] for g in st.pending_gates() if g["key"].startswith("oracle@")], [])
        self.assertNotIn("promise", {d["code"] for d in guard.direction(st) if d["level"] != "ok"})

        # a lane started from another lane (which started from a milestone) is checked against that milestone
        self.ok("lane", "new", "t1", "L3", "--from", "L2", "--direction", "from L2")
        self.ok("try", "t1", "--lane", "L3", "--idea", "x")
        self.assertEqual(self.st().attempts[-1]["status"], "pass")
        self.assertIn("M2", self.ok("status", "t1"))
        self.assertIn("审计通过", self.ok("report", "t1"))
        self.assertIn("## 里程碑", (tdir / "REPORT.md").read_text())

    def test_integration_lane_needs_no_try_and_prep_measures_both_in_one_run(self):
        """User request 11-22: no `mf try` on an integration lane; one three-arm run (frozen base / previous milestone /
        candidate) gives both numbers; the same code and tests are checked once."""
        tdir, wt, c1 = self.milestone1()
        self.ok("lane", "new", "t1", "L2", "--direction", "more speed")
        self.edit(tdir, "L2", r"^SPEED = .*", "SPEED = 1.6")
        self.ok("try", "t1", "--lane", "L2", "--idea", "1.6")
        n = self.st().attempts[-1]["n"]
        self.ok("lane", "new", "t1", "int", "--from", "M1", "--merge", f"L2@{n}", "--direction", "M2 = L2 on M1")
        out = self.ok("ms", "prep", "t1", "M2", "--lane", "int")
        self.assertIn("三方交替", out)
        vs = [v for v in self.st().verifies if v.get("arms") == 3]
        self.assertEqual(sorted(bool(v.get("against")) for v in vs), [False, True])  # cumulative + incremental
        self.assertEqual(len({v["run"] for v in vs}), 1)  # from one run
        prep = self.st().milestones["M2"]["prep"]
        self.assertIsNone(prep["attempt"])
        self.assertAlmostEqual(prep["inc"]["speedup"], 1.6 / 1.3, delta=0.06)
        self.assertAlmostEqual(prep["cum"]["speedup"], 1.6, delta=0.08)
        self.roles("t1", "M2", tdir, wt)
        out = self.ok("ms", "close", "t1", "M2", "--next", "done")
        self.assertIn("复用", out)  # only documents changed since the verify: correctness is not run again

    def test_refreeze_invalidates_old_verifies_and_contract_breaks_milestones(self):
        tdir, wt, c1 = self.milestone1()
        self.ok("lane", "new", "t1", "L2", "--direction", "no-op")
        self.ok("try", "t1", "--lane", "L2", "--idea", "no-op")
        n = self.st().attempts[-1]["n"]
        self.verify("L2", n)
        self.ok("freeze", "t1", "--base", "M1")  # new epoch: the 1.3x above was measured against the old base
        rc, out = mf("ms", "prep", "t1", "M2", "--lane", "L2", "--attempt", n)
        self.assertEqual(rc, 3, out)  # prep re-measured against the new base itself (user request 11-22) ...
        self.assertIn("对冻结基线做同卡 ABBA", out)
        self.assertIn("增量验收未通过", out)  # ... and the old 1.3x did not count
        self.assertIn("复用", out)  # the same code and tests already passed in a clean worktree: not run again
        self.ok("set", "t1", "commands.correctness=python3 -c pass")
        rc, out = mf("ms", "prep", "t1", "M2", "--lane", "L2", "--attempt", n)
        self.assertEqual(rc, 3, out)
        self.assertIn("契约", out)

    def test_finer_bench_is_refrozen_and_published_by_a_maint_milestone(self):
        """User request 11-23: performance data is the main deliverable. An approved finer bench is committed after
        the last milestone and frozen there; a maint milestone without lane publishes it, the entry document shows
        the new freeze per case, sparse size series are flagged and the perf section needs TB/s and a data block."""
        tdir, wt, c1 = self.milestone1()
        side = self.tmp / "side"
        git("worktree", "add", "-q", "--detach", str(side), c1, cwd=self.repo)
        (side / "bench.py").write_text((side / "bench.py").read_text() + "\n# finer sweep\n")
        git("commit", "-qam", "finer bench", cwd=side)
        x = git("rev-parse", "HEAD", cwd=side)
        self.ok("freeze", "t1", "--base", x)
        self.assertEqual(cli._scope_base(self.st(), {"start": x}), x)  # lanes from it: the new freeze is the oracle
        self.ok("ms", "plan", "t1", "M3", "--title", "finer bench", "--type", "maint")
        out = self.ok("ms", "prep", "t1", "M3")
        self.assertIn("重新冻结", out)
        self.assertEqual(self.st().milestones["M3"]["prep"]["candidate"], x)
        d = tdir / "milestones" / "M3"
        (d / "card.md").write_text("### M3\n**What**: finer bench\n")
        self.ok("ms", "review", "t1", "M3", "--session", "rv3", "--card", d / "card.md", "--tests-why", "用户要求细扫")
        self.ok("ms", "close", "t1", "M3", "--next", "await-user")
        c3 = self.st().milestones["M3"]["close"]["commit"]
        self.assertIn("bench.py", git("diff", "--stat", c1, c3, cwd=self.repo))
        self.assertIn(f"重新冻结于 `{x[:10]}`", git("show", f"{c3}:docs/ENTRY.md", cwd=self.repo))
        self.assertIn("最佳累计 —", git("show", f"{c3}:docs/ENTRY.md", cwd=self.repo))  # M1's 1.3x was another base
        self.ok("report", "t1")
        self.assertIn(f"本冻结轮次（`{x[:10]}`）还没有复核过的候选", (tdir / "REPORT.md").read_text())
        self.assertEqual(render.sweep_gaps(["tp2_m1", "tp2_m16", "tp4_m1", "tp4_m2", "tp4_m4", "x"]),
                         ["tp2_m* 缺 2, 4, 8"])
        self.assertEqual(render.sweep_gaps([f"tp2_m{1 << k}" for k in range(16)]), [])
        self.ok("set", "t1", "perf.peak_tbps=5.3")
        bad = " ".join(ms.doc_problems(self.st(), "## 性能\n快一倍\n"))
        self.assertIn("TB/s", bad)
        self.assertIn("详细数据", bad)
        self.assertEqual(ms.doc_problems(self.st(), "## 性能\n1.9 TB/s\n<!-- mf:perf -->\n<!-- /mf:perf -->\n"), [])

    def test_signing_failure_leaves_no_partial_state(self):
        tdir = self.ready()
        self.configure()
        self.ok("set", "t1", "git.sign=true")
        git("config", "tag.gpgSign", "true", cwd=self.repo)
        git("config", "gpg.program", "/bin/false", cwd=self.repo)
        self.edit(tdir, "L1", r"^SPEED = .*", "SPEED = 1.3")
        self.ok("try", "t1", "--lane", "L1", "--idea", "1.3")
        self.verify("L1")
        self.ok("ms", "plan", "t1", "M1", "--title", "speed", "--type", "perf", "--expect", "1.3")
        self.ok("ms", "prep", "t1", "M1", "--lane", "L1")
        self.roles("t1", "M1", tdir, tdir / "wt" / "_ms")
        base = self.st().freeze["base"]
        rc, out = mf("ms", "close", "t1", "M1", "--next", "done")
        self.assertEqual(rc, 2, out)
        self.assertEqual(git("rev-parse", "mf/t1/_ms", cwd=self.repo), base)
        self.assertEqual(self.st().milestones["M1"]["status"], "closing")
        self.ok("set", "t1", "git.sign=false", "git.branch=bad..name")
        out = self.ok("ms", "close", "t1", "M1", "--next", "done")  # closed; the push problem is only reported
        self.assertIn("没有推送", out)
        self.assertEqual(self.st().milestones["M1"]["status"], "done")
        self.ok("set", "t1", "git.branch=feat/t1")
        self.assertIn("需要用户确认推送目标", self.ok("ms", "push", "t1"))

    def test_close_publishes_the_plan_as_of_close(self):
        tdir = self.ready()
        self.configure()
        self.edit(tdir, "L1", r"^SPEED = .*", "SPEED = 1.3")
        self.ok("try", "t1", "--lane", "L1", "--idea", "1.3")
        self.verify("L1")
        self.ok("ms", "plan", "t1", "M1", "--title", "speed 1.3", "--type", "perf", "--expect", "1.3", "--goal", "G1")
        self.ok("ms", "plan", "t1", "M2", "--title", "old plan", "--type", "perf", "--expect", "1.2", "--goal", "G1")
        self.ok("ms", "prep", "t1", "M1", "--lane", "L1")
        wt, d = tdir / "wt" / "_ms", tdir / "milestones" / "M1"
        self.assertIn("old plan", (wt / "docs" / "ENTRY.md").read_text())
        p = wt / "docs" / "ENTRY.md"
        p.write_text(p.read_text() + "\n设计说明\n")
        (d / "card.md").write_text("### M1\n**What**: raise SPEED\n**Next**: M2 old plan, then freeze\n**Notes**: kept\n")
        self.ok("ms", "review", "t1", "M1", "--session", "w", "--card", d / "card.md")
        # the plan changes after the review: what gets pushed must say so
        self.ok("ms", "plan", "t1", "M2", "--title", "split M2a", "--why", "split in two")
        self.ok("ms", "plan", "t1", "M3", "--title", "then M3", "--type", "perf", "--expect", "1.1", "--tentative")
        # e.g. a re-render after the plan changed: only mf's blocks differ, the review need not record again
        ms._fill_entry(self.st(), p, current=self.st().open_ms)
        self.ok("ms", "close", "t1", "M1", "--next", "M2")
        c = self.st().milestones["M1"]["close"]["commit"]
        msg = git("log", "-1", "--format=%B", c, cwd=self.repo)
        for needle in ("split M2a", "then M3", "Notes: kept", "Next: M2 split M2a", "Entry document (rendered"):
            self.assertIn(needle, msg)
        self.assertNotIn("**", msg)  # GitHub shows commit messages as plain text
        self.assertNotIn("old plan", msg)
        doc = git("show", f"{c}:docs/ENTRY.md", cwd=self.repo)
        self.assertIn("下一步（本提交的承诺）：M2 split M2a", doc)
        self.assertIn("设计说明", doc)
        self.assertNotIn("old plan", doc)
        self.assertIn("split M2a", self.ok("status", "t1", "M1"))  # shows the committed text
        tv = self.st().milestones["M1"]["roles"]["review"]["tree"]
        self.assertTrue(ms._blocks_only(self.st(), tv, git("rev-parse", f"{c}^{{tree}}", cwd=self.repo)))
        p.write_text(p.read_text() + "more prose\n")  # prose after the review is a real change
        ms.stage_worktree(wt, ms._ignore(self.st()))
        self.assertFalse(ms._blocks_only(self.st(), tv, ms.write_tree(wt)))

    def test_pyhip_publishes_in_chinese(self):
        self.repo = self.tmp / "pyhip"  # the language rule keys on the repository name
        subprocess.run(["bash", str(ROOT / "examples" / "toy" / "make_toy_repo.sh"), str(self.repo)], check=True,
                       capture_output=True)
        tdir = self.ready()
        t = self.st().task
        self.assertEqual((t["git"]["lang"], t["doc"]["lang"]), ("zh", "zh"))
        self.configure()
        self.edit(tdir, "L1", r"^SPEED = .*", "SPEED = 1.3")
        self.ok("try", "t1", "--lane", "L1", "--idea", "1.3")
        self.verify("L1")
        self.ok("ms", "plan", "t1", "M1", "--title", "提速到 1.3", "--type", "perf", "--expect", "1.3", "--goal", "G1")
        self.ok("ms", "plan", "t1", "M2", "--title", "预取", "--type", "perf", "--expect", "1.2", "--basis", "带宽")
        self.ok("ms", "prep", "t1", "M1", "--lane", "L1")
        wt, d = tdir / "wt" / "_ms", tdir / "milestones" / "M1"
        self.assertIn("**做了什么**", (d / "card.md").read_text())
        p = wt / "docs" / "ENTRY.md"
        p.write_text(p.read_text() + "\n设计说明：把 SPEED 提到 1.3，toy kernel 的三个 case 都更快。\n")
        (d / "card.md").write_text("### M1\n**What**: raise SPEED to 1.3\n**Why**: toy\n")
        rc, out = mf("ms", "review", "t1", "M1", "--session", "w", "--card", d / "card.md")
        self.assertEqual(rc, 3, out)
        self.assertIn("不是中文", out)
        (d / "card.md").write_text("### M1 · 提速到 1.3\n**做了什么**：把 `SPEED` 从 1.0 提到 1.3。\n"
                                   "**原理**：toy kernel 的耗时与 SPEED 成反比。\n")
        self.ok("ms", "review", "t1", "M1", "--session", "w", "--card", d / "card.md")
        self.ok("set", "t1", "git.lang=en")
        self.assertIn("lang_rule", {a["code"] for a in guard.alerts(self.st())})
        rc, out = mf("ms", "close", "t1", "M1", "--next", "M2")
        self.assertEqual(rc, 3, out)
        self.ok("set", "t1", "git.lang=zh")
        self.ok("ms", "close", "t1", "M1", "--next", "M2")
        msg = git("log", "-1", "--format=%B", self.st().milestones["M1"]["close"]["commit"], cwd=self.repo)
        for needle in ("下一步（mf 收尾时按计划卡生成）", "M2（承诺，性能）：预取；预期相对 M1 1.2×；依据：带宽", "入口文档（渲染版",
                       "性能：相对冻结基线", "规模：", "复核：w", "下一步：M2 预取（承诺）"):
            self.assertIn(needle, msg)
        for english in ("Perf:", "Size:", "Review:", "Next:"):
            self.assertNotIn(english, msg)

    def test_revoke_redo_and_replace_on_remote(self):
        self.ok("new", "b1", "--repo", self.repo, "--goal", "new op")
        self.ok("set", "b1", "commands.correctness=python3 test_kernel.py", "commands.bench=python3 bench.py",
                'scope.allow=["kernel.py"]', 'scope.protect=["bench.py","test_kernel.py"]', "gpu.pool=none",
                "freeze.repeats=2", "doc.entry=docs/ENTRY.md", 'tests.paths=["test_kernel.py"]',
                f"git.url={self.remote}", "git.branch=feat/b1")
        tdir = self.tmp / "home" / "tasks" / "b1"
        self.ok("brief", "b1", "--ask", "understood?")
        brief = self.st("b1").gates["spec"]["brief"]
        self.assertIn(f"推送目标：{self.remote} 分支 feat/b1", brief)  # user rule 11-22: one question, not two
        self.ok("decide", "b1", "--gate", "spec", "yes")
        self.ok("ms", "bootstrap", "b1")
        bwt = tdir / "wt" / "_bootstrap"
        (bwt / "newop.py").write_text("def newop(x):\n    return x  # wrong layout\n")
        self.ok("ms", "plan", "b1", "M0", "--type", "feature", "--title", "first try")
        self.ok("ms", "prep", "b1", "M0", "--bootstrap")
        self.roles("b1", "M0", tdir, tdir / "wt" / "_ms")
        self.assertIn("已推送 M0", self.ok("ms", "close", "b1", "M0", "--next", "await-user"))
        self.assertEqual([g for g in self.st("b1").pending_gates() if g["key"].startswith("push")], [])
        old = self.st("b1").milestones["M0"]["close"]["commit"]
        self.assertEqual(git("--git-dir", str(self.remote), "rev-parse", "feat/b1"), old)
        # taken back: integration branch, tag and bootstrap worktree go back to the base; the id is free again
        (bwt / "scratch.py").write_text("x = 1\n")
        rc, out = mf("ms", "revoke", "b1", "M0", "--why", "wrong layout")
        self.assertEqual(rc, 3, out)  # uncommitted work in the bootstrap worktree is not thrown away silently
        self.ok("ms", "revoke", "b1", "M0", "--why", "wrong layout", "--discard")
        st = self.st("b1")
        base = st.bootstrap["start"]
        self.assertEqual(git("rev-parse", "mf/b1/_ms", cwd=self.repo), base)
        self.assertEqual(git("rev-parse", "HEAD", cwd=bwt), base)
        self.assertEqual(git("tag", "-l", "b1-M0", cwd=self.repo), "")
        self.assertEqual((st.milestones["M0"]["status"], st.closed_ms), ("revoked", []))
        self.assertFalse((tdir / "milestones" / "M0" / "card.md").exists())  # archived under revoked-*
        # redo under the same id
        (bwt / "newop.py").write_text("def newop(x):\n    return x  # right layout\n")
        self.ok("ms", "plan", "b1", "M0", "--type", "feature", "--title", "second try")
        self.assertEqual(self.st("b1").milestones["M0"]["roles"], {})
        self.ok("ms", "prep", "b1", "M0", "--bootstrap")
        self.roles("b1", "M0", tdir, tdir / "wt" / "_ms")
        self.assertIn("覆盖远端需要用户批准", self.ok("ms", "close", "b1", "M0", "--next", "await-user"))
        new = self.st("b1").milestones["M0"]["close"]["commit"]
        self.assertEqual(git("--git-dir", str(self.remote), "rev-parse", "feat/b1"), old)  # nothing overwritten yet
        rkey = next(g["key"] for g in self.st("b1").pending_gates() if g["key"].startswith("push-replace:"))
        self.ok("brief", "b1", "--gate", rkey, "覆盖远端上已撤销的 M0 吗？")
        self.ok("decide", "b1", "--gate", rkey, "覆盖")
        self.assertIn("覆盖了已撤销的内容", self.ok("ms", "push", "b1"))
        self.assertEqual(git("--git-dir", str(self.remote), "rev-parse", "feat/b1"), new)
        self.assertEqual(git("--git-dir", str(self.remote), "rev-parse", "b1-M0^{commit}"), new)
        self.assertEqual(self.st("b1").pending_gates(), [])
        self.assertIn("已撤销 M0", self.ok("status", "b1"))

    def test_lean_closing_is_one_review_recorded_once(self):
        """User rule 11-21 (lean process, the default): closing = one independent review that fixes docs and adds
        missing tests itself, recorded once; anything changed after it blocks the close."""
        self.ok("new", "b1", "--repo", self.repo, "--goal", "new op")
        self.ok("set", "b1", "commands.correctness=python3 test_kernel.py", "commands.bench=python3 bench.py",
                'scope.allow=["kernel.py"]', 'scope.protect=["bench.py","test_kernel.py","tests/**"]', "gpu.pool=none",
                "doc.entry=docs/ENTRY.md", 'tests.paths=["test_kernel.py","tests/**"]')
        tdir = self.tmp / "home" / "tasks" / "b1"
        self.ok("brief", "b1", "--ask", "ok?")
        self.ok("decide", "b1", "--gate", "spec", "yes")
        self.ok("ms", "bootstrap", "b1")
        bwt, wt = tdir / "wt" / "_bootstrap", tdir / "wt" / "_ms"
        (bwt / "newop.py").write_text("def newop(x):\n    return x\n")
        (tdir / "notes").mkdir(exist_ok=True)
        (tdir / "notes" / "M0-report.md").write_text("did: newop\n")
        self.ok("ms", "plan", "b1", "M0", "--type", "feature", "--title", "newop")
        out = self.ok("ms", "prep", "b1", "M0", "--bootstrap")
        self.assertIn("myflow-review", out)
        self.assertNotIn("myflow-doc-verifier", out)
        self.assertIn("实现者报告 " + str(tdir / "notes" / "M0-report.md") + "、", out)
        d = tdir / "milestones" / "M0"
        for k in ("**Deviation from plan**", "**Coverage**", "**Known issues**"):
            self.assertIn(k, (d / "card.md").read_text())
        rc, out = mf("ms", "role", "b1", "M0", "--role", "review", "--result", "done", "--session", "rv", "--card", d / "card.md")
        self.assertEqual(rc, 3, out)
        self.assertIn("占位符", out)
        (d / "card.md").write_text("### M0\n**What**: newop\n**Coverage**: tests/test_newop.py\n")
        (wt / "kernel.py").write_text((wt / "kernel.py").read_text() + "# tweak\n")
        rc, out = mf("ms", "role", "b1", "M0", "--role", "review", "--result", "done", "--session", "rv", "--card", d / "card.md")
        self.assertEqual(rc, 3, out)  # the review fixes docs and tests, never runtime code
        self.assertIn("kernel.py", out)
        git("checkout", "HEAD", "--", "kernel.py", cwd=wt)
        (wt / "tests").mkdir(exist_ok=True)
        (wt / "tests" / "test_newop.py").write_text("from newop import newop\nassert newop(2) == 2\n")
        entry = wt / "docs" / "ENTRY.md"
        entry.write_text(entry.read_text() + "\n| newop(2) | tests/test_newop.py | 功能 | ✓ |\n")
        out = self.ok("ms", "role", "b1", "M0", "--role", "review", "--result", "done", "--session", "rv", "--card",
                      d / "card.md", "-m", "doc + 1 test")
        self.assertIn("可以收尾", out)
        self.assertEqual([g for g in self.st("b1").pending_gates() if g["key"].startswith("oracle@")], [])
        good = (d / "card.md").read_text()
        (d / "card.md").write_text(good + "9.99x\n")
        rc, out = mf("ms", "close", "b1", "M0", "--next", "await-user")
        self.assertEqual(rc, 3, out)
        self.assertIn("卡片在复核记录之后被改过", out)
        (d / "card.md").write_text(good)
        entry.write_text(entry.read_text() + "late edit\n")
        rc, out = mf("ms", "close", "b1", "M0", "--next", "await-user")
        self.assertEqual(rc, 3, out)
        self.assertIn("复核记录之后 worktree 又有改动", out)
        self.ok("ms", "role", "b1", "M0", "--role", "review", "--result", "done", "--session", "rv", "--card", d / "card.md")
        self.ok("ms", "close", "b1", "M0", "--next", "await-user")
        c = self.st("b1").milestones["M0"]["close"]["commit"]
        self.assertIn("Review: rv", git("log", "-1", "--format=%B", c, cwd=self.repo))
        self.assertIn("tests/test_newop.py", git("show", "--stat", "--format=", c, cwd=self.repo))
        self.assertIn("- M0 ✅ 本提交 · 功能 · newop", git("show", f"{c}:docs/ENTRY.md", cwd=self.repo))
        # changing an existing protected test needs a stated reason (user rule 11-22), not the user's approval
        (bwt / "newop.py").write_text("def newop(x):\n    return 2 * x\n")
        self.ok("ms", "plan", "b1", "M1", "--type", "feature", "--title", "newop v2")
        self.ok("ms", "prep", "b1", "M1", "--bootstrap")
        d = tdir / "milestones" / "M1"
        tk = wt / "test_kernel.py"
        tk.write_text(tk.read_text() + "assert True\n")
        (d / "card.md").write_text("### M1\n**What**: v2\n")
        rc, out = mf("ms", "role", "b1", "M1", "--role", "review", "--result", "done", "--session", "rv1", "--card", d / "card.md")
        self.assertEqual(rc, 3, out)
        self.assertIn("--tests-why", out)
        self.ok("ms", "role", "b1", "M1", "--role", "review", "--result", "done", "--session", "rv1", "--card", d / "card.md",
                "--tests-why", "只加一条断言，覆盖新行为")
        self.assertEqual(self.st("b1").pending_gates(), [])
        self.ok("ms", "close", "b1", "M1", "--next", "await-user")
        msg = git("log", "-1", "--format=%B", self.st("b1").milestones["M1"]["close"]["commit"], cwd=self.repo)
        self.assertIn("Changed existing tests: test_kernel.py; why (review): 只加一条断言，覆盖新行为", msg)

    def test_bootstrap_pipeline_next_milestone_starts_during_review(self):
        """User request 11-22: the next milestone starts right after prep; close keeps its work; the next prep merges the
        review's documents and tests in automatically."""
        self.ok("new", "b1", "--repo", self.repo, "--goal", "new op")
        self.ok("set", "b1", "commands.correctness=python3 test_kernel.py", "commands.bench=python3 bench.py",
                'scope.allow=["kernel.py"]', 'scope.protect=["bench.py","test_kernel.py","tests/**"]', "gpu.pool=none",
                "doc.entry=docs/ENTRY.md", 'tests.paths=["test_kernel.py","tests/**"]')
        tdir = self.tmp / "home" / "tasks" / "b1"
        self.ok("brief", "b1", "--ask", "ok?")
        self.ok("decide", "b1", "--gate", "spec", "yes")
        self.ok("ms", "bootstrap", "b1")
        bwt, wt = tdir / "wt" / "_bootstrap", tdir / "wt" / "_ms"
        (bwt / "newop.py").write_text("def newop(x):\n    return x\n")
        self.ok("ms", "plan", "b1", "M0", "--type", "feature", "--title", "newop")
        self.ok("ms", "plan", "b1", "M1", "--type", "feature", "--title", "newop v2")
        self.ok("ms", "prep", "b1", "M0", "--bootstrap")
        (bwt / "newop2.py").write_text("def newop2(x):\n    return 2 * x\n")  # M1 starts while M0 is reviewed
        d = tdir / "milestones" / "M0"
        (wt / "tests").mkdir(exist_ok=True)
        (wt / "tests" / "test_newop.py").write_text("from newop import newop\nassert newop(2) == 2\n")
        entry = wt / "docs" / "ENTRY.md"
        entry.write_text(entry.read_text() + "\nM0 一节\n")
        (d / "card.md").write_text("### M0\n**What**: newop\n")
        self.ok("ms", "review", "b1", "M0", "--session", "rv0", "--card", d / "card.md")
        self.assertIn("不重置", self.ok("ms", "close", "b1", "M0", "--next", "M1"))
        self.assertTrue((bwt / "newop2.py").is_file())  # the next milestone's work is untouched
        self.assertIn("流水线", self.ok("ms", "prep", "b1", "M1", "--bootstrap"))
        cand = self.st("b1").milestones["M1"]["prep"]["candidate"]
        files = git("ls-tree", "-r", "--name-only", cand, cwd=self.repo).splitlines()
        self.assertIn("tests/test_newop.py", files)  # the review's new test came along ...
        self.assertIn("newop2.py", files)  # ... with M1's code
        self.assertIn("M0 一节", git("show", f"{cand}:docs/ENTRY.md", cwd=self.repo))
        self.assertEqual(git("rev-parse", "HEAD", cwd=bwt), cand)  # the milestone after M1 continues from here

    def test_lane_pipeline_starts_from_the_closing_candidate(self):
        tdir = self.ready()
        self.configure()
        self.edit(tdir, "L1", r"^SPEED = .*", "SPEED = 1.3")
        self.ok("try", "t1", "--lane", "L1", "--idea", "1.3")
        self.ok("ms", "plan", "t1", "M1", "--title", "speed 1.3", "--type", "perf", "--expect", "1.3", "--goal", "G1")
        self.ok("ms", "plan", "t1", "M2", "--title", "speed 1.6", "--type", "perf", "--expect", "1.2", "--goal", "G1")
        self.ok("ms", "prep", "t1", "M1", "--lane", "L1")  # measures the candidate itself: no separate mf verify
        cand1 = self.st().milestones["M1"]["prep"]["candidate"]
        self.ok("lane", "new", "t1", "L2", "--direction", "more speed")  # while M1 is being reviewed
        self.assertEqual(self.st().lanes["L2"]["start"], cand1)
        self.edit(tdir, "L2", r"^SPEED = .*", "SPEED = 1.6")
        self.ok("try", "t1", "--lane", "L2", "--idea", "1.6")
        self.roles("t1", "M1", tdir, tdir / "wt" / "_ms")
        self.ok("ms", "close", "t1", "M1", "--next", "M2")
        out = self.ok("ms", "prep", "t1", "M2", "--lane", "L2")
        self.assertIn("流水线", out)
        self.assertIn("三方交替", out)
        st = self.st()
        prep, c1 = st.milestones["M2"]["prep"], st.milestones["M1"]["close"]["commit"]
        self.assertTrue(ms.is_ancestor(self.repo, c1, prep["candidate"]))  # M1's documents are in
        self.assertAlmostEqual(prep["inc"]["speedup"], 1.6 / 1.3, delta=0.06)

    def test_bootstrap_new_operator_then_freeze_at_m0(self):
        entry = "docs/设计.md"  # non-ASCII paths must survive git's path quoting
        self.ok("new", "b1", "--repo", self.repo, "--goal", "new op")
        self.ok("set", "b1", "commands.correctness=python3 test_kernel.py", "commands.bench=python3 bench.py",
                'scope.allow=["kernel.py"]', 'scope.protect=["bench.py","test_kernel.py"]', "gpu.pool=none",
                "freeze.repeats=2", f"doc.entry={entry}", 'tests.paths=["test_kernel.py"]')
        tdir = self.tmp / "home" / "tasks" / "b1"
        rc, out = mf("ms", "bootstrap", "b1")
        self.assertEqual(rc, 3, out)  # build the functional version only after the requirements are confirmed
        self.ok("brief", "b1", "--ask", "understood?")
        self.ok("decide", "b1", "--gate", "spec", "yes")
        self.ok("ms", "bootstrap", "b1")
        self.assertEqual(self.st("b1").phase, "bootstrap")  # not "still aligning requirements" for hours
        (tdir / "wt" / "_bootstrap" / "newop.py").write_text("def newop(x):\n    return x\n")
        out = self.ok("ms", "plan", "b1", "M0", "--type", "feature", "--title", "functional newop", "--ask")
        self.assertIn("gate【bootstrap】", out)  # a functional-version plan, not the R1 performance plan
        self.ok("decide", "b1", "--gate", "bootstrap", "ok")
        self.ok("ms", "prep", "b1", "M0", "--bootstrap")
        self.roles("b1", "M0", tdir, tdir / "wt" / "_ms", entry=entry)
        out = self.ok("ms", "close", "b1", "M0", "--next", "await-user")
        self.assertIn("未配置 git.url", out)
        m0 = self.st("b1").milestones["M0"]["close"]["commit"]
        self.assertEqual(git("show", f"{m0}:newop.py", cwd=self.repo).splitlines()[0], "def newop(x):")
        self.assertIn("设计说明", git("show", f"{m0}:{entry}", cwd=self.repo))
        # a second pre-freeze feature milestone builds on M0 (the bootstrap worktree was aligned to it)
        bwt = tdir / "wt" / "_bootstrap"
        self.assertEqual(git("rev-parse", "HEAD", cwd=bwt), m0)
        (bwt / "newop.py").write_text("def newop(x):\n    return 2 * x\n")
        self.ok("ms", "plan", "b1", "M1", "--type", "feature", "--title", "newop v2")
        self.ok("ms", "prep", "b1", "M1", "--bootstrap")
        self.roles("b1", "M1", tdir, tdir / "wt" / "_ms", entry=entry, doc_line="第二版")
        self.ok("ms", "close", "b1", "M1", "--next", "await-user")
        m1 = self.st("b1").milestones["M1"]["close"]["commit"]
        self.assertEqual(git("rev-parse", f"{m1}^", cwd=self.repo), m0)
        self.ok("freeze", "b1")
        self.assertEqual(self.st("b1").freeze["base"], m1)
        self.assertEqual(self.st("b1").phase, "freeze")
        self.assertFalse(self.st("b1").gate_ok("plan", 1))  # still has to ask the user for the performance plan
        self.ok("lane", "new", "b1", "L1", "--direction", "speed")
        self.assertEqual(self.st("b1").lanes["L1"]["start"], m1)


class RelayAndProgressTest(MilestoneBase):
    """The user must actually be asked, and silence must be explained by evidence (not by the agent's prose)."""

    def test_gate_opened_by_mf_must_be_relayed(self):
        tdir = self.ready()
        self.configure()
        self.edit(tdir, "L1", r"^SPEED = .*", "SPEED = 1.3")
        self.ok("try", "t1", "--lane", "L1", "--idea", "1.3")
        self.verify("L1")
        self.ok("ms", "plan", "t1", "M1", "--title", "speed 1.3", "--type", "perf", "--expect", "1.3", "--goal", "G1")
        self.ok("ms", "prep", "t1", "M1", "--lane", "L1")
        self.roles("t1", "M1", tdir, tdir / "wt" / "_ms")
        self.ok("ms", "close", "t1", "M1", "--next", "await-user")
        st = self.st()
        key = next(g["key"] for g in st.pending_gates() if g["key"].startswith("push:"))
        self.assertIsNone(st.gates[key]["asked_t"])  # mf opened it: recorded, but nobody asked the user yet
        now = time.time()
        self.assertEqual({a["code"]: a["level"] for a in guard.alerts(st, now)}.get("gate_unasked"), "warn")
        self.assertEqual({a["code"]: a["level"] for a in guard.alerts(st, now + 31 * 60)}.get("gate_unasked"), "crit")
        self.assertIn("还没问用户", self.ok("note", "t1", "waiting for the review"))
        self.assertNotIn("还没问用户", self.ok("note", "t1", "--lane", "L1", "x"))  # a lane agent cannot ask the user
        rc, out = mf("brief", "t1", "--gate", "push:nope", "推送？")
        self.assertEqual(rc, 2, out)
        self.ok("brief", "t1", "--gate", key, "M1 推送到 feat/t1？")
        st = self.st()
        self.assertEqual(st.gates[key]["asked_via"], "chat")
        al = guard.alerts(st, now + 3 * 3600)
        codes = {a["code"] for a in al}
        self.assertNotIn("gate_unasked", codes)
        self.assertIn("问过你", next(a["msg"] for a in al if a["code"] == "gate"))
        self.assertIn("await_user", codes)  # silent because it waits for the user, not because it is stuck
        self.assertNotIn("stale", codes)
        self.ok("decide", "t1", "--gate", key, "推吧")
        rc, out = mf("brief", "t1", "--gate", key, "推送？")
        self.assertEqual(rc, 2, out)  # no longer pending

    def test_unreported_progress_spin_and_silent_work_are_told_apart(self):
        tdir = self.ready(spin_min=0.02)  # 1.2 s so the test can wait it out
        self.ok("brief", "t1", "plan approved; starting L1")
        self.ok("try", "t1", "--lane", "L1", "--idea", "x")
        later = time.time() + 2 * 3600
        al = {a["code"]: a for a in guard.alerts(self.st(), later)}
        self.assertEqual(al["no_report"]["level"], "warn")
        self.assertIn("不是空转", al["no_report"]["msg"])
        self.ok("brief", "t1", "--lane", "L1", "lane note")  # a lane brief is not the orchestrator telling the user
        self.assertIn("no_report", {a["code"] for a in guard.alerts(self.st(), later)})
        self.ok("brief", "t1", "A1 measured; next: tile 256")
        self.assertNotIn("no_report", {a["code"] for a in guard.alerts(self.st(), later)})
        self.assertIn("最后向你汇报：", render.status_md(self.st()))
        # alive (still calling mf) but no recorded progress and no file changes for longer than spin_min
        time.sleep(1.5)
        self.ok("note", "t1", "--lane", "L1", "still thinking")
        self.assertEqual({a["code"]: a["level"] for a in guard.alerts(self.st())}.get("spin"), "warn")
        # files changing in a worktree: working, the step is just not recorded yet
        p = tdir / "wt" / "L1" / "kernel.py"
        p.write_text(p.read_text() + "# wip\n")
        guard._EVIDENCE.clear()  # same events, new files: drop the per-process scan cache
        codes = {a["code"]: a["level"] for a in guard.alerts(self.st())}
        self.assertNotIn("spin", codes)
        self.assertEqual(codes.get("no_record"), "info")
        # no mf call for 2 h, but the worktree changed a minute ago: silent, not stuck
        t2 = time.time() + 2 * 3600
        os.utime(p, (t2 - 60, t2 - 60))
        guard._EVIDENCE.clear()
        codes = {a["code"]: a["level"] for a in guard.alerts(self.st(), t2)}
        self.assertEqual(codes.get("no_record"), "warn")
        self.assertNotIn("stale", codes)
        os.utime(p, (time.time() - 10, time.time() - 10))
        (tdir / "wt" / "_baseline" / "scratch.txt").write_text("mf-managed worktrees are no evidence\n")
        guard._EVIDENCE.clear()
        self.assertEqual({a["code"]: a["level"] for a in guard.alerts(self.st(), t2)}.get("stale"), "crit")

    def test_phases_follow_actions_and_plan_is_asked_only_off_promise(self):
        """User request 11-22: no manual phase steps; the plan is shown, and asked only when it leaves the promise."""
        tdir, wt, c1 = self.milestone1()  # M1 closed and pushed with the promise "next: M2"
        self.assertEqual(self.st().phase, "integrate")  # prep moved it
        self.ok("ms", "plan", "t1", "M2", "--title", "speed 1.6", "--hours", "2")  # an update in the same direction
        self.assertEqual((self.st().phase, self.st().round), ("plan", 2))  # a new round starts
        self.ok("lane", "new", "t1", "L2", "--direction", "more speed")
        st = self.st()
        self.assertEqual(st.phase, "implement")
        self.assertEqual(st.gates["plan@R2"]["state"], "approved")
        self.assertIn("只通知", st.decisions[-1]["text"])
        self.assertEqual(st.pending_gates(), [])
        self.edit(tdir, "L2", r"^SPEED = .*", "SPEED = 1.6")
        self.ok("try", "t1", "--lane", "L2", "--idea", "1.6")

    def test_plan_that_leaves_the_promise_is_asked(self):
        tdir, wt, c1 = self.milestone1()
        self.ok("ms", "plan", "t1", "M2", "--title", "analysis first", "--why", "换方向：先做分析")
        self.ok("lane", "new", "t1", "L2", "--direction", "analysis")
        self.assertEqual(self.st().phase, "plan")  # the gate holds the phase
        rc, out = mf("try", "t1", "--lane", "L2", "--idea", "x")
        self.assertEqual(rc, 3, out)
        self.assertIn("改过计划", out)
        out = self.ok("ms", "plan", "t1", "M2", "--ask")
        self.assertIn("gate【plan@R2】已挂起", out)
        self.ok("decide", "t1", "--gate", "plan@R2", "同意先做分析")
        self.ok("try", "t1", "--lane", "L2", "--idea", "x")

    def test_await_returns_when_the_orchestrator_must_act(self):
        tdir = self.ready()
        out = self.ok("await", "t1", "--max-min", "0.003", "--poll-s", "0.05")  # ~0.2 s with nothing to do
        self.assertIn("没有需要编排者处理的事", out)
        self.ok("steer", "t1", "please also check T=48")  # a user message for the orchestrator
        self.assertIn("用户留言 #1", self.ok("await", "t1", "--max-min", "5", "--poll-s", "0.05"))
        self.ok("note", "t1", "--ack", "1", "will do")
        append_event(tdir, "brief", "confirm the push target", by="mf", phase="push:x", round=1, ask=True)
        self.assertIn("还没问用户", self.ok("await", "t1", "--max-min", "5", "--poll-s", "0.05"))
        n = len(self.st().events)  # await itself records nothing (it is not agent activity)
        self.ok("await", "t1", "--max-min", "5", "--poll-s", "0.05")
        self.assertEqual(len(self.st().events), n)

    def test_delegation_decides_by_default_and_lists_it(self):
        """User 11-21: "for the next 10 hours, decide questions by the defaults" -- such decisions stay listed."""
        tdir = self.ready()
        rc, out = mf("decide", "t1", "--default", "按推荐项")
        self.assertEqual(rc, 3, out)  # no delegation in force: the user must be asked
        words = "接下来的10个小时中如有问题要确认按照默认值进行"
        self.ok("decide", "t1", "--delegate", "10", words)
        ms._open_gate(self.st(), "push:abcd1234", "推送到 X？")
        al = {a["code"]: a for a in guard.alerts(self.st())}
        self.assertIn("--default", al["gate_unasked"]["msg"])  # await still wakes the orchestrator to decide it
        self.assertIn("delegated", al)
        self.ok("decide", "t1", "--gate", "push:abcd1234", "--default", "推送到已批准的目标（推荐项）")
        st = self.st()
        self.assertEqual(st.gates["push:abcd1234"]["state"], "approved")
        self.assertEqual([(d["text"], d["quote"]) for d in st.default_decisions()], [("推送到已批准的目标（推荐项）", words)])
        self.assertIn("按默认值代你决定", (tdir / "STATUS.md").read_text())
        self.assertIn("决策（按默认值·用户授权）", (tdir / "STATUS.md").read_text())
        self.ok("decide", "t1", "--delegate", "0", "回来了")
        rc, out = mf("decide", "t1", "--default", "x")
        self.assertEqual(rc, 3, out)
        self.assertIn("推送到已批准的目标", render.status_md(self.st()))  # still listed for the user after it ended
        self.assertNotIn("delegated", {a["code"] for a in guard.alerts(self.st())})

    def test_long_spec_phase_is_pace_not_spin(self):
        self.ok("new", "s1", "--repo", self.repo)
        later = time.time() + 5 * 3600
        self.assertNotIn("no_report", {a["code"] for a in guard.alerts(self.st("s1"), later)})  # creating it is not progress
        self.ok("note", "s1", "--kind", "survey", "read the upstream kernels")
        al = {a["code"]: a for a in guard.alerts(self.st("s1"), later)}
        self.assertEqual(al["phase_long"]["level"], "info")
        self.assertNotIn("打转", al["phase_long"]["msg"])
        self.assertIn("还从没汇报过", al["no_report"]["msg"])

    def test_stale_push_gates_and_test_reasons_follow_the_content(self):
        tdir = self.ready()
        self.configure()
        self.edit(tdir, "L1", r"^SPEED = .*", "SPEED = 1.3")
        self.ok("try", "t1", "--lane", "L1", "--idea", "1.3")
        self.verify("L1")
        self.ok("ms", "plan", "t1", "M1", "--title", "speed 1.3", "--type", "perf", "--expect", "1.3", "--goal", "G1")
        self.ok("ms", "prep", "t1", "M1", "--lane", "L1")
        wt = tdir / "wt" / "_ms"
        tk = wt / "test_kernel.py"
        base_tk = tk.read_text()
        d = tdir / "milestones" / "M1"
        (d / "card.md").write_text("### M1\n**What**: raise SPEED\n")
        tk.write_text(base_tk + "assert kernel.run([1]) == [2]\n")
        self.ok("ms", "review", "t1", "M1", "--session", "rv", "--card", d / "card.md", "--tests-why", "加 [1]")
        first = self.st().milestones["M1"]["roles"]["review"]["tests_key"]
        tk.write_text(base_tk + "assert kernel.run([1]) == [2]\nassert kernel.run([0]) == [0]\n")
        rc, out = mf("ms", "review", "t1", "M1", "--session", "rv", "--card", d / "card.md")
        self.assertEqual(rc, 3, out)  # the reason given for the old content does not carry over
        self.ok("ms", "review", "t1", "M1", "--session", "rv", "--card", d / "card.md", "--tests-why", "加 [0]")
        self.assertNotEqual(self.st().milestones["M1"]["roles"]["review"]["tests_key"], first)
        self.assertEqual(self.st().pending_gates(), [])  # no question for the user
        self.ok("ms", "close", "t1", "M1", "--next", "await-user")
        old = next(g["key"] for g in self.st().pending_gates() if g["key"].startswith("push:"))
        self.ok("set", "t1", "git.branch=feat/other")  # the user picked another target before approving
        self.assertIn("需要用户确认推送目标", self.ok("ms", "push", "t1"))
        st = self.st()
        self.assertEqual(st.gates[old]["state"], "superseded")  # the old question stops asking
        new = next(g["key"] for g in st.pending_gates() if g["key"].startswith("push:"))
        self.ok("brief", "t1", "--gate", new, "改推到 feat/other？")
        self.ok("decide", "t1", "--gate", new, "推到 feat/other")
        self.assertIn("已推送 M1", self.ok("ms", "push", "t1"))
        self.assertEqual(self.st().pending_gates(), [])


class SettingsAndRenderingTest(Base):
    """User requests 11-22: configurable data locations; plain-text commit messages that link the rendered entry
    document; Markdown rendered in mf serve."""

    def test_config_sets_where_task_data_lives(self):
        other = self.tmp / "elsewhere"
        out = self.ok("config", f"tasks_dir={other}")
        self.assertIn(f"tasks_dir  {other}", out)
        self.assertIn("配置文件", out)
        self.ok("new", "c1", "--repo", self.repo, "--goal", "g")
        self.assertTrue((other / "c1" / "task.json").is_file())
        self.assertEqual(task_dir_of("c1"), other / "c1")
        os.environ["MYFLOW_TASKS_DIR"] = str(self.tmp / "env-wins")
        self.assertIn("环境变量 MYFLOW_TASKS_DIR", self.ok("config"))
        del os.environ["MYFLOW_TASKS_DIR"]
        # moving home takes the task list along; task directories stay where they are
        h1, h2 = self.tmp / "home", self.tmp / "home2"
        self.ok("config", f"home={h1}")
        del os.environ["MYFLOW_HOME"]
        self.assertIn("登记了原有的 1 个任务", self.ok("config", f"home={h2}"))
        self.assertTrue((h2 / "registry.json").is_file())
        self.assertEqual(task_dir_of("c1"), other / "c1")
        # --fresh: an isolated new environment (e.g. redoing a task from scratch): nothing carried over
        h3, kb = self.tmp / "home3", self.tmp / "kb"
        self.assertIn("不登记原有任务", self.ok("config", f"home={h3}", f"kb={kb}", "--fresh"))
        self.assertIn("0 task(s)", self.ok("status"))
        self.ok("kb", "new", "fresh-card", "--kind", "pitfall")
        self.assertTrue((kb / "cards" / "fresh-card.md").is_file())  # cards go to the configured kb, not myflow's own
        self.ok("config", "--unset", "home", "--unset", "tasks_dir", "--unset", "kb")
        os.environ["MYFLOW_HOME"] = str(h1)

    def test_entry_link_and_markdown_rendering(self):
        self.ok("new", "c2", "--repo", self.repo, "--goal", "g")
        self.ok("set", "c2", "doc.entry=docs/设计.md", "git.url=https://github.com/o/r.git")
        st = State(task_dir_of("c2"))
        self.assertEqual(ms.entry_url(st, "M3"), "https://github.com/o/r/blob/c2-M3/docs/%E8%AE%BE%E8%AE%A1.md")
        card = ("### M1 · 标题\n**做了什么**: 见 [代码](src/a.py#L3)\n```diff\n- a\n+ b\n```\n"
                "| a | b |\n|---|---|\n| 1 | 2 |\n- x\n  - y\n<script>alert(1)</script> [j](javascript:alert(1))\n"
                "> - [`fused_moe`](moe.py#L66) 与 __init__.py，[`x`](javascript:1)\n"
                "<details><summary>逐 case（2 个）</summary>\n\n| c | v |\n|---|---|\n| a | 1 |\n\n</details>\n<details x=1>\n")
        text = render.md_text(card)
        self.assertNotIn("**", text)
        self.assertIn("做了什么: 见 代码（src/a.py#L3）", text)
        self.assertIn("    - a", text)  # code stays readable, indented
        page = render.md_html(card)
        for needle in ("<h3 id=", "<b>做了什么</b>", "<a href='src/a.py#L3'>代码</a>", "<pre><code>- a\n+ b</code></pre>",
                       "<th>a</th>", "<td>2</td>", "<ul>", "&lt;script&gt;"):
            self.assertIn(needle, page)
        self.assertIn("<a href='moe.py#L66'><code>fused_moe</code></a> 与 __init__.py", page)  # code inside link text
        self.assertIn("[<code>x</code>](javascript:1)", page)
        self.assertNotIn("\x00", page)
        self.assertIn("__init__.py", text)
        self.assertNotIn("<script>", page)
        self.assertIn("<details><summary>逐 case（2 个）</summary>\n<table>", page)  # a fold is the one HTML let through
        self.assertIn("</details>", page)
        self.assertIn("&lt;details x=1&gt;", page)
        self.assertNotIn("href='javascript", page)

    def test_serve_renders_markdown(self):
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer

        from myflow import serve

        self.ok("new", "c3", "--repo", self.repo, "--goal", "g")
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{httpd.server_address[1]}/t/c3/SPEC.md"
            with urllib.request.urlopen(base) as r:
                self.assertIn("text/html", r.headers["Content-Type"])
                self.assertIn("<h1 id=", r.read().decode("utf-8"))
            with urllib.request.urlopen(base + "?raw=1") as r:
                self.assertIn("text/plain", r.headers["Content-Type"])
                self.assertTrue(r.read().decode("utf-8").startswith("#"))
        finally:
            httpd.shutdown()
            httpd.server_close()


class CliAndDirectionTest(Base):
    def test_parser_and_run_still_work(self):
        p = cli.build_parser()
        self.assertEqual(p.parse_args(["status", "t1", "M3"]).item, "M3")
        self.assertEqual(p.parse_args(["run", "t1", "echo", "x"]).cmd, ["echo", "x"])
        self.assertEqual(p.parse_args(["ms", "review", "t1", "M1", "--session", "s", "-m", "ok"]).summary, "ok")
        self.ready()
        out = self.ok("run", "t1", "--lane", "L1", "--gpu", "none", "--", "echo", "hi")
        self.assertIn("hi", out)
        self.assertEqual(self.st().notes[-1]["lane"], "L1")  # options before -- are options, not the command
        # several arguments keep their boundaries; one argument is a shell string
        self.assertIn("/", self.ok("run", "t1", "--gpu", "none", "--", "bash", "-lc", "cd / && pwd").splitlines())
        self.assertIn("/", self.ok("run", "t1", "--gpu", "none", "--", "cd / && pwd").splitlines())

    def test_disagreeing_repeated_verifies_are_not_picked(self):
        tdir = self.ready()
        self.ok("try", "t1", "--lane", "L1", "--idea", "x")
        st = self.st()
        commit, seq = st.attempt(1)["commit"], st.freeze["seq"]
        for verdict in ("no_gain", "improved", "improved"):
            append_event(tdir, "verify", "", lane="L1", by="mf", counter="verify", attempt=1, commit=commit,
                         verdict=verdict, speedup=1.05, against="abc", against_ms="M1", freeze_seq=seq)
        self.ok("try", "t1", "--lane", "L1", "--idea", "same code again")  # a fresh attempt number, same commit
        self.assertEqual(self.st().attempt(2)["commit"], commit)
        _, problem = ms._pick_verify(self.st(), 2, against="abc")
        self.assertIn("不一致", problem or "")
        self.assertIn("verify_shopping", {a["code"] for a in guard.alerts(self.st())})

    def test_quality_signal_is_the_entry_document_budget(self):
        """User request 11-22: the entry document is a design document a tech lead reads in 10 minutes: within its line
        budget, one thing per line, no process traces. mf's blocks and the template's comment hints are not read."""
        tdir = self.ready()
        self.ok("set", "t1", "doc.entry=docs/ENTRY.md", "doc.max_lines=5")
        append_event(tdir, "ms.plan", "title M1", id="M1", type="perf", expect=1.1, commitment="committed")
        doc = tdir / "wt" / "_ms" / "docs" / "ENTRY.md"
        doc.parent.mkdir(parents=True)
        doc.write_text("# x\n<!-- mf:perf -->\n" + "generated\n" * 50 + "<!-- /mf:perf -->\n"
                       "<!-- a hint\n  for the writer -->\n[link](" + "y" * 200 + ") prose\n")

        def quality():
            return next(d for d in guard.direction(self.st()) if d["code"] == "quality")

        self.assertEqual(quality()["level"], "ok", quality())  # 2 lines a reader sees; a link target is not read
        doc.write_text(doc.read_text() + "more\n" * 3 + "x" * 130 + "\nsee runs/abc/out.log and V3\n")
        q = quality()
        self.assertEqual(q["level"], "warn")
        for needle in ("7 行（不含 mf 生成块）> 预算 5", "1 行超过 120 字（最长 130", "runs/ 日志路径 ×1", "（A7、V3） ×1"):
            self.assertIn(needle, q["msg"])
        self.assertEqual({a["code"]: a["level"] for a in guard.alerts(self.st())}.get("ms_quality"), "warn")

    def test_promise_prediction_and_plateau_signals(self):
        tdir = self.ready()
        for mid in ("M1", "M2", "M3"):
            append_event(tdir, "ms.plan", f"title {mid}", id=mid, type="perf", expect=1.2, commitment="committed")
        for mid, nxt in (("M1", ["M2"]), ("M3", ["done"])):
            v = {"v": 1, "speedup": 1.03, "verdict": "improved", "noise": 0.001, "pairs": 2}
            append_event(tdir, "ms.prep", "", by="mf", id=mid, inc=v, cum=v, size={"lines": 10}, tree="x",
                         parent="p", candidate="c", runtime_tree="r", wt=str(tdir))
            append_event(tdir, "ms.close", "", by="mf", id=mid, commit=f"c{mid}", tag=f"t1-{mid}", next=nxt)
        codes = {a["code"]: a["level"] for a in guard.alerts(self.st())}
        self.assertEqual(codes.get("ms_promise"), "warn")   # M1 promised M2, M3 was done instead, no reason
        self.assertEqual(codes.get("ms_predict"), "warn")   # 15% of the expected gain, twice
        self.assertEqual(codes.get("ms_plateau"), "ask")    # two marginal perf milestones, target not met
        self.assertEqual(codes.get("ms_goal"), "warn")      # neither says which goal it serves


if __name__ == "__main__":
    unittest.main()
