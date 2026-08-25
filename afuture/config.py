"""TOML 配置加载与实盘安全校验。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from .auto import AutoConfig
from .directional import DirectionalConfig
from .models import ContractInfo, ContractSpec, FeeSpec, PairConfig
from .risk import RiskConfig


@dataclass(frozen=True)
class AppConfig:
    """应用运行配置。敏感 CTP 字段不存入 TOML。"""

    mode: str
    initial_capital: float
    contracts: dict[str, ContractSpec]
    pairs: list[PairConfig]
    risk: RiskConfig
    ctp: object | None
    slippage_ticks: int = 1
    aggressive_ticks: int = 1
    auto_flatten_imbalance: bool = True
    legging_timeout_seconds: float = 2.0
    conservative_simulation: bool = False
    latency_ticks: int = 0
    market_impact_ticks: int = 0
    require_live_metadata: bool = True
    metadata_timeout_seconds: float = 10.0
    state_path: str = "runtime/state.json"
    log_path: str = "runtime/afuture.log"
    report_path: str = "runtime/report.json"
    journal_path: str = "runtime/audit.jsonl"
    alert_path: str = "runtime/alerts.jsonl"
    alert_webhook: str = ""
    auto: AutoConfig = field(default_factory=AutoConfig)
    directional: DirectionalConfig = field(default_factory=DirectionalConfig)
    contract_catalog: list[ContractInfo] = field(default_factory=list)


def load_config(path: str | Path) -> AppConfig:
    """读取配置并在连接柜台之前拒绝明显危险或自相矛盾的参数。"""
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    system = data.get("system", {})
    mode = str(system.get("mode", "replay")).lower()
    if mode not in {"replay", "live"}:
        raise ValueError("system.mode must be replay or live")

    initial_capital = float(system.get("initial_capital", 500000))
    if initial_capital <= 0:
        raise ValueError("initial_capital must be positive")

    risk_raw = data.get("risk", {})
    risk = RiskConfig(
        **{key: value for key, value in risk_raw.items() if key in RiskConfig.__dataclass_fields__}
    )

    contract_rows = data.get("contracts", [])
    contracts = _load_contracts(contract_rows)
    contract_catalog = _load_contract_catalog(contract_rows)
    pairs = _load_pairs(data.get("pairs", []), contracts, mode)
    auto = _load_auto(data.get("auto", {}), mode)
    directional = _load_directional(data.get("directional", {}))
    if directional.enabled and (pairs or auto.enabled):
        raise ValueError(
            "directional mode is account-exclusive and cannot run with static pairs or auto"
        )
    if mode == "replay" and auto.enabled and not contract_catalog:
        raise ValueError("replay auto mode requires contract product/expiry metadata")
    ctp = _load_ctp(data.get("ctp", {}), mode)
    if mode == "live" and not pairs and not auto.enabled and not directional.enabled:
        raise ValueError(
            "live mode requires static pairs, auto.enabled=true, or directional.enabled=true"
        )

    execution = data.get("execution", {})
    slippage_ticks = int(execution.get("slippage_ticks", 1))
    aggressive_ticks = int(execution.get("aggressive_ticks", 1))
    legging_timeout_seconds = float(execution.get("legging_timeout_seconds", 2.0))
    latency_ticks = int(execution.get("latency_ticks", 0))
    market_impact_ticks = int(execution.get("market_impact_ticks", 0))
    metadata_timeout_seconds = float(execution.get("metadata_timeout_seconds", 10.0))
    if any(
        value < 0
        for value in (
            slippage_ticks,
            aggressive_ticks,
            legging_timeout_seconds,
            latency_ticks,
            market_impact_ticks,
        )
    ):
        raise ValueError("execution safety values cannot be negative")
    if metadata_timeout_seconds <= 0:
        raise ValueError("execution metadata_timeout_seconds must be positive")

    paths = data.get("paths", {})
    alert = data.get("alert", {})
    return AppConfig(
        mode=mode,
        initial_capital=initial_capital,
        contracts=contracts,
        pairs=pairs,
        risk=risk,
        ctp=ctp,
        slippage_ticks=slippage_ticks,
        aggressive_ticks=aggressive_ticks,
        auto_flatten_imbalance=bool(execution.get("auto_flatten_imbalance", True)),
        legging_timeout_seconds=legging_timeout_seconds,
        conservative_simulation=bool(execution.get("conservative_simulation", False)),
        latency_ticks=latency_ticks,
        market_impact_ticks=market_impact_ticks,
        require_live_metadata=bool(execution.get("require_live_metadata", mode == "live")),
        metadata_timeout_seconds=metadata_timeout_seconds,
        state_path=str(paths.get("state", "runtime/state.json")),
        log_path=str(paths.get("log", "runtime/afuture.log")),
        report_path=str(paths.get("report", "runtime/report.json")),
        journal_path=str(paths.get("journal", "runtime/audit.jsonl")),
        alert_path=str(paths.get("alert", "runtime/alerts.jsonl")),
        alert_webhook=str(alert.get("webhook", "")),
        auto=auto,
        directional=directional,
        contract_catalog=contract_catalog,
    )


def _load_contracts(rows: list[dict]) -> dict[str, ContractSpec]:
    contracts: dict[str, ContractSpec] = {}
    for raw in rows:
        fee = FeeSpec(
            **{
                key: float(value)
                for key, value in raw.get("fee", {}).items()
                if key in FeeSpec.__dataclass_fields__
            }
        )
        spec = ContractSpec(
            symbol=str(raw["symbol"]),
            exchange=str(raw["exchange"]).upper(),
            multiplier=float(raw["multiplier"]),
            price_tick=float(raw["price_tick"]),
            margin_rate_long=float(raw["margin_rate_long"]),
            margin_rate_short=float(raw["margin_rate_short"]),
            fee=fee,
        )
        if not spec.symbol or spec.symbol in contracts:
            raise ValueError(f"duplicate or empty contract symbol: {spec.symbol}")
        if spec.multiplier <= 0 or spec.price_tick <= 0:
            raise ValueError(f"invalid multiplier/price_tick: {spec.symbol}")
        if not 0 < spec.margin_rate_long < 1 or not 0 < spec.margin_rate_short < 1:
            raise ValueError(f"invalid margin rate: {spec.symbol}")
        if any(value < 0 for value in spec.fee.__dict__.values()):
            raise ValueError(f"fee cannot be negative: {spec.symbol}")
        contracts[spec.symbol] = spec
    return contracts


def _load_contract_catalog(rows: list[dict]) -> list[ContractInfo]:
    """从研究配置提取自动回放所需的品种、挂牌边界和到期日。"""
    result: list[ContractInfo] = []
    for raw in rows:
        expiry = str(raw.get("expiry", "")).strip()
        if not expiry:
            continue
        date.fromisoformat(expiry)
        listing = str(raw.get("listing", "")).strip()
        if listing:
            date.fromisoformat(listing)
        symbol = str(raw["symbol"])
        product = str(raw.get("product", "")).strip()
        if not product:
            product = _contract_root(symbol)
        result.append(
            ContractInfo(
                symbol=symbol,
                exchange=str(raw["exchange"]).upper(),
                product=product,
                expiry=expiry,
                listing=listing,
            )
        )
    return result


def _load_pairs(
    rows: list[dict], contracts: dict[str, ContractSpec], mode: str
) -> list[PairConfig]:
    pairs: list[PairConfig] = []
    pair_ids: set[str] = set()
    used_symbols: set[str] = set()

    for source in rows:
        raw = dict(source)
        if "session_windows" in raw:
            raw["session_windows"] = tuple(raw["session_windows"])
        pair = PairConfig(**raw)

        if not pair.pair_id or pair.pair_id in pair_ids:
            raise ValueError(f"duplicate or empty pair_id: {pair.pair_id}")
        pair_ids.add(pair.pair_id)
        if pair.near_symbol == pair.far_symbol:
            raise ValueError(f"pair {pair.pair_id} uses the same contract twice")
        if pair.volume <= 0:
            raise ValueError(f"pair {pair.pair_id} volume must be positive")
        if pair.sample_seconds < 0:
            raise ValueError(f"pair {pair.pair_id} sample_seconds cannot be negative")
        if pair.lookback < 2 or not 0 <= pair.exit_z < pair.entry_z < pair.stop_z:
            raise ValueError(f"pair {pair.pair_id} has invalid z-score parameters")
        if pair.max_holding_samples < 0:
            raise ValueError(f"pair {pair.pair_id} max_holding_samples cannot be negative")
        if pair.structural_mean_shift_z <= 0 or pair.structural_vol_ratio <= 1:
            raise ValueError(f"pair {pair.pair_id} has invalid structural-break parameters")
        if pair.min_net_edge < 0 or pair.legging_buffer < 0:
            raise ValueError(f"pair {pair.pair_id} net-edge parameters cannot be negative")
        for window in pair.session_windows:
            _validate_session_window(pair.pair_id, window)
        if _contract_root(pair.near_symbol) != _contract_root(pair.far_symbol):
            raise ValueError(f"pair {pair.pair_id} is not a same-product calendar spread")

        for symbol in (pair.near_symbol, pair.far_symbol):
            spec = contracts.get(symbol)
            if spec is None:
                raise ValueError(f"pair {pair.pair_id} missing contract spec: {symbol}")
            if spec.exchange != pair.exchange.upper():
                raise ValueError(f"pair {pair.pair_id} exchange does not match {symbol}")
            if symbol in used_symbols:
                raise ValueError(f"contract {symbol} is reused by multiple pairs")
            used_symbols.add(symbol)

        if mode == "live":
            if not pair.expiry_near or not pair.expiry_far:
                raise ValueError(f"pair {pair.pair_id} expiry dates are required in live mode")
            near_expiry = date.fromisoformat(pair.expiry_near)
            far_expiry = date.fromisoformat(pair.expiry_far)
            if near_expiry >= far_expiry:
                raise ValueError(f"pair {pair.pair_id} expiry_near must be before expiry_far")
            if not pair.session_windows:
                raise ValueError(f"pair {pair.pair_id} session_windows are required in live mode")
        pairs.append(pair)
    return pairs


def _validate_session_window(pair_id: str, raw: str) -> None:
    """只验证格式和时间范围；跨午夜窗口例如 21:00-02:30 合法。"""
    match = re.fullmatch(r"(\d{2}:\d{2})-(\d{2}:\d{2})", raw)
    if not match:
        raise ValueError(f"pair {pair_id} has invalid session window: {raw}")
    for value in match.groups():
        try:
            datetime.strptime(value, "%H:%M")
        except ValueError as exc:
            raise ValueError(f"pair {pair_id} has invalid session window: {raw}") from exc
    if match.group(1) == match.group(2):
        raise ValueError(f"pair {pair_id} session window cannot be zero length")


def _load_auto(raw: dict, mode: str) -> AutoConfig:
    """读取自动发现配置；实盘启用时必须显式给出交易时段。"""
    values = dict(raw)
    for name in ("products", "exchanges", "session_windows"):
        if name in values:
            values[name] = tuple(str(item) for item in values[name])
    auto = AutoConfig(
        **{key: value for key, value in values.items() if key in AutoConfig.__dataclass_fields__}
    )
    auto.validate()
    if auto.enabled:
        for window in auto.session_windows:
            _validate_session_window("auto", window)
        if mode == "live" and not auto.session_windows:
            raise ValueError("auto session_windows are required in live mode")
    return auto


def _load_directional(raw: dict) -> DirectionalConfig:
    values = dict(raw)
    for name in ("products", "exchanges"):
        if name in values:
            values[name] = tuple(str(item) for item in values[name])
    config = DirectionalConfig(
        **{
            key: value
            for key, value in values.items()
            if key in DirectionalConfig.__dataclass_fields__
        }
    )
    config.validate()
    return config


def _load_ctp(raw: dict, mode: str):
    if mode != "live":
        return None
    from .broker.ctp import CtpCredentials

    required_env = {
        "user_id": "AFUTURE_CTP_USER",
        "password": "AFUTURE_CTP_PASSWORD",
        "broker_id": "AFUTURE_CTP_BROKER",
    }
    values = {name: os.getenv(env_name, "") for name, env_name in required_env.items()}
    missing = [env_name for name, env_name in required_env.items() if not values[name]]
    if missing:
        raise ValueError(f"missing CTP environment variables: {', '.join(missing)}")

    td_address = str(raw.get("td_address", "")).strip()
    md_address = str(raw.get("md_address", "")).strip()
    if not td_address or not md_address:
        raise ValueError("ctp td_address and md_address are required")
    environment = str(raw.get("environment", "test")).lower()
    if environment not in {"test", "production"}:
        raise ValueError("ctp.environment must be test or production")

    return CtpCredentials(
        **values,
        td_address=td_address,
        md_address=md_address,
        app_id=os.getenv("AFUTURE_CTP_APP_ID", ""),
        auth_code=os.getenv("AFUTURE_CTP_AUTH_CODE", ""),
        environment=environment,
    )


def _contract_root(symbol: str) -> str:
    match = re.match(r"([A-Za-z]+)", symbol)
    return match.group(1).lower() if match else symbol.lower()
