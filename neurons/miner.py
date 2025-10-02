#!/usr/bin/env python3
# neurons/miner.py

from pathlib import Path

from bittensor import Synapse

from metahash.base.miner import BaseMinerNeuron
from metahash.base.utils.logging import ColoredLogger as clog
from metahash.protocol import AuctionStartSynapse, WinSynapse
from metahash.treasuries import VALIDATOR_TREASURIES
from metahash.utils.pretty_logs import pretty
from metahash.utils.wallet_utils import unlock_wallet

# Compact components
from metahash.miner.state import StateStore
from metahash.miner.runtime import Runtime
from metahash.miner.payments import Payments


class Miner(BaseMinerNeuron):
    """
    Thin orchestrator:
      - Per-coldkey state scoping (safe fallback name if dir fails)
      - Creates Runtime (auction + chain helpers) and Payments (background loop + workers)
      - Delegates protocol handlers
    """

    def __init__(self, config=None):
        super().__init__(config=config)
          
        # Wallet unlock (best-effort)
        unlock_wallet(wallet=self.wallet)

        # ---------------------- Per-coldkey state directory ----------------------
        self._coldkey_ss58: str = getattr(getattr(self.wallet, "coldkey", None), "ss58_address", "") or "unknown_coldkey"
        desired_state_dir: Path = Path("miner_state") / self._coldkey_ss58
        try:
            desired_state_dir.mkdir(parents=True, exist_ok=True)
            state_path = desired_state_dir / "miner_state.json"
            self._state_dir = desired_state_dir
        except Exception:
            # Fallback to cwd but still keep per-coldkey filename to avoid clobbering
            self._state_dir = Path(".")
            state_path = Path(f"miner_state_{self._coldkey_ss58}.json")

        # ---------------------- StateStore ----------------------
        self.state = StateStore(state_path)

        # Optional fresh start
        if getattr(self.config, "fresh", False):
            self.state.wipe()
            pretty.log(f"[magenta]Fresh start requested: cleared local state for coldkey {self._coldkey_ss58}.[/magenta]")

        self.state.load()
        self.state.treasuries = dict(VALIDATOR_TREASURIES)  # re-pin allowlist

        # ---------------------- Runtime & Payments ----------------------
        self.runtime = Runtime(
            config=self.config,
            wallet=self.wallet,
            metagraph=self.metagraph,
            state=self.state,
        )

        self.payments = Payments(
            config=self.config,
            wallet=self.wallet,
            runtime=self.runtime,  # gives access to chain client + locks + block, balances, transfer
            state=self.state,
        )

        # Build bid lines from config and show summary
        self.lines = self.runtime.build_lines_from_config()
        self.runtime.log_cfg_summary(self.lines)

        pretty.banner(
            "Miner started",
            (
                f"uid={self.uid} | coldkey={self._coldkey_ss58} | "
                f"hotkey={self.wallet.hotkey.ss58_address} | lines={len(self.lines)} | "
                f"epoch(e)={getattr(self, 'epoch_index', 0)}"
            ),
            style="bold magenta",
        )
        pretty.kv_panel(
            "[magenta]State scope[/magenta]",
            [("state_dir", str(self._state_dir)), ("state_file", str(self.state.path))],
            style="bold magenta",
        )
        pretty.kv_panel(
            "[magenta]Local treasuries loaded[/magenta]",
            [("allowlisted_validators", len(self.state.treasuries))],
            style="bold magenta",
        )

        # Eager schedule unpaid invoices + watchdog
        try:
            self.payments.ensure_payment_config()
            self.payments.schedule_unpaid_pending()
            pretty.log("[green]Startup: eagerly scheduled unpaid invoices.[/green]")
        except Exception as _e:
            pretty.log(f"[yellow]Eager scheduling skipped:[/yellow] {_e}")

        self.payments.start_background_tasks()

    # ---------------------- Protocol handlers ----------------------

    async def auctionstart_forward(self, synapse: AuctionStartSynapse) -> AuctionStartSynapse:
        # delegate to runtime (includes per-target-subnet stake gate)
        return await self.runtime.handle_auction_start(synapse, self.lines)

    async def win_forward(self, synapse: WinSynapse) -> WinSynapse:
        # delegate to payments (schedule + worker)
        return await self.payments.handle_win(synapse)

    async def forward(self, synapse: Synapse):
        return synapse

    # ---------------------- Context manager shutdown ----------------------

    def __exit__(self, exc_type, exc, tb):
        try:
            self.payments.shutdown_background()
        finally:
            return super().__exit__(exc_type, exc, tb)


if __name__ == "__main__":
    from metahash.bittensor_config import config
    with Miner(config=config(role="miner")) as m:
        import time as _t
        while True:
            clog.info("Miner running…", color="gray")
            _t.sleep(120)
