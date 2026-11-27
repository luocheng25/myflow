---
name: tp-comm-hidden-in-down-wg-specialization
kind: technique
summary: MoE TP 的 RS+AG 可藏进 Down：按 H 列块主序、同一 launch 里 C 计算 / X 归约+RS / Y owner 求和+AG 三类 WG，tp2 大 M 1.11–1.16×；列块切分让 Down 计算慢 28–37%，调度修正压掉 18–68%
applies: gfx942 MI308X, FlyDSL, MoE 两阶段 prefill / 流式 Down（A8W4：fp8 激活 × mxfp4 权重），TP2 / TP4 全互联 XGMI，M≥约 400 的大批
confidence: medium
evidence: task mega-tp-a8w4 M3（lane p1 A23，V5/V6，notes/p1-report.md 第 6 条 E14）、M6（lane dtax A28 → int6，V11/V12，notes/dtax-report.md E1/E28/E29c）
source: mega-tp-a8w4（Qwen3.8 H2560 E513 topk11 TP2/TP4、DeepSeek-V4.1 H5120 TP4），2026-11-26
---
**场景**：TP 下每卡算出全 H 的部分和，要 RS 到 owner、求和、再 AG 回各卡；大 M 时尾部单独的
通信 kernel 占整层 20–35%（tp2 单链路最重）。旁路 stream 并发不可控（见 cross-stream-overlap-mi308x）。

**做法**：
- Down 的 1-D grid 交错派发：C WG 按列块（tp 5×512 列）主序算 tile，写穿后对本卡列块计数原子
  加 1、从不等待；X WG 等列块计满后做本卡 topk 归约，把 owner 份额写进 owner 的收件箱（不用远端
  原子）；Y WG 在 owner 上按 rank 顺序求和后 AG。末尾留一个只拷贝的 gather；求和顺序不变 → 逐位不变。
- 计算税来源（tp4 M32768 只算不发）：一半以上是每个 (路由块, 列块) item 的序幕 / 收尾（gather
  激活 → LDS → barrier 后才发首块权重），其次路由行写穿约 20%、列块主序下激活不在 L2 约 10–20%、
  X/Y 占位约 8%、计数约 5%。
- 压税（都逐位不变）：序幕先发前两块权重 / 行缩放再 gather；n-block 循环中间 barrier 后加
  `rocdl.sched_barrier(0)`——gfx942 的 vmcnt 对 load / store 按发出顺序计数，编译器把下半块 MFMA
  提到权重 load 旁，等权重时会连带等之前的写穿 store；先发下一块权重再写路由行；VGPR 涨了用
  waves_per_eu 保占用；路由行写穿加 nt 只在 tp4 M≥16384 有利。H5120 列块 10→5 再快 3–5%。
  结果：只算不发的税 ds −68%、tp4 −38%、tp2 −18%。
- 剩下的是列块切分本身（通用 gemm2 切 5 列块也慢约 20%）；持久化 C WG + 跨 item 预取在 tp4 上
  会因 128 VGPR / LDS 已满掉占用，没做。

**怎么验证**：DRY 开关（只算不发 / 不计数）与原 Down 同进程配对拆税；三方交替 bench 看大 M；
prefill 档与旧路径 `torch.equal`，bf16 原子累加档按容差。

<!-- 规则：数字用相对值（×、%、占峰值%）；≤40 行；同主题先 mf kb search，更新旧卡而不是新建。 -->
