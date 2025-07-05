# metahash/validator/epoch_validator.py
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import asyncio
import traceback
from datetime import datetime
from typing import Optional, Tuple

import bittensor as bt
from bittensor import BLOCKTIME           # 12 s on Finney

from metahash.base.validator import BaseValidatorNeuron


class EpochValidatorNeuron(BaseValidatorNeuron):
    """
    Simple validator base‑class with:

      • automatic epoch‑length detection (bug‑aware)
      • frequent progress banners (every 1–2 blocks)
      • no long sleeps ⇒ never misses a rollover
    """

    # ------------------------------------------------------------------ #
    def __init__(self, *args, log_interval_blocks: int = 2, **kwargs):
        """
        Args:
            log_interval_blocks: how many blocks to wait between
                                 progress banners while far from the
                                 epoch head (default: 2 blocks ≈ 24 s).
        """
        super().__init__(*args, **kwargs)
        self._log_interval_blocks = max(1, int(log_interval_blocks))
        self._epoch_len: Optional[int] = None        # recalculated each epoch

    # ----------------------- helpers (sync) ---------------------------- #
    def _discover_epoch_length(self) -> int:
        """
        Returns the *real* epoch length, compensating for the historical
        off‑by‑one bug (tempo+1).  Re‑evaluated at every epoch head.
        """
        tempo = self.subtensor.tempo(self.config.netuid) or 360

        try:
            head      = self.subtensor.get_current_block()
            next_head = self.subtensor.get_next_epoch_start_block(self.config.netuid)
            if next_head is None:
                raise ValueError("RPC returned None")

            derived = next_head - (head - head % tempo)
            length  = derived if derived in (tempo, tempo + 1) else tempo + 1
        except Exception as e:
            bt.logging.warning(f"[epoch] RPC error while probing length: {e}")
            length = tempo + 1                       # safest guess

        if self._epoch_len != length:
            bt.logging.info(f"[epoch] detected length = {length}")
        self._epoch_len = length
        return length

    def _epoch_snapshot(self) -> Tuple[int, int, int, int, int]:
        """
        Returns tuple: (head, epoch_start, epoch_end, epoch_index, epoch_len)
        using the *current* epoch length.
        """
        blk  = self.subtensor.get_current_block()
        ep_l = self._epoch_len or self._discover_epoch_length()

        start = blk - (blk % ep_l)
        end   = start + ep_l - 1
        idx   = blk // ep_l
        return blk, start, end, idx, ep_l

    # ----------------------- async wait‑loop --------------------------- #
    async def _wait_for_next_head(self):
        """
        Sleep in short chunks until the first block of the next epoch,
        emitting progress banners frequently.
        """
        netuid = self.config.netuid

        while not self.should_exit:
            try:
                next_head = self.subtensor.get_next_epoch_start_block(netuid)
            except Exception as e:
                bt.logging.warning(f"[epoch] RPC error: {e}")
                next_head = None

            blk = self.subtensor.get_current_block()

            # epoch head reached?
            if next_head and blk >= next_head:
                return

            # compute how many blocks remain / decide sleep size
            if next_head:
                remain = max(0, next_head - blk - 1)
                sleep_blocks = (
                    2 if remain >= 4 else 1           # 2 blocks far away, 1 block when close
                )
            else:
                remain = -1
                sleep_blocks = 1                      # conservative fallback

            eta_s = remain * BLOCKTIME
            bt.logging.info(
                f"[status] Block {blk:,} | {remain if remain>=0 else '?'} "
                f"blocks → next epoch (~{eta_s//60:.0f} m {eta_s%60:02.0f} s)"
            )

            await asyncio.sleep(sleep_blocks * BLOCKTIME * 0.98)  # small safety margin

    # ----------------------------- run -------------------------------- #
    def run(self):  # noqa: D401
        bt.logging.info(
            f"EpochValidator starting at block {self.block:,} (netuid {self.config.netuid})"
        )

        async def _loop():
            while not self.should_exit:
                # snapshot + banner
                blk, start, end, idx, ep_len = self._epoch_snapshot()
                into  = blk - start
                left  = end - blk + 1
                eta_s = left * BLOCKTIME

                bt.logging.info(
                    f"[status] Block {blk:,} | Epoch {idx} "
                    f"[{into}/{ep_len} blocks] – next epoch in {left} "
                    f"blocks (~{eta_s//60:.0f} m {eta_s%60:02.0f} s)"
                )

                # wait for epoch rollover
                if not self.config.no_epoch:
                    await self._wait_for_next_head()

                # reached the new epoch head
                blk2, start2, end2, idx2, ep_len2 = self._epoch_snapshot()
                head_time = datetime.utcnow().strftime("%H:%M:%S")

                # expose to subclasses
                self.epoch_start_block = start2
                self.epoch_end_block   = end2
                self.epoch_index       = idx2
                self.epoch_tempo       = ep_len2

                bt.logging.success(
                    f"[epoch {idx2}] head at block {blk2:,} ({head_time} UTC) – len={ep_len2}"
                )

                # *** validator business logic ***
                try:
                    self.sync()                  # refresh wallet / weights
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
