"""最小 Redis 客户端：只实现这个项目真正用到的那几条命令，纯标准库。

## 为什么不用 `redis` 包

三个理由，按重要性排：

1. **依赖清单是这个仓库的卖点之一**：`requirements.txt` 每条都带 `==` 实测版本，
   并且由 `scripts/check_deps_sync.py` 校验「pyproject 与 requirements 逐字一致」。
   为了一条 SET / GET 再加一个第三方包，等于让三处清单同时动一次。
2. **压测引擎对客户端的行为有要求**：`bench/engine.py` 里每个 worker 用的是
   自己那条 keep-alive 连接（`session.trust_env = False` 也是为了排除代理干扰）。
   一个自带连接池的库会把这个控制权拿走，而「连接到底复用没复用」
   是性能数字能不能解释的前提。
3. **面试时讲协议比讲 API 更有信息量**：RESP2 就五种前缀
   （`+` 简单串 / `-` 错误 / `:` 整数 / `$` 批量串 / `*` 数组），
   自己解析一遍才知道 `SET` 为什么不能用 `+OK` 判断成功以外的任何东西。

## 明确不支持的

- 集群、哨兵、TLS、RESP3、RESP 协议以外的任何东西；
- 命令超集：只做 `PING` / `GET` / `SET` / `SETEX` / `DEL` / `INCR` / `INFO`。
  新增命令请连同「谁在什么场景下需要它」一起写进本文档，别悄悄加。

## 连接模型

**每线程一条长连接**（`threading.local`），不是每请求一条：

- 每个请求都建 TCP，量的就是三次握手而不是被测服务；
- 共享一条连接则必须加锁，压测里那等于人为制造串行点。
- 线程池里 worker 数量固定，所以「每线程一条」的连接数上界是可预期的。

连接一旦出错（超时 / 对端关闭 / 协议破损）就**丢弃并重建**，不做重试：
缓存的语义是「拿不到就走 DB」，重试只会把一个 5ms 的降级拖成 50ms。
"""

from __future__ import annotations

import socket
import threading
from typing import Any

#: 默认超时。刻意很短：缓存超时应该快速降级到数据库，
#: 而不是把「Redis 慢」放大成「接口慢」——那正是缓存最典型的反模式。
DEFAULT_TIMEOUT_S = 0.5


class RedisError(Exception):
    """服务端返回的错误回复（`-ERR ...`）。说明命令用法或数据本身有问题。"""


class RedisUnavailableError(Exception):
    """连不上 / 超时 / 协议破损。调用方应当据此**降级**而不是当成接口失败。"""


def _encode(*args: object) -> bytes:
    """把一条命令编码成 RESP 的「数组 + 批量串」形式。

    `SET foo bar` 会被编码成 `*3\\r\\n$3\\r\\nSET\\r\\n$3\\r\\nfoo\\r\\n$3\\r\\nbar\\r\\n`。
    长度前缀是**字节数**不是字符数，所以中文值必须先用 utf-8 编码再取长度。
    """
    out: list[bytes] = [f"*{len(args)}\r\n".encode()]
    for arg in args:
        raw = arg if isinstance(arg, bytes) else str(arg).encode("utf-8")
        out.append(f"${len(raw)}\r\n".encode())
        out.append(raw)
        out.append(b"\r\n")
    return b"".join(out)


def _read_reply(reader: Any) -> Any:
    """按 RESP2 的五种前缀递归解析一条回复。"""
    line = reader.readline()
    if not line:
        raise RedisUnavailableError("连接被对端关闭（读到 EOF）")
    line = line.rstrip(b"\r\n")
    if not line:
        raise RedisUnavailableError("读到空行，协议没对齐")

    kind, payload = line[:1], line[1:]
    if kind == b"+":
        return payload.decode("utf-8", "replace")
    if kind == b"-":
        raise RedisError(payload.decode("utf-8", "replace"))
    if kind == b":":
        return int(payload)
    if kind == b"$":
        n = int(payload)
        if n < 0:  # $-1 表示 nil
            return None
        data = reader.read(n)
        reader.read(2)  # 丢弃结尾 CRLF
        return data
    if kind == b"*":
        n = int(payload)
        if n < 0:
            return None
        return [_read_reply(reader) for _ in range(n)]
    raise RedisUnavailableError(f"未知的 RESP 前缀：{kind!r}")


class _Connection:
    """一条到 Redis 的 TCP 连接，带一个缓冲的 reader。"""

    __slots__ = ("_reader", "_sock")

    def __init__(self, host: str, port: int, timeout_s: float) -> None:
        self._sock = socket.create_connection((host, port), timeout=timeout_s)
        self._sock.settimeout(timeout_s)
        self._reader = self._sock.makefile("rb")

    def roundtrip(self, *args: object) -> Any:
        self._sock.sendall(_encode(*args))
        return _read_reply(self._reader)

    def close(self) -> None:
        try:
            self._reader.close()
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass


class Redis:
    """每线程一条连接的最小客户端。**不是线程安全的共享实例**，但可以跨线程用。"""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 6379,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        db: int = 0,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout_s = timeout_s
        self.db = db
        self._local = threading.local()
        #: 累计成功建立的连接数。用来在诊断接口里区分「一条连接撑完了整轮」
        #: 与「每个请求都重连」——后者会把 RPS 打骨折，而现象看起来像 SUT 慢。
        self.connections_opened = 0
        self._lock = threading.Lock()

    # -- 连接管理 ---------------------------------------------------------- #

    def _conn(self) -> _Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = _Connection(self.host, self.port, self.timeout_s)
            if self.db:
                conn.roundtrip("SELECT", self.db)
            self._local.conn = conn
            with self._lock:
                self.connections_opened += 1
        return conn

    def _drop(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def _call(self, *args: object) -> Any:
        """一次往返。任何 I/O 异常都翻译成 `RedisUnavailableError` 并丢弃连接。"""
        try:
            return self._conn().roundtrip(*args)
        except RedisError:
            raise
        except (OSError, RedisUnavailableError, ValueError) as exc:
            self._drop()
            raise RedisUnavailableError(str(exc)) from exc

    # -- 命令 -------------------------------------------------------------- #

    def ping(self) -> bool:
        try:
            return self._call("PING") == "PONG"
        except (RedisUnavailableError, RedisError):
            return False

    def get(self, key: str) -> bytes | None:
        value = self._call("GET", key)
        return value if isinstance(value, bytes) else None

    def setex(self, key: str, ttl_s: int, value: str | bytes) -> None:
        self._call("SETEX", key, ttl_s, value)

    def set(self, key: str, value: str | bytes) -> None:
        self._call("SET", key, value)

    def delete(self, *keys: str) -> int:
        result = self._call("DEL", *keys)
        return int(result) if isinstance(result, int) else 0

    def incr(self, key: str) -> int:
        result = self._call("INCR", key)
        return int(result) if isinstance(result, int) else 0

    def info(self, section: str | None = None) -> dict[str, str]:
        """`INFO` 返回的是一段 `key:value` 文本，这里解析成字典。"""
        raw = self._call("INFO", section) if section else self._call("INFO")
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        out: dict[str, str] = {}
        for line in text.splitlines():
            if not line or line.startswith("#") or ":" not in line:
                continue
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip()
        return out

    def close(self) -> None:
        self._drop()
