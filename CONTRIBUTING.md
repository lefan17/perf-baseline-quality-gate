# 贡献指南

这是一个**性能与质量门禁**平台。它的全部价值建立在「数字可复现」上，
所以这里的规矩比普通项目严一些：宁可少一个功能，也不要一个说不清来源的数字。

---

## 一、五分钟上手

```powershell
# 1. 虚拟环境（必须用仓库自带的 .venv；系统 Python 缺 PyYAML 会直接报 ModuleNotFoundError）
python -m venv .venv
& ".\.venv\Scripts\python.exe" -m pip install -r requirements-dev.txt

# 2. 一键脚本（把下面几条命令都包好了）
pwsh -File scripts\run.ps1 -Task help
pwsh -File scripts\run.ps1 -Task test      # 跑 175 条用例
pwsh -File scripts\run.ps1 -Task lint      # ruff + mypy + 依赖一致性
pwsh -File scripts\run.ps1 -Task perf      # 一轮真压测 + 门禁（约 1 分钟）

# 3. 提交前钩子（可选；本仓库默认路线是 Python，不需要 Node）
python -m pre_commit install
```

> `$env:PYTHONIOENCODING="utf-8"` 在 Windows 上不是可选项：
> 控制台默认 GBK，中文报告会直接打崩。一键脚本里已经设好了。

---

## 二、三条不可让步的纪律

### 1. 不许用一个更宽松的阈值让红灯变绿

门禁红了只有四种正当处理方式：

1. **修代码** —— 最常见也最正确；
2. **重设基线** —— 当确认是环境漂移而非代码劣化（`baseline set`，需人显式执行）；
3. **调整阈值并给出论证** —— 像现有的 10% 容忍带那样，附上 `noise --rounds 5`
   量出的运行间波动，说明新阈值对应几个 σ；
4. **标记为已知问题** —— 写进 `docs/已知问题.md`，**保留红色**。

**没有**第五种叫「把它调大」。一条永远不会失败的规则等于没有规则，
而一条被随手调大的规则更糟：它让人以为有人在看着。

### 2. 不许用 `# type: ignore` / `# noqa` 把静态检查刷绿

这个仓库的原则是：**欠债要可枚举**。所以豁免写在 `pyproject.toml` 里
（`[tool.mypy.overrides]` 的 `disable_error_code`、`[tool.ruff.lint.per-file-ignores]`），
那里是一份能被 review 的清单；撒在源码里的 `ignore` 则没人知道还剩多少真问题。

确有必要在源码里写豁免时，**必须**带上错误码与理由：

```python
x = cast(list[dict], y)  # type: ignore[assignment]  # 上游 API 返回可空，契约见 docs/度量口径.md §3
```

### 3. 不许在没有证据的情况下写数字

README 里每一个数字都是实测的。改文档时：

- 新增的数字必须附**得到它的完整命令**；
- 已存在的数字**不许改**，除非你自己重跑并给出新输出；
- 尤其是这些**有论证的**数字，改动要先读懂它们的论证：
  - `gate_rules.yaml` 的 `slow_report_p95_ms_max: 150.0` / `n1_over_agg_max: 10.0`
  - README 里的 2.27σ 容忍带论证、7.81× 比值、175 passed、12 条门禁规则

---

## 三、测试纪律

```powershell
& ".\.venv\Scripts\python.exe" -m pytest tests/ -q          # 175 passed，约 82 秒
```

- **不许删测试或改断言**来让门槛达标。门槛是为了暴露问题，不是为了通过。
- 改门禁/度量逻辑时，补的用例必须**在改之前是红的** ——
  否则它测不到这次改动。
- **不引入 `pytest-xdist`**。这是刻意的设计决策，不是遗漏：
  平台用例里有真实压测，并行会争抢 CPU 与端口，把"被测对象"和
  "干扰源"混在一起 —— 在一个性能项目里，"为了快"的优化是负收益。
  详见 `requirements.txt` 的说明与 README §3 决策 ③。

---

## 四、覆盖率门槛

```powershell
& ".\.venv\Scripts\python.exe" -m pytest tests/ -q `
    --cov=bench --cov=metrics --cov=sut --cov=dashboard --cov=platform_cli `
    --cov-report=term-missing --cov-fail-under=40
```

- 门槛 40 对应的是**只算业务代码**的口径（实测 44%）。
- README §6 引用的 **65%** 是另一个口径（`--cov=.`，含 `tests/`）。
  两个数都对，但它们回答的不是同一个问题 —— **不要拿 65% 去和 40 比较**，
  那是拿"含自学题在内的高分"去比"专业课及格线"。
- ⚠️ 写 `--cov` 时必须重复写多个 `--cov=xxx`，**不能**写 `--cov=a,b`：
  本版本（pytest-cov 7.1.0）下逗号写法**静默收集不到数据**，报告出 0% 而不是报错。

### 已知盲区（不要假装它们不存在）

| 模块 | 覆盖率 | 为什么低 |
|---|---:|---|
| `sut/app.py` / `sut/db.py` / `sut/seed.py` | 0% | SUT 跑在**子进程**里，coverage 默认不追踪子进程 |
| `metrics/pytest_plugin.py` | 0% | 只在 `suite` 子命令起的 pytest 子进程里被加载 |
| `platform_cli.py` | 23% | 压测主路径（`cmd_run` / `cmd_noise` / `cmd_prune`）要真起服务，用例里只覆盖了参数解析与若干纯函数 |

**提高覆盖率不是目标本身。** 把 `sut/*` 从 0% 拉起来需要
`coverage run --parallel-mode` 之类的子进程追踪方案，
而那会改变压测进程的启动路径 —— 在一个性能平台上，
"为了覆盖率改变被测进程的启动方式"是典型的指标反噬。

---

## 五、提交信息规范

采用 Conventional Commits，两条等价路线：

```powershell
# 默认路线（Python，CI 用的就是它）
python scripts\check_commit_msg.py .git\COMMIT_EDITMSG

# Node 路线（本机实测有 Node：v24.16.0 / npx 11.13.0）
npx --yes @commitlint/cli@19 --edit .git/COMMIT_EDITMSG
```

格式：`<type>(<scope>): <subject>`

| type | 用于 |
|---|---|
| `feat` | 新能力 |
| `fix` | 修缺陷 |
| `docs` | 只改文档 |
| `refactor` | 不改行为的重构 |
| `perf` | 性能相关 |
| `test` | 只改用例 |
| `ci` / `build` / `chore` | 工程化外壳 |
| `revert` | 回滚 |

约束：type 必须小写且在上面列表内；标题 ≤ 100 字符
（默认的 72 对中文过严 —— 一个汉字占两列，72 列只够 36 个汉字，写不清"为什么"）；
标题与正文之间空一行；subject 是短语不是句子，不以标点结尾。

> 标题写**为什么**，不要写"改了什么"。
> `fix: 修 bug` 在半年后的 `git log` 里等于没写。

**两条路线的规则必须保持等价**：改 `commitlint.config.mjs` 就要同步改
`scripts/check_commit_msg.py`，否则"同一个提交在我机器上过、在你机器上不过"。

---

## 六、依赖管理

- 运行时依赖的唯一事实源是 **`requirements.txt`**，全部用 `==` 钉死
  （理由是"数字可复现"）。
- 开发工具链在 **`requirements-dev.txt`**。
- `pyproject.toml` 的 `[project].dependencies` 与它们**必须逐字一致**，
  由 `scripts/check_deps_sync.py` 在 pre-commit 与 CI 里强制校验。
  **两处必须一起改** —— 依赖清单漂移会产出最危险的一种状态：
  CI 是绿的，但别人 `pip install .` 装出来的是另一套版本。
- 改动运行时依赖后重新生成锁文件：

  ```powershell
  $env:PIP_TOOLS_CACHE_DIR="$PWD\.pip-tools-cache"
  & ".\.venv\Scripts\python.exe" -m piptools compile requirements.txt --output-file requirements.lock --strip-extras
  ```

  （`PIP_TOOLS_CACHE_DIR` 不是可选的：pip-tools 默认缓存到家目录，
  在某些受限环境里会被拒绝写入 `PermissionError: [WinError 5]`。）

- **依赖升级会让既有基线失效。** 升级 PR 里如果只有"bump 某包，全绿"，
  那不说明性能没变，只说明性能门禁这一轮没跑或没红。
  主版本升级由人显式决定（见 `.github/dependabot.yml` 的 `ignore` 段）。

---

## 七、环境分层

```powershell
$env:PERF_ENV="dev"          # 或 staging / prod / 不设
& ".\.venv\Scripts\python.exe" platform_cli.py env   # 查看当前生效的变量
```

- 档案位于 `scripts/envs/*.env`，语法只有 `KEY=VALUE`（**刻意不支持** shell 展开：
  档案是配置不是脚本，允许 `$(...)` 就等于允许"同一条命令在不同机器上跑出不同环境"）。
- 档案**不覆盖已经显式设置过的环境变量**（`setdefault` 语义）：
  命令行上的 `$env:PERF_SUT_ITEMS="500"` 意图比档案默认值更强。
- **不设 `PERF_ENV` 时，平台行为与引入分层之前逐字节一致** ——
  没有读也没有写任何变量。CI 的三个 job 因此不需要改。
- 档案里**只写与默认值不同的变量**。默认值定义在代码里，不在这里重复 ——
  重复的那一份迟早会漂移。

---

## 八、CI

`.github/workflows/quality-gate.yml` 有五个 job：

| job | 作用 | 会拦住合并吗 |
|---|---|---|
| `lint` | ruff + mypy + 依赖一致性 | ✅ 会 |
| `security-audit` | pip-audit 依赖漏洞扫描 | ❌ 不会（**非阻塞**，结论写 step summary） |
| `platform-tests` | 跑用例 + 套件门禁 + 覆盖率门槛 | ✅ 会 |
| `perf-gate` | 压测 + 基线劣化门禁 | ✅ 会 |
| `n1-probe` | 缺陷探针（**期望它红**） | ❌ 不会 |

关键设计：**判定逻辑不在 YAML 里，在平台里**。
workflow 只负责调用和读退出码，阈值散落在 CI 配置里是门禁最常见的退化方式。

`security-audit` 为什么非阻塞：pip-audit 的结论依赖漏洞库快照，
同一个 commit 今天绿明天红是常态。把它做成阻塞项，长期结果一定是
有人加 `|| true` —— 门禁照样失效，只是没人再看得见。
所以结论留在 step summary 里，由人判断。

---

## 九、文档里的数字怎么改

改 README / docs 里任何数字前，先读 `docs/度量口径.md`（口径定义）
与 `docs/诚实边界.md`（哪些结论不能外推）。

提交时请贴：

```
命令：<完整命令，含 venv 调用方式>
输出：<原始输出，不要只写结论>
口径：机器 / Python 版本 / 热身次数 / 样本数 / 是否复用连接
```

**如果数字变了但代码没变，说明原因**（环境漂移 / 依赖升级 / 机器不同）。
"数字悄悄变了但没人解释"是基线失效的开始。

---

## 十、本机无法验证的事（重要）

这个项目的开发环境有两处硬限制，**不要**把它们当成"应该能跑但没跑"：

| 能力 | 状态 | 原因 |
|---|---|---|
| `docker build` / `docker compose up` | **未验证** | 开发机 Docker daemon 起不来（WSL 报 `E_ACCESSDENIED`）。只能做 `docker compose config --quiet` 静态校验。 |
| `pre-commit run` | **未验证** | 沙箱拒绝 `ensurepip`（`PermissionError: [WinError 5]`），pre-commit 装不上。 |
| `pip-audit` | **未验证** | 同上，安装会挂起。 |

提交涉及这些能力的 PR 时，**必须在 PR 模板第五节写明「本机未验证」**。
声称验证过一件没验证的事，比明说没验证危险得多 ——
这个仓库存在的全部意义就是"不给出没有证据的绿灯"。
