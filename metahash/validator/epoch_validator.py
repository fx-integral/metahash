# metahash/validator/epoch_validator.py
# ──────────────────────────────────────────────────────────────────────────────
from __future__ import annotations
import asyncio
import math
import traceback
from datetime import datetime
from typing import Optional, Tuple

import bittensor as bt
from bittensor import BLOCKTIME
from metahash.base.validator import BaseValidatorNeuron


class EpochValidatorNeuron(BaseValidatorNeuron):
    """
    Base-class for validators that want epoch-level pacing + rich progress logs.

    Differences from the original:
    • Detects the real (bug-compensated) epoch length at runtime.
    • Emits sanity-check logs so you can see if the chain behaviour changes.
    """

    # --------------------------------------------------------------------- #
    def __init__(self, *args, log_interval_s: int = 1_200, **kwargs):
        super().__init__(*args, **kwargs)
        self._log_interval: float = float(max(1, log_interval_s))
        # cached in _epoch_length() to avoid one RPC every loop-tick
        self._epoch_len: Optional[int] = None

    # ----------------------------- internals ----------------------------- #
    async def _epoch_length(self) -> int:
        """
        Returns the *actual* epoch length the chain is using right now.

        If the node still has the off-by-one bug, length = tempo + 1.
        If it has been patched, length = tempo.
        """
        if self._epoch_len is not None:
            return self._epoch_len

        tempo = await self.subtensor.tempo(self.config.netuid) or 360

        # Ask chain where the *next* epoch starts; this helper already
        # compensates for the pallet bug internally.
        head = await self.subtensor.get_current_block()
        next_head = await self.subtensor.get_next_epoch_start_block(
            self.config.netuid
        )
        blocks_since_last = await self.subtensor.blocks_since_last_step(
            self.config.netuid
        )

        if next_head is None or blocks_since_last is None:
            # fall back to +1; extremely unlikely unless node is borked
            length = tempo + 1
        else:
            # distance to next rollover + how far we've already advanced
            length = (next_head - head) + blocks_since_last

        # sanity-guard: make sure we discovered either tempo or tempo+1
        if length not in (tempo, tempo + 1):
            bt.logging.warning(
                f"[epoch] ⛑  Unexpected epoch length {length} "
                f"(tempo={tempo}, bug +1?) – falling back to {tempo}"
            )
            length = tempo

        self._epoch_len = length
        return length

    async def _epoch_snapshot(self) -> Tuple[int, int, int, int, int]:
        """
        Returns (head_blk, start_blk, end_blk, epoch_index, epoch_len)
        with *run-time-detected* epoch length.
        """
        blk = await self.subtensor.get_current_block()
        epoch_len = await self._epoch_length()
        start_blk = blk - (blk % epoch_len)
        end_blk = start_blk + epoch_len - 1
        epoch_idx = blk // epoch_len
        return blk, start_blk, end_blk, epoch_idx, epoch_len

    async def _wait_for_next_head(self):
        """
        Sleep until block == next epoch head, printing progress logs.
        """
        netuid = self.config.netuid
        while not self.should_exit:
            try:
                next_head = await self.subtensor.get_next_epoch_start_block(netuid)
            except Exception as e:
                bt.logging.warning(f"[epoch] RPC failed: {e}")
                next_head = None

            blk = await self.subtensor.get_current_block()
            if next_head and blk >= next_head:
                return

            if next_head:
                sleep_s = max(1.0, (next_head - blk - 1) * BLOCKTIME * 0.9)
            else:
                # fallback: short adaptive poll
                sleep_s = min(
                    self._log_interval,
                    max(1.0, (await self._epoch_length() / 4) * BLOCKTIME),
                )
            await asyncio.sleep(sleep_s)

    # ----------------------------- main loop ----------------------------- #
    def run(self):  # noqa: D401  – top-level run; synchronous wrapper
        bt.logging.info(
            f"EpochValidator starting at block {self.block:,} "
            f"(netuid {self.config.netuid})"
        )

        async def _loop():
            while not self.should_exit:
                blk, start_blk, end_blk, ep_idx, ep_len = await self._epoch_snapshot()
                into = blk - start_blk
                left = end_blk - blk + 1
                eta_s = left * BLOCKTIME

                bt.logging.info(
                    f"[status] Block {blk:,} | Epoch {ep_idx} "
                    f"[{into}/{ep_len} blocks] – next epoch in {left} "
                    f"blocks (~{eta_s//3600:.0f}:{(eta_s%3600)//60:02.0f}:{eta_s%60:02.0f})"
                )

                # ---- wait until head -------------------------------------------------
                if not self.config.no_epoch:
                    await self._wait_for_next_head()

                # ---- epoch rollover ---------------------------------------------------
                blk2, start2, end2, ep_idx2, ep_len2 = await self._epoch_snapshot()
                assert ep_idx2 == ep_idx + 1, "epoch drift check failed"
                head_time = datetime.utcnow().strftime("%H:%M:%S")

                self.epoch_start_block = start2
                self.epoch_end_block = end2
                self.epoch_index = ep_idx2
                self.epoch_tempo = ep_len2

                bt.logging.success(
                    f"[epoch {ep_idx2}] head at block {blk2:,} "
                    f"({head_time} UTC) – len={ep_len2} "
                    f"({'tempo+1' if ep_len2 != (await self.subtensor.tempo(self.config.netuid)) else 'tempo'})"
                )

                # ---- user-defined behaviour ------------------------------------------
                try:
                    self.sync()  # refresh metagraph
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
