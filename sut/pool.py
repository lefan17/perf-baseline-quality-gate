"""固定大小的数据库连接池：让「池耗尽」变成一个可以量出来的现象。

## 为什么这个仓需要它

JD 里「Redis / Kafka / MySQL / 连接池」这几样经常被归成一类要求：
**你知不知道资源是有限的**。缓存那一层已经做了「有 Redis 和没 Redis」的对照，
这里补的是另一半：**连接本身是稀缺资源**。

## 为什么自己写，而不是引 SQLAlchemy / 第三方池

三条理由，按重要性排序：

1. **这个仓的依赖清单是逐字校验的**（`scripts/check_deps_sync.py` 比对
   `requirements.txt` 与实际 import）。为了演示一个池去引一个 ORM，
   等于为了一个概念吞掉整个依赖树 —— 而面试要问的恰恰是"池里到底发生了什么"。
2. **池的语义只有几十行**：限制并发、复用、超时、归还。写出来比调库更能
   回答追问（"拿不到连接时是阻塞还是快速失败？""超时计数器放在哪？"）。
3. **被测服务的定位是"结构清楚、耗时归因干净"**。第三方池会引入自己的
   超时重试与后台回收线程，那些成本会混进压测数字里。

## 池耗尽是什么现象（这是要能被量出来的）

池大小 `N`，每个请求借一条连接、用完归还。当**同时**有超过 `N` 个请求
卡在慢查询上时，第 `N+1` 个请求就借不到连接：

- 它会在 `acquire_timeout_s` 内排队等待（这段时间用户感知为"慢"）；
- 等不到就抛 `PoolExhaustedError`，接口返回 **503 + Retry-After**。

所以「池耗尽」在监控上同时表现为三件事：**503 率上升、排队时长上升、
池饱和度（in_use/size）贴住 1.0**。只报 503 率是不够的 —— 那和"代码报错"
长得一模一样，要能区分「资源不够」和「逻辑错」。

## 一个反直觉但重要的点

池**不是越大越好**。池越大，能同时打到数据库的连接越多，数据库侧的
锁竞争/CPU 争抢越严重；池大小通常要贴着数据库的并发上限设。
这个仓的探针只负责把「池小了会 503、池大了看不出问题」这个现象摆出来，
不去给一个"最佳值" —— 那需要真实数据库的容量测试，单机 SQLite 给不了。
"""

from __future__ import annotations

import queue
import threading
import time
from contextlib import contextmanager
from typing import Callable

#: 借不到连接时最多等多久（秒）。默认 0.5 秒：
#: 比多数上游的超时（1~3 秒）短一个量级，让"池满"先于"上游超时"暴露出来。
#: 这两个现象在监控上很容易混：池满时服务还活着、只是排队；上游超时会直接掐断。
DEFAULT_ACQUIRE_TIMEOUT_S = 0.5


class PoolExhaustedError(RuntimeError):
    """池满且等不到连接。

    刻意与"连接本身坏了"分开：这个错误表示**资源不够**（可以靠扩容/降并发/
    优化慢查询解决），而连接坏掉表示**资源不可用**（要修网络或重建连接）。
    两者的告警处理完全不同，混成一个异常名字等于逼值班的人去读堆栈。
    """


class ConnectionPool:
    """固定容量的连接池。线程安全。

    容量用 `BoundedSemaphore` 表达（而不是只靠队列长度），因为信号量能
    精确限制"同时在借的连接数"；队列只负责存放空闲连接，不承担限流职责。
    借出计数单独维护，是因为 `threading.Semaphore` 没有公开的当前值读数，
    依赖私有属性 `_value` 跨版本不保证 —— 这属于"能跑但别依赖"的那类实现细节。
    """

    def __init__(
        self,
        factory: Callable[[], object],
        size: int,
        acquire_timeout_s: float = DEFAULT_ACQUIRE_TIMEOUT_S,
    ) -> None:
        if size < 1:
            raise ValueError("池大小至少为 1")
        self._factory = factory
        self._size = size
        self._acquire_timeout_s = acquire_timeout_s
        self._free: queue.LifoQueue = queue.LifoQueue()
        self._sem = threading.BoundedSemaphore(size)
        self._lock = threading.Lock()
        self._acquired = 0
        self._waiting = 0
        # 计数器集中放在锁里：这些数字要拿来打监控，
        # 并发写加减导致漂移时很难复现，所以宁可多一把锁换确定性。
        self._stats: dict[str, float] = {
            "acquired_total": 0,
            "created_total": 0,
            "reused_total": 0,
            "timeouts_total": 0,
            "factory_errors_total": 0,
            "wait_ms_total": 0.0,
        }

    # ------------------------------------------------------------------ #
    # 借用 / 归还
    # ------------------------------------------------------------------ #

    @contextmanager
    def acquire(self):
        """借一条连接；拿不到就抛 `PoolExhaustedError`（不返回 None）。

        返回 None 会让每个调用点都得写一次判空，漏写就是 AttributeError
        直接 500 —— 而"池满"是一个可预期的运行状态，不该以 500 的面目出现。
        """
        conn = self.checkout()
        try:
            yield conn
        except BaseException:
            # 连接上可能还有未提交的事务；归还前回滚，
            # 否则下一个人借到的是一条带着脏事务的连接 —— 这类 bug 极难查。
            self._rollback_quietly(conn)
            raise
        finally:
            self.checkin(conn)

    # ------------------------------------------------------------------ #
    # 手动借用（给"拿不到就归还"包装器用）
    # ------------------------------------------------------------------ #

    def checkout(self) -> object:
        """借出一条连接，由调用方负责 `checkin()`。

        `acquire()` 是给 `with` 用的正常路径；`checkout()` / `checkin()` 这对
        是给**包装器**用的：`sut.db.connect()` 要在池开启时返回一个假连接对象，
        它的 `close()` 语义是"归还"。如果不提供这对方法，包装器就只能自己
        重写一遍限流与计数，两份实现迟早漂移。
        """
        start = time.perf_counter()
        with self._lock:
            self._waiting += 1
        try:
            got = self._sem.acquire(timeout=self._acquire_timeout_s)
        finally:
            with self._lock:
                self._waiting -= 1

        waited_ms = (time.perf_counter() - start) * 1000

        if not got:
            with self._lock:
                self._stats["timeouts_total"] += 1
            raise PoolExhaustedError(f"连接池已满（size={self._size}），等待 {waited_ms:.0f}ms 后放弃")

        try:
            conn = self._free.get_nowait()
            reused = True
        except queue.Empty:
            # `_factory()` 会抛异常：数据库暂时连不上、账号错、DNS 挂了。
            # 这条路径以前**不还名额**，于是每失败一次就永久少一个许可，
            # 失败 `size` 次之后这个池就再也借不出东西了 —— 而它报的错是
            # 「连接池已满，等待 500ms 后放弃」，把人引向「扩容」而不是
            # 「上游不可用」。实测：size=2、工厂连抛两次后，第三次 checkout()
            # 直接 PoolExhaustedError，而 `snapshot()['in_use']` 还是 0
            # （一个"满了却没人占着"的池）。
            #
            # 所以这里必须先在 finally 里把名额还回去，再让原始异常往外走：
            # 上层要看到的是 OSError / MySQLUnavailableError（503，等它恢复），
            # 不是 PoolExhaustedError（503，扩容）。两种 503 的原因完全不同，
            # 混成一个之后监控上永远分不出来。
            try:
                conn = self._factory()
            except BaseException:
                with self._lock:
                    self._stats["factory_errors_total"] += 1
                self._sem.release()
                raise
            reused = False

        with self._lock:
            self._acquired += 1
            self._stats["acquired_total"] += 1
            self._stats["wait_ms_total"] += waited_ms
            if reused:
                self._stats["reused_total"] += 1
            else:
                self._stats["created_total"] += 1
        return conn

    def checkin(self, conn: object) -> None:
        """归还一条连接（幂等：重复归还只生效一次）。"""
        with self._lock:
            if self._acquired <= 0:
                # 重复归还说明包装器写错了；这里静默忽略而不是把计数打成负数，
                # 否则后面所有监控数字都会错，且找不到是谁弄坏的。
                return
            self._acquired -= 1
        self._free.put(conn)
        self._sem.release()

    def discard(self, conn: object) -> None:
        """丢弃一条**坏**连接：关掉它，只释放名额，不放回空闲队列。

        与 `checkin()` 的区别就是「放回队列」这一步。为什么必须分开：

        MySQL 连接会因为 `wait_timeout`、服务端重启、网络闪断而失效。
        如果把失效连接 `checkin()` 回队列，下一个人借到的就是它 —— 报错、
        再还回去、让再下一个人也踩。**一条坏连接可以在池里活很久**，
        表现出来是「偶发 500」，而且重跑就消失，是最难复现的那类 bug。

        SQLite 时代不需要这个方法：库就是一个文件，连接不会"半死"。
        换成真数据库之后，**「连接会坏」变成必须显式处理的状态**。
        """
        close = getattr(conn, "close", None)
        if close is not None:
            try:
                close()
            except Exception:
                # 关闭失败也不能掩盖原始异常：调用方要看到的是"这条连接不可用"。
                pass
        with self._lock:
            if self._acquired <= 0:
                return
            self._acquired -= 1
        self._sem.release()

    @staticmethod
    def _rollback_quietly(conn: object | None) -> None:
        if conn is None:
            return
        rollback = getattr(conn, "rollback", None)
        if rollback is None:
            return
        try:
            rollback()
        except Exception:
            # 回滚失败不能掩盖原始异常：调用方要看到的是"查询失败"，
            # 而不是被回滚错误顶替掉。
            pass

    # ------------------------------------------------------------------ #
    # 观测
    # ------------------------------------------------------------------ #

    @property
    def size(self) -> int:
        return self._size

    def snapshot(self) -> dict:
        """给 `/api/_diagnostics/pool` 用的快照。"""
        with self._lock:
            stats = dict(self._stats)
            in_use = self._acquired
            waiting = self._waiting

        return {
            "size": self._size,
            "in_use": in_use,
            # 饱和度贴住 1.0 就是"池已经被打满"，这是判断池耗尽的直接依据。
            "saturation": round(in_use / self._size, 4),
            "waiting": waiting,
            "acquire_timeout_s": self._acquire_timeout_s,
            "acquired_total": int(stats["acquired_total"]),
            "created_total": int(stats["created_total"]),
            "reused_total": int(stats["reused_total"]),
            "timeouts_total": int(stats["timeouts_total"]),
            # 工厂连不上导致的失败次数（**不计入** timeouts_total）。
            # 这两个数字必须分开：timeouts 说明并发给多了，factory_errors
            # 说明上游本身不可用。合成一个指标之后，事故复盘时没法回答
            # 「当时是该扩容还是该等数据库回来」。
            "factory_errors_total": int(stats["factory_errors_total"]),
            "avg_wait_ms": round(stats["wait_ms_total"] / stats["acquired_total"], 2)
            if stats["acquired_total"]
            else 0.0,
        }
