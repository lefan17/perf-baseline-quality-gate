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


@pytest.fixture
def sut_items() -> int:
    """压测类用例用小数据量：目的是验证链路，不是测性能上限。

    真实验收数字由 `platform_cli run --items 20000` 单独跑并写进 docs/，
    不放在单测里 —— 单测要求"稳定复现"，性能数字天然不满足这个前提。
    """
    return 800
