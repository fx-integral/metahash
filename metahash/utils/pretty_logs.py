# ====================================================================== #
# metahash/utils/pretty_logs.py
# Rich-based pretty logging; falls back to plain prints if Rich missing.
# ====================================================================== #

from __future__ import annotations

from typing import Iterable, List, Tuple, Dict, Any

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.text import Text
    from rich import box
    _HAS_RICH = True
except Exception:  # pragma: no cover
    Console = None  # type: ignore
    Table = None    # type: ignore
    Panel = None    # type: ignore
    Text = None     # type: ignore
    box = None      # type: ignore
    _HAS_RICH = False

from metahash.config import PRETTY_LOGS, LOG_TOP_N, MASK_SS58


def _mask(ss58: str) -> str:
    if not MASK_SS58:
        return ss58
    if not ss58 or len(ss58) < 10:
        return ss58
    return f"{ss58[:5]}…{ss58[-4:]}"


class Pretty:
    def __init__(self, enable: bool = True):
        self.enable = bool(enable and _HAS_RICH)
        self.console = Console(log_path=False, highlight=False) if self.enable else None

    # horizontal rule (NEW)
    def rule(self, title: str = ""):
        if self.enable and self.console is not None:
            # allow rich markup in title
            try:
                self.console.rule(Text.from_markup(title) if Text else title)
            except Exception:
                self.console.rule(title)
        else:
            line = "─" * 60
            if title:
                print(f"{line} {title} {line}")
            else:
                print(f"{line*2}")

    # simple log passthrough
    def log(self, msg: str):
        if self.enable and self.console is not None:
            self.console.log(msg)
        else:
            print(msg)

    def banner(self, title: str, subtitle: str = "", style: str = "bold cyan"):
        if self.enable and self.console is not None:
            self.console.print(Panel.fit(Text(f"{title}\n{subtitle}", justify="center"), title="status", style=style))
        else:
            print(f"\n=== {title} ===")
            if subtitle:
                print(subtitle)

    def kv_panel(self, title: str, items: Iterable[Tuple[str, Any]], style: str = "bold"):
        if self.enable and self.console is not None:
            body = "\n".join([f"[white]{k}[/white]: {v}" for k, v in items])
            self.console.print(Panel(body, title=title, border_style=style))
        else:
            print(f"\n[{title}]")
            for k, v in items:
                print(f"  - {k}: {v}")

    def table(self, title: str, columns: List[str], rows: List[List[Any]], caption: str | None = None):
        rows = rows[:LOG_TOP_N]
        if self.enable and self.console is not None:
            t = Table(title=title, box=box.MINIMAL_DOUBLE_HEAD if box else None, show_lines=False)
            for c in columns:
                t.add_column(c)
            for r in rows:
                t.add_row(*[str(x) for x in r])
            if caption:
                t.caption = caption
            self.console.print(t)
        else:
            print(f"\n{title}")
            print(" | ".join(columns))
            for r in rows:
                print(" | ".join([str(x) for x in r]))

    # Convenience formatters
    def show_master_shares(self, shares: List[Tuple[str, int, float, float]]):
        # (hotkey, uid, stake, budget_alpha)
        if not shares:
            self.log("[yellow]No active masters meet the threshold this epoch.[/yellow]")
            return
        rows = [[_mask(hk), uid, f"{st:.3f}", f"{bud:.3f} α"] for hk, uid, st, bud in shares]
        self.table("Active Masters (stake & budget)", ["Hotkey", "UID", "Stake α", "Budget α"], rows)

    def show_caps(self, caps_by_ck: Dict[str, float]):
        rows = sorted([(v, k) for k, v in caps_by_ck.items()], reverse=True)
        rows = [[_mask(k), f"{v:.3f} α"] for v, k in rows]
        if rows:
            self.table("Per-Coldkey Caps (this clearing)", ["Coldkey", "Cap α"], rows)

    def show_reputation(self, rep: Dict[str, float]):
        rows = sorted([(v, k) for k, v in rep.items()], reverse=True)
        rows = [[_mask(k), f"{v:.3f}"] for v, k in rows]
        if rows:
            self.table("Reputation Snapshot (top)", ["Coldkey", "R"], rows)

    def show_winners(self, winners_aggr: List[Tuple[str, float]]):
        rows = sorted(winners_aggr, key=lambda x: x[1], reverse=True)
        rows = [[_mask(ck), f"{amt:.3f} α"] for ck, amt in rows]
        if rows:
            self.table("Winners (allocated α by coldkey)", ["Coldkey", "Allocated α"], rows)

    def show_offenders(self, partial: List[str], nopay: List[str]):
        rows = []
        for ck in partial:
            rows.append([_mask(ck), "PARTIAL"])
        for ck in nopay:
            rows.append([_mask(ck), "NO-PAY"])
        if rows:
            self.table("Offenders (this settlement)", ["Coldkey", "Reason"], rows)

    def show_settlement_window(self, epoch: int, start_blk: int, end_blk: int, plus_delay: int, snapshots: int):
        cap = f"commitments={snapshots}  window=[{start_blk}..{end_blk}] (+{plus_delay})"
        self.banner(f"Settlement for epoch {epoch}", cap, style="bold green")

    def show_burn(self, burn_value_tao: float):
        self.kv_panel("Burn", [("Value (TAO-equivalent)", f"{burn_value_tao:.6f}")], style="bold red")


pretty = Pretty(enable=PRETTY_LOGS)
