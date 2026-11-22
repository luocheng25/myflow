---
name: flydsl-divergent-if-wave-op
kind: pitfall
summary: FlyDSL 中发散 if 之后紧跟 wave 级操作（DPP/permlane/ballot 等）会在发散状态下执行，结果静默出错
applies: FlyDSL kernels, gfx942, wave 级归约/广播
confidence: high
evidence: /opt/lc/work/qsa-opt/COORD_NOTES.md §4.1（B3 versions/prepare_x_divergent_if.py）
source: qsa-opt (2026-11)
---
**症状**：部分 lane 的结果错误，且只在特定数据分布下出现。

**原因**：if 分支结束后 exec mask 尚未恢复，紧随其后的 wave 级操作在部分 lane 关闭时执行。

**解决**：wave 级操作前确保收敛（把操作移出分支，或在分支后显式重新汇合）；改动后用 ISA 扫描 exec 收窄区域内的 wave 级指令。
