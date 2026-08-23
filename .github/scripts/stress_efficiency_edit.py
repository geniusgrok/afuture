# message: feat: attribute production directional turnover
from pathlib import Path

path = Path("afuture/directional_acceptance.py")
text = path.read_text(encoding="utf-8")


def once(old: str, new: str) -> None:
    global text
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"expected one match, got {count}: {old[:80]!r}")
    text = text.replace(old, new, 1)


once(
    "from .directional import RebalancePlan\nfrom .directional_risk import DirectionalRiskGovernor\n",
    "from .directional import RebalancePlan\nfrom .directional_efficiency import attribute_rebalance_deltas\nfrom .directional_risk import DirectionalRiskGovernor\n",
)

once(
    "    def _gross_notional(\n",
    """    def _attribute_normal_turnover(
        self,
        *,
        original_lots: Mapping[str, int],
        target_lots: Mapping[str, int],
        deltas: Mapping[str, int],
        prices: Mapping[str, float],
    ) -> dict[str, float]:
        symbols = set(original_lots) | set(target_lots) | set(deltas)
        lot_notionals = {
            symbol: float(prices.get(symbol, 0.0))
            * PRODUCT_MULTIPLIERS[self._product(symbol)]
            for symbol in symbols
        }
        products = {symbol: self._product(symbol) for symbol in symbols}
        return attribute_rebalance_deltas(
            original_lots=original_lots,
            target_lots=target_lots,
            executed_deltas=deltas,
            lot_notionals=lot_notionals,
            symbol_products=products,
        )

    def _gross_notional(
""",
)

once(
    "            turnover_notional = 0.0\n            risk_reason = \"\"\n",
    """            turnover_notional = 0.0
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
""",
)

zero_columns = """                        \"turnover_notional\": 0.0,
                        \"turnover_roll\": 0.0,
                        \"turnover_resize\": 0.0,
                        \"turnover_reversal\": 0.0,
                        \"turnover_entry_exit\": 0.0,
                        \"turnover_daily_circuit\": 0.0,
                        \"turnover_hard_halt\": 0.0,
                        \"turnover_gross_guard\": 0.0,
"""
old_zero = "                        \"turnover_notional\": 0.0,\n"
if text.count(old_zero) != 2:
    raise SystemExit(f"expected two zero-turnover output rows, got {text.count(old_zero)}")
text = text.replace(old_zero, zero_columns, 2)

risk_flatten = """                if lots:
                    closing = {symbol: -volume for symbol, volume in lots.items()}
                    close_turnover = self._turnover(closing, open_prices)
                    turnover_notional += close_turnover
                    equity -= close_turnover * cost_rate
                    lots.clear()
                if risk_reason == "daily loss limit reached":
                    daily_circuit = True
                else:
                    halted = True
"""
risk_flatten_new = """                if lots:
                    closing = {symbol: -volume for symbol, volume in lots.items()}
                    close_turnover = self._turnover(closing, open_prices)
                    turnover_notional += close_turnover
                    if risk_reason == "daily loss limit reached":
                        turnover_daily_circuit += close_turnover
                    else:
                        turnover_hard_halt += close_turnover
                    equity -= close_turnover * cost_rate
                    lots.clear()
                if risk_reason == "daily loss limit reached":
                    daily_circuit = True
                else:
                    halted = True
"""
once(risk_flatten, risk_flatten_new)

once(
    """                phase = self.rebalance_plan(
                    current_lots=lots,
                    target_lots=target,
                )
""",
    """                normal_original_lots = dict(lots)
                normal_target_lots = dict(target)
                phase = self.rebalance_plan(
                    current_lots=lots,
                    target_lots=target,
                )
""",
)

once(
    """                    turnover_notional += reduction_turnover
                    equity -= reduction_turnover * cost_rate
                    self._apply_deltas(lots, phase.reductions)
""",
    """                    turnover_notional += reduction_turnover
                    for symbol, delta in phase.reductions.items():
                        normal_deltas[symbol] = normal_deltas.get(symbol, 0) + int(delta)
                    equity -= reduction_turnover * cost_rate
                    self._apply_deltas(lots, phase.reductions)
""",
)

once(
    """                        turnover_notional += opening_turnover
                        equity -= opening_turnover * cost_rate
                        self._apply_deltas(lots, phase.openings)
""",
    """                        turnover_notional += opening_turnover
                        for symbol, delta in phase.openings.items():
                            normal_deltas[symbol] = normal_deltas.get(symbol, 0) + int(delta)
                        equity -= opening_turnover * cost_rate
                        self._apply_deltas(lots, phase.openings)
""",
)

once(
    """                    else:
                        first_divergence = first_divergence or margin_reject

                intraday_pnl = 0.0
""",
    """                    else:
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
""",
)

close_flatten = """                    if lots:
                        closing = {symbol: -volume for symbol, volume in lots.items()}
                        close_turnover = self._turnover(closing, close_prices)
                        turnover_notional += close_turnover
                        equity -= close_turnover * cost_rate
                        lots.clear()
                    if risk_reason == "daily loss limit reached":
                        daily_circuit = True
                    else:
                        halted = True
"""
close_flatten_new = """                    if lots:
                        closing = {symbol: -volume for symbol, volume in lots.items()}
                        close_turnover = self._turnover(closing, close_prices)
                        turnover_notional += close_turnover
                        if risk_reason == "daily loss limit reached":
                            turnover_daily_circuit += close_turnover
                        else:
                            turnover_hard_halt += close_turnover
                        equity -= close_turnover * cost_rate
                        lots.clear()
                    if risk_reason == "daily loss limit reached":
                        daily_circuit = True
                    else:
                        halted = True
"""
once(close_flatten, close_flatten_new)

once(
    """                        turnover_notional += guard_turnover
                        equity -= guard_turnover * cost_rate
""",
    """                        turnover_notional += guard_turnover
                        turnover_gross_guard += guard_turnover
                        equity -= guard_turnover * cost_rate
""",
)

post_guard = """                                turnover_notional += close_turnover
                                equity -= close_turnover * cost_rate
                                lots.clear()
                            if risk_reason == "daily loss limit reached":
                                daily_circuit = True
                            else:
                                halted = True
"""
post_guard_new = """                                turnover_notional += close_turnover
                                if risk_reason == "daily loss limit reached":
                                    turnover_daily_circuit += close_turnover
                                else:
                                    turnover_hard_halt += close_turnover
                                equity -= close_turnover * cost_rate
                                lots.clear()
                            if risk_reason == "daily loss limit reached":
                                daily_circuit = True
                            else:
                                halted = True
"""
once(post_guard, post_guard_new)

once(
    """                    \"turnover_notional\": turnover_notional,
                    \"gross_notional\": gross_notional,
""",
    """                    \"turnover_notional\": turnover_notional,
                    \"turnover_roll\": turnover_roll,
                    \"turnover_resize\": turnover_resize,
                    \"turnover_reversal\": turnover_reversal,
                    \"turnover_entry_exit\": turnover_entry_exit,
                    \"turnover_daily_circuit\": turnover_daily_circuit,
                    \"turnover_hard_halt\": turnover_hard_halt,
                    \"turnover_gross_guard\": turnover_gross_guard,
                    \"gross_notional\": gross_notional,
""",
)

once(
    """                    \"equity\", \"daily_return\", \"turnover_notional\",
                    \"gross_notional\", \"margin\", \"risk_reason\",
""",
    """                    \"equity\", \"daily_return\", \"turnover_notional\",
                    \"turnover_roll\", \"turnover_resize\", \"turnover_reversal\",
                    \"turnover_entry_exit\", \"turnover_daily_circuit\",
                    \"turnover_hard_halt\", \"turnover_gross_guard\",
                    \"gross_notional\", \"margin\", \"risk_reason\",
""",
)

path.write_text(text, encoding="utf-8")
