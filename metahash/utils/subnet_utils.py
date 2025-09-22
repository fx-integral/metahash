# metahash/utils/subnet_utils.py
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from statistics import mean
from typing import AsyncGenerator, Optional, List
import random

import bittensor as bt
from bittensor.core.metagraph import AsyncMetagraph
from bittensor import AsyncSubtensor
from bittensor.utils.balance import Balance
from substrateinterface.exceptions import SubstrateRequestException

from metahash.config import (
    DEFAULT_BITTENSOR_NETWORK,
    SAMPLE_POINTS,
    DECIMALS,
)

# ───────────────────── connection helpers ───────────────────── #

@asynccontextmanager
async def _auto_subtensor(network=DEFAULT_BITTENSOR_NETWORK) -> AsyncGenerator[AsyncSubtensor, None]:
    st = AsyncSubtensor(network=network)
    await st.initialize()
    try:
        yield st
    finally:
        await st.close()

@asynccontextmanager
async def _with_subtensor(st: Optional[AsyncSubtensor]) -> AsyncGenerator[AsyncSubtensor, None]:
    if st is None:
        async with _auto_subtensor() as temp:
            yield temp
    else:
        yield st

# A global-ish map of semaphores per substrate websocket object id.
# Ensures there is only ONE in-flight recv() per connection.
# We don't store the object strongly; we just key by id() at call time.
_semaphores: dict[int, asyncio.Semaphore] = {}

def _rpc_gate_for(sub: AsyncSubtensor) -> asyncio.Semaphore:
    key = id(sub.substrate)
    sem = _semaphores.get(key)
    if sem is None:
        sem = asyncio.Semaphore(1)  # strictly serialize on this websocket
        _semaphores[key] = sem
    return sem

# ───────────────────── public helpers ───────────────────── #

async def subnet_info(netuid: int, *, st: AsyncSubtensor | None = None, block: int | None = None):
    async with _with_subtensor(st) as sub:
        sem = _rpc_gate_for(sub)
        async with sem:
            return await sub.subnet(netuid, block=block)

async def current_epoch(netuid: int, *, st=None):
    async with _with_subtensor(st) as sub:
        sem = _rpc_gate_for(sub)
        async with sem:
            tempo = await sub.tempo(netuid)
        async with sem:
            head = await sub.get_current_block()
        return {
            "epoch_index": head // tempo if tempo else None,
            "blocks_into_epoch": head % tempo if tempo else None,
            "tempo": tempo,
            "chain_block": head,
            "utc_time": datetime.now(tz=timezone.utc),
        }

async def subnet_price(netuid: int, *, st=None) -> Balance:
    async with _with_subtensor(st) as sub:
        sem = _rpc_gate_for(sub)
        async with sem:
            info = await sub.subnet(netuid)
        return info.price

async def liquidity_and_slippage(netuid: int, tao_in=1, *, st=None):
    async with _with_subtensor(st) as sub:
        sem = _rpc_gate_for(sub)
        async with sem:
            info = await sub.subnet(netuid)
        tao_pool = info.tao_in
        alpha_pool = info.alpha_in
        price = info.price

        tao_in_rao = int(tao_in * DECIMALS)
        new_price = Balance.from_rao(
            (tao_pool.rao + tao_in_rao) * DECIMALS // (alpha_pool.rao or 1)
        )
        slip = new_price.tao / price.tao - 1
        return {"tao_pool": tao_pool, "alpha_pool": alpha_pool, "price": price, "slippage": slip}

async def average_price(
    netuid: int,
    start_block: int,
    end_block: int,
    *,
    st=None,
    sample: int = SAMPLE_POINTS,
    concurrent: int = 1,   # ← default serialized
    even: bool = True,
) -> Balance:
    if end_block < start_block:
        raise ValueError("end_block must be ≥ start_block")

    total = end_block - start_block + 1
    sample = max(1, min(sample, total))

    if sample == total:
        block_sample: List[int] = list(range(start_block, end_block + 1))
    elif even:
        step = (total - 1) / (sample - 1)
        block_sample = [round(start_block + i * step) for i in range(sample)]
    else:
        block_sample = random.sample(range(start_block, end_block + 1), sample)
        block_sample.sort()

    async def _price_at(sub: AsyncSubtensor, blk: int, sem: asyncio.Semaphore) -> int:
        # All websocket interactions are protected by sem.
        try:
            async with sem:
                info = await sub.subnet(netuid, block=blk)
            return info.price.rao
        except Exception:
            async with sem:
                price_rao = await sub.query_runtime_api(
                    "StakeInfoRuntimeApi",
                    "get_subnet_price_at",
                    params=[netuid, blk],
                )
            return int(price_rao)

    prices: List[int] = []
    async with _with_subtensor(st) as sub:
        sem = _rpc_gate_for(sub)

        # We *allow* scheduling multiple tasks, but every RPC awaits the same semaphore,
        # ensuring only one recv() is in flight at a time.
        # If you pass distinct `st` instances, you can raise `concurrent` above 1.
        for i in range(0, len(block_sample), max(1, concurrent)):
            batch = block_sample[i : i + max(1, concurrent)]
            batch_vals = await asyncio.gather(*(_price_at(sub, b, sem) for b in batch))
            prices.extend(batch_vals)

    return Balance.from_rao(int(mean(prices))) if prices else Balance.tao(0)

# Choose ONE depending on policy:
FALLBACK_DEPTH_RAO: int = 10 ** 30  # effectively disables slippage if used

_warned = False

async def average_depth(
    netuid: int,
    start_block: int,
    end_block: int,
    *,
    st: Optional[AsyncSubtensor] = None,
    sample: int = SAMPLE_POINTS,
    concurrent: int = 1,   # ← default serialized
    even: bool = True,
) -> int:
    if end_block < start_block:
        raise ValueError("end_block must be ≥ start_block")

    total = end_block - start_block + 1
    sample = max(1, min(sample, total))

    if sample == total:
        block_sample: List[int] = list(range(start_block, end_block + 1))
    elif even:
        step = (total - 1) / (sample - 1)
        block_sample = [round(start_block + i * step) for i in range(sample)]
    else:
        block_sample = random.sample(range(start_block, end_block + 1), sample)
        block_sample.sort()

    async def _depth_at(sub: AsyncSubtensor, blk: int, sem: asyncio.Semaphore) -> int:
        global _warned
        try:
            async with sem:
                bh = await sub.substrate.get_block_hash(block_id=blk)
            async with sem:
                storage = await sub.substrate.query(
                    "SubtensorModule",
                    "SubnetAlphaIn",
                    [netuid],
                    block_hash=bh,
                )
            val = getattr(storage, "value", storage)
            return int(val) if val is not None else 0
        except (SubstrateRequestException, Exception) as err:
            if not _warned:
                bt.logging.critical(
                    f"[depth_at] Failed to read SubnetAlphaIn (netuid={netuid}, block={blk}): {err}\n"
                    f"          Using FALLBACK_DEPTH_RAO = {FALLBACK_DEPTH_RAO:,}. "
                    f"Slippage will be disabled until restart."
                )
                _warned = True
            return FALLBACK_DEPTH_RAO

    depths: List[int] = []
    async with _with_subtensor(st) as sub:
        sem = _rpc_gate_for(sub)
        for i in range(0, len(block_sample), max(1, concurrent)):
            batch = block_sample[i : i + max(1, concurrent)]
            batch_vals = await asyncio.gather(*(_depth_at(sub, b, sem) for b in batch))
            depths.extend(batch_vals)

    return int(mean(depths)) if depths else 0

async def get_metagraph(
    netuid: int,
    *,
    st: AsyncSubtensor | None = None,
    lite: bool = False,
    block: int | None = None,
) -> AsyncMetagraph:
    async with _with_subtensor(st) as sub:
        sem = _rpc_gate_for(sub)
        async with sem:
            return await sub.metagraph(netuid, lite=lite, block=block)

if __name__ == "__main__":  # pragma: no cover
    async def _demo():
        print(await current_epoch(0))
    asyncio.run(_demo())
