# <算子>：kernel 设计（任务划分与流水线）

<!-- 入口文档第 2 节只放骨架；这里给每个核心 kernel 的完整任务划分与逐拍流水线，写给要读 / 改 kernel 的人。
     只写当前实现，不写过程（哪个里程碑、谁定的、试过什么）；关键行链接到代码；与代码对不上就改这里。
     源码模块 docstring 已按 /opt/myflow/templates/KERNEL_SOURCE.md 写全（region 表、Naming、Pipeline rules）时，
     这里链接过去，只补任务划分与资源。
     每节先给 Gluon 风格的分阶段函数（用户 11-23：只有逐拍伪代码看不懂；Triton 风格只写块级张量，写不出预取、
     LDS / VMEM 访问等流水线决策），再给任务划分与流水线。 -->

## 先读：Gluon 风格的分阶段写法
<!-- 一段说明（为何不用纯 Triton：放置 / 预取 / barrier / wave 分工由编译器定、源码里写不出）+ 一个代码块：
     维度与张量的名字 / 形状 / dtype；所用的 Gluon 操作（gl.allocate_shared_memory / .index / .load / .store、
     gl.amd.cdna3.buffer_load / buffer_store / mfma、gl.barrier，或目标架构的同类操作）及约定（如 load 的布局、操作数方向）；
     伪操作（Gluon 没有的：wave / lane 编号、readfirstlane、解码、wave 内归约……一行一个，注释写语义）；
     语义函数（量化、激活……写公式）；各路径（分档）的条件与 kernel 调用顺序。一个 program = 一个 CTA；
     张量下标代替 offsets 运算，省略 mask、布局对象与类型转换；只求看懂，不求能运行。 -->
```python
```

## <kernel 名>（[代码](<file>.py#L<n>)）
<!-- 一句话：这个 kernel 算什么、在数据流里的位置。 -->

**Gluon 风格**
<!-- ≤50 行：首行注释写 grid 与线程数；接着「放置」注释：每个操作数的路径（global → VGPR 直读，还是经 LDS
     共享 / 双缓冲、几个槽）、预取距离、累加器位置、写出路径（直写或经 LDS 转置）；函数体按序言 / 稳态 / 收尾分段：
     稳态循环里先发后面几步的 buffer_load 再算本步（即预取），LDS 的 store / load 与 barrier 写在实际位置并注明
     它保护什么，wave 分工用 wave_id()；算什么（K 累加、乘哪个 scale、写到哪里）照写。与代码顺序不同的地方要写出来。
     指令级排布（sched_group、waitcnt 计数、lane 取址）留给流水线。写完做冷读：只给文首记号与本块，
     问放置、预取、barrier 保护什么、wave 分工，答不对就改。 -->
```python
```

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
