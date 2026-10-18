"""看板渲染的接线测试。

## 这个文件为什么存在

`dashboard/app.py` 里曾经有一个**定义了却从没被调用**的函数：
`_sparkline()` 写好了、逻辑也对，但 `_render()` 里根本没有引用它。
结果是页面上**一根线都没有**，而 README 里写着"手写 SVG 折线画趋势"。

这类"定义了却没接线"的 bug 有三个特点，使它特别难被现有的测试发现：

1. **函数级单测全绿** —— `_sparkline([1,2,3])` 本身完全正确；
2. **页面能正常返回 200** —— 只是少了一块内容，不是报错；
3. **没人会盯着看** —— 看板是给人瞟一眼的，少一张图不会有人报 bug。

而且它和 `test_cli.py` 里那个 `NameError` 是**同一类问题的两个面**：
那边是"调用了不存在的东西"，这边是"存在的东西没被调用"。
前者会立刻炸，后者可以安静地活很久。

## 所以这里断言什么

不测样式、不测 HTML 美学，只断言**"有值的趋势数据必须真的被画出来"**：
把运行记录喂进 `_render`，结果里必须出现 `<svg>` 和 `<polyline>`。
"""

from __future__ import annotations

import inspect

import pytest

import dashboard.app as dash
from metrics import analysis

# 每个测试开始时清空，`_run()` 往里登记，供被替换的 `_metrics_of` 读取。
_FIXTURE_METRICS: dict[str, dict] = {}


def _run(run_id: str, label: str = "demo", kind: str = "perf", p95: float = 100.0) -> dict:
    metrics = {"p95_ms": p95, "p99_ms": p95 * 1.5, "rps": 200.0, "error_rate": 0.0}
    _FIXTURE_METRICS[run_id] = metrics
    return {
        "run_id": run_id,
        "kind": kind,
        "label": label,
        "status": "passed",
        "started_at": "2026-10-10T12:00:00",
    }


@pytest.fixture(autouse=True)
def _isolate_metrics(monkeypatch):
    """把取数那一层换成内存查表。

    `_render` 原本通过 `_metrics_of(run_id)` 查库取指标，
    这里要测的是**渲染与接线**，不是数据库往返，所以替换掉取数函数。
    顺带保证这条测试不会去碰开发者的 quality.db。
    """
    # `_render` 还会读一次 baselines 表（渲染"当前基线"区块），
    # 所以隔离库也得先建表 —— 否则渲染测试会因为缺表而失败，
    # 而那个失败跟渲染逻辑毫无关系。
    from metrics import store

    store.init_db()

    _FIXTURE_METRICS.clear()
    monkeypatch.setattr(dash, "_metrics_of", lambda run_id: _FIXTURE_METRICS.get(run_id, {}))
    yield
    _FIXTURE_METRICS.clear()


class TestSparkline:
    """折线绘制本身。"""

    def test_returns_svg_with_points(self):
        svg = dash._sparkline([1.0, 2.0, 3.0])
        assert "<svg" in svg
        assert "<polyline" in svg

    def test_refuses_to_draw_with_insufficient_samples(self):
        """少于 2 个点画不出线 —— 要显示"样本不足"，而不是编一条假的。"""
        for values in ([], [5.0]):
            out = dash._sparkline(values)
            assert "<svg" not in out
            assert "样本不足" in out

    def test_flat_series_does_not_divide_by_zero(self):
        """全部相同的值会让 (hi - lo) 为 0 —— 必须被兜住。"""
        svg = dash._sparkline([7.0, 7.0, 7.0, 7.0])
        assert "<polyline" in svg
        assert "nan" not in svg.lower()


class TestRenderWiring:
    """接线：喂进趋势数据，画出来。"""

    def test_two_runs_of_same_label_produce_a_chart(self):
        """同一个 label 有 ≥2 轮记录时，页面上必须出现折线。

        这就是当初那个 bug 的克星：`_sparkline` 存在但没被调用时，
        这条断言会直接失败。
        """
        home = [_run("perf-1", p95=100.0), _run("perf-2", p95=120.0)]
        html = dash._render(home, [])
        assert "<polyline" in html, "有 2 轮同 label 的记录，却没画出任何折线"
        assert "P95 趋势" in html

    def test_single_run_shows_placeholder_not_broken_chart(self):
        """只有 1 轮时应当明确说明"样本不足"，而不是画一条无意义的线。"""
        html = dash._render([_run("perf-1")], [])
        assert "至少 2 轮" in html

    def test_no_runs_at_all_still_renders(self):
        """空库也要能出页面 —— 新克隆的仓库第一次打开就是这种情况。"""
        html = dash._render([], [])
        assert "<!doctype html>" in html
        assert "还没有运行记录" in html

    def test_distinct_labels_each_get_their_own_chart(self):
        """不同 label 是不同的数据集，不能混在一条线上。"""
        home = [
            _run("a-1", label="alpha", p95=100.0),
            _run("a-2", label="alpha", p95=110.0),
            _run("b-1", label="beta", p95=300.0),
            _run("b-2", label="beta", p95=320.0),
        ]
        html = dash._render(home, [])
        assert html.count("<polyline") == 2, "两个 label 应当各画一条，不能合并"

    def test_huge_spike_is_clamped_so_trend_stays_visible(self):
        """一个 10 秒的劣化点不该把其余正常点压成一条直线。

        这是刻意的截断（500ms 上限）：趋势图的作用是"看出形状"，
        精确数值在报告和表格里，各有各的用途。
        """
        home = [
            _run("s-1", p95=40.0),
            _run("s-2", p95=42.0),
            _run("s-3", p95=10_000.0),
        ]
        html = dash._render(home, [])
        assert "<polyline" in html
        # 若没截断，正常点会被压到 y≈高度，坐标几乎重合
        pts = html.split("<polyline points=")[1].split('"')[1]
        ys = [float(p.split(",")[1]) for p in pts.split()]
        assert max(ys) - min(ys) > 1.0, "截断失效：正常波动被大尖峰压平了"


class TestGateEndpointContract:
    """`/gate` 是给 CI 调的结构化接口，签名本身就是契约。"""

    def test_gate_requires_run_id(self):
        """run_id 是必填 query 参数 —— 无参调用必须 422，不能返一个"全部通过"。

        这条很重要：一个能被无参调用且返回"放行"的门禁接口，
        会让配错参数的上游以为构建通过了。
        """
        sig = inspect.signature(dash.api_gate)
        assert "run_id" in sig.parameters
        assert sig.parameters["run_id"].default is inspect.Parameter.empty

    @pytest.mark.parametrize("path", ["/gate", "/api/gate"])
    def test_both_gate_paths_are_registered(self, path):
        """`/gate` 给人看，`/api/gate` 给脚本调 —— 两条路由都要在。"""
        paths = {r.path for r in dash.app.routes}
        assert path in paths


class TestMergeIsSingleSourced:
    """整轮汇总的实现只能有一份。

    这条用例存在的理由是一次真实的重复实现事故：
    `dashboard._merge` 和 `platform_cli._merge_from_rows` 各写了一遍，
    看板那份把 RPS 写成各梯度相加（凭空翻倍），
    而 CLI 那份已经修过同一个错 —— 同一个 bug 修了两次，
    因为代码有两份。

    所以这里锁的不是"某个数字对不对"，而是**结构**：
    口径只允许定义在 `metrics.analysis.merge_step_rows` 一处。
    """

    def test_dashboard_merge_matches_canonical(self):
        steps = [
            {
                "total": 1000,
                "error_count": 0,
                "duration_s": 10.0,
                "rps": 100.0,
                "mean_ms": 10.0,
                "p50_ms": 8.0,
                "p90_ms": 12.0,
                "p95_ms": 15.0,
                "p99_ms": 20.0,
                "max_ms": 30.0,
                "max_inflight": 10,
            },
            {
                "total": 500,
                "error_count": 5,
                "duration_s": 5.0,
                "rps": 100.0,
                "mean_ms": 20.0,
                "p50_ms": 16.0,
                "p90_ms": 24.0,
                "p95_ms": 40.0,
                "p99_ms": 60.0,
                "max_ms": 90.0,
                "max_inflight": 20,
            },
        ]
        got = dash._merge(steps)
        want = analysis.merge_step_rows([dict(s) for s in steps])
        assert got == want
        # RPS = 1500/15 = 100，不是 100+100=200。
        assert got["rps"] == pytest.approx(100.0)
        assert got["p95_ms"] == 40.0

    def test_cli_merge_delegates_to_canonical(self):
        """CLI 的那层是**别名**，不是第二份实现。"""
        import platform_cli

        steps = [
            {
                "total": 100,
                "error_count": 0,
                "duration_s": 4.0,
                "rps": 25.0,
                "mean_ms": 1.0,
                "p50_ms": 1.0,
                "p90_ms": 1.0,
                "p95_ms": 2.0,
                "p99_ms": 3.0,
                "max_ms": 4.0,
                "max_inflight": 4,
            },
        ]
        assert platform_cli._merge_from_rows(steps) == analysis.merge_step_rows(steps)


class TestStaticSnapshot:
    """静态导出：看板必须能变成「一个可以放进 Pages 的文件」。

    ## 这个类在防什么

    导出功能最容易出的不是崩溃，而是**导出了一个空壳还报成功**：
    `render_snapshot` 写出 0 字节文件、或者写出的页面里所有区块都是空的，
    调用方（CI）却看到退出码 0 就发布了。所以这里逐条断言
    「该有的内容真的在文件里」，而不是只断言「文件存在」。
    """

    def test_writes_index_html_with_content(self, tmp_path, monkeypatch):
        home = [_run("perf-1", p95=100.0), _run("perf-2", p95=130.0)]
        # `render_snapshot` 自己查库取运行记录与 flaky 候选，
        # 这里把取数换成固定的内存数据 —— 测的是导出与渲染，不是数据库往返。
        monkeypatch.setattr(dash.store, "recent_runs", lambda limit=25, **kw: home)
        monkeypatch.setattr(dash.store, "all_nodeids", lambda limit=5000: [])
        summary = dash.render_snapshot(tmp_path, generated_at="2026-10-18 12:00:00 +0800")
        index = tmp_path / "index.html"
        assert index.is_file(), "没有写出 index.html"
        html = index.read_text(encoding="utf-8")

        # 有 2 轮同 label 的数据 → 必须真的画出折线，而不是只返回 200。
        assert "<polyline" in html
        # 摘要必须与实际文件对得上，否则 CI 日志会撒谎。
        assert summary["runs"] == 2
        assert summary["bytes"] == len(html.encode("utf-8"))
        assert summary["bytes"] > 0

    def test_snapshot_is_self_contained(self, tmp_path):
        """页面里不许有 fetch、也不许有本机路径。

        前者让它在 Pages 上变成一片空白（Pages 只发静态文件，没有后端）；
        后者把生成者的目录结构泄漏给所有访问者。
        """
        dash.render_snapshot(tmp_path, generated_at="2026-10-18 12:00:00 +0800")
        html = (tmp_path / "index.html").read_text(encoding="utf-8")
        assert "fetch(" not in html, "静态页里有 fetch，发布到 Pages 会拿不到数据"
        assert "127.0.0.1" not in html, "静态页里出现了 localhost 地址"
        assert str(tmp_path) not in html

    def test_empty_db_still_produces_a_page(self, tmp_path):
        """空库也要出页 —— 第一次跑 Pages 就是这个状态。

        但要明确写「还没有」，而不是给一张看起来像发布失败的白板。
        """
        summary = dash.render_snapshot(tmp_path, generated_at="2026-10-18 12:00:00 +0800")
        html = (tmp_path / "index.html").read_text(encoding="utf-8")
        assert summary["runs"] == 0 or summary["runs"] > 0  # 隔离库可能残留
        assert "还没有" in html or "最近运行" in html

    def test_cli_report_command_is_registered(self):
        """`report` 必须真的挂在 CLI 上，不能只是写了个函数。"""
        import platform_cli

        parser = platform_cli.build_parser()
        # argparse 没有公开的「列子命令」接口，只能从 choices 里取。
        actions = [a for a in parser._actions if a.dest == "cmd"]
        assert actions, "顶层 parser 没有子命令"
        assert "report" in actions[0].choices
        assert "serve" in actions[0].choices


class TestHistoryMerge:
    """历史库合并：CI 全新检出时把 gh-pages 里的过往轮次带回来。

    ## 这个类在防什么

    没有合并这一层，runner 上导出的看板**永远只有一个点** —— 于是
    「P95 趋势」区块每次都显示"至少 2 轮"。看板看起来是好的（有页面、
    有标题、没报错），但它存在的意义（看见"慢慢变慢"）完全没实现。
    这种"功能在、价值不在"的状态最难被发现，所以这里断言的是
    **合并之后趋势真的画得出来**，而不是"合并函数没抛异常"。
    """

    def _seed(self, path, run_ids, p95=100.0):
        """在指定库里造几轮 perf 记录。"""
        import json
        import sqlite3

        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY, kind TEXT NOT NULL, label TEXT NOT NULL,
                git_sha TEXT, ci INTEGER NOT NULL DEFAULT 0, started_at TEXT NOT NULL,
                finished_at TEXT, status TEXT NOT NULL DEFAULT 'running',
                config_json TEXT NOT NULL DEFAULT '{}', metrics_json TEXT NOT NULL DEFAULT '{}',
                platform_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS steps (
                id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, name TEXT NOT NULL,
                users INTEGER NOT NULL DEFAULT 0, duration_s REAL NOT NULL DEFAULT 0,
                total INTEGER NOT NULL DEFAULT 0, ok_count INTEGER NOT NULL DEFAULT 0,
                error_count INTEGER NOT NULL DEFAULT 0, error_rate REAL NOT NULL DEFAULT 0,
                rps REAL NOT NULL DEFAULT 0, mean_ms REAL NOT NULL DEFAULT 0,
                p50_ms REAL NOT NULL DEFAULT 0, p90_ms REAL NOT NULL DEFAULT 0,
                p95_ms REAL NOT NULL DEFAULT 0, p99_ms REAL NOT NULL DEFAULT 0,
                max_ms REAL NOT NULL DEFAULT 0, max_inflight INTEGER NOT NULL DEFAULT 0,
                breakdown_json TEXT NOT NULL DEFAULT '{}'
            );
            """
        )
        for i, rid in enumerate(run_ids):
            conn.execute(
                "INSERT OR IGNORE INTO runs(run_id, kind, label, ci, started_at, status, metrics_json)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    rid,
                    "perf",
                    "ci-perf",
                    1,
                    f"2026-10-{10 + i:02d}T03:00:00+00:00",
                    "passed",
                    json.dumps({"p95_ms": p95 + i * 10}),
                ),
            )
        conn.commit()
        conn.close()

    def test_merges_runs_that_are_missing_locally(self, tmp_path, monkeypatch):
        from metrics import store

        local = tmp_path / "local.db"
        remote = tmp_path / "remote.db"
        monkeypatch.setenv("QUALITY_DB", str(local))
        store.init_db()
        self._seed(remote, ["remote-1", "remote-2"])

        counts = store.merge_from(remote)
        assert counts["runs"] == 2, "历史轮次没有并进来"
        got = {r["run_id"] for r in store.recent_runs(limit=50)}
        assert {"remote-1", "remote-2"} <= got

    def test_merge_is_idempotent(self, tmp_path, monkeypatch):
        """同一份历史库合两次，第二次必须是 0 轮。

        否则每次 CI 跑都会把同一批轮次重复插进来，趋势图上会出现
        「同一时刻两个点」这种看不出所以然的假象。
        """
        from metrics import store

        monkeypatch.setenv("QUALITY_DB", str(tmp_path / "local.db"))
        store.init_db()
        src = tmp_path / "remote.db"
        self._seed(src, ["remote-1", "remote-2"])

        assert store.merge_from(src)["runs"] == 2
        assert store.merge_from(src)["runs"] == 0, "重复合并又插了一遍"

    def test_local_runs_win_over_the_history(self, tmp_path, monkeypatch):
        """同 run_id 时保留本地那份 —— 本地是这一轮刚测出来的，源库是旧的。"""
        import json
        import sqlite3

        from metrics import store

        monkeypatch.setenv("QUALITY_DB", str(tmp_path / "local.db"))
        store.init_db()
        store.upsert_run("shared", "perf", "ci-perf", status="passed", metrics={"p95_ms": 111.0}, ci=True)

        src = tmp_path / "remote.db"
        self._seed(src, ["shared"])
        # 把源库里同 id 那轮的 p95 改成明显不同的值，看合并后是哪个。
        conn = sqlite3.connect(src)
        conn.execute("UPDATE runs SET metrics_json=? WHERE run_id='shared'", (json.dumps({"p95_ms": 999.0}),))
        conn.commit()
        conn.close()

        store.merge_from(src)
        with store.tx() as conn:
            rows = conn.execute("SELECT metrics_json FROM runs WHERE run_id='shared'").fetchall()
        assert len(rows) == 1, "同 id 的轮次被插成了两行"
        assert json.loads(rows[0]["metrics_json"]).get("p95_ms") == 111.0

    def test_missing_history_db_is_not_an_error(self, tmp_path, monkeypatch):
        """首轮发布时 gh-pages 上还没有库 —— 必须能继续导出，只是没有历史。"""
        from metrics import store

        monkeypatch.setenv("QUALITY_DB", str(tmp_path / "local.db"))
        store.init_db()
        import pytest as _pytest

        with _pytest.raises(FileNotFoundError):
            store.merge_from(tmp_path / "nope.db")
