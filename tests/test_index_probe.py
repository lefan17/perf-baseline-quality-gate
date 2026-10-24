"""索引失效探针：把「索引在 vs 索引不在」的差异固化成可复现的数字。

## 为什么这段测量放在 SQL 层，而不是端到端压测

先说结论：**端到端测不出来，而且不测出来才是诚实的。**

实测（本机，10 万行 / 单连接 / 100 次取中位）：

| 写法 | 耗时 | 执行计划 |
|---|---|---|
| `WHERE category_id = ?`（走索引） | 0.218 ms | `SEARCH items USING COVERING INDEX idx_items_category` |
| `WHERE substr(category_id,1,10) = ?`（索引失效） | 6.337 ms | `SCAN items USING COVERING INDEX idx_items_category` |

**29 倍**。但同一条链路套上 HTTP 之后，单请求固定开销约 25 ms，
6 ms 的差异被压成 10% 左右的抖动 —— 而 `docs/诚实边界.md` 里写得很清楚：
单机压测的 P95 在两轮之间本来就有 3% 量级的 CV。于是「29 倍」这个结论
在端到端数字里会变成「看起来差不多」，然后被人当成噪声忽略。

所以这个探针的判据是**执行计划 + SQL 层耗时**，不是压测 P95：

- 断言 ①：两种写法的 `EXPLAIN QUERY PLAN` 必须一个是 `SEARCH`、一个是 `SCAN`；
- 断言 ②：全表扫描的耗时必须是索引查找的若干倍（默认 ≥ 5 倍，实测 29 倍）。

## 这个探针防的是什么事故

不是「有人写了慢 SQL」，而是**索引被人悄悄删掉**。
`idx_items_category` 一旦缺失，`WHERE category_id = ?` 会退化成 `SCAN`，
而接口照样返回 200、错误率 0%、功能用例全绿 —— 只有性能数字慢慢变差，
而单机压测的噪声足够把它解释掉。

所以断言 ① 检查执行计划，正是为了在「数字还没变差」的阶段就把事故拦住。
这和 `sut/app.py` 的 `/api/_diagnostics/indexes`（报 missing 索引）
是同一个防线的两个观察角度。
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

#: 探针的数据量。
#:
#: 5 万行是「跑得完」与「差异够明显」之间的折中：本机 seed 约 7 秒，
#: 全表扫描约 3 ms、索引查找约 0.12 ms（约 25 倍）。再往下掉到 5 000 行，
#: 全表扫描只需 0.03 ms，差值会小到被 Python 的计时精度吃掉。
ROWS = 50_000

#: 全表扫描至少要比索引查找慢多少倍。取 5 而不是实测的 25：
#: 门槛贴着实测值设，任何一次 CI runner 换型号都会把它变成长期红灯，
#: 而「稳定变红的门槛」最后一定被 continue-on-error 绕过。
MIN_SLOWDOWN = 5.0

INDEXED_SQL = "SELECT COUNT(*) FROM items WHERE category_id = ?"
FULLSCAN_SQL = "SELECT COUNT(*) FROM items WHERE substr(category_id, 1, 10) = CAST(? AS TEXT)"


@pytest.fixture(scope="module")
def big_db(tmp_path_factory) -> str:
    db = tmp_path_factory.mktemp("index-probe") / "sut.db"
    env = {**os.environ, "PERF_SUT_DB": str(db), "PYTHONPATH": str(ROOT)}
    proc = subprocess.run(
        [sys.executable, "-m", "sut.seed", str(ROWS)],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        pytest.fail(f"seed 失败：{proc.stdout}\n{proc.stderr}")
    return str(db)


def _median_ms(conn: sqlite3.Connection, sql: str, rounds: int = 30) -> float:
    samples = []
    for i in range(rounds):
        param = (i % 10) + 1
        t0 = time.perf_counter()
        conn.execute(sql, (param,)).fetchall()
        samples.append((time.perf_counter() - t0) * 1000)
    samples.sort()
    return samples[len(samples) // 2]


def _plan(conn: sqlite3.Connection, sql: str, param: int = 1) -> str:
    """`EXPLAIN QUERY PLAN` 的最后一列是可读描述（`SEARCH ...` / `SCAN ...`）。"""
    rows = conn.execute("EXPLAIN QUERY PLAN " + sql, (param,)).fetchall()
    return " | ".join(str(r[-1]) for r in rows)


def test_indexed_query_uses_search(big_db):
    """走索引的写法必须是 `SEARCH`，不是 `SCAN`。"""
    conn = sqlite3.connect(big_db)
    try:
        plan = _plan(conn, INDEXED_SQL)
    finally:
        conn.close()
    assert "SEARCH" in plan.upper(), f"索引没被用上：{plan}"
    assert "idx_items_category" in plan, f"用的是别的索引：{plan}"


def test_fullscan_query_uses_scan(big_db):
    """被 `substr()` 包住之后必须退化成 `SCAN` —— 这是「索引失效」的定义。"""
    conn = sqlite3.connect(big_db)
    try:
        plan = _plan(conn, FULLSCAN_SQL)
    finally:
        conn.close()
    assert "SCAN" in plan.upper(), f"没有退化成全表扫描：{plan}"
    assert "SEARCH" not in plan.upper(), f"意外地仍然用了索引：{plan}"


def test_fullscan_is_much_slower(big_db):
    """全表扫描必须比索引查找慢 `MIN_SLOWDOWN` 倍以上。

    这条用例在**索引被删掉**时会怎么变？两种写法都会变成 `SCAN`，
    耗时变得接近，`slowdown` 掉到 1 左右 → 变红。
    也就是说它同时守住了「索引失效真的会慢」与「索引别被人删了」两件事。
    """
    conn = sqlite3.connect(big_db)
    try:
        indexed = _median_ms(conn, INDEXED_SQL)
        fullscan = _median_ms(conn, FULLSCAN_SQL)
    finally:
        conn.close()

    slowdown = fullscan / indexed if indexed > 0 else float("inf")
    assert slowdown >= MIN_SLOWDOWN, (
        f"全表扫描只慢了 {slowdown:.1f} 倍（门槛 {MIN_SLOWDOWN}）："
        f"indexed={indexed:.3f}ms fullscan={fullscan:.3f}ms。"
        "若两者接近，先查 idx_items_category 是否还在。"
    )


def test_data_volume_is_enough_to_measure(big_db):
    """行数不够时上面那条会变成"计时噪声"而不是"性能差异"。

    单独断言一次，是因为「差异小」有两种完全不同的原因：
    索引失效没生效（真问题）或者数据量太小（测量条件不足）。
    混在一起会让人对着一个假红灯改代码。
    """
    conn = sqlite3.connect(big_db)
    try:
        rows = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    finally:
        conn.close()
    assert rows >= ROWS, f"数据量只有 {rows} 行，低于探针设定，计时不可信"
