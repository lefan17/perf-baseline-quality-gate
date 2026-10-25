"""平台自身测试的公共装置。

## 最重要的一条：测试绝不碰开发者的结果库

`QUALITY_DB` 在每个用例里指向 pytest 的临时目录。没有这一条，
"跑一次单测"就会往你自己的 `quality.db` 里灌几十条假运行记录，
把看板上的趋势线彻底弄脏 —— 而且没人会立刻发现。

这类"测试污染了真实数据"的事故，在度量平台上比在生产代码上更隐蔽：
生产代码写坏数据会被业务发现，度量平台写坏数据只会让人觉得"最近怎么有点抖"。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def isolated_quality_db(tmp_path, monkeypatch):
    """每个用例一个独立的结果库。"""
    monkeypatch.setenv("QUALITY_DB", str(tmp_path / "quality.db"))


# 这里原先还有一个 `sut_items` fixture（返回 800，本意是"压测类用例用小数据量"）。
# 全仓 grep 只有它自己的定义那一行 —— **一个从未被任何用例引用的 fixture**。
# 已删除。它不像普通死代码那样无害：它读起来像是在说"仓库里的压测用例
# 统一用 800 条数据"，而实际上各用例各自决定数据量，没有一个跟着它走。
# 想改数据量的人会先改这里，然后发现毫无作用。
