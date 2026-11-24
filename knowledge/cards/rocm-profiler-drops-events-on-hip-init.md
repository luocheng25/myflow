---
name: rocm-profiler-drops-events-on-hip-init
kind: measurement
summary: ROCm 上 torch.profiler 在同卡另一进程初始化 HIP 时约 0.1 s 内记不到任何 GPU 事件（连续空 9–14 次）；靠 profiler 核对 launch 的用例要先同步、空结果按 0.05→0.4 s 退避重测，计时须独占 GPU
applies: MI308X gfx942, ROCm + torch.profiler（kineto/roctracer, ProfilerActivity.CUDA），共享 GPU 上跑的“核对实际 launch 的 kernel 名”用例，以及 aiter run_perftest 等基于 profiler 的计时
confidence: high
evidence: task gemm-a8w4 M7 收尾 runs/20261124-172452-_ms-close-c0b0（3 次紧挨着的重测在同一毫秒内全部为空）；M7 复核压力测试（独占 0/5228 次空；同卡反复起 HIP 125/8408 次空；加固后 46163 次调用 0 次不符）；M8 复核（加重测前 14 次整套运行 3 次各挂 1 例）
source: gemm-a8w4 (2026-11)
---
**场景**：用例用 `torch.profiler.profile(activities=[CUDA])` 抓本次调用 launch 的 kernel 名，再与分派预测逐个比对；GPU 与其他进程（mf 的并行 lane、复核、其他任务）共用。

**症状 → 原因 → 解决**：
- 症状：偶发 `assert [] == ['<kernel>']`，同一用例单独重跑又过；越短的 kernel（只有 2 个 CTA）越容易碰上。
- 原因：另一个进程在同一张卡上初始化 HIP 时，profiler 约 0.1 s 内一个 GPU 事件都记不到，而且是连续的。紧挨着的重测（同一毫秒内重来 3 次）会全部落空。
- 解决：进入 profiler 前先 `torch.cuda.synchronize()`；结果为空才重测，按 0.05/0.1/0.2/0.4 s 退避，最多 5 次。M=0 这类本来就不 launch 的调用仍返回 []，记到的序列仍须逐个相等，不放宽比较。
- 计时同理：aiter `run_perftest` 也用 profiler，偶尔会测出不可能的低值。正式计时（bench、ABBA）要独占 GPU，正确性用例才共享。

**怎么验证**：在一个进程里循环调用被测用例上千次，另一个进程在同卡反复 `import torch; torch.cuda.init()`，统计空结果次数；加固后应为 0 次不符。
