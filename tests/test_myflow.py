"""myflow self-tests (CPU only, ~30 s): run with `mf selftest` or `python3 -m unittest discover -s tests`."""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from myflow import cli, guard, kb, render  # noqa: E402
from myflow.core import State, task_dir_of  # noqa: E402
from myflow.runner import Lease, parse_metrics, speedup  # noqa: E402


def mf(*args) -> tuple:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main([str(a) for a in args])
    return rc, out.getvalue() + err.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mf-test-"))
        self._env = dict(os.environ)
        os.environ["MYFLOW_HOME"] = str(self.tmp / "home")
        os.environ["MYFLOW_CONFIG"] = str(self.tmp / "config.json")  # never the user's real config
        for k in ("MYFLOW_TASKS_DIR", "MYFLOW_LOCK_DIR", "MYFLOW_KB"):
            os.environ.pop(k, None)
        os.environ["MYFLOW_SMI_CMD"] = "echo '{}'"
        self.repo = self.tmp / "repo"
        subprocess.run(["bash", str(ROOT / "examples" / "toy" / "make_toy_repo.sh"), str(self.repo)], check=True,
                       capture_output=True)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ok(self, *args):
        rc, out = mf(*args)
        self.assertEqual(rc, 0, f"mf {' '.join(map(str, args))} -> {rc}\n{out}")
        return out

    def ready(self, tid="t1", confirm=True, **budget):
        """A frozen task with lane L1; confirm: the user approved the requirements and the R1 plan (no phase steps:
        the phases follow the actions)."""
        self.ok("new", tid, "--repo", self.repo, "--goal", "toy 1.5x", "--target", "1.5")
        sets = ["commands.correctness=python3 test_kernel.py", "commands.bench=python3 bench.py",
                'scope.allow=["kernel.py"]', 'scope.protect=["bench.py","test_kernel.py"]', "gpu.pool=none",
                "verify.pairs=2", "freeze.repeats=2"] + [f"budget.{k}={v}" for k, v in budget.items()]
        self.ok("set", tid, *sets)
        if confirm:
            self.ok("brief", tid, "--ask", "understood?")
            self.ok("decide", tid, "--gate", "spec", "yes")
        self.ok("freeze", tid)
        if confirm:
            self.ok("brief", tid, "--ask", "plan R1")
            self.ok("decide", tid, "--gate", "auto", "go")
        self.ok("lane", "new", tid, "L1", "--direction", "raise SPEED")
        return task_dir_of(tid)

    def verify(self, lane="L1", n=None, tid="t1") -> str:
        """The independent re-measurement `mf ms prep` runs on its candidate (fresh seed, ABBA vs the frozen base)."""
        st = self.st(tid)
        at = st.attempt(n) if n else st.lanes[lane]["best_self"]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.verify_commit(st, lane, at)
        return out.getvalue()

    def edit(self, tdir, lane, old, new, fname="kernel.py"):
        p = tdir / "wt" / lane / fname
        p.write_text(re.sub(old, new, p.read_text(), flags=re.M))

    def st(self, tid="t1") -> State:
        return State(task_dir_of(tid))


class FlowTest(Base):
    def test_gates_block_until_user_confirms_and_stop_ends(self):
        self.ready(confirm=False)
        rc, out = mf("try", "t1", "--lane", "L1", "--idea", "x")
        self.assertEqual(rc, 3, out)
        self.assertIn("需求尚未经用户确认", out)
        self.assertEqual(self.st().phase, "spec")  # actions do not move past an unanswered question
        self.ok("brief", "t1", "--ask", "my understanding ...")
        self.assertEqual(len(self.st().pending_gates()), 1)
        self.ok("decide", "t1", "--gate", "spec", "ok", "--quote", "go ahead")
        self.assertTrue(self.st().gate_ok("spec", 1))
        rc, out = mf("report", "t1", "--done")
        self.assertEqual(rc, 3, out)  # the R1 plan was never confirmed
        self.ok("steer", "t1", "--stop", "enough for now")
        rc, out = mf("try", "t1", "--lane", "L1", "--idea", "x")
        self.assertEqual(rc, 3, out)
        self.assertIn("已请求停止", out)
        self.ok("report", "t1", "--done")  # after a stop the open plan question no longer blocks the end
        self.assertTrue(self.st().done())

    def test_full_flow_try_verify_report_done(self):
        tdir = self.ready()
        self.edit(tdir, "L1", r"^SPEED = .*", "SPEED = 1.5")
        out = self.ok("try", "t1", "--lane", "L1", "--idea", "speed 1.5")
        self.assertIn("A1 [L1] 通过", out)
        st = self.st()
        a = st.attempt(1)
        self.assertEqual(a["status"], "pass")
        self.assertAlmostEqual(a["speedup"], 1.5, delta=0.05)
        out = self.verify("L1")
        st = self.st()
        v = st.verifies[-1]
        self.assertEqual(v["verdict"], "improved", out)
        self.assertAlmostEqual(v["speedup"], 1.5, delta=0.05)
        self.assertFalse(v.get("mismatch"))
        self.assertGreaterEqual(st.progress(), 0.9)
        self.assertIn("1.5", self.ok("status", "t1", "A1"))
        out = self.ok("report", "t1", "--done")
        self.assertIn("审计通过", out)
        self.assertTrue(self.st().done())
        self.assertIn("1.5", (tdir / "REPORT.md").read_text())
        self.assertTrue((tdir / "final.patch").read_text().strip())
        html = (tdir / "dashboard.html").read_text()
        for needle in ("性能轨迹", "Lanes", "独立复核", "A1"):
            self.assertIn(needle, html)
        self.assertIn("## 性能", (tdir / "STATUS.md").read_text())

    def test_stale_pycache_is_not_measured(self):
        tdir = self.ready()
        self.edit(tdir, "L1", r"^SPEED = .*", "SPEED = 1.3")
        self.ok("try", "t1", "--lane", "L1", "--idea", "1.3")
        self.edit(tdir, "L1", r"^SPEED = .*", "SPEED = 1.6")  # same size, same second
        self.ok("try", "t1", "--lane", "L1", "--idea", "1.6")
        self.assertAlmostEqual(self.st().attempt(2)["speedup"], 1.6, delta=0.05)


class AntiCheatTest(Base):
    def test_oracle_and_scope_violations(self):
        tdir = self.ready()
        self.edit(tdir, "L1", r"base_us / kernel.SPEED", "base_us / kernel.SPEED / 3", fname="bench.py")
        self.ok("try", "t1", "--lane", "L1", "--idea", "edit bench")
        self.assertEqual(self.st().attempt(1)["status"], "oracle_modified")
        subprocess.run(["git", "checkout", "bench.py"], cwd=tdir / "wt" / "L1", check=True)
        (tdir / "wt" / "L1" / "README.md").write_text("changed\n")
        self.ok("try", "t1", "--lane", "L1", "--idea", "edit readme")
        self.assertEqual(self.st().attempt(2)["status"], "scope_violation")
        codes = {a["code"] for a in guard.alerts(self.st())}
        self.assertIn("oracle", codes)

    def test_overclaim_from_local_artifact_is_caught(self):
        tdir = self.ready()
        (tdir / "wt" / "L1" / ".boost").write_text("")  # git-ignored, never committed
        self.ok("try", "t1", "--lane", "L1", "--idea", "local trick")
        self.assertAlmostEqual(self.st().attempt(1)["speedup"], 1.3, delta=0.05)
        self.verify("L1")
        v = self.st().verifies[-1]
        self.assertEqual(v["verdict"], "no_gain")
        self.assertTrue(v["mismatch"])
        self.assertIn("overclaim", {a["code"] for a in guard.alerts(self.st())})

    def test_tampering_is_detected(self):
        tdir = self.ready()
        self.edit(tdir, "L1", r"^SPEED = .*", "SPEED = 1.2")
        self.ok("try", "t1", "--lane", "L1", "--idea", "x")
        self.assertEqual(cli.audit_problems(self.st()), [])
        p = tdir / "events.jsonl"
        lines = p.read_text().splitlines()
        for i, line in enumerate(lines):
            e = json.loads(line)
            if e["kind"] == "attempt":
                e["data"]["speedup"] = 9.9
                lines[i] = json.dumps(e, ensure_ascii=False, separators=(",", ":"))
        p.write_text("\n".join(lines) + "\n")
        rc, out = mf("report", "t1")  # the report re-derives every number and says what does not match
        self.assertEqual(rc, 1, out)
        self.assertIn("does not match .events.head", out)
        self.assertIn("!= recomputed", out)
        # the report itself appended events after the edited line: the evidence must survive that
        self.assertIn("tamper", {a["code"] for a in guard.alerts(self.st())})

    def test_audit_follows_each_freeze_and_reports_missing_logs(self):
        """mf report re-derives each number under the freeze it was measured against: a re-freeze (new baseline, new
        parse contract) is not an edit; a missing raw log is reported, not a crash."""
        tdir = self.ready()
        self.edit(tdir, "L1", r"^SPEED = .*", "SPEED = 1.5")
        self.ok("try", "t1", "--lane", "L1", "--idea", "1.5")
        self.verify("L1")
        self.ok("set", "t1", 'parse={"mode": "regex", "pattern": "MF_METRIC case=(?P<case>small) value=(?P<value>[0-9.]+)"}')
        self.ok("freeze", "t1")
        self.assertEqual(list(self.st().freeze["baseline"]), ["small"])
        self.assertIn("审计通过", self.ok("report", "t1"))
        v = self.st().verifies[-1]
        arms = json.loads((Path(v["run"]) / "abba.json").read_text())["arms"]
        Path(next(x["log"] for x in arms if x["arm"] == "B")).unlink()
        rc, out = mf("report", "t1")
        self.assertEqual(rc, 1, out)
        self.assertIn("missing or cannot be read", out)

    def test_contract_change_after_freeze_blocks_try(self):
        tdir = self.ready()
        self.ok("set", "t1", "commands.bench=python3 bench.py --fast")
        rc, out = mf("try", "t1", "--lane", "L1", "--idea", "x")
        self.assertEqual(rc, 3, out)
        self.assertIn("contract", {a["code"] for a in guard.alerts(self.st())})
        self.ok("set", "t1", "metric.target=1.1", "--why", "lower the bar")
        self.assertIn("goalpost", {a["code"] for a in guard.alerts(self.st())})
        self.assertTrue(tdir.exists())


class AntiStuckTest(Base):
    def test_lane_stall_is_refused(self):
        self.ready(max_no_improve=2)
        for i in range(2):
            self.ok("try", "t1", "--lane", "L1", "--idea", f"noop {i}")
        rc, out = mf("try", "t1", "--lane", "L1", "--idea", "again")
        self.assertEqual(rc, 3, out)
        self.assertIn("stall", out)
        self.assertIn("stall", {a["code"] for a in guard.alerts(self.st())})

    def test_attempt_budget_is_refused(self):
        self.ready(max_attempts=1)
        self.ok("try", "t1", "--lane", "L1", "--idea", "one")
        rc, out = mf("try", "t1", "--lane", "L1", "--idea", "two")
        self.assertEqual(rc, 3, out)
        self.ok("set", "t1", "budget.max_attempts=2", "--by", "user", "--why", "one more")
        self.ok("try", "t1", "--lane", "L1", "--idea", "two")

    def test_hung_command_is_killed(self):
        tdir = self.ready()
        self.edit(tdir, "L1", r"^HANG = .*", "HANG = 60")
        t0 = time.time()
        self.ok("try", "t1", "--lane", "L1", "--idea", "hang", "--timeout", "2")
        self.assertLess(time.time() - t0, 30)
        self.assertEqual(self.st().attempt(1)["status"], "timeout")

    def test_liveness_ignores_user_messages(self):
        self.ready()
        later = time.time() + 3 * 3600
        self.assertIn("stale", {a["code"] for a in guard.alerts(self.st(), later) if a["level"] == "crit"})
        self.ok("steer", "t1", "are you alive?")  # a user message must not look like agent activity
        self.assertIn("stale", {a["code"] for a in guard.alerts(self.st(), later) if a["level"] == "crit"})
        self.ok("note", "t1", "--lane", "L1", "reading code")
        self.assertNotIn("stale", {a["code"] for a in guard.alerts(self.st())})
        self.assertIn("reading code", self.st().now["text"])

    def test_steer_is_shown_until_acked(self):
        self.ready()
        self.ok("steer", "t1", "focus on large case")
        out = self.ok("status", "t1", "--lane", "L1")
        self.assertIn("用户留言 #1", out)
        self.ok("note", "t1", "--ack", "1", "will do")
        self.assertEqual(self.st().unacked(), [])
        self.assertNotIn("用户留言 #1", self.ok("status", "t1", "--lane", "L1"))


class UnitTest(Base):
    def test_parse_modes_and_speedup(self):
        m, _ = parse_metrics("MF_METRIC case=a value=2\nMF_METRIC case=a value=4\nMF_METRIC case=b value=10\n",
                             {"mode": "mf"}, {})
        self.assertEqual(m, {"a": 3.0, "b": 10.0})
        m, _ = parse_metrics("a: 5.0 us\nb: 7 us", {"mode": "regex", "pattern": r"(?P<case>\w+): (?P<value>[\d.]+) us"}, {})
        self.assertEqual(m, {"a": 5.0, "b": 7.0})
        (self.tmp / "m.json").write_text(json.dumps({"a": [1, 3], "b": 2}))
        m, _ = parse_metrics("", {"mode": "json", "path": "{run}/m.json"}, {"run": str(self.tmp)})
        self.assertEqual(m, {"a": 2.0, "b": 2.0})
        agg, per, missing = speedup({"a": 10, "b": 10}, {"a": 5}, {"direction": "min"})
        self.assertEqual((agg, missing), (2.0, ["b"]))
        agg, _, _ = speedup({"a": 10, "b": 30}, {"a": 5, "b": 30}, {"direction": "min", "aggregate": "sum"})
        self.assertAlmostEqual(agg, 40 / 35)

    def test_exclusive_lease_serializes(self):
        order = []

        def worker(name):
            with Lease([0], exclusive=True, who=name, wait_s=30):
                order.append(f"{name}+")
                time.sleep(0.5)
                order.append(f"{name}-")

        ts = [threading.Thread(target=worker, args=(n,)) for n in ("a", "b")]
        for t in ts:
            t.start()
            time.sleep(0.1)
        for t in ts:
            t.join()
        self.assertEqual(order, ["a+", "a-", "b+", "b-"])

    def test_render_overview_and_kb(self):
        self.ready()
        txt = render.term_overview([self.st()])
        self.assertIn("t1", txt)
        self.assertIn("t1", self.ok("status"))
        probs = [p for p in kb.lint() if "(warn)" not in p]
        self.assertEqual(probs, [], probs)

    def test_skills_follow_catalog_rules(self):
        for p in sorted((ROOT / "skills").glob("*/SKILL.md")):
            text = p.read_text(encoding="utf-8")
            m = re.match(r"---\n(.*?)\n---\n(.*)", text, re.S)
            self.assertIsNotNone(m, p)
            name = re.search(r"^name:\s*(\S+)", m.group(1), re.M).group(1)
            self.assertEqual(name, p.parent.name)
            self.assertRegex(name, r"^[a-z0-9-]{1,64}$")
            desc = " ".join(x.strip() for x in m.group(1).split("description:", 1)[1].splitlines())
            self.assertLessEqual(len(desc), 1024, p)
            self.assertLessEqual(len(m.group(2).splitlines()), 120, f"{p}: keep skills short")


if __name__ == "__main__":
    unittest.main()
