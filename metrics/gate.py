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
    # 「这条规则本轮没被真正判定过」——例如取不到数据、或 0 个用例根本算不出通过率。
    #
    # 为什么需要一个独立字段而不是"不产出这条结果"：
    # 一条**无声消失**的规则和一条**通过**的规则在报告里长得一样，
    # 而它们的意思正好相反 —— 前者是"没校验"，后者是"校验过了、没问题"。
    # 本项目最值钱的发现（P-4 聚合扇出，SUM(stock) 虚高 2.23 倍）就靠
    # P0-probe-data-equivalence 抓；这条规则一旦静默消失，报告仍然全绿。
    #
    # 语义约定：skipped=True 的结果**永远不进 blocked / failed**，
    # severity 一律给 warning（而不是 blocking）—— 存进 gate_results 表后
    # 只看 severity 的读者不该把"没判定"误读成"拦住了"。
    skipped: bool = False

    def as_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "passed": self.passed,
            "severity": self.severity,
            "message": self.message,
            "observed": self.observed,
            "expected": self.expected,
            "action": self.action,
            "skipped": self.skipped,
        }

    def describe(self) -> str:
        if self.skipped:
            mark = "SKIP"
        else:
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
        # skipped 的规则一律不算拦住：它表示"本轮没判定"，不是"判定为坏"。
        return any((not r.passed) and r.severity == BLOCKING and not r.skipped for r in self.results)

    @property
    def failed(self) -> list[RuleResult]:
        return [r for r in self.results if not r.passed and not r.skipped]

    @property
    def skipped(self) -> list[RuleResult]:
        return [r for r in self.results if r.skipped]

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


def evaluate_probe_data(agg_rows: list[dict] | None, n1_rows: list[dict] | None) -> GateReport:
    """P0：两种实现必须返回**同一份数据**，否则速度对比不成立。

    ## 为什么这条规则必须存在（本项目最讽刺的一处疏漏）

    这个平台最有价值的发现（`docs/已知问题.md` 的 P-4，聚合扇出缺陷）
    正是靠"把 agg 和 n1 的输出逐字段比对"抓出来的：
    旧写法 `SUM(stock)` 虚高 2.23 倍（113194 对 50697），
    而它**不报错、不慢、功能用例全绿** —— 只有交叉校验能发现。

    平台甚至把这条提炼成了第一条方法论写进 README：
    「性能对比必须先证明两种实现语义等价」。结果 `probe` 主路径上
    **从来没有验证过这个前提** —— 它只取耗时不取响应体。
    于是这个前提只在单测里成立，跑压测时是不设防的。

    这条规则就是补上那个缺口：进门禁之前先比数据，
    不一致直接拦住，并且说清楚"此时比快慢是没有意义的"。

    ## 取不到数据时必须"出声"

    以前这里写的是 `if agg_rows is None or n1_rows is None: return report`——
    返回一个**空报告**，理由是"由别的规则去报错"。
    问题是没有任何别的规则会替它报错：`report.results` 里连
    `P0-probe-data-equivalence` 这个名字都不会出现，
    报告照样全绿。**一条无声消失的规则，和一条通过的规则长得一模一样**，
    而前者是"没校验"、后者是"校验过了"。
    本项目三份真实 probe 报告里，这一行都是缺席的。

    现在改成产出一条 `skipped=True` 的结果：它出现在报告里（标 `[SKIP]`），
    但不进 blocked —— 取不到数不等于数据不一致，不该把 CI 判红。
    """
    report = GateReport()
    if agg_rows is None or n1_rows is None:
        missing = [n for n, v in (("agg", agg_rows), ("n1", n1_rows)) if v is None]
        report.results.append(
            RuleResult(
                rule_id="P0-probe-data-equivalence",
                passed=False,
                severity=WARNING,
                skipped=True,
                message=f"取不到 {'/'.join(missing)} 的响应体，本轮**没有校验**两种实现的数据是否等价"
                        f"（不是「一致」，是「没比」）",
                observed="",
                expected=0,
                action="这条规则是本平台最有价值的发现（P-4 聚合扇出，SUM(stock) 虚高 2.23 倍）"
                       "唯一的守卫，它被跳过时速度对比就失去了前提。请查明取数失败的原因"
                       "（服务已停 / 网络抖动 / 接口报错），而不是把本轮当成通过。",
            )
        )
        return report

    diffs, uncovered = _row_diffs(agg_rows, n1_rows)
    extra = (f"；另有 {uncovered} 个供应商只在 agg 出现（n1 侧有 LIMIT 上限），"
             f"这部分没有参与比对" if uncovered else "")

    if diffs:
        report.results.append(
            RuleResult(
                rule_id="P0-probe-data-equivalence",
                passed=False,
                severity=BLOCKING,
                message=f"两种实现返回的数据**不一致**：{len(diffs)} 处差异，{diffs[0]}{extra}",
                observed=len(diffs),
                expected=0,
                action="先修数据一致性，再谈性能。agg 与 n1 返回不同结果时，"
                       "两者不是一个功能的两种写法，比出来的倍数没有意义 —— "
                       "本项目真实踩过这个坑：聚合扇出让 SUM(stock) 虚高 2.23 倍，"
                       "而它不报错、不慢、功能用例全绿。",
            )
        )
    elif uncovered:
        # agg 单方面多出若干行、而两边都覆盖到的部分逐字段相等。
        # 这**既可能是**设计上的 LIMIT 截断（n1 侧只取前 N 个供应商），
        # **也可能是** n1 自己漏了行 —— 两种情形在这里长得一模一样
        # （见 _row_diffs 的注释）。所以不能判 pass：那等于用"一致"
        # 给一个没比完的结论盖章。
        #
        # 之所以不像从前那样报"行数不同 = 不一致"：种子数据恰好 40 个供应商、
        # n1 上限也是 40，一旦有人把 PERF_SUT_N1_SUPPLIERS 调小，
        # 旧写法会把一个**设计上的截断**说成数据缺陷，那是误报。
        # 判 skipped（无法判定）比两种误判都诚实，而且在报告里看得见。
        report.results.append(
            RuleResult(
                rule_id="P0-probe-data-equivalence",
                passed=False,
                severity=WARNING,
                skipped=True,
                message=f"两边的行数不同（agg={len(agg_rows)} n1={len(n1_rows)}）{extra}："
                        f"数据等价性**无法判定**（已覆盖到的行逐字段相等，"
                        f"但没覆盖的部分没人比过）",
                observed=uncovered,
                expected=0,
                action="先确认这是设计上的截断（`PERF_SUT_N1_SUPPLIERS` 上限）还是 n1 漏行。"
                       "若是截断，让两侧取数范围一致再比对；若是漏行，那是真缺陷。"
                       "在这之前，上面那个耗时倍数只对两边都覆盖到的供应商成立。",
            )
        )
    else:
        report.results.append(
            RuleResult(
                rule_id="P0-probe-data-equivalence",
                passed=True,
                severity=BLOCKING,
                message="两种实现返回的数据一致（各 %d 行）" % len(agg_rows),
                observed=0,
                expected=0,
                action="先修数据一致性，再谈性能。agg 与 n1 返回不同结果时，"
                       "两者不是一个功能的两种写法，比出来的倍数没有意义 —— "
                       "本项目真实踩过这个坑：聚合扇出让 SUM(stock) 虚高 2.23 倍，"
                       "而它不报错、不慢、功能用例全绿。",
            )
        )
    return report


# 数据一致性校验要比的字段。故意显式列出而不是取交集：
# 少比一个字段是**静默**的，而多比一个字段会立刻报错 —— 宁可报错。
_PROBE_DATA_FIELDS = ("supplier_id", "item_count", "stock_total", "delta_recent")

# 差异明细最多列几条，避免报告被上百行差异淹没
_MAX_LISTED_DIFFS = 5


def _index_by_supplier(rows: list[dict]) -> dict | None:
    """按 supplier_id 建索引；字段缺失或有重复时返回 None（退回按顺序比对）。"""
    ids = [r.get("supplier_id") for r in rows]
    if any(i is None for i in ids) or len(set(ids)) != len(ids):
        return None
    return dict(zip(ids, rows))


def _row_diffs(agg_rows: list[dict], n1_rows: list[dict]) -> tuple[list[str], int]:
    """逐字段比对两种实现的输出。

    返回 `(差异列表, n1 未覆盖的 agg 行数)`。

    ## 为什么要区分"差异"和"没覆盖"

    agg 分支不 LIMIT，n1 分支 `LIMIT PERF_SUT_N1_SUPPLIERS`（默认 40）。
    种子数据恰好也是 40 个供应商，所以两个数字**永远相等**、"行数不同"
    这条分支从来没被执行过。一旦供应商多过上限，agg 会比 n1 多出几行，
    按老写法就是"行数不同"→ P0 误报不一致 → 把一个**设计上的截断**
    说成数据缺陷。

    所以这里按 supplier_id 对齐：**n1 覆盖的每一行都必须在 agg 里逐字段
    相等**（n1 少一行 = 真实缺陷，照样报）；agg 单方面多出来的行单独计数
    返回给上层写进结论，而不是混进 diffs。

    唯一无法覆盖的情形：n1 因为自身缺陷漏掉了末尾某个供应商 —— 那行
    此时看起来和"被 LIMIT 截断"一样。所以覆盖率数字必须**显式**出现在
    结论里（上层已拼进 message），让读者知道结论的边界在哪。
    """
    diffs: list[str] = []

    def add(text: str) -> bool:
        """追加一条差异；已经够多了就只留省略号，并让调用方停下。"""
        if len(diffs) < _MAX_LISTED_DIFFS:
            diffs.append(text)
            return True
        diffs.append("…")
        return False

    def compare(a: dict, n: dict) -> bool:
        for f in _PROBE_DATA_FIELDS:
            if f not in a or f not in n:
                if not add(f"字段 {f} 缺失：agg={f in a} n1={f in n}"):
                    return False
                continue
            if a[f] != n[f]:
                if not add(f"供应商 {a.get('supplier_id', '?')} 的 {f}：agg={a[f]} n1={n[f]}"):
                    return False
        return True

    agg_idx = _index_by_supplier(agg_rows)
    n1_idx = _index_by_supplier(n1_rows)
    if agg_idx is None or n1_idx is None:
        # supplier_id 不可用时退回按顺序比对：此时行数不同本身就是差异，
        # 这一点绝不能放宽（放宽等于放弃校验）。
        if len(agg_rows) != len(n1_rows):
            diffs.append(f"行数不同：agg={len(agg_rows)} n1={len(n1_rows)}")
        for a, n in zip(agg_rows, n1_rows):
            if not compare(a, n):
                break
        return diffs, 0

    for sid, n in n1_idx.items():
        a = agg_idx.get(sid)
        if a is None:
            if not add(f"供应商 {sid} 在 agg 里缺失（n1 有而 agg 没有）"):
                break
            continue
        if not compare(a, n):
            break
    return diffs, len(set(agg_idx) - set(n1_idx))


def evaluate_perf(run_metrics: dict, baseline: dict | None, deltas: list, rules: dict,
                  comparability=None, r3_advisory: bool = False) -> GateReport:
    """性能门禁。

    `run_metrics` 是"本轮整轮"的汇总（含 p95_ms/p99_ms/error_rate/rps），
    `deltas` 是相对基线的差异列表。

    `comparability` 是 `analysis.Comparability`（可选）。传了它、且判定为
    "不可比 / 无法判定"时，R3 走 `skipped` —— **不给劣化百分比**。这是刻意的：
    把两轮不同配置的差值当成劣化，比不给结论危险得多。不传则保持原行为
    （老调用路径与单元测试构造的极小 baseline 都不带配置）。

    `r3_advisory` 把 R3 从 blocking 降为只报数（`severity=WARNING`，仍
    `passed=False`、仍打印劣化项与百分比、仍入库）。它**不改任何阈值** ——
    改的只是"这条判据在这一环境下算不算数"。

    为什么需要它：容忍带 10% 是在开发机上按 P95 的轮间波动定的（见
    `gate_rules.yaml` 与 `docs/度量口径.md`）。CI runner 上同一份配置连跑几轮，
    实测 P95 稳、**p99 / max 的轮间波动远大于容忍带**（数字见
    `docs/已知问题.md` P-16）。于是 R3 在 CI 上会随噪声随机变红，
    而"会随机变红的门禁"唯一的结果是训练所有人忽略红灯。
    诚实做法不是放宽阈值（那是调参直到它说 PASS），而是：
    **照实打印判定，同时说清这条判据在这里不足以区分劣化与噪声。**
    默认 `False`：本机、单测、以及任何噪声未被证明主导的环境保持原行为。
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
        # "没有基线"不是"比过了没劣化"。
        #
        # 这里原先写 passed=True 且不置 skipped，于是报告里长得像一条
        # **通过的**规则：`[PASS] R3-baseline-degradation: 未设置基线…`。
        # 后果是流水线上的 R3 一直是这个状态，而"跳过"与"比过了没劣化"
        # 在报告里长得一模一样 —— 门禁的核心价值（抓"慢慢变慢"）
        # 在 CI 里从未生效过，却没有任何人看得出来。
        # 现在与 P0 / S1 的"无法判定"共用一套语义：skipped=True，标 [SKIP]，
        # 看得见（不许静默消失），但不进 blocked（首次运行不该拦住任何人）。
        report.results.append(
            RuleResult(
                rule_id="R3-baseline-degradation",
                passed=False,
                severity=WARNING,
                skipped=True,
                message="未设置基线，跳过劣化对比（首次运行请显式设置基线）",
                action="先跑一轮并显式设基线："
                       "`python platform_cli.py run --label X --set-baseline`；"
                       "再 `python platform_cli.py baseline export --label X` 把快照提交进仓库，"
                       "CI 里的 `baseline import` 就能拿到它。",
            )
        )
    else:
        degraded = [d for d in deltas if d.degraded]
        detail = "；".join(
            f"{d.metric} {d.pct_text()}（基线 {d.baseline:.1f}）" for d in degraded
        ) or "全部指标在容忍带内"
        if comparability is not None and comparability.verdict != "ok":
            # "无法判定"不是"通过"：passed=False + skipped=True，既不进 blocked，
            # 也不会在报告里长得像一条通过的规则。
            report.results.append(
                RuleResult(
                    rule_id="R3-baseline-degradation",
                    passed=False,
                    severity=WARNING,
                    skipped=True,
                    message=comparability.describe(),
                    observed={d.metric: d.current for d in deltas},
                    expected="两轮被测配置一致（items / 并发 / report_mode / scenario）",
                    action="基线不可比时不要读劣化百分比：要么用与基线相同的配置复跑本轮，"
                           "要么用 `python -m platform_cli baseline set` 在当前配置上重建基线。",
                )
            )
        else:
            # 有基线、配置可比 —— 真做劣化判定。
            # advisory 模式下照样打印劣化项与百分比（不是静默、不是恒 PASS），
            # 只是不把这条判据当拦人的依据。
            advisory = (
                "本轮以只报数模式运行（`--r3-advisory`）：这条判据在当前环境下"
                "噪声主导，不足以区分劣化与噪声，因此不拦人。劣化项本身仍然成立，"
                "要当回归处理请先按 `docs/已知问题.md` P-16 确认噪声量级。"
                if degraded and r3_advisory
                else ""
            )
            message = f"相对基线劣化 {len(degraded)} 项：{detail}"
            if advisory:
                message = f"{message}；⚠ {advisory}"
            report.results.append(
                RuleResult(
                    rule_id="R3-baseline-degradation",
                    passed=not degraded,
                    severity=WARNING if r3_advisory else BLOCKING,
                    message=message,
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


def evaluate_probe(report_p95: dict, rules: dict, sample_totals: dict | None = None) -> GateReport:
    """性能探针门禁：把"已知的性能缺陷"钉成会红的规则。

    和功能用例的缺陷探针是同一个思路 —— **缺陷由清单跟踪，探针负责让
    "门禁真的能拦住它"这件事可被反复证明**。

    `report_p95` 形如 {"agg": 12.3, "n1": 210.4}，单位 ms。

    `sample_totals` 形如 {"agg": 760, "n1": 10}，是本轮各模式实际采到的请求数。
    **不传时行为与从前完全一致**（不产生任何新规则）—— 这样单测里那些
    「只看比值」的调用不受影响；只有真正跑过压测的调用方才会被要求
    为自己的样本量负责。

    ## 为什么探针必须看样本量（否则 BLOCKING 的判定建在 10 个样本上）

    这条规则的判据是 P95。而 `bench/engine.py` 的分位数是 nearest-rank：
    N 个样本里 P95 取的是第 ceil(0.95N) 个 —— 当 N < 20 时它**就是最大值**。

    CI 日志里的现场（`ci_logs/…N_1_缺陷探针_…txt`）：

        [probe] mode=n1 rps=1.32 p95=3828.6ms p99=3828.6ms err=0.00%

    n1 是慢查询，4 并发 × 8 秒只跑出约 1.32×8 ≈ **10 个样本**，
    于是 p95 与 p99 打印出同一个数字 —— 这不是"长尾很集中"，
    这是分位数在样本不足时退化成了最大值。

    也就是说：**一条 BLOCKING 规则，拿着 10 个样本的"P95"在拦流水线**。
    结论方向恰好是对的（3828.6ms 确实远超 150ms），但这个数字不配叫 P95，
    而报告里对此**只字未提**。样本量校验在 `run` 路径上是有的（R4），
    probe 路径上则完全没有 —— 因为 `evaluate_probe` 从来没接收过样本数。

    处置方式与 R4 保持一致：**severity=WARNING**。
    理由有两条：
    1. 样本少说明"这轮压测不够长"，不说明"系统坏了"。R4 早就为此定过调
       （见 tests/test_platform.py 里那条用例的注释：把它设成 blocking
       会让"压测只跑了 8 秒"变成红色构建）；
    2. 这里真正要修的是**静默**，不是把结论翻过来。所以除了这条 WARNING，
       样本不足时 `P1-n1-probe` 的结论本身也会被显式标注"不可作为最终依据"
       —— 不静默放过，也不假装它没发生。
    """
    report = GateReport()
    probe = rules.get("probe", {})
    max_p95 = probe.get("slow_report_p95_ms_max")
    ratio_max = probe.get("n1_over_agg_max")

    min_total = rules.get("perf", {}).get("thresholds", {}).get("min_total_requests", 0)
    starved: dict[str, int] = {}
    if sample_totals is not None and min_total:
        starved = {m: int(t) for m, t in sample_totals.items() if int(t) < min_total}
        if starved:
            detail = "，".join(f"{m} {t} 次" for m, t in sorted(starved.items()))
            report.results.append(
                RuleResult(
                    rule_id="P3-probe-sample-size",
                    passed=False,
                    severity=WARNING,
                    message=f"样本量不足（建议每种模式 ≥ {min_total} 次）：{detail}"
                            "—— 分位数按 nearest-rank 取值，样本越少分位数越贴近最大值"
                            "（N ≤ 20 时「P95」就是最大值），本轮耗时结论不足以支撑判定",
                    observed=starved,
                    expected=min_total,
                    action="加长时长或加大并发（如 `--duration 60`）后再看结论；"
                           "本轮的数字可以记录趋势，但不要用它下「n1 比 agg 慢多少倍」的结论。",
                )
            )

    if "n1" in report_p95 and max_p95 is not None:
        observed = report_p95["n1"]
        caveat = ""
        if "n1" in starved:
            n_n1 = starved["n1"]
            # 措辞要看样本数：N ≤ 20 时 nearest-rank 的 P95 确实就是最大值，
            # 样本多一些时只是"贴近最大值"。两种都必须说清"别当最终依据"。
            near = "该 P95 实为最大值" if n_n1 <= 20 else "分位数已明显贴近最大值"
            caveat = (f"；⚠ 本轮 n1 只有 {n_n1} 个样本（建议 ≥ {min_total}），"
                      f"{near}，此判定不可作为最终依据")
        report.results.append(
            RuleResult(
                rule_id="P1-n1-probe",
                passed=observed <= max_p95,
                severity=BLOCKING,
                message=f"N+1 报表 P95 {observed:.1f}ms（阈值 {max_p95}ms）{caveat}",
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


def evaluate_suite(passed: int, failed: int, errors: int, flaky: list[dict], rules: dict,
                   exit_code: int | None = None) -> GateReport:
    """功能用例门禁：通过率 + flaky 用例数量 + 子进程退出码。

    ## 0 个用例不能算 100% 通过

    从前这里是 `rate = (passed / total) if total else 1.0`：一个用例都没跑到
    （环境缺依赖全跳过、pytest 收集失败、目标路径写错）时，报告打印
    `通过率 100.00%（0/0）` 并放行 —— **最难发现的失败被写成了满分**。
    0/0 既不是 100% 也不是 0%，它是"无法判定"。
    现在这种情形产出 `skipped=True` 的结果：看得见，但不进 blocked
    （"没跑到用例"不该被记成"用例失败"，那是另一回事，也由别的规则去说）。

    ## exit_code 是账本对不对得上的最后一道检查

    `passed/failed/errors` 是从 `cases` 表里数出来的，而写表的是
    pytest 插件 —— 插件没装上、`-p` 参数写错、收集阶段就崩了，
    表里都会是 0，门禁会安静地放行。所以子进程的退出码必须进判定：
    pytest 的 0/1 分别对应"全过/有用例失败"（已由 S1 覆盖），
    2/3/4/5 是中断、内部错误、用法错误、**没收集到任何用例** —— 都不该放行。
    """
    report = GateReport()
    suite = rules.get("suite", {})
    total = passed + failed + errors

    min_rate = suite.get("min_pass_rate", 1.0)
    if total == 0:
        report.results.append(
            RuleResult(
                rule_id="S1-pass-rate",
                passed=False,
                severity=WARNING,
                skipped=True,
                message=f"没有跑到任何用例，通过率无法判定（不是 100%，也不是 0%）：{passed}/{total}",
                observed=None,
                expected=min_rate,
                action="先确认用例是不是真的被收集到了（`-p metrics.pytest_plugin` 是否生效、目标路径是否正确）。"
                       "把 0/0 当通过，等于给「环境没准备好」发了一张通行证。",
            )
        )
    else:
        rate = passed / total
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

    if exit_code is not None:
        # pytest 退出码：0 全过、1 有用例失败（S1 已覆盖）、
        # 2 中断、3 内部错误、4 用法错误、5 没收集到用例。
        # 另外：pytest 说"有用例失败"（1）但表里一条失败都没记下来 —— 账本坏了，
        # 也不能放行。
        ledger_says_clean = (failed + errors) == 0
        broken = exit_code not in (0, 1) or (exit_code == 1 and ledger_says_clean)
        report.results.append(
            RuleResult(
                rule_id="S3-pytest-exit",
                passed=not broken,
                severity=BLOCKING,
                message=f"pytest 退出码 {exit_code}（0=全过，1=有用例失败）"
                        + ("；注意：退出码为 0/1 之外说明本轮**没有正常跑完**。" if broken else ""),
                observed=exit_code,
                expected="0 或 1",
                action="退出码 5 通常是没收集到用例（路径/插件配置问题），"
                       "2 是中断（超时或 Ctrl-C），3 是 pytest 内部错误，4 是命令行用法错误。"
                       "这些都不代表「代码有问题」，但都代表「本轮结论无效」，必须查清楚再放行。",
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
