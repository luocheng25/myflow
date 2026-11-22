---
name: myflow-lane
description: >-
  在独立 git worktree 中执行 myflow 优化任务的一条实现线：小步改代码、每次用 `mf try` 提交并测量、遵守守卫信号、
  到顶或被拒绝时停下并汇报。Use when a prompt says "使用 skill myflow-lane", points to a myflow lane brief
  (lanes/<lane>/brief.md), or asks to implement one optimization direction under myflow.
  Do not use to orchestrate the whole task (use myflow-kernel-opt).
---

# myflow lane（实现线）

先完整读 brief（路径在 prompt 里）：目标、方向、worktree、允许 / 禁止改的文件、可用命令和工具。

## 循环
1. 定一个小而可证伪的改动，用一句话写成 `--idea`（改了什么、为什么应该更快）。
2. 只在你的 worktree 里改 `scope.allow` 内的文件。
3. `mf try <task> --lane <lane> --idea "…"`：自动快照提交 → 基本功能测试（`commands.correctness`，不过就不跑 bench）→ 冻结的 bench → 记录。
   只想先验证正确性用 `--no-bench`；定义了快速 bench 时可用 `--quick`。
4. 读输出：状态、自测加速比、逐 case、预算，以及末尾「mf 提醒」。
5. 需要诊断时用 `mf run <task> --lane <lane> -- <cmd>`（日志进看板）；PMC 加 `--machine-exclusive`。
6. 超过 20 分钟没有 mf 调用（读代码、长时间编译）时：`mf note <task> --lane <lane> "在做什么"`。

## 必须遵守
- 不自己计时，不引用 mf 以外的数字。`mf try` 的数是“自测、未配对”；官方数字由编排者在 `mf ms prep` 时复核给出。
- 不改测试、bench、参考实现、容差（oracle）：改了会被判 `oracle_modified` 并显示给用户。
- lane 默认从最近的里程碑开始（有里程碑正在收尾时，从它的候选开始）；入口文档与用例由收尾时的复核负责，lane 不改。
- 性能不能来自本地未提交的东西（缓存、生成物、git 忽略的文件）：复核在干净 worktree 里跑，会被识破。
- 📨 用户留言：照做，然后 `mf note <task> --lane <lane> --ack <n> "怎么处理的"`。REFUSED（exit 3）：停止并汇报。
- 你不直接和用户对话：brief / note 一律带 `--lane`（不带 `--lane` 的 `mf brief` 是编排者向用户的汇报）；
  mf 输出里出现「需要用户确认【…】」时，把那段原样写进给编排者的结果里。
- 因权限 / 沙箱无法运行 mf 时：不要绕过、不要自己计时；把代码留在 worktree（或写明路径），汇报阻塞原因和需要编排者执行的命令。
- 连续失败达到上限：`git -C <wt> reset --hard <最佳尝试的 commit>` 后换思路；同一类改法（算法 / 访存 / 计算 / launch）连续 3 次无效就换类别。
- 不重试已被否定的想法：先看 `mf status <task>` 里已放弃的 lane 和死路记录。
- 发现规律或坑：`mf note <task> --lane <lane> --kind finding|deadend "…（证据 A7）" [--file 笔记]`；本线历史：`mf status <task> --lane <lane>`。
- 同一操作做第二次时，写成脚本放进 `<task_dir>/tools/`，第一行 docstring 写“用途 + 用法”。
- 需要再派子 agent（例如只读调研）时，一律用 Claude Opus 5.5、推理强度 max（runSubagent 传 `model="Claude Opus 5.5 (copilot)"`），不用默认模型。

## 结束（达标 / 方向到顶 / 被拒绝）
1. `mf lane set <task> <lane> --status done|abandoned|blocked --reason "结论 + 证据"`
2. 写实现报告 `<task_dir>/notes/<lane>-report.md`（≤20 行）：做了什么 / 为什么 / 与 brief 的差异 / 新逻辑需要哪些用例覆盖 / 已知问题。
   收尾的复核据此写入口文档和卡片。
3. 最后回复 ≤10 行：最佳尝试编号与自测加速、关键改动、停下的原因、未解决的问题。
