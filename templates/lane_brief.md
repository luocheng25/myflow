# Lane brief：{{task}} / {{lane}}

你是实现线 **{{lane}}**（skill：myflow-lane）。只在你的 worktree 里改代码，所有尝试用 `mf try` 提交。

- 任务目标：{{goal}}（目标 {{target}}×，相对冻结基线）
- 本线方向：{{direction}}
- 假设：{{hyp}}
- worktree：`{{wt}}`（只改这里；不要碰主仓库和其他 lane）
- 允许改：{{allow}}
- 禁止改（oracle，改了会被判 oracle_modified）：{{protect}}
- 背景（先读）：`{{spec}}`、`{{survey}}`、`{{plan}}`、经验卡索引 `{{kb}}`

## 命令（`{{mf}}`）
```bash
{{mf}} try {{task}} --lane {{lane}} --idea "<一句话：改了什么、为什么>" [--hyp H1] [--quick | --no-bench]
{{mf}} show {{task}} --lane {{lane}}               # 本线历史
{{mf}} show {{task}} --attempt N                   # 某次尝试的逐 case 数据
{{mf}} note {{task}} --lane {{lane}} --kind finding "<发现>" [--file <详细笔记>]
{{mf}} hyp {{task}} set H1 confirmed|refuted --evidence "A7: ..."
{{mf}} lane set {{task}} {{lane}} --status done|abandoned|blocked --reason "<结论与证据>"
{{mf}} run {{task}} --lane {{lane}} -- <诊断/profiling 命令>   # PMC 等影响全机的加 --machine-exclusive
{{mf}} ping {{task}} --lane {{lane}} "<在做什么>"            # 超过 20 分钟没有其他 mf 调用时
```
mf 会自动执行（不要自己另写计时）：正确性 `{{correctness}}`，bench `{{bench}}`，快速 bench `{{bench_quick}}`。

## 规则
1. 每次 `mf try` 末尾的「mf 提醒」必须先处理：用户留言 → 照做并 `mf ack`；REFUSED → 停下汇报。
2. 数字只引用 `mf try` 的输出；它是"自测、未配对"，正式数字由编排者 `mf verify` 产生。
3. 连续失败 {{max_fail_streak}} 次：回到本线最佳提交重来；同一类改法连续 3 次无效：换类别。
4. 不要重复已被否定的想法（看 `mf show {{task}}` 的假设台账和死路）。
5. 重复做第二次的事写成脚本放 `{{task_dir}}/tools/`（第一行 docstring 写用途+用法）。
6. 结束时（达标 / 本方向到顶 / 预算到）：`mf lane set ... --reason`，最后回复 ≤10 行：最佳 A 编号、自测加速、关键改动、未解决问题。

## 已有工具
{{tools}}
