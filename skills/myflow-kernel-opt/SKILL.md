---
name: myflow-kernel-opt
description: >-
  编排长时间的 GPU kernel / 新算子开发（或整模型）优化任务：与用户对齐需求 → 冻结基线（新算子先做功能版 M0）→ 调研同类实现 →
  按里程碑规划并行 lane → 派发实现 → 独立复核 → 里程碑收尾（一个独立的复核子 agent，只做一次）并推送到 GitHub → 报告与经验沉淀，
  全程用 myflow 的 `mf` 命令记录、可视化、防谎报、防卡死。
  Use when the user asks to optimize / speed up / tune a kernel or op toward a performance target, develop or implement
  a new operator in milestones, run a multi-round or multi-agent optimization, extend it to a whole model, or mentions
  myflow, mf, 里程碑, kernel 优化, 新算子, 性能目标, 并行尝试, 复核.
  Do not use for a one-off benchmark question or plain code review.
---

# myflow 编排者

你负责对齐、规划、派发、复核、汇报；改代码交给 lane 子 agent（skill `myflow-lane`）。
`mf` 不在 PATH 时用 `/opt/myflow/bin/mf`。任务目录 `~/.myflow/tasks/<id>/`（SPEC.md、survey.md、plan.md 都写在这里）。
每条 `mf` 输出末尾的「mf 提醒」必须先处理再继续。超过 20 分钟没有其他 mf 调用时（读代码、调研），`mf note <id> "在做什么"`。

## 铁律
1. 性能数字只来自 `mf try`（自测、未配对）和 `mf ms prep` 的复核（官方：干净 worktree + 同卡 ABBA）。向用户汇报时引用 `mf status` 的表。
2. gate=ask 的阶段：`mf brief <id> --ask "…"`（≤600 字）后**停下来问用户**；得到回复再 `mf decide <id> --gate <key> "…" --quote "<用户原话>"`。绝不替用户批准。
3. 冻结后 lane 不改 oracle（`scope.protect` 下的用例与 bench），也不改度量契约（bench / 正确性命令、`scope.protect` 等）；必须改契约时先问用户，再 `mf set` + `mf freeze`。收尾时复核可按第 13 条补 / 改用例（bench 除外）。
4. 遇到 REFUSED（exit 3）或 crit 预警：停止派发，向用户汇报现状并给出选项。
5. 📨 用户留言优先：照做，然后 `mf note <id> --ack <n> "怎么处理的"`。
6. mf 自己挂起的 gate（推送目标变了的 `push:…`、覆盖远端的 `push-replace:…`）只是记录，**mf 不会问用户**。在任何 mf 输出里（包括子 agent 带回的）看到「需要用户确认【key】」：
   当回合就在对话里问用户（附上要确认的内容，不要攒着和别的问题一起问），然后 `mf brief <id> --gate <key> "问了什么"`；用户回复后 `mf decide`。
   未转达的 gate 超过 30 分钟会升为 crit。
7. 汇报 = 在对话里告诉用户 **并** `mf brief <id> "做完了什么 / 下一步 / 风险"`（不带 `--lane`）。mf 只看得到 brief：
   有进展超过 `budget.report_min`（默认 60 分钟）没汇报会提醒 `no_report`；看板显示“最后向你汇报 N 前”。
8. 子 agent 在后台干活时，同时在后台运行 `mf await <id>`：需要你处理时（未转达的 gate、用户留言、该汇报了、卡死 / 打转）它会返回，
   处理完再启动它；子 agent 的完成通知先到，就照常处理。不要干等子 agent 结束才汇报。
9. 语言：在 pyhip 开发 / 推送到 pyhip 时，文档、提交信息（含子 agent 在 worktree 里的提交）、PR 描述一律中文；代码、标识符、命令
   保持原文。`mf new` 会据仓库名设 `git.lang = doc.lang = zh`；派子 agent 时在 prompt 里写明。其他仓库沿用仓库原有语言。
10. 计划或方向在角色工作中途变了（交界、范围、某个结论）：先问用户、记录决定，再立刻通知正在写文档 / 用例的子 agent——不要等复核去发现。
    规则规定由用户决定的事（例如变慢 >3% 的 case），一律问用户，编排者不自己下结论。
11. 精简流程（用户 11-21 / 11-22 定的规则）：用户只看三样——GitHub 上的里程碑提交、说明“做了什么 / 有没有偏差”的文档、测试覆盖表。
    收尾 = 1 个独立复核子 agent（skill `myflow-review`），只做一次（约 30 分钟），发现问题直接改、不打回。
    **只在这些时候问用户**：
    - 需求确认：一次问齐需求、性能目标与对照来源、推送目标；
    - 第一次计划，以及偏离上次推送承诺的计划（换方向、改目标）；与承诺一致的计划 mf 只通知；
    - 方向或计划要变；
    - 有 case 变慢超过 3%；
    - 用例改动违反测试原则（第 13 条）；
    - 覆盖远端。
    其余流程能省就省。派复核前，先按实现报告里的偏差逐条决定或问用户，并 `mf decide` 记录；复核就能一次写对，不用回头改措辞。
    **流水线**：`ms prep` 之后立刻派下一个里程碑的实现者，与复核并行。
    - 功能版继续在 bootstrap worktree 做；
    - 性能 lane 新建时默认从收尾中的候选开始。
    下一个 `ms prep` 时，mf 自动合并上一个收尾提交（只多了文档 / 用例），并校验代码不变；收尾不会重置正在做的工作。
12. 用户授权按默认值决定（例如“接下来 10 小时有问题按默认值进行”）：先 `mf decide <id> --delegate 10 "<原话>"`。授权期内不问用户：
    需要确认的事按默认值（你在表单里会设为 default 的推荐项）决定，`mf decide <id> [--gate <key>] --default "<选了什么、为什么>"`。
    所有代决的事会单独列在看板上；用户回来时，汇报里先列这些代决事项。
13. 测试原则（用户 11-22）：用例分三类，覆盖表也按这三类写，目标是让用户一眼看清测了什么：
    - **基本功能测试**：简短、数量少，覆盖最常用的主路径（每个公开入口 / 分档 1 例）；用得最多——每次小改动第一个跑它。
      放进 `commands.correctness`（每次 `mf try` 都先跑），整套控制在 1–2 分钟内；
    - **性能测试**：bench 的 case 集（`commands.bench`），每个分档至少一个 case；
    - **其他覆盖测试**：只测前两类覆盖不了的（边界与交界、报错、逐位格式、graph、编译缓存……），每条写明前两类为什么覆盖不了；
      能覆盖的不再加，除非特殊说明（写明原因）。放进 `commands.verify_extra`（prep 与收尾时在干净 worktree 跑）；
    - 改动或删除已有用例 / bench 不需要用户批准：复核记录时用 `--tests-why` 说明为什么符合原则，mf 写进提交信息；
      违反原则时才问用户：放宽容差、删掉仍需要的覆盖、加与已有用例重复的用例。
    派实现者和复核时，把这条原样写进 prompt。
14. 入口文档必须说明基线与对照的来源：
    - 冻结基线由 mf 生成块给出；
    - 性能对照写实现、版本或提交、调用方式、各档实际选中的 kernel、同进程同协议；
    - 精度参考写参考实现与容差。
    spec 阶段就把对照来源写进 SPEC。

## 流程
| 阶段 | 做什么 | 命令 / 产出 |
|---|---|---|
| 1 spec | 读需求和代码；写 `SPEC.md`，重点写第 7 节“我的理解与假设”、第 8 节“待你回答的问题”，并写明性能对照与精度参考的来源、推送目标（URL + 分支）；用 `mf set` 填 commands（`correctness` = 基本功能测试，`verify_extra` = 其他覆盖测试，见第 13 条）/ parse / scope / metric（bench 写法见 skill `myflow-bench`）以及 `doc.entry`、`doc.paths`、`tests.paths`、`git.url`、`git.branch`（先设好再问：批准需求即同时批准推送目标，之后不再单独问） | `mf new <id> --repo R --goal G --target X [--from /opt/myflow/templates/task.example.json]` → `mf brief --ask`（mf 会附上推送目标）→ 等用户 → `mf decide --gate spec` |
| 2 freeze | 冻结基线（正确性必须先在基线通过）。**新算子**基线上还没有它：先做 bootstrap M0（见下），再以 M0 冻结 | `mf freeze <id>` |
| 3 survey（只在新的算子类别 / 没有可用经验时） | 先读经验卡 `/opt/myflow/knowledge/INDEX.md`；再按 `survey.repos` 每个 repo 派一个只读 explore 子 agent 找同类实现 | `survey.md`（≤1 页表：来源·技术·适用性·预计收益·证据）→ `mf note <id> --kind survey --file survey.md` |
| 4 plan | 估上限（roofline / Amdahl）；按“收益×把握÷成本”排序；**本轮的里程碑计划卡**；选互不冲突（改不同文件）的 lane | `mf ms plan <id> M<k> --title … --type perf --expect 1.10 --basis "…" --ceiling X --goal G1 --hours 8`（再计划下一个，作为“下一步”）；`mf lane new <id> L1 --direction "…"`；`mf ms plan … --ask`（与承诺一致时 mf 只通知，否则等用户） |
| 5 implement | 每条 lane 派一个后台子 agent，prompt 就用 `mf lane new` 打印的那句；等待期间只看 `mf status` 与提醒，不替 lane 改代码 | lane 的 `mf try` 记录 |
| 6 收尾（integrate） | 一个里程碑 = 一个主题。按 `mf try` 自测挑一条 lane；prep 的复核不通过（自测未复现、对上一里程碑无提升）就换一条。多条有效 lane 叠加：`lane new int --from M<k-1> --merge L1,L2`，集成 lane 不用 `mf try`。prep 自己补测缺的数字（两个都缺时一次三方交替测量：冻结基线 / 上一里程碑 / 候选），同一份代码和用例的功能用例只跑一次 | `mf ms prep <id> M<k> --lane L` → 派 1 个复核子 agent（不是实现者的会话），同时派下一个里程碑的实现者 → `mf ms close <id> M<k> --next M<k+1>` |
| 7 report | ≤3 行摘要；生成报告（附审计：所有数字从原始日志重算）；用 skill `myflow-learn` 沉淀 ≤3 张经验卡 | `mf note <id> --kind summary "…"` → `mf report <id>` → 用户确认后 `mf report <id> --done` |

阶段由动作自动推进：`ms plan` → 计划，`lane new` → 实现，`ms prep` → 收尾，`ms close --next report` → 报告；
收尾后再计划性能里程碑会开下一轮（R2…）。结束时 `mf report <id> --done`。汇报照常用 `mf brief`。

## 里程碑（让用户在 GitHub 上看到进展；设计见 /opt/myflow/docs/DESIGN-v2-milestones.md）
- 粒度：一句话 + ≤20 行伪代码 diff 能讲清；默认 ≤800 行非文档 / 非用例改动（超了先拆，确需时 `--size-why`）。
- 性能型必须对上一里程碑 improved（只对冻结基线“提速”的空改动会被拒）；有 case 变慢要 `--regress-why`。
- `ms close` 必须写下一步：先 `mf ms plan` 下一个里程碑（承诺），或 `--next done|report|stop|await-user`；与上次承诺不同要 `--why`。
- 推送：目标随需求一起确认（spec gate 批准时 `git.url` / `git.branch` 已设置即可），之后收尾自动推送；
  目标改了或当时没设，mf 才挂起 `push:…`，按铁律 6 问用户。
- 本里程碑改动或删除了已有用例 / bench（实现者或复核改的都算）：不挂 gate；复核记录时必须 `--tests-why` 说明为何符合测试原则（第 13 条），
  mf 把说明写进提交信息。违反原则的改动由复核交给你，你问用户。
- 同一尝试的重复复核结论不一致时 prep 会拒绝：不要再跑一次碰运气，交给用户决定（认可时 `--verify-why`）。
- 已收尾的里程碑做错了（用户指出方向 / 做法不对）：`mf ms revoke <id> M<k> --why "…"`（只限最近收尾的一个）。集成分支、tag、
  bootstrap worktree 回到父提交，旧卡片等归档，同一编号可重新计划；新的 M<k> 推送时，mf 请用户批准后用 force-with-lease 覆盖远端旧提交。
- 新算子：`mf ms bootstrap <id>`（需 spec gate 已批准；阶段进入“功能版”）→ 在 bootstrap worktree 做出功能正确版 + 用例 + bench + 文档骨架 →
  `mf ms plan <id> M0 --type feature …`（开工前要用户确认方向时加 `--ask`，gate【bootstrap】，不顶替冻结后的 plan gate）→ `mf ms prep <id> M0 --bootstrap` → 复核 → `mf ms close` → `mf freeze <id>`（默认以最新的里程碑为基线）。
- 实现者（bootstrap 子 agent 或 lane）结束时写实现报告 `<task_dir>/notes/<M 或 lane>-report.md`（≤20 行：做了什么 / 为什么 / 与计划的差异 /
  新逻辑需要哪些用例覆盖 / 已知问题）；bootstrap 期间实现者按第 13 条自己写新逻辑的用例，每次小改动先跑基本功能测试。派发时在 prompt 里写明。
- 移植 / 替换类里程碑：开工前把“上游文件 → 目标文件”的对应、目标目录里已有的同名文件和调用方、要保留的现有接口写进计划，`--ask` 请用户确认；
  不要自行按上游目录结构另建一份（试点 M0 因此重做）。
- 分析 / 维护类里程碑可以不带 lane（只改文档、用例）：`mf ms prep <id> M<k>`。

## 防卡死
- plan 阶段必须给出上限估计；上限 < 目标会自动变成“需要你确认”，先问用户是否调整目标。
- 单条 lane 连续 `max_no_improve` 次无提升会被拒绝；所有 lane 都停滞 → 向用户给出“停止 / 换方向 / 延长预算”三选一。
- 改目标、预算、范围一律用 `mf set <id> key=value --why "…"`（例如 `budget.max_attempts=80`）：这些改动会显示在看板上。
- “是否在推进”由证据判定，不看自述。进展 = try / 复核 / ms.* / lane / freeze，以及
  `mf note --kind finding|survey|plan|summary|deadend|lesson`；不带 `--kind` 的 `mf note` 只证明活着。没有进展记录时，mf 再看 worktree 的提交 / 未提交改动和任务目录文档：
  `stale` 什么都没有（可能卡死）· `spin` 还在调 mf 但 `budget.spin_min` 内没有进展、文件也没动（原地打转）·
  `no_record` 文件在改但没记录（在干活）· `await_user` 已问用户在等回复 · `no_report` 有进展没汇报。
- 派子 agent 时在 prompt 里要求：≤20 分钟一次 `mf note <id> "在做什么"`；做成一步就 `mf note <id> --kind finding "…"`（bootstrap 期间尤其如此）；
  mf 输出里出现「需要用户确认」时原样带回给你。

## 并行与资源
- lane 数 ≤ 空闲 GPU 数；`mf` 自动给计时独占 GPU、给正确性共享 GPU。
- 子 agent 长时间没有 mf 调用（看板“最后活动”变大）：用运行时的消息通道问它是否被权限 / 沙箱挡住（例如禁止写 /tmp），必要时代为提交。
- PMC 等影响全机的 profiling：`mf run <id> --lane L --machine-exclusive -- <cmd>`。
- 整模型优化：先建模型级任务（bench = 端到端吞吐/时延，`metric.direction=max` 视指标而定），按 Amdahl 排出热点后，
  每个热点 kernel 建子任务 `mf new <kid> --repo R --parent <id>`，子任务同样走本流程。

## 给用户的汇报
一行结论（复核数字 vs 目标）＋ `mf status <id>` 的路线图与方向检查 ＋ 刚完成里程碑的卡片要点 ＋ 下一步 / 需要你做的决定。看板：`mf serve`。
