# ============================================================================ #
# metahash/validator/epoch_validator.py                                        #
# ============================================================================ #


from __future__ import annotations

import asyncio
import traceback
from datetime import datetime
from typing import Tuple, Optional

import bittensor as bt
from bittensor import BLOCKTIME  
from metahash.base.validator import BaseValidatorNeuron


class EpochValidatorNeuron(BaseValidatorNeuron):
    """Drop-in base-class for validators that want epoch-level pacing."""

    # ------------------------------ init ---------------------------------- #
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.epoch_start_block: Optional[int] = None
        self.epoch_end_block: Optional[int] = None
        self.epoch_index: Optional[int] = None
        self.epoch_tempo: Optional[int] = None

    # --------------------------- helpers ---------------------------------- #
    def _tempo(self) -> int:
        """Subnet tempo or default 360."""
        return self.subtensor.tempo(self.config.netuid) or 360

    @staticmethod
    def _epoch_bounds(blk: int, tempo: int) -> Tuple[int, int]:
        """Return (*start_block*, *end_block*) for *blk* given *tempo*."""
        start = blk - blk % tempo
        end = start + tempo - 1
        return start, end

    def _epoch_snapshot(self):
        """Lightweight synchronous snapshot of chain & epoch state."""
        blk = self.block  # synchronous call
        tempo = self._tempo()
        start, end = self._epoch_bounds(blk, tempo)
        return blk, start, end, blk // tempo, tempo

    # ------------------------------------------------------------------ #
    async def _wait_for_next_head(self) -> None:
        """
        Sleep until the first block of the next epoch.

        Uses AsyncSubtensor.get_next_epoch_start_block() for precision,
        but falls back to a local modulo-based loop if the RPC fails.
        """
        netuid = self.config.netuid

        while not self.should_exit:
            # ---------- precise RPC path -------------------------------- #
            next_head: Optional[int] = None
            try:
                next_head = self.subtensor.get_next_epoch_start_block(netuid)
            except Exception as e:
                bt.logging.warning(f"Failed to fetch next epoch head; fallback: {e}")

            if next_head is not None and next_head > self.block:
                delta_blocks = next_head - self.block
                # Sleep for 90 % of the remaining time, re-check near the head
                sleep_s = max(1.0, delta_blocks * float(BLOCKTIME) * 0.9)
                await asyncio.sleep(sleep_s)
                if self.block >= next_head:
                    return  # reached or passed the head, exit
                continue  # keep looping otherwise

            # ---------- fallback adaptive polling ----------------------- #
            blk, _, end_blk, idx_before, tempo = self._epoch_snapshot()
            if blk // tempo != idx_before:
                return  # already crossed into the next epoch

            left_blocks = end_blk - blk + 1  # inclusive
            sleep_s = min(
                float(BLOCKTIME),
                max(1.0, left_blocks * float(BLOCKTIME) / 4.0),
            )
            await asyncio.sleep(sleep_s)

    # --------------------------- overridden run --------------------------- #
    def run(self):  # noqa: D401
        bt.logging.info(f"EpochValidator starting at block: {self.block}")

        async def _loop():
            while not self.should_exit:
                blk, start_blk, end_blk, ep_idx, tempo = self._epoch_snapshot()
                into = blk - start_blk
                left = end_blk - blk + 1

                bt.logging.info(
                    f"[keep-alive] Block {blk:,} | Epoch {ep_idx} "
                    f"({into}/{tempo} blocks) – next epoch is in {left} blocks "
                    f"(~{left * BLOCKTIME:.0f}s)"
                )

                # ----- sleep until the next epoch head ----------------- #
                if not self.config.no_epoch:
                    await self._wait_for_next_head()

                # ----- epoch head reached ------------------------------ #
                head_time = datetime.utcnow().strftime("%H:%M:%S")

                # Snapshot epoch attributes for subclasses ------------- #
                blk, start_blk, end_blk, ep_idx, tempo = self._epoch_snapshot()
                self.epoch_start_block = start_blk
                self.epoch_end_block = end_blk
                self.epoch_index = ep_idx
                self.epoch_tempo = tempo

                bt.logging.info(
                    f"[epoch {ep_idx}] head at block {blk:,} "
                    f"({head_time} UTC) – calling forward()"
                )

                try:
                    # Force sync before the forward to be sure metagraph is updated
                    self.sync()
                    await self.concurrent_forward()

                except Exception as err:
                    bt.logging.error(f"forward() raised: {err}")
                    bt.logging.debug("".join(traceback.format_exception(err)))
                finally:
                    # -------- always sync wallet ----------------------- #
                    try:
                        self.sync()
                    except Exception as e:
                        bt.logging.warning(f"wallet sync failed: {e}")
                    self.step += 1

        try:
            self.loop.run_until_complete(_loop())
        except KeyboardInterrupt:
            if hasattr(self, "axon"):
                self.axon.stop()
            bt.logging.success("Validator stopped by keyboard interrupt.")
