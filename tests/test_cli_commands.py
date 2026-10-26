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

    def test_rejudging_does_not_duplicate_gate_results(self, capsys):
        """重判同一个 run 不许把结论叠加 —— 判定是快照，不是日志。

        ## 这条原来只断言 `n > 0`

        上一条用例已经证明"落库了"，但它对**行数**没有上界要求。于是
        `add_gate_results` 里只有 `INSERT` 这个缺陷活了很久：

        ```
        连跑 3 次 gate --run-id → 五条规则各 3 行，共 15 行
        dashboard.app._gate_for 按 ORDER BY id 全取 → /api/run 返回 15 条
        ```

        `cmd_gate` 的用途恰恰是"改完阈值回头复判"，所以这不是少见路径：
        调阈值时人会连跑好几次，页面上的规则就一层层叠起来。

        ## 为什么断言"恰好等于规则条数"而不是"不重复"

        只断言 `COUNT(DISTINCT rule_id) == COUNT(*)` 的话，一个从不写库的
        实现也能过（0 == 0）。所以这里同时钉住两件事：
        1. 三次重判后行数等于**一次**判定的行数（幂等）；
        2. 规则集合完整（真的有内容，不是空的又"不重复"）。
        """
        _seed_run()
        platform_cli.main(["gate", "--run-id", RUN_ID])
        capsys.readouterr()
        with store.tx() as conn:
            once = conn.execute("SELECT COUNT(*) c FROM gate_results WHERE run_id=?", (RUN_ID,)).fetchone()["c"]
            rules_once = {
                r["rule_id"]
                for r in conn.execute("SELECT rule_id FROM gate_results WHERE run_id=?", (RUN_ID,)).fetchall()
            }
        assert once >= 4, f"一次判定只写了 {once} 行，规则没发全，后面的幂等断言就没意义了"
        assert len(rules_once) == once, f"一次判定里就有重复规则：{rules_once}"

        for _ in range(2):
            platform_cli.main(["gate", "--run-id", RUN_ID])
            capsys.readouterr()

        with store.tx() as conn:
            three = conn.execute("SELECT COUNT(*) c FROM gate_results WHERE run_id=?", (RUN_ID,)).fetchone()["c"]
            rules_three = {
                r["rule_id"]
                for r in conn.execute("SELECT rule_id FROM gate_results WHERE run_id=?", (RUN_ID,)).fetchall()
            }
        assert three == once, (
            f"重判 3 次后 gate_results 变成 {three} 行（一次是 {once} 行）——"
            "add_gate_results 只 INSERT 不替换，看板会把同一规则重复展示"
        )
        assert rules_three == rules_once, "重判后规则集合变了，说明替换时丢行或混进了历史规则"

    def test_probe_run_finds_its_baseline_not_perf(self, capsys):
        """`gate` 必须按 run **自己的 kind** 取基线。

        ## 这条钉住的是一个"永远不会变红"的缺陷

        基线表主键是 `(kind, label)`。`cmd_gate` 原先写死
        `store.get_baseline("perf", label)`：

        * 对 `perf` 轮 —— 碰巧对；
        * 对 `probe` 轮 —— 恒取不到，于是 R3 恒打 `[SKIP] 未设置基线`。

        危害在于它**不会报错**：R3 是 `skipped=True` + `severity=warning`，
        不进 `blocked`，退出码照样 0。所以这个缺陷可以一直住着，
        而「慢慢变慢」这条核心判据在 probe 轮上从来没生效过。

        同一个事实，看板早就改对了（`dashboard/app.py` 按 run 的 kind 取，
        有回归用例 `test_baseline_lookup_follows_the_actual_run_kind`），
        只有 CLI 这一侧漏了 —— 于是同一份库，看板说"有基线"，
        `gate --run-id` 说"未设置基线"。两条路径给出相反的结论。

        ## 断言什么

        造一轮 `kind="probe"` 的 run，把基线挂在 `probe` 下，
        然后 `gate --run-id`。断言输出里**不再出现** SKIP 那句话，
        而是真的做了对比。

        ## 基线必须带 config 快照，否则会以另一种方式 SKIP

        R3 有两条 `[SKIP]` 出路，它们的**取证方向完全不同**：

        * 「未设置基线」→ 基线没取到（本用例要打的就是这条，kind 写死导致）；
        * 「基线没有留下配置快照，无法判定可比性」→ 基线取到了，但没法比。

        第二条是设计意图（拿两轮不同配置比出的劣化百分比没有意义），
        不是缺陷。所以本用例必须把 `config` 一起写进基线，
        否则会卡在第二条上，而看起来像第一条没修好 —— 那样这条用例
        就成了"永远在验证另一件事"的假绿的近亲。
        """
        _seed_run(run_id="probe-run-1", kind="probe")
        # 故意只挂在 probe 下：如果 gate 还写死 "perf"，这里必然取不到。
        store.set_baseline(
            "probe",
            LABEL,
            "probe-run-1",
            {"p95_ms": 100.0, "rps": 50.0, "error_rate": 0.0},
            # 与被门禁那轮**同配置**，才谈得上"可比"。`_seed_run` 写的是
            # items=1000 / report_mode=agg，这里保持一致。
            config={"items": 1000, "report_mode": "agg"},
        )

        platform_cli.main(["gate", "--run-id", "probe-run-1"])
        out = capsys.readouterr().out

        assert "未设置基线" not in out, (
            "probe 轮明明有基线（挂在 kind='probe' 下），gate 却报「未设置基线」——\n"
            "说明它还在按 perf 取。这条判据会一直静默跳过，而门禁照样绿。"
        )
        assert "无法判定可比性" not in out, (
            "基线带了 config 快照却仍被判不可比 —— 说明取到的不是这条基线，\n或可比性校验读错了字段。"
        )
        # 真判时的两种可辨识结局之一必须出现。
        assert ("全部指标在容忍带内" in out) or ("相对基线劣化" in out), out


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

    def test_clear_on_a_brand_new_database_does_not_crash(self, capsys):
        """全新库上 `clear` 必须是「没什么可清」，不是「表不存在」。

        ## 修复前的真实报错

        ```
        $ QUALITY_DB=<一个还不存在的路径> python platform_cli.py baseline clear --label x
        sqlite3.OperationalError: no such table: baselines      (platform_cli.py:755)
        exit=1
        ```

        根因：`clear` 是唯一一个**自己开事务**的 action（`from metrics.store
        import tx`），其余四个 action 都走 `store.*` 函数，而那些函数内部会
        `init_db()`。于是同一个空库上 `show` 优雅返回 0、`clear` 崩成 1。

        ## 为什么要为它单独立一条用例

        `test_clear_removes_it` 是先 `_seed_run()` 再 clear 的 —— 那时表早就
        建好了，`init_db()` 缺失这件事**完全测不到**。**一个「删东西」的用例
        天然会先把东西造出来**，而缺陷恰恰只在"没有东西可删"时出现。
        这类用例的盲区不是不够仔细，是它的构造方式必然绕开缺陷。
        """
        # 注意：这里**刻意不调** store.init_db() / _seed_run()。
        # autouse fixture `isolated_quality_db` 把 QUALITY_DB 指到一个
        # 还不存在的临时文件，于是"全新库"这个前提是天然的。
        code = platform_cli.main(["baseline", "clear", "--label", "nobody"])
        out = capsys.readouterr()
        assert code == 0, f"全新库上 clear 非零退出（{code}）：{out.err}"
        assert "已清除" in out.out

    def test_probe_baseline_roundtrip_with_kind_flag(self, capsys):
        """`--kind probe` 必须能把 probe 轮的基线**存进去并取回来**。

        ## 这是 P-20 修复的另一半

        `gate` 那一侧已改成按 run 自己的 kind 取基线。但如果 `baseline`
        五个 action 全写死 `kind='perf'`，那个修复就是落不了地的：probe 轮的
        基线**没有任何 CLI 入口能设**，于是 `gate --run-id <probe 轮>` 依然恒
        `[SKIP] 未设置基线` —— 症状一模一样，只是根因从"取错"变成"存不进"。

        只修一半比不修更坏：`gate` 的注释会写"按 kind 取"，读的人以为
        probe 轮已经能判了，而实际上没人能给它设基线。

        ## 断言什么

        1. `set --kind probe` 落库后，**probe 下查得到**、**perf 下查不到**
           —— 后者是关键：如果实现里仍然写死 perf，前半条会失败；
           如果实现里把 kind 丢掉（比如总是写两份），后半条会失败。
        2. 紧接着 `gate --run-id`，R3 不再是 SKIP，而是真的判定。
        """
        _seed_run(run_id="probe-k", kind="probe")
        capsys.readouterr()

        assert platform_cli.main(["baseline", "set", "--label", LABEL, "--kind", "probe"]) == 0
        capsys.readouterr()

        assert store.get_baseline("probe", LABEL) is not None, "--kind probe 没有把基线存到 probe 下"
        assert store.get_baseline("perf", LABEL) is None, "设 probe 基线时顺手在 perf 下也写了一份"

        platform_cli.main(["gate", "--run-id", "probe-k"])
        out = capsys.readouterr().out
        assert "未设置基线" not in out, "基线确实存在，gate 却说没有 —— 取基线的那一侧没修完整"
        r3_line = next(ln for ln in out.splitlines() if "R3" in ln)
        assert "SKIP" not in r3_line, f"R3 仍然是 SKIP：{r3_line}"

    def test_show_and_clear_respect_kind(self, capsys):
        """`show` / `clear` 也要认 `--kind`，不能只看 perf 那一行。

        同一个 label 在 perf 与 probe 下可以是**两条不同的基线**（各有各的
        run_id）。`show --kind probe` 如果仍读 perf 那份，会显示一条根本
        不属于 probe 的基线 —— 而 `clear --kind probe` 会删错东西，
        或者什么都不删却打印「已清除」。
        """
        _seed_run(run_id="perf-row", kind="perf")
        _seed_run(run_id="probe-row", kind="probe", p95=50.0)
        platform_cli.main(["baseline", "set", "--label", LABEL])
        platform_cli.main(["baseline", "set", "--label", LABEL, "--kind", "probe"])
        capsys.readouterr()

        assert platform_cli.main(["baseline", "show", "--label", LABEL, "--kind", "probe"]) == 0
        out = capsys.readouterr().out
        assert "probe-row" in out, f"show --kind probe 显示的不是 probe 那条基线：{out}"

        assert platform_cli.main(["baseline", "clear", "--label", LABEL, "--kind", "probe"]) == 0
        capsys.readouterr()
        assert store.get_baseline("probe", LABEL) is None, "clear --kind probe 没删掉 probe 那条"
        assert store.get_baseline("perf", LABEL) is not None, "clear --kind probe 把 perf 那条也删了"


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

    def test_running_row_is_reported_as_running_not_as_baseline(self, capsys):
        """两种保护原因必须分开报（P-30）。

        原先 `protected` 把「被基线引用」和「status='running'」合成一个集合，
        输出只有一句「因被基线引用而强制保留」。于是对一个正在跑的僵尸行，
        prune 会打印一条**无中生有**的解释 —— 而它的真实原因是
        「另一个进程可能正在往里写」。这两种情况的处置完全不同：
        前者是永久保护，后者等一会儿再跑就有了。
        """
        _seed_run(run_id="zombie", started_at="2026-01-01T00:00:00+00:00")
        with store.tx() as conn:
            conn.execute("UPDATE runs SET status='running' WHERE run_id='zombie'")
        assert platform_cli.main(["prune", "--keep-runs", "0", "--no-vacuum"]) == 0
        out = capsys.readouterr().out
        assert "running" in out, "输出必须说清是因为 running 才保留"
        assert "基线" not in out, f"这一轮根本没被基线引用，不该提基线：{out}"
        # 而且它确实没被删掉 —— 说明保护本身是对的，错的只是解释。
        with store.tx() as conn:
            left = {r["run_id"] for r in conn.execute("SELECT run_id FROM runs").fetchall()}
        assert "zombie" in left

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


class TestNoiseCommand:
    """`noise` 量的是本机波动 —— 但它崩溃时不许在库里留下僵尸行（P-31）。"""

    def test_failure_marks_the_run_as_error_instead_of_leaving_it_running(self, monkeypatch, capsys):
        """崩溃的轮次必须落成 status='error'，不能停在 'running'。

        原先只有 `finally: handle.stop()`，于是 `--step-users 0` 让
        ThreadPoolExecutor 抛 ValueError 之后，runs 表留了一行
        status='running'。而 `prune` 把 running 当作「另一个进程可能正在
        往里写」而**永久保护** —— 一次失败就 forever 占着一个保留位。

        这里把 start_sut / Runner 都打桩掉（`noise` 要起真服务、真压测，
        一条用例 10 秒起），只验证"异常路径下 run 的终态"。
        """
        import platform_cli as pc

        class _FakeHandle:
            base_url = "http://127.0.0.1:1"

            def stop(self):
                pass

        monkeypatch.setattr(pc, "start_sut", lambda **kw: _FakeHandle())
        monkeypatch.setattr(pc, "configure_item_range", lambda *a, **k: [])
        monkeypatch.setattr(pc, "build_engine_mix", lambda *a, **k: [])

        class _BadRunner:
            def __init__(self, **kwargs):
                pass

            def run(self):
                raise ValueError("max_workers must be greater than 0")

        monkeypatch.setattr(pc, "Runner", _BadRunner)

        with pytest.raises(ValueError):
            platform_cli.main(["noise", "--label", "boom", "--rounds", "1", "--step-users", "1"])

        with store.tx() as conn:
            rows = list(conn.execute("SELECT run_id, status FROM runs").fetchall())
        assert len(rows) == 1, f"应该只留下一轮记录：{rows}"
        assert rows[0]["status"] == "error", f"崩溃的轮次必须落成 error，否则 prune 会永久保护它：{rows[0]['status']}"
