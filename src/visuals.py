"""Deterministic accessible SVG figures, using only Python's standard library.

Historical counts, specified parameters and conceptual diagrams have different
labels. No simulated market trajectory or invented observation is plotted.
"""
from html import escape
import textwrap

INK='#152b3c'; MUTED='#526777'; TEAL='#087f8c'; BLUE='#436ca3'
WARM='#b3811b'; HOT='#b74e4c'; GRID='#dbe4e9'; PALE='#f4f8fa'
COLORS={'Cold':BLUE,'Warm':WARM,'Hot':HOT}


class SVG:
    def __init__(self, title, subtitle, height, kind):
        self.height=height
        self.parts=[f'<svg xmlns="http://www.w3.org/2000/svg" width="1180" height="{height}" viewBox="0 0 1180 {height}" role="img" aria-labelledby="title description">',
                    f'<title id="title">{escape(title)}</title>',f'<desc id="description">{escape(subtitle)} {escape(kind)}</desc>',
                    '<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8" fill="#526777"/></marker></defs>',
                    f'<rect width="1180" height="{height}" fill="white"/>']
        self.text(42,42,kind.upper(),12,TEAL,weight=700)
        self.text(42,85,title,29,weight=700)
        self.text(42,116,subtitle,16,MUTED)

    def text(self,x,y,s,size=17,color=INK,anchor='start',weight=400):
        self.parts.append(f'<text x="{x}" y="{y}" font-family="Arial, Helvetica, sans-serif" font-size="{size}" fill="{color}" text-anchor="{anchor}" font-weight="{weight}">{escape(str(s))}</text>')

    def lines(self,x,y,text,width=45,size=17,color=MUTED):
        for i,line in enumerate(textwrap.wrap(text,width=width)):
            self.text(x,y+i*(size+7),line,size,color)

    def rect(self,x,y,w,h,fill=PALE,stroke=GRID,dashed=False):
        dash=' stroke-dasharray="7 5"' if dashed else ''
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="{fill}" stroke="{stroke}"{dash}/>')

    def line(self,x1,y1,x2,y2,color=GRID,arrow=False,dashed=False):
        extra=(' marker-end="url(#arrow)"' if arrow else '')+(' stroke-dasharray="7 5"' if dashed else '')
        self.parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" stroke-width="2"{extra}/>')

    def circle(self,x,y,r,color):
        self.parts.append(f'<circle cx="{x}" cy="{y}" r="{r}" fill="{color}"/>')

    def card(self,x,y,w,h,title,body,dashed=False,color=TEAL):
        self.rect(x,y,w,h,dashed=dashed)
        self.text(x+18,y+30,title,19,color,weight=700)
        self.lines(x+18,y+61,body,width=int((w-36)/8.4),size=16)

    def finish(self,footer):
        self.line(42,self.height-61,1138,self.height-61)
        self.lines(42,self.height-36,footer,width=130,size=13)
        return '\n'.join(self.parts+['</svg>'])+'\n'


def timeline_figure(timeline):
    f=SVG('A research program that changed its unit of analysis',
          'Milestones follow evidence dates; spacing is categorical, not proportional to elapsed time.',1000,'documented history')
    for i,item in enumerate(timeline):
        y=150+i*112
        f.text(42,y+30,item['date'],16,TEAL,weight=700)
        f.circle(237,y+23,6,TEAL)
        if i<len(timeline)-1:f.line(237,y+30,237,y+130)
        f.card(265,y,873,93,item['title'],item['detail'])
    return f.finish('Source: published reconstruction chronology. Initial notebook start is undated; 2025 labels are cohort dates, not research dates.')


def ml_figure():
    f=SVG('From a token label to a research decision',
          'Historical ML workflow; the final panel states the evaluation boundary.',660,'conceptual pipeline grounded in source')
    cards=[('Cohort & label','32,000 January rows; 583 graduation-proxy positives. Eligibility and label horizons matter.'),
           ('Feature matrix','23 fields in the main notebook: 30m activity plus engineered rates, ratios and logs.'),
           ('Random outer split','Stratified 80/20, seed 1. Test labels were reused for selection; no untouched temporal verdict.'),
           ('Fit & imbalance','Logistic Regression, ensembles, XGBoost. Class weights and executed training-set SMOTE.'),
           ('Explain & evaluate','Executed SHAP and average precision; threshold sweeps and isotonic calibration.'),
           ('Interpret carefully','Graduation classification differs from future price, executable return and portfolio PnL.')]
    for i,(title,body) in enumerate(cards):
        x=42+(i%3)*370;y=150+(i//3)*215
        f.card(x,y,350,178,title,body)
        if i%3<2:f.line(x+350,y+87,x+367,y+87,MUTED,True)
    f.line(957,328,957,347,MUTED)
    f.line(957,347,217,347,MUTED)
    f.line(217,347,217,362,MUTED,True)
    return f.finish('Saved model scores are historical exploratory outputs. This repository does not refit original estimators.')


def geometry_figure():
    f=SVG('Two phase-space definitions, two evidence boundaries',
          'The checkpoint algorithm and the five-axis proposal must be interpreted separately.',630,'conceptual mathematical representation')
    f.card(42,150,530,270,'Implemented checkpoint space',
           'x = (B, P, S, A). Clipped breadth, ratio-based pressure, inverse buy concentration, arithmetic activity. Hard boxes assign Cold / Warm / Hot. Historical reproduction: 2,100 classifications across 300 tokens.')
    f.card(608,150,530,270,'Five-axis research proposal',
           'x = (B, P, S, A, O). Adds operability as a preferred market-cap band. Soft multiplicative memberships and alternative boundaries were proposed. Calibration pending; not the checkpoint algorithm.',True)
    f.rect(42,450,1096,89)
    f.text(62,483,'Trajectory idea: x(t) → x(t+1) → x(t+2)',21,TEAL,weight=700)
    f.text(62,514,'A sequence of states is not evidence of a chaotic attractor or a validated trading signal.',17)
    return f.finish('Conceptual diagram; no token trajectory is invented. No deterministic chaos was established.')


def classification_figure(a):
    f=SVG('Cold / Warm / Hot: rules and retained checkpoint counts',
          'Four-axis implementation; 2,100 observations across 300 tokens and seven checkpoints.',690,'historical aggregates + exact rule')
    counts=a['phase_snapshots']['phase_counts']
    details={'Cold':'B ≤ 0.20, P ≤ 0.20, A ≤ 0.25. Structure S is not part of this predicate.',
             'Warm':'Residual when neither hard predicate passes. Missing fields can fall here; flagged in public examples.',
             'Hot':'B ≥ 0.60, P ≥ 0.35, S ≥ 0.40, A ≥ 0.35. Alignment is required.'}
    for i,p in enumerate(['Cold','Warm','Hot']):
        x=42+i*370
        f.card(x,150,350,200,p,details[p],color=COLORS[p])
        f.text(x+20,405,f"{counts[p]:,} observations",28,COLORS[p],weight=700)
        f.rect(x,437,350,28)
        f.rect(x,437,350*counts[p]/2100,28,COLORS[p],COLORS[p])
    f.text(42,515,'Checkpoints: 1, 3, 5, 10, 15, 20, 30 minutes',21,TEAL,weight=700)
    f.text(42,551,'Unequal time gaps; counts are repeated observations, not independent tokens.',17)
    return f.finish('Source: verified aggregate fixture. Historical labels were checked privately; public synthetic cases demonstrate the algorithm.')


def bands_figure(policies):
    f=SVG('Bands were research hypotheses, not fixed market laws',
          'Selected documented policy specifications; active paper lanes are distinct from funded execution.',770,'documented parameter evolution')
    xs=[42,190,400,592,760,940]
    for x,s in zip(xs,['Version / date','Low mcap ($)','Low pressure','Core pressure','Core paper $','Low paper $']):f.text(x,162,s,15,TEAL,weight=700)
    for i,r in enumerate(policies):
        y=190+i*57
        f.rect(42,y,1096,50,'#f4f8fa' if i%2==0 else 'white',stroke='white')
        vals=[r['version']+' · '+r['date'][5:],r['low_mcap'],r['low_pressure'],r['core_pressure'],r['core_paper_stake'] or '—',r['low_paper_stake'] or '—']
        for x,s in zip(xs,vals):f.text(x+5,y+31,s,16)
    f.text(42,650,'Allocation reversal: $350/$250 → $400/$150 → $350/$250 (core / low paper stakes)',19,TEAL,weight=700)
    return f.finish('Source: dated policy specifications. A changed boundary is evidence of iteration, not proof of improved out-of-sample returns.')


def collection_figure():
    f=SVG('Observation infrastructure followed the hypotheses',
          'Retained CSV path, analytical imports and incomplete normalized ingestion are distinct.',820,'implemented architecture + explicit incomplete branch')
    f.card(42,150,340,130,'Helius program logs','Observed trade events; event time differs from local arrival time.')
    f.card(420,150,340,130,'Parse & deduplicate','Binary event decoding; bounded session signature / content caches.')
    f.card(798,150,340,130,'Per-token state','First-observed anchor, participation, flow, reserves and features.')
    f.line(382,215,416,215,MUTED,True);f.line(760,215,794,215,MUTED,True)
    f.card(42,340,340,150,'Persistent event tape','Append-only CSV path, cumulative flow and state observations. Retained collection evidence.')
    f.card(420,340,340,150,'Decision observability','Paper decisions, track gates, skips and outcomes. Different grain from market events.')
    f.card(798,340,340,150,'Normalized DuckDB','Schema and ingest scaffold. Inspected raw / snapshot / inference tables were empty.',True)
    f.line(970,280,970,310,MUTED);f.line(210,310,970,310,MUTED)
    f.line(210,310,210,337,MUTED,True);f.line(590,310,590,337,MUTED,True)
    f.line(970,310,970,337,MUTED,True,True)
    f.card(42,550,530,150,'Research reconstruction','Event horizons and state paths; Dune exports imported into a separate analytical warehouse.')
    f.card(608,550,530,150,'Collection after the pause','Actual migration instructions → pool lookup / backoff → PumpSwap reserve snapshots.')
    f.line(210,490,210,547,MUTED,True);f.line(590,490,590,520,MUTED);f.line(590,520,460,520,MUTED);f.line(460,520,460,547,MUTED,True)
    return f.finish('Dashed branch: implemented scaffold without populated ingestion evidence. No gap-free capture, backfill or production SLA is claimed.')


def execution_figure():
    f=SVG('Execution required its own state and evidence',
          'Historical implementation retained; public live signing and submission are disabled.',710,'implemented historical pipeline')
    items=[('Signal / track','Buyer trigger, pressure, market cap, time and policy state.'),
           ('Intent / risk','Quote metadata, sizing, exposure, wallet feasibility and guards.'),
           ('Construct / sign','Historical native solders path, account reads and compute settings.'),
           ('Submit','RPC broadcast; optional Jito experiment and fallback in historical versions.'),
           ('Confirm / account','Local polling, transaction metadata, position journal and USD proxy.'),
           ('Exit / recovery','Exit trigger, SELL 6003 rebuild/retry, failed exits and orphan recovery.')]
    for i,(title,body) in enumerate(items):
        x=42+(i%3)*370;y=150+(i//3)*220
        f.card(x,y,350,180,title,body)
        if i%3<2:f.line(x+350,y+85,x+367,y+85,MUTED,True)
    f.line(957,330,957,350,MUTED)
    f.line(957,350,217,350,MUTED)
    f.line(217,350,217,367,MUTED,True)
    f.text(42,594,'Observation → decision → send → inclusion → confirmation are separate clock boundaries.',18,TEAL,weight=700)
    return f.finish('Historical paths evolved over time. The public release supports offline dry-run; wallet access, signing and submission are disabled.')


def outcomes_figure(a):
    f=SVG('Paper and live summarize different experiments',
          'Separate populations, periods, policies and stakes: no causal execution-tax subtraction.',660,'reported aggregates; non-matched samples')
    panels=[(42,'Paper exit rows',a['paper'],3360,'Apr 7 – May 19, 2026','Nominal observed-price simulation'),
            (608,'Completed live pairs',a['live_log_pair_summary']['return'],188,'Apr 17 – Jun 11, 2026','Logged realized-size accounting')]
    for x,title,r,n,period,mode in panels:
        f.rect(x,150,530,350)
        f.text(x+22,186,title,23,TEAL,weight=700)
        f.text(x+22,220,f'n = {n:,} · {period}',17)
        f.text(x+22,252,mode,16,MUTED)
        mean=r.get('mean_return',r.get('mean'));median=r.get('median_return',r.get('p50'))
        f.text(x+22,310,f'Mean return: {mean*100:+.4f}%',26,weight=700)
        f.text(x+22,351,f'Median return: {median*100:+.4f}%',22)
        if n==3360:
            f.lines(x+22,397,'One repeated ID: 3,359 distinct exit IDs. No finite-order or complete cost model.',width=55,size=16)
        else:
            f.lines(x+22,397,'Proceeds minus cost: −$166.90 under current convention. Not a fee-complete wallet reconciliation.',width=55,size=16)
    f.text(42,551,'The historical 173–174-trade headline remains unreconciled; it is not silently substituted.',17)
    return f.finish('Source: verified aggregate fixture. These are local-log summaries, not an independently released trade ledger.')


def latency_figure(a):
    f=SVG('BUY timing diagnostics: central behavior and long tail',
          '39 selected matched diagnostics · June 1–11, 2026 · milliseconds converted to seconds.',670,'reported measured quantiles')
    start=415;scale=690/35
    for tick in [0,5,10,15,20,25,30,35]:
        x=start+tick*scale;f.line(x,180,x,502);f.text(x,529,str(tick),14,MUTED,'middle')
    rows=[('signal_to_build_ms','Signal → build'),('build_to_send_ms','Build → send'),('send_to_confirm_ms','Send → local confirmation'),('signal_to_fill_ms','Signal → local confirmation')]
    for i,(key,label) in enumerate(rows):
        y=211+i*85;r=a['latency_ms'][key]
        f.text(42,y,label,17)
        f.line(start+r['p50']/1000*scale,y-5,start+r['p99']/1000*scale,y-5,TEAL)
        for k,c in [('p50',TEAL),('p90',WARM),('p99',HOT)]:f.circle(start+r[k]/1000*scale,y-5,6,c)
        f.text(42,y+24,f"p50 {r['p50']/1000:.3f}s · p90 {r['p90']/1000:.3f}s · p99 {r['p99']/1000:.3f}s",13,MUTED)
    for x,key,color in [(49,'p50',TEAL),(145,'p90',WARM),(241,'p99',HOT)]:
        f.circle(x,565,6,color);f.text(x+14,570,key,16)
    f.text(350,570,'Total maximum: 33.214s; stage quantiles do not add.',16,MUTED)
    return f.finish('Reported quantiles, not reconstructed raw timing rows. Local confirmation is not inclusion or complete end-to-end market latency.')


def decision_figure():
    f=SVG('A bounded decision, with research options preserved',
          'Opportunity-cost interpretation; no estimated success probability or invented monetary forecast.',650,'conceptual decision framework')
    f.card(42,150,340,200,'Evidence available','Negative logged live outcomes; changing hypotheses; unresolved exit mechanics and validation.')
    f.card(420,150,340,200,'Cost of another iteration','Engineering time, validation work, infrastructure cost, capital risk and displaced projects.')
    f.card(798,150,340,200,'Decision on June 11','Pause pre-migration trading exposure; prevent automatic rearming; retain collection and a narrower option.')
    f.line(382,249,416,249,MUTED,True);f.line(760,249,794,249,MUTED,True)
    f.rect(42,399,1096,130)
    f.text(62,437,'Value of continuing = incremental benefit + information value − all incremental costs',20,TEAL,weight=700)
    f.text(62,476,'A decision framework, not an estimated equation. The later post-migration checkpoint outcome is unknown.',17)
    return f.finish('Documented action and retrospective interpretation are distinguished. More possible experiments do not imply unlimited research value.')


def association_figure(a):
    f=SVG('State labels separated graduation outcomes descriptively',
          'A materialized warehouse layer; January and February are asymmetric retained cohorts.',640,'verified aggregate association')
    for col,(month,title) in enumerate([('2025-01','January · curated analytical cohort'),('2025-02','February · broad retained export')]):
        x0=85+col*560
        f.text(x0,170,title,19,TEAL,weight=700)
        for tick in [0,10,20,30]:
            y=475-tick*8;f.line(x0,y,x0+460,y)
            f.text(x0-12,y+5,str(tick)+'%',13,MUTED,'end')
        for i,p in enumerate(['Cold','Warm','Hot']):
            r=next(r for r in a['warehouse_phase_rates'] if r['month']==month and r['phase']==p)
            x=x0+50+i*145;h=r['rate']*100*8
            f.rect(x,475-h,90,h,COLORS[p],COLORS[p])
            f.text(x+45,475-h-35,f"{r['rate']*100:.2f}%",17,INK,'middle',700)
            f.text(x+45,475-h-13,f"n={r['n']:,}",12,MUTED,'middle')
            f.text(x+45,507,p,17,COLORS[p],'middle')
    return f.finish('Positives / phase count. Association does not establish causal regimes, incremental predictive value, executable returns or chaos.')


def render_all(aggregate, fixtures, policies, timeline):
    return {
        'research_timeline.svg':timeline_figure(timeline),
        'ml_pipeline.svg':ml_figure(),
        'phase_space_geometry.svg':geometry_figure(),
        'checkpoint_classification.svg':classification_figure(aggregate),
        'threshold_evolution.svg':bands_figure(policies),
        'data_collection_architecture.svg':collection_figure(),
        'execution_pipeline.svg':execution_figure(),
        'paper_live_outcomes.svg':outcomes_figure(aggregate),
        'latency_diagnostics.svg':latency_figure(aggregate),
        'research_decision.svg':decision_figure(),
        'phase_outcome_association.svg':association_figure(aggregate),
    }
