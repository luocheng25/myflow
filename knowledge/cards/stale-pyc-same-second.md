---
name: stale-pyc-same-second
kind: pitfall
summary: 同一秒内修改 .py 且文件长度不变，Python 会继续用旧 .pyc，测到的是旧代码
applies: 所有 Python 前端的 kernel（FlyDSL、Triton、torch），快速迭代的自动化循环
confidence: high
evidence: myflow selftest test_stale_pycache_is_not_measured（SPEED 1.3→1.6 被测成 1.30×）
source: myflow 搭建 (2026-11)
---
**症状**：改了代码，数字完全没变（或与预期严重不符）。

**原因**：.pyc 失效判断基于源文件 mtime(秒) + size；agent 用脚本快速编辑时两者都可能不变。

**解决**：myflow 每次运行前清理 worktree 内的 `__pycache__` 并设 `PYTHONDONTWRITEBYTECODE=1`（已内置）；自己写的循环脚本也要这么做。
