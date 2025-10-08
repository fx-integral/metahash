# metahash/neurons/validator.py
from __future__ import annotations

import asyncio
import time  # <- necesario para time.sleep()
import bittensor as bt
from metahash.base.utils.logging import ColoredLogger as clog
from metahash import __version__
from metahash.utils.pretty_logs import pretty

# Config
from metahash.config import (
    STRATEGY_PATH,
    TESTING,
)

# Engines / state
from metahash.validator.state import StateStore
from metahash.validator.engines.commitments import CommitmentsEngine
from metahash.validator.engines.settlement import SettlementEngine
from metahash.validator.engines.auction import AuctionEngine
from metahash.validator.engines.clearing import ClearingEngine

# Base neuron
from metahash.validator.epoch_validator import EpochValidatorNeuron

# Strategy (unified)
from metahash.validator.strategy import Strategy


class Validator(EpochValidatorNeuron):
    def __init__(self, config=None):
        super().__init__(config=config)

        self.hotkey_ss58: str = self.wallet.hotkey.ss58_address
        self._async_subtensor: bt.AsyncSubtensor | None = None
        self._rpc_lock: asyncio.Lock = asyncio.Lock()

        self.state = StateStore()
        if getattr(self.config, "fresh", False):
            self.state.wipe()

        # Unified strategy: subnet weights from YAML
        self.strategy = Strategy(path=STRATEGY_PATH or "weights.yml")
        self.weights_bps: dict[int, int] = {}  # subnet_id -> bps (0..10_000)

        # Engines
        self.commitments = CommitmentsEngine(self, self.state)
        self.settlement = SettlementEngine(self, self.state)
        self.clearing = ClearingEngine(self, self.state)
        self.auction = AuctionEngine(self, self.state, self.weights_bps, clearer=self.clearing)

        # Population caches
        self.uid2axon: dict[int, object] = {}
        self.active_uids: list[int] = []
        self.active_axons: list[object] = []

        # Enhanced validator initialization logging
        pretty.banner(
            f"🏛️ Validator v{__version__} Initialized",
            " | ".join(
                filter(
                    None,
                    [
                        f"hotkey={self.hotkey_ss58}",
                        f"netuid={self.config.netuid}",
                        f"epoch(e)={getattr(self, 'epoch_index', 0)}",
                        "fresh" if getattr(self.config, "fresh", False) else "",
                        "Testing" if TESTING else "",
                    ],
                )
            ),
            style="bold magenta",
        )
        
        # Add validator configuration summary
        pretty.kv_panel(
            "⚙️ Validator Configuration",
            [
                ("version", __version__),
                ("hotkey", self.hotkey_ss58[:8] + "…"),
                ("netuid", self.config.netuid),
                ("fresh_start", getattr(self.config, "fresh", False)),
                ("testing_mode", TESTING),
                ("strategy_path", STRATEGY_PATH or "weights.yml"),
            ],
            style="bold magenta",
        )

    # ---------------------- AsyncSubtensor helpers ----------------------
    async def _stxn(self) -> bt.AsyncSubtensor:
        if self._async_subtensor is None:
            self._async_subtensor = await self._new_async_subtensor()
        return self._async_subtensor

    async def _new_async_subtensor(self) -> bt.AsyncSubtensor:
        """
        Create AsyncSubtensor compatible with multiple bittensor versions.
        Avoid passing unsupported kwargs like `chain_endpoint`.
        """
        # Minimal constructor; older versions only accept 'network'
        stxn = bt.AsyncSubtensor(network=self.config.subtensor.network)
        # Initialize if method exists and is coroutine
        init = getattr(stxn, "initialize", None)
        if callable(init):
            maybe_coro = init()
            if asyncio.iscoroutine(maybe_coro):
                await maybe_coro
        return stxn

    # ---------------------- Epoch alignment / population ----------------------
    async def _maybe_call_async(self, obj, method_name: str, *args, **kwargs):
        fn = getattr(obj, method_name, None)
        if callable(fn):
            res = fn(*args, **kwargs)
            if asyncio.iscoroutine(res):
                return await res
            return res

    async def _refresh_chain_and_population(self) -> None:
        await self._stxn()

        # Sync metagraph under a lock to avoid concurrent RPC races
        async with self._rpc_lock:
            try:
                self.metagraph.sync(subtensor=self.subtensor)
            except TypeError:
                self.metagraph.sync(self.subtensor)
            except Exception:
                pass  # tolerant; engines handle empty population

        try:
            axons = getattr(self.metagraph, "axons", None)
            n = getattr(self.metagraph, "n", None)
            if n is None:
                n = len(axons) if isinstance(axons, (list, tuple)) else 0

            if isinstance(axons, (list, tuple)) and n:
                def _is_active(ax):
                    if ax is None:
                        return False
                    flags = []
                    for flag in ("is_serving", "is_active", "active", "serving"):
                        v = getattr(ax, flag, None)
                        if isinstance(v, bool):
                            flags.append(v)
                    return any(flags) if flags else False

                self.uid2axon = {uid: axons[uid] for uid in range(min(n, len(axons))) if axons[uid] is not None}
                self.active_uids = [uid for uid, ax in self.uid2axon.items() if _is_active(ax)]
                self.active_axons = [self.uid2axon[uid] for uid in self.active_uids]
            else:
                self.uid2axon, self.active_uids, self.active_axons = {}, [], []
        except Exception:
            self.uid2axon, self.active_uids, self.active_axons = {}, [], []

        # Let engines see fresh metagraph & current (possibly stale) weights placeholder
        for engine in (self.auction, self.clearing, self.settlement, self.commitments):
            if hasattr(engine, "metagraph"):
                engine.metagraph = self.metagraph
            if hasattr(engine, "weights_bps"):
                engine.weights_bps = self.weights_bps
            await self._maybe_call_async(engine, "on_metagraph_update", new=self.metagraph, old=None)

        # NOTE: epoch override handled by EpochValidatorNeuron; do not duplicate here.

    # ---------------------- Dynamic weights each epoch ----------------------
    def _recompute_weights(self) -> None:
        """
        Recompute subnet weights before we run the epoch logic.
        Produces a dict: {subnet_id: bps}.
        """
        try:
            bps_map = self.strategy.compute_weights_bps(
                netuid=self.config.netuid,
                metagraph=self.metagraph,
                active_uids=self.active_uids,
            )
            # Ensure int bps in range
            clean: dict[int, int] = {}
            for sid_raw, bp in (bps_map or {}).items():
                try:
                    sid = int(sid_raw)
                    ival = int(bp)
                except Exception:
                    continue
                clean[sid] = max(0, min(10_000, ival))

            self.weights_bps = clean

            # Propagate to engines that cache reference
            for engine in (self.auction, self.clearing, self.settlement, self.commitments):
                if hasattr(engine, "weights_bps"):
                    engine.weights_bps = self.weights_bps

            nonzero = sum(1 for x in self.weights_bps.values() if x > 0)
            total_bps = sum(self.weights_bps.values())
            pretty.kv_panel(
                "⚖️ Subnet Weights Updated",
                [
                    ("nonzero_subnets", nonzero),
                    ("total_bps", total_bps),
                    ("avg_weight", f"{total_bps / max(1, len(self.weights_bps)):.0f} bps" if self.weights_bps else "0 bps"),
                    ("strategy", "YAML-based"),
                ],
                style="bold cyan",
            )
        except Exception as e:
            pretty.log(f"[yellow]Weights recompute failed:[/yellow] {e}")

    # ---------------------- Epoch routine ----------------------
    async def forward(self):
        await self._stxn()
        await self._refresh_chain_and_population()

        # recompute subnet weights just-in-time
        self._recompute_weights()

        e = int(getattr(self, "epoch_index", 0))
        pretty.banner(
            f"🔄 Epoch {e} Processing",
            f"head_block={self.block} | start={self.epoch_start_block} | end={self.epoch_end_block}\n"
            f"• PHASE 1/3: settle e−2 → {e-2} (scan pe={e-1})\n"
            f"• PHASE 2/3: auction & clear e={e} (miners pay in e+1={e+1})\n"
            f"• PHASE 3/3: publish commitment for e−1={e-1} (strict; post-settlement)\n"
            f"• Weights applied from e in e+2={e+2}",
            style="bold white",
        )

        # Settlement first
        await self.settlement.settle_and_set_weights_all_masters(epoch_to_settle=e - 2)

        # Only master broadcasts/clears
        if not self.auction._is_master_now() and getattr(self.auction, "_not_master_log_epoch", None) != e:
            pretty.kv_panel(
                "👑 Master Status",
                [
                    ("status", "❌ Not Master"),
                    ("epoch", e),
                    ("action", "Skipping broadcast/clear"),
                    ("reason", "Insufficient stake or not in treasury"),
                ],
                style="bold yellow",
            )
            self.auction._not_master_log_epoch = e

        # Broadcast AuctionStart (best-effort)
        try:
            await self.auction.broadcast_auction_start()
        except asyncio.CancelledError as ce:
            # do not return; continue the epoch so commitments still have a chance to publish
            pretty.log(f"[yellow]AuctionStart cancelled by RPC: {ce}. Will retry next epoch.[/yellow]")
        except Exception as exc:
            pretty.log(f"[yellow]AuctionStart failed: {exc}[/yellow]")

        # Housekeeping
        self.auction.cleanup_old_epoch_books(before_epoch=e - 2)

        # Publish commitments (strict)
        try:
            await self.commitments.publish_exact(epoch_cleared=e - 1, max_retries=6)
        except asyncio.CancelledError as ce:
            pretty.log(f"[yellow]Publish e−1 cancelled by RPC: {ce}[/yellow]")
        except Exception as exc:
            pretty.log(f"[yellow]Publish e−1 failed: {exc}[/yellow]")


if __name__ == "__main__":
    from metahash.bittensor_config import config
    with Validator(config=config(role="validator")) as v:
        # Mantener vivo el proceso con un latido cada 120s
        while True:
            clog.info("Validator running…", color="gray")
            time.sleep(120)
