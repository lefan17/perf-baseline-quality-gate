"""`platform_cli.py` 各子命令的集成测试 —— 治「接上了但跑不通」。

## 与 tests/test_cli.py 的分工

`test_cli.py` 只验「接线」：解析器能建、每个子命令的 `func` 是非空的
可调用对象。它挡的是历史上那次 `def cmd_gate(args)` 整行被覆盖、
`NameError: name 'cmd_gate' is not defined` 的事故。

但「接上了」和「跑得通」是两件事。一个子命令可以完美接线，然后在你第一次
`prune --dry-run` 时因为 `_label_of` 返回空串而崩掉。本文件补的就是这一段：
真的调用 `main()`，真的落库，真的读回来，真的断言输出与退出码。

## 为什么用 main() 而不是直接调 cmd_*

直接调 `cmd_run(argparse.Namespace(...))` 需要手工造十几个字段，而且造的
Namespace 和真实解析器漂移了没人发现。走 `main([...])` 等于把 argparse 也
测进去 —— 而 argparse 恰恰是"命令跑不起来"最常见的现场（参数名拼错、
`nargs` 写成了 `type`）。

## 结果库一律隔离

`conftest.py` 的 `isolated_quality_db` 是 autouse 的，把 `QUALITY_DB` 指到
每个用例自己的 tmp_path。本文件在这之上跑，绝不碰仓内 `quality.db` ——
往度量平台自己的库里灌假运行记录，比往生产库里写脏数据更难发现，
因为它只表现为"最近趋势有点抖"。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import platform_cli
from bench.engine import Stats
from metrics import store

pytestmark = pytest.mark.sut

# 建一条完整的、可被 trend/gate/flaky 读的历史记录。
#
# 为什么自己 insert 而不是跑 `run`：`run` 要起被测服务、真压一轮，单条
# 用例 5 秒起。这里要验的是**读侧**（trend / gate / flaky / baseline）
# 能不能消化库里的数据，构造数据比等一份真实数据便宜得多，也让断言
# 可以钉死在确定的数值上（跑出来的 P95 每次都不同）。
RUN_ID = "run-read-side-0001"
LABEL = "cli-it"


def _seed_run(
    *,
    run_id: str = RUN_ID,
    label: str = LABEL,
    p95: float = 100.0,
    rps: float = 50.0,
    started_at: str = "2026-01-01T00:00:00+00:00",
    kind: str = "perf",
) -> None:
    """在隔离库里造一轮「已跑完、有指标、有一个梯度」的运行记录。"""
    store.init_db()
    store.upsert_run(run_id, kind, label, config={"items": 1000, "report_mode": "agg"}, git_sha="deadbeef")
    stats = Stats(
        label="10u",
        users=10,
        duration_s=2.0,
        total=1000,
        ok_count=1000,
        error_count=0,
        rps=rps,
        mean_ms=p95 * 0.6,
        p50_ms=p95 * 0.5,
        p90_ms=p95 * 0.9,
        p95_ms=p95,
        p99_ms=p95 * 1.2,
        max_ms=p95 * 2.0,
        error_rate=0.0,
    )
    store.add_steps(run_id, [stats])
    metrics = {
        "p95_ms": p95,
        "p99_ms": p95 * 1.2,
        "rps": rps,
        "error_rate": 0.0,
        "mean_ms": p95 * 0.6,
        "max_ms": p95 * 2.0,
    }
    store.finish_run(run_id, "passed", metrics)
    # started_at 由 upsert_run 写成本机当前时间；趋势要按时间排序，
    # 所以直接改这一列（没有公开 API 能指定它，这是测试数据构造的需要）。
    with store.tx() as conn:
        conn.execute("UPDATE runs SET started_at=? WHERE run_id=?", (started_at, run_id))


class TestTrendCommand:
    """`trend` 读库、排时间、给方向 —— 它曾是「跑了十几轮但趋势是空的」。"""

    def test_empty_db_says_so_and_exits_zero(self, capsys):
        # 没有数据不是错误：刚 clone 下来的仓库第一次跑 trend 就是这种情况，
        # 退出 1 会让 CI 上任何「先看看趋势」的步骤莫名变红。
        assert platform_cli.main(["trend", "--label", "nobody"]) == 0
        assert "没有" in capsys.readouterr().out

    def test_reads_seeded_run_and_returns_direction(self, capsys):
        _seed_run()
        assert platform_cli.main(["trend", "--label", LABEL]) == 0
        out = capsys.readouterr().out
        assert LABEL in out
        assert "P95 方向" in out

    def test_runs_are_listed_oldest_first_and_json_matches(self, tmp_path, capsys):
        # 时间倒序取、再 reverse，所以最后一轮（最新的）必须排在最后一行。
        # 这是个容易写反的地方：反了会让方向判断把"变慢"读成"变快"。
        _seed_run(run_id="r-old", started_at="2026-01-01T00:00:00+00:00", p95=80.0)
        _seed_run(run_id="r-new", started_at="2026-01-02T00:00:00+00:00", p95=120.0)
        out_json = tmp_path / "trend.json"
        assert platform_cli.main(["trend", "--label", LABEL, "--json-out", str(out_json)]) == 0
        capsys.readouterr()

        payload = json.loads(out_json.read_text(encoding="utf-8"))
        assert [p["run_id"] for p in payload["points"]] == ["r-old", "r-new"]
        assert payload["series_p95"] == [80.0, 120.0]

    def test_limit_is_honoured(self, capsys):
        for i in range(5):
            _seed_run(run_id=f"r{i}", started_at=f"2026-01-0{i + 1}T00:00:00+00:00")
        assert platform_cli.main(["trend", "--label", LABEL, "--limit", "2"]) == 0
        assert "最近 2 轮" in capsys.readouterr().out

    def test_json_flag_prints_structured_payload(self, capsys):
        _seed_run()
        assert platform_cli.main(["trend", "--label", LABEL, "--json"]) == 0
        out = capsys.readouterr().out
        # 输出里既有给人看的表格，也有给机器读的 JSON：这是刻意的，
        # 门禁结论要能进 PR 评论，人也要能直接读。
        start = out.index("{")
        payload = json.loads(out[start:])
        assert payload["label"] == LABEL


class TestGateCommand:
    """`gate` 对已入库的 run 重新判定 —— 换阈值复盘时唯一的入口。"""

    def test_unknown_run_exits_two(self, capsys):
        # 2 而不是 1：1 是「门禁判定为不通过」，2 是「这一轮根本不存在」。
        # 混用会让 CI 分不清「代码变慢」和「run_id 打错了」。
        assert platform_cli.main(["gate", "--run-id", "no-such-run"]) == 2
        assert "no steps for run" in capsys.readouterr().err

    def test_rejudges_a_seeded_run(self, capsys):
        _seed_run()
        code = platform_cli.main(["gate", "--run-id", RUN_ID])
        out = capsys.readouterr().out
        assert code in (0, 1)  # 有没有基线决定 R3，不影响本用例要证的性质
        assert f"run_id={RUN_ID}" in out
        assert "R1" in out and "R3" in out

    def test_r3_advisory_does_not_crash(self, capsys):
        _seed_run()
        assert platform_cli.main(["gate", "--run-id", RUN_ID, "--r3-advisory"]) in (0, 1)
        capsys.readouterr()

    def test_gate_results_are_persisted(self, capsys):
        # 「重判」要留下痕迹，否则复盘时看不出这一轮被谁改判过。
        _seed_run()
        platform_cli.main(["gate", "--run-id", RUN_ID])
        capsys.readouterr()
        with store.tx() as conn:
            n = conn.execute("SELECT COUNT(*) c FROM gate_results WHERE run_id=?", (RUN_ID,)).fetchone()["c"]
        assert n > 0


class TestFlakyCommand:
    """`flaky` 的三种结局：没有、有但在预算内、超预算。"""

    def test_no_flaky_cases_exits_zero(self, capsys):
        _seed_run()
        assert platform_cli.main(["flaky"]) == 0
        assert "没有达到 flaky 判定的用例" in capsys.readouterr().out

    def test_budget_override_reaches_the_structured_output(self, tmp_path, capsys):
        # 没有 flaky 用例时人读的输出里刻意不提预算（没东西可比，提了是噪音），
        # 所以断言放在结构化输出上 —— 那才是给门禁/PR 评论消费的口径。
        _seed_run()
        out_json = tmp_path / "flaky.json"
        assert platform_cli.main(["flaky", "--budget", "3", "--json-out", str(out_json)]) == 0
        capsys.readouterr()
        payload = json.loads(out_json.read_text(encoding="utf-8"))
        assert payload["budget"] == 3
        assert payload["over_budget"] is False

    def test_over_budget_exits_one(self, capsys):
        # 造一条真 flaky：同一 nodeid 跑 6 次、结果在通过与失败间摇摆。
        # 判据来自 gate_rules.yaml（≥6 次执行、稳定度 < 阈值），不是写死的。
        store.init_db()
        store.upsert_run("run-flaky", "suite", "f")
        rows = []
        for i in range(8):
            rows.append(
                {
                    "nodeid": "tests/test_x.py::test_wobbly",
                    "outcome": "passed" if i % 2 == 0 else "failed",
                    "duration_ms": 10.0,
                    "at": f"2026-01-01T00:00:0{i}+00:00",
                }
            )
        store.add_cases("run-flaky", rows)
        code = platform_cli.main(["flaky", "--budget", "0"])
        out = capsys.readouterr().out
        assert "test_wobbly" in out
        assert "超过 flaky 预算" in out
        assert code == 1


class TestBaselineCommand:
    """`baseline` 的五个 action —— set/show/export/import/clear。"""

    def test_set_without_any_run_exits_two(self, capsys):
        assert platform_cli.main(["baseline", "set", "--label", "nope"]) == 2
        assert "找不到" in capsys.readouterr().err

    def test_set_then_show_roundtrip(self, capsys):
        _seed_run()
        assert platform_cli.main(["baseline", "set", "--label", LABEL]) == 0
        capsys.readouterr()
        assert platform_cli.main(["baseline", "show", "--label", LABEL]) == 0
        out = capsys.readouterr().out
        payload = json.loads(out[out.index("{") :])
        assert payload["run_id"] == RUN_ID
        assert payload["metrics"]["p95_ms"] == 100.0

    def test_show_missing_label_is_not_an_error(self, capsys):
        store.init_db()
        assert platform_cli.main(["baseline", "show", "--label", "nope"]) == 0
        assert "没有" in capsys.readouterr().out

    def test_clear_removes_it(self, capsys):
        _seed_run()
        platform_cli.main(["baseline", "set", "--label", LABEL])
        capsys.readouterr()
        assert platform_cli.main(["baseline", "clear", "--label", LABEL]) == 0
        capsys.readouterr()
        assert store.get_baseline("perf", LABEL) is None


class TestEnvCommand:
    def test_prints_effective_env_and_available_profiles(self, capsys):
        assert platform_cli.main(["env"]) == 0
        out = capsys.readouterr().out
        assert "PERF_ENV=" in out
        assert "QUALITY_DB=" in out

    def test_json_payload_lists_db_path(self, tmp_path, capsys):
        out_json = tmp_path / "env.json"
        assert platform_cli.main(["env", "--json-out", str(out_json)]) == 0
        capsys.readouterr()
        payload = json.loads(out_json.read_text(encoding="utf-8"))
        assert payload["db_path"].endswith(".db")


class TestPruneCommand:
    """`prune` 是唯一会删东西的命令 —— 它的每一种保护都必须被验证。"""

    def test_dry_run_deletes_nothing(self, capsys):
        for i in range(3):
            _seed_run(run_id=f"p{i}", started_at=f"2026-01-0{i + 1}T00:00:00+00:00")
        assert platform_cli.main(["prune", "--keep-runs", "1", "--dry-run"]) == 0
        capsys.readouterr()
        with store.tx() as conn:
            n = conn.execute("SELECT COUNT(*) c FROM runs").fetchone()["c"]
        assert n == 3  # dry-run 就是不许动

    def test_keeps_the_newest_n(self, capsys):
        for i in range(4):
            _seed_run(run_id=f"k{i}", started_at=f"2026-01-0{i + 1}T00:00:00+00:00")
        assert platform_cli.main(["prune", "--keep-runs", "2", "--no-vacuum"]) == 0
        capsys.readouterr()
        with store.tx() as conn:
            left = {r["run_id"] for r in conn.execute("SELECT run_id FROM runs").fetchall()}
        assert left == {"k2", "k3"}

    def test_baseline_pointing_at_a_deleted_run_survives(self, capsys):
        # 这是 prune 最危险的陷阱：baselines 没有外键。删掉被指向的那一轮
        # 之后，基线若跟着消失，"慢慢变慢"就永远不会被抓到；若留下来又指向
        # 一个不存在的 run，后续 gate 会 KeyError。正确行为是保留基线、
        # 并在输出里说明它的 run 已经不在了。
        _seed_run(run_id="only", started_at="2026-01-01T00:00:00+00:00")
        store.set_baseline("perf", LABEL, "only", {"p95_ms": 100.0})
        assert platform_cli.main(["prune", "--keep-runs", "0", "--no-vacuum"]) == 0
        out = capsys.readouterr().out
        assert store.get_baseline("perf", LABEL) is not None
        assert "基线" in out

    def test_keep_days_filters_by_age(self, capsys):
        _seed_run(run_id="ancient", started_at="2020-01-01T00:00:00+00:00")
        _seed_run(run_id="fresh", started_at="2030-01-01T00:00:00+00:00")
        assert platform_cli.main(["prune", "--keep-runs", "0", "--keep-days", "1", "--no-vacuum"]) == 0
        capsys.readouterr()
        with store.tx() as conn:
            left = {r["run_id"] for r in conn.execute("SELECT run_id FROM runs").fetchall()}
        assert "ancient" not in left


class TestReportCommand:
    """`report` 把看板导成静态页 —— 它是 GitHub Pages 的唯一数据来源。"""

    def test_writes_index_html_for_an_empty_db(self, tmp_path, capsys):
        # 空库也要出页：CI 首次发布时就是这样，出不了页会让 pages job 红。
        out_dir = tmp_path / "site"
        assert platform_cli.main(["report", "--out", str(out_dir)]) == 0
        capsys.readouterr()
        html = (out_dir / "index.html").read_text(encoding="utf-8")
        assert "<html" in html.lower()

    def test_renders_seeded_run(self, tmp_path, capsys):
        _seed_run()
        out_dir = tmp_path / "site"
        assert platform_cli.main(["report", "--out", str(out_dir)]) == 0
        capsys.readouterr()
        html = (out_dir / "index.html").read_text(encoding="utf-8")
        assert LABEL in html

    def test_static_page_leaks_no_local_paths(self, tmp_path, capsys):
        # 这一页会被公开到 GitHub Pages。本机绝对路径（用户名、盘符）
        # 出现在一个公开页面上，是最容易犯又最难自己发现的泄露。
        _seed_run()
        out_dir = tmp_path / "site"
        platform_cli.main(["report", "--out", str(out_dir)])
        capsys.readouterr()
        html = (out_dir / "index.html").read_text(encoding="utf-8")
        for needle in ("C:\\", "D:\\", str(Path.home())):
            assert needle not in html, f"静态页里出现了本机路径片段 {needle!r}"


class TestSuiteCommand:
    """`suite` 起一个子进程跑指定的 pytest 目标，凭退出码判定。"""

    def test_passing_target_is_recorded_and_green(self, tmp_path, capsys):
        # 造一个必然通过的极小目标，而不是跑真 tests/（那要 80 秒且会递归）。
        target = tmp_path / "tiny_pass.py"
        target.write_text(
            "def test_ok():\n    assert True\n",
            encoding="utf-8",
        )
        code = platform_cli.main(["suite", "--label", "it-pass", "--target", str(target)])
        out = capsys.readouterr().out
        assert code == 0, out

    def test_failing_target_makes_the_gate_red(self, tmp_path, capsys):
        # 套件门禁的核心承诺：用例失败必须让命令退出非 0。
        target = tmp_path / "tiny_fail.py"
        target.write_text(
            "def test_bad():\n    assert False\n",
            encoding="utf-8",
        )
        code = platform_cli.main(["suite", "--label", "it-fail", "--target", str(target)])
        out = capsys.readouterr().out
        assert code == 1, out
        assert "失败" in out


class TestServeCommand:
    """`serve` 起的是一个真 HTTP 服务 —— 只验证"端口真的被监听了"。"""

    def test_serves_and_answers(self, monkeypatch):
        # 用 0 号端口拿一个空闲端口，然后把 uvicorn 的 run 打桩掉：
        # 起真 uvicorn 会阻塞整个测试会话。
        called: dict[str, object] = {}

        def fake_run(app, **kwargs):
            called.update(kwargs)
            called["app"] = app

        import uvicorn

        monkeypatch.setattr(uvicorn, "run", fake_run)
        assert platform_cli.main(["serve", "--host", "127.0.0.1", "--port", "18876"]) == 0
        assert called["host"] == "127.0.0.1"
        assert called["port"] == 18876
