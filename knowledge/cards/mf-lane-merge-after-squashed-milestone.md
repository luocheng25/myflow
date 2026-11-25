---
name: mf-lane-merge-after-squashed-milestone
kind: pitfall
summary: 集成 lane 从 lane 原始提交出发时 prep 拒绝；改 --from M<k> --merge 因里程碑是单提交、合并基点退回更早而大量冲突：按文件取边解决，再重跑同一条 lane new 才登记
applies: myflow（mf lane new / mf ms prep），收尾与下一条集成 lane 流水并行时
confidence: high
evidence: task moe-ptpc-a8w4-r2 int9 A111 → prep M17 REFUSED；int10（6d25c00）冲突解法；/opt/myflow/myflow/cli.py cmd_lane_new
source: moe-ptpc-a8w4-r2，2026-11-24
---
**症状**：`mf ms prep M<k+1> --lane int9` 报 “A111 既不是从 M<k>（收尾提交）出发的，也不是从它收尾时的候选出发的”。
int9 是从 L13 的原始提交（候选 = 它再合并上一收尾）建的。改用 `mf lane new int10 --from M<k> --merge int9`，
src 与 tests 都冲突。

**原因**：
- `_ms` 分支每个里程碑一个提交（不是 git merge），收尾提交与 lane 提交没有祖先关系，合并基点退回 M<k-2>；
  L13 的改动在两边各一份，复核又在收尾提交上改了用例和文档，于是重叠处全部冲突。
- `mf lane new` 在合并失败时抛错退出，lane 还没登记；提示里的“解决后继续 mf try”会报 unknown lane。

**解决**：
- 先确认两边相对真正共同代码（lane 原始提交）各改了哪些文件：`git diff --stat <lane 原始> <int 头>` 与 `git diff --stat <lane 原始> <收尾>`；
  两组文件不相交时，冲突文件逐个取边（`git checkout --theirs/--ours`），再用 `git diff --cached <int 头> -- src/` 与
  `git diff --cached <收尾> -- <其余>` 为空来核对，提交合并。
- 再执行一次完全相同的 `mf lane new ...`：worktree 已存在会复用，合并“已是最新”，这次才登记 lane。
- 预防：集成 lane 等上一里程碑收尾后再从 `--from M<k>` 建；或在收尾前就从候选建（mf 认候选）。

**怎么验证**：`git log -1 --format=%P` 两个父提交分别是收尾提交与 int 头；对收尾提交的 diff 只剩 int 的增量文件。

<!-- 规则：数字用相对值（×、%、占峰值%）；≤40 行；同主题先 mf kb search，更新旧卡而不是新建。 -->
