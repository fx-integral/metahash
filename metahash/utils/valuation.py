# metahash/validator/valuation.py
from __future__ import annotations

from decimal import Decimal, getcontext
from metahash.config import K_SLIP, SLIP_TOLERANCE

# high precision to match rewards.py expectations
getcontext().prec = 60

DEC_RAO = Decimal(10) ** 9       # 1 α = 1e9 planck (rao)
MICRO = Decimal(10) ** 6         # μTAO scaler
K_SLIP_D = Decimal(str(K_SLIP))
SLIP_TOLERANCE_D = Decimal(str(SLIP_TOLERANCE))


def post_slip_tao(alpha_rao: int, price_tao_per_alpha: float, depth_rao: int) -> float:
    """TAO value after slippage for a given α (rao), price, and pool depth."""
    if alpha_rao <= 0 or depth_rao <= 0 or price_tao_per_alpha <= 0:
        return 0.0
    a = Decimal(alpha_rao)
    d = Decimal(depth_rao)
    price = Decimal(str(price_tao_per_alpha))
    ratio = a / (d + a)
    slip = K_SLIP_D * ratio
    if slip <= SLIP_TOLERANCE_D:
        slip = Decimal(0)
    if slip > 1:
        slip = Decimal(1)
    return float(a * price * (Decimal(1) - slip) / DEC_RAO)


def effective_value_tao(alpha_rao: int, weight_bps: int, discount_bps: int,
                        price_tao_per_alpha: float, depth_rao: int) -> float:
    """Effective valuation used by clearing & settlement: TAO_post_slip × weight × (1−discount)."""
    base = post_slip_tao(alpha_rao, price_tao_per_alpha, depth_rao)
    w = max(0, min(10_000, int(weight_bps))) / 10_000.0
    disc = 1.0 - (max(0, min(10_000, int(discount_bps))) / 10_000.0)
    return base * w * disc


def encode_value_mu(value_tao: float) -> int:
    """Compact integer encoding of TAO value in μTAO (micro-TAO)."""
    if value_tao <= 0:
        return 0
    return int((Decimal(str(value_tao)) * MICRO).to_integral_value())


def decode_value_mu(value_mu: int) -> float:
    """Inverse of encode_value_mu."""
    if not isinstance(value_mu, int) or value_mu <= 0:
        return 0.0
    return float(Decimal(value_mu) / MICRO)
