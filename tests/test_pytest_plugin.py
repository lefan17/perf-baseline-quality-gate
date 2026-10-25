"""`metrics/pytest_plugin.py` 的行为 —— 它把用例结果写进度量库。

## 为什么这个文件到这里才出现

插件此前**没有任何用例**，且被 `pyproject.toml` 的 per-file exclude 排除在
覆盖率门槛外（理由写的是「子进程动态加载」）。那理由只解释了"覆盖率看不见它"，
不构成"不需要测它" —— 恰恰相反，它是整条 `suite` 链路上唯一**跑在子进程里、
出错也不会被人看见**的一段：

* 它写错了，`S1-pass-rate` 的分母就错；
* 它在收尾阶段抛异常，屏幕上会是"全部用例通过 + 退出码非零"。

两件事都在本项目里真实发生过（见下面两条用例的 docstring）。

## 测法：一部分直调，一部分起子进程

`_markers` / `_ensure_run_row` / `_short` 是纯函数/纯写库，直接调最省事，
也能让父进程的覆盖率真的看见它们。而 `_RUN_ID` 是**模块导入时**读的环境变量，
同一进程里改 `os.environ` 再重载模块测不准（第一次导入就定下来了），
所以"设了 QUALITY_RUN_ID 才记录、没设就彻底静默"这条只能起子进程验。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from metrics import pytest_plugin, store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _FakeItem:
    def __init__(self, names):
        self._names = names

    def iter_markers(self):
        return [type("M", (), {"name": n})() for n in self._names]


class _FakeReport:
    def __init__(self, nodeid):
        self.nodeid = nodeid


class TestMarkers:
    """`_markers` 曾恒返回空串 —— 因为它在 `TestReport` 上找 `.item`。"""

    def test_reads_markers_from_the_item(self):
        pytest_plugin._ITEMS["x.py::test_a"] = _FakeItem(["perf", "sut"])
        try:
            got = pytest_plugin._markers(_FakeReport("x.py::test_a"))
        finally:
            pytest_plugin._ITEMS.clear()
        assert got == "perf,sut", f"markers 没从 item 上取到：{got!r}"

    def test_markers_are_sorted_and_deduplicated_by_name(self):
        pytest_plugin._ITEMS["x.py::test_b"] = _FakeItem(["sut", "perf", "platform"])
        try:
            got = pytest_plugin._markers(_FakeReport("x.py::test_b"))
        finally:
            pytest_plugin._ITEMS.clear()
        # 排序是为了让同一个用例两次运行的记录可比 —— 集合序不稳定会让
        # "这条用例的 marker 变过吗"这种问题无法回答。
        assert got == "perf,platform,sut"

    def test_unknown_nodeid_is_empty_not_an_error(self):
        # 拿不到 item（老 pytest、或 hook 没跑到）时返回空串是**合理的降级**；
        # 缺陷在于"每条用例都走这条路"，而不是这条路本身存在。
        assert pytest_plugin._markers(_FakeReport("never/seen.py::test")) == ""

    def test_item_without_iter_markers_degrades_quietly(self):
        class Old:
            pass

        pytest_plugin._ITEMS["x.py::test_c"] = Old()
        try:
            assert pytest_plugin._markers(_FakeReport("x.py::test_c")) == ""
        finally:
            pytest_plugin._ITEMS.clear()

    def test_unexpected_error_is_not_swallowed(self):
        """只吞「这台 pytest 不支持」这一种情况，其它异常必须冒出来。

        这条钉的是原实现的根因：`except AttributeError: return ""` 把
        "版本不支持"和"我访问了不存在的属性"变成同一个静默结果。
        """

        class Exploding:
            def iter_markers(self):
                raise RuntimeError("这不是 AttributeError，不该被静默吞掉")

        pytest_plugin._ITEMS["x.py::test_d"] = Exploding()
        try:
            import pytest as _pytest

            with _pytest.raises(RuntimeError):
                pytest_plugin._markers(_FakeReport("x.py::test_d"))
        finally:
            pytest_plugin._ITEMS.clear()


class TestEnsureRunRow:
    """`_ensure_run_row` 补的正是"照文档独立跑会炸外键"那个洞。"""

    def test_creates_the_run_row_when_missing(self, monkeypatch):
        monkeypatch.setattr(pytest_plugin, "_RUN_ID", "standalone-x")
        pytest_plugin._ensure_run_row(store)
        with store.tx() as conn:
            row = conn.execute("SELECT kind, label, config_json FROM runs WHERE run_id=?", ("standalone-x",)).fetchone()
        assert row is not None, "缺 runs 行时没有补，add_cases 会撞外键"
        assert row["kind"] == "suite"
        assert json.loads(row["config_json"])["source"] == "metrics.pytest_plugin"

    def test_does_not_overwrite_an_existing_run(self, monkeypatch):
        """已存在的 run（`cmd_suite` 建的那条）不许被改状态。

        无条件 `upsert_run(..., status="running")` 会把 `cmd_suite` 已经
        写好的状态覆盖回 running —— 一个"顺手多写一行"引起的竞态。
        """
        store.init_db()
        store.upsert_run("existing-x", "suite", "from-cmd-suite", status="passed")
        monkeypatch.setattr(pytest_plugin, "_RUN_ID", "existing-x")
        pytest_plugin._ensure_run_row(store)
        with store.tx() as conn:
            row = conn.execute("SELECT label, status FROM runs WHERE run_id=?", ("existing-x",)).fetchone()
        assert row["label"] == "from-cmd-suite", "插件的兜底逻辑改写了已有的 label"
        assert row["status"] == "passed", "插件的兜底逻辑把已有 run 的状态改回了 running"


class TestEndToEnd:
    """起真子进程跑一遍 —— 这几条才管到 hook 本身。"""

    def _run(self, tmp_path, test_source, extra_env):
        case_dir = tmp_path / "cases"
        case_dir.mkdir(exist_ok=True)
        (case_dir / "test_demo.py").write_text(test_source, encoding="utf-8")
        db = tmp_path / "quality.db"
        env = {
            **os.environ,
            "QUALITY_DB": str(db),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONPATH": ROOT,
            **extra_env,
        }
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", str(case_dir), "-q", "-p", "metrics.pytest_plugin"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return proc, db

    def test_standalone_run_on_a_fresh_database_succeeds(self, tmp_path):
        """照文档独立跑（库里什么都没有）必须成功，而不是收尾阶段炸外键。

        修复前的形态：
        ```
        .                                                                 [100%]
        sqlite3.IntegrityError: FOREIGN KEY constraint failed   (sessionfinish)
        ```
        —— 用例全绿、屏幕上打着 `1 passed`，然后退出码非零，失败原因
        与任何一个用例都没有关系。
        """
        proc, _ = self._run(
            tmp_path,
            "def test_ok():\n    assert True\n",
            {"QUALITY_RUN_ID": "standalone-e2e"},
        )
        assert proc.returncode == 0, f"独立跑失败：{proc.stdout}\n{proc.stderr}"
        assert "IntegrityError" not in proc.stderr
        assert "已记录 1 条用例结果" in proc.stdout

    def test_markers_and_outcome_reach_the_database(self, tmp_path):
        # 装饰器用字符串拼出来，避免直接写在源码里被当成真实 marker 声明
        # （本文件自己也会被 pytest 收集，写在签名上会真的给它打上 marker）。
        at = chr(64)
        proc, db = self._run(
            tmp_path,
            "import pytest\n\n\n"
            f"{at}pytest.mark.perf\n"
            f"{at}pytest.mark.sut\n"
            "def test_marked():\n    assert True\n\n\n"
            "def test_fails():\n    assert False\n",
            {"QUALITY_RUN_ID": "markers-e2e"},
        )
        assert proc.returncode == 1, "有用例失败时 pytest 必须返回 1"
        import sqlite3

        conn = sqlite3.connect(str(db))
        rows = dict(conn.execute("SELECT nodeid, markers FROM cases").fetchall())
        conn.close()
        marked = next(v for k, v in rows.items() if "test_marked" in k)
        plain = next(v for k, v in rows.items() if "test_fails" in k)
        assert marked == "perf,sut", f"marker 没有真正落库（修复前恒为空串）：{marked!r}"
        assert plain == "", f"没打 marker 的用例不该有值：{plain!r}"

    def test_no_run_id_means_completely_silent(self, tmp_path):
        """没设 `QUALITY_RUN_ID` 时一个字节都不许写。

        这是插件的核心设计：「度量平台最忌讳顺手记一笔」，日常 `pytest`
        混进质量记录只会稀释信号。所以要验的不只是"没报错"，
        而是**库文件根本没被创建**。
        """
        env = {k: v for k, v in os.environ.items() if k != "QUALITY_RUN_ID"}
        case_dir = tmp_path / "cases"
        case_dir.mkdir(exist_ok=True)
        (case_dir / "test_demo.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
        db = tmp_path / "untouched.db"
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", str(case_dir), "-q", "-p", "metrics.pytest_plugin"],
            cwd=ROOT,
            env={**env, "QUALITY_DB": str(db), "PYTHONPATH": ROOT, "PYTHONIOENCODING": "utf-8"},
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
        assert "已记录" not in proc.stdout
        assert not db.exists(), "没设 QUALITY_RUN_ID 却建了库 —— 日常 pytest 会污染度量库"
