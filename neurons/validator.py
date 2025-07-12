# ╭──────────────────────────────────────────────────────────────────────╮
# neurons/validator.py                                                   #
# Patched 2025‑07‑07 – persists *all* validated epochs for easy editing  #
#                                                                       #
#  * Complies with new TransferEvent signature                           #
#  * Disk‑deduplicates epochs across restarts via JSON array persistence #
# ╰──────────────────────────────────────────────────────────────────────╯
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable, List, Optional, Union

import bittensor as bt
from metahash.base.utils.logging import ColoredLogger as clog
from metahash.config import (
    TREASURY_COLDKEY,
    AUCTION_DELAY_BLOCKS,
    FORCE_BURN_WEIGHTS,
    STARTING_AUCTIONS_BLOCK,
    TESTING
)
from metahash.validator.rewards import compute_epoch_rewards, TransferEvent
from metahash.validator.epoch_validator import EpochValidatorNeuron
from metahash.utils.subnet_utils import average_depth, average_price

# ────────────────────────── persistence constants ────────────────────── #
STATE_FILE = "validated_epochs.json"
STATE_KEY = "validated_epochs"


class Validator(EpochValidatorNeuron):
    """
    Adaptive validator – guarantees **exactly one** execution per epoch head,
    even across restarts, by persisting the set of validated epochs to disk.
    """

    # ───────────────────────── initialization ───────────────────────── #
    def __init__(self, config=None):
        super().__init__(config=config)

        # one asyncio.Lock guarding the single websocket recv() loop
        self._rpc_lock: asyncio.Lock = asyncio.Lock()

        # Governable parameters
        self.treasury_coldkey: str = TREASURY_COLDKEY

        # Runtime state
        self._async_subtensor: Optional[bt.AsyncSubtensor] = None
        self._validated_epochs: set[int] = self._load_validated_epochs()

    # ╭─────────────────────── persistence helpers ─────────────────────╮
    def _state_path(self) -> Path:
        """
        Return the JSON state‑file path (next to wallet hot‑key file).
        Creates the parent folder if needed.
        """
        hotkey_file: Union[str, Path, "bt.Keyfile"] = self.wallet.hotkey_file
        if isinstance(hotkey_file, (str, Path)):
            hotkey_path = Path(hotkey_file)
        else:
            hotkey_path = Path(getattr(hotkey_file, "path", str(hotkey_file)))

        wallet_root = hotkey_path.expanduser().parent
        wallet_root.mkdir(parents=True, exist_ok=True)
        return wallet_root / STATE_FILE

    def _load_validated_epochs(self) -> set[int]:
        """
        Returns a *set* with all epochs already validated, or an empty set.
        Expected on‑disk schema:  {"validated_epochs": [0, 1, 2, …]}
        """
        try:
            data = json.loads(self._state_path().read_text())
            epochs = set(int(x) for x in data.get(STATE_KEY, []))
            bt.logging.info(f"[state] loaded {len(epochs)} validated epochs")
            return epochs
        except Exception:
            return set()

    def _save_validated_epochs(self):
        """
        Atomically writes the set of validated epochs back to disk.
        """
        try:
            tmp_path = self._state_path().with_suffix(".tmp")
            tmp_path.write_text(json.dumps({STATE_KEY: sorted(self._validated_epochs)}))
            tmp_path.replace(self._state_path())
            bt.logging.debug(
                f"[state] stored {len(self._validated_epochs)} validated epochs"
            )
        except Exception as e:
            bt.logging.warning(f"[state] failed to persist epochs: {e}")

    # ╭────────────────── async‑substrate helper ───────────────────────╮
    async def _ensure_async_subtensor(self):
        if self._async_subtensor is None:
            stxn = bt.AsyncSubtensor(network=self.config.subtensor.network)
            await stxn.initialize()
            self._async_subtensor = stxn

    # ╭──────────────────── providers & scanners ────────────────────────╮
    def _make_scanner(self, async_subtensor: bt.AsyncSubtensor):
        """
        Returns an object that conforms to the TransferScanner Protocol
        expected by compute_epoch_rewards.
        """
        from metahash.validator.alpha_transfers import (
            AlphaTransfersScanner as _AlphaScanner,
        )

        alpha_scanner = _AlphaScanner(
            async_subtensor,
            dest_coldkey=self.treasury_coldkey,
            rpc_lock=self._rpc_lock,
        )

        outer = self  # capture for UID→cold‑key lookup

        class _Scanner:
            async def scan(
                self, from_block: int, to_block: int
            ) -> List[TransferEvent]:
                raw = await alpha_scanner.scan(from_block, to_block)

                uid2ck = outer.metagraph.coldkeys

                def _uid_to_ck(uid: int | None) -> Optional[str]:
                    if uid is None or uid < 0:
                        return None
                    try:
                        return uid2ck[uid]
                    except IndexError:
                        return None

                # ── FIX: clone each event, override only missing fields ── #
                return [
                    replace(
                        ev,
                        src_coldkey=ev.src_coldkey or _uid_to_ck(ev.from_uid),
                        dest_coldkey=ev.dest_coldkey or outer.treasury_coldkey,
                    )
                    for ev in raw
                    if (ev.src_coldkey or _uid_to_ck(ev.from_uid)) is not None
                ]

        return _Scanner()

    # --------------- helpers that feed price & depth oracles --------- #
    def _make_pricing_provider(
        self,
        async_subtensor: bt.AsyncSubtensor,
        start_block: int,
        end_block: int,
    ) -> Callable:
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
    ) -> Callable[[int], asyncio.Future]:
        async def _depth(subnet_id: int) -> int:
            return await average_depth(
                subnet_id,
                start_block=start_block,
                end_block=end_block,
                st=async_subtensor,
            )

        return _depth

    def _make_uid_resolver(self) -> Callable[[str], asyncio.Future]:
        """Cold‑key → UID resolver, refreshed each epoch."""
        async def _resolver(coldkey: str) -> Optional[int]:
            if len(self.metagraph.coldkeys) != getattr(self, "_ck_cache_size", 0):
                self._cold_to_uid_cache = {
                    ck: uid for uid, ck in enumerate(self.metagraph.coldkeys)
                }
                self._ck_cache_size = len(self.metagraph.coldkeys)
            return self._cold_to_uid_cache.get(coldkey)

        # prime cache
        self._cold_to_uid_cache = {
            ck: uid for uid, ck in enumerate(self.metagraph.coldkeys)
        }
        self._ck_cache_size = len(self.metagraph.coldkeys)
        return _resolver

    # ╭──────────────────────────── Phase 1 ────────────────────────────╮
    async def _set_weights_for_previous_epoch(
        self,
        prev_epoch_index: int,
        prev_start_block: int,
        prev_end_block: int,
        async_subtensor: bt.AsyncSubtensor,
    ) -> None:
        """
        Reward accounting for the **previous** epoch, followed by weight update.
        """
        if prev_epoch_index < 0 or prev_epoch_index in self._validated_epochs:
            bt.logging.error(
                f"Phase 1 skipped – epoch {prev_epoch_index} already evaluated"
            )
            return

        miner_uids: List[int] = list(self.get_miner_uids())

        scan_start_block = min(
            prev_start_block + AUCTION_DELAY_BLOCKS, prev_end_block
        )

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
        )

        self._last_epoch_rewards = rewards

        burn_all = (
            FORCE_BURN_WEIGHTS
            or not any(rewards)
            or self.block < STARTING_AUCTIONS_BLOCK
        )

        if not TESTING:
            if burn_all:
                bt.logging.warning("Burn triggered – redirecting full emission to UID 0.")
                self.update_scores(
                    [1.0 if uid == 0 else 0.0 for uid in miner_uids], miner_uids
                )
            else:
                self.update_scores(rewards, miner_uids)

            # ── ✅  broadcast weights on-chain  ────────────────────────── #
            if not self.config.no_epoch:          # honour --no-epoch flag
                self.set_weights()
                self._validated_epochs.add(prev_epoch_index)
                self._save_validated_epochs()

    # ╭──────────────────────────── main loop ──────────────────────────╮
    async def forward(self) -> None:
        """Runs once per epoch head – Phase 1 plus bookkeeping."""
        bt.logging.success(
            f"▶︎ forward() called at block {self.block:,} (epoch {self.epoch_index})"
        )

        await self._ensure_async_subtensor()

        current_start = self.epoch_start_block
        prev_start_block = current_start - self.epoch_tempo
        prev_end_block = current_start - 1
        prev_epoch_index = self.epoch_index - 1

        if prev_epoch_index in self._validated_epochs:
            bt.logging.info(
                f"[forward] epoch {prev_epoch_index} already done – nothing to do."
            )
            return

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
        except Exception as err:
            bt.logging.error(f"forward() – unexpected exception: {err}")
            raise

    # ╭────────────────────── clean shutdown helpers ───────────────────╮
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
                pass  # interpreter shut down


# ╭────────────────── production keep‑alive (optional) ──────────────────╮
if __name__ == "__main__":
    from metahash.bittensor_config import config

    with Validator(config=config()) as validator:
        while True:
            clog.info("Validator Running…", color="gray")
            time.sleep(120)
