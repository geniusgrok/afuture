from __future__ import annotations

import ast
import inspect
import textwrap


def test_operator_roll_forward_has_no_order_or_cancel_callsite() -> None:
    """The operator continuity command has no direct Broker write callsite."""
    from afuture.cli import _run_stress90_operator_roll_forward

    tree = ast.parse(textwrap.dedent(inspect.getsource(_run_stress90_operator_roll_forward)))
    broker_write_calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"send_order", "cancel_order"}
    }
    assert broker_write_calls == set()


def test_operator_roll_forward_reports_zero_order_and_cancel_counts() -> None:
    """The command's audit result must explicitly report a zero-write lifecycle."""
    from afuture.cli import _run_stress90_operator_roll_forward

    source = inspect.getsource(_run_stress90_operator_roll_forward)
    assert '"orders_sent": 0' in source
    assert '"cancels_sent": 0' in source
