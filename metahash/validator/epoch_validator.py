# ============================================================================ #
# metahash/validator/epoch_validator.py                                        #
# ============================================================================ #

from __future__ import annotations
import asyncio, traceback
from datetime import datetime
from typing import Optional, Tuple

import bittensor as bt
from bittensor import BLOCKTIME
from metahash.base.validator import BaseValidatorNeuron


class EpochValidatorNeuron(BaseValidatorNeuron):
    """
    Drop-in base-class for validators that want epoch-level pacing + progress logs.
    """

    # ------------------------------ init ---------------------------------- #
    def __init__(self, *args, log_interval_s: int = 1_200, **kwargs):
        """
        Args
        ----
        log_interval_s : int
            Max seconds to wait between status prints when the epoch head is
            far away (default = 20 min).  Interval shrinks automatically as we
            approach the head.
        """
        super().__init__(*args, **kwargs)

        # Epoch-specific state (filled in each head)
        self.epoch_start_block: Optional[int] = None
        self.epoch_end_block:   Optional[int] = None
        self.epoch_index:       Optional[int] = None
        self.epoch_tempo:       Optional[int] = None

        # ───── new: always hold a real number here ───── #
        self._log_interval: float = float(max(1, log_interval_s))

    # --------------------------- helpers ---------------------------------- #
    def _tempo(self) -> int:
        """Subnet tempo or default 360."""
        return self.subtensor.tempo(self.config.netuid) or 360

    @staticmethod
    def _epoch_bounds(blk: int, tempo: int) -> Tuple[int, int]:
        start = blk - blk % tempo
        return start, start + tempo - 1

    def _epoch_snapshot(self):
        blk = self.block
        tempo = self._tempo()
        start, end = self._epoch_bounds(blk, tempo)
        return blk, start, end, blk // tempo, tempo

    # ------------------------------------------------------------------ #
    async def _wait_for_next_head(self) -> None:
        """
        Sleep until the first block of the next epoch, emitting progress logs
        at least every `self._log_interval` seconds and more frequently as we
        get close.
        """
        netuid = self.config.netuid

        while not self.should_exit:
            # ---------- precise RPC path -------------------------------- #
            next_head: Optional[int] = None
            try:
                next_head = self.subtensor.get_next_epoch_start_block(netuid)
            except Exception as e:
                bt.logging.warning(f"Failed RPC → fallback polling: {e}")

            if next_head and next_head > self.block:
                delta_blocks = next_head - self.block
                # cap long sleeps so we keep printing “[status] …”
                sleep_s = min(
                    delta_blocks * float(BLOCKTIME) * 0.9,
                    self._log_interval,
                )
                await asyncio.sleep(max(1.0, sleep_s))
                if self.block >= next_head:       # head (or later) reached
                    return
                continue

            # ---------- fallback adaptive polling ----------------------- #
            blk, _, end_blk, idx_before, tempo = self._epoch_snapshot()
            if blk // tempo != idx_before:        # already crossed
                return

            left_blocks = end_blk - blk + 1
            # shrink interval as we close in
            sleep_s = min(
                self._log_interval,
                max(1.0, left_blocks * float(BLOCKTIME) / 4.0),
            )
            await asyncio.sleep(sleep_s)

    # --------------------------- overridden run --------------------------- #
    def run(self):                                   # noqa: D401
        bt.logging.info(f"EpochValidator starting at block {self.block:,}")

        async def _loop():
            while not self.should_exit:
                blk, start_blk, end_blk, ep_idx, tempo = self._epoch_snapshot()
                into  = blk - start_blk
                left  = end_blk - blk + 1
                eta   = left * BLOCKTIME

                bt.logging.info(
                    f"[status] Block {blk:,} | Epoch {ep_idx} "
                    f"[{into}/{tempo} blocks] – next epoch in {left} blocks "
                    f"(~{eta//3600:.0f}:{(eta%3600)//60:02.0f}:{eta%60:02.0f})"
                )

                # ----- sleep until the next epoch head ----------------- #
                if not self.config.no_epoch:
                    await self._wait_for_next_head()

                # ----- epoch head reached ------------------------------ #
                head_time = datetime.utcnow().strftime("%H:%M:%S")

                # Snapshot epoch attributes for subclasses ------------- #
                blk, start_blk, end_blk, ep_idx, tempo = self._epoch_snapshot()
                self.epoch_start_block = start_blk
                self.epoch_end_block   = end_blk
                self.epoch_index       = ep_idx
                self.epoch_tempo       = tempo

                bt.logging.info(
                    f"[epoch {ep_idx}] head at block {blk:,} "
                    f"({head_time} UTC) – calling forward()"
                )

                try:
                    self.sync()            # make sure metagraph is fresh
                    await self.concurrent_forward()
                except Exception as err:
                    bt.logging.error(f"forward() raised: {err}")
                    bt.logging.debug("".join(traceback.format_exception(err)))
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
