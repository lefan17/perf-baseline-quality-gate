"""门禁规则引擎。

## 为什么门禁要"能解释被拦的理由"

一个只会 `exit 1` 的门禁，在团队里的寿命大约是两周：第一次拦住人时，
没人知道该改代码还是改阈值，于是阈值被放宽，再拦几次就被
`continue-on-error` 掉。所以这里的每条规则都产出结构化结果：

    rule_id / severity / observed / expected / message

`message` 是**给人看的行动指引**（"把 N+1 改成聚合查询"，而不是"p95 超标"），
`severity` 决定它拦不拦流水线。

## severity 的三档

* `blocking` —— 拦住合并（P95 相对基线劣化、错误率超限、N+1 探针复活）；
* `warning`  —— 只报告不拦（P99 抖动、样本量不足）；
* `info`     —— 记录事实（本轮容量落在哪一级）。

分档的意义是让 blocking 只保留**真的能让生产出事故**的那几条。
全部门禁都是 blocking，等于没有门禁。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import yaml

BLOCKING = "blocking"
WARNING = "warning"
INFO = "info"


@dataclass
class RuleResult:
    rule_id: str
    passed: bool
    severity: str
    message: str
    observed: object = ""
    expected: object = ""
    action: str = ""

    def as_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "passed": self.passed,
            "severity": self.severity,
            "message": self.message,
            "observed": self.observed,
            "expected": self.expected,
            "action": self.action,
        }

    def describe(self) -> str:
        mark = "PASS" if self.passed else ("FAIL" if self.severity == BLOCKING else self.severity.upper())
        line = f"[{mark}] {self.rule_id}: {self.message}"
        if self.action and not self.passed:
            line += f"\n         处理建议：{self.action}"
        return line


@dataclass
class GateReport:
    results: list[RuleResult] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any((not r.passed) and r.severity == BLOCKING for r in self.results)

    @property
    def failed(self) -> list[RuleResult]:
        return [r for r in self.results if not r.passed]

    def render(self) -> str:
        out = ["门禁结果：", ""]
        out.extend(r.describe() for r in self.results)
        out.append("")
        out.append("判定：" + ("❌ 拦住（存在 blocking 未通过）" if self.blocked else "✅ 放行"))
        return "\n".join(out)


# --------------------------------------------------------------------------- #
def load_rules(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def evaluate_perf(run_metrics: dict, baseline: dict | None, deltas: list, rules: dict) -> GateReport:
    """性能门禁。

    `run_metrics` 是"本轮整轮"的汇总（含 p95_ms/p99_ms/error_rate/rps），
    `deltas` 是相对基线的差异列表。
    """
    report = GateReport()
    perf = rules.get("perf", {})
    thresholds = perf.get("thresholds", {})

    # R1 错误率（绝对阈值）—— 放在第一条，因为它最硬：接口都错了，快也没用。
    max_err = thresholds.get("error_rate_max")
    if max_err is not None:
        observed = run_metrics.get("error_rate", 0.0)
        report.results.append(
            RuleResult(
                rule_id="R1-error-rate",
                passed=observed <= max_err,
                severity=BLOCKING,
                message=f"错误率 {observed:.2%}（阈值 {max_err:.2%}）",
                observed=observed,
                expected=max_err,
                action="先看报告里的错误类型分布：超时/连接被拒通常是容量问题，"
                       "HTTP 5xx 是代码缺陷——两者的处理方式完全不同，别混在一起改阈值。",
            )
        )

    # R2 绝对 P95 —— 与基线无关的兜底线，防止"基线本身已经烂了"。
    max_p95 = thresholds.get("p95_ms_max")
    if max_p95 is not None:
        observed = run_metrics.get("p95_ms", 0.0)
        report.results.append(
            RuleResult(
                rule_id="R2-p95-absolute",
                passed=observed <= max_p95,
                severity=BLOCKING,
                message=f"P95 {observed:.1f}ms（绝对阈值 {max_p95}ms）",
                observed=observed,
                expected=max_p95,
                action="绝对阈值是兜底线。如果服务确实变重了（功能增加），"
                       "应当显式走一次基线评审再调阈值，而不是让它在某次提交里悄悄漂移。",
            )
        )

    # R3 相对基线的劣化 —— 这是本平台的核心价值：抓住"慢慢变慢"。
    if baseline is None:
        report.results.append(
            RuleResult(
                rule_id="R3-baseline-degradation",
                passed=True,
                severity=WARNING,
                message="未设置基线，跳过劣化对比（首次运行请显式设置基线）",
                action="运行 `python -m platform_cli baseline set ...` 建立基线。",
            )
        )
    else:
        degraded = [d for d in deltas if d.degraded]
        detail = "；".join(
            f"{d.metric} {d.pct_text()}（基线 {d.baseline:.1f}）" for d in degraded
        ) or "全部指标在容忍带内"
        report.results.append(
            RuleResult(
                rule_id="R3-baseline-degradation",
                passed=not degraded,
                severity=BLOCKING,
                message=f"相对基线劣化 {len(degraded)} 项：{detail}",
                observed={d.metric: d.current for d in degraded},
                expected=f"涨幅均 ≤ 容忍带",
                action="定位这一步的提交：先用 `--scenario slow_report` 单场景复压，"
                       "排除其它场景的干扰，再对照两次的 SQL 与索引。",
            )
        )

    # R4 样本量不足 —— 只警告。用它挡住"用 5 个请求的结论下判断"。
    min_total = thresholds.get("min_total_requests", 0)
    total = run_metrics.get("total", 0)
    if min_total:
        report.results.append(
            RuleResult(
                rule_id="R4-sample-size",
                passed=total >= min_total,
                severity=WARNING,
                message=f"采样 {total} 次（建议 ≥ {min_total}）",
                observed=total,
                expected=min_total,
                action="样本太少时分位数不可信（P99 在 100 个样本里其实就是最大值）。"
                       "通常说明压测时长不够或服务已经卡死。",
            )
        )

    # R5 P99 抖动 —— 只警告。P99 天然抖，把它设成 blocking 会天天红。
    p99_ratio_max = thresholds.get("p99_over_p95_max")
    p95, p99 = run_metrics.get("p95_ms", 0.0), run_metrics.get("p99_ms", 0.0)
    if p99_ratio_max and p95 > 0:
        ratio = p99 / p95
        report.results.append(
            RuleResult(
                rule_id="R5-tail-jitter",
                passed=ratio <= p99_ratio_max,
                severity=WARNING,
                message=f"P99/P95 = {ratio:.2f}（阈值 {p99_ratio_max}）",
                observed=round(ratio, 3),
                expected=p99_ratio_max,
                action="长尾比中位数更能反映用户体感。比值偏高通常指向锁等待、"
                       "GC 停顿或某个慢查询偶发命中——去查 max_ms 附近那几次请求。",
            )
        )
    return report


def evaluate_probe(report_p95: dict, rules: dict) -> GateReport:
    """性能探针门禁：把"已知的性能缺陷"钉成会红的规则。

    和功能用例的缺陷探针是同一个思路 —— **缺陷由清单跟踪，探针负责让
    "门禁真的能拦住它"这件事可被反复证明**。

    `report_p95` 形如 {"agg": 12.3, "n1": 210.4}，单位 ms。
    """
    report = GateReport()
    probe = rules.get("probe", {})
    max_p95 = probe.get("slow_report_p95_ms_max")
    ratio_max = probe.get("n1_over_agg_max")

    if "n1" in report_p95 and max_p95 is not None:
        observed = report_p95["n1"]
        report.results.append(
            RuleResult(
                rule_id="P1-n1-probe",
                passed=observed <= max_p95,
                severity=BLOCKING,
                message=f"N+1 报表 P95 {observed:.1f}ms（阈值 {max_p95}ms）",
                observed=observed,
                expected=max_p95,
                action="`/api/reports/slow-by-supplier` 的 n1 实现是**故意保留的性能缺陷探针**。"
                       "正常情况下走的是 agg 实现；这条红了说明有人把默认实现切回了 N+1，"
                       "或者数据量增长让 N+1 真的爆了。",
            )
        )

    if "n1" in report_p95 and "agg" in report_p95 and ratio_max:
        agg = max(report_p95["agg"], 0.1)
        ratio = report_p95["n1"] / agg
        report.results.append(
            RuleResult(
                rule_id="P2-n1-agg-ratio",
                passed=ratio <= ratio_max,
                severity=WARNING,
                message=f"N+1 / agg 耗时比 {ratio:.1f}×（阈值 {ratio_max}×）",
                observed=round(ratio, 2),
                expected=ratio_max,
                action="这个比值是「改成聚合查询能省多少」的直接证据，"
                       "写进优化提案比单说「慢」有说服力得多。",
            )
        )
    return report


def evaluate_suite(passed: int, failed: int, errors: int, flaky: list[dict], rules: dict) -> GateReport:
    """功能用例门禁：通过率 + flaky 用例数量。"""
    report = GateReport()
    suite = rules.get("suite", {})
    total = passed + failed + errors
    rate = (passed / total) if total else 1.0

    min_rate = suite.get("min_pass_rate", 1.0)
    report.results.append(
        RuleResult(
            rule_id="S1-pass-rate",
            passed=rate >= min_rate,
            severity=BLOCKING,
            message=f"通过率 {rate:.2%}（阈值 {min_rate:.2%}，{passed}/{total}）",
            observed=round(rate, 4),
            expected=min_rate,
            action="先看失败原因归类：断言失败是行为变更，报错是环境/依赖问题。"
                   "后者重跑一次，前者必须先确认是「预期变更」还是「缺陷」。",
        )
    )

    max_flaky = suite.get("max_flaky_cases")
    if max_flaky is not None:
        n = len(flaky)
        report.results.append(
            RuleResult(
                rule_id="S2-flaky-budget",
                passed=n <= max_flaky,
                severity=WARNING,
                message=f"flaky 用例 {n} 条（预算 {max_flaky} 条）",
                observed=n,
                expected=max_flaky,
                action="flaky 用例会训练团队忽略红灯。要么修好（查出真实的时序/数据依赖），"
                       "要么删掉——留在那里是最坏的选择。",
            )
        )
    return report


def merge(*reports: GateReport) -> GateReport:
    merged = GateReport()
    for r in reports:
        merged.results.extend(r.results)
    return merged
