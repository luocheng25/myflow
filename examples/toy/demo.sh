#!/usr/bin/env bash
# Build a demo task (CPU only, ~30 s) to see every myflow view: lanes, failures, timeout, stall,
# an overclaim caught by `mf verify`, a user steer and a pending gate.
# Usage: examples/toy/demo.sh [workdir]     then: MYFLOW_HOME=<workdir>/home mf serve   (or open the printed html)
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
W="${1:-$HOME/.myflow-demo}"  # not /tmp: some agent sandboxes forbid writing there
MF="$here/../../bin/mf"
rm -rf "$W" && mkdir -p "$W"
export MYFLOW_HOME="$W/home"
"$here/make_toy_repo.sh" "$W/repo" >/dev/null
q() { "$MF" "$@" >/dev/null 2>&1 || true; }
speed() { sed -i "s/^SPEED = .*/SPEED = $2/" "$W/home/tasks/demo/wt/$1/kernel.py"; }

q new demo --repo "$W/repo" --title "toy kernel 提速演示" --goal "toy kernel 三个 case 的 geomean 加速 ≥1.5×，正确性不变" --target 1.5
q set demo commands.correctness="python3 test_kernel.py" commands.bench="python3 bench.py" \
  'scope.allow=["kernel.py"]' 'scope.protect=["bench.py","test_kernel.py"]' gpu.pool=none budget.max_no_improve=6 --why "demo 配置"
q brief demo --ask "我理解：只改 kernel.py，bench/test 冻结；目标 1.5×。问题：是否允许改数值顺序？"
q decide demo --gate spec "确认；数值必须逐位不变" --quote "可以，但必须逐位一致"
q freeze demo --repeats 3
q phase demo survey --brief "调研 3 处同类实现：A 向量化最直接；B 分块收益小；C 本地缓存技巧可疑"
q phase demo plan --brief "按 Amdahl 排序：向量化 > 分块；上限估计 1.8×"
q hyp demo add "向量化让三个 case 都 ≥1.3×" --predict "1.4×"
q hyp demo add "分块对 large 有效" --predict "1.1×"
q hyp demo add "本地缓存能再快 30%" --predict "1.4×"
q ceiling demo 1.8 "roofline：带宽上限 / 当前带宽 ≈1.8×"
q lane new demo vec --direction "向量化（unroll + 向量 load）" --hyp H1
q lane new demo tile --direction "分块（tile）" --hyp H2
q lane new demo boost --direction "本地缓存技巧" --hyp H3
q brief demo --ask "R1：三条线并行（vec/tile/boost），每线 ≤6 次尝试"
q decide demo --gate auto "同意" --quote "开始吧"
q phase demo implement --brief "R1 开始：3 条 lane 并行"

speed vec 1.18; q try demo --lane vec --idea "unroll x2" --hyp H1
speed tile 1.04; q try demo --lane tile --idea "tile 64" --hyp H2
speed vec 1.33; q try demo --lane vec --idea "unroll x4 + 向量 load" --hyp H1
sed -i 's/2 \* x for/2 * x + 1 for/' "$W/home/tasks/demo/wt/vec/kernel.py"
q try demo --lane vec --idea "激进重排（引入 bug）"
sed -i 's/2 \* x + 1 for/2 * x for/' "$W/home/tasks/demo/wt/vec/kernel.py"
speed boost 1.10; touch "$W/home/tasks/demo/wt/boost/.boost"
q try demo --lane boost --idea "本地缓存预热" --hyp H3
speed tile 1.05; q try demo --lane tile --idea "tile 128"
sed -i 's/^HANG = .*/HANG = 30/' "$W/home/tasks/demo/wt/tile/kernel.py"
q try demo --lane tile --idea "tile 256 + 双缓冲（卡住）" --timeout 3
sed -i 's/^HANG = .*/HANG = 0/' "$W/home/tasks/demo/wt/tile/kernel.py"
speed tile 1.06; q try demo --lane tile --idea "tile 96"
speed vec 1.44; q try demo --lane vec --idea "修复重排 + 软件流水" --hyp H1
speed tile 1.05; q try demo --lane tile --idea "tile 80"
q verify demo --lane vec --pairs 3
q verify demo --lane boost --pairs 3
q hyp demo set H1 confirmed --evidence "V1：A9 复核 ≈1.44×"
q hyp demo set H3 refuted --evidence "V2：自测 ≈1.43× 复核 ≈1.10×，收益来自未提交的本地文件"
q lane set demo boost --status abandoned --reason "收益来自 git 忽略的本地文件，干净 worktree 不复现"
q note demo --lane vec --kind finding "unroll x4 后 large case 受带宽限制，继续 unroll 无益"
q steer demo --lane tile "tile 方向收益太小，考虑停掉转去做 prefetch"
q phase demo verify --brief "R1 复核：vec 1.44× 已复核；boost 被复核识破；tile 停滞"

# ---- milestone M1 (v2): the verified vec lane becomes one reviewable commit, pushed with "what's next"
git init -q --bare "$W/remote.git"
q set demo doc.entry=docs/ENTRY.md 'tests.paths=["test_kernel.py"]' git.url="$W/remote.git" git.branch=mf/demo \
  --why "里程碑：入口文档、用例路径与推送目标"
q ms plan demo M1 --title "向量化：unroll x4 + 向量 load + 软件流水" --type perf --expect 1.4 \
  --basis "roofline 上限 1.8×；向量化单独约 1.4×" --goal G1 --hours 4
q ms plan demo M2 --title "prefetch：隐藏 large case 的访存延迟" --type perf --expect 1.05 \
  --basis "unroll x4 后 large case 受带宽限制" --goal G1 --hours 4
q phase demo integrate --brief "R1 收尾 M1：vec 已复核 1.44×"
q ms prep demo M1 --lane vec
ms="$W/home/tasks/demo/milestones/M1"
int="$W/home/tasks/demo/wt/_ms"
# three independent roles, simulated here; in real use each is a separate sub-agent session
q ms role demo M1 --role tests --result pass --session demo-tests -m "覆盖项 4 个（3 个 case + 数值逐位一致），无缺失、无冗余"
python3 - "$int/docs/ENTRY.md" <<'PY'
import sys
p = sys.argv[1]
s = open(p, encoding="utf-8").read()
s = s.replace("> 手写 3 行（文档作者）：这是什么 · 为什么这样设计 · 现在最大的风险。",
              "> toy kernel：逐元素 2·x。M1 用 unroll x4 + 向量 load + 软件流水把三个 case 提到约 1.44×；\n"
              "> 设计取舍：只改 kernel.py，数值逐位不变；最大风险：large case 已受带宽限制，离 1.5× 还差约 4%。")
s = s.replace("|---|---|---|---|\n\ndispatch", "|---|---|---|---|\n| `kernel.run(xs)` | list → list | 无 | 逐元素 2·x |\n\ndispatch")
s = s.replace("for t in range(steps):      # 阶段 A：…    阶段 B：…\n    ...",
              "for i in range(0, n, 4):    # 阶段 A：向量 load x[i:i+4]    阶段 B：上一组的 2·x 写回\n    ...")
open(p, "w", encoding="utf-8").write(s)
PY
cat > "$ms/card.md" <<'MD'
### M1 · 向量化：unroll x4 + 向量 load + 软件流水 · perf
**What**: kernel.run 改为每次处理 4 个元素，load 与上一组的写回重叠。
**Why / principle**: 原来逐元素访存、指令发射受限 → 向量 load 减少访存指令，流水让 load 与计算重叠；数值逐位一致（同样的 2·x）。
```diff
- for x in xs: out.append(2 * x)
+ for i in range(0, n, 4): v = load4(xs, i); store4(out, i, 2 * v)
```
**Interface / behavior**: 无变化。
**Tests**: 用例不变；覆盖项 4/4。
**Perf (by mf)**: 见提交信息 Perf 行（对冻结基线 ≈1.44×）。
**Next**: M2 prefetch，预期再 +5%（large case 带宽受限，需隐藏延迟）。
MD
q ms role demo M1 --role writer --result done --session demo-writer --card "$ms/card.md" -m "入口文档 §0、§3.2、§3.5 与卡片"
cat > "$ms/qa.md" <<'MD'
Q1 目标 1.5×，现在 1.44×（§0、§1）✓ · Q2 接口 kernel.run(xs) 未变（§3.2）✓ · Q5 每 4 个元素一组、load 与写回重叠（§3.5）✓
Q6 原理：向量 load + 流水；数值逐位一致（卡片）✓ · Q10 下一步 M2 prefetch，预期 +5%（卡片）✓ · 其余 ✓。闭卷 6 分钟。
MD
q ms role demo M1 --role verifier --result pass --session demo-reader --qa "$ms/qa.md" --minutes 6 -m "10/10，无疑点"
q ms close demo M1 --next M2
key=$(python3 -c "import sys; sys.path.insert(0, '$here/../..'); from myflow.core import State, task_dir_of
print(next((g['key'] for g in State(task_dir_of('demo')).pending_gates() if g['key'].startswith('push:')), ''))")
[ -n "$key" ] && q decide demo --gate "$key" "可以推到这个地址" --quote "可以"
q ms push demo

q phase demo plan --brief "R2 规划中：M2 prefetch；停掉 tile"
q brief demo --ask "R2：vec 已达 1.44×（M1 已推送），差目标 4%；建议按 M2 新开 prefetch 线并停掉 tile，可以吗？"
"$MF" render demo >/dev/null
echo "demo ready:"
echo "  MYFLOW_HOME=$MYFLOW_HOME $MF status"
echo "  MYFLOW_HOME=$MYFLOW_HOME $MF ms show demo      # 路线图 + 方向检查"
echo "  MYFLOW_HOME=$MYFLOW_HOME $MF serve        # http://127.0.0.1:8765/"
echo "  static: $MYFLOW_HOME/tasks/demo/dashboard.html"
echo "  pushed milestones (stand-in for GitHub): git --git-dir $W/remote.git log --oneline mf/demo"
