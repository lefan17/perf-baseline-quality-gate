"""环境变量解析的容错契约：**任何人写错一个变量，服务都得照常起来**。

## 为什么这批用例必须存在

这个仓原先有三个文件、三套容错策略（详见 `sut/envconf.py` 的模块 docstring）：

| 位置 | 原来的行为 |
|---|---|
| `sut/db.py` `pool_size()` | 非法值 → 关闭池，有兜底 |
| `sut/app.py` `N1_SUPPLIER_LIMIT` | 非法值 → **导入期 `ValueError`** |
| `sut/mysql_store.py` `build_store()` | 非法值 → **导入期 `ValueError`** |

后两处直接违反它们自己 docstring 的承诺（"不在这里失败，把失败推迟到用它
的那个请求"）。后果不是"配置没生效"，而是**被测服务根本起不来**：

    $ PERF_SUT_N1_SUPPLIERS=forty python -m uvicorn sut.app:app
    ValueError: invalid literal for int() with base 10: 'forty'

压测平台看到的是「SUT 起不来」，于是人去翻 uvicorn 的 stderr；而真正的原因
是某个人在一个 `.env` 里把一个数字打成了单词。这类故障的排查成本几乎全部
来自"错误信息没有指向配置"。

## 为什么断言的是"不崩 + 有告警"两条

只断言返回值不够：一个静默回退到默认值的实现同样能过，
但它会把"我改的配置没生效"变成一个查不出来的谜。
所以每条用例都同时断言 stderr 里有一行 `[env]` 开头、带变量名与原始值的告警。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from sut.envconf import env_float, env_int, env_off

ROOT = Path(__file__).resolve().parent.parent


# 环境变量解析：缺省 / 空串 / 非法 / 越界 四种输入
#
# 这四种必须分开测，因为它们的正确行为**不一样**：
#   缺省、空串 → 静默取默认值（"没配"是正常状态，不该告警；
#                否则每一个没配的变量都会在日志里刷一行，真告警会被淹掉）
#   非法、越界 → 取默认值/下限 + 一行告警（否则人以为配置生效了）
def test_missing_variable_is_silent(monkeypatch, capsys):
    monkeypatch.delenv("PERF_TEST_INT", raising=False)
    assert env_int("PERF_TEST_INT", 7) == 7
    assert capsys.readouterr().err == "", "没配这个变量不该产生任何告警"


def test_empty_string_counts_as_missing(monkeypatch, capsys):
    """空串是"没配"，不是"配了个空值"。

    `.env` 里写 `PERF_TEST_INT=` 是极常见的手误（想注释掉一行但留下了等号），
    这时候把它当非法值告警会让日志变吵，而结果一样是默认值。
    """
    monkeypatch.setenv("PERF_TEST_INT", "   ")
    assert env_int("PERF_TEST_INT", 7) == 7
    assert capsys.readouterr().err == ""


def test_non_integer_falls_back_with_a_warning(monkeypatch, capsys):
    monkeypatch.setenv("PERF_TEST_INT", "forty")
    assert env_int("PERF_TEST_INT", 7) == 7
    err = capsys.readouterr().err
    assert "[env]" in err, f"回退必须留痕，否则人以为配置生效了：{err!r}"
    assert "PERF_TEST_INT" in err, "告警必须点名是哪个变量"
    assert "forty" in err, "告警必须带上原始值，否则人不知道该去改什么"


def test_below_minimum_is_clamped_with_a_warning(monkeypatch, capsys):
    """越界要**夹住并告警**，不是静默夹住。

    静默夹住的坏处：运维把 `PERF_MYSQL_POOL_SIZE=0` 写进去想关掉池，
    实现夹成 1、日志一个字没有，他会认为"配置没生效"，然后去改别的地方。
    """
    monkeypatch.setenv("PERF_TEST_INT", "0")
    assert env_int("PERF_TEST_INT", 7, minimum=1) == 1
    err = capsys.readouterr().err
    assert "PERF_TEST_INT" in err and "下限" in err, err


def test_float_and_off_helpers(monkeypatch, capsys):
    monkeypatch.setenv("PERF_TEST_FLOAT", "not-a-number")
    assert env_float("PERF_TEST_FLOAT", 0.5) == 0.5
    assert "PERF_TEST_FLOAT" in capsys.readouterr().err

    monkeypatch.setenv("PERF_TEST_FLOAT", "1.25")
    assert env_float("PERF_TEST_FLOAT", 0.5) == pytest.approx(1.25)

    # `env_off` 的四个词与"空串"是这个仓一贯的约定：不设 = 不启用。
    for value in ("", "0", "off", "none", "false", "OFF", " None "):
        monkeypatch.setenv("PERF_TEST_OFF", value)
        assert env_off("PERF_TEST_OFF") is True, f"{value!r} 应该算关闭"
    for value in ("1", "on", "yes", "8"):
        monkeypatch.setenv("PERF_TEST_OFF", value)
        assert env_off("PERF_TEST_OFF") is False, f"{value!r} 应该算开启"


# 回归：真正要防的是"导入期崩"
#
# 这两条必须起**子进程**，且必须真的 `import`，因为在同一进程里
# `sut.app` 早就被 import 过了，模块级 `int()` 的时机已经错过 ——
# 在进程内改环境变量再 import 是测不到它的（这是一个非常容易写出的假绿用例）。
def _import_in_subprocess(env_extra: dict[str, str]) -> subprocess.CompletedProcess:
    import os

    env = os.environ.copy()
    env.update(env_extra)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-c", "import sut.app; print(sut.app.N1_SUPPLIER_LIMIT)"],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


def test_bad_n1_supplier_limit_does_not_break_import():
    """`PERF_SUT_N1_SUPPLIERS=forty` 不能让 `import sut.app` 失败。

    修复前实测：`ValueError: invalid literal for int() with base 10: 'forty'`，
    退出码 1 —— 整个被测服务起不来。
    """
    proc = _import_in_subprocess({"PERF_SUT_N1_SUPPLIERS": "forty"})
    assert proc.returncode == 0, f"导入期不该因为一个环境变量崩掉：\n{proc.stderr}"
    assert proc.stdout.strip() == "40", f"应该回退到默认 40：{proc.stdout!r}"
    assert "PERF_SUT_N1_SUPPLIERS" in proc.stderr, "回退必须留痕"


def test_bad_mysql_pool_size_does_not_break_import():
    """`PERF_MYSQL_POOL_SIZE=abc` 同样不能让导入失败。

    这条尤其重要：`build_store()` 是在 `sut/app.py` 的**模块级**被调用的，
    所以一个填错的 MySQL 池大小会把整个服务（包括不碰 MySQL 的接口）一起拖死。
    """
    proc = _import_in_subprocess({"PERF_MYSQL_URL": "127.0.0.1:1", "PERF_MYSQL_POOL_SIZE": "abc"})
    assert proc.returncode == 0, f"导入期不该因为一个环境变量崩掉：\n{proc.stderr}"
    assert "PERF_MYSQL_POOL_SIZE" in proc.stderr


def test_seed_zero_means_zero_items(tmp_path):
    """`seed(items=0)` 必须灌 0 条，不能静默灌两万条。

    修复前：`items = items or int(...)`，`0` 是 falsy 于是走默认值 20000。
    想量"空库下接口有多快"的人拿到的是满库数字，日志里没有任何提示。

    这里断言"插入行数"而不是只断言返回值 —— 返回值是函数自己算的，
    真正要防的是它和库里的行数对不上。
    """
    import sqlite3

    from sut.seed import seed

    db = tmp_path / "seed0.db"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        import sut.db as sut_db

        old = sut_db.db_path
        sut_db.db_path = lambda: db  # type: ignore[assignment]
        try:
            result = seed(items=0)
        finally:
            sut_db.db_path = old  # type: ignore[assignment]
        assert result["items"] == 0, f"items=0 就该是 0 条：{result}"
        rows = conn.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"]
        assert rows == 0, f"库里也不该有数据：{rows}"
        # 类目与供应商仍然要灌：它们是报表类接口的分组依据，
        # "零条物料"不等于"零条维度数据"，否则空库下 /reports 直接 500。
        assert conn.execute("SELECT COUNT(*) AS c FROM categories").fetchone()["c"] == 10
        assert conn.execute("SELECT COUNT(*) AS c FROM suppliers").fetchone()["c"] == 40
    finally:
        conn.close()
