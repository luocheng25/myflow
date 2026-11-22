# myflow 设计说明

> v2（以里程碑为中心的可视化：路线图、里程碑卡、方向检查、三个收尾角色、推送到 GitHub）见 [DESIGN-v2-milestones.md](./DESIGN-v2-milestones.md)；
> 本文是 v1 的方案与取舍，仍然有效。

## 0. 出发点
来自两个现场：
- 你的 qsa-opt（`/opt/lc/work/qsa-opt`）：GOAL 冻结 → 分析线 A1–A3 → 实现线 B1–B4 → J/J2 独立复核 → 停止条件。流程本身是对的，
  但所有状态都写在散文里（WORKLOG/REPORT/SCOREBOARD 合计约 1.9 MB，单个 WORKLOG 达 130 KB），人很难一眼看出“现在怎样、
  哪里不对”；“自报需更正”“计分工具中途换版本”“PMC 污染他人计时”都是事后才发现。
- GEAK kernel_workflow：COMMANDMENT（冻结命令）+ 不可变 oracle + verify engineer 在干净工作区复测 + 预算/`min_improve`/
  连续无提升上限 + insight/hypothesis 台账 + 生成索引的经验卡 + GPU 锁。机制完整但很重（JS 工作流 + 十几个角色 + 大量 prompt）。

myflow 的取舍：**把 GEAK 的“可信执行”机制压缩成一个 stdlib CLI，把编排留给 agent，用守卫和可视化兜底**。

## 1. 原则
1. **生成，不手写**：agent 只写结构化事件和 ≤600 字的 brief；状态页、看板、报告全部由事件计算生成——不会膨胀，也不会和数据不一致。
2. **异常优先**：L0 只显示健康度、首要预警和“需要你做什么”；全绿就没什么要读。
3. **逐层展开**：L0 → L1 → L2 → L3，每一层都链接到下一层的证据，最底层是原始日志。
4. **两个数字永远分开标注**：自测（lane 自己的、未配对）与复核（官方：干净 worktree + 同卡 ABBA），图上用红线画出差距。
5. **先对齐再执行**：需求和每轮计划是 gate；agent 先复述理解、列出假设和问题。
6. **脚本做确定的事**：计时、解析、比较、判定、记录都在 `mf` 里；agent 只做判断。
7. **便宜地看**：终端、Markdown 预览、静态 HTML、本地网页，同一份数据。

## 2. 问题 1：可视化与早发现（核心）

### 2.1 走偏通常发生在哪里，用什么拦
| 时间点 | 典型走偏 | 拦截手段 |
|---|---|---|
| 开始 15 分钟 | 需求理解错、指标/case 不对、范围不清 | **spec gate**：SPEC.md 第 7 节“我的理解与假设”、第 8 节“待你回答的问题”；未确认就离开 spec → crit；确认后 SPEC 被改 → warn |
| 每次换阶段 | 闷头做、方向选错 | `mf brief`（阶段随动作自动推进）：做完了什么 / 下一步 / 风险，三行出现在看板顶部；**plan gate** 每轮一次 |
| 规划时 | 目标物理上达不到 | `mf ms plan --ceiling`：上限估计 < 目标 → “需要你确认” |
| 实现中 | 原地打转、反复失败、卡死、虚报 | 按 lane 的无提升计数、连败计数、超时、心跳过期、自测未复现、改 oracle/越界、阶段停留过久 |
| 目标被挪动 | 悄悄降低目标或放宽范围 | `mf set` 记录 old→new（谁、为什么）；绕过 `mf` 直接改 task.json → warn；改度量契约 → crit 并要求重新冻结 |
| 任意时刻 | 你想纠偏 | **留言**（看板或 `mf steer`）：agent 下一次调用任何 mf 命令必看到，需 `mf note --ack` 回执；10 分钟未回执 → warn |

### 2.2 各层内容
- **L0（所有任务一屏）**：健康点、阶段（含轮次）、最佳·复核、最佳·自测、目标与进度条、尝试/预算、活跃 lane、最后活动（超过心跳阈值变黄/红）、需要你处理的条数、首要预警。父子任务缩进显示（整模型 → 热点 kernel）。
- **L1（一个任务）**：阶段条；“需要你处理”框（可直接批准/写决定）；6 个 KPI（基线与噪声、最佳复核、最佳自测、目标进度、上限估计、预算与最停滞 lane）；**性能轨迹图**（x=尝试序号，按 lane 着色；○ 自测、◆ 复核、× 失败；目标/上限/基线虚线；轮次分隔线；虚线为“可信最佳轨迹”，有复核时取复核值）；预警；brief 时间线；lanes 表；假设台账；决策与契约变更；笔记。
- **L2（一次尝试）**：想法与假设、提交与 diff 统计、正确性/bench 的返回码、耗时、GPU、门禁前后读数、逐 case 数字、所有复核记录。
- **L3（原始）**：每次运行的 `out.log`、`diff.patch`、`abba.json`、`mf_parsed.json`、`events.jsonl`。

### 2.3 为什么这样做（考虑过的替代）
- 自由文本 WORKLOG：信息全但不可扫读，且 agent 的叙述可能与数据不一致 → 只保留 ≤600 字 brief，其余结构化。
- MLflow / W&B / Grafana：适合指标，不懂 gate、oracle、假设、留言；要部署服务 → 不满足“轻量、任何工程可用”。
- 只用 agent 自带的 todo/plan：看不到跨 lane、跨会话、跨任务的全局，也没有可信数字。
- 静态 HTML 的缺点是“agent 不动就不刷新”：页面用 JS 按时间戳实时更新“多久没动”，`mf serve` 每次请求都重新计算。

## 3. 问题 5：防谎报（威胁 → 机制）
| 威胁 | 机制 | 位置 |
|---|---|---|
| 报告里写一个没测过的数 | 看板/报告只显示 `mf` 产生的数；报告附审计（`mf report` 从原始日志重算） | render / report |
| 改测试、bench、容差 | `scope.protect` 的 blob 哈希在 freeze 时冻结；改动、删除、新增同名文件 → `oracle_modified` | try / verify |
| 改范围外文件 | `scope.allow`；只提交范围内文件 | try 快照 |
| 在错的代码树上测 | 每次尝试自动快照提交，记录 commit；复核按 commit 建干净 worktree | try / verify |
| 本地未提交产物、旧 .pyc 造成虚高 | 复核在干净 worktree；每次运行前清 `__pycache__`、`PYTHONDONTWRITEBYTECODE=1` | verify / runner |
| 换卡、换时段、挑好数 | 复核：同一张卡上冻结基线与候选交错 ABBA，取配对比值中位数；全部原始数据保留；同一尝试被复核 ≥3 次 → warn | verify / guard |
| 噪声当收益 | 判定阈值 = max(min_improve, 2×噪声)；逐 case 回退单列 | verify |
| 基线被换掉 | 加速比分母永远是冻结基线（GEAK 的反作弊不变量） | freeze |
| 中途换测量工具/命令 | 度量契约哈希；改了即 crit，必须重新冻结 | guard |
| 别人在用这张卡 | 空闲门禁（前后各一次；遥测未知不算空闲）；可加 `commands.gate`（如 PTL/时钟） | runner |
| 事后改记录 | 事件哈希链 + 末行哈希；`mf report` 的审计从原始日志重新解析、重算所有数 | core / audit |

威胁模型：防的是“粗心、偷懒、自我欺骗式”的错误数字（这是实际发生过的）；不防一个刻意伪造全套日志的对手。

## 4. 问题 6：防卡死（由内到外）
1. 单条命令：`timeout_s`，超时杀**整个进程组**；`mf` 自己被杀时也会带走子进程（不会留下占卡的孤儿进程）。
2. 单条 lane：连续失败 `max_fail_streak` → 提醒回到最佳版本；连续 `max_no_improve` 次完整尝试无 ≥`min_improve` 提升 → 该 lane 的 `mf try` 被拒绝。
3. 整个任务：尝试次数、时长预算用尽 → 拒绝；所有活跃 lane 都停滞 → crit，交给你决定（停 / 换方向 / `mf set budget.*` 延长）。
4. 活性：`heartbeat_min` 内无任何事件 → warn，3 倍 → crit（agent 可能已卡死或退出）；运行记录长期无结束 → warn。
5. 可达性：plan 阶段的上限估计低于目标 → 立刻问你，而不是跑完预算才发现。

## 5. 问题 4：并行
- lane = `mf/<task>/<lane>` 分支 + 独立 worktree + 独立 `{lane_dir}`（缓存、TMPDIR）；同一 lane 同时只允许一个 try/verify（文件锁，冲突直接拒绝）。
- 复核：每条 lane 独立的干净 worktree；基线 worktree 只读共享。
- GPU 租约（flock，进程死了锁自动释放，不会残留）：计时 = 独占一张卡；正确性 = 共享；PMC 等 = 全机独占（等待正在进行的计时，阻止新计时）。
- 多任务：每个任务独立目录与事件日志，全局 `registry.json` 汇总到 L0；多用户共享一台机器时设 `MYFLOW_LOCK_DIR` 为公共目录。
- 派发子 agent 由你用的 agent 运行时负责（Copilot / Claude Code 的后台子 agent 或多个终端），myflow 只提供 brief、隔离与记录，因此不绑定任何运行时。

## 6. 问题 7：流程与来源
| 阶段 | 来源 | myflow 的落点 |
|---|---|---|
| spec | 你的 GOAL.md 冻结规则；长任务 harness 的“先写清验收标准” | SPEC.md 模板 + gate |
| freeze | GEAK COMMANDMENT / oracle freezer | `mf freeze`：基线提交、oracle 哈希、基线多次测量与噪声 |
| survey | 你的阶段 A（A2：在 aiter 里找可借鉴思路）；GEAK researcher | 每个 repo 一个只读 explore 子 agent；先查经验卡 |
| plan | GEAK tech_lead（正交方向、预算）；e2e 的 Amdahl 排序 | `ceiling` + 假设台账 + 互不冲突的 lane |
| implement | GEAK specialist engineers；你的 B1–B4 | lane brief + `mf try` |
| verify | GEAK verify_engineer；你的 J/J2 | `mf ms prep` 里的复核（脚本，不是另一个 agent，避免“复核者也会说错”） |
| integrate | GEAK integrator | `lane new int --merge` + 复核 |
| report / learn | GEAK update_experience、learned cards + 生成索引 | `mf report` + 经验卡（相对数字、症状→原因→解决） |

与 GEAK 的主要差异：不把编排写死在 JS 里（agent 可以按任务灵活调整），但关键约束由脚本强制（gate、预算、拒绝、复核）；没有十几个角色，只有编排者与 lane 两种 skill。

## 7. 问题 3、8：扩展与轻量
- **skill**：4 个，各一件事、正文 20–40 行；具体技术知识放经验卡，不塞进 skill。
- **经验卡**：一卡一结论，frontmatter 可检索，`INDEX.md` 由 `mf kb check` 生成；lint 限制长度、查重复、提醒绝对数字。
- **工具**：重复第二次的操作写成脚本放 `<task>/tools/`，第一行 docstring 即索引；通用的提升到 `/opt/myflow/tools/`。
- **整模型优化**：父任务度量端到端指标，热点 kernel 作为 `--parent` 子任务，L0 树状显示。
- **新的守卫**：在 `myflow/guard.py` 的 `alerts()` 里加一条规则即可，所有视图和 agent 提醒自动出现。
- **轻量**：约 3.4k 行标准库 Python（其中一半是视图与命令行）；无服务、无数据库；数据是 JSON/JSONL/Markdown，可直接 grep。

## 8. 已知局限
- GPU 索引假设 rocm-smi 的 card 序号 = HIP 序号（MI300 节点通常成立）；不成立时设 `gpu.pool` 并用 `commands.gate` 自定义检查。
- 空闲门禁只在每次运行前后采样，看不到运行中途的短暂干扰；靠复核的交错配对来稀释。
- 无法强杀一个“卡在思考里”的 agent：只能通过心跳告警让你发现，并通过拒绝新尝试阻止它继续消耗预算。
- 留言（steer）只在 agent 下一次调用 `mf` 时被看到；agent 长时间不调用 mf 时（L0 的“最后活动”会暴露），
  由编排者再通过运行时的消息通道转达（例如后台子 agent 的追加消息）。实测：测试时把 MYFLOW_HOME 放在 /tmp，子 agent 的运行时禁止写 /tmp，
  它约 16 分钟无法调用 mf，看板只显示“无活动”、留言未被看到；编排者用运行时消息联系后才拿到它的 kernel（复核 7.79×）。
- 自测（未配对）会有百分之几的偏差（示例里约 4%），所以小收益只看复核。
