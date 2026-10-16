# 性能与稳定性基线平台 · 质量度量与 CI 门禁平台

[![quality-gate](https://github.com/lefan17/perf-baseline-quality-gate/actions/workflows/quality-gate.yml/badge.svg)](https://github.com/lefan17/perf-baseline-quality-gate/actions/workflows/quality-gate.yml)

一个**自己压、自己判、自己拦**的性能质量平台：内置被测服务、自研压测引擎、
结果入库、基线劣化对比、flaky 识别、门禁规则引擎、看板与 CI 集成。

它不是"跑个 Locust 出张图"，而是回答三个具体问题：

1. **这轮到底跑的是什么配置？** —— 配置与平台信息随每次运行入库，可回查。
2. **变慢了没有？和谁比？** —— 基线显式建立，劣化按容忍带判定。
3. **门禁真的能拦住吗？** —— 内置**故意保留的性能缺陷**，探针反复证明门禁会红。

> 🧭 **在作品集中的位置**：这条测试 / 质量线上的**第二段**。起点是
> [fullstack-test-framework](https://github.com/lefan17/fullstack-test-framework)——接口 + UI + 业务全链路
> 自动化测试框架（pytest + FastAPI + Playwright + Allure + CI），解决"执行与断言"；本仓库在它后面，
> 解决"执行完之后的判断"：把性能与稳定性量化成基线，再把判断固化成 CI 门禁。第三段是
> [agent-test-hub](https://github.com/lefan17/agent-test-hub)——Agent 自动测试系统（LangGraph + LLM
> 编排，从 OpenAPI 契约生成用例、执行、重试与缺陷归因）：接口自动化 → 性能与质量门禁 → Agent 自动测试。

---

## 一、先说结论：这个平台抓到了什么

所有数字都是本机实测（Windows 11 / Python 3.12.10 / 20 000 条物料数据），
不是估算。完整的原始输出在 `reports/` 与 `docs/已知问题.md`。

### 1. 一个 N+1 报表：单请求慢 7.8×，并发下直接崩掉

同一份 SQL 的两种写法（都在 `sut/app.py` 里，用环境变量切换）：

| 实现 | 单请求耗时 | **每个请求的 SQL 次数** |
|---|---:|---:|
| `agg`（一次 `GROUP BY`） | 40.6 ms | **1** |
| `n1`（逐供应商、逐物料查） | 316.9 ms | **20 040** |

单请求 7.8× 还算"能忍"。把并发拉起来之后：

> **口径说明（全文只有这一套单请求口径）**：复用连接（每个组合一个
> `requests.Session()`）+ 热身 3 次（样本丢弃）+ 15 个样本取中位数 +
> 每格独立复测 3 轮取中位；本文的数字再对 6 次独立整轮复测取中位。
> 测量日期 2026-10-13，机器 Windows 11 / Python 3.12.10 / 20 000 条物料。
> 本表 20 000 档与 §2 表 20 000 档**是同一组数字**（`agg` 40.6 ms / `n1` 316.9 ms）——
> 它们现在就是同一口径测出来的，不再存在第一套 / 第二套口径之分。
> **绝对值随机器与轮次浮动**（本机 6 轮复测里 `n1` 落在 252–404 ms），
> 能外推的只有**比值趋势**。
>
> 下面的并发表不是单请求中位数：它来自自研压测引擎（`platform_cli.py probe`，
> 一次连续压测内的 P95，单机线程池），口径不同，两者不要互相换算。

| 并发 | `agg` 耗时 | `agg` RPS | `n1` 耗时 | `n1` RPS |
|---:|---:|---:|---:|---:|
| 1 | 37.1 ms | 27.1 | 218.9 ms | 4.59 |
| 4 | 57.5 ms | 69.8 | **2 451 ms** | 1.68 |
| 10 | 105.2 ms | 95.5 | **9 059 ms** | **1.08** |

`agg` 是正常的排队曲线（耗时平缓上升，吞吐 27 → 96 RPS）。
`n1` 是崩塌曲线：**并发从 4 涨到 10，吞吐反而掉到 1.08 RPS，还不如单线程。**
20 040 条 SQL 轮番争抢 SQLite 的连接与页缓存，加并发只是让它们互相踩踏。

> **如果只量一次响应时间（7.8×），完全得不出"这个接口上线会挂"的结论。**
> 这就是为什么性能验证必须在并发下做。

### 2. 同一个缺陷，换数据量就从"看不见"变成"很明显"

| 数据量 | `agg` | `n1` | 比值 |
|---:|---:|---:|---:|
| 800 | 5.5 ms | 11.4 ms | **2.08×** |
| 2 000 | 6.7 ms | 19.6 ms | 2.91× |
| 20 000 | 40.6 ms | 316.9 ms | **7.81×** |

> **口径（与 §1 表完全相同，必须一并读）**：同一台机器（Windows 11 /
> Python 3.12.10）、SUT 进程复用连接 + 热身 3 次 + 15 个样本取中位数，
> 每格独立复测 3 轮取中位；测量日期 2026-10-13。
> 20 000 档与 §1 表是**同一组数字**。**绝对值随机器与轮次浮动** ——
> 本机 6 次独立整轮复测的极差：800 档 `n1` 9.9–15.6 ms，20 000 档
> `agg` 37.1–54.3 ms / `n1` 252.5–404.3 ms。
> 能外推的是**比值趋势**，不是毫秒数。

比值整体随数据量放大：**2.08× → 2.91× → 7.81×**（6 次复测里有 5 次严格单调）。
800 条时每个供应商下只挂 20 个物料，多查 20 次还没到把 SQLite 页缓存压垮的量级，
所以只到 2 倍量级；到 20 000 条就放大到 7.8 倍。

**这条探针最初红过，而且当时的数字更"戏剧化"**：它那时还是功能用例里的一次性探针，
口径是"每个请求新建一个连接、不热身" —— 连接建立成本（约 10–25 ms）压在 `agg`
的 5 ms 稳态耗时上，把比值稀释成了 **1.02×**（断言是比值 > 1.5，于是探针红了）。
所以那张老表里的 `31.7 ms / 32.4 ms / 1.02×` 并不是编出来的，
它是**另一套测量口径**下的真实数字；而同一张老表里的 `2 000 → 6.0 ms`
已经是热身后的稳态值。两套口径混在同一张表里，就出现了"数据翻倍反而快 5 倍"
这种物理上不可能的非单调数字。
现在探针已被从功能用例里拆出来单列 `tests/test_probes.py`，
口径也统一成了上面注明的热身 / 复用写法。

> **旧口径（"每请求新建连接 + 不热身"）为什么被放弃**：它不是"另一个同样有效的
> 视角"，而是一个**会给每个接口加一笔固定成本**的坏口径 —— 这笔成本在快接口上
> 占比大、在慢接口上占比小，于是系统性地**压低比值**（同机对照实验见
> `docs/诚实边界.md` 第五节）。
> 保留它只有一个用途：当作"为什么必须热身 + 复用连接"的教学案例。
> 它**不再作为并列的另一组数字**出现在任何结论表里。

> **在玩具数据量上做性能判断，得到的结论是"没有性能问题"。**
> 这比不测更危险 —— 它给的是一个一直持续到上线的虚假安心感。

### 3. 门禁真的会拦：一次完整的基线劣化闭环

```
① 建基线        10u / 20k / agg   → P95 100.5ms   判定 ✅ 放行   --set-baseline
② 同配置复跑    10u / 20k / agg   → P95  88.7ms   判定 ✅ 放行   R3：劣化 0 项
③ 模拟回归      10u / 20k / n1    → P95 10013ms   判定 ❌ 拦住   退出码 1
```

第三步的门禁输出（节选）：

```
[FAIL] R1-error-rate: 错误率 40.00%（阈值 1.00%）
[FAIL] R2-p95-absolute: P95 10013.0ms（绝对阈值 300.0ms）
[FAIL] R3-baseline-degradation: 相对基线劣化 6 项：p95_ms +9861.7%（基线 100.5）；
       error_rate 从 0 变为 0.400（基线 0.0）；rps -99.7%（基线 304.1）
判定：❌ 拦住（存在 blocking 未通过）
```

**退出码 0 = 放行，1 = 拦住**，可直接接进 CI。上面三步都是真实跑出来的。

---

## 二、快速开始

```powershell
# 1. 建虚拟环境 + 装依赖（运行时 + 开发工具链）
pwsh -File scripts\run.ps1 -Task setup

# 2. 跑平台自身的测试（175 passed，约 82 秒）
pwsh -File scripts\run.ps1 -Task test

# 3. 跑一轮真压测 + 门禁（20k 数据、10u/20u 两级并发，约 1 分钟）
pwsh -File scripts\run.ps1 -Task perf
```

Linux / macOS / Git Bash 用 `scripts/run.sh`，任务名与退出码语义完全一致。

`-Task help` **打印将要执行的命令但不真跑**：

```powershell
pwsh -File scripts\run.ps1 -Task help
```

这不是"另写一份文档"——`help` 与真正执行的命令来自脚本里**同一份定义**，
所以两者不可能漂移。之所以做成脚本而不是继续手敲下面这段，是因为它有
一条不会在报错里体现的前提：漏掉 `PYTHONIOENCODING` 的表现是
「中文报告打崩」而不是「命令失败」，很容易被误判成平台 bug。

<details>
<summary>不用脚本时的手敲命令（等价 —— 脚本只是把它固化了）</summary>

```powershell
# 1. 建虚拟环境并装依赖（Windows / PowerShell）
python -m venv .venv
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 2. 跑平台自身的测试（默认用小数据量，约 40 秒）
$env:PYTHONIOENCODING="utf-8"
& ".\.venv\Scripts\python.exe" -m pytest tests/ -q

# 3. 跑一轮真压测（20k 数据，三级并发梯度）
& ".\.venv\Scripts\python.exe" platform_cli.py run --label my-run --items 20000 --step-users 10 20 40

# 4. 看板
& ".\.venv\Scripts\python.exe" platform_cli.py serve --port 8765

# 5. （可选）量一下本机的性能噪声，确认容忍带是否合适
& ".\.venv\Scripts\python.exe" platform_cli.py noise --rounds 5
```

</details>

> `$env:PYTHONIOENCODING="utf-8"` 在 Windows 上不是可选项：
> 控制台默认 GBK 编码，中文报告会直接打崩。一键脚本里已经设好了。

### 命令行一览

| 命令 | 作用 | 退出码 |
|---|---|---|
| `run` | 梯度加压（10/20/40/80）→ 入库 → 找拐点 → 比基线 → 出门禁 | 0/1 |
| `probe` | `agg` vs `n1` 两种实现各压一次并对比 | 0/1 |
| `suite` | 跑功能用例 → 入库 → 套件门禁（通过率 + flaky 预算） | 0/1 |
| `flaky` | 列出摇摆用例（执行 ≥ 6 次才下结论） | 0/1 |
| `baseline set/show/clear` | 基线管理（**只由显式命令写入**） | 0 |
| `baseline export/import` | 基线快照出/入仓库（`baselines/<label>.json`）—— CI 靠它拿到人设的基线 | 0/2 |
| `trend --label X` | 历史趋势 + 方向判断 | 0 |
| `gate --run-id X` | 对已入库的 run 重跑门禁（改阈值后复盘的入口） | 0/1 |
| `noise --rounds 5` | **量出本机的运行间波动**，用来定劣化容忍带（不自动改配置） | 0 |
| `serve` | 起看板（含 `/gate` JSON API） | 0 |
| `env` | 显示当前环境档案（`PERF_ENV`）与生效变量 | 0 |

CI 可以直接调看板的结构化判定，不用解析 stdout：

```
GET /gate?run_id=perf-20261010-190550-f63da4
→ {"blocked": true, "metrics": {...}, "deltas": [...], "results": [...]}
```

### 在 GitHub Actions 上的实测结果

本仓库推上 GitHub 后 `.github/workflows/quality-gate.yml` 真的跑了，
三个 job 全部通过（run [38055102821](https://github.com/lefan17/perf-baseline-quality-gate/actions/runs/38055102821)，
`ubuntu-24.04` / Python 3.12）。从日志里摘的关键行：

| job | 日志原文 | 结论 |
|---|---|---|
| 平台自身测试 | `[suite] 通过 117 / 失败 0 / 报错 0 / 跳过 0`、`[PASS] S1-pass-rate: 通过率 100.00%（阈值 100.00%，117/117）`、`[PASS] S2-flaky-budget: flaky 用例 0 条（预算 2 条）` | ✅ 放行 |
| 性能门禁 | `10u n=2450 rps=376.10 p95=70.62ms`、`20u n=2804 rps=429.05 p95=95.29ms`；`[PASS] R1/R2/R3/R4/R5` 全过（R3 是「未设置基线，跳过劣化对比」） | ✅ 放行 |
| N+1 缺陷探针 | `[probe] mode=agg rps=95.00 p95=46.9ms` vs `mode=n1 rps=1.32 p95=3828.6ms`；`[FAIL] P1-n1-probe: N+1 报表 P95 3828.6ms（阈值 150.0ms）`、`[WARNING] P2-n1-agg-ratio: 81.6×（阈值 10.0×）` | 探针按预期变红（`continue-on-error: true`），证明门禁真的会拦 |

三点值得注意：

1. **缺陷在别人的机器上依然复现，而且更严重**：本机 n1/agg 是 49.3×，
   GitHub runner 上是 **81.6×**（P95 3828.6ms vs 本机 2985.4ms）。
   N+1 的代价随机器而变，但"它是灾难"这个结论与机器无关。
2. **换机器后绝对数字全线不同**：runner 上 10 并发 P95 70.62ms / RPS 376，
   本机同配置是 79.99ms / 369.59。同量级，但不相等 ——
   这正是 `docs/诚实边界.md` 说"绝对数字不可外推"的现场证据。
3. **CI 里的 R3 是「未设置基线，跳过劣化对比」**，不是"比过了没劣化"。
   runner 是干净环境，没有 `quality.db`，所以拿不到基线。
   这一步**故意不自动建基线**（见 `quality-gate.yml` 第 66-69 行注释）——
   否则"慢慢变慢"会被每一轮自己批准。

> 上面这张表是 **run 38055102821 的时点快照**（2026-10-10），数字是那次日志的原文，
> **不改**。它记下的那个缺陷（CI 里 R3 永远是"跳过"）已经修掉了：
> 机制见下一节。

### 基线怎么在没有数据库的 runner 上活下来

CI 是干净环境，`quality.db` 是它自己从零建的。基线要跨过去，只能靠**仓库里的文件**：

```powershell
# 1. 人显式建基线（CI 永远不会替你干这件事）
& ".\.venv\Scripts\python.exe" platform_cli.py run --label ci-perf --items 20000 `
    --step-users 10 20 --step-duration 8 --set-baseline

# 2. 导出成快照并提交 —— 谁在哪次提交里动了基线，git log 能回答
& ".\.venv\Scripts\python.exe" platform_cli.py baseline export --label ci-perf
git add baselines/ci-perf.json; git commit -m "chore(baseline): 重设 ci-perf 基线"
```

`quality-gate.yml` 的 `perf-gate` job 在跑压测**之前** `baseline import --label ci-perf`，
于是 R3 真的做对比。三条边界都有确定行为，且**没有一条是"安静放行"**：

| 情况 | 行为 |
|---|---|
| `baselines/ci-perf.json` 不存在 | 打 `::warning::` + 写 step summary，R3 报 `[SKIP]「未设置基线」`（**不是** 通过） |
| 文件损坏 / 版本不认识 / label 不符 | `baseline import` 退出码 2，**这一步就红** —— 读不对的基线必须吵闹 |
| 快照的配置与本轮不可比（items/并发/场景/报表实现不同） | R3 报 `[SKIP]「两轮配置不可比」`，不给劣化百分比 |

**没选 artifact / 存储分支，理由**：`quality-db-and-reports` 由**同一个 run 自己**的
`platform-tests` job 上传，跨 job 下载拿到的是本轮刚建的库（`baselines` 表恒空）——
这正是原来的缺陷；存储分支则要求给 CI `contents: write`，等于给它一个"改基线"的能力，
与"基线只能由人显式设置"直接冲突。快照文件走普通提交：不需要任何 secret，不需要额外权限，
且改动留痕。

---

## 三、架构：为什么这么切

```
sut/          被测服务（FastAPI + SQLite），内置可切换的性能缺陷探针
bench/        自研压测引擎（进程内线程池，采原始样本）
metrics/      结果入库、基线对比、拐点定位、flaky 识别、门禁规则引擎
dashboard/    看板 + /gate JSON API（零前端构建链）
tests/        平台自身测试（含性能探针）
```

### 关键设计决策与理由

**① 自研压测引擎，不用 Locust**

要自己算分位数、要精确控制每个梯度的稳态时长（为了定位拐点），
不想在平台里引入第二套 HTTP 栈。代价说清楚：**没有分布式能力，单机是天花板** ——
压测机和被测服务抢同一份 CPU，数字偏保守。这条边界写在每份报告里。

**② 自建被测服务，不打公网**

压测结论只有在**能解释每一个百分点**时才有价值。打公网遇到 P99 抖动，
你无法判断是网络、限流还是自己脚本的问题。自建服务本地起、数据本地落、
瓶颈可归因（SQL？CPU？还是故意注入的 N+1？）。

**③ 不引入 pytest-xdist**

并行跑用例会争抢 CPU 与端口。在性能项目里这不是"更快"，
而是"把测量对象和干扰源混在一起"。串行跑得慢是特性，不是缺陷。

**④ 看板不引入任何前端构建链**

手写 SVG 折线，没有 npm、没有 CDN。为画三条折线要求一个内部看板装
node_modules，是它最后没人维护的常见原因。

**⑤ 门禁的每条规则都带「处理建议」**

只会 `exit 1` 的门禁寿命大约两周 —— 没人知道该做什么，最后被注释掉。
所以 `RuleResult` 强制携带 `action` 字段，且有一条测试专门检查
"每条规则都带行动指引"（`test_every_rule_carries_an_action`）。

---

## 四、门禁规则一览

阈值集中在 `gate_rules.yaml` 一处，改这里就是改全平台的判定标准。

| 规则 | 级别 | 判据 | 拦住什么 | 出现在哪个 CI job |
|---|---|---|---|---|
| `R1-error-rate` | blocking | 错误率 > 1% | 服务已经在报错 | `run`（性能门禁） |
| `R2-p95-absolute` | blocking | P95 > 300 ms | 兜底线，防止基线本身已经烂了 | `run` |
| `R3-baseline-degradation` | blocking | 相对基线劣化 > 10% | **慢慢变慢**（本平台核心价值）。**没有基线时以 `[SKIP]`（warning）出现** —— 「跳过」不是「通过」，不许长得像「比过了没劣化」；两轮配置不可比（数据量/并发/场景/报表实现不同，或老基线没有配置快照）时同样以 `[SKIP]` 出现，判定为"无法判定可比性"而**不给劣化百分比** | `run` |
| `R4-sample-size` | warning | 样本 < 2000 | 用太少样本下结论 | `run` |
| `R5-tail-jitter` | warning | P99/P95 > 4（同一梯度内配对计算） | 尾部抖动异常 | `run` |
| `P0-probe-data-equivalence` | blocking | `agg` 与 `n1` 返回的数据不一致 | **两个实现语义不等价时，谁快谁慢没有意义**。取不到响应体或覆盖面不足时以 `[SKIP]`（warning）出现在报告里，判定为"无法判定"而不是"一致" | `probe`（N+1 缺陷探针） |
| `P1-n1-probe` | blocking | N+1 报表 P95 > 150 ms | 缺陷复活 | `probe` |
| `P2-n1-agg-ratio` | warning | n1/agg > 10× | 优化收益量化 | `probe` |
| `P3-probe-sample-size` | warning | 某一模式的样本数 < 2000 | 用太少样本给一个 blocking 结论。**仅在调用方显式传入每模式样本数时产出**（`evaluate_probe(p95, rules, sample_totals)`；`probe` 主路径会传） | `probe` |
| `S1-pass-rate` | blocking | 通过率 < 100% | 功能用例挂了。**0 个用例时以 `[SKIP]`（warning）出现**：不是 100%，也不是 0% | `suite`（平台自身测试） |
| `S2-flaky-budget` | warning | flaky > 2 条 | 摇摆用例污染信号 | `suite` |
| `S3-pytest-exit` | blocking | pytest 退出码不是 0/1，或退出码为 1 但库里没有任何 failed/error 用例 | 本轮**没有正常跑完**（5=没收集到用例，2=被中断，3=内部错误，4=用法错误），而用例表看起来是干净的。**仅在调用方显式传入 `exit_code` 时产出** | `suite` |

12 条规则全部住在一个函数里对应一处判定，判定逻辑不在 YAML 里 ——
YAML 只放阈值。CI 的三个 job（`suite` / `run` / `probe`）合起来正好覆盖这 12 条。

**为什么劣化容忍带是 10%：它量出来的，不是拍出来的。**

最初写的是 3%（当"经验下界"），实测立刻打脸 ——
同一份代码、同一配置连跑两轮，P95 从 105.9ms 到 110.2ms（+4.1%），
门禁判「拦住」。**没有任何代码变慢，这是一次纯粹的假阳性。**

于是补了 `platform_cli noise` 子命令来量本机的真实波动，
实测 5 轮：

```
各轮 P95 = [128.5, 127.5, 137.9, 130.8, 132.2] ms
均值 131.4ms，样本标准差 4.09ms → 单轮 CV = 3.11%
R3 判的是**两轮之差**，不是单轮相对均值：
CV_diff = √2 × CV ≈ 4.40%；10% / 4.40% ≈ 2.27σ
```

这里要如实说清两点：**极差 8.16% 只是同一批数据的另一种描述，不是推导**
（样本才 5 个，极差本身很抖）；真正决定假阳性率的是差值分布的标准差，
所以按 `CV_diff` 走。而 **2.27σ 是工程折中，不是统计保证** ——
它对应的单尾假阳性率约 1.2%，不是 0。想要更硬的边界，
要么多跑几轮把 CV 量准，要么把带子放到 3σ ≈ 13.2%（代价是漏报更多）。

**容忍带取决于机器和环境，不能凭常识填。** 换机器后应重跑
`noise` 重新定，而不是沿用别人环境里的值。
一个比噪声还窄的容忍带**等于没有容忍带**：它不会更严格，
只会稳定地产出假阳性 —— 而稳定变红的门禁，
唯一的结果是训练所有人忽略红灯。

> `noise` **刻意不自动改 `gate_rules.yaml`**：
> 自动调阈值等于让门禁自己给自己发免死金牌。

---

## 五、诚实边界：这套东西**不能**证明什么

这一节不是免责声明，是使用说明。越界解读比没有数据更危险。

**1. 数字全部来自单机，且压测机与被测服务抢同一份 CPU。**
真实容量会比这里高（服务能独占资源），但**比例关系**（N+1 vs 聚合、
写 vs 读）是可信的。不要拿绝对 RPS 去做容量规划。

**2. 自研引擎没有分布式能力。** 单机线程池到不了生产级别的压力上限。
要测万级并发得上分布式压测集群，这不在本项目范围内。

**3. SQLite 的写路径数字不可外推。**
`stock_write` 场景实测只有 66 RPS（P99 916ms），瓶颈是 SQLite 的
**单写者模型**（`BEGIN IMMEDIATE` 独占写锁），不是应用代码质量。
换 MySQL/PostgreSQL 后写并发模型完全不同，这组数字没有参考价值。

**4. 性能探针的数据量必须接近真实规模。**
见上面第 2 条结论 —— 数据量越小，N+1 的比值越被连接 / 热身等固定成本稀释
（老口径下 800 条能测出 1.02×，看起来"没有性能问题"）。

**5. 基线劣化对比只能告诉你"变慢了"，不能告诉你"为什么"。**
归因要靠报告里的场景拆解（按权重分开压）+ 对照两次的 SQL 与索引。

**6. flaky 判定需要至少 6 次执行历史**，样本不足时明确拒绝下结论
（而不是给一个不可靠的结论）。

---

## 六、项目自身质量

```
$ pytest tests/ -q
175 passed in ~82s
```

| 测试文件 | 用例数 | 覆盖 |
|---|---:|---|
| `test_engine.py` | 13 | 分位数算法（nearest-rank）、Runner、样本合并、场景 ID 范围 |
| `test_platform.py` | 71 | 基线对比、拐点定位、flaky 分析、全部 12 条门禁规则、尾比口径、基线可比性 |
| `test_sut_api.py` | 20 | 被测服务功能（含**两种实现数据一致性交叉校验**）+ 临时目录回收 |
| `test_probes.py` | 3 | 性能缺陷探针（真实数据量 20 000） |
| `test_cli.py` | 19 | CLI 解析器接线（每个子命令都绑定真实函数；`baseline` 的 5 个 action 也是契约） |
| `test_dashboard.py` | 13 | 看板渲染接线（折线真的被画出来，不是定义了没用）+ 汇总口径单一来源 |
| `test_r3_end_to_end.py` | 2 | R3 端到端：真的压一轮、真的比基线、真的会变红（`sut` + `perf` 标记） |
| `test_rules_single_source.py` | 10 | 阈值只有一个住址（`gate_rules.yaml`），改 YAML 真的会改判定 |
| `test_baseline_snapshot.py` | 24 | 基线快照 `baseline export/import`：缺文件 / 损坏 / 版本不认识 / label 不符 / 配置不可比 |
| **合计** | **175** | |

### 七个真实踩坑，都留了测试防回归

这个项目最有价值的部分可能是它**踩过的坑**，每个都变成了用例：
**① 性能对比必须先证明两种实现语义等价。**
`agg` 实现最初写成三张表一路 `LEFT JOIN`，导致 `ledger` 的扇出
让每个物料按流水条数重复出现，`SUM(i.stock)` 虚高 **2.23 倍**
（113194 vs 真值 50697）。**它骗过了当时所有的功能用例** ——
因为不报错、不慢，而且同一句 SQL 里的 `COUNT(DISTINCT i.id)`
因为有 DISTINCT 反而是对的（"一个指标对、另一个指标错"最难发现）。

抓到它的是一条交叉校验：`assert agg == n1`。
**如果两种实现返回的数据不一样，比的就不是"同一功能的两种写法"，
速度对比立刻失去意义。**

**② CLI 的接线问题，纯逻辑测试测不到。**
`def cmd_gate(args):` 这一行曾被误删，函数体变成悬空代码
（`NameError: name 'cmd_gate' is not defined`），**整个 CLI 起不来** ——
而这个错误通过了当时全部 84 条测试，因为它们测的全是
`metrics/` 里的纯逻辑，没有一条去构建 argparse 解析器。
补上 `test_cli.py` 后这类故障才有防线。

**③ 不热身的测量会系统性地低估差距。**
同一个"报表比点查慢 5 倍"的断言，不热身时量出 3.0×（红），
热身并复用连接后是 **9.8×**（绿，点查 4.2ms / 报表 40.6ms）。差别全在分母：
冷启动时每个接口背约 12ms 固定成本（TCP 建连 + 页缓存冷 + uvicorn 惰性导入），
这个成本加在 4.2ms 的点查上占比远大于加在 40.6ms 的报表上。
**修法不是放宽阈值，是修测量方法。**
（对照实验：把两套口径拆成四个组合分别测，见 `docs/诚实边界.md` 第五节。）

> 还有一种"测了但等于没测"的情况：性能探针最初写在功能用例文件里，
> 用 800 条数据跑，得到"没有性能问题"的结论并一路绿灯。
> 详情见 `tests/test_probes.py` 的文件头。

**④ 压测工具自己造出了 28% 的错误率。**

`--items 5000` 跑混合压测，门禁报「错误率 28.02% → 拦住」，
延迟却完全正常（P95 41ms）。**看起来就是被测服务在高并发下开始报错。**

查 `status_breakdown` 才发现 **409 个全是 404**：
`bench/scenarios.py` 把随机物料 ID 的范围写死成 `1..20000`，
而这次只灌了 5 000 条，于是 75% 的点查打到不存在的 ID
（实测 28%，因为只有点查 30% + 写 5% 两类场景用这个 ID，比例对得上）。

危险之处在于它**伪装成被测服务的缺陷**：结果面板上只有一个错误率数字，
不查错误分布，结论就会是"被测服务有 28% 失败率"——
一个完全错误、而且会被写进报告的结论。修法是让 ID 范围跟随实际数据量
（`configure_item_range`），**不是放宽阈值**：28% 确实该拦，
错的是造出这 28% 的人。

**⑤ 门禁的容忍带最初是拍的，实测证明它比噪声还窄。**

容忍带原写 3%（当"经验下界"）。同一份代码、同一配置连跑两轮，
P95 从 105.9ms 到 110.2ms（+4.1%），门禁判「拦住」——
**一次没有任何代码变慢的假阳性**。

于是补了 `platform_cli noise` 来量本机的真实波动，实测 5 轮：
`[128.5, 127.5, 137.9, 130.8, 132.2]`，均值 131.4ms、样本标准差 4.09ms
→ 单轮 CV **3.11%**。而 R3 判的是**两轮之差**，差值序列的方差是单轮的两倍，
所以 `CV_diff = √2 × CV ≈ 4.40%`，**10% ≈ 2.27σ** → 容忍带改为 **10%**。
（如实说明：2.27σ 是工程折中不是统计保证，单尾假阳性率约 1.2%；
极差 8.16% 只是同一批数据的另一种描述，不构成推导。）

**一个比噪声还窄的容忍带等于没有容忍带**：它不会更严格，
只会稳定产出假阳性，而稳定变红的门禁唯一的结果是训练所有人忽略红灯。
换机器后应重跑 `noise` 重新定 —— 这个数字是环境的属性，不是常识。

> 同一处还藏着一个浮点坑：`100.0 * (1 + 0.10)` 在 IEEE 754 下是
> `110.00000000000001`，算出 +10.0000000000009%，**比容忍带大一点点**。
> 于是"刚好涨 10.0%"这个最容易被触发的点反而被判劣化，
> 和「边界取不大于」自相矛盾。修法是加 `_EPS = 1e-9` 参与比较。

**⑥ 同一个 run，报告和门禁曾经给出两个不同的 P95。**

`run` 手里有原始样本，于是池化后重算分位数（105.9ms）；
`gate` 只有入库的汇总，只能取最差梯度（128.9ms）。
**报告上的数字和门禁据以拦人的数字对不上**，
而这不会报错、不会变红 —— 只会在某天有人问
"报告说没超阈值，为什么这条流水线红了"的时候，没人解释得清。

顺带查出同一个函数里 RPS 写成了 `sum(r["rps"])` ——
把各梯度吞吐**相加**，两梯度各约 370 RPS 汇总成 740，凭空翻倍。
它能活很久是因为门禁只在 RPS **下跌**时判劣化，而"翻倍"方向朝上，
正好躲过检查。现在统一走 `_merge_from_rows`，RPS 按 `total / duration` 算。

**教训：取数函数有两个调用方时，必须保证它们算出同一个数。**

> 修复前后：错误率 **28.02% → 0.00%**，退出码 **1 → 0**。

**⑦ 一个不存在的 git 仓库，让一次编码事故差点不可逆。**

为了验证"看板折线真的被画出来"这条用例并非装饰，我做了变异测试 ——
把 `_sparkline` 临时改成返回空。改写用的是 PowerShell 5.1 的
`Get-Content -Raw` + `Set-Content`。

**PowerShell 5.1 的 `Set-Content` 默认按 ANSI/GBK 写盘，不是 UTF-8。**
文件里 81 处中文立刻变成替换符，`dashboard/app.py` 抛
`SyntaxError: invalid character '·' (U+00B7)`，且 utf-8 与 gbk
**都无法解码** —— 数据已经丢了，不是"编码选错了"。

更糟的是当时**没有 git 仓库**（`git rev-parse --is-inside-work-tree`
报 `fatal: not a git repository`），也没有备份。最后是从
`__pycache__/app.cpython-312.pyc` 里用 `marshal` 提取字符串常量
（110 个常量、90 个唯一串、19 个含中文，含全部 docstring 与 HTML 模板）
逐字重建的 —— **靠的是编译产物比源文件活得久这个巧合。**

三件事因此改掉：①本目录已 `git init` 并提交；②不再用 PowerShell
往返改写非 ASCII 源文件，变异测试改用 Python 的
`pathlib.Path.read_text(encoding="utf-8")`；③`dashboard/app.py` 重建后
用 AST 常量集合与原 `.pyc` 逐串比对，**确认除两处有意的措辞改动外
逐字一致**（重建时把页脚硬编码的 `3%` 改成插值
`{analysis.DEGRADATION_TOLERANCE:.0%}`，与 10% 定稿对齐）。

> 这条没有测试可以防 —— 它的防线是"别那么做"加"有 git"。

---

## 七、文档索引

| 文件 | 内容 |
|---|---|
| `docs/已知问题.md` | 14 个缺陷的完整记录（P-12 合并口径抹平长尾比、P-13 不可比基线、P-14 临时目录泄漏为本次新增）：机制、实测数字、门禁、探针 |
| `docs/度量口径.md` | 分位数算法、容忍带、拐点判据、flaky 判据的全部口径 |
| `docs/诚实边界.md` | 结论的适用范围与不可外推的部分 |
| `docs/incident-encoding-20261010/` | 一次源文件编码损坏事故的完整记录，与从 `.pyc` 重建的验证过程 |

## 八、环境

```
Python 3.12.10 · FastAPI 0.143 · uvicorn 0.54 · SQLite（WAL）
pytest 9.1.1 · Playwright 未使用 · 无前端构建链 · 无分布式压测
```

依赖全部**钉死**在 `requirements.txt`（全 `==`，版本取 `pip list --format=freeze`
的实测值）。理由很直接：这个平台卖的"数字可复现"，前提是依赖也复现 ——
`>=` 会让"今天能重现的数"和"下周能重现的数"跑在两套不同版本的库上。
代价是升级要显式改一行，这是有意为之：依赖变更应当是一次提交，
而不是 CI 某天早上自己发生的事。

一键脚本：`scripts/run.ps1`（Windows / PowerShell）与 `scripts/run.sh`
（Linux / macOS / Git Bash），任务与退出码语义一致。
覆盖率门槛、mypy / ruff 的严格度、锁文件生成方式，全部写在
`pyproject.toml` 与 `CONTRIBUTING.md` 里，不散落在 CI YAML 中。

---

## 九、工程化外壳（可被追问的部分）

> 这一节里的**所有数字都是在本机实测的**，与前面几节同一标准；
> 而**本机跑不了的三件事在表里明确标为"未验证"** ——
> 这不是遗漏，是这里的规矩：不给没有证据的绿灯。

### 1. 容器化

`Dockerfile`（`python:3.12-slim`）+ `docker-compose.yml`，三种用法用
profiles 隔开，因为跑压测是 CPU 独占的，和看板同时来会互相污染数字：

| 命令 | 做什么 |
|---|---|
| `docker compose run --rm suite` | 跑平台自身用例 + 覆盖率门槛 + 套件门禁 |
| `docker compose run --rm perf-gate` | 跑压测 + 基线劣化门禁（**退出码即结论**） |
| `docker compose up dashboard` | 起看板，宿主 `8765` 可访问 |

编排上的两个刻意选择：

- **不设 `PERF_ENV`**：容器内外默认行为必须逐字节一致，否则
  "同一份代码量出不同数"会变得无法解释；
- **结果库走命名卷**而非 bind mount 到 `./quality.db` ——
  绑一个尚不存在的**文件**路径时，Docker 会创建一个同名**目录**，
  然后 SQLite 报 `unable to open database file`。

> **未验证：本机未执行 `docker build`。** 开发机的 Docker daemon 起不来
> （WSL 报 `E_ACCESSDENIED`），所以这里只做到静态校验：
> `docker compose -f docker-compose.yml config --quiet` → 退出码 0。
> **首次真构建请在 daemon 可用的机器上执行 `docker build -t perf-gate .`。**

### 2. 覆盖率门槛

覆盖率第一次被真正接进 CI —— 在此之前装了 `pytest-cov` 却从未调用过。

| 口径 | 命令 | 实测 |
|---|---|---|
| 只算业务代码（**门槛口径**） | `--cov=bench --cov=metrics --cov=sut --cov=dashboard --cov=platform_cli` | **1950 stmts / 1082 miss / 45%** |
| 含 `tests/` 自身 | `--cov=.` | 3110 stmts / 1092 miss / 65% |

**门槛取 40**，理由：

- 低于实测 44，留 4 个点余量 —— 门槛贴着实测值时，任何一次无关改动
  都会让 CI 变红，而"稳定变红的门槛"唯一的结局是所有人学会忽略它；
- 也不取 30 以下 —— 那对一个 44% 的仓库存量等于没有门槛；
- README §6 引用的 **65%** 是另一个口径，**不要拿它和 40 比**，
  那等于拿"含自学题在内的高分"去比"专业课及格线"。

已知盲区（**不假装它们不存在**）：

| 模块 | 覆盖 | 为什么低 |
|---|---:|---|
| `sut/app.py`、`sut/db.py`、`sut/seed.py` | **0%** | SUT 跑在**子进程**里，coverage 默认不追踪子进程 |
| `metrics/pytest_plugin.py` | **0%** | 只在 `suite` 子命令起的 pytest 子进程里被加载 |
| `platform_cli.py` | **23%** | 压测主路径（`run` / `noise` / `prune`）要真起服务，用例只覆盖参数解析与纯函数 |
| `metrics/store.py` | **100%** | — |
| `sut/server_ctl.py` | **81%** | — |

提高覆盖率不是目标本身。把 `sut/*` 从 0% 拉起来需要子进程追踪方案
（`coverage run --parallel-mode` 之类），而那会改变压测进程的启动路径 ——
在一个性能平台上，"为了覆盖率改变被测进程的启动方式"是典型的指标反噬。

⚠️ 还有一个静默坑：写 `--cov` 时**必须重复写多个 `--cov=xxx`**。
`--cov=a,b` 这种逗号写法在 pytest-cov 7.1.0 下**不报错、只是收集不到数据**，
报告出 0% —— 一个看起来像"覆盖率极低"的真 bug。

### 3. 静态检查：ruff + mypy

两个工具此前都不存在（仓库里连 `pyproject.toml` 都没有）。
**先跑真实错误量，再定范围** —— 而不是先定一个漂亮的配置再想办法凑绿。

**ruff**：全默认规则集下报 **1600+ 条**，其中 **1642 条是
`RUF001/002/003`（ambiguous-unicode-character）** ——
这个项目通篇中文注释与中文报告，这三条规则会把全角标点全部判成问题。
所以 select 只开**能长期零误报、且真能抓 bug 的族**：

```toml
select = ["E4","E7","E9","F","B","C4","N","FURB","PIE",
          "RUF015","RUF022","A","T10","LOG","INT","ICN","ERA","TID","SLOT"]
```

`line-length = 120` 也是量出来的：同一份代码下 88 报 195 条 `E501`、
100 报 37、110 报 9、**120 报 3**、140 报 0。定 140 等于这条规则失效；
定 88 会产出上百条与"代码对不对"无关的噪声。剩下 3 条超出者全部落在
`metrics/analysis.py` 与 `metrics/store.py`（本次不动业务代码），
因此 `E501` 不进 select，并如实记在这里。

**mypy**：加 `--explicit-package-bases` 后全仓真实错误 **5 个**，
分布在 5 个文件；不加这个 flag 会直接报
`bench: error: Duplicate module named "__main__" (also at "metrics")`
并**一行有效结论都没有**。5 个错误里 4 个落在本次"只动工程化外壳"的
边界之外（`metrics/gate.py`、`metrics/analysis.py`、`dashboard/app.py`、
`sut/server_ctl.py`）→ 用 `[[tool.mypy.overrides]]` 的
**`disable_error_code` 逐模块按错误码登记**，而不是在源码里撒 `# type: ignore`：

| 模块 | 豁免的错误码 | 站点 |
|---|---|---|
| `metrics.gate` | `assignment` | `gate.py:297` |
| `metrics.analysis` | `union-attr` | `analysis.py:575` |
| `dashboard.app` | `arg-type` | `app.py:136` |
| `sut.server_ctl` | `attr-defined` | `server_ctl.py:66` |
| `platform_cli` | `assignment` | `platform_cli.py`（`rows_by_mode` 容空语义） |

这是**一份可枚举的欠债清单**，不是"假装没有类型"。每条豁免都做过
承重验证：把 `module` 改成一个不存在的名字，对应的错误立刻回来 ——
证明它豁免的确实是那一个站点，而不是把整模块静默关掉。

> 诚实边界：`disable_error_code` 是**模块级**的，无法精确到行。
> 同一个模块里将来出现同类错误也不会报 —— 这是这份配置已知的代价，
> 接受它的原因是"整模块 `ignore_errors`"（一开始的写法）代价更大：
> 那样连**别的**错误码也一起消失，`mypy` 会输出
> `Success: no issues found in 15 source files`，
> 而 5 个真实错误一个都没消失 —— 那正是这个仓库最反对的**静默放行**。

### 4. 依赖锁文件与漏洞扫描

- `requirements.lock` 由 `pip-tools` 从 `requirements.txt` 编译，
  含全部传递依赖（`annotated-doc`、`anyio`、`urllib3` 等）。
  ⚠️ 必须设 `PIP_TOOLS_CACHE_DIR` 指向仓库内：默认缓存到用户家目录，
  在受限环境里会报 `PermissionError: [WinError 5] 拒绝访问`。
- `.github/dependabot.yml`：`pip` + `github-actions` 两个 ecosystem，
  weekly；主版本升级被 `ignore` 挡住 —— 依赖主版本变更会让既有基线失效，
  这必须是人的一次显式提交，不能是 CI 某天早上自己发生的事。
- CI 里有一个 **`security-audit` job（非阻塞）**：pip-audit 的结论依赖
  漏洞库快照，同一个 commit 今天绿明天红是常态。做成阻塞项长期结果必然是
  有人加 `|| true` —— 门禁照样失效，只是没人再看得见。所以结论写进
  step summary 与 artifact，由人判断。

> **未验证：本机未执行 `pip-audit`。** 沙箱拒绝 `ensurepip`
> （`PermissionError: [WinError 5] 拒绝访问`），安装会挂起。
> 因此 `requirements-dev.txt` 里 `pre-commit` 与 `pip-audit`
> **刻意不写版本号** —— 未经实测的版本数字不写，而不是编一个看起来专业的。

### 5. pre-commit 与提交规范

`.pre-commit-config.yaml`：`pre-commit-hooks`（trailing-whitespace、
end-of-file-fixer、check-yaml/toml/json、mixed-line-ending、detect-private-key）
+ `ruff` + `mypy` + 两个 local hook（依赖清单一致性、提交信息规范）。
两个格式化钩子**排除** `docs/incident-encoding-20261010/` ——
那里的 `.corrupt` 文件是按字节保真的**事故证据**，被"顺手修好"就毁了。

提交信息走 Conventional Commits，**两条等价路线**：

| 路线 | 命令 | 何时用 |
|---|---|---|
| Python（默认） | `python scripts/check_commit_msg.py <msgfile>` | pre-commit 与 CI |
| Node | `npx --yes @commitlint/cli@19 --edit <msgfile>` | 团队本来就有 Node 时 |

本机 Node 是可用的（`node --version` → `v24.16.0`，`npx --version` → `11.13.0`），
所以 `commitlint.config.mjs` 也一并给了。**默认仍走 Python**：
这是个纯 Python 仓库，CI 的五个 job 没有一个装 Node，
为一个提交信息检查把"跑一次门禁"的依赖面从 Python 变成 Python + Node
不划算。两条路线的规则必须保持等价，否则会出现"同一个提交在我机器上过、
在你机器上不过"。

### 6. CI 强化

| 变化 | 为什么 |
|---|---|
| 三个 job 加 `cache: pip` | 每次 CI 重装全部依赖是纯浪费；缓存 key 依赖两个 requirements 文件 |
| 五个 job 加 `timeout-minutes` | 默认 360 分钟。压测一旦卡住（服务起不来、端口被占），没人盯着就是白烧 6 小时 |
| 加 `schedule`（`17 3 * * 1`，每周一凌晨） | 缺陷不会等到有人推代码才出现（依赖漂移、runner 镜像更新、系统时钟/调度行为变化）。**没有定时回归的性能门禁只能证明"我推代码那天没退化"** |
| 加 `concurrency` | 同一个分支连推两次时，取消旧的那次 —— 但 **schedule 不参与取消**（夜间回归被白天的 push 顶掉是最常见的"门禁悄悄消失"） |
| `permissions: contents: read` | 最小权限 |
| 新增 `lint` job 作为其余 job 的前置 | 静态检查不过就没必要跑 30 分钟的压测 |
| 失败通知用 `if: failure()` 写 `$GITHUB_STEP_SUMMARY` | **不需要任何 secret**。webhook / 邮件方案只在 workflow 注释里留了说明 —— 引用一个不存在的 secret 会让整个 workflow 报错，比没有通知更糟 |

### 7. 环境分层

```powershell
$env:PERF_ENV="dev"                                    # dev / staging / prod / 不设
& ".\.venv\Scripts\python.exe" platform_cli.py env     # 看当前生效了什么
```

档案在 `scripts/envs/{dev,staging,prod}.env`，语法只有 `KEY=VALUE`
（**刻意不支持** shell 展开：档案是配置不是脚本，允许 `$(...)` 就等于允许
"同一条命令在不同机器上跑出不同环境"）。三条不变量：

1. **不设 `PERF_ENV` 时行为与引入分层之前逐字节一致** —— 不读、不写任何变量。
   所以 CI 的三个 job 一行都不用改，既有基线也不会因为分层而失效。
2. 档案**不覆盖已显式设置的环境变量**（`setdefault` 语义）：
   命令行上 `$env:PERF_SUT_ITEMS="500"` 的意图比档案默认值更强。
3. 档案里**只写与默认值不同的变量** —— 默认值定义在代码里，重复的那份迟早漂移。

### 8. 协作模板

`CONTRIBUTING.md`（三条不可让步的纪律 + 覆盖率/依赖/环境分层/CI 的口径）、
`CODEOWNERS`、`.github/PULL_REQUEST_TEMPLATE.md`、
`.github/ISSUE_TEMPLATE/{bug_report,feature_request}.md` + `config.yml`、
`CHANGELOG.md`（**基于 6 条真实提交写，不编造版本号** —— 这个仓库从未打过 tag）。

> **未验证：本机未执行 `pre-commit run`。** 同 §4 的原因，pre-commit 装不上。
> 配置文件、钩子版本与 `stages` 都已写好，但**只有 CI 能证明它们能跑**。
