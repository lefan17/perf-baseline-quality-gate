"""pytest 插件：把用例执行结果直接写进度量库。

## 为什么不用 pytest-json-report

那是个好插件，但为它在依赖里加一项，就等于把"结果格式"交给了外部项目：
它改一次字段名，平台的 flaky 分析就静默失效（读到 None 又不报错）。
这里只用 pytest 自带的 hook —— 少一个依赖，就少一个"某天它悄悄变了"的来源。

用法（`platform_cli suite` 就是这么调的）::

    QUALITY_RUN_ID=suite-xxx pytest -p metrics.pytest_plugin

没设 `QUALITY_RUN_ID` 时插件完全静默：开发者日常 `pytest` 不会往库里写东西。
这是刻意的 —— **度量平台最忌讳"顺手记一笔"**，
日常开发跑的单测不是质量记录，混进去只会稀释信号。
"""

from __future__ import annotations

import os

_RUN_ID = os.environ.get("QUALITY_RUN_ID")
_RESULTS: list[dict] = []
# `nodeid -> item`。marker 只能从 `Item` 上取，而结果只在 `TestReport` 上拿得到 ——
# 两个对象唯一的公共键就是 `nodeid`，所以在这里做一次映射。
_ITEMS: dict[str, object] = {}


def pytest_runtest_setup(item):
    """在每条用例开始前记下它的 `Item`，供 `_markers()` 取 marker。

    为什么不用 `pytest_collection_modifyitems`：那个 hook 在收集阶段跑，
    模块级 `pytest.skip()` 会让用例根本没被收集，而这类"没收集到"的用例
    恰恰是 `S1-pass-rate` 分母口径要盯的东西 —— 混进来只会让插件的行为
    和 `_case_summary` 对不上。
    """
    if not _RUN_ID:
        return
    _ITEMS[item.nodeid] = item


def pytest_configure(config):  # noqa: ARG001
    if _RUN_ID:
        config.addinivalue_line("markers", "quality_recorded: 结果已写入度量库")


def pytest_runtest_logreport(report):
    """只记一条"最终结论"。

    一个用例在 setup 阶段通过、call 阶段失败，它的结论是 failed；
    把 setup/call/teardown 三个阶段各记一条，flaky 分析会被阶段细节污染
    （同一个用例在同一次运行里出现两条相反的结果）。
    teardown 失败同样记 failed —— 那是有意义的失败（例如资源没清理干净）。
    """
    if not _RUN_ID:
        return
    if report.when == "call":
        outcome = report.outcome
    elif report.when in ("setup", "teardown") and report.failed:
        outcome = "error" if report.when == "setup" else "failed"
    else:
        return  # 成功的 setup/teardown 不是一条结果

    _RESULTS.append(
        {
            "nodeid": report.nodeid,
            "outcome": outcome,
            "duration": float(report.duration or 0.0),
            "fail_message": _short(report.longrepr),
            "markers": _markers(report),
        }
    )


def _markers(report) -> str:
    """记录用例隶属的 marker（perf/sut/platform），便于按类型统计耗时。

    ## 这里之前恒返回空串

    原先的实现是 `for m in report.item.iter_markers()`。**`report` 是
    `TestReport`，没有 `.item` 属性**（`.item` 在 `Item`/`Function` 上）——
    于是每一条用例都会抛 `AttributeError`，被 `except AttributeError: return ""`
    接住，`markers` 列恒为空。实测入 `cases` 表的三列是
    `('...::test_marked', 'passed', '')`，即使那个用例明明打了
    `@pytest.mark.perf` 和 `@pytest.mark.sut`。

    这个 bug 的形状值得记：`except AttributeError` 本意是「兼容老版本 pytest
    没有 `iter_markers`」，实际却把**同一类异常的所有来源**一起吞了，
    包括"我访问了不存在的属性"这种纯代码错误。宽泛的 except 会把
    「版本不兼容」和「我写错了」变成同一个静默结果。

    ## 正确做法

    marker 得从 `item` 上取，而 `item` 只在 `pytest_runtest_setup` 这类
    带 `item` 参数的 hook 里拿得到、且那时还不知道结果。所以改用
    `item` 阶段存一份映射、`logreport` 阶段查表。`report.nodeid` 是两边的
    公共键。

    对拿不到 item 的老 pytest：返回空串（这是真的「不支持」，
    和「写了但恒为空」不是一回事），并**只在这个分支**用 `getattr`，
    让其它任何异常照常冒出来。
    """
    item = _ITEMS.get(report.nodeid)
    if item is None:
        return ""
    try:
        iter_markers = getattr(item, "iter_markers", None)
        if iter_markers is None:
            return ""
        names = sorted(m.name if hasattr(m, "name") else str(m) for m in iter_markers())
    except TypeError:
        return ""
    return ",".join(names)


def _short(longrepr) -> str:
    if longrepr is None:
        return ""
    text = str(longrepr)
    return text[-1500:] if len(text) > 1500 else text


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    if not _RUN_ID or not _RESULTS:
        return
    from metrics import store

    _ensure_run_row(store)
    store.add_cases(_RUN_ID, _RESULTS)
    print(f"\n[quality] 已记录 {len(_RESULTS)} 条用例结果到 run_id={_RUN_ID}", flush=True)


def _ensure_run_row(store) -> None:
    """库里还没有这一轮时补一条 `runs`，否则 `add_cases` 必炸外键。

    ## 为什么需要这一步

    插件文档（本文件开头）给的用法是独立跑：

        QUALITY_RUN_ID=suite-xxx pytest -p metrics.pytest_plugin

    而 `cases.run_id` 是指向 `runs.run_id` 的外键。`platform_cli suite`
    会先 `upsert_run` 再起 pytest，所以这条路径没问题；但**照文档独立跑**
    时没有那一行，`store.add_cases()` 会在 `sessionfinish` 里抛：

        sqlite3.IntegrityError: FOREIGN KEY constraint failed

    最坏的地方是**时机**：所有用例已经跑完、全绿、屏幕上打着 `1 passed`，
    然后收尾阶段炸在插件里 —— 退出码变成非零，而失败原因与任何一个用例
    都没有关系。读日志的人第一反应是"用例有问题"，实际是记账本时发现
    没有账本首页。

    ## 为什么只在缺失时补，而不是每次 upsert

    `upsert_run` 的 `ON CONFLICT` 会把 `status` 改回传入值。`cmd_suite`
    先建的那条 run 最终由它自己 `finish_run` 收尾；如果这里无条件
    `upsert_run(..., status="running")`，就会在 cmd_suite 已经写好的状态上
    再覆盖一次 —— 一个"顺手多写一行"引起的竞态。
    """
    # `tx()` 只开事务，不建表；而这里读 `runs` 之前表可能还不存在。
    store.init_db()
    with store.tx() as conn:
        exists = conn.execute("SELECT 1 FROM runs WHERE run_id=?", (_RUN_ID,)).fetchone()
    if exists:
        return
    store.upsert_run(
        _RUN_ID,
        "suite",
        os.environ.get("QUALITY_LABEL", "standalone-pytest"),
        status="running",
        config={"source": "metrics.pytest_plugin"},
    )
