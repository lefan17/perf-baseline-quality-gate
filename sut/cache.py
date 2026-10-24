"""物料详情缓存层：Redis 优先，连不上就**显式降级**到数据库。

## 这个模块要证明的不是"加了缓存变快了"

性能工程里最廉价的结论是「加缓存之后 P95 从 41ms 变成 6ms」。
真正会出事的是另外三件事，这个模块针对性地各留了一个观察点：

1. **缓存穿透**：请求一个**不存在**的 id。朴素实现每次都会穿透到数据库，
   而这恰恰是攻击者最喜欢打的点。这里的做法是缓存「负结果」
   （用 `__MISS__` 哨兵，而不是 JSON 的 `null`——后者没法与「取到了空值」区分）。
2. **缓存一致性**：改库存的写路径必须让对应的缓存失效，否则脏读会被压测
   平滑掉——错误率 0%、P95 很好看，数据却是旧的。`invalidate()` 由写路径显式调用。
3. **降级路径**：Redis 挂了 / 超时 / 连不上时，接口**必须**还能返回 200
   （只是慢）。降级不静默：`CacheStats.errors` 会一路透到 `/api/_diagnostics/cache`，
   让「这一轮到底有没有命中缓存」变成可查的事实，而不是靠报告里的
   `cache=on` 字样自我暗示。

## 为什么统计放在进程内而不是只读 Redis 的 `INFO`

`INFO` 里的 `keyspace_hits/misses` 是**实例级**的：同一个 Redis 上跑两轮压测，
第二轮的命中率会被第一轮的残留污染。进程内计数与「这一轮」严格一一对应，
所以报告里的命中率是可复核的（这一点和 `docs/度量口径.md` 里
「口径必须能在一条命令里复现」是同一条原则）。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from typing import Any

from sut.redis_client import Redis, RedisError, RedisUnavailableError

#: 负结果的哨兵值。用固定字节串而不是 JSON `null`：
#: `GET` 返回 `None` 表示"键不存在"（未缓存），而空的 JSON 对象是合法数据，
#: 两者混用会让「缓存了空结果」与「没缓存」在代码里长得一模一样。
MISS_SENTINEL = b"__PERF_MISS__"

#: 默认 TTL。5 分钟足够让压测在缓存温起来之后观察到收益，
#: 又不至于让「改完数据还在读旧值」这种问题被掩盖。
DEFAULT_TTL_S = 300


class CacheStats:
    """进程内的命中统计。所有计数都在一把锁里，读写都很短。"""

    __slots__ = ("_lock", "bypass", "errors", "hits", "misses")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.errors = 0
        self.bypass = 0

    def record_hit(self) -> None:
        with self._lock:
            self.hits += 1

    def record_miss(self) -> None:
        with self._lock:
            self.misses += 1

    def record_error(self) -> None:
        with self._lock:
            self.errors += 1

    def record_bypass(self) -> None:
        with self._lock:
            self.bypass += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            hits, misses, errors, bypass = self.hits, self.misses, self.errors, self.bypass
        lookups = hits + misses
        return {
            "hits": hits,
            "misses": misses,
            "errors": errors,
            "bypass": bypass,
            "lookups": lookups,
            # 分母只算真正查过 Redis 的次数（hits + misses），
            # 不把 bypass（cache=off 的直查）和 errors（降级）混进去：
            # 把降级算成"未命中"会让命中率看起来比实际更差，
            # 而把它算成"命中"则是彻头彻尾的撒谎。
            "hit_rate": (hits / lookups) if lookups else None,
        }

    def reset(self) -> None:
        with self._lock:
            self.hits = self.misses = self.errors = self.bypass = 0


class ItemCache:
    """`item:detail:{id}` 的读穿缓存。

    线程安全：内部只持有 `Redis`（每线程一条连接）与 `CacheStats`（带锁），
    所以可以被 FastAPI 的线程池并发调用。
    """

    __slots__ = ("_redis", "stats", "ttl_s")

    def __init__(self, redis: Redis | None, ttl_s: int = DEFAULT_TTL_S) -> None:
        self._redis = redis
        self.ttl_s = ttl_s
        self.stats = CacheStats()

    # -- 读路径 ------------------------------------------------------------ #

    def get_or_load(
        self,
        item_id: int,
        loader: Callable[[], dict[str, Any] | None],
        *,
        enabled: bool = True,
        refresh: bool = False,
    ) -> tuple[dict[str, Any] | None, bool]:
        """返回 `(值, 是否命中缓存)`。

        `enabled=False` 走纯数据库路径（对照组）；`refresh=True` 跳过读但会回填
        （用来量"冷缓存"这一档的耗时）。Redis 不可用时**不抛异常**，
        记一次 `errors` 之后照常查库。
        """
        if not enabled or self._redis is None:
            self.stats.record_bypass()
            return loader(), False

        key = f"item:detail:{item_id}"
        if not refresh:
            try:
                raw = self._redis.get(key)
            except (RedisUnavailableError, RedisError):
                self.stats.record_error()
                return loader(), False

            if raw is not None:
                self.stats.record_hit()
                if raw == MISS_SENTINEL:
                    return None, True
                try:
                    decoded = json.loads(raw)
                except json.JSONDecodeError:
                    # 缓存里是脏数据（被人手工 SET 过、或旧版本写进去的格式）：
                    # 当成未命中并覆盖掉，而不是把 500 抛给调用方。
                    self.stats.record_miss()
                    value = loader()
                    self._store(key, value)
                    return value, False
                return decoded if isinstance(decoded, dict) else None, True

        value = loader()
        self.stats.record_miss()
        self._store(key, value)
        return value, False

    def _store(self, key: str, value: dict[str, Any] | None) -> None:
        if self._redis is None:
            return
        payload = MISS_SENTINEL if value is None else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        try:
            self._redis.setex(key, self.ttl_s, payload)
        except (RedisUnavailableError, RedisError):
            # 回填失败不影响这次请求的正确性：值已经查到了，只是下次命不中。
            self.stats.record_error()

    # -- 写路径 ------------------------------------------------------------ #

    def invalidate(self, item_id: int) -> None:
        """改库存之后必须调用。失败只记 error，不抛——写路径不能因为缓存挂了而失败。"""
        if self._redis is None:
            return
        try:
            self._redis.delete(f"item:detail:{item_id}")
        except (RedisUnavailableError, RedisError):
            self.stats.record_error()

    # -- 诊断 -------------------------------------------------------------- #

    def diagnostics(self) -> dict[str, Any]:
        info = self.stats.snapshot()
        info["ttl_s"] = self.ttl_s
        info["backend"] = "redis" if self._redis is not None else "none"
        if self._redis is not None:
            info["redis_host"] = f"{self._redis.host}:{self._redis.port}"
            info["redis_connections_opened"] = self._redis.connections_opened
        return info
