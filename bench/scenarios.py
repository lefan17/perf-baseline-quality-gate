"""压测场景定义。

场景不是"随便打几个接口"，而是按**线上真实的读写配比**配的：
物料查询是高频只读，报表是低频但重，改库存是写。
只压读接口的结论会过于乐观 —— 真实系统里把 CPU 打满的往往是写路径的锁竞争。

每个场景都是一个 `(session) -> Sample` 的函数，和功能用例一样按"输入→调用→断言"
组织，只是断言的粒度从"业务对不对"变成"响应码符合预期 + 耗时被记账"。
"""

from __future__ import annotations

import os
import random

import requests

from bench.engine import Sample, run_api_call

# (场景函数, 权重)。权重之和会被归一化。
DEFAULT_MIX = {
    "items_list": 40,
    "item_detail": 30,
    "stock_by_category": 15,
    "slow_report": 10,
    "stock_write": 5,
}

# 物料 ID 的随机范围上限 = 实际灌进 SUT 的物料条数。
#
# 这里曾经写死 `randint(1, 20_000)`，制造了一个**很隐蔽的假故障**：
# 只要用 `--items 5000` 跑，就有约 75% 的 `item_detail` 请求打到
# 不存在的 ID 拿到 404，门禁 R1 立刻报"错误率 28%"。
#
# 更糟的是它**看起来像被测服务的缺陷**：R1 的结果是
# 「错误率 28.02%（阈值 1.00%）→ 判定：拦住」，
# 完全不会有人怀疑是压测脚本自己发错了请求。
# 一个压测工具凭空造出 28% 的错误率，比它测不出问题更危险。
#
# 正确做法：范围必须由**实际数据量**决定，而不是由某个默认值决定。
# `configure_item_range()` 由 CLI 在知道 `--items` 后显式调用。
_ITEM_ID_MAX = 20_000


def configure_item_range(item_count: int) -> None:
    """告诉场景实际有多少条物料，供随机取 ID 用。

    由 `platform_cli` 在启动 SUT 之后调用。不调用时退回默认的 20 000，
    这样单独 import 场景函数做冒烟测试仍然可用。
    """
    global _ITEM_ID_MAX
    _ITEM_ID_MAX = max(1, int(item_count))


def item_id_max() -> int:
    return _ITEM_ID_MAX


def _rnd() -> random.Random:
    # 每个线程各自持有一个 Random，避免共享实例被 GIL 打断成串行。
    return random.Random()


def _random_item_id(r: random.Random) -> int:
    """在真实存在的物料 ID 范围内取一个。"""
    return r.randint(1, _ITEM_ID_MAX)


def items_list(session: requests.Session, base_url: str, timeout_s: float = 10.0) -> Sample:
    r = _rnd()
    if r.random() < 0.5:
        path = f"/api/items?limit={r.choice([10, 20, 50])}&offset={r.choice([0, 100, 1000])}"
    else:
        path = f"/api/items?category_id={r.randint(1, 10)}&limit=20"
    return run_api_call(session, base_url, "GET", path, scenario="items_list", timeout_s=timeout_s)


def item_detail(session: requests.Session, base_url: str, timeout_s: float = 10.0) -> Sample:
    item_id = _random_item_id(_rnd())
    return run_api_call(session, base_url, "GET", f"/api/items/{item_id}", scenario="item_detail", timeout_s=timeout_s)


#: 缓存探针的热点集大小。
#:
#: 这里有一个**很容易把结论做反**的细节：如果按全量 id 均匀随机，缓存命中率会
#: 接近 0（20 000 个 id 里取 3 000 次，重复率只有十几个百分点），
#: 于是"加了缓存"看起来几乎没用 —— 但那不是缓存不行，是**压测脚本没有模拟
#: 真实的访问分布**。真实系统里少数热点承担绝大多数读取（长尾/Zipf）。
#:
#: 所以探针显式把流量收在前 500 个 id 上，并且 `cached` 与 `nocache` 两组
#: 用**完全相同的 id 集合**，唯一的差别是 `cache=` 参数 —— 这样两组的差异
#: 只能来自缓存，不会混进"访问到的数据不一样"这个变量。
HOT_SET_SIZE = 500


def _hot_item_id(r: random.Random) -> int:
    return r.randint(1, max(1, min(HOT_SET_SIZE, _ITEM_ID_MAX)))


def item_detail_cached(session: requests.Session, base_url: str, timeout_s: float = 10.0) -> Sample:
    """带缓存读详情（热点集）。对照组是 `item_detail_nocache`。"""
    item_id = _hot_item_id(_rnd())
    return run_api_call(
        session,
        base_url,
        "GET",
        f"/api/items/{item_id}/detail?cache=on",
        scenario="item_detail_cached",
        timeout_s=timeout_s,
    )


def item_detail_nocache(session: requests.Session, base_url: str, timeout_s: float = 10.0) -> Sample:
    """不带缓存读详情（同一个热点集）。"""
    item_id = _hot_item_id(_rnd())
    return run_api_call(
        session,
        base_url,
        "GET",
        f"/api/items/{item_id}/detail?cache=off",
        scenario="item_detail_nocache",
        timeout_s=timeout_s,
    )


def items_by_category_indexed(session: requests.Session, base_url: str, timeout_s: float = 10.0) -> Sample:
    """按类目过滤，走 `idx_items_category`（对照组）。"""
    category_id = _rnd().randint(1, 10)
    return run_api_call(
        session,
        base_url,
        "GET",
        f"/api/items?category_id={category_id}&limit=20",
        scenario="items_by_category_indexed",
        timeout_s=timeout_s,
    )


def items_by_category_fullscan(session: requests.Session, base_url: str, timeout_s: float = 10.0) -> Sample:
    """同一个查询，但过滤列被 `substr()` 包住 → 索引失效、退化成全表扫描。

    两组用**同样的类目分布**，唯一的差别是 `index=false`。
    这样差异只能来自执行计划，不会混进"查的类目不一样"。
    """
    category_id = _rnd().randint(1, 10)
    return run_api_call(
        session,
        base_url,
        "GET",
        f"/api/items?category_id={category_id}&index=false&limit=20",
        scenario="items_by_category_fullscan",
        timeout_s=timeout_s,
    )


def stock_by_category(session: requests.Session, base_url: str, timeout_s: float = 10.0) -> Sample:
    return run_api_call(
        session,
        base_url,
        "GET",
        "/api/reports/stock-by-category",
        scenario="stock_by_category",
        timeout_s=timeout_s,
    )


def slow_report(session: requests.Session, base_url: str, timeout_s: float = 10.0) -> Sample:
    """报表接口。走的是服务端 `PERF_SUT_REPORT_MODE` 决定的实现。"""
    days = _rnd().choice([7, 30, 90])
    return run_api_call(
        session,
        base_url,
        "GET",
        f"/api/reports/slow-by-supplier?window_days={days}",
        scenario="slow_report",
        timeout_s=timeout_s,
    )


def stock_write(session: requests.Session, base_url: str, timeout_s: float = 10.0) -> Sample:
    """写路径：+1 再 -1，保证库存回到原值，压测不改变数据基线。"""
    r = _rnd()
    item_id = _random_item_id(r)
    s1 = run_api_call(
        session,
        base_url,
        "POST",
        f"/api/items/{item_id}/stock?delta=1",
        expect_status=201,
        scenario="stock_write",
        timeout_s=timeout_s,
    )
    if not s1.ok:
        return s1
    s2 = run_api_call(
        session,
        base_url,
        "POST",
        f"/api/items/{item_id}/stock?delta=-1",
        expect_status=201,
        scenario="stock_write",
        timeout_s=timeout_s,
    )
    # 两次往返算一个业务动作：只记第二次的耗时会把写路径记成一半。
    s2.elapsed_ms += s1.elapsed_ms
    return s2


def _mysql_rows() -> int:
    """MySQL 侧到底渗了多少行 —— 必须与 SUT 的 `PERF_MYSQL_ROWS` 一致。

    不一致的后果很隐蔽：场景随机取的 item_id 超出行数时，请求是 **404**，
    错误率上去了，但 P95 反而很好看（404 比写事务快得多）。
    「写路径变快了」这种结论只要混进一次 404 就是假的。
    """
    return int(os.environ.get("PERF_MYSQL_ROWS", "1000"))


def stock_write_mysql(session: requests.Session, base_url: str, timeout_s: float = 10.0) -> Sample:
    """写路径（MySQL）：与 `stock_write` 打同一件事，只换后端。

    两个场景刻意保持**完全相同的请求形状**（+1 再 -1、两次往返算一个动作），
    否则"SQLite 66 RPS vs MySQL N RPS"就变成了两个不同工作的比较。
    """
    r = _rnd()
    ceiling = min(item_id_max(), _mysql_rows())
    item_id = r.randint(1, ceiling) if ceiling > 0 else 1
    s1 = run_api_call(
        session,
        base_url,
        "POST",
        f"/api/mysql/stock/{item_id}?delta=1",
        expect_status=201,
        scenario="stock_write_mysql",
        timeout_s=timeout_s,
    )
    if not s1.ok:
        return s1
    s2 = run_api_call(
        session,
        base_url,
        "POST",
        f"/api/mysql/stock/{item_id}?delta=-1",
        expect_status=201,
        scenario="stock_write_mysql",
        timeout_s=timeout_s,
    )
    s2.elapsed_ms += s1.elapsed_ms
    return s2


REGISTRY = {
    "items_list": items_list,
    "item_detail": item_detail,
    "stock_by_category": stock_by_category,
    "slow_report": slow_report,
    "stock_write": stock_write,
    # 下面四组是**探针对照组**，刻意不进 DEFAULT_MIX：
    # 默认混合的比例一旦变化，历史基线就不可比（`metrics/analysis.py`
    # 的 check_comparability 只看 `scenario="(mix)"` 这个字符串，
    # 看不出混合比例已经变了）。所以它们只能被 `--scenario` 点名使用，
    # 由专门的探针命令成对跑。
    "item_detail_cached": item_detail_cached,
    "item_detail_nocache": item_detail_nocache,
    "items_by_category_indexed": items_by_category_indexed,
    "items_by_category_fullscan": items_by_category_fullscan,
    # MySQL 写路径同理：它是「同一个写动作换后端」的对照组，
    # 混进默认混合会让历史基线不可比，而且还要依赖一个外部容器。
    "stock_write_mysql": stock_write_mysql,
}


def build_mix(names: list[str] | None = None) -> list[tuple]:
    """按名字构造 [(场景函数, 权重)]，权重取自 DEFAULT_MIX。

    注意：`names` 里可以出现不在 `DEFAULT_MIX` 的探针场景（如
    `item_detail_cached`），此时权重取 1 —— 探针场景本来就是一次只压一个。
    """
    names = names or list(DEFAULT_MIX)
    out = []
    for n in names:
        if n not in REGISTRY:
            raise KeyError(f"未知场景 {n}，可选：{sorted(REGISTRY)}")
        out.append((REGISTRY[n], DEFAULT_MIX.get(n, 1)))
    return out


def bind(fn, base_url: str, timeout_s: float = 10.0):
    """把 (session, base_url, timeout) 场景适配成引擎要的 (session) -> Sample。

    场景函数写成显式传参而不是闭包捕获 base_url，是为了**能被单独调用做冒烟**：
    拐点定位出错时，第一件事就是"把那个场景手工跑一次看看"，
    闭包会让这一步变得别扭。
    """
    name = getattr(fn, "__name__", "scenario")

    def bound(session: requests.Session) -> Sample:
        return fn(session, base_url, timeout_s=timeout_s)

    bound.__name__ = name
    return bound


def build_engine_mix(
    base_url: str,
    names: list[str] | None = None,
    timeout_s: float = 10.0,
) -> list[tuple]:
    """给 Runner 用的 [(绑定后的场景函数, 权重)]。"""
    return [(bind(fn, base_url, timeout_s), w) for fn, w in build_mix(names)]


def engine_single(base_url: str, scenario_name: str, timeout_s: float = 10.0) -> list[tuple]:
    """只压一个场景（拐点定位时用：混压会把干扰项一起放大，看谁先塌就难了）。"""
    if scenario_name not in REGISTRY:
        raise KeyError(f"未知场景 {scenario_name}，可选：{sorted(REGISTRY)}")
    return [(bind(REGISTRY[scenario_name], base_url, timeout_s), 1)]
