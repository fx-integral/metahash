# metahash/miner/logging.py
"""
Clean, phase-aware logging system for miner components.
Provides consistent colors and structured formatting for each phase.
"""

from __future__ import annotations

from typing import Any, Iterable, List, Tuple, Dict, Optional
from enum import Enum
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text

from metahash.utils.pretty_logs import pretty
from metahash.base.utils.logging import ColoredLogger as clog


class MinerPhase(Enum):
    """Miner operation phases with associated colors and icons."""
    INITIALIZATION = ("init", "🔧", "bold magenta")
    AUCTION = ("auction", "🎯", "bold cyan") 
    COMMITMENTS = ("commitments", "📋", "bold blue")
    SETTLEMENT = ("settlement", "💰", "bold green")


class LogLevel(Enum):
    """Log importance levels - kept for compatibility but not displayed."""
    CRITICAL = ("CRITICAL", "bold red")
    HIGH = ("HIGH", "bold yellow")
    MEDIUM = ("MEDIUM", "bold white")
    LOW = ("LOW", "dim white")
    DEBUG = ("DEBUG", "dim gray")


class MinerLogger:
    """
    Clean, phase-aware logger for miner operations.
    Provides consistent formatting and color coding across all miner components.
    """
    
    def __init__(self, component_name: str = "miner"):
        self.component_name = component_name
        self.console = Console()
    
    def _format_phase_prefix(self, phase: MinerPhase) -> str:
        """Format the phase prefix with appropriate styling - no severity levels."""
        phase_name, icon, phase_color = phase.value
        return f"[{phase_color}]{icon}[/{phase_color}]"
    
    def _format_message(self, phase: MinerPhase, message: str, 
                       component: str = None, details: Dict[str, Any] = None) -> str:
        """Format a complete log message with phase information."""
        prefix = self._format_phase_prefix(phase)
        comp = f"[{self.component_name}]" if component is None else f"[{component}]"
        
        formatted_msg = f"{prefix} {comp} {message}"
        
        # Add details if provided
        if details:
            detail_strs = []
            for key, value in details.items():
                if isinstance(value, (int, float)):
                    detail_strs.append(f"{key}={value}")
                else:
                    detail_strs.append(f"{key}='{value}'")
            if detail_strs:
                formatted_msg += f" | {' | '.join(detail_strs)}"
        
        return formatted_msg
    
    def log(self, phase: MinerPhase, message: str, 
            component: str = None, details: Dict[str, Any] = None):
        """Log a message with phase information."""
        formatted_msg = self._format_message(phase, message, component, details)
        pretty.log(formatted_msg)
    
    def critical(self, phase: MinerPhase, message: str, component: str = None, details: Dict[str, Any] = None):
        """Log a critical message."""
        self.log(phase, message, component, details)
    
    def high(self, phase: MinerPhase, message: str, component: str = None, details: Dict[str, Any] = None):
        """Log a high importance message."""
        self.log(phase, message, component, details)
    
    def medium(self, phase: MinerPhase, message: str, component: str = None, details: Dict[str, Any] = None):
        """Log a medium importance message."""
        self.log(phase, message, component, details)
    
    def low(self, phase: MinerPhase, message: str, component: str = None, details: Dict[str, Any] = None):
        """Log a low importance message."""
        self.log(phase, message, component, details)
    
    def debug(self, phase: MinerPhase, message: str, component: str = None, details: Dict[str, Any] = None):
        """Log a debug message."""
        self.log(phase, message, component, details)
    
    def phase_banner(self, phase: MinerPhase, title: str, subtitle: str = "", details: List[Tuple[str, Any]] = None):
        """Create a clean phase-specific banner."""
        phase_name, icon, phase_color = phase.value
        
        # Create clean title with phase icon
        enhanced_title = f"{icon} {title}"
        
        # Add phase information to subtitle
        if subtitle:
            enhanced_subtitle = f"[{phase_name.upper()}] {subtitle}"
        else:
            enhanced_subtitle = f"[{phase_name.upper()}] Phase"
        
        # Add details if provided
        if details:
            detail_strs = [f"{k}: {v}" for k, v in details]
            enhanced_subtitle += f" | {' | '.join(detail_strs)}"
        
        pretty.banner(enhanced_title, enhanced_subtitle, style=phase_color)
    
    def phase_panel(self, phase: MinerPhase, title: str, items: Iterable[Tuple[str, Any]], 
                   level: LogLevel = LogLevel.MEDIUM):
        """Create a clean phase-specific information panel."""
        phase_name, icon, phase_color = phase.value
        
        # Create clean title with phase icon
        enhanced_title = f"{icon} {title}"
        
        # Create panel with phase-specific styling
        pretty.kv_panel(enhanced_title, items, style=phase_color)

    def phase_panel_with_color(self, phase: MinerPhase, title: str, items: Iterable[Tuple[str, Any]], 
                              color: str):
        """Create a phase-specific information panel with custom color."""
        phase_name, icon, phase_color = phase.value
        
        # Create clean title with phase icon
        enhanced_title = f"{icon} {title}"
        
        # Create panel with custom color styling
        pretty.kv_panel(enhanced_title, items, style=color)

    def phase_panel_with_icon_and_color(self, phase: MinerPhase, title: str, items: Iterable[Tuple[str, Any]], 
                                       color: str):
        """Create a phase-specific information panel with custom icon and color."""
        # Use the title as-is since it already contains the custom icon
        enhanced_title = title
        
        # Create panel with custom color styling
        pretty.kv_panel(enhanced_title, items, style=color)
    
    def phase_table(self, phase: MinerPhase, title: str, columns: List[str], rows: List[List[Any]], 
                   level: LogLevel = LogLevel.MEDIUM, caption: str = None):
        """Create a clean phase-specific table."""
        phase_name, icon, phase_color = phase.value
        
        # Create clean title with phase icon
        enhanced_title = f"{icon} {title}"
        
        # Create table with phase-specific styling
        pretty.table(enhanced_title, columns, rows, caption)
    
    def success(self, phase: MinerPhase, message: str, component: str = None, details: Dict[str, Any] = None):
        """Log a success message with green styling."""
        formatted_msg = self._format_message(phase, message, component, details)
        # Override with green for success
        success_msg = formatted_msg.replace(f"[{phase.value[2]}]", "[bold green]")
        success_msg = success_msg.replace("[/bold magenta]", "[/bold green]")
        success_msg = success_msg.replace("[/bold cyan]", "[/bold green]")
        success_msg = success_msg.replace("[/bold blue]", "[/bold green]")
        pretty.log(success_msg)
    
    def warning(self, phase: MinerPhase, message: str, component: str = None, details: Dict[str, Any] = None):
        """Log a warning message with yellow styling."""
        formatted_msg = self._format_message(phase, message, component, details)
        # Override with yellow for warning
        warning_msg = formatted_msg.replace(f"[{phase.value[2]}]", "[bold yellow]")
        warning_msg = warning_msg.replace("[/bold magenta]", "[/bold yellow]")
        warning_msg = warning_msg.replace("[/bold cyan]", "[/bold yellow]")
        warning_msg = warning_msg.replace("[/bold blue]", "[/bold yellow]")
        pretty.log(warning_msg)
    
    def error(self, phase: MinerPhase, message: str, component: str = None, details: Dict[str, Any] = None):
        """Log an error message with red styling."""
        formatted_msg = self._format_message(phase, message, component, details)
        # Override with red for error
        error_msg = formatted_msg.replace(f"[{phase.value[2]}]", "[bold red]")
        error_msg = error_msg.replace("[/bold magenta]", "[/bold red]")
        error_msg = error_msg.replace("[/bold cyan]", "[/bold red]")
        error_msg = error_msg.replace("[/bold blue]", "[/bold red]")
        pretty.log(error_msg)

    def auction_summary(self, validator: str, epoch: int, budget_alpha: float, min_stake_alpha: float, 
                       weights_count: int, discount_mode: str, timeline: str, treasury_src: str):
        """Create a clean auction summary panel."""
        phase_name, icon, phase_color = MinerPhase.AUCTION.value
        
        items = [
            ("validator", validator),
            ("epoch (e)", epoch),
            ("budget α", f"{budget_alpha:.3f}"),
            ("min_stake", f"{min_stake_alpha:.3f} α"),
            ("weights", f"{weights_count} subnet(s)"),
            ("discount_mode", discount_mode),
            ("timeline", timeline),
            ("treasury_src", treasury_src),
        ]
        
        self.phase_panel(MinerPhase.AUCTION, "Auction Start", items)

    def stake_summary(self, stake_data: Dict[int, float], min_stake_alpha: float):
        """Create a clean stake summary table and panel."""
        phase_name, icon, phase_color = MinerPhase.AUCTION.value
        
        # Create stake table
        rows = [[f"SN-{sid}", f"{amt:.4f} α", "✅" if amt >= min_stake_alpha else "❌"] 
                for sid, amt in sorted(stake_data.items())]
        
        if rows:
            self.phase_table(
                MinerPhase.AUCTION, "Available Stake with Validator (per subnet)", 
                ["Subnet", "α available", "meets_min"], rows
            )
            
            # Add summary statistics
            total_stake = sum(stake_data.values())
            eligible_subnets = sum(1 for amt in stake_data.values() if amt >= min_stake_alpha)
            
            summary_items = [
                ("total_stake", f"{total_stake:.4f} α"),
                ("eligible_subnets", f"{eligible_subnets}/{len(stake_data)}"),
                ("min_stake_required", f"{min_stake_alpha:.4f} α"),
            ]
            
            self.phase_panel(MinerPhase.AUCTION, "Stake Summary", summary_items)

    def bid_summary(self, bids_data: List[List], epoch: int):
        """Create a clean bid summary table and panel."""
        if not bids_data:
            return
            
        # bids_data is already formatted for display, so we can use it directly
        self.phase_table(
            MinerPhase.AUCTION, "Bids Sent",
            ["Subnet", "Alpha", "Disc(cfg)", "Disc(send)", "Weight", "Eff", "BidID", "Epoch", "Mode"],
            bids_data
        )
        
        # For summary statistics, we need to extract numeric values from the formatted strings
        total_alpha_sent = 0.0
        unique_subnets = set()
        total_discount = 0.0
        
        for bid in bids_data:
            subnet_id, alpha_str, disc_cfg_str, disc_send_str, weight_str, eff_str, bid_id, bid_epoch, mode = bid
            
            # Extract numeric values from formatted strings
            try:
                # Parse alpha value - remove " α" suffix
                alpha_val = float(alpha_str.replace(" α", ""))
                
                # Parse discount value - handle both "1000 bps" and "1000 bps (eff)" formats
                disc_send_clean = disc_send_str.split(" bps")[0]  # Get everything before " bps"
                disc_send_val = float(disc_send_clean)
                
                total_alpha_sent += alpha_val
                total_discount += disc_send_val
                unique_subnets.add(subnet_id)
            except (ValueError, AttributeError):
                continue
        
        avg_discount = total_discount / len(bids_data) if bids_data else 0
        
        summary_items = [
            ("total_bids", len(bids_data)),
            ("total_alpha", f"{total_alpha_sent:.4f} α"),
            ("unique_subnets", len(unique_subnets)),
            ("avg_discount", f"{avg_discount:.0f} bps"),
            ("epoch", epoch),
        ]
        
        self.phase_panel(MinerPhase.AUCTION, "Bid Summary", summary_items)

    def win_summary(self, validator: str, epoch_now: int, subnet_id: int, accepted_alpha: float, 
                   requested_alpha: float, was_partial: bool, discount_bps: int, 
                   pay_epoch: int, window: str, amount_to_pay: float, invoice_id: str, treasury_src: str):
        """Create a clean win summary panel with green color for success."""
        items = [
            ("validator", validator),
            ("epoch_now (e)", epoch_now),
            ("subnet", f"SN-{subnet_id}"),
            ("accepted α", f"{accepted_alpha:.4f}"),
            ("requested α", f"{requested_alpha:.4f}"),
            ("partial", "✅" if was_partial else "❌"),
            ("discount", f"{discount_bps} bps"),
            ("pay_epoch (e+1)", pay_epoch),
            ("window", window),
            ("amount_to_pay", f"{amount_to_pay:.4f} α"),
            ("invoice_id", invoice_id),
            ("treasury_src", treasury_src),
        ]
        
        # Use green color for win received (success)
        self.phase_panel_with_color(MinerPhase.COMMITMENTS, "Win Received", items, "bold green")

    def payment_summary(self, invoice_id: str, subnet_id: int, amount_alpha: float, 
                       window: str, safety_blocks: int, retry_every: int, retry_max: int, treasury: str):
        """Create a clean payment summary panel with yellow color for pending."""
        items = [
            ("invoice", invoice_id),
            ("subnet", f"SN-{subnet_id}"),
            ("α", f"{amount_alpha:.4f}"),
            ("window", window),
            ("safety", f"+{safety_blocks} blk"),
            ("retry", f"every {retry_every} blk × {retry_max}"),
            ("treasury", treasury[:8] + "…"),
        ]
        
        # Use yellow color for payment scheduled (pending/warning)
        self.phase_panel_with_color(MinerPhase.SETTLEMENT, "Payment Scheduled", items, "bold yellow")

    def payment_success_summary(self, invoice_id: str, amount_alpha: float, destination: str, 
                               source: str, subnet_id: int, pay_epoch: int, tx_hash: str = None, 
                               block: int = None, attempts: int = None):
        """Create a clean payment success summary panel with green color."""
        items = [
            ("invoice", invoice_id),
            ("amount", f"{amount_alpha:.4f} α"),
            ("destination", f"Treasury {destination[:8]}…"),
            ("source", f"Hotkey {source[:8]}…"),
            ("subnet", f"SN-{subnet_id}"),
            ("pay_epoch", pay_epoch),
        ]
        
        # Add optional fields if provided
        if tx_hash:
            items.append(("tx_hash", tx_hash[:10] + "…"))
        if block:
            items.append(("block", block))
        if attempts:
            items.append(("attempts", attempts))
        
        # Use green color for payment success
        self.phase_panel_with_color(MinerPhase.SETTLEMENT, "Payment Success", items, "bold green")

    def payment_failed_summary(self, invoice_id: str, response: str, subnet_id: int, 
                              attempts: int, max_attempts: int, amount_alpha: float, current_block: int = None):
        """Create a clean payment failed summary panel with red color and cross icon."""
        items = [
            ("invoice", invoice_id),
            ("response", response[:80]),
            ("subnet", f"SN-{subnet_id}"),
            ("attempts", f"{attempts}/{max_attempts}"),
            ("amount", f"{amount_alpha:.4f} α"),
        ]
        
        # Add current block if provided
        if current_block:
            items.append(("current_block", current_block))
        
        # Use red color for payment failed with cross icon
        self.phase_panel_with_icon_and_color(MinerPhase.SETTLEMENT, "❌ Payment Failed", items, "bold red")


# Global logger instance for miner components
miner_logger = MinerLogger("miner")


# Convenience functions for quick access
def log_init(level: LogLevel, message: str, component: str = None, details: Dict[str, Any] = None):
    """Log an initialization phase message."""
    miner_logger.log(MinerPhase.INITIALIZATION, message, component, details)

def log_auction(level: LogLevel, message: str, component: str = None, details: Dict[str, Any] = None):
    """Log an auction phase message."""
    miner_logger.log(MinerPhase.AUCTION, message, component, details)

def log_commitments(level: LogLevel, message: str, component: str = None, details: Dict[str, Any] = None):
    """Log a commitments phase message."""
    miner_logger.log(MinerPhase.COMMITMENTS, message, component, details)

def log_settlement(level: LogLevel, message: str, component: str = None, details: Dict[str, Any] = None):
    """Log a settlement phase message."""
    miner_logger.log(MinerPhase.SETTLEMENT, message, component, details)


# Phase-specific convenience functions
def init_banner(title: str, subtitle: str = "", details: List[Tuple[str, Any]] = None):
    """Create an initialization phase banner."""
    miner_logger.phase_banner(MinerPhase.INITIALIZATION, title, subtitle, details)

def auction_banner(title: str, subtitle: str = "", details: List[Tuple[str, Any]] = None):
    """Create an auction phase banner."""
    miner_logger.phase_banner(MinerPhase.AUCTION, title, subtitle, details)

def commitments_banner(title: str, subtitle: str = "", details: List[Tuple[str, Any]] = None):
    """Create a commitments phase banner."""
    miner_logger.phase_banner(MinerPhase.COMMITMENTS, title, subtitle, details)

def settlement_banner(title: str, subtitle: str = "", details: List[Tuple[str, Any]] = None):
    """Create a settlement phase banner."""
    miner_logger.phase_banner(MinerPhase.SETTLEMENT, title, subtitle, details)