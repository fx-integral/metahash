# metahash/validator/engines/auction.py
from __future__ import annotations

import inspect
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from metahash.utils.pretty_logs import pretty
from metahash.treasuries import VALIDATOR_TREASURIES

from metahash.validator.state import StateStore

# Config constants
from metahash.config import (AUCTION_BUDGET_ALPHA, AUCTION_START_TIMEOUT, LOG_TOP_N,
                             MAX_BIDS_PER_MINER, S_MIN_ALPHA_MINER, S_MIN_MASTER_VALIDATOR, START_V3_BLOCK)
from metahash.protocol import AuctionStartSynapse

# Use same α epsilon everywhere to avoid accept/clear inconsistencies
EPS_ALPHA = 1e-12  # small epsilon for partial fill checks and gating


def _strip_internals_inplace(syn: Any) -> None:
    """
    Remove transient / non-serializable attributes that may be attached by the
    inbound call context (e.g., dendrite with a `uids` slice). This prevents
    Axon/Pydantic from walking into a Python `slice` and raising:
      TypeError: unhashable type: 'slice'
    """
    def _recursive_clean(obj, visited=None, path=""):
        if visited is None:
            visited = set()

        # Handle None objects early
        if obj is None:
            return

        # Prevent infinite recursion
        obj_id = id(obj)
        if obj_id in visited:
            return
        visited.add(obj_id)

        # Handle slice objects directly
        if isinstance(obj, slice):
            return None

        # Handle dictionaries
        if isinstance(obj, dict):
            for key, value in list(obj.items()):
                if isinstance(value, slice):
                    obj[key] = None
                elif value is not None and (hasattr(value, '__dict__') or isinstance(value, (dict, list, tuple))):
                    _recursive_clean(value, visited, f"{path}[{key}]")

        # Handle lists and tuples
        elif isinstance(obj, (list, tuple)):
            for i, item in enumerate(obj):
                if isinstance(item, slice):
                    if isinstance(obj, list):
                        obj[i] = None
                elif item is not None and (hasattr(item, '__dict__') or isinstance(item, (dict, list, tuple))):
                    _recursive_clean(item, visited, f"{path}[{i}]")

        # Handle objects with attributes
        elif hasattr(obj, '__dict__'):
            for attr_name in list(obj.__dict__.keys()):
                try:
                    attr_value = getattr(obj, attr_name)
                    if isinstance(attr_value, slice):
                        setattr(obj, attr_name, None)
                    elif attr_value is not None and (hasattr(attr_value, '__dict__') or isinstance(attr_value, (dict, list, tuple))):
                        _recursive_clean(attr_value, visited, f"{path}.{attr_name}")
                except Exception:
                    pass

    # First, recursively clean any nested slice objects
    _recursive_clean(syn)

    # Most important: inbound context
    for attr in ("dendrite", "_dendrite"):
        if hasattr(syn, attr):
            try:
                setattr(syn, attr, None)
            except Exception:
                try:
                    delattr(syn, attr)
                except Exception:
                    pass

    # Extra caution: sometimes frameworks attach other transient handles
    for attr in ("axon", "_axon", "server", "_server", "context", "_context"):
        if hasattr(syn, attr):
            try:
                setattr(syn, attr, None)
            except Exception:
                try:
                    delattr(syn, attr)
                except Exception:
                    pass


@dataclass(slots=True)
class _Bid:
    epoch: int
    subnet_id: int
    alpha: float              # α requested for this bid (payment target)
    miner_uid: int
    coldkey: str
    discount_bps: int         # for ordering (not payment)
    weight_snap: float = 0.0  # snapshot at acceptance (0..1)
    idx: int = 0              # stable order per miner across subnets


@dataclass(slots=True, frozen=True)
class BidInput:
    miner_uid: int
    coldkey: str
    subnet_id: int
    alpha: float                # requested α (in α units, not rao)
    discount_bps: int           # 0..10_000
    weight_bps: int             # 0..10_000
    idx: int = 0                # stable per-miner ordering


@dataclass(slots=True)
class WinAllocation:
    miner_uid: int
    coldkey: str
    subnet_id: int
    alpha_requested: float
    alpha_accepted: float
    discount_bps: int
    weight_bps: int


def budget_from_share(*, share: float, auction_budget_alpha: float = AUCTION_BUDGET_ALPHA) -> float:
    """
    Per-epoch budget in α (base-subnet alpha) from master share.
    """
    if share <= 0:
        return 0.0
    return auction_budget_alpha * float(share)


def budget_tao_from_share(
    *,
    share: float,
    auction_budget_alpha: float,
    price_tao_per_alpha_base: float,
) -> float:
    """
    Convert master share → TAO VALUE budget using the base-subnet α→TAO price.
    """
    if share <= 0 or auction_budget_alpha <= 0 or price_tao_per_alpha_base <= 0:
        return 0.0
    return float(share) * float(auction_budget_alpha) * float(price_tao_per_alpha_base)


class AuctionEngine:
    """AuctionStart broadcast & bid management (masters only)."""

    def __init__(self, parent, state: StateStore, weights_bps: Dict[int, int], clearer=None):
        self.parent = parent
        self.state = state
        self.weights_bps = weights_bps  # subnet_id -> bps
        self.clearer = clearer  # object with async clear_now_and_notify(epoch: int)

        # epoch-local bid state (masters only)
        self._bid_book: Dict[int, Dict[Tuple[int, int], _Bid]] = defaultdict(dict)
        self._ck_uid_epoch: Dict[int, Dict[str, int]] = defaultdict(dict)
        self._ck_subnets_bid: Dict[int, Dict[str, int]] = defaultdict(dict)  # distinct subnet count per coldkey

        # NEW: record rejected bids (for IPFS diagnostics snapshot)
        self._rejected_bids_by_epoch: Dict[int, List[Dict[str, object]]] = defaultdict(list)

        # snapshot of master stakes & our share per epoch e
        self._master_stakes_by_epoch: Dict[int, Dict[str, float]] = {}
        self._my_share_by_epoch: Dict[int, float] = {}

        # remember keyed-by-epoch control flags
        self._auction_start_sent_for: Optional[int] = None
        self._wins_notified_for: Optional[int] = None
        self._not_master_log_epoch: Optional[int] = None  # avoid spam

        # Detect WinAllocation signature once (compat across versions)
        try:
            self._winalloc_params = set(inspect.signature(WinAllocation).parameters.keys())
        except Exception:
            self._winalloc_params = set()

    # ---------- utilities ----------
    def _hotkey_to_uid(self) -> Dict[str, int]:
        mapping: Dict[str, int] = {}
        for i, ax in enumerate(self.parent.metagraph.axons):
            hk = getattr(ax, "hotkey", None)
            if hk:
                mapping[hk] = i
        if not mapping and hasattr(self.parent.metagraph, "hotkeys"):
            for i, hk in enumerate(getattr(self.parent.metagraph, "hotkeys")):
                mapping[hk] = i
        return mapping

    def _is_master_now(self) -> bool:
        tre = VALIDATOR_TREASURIES.get(self.parent.hotkey_ss58)
        if not tre:
            return False
        uid = self._hotkey_to_uid().get(self.parent.hotkey_ss58)
        if uid is None:
            return False
        try:
            return float(self.parent.metagraph.stake[uid]) >= S_MIN_MASTER_VALIDATOR
        except Exception:
            return False

    def _norm_weight(self, subnet_id: int) -> float:
        """
        Normalize subnet weight to 0..1 from bps or float. Works with dict or list.
        """
        raw = 0.0
        try:
            if isinstance(self.weights_bps, dict):
                raw = float(self.weights_bps.get(subnet_id, 0.0))
            elif isinstance(self.weights_bps, (list, tuple)) and 0 <= subnet_id < len(self.weights_bps):
                raw = float(self.weights_bps[subnet_id])
        except Exception:
            raw = 0.0
        if raw > 1.0:
            raw = raw / 10_000.0
        return max(0.0, min(1.0, raw))

    def _snapshot_master_stakes_for_epoch(self, epoch: int) -> float:
        pretty.kv_panel(
            "Master Stakes => Budget",
            [("epoch (e)", epoch), ("action", "Calculating master validators’ stakes to compute personal budget share…")],
            style="bold cyan",
        )
        hk2uid = self._hotkey_to_uid()
        stakes: Dict[str, float] = {}
        total = 0.0
        shares_table: List[Tuple[str, int, float, float]] = []
        for hk in VALIDATOR_TREASURIES.keys():
            uid = hk2uid.get(hk)
            if uid is None or uid >= len(self.parent.metagraph.stake):
                continue
            try:
                st = float(self.parent.metagraph.stake[uid])
            except Exception:
                st = 0.0
            if st >= S_MIN_MASTER_VALIDATOR:
                stakes[hk] = st
                total += st
        self._master_stakes_by_epoch[epoch] = stakes
        my_stake = stakes.get(self.parent.hotkey_ss58, 0.0)
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

    # --- Axon reachability helpers ---
    @staticmethod
    def _extract_ip_port(ax) -> Tuple[Optional[str], Optional[int]]:
        ip, port = None, None
        try:
            ip = getattr(ax, "ip", None) or getattr(ax, "external_ip", None)
            port = getattr(ax, "port", None) or getattr(ax, "external_port", None)
            ep = getattr(ax, "endpoint", None)
            if ep is not None:
                ip = ip or getattr(ep, "ip", None)
                port = port or getattr(ep, "port", None)
        except Exception:
            pass
        try:
            port = int(port) if port is not None else None
        except Exception:
            port = None
        return ip, port

    @staticmethod
    def _is_bad_ip(ip: Optional[str]) -> bool:
        if not ip:
            return True
        ip_l = str(ip).strip().lower()
        return ip_l in {"0.0.0.0", "::", "localhost", "127.0.0.1", "::1"}

    @classmethod
    def _is_ax_reachable(cls, ax) -> Tuple[bool, str]:
        hk = getattr(ax, "hotkey", None)
        if not hk:
            return False, "missing hotkey"
        ip, port = cls._extract_ip_port(ax)
        if cls._is_bad_ip(ip):
            return False, f"bad ip {ip!r}"
        if port is None or not (1 <= int(port) <= 65535):
            return False, f"bad port {port!r}"
        is_serving = getattr(ax, "is_serving", True)
        if is_serving is False:
            return False, "not serving"
        return True, "ok"

    def _filter_reachable_axons(self, axons: List[object]) -> List[object]:
        if not axons:
            return []
        good: List[object] = []
        bad_rows: List[List[object]] = []
        for ax in axons:
            ok, why = self._is_ax_reachable(ax)
            if ok:
                good.append(ax)
            else:
                hk = getattr(ax, "hotkey", "?")
                ip, port = self._extract_ip_port(ax)
                if len(bad_rows) < max(10, LOG_TOP_N):
                    bad_rows.append([hk, str(ip), str(port), why])
        if bad_rows:
            pretty.table("Filtered unreachable axons", ["Hotkey", "IP", "Port", "Reason"], bad_rows)
        pretty.kv_panel(
            "Axon filter",
            [("received", len(axons)), ("usable", len(good)), ("filtered_out", len(axons) - len(good))],
            style="bold cyan",
        )
        return good

    # -----------------------------------------------------------------------

    def _accept_bid_from(self, *, uid: int, subnet_id: int, alpha: float, discount_bps: int) -> Tuple[bool, Optional[str]]:
        epoch = self.parent.epoch_index
        if not self._is_master_now():
            return False, "bids disabled on non-master validator"
        if uid == 0:
            return False, "uid 0 not allowed"

        # basic bounds for metagraph arrays
        try:
            n_stake = len(self.parent.metagraph.stake)
        except Exception:
            n_stake = 0
        try:
            n_ck = len(self.parent.metagraph.coldkeys)
        except Exception:
            n_ck = 0
        if uid < 0 or uid >= max(n_stake, n_ck):
            return False, f"bad uid {uid}"

        # epoch-jail (by coldkey)
        ck = ""
        try:
            if uid < n_ck:
                ck = self.parent.metagraph.coldkeys[uid]
        except Exception:
            ck = ""
        jail_upto = self.state.ck_jail_until_epoch.get(ck, -1)
        if jail_upto is not None and epoch < jail_upto:
            return False, f"jailed until epoch {jail_upto}"

        # stake gate (miner’s UID)
        try:
            stake_alpha = float(self.parent.metagraph.stake[uid])
        except Exception:
            stake_alpha = 0.0
        if stake_alpha < S_MIN_ALPHA_MINER:
            return False, f"stake {stake_alpha:.3f} α < S_MIN_ALPHA_MINER"

        # sanity-checks
        try:
            alpha_val = float(alpha)
        except Exception:
            alpha_val = 0.0
        if not (alpha_val > 0):
            return False, "invalid α"
        if alpha_val > AUCTION_BUDGET_ALPHA:
            return False, "α exceeds max per bid"
        if not (0 <= int(discount_bps) <= 10_000):
            return False, "discount out of range"

        # weight gate: reject zero-weight subnets outright
        w = self._norm_weight(subnet_id)
        if w <= 0.0:
            return False, f"subnet weight {w:.4f} ≤ 0 (sid={subnet_id})"

        # enforce one uid per coldkey (per epoch per master)
        ck_map = self._ck_uid_epoch[epoch]
        existing_uid = ck_map.get(ck)
        if existing_uid is not None and existing_uid != uid:
            return False, f"coldkey already bidding as uid={existing_uid}"

        # per-coldkey bid limit on distinct subnets
        count = self._ck_subnets_bid[epoch].get(ck, 0)
        first_on_subnet = (uid, subnet_id) not in self._bid_book[epoch]
        if first_on_subnet and count >= MAX_BIDS_PER_MINER:
            return False, "per-coldkey bid limit reached"

        # accept / upsert (one (uid,subnet) entry per epoch)
        ck_map.setdefault(ck, uid)
        if first_on_subnet:
            self._ck_subnets_bid[epoch][ck] = count + 1

        # idx should be stable per-miner across all subnets, not per (uid, subnet)
        idx = sum(1 for (u, _s), _b in self._bid_book[epoch].items() if u == uid)

        self._bid_book[epoch][(uid, subnet_id)] = _Bid(
            epoch=epoch,
            subnet_id=subnet_id,
            alpha=alpha_val,
            miner_uid=uid,
            coldkey=ck,
            discount_bps=int(discount_bps),
            weight_snap=w,
            idx=idx,
        )
        return True, None

    async def broadcast_auction_start(self):
        """Broadcast AuctionStart and accept bids; then clear and notify winners immediately."""
        if not self._is_master_now():
            return
        if getattr(self, "_auction_start_sent_for", None) == self.parent.epoch_index:
            return

        pretty.kv_panel(
            "🎯 AuctionStart Phase",
            [
                ("epoch (e)", self.parent.epoch_index),
                ("action", "Starting auction…"),
                ("block", self.parent.block),
                ("master_status", "✅ Active"),
            ],
            style="bold cyan",
        )

        share = self._snapshot_master_stakes_for_epoch(self.parent.epoch_index)
        my_budget = budget_from_share(share=share, auction_budget_alpha=AUCTION_BUDGET_ALPHA)
        if my_budget <= 0:
            pretty.log("[yellow]Budget share is zero – not broadcasting AuctionStart (no stake share this epoch).[/yellow]")
            self._auction_start_sent_for = self.parent.epoch_index
            return

        raw_axons = list(self.parent.metagraph.axons or [])
        axons = self._filter_reachable_axons(raw_axons)
        if not axons:
            pretty.log("[yellow]No usable axons after filtering; skipping AuctionStart broadcast.[/yellow]")
            self._auction_start_sent_for = self.parent.epoch_index
            return

        e = self.parent.epoch_index
        v_uid = self._hotkey_to_uid().get(self.parent.hotkey_ss58, None)
        syn = AuctionStartSynapse(
            epoch_index=e,
            auction_start_block=self.parent.block,
            min_stake_alpha=S_MIN_ALPHA_MINER,
            auction_budget_alpha=my_budget,
            weights_bps=dict(self.weights_bps),  # subnet_id -> bps
            treasury_coldkey=VALIDATOR_TREASURIES.get(self.parent.hotkey_ss58, ""),
            validator_uid=v_uid,
            validator_hotkey=self.parent.hotkey_ss58,
        )

        # Clean the AuctionStartSynapse before sending to prevent slice errors
        _strip_internals_inplace(syn)

        try:
            pretty.log("[cyan]Broadcasting AuctionStart to miners…[/cyan]")
            resps = await self.parent.dendrite(axons=axons, synapse=syn, deserialize=True, timeout=AUCTION_START_TIMEOUT)
        except Exception as e_exc:
            resps = []
            pretty.log(f"[yellow]AuctionStart broadcast exceptions: {e_exc}[/yellow]")

        hk2uid = self._hotkey_to_uid()
        ack_count = 0
        total = len(axons)
        bids_accepted = 0
        bids_rejected = 0
        reject_rows: List[List[object]] = []
        reasons_counter: Dict[str, int] = defaultdict(int)

        for idx, ax in enumerate(axons):
            resp = resps[idx] if isinstance(resps, list) and idx < len(resps) else None
            if resp is None:
                continue
            if not isinstance(resp, AuctionStartSynapse):
                continue
            if bool(getattr(resp, "ack", False)):
                ack_count += 1
            bids = getattr(resp, "bids", None) or []
            uid = hk2uid.get(getattr(ax, "hotkey", None))
            if uid is None:
                continue
            for b in bids:
                malformed = False
                try:
                    subnet_id = int(b.get("subnet_id"))
                    alpha = float(b.get("alpha"))
                    discount_bps = int(b.get("discount_bps"))
                except Exception:
                    malformed = True
                    subnet_id = b.get("subnet_id", None)
                    alpha = b.get("alpha", None)
                    discount_bps = b.get("discount_bps", None)

                if malformed:
                    bids_rejected += 1
                    reason = "malformed bid"
                    reasons_counter[reason] += 1
                    if len(reject_rows) < max(10, LOG_TOP_N):
                        reject_rows.append([uid, subnet_id if subnet_id is not None else "?", f"{alpha}", f"{discount_bps}", reason])
                    ck = self.parent.metagraph.coldkeys[uid] if (0 <= uid < len(self.parent.metagraph.coldkeys)) else ""
                    self._rejected_bids_by_epoch[e].append({
                        "uid": uid, "coldkey": ck, "subnet_id": subnet_id, "alpha": alpha,
                        "discount_bps": discount_bps, "reason": reason,
                    })
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
                    ck = self.parent.metagraph.coldkeys[uid] if (0 <= uid < len(self.parent.metagraph.coldkeys)) else ""
                    self._rejected_bids_by_epoch[e].append({
                        "uid": uid, "coldkey": ck, "subnet_id": subnet_id, "alpha": float(alpha),
                        "discount_bps": int(discount_bps), "reason": r,
                    })

        self._auction_start_sent_for = self.parent.epoch_index

        # Enhanced auction broadcast summary
        pretty.kv_panel(
            "📡 AuctionStart Broadcast Summary",
            [
                ("epoch (e)", e),
                ("block", self.parent.block),
                ("budget α", f"{my_budget:.3f}"),
                ("treasury", VALIDATOR_TREASURIES.get(self.parent.hotkey_ss58, "")[:8] + "…"),
                ("acks_received", f"{ack_count}/{total}"),
                ("bids_accepted", bids_accepted),
                ("bids_rejected", bids_rejected),
                ("success_rate", f"{(ack_count/total*100):.1f}%" if total > 0 else "0%"),
                ("note", "miners pay for e in e+1; weights from e in e+2"),
            ],
            style="bold cyan",
        )

        if bids_rejected > 0:
            pretty.log("[red]Some bids were rejected. See reasons below.[/red]")
            if reject_rows:
                pretty.table("❌ Rejected Bids", ["UID", "Subnet", "Alpha", "Discount", "Reason"], reject_rows)
            reason_rows = [[k, v] for k, v in sorted(reasons_counter.items(), key=lambda x: (-x[1], x[0]))[:max(6, LOG_TOP_N // 2)]]
            if reason_rows:
                pretty.table("📊 Rejection Summary", ["Reason", "Count"], reason_rows)

        if self._wins_notified_for != self.parent.epoch_index:
            if self.clearer is not None:
                ok = await self.clearer.clear_now_and_notify(epoch_to_clear=self.parent.epoch_index)

                try:
                    keys = list(self.state.pending_commits.keys()) if isinstance(self.state.pending_commits, dict) else []
                except Exception:
                    keys = []
                pretty.kv_panel(
                    "📋 Post-Clear Staging Snapshot",
                    [
                        ("epoch_cleared(e)", self.parent.epoch_index),
                        ("staged_key_expected", str(self.parent.epoch_index)),
                        ("pending_keys", len(keys)),
                        ("keys_sample", ", ".join(keys[:8])),
                        ("clear_status", "✅ Success" if ok else "❌ Failed"),
                    ],
                    style="bold magenta",
                )
            else:
                pretty.log("[yellow]Clearing engine not configured; skipping clear_now_and_notify.[/yellow]")
            self._wins_notified_for = self.parent.epoch_index

    def cleanup_old_epoch_books(self, before_epoch: int):
        """Cleanup old bid state **older than** the given epoch."""
        for d in (self._bid_book, self._ck_uid_epoch, self._ck_subnets_bid, self._rejected_bids_by_epoch):
            for k in list(d.keys()):
                if k < before_epoch:
                    d.pop(k, None)
