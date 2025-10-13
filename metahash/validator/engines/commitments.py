# metahash/validator/engines/commitments.py — Strict v4 publisher (CID-only on-chain; full payload in IPFS)
from __future__ import annotations

import asyncio
import json
from typing import Dict

from bittensor import BLOCKTIME
from metahash.utils.ipfs import aadd_json, minidumps as ipfs_minidumps, IPFSError
from metahash.utils.pretty_logs import pretty
from metahash.utils.phase_logs import set_phase, phase_start, phase_end, phase_table, phase_summary, compact_status, log as phase_log, grouped_info
from metahash.utils.commitments import write_plain_commitment_json
from metahash.validator.state import StateStore
from metahash.treasuries import VALIDATOR_TREASURIES  # only used by _is_master_now()


def _minidumps(obj: dict) -> str:
    # Minimal JSON: no extra whitespace, keep unicode as-is
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


class CommitmentsEngine:
    """
    Publish commitments with a single, strict behavior:

      • Store the full winners payload in IPFS (now includes bids/reputation/jails/weights snapshots).
      • Store a tiny, CID-only v4 object on-chain: {"v":4,"e":<e>,"pe":<e+1>,"c":"<cid>"}
      • Publish ONLY a specifically requested epoch (no global catch-up).
    """

    def __init__(self, parent, state: StateStore):
        self.parent = parent
        self.state = state

    async def publish_commitment_for(self, epoch_cleared: int, *, max_retries: int = 3) -> None:
        """
        Publish winners payload for a specific cleared epoch (typically e−1).
        Reads payload from state.pending_commits[str(epoch_cleared)] and does:

          1) IPFS: add the full payload as-is.
          2) On-chain: write v4 CID-only commitment with e=epoch_cleared, pe=epoch_cleared+1.

        Retries on common pool errors like "Priority is too low".
        """
        if epoch_cleared < 0:
            return
        if not self._is_master_now():
            return

        key = str(epoch_cleared)
        payload = self.state.pending_commits.get(key) if isinstance(self.state.pending_commits, dict) else None
        if not isinstance(payload, dict):
            phase_log(f"No pending winners to publish for epoch {epoch_cleared}.", 'commitments')
            return

        # diagnostics: record attempt
        self._touch_pending_meta(epoch_cleared, status="attempt")

        # 1) Upload full payload to IPFS (no modifications, no size checks)
        try:
            cid, sha_hex, byte_len = await aadd_json(
                payload,
                filename=f"commit_e{epoch_cleared}.json",
                pin=True,
                sort_keys=True,  # deterministic canonicalization for hash stability
            )
            preview_inv = payload.get("inv") or payload.get("i")
            has_bids = bool(payload.get("b") or payload.get("bids"))
            has_rej = bool(payload.get("rj"))
            has_rep = bool(payload.get("rep"))
            has_jail = bool(payload.get("jail"))
            has_wbps = bool(payload.get("wbps") or payload.get("weights_bps"))
            set_phase('commitments')
            phase_start('commitments', f"Publishing commitment for epoch {epoch_cleared}")
            phase_summary(
                'commitments',
                {
                    "epoch": payload.get("e"),
                    "pay_epoch(pe)": payload.get("pe"),
                    "as": payload.get("as"),
                    "de": payload.get("de"),
                    "has_inv": str(bool(preview_inv)).lower(),
                    "has_bids": str(has_bids).lower(),
                    "has_rejected": str(has_rej).lower(),
                    "has_rep": str(has_rep).lower(),
                    "has_jail": str(has_jail).lower(),
                    "has_weights": str(has_wbps).lower(),
                }
            )
            
            # Add structured payload inspection for debugging
            try:
                # Create a sanitized version for logging (remove sensitive data if any)
                payload_copy = payload.copy()
                
                # Enhanced payload breakdown with human-readable explanations
                self._display_enhanced_payload_breakdown(payload_copy, epoch_cleared)
                
            except Exception as e:
                phase_log(f"Failed to serialize payload for inspection: {e}", 'commitments', 'warning')
        except IPFSError as ie:
            phase_log(f"IPFS publish failed (no fallback): {ie}", 'commitments', 'warning')
            self._touch_pending_meta(epoch_cleared, status="ipfs_error", last_error=str(ie))
            return
        except Exception as e:
            phase_log(f"IPFS publish failed (no fallback): {e}", 'commitments', 'warning')
            self._touch_pending_meta(epoch_cleared, status="ipfs_error", last_error=str(e))
            return

        # 2) Write v4, CID-only commitment on-chain — with retry on pool priority
        commit_v4 = {
            "v": 4,
            "e": int(epoch_cleared),
            "pe": int(epoch_cleared + 1),
            "c": str(cid),
        }
        commit_str = ipfs_minidumps(commit_v4, sort_keys=True)

        ok = await self._write_commitment_with_retry(commit_str, max_retries=max_retries)
        if ok:
            set_phase('commitments')
            phase_summary(
                'commitments',
                {
                    "epoch_cleared": epoch_cleared,
                    "payment_epoch (pe)": epoch_cleared + 1,
                    "cid": str(cid),
                    "json_bytes@ipfs": byte_len,
                    "sha256": sha_hex,
                }
            )
            phase_end('commitments', f"Published epoch {epoch_cleared}")
            # Clear pending payload after successful publish
            if isinstance(self.state.pending_commits, dict):
                self.state.pending_commits.pop(key, None)
            self.state.save_pending_commits()
            self._mark_committed(epoch_cleared, cid)
        else:
            phase_log("Commitment publish failed after retries (no fallback).", 'commitments', 'warning')
            self._touch_pending_meta(epoch_cleared, status="onchain_error")

    async def publish_exact(self, epoch_cleared: int, *, max_retries: int = 3):
        """
        Strict publisher: only attempts to publish the pending payload for `epoch_cleared`.
        Never publishes older epochs. Older pendings remain as diagnostics (stale),
        and are never auto-published by this process.
        """
        if not self._is_master_now():
            return
        await self.publish_commitment_for(epoch_cleared, max_retries=max_retries)

    # ---------- utils ----------
    def _is_master_now(self) -> bool:
        """
        Minimal master check: requires a known treasury for our hotkey and stake ≥ S_MIN_MASTER_VALIDATOR.
        """
        tre = VALIDATOR_TREASURIES.get(self.parent.hotkey_ss58)
        if not tre:
            return False
        uid = self._hotkey_to_uid().get(self.parent.hotkey_ss58)
        if uid is None:
            return False
        from metahash.config import S_MIN_MASTER_VALIDATOR
        try:
            return float(self.parent.metagraph.stake[uid]) >= S_MIN_MASTER_VALIDATOR
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

    async def _write_commitment_with_retry(self, commit_str: str, *, max_retries: int = 3) -> bool:
        """
        Sends the commitment extrinsic, retrying on common pool errors (e.g., 'Priority is too low').
        Uses the parent's RPC lock to serialize submissions from this process.
        """
        last_exc: Exception | None = None
        for attempt in range(1, int(max_retries) + 1):
            try:
                st = await self.parent._stxn()
                async with self.parent._rpc_lock:
                    ok = await write_plain_commitment_json(
                        st,
                        wallet=self.parent.wallet,
                        data=commit_str,
                        netuid=self.parent.config.netuid,
                    )
                if ok:
                    return True
                phase_log(f"Commitment write returned False (attempt {attempt}/{max_retries}). Retrying…", 'commitments', 'warning')
            except Exception as e:
                last_exc = e
                msg = str(e) if e else ""
                if "Priority is too low" in msg or "Transaction is outdated" in msg or "already imported" in msg:
                    phase_log(f"Commitment write pool conflict (attempt {attempt}/{max_retries}): {msg} — waiting ~1 block…", 'commitments', 'warning')
                else:
                    phase_log(f"Commitment write exception (attempt {attempt}/{max_retries}): {msg} — waiting ~1 block…", 'commitments', 'warning')

            try:
                await asyncio.sleep(max(1.0, float(BLOCKTIME)))
            except Exception:
                await asyncio.sleep(2.0)

        if last_exc:
            phase_log(f"On-chain commitment write failed after {max_retries} retries: {last_exc}", 'commitments', 'warning')
        return False

    # ---------- enhanced payload display ----------
    def _display_enhanced_payload_breakdown(self, payload: dict, epoch_cleared: int):
        """Display payload with human-readable field explanations and structured breakdown."""
        
        # Basic payload info
        payload_size = len(json.dumps(payload, separators=(',', ':')))
        phase_table(
            'commitments',
            "Payload Overview",
            ["Key", "Value"],
            [
                ["epoch_cleared", epoch_cleared],
                ["payload_size_bytes", payload_size],
                ["payload_version", payload.get("v", "unknown")],
                ["total_fields", len(payload)],
            ],
        )
        
        # Field mapping with explanations
        field_explanations = {
            "e": "epoch",
            "pe": "payment_epoch", 
            "as": "auction_start_block",
            "de": "auction_end_block",
            "hk": "validator_hotkey",
            "t": "treasury_coldkey",
            "v": "payload_version",
            "b": "winner_bids",
            "i": "all_bids", 
            "inv": "invoices",
            "rj": "rejected_bids",
            "rep": "reputation_data",
            "jail": "jailed_miners",
            "wbps": "subnet_weights",
            "ck_by_uid": "coldkey_mapping",
            "bl_mu": "budget_leftover_mu",
            "bl_tao": "budget_leftover_tao",
            "bt_mu": "budget_total_mu", 
            "bt_tao": "budget_total_tao",
        }
        
        # Display field explanations
        explained_fields = []
        for key, explanation in field_explanations.items():
            if key in payload:
                explained_fields.append(f"{key} → {explanation}")
        
        if explained_fields:
            phase_table(
                'commitments',
                "Field Explanations",
                ["Key", "Value"],
                [
                    ["fields_present", len(explained_fields)],
                    ["field_mapping", "; ".join(explained_fields[:5]) + ("..." if len(explained_fields) > 5 else "")],
                ],
            )
        
        # Detailed breakdown of key sections
        self._display_bids_breakdown(payload)
        self._display_reputation_breakdown(payload)
        self._display_weights_breakdown(payload)
        self._display_budget_breakdown(payload)
        self._display_timeline_breakdown(payload)
        
        # Raw payload for reference (condensed)
        try:
            condensed_payload = self._condense_payload_for_display(payload)
            phase_table(
                'commitments',
                "Raw Payload (Condensed)",
                ["Key", "Value"],
                [["full_payload", json.dumps(condensed_payload, indent=2, sort_keys=True)]],
            )
        except Exception as e:
            pretty.log(f"[yellow]Failed to display condensed payload: {e}[/yellow]")

    def _display_bids_breakdown(self, payload: dict):
        """Display detailed breakdown of bids data."""
        winner_bids = payload.get("b", [])
        all_bids = payload.get("i", [])
        rejected_bids = payload.get("rj", [])
        
        if winner_bids or all_bids:
            phase_table(
                'commitments',
                "Bids Analysis",
                ["Metric", "Value"],
                [
                    ["winner_bids_count", len(winner_bids) if isinstance(winner_bids, list) else 0],
                    ["all_bids_count", len(all_bids) if isinstance(all_bids, list) else 0],
                    ["rejected_bids_count", len(rejected_bids) if isinstance(rejected_bids, list) else 0],
                    ["win_rate", f"{len(winner_bids) / max(1, len(all_bids)) * 100:.1f}%" if isinstance(all_bids, list) and all_bids else "0%"],
                ],
            )
            
            # Show sample winner bids
            if winner_bids and isinstance(winner_bids, list):
                sample_winners = []
                for i, bid_group in enumerate(winner_bids[:3]):  # Show first 3 winner groups
                    if isinstance(bid_group, list) and len(bid_group) >= 2:
                        uid = bid_group[0] if isinstance(bid_group[0], int) else "?"
                        bids = bid_group[1] if isinstance(bid_group[1], list) else []
                        if bids:
                            for bid in bids[:2]:  # Show first 2 bids per winner
                                if isinstance(bid, list) and len(bid) >= 5:
                                    subnet_id, discount_bps, weight_bps, amount_rao, value_mu = bid[:5]
                                    sample_winners.append(f"UID{uid}: SN-{subnet_id}, {amount_rao/1e9:.4f}α, {discount_bps}bps")
                
                if sample_winners:
                    phase_table(
                        'commitments',
                        "Sample Winner Bids",
                        ["Key", "Value"],
                        [["sample_winners", "; ".join(sample_winners[:3])]],
                    )

    def _display_reputation_breakdown(self, payload: dict):
        """Display detailed breakdown of reputation data."""
        rep_data = payload.get("rep", {})
        if rep_data and isinstance(rep_data, dict):
            scores = rep_data.get("scores", {})
            caps = rep_data.get("cap_tao_mu", {})
            quotas = rep_data.get("quota_frac", {})
            
            phase_table(
                'commitments',
                "Reputation System",
                ["Metric", "Value"],
                [
                    ["enabled", str(bool(rep_data.get("enabled", False))).lower()],
                    ["baseline_score", rep_data.get("baseline", 0.0)],
                    ["cap_max", rep_data.get("capmax", 0.0)],
                    ["gamma", rep_data.get("gamma", 0.0)],
                    ["miners_with_scores", len(scores) if isinstance(scores, dict) else 0],
                    ["miners_with_caps", len(caps) if isinstance(caps, dict) else 0],
                ],
            )
            
            # Show sample reputation data
            if scores and isinstance(scores, dict):
                sample_scores = []
                for ck, score in list(scores.items())[:3]:
                    ck_short = ck[:8] + "…" if len(ck) > 8 else ck
                    sample_scores.append(f"{ck_short}: {score:.3f}")
                
                if sample_scores:
                    phase_table(
                        'commitments',
                        "Sample Reputation Scores",
                        ["Key", "Value"],
                        [["sample_scores", "; ".join(sample_scores)]],
                    )

    def _display_weights_breakdown(self, payload: dict):
        """Display detailed breakdown of subnet weights."""
        weights = payload.get("wbps", {})
        if weights and isinstance(weights, dict):
            weight_map = weights.get("map", {})
            default_weight = weights.get("default", 10000)
            
            phase_table(
                'commitments',
                "Subnet Weights",
                ["Metric", "Value"],
                [
                    ["default_weight", f"{default_weight} bps"],
                    ["configured_subnets", len(weight_map) if isinstance(weight_map, dict) else 0],
                ],
            )
            
            # Show weight details
            if weight_map and isinstance(weight_map, dict):
                weight_details = []
                for subnet_id, weight_bps in list(weight_map.items())[:5]:
                    weight_details.append(f"SN-{subnet_id}: {weight_bps} bps")
                
                if weight_details:
                    phase_table(
                        'commitments',
                        "Weight Details",
                        ["Key", "Value"],
                        [["weight_details", "; ".join(weight_details)]],
                    )

    def _display_budget_breakdown(self, payload: dict):
        """Display detailed breakdown of budget information."""
        bl_mu = payload.get("bl_mu", 0)
        bl_tao = payload.get("bl_tao", 0.0)
        bt_mu = payload.get("bt_mu", 0)
        bt_tao = payload.get("bt_tao", 0.0)
        
        if bl_mu or bl_tao or bt_mu or bt_tao:
            phase_table(
                'commitments',
                "Budget Information",
                ["Metric", "Value"],
                [
                    ["budget_total_tao", f"{bt_tao:.6f}"],
                    ["budget_leftover_tao", f"{bl_tao:.6f}"],
                    ["budget_used_tao", f"{bt_tao - bl_tao:.6f}"],
                    ["budget_utilization", f"{(bt_tao - bl_tao) / max(bt_tao, 1e-12) * 100:.1f}%"],
                ],
            )

    def _display_timeline_breakdown(self, payload: dict):
        """Display detailed breakdown of timeline information."""
        epoch = payload.get("e")
        pay_epoch = payload.get("pe")
        start_block = payload.get("as")
        end_block = payload.get("de")
        
        if epoch is not None and pay_epoch is not None and start_block is not None and end_block is not None:
            window_blocks = end_block - start_block
            phase_table(
                'commitments',
                "Timeline Information",
                ["Metric", "Value"],
                [
                    ["epoch", epoch],
                    ["payment_epoch", pay_epoch],
                    ["auction_start_block", start_block],
                    ["auction_end_block", end_block],
                    ["payment_window_blocks", window_blocks],
                    ["estimated_duration_minutes", f"{window_blocks * 12 / 60:.1f}"],
                ],
            )

    def _condense_payload_for_display(self, payload: dict) -> dict:
        """Create a condensed version of the payload for display, with human-readable keys."""
        condensed = {}
        
        # Map cryptic keys to readable ones
        key_mapping = {
            "e": "epoch",
            "pe": "payment_epoch",
            "as": "auction_start_block", 
            "de": "auction_end_block",
            "hk": "validator_hotkey",
            "t": "treasury_coldkey",
            "v": "version",
            "b": "winner_bids",
            "i": "all_bids",
            "inv": "invoices",
            "rj": "rejected_bids",
            "rep": "reputation",
            "jail": "jailed_miners",
            "wbps": "subnet_weights",
            "ck_by_uid": "coldkey_mapping",
            "bl_mu": "budget_leftover_mu",
            "bl_tao": "budget_leftover_tao",
            "bt_mu": "budget_total_mu",
            "bt_tao": "budget_total_tao",
        }
        
        for old_key, new_key in key_mapping.items():
            if old_key in payload:
                condensed[new_key] = payload[old_key]
        
        return condensed

    # ---------- diagnostics helpers ----------
    def _touch_pending_meta(self, epoch: int, *, status: str, last_error: str | None = None):
        """
        Track attempts/errors without altering the payload itself.
        pending_commits_meta structure (in StateStore) is an aux map: { "<e>": {status, tries, last_error, ts} }
        """
        try:
            from time import time as _now
            meta = getattr(self.state, "pending_commits_meta", None)
            if meta is None:
                self.state.pending_commits_meta = {}
                meta = self.state.pending_commits_meta
            entry = meta.get(str(epoch), {"tries": 0})
            entry["tries"] = int(entry.get("tries", 0)) + (1 if status == "attempt" else 0)
            entry["status"] = status
            entry["last_error"] = last_error or entry.get("last_error")
            entry["ts"] = int(_now())
            meta[str(epoch)] = entry
            self.state.save_pending_commits()  # reuse same persistence path
        except Exception:
            pass

    def _mark_committed(self, epoch: int, cid: str):
        """
        Record a local 'committed' mark to avoid accidental re-commit attempts.
        """
        try:
            committed = getattr(self.state, "committed_epochs", None)
            if committed is None:
                self.state.committed_epochs = {}
                committed = self.state.committed_epochs
            committed[str(epoch)] = {"cid": cid}
            self.state.save_pending_commits()
        except Exception:
            pass
