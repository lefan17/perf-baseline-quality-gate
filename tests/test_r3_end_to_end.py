"""R3 端到端：钉住「基线劣化」这条门禁真的会判、也真的会红。

## 为什么必须有这个文件

`metrics/gate.py:371-393`：`baseline is None` 时 R3（`R3-baseline-degradation`，
本平台对外宣称的核心卖点「抓住慢慢变慢」）曾经直接 `passed=True` + 一条
「未设置基线，跳过劣化对比」（现在改成 `skipped=True` + `[SKIP]`，见 `:379-380`）。
而「恢复基线」步（现在在 `.github/workflows/quality-gate.yml:275`）曾经用
`download-artifact` 拉 `quality-db-and-reports` —— 那个 artifact 是**同一个 run
自己**在 `:214` 上传的（由 `platform-tests` job 产出），所以 `baselines` 表恒空。
这段历史记在 `quality-gate.yml:260-266` 的注释里。

两者叠加的后果：这条规则不会报错、不会变红，只会安静地打印一行 `[PASS]`，
于是在流水线上**从未真正判过一次**。

## 为什么纯函数用例抓不到它

`tests/test_platform.py` 里那些用例自己构造 `baseline` 传进 `evaluate_perf`，
而缺陷不在函数内部，在**调用链有没有走到那个函数**。所以本文件刻意不测函数，
只测一条链（真实 CLI + 真实 sqlite，库落在 pytest 临时目录）：

    run --set-baseline（显式建基线，退出码 0）
      → 同配置再跑一轮（断言 R3 真的做了对比，而不是「跳过」）
      → 把基线改快 10×（= 本轮相对基线劣化 10×，断言 R3 变红、退出码 1）

## 为什么不顺手「让 CI 自动把本轮设成基线」

`quality-gate.yml:260-263` 的注释写明了理由：那样「慢慢变慢」会被每一轮
自己批准，门禁失效。这是设计意图，本文件不改变它 —— 它只把「人显式设过
基线之后，R3 会不会真判、能不能拦住」钉成回归测试。

## 参数为什么这么小

`--items 400 / --step-users 10 / --step-duration 2`：单轮实测约 5 秒，
三轮合计约 20 秒。目的是验证链路，不是量性能上限（真实验收数字由
`run --items 20000` 单独跑并写进 docs/）。

## 为什么中间那轮不断言「一定 PASS」

R3 比的指标里有 `max_ms`（单次最大延迟），本机实测同配置连跑会抖出 >10%
的差值，所以同一份代码也可能被判成劣化。**那不是被测对象的缺陷，是这条
指标的性质**。本阶段要证明的性质是「真的做了对比」，因此 PASS 与 FAIL
都算通过，但两者必须给出可辨识的判定文本（`全部指标在容忍带内` /
`相对基线劣化 N 项`），不能是「未设置基线，跳过劣化对比」。
第三阶段注入的劣化是 10×，远超容忍带，所以那一步的判定是确定性的。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "platform_cli.py"

pytestmark = [pytest.mark.sut, pytest.mark.perf]

LABEL = "r3-e2e"
ITEMS = 400
STEP_USERS = (10,)
STEP_DURATION = 2.0
WARMUP_DROP = 0.3

# 跳过劣化对比时 R3 会打印的那句话。它的存在本身就是「从未真判」的证据，
# 所以每一处断言都把「出现它」当成失败。
SKIPPED_MARK = "未设置基线"
# 「真判」的两种可辨识结局。
COMPARED_MARKS = ("全部指标在容忍带内", "相对基线劣化")


def _run_cli(db: Path, sut_tmp: Path, *extra: str) -> subprocess.CompletedProcess:
    """跑一次真实 CLI。

    库与 SUT 临时目录都指到 pytest 的 tmp_path：
    * `QUALITY_DB` 隔离结果库（conftest 的 autouse 装置只覆盖同进程，
      这里是子进程，必须显式传），保证**不碰仓内 quality.db**；
    * `TMPDIR` 让 `sut.server_ctl` 里的 `tempfile.mkdtemp()` 也落在临时目录，
      而不是系统 Temp（本机沙箱下系统 Temp 对 Python 子进程不可写）。
    """
    env = os.environ.copy()
    env["QUALITY_DB"] = str(db)
    env["TMPDIR"] = str(sut_tmp)
    env["PYTHONIOENCODING"] = "utf-8"
    cmd = [
        sys.executable,
        str(CLI),
        "run",
        "--label",
        LABEL,
        "--items",
        str(ITEMS),
        "--step-users",
        *(str(u) for u in STEP_USERS),
        "--step-duration",
        str(STEP_DURATION),
        "--warmup-drop",
        str(WARMUP_DROP),
        *extra,
    ]
    return subprocess.run(
        cmd,
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )


def _r3_line(stdout: str) -> str:
    lines = [ln for ln in stdout.splitlines() if "R3-baseline-degradation" in ln]
    assert lines, f"CLI 输出里没有 R3 行：\n{stdout[-2000:]}"
    return lines[-1]


def _run_id_of(stdout: str) -> str:
    m = re.search(r"^\[run\] run_id=(\S+)", stdout, re.MULTILINE)
    assert m, f"CLI 输出里没有 run_id：\n{stdout[-2000:]}"
    return m.group(1)


def _gate_r3_message(db: Path, run_id: str) -> str:
    """从结果库读回该轮的 R3 判定文本。

    比解析 stdout 更硬：它证明判定**真的落库**了，而不只是被打印出来。
    """
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT message FROM gate_results WHERE run_id=? AND rule_id='R3-baseline-degradation'"
            " ORDER BY id DESC LIMIT 1",
            (run_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, f"结果库里没有 run {run_id} 的 R3 判定"
    return row[0]


def _shrink_baseline(db: Path, factor: float = 10.0) -> dict:
    """把已建立的基线改**快** `factor` 倍 —— 于是本轮相对它变成劣化。

    方向不能搞反：R3 判的是「本轮比基线差了多少」，所以要让门禁红，
    必须把**基线**改好（延迟除、RPS 乘），而不是把它改坏 —— 改坏只会让
    本轮看起来是「改善」，判出 `相对基线劣化 0 项`。

    这是本文件里唯一的人为注入点：不失真的那种做法（等真实劣化发生）
    需要改被测代码，而那样测到的是「改完之后恰好红了」，不是「门禁能拦住」。
    """
    conn = sqlite3.connect(db)
    try:
        row = conn.execute("SELECT metrics_json FROM baselines WHERE kind='perf' AND label=?", (LABEL,)).fetchone()
        assert row is not None, "基线应当已由 `run --set-baseline` 写入"
        metrics = json.loads(row[0])
        shrunk = dict(metrics)
        for key in ("p95_ms", "p99_ms", "mean_ms", "max_ms"):
            if key in shrunk:
                shrunk[key] = round(float(shrunk[key]) / factor, 3)
        if "rps" in shrunk:
            shrunk["rps"] = round(float(shrunk["rps"]) * factor, 3)
        conn.execute(
            "UPDATE baselines SET metrics_json=? WHERE kind='perf' AND label=?",
            (json.dumps(shrunk, ensure_ascii=False), LABEL),
        )
        conn.commit()
    finally:
        conn.close()
    return shrunk


def test_r3_skips_only_when_no_baseline_was_ever_set(tmp_path):
    """没有基线时 R3 仍然是「跳过」—— 把现状语义钉死，防止被顺手改成别的样子。

    这条不是缺陷：`baselines` 表只由 `--set-baseline`/`baseline set` 显式写入
    （见 `metrics/store.py` 的模块 docstring）。要防的是**只有这一种情况**
    才能是跳过 —— 一旦基线存在，就绝不允许再打印这句话。
    """
    db = tmp_path / "quality.db"
    sut_tmp = tmp_path / "sut-tmp"
    sut_tmp.mkdir()

    proc = _run_cli(db, sut_tmp)
    line = _r3_line(proc.stdout)

    assert SKIPPED_MARK in line, f"没有基线时 R3 应当跳过，实际：{line}"
    assert "[SKIP]" in line, f"跳过必须标成 SKIP —— 标 [PASS] 会被读成「比过了没劣化」，实际：{line}"
    assert proc.returncode == 0, f"没有基线不该拦住构建，实际退出码 {proc.returncode}"


def test_r3_compares_and_can_block_end_to_end(tmp_path):
    """核心：基线存在时 R3 必须真判；基线变慢时 R3 必须变红并让退出码为 1。"""
    db = tmp_path / "quality.db"
    sut_tmp = tmp_path / "sut-tmp"
    sut_tmp.mkdir()

    # ---- 阶段 1：显式建立基线（质量门禁的合法入场券，且必须退出码 0）----
    first = _run_cli(db, sut_tmp, "--set-baseline")
    assert first.returncode == 0, f"建立基线那轮不该被拦：\n{first.stdout[-2000:]}"
    assert "[baseline] 已把本轮设为基线" in first.stdout, first.stdout[-2000:]

    conn = sqlite3.connect(db)
    try:
        stored = conn.execute("SELECT run_id FROM baselines WHERE kind='perf' AND label=?", (LABEL,)).fetchone()
    finally:
        conn.close()
    assert stored is not None, "`--set-baseline` 之后 baselines 表里必须有这条基线"

    # ---- 阶段 2：基线已存在时，R3 必须真的做对比（这一步消灭「从未真判过」）----
    second = _run_cli(db, sut_tmp)
    line = _r3_line(second.stdout)
    assert SKIPPED_MARK not in line, f"基线已存在，R3 却还在跳过 —— 「抓住慢慢变慢」在流水线上是装饰：{line}"
    assert any(mark in line for mark in COMPARED_MARKS), (
        f"R3 既没说在容忍带内、也没说劣化了几项，判定文本不可辨识：{line}"
    )
    assert SKIPPED_MARK not in _gate_r3_message(db, _run_id_of(second.stdout)), "落库的 R3 判定仍然是「未设置基线」"

    # ---- 阶段 3：把基线改快 10×（= 本轮相对基线劣化 10×），R3 必须变红 ----
    shrunk = _shrink_baseline(db)
    assert shrunk["p95_ms"] < 100.0, f"注入后基线应当明显更快：{shrunk}"

    third = _run_cli(db, sut_tmp)
    line = _r3_line(third.stdout)
    assert "[FAIL]" in line, f"本轮 p95 是基线的 10×，却仍没红：{line}"
    assert "R3-baseline-degradation" in line, line
    assert "相对基线劣化" in line, f"红灯必须说清劣化了什么：{line}"
    assert third.returncode == 1, (
        f"存在 blocking 未通过时退出码必须是 1（CI 靠这个红），实际 {third.returncode}：\n{third.stdout[-2000:]}"
    )
