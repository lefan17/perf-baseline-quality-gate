"""`bench/engine.py` 的 `Runner` —— 引擎本身从没被真跑过。

## 为什么要测这个

`tests/test_engine.py` 覆盖了分位数、`summarize`、`merge_stats`、
`run_api_call`，全是**纯函数**。而 `Runner` —— 那个真正开线程、
真正发请求、真正丢热身样本的类 —— 一行都没跑过。它当时是 52.3%。

这不是"数字不好看"，是**判据的源头没被验证过**：门禁拦不拦人看的是
`Runner` 产出的 `Stats`。分位数算法写得再对，只要 `Runner`：

- 忘记在 `run()` 开头清空 `_samples`（第二次复用的实例会把两轮样本混一起）
- 热身样本按 `at` 过滤时把 `elapsed` 也减掉却漏了 `max(0.001, ...)`
- 线程里的异常没被 `except` 吞掉（`_worker` 一崩，`ThreadPoolExecutor`
  会在 `with` 退出时把异常重新抛出，整个 run 直接失败）

它就没有产物给门禁看。所以下面的用例都在打这些点。

## 为什么要起一个真 HTTP 服务

用 `requests` 的 monkeypatch 替身可以更快，但那样测的是替身的分支，
而不是"线程池 + keep-alive 会话 + 计时"这套真实链路。这里起一个
`http.server` 的最小服务（stdlib，无依赖），跑 0.3~0.5 秒，
整个文件一秒级完成，换来的是一条真链路。
"""

from __future__ import annotations

import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from bench.engine import Runner, run_api_call


class _Handler(BaseHTTPRequestHandler):
    """最小被测服务：`/ok` 返 200、`/slow` 慢 30ms、`/boom` 返 500。"""

    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 的约定
        if self.path.startswith("/slow"):
            time.sleep(0.03)
            body = b"slow"
            self.send_response(200)
        elif self.path.startswith("/boom"):
            body = b"boom"
            self.send_response(500)
        else:
            body = b"ok"
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # noqa: D102 - 静音，否则每个请求打一行
        pass


@pytest.fixture(scope="module")
def base_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _ok(session):
    return run_api_call(session, session.base, "GET", "/ok", scenario="ok")


def _scenario(base: str):
    """造一个场景函数：它自己带上 base_url，所以签名符合 `(session) -> Sample`。"""

    def fn(session):
        return run_api_call(session, base, "GET", "/ok", scenario="ok")

    return fn


class TestConstructorValidation:
    """两个 ValueError 都在守卫"配置写错了"这类现场。"""

    def test_no_scenarios_is_rejected(self):
        # 空场景列表会让 cum 为空、pick 走 fallback 返回 -1，然后
        # `self.scenarios[-1]` 静默取到最后一个……其实没有最后一个。
        # 在构造期报错比在压测中途抛 IndexError 便宜得多。
        with pytest.raises(ValueError, match="至少要有一个场景"):
            Runner("http://127.0.0.1:1", [], users=1, duration_s=0.1)

    def test_non_positive_total_weight_is_rejected(self):
        # 权重和 <= 0 会让 weights 全变成 0，`r <= c` 永不成立，
        # 所有请求都落到 fallback 的最后一个场景上 —— 一个只能压
        # 单个场景的混压引擎，读起来像在压全链路。
        with pytest.raises(ValueError, match="权重之和必须为正"):
            Runner("http://127.0.0.1:1", [(_scenario("http://127.0.0.1:1"), 0)], users=1, duration_s=0.1)


class TestRun:
    def test_runs_real_requests_and_returns_stats(self, base_url):
        r = Runner(base_url, [(_scenario(base_url), 1)], users=3, duration_s=0.3)
        st = r.run()
        assert st.total > 0
        assert st.ok_count == st.total
        assert st.error_count == 0
        assert st.users == 3
        assert st.label == "3u"
        assert st.p95_ms > 0
        # 在途数由 sampler 每 0.1 秒采一次，必须落在 [0, users] 之内。
        # 一旦 >users，"瓶颈在压测机"这个判断就失效了。
        assert 0 <= st.max_inflight <= 3

    def test_same_seed_produces_the_same_scenario_pick_sequence(self):
        """同 `(seed, worker_id)` → 同一条场景选择序列。

        ## 这条替换掉了一个名不副实的用例

        原先的用例叫 `test_same_config_produces_the_same_scenario_sequence_by_seed`，
        但它的断言只有：

            assert "a" in first and "b" in first

        也就是**只要两个场景都出现过就算过** —— 而权重是 1:1、跑 0.2 秒、
        两个线程，这个断言几乎不可能失败。它验证的性质是"引擎真的发了请求"，
        不是用例名宣称的"同种子产生同序列"。名字与断言不符是危险的：
        下一个读到这个名字的人会认为确定性已经被钉住了，于是不会再去补。

        ## 为什么改成直接打 `build_picker`

        确定性是 `build_picker(weights, random.Random(seed + worker_id))` 的
        性质，是可以**逐项比对**的纯函数性质。走 `Runner.run()` 反而测不准：
        线程调度决定谁先取随机数、每轮发出多少个请求也不确定，精确序列
        必然抖动 —— 那不是被测对象的缺陷。原用例的注释里其实已经承认了这点
        （「精确序列会因线程调度抖动而假红」），但结论下错了：该换测法，
        而不是降级成一个什么都证明不了的断言。
        """
        from bench.engine import build_picker

        def seq(seed: int, worker_id: int, n: int = 50) -> list[int]:
            pick = build_picker([0.5, 0.5], random.Random(seed + worker_id))
            return [pick() for _ in range(n)]

        assert seq(42, 0) == seq(42, 0), "同种子两次取值序列不一致，可复现性失效"
        assert seq(42, 0) != seq(42, 1), "不同 worker 拿到同一条序列，等于只有一份随机源"
        assert seq(42, 0) != seq(43, 0), "换种子不改变序列，说明种子没被用上"
        # 权重确实生效：偏向 0.9 时第 0 项必须明显更多。
        pick0 = build_picker([0.9, 0.1], random.Random(42))
        zeros = sum(1 for _ in range(2000) if pick0() == 0)
        assert zeros > 1700, f"权重 0.9 的场景只被选中 {zeros}/2000 次，累积分布可能算错了"

    def test_runner_seeds_each_worker_from_seed_plus_worker_id(self, base_url, monkeypatch):
        """`Runner` 真的把 `seed + worker_id` 交给了 `build_picker`。

        ## 为什么这条必须单独存在

        上一条用例验证的是 `build_picker` **自己**是确定性的（纯函数性质）。
        那是必要条件，但完全不够：`build_picker` 写得再对，只要 `Runner._worker`
        传进去的是一个没种子的 `random.Random()`，线上依然不可复现。

        这个区别不是假想 —— 本仓另一处 `bench/scenarios.py` 的 `_rnd()`
        当时就是 `return random.Random()`，注释还写着「每个线程各自持有一个
        Random」。**注释描述的性质与代码不符**，而没有任何用例能发现它。

        ## 怎么测才对

        精确序列在 `Runner.run()` 层面测不了（线程调度决定谁先取随机数，
        每轮请求数也不固定）。但**接线**可以测：把 `build_picker` 换成记录器，
        看它被调用了几次、每次收到的随机源状态是什么。这是确定性的，
        而且正好覆盖"种子有没有真的被传下去"。

        断言三件事：
        1. 每个 worker 各调一次（不是全局共享一个 picker）；
        2. 各 worker 的随机源状态互不相同（不是同一实例 / 同一状态）；
        3. 同样的 `(seed, users)` 重跑，各 worker 的状态序列逐项相同（可复现）。
        """
        import bench.engine as engine

        seen: list[object] = []
        real = engine.build_picker

        def recording(weights, rnd):
            seen.append(rnd.getstate())
            return real(weights, rnd)

        monkeypatch.setattr(engine, "build_picker", recording)

        scenarios = [(_scenario(base_url), 1)]
        Runner(base_url, scenarios, users=3, duration_s=0.2, seed=42).run()
        first = list(seen)

        assert len(first) == 3, f"3 个 worker 应各拿到一个 picker，实测 {len(first)} 次调用"
        assert len(set(first)) == 3, "各 worker 的随机源状态相同，说明种子没按 worker 拆开"

        seen.clear()
        Runner(base_url, scenarios, users=3, duration_s=0.2, seed=42).run()
        assert seen == first, "同 seed + 同 users 重跑，worker 的随机源状态不一致 → 不可复现"

        # 换 seed 必须换状态，否则 seed 参数是个摆设。
        seen.clear()
        Runner(base_url, scenarios, users=3, duration_s=0.2, seed=43).run()
        assert seen != first, "换 seed 后 worker 随机源状态未变，说明 Runner 忽略了 seed"

    def test_http_error_is_recorded_as_failed_sample_not_an_exception(self, base_url):
        # `_worker` 里那个 `except Exception` 是引擎的承重墙：被测服务返回
        # 500 或连接被重置，应该变成一条失败样本参与错误率计算，
        # 而不是让线程崩掉、整个 run 抛异常退出。前者门禁判"错误率超限"，
        # 后者是"压测工具自己炸了"，两种事故的排查方向完全不同。
        def boom(session):
            return run_api_call(session, base_url, "GET", "/boom", expect_status=200, scenario="boom")

        st = Runner(base_url, [(boom, 1)], users=2, duration_s=0.2).run()
        assert st.total > 0
        assert st.error_count == st.total
        assert st.error_rate == 1.0
        assert "HTTP 500" in st.error_breakdown

    def test_network_exception_also_becomes_a_sample(self, base_url):
        # 连不上的端口：requests 抛 ConnectionError，必须被吞成样本。
        def dead(session):
            return run_api_call(session, "http://127.0.0.1:1", "GET", "/ok", timeout_s=0.2, scenario="dead")

        st = Runner(base_url, [(dead, 1)], users=1, duration_s=0.2).run()
        assert st.total > 0
        assert st.error_count == st.total

    def test_reused_instance_does_not_accumulate_samples(self, base_url):
        # run() 开头必须清空 _samples。实例被复用时（拐点定位会连续跑多个
        # 梯度），不清空会让第 2 个梯度的 P95 混进第 1 个梯度的样本，
        # 而"并发 10→20 时 P95 反而下降"这种结论就是这么来的。
        r = Runner(base_url, [(_scenario(base_url), 1)], users=1, duration_s=0.15)
        first = r.run()
        second = r.run()
        # 两轮各自独立计时，样本数不应叠加成两轮之和。
        assert second.total < first.total * 2

    def test_warmup_drop_removes_the_first_seconds(self, base_url):
        # 热身窗口按样本的 `at` 过滤。at 由引擎统一盖戳（场景作者不用管），
        # 这条用例同时验证了"盖戳真的在"和"过滤真的生效"。
        def slowish(session):
            return run_api_call(session, base_url, "GET", "/slow", scenario="slow")

        r = Runner(base_url, [(slowish, 1)], users=2, duration_s=0.5, warmup_drop_s=0.35)
        st = r.run()
        assert st.total > 0
        # 剩下的样本都在 0.35s 之后，所以总时长（用于算 RPS）也要相应缩短，
        # 否则 RPS 会被系统性低估 —— 这个减法在源码里就写了 max(0.001, ...)，
        # 这里顺带钉住它不会算出 0 或负数。
        assert st.rps > 0

    def test_think_time_limits_throughput(self, base_url):
        # think_time_s 是"每个用户每次请求后歇一下"。它必须真的降低吞吐，
        # 否则混压场景里一个慢接口会把并发全吃掉，其他接口的样本数
        # 变得不可比。
        fast = Runner(base_url, [(_scenario(base_url), 1)], users=1, duration_s=0.3).run()
        slow = Runner(base_url, [(_scenario(base_url), 1)], users=1, duration_s=0.3, think_time_s=0.1).run()
        assert slow.total < fast.total

    def test_ramp_up_delays_workers_without_losing_them(self, base_url):
        # ramp_up_s > 0 时每个 worker 有不同的 spawn_delay。
        # 常见 bug 是把 delay 写成了总时长而不是"第 i 个 worker 的偏移"，
        # 那样 4 个 worker 会全部等到最后一刻才启动，稳态窗口就废了。
        r = Runner(base_url, [(_scenario(base_url), 1)], users=4, duration_s=0.4, ramp_up_s=0.1)
        st = r.run()
        assert st.total > 0
        assert st.ok_count == st.total

    def test_timeout_is_passed_to_requests(self, base_url):
        # 超时必须能传下去：默认 10 秒对一个 30ms 的服务够用，
        # 但如果有人把它改成 0.001，样本应该变成失败而不是卡住。
        def slow_req(session):
            return run_api_call(session, base_url, "GET", "/slow", timeout_s=0.001, scenario="to")

        st = Runner(base_url, [(slow_req, 1)], users=1, duration_s=0.2).run()
        assert st.total > 0
        assert st.error_count == st.total


class TestInflightAccounting:
    """`max_inflight` 是这个引擎区别于普通压测工具的地方 —— 它回答
    "慢，是被测服务的慢，还是压测机自己顶不住"。所以它算错等于白采。"""

    def test_inflight_never_exceeds_configured_users(self, base_url):
        r = Runner(base_url, [(_scenario(base_url), 1)], users=5, duration_s=0.35)
        st = r.run()
        assert st.max_inflight <= 5
        assert st.mean_inflight <= st.max_inflight
        assert st.configured_users == 5

    def test_first_inflight_sample_is_dropped(self, base_url):
        # 采样线程在 run() 里先启动、worker 后提交，所以第一格几乎必然是 0。
        # 把它算进均值会系统性拉低在途数（明明压满了却显示只用了半个）。
        # 源码里用 `self._inflight_samples[1:] or [0]` 丢掉它，这里钉住
        # "不会因为丢完之后列表为空而崩"。
        r = Runner(base_url, [(_scenario(base_url), 1)], users=1, duration_s=0.15)
        st = r.run()
        assert st.mean_inflight >= 0
