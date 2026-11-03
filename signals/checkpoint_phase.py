"""Original checkpoint phase assignment, summaries, transitions and archetypes.
No original token observations are included; explicit input is required.
"""
import pandas as pd
import numpy as np
from pathlib import Path
def clamp(x, lo=0.0, hi=1.0):
    return np.clip(x, lo, hi)

def add_phase(df):
    df = df.copy()
    for c in [
        'unique_buyers_to_checkpoint','buy_sell_ratio_to_checkpoint','top1_wallet_pct_buy_usd_to_checkpoint',
        'total_volume_usd_to_checkpoint','n_trades_to_checkpoint','avg_trade_size_usd_to_checkpoint'
    ]:
        df[c] = pd.to_numeric(df[c], errors='coerce')

    df['B'] = clamp((df['unique_buyers_to_checkpoint'] - 20.0) / 80.0)
    df['P'] = clamp((df['buy_sell_ratio_to_checkpoint'] - 1.0) / 1.0)
    df['S'] = 1.0 - clamp((df['top1_wallet_pct_buy_usd_to_checkpoint'] - 0.20) / 0.30)
    v = clamp((df['total_volume_usd_to_checkpoint'] - 1000.0) / 9000.0)
    n = clamp((df['n_trades_to_checkpoint'] - 20.0) / 180.0)
    t = clamp((df['avg_trade_size_usd_to_checkpoint'] - 20.0) / 100.0)
    df['A'] = (0.5 * v) + (0.3 * n) + (0.2 * t)

    hot = (df['B'] >= 0.60) & (df['P'] >= 0.35) & (df['S'] >= 0.40) & (df['A'] >= 0.35)
    cold = (df['B'] <= 0.20) & (df['P'] <= 0.20) & (df['A'] <= 0.25)
    df['phase'] = np.where(hot, 'Hot', np.where(cold, 'Cold', 'Warm'))
    return df

def phase_summary(df):
    g = df.groupby(['checkpoint_min','phase'], as_index=False).agg(
        n_rows=('mint','size'),
        avg_B=('B','mean'), avg_P=('P','mean'), avg_S=('S','mean'), avg_A=('A','mean'),
        p50_buyers=('unique_buyers_to_checkpoint','median'),
        p50_ratio=('buy_sell_ratio_to_checkpoint','median'),
        p50_volume_usd=('total_volume_usd_to_checkpoint','median'),
        p50_top1=('top1_wallet_pct_buy_usd_to_checkpoint','median')
    )
    g['share_pct'] = g.groupby('checkpoint_min')['n_rows'].transform(lambda s: 100*s/s.sum())
    return g.sort_values(['checkpoint_min','phase'])

def transition_matrix(df):
    ordered = [1,3,5,10,15,20,30]
    nxt = {1:3,3:5,5:10,10:15,15:20,20:30}
    pairs = []
    for mint, sub in df.sort_values(['mint','checkpoint_min']).groupby('mint'):
        ph = dict(zip(sub['checkpoint_min'], sub['phase']))
        for a,b in nxt.items():
            if a in ph and b in ph:
                pairs.append((a,b,ph[a],ph[b]))
    out = pd.DataFrame(pairs, columns=['from_checkpoint','to_checkpoint','from_phase','to_phase'])
    if out.empty:
        return out
    res = out.groupby(['from_checkpoint','to_checkpoint','from_phase','to_phase'], as_index=False).size().rename(columns={'size':'n_tokens'})
    res['pct_within_from_phase'] = res.groupby(['from_checkpoint','to_checkpoint','from_phase'])['n_tokens'].transform(lambda s: 100*s/s.sum())
    return res.sort_values(['from_checkpoint','to_checkpoint','from_phase','to_phase'])

def archetypes(df):
    rows=[]
    for mint, sub in df.sort_values(['mint','checkpoint_min']).groupby('mint'):
        phases=list(sub['phase'])
        cps=list(sub['checkpoint_min'])
        hot_idx=[cp for cp,p in zip(cps,phases) if p=='Hot']
        warm_idx=[cp for cp,p in zip(cps,phases) if p=='Warm']
        cold_idx=[cp for cp,p in zip(cps,phases) if p=='Cold']
        first_hot=min(hot_idx) if hot_idx else np.nan
        max_run=0; cur=0
        for p in phases:
            if p=='Hot': cur+=1; max_run=max(max_run,cur)
            else: cur=0
        if not hot_idx and not warm_idx:
            arch='cold_only'
        elif not hot_idx and warm_idx and cold_idx:
            arch='cold_to_warm_never_hot'
        elif not hot_idx and len(warm_idx)>=4:
            arch='warm_persistent_never_hot'
        elif hot_idx and first_hot <= 5:
            arch='hot_early_persistent' if max_run>=2 else 'hot_early_brief'
        elif hot_idx and first_hot > 5:
            arch='hot_late_persistent' if max_run>=2 else 'hot_late_brief'
        else:
            arch='other'
        rows.append((mint, first_hot, max_run, arch))
    return pd.DataFrame(rows, columns=['mint','first_hot_checkpoint','max_hot_run_len','archetype'])
def main():
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(); args.output.mkdir(parents=True,exist_ok=True)
    df=add_phase(pd.read_csv(args.input))
    df.to_csv(args.output/'02_phase_assignment.csv',index=False)
    phase_summary(df).to_csv(args.output/'03_phase_summary.csv',index=False)
    transition_matrix(df).to_csv(args.output/'04_transition_matrix.csv',index=False)
    archetypes(df).to_csv(args.output/'06_archetypes.csv',index=False)

if __name__=='__main__':main()
