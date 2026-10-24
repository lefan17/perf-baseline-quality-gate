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
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from sut.cache import ItemCache
from sut.db import connect, get_pool, pool_size
from sut.mysql_store import (
    MYSQL_URL_ENV,
    InsufficientStockError,
    MySQLStore,
    MySQLUnavailableError,
    UnknownItemError,
    build_store,
)
from sut.pool import PoolExhaustedError
from sut.redis_client import Redis

app = FastAPI(title="Inventory Perf SUT", version="1.0.0")

REPORT_MODE = "PERF_SUT_REPORT_MODE"
N1_SUPPLIER_LIMIT = int(os.environ.get("PERF_SUT_N1_SUPPLIERS", "40"))

#: Redis 地址。留空 / 设为 `off` 时**不连**，所有缓存路径退化成直查数据库。
REDIS_URL = "PERF_REDIS_URL"


@app.exception_handler(PoolExhaustedError)
async def _pool_exhausted_handler(request: Request, exc: PoolExhaustedError) -> JSONResponse:
    """连接池打满 → **503** + `Retry-After`，而不是 500。

    为什么这个区分很重要：

    - 500 在监控里等于"服务有 bug"，值班的人第一反应是回滚；
    - 503 表示"服务是好的，只是这一下接不住"，正确动作是扩容 / 降并发 / 加排队预算。

    把资源不足伪装成代码错误，是压测报告里最常见的"结论被误读"来源之一：
    P95 会因为大量 503 而看起来"很快"（失败请求耗时短），而错误率被记成
    "服务异常"。两条曲线一起看才会发现是容量问题。

    `Retry-After` 给 1 秒：这个仓的慢查询量级是 10 秒级（`slow-by-supplier`
    的 n1 模式），1 秒足够让客户端退避一轮而不至于瞬间重试把池再打满。
    """
    return JSONResponse(
        status_code=503,
        content={
            "error": "database pool exhausted",
            "detail": str(exc),
            "hint": "pool size 不够或慢查询占住了连接；看 /api/_diagnostics/pool 的 saturation 与 timeouts_total",
        },
        headers={"Retry-After": "1"},
    )


def _report_mode(explicit: str | None) -> str:
    mode = (explicit or os.environ.get(REPORT_MODE) or "agg").lower()
    if mode not in ("agg", "n1"):
        raise HTTPException(status_code=400, detail=f"unknown mode: {mode}")
    return mode


def _build_cache() -> ItemCache:
    """按环境变量装配缓存；连不上就退化成「无缓存」而不是让服务起不来。

    **为什么允许没有 Redis**：这个仓库的三个门禁 job 里只有压测需要缓存，
    而本地开发、`platform_cli suite`、以及不留神少起一个容器的 CI，
    都不该因为「Redis 没起来」而整条流水线红。代价是「降级」这件事必须
    可观测：降级时 `/api/_diagnostics/cache` 的 `backend` 会写成 `none`，
    报告里也据此标注，绝不允许它静默发生。
    """
    raw = (os.environ.get(REDIS_URL) or "").strip()
    if raw.lower() in ("", "off", "none", "0"):
        return ItemCache(None)

    host, _, port_text = raw.partition(":")
    try:
        port = int(port_text) if port_text else 6379
    except ValueError:
        port = 6379

    client = Redis(host=host or "127.0.0.1", port=port)
    if not client.ping():
        # 注意：这里**不抛异常**。起不来的缓存是部署问题，不是接口错误。
        return ItemCache(None)
    return ItemCache(client)


CACHE = _build_cache()


def _build_mysql() -> MySQLStore | None:
    """按环境变量装配 MySQL 写路径；没配 `PERF_MYSQL_URL` 就是 None。

    **与缓存一样不在导入期连库**：`uvicorn` 启动时容器可能还没就绪，
    启动即失败会让整个服务起不来。真正的连接延迟到第一次请求，
    失败也只影响那一个请求（503），不影响其他 8 个 SQLite 路由。
    """
    return build_store()


MYSQL = _build_mysql()


@app.exception_handler(MySQLUnavailableError)
async def _mysql_unavailable_handler(request: Request, exc: MySQLUnavailableError) -> JSONResponse:
    """MySQL 不可用 → 503。

    为什么不是 500：503 的含义是「现在接不住，稍后重试」，500 的含义是
    「代码有 bug」。把连不上数据库记成 500，值班的人会去翻代码而不是看数据库。
    """
    return JSONResponse(
        status_code=503,
        headers={"Retry-After": "1"},
        content={
            "error": "mysql unavailable",
            "detail": str(exc),
            "hint": "看 /api/_diagnostics/mysql 的 ping 与池快照；容器没起来时这里会一直是 false",
        },
    )


@app.exception_handler(UnknownItemError)
async def _mysql_unknown_item_handler(request: Request, exc: UnknownItemError) -> JSONResponse:
    item_id = exc.args[0] if exc.args else None
    return JSONResponse(status_code=404, content={"error": "item not found", "item_id": item_id})


@app.exception_handler(InsufficientStockError)
async def _mysql_insufficient_stock_handler(request: Request, exc: InsufficientStockError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"error": "insufficient stock", "detail": str(exc)})


@app.get("/healthz")
def healthz():
    return {"status": "ok", "report_mode": _report_mode(None)}


@app.get("/api/items")
def list_items(
    category_id: int | None = None,
    supplier_id: int | None = None,
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    index: bool = Query(True, description="false = 把过滤列包进 substr()，故意让 idx_items_category 失效"),
):
    where, params = [], []
    if category_id is not None:
        if index:
            where.append("category_id = ?")
            params.append(category_id)
        else:
            # 索引失效的写法：列被 `substr()` 包住之后，索引里存的原始列值
            # 就对不上了。线上最常见的形态是 `DATE(created_at) = ?` /
            # `LOWER(email) = ?`，这里用 `substr` 只是同一类错误里最直观的一种。
            where.append("substr(category_id, 1, 10) = CAST(? AS TEXT)")
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


def _load_item_detail(item_id: int) -> dict[str, Any] | None:
    """缓存未命中时真正干活的查询。单独抽出来，才能把它交给 `ItemCache.get_or_load`。"""
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
        return dict(row) if row is not None else None
    finally:
        conn.close()


@app.get("/api/items/{item_id}/detail")
def item_detail(
    item_id: int,
    cache: str = Query("on", pattern="^(on|off)$"),
    refresh: bool = False,
):
    """带缓存的物料详情 —— 缓存探针的**主对照组**。

    * `cache=off`：每次都查库。这是"没有缓存时"的样子。
    * `cache=on`：先查 Redis，未命中再查库并回填。
    * `refresh=true`：跳过读但仍然回填，用来量"冷缓存"（每请求都写 Redis 但从不命中）。

    三者配合能回答一个面试常问的问题：**收益到底来自"少查一次库"还是"少建一次连接"**。
    如果 `refresh=true` 与 `cache=off` 差不多慢，说明写缓存的成本吃掉了读的收益。
    """
    enabled = cache == "on"
    value, hit = CACHE.get_or_load(item_id, lambda: _load_item_detail(item_id), enabled=enabled, refresh=refresh)
    if value is None:
        # 注意：这里**不**因为负缓存而改变响应码。404 是正确语义，
        # 负缓存只影响"下次还查不查库"，不影响"这个 id 存不存在"。
        raise HTTPException(status_code=404, detail="item not found")
    value["cache"] = {"enabled": enabled, "hit": hit, "refresh": refresh}
    return value


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
def stock_by_category(mode: str = Query("indexed", pattern="^(indexed|fullscan)$")):
    """按类目统计库存 —— 同时是**索引失效探针**。

    * `mode=indexed`：`JOIN ... ON i.category_id = c.id`，走 `idx_items_category`。
    * `mode=fullscan`：把关联列包进 `substr()` 之后再比较。
      SQLite 的查询规划器**不会**对表达式右边用上该列的索引
      （索引里存的是列值本身，不是 `substr(列值)`），于是退化成全表扫描。

    这不是"为了造慢查询而造"：线上最常见的索引失效原因就是
    「在关联/过滤条件里给列套了函数」——`DATE(created_at) = ?`、
    `LOWER(email) = ?`、`substr(code, 1, 4) = ?` 都属于这一类。
    把它固化成探针，就能用一组数字证明「索引在的时候是多少毫秒、
    不在的时候是多少毫秒」，而不是对着 `EXPLAIN` 空口解释。

    两者的返回结构完全一致，所以对比是公平的（同一份数据、同样的聚合）。
    """
    conn = connect()
    try:
        if mode == "fullscan":
            rows = conn.execute(
                "SELECT c.id AS category_id, c.name AS category_name,"
                "       COUNT(i.id) AS item_count, COALESCE(SUM(i.stock), 0) AS stock_total"
                " FROM categories c LEFT JOIN items i"
                "   ON substr(i.category_id, 1, 10) = CAST(c.id AS TEXT)"
                " GROUP BY c.id, c.name ORDER BY c.id"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT c.id AS category_id, c.name AS category_name,"
                "       COUNT(i.id) AS item_count, COALESCE(SUM(i.stock), 0) AS stock_total"
                " FROM categories c LEFT JOIN items i ON i.category_id = c.id"
                " GROUP BY c.id, c.name ORDER BY c.id"
            ).fetchall()
        return {"mode": mode, "rows": [dict(r) for r in rows]}
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
        suppliers = conn.execute("SELECT id, name FROM suppliers ORDER BY id LIMIT ?", (N1_SUPPLIER_LIMIT,)).fetchall()
        cutoff = _cutoff(window_days)
        out = []
        for s in suppliers:
            items = conn.execute("SELECT id, stock FROM items WHERE supplier_id = ?", (s["id"],)).fetchall()
            stock_total = 0
            delta_recent = 0
            for it in items:
                stock_total += it["stock"]
                row = conn.execute(
                    "SELECT COALESCE(SUM(delta), 0) AS d FROM ledger WHERE item_id = ? AND ts >= ?",
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
        # 缓存失效必须放在 commit **之后**：事务还没落地就删缓存，
        # 中间那一瞬间如果有并发读，会读到旧值并把它重新写回缓存 ——
        # 于是脏数据活得比 TTL 还久。这是缓存一致性最经典的一个坑。
        CACHE.invalidate(item_id)
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
            "INSERT INTO orders(item_id, qty, status, idempotency_key, created_at) VALUES (?, ?, 'CREATED', ?, ?)",
            (item_id, qty, key, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        # 下单也会改库存，所以同样要让详情缓存失效。漏掉这一处的后果很隐蔽：
        # 压测里下单与查详情是不同场景，脏读只会在**跨场景**时暴露 ——
        # 单独压详情永远是 100% 命中率的漂亮数字。
        CACHE.invalidate(item_id)
        return {"id": cur.lastrowid, "item_id": item_id, "qty": qty, "status": "CREATED"}
    finally:
        conn.close()


@app.get("/api/_diagnostics/indexes")
def diagnostics_indexes(response: Response):
    """报告报表索引是否存在 —— 门禁的 demo 用它证明索引被误删时能被拦住。"""
    conn = connect()
    try:
        names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
        expected = {"idx_items_category", "idx_items_supplier", "idx_ledger_item"}
        missing = sorted(expected - names)
    finally:
        conn.close()
    response.headers["X-Report-Mode"] = _report_mode(None)
    return {"present": sorted(names & expected), "missing": missing}


@app.get("/api/_diagnostics/cache")
def diagnostics_cache():
    """把「这一轮到底有没有命中缓存」变成可查询的事实。

    报告里出现 `cache=on` 不代表缓存真的生效了：Redis 连不上、key 全部过期、
    写路径把缓存删得太勤，都会让 `cache=on` 跑出与 `cache=off` 一样的结果。
    这个接口存在的意义就是**让那种情况无法被含糊过去**。
    """
    return CACHE.diagnostics()


@app.get("/api/_diagnostics/pool")
def diagnostics_pool():
    """连接池的饱和度与超时计数。

    「池耗尽」不是一个能靠猜的状态：`saturation` 贴住 1.0 且 `timeouts_total`
    在涨，才是池不够；如果 `saturation` 很低但接口在 503，那是别的问题
    （比如上游挂了），别把两种情况混成一句"数据库扛不住"。
    """
    pool = get_pool()
    if pool is None:
        return {"enabled": False, "size": 0, "note": "PERF_POOL_SIZE 未设置，连接池未启用（一请求一连接）"}
    return {"enabled": True, **pool.snapshot()}


@app.post("/api/mysql/stock/{item_id}", status_code=201)
def adjust_stock_mysql(item_id: int, delta: int = Query(...)):
    """写路径（MySQL）：行锁 → 更新 → 记流水，一个事务。

    与 `/api/items/{id}/stock` 的差别不止是存储：那边用 `BEGIN IMMEDIATE`
    拿**整库写锁**（SQLite 只有这一档），这边用 `SELECT … FOR UPDATE`
    拿**行锁**。同样的防超卖逻辑，两边的并发上限完全不同 —— 这正是
    `stock_write` 在两个后端上量出两个数字的原因。
    """
    if MYSQL is None:
        raise HTTPException(status_code=503, detail="MySQL 未配置（PERF_MYSQL_URL 未设置）")
    new_stock = MYSQL.adjust_stock(item_id, delta)
    return {"item_id": item_id, "stock": new_stock, "backend": "mysql"}


@app.get("/api/mysql/stock/{item_id}")
def get_stock_mysql(item_id: int):
    """读路径（MySQL）：普通快照读，不拿锁。"""
    if MYSQL is None:
        raise HTTPException(status_code=503, detail="MySQL 未配置（PERF_MYSQL_URL 未设置）")
    return {"item_id": item_id, "stock": MYSQL.get_stock(item_id), "backend": "mysql"}


@app.get("/api/_diagnostics/mysql")
def diagnostics_mysql():
    """MySQL 到底通不通、池现在什么状态。

    诊断接口自己**不能因为数据库挂了就 500**：它存在的意义就是在数据库
    出问题时还能回答「是不是数据库的问题」。所以这里把异常吃成
    `ping: false` 加错误文本。
    """
    if MYSQL is None:
        return {"enabled": False, "note": "PERF_MYSQL_URL 未设置，MySQL 写路径未启用"}
    snapshot = MYSQL.snapshot()
    try:
        rows = MYSQL.row_count()
    except Exception as exc:  # noqa: BLE001 —— 见 docstring：诊断接口要把异常变成数据
        return {"enabled": True, "ping": False, "error": str(exc), **snapshot}
    return {"enabled": True, "ping": True, "row_count": rows, **snapshot}


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
        "pool_size": pool_size(),
        "redis_url": os.environ.get(REDIS_URL, "") or "(off)",
        "mysql_url": os.environ.get(MYSQL_URL_ENV, "") or "(off)",
        "python": sys.version.split()[0],
        "fastapi": __import__("fastapi").__version__,
        "uvicorn": __import__("uvicorn").__version__,
        "requests": __import__("requests").__version__,
        "platform": sys.platform,
        "cpu_count": os.cpu_count(),
    }
