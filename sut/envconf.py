"""环境变量的统一解析：**配错不许让服务起不来**。

## 为什么要单独一个模块

这个仓的三个文件原本各有一套容错策略：

| 位置 | 原来的行为 |
|---|---|
| `sut/db.py:141` `pool_size()` | 非法值 → `0`（关闭池），有兜底 |
| `sut/app.py:50` `N1_SUPPLIER_LIMIT` | 非法值 → **导入期 `ValueError`**，服务根本起不来 |
| `sut/mysql_store.py:354` 池大小 / 行数 | 非法值 → **导入期 `ValueError`**，同上 |

而 `sut/mysql_store.py:347` 的 `build_store()` 在 docstring 里明写：

> 刻意**不在导入期连库**：`uvicorn` 启动时数据库可能还没起来（compose 里常见），
> 启动即失败会让整个服务起不来。

意图是「配错 / 依赖没起来也不崩，把失败推迟到用它的那一个请求」，
实现却因为一个 `int()` 在导入期崩掉。**三份代码三种策略**本身就是缺陷：
下一个人不知道该照着哪一份写。

所以这些形态收在这里，规则只有一条：

> **任何人写错一个环境变量，最多让相关功能降级并打一行告警，
> 绝不能让 `import sut.app` 失败。**

## 为什么不抛异常让调用方处理

因为这些变量的值是**部署时**填的，而抛异常的时机是**服务启动时**。
一个填错的 `PERF_MYSQL_POOL_SIZE=8 个`会让整个被测服务起不来，
于是压测平台报的是「SUT 起不来」，而不是「你的配置值写错了」——
后者一行日志就能说清，前者要人去翻 uvicorn 的 stderr。
"""

from __future__ import annotations

import os
import sys

#: 告警前缀。和仓库其它输出一样用 `[env]`，便于 `grep '^\[env\]'` 捞出来。
_WARN_PREFIX = "[env]"


def _warn(name: str, raw: str, default: object, reason: str) -> None:
    print(
        f"{_WARN_PREFIX} {name}={raw!r} {reason}，回退到默认值 {default!r}（服务继续启动）",
        file=sys.stderr,
    )


def env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    """读一个整数环境变量。**永不抛异常。**

    缺省 / 空串 → `default`（不告警：没配是正常状态）。
    非整数   → `default` + 一行告警。
    小于 `minimum` → `minimum` + 一行告警（比"静默夹到最小值"多说一句话，
    否则运维改了 0、日志里什么也没有，只会以为配置没生效）。
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    text = raw.strip()
    try:
        value = int(text)
    except ValueError:
        _warn(name, raw, default, "不是整数")
        return default
    if minimum is not None and value < minimum:
        _warn(name, raw, minimum, f"小于下限 {minimum}")
        return minimum
    return value


def env_float(name: str, default: float, *, minimum: float | None = None) -> float:
    """读一个浮点环境变量。**永不抛异常。**语义同 `env_int`。"""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    text = raw.strip()
    try:
        value = float(text)
    except ValueError:
        _warn(name, raw, default, "不是数字")
        return default
    if minimum is not None and value < minimum:
        _warn(name, raw, minimum, f"小于下限 {minimum}")
        return minimum
    return value


def env_off(name: str) -> bool:
    """这个变量是不是被显式关掉了（空 / `0` / `off` / `none` / `false`）。

    「空串算关闭」是这个仓一贯的约定：不设变量 = 不启用这个可选能力。
    抽出来是因为连接池与缓存都按同一套词表判断，两处各写一份必然漂移。
    """
    raw = os.environ.get(name, "").strip().lower()
    return not raw or raw in ("0", "off", "none", "false")
