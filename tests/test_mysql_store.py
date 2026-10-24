"""MySQL 写路径与连接池的用例。

## 这批用例保护的是什么

不是「MySQL 比 SQLite 快」——那件事由 `scripts/mysql_probe.py` 的数字证明。
这里是四件**出事时压测报告看不出来**的事：

1. 表没建起来 / 数据没灌进去，写请求全部 404，而错误率一栏被当成"存储慢"；
2. 扣到负数没有 409，库存被写穿；
3. 连接坏了还放回池里 —— 表现为「偶发 500、重跑就好」，最难复现的一类；
4. 连不上 MySQL 时接口返回 500 而不是 503，把"资源不可用"记成"代码有 bug"。

第 3 条是这次换真数据库才浮出来的：SQLite 时代连接不会"半死"，
`checkin()` 把连接放回队列就够了；换成 MySQL 之后必须能 `discard()`。

## 没有 MySQL 怎么办

整组 **skip**（不是 fail）：本地没有容器是常态，强迫每个人先起 MySQL
才会去跑单测，结果一定是有人把它标成 `xfail` 然后没人再看。
CI 里由 service container 提供，判据仍然在流水线上成立。
"""

from __future__ import annotations

import os

import pytest
import requests

from sut.mysql_store import (
    MYSQL_PASSWORD_ENV,
    MYSQL_URL_ENV,
    MYSQL_USER_ENV,
    InsufficientStockError,
    MySQLStore,
    MySQLUnavailableError,
    UnknownItemError,
)
from sut.pool import ConnectionPool
from sut.server_ctl import start_sut

pytestmark = pytest.mark.sut

MYSQL_URL = os.environ.get(MYSQL_URL_ENV, "127.0.0.1:3307")


def _mysql_ready() -> bool:
    """真的连一次，而不是探端口：端口开着但认证/库不对，也算连不上。"""
    import pymysql

    host, _, port_text = MYSQL_URL.partition(":")
    try:
        conn = pymysql.connect(
            host=host or "127.0.0.1",
            port=int(port_text or "3306"),
            user=os.environ.get(MYSQL_USER_ENV, "perf"),
            password=os.environ.get(MYSQL_PASSWORD_ENV, "perfpass"),
            database="perf",
            connect_timeout=2,
        )
    except Exception:
        return False
    else:
        conn.close()
        return True


MYSQL_UP = _mysql_ready()
requires_mysql = pytest.mark.skipif(not MYSQL_UP, reason=f"探测不到 MySQL（{MYSQL_URL_ENV}={MYSQL_URL}）")


def _store(pool_size: int = 4, seed_rows: int = 200) -> MySQLStore:
    """直接构造，不走 `build_store()`。

    `build_store()` 读环境变量决定池大小，测试里要精确控制 `pool_size`
    （否则"复用"与"泄漏"两条断言测的就不是自己以为的东西）。
    """
    os.environ[MYSQL_URL_ENV] = MYSQL_URL
    host, _, port_text = MYSQL_URL.partition(":")
    store = MySQLStore(
        {
            "host": host or "127.0.0.1",
            "port": int(port_text or "3306"),
            "user": os.environ.get(MYSQL_USER_ENV, "perf"),
            "password": os.environ.get(MYSQL_PASSWORD_ENV, "perfpass"),
            "database": "perf",
        },
        pool_size=pool_size,
        seed_rows=seed_rows,
    )
    assert store.ping(), "MySQL 明明在探测阶段可达，装配后却 ping 不通"
    return store


# --------------------------------------------------------------------------- #
# 存储层本身
# --------------------------------------------------------------------------- #
@requires_mysql
def test_schema_is_created_and_seeded():
    """表建得出来、行灌得进去，且**第二次调用不会重复灌**。

    刻意不写 `== 200`：这个库在开发机上会被反复跑（探针也往里灌），
    行数只会多不会少。断言"至少这么多"+"再调一次行数不变"，测的是幂等，
    而不是去猜数据库里现在有几行。
    """
    store = _store(seed_rows=200)
    store.ensure_schema()
    assert store.row_count() >= 200
    before = store.row_count()
    store.ensure_schema()
    assert store.row_count() == before, "第二次 ensure_schema 又灌了一遍数据"


@requires_mysql
def test_adjust_stock_round_trip():
    store = _store()
    before = store.get_stock(3)
    assert before is not None
    assert store.adjust_stock(3, 1) == before + 1
    assert store.adjust_stock(3, -1) == before


@requires_mysql
def test_unknown_item_raises_keyerror_subclass():
    """404 的判据必须能和"库存不足"分开，否则接口只能笼统地返回 400。"""
    store = _store()
    with pytest.raises(UnknownItemError):
        store.adjust_stock(999_999, 1)


@requires_mysql
def test_negative_stock_is_rejected_without_writing():
    store = _store()
    current = store.get_stock(5)
    assert current is not None
    with pytest.raises(InsufficientStockError):
        store.adjust_stock(5, -(current + 1))
    # 关键：拒绝必须真的没写进去（rollback），否则"报错了但数据已经坏了"
    assert store.get_stock(5) == current


@requires_mysql
def test_pool_reuses_connections_across_calls():
    store = _store(pool_size=2)
    for _ in range(10):
        store.get_stock(1)
    snapshot = store.snapshot()
    assert snapshot["created_total"] <= 2, f"每次调用都新建了连接：{snapshot}"
    assert snapshot["reused_total"] >= 8, f"复用没生效：{snapshot}"
    assert snapshot["in_use"] == 0, f"借出的连接没归还：{snapshot}"


@requires_mysql
def test_unreachable_mysql_raises_unavailable_not_keyerror():
    """连不上必须是 `MySQLUnavailableError`（→503），不能混成 404/500。"""
    store = MySQLStore(
        {"host": "127.0.0.1", "port": 1, "user": "perf", "password": "perfpass", "database": "perf"},
        pool_size=1,
        acquire_timeout_s=0.2,
        seed_rows=10,
    )
    with pytest.raises(MySQLUnavailableError):
        store.adjust_stock(1, 1)
    # 失败的连接必须被丢弃，不能留在池里等下一个倒霉的人
    assert store.pool.snapshot()["in_use"] == 0


# --------------------------------------------------------------------------- #
# 池的 discard 语义（这次才需要的能力）
# --------------------------------------------------------------------------- #
def test_discard_releases_the_slot_without_recycling():
    """坏连接不能放回队列：下一个借到它的人会莫名其妙报错。"""
    pool = ConnectionPool(lambda: object(), size=1)
    first = pool.checkout()
    pool.discard(first)
    second = pool.checkout()  # 名额必须被释放，否则这里会抛 PoolExhaustedError
    assert second is not first, "discard 之后又把坏连接发出来了"
    pool.checkin(second)
    assert pool.snapshot()["size"] == 1


def test_discard_is_idempotent():
    """重复丢弃不该把 in_use 打成负数 —— 监控数字一错，后面全错。"""
    pool = ConnectionPool(lambda: object(), size=2)
    conn = pool.checkout()
    pool.discard(conn)
    pool.discard(conn)
    assert pool.snapshot()["in_use"] == 0


# --------------------------------------------------------------------------- #
# 端到端：SUT 上的三个新路由
# --------------------------------------------------------------------------- #
@pytest.fixture()
def mysql_sut():
    handle = start_sut(
        report_mode="agg",
        items=300,
        extra_env={
            "PERF_REDIS_URL": "",
            MYSQL_URL_ENV: MYSQL_URL,
            "PERF_MYSQL_ROWS": "300",
            "PERF_MYSQL_POOL_SIZE": "4",
        },
    )
    yield handle
    handle.stop()


@requires_mysql
def test_diagnostics_mysql_reports_reachable(mysql_sut):
    body = requests.get(f"{mysql_sut.base_url}/api/_diagnostics/mysql", timeout=10).json()
    assert body["enabled"] is True, body
    assert body["ping"] is True, body
    # 同样不断言精确值：库是持久的，探针可能已经灌过更多行
    assert body["row_count"] >= 300, body


@requires_mysql
def test_config_exposes_mysql_url(mysql_sut):
    body = requests.get(f"{mysql_sut.base_url}/api/_diagnostics/config", timeout=10).json()
    assert body["mysql_url"] == MYSQL_URL, body


@requires_mysql
def test_http_write_hits_mysql(mysql_sut):
    base = mysql_sut.base_url
    before = requests.get(f"{base}/api/mysql/stock/11", timeout=10).json()
    assert before["stock"] is not None
    created = requests.post(f"{base}/api/mysql/stock/11?delta=1", timeout=10)
    assert created.status_code == 201, created.text
    assert created.json()["stock"] == before["stock"] + 1
    requests.post(f"{base}/api/mysql/stock/11?delta=-1", timeout=10)


@requires_mysql
def test_http_write_rejects_unknown_item_with_404(mysql_sut):
    resp = requests.post(f"{mysql_sut.base_url}/api/mysql/stock/999999?delta=1", timeout=10)
    assert resp.status_code == 404, resp.text


@requires_mysql
def test_http_write_rejects_negative_stock_with_409(mysql_sut):
    base = mysql_sut.base_url
    stock = requests.get(f"{base}/api/mysql/stock/12", timeout=10).json()["stock"]
    resp = requests.post(f"{base}/api/mysql/stock/12?delta={-(stock + 1)}", timeout=10)
    assert resp.status_code == 409, resp.text
    # 拒绝之后必须还能正常读写，说明事务被回滚而不是把连接弄脏了
    after = requests.get(f"{base}/api/mysql/stock/12", timeout=10).json()["stock"]
    assert after == stock
