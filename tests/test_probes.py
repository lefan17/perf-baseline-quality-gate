"""性能缺陷探针 —— 必须在**真实数据量**下运行。

## 这个文件是怎么来的（一次真实的误判）

最初这两条探针写在 `tests/test_sut_api.py` 里，用 800 条数据跑。结果是：

    探针失效：agg=31.7ms  n1=32.4ms  比值=1.02
    点查 16.2ms / 报表 30.9ms          比值=1.9

而同样的探针在 20000 条数据下是：

    2 000 条：  agg=6.0ms   n1=17.7ms   比值=2.94
    20 000 条： agg=38.2ms  n1=219.2ms  比值=5.73

**同一份代码，同一台机器，只换了数据量，缺陷就从"几乎不存在"
变成"非常明显"。**

原因是 N+1 的代价正比于"每个供应商下挂的物料数"：
800 条数据时每个供应商下才 20 个物料，多查 20 次的开销被 SQLite 的
页缓存完全吸收；20000 条时是 500 个物料，查询次数直接涨 25 倍。

## 第二条教训：比例型断言对测量方法极度敏感

`test_probe_report_is_slowest_read_endpoint` 也曾红过一次，
原因不是服务变快了，而是**没热身**：不热身时点查 15.7ms、
报表 47.2ms（3.0×）；热身 3 次并复用连接后是 3.6ms vs 38.2ms（10.7×）。

不热身时每个接口都背上约 12ms 的固定成本（TCP 建连 + 页缓存冷 +
uvicorn 惰性导入）。这个固定成本加在分母（快接口）上的占比
远大于加在分子（慢接口）上，于是**比例被系统性地压低了**。
所以 `_collect()` 里的热身不是可选项。

## 探针的断言策略

断言的是**性质**（"n1 明显更慢"、"差距随数据量放大"），不是**具体数字**。
写死 "n1 = 210ms" 的断言换台机器就红，而一条会随机变红的用例
唯一的结果是训练所有人忽略红灯。
"""

from __future__ import annotations

import time

import pytest
import requests

from sut.server_ctl import start_sut

pytestmark = [pytest.mark.sut, pytest.mark.perf]

PROBE_ITEMS = 20_000
SCALE_ITEMS = 2_000
SAMPLES = 15
WARMUP = 3


def _collect(base_url: str, path: str, n: int = SAMPLES) -> list[float]:
    """打 n 次并返回耗时列表（已热身）。

    **热身不是可选项。** 这里踩过一个坑：不加热身时点查实测 15.7ms，
    加 3 次热身 + 复用连接后是 3.6ms —— 差 4 倍多。冷启动量到的是
    TCP 建连、首个请求的页面缓存未命中、以及 uvicorn 的惰性导入，
    不是接口本身的开销。

    所以"报表比点查慢 5 倍"这条断言在无热身的口径下会失败（实测 3.0×），
    而真相是 10.7×。**热身不足会系统性地低估快接口、高估慢接口的比例**，
    因为它给每个接口都加了一个约 12ms 的固定成本，而这个成本
    在分母（快接口）上占比更大。
    """
    session = requests.Session()
    try:
        for _ in range(WARMUP):
            session.get(f"{base_url}{path}", timeout=30)
        times = []
        for _ in range(n):
            t0 = time.perf_counter()
            resp = session.get(f"{base_url}{path}", timeout=30)
            assert resp.status_code == 200, f"{path} 返回 {resp.status_code}"
            times.append((time.perf_counter() - t0) * 1000)
        times.sort()
        return times
    finally:
        session.close()


def _median_ms(mode: str, items: int, path: str = "/api/reports/slow-by-supplier") -> float:
    """起服务、热身、打 N 次、取中位数耗时。

    用中位数而不是 P95：N=15 时"P95"按 nearest-rank 就是最大值，
    一次 GC 停顿就能把它抬高一倍，探针就会开始随机红。
    中位数对单次离群点免疫，适合这种"判断性质"的场合
    （正式的 P95/P99 由压测引擎在几千个样本上算，见 bench/engine.py）。
    """
    handle = start_sut(report_mode=mode, items=items)
    try:
        times = _collect(handle.base_url, path)
        return times[len(times) // 2]
    finally:
        handle.stop()


def _ratio_at_scale(items: int) -> tuple[float, float, float]:
    agg = _median_ms("agg", items)
    n1 = _median_ms("n1", items)
    return agg, n1, n1 / max(agg, 0.1)


def test_probe_n1_is_slower_than_agg_at_realistic_scale():
    """探针 1：N+1 实现在真实数据量下明显更慢。

    **当前状态：缺陷存在（预期通过）。**

    阈值取 1.5× 而不是实测的 5.7×：留出余量。探针要证明的是
    "N+1 是个真实存在的性能问题"，不是"这台机器上它恰好慢 5.7 倍"。
    """
    agg, n1, ratio = _ratio_at_scale(PROBE_ITEMS)
    assert ratio > 1.5, (
        f"N+1 探针失效：{PROBE_ITEMS} 条数据下 agg={agg:.1f}ms n1={n1:.1f}ms 比值={ratio:.2f}。\n"
        "若 N+1 已被修复，请更新 docs/已知问题.md 并同步调整 gate_rules.yaml 的 probe 阈值。"
    )


def test_probe_gap_widens_with_data_volume():
    """探针 2：N+1 的代价随数据量放大 —— 这正是它必须在大数据量下测的证据。

    **当前状态：缺陷存在（预期通过）。**

    这条探针的价值不在"N+1 慢"，而在"它慢多少取决于数据量"。
    N+1 的查询次数 = 供应商数 × 每个供应商的物料数，后者随数据量线性增长；
    而 agg 只扫一遍物料表，增长曲线平缓得多。两条曲线的斜率差，
    就是这条断言的依据。

    实测：2 000 条时 2.9×，20 000 条时 5.7×。

    如果一个"N+1 优化"只在 2000 条数据下验证过，那它证明不了什么 ——
    这正是本用例存在的原因。
    """
    agg_small, n1_small, ratio_small = _ratio_at_scale(SCALE_ITEMS)
    agg_big, n1_big, ratio_big = _ratio_at_scale(PROBE_ITEMS)

    assert ratio_big > ratio_small, (
        f"预期差距随数据量放大，实测反而收窄：\n"
        f"  {SCALE_ITEMS} 条：agg={agg_small:.1f}ms n1={n1_small:.1f}ms 比值={ratio_small:.2f}\n"
        f"  {PROBE_ITEMS} 条：agg={agg_big:.1f}ms n1={n1_big:.1f}ms 比值={ratio_big:.2f}\n"
        "若 N+1 已被改为按需 JOIN，本用例应随之删除或改写。"
    )
    assert n1_big > n1_small, (
        f"n1 耗时未随数据量增长（{n1_small:.1f}ms → {n1_big:.1f}ms），"
        "怀疑数据没真正灌进去或查询被缓存了。"
    )


def test_probe_report_is_slowest_read_endpoint():
    """探针 3：报表是全站最慢的读接口。

    **当前状态：缺陷存在（预期通过）。**

    实测（20 000 条，双方都充分热身）：点查 3.6ms / 报表 38.2ms = **10.7×**。

    这条用例最初写成"不热身、直接打 15 次"，结果量出 15.7ms vs 47.2ms
    只有 3.0×，**用例红了**。复盘发现是方法问题而非服务问题：

    不热身时每个接口都背上约 12ms 的固定成本（TCP 建连 + 页缓存冷 +
    uvicorn 惰性导入），这个成本加在分母（3.6ms 的点查）上占比远大于
    加在分子（38ms 的报表）上，于是比例被系统性地压低了。

    **教训：比例型断言对测量方法极度敏感，热身不足会低估真实差距。**
    阈值留在 5×，因为这条断言要证明的是"报表明显更重"这个性质，
    不是"恰好 10.7 倍"这个数字。
    """
    handle = start_sut(report_mode="agg", items=PROBE_ITEMS)
    try:
        detail = _collect(handle.base_url, "/api/items/1")
        report = _collect(handle.base_url, "/api/reports/slow-by-supplier")
    finally:
        handle.stop()

    detail_med = detail[len(detail) // 2]
    report_med = report[len(report) // 2]
    assert report_med > detail_med * 5, (
        f"点查 {detail_med:.1f}ms / 报表 {report_med:.1f}ms（{PROBE_ITEMS} 条数据，均已热身），"
        "报表不再显著慢于点查 —— 要么聚合被优化了，要么索引/数据出了问题。"
    )
