# 供需输入与 S1 离线研究

本入口不改变 B0 默认，不接柜台、不报单，也不授权生产或 Shadow 启用新机制。

`tools/collect_supply_demand.py` 从事前列出的免费公开 HTTPS 地址保存原始字节、失败响应、实际观察时间、HTTP 元数据、SHA-256 和 Git blob ID。每次使用新输出目录；HTTP 成功、统计日期、`update_date` 和 `Last-Modified` 均不能自动证明历史可得时间。

```bash
python tools/collect_supply_demand.py public_sources.json captures --workers 4
```

输入为 `[{"id": "unique_safe_id", "url": "https://..."}]`。脚本不执行挑战页脚本，不登录，也不部署持续采集服务。返回体超过 4 MB 时保留截断证据并标记不完整，不能把它认证为完整原件。

原件资格核验在私有证据中完成。每条版本至少保留品种、统计日、单位与范围、原件引用及哈希、独立可证明的可用时间、修订身份和实际采集时间。库存小计、注册仓单与可用库容分开；厂库与仓库分开；小计与总计不能重复相加。历史最终版只能从其被证明可得的时刻起使用，不回填到报告日。

`afuture.supply_demand.warrant_signal` 是研究与将来经批准接线可共用的纯信号核心。它选择决策时已知的最新版本，使用自身前两年的因果季节中位数、四周变化和两个真实月份合约的期限结构共同确定方向。缺失、过期、预热不足、最新版本不可信或条件冲突均产生中性目标；不拥有账户、持仓或风险状态。

```bash
python tools/run_supply_demand.py \
  --observations qualified_observations.json --original-root evidence \
  --specific-market specific.csv --continuous-market continuous.csv \
  --days frozen_980_days.csv --protocol frozen_protocol.json --output s1-account
```

记录使用 `product/statistical_day/value/available_at/source_sha256/source_id/scope/qualified/original_path`；日期为 ISO 格式，可用时间必须含时区。先核对引用原件的完整哈希，再调用核心。输入、协议与源码各有新身份，不能借用旧批准 hash。

真实调用链是：原件资格 → 同一纯信号核心 → 原九品种 OI 门 → 原成本门 → 既有 `ExpandingMedianConcentrationFreezeDirectionalProductionAcceptance` → 原具体合约选择、整数手、保证金、成交费用、HHI 和回撤储备。可用 `--completed-oi-flow completed_flow.csv` 接入已完成交易日的原 OI 证据，首列为统计交易日、列为原九个品种、值为 -1/0/1。仅匹配前一完整市场交易日；缺失关闭该供需请求，不前向填充旧 OI。净额和账户事实仍由现有执行链维护。

显式 `--diagnostic-untrusted-source` 仅用于已冻结的条件诊断，输出身份也标为不可信输入诊断；`--ablation` 移除库存状态/变化条件，保留相同可得时间和预算，仅供配对归因。两者均不能晋级。没有非零成本批准目标时保存资格与权重并跳过空账户矩阵。完整合同仍须使用原窗口、完整池、ex-AG、两档成本、原风险与其后适用的剔除和集中度验收。

独立的 `S1_EXECUTION_FRESHNESS_COST_20261003` 规格保留原信号和风险限制，并增加两项资格检查：统计日距执行交易日也不得超过 10 个日历日；新增风险还须有至少四个决策前已知、同品种同方向、互不重叠且已结束的 S1 单手观察过程，其毛收益扣除 Stress 全部进出及换月腿费用后的收益基点中位数为正。单手观察只估计机制持有期和完整换手成本，不拥有资金、持仓或账户盈亏。缺失价格使整个观察失效，未结束过程不能训练估计，减仓始终允许。四个过程只代表研究样本就绪，不是统计显著性或经济晋级。原 20 日收益代理和 15bp 门继续保留。

该规格使用同一日线模型中的前一市场日 15:00 决策和下一交易日 09:00 执行标记。它检查版本可用时序及跨周末、假期的执行日年龄，仍不能认证结算价真实发布时间、夜盘首次可执行分钟或柜台成交。旧规格不传 `execution_at` 时保留其原行为；不得把新结果覆盖为旧结果。`--freshness-only-diagnostic` 只归因执行新鲜度变化，不得晋级。

存在合格局部配对净贡献后，可用 `--baseline-weights` 和 `--exag-baseline-weights` 成对接入冻结 B0 的原 980 日、完整 50 品种目标。完整池与 ex-AG 分别构造供需目标、OI、成本历史和对应 B0 底座，排除发生在组预算分配之前。两种机制使用共同具体合约选择，先净掉相反请求，再把剩余新增请求统一缩放到 2 倍总敞口的剩余目标预算；随后仅进入一个既有账户模拟，由真实路径权益、整数手、保证金、HHI 和回撤储备决定可执行量。此目标预算不等同于闲置现金，也不能保证 B0 权益路径不变。不得拼接两份独立账户利润，未有合格局部贡献时不运行救收益组合。
