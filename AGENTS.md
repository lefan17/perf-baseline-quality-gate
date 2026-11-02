# AGENTS.md · perf-baseline-quality-gate

## 这是什么

一个 **性能基线与质量门禁平台**。不是"跑一次压测出个报告"，而是把
「基线 → 劣化判定 → 拦截 / 放行」做成可复现、可解释、可回滚的闭环：

- 被测服务（SUT）自带：FastAPI + SQLite（WAL），含一处**故意保留的 N+1 报表**做缺陷样本
- 梯度压测引擎：`bench/engine.py`（线程池）、`bench/scenarios.py`（场景）、`bench/distributed.py`（分布式抬天花板）
- 门禁判定：`metrics/gate.py`（规则引擎）+ `gate_rules.yaml`（阈值全部集中在这一个文件）
- 基线存储：`metrics/store.py`（SQLite `quality.db`，WAL）、`metrics/baseline.py`、`metrics/analysis.py`
- 看板：`dashboard/app.py`（`serve` 起服务，`report` 导出静态 HTML 给 GitHub Pages）

## 怎么跑

一律用本仓 venv 的绝对路径调 `-m`（不必 activate）：

```bash
VENV=/d/DSH/test-projects/perf-baseline-quality-gate/.venv/Scripts/python.exe
cd /d/DSH/test-projects/perf-baseline-quality-gate

$VENV -m pytest tests/ -q                    # 全量：实测 379 passed, 19 skipped, 109s
$VENV -m mypy --cache-dir /tmp/mypy-cache    # 29 个源文件
$VENV -m ruff check . && $VENV -m ruff format --check .

$VENV platform_cli.py --help                 # 子命令一览
$VENV platform_cli.py env                    # 当前环境档案与生效变量
$VENV platform_cli.py run --items 20000 --step-users 10 20    # 一轮梯度压测 + 门禁判定
$VENV platform_cli.py noise --rounds 5       # 量本机运行间波动（定容忍带用）
$VENV platform_cli.py serve                  # 起看板
```

`platform_cli.py` 的 12 个子命令：`run` / `probe` / `baseline` / `trend` / `gate` /
`suite` / `flaky` / `noise` / `prune` / `serve` / `report` / `env`
（构建在 `platform_cli.py:1285` 的 `build_parser()`，入口 `platform_cli.py:1485` 的 `main()`）。

一键脚本：`scripts/run.ps1`（Windows）/ `scripts/run.sh`（Linux / macOS / Git Bash），
`-Task` 取值 `help/setup/env/test/cov/lint/lock/perf/probe/dash/all`。
**退出码一律透传自 `platform_cli.py`**（0 放行 / 1 拦住 / 2 用法错），脚本自身不做判定。

## 环境硬约束（本仓特有，必读）

### 1. `scripts/run.ps1` 必须带 UTF-8 BOM —— 而它现在**已经丢了**

文件自己的 docstring（`scripts/run.ps1:17-23`）写着：

> 编码注意事项（**不要"顺手修好"**）：本文件必须带 UTF-8 BOM。
> 实测：同一份内容带 BOM → 退出码 0；去掉 BOM → 退出码 1。

**实测（2026-11-02）这份 BOM 已经不存在了**：

```bash
head -c 3 scripts/run.ps1 | od -An -tx1   # → 3c 23 0d   （`<#` + CR，不是 ef bb bf）
git show HEAD:scripts/run.ps1 | head -c 3 | od -An -tx1  # → 3c 23 0a
```

后果与后果的边界：

- **Windows PowerShell 5.1**（系统自带）执行 `powershell -ExecutionPolicy Bypass -File scripts/run.ps1 -Task help`
  直接解析崩：`表达式或语句中包含意外的标记 '{'`，报在 `scripts/run.ps1:180`。
  已知线索：`8fc7634` 那版是带 BOM 的（`ef bb bf`），`25b8281` 之后变成 `3c 23 0a` ——
  某次提交把 BOM 洗掉了，而 `.gitattributes` 是 `* text=auto eol=lf`，不负责 BOM 保留/恢复。
- **PowerShell 7+**（`C:\Program Files\PowerShell\7\pwsh.exe`）默认按 UTF-8 读，**不要求 BOM**，
  实测能正常输出「仓库根 / Python / 环境档案 / 可用任务」。

所以：**用 PS 5.1 时要先把 BOM 补回去**（`printf '\xef\xbb\xbf' | cat - 原文件 > 新文件`），
或者直接用 `pwsh` 7 跑。别以为"脚本坏了"去改逻辑 —— 坏的是编码。

### 2. `PYTHONIOENCODING=utf-8` 不是可选项

Windows 控制台默认 GBK，中文报告（门禁 message、看板输出、`noise` 的统计表）
会直接抛 `UnicodeEncodeError`。`run.ps1` / `run.sh` 里都在最前面强制设置了这个变量；
手敲命令时自己带。

### 3. mypy 的缓存目录要可写

本机沙箱下 `.mypy_cache` 可能不可写，表现为：

```
error: INTERNAL ERROR -- Please try using mypy master on GitHub:
  ... mypy/metastore.py, line 172, in connect_db
```

**这不是类型问题，是 mypy 建不了缓存数据库。** 给一个可写目录即可：

```bash
$VENV -m mypy --cache-dir /tmp/mypy-cache    # → Success: no issues found in 29 source files
```

### 4. 沙箱限制下 pytest 会假红（环境问题，不是仓库问题）

在**受限沙箱**里跑 `pytest tests/ -q` 实测 **379 errors**（而不是 379 passed），
两种根因都来自进程权限被收窄，与代码无关：

- `PermissionError: [WinError 5] ...: '...\pytest-of-lefan'` —— pytest 的 basetemp
  目录 `os.scandir()` 被拒，`--basetemp` 指到别处也救不了（目录建得出来但列不了）
- `PermissionError: [WinError 5]` 落在 `subprocess.py:1375 _get_handles` ——
  `sut/server_ctl.py:184` 起 SUT 子进程时**建不了管道**

放开权限后同一命令是 `379 passed, 19 skipped in 109.29s`。
判定方法：看报错栈里是不是 `PermissionError [WinError 5]` + 不涉及本仓源码行；
是就换一个完整权限的终端重跑，不要改代码。

### 5. `fail_under` 是棘轮，不是目标

- 仓库级：`[tool.coverage.report] fail_under = 40`，CI 里以 `--cov-fail-under=40` 出现
  （**同一常量，改就要一起改**，`pyproject.toml:233` 有注释钉住这条）
- 单文件级：`[tool.coverage.perfile] fail_under = 60`、`min_statements = 10`，
  判定逻辑在 `scripts/check_coverage.py` —— coverage 7.16.2 的 `CoverageConfig`
  只有全局 `fail_under`，没有 per-file 版本，所以只能读 `--cov-report=json` 的产物自己判

规则写在 `pyproject.toml:281` 的注释里：**门槛 = 当前最差入选文件 - 2pp，修完再抬，
每轮只抬一次**。理由是不让它变成"看到红灯就把数字调低"。
演进记录也在那段注释里（22 → 60，每一步对应补了哪些测试文件）。

### 6. 依赖全 `==` 钉死，锁文件必须重生成

`requirements.txt`（30 行）/ `requirements-dev.txt`（27 行）全部 `==`。
改完必须重生成 `requirements.lock`，否则 `check_deps_sync` 用例红。
理由写在 README §八：这个平台卖的"数字可复现"，前提是依赖也复现 ——
`>=` 会让"今天能重现的数"和"下周能重现的数"跑在两套不同版本的库上。

## 硬约束

- **阈值只改 `gate_rules.yaml`，不改代码**。`gate_rules.yaml:1` 写死了这条：
  "改标准改这里，不改进代码"。
- **`severity` 只有三档处置**：`blocking`（拦住合并）/ `warning`（只报告）/ `skipped`（没判定）。
  曾经有过第三档 `info` 并导出 `INFO` 常量，但 12 条规则没有一条用、全仓搜不到调用点 ——
  已删除，**不要再加回来**（`metrics/gate.py` 的模块 docstring 记了这个决定）。
- **`blocking` 只留真的能让生产出事故的几条**。全部门禁都是 blocking，等于没有门禁。
- **`gate_rules.yaml` 里的容忍带必须本机实测**：`python platform_cli.py noise --rounds 5`
  量出来再填，不要照抄。文件里记着一次真实打脸：拍脑袋写 3%，同代码同配置连跑两轮
  P95 从 105.9ms → 110.2ms（+4.1%），门禁判"拦住"—— 一次没有任何代码变慢的假阳性。
  本机 5 轮实测 `[128.5, 127.5, 137.9, 130.8, 132.2]` ms，均值 131.4ms，CV = 3.11%。
- **R3 只报数不拦人**：这条判据在本环境噪声主导，见 `docs/已知问题.md` P-16。
  CI 里给 `perf-gate` 用了 `--r3-advisory`。
- **不给没有证据的绿灯**：README §九 明确把「本机跑不了的三件事」标为"未验证"。
  新加门禁/新加数字时保持同一标准。

## 这个仓最值得读的三份文档

| 文档 | 内容 | 为什么重要 |
|---|---|---|
| `docs/已知问题.md` | **P-1 ~ P-32** 编号缺陷，每条写清现象、根因、是否修、没修的理由 | 面试时被追问"这系统有什么问题"就翻它 —— 32 条自曝的缺陷比 0 条更像真的 |
| `docs/诚实边界.md` | 九节，逐条说明这套东西**不能**证明什么 | 性能项目最容易吹的就是"我的基线很准"，这份文档专门唱反调 |
| `docs/度量口径.md` | p95/p99 怎么算、热身样本怎么丢、样本量下限为什么是 2000 | 数字口径不同则结论完全相反，先看口径再看数 |

代表性缺陷：P-27（热身兜底没有用例守住）、P-28（`run.ps1` 在全新克隆上无法 bootstrap ——
与上面第 1 条 BOM 缺陷是同一族问题）、P-32（N+1 查询次数 20040 → 20041 的归因）。

## 已知坑

- **`.gitattributes` 是 `* text=auto eol=lf`**，且明确注释"本项目有一次事故就是编码/换行往返
  损坏源文件"，事故证据逐字节保留在 `docs/incident-encoding-20261010/`（`*.corrupt -text`）。
  动换行/编码相关文件前先看那份事故记录。
- **CI 有 6 个 job**：`lint`（ruff + mypy）/ `security-audit`（pip-audit，**非阻塞**）/
  `platform-tests` / `perf-gate` / `n1-probe` / `pages`。都在 `.github/workflows/quality-gate.yml`。
- **本机跑不了的三件事**（README §九 明确标注"未验证"）：容器化、MySQL 写路径实测
  （`tests/test_mysql_store.py` 会 skip，reason：探测不到 MySQL `PERF_MYSQL_URL=127.0.0.1:3307`）、
  分布式压测的真实多机效果。看到 skip 别当成红。
- **`quality.db` 是 SQLite + WAL**：`prune` 清历史数据时留了"不会误删当前基线"的保护，
  改动那段逻辑前读 `docs/已知问题.md` P-29~P-31（`--rules` 死参数、prune 错报保护原因、
  noise 崩溃留僵尸行，三个都是实际修过的）。

## 提交纪律

- Conventional Commits，`scripts/check_commit_msg.py` 校验。
- 一次提交只做一件事。`CHANGELOG.md` 按版本维护，修缺陷时把 P 编号写进提交信息
  （历史提交就是这么写的：`fix: 修好 run.ps1 在全新克隆上无法 bootstrap（P-28）`）。
- 改完先跑 `$VENV -m pytest tests/ -q` + `$VENV -m ruff check .` + `mypy --cache-dir`，三条都绿再提交。
