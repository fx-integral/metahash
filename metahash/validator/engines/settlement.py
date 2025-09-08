# ╭────────────────────────────────────────────────────────────────────────╮
# metahash/validator/settlement.py — Simple settlement (TESTING-aware)
#
# Policy:
#   For each master snapshot (fetched from CID in v4 commitments):
#     • For each miner and subnet in the payload:
#         - Let required_rao = Σ (line.α_required_rao) on that subnet.
#         - Let paid_rao     = α actually paid by the miner to THIS master's treasury on THAT subnet
#                              within [as, de] (+ POST_PAYMENT_CHECK_DELAY_BLOCKS).
#         - If paid_rao >= required_rao:
#               reward_subnet = TAO(required_rao with slippage) and split across lines by α share:
#               reward_line   = (reward_subnet × line_share) × weight × (1 − discount)
#           else:
#               no credit for that subnet (strict full-pay rule).
#
# No jailing, no reputation updates, no burn accounting beyond the usual
# burn-all fallback if all scores end up 0 (or FORCE_BURN_WEIGHTS is set).
# TESTING mode: compute & log everything but DO NOT call set_weights().
# ╰────────────────────────────────────────────────────────────────────────╯

from __future__ import annotations

import asyncio
from collections import defaultdict
from decimal import Decimal, getcontext
from typing import Dict, List, Optional, Set, Tuple

from bittensor import BLOCKTIME
from metahash.utils.pretty_logs import pretty
from metahash.utils.ipfs import aget_json
from metahash.utils.commitments import read_all_plain_commitments
from metahash.validator.alpha_transfers import AlphaTransfersScanner
from metahash.treasuries import VALIDATOR_TREASURIES

from metahash.validator.state import StateStore
from metahash.utils.helpers import safe_json_loads, safe_int

from metahash.config import (
    TESTING,                         # ← single switch for "no real set_weights"
    POST_PAYMENT_CHECK_DELAY_BLOCKS,
    FORBIDDEN_ALPHA_SUBNETS,
    LOG_TOP_N,
    FORCE_BURN_WEIGHTS,
    K_SLIP,
    SLIP_TOLERANCE,
)

# high-precision, same as rewards.py
getcontext().prec = 60
_1e9 = Decimal(10) ** 9
K_SLIP_D = Decimal(str(K_SLIP))
SLIP_TOLERANCE_D = Decimal(str(SLIP_TOLERANCE))


class SettlementEngine:
    """
    Simplified settlement across ALL masters for epoch e−2.
    TESTING=True → compute everything, show weights, skip on-chain set_weights().
    """

    def __init__(self, parent, state: StateStore):
        self.parent = parent
        self.state = state
        self._scanner: Optional[AlphaTransfersScanner] = None
        self._rpc_lock: asyncio.Lock = parent._rpc_lock  # reuse parent's lock

    # ---------- public API ----------
    async def settle_and_set_weights_all_masters(self, epoch_to_settle: int):
        if epoch_to_settle < 0 or epoch_to_settle in self.state.validated_epochs:
            return

        st = await self.parent._stxn()
        try:
            commits = await read_all_plain_commitments(
                st, netuid=self.parent.config.netuid, block=None
            )
        except Exception as e:
            pretty.log(f"[red]Commitment read_all failed: {e}[/red]")
            return

        masters_hotkeys: Set[str] = set(VALIDATOR_TREASURIES.keys())
        masters_treasuries: Set[str] = set(VALIDATOR_TREASURIES.values())
        snapshots: List[Dict] = []

        # ── 1) Extract v4 snapshots (CID) or inline legacy for this epoch ──
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
                # v4 compact (CID-only)
                if s.get("v") == 4 and s.get("e") == epoch_to_settle and s.get("pe") == (epoch_to_settle + 1):
                    cid = s.get("c", "")
                    if isinstance(cid, str) and cid:
                        try:
                            full, _norm_bytes, _h = await aget_json(cid)
                            full_parsed = safe_json_loads(full)
                            if not isinstance(full_parsed, dict):
                                continue
                            if "as" not in full_parsed or "de" not in full_parsed:
                                continue
                            full_parsed.setdefault("e", s.get("e"))
                            full_parsed.setdefault("pe", s.get("pe"))
                            full_parsed.setdefault("hk", hk_key)
                            full_parsed.setdefault("t", VALIDATOR_TREASURIES.get(hk_key, ""))
                            chosen = dict(full_parsed)
                            break
                        except Exception as ipfs_exc:
                            # Dev fallback: if this is our own hk and we still have local pending payload, use it.
                            if hk_key == self.parent.hotkey_ss58:
                                local = self.state.pending_commits.get(str(epoch_to_settle))
                                if isinstance(local, dict) and int(local.get("pe", epoch_to_settle + 1)) == epoch_to_settle + 1:
                                    chosen = dict(local)
                                    chosen.setdefault("e", epoch_to_settle)
                                    chosen.setdefault("pe", epoch_to_settle + 1)
                                    chosen.setdefault("hk", hk_key)
                                    chosen.setdefault("t", VALIDATOR_TREASURIES.get(hk_key, ""))
                                    pretty.log("[magenta]Using local pending payload as fallback for our own v4 commit (IPFS fetch failed).[/magenta]")
                                    break
                            pretty.log(f"[yellow]Skip hk={hk_key[:6]}… — failed to fetch CID: {ipfs_exc}[/yellow]")
                            continue

                # Legacy inline (rare)
                if s.get("e") == epoch_to_settle and s.get("pe") == (epoch_to_settle + 1) and "as" in s and "de" in s:
                    chosen = dict(s)
                    chosen.setdefault("hk", hk_key)
                    chosen.setdefault("t", VALIDATOR_TREASURIES.get(hk_key, ""))
                    break

            if not chosen:
                continue

            tre = chosen.get("t", "")
            if hk_key not in masters_hotkeys and tre not in masters_treasuries:
                continue

            snapshots.append({"hk": hk_key, **chosen})

        miner_uids_all: List[int] = list(self.parent.get_miner_uids())

        if not snapshots:
            pretty.log("[grey]No master commitments found — applying burn-all weights (no payments to account).[/grey]")
            final_scores = [1.0 if uid == 0 else 0.0 for uid in miner_uids_all]
            self._log_final_scores_table(final_scores, miner_uids_all, reason="no commitments → burn-all")
            self._log_weights_preview(final_scores, miner_uids_all, mode="burn-all (no snapshots)")

            if not TESTING:
                self.parent.update_scores(final_scores, miner_uids_all)
                self.parent.set_weights()
            else:
                pretty.log("[yellow]TESTING: skipping on-chain set_weights().[/yellow]")

            self.state.validated_epochs.add(epoch_to_settle)
            self.state.save_validated_epochs()
            return

        # ── 2) Normalize snapshots & union payment window ──
        as_vals: List[int] = []
        de_vals: List[int] = []
        clean_snapshots: List[Dict] = []
        for s in snapshots:
            inv = self._normalize_inv(s)
            if inv is None:
                continue
            s["inv"] = inv
            a = safe_int(s.get("as"))
            d = safe_int(s.get("de"))
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
        pretty.show_settlement_window(
            epoch_to_settle, start_block, end_block, POST_PAYMENT_CHECK_DELAY_BLOCKS, len(clean_snapshots)
        )
        end_block += max(0, int(POST_PAYMENT_CHECK_DELAY_BLOCKS or 0))

        # Ensure pay window is closed
        if self.parent.block < end_block:
            remain = end_block - self.parent.block
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

        # ── 3) Scan events and build paid α by (uid, treasury, subnet) ──
        if self._scanner is None:
            self._scanner = AlphaTransfersScanner(await self.parent._stxn(), dest_coldkey=None, rpc_lock=self._rpc_lock)

        try:
            events = await self._scanner.scan(start_block, end_block)
            if not isinstance(events, list):
                events = []
        except Exception as scan_exc:
            pretty.kv_panel("Settlement postponed", [("epoch", epoch_to_settle), ("reason", f"scanner failed: {scan_exc}")], style="bold yellow")
            return

        master_treasuries: Set[str] = {VALIDATOR_TREASURIES.get(s.get("hk", ""), s.get("t", "")) for s in clean_snapshots}
        uids_present: Set[int] = set()
        subnets_needed: Set[int] = set()

        # Collect UIDs present in payloads & all subnets referenced
        for s in clean_snapshots:
            inv: Dict[str, Dict] = s.get("inv", {}) or {}
            for uid_s, inv_d in inv.items():
                try:
                    uid = int(uid_s)
                except Exception:
                    continue
                uids_present.add(uid)
                for ln in inv_d.get("ln", []) or []:
                    if isinstance(ln, list) and len(ln) >= 4:
                        subnets_needed.add(int(ln[0]))

        # Build coldkey→uid map limited to present UIDs
        ck_to_uid: Dict[str, int] = {}
        try:
            for uid, ck in enumerate(self.parent.metagraph.coldkeys):
                if uid in uids_present:
                    ck_to_uid[ck] = uid
        except Exception:
            pass

        # Sum α actually paid per (miner uid, treasury, subnet)
        paid_rao_by_uid_tre_sid: Dict[Tuple[int, str, int], int] = defaultdict(int)
        for ev in (events or []):
            try:
                if ev.dest_coldkey not in master_treasuries:
                    continue
                if ev.subnet_id in FORBIDDEN_ALPHA_SUBNETS:
                    continue
                uid = ck_to_uid.get(ev.src_coldkey)
                if uid is None or uid not in uids_present:
                    continue
                src_sid = getattr(ev, "src_subnet_id", ev.subnet_id)
                if src_sid != ev.subnet_id:
                    # Guard against cross-subnet crediting
                    continue
                paid_rao_by_uid_tre_sid[(uid, ev.dest_coldkey, ev.subnet_id)] += int(ev.amount_rao)
            except Exception:
                continue

        # ── 4) Price & depth caches ──
        price_cache: Dict[int, float] = {}
        depth_cache: Dict[int, int] = {}
        await self._populate_price_and_depth_caches(subnets_needed, start_block, end_block, price_cache, depth_cache)

        # ── 5) Score miners (strict full-pay per subnet; split TAO across lines) ──
        scores_by_uid: Dict[int, float] = defaultdict(float)

        for s in clean_snapshots:
            tre: str = s.get("t", "") or ""
            inv: Dict[str, Dict] = s.get("inv", {}) or {}

            for uid_s, inv_d in inv.items():
                try:
                    uid = int(uid_s)
                except Exception:
                    continue

                # Group lines by subnet: [sid, disc_bps, weight_bps, req_rao]
                lines_by_sid: Dict[int, List[Tuple[int, int, int, int]]] = defaultdict(list)
                for ln in inv_d.get("ln", []) or []:
                    if not (isinstance(ln, list) and len(ln) >= 4):
                        continue
                    sid = int(ln[0]); disc_bps = int(ln[1]); w_bps = int(ln[2]); req_rao = int(ln[3])
                    if req_rao > 0:
                        lines_by_sid[sid].append((sid, disc_bps, w_bps, req_rao))

                for sid, lines in lines_by_sid.items():
                    required_rao_total = sum(req_rao for (_, _, _, req_rao) in lines)
                    if required_rao_total <= 0:
                        continue

                    paid = int(paid_rao_by_uid_tre_sid.get((uid, tre, sid), 0))
                    if paid < required_rao_total:
                        # Not fully paid on this subnet → no credit
                        continue

                    # Convert the *required* α to TAO with slippage (aggregate then split)
                    price = float(price_cache.get(sid, 0.0))
                    depth = int(depth_cache.get(sid, 0))
                    tao_for_required = self._rao_to_tao(required_rao_total, price, depth)
                    if tao_for_required <= 0.0:
                        continue

                    for (_sid, disc_bps, w_bps, req_rao) in lines:
                        share = float(req_rao) / float(required_rao_total)
                        line_tao = tao_for_required * share
                        weight = max(0, min(10_000, w_bps)) / 10_000.0
                        disc = 1.0 - (max(0, min(10_000, disc_bps)) / 10_000.0)
                        scores_by_uid[uid] += line_tao * weight * disc

        # ── 6) Produce final scores & set weights (or TESTING preview) ──
        miner_uids_all = list(self.parent.get_miner_uids())
        final_scores: List[float] = [float(scores_by_uid.get(uid, 0.0)) for uid in miner_uids_all]

        burn_reason = None
        burn_all = FORCE_BURN_WEIGHTS or (not any(final_scores))
        if FORCE_BURN_WEIGHTS:
            burn_reason = "FORCE_BURN_WEIGHTS"
        elif not any(final_scores):
            burn_reason = "NO_POSITIVE_SCORES"

        self._log_final_scores_table(final_scores, miner_uids_all, reason=burn_reason)
        self._log_weights_preview(final_scores, miner_uids_all, mode="burn-all" if burn_all else "normal")

        if burn_all and not TESTING:
            pretty.log(f"[red]Burn-all triggered – reason: {burn_reason}.[/red]")
            final_scores = [1.0 if uid == 0 else 0.0 for uid in miner_uids_all]

        if not TESTING:
            pretty.log("[green]Applying weights on-chain.[/green]")
            self.parent.update_scores(final_scores, miner_uids_all)
            self.parent.set_weights()
        else:
            pretty.kv_panel("TESTING — on-chain set_weights() suppressed",
                            [("nonzero_miners", sum(1 for s in final_scores if s > 0)),
                             ("sum(scores)", f"{sum(final_scores):.6f}"),
                             ("mode", "burn-all" if burn_all else "normal")],
                            style="bold magenta")

        self.state.validated_epochs.add(epoch_to_settle)
        self.state.save_validated_epochs()
        pretty.kv_panel("Settlement Complete",
                        [("epoch_settled (e−2)", epoch_to_settle),
                         ("miners_scored", sum(1 for x in final_scores if x > 0)),
                         ("snapshots_used", len(clean_snapshots)),
                         ("testing", str(TESTING).lower())],
                        style="bold green")

    # ---------- helpers ----------

    def _decode_commit_entry(self, entry):
        if entry is None:
            return None
        if isinstance(entry, dict):
            raw = entry.get("raw")
            parsed = safe_json_loads(raw) if isinstance(raw, (str, bytes, bytearray, dict, list)) else None
            if parsed is not None:
                return parsed
            return entry
        if isinstance(entry, (str, bytes, bytearray)):
            parsed = safe_json_loads(entry)
            if parsed is not None:
                return parsed
            return None
        return None

    def _normalize_inv(self, s: Dict) -> Optional[Dict[str, Dict]]:
        """
        Normalize inventory shape to:
          inv = {
            "<uid>": { "ck": "<coldkey>", "ln": [[sid, disc_bps, weight_bps, required_rao], ...] },
            ...
          }
        Accepts legacy `i: [[uid, [[sid, weight_bps, required_rao, disc_bps?], ...]], ...]`.
        """
        inv: Dict[str, Dict] | None = s.get("inv")  # type: ignore[assignment]
        if isinstance(inv, dict) and inv:
            return inv

        inv_tmp: Dict[str, Dict] = {}
        if "i" in s and isinstance(s["i"], list):
            for item in s["i"]:
                if not (isinstance(item, list) and len(item) == 2):
                    continue
                uid, lines = item
                try:
                    uid_int = int(uid)
                except Exception:
                    continue
                ck = ""
                try:
                    if 0 <= uid_int < len(self.parent.metagraph.coldkeys):
                        ck = self.parent.metagraph.coldkeys[uid_int]
                except Exception:
                    ck = ""
                ln_list: List[List[int]] = []
                for ln in (lines or []):
                    if not (isinstance(ln, list) and len(ln) >= 3):
                        continue
                    try:
                        sid = int(ln[0])
                        w_bps = int(ln[1])
                        rao = int(ln[2])
                        disc = int(ln[3]) if len(ln) >= 4 else 0
                    except Exception:
                        continue
                    # normalized line: [sid, disc_bps, weight_bps, required_rao]
                    ln_list.append([sid, disc, w_bps, rao])
                inv_tmp[str(uid_int)] = {"ck": ck, "ln": ln_list}
        return inv_tmp if inv_tmp else None

    async def _populate_price_and_depth_caches(
        self,
        subnets_needed: Set[int],
        start: int,
        end: int,
        price_cache_out: Dict[int, float],
        depth_cache_out: Dict[int, int],
    ) -> None:
        from metahash.utils.subnet_utils import average_price, average_depth

        async def _fetch_price(sid: int) -> Tuple[int, float]:
            p = await average_price(sid, start_block=start, end_block=end, st=await self.parent._stxn())
            return sid, float(getattr(p, "tao", 0.0) or 0.0)

        async def _fetch_depth(sid: int) -> Tuple[int, int]:
            try:
                d = await average_depth(sid, start_block=start, end_block=end, st=await self.parent._stxn())
                return sid, int(d or 0)
            except Exception:
                return sid, 0

        price_pairs, depth_pairs = await asyncio.gather(
            asyncio.gather(*(_fetch_price(s) for s in sorted(subnets_needed))),
            asyncio.gather(*(_fetch_depth(s) for s in sorted(subnets_needed))),
        )
        price_cache_out.update(dict(price_pairs))
        depth_cache_out.update(dict(depth_pairs))

    def _rao_to_tao(self, alpha_rao: int, price_tao_per_alpha: float, depth_rao: int) -> float:
        """
        Same slippage model as rewards. Kept local to avoid cross-module imports.
        """
        if alpha_rao <= 0 or depth_rao <= 0 or price_tao_per_alpha <= 0:
            return 0.0
        a = Decimal(alpha_rao)
        d = Decimal(depth_rao)
        price = Decimal(str(price_tao_per_alpha))
        ratio = a / (d + a)
        slip = K_SLIP_D * ratio
        if slip <= SLIP_TOLERANCE_D:
            slip = Decimal(0)
        if slip > 1:
            slip = Decimal(1)
        tao = a * price * (Decimal(1) - slip) / _1e9
        return float(tao)

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

    def _log_weights_preview(self, scores: List[float], uids: List[int], mode: str):
        total = sum(scores) or 1.0
        weights = [s / total for s in scores]
        pairs = list(zip(uids, weights))
        pairs.sort(key=lambda x: x[1], reverse=True)
        top_n = max(1, int(LOG_TOP_N))
        rows = [[uid, f"{w:.6f}", f"{(w*100):.2f}%"] for uid, w in pairs[:top_n]]
        pretty.table(
            f"[magenta]WEIGHTS PREVIEW — mode={mode}[/magenta]",
            ["UID", "Weight", "% of total"],
            rows
        )
        pretty.kv_panel("Weights Summary",
                        [("nonzero", sum(1 for w in weights if w > 0)),
                         ("sum(weights)", f"{sum(weights):.6f}"),
                         ("max", f"{max(weights) if weights else 0:.6f}")],
                        style="bold magenta")
