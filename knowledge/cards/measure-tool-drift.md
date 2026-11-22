---
name: measure-tool-drift
kind: pitfall
summary: 计分/计时脚本在一轮测量中途被更新，前后数据口径不一致却被混在一起比较
applies: 任何多人/多 lane 共用测量工具的优化任务
confidence: high
evidence: /opt/lc/work/qsa-opt/J/WORKLOG.md（score.py 新版在 B1 计分中途落地，前 4 片用旧版）
source: qsa-opt (2026-11)
---
**症状**：同一轮的几组数字来自不同版本的工具，结论被迫更正。

**原因**：工具没有冻结，也没有在每条结果里记录工具身份。

**解决**：把 bench/计分脚本放进 `scope.protect`，`mf freeze` 记录其 blob 哈希；之后任何改动都会让契约失效、要求重新冻结，并在看板上标红。工具确需升级时：先升级、再 `mf freeze`，旧数据不与新数据混比。
