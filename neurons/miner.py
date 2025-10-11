#!/usr/bin/env python3
# neurons/miner.py

import asyncio
from pathlib import Path
from typing import Any

import bittensor as bt
from bittensor import Synapse

from metahash.base.miner import BaseMinerNeuron
from metahash.base.utils.logging import ColoredLogger as clog
from metahash.protocol import AuctionStartSynapse, WinSynapse
from metahash.treasuries import VALIDATOR_TREASURIES
from metahash.utils.pretty_logs import pretty
from metahash.utils.wallet_utils import unlock_wallet
from metahash.miner.logging import (
    MinerPhase, LogLevel, miner_logger, 
    init_banner, log_init, log_auction, log_commitments, log_settlement
)

# Compact components
from metahash.miner.state import StateStore
from metahash.miner.runtime import Runtime
from metahash.miner.payments import Payments
from metahash.miner.autosell import AutoSellManager, AutoSellConfig
from metahash.miner.bidding_control import BiddingController, BiddingControlConfig


def _strip_internals_inplace(syn: Any) -> None:
    """
    Remove transient / non-serializable attributes that may be attached by the
    inbound call context (e.g., dendrite with a `uids` slice). This prevents
    Axon/Pydantic from walking into a Python `slice` and raising:
      TypeError: unhashable type: 'slice'
    """
    def _recursive_clean(obj, visited=None, path=""):
        if visited is None:
            visited = set()

        # Handle None objects early
        if obj is None:
            return

        # Prevent infinite recursion
        obj_id = id(obj)
        if obj_id in visited:
            return
        visited.add(obj_id)

        # Handle slice objects directly
        if isinstance(obj, slice):
            return None

        # Handle dictionaries
        if isinstance(obj, dict):
            for key, value in list(obj.items()):
                if isinstance(value, slice):
                    obj[key] = None
                elif value is not None and (hasattr(value, '__dict__') or isinstance(value, (dict, list, tuple))):
                    _recursive_clean(value, visited, f"{path}[{key}]")

        # Handle lists and tuples
        elif isinstance(obj, (list, tuple)):
            for i, item in enumerate(obj):
                if isinstance(item, slice):
                    if isinstance(obj, list):
                        obj[i] = None
                elif item is not None and (hasattr(item, '__dict__') or isinstance(item, (dict, list, tuple))):
                    _recursive_clean(item, visited, f"{path}[{i}]")

        # Handle objects with attributes
        elif hasattr(obj, '__dict__'):
            for attr_name in list(obj.__dict__.keys()):
                try:
                    attr_value = getattr(obj, attr_name)
                    if isinstance(attr_value, slice):
                        setattr(obj, attr_name, None)
                    elif attr_value is not None and (hasattr(attr_value, '__dict__') or isinstance(attr_value, (dict, list, tuple))):
                        _recursive_clean(attr_value, visited, f"{path}.{attr_name}")
                except Exception:
                    pass
    
    # First, recursively clean any nested slice objects
    _recursive_clean(syn)
    
    # Most important: inbound context
    for attr in ("dendrite", "_dendrite"):
        if hasattr(syn, attr):
            try:
                setattr(syn, attr, None)
            except Exception:
                try:
                    delattr(syn, attr)
                except Exception:
                    pass

    # Extra caution: sometimes frameworks attach other transient handles
    for attr in ("axon", "_axon", "server", "_server", "context", "_context"):
        if hasattr(syn, attr):
            try:
                setattr(syn, attr, None)
            except Exception:
                try:
                    delattr(syn, attr)
                except Exception:
                    pass


class Miner(BaseMinerNeuron):
    """
    Thin orchestrator:
      - Per-coldkey state scoping (safe fallback name if dir fails)
      - Creates Runtime (auction + chain helpers) and Payments (background loop + workers)
      - Delegates protocol handlers
    """

    def __init__(self, config=None):
        super().__init__(config=config)

        # Initialize phase-aware logging
        init_banner("Miner Initialization Started", "Setting up miner components and configuration")
        
        # Initialize shared AsyncSubtensor with proper network configuration
        self._initialize_shared_async_subtensor()
        
        # Wallet unlock (best-effort)
        log_init(LogLevel.MEDIUM, "Unlocking wallet", "wallet", {"hotkey": getattr(self.wallet.hotkey, "ss58_address", "unknown")})
        unlock_wallet(wallet=self.wallet)

        # ---------------------- Per-coldkey state directory ----------------------
        log_init(LogLevel.MEDIUM, "Setting up per-coldkey state directory", "state")
        self._coldkey_ss58: str = getattr(getattr(self.wallet, "coldkey", None), "ss58_address", "") or "unknown_coldkey"
        desired_state_dir: Path = Path("miner_state") / self._coldkey_ss58
        try:
            desired_state_dir.mkdir(parents=True, exist_ok=True)
            state_path = desired_state_dir / "miner_state.json"
            self._state_dir = desired_state_dir
        except Exception as e:
            self._state_dir = Path(".")
            state_path = Path(f"miner_state_{self._coldkey_ss58}.json")
            log_init(LogLevel.HIGH, "State directory creation failed, using fallback", "state", {"error": str(e), "fallback_path": str(state_path)})

        # ---------------------- StateStore ----------------------
        log_init(LogLevel.MEDIUM, "Initializing state store", "state", {"state_file": str(state_path)})
        self.state = StateStore(state_path)

        # Optional fresh start
        if getattr(self.config, "fresh", False):
            log_init(LogLevel.HIGH, "Fresh start requested - clearing local state", "state", {"coldkey": self._coldkey_ss58})
            self.state.wipe()
            log_init(LogLevel.MEDIUM, "Local state cleared successfully", "state")

        self.state.load()
        self.state.treasuries = dict(VALIDATOR_TREASURIES)  # re-pin allowlist

        # ---------------------- Bidding Controller (needed for runtime) ----------------------
        log_init(LogLevel.MEDIUM, "Initializing bidding controller", "bidding_control")
        self.bidding_control_config = self._build_bidding_control_config()
        self.bidding_controller = BiddingController(
            config=self.bidding_control_config,
            state_store=self.state
        )

        # ---------------------- Runtime & Payments ----------------------
        log_init(LogLevel.MEDIUM, "Initializing runtime component", "runtime")
        self.runtime = Runtime(
            config=self.config,
            wallet=self.wallet,
            metagraph=self.metagraph,
            state=self.state,
            shared_async_subtensor=self.shared_async_subtensor,
            bidding_controller=self.bidding_controller,
        )

        # Initialize separate AsyncSubtensor for payments
        self._initialize_payments_async_subtensor()
        
        log_init(LogLevel.MEDIUM, "Initializing payments component", "payments")
        self.payments = Payments(
            config=self.config,
            wallet=self.wallet,
            runtime=self.runtime,
            state=self.state,
            payments_async_subtensor=self.payments_async_subtensor,
        )

        # Build bid lines from config and show summary
        log_init(LogLevel.MEDIUM, "Building bid lines from configuration", "config")
        self.lines = self.runtime.build_lines_from_config()
        self.runtime.log_cfg_summary(self.lines)

        # Enhanced startup logging with structured information
        init_banner(
            "Miner Started Successfully",
            f"uid={self.uid} | coldkey={self._coldkey_ss58} | hotkey={self.wallet.hotkey.ss58_address} | lines={len(self.lines)} | epoch(e)={getattr(self, 'epoch_index', 0)}",
            [
                ("uid", self.uid),
                ("coldkey", self._coldkey_ss58),
                ("hotkey", self.wallet.hotkey.ss58_address),
                ("bid_lines", len(self.lines)),
                ("epoch", getattr(self, 'epoch_index', 0))
            ]
        )
        
        # Enhanced state information panel
        miner_logger.phase_panel(
            MinerPhase.INITIALIZATION, "State Configuration", 
            [
                ("state_dir", str(self._state_dir)),
                ("state_file", str(self.state.path)),
                ("fresh_start", getattr(self.config, "fresh", False)),
                ("config_loaded", "✅" if hasattr(self.config, "miner") else "❌"),
            ]
        )
        
        # Enhanced treasury information
        treasury_list = list(self.state.treasuries.keys()) if self.state.treasuries else []
        miner_logger.phase_panel(
            MinerPhase.INITIALIZATION, "Treasury Configuration",
            [
                ("allowlisted_validators", len(self.state.treasuries)),
                ("treasury_hotkeys", ", ".join([hk[:8] + "…" for hk in treasury_list[:3]]) + ("..." if len(treasury_list) > 3 else "")),
                ("treasury_source", "LOCAL allowlist (pinned)"),
            ]
        )
        
        # Enhanced bid lines summary
        if self.lines:
            total_alpha = sum(line.alpha for line in self.lines)
            subnets = [str(line.subnet_id) for line in self.lines]
            miner_logger.phase_panel(
                MinerPhase.INITIALIZATION, "Bid Configuration",
                [
                    ("total_bid_lines", len(self.lines)),
                    ("total_alpha", f"{total_alpha:.4f} α"),
                    ("target_subnets", ", ".join(subnets)),
                    ("discount_mode", "EFFECTIVE-DISCOUNT (weight-adjusted)"),
                ]
            )

        # Eager schedule unpaid invoices + watchdog
        log_init(LogLevel.MEDIUM, "Setting up payment scheduling", "payments")
        try:
            self.payments.ensure_payment_config()
            self.payments.schedule_unpaid_pending()
        except Exception as _e:
            log_init(LogLevel.HIGH, "Eager scheduling skipped", "payments", {"error": str(_e)})

        log_init(LogLevel.MEDIUM, "Payment system initialized (will start in main loop)", "payments")
        
        # ---------------------- Auto-Sell Manager ----------------------
        log_init(LogLevel.MEDIUM, "Initializing auto-sell manager", "autosell")
        self.autosell_config = self._build_autosell_config()
        self.autosell_manager = AutoSellManager(
            config=self.autosell_config,
            wallet=self.wallet,
            runtime=self.runtime
        )
        
        # Auto-sell will start in main event loop (PM2 compatible)
        if self.autosell_config.enabled:
            log_init(LogLevel.MEDIUM, "Auto-sell manager initialized (will start in main loop)", "autosell")
        else:
            log_init(LogLevel.MEDIUM, "Auto-sell manager initialized (disabled)", "autosell")
        
        log_init(LogLevel.MEDIUM, "Miner initialization completed successfully", "main")

    def _build_autosell_config(self) -> AutoSellConfig:
        """Build AutoSellConfig from miner configuration."""
        return AutoSellConfig(
            enabled=getattr(self.config, 'autosell.enabled', False),
            keep_alpha=getattr(self.config, 'autosell.keep_alpha', 0.0),
            subnet_id=getattr(self.config, 'autosell.subnet_id', 73),
            check_interval=getattr(self.config, 'autosell.check_interval', 30.0),
            max_retries=getattr(self.config, 'autosell.max_retries', 3),
            wait_for_inclusion=getattr(self.config, 'autosell.wait_for_inclusion', True),
            wait_for_finalization=getattr(self.config, 'autosell.wait_for_finalization', False),
            period=getattr(self.config, 'autosell.period', 512),
            total_alpha_target=getattr(self.config, 'autosell.total_alpha_target', 0.0)
        )

    def _build_bidding_control_config(self) -> BiddingControlConfig:
        """Build BiddingControlConfig from miner configuration."""
        return BiddingControlConfig(
            max_total_alpha=getattr(self.config, 'bidding.max_total_alpha', 0.0),
            min_stake_alpha=getattr(self.config, 'bidding.min_stake_alpha', 0.0),
            stop_on_low_stake=getattr(self.config, 'bidding.stop_on_low_stake', False)
        )

    # ---------------------- Shared AsyncSubtensor ----------------------
    
    def _initialize_shared_async_subtensor(self):
        """
        Initialize shared AsyncSubtensor with custom endpoint for read operations.
        This uses the custom endpoint for AsyncSubtensor operations while main subtensor handles axon serving.
        """
        log_init(LogLevel.MEDIUM, "Starting shared AsyncSubtensor initialization", "chain")
        
        # Log configuration details
        log_init(LogLevel.MEDIUM, f"Config subtensor.network: {getattr(self.config.subtensor, 'network', 'None')}", "chain")
        log_init(LogLevel.MEDIUM, f"Config subtensor.chain_endpoint: {getattr(self.config.subtensor, 'chain_endpoint', 'None')}", "chain")
        log_init(LogLevel.MEDIUM, f"Config subtensor._mock: {getattr(self.config.subtensor, '_mock', 'None')}", "chain")
        
        # Use the custom endpoint for AsyncSubtensor operations (read-only operations)
        custom_endpoint = self.config.subtensor.chain_endpoint
        if not custom_endpoint:
            log_init(LogLevel.CRITICAL, "No chain_endpoint configuration found in config.subtensor.chain_endpoint", "chain")
            raise RuntimeError("No chain_endpoint configuration found in config.subtensor.chain_endpoint")
        
        log_init(LogLevel.MEDIUM, f"Creating AsyncSubtensor with custom endpoint: {custom_endpoint}", "chain")
        self.shared_async_subtensor = bt.AsyncSubtensor(network=custom_endpoint)
        
        # Ensure the network attribute is set for debugging
        self.shared_async_subtensor.network = custom_endpoint
        log_init(LogLevel.MEDIUM, f"Shared AsyncSubtensor created successfully", "chain")
        log_init(LogLevel.MEDIUM, f"AsyncSubtensor.network attribute: {getattr(self.shared_async_subtensor, 'network', 'None')}", "chain")
        log_init(LogLevel.MEDIUM, f"AsyncSubtensor.chain_endpoint attribute: {getattr(self.shared_async_subtensor, 'chain_endpoint', 'None')}", "chain")

    async def _ensure_shared_async_subtensor(self):
        """Ensure the shared AsyncSubtensor is initialized and ready to use."""
        if self.shared_async_subtensor is None:
            self._initialize_shared_async_subtensor()
        if self.shared_async_subtensor is None:
            raise RuntimeError("Failed to initialize shared AsyncSubtensor")
        
        # Initialize if method exists and is coroutine
        init = getattr(self.shared_async_subtensor, "initialize", None)
        if callable(init):
            try:
                maybe_coro = init()
                if asyncio.iscoroutine(maybe_coro):
                    await maybe_coro
            except Exception as e:
                log_init(LogLevel.HIGH, f"Shared AsyncSubtensor initialization failed: {e}", "chain")
        
        return self.shared_async_subtensor

    def _initialize_payments_async_subtensor(self):
        """
        Initialize separate AsyncSubtensor for payments with custom endpoint.
        This ensures payments have their own connection for parallel processing.
        """
        log_init(LogLevel.MEDIUM, "Starting payments AsyncSubtensor initialization", "payments")
        
        # Log configuration details
        log_init(LogLevel.MEDIUM, f"Config subtensor.network: {getattr(self.config.subtensor, 'network', 'None')}", "payments")
        log_init(LogLevel.MEDIUM, f"Config subtensor.chain_endpoint: {getattr(self.config.subtensor, 'chain_endpoint', 'None')}", "payments")
        log_init(LogLevel.MEDIUM, f"Config subtensor._mock: {getattr(self.config.subtensor, '_mock', 'None')}", "payments")
        
        # Use the custom endpoint for payments AsyncSubtensor operations
        custom_endpoint = self.config.subtensor.chain_endpoint
        if not custom_endpoint:
            log_init(LogLevel.CRITICAL, "No chain_endpoint configuration found in config.subtensor.chain_endpoint", "payments")
            raise RuntimeError("No chain_endpoint configuration found in config.subtensor.chain_endpoint")
        
        log_init(LogLevel.MEDIUM, f"Creating payments AsyncSubtensor with custom endpoint: {custom_endpoint}", "payments")
        self.payments_async_subtensor = bt.AsyncSubtensor(network=custom_endpoint)
        
        # Ensure the network attribute is set for debugging
        self.payments_async_subtensor.network = custom_endpoint
        log_init(LogLevel.MEDIUM, f"Payments AsyncSubtensor created successfully", "payments")
        log_init(LogLevel.MEDIUM, f"Payments AsyncSubtensor.network attribute: {getattr(self.payments_async_subtensor, 'network', 'None')}", "payments")
        log_init(LogLevel.MEDIUM, f"Payments AsyncSubtensor.chain_endpoint attribute: {getattr(self.payments_async_subtensor, 'chain_endpoint', 'None')}", "payments")

    async def _ensure_payments_async_subtensor(self):
        """Ensure the payments AsyncSubtensor is initialized and ready to use."""
        if self.payments_async_subtensor is None:
            self._initialize_payments_async_subtensor()
        if self.payments_async_subtensor is None:
            raise RuntimeError("Failed to initialize payments AsyncSubtensor")
        
        # Initialize if method exists and is coroutine
        init = getattr(self.payments_async_subtensor, "initialize", None)
        if callable(init):
            try:
                maybe_coro = init()
                if asyncio.iscoroutine(maybe_coro):
                    await maybe_coro
            except Exception as e:
                log_init(LogLevel.HIGH, f"Payments AsyncSubtensor initialization failed: {e}", "payments")
        
        return self.payments_async_subtensor

    # ---------------------- Protocol handlers ----------------------

    async def auctionstart_forward(self, synapse: AuctionStartSynapse) -> AuctionStartSynapse:
        """
        Keep the standard pattern: fill fields into the incoming synapse in Runtime,
        then strip non-serializable internals before returning it.
        """
        log_auction(LogLevel.MEDIUM, "Processing auction start request", "handler", {
            "validator_uid": getattr(synapse, "validator_uid", "unknown"),
            "epoch": getattr(synapse, "epoch_index", "unknown")
        })
        _strip_internals_inplace(synapse)
        
        # Auto-sell monitoring runs in background via main event loop
        
        # Use timeout to prevent auction processing from blocking other requests
        try:
            out = await asyncio.wait_for(
                self.runtime.handle_auction_start(synapse, self.lines),
                timeout=3.0  # 3 second timeout for auction processing
            )
        except asyncio.TimeoutError:
            log_auction(LogLevel.HIGH, "Auction processing timed out, returning empty response", "handler", {
                "validator_uid": getattr(synapse, "validator_uid", "unknown"),
                "epoch": getattr(synapse, "epoch_index", "unknown")
            })
            # Return empty response to avoid blocking
            synapse.ack = True
            synapse.retries_attempted = 0
            synapse.bids = []
            synapse.bids_sent = 0
            synapse.note = "auction processing timeout"
            out = synapse
        
        _strip_internals_inplace(out)
        return out

    async def win_forward(self, synapse: WinSynapse) -> WinSynapse:
        """
        Same approach for wins: Payments fills the same object; we sanitize it
        before giving it back to Axon for serialization.
        """
        log_commitments(LogLevel.MEDIUM, "Processing win notification", "handler", {
            "validator_uid": getattr(synapse, "validator_uid", "unknown"),
            "subnet_id": getattr(synapse, "subnet_id", "unknown"),
            "accepted_alpha": getattr(synapse, "accepted_alpha", "unknown")
        })
        
        # Use timeout to prevent win processing from blocking other requests
        try:
            synapse = await asyncio.wait_for(
                self.payments.handle_win(synapse),
                timeout=1.0  # 1 second timeout for win processing (should be fast)
            )
        except asyncio.TimeoutError:
            log_commitments(LogLevel.HIGH, "Win processing timed out, returning error response", "handler", {
                "validator_uid": getattr(synapse, "validator_uid", "unknown"),
                "subnet_id": getattr(synapse, "subnet_id", "unknown")
            })
            # Return error response to avoid blocking
            synapse.ack = False
            synapse.payment_attempted = False
            synapse.payment_ok = False
            synapse.attempts = 0
            synapse.last_response = "win processing timeout"
        
        _strip_internals_inplace(synapse)
        return synapse

    async def forward(self, synapse: Synapse):
        """
        You requested to keep echoing the inbound base Synapse. That's fine;
        the slice problem comes from typed routes, which we now sanitize.
        """
        _strip_internals_inplace(synapse)
        return synapse

    # ---------------------- Context manager shutdown ----------------------

    def __exit__(self, exc_type, exc, tb):
        log_init(LogLevel.MEDIUM, "Shutting down miner", "shutdown")
        try:
            self.payments.shutdown_background()
        except Exception as e:
            log_init(LogLevel.HIGH, "Error during payments shutdown", "shutdown", {"error": str(e)})
        
        try:
            if hasattr(self, 'autosell_manager') and self.autosell_manager:
                # Auto-sell shutdown is handled by task cancellation in main event loop
                log_init(LogLevel.MEDIUM, "Auto-sell shutdown handled by main event loop", "shutdown")
        except Exception as e:
            log_init(LogLevel.HIGH, "Error during auto-sell shutdown", "shutdown", {"error": str(e)})
        finally:
            return super().__exit__(exc_type, exc, tb)


if __name__ == "__main__":
    from metahash.bittensor_config import config
    with Miner(config=config(role="miner")) as m:
        import time as _t
        while True:
            _t.sleep(12)
