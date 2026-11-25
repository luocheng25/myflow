---
name: vscode-exthost-oom-kills-orchestrator
kind: pitfall
summary: VS Code Copilot Chat 里连跑数天的编排会话会把远程扩展宿主撑到约 4 GB 堆而 OOM，进行中的请求连同子 agent 静默消失、不会续跑；按轮开新会话、限并行子 agent，用 mf revive 让 Copilot CLI 接手
applies: VS Code Remote / Dev Containers 中的 Copilot Chat agent 模式编排 myflow 任务（多子 agent、上万次工具调用）
confidence: high
evidence: task moe-ptpc-a8w4-r2；~/.vscode-server/data/logs/20261108T090548/remoteagent.log 11-23 03:46、17:55，11-24 04:18、16:29 四次 heap out of memory → SIGABRT；会话记录 41 MB、5.4 万条事件
source: moe-ptpc-a8w4-r2，2026-11-25
notes: absolute-ok
---
**症状**：编排停在一次工具调用上（会话记录里该调用“已请求、未开始”或“已开始、未完成”），之后数小时没有任何事件；
没有完成、也没有在等用户。外部作业（mf run、nohup）照常跑完，看板只显示“最后活动 N 小时前”。

**原因**：
- 扩展宿主进程 V8 堆到约 4 GB 时 `FATAL ERROR: ... JavaScript heap out of memory`，进程 SIGABRT；
  Copilot Chat 的 agent 请求与所有 runSubagent 都在这个进程里，一起被杀。VS Code 随即起新扩展宿主，但不重放进行中的请求。
- 本任务 4 次崩溃间隔 3–18 小时；两次发生在无人值守时，分别空转 7.1 / 6.4 小时，直到用户回来发“继续”。
  其中一次正有 5 个子 agent 并行。重启后的新扩展宿主 9.5 小时 RSS 又到 2.5 GB。
- 调大堆不容易：这个 build 的 server 在启动扩展宿主前删掉环境里的 NODE_OPTIONS（`removeDangerousEnvVariables`），
  扩展宿主只继承 server 进程自己的 execArgv，所以 devcontainer 的 remoteEnv / NODE_OPTIONS 无效。

**解决**：
- 一轮（或一个里程碑）开一个新会话，交接靠磁盘状态（STATUS.md、events.jsonl、会话笔记），不要让一个会话跑几天。
- 并行子 agent 控制在 2–3 个；工具输出写文件只读摘要；异步终端用完就关。
- 会话内的 agent 已经不存在，只能由外部发现并接手：`mf revive start <任务>` 起看门狗，任务未完成、不在等用户且长时间无活动
  （扩展宿主崩溃后 10 分钟）时用 Copilot CLI（`copilot -p`，每任务固定会话）接手编排；回 VS Code 继续前先 `mf revive stop --kill`。

**怎么验证**：`grep -n "heap out of memory\|Extension Host Process exited" ~/.vscode-server/data/logs/*/remoteagent.log`；
`ps -o pid,etime,rss -p $(pgrep -d, -f 'type=extensionHost')` 看各扩展宿主的运行时长与内存（多个窗口各有一个）。

<!-- 规则：数字用相对值（×、%、占峰值%）；≤40 行；同主题先 mf kb search，更新旧卡而不是新建。 -->
