"""Offline implementation of the historical four-axis checkpoint definition.

This module contains no event connection, trading, inference model or wallet code.
Missing fields preserve the historical comparison semantics and are flagged.
"""
import math
from typing import Mapping

FIELDS = ('buyers', 'buy_sell_ratio', 'top1_buy_share', 'volume_usd',
          'trade_count', 'average_trade_usd')


def clip(value: float) -> float:
    """Clip to [0,1], preserving NaN so comparisons remain false."""
    return value if math.isnan(value) else min(1.0, max(0.0, value))


def _number(value) -> float:
    if value is None or value == '':
        return float('nan')
    number = float(value)
    if math.isinf(number):
        raise ValueError('Infinite inputs are not valid checkpoint observations')
    return number


def coordinates(row: Mapping) -> dict:
    """Normalize checkpoint fields. Ratios are not volume shares.

    Missing inputs propagate as NaN. Finite negatives and share >1 are rejected
    by this public input wrapper; no epsilon is added to the historical rules.
    """
    u, r, c, v, n, a = (_number(row.get(key)) for key in FIELDS)
    if any(x < 0 for x in (u, r, c, v, n, a)) or c > 1:
        raise ValueError('Counts/amounts/ratio must be nonnegative; share <=1')
    return {
        'B': clip((u - 20.0) / 80.0),
        'P': clip(r - 1.0),
        'S': 1.0 - clip((c - 0.20) / 0.30),
        'A': (0.5 * clip((v - 1000.0) / 9000.0)
              + 0.3 * clip((n - 20.0) / 180.0)
              + 0.2 * clip((a - 20.0) / 100.0)),
    }


def classify_state(state: Mapping) -> str:
    """Historical hard boxes; Warm is the residual, not a missing-data cure."""
    b, p, s, a = (state[key] for key in ('B', 'P', 'S', 'A'))
    if b >= .60 and p >= .35 and s >= .40 and a >= .35:
        return 'Hot'
    if b <= .20 and p <= .20 and a <= .25:
        return 'Cold'
    return 'Warm'


def classify_checkpoint(row: Mapping) -> dict:
    state = coordinates(row)
    # Missing S does not prevent Cold under the original predicate.
    return {'phase': classify_state(state), 'missing_input': any(
        math.isnan(_number(row.get(key))) for key in FIELDS),
        'coordinates': {k: None if math.isnan(v) else v for k, v in state.items()}}


def band_membership(value: float, a: float, b: float, c: float, d: float) -> float:
    """Five-axis PROPOSAL's trapezoid; not used by checkpoint classification."""
    if not a < b <= c < d or not math.isfinite(value):
        raise ValueError('Require finite value and a < b <= c < d')
    if value <= a or value >= d:
        return 0.0
    if value < b:
        return (value - a) / (b - a)
    if value <= c:
        return 1.0
    return (d - value) / (d - c)
