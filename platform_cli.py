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
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from bench.engine import Runner  # noqa: E402
from bench.scenarios import build_engine_mix, configure_item_range, engine_single  # noqa: E402
from metrics import analysis, baseline, gate, store  # noqa: E402
from sut.server_ctl import start_sut  # noqa: E402

REPORTS_DIR = ROOT / "reports"
BASELINE_DIR = ROOT / "baselines"
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
# 环境分层（dev / staging / prod）
# --------------------------------------------------------------------------- #
ENV_PROFILE_VAR = "PERF_ENV"
ENV_PROFILE_DIR = ROOT / "scripts" / "envs"

# 只允许「安全档案名」：档案名会被拼进文件路径，不校验就等于开了一个
# 路径穿越（`PERF_ENV=../../../etc/passwd` 会让平台去读仓库外的文件）。
_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

# 这些值表示"不套任何档案"。与"没设 PERF_ENV"是同一件事 ——
# 必须是显式的空值语义，否则 CI 里传一个空字符串会导致找不到档案而报错。
_PROFILE_OFF = {"", "off", "none", "0", "false"}


def _parse_env_file(path: Path) -> dict[str, str]:
    """解析 KEY=VALUE 档案。只支持这一种语法，刻意不实现 shell 展开。

    理由：档案是**配置**，不是脚本。允许 `$(...)`、`${VAR}` 之类的东西，
    就等于允许"同一条命令在不同机器上跑出不同环境"，而这个仓库的
    整个卖点是数字可复现。值里允许出现 `=`（只按第一个 = 切分）。
    """
    values: dict[str, str] = {}
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"{path.name} 第 {lineno} 行不是 KEY=VALUE：{raw!r}")
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def apply_env_profile(argv: list[str] | None = None) -> str | None:
    """按 `PERF_ENV` 套用环境档案；返回生效的档案名（没套则 None）。

    ## 默认行为必须与引入本函数之前**完全一致**

    没设 `PERF_ENV`（或设成 off/none/0/false）时本函数**直接返回**，
    一个环境变量都不读也不写。CI 的三个 job 因此不需要任何改动，
    本地 `pytest tests/ -q` 的行为也与从前逐字节相同。

    ## 不覆盖已显式设置过的变量

    档案里的一份值是"团队约定"，而命令行上 `$env:PERF_SUT_ITEMS="500"`
    是"我这一次就要这个数"。后者意图更强，所以只做 `setdefault` 语义。
    这条也让测试天然安全：conftest 用 monkeypatch 设的 `QUALITY_DB`
    指向临时目录，套档案也覆盖不掉它，用例不会去写开发者的真实结果库。

    ## 为什么不放 argparse 参数

    因为档案要影响的是**子进程**的环境（SUT 是 `subprocess` 起的），
    而不是 argv 解析结果。走环境变量是唯一能让 `sut.server_ctl` 里
    那份 `os.environ.copy()` 自动继承到的方式。
    """
    name = os.environ.get(ENV_PROFILE_VAR, "").strip()
    if name.lower() in _PROFILE_OFF:
        return None

    if not _PROFILE_NAME_RE.match(name):
        raise SystemExit(
            f"{ENV_PROFILE_VAR}={name!r} 不是合法的档案名"
            f"（只允许字母/数字/下划线/连字符，长度 1-32）。"
        )

    path = ENV_PROFILE_DIR / f"{name}.env"
    if not path.is_file():
        available = sorted(p.stem for p in ENV_PROFILE_DIR.glob("*.env")) if ENV_PROFILE_DIR.is_dir() else []
        raise SystemExit(
            f"找不到环境档案：{path}\n"
            f"可用档案：{', '.join(available) or '（无）'}\n"
            f"档案目录：{ENV_PROFILE_DIR}"
        )

    applied: list[str] = []
    for key, value in _parse_env_file(path).items():
        if key in os.environ:
            continue  # 显式设置优先，见 docstring
        os.environ[key] = value
        applied.append(key)

    print(f"[env] profile={name} 生效 {len(applied)} 项：{', '.join(applied) or '（无）'}")
    return name
# --------------------------------------------------------------------------- #
# 结构化输出
# --------------------------------------------------------------------------- #
JSON_FLAG_HELP = "以 JSON 输出结果（给 CI/工单/PR 评论消费）"


def add_json_flags(parser: argparse.ArgumentParser) -> None:
    """给子命令挂上结构化输出开关。

    ## 为什么必须有这个

    平台有 9 个子命令，改造前**只有 `baseline show` 输出 JSON**，
    其余全是给人看的表格。而 README 自己写过一句话：
    「只会 `exit 1` 的门禁，在团队里的寿命大约是两周」。

    退出码能让流水线红，但**不能告诉 PR 该改什么**。
    想让门禁结论进工单/PR 评论（这才是一个门禁在团队里活下来的方式），
    就必须有机器可读的结构化结果。
    """
    parser.add_argument("--json", action="store_true", help=JSON_FLAG_HELP)
    parser.add_argument("--json-out", default=None,
                        help="把结构化结果写到指定文件（同时仍可 --json 打到 stdout）")


def emit(args: argparse.Namespace, payload: dict) -> None:
    """按开关把结构化结果打到 stdout 和/或文件。

    文件写法固定 `ensure_ascii=False` + UTF-8：报告里有中文，
    写成 ASCII 转义后人在工单里读不了。
    """
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if getattr(args, "json_out", None):
        path = Path(args.json_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        if not getattr(args, "json", False):
            print(f"[json] 已写入 {path}")
    if getattr(args, "json", False):
        print(text)


def _delta_dicts(deltas: list) -> list[dict]:
    return [
        {
            "metric": d.metric,
            "baseline": d.baseline,
            "current": d.current,
            "delta_pct": None if d.delta_pct == float("inf") else d.delta_pct,
            "tolerance_pct": d.tolerance_pct,
            "degraded": d.degraded,
            "improved": d.improved,
            "text": d.pct_text(),
        }
        for d in deltas
    ]


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
        # 尾部 p95/p99 成对取自比值最大的那一级，其余分位取最差梯度：对门禁是
        # 安全方向（不会把劣化抹平），且 Report 与 Gate 必然自洽。
        merged = _merge_from_rows(step_dicts)

        knee = analysis.find_knee(step_dicts)
        baseline = store.get_baseline("perf", label)
        run_metrics = merged
        deltas = (
            analysis.compare_to_baseline(run_metrics, baseline["metrics"], rules) if baseline else []
        )
        # 基线可比性：口径不同（数据量/并发/场景/报表实现）时，劣化百分比没有意义。
        comparability = (
            analysis.check_comparability(config, baseline.get("config"), rules) if baseline else None
        )

        report = gate.evaluate_perf(run_metrics, baseline, deltas, rules, comparability,
                                    r3_advisory=args.r3_advisory)
        store.add_gate_results(run_id, [r.as_dict() for r in report.results])
        status = "failed" if report.blocked else "passed"
        store.finish_run(run_id, status, {"knee": knee, **run_metrics})

        text = analysis.render_perf_report(run_id, label, step_dicts, deltas, knee, info, config,
                                           comparability, rules)
        text += "\n\n" + report.render() + "\n"
        REPORTS_DIR.mkdir(exist_ok=True)
        out_path = REPORTS_DIR / f"{run_id}.md"
        out_path.write_text(text, encoding="utf-8")
        print(f"\n[report] {out_path}")
        print(report.render())
        if args.set_baseline:
            store.set_baseline("perf", label, run_id, run_metrics, config=config)
            print(f"[baseline] 已把本轮设为基线 kind=perf label={label} run_id={run_id}")
        emit(args, {
            "run_id": run_id,
            "kind": "perf",
            "label": label,
            "status": status,
            "blocked": report.blocked,
            "report_path": str(out_path),
            "config": config,
            "metrics": run_metrics,
            "steps": step_dicts,
            "knee": knee,
            "baseline_run_id": baseline["run_id"] if baseline else None,
            "deltas": _delta_dicts(deltas),
            "rules": [r.as_dict() for r in report.results],
            "set_baseline": bool(args.set_baseline),
        })
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
    rows_by_mode: dict[str, list[dict]] = {}
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
                # 取一次响应体用于**数据一致性校验**。
                #
                # 这里原先只取耗时（`st.as_dict()`），从不看返回了什么 ——
                # 于是"两种实现语义等价"这个性能对比的前提，在主路径上
                # 从来没被验证过，只在单测里成立。而这个前提恰恰是本项目
                # 最有价值的发现（聚合扇出 SUM(stock) 虚高 2.23 倍）的来源。
                rows_by_mode[mode] = _fetch_report_rows(handle.base_url, args.timeout)
            finally:
                handle.stop()
            print(f"[probe] mode={mode} rps={st.rps:.2f} p95={st.p95_ms:.1f}ms "
                  f"p99={st.p99_ms:.1f}ms err={st.error_rate:.2%}")
            result[mode] = st.as_dict()
            st.label = f"slow_report-{mode}"
            store.add_steps(run_id, [st])

        p95 = {k: v["p95_ms"] for k, v in result.items()}
        # 每种模式实际采到多少个请求。`bench/engine.py` 的分位数是 nearest-rank：
        # N < 20 时「P95」取的就是最大值 —— CI 里 n1 只跑了约 10 个样本，
        # 打印出来的 p95 与 p99 是同一个数字（3828.6ms），那不是长尾集中，
        # 是分位数退化了。而 P1-n1-probe 是 **BLOCKING** 规则。
        #
        # 所以样本量必须一起进门禁。做法与 R4 保持一致（WARNING，不翻结论），
        # 见 metrics/gate.py 的 evaluate_probe。
        sample_totals = {m: int(v.get("total", 0)) for m, v in result.items()}
        # P0 先跑：数据不一致时，"谁快"这个问题本身没有意义，
        # 所以它必须排在 P1/P2 之前，且在报告里要能一眼看到。
        data_report = gate.evaluate_probe_data(rows_by_mode.get("agg"), rows_by_mode.get("n1"))
        report = gate.merge(data_report, gate.evaluate_probe(p95, rules, sample_totals))
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
        p0 = next((r for r in report.results if r.rule_id == "P0-probe-data-equivalence"), None)
        if p0 is not None:
            # skipped 单独一个记号：它既不是"一致"（✅）也不是"不一致"（❌），
            # 而是"本轮没比"——三态必须分开，否则没校验会被读成校验通过。
            mark = "⏭" if p0.skipped else ("✅" if p0.passed else "❌")
            lines += [
                "",
                "## 前提校验：两种实现的数据是否一致",
                "",
                f"{mark} {p0.message}",
                "",
                "> 这一条是**速度对比的前提**，不是附加项。本项目真实踩过：",
                "> 聚合扇出让 `SUM(stock)` 虚高 2.23 倍，而它不报错、不慢、"
                "功能用例全绿 —— 只有把两种实现的输出逐字段比对才能发现。",
                "> 数据不一致时，上面那个倍数不该被引用。",
            ]
        REPORTS_DIR.mkdir(exist_ok=True)
        out_path = REPORTS_DIR / f"{run_id}.md"
        out_path.write_text("\n".join(lines) + "\n\n" + report.render() + "\n", encoding="utf-8")
        print(f"\n[report] {out_path}")
        print(report.render())
        emit(args, {
            "run_id": run_id,
            "kind": "probe",
            "label": args.label,
            "status": "failed" if report.blocked else "passed",
            "blocked": report.blocked,
            "report_path": str(out_path),
            "config": {"users": args.users, "duration_s": args.duration, "items": args.items},
            "modes": merged.get("modes"),
            "p95_ratio": round(ratio, 3),
            "metrics": merged,
            "rules": [r.as_dict() for r in report.results],
        })
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
    flaky = _flaky_cases(rules)
    # 退出码必须进判定。passed/failed/errors 是从 cases 表里数出来的，
    # 而写表的是 pytest 插件：插件没生效、`-p` 参数写错、收集阶段就崩了，
    # 表里都会是 0，光看表会安静地放行（历史上正是如此 —— 一个用例都没跑，
    # 却能打印「通过率 100.00%（0/0）」）。
    # pytest 退出码：0 全过、1 有用例失败、2 中断、3 内部错误、
    # 4 用法错误、5 没收集到任何用例。后四种都不代表代码有问题，
    # 但都代表「本轮结论无效」，不能放行。
    report = gate.evaluate_suite(summary["passed"], summary["failed"], summary["errors"],
                                 flaky, rules, exit_code=proc.returncode)
    store.add_gate_results(run_id, [r.as_dict() for r in report.results])
    status = "failed" if report.blocked else "passed"
    store.finish_run(run_id, status, summary)
    print(f"\n[suite] 通过 {summary['passed']} / 失败 {summary['failed']} / 报错 {summary['errors']}"
          f" / 跳过 {summary['skipped']}")
    print(f"[suite] flaky 用例 {len(flaky)} 条")
    print(report.render())
    emit(args, {
        "run_id": run_id,
        "kind": "suite",
        "label": args.label,
        "status": status,
        "blocked": report.blocked,
        "target": args.target,
        "pytest_exit_code": proc.returncode,
        "summary": summary,
        "flaky": flaky,
        "rules": [r.as_dict() for r in report.results],
    })
    # 退出码由 S3-pytest-exit 参与判定，所以这里不必再单独看 proc.returncode：
    # 退出码不是 0/1、或"pytest 说失败了却一条都没记进表"时，S3 是 blocking 的，
    # report.blocked 已经为真。
    return 1 if report.blocked else 0


def _fetch_report_rows(base_url: str, timeout_s: float) -> list[dict] | None:
    """取一次探针接口的响应体，用于数据一致性校验。

    取不到（网络抖动/服务已停）时返回 None 而不是抛异常：
    探针的**主要**产物是耗时，不该因为一次取数失败就整轮作废。
    但返回 None **不等于**"两种实现的数据一致"——`evaluate_probe_data`
    收到 None 时会产出一条 `skipped=True` 的 P0 结果，在报告里标成
    `[SKIP]` 明说"本轮没校验"（既不静默消失，也不把 CI 判红）。
    """
    import requests

    try:
        resp = requests.get(f"{base_url}/api/reports/slow-by-supplier",
                            params={"window_days": 30}, timeout=timeout_s)
        resp.raise_for_status()
        return resp.json().get("rows", [])
    except Exception as exc:  # noqa: BLE001 - 取数失败不该让整轮探针崩掉
        print(f"[probe] ⚠ 数据一致性校验取数失败（{exc}）；本轮 P0 会以 [SKIP] 出现在报告里",
              file=sys.stderr)
        return None


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
        # 0 个用例时既不是 100% 也不是 0%，而是「无法判定」——
        # 从前这里回落到 1.0，等于把"一个用例都没跑到"写成满分。
        "pass_rate": round(passed / total, 4) if total else None,
    }


def _flaky_cases(rules: dict | None = None) -> list[dict]:
    out = []
    for nodeid in store.all_nodeids():
        v = analysis.analyze_flaky(store.cases_for_node(nodeid), nodeid, rules)
        if v.flaky:
            out.append(v.__dict__)
    return out


def cmd_flaky(args: argparse.Namespace) -> int:
    """列出 flaky 用例。退出码非 0 表示超过了预算（可直接给 CI 用）。"""
    rules = gate.load_rules(args.rules)
    budget = getattr(args, "budget", None)
    if budget is None:
        budget = rules.get("suite", {}).get("max_flaky_cases")
    min_runs, stability_max = analysis.flaky_thresholds_from_rules(rules)
    flaky = _flaky_cases(rules)
    if not flaky:
        print("没有达到 flaky 判定的用例。")
        print(f"（判据：执行 ≥ {min_runs} 次、结果在通过/失败之间摇摆、"
              f"稳定度 < {stability_max:.0%}）")
        emit(args, {"flaky": [], "budget": budget, "over_budget": False,
                    "min_runs": min_runs,
                    "stability_max": stability_max})
        return 0
    print(f"{'用例':<58}{'执行':>5}{'通过':>6}{'失败':>6}{'稳定度':>9}  说明")
    for v in sorted(flaky, key=lambda x: x["stability"]):
        print(f"{v['nodeid']:<58}{v['runs']:>5}{v['passed']:>6}{v['failed']:>6}"
              f"{v['stability']:>9.0%}  {v['note']}")
    over = budget is not None and len(flaky) > budget
    print(f"\n共 {len(flaky)} 条 flaky" + (f"（预算 {budget} 条）" if budget is not None else ""))
    if over:
        print("超过 flaky 预算。flaky 用例会训练团队忽略红灯：要么修好，要么删掉。")
    emit(args, {
        "flaky": sorted(flaky, key=lambda x: x["stability"]),
        "budget": budget,
        "over_budget": over,
        "min_runs": min_runs,
        "stability_max": stability_max,
    })
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
    deltas = analysis.compare_to_baseline(merged, baseline["metrics"], rules) if baseline else []
    # 被门禁的那轮 run 自己的配置（不是当前命令行的配置）：这是唯一正确的比法，
    # 否则用「今天跑 gate 的环境」去比「当时那轮」的配置。
    comparability = (
        analysis.check_comparability(_run_config(args.run_id), baseline.get("config"), rules)
        if baseline else None
    )
    report = gate.evaluate_perf(merged, baseline, deltas, rules, comparability,
                                r3_advisory=args.r3_advisory)
    store.add_gate_results(args.run_id, [r.as_dict() for r in report.results])
    print(f"run_id={args.run_id} label={label}")
    print(report.render())
    emit(args, {
        "run_id": args.run_id,
        "label": label,
        "blocked": report.blocked,
        "metrics": merged,
        "baseline_run_id": baseline["run_id"] if baseline else None,
        "deltas": _delta_dicts(deltas),
        "rules": [r.as_dict() for r in report.results],
    })
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
        store.set_baseline("perf", args.label, run_id, metrics, config=_run_config(run_id))
        print(f"[baseline] kind=perf label={args.label} run_id={run_id}")
        print(f"           p95={metrics['p95_ms']}ms p99={metrics['p99_ms']}ms rps={metrics['rps']}")
        print(f"           下一步：`baseline export --label {args.label}` 把快照提交进仓库，"
              f"CI 才能拿到这条基线")
        emit(args, {"action": "set", "kind": "perf", "label": args.label,
                    "run_id": run_id, "metrics": metrics})
        return 0
    if args.action == "show":
        b = store.get_baseline("perf", args.label)
        print(json.dumps(b, ensure_ascii=False, indent=2) if b else f"没有 label={args.label} 的基线")
        emit(args, {"action": "show", "kind": "perf", "label": args.label, "baseline": b})
        return 0
    if args.action == "export":
        return _baseline_export(args)
    if args.action == "import":
        return _baseline_import(args)
    if args.action == "clear":
        from metrics.store import tx

        with tx() as conn:
            conn.execute("DELETE FROM baselines WHERE kind='perf' AND label=?", (args.label,))
        print(f"[baseline] 已清除 label={args.label}")
        emit(args, {"action": "clear", "kind": "perf", "label": args.label, "cleared": True})
        return 0
    return 2


def _default_snapshot_path(label: str) -> Path:
    return BASELINE_DIR / f"{label}.json"


def _baseline_export(args: argparse.Namespace) -> int:
    """把本机那条基线写成快照文件 —— 人做完这一步再 `git add` 提交。"""
    b = store.get_baseline("perf", args.label)
    if b is None:
        print(f"没有 label={args.label} 的基线可导出（先跑一轮并用 --set-baseline 设定）",
              file=sys.stderr)
        return 2
    # 快照记的必须是**量出这条基线的那台机器**，不是执行 export 的这台机器。
    # 典型场景：人把 runner 上传的库下载到开发机上导出快照 —— 那时本机与
    # "量出基线的机器"不是同一台，写错了会让后来读快照的人判断错方向。
    measured_on = _run_platform(b["run_id"]) or platform_info()
    snap = baseline.snapshot_of("perf", args.label, measured_on)
    if snap is None:  # pragma: no cover - 上面刚查到过，这里只为类型收口
        print(f"没有 label={args.label} 的基线可导出", file=sys.stderr)
        return 2
    path = Path(args.file) if args.file else _default_snapshot_path(args.label)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(baseline.dump(snap), encoding="utf-8")
    print(f"[baseline] 已导出快照 {path}")
    print(f"           run_id={snap['run_id']} set_at={snap['set_at']}")
    m = snap["metrics"]
    print(f"           p95={m.get('p95_ms')}ms p99={m.get('p99_ms')}ms rps={m.get('rps')}")
    print("           ⚠ 请把该文件提交进仓库，CI 的 `baseline import` 才拿得到它；"
          "CI 不会自己设基线")
    emit(args, {"action": "export", "kind": "perf", "label": args.label,
                "path": str(path), "snapshot": snap})
    return 0


def _baseline_import(args: argparse.Namespace) -> int:
    """从快照文件恢复基线。任何一步不对都如实报错（退出码非 0），不猜着读。"""
    path = Path(args.file) if args.file else _default_snapshot_path(args.label)
    try:
        snap = baseline.load_file(path)
        baseline.apply(snap, expect_label=args.label)
    except baseline.SnapshotError as exc:
        print(f"[baseline] 导入失败：{exc}", file=sys.stderr)
        print("[baseline] 基线没有恢复 —— 本轮 R3 会如实报「未设置基线」，"
              "不会假装比过了。", file=sys.stderr)
        return 2
    print(f"[baseline] 已从 {path} 恢复基线 label={args.label} run_id={snap['run_id']}")
    cfg = snap["config"]
    print(f"           快照配置 items={cfg.get('items')} report_mode={cfg.get('report_mode')} "
          f"step_users={cfg.get('step_users')}")
    bake = snap.get("platform") or {}
    if bake:
        # 如实印出来由人判断，而不是替人判成"不可比"：
        # 换机器绝对数字必然不同，那会把 CI 上的 R3 变成永久 SKIP。
        print(f"           该基线量于 {bake.get('platform')} / {bake.get('cpu_count')} 核；"
              f"本轮是本机 {platform_info().get('platform')} —— 机器不同时绝对值不可直接比")
    emit(args, {"action": "import", "kind": "perf", "label": args.label,
                "path": str(path), "run_id": snap["run_id"]})
    return 0


def _run_metrics(run_id: str) -> dict | None:
    with store.tx() as conn:
        row = conn.execute("SELECT metrics_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        return None
    metrics = json.loads(row["metrics_json"])
    if not metrics:
        return None
    return metrics


def _run_config(run_id: str) -> dict:
    """读一轮运行的被测配置快照（runs.config_json）。读不到就返回空 dict。"""
    with store.tx() as conn:
        row = conn.execute("SELECT config_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        return {}
    return json.loads(row["config_json"] or "{}")


def _run_platform(run_id: str) -> dict:
    """读一轮运行是**在哪台机器上**量出来的（runs.platform_json）。

    快照里的 `platform` 必须是这个，而不是"执行 export 的这台机器"：
    两者经常不是同一台 —— 本机基线在 runner 上重设、或把 runner 上传的
    库下载到开发机上导出，都会让"是哪台机器量的"这句话失真。
    读不到（老库没有这列内容）才退回本机。
    """
    with store.tx() as conn:
        row = conn.execute("SELECT platform_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        return {}
    return json.loads(row["platform_json"] or "{}") or {}


def cmd_trend(args: argparse.Namespace) -> int:
    """趋势：把最近 N 轮的 P95/RPS 打出来，并给一个方向判断。"""
    # 方向标的边界就是容忍带，必须和门禁读同一份规则；否则 YAML 改了容忍带，
    # 趋势页还在按模块常量说"持平"。
    rules = gate.load_rules(args.rules)
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
    points: list[dict] = []
    for r in rows:
        m = json.loads(r["metrics_json"] or "{}")
        if not m:
            continue
        series_p95.append(float(m.get("p95_ms", 0)))
        points.append({
            "run_id": r["run_id"], "kind": r["kind"], "status": r["status"],
            "started_at": r["started_at"],
            "p95_ms": m.get("p95_ms"), "p99_ms": m.get("p99_ms"),
            "rps": m.get("rps"), "error_rate": m.get("error_rate"),
        })
        print(
            f"{r['started_at'][:19]:<22}{r['status']:<9}{m.get('p95_ms', 0):>10.2f}"
            f"{m.get('p99_ms', 0):>10.2f}{m.get('rps', 0):>10.2f}{m.get('error_rate', 0):>10.2%}"
        )
    direction = analysis.moving_direction(series_p95, rules=rules)
    tol_pct = analysis.tolerance_from_rules(rules)[0]
    print(f"\nP95 方向：{direction}")
    emit(args, {
        "label": args.label,
        "points": points,
        "series_p95": series_p95,
        "p95_direction": direction,
        "tolerance_pct": tol_pct,
    })
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
    # 文案指向**真实存在**的键。
    #
    # 这里原先打印 `baseline.degradation_tolerance_pct` —— 一个
    # gate_rules.yaml 里根本没有的键（它全部 8 个键里没有这个）。
    # 用户照着提示去改，会找不到东西；而同一份 YAML 的头注释
    # 又写着「阈值集中在这一处，改标准改这里」。两句互相矛盾。
    print(f"[noise] 建议把 gate_rules.yaml 的 perf.tolerance.degradation_pct "
          f"设为 ≥ {suggested / 100:.2f}"
          f"（约为实测最大偏离 {worst:.1f}% 的 2 倍，留出余量）")
    print(f"[noise] 同时考虑 perf.tolerance.rps_degradation_pct（RPS 方向相反）。")
    print(f"[noise] 本命令**不自动改配置** —— 阈值必须由人定，"
          f"否则门禁会自己给自己发免死金牌。")
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    """清理 quality.db 里的历史运行并回收空间（默认保留每个 label 最近 200 轮）。

    ## 为什么这个命令必须存在

    平台的设计是「每次运行都入库 + 每次运行都出报告」（见本模块 docstring）。
    这是对的：只存成功记录的度量平台，通过率是假的。代价是 `quality.db`
    在 CI 上无限长大 —— 每个 job 都 insert，没有任何东西删。

    ## 删除时必须绕开的三处陷阱

    1. **`baselines` 没有外键**。它按 `(kind, label)` 存一行、用 `run_id`
       指向基线那次运行。如果那次运行被删掉，基线不会消失，只会变成
       悬空的 `run_id` —— 而门禁的 `R3-baseline-degradation` 恰恰是拿
       基线里的 `metrics_json` 做比较（不是拿 run 做 join），
       所以**悬空基线还是能用的**，但 `platform_cli baseline show` 会指向
       一个查不到的 run。这里的选择是：**保留被基线引用的 run**，
       因为基线是「故意留着的老数据」，它比任何统计都重要。

    2. **`gate_results` 没有外键、没有 CASCADE**。`steps`/`cases` 靠
       `REFERENCES runs(run_id) ON DELETE CASCADE` 自动清（`connect()`
       里 `PRAGMA foreign_keys=ON` 是生效的前提），但 `gate_results` 会
       留下来变成孤儿行。必须手工删。

    3. **正在跑的运行不能删**。`status='running'` 说明有另一个进程
       正在往里写，删掉它会让那个进程写进一个不存在的 run。

    ## VACUUM 的两个约束

    - **不能在事务里执行**（SQLite 直接报错），所以要单独开连接；
    - 删完之后不 VACUUM，文件大小不会变 —— 空间回收一半的清理等于没清理。
      但 VACUUM 会重写整个文件、需要额外磁盘空间，且会锁库，
      所以在 CI 上应当能关掉（`--no-vacuum`）。
    """
    store.init_db()
    keep_runs = max(0, args.keep_runs)
    keep_days = args.keep_days
    now = datetime.now(timezone.utc)

    with store.tx() as conn:
        # 被基线引用的 run：永久保留（见 docstring 第 1 点）。
        protected = {
            row["run_id"]
            for row in conn.execute("SELECT run_id FROM baselines")
        }
        # 正在跑的 run 也要保留（第 3 点）。
        protected |= {
            row["run_id"]
            for row in conn.execute("SELECT run_id FROM runs WHERE status='running'")
        }

        groups = conn.execute(
            "SELECT DISTINCT kind, label FROM runs ORDER BY kind, label"
        ).fetchall()

        doomed: list[str] = []
        plan: list[dict] = []
        for g in groups:
            kind, label = g["kind"], g["label"]
            # 新的在前：保留策略永远按"最近的"算。
            rows = conn.execute(
                "SELECT run_id, started_at, status FROM runs "
                "WHERE kind=? AND label=? ORDER BY started_at DESC",
                (kind, label),
            ).fetchall()

            keep: set[str] = set()
            for i, row in enumerate(rows):
                if i < keep_runs:
                    keep.add(row["run_id"])
                    continue
                if keep_days is not None:
                    # started_at 是 now_iso() 写的（带时区的 ISO 串）,
                    # 直接解析比较，别用字符串前缀比 —— 时区后缀会让它错。
                    try:
                        started = datetime.fromisoformat(row["started_at"])
                    except ValueError:
                        keep.add(row["run_id"])  # 解析不了就不敢删
                        continue
                    if started.tzinfo is None:
                        started = started.replace(tzinfo=timezone.utc)
                    if (now - started).total_seconds() <= keep_days * 86400:
                        keep.add(row["run_id"])

            dropped = [r["run_id"] for r in rows if r["run_id"] not in keep]
            kept_protected = [rid for rid in dropped if rid in protected]
            dropped = [rid for rid in dropped if rid not in protected]
            if dropped or kept_protected:
                plan.append({
                    "kind": kind,
                    "label": label,
                    "total": len(rows),
                    "keep": len(rows) - len(dropped),
                    "delete": len(dropped),
                    "kept_by_baseline": len(kept_protected),
                })
            doomed.extend(dropped)

        # 先数一遍连带影响的行数，好让 --dry-run 报告也说得清楚。
        def count_for(table: str, ids: list[str]) -> int:
            if not ids:
                return 0
            q = ",".join("?" * len(ids))
            return conn.execute(
                f"SELECT COUNT(*) c FROM {table} WHERE run_id IN ({q})", ids
            ).fetchone()["c"]

        steps_n = count_for("steps", doomed)
        cases_n = count_for("cases", doomed)
        gate_n = count_for("gate_results", doomed)

        deleted = {"runs": 0, "steps": 0, "cases": 0, "gate_results": 0}
        if doomed and not args.dry_run:
            q = ",".join("?" * len(doomed))
            # gate_results 没有外键，必须手工删（第 2 点）。
            deleted["gate_results"] = conn.execute(
                f"DELETE FROM gate_results WHERE run_id IN ({q})", doomed
            ).rowcount
            # steps / cases 由 ON DELETE CASCADE 带走。
            deleted["runs"] = conn.execute(
                f"DELETE FROM runs WHERE run_id IN ({q})", doomed
            ).rowcount
            deleted["steps"] = steps_n
            deleted["cases"] = cases_n

    vacuumed = False
    if not args.dry_run and not args.no_vacuum and deleted["runs"]:
        # VACUUM 不能在事务里跑，所以这里直接拿 store.connect()，
        # 并把 isolation_level 设为 None（自动提交），避免 sqlite3 隐式开事务。
        conn = store.connect()
        try:
            conn.isolation_level = None
            conn.execute("VACUUM")
            vacuumed = True
        finally:
            conn.close()

    payload = {
        "db": str(store.db_path()),
        "dry_run": bool(args.dry_run),
        "keep_runs": keep_runs,
        "keep_days": keep_days,
        "groups": plan,
        "delete": {
            "runs": len(doomed) if args.dry_run else deleted["runs"],
            "steps": steps_n if args.dry_run else deleted["steps"],
            "cases": cases_n if args.dry_run else deleted["cases"],
            "gate_results": gate_n if args.dry_run else deleted["gate_results"],
        },
        "vacuumed": vacuumed,
        "protected_by_baseline": sorted(protected),
    }

    verb = "将要删除" if args.dry_run else "已删除"
    print(f"[prune] {store.db_path()}")
    print(f"[prune] 保留策略：每个 label 最近 {keep_runs} 轮"
          + (f"，或最近 {keep_days} 天内" if keep_days is not None else ""))
    if not plan:
        print(f"[prune] 没有需要清理的运行")
    for g in plan:
        print(f"[prune]   {g['kind']}/{g['label']}: 共 {g['total']} 轮，"
              f"{verb} {g['delete']} 轮，保留 {g['keep']} 轮"
              + (f"（其中 {g['kept_by_baseline']} 轮因被基线引用而强制保留）"
                 if g["kept_by_baseline"] else ""))
    d = payload["delete"]
    print(f"[prune] 合计：runs {d['runs']} / steps {d['steps']} / cases {d['cases']} "
          f"/ gate_results {d['gate_results']}")
    if args.dry_run:
        print("[prune] --dry-run：没有真的删除任何东西。")
    elif vacuumed:
        print("[prune] 已执行 VACUUM 回收空间。")
    elif not args.no_vacuum:
        print("[prune] 没有删除任何行，跳过 VACUUM。")
    else:
        print("[prune] --no-vacuum：已跳过 VACUUM（文件大小不会变小）。")

    emit(args, payload)
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
    r.add_argument("--r3-advisory", action="store_true",
                   help="R3 只报数不拦人：这条判据在本环境噪声主导（见 docs/已知问题.md P-16）")
    add_json_flags(r)
    r.set_defaults(func=cmd_run)

    pr = sub.add_parser("probe", help="性能探针：agg vs n1")
    pr.add_argument("--label", default="probe-slow-report")
    pr.add_argument("--items", type=int, default=20_000)
    pr.add_argument("--users", type=int, default=10)
    pr.add_argument("--duration", type=float, default=8.0)
    pr.add_argument("--timeout", type=float, default=30.0)
    pr.add_argument("--warmup-drop", type=float, default=1.0)
    add_json_flags(pr)
    pr.set_defaults(func=cmd_probe)

    b = sub.add_parser("baseline", help="基线管理")
    b.add_argument("action", choices=["set", "show", "export", "import", "clear"])
    b.add_argument("--label", required=True)
    b.add_argument("--run-id", default=None)
    b.add_argument("--file", default=None,
                   help="export/import 的快照文件，默认 baselines/<label>.json")
    add_json_flags(b)
    b.set_defaults(func=cmd_baseline)

    t = sub.add_parser("trend", help="趋势")
    t.add_argument("--label", required=True)
    t.add_argument("--limit", type=int, default=10)
    add_json_flags(t)
    t.set_defaults(func=cmd_trend)

    g = sub.add_parser("gate", help="对已入库的 run 重新判定门禁")
    g.add_argument("--run-id", required=True)
    g.add_argument("--r3-advisory", action="store_true",
                   help="R3 只报数不拦人：这条判据在本环境噪声主导（见 docs/已知问题.md P-16）")
    add_json_flags(g)
    g.set_defaults(func=cmd_gate)

    su = sub.add_parser("suite", help="跑功能用例并入库、判定套件门禁")
    su.add_argument("--label", default="platform-tests")
    su.add_argument("--target", default="tests", help="pytest 目标路径")
    su.add_argument("extra", nargs="*", help="透传给 pytest 的额外参数")
    add_json_flags(su)
    su.set_defaults(func=cmd_suite)

    fl = sub.add_parser("flaky", help="列出 flaky 用例")
    fl.add_argument("--budget", type=int, default=None)
    add_json_flags(fl)
    fl.set_defaults(func=cmd_flaky)

    ns = sub.add_parser("noise", help="量出本机的运行间波动，用来定容忍带")
    ns.add_argument("--label", default="noise-study")
    ns.add_argument("--items", type=int, default=20_000)
    ns.add_argument("--rounds", type=int, default=5, help="连跑几轮（建议 ≥5）")
    ns.add_argument("--step-users", type=int, nargs="+", default=[10, 20])
    ns.add_argument("--step-duration", type=float, default=6.0)
    ns.add_argument("--warmup-drop", type=float, default=1.5)
    ns.add_argument("--timeout", type=float, default=10.0)
    add_json_flags(ns)
    ns.set_defaults(func=cmd_noise)

    pn = sub.add_parser("prune", help="清理 quality.db 的历史数据并回收空间")
    pn.add_argument("--keep-runs", type=int, default=200, help="每个 label 保留最近多少轮")
    pn.add_argument("--keep-days", type=float, default=None, help="只保留最近 N 天的运行")
    pn.add_argument("--dry-run", action="store_true", help="只报告将要删除什么")
    pn.add_argument("--no-vacuum", action="store_true", help="不执行 VACUUM")
    add_json_flags(pn)
    pn.set_defaults(func=cmd_prune)

    s = sub.add_parser("serve", help="起看板")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.set_defaults(func=cmd_serve)

    ev = sub.add_parser("env", help="显示当前环境档案与生效的变量")
    add_json_flags(ev)
    ev.set_defaults(func=cmd_env)
    return p


def _make_stdout_utf8() -> None:
    """让 stdout/stderr 能吃下门禁结论里的 ❌ / ✅ / ⚠。

    这两件事凑在一起就必炸：门禁结论用 emoji 表达（`gate.GateReport.render()`），
    而 Windows 控制台默认是 GBK 代码页 —— 于是**只要门禁真的拦住一次**，
    `print(report.render())` 就抛 UnicodeEncodeError，退出码变成 1 但原因
    是"打印失败"而不是"门禁拦住"，报告文件写不进去，CI 日志里也看不到判定。
    偏偏只有拦住的那条路径会崩：全绿时渲染出的字符串里没有这些字符，
    所以这个坑在本地一直看不见，直到第一次真正需要门禁拦住构建。

    用 errors="replace" 兜底：即使遇到再离谱的字符，也只退化成一个问号，
    不能让"打印结论"这件事决定门禁是否可用。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def cmd_env(args: argparse.Namespace) -> int:
    """显示当前生效的环境档案与相关变量（不跑任何东西）。"""
    keys = [
        "PERF_SUT_REPORT_MODE", "PERF_SUT_N1_SUPPLIERS",
        "PERF_SUT_ITEMS", "PERF_SUT_DB", "QUALITY_DB", "QUALITY_RUN_ID", "CI",
    ]
    profile = os.environ.get(ENV_PROFILE_VAR, "").strip() or "（未设 = 默认行为）"
    payload = {
        "profile": profile,
        "profile_dir": str(ENV_PROFILE_DIR),
        "available": sorted(p.stem for p in ENV_PROFILE_DIR.glob("*.env"))
        if ENV_PROFILE_DIR.is_dir() else [],
        "env": {k: os.environ.get(k) for k in keys},
        "db_path": str(store.db_path()),
    }
    print(f"[env] PERF_ENV={profile}")
    print(f"[env] 档案目录={ENV_PROFILE_DIR}")
    print(f"[env] 可用档案={', '.join(payload['available']) or '（无）'}")
    for k in keys:
        print(f"[env]   {k}={os.environ.get(k, '(未设)')}")
    emit(args, payload)
    return 0


def main(argv: list[str] | None = None) -> int:
    _make_stdout_utf8()
    # 先套档案再解析 argv：档案决定的是**环境**（会被 SUT 子进程继承），
    # 而 argparse 只决定这一次调用的参数。顺序反过来的话，
    # `--help` 之类的路径也会先把档案套上 —— 没必要，也更容易误伤。
    apply_env_profile(argv)
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
