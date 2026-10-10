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
    """记录用例隶属的 marker（perf/sut/platform），便于按类型统计耗时。"""
    try:
        names = sorted(
            m.name if hasattr(m, "name") else str(m)
            for m in report.item.iter_markers()  # pytest 7+
        )
    except AttributeError:
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

    store.add_cases(_RUN_ID, _RESULTS)
    print(f"\n[quality] 已记录 {len(_RESULTS)} 条用例结果到 run_id={_RUN_ID}", flush=True)
