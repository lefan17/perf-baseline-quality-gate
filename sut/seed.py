"""被测服务的种子数据生成。

数据量可调（环境变量 PERF_SUT_ITEMS），这一点很重要：
"数据量 → 响应时间" 的关系本身就是平台要量的一条基线。
空库压测没有意义 —— 索引全命中、页缓存全热，量到的是假快。
"""

from __future__ import annotations

import os
import random
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sut.db import connect, db_path, init_db  # noqa: E402
from sut.envconf import env_int  # noqa: E402

DEFAULT_ITEMS = 20_000
CATEGORY_NAMES = [
    "紧固件",
    "轴承",
    "密封件",
    "传动件",
    "电气件",
    "气动件",
    "液压件",
    "传感器",
    "线缆",
    "工具",
]
SUPPLIER_NAMES = [f"供应商-{i:03d}" for i in range(1, 41)]


def seed(items: int | None = None, force: bool = False) -> dict:
    """建表并灌入演示数据。已存在数据且未 force 时直接返回现状。

    `items=0` 是**显式请求零条数据**，不是"没传参数"：原先写成
    `items = items or int(...)`，`0` 是 falsy，于是 `seed(items=0)` 会静默
    灌进去两万条 —— 一个想量"空库下接口有多快"的人拿到的是满库的数字，
    而且日志里没有任何提示。`bench/scenarios.py:52` 的 `configure_item_range`
    专门用 `max(1, int(...))` 处理 0，说明这个边界作者是知道的，这里漏了。

    所以判断条件必须是 `is None`，与"没传"严格区分开。
    """
    if items is None:
        items = env_int("PERF_SUT_ITEMS", DEFAULT_ITEMS, minimum=0)
    # 负数一律按 0 处理：`range(-5)` 本来也不产生行，但显式夹住能让
    # 返回体里的 `items` 与真实插入行数一致（否则报 -5 条，看着像 bug）。
    items = max(0, int(items))
    conn = connect()
    try:
        init_db(conn)
        existing = conn.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"]
        if existing and not force:
            return {"seeded": False, "items": existing}

        conn.execute("DELETE FROM ledger")
        conn.execute("DELETE FROM orders")
        conn.execute("DELETE FROM items")
        conn.execute("DELETE FROM categories")
        conn.execute("DELETE FROM suppliers")

        conn.executemany(
            "INSERT INTO categories(id, name) VALUES (?, ?)",
            [(i + 1, n) for i, n in enumerate(CATEGORY_NAMES)],
        )
        conn.executemany(
            "INSERT INTO suppliers(id, name) VALUES (?, ?)",
            [(i + 1, n) for i, n in enumerate(SUPPLIER_NAMES)],
        )

        rnd = random.Random(20261010)
        now = datetime.now(timezone.utc)
        rows = [
            (
                i,
                f"SKU-{i:07d}",
                f"物料-{i:07d}",
                rnd.randint(1, len(CATEGORY_NAMES)),
                rnd.randint(1, len(SUPPLIER_NAMES)),
                rnd.randint(100, 500_000),
                rnd.randint(0, 5000),
            )
            for i in range(1, items + 1)
        ]
        conn.executemany(
            "INSERT INTO items(id, sku, name, category_id, supplier_id, price_cents, stock)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )

        ledger_rows = []
        lid = 1
        for i in range(1, items + 1):
            for _ in range(rnd.randint(1, 3)):
                ledger_rows.append(
                    (lid, i, rnd.randint(-20, 200), (now - timedelta(days=rnd.randint(0, 400))).isoformat())
                )
                lid += 1
        conn.executemany("INSERT INTO ledger(id, item_id, delta, ts) VALUES (?, ?, ?, ?)", ledger_rows)
        conn.commit()
        return {"seeded": True, "items": items, "ledger_rows": len(ledger_rows)}
    finally:
        conn.close()


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else None
    result = seed(items=n, force=True)
    print(f"db     = {db_path()}")
    print(f"result = {result}")
