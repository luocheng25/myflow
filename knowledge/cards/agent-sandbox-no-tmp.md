---
name: agent-sandbox-no-tmp
kind: pitfall
summary: 本机的 agent 运行时禁止写 /tmp；MYFLOW_HOME、worktree、TMPDIR、JIT 缓存放在 /tmp 会让子 agent 完全无法工作
applies: Copilot CLI / VS Code 子 agent 沙箱，本机（MI308X 节点）
confidence: high
evidence: myflow 试跑 rms2/fuse（子 agent 无法调用 mf）；/opt/lc/work/qsa-opt/GOAL.md §0（“本环境不允许写 /tmp”）
source: myflow 搭建 (2026-11)；qsa-opt (2026-11)
---
**症状**：子 agent 迟迟没有任何 mf 调用，看板显示“无活动”；它最后汇报“安全规定禁止写 /tmp”。

**原因**：运行时策略禁止写 /tmp，而任务数据、worktree 或临时目录被放在了 /tmp。

**解决**：MYFLOW_HOME 用默认 `~/.myflow`；task.json 的 env 里把 `TMPDIR`、各类 JIT 缓存指向 `{lane_dir}/tmp`、`{lane_dir}/cache`；多用户共享锁目录也不要选 /tmp。
