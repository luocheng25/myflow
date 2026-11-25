---
name: moe-down-large-k-two-pass-lds
kind: technique
summary: MoE prefill Down 的 BM×K 激活块超 32 KiB（如 I576）时每 CU 仅 1 WG；A 分两段经 LDS 搬，LDS 降近半、2 WG/CU，大 M 整体快约 10%；小 grid 保持一段
applies: gfx942 MI308X, FlyDSL, MoE 两阶段 prefill Down GEMM（mxfp4 权重 / fp8 激活，BM64），K 不是 512 的倍数的大 I（如 I576）
confidence: medium
evidence: task moe-ptpc-a8w4-r2 L14 A104（4d8e4c4，两段搬运）/A105（e7b0113，切分规则重测）/A110；notes/L14-report.md
source: moe-ptpc-a8w4-r2（DeepSeek-V4.1-Flash TP4，H5120 I576），2026-11-24
---
**场景**：Down 的 K = 每卡中间维 I。I 变大（如 576）后 BM64×K 的激活块 36 KiB，加上其余 LDS，
每 CU 只放得下 1 个 WG；kernel 分解里 Down 占 M≥2048 总时间的 38–40%。

**做法**：
- K>512 且 BM·K>32 KiB 时把 A 分两段：先搬前 5 个 64 列块算完，再搬后 4 个（奇数块数、含 padding 的半块都要处理），
  LDS 36→20 KiB，每 CU 1→2 个 WG（编译期开关 TWO_PASS，分派处按“估计 WG 数 > 80”开启）。
- 同进程 ABBA：M2048–32768 整体 0.89–0.91× 的时间；估计 WG ≤ 80 的小 grid（M1/2 回退 prefill 时）两段反而慢，保持一段。
- 改了占用后，原先“不切 hidden”的特例要重测：两段后原切分规则在 M2048/4096 又快 1.5–4.6%。

**怎么验证**：两段与一段输出逐位相同（K 奇数块、padding 半块）；`rocprofv3 --kernel-trace` 看 Down 时间，
ISA 元数据 LDS 大小与每 CU WG 数（occupancy）同时变化才算生效。

<!-- 规则：数字用相对值（×、%、占峰值%）；≤40 行；同主题先 mf kb search，更新旧卡而不是新建。 -->
