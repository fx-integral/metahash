# neurons/clearing.py
from __future__ import annotations

import hashlib
import inspect
from collections import defaultdict
from typing import Dict, List, Tuple

from metahash.utils.pretty_logs import pretty
from metahash.validator.state import StateStore

# Config & rewards utilities
from metahash.config import (AUCTION_BUDGET_ALPHA, LOG_TOP_N, METAHASH_SUBNET_ID, PLANCK,
    REPUTATION_BASELINE_CAP_FRAC, REPUTATION_ENABLED, REPUTATION_MAX_CAP_FRAC,
    REPUTATION_MIX_GAMMA)
from metahash.validator.rewards import WinAllocation, BidInput, budget_tao_from_share
from metahash.protocol import WinSynapse
from treasuries import VALIDATOR_TREASURIES

# Price oracle (for ordering and budget conversion; prices are NOT written into commitments)
from metahash.utils.subnet_utils import average_price


EPS_ALPHA = 1e-12  # tolerance used to mark partial fills


class ClearingEngine:
    """
    Clear winners *now* for the current epoch and notify miners.
    Populates StateStore.pending_commits[e] with the exact pay window (as,de) used,
    so CommitmentsEngine can publish the same snapshot in e+1.

    CHANGES:
      - Budget is enforced in TAO, not α. Base budget = AUCTION_BUDGET_ALPHA (α of validator's netuid) * price_tao_per_alpha[base].
      - Per-CK quotas are caps in TAO (normalized reputation), enforced during allocation.
      - Ordering and consumption metric (TAO):
            per_unit_tao = weight_frac * (1 - discount) * price_tao_per_alpha[subnet]
            line_tao     = per_unit_tao * alpha_taken
      - Invoices & WinSynapse continue to carry FULL α owed (discount does not reduce α).
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
        """Defensive wrapper around pricing oracle used for ordering and budget conversion."""
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

    def _compute_quota_caps_tao(self, bids: List[BidInput], my_budget_tao: float) -> Tuple[Dict[str, float], Dict[str, float]]:
        """
        Returns:
          cap_tao_by_ck: coldkey -> cap in TAO
          quota_frac_by_ck: coldkey -> normalized fraction q (for logs)
        """
        cks = sorted({b.coldkey for b in bids})
        if not cks or my_budget_tao <= 0:
            return ({}, {})

        # Reputation comes from StateStore if present; default to 0.0
        rep_store: Dict[str, float] = getattr(self.state, "reputation", {}) or {}
        if not REPUTATION_ENABLED:
            N = max(1, len(cks))
            q = {ck: 1.0 / N for ck in cks}
            return ({ck: my_budget_tao * q[ck] for ck in cks}, q)

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

        caps = {ck: my_budget_tao * q[ck] for ck in cks}
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

        Budget is enforced in TAO, computed from (my_share * AUCTION_BUDGET_ALPHA[base]) * price_tao_per_alpha[base].
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

        # 1) Compute share and TAO budget for this epoch from master share snapshot
        share = float(self.parent.auction._my_share_by_epoch.get(epoch_to_clear, 0.0))

        start_block = int(self.parent.epoch_start_block)
        end_block = int(self.parent.epoch_end_block)

        # Base subnet = this validator's netuid (e.g., SN-73)
        base_sid = int(getattr(self.parent.config, "netuid", METAHASH_SUBNET_ID))
        base_price_tao_per_alpha = await self._safe_price(base_sid, start_block, end_block)

        my_budget_tao = budget_tao_from_share(
            share=share,
            auction_budget_alpha=AUCTION_BUDGET_ALPHA,     # α (of base subnet) per epoch
            price_tao_per_alpha_base=base_price_tao_per_alpha,
        )

        if my_budget_tao <= 0:
            pretty.log("[yellow]TAO budget share is zero – skipping clear.[/yellow]")
            return

        pretty.kv_panel(
            "Budget (TAO) — base subnet",
            [
                ("epoch", epoch_to_clear),
                ("base_sid", base_sid),
                ("base_price_tao/α", f"{base_price_tao_per_alpha:.8f}"),
                ("share", f"{share:.6f}"),
                ("AUCTION_BUDGET_ALPHA(α)", f"{float(AUCTION_BUDGET_ALPHA):.6f}"),
                ("my_budget_tao", f"{my_budget_tao:.6f}"),
            ],
            style="bold cyan",
        )

        # 2) Build allocation inputs (subnet weights already snapped when accepting the bid)
        bids_in: List[BidInput] = self._to_bid_inputs(bid_map)

        # 2.a Estimate prices for **ordering + TAO valuation** (do NOT write prices into commitments)
        price_by_sid: Dict[int, float] = await self._estimate_prices_for_bids(bids_in, start_block, end_block)

        def per_unit_tao(b: BidInput) -> float:
            w_frac = (b.weight_bps / 10_000.0)
            disc = (1.0 - b.discount_bps / 10_000.0)
            price = price_by_sid.get(b.subnet_id, 0.0)
            return w_frac * disc * price

        # Ordered preview by estimated TAO value for operator visibility
        ordered_preview = sorted(
            bids_in,
            key=lambda b: -(per_unit_tao(b) * max(0.0, float(b.alpha))),
        )
        rows_bids = []
        for b in ordered_preview[:max(5, LOG_TOP_N)]:
            pu = per_unit_tao(b)
            line_val = pu * float(b.alpha)
            rows_bids.append([
                b.miner_uid, b.coldkey[:8] + "…", b.subnet_id, b.weight_bps, b.discount_bps,
                f"{b.alpha:.4f} α", f"{pu:.10f} TAO/α", f"{line_val:.6f} TAO"
            ])
        if rows_bids:
            pretty.table(
                "[yellow]Bids (ordered by TAO value)[/yellow]",
                ["UID", "CK", "Subnet", "W_bps", "Disc_bps", "Req α", "PU TAO/α", "Line TAO"],
                rows_bids
            )

        # 3) Single‑pass quotas from normalized reputation (caps in TAO)
        cap_tao_by_ck, quota_frac = self._compute_quota_caps_tao(bids_in, my_budget_tao)
        cap_rows = [[ck[:8] + "…", f"{quota_frac.get(ck, 0.0):.3f}", f"{cap_tao_by_ck.get(ck, 0.0):.6f} TAO"] for ck in sorted(cap_tao_by_ck.keys())]
        if cap_rows:
            pretty.table("Per‑Coldkey Quotas (normalized) → Caps(TAO)", ["Coldkey", "Quota frac", "Cap TAO"], cap_rows)

        # 4) TAO‑budget greedy allocator honoring per‑CK TAO caps
        ordered = sorted(bids_in, key=lambda b: (per_unit_tao(b), float(b.alpha), -int(b.idx)), reverse=True)

        remaining_budget_tao = float(my_budget_tao)
        cap_left_tao = {ck: float(v) for ck, v in cap_tao_by_ck.items()}

        winners: List[WinAllocation] = []
        dbg_rows = []
        req_alpha_by_win: Dict[int, float] = {}
        tao_taken_by_ck: Dict[str, float] = defaultdict(float)

        def _make_winalloc(b: BidInput, take_alpha: float) -> WinAllocation:
            kwargs = dict(
                miner_uid=b.miner_uid,
                coldkey=b.coldkey,
                subnet_id=b.subnet_id,
                alpha_accepted=take_alpha,
                discount_bps=b.discount_bps,
                weight_bps=b.weight_bps,
            )
            if "alpha_requested" in self._winalloc_params:
                kwargs["alpha_requested"] = float(b.alpha)
            w = WinAllocation(**kwargs)
            req_alpha_by_win[id(w)] = float(b.alpha)
            return w

        for b in ordered:
            if remaining_budget_tao <= 0:
                break
            ck = b.coldkey
            want_alpha = max(0.0, float(b.alpha))
            if want_alpha <= 0:
                continue

            pu_tao = per_unit_tao(b)  # TAO per α accepted
            if pu_tao <= 0:
                continue

            cap_avail_tao = cap_left_tao.get(ck, remaining_budget_tao)
            if cap_avail_tao <= 0:
                continue

            # Max TAO we can spend on this bid (respecting remaining budget, CK cap, and request size)
            max_tao_this_bid = min(remaining_budget_tao, cap_avail_tao, want_alpha * pu_tao)
            if max_tao_this_bid <= 0:
                continue

            take_alpha = max_tao_this_bid / pu_tao
            if take_alpha <= 0:
                continue

            w = _make_winalloc(b, take_alpha)
            winners.append(w)

            # Debug row: show α vs TAO consumption and caps
            tao_consumed = take_alpha * pu_tao
            dbg_rows.append([
                b.miner_uid, str(ck)[:8] + "…", b.subnet_id,
                f"{b.alpha:.4f} α", f"{take_alpha:.4f} α",
                f"{pu_tao:.10f} TAO/α", f"{tao_consumed:.6f} TAO",
                b.discount_bps, b.weight_bps,
                f"{cap_avail_tao:.6f}→{cap_avail_tao - tao_consumed:.6f} TAO"
            ])

            remaining_budget_tao -= tao_consumed
            cap_left_tao[ck] = max(0.0, cap_avail_tao - tao_consumed)
            tao_taken_by_ck[ck] += tao_consumed

        if dbg_rows:
            pretty.table(
                "Allocations — TAO Budget (caps enforced in TAO)",
                ["UID", "CK", "Subnet", "Req α", "Take α", "PU TAO/α", "Spend TAO", "Disc_bps", "W_bps", "Cap TAO (before→after)"],
                dbg_rows[:max(10, LOG_TOP_N)]
            )

        if not winners:
            pretty.log("[red]No winners after applying TAO quotas & budget.[/red]")
            return

        rows_w = []
        for w in winners[:max(10, LOG_TOP_N)]:
            req_alpha = req_alpha_by_win.get(id(w), getattr(w, "alpha_requested", w.alpha_accepted))
            pu = per_unit_tao(BidInput(
                miner_uid=w.miner_uid, coldkey=w.coldkey, subnet_id=w.subnet_id,
                alpha=req_alpha, discount_bps=w.discount_bps, weight_bps=w.weight_bps, idx=0
            ))
            rows_w.append([
                w.miner_uid, w.coldkey[:8] + "…", w.subnet_id,
                f"{req_alpha:.4f} α",
                f"{w.alpha_accepted:.4f} α",
                w.discount_bps, w.weight_bps,
                f"{(w.alpha_accepted * pu):.6f} TAO",
                "P" if (w.alpha_accepted + EPS_ALPHA) < float(req_alpha) else "F"
            ])
        pretty.table(
            "Winners — detail (TAO spend)",
            ["UID", "CK", "Subnet", "Req α", "Acc α", "Disc_bps", "W_bps", "Spend TAO", "Fill"],
            rows_w
        )

        winners_aggr_alpha: Dict[str, float] = defaultdict(float)
        for w in winners:
            winners_aggr_alpha[w.coldkey] += w.alpha_accepted
        pretty.show_winners(list(winners_aggr_alpha.items()))

        # 5) Stash pending commitment snapshot (publish later with SAME window)
        pay_epoch = int(epoch_to_clear + 1)
        epoch_len = int(self.parent.epoch_end_block - self.parent.epoch_start_block + 1)
        win_start = int(self.parent.epoch_end_block) + 1
        win_end = int(self.parent.epoch_end_block + epoch_len)

        inv_i: Dict[int, List[List[int]]] = defaultdict(list)
        for w in winners:
            # FULL α; discount does NOT reduce payment owed on-chain
            req_rao_full = int(round(float(w.alpha_accepted) * PLANCK))
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
                        alpha=w.alpha_accepted,  # FULL α to be paid by the miner
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
                pretty.log(f"[yellow]WinSynapse to uid={w.miner_uid} failed: {e_exc}[/yellow]")

        pretty.kv_panel(
            "Early Clear & Notify (epoch e)",
            [
                ("epoch_cleared (e)", epoch_to_clear),
                ("pay_epoch (e+1)", pay_epoch),
                ("winners", len(winners)),
                ("budget_left TAO", f"{remaining_budget_tao:.6f}"),
                ("treasury", VALIDATOR_TREASURIES.get(self.parent.hotkey_ss58, "")),
                ("attempts", attempts),
                ("targets_resolved", targets_resolved),
                ("calls_sent", calls_sent),
                ("win_acks", f"{ack_ok}/{calls_sent}"),
            ],
            style="bold green",
        )
