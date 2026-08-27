from pathlib import Path
import subprocess

path = Path('.github/live-risk-capacity-fix.py')
text = path.read_text(encoding='utf-8')
old = '''        raw_target_gross = float(facts["target_gross"])
        facts["raw_target_gross"] = raw_target_gross
        facts["scaled_target_gross"] = raw_target_gross * float(config.directional.live_risk_scale)
        facts["raw_product_weights"] = (
            dict(prepared.survivor_weights)
            if prepared is not None
            else dict(policy_state.last_survivor_weights)
        )
        facts["scaled_product_weights"] = {
            product: float(weight) * float(config.directional.live_risk_scale)
            for product, weight in facts["raw_product_weights"].items()
        }
'''
new = '''        raw_product_weights = (
            dict(prepared.survivor_weights)
            if prepared is not None
            else dict(policy_state.last_survivor_weights)
        )
        raw_target_gross = sum(abs(float(value)) for value in raw_product_weights.values())
        facts["raw_target_gross"] = raw_target_gross
        facts["scaled_target_gross"] = raw_target_gross * float(config.directional.live_risk_scale)
        facts["raw_product_weights"] = raw_product_weights
        facts["scaled_product_weights"] = {
            product: float(weight) * float(config.directional.live_risk_scale)
            for product, weight in raw_product_weights.items()
        }
'''
if text.count(old) != 1:
    raise RuntimeError(f'runner status typing anchor mismatch: {text.count(old)}')
path.write_text(text.replace(old, new, 1), encoding='utf-8')
subprocess.run(['python', str(path)], check=True)
