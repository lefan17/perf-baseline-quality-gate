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


def init_db() -> None:
    with tx() as conn:
        conn.executescript(SCHEMA)


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
    with tx() as conn:
        conn.execute(
            "UPDATE runs SET status=?, finished_at=?,"
            " metrics_json=CASE WHEN ? IS NULL THEN metrics_json ELSE ? END"
            " WHERE run_id=?",
            (status, now_iso(), None if metrics is None else 1, json.dumps(metrics or {}, ensure_ascii=False), run_id),
        )


def add_steps(run_id: str, stats_list: list) -> None:
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
    with tx() as conn:
        for c in cases:
            conn.execute(
                "INSERT INTO cases(run_id, nodeid, outcome, duration_s, fail_message, markers)"
                " VALUES (?,?,?,?,?,?)",
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
        row = conn.execute(
            "SELECT * FROM baselines WHERE kind=? AND label=?", (kind, label)
        ).fetchone()
    if row is None:
        return None
    return {
        "run_id": row["run_id"],
        "set_at": row["set_at"],
        "metrics": json.loads(row["metrics_json"]),
    }


def set_baseline(kind: str, label: str, run_id: str, metrics: dict) -> None:
    init_db()
    with tx() as conn:
        conn.execute(
            "INSERT INTO baselines(kind, label, run_id, set_at, metrics_json) VALUES (?,?,?,?,?)"
            " ON CONFLICT(kind, label) DO UPDATE SET run_id=excluded.run_id,"
            " set_at=excluded.set_at, metrics_json=excluded.metrics_json",
            (kind, label, run_id, now_iso(), json.dumps(metrics, ensure_ascii=False)),
        )


def add_gate_results(run_id: str, results: list[dict]) -> None:
    init_db()
    with tx() as conn:
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
                "SELECT run_id, outcome, duration_s, fail_message FROM cases WHERE nodeid=?"
                " ORDER BY id DESC LIMIT ?",
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
