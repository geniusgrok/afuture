# CTP 运行、Shadow 与监督

运行配置从仓库示例复制到私有目录，填写已确认的前置地址；凭证与账户身份只进受控环境变量，见[配置](configuration.md)。不得把凭证、私有路径或账户响应提交到 Git。Stress-90 的预热、身份与维护命令另见[手册](stress90-live-runbook.md)。

## 目标机与现场门

CTP live 使用 POSIX 目标机，在最终 OS/CPU/Python 环境安装受约束 `.[live]` 并验证 `import vnpy_ctp`。开发机、Windows core smoke 或测试 Broker 不能认证原生 ABI、真实登录和回调顺序。

上线顺序为：数据/回放与风险检查→本地状态及柜台预检→连续多日隔离 Shadow→真实行情来源比较→专用测试柜台生命周期/重连→获明确授权的极小资金→核对结算与执行质量→新发生数据复核。扩大风险须独立判断和授权。当前代码不声称真实资金连续性或无人值守生产已获认证。

生产连接/报单须 `AFUTURE_LIVE_ACK=I_UNDERSTAND_FUTURES_RISK` 与 `--confirm-live`。Stress-90 还须明确预期 AccountID/CurrencyID，并与原始账户响应、权威交易日及当次查询精确匹配；额外身份字段在柜台提供时也须匹配。

## 状态和无报单预检

```bash
afuture status --config config/afuture.directional-live.example.toml
afuture doctor --config config/afuture.directional-live.example.toml --confirm-live
```

`status` 只读本地，不连接、不初始化日志，也不需要凭证。它检查 current/`.prev` 完整性、停机/持仓、缓存身份、路径和磁盘；损坏返回 2，`.prev` 不作为恢复源。

`doctor` 连接并等待新的账户/完整持仓快照，核验权威交易日、活动委托、对账、风险、合约目录/乘数/保证金/手续费及新行情。方向组合须有上一完整日 activity 和覆盖同日的 OHLC；新部署缺完整快照不开仓。Stress-90 还预览每层 decision/weights/lots、OI coverage、target continuity、cost 和 freeze；任一必需项不可信即 `stress90_ready=false`。`orders_sent=0` 是必要结果，但连接柜台可能自动执行结算确认，不能把无报单理解为零柜台副作用。

## Shadow 与启动

```bash
afuture shadow --config config/afuture.directional-live.example.toml --confirm-live --duration-seconds 3600
afuture quality-report --config config/afuture.directional-live.example.toml --shadow --output runtime/shadow-quality.json
```

Shadow 使用实时 CTP 信息，订单、成交、持仓和资金由隔离的本地模拟 Broker 维护，不向柜台发送委托。Stress-90 使用独立 `runtime/shadow/` 的持久 account/policy/OI/intent/cache，须单独预热、身份绑定和技术许可；不能借用 live soft path 或 cache。观察多个完整交易日，含夜盘、跨日、正常停机及受控重启，解释计划/实际价格、费用、部分成交、拒单、延迟和数据来源差异。

生产启动使用既有账户/runtime 互斥锁；重复启动只读失败，不改写活动实例。Stress-90 当前盘前链为：

```text
deployment-verify → prepare-session → 按 allowed_next_actions 处理必要维护
→ doctor → capacity report → 人工核验及新技术许可 → live → watchdog
```

```bash
afuture prepare-session --config CONFIG --confirm-live --output PRECHECK_JSON --refresh-ohlc
afuture watchdog --config CONFIG --once --max-age-seconds 15
```

`prepare-session` 一次退出，可刷新 cache 和输出检查；不解除停机、不签 permit、不自动 rebase/roll-forward、不调风险。`watchdog` 只读并告警，不平仓、不控制账户。获现场许可后才运行 `afuture live --config CONFIG --confirm-live`。

## 日历、停机和异常重启

每笔普通/方向报单、失衡退出及回滚都经过当前真实时段、柜台交易日、行情和硬风控。版本化 `runtime_calendar.json` 当前覆盖 2025-12-31 至 2026-12-31；未知日期/产品、损坏或柜台冲突拒绝，不退回周一至周五猜测。夜盘自然日与柜台交易日分开，取消夜盘、休息和集合竞价不能当作允许开仓的窗口。

人工或硬风险停机保留 kill switch，日切不自动清除。异常退出的 `process_run.json` 使 live 在 gateway 启动/真实连接前默认以 75 拒绝并失效旧技术许可；不得用 restart loop、删除状态或 `.prev` 覆盖绕过。仅同次已消费许可的提交恢复，或具认证提供者及原授权的正常日终续接，可按完整证据重新核验；损坏/失配或账户状态丢失持续阻断。

`deploy/systemd/` 为待目标机批准安装的模板。live 的退出码 75 防止自动重启；prepare-session 定时值只作起点，资料不齐/节假日失败关闭，不追补错过运行。systemd 启动成功不构成交易许可或一个月现场验收。

## 结算格式采集

仅在性质已确认的零真钱测试柜台、账户绑定及该次连接已获批准后使用：

```bash
afuture ctp-settlement-capture --config TEST_CONFIG --trading-day YYYYMMDD --output PRIVATE_JSON --confirm-test-connection
```

它拒绝 production、缺预期身份、不规范绝对路径和已有输出，持有账户/runtime 锁，等待 fresh snapshot 后查询 SettlementInfo；不下单、撤单、推进账户或签许可。登录可能自动结算确认。输出为私有 Unicode 分片和请求/账户/交易日/结束证据，不声称原生 GBK 或未经认证的序号原点；`financial_continuity_verified=false`。未知格式、缺片或冲突拒绝，不从文本猜出最终资金。

## 告警与证据

审计、执行质量及关键回调积压需可观测。`AFUTURE_ALERT_WEBHOOK` 可用受限权限 SQLite outbox 持久化通知；目标摘要绑定，不跟随重定向。队列/数据库容量、重试耗尽和写失败会升级，未确认事件不被删。通知至少一次送达，接收端按 event_id/Idempotency-Key 去重；2xx 只代表端点接受。spool 损坏不自动重建，保留原件按事故处理。

同机 watchdog 无法证明整机断电后送达，独立失联检测须现场验证。异常恢复按[排障](troubleshooting.md)，证据要求按[上线检查](production-checklist.md)。
