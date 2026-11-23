# 经验卡索引（生成文件：`mf kb check`，不要手改）

先读这张表，按语义挑相关卡片再打开；卡片是先验，最终以本机测量为准。

| kind | card | summary | applies | conf |
|---|---|---|---|---|
| dead-end | [nt-store-hurts-downstream](cards/nt-store-hurts-downstream.md) | 输出改用 nontemporal store 让 kernel 本身变快，但结果不进 MALL，下游 GEMM 改从 HBM 读，端到端净亏 | gfx942 MALL, attention/任何输出紧接着被下游 kernel 读取的 kernel | high |
| measurement | [moe-stream-prefill-crossover-rows-per-expert](cards/moe-stream-prefill-crossover-rows-per-expert.md) | MoE 流式小 BM 路径与 prefill 的交界由每 expert 平均行数（M·topk/E）决定而非 M；prefill 一优化交界就前移，须重测，bench 没覆盖的档会藏回退 | gfx942 MI308X, FlyDSL, MoE mxfp4 权重 / fp8 激活（A8W4），两阶段 gate/up + down | medium |
| measurement | [ptl-caps-sustained-mfma](cards/ptl-caps-sustained-mfma.md) | MI308X PTL(VECTOR,F8) 下持续高 MFMA 负载会降频：BF16 按指令条数限流，FP8 在约 80% 峰值处触发，利用率目标需按持续上限设定 | MI308X gfx942, PTL Enabled VECTOR,F8, MFMA 密集 kernel（attention/GEMM），以及小 M 的 BF16 MFMA（MoE decode） | high |
| measurement | [unpaired-vs-abba-gap](cards/unpaired-vs-abba-gap.md) | 未配对自测（不同时间/不同卡对比冻结基线）与同卡 ABBA 复核可差约 4%，小于约 5% 的收益只能信 ABBA | AMD Instinct 多卡机器，GPU kernel 计时 | medium |
| pitfall | [agent-sandbox-no-tmp](cards/agent-sandbox-no-tmp.md) | 本机的 agent 运行时禁止写 /tmp；MYFLOW_HOME、worktree、TMPDIR、JIT 缓存放在 /tmp 会让子 agent 完全无法工作 | Copilot CLI / VS Code 子 agent 沙箱，本机（MI308X 节点） | high |
| pitfall | [bench-timing-follows-op-config](cards/bench-timing-follows-op-config.md) | bench 若按算子自己的配置表决定计时方式（如“decode 档用 CUDA graph 计时”），改算子的档位交界就会悄悄改变冻结的度量口径；交界应由 bench 自己固定，或改交界时重新冻结 | 任何带配置表分档的算子 bench（MoE decode/prefill、attention 分档），myflow 冻结基线 | high |
| pitfall | [flydsl-divergent-if-wave-op](cards/flydsl-divergent-if-wave-op.md) | FlyDSL 中发散 if 之后紧跟 wave 级操作（DPP/permlane/ballot 等）会在发散状态下执行，结果静默出错 | FlyDSL kernels, gfx942, wave 级归约/广播 | high |
| pitfall | [llvm-isa-nondeterminism](cards/llvm-isa-nondeterminism.md) | 同一份 LLVM IR 两次编译可能得到不同 ISA，性能差异未必来自源码改动 | FlyDSL/Triton/HIP 经 LLVM AMDGPU 后端编译的 kernel, gfx942 | medium |
| pitfall | [measure-tool-drift](cards/measure-tool-drift.md) | 计分/计时脚本在一轮测量中途被更新，前后数据口径不一致却被混在一起比较 | 任何多人/多 lane 共用测量工具的优化任务 | high |
| pitfall | [merge-default-before-dispatch-silently-disables-paths](cards/merge-default-before-dispatch-silently-disables-paths.md) | 合并多条 lane 时，一方在“未给选项才走快路径”的分派判断之前填参数默认值，会让快路径永远不走而全部用例照过；分派后再取默认值，用例按分档断言实际 kernel 序列 | 任意按选项 / 形状分派的多路径算子（本例 gfx942 FlyDSL MoE A8W4 的 decode / stream / prefill） | high |
| pitfall | [pmc-perturbs-all-gpus](cards/pmc-perturbs-all-gpus.md) | 任何人采 PMC 都会关闭整机 8 卡的 PTL 并把时钟锁在约 1.4GHz，同机并行计时全部被污染 | MI308X gfx942, rocprofv3 PMC, 多人/多 lane 共用一台机器 | high |
| pitfall | [stale-pyc-same-second](cards/stale-pyc-same-second.md) | 同一秒内修改 .py 且文件长度不变，Python 会继续用旧 .pyc，测到的是旧代码 | 所有 Python 前端的 kernel（FlyDSL、Triton、torch），快速迭代的自动化循环 | high |
| technique | [moe-decode-mxfp4-no-sort-16b-loads](cards/moe-decode-mxfp4-no-sort-16b-loads.md) | gfx942 MoE decode（M≤32）mxfp4 权重：免排序、gate/up 每路由一个 WG、down 每 token 一个 WG 在 LDS 内规约 topk 直写 bf16、16 B 加载覆盖两块/两槽，小 M 在 WG 内量化激活 | gfx942 MI308X, FlyDSL, MoE decode（M≤32），mxfp4 权重 → fp8 MFMA（LUT 解码），topk≈8–16 | medium |
| technique | [moe-sorted-block-half-m](cards/moe-sorted-block-half-m.md) | MoE 两段 GEMM 在每专家行数远小于排序块（如 M64 块里约 20 行）时，按块在运行时切到只算前 32 行的路径，Gate/Up 与 Down 的 MFMA 和写出减半；中等 T 提速约 1.35–1.40×，无需改排序与已有用例 | gfx942 MI308X, FlyDSL MoE 2-stage（上游 gfx9_fp8 prefill_1x4 / gemm2_1x4 派生）, fp8 MFMA, prefill 中等 T（T·topk/E ≈ 10–40） | high |
