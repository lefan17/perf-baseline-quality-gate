"""被测服务的启动/停止控制。

压测报告里必须能写清"打的是哪个进程、端口几号、库在哪"。
随手 `uvicorn sut.app:app` 起的服务，端口靠猜、库落在仓库里、
跑完不回收 —— 这是压测结论不可信的常见起点，所以在代码里就堵掉：

* **端口由系统分配**（bind 0），不是写死的 8000；
* **数据库落在 pytest 临时目录**，跑完即弃，不污染开发库；
* **临时目录由 `SutHandle.stop()` 收回**：自己 mkdtemp 的自己删，调用方
  传进来的路径一律不碰；调试时用 `keep_tmp=True` 显式保留；
* **就绪探针等真返回 200**，不用 `sleep 2` 赌它起没起来
  （这是"性能测试跑出假失败"最常见的原因之一）。
"""

from __future__ import annotations

import os
import shutil
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
    # 由 start_sut 自己 mkdtemp 出来的目录（调用方传了 db_path/log_dir 时这里为空，
    # 那些目录**不是我们的，不能删**）。stop() 负责把它们收干净。
    tmp_dirs: list[str] = field(default_factory=list)
    # 调试用：保留临时目录便于事后翻日志。默认关闭 —— 曾经因为没人清理，
    # 系统 Temp 下累积了 289 组 perf-sut-db-* / perf-sut-log-*。
    keep_tmp: bool = False
    log_file: object | None = None

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        # 先关日志句柄再删目录：Windows 上被占用的文件删不掉。
        if self.log_file is not None:
            try:
                self.log_file.close()
            except OSError:
                pass
            self.log_file = None
        self.cleanup_tmp()

    def cleanup_tmp(self) -> list[str]:
        """删掉自己建的临时目录，返回**没删成功**的路径列表。

        尽力而为：uvicorn 刚退出时 Windows 可能还短暂占着句柄，重试几次即可；
        仍然失败就返回路径，由调用方/测试决定要不要抱怨 —— 清理失败不该
        把一次成功的压测变成失败。
        """
        if self.keep_tmp:
            return []
        failed: list[str] = []
        for d in self.tmp_dirs:
            p = Path(d)
            if not p.exists():
                continue
            for attempt in range(3):
                try:
                    shutil.rmtree(p)
                    break
                except OSError:
                    if attempt == 2:
                        failed.append(str(p))
                    else:
                        time.sleep(0.2)
        self.tmp_dirs = []
        return failed

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
    keep_tmp: bool = False,
) -> SutHandle:
    """起一个被测服务，灌好数据，等它就绪。

    `db_path` / `log_dir` 只在自己没传时才由本函数 mkdtemp —— 只有这种"自己建的"
    目录才会在 `stop()` 里被删。调用方传进来的路径一律不碰：那可能是仓库里的库、
    也可能是别人还要复用的目录，删了就是数据事故。

    `keep_tmp=True`（调试用）保留临时目录，便于事后翻 uvicorn 日志与库文件；
    默认 False，因为**没人清理正是这批目录累积到 289 组的原因**。
    """
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"

    tmp_dirs: list[str] = []
    if db_path is None:
        tmp = Path(tempfile.mkdtemp(prefix="perf-sut-db-"))
        tmp_dirs.append(str(tmp))
        db_path = str(tmp / "sut.db")
    if log_dir is None:
        log_dir = tempfile.mkdtemp(prefix="perf-sut-log-")
        tmp_dirs.append(str(log_dir))
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    log_path = str(Path(log_dir) / f"sut-{port}.log")

    def _discard_tmp() -> None:
        # 起服务失败时也要收拾：失败路径漏掉的临时目录最难被发现。
        for d in tmp_dirs:
            shutil.rmtree(d, ignore_errors=True)

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
        _discard_tmp()
        raise RuntimeError(f"seed 失败：{seed.stdout}\n{seed.stderr}")

    log_file = open(log_path, "w", encoding="utf-8")  # noqa: SIM115 - 由 SutHandle.stop() 关闭
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
        tmp_dirs=tmp_dirs,
        keep_tmp=keep_tmp,
        log_file=log_file,
    )
    if not wait_ready(base_url):
        # 先取日志再收摊：stop() 会把日志目录删掉，之后再 tail 就只剩空字符串，
        # 而这行日志正是排查"服务为什么没起来"的唯一线索。
        tail = handle.tail()
        handle.stop()
        raise RuntimeError(f"被测服务未就绪（{base_url}）：\n{tail}")
    return handle
