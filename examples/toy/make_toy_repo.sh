#!/usr/bin/env bash
# Create a tiny git repo with a fake "kernel" whose simulated latency is base/SPEED (CPU only).
# Usage: make_toy_repo.sh <dir>   then follow examples/toy/README.md
set -euo pipefail
d="${1:?usage: make_toy_repo.sh <dir>}"
mkdir -p "$d" && cd "$d"
git init -q
cat > kernel.py <<'PY'
"""Toy kernel. SPEED stands in for real optimization work: simulated latency = base / SPEED."""
import os
import time

SPEED = 1.0
HANG = 0
# An untracked local artifact (git-ignored) makes a lane's own measurement look better than the
# committed code really is; the verify in `mf ms prep` runs in a clean worktree and exposes it.
if os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".boost")):
    SPEED *= 1.3


def run(xs):
    time.sleep(HANG)
    return [2 * x for x in xs]
PY
printf '.boost\n' > .gitignore
cat > bench.py <<'PY'
"""Frozen bench: prints MF_METRIC lines (simulated latency with ~0.5% noise)."""
import os
import random

import kernel

rng = random.Random(int(os.environ.get("MF_SEED", "0")) ^ os.getpid())
for case, base_us in (("small", 10.0), ("medium", 100.0), ("large", 1000.0)):
    for _ in range(5):
        print(f"MF_METRIC case={case} value={base_us / kernel.SPEED * (1 + rng.gauss(0, 0.005)):.4f} unit=us")
PY
cat > test_kernel.py <<'PY'
"""Frozen correctness test: exit 0 = pass."""
import sys

import kernel

sys.exit(0 if kernel.run([1, 2, 3]) == [2, 4, 6] else 1)
PY
echo "notes" > README.md
git add -A
git -c user.name=toy -c user.email=toy@localhost commit -qm "toy baseline"
echo "$d"
