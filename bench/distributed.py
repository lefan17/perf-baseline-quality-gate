"""分布式压测：1 个 coordinator + N 个 worker（最小可用形态）。

## 先说清楚这件事为什么值得写

单机压测的天花板是**压测机自己**：GIL 竞争、网卡、端口耗尽都会让"服务变慢"这个
结论站不住。`bench/engine.py` 的文档里一直写着「没有分布式能力，单机就是天花板」。
这个模块把那个天花板抬高一层 —— 但只抬一层，边界写在最后。

## 三个必须解决的东西（也是这个文件存在的理由）

### 1. 时钟对齐：不是"同时开始"，是"同一个 epoch 开始"

coordinator 先算出 `start_at = time.time() + lead_s`，把**绝对时间**发给每个 worker，
worker 各自睡到那个时刻再开跑。这样做的原因很实际：

- 若用「收到命令就开始」，网络往返会把启动时间差摊开；两个 worker 差 300ms，
  在 5 秒的梯度里就是 6% 的窗口错位，RPS 直接算错；
- 若用「run 内部自己对齐倒计时」，仍然要传一个基准。

真正的多机部署还得靠 NTP/chrony 把机器时钟对齐；本模块只负责**在同一时钟下**对齐。
所以 `hello` 里带了 worker 的 `now`，coordinator 顺手算一个 skew（含一半 RTT 的误差），
这个数字跟着结果一起输出 —— 机器间时钟差到几百毫秒时，看这个值比看结论有用。

### 2. 分位不可合并：回传原始样本，池化重算

这是分布式压测最容易写错、也最难被发现的一步。

    1000 个样本全 10ms 的 worker + 100 个样本全 900ms 的 worker
    各自 p95 = 10ms / 900ms
    加权平均 → 91ms        ← 错得最厉害
    池化重算 → 900ms       ← 真相

分位数是**顺序统计量**，不是可加量：既不能取 max（保守但答非所问），
也不能按样本数加权平均（会把尾部抹平）。
所以协议里回传的是**原始耗时数组**，合并后统一走 `engine.summarize`（nearest-rank），
与单机口径逐位一致。代价是 payload 变大 —— 20 000 个样本约 300KB JSON，
在本机/内网完全可接受，跨广域网就该改成直方图并接受分桶误差。这个取舍写在这里，
而不是假装"两条路都对"。

`naive_tail_p95()` 特意保留在这个模块里：它是错的算法，但它是个**可测的对照组**，
`tests/test_distributed.py` 用它证明"合并方式会改变结论"。

### 3. 一个人跑不动的时候才需要它

本机多进程**不会**让压测更快 —— 三个 worker 抢同一块 CPU，结论往往是比单进程更差。
它的真实用途有两个：① 用多进程把压测端的 GIL/线程调度开销分散开，
验证"瓶颈到底在被测服务还是在压测机"；② 在多台机器上跑，突破单机网卡与端口上限。

所以任何一次分布式运行都必须同时报告**单机基线**，否则读者无法判断这点差异
是算法带来的还是抖动带来的。`scripts/distributed_run.py` 默认两条都跑。

## 边界（写进 README，也写在这里）

- **不是真正的"多机调度"**：没有服务发现、没有故障重试、没有心跳；
  worker 挂了 coordinator 会报错退出，而不是把它的份额分给别人 ——
  在压测里"默默少跑一半并发"比直接失败危险得多。
- **同一台机器上的分布式只是"多进程"**，不代表扩容能力。
- 协议是**单条命令一问一答**（`hello` / `run`），没有流控与背压。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from bench import scenarios as scenario_mod
from bench.engine import Runner, Sample, Stats, summarize

PROTOCOL_VERSION = 1

# 给 worker 留出的启动窗口：收到 run 之后到真正开跑之间的准备时间
# （建会话池、构造场景、Python 首次导入都在这里）。
DEFAULT_LEAD_S = 1.0


# --------------------------------------------------------------------------- #
# 分片与合并（纯函数，可单独测）
# --------------------------------------------------------------------------- #
def split_users(total: int, workers: int) -> list[int]:
    """把总并发平均分给各 worker，余数从前往后各加一个。

    拒绝"worker 分到 0 个用户"：那种 worker 会立刻返回空样本，
    合并后的 tps = 总样本 / 窗口 仍然算得出来，但"实际并发"已经不是配置值了。
    压测里最忌讳的就是报告一个没跑满的并发还当成配置值。
    """
    if workers <= 0:
        raise ValueError("workers 必须为正")
    if total < workers:
        raise ValueError(f"总并发 {total} 小于 worker 数 {workers}，会有 worker 分不到用户")
    base, extra = divmod(total, workers)
    return [base + (1 if i < extra else 0) for i in range(workers)]


def samples_from_payload(payload: dict, worker_index: int = 0) -> list[Sample]:
    """把 worker 回传的**并行数组**还原成 Sample 列表。

    用并行数组而不是对象数组，是因为 JSON 里 `[12.5,200,true,0.31]` 比
    `{"elapsed_ms":12.5,...}` 小一半以上，而这个协议最大的包就是样本。
    """
    elapsed = payload.get("elapsed_ms") or []
    status = payload.get("status") or []
    # 注意键名是 ok_flags 而不是 ok：回复顶层的 "ok" 表示**命令是否执行成功**，
    # 两个含义共用一个键，一次失败的 run 就会被读成"这条样本成功"。
    ok = payload.get("ok_flags") or []
    at = payload.get("at") or []
    n = len(elapsed)
    if not (len(status) == len(ok) == len(at) == n):
        raise ValueError("worker 回传的样本数组长度不一致，协议已不一致，拒绝合并")
    prefix = f"w{worker_index}"
    return [
        Sample(
            scenario=prefix,
            status=int(status[i]),
            elapsed_ms=float(elapsed[i]),
            ok=bool(ok[i]),
            error="" if ok[i] else f"HTTP {status[i]}",
            at=float(at[i]),
        )
        for i in range(n)
    ]


def merge_workers(
    payloads: list[dict],
    total_users: int,
    window_s: float,
    warmup_drop_s: float = 0.0,
) -> Stats:
    """把 N 个 worker 的样本**池化**后重算统计。

    `window_s` 是整轮的统一窗口（不是各 worker 的耗时之和）：
    三个 worker 各跑 5 秒是"并发跑了 5 秒"，不是 15 秒。
    """
    all_samples: list[Sample] = []
    for i, payload in enumerate(payloads):
        all_samples.extend(samples_from_payload(payload, worker_index=i))
    effective = max(0.001, window_s - warmup_drop_s)
    return summarize(all_samples, label=f"dist{len(payloads)}w/{total_users}u", users=total_users, duration_s=effective)


def naive_tail_p95(payloads: list[dict]) -> float:
    """**错误的**合并方式：按样本数加权平均各 worker 的 p95。

    留在这里是为了被测试和文档指着说"这么算会把 900ms 报成 91ms"。
    不要在任何报告口径里用它。

    两种常见的错法，说清楚差别，因为面试很容易被追问到这里：

    - **取各 worker p95 的最大值**：方向上是保守的（池化 p95 不会超过它），
      但它回答的是"最慢那个 worker 的 p95"，不是"整轮请求的 p95"；
      而且各 worker 的窗口未必一样大，混在一起没有统一定义。
    - **按样本数加权平均**（本函数）：看着最"公平"，错得最厉害 ——
      10% 的请求慢了 90 倍，按平均只把 p95 抬了 8 倍；
      尾延迟恰恰是被平均数抹掉的那个东西。

        1000 个样本全 10ms 的 worker + 100 个样本全 900ms 的 worker
        加权平均 p95 ≈ 91ms     ← 错
        池化 1100 个样本重算  = 900ms  ← 真相
    """
    total = 0
    weighted = 0.0
    for i, payload in enumerate(payloads):
        samples = samples_from_payload(payload, worker_index=i)
        st = summarize(samples, duration_s=1.0)
        weighted += st.p95_ms * st.total
        total += st.total
    return round(weighted / total, 3) if total else 0.0


# --------------------------------------------------------------------------- #
# 协议（一行一个 JSON）
# --------------------------------------------------------------------------- #
def encode_message(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


def decode_message(raw: bytes) -> dict:
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("协议消息必须是 JSON 对象")
    return payload


def call(endpoint: str, payload: dict, timeout_s: float = 60.0) -> dict:
    """向一个 worker 发一条命令并读回一条应答（一问一答，不保持连接）。"""
    host, _, port_s = endpoint.rpartition(":")
    if not host:
        raise ValueError(f"worker 地址必须是 host:port，收到 {endpoint!r}")
    with socket.create_connection((host, int(port_s)), timeout=timeout_s) as sock:
        sock.sendall(encode_message(payload))
        reader = sock.makefile("rb")
        line = reader.readline()
    if not line:
        raise ConnectionError(f"worker {endpoint} 没有回任何数据就断开了")
    return decode_message(line)


# --------------------------------------------------------------------------- #
# Worker 端
# --------------------------------------------------------------------------- #
def _run_on_worker(payload: dict) -> dict:
    """执行一条 run 命令。异常一律转成 {"ok": false}，不让 worker 直接死掉。"""
    try:
        base_url = str(payload["base_url"])
        users = int(payload["users"])
        duration_s = float(payload["duration_s"])
        timeout_s = float(payload.get("timeout_s", 10.0))
        think_time_s = float(payload.get("think_time_s", 0.0))
        ramp_up_s = float(payload.get("ramp_up_s", 0.0))
        warmup_drop_s = float(payload.get("warmup_drop_s", 0.0))
        names = payload.get("scenarios") or None
        items = payload.get("items")
        if items:
            # worker 是独立进程，必须自己知道物料范围，否则会打出一批 404，
            # 而 404 比真请求快得多 —— 错误率与 P95 会同时"变好"。
            scenario_mod.configure_item_range(int(items))

        start_at = float(payload.get("start_at") or 0.0)
        if start_at:
            delay = start_at - time.time()
            if delay > 0:
                time.sleep(delay)

        mix = scenario_mod.build_engine_mix(base_url, names, timeout_s=timeout_s)
        runner = Runner(
            base_url=base_url,
            scenarios=mix,
            users=users,
            duration_s=duration_s,
            think_time_s=think_time_s,
            ramp_up_s=ramp_up_s,
            timeout_s=timeout_s,
            warmup_drop_s=warmup_drop_s,
        )
        started = time.time()
        stats = runner.run()
        ended = time.time()
        return {
            "ok": True,
            "pid": os.getpid(),
            "users": users,
            "start_epoch": started,
            "end_epoch": ended,
            "stats": stats.as_dict(),
            "elapsed_ms": [round(s.elapsed_ms, 3) for s in stats.samples],
            "status": [s.status for s in stats.samples],
            "ok_flags": [s.ok for s in stats.samples],
            "at": [round(s.at, 3) for s in stats.samples],
        }
    except Exception as exc:  # noqa: BLE001 - worker 必须把错误原样带回 coordinator
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _handle_connection(conn: socket.socket, stop: threading.Event) -> None:
    try:
        with conn:
            reader = conn.makefile("rb")
            line = reader.readline()
            if not line:
                return
            payload = decode_message(line)
            cmd = payload.get("cmd")
            if cmd == "hello":
                reply = {
                    "ok": True,
                    "protocol": PROTOCOL_VERSION,
                    "pid": os.getpid(),
                    "now": time.time(),
                    "cwd": os.getcwd(),
                }
            elif cmd == "run":
                reply = _run_on_worker(payload)
                reply["now"] = time.time()
            elif cmd == "stop":
                reply = {"ok": True, "stopping": True}
                stop.set()
            else:
                reply = {"ok": False, "error": f"未知命令 {cmd!r}"}
            conn.sendall(encode_message(reply))
    except Exception:  # noqa: BLE001 - 单个连接的异常不能让服务循环退出
        return


def serve(host: str = "127.0.0.1", port: int = 0, ready_file: str | Path | None = None) -> int:
    """起一个 worker，阻塞直到收到 stop 命令。

    `port=0` 时由系统分配，并写进 `ready_file`（父进程靠它拿到端口）。
    用文件而不是 stdout 传端口，是因为在 Windows 上把子进程 stdout 接到管道
    再读，很容易因为缓冲/未刷新卡住 —— 这种"卡住"很难和"worker 跑得慢"区分开。
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(8)
    actual_port = srv.getsockname()[1]
    srv.settimeout(0.5)

    if ready_file:
        Path(ready_file).write_bytes(
            json.dumps({"port": actual_port, "pid": os.getpid(), "host": host}).encode("utf-8")
        )
    print(f"[worker] 监听 {host}:{actual_port} pid={os.getpid()}", flush=True)

    stop = threading.Event()
    try:
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            threading.Thread(target=_handle_connection, args=(conn, stop), daemon=True).start()
    finally:
        srv.close()
    return 0


# --------------------------------------------------------------------------- #
# Coordinator 端
# --------------------------------------------------------------------------- #
@dataclass
class WorkerInfo:
    endpoint: str
    pid: int = 0
    skew_ms: float = 0.0
    reply: dict = field(default_factory=dict)


@dataclass
class DistributedResult:
    stats: Stats
    payloads: list[dict]
    workers: list[WorkerInfo]
    total_users: int
    window_s: float
    naive_p95_ms: float
    start_epoch: float

    @property
    def max_skew_ms(self) -> float:
        return round(max((abs(w.skew_ms) for w in self.workers), default=0.0), 2)

    def as_dict(self) -> dict:
        return {
            "workers": len(self.workers),
            "total_users": self.total_users,
            "window_s": round(self.window_s, 3),
            "start_epoch": round(self.start_epoch, 3),
            "max_clock_skew_ms": self.max_skew_ms,
            "stats": self.stats.as_dict(),
            "naive_tail_p95_ms": self.naive_p95_ms,
            "per_worker": [p.get("stats", {}) for p in self.payloads],
        }


class Coordinator:
    """只管两件事：让所有 worker 在同一 epoch 开跑，把它们的样本池化。"""

    def __init__(self, endpoints: list[str], timeout_s: float = 120.0):
        if not endpoints:
            raise ValueError("至少要有一个 worker")
        self.endpoints = list(endpoints)
        self.timeout_s = timeout_s
        self.workers: list[WorkerInfo] = []

    def hello(self) -> list[WorkerInfo]:
        self.workers = []
        for endpoint in self.endpoints:
            sent_at = time.time()
            reply = call(endpoint, {"cmd": "hello"}, timeout_s=10.0)
            recv_at = time.time()
            if not reply.get("ok"):
                raise RuntimeError(f"worker {endpoint} hello 失败: {reply.get('error')}")
            if reply.get("protocol") != PROTOCOL_VERSION:
                raise RuntimeError(
                    f"worker {endpoint} 协议版本 {reply.get('protocol')} 与本端 {PROTOCOL_VERSION} 不一致"
                )
            # 一半 RTT 是对齐误差的上界：worker 的 now 落在 [sent_at, recv_at] 之间
            skew_ms = (float(reply.get("now", 0.0)) - (sent_at + recv_at) / 2.0) * 1000.0
            self.workers.append(
                WorkerInfo(endpoint=endpoint, pid=int(reply.get("pid", 0)), skew_ms=skew_ms, reply=reply)
            )
        return self.workers

    def run(
        self,
        *,
        base_url: str,
        users: int,
        duration_s: float,
        scenarios: list[str] | None = None,
        think_time_s: float = 0.0,
        ramp_up_s: float = 0.0,
        timeout_s: float = 10.0,
        warmup_drop_s: float = 0.0,
        items: int | None = None,
        lead_s: float = DEFAULT_LEAD_S,
    ) -> DistributedResult:
        if not self.workers:
            self.hello()
        shares = split_users(users, len(self.workers))
        start_at = time.time() + lead_s
        command = {
            "cmd": "run",
            "base_url": base_url,
            "scenarios": scenarios,
            "duration_s": duration_s,
            "think_time_s": think_time_s,
            "ramp_up_s": ramp_up_s,
            "timeout_s": timeout_s,
            "warmup_drop_s": warmup_drop_s,
            "items": items,
            "start_at": start_at,
        }

        results: list[dict | None] = [None] * len(self.workers)
        errors: list[str] = []

        def fire(index: int) -> None:
            payload = dict(command, users=shares[index])
            try:
                results[index] = call(self.endpoints[index], payload, timeout_s=self.timeout_s)
            except Exception as exc:  # noqa: BLE001 - 汇总后再统一报错，别让一个 worker 静默消失
                errors.append(f"{self.endpoints[index]}: {type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=fire, args=(i,)) for i in range(len(self.workers))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        if errors:
            raise RuntimeError("部分 worker 失败（不把份额分给别人，宁可整轮失败）: " + "; ".join(errors))
        payloads: list[dict] = []
        for i, reply in enumerate(results):
            assert reply is not None
            if not reply.get("ok"):
                raise RuntimeError(f"worker {self.endpoints[i]} 执行失败: {reply.get('error')}")
            payloads.append(reply)

        window = max(float(p.get("end_epoch", 0.0)) for p in payloads) - start_at
        stats = merge_workers(payloads, total_users=users, window_s=window, warmup_drop_s=warmup_drop_s)
        return DistributedResult(
            stats=stats,
            payloads=payloads,
            workers=self.workers,
            total_users=users,
            window_s=window,
            naive_p95_ms=naive_tail_p95(payloads),
            start_epoch=start_at,
        )

    def stop(self) -> None:
        for endpoint in self.endpoints:
            try:
                call(endpoint, {"cmd": "stop"}, timeout_s=5.0)
            except Exception:  # noqa: BLE001 - 关停阶段失败不该盖住测量结果
                continue


# --------------------------------------------------------------------------- #
# 进程入口：python -m bench.distributed --worker --port 0 --ready-file X
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="分布式压测 worker")
    parser.add_argument("--worker", action="store_true", help="以 worker 身份运行（阻塞服务）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0, help="0 表示由系统分配")
    parser.add_argument("--ready-file", default="", help="启动后把实际端口写进这个文件")
    args = parser.parse_args(argv)

    if not args.worker:
        parser.error("这个入口只用于启动 worker；编排请用 scripts/distributed_run.py")
    return serve(host=args.host, port=args.port, ready_file=args.ready_file or None)


if __name__ == "__main__":
    sys.exit(main())
