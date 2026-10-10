"""CLI 冒烟测试 —— 保证每个子命令"接线"是通的。

## 这个文件为什么存在

`platform_cli.py` 里曾经出现过一个真实的低级错误：插入新函数时，
`def cmd_gate(args):` 这一行被覆盖掉了，函数体变成了一段悬空代码
（`NameError: name 'cmd_gate' is not defined`）。

**这个错误能通过当时全部 84 条测试**，因为那些用例测的全是
`metrics/gate.py` 和 `metrics/analysis.py` 里的纯逻辑 —— 没有一条
真正去构建 argparse 解析器。于是"整个 CLI 起不来"这种最严重的故障
在测试里是不可见的。

## 教训

纯函数测试覆盖率再高，也覆盖不到"模块能不能被导入"、
"子命令有没有接上函数"这类**接线问题**。
这类问题不需要复杂设计，一条便宜的结构化用例就能挡住 ——
它检查的是"每个子命令都指向一个真实可调用的函数"，
而不是任何业务逻辑。

所以这里刻意**不测业务逻辑**（那些在上面的文件里测过了），
只测：能导入、能建解析器、每个子命令的 func 真的存在、
`--help` 能正常渲染。
"""

from __future__ import annotations

import argparse

import pytest

import platform_cli


class TestParserWiring:
    """每个子命令都必须指向一个真实存在的可调用对象。"""

    def test_parser_builds(self):
        """构建解析器本身不能抛异常 —— 这是最初那个 NameError 的直接克星。"""
        parser = platform_cli.build_parser()
        assert isinstance(parser, argparse.ArgumentParser)

    def test_no_subcommand_has_broken_func(self):
        """遍历所有子命令，确认 func 已绑定且可调用。

        这与 `test_parser_builds` 是两道不同的关卡：前者会发现
        "构建时引用了不存在的名字"，后者会发现"某个子命令的 func
        压根没设上"（set_defaults 漏了一行）。
        """
        parser = platform_cli.build_parser()
        subs = [
            a for a in parser._actions
            if isinstance(a, argparse._SubParsersAction)
        ]
        assert subs, "解析器必须注册子命令"

        broken = []
        for action in subs:
            for name in action.choices:
                func = getattr(action.choices[name], "_defaults", {}).get("func")
                if not callable(func):
                    broken.append(name)
        assert not broken, f"这些子命令没有绑定可调用的 func：{broken}"

    def test_expected_subcommands_exist(self):
        """子命令集合是稳定的契约 —— CI 脚本按名字调用它们。"""
        parser = platform_cli.build_parser()
        action = next(
            a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
        )
        names = set(action.choices)
        expected = {"run", "probe", "baseline", "trend", "gate", "suite", "flaky", "noise", "serve"}
        missing = expected - names
        assert not missing, f"缺少子命令：{missing}"

    @pytest.mark.parametrize(
        "name",
        ["run", "probe", "baseline", "trend", "gate", "suite", "flaky", "noise", "serve"],
    )
    def test_help_renders_for_each_subcommand(self, name, capsys):
        """每个子命令的 --help 都要能正常渲染。

        渲染 --help 会走一遍该子命令的全部参数定义，
        能抓出"参数名笔误"、"help 文本里混了非法字符"这类问题。
        """
        parser = platform_cli.build_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args([name, "--help"])
        assert exc.value.code == 0
        assert name in capsys.readouterr().out


class TestHelpers:
    """几个被 CLI 依赖的纯函数 —— 它们是拼接进命令行/报告的，容易出错。"""

    def test_new_run_id_is_prefixed_and_unique(self):
        a = platform_cli.new_run_id("perf")
        b = platform_cli.new_run_id("perf")
        assert a.startswith("perf-")
        assert a != b, "同一秒内的两次调用也必须不同（否则会覆盖入库记录）"

    def test_merge_from_rows_takes_worst_step_percentile(self):
        """合并多梯度时，分位数取最差的一级。

        这是刻意的保守设计：原始样本没有入库，无法重算真实分位数，
        所以宁可取最差梯度 —— 对门禁而言是安全方向，
        不会因为合并方式把劣化抹平。
        """
        rows = [
            {"total": 100, "error_count": 0, "rps": 50.0, "mean_ms": 10.0,
             "p50_ms": 8.0, "p90_ms": 12.0, "p95_ms": 15.0, "p99_ms": 20.0, "max_ms": 30.0},
            {"total": 300, "error_count": 3, "rps": 40.0, "mean_ms": 20.0,
             "p50_ms": 18.0, "p90_ms": 30.0, "p95_ms": 90.0, "p99_ms": 120.0, "max_ms": 200.0},
        ]
        merged = platform_cli._merge_from_rows(rows)
        assert merged["total"] == 400
        assert merged["p95_ms"] == 90.0, "必须取最差梯度，而不是平均或最优"
        assert merged["p99_ms"] == 120.0
        assert merged["max_ms"] == 200.0
        # mean 按样本量加权，不是简单平均
        assert merged["mean_ms"] == pytest.approx((10.0 * 100 + 20.0 * 300) / 400)
        assert merged["error_rate"] == pytest.approx(3 / 400)

    def test_merge_from_rows_handles_empty(self):
        """空输入不能除零崩溃 —— 门禁跑到一个没有步骤的 run 时会走到这里。"""
        merged = platform_cli._merge_from_rows([])
        assert merged["total"] == 0
        assert merged["error_rate"] == 0.0
        assert merged["p95_ms"] == 0

    def test_merge_from_rows_does_not_sum_rps(self):
        """RPS 必须是"总请求数 / 总时长"，不能把各梯度的吞吐相加。

        这里原先写的是 `sum(r["rps"] for r in rows)`，两梯度各约 370 RPS
        的同一轮汇总成 740，凭空翻倍。

        它之所以能活很久，是因为门禁只在 RPS **下跌**时判劣化，
        而"凭空翻倍"方向朝上，正好躲过检查 —— 又一个静默的错误。
        只有把多轮趋势放在一起看，才会发现某轮吞吐高得没道理。

        注意：round 那一行同时也在断言**被加数确实不同**（370 和 200），
        否则这条用例即使实现是错的也会通过（两数相同则相加=平均）。
        """
        rows = [
            {"total": 1000, "duration_s": 10.0, "error_count": 0, "rps": 370.0,
             "mean_ms": 10.0, "p50_ms": 8.0, "p90_ms": 12.0, "p95_ms": 15.0,
             "p99_ms": 20.0, "max_ms": 30.0},
            {"total": 500, "duration_s": 5.0, "error_count": 0, "rps": 200.0,
             "mean_ms": 20.0, "p50_ms": 18.0, "p90_ms": 30.0, "p95_ms": 90.0,
             "p99_ms": 120.0, "max_ms": 200.0},
        ]
        assert rows[0]["rps"] != rows[1]["rps"], "被加数必须不同，否则测不出相加的错误"
        merged = platform_cli._merge_from_rows(rows)
        assert merged["rps"] == pytest.approx(1500 / 15.0)   # = 100.0
        assert merged["rps"] != pytest.approx(570.0), "各梯度 RPS 被相加了"

    def test_merge_from_rows_keeps_worst_percentile_and_duration(self):
        """分位数取最差一级；时长累加（RPS 的分母）。"""
        rows = [
            {"total": 100, "duration_s": 4.0, "error_count": 0, "rps": 25.0,
             "mean_ms": 10.0, "p50_ms": 8.0, "p90_ms": 12.0, "p95_ms": 15.0,
             "p99_ms": 20.0, "max_ms": 30.0, "max_inflight": 8},
            {"total": 300, "duration_s": 6.0, "error_count": 0, "rps": 50.0,
             "mean_ms": 20.0, "p50_ms": 18.0, "p90_ms": 30.0, "p95_ms": 90.0,
             "p99_ms": 120.0, "max_ms": 200.0, "max_inflight": 19},
        ]
        merged = platform_cli._merge_from_rows(rows)
        assert merged["duration_s"] == pytest.approx(10.0)
        assert merged["p95_ms"] == 90.0
        assert merged["max_inflight"] == 19, "在途取最大：回答压测机有没有成为瓶颈"
