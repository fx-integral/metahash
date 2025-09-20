# neurons/validator.py – SN-73 v2.3.7
# Flow tweaks:
#   • Publish ONLY e−2 (if pending) BEFORE settlement (prevents overwriting by e−1),
#   • Keep weights-first semantics for the epoch,
#   • After settlement & clear, publish catch-up (≤ e−1),
#   • Same auction/clear and catch-up logic; TESTING still suppresses on-chain set_weights.
#   • v2.3.7: Settlement anti-double-counting by coldkey; Clearing 2-pass (caps then relaxed)

from __future__ import annotations

import asyncio
import time

import bittensor as bt
from metahash.base.utils.logging import ColoredLogger as clog
from metahash import __version__
from metahash.utils.pretty_logs import pretty

# Config
from metahash.config import (
    STRATEGY_PATH,
    START_V3_BLOCK,
    EPOCH_LENGTH_OVERRIDE,
    TESTING,
)
from metahash.utils.helpers import load_weights

# State & Engines
from metahash.validator.state import StateStore
from metahash.validator.engines.commitments import CommitmentsEngine
from metahash.validator.engines.settlement import SettlementEngine
from metahash.validator.engines.auction import AuctionEngine
from metahash.validator.engines.clearing import ClearingEngine

# Base neuron
from metahash.validator.epoch_validator import EpochValidatorNeuron


class Validator(EpochValidatorNeuron):
    """
    v2.3.7:
      • Publish-only the specific e−2 commitment (if pending) before settlement,
      • DRY-RUN set_weights (TESTING=True suppresses on-chain),
      • single-pass quotas by normalized reputation,
      • early winner notification, window recorded (pe, as, de),
      • v4 commitments: CID-only on-chain, full in IPFS,
      • strict commitment publisher (no inline fallback),
      • settlement guarded; budget-aware burn handled in SettlementEngine.
      • FIX: anti double-count at settlement by coldkey; 2-pass clearing (caps then relaxed).

    NOTE:
      • Clearing uses TAO budget (base subnet = this validator's netuid) and TAO-valued bids
        = α × (1 − disc) × price_tao_per_alpha[subnet] × weight_fraction (with α-slippage).
    """

    def __init__(self, config=None):
        super().__init__(config=config)
        self.hotkey_ss58: str = self.wallet.hotkey.ss58_address

        # Single AsyncSubtensor client + a per-connection RPC lock.
        self._async_subtensor: bt.AsyncSubtensor | None = None
        self._rpc_lock: asyncio.Lock = asyncio.Lock()

        # State (load before strategies)
        self.state = StateStore()
        if getattr(self.config, "fresh", False):
            self.state.wipe()

        # Strategy (weights per subnet)
        self.weights_bps = load_weights(STRATEGY_PATH)

        # Engines (they reuse the single client)
        self.commitments = CommitmentsEngine(self, self.state)
        self.settlement = SettlementEngine(self, self.state)
        self.clearing = ClearingEngine(self, self.state)
        self.auction = AuctionEngine(self, self.state, self.weights_bps, clearer=self.clearing)

        pretty.banner(
            f"Validator v{__version__} initialized",
            " | ".join(
                filter(
                    None,
                    [
                        f"hotkey={self.hotkey_ss58}",
                        f"netuid={self.config.netuid}",
                        f"epoch(e)={getattr(self, 'epoch_index', 0)}",
                        "fresh" if getattr(self.config, "fresh", False) else "",
                        "Testing" if TESTING else "",
                    ],
                )
            ),
            style="bold magenta",
        )

    async def _stxn(self) -> bt.AsyncSubtensor:
        """Return the shared AsyncSubtensor client (create once)."""
        if self._async_subtensor is None:
            self._async_subtensor = await self._new_async_subtensor()
        return self._async_subtensor

    async def _new_async_subtensor(self) -> bt.AsyncSubtensor:
        stxn = bt.AsyncSubtensor(network=self.config.subtensor.network)
        await stxn.initialize()
        return stxn

    def _apply_epoch_override(self):
        """Keep the fast test cadence if EPOCH_LENGTH_OVERRIDE is set."""
        try:
            if EPOCH_LENGTH_OVERRIDE and EPOCH_LENGTH_OVERRIDE > 0:
                L = int(EPOCH_LENGTH_OVERRIDE)
                blk = int(getattr(self, "block", 0))
                e = blk // L
                self.epoch_index = e
                self.epoch_start_block = e * L
                self.epoch_end_block = self.epoch_start_block + L - 1
                self.epoch_length = L
        except Exception:
            pass  # non-fatal

    async def forward(self):
        """
        Per-epoch driver:
          0) Ensure single AsyncSubtensor client; apply epoch override.
          1) Publish ONLY e−2 (if pending) to guarantee settlement sees its snapshot.
          2) Settle epoch (e−2) and set weights (from commitments) [weights-first].
          3) Masters: auction & clear NOW (epoch e) — stage payload for (e−1).
          4) Publish catch-up (≤ e−1) AFTER settlement (safe for our own node).
        """
        await self._stxn()          # ensure single client exists
        self._apply_epoch_override()

        if self.block < START_V3_BLOCK:
            pretty.banner(
                "Waiting for START_V3_BLOCK",
                f"current_block={self.block} < start_v3={START_V3_BLOCK}",
                style="bold yellow",
            )
            return

        e = self.epoch_index
        pretty.banner(
            f"Epoch {e} (label: e)",
            f"head_block={self.block} | start={self.epoch_start_block} | end={self.epoch_end_block}\n"
            f"• PHASE 1/4: publish e−2 → {e-2} (only if pending; retry on low priority)\n"
            f"• PHASE 2/4: settle e−2 → {e-2} (scan pe={e-1})\n"
            f"• PHASE 3/4: auction & clear e={e} (miners pay in e+1={e+1})\n"
            f"• PHASE 4/4: publish commitments for e−1={e-1} (stored pe) [post‑settlement]\n"
            f"• Weights applied from e in e+2={e+2}",
            style="bold white",
        )

        # 1) NEW: Publish ONLY the specific e−2 payload if it is still pending.
        try:
            await self.commitments.publish_commitment_for(epoch_cleared=e - 2)
        except asyncio.CancelledError as ce:
            pretty.log(f"[yellow]Publish e−2 cancelled by RPC: {ce}[/yellow]")
        except Exception as exc:
            pretty.log(f"[yellow]Publish e−2 failed: {exc}[/yellow]")

        # 2) WEIGHTS FIRST: settle older epoch (e−2)
        await self.settlement.settle_and_set_weights_all_masters(epoch_to_settle=e - 2)

        # 3) Masters: broadcast & clear immediately (if master)
        if not self.auction._is_master_now() and getattr(self.auction, "_not_master_log_epoch", None) != e:
            pretty.log("[yellow]Validator is not a master — skipping broadcast/clear for this epoch.[/yellow]")
            self.auction._not_master_log_epoch = e

        try:
            await self.auction.broadcast_auction_start()
        except asyncio.CancelledError as ce:
            pretty.log(f"[yellow]AuctionStart cancelled by RPC: {ce}. Will retry next epoch.[/yellow]")
            return
        except Exception as exc:
            pretty.log(f"[yellow]AuctionStart failed: {exc}[/yellow]")

        # cleanup bid books older than (e−1)
        self.auction.cleanup_old_epoch_books(before_epoch=e - 2)

        # 4) POST‑SETTLEMENT: publish any pending commitments ≤ e−1.
        try:
            await self.commitments.publish_catch_up(up_to_epoch=e - 1)
        except asyncio.CancelledError as ce:
            pretty.log(f"[yellow]Post-settlement publish cancelled by RPC: {ce}[/yellow]")
        except Exception as exc:
            pretty.log(f"[yellow]Post-settlement publish failed: {exc}[/yellow]")


if __name__ == "__main__":
    from metahash.bittensor_config import config
    with Validator(config=config(role="validator")) as v:
        while True:
            clog.info("Validator running…", color="gray")
            time.sleep(120)
