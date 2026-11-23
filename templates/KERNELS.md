# <算子>：kernel 设计（任务划分与流水线）

<!-- 入口文档第 2 节只放骨架；这里给每个核心 kernel 的完整任务划分与逐拍流水线，写给要读 / 改 kernel 的人。
     只写当前实现，不写过程（哪个里程碑、谁定的、试过什么）；关键行链接到代码；与代码对不上就改这里。
     源码模块 docstring 已按 /opt/myflow/templates/KERNEL_SOURCE.md 写全（region 表、Naming、Pipeline rules）时，
     这里链接过去，只补任务划分与资源。 -->

## <kernel 名>（[代码](<file>.py#L<n>)）
<!-- 一句话：这个 kernel 算什么、在数据流里的位置。 -->

**任务划分**
| 层级 | 形状 / 数量 | 负责的数据 |
|---|---|---|
| grid | | |
| CTA（线程数） | | |
| wave | | |
| lane | | |
| K 步 | | |

**流水线**（逐拍，≤40 行；标出预取距离、LDS 读写与 barrier）
<!-- 多 region 的 K 循环用表：region | MFMA | LDS → VGPR | DMA → LDS，每个变体一张；再写序言、job 衔接与收尾。 -->
```text
```

**命名**
<!-- 维度约定（M / N / K；名字里的 m / n / k 只表示方向，n_ 不表示数量）与本 kernel 的符号表：
     操作数块 / VGPR fragment / buffer / K 范围 / 搬运函数（_g2s、_s2r）/ 注释缩写（DMA、LR、X(k)）。 -->

**流水线不变量**（改 kernel 时必须保持；编号，每条写清由什么保证）
<!-- region 内发射顺序；DMA 组顺序与 in_flight 推导；LDS 先读后写；VGPR 先写后读；预取距离与 job 衔接；
     epilogue 访存位置；收尾排空。 -->
1.

**资源**：LDS 用量、寄存器 / 占用（知道就写）。
