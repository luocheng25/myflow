"""GPU leases with gpu.count > 1 (multi-GPU TP jobs): all or nothing, always exclusive, every GPU in env / gates /
holder records; gpu.count = 1 keeps the single-GPU path."""

from __future__ import annotations

import fcntl
import json
import os
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from test_myflow import Base, mf  # noqa: F401  (also puts myflow on sys.path)

from myflow import cli  # noqa: E402
from myflow.core import MfError, lock_dir, task_dir_of  # noqa: E402
from myflow.runner import Lease, build_env, gate_passes, gate_snapshot, run_cmd  # noqa: E402

IDLE = {g: {"use": 0.0, "vram": 0.0} for g in range(8)}
TASK = {"gpu": {"env": "HIP_VISIBLE_DEVICES"}}


def held(g: int, mode=fcntl.LOCK_EX) -> bool:
    """Someone holds GPU g's lock (probed without waiting)."""
    with open(lock_dir() / f"gpu{g}.lock", "a+") as fh:
        try:
            fcntl.flock(fh, mode | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fh, fcntl.LOCK_UN)
    return False


def holders() -> list:
    return sorted(p.name for p in lock_dir().glob("gpu*.json"))


class LeaseTest(Base):
    def setUp(self):
        super().setUp()
        patcher = mock.patch("myflow.runner.smi_snapshot", return_value=IDLE)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_count1_keeps_the_single_gpu_path(self):
        with Lease([0, 1], exclusive=False, gpu_cfg={"count": 1}) as a, Lease([0, 1], exclusive=False) as b:
            self.assertEqual((a.gpu, a.gpus, b.gpu, a.exclusive), (0, [0], 0, False))  # shared: same GPU
            self.assertEqual(a.gpu_fields(), {"gpu": 0})
            self.assertEqual(build_env(TASK, a.gpu_fields())["HIP_VISIBLE_DEVICES"], "0")
        with Lease([0, 1], who="x", gpu_cfg={"count": 1}) as a, Lease([0, 1], who="y") as b:
            self.assertEqual((a.gpu, b.gpu), (0, 1))  # exclusive: the next GPU
            info = json.loads((lock_dir() / f"gpu0.x.{os.getpid()}.json").read_text())
            self.assertEqual(sorted(info), ["exclusive", "pid", "since", "who"])
        self.assertEqual(holders(), [])

    def test_count_n_is_exclusive_with_env_list_records_and_status(self):
        pid = os.getpid()
        with Lease([3, 1, 0, 2], exclusive=False, who="t/_task/run", gpu_cfg={"count": 4}) as L:
            self.assertTrue(L.exclusive)  # N > 1: correctness leases are exclusive too
            self.assertEqual((L.gpu, L.gpus, L.gpu_fields()), (0, [0, 1, 2, 3], {"gpu": 0, "gpus": [0, 1, 2, 3]}))
            self.assertTrue(all(held(g, fcntl.LOCK_SH) for g in range(4)))
            self.assertFalse(held(4))
            self.assertEqual(holders(), [f"gpu{g}.x.{pid}.json" for g in range(4)])
            env = build_env(TASK, L.gpu_fields())
            self.assertEqual(env["HIP_VISIBLE_DEVICES"], "0,1,2,3")
            self.assertNotIn("CUDA_VISIBLE_DEVICES", env)
            r = run_cmd("echo VIS=$HIP_VISIBLE_DEVICES", self.tmp, env, self.tmp / "run" / "out.log", 30)
            log = Path(r["log"]).read_text().splitlines()
            self.assertIn("gpu=0,1,2,3", log[1])
            self.assertIn("VIS=0,1,2,3", log)
            lines = cli.gpu_lines()  # `mf status`: one job on every GPU
            self.assertEqual(len(lines), 4)
            for g, line in enumerate(lines):
                self.assertRegex(line, rf"^GPU{g}: .*EXCL t/_task/run pid{pid} .*\[4-GPU lease 0,1,2,3\]")
        self.assertEqual(holders(), [])
        self.assertFalse(any(held(g) for g in range(4)))
        self.assertEqual(cli.gpu_lines(), [])

    def test_pool_smaller_than_count_and_bad_count_fail_at_once(self):
        t0 = time.time()
        with self.assertRaises(MfError) as cm:
            with Lease([0, 1, 2], gpu_cfg={"count": 4}, wait_s=60):
                pass
        self.assertLess(time.time() - t0, 1)
        self.assertIn("gpu.count=4 but pool [0, 1, 2]", str(cm.exception))
        for bad in (0, -1, "4", 2.0, True):
            with self.assertRaises(MfError):
                Lease([0, 1], gpu_cfg={"count": bad})
        self.assertEqual(holders(), [])

    def test_all_or_nothing_gives_everything_back(self):
        with Lease([2], who="single"):
            with self.assertRaises(MfError):
                with Lease([0, 1, 2, 3], gpu_cfg={"count": 4}, wait_s=0):
                    pass
            self.assertEqual([g for g in range(4) if held(g)], [2])  # 0 and 1 were taken in passing, then released
            self.assertEqual(holders(), [f"gpu2.x.{os.getpid()}.json"])
        with Lease(None, machine="excl", wait_s=0):  # the failed lease released its machine lock too
            pass

    def test_waits_for_a_single_lease_without_deadlock(self):
        events, started = [], threading.Event()

        def single():
            with Lease([2], who="single", wait_s=10):
                started.set()
                time.sleep(1)  # the multi-GPU lease is waiting by now ...
                t0 = time.time()
                with Lease([0], who="single0", wait_s=10):  # ... and holds nothing meanwhile: GPU0 is free
                    events.append(("single0", time.time() - t0))
                events.append(("single-", 0))

        t = threading.Thread(target=single)
        t.start()
        self.assertTrue(started.wait(10))
        with Lease([0, 1, 2, 3], who="multi", gpu_cfg={"count": 4}, wait_s=30) as L:
            events.append(("multi+", 0))
            self.assertEqual(L.gpus, [0, 1, 2, 3])
        t.join(10)
        self.assertFalse(t.is_alive())
        self.assertEqual([e for e, _ in events], ["single0", "single-", "multi+"])
        self.assertLess(events[0][1], 2.5)

    def test_two_multi_gpu_leases_take_turns(self):
        order = []

        def worker(name):
            with Lease([0, 1, 2, 3], who=name, gpu_cfg={"count": 4}, wait_s=30):
                order.append(f"{name}+")
                time.sleep(0.3)
                order.append(f"{name}-")

        ts = [threading.Thread(target=worker, args=(n,)) for n in ("a", "b")]
        for t in ts:
            t.start()
            time.sleep(0.1)
        for t in ts:
            t.join(30)
        self.assertIn(order, (["a+", "a-", "b+", "b-"], ["b+", "b-", "a+", "a-"]))  # never interleaved

    def test_gate_checks_every_gpu(self):
        cfg = {"gate": "strict", "max_use": 5, "max_vram": 20}
        snap = {0: {"use": 0.0, "vram": 1.0}, 1: {"use": 50.0, "vram": 30.0}, 2: {"use": 1.0, "vram": 0.0}}
        with mock.patch("myflow.runner.smi_snapshot", return_value=snap):
            g = gate_snapshot([0, 1, 2], cfg)
            self.assertEqual((g["ok"], g["use"], g["vram"]), (False, 50.0, 30.0))
            self.assertEqual(g["gpus"], [{"gpu": 0, "use": 0.0, "vram": 1.0}, {"gpu": 1, "use": 50.0, "vram": 30.0},
                                         {"gpu": 2, "use": 1.0, "vram": 0.0}])
            self.assertFalse(gate_passes(g))
            self.assertTrue(gate_passes(gate_snapshot([0, 1, 2], dict(cfg, gate="warn"))))
            self.assertIs(gate_snapshot([0, 2], cfg)["ok"], True)
            self.assertIs(gate_snapshot([0, 7], cfg)["ok"], None)  # unknown is never treated as idle
            self.assertFalse(gate_passes(gate_snapshot([0, 7], cfg)))
            self.assertEqual(gate_snapshot(0, cfg), {"ok": True, "mode": "strict", "use": 0.0, "vram": 1.0})

    def test_mf_run_auto_n_and_none(self):
        self.ok("new", "t1", "--repo", self.repo, "--goal", "multi-GPU")
        self.ok("set", "t1", "gpu.pool=0-3", "gpu.count=4")
        cmd = "echo VIS=${HIP_VISIBLE_DEVICES:-unset}"
        out = self.ok("run", "t1", "--", cmd)
        self.assertIn("VIS=0,1,2,3", out)
        self.assertIn("GPU0,1,2,3", out)
        self.assertIn("VIS=0,1,2,3", self.ok("run", "t1", "--gpu", "2", "--", cmd))  # N first, the rest from the pool
        self.assertIn(" CPU ", self.ok("run", "t1", "--gpu", "none", "--", cmd))
        evs = [json.loads(x) for x in (task_dir_of("t1") / "events.jsonl").read_text().splitlines()]
        runs = [e["data"] for e in evs if e["kind"] == "note" and (e.get("data") or {}).get("kind") == "run"]
        self.assertEqual([(r["gpu"], r.get("gpus")) for r in runs], [(0, [0, 1, 2, 3]), (0, [0, 1, 2, 3]), (None, None)])
        self.ok("set", "t1", "gpu.pool=0-2")
        rc, out = mf("run", "t1", "--", cmd)
        self.assertEqual(rc, 2, out)
        self.assertIn("gpu.count=4 but pool [0, 1, 2]", out)

    def test_try_and_verify_lease_and_gate_every_gpu(self):
        tdir = self.ready()  # frozen on the CPU
        self.ok("set", "t1", "gpu.pool=0-1", "gpu.count=2")  # not part of the measurement contract: no re-freeze
        self.edit(tdir, "L1", r"^SPEED = .*", "SPEED = 1.5")
        out = self.ok("try", "t1", "--lane", "L1", "--idea", "speed 1.5")
        a = self.st().attempt(1)
        self.assertEqual(a["status"], "pass", out)
        self.assertIn("GPU0,1", out)
        for key in ("correctness", "bench"):
            self.assertEqual((a[key]["gpu"], a[key]["gpus"]), (0, [0, 1]))
            self.assertIn("gpu=0,1", Path(a[key]["log"]).read_text().splitlines()[1])
        self.assertEqual([x["gpu"] for x in a["bench"]["gate_before"]["gpus"]], [0, 1])
        out = self.verify("L1")
        v = self.st().verifies[-1]
        self.assertEqual((v["verdict"], v["gpu"], v["gpus"]), ("improved", 0, [0, 1]), out)
        self.assertIn("GPU0,1", out)


if __name__ == "__main__":
    unittest.main()
