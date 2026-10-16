"""被测服务（SUT）：一个物料的查询 + 统计 + 写入服务。

## 为什么自己写被测服务

压测的结论只有在**能解释每一个百分点**的时候才有价值。用公网站或别人的
服务，量到 P99 抖动时你无法判断是网络、限流还是自己的脚本问题。
这个服务本地起、数据本地落、瓶颈可归因。

## 内置的性能探针（关键设计）

`GET /api/reports/slow-by-supplier` 有**两种实现**，用 `mode` 切换：

* `mode=agg`：一条 SQL `GROUP BY` 聚合，走索引，是"修好之后"的样子；
* `mode=n1`：先查出供应商列表，再**每个供应商发一次查询**在 Python 里累加，
  是典型的 N+1，是"出事故时"的样子。默认值由环境变量
  `PERF_SUT_REPORT_MODE` 决定（默认 `agg`）。

这不是为了造一个假的慢接口，而是把**已知的性能缺陷固化成可以随时复现的探针**——
和 fullstack-test-framework 里那个"故意带 6 个缺陷的登录服务"是同一个思路：
缺陷由清单跟踪，探针负责让"门禁真的能拦住它"这件事可被反复证明，而不是靠嘴说。
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Query, Response

from sut.db import connect

app = FastAPI(title="Inventory Perf SUT", version="1.0.0")

REPORT_MODE = "PERF_SUT_REPORT_MODE"
N1_SUPPLIER_LIMIT = int(os.environ.get("PERF_SUT_N1_SUPPLIERS", "40"))


def _report_mode(explicit: str | None) -> str:
    mode = (explicit or os.environ.get(REPORT_MODE) or "agg").lower()
    if mode not in ("agg", "n1"):
        raise HTTPException(status_code=400, detail=f"unknown mode: {mode}")
    return mode


@app.get("/healthz")
def healthz():
    return {"status": "ok", "report_mode": _report_mode(None)}


@app.get("/api/items")
def list_items(
    category_id: int | None = None,
    supplier_id: int | None = None,
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    where, params = [], []
    if category_id is not None:
        where.append("category_id = ?")
        params.append(category_id)
    if supplier_id is not None:
        where.append("supplier_id = ?")
        params.append(supplier_id)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    conn = connect()
    try:
        total = conn.execute(f"SELECT COUNT(*) AS c FROM items {clause}", params).fetchone()["c"]
        rows = conn.execute(
            f"SELECT id, sku, name, category_id, supplier_id, price_cents, stock"
            f" FROM items {clause} ORDER BY id LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        return {"total": total, "limit": limit, "offset": offset, "items": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.get("/api/items/{item_id}")
def get_item(item_id: int):
    conn = connect()
    try:
        row = conn.execute(
            "SELECT i.*, c.name AS category_name, s.name AS supplier_name"
            " FROM items i"
            " JOIN categories c ON c.id = i.category_id"
            " JOIN suppliers s ON s.id = i.supplier_id"
            " WHERE i.id = ?",
            (item_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="item not found")
        return dict(row)
    finally:
        conn.close()


@app.get("/api/items/{item_id}/ledger")
def item_ledger(item_id: int, limit: int = Query(50, ge=1, le=500)):
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, delta, ts FROM ledger WHERE item_id = ? ORDER BY ts DESC LIMIT ?",
            (item_id, limit),
        ).fetchall()
        return {"item_id": item_id, "entries": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.get("/api/reports/stock-by-category")
def stock_by_category():
    """健康的聚合：一次 SQL，走 idx_items_category。"""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT c.id AS category_id, c.name AS category_name,"
            "       COUNT(i.id) AS item_count, COALESCE(SUM(i.stock), 0) AS stock_total"
            " FROM categories c LEFT JOIN items i ON i.category_id = c.id"
            " GROUP BY c.id, c.name ORDER BY c.id"
        ).fetchall()
        return {"rows": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.get("/api/reports/slow-by-supplier")
def slow_by_supplier(mode: str | None = None, window_days: int = Query(30, ge=1, le=365)):
    """疑似慢接口 —— 两种实现，见模块 docstring。"""
    mode = _report_mode(mode)
    conn = connect()
    try:
        if mode == "agg":
            # 两个预先聚合的子查询再 JOIN，而不是"三张表一路 LEFT JOIN 到底"。
            #
            # 这里踩过一个真实的坑（由 tests/test_sut_api.py 的交叉校验抓出来）：
            # 写成 suppliers LEFT JOIN items LEFT JOIN ledger 之后，
            # 每个物料会按它的流水条数**重复出现**，于是 SUM(i.stock)
            # 把同一个物料的库存重复计了 N 次 —— 实测比真值高 2.23 倍
            # （20 条数据下 113194 vs 50697）。
            # 而 COUNT(DISTINCT i.id) 因为有 DISTINCT 反而是对的，
            # 这种"一个指标对、另一个指标错"的混合状态最难靠肉眼发现。
            #
            # 教训：**聚合的输入必须先收敛到正确的粒度**，
            # 否则 JOIN 的扇出会静默污染所有非 DISTINCT 的聚合。
            rows = conn.execute(
                "SELECT s.id AS supplier_id, s.name AS supplier_name,"
                "       COALESCE(it.item_count, 0) AS item_count,"
                "       COALESCE(it.stock_total, 0) AS stock_total,"
                "       COALESCE(lg.delta_recent, 0) AS delta_recent"
                " FROM suppliers s"
                " LEFT JOIN (SELECT supplier_id, COUNT(*) AS item_count,"
                "                   SUM(stock) AS stock_total"
                "            FROM items GROUP BY supplier_id) it"
                "        ON it.supplier_id = s.id"
                " LEFT JOIN (SELECT i.supplier_id AS supplier_id, SUM(l.delta) AS delta_recent"
                "            FROM ledger l JOIN items i ON i.id = l.item_id"
                "            WHERE l.ts >= ?"
                "            GROUP BY i.supplier_id) lg"
                "        ON lg.supplier_id = s.id"
                " ORDER BY s.id",
                (_cutoff(window_days),),
            ).fetchall()
            return {"mode": "agg", "window_days": window_days, "rows": [dict(r) for r in rows]}

        # mode == "n1"：N+1 查询 —— 供应商查一次，再逐供应商各查两次。
        suppliers = conn.execute(
            "SELECT id, name FROM suppliers ORDER BY id LIMIT ?", (N1_SUPPLIER_LIMIT,)
        ).fetchall()
        cutoff = _cutoff(window_days)
        out = []
        for s in suppliers:
            items = conn.execute(
                "SELECT id, stock FROM items WHERE supplier_id = ?", (s["id"],)
            ).fetchall()
            stock_total = 0
            delta_recent = 0
            for it in items:
                stock_total += it["stock"]
                row = conn.execute(
                    "SELECT COALESCE(SUM(delta), 0) AS d FROM ledger"
                    " WHERE item_id = ? AND ts >= ?",
                    (it["id"], cutoff),
                ).fetchone()
                delta_recent += row["d"]
            out.append(
                {
                    "supplier_id": s["id"],
                    "supplier_name": s["name"],
                    "item_count": len(items),
                    "stock_total": stock_total,
                    "delta_recent": delta_recent,
                }
            )
        return {"mode": "n1", "window_days": window_days, "rows": out}
    finally:
        conn.close()


def _cutoff(window_days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()


@app.post("/api/items/{item_id}/stock", status_code=201)
def adjust_stock(item_id: int, delta: int = Query(...)):
    """写入路径：改库存 + 记流水，一个事务。用于混合读写压测。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT stock FROM items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            raise HTTPException(status_code=404, detail="item not found")
        new_stock = row["stock"] + delta
        if new_stock < 0:
            conn.execute("ROLLBACK")
            raise HTTPException(status_code=409, detail="insufficient stock")
        conn.execute("UPDATE items SET stock = ? WHERE id = ?", (new_stock, item_id))
        conn.execute(
            "INSERT INTO ledger(item_id, delta, ts) VALUES (?, ?, ?)",
            (item_id, delta, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return {"item_id": item_id, "stock": new_stock}
    finally:
        conn.close()


@app.post("/api/orders", status_code=201)
def create_order(item_id: int = Query(...), qty: int = Query(..., ge=1), idempotency_key: str | None = None):
    """带幂等键的下单：同一个 key 重复提交返回同一订单。"""
    key = idempotency_key or str(uuid.uuid4())
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT id, item_id, qty, status FROM orders WHERE idempotency_key = ?", (key,)
        ).fetchone()
        if existing is not None:
            conn.execute("ROLLBACK")
            return dict(existing)
        item = conn.execute("SELECT stock FROM items WHERE id = ?", (item_id,)).fetchone()
        if item is None:
            conn.execute("ROLLBACK")
            raise HTTPException(status_code=404, detail="item not found")
        if item["stock"] < qty:
            conn.execute("ROLLBACK")
            raise HTTPException(status_code=409, detail="insufficient stock")
        conn.execute("UPDATE items SET stock = stock - ? WHERE id = ?", (qty, item_id))
        cur = conn.execute(
            "INSERT INTO orders(item_id, qty, status, idempotency_key, created_at)"
            " VALUES (?, ?, 'CREATED', ?, ?)",
            (item_id, qty, key, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return {"id": cur.lastrowid, "item_id": item_id, "qty": qty, "status": "CREATED"}
    finally:
        conn.close()


@app.get("/api/_diagnostics/indexes")
def diagnostics_indexes(response: Response):
    """报告报表索引是否存在 —— 门禁的 demo 用它证明索引被误删时能被拦住。"""
    conn = connect()
    try:
        names = {
            r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
        }
        expected = {"idx_items_category", "idx_items_supplier", "idx_ledger_item"}
        missing = sorted(expected - names)
    finally:
        conn.close()
    response.headers["X-Report-Mode"] = _report_mode(None)
    return {"present": sorted(names & expected), "missing": missing}


@app.get("/api/_diagnostics/config")
def diagnostics_config():
    """把"这一轮跑的是什么配置"变成可查询的事实。

    压测报告最忌讳的一句话是"环境不同所以结论不同"，却拿不出到底哪里不同。
    这里把影响结果的每个开关都暴露出来，报告可以原样附上。
    """
    return {
        "db": os.environ.get("PERF_SUT_DB", "(default: sut/sut.db)"),
        "report_mode": _report_mode(None),
        "n1_suppliers": N1_SUPPLIER_LIMIT,
        "python": sys.version.split()[0],
        "fastapi": __import__("fastapi").__version__,
        "uvicorn": __import__("uvicorn").__version__,
        "requests": __import__("requests").__version__,
        "platform": sys.platform,
        "cpu_count": os.cpu_count(),
    }
