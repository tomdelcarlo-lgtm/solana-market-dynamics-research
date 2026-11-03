"""Render the source-component map without optional plotting dependencies."""
from pathlib import Path
from src.visuals import SVG


def render():
    f=SVG('A research system, with its real implementation retained',
          'Original algorithms plus documented packaging/safety changes. Public execution is paper or dry-run only.',760,
          'published source components')
    cards=[('collector/','Actual binary decoder, program-log subscription and migration/pool collection. Offline synthetic replay by default.'),
           ('market_state/','Original bounded dedup, observed token histories, tape/decision writers, atomic positions and normalized SQL DDL.'),
           ('signals/','Original feature formulas, buyer-trigger anchor, final track snapshot, ranking/entry/exit runtime and ML cell bodies.'),
           ('trading/','Original intent/risk/journal/state/adapter. Native construction retained for inspection; signing and sends blocked.'),
           ('monitor/','Original Streamlit dashboard and market-regime diagnostics; standard-library offline monitor over demo outputs.'),
           ('research/ · src/','Article, state/trajectory hypotheses, evidence-qualified chapters, public aggregates and deterministic research figures.')]
    for i,(title,body) in enumerate(cards):
        x=42+(i%3)*370;y=155+(i//3)*240
        f.card(x,y,350,204,title,body)
        if i%3<2:f.line(x+350,y+100,x+367,y+100,arrow=True)
    return f.finish('No private data/configuration is included. Historical source is not a validated deployment; read docs/source_integration.md for changes.')


if __name__=='__main__':
    (Path(__file__).resolve().parents[1]/'figures/system_components.svg').write_text(render())
    print('Wrote figures/system_components.svg')
