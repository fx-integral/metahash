#!/usr/bin/env python3
# neurons/miner.py

from pathlib import Path
from typing import Any

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

        # ---------------------- Runtime & Payments ----------------------
        log_init(LogLevel.MEDIUM, "Initializing runtime component", "runtime")
        self.runtime = Runtime(
            config=self.config,
            wallet=self.wallet,
            metagraph=self.metagraph,
            state=self.state,
        )

        log_init(LogLevel.MEDIUM, "Initializing payments component", "payments")
        self.payments = Payments(
            config=self.config,
            wallet=self.wallet,
            runtime=self.runtime,
            state=self.state,
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

        log_init(LogLevel.MEDIUM, "Starting background payment tasks", "payments")
        self.payments.start_background_tasks()
        
        log_init(LogLevel.MEDIUM, "Miner initialization completed successfully", "main")

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
        out = await self.runtime.handle_auction_start(synapse, self.lines)
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
        synapse = await self.payments.handle_win(synapse)
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
            log_init(LogLevel.HIGH, "Error during shutdown", "shutdown", {"error": str(e)})
        finally:
            return super().__exit__(exc_type, exc, tb)


if __name__ == "__main__":
    from metahash.bittensor_config import config
    with Miner(config=config(role="miner")) as m:
        import time as _t
        while True:
            _t.sleep(12)
