# ====================================================================== #
# metahash/miner/bidding_control.py                                     #
# ====================================================================== #

from __future__ import annotations

from typing import Optional, Dict, Any
from dataclasses import dataclass

from metahash.miner.logging import (
    MinerPhase, LogLevel, miner_logger, 
    log_init, log_auction, log_commitments, log_settlement
)


@dataclass
class BiddingControlConfig:
    """Configuration for bidding control functionality."""
    max_total_alpha: float = 0.0  # Maximum total alpha to spend on bidding (0 = unlimited)
    min_stake_alpha: float = 0.0  # Minimum alpha stake to maintain
    stop_on_low_stake: bool = False  # Stop bidding when stake falls below threshold


class BiddingController:
    """
    Controls bidding behavior based on total alpha spent and stake thresholds.
    
    This module tracks total alpha spent on bidding and stops bidding when:
    1. Total alpha spent reaches max_total_alpha threshold
    2. Current stake falls below min_stake_alpha threshold (if enabled)
    """
    
    def __init__(self, config: BiddingControlConfig, state_store):
        self.config = config
        self.state = state_store
        
        # Track total alpha spent on bidding
        self._total_alpha_spent = 0.0
        self._bidding_disabled = False
        self._disable_reason = ""
        
        log_init(LogLevel.MEDIUM, "Bidding controller initialized", "bidding_control", {
            "max_total_alpha": config.max_total_alpha,
            "min_stake_alpha": config.min_stake_alpha,
            "stop_on_low_stake": config.stop_on_low_stake
        })
    
    def should_bid(self, current_stake: float, bid_amount: float) -> tuple[bool, str]:
        """
        Check if bidding should be allowed based on current conditions.
        
        Args:
            current_stake: Current alpha stake
            bid_amount: Amount of alpha to bid
            
        Returns:
            tuple: (should_bid: bool, reason: str)
        """
        # Handle None values safely - MUST be first to prevent comparison errors
        if current_stake is None:
            log_auction(LogLevel.HIGH, "Current_stake is None in bidding controller, setting to 0.0", "bidding_control", {
                "current_stake": current_stake,
                "bid_amount": bid_amount
            })
            current_stake = 0.0
        if bid_amount is None:
            log_auction(LogLevel.HIGH, "Bid_amount is None in bidding controller, setting to 0.0", "bidding_control", {
                "current_stake": current_stake,
                "bid_amount": bid_amount
            })
            bid_amount = 0.0
            
        # Ensure config values are not None - CRITICAL: must be before any comparisons
        if self.config.min_stake_alpha is None:
            log_auction(LogLevel.HIGH, "Min_stake_alpha is None, setting to 0.0", "bidding_control", {
                "current_stake": current_stake,
                "bid_amount": bid_amount
            })
            self.config.min_stake_alpha = 0.0
            
        if self.config.max_total_alpha is None:
            log_auction(LogLevel.HIGH, "Max_total_alpha is None, setting to 0.0", "bidding_control", {
                "current_stake": current_stake,
                "bid_amount": bid_amount
            })
            self.config.max_total_alpha = 0.0
            
        if self.config.stop_on_low_stake is None:
            log_auction(LogLevel.HIGH, "Stop_on_low_stake is None, setting to False", "bidding_control", {
                "current_stake": current_stake,
                "bid_amount": bid_amount
            })
            self.config.stop_on_low_stake = False
            
        log_auction(LogLevel.DEBUG, "Bidding controller input values", "bidding_control", {
            "current_stake": current_stake,
            "current_stake_type": type(current_stake).__name__,
            "bid_amount": bid_amount,
            "bid_amount_type": type(bid_amount).__name__,
            "min_stake_alpha": self.config.min_stake_alpha,
            "min_stake_alpha_type": type(self.config.min_stake_alpha).__name__
        })
            
        # Check if bidding is already disabled
        if self._bidding_disabled:
            return False, f"Bidding disabled: {self._disable_reason}"
        
        # Check max total alpha threshold
        if self.config.max_total_alpha > 0:
            if self._total_alpha_spent >= self.config.max_total_alpha:
                self._bidding_disabled = True
                self._disable_reason = f"Max total alpha reached ({self._total_alpha_spent:.4f}/{self.config.max_total_alpha:.4f})"
                log_auction(LogLevel.MEDIUM, "Bidding disabled - max total alpha reached", "bidding_control", {
                    "total_spent": self._total_alpha_spent,
                    "max_total": self.config.max_total_alpha
                })
                return False, self._disable_reason
            
            # Check if this bid would exceed the limit
            if self._total_alpha_spent + bid_amount > self.config.max_total_alpha:
                remaining = self.config.max_total_alpha - self._total_alpha_spent
                if remaining <= 0:
                    self._bidding_disabled = True
                    self._disable_reason = f"Max total alpha reached ({self._total_alpha_spent:.4f}/{self.config.max_total_alpha:.4f})"
                    return False, self._disable_reason
                else:
                    # Allow partial bid up to the limit
                    return True, f"Partial bid allowed (remaining: {remaining:.4f})"
        
        # Check minimum stake threshold
        if self.config.stop_on_low_stake and self.config.min_stake_alpha > 0:
            log_auction(LogLevel.DEBUG, "Checking minimum stake threshold", "bidding_control", {
                "current_stake": current_stake,
                "min_stake_alpha": self.config.min_stake_alpha,
                "comparison": f"{current_stake} < {self.config.min_stake_alpha}"
            })
            if current_stake < self.config.min_stake_alpha:
                self._bidding_disabled = True
                self._disable_reason = f"Stake below minimum threshold ({current_stake:.4f}/{self.config.min_stake_alpha:.4f})"
                log_auction(LogLevel.MEDIUM, "Bidding disabled - stake below minimum", "bidding_control", {
                    "current_stake": current_stake,
                    "min_stake": self.config.min_stake_alpha
                })
                return False, self._disable_reason
            
            # Check if this bid would bring us below the minimum
            log_auction(LogLevel.DEBUG, "Checking if bid would bring us below minimum", "bidding_control", {
                "current_stake": current_stake,
                "bid_amount": bid_amount,
                "min_stake_alpha": self.config.min_stake_alpha,
                "calculation": f"{current_stake} - {bid_amount} = {current_stake - bid_amount}",
                "comparison": f"{current_stake - bid_amount} < {self.config.min_stake_alpha}"
            })
            if current_stake - bid_amount < self.config.min_stake_alpha:
                max_bid = current_stake - self.config.min_stake_alpha
                if max_bid <= 0:
                    self._bidding_disabled = True
                    self._disable_reason = f"Stake below minimum threshold ({current_stake:.4f}/{self.config.min_stake_alpha:.4f})"
                    return False, self._disable_reason
                else:
                    # Allow partial bid up to the limit
                    return True, f"Partial bid allowed (max: {max_bid:.4f})"
        
        return True, "Bidding allowed"
    
    def record_bid(self, bid_amount: float, subnet_id: int, epoch: int):
        """
        Record a successful bid for tracking purposes.
        
        Args:
            bid_amount: Amount of alpha bid
            subnet_id: Subnet ID where bid was placed
            epoch: Epoch when bid was placed
        """
        self._total_alpha_spent += bid_amount
        
        log_auction(LogLevel.MEDIUM, "Bid recorded", "bidding_control", {
            "bid_amount": bid_amount,
            "subnet_id": subnet_id,
            "epoch": epoch,
            "total_spent": self._total_alpha_spent,
            "max_total": self.config.max_total_alpha
        })
        
        # Check if we've reached the limit after this bid
        if self.config.max_total_alpha > 0 and self._total_alpha_spent >= self.config.max_total_alpha:
            self._bidding_disabled = True
            self._disable_reason = f"Max total alpha reached ({self._total_alpha_spent:.4f}/{self.config.max_total_alpha:.4f})"
            
            log_auction(LogLevel.MEDIUM, "Bidding disabled - max total alpha reached", "bidding_control", {
                "total_spent": self._total_alpha_spent,
                "max_total": self.config.max_total_alpha
            })
            
            # Show bidding control summary
            miner_logger.phase_panel(
                MinerPhase.AUCTION, "Bidding Disabled - Max Total Alpha Reached",
                [
                    ("total_spent_α", f"{self._total_alpha_spent:.4f}"),
                    ("max_total_α", f"{self.config.max_total_alpha:.4f}"),
                    ("status", "BIDDING DISABLED"),
                ],
                LogLevel.MEDIUM
            )
    
    def get_stats(self) -> Dict[str, Any]:
        """Get bidding control statistics."""
        return {
            "max_total_alpha": self.config.max_total_alpha,
            "min_stake_alpha": self.config.min_stake_alpha,
            "stop_on_low_stake": self.config.stop_on_low_stake,
            "total_alpha_spent": self._total_alpha_spent,
            "bidding_disabled": self._bidding_disabled,
            "disable_reason": self._disable_reason,
            "remaining_alpha": max(0, self.config.max_total_alpha - self._total_alpha_spent) if self.config.max_total_alpha > 0 else float('inf')
        }
    
    def log_stats(self):
        """Log current bidding control statistics."""
        stats = self.get_stats()
        
        miner_logger.phase_panel(
            MinerPhase.SETTLEMENT, "Bidding Control Statistics",
            [
                ("max_total_α", f"{stats['max_total_alpha']:.4f}" if stats['max_total_alpha'] > 0 else "∞"),
                ("min_stake_α", f"{stats['min_stake_alpha']:.4f}" if stats['min_stake_alpha'] > 0 else "0"),
                ("stop_on_low_stake", "✅" if stats["stop_on_low_stake"] else "❌"),
                ("total_spent_α", f"{stats['total_alpha_spent']:.4f}"),
                ("remaining_α", f"{stats['remaining_alpha']:.4f}" if stats['remaining_alpha'] != float('inf') else "∞"),
                ("bidding_disabled", "✅" if stats["bidding_disabled"] else "❌"),
                ("disable_reason", stats["disable_reason"] if stats["disable_reason"] else "None"),
            ],
            LogLevel.MEDIUM
        )
