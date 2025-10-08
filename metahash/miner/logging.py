# metahash/miner/logging.py
"""
Phase-aware logging system for miner components.
Provides consistent colors and importance levels for each phase.
"""

from __future__ import annotations

from typing import Any, Iterable, List, Tuple, Dict
from enum import Enum

from metahash.utils.pretty_logs import pretty
from metahash.base.utils.logging import ColoredLogger as clog


class MinerPhase(Enum):
    """Miner operation phases with associated colors and icons."""
    INITIALIZATION = ("init", "🔧", "bold magenta")
    AUCTION = ("auction", "🎯", "bold cyan") 
    COMMITMENTS = ("commitments", "📋", "bold blue")
    SETTLEMENT = ("settlement", "💰", "bold green")


class LogLevel(Enum):
    """Log importance levels."""
    CRITICAL = ("CRITICAL", "bold red")
    HIGH = ("HIGH", "bold yellow")
    MEDIUM = ("MEDIUM", "bold white")
    LOW = ("LOW", "dim white")
    DEBUG = ("DEBUG", "dim gray")


class MinerLogger:
    """
    Phase-aware logger for miner operations.
    Provides consistent formatting and color coding across all miner components.
    """
    
    def __init__(self, component_name: str = "miner"):
        self.component_name = component_name
    
    def _format_phase_prefix(self, phase: MinerPhase, level: LogLevel) -> str:
        """Format the phase prefix with appropriate styling."""
        phase_name, icon, phase_color = phase.value
        level_name, level_color = level.value
        
        # Use phase color for the icon and phase name
        # Use level color for the level indicator in brackets
        return f"[{phase_color}]{icon} [{level_color}]{level_name}[/{level_color}][/{phase_color}]"
    
    def _format_message(self, phase: MinerPhase, level: LogLevel, message: str, 
                       component: str = None, details: Dict[str, Any] = None) -> str:
        """Format a complete log message with phase and level information."""
        prefix = self._format_phase_prefix(phase, level)
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
    
    def log(self, phase: MinerPhase, level: LogLevel, message: str, 
            component: str = None, details: Dict[str, Any] = None):
        """Log a message with phase and level information."""
        # Filter out LOW and DEBUG logs to reduce noise
        if level in [LogLevel.LOW, LogLevel.DEBUG]:
            return
        formatted_msg = self._format_message(phase, level, message, component, details)
        pretty.log(formatted_msg)
    
    def critical(self, phase: MinerPhase, message: str, component: str = None, details: Dict[str, Any] = None):
        """Log a critical message."""
        self.log(phase, LogLevel.CRITICAL, message, component, details)
    
    def high(self, phase: MinerPhase, message: str, component: str = None, details: Dict[str, Any] = None):
        """Log a high importance message."""
        self.log(phase, LogLevel.HIGH, message, component, details)
    
    def medium(self, phase: MinerPhase, message: str, component: str = None, details: Dict[str, Any] = None):
        """Log a medium importance message."""
        self.log(phase, LogLevel.MEDIUM, message, component, details)
    
    def low(self, phase: MinerPhase, message: str, component: str = None, details: Dict[str, Any] = None):
        """Log a low importance message."""
        self.log(phase, LogLevel.LOW, message, component, details)
    
    def debug(self, phase: MinerPhase, message: str, component: str = None, details: Dict[str, Any] = None):
        """Log a debug message."""
        self.log(phase, LogLevel.DEBUG, message, component, details)
    
    def phase_banner(self, phase: MinerPhase, title: str, subtitle: str = "", details: List[Tuple[str, Any]] = None):
        """Create a phase-specific banner with enhanced styling."""
        phase_name, icon, phase_color = phase.value
        
        # Create enhanced title with phase icon
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
        """Create a phase-specific information panel."""
        # Filter out LOW and DEBUG panels to reduce noise
        if level in [LogLevel.LOW, LogLevel.DEBUG]:
            return
        phase_name, icon, phase_color = phase.value
        level_name, level_color = level.value
        
        # Enhance title with phase icon and level in brackets
        enhanced_title = f"{icon} [{level_name}] {title}"
        
        # Create panel with phase-specific styling
        pretty.kv_panel(enhanced_title, items, style=phase_color)
    
    def phase_table(self, phase: MinerPhase, title: str, columns: List[str], rows: List[List[Any]], 
                   level: LogLevel = LogLevel.MEDIUM, caption: str = None):
        """Create a phase-specific table."""
        # Filter out LOW and DEBUG tables to reduce noise
        if level in [LogLevel.LOW, LogLevel.DEBUG]:
            return
        phase_name, icon, phase_color = phase.value
        level_name, level_color = level.value
        
        # Enhance title with phase icon and level in brackets
        enhanced_title = f"{icon} [{level_name}] {title}"
        
        # Create table with phase-specific styling
        pretty.table(enhanced_title, columns, rows, caption)
    
    def success(self, phase: MinerPhase, message: str, component: str = None, details: Dict[str, Any] = None):
        """Log a success message with green styling."""
        formatted_msg = self._format_message(phase, LogLevel.HIGH, message, component, details)
        # Override with green for success
        success_msg = formatted_msg.replace("[bold yellow]HIGH[/bold yellow]", "[bold green]SUCCESS[/bold green]")
        pretty.log(success_msg)
    
    def warning(self, phase: MinerPhase, message: str, component: str = None, details: Dict[str, Any] = None):
        """Log a warning message with yellow styling."""
        formatted_msg = self._format_message(phase, LogLevel.HIGH, message, component, details)
        # Override with yellow for warning
        warning_msg = formatted_msg.replace("[bold yellow]HIGH[/bold yellow]", "[bold yellow]WARNING[/bold yellow]")
        pretty.log(warning_msg)
    
    def error(self, phase: MinerPhase, message: str, component: str = None, details: Dict[str, Any] = None):
        """Log an error message with red styling."""
        formatted_msg = self._format_message(phase, LogLevel.CRITICAL, message, component, details)
        # Override with red for error
        error_msg = formatted_msg.replace("[bold red]CRITICAL[/bold red]", "[bold red]ERROR[/bold red]")
        pretty.log(error_msg)


# Global logger instance for miner components
miner_logger = MinerLogger("miner")


# Convenience functions for quick access
def log_init(level: LogLevel, message: str, component: str = None, details: Dict[str, Any] = None):
    """Log an initialization phase message."""
    miner_logger.log(MinerPhase.INITIALIZATION, level, message, component, details)

def log_auction(level: LogLevel, message: str, component: str = None, details: Dict[str, Any] = None):
    """Log an auction phase message."""
    miner_logger.log(MinerPhase.AUCTION, level, message, component, details)

def log_commitments(level: LogLevel, message: str, component: str = None, details: Dict[str, Any] = None):
    """Log a commitments phase message."""
    miner_logger.log(MinerPhase.COMMITMENTS, level, message, component, details)

def log_settlement(level: LogLevel, message: str, component: str = None, details: Dict[str, Any] = None):
    """Log a settlement phase message."""
    miner_logger.log(MinerPhase.SETTLEMENT, level, message, component, details)


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
