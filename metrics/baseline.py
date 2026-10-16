"""基线快照文件：让"人显式设的基线"能离开本机、被 CI 读到。

## 为什么需要这个模块

`baselines` 表是本机 SQLite 里的一行。CI 是干净环境 —— runner 上那个
`quality.db` 是它自己从零建的，**拿不到人在这台机器上设的基线**。
于是流水线上的 R3 一直是「未设置基线，跳过劣化对比」：
门禁的核心价值（抓住"慢慢变慢"）在 CI 里从未生效过。

## 为什么不用 artifact，也不用存储分支

* **artifact 是死的**：`quality-db-and-reports` 由**同一个 run 自己**
  的 `platform-tests` job 上传（`if: always()`）。跨 job 下载它，拿到的
  是本轮刚建的库，`baselines` 表恒空 —— 这正是原来那个缺陷。
* **存储分支要写权限**：workflow 目前是 `permissions: contents: read`。
  要往分支里写，就得给 CI `contents: write`，等于给它一个"改基线"的能力 ——
  而本仓库的硬纪律是**基线只能由人显式设置**
  （见 `quality-gate.yml` 的「恢复基线」步注释）。

快照文件走普通提交：**谁、在哪次提交里动了基线，`git log` 与
`git blame` 都能回答**。这是"显式"的最强形式，而且不需要任何
secret、不需要任何额外权限。

## 文件格式

```json
{"schema": 1, "kind": "perf", "label": "ci-perf", "run_id": "perf-...",
 "set_at": "2026-10-16T14:47:09+00:00", "metrics": {...}, "config": {...},
 "platform": {"platform": "Windows-...", "cpu_count": 8, ...}}
```

`schema` 是版本号。将来改格式时**必须拒绝不认识的版本**，而不是猜着读 ——
一条被误读的基线会安静地比出假的"没劣化"。

## `platform` 是如实记录，不是可比性判据

快照里记了基线是在哪台机器上量的。**它不参与 `check_comparability`**：
`README` 自己写过"换机器后绝对数字全线不同"（runner 上 10 并发 P95 70.62ms /
本机 79.99ms）。如果拿机器不同就把 R3 判成不可比，这条规则在 CI 上会永远
是 `[SKIP]` —— 那正是本次要修的病。

所以机器的处理方式是**印出来给人看**（`baseline import` 会打印它），
由人决定要不要重设基线；而不是替人下结论。
"""

from __future__ import annotations

import json
from pathlib import Path

from metrics import store

SCHEMA_VERSION = 1


class SnapshotError(ValueError):
    """快照不可用：文件不在、不是 JSON、版本不认识、必填字段缺失。"""


def snapshot_of(kind: str, label: str, platform_info: dict | None = None) -> dict | None:
    """把库里那条基线读成快照 dict。没有基线时返回 None。"""
    b = store.get_baseline(kind, label)
    if b is None:
        return None
    return {
        "schema": SCHEMA_VERSION,
        "kind": kind,
        "label": label,
        "run_id": b["run_id"],
        "set_at": b["set_at"],
        "metrics": b["metrics"],
        "config": b["config"],
        "platform": platform_info or {},
    }


def dump(snap: dict) -> str:
    """序列化成给人读、给 git diff 看的 JSON（末尾带换行）。"""
    return json.dumps(snap, ensure_ascii=False, indent=2) + "\n"


def parse(text: str) -> dict:
    """解析并校验快照文本。任何一处不对都抛 `SnapshotError`。"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SnapshotError(f"不是合法的 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise SnapshotError("顶层必须是一个 JSON 对象")
    schema = data.get("schema")
    if schema != SCHEMA_VERSION:
        raise SnapshotError(f"快照格式版本不认识：schema={schema!r}（本工具只认 {SCHEMA_VERSION}）；不要猜着读一条基线")
    for key in ("kind", "label", "metrics", "config"):
        if key not in data:
            raise SnapshotError(f"缺少字段：{key}")
    if not isinstance(data["label"], str) or not data["label"]:
        raise SnapshotError("label 必须是非空字符串")
    if not isinstance(data["metrics"], dict) or not data["metrics"]:
        raise SnapshotError("metrics 必须是非空对象（没有指标的快照当不了基线）")
    if not isinstance(data["config"], dict):
        raise SnapshotError("config 必须是对象（可以是空对象，但写了就要能读）")
    if "platform" in data and not isinstance(data["platform"], dict):
        raise SnapshotError("platform 必须是对象（记录基线是在哪台机器上量的）")
    return data


def load_file(path: Path) -> dict:
    """读快照文件。文件不存在与内容不对同样是 `SnapshotError`（都由调用方如实报错）。"""
    if not path.is_file():
        raise SnapshotError(f"找不到快照文件：{path}")
    return parse(path.read_text(encoding="utf-8"))


def apply(snap: dict, expect_label: str | None = None, kind: str = "perf") -> dict:
    """把快照写进本机 `baselines` 表，返回写入的那份快照。

    `expect_label` 非空时要求快照的 label 与它一致 —— 防止"把 A 配置的
    基线导进 B label"，那种错会以"配置不可比"的形式出现在很久以后。
    """
    if snap["kind"] != kind:
        raise SnapshotError(f"快照的 kind={snap['kind']!r} 与本命令处理的 kind={kind!r} 不一致")
    if expect_label is not None and snap["label"] != expect_label:
        raise SnapshotError(f"快照是为 label={snap['label']!r} 建的，与 --label {expect_label!r} 不一致")
    store.set_baseline(
        snap["kind"],
        snap["label"],
        str(snap.get("run_id") or "snapshot"),
        snap["metrics"],
        config=snap["config"],
    )
    return snap
