---
name: nt-store-hurts-downstream
kind: dead-end
summary: 输出改用 nontemporal store 让 kernel 本身变快，但结果不进 MALL，下游 GEMM 改从 HBM 读，端到端净亏
applies: gfx942 MALL, attention/任何输出紧接着被下游 kernel 读取的 kernel
confidence: high
evidence: /opt/lc/work/qsa-opt/COORD_NOTES.md §3（B1/B2/B4 三个版本；A1/VERIFY_R5_MERGED）
source: qsa-opt (2026-11)
---
**症状**：单 kernel bench 显示提速，接上下游（o_proj GEMM）后整体变慢。

**原因**：nt 写的数据行不进 MALL；下游 kernel 原本可以从 MALL 命中，现在只能从 HBM 读。

**做法**：评估 store/cache 提示（nt、glc/slc）时，bench 必须包含下游消费者，或至少跑“本 kernel + 下游 kernel”的组合 case；只看单 kernel 时间不算数。
