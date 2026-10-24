"""平台自身逻辑的测试：分析口径、门禁规则、结果入库。

## 为什么这些测试比普通单测更重要

引擎和接口的 bug 会让人**看到错误的数据**；而判断口径的 bug 会让人
**看到正确数据得出错误结论** —— 后者更危险，因为它不报错。
所以 `find_knee`、`compare_metric`、`analyze_flaky` 的边界行为
（正好在容忍带上、样本不足、基线为零）都要有用例钉住。
"""

from __future__ import annotations

import pytest

from metrics import analysis, gate, store

pytestmark = pytest.mark.platform


# --------------------------------------------------------------------------- #
# 基线对比
# --------------------------------------------------------------------------- #
class TestCompareMetric:
    """容忍带相关。

    **这些用例从常量推导阈值，不写死数字。**
    原因很实际：容忍带最初写 3%，用 `noise` 实测后发现本机噪声
    就有 8%，必须放宽到 10% —— 而一条写死 `105.0` 的用例
    在阈值变化时会红，红的原因却和被测逻辑毫无关系。
    照抄阈值的用例会把"改标准"变成"改一堆测试"，
    久而久之团队就不敢动标准了。
    """

    def test_within_tolerance_is_not_degradation(self):
        t = analysis.DEGRADATION_TOLERANCE
        d = analysis.compare_metric("p95_ms", 100.0, 100.0 * (1 + t / 2))
        assert not d.degraded and not d.improved

    def test_beyond_tolerance_is_degradation(self):
        t = analysis.DEGRADATION_TOLERANCE
        d = analysis.compare_metric("p95_ms", 100.0, 100.0 * (1 + t * 1.5))
        assert d.degraded

    def test_exactly_on_tolerance_boundary_is_not_degraded(self):
        """边界取"不大于"。

        这条看着吹毛求疵，其实很实际：如果边界算劣化，
        那么每次刚好踩线的抖动都会让流水线变红，
        团队很快就会去放宽阈值 —— 门禁于是被自己训练坏了。
        """
        t = analysis.DEGRADATION_TOLERANCE
        d = analysis.compare_metric("p95_ms", 100.0, 100.0 * (1 + t))
        assert not d.degraded

    def test_zero_baseline_does_not_crash(self):
        """基线为零（例如上一轮全部失败）不能除零。"""
        d = analysis.compare_metric("error_rate", 0.0, 0.0)
        assert d.delta_pct == 0.0 and not d.degraded
        d = analysis.compare_metric("error_rate", 0.0, 0.5)
        assert d.delta_pct == float("inf")
        assert d.degraded

    def test_tolerance_has_empirical_evidence_behind_it(self):
        """容忍带必须和实测噪声量级相符，不能比实测噪声还小。

        3% 曾经就是这个问题：本机实测波动 8.16%，
        容忍带却只有 3%，于是同一份代码连跑两轮也会红。
        **一个比噪声还窄的容忍带等于没有容忍带**，
        只会稳定地产出假阳性。

        这里守住的下限是"至少能吸收 `noise` 命令量到的那种波动"
        （实测单轮最大偏离均值 4.95%，取整 5%）。
        """
        assert analysis.DEGRADATION_TOLERANCE >= 0.05, "容忍带小于实测噪声量级（4.95%），会稳定产生假阳性"


class TestCompareToBaseline:
    def test_rps_direction_is_inverted(self):
        """RPS 掉是坏、涨是好 —— 和延迟的符号正好相反。

        用一个公式套所有指标是这类平台最常见的逻辑错误：
        它会把"吞吐掉了 20%"报成 🟢 改善。
        """
        deltas = analysis.compare_to_baseline(
            {"p95_ms": 100.0, "error_rate": 0.0, "rps": 80.0},
            {"p95_ms": 100.0, "error_rate": 0.0, "rps": 100.0},
        )
        rps = next(d for d in deltas if d.metric == "rps")
        assert rps.degraded and not rps.improved

    def test_rps_improvement_is_green(self):
        deltas = analysis.compare_to_baseline({"rps": 150.0}, {"rps": 100.0})
        assert deltas[0].improved and not deltas[0].degraded

    def test_error_rate_has_zero_tolerance(self):
        """错误率不允许任何"噪声带上涨"：0% → 0.4% 必须判劣化。"""
        deltas = analysis.compare_to_baseline({"error_rate": 0.004}, {"error_rate": 0.0})
        assert deltas[0].degraded

    def test_missing_metrics_are_skipped(self):
        """基线里没有的指标不能凭空造出 0，否则会伪造成"巨大改善"。"""
        deltas = analysis.compare_to_baseline({"p95_ms": 10.0}, {"p95_ms": 20.0})
        assert [d.metric for d in deltas] == ["p95_ms"]


class TestBaselineComparability:
    """两轮配置不同时，"劣化 40%"是假的 —— 平台上真的发生过。

    实测现场：baseline `items=5254 / 最高并发 20`，current `items=900 / 最高并发 80`，
    报告照样打出"p95 +40.0% 劣化"。两轮量的不是同一回事，那个百分比没有意义。
    三种结局必须分得开：ok / incomparable / unknown。
    """

    def test_same_config_is_comparable(self):
        cfg = {"items": 20000, "report_mode": "agg", "scenario": "(mix)", "step_users": [10, 20]}
        assert analysis.check_comparability(cfg, dict(cfg)).verdict == "ok"

    def test_different_items_is_incomparable(self):
        v = analysis.check_comparability(
            {"items": 900, "report_mode": "agg"},
            {"items": 5254, "report_mode": "agg"},
        )
        assert v.verdict == "incomparable"
        assert any("items" in r for r in v.reasons)

    def test_different_report_mode_is_incomparable(self):
        """agg 与 n1 是两种报表实现，混比等于把 N+1 的耗时当劣化。"""
        v = analysis.check_comparability(
            {"items": 20000, "report_mode": "n1"},
            {"items": 20000, "report_mode": "agg"},
        )
        assert v.verdict == "incomparable"

    def test_different_top_concurrency_is_incomparable(self):
        v = analysis.check_comparability(
            {"items": 20000, "step_users": [10, 20, 40, 80]},
            {"items": 20000, "step_users": [10, 20]},
        )
        assert v.verdict == "incomparable"
        assert any("并发" in r for r in v.reasons)

    def test_missing_baseline_config_is_unknown_not_ok(self):
        """ "老基线没记配置"是"无法判定"，不是"没问题"。"""
        v = analysis.check_comparability({"items": 20000}, {})
        assert v.verdict == "unknown"

    def test_no_current_config_keeps_legacy_behaviour(self):
        """老调用路径不传配置 → 不做校验，保持原有行为不变。"""
        assert analysis.check_comparability(None, {"items": 1}).verdict == "ok"

    def test_incomparable_baseline_turns_r3_into_a_skip(self):
        """不可比时 R3 必须是 SKIP：既不 blocked，也不写成"劣化 0 项"。"""
        current = {"items": 900, "report_mode": "agg", "step_users": [80]}
        baseline_cfg = {"items": 5254, "report_mode": "agg", "step_users": [20]}
        comparability = analysis.check_comparability(current, baseline_cfg)
        # 指标层面确实"劣化"了 40%，但两轮配置不可比 —— 这个百分比不该进判定。
        deltas = analysis.compare_to_baseline({"p95_ms": 140.0}, {"p95_ms": 100.0})
        report = gate.evaluate_perf(
            _perf_run(p95_ms=140.0),
            {"metrics": {"p95_ms": 100.0}, "config": baseline_cfg},
            deltas,
            RULES,
            comparability,
        )
        r3 = next(r for r in report.results if r.rule_id == "R3-baseline-degradation")
        assert r3.skipped and not r3.passed
        assert r3.severity == gate.WARNING
        assert not report.blocked
        assert "不可比" in r3.message
        assert "items" in r3.message  # 说清为什么不可比，而不是一句"不可比"
        # 关键：报告里不能出现一个看起来像结论的劣化百分比。
        assert "劣化 1 项" not in r3.message

    def test_comparable_baseline_still_blocks(self):
        """配置一致时旧行为原样保留：该拦还是拦。"""
        cfg = {"items": 20000, "report_mode": "agg", "step_users": [10, 20]}
        deltas = analysis.compare_to_baseline({"p95_ms": 140.0}, {"p95_ms": 100.0})
        report = gate.evaluate_perf(
            _perf_run(p95_ms=140.0),
            {"metrics": {"p95_ms": 100.0}, "config": cfg},
            deltas,
            RULES,
            analysis.check_comparability(cfg, dict(cfg)),
        )
        r3 = next(r for r in report.results if r.rule_id == "R3-baseline-degradation")
        assert not r3.skipped and not r3.passed
        assert report.blocked


# --------------------------------------------------------------------------- #
# 拐点
# --------------------------------------------------------------------------- #
def _step(users, rps, p95=10.0, err=0.0):
    return {"users": users, "rps": rps, "p95_ms": p95, "p99_ms": p95 * 2, "error_rate": err}


class TestFindKnee:
    def test_finds_knee_when_per_user_throughput_collapses(self):
        """每用户吞吐 5→5→5→3（掉 40%）→ 拐点在 80 并发。"""
        steps = [
            _step(10, 50.0),
            _step(20, 100.0),
            _step(40, 200.0),
            _step(80, 240.0),  # rps 还在涨，但每用户吞吐从 5 掉到 3
        ]
        knee = analysis.find_knee(steps)
        assert knee is not None
        assert knee["users"] == 80
        assert "每用户吞吐" in knee["reason"]

    def test_no_knee_when_linear_scaling(self):
        """完美线性扩展 → 没有拐点。此时报告必须说"没测到"，不能编一个。"""
        steps = [_step(u, u * 5.0) for u in (10, 20, 40, 80)]
        assert analysis.find_knee(steps) is None

    def test_error_rate_alone_triggers_knee(self):
        """吞吐没塌但开始报错，同样是过载信号。"""
        steps = [_step(10, 50.0), _step(20, 100.0), _step(40, 198.0, err=0.02)]
        knee = analysis.find_knee(steps)
        assert knee is not None and knee["users"] == 40
        assert "错误率" in knee["reason"]

    def test_needs_at_least_two_steps(self):
        assert analysis.find_knee([_step(10, 50.0)]) is None
        assert analysis.find_knee([]) is None

    def test_all_zero_rps_is_safe(self):
        """服务全挂（rps 全 0）不能除零崩溃 —— 这时更要出报告。"""
        assert analysis.find_knee([_step(10, 0.0), _step(20, 0.0)]) is None

    def test_warmup_ramp_is_not_a_knee(self):
        """低并发头几级天然偏低（预热/连接建立），全程线性扩展时不能报拐点。

        回归：旧实现拿"整段序列的最大值"当基准，于是第 1 级 4.0 相对后面
        稳定下来的 5.0 正好掉 20%，拐点被报在**最低并发**处，容量结论
        被打成 10 并发。
        """
        steps = [
            _step(10, 40.0),
            _step(20, 100.0),
            _step(40, 200.0),
            _step(80, 400.0),
        ]
        assert analysis.find_knee(steps) is None

    def test_transient_dip_that_recovers_is_not_a_knee(self):
        """中途抖一下、随后又回到峰值 —— 那不是容量边界。"""
        steps = [
            _step(10, 50.0),
            _step(20, 100.0),
            _step(40, 160.0),
            _step(80, 400.0),
        ]
        assert analysis.find_knee(steps) is None

    def test_sustained_decline_is_found_after_a_recovered_dip(self):
        """先抖一下恢复、之后才真的持续掉下去 —— 报持续下降的那一级。"""
        steps = [
            _step(10, 50.0),
            _step(20, 100.0),
            _step(40, 160.0),
            _step(80, 400.0),
            _step(160, 480.0),
        ]
        knee = analysis.find_knee(steps)
        assert knee is not None
        assert knee["users"] == 160
        assert "持续到末级" in knee["reason"]

    def test_last_stable_step_is_before_knee(self):
        """容量的结论取拐点前一级，不是拐点本身。

        拐点那一级已经在劣化了，拿它当容量承诺等于承诺一个做不到的数。
        """
        steps = [_step(10, 50.0), _step(20, 100.0), _step(40, 120.0)]
        knee = analysis.find_knee(steps)
        assert knee["users"] == 40
        assert analysis.last_stable_step(steps)["users"] == 20

    def test_last_stable_step_without_knee_returns_last(self):
        steps = [_step(10, 50.0), _step(20, 100.0)]
        assert analysis.last_stable_step(steps)["users"] == 20
        assert analysis.last_stable_step([]) is None


# --------------------------------------------------------------------------- #
# flaky
# --------------------------------------------------------------------------- #
def _hist(n_pass: int, n_fail: int) -> list[dict]:
    return [{"outcome": "passed", "duration_s": 0.1}] * n_pass + [{"outcome": "failed", "duration_s": 0.2}] * n_fail


class TestAnalyzeFlaky:
    def test_below_min_runs_refuses_to_conclude(self):
        """5 次执行里 2 次失败，看起来很像 flaky —— 但样本不够。

        在样本不足时下结论，会让人去"修"一个本来没问题的地方，
        比不下结论更浪费。
        """
        v = analysis.analyze_flaky(_hist(3, 2), "tests/test_x.py::test_a")
        assert not v.flaky
        assert "样本不足" in v.note

    def test_alternating_is_flaky(self):
        v = analysis.analyze_flaky(_hist(3, 3), "t::a")  # 6 次，稳定度 50%
        assert v.flaky and v.stability == pytest.approx(0.5)

    def test_mostly_passing_is_not_flaky(self):
        """19 过 1 失：偶发，但不是摇摆（稳定度 95%）。

        只看"失败次数"会把这条判成 flaky，从而制造无意义的工作。
        """
        v = analysis.analyze_flaky(_hist(19, 1), "t::b")
        assert not v.flaky
        assert v.stability == pytest.approx(0.95)

    def test_single_outcome_is_stable(self):
        v = analysis.analyze_flaky(_hist(10, 0), "t::c")
        assert not v.flaky and "稳定" in v.note

    def test_median_duration_is_reported(self):
        v = analysis.analyze_flaky(_hist(8, 0), "t::d")
        assert v.median_duration_s == pytest.approx(0.1)

    def test_empty_history(self):
        v = analysis.analyze_flaky([], "t::e")
        assert v.runs == 0 and not v.flaky


# --------------------------------------------------------------------------- #
# 趋势
# --------------------------------------------------------------------------- #
class TestTrend:
    def test_moving_direction_up_and_down(self):
        assert "上升" in analysis.moving_direction([10, 10, 10, 20, 20, 20])
        assert "下降" in analysis.moving_direction([20, 20, 20, 10, 10, 10])

    def test_flat_within_tolerance(self):
        assert analysis.moving_direction([100, 100, 100, 101, 100, 100]) == "持平"

    def test_insufficient_samples(self):
        assert analysis.moving_direction([1, 2]) == "样本不足"

    def test_trend_series(self):
        rows = [{"started_at": "t1", "p95_ms": 10.0}, {"started_at": "t2", "p95_ms": 12.0}]
        assert analysis.trend_series(rows, "p95_ms") == [("t1", 10.0), ("t2", 12.0)]
        assert analysis.trend_series(rows, "nope") == []


# --------------------------------------------------------------------------- #
# 门禁
# --------------------------------------------------------------------------- #
RULES = {
    "perf": {
        "thresholds": {"error_rate_max": 0.01, "p95_ms_max": 300.0, "min_total_requests": 2000, "p99_over_p95_max": 4.0}
    },
    "probe": {"slow_report_p95_ms_max": 150.0, "n1_over_agg_max": 10.0},
    "suite": {"min_pass_rate": 1.0, "max_flaky_cases": 2},
}


def _perf_run(**over):
    base = {
        "error_rate": 0.0,
        "p95_ms": 50.0,
        "p99_ms": 80.0,
        "total": 5000,
        "rps": 100.0,
        "mean_ms": 20.0,
        "max_ms": 200.0,
    }
    base.update(over)
    return base


class TestPerfGate:
    def test_clean_run_passes(self):
        report = gate.evaluate_perf(_perf_run(), {"metrics": {"p95_ms": 50.0, "rps": 100.0}}, [], RULES)
        assert not report.blocked

    def test_error_rate_blocks(self):
        report = gate.evaluate_perf(_perf_run(error_rate=0.05), None, [], RULES)
        assert report.blocked
        assert any(r.rule_id == "R1-error-rate" and not r.passed for r in report.results)

    def test_absolute_p95_is_the_backstop(self):
        """没有基线时，绝对阈值仍要能拦住 2 秒的 P95。"""
        report = gate.evaluate_perf(_perf_run(p95_ms=2000.0), None, [], RULES)
        assert report.blocked

    def test_missing_baseline_is_a_skip_not_a_pass(self):
        """首次运行没有基线：不拦人，但**不许长得像"比过了没劣化"**。

        `skipped=True` + `passed=False` 是这条的分界线：
        标成 `[SKIP]`（"本轮没判定"），而不是 `[PASS]`（"判定为没问题"）。
        流水线上曾经长期是后者 —— 报告全绿，而门禁的核心价值从未生效。
        """
        report = gate.evaluate_perf(_perf_run(), None, [], RULES)
        r3 = next(r for r in report.results if r.rule_id == "R3-baseline-degradation")
        assert r3.skipped and not r3.passed
        assert r3.severity == gate.WARNING, "skip 一律给 warning：只看 severity 的读者不该误读成拦住"
        assert "[SKIP]" in r3.describe() and "PASS" not in r3.describe()
        assert not report.blocked
        assert r3 in report.skipped and r3 not in report.failed

    def test_degradation_blocks(self):
        deltas = analysis.compare_to_baseline({"p95_ms": 200.0}, {"p95_ms": 100.0})
        report = gate.evaluate_perf(_perf_run(p95_ms=200.0), {"metrics": {"p95_ms": 100.0}}, deltas, RULES)
        assert report.blocked

    def test_advisory_keeps_the_verdict_but_drops_blocking(self):
        """`r3_advisory=True`：劣化照旧算出来、照旧打印，只是不拦人。

        这不是"恒 PASS"——`passed` 仍是 False，`describe()` 里仍有劣化项与
        百分比，`report.failed` 里仍有这条。区别只在 severity 与 blocked。
        """
        deltas = analysis.compare_to_baseline({"p95_ms": 200.0}, {"p95_ms": 100.0})
        report = gate.evaluate_perf(
            _perf_run(p95_ms=200.0), {"metrics": {"p95_ms": 100.0}}, deltas, RULES, r3_advisory=True
        )
        r3 = next(r for r in report.results if r.rule_id == "R3-baseline-degradation")
        assert not r3.passed, "只报数不等于判定通过"
        assert r3.severity == gate.WARNING
        assert not r3.skipped, "它真的判了，不该混进 skip"
        assert "相对基线劣化 1 项" in r3.message and "p95_ms" in r3.message
        assert "噪声主导" in r3.message, "必须如实说明这条判据为什么不算数"
        assert not report.blocked and r3 in report.failed

    def test_advisory_does_not_change_absolute_thresholds(self):
        """只报数模式**不放宽任何阈值**：R1/R2 该拦还是拦。"""
        report = gate.evaluate_perf(_perf_run(error_rate=0.05, p95_ms=9999.0), None, [], RULES, r3_advisory=True)
        assert report.blocked, "错误率与绝对 P95 是兜底线，advisory 不该动它们"

    def test_advisory_with_a_clean_run_still_passes(self):
        """没劣化时 advisory 不改变任何东西（不制造假的劣化文案）。"""
        deltas = analysis.compare_to_baseline({"p95_ms": 100.0}, {"p95_ms": 100.0})
        report = gate.evaluate_perf(
            _perf_run(p95_ms=100.0), {"metrics": {"p95_ms": 100.0}}, deltas, RULES, r3_advisory=True
        )
        r3 = next(r for r in report.results if r.rule_id == "R3-baseline-degradation")
        assert r3.passed and "全部指标在容忍带内" in r3.message
        assert "噪声主导" not in r3.message, "没劣化就别提噪声，那是噪音（字面意义的）"

    def test_small_sample_is_warning_only(self):
        """样本不足只警告。

        把它设成 blocking 会让"压测只跑了 8 秒"变成红色构建，
        而人真正该做的是把时长调回去，不是改阈值。
        """
        report = gate.evaluate_perf(_perf_run(total=500), None, [], RULES)
        r4 = next(r for r in report.results if r.rule_id == "R4-sample-size")
        assert not r4.passed and r4.severity == gate.WARNING
        assert not report.blocked

    def test_tail_jitter_is_warning_only(self):
        report = gate.evaluate_perf(_perf_run(p95_ms=50.0, p99_ms=500.0), None, [], RULES)
        r5 = next(r for r in report.results if r.rule_id == "R5-tail-jitter")
        assert not r5.passed and r5.severity == gate.WARNING
        assert not report.blocked

    def test_every_rule_carries_an_action(self):
        """每条规则的 action 都不能是空字符串。

        门禁的价值一半在拦人，一半在告诉人怎么办。
        一个只说"红了"的门禁，两周后就会被 ignore 掉。
        """
        report = gate.evaluate_perf(
            _perf_run(error_rate=0.9, p95_ms=9999.0, total=1, p99_ms=99999.0), {"metrics": {"p95_ms": 1.0}}, [], RULES
        )
        for r in report.results:
            assert r.action, f"{r.rule_id} 缺少处理建议"


class TestTailRatioIsMeasuredOnOneStep:
    """R5 的 P99/P95 必须来自**同一个梯度**。

    这条是踩出来的：`merge_step_rows` 原先让各分位数**各自取 max**，
    于是 P95 可能来自 A 级、P99 来自 B 级，比值对应的是一个
    **从未发生过的组合**。R5 判的偏偏就是这个比值 ——
    "各自取 max"在比值上不是保守方向，而是系统性掩盖。

    构造（真实能出现的一组数据）：

        梯度1  P95=50   P99=300  → 该级真实比值 6.00（> 4，应拦）
        梯度2  P95=200  P99=210  → 该级真实比值 1.05

    旧口径：P95=200 P99=300 → 1.50 → R5 判 **passed**（漏报）
    新口径：取比值最大的那一级 → 50/300 → 6.00 → R5 判 **FAIL**
    """

    ROWS = [
        {
            "total": 1000,
            "duration_s": 10.0,
            "error_count": 0,
            "rps": 100.0,
            "mean_ms": 20.0,
            "p50_ms": 30.0,
            "p90_ms": 45.0,
            "p95_ms": 50.0,
            "p99_ms": 300.0,
            "max_ms": 400.0,
            "max_inflight": 10,
        },
        {
            "total": 1000,
            "duration_s": 10.0,
            "error_count": 0,
            "rps": 100.0,
            "mean_ms": 60.0,
            "p50_ms": 150.0,
            "p90_ms": 190.0,
            "p95_ms": 200.0,
            "p99_ms": 210.0,
            "max_ms": 220.0,
            "max_inflight": 20,
        },
    ]

    def test_tail_ratio_comes_from_a_single_step(self):
        merged = analysis.merge_step_rows(self.ROWS)
        # 成对取值：不是 200/300，而是比值最大的那一级的 50/300。
        assert (merged["p95_ms"], merged["p99_ms"]) == (50.0, 300.0)
        assert merged["p99_ms"] / merged["p95_ms"] == pytest.approx(6.0)

    def test_tail_jitter_fails_where_naive_max_would_pass(self):
        """判别力所在：旧口径下这条用例会红。"""
        merged = analysis.merge_step_rows(self.ROWS)
        # 旧口径（各自取 max）会拼出的那个组合：
        naive = {"p95_ms": 200.0, "p99_ms": 300.0}
        assert naive["p99_ms"] / naive["p95_ms"] <= RULES["perf"]["thresholds"]["p99_over_p95_max"]

        run = _perf_run(p95_ms=merged["p95_ms"], p99_ms=merged["p99_ms"])
        r5 = next(r for r in gate.evaluate_perf(run, None, [], RULES).results if r.rule_id == "R5-tail-jitter")
        assert not r5.passed, "同一梯度内 6.00 的尾比必须被判 FAIL"
        assert r5.severity == gate.WARNING

    def test_other_percentiles_still_take_the_worst_step(self):
        """p50/p90/max 不参与比值判定，仍各自取最差一级。"""
        merged = analysis.merge_step_rows(self.ROWS)
        assert merged["p50_ms"] == 150.0
        assert merged["p90_ms"] == 190.0
        assert merged["max_ms"] == 400.0


class TestAbsoluteP95UsesWorstStep:
    """R2 的绝对 P95 必须看**最慢的那一级**，不是尾比最大的那一级。

    与上一个类同源：`p95_ms` 为了让 R5 的 P99/P95 成对取自同一梯度，
    选的是「比值最大」的那一级，它**不一定是绝对值最大的**。
    拿它去判 R2 的 300ms 绝对阈值，会在「某级整体偏慢但尾比平」时漏报。

    构造（两个梯度都真实存在）：

        梯度1  P95=350  P99=400  → 比值 1.14
        梯度2  P95=200  P99=310  → 比值 1.55  ← merge 选它

    旧口径：R2 看到 200ms < 300ms → **passed**（漏报，实际有一级已 350ms）
    新口径：取 max(200, 350) = 350ms > 300ms → **FAIL**
    """

    ROWS = [
        {
            "total": 1000,
            "duration_s": 10.0,
            "error_count": 0,
            "rps": 100.0,
            "mean_ms": 120.0,
            "p50_ms": 200.0,
            "p90_ms": 300.0,
            "p95_ms": 350.0,
            "p99_ms": 400.0,
            "max_ms": 500.0,
            "max_inflight": 10,
        },
        {
            "total": 1000,
            "duration_s": 10.0,
            "error_count": 0,
            "rps": 100.0,
            "mean_ms": 90.0,
            "p50_ms": 120.0,
            "p90_ms": 180.0,
            "p95_ms": 200.0,
            "p99_ms": 310.0,
            "max_ms": 330.0,
            "max_inflight": 20,
        },
    ]

    def test_two_scopes_are_reported_separately(self):
        merged = analysis.merge_step_rows(self.ROWS)
        # 尾比口径：比值最大的那一级（梯度2）
        assert merged["p95_ms"] == 200.0
        # 绝对值口径：最慢的那一级（梯度1）
        assert merged["p95_abs_max_ms"] == 350.0
        assert merged["p99_abs_max_ms"] == 400.0

    def test_absolute_threshold_reads_the_slowest_step(self):
        """判别力所在：只喂 `p95_ms` 的旧口径下这条会红。"""
        merged = analysis.merge_step_rows(self.ROWS)
        run = _perf_run(
            p95_ms=merged["p95_ms"],
            p99_ms=merged["p99_ms"],
            p95_abs_max_ms=merged["p95_abs_max_ms"],
        )
        r2 = next(r for r in gate.evaluate_perf(run, None, [], RULES).results if r.rule_id == "R2-p95-absolute")
        assert not r2.passed, "有一级已 350ms，绝对阈值 300ms 必须拦住"
        assert r2.observed == 350.0

    def test_old_baseline_without_the_new_key_still_works(self):
        """老基线没有 `p95_abs_max_ms`，退化回 `p95_ms`，不强制重建基线。"""
        r2 = next(
            r
            for r in gate.evaluate_perf(_perf_run(p95_ms=250.0), None, [], RULES).results
            if r.rule_id == "R2-p95-absolute"
        )
        assert r2.passed
        assert r2.observed == 250.0


class TestProbeGate:
    def test_healthy_ratio_passes(self):
        assert not gate.evaluate_probe({"agg": 10.0, "n1": 30.0}, RULES).blocked

    def test_slow_n1_blocks(self):
        """N+1 复活（或数据量涨到让它爆掉）必须拦住。"""
        report = gate.evaluate_probe({"agg": 12.0, "n1": 400.0}, RULES)
        assert report.blocked
        assert any(r.rule_id == "P1-n1-probe" and not r.passed for r in report.results)

    def test_ratio_warning_is_not_blocking(self):
        report = gate.evaluate_probe({"agg": 10.0, "n1": 140.0}, RULES)  # 14× 但 < 150ms
        assert not report.blocked
        p2 = next(r for r in report.results if r.rule_id == "P2-n1-agg-ratio")
        assert not p2.passed and p2.severity == gate.WARNING

    def test_missing_agg_does_not_crash(self):
        assert not gate.evaluate_probe({"n1": 10.0}, RULES).blocked


class TestSuiteGate:
    def test_all_pass(self):
        assert not gate.evaluate_suite(40, 0, 0, [], RULES).blocked

    def test_any_failure_blocks(self):
        report = gate.evaluate_suite(39, 1, 0, [], RULES)
        assert report.blocked

    def test_flaky_over_budget_is_warning(self):
        """flaky 超预算只警告。flaky 是"要处理的债务"，不是"必须回滚的事故"。"""
        report = gate.evaluate_suite(40, 0, 0, [{"nodeid": "a"}, {"nodeid": "b"}, {"nodeid": "c"}], RULES)
        s2 = next(r for r in report.results if r.rule_id == "S2-flaky-budget")
        assert not s2.passed and s2.severity == gate.WARNING
        assert not report.blocked

    def test_empty_suite_is_not_a_failure(self):
        """没跑到任何用例（例如环境缺依赖全跳过）不该被算成 0% 通过率。"""
        report = gate.evaluate_suite(0, 0, 0, [], RULES)
        assert not report.blocked


def test_merge_combines_reports():
    a = gate.evaluate_probe({"agg": 10.0, "n1": 20.0}, RULES)
    b = gate.evaluate_suite(1, 0, 0, [], RULES)
    merged = gate.merge(a, b)
    assert {r.rule_id for r in merged.results} == {"P1-n1-probe", "P2-n1-agg-ratio", "S1-pass-rate", "S2-flaky-budget"}


def test_load_rules_reads_real_config():
    """真实配置文件必须能被解析 —— 别让阈值只在示例里存在。"""
    from platform_cli import DEFAULT_RULES

    rules = gate.load_rules(str(DEFAULT_RULES))
    assert rules["perf"]["thresholds"]["p95_ms_max"] > 0
    assert rules["probe"]["n1_over_agg_max"] > 0
    assert rules["suite"]["min_pass_rate"] == 1.0


# --------------------------------------------------------------------------- #
# 入库
# --------------------------------------------------------------------------- #
class TestStore:
    def test_run_lifecycle(self):
        store.upsert_run("r1", "perf", "lab", config={"a": 1})
        runs = store.recent_runs(kind="perf", label="lab")
        assert runs[0]["run_id"] == "r1" and runs[0]["status"] == "running"
        store.finish_run("r1", "passed", {"p95_ms": 12.0})
        runs = store.recent_runs(kind="perf", label="lab")
        assert runs[0]["status"] == "passed"

    def test_failed_runs_are_stored_too(self):
        """失败也要入库。只存成功记录的度量平台，通过率是假的。"""
        store.upsert_run("r2", "perf", "lab")
        store.finish_run("r2", "failed", {"p95_ms": 999.0})
        assert any(r["run_id"] == "r2" and r["status"] == "failed" for r in store.recent_runs(kind="perf", label="lab"))

    def test_upsert_is_idempotent(self):
        store.upsert_run("r3", "perf", "lab", config={"a": 1})
        store.upsert_run("r3", "perf", "lab", status="passed", config={"a": 2})
        assert len([r for r in store.recent_runs() if r["run_id"] == "r3"]) == 1

    def test_baseline_roundtrip_and_overwrite(self):
        assert store.get_baseline("perf", "lab") is None
        store.set_baseline("perf", "lab", "r1", {"p95_ms": 10.0})
        assert store.get_baseline("perf", "lab")["metrics"]["p95_ms"] == 10.0
        store.set_baseline("perf", "lab", "r3", {"p95_ms": 30.0})  # 显式覆盖
        b = store.get_baseline("perf", "lab")
        assert b["run_id"] == "r3" and b["metrics"]["p95_ms"] == 30.0

    def test_baseline_is_not_touched_by_finishing_a_run(self):
        """这是平台最重要的一条纪律：跑一轮不会自动更新基线。

        如果基线跟着每次运行走，"劣化对比"永远是绿的 ——
        因为它比较的永远是最新值和自己。
        """
        store.set_baseline("perf", "lab", "r1", {"p95_ms": 10.0})
        store.upsert_run("r9", "perf", "lab")
        store.finish_run("r9", "passed", {"p95_ms": 999.0})
        assert store.get_baseline("perf", "lab")["run_id"] == "r1"

    def test_baseline_config_snapshot_roundtrip(self):
        """基线要连"当时被测的是什么"一起存，否则事后无从判断可比性。"""
        store.set_baseline(
            "perf", "lab", "r1", {"p95_ms": 10.0}, config={"items": 20000, "report_mode": "agg", "step_users": [10, 20]}
        )
        b = store.get_baseline("perf", "lab")
        assert b["config"]["items"] == 20000
        assert b["config"]["step_users"] == [10, 20]

    def test_baseline_without_config_reads_as_empty(self):
        """不带 config 的老调用方式仍可用，但快照读出来是空 dict（→ 校验报 unknown）。"""
        store.set_baseline("perf", "lab2", "r1", {"p95_ms": 10.0})
        assert store.get_baseline("perf", "lab2")["config"] == {}

    def test_old_database_gets_the_config_column_added(self):
        """老库没有 config_json 列，升级后必须能自动补上（幂等 ALTER TABLE）。

        直接用一份"旧版 schema"建库，再调 init_db()，模拟真实的升级路径：
        不补列的话，第一次 set_baseline 就会 `no such column: config_json`。
        """
        import sqlite3

        db = store.db_path()
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE baselines (kind TEXT NOT NULL, label TEXT NOT NULL, run_id TEXT NOT NULL,"
            " set_at TEXT NOT NULL, metrics_json TEXT NOT NULL, PRIMARY KEY (kind, label))"
        )
        conn.commit()
        conn.close()

        store.init_db()  # 应当就地补列，而不是抛异常
        store.init_db()  # 再调一次：幂等，不重复 ALTER
        store.set_baseline("perf", "old", "r1", {"p95_ms": 10.0}, config={"items": 5})
        assert store.get_baseline("perf", "old")["config"]["items"] == 5

    def test_add_steps_accepts_stats_and_dicts(self):
        from bench.engine import summarize, Sample

        store.upsert_run("r4", "perf", "lab")
        st = summarize(
            [Sample(scenario="s", status=200, elapsed_ms=float(i), ok=True, at=1.0) for i in range(1, 101)],
            label="10u",
            users=10,
            duration_s=10.0,
        )
        store.add_steps("r4", [st, st.as_dict()])
        rows = store.steps_for_run("r4")
        assert len(rows) == 2
        assert rows[0]["p95_ms"] == 95.0  # nearest-rank: ceil(0.95*100)-1 = 94 → 第 95 个数

    def test_gate_results_roundtrip(self):
        store.upsert_run("r5", "perf", "lab")
        store.add_gate_results(
            "r5",
            [
                {
                    "rule_id": "R1-error-rate",
                    "passed": False,
                    "severity": "blocking",
                    "message": "m",
                    "observed": 1,
                    "expected": 0,
                },
            ],
        )
        with store.tx() as conn:
            rows = conn.execute("SELECT * FROM gate_results WHERE run_id='r5'").fetchall()
        assert len(rows) == 1 and rows[0]["passed"] == 0

    def test_cases_and_flaky_history(self):
        store.upsert_run("r6", "suite", "lab")
        store.add_cases(
            "r6",
            [
                {"nodeid": "t::a", "outcome": "passed", "duration": 0.1},
                {"nodeid": "t::a", "outcome": "failed", "duration": 0.2, "fail_message": "boom"},
            ],
        )
        hist = store.cases_for_node("t::a")
        assert len(hist) == 2
        assert store.all_nodeids() == ["t::a"]

    def test_foreign_key_cascade_on_run_delete(self):
        """删运行记录时要级联删掉它的步骤，否则库里会积累孤儿行。

        孤儿行不会立刻出问题，但会让"总请求数"这类全库聚合悄悄偏大。
        """
        store.upsert_run("r7", "perf", "lab")
        store.add_steps("r7", [{"label": "10u", "users": 10, "total": 1, "p95_ms": 1.0}])
        with store.tx() as conn:
            conn.execute("DELETE FROM runs WHERE run_id='r7'")
        assert store.steps_for_run("r7") == []


def test_db_path_honours_env(monkeypatch, tmp_path):
    """测试必须能把自己隔离到临时库 —— 否则跑单测会污染看板数据。"""
    target = tmp_path / "custom.db"
    monkeypatch.setenv(store.DB_ENV, str(target))
    assert store.db_path() == target
