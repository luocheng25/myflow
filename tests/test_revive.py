"""mf revive: the restart decision, crash / transcript detection, and the launch with a stand-in copilot."""

from __future__ import annotations

import os
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from test_myflow import Base, ROOT  # noqa: F401  (ROOT puts myflow on sys.path)

from myflow import revive as rv  # noqa: E402

CFG = dict(rv.DEFAULTS)
NOW = 1_000_000.0


def fake(done=False, stop=None, gates=(), runs=None, last=NOW - 3600 * 2, progress=3):
    return SimpleNamespace(done=lambda: done, stop=stop, pending_gates=lambda: list(gates), runs_open=runs or {},
                           last_agent_t=last, progress_ts=[0.0] * progress)


class DecideTest(unittest.TestCase):
    def act(self, st, **kw):
        return rv.decide(st, NOW, CFG, **kw)[0]

    def test_never_resumes_a_finished_stopped_or_waiting_task(self):
        self.assertEqual(self.act(fake(done=True)), "skip")
        self.assertEqual(self.act(fake(stop={"t": NOW})), "skip")
        self.assertEqual(self.act(fake(gates=[{"key": "report", "asked_t": NOW - 7200}])), "skip")
        self.assertEqual(self.act(fake(gates=[{"key": "spec"}])), "resume")  # opened but never asked: orchestrator's turn
        self.assertEqual(self.act(fake(runs={"r1": {"t": NOW - 600, "timeout": 3600}})), "skip")
        self.assertEqual(self.act(fake(runs={"r1": {"t": NOW - 7200, "timeout": 600}})), "resume")  # lost run

    def test_idle_threshold_and_crash_grace(self):
        self.assertEqual(self.act(fake(last=NOW - 30 * 60)), "skip")
        self.assertEqual(self.act(fake(last=NOW - 61 * 60)), "resume")
        self.assertEqual(self.act(fake(), other_t=NOW - 5 * 60), "skip")  # e.g. a VS Code transcript still written
        last = NOW - 15 * 60
        self.assertEqual(self.act(fake(last=last), crashes=[last + 60]), "resume")
        self.assertEqual(self.act(fake(last=last), crashes=[last - 60]), "skip")  # the crash was before the activity

    def test_running_cli_is_left_alone_unless_stalled(self):
        self.assertEqual(self.act(fake(last=NOW - 3600), cli_alive=True), "skip")
        self.assertEqual(self.act(fake(last=NOW - 4 * 3600), cli_alive=True), "kill")

    def test_launch_caps(self):
        many = [{"t": NOW - 3600 * k, "progress": k} for k in range(CFG["max_launches"])]
        self.assertEqual(self.act(fake(progress=99), launches=many), "halt")
        idle = [{"t": NOW - 7 * 3600, "progress": 3}, {"t": NOW - 3 * 3600, "progress": 3}]
        self.assertEqual(self.act(fake(progress=3), launches=idle), "halt")
        self.assertEqual(self.act(fake(progress=4), launches=idle), "resume")
        self.assertEqual(self.act(fake(progress=3), launches=idle[1:]), "resume")


class OutsideTest(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        self.tmp = Path(__import__("tempfile").mkdtemp(prefix="mf-rv-"))
        os.environ["MF_VSCODE_SERVER_DIR"] = str(self.tmp)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        __import__("shutil").rmtree(self.tmp, ignore_errors=True)

    def test_exthost_crashes_skips_clean_exits(self):
        log = self.tmp / "data/logs/20261108T090548/remoteagent.log"
        log.parent.mkdir(parents=True)
        pre = "[info] [127.0.0.1][a465be9d][ExtensionHostConnection] <1382319> Extension Host Process exited with"
        log.write_text(f"2026-11-24 16:29:18.786 {pre} code: null, signal: SIGABRT.\n"
                       f"2026-11-24 17:00:00.000 {pre} code: 0, signal: null.\n")
        t = time.mktime(time.strptime("2026-11-24 16:29:18", "%Y-%m-%d %H:%M:%S"))
        self.assertEqual(rv.exthost_crashes(t - 1), [t])
        self.assertEqual(rv.exthost_crashes(t), [])

    def test_transcript_found_by_task_id_near_its_end(self):
        d = self.tmp / "data/User/workspaceStorage/ws/GitHub.copilot-chat/transcripts"
        d.mkdir(parents=True)
        (d / "a.jsonl").write_text('{"x": "nothing here"}\n')
        self.assertIsNone(rv.vscode_transcript("moe-x"))
        (d / "b.jsonl").write_text('{"x": "mf status moe-x"}\n')
        self.assertEqual(rv.vscode_transcript("moe-x")[0], d / "b.jsonl")

    def test_session_id_is_fixed_per_task(self):
        self.assertEqual(rv.session_id("a"), rv.session_id("a"))
        self.assertNotEqual(rv.session_id("a"), rv.session_id("b"))


class WatchTest(Base):
    def setUp(self):
        super().setUp()
        os.environ["MF_VSCODE_SERVER_DIR"] = str(self.tmp / "vscode")
        os.environ["COPILOT_HOME"] = str(self.tmp / "copilot")
        self.args = self.tmp / "copilot-args.txt"
        fake_cli = self.tmp / "copilot"
        fake_cli.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{self.args}"\n')
        fake_cli.chmod(0o755)
        self.cfg = dict(rv.DEFAULTS, copilot=str(fake_cli), idle_min=0.0, crash_grace_min=0.0)

    def test_check_launches_then_halts_without_progress(self):
        self.ok("new", "t1", "--repo", self.repo, "--goal", "toy 1.5x", "--target", "1.5")
        out = self.ok("revive", "check", "t1")
        self.assertIn("t1: skip", out)  # just created: active a moment ago (default idle 60 min)
        seen = {}
        for _ in range(3):
            rv.check_once("t1", self.cfg, seen)
            pid = rv.load_rec("t1").get("pid")
            for _ in range(100):
                if not rv.pid_alive(pid, "copilot"):
                    break
                time.sleep(0.05)
        rec = rv.load_rec("t1")
        self.assertEqual(len(rec["launches"]), 2)
        self.assertIn("连续 2 次续跑都没有记录到进展", rec["halted"]["why"])
        argv = self.args.read_text().splitlines()
        self.assertIn("--session-id", argv)
        self.assertEqual(argv[argv.index("--deny-tool") + 1], "shell(git push)")
        prompt = argv[argv.index("-p") + 1]
        self.assertIn("myflow 任务 t1", prompt)
        briefs = [b["text"] for b in self.st().briefs]
        self.assertTrue(any("自动续跑：" in b for b in briefs) and any("自动续跑已停止" in b for b in briefs), briefs)


if __name__ == "__main__":
    unittest.main()
