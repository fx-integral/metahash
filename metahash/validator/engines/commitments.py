# metahash/validator/engines/commitments.py — Strict v4 publisher (CID-only on-chain; full payload in IPFS)
from __future__ import annotations

import asyncio
import json
from typing import Dict

from bittensor import BLOCKTIME
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

    async def publish_commitment_for(self, epoch_cleared: int, *, max_retries: int = 3) -> None:
        """
        Publish winners payload for epoch (e − 1) that was cleared.
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
            preview_inv = payload.get("inv") or payload.get("i")
            pretty.kv_panel(
                "Commit Payload (preview)",
                [
                    ("epoch", payload.get("e")),
                    ("pay_epoch(pe)", payload.get("pe")),
                    ("as", payload.get("as")),
                    ("de", payload.get("de")),
                    ("has_inv", str(bool(preview_inv)).lower()),
                ],
                style="bold cyan",
            )
        except IPFSError as ie:
            pretty.log(f"[yellow]IPFS publish failed (no fallback): {ie}[/yellow]")
            return
        except Exception as e:
            pretty.log(f"[yellow]IPFS publish failed (no fallback): {e}[/yellow]")
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
            pretty.log("[yellow]Commitment publish failed after retries (no fallback).[/yellow]")

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

    async def publish_catch_up(self, up_to_epoch: int, *, max_retries: int = 3):
        """
        Publish any staged payloads with key <= up_to_epoch.
        This prevents gaps if a previous epoch missed publishing.
        (Used post‑settlement in the validator to avoid overwriting e−2.)
        """
        if not self._is_master_now():
            return

        # Collect int keys safely
        keys: list[int] = []
        pcs = self.state.pending_commits if isinstance(self.state.pending_commits, dict) else {}
        for k in list(pcs.keys()):
            try:
                ki = int(k)
                keys.append(ki)
            except Exception:
                pretty.log(f"[yellow]Skipping non-integer pending key: {k!r}[/yellow]")

        for e in sorted(ki for ki in keys if ki <= int(up_to_epoch)):
            await self.publish_commitment_for(e, max_retries=max_retries)

    # ---------- internal: write with retry ----------
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
                pretty.log(f"[yellow]Commitment write returned False (attempt {attempt}/{max_retries}). Retrying…[/yellow]")
            except Exception as e:
                last_exc = e
                msg = str(e) if e else ""
                if "Priority is too low" in msg or "Transaction is outdated" in msg or "already imported" in msg:
                    pretty.log(f"[yellow]Commitment write pool conflict (attempt {attempt}/{max_retries}): {msg} — waiting ~1 block…[/yellow]")
                else:
                    pretty.log(f"[yellow]Commitment write exception (attempt {attempt}/{max_retries}): {msg} — waiting ~1 block…[/yellow]")

            try:
                await asyncio.sleep(max(1.0, float(BLOCKTIME)))
            except Exception:
                await asyncio.sleep(2.0)

        if last_exc:
            pretty.log(f"[yellow]On-chain commitment write failed after {max_retries} retries: {last_exc}[/yellow]")
        return False
