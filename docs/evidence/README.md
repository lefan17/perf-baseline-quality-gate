# evidence/ · 可复核的证据

本目录把 CI 基线快照的关键指标固化下来，避免「README 里的性能数字是哪来的」这类追问。

## summary.json

| 字段 | 含义 |
| --- | --- |
`run_id` | 产生这份基线的压测运行 id（和 `reports/perf-*.md` 的文件名对得上） |
`produced_at` | 基线写入时间 |
`metrics.total` | 全部样本池化后的指标 |
`metrics.knee` | 拐点那一档的指标（**不能与 total 混用**） |
`probes.cache` | 缓存探针：`cache=on` vs `cache=off` 的端到端 P95（2.2 倍） |
`probes.index` | 索引探针：`SEARCH` vs `SCAN` 的 SQL 层耗时（29 倍）与执行计划 |
`notes` | 哪些数字能外推、哪些不能 |

## 怎么复核

```bash
python -m platform_cli baseline show --label ci-perf
```

两组探针各有自己的复核入口（**都写在 `probes.*.reproduce` 里**）：

```bash
# 缓存：需要一个真 Redis
docker run -d -p 6379:6379 --name perf-redis redis:7-alpine
set PERF_REDIS_URL=127.0.0.1:6379
python platform_cli.py run --scenario item_detail_cached --items 5000 --step-users 10 --step-duration 8
python platform_cli.py run --scenario item_detail_nocache --items 5000 --step-users 10 --step-duration 8

# 索引：跑那 4 条断言（自动 seed 5 万行）
python -m pytest tests/test_index_probe.py -v

# 连接池：11 条（不需要 Redis，也不需要 MySQL）
python -m pytest tests/test_connection_pool.py -v

# 反向代理：需要一个 nginx（没有会自动跳过并打印安装提示）
$env:PERF_NGINX_EXE = "D:\study\AppCache\nginx-1.27.2\nginx.exe"
python scripts/proxy_probe.py --items 20000 --users 20 --duration 8
```

## 与 reports/ 的关系

`reports/` 被 `.gitignore` 忽略（每次运行都会产生新文件），所以**不入库**。
如果你要一份带具体 run_id 的原始报告，CI 的 `perf-report` artifact 保留 14 天。

## 别把这两个口径混起来

- `total.p95` = 把所有并发档的样本倒进一个池子算的分位数（反映「整体体验」）
- `knee.p95` = 只取拐点那一档的分位数（反映「系统快撑不住时有多慢」）

同一个仓库里 `53.9ms` 和 `110.1ms` 都真实存在，差 2 倍，原因是口径不同。

## 探针为什么各在各的层里量

这一点值得单独说，因为它是**刻意的不对称**，不是遗漏：

| 探针 | 在哪量 | 为什么不在另一端 |
| --- | --- | --- |
| 缓存 | **端到端 P95** | Redis 往返真实存在于请求链路里，端到端才量得准 |
| 索引 | **SQL 层** | 29 倍的差异套上 HTTP 后，会被约 25 ms 的单请求固定开销压成 10% 抖动；而单机压测两轮之间本来就有 3% 量级的 CV → 端到端「看起来差不多」，会被当成噪声忽略 |
| 连接池 | **状态码 + 诊断计数** | 池满时失败很快，**P95 反而会变好**；只看错误率也分不清「池满了」和「代码挂了」。所以断言 503 + `Retry-After` + `saturation` + `timeouts_total` 四件一起 |
| 反向代理 | **端到端 P95 / RPS / 错误率** | 代理本来就活在请求链路上，且它的成本（尤其上游连接不复用）正是端到端才体现 |

在测不出来的地方就不假装测得出来 —— 这是 `docs/诚实边界.md` 的同一条原则。

## 两个不要读错的地方

1. **连接池是自写的池，不是 MySQL 的池。** SUT 主库仍是 SQLite。相同的是现象与判据
   （503 率、饱和度、获取超时计数），不是产品。`sut/pool.py` 与 `tests/test_connection_pool.py`
   的 docstring 里都写了这一点。
2. **反向代理的绝对数字不能外推。** 那是 Windows 单进程 nginx + 本机回环；
   生产上是 Linux 多 worker，绝对开销不同。可外推的是**方向与量级**：
   上游每次新建连接会同时抬高延迟和错误率（20 并发下 P95 1.70×、错误率 0 → 18.67%）。
