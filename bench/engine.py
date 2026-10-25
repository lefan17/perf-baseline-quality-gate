"""自研压测引擎。

## 为什么不用 Locust

Locust 是更好的"压测工具"，但这个项目要的不是"压一下看看"，而是
**能定位拐点的度量**。这两件事对引擎的要求不一样：

1. **要拿到原始样本，自己算分位数。** Locust 只给你它算好的结果，
   而本项目要对比"最近一小时的样本"和"CI 里的样本"用的是同一套口径，
   换工具就得重新校准阈值。分位数算法（nearest-rank）写在这里，
   就是可解释、可复现的。
2. **要精确控制梯度。** 拐点定位靠的是"并发 10→20→40→80 时，
   每用户吞吐在哪一步塌了"。Locust 的 `--headless -u -r` 是连续爬坡，
   每个梯度的稳态窗口是模糊的；这里直接"每个梯度跑满 N 秒稳态再换"。
3. **不能引入第二个 HTTP 栈。** 平台侧已经有 `requests`，
   压测脚本复用同一套会话/超时/重试语义，报告的结论和功能用例的结论
   指向同一个客户端行为。

代价要说清楚：**这个引擎本身没有分布式能力**，单进程就是它的天花板。
2026-10 补了一个**最小**的分布式形态（`bench/distributed.py`：1 个 coordinator +
N 个 worker，靠"同一 epoch 开跑 + 回传原始样本池化重算"保证口径一致），
但它只是把压测端的并发摊到多个进程/机器上，**没有**服务发现、故障重投、
心跳与结果落库；同一台机器上多进程抢的是同一块 CPU，不代表扩容能力。
结论的适用范围仍然必须写明（见 README「诚实边界」）。
"""

from __future__ import annotations

import math
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

import requests


# --------------------------------------------------------------------------- #
# 分位数
# --------------------------------------------------------------------------- #
def percentile(values: Sequence[float], q: float) -> float:
    """nearest-rank 分位数（q 取 0~100）。

    选 nearest-rank 而不是线性插值，是因为它**永远返回一个真实观测到的值**。
    报告里写"P95 = 12.3ms"，这个 12.3ms 是某一类用户身上真发生过的延迟，
    而不是两个样本插出来的数字。面试被追问"你的 P95 怎么算的"时，
    这个选择本身就是能讲的东西。
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    if q <= 0:
        return float(ordered[0])
    idx = math.ceil(q / 100.0 * len(ordered)) - 1
    idx = max(0, min(idx, len(ordered) - 1))
    return float(ordered[idx])


# --------------------------------------------------------------------------- #
# 样本与统计
# --------------------------------------------------------------------------- #
@dataclass
class Sample:
    scenario: str
    status: int
    elapsed_ms: float
    ok: bool
    error: str = ""
    at: float = 0.0  # 相对本次运行开始的秒数


@dataclass
class Stats:
    """一次运行（或一个梯度）的统计结果。"""

    label: str = ""
    users: int = 0
    duration_s: float = 0.0
    total: int = 0
    ok_count: int = 0
    error_count: int = 0
    error_breakdown: dict[str, int] = field(default_factory=dict)
    status_breakdown: dict[str, int] = field(default_factory=dict)
    rps: float = 0.0
    mean_ms: float = 0.0
    p50_ms: float = 0.0
    p90_ms: float = 0.0
    p95_ms: float = 0.0
    p99_ms: float = 0.0
    max_ms: float = 0.0
    error_rate: float = 0.0
    rps_per_user: float = 0.0
    samples: list[Sample] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "samples"}
        return d


def summarize(samples: Iterable[Sample], label: str = "", users: int = 0, duration_s: float = 0.0) -> Stats:
    samples = list(samples)
    st = Stats(label=label, users=users, duration_s=round(duration_s, 3))
    st.total = len(samples)
    if not samples:
        return st

    latencies = [s.elapsed_ms for s in samples]
    st.ok_count = sum(1 for s in samples if s.ok)
    st.error_count = st.total - st.ok_count
    st.error_rate = round(st.error_count / st.total, 6)
    st.rps = round(st.total / duration_s, 3) if duration_s > 0 else 0.0
    st.rps_per_user = round(st.rps / users, 4) if users else 0.0
    st.mean_ms = round(sum(latencies) / len(latencies), 3)
    st.p50_ms = round(percentile(latencies, 50), 3)
    st.p90_ms = round(percentile(latencies, 90), 3)
    st.p95_ms = round(percentile(latencies, 95), 3)
    st.p99_ms = round(percentile(latencies, 99), 3)
    st.max_ms = round(max(latencies), 3)

    for s in samples:
        st.status_breakdown[str(s.status)] = st.status_breakdown.get(str(s.status), 0) + 1
        if not s.ok and s.error:
            key = s.error.split(":", 1)[0]
            st.error_breakdown[key] = st.error_breakdown.get(key, 0) + 1
    st.samples = samples
    return st


def merge_stats(stats: list[Stats], label: str) -> Stats:
    """把多个梯度/多次运行合并成一份总账。

    ## 用之前先想清楚：口径必须和 `_merge_from_rows` 一致

    这个函数把所有梯度的**原始样本池化**后重算分位数，
    而 `platform_cli._merge_from_rows` 拿不到样本，只能取各梯度的**最大值**。
    两者**不是同一个口径**：

        各梯度 p95 = [83.8, 128.9]
        池化（本函数）  → 105.9 ms
        取 max（入库路径）→ 128.9 ms

    曾经 `run` 用本函数、`gate` 用最大值，于是同一个 run
    在报告里和门禁里是两个数字，出了争议没人解释得清。

    现在整轮汇总统一走 `_merge_from_rows`，本函数只保留给
    **单轮内的多次运行合并**（例如同一配置重复 N 次取总账），
    不要把它用在"报告 vs 门禁"这条链路上。
    """
    all_samples: list[Sample] = []
    duration = 0.0
    for st in stats:
        all_samples.extend(st.samples)
        duration += st.duration_s
    users = max((st.users for st in stats), default=0)
    return summarize(all_samples, label=label, users=users, duration_s=duration)


# --------------------------------------------------------------------------- #
# 运行器
# --------------------------------------------------------------------------- #
ScenarioFn = Callable[[requests.Session], Sample]


def build_picker(weights: Sequence[float], rnd: random.Random) -> Callable[[], int]:
    """按权重构造"选场景"的闭包，随机源由调用方传入。

    为什么把随机源做成参数而不是模块级共享：见 `Runner` 的说明。
    权重要先归一化（调用方保证），这里只负责累积分布。
    """
    cum: list[float] = []
    acc = 0.0
    for w in weights:
        acc += w
        cum.append(acc)
    last = len(cum) - 1

    def pick() -> int:
        r = rnd.random()
        for j, c in enumerate(cum):
            if r <= c:
                return j
        return last

    return pick


class Runner:
    """定并发压测执行器。

    并发模型是**线程池**；Python 的 GIL 在这里不是瓶颈，因为线程绝大部分时间
    阻塞在 socket 上。真正会误导人的是**压测机自身的瓶颈**——GIL 竞争、
    事件循环、网卡打满都会让"服务变慢"这个结论站不住。
    所以引擎顺带采了 `inflight`（实际在途请求数）：配置 50 并发但实测只有 12，
    说明瓶颈在压测机，不在被测服务。

    场景选择的随机源是**每个 worker 一个** `random.Random(seed + worker_id)`，
    不是所有 worker 共享一个。这里曾经写的是共享实例并注释「同样的配置，
    同样的场景选择序列」——那句话是假的：谁先拿到 GIL 谁先取随机数，
    线程调度变了序列就变了；顺带还让随机数生成在 GIL 下变成串行点之一，
    把压测端自己的争用混进了"服务变慢"的数字里。
    """

    def __init__(
        self,
        base_url: str,
        scenarios: Sequence[tuple[ScenarioFn, int]],
        users: int,
        duration_s: float,
        think_time_s: float = 0.0,
        ramp_up_s: float = 0.0,
        timeout_s: float = 10.0,
        warmup_drop_s: float = 0.0,
        seed: int = 42,
    ):
        if not scenarios:
            raise ValueError("至少要有一个场景")
        total_weight = sum(w for _, w in scenarios)
        if total_weight <= 0:
            raise ValueError("场景权重之和必须为正")
        self.base_url = base_url.rstrip("/")
        self.scenarios = scenarios
        self.weights = [w / total_weight for _, w in scenarios]
        self.users = users
        self.duration_s = duration_s
        self.think_time_s = think_time_s
        self.ramp_up_s = ramp_up_s
        self.timeout_s = timeout_s
        self.warmup_drop_s = warmup_drop_s
        self.seed = seed

        self._samples: list[Sample] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._inflight = 0
        self._inflight_samples: list[int] = []
        self._start = 0.0

    # -- 内部 ------------------------------------------------------------- #
    def _record(self, s: Sample) -> None:
        with self._lock:
            self._samples.append(s)

    def _worker(self, worker_id: int, spawn_delay: float) -> None:
        if spawn_delay:
            time.sleep(spawn_delay)
        session = requests.Session()
        # 压测打的是本机 SUT。代理会把它变成"代理的性能"：延迟里混进代理
        # 转发的开销，而 P95/尾比这些数字正是本仓全部结论的来源。
        session.trust_env = False
        session.headers.update({"User-Agent": "perf-baseline-platform/1.0", "Connection": "keep-alive"})
        # 每个 worker 独立的随机源：种子由 (seed, worker_id) 决定，
        # 所以"同样的配置 + 同样的并发数"是真的可复现，而不是取决于线程调度。
        pick = build_picker(self.weights, random.Random(self.seed + worker_id))
        try:
            while not self._stop.is_set():
                fn = self.scenarios[pick()][0]
                with self._lock:
                    self._inflight += 1
                try:
                    sample = fn(session)
                except Exception as exc:  # noqa: BLE001 - 引擎必须吞掉任何异常并记为失败
                    sample = Sample(
                        scenario=getattr(fn, "__name__", "unknown"),
                        status=0,
                        elapsed_ms=0.0,
                        ok=False,
                        error=f"{type(exc).__name__}: {exc}",
                        at=time.perf_counter() - self._start,
                    )
                finally:
                    with self._lock:
                        self._inflight -= 1
                # 在途时间戳在引擎这一层盖，而不是让每个场景自己记：
                # 场景作者忘一次，热身窗口就会静默失效（样本 at 全是 0，
                # 全被当成热身丢掉，过滤逻辑又会因为"丢完没剩"而整体回退）。
                sample.at = time.perf_counter() - self._start
                self._record(sample)
                if self.think_time_s:
                    self._stop.wait(self.think_time_s)
        finally:
            session.close()

    def _sampler(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                self._inflight_samples.append(self._inflight)
            self._stop.wait(0.1)

    # -- 对外接口 --------------------------------------------------------- #
    def run(self) -> Stats:
        try:
            self._samples = []
            self._inflight_samples = []
            self._stop.clear()
            self._start = time.perf_counter()

            sampler = threading.Thread(target=self._sampler, daemon=True)
            sampler.start()

            spawn_step = self.ramp_up_s / self.users if self.ramp_up_s and self.users else 0.0
            with ThreadPoolExecutor(max_workers=self.users) as pool:
                for i in range(self.users):
                    pool.submit(self._worker, i, i * spawn_step)
                time.sleep(self.duration_s)
                self._stop.set()

            elapsed = time.perf_counter() - self._start
            self._stop.set()
            sampler.join(timeout=1.0)
        finally:
            self._stop.set()

        samples = self._samples
        if self.warmup_drop_s > 0:
            # 丢掉热身窗口：TCP 冷启动、SQLite 页缓存、JIT 式的首次导入
            # 都会把 P99 抬高一大截。真实系统里用户感受到的也不是第一秒。
            cutoff = self.warmup_drop_s
            kept = [s for s in samples if s.at >= cutoff]
            if kept:
                samples = kept
                elapsed = max(0.001, elapsed - cutoff)

        stats = summarize(samples, label=f"{self.users}u", users=self.users, duration_s=elapsed)
        with self._lock:
            measured = self._inflight_samples[1:] or [0]
        stats.max_inflight = max(measured)  # type: ignore[attr-defined]
        stats.mean_inflight = round(sum(measured) / len(measured), 2)  # type: ignore[attr-defined]
        stats.configured_users = self.users  # type: ignore[attr-defined]
        return stats


def run_api_call(
    session: requests.Session,
    base_url: str,
    method: str,
    path: str,
    expect_status: int | tuple[int, ...] = 200,
    timeout_s: float = 10.0,
    scenario: str = "",
    **kwargs,
) -> Sample:
    """发一次请求并产出样本。所有压测场景都用它，保证口径一致。"""
    url = f"{base_url.rstrip('/')}{path}"
    expect = (expect_status,) if isinstance(expect_status, int) else expect_status
    t0 = time.perf_counter()
    try:
        resp = session.request(method, url, timeout=timeout_s, **kwargs)
        elapsed = (time.perf_counter() - t0) * 1000.0
        ok = resp.status_code in expect
        err = "" if ok else f"HTTP {resp.status_code}"
        return Sample(
            scenario=scenario or f"{method} {path}",
            status=resp.status_code,
            elapsed_ms=elapsed,
            ok=ok,
            error=err,
        )
    except Exception as exc:  # noqa: BLE001
        elapsed = (time.perf_counter() - t0) * 1000.0
        return Sample(
            scenario=scenario or f"{method} {path}",
            status=0,
            elapsed_ms=elapsed,
            ok=False,
            error=f"{type(exc).__name__}",
        )
