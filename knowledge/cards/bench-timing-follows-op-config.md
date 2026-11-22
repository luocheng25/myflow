---
name: bench-timing-follows-op-config
kind: pitfall
summary: bench 若按算子自己的配置表决定计时方式（如“decode 档用 CUDA graph 计时”），改算子的档位交界就会悄悄改变冻结的度量口径；交界应由 bench 自己固定，或改交界时重新冻结
applies: 任何带配置表分档的算子 bench（MoE decode/prefill、attention 分档），myflow 冻结基线
confidence: high
evidence: task moe-ptpc-a8w4 R1：L1 发现 T=32..128 走 prefill 档快 9%–14%，但 benchmarks/moe/bench_moe_a8w4_ptpc.py 用 a8w4_ptpc.select_config(T)["path"]=="decode" 决定是否 graph 计时
source: moe-ptpc-a8w4 (2026-11)
---
**症状**：性能 lane 想把 decode/prefill 交界从 128 下移，自测“变快”。但 bench 计时方式也跟着从 graph 变成非 graph，对照行同样变了，数字和冻结基线不再同口径。

**原因**：bench（oracle）读取了运行时代码（算子配置表）来决定度量协议，lane 改运行时代码就等于改了度量。

**解决**：
- 写 bench 时把“哪些 T 用 graph 计时”写死在 bench 里，或按 case 名固定，不依赖算子配置；
- 已冻结的任务：本轮不动交界，作为单独里程碑处理并重新冻结；
- 审 lane 改动时，查 bench 是否 import 了被改的模块。

**怎么验证**：`grep -n "select_config\|CONFIGS" <bench>`，bench 的计时分支不应依赖被优化代码。
