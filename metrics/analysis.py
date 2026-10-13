"""度量分析：基线对比、拐点定位、趋势、flaky 识别。

这是整个平台的"判断"部分。把它单独成模块的原因很实际：
**判断的口径比采集更容易出错**，而口径错了没人看得出来。
`DEGRADATION_TOLERANCE`（当前 10%）、`KNEE_RPS_PER_USER_DROP`（20%）
这些数字必须是代码里的一行，而不是某个人脑子里的印象 —— 而且它们
是**量出来的**，不是拍的。

## 容忍带从哪来

`DEGRADATION_TOLERANCE` / `RPS_DEGRADATION_TOLERANCE` 是**默认值**，
真正的值可以由 `gate_rules.yaml` 的 `perf.tolerance` 覆盖 —— 见
`tolerance_from_rules()`。之所以两个地方都留，是因为：

* `gate_rules.yaml` 的头注释承诺"阈值集中在这一处，改标准改这里"，
  而容忍带是**最需要常改**的那个数（换机器就要重量）；
* 但纯逻辑用例（`tests/test_platform.py`）不应该为了测一个比较函数
  去读文件系统，所以模块常量必须能独立成立。

约定：**运行时以 YAML 为准，模块常量只是它的后备**。两者不一致时
（YAML 改了、常量没改），以 YAML 为准，这正是"集中在一处"的含义。
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median

# --------------------------------------------------------------------------- #
# 口径常量：改这里就是改全平台的门禁标准
# --------------------------------------------------------------------------- #
# 10% 以内算噪声，不判劣化。**这是默认值，运行时由 gate_rules.yaml 覆盖。**
#
# **这个数字是量出来的，不是拍的。** 最初写 3%（"经验下界"），
# 实测立刻证明太紧：同一份代码、同一配置连跑两轮，
# P95 从 105.9ms 变到 110.2ms（+4.1%），门禁判「拦住」——
# 一次没有任何代码变慢的假阳性。
#
# 用 `platform_cli noise --rounds 5` 在本机实测 5 轮：
#   各轮 P95 = [128.5, 127.5, 137.9, 130.8, 132.2] ms
#   极差 8.16%，单轮最大偏离均值 4.95%
# 取实测最大偏离的约 2 倍作余量 → 10%。
#
# 教训：**容忍带取决于机器和环境，不能凭常识填。**
# 换机器/换容器后应当重跑 `noise` 重新定，而不是沿用这里的值。
DEGRADATION_TOLERANCE = 0.10
RPS_DEGRADATION_TOLERANCE = 0.10

# 浮点比较的容差。
#
# `100.0 * (1 + 0.10)` 在 IEEE 754 下是 `110.00000000000001`，
# 于是 `(110.00000000000001 - 100) / 100` 得到 `0.10000000000000009` —— 
# **比容忍带大了一点点**。结果就是"正好踩在边界上"的值被判成劣化，
# 和 docstring 里写的「边界取不大于」自相矛盾。
#
# 单独看这像是理论问题，但它会稳定地惩罚恰好踩线的运行：
# 而"刚好涨了 10.0%"正是最容易被反复触发的那个点。
_EPS = 1e-9
KNEE_RPS_PER_USER_DROP = 0.20  # 每用户吞吐相对峰值掉 20% 即认为过拐点
KNEE_ERROR_RATE = 0.01         # 错误率超过 1% 也算拐点
FLAKY_MIN_RUNS = 6             # 少于 6 次执行不给 flaky 结论（样本太小）
FLAKY_STABILITY_MAX = 0.85     # 稳定度（多数派占比）低于 85% 判 flaky


# --------------------------------------------------------------------------- #
# 基线劣化
# --------------------------------------------------------------------------- #
@dataclass
class Delta:
    metric: str
    baseline: float
    current: float
    delta_pct: float
    tolerance_pct: float
    degraded: bool
    improved: bool

    def describe(self) -> str:
        arrow = "→"
        state = "持平"
        if self.degraded:
            state = "劣化"
        elif self.improved:
            state = "改善"
        return (
            f"{self.metric}: {self.baseline:.3f} {arrow} {self.current:.3f} "
            f"({self.pct_text()}) {state}"
        )

    def pct_text(self) -> str:
        """百分比的**给人看**的格式。

        基线是 0 时，涨幅在数学上是无穷大（0 → 0.4 错误率），
        直接 `%` 格式化会输出 `+inf%` —— 技术正确，但对着一行
        「error_rate +inf%」没人知道下一步该做什么。
        错误率这类指标从 0 涨到任何值，说清楚"从无到有"比给出
        一个无穷大符号有用得多。
        """
        if self.baseline == 0:
            if self.current == 0:
                return "持平"
            return f"从 0 变为 {self.current:.3f}"
        return f"{self.delta_pct:+.1%}"


def compare_metric(
    metric: str, baseline: float, current: float, tolerance: float = DEGRADATION_TOLERANCE
) -> Delta:
    if baseline in (0, None) or baseline == 0:
        pct = 0.0 if current == 0 else float("inf")
    else:
        pct = (current - baseline) / baseline
    return Delta(
        metric=metric,
        baseline=baseline,
        current=current,
        delta_pct=pct,
        tolerance_pct=tolerance,
        # `+ _EPS`：边界取「不大于」。少了这个 eps，
        # 恰好踩线的值会因浮点表示误差被判成劣化（见 _EPS 的注释）。
        degraded=pct > tolerance + _EPS,
        improved=pct < -tolerance - _EPS,
    )


def tolerance_from_rules(rules: dict | None) -> tuple[float, float]:
    """从 `gate_rules.yaml` 读出容忍带，返回 `(延迟容忍带, RPS 容忍带)`。

    缺项时回落到模块常量。这不是"两个真相"：YAML 是唯一的**配置**，
    模块常量是"没给配置时"的默认。之所以允许缺项，是因为门禁规则文件
    可能被裁剪过（比如只保留 probe 一节），而容忍带不该让整个判定崩掉。
    """
    tol = ((rules or {}).get("perf") or {}).get("tolerance") or {}
    d = tol.get("degradation_pct")
    r = tol.get("rps_degradation_pct")
    return (
        DEGRADATION_TOLERANCE if d is None else float(d),
        RPS_DEGRADATION_TOLERANCE if r is None else float(r),
    )


# 两轮配置差多少就认为"不是同一套"？
# 5% 不是统计推导出来的，是一条工程线：items 从 5000 变成 5100 是同一个实验，
# 从 5254 变成 900 完全是另一个实验。真正的判据是"被测对象变了没有"，
# report_mode / scenario 一律要求**精确相等**（见 check_comparability）。
COMPARABILITY_TOLERANCE = 0.05


@dataclass
class Comparability:
    """两轮（本轮 vs 基线）配置的可比性判定。

    三种结局，缺一不可，因为"没法判"和"判过、没问题"在报告里必须长得不一样
    —— 这正是 `RuleResult.skipped` 存在的理由（见 metrics/gate.py）。
    """

    verdict: str  # "ok" | "incomparable" | "unknown"
    reasons: list[str]

    def describe(self) -> str:
        if self.verdict == "ok":
            return "两轮配置一致，劣化百分比可比。"
        head = ("两轮配置不可比" if self.verdict == "incomparable"
                else "基线没有留下配置快照，无法判定可比性")
        return f"{head}：{'；'.join(self.reasons)}"


def _rel_diff(a: float, b: float) -> float:
    base = max(abs(a), abs(b))
    return 0.0 if base == 0 else abs(a - b) / base


def check_comparability(current_config: dict | None, baseline_config: dict | None) -> Comparability:
    """判断本轮与基线是不是"同一套实验"。

    比的是**被测配置**，不是指标值：items（数据量）、并发梯度（step_users）、
    report_mode、scenario。实测出现过 baseline `items=5254 / users=20` 与
    current `items=900 / users=80` 比出"p95 +40.0% 劣化"—— 那个百分比没有意义，
    因为两轮量的根本不是一回事。

    `current_config` 为 None 表示调用方没提供配置（老调用路径/单元测试构造的
    极小 baseline）：此时**不做校验**，返回 "ok"，由调用方保持原有行为。
    """
    if current_config is None:
        return Comparability("ok", [])
    if not current_config:
        return Comparability("unknown", ["本轮没有留下配置快照（老数据），无从比对"])
    if not baseline_config:
        return Comparability("unknown", ["基线建立时未记录 items / 并发等配置"])

    reasons: list[str] = []
    verdict = "ok"

    for key, label in (("report_mode", "报表实现"), ("scenario", "场景")):
        b, c = baseline_config.get(key), current_config.get(key)
        if b is not None and c is not None and b != c:
            verdict = "incomparable"
            reasons.append(f"{label} 从 `{b}` 变成 `{c}`")

    b_items, c_items = baseline_config.get("items"), current_config.get("items")
    if b_items is not None and c_items is not None and _rel_diff(float(b_items), float(c_items)) > COMPARABILITY_TOLERANCE:
        verdict = "incomparable"
        reasons.append(f"数据量 items 从 {b_items} 变成 {c_items}")

    b_users = baseline_config.get("step_users") or []
    c_users = current_config.get("step_users") or []
    if b_users and c_users:
        b_max, c_max = max(b_users), max(c_users)
        if _rel_diff(float(b_max), float(c_max)) > COMPARABILITY_TOLERANCE:
            verdict = "incomparable"
            reasons.append(f"最高并发从 {b_max} 变成 {c_max}")
        elif list(b_users) != list(c_users):
            # 最高并发相同但梯度不同：量级可比，只是密度不同 —— 记录一句，不判不可比。
            reasons.append(f"并发梯度不同（{b_users} → {c_users}），最高并发一致")

    if verdict == "incomparable":
        return Comparability("incomparable", reasons)
    return Comparability("ok", reasons)


def compare_to_baseline(current: dict, baseline: dict, rules: dict | None = None) -> list[Delta]:
    """P95/P99/错误率看"涨了没"，RPS 看"掉了没"（方向相反，不能一个公式套）。

    `rules` 传入时用其中的 `perf.tolerance` 覆盖容忍带 —— 门禁规则文件里
    写的那个数才是生效的，模块常量只是后备（见 `tolerance_from_rules`）。
    """
    tol_delay, tol_rps = tolerance_from_rules(rules)
    out: list[Delta] = []
    for metric in ("p95_ms", "p99_ms", "mean_ms", "max_ms"):
        if metric in baseline and metric in current:
            out.append(compare_metric(metric, float(baseline[metric]), float(current[metric]),
                                      tol_delay))
    for metric in ("error_rate",):
        if metric in baseline and metric in current:
            out.append(compare_metric(metric, float(baseline[metric]), float(current[metric]), 0.0))
    if "rps" in baseline and "rps" in current:
        b, c = float(baseline["rps"]), float(current["rps"])
        pct = 0.0 if b == 0 else (c - b) / b
        out.append(
            Delta(
                metric="rps",
                baseline=b,
                current=c,
                delta_pct=pct,
                tolerance_pct=tol_rps,
                degraded=pct < -tol_rps - _EPS,  # RPS 掉就是坏
                improved=pct > tol_rps + _EPS,
            )
        )
    return out


# --------------------------------------------------------------------------- #
# 拐点定位
# --------------------------------------------------------------------------- #
def find_knee(steps: list[dict]) -> dict | None:
    """在一个并发梯度序列里找拐点。

    `steps` 需要按并发数升序，每项至少含 users / rps / p95_ms / error_rate。

    判据（两个，命中任一即算）：
      1. 每用户吞吐相对峰值掉 20% —— 这是"加人不再换来产能"的定义；
      2. 错误率超过 1% —— 这是"系统开始拒客"的定义。

    为什么用 `rps / users` 而不是 `rps`：RPS 通常会一直小幅上涨，
    永远不"掉"，看不出饱和。而**每用户吞吐**是先平后塌的，
    它的拐点就是系统的容量边界。
    """
    if len(steps) < 2:
        return None
    peak_rps_per_user = 0.0
    for s in steps:
        rps_per_user = (s.get("rps", 0) / s["users"]) if s.get("users") else 0.0
        peak_rps_per_user = max(peak_rps_per_user, rps_per_user)
    if peak_rps_per_user <= 0:
        return None

    for s in steps:
        rps_per_user = (s.get("rps", 0) / s["users"]) if s.get("users") else 0.0
        drop = (peak_rps_per_user - rps_per_user) / peak_rps_per_user
        over_err = (s.get("error_rate") or 0.0) > KNEE_ERROR_RATE
        if drop >= KNEE_RPS_PER_USER_DROP or over_err:
            return {
                "users": s["users"],
                "reason": "每用户吞吐较峰值下降 %.1f%%" % (drop * 100)
                if drop >= KNEE_RPS_PER_USER_DROP
                else "错误率 %.2f%% 超过 %.0f%%" % ((s.get("error_rate") or 0) * 100, KNEE_ERROR_RATE * 100),
                "peak_rps_per_user": round(peak_rps_per_user, 4),
                "rps_per_user": round(rps_per_user, 4),
                "p95_ms": s.get("p95_ms"),
                "p99_ms": s.get("p99_ms"),
                "rps": s.get("rps"),
                "error_rate": s.get("error_rate"),
            }
    return None


def last_stable_step(steps: list[dict]) -> dict | None:
    """拐点之前最后一个"健康"的梯度 —— 报告里真正有业务含义的数字。"""
    knee = find_knee(steps)
    if knee is None:
        return steps[-1] if steps else None
    for s in steps:
        if s["users"] == knee["users"]:
            idx = steps.index(s)
            return steps[idx - 1] if idx > 0 else None
    return None


# --------------------------------------------------------------------------- #
# flaky 识别
# --------------------------------------------------------------------------- #
@dataclass
class FlakyVerdict:
    nodeid: str
    runs: int
    passed: int
    failed: int
    stability: float
    flaky: bool
    median_duration_s: float
    note: str

    def describe(self) -> str:
        return (
            f"{self.nodeid}: {self.passed}P/{self.failed}F (稳定度 {self.stability:.0%}) "
            f"{'← flaky' if self.flaky else ''}"
        )


def analyze_flaky(history: list[dict], nodeid: str = "") -> FlakyVerdict:
    """一条用例的历史执行 → flaky 判定。

    判定用"稳定度"（多数派占比）而不是"失败次数"：
    一个跑 200 次失败 3 次的用例比跑 8 次失败 3 次的用例健康得多，
    只看失败次数会把前者一起判成 flaky，把噪声当信号。
    """
    runs = len(history)
    passed = sum(1 for h in history if h["outcome"] == "passed")
    failed = sum(1 for h in history if h["outcome"] in ("failed", "error"))
    skipped = runs - passed - failed
    durations = [h["duration_s"] for h in history if h.get("duration_s")]

    if runs < FLAKY_MIN_RUNS:
        return FlakyVerdict(
            nodeid=nodeid,
            runs=runs,
            passed=passed,
            failed=failed,
            stability=round(passed / runs, 4) if runs else 0.0,
            flaky=False,
            median_duration_s=round(median(durations), 3) if durations else 0.0,
            note=f"样本不足（{runs} < {FLAKY_MIN_RUNS}），不下结论",
        )

    stability = max(passed, failed) / runs
    has_both = passed > 0 and failed > 0
    flaky = has_both and stability < FLAKY_STABILITY_MAX
    note = ""
    if not has_both:
        note = "只有单一结果，稳定"
    elif flaky:
        note = f"结果在通过/失败之间摇摆（稳定度 {stability:.0%} < {FLAKY_STABILITY_MAX:.0%}）"
    else:
        note = f"偶发失败但多数派稳定（{stability:.0%}）"
    if skipped:
        note += f"；另有 {skipped} 次跳过"
    return FlakyVerdict(
        nodeid=nodeid,
        runs=runs,
        passed=passed,
        failed=failed,
        stability=round(stability, 4),
        flaky=flaky,
        median_duration_s=round(median(durations), 3) if durations else 0.0,
        note=note,
    )


# --------------------------------------------------------------------------- #
# 整轮汇总（多个梯度 → 一份总账）
# --------------------------------------------------------------------------- #
def merge_step_rows(rows: list[dict]) -> dict:
    """从入库（或刚跑完）的步骤行重建整轮汇总。

    ## 尾比分位数必须成对来自同一梯度

    `p95_ms` / `p99_ms` **不是**各自取各梯度的最大值，而是
    「P99/P95 比值最大的那一级」的**成对**取值。

    这条是踩出来的。门禁 R5 判的是 `P99/P95` 这个**比值**，
    而"各自取 max"在比值上不是保守方向，是**系统性掩盖**：

        梯度1  P95=50   P99=300  → 该级真实比值 6.00（应当拦住）
        梯度2  P95=200  P99=210  → 该级真实比值 1.05
        各自取 max → P95=200 P99=300 → 比值 1.50 → R5 判 passed

    两个分位数来自两个不同的梯度，比值就不再对应任何一次真实测量：
    它把"某一级有长尾"和"另一级整体偏慢"拼成了一个从未发生过的组合，
    结果是把 6.00 的抖动稀释成 1.50 —— 漏报。

    取"比值最大的那一级"让这两个数重新落在同一量纲上，而且仍是保守方向：
    给出的是真实发生过的**最坏尾比**。
    比值相同（浮点容差内）时取 `p95_ms` 更大的一级，
    保持原先"取最差一级"的语义。

    原始样本没有入库（入库的是汇总），所以无法重算真实分位数 ——
    这一点没变；变的只是**在比值类判定上怎么取才真的保守**。

    `p50_ms` / `p90_ms` / `max_ms` 仍各自取最大值：它们是独立指标，
    不参与任何比值判定，"取最差一级"对它们就是保守方向。

    ## 这个函数是整轮汇总的唯一出口

    `run`、`gate`、看板、`probe` 全都必须走它。**这条纪律是踩出来的：**

    曾经 `run` 手里还有原始样本，于是用 `bench.engine.merge_stats`
    把所有梯度的样本**池化**后重算分位数；而 `gate` 拿不到样本，
    只能取最差一级。同一个 run 于是有两个不同的 P95：

        各梯度 p95 = [83.8, 128.9]
        池化（merge_stats）  → 105.9 ms
        取最差一级（本函数） → 128.9 ms

    **报告上的数字和门禁据以拦人的数字对不上。**
    它不会报错、不会变红，只会在某天有人问
    "报告说没超阈值，为什么这条流水线红了"的时候，没人能解释。

    看板里也曾经有**第三份**复制粘贴的实现，并且把 RPS 写成了
    `sum(r["rps"])`（各梯度相加，凭空翻倍）。所以现在只有一个实现，
    其余地方一律 import —— 复制粘贴是这类不一致的温床。

    ## RPS 是"总请求数 / 总时长"

    不是各梯度相加。相加会把两梯度各约 370 RPS 的同一轮汇总成 740。
    这个错误能活很久，是因为门禁只在 RPS **下跌**时判劣化，
    而"凭空翻倍"方向朝上，正好躲过检查。
    """
    total = sum(r["total"] for r in rows)
    duration = sum(r.get("duration_s") or 0.0 for r in rows)
    weighted_mean = (sum(r["mean_ms"] * r["total"] for r in rows) / total) if total else 0.0
    if duration > 0:
        rps = total / duration
    else:
        # 没有时长信息（老数据）时退化为均值，至少不凭空翻倍。
        rps = (sum(r["rps"] for r in rows) / len(rows)) if rows else 0.0

    # 尾比 p95/p99 取"比值最大的那一级"的成对值 —— 见 docstring：
    # 各自取 max 会拼出一个从未发生过的组合，把真实的长尾稀释掉。
    # 比值打平时依次比较 p95_ms、p99_ms，保持"取最差一级"的语义。
    worst_tail = max(
        ((r["p99_ms"] / r["p95_ms"]) if r["p95_ms"] > 0 else 0.0, r["p95_ms"], r["p99_ms"])
        for r in rows
    ) if rows else None
    p95_worst = worst_tail[1] if worst_tail else 0
    p99_worst = worst_tail[2] if worst_tail else 0

    return {
        "total": total,
        "duration_s": duration,
        "error_rate": (sum(r["error_count"] for r in rows) / total) if total else 0.0,
        "rps": rps,
        "mean_ms": weighted_mean,
        # 最差一级代表"这轮最坏到什么程度"，也是交给门禁的那个数。
        "p50_ms": max((r["p50_ms"] for r in rows), default=0),
        "p90_ms": max((r["p90_ms"] for r in rows), default=0),
        "p95_ms": p95_worst,
        "p99_ms": p99_worst,
        "max_ms": max((r["max_ms"] for r in rows), default=0),
        # 实际在途取最大：它回答的是"压测机有没有成为瓶颈"，
        # 取整轮中并发真正打满的那一级才有意义。
        "max_inflight": max((r.get("max_inflight") or 0 for r in rows), default=0),
    }


# --------------------------------------------------------------------------- #
# 趋势
# --------------------------------------------------------------------------- #
def trend_series(rows: list[dict], metric: str) -> list[tuple[str, float]]:
    """把入库的行整理成 (时间, 值) 序列，用于画趋势。"""
    out = []
    for r in rows:
        if r.get(metric) is None:
            continue
        out.append((r.get("started_at", ""), float(r[metric])))
    return out


def moving_direction(series: list[float], window: int = 3) -> str:
    """最近 window 个点相对更早的点是升还是降 —— 看板上一眼能看懂的方向标。"""
    if len(series) < window + 1:
        return "样本不足"
    recent = sum(series[-window:]) / window
    earlier = sum(series[-2 * window : -window]) / window
    if earlier == 0:
        return "持平"
    delta = (recent - earlier) / earlier
    if delta > DEGRADATION_TOLERANCE:
        return f"上升 {delta:+.1%}"
    if delta < -DEGRADATION_TOLERANCE:
        return f"下降 {delta:+.1%}"
    return "持平"


# --------------------------------------------------------------------------- #
# 报告文本
# --------------------------------------------------------------------------- #
def render_perf_report(run_id: str, label: str, steps: list[dict], deltas: list[Delta],
                       knee: dict | None, platform_info: dict, config: dict,
                       comparability: "Comparability | None" = None) -> str:
    """生成一页纸的压测报告（Markdown）。

    这份文本是给人看的，也是给面试官看的：**它必须能自己解释自己**
    —— 环境是什么、口径是什么、结论的边界在哪，都在同一页里。

    `comparability` 传入且判定非 "ok" 时，「与基线对比」整段换成"无法判定可比性"：
    照样把差异列出来供参考，但**不写劣化判定** —— 一段没有判定的话，
    比一段看起来像判定的错话安全。
    """
    lines: list[str] = []
    lines.append(f"# 压测报告 · {label}")
    lines.append("")
    lines.append(f"- run_id: `{run_id}`")
    lines.append(f"- 平台: Python {platform_info.get('python', '?')} / {platform_info.get('platform', '?')}"
                 f" / CPU {platform_info.get('cpu_count', '?')} 核")
    lines.append(f"- 被测服务: FastAPI {platform_info.get('fastapi', '?')} + uvicorn {platform_info.get('uvicorn', '?')}"
                 f" + SQLite(WAL)，数据量 {config.get('items', '?')} 条物料")
    lines.append(f"- 报表实现: `{config.get('report_mode', '?')}`"
                 f"（agg = 单条 SQL 聚合；n1 = 逐供应商查询，N+1）")
    lines.append("")
    lines.append("## 各梯度结果")
    lines.append("")
    lines.append("| 并发 | 时长(s) | 请求数 | 错误率 | RPS | 每用户吞吐 | P50 | P95 | P99 | Max | 实际在途 |")
    lines.append("|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for s in steps:
        rpu = (s.get("rps", 0) / s["users"]) if s.get("users") else 0
        lines.append(
            f"| {s['users']} | {s.get('duration_s', 0):.1f} | {s.get('total', 0)} |"
            f" {s.get('error_rate', 0):.2%} | {s.get('rps', 0):.1f} | {rpu:.3f} |"
            f" {s.get('p50_ms', 0):.1f} | {s.get('p95_ms', 0):.1f} | {s.get('p99_ms', 0):.1f} |"
            f" {s.get('max_ms', 0):.1f} | {s.get('max_inflight', 0)} |"
        )
    lines.append("")
    if knee:
        lines.append("## 拐点")
        lines.append("")
        lines.append(
            f"**并发 {knee['users']} 时出现拐点**：{knee['reason']}。 "
            f"该点 P95 = {knee.get('p95_ms')} ms，RPS = {knee.get('rps')}，"
            f"错误率 = {(knee.get('error_rate') or 0):.2%}。"
        )
        lines.append("")
        lines.append(f"峰值每用户吞吐 {knee['peak_rps_per_user']} req/s·用户 "
                     f"→ 拐点处 {knee['rps_per_user']}。")
        stable = last_stable_step(steps)
        if stable:
            lines.append("")
            lines.append(
                f"**可用的最后一级**是并发 {stable['users']}："
                f"P95 {stable.get('p95_ms')} ms、RPS {stable.get('rps')}、"
                f"错误率 {(stable.get('error_rate') or 0):.2%}。容量的结论取这一级，不取拐点。"
            )
    else:
        lines.append("## 拐点")
        lines.append("")
        lines.append(
            "在本次测到的并发范围内**没有出现拐点**（每用户吞吐未见 20% 以上下降、错误率未超 1%）。"
            "注意这不等于「系统没上限」，只等于「上限在这条曲线之外」——要下容量结论必须继续加压。"
        )
    lines.append("")
    incomparable = comparability is not None and comparability.verdict != "ok"
    if incomparable:
        lines.append("## 与基线对比")
        lines.append("")
        lines.append(f"**本轮的劣化百分比不可用** —— {comparability.describe()}。")
        if deltas:
            lines.append("")
            lines.append("下列差异**仅供参考，不构成劣化判定**（两轮测的不是同一套配置）：")
            for d in deltas:
                mark = "🔴" if d.degraded else ("🟢" if d.improved else "⚪")
                lines.append(f"- {mark} {d.describe()}")
        lines.append("")
        lines.append("**判定：无法判定可比性。**")
        lines.append("")
        lines.append("要拿到可比的结论，二选一：用与基线相同的配置复跑本轮，"
                     "或在当前配置上用 `--set-baseline` 重建基线。")
    elif deltas:
        lines.append("## 与基线对比")
        lines.append("")
        for d in deltas:
            mark = "🔴" if d.degraded else ("🟢" if d.improved else "⚪")
            lines.append(f"- {mark} {d.describe()}")
        lines.append("")
        worst = [d for d in deltas if d.degraded]
        if worst:
            lines.append(f"**判定：相对基线劣化 {len(worst)} 项。**")
        else:
            lines.append("**判定：相对基线无劣化。**")
    else:
        lines.append("## 与基线对比")
        lines.append("")
        lines.append("未设置基线（首次运行请用 `--set-baseline` 显式设定）。")
    lines.append("")
    lines.append("## 结论的边界（必须连同数字一起说）")
    lines.append("")
    lines.append("- **压测机与被测服务在同一台机器上**，两者抢同一份 CPU 与内存。"
                 "真实部署里这是两批机器，所以本报告的绝对性能数字**偏保守**，"
                 "有意义的是**趋势与拐点形状**（同一口径下的相对变化）。")
    lines.append("- 单机自研引擎，没有分布式加压能力。并发上限受压测机线程数与 GIL 影响，"
                 "`实际在途` 明显低于配置并发时，瓶颈在压测机而不是被测服务。")
    lines.append("- SQLite(WAL) 是单文件数据库，写路径的并发能力远低于真实 MySQL/PostgreSQL。"
                 "**写场景的绝对数字不可外推**。")
    lines.append("- 本机有杀毒/索引等后台负载，短时压测的 P99 抖动大，"
                 f"因此口径里设了 {DEGRADATION_TOLERANCE:.0%} 的噪声容忍带。")
    return "\n".join(lines)
