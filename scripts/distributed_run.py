"""分布式压测跑一轮：1 个 coordinator + N 个 worker。

## 用法

三个 worker 都在本机（多进程）：

    python scripts/distributed_run.py --workers 3 --users 24 --duration 8 --items 5000

worker 在别的机器上（先在那台机器上起 worker，再把地址传进来）：

    # 机器 B
    python -m bench.distributed --worker --host 0.0.0.0 --port 9101
    # 机器 A
    python scripts/distributed_run.py --remote 10.0.0.12:9101 --remote 10.0.0.13:9101 ...

默认会**同时**跑一次单机基线（同样的总并发、同样的数据量），因为
"分布式比单机快 x%" 这个说法只有在同一台机器、同一批数据上才有意义；
少了基线，读者无法判断差异是算法带来的还是抖动带来的。

## 输出两个 p95，是故意的

`pooled p95` 是正确口径（池化原始样本重算）；`naive p95` 是"按样本数加权平均各
worker 的 p95"—— 错的算法。两个都打出来，是因为这一个数字之差就把"分位不可合并"
讲清楚了：

    1000 个样本全 10ms 的 worker + 100 个样本全 900ms 的 worker
    加权平均 ≈ 91ms     ← naive
    池化重算 = 900ms    ← 真相
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    # `python scripts/x.py` 时 sys.path[0] 是 scripts/，import bench 会失败。
    sys.path.insert(0, str(ROOT))

from bench.distributed import Coordinator  # noqa: E402
from bench.engine import Runner  # noqa: E402
from bench import scenarios as scenario_mod  # noqa: E402
from sut.server_ctl import start_sut  # noqa: E402


def _spawn_worker(ready_file: Path, python: str, host: str = "127.0.0.1") -> subprocess.Popen:
    """起一个本地 worker 进程，等它把端口写进 ready_file 再返回。"""
    if ready_file.exists():
        ready_file.unlink()
    proc = subprocess.Popen(  # noqa: S603 - 参数是常量，没有 shell
        [python, "-m", "bench.distributed", "--worker", "--host", host, "--port", "0", "--ready-file", str(ready_file)],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    deadline = time.time() + 20.0
    while time.time() < deadline:
        if ready_file.exists():
            return proc
        if proc.poll() is not None:
            raise RuntimeError(f"worker 进程启动即退出，退出码 {proc.returncode}")
        time.sleep(0.05)
    proc.kill()
    raise RuntimeError("worker 20 秒内没有写出就绪文件")


def _run_single(
    base_url: str,
    users: int,
    duration_s: float,
    names: list[str] | None,
    timeout_s: float,
    warmup_drop_s: float,
):
    mix = scenario_mod.build_engine_mix(base_url, names, timeout_s=timeout_s)
    # 超时**只**在这里生效：`build_engine_mix(..., timeout_s=)` 把它烘进了
    # 场景闭包。`Runner` 刻意没有 `timeout_s` 参数（见 bench/engine.py 里
    # `Runner` 的 docstring）—— 它曾经有，且从未被读过，属于
    # 「看起来能配、实际不归它管」的参数，已于本版删除。
    # 这里曾经多写一行 `timeout_s=timeout_s`，被 mypy 的 call-arg 抓到。
    runner = Runner(
        base_url=base_url,
        scenarios=mix,
        users=users,
        duration_s=duration_s,
        warmup_drop_s=warmup_drop_s,
    )
    return runner.run()


def _fmt(stats, label: str, naive: float | None = None) -> str:
    line = (
        f"{label:<14} n={stats.total:<7} rps={stats.rps:8.2f} "
        f"p50={stats.p50_ms:8.2f} p95={stats.p95_ms:8.2f} p99={stats.p99_ms:8.2f} "
        f"err={stats.error_rate:7.2%}"
    )
    if naive is not None:
        line += f"  naive_p95={naive:8.2f}"
    return line


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="分布式压测（1 coordinator + N worker）")
    parser.add_argument("--workers", type=int, default=2, help="本机起几个 worker 进程")
    parser.add_argument("--remote", action="append", default=[], help="已存在的 worker 地址 host:port，可重复")
    parser.add_argument("--users", type=int, default=20, help="总并发（会被拆给各 worker）")
    parser.add_argument("--duration", type=float, default=8.0)
    parser.add_argument("--items", type=int, default=5000, help="SUT 灌入的物料条数")
    parser.add_argument("--report-mode", default="agg", choices=["agg", "n1"])
    parser.add_argument("--scenario", default="", help="只压单个场景，如 items_list")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--warmup-drop", type=float, default=1.0)
    parser.add_argument("--no-single", action="store_true", help="跳过单机基线（只跑分布式）")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    names = [args.scenario] if args.scenario else None
    python = sys.executable
    tmp_dir = Path(os.environ.get("TEMP") or ".") / f"perf-dist-{os.getpid()}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    procs: list[subprocess.Popen] = []
    endpoints: list[str] = list(args.remote)

    handle = start_sut(report_mode=args.report_mode, items=args.items)
    # 两个进程都按同一批物料取随机 ID；不设的话会打出一批 404，
    # 而 404 比真请求快得多，错误率与 P95 会同时"变好"。
    scenario_mod.configure_item_range(args.items)
    print(f"[sut] {handle.base_url} items={args.items} report_mode={args.report_mode}")

    try:
        for i in range(args.workers if not args.remote else 0):
            ready = tmp_dir / f"worker-{i}.json"
            procs.append(_spawn_worker(ready, python))
            info = json.loads(ready.read_text(encoding="utf-8"))
            endpoints.append(f"{info['host']}:{info['port']}")
            print(f"[worker] #{i} pid={info['pid']} {info['host']}:{info['port']}")
        if not endpoints:
            raise SystemExit("没有可用的 worker（--workers 0 且没给 --remote）")

        coordinator = Coordinator(endpoints)
        infos = coordinator.hello()
        for info in infos:
            print(f"[hello] {info.endpoint} pid={info.pid} 时钟偏移≈{info.skew_ms:+.2f}ms")

        single = None
        if not args.no_single:
            print("\n[baseline] 单机基线（同样的总并发、同样的数据量）")
            single = _run_single(handle.base_url, args.users, args.duration, names, args.timeout, args.warmup_drop)
            print(_fmt(single, f"single/{args.users}u"))

        print(f"\n[dist] {len(endpoints)} worker，总并发 {args.users}")
        result = coordinator.run(
            base_url=handle.base_url,
            users=args.users,
            duration_s=args.duration,
            scenarios=names,
            timeout_s=args.timeout,
            warmup_drop_s=args.warmup_drop,
            items=args.items,
        )
        print(_fmt(result.stats, f"dist/{len(endpoints)}w", naive=result.naive_p95_ms))
        print(f"  窗口 {result.window_s:.2f}s，最大时钟偏移 ≈ {result.max_skew_ms}ms")
        for i, payload in enumerate(result.payloads):
            st = payload.get("stats", {})
            print(
                f"  worker#{i} users={payload.get('users')} n={st.get('total', 0):<6} "
                f"rps={st.get('rps', 0):8.2f} p95={st.get('p95_ms', 0):8.2f}"
            )

        if single is not None and single.rps > 0:
            print(
                f"\n[对比] 分布式/单机 RPS = {result.stats.rps / single.rps:.2f}x，"
                f"p95 = {result.stats.p95_ms / max(single.p95_ms, 0.001):.2f}x"
                "\n  注意：同一台机器上的『分布式』只是多进程，抢的是同一块 CPU。"
                "这个比值反映的是压测端的并发开销怎么分摊，不是被测服务的扩容能力。"
            )

        if args.json:
            print(
                json.dumps(
                    {
                        "sut": handle.base_url,
                        "items": args.items,
                        "single": single.as_dict() if single else None,
                        "distributed": result.as_dict(),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        return 0
    finally:
        try:
            if endpoints:
                Coordinator(endpoints).stop()
        except Exception:  # noqa: BLE001 - 关停失败不该掩盖测量结果
            pass
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
        handle.stop()
        for leftover in tmp_dir.glob("*.json"):
            leftover.unlink(missing_ok=True)
        if tmp_dir.exists():
            tmp_dir.rmdir()


if __name__ == "__main__":
    sys.exit(main())
