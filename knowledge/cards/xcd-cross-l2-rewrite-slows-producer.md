---
name: xcd-cross-l2-rewrite-slows-producer
kind: measurement
summary: MI308X 上一个缓冲被多个 XCD 的 L2 读过之后，下一个 kernel 再写它会慢 2–4×（跨 XCD 失效）。“量化→GEMM”这类连发要按 M 块把读者绑到同一 XCD，并让生产者的存储写穿 L2；单独测生产者看不到这个开销
applies: MI308X/MI300 gfx942（4 XCD、各自 L2、CTA→XCD = 线性号 % 4），连发的生产者 / 消费者 kernel 对（per-token 量化 → GEMM、MoE 排序 → GEMM 等），bench 里每次 torch.empty 拿到同一块内存的场景
confidence: high
evidence: task gemm-a8w4 lanes/L3/finding_xcd_quant.md（按 op 顺序连发时量化慢 4–5×，同一 M 块的 CTA 绑到同一 XCD 后基本恢复）；notes/L7-report.md（gemm_large 之后的 op 内量化比单独测慢 2–2.5×；量化存储改 sc0|sc1 写穿后降 32%）；notes/L4c-report.md（整块 M 按 XCD 分段的 tile 次序：tp4_o M2048 −18%、tp2_qkvz M256/512 −15/−9%）；M7 复核 V13 tp4_o_m2048 对 M3 1.247×
source: gemm-a8w4 (2026-11)
---
**场景**：算子由两个 kernel 连发组成（先量化 X 得到 xq，再由 GEMM 读 xq）。bench 循环里每次调用的 xq 都是 torch.empty 出来的同一块内存。

**症状 → 原因 → 解决**：
- 症状：生产者单独测很快，放在 op 里连发却慢 2–5×；消费者本身不变。aiter 的对照也同样中招。
- 原因：CTA 按线性号轮转派到 4 个 XCD。同一行 xq 被多个 XCD 的 L2 读入后，下一次写它要跨 XCD 失效。消费者的读改成 nt 无效。
- 解决（可叠加）：
  - ① 消费者按 M 块把 CTA 绑到同一 XCD：中间档用超块映射 xcd_m；持久化大核用“每个 XCD 拿整的 M 块”的 tile 次序，适用于 W 或 X 能放进单个 XCD 的 L2（约 3 MiB 以内）；
  - ② 生产者在大 M 配置下把存储改为写穿 L2（sc0|sc1）；
  - ③ 判断收益要用 op 内连发的同卡 A/B，不能只测隔离的单核。

**怎么验证**：按 op 顺序连发“生产者 → 消费者 → 生产者…”，和单独连发生产者对比生产者的时间；再把消费者的 CTA→M 块映射改成同 XCD，应基本回到单独测的水平。
