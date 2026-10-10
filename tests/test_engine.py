"""压测引擎自身的测试。

引擎是"度量"的来源，它自己错了，后面所有的判断都是错的。
所以这里测的不是"能不能发出请求"，而是**统计口径是否精确**：
分位数取的是哪个样本、合并时权重怎么算、超时算不算错误。
"""

from __future__ import annotations

import pytest

from bench.engine import Sample, merge_stats, percentile, run_api_call, summarize
from bench.scenarios import configure_item_range, item_id_max

pytestmark = pytest.mark.engine


# --------------------------------------------------------------------------- #
# 场景参数必须跟随实际数据量
# --------------------------------------------------------------------------- #
def test_item_range_follows_configured_count():
    """物料的随机 ID 上限必须等于实际灌入的条数。

    这里曾经写死 20 000，制造了一个很隐蔽的假故障：
    用 `--items 5000` 跑时约 75% 的点查打到不存在的 ID 拿到 404，
    门禁 R1 报出「错误率 28% → 拦住」。

    它之所以危险，是因为**看起来像被测服务的缺陷** ——
    结果里只有一个错误率数字，不会有人怀疑是压测脚本发错了请求。
    **压测工具凭空造出错误率，比它测不出问题更糟。**
    """
    original = item_id_max()
    try:
        configure_item_range(5_000)
        assert item_id_max() == 5_000
        configure_item_range(1)
        assert item_id_max() == 1, "至少为 1，不能退化成 randint(1, 0)"
    finally:
        configure_item_range(original)


def test_item_range_is_clamped_to_at_least_one():
    """传 0 或负数不能让 randint 崩掉（无数据的 SUT 是合法场景）。"""
    original = item_id_max()
    try:
        configure_item_range(0)
        assert item_id_max() >= 1
    finally:
        configure_item_range(original)


# --------------------------------------------------------------------------- #
# 分位数：nearest-rank 的定义必须是"返回真实观测值"
# --------------------------------------------------------------------------- #
def test_percentile_returns_observed_value():
    values = [float(i) for i in range(1, 101)]  # 1..100
    assert percentile(values, 50) in values
    assert percentile(values, 95) in values
    assert percentile(values, 99) in values
    # nearest-rank: ceil(0.95*100)-1 = 94 → 第 95 个值
    assert percentile(values, 95) == 95.0
    assert percentile(values, 99) == 99.0
    assert percentile(values, 50) == 50.0


def test_percentile_edges():
    assert percentile([], 95) == 0.0            # 空集合不抛异常，压测失败时也要能出报告
    assert percentile([7.0], 99) == 7.0         # 单样本：P99 就是它自己
    assert percentile([1.0, 2.0], 0) == 1.0
    assert percentile([1.0, 2.0], 100) == 2.0
    assert percentile([3.0, 1.0, 2.0], 100) == 3.0  # 输入乱序也要对


def test_percentile_is_not_interpolated():
    """两个样本之间不该插出新数字 —— 这正是选 nearest-rank 的理由。"""
    values = [10.0, 20.0]
    assert percentile(values, 25) in values
    assert percentile(values, 75) in values


# --------------------------------------------------------------------------- #
# 汇总口径
# --------------------------------------------------------------------------- #
def _s(status=200, ms=10.0, ok=True, err="", scenario="x", at=1.0):
    return Sample(scenario=scenario, status=status, elapsed_ms=ms, ok=ok, error=err, at=at)


def test_summarize_counts_errors_not_just_non_200():
    samples = [_s() for _ in range(8)] + [_s(status=500, ok=False, err="HTTP 500") for _ in range(2)]
    st = summarize(samples, label="10u", users=10, duration_s=10.0)
    assert st.total == 10
    assert st.ok_count == 8
    assert st.error_count == 2
    assert st.error_rate == pytest.approx(0.2)
    assert st.rps == pytest.approx(1.0)
    assert st.rps_per_user == pytest.approx(0.1)
    assert st.status_breakdown == {"200": 8, "500": 2}
    assert st.error_breakdown == {"HTTP 500": 2}


def test_summarize_error_breakdown_groups_by_prefix():
    """错误按 '类型: 细节' 的前缀归类，否则每个 URL 都会变成一类。"""
    samples = [
        _s(ok=False, err="HTTP 500: /a"),
        _s(ok=False, err="HTTP 500: /b"),
        _s(ok=False, err="Timeout: /a"),
    ]
    st = summarize(samples, duration_s=3.0)
    assert st.error_breakdown == {"HTTP 500": 2, "Timeout": 1}


def test_summarize_empty_run_is_safe():
    st = summarize([], label="0u", users=5, duration_s=1.0)
    assert st.total == 0
    assert st.p95_ms == 0.0
    assert st.error_rate == 0.0
    assert st.rps == 0.0


def test_as_dict_drops_samples():
    """入库时不能把几万个样本带进 JSON —— 那是内存和体积的双重浪费。"""
    st = summarize([_s() for _ in range(3)], label="3u", users=3, duration_s=1.0)
    d = st.as_dict()
    assert "samples" not in d
    assert d["total"] == 3


def test_merge_stats_weights_mean_by_request_count():
    """合并平均耗时必须按请求数加权。

    直接对两个梯度的 mean 取算术平均是错的：10 并发那一步可能只有
    100 个请求，它的平均值凭什么和 40 并发那一步的 5000 个请求一样重？
    """
    a = summarize([_s(ms=10.0) for _ in range(100)], label="10u", users=10, duration_s=10.0)
    b = summarize([_s(ms=20.0) for _ in range(900)], label="90u", users=90, duration_s=10.0)
    merged = merge_stats([a, b], label="all")
    assert merged.total == 1000
    assert merged.mean_ms == pytest.approx((10.0 * 100 + 20.0 * 900) / 1000)
    assert merged.duration_s == pytest.approx(20.0)
    assert merged.users == 90  # 取最大并发，代表这一轮的峰值


# --------------------------------------------------------------------------- #
# 请求口径
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, status_code):
        self.status_code = status_code


class _Session:
    def __init__(self, outcome):
        self.outcome = outcome

    def request(self, method, url, timeout=None, **kw):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return _Resp(self.outcome)


def test_run_api_call_expected_status_variants():
    s = run_api_call(_Session(201), "http://x", "POST", "/a", expect_status=201)
    assert s.ok and s.status == 201
    # 接受元组：POST 创建返回 201、幂等命中返回 200，都是成功
    s = run_api_call(_Session(200), "http://x", "POST", "/a", expect_status=(200, 201))
    assert s.ok
    s = run_api_call(_Session(500), "http://x", "GET", "/a")
    assert not s.ok and s.error == "HTTP 500"


def test_run_api_call_records_exception_as_failed_sample():
    """连不上也算一个样本。丢掉它会让错误率看起来是 0 —— 服务全挂了却全绿。"""
    s = run_api_call(_Session(ConnectionError("refused")), "http://x", "GET", "/a")
    assert not s.ok
    assert s.status == 0
    assert s.error == "ConnectionError"
    assert s.elapsed_ms >= 0


def test_run_api_call_measures_elapsed():
    s = run_api_call(_Session(200), "http://x", "GET", "/a")
    assert s.elapsed_ms >= 0
    assert s.scenario == "GET /a"
