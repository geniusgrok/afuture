"""Execute the staged source edit after tightening one non-unique acceptance anchor."""
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
    raise SystemExit(f"runner expected one staged anchor, got {source.count(old)}")
source = source.replace(old, new, 1)
exec(compile(source, str(path), "exec"), {"__name__": "__main__", "__file__": str(path)})
