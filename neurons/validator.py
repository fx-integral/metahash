# ╭────────────────────────────────────────────────────────────────────────╮
# neurons/validator.py                                                     #
# ╰────────────────────────────────────────────────────────────────────────╯


from __future__ import annotations

import asyncio
import time
from typing import Optional
import bittensor as bt
from metahash.base.utils.logging import ColoredLogger as clog
from metahash.config import (
    BAG_SN73, P_S_PAR,
    ADJUST_BOND_CURVE,
    D_START, D_TAIL_TARGET,
    GAMMA_TARGET,
    TREASURY_COLDKEY,
    AUCTION_DELAY_BLOCKS,
    FORCE_BURN_WEIGHTS,
)
from metahash.utils.bond_utils import (
    beta_from_gamma,
    curve_params,
    get_bond_curve,
)
from metahash.validator.rewards import (
    compute_epoch_rewards,
    TransferEvent,
)
from metahash.validator.epoch_validator import EpochValidatorNeuron

# Official helper for average subnet price
from metahash.utils.subnet_utils import average_price, average_depth


class Validator(EpochValidatorNeuron):
    """
    Adaptive validator – executed exactly ONCE per epoch head.
    """

    # ───────────────────────────────────────────────────────────────── #
    def __init__(self, config=None):
        super().__init__(config=config)

        # single lock shared by every RPC that touches the websocket
        self._rpc_lock: asyncio.Lock = asyncio.Lock()

        # Governable parameters
        self.treasury_coldkey: str = TREASURY_COLDKEY

        # Runtime state
        self._async_subtensor: Optional[bt.AsyncSubtensor] = None
        self._last_validated_epoch: Optional[int] = None

        # Bond‑curve Updating
        self.gamma: float = GAMMA_TARGET
        # Tail discount must be *smaller* than the apex discount
        self.r_min_factor: float = (1 - D_TAIL_TARGET) / (1 - D_START)
        assert self.r_min_factor <= 1.0, "r_min must not exceed c0"

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

    # ╭────────────────── async‑substrate helper ───────────────────────╮
    async def _ensure_async_subtensor(self):
        if self._async_subtensor is None:
            stxn = bt.AsyncSubtensor(network=self.config.subtensor.network)
            await stxn.initialize()
            self._async_subtensor = stxn

    # ╭──────────────────── providers & scanners ────────────────────────╮
    def _make_scanner(self, async_subtensor: bt.AsyncSubtensor):
        """
        Wrap AlphaTransfersScanner → TransferEvent objects understood by
        the pure‑Python rewards pipeline.
        """
        from metahash.validator.alpha_transfers import (
            AlphaTransfersScanner as _AlphaScanner,
        )

        alpha_scanner = _AlphaScanner(
            async_subtensor,
            dest_coldkey=TREASURY_COLDKEY,
            rpc_lock=self._rpc_lock,  # single websocket recv() lock
        )

        outer = self                 # capture Validator for UID→cold‑key map

        class _Scanner:
            async def scan(self, from_block: int, to_block: int):
                raw = await alpha_scanner.scan(from_block, to_block)

                # Fast UID→cold‑key map (may be missing for legacy chains)
                uid2ck = outer.metagraph.coldkeys

                def _uid_to_ck(uid: int | None) -> str:
                    try:
                        return uid2ck[uid] if uid is not None and uid >= 0 else None
                    except IndexError:
                        return None

                return [
                    TransferEvent(
                        src_coldkey=ev.src_coldkey or _uid_to_ck(ev.from_uid),
                        dest_coldkey=ev.dest_coldkey or outer.treasury_coldkey,
                        subnet_id=ev.subnet_id,
                        amount_rao=ev.amount_rao,
                    )
                    for ev in raw
                    if (ev.src_coldkey or _uid_to_ck(ev.from_uid)) is not None
                ]

        return _Scanner()

    def _make_pricing_provider(
        self,
        async_subtensor: bt.AsyncSubtensor,
        start_block: int,
        end_block: int,
    ):
        async def _pricing(subnet_id: int, *_unused):
            return await average_price(
                subnet_id,
                start_block=start_block,
                end_block=end_block,
                st=async_subtensor,
            )
        return _pricing

    def _make_pool_depth_provider(
        self,
        async_subtensor: bt.AsyncSubtensor,
        start_block: int,
        end_block: int,
    ):
        async def _depth(subnet_id: int) -> int:
            return await average_depth(
                subnet_id,
                start_block=start_block,
                end_block=end_block,
                st=async_subtensor,
            )

        return _depth
    # ----------------------------------------------------------------------- #

    def _make_uid_resolver(self) -> callable:
        """Coldkey → UID map, refreshed every epoch head."""
        async def _resolver(coldkey: str) -> Optional[int]:
            if len(self.metagraph.coldkeys) != getattr(
                self, "_ck_cache_size", 0
            ):
                self._cold_to_uid_cache = {
                    ck: uid for uid, ck in enumerate(self.metagraph.coldkeys)
                }
                self._ck_cache_size = len(self.metagraph.coldkeys)
            return self._cold_to_uid_cache.get(coldkey)

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
        """
        if prev_epoch_index < 0 or prev_epoch_index == self._last_validated_epoch:
            bt.logging.error("Phase 1 skipped – epoch calculation mismatch")
            return

        miner_uids: list[int] = list(self.get_miner_uids())

        # Honour auction delay: skip the first AUCTION_DELAY_BLOCKS blocks
        scan_start_block = min(
            prev_start_block + AUCTION_DELAY_BLOCKS, prev_end_block
        )

        beta_prev, c0_prev, r_min_prev = self._curve_params

        rewards = await compute_epoch_rewards(
            miner_uids=miner_uids,
            scanner=self._make_scanner(async_subtensor),
            pricing=self._make_pricing_provider(
                async_subtensor, prev_start_block, prev_end_block
            ),
            pool_depth_of=self._make_pool_depth_provider(
                async_subtensor, prev_start_block, prev_end_block
            ),
            uid_of_coldkey=self._make_uid_resolver(),
            start_block=scan_start_block,
            end_block=prev_end_block,
            log=lambda m: clog.debug(m, color="gray"),
        )

        self._last_epoch_rewards = rewards

        # Burn‑all fallback
        are_rewards_empty = not any(rewards)
        if FORCE_BURN_WEIGHTS or are_rewards_empty:
            bt.logging.warning(
                "Burn triggered – redirecting full emission to UID 0."
            )
            bt.logging.debug(
                f"FORCE_BURN_WEIGHTS: {FORCE_BURN_WEIGHTS}. are_rewards_empty: {are_rewards_empty}"
            )

            burn_weights = [1.0 if uid == 0 else 0.0 for uid in miner_uids]
            self.update_scores(burn_weights, miner_uids)
            if hasattr(self, "set_weights"):
                self.set_weights()
            self._last_validated_epoch = prev_epoch_index
            return

        # Normal path
        self.update_scores(rewards, miner_uids)
        self.set_weights()

        # Any additional controller logic
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
        prev_start_block = current_start - self.epoch_tempo
        if prev_start_block is None:
            prev_start_block = current_start - (self.epoch_tempo + 1)
        prev_end_block = current_start - 1
        prev_epoch_index = self.epoch_index - 1

        clog.info(
            f"⤵︎  Entering epoch {self.epoch_index}  (block {current_start})",
            color="cyan",
        )

        try:
            clog.info("▶︎ Phase 1 – reward accounting", color="yellow")
            await self._set_weights_for_previous_epoch(
                prev_epoch_index,
                prev_start_block,
                prev_end_block,
                self._async_subtensor,
            )

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
    from metahash.bittensor_config import config
    with Validator(config=config()) as validator:
        while True:
            clog.info("Validator Running…", color="gray")
            time.sleep(120)
