# ╭────────────────────────────────────────────────────────────────────────╮
# neurons/validator.py                                                     #
# ╰────────────────────────────────────────────────────────────────────────╯
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Optional

import bittensor as bt
from metahash.base.utils.logging import ColoredLogger as clog
from metahash.config import (
    TREASURY_COLDKEY,
    AUCTION_DELAY_BLOCKS,
    FORCE_BURN_WEIGHTS,
    STARTING_AUCTIONS_BLOCK,
)
from metahash.validator.rewards import compute_epoch_rewards, TransferEvent
from metahash.validator.epoch_validator import EpochValidatorNeuron
from metahash.utils.subnet_utils import average_price, average_depth


# ────────────────────────── constants ────────────────────────── #
STATE_FILE = "last_epoch_state.json"        # one tiny JSON file
STATE_KEY = "last_validated_epoch"


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
        self._last_validated_epoch: Optional[int] = self._load_last_epoch()

    # ╭──────────────────── persistence helpers ─────────────────────╮
    def _state_path(self) -> Path:
        """Path of the state-file (next to hotkey file by default)."""
        wallet_root = Path(self.wallet.hotkey_file).expanduser().parent
        return wallet_root / STATE_FILE

    def _load_last_epoch(self) -> Optional[int]:
        """Return the persisted last-validated epoch (or None)."""
        try:
            data = json.loads(self._state_path().read_text())
            val = int(data.get(STATE_KEY))
            bt.logging.info(f"[state] loaded last epoch = {val}")
            return val
        except Exception:
            return None

    def _save_last_epoch(self, idx: int):
        try:
            self._state_path().write_text(json.dumps({STATE_KEY: idx}))
            bt.logging.debug(f"[state] wrote last epoch = {idx}")
        except Exception as e:
            bt.logging.warning(f"[state] failed to store epoch {idx}: {e}")

    # ╭────────────────── async-substrate helper ───────────────────────╮
    async def _ensure_async_subtensor(self):
        if self._async_subtensor is None:
            stxn = bt.AsyncSubtensor(network=self.config.subtensor.network)
            await stxn.initialize()
            self._async_subtensor = stxn

    # ╭──────────────────── providers & scanners ────────────────────────╮
    # … (same as previous full version; omitted for brevity) …
    # Keep all helper factories unchanged.

    # ----------------------------------------------------------------------- #
    # ╭────────────────────────────── PHASE 1 ─────────────────────────────╮
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
        if (
            prev_epoch_index < 0
            or prev_epoch_index == self._last_validated_epoch
        ):
            bt.logging.error(
                f"Phase 1 skipped – epoch {prev_epoch_index} already evaluated"
            )
            return

        miner_uids: list[int] = list(self.get_miner_uids())

        # Honour auction delay: skip the first AUCTION_DELAY_BLOCKS blocks
        scan_start_block = min(prev_start_block + AUCTION_DELAY_BLOCKS, prev_end_block)

        rewards = await compute_epoch_rewards(
            miner_uids=miner_uids,
            scanner=self._make_scanner(async_subtensor),
            pricing=self._make_pricing_provider(async_subtensor, prev_start_block, prev_end_block),
            pool_depth_of=self._make_pool_depth_provider(
                async_subtensor, prev_start_block, prev_end_block
            ),
            uid_of_coldkey=self._make_uid_resolver(),
            start_block=scan_start_block,
            end_block=prev_end_block,
            log=lambda m: clog.debug(m, color="gray"),
        )

        self._last_epoch_rewards = rewards

        # Burn-all fallback
        are_rewards_empty = not any(rewards)
        if FORCE_BURN_WEIGHTS or are_rewards_empty or self.block < STARTING_AUCTIONS_BLOCK:
            bt.logging.warning("Burn triggered – redirecting full emission to UID 0.")
            burn_weights = [1.0 if uid == 0 else 0.0 for uid in miner_uids]
            self.update_scores(burn_weights, miner_uids)
            self.set_weights()
            self._last_validated_epoch = prev_epoch_index
            self._save_last_epoch(prev_epoch_index)
            return

        # Normal path
        self.update_scores(rewards, miner_uids)
        self.set_weights()

        # Persist & update controller
        self._last_validated_epoch = prev_epoch_index
        self._save_last_epoch(prev_epoch_index)

    # ╭───────────────────────────── Main loop ────────────────────────────╮
    async def forward(self) -> None:
        """Runs once per epoch head – Phase 1 & any extra logic."""
        bt.logging.success(
            f"▶︎ forward() called at block {self.block:,} (epoch {self.epoch_index})"
        )

        await self._ensure_async_subtensor()
        current_start = self.epoch_start_block
        prev_start_block = current_start - self.epoch_tempo
        if prev_start_block is None:
            prev_start_block = current_start - (self.epoch_tempo + 1)
        prev_end_block = current_start - 1
        prev_epoch_index = self.epoch_index - 1

        # Skip if already validated (guard for bootstrap call)
        if prev_epoch_index == self._last_validated_epoch:
            bt.logging.info(
                f"[forward] epoch {prev_epoch_index} already done – nothing to do."
            )
            return

        clog.info(
            f"⤵︎  Entering epoch {self.epoch_index}  (block {current_start})", color="cyan"
        )

        try:
            clog.info("▶︎ Phase 1 – reward accounting", color="yellow")
            await self._set_weights_for_previous_epoch(
                prev_epoch_index,
                prev_start_block,
                prev_end_block,
                self._async_subtensor,
            )
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


# ╭────────────────── production keep-alive (optional) ───────────────────╮
if __name__ == "__main__":
    from metahash.bittensor_config import config

    with Validator(config=config()) as validator:
        while True:
            clog.info("Validator Running…", color="gray")
            time.sleep(120)
