# 两年真实数据验收：corrected 最终结论

> **Historical record.** Preserved for the corrected earlier validation lineage. Current checkpoint and authority are listed in [`documentation-index.md`](documentation-index.md).

## 结论

本文件替代早期 run #75 的旧 M/OI 晋级结论。

早期研究按“自然日最后一根 60 分钟 bar”等不完全一致的口径得出了 M/OI 正向结果；后续 Code Review 修正中国期货交易日、完全同步采样、point-in-time front-3、20 天交割黑窗和当时可见成交量后，旧结论被推翻。

当前事实：

```text
qualified_prior      = MA
prior-forward        = 4 trades, about -1.958R
qualified_current    = M
final OOS            = 2 trades, about +0.296R
recent two years     = 5 trades, about +1.028R
neighbor stability   = 0 / 16
2% risk proxy annualized ≈ 1.07%
100% annualized target = NOT MET
```

因此 M/OI 日频 log-ratio 规则**没有生产晋级**。`config/afuture.live.example.toml` 只保留为 test/Shadow 研究模板，不能把早期 artifact 当成当前经济证据。

完整跨家族研究结果见 [最终研究证据结论](research-final-evidence.md)。

## 数据

主数据来自 AKShare/Sina 具体交割合约历史接口，而不是随机合成数据。

- 最近窗口：2024-08-21 ~ 2026-08-20；
- 独立前置窗口：2022-08-22 ~ 2024-08-20；
- 研究根：A、C、EG、FG、I、M、MA、OI、P、PP、RB、RM、SA、TA、Y；
- 使用具体月份 60 分钟 K 线构建每日 point-in-time 相邻跨期；
- 公开源提供 OHLC、成交量和持仓量，但没有完整多年 L1 bid/ask/depth。

所以本研究只证明信号层经济性，真实执行质量仍必须依赖 Shadow、测试柜台和小资金实盘证据。

## corrected 时间语义

### 中国期货交易日

自然时间 `>=20:00` 的夜盘 bar 映射到下一观察到的日盘交易日。周五夜盘因此归到下一实际交易日；样本尾部无法确认后续交易日时 fail-closed。

### 同步采样

研究与生产冻结日频采样窗口均为：

```text
22:55-23:00 Asia/Shanghai
```

只接受两腿**完全相同时间戳**的 60 分钟样本。不能用 15:00、其它时刻或两腿不同时间戳补齐缺失样本。

### 当时可见成交量

历史 `visible_volume` 是同一 mapped futures trading day 内截至采样时刻已经发生的累计成交量，而不是把未来日盘成交量带回夜盘决策。

冻结门：

```text
min_volume = 1000
min_open_interest = 5000
```

## Point-in-time Universe

每个交易日按以下顺序构造候选：

```text
20-day delivery blackout
→ eligible contracts
→ front 3
→ adjacent pairs only
```

生产使用 CTP 官方 ExpireDate。公开历史源缺少完整历史 ExpireDate，研究明确使用交割月 15 日作为保守 proxy。

历史 replay 的 `ContractInfo.listing` 也会被保留，以防未来合约提前进入历史 Universe。

## 无泄漏切分

| 阶段 | 区间 | 用途 |
|---|---|---|
| Prior-1 | 2022-08-22 ~ 2023-08-20 | 旧资格窗口 1 |
| Prior-2 | 2023-08-21 ~ 2024-08-20 | 旧资格窗口 2 |
| Train | 2024-08-21 ~ 2025-08-20 | 当前资格/前向检查 |
| Validation | 2025-08-21 ~ 2026-02-20 | 当前资格 |
| Final OOS | 2026-02-21 ~ 2026-08-20 | 最终否决窗口 |

参数/品种资格不应使用 Final OOS。

但需要额外强调：经过本轮多家族研究后，**这一 Final OOS 日期窗口已经被观察，不再是未来新研究的 pristine holdout**。新功能不能继续把它宣传为真正未见数据。

## 冻结 M/OI 研究参数

```text
signal_transform          = log_ratio
lookback                  = 25
entry_z                   = 2.50
confirmation_retrace_z    = 0.30
min_confirmed_entry_z     = 1.75
exit_z                    = 0.75
stop_z                    = 4.00
entry_trend_window        = 6
max_entry_z_slope         = 0.75
min_stationarity_score    = 0.01
max_half_life             = 60
max_holding_samples       = 20 days
```

这些参数保留用于重复研究和 Shadow，不表示策略已证明盈利。

## corrected 经济门

当前 evaluator 不再硬编码“必须选出 A/M/OI”等品种名；品种身份必须是历史资格规则的输出。

经济门检查：

- prior qualification 非空；
- prior → forward 至少 2 笔且 R > 0；
- current qualification 非空；
- Final OOS 至少 3 笔且 R > 0；
- Final OOS 回撤不超过 0.5R；
- 最近两年至少 10 笔且 R > 2；
- 最近两年回撤不超过 0.5R；
- 16 个局部邻域至少 50% 同时通过双前向门。

corrected 结果没有满足这些条件，所以 `accepted=false`。

研究程序正常运行但经济门拒绝时返回 code `2`；这表示“程序工作正常、策略没过门”，不能与工程测试失败混为一谈。

## 风险预算

旧文档中用早期 R 序列计算的 1%/1.5%/2% 风险复合收益不再作为有效生产证据。

corrected 2% 风险代理年化只有约 1.07%。当前 live 示例把风险降到 test/Shadow 级：1 个 pair、1 手、低 risk budget 和更高可用资金要求。不能通过提高风险预算把低收益历史证据放大成“100% 年化”。

## 可重复验收

`.github/workflows/research-2y.yml` 是昂贵的手动 L4 milestone gate。它重新执行：

```text
fetch_two_year_60m_universe.py
fetch_prior_two_year_60m_universe.py
test_alignment.py
evaluate_daily_relative_strategy.py
```

普通小改动不重复抓取相同历史状态；主 `ci.yml` 负责工程正确性。

## 后续证据要求

继续研究需要新的信息，而不是继续调同一历史：

1. 新发生、此前未见的交易日；
2. 真实 CTP L1 Shadow；
3. 测试柜台异常场景与真实手续费/滑点；
4. 极小真实仓位的 realized Net Edge 和实际回撤。

在这些证据出现前，M/OI 及其它研究 Alpha 都不能被写成“已达到年化 100%”或“已晋级生产”。
