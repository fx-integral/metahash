from __future__ import annotations
from dataclasses import dataclass
from decimal import getcontext
from math import isclose
from typing import Tuple
from metahash.config import D_START, D_TAIL_TARGET, P_S_PAR, BAG_SN73


# 60-digit precision – comfortably exceeds 18-dec TAO maths
getcontext().prec = 60

# ────────────────────────── validation helpers ───────────────────────── #


def _chk_pos(name: str, val: float | int):
    if val <= 0:
        raise ValueError(f"{name} must be > 0 (got {val})")


def _chk_rng(
    name: str,
    val: float,
    lo: float,
    hi: float,
    *,
    inclusive_hi: bool = False,
):
    """Ensure *val* ∈ (lo, hi] when inclusive_hi else (lo, hi)."""
    ok = lo < val <= hi if inclusive_hi else lo < val < hi
    rng = f"({lo}, {hi}]" if inclusive_hi else f"({lo}, {hi})"
    if not ok:
        raise ValueError(f"{name} must be in {rng} (got {val})")

# ─────────────────────────── core kernel r(m) ────────────────────────── #


def _curve_rate(
    m: float,
    *,
    c0: float,
    beta: float,
    r_min: float,
) -> float:
    if m < 0:
        raise ValueError("m must be ≥ 0")

    if beta * m <= 1.0:
        rate = c0 / (1.0 + beta * m)
    else:
        rate = c0 / (1.0 + beta * m * m)

    # Clamp to the legal range [r_min, 1.0]
    return max(r_min, min(rate, 1.0))


# ───────────────────────── bond-curve dataclass ──────────────────────── #


@dataclass(frozen=True, slots=True)
class BondCurve:
    d_start: float
    d_tail_target: float
    p_s_par: float
    bag_sn73: int
    c0: float = 0.0
    r_min: float = 0.0
    beta: float = 0.0

    def __post_init__(self):
        _chk_rng("d_start", self.d_start, 0.0, 1.0)
        _chk_rng("d_tail_target", self.d_tail_target, 0.0, 1.0)
        _chk_rng("p_s_par", self.p_s_par, 0.0, 1.0, inclusive_hi=True)
        _chk_pos("bag_sn73", self.bag_sn73)

        r0_f = (1.0 - self.d_start) / self.p_s_par
        r_min_f = (1.0 - self.d_tail_target) / self.p_s_par
        beta_f = (r0_f / r_min_f - 1.0) / self.bag_sn73

        object.__setattr__(self, "c0", r0_f)
        object.__setattr__(self, "r_min", r_min_f)
        object.__setattr__(self, "beta", beta_f)

        if not isclose(1.0 - r0_f * self.p_s_par, self.d_start, rel_tol=1e-6):
            raise ValueError("d_start mismatch after derivation")
        if not isclose(1.0 - r_min_f * self.p_s_par, self.d_tail_target, rel_tol=1e-6):
            raise ValueError("d_tail_target mismatch after derivation")
        if self.d_start >= self.d_tail_target:
            raise ValueError("Discount must widen along epoch")

# ───────────────────────── helper formulae ──────────────────────────── #


def beta_from_gamma(
    bag_sn73: int | float,
    d_start: float,
    gamma_target: float,
) -> float:
    _chk_pos("bag_sn73", bag_sn73)
    _chk_rng("d_start", d_start, 0.0, 1.0)
    _chk_pos("gamma_target", gamma_target)

    excess = gamma_target * (1.0 + d_start) - 1.0
    if excess <= 0:              # flat payout already satisfies γ
        return 0.0
    return 2.0 * excess / bag_sn73


def curve_params(
    price: float,
    d_start: float,
    r_min_factor: float,
) -> Tuple[float, float]:
    """
    Return (c0, r_min) for the requested apex discount and tail factor.

    The production code never sets d_start to zero, but the test-suite does,
    so we explicitly allow d_start == 0.0 here.
    """
    if not (0.0 <= d_start < 1.0):
        raise ValueError(f"d_start must be in [0, 1) (got {d_start})")
    _chk_pos("price", price)
    _chk_pos("r_min_factor", r_min_factor)

    c0 = 1.0 / (price * (1.0 + d_start))
    r_min = c0 * r_min_factor
    return float(c0), float(r_min)

# ────────────────── canonical-curve factory for callers ───────────────── #


def get_bond_curve() -> BondCurve:
    """Return a fresh dataclass for the current governance constants."""
    return BondCurve(
        d_start=D_START,
        d_tail_target=D_TAIL_TARGET,
        p_s_par=P_S_PAR,
        bag_sn73=BAG_SN73,
    )


# ─── remainder of the module (discount_for_deposit, etc.) unchanged ──── #
from decimal import Decimal          # only needed below this comment

DECIMALS: Decimal = Decimal(10) ** 18


def discount_for_deposit(
    *,
    tao_value: float,
    bag_sn73: int,
    beta: float,
    m_eaten: float | None = None,
    m_eaten_prev: float | None = None,
    pool_price_tao: float,
    d_start: float | None = None,
    r_min_factor: float | None = None,
    c0: float | None = None,
    r_min: float | None = None,
) -> float:
    """
    Approximate discount % (0 =no discount, 1 = 100 % lost) that would apply
    right now for `tao_value` deposited.
    """
    if tao_value <= 0:
        return 1.0

    m_now = m_eaten_prev if m_eaten_prev is not None else (m_eaten or 0.0)

    # derive curve parameters if not provided
    if c0 is None or r_min is None:
        d_start = 0.0 if d_start is None else d_start
        r_min_factor = 0.0 if r_min_factor is None else r_min_factor
        c0, r_min = curve_params(pool_price_tao, d_start, r_min_factor)

    # instantaneous payout rate r(m)
    r = _curve_rate(m_now, c0=float(c0), beta=beta, r_min=float(r_min))

    # convert to discount
    disc_base = 1.0 - r * pool_price_tao       # hyperbolic discount
    disc_occ = round(m_now / bag_sn73, 1)     # occupancy heuristic
    disc = max(disc_base, disc_occ)

    return min(1.0, max(0.0, disc))            # guard-rails


from decimal import Decimal, getcontext

getcontext().prec = 60     # keep full sub-planck accuracy

# copied from config / utils so we don’t import heavy deps here
PLANCK_D = Decimal(10) ** 9
K_SLIP = Decimal("0.003")
SLIP_TOL = Decimal("0.00001")


def quote_alpha_cost(
    alpha_raw: int,
    *,
    price_tao: Decimal,
    depth_rao: int,
    c0: Decimal,              # baseline curve (D_START)
    k_slip: Decimal = K_SLIP,
) -> tuple[Decimal, Decimal, Decimal]:
    """
    Return (tao_post, tao_pre, discount).
    • tao_pre  – α × price
    • tao_post – tao_pre after 1-c0 baseline discount *and* dynamic slip
    • discount – 1 − tao_post / tao_pre     (0.10 == 10 %)
    """
    if depth_rao <= 0 or alpha_raw <= 0:
        return Decimal(0), Decimal(0), Decimal(0)

    # dynamic slip (same form the validator uses)
    ratio = Decimal(alpha_raw) / (Decimal(depth_rao) + Decimal(alpha_raw))
    slip = k_slip * ratio
    if slip <= SLIP_TOL:
        slip = Decimal(0)
    slip = min(slip, Decimal(1))

    tao_pre = (Decimal(alpha_raw) / PLANCK_D) * price_tao
    tao_post = tao_pre * (Decimal(1) - c0) * (Decimal(1) - slip)
    discount = (Decimal(1) - tao_post / tao_pre) if tao_pre else Decimal(0)
    return tao_post, tao_pre, discount


# ───── public re-exports ───── #
__all__ = [
    "_curve_rate",
    "beta_from_gamma",
    "curve_params",
    "discount_for_deposit",
    "BondCurve",
    "get_bond_curve",
]
