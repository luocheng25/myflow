---
name: myflow-bench
description: >-
  编写或审查 myflow 冻结的正确性测试和 bench（MF_METRIC 输出、计时协议、防作弊检查、机器门禁）。
  Use when setting up commands.correctness / commands.verify_extra / commands.bench for a myflow task, when benchmark numbers look
  suspicious (too good, unstable, not reproduced by the verify in mf ms prep), or when asked how to benchmark a GPU kernel
  reliably on AMD (HIP, FlyDSL, Triton, torch). Not for root-causing performance with profilers.
---

# 冻结的正确性测试与 bench

## 输出约定
- bench：每个 case 打印一行 `MF_METRIC case=<名字> value=<数字> [unit=us] [flops=<F>] [bytes=<B>]`；同一 case 多行取中位数。
  也可用 `parse.mode=regex`（命名组 case/value）或 `json`（`{run}/metrics.json`），见 `/opt/myflow/templates/task.example.json`。
- 对照实现（同进程、同协议计时）：每个 case 打印 `MF_REF case=<名字> impl=<名字> value=<数字> [unit=us] [flops=] [bytes=]`，
  只进表格不进指标；只取第一个 impl。
- 吞吐（用户 11-22：性能数据含对照一律带 TFLOPS 与带宽）：F = 每次调用的运算量（GEMM 计 2·M·N·K），
  B = 每次调用最少必须搬运的字节（用到的权重 / 输入各读一次、输出写一次，按各实现自己的数据格式算）。
  mf 据此在 STATUS.md 逐 case、REPORT.md、入口文档生成块和 try / 复核输出里给出每个实现的 TB/s、TFLOPS；
  在 task 里设器件峰值 `perf.peak_tbps`、`perf.peak_tflops` 显示占峰值。已冻结、只在别处打印吞吐的 bench
  不改，用 `perf.extract`（字段 tbps / tflops / ref_tbps / ref_tflops，正则命名组 case / value）读出。
- 正确性：exit 0 = 通过。两者都放进 `scope.protect`，`mf freeze` 后就不能再改。
- 参考实现：`/opt/myflow/examples/torch-rmsnorm/make_repo.sh` 里的 `bench.py` 和 `test_kernel.py`。

## 计时协议（默认）
- 计一次完整调用（含 prologue / epilogue / 尾部），不是稳态片段；JIT、准备、校验不计入。
- warmup ≥2 次（JIT / autotune 在这里发生）；采样 ≥20 次取中位数；用 GPU event 计时并 synchronize。
- 轮换 ≥4 组输入；需要冷缓存时每次调用前写一个大于 L2+MALL 的 flush buffer。
- 输入用 `MF_SEED` 播种（复核会换种子）；case 集合与 `SPEC.md` 一致。
- 打印全部 case，不在 bench 里挑结果（不丢长尾、不按频率筛样本）。

## 正确性：三类用例（测试原则见 skill myflow-kernel-opt 第 13 条）
- `commands.correctness` = **基本功能测试**：简短、数量少，覆盖最常用的主路径（每个公开入口 / 分档 1 例）；
  每次 `mf try` 都先跑，整套控制在 1–2 分钟内。
- `commands.verify_extra` = **其他覆盖测试**：边界 shape（非对齐、最小、最大、交界两侧）、报错、graph 等前两类覆盖不了的；
  prep 与收尾时在干净 worktree 跑。前两类能覆盖的不再加。
- 三类测试放同一个文件（注释分区）：基本功能标 `@pytest.mark.basic`，`correctness` 用 `-m basic`、`verify_extra` 用 `-m "not basic"`；
  bench 写成该文件的 `__main__` 脚本入口（`commands.bench` = `python <文件>`，函数名不以 `test` 开头，pytest 不收集），
  性能测试区域放在最后一个 test / fixture 之后（mf 据此把它及其用到的顶层定义算作 bench，其余算用例）；
  不新建测试文件（用户 11-23，见 kernel-opt 第 13 条；上面的参考实现仍是分开的两个文件，只借鉴写法）。
- `commands.bench` = **性能测试**：每个分档至少一个 case。性能数据是最重要的交付物（用户 11-23）：每个配置把主规模维度
  按 2 的幂细扫，覆盖使用范围两端（如 batch 1、2、4…32768），不只取分档代表点；每个 case 都打印 MF_REF 与 flops / bytes。
  `mf freeze` 对按 2 的幂不连续的档位报警（确实不需要时 `perf.sweep=false`）。
- 对独立参考（高精度或原实现）逐元素比较；容差写死。校验的必须是被计时的那条路径与那个输出。
- 能断言时，断言新路径确实被执行（例如检查 dispatch 选中的 kernel 名），防止静默回退。

## 常见“假提速”与防法
| 手法 | 防法 |
|---|---|
| 改测试 / bench / 容差 | `scope.protect` 冻结哈希，改了即 `oracle_modified` |
| 缓存结果、只算一次 | 轮换输入；verify 换种子 |
| 针对 bench 的 shape 特判 | `commands.verify_extra` 跑额外 shape |
| 异步未同步、计时漏 kernel | 计时只在冻结的 bench 里做 |
| 新 kernel 没被调用（回退旧路径） | 正确性里断言 dispatch；或 `mf run` 抓 kernel trace |
| 本地未提交产物、旧 .pyc | verify 用干净 worktree；mf 运行前清 `__pycache__` |
| 换卡 / 换时段比较 | verify 同卡交错 ABBA 对照冻结基线 |
| profiling 干扰他人计时 | `mf run --machine-exclusive` |

## 机器门禁
- `gpu.gate=strict`：计时前后各查一次，GPU use ≤ `max_use`% 且 VRAM ≤ `max_vram`%；遥测未知不算空闲。
- 额外条件（PTL 状态、时钟下限等）写成 `commands.gate` 脚本，exit 0 才计时。
- 门禁失败的数据保留但不计入；不要反复重跑直到“碰上”一个好数。
