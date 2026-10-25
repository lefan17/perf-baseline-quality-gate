"""分布式压测：分片、协议、合并口径，以及"分位不能合并"这条结论的对照组。

分两半测，理由和连接池那批一样：

- **纯函数**（分片、样本还原、合并、naive 对照）随时可测，不需要网络；
- **真的起两个 worker 跑一轮**必须能起本地 SUT，慢，但它测的是这个模块存在的理由
  ——"时钟对齐 + 一个窗口"这两件事只有跑起来才能验证。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from bench import distributed as dist
from sut.server_ctl import start_sut as _start_sut


# --------------------------------------------------------------------------- #
# 分片
# --------------------------------------------------------------------------- #
def test_split_users_is_exact():
    """余数从前往后各加一个，总数必须守恒。"""
    assert dist.split_users(20, 3) == [7, 7, 6]
    assert sum(dist.split_users(20, 3)) == 20
    assert dist.split_users(24, 3) == [8, 8, 8]


def test_split_users_never_hands_a_worker_zero():
    """总并发小于 worker 数时必须直接报错。

    放过去的后果是某个 worker 分到 0 个用户、立刻返回空样本，
    而合并后的 RPS = 总样本 / 窗口 照样算得出来 —— 报告会写着"12 并发"，
    实际只跑了 2 个。压测里最怕的就是报告一个没跑满的并发还当成配置值。
    """
    with pytest.raises(ValueError):
        dist.split_users(2, 3)


# --------------------------------------------------------------------------- #
# 合并口径：分位不能合并
# --------------------------------------------------------------------------- #
def _payload(elapsed: list[float], status: list[int] | None = None) -> dict:
    n = len(elapsed)
    return {
        "ok": True,
        "elapsed_ms": elapsed,
        "status": status or [200] * n,
        "ok_flags": [True] * n,
        "at": [0.5] * n,
    }


def test_naive_tail_merge_understates_the_true_p95():
    """这是整个模块最重要的一条断言。

    一个 worker 有 1000 个 10ms 的样本，另一个有 100 个 900ms 的样本：

        各自 p95      = 10ms / 900ms
        加权平均      ≈ 91ms        ← naive，错得最厉害
        池化 1100 个  = 900ms       ← 真相

    错因：分位数是**顺序统计量**，不是可加量。10% 的请求慢了 90 倍，
    按样本数加权平均只把 p95 抬了 9 倍 —— 尾延迟恰恰是被平均数抹掉的东西。

    （另一种常见错法是"取各 worker p95 的最大值"，方向保守但同样错：
    它回答的是"最慢那个 worker 的分位"，不是"整轮请求的分位"。）
    """
    fast = _payload([10.0] * 1000)
    slow = _payload([900.0] * 100)

    naive = dist.naive_tail_p95([fast, slow])
    pooled = dist.merge_workers([fast, slow], total_users=20, window_s=10.0)

    assert naive == pytest.approx(90.909, abs=0.01)
    assert pooled.p95_ms == 900.0
    assert pooled.p95_ms > naive * 5, "池化后的 p95 必须远高于加权平均——低了说明合并实现错了"


def test_merge_uses_one_window_not_the_sum_of_workers():
    """三个 worker 各跑 10 秒是"并发跑了 10 秒"，不是 30 秒。

    如果把各 worker 的耗时相加当窗口，RPS 会被除以 3 —— 这是分布式压测里
    第二常见的错账方式（第一种是合并分位）。
    """
    payloads = [_payload([10.0] * 50) for _ in range(3)]
    stats = dist.merge_workers(payloads, total_users=30, window_s=10.0)

    assert stats.total == 150
    assert stats.rps == 15.0  # 150 / 10s，而不是 150 / 30s
    assert stats.users == 30


def test_warmup_is_dropped_from_the_window():
    """热身窗口要从分母里扣掉，否则 RPS 会被系统性低估。"""
    stats = dist.merge_workers([_payload([10.0] * 100)], total_users=10, window_s=10.0, warmup_drop_s=2.0)
    assert stats.rps == 12.5  # 100 / 8s


def test_sample_arrays_must_agree_in_length():
    """协议不一致时必须拒绝合并，而不是猜一个长度。

    "少传了一个 ok_flags"这种错误如果被补齐成 True/False，
    错误率就会被静默改写 —— 而错误率正是门禁 R1 的唯一输入。
    """
    broken = {"ok": True, "elapsed_ms": [1.0, 2.0], "status": [200, 200], "ok_flags": [True], "at": [0.0, 0.1]}
    with pytest.raises(ValueError):
        dist.samples_from_payload(broken)


def test_failed_sample_reads_status_not_command_flag():
    """回复顶层的 `ok` 是"命令成功了吗"，样本的成功与否在 `ok_flags` 里。

    两个含义共用一个键名，是这类协议最好犯的错：一次失败的 run 会被读成
    "每条样本都成功"，错误率恒为 0。
    """
    payload = {
        "ok": True,  # 命令执行成功
        "elapsed_ms": [12.0, 12.0],
        "status": [200, 500],
        "ok_flags": [True, False],
        "at": [0.1, 0.2],
    }
    samples = dist.samples_from_payload(payload)
    assert [s.ok for s in samples] == [True, False]
    assert samples[1].error == "HTTP 500"


def test_protocol_roundtrip_keeps_chinese_and_numbers():
    raw = dist.encode_message({"cmd": "run", "scenario": "库存报表", "users": 12, "ratio": 0.95})
    assert raw.endswith(b"\n")
    assert dist.decode_message(raw) == {"cmd": "run", "scenario": "库存报表", "users": 12, "ratio": 0.95}


def test_protocol_rejects_non_object_payload():
    with pytest.raises(ValueError):
        dist.decode_message(b"[1, 2, 3]\n")


def test_endpoint_must_carry_a_port():
    with pytest.raises(ValueError):
        dist.call("worker-without-port", {"cmd": "hello"})


# --------------------------------------------------------------------------- #
# 真起两个 worker：时钟对齐 + 一个窗口
# --------------------------------------------------------------------------- #
def _serve_in_thread(tmp_path: Path, name: str) -> tuple[str, object]:
    """在后台线程起一个 worker，用它写出的 ready 文件拿端口。"""
    import threading

    ready = tmp_path / f"{name}.json"
    worker = threading.Thread(
        target=dist.serve, kwargs={"host": "127.0.0.1", "port": 0, "ready_file": ready}, daemon=True
    )
    worker.start()
    deadline = time.time() + 10.0
    while time.time() < deadline:
        if ready.exists() and ready.read_text(encoding="utf-8").strip():
            info = json.loads(ready.read_text(encoding="utf-8"))
            return f"{info['host']}:{info['port']}", worker
        time.sleep(0.05)
    raise AssertionError("worker 没有在 10 秒内就绪")


@pytest.fixture()
def sut(tmp_path):
    handle = _start_sut(report_mode="agg", items=800)
    yield handle
    handle.stop()


@pytest.mark.sut
def test_two_workers_start_on_the_same_epoch_and_share_one_window(tmp_path, sut):
    """两个 worker 真的在同一时刻开跑，且合并后只有一个窗口。

    - 每个 worker 都必须有样本（否则"2 个 worker"只是名义上的并发）；
    - 两个 worker 的首个样本时刻差要远小于整轮时长（这里是 200ms 对 2s）；
    - 合并窗口必须接近配置时长，而不是两个 worker 时长之和。
    """
    a, _ = _serve_in_thread(tmp_path, "a")
    b, _ = _serve_in_thread(tmp_path, "b")
    coordinator = dist.Coordinator([a, b])
    infos = coordinator.hello()
    assert len(infos) == 2
    for info in infos:
        assert abs(info.skew_ms) < 1000.0, f"本机 worker 的时钟偏移不该到秒级：{info.skew_ms}ms"

    duration_s = 2.0
    result = coordinator.run(
        base_url=sut.base_url,
        users=4,
        duration_s=duration_s,
        scenarios=["items_list"],
        timeout_s=5.0,
        warmup_drop_s=0.0,
    )

    assert len(result.payloads) == 2
    for i, payload in enumerate(result.payloads):
        assert payload["stats"]["total"] > 0, f"worker#{i} 一条样本都没有"
        assert payload["users"] == 2

    first_at = [min(p["at"]) for p in result.payloads]
    assert abs(first_at[0] - first_at[1]) < 0.2, (
        f"两个 worker 的起跑时间差了 {abs(first_at[0] - first_at[1]) * 1000:.0f}ms"
    )

    assert result.stats.total == sum(p["stats"]["total"] for p in result.payloads)
    assert result.window_s < duration_s * 1.6, "窗口明显超过配置时长，说明把各 worker 的耗时当成了串行累加"
    assert result.stats.users == 4


@pytest.mark.sut
def test_unreachable_worker_fails_loudly(tmp_path):
    """一个 worker 连不上时，整轮必须失败。

    危险做法是"把它的份额分给剩下的 worker 继续跑"：报告还会出来，
    每个数字都像模像样，但并发已经不是配置值了 —— 这比直接报错糟得多。
    """
    a, _ = _serve_in_thread(tmp_path, "a")
    coordinator = dist.Coordinator([a, "127.0.0.1:1"])
    with pytest.raises((OSError, RuntimeError)):
        coordinator.hello()
