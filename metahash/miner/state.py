# metahash/miner/state.py
from __future__ import annotations

import os
import json
import tempfile
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Tuple

from metahash.base.utils.logging import ColoredLogger as clog
from metahash.utils.pretty_logs import pretty
from metahash.config import PLANCK, LOG_TOP_N
from metahash.miner.models import WinInvoice
from metahash.miner.logging import (
    MinerPhase, LogLevel, miner_logger, 
    log_init, log_auction, log_commitments, log_settlement
)


class StateStore:
    """
    Per-coldkey persistent state (JSON, atomic, cross-thread safe).
    Keeps: treasuries allowlist, already_bid (dedupe), wins (invoices).
    """

    def __init__(self, path: Path):
        self.path: Path = Path(path)
        self.treasuries: Dict[str, str] = {}
        self.already_bid: Dict[str, List[Tuple[int, int, float, int]]] = {}
        self.wins: List[WinInvoice] = []

        self._state_lock: threading.Lock = threading.Lock()
        

    # ---------------------- I/O ----------------------

    def wipe(self):
        log_init(LogLevel.MEDIUM, "Wiping state store", "state", {
            "treasuries_count": len(self.treasuries),
            "bids_count": sum(len(bids) for bids in self.already_bid.values()),
            "wins_count": len(self.wins)
        })
        self.treasuries.clear()
        self.already_bid.clear()
        self.wins.clear()
        try:
            if self.path.exists():
                self.path.unlink()
        except Exception as e:
            log_init(LogLevel.HIGH, "Could not remove state file", "state", {"error": str(e)})

    def load(self):
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text())
            log_init(LogLevel.MEDIUM, "State file loaded successfully", "state", {
                "file_size": len(self.path.read_text())
            })
        except Exception as e:
            log_init(LogLevel.HIGH, "Could not load state file", "state", {"error": str(e)})
            return

        self.treasuries = dict(data.get("treasuries", {}))
        self.already_bid = {k: [tuple(x) for x in v] for k, v in data.get("already_bid", {}).items()}

        self.wins = []
        loaded_wins = 0
        skipped_wins = 0
        for w in data.get("wins", []):
            try:
                allowed = set(WinInvoice.__dataclass_fields__.keys())
                clean = {k: v for k, v in w.items() if k in allowed}
                if not clean.get("invoice_id"):
                    payload = (
                        f"{clean.get('validator_key', '')}|"
                        f"{int(clean.get('subnet_id', 0) or 0)}|"
                        f"{float(clean.get('alpha', 0.0) or 0.0):.12f}|"
                        f"{int(clean.get('discount_bps', 0) or 0)}|"
                        f"{int(clean.get('amount_rao', 0) or 0)}|"
                        f"{int(clean.get('pay_window_start_block', 0) or 0)}|"
                        f"{int(clean.get('pay_window_end_block', 0) or 0)}|"
                        f"{int(clean.get('pay_epoch_index', 0) or 0)}"
                    )
                    import hashlib
                    clean["invoice_id"] = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]

                clean.setdefault("alpha_requested", float(clean.get("alpha", 0.0) or 0.0))
                clean.setdefault("was_partial", False)

                self.wins.append(WinInvoice(**clean))
                loaded_wins += 1
            except Exception as e:
                log_init(LogLevel.HIGH, "Skipping malformed win entry", "state", {"error": str(e)})
                skipped_wins += 1
        
        log_init(LogLevel.MEDIUM, "State loading completed", "state", {
            "treasuries_loaded": len(self.treasuries),
            "bids_loaded": sum(len(bids) for bids in self.already_bid.values()),
            "wins_loaded": loaded_wins,
            "wins_skipped": skipped_wins
        })

    def _save_sync(self):
        payload = {
            "treasuries": self.treasuries,
            "already_bid": self.already_bid,
            "wins": [asdict(w) for w in self.wins],
        }

        with self._state_lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass

            tmp_fd, tmp_path = tempfile.mkstemp(
                prefix=self.path.name + ".",
                suffix=".tmp",
                dir=str(self.path.parent) if self.path.parent else None,
            )
            try:
                with os.fdopen(tmp_fd, "w") as f:
                    json.dump(payload, f, indent=2, sort_keys=True)
                os.replace(tmp_path, self.path)
            finally:
                try:
                    if os.path.exists(tmp_path):
                        os.unlink(tmp_path)
                except Exception:
                    pass

    async def save_async(self):
        import asyncio
        await asyncio.to_thread(self._save_sync)

    def save(self):
        self._save_sync()

    # ---------------------- Dedupe helpers ----------------------

    def remember_bid(self, validator_key: str, epoch: int, subnet_id: int, alpha: float, discount_bps: int):
        rec = (int(epoch), int(subnet_id), float(alpha), int(discount_bps))
        self.already_bid.setdefault(validator_key, [])
        if rec not in self.already_bid[validator_key]:
            self.already_bid[validator_key].append(rec)

    def has_bid(self, validator_key: str, epoch: int, subnet_id: int, alpha: float, discount_bps: int) -> bool:
        rec = (int(epoch), int(subnet_id), float(alpha), int(discount_bps))
        return rec in self.already_bid.get(validator_key, [])

    # ---------------------- Tables & aggregates ----------------------

    def aggregate_by_subnet(self) -> Dict[int, tuple[float, float, float]]:
        from collections import defaultdict
        bidded = defaultdict(float)
        for _vkey, recs in self.already_bid.items():
            for epoch, subnet, alpha, _disc in recs:
                if float(alpha) > 0:
                    bidded[int(subnet)] += float(alpha)

        won = defaultdict(float)
        paid = defaultdict(float)
        for w in self.wins:
            sid = int(w.subnet_id)
            won[sid] += float(w.alpha or 0.0)
            if w.paid:
                paid[sid] += (float(w.amount_rao) / PLANCK) if w.amount_rao else float(w.alpha or 0.0)

        all_sids = set(bidded) | set(won) | set(paid)
        return {
            sid: (
                float(bidded.get(sid, 0.0)),
                float(won.get(sid, 0.0)),
                float(paid.get(sid, 0.0)),
            )
            for sid in sorted(all_sids)
        }

    def log_aggregate_summary(self):
        stats = self.aggregate_by_subnet()
        if not stats:
            return
        rows = [[sid, f"{b:.4f} α", f"{w:.4f} α", f"{p:.4f} α"] for sid, (b, w, p) in stats.items()]
        log_settlement(LogLevel.MEDIUM, "Aggregate summary generated", "state", {
            "subnets_count": len(stats),
            "total_bidded": sum(b for b, w, p in stats.values()),
            "total_won": sum(w for b, w, p in stats.values()),
            "total_paid": sum(p for b, w, p in stats.values())
        })
        miner_logger.phase_table(
            MinerPhase.SETTLEMENT, "Aggregate per Subnet (α bidded / won / paid)", 
            ["Subnet", "Bidded", "Won", "Paid"], rows,
            LogLevel.MEDIUM
        )

    def status_tables(self):
        pend = [w for w in self.wins if not w.paid]
        done = [w for w in self.wins if w.paid]


        rows_pend = [[
            (w.validator_key[:8] + "…"),
            int(w.epoch_seen or 0),
            int(w.subnet_id),
            f"{float(w.alpha):.4f} α",
            f"{float(w.alpha_requested):.4f} α",
            "P" if w.was_partial else "F",
            f"{int(w.discount_bps)} bps",
            int(w.pay_epoch_index or 0),
            int(w.pay_window_start_block or 0),
            (int(w.pay_window_end_block) if w.pay_window_end_block else 0),
            f"{(float(w.amount_rao)/PLANCK):.4f} α",
            int(w.pay_attempts or 0),
            (w.invoice_id[:8] + "…"),
            (w.last_response or "")[:24] + ("…" if len(w.last_response or "") > 24 else "")
        ] for w in sorted(pend, key=lambda x: (x.pay_epoch_index, x.pay_window_start_block, x.validator_key))[:LOG_TOP_N]]

        rows_done = [[
            (w.validator_key[:8] + "…"),
            int(w.epoch_seen or 0),
            int(w.subnet_id),
            f"{float(w.alpha):.4f} α",
            f"{float(w.alpha_requested):.4f} α",
            "P" if w.was_partial else "F",
            f"{int(w.discount_bps)} bps",
            int(w.pay_epoch_index or 0),
            int(w.pay_window_start_block or 0),
            (int(w.pay_window_end_block) if w.pay_window_end_block else 0),
            f"{(float(w.amount_rao)/PLANCK):.4f} α",
            int(w.pay_attempts or 0),
            (w.invoice_id[:8] + "…"),
            (w.last_response or "")[:24] + ("…" if len(w.last_response or "") > 24 else "")
        ] for w in sorted(done, key=lambda w: -float(getattr(w, "last_attempt_ts", 0.0) or 0.0))[:LOG_TOP_N]]

        if rows_pend:
            miner_logger.phase_table(
                MinerPhase.SETTLEMENT, "Pending Wins (to pay)",
                ["Validator", "E_seen", "Subnet", "Accepted", "Requested", "Fill", "Disc", "PayE", "WinStart", "WinEnd", "Amount", "Attempts", "InvID", "LastResp"],
                rows_pend,
                LogLevel.MEDIUM
            )
        if rows_done:
            miner_logger.phase_table(
                MinerPhase.SETTLEMENT, "Paid Wins (recent)",
                ["Validator", "E_seen", "Subnet", "Accepted", "Requested", "Fill", "Disc", "PayE", "WinStart", "WinEnd", "Amount", "Attempts", "InvID", "LastResp"],
                rows_done,
                LogLevel.MEDIUM
            )
