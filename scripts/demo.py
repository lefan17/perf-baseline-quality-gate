#!/usr/bin/env python
"""五分钟现场演示：一条命令跑完「跑得动 → 有数据 → 门禁会红 → 门禁会绿」。

## 演示为什么要脚本化

面试现场最怕两件事：网络不通（在线报表打不开）、以及「我改一下参数就红给你看」
这种不可复现的表演。这个脚本把四幕都钉在**本机临时库**上：

1. 起被测服务 + 跑一轮梯度压测 → 打印 R1–R5 判定（全绿）；
2. 把这轮显式设成基线（门禁的合法入场券）；
3. 把基线改快 10 倍 —— 模拟「上周的基线和今天的差距」；
4. 用 `gate --run-id` **重新判定同一个 run** → R3 变红、退出码 1；
5. 把基线改回去 → 同一个 run 重新判定 → 绿。

为什么第 4 幕用「重新判定」而不是「再跑一轮压测」：压测有噪声，再跑一轮
可能因为抖动红在别的规则上，于是你无法确定是「注入的劣化被抓到了」还是
「机器今天心情不好」。重新判定同一个 run，输入完全固定，输出必然确定——
这才是可复现的演示。

为什么不改被测代码：改代码让服务变慢，测到的是「改完恰好红了」；
改基线才是「门禁能拦住劣化」这件事本身。

所有临时文件都在 `--workdir`（默认系统临时目录）下，**不碰仓内 `quality.db`**。

## 用法

```bash
python scripts/demo.py              # 完整四幕，本机实测约 12~15 秒
python scripts/demo.py --keep       # 保留临时目录，方便演示后继续查
python scripts/demo.py --duration 1 # 更快的口播版（数字更抖，但链路一样）
```

> 早先这里写的是「约 60~90 秒」，那是照着 `STEP_DURATION = 2.0` × 4 幕
> 心算出来的，**没有实测**。实测是 12.4 秒（Windows 11 / Python 3.12.10，
> 输出末尾自己会打印 `演示结束：12.4 秒 / 5 条判定`）。
> 一个"照着算"写下的数字，和"跑一遍"写下的数字，是小项目里最容易分叉的两件事。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CLI = ROOT / "platform_cli.py"
LABEL = "demo"
ITEMS = 400
STEP_USERS = 10
STEP_DURATION = 2.0
WARMUP_DROP = 0.3

# 门禁里会被打印出来的规则行（R1–R5 是性能门禁，S1–S3 是套件门禁，本演示只跑性能）。
RULE_LINE = re.compile(r"^\[(PASS|FAIL|SKIP|WARNING)\]\s+(R\d|P\d|S\d)")


def say(text: str = "") -> None:
    """演示用的旁白：带一点节奏，让人来得及看。"""
    print(text, flush=True)


def act(number: int, total: int, title: str) -> None:
    say()
    say("=" * 72)
    say(f"  第 {number}/{total} 幕 · {title}")
    say("=" * 72)


def _env(db: Path, sut_tmp: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["QUALITY_DB"] = str(db)
    env["TMPDIR"] = str(sut_tmp)
    env["TEMP"] = str(sut_tmp)
    env["TMP"] = str(sut_tmp)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _cli(db: Path, sut_tmp: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        cwd=str(ROOT),
        env=_env(db, sut_tmp),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=900,
    )


def _show_gate_lines(stdout: str) -> list[str]:
    """只把门禁判定行挑出来打印 —— 现场不需要看压测的全部日志。"""
    lines = [ln for ln in stdout.splitlines() if RULE_LINE.match(ln)]
    for ln in lines:
        say("   " + ln)
    return lines


def _run_id(stdout: str) -> str:
    m = re.search(r"^\[run\] run_id=(\S+)", stdout, re.MULTILINE)
    if not m:
        raise SystemExit("没能从 CLI 输出里解析出 run_id：\n" + stdout[-2000:])
    return m.group(1)


def _scale_baseline(db: Path, factor: float) -> dict:
    """把基线里的延迟除以 `factor`、RPS 乘 `factor` —— 即「基线比本轮快 factor 倍」。

    方向不能反：R3 判的是「本轮比基线差多少」。把基线改**好**，本轮才显劣化；
    把基线改**坏**，本轮会显示成「改善」，门禁当然不会红。
    """
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT metrics_json FROM baselines WHERE kind='perf' AND label=?",
            (LABEL,),
        ).fetchone()
        if row is None:
            raise SystemExit("基线不存在，第 2 幕应当已经用 --set-baseline 写入")
        metrics = dict(json.loads(row[0]))
        for key in ("p95_ms", "p99_ms", "mean_ms", "max_ms"):
            if key in metrics:
                metrics[key] = round(float(metrics[key]) / factor, 3)
        if "rps" in metrics:
            metrics["rps"] = round(float(metrics["rps"]) * factor, 3)
        conn.execute(
            "UPDATE baselines SET metrics_json=? WHERE kind='perf' AND label=?",
            (json.dumps(metrics, ensure_ascii=False), LABEL),
        )
        conn.commit()
        return metrics
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="五分钟现场演示：门禁会红，也会绿")
    parser.add_argument("--duration", type=float, default=STEP_DURATION, help="每个梯度的稳态秒数")
    parser.add_argument("--items", type=int, default=ITEMS, help="被测服务数据量")
    parser.add_argument("--keep", action="store_true", help="保留临时目录（演示后可继续查）")
    parser.add_argument("--workdir", type=Path, default=None, help="临时目录位置，默认系统临时目录")
    args = parser.parse_args(argv)

    workdir = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="perf-demo-"))
    workdir.mkdir(parents=True, exist_ok=True)
    db = workdir / "quality.db"
    sut_tmp = workdir / "tmp"
    sut_tmp.mkdir(exist_ok=True)

    started = time.time()
    say("性能基线 · 质量门禁平台 —— 五分钟演示")
    say(f"临时库：{db}（不碰仓内 quality.db）")

    # ---- 第 1 幕：跑一轮，门禁全绿 ----
    act(1, 4, "跑一轮梯度压测，门禁全绿")
    say(f"  {STEP_USERS} 并发 × {args.duration}s × {args.items} 条数据（本机实测约 5~10 秒）")
    first = _cli(
        db,
        sut_tmp,
        "run",
        "--label",
        LABEL,
        "--items",
        str(args.items),
        "--step-users",
        str(STEP_USERS),
        "--step-duration",
        str(args.duration),
        "--warmup-drop",
        str(WARMUP_DROP),
    )
    run_id = _run_id(first.stdout)
    _show_gate_lines(first.stdout)
    say(f"  → run_id={run_id}，退出码 {first.returncode}")

    # ---- 第 2 幕：显式设基线 ----
    act(2, 4, "把这轮设成基线（门禁的合法入场券）")
    second = _cli(
        db,
        sut_tmp,
        "run",
        "--label",
        LABEL,
        "--items",
        str(args.items),
        "--step-users",
        str(STEP_USERS),
        "--step-duration",
        str(args.duration),
        "--warmup-drop",
        str(WARMUP_DROP),
        "--set-baseline",
    )
    baseline_run = _run_id(second.stdout)
    _show_gate_lines(second.stdout)
    say(f"  → 基线已建立（run_id={baseline_run}）")
    say("  注意：基线只由 --set-baseline / baseline set 显式写入，CI 不会自己批准自己")

    # ---- 第 3 幕：注入劣化 ----
    act(3, 4, "把基线改快 10 倍 —— 模拟「上周比现在快得多」")
    scaled = _scale_baseline(db, 10.0)
    say(f"  注入后的基线：p95={scaled.get('p95_ms')}ms / rps={scaled.get('rps')}")
    say("  只改基线，不动被测代码 —— 这样测的是「门禁能不能拦住劣化」本身")

    # ---- 第 4 幕：同一个 run 重新判定 → 红 ----
    act(4, 4, "重新判定同一个 run（输入固定，结果确定）")
    red = _cli(db, sut_tmp, "gate", "--run-id", baseline_run)
    lines = _show_gate_lines(red.stdout)
    say(f"  → 退出码 {red.returncode}（非 0 意味着流水线会在这里停下）")
    verdict_red = red.returncode

    # ---- 第 5 幕（口播彩蛋）：改回去 → 绿 ----
    say()
    say("  彩蛋：把基线改回去，同一个 run 再判一次 ——")
    _scale_baseline(db, 0.1)
    green = _cli(db, sut_tmp, "gate", "--run-id", baseline_run)
    _show_gate_lines(green.stdout)
    say(f"  → 退出码 {green.returncode}")

    # ---- 收尾 ----
    elapsed = time.time() - started
    say()
    say("=" * 72)
    say(f"  演示结束：{elapsed:.1f} 秒 / {len(lines)} 条判定")
    say("  可以讲的三个点：")
    say("   1. 门禁不是「打印一行 PASS」，它有退出码，CI 因此能真的拦住；")
    say("   2. 门禁变红必须可复现：同一个 run + 固定输入，红是确定的，不是运气；")
    say("   3. 基线只能由人显式设置 —— 否则「慢慢变慢」会被每一轮自己批准。")
    say("=" * 72)

    if args.keep:
        say(f"临时目录保留在：{workdir}")
    else:
        shutil.rmtree(workdir, ignore_errors=True)

    # 演示成功的判据：第 4 幕必须真的拦住
    if verdict_red == 0:
        say("✘ 第 4 幕没有变红 —— 演示失败（门禁失效）")
        return 1
    if green.returncode != 0:
        say("✘ 第 5 幕没有恢复成绿 —— 演示失败")
        return 1
    say("✔ 四幕全部按预期：绿 → 注入 → 红 → 恢复绿")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
