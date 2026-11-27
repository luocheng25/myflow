---
name: device-sdma-oneshot-tp2-token-chunks
kind: technique
summary: TP2 大 M 把 token 分两块，块 0 的部分和由 kernel 触发的 SDMA 推给对端、藏在块 1 的计算下，末 kernel 按 rank 求和：再快 5–10%；要推送不拉取、引擎按 KFD 推荐，TP4 与小 M 不赢
notes: absolute-ok
applies: gfx942 MI308X（xGMI SDMA 引擎只有 2/4/6），FlyDSL + ctypes KFD，MoE TP2 大 M（每方向通信 ≥ 约 40 MB），HIP graph 可捕获
confidence: medium
evidence: task mega-tp-a8w4 M4（lane tcs A25，V7/V8；notes/tcs-report.md、notes/sdma-report.md、notes/sdma-design.md；runs 722b、b19c、9bfe、0a5b、c2a7）
source: mega-tp-a8w4（Qwen3.8 H2560 TP2/TP4、DeepSeek-V4.1 H5120 TP4），2026-11-26
---
**场景**：通信只能藏在 Down 下面时，tp2 单链路（约 45–50 GB/s）大 M 仍露出约 15–20% 的通信；
要让通信跨进下一段计算，又不能靠两条 stream 并发（见 cross-stream-overlap-mi308x）。

**做法**：
- 设备端 SDMA：host 每进程每卡用 KFD `CREATE_QUEUE(SDMA_BY_ENG_ID)` 建一次用户态队列（ring 与
  rptr / wptr 用 ROCr VRAM 池分配），kernel 里一个 wave 写包（POLL 旗标 → COPY / 2-D COPY → FENCE）、
  写 wptr、敲 doorbell（GPU 直接写 /dev/kfd 映射里的 doorbell 页）：每次调用 host 开销 0，可被 graph
  捕获；1 KB 宽 2-D 拷贝与线性同速，单引擎约 48–50 GB/s，计算负载下不降。
- token 分块：全局排序只做一次，按 sorted block 的首 token 给每块列活块（moe_sorting 在 expert
  内按 token 保序 ⇒ 块 c 做完时它的 token 的路由全算完）；紧凑块列表把分块税从约 9% 压到 2–4%。
  只分两块，三块更差；tp2 M≤4096 分块税高于露出的通信，不分。
- one-shot（W=2）：每卡把完整部分和推给对端（每方向 U 字节，与 RS+AG 相同但只一跳、不要 owner
  求和 WG），末 kernel 算 f32(本卡 + landing)，只舍入一次 → 两卡逐位相同、与原路径逐位相同。
- 推送不拉取：对端 SDMA 远读本卡内存会让本卡 GateUp 慢约 8%，推送只慢约 2%。
- 引擎：用 KFD io_links 推荐的引擎；用错引擎（这里是 2）会让对端 CU 计算慢约 6%（SDMA 速率不变），
  整体收益从 0.955 变 0.902。
- 不赢的：TP4 one-shot 每链路字节是 RS+AG 的 2 倍；TP4 / H5120 分块 + SDMA AG 只有 M32768 且标定
  引擎时赢约 2%。约束：SDMA 队列按设备共享，用它的 forward 不能在不同 stream 上并发（检测到则置错误位，
  reset 重建队列）；依赖 KFD ioctl ABI 与 ROCr 内部 doorbell 映射，升级 ROCm 要用探针复测。

**怎么验证**：kernel 里用 SDMA TIMESTAMP 包 + `s_memrealtime` 打时间线，看推送是否连续满速、何时空闲；
与原路径同进程 ABBA 配对；graph 捕获后重放结果逐位相同。

<!-- 规则：数字用相对值（×、%、占峰值%）；≤40 行；同主题先 mf kb search，更新旧卡而不是新建。 -->
