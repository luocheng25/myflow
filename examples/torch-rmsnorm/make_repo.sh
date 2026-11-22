#!/usr/bin/env bash
# Create a small GPU example repo (torch RMSNorm) to exercise myflow on real AMD GPUs.
# Usage: make_repo.sh <dir>     bench prints MF_METRIC lines; needs torch with ROCm (or CUDA).
set -euo pipefail
d="${1:?usage: make_repo.sh <dir>}"
mkdir -p "$d" && cd "$d" && git init -q
cat > kernel.py <<'PY'
"""RMSNorm under optimization. Baseline: several unfused elementwise/reduction kernels."""
import torch

EPS = 1e-6


def rmsnorm(x: torch.Tensor, w: torch.Tensor) -> torch.Tensor:
    xf = x.float()
    var = (xf * xf).sum(-1, keepdim=True) / xf.shape[-1]
    y = xf / torch.sqrt(var + EPS)
    return (y * w.float()).to(x.dtype)
PY
cat > bench.py <<'PY'
"""Frozen bench: CUDA-event timing of one call, rotating inputs, cache flush, median of 30. Prints MF_METRIC."""
import os
import statistics

import torch

import kernel

torch.manual_seed(int(os.environ.get("MF_SEED", "0")))
dev = "cuda"
flush = torch.empty(512 * 1024 * 1024, dtype=torch.uint8, device=dev)  # larger than L2 + MALL, so every call is cold
for m, n in ((4096, 4096), (16384, 4096), (8192, 8192)):
    xs = [torch.randn(m, n, device=dev, dtype=torch.bfloat16) for _ in range(4)]
    w = torch.randn(n, device=dev, dtype=torch.bfloat16)
    for x in xs:
        kernel.rmsnorm(x, w)
    torch.cuda.synchronize()
    times = []
    for i in range(30):
        x = xs[i % len(xs)]
        flush.zero_()
        s, e = torch.cuda.Event(enable_timing=True), torch.cuda.Event(enable_timing=True)
        s.record()
        kernel.rmsnorm(x, w)
        e.record()
        torch.cuda.synchronize()
        times.append(s.elapsed_time(e) * 1000.0)
    # work of one call: read x and w, write y (bf16); ~4 flops per element
    print(f"MF_METRIC case=m{m}_n{n} value={statistics.median(times):.3f} unit=us flops={4 * m * n} "
          f"bytes={(2 * m * n + n) * 2}", flush=True)
PY
cat > test_kernel.py <<'PY'
"""Frozen correctness: fp32 reference, odd shapes, tolerance 2e-2 (bf16 output). Exit 0 = pass."""
import os
import sys

import torch

import kernel

torch.manual_seed(int(os.environ.get("MF_SEED", "0")) + 1)
for m, n in ((7, 4096), (1024, 4096), (333, 8192)):
    x = torch.randn(m, n, device="cuda", dtype=torch.bfloat16)
    w = torch.randn(n, device="cuda", dtype=torch.bfloat16)
    xf = x.float()
    ref = xf * torch.rsqrt(xf.pow(2).mean(-1, keepdim=True) + kernel.EPS) * w.float()
    out = kernel.rmsnorm(x, w).float()
    if out.shape != ref.shape or not torch.allclose(out, ref, atol=2e-2, rtol=2e-2):
        print(f"FAIL m={m} n={n} max_err={(out - ref).abs().max().item():.4g}")
        sys.exit(1)
print("ok")
PY
git add -A && git -c user.name=demo -c user.email=demo@localhost commit -qm "rmsnorm baseline"
echo "$d"
