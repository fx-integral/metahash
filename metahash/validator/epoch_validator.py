# metahash/validator/epoch_validator.py
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations
import asyncio, traceback
from datetime import datetime
from typing import Optional, Tuple

import bittensor as bt
from bittensor import BLOCKTIME
from metahash.base.validator import BaseValidatorNeuron


class EpochValidatorNeuron(BaseValidatorNeuron):
    """
    Validator base-class with:
      • automatic detection of the real (bug-compensated) epoch length
      • sanity banners at every rollover
      • drop-in replacement for the original class
    """

    # --------------------------------------------------------------------- #
    def __init__(self, *args, log_interval_s: int = 1_200, **kwargs):
        super().__init__(*args, **kwargs)
        self._log_interval: float = float(max(1, log_interval_s))
        self._epoch_len: Optional[int] = None        # cached once per process

    # ----------------------- helpers ( **sync** ) ------------------------- #
    def _discover_epoch_length(self) -> int:
        """
        Detects whether the node still has the off-by-one bug.

        Returns:
            tempo            (e.g. 360)  if pallet already fixed
            tempo + 1        (e.g. 361)  otherwise
        """
        if self._epoch_len is not None:
            return self._epoch_len

        tempo      = self.subtensor.tempo(self.config.netuid) or 360
        head       = self.subtensor.get_current_block()
        next_head  = self.subtensor.get_next_epoch_start_block(self.config.netuid)
        bslu       = self.subtensor.blocks_since_last_step(self.config.netuid)

        # Fallbacks: if RPCs mis-behave just assume bug is present
        if next_head is None or bslu is None:
            length = tempo + 1
        else:
            length = (next_head - head) + bslu

        if length not in (tempo, tempo + 1):
            bt.logging.warning(
                f"[epoch] Unexpected derived length {length}; "
                f"using advertised tempo {tempo}"
            )
            length = tempo

        self._epoch_len = length
        return length

    def _epoch_snapshot(self) -> Tuple[int, int, int, int, int]:
        """
        Returns (head, start, end, index, length) using the *real* epoch length.
        """
        blk       = self.subtensor.get_current_block()
        ep_len    = self._discover_epoch_length()
        start_blk = blk - (blk % ep_len)
        end_blk   = start_blk + ep_len - 1
        ep_idx    = blk // ep_len
        return blk, start_blk, end_blk, ep_idx, ep_len

    # ----------------------- async wait-loop ------------------------------ #
    async def _wait_for_next_head(self):
        """Sleep until the first block of the next epoch, logging progress."""
        netuid = self.config.netuid
        while not self.should_exit:
            try:
                next_head = self.subtensor.get_next_epoch_start_block(netuid)
            except Exception as e:
                bt.logging.warning(f"[epoch] RPC error: {e}")
                next_head = None

            blk = self.subtensor.get_current_block()
            if next_head and blk >= next_head:
                return

            sleep_s = (
                max(1.0, (next_head - blk - 1) * BLOCKTIME * 0.9)
                if next_head
                else min(
                    self._log_interval,
                    max(1.0, self._discover_epoch_length() * BLOCKTIME / 4),
                )
            )
            await asyncio.sleep(sleep_s)

    # --------------------------- main run() ------------------------------- #
    def run(self):  # noqa: D401
        bt.logging.info(
            f"EpochValidator starting at block {self.block:,} "
            f"(netuid {self.config.netuid})"
        )

        async def _loop():
            while not self.should_exit:
                blk, start, end, idx, ep_len = self._epoch_snapshot()
                into  = blk - start
                left  = end - blk + 1
                eta_s = left * BLOCKTIME

                bt.logging.info(
                    f"[status] Block {blk:,} | Epoch {idx} "
                    f"[{into}/{ep_len} blocks] – next epoch in {left} "
                    f"blocks (~{eta_s//3600:.0f}:{(eta_s%3600)//60:02.0f}:{eta_s%60:02.0f})"
                )

                # wait for rollover
                if not self.config.no_epoch:
                    await self._wait_for_next_head()

                # epoch head reached
                blk2, start2, end2, idx2, ep_len2 = self._epoch_snapshot()
                head_time = datetime.utcnow().strftime("%H:%M:%S")

                # expose to subclasses
                self.epoch_start_block = start2
                self.epoch_end_block   = end2
                self.epoch_index       = idx2
                self.epoch_tempo       = ep_len2

                bt.logging.success(
                    f"[epoch {idx2}] head at block {blk2:,} "
                    f"({head_time} UTC) – len={ep_len2} "
                    f"({'tempo+1' if ep_len2 != (self.subtensor.tempo(self.config.netuid) or 360) else 'tempo'})"
                )

                try:
                    self.sync()                    # refresh weights / wallet
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
