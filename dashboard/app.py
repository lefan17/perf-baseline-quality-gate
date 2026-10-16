"""看板：把入库的度量变成"一眼能看懂"的页面和 API。

## 为什么看板值得单独做

平台的价值在**被使用**。一份只存在于 CI 日志里的性能报告，
读的人是"发现流水线红了才去翻"的；而看板把趋势摆在那里，
问题会在没人踩到之前被看见。这也是`质量度量`这个词的实际意思：
度量只有被看见，才可能影响决策。

页面上刻意不引入任何前端构建链（没有 npm、没有 CDN）：
一个内部看板为了画三条折线而要求装 node_modules，
是它最后没人维护的常见原因。这里用手写 SVG。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from metrics import analysis, gate, store  # noqa: E402

app = FastAPI(title="Quality Dashboard", version="1.0.0")
DEFAULT_RULES = ROOT / "gate_rules.yaml"


def _load_rules(rules: str | None = None) -> dict:
    """加载门禁规则：看板上的所有口径都必须来自同一个文件。

    看板不是"另一套标准"：它显示的方向标、flaky 判据、劣化百分比
    全部和 CLI 门禁读同一份 `gate_rules.yaml`，否则页面和流水线
    会对同一个事实给出两种说法。
    """
    return gate.load_rules(rules or DEFAULT_RULES)


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@app.get("/api/runs")
def api_runs(kind: str | None = None, label: str | None = None, limit: int = 20) -> list[dict]:
    rows = store.recent_runs(kind=kind, label=label, limit=limit)
    out = []
    for r in rows:
        item = dict(r)
        item["metrics"] = _metrics_of(r["run_id"])
        out.append(item)
    return out


@app.get("/api/runs/{run_id}")
def api_run(run_id: str) -> JSONResponse:
    with store.tx() as conn:
        row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        return JSONResponse({"error": "not found", "run_id": run_id}, status_code=404)
    d = dict(row)
    for key in ("config_json", "metrics_json", "platform_json"):
        try:
            d[key.replace("_json", "")] = json.loads(d.pop(key) or "{}")
        except json.JSONDecodeError:
            d[key.replace("_json", "")] = {}
    return JSONResponse(
        {
            "run": d,
            "steps": store.steps_for_run(run_id),
            "gate": _gate_for(run_id),
        }
    )


@app.get("/api/trend")
def api_trend(label: str, limit: int = 20, kind: str | None = None,
              rules: str | None = None) -> dict:
    """趋势序列：给看板画线，也给外部系统取数。

    `kind` 默认不过滤 —— 早先这里写死了 `kind="perf"`，导致
    probe 类型的运行（kind="probe"）在趋势里**永远是空的**：
    压测探针跑了十几轮，看板上却一条线都没有。
    按 label 取数时把 kind 写死是不成立的假设，因为 label 本身
    已经足够定位一组运行；kind 只作为可选的额外过滤条件。
    """
    rows = store.recent_runs(kind=kind, label=label, limit=limit)
    rows = list(reversed(rows))
    points = []
    for r in rows:
        m = _metrics_of(r["run_id"])
        if not m:
            continue
        points.append(
            {
                "run_id": r["run_id"],
                "kind": r["kind"],
                "at": r["started_at"],
                "status": r["status"],
                "p95_ms": m.get("p95_ms"),
                "p99_ms": m.get("p99_ms"),
                "rps": m.get("rps"),
                "error_rate": m.get("error_rate"),
            }
        )
    # 基线的 kind 跟随实际取到的运行类型，而不是写死 perf
    baseline_kind = points[-1]["kind"] if points else (kind or "perf")
    loaded_rules = _load_rules(rules)
    return {
        "label": label,
        "points": points,
        "p95_direction": analysis.moving_direction(
            [p["p95_ms"] for p in points if p["p95_ms"]], rules=loaded_rules
        ),
        "rps_direction": analysis.moving_direction(
            [p["rps"] for p in points if p["rps"]], rules=loaded_rules
        ),
        "tolerance_pct": analysis.tolerance_from_rules(loaded_rules)[0],
        "baseline": store.get_baseline(baseline_kind, label),
    }


@app.get("/gate")
@app.get("/api/gate")
def api_gate(run_id: str, rules: str | None = None) -> dict:
    """门禁 API：CI 可以直接调它拿结构化判定，不用解析 stdout。

    这是"度量平台"和"CI 插件"的区别 —— 判定逻辑留在服务端，
    CI 只负责问一次、拿结论、决定回滚还是合并。
    """
    rows = store.steps_for_run(run_id)
    if not rows:
        return {"run_id": run_id, "error": "no steps", "blocked": True}
    merged = _merge([dict(r) for r in rows])
    with store.tx() as conn:
        row = conn.execute("SELECT kind, label, config_json FROM runs WHERE run_id=?",
                           (run_id,)).fetchone()
    label = row["label"] if row else ""
    run_config = json.loads(row["config_json"] or "{}") if row else {}
    baseline = store.get_baseline("perf", label) if row and row["kind"] in ("perf", "probe") else None
    # rules 只加载一次，并**同时**喂给容忍带与门禁。
    #
    # 这里曾经是 `compare_to_baseline(merged, baseline["metrics"])`（不传 rules）
    # 而下一行 `evaluate_perf(..., gate.load_rules(rules or DEFAULT_RULES))` 传了 ——
    # 于是同一个响应里，"哪些指标算劣化"用模块常量、"拦不拦"用 YAML。
    # 两边当前数值恰好都是 10%，所以看不出问题；但只要有人按业务把
    # `perf.tolerance.degradation_pct` 调成 5%，看板就会出现
    # 「deltas 说在容忍带内、门禁却判拦住」这种自相矛盾的结论。
    # 口径必须单点定义：加载一次，两处共用。
    loaded_rules = _load_rules(rules)
    deltas = analysis.compare_to_baseline(merged, baseline["metrics"], loaded_rules) if baseline else []
    # 可比性也走单点：口径不同时不报劣化百分比，而是把 R3 标成 SKIP。
    comparability = (
        analysis.check_comparability(run_config, baseline.get("config"), loaded_rules)
        if baseline else None
    )
    report = gate.evaluate_perf(merged, baseline, deltas, loaded_rules, comparability)
    return {
        "run_id": run_id,
        "label": label,
        "blocked": report.blocked,
        "metrics": merged,
        "comparability": comparability.__dict__ if comparability else None,
        "deltas": [d.__dict__ for d in deltas],
        "results": [r.as_dict() for r in report.results],
    }


@app.get("/api/cases/flaky")
def api_flaky(rules: str | None = None) -> list[dict]:
    loaded_rules = _load_rules(rules)
    out = []
    for nodeid in store.all_nodeids():
        verdict = analysis.analyze_flaky(store.cases_for_node(nodeid), nodeid, loaded_rules)
        if verdict.runs:
            out.append(verdict.__dict__)
    out.sort(key=lambda v: (not v["flaky"], v["stability"]))
    return out


# --------------------------------------------------------------------------- #
# 页面
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
def page(rules: str | None = None) -> str:
    loaded_rules = _load_rules(rules)
    return _render(
        home=store.recent_runs(limit=25),
        flaky=[
            v
            for v in (
                analysis.analyze_flaky(store.cases_for_node(n), n, loaded_rules)
                for n in store.all_nodeids()
            )
            if v.flaky
        ],
        rules=loaded_rules,
    )


def _metrics_of(run_id: str) -> dict:
    with store.tx() as conn:
        row = conn.execute("SELECT metrics_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        return {}
    try:
        return json.loads(row["metrics_json"] or "{}")
    except json.JSONDecodeError:
        return {}


def _gate_for(run_id: str) -> list[dict]:
    with store.tx() as conn:
        rows = conn.execute(
            "SELECT rule_id, passed, severity, message, observed, expected FROM gate_results"
            " WHERE run_id=? ORDER BY id",
            (run_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def _merge(step_dicts: list[dict]) -> dict:
    """整轮汇总 —— 实现只有一处，在 `metrics.analysis.merge_step_rows`。

    ## 这里曾经是第三份复制粘贴的实现

    它把 RPS 写成了 `sum(r["rps"] for r in step_dicts)`
    （各梯度吞吐相加，凭空翻倍），而 `platform_cli._merge_from_rows`
    已经修过一次同样的错。

    **同一个口径在两个文件里各写一遍，就是在给未来埋一个对不上账的雷。**
    现在统一 import：改口径只需要改 `analysis.py` 一处，CLI、看板、
    门禁必然自洽。
    """
    return analysis.merge_step_rows([dict(r) for r in step_dicts])


def _sparkline(values: list[float], width: int = 160, height: int = 36) -> str:
    """手写 SVG 折线。有值就画，没值就留白 —— 不为了"好看"造数据。"""
    if len(values) < 2:
        return '<span class="muted">样本不足</span>'
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    pts = []
    for i, v in enumerate(values):
        x = i / (len(values) - 1) * (width - 4) + 2
        y = height - 2 - (v - lo) / span * (height - 4)
        pts.append(f"{x:.1f},{y:.1f}")
    return (
        f'<svg width="{width}" height="{height}" role="img">'
        f'<polyline points="{" ".join(pts)}" fill="none" stroke="#2f81f7" stroke-width="1.6"/></svg>'
    )


def _render(home: list[dict], flaky: list, rules: dict | None = None) -> str:
    # rules 缺省时不读文件系统：moving_direction 与下面那句文案都会回落到
    # 模块常量，而模块常量与 YAML 当前默认值一致，渲染结果逐字节相同。
    tol_pct = analysis.tolerance_from_rules(rules)[0]
    rows_html = []
    for r in home:
        m = _metrics_of(r["run_id"])
        badge = "ok" if r["status"] == "passed" else ("bad" if r["status"] == "failed" else "warn")
        rows_html.append(
            f'<tr><td class="mono">{r["run_id"]}</td>'
            f'<td>{r["kind"]}</td><td>{r["label"]}</td>'
            f'<td><span class="badge {badge}">{r["status"]}</span></td>'
            f'<td class="num">{m.get("p95_ms", 0):.1f}</td>'
            f'<td class="num">{m.get("p99_ms", 0):.1f}</td>'
            f'<td class="num">{m.get("rps", 0):.1f}</td>'
            f'<td class="num">{m.get("error_rate", 0):.2%}</td>'
            f'<td class="muted">{r["started_at"][:19]}</td></tr>'
        )

    # 趋势折线：每个 label 画一条 P95 走势。
    #
    # 这里曾经只有 `_sparkline` 函数却从没被调用过 —— 页面上一根线都没有，
    # 而 README 里写着"手写 SVG 折线"。**定义了却没接线**是看板类代码最
    # 常见的空转：函数写好、测试绿色、功能不存在。
    trend_html = []
    seen: set[tuple[str, str]] = set()
    for r in home:
        key = (r["kind"], r["label"])
        if key in seen:
            continue
        seen.add(key)
        series = []
        for prev in reversed(home):
            if (prev["kind"], prev["label"]) != key:
                continue
            pm = _metrics_of(prev["run_id"])
            if pm.get("p95_ms"):
                # 把超过 500ms 的点截断显示：否则一个 10s 的劣化点会把其余
                # 正常的点全压成一条直线，走势反而看不见了。
                series.append(min(float(pm["p95_ms"]), 500.0))
        if len(series) < 2:
            continue
        direction = analysis.moving_direction(series, rules=rules)
        trend_html.append(
            f'<div style="margin:6px 0 14px"><div class="mono" style="margin-bottom:3px">'
            f'{r["kind"]}/{r["label"]} <span class="muted">P95 走势（{len(series)} 轮）· '
            f"{direction}</span></div>"
            f"{_sparkline(series, width=420, height=44)}</div>"
        )
    trend_section = "".join(trend_html) or (
        '<p class="muted">还没有足够的运行历史画趋势（同一个 label 至少 2 轮）。</p>'
    )

    flaky_html = "".join(
        f'<li><span class="mono">{v.nodeid}</span> — {v.passed}P/{v.failed}F，'
        f"稳定度 {v.stability:.0%}，{v.note}</li>"
        for v in flaky
    ) or '<li class="muted">没有达到 flaky 判定的用例（需要 ≥6 次执行且结果摇摆）</li>'

    baselines = []
    with store.tx() as conn:
        for b in conn.execute("SELECT kind, label, run_id, set_at FROM baselines ORDER BY kind, label"):
            bm = json.loads(
                conn.execute(
                    "SELECT metrics_json FROM baselines WHERE kind=? AND label=?",
                    (b["kind"], b["label"]),
                ).fetchone()["metrics_json"]
            )
            baselines.append(
                f'<li><span class="mono">{b["kind"]}/{b["label"]}</span> → {b["run_id"]}，'
                f'P95 {bm.get("p95_ms", 0):.1f}ms，RPS {bm.get("rps", 0):.1f}，'
                f'<span class="muted">set {b["set_at"][:19]}</span></li>'
            )
    baseline_html = "".join(baselines) or (
        '<li class="muted">还没有基线。用 <span class="mono">'
        "platform_cli baseline set --label ...</span> 显式设定。</li>"
    )

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>性能基线 · 质量门禁看板</title>
<style>
 :root {{ color-scheme: light dark; }}
 body {{ font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
        margin: 0; padding: 28px; background:#0d1117; color:#e6edf3; }}
 h1 {{ font-size: 20px; margin: 0 0 4px; }}
 h2 {{ font-size: 15px; margin: 28px 0 10px; color:#9da7b3; font-weight:600;
       text-transform: uppercase; letter-spacing:.06em; }}
 .muted {{ color:#8b949e; }}
 .mono {{ font-family: ui-monospace, Consolas, monospace; font-size: 12px; }}
 table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
 th, td {{ padding: 7px 10px; border-bottom: 1px solid #21262d; text-align: left; }}
 th {{ color:#9da7b3; font-weight:600; font-size: 12px; }}
 td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
 .badge {{ padding:1px 7px; border-radius: 10px; font-size: 11px; }}
 .ok {{ background:#133620; color:#3fb950; }} .bad {{ background:#3d1418; color:#f85149; }}
 .warn {{ background:#3a2d10; color:#d29922; }}
 ul {{ margin: 0; padding-left: 18px; font-size: 13px; line-height: 1.8; }}
 .note {{ background:#161b22; border:1px solid #21262d; border-radius:6px;
          padding:12px 14px; font-size:12.5px; line-height:1.7; color:#9da7b3; }}
 code {{ background:#161b22; padding:1px 5px; border-radius:4px; font-size:12px; }}
</style></head><body>
<h1>性能基线 · 质量门禁看板</h1>
<p class="muted mono">db: {store.db_path()}</p>

<h2>最近运行</h2>
<table><thead><tr><th>run_id</th><th>类型</th><th>标签</th><th>状态</th>
<th>P95(ms)</th><th>P99(ms)</th><th>RPS</th><th>错误率</th><th>开始时间</th></tr></thead>
<tbody>{''.join(rows_html) or '<tr><td colspan="9" class="muted">还没有运行记录</td></tr>'}</tbody></table>

<h2>P95 趋势</h2>
{trend_section}

<h2>当前基线</h2>
<ul>{baseline_html}</ul>

<h2>flaky 用例</h2>
<ul>{flaky_html}</ul>

<h2>接口</h2>
<div class="note">
<code>GET /api/runs?kind=perf&amp;label=...</code> 运行列表 ·
<code>GET /api/runs/&lt;run_id&gt;</code> 单轮明细（含各梯度与门禁结果）<br>
<code>GET /api/trend?label=...</code> 趋势序列与方向 ·
<code>GET /gate?run_id=...</code> 门禁判定（CI 直接调）<br>
<code>GET /api/cases/flaky</code> flaky 清单
</div>

<h2>看板在看什么</h2>
<div class="note">
P95 比平均值值得看：平均值会被少数极快的请求拉平，而用户抱怨的永远是长尾。
RPS 掉、P95 涨、错误率起 —— 这三件事同时出现才是"容量到顶"，
单独一项抖动通常是噪声（本平台的口径里 {tol_pct:.0%} 以内算持平）。
</div>
</body></html>"""
