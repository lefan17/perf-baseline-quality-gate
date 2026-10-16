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

### 修复

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
  `135 passed / ~46s / ~50s` → **175 passed / 82s**；`README.md` 的用例表
  从 6 行补到 9 行（新增 `test_r3_end_to_end.py` / `test_rules_single_source.py` /
  `test_baseline_snapshot.py`，合计 175）；覆盖率双口径
  `1829 stmts / 1043 miss / 43%` → **1950 / 1082 / 45%**，
  `--cov=.` `2597 / 1046 / 60%` → **3110 / 1092 / 65%**。
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
