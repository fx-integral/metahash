# ╭──────────────────────────────────────────────────────────────────────╮
# metahash/validator/epoch_validator.py                                  #
# ╰──────────────────────────────────────────────────────────────────────╯
from __future__ import annotations

import asyncio
import traceback
from datetime import datetime
from typing import Optional, Tuple

import bittensor as bt
from bittensor import BLOCKTIME  # 12 s on Finney

from metahash.base.validator import BaseValidatorNeuron


class EpochValidatorNeuron(BaseValidatorNeuron):
    """Validator base‑class with robust epoch rollover handling.

    *Does not change the definition of epoch start/end – only how we wait
    for the next head so `forward()` is triggered in the very first
    blocks of every epoch.*
    """

    # ------------------------------------------------------------------ #
    def __init__(self, *args, log_interval_blocks: int = 2, **kwargs):
        super().__init__(*args, **kwargs)
        self._log_interval_blocks = max(1, int(log_interval_blocks))
        self._epoch_len: Optional[int] = None  # recalculated each epoch
        self.epoch_end_block: Optional[int] = None  # cached for waiter

    # ----------------------- helpers ---------------------------------- #
    def _discover_epoch_length(self) -> int:
        """Return the real epoch length, compensating for historical
        tempo + 1 bug. Re‑evaluated at every head."""
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
            length = tempo + 1  # safest guess

        if self._epoch_len != length:
            bt.logging.info(f"[epoch] detected length = {length}")
        self._epoch_len = length
        return length

    def _epoch_snapshot(self) -> Tuple[int, int, int, int, int]:
        """Return (head, start, end, index, length) for the *current* epoch."""
        blk = self.subtensor.get_current_block()
        ep_l = self._epoch_len or self._discover_epoch_length()

        start = blk - (blk % ep_l)
        end = start + ep_l - 1
        idx = blk // ep_l

        # Cache epoch end for the waiter
        self.epoch_end_block = end
        return blk, start, end, idx, ep_l

    # ------------------------ wait‑loop (patched) ---------------------- #
    async def _wait_for_next_head(self):
        """Sleep until the *first block* of the next epoch.

        We compute the target head **once**, outside the loop, so that the
        comparison remains stable and we return exactly at the epoch head.
        """
        head_block = self.subtensor.get_current_block()
        ep_l = self._epoch_len or self._discover_epoch_length()

        # first block of the next epoch
        target_head = head_block - (head_block % ep_l) + ep_l

        while not self.should_exit:
            blk = self.subtensor.get_current_block()
            if blk >= target_head:
                return  # ← will be True exactly ONCE, at the head

            remain = target_head - blk
            eta_s = remain * BLOCKTIME
            bt.logging.info(
                f"[status] Block {blk:,} | {remain} blocks → next epoch "
                f"(~{eta_s // 60:.0f} m {eta_s % 60:02.0f} s)"
            )

            # Sleep a fraction of remaining blocks (1–30) to keep logs fresh
            sleep_blocks = max(1, min(30, remain // 2))
            await asyncio.sleep(sleep_blocks * BLOCKTIME * 0.95)

    # ----------------------------- run -------------------------------- #
    def run(self):  # noqa: D401
        bt.logging.info(
            f"EpochValidator starting at block {self.block:,} (netuid {self.config.netuid})"
        )

        async def _loop():
            while not self.should_exit:
                # Snapshot ---------------------------------------------------
                blk, start, end, idx, ep_len = self._epoch_snapshot()

                # ───────── Bootstrap: run forward immediately on first loop ─
                if not getattr(self, "_bootstrapped", False):
                    self.epoch_start_block = start
                    self.epoch_end_block = end
                    self.epoch_index = idx
                    self.epoch_tempo = ep_len

                    bt.logging.info("[bootstrap] running forward immediately")
                    try:
                        self.sync()
                        await self.concurrent_forward()    # ← runs forward()
                    except Exception as e:
                        bt.logging.error(f"bootstrap forward failed: {e}")

                    self._bootstrapped = True
                # -----------------------------------------------------------

                # Compute same target head used by the waiter for banners
                next_head = start + ep_len
                into = blk - start
                left = max(1, next_head - blk)
                eta_s = left * BLOCKTIME

                bt.logging.info(
                    f"[status] Block {blk:,} | Epoch {idx} "
                    f"[{into}/{ep_len} blocks] – next epoch in {left} "
                    f"blocks (~{eta_s // 60:.0f} m {eta_s % 60:02.0f} s)"
                )

                # Wait for rollover -----------------------------------------
                if not self.config.no_epoch:
                    await self._wait_for_next_head()

                # -------- new epoch head ------------------------------------
                self._epoch_len = None  # force re‑probe
                blk2, start2, end2, idx2, ep_len2 = self._epoch_snapshot()
                head_time = datetime.utcnow().strftime("%H:%M:%S")

                # Expose to subclasses
                self.epoch_start_block = start2
                self.epoch_end_block = end2
                self.epoch_index = idx2
                self.epoch_tempo = ep_len2

                bt.logging.success(
                    f"[epoch {idx2}] head at block {blk2:,} "
                    f"({head_time} UTC) – len={ep_len2}"
                )

                # *** validator business logic *******************************
                try:
                    self.sync()                   # refresh wallet / weights
                    await self.concurrent_forward()
                except Exception as err:
                    bt.logging.error(f"forward() raised: {err}")
                    bt.logging.debug(
                        "".join(traceback.format_exception(err))
                    )
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
