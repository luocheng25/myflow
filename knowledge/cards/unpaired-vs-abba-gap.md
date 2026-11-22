---
name: unpaired-vs-abba-gap
kind: measurement
summary: 未配对自测（不同时间/不同卡对比冻结基线）与同卡 ABBA 复核可差约 4%，小于约 5% 的收益只能信 ABBA
applies: AMD Instinct 多卡机器，GPU kernel 计时
confidence: medium
evidence: myflow examples/torch-rmsnorm 任务 rms：A3 自测 4.295× vs V3 复核 4.463×（GPU0，ABBA 4 对）
source: myflow 搭建 (2026-11)
---
**场景**：lane 用 `mf try` 看到小幅提升（或回退），想据此做决定。

**结论**：自测只用于方向判断；收益 <5% 或接近目标线时，必须 `mf verify`（同卡、交错 ABBA、干净 worktree）再下结论；离阈值很近时加大 `--pairs`。
