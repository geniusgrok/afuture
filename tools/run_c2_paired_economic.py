"""Replay B0_M and the sole C2_M on the same verified D1 and risk/account chain."""
import argparse
import json
import sys
import types
from pathlib import Path

import numpy as np
import pandas as pd

sys.modules['akshare'] = types.ModuleType('akshare')
SOURCE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(SOURCE), str(SOURCE / 'tools')]
import evaluate_directional_stress80_final as pipeline
import evaluate_directional_stress90_final as stress90
import evaluate_directional_production_mechanics as mechanics
import evaluate_directional_60m_oi_confirmation as oi_gate
from afuture.directional_stress90_policy import STRESS90_POLICY, build_stress90_candidate_path, candidate_weight_digest
from afuture.directional_concentration_freeze import ExpandingMedianConcentrationFreezeDirectionalProductionAcceptance as Account
from afuture.directional_acceptance import ProductionMechanicsConfig
from afuture.execution_aligned_policy import ExecutionAlignedAggressivePolicy, HOLDING_SCORE_SOURCE

cli = argparse.ArgumentParser()
cli.add_argument('label', choices=('B0', 'C2', 'B0_exAG', 'C2_exAG'))
cli.add_argument('--evidence', type=Path, required=True)
cli.add_argument('--path-only', action='store_true')
opts = cli.parse_args()
ROOT = opts.evidence.resolve()
ROOT.mkdir(parents=True, exist_ok=True)
specific, continuous, frozen_base, bars, manifest = pipeline._load_inputs(
    ROOT.parent / 'restored/inputs'
)
specific = pd.concat([specific, pd.read_csv(ROOT.parent / 'D1/D1_specific_patch.csv')],
                     ignore_index=True)
continuous = pd.concat([continuous, pd.read_csv(ROOT.parent / 'D1/D1_continuous_patch.csv')],
                       ignore_index=True)
extension = ROOT.parent / 'restored/replay-out-final'
new_continuous = pd.read_csv(extension / 'market_normalized/continuous_provider_overlap_and_extension.csv')
new_specific = pd.read_csv(extension / 'market_normalized/specific_provider_overlap_and_extension.csv')
new_continuous = new_continuous.loc[new_continuous.date > '2026-08-20']
new_specific = new_specific.loc[new_specific.date > '2026-08-20']
continuous = pd.concat([continuous, new_continuous], ignore_index=True)
specific = pd.concat([specific, new_specific], ignore_index=True)
continuous['date'] = pd.to_datetime(continuous['date'])
opens = continuous.pivot(index='date', columns='product', values='open').sort_index()
closes = continuous.pivot(index='date', columns='product', values='close').sort_index()
assert len(opens) == 992 and opens.index.is_unique
flow_cache = ROOT.parent / 'restored/frozen_lagged_oi_flow.pkl'
if flow_cache.exists():
    lagged = pd.read_pickle(flow_cache)
else:
    flow = oi_gate.build_daily_price_oi_flow(bars)
    lagged = oi_gate.lag_flow_to_target_days(flow, target_days=frozen_base.index, products=frozen_base.columns)
    lagged.to_pickle(flow_cache)
extension_flow = pd.read_csv(extension / 'strategy/oi_flow_available_by_target_day.csv', index_col=0, parse_dates=True)
all_flow = pd.concat([lagged, extension_flow]).sort_index().reindex(columns=STRESS90_POLICY.oi_products)
assert all_flow.index.is_unique
archived_new = pd.read_csv(extension / 'strategy/survivor_weights.csv', index_col=0, parse_dates=True)
frozen_new_base = pd.read_csv(extension / 'strategy/base_weights.csv', index_col=0, parse_dates=True)

def metrics(result, cost):
    daily = result.daily
    eq = daily['equity'].astype(float)
    events = result.events
    turn = float(daily['turnover_notional'].sum())
    gross = float(events.loc[events['kind'].astype(str).eq('pnl'), 'gross_pnl'].sum())
    return {'final_equity': float(eq.iloc[-1]), 'net_profit': float(eq.iloc[-1] - 500000),
            'mdd': float((1 - eq / eq.cummax()).max()), 'worst_daily_return': float(eq.pct_change().min()),
            'turnover_notional': turn, 'transaction_cost': turn * cost / 10000,
            'gross_signal_pnl': gross, 'net_alpha_per_turnover_bps': (gross / turn * 10000 - cost) if turn else 0,
            'max_margin_ratio': float(daily['margin_ratio'].max()) if 'margin_ratio' in daily else None,
            'columns': list(daily.columns)}

def run(label, holding, exclude_ag):
    products = tuple(p for p in STRESS90_POLICY.products if not (exclude_ag and p == 'AG'))
    base_cache = ROOT / (label + '_base_weights.csv')
    if base_cache.exists():
        base = pd.read_csv(base_cache,index_col=0,parse_dates=True)
    else:
        base = ExecutionAlignedAggressivePolicy(
            products=products,
            meta_score_source=HOLDING_SCORE_SOURCE if holding else
                'continuous_intraday_base_rank_stress_survival',
        ).weight_history(
            opens[list(products)], closes[list(products)],
            **({'specific_contracts': specific} if holding else {}),
        )
    # D1 repairs five actual input cells; B0(D1) can legitimately differ from
    # the archived B0(D0). Both sides of the paired comparison use D1.
    base = base.reindex(columns=STRESS90_POLICY.products, fill_value=0.0)
    base.to_csv(ROOT / (label + '_base_weights.csv'))
    print(json.dumps({'stage':'selector', 'label': label, 'days':len(base)}), flush=True)
    target_days = frozen_base.index.append(frozen_new_base.index)
    assert len(target_days) == 980 and target_days.is_unique
    path = build_stress90_candidate_path(base_weights=base.loc[target_days], completed_close_prices=pipeline.continuous_close_panel(continuous,list(STRESS90_POLICY.products)),confirming_flow=all_flow)
    weights = path.survivor_weights
    weights.to_csv(ROOT / (label + '_survivor_weights.csv'))
    path.oi_confirmed_weights.to_csv(ROOT / (label + '_oi_confirmed_weights.csv'))
    path.cost_approved_weights.to_csv(ROOT / (label + '_cost_approved_weights.csv'))
    pd.DataFrame({'date':weights.index,'hhi':path.current_hhi.to_numpy(),
                  'strictly_prior_median':path.prior_hhi_median.to_numpy(),
                  'concentration_freeze':path.concentration_freeze.to_numpy(),
                  'decision_digest':[d.daily_decision_digest for d in path.decisions],
                  'input_days':[json.dumps(dict(d.input_days)) for d in path.decisions],
                  'input_digests':[json.dumps(dict(d.input_digests)) for d in path.decisions],
                  'layer_digests':[json.dumps(dict(d.layer_digests)) for d in path.decisions],
                  }).to_csv(ROOT / (label + '_daily_decisions.csv'),index=False)
    print(json.dumps({'stage':'pipeline', 'label': label,'digest': candidate_weight_digest(weights)}),flush=True)
    if opts.path_only:
        return
    full = weights.loc['2022-09-06':'2026-09-22']
    assert len(full) == 980
    specific_sample = specific.loc[specific['product'].ne('AG')].copy() if exclude_ag else specific
    prepared = Account().prepare_contracts(specific_sample)
    historic = stress90.completed_concentrations_before(weights,start=full.index[0])
    output = {'label':label, 'days':len(full),'path_digest':candidate_weight_digest(weights),'scenarios':{}}
    for scenario,cost,margin in [('base',mechanics.BASE_COST_BPS,mechanics.BASE_MARGIN_PROXY),('stress',mechanics.STRESS_COST_BPS,mechanics.STRESS_MARGIN_PROXY)]:
        account = Account(ProductionMechanicsConfig(initial_capital=500000.,margin_rate_proxy=margin),completed_concentrations=historic)
        result = account.simulate(specific_sample,full,cost_bps=cost,prepared=prepared)
        result.daily.rename_axis('date').reset_index().to_csv(ROOT / (label+'_'+scenario+'_daily.csv'),index=False)
        result.events.to_csv(ROOT / (label+'_'+scenario+'_events.csv'),index=False)
        output['scenarios'][scenario]=metrics(result,cost)
        print(json.dumps({'stage':'account','label':label,'scenario':scenario,**output['scenarios'][scenario]}),flush=True)
    (ROOT/(label+'_summary.json')).write_text(json.dumps(output,indent=2))
    return output

if __name__ == '__main__':
    run(opts.label,opts.label.startswith('C2'),opts.label.endswith('exAG'))
