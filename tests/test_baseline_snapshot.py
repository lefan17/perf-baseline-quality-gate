"""基线快照文件（`baseline export/import`）：让"人设的基线"能被 CI 读到。

## 为什么需要这一层

`baselines` 表是本机 SQLite 里的一行，CI 是干净环境 —— runner 上那个
`quality.db` 是它自己从零建的。于是线上 R3 一直是「未设置基线，跳过劣化对比」：
门禁的核心价值（抓"慢慢变慢"）在 CI 里从未生效，而报告里那一行长得像"通过"。

修法是把那条基线导出成 `baselines/<label>.json` 提交进仓库，
CI 在跑门禁前 `baseline import` 回来。这个文件测的就是这条机制。

## 本文件刻意不启动被测服务

这里要证明的是**机制**（文件 → 库 → R3 判定），不是压测本身。
所以用 `store.upsert_run` / `store.add_steps` 造一轮入库记录，再跑真的
`platform_cli.py gate` 去重判 —— 走的是与 CI 完全同一条判定链
（`store.get_baseline` → `analysis.compare_to_baseline` → `gate.evaluate_perf`），
但不需要 20 秒压测。真实压测那一层由 `tests/test_r3_end_to_end.py` 覆盖。

## 为什么每条边界都要有

这个机制最危险的失败方式不是"报错"，而是**安静地读不对**：
一条读歪了的基线会得出"比过了，没劣化"的绿色结论。
所以缺文件、文件损坏、版本不认识、label 对不上、配置不可比 ——
每一种都必须有确定的行为，且**都不能是放行**。
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import platform_cli
from metrics import baseline, store

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "platform_cli.py"

LABEL = "snap-lab"
BASELINE_METRICS = {
    "p95_ms": 100.0,
    "p99_ms": 130.0,
    "mean_ms": 20.0,
    "max_ms": 200.0,
    "error_rate": 0.0,
    "rps": 300.0,
    "total": 5000,
}
CONFIG = {"items": 20000, "report_mode": "agg", "scenario": "(mix)", "step_users": [10, 20]}

CLI_TIMEOUT_S = 120


# --------------------------------------------------------------------------- #
# 夹具：conftest 的 autouse fixture 已把 QUALITY_DB 指到 tmp_path 下
# --------------------------------------------------------------------------- #
def _db() -> Path:
    """本次测试要用的库路径（由 conftest 隔离到 tmp_path，跨测试不串）。"""
    return store.db_path()


def _seed_run(run_id: str, *, p95_ms: float, platform_info: dict | None = None) -> None:
    """造一轮已入库的 perf 记录 —— 与 `run` 子命令落库的形状一致。

    `metrics_json` 必须是完整的一份（`baseline set` 会直接读 `p95_ms`/`p99_ms`/`rps`
    去打印），所以这里按 `analysis.merge_step_rows` 的形状造。
    """
    step = {
        "label": "10u",
        "users": 10,
        "duration_s": 8.0,
        "total": 5000,
        "ok_count": 5000,
        "error_count": 0,
        "error_rate": 0.0,
        "rps": 300.0,
        "mean_ms": 20.0,
        "p50_ms": 10.0,
        "p90_ms": 60.0,
        "p95_ms": p95_ms,
        "p99_ms": p95_ms * 1.3,
        "max_ms": 200.0,
        "max_inflight": 10,
    }
    store.upsert_run(run_id, "perf", LABEL, config=dict(CONFIG), platform_info=platform_info or {})
    store.add_steps(run_id, [step])
    metrics = {k: step[k] for k in ("total", "error_rate", "rps", "mean_ms", "p95_ms", "p99_ms", "max_ms")}
    store.finish_run(run_id, "passed", metrics)


def _write_snapshot(path: Path, **over) -> Path:
    snap = {
        "schema": baseline.SCHEMA_VERSION,
        "kind": "perf",
        "label": LABEL,
        "run_id": "perf-20261016-000000-aaaaaa",
        "set_at": "2026-10-16T00:00:00+00:00",
        "metrics": dict(BASELINE_METRICS),
        "config": dict(CONFIG),
    }
    snap.update(over)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(baseline.dump(snap), encoding="utf-8")
    return path


def _cli(*argv: str) -> subprocess.CompletedProcess:
    """在子进程里跑真的 CLI —— 继承 conftest 设好的 QUALITY_DB。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, str(CLI), *argv],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=CLI_TIMEOUT_S,
    )


def _baseline_rows() -> list[tuple]:
    """读 baselines 表。库还不存在（导入失败后没有别的写操作）时按"空"处理。"""
    db = _db()
    if not db.is_file():
        return []
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT kind, label, run_id FROM baselines").fetchall()
    finally:
        conn.close()


def _r3_line(stdout: str) -> str:
    for line in reversed(stdout.splitlines()):
        if "R3-baseline-degradation" in line:
            return line
    raise AssertionError(f"输出里没有 R3 那一行：\n{stdout[-2000:]}")


# --------------------------------------------------------------------------- #
# 序列化层
# --------------------------------------------------------------------------- #
class TestSnapshotFormat:
    def test_roundtrip_through_dump_and_parse(self):
        snap = baseline.parse(
            baseline.dump(
                {
                    "schema": baseline.SCHEMA_VERSION,
                    "kind": "perf",
                    "label": "x",
                    "run_id": "r",
                    "set_at": "t",
                    "metrics": {"p95_ms": 1.0},
                    "config": {},
                }
            )
        )
        assert snap["label"] == "x" and snap["metrics"]["p95_ms"] == 1.0

    def test_dump_keeps_chinese_readable(self):
        """快照要进 git，diff 得有人能读 —— 中文不能转成 \\uXXXX。"""
        text = baseline.dump(
            {
                "schema": 1,
                "kind": "perf",
                "label": "标签",
                "run_id": "r",
                "set_at": "t",
                "metrics": {"p95_ms": 1.0},
                "config": {"scenario": "慢报表"},
            }
        )
        assert text.endswith("\n"), "少一个换行，diff 会显示 '\\ No newline at end of file'"
        assert "标签" in text and "慢报表" in text and "\\u" not in text

    def test_snapshot_of_records_which_machine_measured_it(self):
        """记机器是**如实**，不是拿它判不可比 —— 那会让 CI 上的 R3 永久 SKIP。"""
        store.set_baseline("perf", LABEL, "r1", dict(BASELINE_METRICS), config=dict(CONFIG))
        snap = baseline.snapshot_of("perf", LABEL, platform_info={"platform": "Linux-6.17", "cpu_count": 4})
        assert snap is not None
        assert snap["platform"]["platform"] == "Linux-6.17"
        assert snap["config"]["items"] == 20000

    def test_snapshot_of_returns_none_without_a_baseline(self):
        """没有基线时导出必须是 None，而不是造一条空基线出来。"""
        assert baseline.snapshot_of("perf", "nope") is None

    @pytest.mark.parametrize(
        "bad",
        [
            "{ not json",
            "",
            json.dumps([1, 2, 3]),
            json.dumps({"schema": 99, "kind": "perf", "label": "x", "metrics": {"p95_ms": 1}, "config": {}}),
            json.dumps({"schema": 1, "kind": "perf", "metrics": {"p95_ms": 1}, "config": {}}),
            json.dumps({"schema": 1, "kind": "perf", "label": "", "metrics": {"p95_ms": 1}, "config": {}}),
            json.dumps({"schema": 1, "kind": "perf", "label": "x", "metrics": {}, "config": {}}),
            json.dumps(
                {"schema": 1, "kind": "perf", "label": "x", "metrics": {"p95_ms": 1}, "config": [], "platform": {}}
            ),
            json.dumps(
                {"schema": 1, "kind": "perf", "label": "x", "metrics": {"p95_ms": 1}, "config": {}, "platform": "Linux"}
            ),
        ],
    )
    def test_parse_rejects_everything_it_cannot_vouch_for(self, bad):
        with pytest.raises(baseline.SnapshotError):
            baseline.parse(bad)

    def test_missing_file_is_an_error_not_an_empty_baseline(self, tmp_path):
        with pytest.raises(baseline.SnapshotError) as exc:
            baseline.load_file(tmp_path / "nope.json")
        assert "找不到快照文件" in str(exc.value)

    def test_apply_refuses_a_label_mismatch(self):
        """把 A 配置的基线导进 B label，错会在很久以后才以"不可比"的形式冒出来。"""
        snap = {
            "schema": 1,
            "kind": "perf",
            "label": "other",
            "run_id": "r",
            "set_at": "t",
            "metrics": {"p95_ms": 1.0},
            "config": {},
        }
        with pytest.raises(baseline.SnapshotError):
            baseline.apply(snap, expect_label=LABEL)


# --------------------------------------------------------------------------- #
# CLI 层：硬边界（缺文件 / 损坏 / 版本不认识 / label 不符）
# --------------------------------------------------------------------------- #
class TestImportBoundaries:
    def test_missing_file_exits_nonzero_and_keeps_no_baseline(self, tmp_path):
        missing = tmp_path / "baselines" / f"{LABEL}.json"

        proc = _cli("baseline", "import", "--label", LABEL, "--file", str(missing))

        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert "导入失败" in proc.stderr and "找不到快照文件" in proc.stderr
        # 关键：失败必须留下"没有基线"，而不是一条空基线 —— 后者会让 R3 假装比过了。
        assert _baseline_rows() == []

    def test_corrupt_file_exits_nonzero(self, tmp_path):
        broken = tmp_path / "broken.json"
        broken.write_text('{"schema": 1, "kind": "perf", "label": "snap-lab", "metrics": {', encoding="utf-8")

        proc = _cli("baseline", "import", "--label", LABEL, "--file", str(broken))

        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert "不是合法的 JSON" in proc.stderr
        assert _baseline_rows() == []

    def test_unknown_schema_version_is_refused_not_guessed(self, tmp_path):
        """将来改格式时读到不认识的版本必须报错，而不是猜着读出一份假基线。"""
        path = _write_snapshot(tmp_path / "v99.json", schema=99)

        proc = _cli("baseline", "import", "--label", LABEL, "--file", str(path))

        assert proc.returncode == 2
        assert "schema" in proc.stderr and "不要猜着读" in proc.stderr
        assert _baseline_rows() == []

    def test_import_rejects_a_snapshot_for_another_label(self, tmp_path):
        path = _write_snapshot(tmp_path / "other.json", label="other-label")

        proc = _cli("baseline", "import", "--label", LABEL, "--file", str(path))

        assert proc.returncode == 2
        assert "不一致" in proc.stderr
        assert _baseline_rows() == []

    def test_export_without_a_baseline_exits_nonzero(self, tmp_path):
        out = tmp_path / "out.json"

        proc = _cli("baseline", "export", "--label", LABEL, "--file", str(out))

        assert proc.returncode == 2
        assert "没有" in proc.stderr
        assert not out.exists(), "没有基线时不该造出一个空快照文件"

    def test_export_records_the_machine_that_measured_it_not_this_one(self, tmp_path):
        """快照里的 `platform` 必须是**量出基线的那台机器**。

        真实场景：CI runner 上传了它的库，人下载到开发机上 `baseline set` +
        `baseline export`。那时本机是 Windows、被测机器是 Linux —— 写成本机
        会把"是哪台机器量的"这句关键信息记反，而它正是判断跨机器比较的依据。
        """
        runner = {"python": "3.12.15", "platform": "Linux-6.17.0-1022-azure", "cpu_count": 4}
        _seed_run("runner-run", p95_ms=120.0, platform_info=runner)
        assert _cli("baseline", "set", "--label", LABEL).returncode == 0

        out = tmp_path / "out.json"
        exported = _cli("baseline", "export", "--label", LABEL, "--file", str(out))
        assert exported.returncode == 0, exported.stdout + exported.stderr

        recorded = json.loads(out.read_text(encoding="utf-8"))["platform"]
        assert recorded["platform"] == "Linux-6.17.0-1022-azure"
        assert recorded["cpu_count"] == 4
        assert recorded != platform_cli.platform_info(), "记成本机等于把跨机器这件事抹掉了"


# --------------------------------------------------------------------------- #
# 机制层：导入的基线必须真的驱动 R3
# --------------------------------------------------------------------------- #
class TestImportDrivesR3:
    """这一组才是"CI 上真的判过"的本机证据（判定链与 CI 完全相同）。"""

    def test_imported_baseline_makes_gate_compare_instead_of_skip(self, tmp_path):
        path = _write_snapshot(tmp_path / "snap.json")
        _seed_run("curr-1", p95_ms=100.0)  # 与基线一致 → 不该拦

        imported = _cli("baseline", "import", "--label", LABEL, "--file", str(path))
        assert imported.returncode == 0, imported.stdout + imported.stderr
        assert "恢复基线" in imported.stdout

        proc = _cli("gate", "--run-id", "curr-1")
        line = _r3_line(proc.stdout)

        assert "未设置基线" not in line, f"导入之后 R3 不该再跳过：{line}"
        assert "[PASS]" in line and "全部指标在容忍带内" in line, line
        assert proc.returncode == 0

    def test_imported_baseline_can_block_a_degraded_run(self, tmp_path):
        """劣化 50%（远超 10% 容忍带）却没超绝对阈值 300ms → 拦住它的只能是 R3。"""
        path = _write_snapshot(tmp_path / "snap.json")
        _seed_run("curr-2", p95_ms=150.0)

        assert _cli("baseline", "import", "--label", LABEL, "--file", str(path)).returncode == 0
        proc = _cli("gate", "--run-id", "curr-2")
        line = _r3_line(proc.stdout)

        assert "[FAIL]" in line and "相对基线劣化" in line, line
        assert "p95_ms" in line, "必须指出是哪个指标劣化了"
        assert proc.returncode == 1, f"劣化必须让退出码为 1：\n{proc.stdout[-1500:]}"

    def test_advisory_mode_still_reports_the_degradation_but_does_not_block(self, tmp_path):
        """CI 用的 `--r3-advisory`：劣化照旧打印，但退出码是 0。

        这是"如实"的关键：报告里必须还有劣化项与百分比（不是恒 PASS、不是静默），
        同时说清这条判据在本环境噪声主导、不足以拦人。
        """
        path = _write_snapshot(tmp_path / "snap.json")
        _seed_run("curr-9", p95_ms=150.0)  # 劣化 50%，远超 10% 容忍带

        assert _cli("baseline", "import", "--label", LABEL, "--file", str(path)).returncode == 0
        proc = _cli("gate", "--run-id", "curr-9", "--r3-advisory")
        line = _r3_line(proc.stdout)

        assert "相对基线劣化" in line and "p95_ms" in line, f"劣化必须照旧打印：{line}"
        assert "[FAIL]" not in line, f"advisory 不该标 FAIL（那会被读成拦住）：{line}"
        assert "噪声主导" in line, f"必须如实说明为什么这条判据不算数：{line}"
        assert proc.returncode == 0, "advisory 模式下 R3 不拦人"

    def test_incomparable_baseline_is_skipped_not_passed(self, tmp_path):
        """快照的配置与本轮不同 → 必须报 [SKIP]（无法判定），而不是放行。

        本任务点名要求的第三条边界：`skipped`，而不是"比过了没劣化"。
        """
        other = dict(CONFIG, items=800)  # 数据量差 25×，两轮不可比
        path = _write_snapshot(tmp_path / "snap.json", config=other)
        _seed_run("curr-3", p95_ms=100.0)

        assert _cli("baseline", "import", "--label", LABEL, "--file", str(path)).returncode == 0
        proc = _cli("gate", "--run-id", "curr-3")
        line = _r3_line(proc.stdout)

        assert "[SKIP]" in line, line
        assert "不可比" in line and "items" in line, f"要说清为什么不可比：{line}"
        assert "劣化" not in line, f"不可比时不许给劣化结论：{line}"
        assert proc.returncode == 0, "不可比不是拦住，是没判定"

    def test_export_then_import_round_trips_into_a_clean_checkout(self, tmp_path):
        """模拟 CI 的跨环境路径：本地导出 → 提交 → 干净环境里导入 → 基线真的在里面。

        两阶段都用**真的 CLI**（不是直接调库），否则测不到"人怎么用"。
        """
        # ---- 阶段 1：本地有一轮 perf 记录，人显式设基线并导出 ----
        _seed_run("local-run", p95_ms=120.0)
        assert _cli("baseline", "set", "--label", LABEL).returncode == 0
        out = tmp_path / "committed" / f"{LABEL}.json"
        exported = _cli("baseline", "export", "--label", LABEL, "--file", str(out))
        assert exported.returncode == 0, exported.stdout + exported.stderr
        assert out.is_file()
        assert json.loads(out.read_text(encoding="utf-8"))["metrics"]["p95_ms"] == 120.0

        # ---- 阶段 2：干净环境（另一个库，等价于 runner 上刚 checkout 的机器）----
        ci_db = tmp_path / "ci" / "quality.db"
        ci_db.parent.mkdir()
        prev = os.environ["QUALITY_DB"]
        os.environ["QUALITY_DB"] = str(ci_db)
        try:
            assert store.get_baseline("perf", LABEL) is None, "干净环境里本来就没有基线"
            imported = _cli("baseline", "import", "--label", LABEL, "--file", str(out))
            assert imported.returncode == 0, imported.stdout + imported.stderr
            got = store.get_baseline("perf", LABEL)
        finally:
            os.environ["QUALITY_DB"] = prev

        assert got is not None, "导入之后基线必须在库里"
        assert got["metrics"]["p95_ms"] == 120.0
        # 配置快照必须一起回来，否则可比性校验会退化成"无法判定"。
        assert got["config"]["items"] == 20000
        assert got["config"]["step_users"] == [10, 20]
