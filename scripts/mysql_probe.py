r"""写路径探针：同一个写动作，SQLite vs MySQL。

## 这个探针回答什么

`docs/诚实边界.md` 里记过一条自我限制：**SQLite 是单写者，写路径的数字
没有参考价值**。`stock_write` 量出来的 66 RPS / P95 403.9ms 不是业务慢，
是 `BEGIN IMMEDIATE` 在把一个库的写权限排成队。

这个探针把同一件事在真 MySQL 上再量一次：

* **SQLite**：`POST /api/items/{id}/stock`，事务用 `BEGIN IMMEDIATE`（整库写锁）；
* **MySQL**：`POST /api/mysql/stock/{id}`，事务用 `SELECT … FOR UPDATE`（行锁）。

两个场景的请求形状**完全相同**（`+1` 再 `-1`，两次往返算一个业务动作），
所以差值是「存储与锁粒度」这一项的，不是请求数或代码路径的。

## 怎么读这两个数字

- 「同样的并发下 SQLite 上不去」= 单写者瓶颈的证据，与业务逻辑无关；
- 「MySQL 上去了」不等于「MySQL 一定更快」：它只是把排队从**进程内**
  挪到了**数据库行锁**上。并发再高，MySQL 也会有它的拐点。

能外推的是**方向**（锁粒度决定写并发上限），不能外推的是**绝对值**
（本机、同一个 box、容器与压测客户端抢 CPU）。

## 用法

```powershell
$env:PYTHONIOENCODING="utf-8"
# 先起一个 MySQL（3307 是为了不和本机已有的 3306 打架）
docker run -d --name perf-mysql `
  -e MYSQL_ROOT_PASSWORD=perfroot -e MYSQL_DATABASE=perf `
  -e MYSQL_USER=perf -e MYSQL_PASSWORD=perfpass `
  -p 3307:3306 mysql:8.0 --default-authentication-plugin=mysql_native_password

& ".\.venv\Scripts\python.exe" scripts\mysql_probe.py --users 10 --duration 8
```

MySQL 连不上时**不报错**，只打印一条怎么起的提示并以 0 退出 ——
和 `proxy_probe.py` 一样：这是附加证据，不该成为跑仓库的门槛。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench.engine import Runner, Stats  # noqa: E402
from bench.scenarios import bind, configure_item_range, stock_write, stock_write_mysql  # noqa: E402
from sut.mysql_store import MYSQL_PASSWORD_ENV, MYSQL_URL_ENV, MYSQL_USER_ENV  # noqa: E402
from sut.server_ctl import start_sut  # noqa: E402

DEFAULT_MYSQL_URL = "127.0.0.1:3307"


def _mysql_url() -> str:
    return (os.environ.get(MYSQL_URL_ENV) or DEFAULT_MYSQL_URL).strip()


def _probe_mysql(url: str) -> bool:
    """用与 SUT 相同的驱动连一次，回答「到底通不通」。

    不用 socket 探端口：端口开着但认证失败、库不存在，都是「连不上」，
    而这个探针要的是后者那个结论。
    """
    import pymysql

    host, _, port_text = url.partition(":")
    try:
        conn = pymysql.connect(
            host=host or "127.0.0.1",
            port=int(port_text or "3306"),
            user=os.environ.get(MYSQL_USER_ENV, "perf"),
            password=os.environ.get(MYSQL_PASSWORD_ENV, "perfpass"),
            database="perf",
            connect_timeout=3,
        )
    except Exception as exc:  # noqa: BLE001 —— 探针要把任何连不上都归成"跳过"
        print(f"[mysql-probe] 连不上 {url}：{exc}")
        return False
    else:
        conn.close()
        return True


def _measure(fn, base_url: str, users: int, duration_s: float) -> Stats:
    """用与平台同一套引擎量一个口径，保证两个数字可比。"""
    runner = Runner(
        base_url=base_url,
        scenarios=[(bind(fn, base_url), 1)],
        users=users,
        duration_s=duration_s,
        warmup_drop_s=1.0,
    )
    return runner.run()


def _fmt(stats: Stats) -> str:
    return (
        f"P50 {stats.p50_ms:7.2f}ms  P95 {stats.p95_ms:7.2f}ms  "
        f"P99 {stats.p99_ms:7.2f}ms  RPS {stats.rps:7.1f}  "
        f"错误率 {stats.error_rate * 100:5.2f}%"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="写路径探针：同一个写动作，SQLite vs MySQL")
    parser.add_argument("--items", type=int, default=5_000, help="SUT 数据量（SQLite 与 MySQL 用同一档）")
    parser.add_argument("--users", type=int, default=10, help="并发用户数")
    parser.add_argument("--duration", type=float, default=8.0, help="每个口径的持续秒数")
    parser.add_argument("--mysql-url", default=None, help=f"host:port，默认 {DEFAULT_MYSQL_URL}")
    args = parser.parse_args(argv)

    url = (args.mysql_url or _mysql_url()).strip()
    if not _probe_mysql(url):
        print("[mysql-probe] 跳过。起一个 MySQL 再跑：")
        print("  docker run -d --name perf-mysql -e MYSQL_ROOT_PASSWORD=perfroot \\")
        print("    -e MYSQL_DATABASE=perf -e MYSQL_USER=perf -e MYSQL_PASSWORD=perfpass \\")
        print("    -p 3307:3306 mysql:8.0 --default-authentication-plugin=mysql_native_password")
        return 0

    print(f"[mysql-probe] MySQL = {url}，数据量 {args.items}，{args.users} 并发，每口径 {args.duration}s")
    # 必须告诉场景真的有多少条数据。`configure_item_range` 不调用时默认 20000：
    # 数据量设成 5000 却按 20000 取 ID，大部分请求会打到不存在的 item 上，
    # 得到 404 —— 而 404 比写事务快得多，于是「写路径变快了」。
    # 这个坑我第一次跑就踩了（SQLite 口径 72% 错误率、P95 反而 244ms）。
    configure_item_range(args.items)
    # `PERF_MYSQL_ROWS` 必须与 `--items` 一致：不一致时场景会随机取到不存在的
    # item_id，得到 404 —— 而 404 比写事务快得多，P95 会变得很好看。
    sut = start_sut(
        report_mode="agg",
        items=args.items,
        extra_env={
            MYSQL_URL_ENV: url,
            "PERF_MYSQL_ROWS": str(args.items),
            "PERF_REDIS_URL": "",
        },
    )
    try:
        print()
        rows: list[tuple[str, Stats]] = []
        for label, fn in (
            ("SQLite（BEGIN IMMEDIATE，整库写锁）", stock_write),
            ("MySQL（SELECT…FOR UPDATE，行锁）", stock_write_mysql),
        ):
            stats = _measure(fn, sut.base_url, args.users, args.duration)
            rows.append((label, stats))
            print(f"  {label:36s} {_fmt(stats)}")

        print()
        sqlite_stats, mysql_stats = rows[0][1], rows[1][1]
        if sqlite_stats.rps > 0 and sqlite_stats.p95_ms > 0:
            print("相对 SQLite：")
            print(
                f"  {'MySQL':36s} "
                f"P95 {mysql_stats.p95_ms / sqlite_stats.p95_ms:.2f}x  "
                f"RPS {mysql_stats.rps / sqlite_stats.rps:.2f}x"
            )
        else:
            print("SQLite 口径本身就在失败（错误率见上），不做比值。")

        print()
        print("怎么读这两个数字：")
        print("  - 这里的差值是「写锁粒度」的价格：整库写锁 vs 行锁，不是代码路径的差异。")
        print("  - 绝对值不可外推（本机、同一个 box、容器与压测客户端抢 CPU）；能外推的是方向。")
        print("  - 冲突时先看错误率：404 比写事务快，会把 P95 变得很好看。")
        return 0
    finally:
        sut.stop()


if __name__ == "__main__":
    sys.exit(main())
