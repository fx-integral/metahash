#!/usr/bin/env python3
# ╭────────────────────────────────────────────────────────────────────╮
#  neurons/miner.py — Event-driven multi-line bidder (v2)              #
#  • Reads bid lines from bittensor_config:                             #
#      --miner.bids.netuids 30 30 --miner.bids.amounts 1000 500        #
#      --miner.bids.discounts 10 5                                      #
#  • Pays via Bittensor stake transfer (no HTTP).                       #
#  • Persists **miner-only** state to ./miner_state.json.               #
#  • Fully event-driven:                                                #
#      - AuctionStart: retry unpaid wins, then send bids for this epoch #
#      - WinSynapse:   pay immediately                                   #
# ╰────────────────────────────────────────────────────────────────────╯

from __future__ import annotations

import json
import time
from dataclasses import dataclass, asdict
from math import isfinite
from pathlib import Path
from typing import Dict, List, Tuple

import bittensor as bt

from metahash.base.miner import BaseMinerNeuron
from metahash.base.utils.logging import ColoredLogger as clog
from metahash.protocol import AuctionStartSynapse, BidSynapse, AckSynapse, WinSynapse
from metahash.config import PLANCK, AUCTION_BUDGET_ALPHA, S_MIN_ALPHA_MINER, LOG_TOP_N
from metahash.utils.pretty_logs import pretty  # Rich (fallback-safe)
from metahash.utils.wallet_utils import transfer_alpha  # wraps Subtensor transfer_stake


# ─────────────────────────── data models ────────────────────────────── #

@dataclass
class BidLine:
    subnet_id: int
    alpha: float            # requested α
    discount_bps: int       # 0..10000


@dataclass
class WinInvoice:
    """Persisted invoice from a master validator."""
    validator_hotkey: str
    treasury_coldkey: str
    subnet_id: int
    alpha: float
    discount_bps: int
    deadline_block: int
    amount_rao: int
    epoch_seen: int
    paid: bool = False
    pay_attempts: int = 0
    last_attempt_ts: float = 0.0
    last_response: str = ""


class Miner(BaseMinerNeuron):
    """
    Event-driven miner:
      • auctionstart_forward(): retry unpaid invoices then send bids for this epoch.
      • win_forward(): pay invoice immediately with transfer_stake.
    Reads bid lines from config.miner.bids.{netuids,amounts,discounts}.
    State file: ./miner_state.json
    """

    LATE_GRACE_BLOCKS = 0  # do not pay past deadline (validators won’t count it)

    # ------------------------------ construction ---------------------------- #
    def __init__(self, config=None):
        super().__init__(config=config)

        # ---- miner-local state (file-backed) --------------------------------
        self._state_file = Path("miner_state.json")
        self._treasuries: Dict[str, str] = {}                # validator_hot → treasury
        self._already_bid: Dict[str, List[Tuple[int, int, float, int]]] = {}
        # per validator_hot: list of (epoch, subnet, alpha, discount_bps)
        self._wins: List[WinInvoice] = []

        self._load_state_file()

        self.lines: List[BidLine] = self._build_lines_from_config()

        pretty.banner(
            "Miner started",
            f"uid={self.uid} | hotkey={self.wallet.hotkey.ss58_address} | lines={len(self.lines)}",
            style="bold magenta",
        )
        self._log_cfg_summary()

    # ------------------------------- state I/O ------------------------------- #
    def _load_state_file(self):
        if self._state_file.exists():
            try:
                data = json.loads(self._state_file.read_text())
                self._treasuries = dict(data.get("treasuries", {}))
                self._already_bid = {
                    k: [tuple(x) for x in v] for k, v in data.get("already_bid", {}).items()
                }
                self._wins = [WinInvoice(**w) for w in data.get("wins", [])]
            except Exception as e:
                clog.warning(f"[state] could not load state: {e}", color="yellow")

    def _save_state_file(self):
        tmp = self._state_file.with_suffix(".tmp")
        payload = {
            "treasuries": self._treasuries,
            "already_bid": self._already_bid,
            "wins": [asdict(w) for w in self._wins],
        }
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
        tmp.replace(self._state_file)

    # Optionally persist on BaseNeuron.sync cadence
    def save_state(self):
        self._save_state_file()

    # --------------------------- bid-line builders --------------------------- #
    def _parse_discount_token(self, tok: str) -> int:
        """Accept '10' (→ 1000bps), '10.5' (→ 1050bps), '1000bps' (→ 1000bps)."""
        t = str(tok).strip().lower().replace("%", "")
        if t.endswith("bps"):
            try:
                bps = int(float(t[:-3]))
                return max(0, min(10_000, bps))
            except Exception:
                pass
        try:
            val = float(t)
            if val > 100:  # assume it's bps already
                return max(0, min(10_000, int(round(val))))
            return max(0, min(10_000, int(round(val * 100))))
        except Exception:
            raise ValueError(f"Invalid discount token: {tok!r}")

    def _build_lines_from_config(self) -> List[BidLine]:
        bids = getattr(self.config, "miner", None)
        nets = getattr(getattr(bids, "bids", bids), "netuids", []) if bids else []
        amts = getattr(getattr(bids, "bids", bids), "amounts", []) if bids else []
        discs = getattr(getattr(bids, "bids", bids), "discounts", []) if bids else []

        netuids = [int(x) for x in list(nets or [])]
        amounts = [float(x) for x in list(amts or [])]
        discounts = [self._parse_discount_token(str(x)) for x in list(discs or [])]

        if not (len(netuids) == len(amounts) == len(discounts)):
            raise ValueError("miner.bids.* lengths must match (netuids, amounts, discounts)")

        lines: List[BidLine] = []
        for sid, amt, disc in zip(netuids, amounts, discounts):
            if amt <= 0:
                pretty.log(f"[yellow]Skipping non-positive amount: {amt}[/yellow]")
                continue
            if disc < 0 or disc > 10_000:
                pretty.log(f"[yellow]Skipping invalid discount: {disc} bps[/yellow]")
                continue
            lines.append(BidLine(subnet_id=int(sid), alpha=float(amt), discount_bps=int(disc)))
        return lines

    # -------------------------------- helpers -------------------------------- #
    def _log_cfg_summary(self):
        rows = [[i, ln.subnet_id, f"{ln.alpha:.4f} α", f"{ln.discount_bps} bps"]
                for i, ln in enumerate(self.lines)]
        if rows:
            pretty.table("Configured Bid Lines", ["#", "Subnet", "Alpha", "Discount"], rows)

    def _status_tables(self):
        pend = [w for w in self._wins if not w.paid]
        done = [w for w in self._wins if w.paid]
        rows_pend = [
            [w.validator_hotkey[:8] + "…", w.subnet_id, f"{w.alpha:.4f} α",
             f"{w.discount_bps} bps", w.deadline_block, f"{w.amount_rao/PLANCK:.4f} α", w.pay_attempts]
            for w in sorted(pend, key=lambda x: (x.deadline_block, x.validator_hotkey))[:LOG_TOP_N]
        ]
        rows_done = [
            [w.validator_hotkey[:8] + "…", w.subnet_id, f"{w.alpha:.4f} α",
             f"{w.discount_bps} bps", w.deadline_block, f"{w.amount_rao/PLANCK:.4f} α", w.pay_attempts]
            for w in sorted(done, key=lambda x: (-x.last_attempt_ts))[:LOG_TOP_N]
        ]
        if rows_pend:
            pretty.table("Pending Wins (to pay)", ["Validator", "Subnet", "Alpha", "Disc", "Deadline", "Amount", "Attempts"], rows_pend)
        if rows_done:
            pretty.table("Paid Wins (recent)", ["Validator", "Subnet", "Alpha", "Disc", "Deadline", "Amount", "Attempts"], rows_done)

    def _remember_bid(self, validator_hot: str, epoch: int, subnet_id: int, alpha: float, discount_bps: int):
        rec = (int(epoch), int(subnet_id), float(alpha), int(discount_bps))
        self._already_bid.setdefault(validator_hot, [])
        if rec not in self._already_bid[validator_hot]:
            self._already_bid[validator_hot].append(rec)

    def _has_bid(self, validator_hot: str, epoch: int, subnet_id: int, alpha: float, discount_bps: int) -> bool:
        rec = (int(epoch), int(subnet_id), float(alpha), int(discount_bps))
        return rec in self._already_bid.get(validator_hot, [])

    # ---------------------------- auction handlers --------------------------- #
    async def auctionstart_forward(self, synapse: AuctionStartSynapse) -> AuctionStartSynapse:
        """
        At the head of epoch E from a master validator:
          1) Retry any unpaid wins (before their deadlines).
          2) Send this epoch’s configured bids to this master (once per line).
        """
        validator_hot = getattr(synapse, "caller_hotkey", None) or ""
        epoch = int(synapse.epoch_index)

        # remember treasury for future invoices
        try:
            if synapse.treasury_coldkey:
                self._treasuries[validator_hot] = synapse.treasury_coldkey
                self._save_state_file()
        except Exception:
            pass

        pretty.kv_panel(
            "AuctionStart received",
            [
                ("validator", validator_hot),
                ("epoch", epoch),
                ("budget α", f"{getattr(synapse, 'auction_budget_alpha', 0):.3f}"),
                ("min_stake", f"{synapse.min_stake_alpha:.3f} α"),
            ],
            style="bold cyan",
        )

        # (1) Retry unpaid invoices (all validators)
        await self._retry_unpaid_invoices()

        # Stake gate
        my_stake = float(self.metagraph.stake[self.uid])
        if my_stake < float(synapse.min_stake_alpha or S_MIN_ALPHA_MINER):
            pretty.log("[yellow]Stake below S_MIN_ALPHA_MINER – skipping bids to this validator.[/yellow]")
            self._status_tables()
            return synapse

        # (2) Send bids to this master
        axon = next((ax for ax in self.metagraph.axons if ax.hotkey == validator_hot), None)
        if axon is None:
            clog.warning("Origin validator axon not found.", color="red")
            self._status_tables()
            return synapse

        sent = 0
        for ln in self.lines:
            if not isfinite(ln.alpha) or ln.alpha <= 0 or ln.alpha > AUCTION_BUDGET_ALPHA:
                pretty.log(f"[yellow]Invalid alpha {ln.alpha} for subnet {ln.subnet_id} – skipping line.[/yellow]")
                continue
            if not (0 <= ln.discount_bps <= 10_000):
                pretty.log(f"[yellow]Invalid discount {ln.discount_bps} bps – skipping line.[/yellow]")
                continue

            if self._has_bid(validator_hot, epoch, ln.subnet_id, ln.alpha, ln.discount_bps):
                continue  # already sent this exact line to this master+epoch

            bid = BidSynapse(subnet_id=ln.subnet_id, alpha=ln.alpha, discount_bps=ln.discount_bps)
            try:
                ack = await self.dendrite(axons=[axon], synapse=bid, deserialize=True, timeout=8)
            except Exception as e:
                pretty.log(f"[red]Bid RPC failed: {e}[/red]")
                continue

            if isinstance(ack, AckSynapse) and ack.accepted:
                self._remember_bid(validator_hot, epoch, ln.subnet_id, ln.alpha, ln.discount_bps)
                self._save_state_file()
                pretty.log(f"[green]Bid sent[/green] validator={validator_hot[:8]}… subnet={ln.subnet_id} alpha={ln.alpha:.4f} disc={ln.discount_bps}bps")
                sent += 1
            else:
                err = getattr(ack, "error", "<unknown>") if isinstance(ack, AckSynapse) else "<no-ack>"
                pretty.log(f"[yellow]Bid rejected[/yellow] validator={validator_hot[:8]}… subnet={ln.subnet_id} reason={err}")

        if sent == 0:
            pretty.log("[grey]No bids were sent (all lines either invalid or already sent).[/grey]")

        self._status_tables()
        return synapse

    async def win_forward(self, synapse: WinSynapse) -> WinSynapse:
        """Receive invoice for a won line; pay immediately via transfer_stake."""
        validator_hot = getattr(synapse, "caller_hotkey", None) or ""
        treasury_ck = self._treasuries.get(validator_hot, "")

        if not treasury_ck:
            pretty.log("[red]Treasury unknown for this validator – cannot pay.[/red]")
            return synapse

        pay_alpha = float(synapse.alpha) * (1 - synapse.clearing_discount_bps / 10_000)
        amount_rao = int(round(pay_alpha * PLANCK))

        inv = WinInvoice(
            validator_hotkey=validator_hot,
            treasury_coldkey=treasury_ck,
            subnet_id=int(synapse.subnet_id),
            alpha=float(synapse.alpha),
            discount_bps=int(synapse.clearing_discount_bps),
            deadline_block=int(synapse.pay_deadline_block),
            amount_rao=amount_rao,
            epoch_seen=int(getattr(self, "epoch_index", 0)),
        )
        # idempotent merge
        for w in self._wins:
            if (
                w.validator_hotkey == inv.validator_hotkey
                and w.subnet_id == inv.subnet_id
                and w.alpha == inv.alpha
                and w.discount_bps == inv.discount_bps
                and w.deadline_block == inv.deadline_block
            ):
                inv = w
                break
        else:
            self._wins.append(inv)
            self._save_state_file()

        pretty.kv_panel(
            "Win received",
            [
                ("validator", validator_hot),
                ("subnet", inv.subnet_id),
                ("alpha_won", f"{inv.alpha:.4f} α"),
                ("discount", f"{inv.discount_bps} bps"),
                ("deadline", inv.deadline_block),
                ("amount_to_pay", f"{inv.amount_rao/PLANCK:.4f} α"),
            ],
            style="bold green",
        )

        await self._attempt_payment(inv)
        self._status_tables()
        return synapse

    # ----------------------------- payments ----------------------------- #
    async def _attempt_payment(self, inv: WinInvoice):
        """Pay a single invoice now (idempotent)."""
        if inv.paid:
            return

        blk = self.block
        if blk > inv.deadline_block + self.LATE_GRACE_BLOCKS:
            pretty.log(f"[yellow]Skipping late payment (block {blk} > deadline {inv.deadline_block}+{self.LATE_GRACE_BLOCKS}).[/yellow]")
            return

        try:
            ok = await transfer_alpha(
                subtensor=self.subtensor,
                wallet=self.wallet,
                hotkey_ss58=self.wallet.hotkey.ss58_address,  # pay from our own hotkey
                origin_and_dest_netuid=inv.subnet_id,
                dest_coldkey_ss58=inv.treasury_coldkey,
                amount=bt.Balance.from_rao(inv.amount_rao),
                wait_for_inclusion=True,
                wait_for_finalization=False,
            )
            resp = "ok" if ok else "rejected"
        except Exception as exc:
            ok = False
            resp = f"exception: {exc}"

        inv.pay_attempts += 1
        inv.last_attempt_ts = time.time()
        inv.last_response = str(resp)[:300]
        if ok:
            inv.paid = True
            pretty.log(f"[green]Payment OK[/green] {inv.amount_rao/PLANCK:.4f} α → {inv.treasury_coldkey[:8]}…")
        else:
            pretty.log(f"[red]Payment FAILED[/red] resp={resp}")

        self._save_state_file()

    async def _retry_unpaid_invoices(self):
        """Try to pay any stored unpaid invoices still before their deadlines."""
        pend = [w for w in self._wins if not w.paid]
        if not pend:
            return
        pretty.log(f"[cyan]Retrying {len(pend)} unpaid invoice(s) at epoch head…[/cyan]")
        for w in pend:
            await self._attempt_payment(w)


# ╭────────────────── keep‑alive (event-driven) ─────────────────────────╮
if __name__ == "__main__":
    from metahash.bittensor_config import config
    with Miner(config=config()) as m:
        # Keep process alive; all logic runs in axon handlers.
        import time as _t
        while True:
            clog.info("Miner running…", color="gray")
            _t.sleep(120)
