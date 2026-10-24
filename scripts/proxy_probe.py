r"""反向代理探针：直连 / 经 nginx（上游 keep-alive）/ 经 nginx（每次新建上游连接）。

## 这个探针回答什么

「压测数字是服务端的能力，还是链路的能力？」——同一个 SUT、同一批请求，
只把链路换掉，P95 与 RPS 会变多少。三个口径：

1. **直连**：`client → SUT`，基准线；
2. **经 nginx，上游 1.1**：`proxy_http_version 1.1` + `proxy_set_header Connection ""`，
   代理与后端之间复用连接；
3. **经 nginx，上游 1.0**：`proxy_http_version 1.0`，每次转发都新建一条到后端的连接。

2 与 3 的差值是「上游连接复用」这一项配置的价格；1 与 2 的差值是
「多一跳代理」本身的价格。两个数都值得知道，因为**很多团队的"性能问题"
最后定位到的是代理没开 keep-alive**，而不是服务端代码。

## 为什么本机跑 nginx 而不是 Docker

试过 Docker 容器反代：容器里 `wget http://host.docker.internal:18099/` 报
`can't connect to remote host (192.168.65.254): Connection refused` ——
Docker Desktop 的网关能解析但回连不到宿主的 127.0.0.1，而 SUT 默认只绑
127.0.0.1（`start_sut` 的 `host` 参数就是为此加的，可以传 `0.0.0.0` 绕开，
但那等于把被测服务暴露到局域网）。本机 nginx.exe 反代 127.0.0.1 没有这层
网络穿越，配置也更接近「服务部署在同一台机器上」的常见形态。

## 用法

```powershell
$env:PYTHONIOENCODING="utf-8"
& ".\.venv\Scripts\python.exe" scripts\proxy_probe.py --users 10 --duration 8
```

找不到 nginx 时**不报错**，只打印一条怎么装的提示并以 0 退出 ——
这个探针是「有 nginx 就能量」的附加证据，不该成为跑仓库的门槛。
"""

from __future__ import annotations

import argparse
import os
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from bench.engine import Runner, Stats, run_api_call
from bench.scenarios import bind
from sut.server_ctl import start_sut

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_KEEPALIVE_PORT = 18090
DEFAULT_PLAIN_PORT = 18091

#: 反代到后端时显式声明「不要 Close」。nginx 默认向上游发 `Connection: close`
#: （`proxy_http_version 1.0` + 不带 Connection 头时），于是每个请求都在
#: 后端新建一条 TCP —— 服务端的 `accept()` 与握手成本会全部算进 P95，
#: 而这块成本跟业务代码毫无关系。
KEEPALIVE_HEADERS = '            proxy_http_version 1.1;\n            proxy_set_header Connection "";'

PLAIN_HEADERS = "            proxy_http_version 1.0;\n            proxy_set_header Connection close;"

CONFIG_TEMPLATE = """\
# 由 scripts/proxy_probe.py 生成，跑完即删。
worker_processes 1;
error_log logs/error.log warn;
pid logs/nginx.pid;

events {{
    worker_connections 512;
}}

http {{
    access_log off;
    client_body_temp_path temp/client_body;
    proxy_temp_path temp/proxy;
    fastcgi_temp_path temp/fastcgi;
    uwsgi_temp_path temp/uwsgi;
    scgi_temp_path temp/scgi;

    server {{
        listen 127.0.0.1:{keepalive_port};
        location / {{
            proxy_pass http://127.0.0.1:{sut_port};
{keepalive}
            proxy_set_header Host $host;
        }}
    }}

    server {{
        listen 127.0.0.1:{plain_port};
        location / {{
            proxy_pass http://127.0.0.1:{sut_port};
{plain}
            proxy_set_header Host $host;
        }}
    }}
}}
"""


def render_config(sut_port: int, keepalive_port: int, plain_port: int) -> str:
    """渲染 nginx 配置。纯函数，便于在不装 nginx 的机器上测。"""
    return CONFIG_TEMPLATE.format(
        sut_port=sut_port,
        keepalive_port=keepalive_port,
        plain_port=plain_port,
        keepalive=KEEPALIVE_HEADERS,
        plain=PLAIN_HEADERS,
    )


def find_nginx() -> Path | None:
    """找 nginx 可执行文件：环境变量优先，其次 PATH，最后几个常见安装位置。"""
    env = os.environ.get("PERF_NGINX_EXE")
    if env and Path(env).is_file():
        return Path(env)

    found = shutil.which("nginx")
    if found:
        return Path(found)

    candidates = [
        Path(r"D:\study\AppCache\nginx-1.27.2\nginx.exe"),
        Path(r"C:\nginx\nginx.exe"),
        Path("/usr/sbin/nginx"),
        Path("/usr/local/bin/nginx"),
    ]
    for p in candidates:
        if p.is_file():
            return p
    return None


def _wait_port(port: int, timeout_s: float = 10.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.1)
    return False


class NginxHandle:
    """起一个前台可控的 nginx 实例（Windows 上它会自己fork 到后台，用 -s quit 收）。"""

    def __init__(self, exe: Path, prefix: Path, config: Path) -> None:
        self.exe = exe
        self.prefix = prefix
        self.config = config
        self._proc: subprocess.Popen | None = None

    def start(self, ports: list[int]) -> None:
        for sub in ("logs", "temp"):
            (self.prefix / sub).mkdir(parents=True, exist_ok=True)
        # Windows 上 nginx **不会**自己退到后台：不带 -s 时它就占着控制台一直跑。
        # 所以这里必须 Popen 而不是 run —— 用 run 会一直等到超时（第一次跑就卡在这）。
        self._proc = subprocess.Popen(
            [str(self.exe), "-p", str(self.prefix), "-c", str(self.config)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for port in ports:
            if not _wait_port(port):
                self.stop()
                raise RuntimeError(f"nginx 没在 {port} 上监听，日志：{self._tail_log()}")

    def _tail_log(self) -> str:
        log = self.prefix / "logs" / "error.log"
        if not log.is_file():
            return "(无 error.log)"
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-20:])

    def _run(self, cmd: list[str]) -> None:
        """跑一条 nginx 命令并等它返回。

        这里**不能**用 `capture_output=True`：Windows 上 nginx 会把自己 fork 到
        后台，子进程继承了 stdout 句柄，于是管道永远等不到 EOF，
        `subprocess.run` 会一直挂住（第一次跑就卡死在这里）。
        输出本来就写进 `-p` 目录下的 `logs/error.log`，需要时读文件即可。
        """
        proc = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"nginx 命令失败（exit {proc.returncode}）：{self._tail_log()}")

    def stop(self) -> None:
        """停掉 nginx，并**确保进程真的没了**。

        只发 `-s quit` 是不够的：它按 prefix 里的 `logs/nginx.pid` 找 master，
        一旦那个目录先被删掉（探针的 finally 就是先 rmtree 再 stop），
        quit 会静默失败，master 与 worker 变成孤儿继续占着端口 ——
        下一次跑探针会连到那个"旧配置指向已死后端"的实例，得到一堆 502，
        而且很难看出是残留进程造成的（第一次踩坑就是这个）。
        """
        try:
            self._run([str(self.exe), "-p", str(self.prefix), "-c", str(self.config), "-s", "quit"])
        except Exception:  # noqa: BLE001 - 收尾阶段失败不该盖掉真正的错误
            pass
        if self._proc is not None:
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover - 兜底
                pass
            if self._proc.poll() is None:
                # Windows 上要连子进程一起杀，否则 worker 继续监听
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(self._proc.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    )
                else:  # pragma: no cover - 非 Windows 分支
                    self._proc.kill()
            self._proc = None
        time.sleep(0.3)


def _measure(base_url: str, item_ids: list[int], users: int, duration_s: float) -> Stats:
    """用与平台同一套引擎量一个口径，保证数字之间可比。

    场景签名用仓库既有的 `(session, base_url, timeout_s)` 形式，再交给
    `bench.scenarios.bind` 适配成引擎要的 `(session) -> Sample` ——
    不自己写闭包，是为了和 `REGISTRY` 里的场景走同一条路径。
    """
    rnd = random.Random(42)

    def scenario(session, url, timeout_s: float = 10.0):
        item_id = rnd.choice(item_ids)
        return run_api_call(
            session,
            url,
            "GET",
            f"/api/items/{item_id}",
            scenario="GET /api/items/{id}",
            timeout_s=timeout_s,
        )

    runner = Runner(
        base_url=base_url,
        scenarios=[(bind(scenario, base_url), 1)],
        users=users,
        duration_s=duration_s,
        warmup_drop_s=1.0,
    )
    return runner.run()


def _fmt(stats: Stats) -> str:
    return (
        f"P50 {stats.p50_ms:7.2f}ms  P95 {stats.p95_ms:7.2f}ms  "
        f"P99 {stats.p99_ms:7.2f}ms  RPS {stats.rps:7.1f}  "
        f"错误率 {stats.error_rate * 100:5.2f}%"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="反向代理探针：直连 vs 经 nginx（上游 keep-alive / 新建）")
    parser.add_argument("--items", type=int, default=20_000, help="SUT 数据量")
    parser.add_argument("--users", type=int, default=10, help="并发用户数")
    parser.add_argument("--duration", type=float, default=8.0, help="每个口径的持续秒数")
    parser.add_argument("--keepalive-port", type=int, default=DEFAULT_KEEPALIVE_PORT)
    parser.add_argument("--plain-port", type=int, default=DEFAULT_PLAIN_PORT)
    args = parser.parse_args(argv)

    nginx = find_nginx()
    if nginx is None:
        print("[proxy-probe] 没找到 nginx，跳过。")
        print("  设置环境变量 PERF_NGINX_EXE 指向 nginx 可执行文件，或把它放进 PATH。")
        print("  Windows: https://nginx.org/download/nginx-1.27.2.zip 解压即可，无需安装。")
        return 0

    print(f"[proxy-probe] nginx = {nginx}")
    print(f"[proxy-probe] 数据量 {args.items}，{args.users} 并发，每口径 {args.duration}s（丢弃前 1s 热身）")

    sut = start_sut(report_mode="agg", items=args.items)
    prefix = Path(tempfile.mkdtemp(prefix="perf-nginx-"))
    handle: NginxHandle | None = None
    try:
        config = prefix / "nginx.conf"
        config.write_text(
            render_config(sut.port, args.keepalive_port, args.plain_port),
            encoding="utf-8",
            newline="\n",
        )
        handle = NginxHandle(nginx, prefix, config)
        handle.start([args.keepalive_port, args.plain_port])

        # 同一个热点集，避免三个口径打到的数据分布不同
        rnd = random.Random(7)
        item_ids = [rnd.randint(1, args.items) for _ in range(200)]

        print()
        rows: list[tuple[str, Stats]] = []
        for label, url in (
            ("直连 SUT", sut.base_url),
            ("经 nginx（上游 keep-alive）", f"http://127.0.0.1:{args.keepalive_port}"),
            ("经 nginx（上游每次新建）", f"http://127.0.0.1:{args.plain_port}"),
        ):
            stats = _measure(url, item_ids, args.users, args.duration)
            rows.append((label, stats))
            print(f"  {label:28s} {_fmt(stats)}")

        print()
        base = rows[0][1]
        if base.p95_ms > 0 and base.rps > 0:
            print("相对直连：")
            for label, stats in rows[1:]:
                print(f"  {label:28s} P95 {stats.p95_ms / base.p95_ms:.2f}x  RPS {stats.rps / base.rps:.2f}x")
        else:
            print("基线口径本身就在失败（错误率见上），不做比值。")

        print()
        print("怎么读这两个数字：")
        print("  - 「多一跳代理」的价格 = 经代理 keep-alive / 直连")
        print("  - 「上游连接不复用」的价格 = 经代理新建 / 经代理 keep-alive")
        print("  两者都是链路成本，不是被测服务的能力；本仓的基线口径一律是直连。")
        return 0
    finally:
        if handle is not None:
            handle.stop()
        shutil.rmtree(prefix, ignore_errors=True)
        sut.stop()


if __name__ == "__main__":
    sys.exit(main())
