---
name: ptl-caps-sustained-mfma
kind: measurement
summary: MI308X PTL(VECTOR,F8) 下持续高 MFMA 负载会降频：BF16 按指令条数限流，FP8 在约 80% 峰值处触发，利用率目标需按持续上限设定
applies: MI308X gfx942, PTL Enabled VECTOR,F8, MFMA 密集 kernel（attention/GEMM），以及小 M 的 BF16 MFMA（MoE decode）
confidence: high
evidence: /opt/lc/work/qsa-opt/GOAL.md §4（A1 实测）；COORD_NOTES.md §4.7；moe-ptpc-a8w4 notes/L2-report.md、L2-throttle-finding.md；gemm-a8w4 M7 复核 V12（大 M 持久化 FP8 GEMM 平台 452–464 TF）、notes/L4b-report.md
source: qsa-opt (2026-11)
notes: absolute-ok
---
**场景**：给 MFMA 密集 kernel 定“达到 bound”的目标，或解释持续负载下的时延抖动。

**结论**：
- BF16 MFMA 不在 PTL 全速格式里，按指令条数计：全卡持续约 0.2s 高指令率就降到 1.25–1.45GHz，并在快慢两态摆动；指令条数少的形状（如 32x32x8）在持续负载下有额外收益。
- FP8 持续约 0.2s 达到约 80% 峰值也会碰到 PTL：80% 目标基本就是持续上限。佐证：gemm-a8w4 的大 M FP8 GEMM（A8W4，持久化 8-wave ping-pong）各形状停在 452–464 TF（565.8 TF 峰值的 80–82%）；尾轮 split-K 让末轮单 tile 更快，但满载时降频约 2%，整体反而慢 1.5–4%（L4b）。
- 低利用率也会触发：MoE decode batch1 每条路由只有 1 行激活（MFMA 只用 16 列中的 1 列），持续 BF16 MFMA 发射仍让时延在两态间跳（TP2 T=16 约 1.2× 差）；改成不发 MFMA 的 VALU f32 乘加（gemv）后不再降频，反而快 1.14–1.24×（moe-ptpc-a8w4 L2 A5，V1/V4）。消融：去掉解码仍降频，换掉 MFMA 才不降频。
- 计时要带时钟检查（每次调用前后 ≥1600MHz 才算有效），降频调用 >5% 的运行重跑。

**怎么验证**：在 `commands.gate` 或 bench 内记录调用前后的时钟。
