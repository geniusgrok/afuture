# Stress-90 固定历史输入 GitHub 私库收据

> **Historical provenance only.** 本收据记录已退役 historical fixed-archive replay 的输入
> 身份与恢复事实；它不是 current runtime/replay 指令。当前研究 loader 仍验证同一冻结输入的
> SHA-256，但仓库不再包含或支持 historical compatibility adapter。

原始 CSV 未进入本公开仓库。本收据不包含 token、clone credential、signed URL、原始文件内容或
临时路径。

## 归档身份

- Vault repository：`ychenracing/afuture-evidence-vault`
- GitHub repository ID：`1352283086`
- Visibility：已通过 GitHub API 核验为 `private`
- Vault `main` commit：`5d3c4ba7f941f845e6610028e908166861e18c85`
- Historical replay commit：`9c51195042393304eb05d783d1895a165f99b0a7`
- 已退役的 compatibility replay checkpoint：`efb44371b0e60c8654d574a86675f8cd339950ce`
- Candidate SHA-256：`8e38dbf6441b561dd1728df08665b94b15cc3358823257505c2fcb9d63f09f28`
- Deterministic TAR：52,060,160 bytes，SHA-256
  `05c763b9cc4a05336d8f59e8d73106c941c6ff3ee7afaa45ebfd08df2771c3b0`

## 固定输入

| Basename | Size (bytes) | SHA-256 |
|---|---:|---|
| `broad_daily_universe.csv` | 3,278,200 | `c1d46bc113a79bd2e000bf5d66ee53c59337eda750b2d3b1409e40f3f4833d0f` |
| `return_target_specific_contracts.csv` | 40,842,073 | `f8b3f4232cb1bc9eaed65a4401dff6bed121faee064252727202874ad7d53c64` |
| `execution_aligned_weights.csv` | 109,977 | `250a55c1c18ecd3754f489136515275eec3a48d505fd41026d68ab43924e08c1` |
| `prior_two_year_broad_60m.csv` | 3,698,425 | `3351aa3ae8dec0cf0e9b9a64026181ac8ff2cc22858c442821fe3c94767036b1` |
| `two_year_broad_60m.csv` | 4,080,136 | `5faf112bb69dd5ddf48ed34419e2046b1c6bdd651b595ca46a46804d8317a27b` |

## 来源

| Workflow run ID | Artifact ID | 恢复文件 |
|---:|---:|---|
| `32562548653` | `9473260618` | `broad_daily_universe.csv`、`return_target_specific_contracts.csv` |
| `32634296589` | `9491959916` | `execution_aligned_weights.csv` |
| `32717335780` | `9516473115` | `prior_two_year_broad_60m.csv` |
| `32717335780` | `9516472727` | `two_year_broad_60m.csv` |

这些文件是从上述历史 artifacts 恢复的原始字节；没有用新下载的市场数据替代，
也没有改写、转码、排序或填充 CSV。

## 验证结果

- 当时的固定历史回放层只接受五个 basename、size 和 SHA-256 全部精确匹配的输入；
  任一不匹配立即拒绝。该回放层已在 current-only cleanup 中退役；保留的当前研究 loader
  仍在解析前验证五个输入的 SHA-256，矩阵继续精确校验 manifest size。
- 当时的 current-main compatibility replay 与 fixed-archive parity 通过；定向套件
  `49 passed`，synthetic batch/incremental parity 保持通过。
- 七个窗口 `base/full_recent`、`stress/prior1`、`stress/prior2`、
  `stress/train`、`stress/validation`、`stress/oos` 和
  `stress/full_recent` 全部完成；historical gate `passed=true` 且
  `reasons=[]`。
- 从一个新的空目录，仅通过已授权的 GitHub Git Data API 按 vault commit
  读取 Git blobs 完成恢复；五个 Git blob ID、size 和 SHA-256 均匹配，恢复后
  candidate、七窗口 matrix 和 archived matrix 全量相等，定向套件再次为
  `49 passed`。
- 该执行环境没有向命令行暴露私库 clone credential，因此没有把 API 恢复
  虚报为普通命令行 `git clone`；vault 内 `restore_receipt.json` 固定记录
  `restore_transport=github_git_data_api` 和 `command_line_clone=false`。
- 生产 policy 文件摘要在兼容层前后均为
  `03a22a4a4f9adfdf8b5a5e13d44fc287aa774d9be9b5377e4d99bbc2323b7e14`；
  live/runtime import graph 不包含历史兼容模块。
- Vault 中 CSV 使用普通 Git blobs，`.gitattributes` 设置 `*.csv -diff`，未使用
  Git LFS。

## 授权边界

- `evidence_scope=historical_research_only`
- `provider_independent=false`
- `live_authorized=false`
- `risk_increase_authorized=false`
- `prospective_evidence=false`

该 vault 只是 GitHub 上的私有副本，不是跨服务灾备。历史回放、candidate
摘要或 CI 通过均不代表 CTP、Shadow、测试柜台、极小真钱、真实账户或未来
证据已经完成，也不授权实盘或扩大风险。
