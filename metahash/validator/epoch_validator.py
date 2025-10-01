# metahash/validator/epoch_validator.py
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Optional, Tuple, Any

import bittensor as bt
from bittensor import BLOCKTIME

from metahash.base.validator import BaseValidatorNeuron
from metahash.config import EPOCH_LENGTH_OVERRIDE, TESTING
from metahash.validator.strategy import Strategy  # unified


class EpochValidatorNeuron(BaseValidatorNeuron):
    """
    Validator base-class with robust epoch rollover handling.

    This version honors `EPOCH_LENGTH_OVERRIDE` and refreshes Strategy weights
    immediately before each forward() invocation.

    Note: Strategy here is used for operator visibility; subnet weights are
    also recomputed in the concrete Validator and injected into engines.
    """

    def __init__(self, *args, log_interval_blocks: int = 2, **kwargs):
        super().__init__(*args, **kwargs)
        self._log_interval_blocks = max(1, int(log_interval_blocks))
        self._epoch_len: Optional[int] = None
        self.epoch_end_block: Optional[int] = None
        self._override_active: bool = False
        self._bootstrapped: bool = False

        # Strategy wiring
        strategy_path = getattr(self.config, "strategy_path", "weights.yml")
        strategy_algo_path = getattr(self.config, "strategy_algo_path", None)
        self.strategy = Strategy(path=strategy_path, algorithm_path=strategy_algo_path)
        self.current_strategy_out: Any = None  # can be dict (subnet bps) or list

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

    # ----------------------- strategy refresh ------------------------- #
    def _refresh_strategy_before_forward(self):
        """
        Recompute strategy output for operator visibility.
        Supports dict (subnet bps) or list outputs.
        """
        try:
            out = self.strategy.compute_weights_bps(
                netuid=self.config.netuid,
                metagraph=self.metagraph,
                active_uids=None,
            )
            self.current_strategy_out = out
            # tolerant logging
            if isinstance(out, dict):
                nonzero = sum(1 for v in out.values() if int(v) > 0)
                total = sum(int(v) for v in out.values())
                bt.logging.info(f"[strategy] refreshed subnet weights (entries={len(out)} nonzero={nonzero} sum_bps={total})")
            else:
                nz = sum(1 for v in (out or []) if v)
                bt.logging.info(f"[strategy] refreshed weights (len={len(out or [])} nonzeros={nz})")
        except Exception as e:
            bt.logging.warning(f"[strategy] refresh failed: {e}")

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

                # TESTING bootstrap
                if TESTING and not self._bootstrapped:
                    self._apply_epoch_state(blk, start, end, idx, ep_len)
                    self._bootstrapped = True

                    try:
                        self.sync()
                        self._refresh_strategy_before_forward()
                        await self.concurrent_forward()
                    except Exception as err:
                        bt.logging.error(f"bootstrap forward() raised: {err}")
                    finally:
                        try:
                            self.sync()
                        except Exception as e:
                            bt.logging.warning(f"wallet sync failed: {e}")
                        self.step += 1

                    if self.config.no_epoch:
                        pass
                    else:
                        await self._wait_for_next_head()

                else:
                    if not self.config.no_epoch:
                        await self._wait_for_next_head()

                # head snapshot
                self._epoch_len = None
                blk2, start2, end2, idx2, ep_len2 = self._epoch_snapshot()
                self._apply_epoch_state(blk2, start2, end2, idx2, ep_len2)

                try:
                    self.sync()
                    self._refresh_strategy_before_forward()
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
