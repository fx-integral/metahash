# ╭────────────────────────────────────────────────────────────────────────╮
# neurons/validator.py – SN‑73 v2.2 (early wins, payment window = e+1, correct valuation)
# Pretty logs for testnet using Rich (with safe fallback).
# ╰────────────────────────────────────────────────────────────────────────╯

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import defaultdict
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Set

import bittensor as bt
from bittensor import BLOCKTIME

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
    START_V2_BLOCK,
    TESTING,
    FORCE_BURN_WEIGHTS,
    POST_PAYMENT_CHECK_DELAY_BLOCKS,
    JAIL_EPOCHS_NO_PAY,
    JAIL_EPOCHS_PARTIAL,
    REPUTATION_ENABLED,
    REPUTATION_BETA,
    REPUTATION_BASELINE_CAP_FRAC,
    REPUTATION_MAX_CAP_FRAC,
    LOG_TOP_N,
    FORBIDDEN_ALPHA_SUBNETS,
)
from metahash.validator.strategy import load_weights
from metahash.validator.rewards import (
    compute_epoch_rewards,               # existing pipeline (unchanged)
    TransferEvent,                       # event type
    BidInput, WinAllocation,             # NEW: allocation dataclasses
    budget_from_share,                   # NEW
    caps_by_reputation_for_bids,         # NEW
    allocate_bids,                       # NEW
    build_pending_commitment,            # NEW
    calc_required_rao,                   # NEW
    evaluate_commitment_snapshots,       # NEW
)
from metahash.validator.alpha_transfers import AlphaTransfersScanner
from metahash.validator.epoch_validator import EpochValidatorNeuron
from metahash.utils.subnet_utils import average_price, average_depth
from metahash.protocol import (
    AuctionStartSynapse,  # miners return bids in this response
    WinSynapse,           # winners notification (contains e+1 window)
)
from metahash.utils.commitments import (
    write_plain_commitment_json,
    read_all_plain_commitments,
    read_plain_commitment,
)
from metahash.utils.pretty_logs import pretty  # ← Rich logs (fallback-safe)

# ───────────────────────── data models ──────────────────────────────── #


@dataclass(slots=True)
class _Bid:
    epoch: int
    subnet_id: int
    alpha: float       # α requested for this bid (payment target before discount)
    miner_uid: int
    coldkey: str
    discount_bps: int  # valuation discount; also used to compute amount_rao to pay
    weight_snap: float = 0.0  # snapshot at acceptance (0..1)
    idx: int = 0       # stable order per miner+subnet


class Validator(EpochValidatorNeuron):
    """
    v2.2 with:
      • auto master detection,
      • early winner notification in epoch e,
      • payment window recorded as epoch e+1,
      • commitments published in epoch e+1,
      • settlement by ALL validators in epoch e+2 scanning the exact window [start(e+1), end(e+1)],
      • reputation-capped per-coldkey allocation,
      • deterministic master budgets using stake snapshot at AuctionStart in epoch e.

    Scoring value per fully paid line:
        value = TAO_paid(line) × subnet_weight
              = (α_bidded × (1 − discount)) × subnet_weight
    (No double discount.)
    """

    # ─── init ─────────────────────────────────────────────────────────
    def __init__(self, config=None):
        super().__init__(config=config)

        self.hotkey_ss58: str = self.wallet.hotkey.ss58_address

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
        # winners cleared in epoch e, to publish in epoch e+1
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

        pretty.banner(
            f"Validator v{__version__} initialized",
            f"hotkey={self.hotkey_ss58} | netuid={self.config.netuid} | epoch={getattr(self, 'epoch_index', 0)}"
            + (" | fresh" if getattr(self.config, "fresh", False) else ""),
            style="bold magenta",
        )

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

    def _load_set(self, filename: str) -> set[int]:
        path = Path.cwd() / filename
        try:
            return set(json.loads(path.read_text()))
        except Exception:
            return set()

    def _save_set(self, s: set[int], filename: str):
        path = Path.cwd() / filename
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(sorted(s)))
        tmp.replace(path)

    def _load_dict_int(self, filename: str) -> Dict[str, int]:
        path = Path.cwd() / filename
        try:
            return {str(k): int(v) for k, v in json.loads(path.read_text()).items()}
        except Exception:
            return {}

    def _save_dict_int(self, d: Dict[str, int], filename: str):
        path = Path.cwd() / filename
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, sort_keys=True))
        tmp.replace(path)

    def _load_dict_float(self, filename: str) -> Dict[str, float]:
        path = Path.cwd() / filename
        try:
            return {str(k): float(v) for k, v in json.loads(path.read_text()).items()}
        except Exception:
            return {}

    def _save_dict_float(self, d: Dict[str, float], filename: str):
        path = Path.cwd() / filename
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, sort_keys=True))
        tmp.replace(path)

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
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, sort_keys=True))
        tmp.replace(path)

    # ─── async‑Subtensor getter ───────────────────────────────────────
    async def _stxn(self):
        if self._async_subtensor is None:
            stxn = bt.AsyncSubtensor(network=self.config.subtensor.network)
            await stxn.initialize()
            self._async_subtensor = stxn
        return self._async_subtensor

    # ─── utilities ────────────────────────────────────────────────────
    def _norm_weight(self, subnet_id: int) -> float:
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

    # ─── internal: accept bids coming from AuctionStart responses ───── #
    def _accept_bid_from(self, *, uid: int, subnet_id: int, alpha: float, discount_bps: int) -> Tuple[bool, Optional[str]]:
        epoch = self.epoch_index
        if self.block < START_V2_BLOCK:
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
            weight_snap=self._norm_weight(subnet_id),
            idx=idx,
        )
        return True, None

    # ───────────────────────── broadcast helper (masters) ───────────── #
    async def _broadcast_auction_start(self):
        if not self._is_master_now():
            return
        if getattr(self, "_auction_start_sent_for", None) == self.epoch_index:
            return
        if self.block < START_V2_BLOCK:
            return

        # Stake snapshot & share at the beginning of this epoch (used for budget)
        share = self._snapshot_master_stakes_for_epoch(self.epoch_index)
        my_budget = budget_from_share(share=share, auction_budget_alpha=AUCTION_BUDGET_ALPHA)
        if my_budget <= 0:
            pretty.log("[yellow]Budget share is zero – not broadcasting AuctionStart (no stake share this epoch).[/yellow]")
            self._auction_start_sent_for = self.epoch_index
            return

        axons = self.metagraph.axons
        if not axons:
            pretty.log("[yellow]Metagraph has no axons; skipping AuctionStart broadcast.[/yellow]")
            self._auction_start_sent_for = self.epoch_index
            return

        syn = AuctionStartSynapse(
            epoch_index=self.epoch_index,
            auction_start_block=self.block,
            min_stake_alpha=S_MIN_ALPHA_MINER,
            auction_budget_alpha=my_budget,
            weights_bps=dict(self.weights_bps),
            treasury_coldkey=VALIDATOR_TREASURIES.get(self.hotkey_ss58, ""),
        )

        # Expect a list[AuctionStartSynapse]
        try:
            resps = await self.dendrite(axons=axons, synapse=syn, deserialize=True, timeout=8)
        except Exception as e:
            resps = []
            pretty.log(f"[yellow]AuctionStart broadcast exceptions: {e}[/yellow]")

        # Map hotkey->uid for attribution of bids
        hk2uid = self._hotkey_to_uid()

        # Count acks and collected bids
        ack_count = 0
        total = len(axons) if axons else 0
        bids_accepted = 0
        bids_rejected = 0

        # Responses come in same order as axons; zip to know who sent what
        for idx, ax in enumerate(axons or []):
            resp = resps[idx] if isinstance(resps, list) and idx < len(resps) else None
            if not isinstance(resp, AuctionStartSynapse):
                continue
            if bool(getattr(resp, "ack", False)):
                ack_count += 1

            # Collect bids filled by miner in the response
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
                    continue
                ok, _ = self._accept_bid_from(uid=uid, subnet_id=subnet_id, alpha=alpha, discount_bps=discount_bps)
                if ok:
                    bids_accepted += 1
                else:
                    bids_rejected += 1

        self._auction_start_sent_for = self.epoch_index

        pretty.kv_panel(
            "AuctionStart Broadcast",
            [
                ("epoch", self.epoch_index),
                ("block", self.block),
                ("budget α", f"{my_budget:.3f}"),
                ("treasury", VALIDATOR_TREASURIES.get(self.hotkey_ss58, "")),
                ("acks_received", f"{ack_count}/{total}"),
                ("bids_accepted", bids_accepted),
                ("bids_rejected", bids_rejected),
            ],
            style="bold cyan",
        )

        # Immediately clear current epoch e bids and notify winners once per epoch
        if self._wins_notified_for != self.epoch_index:
            await self._clear_now_and_notify(epoch_to_clear=self.epoch_index)
            self._wins_notified_for = self.epoch_index

    # ─────────────────────────── forward() ──────────────────────────── #
    async def forward(self):
        """ Per-epoch driver (weights-first):
        1) Settle epoch (e−2) and set weights (from commitments).
        2) Masters: broadcast auction start for current epoch e, accept bids, and CLEAR NOW + notify winners.
        3) Masters: publish previous epoch’s cleared winners (e−1) to commitments using current epoch (e) window [start,end] for e+1.
        """
        await self._stxn()
        if self.block < START_V2_BLOCK:
            pretty.banner(
                "Waiting for START_V2_BLOCK",
                f"current_block={self.block} < start_v2={START_V2_BLOCK}",
                style="bold yellow",
            )
            return

        # Epoch banner
        pretty.banner(
            f"Epoch {self.epoch_index}",
            f"head_block={self.block} | start={self.epoch_start_block} | end={self.epoch_end_block}",
            style="bold white",
        )

        # 1) WEIGHTS FIRST: settle older epoch (e−2)
        await self._settle_and_set_weights_all_masters(epoch_to_settle=self.epoch_index - 2)

        # If we're not a master, say so once per epoch (clear operator signal)
        if not self._is_master_now() and self._not_master_log_epoch != self.epoch_index:
            pretty.log("[yellow]validator is not a master so no biddings.[/yellow]")
            self._not_master_log_epoch = self.epoch_index

        # 2) Masters broadcast auction start for current epoch (e), collect bids, and clear immediately
        await self._broadcast_auction_start()

        # 3) Masters publish previous epoch’s cleared winners (e−1) to commitments using *this* epoch’s window (e)
        await self._publish_commitment_for(epoch_cleared=self.epoch_index - 1)

        # cleanup bid books older than (e−1)
        self._bid_book.pop(self.epoch_index - 2, None)
        self._ck_uid_epoch.pop(self.epoch_index - 2, None)
        self._ck_subnets_bid.pop(self.epoch_index - 2, None)

    # ─── small helper: convert internal bids to allocation inputs ────── #
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

    # ─── (masters) Auction CLEAR NOW (epoch e) → notify winners; stash for e+1 publish ──
    async def _clear_now_and_notify(self, epoch_to_clear: int):
        if epoch_to_clear < 0:
            return
        if not self._is_master_now():
            return

        bid_map = self._bid_book.get(epoch_to_clear, {})
        if not bid_map:
            pretty.log(f"[grey]No bids to clear this epoch (master). epoch={epoch_to_clear}[/grey]")
            return

        # 1) Compute this validator's α budget for this epoch
        share = float(self._my_share_by_epoch.get(epoch_to_clear, 0.0))
        my_budget = budget_from_share(share=share, auction_budget_alpha=AUCTION_BUDGET_ALPHA)
        if my_budget <= 0:
            pretty.log("[yellow]Budget share is zero – skipping clear.[/yellow]")
            return

        # 2) Build allocation inputs (subnet weights already snapped to this epoch)
        bids_in: List[BidInput] = self._to_bid_inputs(bid_map)

        # 3) Compute per‑coldkey caps from reputation (or unlimited per CK if disabled)
        if REPUTATION_ENABLED:
            cap_alpha_by_ck = caps_by_reputation_for_bids(
                bids_in,
                my_budget_alpha=my_budget,
                reputation=self._reputation,
                baseline_cap_frac=float(REPUTATION_BASELINE_CAP_FRAC),
                max_cap_frac=float(REPUTATION_MAX_CAP_FRAC),
            )
            pretty.show_caps(cap_alpha_by_ck)
        else:
            cap_alpha_by_ck = None  # unlimited up to budget

        # 4) Allocate winners deterministically (weight, discount, alpha, idx)
        winners: List[WinAllocation]
        winners, budget_left = allocate_bids(
            bids_in,
            my_budget_alpha=my_budget,
            cap_alpha_by_ck=cap_alpha_by_ck,
        )
        if not winners:
            pretty.log("[red]No winners after applying caps & budget.[/red]")
            return

        # 5) Log winners summary (per coldkey)
        winners_aggr: Dict[str, float] = defaultdict(float)
        for w in winners:
            winners_aggr[w.coldkey] += w.alpha_accepted
        pretty.show_winners(list(winners_aggr.items()))

        # 6) Stash pending commitment snapshot (to be published in e+1)
        treasury = VALIDATOR_TREASURIES.get(self.hotkey_ss58, "")
        pending = build_pending_commitment(
            winners,
            epoch_cleared=epoch_to_clear,
            treasury_coldkey=treasury,
            stake_snapshot_block=int(self.epoch_start_block),
            planck=PLANCK,
        )
        self._pending_commits[str(epoch_to_clear)] = pending
        self._save_dict_obj(self._pending_commits, "pending_commits.json")

        # 7) Notify winners immediately; payment window = next epoch (e+1)
        next_start = int(self.epoch_end_block) + 1
        pay_epoch = self.epoch_index + 1

        ack_ok = 0
        total_sent = 0
        for w in winners:
            try:
                amount_rao = calc_required_rao(w.alpha_accepted, w.discount_bps, planck=PLANCK)
                invoice_id = self._make_invoice_id(
                    self.hotkey_ss58, self.epoch_index, w.subnet_id, w.alpha_accepted, w.discount_bps, pay_epoch, amount_rao
                )
                pretty.log(
                    f"[cyan]Sending EARLY Win[/cyan] uid={w.miner_uid} subnet={w.subnet_id} "
                    f"alpha={w.alpha_accepted:.4f} disc={w.discount_bps}bps pay_epoch={pay_epoch} "
                    f"win_start={next_start} inv={invoice_id}"
                )
                resps = await self.dendrite(
                    axons=[self.metagraph.axons[w.miner_uid]],
                    synapse=WinSynapse(
                        subnet_id=w.subnet_id,
                        alpha=w.alpha_accepted,
                        clearing_discount_bps=w.discount_bps,
                        pay_window_start_block=next_start,
                        pay_window_end_block=None,          # end will be known at publish time
                        pay_epoch_index=pay_epoch,
                    ),
                    deserialize=True,
                )
                total_sent += 1
                resp = resps[0] if isinstance(resps, list) and resps else resps
                if isinstance(resp, WinSynapse) and bool(getattr(resp, "ack", False)):
                    ack_ok += 1
            except Exception as e:
                clog.warning(f"WinSynapse to uid={w.miner_uid} failed: {e}", color="red")

        pretty.kv_panel(
            "Early Clear & Notify (epoch e)",
            [
                ("epoch_cleared", epoch_to_clear),
                ("winners", len(winners)),
                ("budget_left α", f"{budget_left:.3f}"),
                ("treasury", treasury),
                ("win_acks", f"{ack_ok}/{total_sent}"),
            ],
            style="bold green",
        )

    # ─── (masters) Publish commitments for epoch e (the one cleared last epoch) using this epoch window (e+1) ──
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

        # attach payment window: this epoch's [start, end] and pe = this epoch index
        pending_to_pub = dict(pending)
        pending_to_pub["pe"] = int(self.epoch_index)              # payment epoch (this)
        pending_to_pub["as"] = int(self.epoch_start_block)        # window start (this epoch)
        pending_to_pub["de"] = int(self.epoch_end_block)          # window end   (this epoch)

        # Build ring buffer payload (keep at most 1 older snapshot)
        st = await self._stxn()
        try:
            prev = await read_plain_commitment(st, netuid=self.config.netuid, hotkey_ss58=self.hotkey_ss58)
        except Exception:
            prev = None

        snaps: List[Dict] = [pending_to_pub]
        if isinstance(prev, dict):
            old_list = []
            if "sn" in prev and isinstance(prev["sn"], list):
                old_list = [s for s in prev["sn"] if isinstance(s, dict)]
            elif "e" in prev:
                old_list = [prev]
            for s in old_list:
                if isinstance(s, dict) and s.get("e") != pending_to_pub.get("e"):
                    snaps.append(s)
                    break

        payload = {"v": 2, "sn": snaps}
        try:
            payload_bytes = len(json.dumps(payload).encode("utf-8"))
            ok = await write_plain_commitment_json(st, wallet=self.wallet, data=payload, netuid=self.config.netuid)
            if ok:
                pretty.kv_panel(
                    "Commitment Published (epoch e winners; pay = this epoch window)",
                    [
                        ("epoch_cleared", epoch_cleared),
                        ("payment_epoch", self.epoch_index),
                        ("window", f"[{self.epoch_start_block}, {self.epoch_end_block}]"),
                        ("treasury", pending_to_pub.get("t", "")),
                        ("payload_bytes", payload_bytes),
                    ],
                    style="bold green",
                )
                # once published, drop it from pending
                self._pending_commits.pop(key, None)
                self._save_dict_obj(self._pending_commits, "pending_commits.json")
            else:
                pretty.log("[yellow]Commitment publish returned False.[/yellow]")
        except ValueError as ve:
            pretty.log(f"[red]Commitment payload too large: {ve}[/red]")
        except Exception as e:
            pretty.log(f"[red]Commitment publish failed: {e}[/red]")

    # ─── (all) Settlement for epoch e-2 across ALL masters ────────────
    async def _settle_and_set_weights_all_masters(self, epoch_to_settle: int):
        if epoch_to_settle < 0 or epoch_to_settle in self._validated_epochs:
            return

        st = await self._stxn()
        try:
            commits = await read_all_plain_commitments(st, netuid=self.config.netuid, block=None)
        except Exception as e:
            pretty.log(f"[red]Commitment read_all failed: {e}[/red]")
            return

        masters_hotkeys: Set[str] = set(VALIDATOR_TREASURIES.keys())
        snapshots: List[Dict] = []
        for hk in masters_hotkeys:
            data = commits.get(hk)
            if not isinstance(data, dict):
                continue
            cand_list: List[Dict] = []
            if "sn" in data and isinstance(data["sn"], list):
                cand_list = [s for s in data["sn"] if isinstance(s, dict)]
            elif "e" in data:
                cand_list = [data]

            # choose snapshot with e == epoch_to_settle and pe == epoch_to_settle+1
            chosen = None
            for s in cand_list:
                if s.get("e") == epoch_to_settle and s.get("pe") == (epoch_to_settle + 1):
                    chosen = s
                    break
            if chosen:
                tre = chosen.get("t", "")
                if tre and tre != VALIDATOR_TREASURIES.get(hk, tre):
                    pretty.log(f"[yellow]Treasury mismatch in commitment for {hk}[/yellow]")
                snapshots.append({"hk": hk, **chosen})

        miner_uids_all: List[int] = list(self.get_miner_uids())

        if not snapshots:
            # burn-all path when there were no commitments to settle
            pretty.log("[grey]No master commitments found for this settlement epoch – applying burn‑all weights (no payments to account).[/grey]")
            final_scores = [1.0 if uid == 0 else 0.0 for uid in miner_uids_all]
            self._log_final_scores_table(final_scores, miner_uids_all, reason="no commitments → burn-all")
            if TESTING:
                pretty.log("[yellow]TESTING=True → not updating weights; burn‑all shown only.[/yellow]")
            else:
                self.update_scores(final_scores, miner_uids_all)
                if not self.config.no_epoch:
                    self.set_weights()
            pretty.kv_panel(
                "Weights Set (Burn‑All: No Commitments)",
                [
                    ("epoch_settled", epoch_to_settle),
                    ("miners_scored", sum(1 for x in final_scores if x > 0)),
                    ("mode", "burn-all (uid 0)"),
                ],
                style="bold red",
            )
            self._validated_epochs.add(epoch_to_settle)
            self._save_set(self._validated_epochs, "validated_epochs.json")
            return

        # settlement window is EXACTLY each master's recorded e+1 window; take union
        start_block = min(int(s["as"]) for s in snapshots if "as" in s)
        end_block = max(int(s["de"]) for s in snapshots if "de" in s)
        pretty.show_settlement_window(epoch_to_settle, start_block, end_block, POST_PAYMENT_CHECK_DELAY_BLOCKS, len(snapshots))
        end_block += POST_PAYMENT_CHECK_DELAY_BLOCKS

        if self.block < end_block:
            remain = end_block - self.block
            eta_s = remain * BLOCKTIME
            pretty.kv_panel("Waiting for window to close",
                            [("blocks_left", remain), ("~eta", f"{int(eta_s//60)}m{int(eta_s%60):02d}s")],
                            style="bold yellow")
            return

        # Prepare scanner; accept all, filter by treasuries after
        if self._scanner is None:
            self._scanner = AlphaTransfersScanner(await self._stxn(), dest_coldkey=None, rpc_lock=self._rpc_lock)

        events: List[TransferEvent] = await self._scanner.scan(start_block, end_block)

        # Filter only events to master treasuries, from known coldkeys, and not in forbidden subnets
        master_treasuries: Set[str] = {VALIDATOR_TREASURIES.get(s["hk"], s.get("t", "")) for s in snapshots}
        filtered_events = [
            ev for ev in events
            if ev.dest_coldkey in master_treasuries
            and ev.subnet_id not in FORBIDDEN_ALPHA_SUBNETS
        ]

        # build ck -> uid map and needed subnets
        subnets_needed: Set[int] = set()
        ck_uid_map: Dict[str, int] = {}
        for s in snapshots:
            inv = s.get("inv", {})
            for uid_s, inv_d in inv.items():
                uid = int(uid_s)
                ck = inv_d.get("ck")
                if ck and ck not in ck_uid_map:
                    ck_uid_map[ck] = uid
                for ln in inv_d.get("ln", []):
                    if isinstance(ln, list) and len(ln) >= 4:
                        subnets_needed.add(int(ln[0]))

        # keep only events whose src_coldkey we actually have in commitments
        filtered_events = [ev for ev in filtered_events if ev.src_coldkey in ck_uid_map]

        # Pre-fetch price cache for fallback burn valuation (no slippage)
        price_cache: Dict[int, float] = {}
        price_fn = self._make_price(start_block, end_block)
        for sid in sorted(subnets_needed):
            p = await price_fn(sid, start_block, end_block)
            price_cache[sid] = float(getattr(p, "tao", 0.0) or 0.0)

        # RAO per (uid, dest_treasury, subnet)
        rao_by_uid_dest_subnet: Dict[Tuple[int, str, int], int] = defaultdict(int)
        for ev in filtered_events:
            uid = ck_uid_map.get(ev.src_coldkey)
            if uid is None:
                continue
            rao_by_uid_dest_subnet[(uid, ev.dest_coldkey, ev.subnet_id)] += int(ev.amount_rao)

        # TAO per (uid, dest_treasury, subnet) via canonical pricing pipeline
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
                rewards_list_sub = await compute_epoch_rewards(
                    miner_uids=miner_uids_all,
                    events=evs,
                    scanner=None,
                    pricing=self._make_price(start_block, end_block),
                    pool_depth_of=self._make_depth(start_block, end_block),
                    uid_of_coldkey=self._uid_resolver_from_map(ck_uid_map),
                    start_block=start_block,
                    end_block=end_block,
                )
                for i, uid in enumerate(miner_uids_all):
                    val = float(rewards_list_sub[i])
                    if val > 0:
                        tao_by_uid_dest_subnet[(uid, tre, sid)] = val

        # Use a single function to turn commitments + payments into scores/burn/offenders.
        scores_by_uid, burn_total_value, offenders_nopay, offenders_partial, paid_effective_by_ck = (
            evaluate_commitment_snapshots(
                snapshots=snapshots,
                rao_by_uid_dest_subnet=rao_by_uid_dest_subnet,
                tao_by_uid_dest_subnet=tao_by_uid_dest_subnet,
                price_cache=price_cache,
                planck=PLANCK,
            )
        )

        pretty.show_offenders(sorted(list(offenders_partial)), sorted(list(offenders_nopay)))

        # Jail by coldkey (partial and zero both penalized)
        if offenders_nopay or offenders_partial:
            now_ep = self.epoch_index
            for ck in offenders_nopay:
                self._ck_jail_until_epoch[ck] = max(self._ck_jail_until_epoch.get(ck, 0), now_ep + JAIL_EPOCHS_NO_PAY)
            for ck in offenders_partial:
                next_ep = now_ep + JAIL_EPOCHS_PARTIAL
                if self._ck_jail_until_epoch.get(ck, 0) < next_ep:
                    self._ck_jail_until_epoch[ck] = next_ep
            self._save_dict_int(self._ck_jail_until_epoch, "jailed_coldkeys.json")

        # Reputation update (EMA on paid *effective* share)
        if REPUTATION_ENABLED:
            total_eff = sum(paid_effective_by_ck.values())
            if total_eff > 0:
                beta = float(REPUTATION_BETA)
                for ck, eff in paid_effective_by_ck.items():
                    share = eff / total_eff
                    prev = max(0.0, min(1.0, float(self._reputation.get(ck, 0.0))))
                    new_r = (1.0 - beta) * prev + beta * share
                    self._reputation[ck] = max(0.0, min(1.0, new_r))
                self._save_dict_float(self._reputation, "reputation.json")
        pretty.show_reputation(self._reputation)

        # finalize weights; add burn to UID 0
        miner_uids_all = list(self.get_miner_uids())
        if 0 in miner_uids_all and burn_total_value > 0:
            scores_by_uid[0] = scores_by_uid.get(0, 0.0) + burn_total_value
        pretty.show_burn(burn_total_value)

        final_scores: List[float] = [scores_by_uid.get(uid, 0.0) for uid in miner_uids_all]

        # Ordered, explicit logs: 1) show scores, 2) show TESTING/burn, 3) act
        burn_reason = None
        burn_all = FORCE_BURN_WEIGHTS or (not any(final_scores))
        if FORCE_BURN_WEIGHTS:
            burn_reason = "FORCE_BURN_WEIGHTS"
        elif not any(final_scores):
            burn_reason = "NO_POSITIVE_SCORES"

        self._log_final_scores_table(final_scores, miner_uids_all, reason=burn_reason)

        if TESTING:
            pretty.log("[yellow]TESTING=True → not updating weights; displaying computed scores only.[/yellow]")
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
                ("epoch_settled", epoch_to_settle),
                ("miners_scored", sum(1 for x in final_scores if x > 0)),
                ("snapshots_used", len(snapshots)),
                ("mode", "burn-all" if burn_all and not TESTING else "normal" if not TESTING else "TESTING (no set)"),
            ],
            style="bold green",
        )

    # ─── internal: score table printer ────────────────────────────────
    def _log_final_scores_table(self, scores: List[float], uids: List[int], reason: str | None = None):
        """ Prints a compact table of top-N final scores (before any burn-all overwrite). """
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
