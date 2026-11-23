# 流水线 kernel 源码写法（docstring、命名、流水线代码）

<!-- 适用于多 region、LDS 多 buffer、异步 DMA 的流水线 kernel（GEMM、MoE、attention 等）。
     范例：aiter gfx9_fp8 gemm1_2x2.py 第 4–89 行
     https://github.com/luocheng25/aiter/blob/f1a38237422cf904087e929733d109e569394087/aiter/ops/flydsl/kernels/moe_gemm_2stage/gfx9_fp8/gemm1_2x2.py#L4-L89
     docstring 与注释沿用仓库语言（pyhip / aiter 为英文）。 -->

## 1. 模块 docstring：按这个顺序写
1. 一句话：架构、wave 数、tile、算子与场景（如 "gfx950 four-wave 256x256 FP8 Gate/Up for MoE prefill."）。
2. 来源：从哪里移植、与原实现的差别。
3. **Tile**：一个 CTA 负责的数据块，怎样切成子块（操作数名字在这里首次出现），每个 wave 负责什么，
   累加器放在哪、每 lane 多少个，MFMA 形状与操作数角色，变体（如半 tile）少了什么，epilogue 做什么。
4. **Schedule**：grid 或持久化，CTA → tile 的映射（XCD 交织、L2 共享），特殊顺序。
5. **Stream-K / split-K**（有才写）：怎么切，谁合并，标志位怎么复位。
6. **K step**：每个操作数在 LDS 里几个 buffer，一个 K step 分几个 region，每个 region 一行：

       region  MFMA              LDS -> VGPR   DMA -> LDS
       0       C_tl += B_l A_t   A_b(k)        B_l(k+2)
       1       C_bl += B_l A_b   B_r(k)        A_t(k+2)

   每个变体一张表；再写 job 之间怎么衔接（如最后一个 K pair 预取下一个 job 的前两个 K tile）。
7. **Naming**：维度约定加本 kernel 的符号表（第 2 节）。
8. **Pipeline rules, to keep when editing the kernel body**：编号的不变量（第 4 节）。

## 2. 命名
- 维度：M = 行（如排序后的路由），N = 输出列，K = 规约维。名字里的 m / n / k 只表示方向
  （`m_block`、`n_tile`、`k_pairs`、`wave_m`）；`n_` 前缀永远指 N 方向、不表示“数量”，数量用 `num_`（`num_ctas`）。
- 操作数：global / LDS 里的块用大写加位置后缀（`A_t` / `A_b` 上下半，`B_l` / `B_r` 左右半）；
  它在 VGPR 里的 fragment 用同名小写（`a_t`、`b_l`），下一个 K tile 的 fragment 加数字（`a_t1`）；
  累加器按象限命名（`C.tl` … `C.br`）。
- K 循环：`k` 是 K tile 序号，放在 `buf = k % 2`，`next_buf` 放 k+1，本 region 的 DMA 加载 `k2 = k + 2`；
  一个 job 的 K 范围 `[kp0, kp1)`，split 时 `w0 / w1` 是本 CTA 的份额。
- 坐标与下标：`(ni, mi)` 是象限内的 fragment；`i` 是 job；`j` 是一组 DMA 里的第几个。
- 搬运函数 `<动作>_<源>2<目的>`，g = global、s = LDS、r = VGPR：`load_g2s`（region 内穿插的 DMA）、
  `fill_g2s`（单独发，用于 prologue）、`read_s2r`（LDS 读到 VGPR）。
- 注释缩写：`DMA` = LDS DMA，`LR` = LDS read，`X(k)` = 操作数 X 的第 k 个 K tile。
- 后缀 `0` 表示 job 0 / prologue 里的量（`tile0`、`k0`）。

## 3. 流水线代码
- 每个 region 写成 `with region(buf, r, dma=…, read=…):  # C_tl += B_l(k) A_t(k)`，行尾注释是这个 region 的 MFMA。
- 块内顺序固定为 DMA → LR → MFMA，每条搬运语句行尾注明搬哪个 tile，与 docstring 的 region 表逐行一致：
  `B_l.load_g2s(src, buf, k2)  # DMA B_l(k+2)`、`a_b = A_b.read_s2r(buf)  # LR A_b(k)`。
- `region()` 进入时等在途 DMA 降到 `in_flight` 并同步 CTA（如 `wait_asyncmark` + `lgkmcnt(0)` + `s_barrier` +
  `sched_barrier`）；退出时用 `sched_group_barrier` 把 DMA 和 LR 按“每条 MFMA 一个、DMA 在前”穿插进 MFMA。
- 阶段用横幅分开，横幅里写本阶段做什么：`# ======== Prologue: … ========`、Main loop、Last K pair、Epilogue、Stream-K。
- 计数类常量写推导、不写魔数：如 `in_flight = 2 * groups - 3`，注释说明为什么（读的那组之后又发了几组）。
- 构建函数按阶段拆成返回 `Bundle` 的小函数（context → schedule → global_io → operands → exchange →
  epilogue → cursor），kernel 正文只编排；各操作数统一提供 `fill_g2s / load_g2s / read_s2r`；逻辑记录用 `NamedTuple`。

## 4. 流水线不变量（docstring 里编号列出，每条写清由什么保证）
1. region 内发射顺序（DMA → LR → MFMA）及原因：编译器让 LDS 读排在可能与之别名的 LDS DMA 写之后。
2. DMA 组按固定顺序发、每组一个 asyncmark；region 开头允许的在途组数 `in_flight` 及其推导。
   移动任何一个 DMA 或读，这个数都会变。
3. LDS 先读后写（WAR）：DMA 最早在该 buffer 最后一次被读之后的那个 region 重填，
   由那个 region 开头的 `lgkmcnt(0)` + `s_barrier` 保证所有 wave 已读完。
4. VGPR 先写后读（RAW）：region r 读出的 fragment 从 r+1 起才喂给 MFMA；
   读只能在 fragment 的最后一条 MFMA 之后覆盖它（来不及就双缓冲）。
5. 预取距离（如 2）；job 的最后一个 K pair 改为加载下一个 job 的前几个 K tile，下一个 job 开始时操作数已就绪。
6. epilogue 的访存位置：读放在最后一个 K pair 之前、由它的 MFMA 掩盖，写放在之后。
7. 收尾：最后一个 job 之后先完成合并（stream-K 等），再 `wait_asyncmark(0)` / `vmcnt(0)` 排空 DMA。

## 5. 改流水线时
- 移动 DMA 或读、改预取距离或 buffer 数：同一次提交更新 docstring 的 region 表、`in_flight` 推导和受影响的规则。
- 新增变体（半 tile、新形状）：docstring 加一张该变体的 region 表，命名表补上新符号。
