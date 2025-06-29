# ====================================================================== #
# metahash/utils/subnet_utils.py                            
# ====================================================================== #


from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from statistics import mean
from typing import AsyncGenerator, Optional
from bittensor.core.metagraph import AsyncMetagraph
from bittensor import AsyncSubtensor
from bittensor.utils.balance import Balance

# ── project‑specific constants (keep or replace) ────────────────────────────
from metahash.config import (
    DEFAULT_NETWORK,   # e.g. "finney"
    DEFAULT_NETUID,       # default netuid for your project
    DECIMALS,          # 10**9 for TAO
    SAMPLE_POINTS,
)
# ─────────────────────────────────────────────────────────────────────────────


# ╭────────────────────────────── context helpers ───────────────────────────╮


@asynccontextmanager
async def _auto_subtensor(network=DEFAULT_NETWORK) -> AsyncGenerator[AsyncSubtensor, None]:
    """Create, initialise, yield, and tear‑down a subtensor client."""
    st = AsyncSubtensor(network=network)   # sync ctor
    await st.initialize()                  # async handshake
    try:
        yield st
    finally:
        await st.close()


@asynccontextmanager
async def _with_subtensor(
    st: Optional[AsyncSubtensor],
) -> AsyncGenerator[AsyncSubtensor, None]:
    """Yield the supplied client (if any) or manage one automatically."""
    if st is None:
        async with _auto_subtensor() as temp:
            yield temp
    else:
        yield st


# ╭───────────────────────────── public helpers ──────────────────────────────╮
async def subnet_info(
    netuid: int = DEFAULT_NETUID,
    *,
    st: AsyncSubtensor | None = None,
    block: int | None = None,
):
    """Return the ``DynamicInfo`` object for *netuid* at *block* (or head)."""
    async with _with_subtensor(st) as sub:
        return await sub.subnet(netuid, block=block)


async def current_epoch(netuid: int = DEFAULT_NETUID, *, st=None):
    """Snapshot of where we are in the current epoch."""
    async with _with_subtensor(st) as sub:
        tempo = await sub.tempo(netuid)
        head = await sub.get_current_block()
        return {
            "epoch_index": head // tempo if tempo else None,
            "blocks_into_epoch": head % tempo if tempo else None,
            "tempo": tempo,
            "chain_block": head,
            "utc_time": datetime.now(tz=timezone.utc),
        }


async def subnet_price(netuid: int = DEFAULT_NETUID, *, st=None) -> Balance:
    """TAO price of 1 α‑share in the subnet’s liquidity pool."""
    async with _with_subtensor(st) as sub:
        info = await sub.subnet(netuid)
        return info.price


async def liquidity_and_slippage(netuid: int = DEFAULT_NETUID, tao_in=1, *, st=None):
    """Return pool sizes and simple slippage estimate for a buy of *tao_in* TAO."""
    async with _with_subtensor(st) as sub:
        info = await sub.subnet(netuid)
        tao_pool = info.tao_in
        alpha_pool = info.alpha_in
        price = info.price

        tao_in_rao = int(tao_in * DECIMALS)
        new_price = Balance.from_rao(
            (tao_pool.rao + tao_in_rao) * DECIMALS // (alpha_pool.rao or 1)
        )
        slip = new_price.tao / price.tao - 1

        return {"tao_pool": tao_pool, "alpha_pool": alpha_pool,
                "price": price, "slippage": slip}


async def average_price(netuid: int, start_block: int, end_block: int, *,
                        st=None, sample=SAMPLE_POINTS) -> Balance:
    """Arithmetic mean TAO price sampled across a block interval."""
    step = max((end_block - start_block) // max(sample - 1, 1), 1)
    prices = []

    async with _with_subtensor(st) as sub:
        for blk in range(start_block, end_block + 1, step):
            try:
                prices.append((await sub.subnet(netuid, block=blk)).price.rao)
            except Exception:
                price_rao = await sub.query_runtime_api(
                    "StakeInfoRuntimeApi", "get_subnet_price_at",
                    params=[netuid, blk])
                prices.append(int(price_rao))

    return Balance.from_rao(int(mean(prices))) if prices else Balance.tao(0)


# ╭────────────────────────────── NEW helper ────────────────────────────────╮
async def get_metagraph(
    netuid: int,
    *,
    st: AsyncSubtensor | None = None,
    lite: bool = False,              # need full fields for emission
    block: int | None = None,
) -> AsyncMetagraph:
    """
    Wrapper around ``AsyncSubtensor.metagraph()`` with auto‑managed client.

    Returns a fully‑synced ``AsyncMetagraph`` for *netuid*.
    """
    async with _with_subtensor(st) as sub:
        return await sub.metagraph(netuid, lite=lite, block=block)


# ╭──────────────────────────────── quick demo ──────────────────────────────╮
if __name__ == "__main__":          # pragma: no cover
    async def _demo():
        print(await current_epoch())
        print(await subnet_price())
        print(await liquidity_and_slippage(tao_in=5))

    asyncio.run(_demo())
