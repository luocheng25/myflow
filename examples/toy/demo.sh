#!/usr/bin/env bash
# Build a demo task (CPU only, ~1 min) the way a real task runs: requirements + push target asked once, baseline,
# survey, plan, three parallel lanes (a failure, a timeout, a stall, an overclaim caught by the verify in prep),
# milestone M1 reviewed, committed and pushed, then an R2 plan that leaves the promise and is asked.
# Usage: examples/toy/demo.sh [workdir]     then: MYFLOW_HOME=<workdir>/home mf serve   (or open the printed html)
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
W="${1:-$HOME/.myflow-demo}"  # not /tmp: some agent sandboxes forbid writing there
MF="$here/../../bin/mf"
rm -rf "$W" && mkdir -p "$W"
export MYFLOW_HOME="$W/home"
"$here/make_toy_repo.sh" "$W/repo" >/dev/null
git init -q --bare "$W/remote.git"
q() { "$MF" "$@" >/dev/null 2>&1 || { echo "[demo] ✖ mf $*" >&2; exit 1; }; }
refused() { if "$MF" "$@" >/dev/null 2>&1; then echo "[demo] ✖ expected a refusal: mf $*" >&2; exit 1; fi; }
speed() { sed -i "s/^SPEED = .*/SPEED = $2/" "$W/home/tasks/demo/wt/$1/kernel.py"; }

# ---- requirements: one question, push target included
q new demo --repo "$W/repo" --title "toy kernel 提速演示" --goal "toy kernel 三个 case 的 geomean 加速 ≥1.5×，正确性不变" --target 1.5
q set demo commands.correctness="python3 test_kernel.py" commands.bench="python3 bench.py" \
  'scope.allow=["kernel.py"]' 'scope.protect=["bench.py","test_kernel.py"]' gpu.pool=none budget.max_no_improve=6 \
  doc.entry=docs/ENTRY.md 'tests.paths=["test_kernel.py"]' git.url="$W/remote.git" git.branch=mf/demo --why "demo 配置"
q brief demo --ask "我理解：只改 kernel.py，bench/test 冻结；目标 1.5×。问题：是否允许改数值顺序？"
q decide demo --gate spec "确认；数值必须逐位不变；推送目标同意" --quote "可以，但必须逐位一致"
q freeze demo --repeats 3

# ---- survey and the R1 plan (asked: nothing was promised yet)
q note demo --kind survey "调研 3 处同类实现：A 向量化最直接；B 分块收益小；C 本地缓存技巧可疑"
q ms plan demo M1 --title "向量化：unroll x4 + 向量 load + 软件流水" --type perf --expect 1.4 \
  --basis "roofline：带宽上限 / 当前带宽 ≈1.8×；向量化单独约 1.4×" --ceiling 1.8 --goal G1 --hours 4
q ms plan demo M2 --title "prefetch：隐藏 large case 的访存延迟" --type perf --expect 1.05 \
  --basis "向量化后 large case 预计受带宽限制" --goal G1 --hours 4 --tentative
q ms plan demo M1 --ask
q decide demo --gate auto "同意：三条线并行（vec/tile/boost），每线 ≤6 次尝试" --quote "开始吧"

# ---- implementers: three lanes in parallel
q lane new demo vec --direction "向量化（unroll + 向量 load）：预期三个 case 都 ≥1.3×"
q lane new demo tile --direction "分块（tile）：预期 large case +10%"
q lane new demo boost --direction "本地缓存技巧：预期再快 30%"
speed vec 1.18; q try demo --lane vec --idea "unroll x2"
speed tile 1.04; q try demo --lane tile --idea "tile 64"
speed vec 1.33; q try demo --lane vec --idea "unroll x4 + 向量 load"
sed -i 's/2 \* x for/2 * x + 1 for/' "$W/home/tasks/demo/wt/vec/kernel.py"
q try demo --lane vec --idea "激进重排（引入 bug）"
sed -i 's/2 \* x + 1 for/2 * x for/' "$W/home/tasks/demo/wt/vec/kernel.py"
speed boost 1.01; touch "$W/home/tasks/demo/wt/boost/.boost"  # git-ignored, never committed
q try demo --lane boost --idea "本地缓存预热"
speed tile 1.05; q try demo --lane tile --idea "tile 128"
sed -i 's/^HANG = .*/HANG = 30/' "$W/home/tasks/demo/wt/tile/kernel.py"
q try demo --lane tile --idea "tile 256 + 双缓冲（卡住）" --timeout 3
sed -i 's/^HANG = .*/HANG = 0/' "$W/home/tasks/demo/wt/tile/kernel.py"
speed tile 1.06; q try demo --lane tile --idea "tile 96"
speed vec 1.44; q try demo --lane vec --idea "修复重排 + 软件流水"
speed tile 1.05; q try demo --lane tile --idea "tile 80"
q note demo --lane vec --kind finding "unroll x4 后 large case 受带宽限制，继续 unroll 无益（A9）"
q steer demo --lane tile "tile 方向收益太小，考虑停掉转去做 prefetch"

# ---- milestone M1: prep re-measures the candidate (fresh seed, clean worktree, ABBA on one GPU)
# boost looked suspicious (survey: C); the re-measurement prep runs refuses it:
refused ms prep demo M1 --lane boost   # self-test ≈1.31×, clean worktree ≈1.01×: the gain came from a local file
q lane set demo boost --status abandoned --reason "收益来自 git 忽略的本地文件，干净 worktree 不复现"
q ms prep demo M1 --lane vec
ms="$W/home/tasks/demo/milestones/M1"
int="$W/home/tasks/demo/wt/_ms"
# the closing review, simulated here; in real use it is one independent sub-agent (skill myflow-review)
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
**Tests**: 用例不变；覆盖 3 个 case + 数值逐位一致，无缺失。
**Perf (by mf)**: 见提交信息 Perf 行（对冻结基线 ≈1.44×）。
**Next**: M2 prefetch，预期再 +5%（large case 带宽受限，需隐藏延迟）。
MD
q ms review demo M1 --session demo-review --card "$ms/card.md" -m "入口文档 §0、§3.2、§3.5 与卡片；用例无缺失"
q ms close demo M1 --next M2   # pushed right away: the target was approved with the requirements
q brief demo "M1 已推送：vec 复核 ≈1.44×；boost 的收益被复核识破（本地文件）；tile 停滞。下一步 M2 prefetch"

# ---- R2: the plan leaves what M1 promised, so it is asked (a plan that follows the promise is only shown)
q ms plan demo M2 --title "prefetch，并停掉 tile" --why "tile 到顶（+4~6%），把它的预算转给 prefetch"
q ms plan demo M2 --ask
q status demo
echo "demo ready:"
echo "  MYFLOW_HOME=$MYFLOW_HOME $MF status            # 所有任务 + GPU"
echo "  MYFLOW_HOME=$MYFLOW_HOME $MF status demo       # 路线图、方向检查、提醒"
echo "  MYFLOW_HOME=$MYFLOW_HOME $MF status demo M1    # 里程碑：计划、数字、复核、提交信息"
echo "  MYFLOW_HOME=$MYFLOW_HOME $MF serve             # http://127.0.0.1:8765/"
echo "  static: $MYFLOW_HOME/tasks/demo/dashboard.html"
