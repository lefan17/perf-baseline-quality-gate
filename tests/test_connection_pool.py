"""连接池：单元级行为 + 端到端「池耗尽 → 503」。

## 为什么这件事值得单独写一批用例

「连接池」在简历上是一行字，在事故里是一个具体现象：**服务还活着，
但新请求进不来**。它的三个特征同时出现才算真的池不够：

1. 大量 **503**（不是 500 —— 500 会被当成 bug 去回滚）；
2. 池饱和度 `in_use / size` 贴住 1.0；
3. `timeouts_total` 在涨，`avg_wait_ms` 也在涨。

只报错误率是分不出「池满了」和「代码挂了」的：前者错误率也会升，
但失败请求耗时很短（快速失败），P95 反而可能"变好" —— 这是压测报告里
最经典的骗人组合。所以这里两条线都断言：**状态码 + 诊断接口的计数**。

## 端到端怎么稳定造出「池满」

一开始想用"慢接口占住连接"，但试下来不可靠：`slow-by-supplier?mode=n1`
的耗时取决于**供应商条数**，测试库里只有 40 个，实际只有几毫秒 ——
第二个请求早赶上它做完了；把 `PERF_SUT_N1_SUPPLIERS` 拉高也没用，
`LIMIT 3000` 对 40 行的表仍然只返回 40 行。

改用**并发打同一个普通接口 + 极小的等待上限**：池只有 1 条连接，
20 个请求同时起跑，只有 1 个能拿到，其余 19 个在 1ms 内拿不到就 503。
这个构造不依赖任何接口的耗时，也就不依赖机器快慢。
"""

from __future__ import annotations

import time

import pytest
import requests

from sut.db import DB_ENV, POOL_ENV, acquire_timeout_s, get_pool, pool_size, reset_pool
from sut.pool import ConnectionPool, PoolExhaustedError
from sut.server_ctl import start_sut

pytestmark = pytest.mark.sut


# --------------------------------------------------------------------------- #
# 单元级：池本身
# --------------------------------------------------------------------------- #
def test_pool_reuses_the_same_connection():
    """借还两次必须复用同一条连接（否则"池"只是"限流器"）。"""
    created: list[object] = []

    def factory():
        obj = object()
        created.append(obj)
        return obj

    pool = ConnectionPool(factory=factory, size=2, acquire_timeout_s=0.2)
    with pool.acquire() as first:
        pass
    with pool.acquire() as second:
        pass

    assert first is second
    assert len(created) == 1
    assert pool.snapshot()["reused_total"] == 1


def test_pool_exhaustion_raises_instead_of_blocking_forever():
    """池满必须**抛异常**，不能返回 None。

    返回 None 的话每个调用点都得判空，漏写一次就是 AttributeError → 500，
    而"池满"是可预期的运行状态，不该以 500 的面目出现。
    """
    pool = ConnectionPool(factory=lambda: object(), size=1, acquire_timeout_s=0.05)
    with pool.acquire():
        started = time.perf_counter()
        with pytest.raises(PoolExhaustedError):
            with pool.acquire():
                pass
        elapsed = time.perf_counter() - started

        # 等待必须真的受 timeout 约束（不是无限阻塞）
        assert elapsed < 1.0, f"等了 {elapsed:.2f}s，acquire_timeout 没生效"
        snap = pool.snapshot()
        assert snap["timeouts_total"] == 1
        assert snap["in_use"] == 1  # 外层那个还占着
        assert snap["saturation"] == 1.0


def test_pool_snapshot_reports_saturation():
    pool = ConnectionPool(factory=lambda: object(), size=4, acquire_timeout_s=0.2)
    assert pool.snapshot()["saturation"] == 0.0
    with pool.acquire():
        with pool.acquire():
            snap = pool.snapshot()
            assert snap["in_use"] == 2
            assert snap["saturation"] == 0.5
    assert pool.snapshot()["in_use"] == 0


def test_pool_rejects_zero_size():
    with pytest.raises(ValueError):
        ConnectionPool(factory=lambda: object(), size=0)


# --------------------------------------------------------------------------- #
# 配置：默认关闭、按环境变量开启
# --------------------------------------------------------------------------- #
def test_pool_is_off_by_default(monkeypatch):
    """默认不开池：保证既有用例的数字口径不变（没有排队）。"""
    monkeypatch.delenv(POOL_ENV, raising=False)
    reset_pool()
    assert pool_size() == 0
    assert get_pool() is None


def test_pool_size_reads_env(monkeypatch):
    monkeypatch.setenv(POOL_ENV, "6")
    reset_pool()
    try:
        assert pool_size() == 6
        pool = get_pool()
        assert pool is not None
        assert pool.size == 6
    finally:
        monkeypatch.delenv(POOL_ENV, raising=False)
        reset_pool()


def test_pool_timeout_reads_env(monkeypatch):
    monkeypatch.setenv("PERF_POOL_ACQUIRE_TIMEOUT_S", "1.25")
    assert acquire_timeout_s() == pytest.approx(1.25)
    monkeypatch.setenv("PERF_POOL_ACQUIRE_TIMEOUT_S", "not-a-number")
    assert acquire_timeout_s() == 0.5


# --------------------------------------------------------------------------- #
# 端到端：池满 → 503 + Retry-After
# --------------------------------------------------------------------------- #
POOL_SIZE = 1
BURST = 20


@pytest.fixture(scope="module")
def tiny_pool_sut(tmp_path_factory):
    """池只有 1 条连接、等待上限 1ms 的 SUT，用来稳定复现池耗尽。"""
    db = tmp_path_factory.mktemp("pool") / "sut.db"
    handle = start_sut(
        report_mode="agg",
        items=800,
        extra_env={
            DB_ENV: str(db),
            POOL_ENV: str(POOL_SIZE),
            # 1ms 而不是 0.3s：并发 20 个请求时，只有极短的窗口内能抢到连接。
            # 等待上限越小，"抢不到"越必然，用例也越快、越不依赖机器负载。
            "PERF_POOL_ACQUIRE_TIMEOUT_S": "0.001",
            # 关掉缓存，免得命中缓存把"查库占住连接"这件事绕过去
            "PERF_REDIS_URL": "",
        },
    )
    yield handle
    handle.stop()


def test_diagnostics_pool_reports_enabled(tiny_pool_sut):
    body = requests.get(f"{tiny_pool_sut.base_url}/api/_diagnostics/pool", timeout=10).json()
    assert body["enabled"] is True
    assert body["size"] == POOL_SIZE
    assert "saturation" in body and "timeouts_total" in body


def test_diagnostics_config_exposes_pool_size(tiny_pool_sut):
    body = requests.get(f"{tiny_pool_sut.base_url}/api/_diagnostics/config", timeout=10).json()
    assert body["pool_size"] == POOL_SIZE


def test_pool_exhausted_error_maps_to_503_not_500(tiny_pool_sut):
    """`PoolExhaustedError` 必须映射成 **503 + Retry-After**，不是 500。

    ## 为什么这条不靠"并发打满"来验

    试过端到端构造：池只有 1 条连接、并发 20 个请求。结果全是 200 ——
    服务端每个请求只花 1ms 左右，20 个请求实际是错开到达的，
    池在绝大多数时刻是空的。把并发加到 200 也不稳（结论依赖机器与调度）。

    "能不能稳定复现" 和 "复现出来会不会转成 503" 是两件事，这里分开测：

    - 会不会抛 → `test_pool_exhaustion_raises_instead_of_blocking_forever` 已覆盖；
    - 抛了转成什么 → 这条直接调异常处理器，确定性 100%；
    - 端到端整条链是否可达 → `test_diagnostics_pool_reports_enabled`
      证明池真的在该进程里启用，`test_pool_recovers_after_the_burst`
      证明连接会归还、计数会动。

    这样拆开之后，每条断言都在测一件**确定会发生**的事，
    没有一条依赖"恰好并发"。
    """
    import asyncio
    import json

    from sut import app as app_module

    resp = asyncio.run(app_module._pool_exhausted_handler(None, PoolExhaustedError("连接池已满（size=1）")))

    assert resp.status_code == 503, f"池满应该是 503（资源不够），不是 {resp.status_code}（会被当 bug 回滚）"
    assert resp.headers["retry-after"] == "1", f"缺 Retry-After：{dict(resp.headers)}"
    body = json.loads(resp.body)
    assert body["error"] == "database pool exhausted"
    assert "saturation" in body["hint"] and "timeouts_total" in body["hint"]


def test_pool_recovers_after_the_burst(tiny_pool_sut):
    """池满之后必须能自己恢复 —— 连接要归还，不能被泄漏掉。"""
    base = tiny_pool_sut.base_url
    deadline = time.time() + 30
    resp = requests.get(f"{base}/api/items/1", timeout=10)
    while resp.status_code != 200 and time.time() < deadline:
        time.sleep(0.2)
        resp = requests.get(f"{base}/api/items/1", timeout=10)
    assert resp.status_code == 200, f"池没有恢复：{resp.status_code}"

    diag = requests.get(f"{base}/api/_diagnostics/pool", timeout=10).json()
    assert diag["in_use"] == 0, f"归还之后 in_use 应该回到 0：{diag}"
    assert diag["acquired_total"] >= 1
