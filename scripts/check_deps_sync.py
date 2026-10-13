"""依赖清单一致性检查：`requirements*.txt` ↔ `pyproject.toml`。

## 为什么需要这个脚本

`pyproject.toml` 里的 `[project].dependencies` 与 `requirements.txt` 必须
表达同一件事，但它们是**两份文本**。这个仓库把「依赖全部 == 钉死」当成
核心卖点（README 第八节），一旦两处漂移，就会出现最糟的组合：

* CI 装的是 requirements.txt 的版本 → 徽章是绿的；
* 而 `pip install .` 或任何读 pyproject 的工具装的是另一套 → 数字对不上。

一份**看起来一直是绿的**依赖清单，比一份明显过时的清单危险得多
（跟仓库里记的「静默放行」是同一类问题）。

## 口径

按**规格字符串逐字比较**，不做版本号语义解析：
`requirements.txt` 里的每一行非注释内容，都必须逐字出现在对应的
pyproject 列表里，反之亦然。顺序不敏感（比对的是集合），
大小写规范化只用于报错提示。

    python scripts/check_deps_sync.py

退出码 0 = 一致，1 = 漂移。
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path


def _force_utf8_stdio() -> None:
    """让输出在所有平台上都是 UTF-8。

    不是洁癖：本脚本要打印 `↔`，而 Windows 控制台默认是 GBK，
    实测会直接抛
        UnicodeEncodeError: 'gbk' codec can't encode character '\\u2194'
    也就是说这个检查在 Windows 上**还没开始比对就先崩了**。
    与 platform_cli.py 的 `_make_stdout_utf8()` 是同一个理由。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):  # 已被重定向成不可重配置的对象
                pass


_force_utf8_stdio()

ROOT = Path(__file__).resolve().parent.parent

# (requirements 文件名, pyproject 取值路径, 说明)
PAIRS = [
    ("requirements.txt", ("project", "dependencies"), "运行时依赖"),
    ("requirements-dev.txt", ("project", "optional-dependencies", "dev"), "开发工具链"),
]


def read_requirements(path: Path) -> list[str]:
    """读出 requirements 文件里的规格行，丢掉注释与空行。"""
    specs: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        specs.append(line)
    return specs


def dig(data: dict, keys: tuple[str, ...]) -> list[str] | None:
    node: object = data
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    if not isinstance(node, list):
        return None
    return [str(x) for x in node]


def main() -> int:
    pyproject_path = ROOT / "pyproject.toml"
    if not pyproject_path.is_file():
        print(f"缺少 {pyproject_path}", file=sys.stderr)
        return 2

    with pyproject_path.open("rb") as fh:
        pyproject = tomllib.load(fh)

    failed = False
    for filename, keys, label in PAIRS:
        req_path = ROOT / filename
        if not req_path.is_file():
            print(f"[跳过] {filename} 不存在")
            continue

        want = read_requirements(req_path)
        got = dig(pyproject, keys)
        if got is None:
            print(f"[失败] pyproject.toml 缺少 {'/'.join(keys)}（{label}）", file=sys.stderr)
            failed = True
            continue

        only_in_req = sorted(set(want) - set(got))
        only_in_pyproject = sorted(set(got) - set(want))

        if not only_in_req and not only_in_pyproject:
            print(f"[通过] {filename} ↔ {'/'.join(keys)}：{len(want)} 条规格逐字一致")
            continue

        failed = True
        print(f"[失败] {filename} ↔ {'/'.join(keys)} 不一致：", file=sys.stderr)
        for spec in only_in_req:
            print(f"  只出现在 {filename}：{spec}", file=sys.stderr)
        for spec in only_in_pyproject:
            print(f"  只出现在 pyproject.toml：{spec}", file=sys.stderr)

    if failed:
        print(
            "\n两处必须一起改。理由见 scripts/check_deps_sync.py 的文件头："
            "依赖清单漂移会产出「CI 绿、安装出的却是另一套版本」这种最危险的组合。",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
