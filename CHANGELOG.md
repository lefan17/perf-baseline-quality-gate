# 变更日志
#
# 本文件**只记录已经发生的事情**：每一条都对应一个真实提交，
# SHA 可在 `git log` 里查到。没有发布日期、没有版本号 ——
# 这个仓库从未打过 tag，也没有发布流程。
# 编一套 v0.1.0 / v0.2.0 出来会让"版本"这个词在这里变得不可信。
#
# 格式参考 Keep a Changelog，但**不声明**遵循它（我们没打 tag，
# 声明了也无法满足它对版本语义的要求）。语义化提交类型沿用
# Conventional Commits：feat / fix / docs / refactor / perf / ci / chore。
#
# 新增条目请写在最上面，并在 CI 里由 scripts/check_commit_msg.py 保证
# 提交信息本身是可解析的（否则这份日志没法自动生成）。

## 未发布

### 新增（真 MySQL 写路径）

- **接一台真 MySQL，让「连接池」不再只是 SQLite 的类比。**
  `docs/诚实边界.md` 里那条自我限制（SQLite 单写者 ⇒ 写路径数字没有参考价值，
  `stock_write` 66 RPS / P95 403.9ms）现在有了反证：同一个写动作
  （`+1` 再 `-1`，两次往返算一个动作）只换存储，SQLite P95 **346.49ms / 69.7 RPS**，
  MySQL P95 **65.80ms / 189.8 RPS** → **P95 0.19×、RPS 2.72×**。
  **池没有被替换**：`sut/pool.py` 原样复用，只把 factory 从
  「打开一条 sqlite3 连接」换成 `pymysql.connect()`。驱动必须用库
  （没人为了演示池手写 MySQL 协议），池必须自己写。
- **`sut/mysql_store.py`**：`SELECT … FOR UPDATE` 行锁 → 更新 → 记流水，一个事务；
  与 SQLite 侧 `BEGIN IMMEDIATE`（整库写锁）形成对照，**同样的防超卖在两套存储上
  不是同一种机制**，这是两套数字不同的原因。
- **池新增 `discard(conn)`：连接会坏，这件事是换真数据库才出现的。**
  `wait_timeout` / 服务端重启 / 网络闪断会让连接"半死"；把它 `checkin()` 回队列
  的后果是下一个人拿到它、报错、再还回去 —— 一条坏连接能在池里活很久，
  表现为「偶发 500、重跑就好」。`discard` 只释放名额，**不放进空闲队列**。
- **三条状态码映射**：连不上 → 503 + `Retry-After`；item 不存在 → 404；
  扣成负数 → 409 并回滚。混成 500 会让值班的人去翻代码，而事实是数据库不可用。
- **`scripts/mysql_probe.py`**：写路径对照探针，MySQL 连不上时以 0 退出并打印
  怎么起容器 —— 与 `proxy_probe.py` 同样的「附加证据，不成为门槛」。
- `tests/test_mysql_store.py` **13 条**（探测不到 MySQL 整组 skip）：建表幂等、
  写读往返、404、409（并验证真的回滚了）、池复用、连不上抛
  `MySQLUnavailableError`、`discard` 不回收坏连接、两个新路由与诊断接口。
  这批用例第一次跑就抓到两个真 bug：`ping()` 在 `try` 里 `return` 导致连接
  泄漏（`in_use` 永远下不来）、`ensure_schema()` 的建连失败没被映射成
  `MySQLUnavailableError`（会漏成 500）。
- **依赖**：新增 `PyMySQL==1.1.1`，同时写进 `requirements.txt` 与
  `pyproject.toml [project].dependencies`（由 `check_deps_sync.py` 逐字校验）。

### 变更（CI）

- **`platform-tests` job 显式安装 nginx，并跑一次三口径探针。**
  这不是「加了新能力」，而是**去掉对 runner 镜像的隐含依赖**：`ubuntu-latest`
  目前自带 `/usr/sbin/nginx`，反向代理集成用例
  （`test_proxy_returns_the_same_payload_as_direct`）本来就能跑（suite 一直是
  `通过 318 / 跳过 0`）；镜像哪天不带了，它会**安静地变成 skip**。现在显式
  `apt-get install nginx`，依赖写在 workflow 里而不是猜。
  另加一步跑 `scripts/proxy_probe.py --items 5000 --users 10 --duration 5`，
  数字贴进 step summary，**不断言比值**（共享 runner 上 1.21x / 1.70x 抖动太大）。
- **修一个自己造出来的假绿**：探针那一步第一版没设 `PYTHONPATH`（`python scripts/x.py`
  的 `sys.path[0]` 是 `scripts/`，`from bench.engine import ...` 直接
  `ModuleNotFoundError`），又用了 `python … | tee` 而没开 `pipefail`——退出码取
  tee 的 0，**步骤报 success，日志里躺着一段 Traceback**。现在 `env: PYTHONPATH: .`
  + `set -euo pipefail` + `grep -q "直连 SUT"` 三重保险。

### 新增（连接池与反向代理探针）

- **自实现一个连接池，并让「池耗尽」以 503 而不是 500 现身。**
  `sut/pool.py`（`ConnectionPool` / `PoolExhaustedError`）不引第三方池：
  池的语义只有几十行（限流、复用、获取超时、归还），而依赖清单由
  `check_deps_sync.py` 逐字校验，为一个概念吞一棵依赖树不划算。
  `sut/db.py` 里用 `_PooledConnection` 把 `sqlite3.Connection` 包了一层
  （`close()` 的语义改成「归还」、`row_factory` 转发到底层），所以**九个既有路由
  一行没改**就走上了池。池默认关闭，既有基线口径不受影响。
  两个刻意的设计：**拿不到连接抛异常而不是返回 None**（返回 None 会让每个调用点
  都得判空，漏一次就是 AttributeError → 500）；**`PoolExhaustedError` 映射成
  503 + `Retry-After: 1`**（500 会被当成 bug 去回滚，503 才表示「接不住、要扩容」）。
  `/api/_diagnostics/pool` 同时暴露 `saturation` 与 `timeouts_total` ——
  只报错误率分不出「池满了」和「代码挂了」：前者失败请求耗时很短，
  **P95 反而会「变好」**，这是压测报告里最经典的骗人组合。

- **反向代理探针：多一跳代理、以及上游连接复不复用，各值多少。**
  `scripts/proxy_probe.py` 三个口径打同一个后端（20 000 条数据 / 20 并发 /
  每个口径 8 秒、丢弃前 1 秒热身）：直连 P95 **44.78ms** / RPS **896.3** /
  错误率 0.00%；经 nginx 且上游 keep-alive（`proxy_http_version 1.1` +
  `Connection ""`）P95 54.07ms / RPS 573.4 / 0.00%；经 nginx 但上游每次新建
  （`1.0` + `Connection close`）P95 **76.34ms** / RPS **482.0** /
  错误率 **18.67%**。同一份配置只差两行指令，错误率从 0 变成 18.67% ——
  「代理没开 keep-alive 会被误判成服务端扛不住」的最小可复现例子。
  低并发（5000 条 / 10 并发）下这个差小得多（P95 1.10× / 1.19×，且无错误），
  所以真正的风险区间是**上游连接数被并发推高之后**，这一点也写进了 README。
  没有 nginx 时探针打印安装提示并以 0 退出：它不该成为跑仓库的门槛。

- **`start_sut(host=...)`**：默认仍是 `127.0.0.1`，反向代理探针需要对外时
  显式传 `0.0.0.0`。加这个参数是因为先试过 Docker 容器反代 —— 容器里
  `host.docker.internal` 解析得到却回连不到宿主的 `127.0.0.1`。

### 新增（缓存与索引探针）

- **接入一台真 Redis，并补上三条最容易出事的缓存失效路径。**
  sut/redis_client.py 是纯标准库的 RESP2 客户端（不引 `redis` 包：依赖清单
  由 check_deps_sync.py 逐字校验，而压测引擎本来就要求每 worker 自持连接）；
  sut/cache.py 是缓存层，GET /api/items/{id}/detail 支持 cache=on|off。
  被单独盯住的三条：**穿透**（负缓存哨兵 __PERF_MISS__）、**一致性**
  （失效放在 commit 之后，否则并发读会把旧值写回）、**降级**
  （Redis 连不上时接口照常 200，且 /api/_diagnostics/cache 的 `backend`
  会写成 `none` —— 降级不允许静默发生）。CI 的 platform-tests 用
  service container 起 `redis:7-alpine`；连不上时那批用例自动 skip。
  实测（5000 条数据 / 10u / 8s / 同一个 500 条热点集）：
  cache=on P95 **16.8ms**（5520 次）vs cache=off P95 **36.9ms**（4593 次），**2.2×**。

- **索引失效探针：把「本该快」做成可断言的东西。**
  GET /api/items 新增 index=false，把过滤列包进 substr() 让索引失效。
  `tests/test_index_probe.py`（4 条）在 SQL 层断言两件事：执行计划必须一个是
  SEARCH、一个是 SCAN；全表扫描必须比索引查找慢 5 倍以上（实测 10 万行下
  **0.218ms vs 6.337ms，29×**）。
  **刻意不做端到端断言**：同样的 29 倍，套上 HTTP 后单请求固定开销约 25ms
  会把 6ms 压成 10% 抖动，而单机压测两轮之间本来就有 3% 量级 CV —— 在测不出来的
  地方不假装测得出来，这是 docs/诚实边界.md 同一条原则。

- **四个探针场景进 REGISTRY 但不进 DEFAULT_MIX**：item_detail_cached /
  item_detail_nocache / items_by_category_indexed / items_by_category_fullscan。
  理由：check_comparability 只看 scenario="(mix)" 这个字符串，看不出混合比例
  已经变了；探针场景一旦混进默认压测，历史基线就悄悄不可比了。


### 修复

- **CI 上的 R3 降级为"只报数不拦人"（`--r3-advisory`），并把噪声量出来留档。**
  基线接上之后 R3 确实开始判了，但立刻暴露出**判据被噪声淹没**：同一提交、
  同一配置、同一台 runner 连跑 4 轮，R3 一次 PASS 三次 FAIL，红的项每次都不一样。
  从 runner 上传的 `ci-quality-db` 直接读出：单轮 CV p95 11.7% / **p99 39.0% /
  max 54.1%**，而容忍带 10% 是在**开发机按 P95**定的（`CV_diff ≈ 4.40%` → 2.27σ）。
  到 runner 上容忍带对 p99 / max 只剩 **0.18σ / 0.13σ** —— 每轮必然越带。

  处置：`evaluate_perf` 新增 `r3_advisory`（默认关闭），`run` / `gate` 新增
  `--r3-advisory`，CI 的 `run` 显式带上。R3 照样算、照样把劣化项与百分比
  打印进报告、照样入库，只是 `severity` 由 blocking 降为 warning，不进 `blocked`。
  **`gate_rules.yaml` 的阈值一个字没改**，快照也没有为了变绿而重设 ——
  那是"调参直到它说 PASS"。R1（错误率）与 R2（绝对 P95）仍然 blocking。
  代价如实记录：CI 上的 R3 从此不会让流水线变红，"慢慢变慢"在 CI 上
  仍然看不见。完整数字、另三条被否决的路、以及允许降级的四条前提，
  见 `docs/已知问题.md` P-16 与 `CONTRIBUTING.md` §1b。

- **让 R3 在流水线上真的判过一次。**这个平台的核心价值是抓"慢慢变慢"，
  而线上（run `38055102821`）每一次性能门禁的日志都是
  `[PASS] R3-baseline-degradation: 未设置基线，跳过劣化对比` ——
  门禁从未生效，报告却全绿。两个缺陷叠在一起：

  1. 「恢复基线」步下载 artifact `quality-db-and-reports`，而它是**同一个
     run 自己**的 `platform-tests` job 刚上传的（`if: always()`）——
     跨 job 拿不到上一轮的库，`baselines` 表恒空。
  2. R3 在 `baseline is None` 时写 `passed=True` 且不置 `skipped`，
     于是"没判定"被打成 `[PASS]`，和"比过了、没劣化"长得一模一样。

  修法：新增 `metrics/baseline.py` 与 `baseline export/import`，
  基线以 `baselines/<label>.json` 快照的形式**提交进仓库**
  （不需要 secret、不需要给 CI `contents: write`，且谁在哪次提交里动了
  基线 `git log` 能回答）；`perf-gate` 在压测前 `baseline import`；
  R3 无基线时改为 `passed=False` + `skipped=True` → `[SKIP]`，
  与 P-13 的"不可比"、P0、S1 共用同一套语义。详见 `docs/已知问题.md` P-15。

  既有断言同步更新：`test_missing_baseline_is_a_warning_not_a_block` →
  `test_missing_baseline_is_a_skip_not_a_pass`；`test_r3_end_to_end.py` 里对
  无基线那一行从断言 `[PASS]` 改成断言 `[SKIP]`。
  **门禁阈值一个字没动。**

- **`baseline export/import`**：把本机那条基线写成 `baselines/<label>.json`，
  再由人提交进仓库 —— CI 的干净环境靠它拿到基线。快照带 `schema` 版本号，
  读到不认识的版本**必须拒绝**（"猜着读"会安静地比出假的"没劣化"）；
  缺文件 / 损坏 / label 不符 / 配置不可比四种边界都有确定行为，
  **没有一种是安静放行**。CI 的「恢复基线」步因此不再需要 artifact，
  也不再需要 `continue-on-error`。详见 `docs/已知问题.md` P-15。

### 文档

- 修正测试数与耗时的过期口径（实测，命令见 `CONTRIBUTING.md`）：
  `135 passed / ~46s / ~50s` → **180 passed / 85s**；`README.md` 的用例表
  从 6 行补到 9 行（新增 `test_r3_end_to_end.py` / `test_rules_single_source.py` /
  `test_baseline_snapshot.py`，合计 180）；覆盖率双口径
  `1829 stmts / 1043 miss / 43%` → **1964 / 1088 / 45%**，
  `--cov=.` `2597 / 1046 / 60%` → **3165 / 1098 / 65%**。
  `README.md` 的「GitHub runner 实测」表是**时点快照**，数字一个字没改。
- `docs/度量口径.md`：`R3` 无基线时的描述从「PASS + WARNING」改为
  「`[SKIP]` + WARNING，不是 PASS 也不是 FAIL」，并写清为什么
  "跳过"必须在报告里长得和"通过"不一样。

### 新增（工程化外壳）

一次补齐十项工程化能力，**不改业务逻辑、不改既有测试断言、不动任何门禁阈值**。

- **容器化**：`Dockerfile`（`python:3.12-slim`）+ `docker-compose.yml`
  （`suite` / `perf-gate` / `dashboard` 三种用法用 profiles 隔开）+ `.dockerignore`。
- **覆盖率门槛**：CI 新增独立的覆盖率步骤。口径是**只统计业务代码**
  （`bench` / `metrics` / `sut` / `dashboard` / `platform_cli`），实测 43%，
  门槛设为 40。含 `tests/` 的 60% 口径仍然保留（README 引用的是它），
  但**不用它当门槛** —— tests 自己也进分母且几乎全被执行，拿它给自己打分是稀释问题。
- **类型检查**：新增 `pyproject.toml` 承载 `[tool.mypy]`。实测全仓 5 个错误、
  分布在 5 个文件，其中 4 个落在本次边界外。采用**按错误码逐模块豁免**
  （`disable_error_code`）而非整模块 `ignore_errors`，也不用 `# type: ignore`。
- **ruff**：新增本仓库此前没有的静态检查，`select` 范围在 `pyproject.toml`，
  CI 里真的在跑（`ruff check .` 全绿）。
- **依赖漏洞扫描**：`.github/dependabot.yml`（pip + github-actions，weekly）
  + CI 里的 pip-audit 步骤（**非阻塞**，结论写入 step summary）。
- **依赖锁文件**：`requirements.lock`（pip-tools 生成，94 行，含全部传递依赖）
  + 新增 `requirements-dev.txt` 与 `scripts/check_deps_sync.py`
  （防止 `pyproject.toml` 与 `requirements*.txt` 漂移）。
- **提交前钩子**：`.pre-commit-config.yaml`。
- **提交信息规范**：`scripts/check_commit_msg.py`（默认路线，Python）
  与 `commitlint.config.mjs`（Node 路线，规则等价）。
- **协作模板**：`CONTRIBUTING.md`、`CODEOWNERS`、`CHANGELOG.md`、
  `.github/PULL_REQUEST_TEMPLATE.md`、`.github/ISSUE_TEMPLATE/`。
- **CI 强化**：三个 job 全部补 pip 缓存与 `timeout-minutes`；
  新增 `schedule`（每周一 03:17 UTC 夜间回归）与 `concurrency`；
  新增 `lint` 与 `security-audit` 两个 job；
  失败时用 `if: failure()` 写 step summary 通知（**不需要任何 secret**）。
- **环境分层**：新增 `PERF_ENV` 选择 `scripts/envs/{dev,staging,prod}.env` 档案
  （+ `platform_cli.py env` 子命令查看当前生效变量）。
  **不设 `PERF_ENV` 时行为与引入之前逐字节一致**。
- **一键脚本**：`scripts/run.ps1` + `scripts/run.sh`，把 README 的手敲命令固化。

### 修复

- `598d27c` 写路径探针（`tests/test_sut_api.py::test_probe_write_path_slower_than_read_path`）
  改为**双判据**，消除 CI 上因固定成本占比摇摆导致的偶发红。判据一改成比较
  **扣除固定成本后**的数据库工作量（`(write - health) > (read - health) * 3`，
  固定成本用同一窗口的 `/healthz` 量出），阈值仍是 3；判据二是新增的
  **并发不变量**（6 线程 × 10 次 +1，失败响应数为 0 且库存精确 +60）。
  变异检验：把 `sut/app.py:210` 的 `BEGIN IMMEDIATE` 换成延迟锁 `BEGIN` 后，
  旧耗时候选全部照样"通过"（4.63× → 4.54×），而新判据 **EXIT=1** 报
  "并发写出现 33 次非 201 响应"。详见 `docs/已知问题.md` P-3。

### 未验证（如实记录，不要当成已完成）

- 容器镜像**未在本机构建过**：开发机 Docker daemon 起不来
  （WSL 报 `E_ACCESSDENIED`）。只做了 `docker compose config --quiet` 静态校验。
- `pre-commit` 与 `pip-audit` **未在本机执行过**：沙箱拒绝 `ensurepip`，
  报 `PermissionError: [WinError 5] 拒绝访问`。两者仅 CI 可验证。
- 上述两项在首次真跑之前，不能算"已验证"。

## 2026-10-13

### 文档

- `2a1741c` 统一单请求耗时口径为「复用连接 + 热身 + 多轮取中位」。
  旧口径"每请求新建连接 + 不热身"会给每个接口加一笔固定成本，
  在快接口上占比大、慢接口上占比小，**系统性地压低比值** ——
  于是它把一个真实的 N+1 缺陷稀释成了 1.02×。该口径不再作为
  并列结论出现在任何表里，只保留为教学案例。

### 修复

- `8fc029b` 修统计口径缺陷（尾比合并、容忍带论证、基线可比性）并补三处工程缺口。
  其中容忍带的 2.27σ 论证（CV 3.11% / CV_diff 4.40%）是"10% 这个阈值
  从哪来"的答案 —— 在此之前它是个凭感觉定的数。

- `ed2b881` 补齐 `prune` 子命令并修复门禁静默放行（P0/P1 六项）。
  「静默放行」指门禁在某条规则无法判定时**默认放行**，
  这比误报危险得多：误报会被修，静默放行会被当成绿灯。

## 2026-10-10

### 文档

- `8f07a6f` 记录 GitHub Actions 实测结果与 CI 徽章
  （run `38055102821`，ubuntu-24.04，117/117 用例）。

- `70bd2f1` 记录编码事故并补文档索引；看板重建后经 AST 常量比对与端到端 HTTP 验证。
  事故证据逐字节保留在 `docs/incident-encoding-20261010/`，
  并由 `.gitattributes` 的 `*.corrupt -text` 禁止任何换行符转换。

### 新增

- `f1df416` 初始提交：性能与稳定性基线平台 + 质量度量与 CI 门禁平台。
  - 自研压测引擎（不依赖 Locust）
  - 自建被测服务 SUT（不打公网）
  - 12 条门禁规则（R1-R5 / P0-P3 / S1-S3）
  - `platform_cli.py` 十一个子命令
  - 看板（零前端构建链）
  - `metrics/pytest_plugin.py`：把用例结果直接写进 `quality.db`
