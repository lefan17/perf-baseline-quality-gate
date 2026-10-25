"""结果库（平台自己的数据库，和被压的服务无关）。

## 一条会红的纪律：基线不许被"顺手更新"

基线表 `baselines` 只由 `--set-baseline` 显式写入。跑一次压测不会
自动把基线刷成最新值 —— 否则"劣化检测"永远显示绿色，
这正是很多团队的性能门禁最后没人信的原因：**它自己会跟着劣化一起长大**。

## 另一条：失败也要入库

`runs.status != 'passed'` 的记录照样落库。只存成功记录的度量平台，
给出的通过率是假的（分母被美化过）。
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "quality.db"
DB_ENV = "QUALITY_DB"


def db_path() -> Path:
    raw = os.environ.get(DB_ENV)
    return Path(raw) if raw else DEFAULT_DB


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id          TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,            -- perf | suite | flaky | gate
    label           TEXT NOT NULL,
    git_sha         TEXT,
    ci              INTEGER NOT NULL DEFAULT 0,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    status          TEXT NOT NULL DEFAULT 'running',
    config_json     TEXT NOT NULL DEFAULT '{}',
    metrics_json    TEXT NOT NULL DEFAULT '{}',
    platform_json   TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_runs_kind_label ON runs(kind, label, started_at);

CREATE TABLE IF NOT EXISTS steps (
    id          INTEGER PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    users       INTEGER NOT NULL DEFAULT 0,
    duration_s  REAL NOT NULL DEFAULT 0,
    total       INTEGER NOT NULL DEFAULT 0,
    ok_count    INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    error_rate  REAL NOT NULL DEFAULT 0,
    rps         REAL NOT NULL DEFAULT 0,
    mean_ms     REAL NOT NULL DEFAULT 0,
    p50_ms      REAL NOT NULL DEFAULT 0,
    p90_ms      REAL NOT NULL DEFAULT 0,
    p95_ms      REAL NOT NULL DEFAULT 0,
    p99_ms      REAL NOT NULL DEFAULT 0,
    max_ms      REAL NOT NULL DEFAULT 0,
    max_inflight INTEGER NOT NULL DEFAULT 0,
    breakdown_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_steps_run ON steps(run_id);

CREATE TABLE IF NOT EXISTS cases (
    id           INTEGER PRIMARY KEY,
    run_id       TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    nodeid       TEXT NOT NULL,
    outcome      TEXT NOT NULL,               -- passed | failed | error | skipped
    duration_s   REAL NOT NULL DEFAULT 0,
    fail_message TEXT,
    markers      TEXT
);
CREATE INDEX IF NOT EXISTS idx_cases_nodeid ON cases(nodeid);

CREATE TABLE IF NOT EXISTS baselines (
    kind        TEXT NOT NULL,
    label       TEXT NOT NULL,
    run_id      TEXT NOT NULL,
    set_at      TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    -- 建立基线那一刻的**被测配置**快照（items / report_mode / step_users）。
    -- 没有它就分不清"变慢了"和"测的不是一回事"：实测出现过 baseline
    -- users=20 items=5254 与 current users=80 items=900 直接比出
    -- "p95 +40.0% 劣化"—— 两轮根本不可比。老库由 `_migrate()` 补列。
    config_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (kind, label)
);

CREATE TABLE IF NOT EXISTS gate_results (
    id          INTEGER PRIMARY KEY,
    run_id      TEXT NOT NULL,
    rule_id     TEXT NOT NULL,
    passed      INTEGER NOT NULL,
    severity    TEXT NOT NULL,
    observed    TEXT,
    expected    TEXT,
    message     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gate_run ON gate_results(run_id);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    p = db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def tx():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _add_baseline_config_column(conn) -> None:
    """给 `baselines` 补 `config_json` 列（幂等）。

    `CREATE TABLE IF NOT EXISTS` 对**已存在**的表什么都不做，所以老库必须靠
    `ALTER TABLE ... ADD COLUMN` 才能拿到新列 —— 否则升级后第一次 `set_baseline`
    会直接报 "no such column"。缺列时补上，已有则不动，重复调用无副作用。
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(baselines)")}
    if "config_json" not in cols:
        conn.execute("ALTER TABLE baselines ADD COLUMN config_json TEXT NOT NULL DEFAULT '{}'")


def init_db() -> None:
    with tx() as conn:
        conn.executescript(SCHEMA)
        _add_baseline_config_column(conn)


# --------------------------------------------------------------------------- #
def upsert_run(
    run_id: str,
    kind: str,
    label: str,
    status: str = "running",
    config: dict | None = None,
    metrics: dict | None = None,
    platform_info: dict | None = None,
    git_sha: str | None = None,
    ci: bool = False,
) -> None:
    init_db()
    started = now_iso()
    with tx() as conn:
        conn.execute(
            "INSERT INTO runs(run_id, kind, label, git_sha, ci, started_at, status, config_json, metrics_json, platform_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(run_id) DO UPDATE SET"
            "   status=excluded.status, metrics_json=excluded.metrics_json,"
            "   finished_at=CASE WHEN excluded.status='running' THEN NULL ELSE ? END",
            (
                run_id,
                kind,
                label,
                git_sha,
                1 if ci else 0,
                started,
                status,
                json.dumps(config or {}, ensure_ascii=False),
                json.dumps(metrics or {}, ensure_ascii=False),
                json.dumps(platform_info or {}, ensure_ascii=False),
                now_iso(),
            ),
        )


def finish_run(run_id: str, status: str, metrics: dict | None = None) -> None:
    init_db()
    with tx() as conn:
        conn.execute(
            "UPDATE runs SET status=?, finished_at=?,"
            " metrics_json=CASE WHEN ? IS NULL THEN metrics_json ELSE ? END"
            " WHERE run_id=?",
            (status, now_iso(), None if metrics is None else 1, json.dumps(metrics or {}, ensure_ascii=False), run_id),
        )


def add_steps(run_id: str, stats_list: list) -> None:
    init_db()
    with tx() as conn:
        for st in stats_list:
            d = st.as_dict() if hasattr(st, "as_dict") else dict(st)
            conn.execute(
                "INSERT INTO steps(run_id, name, users, duration_s, total, ok_count, error_count,"
                " error_rate, rps, mean_ms, p50_ms, p90_ms, p95_ms, p99_ms, max_ms, max_inflight, breakdown_json)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    d.get("label", ""),
                    d.get("users", 0),
                    d.get("duration_s", 0),
                    d.get("total", 0),
                    d.get("ok_count", 0),
                    d.get("error_count", 0),
                    d.get("error_rate", 0),
                    d.get("rps", 0),
                    d.get("mean_ms", 0),
                    d.get("p50_ms", 0),
                    d.get("p90_ms", 0),
                    d.get("p95_ms", 0),
                    d.get("p99_ms", 0),
                    d.get("max_ms", 0),
                    d.get("max_inflight", 0),
                    json.dumps(
                        {
                            "status": d.get("status_breakdown", {}),
                            "error": d.get("error_breakdown", {}),
                        },
                        ensure_ascii=False,
                    ),
                ),
            )


def add_cases(run_id: str, cases: list[dict]) -> None:
    init_db()
    with tx() as conn:
        for c in cases:
            conn.execute(
                "INSERT INTO cases(run_id, nodeid, outcome, duration_s, fail_message, markers) VALUES (?,?,?,?,?,?)",
                (
                    run_id,
                    c.get("nodeid", ""),
                    c.get("outcome", ""),
                    float(c.get("duration") or 0.0),
                    (c.get("fail_message") or "")[:2000],
                    c.get("markers", ""),
                ),
            )


def get_baseline(kind: str, label: str) -> dict | None:
    init_db()
    with tx() as conn:
        row = conn.execute("SELECT * FROM baselines WHERE kind=? AND label=?", (kind, label)).fetchone()
    if row is None:
        return None
    return {
        "run_id": row["run_id"],
        "set_at": row["set_at"],
        "metrics": json.loads(row["metrics_json"]),
        # 老库/手工插入的行可能是 '{}'，统一读成空 dict：
        # "没有配置记录"和"配置是空的"对可比性校验是同一种处境。
        "config": json.loads(row["config_json"] or "{}"),
    }


def set_baseline(kind: str, label: str, run_id: str, metrics: dict, config: dict | None = None) -> None:
    """设定基线。`config` 是被测配置快照（items / report_mode / step_users）。

    不带 `config` 的老调用方式仍然可用（写空快照），但那意味着**这条基线没法
    参与可比性校验** —— 校验会以 `skipped` 报"无法判定"，而不是默默放行。
    """
    init_db()
    with tx() as conn:
        conn.execute(
            "INSERT INTO baselines(kind, label, run_id, set_at, metrics_json, config_json)"
            " VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(kind, label) DO UPDATE SET run_id=excluded.run_id,"
            " set_at=excluded.set_at, metrics_json=excluded.metrics_json,"
            " config_json=excluded.config_json",
            (
                kind,
                label,
                run_id,
                now_iso(),
                json.dumps(metrics, ensure_ascii=False),
                json.dumps(config or {}, ensure_ascii=False),
            ),
        )


def add_gate_results(run_id: str, results: list[dict]) -> None:
    """把一轮的门禁判定写进 `gate_results`。**同一 run 的旧结论先删掉。**

    曾经这里只有 `INSERT`。后果不是抽象的：`cmd_gate` 的用途恰恰是
    「改完阈值回头复判同一轮」，复判几次就叠几倍行。实测对同一个 run
    连跑 3 次 `gate --run-id`，五条规则各 3 行，共 15 行；而
    `dashboard.app._gate_for` 是 `ORDER BY id` 全取 → `/api/run` 返回
    15 条结果而不是 5 条，页面上同一规则重复出现。

    为什么选"先删后插"而不是"按 (run_id, rule_id) UPSERT"：
    `gate_results` 没有 `(run_id, rule_id)` 唯一索引，且规则集会随代码
    增删（比如 R3 某次不再发射），UPSERT 会把**上一版才有的规则**留成
    幽灵行。整批替换的语义才和"这一轮的判定"对得上 —— 判定是一份完整
    快照，不是增量追加。
    """
    init_db()
    with tx() as conn:
        conn.execute("DELETE FROM gate_results WHERE run_id=?", (run_id,))
        for r in results:
            conn.execute(
                "INSERT INTO gate_results(run_id, rule_id, passed, severity, observed, expected, message, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    r.get("rule_id", ""),
                    1 if r.get("passed") else 0,
                    r.get("severity", "blocking"),
                    str(r.get("observed", "")),
                    str(r.get("expected", "")),
                    r.get("message", ""),
                    now_iso(),
                ),
            )


def recent_runs(kind: str | None = None, label: str | None = None, limit: int = 20) -> list[dict]:
    init_db()
    sql = "SELECT run_id, kind, label, status, started_at, finished_at, ci FROM runs WHERE 1=1"
    params: list = []
    if kind:
        sql += " AND kind=?"
        params.append(kind)
    if label:
        sql += " AND label=?"
        params.append(label)
    sql += " ORDER BY started_at DESC LIMIT ?"
    params.append(limit)
    with tx() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def steps_for_run(run_id: str) -> list[dict]:
    init_db()
    with tx() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM steps WHERE run_id=?", (run_id,)).fetchall()]


def cases_for_node(nodeid: str, limit: int = 50) -> list[dict]:
    """某条用例的历史执行记录 —— flaky 识别的原料。"""
    init_db()
    with tx() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT run_id, outcome, duration_s, fail_message FROM cases WHERE nodeid=? ORDER BY id DESC LIMIT ?",
                (nodeid, limit),
            ).fetchall()
        ]


def all_nodeids(limit: int = 5000) -> list[str]:
    init_db()
    with tx() as conn:
        return [
            r["nodeid"]
            for r in conn.execute(
                "SELECT nodeid FROM cases GROUP BY nodeid ORDER BY nodeid LIMIT ?", (limit,)
            ).fetchall()
        ]


def _common_columns(conn: sqlite3.Connection, table: str, *, skip_id: bool) -> list[str]:
    """`main` 与 `src` 两张同名表都有的列。

    为什么不用写死的列名：`src` 可能是**旧版本**生成的库（gh-pages 上
    上一轮留下的那份），而 schema 是会长列的 —— `baselines.config_json`
    就是这么加进来的。写死列名会让合并在升级后直接崩，
    而"取交集"能让新旧库都能合：缺的列取目标表默认值。

    `id` 必须排除：它是 `INTEGER PRIMARY KEY`（rowid 别名），
    把源库的 id 原样搬过来会在自增序列上撞车。
    """
    dst = [r["name"] for r in conn.execute(f"PRAGMA main.table_info({table})")]
    src = {r["name"] for r in conn.execute(f"PRAGMA src.table_info({table})")}
    return [c for c in dst if c in src and not (skip_id and c == "id")]


def merge_from(src: Path | str) -> dict:
    """把另一个结果库里的**历史轮次**并入当前库。

    ## 为什么需要它

    CI 的每一步都是全新检出，`quality.db` 里只有**本轮**那几行。于是
    在 runner 上导出看板时，趋势图永远只有一个点 —— 一个画不出趋势的
    趋势图，等于没有。要画出"慢慢变慢"的形状，必须把历史带过来。

    ## 合并规则

    * **只补本地没有的轮次**（按 `run_id`）。同名轮次以本地为准：
      本地那份是这一轮刚从 runner 上量出来的，源库那份是旧的。
    * **基线不动**。`baselines` 的权威副本在仓库里（`baselines/*.json`
      快照），库里的基线是给人看的缓存；合并把它覆盖掉，等于让
      "上一轮的基线"悄悄替换掉"这一轮的基线"。
    * 缺列时取目标表默认值，所以新旧库可以互合。

    返回各表实际写入的行数，供调用方打日志（合并 0 行和合并 20 轮
    是完全不同的两件事，日志里必须能区分）。
    """
    src_path = Path(src)
    if not src_path.is_file():
        raise FileNotFoundError(f"没有这个库：{src_path}")

    init_db()
    counts = {"runs": 0, "steps": 0, "cases": 0, "gate_results": 0}
    with tx() as conn:
        conn.execute("ATTACH DATABASE ? AS src", (str(src_path),))
        try:
            # 只挑本地没有的轮次，避免把本地刚测出来的数据覆盖成旧值。
            new_ids = [
                r["run_id"]
                for r in conn.execute(
                    "SELECT run_id FROM src.runs WHERE run_id NOT IN (SELECT run_id FROM main.runs)"
                ).fetchall()
            ]
            if not new_ids:
                # 没有新轮次是**正常结果**（上一轮已经合过），不是错误。
                # 先 commit 再往下走：附加库上还挂着事务时 DETACH 会报
                # `database src is locked`。
                conn.commit()
                placeholders = None
            else:
                placeholders = ",".join("?" * len(new_ids))

            if placeholders is not None:
                run_cols = _common_columns(conn, "runs", skip_id=True)
                conn.execute(
                    f"INSERT OR IGNORE INTO main.runs ({','.join(run_cols)})"
                    f" SELECT {','.join(run_cols)} FROM src.runs WHERE run_id IN ({placeholders})",
                    new_ids,
                )
                counts["runs"] = conn.execute(
                    f"SELECT COUNT(*) FROM main.runs WHERE run_id IN ({placeholders})", new_ids
                ).fetchone()[0]

                for table in ("steps", "cases", "gate_results"):
                    cols = _common_columns(conn, table, skip_id=True)
                    if not cols:
                        continue
                    before = conn.total_changes
                    conn.execute(
                        f"INSERT INTO main.{table} ({','.join(cols)})"
                        f" SELECT {','.join(cols)} FROM src.{table} WHERE run_id IN ({placeholders})",
                        new_ids,
                    )
                    counts[table] = conn.total_changes - before
                # DETACH 必须在事务结束之后：日志模式下分离一个正处于写事务中的
                # 附加库会报 `database src is locked`。先 commit 落盘再分离。
                conn.commit()
        finally:
            conn.execute("DETACH DATABASE src")
    return counts
