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
import threading
import time
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
            results[mode] = requests.get(f"{handle.base_url}/api/reports/slow-by-supplier", timeout=30).json()["rows"]
        finally:
            handle.stop()

    agg, n1 = results["agg"], results["n1"]
    assert agg and n1
    assert all(
        set(r) == {"supplier_id", "supplier_name", "item_count", "stock_total", "delta_recent"} for r in agg + n1
    )
    assert len(agg) == len(n1) == 40, "供应商数应为 40（N1_SUPPLIER_LIMIT 设计上限）"
    assert [r["supplier_id"] for r in agg] == list(range(1, 41)), "供应商顺序必须稳定"
    assert sum(r["item_count"] for r in agg) == 800, "物料总数必须与灌入量一致"

    # 逐行逐字段：这才是"两种实现等价"的证明
    assert agg == n1, "agg 与 n1 返回的数据必须完全一致，否则性能对比不成立"


# --------------------------------------------------------------------------- #
# 已知缺陷探针（性能类）
#
# 注意：性能探针**不在这里**，它们在 tests/test_probes.py。
# 原因是本文件用 800 条数据（跑得快），而在 800 条时 N+1 与聚合的差距
# 只有约 2×（旧口径下更是被建连成本稀释到 1.02×），
# 到 20000 条时才拉开到约 7.8× —— 在玩具数据量上测性能缺陷，
# 测出来的结论是"没有缺陷"。见 test_probes.py 的说明。
# --------------------------------------------------------------------------- #
def test_probe_write_path_slower_than_read_path(base):
    """探针：写路径比点查慢一个数量级以上，且并发写不丢更新。

    **当前状态：缺陷存在（预期通过）。**
    机制有两条，都长在 SQLite 的单写者模型上：

    1. 写要走 `BEGIN IMMEDIATE`（独占写锁）—— 写请求之间必须串行化；
    2. 一个业务动作（库存 +1 再 -1）包含两次往返。

    SQLite 的单写者模型在这里被如实暴露 —— 这正是"写路径不可外推"
    这条边界结论的来源。

    这条探针留在本文件而不进 test_probes.py，因为它是**结构性**的
    （单写者模型与数据量无关），不像 N+1 那样需要大数据量才显著。

    ## 两个判据，分工不同

    **判据一（耗时）**：写一条业务动作的耗时，**扣除两端共有的固定成本**
    后，要 > 点查净耗时的 3 倍。固定成本用同一个测量窗口里的
    `/healthz`（不碰数据库）量出来再减掉。

    **判据二（不变量）**：并发 N 线程各 +1 共 M 次，最终库存必须精确等于
    `初值 + N*M`，且不允许出现任何非 201 响应。**这一条才是写锁语义
    真正的判别器** —— 把 `BEGIN IMMEDIATE` 换成普通 `BEGIN` 后，
    实测稳定出现 500 / 连接中断与成片的丢失更新（见文末实测记录）。

    ## 为什么耗时判据要减掉固定成本

    旧写法是 `write_ms > read_ms * 3`，直接比总耗时。它有一个**结构性**
    问题：读路径的耗时里有约一半（本机实测 41%–57%）是与写锁**无关**的
    固定成本（HTTP 往返 + FastAPI 路由开销，用 `/healthz` 量出来
    1.6–3.4 ms）。那个比值实际上在赌"固定成本 / 数据库工作量"的比例。
    两个后果：

    * **在共享 runner 上它会在阈值附近摇摆。** CI 实测过一次
      点查 2.2 ms / 写 6.4 ms = 2.87×，红；同一份代码在本机是 4.3–5.0×。
      这不是"CI 上的缺陷消失了"，是固定成本占比变了。
    * **它抓不到写锁退化。** 实测把 `BEGIN IMMEDIATE` 改成 `BEGIN`：
      总耗时比只在 4.30–5.06 区间里抖（基线 6 轮 median 4.63、
      变异后 4 轮 median 4.54），阈值 3 两次都通过 ——
      也就是说这个判据对写锁语义**没有判别力**。

    减掉固定成本后判据变成 `(write - health) > (read - health) * 3`。
    这不是把"慢一个数量级"这个说法放松，而是**换到真正该比的那个量上**：
    写路径的数据库工作量要 ≥ 读路径的 3 倍 —— 这是同一条断言，
    只是把两端共有的、与写锁无关的那部分时间从分母里去掉。

    **诚实说明这个改动的方向**：阈值 `3` 一个字没改，但在新口径下
    比值**变大**了，所以同一个 3 更容易过。用真实读数核对（本机 6 轮
    基线 + CI 那次失败）：

        场景              点查 r   写 w2   |  旧总比 w2/r   新 DB 比
        本机 baseline    4.72 ms  23.39 ms |      4.96×      8.60×
        本机 baseline    6.05 ms  26.01 ms |      4.30×      6.76×
        CI 失败那次      2.23 ms   6.41 ms |      2.88× ←红   6.75× ←绿

    也就是说：**旧口径是一个被机器项污染的比值**，它在 CI 上红不是因为
    缺陷变轻了（CI 那次的写净工作量 4.9 ms 反而比本机更大），而是因为
    分母里混进了固定成本。这个 2.88× → 6.75× 的差别才是问题的真相。
    换口径之后，"机器快/忙导致固定成本占比变化"不再能左右结论，
    而"写路径的数据库工作量是否真的比读路径大一个量级"变成了被真正
    检验的东西 —— 代价是它也因此不再能顺带抓住"写锁语义退化"
    （那种退化本来就不改变单请求的工作量）。判别写锁语义的职责
    交给下面第二条判据，这是分工，不是放水。


    ## 这条用例曾经是 flaky 的，值得记一笔

    它最初不热身、每个请求新建连接，结果连续两次运行给出相反结论：

        第一次：点查 15.1ms / 写 41.4ms   → 3.0×，红
        第二次：点查  4.0ms / 写 41.4ms   → 10×，绿

    差别全在分子（点查）。冷启动时点查要背 TCP 建连 + 页缓存冷的
    约 12ms 固定成本，热身并复用连接后只剩 3~4ms —— **同一份代码、
    同一个断言，因为测量方法不同而在红绿之间摇摆**。

    这正是"会随机变红的用例"的典型来源，它唯一的结果是训练团队
    忽略红灯。修复方式不是放宽阈值，而是**修测量方法**：
    热身 3 次 + 复用 `requests.Session` + 三个量在同一窗口内交替采样
    （固定成本随机器负载漂移，不在同一窗口里量就减不干净）。

    ## 变红了怎么办

    * **耗时判据红** —— 写路径真被优化了（例如把两次往返合成一次）：
      更新 `docs/已知问题.md` 的 P-3。
    * **不变量判据红** —— 这是**正确性**问题，不是性能问题。两种成因
      都被实测复现过：① 写锁退化成延迟锁 `BEGIN` → 真的丢更新；
      ② 读路径被加上不失效的缓存 → 更新写进去了但读到了过期值。
      先查 `sut/app.py` 的 `adjust_stock` 写锁，再查 `get_item` 有没有
      引入缓存。**不要**改这个断言。
    """
    measure_n = 15
    conc_threads = 6
    conc_iters = 10

    def _median(values: list[float]) -> float:
        ordered = sorted(values)
        return ordered[len(ordered) // 2]

    def _timed(fn) -> float:
        t0 = time.perf_counter()
        fn()
        return (time.perf_counter() - t0) * 1000

    session = requests.Session()
    try:
        # 热身：把建连与页缓存冷的固定成本从测量窗口里挤出去
        for _ in range(3):
            session.get(f"{base}/api/items/11", timeout=30)
            session.post(f"{base}/api/items/11/stock?delta=1", timeout=30)
            session.post(f"{base}/api/items/11/stock?delta=-1", timeout=30)

        health: list[float] = []
        read: list[float] = []
        write: list[float] = []
        for _ in range(measure_n):
            health.append(_timed(lambda: session.get(f"{base}/healthz", timeout=30)))
            read.append(_timed(lambda: session.get(f"{base}/api/items/11", timeout=30)))
            write.append(
                _timed(
                    lambda: (
                        session.post(f"{base}/api/items/11/stock?delta=1", timeout=30),
                        session.post(f"{base}/api/items/11/stock?delta=-1", timeout=30),
                    )
                )
            )
    finally:
        session.close()

    health_ms = _median(health)
    read_ms = _median(read)
    write_ms = _median(write)
    # 读路径数据库工作量的**测量下限**：低于它说明本次测量已经分辨不出
    # 读路径的数据库部分（几乎只剩固定成本），此时拿它做分母会把比值放大到
    # 无意义的量级。用这个下限兜底，判据仍然要求写路径的净耗时多出
    # 至少 3 × 0.5ms。这个数不是拍的：本机读路径净耗时实测 2.2–3.0ms，
    # CI 那次折算下来约 0.7ms，0.5ms 落在两者之下、只作退化保护。
    min_read_db_ms = 0.5
    read_db_ms = max(read_ms - health_ms, min_read_db_ms)
    write_db_ms = write_ms - health_ms

    # --- 判据二：并发不变量 -------------------------------------------------
    # 用 item 12：本文件其余用例占用的是 1/7/8/9/10/11/999999。
    before = requests.get(f"{base}/api/items/12", timeout=30).json()["stock"]
    barrier = threading.Barrier(conc_threads)
    codes: list[str] = []
    codes_lock = threading.Lock()

    def _worker() -> None:
        sess = requests.Session()
        try:
            barrier.wait(timeout=60)
            for _ in range(conc_iters):
                try:
                    resp = sess.post(f"{base}/api/items/12/stock?delta=1", timeout=60)
                    code = str(resp.status_code)
                except Exception as exc:  # 连接层异常也算这次写失败，要计入非 201
                    code = type(exc).__name__
                with codes_lock:
                    codes.append(code)
        finally:
            sess.close()

    workers = [threading.Thread(target=_worker) for _ in range(conc_threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    after = requests.get(f"{base}/api/items/12", timeout=30).json()["stock"]
    expected = before + conc_threads * conc_iters
    failures = [c for c in codes if c != "201"]
    distribution = {c: codes.count(c) for c in sorted(set(codes))}

    # 先复原再断言：即使不变量被破坏，也不能把多出来的库存留给后面的用例
    if after > before:
        requests.post(f"{base}/api/items/12/stock?delta=-{after - before}", timeout=60)

    assert write_db_ms > read_db_ms * 3, (
        f"扣除固定成本后：点查净 {read_db_ms:.1f}ms / 写净 {write_db_ms:.1f}ms "
        f"（原始中位：点查 {read_ms:.1f}ms、写 {write_ms:.1f}ms、"
        f"/healthz {health_ms:.1f}ms），写路径的数据库工作量不再显著更大。"
        "若写路径真被优化了（例如把两次往返合成一次），"
        "请更新 docs/已知问题.md 的 P-3；"
        "**不要**为了让它变绿而调低这里的 3。"
    )
    assert not failures, (
        f"并发写出现 {len(failures)} 次非 201 响应（分布 {distribution}）。"
        "单写者模型下写请求必须被串行化；出现 500 / 连接中断说明写锁语义"
        "退化了（例如 BEGIN IMMEDIATE 被换成了先读后升级的延迟锁 BEGIN）。"
        "请检查 sut/app.py 的 adjust_stock。"
    )
    assert after == expected, (
        f"并发 {conc_threads} 线程 × {conc_iters} 次 +1 后库存是 {after}，"
        f"期望 {expected}（差 {expected - after} 次更新）。"
        "**并发写之后库存对不上，是数据正确性问题、不是性能问题。** "
        "两种可能，都要查："
        "① 写锁退化成延迟锁（BEGIN IMMEDIATE → BEGIN）会真的丢更新；"
        "② 读路径被改成了会返回旧值的缓存（写后没有失效），"
        "   此时更新其实写进去了，只是这次读到了过期值 —— "
        "两种都会让这个断言红。"
        "先查 sut/app.py 的 adjust_stock 是否还在用 BEGIN IMMEDIATE，"
        "再查 get_item 是否引入了未失效的缓存。"
        "**不要**为了让它变绿而放宽这个断言。"
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
        handle = start_sut(report_mode="agg", items=50, db_path=str(db_dir / "sut.db"), log_dir=str(log_dir))
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
