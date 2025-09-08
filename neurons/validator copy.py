# neurons/validator.py – SN‑73 v2.3.3
# (CID‑only v4 commitments, single-pass quotas, robust commitment decode & settlement guards)
# Changelog vs 2.3.2:
#  - TESTING short-circuits:
#      * Skip chain scanner & settlement RPCs (prevents async_substrate JSON/coroutine errors in dev).
#      * Skip IPFS + on-chain commitment publishing (no "coroutine not JSON serializable" failures).
#  - Network hygiene:
#      * Filter unreachable axons (0.0.0.0 / [::] / port=0) for broadcasts & win notifications.
#  - Same production behavior when TESTING=False.

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import inspect
import os
import uuid
import threading
from collections import defaultdict
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Set

import bittensor as bt
from bittensor import BLOCKTIME
from metahash.utils.ipfs import aadd_json, aget_json, minidumps as ipfs_minidumps, IPFSError

from metahash.base.utils.logging import ColoredLogger as clog
from metahash import __version__
from treasuries import VALIDATOR_TREASURIES

# ── config -------------------------------------------------------------
from metahash.config import (
    PLANCK,
    AUCTION_BUDGET_ALPHA,
    MAX_BIDS_PER_MINER,
    S_MIN_ALPHA_MINER,
    S_MIN_MASTER_VALIDATOR,
    STRATEGY_PATH,
    START_V3_BLOCK,
    TESTING,
    FORCE_BURN_WEIGHTS,
    POST_PAYMENT_CHECK_DELAY_BLOCKS,
    JAIL_EPOCHS_NO_PAY,
    JAIL_EPOCHS_PARTIAL,
    REPUTATION_ENABLED,
    REPUTATION_BETA,
    REPUTATION_BASELINE_CAP_FRAC,
    REPUTATION_MAX_CAP_FRAC,
    REPUTATION_MIX_GAMMA,
    LOG_TOP_N,
    FORBIDDEN_ALPHA_SUBNETS,
    EPOCH_LENGTH_OVERRIDE,
)
from metahash.validator.strategy import load_weights
from metahash.validator.rewards import (
    compute_epoch_rewards,  # settlement valuation pipeline
    TransferEvent,          # event type
    BidInput,
    WinAllocation,          # keep type for compatibility (constructor may differ across versions)
    budget_from_share,      # budget share
)
from metahash.validator.alpha_transfers import AlphaTransfersScanner
from metahash.validator.epoch_validator import EpochValidatorNeuron
from metahash.utils.subnet_utils import average_price, average_depth
from metahash.protocol import (
    AuctionStartSynapse,
    WinSynapse,
)
from metahash.utils.commitments import (
    write_plain_commitment_json,
    read_all_plain_commitments,
)
from metahash.utils.pretty_logs import pretty  # ← Rich logs (fallback-safe)

# ───────────────────────── data models ────────────────────────────────

EPS_ALPHA = 1e-12  # tolerance for partial vs full acceptance

# On-chain raw bytes ceiling for the commitment blob (CID-only fits easily).
RAW_BYTES_CEILING = 120

# In dev/testing we avoid any network mutations & heavy RPC scans
TESTING_SKIP_CHAIN_IO = False


@dataclass(slots=True)
class _Bid:
    epoch: int
    subnet_id: int
    alpha: float              # α requested for this bid (payment target)
    miner_uid: int
    coldkey: str
    discount_bps: int         # for ordering (not payment)
    weight_snap: float = 0.0  # snapshot at acceptance (0..1)
    idx: int = 0              # stable order per miner+subnet


# ───────────────────────── helpers (new) ──────────────────────────────

def _safe_json_loads(val):
    """Returns dict/list if val is a JSON string, else returns val if already a dict/list; otherwise None."""
    if isinstance(val, (bytes, bytearray)):
        try:
            val = val.decode("utf-8", errors="ignore")
        except Exception:
            return None
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return None
    if isinstance(val, (dict, list)):
        return val
    return None


def _safe_int(x) -> Optional[int]:
    try:
        if isinstance(x, (int,)):
            return int(x)
        if isinstance(x, float):
            return int(x)
        if isinstance(x, str):
            # tolerates "123" only
            return int(x.strip())
    except Exception:
        return None
    return None


class Validator(EpochValidatorNeuron):
    """
    v2.3.3 with:
      • auto master detection,
      • single‑pass allocation using normalized‑reputation quotas (soft envelope),
      • early winner notification in epoch e,
      • payment window recorded & sent as epoch e+1: [start(e+1), end(e+1)],
      • commitments published (and retried) with the **same** (pe, as, de) miners actually used,
      • settlement by ALL validators scanning the exact window (pe),
      • deterministic master budgets using stake snapshot at AuctionStart in epoch e,
      • robust commitment decoding + safer master filtering,
      • v4 commitments CID‑only on‑chain (full winners snapshot in IPFS),
      • TESTING mode: skip chain scanning & publishing to avoid substrate/ipfs dev errors,
      • robust settlement: sanitized snapshots, guarded scanner/price/rewards, cannot crash loop.
    """

    # ─── init ─────────────────────────────────────────────────────────
    def __init__(self, config=None):
        super().__init__(config=config)
        self.hotkey_ss58: str = self.wallet.hotkey.ss58_address

        # File-write lock for atomic saves
        self._file_lock = threading.Lock()

        # Wipe local persisted state if requested BEFORE loading it.
        if getattr(self.config, "fresh", False):
            self._wipe_state_files()

        # strategy (weights per subnet), accepts [0,1] or bps
        self.weights_bps = load_weights(Path(STRATEGY_PATH))

        # epoch-local bid state (masters only)
        self._bid_book: Dict[int, Dict[Tuple[int, int], _Bid]] = defaultdict(dict)
        self._ck_uid_epoch: Dict[int, Dict[str, int]] = defaultdict(dict)
        self._ck_subnets_bid: Dict[int, Dict[str, int]] = defaultdict(dict)  # distinct subnet count per coldkey

        # persistence
        self._validated_epochs: set[int] = self._load_set("validated_epochs.json")
        self._ck_jail_until_epoch: Dict[str, int] = self._load_dict_int("jailed_coldkeys.json")
        self._reputation: Dict[str, float] = self._load_dict_float("reputation.json")  # R in [0,1]

        # winners cleared in epoch e, to publish using **their** pay window (pe, as, de)
        self._pending_commits: Dict[str, Dict] = self._load_dict_obj("pending_commits.json")

        # snapshot of master stakes & our share per epoch e (at e-start time of broadcast)
        self._master_stakes_by_epoch: Dict[int, Dict[str, float]] = {}
        self._my_share_by_epoch: Dict[int, float] = {}

        # chain I/O
        self._async_subtensor: Optional[bt.AsyncSubtensor] = None
        self._scanner: Optional[AlphaTransfersScanner] = None
        self._rpc_lock: asyncio.Lock = asyncio.Lock()

        # remember when we broadcast per-epoch
        self._auction_start_sent_for: Optional[int] = None
        self._wins_notified_for: Optional[int] = None
        self._not_master_log_epoch: Optional[int] = None  # ← avoid spam

        # Detect WinAllocation signature once (compat across versions)
        try:
            self._winalloc_params = set(inspect.signature(WinAllocation).parameters.keys())
        except Exception:
            self._winalloc_params = set()

        pretty.banner(
            f"Validator v{__version__} initialized",
            f"hotkey={self.hotkey_ss58} | netuid={self.config.netuid} | epoch(e)={getattr(self, 'epoch_index', 0)}"
            + (" | fresh" if getattr(self.config, "fresh", False) else ""),
            style="bold magenta",
        )

    # ─── epoch override (testing) ─────────────────────────────────────
    def _apply_epoch_override(self):
        """Force short epochs for testing if EPOCH_LENGTH_OVERRIDE > 0."""
        try:
            if EPOCH_LENGTH_OVERRIDE and EPOCH_LENGTH_OVERRIDE > 0:
                L = int(EPOCH_LENGTH_OVERRIDE)
                blk = int(getattr(self, "block", 0))
                e = blk // L
                self.epoch_index = e
                self.epoch_start_block = e * L
                self.epoch_end_block = self.epoch_start_block + L - 1
                self.epoch_length = L
        except Exception:
            pass

    # ─── persistence helpers ──────────────────────────────────────────
    def _wipe_state_files(self):
        for fn in ("validated_epochs.json", "jailed_coldkeys.json", "reputation.json", "pending_commits.json"):
            path = Path.cwd() / fn
            try:
                if path.exists():
                    path.unlink()
            except Exception as e:
                pretty.log(f"[yellow]Could not remove {fn}: {e}[/yellow]")
        pretty.log("[magenta]Fresh start requested: cleared validator local state files.[/magenta]")

    def _atomic_write_text(self, path: Path, text: str):
        """Write atomically with unique tmp and replace."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        tmp_name = f"{path.name}.tmp.{os.getpid()}.{int(time.time()*1000)}.{uuid.uuid4().hex[:6]}"
        tmp_path = path.with_name(tmp_name)
        with self._file_lock:
            tmp_path.write_text(text)
            os.replace(tmp_path, path)

    def _load_set(self, filename: str) -> set[int]:
        path = Path.cwd() / filename
        try:
            return set(json.loads(path.read_text()))
        except Exception:
            return set()

    def _save_set(self, s: set[int], filename: str):
        path = Path.cwd() / filename
        try:
            self._atomic_write_text(path, json.dumps(sorted(s)))
        except Exception as e:
            pretty.log(f"[yellow]Could not save {filename}: {e}[/yellow]")

    def _load_dict_int(self, filename: str) -> Dict[str, int]:
        path = Path.cwd() / filename
        try:
            return {str(k): int(v) for k, v in json.loads(path.read_text()).items()}
        except Exception:
            return {}

    def _save_dict_int(self, d: Dict[str, int], filename: str):
        path = Path.cwd() / filename
        try:
            self._atomic_write_text(path, json.dumps(d, sort_keys=True))
        except Exception as e:
            pretty.log(f"[yellow]Could not save {filename}: {e}[/yellow]")

    def _load_dict_float(self, filename: str) -> Dict[str, float]:
        path = Path.cwd() / filename
        try:
            return {str(k): float(v) for k, v in json.loads(path.read_text()).items()}
        except Exception:
            return {}

    def _save_dict_float(self, d: Dict[str, float]):
        path = Path.cwd() / "reputation.json"
        try:
            self._atomic_write_text(path, json.dumps(d, sort_keys=True))
        except Exception as e:
            pretty.log(f"[yellow]Could not save reputation.json: {e}[/yellow]")

    def _load_dict_obj(self, filename: str) -> Dict[str, Dict]:
        path = Path.cwd() / filename
        try:
            obj = json.loads(path.read_text())
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
        return {}

    def _save_dict_obj(self, d: Dict[str, Dict], filename: str):
        path = Path.cwd() / filename
        try:
            self._atomic_write_text(path, json.dumps(d, sort_keys=True))
        except Exception as e:
            pretty.log(f"[yellow]Could not save {filename}: {e}[/yellow]")

    # ─── async‑Subtensor getter ───────────────────────────────────────
    async def _stxn(self):
        if self._async_subtensor is None:
            stxn = bt.AsyncSubtensor(network=self.config.subtensor.network)
            await stxn.initialize()
            self._async_subtensor = stxn
        return self._async_subtensor

    # ─── axon reachability filter ─────────────────────────────────────
    @staticmethod
    def _is_reachable_axon(ax) -> bool:
        try:
            host = getattr(ax, "external_ip", None) or getattr(ax, "ip", None)
            port = getattr(ax, "external_port", None) or getattr(ax, "port", None)
            if isinstance(host, str):
                host = host.strip()
            if not host or host in ("0.0.0.0", "[::]"):
                return False
            try:
                port = int(port)
            except Exception:
                return False
            if port <= 0:
                return False
            return True
        except Exception:
            return False

    def _filter_reachable_axons(self, axons):
        try:
            return [ax for ax in (axons or []) if self._is_reachable_axon(ax)]
        except Exception:
            return []

    # ─── utilities ────────────────────────────────────────────────────
    def _norm_weight(self, subnet_id: int) -> float:
        """Normalize strategy weight to [0,1]. Accepts raw in [0,1] or in bps."""
        try:
            raw = float(self.weights_bps[subnet_id])
        except Exception:
            raw = float(self.weights_bps.get(subnet_id, 0.0))
        if raw > 1.0:
            raw = raw / 10_000.0
        return max(0.0, min(1.0, raw))

    def _hotkey_to_uid(self) -> Dict[str, int]:
        mapping: Dict[str, int] = {}
        for i, ax in enumerate(self.metagraph.axons):
            hk = getattr(ax, "hotkey", None)
            if hk:
                mapping[hk] = i
        if not mapping and hasattr(self.metagraph, "hotkeys"):
            for i, hk in enumerate(getattr(self.metagraph, "hotkeys")):
                mapping[hk] = i
        return mapping

    def _is_master_now(self) -> bool:
        tre = VALIDATOR_TREASURIES.get(self.hotkey_ss58)
        if not tre:
            return False
        uid = self._hotkey_to_uid().get(self.hotkey_ss58)
        if uid is None:
            return False
        try:
            return float(self.metagraph.stake[uid]) >= S_MIN_MASTER_VALIDATOR
        except Exception:
            return False

    # Snapshot masters’ stakes & compute our share (at AuctionStart time in epoch e)
    def _snapshot_master_stakes_for_epoch(self, epoch: int) -> float:
        pretty.kv_panel(
            "PHASE 2/3 — Snapshot masters (stakes → budget)",
            [("epoch (e)", epoch), ("action", "Calculating master validators’ stakes to compute personal budget share…")],
            style="bold cyan",
        )
        hk2uid = self._hotkey_to_uid()
        stakes: Dict[str, float] = {}
        total = 0.0
        shares_table: List[Tuple[str, int, float, float]] = []
        for hk in VALIDATOR_TREASURIES.keys():  # only listed hotkeys count
            uid = hk2uid.get(hk)
            if uid is None or uid >= len(self.metagraph.stake):
                continue
            st = float(self.metagraph.stake[uid])
            if st >= S_MIN_MASTER_VALIDATOR:
                stakes[hk] = st
                total += st
        self._master_stakes_by_epoch[epoch] = stakes
        my_stake = stakes.get(self.hotkey_ss58, 0.0)
        share = (my_stake / total) if (total > 0 and my_stake > 0) else 0.0
        self._my_share_by_epoch[epoch] = share
        if total > 0:
            for hk, st in stakes.items():
                uid = hk2uid.get(hk, -1)
                shares_table.append((hk, uid, st, AUCTION_BUDGET_ALPHA * (st / total)))
            pretty.show_master_shares(shares_table)
        else:
            pretty.log("[yellow]No active masters meet the threshold this epoch.[/yellow]")
        return share

    # ─── accept bids coming from AuctionStart responses ───────────────
    def _accept_bid_from(self, *, uid: int, subnet_id: int, alpha: float, discount_bps: int) -> Tuple[bool, Optional[str]]:
        epoch = self.epoch_index
        if self.block < START_V3_BLOCK:
            return False, "auction not started (v2 gating)"
        if not self._is_master_now():
            return False, "bids disabled on non-master validator"
        if uid == 0:
            return False, "uid 0 not allowed"

        # epoch‑jail (by coldkey)
        ck = self.metagraph.coldkeys[uid]
        jail_upto = self._ck_jail_until_epoch.get(ck, -1)
        if jail_upto is not None and epoch < jail_upto:
            return False, f"jailed until epoch {jail_upto}"

        # stake gate (miner’s UID)
        stake_alpha = self.metagraph.stake[uid]
        if stake_alpha < S_MIN_ALPHA_MINER:
            return False, f"stake {stake_alpha:.3f} α < S_MIN_ALPHA_MINER"

        # sanity‑checks
        if not isfinite(alpha) or alpha <= 0:
            return False, "invalid α"
        if alpha > AUCTION_BUDGET_ALPHA:
            return False, "α exceeds max per bid"
        if not (0 <= discount_bps <= 10_000):
            return False, "discount out of range"

        # weight gate: reject zero‑weight subnets outright
        w = self._norm_weight(subnet_id)
        if w <= 0.0:
            return False, f"subnet weight {w:.4f} ≤ 0 (sid={subnet_id})"

        # enforce one uid per coldkey (per epoch per master)
        ck_map = self._ck_uid_epoch[epoch]
        existing_uid = ck_map.get(ck)
        if existing_uid is not None and existing_uid != uid:
            return False, f"coldkey already bidding as uid={existing_uid}"

        # per‑coldkey bid limit on distinct subnets
        count = self._ck_subnets_bid[epoch].get(ck, 0)
        first_on_subnet = True
        key = (uid, subnet_id)
        if key in self._bid_book[epoch]:
            first_on_subnet = False
        if first_on_subnet and count >= MAX_BIDS_PER_MINER:
            return False, "per-coldkey bid limit reached"

        # accept / upsert
        ck_map.setdefault(ck, uid)
        if first_on_subnet:
            self._ck_subnets_bid[epoch][ck] = count + 1

        idx = len([b for (u, s), b in self._bid_book[epoch].items() if u == uid and s == subnet_id])
        self._bid_book[epoch][key] = _Bid(
            epoch=epoch,
            subnet_id=subnet_id,
            alpha=alpha,
            miner_uid=uid,
            coldkey=ck,
            discount_bps=discount_bps,
            weight_snap=w,
            idx=idx,
        )
        return True, None

    # ─── invoice id (deterministic, short) ───────────────────────────
    def _make_invoice_id(
        self,
        validator_key: str,
        epoch: int,
        subnet_id: int,
        alpha: float,
        discount_bps: int,
        pay_epoch: int,
        amount_rao: int,
    ) -> str:
        payload = f"{validator_key}|{epoch}|{subnet_id}|{alpha:.12f}|{discount_bps}|{pay_epoch}|{amount_rao}"
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]

    # ───────────────────────── broadcast helper (masters) ─────────────
    async def _broadcast_auction_start(self):
        if not self._is_master_now():
            return
        if getattr(self, "_auction_start_sent_for", None) == self.epoch_index:
            return
        if self.block < START_V3_BLOCK:
            return

        # PHASE 2/3 — auction for e
        pretty.kv_panel(
            "PHASE 2/3 — AuctionStart & Clear (e)",
            [("epoch (e)", self.epoch_index), ("action", "Starting auction…")],
            style="bold cyan",
        )

        # Stake snapshot & share at the beginning of this epoch (used for budget)
        share = self._snapshot_master_stakes_for_epoch(self.epoch_index)
        my_budget = budget_from_share(share=share, auction_budget_alpha=AUCTION_BUDGET_ALPHA)
        if my_budget <= 0:
            pretty.log("[yellow]Budget share is zero – not broadcasting AuctionStart (no stake share this epoch).[/yellow]")
            self._auction_start_sent_for = self.epoch_index
            return

        axons_all = self.metagraph.axons
        axons = self._filter_reachable_axons(axons_all)
        if not axons:
            pretty.log("[yellow]Metagraph has no reachable axons; skipping AuctionStart broadcast.[/yellow]")
            self._auction_start_sent_for = self.epoch_index
            return

        e = self.epoch_index
        v_uid = self._hotkey_to_uid().get(self.hotkey_ss58, None)
        syn = AuctionStartSynapse(
            epoch_index=e,
            auction_start_block=self.block,
            min_stake_alpha=S_MIN_ALPHA_MINER,
            auction_budget_alpha=my_budget,
            weights_bps=dict(self.weights_bps),
            treasury_coldkey=VALIDATOR_TREASURIES.get(self.hotkey_ss58, ""),
            validator_uid=v_uid,
            validator_hotkey=self.hotkey_ss58,
        )

        # Expect a list[AuctionStartSynapse]
        try:
            pretty.log("[cyan]Broadcasting AuctionStart to miners…[/cyan]")
            resps = await self.dendrite(axons=axons, synapse=syn, deserialize=True, timeout=8)
        except Exception as e_exc:
            resps = []
            pretty.log(f"[yellow]AuctionStart broadcast exceptions: {e_exc}[/yellow]")

        # Map hotkey->uid for attribution of bids
        hk2uid = self._hotkey_to_uid()

        # Count acks and collected bids
        ack_count = 0
        total = len(axons) if axons else 0
        bids_accepted = 0
        bids_rejected = 0

        # Collect rejection details for better operator visibility
        reject_rows: List[List[object]] = []
        reasons_counter: Dict[str, int] = defaultdict(int)

        # Responses come in same order as axons; zip to know who sent what
        for idx, ax in enumerate(axons or []):
            resp = resps[idx] if isinstance(resps, list) and idx < len(resps) else None
            if not isinstance(resp, AuctionStartSynapse):
                continue
            if bool(getattr(resp, "ack", False)):
                ack_count += 1
            bids = getattr(resp, "bids", None) or []
            uid = hk2uid.get(ax.hotkey)
            if uid is None:
                continue
            for b in bids:
                try:
                    subnet_id = int(b.get("subnet_id"))
                    alpha = float(b.get("alpha"))
                    discount_bps = int(b.get("discount_bps"))
                except Exception:
                    bids_rejected += 1
                    reason = "malformed bid"
                    reasons_counter[reason] += 1
                    if len(reject_rows) < max(10, LOG_TOP_N):
                        reject_rows.append([uid, subnet_id if 'subnet_id' in b else "?", f"{b.get('alpha', '?')}", f"{b.get('discount_bps', '?')}", reason])
                    continue
                ok, reason = self._accept_bid_from(uid=uid, subnet_id=subnet_id, alpha=alpha, discount_bps=discount_bps)
                if ok:
                    bids_accepted += 1
                else:
                    bids_rejected += 1
                    r = reason or "rejected"
                    reasons_counter[r] += 1
                    if len(reject_rows) < max(10, LOG_TOP_N):
                        reject_rows.append([uid, subnet_id, f"{alpha:.4f} α", f"{discount_bps} bps", r])

        self._auction_start_sent_for = self.epoch_index

        pretty.kv_panel(
            "AuctionStart Broadcast (epoch e)",
            [
                ("e (now)", e),
                ("block", self.block),
                ("budget α", f"{my_budget:.3f}"),
                ("treasury", VALIDATOR_TREASURIES.get(self.hotkey_ss58, "")),
                ("acks_received", f"{ack_count}/{total}"),
                ("bids_accepted", bids_accepted),
                ("bids_rejected", bids_rejected),
                ("note", "miners pay for e in e+1; weights from e in e+2"),
            ],
            style="bold cyan",
        )

        if bids_rejected > 0:
            pretty.log("[red]Some bids were rejected. See reasons below.[/red]")
            if reject_rows:
                pretty.table("Rejected bids (why)", ["UID", "Subnet", "Alpha", "Discount", "Reason"], reject_rows)
            reason_rows = [[k, v] for k, v in sorted(reasons_counter.items(), key=lambda x: (-x[1], x[0]))[:max(6, LOG_TOP_N // 2)]]
            if reason_rows:
                pretty.table("Rejection counts (top)", ["Reason", "Count"], reason_rows)

        # Immediately clear current epoch e bids and notify winners once per epoch
        if self._wins_notified_for != self.epoch_index:
            await self._clear_now_and_notify(epoch_to_clear=self.epoch_index)
            self._wins_notified_for = self.epoch_index

    # ─────────────────────────── forward() ────────────────────────────
    async def forward(self):
        """
        Per-epoch driver (weights-first):
          1) Settle epoch (e−2) and set weights (from commitments).
          2) Masters: broadcast auction start for current epoch e, accept bids, and CLEAR NOW + notify winners.
          3) Masters: publish previous epoch’s cleared winners (e−1) using **stored** window
        """
        await self._stxn()
        self._apply_epoch_override()

        if self.block < START_V3_BLOCK:
            pretty.banner(
                "Waiting for START_V3_BLOCK",
                f"current_block={self.block} < start_v2={START_V3_BLOCK}",
                style="bold yellow",
            )
            return

        # Epoch banner with explicit e/e+1/e+2 roles
        e = self.epoch_index
        pretty.banner(
            f"Epoch {e} (label: e)",
            f"head_block={self.block} | start={self.epoch_start_block} | end={self.epoch_end_block}\n"
            f"• PHASE 1/3: settle e−2 → {e-2} (scan pe={e-1})\n"
            f"• PHASE 2/3: auction & clear e={e} (miners pay in e+1={e+1})\n"
            f"• PHASE 3/3: publish commitments for e−1={e-1} (stored pe)\n"
            f"• Weights applied from e in e+2={e+2}",
            style="bold white",
        )

        # 1) WEIGHTS FIRST: settle older epoch (e−2)
        pretty.kv_panel(
            "PHASE 1/3 — Settle & Weights",
            [("settle epoch (e−2)", e - 2), ("payment epoch scanned (pe)", e - 1)],
            style="bold cyan",
        )
        await self._settle_and_set_weights_all_masters(epoch_to_settle=e - 2)

        # If we're not a master, say so once per epoch (clear operator signal)
        if not self._is_master_now() and self._not_master_log_epoch != e:
            pretty.log("[yellow]validator is not a master so no biddings.[/yellow]")
            self._not_master_log_epoch = e

        # 2) Masters: broadcast & clear immediately
        await self._broadcast_auction_start()

        # 3) Masters: publish previous epoch’s cleared winners (e−1) using **stored** window
        pretty.kv_panel(
            "PHASE 3/3 — Publish commitments",
            [("epoch_cleared (e−1)", e - 1), ("pay_epoch (pe)", "stored")],
            style="bold cyan",
        )
        await self._publish_commitment_for(epoch_cleared=e - 1)

        # cleanup bid books older than (e−1)
        self._bid_book.pop(e - 2, None)
        self._ck_uid_epoch.pop(e - 2, None)
        self._ck_subnets_bid.pop(e - 2, None)

    # ─── small helper: convert internal bids to allocation inputs ──────
    def _to_bid_inputs(self, bid_map: Dict[Tuple[int, int], _Bid]) -> List[BidInput]:
        out: List[BidInput] = []
        for (uid, subnet_id), b in bid_map.items():
            out.append(
                BidInput(
                    miner_uid=int(b.miner_uid),
                    coldkey=str(b.coldkey),
                    subnet_id=int(b.subnet_id),
                    alpha=float(b.alpha),
                    discount_bps=int(b.discount_bps),
                    weight_bps=int(round(float(b.weight_snap) * 10_000.0)),
                    idx=int(b.idx),
                )
            )
        return out

    # ─── price helpers for ordering/estimation ────────────────────────
    async def _safe_price(self, price_fn, sid: int, start_block: int, end_block: int) -> float:
        """Defensive wrapper around pricing oracle."""
        try:
            p = await price_fn(sid, start_block, end_block)
            return float(getattr(p, "tao", 0.0) or 0.0)
        except Exception as e:
            pretty.log(f"[yellow]Price lookup failed for sid={sid}: {e}[/yellow]")
            return 0.0

    async def _estimate_prices_for_bids(self, bids: List[BidInput], start_block: int, end_block: int) -> Dict[int, float]:
        """Return estimated price per subnet for ordering (current epoch window)."""
        price_fn = self._make_price(start_block, end_block)
        sids = sorted({b.subnet_id for b in bids})
        out: Dict[int, float] = {}
        for sid in sids:
            out[sid] = await self._safe_price(price_fn, sid, start_block, end_block)
        return out

    # ─── compute single-pass quotas (normalized reputation) ───────────
    def _compute_quota_caps(self, bids: List[BidInput], my_budget: float) -> Tuple[Dict[str, float], Dict[str, float]]:
        """
        Returns:
          cap_alpha_by_ck: coldkey -> cap α
          quota_frac_by_ck: coldkey -> normalized fraction q (for logs)
        """
        if not REPUTATION_ENABLED:
            cks = sorted({b.coldkey for b in bids})
            N = max(1, len(cks))
            q = {ck: 1.0 / N for ck in cks}
            return ({ck: my_budget * q[ck] for ck in cks}, q)

        cks = sorted({b.coldkey for b in bids})
        if not cks:
            return ({}, {})

        N = len(cks)
        rep = {ck: max(0.0, min(1.0, float(self._reputation.get(ck, 0.0)))) for ck in cks}
        sum_rep = sum(rep.values())
        if sum_rep > 0:
            norm_rep = {ck: (rep[ck] / sum_rep) for ck in cks}
        else:
            norm_rep = {ck: 1.0 / N for ck in cks}

        gamma = max(0.0, min(1.0, float(REPUTATION_MIX_GAMMA)))
        baseline = max(0.0, min(1.0, float(REPUTATION_BASELINE_CAP_FRAC)))
        capmax = max(baseline, min(1.0, float(REPUTATION_MAX_CAP_FRAC)))

        q_raw = {ck: (1.0 - gamma) * (1.0 / N) + gamma * norm_rep[ck] for ck in cks}
        q_env = {ck: max(baseline, min(capmax, q_raw[ck])) for ck in cks}
        sum_env = sum(q_env.values()) or 1.0
        q = {ck: q_env[ck] / sum_env for ck in cks}  # normalized to sum 1 → full budget usable

        caps = {ck: my_budget * q[ck] for ck in cks}
        return caps, q

    # ─── (masters) auction CLEAR NOW (epoch e) → notify; stash for publish ───
    async def _clear_now_and_notify(self, epoch_to_clear: int):
        if epoch_to_clear < 0:
            return
        if not self._is_master_now():
            return

        bid_map = self._bid_book.get(epoch_to_clear, {})
        if not bid_map:
            pretty.log(f"[grey]No bids to clear this epoch (master). epoch(e)={epoch_to_clear}[/grey]")
            return

        # 1) Compute this validator's α budget for this epoch
        share = float(self._my_share_by_epoch.get(epoch_to_clear, 0.0))
        my_budget = budget_from_share(share=share, auction_budget_alpha=AUCTION_BUDGET_ALPHA)
        if my_budget <= 0:
            pretty.log("[yellow]Budget share is zero – skipping clear.[/yellow]")
            return

        # 2) Build allocation inputs (subnet weights already snapped to this epoch)
        bids_in: List[BidInput] = self._to_bid_inputs(bid_map)

        # 2.a Estimate prices for **ordering** (do NOT write prices into commitments)
        price_by_sid: Dict[int, float] = await self._estimate_prices_for_bids(
            bids_in, self.epoch_start_block, self.epoch_end_block
        )

        # Display ordered preview (by estimated value)
        ordered_preview = sorted(
            bids_in,
            key=lambda b: -(
                (b.weight_bps / 10_000.0)
                * (price_by_sid.get(b.subnet_id, 0.0))
                * (1.0 - b.discount_bps / 10_000.0)
                * b.alpha
            ),
        )
        rows_bids = []
        for b in ordered_preview[:max(5, LOG_TOP_N)]:
            per_unit = (b.weight_bps / 10_000.0) * (1.0 - b.discount_bps / 10_000.0) * (price_by_sid.get(b.subnet_id, 0.0))
            line_val = per_unit * b.alpha
            rows_bids.append([
                b.miner_uid, b.coldkey[:8] + "…", b.subnet_id, b.weight_bps, b.discount_bps,
                f"{b.alpha:.4f} α", f"{per_unit:.6f}", f"{line_val:.6f}"
            ])
        if rows_bids:
            pretty.table(
                "[yellow]Bids (ordered by est. value)[/yellow]",
                ["UID", "CK", "Subnet", "W_bps", "Disc_bps", "Req α", "PU est", "Line value est"],
                rows_bids
            )

        # 3) Single‑pass quotas from normalized reputation (soft envelope)
        cap_alpha_by_ck, quota_frac = self._compute_quota_caps(bids_in, my_budget)
        cap_rows = [[ck[:8] + "…", f"{quota_frac.get(ck, 0.0):.3f}", f"{cap_alpha_by_ck.get(ck, 0.0):.3f} α"] for ck in sorted(cap_alpha_by_ck.keys())]
        if cap_rows:
            pretty.table("Per‑Coldkey Quotas (normalized) → Caps(α)", ["Coldkey", "Quota frac", "Cap α"], cap_rows)

        # 4) Single‑pass greedy allocator honoring your metric and caps
        def sort_key(b: BidInput):
            per_unit = (b.weight_bps / 10_000.0) * (1.0 - b.discount_bps / 10_000.0) * price_by_sid.get(b.subnet_id, 0.0)
            return (per_unit, b.alpha, -b.idx)

        ordered = sorted(bids_in, key=lambda b: (sort_key(b)), reverse=True)

        remaining_budget = float(my_budget)
        cap_left = {ck: float(v) for ck, v in cap_alpha_by_ck.items()}

        winners: List[WinAllocation] = []
        dbg_rows = []
        req_alpha_by_win: Dict[int, float] = {}

        def _make_winalloc(b: BidInput, take: float) -> WinAllocation:
            kwargs = dict(
                miner_uid=b.miner_uid,
                coldkey=b.coldkey,
                subnet_id=b.subnet_id,
                alpha_accepted=take,
                discount_bps=b.discount_bps,
                weight_bps=b.weight_bps,
            )
            if "alpha_requested" in self._winalloc_params:
                kwargs["alpha_requested"] = float(b.alpha)
            w = WinAllocation(**kwargs)
            req_alpha_by_win[id(w)] = float(b.alpha)
            return w

        for b in ordered:
            if remaining_budget <= 0:
                break
            ck = b.coldkey
            want = float(b.alpha)
            cap_avail = cap_left.get(ck, 0.0)
            if cap_avail <= 0 or want <= 0:
                continue
            take = min(remaining_budget, cap_avail, want)
            if take <= 0:
                continue
            w = _make_winalloc(b, take)
            winners.append(w)

            dbg_rows.append([
                b.miner_uid, str(ck)[:8] + "…", b.subnet_id, f"{b.alpha:.4f} α", f"{take:.4f} α",
                b.discount_bps, b.weight_bps, f"{cap_avail:.4f}→{cap_avail - take:.4f}"
            ])

            remaining_budget -= take
            cap_left[ck] = max(0.0, cap_avail - take)

        if dbg_rows:
            pretty.table("Allocations — Single Pass (caps enforced)", ["UID", "CK", "Subnet", "Req α", "Take α", "Disc_bps", "W_bps", "Cap rem (before→after)"], dbg_rows[:max(10, LOG_TOP_N)])

        if not winners:
            pretty.log("[red]No winners after applying quotas & budget.[/red]")
            return

        rows_w = []
        for w in winners[:max(10, LOG_TOP_N)]:
            req_alpha = req_alpha_by_win.get(id(w), getattr(w, "alpha_requested", w.alpha_accepted))
            rows_w.append([
                w.miner_uid, w.coldkey[:8] + "…", w.subnet_id,
                f"{req_alpha:.4f} α",
                f"{w.alpha_accepted:.4f} α",
                w.discount_bps, w.weight_bps,
                "P" if (w.alpha_accepted + EPS_ALPHA) < float(req_alpha) else "F"
            ])
        pretty.table("Winners — detail", ["UID", "CK", "Subnet", "Req α", "Acc α", "Disc_bps", "W_bps", "Fill"], rows_w)

        winners_aggr: Dict[str, float] = defaultdict(float)
        for w in winners:
            winners_aggr[w.coldkey] += w.alpha_accepted
        pretty.show_winners(list(winners_aggr.items()))

        # 5) Stash pending commitment snapshot (publish later with SAME window)
        pay_epoch = self.epoch_index + 1
        epoch_len = int(self.epoch_end_block - self.epoch_start_block + 1)
        win_start = int(self.epoch_end_block) + 1
        win_end = int(self.epoch_end_block + epoch_len)

        inv_i: Dict[int, List[List[int]]] = defaultdict(list)
        for w in winners:
            req_rao_full = int(round(float(w.alpha_accepted) * PLANCK))  # FULL α; discount does NOT reduce payment
            inv_i[int(w.miner_uid)].append([int(w.subnet_id), int(w.weight_bps), int(req_rao_full), int(w.discount_bps)])

        # Full snapshot payload for IPFS (unconstrained by on-chain size)
        self._pending_commits[str(epoch_to_clear)] = {
            "v": 3,  # payload schema label (kept for downstream compat)
            "e": int(epoch_to_clear),
            "pe": int(pay_epoch),
            "as": int(win_start),
            "de": int(win_end),
            "hk": self.hotkey_ss58,  # include hotkey for cross-check
            "t": VALIDATOR_TREASURIES.get(self.hotkey_ss58, ""),  # include treasury for cross-check
            "i": [[uid, lines] for uid, lines in inv_i.items()],
        }
        self._save_dict_obj(self._pending_commits, "pending_commits.json")

        # 6) Notify winners immediately; payment window = next epoch (e+1) with **defined end**
        ack_ok = 0
        calls_sent = 0
        targets_resolved = 0
        attempts = len(winners)
        v_uid = self._hotkey_to_uid().get(self.hotkey_ss58, None)
        for w in winners:
            try:
                amount_rao_full = int(round(float(w.alpha_accepted) * PLANCK))
                req_alpha = req_alpha_by_win.get(id(w), getattr(w, "alpha_requested", w.alpha_accepted))
                invoice_id = self._make_invoice_id(
                    self.hotkey_ss58, self.epoch_index, w.subnet_id, w.alpha_accepted, w.discount_bps, pay_epoch, amount_rao_full
                )
                uid = int(w.miner_uid)
                if not (0 <= uid < len(self.metagraph.axons)):
                    pretty.log(f"[yellow]Skip notify: invalid uid {uid} (no axon).[/yellow]")
                    continue

                target_ax = self.metagraph.axons[uid]
                if not self._is_reachable_axon(target_ax):
                    pretty.log(f"[yellow]Skip notify: uid {uid} axon not reachable (host/port invalid).[/yellow]")
                    continue

                targets_resolved += 1
                partial = (w.alpha_accepted + EPS_ALPHA) < float(req_alpha)
                pretty.log(
                    f"[cyan]EARLY Win[/cyan] e={self.epoch_index}→pay_e={pay_epoch} "
                    f"uid={uid} subnet={w.subnet_id} "
                    f"req={req_alpha:.4f} α acc={w.alpha_accepted:.4f} α "
                    f"disc={w.discount_bps}bps partial={partial} "
                    f"win=[{win_start},{win_end}] inv={invoice_id}"
                )
                resps = await self.dendrite(
                    axons=[target_ax],
                    synapse=WinSynapse(
                        subnet_id=w.subnet_id,
                        alpha=w.alpha_accepted,  # accepted amount (FULL α to be paid)
                        clearing_discount_bps=w.discount_bps,
                        pay_window_start_block=win_start,
                        pay_window_end_block=win_end,
                        pay_epoch_index=pay_epoch,
                        validator_uid=v_uid,
                        validator_hotkey=self.hotkey_ss58,
                        requested_alpha=float(req_alpha),
                        accepted_alpha=float(w.alpha_accepted),
                        was_partially_accepted=partial,
                        accepted_amount_rao=amount_rao_full,
                    ),
                    deserialize=True,
                )
                calls_sent += 1
                resp = resps[0] if isinstance(resps, list) and resps else resps
                if isinstance(resp, WinSynapse) and bool(getattr(resp, "ack", False)):
                    ack_ok += 1
            except Exception as e_exc:
                clog.warning(f"WinSynapse to uid={w.miner_uid} failed: {e_exc}", color="red")

        pretty.kv_panel(
            "Early Clear & Notify (epoch e)",
            [
                ("epoch_cleared (e)", epoch_to_clear),
                ("pay_epoch (e+1)", pay_epoch),
                ("winners", len(winners)),
                ("budget_left α", f"{remaining_budget:.3f}"),
                ("treasury", VALIDATOR_TREASURIES.get(self.hotkey_ss58, "")),
                ("attempts", attempts),
                ("targets_resolved", targets_resolved),
                ("calls_sent", calls_sent),
                ("win_acks", f"{ack_ok}/{calls_sent}"),
            ],
            style="bold green",
        )

    # ─── (masters) Publish commitments for epoch e−1 using stored window (v4 via IPFS) ──
    async def _publish_commitment_for(self, epoch_cleared: int):
        if epoch_cleared < 0:
            return
        if not self._is_master_now():
            return

        key = str(epoch_cleared)
        pending = self._pending_commits.get(key)
        if not isinstance(pending, dict):
            pretty.log(f"[grey]No pending winners to publish for epoch {epoch_cleared}.[/grey]")
            return

        # Short-circuit in TESTING: do not touch IPFS / chain
        if TESTING_SKIP_CHAIN_IO:
            pretty.kv_panel(
                "Commitment publish skipped (TESTING)",
                [("epoch_cleared", epoch_cleared), ("reason", "TESTING=True: chain/IPFS disabled")],
                style="bold magenta",
            )
            # Drop the pending item so it doesn't retry forever in testing
            self._pending_commits.pop(key, None)
            self._save_dict_obj(self._pending_commits, "pending_commits.json")
            return

        # Ensure metadata present in the heavy payload
        pending.setdefault("hk", self.hotkey_ss58)
        pending.setdefault("t", VALIDATOR_TREASURIES.get(self.hotkey_ss58, ""))

        payload = dict(pending)
        payload.setdefault("pe", epoch_cleared + 1)
        if "as" not in payload or "de" not in payload:
            epoch_len = int(self.epoch_end_block - self.epoch_start_block + 1)
            payload["as"] = int(self.epoch_end_block) + 1
            payload["de"] = int(self.epoch_end_block + epoch_len)

        # Try IPFS-first publish (v4 compact commitment, CID-only on-chain)
        st = await self._stxn()
        try:
            # Upload full JSON (minified, sorted keys for determinism) to IPFS
            cid, sha_hex, byte_len = await aadd_json(payload, filename=f"commit_e{epoch_cleared}.json", pin=True, sort_keys=True)

            # Compose the tiny on-chain commitment (CID‑only)  ← CID‑only ENFORCED
            commit_v4 = {
                "v": 4,
                "e": int(payload.get("e", epoch_cleared)),
                "pe": int(payload.get("pe")),
                "c": str(cid),      # IPFS CID (the "hash" we store on-chain)
            }
            commit_str = ipfs_minidumps(commit_v4, sort_keys=True)
            bytes_commit = len(commit_str.encode("utf-8"))

            # Guardrail: should always fit, but keep defensive ceiling check
            if bytes_commit > RAW_BYTES_CEILING:
                raise ValueError(f"v4 CID-only commit unexpectedly too large ({bytes_commit}>{RAW_BYTES_CEILING})")

            ok = await write_plain_commitment_json(
                st, wallet=self.wallet, data=commit_str, netuid=self.config.netuid
            )
            if ok:
                pretty.kv_panel(
                    "Commitment Published (v4 via IPFS, CID‑only on‑chain)",
                    [
                        ("epoch_cleared", epoch_cleared),
                        ("payment_epoch (pe)", payload.get("pe")),
                        ("window@ipfs", f"[{payload.get('as')}, {payload.get('de')}]"),
                        ("cid", cid),
                        ("json_bytes@ipfs", byte_len),
                        ("onchain_bytes", bytes_commit),
                        ("sha256", sha_hex[:16] + "…"),
                    ],
                    style="bold green",
                )
                self._pending_commits.pop(key, None)
                self._save_dict_obj(self._pending_commits, "pending_commits.json")
                return
            else:
                pretty.log("[yellow]Commitment publish returned False (will retry next epoch).[/yellow]")
                return

        except IPFSError as ie:
            pretty.log(f"[yellow]IPFS publish failed; falling back to legacy inline mode: {ie}[/yellow]")
        except Exception as e:
            pretty.log(f"[yellow]Commit via IPFS failed unexpectedly; falling back: {e}[/yellow]")

        # ───── Fallback to legacy inline/trimmed publish (v3) if IPFS failed ─────
        def _minidumps(obj: dict) -> str:
            return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)

        payload_str = _minidumps(payload)

        def bytes_len(s: str) -> int:
            return len(s.encode("utf-8"))

        if bytes_len(payload_str) > RAW_BYTES_CEILING:
            i_list = list(payload.get("i", []))
            scored = []
            for j, (uid, lines) in enumerate(i_list):
                for k, ln in enumerate(lines):
                    if not (isinstance(ln, list) and len(ln) >= 3):
                        continue
                    sid = int(ln[0])
                    w_bps = int(ln[1])
                    rao = int(ln[2])
                    disc = int(ln[3]) if len(ln) >= 4 else 0
                    score = (w_bps / 10_000.0) * (1.0 - disc / 10_000.0) * (rao / max(1, PLANCK))
                    scored.append((j, k, score))
            scored.sort(key=lambda x: x[2])

            trimmed = 0
            idx = 0
            while bytes_len(payload_str) > RAW_BYTES_CEILING and idx < len(scored):
                j, k, _ = scored[idx]
                idx += 1
                if 0 <= j < len(i_list) and 0 <= k < len(i_list[j][1]):
                    try:
                        del i_list[j][1][k]
                        if not i_list[j][1]:
                            del i_list[j]
                        # rebuild scores
                        scored = []
                        for jj, (u2, ln2) in enumerate(i_list):
                            for kk in range(len(ln2)):
                                ln = ln2[kk]
                                sid = int(ln[0])
                                w_bps = int(ln[1])
                                rao = int(ln[2])
                                disc = int(ln[3]) if len(ln) >= 4 else 0
                                score = (w_bps / 10_000.0) * (1.0 - disc / 10_000.0) * (rao / max(1, PLANCK))
                                scored.append((jj, kk, score))
                        scored.sort(key=lambda x: x[2])
                        trimmed += 1
                        payload = dict(payload, i=i_list)
                        payload_str = _minidumps(payload)
                    except Exception:
                        break

            pretty.kv_panel(
                "Commitment payload trimmed (fallback)",
                [("lines_trimmed", trimmed), ("bytes_final", bytes_len(payload_str)), ("limit", RAW_BYTES_CEILING)],
                style="bold yellow",
            )

        if bytes_len(payload_str) > RAW_BYTES_CEILING:
            i_list = list(payload.get("i", []))
            while i_list and bytes_len(_minidumps(dict(payload, i=i_list))) > RAW_BYTES_CEILING:
                if i_list[-1][1]:
                    i_list[-1][1].pop()
                    if not i_list[-1][1]:
                        i_list.pop()
                else:
                    i_list.pop()
            payload = dict(payload, i=i_list)
            payload_str = _minidumps(payload)

        try:
            pretty.log(f"[grey]Commitment (fallback) bytes={len(payload_str.encode('utf-8'))} (limit={RAW_BYTES_CEILING})[/grey]")
            ok = await write_plain_commitment_json(st, wallet=self.wallet, data=payload_str, netuid=self.config.netuid)
            if ok:
                pretty.kv_panel(
                    "Commitment Published (fallback v3 inline)",
                    [
                        ("epoch_cleared", epoch_cleared),
                        ("payment_epoch (pe)", payload.get("pe")),
                        ("window", f"[{payload.get('as')}, {payload.get('de')}]"),
                        ("payload_bytes", len(payload_str.encode("utf-8"))),
                    ],
                    style="bold green",
                )
                self._pending_commits.pop(key, None)
                self._save_dict_obj(self._pending_commits, "pending_commits.json")
            else:
                pretty.log("[yellow]Commitment publish returned False (will retry next epoch).[/yellow]")
        except ValueError as ve:
            pretty.log(f"[red]Commitment payload rejected: {ve}[/red]")
        except Exception as e:
            pretty.log(f"[yellow]Commitment publish failed (will retry next epoch): {e}[/yellow]")

    # ─── (all) Settlement for epoch e-2 across ALL masters ────────────

    def _decode_commit_entry(self, entry):
        """Return a dict snapshot from various shapes: dict, {'raw': json}, or raw json string."""
        if entry is None:
            return None
        if isinstance(entry, dict):
            # if wrapped with 'raw'
            raw = entry.get("raw")
            parsed = _safe_json_loads(raw) if isinstance(raw, (str, bytes, bytearray, dict, list)) else None
            if parsed is not None:
                return parsed
            return entry
        if isinstance(entry, (str, bytes, bytearray)):
            parsed = _safe_json_loads(entry)
            if parsed is not None:
                return parsed
            return None
        return None

    async def _settle_and_set_weights_all_masters(self, epoch_to_settle: int):
        if epoch_to_settle < 0 or epoch_to_settle in self._validated_epochs:
            return

        # In TESTING we avoid all chain reads & scans to prevent substrate/IPFS noise
        if TESTING_SKIP_CHAIN_IO:
            miner_uids_all: List[int] = list(self.get_miner_uids())
            pretty.log("[magenta]TESTING=True: skipping on-chain commitment read & scanner; applying MOCK burn-all.[/magenta]")
            final_scores = [1.0 if uid == 0 else 0.0 for uid in miner_uids_all]
            self._log_final_scores_table(final_scores, miner_uids_all, reason="testing-skip → burn-all")
            total = sum(final_scores) or 1.0
            rows = [[uid, f"{sc:.6f}", f"{(sc/total)*100:.2f}%"] for uid, sc in sorted(zip(miner_uids_all, final_scores), key=lambda x: x[1], reverse=True)[:LOG_TOP_N]]
            pretty.table("[magenta]MOCK Weights (top N) — TESTING=True[/magenta]", ["UID", "Score(TAO)", "% of total"], rows)
            pretty.kv_panel("MOCK: set_weights suppressed", [("why", "TESTING=True"), ("mode", "burn-all (uid 0)")], style="bold magenta")

            self._validated_epochs.add(epoch_to_settle)
            self._save_set(self._validated_epochs, "validated_epochs.json")
            return

        st = await self._stxn()
        try:
            commits = await read_all_plain_commitments(st, netuid=self.config.netuid, block=None)
        except Exception as e:
            pretty.log(f"[red]Commitment read_all failed: {e}[/red]")
            return

        # Allow both hk and treasury based filtering.
        masters_hotkeys: Set[str] = set(VALIDATOR_TREASURIES.keys())
        masters_treasuries: Set[str] = set(VALIDATOR_TREASURIES.values())

        snapshots: List[Dict] = []

        # Iterate over *all* available keys and then filter to masters.
        for hk_key, data_raw in (commits or {}).items():
            data_decoded = self._decode_commit_entry(data_raw)
            cand_list: List[Dict] = []

            if isinstance(data_decoded, dict) and "sn" in data_decoded and isinstance(data_decoded["sn"], list):
                for s in data_decoded["sn"]:
                    s_dec = self._decode_commit_entry(s)
                    if isinstance(s_dec, dict):
                        cand_list.append(s_dec)
            elif isinstance(data_decoded, dict):
                cand_list = [data_decoded]
            else:
                continue

            chosen = None
            for s in cand_list:
                # v4 compact commitment (IPFS) has "c": <cid>; on-chain does NOT include as/de in this build
                if s.get("v") == 4 and s.get("e") == epoch_to_settle and s.get("pe") == (epoch_to_settle + 1):
                    try:
                        cid = s.get("c", "")
                        if not isinstance(cid, str) or not cid:
                            continue
                        # Fetch full snapshot from IPFS
                        full, _norm_bytes, _h = await aget_json(cid)

                        # If IPFS returns raw JSON string, parse it.
                        full_parsed = _safe_json_loads(full)
                        if not isinstance(full_parsed, dict):
                            pretty.log("[yellow]IPFS payload was not a dict – skipping snapshot.[/yellow]")
                            continue

                        # Ensure required fields
                        full_parsed.setdefault("e", s.get("e"))
                        full_parsed.setdefault("pe", s.get("pe"))

                        # Validate required window in the IPFS payload
                        if "as" not in full_parsed or "de" not in full_parsed:
                            pretty.log("[yellow]IPFS payload missing 'as' or 'de' – skipping snapshot.[/yellow]")
                            continue

                        # Ensure identity context
                        full_parsed.setdefault("hk", hk_key)
                        full_parsed.setdefault("t", VALIDATOR_TREASURIES.get(hk_key, ""))

                        chosen = dict(full_parsed)
                        break
                    except Exception as ipfs_exc:
                        pretty.log(f"[red]Failed to fetch v4 commit from IPFS (hk={hk_key}): {ipfs_exc}[/red]")
                        continue

                # Legacy inline commitment (v3 and earlier)
                if s.get("e") == epoch_to_settle and s.get("pe") == (epoch_to_settle + 1):
                    chosen = dict(s)
                    chosen.setdefault("hk", hk_key)
                    chosen.setdefault("t", VALIDATOR_TREASURIES.get(hk_key, ""))
                    # Must include as/de to be usable for settlement
                    if "as" not in chosen or "de" not in chosen:
                        pretty.log("[yellow]Inline commit missing 'as' or 'de' – skipping snapshot.[/yellow]")
                        chosen = None
                    if chosen:
                        break

            if not chosen:
                continue

            # Filter to masters: accept if commit key is a listed hotkey OR commitment declares a master treasury.
            tre = chosen.get("t", "")
            if hk_key not in masters_hotkeys and tre not in masters_treasuries:
                continue

            snapshots.append({"hk": hk_key, **chosen})

        miner_uids_all: List[int] = list(self.get_miner_uids())
        if not snapshots:
            pretty.log("[grey]No master commitments found for this settlement epoch – applying burn‑all weights (no payments to account).[/grey]")
            final_scores = [1.0 if uid == 0 else 0.0 for uid in miner_uids_all]
            self._log_final_scores_table(final_scores, miner_uids_all, reason="no commitments → burn-all")
            if TESTING:
                total = sum(final_scores) or 1.0
                rows = [[uid, f"{sc:.6f}", f"{(sc/total)*100:.2f}%"] for uid, sc in sorted(zip(miner_uids_all, final_scores), key=lambda x: x[1], reverse=True)[:LOG_TOP_N]]
                pretty.table("[magenta]MOCK Weights (top N) — TESTING=True[/magenta]", ["UID", "Score(TAO)", "% of total"], rows)
                pretty.kv_panel("MOCK: set_weights suppressed", [("why", "TESTING=True"), ("mode", "burn-all (uid 0)")], style="bold magenta")
            else:
                self.update_scores(final_scores, miner_uids_all)
                if not self.config.no_epoch:
                    self.set_weights()
                pretty.kv_panel(
                    "Weights Set (Burn‑All: No Commitments)",
                    [("epoch_settled (e−2)", epoch_to_settle), ("miners_scored", sum(1 for x in final_scores if x > 0)), ("mode", "burn-all (uid 0)")],
                    style="bold red",
                )
            self._validated_epochs.add(epoch_to_settle)
            self._save_set(self._validated_epochs, "validated_epochs.json")
            return

        # Sanitize snapshots and build union window
        as_vals: List[int] = []
        de_vals: List[int] = []
        clean_snapshots: List[Dict] = []
        for s in snapshots:
            if not isinstance(s, dict):
                continue
            a = _safe_int(s.get("as"))
            d = _safe_int(s.get("de"))
            if a is None or d is None:
                continue
            as_vals.append(a)
            de_vals.append(d)
            clean_snapshots.append(s)

        if not as_vals or not de_vals:
            pretty.log("[yellow]No valid windows extracted from commitments — postponing settlement this epoch.[/yellow]")
            return

        start_block = min(as_vals)
        end_block = max(de_vals)
        pretty.show_settlement_window(epoch_to_settle, start_block, end_block, POST_PAYMENT_CHECK_DELAY_BLOCKS, len(clean_snapshots))
        end_block += max(0, int(POST_PAYMENT_CHECK_DELAY_BLOCKS or 0))

        # Ensure pay window is closed
        if self.block < end_block:
            remain = end_block - self.block
            eta_s = remain * BLOCKTIME
            pretty.kv_panel(
                "Waiting for payment window to close",
                [
                    ("settle epoch (e−2)", epoch_to_settle),
                    ("payment epoch scanned (e−1+1)", epoch_to_settle + 1),
                    ("blocks_left", remain),
                    ("~eta", f"{int(eta_s//60)}m{int(eta_s%60):02d}s"),
                ],
                style="bold yellow",
            )
            return

        # Scanner init
        if self._scanner is None:
            self._scanner = AlphaTransfersScanner(await self._stxn(), dest_coldkey=None, rpc_lock=self._rpc_lock)

        # Events scan (guarded)
        try:
            events = await self._scanner.scan(start_block, end_block)
            if not isinstance(events, list):
                events = []
        except Exception as scan_exc:
            pretty.kv_panel(
                "Settlement postponed",
                [("epoch_settle", epoch_to_settle), ("reason", f"scanner failed: {scan_exc}")],
                style="bold yellow",
            )
            return

        master_treasuries: Set[str] = {VALIDATOR_TREASURIES.get(s.get("hk", ""), s.get("t", "")) for s in clean_snapshots}
        uids_present: Set[int] = set()
        subnets_needed: Set[int] = set()
        for s in clean_snapshots:
            inv = s.get("inv", {})
            if not inv and "i" in s:
                # handle raw "i" format for downstream steps too
                inv_tmp: Dict[str, Dict[str, object]] = {}
                for item in s.get("i", []):
                    if not (isinstance(item, list) and len(item) == 2):
                        continue
                    uid, lines = item
                    try:
                        uid_int = int(uid)
                    except Exception:
                        continue
                    ck = ""
                    try:
                        if 0 <= uid_int < len(self.metagraph.coldkeys):
                            ck = self.metagraph.coldkeys[uid_int]
                    except Exception:
                        ck = ""
                    ln_list: List[List[int]] = []
                    for ln in lines or []:
                        if not (isinstance(ln, list) and len(ln) >= 3):
                            continue
                        try:
                            sid = int(ln[0])
                            w_bps = int(ln[1])
                            rao = int(ln[2])
                            disc = int(ln[3]) if len(ln) >= 4 else 0
                        except Exception:
                            continue
                        ln_list.append([sid, disc, w_bps, rao])
                    inv_tmp[str(uid_int)] = {"ck": ck, "ln": ln_list}
                s["inv"] = inv_tmp
                inv = inv_tmp

            for uid_s, inv_d in (inv or {}).items():
                try:
                    uid = int(uid_s)
                except Exception:
                    continue
                uids_present.add(uid)
                for ln in inv_d.get("ln", []):
                    if isinstance(ln, list) and len(ln) >= 4:
                        try:
                            subnets_needed.add(int(ln[0]))
                        except Exception:
                            continue

        ck_to_uid: Dict[str, int] = {}
        try:
            for uid, ck in enumerate(self.metagraph.coldkeys):
                if uid in uids_present:
                    ck_to_uid[ck] = uid
        except Exception:
            pass

        # Filter events:
        #  - must be to a master treasury,
        #  - must not be in forbidden subnets,
        #  - source uid must be one of the committed miners,
        #  - if src_subnet_id exists, require it to match subnet_id (prevent cross-subnet alpha).
        filtered_events: List[TransferEvent] = []
        for ev in events:
            try:
                if ev.dest_coldkey not in master_treasuries:
                    continue
                if ev.subnet_id in FORBIDDEN_ALPHA_SUBNETS:
                    continue
                src_uid = ck_to_uid.get(ev.src_coldkey)
                if src_uid not in uids_present:
                    continue
                src_sid = getattr(ev, "src_subnet_id", ev.subnet_id)
                if src_sid != ev.subnet_id:
                    # discard cross-subnet alpha if detector available
                    continue
                filtered_events.append(ev)
            except Exception:
                # Ignore any malformed event defensively
                continue

        # Price cache (guarded)
        price_cache: Dict[int, float] = {}
        price_fn = self._make_price(start_block, end_block)
        for sid in sorted(subnets_needed):
            price_cache[sid] = await self._safe_price(price_fn, sid, start_block, end_block)

        rao_by_uid_dest_subnet: Dict[Tuple[int, str, int], int] = defaultdict(int)
        for ev in filtered_events:
            try:
                uid = ck_to_uid.get(ev.src_coldkey)
                if uid is None:
                    continue
                rao_by_uid_dest_subnet[(uid, ev.dest_coldkey, ev.subnet_id)] += int(ev.amount_rao)
            except Exception:
                continue

        miner_uids_all = list(self.get_miner_uids())  # refresh
        tao_by_uid_dest_subnet: Dict[Tuple[int, str, int], float] = {}
        for sid in sorted(subnets_needed):
            sub_events = [ev for ev in filtered_events if ev.subnet_id == sid]
            if not sub_events:
                continue
            for tre in master_treasuries:
                evs = [ev for ev in sub_events if ev.dest_coldkey == tre]
                if not evs:
                    continue
                try:
                    rewards_list_sub = await compute_epoch_rewards(
                        miner_uids=miner_uids_all,
                        events=evs,
                        scanner=None,
                        pricing=self._make_price(start_block, end_block),
                        pool_depth_of=self._make_depth(start_block, end_block),
                        uid_of_coldkey=lambda ck: ck_to_uid.get(ck),
                        start_block=start_block,
                        end_block=end_block,
                    )
                except Exception as re_exc:
                    pretty.log(f"[yellow]compute_epoch_rewards failed for sid={sid}, tre={tre[:6]}…: {re_exc}[/yellow]")
                    continue
                for i, uid in enumerate(miner_uids_all):
                    try:
                        val = float(rewards_list_sub[i])
                    except Exception:
                        val = 0.0
                    if val > 0:
                        tao_by_uid_dest_subnet[(uid, tre, sid)] = val

        from metahash.validator.rewards import evaluate_commitment_snapshots
        try:
            scores_by_uid, burn_total_value, offenders_nopay, offenders_partial, paid_effective_by_ck = (
                evaluate_commitment_snapshots(
                    snapshots=clean_snapshots,
                    rao_by_uid_dest_subnet=rao_by_uid_dest_subnet,
                    tao_by_uid_dest_subnet=tao_by_uid_dest_subnet,
                    price_cache=price_cache,
                    planck=PLANCK,
                )
            )
        except Exception as eval_exc:
            pretty.kv_panel(
                "Settlement aborted",
                [("epoch_settle", epoch_to_settle), ("reason", f"evaluate_commitment_snapshots failed: {eval_exc}")],
                style="bold yellow",
            )
            return

        pretty.show_offenders(sorted(list(offenders_partial)), sorted(list(offenders_nopay)))

        if offenders_nopay or offenders_partial:
            now_ep = self.epoch_index
            for ck in offenders_nopay:
                self._ck_jail_until_epoch[ck] = max(self._ck_jail_until_epoch.get(ck, 0), now_ep + JAIL_EPOCHS_NO_PAY)
            for ck in offenders_partial:
                next_ep = now_ep + JAIL_EPOCHS_PARTIAL
                if self._ck_jail_until_epoch.get(ck, 0) < next_ep:
                    self._ck_jail_until_epoch[ck] = next_ep
            self._save_dict_int(self._ck_jail_until_epoch, "jailed_coldkeys.json")

        if REPUTATION_ENABLED:
            total_eff = sum(paid_effective_by_ck.values())
            if total_eff > 0:
                beta = float(REPUTATION_BETA)
                for ck, eff in paid_effective_by_ck.items():
                    share = eff / total_eff
                    prev = max(0.0, min(1.0, float(self._reputation.get(ck, 0.0))))
                    new_r = (1.0 - beta) * prev + beta * share
                    self._reputation[ck] = max(0.0, min(1.0, new_r))
                self._save_dict_float(self._reputation)

        miner_uids_all = list(self.get_miner_uids())
        if 0 in miner_uids_all and burn_total_value > 0:
            scores_by_uid[0] = scores_by_uid.get(0, 0.0) + burn_total_value
        pretty.show_burn(burn_total_value)

        final_scores: List[float] = [scores_by_uid.get(uid, 0.0) for uid in miner_uids_all]

        burn_reason = None
        burn_all = FORCE_BURN_WEIGHTS or (not any(final_scores))
        if FORCE_BURN_WEIGHTS:
            burn_reason = "FORCE_BURN_WEIGHTS"
        elif not any(final_scores):
            burn_reason = "NO_POSITIVE_SCORES"

        self._log_final_scores_table(final_scores, miner_uids_all, reason=burn_reason)

        if TESTING:
            total = sum(final_scores) or 1.0
            rows = [[uid, f"{sc:.6f}", f"{(sc/total)*100:.2f}%"] for uid, sc in sorted(zip(miner_uids_all, final_scores), key=lambda x: x[1], reverse=True)[:LOG_TOP_N]]
            pretty.table("[magenta]MOCK Weights (top N) — TESTING=True[/magenta]", ["UID", "Score(TAO)", "% of total"], rows)
            pretty.kv_panel("MOCK: set_weights suppressed", [("why", "TESTING=True"), ("mode", "normal" if not burn_all else "burn-all")], style="bold magenta")
        else:
            if burn_all:
                pretty.log(f"[red]Burn‑all triggered – reason: {burn_reason}.[/red]")
                final_scores = [1.0 if uid == 0 else 0.0 for uid in miner_uids_all]
            else:
                pretty.log("[green]Setting weights from final scores.[/green]")
            self.update_scores(final_scores, miner_uids_all)
            if not self.config.no_epoch:
                self.set_weights()

        self._validated_epochs.add(epoch_to_settle)
        self._save_set(self._validated_epochs, "validated_epochs.json")
        pretty.kv_panel(
            "Settlement Complete",
            [
                ("epoch_settled (e−2)", epoch_to_settle),
                ("miners_scored", sum(1 for x in final_scores if x > 0)),
                ("snapshots_used", len(clean_snapshots)),
                ("mode", "burn-all" if burn_all and not TESTING else "normal" if not TESTING else "TESTING (no set)"),
            ],
            style="bold green",
        )

    # ─── internal: score table printer ────────────────────────────────
    def _log_final_scores_table(self, scores: List[float], uids: List[int], reason: str | None = None):
        pairs = list(zip(uids, scores))
        pairs.sort(key=lambda x: x[1], reverse=True)
        top_n = max(1, int(LOG_TOP_N))
        head = pairs[:top_n]
        total = sum(scores)
        nonzero = sum(1 for _, s in pairs if s > 0)
        lines = []
        lines.append("┌──────────── Final Scores (top {}) ────────────┐".format(min(top_n, len(pairs))))
        lines.append("│ {:>6} │ {:>14} │".format("UID", "score (TAO)"))
        lines.append("├─────────┼────────────────┤")
        for uid, sc in head:
            lines.append("│ {:>6} │ {:>14.6f} │".format(uid, sc))
        if len(pairs) > top_n:
            lines.append("│ ... │ ({} more) │".format(len(pairs) - top_n))
        lines.append("├─────────┴────────────────┤")
        lines.append("│ total: {:>10.6f} | nonzero: {:>4} │".format(total, nonzero))
        if reason:
            lines.append("│ reason: {:<28} │".format(reason))
        lines.append("└───────────────────────────┘")
        pretty.log("\n".join(lines))

    # ─── oracle helpers ───────────────────────────────────────────────
    def _make_price(self, start: int, end: int):
        async def _price(subnet_id: int, *_):
            return await average_price(subnet_id, start_block=start, end_block=end, st=await self._stxn())
        return _price

    def _make_depth(self, start: int, end: int):
        async def _depth(subnet_id: int):
            return await average_depth(subnet_id, start_block=start, end_block=end, st=await self._stxn())
        return _depth

    def _uid_resolver_from_map(self, mapping: Dict[str, int]):
        async def _res(ck: str) -> Optional[int]:
            return mapping.get(ck)
        return _res


# ╭────────────────── keep‑alive (optional) ───────────────────────────╮
if __name__ == "__main__":
    from metahash.bittensor_config import config
    with Validator(config=config(role="validator")) as v:
        while True:
            clog.info("Validator running…", color="gray")
            time.sleep(120)
