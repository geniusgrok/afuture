"""Execute the staged source edit after tightening known non-unique anchors."""
from pathlib import Path

path = Path(".github/scripts/stress_efficiency_edit.py")
source = path.read_text(encoding="utf-8")

old = '''text = replace_once(text, ''' + "'''" + '''                phase = self.rebalance_plan(
                    current_lots=lots,
                    target_lots=target,
                )
''' + "'''" + ''', ''' + "'''" + '''                normal_original_lots = dict(lots)
                normal_target_lots = dict(target)
                phase = self.rebalance_plan(
                    current_lots=lots,
                    target_lots=target,
                )
''' + "'''" + ''', "acceptance original target")'''
new = '''text = replace_once(text, ''' + "'''" + '''                phase = self.rebalance_plan(
                    current_lots=lots,
                    target_lots=target,
                )
                if phase.reductions:
''' + "'''" + ''', ''' + "'''" + '''                normal_original_lots = dict(lots)
                normal_target_lots = dict(target)
                phase = self.rebalance_plan(
                    current_lots=lots,
                    target_lots=target,
                )
                if phase.reductions:
''' + "'''" + ''', "acceptance original target")'''
if source.count(old) != 1:
    raise SystemExit(f"runner expected one staged acceptance anchor, got {source.count(old)}")
source = source.replace(old, new, 1)

old = '''text = replace_once(
    text,
    "        self.activity_tracker = activity_tracker\\n",
    "        self.activity_tracker = activity_tracker\\n        self.completed_returns_provider = completed_returns_provider\\n",
    "runtime provider assignment",
)'''
new = '''text = replace_once(
    text,
    "        else:\\n            self.activity_tracker = None\\n        self._catalog_by_symbol: dict[str, object] = {}\\n",
    "        else:\\n            self.activity_tracker = None\\n        self.completed_returns_provider = completed_returns_provider\\n        self._catalog_by_symbol: dict[str, object] = {}\\n",
    "runtime provider assignment",
)'''
if source.count(old) != 1:
    raise SystemExit(f"runner expected one staged runtime provider anchor, got {source.count(old)}")
source = source.replace(old, new, 1)

exec(compile(source, str(path), "exec"), {"__name__": "__main__", "__file__": str(path)})
