---
name: pmc-perturbs-all-gpus
kind: pitfall
summary: 任何人采 PMC 都会关闭整机 8 卡的 PTL 并把时钟锁在约 1.4GHz，同机并行计时全部被污染
applies: MI308X gfx942, rocprofv3 PMC, 多人/多 lane 共用一台机器
confidence: high
evidence: /opt/lc/work/qsa-opt/GOAL.md §4；COORD_NOTES.md §4.7
source: qsa-opt (2026-11)
notes: absolute-ok
---
**症状**：别人（或另一条 lane）采 PMC 时，你的计时整体变慢或抖动；PMC 期间的时间数据与平时不可比。

**原因**：PMC 采集会关闭整机 PTL 并锁频；ATT 不会。

**解决**：
- PMC 一律 `mf run <task> --lane L --machine-exclusive -- <rocprofv3 ...>`：等正在进行的计时结束，并阻止新计时开始。
- PMC 只用来算字节数/指令数，时间一律取不采 PMC 时的计时。

**怎么验证**：PMC 前后各跑一次 bench，对比时钟（rocm-smi --showclocks）与时延。
