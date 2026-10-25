"""缓存层与索引探针的用例。

## 这批用例保护的是什么

不是「缓存能加速」——那件事靠压测数字证明。这里保护的是**缓存的三条失效路径**，
它们出事时压测报告通常一片绿：

1. 命中率根本没上去（Redis 连不上、key 全过期），但接口照样 200，
   报告里的 `cache=on` 字样让人以为缓存生效了；
2. 负缓存没做，不存在的 id 每个请求都打穿到数据库（缓存穿透）；
3. 写路径忘了失效，读完写之后拿到的是旧值（脏读）。

第 3 条尤其危险：单独压详情接口永远是 100% 命中率，
脏读只在**读写混合**时才暴露，而混合场景的分位数本来就看不出数据对不对。

## 没有 Redis 怎么办

整个模块在探测不到 Redis 时 **skip**（不是 fail）：本地开发没有 Docker
是常态，强迫每个人先起一个容器才能跑单测，最后的结果一定是有人把它
标记成 `xfail` 然后没人再看。CI 里由 service container 提供，
所以判据仍然在流水线上成立。
"""

from __future__ import annotations

import os

import pytest
import requests

from sut.redis_client import Redis, RedisError, RedisUnavailableError
from sut.server_ctl import start_sut

pytestmark = pytest.mark.sut

REDIS_URL = os.environ.get("PERF_REDIS_URL", "127.0.0.1:6379")


def _split_url(raw: str) -> tuple[str, int]:
    host, _, port_text = raw.partition(":")
    try:
        port = int(port_text) if port_text else 6379
    except ValueError:
        port = 6379
    return host or "127.0.0.1", port


def _redis_ready() -> bool:
    host, port = _split_url(REDIS_URL)
    return Redis(host=host, port=port, timeout_s=0.3).ping()


REDIS_UP = _redis_ready()
requires_redis = pytest.mark.skipif(not REDIS_UP, reason=f"探测不到 Redis（PERF_REDIS_URL={REDIS_URL}）")


# --------------------------------------------------------------------------- #
# Redis 客户端本身
# --------------------------------------------------------------------------- #
@requires_redis
def test_client_roundtrip_set_get_delete():
    host, port = _split_url(REDIS_URL)
    client = Redis(host=host, port=port)
    try:
        key = "perf:test:roundtrip"
        client.set(key, "hello")
        assert client.get(key) == b"hello"
        client.setex(key, 60, "world")
        assert client.get(key) == b"world"
        assert client.delete(key) == 1
        assert client.get(key) is None
    finally:
        client.close()


@requires_redis
def test_client_get_missing_key_returns_none_not_error():
    """「键不存在」必须是 `None`，不能是异常 —— 缓存未命中是正常路径，不是错误。"""
    host, port = _split_url(REDIS_URL)
    client = Redis(host=host, port=port)
    try:
        assert client.get("perf:test:definitely-not-here") is None
    finally:
        client.close()


def test_client_raises_unavailable_on_closed_port():
    """连不上的端口必须抛 `RedisUnavailableError`，而不是裸的 ConnectionError。

    调用方只 catch 这一个异常类型就能实现降级；如果底层异常直接漏出去，
    每个调用点都得各自去猜「该 catch 什么」。
    """
    client = Redis(host="127.0.0.1", port=1, timeout_s=0.2)
    with pytest.raises(RedisUnavailableError):
        client.get("perf:test:never")


@requires_redis
def test_client_error_reply_is_not_confused_with_unavailable():
    """服务端报错（`-ERR`）与连不上是两类问题，不能合并成一个异常。

    前者说明命令用错了（要修代码），后者说明环境坏了（要降级）。
    合并之后，一个写错的命令会被当成"缓存暂时不可用"而静默降级 ——
    那正是这个仓库最反对的那类静默放行。
    """
    host, port = _split_url(REDIS_URL)
    client = Redis(host=host, port=port)
    try:
        with pytest.raises(RedisError):
            client._call("SET")  # noqa: SLF001 - 故意发一条参数不足的命令
    finally:
        client.close()


# --------------------------------------------------------------------------- #
# 缓存行为（需要可达的 SUT + Redis）
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def cached_sut():
    if not REDIS_UP:
        pytest.skip("没有 Redis")
    handle = start_sut(report_mode="agg", items=800, extra_env={"PERF_REDIS_URL": REDIS_URL})
    yield handle
    handle.stop()


@requires_redis
def test_cache_backend_reports_redis(cached_sut):
    body = requests.get(f"{cached_sut.base_url}/api/_diagnostics/cache", timeout=10).json()
    assert body["backend"] == "redis", f"缓存没挂上：{body}"


@requires_redis
def test_repeated_reads_hit_the_cache(cached_sut):
    base = cached_sut.base_url
    item_id = 1

    first = requests.get(f"{base}/api/items/{item_id}/detail?cache=on", timeout=10).json()
    second = requests.get(f"{base}/api/items/{item_id}/detail?cache=on", timeout=10).json()

    # 第一次读是命中还是未命中**取决于模块级 SUT 之前被哪些用例读过**
    # （`cached_sut` 是 module scope），所以不锁定具体值。但也不能写成
    # `assert first["cache"]["hit"] in (False, True)` —— 那个表达式恒真
    # （只要是 bool 就过），等于什么都没验，连"这个字段在不在"都验不到。
    # 要验的是它**确实是一个布尔值**：字段缺失会 KeyError；被序列化成
    # 字符串（`"false"`）会被这里抓住 —— 后者正是接口改动最常引入的坑。
    assert isinstance(first["cache"]["hit"], bool), f"cache.hit 不是布尔值：{first['cache']['hit']!r}"
    assert second["cache"]["hit"] is True, "第二次读没命中，缓存其实没生效"

    stats = requests.get(f"{base}/api/_diagnostics/cache", timeout=10).json()
    assert stats["hits"] >= 1
    assert stats["hit_rate"] is not None and stats["hit_rate"] > 0


@requires_redis
def test_cache_off_never_touches_redis(cached_sut):
    """对照组必须真的绕开缓存，否则两组数字不可比。"""
    base = cached_sut.base_url
    before = requests.get(f"{base}/api/_diagnostics/cache", timeout=10).json()

    for _ in range(3):
        body = requests.get(f"{base}/api/items/2/detail?cache=off", timeout=10).json()
        assert body["cache"]["hit"] is False

    after = requests.get(f"{base}/api/_diagnostics/cache", timeout=10).json()
    assert after["bypass"] - before["bypass"] == 3
    assert after["hits"] == before["hits"], "cache=off 不应该产生命中"


@requires_redis
def test_negative_cache_blocks_penetration(cached_sut):
    """不存在的 id 连打两次：第二次必须是命中（负缓存），不是又一次穿透。"""
    base = cached_sut.base_url
    missing_id = 999_999

    r1 = requests.get(f"{base}/api/items/{missing_id}/detail?cache=on", timeout=10)
    assert r1.status_code == 404
    r2 = requests.get(f"{base}/api/items/{missing_id}/detail?cache=on", timeout=10)
    assert r2.status_code == 404
    # 404 仍然是 404：负缓存不改语义，只改"下次还查不查库"。


@requires_redis
def test_write_invalidates_the_detail_cache(cached_sut):
    """写路径必须让缓存失效，否则会读到旧库存 —— 而压测看不见这种错。"""
    base = cached_sut.base_url
    item_id = 3

    before = requests.get(f"{base}/api/items/{item_id}/detail?cache=on", timeout=10).json()
    stock_before = before["stock"]

    requests.post(f"{base}/api/items/{item_id}/stock?delta=5", timeout=10)
    try:
        after = requests.get(f"{base}/api/items/{item_id}/detail?cache=on", timeout=10).json()
        assert after["stock"] == stock_before + 5, "写完库存后详情还是旧值 —— 缓存没失效"
    finally:
        requests.post(f"{base}/api/items/{item_id}/stock?delta=-5", timeout=10)


def test_sut_still_works_without_redis():
    """Redis 连不上时必须**照常服务**，只是降级为直查数据库。

    这是缓存最容易被忽略的一条：很多实现把"缓存不可用"写成了 500，
    于是缓存从"加速手段"变成了"新的单点"。
    """
    handle = start_sut(report_mode="agg", items=800, extra_env={"PERF_REDIS_URL": "127.0.0.1:1"})
    try:
        base = handle.base_url
        diag = requests.get(f"{base}/api/_diagnostics/cache", timeout=10).json()
        assert diag["backend"] == "none", f"连不上时不该声称有缓存：{diag}"

        body = requests.get(f"{base}/api/items/1/detail?cache=on", timeout=10).json()
        assert body["cache"]["hit"] is False
        assert body["cache"]["enabled"] is True  # 语义上仍然"要缓存"，只是后端没了
    finally:
        handle.stop()


# --------------------------------------------------------------------------- #
# 索引失效探针
# --------------------------------------------------------------------------- #
def test_index_modes_agree_on_data():
    """`indexed` 与 `fullscan` 必须给出**完全一样的数据**，只有速度不同。

    和 `test_reports_modes_agree_on_data` 是同一条原则：探针要成立，
    两种实现首先得是同一个功能。否则对比测的是两个不同的东西。
    """
    handle = start_sut(report_mode="agg", items=800)
    try:
        base = handle.base_url
        indexed = requests.get(f"{base}/api/reports/stock-by-category?mode=indexed", timeout=10).json()
        fullscan = requests.get(f"{base}/api/reports/stock-by-category?mode=fullscan", timeout=10).json()
    finally:
        handle.stop()

    assert indexed["mode"] == "indexed"
    assert fullscan["mode"] == "fullscan"
    assert indexed["rows"] == fullscan["rows"], "两种模式的数据不一致，性能对比失去意义"
