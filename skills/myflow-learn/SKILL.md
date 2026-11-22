---
name: myflow-learn
description: >-
  把完成的 myflow 任务中可复用的经验沉淀成短小的经验卡（`mf config` 所示 kb 目录下的 cards/），并保持生成的索引最新；
  调研前查阅已有经验。Use at the end of a myflow task (report phase), when the user says 沉淀经验 / 总结教训 /
  记录这个坑 / update the knowledge base, or before a survey to look up prior lessons for a kernel class.
---

# 经验沉淀

## 写什么
- 只写会改变下次决策的东西：有效技巧（technique）、坑（pitfall）、死路（dead-end）、测量规律（measurement）。
- 每个任务最多 3 张；没有值得写的就不写。

## 步骤
1. 查重：`mf kb search <关键词…>`。已有同主题的卡就**更新旧卡**（补证据、调整置信度），不要新建。
2. 新建：`mf kb new <name> --kind technique|pitfall|dead-end|measurement`，按模板填写。
3. 规则：
   - `summary` 一句话、能直接拿来做判断；正文 ≤40 行。
   - 数字用相对值（×、%、占峰值 %）；确实需要绝对值时在 frontmatter 加 `notes: absolute-ok`。
   - pitfall / dead-end 写成“症状 → 原因 → 解决”；technique 写“场景 → 做法 → 怎么验证”。
   - `evidence` 写任务名与 A/V 编号或文件路径；`applies` 写清 arch、DSL、kernel 类型、dtype。
4. `mf kb check`（检查卡片并重建 `INDEX.md`；它是生成文件，不要手改）。

## 读
- 调研前先 `mf kb search <词>` 或读 kb 目录下的 INDEX.md，按语义挑卡片再打开。
- 卡片是先验，不是结论：与本机测量冲突时以测量为准，并回头更新卡片。
