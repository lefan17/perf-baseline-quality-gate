# 安全策略

这是**性能与稳定性基线平台 + 质量度量与 CI 门禁**：内置被测服务（`sut/` 含一处**故意保留**的
N+1 性能缺陷）、自研压测引擎、基线劣化对比与门禁规则引擎。只在本机与 CI runner 上跑，不部署、
不处理真实用户数据，所以这里的"安全"主要指依赖链与扫描可信度。

## 支持的版本

只维护 `main` 的最新提交；历史提交与 fork 不接收修复。没有发布版本（`gh release list`
为空），也没有任何 tag —— `CHANGELOG.md` 按真实提交写，不编造版本号。

## 怎么私下报告

走 GitHub Security Advisories：<https://github.com/lefan17/perf-baseline-quality-gate/security/advisories/new>
**不要开公开 issue**——本仓库没有安全邮箱，Advisory 是唯一渠道，修复发布前对公众不可见。

## 我们怎么回应

3 个工作日内确认收到；确认后 7 个工作日内给出初步判断（能否复现、影响哪一部分、怎么修）。
修好后是否署名由报告者决定。

## 依赖漏洞

- `Dependabot` 覆盖 `pip` 与 `github-actions`，每周一 03:30（Asia/Shanghai），只开 PR。主版本一律
  不由机器人提（`pip` 走 `ignore`，`actions` 的组只收 minor/patch）——主版本变更会让既有基线失效。
- CI 的 `security-audit` job 跑 `pip-audit --requirement requirements.txt`，**非阻塞**：它的
  结论依赖漏洞库快照，同一 commit 今天绿明天红是常态，做成阻塞项长期必然被人加 `|| true`。
  结论写进 step summary 与 artifact，由人判断。
- 已知边界：`pip-audit -r` 从未在本机跑通（受限环境拒绝 `ensurepip`），该路径**只在 CI 可验证**；
  `requirements.lock` 由 `pip-tools` 从 `requirements.txt` 编译，含全部传递依赖。