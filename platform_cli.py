"""CLI：压测执行、结果入库、门禁判定、看板导出。

## 设计上刻意做的事

* **跑压测和设基线是两条命令。** `run` 永远不会改基线，
  只能由 `baseline set` 显式设定。这一条是为了防止
  "每次跑都把基线刷成最新值"这种让门禁失效的用法。
* **每次运行都产出一个 Markdown 报告文件**，路径打进 stdout。
  CI 里直接当 artifact 上传，人不用去数据库里翻。
* **退出码就是门禁结果。** 0 = 放行，1 = 拦住。CI 不需要解析文本。
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from bench.engine import Runner  # noqa: E402
from bench.scenarios import build_engine_mix, configure_item_range, engine_single  # noqa: E402
from metrics import analysis, gate, store  # noqa: E402
from sut.server_ctl import start_sut  # noqa: E402

REPORTS_DIR = ROOT / "reports"
DEFAULT_RULES = ROOT / "gate_rules.yaml"


# --------------------------------------------------------------------------- #
def platform_info() -> dict:
    return {
        "python": sys.version.split()[0],
        "platform": f"{platform.system()}-{platform.release()}",
        "cpu_count": os.cpu_count(),
        "fastapi": __import__("fastapi").__version__,
        "uvicorn": __import__("uvicorn").__version__,
        "requests": __import__("requests").__version__,
        "machine": platform.machine(),
    }


def new_run_id(prefix: str) -> str:
    return f"{prefix}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


def git_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() or None
    except Exception:  # noqa: BLE001 - 没有 git 仓库不是错误
        return None


# --------------------------------------------------------------------------- #
def cmd_run(args: argparse.Namespace) -> int:
    """跑一轮压测：梯度加压 → 入库 → 与基线比 → 门禁判定 → 出报告。"""
    store.init_db()
    label = args.label or f"{args.report_mode}-{args.items // 1000}k"
    run_id = new_run_id("perf")
    rules = gate.load_rules(args.rules)
    info = platform_info()
    config = {
        "items": args.items,
        "report_mode": args.report_mode,
        "scenario": args.scenario or "(mix)",
        "step_users": args.step_users,
        "step_duration_s": args.step_duration,
        "think_time_s": args.think_time,
    }
    store.upsert_run(run_id, "perf", label, config=config, platform_info=info,
                     git_sha=git_sha(), ci=bool(os.environ.get("CI")))

    print(f"[run] run_id={run_id} label={label}")
    print(f"[run] 配置 {json.dumps(config, ensure_ascii=False)}")
    handle = start_sut(report_mode=args.report_mode, items=args.items)
    print(f"[run] 被测服务 {handle.base_url} db={handle.db_path}")
    # 让场景按**实际灌入的物料条数**取随机 ID。不设置的话，
    # `--items` 小于默认的 20 000 时会有大批点查打到不存在的 ID，
    # 压测脚本自己凭空造出高错误率（实测 5 000 条时报出 28%）。
    configure_item_range(args.items)
    try:
        stats_by_step = []
        for users in args.step_users:
            if args.scenario:
                mix = engine_single(handle.base_url, args.scenario, timeout_s=args.timeout)
            else:
                mix = build_engine_mix(handle.base_url, timeout_s=args.timeout)
            runner = Runner(
                base_url=handle.base_url,
                scenarios=mix,
                users=users,
                duration_s=args.step_duration,
                think_time_s=args.think_time,
                ramp_up_s=min(2.0, args.step_duration / 4),
                timeout_s=args.timeout,
                warmup_drop_s=args.warmup_drop,
            )
            st = runner.run()
            stats_by_step.append(st)
            inflight_warn = ""
            if getattr(st, "max_inflight", 0) < users * 0.5:
                inflight_warn = "  ⚠ 实际在途远低于配置并发，瓶颈可能在压测机"
            print(
                f"[step] {users:>4}u  {st.duration_s:5.1f}s  n={st.total:<6} "
                f"rps={st.rps:8.2f}  p95={st.p95_ms:8.2f}ms  p99={st.p99_ms:8.2f}ms  "
                f"err={st.error_rate:.2%}  inflight={getattr(st, 'max_inflight', 0)}{inflight_warn}"
            )

        store.add_steps(run_id, stats_by_step)
        step_dicts = [s.as_dict() for s in stats_by_step]
        # 汇总口径必须和 `_merge_from_rows`（gate/看板走的那条路）一致。
        #
        # 这里原先用 `merge_stats`，它把所有梯度的**原始样本池化**后再算分位数。
        # 而 gate 拿不到原始样本，只能取各梯度的**最大值**。同一个 run
        # 于是有两个不同的 P95：实测 10u=83.8ms、20u=128.9ms 时，
        # run 报告 105.9ms（池化），gate 却按 128.9ms 判定。
        # 结果是**报告上的数字和门禁据以拦人的数字对不上** ——
        # 出了争议没人能解释到底哪个是真的。
        #
        # 现在统一取最差梯度：对门禁是安全方向（不会把劣化抹平），
        # 且 Report 与 Gate 必然自洽。
        merged = _merge_from_rows(step_dicts)

        knee = analysis.find_knee(step_dicts)
        baseline = store.get_baseline("perf", label)
        run_metrics = merged
        deltas = (
            analysis.compare_to_baseline(run_metrics, baseline["metrics"]) if baseline else []
        )

        report = gate.evaluate_perf(run_metrics, baseline, deltas, rules)
        store.add_gate_results(run_id, [r.as_dict() for r in report.results])
        status = "failed" if report.blocked else "passed"
        store.finish_run(run_id, status, {"knee": knee, **run_metrics})

        text = analysis.render_perf_report(run_id, label, step_dicts, deltas, knee, info, config)
        text += "\n\n" + report.render() + "\n"
        REPORTS_DIR.mkdir(exist_ok=True)
        out_path = REPORTS_DIR / f"{run_id}.md"
        out_path.write_text(text, encoding="utf-8")
        print(f"\n[report] {out_path}")
        print(report.render())
        if args.set_baseline:
            store.set_baseline("perf", label, run_id, run_metrics)
            print(f"[baseline] 已把本轮设为基线 kind=perf label={label} run_id={run_id}")
        return 1 if report.blocked else 0
    finally:
        handle.stop()


def cmd_probe(args: argparse.Namespace) -> int:
    """性能探针：把 agg 与 n1 两种实现在同一配置下各压一次，对比并门禁。"""
    store.init_db()
    rules = gate.load_rules(args.rules)
    run_id = new_run_id("probe")
    info = platform_info()
    store.upsert_run(run_id, "probe", args.label, config={"users": args.users,
                      "duration_s": args.duration, "items": args.items},
                     platform_info=info, git_sha=git_sha())

    result: dict[str, dict] = {}
    configure_item_range(args.items)
    try:
        for mode in ("agg", "n1"):
            handle = start_sut(report_mode=mode, items=args.items)
            print(f"[probe] mode={mode} {handle.base_url}")
            try:
                runner = Runner(
                    base_url=handle.base_url,
                    scenarios=engine_single(handle.base_url, "slow_report", timeout_s=args.timeout),
                    users=args.users,
                    duration_s=args.duration,
                    think_time_s=0.0,
                    ramp_up_s=1.0,
                    timeout_s=args.timeout,
                    warmup_drop_s=args.warmup_drop,
                )
                st = runner.run()
            finally:
                handle.stop()
            print(f"[probe] mode={mode} rps={st.rps:.2f} p95={st.p95_ms:.1f}ms "
                  f"p99={st.p99_ms:.1f}ms err={st.error_rate:.2%}")
            result[mode] = st.as_dict()
            st.label = f"slow_report-{mode}"
            store.add_steps(run_id, [st])

        p95 = {k: v["p95_ms"] for k, v in result.items()}
        report = gate.evaluate_probe(p95, rules)
        store.add_gate_results(run_id, [r.as_dict() for r in report.results])

        # 入库一份**扁平**的汇总指标，而不是 {"agg": {...}, "n1": {...}} 这种嵌套形状。
        #
        # 嵌套形状踩过一个坑：看板的 /api/trend 和 `platform_cli trend` 都按
        # `metrics["p95_ms"]` 这种扁平键取值，于是 probe 类型的运行在趋势里
        # 永远显示 0.00 —— 压测探针跑了很多轮，趋势页上却什么都看不到。
        # **写入形状和读取形状必须一致**，否则失败是静默的。
        #
        # 汇总口径与 perf 运行保持一致（分位数取最差的一级），
        # 每种模式的明细仍保留在 `modes` 下供查阅。
        merged = _merge_from_rows([result[m] for m in ("agg", "n1") if m in result])
        merged["modes"] = {
            m: {k: v[k] for k in ("rps", "mean_ms", "p95_ms", "p99_ms", "max_ms",
                                  "error_rate", "total")}
            for m, v in result.items()
        }
        store.finish_run(run_id, "failed" if report.blocked else "passed", merged)

        lines = [
            "# 性能探针报告 · 报表接口 agg vs n1",
            "",
            f"- run_id: `{run_id}`",
            f"- 并发 {args.users} / 时长 {args.duration}s / 数据量 {args.items} 条",
            "",
            "| 实现 | RPS | 平均 | P95 | P99 | Max | 错误率 |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
        for mode in ("agg", "n1"):
            v = result[mode]
            lines.append(
                f"| {mode} | {v['rps']:.2f} | {v['mean_ms']:.1f} | {v['p95_ms']:.1f} |"
                f" {v['p99_ms']:.1f} | {v['max_ms']:.1f} | {v['error_rate']:.2%} |"
            )
        ratio = p95.get("n1", 0) / max(p95.get("agg", 0.1), 0.1)
        lines += [
            "",
            f"**P95 倍数：{ratio:.1f}×** —— 同样的输出，一次 SQL 聚合和 40 次循环查询的差距。",
            "",
            "> 这两个实现都在 `sut/app.py` 里，用 `PERF_SUT_REPORT_MODE` 切换。",
            "> n1 不是「为了压测造的假代码」，它是真实系统里最常见的性能缺陷形态。",
        ]
        REPORTS_DIR.mkdir(exist_ok=True)
        out_path = REPORTS_DIR / f"{run_id}.md"
        out_path.write_text("\n".join(lines) + "\n\n" + report.render() + "\n", encoding="utf-8")
        print(f"\n[report] {out_path}")
        print(report.render())
        return 1 if report.blocked else 0
    except Exception:
        store.finish_run(run_id, "error")
        raise


def cmd_suite(args: argparse.Namespace) -> int:
    """跑被测服务/平台自身的功能用例，结果入库，再走一遍套件门禁。

    这里刻意**不起被测服务**：功能用例要能在没有服务的机器上跑出"全部跳过"
    而不是"全部报错"。跳过和失败在门禁里的含义完全不同 ——
    把"环境没准备好"记成失败，会让通过率变得不可信。
    """
    store.init_db()
    rules = gate.load_rules(args.rules)
    run_id = new_run_id("suite")
    info = platform_info()
    store.upsert_run(run_id, "suite", args.label, config={"target": args.target},
                     platform_info=info, git_sha=git_sha(), ci=bool(os.environ.get("CI")))

    env = os.environ.copy()
    env["QUALITY_RUN_ID"] = run_id
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    cmd = [sys.executable, "-m", "pytest", args.target, "-p", "metrics.pytest_plugin", "-q"]
    if args.extra:
        cmd.extend(args.extra)
    print(f"[suite] run_id={run_id} {args.target}")
    proc = subprocess.run(cmd, cwd=str(ROOT), env=env, text=True, encoding="utf-8", errors="replace")

    summary = _case_summary(run_id)
    flaky = _flaky_cases()
    report = gate.evaluate_suite(summary["passed"], summary["failed"], summary["errors"], flaky, rules)
    store.add_gate_results(run_id, [r.as_dict() for r in report.results])
    status = "failed" if report.blocked else "passed"
    store.finish_run(run_id, status, summary)
    print(f"\n[suite] 通过 {summary['passed']} / 失败 {summary['failed']} / 报错 {summary['errors']}"
          f" / 跳过 {summary['skipped']}")
    print(f"[suite] flaky 用例 {len(flaky)} 条")
    print(report.render())
    return 1 if report.blocked else 0


def _case_summary(run_id: str) -> dict:
    with store.tx() as conn:
        rows = conn.execute("SELECT outcome, COUNT(*) c FROM cases WHERE run_id=? GROUP BY outcome",
                            (run_id,)).fetchall()
    counts = {r["outcome"]: r["c"] for r in rows}
    passed = counts.get("passed", 0)
    failed = counts.get("failed", 0)
    errors = counts.get("error", 0)
    total = passed + failed + errors
    return {
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "skipped": counts.get("skipped", 0),
        "total": total,
        "pass_rate": round(passed / total, 4) if total else 1.0,
    }


def _flaky_cases() -> list[dict]:
    out = []
    for nodeid in store.all_nodeids():
        v = analysis.analyze_flaky(store.cases_for_node(nodeid), nodeid)
        if v.flaky:
            out.append(v.__dict__)
    return out


def cmd_flaky(args: argparse.Namespace) -> int:
    """列出 flaky 用例。退出码非 0 表示超过了预算（可直接给 CI 用）。"""
    rules = gate.load_rules(args.rules)
    budget = getattr(args, "budget", None)
    if budget is None:
        budget = rules.get("suite", {}).get("max_flaky_cases")
    flaky = _flaky_cases()
    if not flaky:
        print("没有达到 flaky 判定的用例。")
        print(f"（判据：执行 ≥ {analysis.FLAKY_MIN_RUNS} 次、结果在通过/失败之间摇摆、"
              f"稳定度 < {analysis.FLAKY_STABILITY_MAX:.0%}）")
        return 0
    print(f"{'用例':<58}{'执行':>5}{'通过':>6}{'失败':>6}{'稳定度':>9}  说明")
    for v in sorted(flaky, key=lambda x: x["stability"]):
        print(f"{v['nodeid']:<58}{v['runs']:>5}{v['passed']:>6}{v['failed']:>6}"
              f"{v['stability']:>9.0%}  {v['note']}")
    over = budget is not None and len(flaky) > budget
    print(f"\n共 {len(flaky)} 条 flaky" + (f"（预算 {budget} 条）" if budget is not None else ""))
    if over:
        print("超过 flaky 预算。flaky 用例会训练团队忽略红灯：要么修好，要么删掉。")
    return 1 if over else 0

def cmd_gate(args: argparse.Namespace) -> int:
    """对一个已经入库的 run 重新跑门禁（用于「改阈值后复盘」）。"""
    rules = gate.load_rules(args.rules)
    rows = store.steps_for_run(args.run_id)
    if not rows:
        print(f"no steps for run {args.run_id}", file=sys.stderr)
        return 2
    step_dicts = [dict(r) for r in rows]
    merged = _merge_from_rows(step_dicts)
    label = _label_of(args.run_id)
    baseline = store.get_baseline("perf", label) if label else None
    deltas = analysis.compare_to_baseline(merged, baseline["metrics"]) if baseline else []
    report = gate.evaluate_perf(merged, baseline, deltas, rules)
    store.add_gate_results(args.run_id, [r.as_dict() for r in report.results])
    print(f"run_id={args.run_id} label={label}")
    print(report.render())
    return 1 if report.blocked else 0


def _label_of(run_id: str) -> str:
    with store.tx() as conn:
        row = conn.execute("SELECT label FROM runs WHERE run_id=?", (run_id,)).fetchone()
    return row["label"] if row else ""


def _merge_from_rows(rows: list[dict]) -> dict:
    """整轮汇总 —— 实现只有一处，在 `metrics.analysis.merge_step_rows`。

    这里保留一个薄别名，是为了不让调用点（`run`/`gate`/`probe`/`trend`）
    都去记住实现被搬到哪个模块了。**别名不是第二份实现**：
    它没有自己的逻辑，改口径只需要改 `analysis.py` 那一处。
    """
    return analysis.merge_step_rows(rows)


# --------------------------------------------------------------------------- #
def cmd_baseline(args: argparse.Namespace) -> int:
    if args.action == "set":
        runs = store.recent_runs(kind="perf", label=args.label, limit=1)
        if not runs:
            print(f"找不到 label={args.label} 的 perf 运行记录", file=sys.stderr)
            return 2
        run_id = args.run_id or runs[0]["run_id"]
        metrics = _run_metrics(run_id)
        if metrics is None:
            print(f"run {run_id} 没有可用的指标", file=sys.stderr)
            return 2
        store.set_baseline("perf", args.label, run_id, metrics)
        print(f"[baseline] kind=perf label={args.label} run_id={run_id}")
        print(f"           p95={metrics['p95_ms']}ms p99={metrics['p99_ms']}ms rps={metrics['rps']}")
        return 0
    if args.action == "show":
        b = store.get_baseline("perf", args.label)
        print(json.dumps(b, ensure_ascii=False, indent=2) if b else f"没有 label={args.label} 的基线")
        return 0
    if args.action == "clear":
        from metrics.store import tx

        with tx() as conn:
            conn.execute("DELETE FROM baselines WHERE kind='perf' AND label=?", (args.label,))
        print(f"[baseline] 已清除 label={args.label}")
        return 0
    return 2


def _run_metrics(run_id: str) -> dict | None:
    with store.tx() as conn:
        row = conn.execute("SELECT metrics_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        return None
    metrics = json.loads(row["metrics_json"])
    if not metrics:
        return None
    return metrics


def cmd_trend(args: argparse.Namespace) -> int:
    """趋势：把最近 N 轮的 P95/RPS 打出来，并给一个方向判断。"""
    # 不写死 kind='perf'：早先这里硬编码了 perf，于是 probe 类型的运行
    # 在趋势里永远查不到（跑了十几轮压测探针，趋势页却是空的）。
    # label 本身已经足够定位一组运行，kind 只当可选过滤。
    with store.tx() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT r.run_id, r.label, r.kind, r.started_at, r.status, r.metrics_json"
                " FROM runs r WHERE r.label=? AND r.metrics_json IS NOT NULL"
                " AND r.metrics_json != '{}'"
                " ORDER BY r.started_at DESC LIMIT ?",
                (args.label, args.limit),
            ).fetchall()
        ]
    if not rows:
        print(f"没有 label={args.label} 的历史记录")
        return 0
    rows.reverse()
    print(f"label={args.label} 最近 {len(rows)} 轮：")
    print(f"{'时间':<22}{'状态':<9}{'P95(ms)':>10}{'P99(ms)':>10}{'RPS':>10}{'错误率':>10}")
    series_p95: list[float] = []
    for r in rows:
        m = json.loads(r["metrics_json"] or "{}")
        if not m:
            continue
        series_p95.append(float(m.get("p95_ms", 0)))
        print(
            f"{r['started_at'][:19]:<22}{r['status']:<9}{m.get('p95_ms', 0):>10.2f}"
            f"{m.get('p99_ms', 0):>10.2f}{m.get('rps', 0):>10.2f}{m.get('error_rate', 0):>10.2%}"
        )
    print(f"\nP95 方向：{analysis.moving_direction(series_p95)}")
    return 0


def cmd_noise(args: argparse.Namespace) -> int:
    """噪声研究：同一配置连跑 N 轮，量出这个环境**自己的**波动幅度。

    ## 为什么必须有这个命令

    门禁 `R3` 用 3% 作为"算不算劣化"的容忍带。这个 3% 最初是**拍的**。

    实测立刻打脸：同一份代码、同一配置、连跑两轮，
    P95 从 105.9ms 变到 110.2ms（+4.1%），门禁判「拦住」。
    **这是一次假阳性** —— 没有任何代码变慢，只是本机噪声。

    容忍带不是一个可以凭常识填的常数：它取决于机器负载、
    被测服务的锁竞争、JIT/缓存预热等，**每个环境都不一样**。
    正确的做法是在**部署这个平台的那台机器上**实测出来。

    这个命令就是把"拍一个数字"换成"量一个数字"。
    """
    store.init_db()
    print(f"[noise] label={args.label} 连跑 {args.rounds} 轮，每轮 {args.step_users} 梯度 "
          f"各 {args.step_duration}s，数据量 {args.items}")
    series: list[float] = []
    for i in range(1, args.rounds + 1):
        run_id = new_run_id("noise")
        store.upsert_run(run_id, "perf", args.label, status="running", ci=bool(os.environ.get("CI")))
        handle = start_sut(report_mode="agg", items=args.items)
        configure_item_range(args.items)
        try:
            stats_by_step = []
            for users in args.step_users:
                runner = Runner(
                    base_url=handle.base_url,
                    scenarios=build_engine_mix(handle.base_url, timeout_s=args.timeout),
                    users=users,
                    duration_s=args.step_duration,
                    warmup_drop_s=args.warmup_drop,
                    timeout_s=args.timeout,
                )
                stats_by_step.append(runner.run())
            store.add_steps(run_id, stats_by_step)
            merged = _merge_from_rows([s.as_dict() for s in stats_by_step])
            store.finish_run(run_id, "passed", merged)
            series.append(float(merged["p95_ms"]))
            print(f"[noise] 第 {i} 轮 p95={merged['p95_ms']:.1f}ms rps={merged['rps']:.1f} "
                  f"err={merged['error_rate']:.2%}")
        finally:
            handle.stop()

    if len(series) < 2:
        print("[noise] 至少需要 2 轮才能给结论", file=sys.stderr)
        return 2

    lo, hi = min(series), max(series)
    spread_pct = (hi - lo) / lo * 100
    mean = sum(series) / len(series)
    # 用"最大偏离均值"而不是"极差"，因为容忍带是**对基线**比较的：
    # 基线是某一轮的实测值，它本身离真值就有偏差。
    worst = max(abs(x - mean) / mean for x in series) * 100
    print(f"\n[noise] 各轮 P95：{[round(x, 1) for x in series]}")
    print(f"[noise] min={lo:.1f} max={hi:.1f} 均值={mean:.1f}ms")
    print(f"[noise] 极差 = {spread_pct:.2f}%（相对最小值）")
    print(f"[noise] 单轮最大偏离均值 = {worst:.2f}%")
    print()
    if spread_pct > 15:
        print(f"[noise] ⚠ 波动 {spread_pct:.1f}% 偏大：这台机器不适合做精细的性能门禁，"
              f"先排查是否有其他进程在抢 CPU。")
    suggested = max(5.0, round(worst * 2 / 5) * 5)
    print(f"[noise] 建议 baseline.degradation_tolerance_pct ≥ {suggested:.0f}"
          f"（约为实测最大偏离 {worst:.1f}% 的 2 倍，留出余量）")
    print(f"[noise] 当前 gate_rules.yaml 的值请对照 docs/度量口径.md 自行调整；"
          f"本命令**不自动改配置**。")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """起看板服务。"""
    import uvicorn

    os.environ.setdefault("QUALITY_DB", str(store.db_path()))
    uvicorn.run("dashboard.app:app", host=args.host, port=args.port, log_level="warning",
                app_dir=str(ROOT))
    return 0


# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="platform_cli", description="性能基线 + 质量门禁平台")
    p.add_argument("--rules", default=str(DEFAULT_RULES))
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="跑一轮梯度压测并判定门禁")
    r.add_argument("--label", default=None)
    r.add_argument("--items", type=int, default=20_000, help="被测服务数据量")
    r.add_argument("--report-mode", default="agg", choices=["agg", "n1"])
    r.add_argument("--scenario", default=None, help="只压单个场景（拐点定位用）")
    r.add_argument("--step-users", type=int, nargs="+", default=[10, 20, 40, 80])
    r.add_argument("--step-duration", type=float, default=8.0, help="每个梯度的稳态时长(秒)")
    r.add_argument("--think-time", type=float, default=0.0)
    r.add_argument("--timeout", type=float, default=10.0)
    r.add_argument("--warmup-drop", type=float, default=1.5, help="丢弃前 N 秒热身样本")
    r.add_argument("--set-baseline", action="store_true", help="把本轮显式设为基线")
    r.set_defaults(func=cmd_run)

    pr = sub.add_parser("probe", help="性能探针：agg vs n1")
    pr.add_argument("--label", default="probe-slow-report")
    pr.add_argument("--items", type=int, default=20_000)
    pr.add_argument("--users", type=int, default=10)
    pr.add_argument("--duration", type=float, default=8.0)
    pr.add_argument("--timeout", type=float, default=30.0)
    pr.add_argument("--warmup-drop", type=float, default=1.0)
    pr.set_defaults(func=cmd_probe)

    b = sub.add_parser("baseline", help="基线管理")
    b.add_argument("action", choices=["set", "show", "clear"])
    b.add_argument("--label", required=True)
    b.add_argument("--run-id", default=None)
    b.set_defaults(func=cmd_baseline)

    t = sub.add_parser("trend", help="趋势")
    t.add_argument("--label", required=True)
    t.add_argument("--limit", type=int, default=10)
    t.set_defaults(func=cmd_trend)

    g = sub.add_parser("gate", help="对已入库的 run 重新判定门禁")
    g.add_argument("--run-id", required=True)
    g.set_defaults(func=cmd_gate)

    su = sub.add_parser("suite", help="跑功能用例并入库、判定套件门禁")
    su.add_argument("--label", default="platform-tests")
    su.add_argument("--target", default="tests", help="pytest 目标路径")
    su.add_argument("extra", nargs="*", help="透传给 pytest 的额外参数")
    su.set_defaults(func=cmd_suite)

    fl = sub.add_parser("flaky", help="列出 flaky 用例")
    fl.add_argument("--budget", type=int, default=None)
    fl.set_defaults(func=cmd_flaky)

    ns = sub.add_parser("noise", help="量出本机的运行间波动，用来定容忍带")
    ns.add_argument("--label", default="noise-study")
    ns.add_argument("--items", type=int, default=20_000)
    ns.add_argument("--rounds", type=int, default=5, help="连跑几轮（建议 ≥5）")
    ns.add_argument("--step-users", type=int, nargs="+", default=[10, 20])
    ns.add_argument("--step-duration", type=float, default=6.0)
    ns.add_argument("--warmup-drop", type=float, default=1.5)
    ns.add_argument("--timeout", type=float, default=10.0)
    ns.set_defaults(func=cmd_noise)

    s = sub.add_parser("serve", help="起看板")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.set_defaults(func=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
