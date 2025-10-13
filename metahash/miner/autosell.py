# ====================================================================== #
# metahash/miner/autosell.py                                            #
# ====================================================================== #

from __future__ import annotations

import asyncio
import threading
from typing import Optional, Dict, Any
from dataclasses import dataclass

import bittensor as bt
from bittensor import AsyncSubtensor, Balance

from metahash.base.utils.logging import ColoredLogger as clog
from metahash.miner.logging import (
    MinerPhase, LogLevel, miner_logger, 
    log_init, log_auction, log_commitments, log_settlement
)
from metahash.config import PLANCK


@dataclass
class AutoSellConfig:
    """Configuration for auto-sell functionality."""
    enabled: bool = False
    keep_alpha: float = 0.0  # Amount of alpha to keep (default: sell everything)
    subnet_id: int = 73  # SN73 (metahash subnet)
    check_interval: float = 30.0  # Check interval in seconds
    max_retries: int = 3  # Max retries for failed transactions
    wait_for_inclusion: bool = True
    wait_for_finalization: bool = False
    period: int = 512  # Block period for transaction confirmation
    total_alpha_target: float = 0.0  # Total alpha to sell before stopping (0 = unlimited)


class AutoSellManager:
    """
    Manages automatic selling of metahash SN73 alpha stake.
    
    This module monitors the miner's alpha balance on SN73 and automatically
    sells any amount above the configured keep_alpha threshold when auctions start.
    """
    
    def __init__(self, config: AutoSellConfig, wallet, runtime):
        self.config = config
        self.wallet = wallet
        self.runtime = runtime
        
        # Async subtensor connection (reuse from runtime)
        self._async_subtensor: Optional[AsyncSubtensor] = None
        self._rpc_lock: Optional[asyncio.Lock] = None
        
        # Main event loop task management (PM2 compatible)
        self._task: Optional[asyncio.Task] = None
        self._running = False
        
        # Statistics
        self._total_sold_alpha = 0.0
        self._sell_attempts = 0
        self._successful_sells = 0
        self._failed_sells = 0
        
        log_init(LogLevel.MEDIUM, "Auto-sell manager initialized", "autosell", {
            "enabled": config.enabled,
            "keep_alpha": config.keep_alpha,
            "subnet_id": config.subnet_id,
            "check_interval": config.check_interval,
            "total_alpha_target": config.total_alpha_target
        })
    
    async def _ensure_async_subtensor(self):
        """Ensure async subtensor connection is available."""
        if self._async_subtensor is None:
            log_init(LogLevel.MEDIUM, "Initializing async subtensor for auto-sell", "autosell")
            try:
                # Reuse the runtime's async subtensor if available
                if hasattr(self.runtime, '_async_subtensor') and self.runtime._async_subtensor:
                    self._async_subtensor = self.runtime._async_subtensor
                    self._rpc_lock = getattr(self.runtime, '_rpc_lock', None)
                else:
                    # Create new connection
                    network = getattr(self.runtime.config.subtensor, 'network', 'finney')
                    self._async_subtensor = AsyncSubtensor(network=network)
                    init = getattr(self._async_subtensor, "initialize", None)
                    if callable(init):
                        try:
                            await init()
                        except Exception as e:
                            log_init(LogLevel.HIGH, "Failed to initialize async subtensor for auto-sell", "autosell", {"error": str(e)})
                    
                    self._rpc_lock = asyncio.Lock()
                    
            except Exception as e:
                log_init(LogLevel.HIGH, "Failed to create async subtensor for auto-sell", "autosell", {"error": str(e)})
                raise
        
        if self._rpc_lock is None:
            self._rpc_lock = asyncio.Lock()
    
    async def get_alpha_balance(self) -> float:
        """Get current alpha balance for SN73."""
        await self._ensure_async_subtensor()
        
        try:
            hotkey_ss58 = self.wallet.hotkey.ss58_address
            balance = await self._async_subtensor.get_alpha_balance(
                hotkey_ss58, 
                netuid=self.config.subnet_id
            )
            
            if hasattr(balance, "rao"):
                return float(balance.rao) / float(PLANCK)
            elif hasattr(balance, "value"):
                return float(balance.value) / float(PLANCK)
            else:
                return float(balance) if balance else 0.0
                
        except Exception as e:
            log_init(LogLevel.HIGH, "Failed to get alpha balance", "autosell", {
                "subnet_id": self.config.subnet_id,
                "error": str(e)
            })
            return 0.0
    
    async def sell_alpha(self, amount_alpha: float) -> bool:
        """
        Sell the specified amount of alpha stake.
        
        Args:
            amount_alpha: Amount of alpha to sell (in alpha units, not rao)
            
        Returns:
            bool: True if successful, False otherwise
        """
        if amount_alpha <= 0:
            return True  # Nothing to sell
            
        await self._ensure_async_subtensor()
        
        # Convert alpha to rao
        amount_rao = int(amount_alpha * PLANCK)
        amount_balance = Balance.from_rao(amount_rao)
        
        hotkey_ss58 = self.wallet.hotkey.ss58_address
        coldkey_ss58 = self.wallet.coldkey.ss58_address
        
        log_auction(LogLevel.MEDIUM, "Attempting to sell alpha stake", "autosell", {
            "amount_alpha": amount_alpha,
            "amount_rao": amount_rao,
            "subnet_id": self.config.subnet_id,
            "hotkey": hotkey_ss58[:8] + "…",
            "coldkey": coldkey_ss58[:8] + "…"
        })
        
        # Use the transfer_alpha function from wallet_utils
        from metahash.utils.wallet_utils import transfer_alpha
        
        try:
            success = await transfer_alpha(
                subtensor=self._async_subtensor,
                wallet=self.wallet,
                hotkey_ss58=hotkey_ss58,
                origin_and_dest_netuid=self.config.subnet_id,
                dest_coldkey_ss58=coldkey_ss58,  # Transfer to self (sell)
                amount=amount_balance,
                wait_for_inclusion=self.config.wait_for_inclusion,
                wait_for_finalization=self.config.wait_for_finalization,
                period=self.config.period,
                max_retries=self.config.max_retries
            )
            
            if success:
                self._successful_sells += 1
                self._total_sold_alpha += amount_alpha
                log_auction(LogLevel.MEDIUM, "Alpha stake sold successfully", "autosell", {
                    "amount_alpha": amount_alpha,
                    "total_sold": self._total_sold_alpha
                })
                return True
            else:
                self._failed_sells += 1
                log_auction(LogLevel.HIGH, "Failed to sell alpha stake", "autosell", {
                    "amount_alpha": amount_alpha,
                    "attempt": self._sell_attempts
                })
                return False
                
        except Exception as e:
            self._failed_sells += 1
            log_auction(LogLevel.HIGH, "Exception during alpha sale", "autosell", {
                "amount_alpha": amount_alpha,
                "error": str(e)
            })
            return False
    
    async def check_and_sell(self) -> bool:
        """
        Check current alpha balance and sell if above keep_alpha threshold.
        
        Returns:
            bool: True if a sale was attempted, False if no sale needed
        """
        if not self.config.enabled:
            return False
            
        try:
            current_balance = await self.get_alpha_balance()
            
            # Check if we've reached the total alpha target
            if self.config.total_alpha_target > 0 and self._total_sold_alpha >= self.config.total_alpha_target:
                log_auction(LogLevel.MEDIUM, "Total alpha target reached, stopping auto-sell", "autosell", {
                    "total_sold": self._total_sold_alpha,
                    "target": self.config.total_alpha_target,
                    "subnet_id": self.config.subnet_id
                })
                miner_logger.phase_panel(
                    MinerPhase.AUCTION, "Auto-Sell Target Reached",
                    [
                        ("subnet", f"SN{self.config.subnet_id}"),
                        ("total_sold_α", f"{self._total_sold_alpha:.4f}"),
                        ("target_α", f"{self.config.total_alpha_target:.4f}"),
                        ("status", "STOPPING"),
                    ],
                    LogLevel.MEDIUM
                )
                # Stop the monitoring task
                self._running = False
                if self._task:
                    self._task.cancel()
                return False
            
            if current_balance <= self.config.keep_alpha:
                log_auction(LogLevel.MEDIUM, "Alpha balance below keep threshold", "autosell", {
                    "current_balance": current_balance,
                    "keep_alpha": self.config.keep_alpha,
                    "subnet_id": self.config.subnet_id
                })
                return False
            
            # Calculate amount to sell
            amount_to_sell = current_balance - self.config.keep_alpha
            
            # If we have a total target, limit the amount to sell to not exceed it
            if self.config.total_alpha_target > 0:
                remaining_target = self.config.total_alpha_target - self._total_sold_alpha
                if remaining_target <= 0:
                    log_auction(LogLevel.MEDIUM, "Target already reached, no more selling needed", "autosell", {
                        "total_sold": self._total_sold_alpha,
                        "target": self.config.total_alpha_target
                    })
                    self._running = False
                    if self._task:
                        self._task.cancel()
                    return False
                amount_to_sell = min(amount_to_sell, remaining_target)
            
            log_auction(LogLevel.MEDIUM, "Alpha balance above threshold, initiating sale", "autosell", {
                "current_balance": current_balance,
                "keep_alpha": self.config.keep_alpha,
                "amount_to_sell": amount_to_sell,
                "total_sold": self._total_sold_alpha,
                "target": self.config.total_alpha_target,
                "subnet_id": self.config.subnet_id
            })
            
            # Show auto-sell summary
            miner_logger.phase_panel(
                MinerPhase.AUCTION, "Auto-Sell Triggered",
                [
                    ("subnet", f"SN{self.config.subnet_id}"),
                    ("current_α", f"{current_balance:.4f}"),
                    ("keep_α", f"{self.config.keep_alpha:.4f}"),
                    ("selling_α", f"{amount_to_sell:.4f}"),
                    ("total_sold_α", f"{self._total_sold_alpha:.4f}"),
                    ("target_α", f"{self.config.total_alpha_target:.4f}" if self.config.total_alpha_target > 0 else "∞"),
                ],
                LogLevel.MEDIUM
            )
            
            self._sell_attempts += 1
            success = await self.sell_alpha(amount_to_sell)
            
            if success:
                log_auction(LogLevel.MEDIUM, "Auto-sell completed successfully", "autosell", {
                    "amount_sold": amount_to_sell,
                    "total_sold": self._total_sold_alpha
                })
                
                # Check if we've reached the target after this sale
                if self.config.total_alpha_target > 0 and self._total_sold_alpha >= self.config.total_alpha_target:
                    log_auction(LogLevel.MEDIUM, "Total alpha target reached after sale, stopping auto-sell", "autosell", {
                        "total_sold": self._total_sold_alpha,
                        "target": self.config.total_alpha_target
                    })
                    self._running = False
                    if self._task:
                        self._task.cancel()
            else:
                log_auction(LogLevel.HIGH, "Auto-sell failed", "autosell", {
                    "amount_attempted": amount_to_sell,
                    "attempts": self._sell_attempts
                })
            
            return True
            
        except Exception as e:
            log_auction(LogLevel.HIGH, "Error during auto-sell check", "autosell", {
                "error": str(e)
            })
            return False
    
    async def _monitor_loop(self):
        """Main event loop monitoring that periodically checks and sells alpha."""
        while self._running:
            try:
                await self.check_and_sell()
            except Exception as e:
                log_auction(LogLevel.HIGH, "Error in auto-sell monitoring loop", "autosell", {
                    "error": str(e)
                })
            
            # Use asyncio.sleep instead of shutdown event (PM2 compatible)
            try:
                await asyncio.sleep(self.config.check_interval)
            except asyncio.CancelledError:
                break  # Task was cancelled
    
    def start_background_tasks(self):
        """Start auto-sell monitoring in the main event loop (PM2 compatible)."""
        if not self.config.enabled:
            return
            
        if self._task and not self._task.done():
            log_init(LogLevel.MEDIUM, "Auto-sell monitoring already running", "autosell")
            return
            
        log_init(LogLevel.MEDIUM, "Starting auto-sell monitoring in main event loop", "autosell")
        self._task = asyncio.create_task(self._monitor_loop())
        self._running = True

    async def stop_background_monitor(self):
        """Stop the auto-sell monitoring task."""
        if self._task and not self._task.done():
            log_init(LogLevel.MEDIUM, "Stopping auto-sell monitoring", "autosell")
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                log_init(LogLevel.HIGH, "Error stopping auto-sell monitoring", "autosell", {"error": str(e)})
            finally:
                self._running = False
                self._task = None

    def get_stats(self) -> Dict[str, Any]:
        """Get auto-sell statistics."""
        return {
            "enabled": self.config.enabled,
            "keep_alpha": self.config.keep_alpha,
            "subnet_id": self.config.subnet_id,
            "total_alpha_target": self.config.total_alpha_target,
            "total_sold_alpha": self._total_sold_alpha,
            "sell_attempts": self._sell_attempts,
            "successful_sells": self._successful_sells,
            "failed_sells": self._failed_sells,
            "success_rate": (self._successful_sells / max(1, self._sell_attempts)) * 100,
            "running": self._running,
            "target_reached": self.config.total_alpha_target > 0 and self._total_sold_alpha >= self.config.total_alpha_target
        }
    
    def log_stats(self):
        """Log current auto-sell statistics."""
        stats = self.get_stats()
        
        if not stats["enabled"]:
            return
            
        miner_logger.phase_panel(
            MinerPhase.SETTLEMENT, "Auto-Sell Statistics",
            [
                ("enabled", "✅" if stats["enabled"] else "❌"),
                ("keep_α", f"{stats['keep_alpha']:.4f}"),
                ("subnet", f"SN{stats['subnet_id']}"),
                ("target_α", f"{stats['total_alpha_target']:.4f}" if stats['total_alpha_target'] > 0 else "∞"),
                ("total_sold_α", f"{stats['total_sold_alpha']:.4f}"),
                ("target_reached", "✅" if stats["target_reached"] else "❌"),
                ("attempts", stats["sell_attempts"]),
                ("successful", stats["successful_sells"]),
                ("failed", stats["failed_sells"]),
                ("success_rate", f"{stats['success_rate']:.1f}%"),
                ("running", "✅" if stats["running"] else "❌"),
            ],
            LogLevel.MEDIUM
        )
