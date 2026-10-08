# 数据格式

本文定义 `afuture replay`、`scan`、`accept` 和 `data-check` 使用的标准 Tick CSV。数据在进入策略前会转换成统一 `Tick` 模型；读取失败或字段非法时不会继续回放。

## 1. 文件要求

- UTF-8 编码，首行为字段名，逗号分隔；
- 每行表示一个合约在一个明确时点的一档行情；
- `timestamp` 必须包含时区偏移，例如 `2026-08-21T09:00:00+08:00`；
- 同一数据集不要混用本地无时区时间、UTC 和中国时间；
- `trading_day` 使用柜台交易日 `YYYYMMDD`，不能用夜盘所在的自然日期代替；
- 原始文件应按时间递增。`replay` 默认会排序，但 `data-check` 会检查源文件的乱序事实。

示例：

```csv
timestamp,symbol,exchange,bid_price,ask_price,last_price,bid_volume,ask_volume,trading_day,limit_up,limit_down,volume,open_interest,open_price,source_trading_day,source_action_day,source_trading_day_verified
2026-08-21T09:00:00+08:00,m2609,DCE,3000,3001,3000.5,20,18,20260821,3300,2700,125000,82000,2998,20260821,20260821,true
```

## 2. 字段定义

| 字段 | 必需 | 类型/单位 | 约束和语义 |
| --- | --- | --- | --- |
| `timestamp` | 是 | ISO 8601 时间 | 必须带时区；表示该条行情实际可见的时点。 |
| `symbol` | 是 | 字符串 | 具体合约代码，不是连续合约或品种代码。 |
| `exchange` | 是 | 字符串 | 交易所代码，例如 `DCE`、`CZCE`、`SHFE`、`INE`。 |
| `bid_price` | 是 | 浮点数，元/报价单位 | 最优买价，必须有限且大于 0。 |
| `ask_price` | 是 | 浮点数，元/报价单位 | 最优卖价，必须有限、大于 0 且不低于买价。 |
| `last_price` | 是 | 浮点数，元/报价单位 | 最新成交价，必须有限且大于 0。 |
| `bid_volume` | 是 | 浮点数，手 | 最优买价可见数量，必须有限且大于 0。 |
| `ask_volume` | 是 | 浮点数，手 | 最优卖价可见数量，必须有限且大于 0。 |
| `trading_day` | 是 | `YYYYMMDD` | 柜台交易日；跨期两腿必须一致。 |
| `limit_up` | 否 | 浮点数，价格 | 当日涨停价；空值按 0 读取，表示没有可信证据，数据检查会告警。 |
| `limit_down` | 否 | 浮点数，价格 | 当日跌停价；空值按 0 读取。两者均非 0 时必须满足涨停价高于跌停价。 |
| `volume` | 否 | 浮点数，手 | 柜台当日累计成交量，不是该 Tick 的增量；空值按 0 读取并产生数据质量告警。 |
| `open_interest` | 否 | 浮点数，手 | 当前总持仓量；空值按 0 读取并产生数据质量告警。 |
| `open_price` | 否 | 浮点数，元/报价单位 | CTP 当日开盘价；必须有限且非负。0 表示没有可信开盘证据，不能替代首个完整区间的开盘。 |
| `source_trading_day` | 否 | `YYYYMMDD` | 原始 CTP 行情包的 `TradingDay` 字段；保留包来源身份，不单独构成柜台交易日权威。 |
| `source_action_day` | 否 | `YYYYMMDD` | 原始 CTP 行情包的 `ActionDay` 字段；用于审计夜盘/自然日包身份，不能替代权威交易日。 |
| `source_trading_day_verified` | 否 | 布尔值 | 该行情来源交易日是否已由权威 TD API/会话证据验证；实时 CTP 适配器只在有原始 `TradingDay` 包字段时标记，Stress-90 仍以 Broker 派生的 TD/会话交易日作最终权限判断。 |

所有数值字段拒绝 NaN 和无穷值。价格和一档数量必须为正；涨跌停、累计成交量和持仓量可以为 0，但 0 表示证据缺失，不能据此通过需要这些字段的开仓门。

## 3. 排序、重复和缺口

`read_ticks()` 默认按 `timestamp` 排序，保证确定性回放。这不会把源数据修复成高质量数据：

- 同一 `symbol + timestamp` 重复会由 `data-check` 记录；
- 同一合约时间倒退属于硬失败；
- 同一交易日的长时间缺口会告警；
- 非法价格、盘口或时间戳属于硬失败；
- 多品种样本被单一品种过度占据会告警；
- Auto 数据在某个交易日无法形成两腿候选时属于硬失败。

先运行：

```bash
afuture data-check --config config/afuture.auto-replay.example.toml --data examples/research_ticks.csv
```

警告不是自动豁免。是否可以继续研究取决于缺口会不会改变选约、信号、成交或成本；不能通过排序、向前填充或删除失败日期来隐藏问题。

## 4. 时间与因果约束

- 决策只能读取该 `timestamp` 当时已经出现的行；
- 方向组合交易日 D 的完整数据最早影响 D+1；
- 当前交易日尚未完成的 `volume` 和 `open_interest` 不能改写上一完整交易日的选约快照；
- 不允许用当日收盘后才知道的结算价、最终成交量或最终持仓量回填盘中样本；
- 连续合约只用于信号研究，具体合约成交和换月收益必须由具体合约数据决定；
- 训练、验证、样本外和前序窗口必须拥有独立账户状态，不能共享未来高水位、持仓或候选选择结果。

## 5. 与 CTP 实时数据的差异

CTP 行情由适配器直接构造相同的 `Tick` 模型，不先写入 CSV。字段语义必须一致，但实时运行还要求：

- 行情相对当前时间未过期；
- 交易日与账户和另一腿一致；
- 原始 `source_trading_day`、`source_action_day` 只描述 CTP 行情包；权威日必须由 TD API 账户/会话证据与 Broker 派生交易日一致地验证，缺失或不匹配时拒绝推进 Stress-90 数据或决策；
- 合约目录身份和交易所一致；
- 合约乘数、最小变动价位、保证金和手续费来自可信柜台元数据；
- 断线或快照不完整时拒绝增加风险。

## 6. 日频输入与缓存

方向价格历史的开盘/收盘索引必须在转换前为无时区自然日午夜、唯一递增且完全对齐；正有限数值能无损规范成 `float64`。`directional_ohlc_cache.json` 保存共享日期向量、开盘/收盘矩阵、品种 manifest、content SHA-256 和 envelope SHA-256。provider 追加须保留缓存全部日期及规范值，修订、删日期、损坏或缺 required completed day 均阻止新风险。Stress-90 live 只由独立 `directional-ohlc-refresh` 访问 provider，订单与回调路径只读 validated cache。

activity 保存 `completed` 与 `in_progress`；OI 保存 expected/observed/missing 合约 coverage。潜在 dominant 合约未观察、缺首/末区间不能当作合法 `flow=0`。target day 来自 CTP，上一完整日及 gap 必须由持久证据确认；不能用工作日推算或未来数据补齐。

对活跃敞口的当日 OHLC，缺失、非正、非有限或绝对日内变化超过 20% 均失败关闭；仅零敞口且与当前目标无关的异常单元不会妨碍其他已验证风险收缩，不能用于新开仓。

## 7. 单位与回放限制

名义价值为价格×合约乘数×手数；gross 为多空名义绝对值之和，保证金不是名义价值。1 bp=0.01%，手续费与滑点须以同一乘数、数量和金额单位记账。HHI 为各品种绝对权重占比平方和。

公开日线不能重现真实一档深度、排队、拒单、部分成交、柜台逐日费率和保证金。回放账户由成交推进，不能把连续合约跳空计为可交易收益。各验证窗口从独立账户状态开始；汇总窗口可能与子窗口重叠，已用于选择的样本不再是纯净样本外。历史结果不能替代实际柜台或新发生数据。
