---
name: moe-sorted-block-half-m
kind: technique
summary: MoE 两段 GEMM 在每专家行数远小于排序块（如 M64 块里约 20 行）时，按块在运行时切到只算前 32 行的路径，Gate/Up 与 Down 的 MFMA 和写出减半；中等 T 提速约 1.35–1.40×，无需改排序与已有用例
applies: gfx942 MI308X, FlyDSL MoE 2-stage（上游 gfx9_fp8 prefill_1x4 / gemm2_1x4 派生）, fp8 MFMA, prefill 中等 T（T·topk/E ≈ 10–40）
confidence: high
evidence: task moe-ptpc-a8w4 L1 A1–A6，V2（TP2/TP4 T=1024 1.375× / 1.405×），M3 a294918e91；notes/L1-report.md
source: moe-ptpc-a8w4 (2026-11)
---
**场景**：排序按 BLOCK_M=64 给每个专家补齐行，而 T·topk/E 只有约 20：约 2/3 的 MFMA、写出都算在补齐行上。对照实现（aiter 1-stage ASM）用的是 M32 排序。

**做法**：
- 不改排序（冻结用例可能要求 Down 拒绝 M32 tile），在 kernel 内按块判定：第 33 行是补齐行 → 走 32 行 tile 路径，否则走 64 行；
- Gate/Up 与 Down 都做，Down 的写出也只写前 32 行；
- 32 行路径 MFMA 少、更受 HBM 延迟限制：把权重预取从 1 步加到 2 步，又快约 3%；
- 中间结果的按行量化换成融合的 FlyDSL kernel（每行 8 或 64 个 lane、shuffle_xor 求 amax），T=1024 时这一步快约 4×。

**怎么验证**：
- 用非均匀路由构造某专家恰好 32 / 33 行，断言两条路径都对、与整块路径逐位相同；
- ABBA 对上一里程碑：大 T（全部整块）不能变慢。
