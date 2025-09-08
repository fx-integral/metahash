# ╭──────────────────────────────────────────────────────────────────────╮
# metahash/validator/epoch_validator.py                                  #
# (v2.3 + EPOCH_LENGTH_OVERRIDE support + TESTING bootstrap forward)     #
# ╰──────────────────────────────────────────────────────────────────────╯
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Optional, Tuple

import bittensor as bt
from bittensor import BLOCKTIME  # 12 s on Finney

from metahash.base.validator import BaseValidatorNeuron
from metahash.config import EPOCH_LENGTH_OVERRIDE, TESTING


class EpochValidatorNeuron(BaseValidatorNeuron):
    """Validator base-class with robust epoch rollover handling.

    This version additionally honors `EPOCH_LENGTH_OVERRIDE` from
    `metahash.config`. When > 0, epoch length and the wait loop are driven
    by the override (e.g., 10 blocks) instead of the chain's tempo.
    This lets you test full e/e+1/e+2 flows quickly.

    ⚠️ Note: With overrides you are *not* aligned to real chain epoch heads.
    If you try to call `set_weights()` outside real heads, the chain will
    reject it. When `TESTING=True`, you should also configure the validator
    to avoid on-chain `set_weights` (e.g., via `config.no_epoch=True`).
    """

    def __init__(self, *args, log_interval_blocks: int = 2, **kwargs):
        super().__init__(*args, **kwargs)
        self._log_interval_blocks = max(1, int(log_interval_blocks))
        self._epoch_len: Optional[int] = None
        self.epoch_end_block: Optional[int] = None
        self._override_active: bool = False
        self._bootstrapped: bool = False  # run forward immediately if TESTING

    # ----------------------- helpers ---------------------------------- #
    def _discover_epoch_length(self) -> int:
        try:
            override = int(EPOCH_LENGTH_OVERRIDE or 0)
        except Exception:
            override = 0

        if override > 0:
            self._override_active = True
            length = max(1, override)
            if self._epoch_len != length:
                bt.logging.info(
                    f"[epoch] using EPOCH_LENGTH_OVERRIDE = {length} blocks "
                    f"(TESTING={TESTING})"
                )
            self._epoch_len = length
            return length

        self._override_active = False
        tempo = self.subtensor.tempo(self.config.netuid) or 360
        try:
            head = self.subtensor.get_current_block()
            next_head = self.subtensor.get_next_epoch_start_block(self.config.netuid)
            if next_head is None:
                raise ValueError("RPC returned None")
            derived = next_head - (head - head % tempo)
            length = derived if derived in (tempo, tempo + 1) else tempo + 1
        except Exception as e:
            bt.logging.warning(f"[epoch] RPC error while probing length: {e}")
            length = tempo + 1

        if self._epoch_len != length:
            bt.logging.info(f"[epoch] detected length = {length} (chain mode)")
        self._epoch_len = length
        return length

    def _epoch_snapshot(self) -> Tuple[int, int, int, int, int]:
        blk = self.subtensor.get_current_block()
        ep_l = self._epoch_len or self._discover_epoch_length()
        start = blk - (blk % ep_l)
        end = start + ep_l - 1
        idx = blk // ep_l
        self.epoch_end_block = end
        return blk, start, end, idx, ep_l

    def _apply_epoch_state(self, blk: int, start: int, end: int, idx: int, ep_len: int):
        """Persist epoch fields to the instance and log the head."""
        self.epoch_start_block = start
        self.epoch_end_block = end
        self.epoch_index = idx
        self.epoch_tempo = ep_len
        label = "override" if self._override_active else "chain"
        head_time = datetime.utcnow().strftime("%H:%M:%S")
        bt.logging.success(
            f"[epoch {idx} @ {label}] head at block {blk:,} ({head_time} UTC) – len={ep_len}"
        )

    async def _wait_for_next_head(self):
        head_block = self.subtensor.get_current_block()
        ep_l = self._epoch_len or self._discover_epoch_length()
        target_head = head_block - (head_block % ep_l) + ep_l
        label = "override" if self._override_active else "chain"

        while not self.should_exit:
            blk = self.subtensor.get_current_block()
            if blk >= target_head:
                return
            remain = max(0, target_head - blk)
            eta_s = remain * BLOCKTIME
            bt.logging.info(
                f"[status:{label}] Block {blk:,} | {remain} blocks → next {label} head "
                f"(~{int(eta_s // 60)}m{int(eta_s % 60):02d}s) | len={ep_l}"
            )
            sleep_blocks = max(1, min(30, remain // 2 or 1))
            await asyncio.sleep(sleep_blocks * BLOCKTIME * 0.95)

    # ----------------------- main loop -------------------------------- #
    def run(self):  # noqa: D401
        bt.logging.info(
            f"EpochValidator starting at block {self.block:,} (netuid {self.config.netuid})"
        )

        async def _loop():
            while not self.should_exit:
                blk, start, end, idx, ep_len = self._epoch_snapshot()

                next_head = start + ep_len
                into = blk - start
                left = max(1, next_head - blk)
                eta_s = left * BLOCKTIME
                label = "override" if self._override_active else "chain"

                bt.logging.info(
                    f"[status:{label}] Block {blk:,} | Epoch {idx} "
                    f"[{into}/{ep_len} blocks] – next {label} head in {left} "
                    f"blocks (~{int(eta_s // 60)}m{int(eta_s % 60):02d}s)"
                )

                # --- TESTING bootstrap: run a forward immediately on first loop ---
                if TESTING and not self._bootstrapped:
                    # Treat the *current* position as a head for testing purposes.
                    # This avoids waiting for the next real/override head on startup.
                    self._apply_epoch_state(blk, start, end, idx, ep_len)
                    self._bootstrapped = True

                    try:
                        self.sync()
                        await self.concurrent_forward()
                    except Exception as err:
                        bt.logging.error(f"bootstrap forward() raised: {err}")
                    finally:
                        try:
                            self.sync()
                        except Exception as e:
                            bt.logging.warning(f"wallet sync failed: {e}")
                        self.step += 1

                    # After bootstrap, continue the loop to await the next head normally (unless no_epoch).
                    if self.config.no_epoch:
                        # When no_epoch is True, we keep looping to call forward at each detected head
                        # (below, after the wait). Skip the wait here so we can refresh the snapshot now.
                        pass
                    else:
                        await self._wait_for_next_head()

                else:
                    # Normal path: wait for the next head unless explicitly disabled.
                    if not self.config.no_epoch:
                        await self._wait_for_next_head()

                # Recompute snapshot *at* the head (or immediately after bootstrap)
                self._epoch_len = None  # allow re-probe in case tempo/override changed
                blk2, start2, end2, idx2, ep_len2 = self._epoch_snapshot()
                self._apply_epoch_state(blk2, start2, end2, idx2, ep_len2)

                try:
                    self.sync()
                    await self.concurrent_forward()
                except Exception as err:
                    bt.logging.error(f"forward() raised: {err}")
                finally:
                    try:
                        self.sync()
                    except Exception as e:
                        bt.logging.warning(f"wallet sync failed: {e}")
                    self.step += 1

        try:
            self.loop.run_until_complete(_loop())
        except KeyboardInterrupt:
            getattr(self, "axon", bt.logging).stop()
            bt.logging.success("Validator stopped by keyboard interrupt.")
