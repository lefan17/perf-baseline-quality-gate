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
        assert analysis.DEGRADATION_TOLERANCE >= 0.05, (
            "容忍带小于实测噪声量级（4.95%），会稳定产生假阳性"
        )


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
    return [{"outcome": "passed", "duration_s": 0.1}] * n_pass + [
        {"outcome": "failed", "duration_s": 0.2}
    ] * n_fail


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
    "perf": {"thresholds": {"error_rate_max": 0.01, "p95_ms_max": 300.0,
                            "min_total_requests": 2000, "p99_over_p95_max": 4.0}},
    "probe": {"slow_report_p95_ms_max": 150.0, "n1_over_agg_max": 10.0},
    "suite": {"min_pass_rate": 1.0, "max_flaky_cases": 2},
}


def _perf_run(**over):
    base = {"error_rate": 0.0, "p95_ms": 50.0, "p99_ms": 80.0, "total": 5000, "rps": 100.0,
            "mean_ms": 20.0, "max_ms": 200.0}
    base.update(over)
    return base


class TestPerfGate:
    def test_clean_run_passes(self):
        report = gate.evaluate_perf(_perf_run(), {"metrics": {"p95_ms": 50.0, "rps": 100.0}},
                                    [], RULES)
        assert not report.blocked

    def test_error_rate_blocks(self):
        report = gate.evaluate_perf(_perf_run(error_rate=0.05), None, [], RULES)
        assert report.blocked
        assert any(r.rule_id == "R1-error-rate" and not r.passed for r in report.results)

    def test_absolute_p95_is_the_backstop(self):
        """没有基线时，绝对阈值仍要能拦住 2 秒的 P95。"""
        report = gate.evaluate_perf(_perf_run(p95_ms=2000.0), None, [], RULES)
        assert report.blocked

    def test_missing_baseline_is_a_warning_not_a_block(self):
        """首次运行没有基线，不能被拦住 —— 否则 CI 第一天就是红的。"""
        report = gate.evaluate_perf(_perf_run(), None, [], RULES)
        r3 = next(r for r in report.results if r.rule_id == "R3-baseline-degradation")
        assert r3.passed and r3.severity == gate.WARNING
        assert not report.blocked

    def test_degradation_blocks(self):
        deltas = analysis.compare_to_baseline({"p95_ms": 200.0}, {"p95_ms": 100.0})
        report = gate.evaluate_perf(_perf_run(p95_ms=200.0), {"metrics": {"p95_ms": 100.0}},
                                    deltas, RULES)
        assert report.blocked

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
        report = gate.evaluate_perf(_perf_run(error_rate=0.9, p95_ms=9999.0, total=1,
                                              p99_ms=99999.0),
                                    {"metrics": {"p95_ms": 1.0}}, [], RULES)
        for r in report.results:
            assert r.action, f"{r.rule_id} 缺少处理建议"


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
        report = gate.evaluate_suite(40, 0, 0, [{"nodeid": "a"}, {"nodeid": "b"}, {"nodeid": "c"}],
                                     RULES)
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
    assert {r.rule_id for r in merged.results} == {"P1-n1-probe", "P2-n1-agg-ratio", "S1-pass-rate",
                                                   "S2-flaky-budget"}


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
        assert any(r["run_id"] == "r2" and r["status"] == "failed"
                   for r in store.recent_runs(kind="perf", label="lab"))

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

    def test_add_steps_accepts_stats_and_dicts(self):
        from bench.engine import summarize, Sample

        store.upsert_run("r4", "perf", "lab")
        st = summarize(
            [Sample(scenario="s", status=200, elapsed_ms=float(i), ok=True, at=1.0) for i in range(1, 101)],
            label="10u", users=10, duration_s=10.0,
        )
        store.add_steps("r4", [st, st.as_dict()])
        rows = store.steps_for_run("r4")
        assert len(rows) == 2
        assert rows[0]["p95_ms"] == 95.0  # nearest-rank: ceil(0.95*100)-1 = 94 → 第 95 个数

    def test_gate_results_roundtrip(self):
        store.upsert_run("r5", "perf", "lab")
        store.add_gate_results("r5", [
            {"rule_id": "R1-error-rate", "passed": False, "severity": "blocking",
             "message": "m", "observed": 1, "expected": 0},
        ])
        with store.tx() as conn:
            rows = conn.execute("SELECT * FROM gate_results WHERE run_id='r5'").fetchall()
        assert len(rows) == 1 and rows[0]["passed"] == 0

    def test_cases_and_flaky_history(self):
        store.upsert_run("r6", "suite", "lab")
        store.add_cases("r6", [
            {"nodeid": "t::a", "outcome": "passed", "duration": 0.1},
            {"nodeid": "t::a", "outcome": "failed", "duration": 0.2, "fail_message": "boom"},
        ])
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
