"""真 MySQL 侧的一条写路径：让「连接池」不再只是 SQLite 的类比。

## 为什么加这个文件

`docs/诚实边界.md` 里有一条自己写的结论：**SQLite 是单写者，写路径的数字
没有参考价值**（`stock_write` 66 RPS / P95 403.9ms，全是 `BEGIN IMMEDIATE`
在排队，不是业务慢）。这句话在面试里是个诚实边界，但它同时也是一个
「你连真实数据库都没接过」的证据。

所以这里补一条真 MySQL 的写路径，用同一套连接池语义：

- 池还是 `sut/pool.py` 里那个自写的 `ConnectionPool`（限流、复用、获取超时、
  归还、丢弃坏连接），只是 factory 从「打开一条 sqlite3 连接」换成
  「`pymysql.connect()`」。**池没有被替换，被替换的是池里装的东西**。
- 事务语义也对齐真实数据库：`SELECT ... FOR UPDATE` 拿行锁 → 更新 → 写流水。
  SQLite 那边用的是 `BEGIN IMMEDIATE`（库级写锁），MySQL 这边是行级锁，
  **同样的「防超卖」在两套存储上不是同一种机制** —— 这个差别面试里很值钱。

## 为什么引 PyMySQL，而池仍然自己写

没有任何人为了演示连接池去手写 MySQL 协议：握手、认证插件、包序号、
字符集协商，全部是「实现了也讲不清」的工程量。**驱动必须用库，池必须自己写**
——后者才是「你知道资源有限」这件事的考点。

PyMySQL 是纯 Python（不需要编译工具链），钉死在 1.1.1，并且由
`scripts/check_deps_sync.py` 保证它同时出现在 `requirements.txt` 与
`pyproject.toml` 里。

## 连不上时怎么办

**不重试、不静默降级。** 抛 `MySQLUnavailableError`，接口映射成 503。
理由：MySQL 连不上时，重试只会让请求堆得更久，最后超时被上游掐断，
监控上看到的是「超时」而不是「数据库不可用」——那会把排查方向带偏。
"""

from __future__ import annotations

import os
import threading
from typing import Any

import pymysql

from sut.envconf import env_int
from sut.pool import ConnectionPool, PoolExhaustedError


def _rollback_quietly(conn: Any) -> None:
    """回滚一条连接上的未提交事务，失败也不抛。

    不能直接用 `ConnectionPool._rollback_quietly`：那是私有的静态方法，
    跨模块调用私有成员会让「池」和「存储」耦合在一处实现细节上。
    这里要的语义只有一行，重复它比穿透封装更便宜。
    """
    rollback = getattr(conn, "rollback", None)
    if rollback is None:
        return
    try:
        rollback()
    except Exception:
        # 回滚失败不能掩盖原始异常：调用方要看到的是"查询失败"。
        pass


#: `host:port`。**不设这个环境变量 = MySQL 未启用**，路由会返回 503 并说明原因。
MYSQL_URL_ENV = "PERF_MYSQL_URL"
MYSQL_USER_ENV = "PERF_MYSQL_USER"
MYSQL_PASSWORD_ENV = "PERF_MYSQL_PASSWORD"
MYSQL_DB_ENV = "PERF_MYSQL_DB"

#: 池大小默认比 SQLite 那个大一点：MySQL 的连接是真实的网络资源，
#: 太小会在低并发下就 503，太大则把锁竞争推给数据库侧。
DEFAULT_POOL_SIZE = 8

#: 建行锁时最多等多久（秒）。0 表示不等、立即报 1205/1213。
#: 这里给 5 秒而不是 0：压测里短暂的行锁等待是正常现象，
#: 设 0 会把「正常竞争」记成失败，把错误率打花。
LOCK_WAIT_TIMEOUT_S = 5

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS item_stock (
        item_id  INT     NOT NULL PRIMARY KEY,
        stock    INT     NOT NULL,
        updated_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                   ON UPDATE CURRENT_TIMESTAMP(6)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS stock_ledger (
        id      BIGINT  NOT NULL AUTO_INCREMENT PRIMARY KEY,
        item_id INT     NOT NULL,
        delta   INT     NOT NULL,
        ts      TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
        KEY idx_stock_ledger_item (item_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
)


class MySQLUnavailableError(RuntimeError):
    """连不上 MySQL，或连接已失效。

    与 `PoolExhaustedError` 分开：那个是**池不够**（扩容/降并发能救），
    这个是**数据库不可用**（要去看服务端）。两者在监控上都表现为 503，
    混成一句「数据库扛不住」等于把排查方向带偏。
    """


class UnknownItemError(KeyError):
    """MySQL 侧不存在这个 item_id（→ 404）。"""


class InsufficientStockError(ValueError):
    """扣减后库存为负（→ 409）。"""


def _config() -> dict[str, Any] | None:
    """读环境变量。没配 `PERF_MYSQL_URL` 就返回 None（= 不启用）。"""
    url = (os.environ.get(MYSQL_URL_ENV) or "").strip()
    if not url:
        return None
    host, _, port_text = url.partition(":")
    return {
        "host": host or "127.0.0.1",
        "port": int(port_text or "3306"),
        "user": os.environ.get(MYSQL_USER_ENV, "perf"),
        "password": os.environ.get(MYSQL_PASSWORD_ENV, "perfpass"),
        "database": os.environ.get(MYSQL_DB_ENV, "perf"),
    }


def _connect(config: dict[str, Any]) -> Any:
    """建一条真连接。

    `autocommit=False` 是刻意的：写路径要「锁 → 改 → 记流水」在一个事务里，
    自动提交会让中间态对外可见。驱动侧的超时交给 `connect_timeout`，
    它与池的 `acquire_timeout_s` 是两件事：池超时是「排不到队」，
    连接超时是「网络不通」，两者都要能分别观测。
    """
    return pymysql.connect(
        host=config["host"],
        port=config["port"],
        user=config["user"],
        password=config["password"],
        database=config["database"],
        charset="utf8mb4",
        autocommit=False,
        connect_timeout=3,
        read_timeout=10,
        write_timeout=10,
        init_command=f"SET SESSION innodb_lock_wait_timeout={LOCK_WAIT_TIMEOUT_S}",
    )


class MySQLStore:
    """一条以 MySQL 为后端的写路径，跑在同一个自写池上。"""

    def __init__(
        self,
        config: dict[str, Any],
        pool_size: int = DEFAULT_POOL_SIZE,
        acquire_timeout_s: float = 0.5,
        seed_rows: int = 1000,
    ) -> None:
        self._config = config
        self._seed_rows = seed_rows
        self.pool = ConnectionPool(lambda: _connect(config), size=pool_size, acquire_timeout_s=acquire_timeout_s)
        self._schema_lock = threading.Lock()
        self._schema_ready = False

    def _checkout(self) -> Any:
        """借一条连接，把"建连失败"统一成 `MySQLUnavailableError`。

        为什么要单独包一层：`pool.checkout()` 在空闲队列为空时会**现场新建**
        连接（`pymysql.connect`），而建连失败抛的是驱动自己的 `OperationalError`。
        如果每个调用点各自 try，迟早有一处漏掉，那处的 500 就会混进 503 里 ——
        「数据库不可用」和「代码有 bug」在监控上必须能分开。
        """
        try:
            return self.pool.checkout()
        except (pymysql.err.OperationalError, pymysql.err.InterfaceError) as exc:
            raise MySQLUnavailableError(str(exc)) from exc

    # ------------------------------------------------------------------ #
    # 初始化
    # ------------------------------------------------------------------ #

    def ensure_schema(self, rows: int | None = None) -> None:
        """建表 + 灌初始库存。只做一次。

        灌数据而不是空表，是因为压测要打真实的随机行；空表会让所有写请求
        都撞在同一个主键上，量出来的是锁竞争而不是写入性能。
        """
        rows = self._seed_rows if rows is None else rows
        with self._schema_lock:
            if self._schema_ready:
                return
            conn = self._checkout()
            broken = False
            try:
                with conn.cursor() as cur:
                    for statement in SCHEMA_STATEMENTS:
                        cur.execute(statement)
                    cur.execute("SELECT COUNT(*) FROM item_stock")
                    row = cur.fetchone()
                    existing = int(row[0]) if row else 0
                    if existing < rows:
                        cur.executemany(
                            "INSERT IGNORE INTO item_stock(item_id, stock) VALUES (%s, %s)",
                            [(i, 1000) for i in range(existing + 1, rows + 1)],
                        )
                conn.commit()
                self._schema_ready = True
            except (pymysql.err.OperationalError, pymysql.err.InterfaceError) as exc:
                # 建表阶段连不上，与写路径是同一类问题，必须抛同一个异常：
                # 否则调用方要分别处理"初始化失败"和"写入失败"，而它们对
                # 上游来说是同一件事 —— 数据库不可用。
                broken = True
                _rollback_quietly(conn)
                raise MySQLUnavailableError(str(exc)) from exc
            except BaseException:
                _rollback_quietly(conn)
                raise
            finally:
                if broken:
                    self.pool.discard(conn)
                else:
                    self.pool.checkin(conn)

    # ------------------------------------------------------------------ #
    # 读写
    # ------------------------------------------------------------------ #

    def adjust_stock(self, item_id: int, delta: int) -> int:
        """`SELECT … FOR UPDATE` → 更新 → 记流水，一个事务。

        行锁是 MySQL 侧与 SQLite 侧最本质的差别：SQLite 的
        `BEGIN IMMEDIATE` 锁的是**整个库的写权限**，所以写并发被压成串行；
        MySQL 只锁这一行，不同 item 的写可以真并发。这就是同样的
        `stock_write` 场景在两套存储上量出两个数字的原因。
        """
        self.ensure_schema()
        conn = self._checkout()
        broken = False
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT stock FROM item_stock WHERE item_id = %s FOR UPDATE", (item_id,))
                row = cur.fetchone()
                if row is None:
                    conn.rollback()
                    raise UnknownItemError(item_id)
                new_stock = int(row[0]) + delta
                if new_stock < 0:
                    conn.rollback()
                    raise InsufficientStockError(f"item {item_id} 库存不足：{row[0]} + ({delta}) < 0")
                cur.execute("UPDATE item_stock SET stock = %s WHERE item_id = %s", (new_stock, item_id))
                cur.execute("INSERT INTO stock_ledger(item_id, delta) VALUES (%s, %s)", (item_id, delta))
            conn.commit()
            return new_stock
        except (pymysql.err.OperationalError, pymysql.err.InterfaceError) as exc:
            # 连接断了 / 被服务端踢掉 / 包序号错乱：这条连接已经不可信，
            # **必须丢弃而不是归还**。还回去的话下一个人拿到它、再报错、
            # 再还回去 —— 一条坏连接能在池里活很久，表现为「随机 500」。
            broken = True
            raise MySQLUnavailableError(str(exc)) from exc
        except BaseException:
            _rollback_quietly(conn)
            raise
        finally:
            if broken:
                self.pool.discard(conn)
            else:
                self.pool.checkin(conn)

    def get_stock(self, item_id: int) -> int | None:
        """读一条库存。读路径不走 `FOR UPDATE`：普通快照读，不拿锁。"""
        self.ensure_schema()
        conn = self._checkout()
        broken = False
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT stock FROM item_stock WHERE item_id = %s", (item_id,))
                row = cur.fetchone()
            return int(row[0]) if row else None
        except (pymysql.err.OperationalError, pymysql.err.InterfaceError) as exc:
            broken = True
            raise MySQLUnavailableError(str(exc)) from exc
        finally:
            if broken:
                self.pool.discard(conn)
            else:
                self.pool.checkin(conn)

    # ------------------------------------------------------------------ #
    # 观测
    # ------------------------------------------------------------------ #

    def ping(self) -> bool:
        """连一次试试。诊断接口用它回答「到底通不通」，而不是靠猜。

        注意这里的写法：**不能**在 `try` 里直接 `return True` 然后在 `else`
        里归还 —— `return` 会跳过 `else`，连接就漏在池里了（`in_use` 永远
        少不下来，压到池大小之后所有请求都拿不到连接）。这个 bug 是这批
        用例第一次跑就抓到的。
        """
        try:
            conn = self._checkout()
        except (PoolExhaustedError, MySQLUnavailableError):
            # 两种"不通"都算 ping 失败：池满（表明显有请求在排队）
            # 与建连失败（数据库没了）。诊断接口要的是一个是/否，不是异常。
            return False
        ok = False
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            ok = True
        except Exception:
            self.pool.discard(conn)
        else:
            self.pool.checkin(conn)
        return ok

    def row_count(self) -> int:
        self.ensure_schema()
        conn = self._checkout()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM item_stock")
                row = cur.fetchone()
            return int(row[0]) if row else 0
        finally:
            self.pool.checkin(conn)

    def snapshot(self) -> dict:
        return {
            "url": f"{self._config['host']}:{self._config['port']}",
            "database": self._config["database"],
            "schema_ready": self._schema_ready,
            **self.pool.snapshot(),
        }


def build_store() -> MySQLStore | None:
    """按环境变量决定要不要启用 MySQL。未配置 → None。

    刻意**不在导入期连库**：`uvicorn` 启动时数据库可能还没起来
    （compose 里常见），启动即失败会让整个服务起不来。
    真正的连接延迟到第一次请求，失败也只是那一个请求 503。
    """
    config = _config()
    if config is None:
        return None
    # 这两个值按上面 docstring 的承诺必须容错：`PERF_MYSQL_POOL_SIZE=abc`
    # 原本在这里 ValueError，而 `build_store()` 是在 `sut/app.py` 的**导入期**
    # 被调用的 —— 于是一个填错的池大小会让整个被测服务起不来。
    size = env_int("PERF_MYSQL_POOL_SIZE", DEFAULT_POOL_SIZE, minimum=1)
    seed_rows = env_int("PERF_MYSQL_ROWS", 1000, minimum=1)
    return MySQLStore(config, pool_size=size, seed_rows=seed_rows)
