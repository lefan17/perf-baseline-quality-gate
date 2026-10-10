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
            {"total": 1000, "error_count": 0, "duration_s": 10.0, "rps": 100.0,
             "mean_ms": 10.0, "p50_ms": 8.0, "p90_ms": 12.0, "p95_ms": 15.0,
             "p99_ms": 20.0, "max_ms": 30.0, "max_inflight": 10},
            {"total": 500, "error_count": 5, "duration_s": 5.0, "rps": 100.0,
             "mean_ms": 20.0, "p50_ms": 16.0, "p90_ms": 24.0, "p95_ms": 40.0,
             "p99_ms": 60.0, "max_ms": 90.0, "max_inflight": 20},
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
            {"total": 100, "error_count": 0, "duration_s": 4.0, "rps": 25.0,
             "mean_ms": 1.0, "p50_ms": 1.0, "p90_ms": 1.0, "p95_ms": 2.0,
             "p99_ms": 3.0, "max_ms": 4.0, "max_inflight": 4},
        ]
        assert platform_cli._merge_from_rows(steps) == analysis.merge_step_rows(steps)
