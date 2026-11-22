---
name: llvm-isa-nondeterminism
kind: pitfall
summary: 同一份 LLVM IR 两次编译可能得到不同 ISA，性能差异未必来自源码改动
applies: FlyDSL/Triton/HIP 经 LLVM AMDGPU 后端编译的 kernel, gfx942
confidence: medium
evidence: /opt/lc/work/qsa-opt/COORD_NOTES.md §4.9（2 个 BF16 变体，J2/VERIFY_R4_B4）
source: qsa-opt (2026-11)
---
**症状**：源码只改了无关处，或根本没改，性能却变了；或复核与自测不一致。

**原因**：后端编译存在不确定性（调度/寄存器分配可能变化）。

**解决**：比较性能前先核对 ISA/ELF 身份（指令数、VGPR/SGPR、spill、关键循环的指令序列）；把“ISA 是否相同”写进尝试的 note；结论只基于同一份已核对的产物。
