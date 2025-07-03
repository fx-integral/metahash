# ------------------------------------------------------------------------
# tests/test_rewards.py
# ------------------------------------------------------------------------
# Exhaustive unit‑tests for metahash.validator.rewards.compute_epoch_rewards
#
# Scenarios
#   ① flat‑rate, no‑slip, single miner
#   ② flat‑rate, no‑slip, unknown miner is burnt
#   ③ bond‑curve (β>0): diminishing rate across two miners
#   ④ slippage: shallow pool ⇒ award < deposit
#   ⑤ forbidden subnet: all transfers dropped, full bag burnt
#   ⑥ weights normalise to 1 ± ε across miners (UID 0 excluded)
#
# Run with:
#   pytest -v -s
# ------------------------------------------------------------------------

from decimal import Decimal, getcontext
from typing import List, Dict

import pytest

from metahash.config import (
    BAG_SN73,
    FORBIDDEN_ALPHA_SUBNETS,
    K_SLIP,
    SLIP_TOLERANCE,
)
from metahash.validator.rewards import (
    compute_epoch_rewards,
    TransferEvent,
)

# ───── helpers ────────────────────────────────────────────────────────── #


class DummyScanner:
    """Implements the scanner protocol, yielding canned events."""

    def __init__(self, events: List[TransferEvent]):
        self._events = events

    async def scan(self, *_):  # noqa: D401
        return self._events


class DummyPricing:
    """Pricing provider with a constant TAO/α price."""

    def __init__(self, tao: float = 1.0):
        self.tao = tao

    async def __call__(self, *_):
        return type("Price", (), {"tao": self.tao, "rao": None})


def depth_provider(depth: int):
    async def _depth(_):  # noqa: D401
        return depth
    return _depth


class DummyResolver:
    """Cold‑key → UID mapping stub."""

    def __init__(self, mapping: Dict[str, int]):
        self.map = mapping

    async def __call__(self, ck):  # noqa: D401
        return self.map.get(ck)


class DummyValidator:
    """Implements only the get_miner_uids() accessor."""

    def __init__(self, miner_uids):
        self._uids = miner_uids

    def get_miner_uids(self):
        return self._uids


# ───── common constants ───────────────────────────────────────────────── #
BAG = BAG_SN73               # test bag size (from config)
START, END = 1_000, 1_100    # dummy block‑range
CK1, CK2 = "ck1", "ck2"      # cold‑keys
UID1, UID2 = 1, 2
ONE_TAO = 10**18             # 1 TAO in planck

# Decimal math with generous precision
getcontext().prec = 60


# ====================================================================== #
# ① flat‑rate, no‑slip, single miner
# ====================================================================== #
@pytest.mark.asyncio
async def test_no_slip_single_miner(capsys):
    big_depth = 10**24  # slip < tolerance ⇒ no slip
    scanner = DummyScanner([TransferEvent(CK1, 1, ONE_TAO)])

    res = await compute_epoch_rewards(
        validator=DummyValidator([0, UID1]),
        scanner=scanner,
        pricing=DummyPricing(1.0),
        uid_of_coldkey=DummyResolver({CK1: UID1}),
        start_block=START,
        end_block=END,
        bag_sn73=BAG,
        c0=1,
        beta=0,
        r_min=1,
        pool_depth_of=depth_provider(big_depth),
    )

    # Award equals TAO value (β = 0, no slip)
    assert res.rewards_per_miner[UID1] == Decimal("1")
    assert res.weights == [0.0, 1.0]

    captured = capsys.readouterr().out.strip()
    print("LOG:", captured)


# ====================================================================== #
# ② unknown miner is burnt (UID 0)
# ====================================================================== #
@pytest.mark.asyncio
async def test_unknown_miner_burned():
    big_depth = 10**24
    events = [
        TransferEvent(CK1, 1, ONE_TAO),            # known miner
        TransferEvent("mystery‑ck", 1, ONE_TAO),   # unknown miner
    ]

    res = await compute_epoch_rewards(
        validator=DummyValidator([0, UID1]),
        scanner=DummyScanner(events),
        pricing=DummyPricing(1.0),
        uid_of_coldkey=DummyResolver({CK1: UID1}),
        start_block=START,
        end_block=END,
        bag_sn73=BAG,
        c0=1,
        beta=0,
        r_min=1,
        pool_depth_of=depth_provider(big_depth),
    )

    assert res.rewards_per_miner[UID1] == Decimal("1")
    # Everything else (bag – 1) is burnt to UID 0
    assert res.rewards_per_miner[0] == Decimal(BAG) - Decimal("1")
    assert res.weights == [0.0, 1.0]


# ====================================================================== #
# ③ bond‑curve (β>0): diminishing rate for second miner
# ====================================================================== #
@pytest.mark.asyncio
async def test_bond_curve_decreasing_rate():
    """
    Bag = 100 SN‑73
    Deposits: two miners, 10 TAO each
    Parameters: c0=1, beta=0.1, r_min=0.2

    Expected:
        miner‑1 rate = 1.0       ⇒ award = 10
        miner‑2 rate = 1/(1+0.1*10)=0.5 ⇒ award = 5
    """
    depth = 10**24   # slip = 0
    c0, beta, r_min = 1, 0.1, 0.2

    events = [
        TransferEvent(CK1, 1, 10 * ONE_TAO),
        TransferEvent(CK2, 1, 10 * ONE_TAO),
    ]

    res = await compute_epoch_rewards(
        validator=DummyValidator([0, UID1, UID2]),
        scanner=DummyScanner(events),
        pricing=DummyPricing(1.0),
        uid_of_coldkey=DummyResolver({CK1: UID1, CK2: UID2}),
        start_block=START,
        end_block=END,
        bag_sn73=BAG,
        c0=c0,
        beta=beta,
        r_min=r_min,
        pool_depth_of=depth_provider(depth),
    )

    assert res.rewards_per_miner[UID1] == Decimal("10")
    assert res.rewards_per_miner[UID2] == Decimal("5")
    assert res.rewards_per_miner[0] == Decimal("85")

    w1, w2 = res.weights[1], res.weights[2]
    assert pytest.approx(w1, abs=1e-12) == 10 / 15
    assert pytest.approx(w2, abs=1e-12) == 5 / 15


# ====================================================================== #
# ④ slippage reduces award below TAO value
# ====================================================================== #
@pytest.mark.asyncio
async def test_slippage_effect():
    shallow = ONE_TAO  # depth == deposit  ⇒ maximum slip
    res = await compute_epoch_rewards(
        validator=DummyValidator([0, UID1]),
        scanner=DummyScanner([TransferEvent(CK1, 1, ONE_TAO)]),
        pricing=DummyPricing(1.0),
        uid_of_coldkey=DummyResolver({CK1: UID1}),
        start_block=START,
        end_block=END,
        bag_sn73=BAG,
        c0=1,
        beta=0,
        r_min=1,
        pool_depth_of=depth_provider(shallow),
    )

    award = res.rewards_per_miner[UID1]
    assert Decimal("0") < award < Decimal("1")  # slip happened


# ====================================================================== #
# ⑤ forbidden subnet events are ignored and full bag is burnt
# ====================================================================== #
@pytest.mark.asyncio
async def test_forbidden_subnet_dropped():
    if not FORBIDDEN_ALPHA_SUBNETS:
        pytest.skip("No forbidden subnets configured – skip test")

    sid = FORBIDDEN_ALPHA_SUBNETS[0]
    events = [TransferEvent(CK1, sid, ONE_TAO)]

    res = await compute_epoch_rewards(
        validator=DummyValidator([0, UID1]),
        scanner=DummyScanner(events),
        pricing=DummyPricing(1.0),
        uid_of_coldkey=DummyResolver({CK1: UID1}),
        start_block=START,
        end_block=END,
        bag_sn73=BAG,
        c0=1,
        beta=0,
        r_min=1,
        pool_depth_of=depth_provider(10**24),
    )

    # No rewards issued to miners; full bag is burnt
    assert res.rewards_per_miner[0] == Decimal(BAG)
    assert all(w == 0.0 for w in res.weights)


# ====================================================================== #
# ⑥ weight vector normalises to 1 across miners (burn UID excluded)
# ====================================================================== #
@pytest.mark.asyncio
async def test_weights_normalise():
    depth = 10**24
    events = [
        TransferEvent(CK1, 1, ONE_TAO),
        TransferEvent(CK2, 1, ONE_TAO),
    ]

    res = await compute_epoch_rewards(
        validator=DummyValidator([0, UID1, UID2]),
        scanner=DummyScanner(events),
        pricing=DummyPricing(1.0),
        uid_of_coldkey=DummyResolver({CK1: UID1, CK2: UID2}),
        start_block=START,
        end_block=END,
        bag_sn73=BAG,
        c0=1,
        beta=0,
        r_min=1,
        pool_depth_of=depth_provider(depth),
    )

    # Sum of weights across non‑burn UIDs ≈ 1
    total = sum(res.weights[1:])  # skip UID‑0
    assert pytest.approx(total, abs=1e-12) == 1.0
