"""被测服务的数据层。

刻意保持"朴素"：没有 ORM。连接默认一个请求一个，**可选**走连接池
（`PERF_POOL_SIZE`），这样平台侧量到的耗时归因仍然清楚 ——
瓶颈只会来自 SQL 本身、CPU 计算、人为注入的延迟，或者池排队。

为什么池是可选的、而且默认关闭：

- 默认关闭保证既有 297 条用例的口径不变（没有池就没有排队，数字可对历史）；
- 打开之后，`/api/_diagnostics/pool` 会给出饱和度与超时计数，
  让"池耗尽"成为可观测现象，而不是一个只能靠现象猜的故障。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from sut.pool import ConnectionPool

DB_ENV = "PERF_SUT_DB"
POOL_ENV = "PERF_POOL_SIZE"
#: 借不到连接时的等待上限（秒）。单独给一个变量，是为了能在压测里
#: 把"快速失败"和"慢慢排队"两种行为分开量。
POOL_TIMEOUT_ENV = "PERF_POOL_ACQUIRE_TIMEOUT_S"


def db_path() -> Path:
    """数据库位置：环境变量优先（平台跑用例时指向临时目录），否则落在 sut/ 下。"""
    raw = os.environ.get(DB_ENV)
    if raw:
        return Path(raw)
    return Path(__file__).resolve().parent / "sut.db"


def _open_connection(*, check_same_thread: bool = True) -> sqlite3.Connection:
    """真正开一条 sqlite 连接（池的 factory 与直连路径共用）。"""
    conn = sqlite3.connect(db_path(), timeout=30, isolation_level=None, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    # WAL：读写不互相阻塞，否则压测时读请求会被写请求整体卡住，
    # 量到的"拐点"就只是 SQLite 的写锁排队，不是业务瓶颈。
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


class _PooledConnection:
    """把池化连接伪装成 `sqlite3.Connection`。

    ## 为什么要这层伪装

    `app.py` 的每个路由都是 `conn = connect()` / `... / conn.close()` 的老写法。
    要让它们走池，只有两条路：

    1. 把 9 个路由全部改成 `with cursor() as conn:` —— 侵入面大，
       而且以后有人新写一个路由很容易忘掉，池就悄悄失效；
    2. 让 `connect()` 返回一个**行为相同的对象**，`close()` 的语义变成"归还"。

    选 2：调用点一行不改，池对所有现存与未来的路由自动生效。
    代价是这个对象不是真的 `sqlite3.Connection`（`isinstance` 会为假），
    所以任何依赖类型判断的代码都得注意 —— 这个仓里没有这种代码。

    `__getattr__` 转发保证了 `execute` / `commit` / `row_factory` 这些
    常用成员不需要逐个代理，也就不会因为漏代理某个方法而悄悄坏掉。
    """

    def __init__(self, pool: ConnectionPool, conn: Any) -> None:
        # 用 object.__setattr__ 绕过下面自定义的 __setattr__，避免自己转发给自己
        object.__setattr__(self, "_pool", pool)
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "_released", False)

    # 读属性转发到真连接
    def __getattr__(self, name: str):
        return getattr(object.__getattribute__(self, "_conn"), name)

    # 写属性（row_factory 之类）也写到真连接上
    def __setattr__(self, name: str, value) -> None:
        setattr(object.__getattribute__(self, "_conn"), name, value)

    def close(self) -> None:
        """归还连接，而不是真的关掉。

        归还前回滚：调用点如果异常退出、没 commit 也没 rollback，
        这条连接上就挂着未提交事务；下一个人借到它就是一条带脏事务的连接，
        这种 bug 在压测数字里表现为"偶发的诡异结果"，极难归因。
        """
        if object.__getattribute__(self, "_released"):
            return
        object.__setattr__(self, "_released", True)

        conn = object.__getattribute__(self, "_conn")
        pool = object.__getattribute__(self, "_pool")
        rollback = getattr(conn, "rollback", None)
        if rollback is not None:
            try:
                rollback()
            except Exception:
                pass
        pool.checkin(conn)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def connect(*, check_same_thread: bool = True) -> sqlite3.Connection:
    """拿一条连接。

    池开启（`PERF_POOL_SIZE`）时返回 `_PooledConnection`（`close()` = 归还），
    否则返回一条新的 `sqlite3.Connection`（`close()` = 真关）。

    两条路径对调用点完全同形 —— 这正是"池可选"能成立的前提：
    打开池只改变资源模型，不改变任何业务代码。
    """
    pool = get_pool()
    if pool is None:
        return _open_connection(check_same_thread=check_same_thread)
    # 池里的连接会被不同线程借走，所以关掉 sqlite 的同线程检查
    # cast：这个对象对外行为等同 Connection（execute/commit/row_factory 全部转发），
    # 但 isinstance 判断会为假。用 cast 而不是把返回类型改成联合类型，
    # 是为了让所有既有调用点继续按 sqlite3.Connection 用，不用改一行。
    return cast(sqlite3.Connection, _PooledConnection(pool, pool.checkout()))


# ---------------------------------------------------------------------- #
# 可选连接池
# ---------------------------------------------------------------------- #

_POOL: ConnectionPool | None = None
_POOL_LOCK = threading.Lock()


def pool_size() -> int:
    """读池大小配置。非正数或 `off/none` 都表示不启用池。"""
    raw = os.environ.get(POOL_ENV, "").strip().lower()
    if not raw or raw in ("0", "off", "none", "false"):
        return 0
    try:
        size = int(raw)
    except ValueError:
        return 0
    return max(0, size)


def acquire_timeout_s() -> float:
    raw = os.environ.get(POOL_TIMEOUT_ENV, "").strip()
    if not raw:
        return 0.5
    try:
        value = float(raw)
    except ValueError:
        return 0.5
    return value if value > 0 else 0.5


def get_pool() -> ConnectionPool | None:
    """按当前环境返回池；大小变了会自动重建（用例里要来回切配置）。"""
    global _POOL
    size = pool_size()
    if size == 0:
        return None

    with _POOL_LOCK:
        if _POOL is None or _POOL.size != size:
            _POOL = ConnectionPool(
                # 池里的连接会被不同线程借用，所以必须关掉 sqlite 的
                # 同线程检查 —— 池本身保证了同一时刻只有一条线程用它。
                # 这里用 _open_connection 而不是 connect()：后者会再走一遍
                # get_pool()，虽然时序上不会递归，但读代码的人得停下来想，
                # 不如直接指向"真正开连接"的那一层。
                factory=lambda: _open_connection(check_same_thread=False),
                size=size,
                acquire_timeout_s=acquire_timeout_s(),
            )
        return _POOL


def reset_pool() -> None:
    """丢掉当前池（测试之间隔离用）。已在借的连接不强制关闭。"""
    global _POOL
    with _POOL_LOCK:
        _POOL = None


@contextmanager
def cursor():
    pool = get_pool()
    if pool is None:
        conn = connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()
        return

    with pool.acquire() as conn:
        # 走池时异常必须回滚：连接会被下一个人复用，
        # 带着未提交事务归还等于把脏数据传染给后续请求。
        try:
            yield conn
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()


SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS suppliers (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY,
    sku         TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    category_id INTEGER NOT NULL REFERENCES categories(id),
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
    price_cents INTEGER NOT NULL,
    stock       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_items_category ON items(category_id);
CREATE INDEX IF NOT EXISTS idx_items_supplier ON items(supplier_id);
CREATE TABLE IF NOT EXISTS ledger (
    id      INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES items(id),
    delta   INTEGER NOT NULL,
    ts      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_item ON ledger(item_id);
CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY,
    item_id         INTEGER NOT NULL REFERENCES items(id),
    qty             INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'CREATED',
    idempotency_key TEXT UNIQUE,
    created_at      TEXT NOT NULL
);
"""


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
