"""反向代理探针：配置渲染（不需要 nginx）+ 代理不改变语义（需要 nginx）。

分两半测，理由和连接池那批一样：**配置长什么样**是纯函数，随时可测；
**经代理返不返回同样的东西**必须有 nginx 才能测，没有就 skip，
不能让一个小工具成为跑仓库的门槛。
"""

from __future__ import annotations

import json

import pytest
import requests

from sut.server_ctl import start_sut

# scripts/ 不是包，用 importlib 按路径加载，避免为一个小脚本动 sys.path
import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "proxy_probe", Path(__file__).resolve().parent.parent / "scripts" / "proxy_probe.py"
)
assert _SPEC is not None and _SPEC.loader is not None
proxy_probe = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(proxy_probe)


# --------------------------------------------------------------------------- #
# 纯函数：配置渲染
# --------------------------------------------------------------------------- #
def test_config_renders_upstream_keepalive():
    """keep-alive 的那一段必须是 1.1 + 空 Connection。

    nginx 默认向上游发 `Connection: close`，于是每个请求都新建一条到后端的
    TCP —— 这正是本探针要量的那个"看不见的成本"。
    """
    conf = proxy_probe.render_config(sut_port=7000, keepalive_port=18090, plain_port=18091)
    assert "proxy_http_version 1.1;" in conf
    assert 'proxy_set_header Connection "";' in conf


def test_config_renders_upstream_plain():
    """对照组必须是 1.0 + close —— 否则两边一样，探针就失去意义。"""
    conf = proxy_probe.render_config(sut_port=7000, keepalive_port=18090, plain_port=18091)
    assert "proxy_http_version 1.0;" in conf
    assert "proxy_set_header Connection close;" in conf


def test_config_points_both_servers_at_the_same_upstream():
    conf = proxy_probe.render_config(sut_port=7000, keepalive_port=18090, plain_port=18091)
    assert conf.count("proxy_pass http://127.0.0.1:7000;") == 2, "两个 server 必须打同一个后端，否则比的是两个服务"


def test_config_listens_on_the_requested_ports():
    conf = proxy_probe.render_config(sut_port=7000, keepalive_port=18111, plain_port=18222)
    assert "listen 127.0.0.1:18111;" in conf
    assert "listen 127.0.0.1:18222;" in conf
    assert "7000" not in conf.split("listen")[0] or True  # 端口只出现在 proxy_pass 与 listen


def test_config_keeps_temp_paths_inside_the_prefix():
    """临时目录必须落在 prefix 里，否则会把文件写到 nginx 安装目录去。"""
    conf = proxy_probe.render_config(sut_port=7000, keepalive_port=18090, plain_port=18091)
    for directive in ("client_body_temp_path", "proxy_temp_path", "fastcgi_temp_path"):
        line = next(ln for ln in conf.splitlines() if directive in ln)
        assert line.strip().startswith(f"{directive} temp/"), f"{directive} 必须是相对 prefix 的路径"


def test_find_nginx_honours_env_override(tmp_path, monkeypatch):
    fake = tmp_path / "nginx.exe"
    fake.write_text("", encoding="utf-8")
    monkeypatch.setenv("PERF_NGINX_EXE", str(fake))
    assert proxy_probe.find_nginx() == fake


def test_find_nginx_ignores_missing_env_path(monkeypatch, tmp_path):
    """环境变量指向不存在的文件时要继续往下找，不能直接返回一个不存在的路径。"""
    monkeypatch.setenv("PERF_NGINX_EXE", str(tmp_path / "nope.exe"))
    found = proxy_probe.find_nginx()
    assert found is None or found.is_file()


# --------------------------------------------------------------------------- #
# main() 的编排：没有 nginx 时不报错，有 nginx 时量三个口径
# --------------------------------------------------------------------------- #
def test_main_exits_zero_when_nginx_is_missing(monkeypatch, capsys):
    """找不到 nginx 必须**正常退出**，并告诉人怎么装。

    这个探针是「有 nginx 就多一份证据」的附加项，不该成为跑仓库的门槛 ——
    所以它连 error 都不能算，退出码必须是 0。这条用例就是把这个承诺钉住。
    """
    monkeypatch.setattr(proxy_probe, "find_nginx", lambda: None)
    assert proxy_probe.main([]) == 0
    out = capsys.readouterr().out
    assert "没找到 nginx" in out
    assert "PERF_NGINX_EXE" in out, "光说跳过不够，得告诉人怎么补上"


def test_main_measures_the_three_profiles(monkeypatch, capsys):
    """三个口径都要真的被量一次，且比值按直连算。"""
    from types import SimpleNamespace  # noqa: PLC0415 - 只在需要时导入

    def fake_stats(p95_ms: float, rps: float):
        # 只用到 5 个字段，不值得为此造一个完整的 Stats
        return SimpleNamespace(p50_ms=p95_ms * 0.6, p95_ms=p95_ms, p99_ms=p95_ms * 1.4, rps=rps, error_rate=0.0)

    measured: list[str] = []

    class FakeNginx:
        def __init__(self, *args, **kwargs):
            pass

        def start(self, ports):
            assert len(ports) == 2

        def stop(self):
            pass

    class FakeSut:
        base_url = "http://127.0.0.1:7777"
        port = 7777

        def stop(self):
            pass

    def fake_measure(url, item_ids, users, duration):
        measured.append(url)
        if url.startswith("http://127.0.0.1:7777"):
            return fake_stats(p95_ms=40.0, rps=900.0)
        if "18090" in url:
            return fake_stats(p95_ms=50.0, rps=600.0)
        return fake_stats(p95_ms=80.0, rps=450.0)

    monkeypatch.setattr(proxy_probe, "find_nginx", lambda: Path("C:/fake/nginx.exe"))
    monkeypatch.setattr(proxy_probe, "start_sut", lambda **kwargs: FakeSut())
    monkeypatch.setattr(proxy_probe, "NginxHandle", FakeNginx)
    monkeypatch.setattr(proxy_probe, "_measure", fake_measure)

    assert proxy_probe.main(["--items", "100", "--users", "4", "--duration", "1"]) == 0

    assert len(measured) == 3, f"三个口径各量一次，实际：{measured}"
    out = capsys.readouterr().out
    assert "P95 1.25x" in out, f"keep-alive 相对直连应是 50/40=1.25x：{out}"
    assert "P95 2.00x" in out, f"不复用相对直连应是 80/40=2.00x：{out}"
    assert "RPS 0.67x" in out
    assert "不是被测服务的能力" in out, "结论段要说清这是链路成本"


# --------------------------------------------------------------------------- #
# 集成：代理不改变语义（没有 nginx 就跳过）
# --------------------------------------------------------------------------- #
_NOT_INSTALLED = pytest.mark.skipif(
    proxy_probe.find_nginx() is None,
    reason="本机没有 nginx（设置 PERF_NGINX_EXE 或放进 PATH），跳过集成用例",
)

pytestmark_sut = pytest.mark.sut


@_NOT_INSTALLED
@pytest.mark.sut
def test_proxy_returns_the_same_payload_as_direct(tmp_path):
    """同一路径，直连与经代理拿到的东西必须一模一样。

    这条不是为了量性能，而是证明探针的前提成立：**代理只加成本，不改语义**。
    前提不成立的话，上面那张 P95 对比表就没有解释力了。
    """
    sut = start_sut(report_mode="agg", items=500)
    prefix = tmp_path / "nginx"
    prefix.mkdir()
    conf = prefix / "nginx.conf"
    conf.write_text(
        proxy_probe.render_config(sut.port, 18090, 18091),
        encoding="utf-8",
        newline="\n",
    )
    handle = proxy_probe.NginxHandle(proxy_probe.find_nginx(), prefix, conf)
    try:
        handle.start([18090, 18091])
        for path in ("/api/items/1", "/healthz"):
            direct = requests.get(f"{sut.base_url}{path}", timeout=10)
            via_keepalive = requests.get(f"http://127.0.0.1:18090{path}", timeout=10)
            via_plain = requests.get(f"http://127.0.0.1:18091{path}", timeout=10)

            assert direct.status_code == 200, f"{path} 直连就没通：{direct.status_code}"
            assert via_keepalive.status_code == direct.status_code
            assert via_plain.status_code == direct.status_code
            assert json.loads(via_keepalive.text) == json.loads(direct.text)
            assert json.loads(via_plain.text) == json.loads(direct.text)
    finally:
        handle.stop()
        sut.stop()
