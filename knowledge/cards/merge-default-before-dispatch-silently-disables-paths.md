---
name: merge-default-before-dispatch-silently-disables-paths
kind: pitfall
summary: 合并多条 lane 时，一方在“未给选项才走快路径”的分派判断之前填参数默认值，会让快路径永远不走而全部用例照过；分派后再取默认值，用例按分档断言实际 kernel 序列
applies: 任意按选项 / 形状分派的多路径算子（本例 gfx942 FlyDSL MoE A8W4 的 decode / stream / prefill）
confidence: high
evidence: task moe-ptpc-a8w4-r2 int4 合并 L3@25 的冲突解决（提交 f25fde9）
source: moe-ptpc-a8w4-r2，2026-11-22
---
**场景**：几条 lane 各自改了同一个分派函数，git 自动合并不报冲突。

**症状 → 原因 → 解决**：
- 症状：合并后正确性全过、性能只看到 prefill 档变快，小 M 档悄悄回到慢路径（bench 若无对应档就看不出）。
- 原因：lane A 的分派是 `if block_m is None and tile_k is None: 走 decode / stream`；lane B 在函数开头加了
  `if tile_k is None: tile_k = 128 if M > 2048 else 256`。git 自动合并了 B 的这一段（不冲突），从此 tile_k 永不为 None，
  快路径分支永远不进；回退的 prefill 结果正确，用例不会失败。
- 解决：默认值放在分派之后、只作用于选中的路径；每个分档的用例断言实际 kernel 序列（如 `last_path`），而不只是数值正确；
  合并后用 bench 逐档对比合并前两边的数字。

**怎么验证**：合并后对每个分档代表 M 打印 last_path，与预期分档表逐项比对；性能 case 每档都要有。
