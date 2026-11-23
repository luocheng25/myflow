---
name: moe-decode-mxfp4-no-sort-16b-loads
kind: technique
summary: gfx942 MoE decode（M≤32）mxfp4 权重：免排序、gate/up 每路由一个 WG、down 每 token 一个 WG 在 LDS 内规约 topk 直写 bf16、16 B 加载覆盖两块/两槽，小 M 在 WG 内量化激活
applies: gfx942 MI308X, FlyDSL, MoE decode（M≤32），mxfp4 权重 → fp8 MFMA（LUT 解码），topk≈8–16
confidence: medium
evidence: task moe-ptpc-a8w4-r2 L1 A26/A30/A31、V1（m1/m16 为 aiter fp8 ptpc 的 0.58–0.82×）；下限 tools/l4_floor.py（run 20261122-233217-L4-run-a8b5）
source: moe-ptpc-a8w4-r2，2026-11-22
notes: absolute-ok
---
**场景**：decode 小 M，aiter 的 batch1 / split-K 路径受排序、清零、规约等多次 launch 与访存指令条数限制。

**做法**：
- 不跑 moe_sorting：Gate/Up grid = (I/16, M·topk)，4 wave = gate/up × K 两半；Down grid = (H/16, M)，
  WG 内把 topk 个 expert 的部分和在 LDS 相加后直接写 bf16（不清零、不需 sorted_sum）。
- decode 受访存指令条数限制（只访存不计算 ≈ 正常耗时）：一条 dwordx4 覆盖相邻两个 64-K 块（Gate/Up）
  或两个路由槽的同一块（Down，A 操作数按 lane 半区屏蔽为零），同字节数比 dwordx2 快约 13%。
- 小 kernel 有约 4.5 µs 固定开销：M≤3 时每个 WG 自己量化整行激活，比单独的行量化 kernel 快；M 更大再切到预量化。
- 分派前检查形状上限（Gate/Up 单线程一片 16 B → H≤4096；Down 暂存的中间行进 64 KiB LDS），不满足就回退，
  别让 assert 在用户形状上炸。
- 定“比 fp8 快 2×”这类目标前，先测同协议的理想单 kernel（只读被选 expert 的全部权重、不计算）：m1 时它已约为
  aiter fp8 的 0.5×，本做法约为它的 1.55×；M=8–32 再做单 kernel 融合或按 expert 去重读权重只得 0–4%。

**怎么验证**：rocprof kernel trace 看两 kernel 耗时之和；ISA 统计 buffer_load_dwordx4 / dwordx2 条数；
对每种分派（核内量化 / 预量化）各跑端到端对参考并断言 last_path。
