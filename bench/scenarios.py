"""压测场景定义。

场景不是"随便打几个接口"，而是按**线上真实的读写配比**配的：
物料查询是高频只读，报表是低频但重，改库存是写。
只压读接口的结论会过于乐观 —— 真实系统里把 CPU 打满的往往是写路径的锁竞争。

每个场景都是一个 `(session) -> Sample` 的函数，和功能用例一样按"输入→调用→断言"
组织，只是断言的粒度从"业务对不对"变成"响应码符合预期 + 耗时被记账"。
"""

from __future__ import annotations

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


REGISTRY = {
    "items_list": items_list,
    "item_detail": item_detail,
    "stock_by_category": stock_by_category,
    "slow_report": slow_report,
    "stock_write": stock_write,
}


def build_mix(names: list[str] | None = None) -> list[tuple]:
    """按名字构造 [(场景函数, 权重)]，权重取自 DEFAULT_MIX。"""
    names = names or list(DEFAULT_MIX)
    out = []
    for n in names:
        if n not in REGISTRY:
            raise KeyError(f"未知场景 {n}，可选：{sorted(REGISTRY)}")
        out.append((REGISTRY[n], DEFAULT_MIX[n]))
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
