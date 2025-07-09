# ╭────────────────────────────────────────────────────────────────────╮
# neurons/miner.py  – push‑trigger bidder (July 2025, patched)        #
# ╰────────────────────────────────────────────────────────────────────╯
from __future__ import annotations

import asyncio
import os
import time
from math import isfinite
from typing import Dict, Tuple, Optional

import aiohttp
import bittensor as bt
from metahash.base.miner import BaseMinerNeuron
from metahash.base.utils.logging import ColoredLogger as clog
from metahash.protocol import (
    AuctionStartSynapse, BidSynapse, AckSynapse, WinSynapse,
)
from metahash.config import PLANCK, AUCTION_BUDGET_ALPHA   # type‑check only

# ---------------------- SIMPLE POLICY CONFIG ------------------------- #
SUBNET_ID = 73                  # bid on this subnet
ALPHA_TO_BID = 5.0              # α
DISCOUNT_BPS = 0                # 0 bps
HTTP_ENDPOINT = os.getenv("PAYMENT_ENDPOINT", "http://localhost:8080/pay")
HTTP_TIMEOUT = 10.0             # seconds
# -------------------------------------------------------------------- #


class Miner(BaseMinerNeuron):
    """Minimal push‑trigger bidder neuron."""

    def __init__(self, config=None):
        super().__init__(config=config)

        # validator_hotkey → treasury coldkey (learned from AuctionStart)
        self._treasuries: Dict[str, str] = {}

        # already bid (validator_hot, epoch)
        self._already_bid: set[Tuple[str, int]] = set()

    # ---------------- AuctionStart handler --------------------------- #
    async def auctionstart_forward(
        self, synapse: AuctionStartSynapse
    ) -> AuctionStartSynapse:
        validator_hot = getattr(synapse, "caller_hotkey", None)
        epoch = synapse.epoch_index
        key = (validator_hot, epoch)

        clog.info(f"AuctionStart received from {validator_hot[:6]}… "
                  f"(epoch {epoch})", color="cyan")

        # remember treasury
        self._treasuries[validator_hot] = synapse.treasury_coldkey

        # already bid?
        if key in self._already_bid:
            return synapse

        # stake gate (optional – miner can read own stake)
        stake_alpha = self.metagraph.stake[self.uid]
        if stake_alpha < synapse.min_stake_alpha:
            clog.warning("Stake below S_MIN_ALPHA – skipping bid.", color="yellow")
            return synapse

        # ← FIX: local sanity checks mirror validator
        if not isfinite(ALPHA_TO_BID) or ALPHA_TO_BID <= 0 or ALPHA_TO_BID > AUCTION_BUDGET_ALPHA:
            clog.warning("Invalid ALPHA_TO_BID – not bidding.", color="red")
            return synapse
        if not (0 <= DISCOUNT_BPS <= 10_000):
            clog.warning("DISCOUNT_BPS out of range – not bidding.", color="red")
            return synapse

        bid = BidSynapse(
            subnet_id=SUBNET_ID,
            alpha=ALPHA_TO_BID,
            discount_bps=DISCOUNT_BPS,
        )

        # send bid back to the origin validator only
        axon = next((ax for ax in self.metagraph.axons if ax.hotkey == validator_hot), None)
        if axon is None:
            clog.warning("Origin validator axon not found.", color="red")
            return synapse

        ack = await self.dendrite(
            axons=[axon],
            synapse=bid,
            deserialize=True,
            timeout=8,
        )
        if isinstance(ack, AckSynapse) and ack.accepted:
            self._already_bid.add(key)
            clog.success(f"Bid {ALPHA_TO_BID} α sent to {validator_hot[:6]}…")
        else:
            clog.warning(f"Bid rejected by {validator_hot[:6]}…")

        return synapse

    # ---------------- WinSynapse handler ----------------------------- #
    async def win_forward(self, synapse: WinSynapse) -> WinSynapse:
        validator_hot = getattr(synapse, "caller_hotkey", None)
        treasury_ck = self._treasuries.get(validator_hot)

        if treasury_ck is None:
            clog.warning("Treasury unknown – cannot pay.", color="red")
            return synapse

        payload = {
            "subnet_id": synapse.subnet_id,
            "alpha": synapse.alpha * (1 - synapse.clearing_discount_bps / 10_000),
            "deadline_block": synapse.pay_deadline_block,
            "dest_coldkey": treasury_ck,
        }

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
            ) as sess:
                async with sess.post(HTTP_ENDPOINT, json=payload) as resp:
                    ok = resp.status == 200
                    txt = await resp.text()
        except Exception as exc:  # noqa: BLE001
            clog.warning(f"HTTP payment call failed: {exc}", color="red")
            ok = False
            txt = "-"

        if ok:
            clog.success(f"Payment endpoint accepted tx ({txt})")
        else:
            clog.warning(f"Payment endpoint error ({txt})", color="red")

        return synapse


# ---------------------------- main loop ------------------------------ #
if __name__ == "__main__":
    """
    Run:

        export PAYMENT_ENDPOINT=http://127.0.0.1:8000/pay
        python neurons/miner.py --netuid 73
    """
    with Miner(config=bt.config()) as m:
        while True:
            clog.info("Miner running…", color="gray")
            time.sleep(30)
