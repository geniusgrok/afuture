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

数据在系统中的可见时点和研究窗口见 [`data-and-backtest.md`](data-and-backtest.md)，字段相关术语见 [`glossary.md`](glossary.md)。
