# afuture

个人中国期货交易工具。固定或自动选择跨期价差，以及 `execution_aligned` / `stress90` 方向组合，共用 Broker 成交、账户风控、状态存储和审计链。提供历史 Tick 回放、实时 CTP、无柜台委托的 Shadow、只读状态检查及受保护的恢复命令。

代码和离线检查不证明真实柜台、资金连续性或无人值守交易已获认证。实盘前须在目标机完成原生 CTP、连续 Shadow、测试柜台和实际结算验证；启用真实资金需要另行明确授权。

## 安装

Python 3.10 及以上。核心支持 Windows；CTP live 使用 POSIX 目标机，并须验证实际 Python/CPU/原生扩展组合。

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]" -c constraints/core-dev.txt
```

需要实时 CTP 时，在目标机安装：

```bash
python -m pip install -e ".[live,dev]" -c constraints/core-dev.txt -c constraints/live.txt
python -c "import vnpy_ctp"
```

Windows 核心环境使用 `.venv\Scripts\Activate.ps1` 激活。运行命令从仓库根目录执行；生产配置与运行文件放在私有目录，凭证只通过环境变量提供。

## 本地使用

```bash
afuture validate --config config/afuture.example.toml
afuture data-check --config config/afuture.auto-replay.example.toml --data examples/research_ticks.csv
afuture replay --config config/afuture.example.toml --data examples/sample_ticks.csv
afuture scan --config config/afuture.auto-replay.example.toml --data examples/research_ticks.csv
afuture status --config config/afuture.directional-stress90-live.example.toml
```

`replay` 使用模拟 Broker；`status` 不连接 Broker，也不改文件。`live`、`doctor` 和读取实时柜台的准备命令有连接及身份确认要求，按[运行说明](docs/live-trading.md)执行。Shadow 接收实时信息，其订单、成交和资金由隔离的模拟 Broker 维护。

| 示例配置 | 用途 |
| --- | --- |
| `config/afuture.example.toml` | 固定双腿跨期回放 |
| `config/afuture.auto-replay.example.toml` | 自动选择跨期组合回放 |
| `config/afuture.live.example.toml` | 自动跨期 CTP 配置起点 |
| `config/afuture.directional-live.example.toml` | 普通方向组合配置起点 |
| `config/afuture.directional-stress90-live.example.toml` | Stress-90 预热与 commissioning 配置起点 |

示例前置地址、合约和资金不是投产配置。普通方向示例与 Stress-90 政策硬包络包括总敞口不超过权益 2 倍、保证金不超过 35%、可用资金至少 25%、单日亏损 5%、总回撤 30%、单合约至多 35 手；真实配置仍须按账户确认，目标拟合只能向下缩减。

## 文档

- [配置与单位](docs/configuration.md)、[Tick 与市场数据](docs/data-formats.md)、[当前策略](docs/strategies.md)
- [账户与执行架构](docs/architecture.md)、[CTP 运行](docs/live-trading.md)、[上线检查](docs/production-checklist.md)
- [Stress-90 预热、身份与恢复](docs/stress90-live-runbook.md)、[排障](docs/troubleshooting.md)

历史设计、研究和交接材料保存在[归档分支](https://github.com/geniusgrok/afuture/tree/archive/pre-slim-20261008)。

## 开发检查

```bash
python -m pytest -q
python -m compileall -q afuture
python -m ruff check .
```

使用 [AGENTS.md](AGENTS.md) 中的工作约定，按改动影响选择检查。Broker/CTP 拥有外部账户与订单真相，`PositionBook` 只由成交推进，`RiskManager` 拥有硬限制；禁止第二套账户、自动采用损坏状态或 `.prev`，以及用研究结果授权实盘。
