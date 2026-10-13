"""被测服务的功能用例。

## 为什么性能平台也要有功能用例

压测的前提是"被测的东西是对的"。一个返回 500 但很快的接口，
在性能报告里会显示成"漂亮的 P95" —— 因为快，还没来得及出错就返回了。
所以**每个压测结论背后都要有一组功能用例兜底**，说明这个响应是业务正确的。

## 六个刻意保留的缺陷探针

`test_probe_*` 系列是**已知缺陷清单的代码化**：

* 它们现在**全部通过** —— 因为探针断言的是"缺陷存在"这一事实。
* 一旦有人修好了缺陷，这些用例会变红，提醒去更新文档里的清单。
* 它们的价值在于：**证明门禁真的能拦住东西**。一个从没红过的门禁，
  和没有门禁的区别只是多花了几分钟 CI 时间。

这和接口测试里"已知缺陷探针"是同一个思路，只是探针的对象
从功能缺陷换成了性能缺陷。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import requests

from sut.server_ctl import start_sut

pytestmark = pytest.mark.sut


@pytest.fixture(scope="module")
def sut():
    """整个模块共用一个服务实例。

    这里**故意不用 function 级**：每次起服务要 4 秒（含灌数据），
    十个用例就是 40 秒，而它们之间没有状态依赖。
    唯一需要隔离的是写操作，那几个用例自己造自己的数据。
    """
    handle = start_sut(report_mode="agg", items=800)
    yield handle
    handle.stop()


@pytest.fixture(scope="module")
def base(sut):
    return sut.base_url


# --------------------------------------------------------------------------- #
# 健康与元信息
# --------------------------------------------------------------------------- #
def test_healthz(base):
    r = requests.get(f"{base}/healthz", timeout=10)
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_diagnostics_reports_all_expected_indexes(base):
    """索引必须在位。

    这是本平台最容易踩的坑：SQLite 少一个索引，报表就从 10ms 变成 2 秒，
    而压测报告只会写"报表慢"，不会写"因为迁移脚本漏了建索引"。
    把"索引在位"变成一条会红的用例，比事后挖 SQL 便宜得多。
    """
    r = requests.get(f"{base}/api/_diagnostics/indexes", timeout=10)
    assert r.status_code == 200
    body = r.json()
    assert body["missing"] == [], f"缺失索引：{body['missing']}"
    assert set(body["present"]) >= {"idx_items_category", "idx_items_supplier", "idx_ledger_item"}


def test_diagnostics_config_is_queryable(base):
    """配置可查询 —— 报告里"这一轮跑的是什么"必须有据可查。"""
    cfg = requests.get(f"{base}/api/_diagnostics/config", timeout=10).json()
    assert cfg["report_mode"] == "agg"
    assert cfg["n1_suppliers"] == 40
    assert cfg["cpu_count"] >= 1


# --------------------------------------------------------------------------- #
# 读接口
# --------------------------------------------------------------------------- #
def test_items_list_pagination(base):
    r = requests.get(f"{base}/api/items?limit=5&offset=0", timeout=10)
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 800
    assert len(body["items"]) == 5
    assert [it["id"] for it in body["items"]] == [1, 2, 3, 4, 5], "分页必须稳定有序"


def test_items_list_rejects_oversized_limit(base):
    """limit 上限 200。不设上限的接口是 DoS 入口，也是压测里的假慢。"""
    assert requests.get(f"{base}/api/items?limit=500", timeout=10).status_code == 422
    assert requests.get(f"{base}/api/items?limit=0", timeout=10).status_code == 422


def test_items_filter_by_category(base):
    body = requests.get(f"{base}/api/items?category_id=1&limit=200", timeout=10).json()
    assert body["items"], "类目 1 应该有物料"
    assert all(it["category_id"] == 1 for it in body["items"])


def test_item_detail_joins_category_and_supplier(base):
    body = requests.get(f"{base}/api/items/1", timeout=10).json()
    assert body["id"] == 1
    # 详情页要展示类目名和供应商名，不能只有 id —— 这是 N+1 的常见来源，
    # 用 JOIN 一次查回来，接口语义上也更完整。
    assert body["category_name"]
    assert body["supplier_name"]


def test_item_detail_404(base):
    r = requests.get(f"{base}/api/items/999999", timeout=10)
    assert r.status_code == 404


def test_item_ledger(base):
    body = requests.get(f"{base}/api/items/1/ledger?limit=10", timeout=10).json()
    assert "entries" in body


# --------------------------------------------------------------------------- #
# 写接口
# --------------------------------------------------------------------------- #
def test_stock_delta_updates_and_reverts(base):
    before = requests.get(f"{base}/api/items/7", timeout=10).json()["stock"]
    assert requests.post(f"{base}/api/items/7/stock?delta=5", timeout=10).status_code == 201
    assert requests.get(f"{base}/api/items/7", timeout=10).json()["stock"] == before + 5
    assert requests.post(f"{base}/api/items/7/stock?delta=-5", timeout=10).status_code == 201
    assert requests.get(f"{base}/api/items/7", timeout=10).json()["stock"] == before


def test_stock_never_goes_negative(base):
    """库存是业务不变量。压测里写路径占 5%，如果它允许负库存，
    那这个"写场景"测的就不是真实业务了。"""
    huge = requests.get(f"{base}/api/items/8", timeout=10).json()["stock"] + 1
    r = requests.post(f"{base}/api/items/8/stock?delta=-{huge}", timeout=10)
    assert r.status_code == 409
    assert requests.get(f"{base}/api/items/8", timeout=10).json()["stock"] >= 0


def test_order_idempotency(base):
    """幂等键：同一 key 重复提交返回同一订单。

    这是压测里最容易被漏掉的真实约束 —— 客户端超时重试是常态，
    没有幂等键的写接口在压测中会造出重复订单，看起来"吞吐不错"，
    实际上在污染数据。
    """
    url = f"{base}/api/orders?item_id=9&qty=2&idempotency_key=test-key-1"
    a = requests.post(url, timeout=10)
    b = requests.post(url, timeout=10)
    assert a.status_code == 201 and b.status_code == 201
    assert a.json()["id"] == b.json()["id"]
    assert a.json()["status"] == "CREATED"


def test_order_rejects_bad_qty(base):
    """qty 有下界约束：0 或负数下单是业务上说不通的事。"""
    assert requests.post(f"{base}/api/orders?item_id=9&qty=0&idempotency_key=z", timeout=10).status_code == 422


def test_different_idempotency_keys_create_different_orders(base):
    a = requests.post(f"{base}/api/orders?item_id=10&qty=1&idempotency_key=k-a", timeout=10).json()
    b = requests.post(f"{base}/api/orders?item_id=10&qty=1&idempotency_key=k-b", timeout=10).json()
    assert a["id"] != b["id"]


# --------------------------------------------------------------------------- #
# 报表：健康实现 vs 性能缺陷探针
# --------------------------------------------------------------------------- #
def test_stock_by_category_report(base):
    body = requests.get(f"{base}/api/reports/stock-by-category", timeout=10).json()
    assert len(body["rows"]) == 10
    assert sum(row["item_count"] for row in body["rows"]) == 800


def test_reports_modes_agree_on_data():
    """两种实现必须给出**完全同样的数据**，只有速度不同。

    这是性能探针成立的前提：如果 n1 的结果和 agg 不一样，
    那它测的就不是"同一个功能的两种写法"，而是两个不同的功能 ——
    性能对比立刻失去意义。

    所以这条用例把同一个库分别用两种模式读一遍，**逐字段比对**。
    数据一致性钉死了，后面才敢单独讨论速度。
    """
    results = {}
    for mode in ("agg", "n1"):
        handle = start_sut(report_mode=mode, items=800)
        try:
            results[mode] = requests.get(
                f"{handle.base_url}/api/reports/slow-by-supplier", timeout=30
            ).json()["rows"]
        finally:
            handle.stop()

    agg, n1 = results["agg"], results["n1"]
    assert agg and n1
    assert all(set(r) == {"supplier_id", "supplier_name", "item_count", "stock_total",
                          "delta_recent"} for r in agg + n1)
    assert len(agg) == len(n1) == 40, "供应商数应为 40（N1_SUPPLIER_LIMIT 设计上限）"
    assert [r["supplier_id"] for r in agg] == list(range(1, 41)), "供应商顺序必须稳定"
    assert sum(r["item_count"] for r in agg) == 800, "物料总数必须与灌入量一致"

    # 逐行逐字段：这才是"两种实现等价"的证明
    assert agg == n1, "agg 与 n1 返回的数据必须完全一致，否则性能对比不成立"


# --------------------------------------------------------------------------- #
# 已知缺陷探针（性能类）
#
# 注意：性能探针**不在这里**，它们在 tests/test_probes.py。
# 原因是本文件用 800 条数据（跑得快），而 N+1 的差距在 800 条时只有 1.02×，
# 到 20000 条时才拉开到 2.2× —— 在玩具数据量上测性能缺陷，
# 测出来的结论是"没有缺陷"。见 test_probes.py 的说明。
# --------------------------------------------------------------------------- #
def test_probe_write_path_slower_than_read_path(base):
    """探针：写路径比点查慢一个数量级以上。

    **当前状态：缺陷存在（预期通过）。**
    原因是写要走 `BEGIN IMMEDIATE`（独占写锁）+ 两次往返（+1/-1）。
    SQLite 的单写者模型在这里被如实暴露 —— 这正是"写路径不可外推"
    这条边界结论的来源。

    这条探针留在本文件而不进 test_probes.py，因为它是**结构性**的
    （单写者模型与数据量无关），不像 N+1 那样需要大数据量才显著。

    ## 这条用例曾经是 flaky 的，值得记一笔

    它最初不热身、每个请求新建连接，结果连续两次运行给出相反结论：

        第一次：点查 15.1ms / 写 41.4ms   → 3.0×，红
        第二次：点查  4.0ms / 写 41.4ms   → 10×，绿

    差别全在分子（点查）。冷启动时点查要背 TCP 建连 + 页缓存冷的
    约 12ms 固定成本，热身并复用连接后只剩 3~4ms —— **同一份代码、
    同一个断言，因为测量方法不同而在红绿之间摇摆**。

    这正是"会随机变红的用例"的典型来源，它唯一的结果是训练团队
    忽略红灯。修复方式不是放宽阈值，而是**修测量方法**：
    热身 3 次 + 复用 `requests.Session`。
    """
    import time

    session = requests.Session()
    try:
        # 热身：把建连与页缓存冷的固定成本从测量窗口里挤出去
        for _ in range(3):
            session.get(f"{base}/api/items/11", timeout=30)
            session.post(f"{base}/api/items/11/stock?delta=1", timeout=30)
            session.post(f"{base}/api/items/11/stock?delta=-1", timeout=30)

        def _median(fn, n: int = 10) -> float:
            times = []
            for _ in range(n):
                t0 = time.perf_counter()
                fn()
                times.append((time.perf_counter() - t0) * 1000)
            times.sort()
            return times[len(times) // 2]

        read_ms = _median(lambda: session.get(f"{base}/api/items/11", timeout=30))
        write_ms = _median(
            lambda: (
                session.post(f"{base}/api/items/11/stock?delta=1", timeout=30),
                session.post(f"{base}/api/items/11/stock?delta=-1", timeout=30),
            )
        )
    finally:
        session.close()

    assert write_ms > read_ms * 3, (
        f"点查 {read_ms:.1f}ms / 写 {write_ms:.1f}ms（均已热身），写路径不再显著更慢。"
        "若写路径真的被优化了（例如去掉了 BEGIN IMMEDIATE 的两次往返），"
        "请更新 docs/已知问题.md 的 P-3 并同步调整阈值。"
    )


# --------------------------------------------------------------------------- #
# 临时目录的回收
# --------------------------------------------------------------------------- #
class TestTempDirCleanup:
    """起服务时 mkdtemp 出来的目录必须被收回。

    这不是洁癖：`start_sut` 每组会建一个 db 目录和一个 log 目录，而
    清理逻辑曾经**完全不存在** —— 系统 Temp 下累积了 289 组 `perf-sut-*`。
    一个每次跑都留垃圾的平台，会让"跑过几次"变成一种代价。
    """

    def test_stop_removes_the_dirs_it_created(self):
        handle = start_sut(report_mode="agg", items=50)
        dirs = list(handle.tmp_dirs)
        assert len(dirs) == 2, "应当自己建了 db 与 log 两个目录"
        assert all(Path(d).exists() for d in dirs)
        handle.stop()
        assert not any(Path(d).exists() for d in dirs), f"stop() 之后仍在：{dirs}"

    def test_caller_supplied_dirs_are_never_deleted(self, tmp_path):
        """调用方指定的目录不是我们的，删了就是数据事故。"""
        db_dir = tmp_path / "mine-db"
        log_dir = tmp_path / "mine-log"
        db_dir.mkdir()
        log_dir.mkdir()
        handle = start_sut(report_mode="agg", items=50,
                           db_path=str(db_dir / "sut.db"), log_dir=str(log_dir))
        assert handle.tmp_dirs == []
        handle.stop()
        assert db_dir.exists() and log_dir.exists()

    def test_keep_tmp_retains_the_dirs_for_debugging(self):
        handle = start_sut(report_mode="agg", items=50, keep_tmp=True)
        dirs = list(handle.tmp_dirs)
        try:
            handle.stop()
            assert all(Path(d).exists() for d in dirs), "keep_tmp=True 时应当保留"
        finally:
            for d in dirs:
                shutil.rmtree(d, ignore_errors=True)

