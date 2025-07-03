# ----------------------------------------------------------------------
# tests/test_rewards_integration.py
# ----------------------------------------------------------------------
"""
Integration scenarios for compute_epoch_rewards against a live
Subtensor node.  Only the transfer scanner is mocked – pricing and depth
providers use the real on‑chain oracles (average_price / average_depth).

Invoke with e.g.:

    pytest -m integration \
           --network ws://<NODE‑URL> \
           --netuid 1 -s
"""
from __future__ import annotations

import os
import random
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Dict

import pytest
import pytest_asyncio
import bittensor as bt

from metahash.config import (
    BAG_SN73,
    P_S_PAR,
    D_START,
    D_TAIL_TARGET,
    GAMMA_TARGET,
)
from metahash.utils.bond_utils import beta_from_gamma, curve_params
from metahash.utils.subnet_utils import average_price, average_depth
from metahash.validator.rewards import (
    compute_epoch_rewards,
    TransferEvent,
)

# ────────────────────────────────
# Simple test‑doubles
# ────────────────────────────────


class _DummyScanner:
    """Returns the canned events supplied at construction."""

    def __init__(self, events: List[TransferEvent]):
        self._events = events

    async def scan(self, *_):   # noqa: D401
        return self._events


class _SimpleValidator:
    """Minimal validator interface for the pipeline."""

    def __init__(self, miner_uids):
        self._uids = miner_uids

    def get_miner_uids(self):
        return self._uids


# ────────────────────────────────
# Shared fixtures
# ────────────────────────────────
@pytest_asyncio.fixture(scope="module")
async def live_chain(request):
    """
    Yields (AsyncSubtensor, metagraph, window_start, window_end).
    """
    net = request.config.getoption("--network") or os.getenv("TAO_NETWORK")
    if not net:
        pytest.skip("Specify --network on CLI or set TAO_NETWORK env var")

    netuid: int = int(request.config.getoption("--netuid"))

    st = bt.AsyncSubtensor(network=net)
    await st.initialize()
    meta = bt.Subtensor(network=net).metagraph(netuid=netuid)

    if len(meta.uids) == 0:
        await st.__aexit__(None, None, None)
        pytest.skip("Metagraph is empty – nothing to test")

    # Pick a 200‑block window inside the previous epoch
    head = await st.get_current_block()
    tempo = getattr(meta, "tempo", 100)
    epoch_start = head - (head % tempo)
    window_start = max(epoch_start - 200, 0)
    window_end = epoch_start - 1

    print(
        f"[shared] window {window_start}–{window_end} "
        f"({datetime.now(timezone.utc).isoformat(timespec='seconds')})"
    )

    yield st, meta, window_start, window_end
    await st.__aexit__(None, None, None)


@pytest_asyncio.fixture(scope="module")
async def bond_curve_params():
    """Mirror the production β, c₀, rₘᵢₙ calculation."""
    beta = beta_from_gamma(P_S_PAR, D_START, GAMMA_TARGET)
    c0, r_min = curve_params(
        P_S_PAR,
        D_START,
        (1 - D_START) / (1 - D_TAIL_TARGET),
    )
    return c0, beta, r_min


# ────────────────────────────────
# Provider helpers (production logic)
# ────────────────────────────────
def make_providers(st, start_block, end_block):
    async def pricing_provider(subnet_id: int, *_):
        return await average_price(
            subnet_id,
            start_block=start_block,
            end_block=end_block,
            st=st,
        )

    async def pool_depth_of(subnet_id: int):
        return await average_depth(
            subnet_id,
            start_block=start_block,
            end_block=end_block,
            st=st,
        )

    return pricing_provider, pool_depth_of


def make_uid_resolver(mapping: Dict[str, int]):
    async def _resolver(coldkey):   # noqa: D401
        return mapping.get(coldkey)
    return _resolver


# ======================================================================
# 1️⃣  Bond‑curve: first‑mover advantage
# ======================================================================
@pytest.mark.asyncio
@pytest.mark.integration
async def test_bond_curve_ordering(live_chain, bond_curve_params):
    st, meta, start_block, end_block = live_chain
    c0, beta, r_min = bond_curve_params

    if len(meta.uids) < 2:
        pytest.skip("Need ≥2 miners for bond‑curve test")

    (ck1, uid1), (ck2, uid2) = list(zip(meta.coldkeys, meta.uids))[:2]

    pricing, depth = make_providers(st, start_block, end_block)
    resolver = make_uid_resolver({ck1: uid1, ck2: uid2})

    ONE_TAO = 10**18
    events = [
        TransferEvent(ck1, meta.netuid, ONE_TAO),  # first in list
        TransferEvent(ck2, meta.netuid, ONE_TAO),  # second
    ]

    rewards = await compute_epoch_rewards(
        validator=_SimpleValidator(list(meta.uids)),
        scanner=_DummyScanner(events),
        pricing=pricing,
        uid_of_coldkey=resolver,
        start_block=start_block,
        end_block=end_block,
        bag_sn73=BAG_SN73,
        c0=c0,
        beta=beta,
        r_min=r_min,
        pool_depth_of=depth,
    )

    assert rewards.rewards_per_miner[uid1] > rewards.rewards_per_miner[uid2] > 0
    print(
        f"Bond‑curve OK – UID {uid1} got {rewards.rewards_per_miner[uid1]:.6f}, "
        f"UID {uid2} got {rewards.rewards_per_miner[uid2]:.6f}"
    )


# ======================================================================
# 2️⃣  Slippage: shallow pool ⇒ award < TAO value
# ======================================================================
@pytest.mark.asyncio
@pytest.mark.integration
async def test_slippage_kicks_in(live_chain, bond_curve_params):
    st, meta, start_block, end_block = live_chain
    c0, beta, r_min = bond_curve_params

    ck, uid = meta.coldkeys[0], int(meta.uids[0])

    pricing, depth = make_providers(st, start_block, end_block)
    resolver = make_uid_resolver({ck: uid})

    # Live depth to craft a max‑slip deposit
    live_depth = await depth(meta.netuid)
    if live_depth == 0:
        pytest.skip("Pool depth is zero – cannot demonstrate slippage")

    price_obj = await pricing(meta.netuid, start_block, end_block)
    tao_price = Decimal(str(price_obj.tao))

    events = [TransferEvent(ck, meta.netuid, int(live_depth))]

    rewards = await compute_epoch_rewards(
        validator=_SimpleValidator(list(meta.uids)),
        scanner=_DummyScanner(events),
        pricing=pricing,
        uid_of_coldkey=resolver,
        start_block=start_block,
        end_block=end_block,
        bag_sn73=BAG_SN73,
        c0=c0,
        beta=beta,
        r_min=r_min,
        pool_depth_of=depth,
    )

    raw_value = Decimal(live_depth) * tao_price / Decimal(10**18)
    awarded = rewards.rewards_per_miner[uid]

    assert awarded < raw_value
    assert awarded > 0
    print(
        f"Slippage OK – raw {raw_value:.6f} TAO ⇒ "
        f"post‑slip {awarded:.6f} SN‑73"
    )


# ======================================================================
# 3️⃣  Bag cap & surplus burn
# ======================================================================
@pytest.mark.asyncio
@pytest.mark.integration
async def test_bag_cap_and_burn(live_chain, bond_curve_params):
    st, meta, start_block, end_block = live_chain
    c0, beta, r_min = bond_curve_params

    pricing, depth = make_providers(st, start_block, end_block)
    ck_to_uid = {ck: int(uid) for ck, uid in zip(meta.coldkeys, meta.uids)}
    resolver = make_uid_resolver(ck_to_uid)

    # Craft enough deposits to overshoot the SN‑73 bag
    ONE_TAO = 10**18
    events: List[TransferEvent] = []
    rng = random.Random(42)
    miners = list(ck_to_uid.keys())
    rng.shuffle(miners)

    for _ in range(len(miners) * 3):  # generous overshoot
        events.append(TransferEvent(rng.choice(miners), meta.netuid, ONE_TAO))

    rewards = await compute_epoch_rewards(
        validator=_SimpleValidator(list(meta.uids)),
        scanner=_DummyScanner(events),
        pricing=pricing,
        uid_of_coldkey=resolver,
        start_block=start_block,
        end_block=end_block,
        bag_sn73=BAG_SN73,
        c0=c0,
        beta=beta,
        r_min=r_min,
        pool_depth_of=depth,
    )

    issued = sum(rewards.rewards_per_miner.values())
    burned = rewards.rewards_per_miner.get(0, Decimal(0))

    assert issued <= Decimal(BAG_SN73)
    assert burned == Decimal(BAG_SN73) - issued
    assert burned > 0

    print(
        f"Bag‑cap OK – issued {issued:.6f}/{BAG_SN73} SN‑73, "
        f"burned {burned:.6f} to UID 0"
    )
