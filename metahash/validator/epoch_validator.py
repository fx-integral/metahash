# ============================================================================ #
# metahash/validator/epoch_validator.py                                        #
# ============================================================================ #
"""Improved epoch‑paced validator base‑class.

Highlights
----------
* **Adaptive wait strategy** – wakes up at most every ``log_interval_s``
  seconds (20 min by default) and decreases the interval as the epoch
  head approaches, giving you a regular, tightening countdown.
* **Richer logging** – every wake‑up prints an at‑a‑glance banner with
  start/end block of the *current* epoch, its index, current block, and
  an estimate of the time left until the *next* epoch.

The public interface is unchanged, so subclasses can simply inherit from
``EpochValidatorNeuron`` and continue overriding ``forward()`` as usual.
"""
from __future__ import annotations

import asyncio
import traceback
from datetime import datetime, timedelta
from typing import Tuple, Optional

import bittensor as bt
from bittensor import BLOCKTIME  # seconds per block
from metahash.base.validator import BaseValidatorNeuron

###############################################################################


class EpochValidatorNeuron(BaseValidatorNeuron):
    """Drop‑in base‑class for validators that want epoch‑level pacing."""

    # --------------------------------------------------------------------- #
    # Configuration helpers
    # --------------------------------------------------------------------- #
    @property
    def _tempo(self) -> int:  # noqa: D401  (looks like a constant)
        """Subnet tempo in *blocks* (defaults to 360 if RPC returns 0)."""
        return self.subtensor.tempo(self.config.netuid) or 360

    @property
    def _log_interval(self) -> int:  # noqa: D401
        """Maximum seconds to sleep between progress logs (default 1 200 s).

        Can be overridden via *config.log_interval_s* CLI/INI option.
        """
        return getattr(self.config, "log_interval_s", 20 * 60)

    # ------------------------------------------------------------------ #
    # Epoch‑math helpers (pure functions, easy to unit‑test)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _epoch_bounds(blk: int, tempo: int) -> Tuple[int, int]:
        """Return (start_block, end_block) for *blk* given *tempo*."""
        start = blk - blk % tempo
        end = start + tempo - 1
        return start, end

    def _epoch_snapshot(self):
        """Lightweight synchronous snapshot of chain & epoch state."""
        blk = self.block  # synchronous property call
        tempo = self._tempo
        start, end = self._epoch_bounds(blk, tempo)
        return blk, start, end, blk // tempo, tempo

    # ------------------------------------------------------------------ #
    # Async utilities
    # ------------------------------------------------------------------ #
    async def _wait_for_next_head(self) -> None:
        """Sleep until the first block of the next epoch.

        Strategy
        --------
        1. Try the precise ``AsyncSubtensor.get_next_epoch_start_block`` RPC.
        2. If RPC unavailable, fall back to local modulo arithmetic.
        3. In either case sleep in *chunks* so that we print a progress log
           at least every ``self._log_interval`` seconds and progressively
           shrink the chunk as the head approaches (never longer than a
           quarter of the remaining time, nor shorter than one *BLOCKTIME*).
        """
        netuid = self.config.netuid

        while not self.should_exit:
            # ------------------ 1) precise RPC path --------------------- #
            next_head: Optional[int] = None
            try:
                next_head = self.subtensor.get_next_epoch_start_block(netuid)
            except Exception as e:  # noqa: BLE001  (log noise is ok here)
                bt.logging.warning(f"Failed to fetch next epoch head; » fallback: {e}")

            # If RPC succeeded *and* said head is ahead of our local block
            if next_head is not None and next_head > self.block:
                delta_blocks = next_head - self.block
                time_left_s = delta_blocks * float(BLOCKTIME)

                # Choose a sleep duration: min( quarter‑time, log_interval )
                sleep_s = min(
                    max(1.0, time_left_s / 4),  # adaptive – shrink as we near head
                    float(self._log_interval),  # but cap to keep regular logs
                )

                bt.logging.info(
                    f"[wait] epoch head in {delta_blocks} blocks "
                    f"(~{timedelta(seconds=int(time_left_s))}) – sleeping {int(sleep_s)} s"
                )

                await asyncio.sleep(sleep_s)

                # After waking up, re‑evaluate – if we've reached/passed head, exit
                if self.block >= next_head:
                    return

                continue  # otherwise loop again and keep waiting/logging

            # ------------------ 2) fallback adaptive polling ------------- #
            blk, start_blk, end_blk, idx_before, tempo = self._epoch_snapshot()

            # Already crossed the epoch boundary? ─────────────────────── #
            if blk // tempo != idx_before:
                return

            left_blocks = end_blk - blk + 1  # inclusive
            time_left_s = left_blocks * float(BLOCKTIME)

            sleep_s = min(
                max(1.0, time_left_s / 4),  # shrink as we near end
                float(self._log_interval),
            )

            bt.logging.info(
                f"[poll] epoch {idx_before} end in {left_blocks} blocks "
                f"(~{timedelta(seconds=int(time_left_s))}) – sleeping {int(sleep_s)} s"
            )

            await asyncio.sleep(sleep_s)

    # ------------------------------------------------------------------ #
    # Main run loop – unchanged API, richer telemetry
    # ------------------------------------------------------------------ #
    def run(self):  # noqa: D401
        bt.logging.info(f"EpochValidator starting at block: {self.block}")

        async def _loop():
            while not self.should_exit:
                # ---------------- current epoch snapshot --------------- #
                blk, start_blk, end_blk, ep_idx, tempo = self._epoch_snapshot()
                into = blk - start_blk  # blocks into current epoch
                left = end_blk - blk + 1  # blocks left (inclusive)

                bt.logging.info(
                    "[status] Block {blk:,} | Epoch {ep_idx} "
                    "[{into}/{tempo} blocks] – "
                    "next epoch in {left} blocks (~{eta})".format(
                        blk=blk,
                        ep_idx=ep_idx,
                        into=into,
                        tempo=tempo,
                        left=left,
                        eta=timedelta(seconds=left * BLOCKTIME),
                    )
                )

                # ------------- wait until next epoch head -------------- #
                if not self.config.no_epoch:
                    await self._wait_for_next_head()

                # ------------- epoch head reached ---------------------- #
                head_time = datetime.utcnow().strftime("%H:%M:%S")

                # snapshot *after* crossing the boundary so we store the new epoch
                blk, start_blk, end_blk, ep_idx, tempo = self._epoch_snapshot()
                self.epoch_start_block = start_blk
                self.epoch_end_block = end_blk
                self.epoch_index = ep_idx
                self.epoch_tempo = tempo

                bt.logging.info(
                    f"[epoch {ep_idx}] head at block {blk:,} (start={start_blk:,}, "
                    f"end={end_blk:,}) – {head_time} UTC – calling forward()"
                )

                try:
                    # Force sync before the forward to be sure metagraph is updated
                    self.sync()
                    await self.concurrent_forward()

                except Exception as err:  # noqa: BLE001
                    bt.logging.error(f"forward() raised: {err}")
                    bt.logging.debug("".join(traceback.format_exception(err)))
                finally:
                    # Always sync wallet to flush rewards/transactions
                    try:
                        self.sync()
                    except Exception as e:  # noqa: BLE001
                        bt.logging.warning(f"wallet sync failed: {e}")
                    self.step += 1

        try:
            self.loop.run_until_complete(_loop())
        except KeyboardInterrupt:
            if hasattr(self, "axon"):
                self.axon.stop()
            bt.logging.success("Validator stopped by keyboard interrupt.")
