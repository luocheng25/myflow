# myflow — 长时间 GPU kernel / 新算子开发的轻量流程

让 agent 按**里程碑**开发或优化 kernel：每个里程碑是 GitHub 上的一个提交，性能数字由 mf 测出、可从原始日志重算，你只在少数几个点回答问题。
纯 Python 标准库（≥3.8）、零依赖；不改你的仓库（每条实现线一个 git worktree）。

你只需要看三样东西：
1. **GitHub 上的里程碑提交**：一个里程碑 = 一个提交 + tag。提交信息写做了什么、为什么、与计划的差异、覆盖、性能、下一步，末尾链接到该 tag 下的入口文档。
2. **入口文档**：给 tech lead 的设计文档，10 分钟读完能知道做什么、整体怎么做、为什么这样做、做到什么程度、风险在哪。只写当前设计（过程在提交信息里），正文 ≤150 行；每个里程碑由一个独立的复核子 agent 更新一次。
3. **测试覆盖表**：按三类列出测了什么——基本功能测试（少而快，每次改动先跑）、性能测试、其他覆盖测试（只测前两类覆盖不了的，写明原因）；不看代码也能判断可靠性。

## 安装
```bash
ln -s /opt/myflow/bin/mf /usr/local/bin/mf   # 可选：放进 PATH（否则用 /opt/myflow/bin/mf）
mf install-skills --user                      # 5 个 skill 链接到 ~/.copilot/skills、~/.claude/skills（或 --project <repo>）
mf config tasks_dir=/data/myflow/tasks        # 可选：任务数据放到别处（mf config 查看当前值与来源）
examples/toy/demo.sh                          # 可选：约 10 秒的 CPU 演示，然后 MYFLOW_HOME=~/.myflow-demo/home mf status demo
```

## 如何使用（以试点任务 moe-ptpc-a8w4 的实际过程为例）

**1. 用一句话开始。** 说清做什么、对标什么、推到哪里：
> 用 myflow 按里程碑开发 pyhip 的 MoE A8W4（PTPC）算子，功能正确，性能对标 aiter_ptpc，推送到 github.com/luocheng25/pyhip

编排 agent 按 skill `myflow-kernel-opt` 工作：建任务（`mf new`），读代码，写 `SPEC.md`。

**2. 需求确认：唯一一次必答的问题。** agent 一次问齐五件事：
- 它的理解与假设；
- 待你回答的问题；
- 性能目标与对照来源；
- 精度参考；
- 推送目标（URL + 分支）。

你回答（或说“按建议”），它用 `mf decide --gate spec` 记下你的原话。批准需求就同时批准了推送目标，之后推送不再单独问。

**3. 功能里程碑（新算子先做功能版）。** 实现者在 bootstrap worktree 做出功能正确的版本，带用例和文档骨架。每个里程碑这样收尾：
1. `mf ms prep`：在干净 worktree 跑功能用例；
2. 派 1 个独立的复核子 agent：核对入口文档，补测试覆盖表和缺的用例，写卡片，只做一次；
3. `mf ms close`：提交 + tag + 推送，提交信息写明下一步。

复核进行的同时，下一个里程碑已经开工（流水线）。
试点：M0 把上游 gfx9_fp8 kernel 更新到同名文件，M1 是 decode 路径，M2 是 prefill 路径。

**4. 冻结基线，再一轮轮做性能。** `mf freeze` 以最新的里程碑为基线，测出逐 case 数字和噪声；性能目标由对照（如 aiter_ptpc）换算。每一轮分四步：
- **计划**：`mf ms plan` 写计划卡，含预期、依据、上限估计。第一次计划和偏离上次推送承诺的计划会问你；与承诺一致的只通知。
- **并行实现**：每个方向一条 lane（独立 worktree）。子 agent 用 `mf try` 提交尝试，跑冻结的正确性测试和 bench，得到自测数字。
- **验收**：`mf ms prep` 在干净 worktree、换随机种子，在同一张卡上交替测量冻结基线、上一里程碑和候选。
  性能型里程碑必须比上一个里程碑快。自测数字复现不了的（例如靠未提交的文件变快）会被识破。
- **收尾**：同第 3 步：一个复核，然后提交 + tag + 推送。

试点结果：M3（T=1024 prefill）提速 1.238×；M4（TP2 T=16 decode）再提速 1.074×，16 个 case 都在 aiter_ptpc 的 1.053× 以内；M5/M6 整理入口文档。

**5. 过程中你看什么。**

| 想知道 | 看哪里 |
|---|---|
| 完成了哪些里程碑，各改了什么、为什么 | GitHub 分支上的里程碑提交（tag `<任务>-M<k>`）；提交信息末尾是该 tag 下渲染好的入口文档链接 |
| 整体设计、关键取舍、覆盖了哪些情况、基线从哪来 | 入口文档（试点：`src/pyhip/ops/moe/flydsl/a8w4_ptpc/README.md`） |
| 某个 kernel 的任务划分与逐拍流水线 | 入口文档同目录的 `kernels.md`（入口文档第 2 节有链接） |
| 现在在做什么、有没有跑偏 | `mf status <任务>`：路线图（✅ 已推送 · 🔄 收尾中 · ⏳ 承诺）、方向检查（承诺兑现、预测偏差、规模、收益曲线…）、提醒 |
| 某个里程碑 / 某次尝试的细节 | `mf status <任务> M3` · `mf status <任务> A7` |
| 网页 | `mf serve`：看板、渲染 Markdown、直接批准问题、给 agent 留言（VS Code Remote 需在 Ports 面板转发端口） |

agent 是否在推进，由证据判断，不看它的自述：

| 信号 | 含义 |
|---|---|
| `stale` | 什么活动都没有（可能卡死或退出） |
| `spin` | 还在调用 mf，但没有进展记录，文件也没动（原地打转） |
| `no_record` | 文件在改但没记录（在干活） |
| `await_user` | 在等你答复 |
| `no_report` | 有进展，但超过 60 分钟没向你汇报 |

mf 挂起的问题，agent 30 分钟内没有转达给你，会升为严重预警。

**6. 过程中你做什么（只在需要时）。**

| 情况 | 你说 / 做 | mf 里的记录 |
|---|---|---|
| agent 问你问题 | 回答 | `mf decide <任务> --gate <key> "…" --quote "<你的原话>"` |
| 补充要求或纠正方向 | 直接告诉 agent，或 `mf steer <任务> "…"` | agent 处理后回执 `mf note <任务> --ack N "…"` |
| 已推送的里程碑做错了（试点：“M0 移植方式错误，按 … 重做”） | 告诉 agent | `mf ms revoke` 撤回最近一个里程碑，同一编号重做；覆盖远端前问你 |
| 要离开一段时间（“接下来 10 小时有问题按默认值进行”） | 告诉 agent | `mf decide <任务> --delegate 10 "<原话>"`；代决的事单独列出，你回来先看 |
| 停止 | `mf steer <任务> --stop "…"` | 不再开始新尝试，编排者收尾并出报告 |

**7. 结束。** agent 写 ≤3 行摘要，然后：
1. 运行 `mf report` 生成 REPORT.md：最终数字、里程碑、死路与经验、决策。报告附审计：所有数字从原始日志重算，并检查哈希链完整。
2. 用 skill `myflow-learn` 沉淀 ≤3 张经验卡。
3. 你同意后，`mf report <任务> --done`。

## 命令（19 个；`mf <cmd> -h` 看参数）
| 谁用 | 命令 | 用途 |
|---|---|---|
| 你 | `status [任务] [M3\|A7] [--lane L]` | 总览 / 任务 / 里程碑 / 尝试 / lane |
| | `serve` | 网页看板：渲染 Markdown、批准问题、留言 |
| | `steer <任务> "…" [--stop]` | 给 agent 留言；`--stop` 结束工作 |
| 编排者 | `new` · `set key=value [--why]` | 建任务；改 task.json（目标、预算、范围、推送目标……，改动显示在看板） |
| | `brief "…" [--ask] [--gate KEY]` | 向你汇报；`--ask` 提问；`--gate`：已把 mf 挂起的问题转达给你 |
| | `decide "…" [--gate KEY\|auto] [--quote] [--default] [--delegate H]` | 记录你的答复；授权期内按默认值代决；记录授权 |
| | `freeze` | 冻结基线 |
| | `ms plan\|bootstrap\|prep\|review\|close\|push\|drop\|revoke` | 里程碑：计划 → 验收 → 复核 → 提交推送；放弃 / 撤回 |
| | `await` | 后台等到需要编排者处理（问题待转达、留言、该汇报、卡住） |
| | `report [--done]` | 报告 + 审计；`--done` 结束任务 |
| 实现者 | `lane new\|set` | 开 / 关一条实现线（独立 worktree） |
| | `try --lane L --idea "…"` | 提交一次尝试：冻结的正确性测试 + bench |
| | `run -- <cmd>` | 在任务里记录任意命令（profiling），带 GPU 租约 |
| | `note "…" [--kind K] [--ack N]` | 不带 `--kind`：在做什么（证明活着）；`--kind finding\|deadend\|survey\|plan\|summary\|lesson`：进展；`--ack N`：处理了留言 #N |
| 设置 | `kb search\|new\|check` · `config` · `install-skills` · `selftest` | 经验卡；数据位置；安装 skill；自测 |

退出码：0 成功 · 1 检查未通过（审计）· 2 错误 · 3 被守卫拒绝（预算、问题未答、已停止、停滞、收尾条件未满足）。

<details><summary>旧命令对照（11-22 精简前的写法）</summary>

| 旧 | 新 |
|---|---|
| `phase <p>` | 阶段随动作自动推进；结束用 `report --done` |
| `asked <id> <key>` | `brief <id> --gate <key> "问了什么"` |
| `delegate --hours H --quote "…"` / `--off` | `decide <id> --delegate H "<原话>"` / `--delegate 0 "…"` |
| `ping "…"` · `ack <id> N "…"` | `note "…"` · `note <id> --ack N "…"` |
| `stop "…"` | `steer <id> --stop "…"` |
| `budget --attempts +N` | `set <id> budget.max_attempts=<N> --why "…"` |
| `verify` | `ms prep` 自己复核候选 |
| `ceiling X "依据"` | `ms plan … --ceiling X --basis "…"` |
| `hyp add/set` | `note --kind plan\|finding\|deadend`（写明证据 A7 / V3） |
| `show` · `ms show` · `tail` · `watch` · `render` · `guard` · `gpu` | `status`（`status <id> M3`、`status <id> A7`、`--lane`） |
| `audit` | `report`（每次都审计） |
| `ms role` | `ms review`（旧写法仍可用） |
| `doc render` | prep / close 自动渲染 |
| `kb index` · `kb lint` | `kb check` |
| `tools` | lane brief 自动列出任务的 `tools/` 脚本 |

</details>

## 为什么数字可信、不会跑偏
- **数字只由 mf 产生**：`mf try` 给自测数字；`mf ms prep` 在干净 worktree、换种子、同卡交替测量，给官方数字。
  正确性测试和 bench 在冻结时记下哈希，改了就判 `oracle_modified`。事件日志是哈希链，`mf report` 从原始日志重算全部数字。
- **推送的就是验收过的**：推送提交的运行时代码与验收的候选逐 blob 相同，并在最终提交上重跑功能用例。复核只能改文档和用例。
  改动 / 删除已有用例不用你批准，但必须用 `--tests-why` 说明符合测试原则：
  - 用例分三类：基本功能测试（`commands.correctness`，每次 `mf try` 先跑）、性能测试（bench）、
    其他覆盖测试（`commands.verify_extra`，prep / 收尾时跑，只测前两类覆盖不了的）；能覆盖的不再加，但不缺失；
  - 三类测试只放一个文件，用注释分区：`-m basic` 选基本功能、`-m "not basic"` 选其他覆盖，性能测试是该文件的脚本入口
    （放在文件最后：mf 只把它和它用到的顶层定义算 bench，复核仍可改前面的用例）；不随意新建测试文件；
  - 违反原则（例如放宽容差）才问你。
- **不会无限跑下去**：
  - 尝试 / 时间预算；
  - 每条 lane 的“连续无提升”上限；
  - 超时杀掉整个进程组；
  - 上限估计低于目标时，先问你是否调整目标；
  - 方向检查：承诺兑现、预测偏差、收益曲线等。

## task.json 契约（关键字段）
```jsonc
"metric":   {"direction": "min", "aggregate": "geomean", "target": 1.25, "min_improve": 0.02}
"commands": {"correctness": "...", "bench": "... 打印 MF_METRIC case=<c> value=<v>", "bench_quick": "", "verify_extra": "", "gate": "", "setup": ""}
"scope":    {"allow": ["aiter/ops/flydsl/**"], "protect": ["op_tests/test_x.py", "bench.py"]}
"env":      {"FLYDSL_RUNTIME_CACHE_DIR": "{lane_dir}/cache/flydsl", "TMPDIR": "{lane_dir}/tmp", "PYTHONPATH": "{wt}"}
"gpu":      {"pool": "0-7", "gate": "strict", "max_use": 5, "max_vram": 20}
"budget":   {"max_attempts": 40, "max_hours": 24, "max_no_improve": 10, "max_fail_streak": 4, "heartbeat_min": 30, "report_min": 60, "spin_min": 120, "timeout_s": 1800}
"verify":   {"pairs": 4}   "gates": {"spec": "ask", "plan": "ask"}   "survey": {"repos": ["/opt/aiter", "/opt/lc/FlyDSL"]}
"git":      {"url": "https://github.com/<you>/<repo>.git", "branch": "<you>/<task>", "push": "first-ask", "lang": "en"}   // 仓库是 pyhip 时 mf new 自动设为 zh
"doc":      {"entry": "src/pyhip/ops/<op>/README.md", "paths": ["src/pyhip/ops/<op>/**/*.md"], "lang": "zh", "max_lines": 150, "max_line_chars": 120}
"tests":    {"paths": ["tests/ops/<op>/**"]}   "milestone": {"max_lines": 800, "max_hours": 24}
```
完整示例（aiter + FlyDSL）：`templates/task.example.json`。命令中的模板变量：`{wt} {lane} {lane_dir} {run} {gpu} {seed} {task_dir} {repo} {base}`。

## 目录
```
bin/mf                 CLI 入口            myflow/            实现（core/runner/gitops/guard/render/serve/kb/cli/ms）
skills/                5 个 skill          knowledge/         经验卡 cards/ + 生成的 INDEX.md
templates/             SPEC / lane brief / 入口文档 / kernel 设计说明 / 经验卡 / task 示例
examples/toy           CPU 演示            examples/torch-rmsnorm   真实 GPU 示例（bench/正确性参考写法）
docs/DESIGN.md         方案与取舍          tests/             自测（mf selftest）
~/.myflow/             registry.json · STATUS.md · index.html · locks/ · tasks/<id>/{task.json,SPEC.md,events.jsonl,STATUS.md,dashboard.html,runs/,lanes/,wt/,milestones/,notes/,tools/}
```
数据位置用 `mf config` 持久设置（`~/.config/myflow/config.json`，`MYFLOW_CONFIG` 可换位置），同名环境变量优先：`home` / `MYFLOW_HOME`（任务登记、总览，默认 `~/.myflow`；不要放 /tmp——部分 agent 沙箱禁止写 /tmp）、`tasks_dir` / `MYFLOW_TASKS_DIR`（新任务的数据目录，默认 `<home>/tasks`；单个任务也可 `mf new --dir`）、`lock_dir` / `MYFLOW_LOCK_DIR`（多用户共享 GPU 锁时设为所有人可写的公共目录）、`kb` / `MYFLOW_KB`（经验卡目录，默认 myflow 自带的 knowledge/）。`mf config home=<新目录> --fresh` 开一个空的新环境（不登记原有任务），例如从零重做一个任务。其他环境变量：`MYFLOW_PYTHON`。
