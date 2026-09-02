"""TOML 配置加载与实盘安全校验。"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, cast

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from .auto import AutoConfig
from .config_validation import (
    require_bool,
    require_finite_number,
    require_integer,
    require_keys,
    require_mapping,
    require_string,
    require_string_sequence,
)
from .directional import DirectionalConfig
from .models import ContractInfo, ContractSpec, FeeSpec, PairConfig
from .risk import RiskConfig
from .runtime_paths import PRODUCTION_ACCOUNT_RUNTIME_REGISTRY_PATH


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
    heartbeat_path: Path = field(
        default_factory=lambda: Path("runtime/state.json").with_name("heartbeat.json")
    )
    heartbeat_interval_seconds: float = 5.0
    log_path: str = "runtime/afuture.log"
    report_path: str = "runtime/report.json"
    journal_path: str = "runtime/audit.jsonl"
    alert_path: str = "runtime/alerts.jsonl"
    account_registry_path: str = "/var/lib/afuture/account-runtime-registry.json"
    ctp_environment: str = "test"
    alert_webhook: str = ""
    auto: AutoConfig = field(default_factory=AutoConfig)
    directional: DirectionalConfig = field(default_factory=DirectionalConfig)
    contract_catalog: list[ContractInfo] = field(default_factory=list)


def load_config(
    path: str | Path,
    *,
    require_ctp_credentials: bool = True,
) -> AppConfig:
    """读取配置并在连接柜台之前拒绝明显危险或自相矛盾的参数。

    只有完全本地、只读的运维检查可以跳过凭证注入；地址、模式、策略与风险配置
    仍照常校验，且返回的 ``ctp`` 为 ``None``，不能被误用于连接柜台。
    """
    data = require_mapping(tomllib.loads(Path(path).read_text(encoding="utf-8")), "config")
    require_keys(
        data,
        {
            "system",
            "risk",
            "contracts",
            "pairs",
            "auto",
            "directional",
            "ctp",
            "execution",
            "paths",
            "alert",
        },
        "config",
    )
    system = _section(data, "system", {"mode", "initial_capital"})
    mode = require_string(system.get("mode", "replay"), "system.mode").lower()
    if mode not in {"replay", "live"}:
        raise ValueError("system.mode must be replay or live")

    initial_capital = require_finite_number(
        system.get("initial_capital", 500000), "system.initial_capital"
    )
    if initial_capital <= 0:
        raise ValueError("initial_capital must be positive")

    risk_raw = _section(data, "risk", set(RiskConfig.__dataclass_fields__))
    risk = RiskConfig(**cast(Any, risk_raw))
    risk.validate()

    contract_rows = _rows(data.get("contracts", []), "contracts")
    contracts = _load_contracts(contract_rows)
    contract_catalog = _load_contract_catalog(contract_rows)
    pairs = _load_pairs(_rows(data.get("pairs", []), "pairs"), contracts, mode)
    auto = _load_auto(_section(data, "auto", set(AutoConfig.__dataclass_fields__)), mode)
    directional = _load_directional(
        _section(data, "directional", set(DirectionalConfig.__dataclass_fields__)),
        mode,
    )
    if directional.enabled and (pairs or auto.enabled):
        raise ValueError(
            "directional mode is account-exclusive and cannot run with static pairs or auto"
        )
    if mode == "replay" and auto.enabled and not contract_catalog:
        raise ValueError("replay auto mode requires contract product/expiry metadata")
    ctp_raw = _section(
        data,
        "ctp",
        {"td_address", "md_address", "environment"},
    )
    ctp_environment = require_string(ctp_raw.get("environment", "test"), "ctp.environment").lower()
    ctp = _load_ctp(
        ctp_raw,
        mode,
        require_credentials=require_ctp_credentials,
        require_account_identity=(directional.enabled and directional.policy == "stress90"),
    )
    if mode == "live" and not pairs and not auto.enabled and not directional.enabled:
        raise ValueError(
            "live mode requires static pairs, auto.enabled=true, or directional.enabled=true"
        )

    execution = _section(
        data,
        "execution",
        {
            "slippage_ticks",
            "aggressive_ticks",
            "auto_flatten_imbalance",
            "legging_timeout_seconds",
            "conservative_simulation",
            "latency_ticks",
            "market_impact_ticks",
            "require_live_metadata",
            "metadata_timeout_seconds",
            "heartbeat_interval_seconds",
        },
    )
    slippage_ticks = require_integer(execution.get("slippage_ticks", 1), "execution.slippage_ticks")
    aggressive_ticks = require_integer(
        execution.get("aggressive_ticks", 1), "execution.aggressive_ticks"
    )
    legging_timeout_seconds = require_finite_number(
        execution.get("legging_timeout_seconds", 2.0), "execution.legging_timeout_seconds"
    )
    latency_ticks = require_integer(execution.get("latency_ticks", 0), "execution.latency_ticks")
    market_impact_ticks = require_integer(
        execution.get("market_impact_ticks", 0), "execution.market_impact_ticks"
    )
    metadata_timeout_seconds = require_finite_number(
        execution.get("metadata_timeout_seconds", 10.0), "execution.metadata_timeout_seconds"
    )
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
    heartbeat_interval_seconds = require_finite_number(
        execution.get("heartbeat_interval_seconds", 5),
        "execution.heartbeat_interval_seconds",
    )
    if not 1.0 <= heartbeat_interval_seconds <= 60.0:
        raise ValueError("execution.heartbeat_interval_seconds must be between 1 and 60")

    paths = _section(
        data,
        "paths",
        {"state", "heartbeat", "log", "report", "journal", "alert", "account_registry"},
    )
    state_path = require_string(paths.get("state", "runtime/state.json"), "paths.state")
    raw_heartbeat_path = paths.get("heartbeat")
    if raw_heartbeat_path is None:
        heartbeat_path = Path(state_path).resolve(strict=False).with_name("heartbeat.json")
    else:
        configured_heartbeat_path = require_string(raw_heartbeat_path, "paths.heartbeat")
        if not configured_heartbeat_path.strip():
            raise ValueError("paths.heartbeat must be a non-empty string")
        heartbeat_path = Path(configured_heartbeat_path)
        if not heartbeat_path.is_absolute():
            heartbeat_path = Path.cwd() / heartbeat_path
        heartbeat_path = heartbeat_path.resolve(strict=False)
    account_registry_path = require_string(
        paths.get(
            "account_registry",
            "/var/lib/afuture/account-runtime-registry.json",
        ),
        "paths.account_registry",
    )
    if mode == "live" and directional.enabled and directional.policy == "stress90":
        registry_path = Path(account_registry_path)
        if not registry_path.is_absolute():
            raise ValueError("paths.account_registry must be an absolute machine-level path")
        if registry_path != PRODUCTION_ACCOUNT_RUNTIME_REGISTRY_PATH:
            raise ValueError(
                "paths.account_registry must use the fixed machine-level registry path"
            )
    alert = _section(data, "alert", {"webhook"})
    return AppConfig(
        mode=mode,
        initial_capital=initial_capital,
        contracts=contracts,
        pairs=pairs,
        risk=risk,
        ctp=ctp,
        slippage_ticks=slippage_ticks,
        aggressive_ticks=aggressive_ticks,
        auto_flatten_imbalance=require_bool(
            execution.get("auto_flatten_imbalance", True), "execution.auto_flatten_imbalance"
        ),
        legging_timeout_seconds=legging_timeout_seconds,
        conservative_simulation=require_bool(
            execution.get("conservative_simulation", False), "execution.conservative_simulation"
        ),
        latency_ticks=latency_ticks,
        market_impact_ticks=market_impact_ticks,
        require_live_metadata=require_bool(
            execution.get("require_live_metadata", mode == "live"),
            "execution.require_live_metadata",
        ),
        metadata_timeout_seconds=metadata_timeout_seconds,
        state_path=state_path,
        heartbeat_path=heartbeat_path,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        log_path=require_string(paths.get("log", "runtime/afuture.log"), "paths.log"),
        report_path=require_string(paths.get("report", "runtime/report.json"), "paths.report"),
        journal_path=require_string(paths.get("journal", "runtime/audit.jsonl"), "paths.journal"),
        alert_path=require_string(paths.get("alert", "runtime/alerts.jsonl"), "paths.alert"),
        account_registry_path=account_registry_path,
        ctp_environment=ctp_environment,
        alert_webhook=require_string(alert.get("webhook", ""), "alert.webhook"),
        auto=auto,
        directional=directional,
        contract_catalog=contract_catalog,
    )


def _section(data: Mapping[str, object], name: str, allowed: set[str]) -> Mapping[str, object]:
    raw = require_mapping(data.get(name, {}), name)
    require_keys(raw, allowed, name)
    return raw


def _rows(value: object, name: str) -> list[Mapping[str, object]]:
    if type(value) is not list:
        raise ValueError(f"{name} must be an array of tables")
    return [require_mapping(row, f"{name}[{index}]") for index, row in enumerate(value)]


def _contract_row(source: Mapping[str, object], index: int) -> dict[str, object]:
    raw = dict(source)
    section = f"contracts[{index}]"
    require_keys(
        raw,
        {
            "symbol",
            "exchange",
            "product",
            "expiry",
            "listing",
            "multiplier",
            "price_tick",
            "margin_rate_long",
            "margin_rate_short",
            "fee",
        },
        section,
    )
    for field_name in ("symbol", "exchange", "product", "expiry", "listing"):
        if field_name in raw:
            require_string(raw[field_name], f"{section}.{field_name}")
    for field_name in (
        "multiplier",
        "price_tick",
        "margin_rate_long",
        "margin_rate_short",
    ):
        if field_name in raw:
            require_finite_number(raw[field_name], f"{section}.{field_name}")
    fee = require_mapping(raw.get("fee", {}), f"{section}.fee")
    require_keys(fee, set(FeeSpec.__dataclass_fields__), f"{section}.fee")
    for field_name, value in fee.items():
        require_finite_number(value, f"{section}.fee.{field_name}")
    return raw


def _pair_row(source: Mapping[str, object], index: int) -> dict[str, object]:
    raw = dict(source)
    section = f"pairs[{index}]"
    require_keys(raw, set(PairConfig.__dataclass_fields__), section)
    for field_name in (
        "pair_id",
        "near_symbol",
        "far_symbol",
        "exchange",
        "expiry_near",
        "expiry_far",
        "risk_group",
        "signal_transform",
        "daily_sample_window",
    ):
        if field_name in raw:
            require_string(raw[field_name], f"{section}.{field_name}")
    for field_name in (
        "volume",
        "lookback",
        "sample_seconds",
        "max_holding_samples",
        "entry_trend_window",
    ):
        if field_name in raw:
            require_integer(raw[field_name], f"{section}.{field_name}")
    for field_name in (
        "entry_z",
        "exit_z",
        "stop_z",
        "structural_mean_shift_z",
        "structural_vol_ratio",
        "min_net_edge",
        "legging_buffer",
        "confirmation_retrace_z",
        "min_confirmed_entry_z",
        "max_entry_z_slope",
        "min_mean_reversion_score",
        "max_half_life",
    ):
        if field_name in raw:
            require_finite_number(raw[field_name], f"{section}.{field_name}")
    if "confirm_entry" in raw:
        require_bool(raw["confirm_entry"], f"{section}.confirm_entry")
    return raw


def _load_contracts(rows: list[Mapping[str, object]]) -> dict[str, ContractSpec]:
    contracts: dict[str, ContractSpec] = {}
    for index, source in enumerate(rows):
        raw = _contract_row(source, index)
        fee = FeeSpec(
            **{
                key: require_finite_number(value, f"contracts[{index}].fee.{key}")
                for key, value in require_mapping(
                    raw.get("fee", {}), f"contracts[{index}].fee"
                ).items()
            }
        )
        spec = ContractSpec(
            symbol=require_string(raw["symbol"], f"contracts[{index}].symbol"),
            exchange=require_string(raw["exchange"], f"contracts[{index}].exchange").upper(),
            multiplier=require_finite_number(raw["multiplier"], f"contracts[{index}].multiplier"),
            price_tick=require_finite_number(raw["price_tick"], f"contracts[{index}].price_tick"),
            margin_rate_long=require_finite_number(
                raw["margin_rate_long"], f"contracts[{index}].margin_rate_long"
            ),
            margin_rate_short=require_finite_number(
                raw["margin_rate_short"], f"contracts[{index}].margin_rate_short"
            ),
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


def _load_contract_catalog(rows: list[Mapping[str, object]]) -> list[ContractInfo]:
    """从研究配置提取自动回放所需的品种、挂牌边界和到期日。"""
    result: list[ContractInfo] = []
    for index, source in enumerate(rows):
        raw = _contract_row(source, index)
        expiry = require_string(raw.get("expiry", ""), f"contracts[{index}].expiry").strip()
        if not expiry:
            continue
        date.fromisoformat(expiry)
        listing = require_string(raw.get("listing", ""), f"contracts[{index}].listing").strip()
        if listing:
            date.fromisoformat(listing)
        symbol = require_string(raw["symbol"], f"contracts[{index}].symbol")
        product = require_string(raw.get("product", ""), f"contracts[{index}].product").strip()
        if not product:
            product = _contract_root(symbol)
        result.append(
            ContractInfo(
                symbol=symbol,
                exchange=require_string(raw["exchange"], f"contracts[{index}].exchange").upper(),
                product=product,
                expiry=expiry,
                listing=listing,
            )
        )
    return result


def _load_pairs(
    rows: list[Mapping[str, object]], contracts: dict[str, ContractSpec], mode: str
) -> list[PairConfig]:
    pairs: list[PairConfig] = []
    pair_ids: set[str] = set()
    used_symbols: set[str] = set()

    for index, source in enumerate(rows):
        raw = _pair_row(source, index)
        if "session_windows" in raw:
            raw["session_windows"] = require_string_sequence(
                raw["session_windows"], f"pairs[{index}].session_windows"
            )
        pair = PairConfig(**cast(Any, raw))

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


def _load_auto(raw: Mapping[str, object], mode: str) -> AutoConfig:
    """读取自动发现配置；实盘启用时必须显式给出交易时段。"""
    values = dict(raw)
    for name in ("products", "exchanges", "session_windows"):
        if name in values:
            values[name] = require_string_sequence(values[name], f"auto.{name}")
    auto = AutoConfig(**cast(Any, values))
    auto.validate()
    if auto.enabled:
        for window in auto.session_windows:
            _validate_session_window("auto", window)
        if mode == "live" and not auto.session_windows:
            raise ValueError("auto session_windows are required in live mode")
    return auto


def _load_directional(raw: Mapping[str, object], mode: str) -> DirectionalConfig:
    values = dict(raw)
    for name in ("products", "exchanges"):
        if name in values:
            values[name] = require_string_sequence(values[name], f"directional.{name}")
    if values.get("policy") == "stress90" and "rebalance_window" in values:
        raise ValueError("directional.rebalance_window is not a current Stress-90 field")
    config = DirectionalConfig(**cast(Any, values))
    config.validate()
    if config.account_continuity_mode == "operator_managed" and (
        mode != "live"
        or not config.enabled
        or config.policy != "stress90"
        or not config.account_exclusive
    ):
        raise ValueError(
            "directional.account_continuity_mode=operator_managed requires "
            "system.mode=live, directional.enabled=true, policy=stress90, "
            "and account_exclusive=true"
        )
    if config.enabled and not config.policy:
        raise ValueError("directional.policy must be explicit when directional.enabled=true")
    if config.enabled and config.policy == "stress90":
        from .execution_aligned_policy import FROZEN_PRODUCTS

        if tuple(sorted({item.upper() for item in config.products})) != FROZEN_PRODUCTS:
            raise ValueError("Stress-90 requires the frozen 50-product universe")
    return config


def _load_ctp(
    raw: Mapping[str, object],
    mode: str,
    *,
    require_credentials: bool = True,
    require_account_identity: bool = False,
):
    td_address = require_string(raw.get("td_address", ""), "ctp.td_address").strip()
    md_address = require_string(raw.get("md_address", ""), "ctp.md_address").strip()
    environment = require_string(raw.get("environment", "test"), "ctp.environment").lower()
    if environment not in {"test", "production"}:
        raise ValueError("ctp.environment must be test or production")
    if mode != "live":
        return None
    if not td_address or not md_address:
        raise ValueError("ctp td_address and md_address are required")
    if not require_credentials:
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

    identity_env = {
        "account_id": "AFUTURE_CTP_ACCOUNT_ID",
        "currency_id": "AFUTURE_CTP_CURRENCY_ID",
        "investor_id": "AFUTURE_CTP_INVESTOR_ID",
        "invest_unit_id": "AFUTURE_CTP_INVEST_UNIT_ID",
    }
    identity = {name: os.getenv(env_name, "").strip() for name, env_name in identity_env.items()}
    if require_account_identity:
        missing_identity = [
            identity_env[name] for name in ("account_id", "currency_id") if not identity[name]
        ]
        if missing_identity:
            raise ValueError(
                "missing Stress-90 CTP account identity environment variables: "
                + ", ".join(missing_identity)
            )

    return CtpCredentials(
        **values,
        **identity,
        td_address=td_address,
        md_address=md_address,
        app_id=os.getenv("AFUTURE_CTP_APP_ID", ""),
        auth_code=os.getenv("AFUTURE_CTP_AUTH_CODE", ""),
        environment=environment,
    )


def _contract_root(symbol: str) -> str:
    match = re.match(r"([A-Za-z]+)", symbol)
    return match.group(1).lower() if match else symbol.lower()
