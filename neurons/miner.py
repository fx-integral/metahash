#!/usr/bin/env python3
# neurons/miner.py — Event-driven multi-line bidder (v2, no local dendrite bidding)

import asyncio
import hashlib
import bittensor as bt
import json
import time
from dataclasses import dataclass, asdict
from math import isfinite
from pathlib import Path
from typing import Dict, List, Tuple, Optional

from bittensor import Synapse
from metahash.base.miner import BaseMinerNeuron
from metahash.base.utils.logging import ColoredLogger as clog
from metahash.protocol import AuctionStartSynapse, WinSynapse
from metahash.config import PLANCK, AUCTION_BUDGET_ALPHA, S_MIN_ALPHA_MINER, LOG_TOP_N
from metahash.utils.pretty_logs import pretty
from metahash.utils.wallet_utils import transfer_alpha


@dataclass
class BidLine:
    subnet_id: int
    alpha: float
    discount_bps: int  # 0..10000


@dataclass
class WinInvoice:
    # identity
    invoice_id: str = ""                      # <- derived, filled automatically for new items
    validator_key: str = ""                   # hotkey if known else "uid:<n>"
    treasury_coldkey: str = ""
    # bid / allocation
    subnet_id: int = 0
    alpha: float = 0.0
    discount_bps: int = 0
    # chain settlement
    deadline_block: int = 0
    amount_rao: int = 0
    # context
    epoch_seen: int = 0
    # payment progress
    paid: bool = False
    pay_attempts: int = 0
    last_attempt_ts: float = 0.0
    last_response: str = ""


class Miner(BaseMinerNeuron):
    LATE_GRACE_BLOCKS = 0  # do not pay past deadline (validators won’t count it)

    def __init__(self, config=None):
        super().__init__(config=config)

        self._state_file = Path("miner_state.json")
        self._treasuries: Dict[str, str] = {}  # validator_key -> treasury coldkey
        self._already_bid: Dict[str, List[Tuple[int, int, float, int]]] = {}  # validator_key -> list of (epoch, subnet, alpha, discount_bps)
        self._wins: List[WinInvoice] = []

        # Payment guard (prevents concurrent ws recv)
        self._pay_lock: asyncio.Lock = asyncio.Lock()

        # Optional: start fresh
        if getattr(self.config, "fresh", False):
            self._wipe_state()
            pretty.log("[magenta]Fresh start requested: cleared local miner state.[/magenta]")

        self._load_state_file()
        self.lines: List[BidLine] = self._build_lines_from_config()

        pretty.banner(
            "Miner started",
            f"uid={self.uid} | hotkey={self.wallet.hotkey.ss58_address} | lines={len(self.lines)} | epoch={getattr(self, 'epoch_index', 0)}",
            style="bold magenta",
        )
        self._log_cfg_summary()

    # ------------- state I/O -------------

    def _wipe_state(self):
        self._treasuries.clear()
        self._already_bid.clear()
        self._wins.clear()
        try:
            if self._state_file.exists():
                self._state_file.unlink()
        except Exception as e:
            clog.warning(f"[state] could not remove state file: {e}", color="yellow")

    def _make_invoice_id(
        self,
        validator_key: str,
        epoch: int,
        subnet_id: int,
        alpha: float,
        discount_bps: int,
        deadline_block: int,
        amount_rao: int,
    ) -> str:
        payload = f"{validator_key}|{epoch}|{subnet_id}|{alpha:.12f}|{discount_bps}|{deadline_block}|{amount_rao}"
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]

    def _make_bid_id(self, validator_key: str, epoch: int, subnet_id: int, alpha: float, discount_bps: int) -> str:
        payload = f"{validator_key}|{epoch}|{subnet_id}|{alpha:.12f}|{discount_bps}"
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:10]

    def _load_state_file(self):
        """
        Robust loader:
        - tolerates extra legacy keys (e.g., 'validator_hotkey')
        - maps 'validator_hotkey' -> 'validator_key' when present
        - backfills missing 'invoice_id'
        """
        if not self._state_file.exists():
            return
        try:
            data = json.loads(self._state_file.read_text())
        except Exception as e:
            clog.warning(f"[state] could not load state (json): {e}", color="yellow")
            return

        self._treasuries = dict(data.get("treasuries", {}))

        self._already_bid = {
            k: [tuple(x) for x in v] for k, v in data.get("already_bid", {}).items()
        }

        self._wins = []
        for w in data.get("wins", []):
            try:
                # Map legacy field names
                if "validator_key" not in w and "validator_hotkey" in w:
                    w["validator_key"] = w.pop("validator_hotkey", "")

                # Keep only fields known to WinInvoice
                allowed = set(WinInvoice.__dataclass_fields__.keys())
                clean = {k: v for k, v in w.items() if k in allowed}

                # Backfill invoice_id if missing
                if not clean.get("invoice_id"):
                    inv_id = self._make_invoice_id(
                        clean.get("validator_key", ""),
                        int(clean.get("epoch_seen", 0) or 0),
                        int(clean.get("subnet_id", 0) or 0),
                        float(clean.get("alpha", 0.0) or 0.0),
                        int(clean.get("discount_bps", 0) or 0),
                        int(clean.get("deadline_block", 0) or 0),
                        int(clean.get("amount_rao", 0) or 0),
                    )
                    clean["invoice_id"] = inv_id

                self._wins.append(WinInvoice(**clean))
            except Exception as e:
                clog.warning(f"[state] skipping malformed win entry: {e}", color="yellow")

    def _save_state_file(self):
        tmp = self._state_file.with_suffix(".tmp")
        payload = {
            "treasuries": self._treasuries,
            "already_bid": self._already_bid,
            "wins": [asdict(w) for w in self._wins],
        }
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
        tmp.replace(self._state_file)

    def save_state(self):
        self._save_state_file()

    # ------------- helpers -------------

    def _parse_discount_token(self, tok: str) -> int:
        t = str(tok).strip().lower().replace("%", "")
        if t.endswith("bps"):
            try:
                bps = int(float(t[:-3]))
                return max(0, min(10_000, bps))
            except Exception:
                pass
        val = float(t)
        if val > 100:
            return max(0, min(10_000, int(round(val))))
        return max(0, min(10_000, int(round(val * 100))))

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

    def _log_cfg_summary(self):
        rows = [[i, ln.subnet_id, f"{ln.alpha:.4f} α", f"{ln.discount_bps} bps"] for i, ln in enumerate(self.lines)]
        if rows:
            pretty.table("Configured Bid Lines", ["#", "Subnet", "Alpha", "Discount"], rows)

    def _status_tables(self):
        now_blk = int(getattr(self, "block", 0))
        pend = [w for w in self._wins if not w.paid]
        done = [w for w in self._wins if w.paid]

        def left_blocks(deadline: int) -> int:
            return max(0, int(deadline) - now_blk)

        rows_pend = [[
            (w.validator_key[:8] + "…"),
            w.epoch_seen,
            w.subnet_id,
            f"{w.alpha:.4f} α",
            f"{w.discount_bps} bps",
            w.deadline_block,
            left_blocks(w.deadline_block),
            f"{w.amount_rao/PLANCK:.4f} α",
            w.pay_attempts,
            (w.invoice_id[:8] + "…"),
            (w.last_response or "")[:24] + ("…" if len(w.last_response or "") > 24 else "")
        ] for w in sorted(pend, key=lambda x: (x.deadline_block, x.validator_key))[:LOG_TOP_N]]

        rows_done = [[
            (w.validator_key[:8] + "…"),
            w.epoch_seen,
            w.subnet_id,
            f"{w.alpha:.4f} α",
            f"{w.discount_bps} bps",
            w.deadline_block,
            left_blocks(w.deadline_block),
            f"{w.amount_rao/PLANCK:.4f} α",
            w.pay_attempts,
            (w.invoice_id[:8] + "…"),
            (w.last_response or "")[:24] + ("…" if len(w.last_response or "") > 24 else "")
        ] for w in sorted(done, key=lambda x: (-x.last_attempt_ts))[:LOG_TOP_N]]

        if rows_pend:
            pretty.table(
                "Pending Wins (to pay)",
                ["Validator", "E", "Subnet", "Alpha", "Disc", "Deadline", "Left", "Amount", "Attempts", "InvID", "LastResp"],
                rows_pend,
            )
        if rows_done:
            pretty.table(
                "Paid Wins (recent)",
                ["Validator", "E", "Subnet", "Alpha", "Disc", "Deadline", "Left", "Amount", "Attempts", "InvID", "LastResp"],
                rows_done,
            )

    def _resolve_caller(self, synapse: Synapse) -> Tuple[Optional[int], str]:
        """Return (uid, hotkey) for caller, best-effort."""
        uid = None
        try:
            uid = int(getattr(getattr(synapse, "dendrite", None), "origin", None))
        except Exception:
            uid = None
        hk = None
        if uid is not None and 0 <= uid < len(self.metagraph.axons):
            hk = getattr(self.metagraph.axons[uid], "hotkey", None)
        if not hk and hasattr(self.metagraph, "hotkeys") and uid is not None and uid < len(self.metagraph.hotkeys):
            hk = self.metagraph.hotkeys[uid]
        if not hk:
            hk = getattr(synapse, "caller_hotkey", None)
        return uid, hk or ""

    def _validator_key(self, uid: Optional[int], hotkey: str) -> str:
        return hotkey if hotkey else (f"uid:{uid}" if uid is not None else "<unknown>")

    def _remember_bid(self, validator_key: str, epoch: int, subnet_id: int, alpha: float, discount_bps: int):
        rec = (int(epoch), int(subnet_id), float(alpha), int(discount_bps))
        self._already_bid.setdefault(validator_key, [])
        if rec not in self._already_bid[validator_key]:
            self._already_bid[validator_key].append(rec)

    def _has_bid(self, validator_key: str, epoch: int, subnet_id: int, alpha: float, discount_bps: int) -> bool:
        rec = (int(epoch), int(subnet_id), float(alpha), int(discount_bps))
        return rec in self._already_bid.get(validator_key, [])

    # ------------- auction handlers -------------

    async def auctionstart_forward(self, synapse: AuctionStartSynapse) -> AuctionStartSynapse:
        uid, caller_hot = self._resolve_caller(synapse)
        vkey = self._validator_key(uid, caller_hot)
        epoch = int(synapse.epoch_index)

        # remember treasury for future invoices
        try:
            if synapse.treasury_coldkey:
                self._treasuries[vkey] = synapse.treasury_coldkey
                self._save_state_file()
        except Exception:
            pass

        pretty.kv_panel(
            "AuctionStart received",
            [
                ("validator", caller_hot or vkey),
                ("epoch", epoch),
                ("budget α", f"{getattr(synapse, 'auction_budget_alpha', 0):.3f}"),
                ("min_stake", f"{synapse.min_stake_alpha:.3f} α"),
            ],
            style="bold cyan",
        )

        # (1) Retry unpaid invoices (all validators)
        pre_unpaid = len([w for w in self._wins if not w.paid])
        await self._retry_unpaid_invoices()

        # Stake gate
        my_stake = float(self.metagraph.stake[self.uid])
        if my_stake < float(synapse.min_stake_alpha or S_MIN_ALPHA_MINER):
            pretty.log(f"[yellow]Stake below S_MIN_ALPHA_MINER – not bidding to this validator (epoch {epoch}).[/yellow]")
            self._status_tables()
            synapse.ack = True
            synapse.bids = []
            synapse.bids_sent = 0
            synapse.retries_attempted = pre_unpaid
            synapse.note = "stake gate"
            return synapse

        # (2) Build bids in the same synapse (no dendrite here)
        out_bids: List[Dict[str, int | float | str]] = []
        sent = 0
        rows_sent = []
        for ln in self.lines:
            if not isfinite(ln.alpha) or ln.alpha <= 0 or ln.alpha > AUCTION_BUDGET_ALPHA:
                pretty.log(f"[yellow]Invalid alpha {ln.alpha} for subnet {ln.subnet_id} – skipping line.[/yellow]")
                continue
            if not (0 <= ln.discount_bps <= 10_000):
                pretty.log(f"[yellow]Invalid discount {ln.discount_bps} bps – skipping line.[/yellow]")
                continue
            if self._has_bid(vkey, epoch, ln.subnet_id, ln.alpha, ln.discount_bps):
                continue

            bid_id = self._make_bid_id(vkey, epoch, ln.subnet_id, ln.alpha, ln.discount_bps)
            out_bids.append({
                "subnet_id": int(ln.subnet_id),
                "alpha": float(ln.alpha),
                "discount_bps": int(ln.discount_bps),
                # Optional, for traceability; allowed by protocol type (str)
                "bid_id": bid_id,
            })
            self._remember_bid(vkey, epoch, ln.subnet_id, ln.alpha, ln.discount_bps)
            sent += 1
            rows_sent.append([ln.subnet_id, f"{ln.alpha:.4f} α", f"{ln.discount_bps} bps", bid_id, epoch])

        if sent == 0:
            pretty.log("[grey]No bids were added (all lines either invalid or already added).[/grey]")
        else:
            pretty.table("Bids Sent", ["Subnet", "Alpha", "Discount", "BidID", "Epoch"], rows_sent)

        self._save_state_file()
        self._status_tables()

        synapse.ack = True
        synapse.bids = out_bids
        synapse.bids_sent = sent
        synapse.retries_attempted = pre_unpaid
        synapse.note = None
        return synapse

    async def win_forward(self, synapse: WinSynapse) -> WinSynapse:
        uid, caller_hot = self._resolve_caller(synapse)
        vkey = self._validator_key(uid, caller_hot)

        treasury_ck = self._treasuries.get(vkey, "")
        if not treasury_ck:
            pretty.log("[red]Treasury unknown for this validator – cannot pay.[/red]")
            synapse.ack = False
            synapse.payment_attempted = False
            synapse.payment_ok = False
            synapse.attempts = 0
            synapse.last_response = "treasury unknown"
            return synapse

        pay_alpha = float(synapse.alpha) * (1 - synapse.clearing_discount_bps / 10_000)
        amount_rao = int(round(pay_alpha * PLANCK))
        epoch_now = int(getattr(self, "epoch_index", 0))

        inv = WinInvoice(
            validator_key=vkey,
            treasury_coldkey=treasury_ck,
            subnet_id=int(synapse.subnet_id),
            alpha=float(synapse.alpha),
            discount_bps=int(synapse.clearing_discount_bps),
            deadline_block=int(synapse.pay_deadline_block),
            amount_rao=amount_rao,
            epoch_seen=epoch_now,
        )

        # derive invoice_id
        inv.invoice_id = self._make_invoice_id(
            vkey, epoch_now, inv.subnet_id, inv.alpha, inv.discount_bps, inv.deadline_block, inv.amount_rao
        )

        # idempotent merge
        for w in self._wins:
            if (
                w.validator_key == inv.validator_key
                and w.subnet_id == inv.subnet_id
                and w.alpha == inv.alpha
                and w.discount_bps == inv.discount_bps
                and w.deadline_block == inv.deadline_block
                and w.amount_rao == inv.amount_rao
            ):
                inv = w
                break
        else:
            self._wins.append(inv)

        self._save_state_file()

        pretty.kv_panel(
            "Win received",
            [
                ("validator", caller_hot or vkey),
                ("epoch_now", epoch_now),
                ("subnet", inv.subnet_id),
                ("alpha_won", f"{inv.alpha:.4f} α"),
                ("discount", f"{inv.discount_bps} bps"),
                ("deadline", inv.deadline_block),
                ("amount_to_pay", f"{inv.amount_rao/PLANCK:.4f} α"),
                ("invoice_id", inv.invoice_id),
            ],
            style="bold green",
        )

        await self._attempt_payment(inv)
        self._status_tables()

        synapse.ack = True
        synapse.payment_attempted = True
        synapse.payment_ok = inv.paid
        synapse.attempts = inv.pay_attempts
        synapse.last_response = inv.last_response[:300] if inv.last_response else ""
        return synapse

    async def forward(self, synapse: Synapse):
        return synapse

    # ------------- payments -------------

    async def _attempt_payment(self, inv: WinInvoice):
        if inv.paid:
            return

        blk = int(getattr(self, "block", 0))
        if blk > inv.deadline_block + self.LATE_GRACE_BLOCKS:
            pretty.log(f"[yellow]Skipping late payment (block {blk} > deadline {inv.deadline_block}+{self.LATE_GRACE_BLOCKS}).[/yellow]")
            return

        async with self._pay_lock:
            try:
                ok = await transfer_alpha(
                    subtensor=self.subtensor,
                    wallet=self.wallet,
                    hotkey_ss58=self.wallet.hotkey.ss58_address,
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
            pretty.log(
                f"[green]Payment OK[/green] "
                f"{inv.amount_rao/PLANCK:.4f} α → {inv.treasury_coldkey[:8]}… "
                f"(epoch={inv.epoch_seen}, inv={inv.invoice_id})"
            )
        else:
            pretty.log(
                f"[red]Payment FAILED[/red] resp={resp} "
                f"(epoch={inv.epoch_seen}, inv={inv.invoice_id})"
            )

        self._save_state_file()

    async def _retry_unpaid_invoices(self):
        pend = [w for w in self._wins if not w.paid]
        if not pend:
            return
        pretty.log(f"[cyan]Retrying {len(pend)} unpaid invoice(s) at epoch head…[/cyan]")
        for w in pend:
            await self._attempt_payment(w)


if __name__ == "__main__":
    from metahash.bittensor_config import config

    with Miner(config=config(role="miner")) as m:
        import time as _t
        while True:
            clog.info("Miner running…", color="gray")
            _t.sleep(120)
