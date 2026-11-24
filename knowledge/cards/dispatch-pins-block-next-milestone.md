---
name: dispatch-pins-block-next-milestone
kind: pitfall
summary: 复核把当前分派表的确切交界钉进受保护的测试文件，而并行 lane 正在改分派表时，下一个里程碑的 prep 一定挂在 verify_extra 上（lane 不能改用例）；收尾前先拿复核后的用例在下一个候选上预检，只钉两版都成立的交界
applies: myflow 任务（单一受保护测试文件、scope.protect 按整文件哈希），带按形状 / M 分派表的算子（GEMM、MoE、attention 分档），流水线并行的多里程碑
confidence: high
evidence: task gemm-a8w4：M3 prep 两次失败（M1/M2 复核加的 7 个分派用例写死旧交界，int3 改用例被判 oracle_modified）→ 插入维护里程碑 M8 去耦；M3 复核钉的 tp4_qkvz 512|513 在 M7 候选上挂（runs/20261124-161527-_task-run-53ea）→ 改为两侧钉，M7 复核再钉 1024|1025
source: gemm-a8w4 (2026-11)
---
**症状**：上一个里程碑刚收尾，下一个里程碑 prep 时 verify_extra 挂在“M=x 应走 kernel A”这类用例上。lane 或集成 lane 去改用例会被判 oracle_modified（受保护文件按整文件哈希比对），只有收尾复核能改用例。

**原因**：复核按当时的代码钉死了分派表的确切交界（以及引用的私有常量）；而流水线里下一个候选早就调了这张表（交界移动、档位改走别的 kernel）。

**解决**：
- 分派用例只核对“实际 launch == 公开的 kernel_names 预测”，再按 kernel 名前缀判档位，不比对完整核名和私有常量；
- 收尾前，用复核后的测试文件在下一个候选的代码上跑一遍：建 scratch worktree，拷入测试文件。哪一行挂了，就只钉两版都成立的两侧，确切交界留给移动它的那个里程碑复核再钉；
- 已经卡住：插入不带 lane 的维护里程碑，由复核去耦用例，并在两版代码上都跑通，然后从该里程碑重建集成 lane；
- lane 起点要能回溯到最新的已收尾里程碑，否则 mf 用旧里程碑的测试文件当 oracle，合并新测试文件就会被误判。

**怎么验证**：`git worktree add --detach <scratch> <next-candidate>`；把复核后的测试文件拷进去；用 `pytest -m "not basic"` 跑，应全部通过。
