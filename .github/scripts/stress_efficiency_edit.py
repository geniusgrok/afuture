# message: feat: integrate directional execution efficiency
from pathlib import Path


def read(name: str) -> str:
    return Path(name).read_text(encoding="utf-8")


def write(name: str, text: str) -> None:
    Path(name).write_text(text, encoding="utf-8")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected one match, got {count}")
    return text.replace(old, new, 1)


def replace_section(text: str, start: str, end: str, new: str, label: str) -> str:
    if text.count(start) != 1 or text.count(end) < 1:
        raise SystemExit(f"{label}: section markers not unique/present")
    left = text.index(start)
    right = text.index(end, left)
    return text[:left] + new + text[right:]


# --- directional.py: adaptive causal soft margin + one-lot increase persistence ---
name = "afuture/directional.py"
text = read(name)
text = replace_once(
    text,
    "from math import floor\nimport re\nfrom typing import Iterable, Mapping\n",
    "from math import floor\nimport re\nfrom statistics import stdev\nfrom typing import Iterable, Mapping\n",
    "directional imports",
)
text = replace_once(
    text,
    "from .models import (\n",
    "from .directional_efficiency import stabilize_one_lot_increases\nfrom .models import (\n",
    "directional efficiency import",
)
new_margin = '''\ndef adaptive_margin_sizing_share(
    *,
    max_margin_ratio: float,
    min_available_ratio: float,
    max_daily_loss_ratio: float,
    completed_returns: Iterable[float] = (),
    volatility_trigger: float = 0.03,
) -> float:
    """Causal soft margin envelope below unchanged account hard gates.

    Missing completed-return evidence keeps the conservative 30%-equivalent envelope.
    With completed evidence, calm conditions can recover some capacity while the formula
    still reserves the configured 5% equity-loss budget plus an observed shock allowance.
    """
    margin_ratio = float(max_margin_ratio)
    available_ratio = float(min_available_ratio)
    daily_loss_ratio = float(max_daily_loss_ratio)
    if not 0 < margin_ratio < 1:
        raise ValueError("max_margin_ratio must be in (0, 1)")
    if not 0 <= available_ratio < 1:
        raise ValueError("min_available_ratio must be in [0, 1)")
    if not 0 < daily_loss_ratio < 1:
        raise ValueError("max_daily_loss_ratio must be in (0, 1)")
    if volatility_trigger <= 0:
        raise ValueError("volatility_trigger must be positive")
    hard_share = min(margin_ratio, 1.0 - available_ratio)
    conservative = max(0.0, hard_share - daily_loss_ratio)
    values = [float(value) for value in completed_returns]
    if not values:
        return min(hard_share, conservative)
    sample = values[-2:]
    sample_vol = stdev(sample) if len(sample) >= 2 else 0.0
    shock = max(volatility_trigger, abs(values[-1]), sample_vol)
    shock = min(max(shock, volatility_trigger), daily_loss_ratio)
    adaptive = hard_share * (1.0 - daily_loss_ratio) / (1.0 + shock)
    return min(hard_share, max(conservative, adaptive))


def margin_sizing_share(
    *,
    max_margin_ratio: float,
    min_available_ratio: float,
    max_daily_loss_ratio: float,
) -> float:
    """Compatibility wrapper for the no-history conservative soft margin envelope."""
    return adaptive_margin_sizing_share(
        max_margin_ratio=max_margin_ratio,
        min_available_ratio=min_available_ratio,
        max_daily_loss_ratio=max_daily_loss_ratio,
        completed_returns=(),
    )

'''
text = replace_section(text, "\ndef margin_sizing_share(\n", "\ndef build_target_lots(\n", new_margin, "margin section")
text = replace_once(
    text,
    "    margin_estimate_buffer: float,\n) -> dict[str, int]:\n",
    "    margin_estimate_buffer: float,\n    completed_returns: Iterable[float] = (),\n    current_lots: Mapping[str, int] | None = None,\n) -> dict[str, int]:\n",
    "margin target signature",
)
old_tail = '''    sizing_share = margin_sizing_share(
        max_margin_ratio=max_margin_ratio,
        min_available_ratio=min_available_ratio,
        max_daily_loss_ratio=max_daily_loss_ratio,
    )
    return fit_target_lots_to_margin_budget(
        requested,
        per_lot_margin,
        margin_budget=float(account.equity) * sizing_share,
    )
'''
new_tail = '''    sizing_share = adaptive_margin_sizing_share(
        max_margin_ratio=max_margin_ratio,
        min_available_ratio=min_available_ratio,
        max_daily_loss_ratio=max_daily_loss_ratio,
        completed_returns=completed_returns,
    )
    fitted = fit_target_lots_to_margin_budget(
        requested,
        per_lot_margin,
        margin_budget=float(account.equity) * sizing_share,
    )
    current = {str(symbol): int(volume) for symbol, volume in (current_lots or {}).items() if int(volume)}
    if not current:
        return fitted
    lot_notionals = {
        symbol: float(ticks_by_symbol[symbol].mid_price) * float(specs[symbol].multiplier)
        for symbol in requested
        if symbol in ticks_by_symbol and symbol in specs
    }
    if not set(current).issubset(lot_notionals) or not set(current).issubset(per_lot_margin):
        return fitted
    return stabilize_one_lot_increases(
        current_lots=current,
        target_lots=fitted,
        lot_notionals=lot_notionals,
        per_lot_margin=per_lot_margin,
        equity=float(account.equity),
        soft_margin_share=sizing_share,
        max_gross_ratio=MAX_GROSS_LEVERAGE,
    )
'''
text = replace_once(text, old_tail, new_tail, "margin target body")
write(name, text)


# --- directional_activity.py: incumbent contract hysteresis on completed evidence ---
name = "afuture/directional_activity.py"
text = read(name)
text = replace_once(text, "from typing import Iterable\n", "from typing import Iterable, Mapping\n", "activity typing")
new_selector = '''def select_contracts_from_activity(
    config: DirectionalConfig,
    catalog: Iterable[ContractInfo],
    snapshot: DirectionalActivitySnapshot | None,
    planned_date: date,
    preferred_symbols: Mapping[str, str] | None = None,
) -> dict[str, ContractInfo]:
    """Choose next-day contracts from completed activity with causal roll hysteresis.

    An eligible incumbent is retained unless one challenger has both strictly higher
    completed-day open interest and strictly higher volume. Expiry/listing/activity
    eligibility remains authoritative and immediately forces a deterministic roll.
    """
    if snapshot is None:
        return {}
    preferred = {str(k).upper(): str(v) for k, v in (preferred_symbols or {}).items()}
    products = {item.upper() for item in config.products}
    exchanges = {item.upper() for item in config.exchanges}
    candidates: dict[str, list[tuple[float, float, date, ContractInfo]]] = {}
    for item in catalog:
        product = item.product.upper()
        if product not in products or item.exchange.upper() not in exchanges:
            continue
        if item.listing:
            try:
                if date.fromisoformat(item.listing) > planned_date:
                    continue
            except ValueError:
                continue
        try:
            expiry = date.fromisoformat(item.expiry)
        except ValueError:
            continue
        if (expiry - planned_date).days < config.min_days_to_expiry:
            continue
        activity = snapshot.contracts.get(item.symbol)
        if activity is None or activity.trading_day != snapshot.trading_day:
            continue
        if activity.volume < config.min_volume or activity.open_interest < config.min_open_interest:
            continue
        candidates.setdefault(product, []).append((activity.open_interest, activity.volume, expiry, item))

    result: dict[str, ContractInfo] = {}
    for product, rows in candidates.items():
        rows.sort(key=lambda row: (-row[0], -row[1], row[2], row[3].symbol))
        incumbent_symbol = preferred.get(product)
        incumbent = next((row for row in rows if row[3].symbol == incumbent_symbol), None)
        if incumbent is not None:
            dominant = [row for row in rows if row[0] > incumbent[0] and row[1] > incumbent[1]]
            if not dominant:
                result[product] = incumbent[3]
                continue
            dominant.sort(key=lambda row: (-row[0], -row[1], row[2], row[3].symbol))
            result[product] = dominant[0][3]
            continue
        result[product] = rows[0][3]
    return result
'''
text = replace_section(text, "def select_contracts_from_activity(\n", "\n", new_selector, "activity selector") if False else text
# selector is the final function, replace from marker to EOF.
marker = "def select_contracts_from_activity(\n"
if text.count(marker) != 1:
    raise SystemExit("activity selector marker")
text = text[:text.index(marker)] + new_selector
write(name, text)


# --- execution_aligned_policy.py: meta switching + product no-trade hysteresis ---
name = "afuture/execution_aligned_policy.py"
text = read(name)
text = replace_once(
    text,
    "import pandas as pd\n\nMAX_GROSS_LEVERAGE",
    "import pandas as pd\n\nfrom .directional_efficiency import should_switch_meta, stabilize_same_direction_weights\n\nMAX_GROSS_LEVERAGE",
    "policy imports",
)
new_weight_history = '''    def weight_history(
        self,
        open_prices: pd.DataFrame,
        close: pd.DataFrame,
    ) -> pd.DataFrame:
        close = _clean_prices(close, self.products)
        open_prices = _clean_prices(open_prices, self.products).reindex(close.index)
        returns = close.pct_change(fill_method=None)
        returns = returns.mask(returns.abs() > MAX_ABS_DAILY_RETURN)
        intraday = close.div(open_prices) - 1.0
        intraday = intraday.mask(intraday.abs() > MAX_ABS_DAILY_RETURN).fillna(0.0)

        base_streams: dict[str, pd.Series] = {}
        stress_streams: dict[str, pd.Series] = {}
        paths: dict[str, pd.DataFrame] = {}
        for template_id, template in zip(self.template_ids, _EXECUTION_TEMPLATES):
            weights = _template_weight_path(returns, template)
            paths[template_id] = weights
            base_streams[template_id] = _intraday_proxy_stream(open_prices, close, weights, cost_bps=BASE_COST_BPS)
            stress_streams[template_id] = _intraday_proxy_stream(open_prices, close, weights, cost_bps=STRESS_COST_BPS)

        base_frame = pd.DataFrame(base_streams).sort_index().fillna(0.0)
        stress_frame = pd.DataFrame(stress_streams).reindex(index=base_frame.index, columns=base_frame.columns).fillna(0.0)
        scores = _robust_trailing_scores(base_frame, stress_frame, lookback=self.meta_lookback)
        names = list(base_frame.columns)
        final = pd.DataFrame(0.0, index=close.index, columns=close.columns)
        selected: list[int] = []
        previous_weights: dict[str, float] = {}

        def aggregate(indices: list[int], timestamp) -> dict[str, float]:
            if not indices:
                return {}
            rows = [paths[names[item]].loc[timestamp] for item in indices]
            series = pd.concat(rows, axis=1).mean(axis=1)
            return {str(product): float(value) for product, value in series.items() if abs(float(value)) > 1e-15}

        for position, timestamp in enumerate(close.index):
            if position >= self.meta_lookback and (not selected or position % self.meta_rebalance == 0):
                row = scores[position]
                valid = np.flatnonzero(np.isfinite(row))
                candidate = ([int(item) for item in valid[np.argsort(-row[valid], kind="stable")][: self.meta_count]] if valid.size else [])
                if not selected:
                    selected = candidate
                elif candidate != selected:
                    incumbent_survives = all(np.isfinite(row[item]) for item in selected)
                    history = base_frame.iloc[position - self.meta_lookback : position]
                    incumbent_mean = float(history.iloc[:, selected].mean(axis=1).mean()) if selected else 0.0
                    candidate_mean = float(history.iloc[:, candidate].mean(axis=1).mean()) if candidate else 0.0
                    if should_switch_meta(
                        incumbent_mean_return=incumbent_mean,
                        candidate_mean_return=candidate_mean,
                        incumbent_weights=aggregate(selected, timestamp),
                        candidate_weights=aggregate(candidate, timestamp),
                        horizon=self.meta_rebalance,
                        cost_bps=STRESS_COST_BPS,
                        incumbent_survives=incumbent_survives,
                    ):
                        selected = candidate

            raw = aggregate(selected, timestamp)
            if position > 0 and raw:
                trailing = intraday.iloc[max(0, position - self.meta_lookback) : position].mean(axis=0).to_dict()
                raw = stabilize_same_direction_weights(
                    previous_weights,
                    raw,
                    trailing_mean_returns=trailing,
                    horizon=self.meta_rebalance,
                    cost_bps=STRESS_COST_BPS,
                )
            if raw:
                final.loc[timestamp] = pd.Series(raw).reindex(final.columns).fillna(0.0)
            previous_weights = {str(product): float(value) for product, value in final.loc[timestamp].items() if abs(float(value)) > 1e-15}

        gross = final.abs().sum(axis=1)
        if bool((gross > MAX_GROSS_LEVERAGE + 1e-10).any()):
            raise AssertionError("execution-aligned policy exceeded 2x gross")
        return final

'''
text = replace_section(text, "    def weight_history(\n", "    def target_weights(\n", new_weight_history, "policy weight_history")
write(name, text)


# --- execution_aligned_runtime.py: live completed-return margin + preferred roll ---
name = "afuture/execution_aligned_runtime.py"
text = read(name)
text = replace_once(
    text,
    "        activity_tracker: DirectionalActivityTracker | None = None,\n        **kwargs,\n",
    "        activity_tracker: DirectionalActivityTracker | None = None,\n        completed_returns_provider=None,\n        **kwargs,\n",
    "runtime constructor signature",
)
text = replace_once(
    text,
    "        self.activity_tracker = activity_tracker\n",
    "        self.activity_tracker = activity_tracker\n        self.completed_returns_provider = completed_returns_provider\n",
    "runtime provider assignment",
)
text = replace_once(
    text,
    "    @staticmethod\n    def _post_reduction_target(positions, reductions) -> dict[str, int]:\n",
    '''    def _completed_returns(self) -> tuple[float, ...]:
        provider = self.completed_returns_provider
        if callable(provider):
            return tuple(float(value) for value in provider())
        return ()

    @staticmethod
    def _post_reduction_target(positions, reductions) -> dict[str, int]:
''',
    "runtime completed returns",
)
old_select = '''        planned_date = self._planned_trading_date(now)
        selected = (
            select_contracts_from_activity(
                self.config, self._catalog, snapshot, planned_date
            )
            if snapshot is not None
            else self.selector.select(self._catalog, self._ticks, planned_date)
        )
'''
new_select = '''        planned_date = self._planned_trading_date(now)
        catalog_by_symbol = {item.symbol: item for item in self._catalog}
        preferred_symbols = {
            catalog_by_symbol[position.symbol].product.upper(): position.symbol
            for position in positions
            if not position.empty and position.symbol in catalog_by_symbol
        }
        selected = (
            select_contracts_from_activity(
                self.config,
                self._catalog,
                snapshot,
                planned_date,
                preferred_symbols=preferred_symbols,
            )
            if snapshot is not None
            else self.selector.select(self._catalog, self._ticks, planned_date)
        )
'''
text = replace_once(text, old_select, new_select, "runtime preferred roll")
old_builder_end = '''            max_daily_loss_ratio=self.risk_manager.config.max_daily_loss_ratio,
            margin_estimate_buffer=self.risk_manager.config.margin_estimate_buffer,
        )
'''
new_builder_end = '''            max_daily_loss_ratio=self.risk_manager.config.max_daily_loss_ratio,
            margin_estimate_buffer=self.risk_manager.config.margin_estimate_buffer,
            completed_returns=self._completed_returns(),
            current_lots={position.symbol: position.net_volume for position in positions if not position.empty},
        )
'''
text = replace_once(text, old_builder_end, new_builder_end, "runtime margin evidence")
write(name, text)


# --- directional_engine.py: expose persisted completed returns to live sizing ---
name = "afuture/directional_engine.py"
text = read(name)
needle = '''        policy = getattr(self.directional_manager, "policy", None)
        if policy is not None and not isinstance(policy, DirectionalRiskScaledPolicy):
'''
replacement = '''        self.directional_manager.completed_returns_provider = (
            lambda: tuple(self.state.recent_daily_returns)
        )
        policy = getattr(self.directional_manager, "policy", None)
        if policy is not None and not isinstance(policy, DirectionalRiskScaledPolicy):
'''
text = replace_once(text, needle, replacement, "engine completed returns provider")
write(name, text)


# --- directional_acceptance.py: attribution, preferred roll and causal evidence plumbing ---
name = "afuture/directional_acceptance.py"
text = read(name)
text = replace_once(text, "from .directional import RebalancePlan\n", "from .directional import RebalancePlan\nfrom .directional_efficiency import attribute_rebalance_deltas\n", "acceptance import")
text = replace_once(
    text,
    "        selected_symbols: Mapping[str, str],\n    ) -> dict[str, int]:\n",
    "        selected_symbols: Mapping[str, str],\n        current_lots: Mapping[str, int] | None = None,\n        completed_returns: tuple[float, ...] = (),\n    ) -> dict[str, int]:\n",
    "acceptance target signature",
)
new_accept_selector = '''    def _select_contracts_from_snapshot(
        self,
        snapshot: pd.DataFrame,
        target_day: pd.Timestamp,
        preferred_symbols: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        day = pd.Timestamp(target_day).normalize()
        eligible = snapshot[(snapshot["delivery"] - day).dt.days >= self.config.min_days_to_delivery]
        eligible = eligible[(eligible["volume"] >= self.config.min_volume) & (eligible["hold"] >= self.config.min_open_interest)]
        preferred = {str(k).upper(): str(v).upper() for k, v in (preferred_symbols or {}).items()}
        result: dict[str, str] = {}
        for product, rows in eligible.groupby("product"):
            rows = rows.sort_values(["hold", "volume", "delivery", "symbol"], ascending=[False, False, True, True])
            if rows.empty:
                continue
            incumbent_symbol = preferred.get(str(product).upper())
            incumbent_rows = rows[rows["symbol"] == incumbent_symbol] if incumbent_symbol else rows.iloc[0:0]
            if not incumbent_rows.empty:
                incumbent = incumbent_rows.iloc[0]
                dominant = rows[(rows["hold"] > float(incumbent["hold"])) & (rows["volume"] > float(incumbent["volume"]))]
                if dominant.empty:
                    result[str(product)] = str(incumbent["symbol"])
                    continue
                dominant = dominant.sort_values(["hold", "volume", "delivery", "symbol"], ascending=[False, False, True, True])
                result[str(product)] = str(dominant.iloc[0]["symbol"])
            else:
                result[str(product)] = str(rows.iloc[0]["symbol"])
        return result

'''
text = replace_section(text, "    def _select_contracts_from_snapshot(\n", "    def _select_contracts_from_normalized(\n", new_accept_selector, "acceptance selector")
text = replace_once(text, "    def _gross_notional(\n", '''    def _attribute_normal_turnover(
        self,
        *,
        original_lots: Mapping[str, int],
        target_lots: Mapping[str, int],
        deltas: Mapping[str, int],
        prices: Mapping[str, float],
    ) -> dict[str, float]:
        symbols = set(original_lots) | set(target_lots) | set(deltas)
        lot_notionals = {symbol: float(prices.get(symbol, 0.0)) * PRODUCT_MULTIPLIERS[self._product(symbol)] for symbol in symbols}
        products = {symbol: self._product(symbol) for symbol in symbols}
        return attribute_rebalance_deltas(
            original_lots=original_lots,
            target_lots=target_lots,
            executed_deltas=deltas,
            lot_notionals=lot_notionals,
            symbol_products=products,
        )

    def _gross_notional(
''', "acceptance attribution helper")
text = replace_once(text, "            turnover_notional = 0.0\n            risk_reason = \"\"\n", '''            turnover_notional = 0.0
            turnover_roll = 0.0
            turnover_resize = 0.0
            turnover_reversal = 0.0
            turnover_entry_exit = 0.0
            turnover_daily_circuit = 0.0
            turnover_hard_halt = 0.0
            turnover_gross_guard = 0.0
            normal_original_lots: dict[str, int] = {}
            normal_target_lots: dict[str, int] = {}
            normal_deltas: dict[str, int] = {}
            risk_reason = ""
''', "acceptance counters")
old_zero = "                        \"turnover_notional\": 0.0,\n"
if text.count(old_zero) != 2:
    raise SystemExit(f"acceptance zero output rows: {text.count(old_zero)}")
zero = old_zero + '''                        "turnover_roll": 0.0,
                        "turnover_resize": 0.0,
                        "turnover_reversal": 0.0,
                        "turnover_entry_exit": 0.0,
                        "turnover_daily_circuit": 0.0,
                        "turnover_hard_halt": 0.0,
                        "turnover_gross_guard": 0.0,
'''
text = text.replace(old_zero, zero, 2)
# classify three forced flatten sites without changing risk semantics.
for prices_name in ("open_prices", "close_prices"):
    old = f'''                    turnover_notional += close_turnover
                    equity -= close_turnover * cost_rate
                    lots.clear()
                if risk_reason == "daily loss limit reached":
'''
    if prices_name == "open_prices":
        new = '''                    turnover_notional += close_turnover
                    if risk_reason == "daily loss limit reached":
                        turnover_daily_circuit += close_turnover
                    else:
                        turnover_hard_halt += close_turnover
                    equity -= close_turnover * cost_rate
                    lots.clear()
                if risk_reason == "daily loss limit reached":
'''
        text = replace_once(text, old, new, "acceptance open flatten")
        break
# close-risk flatten has deeper indentation.
text = replace_once(text, '''                        turnover_notional += close_turnover
                        equity -= close_turnover * cost_rate
                        lots.clear()
                    if risk_reason == "daily loss limit reached":
''', '''                        turnover_notional += close_turnover
                        if risk_reason == "daily loss limit reached":
                            turnover_daily_circuit += close_turnover
                        else:
                            turnover_hard_halt += close_turnover
                        equity -= close_turnover * cost_rate
                        lots.clear()
                    if risk_reason == "daily loss limit reached":
''', "acceptance close flatten")
text = replace_once(text, '''                                turnover_notional += close_turnover
                                equity -= close_turnover * cost_rate
                                lots.clear()
                            if risk_reason == "daily loss limit reached":
''', '''                                turnover_notional += close_turnover
                                if risk_reason == "daily loss limit reached":
                                    turnover_daily_circuit += close_turnover
                                else:
                                    turnover_hard_halt += close_turnover
                                equity -= close_turnover * cost_rate
                                lots.clear()
                            if risk_reason == "daily loss limit reached":
''', "acceptance post-guard flatten")
text = replace_once(text, '''                    selected = self._select_contracts_from_snapshot(
                        activity_by_day[completed_activity_day], day
                    )
''', '''                    preferred_symbols = {self._product(symbol): symbol for symbol in lots}
                    selected = self._select_contracts_from_snapshot(
                        activity_by_day[completed_activity_day],
                        day,
                        preferred_symbols=preferred_symbols,
                    )
''', "acceptance preferred roll")
text = replace_once(text, '''                    selected_symbols=selected_symbols,
                )
''', '''                    selected_symbols=selected_symbols,
                    current_lots=lots,
                    completed_returns=tuple(completed_returns),
                )
''', "acceptance target evidence")
text = replace_once(text, '''                phase = self.rebalance_plan(
                    current_lots=lots,
                    target_lots=target,
                )
''', '''                normal_original_lots = dict(lots)
                normal_target_lots = dict(target)
                phase = self.rebalance_plan(
                    current_lots=lots,
                    target_lots=target,
                )
''', "acceptance original target")
text = replace_once(text, '''                    turnover_notional += reduction_turnover
                    equity -= reduction_turnover * cost_rate
                    self._apply_deltas(lots, phase.reductions)
''', '''                    turnover_notional += reduction_turnover
                    for symbol, delta in phase.reductions.items():
                        normal_deltas[symbol] = normal_deltas.get(symbol, 0) + int(delta)
                    equity -= reduction_turnover * cost_rate
                    self._apply_deltas(lots, phase.reductions)
''', "acceptance reduction attribution")
text = replace_once(text, '''                        turnover_notional += opening_turnover
                        equity -= opening_turnover * cost_rate
                        self._apply_deltas(lots, phase.openings)
''', '''                        turnover_notional += opening_turnover
                        for symbol, delta in phase.openings.items():
                            normal_deltas[symbol] = normal_deltas.get(symbol, 0) + int(delta)
                        equity -= opening_turnover * cost_rate
                        self._apply_deltas(lots, phase.openings)
''', "acceptance opening attribution")
text = replace_once(text, '''                    else:
                        first_divergence = first_divergence or margin_reject

                intraday_pnl = 0.0
''', '''                    else:
                        first_divergence = first_divergence or margin_reject

                if normal_deltas:
                    attributed = self._attribute_normal_turnover(
                        original_lots=normal_original_lots,
                        target_lots=normal_target_lots,
                        deltas=normal_deltas,
                        prices=open_prices,
                    )
                    turnover_roll += attributed["roll"]
                    turnover_resize += attributed["resize"]
                    turnover_reversal += attributed["reversal"]
                    turnover_entry_exit += attributed["entry_exit"]

                intraday_pnl = 0.0
''', "acceptance attribution aggregate")
text = replace_once(text, '''                        turnover_notional += guard_turnover
                        equity -= guard_turnover * cost_rate
''', '''                        turnover_notional += guard_turnover
                        turnover_gross_guard += guard_turnover
                        equity -= guard_turnover * cost_rate
''', "acceptance gross guard attribution")
text = replace_once(text, '''                    "turnover_notional": turnover_notional,
                    "gross_notional": gross_notional,
''', '''                    "turnover_notional": turnover_notional,
                    "turnover_roll": turnover_roll,
                    "turnover_resize": turnover_resize,
                    "turnover_reversal": turnover_reversal,
                    "turnover_entry_exit": turnover_entry_exit,
                    "turnover_daily_circuit": turnover_daily_circuit,
                    "turnover_hard_halt": turnover_hard_halt,
                    "turnover_gross_guard": turnover_gross_guard,
                    "gross_notional": gross_notional,
''', "acceptance output attribution")
text = replace_once(text, '''                    "equity", "daily_return", "turnover_notional",
                    "gross_notional", "margin", "risk_reason",
''', '''                    "equity", "daily_return", "turnover_notional",
                    "turnover_roll", "turnover_resize", "turnover_reversal",
                    "turnover_entry_exit", "turnover_daily_circuit",
                    "turnover_hard_halt", "turnover_gross_guard",
                    "gross_notional", "margin", "risk_reason",
''', "acceptance empty attribution")
write(name, text)


# --- directional_robustness.py: acceptance parity for adaptive margin + one-lot hold ---
write("afuture/directional_robustness.py", '''"""Robust production-mechanics adapters for the execution-aligned directional path."""
from __future__ import annotations

from typing import Mapping

from .directional import adaptive_margin_sizing_share, fit_target_lots_to_margin_budget
from .directional_acceptance import DirectionalProductionAcceptance, PRODUCT_MULTIPLIERS
from .directional_efficiency import stabilize_one_lot_increases


class MarginAwareDirectionalProductionAcceptance(DirectionalProductionAcceptance):
    """Production proxy whose integer target is feasible before opening hard gates."""

    def target_lots(
        self,
        *,
        equity: float,
        product_weights: Mapping[str, float],
        product_open_prices: Mapping[str, float],
        selected_symbols: Mapping[str, str],
        current_lots: Mapping[str, int] | None = None,
        completed_returns: tuple[float, ...] = (),
    ) -> dict[str, int]:
        requested = super().target_lots(
            equity=equity,
            product_weights=product_weights,
            product_open_prices=product_open_prices,
            selected_symbols=selected_symbols,
            current_lots=current_lots,
            completed_returns=completed_returns,
        )
        if not requested or equity <= 0:
            return {}
        symbol_product = {str(symbol): str(product).upper() for product, symbol in selected_symbols.items()}
        per_lot_margin: dict[str, float] = {}
        lot_notionals: dict[str, float] = {}
        for symbol in requested:
            product = symbol_product.get(str(symbol))
            if product is None:
                raise ValueError(f"missing target product for margin estimate: {symbol}")
            price = float(product_open_prices.get(product, 0.0))
            multiplier = PRODUCT_MULTIPLIERS.get(product)
            if price <= 0 or multiplier is None:
                raise ValueError(f"missing positive target margin evidence: {symbol}")
            lot_notionals[str(symbol)] = price * float(multiplier)
            per_lot_margin[str(symbol)] = lot_notionals[str(symbol)] * float(self.config.margin_rate_proxy) * float(self.config.margin_estimate_buffer)
        sizing_share = adaptive_margin_sizing_share(
            max_margin_ratio=self.config.max_margin_ratio,
            min_available_ratio=self.config.min_available_ratio,
            max_daily_loss_ratio=self.config.max_daily_loss_ratio,
            completed_returns=completed_returns,
        )
        fitted = fit_target_lots_to_margin_budget(requested, per_lot_margin, margin_budget=float(equity) * sizing_share)
        current = {str(symbol): int(volume) for symbol, volume in (current_lots or {}).items() if int(volume)}
        if not current or not set(current).issubset(lot_notionals):
            return fitted
        return stabilize_one_lot_increases(
            current_lots=current,
            target_lots=fitted,
            lot_notionals=lot_notionals,
            per_lot_margin=per_lot_margin,
            equity=float(equity),
            soft_margin_share=sizing_share,
            max_gross_ratio=self.config.max_realized_gross_ratio,
        )
''')


# --- evaluator: report execution attribution without altering economics ---
name = "tools/evaluate_directional_production_mechanics.py"
text = read(name)
text = replace_once(text, '''    production_gap: dict[str, dict] = {}
''', '''    turnover_columns = [
        "turnover_roll", "turnover_resize", "turnover_reversal",
        "turnover_entry_exit", "turnover_daily_circuit",
        "turnover_hard_halt", "turnover_gross_guard",
    ]
    def summarize_turnover(frame: pd.DataFrame) -> dict[str, float]:
        values = {column: float(frame[column].sum()) if column in frame else 0.0 for column in turnover_columns}
        values["total"] = float(frame["turnover_notional"].sum()) if "turnover_notional" in frame else 0.0
        values["attributed_total"] = float(sum(values[column] for column in turnover_columns))
        return values
    turnover_attribution = {"base": summarize_turnover(base_daily), "stress": summarize_turnover(stress_daily)}

    production_gap: dict[str, dict] = {}
''', "evaluator turnover summary")
text = replace_once(text, '''        "state_reset_per_window": True,
        "mechanics": {
''', '''        "state_reset_per_window": True,
        "turnover_attribution": turnover_attribution,
        "mechanics": {
''', "evaluator report attribution")
write(name, text)
