#!/usr/bin/env python3
# ====================================================================== #
# metahash/utils/epoch_metrics.py                                        #
# ====================================================================== #
"""
Light‑weight helpers for *“How much has the auction moved so far?”*

Changes vs. the original version
--------------------------------
1.  **Full‑precision math** – all accumulators are kept as ``Decimal``.
2.  **No premature float casts** – conversion happens only in the UI
    layer (dashboard or JSON serialiser).
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Dict, Iterable, Optional, Set, TypedDict

from metahash.validator.rewards import AlphaDeposit, DECIMALS


# ──────────────────────── internal helpers ──────────────────────────── #

def _d(x) -> Decimal:
    """`None` → 0;  int/float/Decimal → Decimal."""
    return Decimal(0) if x is None else Decimal(str(x))


class _Metrics(TypedDict):
    sn73: Decimal
    alpha: Decimal
    tao_value: Decimal
    tao_post_slip: Decimal


def _blank() -> _Metrics:
    z = Decimal(0)
    return {"sn73": z, "alpha": z, "tao_value": z, "tao_post_slip": z}


def _add(
    rec: _Metrics,
    *,
    sn73: Decimal = Decimal(0),
    alpha: Decimal = Decimal(0),
    tao: Decimal = Decimal(0),
    post: Decimal = Decimal(0),
) -> None:
    rec["sn73"] += sn73
    rec["alpha"] += alpha
    rec["tao_value"] += tao
    rec["tao_post_slip"] += post


# ─────────────────────────── public API ─────────────────────────────── #

def summarize_sn73_rewards(
    deposits: Iterable[AlphaDeposit],
) -> Dict[str, Dict[int, _Metrics] | _Metrics]:
    """Aggregate **rewards already distributed** this epoch."""
    per_subnet: Dict[int, _Metrics] = defaultdict(_blank)
    aggregate: _Metrics = _blank()

    for d in deposits:
        if not d.sn73_awarded:
            continue

        subnet = d.subnet_id
        _add(
            per_subnet[subnet],
            sn73=_d(d.sn73_awarded),
            alpha=_d(Decimal(d.alpha_raw) / DECIMALS),
            tao=_d(d.tao_value),
            post=_d(d.tao_value_post_slip),
        )
        _add(
            aggregate,
            sn73=_d(d.sn73_awarded),
            alpha=_d(Decimal(d.alpha_raw) / DECIMALS),
            tao=_d(d.tao_value),
            post=_d(d.tao_value_post_slip),
        )

    return {"per_subnet": dict(per_subnet), "aggregate": aggregate}


def summarize_alpha_deposits(
    deposits: Iterable[AlphaDeposit],
    *,
    exclude_coldkeys: Optional[Set[str]] = None,
) -> Dict[str, Dict[int, _Metrics] | _Metrics]:
    """Aggregate **α deposited so far** this epoch."""
    per_subnet: Dict[int, _Metrics] = defaultdict(_blank)
    aggregate: _Metrics = _blank()
    exclude_coldkeys = exclude_coldkeys or set()

    for d in deposits:
        if d.coldkey in exclude_coldkeys:
            continue

        subnet = d.subnet_id
        _add(
            per_subnet[subnet],
            alpha=_d(Decimal(d.alpha_raw) / DECIMALS),
            tao=_d(d.tao_value),
            post=_d(d.tao_value_post_slip),
        )
        _add(
            aggregate,
            alpha=_d(Decimal(d.alpha_raw) / DECIMALS),
            tao=_d(d.tao_value),
            post=_d(d.tao_value_post_slip),
        )

    return {"per_subnet": dict(per_subnet), "aggregate": aggregate}
