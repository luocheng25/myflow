---
name: cross-stream-overlap-mi308x
kind: dead-end
summary: MI308X 上用第二条 stream 让两个吃满 CU 的 kernel 并发来藏通信行不通：后发的大 grid 把前一 kernel 末尾的通信 WG 挤后（慢 12–21%），常驻消费者会等到超时；改用 WG 特化或设备端 SDMA
notes: absolute-ok
applies: gfx942 MI308X, HIP / PyTorch 多 stream, 计算与通信重叠（MoE TP、任何“生产者 kernel + 消费者 kernel”）
confidence: medium
evidence: task mega-tp-a8w4 lane tcs deadend（run 20261126-151507-tcs-run-48cd）、notes/p1-report.md 第 4 条（S2 实测）、notes/W/prompt_p1.md（cpk 实测高优先级流）
source: mega-tp-a8w4，2026-11-26
---
**症状**：
- 非末块 Down(c) 放副 stream、主 stream 跑 quant / GateUp(c+1)：tp2 大 M 整体慢 1.12–1.21×
  （高优先级副流 1.12–1.15×），同次的顺序版只慢 1.02–1.06×。
- 普通优先级副流上的常驻消费者 kernel（轮询计数器）先提交时，生产者迟迟不上卡，等到超时。
- 进程里多一条高优先级 stream，同进程所有 kernel 都变慢（实测约 600 µs）。

**原因**：两个 kernel 都能占满 80 个 CU 时，WG 派发基本按提交顺序；Down(c) grid 末尾的通信 WG 被
GateUp(c+1) 的约 3 万个 WG 挤到后面，Down 内原有的通信重叠也丢了。常驻消费者占住 CU 后，生产者拿
不到资源（不是 stream 语义问题）。跨 kernel 的并发度不可控。

**解决**：把通信放进同一个 kernel 的专职 WG（见 tp-comm-hidden-in-down-wg-specialization），或交给不占
CU 的设备端 SDMA（见 device-sdma-oneshot-tp2-token-chunks）；只有一边几乎不用 CU（如 SDMA 拷贝）时，
多 stream 重叠才有收益。

**怎么验证**：同进程顺序版 / 双 stream 版 ABBA 配对；kernel 内打 `s_memrealtime` 时间戳看通信 WG 的
开始时刻是否被推迟。

<!-- 规则：数字用相对值（×、%、占峰值%）；≤40 行；同主题先 mf kb search，更新旧卡而不是新建。 -->
