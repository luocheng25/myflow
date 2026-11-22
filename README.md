# myflow — 长时间 GPU kernel 优化的轻量 harness

把“多轮、多 agent、长时间”的 kernel 优化变成**看得见、数字可信、不会无限跑下去**的流程。
纯 Python 标准库（≥3.8）、零依赖；代码在 `/opt/myflow`，数据在 `~/.myflow`，不改你的仓库（每条线一个 git worktree）。

| 你的诉求 | myflow 的做法 |
|---|---|
| 1 防走偏、可视化 | 分层视图 L0→L3（全部由事件自动生成）＋ **里程碑路线图与方向检查** ＋ 需求/计划 gate ＋ 早预警 ＋ 双向窗口（批准、留言） |
| 2 重复任务一致 | 每个重复步骤是一条 `mf` 命令；命令、环境、解析、范围写进冻结的 `task.json` 契约 |
| 3 可扩展、积累经验 | 5 个精炼 skill；经验卡 + 生成的索引；任务内 `tools/` 脚本自动列入 lane brief |
| 4 多任务并行 | lane = 独立 worktree + 独立缓存目录；flock GPU 租约（计时独占、正确性共享、PMC 全机独占）；全局总览 |
| 5 防谎报性能 | 数字只由脚本产生；冻结 oracle 哈希；干净 worktree + 换种子 + 同卡 ABBA 复核；哈希链 + 原始日志重算审计 |
| 6 防卡死 | 超时杀整个进程组；尝试/时间预算；按 lane 的“连续无提升”上限；心跳过期告警；上限估计低于目标先问人 |
| 7 优化的流程 | spec → freeze → survey → plan → implement ⇄ verify → 收尾里程碑（推送）→ report（参考 GEAK，见 docs/DESIGN.md、docs/DESIGN-v2-milestones.md） |
| 8 轻量 | 一个 `mf` 命令；`mf new --repo <任意仓库>` 即用；skills 可装到任何 agent（Copilot / Claude / Codex） |

## 30 秒看效果
```bash
/opt/myflow/examples/toy/demo.sh            # CPU 演示（~10 秒）：3 条 lane、失败、超时、停滞、被复核识破的虚高、一个推送了的里程碑、待确认 gate
MYFLOW_HOME=~/.myflow-demo/home /opt/myflow/bin/mf status
MYFLOW_HOME=~/.myflow-demo/home /opt/myflow/bin/mf ms show demo     # 路线图 + 方向检查
MYFLOW_HOME=~/.myflow-demo/home /opt/myflow/bin/mf serve    # 浏览器打开 http://127.0.0.1:8765/（VS Code Remote 需在 Ports 面板转发）
/opt/myflow/bin/mf selftest                 # 自测（CPU，约 15 秒）
```

## 在任何工程里使用
```bash
ln -s /opt/myflow/bin/mf /usr/local/bin/mf  # 可选：放进 PATH
mf install-skills --user                     # 或 --project <repo>：把 5 个 skill 链接到 ~/.copilot/skills、~/.claude/skills
mf config tasks_dir=/data/myflow/tasks       # 可选：任务数据放到别处（还有 home、lock_dir；mf config 查看当前值与来源）
```
然后对 agent 说：“用 myflow 优化 `<仓库>` 里的 `<kernel>`，目标 `<X>×`，参考 `<repo1>,<repo2>` 的同类实现”
（新算子：“用 myflow 按里程碑开发 `<算子>`”）。
编排 agent 会按 skill `myflow-kernel-opt` 走流程；在 spec 和 plan 两个 gate 停下来问你，首次推送前也会问你推送目标。

## 可视化：路线图 + 分层视图 + 一个双向窗口
| 层 | 回答的问题 | 在哪看 |
|---|---|---|
| L0 总览 | 每个任务：阶段、健康、最佳复核/自测、目标进度、预算、**里程碑进度与下一步**、最后活动、**需要你做什么** | `mf status` · `~/.myflow/STATUS.md`（VS Code Markdown 预览会自动刷新）· `index.html` |
| L1 任务 | **路线图**（✅ 已推送 · 🔄 收尾中 · ⏳ 承诺）、**方向检查**、阶段条、需要你处理、KPI、性能轨迹图（自测○ / 复核◆ / 失败×）、预警、brief、lanes、假设台账、决策 | `mf show <id>` · `mf ms show <id>` · `STATUS.md` · `dashboard.html` |
| L2 里程碑 | 计划卡（预期与依据）、候选与运行时树、增量 / 累计性能、复核记录、**里程碑卡**、提交 / tag | `mf ms show <id> M3` · 看板里点开某个里程碑 · GitHub 上的提交信息 |
| L3 尝试 | 想法、提交与 diff、正确性/门禁、逐 case 数字、复核记录 | `mf show <id> --attempt N` · 看板里点开某次尝试 |
| L4 原始 | 每次运行的完整日志、diff.patch、abba.json、events.jsonl | 看板链接 · `runs/` 目录 |

- **里程碑**（设计见 `docs/DESIGN-v2-milestones.md`）：一个可评审的完整改动 = 一个提交 + tag，推送到你批准的 fork；
  提交信息就是卡片（做了什么 / 为什么 / 与计划的差异 / 覆盖 / 已知问题 / 性能 / 下一步）。GitHub 不渲染提交信息里的 Markdown，
  所以 mf 把卡片转成纯文本，并附上该 tag 下入口文档的链接（渲染版：本里程碑一节、测试覆盖、性能与基线来源）。
  性能型必须对**上一个里程碑**做同卡 ABBA 且 improved；
  收尾默认只派 1 个独立复核（skill `myflow-review`，只做一次、直接改不打回）：写 / 核对入口文档的本里程碑一节、基线与对照来源和测试覆盖表、
  补缺的用例、填卡片。它只能改文档和用例：改动 / 删除已有用例不用你批准，但要 `--tests-why` 说明为何符合测试原则（写进提交信息），
  违反原则才问你。推送的提交与验收的候选运行时树逐 blob 相同，并在最终提交上重跑功能用例。
- **方向检查**：目标对齐 · 承诺兑现（上次推送说的下一步 vs 这次做的）· 预测偏差（实测 / 预期）· 规模 · 质量 · 收益曲线 · 时间 · 未推送。
- **gate**：spec、plan（每轮）默认 `ask`——agent 用 ≤600 字的 brief 复述理解、假设和问题，等你确认后才能往下走。
  推送目标随需求一起确认（批准 spec 即批准推送到当时设置的 URL + 分支），之后不再单独问；目标变了或要覆盖远端时，mf 才挂起 gate。
  这类 gate 只是记录：编排者在对话里问过你之后要 `mf asked`，否则看板标“agent 尚未转达给你”，30 分钟后升为 crit。
- **网页**：`mf serve` 把卡片、笔记、入口文档等 Markdown 渲染成网页（文件链接加 `?raw=1` 看原文）；STATUS.md 用 VS Code 的 Markdown 预览看。
- **早预警**（每次 `mf` 调用都会打印给 agent，也显示在看板）：需求未确认 / SPEC 确认后被改 / 目标或预算被改 / lane 停滞 /
  自测未被复核复现 / 改了 oracle 或越界 / 超时 / 上限估计低于目标 / 留言未处理 / 方向检查的信号 / gate 未转达 / 有进展没汇报。
- **是否在推进**由证据判定（不看自述）：`stale` 无任何活动 · `spin` 还在调 mf 但长时间没有进展、文件也没动（原地打转）·
  `no_record` 文件在改但没记录（在干活）· `await_user` 在等你回复 · `no_report` 有进展但超过 60 分钟没向你汇报（看板显示“最后向你汇报 N 前”）。
- **双向窗口**：`mf serve` 页面上可直接批准 gate、给 agent 留言；或命令行 `mf steer <id> "…"`。agent 下一次调用任何 `mf` 命令就会看到，并需 `mf ack` 回执。

## 流程（skill `myflow-kernel-opt` 的骨架）
阶段由动作自动推进（`ms plan` / `lane new` / `ms prep` / `ms close --next report`），只有结束时 `mf phase <id> done`。
- 计划：与上次推送承诺的下一步一致时只通知，偏离或改了目标才问你。
- 流水线：`ms prep` 后下一个里程碑立即开工，复核同时进行；下一个 prep 时 mf 自动合并上一个收尾提交，并校验代码不变。
- 验收：prep 自己补测缺的数字，两个都缺时做一次三方交替测量（冻结基线 / 上一里程碑 / 候选）；集成 lane 不用 `mf try`。
- 功能用例：同一份代码和用例在干净 worktree 里只跑一次，结果复用。

```
spec ─gate→ [新算子：bootstrap M0] → freeze → survey → plan ─gate→ implement(lanes ∥) ⇄ verify → 收尾 M_k → report → done
  对齐需求       功能正确版+用例+文档      冻结基线   调研同类   计划卡+分线      每条 lane 一个子 agent   官方数字   复核+提交+推送
                                                         ↑──────────────── 未达标且预算未尽：下一轮 R2 ────────────────┘
```

## 命令速查（`mf <cmd> -h` 看参数）
| 类别 | 命令 |
|---|---|
| 任务 | `new` `set key=value` `budget --attempts +10` `phase <p> --brief` `brief [--ask]` `asked <gate>` `decide --gate <key> [--default]` `delegate --hours H --quote …`（你不在时按默认值决定，代决事项单独列出） `await`（后台等到需要编排者处理） `stop` |
| 测量 | `freeze [--base M0]` · `lane new/set` · `try --lane L --idea` · `verify --lane L [--attempt N] [--against M3]` · `run -- <cmd>` · `gpu` |
| 里程碑 | `ms plan <id> M3 --title … --expect 1.1 --basis …` · `ms prep <id> M3 --lane L` · `ms role <id> M3 --session … --card …`（复核） · `ms close <id> M3 --next M4` · `ms push` · `ms drop` · `ms revoke`（撤销最近收尾的，重做同一编号） · `ms show` · `ms bootstrap` · `doc render` |
| 台账 | `hyp add/set` · `ceiling X "依据"` · `note [--file]` · `ping "在做什么"` · `steer` · `ack` |
| 查看 | `status` · `show [--attempt N/--lane L]` · `tail` · `watch` · `render` · `serve` · `guard` · `audit` · `report` |
| 经验 / 设置 | `kb index/lint/search/new` · `tools` · `install-skills` · `config [home=… tasks_dir=… lock_dir=…]` · `selftest` |

退出码：0 成功 · 2 错误 · 3 被守卫拒绝（预算、gate、停止、停滞、收尾条件未满足）。

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
"doc":      {"entry": "src/pyhip/ops/<op>/README.md", "paths": ["src/pyhip/ops/<op>/**/*.md"], "lang": "zh", "max_lines": 250}
"tests":    {"paths": ["tests/ops/<op>/**"]}   "milestone": {"max_lines": 800, "max_hours": 24}
```
完整示例（aiter + FlyDSL）：`templates/task.example.json`。命令中的模板变量：`{wt} {lane} {lane_dir} {run} {gpu} {seed} {task_dir} {repo} {base}`。

## 目录
```
bin/mf                 CLI 入口            myflow/            实现（core/runner/gitops/guard/render/serve/kb/cli/ms）
skills/                5 个 skill          knowledge/         经验卡 cards/ + 生成的 INDEX.md
templates/             SPEC / lane brief / 入口文档 / 经验卡 / task 示例
examples/toy           CPU 演示            examples/torch-rmsnorm   真实 GPU 示例（bench/正确性参考写法）
docs/DESIGN.md         方案与取舍          tests/             自测（mf selftest）
~/.myflow/             registry.json · STATUS.md · index.html · locks/ · tasks/<id>/{task.json,SPEC.md,events.jsonl,STATUS.md,dashboard.html,runs/,lanes/,wt/,milestones/,notes/,tools/}
```
数据位置用 `mf config` 持久设置（`~/.config/myflow/config.json`，`MYFLOW_CONFIG` 可换位置），同名环境变量优先：`home` / `MYFLOW_HOME`（任务登记、总览，默认 `~/.myflow`；不要放 /tmp——部分 agent 沙箱禁止写 /tmp）、`tasks_dir` / `MYFLOW_TASKS_DIR`（新任务的数据目录，默认 `<home>/tasks`；单个任务也可 `mf new --dir`）、`lock_dir` / `MYFLOW_LOCK_DIR`（多用户共享 GPU 锁时设为所有人可写的公共目录）。其他环境变量：`MYFLOW_KB`（另一个经验库）、`MYFLOW_PYTHON`。
