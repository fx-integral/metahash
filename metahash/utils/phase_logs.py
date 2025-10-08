# metahash/utils/phase_logs.py
"""
Phase-based logging system for validator operations.
Organizes logs by phases (auction, clearing, commitments, settlement) with color coding.
"""

from typing import Dict, List, Optional, Any
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich import box
import time

class PhaseLogger:
    """Phase-based logger with color coding and organized output."""
    
    # Phase colors
    PHASE_COLORS = {
        'auction': 'cyan',
        'clearing': 'orange1',
        'commitments': 'magenta',
        'settlement': 'yellow',
        'general': 'white'
    }
    
    def __init__(self):
        self.console = Console()
        self.current_phase = None
        self.phase_data = {}
        
    def set_phase(self, phase: str):
        """Set the current phase for logging."""
        self.current_phase = phase
        if phase not in self.phase_data:
            self.phase_data[phase] = []
    
    def log(self, message: str, phase: Optional[str] = None, level: str = "info"):
        """Log a message with phase context."""
        phase = phase or self.current_phase or 'general'
        color = self.PHASE_COLORS.get(phase, 'white')
        
        # Format: [PHASE] message
        formatted_msg = f"[{phase.upper()}] {message}"
        
        if level == "error":
            self.console.print(f"[red]{formatted_msg}[/red]")
        elif level == "warning":
            self.console.print(f"[yellow]{formatted_msg}[/yellow]")
        elif level == "success":
            self.console.print(f"[green]{formatted_msg}[/green]")
        else:
            self.console.print(f"[{color}]{formatted_msg}[/{color}]")
    
    def phase_banner(self, phase: str, title: str, details: Optional[str] = None):
        """Create a phase banner."""
        color = self.PHASE_COLORS.get(phase, 'white')
        
        banner_text = f"[{phase.upper()}] {title}"
        if details:
            banner_text += f"\n{details}"
            
        panel = Panel(
            banner_text,
            title=f"PHASE: {phase.upper()}",
            border_style=color,
            box=box.ROUNDED
        )
        self.console.print(panel)

    def phase_rule(self, phase: str, text: str):
        """Horizontal rule style announcement for phase begin/end."""
        color = self.PHASE_COLORS.get(phase, 'white')
        try:
            self.console.rule(Text.from_markup(f"[{color}]{phase.upper()} — {text}[/{color}]"), style=color)
        except Exception:
            self.console.rule(f"{phase.upper()} — {text}", style=color)

    def phase_start(self, phase: str, details: Optional[str] = None):
        self.set_phase(phase)
        self.phase_rule(phase, "BEGIN")
        if details:
            self.log(details, phase)

    def phase_end(self, phase: str, details: Optional[str] = None):
        self.set_phase(phase)
        self.phase_rule(phase, "END")
        if details:
            self.log(details, phase)
    
    def phase_table(self, phase: str, title: str, headers: List[str], rows: List[List[Any]]):
        """Create a phase-specific table."""
        color = self.PHASE_COLORS.get(phase, 'white')
        
        table = Table(title=f"[{phase.upper()}] {title}", box=box.ROUNDED)
        table.border_style = color
        
        # Add headers
        for header in headers:
            table.add_column(header, style=color)
        
        # Add rows
        for row in rows:
            table.add_row(*[str(item) for item in row])
        
        self.console.print(table)
    
    def phase_summary(self, phase: str, data: Dict[str, Any]):
        """Create a phase summary panel."""
        color = self.PHASE_COLORS.get(phase, 'white')
        
        # Create key-value pairs
        kv_items = []
        for key, value in data.items():
            kv_items.append(f"{key}: {value}")
        
        summary_text = "\n".join(kv_items)
        
        panel = Panel(
            summary_text,
            title=f"[{phase.upper()}] Summary",
            border_style=color,
            box=box.ROUNDED
        )
        self.console.print(panel)
    
    def grouped_info(self, phase: str, title: str, sections: Dict[str, Dict[str, Any]]):
        """Display grouped information in a clean format."""
        color = self.PHASE_COLORS.get(phase, 'white')
        
        # Create a table with sections
        table = Table(title=f"[{phase.upper()}] {title}", box=box.ROUNDED)
        table.border_style = color
        table.add_column("Section", style=color, width=20)
        table.add_column("Details", style="white")
        
        for section_name, section_data in sections.items():
            details = []
            for key, value in section_data.items():
                details.append(f"{key}: {value}")
            
            table.add_row(section_name, "\n".join(details))
        
        self.console.print(table)
    
    def compact_status(self, phase: str, status: str, details: Optional[Dict[str, Any]] = None):
        """Display compact status information."""
        color = self.PHASE_COLORS.get(phase, 'white')
        
        status_text = f"[{phase.upper()}] {status}"
        if details:
            detail_str = " | ".join([f"{k}: {v}" for k, v in details.items()])
            status_text += f" | {detail_str}"
        
        self.console.print(f"[{color}]{status_text}[/{color}]")
    
    def error(self, message: str, phase: Optional[str] = None):
        """Log an error message."""
        self.log(message, phase, "error")
    
    def warning(self, message: str, phase: Optional[str] = None):
        """Log a warning message."""
        self.log(message, phase, "warning")
    
    def success(self, message: str, phase: Optional[str] = None):
        """Log a success message."""
        self.log(message, phase, "success")

# Global instance
phase_logger = PhaseLogger()

# Convenience functions
def set_phase(phase: str):
    """Set the current phase."""
    phase_logger.set_phase(phase)

def phase_start(phase: str, details: Optional[str] = None):
    """Announce the start of a phase."""
    phase_logger.phase_start(phase, details)

def phase_end(phase: str, details: Optional[str] = None):
    """Announce the end of a phase."""
    phase_logger.phase_end(phase, details)

def log(message: str, phase: Optional[str] = None, level: str = "info"):
    """Log a message."""
    phase_logger.log(message, phase, level)

def phase_banner(phase: str, title: str, details: Optional[str] = None):
    """Create a phase banner."""
    phase_logger.phase_banner(phase, title, details)

def phase_table(phase: str, title: str, headers: List[str], rows: List[List[Any]]):
    """Create a phase-specific table."""
    phase_logger.phase_table(phase, title, headers, rows)

def phase_summary(phase: str, data: Dict[str, Any]):
    """Create a phase summary."""
    phase_logger.phase_summary(phase, data)

def grouped_info(phase: str, title: str, sections: Dict[str, Dict[str, Any]]):
    """Display grouped information."""
    phase_logger.grouped_info(phase, title, sections)

def compact_status(phase: str, status: str, details: Optional[Dict[str, Any]] = None):
    """Display compact status."""
    phase_logger.compact_status(phase, status, details)

def error(message: str, phase: Optional[str] = None):
    """Log an error."""
    phase_logger.error(message, phase)

def warning(message: str, phase: Optional[str] = None):
    """Log a warning."""
    phase_logger.warning(message, phase)

def success(message: str, phase: Optional[str] = None):
    """Log a success."""
    phase_logger.success(message, phase)
