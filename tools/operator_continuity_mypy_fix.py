from pathlib import Path

# Temporary development-only patch; removed before the final PR.
path = Path("afuture/stress90_operator_continuity.py")
text = path.read_text(encoding="utf-8")
text = text.replace(
    "    for value, name in (\n        (evidence.source_generic_state_sequence,",
    "    for sequence_value, name in (\n        (evidence.source_generic_state_sequence,",
    1,
).replace(
    "    ):\n        _positive_sequence(value, name)\n    for value, name in (\n        (evidence.source_generic_state_checksum,",
    "    ):\n        _positive_sequence(sequence_value, name)\n    for digest_value, name in (\n        (evidence.source_generic_state_checksum,",
    1,
).replace(
    "    ):\n        _sha(value, name)\n\n    canonical, runtime_digest",
    "    ):\n        _sha(digest_value, name)\n\n    canonical, runtime_digest",
    1,
).replace(
    "    for value, name in (\n        (evidence.source_last_account_equity,",
    "    for equity_value, name in (\n        (evidence.source_last_account_equity,",
    1,
).replace(
    "    ):\n        _finite(value, name, positive=True)\n    if (",
    "    ):\n        _finite(equity_value, name, positive=True)\n    if (",
    1,
)
if "from typing import TYPE_CHECKING\n" not in text:
    text = text.replace(
        "from tempfile import NamedTemporaryFile\n",
        "from tempfile import NamedTemporaryFile\nfrom typing import TYPE_CHECKING\n",
        1,
    )
if "if TYPE_CHECKING:" not in text:
    text = text.replace(
        "STRESS90_OPERATOR_CONTINUITY_CONFIRMATION =",
        "if TYPE_CHECKING:\n    from .stress90_lifecycle_transaction import Stress90SettlementRollForwardTargets\n\n\nSTRESS90_OPERATOR_CONTINUITY_CONFIRMATION =",
        1,
    )
text = text.replace(
    "    targets: object\n",
    "    targets: Stress90SettlementRollForwardTargets\n",
    1,
).replace(
    '    targets: "Stress90SettlementRollForwardTargets"\n',
    "    targets: Stress90SettlementRollForwardTargets\n",
    1,
)
path.write_text(text, encoding="utf-8")
