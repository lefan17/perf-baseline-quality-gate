"""阈值只应有一个来源：`gate_rules.yaml`。

## 为什么必须有这个文件

`gate_rules.yaml` 的头注释对外承诺"阈值集中在这一处 —— 改标准改这里，
不改进代码"。审计时这句话是**假的**：可比性容忍带、flaky 判据、
容忍带文案全都硬编码在 `metrics/analysis.py` 里，YAML 改了它们不动。

字面意义上的"常量在哪个文件"不重要，重要的是**改一个数能不能改变行为**。
一条容易被写错的测试是"断言 YAML 里的值等于常量" —— 那种用例在
常量被硬编码时同样会绿。这里每条用例都走完整调用链：
改 YAML → 同一个函数返回不同结论。

## 为什么默认值必须与 YAML 一致

纯逻辑用例（`tests/test_platform.py`）不读文件系统，直接用模块常量。
所以模块常量必须等于 YAML 里的当前值，否则"不读配置的路径"与
"读了配置的路径"会对同一个事实给出两个答案 —— 这正是审计发现的老毛病
（`dashboard.api_gate` 曾经一处用常量、一处用 YAML）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from metrics import analysis, gate

pytestmark = pytest.mark.platform

ROOT = Path(__file__).resolve().parent.parent
RULES_PATH = ROOT / "gate_rules.yaml"


def _rules_with(tmp_path: Path, **overrides: object) -> dict:
    """把仓内规则读出来、按点号路径改几项，写成一份临时规则文件再加载。

    走"真的写文件再读"而不是直接改 dict：只有这样才能证明
    `load_rules` 读到的 YAML 键名是对的 —— 键名打错时改 dict 的用例照样绿。
    """
    raw = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))
    for dotted, value in overrides.items():
        node = raw
        *parents, leaf = dotted.split(".")
        for key in parents:
            node = node[key]
        node[leaf] = value
    path = tmp_path / "gate_rules.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return gate.load_rules(path)


class TestDefaultsMatchYaml:
    """YAML 里的当前值必须等于模块常量 —— 否则两条路径会互相打脸。"""

    def test_rules_file_is_readable(self):
        rules = gate.load_rules(RULES_PATH)
        assert rules["perf"]["thresholds"]["p95_ms_max"] > 0

    def test_tolerance_default_matches_yaml(self):
        rules = gate.load_rules(RULES_PATH)
        tol_delay, tol_rps = analysis.tolerance_from_rules(rules)
        assert tol_delay == analysis.DEGRADATION_TOLERANCE
        assert tol_rps == analysis.RPS_DEGRADATION_TOLERANCE

    def test_comparability_default_matches_yaml(self):
        rules = gate.load_rules(RULES_PATH)
        assert analysis.comparability_tolerance_from_rules(rules) == analysis.COMPARABILITY_TOLERANCE

    def test_flaky_defaults_match_yaml(self):
        rules = gate.load_rules(RULES_PATH)
        assert analysis.flaky_thresholds_from_rules(rules) == (
            analysis.FLAKY_MIN_RUNS,
            analysis.FLAKY_STABILITY_MAX,
        )

    def test_missing_section_falls_back_to_module_constant(self):
        """只留一部分规则的 YAML 不能让判定崩掉，也不能悄悄变成 0。"""
        assert analysis.comparability_tolerance_from_rules({}) == analysis.COMPARABILITY_TOLERANCE
        assert analysis.flaky_thresholds_from_rules({}) == (
            analysis.FLAKY_MIN_RUNS,
            analysis.FLAKY_STABILITY_MAX,
        )
        assert analysis.comparability_tolerance_from_rules(None) == analysis.COMPARABILITY_TOLERANCE
        assert analysis.flaky_thresholds_from_rules(None) == (
            analysis.FLAKY_MIN_RUNS,
            analysis.FLAKY_STABILITY_MAX,
        )


class TestYamlOverridesChangeVerdicts:
    """改 YAML 必须真的改结论 —— 这才是"阈值集中"的判据。"""

    def test_comparability_tolerance_comes_from_rules(self, tmp_path):
        """items 改了 30%：容忍带 5% 时不可比，容忍带 50% 时可比的。"""
        current = {
            "items": 13000,
            "report_mode": "agg",
            "scenario": "(mix)",
            "step_users": [10, 20],
            "step_duration_s": 8.0,
            "think_time_s": 0.0,
        }
        baseline = dict(current, items=10000)

        strict = analysis.check_comparability(
            current, baseline, _rules_with(tmp_path, **{"perf.comparability.tolerance": 0.05})
        )
        loose = analysis.check_comparability(
            current, baseline, _rules_with(tmp_path, **{"perf.comparability.tolerance": 0.50})
        )

        assert strict.verdict == "incomparable"
        assert loose.verdict == "ok"

    def test_analyze_flaky_reads_min_runs(self, tmp_path):
        """同一个用例 4 次执行里摇摆：门槛 6 次时"不下结论"，门槛 3 次时判 flaky。"""
        history = [
            {"outcome": "passed", "duration_s": 0.1},
            {"outcome": "failed", "duration_s": 0.1},
            {"outcome": "passed", "duration_s": 0.1},
            {"outcome": "failed", "duration_s": 0.1},
        ]
        patient = analysis.analyze_flaky(history, "t::e", _rules_with(tmp_path, **{"suite.flaky_min_runs": 6}))
        eager = analysis.analyze_flaky(history, "t::e", _rules_with(tmp_path, **{"suite.flaky_min_runs": 3}))

        assert not patient.flaky and patient.runs == 4
        assert eager.flaky

    def test_analyze_flaky_reads_stability_max(self, tmp_path):
        """6 次里 5 通过 = 稳定度 83.3%：上限 85% 判 flaky，上限 50% 不判。

        执行次数必须 ≥ 默认的 `flaky_min_runs`，否则会先被"样本不足"短路，
        测到的就不是稳定度上限了。
        """
        history = [{"outcome": "passed", "duration_s": 0.1}] * 5 + [{"outcome": "failed", "duration_s": 0.1}]
        is_flaky = analysis.analyze_flaky(history, "t::e", _rules_with(tmp_path, **{"suite.flaky_stability_max": 0.85}))
        is_fine = analysis.analyze_flaky(history, "t::e", _rules_with(tmp_path, **{"suite.flaky_stability_max": 0.50}))

        assert is_flaky.flaky
        assert not is_fine.flaky

    def test_moving_direction_reads_tolerance(self, tmp_path):
        """P95 涨 15%：容忍带 10% 时报"上升"，容忍带 20% 时报"持平"。"""
        series = [10.0, 10.0, 10.0, 11.5, 11.5, 11.5]
        strict = analysis.moving_direction(
            series, rules=_rules_with(tmp_path, **{"perf.tolerance.degradation_pct": 0.10})
        )
        loose = analysis.moving_direction(
            series, rules=_rules_with(tmp_path, **{"perf.tolerance.degradation_pct": 0.20})
        )

        assert "上升" in strict
        assert loose == "持平"

    def test_degradation_tolerance_reaches_r3(self, tmp_path):
        """端到端：容忍带从 YAML 一路走到 R3 的判定。

        同一份数据（P95 涨 15%），容忍带 5% 时 R3 拦住，
        容忍带 50% 时 R3 放行 —— 证明门禁读的是 YAML 而不是常量。
        """
        current = {"p95_ms": 115.0, "p99_ms": 115.0, "mean_ms": 115.0, "max_ms": 115.0, "error_rate": 0.0, "rps": 100.0}
        baseline = {
            "p95_ms": 100.0,
            "p99_ms": 100.0,
            "mean_ms": 100.0,
            "max_ms": 100.0,
            "error_rate": 0.0,
            "rps": 100.0,
        }

        def blocked_with(tol: float) -> bool:
            rules = _rules_with(
                tmp_path,
                **{
                    "perf.tolerance.degradation_pct": tol,
                    "perf.tolerance.rps_degradation_pct": tol,
                },
            )
            deltas = analysis.compare_to_baseline(current, baseline, rules)
            report = gate.evaluate_perf(current, {"metrics": baseline}, deltas, rules)
            return report.blocked

        assert blocked_with(0.05)
        assert not blocked_with(0.50)
