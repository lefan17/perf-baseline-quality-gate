"""被测服务的启动/停止控制。

压测报告里必须能写清"打的是哪个进程、端口几号、库在哪"。
随手 `uvicorn sut.app:app` 起的服务，端口靠猜、库落在仓库里、
跑完不回收 —— 这是压测结论不可信的常见起点，所以在代码里就堵掉：

* **端口由系统分配**（bind 0），不是写死的 8000；
* **数据库落在 pytest 临时目录**，跑完即弃，不污染开发库；
* **就绪探针等真返回 200**，不用 `sleep 2` 赌它起没起来
  （这是"性能测试跑出假失败"最常见的原因之一）。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class SutHandle:
    base_url: str
    port: int
    db_path: str
    proc: subprocess.Popen
    log_path: str
    items: int = 0
    env: dict = field(default_factory=dict)

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)

    def tail(self, n: int = 25) -> str:
        try:
            return "\n".join(Path(self.log_path).read_text(encoding="utf-8", errors="replace").splitlines()[-n:])
        except OSError:
            return ""


def wait_ready(base_url: str, timeout_s: float = 40.0) -> bool:
    deadline = time.perf_counter() + timeout_s
    while time.perf_counter() < deadline:
        try:
            if requests.get(f"{base_url}/healthz", timeout=1.0).status_code == 200:
                return True
        except requests.RequestException:
            time.sleep(0.15)
    return False


def start_sut(
    report_mode: str = "agg",
    items: int = 20_000,
    db_path: str | None = None,
    log_dir: str | None = None,
    extra_env: dict | None = None,
) -> SutHandle:
    """起一个被测服务，灌好数据，等它就绪。"""
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"

    if db_path is None:
        tmp = Path(tempfile.mkdtemp(prefix="perf-sut-db-"))
        db_path = str(tmp / "sut.db")
    if log_dir is None:
        log_dir = tempfile.mkdtemp(prefix="perf-sut-log-")
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    log_path = str(Path(log_dir) / f"sut-{port}.log")

    env = os.environ.copy()
    env.update(
        {
            "PERF_SUT_DB": db_path,
            "PERF_SUT_REPORT_MODE": report_mode,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONPATH": str(ROOT),
        }
    )
    if extra_env:
        env.update(extra_env)

    # 先灌数据再起服务：避免"第一个请求撞上建表"这种热身噪声混进基线。
    seed = subprocess.run(
        [sys.executable, "-m", "sut.seed", str(items)],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if seed.returncode != 0:
        raise RuntimeError(f"seed 失败：{seed.stdout}\n{seed.stderr}")

    log_file = open(log_path, "w", encoding="utf-8")  # noqa: SIM115 - 由进程生命周期持有
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "sut.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=str(ROOT),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    handle = SutHandle(
        base_url=base_url,
        port=port,
        db_path=db_path,
        proc=proc,
        log_path=log_path,
        items=items,
        env={"report_mode": report_mode},
    )
    if not wait_ready(base_url):
        handle.stop()
        raise RuntimeError(f"被测服务未就绪（{base_url}）：\n{handle.tail()}")
    return handle
