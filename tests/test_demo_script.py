"""`scripts/demo.py` 的两个纯逻辑：解析 run_id、以及「把基线改快」的方向。

演示脚本本身要起真服务、真压测（约 12 秒），不适合放进每次 CI 的用例集；
但它的**判据**必须钉住，否则「演示看起来是红的」这件事没有保证：

* `_run_id` 解析错了 → 第 4 幕会拿错的 run 重新判定，红/绿都说明不了问题；
* `_scale_baseline` 方向搞反了（把基线改坏而不是改好）→ 本轮会显示成
  「改善」，门禁当然不会红，于是演示失败看起来像门禁失效。

所以这里只测这两件事，用最小的 sqlite 造 `baselines` 表，不碰真库。
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.platform


def _demo_module():
    """按路径加载 `scripts/demo.py`（`scripts/` 不是包，不能 `import scripts.demo`）。"""
    path = ROOT / "scripts" / "demo.py"
    spec = importlib.util.spec_from_file_location("_demo_under_test", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def demo():
    return _demo_module()


def _seed_baseline(db: Path, metrics: dict) -> None:
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE baselines (kind TEXT, label TEXT, metrics_json TEXT)")
        conn.execute(
            "INSERT INTO baselines VALUES ('perf', 'demo', ?)",
            (json.dumps(metrics),),
        )
        conn.commit()
    finally:
        conn.close()


def _read_metrics(db: Path) -> dict:
    conn = sqlite3.connect(db)
    try:
        row = conn.execute("SELECT metrics_json FROM baselines WHERE kind='perf' AND label='demo'").fetchone()
        assert row is not None
        return json.loads(row[0])
    finally:
        conn.close()


def test_run_id_is_parsed_from_cli_output(demo):
    stdout = "[run] run_id=perf-20261025-141358-27b22a\n[run] 完成\n"
    assert demo._run_id(stdout) == "perf-20261025-141358-27b22a"


def test_run_id_missing_fails_loudly(demo):
    with pytest.raises(SystemExit):
        demo._run_id("[run] 没有 run_id 的输出版本\n")


def test_scale_baseline_makes_the_baseline_faster(demo, tmp_path):
    """基线变快 → 同一轮数据相对它就成了劣化（这是演示第 3 幕的核心方向）。"""
    db = tmp_path / "quality.db"
    _seed_baseline(db, {"p95_ms": 100.0, "p99_ms": 200.0, "mean_ms": 50.0, "max_ms": 400.0, "rps": 100.0})

    demo._scale_baseline(db, 10.0)
    metrics = _read_metrics(db)

    assert metrics["p95_ms"] == pytest.approx(10.0)
    assert metrics["max_ms"] == pytest.approx(40.0)
    assert metrics["rps"] == pytest.approx(1000.0)


def test_scale_baseline_is_reversible(demo, tmp_path):
    """第 5 幕要能改回去：10× 再 0.1× 必须回到原值（否则「恢复绿」不成立）。"""
    db = tmp_path / "quality.db"
    original = {"p95_ms": 100.0, "p99_ms": 200.0, "mean_ms": 50.0, "max_ms": 400.0, "rps": 100.0}
    _seed_baseline(db, original)

    demo._scale_baseline(db, 10.0)
    demo._scale_baseline(db, 0.1)
    metrics = _read_metrics(db)

    for key, value in original.items():
        assert metrics[key] == pytest.approx(value, rel=1e-6), key


def test_scale_baseline_refuses_when_no_baseline_exists(demo, tmp_path):
    """没有基线时应当明确失败，而不是造一个空基线让演示「看起来红了」。"""
    db = tmp_path / "quality.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE baselines (kind TEXT, label TEXT, metrics_json TEXT)")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(SystemExit):
        demo._scale_baseline(db, 10.0)
