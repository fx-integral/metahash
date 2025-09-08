# neurons/commitments.py
from __future__ import annotations

import json
from typing import Dict

from metahash.utils.ipfs import aadd_json, minidumps as ipfs_minidumps, IPFSError
from metahash.utils.pretty_logs import pretty
from metahash.utils.commitments import write_plain_commitment_json
from metahash.validator.state import StateStore
from metahash.treasuries import VALIDATOR_TREASURIES  # only used by _is_master_now()


def _minidumps(obj: dict) -> str:
    # Minimal JSON: no extra whitespace, keep unicode as-is
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


class CommitmentsEngine:
    """
    Publish commitments with a single, strict behavior:

      • Store the full winners payload in IPFS.
      • Store a tiny, CID-only v4 object on-chain: {"v":4,"e":<e>,"pe":<e+1>,"c":"<cid>"}

    No inline fallback. No byte-size checks. No payload trimming. No mutation of the payload.
    """

    def __init__(self, parent, state: StateStore):
        self.parent = parent
        self.state = state

    async def publish_commitment_for(self, epoch_cleared: int):
        """
        Publish winners payload for epoch (e - 1) that was cleared.
        Reads payload from state.pending_commits[str(epoch_cleared)] and does:

          1) IPFS: add the full payload as-is.
          2) On-chain: write v4 CID-only commitment with e=epoch_cleared, pe=epoch_cleared+1.

        If anything fails, it logs and returns (no fallback).
        """
        if epoch_cleared < 0:
            return
        if not self._is_master_now():
            return

        key = str(epoch_cleared)
        payload = self.state.pending_commits.get(key)
        if not isinstance(payload, dict):
            pretty.log(f"[grey]No pending winners to publish for epoch {epoch_cleared}.[/grey]")
            return

        # 1) Upload full payload to IPFS (no modifications, no size checks)
        try:
            cid, sha_hex, byte_len = await aadd_json(
                payload,
                filename=f"commit_e{epoch_cleared}.json",
                pin=True,
                sort_keys=True,  # deterministic canonicalization for hash stability
            )
        except IPFSError as ie:
            pretty.log(f"[yellow]IPFS publish failed (no fallback): {ie}[/yellow]")
            return
        except Exception as e:
            pretty.log(f"[yellow]IPFS publish failed (no fallback): {e}[/yellow]")
            return

        # 2) Write v4, CID-only commitment on-chain
        commit_v4 = {
            "v": 4,
            "e": int(epoch_cleared),
            "pe": int(epoch_cleared + 1),
            "c": str(cid),
        }
        commit_str = ipfs_minidumps(commit_v4, sort_keys=True)

        try:
            st = await self.parent._stxn()
            ok = await write_plain_commitment_json(
                st,
                wallet=self.parent.wallet,
                data=commit_str,
                netuid=self.parent.config.netuid,
            )
        except Exception as e:
            pretty.log(f"[yellow]On-chain commitment write failed (no fallback): {e}[/yellow]")
            return

        if ok:
            pretty.kv_panel(
                "Commitment Published (v4 CID-only, full payload in IPFS)",
                [
                    ("epoch_cleared", epoch_cleared),
                    ("payment_epoch (pe)", epoch_cleared + 1),
                    ("cid", str(cid)),
                    ("json_bytes@ipfs", byte_len),
                    ("sha256", sha_hex),
                ],
                style="bold green",
            )
            # Clear pending payload after successful publish
            self.state.pending_commits.pop(key, None)
            self.state.save_pending_commits()
        else:
            pretty.log("[yellow]Commitment publish returned False (no fallback).[/yellow]")

    # ---------- utils ----------
    def _is_master_now(self) -> bool:
        """
        Minimal master check: requires a known treasury for our hotkey and stake >= S_MIN_MASTER_VALIDATOR.
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
