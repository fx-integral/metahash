# ╭────────────────────────────────────────────────────────────────────────╮
# neurons/validator.py                                                    #
# ╰────────────────────────────────────────────────────────────────────────╯
from __future__ import annotations

import asyncio
import time
from typing import Optional, List

import bittensor as bt
from metahash.base.utils.logging import ColoredLogger as clog
from metahash.config import (
    BAG_SN73, P_S_PAR,
    ADJUST_BOND_CURVE,
    D_START, D_TAIL_TARGET,
    GAMMA_TARGET,
    TREASURY_COLDKEY,
    STARTING_AUCTIONS_BLOCK,
    AUCTION_DELAY_BLOCKS,          
)
from metahash.utils.bond_utils import (
    beta_from_gamma,
    curve_params,
    get_bond_curve,
)
from metahash.validator.rewards import compute_epoch_rewards, TransferEvent
from metahash.validator.epoch_validator import EpochValidatorNeuron
from metahash.validator.alpha_transfers import AlphaTransfersScanner as _AlphaScanner
from metahash.bittensor_config import config


class Validator(EpochValidatorNeuron):
    """
    Adaptive validator – executed exactly ONCE per epoch head.
    Phases (executed in this order):

        1) Score miners for previous epoch
        2) Optionally update bond‑curve parameters
    """

    # ───────────────────────────────────────────────────────────────────── #
    def __init__(self, config=None):
        super().__init__(config=config)

        # Governable parameters
        self.treasury_coldkey: str = TREASURY_COLDKEY

        # Runtime state
        self._async_subtensor: Optional[bt.AsyncSubtensor] = None
        self._last_validated_epoch: Optional[int] = None
        self._eaten_metric_prev: float = 0.0

        # Bond‑curve Updating
        self.gamma: float = GAMMA_TARGET
        self.r_min_factor: float = (1 - D_START) / (1 - D_TAIL_TARGET)

        # Snapshot of the curve for *this* epoch
        curve = get_bond_curve()
        self._beta_current: float = curve.beta
        self._c0_current: float = curve.c0
        self._r_min_current: float = curve.r_min
        self._curve_params = (
            self._beta_current,
            self._c0_current,
            self._r_min_current,
        )

    # ╭────────────────── async‑substrate helper ──────────────────────────╮
    async def _ensure_async_subtensor(self):
        if self._async_subtensor is None:
            stxn = bt.AsyncSubtensor(
                network=self.config.subtensor.network,
            )
            await stxn.initialize()
            self._async_subtensor = stxn

    # ╭──────────────────── providers & scanners ──────────────────────────╮
    @staticmethod
    def _make_scanner(async_subtensor: bt.AsyncSubtensor):
        """Wrap AlphaTransfersScanner → TransferEvent objects
           understood by the pure‑Python rewards pipeline."""
        alpha_scanner = _AlphaScanner(
            async_subtensor, dest_coldkey=TREASURY_COLDKEY
        )

        class _Scanner:
            async def scan(self, from_block: int, to_block: int):
                raw = await alpha_scanner.scan(from_block, to_block)
                return [
                    TransferEvent(
                        coldkey=ev.dest_coldkey or "",
                        subnet_id=ev.subnet_id,
                        amount_rao=ev.amount_rao,
                    )
                    for ev in raw
                ]

        return _Scanner()

    def _make_pricing_provider(self, async_subtensor: bt.AsyncSubtensor):
        class _Pricing:
            async def __call__(self, subnet_id: int, start: int, end: int):
                spot = await async_subtensor.alpha_tao_avg_price(
                    subnet_id=subnet_id, start=start, end=end
                )
                return type("Price", (), {"tao": spot, "rao": None})  # shim

        return _Pricing()

    def _make_pool_depth_provider(self, async_subtensor: bt.AsyncSubtensor):
        async def _depth(subnet_id: int) -> int:
            return await async_subtensor.alpha_pool_depth(subnet_id=subnet_id)

        return _depth

    def _make_uid_resolver(self) -> callable:
        """
        Re‑build the coldkey→UID map **every epoch head** so new miners start
        receiving rewards immediately.
        """

        async def _resolver(coldkey: str) -> Optional[int]:
            # Detect metagraph growth
            if len(self.metagraph.coldkeys) != getattr(
                self, "_ck_cache_size", 0
            ):
                cold_to_uid = {
                    ck: uid for uid, ck in enumerate(self.metagraph.coldkeys)
                }
                self._cold_to_uid_cache = cold_to_uid
                self._ck_cache_size = len(cold_to_uid)

            return self._cold_to_uid_cache.get(coldkey)

        # Prime cache at construction time
        self._cold_to_uid_cache = {
            ck: uid for uid, ck in enumerate(self.metagraph.coldkeys)
        }
        self._ck_cache_size = len(self.metagraph.coldkeys)

        return _resolver

    # ╭────────────────────────────── PHASE 1 ─────────────────────────────╮
    async def _set_weights_for_previous_epoch(
        self,
        prev_epoch_index: int,
        prev_start_block: int,
        prev_end_block: int,
        async_subtensor: bt.AsyncSubtensor,
    ) -> None:
        """
        Reward accounting for the *previous* epoch and score update.
        Scans **only the last (epoch_len – AUCTION_DELAY_BLOCKS) blocks**
        of the epoch so that miners have 50 blocks to deposit α before
        the auction window opens.
        """
        if prev_epoch_index < 0 or prev_epoch_index == self._last_validated_epoch:
            bt.logging.error("Phase 1 skipped – epoch calculation mismatch")
            return

        # ── NEW: honour auction delay ────────────────────────────────── #
        scan_start_block = prev_start_block + AUCTION_DELAY_BLOCKS

        # Safety: never allow the range to be empty / negative
        scan_start_block = min(scan_start_block, prev_end_block)

        beta_prev, c0_prev, r_min_prev = self._curve_params

        epoch_out = await compute_epoch_rewards(
            validator=self,
            scanner=self._make_scanner(async_subtensor),
            pricing=self._make_pricing_provider(async_subtensor),
            pool_depth_of=self._make_pool_depth_provider(async_subtensor),
            uid_of_coldkey=self._make_uid_resolver(),
            start_block=scan_start_block,          
            end_block=prev_end_block,
            bag_sn73=BAG_SN73,
            c0=c0_prev,
            beta=beta_prev,
            r_min=r_min_prev,
            log=lambda m: clog.debug(m, color="gray"),
        )

        self._last_epoch_rewards = epoch_out.rewards

        # ---------------------------------------------------------------- #
        miner_uids: List[int] = self.get_miner_uids()
        if len(miner_uids) != len(epoch_out.weights):
            raise ValueError(
                f"Bug: weight vector length {len(epoch_out.weights)} "
                f"≠ metagraph size {len(miner_uids)}"
            )

        current_block = self.subtensor.get_current_block()   # get the chain height

        # ── Burn‑all fallback on zero‑weights *or* pre‑cut‑off block ───── #
        if not any(epoch_out.weights) or current_block < STARTING_AUCTIONS_BLOCK:
            bt.logging.error(
                "Burn triggered – "
                + ("zero-weight vector" if not any(epoch_out.weights) else
                   f"height {current_block} < {STARTING_AUCTIONS_BLOCK}")
                + ". Redirecting full emission to UID 0."
            )
            burn_weights = [1.0 if uid == 0 else 0.0 for uid in miner_uids]
            self.update_scores(burn_weights, miner_uids)
            if hasattr(self, "set_weights"):
                self.set_weights()
            self._last_validated_epoch = prev_epoch_index
            return
        # ---------------------------------------------------------------- #

        # Normal path
        self.update_scores(epoch_out.weights, miner_uids)
        if hasattr(self, "set_weights"):
            self.set_weights()

        self.update_controllers(epoch_out)
        self._last_validated_epoch = prev_epoch_index

    # ╭────────────────────────────── PHASE 2 ─────────────────────────────╮
    def _maybe_update_curve(self) -> None:
        if not ADJUST_BOND_CURVE:
            clog.debug("Bond‑curve auto‑tune disabled.")
            return

        beta = beta_from_gamma(BAG_SN73, D_START, self.gamma)
        c0, r_min = curve_params(P_S_PAR, D_START, self.r_min_factor)

        self._beta_current, self._c0_current, self._r_min_current = (
            beta,
            c0,
            r_min,
        )
        self._curve_params = (beta, c0, r_min)

        clog.info(
            f"Bond‑curve set: β={beta:.6g}, c0={c0:.4f}, r_min={r_min:.4f}",
            color="cyan",
        )

    # ╭───────────────────────────── Main loop ────────────────────────────╮
    async def forward(self) -> None:
        await self._ensure_async_subtensor()
        current_start = self.epoch_start_block

        # Ask chain for the previous epoch start block
        prev_start_block = current_start - self.epoch_tempo

        # Fallback if substrate method fails
        if prev_start_block is None:
            prev_start_block = current_start - (self.epoch_tempo + 1)

        prev_end_block = current_start - 1
        prev_epoch_index = self.epoch_index - 1

        clog.info(
            f"⤵︎  Entering epoch {self.epoch_index}  (block {current_start})",
            color="cyan",
        )

        try:
            # ── Phase 1 ─────────────────────────────────────────────────── #
            clog.info(
                "▶︎ Phase 1 – reward accounting & γ / tail controllers",
                color="yellow",
            )
            await self._set_weights_for_previous_epoch(
                prev_epoch_index,
                prev_start_block,
                prev_end_block,
                self._async_subtensor,
            )

            # ── Phase 2 ─────────────────────────────────────────────────── #
            clog.info("▶︎ Phase 2 – bond‑curve auto‑tune", color="yellow")
            self._maybe_update_curve()

        except Exception as err:
            bt.logging.error(f"forward() – unexpected exception: {err}")
            raise

    # ╭────────────────────── clean shutdown helpers ───────────────────────╮
    async def _close_async_subtensor(self):
        if self._async_subtensor:
            try:
                await self._async_subtensor.__aexit__(None, None, None)
            except Exception as e:
                bt.logging.warning(f"AsyncSubtensor close failed: {e}")

    def __del__(self):
        if self._async_subtensor:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(self._close_async_subtensor())
                else:
                    loop.run_until_complete(self._close_async_subtensor())
            except RuntimeError:
                pass  # interpreter shutting down


# ╭────────────────── production keep‑alive (optional) ───────────────────╮
if __name__ == "__main__":
    with Validator(config=config()) as validator:
        while True:
            clog.info("Validator Running...", color="gray")
            time.sleep(15)
