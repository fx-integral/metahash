# neurons/clearing.py
from __future__ import annotations

import hashlib
import inspect
from collections import defaultdict
from typing import Dict, List, Tuple

from metahash.utils.pretty_logs import pretty
from metahash.validator.state import StateStore

# Config & rewards utilities
from metahash.config import (
    PLANCK,
    AUCTION_BUDGET_ALPHA,
    REPUTATION_ENABLED,
    REPUTATION_BASELINE_CAP_FRAC,
    REPUTATION_MAX_CAP_FRAC,
    REPUTATION_MIX_GAMMA,
    LOG_TOP_N,
)
from metahash.validator.rewards import WinAllocation, BidInput, budget_from_share
from metahash.protocol import WinSynapse
from treasuries import VALIDATOR_TREASURIES

# Price oracle (for ordering only; prices are NOT written into commitments)
from metahash.utils.subnet_utils import average_price


EPS_ALPHA = 1e-12  # tolerance used to mark partial fills


class ClearingEngine:
    """
    Clear winners *now* for the current epoch and notify miners.
    Populates StateStore.pending_commits[e] with the exact pay window (as,de) used,
    so CommitmentsEngine can publish the same snapshot in e+1.

    Responsibilities:
      - Determine winners for epoch e from AuctionEngine's collected bids
      - Compute single-pass allocation with normalized-reputation quotas
      - Build 'i' commitment payload { uid -> [[sid, w_bps, rao, disc], ...] }
      - Persist to StateStore.pending_commits and save
      - Send early WinSynapse notifications to winners (reachable axons only)
    """

    def __init__(self, parent, state: StateStore):
        self.parent = parent
        self.state = state

        # Detect WinAllocation signature once (handles schema variations)
        try:
            self._winalloc_params = set(inspect.signature(WinAllocation).parameters.keys())
        except Exception:
            self._winalloc_params = set()

    # ──────────────── helpers ────────────────

    @staticmethod
    def _is_reachable_axon(ax) -> bool:
        """Guard against axons with host=0.0.0.0/[::] or port=0."""
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

    def _to_bid_inputs(self, bid_map) -> List[BidInput]:
        """Convert AuctionEngine._Bid entries → BidInput for allocator."""
        out: List[BidInput] = []
        for (uid, subnet_id), b in bid_map.items():
            w_bps = int(round(float(getattr(b, "weight_snap", 0.0)) * 10_000.0))
            out.append(
                BidInput(
                    miner_uid=int(getattr(b, "miner_uid", uid)),
                    coldkey=str(getattr(b, "coldkey", "")),
                    subnet_id=int(subnet_id),
                    alpha=float(getattr(b, "alpha", 0.0)),
                    discount_bps=int(getattr(b, "discount_bps", 0)),
                    weight_bps=w_bps,
                    idx=int(getattr(b, "idx", 0)),
                )
            )
        return out

    async def _safe_price(self, sid: int, start_block: int, end_block: int) -> float:
        """Defensive wrapper around pricing oracle used only for ordering."""
        try:
            st = await self.parent._stxn()
            p = await average_price(sid, start_block=start_block, end_block=end_block, st=st)
            return float(getattr(p, "tao", 0.0) or 0.0)
        except Exception as e:
            pretty.log(f"[yellow]Price lookup failed for sid={sid}: {e}[/yellow]")
            return 0.0

    async def _estimate_prices_for_bids(self, bids: List[BidInput], start_block: int, end_block: int) -> Dict[int, float]:
        sids = sorted({b.subnet_id for b in bids})
        out: Dict[int, float] = {}
        for sid in sids:
            out[sid] = await self._safe_price(sid, start_block, end_block)
        return out

    def _compute_quota_caps(self, bids: List[BidInput], my_budget: float) -> Tuple[Dict[str, float], Dict[str, float]]:
        """
        Returns:
          cap_alpha_by_ck: coldkey -> cap α
          quota_frac_by_ck: coldkey -> normalized fraction q (for logs)
        """
        cks = sorted({b.coldkey for b in bids})
        if not cks or my_budget <= 0:
            return ({}, {})

        # Reputation comes from StateStore if present; default to 0.0
        rep_store: Dict[str, float] = getattr(self.state, "reputation", {}) or {}
        if not REPUTATION_ENABLED:
            N = max(1, len(cks))
            q = {ck: 1.0 / N for ck in cks}
            return ({ck: my_budget * q[ck] for ck in cks}, q)

        N = len(cks)
        rep = {ck: max(0.0, min(1.0, float(rep_store.get(ck, 0.0)))) for ck in cks}
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
        q = {ck: q_env[ck] / sum_env for ck in cks}  # re-normalized to sum 1

        caps = {ck: my_budget * q[ck] for ck in cks}
        return caps, q

    @staticmethod
    def _make_invoice_id(
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

    # ──────────────── main entrypoint ────────────────

    async def clear_now_and_notify(self, epoch_to_clear: int):
        """
        Clear and notify winners for the current epoch `epoch_to_clear`.
        Populates StateStore.pending_commits and persists it.
        """
        if epoch_to_clear is None or epoch_to_clear < 0:
            return

        # Must be a master to clear
        if not getattr(self.parent, "auction", None) or not self.parent.auction._is_master_now():
            return

        bid_map = self.parent.auction._bid_book.get(epoch_to_clear, {})
        if not bid_map:
            pretty.log(f"[grey]No bids to clear this epoch (master). epoch(e)={epoch_to_clear}[/grey]")
            return

        # 1) Compute this validator's α budget for this epoch from the master share snapshot
        share = float(self.parent.auction._my_share_by_epoch.get(epoch_to_clear, 0.0))
        my_budget = budget_from_share(share=share, auction_budget_alpha=AUCTION_BUDGET_ALPHA)
        if my_budget <= 0:
            pretty.log("[yellow]Budget share is zero – skipping clear.[/yellow]")
            return

        # 2) Build allocation inputs (subnet weights already snapped when accepting the bid)
        bids_in: List[BidInput] = self._to_bid_inputs(bid_map)

        # 2.a Estimate prices for **ordering** (do NOT write prices into commitments)
        start_block = int(self.parent.epoch_start_block)
        end_block = int(self.parent.epoch_end_block)
        price_by_sid: Dict[int, float] = await self._estimate_prices_for_bids(bids_in, start_block, end_block)

        # Ordered preview by estimated value for operator visibility
        ordered_preview = sorted(
            bids_in,
            key=lambda b: -(
                (b.weight_bps / 10_000.0)
                * (1.0 - b.discount_bps / 10_000.0)
                * price_by_sid.get(b.subnet_id, 0.0)
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

        # 4) Single‑pass greedy allocator honoring metric & caps
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
            # fallback map in case schema lacks alpha_requested
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
            pretty.table(
                "Allocations — Single Pass (caps enforced)",
                ["UID", "CK", "Subnet", "Req α", "Take α", "Disc_bps", "W_bps", "Cap rem (before→after)"],
                dbg_rows[:max(10, LOG_TOP_N)]
            )

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
        pay_epoch = int(epoch_to_clear + 1)
        epoch_len = int(self.parent.epoch_end_block - self.parent.epoch_start_block + 1)
        win_start = int(self.parent.epoch_end_block) + 1
        win_end = int(self.parent.epoch_end_block + epoch_len)

        inv_i: Dict[int, List[List[int]]] = defaultdict(list)
        for w in winners:
            req_rao_full = int(round(float(w.alpha_accepted) * PLANCK))  # FULL α; discount does NOT reduce payment
            inv_i[int(w.miner_uid)].append([int(w.subnet_id), int(w.weight_bps), int(req_rao_full), int(w.discount_bps)])

        # Persist to StateStore so CommitmentsEngine can publish in e+1
        self.state.pending_commits[str(epoch_to_clear)] = {
            "v": 3,  # payload schema label (kept for downstream compat)
            "e": int(epoch_to_clear),
            "pe": pay_epoch,
            "as": int(win_start),
            "de": int(win_end),
            "hk": self.parent.hotkey_ss58,  # include hotkey for cross-check
            "t": VALIDATOR_TREASURIES.get(self.parent.hotkey_ss58, ""),  # include treasury for cross-check
            "i": [[uid, lines] for uid, lines in inv_i.items()],
        }
        # StateStore should provide persistence
        if hasattr(self.state, "save_pending_commits"):
            self.state.save_pending_commits()

        # 6) Notify winners immediately; payment window = next epoch (e+1) with **defined end**
        ack_ok = 0
        calls_sent = 0
        targets_resolved = 0
        attempts = len(winners)

        v_uid = self._hotkey_to_uid().get(self.parent.hotkey_ss58, None)
        for w in winners:
            try:
                amount_rao_full = int(round(float(w.alpha_accepted) * PLANCK))
                req_alpha = req_alpha_by_win.get(id(w), getattr(w, "alpha_requested", w.alpha_accepted))
                invoice_id = self._make_invoice_id(
                    self.parent.hotkey_ss58, epoch_to_clear, w.subnet_id, w.alpha_accepted, w.discount_bps, pay_epoch, amount_rao_full
                )
                uid = int(w.miner_uid)
                if not (0 <= uid < len(self.parent.metagraph.axons)):
                    pretty.log(f"[yellow]Skip notify: invalid uid {uid} (no axon).[/yellow]")
                    continue

                target_ax = self.parent.metagraph.axons[uid]
                if not self._is_reachable_axon(target_ax):
                    pretty.log(f"[yellow]Skip notify: uid {uid} axon not reachable (host/port invalid).[/yellow]")
                    continue

                targets_resolved += 1
                partial = (w.alpha_accepted + EPS_ALPHA) < float(req_alpha)
                pretty.log(
                    f"[cyan]EARLY Win[/cyan] e={epoch_to_clear}→pay_e={pay_epoch} "
                    f"uid={uid} subnet={w.subnet_id} "
                    f"req={req_alpha:.4f} α acc={w.alpha_accepted:.4f} α "
                    f"disc={w.discount_bps}bps partial={partial} "
                    f"win=[{win_start},{win_end}] inv={invoice_id}"
                )
                resps = await self.parent.dendrite(
                    axons=[target_ax],
                    synapse=WinSynapse(
                        subnet_id=w.subnet_id,
                        alpha=w.alpha_accepted,  # accepted amount (FULL α to be paid)
                        clearing_discount_bps=w.discount_bps,
                        pay_window_start_block=win_start,
                        pay_window_end_block=win_end,
                        pay_epoch_index=pay_epoch,
                        validator_uid=v_uid,
                        validator_hotkey=self.parent.hotkey_ss58,
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
                # Use your logger if you prefer: clog.warning(...)
                pretty.log(f"[yellow]WinSynapse to uid={w.miner_uid} failed: {e_exc}[/yellow]")

        pretty.kv_panel(
            "Early Clear & Notify (epoch e)",
            [
                ("epoch_cleared (e)", epoch_to_clear),
                ("pay_epoch (e+1)", pay_epoch),
                ("winners", len(winners)),
                ("budget_left α", f"{remaining_budget:.3f}"),
                ("treasury", VALIDATOR_TREASURIES.get(self.parent.hotkey_ss58, "")),
                ("attempts", attempts),
                ("targets_resolved", targets_resolved),
                ("calls_sent", calls_sent),
                ("win_acks", f"{ack_ok}/{calls_sent}"),
            ],
            style="bold green",
        )
