# evidence/ · 可复核的证据

本目录把 CI 基线快照的关键指标固化下来，避免「README 里的性能数字是哪来的」这类追问。

## summary.json

| 字段 | 含义 |
| --- | --- |
`run_id` | 产生这份基线的压测运行 id（和 `reports/perf-*.md` 的文件名对得上） |
`produced_at` | 基线写入时间 |
`metrics.total` | 全部样本池化后的指标 |
`metrics.knee` | 拐点那一档的指标（**不能与 total 混用**） |
`notes` | 哪些数字能外推、哪些不能 |

## 怎么复核

```bash
python -m platform_cli baseline show --label ci-perf
```

## 与 reports/ 的关系

`reports/` 被 `.gitignore` 忽略（每次运行都会产生新文件），所以**不入库**。
如果你要一份带具体 run_id 的原始报告，CI 的 `perf-report` artifact 保留 14 天。

## 别把这两个口径混起来

- `total.p95` = 把所有并发档的样本倒进一个池子算的分位数（反映「整体体验」）
- `knee.p95` = 只取拐点那一档的分位数（反映「系统快撑不住时有多慢」）

同一个仓库里 `53.9ms` 和 `110.1ms` 都真实存在，差 2 倍，原因是口径不同。
