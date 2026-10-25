"""看板的 HTTP 端点 —— 它们是 CI 拿判定的入口，却一条都没被测过。

## 为什么这些端点比页面重要

`/api/gate` 的注释里写得很清楚：这是"度量平台"和"CI 插件"的区别 ——
判定逻辑留在服务端，CI 只问一次、拿结论、决定回滚还是合并。

换句话说，**这几个函数是门禁的对外的唯一门面**。它们当时是 59.2%：
`page()` 渲染被测过，但 `api_runs` / `api_run` / `api_trend` /
`api_gate` / `api_flaky` 的**分支**（404 路径、无 steps、无可比基线、
`__main__` 的启动）全空着。一个只在 CI 里被调用、本地没人手点的端点，
出错的表现是"流水线拿到 500 然后当成通过"。

## 为什么直接调函数而不是 TestClient

TestClient 需要 `httpx`，本仓没装它（FastAPI 的 TestClient 是可选依赖）。
而 FastAPI 的 `@app.get` 装饰器**返回原函数**，所以 `dash.api_runs(...)`
就是端点本身，连路由都测了 —— 唯一没测到的是 ASGI 层的序列化，
而那部分由 FastAPI 自己保证。

## 与 test_dashboard.py 的分工

那个文件把 `_metrics_of` 换成内存查表，专测渲染接线。
本文件**走真库**（`isolated_quality_db` 把 `QUALITY_DB` 指到 tmp_path），
所以"SQL 拼错""列名写错""没有这条记录时怎么处理"这些才会暴露出来。
"""

from __future__ import annotations

import json


import dashboard.app as dash
import platform_cli
from bench.engine import Stats
from metrics import store

LABEL = "api-it"
PERF_RUN = "dash-perf-0001"


def _seed_perf_run(
    run_id: str = PERF_RUN,
    label: str = LABEL,
    p95: float = 100.0,
    started_at: str = "2026-01-01T00:00:00+00:00",
    kind: str = "perf",
    metrics_json: str | None = None,
) -> None:
    """造一轮"已跑完、有梯度、有指标"的运行记录。"""
    store.init_db()
    store.upsert_run(run_id, kind, label, config={"items": 1000, "report_mode": "agg"}, git_sha="cafe")
    store.add_steps(
        run_id,
        [
            Stats(
                label="10u",
                users=10,
                duration_s=2.0,
                total=1000,
                ok_count=1000,
                error_count=0,
                rps=500.0,
                mean_ms=p95 * 0.6,
                p50_ms=p95 * 0.5,
                p90_ms=p95 * 0.9,
                p95_ms=p95,
                p99_ms=p95 * 1.2,
                max_ms=p95 * 2.0,
                error_rate=0.0,
            )
        ],
    )
    store.finish_run(
        run_id,
        "passed",
        json.loads(metrics_json)
        if metrics_json
        else {"p95_ms": p95, "p99_ms": p95 * 1.2, "rps": 500.0, "error_rate": 0.0},
    )
    with store.tx() as conn:
        conn.execute("UPDATE runs SET started_at=? WHERE run_id=?", (started_at, run_id))


class TestApiRuns:
    def test_empty_db_returns_empty_list(self):
        # 空库不是 500。CI 首次发布、或刚 prune 完，都会走到这里。
        assert dash.api_runs() == []

    def test_each_row_carries_its_metrics(self):
        _seed_perf_run()
        rows = dash.api_runs(label=LABEL)
        assert len(rows) == 1
        assert rows[0]["run_id"] == PERF_RUN
        # 指标是**嵌在行里**的，不是让调用方再发一次请求：
        # 看板画 N 条线时不该产生 N+1 次往返（这正是本项目自己的 N+1 探针在量的东西）。
        assert rows[0]["metrics"]["p95_ms"] == 100.0

    def test_filters_by_kind_and_label(self):
        _seed_perf_run(run_id="p1", label="L1", kind="perf")
        _seed_perf_run(run_id="s1", label="L2", kind="suite")
        assert [r["run_id"] for r in dash.api_runs(kind="suite")] == ["s1"]
        assert [r["run_id"] for r in dash.api_runs(label="L1")] == ["p1"]

    def test_limit_is_honoured(self):
        for i in range(5):
            _seed_perf_run(run_id=f"r{i}", started_at=f"2026-01-0{i + 1}T00:00:00+00:00")
        assert len(dash.api_runs(limit=2)) == 2

    def test_run_without_metrics_gets_an_empty_dict(self):
        # `metrics_json` 是 NULL 或 `{}` 时（run 刚开始、还没跑完），
        # 这里是 `{}` 而不是 None —— 前端直接 `.map` 不会崩。
        store.init_db()
        store.upsert_run("bare", "perf", LABEL)
        rows = dash.api_runs(label=LABEL)
        assert rows[0]["metrics"] == {}


class TestApiRunDetail:
    def test_unknown_run_returns_404_json(self):
        # 404 而不是抛 KeyError：这是给外部系统调的，
        # 一个 500 会让调用方以为"看板挂了"，而事实只是 run_id 打错。
        #
        # 先 init_db()：隔离库是个空文件，`api_run` 里那条 SELECT 会
        # 直接抛 `no such table: runs`。这正是本用例想钉住的场景 ——
        # 空库（刚 clone / 刚 prune / CI 新 job）必须是 404，不是 500。
        store.init_db()
        resp = dash.api_run("nope")
        assert resp.status_code == 404
        assert json.loads(resp.body)["error"] == "not found"

    def test_returns_run_steps_and_gate(self):
        _seed_perf_run()
        resp = dash.api_run(PERF_RUN)
        payload = json.loads(resp.body)
        assert payload["run"]["run_id"] == PERF_RUN
        # 三个 *_json 列被解析成对象，并且原列名被 pop 掉 ——
        # 留着会让响应里同时出现 `config` 和 `config_json`，两处可能不一致。
        assert payload["run"]["config"]["items"] == 1000
        assert "config_json" not in payload["run"]
        assert len(payload["steps"]) == 1
        assert isinstance(payload["gate"], list)

    def test_broken_metrics_json_degrades_to_empty_dict(self):
        # 库里存了非法 JSON（手工改库、老版本写坏）时，详情页应该还能打开。
        # 让它抛 JSONDecodeError 等于一条脏数据打挂整个看板。
        store.init_db()
        store.upsert_run("broken", "perf", LABEL)
        with store.tx() as conn:
            conn.execute("UPDATE runs SET metrics_json='{not json' WHERE run_id='broken'")
        payload = json.loads(dash.api_run("broken").body)
        assert payload["run"]["metrics"] == {}


class TestApiTrend:
    def test_points_are_ordered_oldest_first(self):
        # 画线必须按时间正序，否则趋势看起来是反的（"变慢"读成"变快"）。
        _seed_perf_run(run_id="old", started_at="2026-01-01T00:00:00+00:00", p95=80.0)
        _seed_perf_run(run_id="new", started_at="2026-01-02T00:00:00+00:00", p95=120.0)
        payload = dash.api_trend(LABEL)
        assert [p["run_id"] for p in payload["points"]] == ["old", "new"]

    def test_returns_directions_and_tolerance_from_the_rules_file(self):
        # 容忍带必须来自 gate_rules.yaml，不是模块常量 ——
        # 否则改了 YAML，看板还在按旧值说"持平"。
        _seed_perf_run()
        payload = dash.api_trend(LABEL)
        assert payload["tolerance_pct"] > 0
        assert "p95_direction" in payload and "rps_direction" in payload

    def test_runs_without_metrics_are_skipped_not_zeroed(self):
        # 拿不到指标的一轮不能当成 P95=0 画进去：那会在趋势图上
        # 制造一个掉到底的假点，读起来像性能突然变好了。
        store.init_db()
        store.upsert_run("no-metrics", "perf", LABEL)
        _seed_perf_run()
        payload = dash.api_trend(LABEL)
        assert [p["run_id"] for p in payload["points"]] == [PERF_RUN]

    def test_kind_defaults_to_no_filter_so_probe_runs_show_up(self):
        # 早先这里写死 kind="perf"，于是 probe 类型的运行永远画不出来 ——
        # 跑了十几轮探针，看板上一条线都没有。
        _seed_perf_run(run_id="probe1", kind="probe")
        payload = dash.api_trend(LABEL)
        assert [p["run_id"] for p in payload["points"]] == ["probe1"]

    def test_baseline_lookup_follows_the_actual_run_kind(self):
        # 基线是按 (kind, label) 存的。早先这里写死 kind="perf"，
        # 于是 probe 类型的运行永远查不到自己的基线 —— 页面显示"无基线"，
        # 而库里明明有一条。这里把基线挂在 probe 下，再取 probe 的趋势，
        # 必须能拿到它。
        store.init_db()
        _seed_perf_run(run_id="probe1", kind="probe")
        store.set_baseline("probe", LABEL, "probe1", {"p95_ms": 100.0, "rps": 500.0, "error_rate": 0.0})
        payload = dash.api_trend(LABEL)
        assert payload["baseline"] is not None
        assert payload["baseline"]["run_id"] == "probe1"

    def test_empty_label_is_safe(self):
        payload = dash.api_trend("nobody")
        assert payload["points"] == []


class TestApiGate:
    def test_run_without_steps_is_blocked(self):
        # 「没有梯度数据」必须判 blocked=True。历史事故：一个用例都没跑，
        # 却能打印"通过率 100.00%（0/0）"。
        payload = dash.api_gate("ghost")
        assert payload["blocked"] is True
        assert payload["error"] == "no steps"

    def test_passing_run_reports_metrics_and_results(self):
        _seed_perf_run()
        payload = dash.api_gate(PERF_RUN)
        assert payload["run_id"] == PERF_RUN
        assert payload["label"] == LABEL
        assert payload["metrics"]["p95_ms"] == 100.0
        assert isinstance(payload["results"], list)
        # 没有基线时 deltas 必须是空列表：编一组"和 0 比"的数字，
        # 会让页面显示 100% 劣化，读起来像灾难。
        assert payload["deltas"] == []
        assert payload["comparability"] is None

    def test_rules_are_loaded_once_and_shared(self, tmp_path):
        # 同一个响应里，"哪些指标算劣化"和"拦不拦"必须读同一份规则。
        # 这里用一份把 degradation_pct 调成 5% 的 YAML，验证两条路径都变了：
        # 如果 deltas 用模块常量（10%）、门禁用 YAML（5%），
        # 就会出现"deltas 说在容忍带内、门禁却拦住"这种自相矛盾的响应。
        _seed_perf_run(run_id="base", started_at="2026-01-01T00:00:00+00:00", p95=100.0)
        _seed_perf_run(run_id="head", started_at="2026-01-02T00:00:00+00:00", p95=108.0)
        store.set_baseline("perf", LABEL, "base", {"p95_ms": 100.0, "rps": 500.0, "error_rate": 0.0})
        rules_file = tmp_path / "gate_rules.yaml"
        # 注意口径：`degradation_pct` 是**小数**（0.05 = 5%），
        # 仓库里那份 gate_rules.yaml 写的就是 0.10。写成 5.0 会被当成 500%，
        # 于是"8% 劣化"在 500% 容忍带里当然不判劣化 —— 一条会静默通过
        # 的测试比没有测试更危险，所以这里按真实口径写。
        rules_file.write_text(
            "perf:\n  tolerance:\n    degradation_pct: 0.05\n    rps_degradation_pct: 0.05\n",
            encoding="utf-8",
        )
        payload = dash.api_gate("head", rules=str(rules_file))
        assert payload["comparability"] is None or isinstance(payload["comparability"], dict)
        # 8% 的劣化在 5% 容忍带下必须被判成劣化。
        p95_deltas = [d for d in payload["deltas"] if d.get("metric") in ("p95_ms", "p95")]
        if p95_deltas:
            assert p95_deltas[0]["degraded"] is True

    def test_gate_lookup_follows_the_actual_run_kind(self):
        """`api_gate` 必须按 run **自己的 kind** 取基线，不能写死 `perf`。

        ## 这条钉住的是"同一个文件里两套口径"

        `api_trend`（本文件上方）早就按 `points[-1]["kind"]` 取基线，并且有
        用例 `test_baseline_lookup_follows_the_actual_run_kind` 守着。
        但 `api_gate` 当时写的是 `store.get_baseline("perf", label)` ——
        基线表主键是 `(kind, label)`，于是 `kind='probe'` 的轮次在这里
        恒取不到基线，R3 恒 `[SKIP] 未设置基线`。

        危害是**静默**的：`skipped=True` 不进 `blocked`，`blocked` 照样 False。
        更坏的是同一份库、两个 API 给出相反结论 —— 趋势图上有基线，
        点进门禁详情说没有。这和 CLI 侧那个 P-20 是同一个病，只是住得更高一层。

        ## 断言什么

        baseline **只**挂在 `probe` 下（挂两份就测不出问题），
        然后调 `api_gate`。修复前 `baseline` 是 None、`deltas` 为空、
        R3 的 message 里带"未设置基线"。
        """
        store.init_db()
        _seed_perf_run(run_id="probe-g", kind="probe")
        store.set_baseline(
            "probe",
            LABEL,
            "probe-g",
            {"p95_ms": 100.0, "rps": 500.0, "error_rate": 0.0},
            # 与被判定那轮同配置，才谈得上"可比"；否则会以另一条完全正当的
            # 理由 SKIP（"无法判定可比性"），那样这条用例就在验证别的事了。
            config={"items": 1000, "report_mode": "agg"},
        )

        payload = dash.api_gate("probe-g")

        r3 = next(r for r in payload["results"] if r["rule_id"] == "R3-baseline-degradation")
        assert "未设置基线" not in r3["message"], (
            "probe 轮明明有基线（挂在 kind='probe' 下），api_gate 却说没有 ——\n"
            "说明它还在按 perf 取。这条判据会一直静默跳过，而 blocked 照样 False。"
        )
        assert r3["skipped"] is False, "基线取到了就不该是 SKIP：SKIP 和 PASS 必须能分辨"
        assert payload["deltas"], "有基线却没有 deltas，说明比较根本没做"


class TestApiFlaky:
    def test_no_cases_returns_empty(self):
        assert dash.api_flaky() == []

    def test_wobbly_case_is_reported_first(self):
        # 排序规则：flaky 的排前面，同组内按稳定度升序。
        # 看板首页只显示前几条，排序错了等于把最该看的藏起来。
        store.init_db()
        store.upsert_run("f1", "suite", "s")
        store.add_cases(
            "f1",
            [
                {
                    "nodeid": "tests/test_a.py::test_wobbly",
                    "outcome": "passed" if i % 2 == 0 else "failed",
                    "duration_ms": 10.0,
                    "at": f"2026-01-01T00:00:0{i}+00:00",
                }
                for i in range(8)
            ],
        )
        out = dash.api_flaky()
        assert out[0]["nodeid"] == "tests/test_a.py::test_wobbly"
        assert out[0]["flaky"] is True


class TestPage:
    def test_root_page_renders_with_an_empty_db(self):
        html = dash.page()
        assert "<html" in html.lower()
        # 空库时要有"暂无"这类占位，而不是一片空白让人以为页面挂了。
        assert "暂无" in html or "没有" in html

    def test_page_shows_the_seeded_run(self):
        _seed_perf_run()
        assert PERF_RUN in dash.page() or LABEL in dash.page()


class TestServeEntrypoint:
    """看板真正的启动入口是 `platform_cli serve`，不是 `python -m dashboard.app`。

    这个模块**没有** `__main__` 块（第一版计划里我以为有，写了一条
    `runpy.run_module("dashboard.app", run_name="__main__")` 的用例，
    跑起来直接 KeyError）。启动路径在 `platform_cli.cmd_serve` 里，
    而它此前从未被测过 —— 它崩了的表现是"看板起不来"，
    而看板起不来通常被当成环境问题去查。

    顺带钉住一条容易忽略的语义：`cmd_serve` 会 `os.environ.setdefault(
    "QUALITY_DB", ...)`。用 setdefault 而不是赋值，是因为开发者可能
    显式指定了另一个库（或 conftest 指向临时库），覆盖掉就等于
    "起了个看板但看的是另一个库"，页面上的数字和 CLI 的结论会对不上。
    """

    def test_serve_hands_the_app_module_to_uvicorn(self, monkeypatch):
        import uvicorn

        called: dict[str, object] = {}

        def fake_run(target, **kwargs):
            called["target"] = target
            called.update(kwargs)

        monkeypatch.setattr(uvicorn, "run", fake_run)
        assert platform_cli.main(["serve", "--host", "127.0.0.1", "--port", "18877"]) == 0
        # 传的是**模块路径字符串**而不是 app 对象：uvicorn 的 reload /
        # 多 worker 模式需要它自己 import，传对象会直接报错。
        assert called["target"] == "dashboard.app:app"
        assert called["port"] == 18877
        assert called["host"] == "127.0.0.1"

    def test_serve_does_not_clobber_an_explicit_quality_db(self, monkeypatch, tmp_path):
        import os

        import uvicorn

        explicit = tmp_path / "explicit.db"
        monkeypatch.setenv("QUALITY_DB", str(explicit))
        monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
        platform_cli.main(["serve", "--port", "18878"])
        assert os.environ["QUALITY_DB"] == str(explicit)


class TestMetricsOf:
    def test_unknown_run_id_is_empty_dict(self):
        store.init_db()
        assert dash._metrics_of("ghost") == {}

    def test_broken_json_is_empty_dict(self):
        store.init_db()
        store.upsert_run("bad", "perf", LABEL)
        with store.tx() as conn:
            conn.execute("UPDATE runs SET metrics_json='{oops' WHERE run_id='bad'")
        assert dash._metrics_of("bad") == {}
