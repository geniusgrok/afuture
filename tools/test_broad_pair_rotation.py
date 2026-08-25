import importlib.util
import sys
from pathlib import Path

# Make sibling research modules importable exactly as the workflow executes them.
tools_dir = str(Path(__file__).resolve().parent)
if tools_dir not in sys.path:
    sys.path.insert(0, tools_dir)

spec = importlib.util.spec_from_file_location(
    "rotation_research",
    Path(__file__).with_name("evaluate_broad_pair_rotation.py"),
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

base = module.base
lookup = {
    "A/M": base.EconomicPair("A", "M", "DCE", "soy"),
    "M/Y": base.EconomicPair("M", "Y", "DCE", "soy"),
    "FG/SA": base.EconomicPair("FG", "SA", "CZCE", "glass_soda"),
}

# Ranking is deterministic and strongest-first. With max_active_pairs=1 the
# overlapping A/M and M/Y candidates cannot both consume capital.
selected = module._select_pair_ids(
    [("M/Y", 2.1), ("A/M", 2.5), ("FG/SA", 1.9)],
    lookup,
    max_active_pairs=1,
)
assert selected == ["A/M"]

# With two slots, the selector keeps the strongest pair and then a disjoint pair.
selected = module._select_pair_ids(
    [("M/Y", 2.1), ("A/M", 2.5), ("FG/SA", 1.9)],
    lookup,
    max_active_pairs=2,
)
assert selected == ["A/M", "FG/SA"]

assert module.MAX_ACTIVE_PAIRS == 1
assert base.MAX_GROSS_LEVERAGE == 2.0
