"""被测服务的数据层。

刻意保持"朴素"：没有 ORM、没有连接池中间件，一个请求一个连接。
这样平台侧量到的耗时归因是清楚的 —— 瓶颈只会来自 SQL 本身、
CPU 计算或人为注入的延迟，而不是框架的抽象成本。
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_ENV = "PERF_SUT_DB"


def db_path() -> Path:
    """数据库位置：环境变量优先（平台跑用例时指向临时目录），否则落在 sut/ 下。"""
    raw = os.environ.get(DB_ENV)
    if raw:
        return Path(raw)
    return Path(__file__).resolve().parent / "sut.db"


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # WAL：读写不互相阻塞，否则压测时读请求会被写请求整体卡住，
    # 量到的"拐点"就只是 SQLite 的写锁排队，不是业务瓶颈。
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def cursor():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


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
