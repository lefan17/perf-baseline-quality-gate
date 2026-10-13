"""提交信息规范检查（Conventional Commits 的轻量实现）。

## 为什么是 Python 而不是 commitlint

仓库里同时给了 `commitlint.config.mjs`，两者规则一致，但**默认走这一条**：

1. 这个仓库是纯 Python 项目（无 `package.json`、无前端构建链），
   为了一个提交信息检查引入整条 Node 工具链，是把「可复现」的成本
   从 Python 一项变成 Python + Node 两项。
2. 本项目的 CI 是三个 Python job，没有一个装 Node。
   在 CI 里为一个普通钩子单独装 Node，会让「跑一次门禁」的依赖面变大。
3. 这条规则本身很短（正则 + 类型白名单）。能在 60 行里说清的东西，
   不值得多一个运行时。

Node 路线保留给「团队本来就有 Node」的场景：`npx commitlint --edit <file>`，
配置见 `commitlint.config.mjs`。两条路线的**规则必须保持等价**。

## 用法

    python scripts/check_commit_msg.py .git/COMMIT_EDITMSG

作为 pre-commit 的 `commit-msg` 钩子运行；也可直接喂一个消息文件。
退出码 0 = 通过，1 = 不通过。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Conventional Commits 的类型白名单。与 commitlint.config.mjs 的 type-enum 一一对应。
TYPES = (
    "feat", "fix", "docs", "style", "refactor",
    "perf", "test", "build", "ci", "chore", "revert",
)

# type(scope)!: subject
# scope 不允许含括号；`!` 表示破坏性变更。
HEADER_RE = re.compile(
    r"^(?P<type>[a-z]+)"
    r"(?:\((?P<scope>[^()\s]+)\))?"
    r"(?P<breaking>!)?"
    r": (?P<subject>.+)$"
)

# 不检查的消息：这些是 git 自己生成的，人不该被一条钩子拦住。
SKIP_PREFIXES = (
    "Merge ",
    "Revert ",
    "fixup! ",
    "squash! ",
    "amend! ",
)

MAX_HEADER_LEN = 100
# 结尾句号：中英文都拦。Conventional Commits 约定标题不是句子。
BAD_SUBJECT_TAILS = (".", "。", "！", "!", "；", ";", "，", ",")


def strip_comments(text: str) -> str:
    """丢掉 git 的注释行与尾部空行，只留真正的内容。"""
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def check(message: str) -> list[str]:
    """返回问题清单；空列表表示通过。"""
    body = strip_comments(message)
    if not body.strip():
        return ["提交信息为空。"]

    lines = body.splitlines()
    header = lines[0].rstrip()

    if header.startswith(SKIP_PREFIXES):
        return []

    problems: list[str] = []

    if len(header) > MAX_HEADER_LEN:
        problems.append(
            f"标题过长：{len(header)} 字符（上限 {MAX_HEADER_LEN}）。"
            "过长的标题在 git log --oneline 与 GitHub 列表里都会被截断。"
        )

    m = HEADER_RE.match(header)
    if m is None:
        problems.append(
            "标题不符合 `<type>(<scope>): <subject>` 格式。"
            "例如 `fix(metrics): 修分位数合并口径`。"
        )
        return problems

    if m.group("type") not in TYPES:
        problems.append(
            f"未知的 type：`{m.group('type')}`。"
            f"只允许：{', '.join(TYPES)}。"
        )

    subject = m.group("subject").strip()
    if not subject:
        problems.append("subject 不能为空。")
    elif subject.endswith(BAD_SUBJECT_TAILS):
        problems.append(
            f"subject 不应以标点结尾：`{subject}`。"
            "标题是列表项不是句子，结尾标点会在 log 里显得很吵。"
        )
    elif subject[0].isupper() and subject.isascii():
        # 只对纯 ASCII 标题提醒首字母大写：中文没有大小写概念。
        problems.append(
            f"subject 应以小写开头（当前 `{subject}`）。"
            "Conventional Commits 的 subject 是短语不是句子。"
        )

    if len(lines) > 1 and lines[1].strip():
        problems.append(
            "标题与正文之间必须空一行（git 靠这个空行区分 subject 与 body）。"
        )

    return problems


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("用法：python scripts/check_commit_msg.py <提交信息文件路径>", file=sys.stderr)
        return 2

    path = Path(argv[1])
    if not path.is_file():
        print(f"读不到提交信息文件：{path}", file=sys.stderr)
        return 2

    problems = check(path.read_text(encoding="utf-8", errors="replace"))
    if not problems:
        return 0

    print("提交信息不符合规范：", file=sys.stderr)
    for p in problems:
        print(f"  - {p}", file=sys.stderr)
    print(
        "\n规范见 CONTRIBUTING.md「提交信息规范」；"
        "本检查是同名 Node 配置 commitlint.config.mjs 的等价实现。",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
