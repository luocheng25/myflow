---
name: moe-stream-prefill-crossover-rows-per-expert
kind: measurement
summary: MoE 流式小 BM 路径与 prefill 的交界由每 expert 平均行数（M·topk/E）决定而非 M；prefill 一优化交界就前移，须重测，bench 没覆盖的档会藏回退
applies: gfx942 MI308X, FlyDSL, MoE mxfp4 权重 / fp8 激活（A8W4），两阶段 gate/up + down
confidence: medium
evidence: task moe-ptpc-a8w4-r2 tools/m3_bm64_tier.py（M3 候选与 int4 同协议测量）、V4/V5
source: moe-ptpc-a8w4-r2，2026-11-22
---
**场景**：同一算子有“按 expert 流式读权重”的小 BM 路径和“大 tile 计算”的 prefill 路径，按 token 数分派。

**结论**：
- 交界在每 expert 平均约 22–25 行（E513/topk11：M≈1024–1152），TP2（I=320）与 TP4（I=192）一致；按 M 写死的边界换 E / topk 就错。
- prefill 优化（指令排布、tile_k=128 等）后，原本流式更快的 25–45 行/expert 区间变成 prefill 快 2–20%：
  交界必须在每次改 prefill 后重测。
- 该区间只有排序块 64 的流式变体在用，而 bench 只有 m128 / m1024 两档：不重测就会把慢路径留在默认分派里且所有用例通过。
- 同区间 prefill tile_k=128 比 256 快 2–4%（占用率更高）。

**怎么验证**：同 bench 协议（冷缓存 + 空转 + 交替 + 中位数）在 rows/expert = 11…44 上扫默认分派与各路径，
默认分派应等于各路径中最快者（±1%）。
