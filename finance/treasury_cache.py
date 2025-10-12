# metahash/finance/treasury_cache.py
# --------------------------------------------------------------------------- #
# Utilities for maintaining an append-only cache of treasury stake transfers.
# The cache is persisted as JSON so it can be inspected manually and re-used
# by downstream analytics without re-scanning the chain each time.
# --------------------------------------------------------------------------- #

from __future__ import annotations

import json
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple, Callable

import bittensor as bt
from metahash.validator.alpha_transfers import AlphaTransfersScanner, TransferEvent

from .constants import BLOCKS_PER_DAY, LOCK_PERIOD_DAYS
from .models import TransactionRecord, Direction

try:
    from tqdm import tqdm
except ImportError:  # pragma: no cover
    tqdm = None  # type: ignore


def _tx_key(tx: TransactionRecord) -> Tuple[int, int, int, str, str, Direction]:
    """Lightweight deduplication key."""
    return (tx.block, tx.subnet_id, tx.amount_rao, tx.src, tx.dest, tx.direction)


class TreasuryTransactionCache:
    """
    JSON-backed cache that stores every transfer touching the treasury coldkeys.

    The cache keeps track of:
      • metadata (network, tracked addresses, lock policy)
      • a chronological list of transactions touching any monitored treasury
    """

    def __init__(
        self,
        cache_path: Path,
        *,
        treasuries: Iterable[str],
        network: str,
    ) -> None:
        self.path = Path(cache_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

        self.network = network
        self.treasury_set: Set[str] = {addr.strip() for addr in treasuries if addr}

        self._records: List[TransactionRecord] = []
        self._seen: Set[Tuple[int, int, int, str, str, Direction]] = set()
        self.meta: Dict[str, object] = {}

        self._load()

    # ------------------------------------------------------------------ #
    # Persistence helpers
    # ------------------------------------------------------------------ #

    def _load(self) -> None:
        if not self.path.exists():
            self.meta = {
                "network": self.network,
                "treasuries": sorted(self.treasury_set),
                "lock_period_days": LOCK_PERIOD_DAYS,
                "blocks_per_day": BLOCKS_PER_DAY,
                "last_block": None,
                "last_updated": None,
            }
            self._records = []
            self._seen = set()
            return

        try:
            with self.path.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except Exception as exc:  # pragma: no cover - logged upstream
            bt.logging.error(f"[treasury-cache] Failed to load cache {self.path}: {exc}")
            self.meta = {}
            self._records = []
            self._seen = set()
            return

        self.meta = payload.get("meta", {})
        txs_raw = payload.get("transactions", [])

        records: List[TransactionRecord] = []
        seen: Set[Tuple[int, int, int, str, str, Direction]] = set()
        for item in txs_raw:
            try:
                tx = TransactionRecord.from_dict(item)
                key = _tx_key(tx)
                if key not in seen:
                    records.append(tx)
                    seen.add(key)
            except Exception:
                continue

        records.sort(key=lambda tx: (tx.block, tx.amount_rao, tx.src, tx.dest))
        self._records = records
        self._seen = seen

        # Merge treasury set with whatever we were initialised with.
        cached_treasuries = set(self.meta.get("treasuries", []))
        if cached_treasuries:
            self.treasury_set.update(cached_treasuries)
        if not self.meta:
            self.meta = {}

        self.meta.setdefault("network", self.network)
        self.meta.setdefault("treasuries", sorted(self.treasury_set))
        self.meta.setdefault("lock_period_days", LOCK_PERIOD_DAYS)
        self.meta.setdefault("blocks_per_day", BLOCKS_PER_DAY)

        # Normalise meta fields in case they were missing.
        if "last_block" not in self.meta and self._records:
            self.meta["last_block"] = self._records[-1].block

    def _flush(self) -> None:
        data = {
            "meta": self.meta,
            "transactions": [tx.to_dict() for tx in self._records],
        }
        tmp_path = self.path.with_suffix(".tmp")
        with tmp_path.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        tmp_path.replace(self.path)

    # ------------------------------------------------------------------ #
    # Accessors
    # ------------------------------------------------------------------ #

    @property
    def records(self) -> List[TransactionRecord]:
        return list(self._records)

    def last_block(self) -> Optional[int]:
        if self._records:
            return self._records[-1].block
        return self.meta.get("last_block")

    # ------------------------------------------------------------------ #
    # Update logic
    # ------------------------------------------------------------------ #

    async def update(
        self,
        subtensor: bt.AsyncSubtensor,
        *,
        start_block: int,
        end_block: int,
        allow_rescan: bool = False,
        chunk_size: int = 600,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        progress_label: Optional[str] = None,
    ) -> List[TransactionRecord]:
        """
        Scan the chain for new transfers touching the treasury.

        Args:
            subtensor: Initialised AsyncSubtensor client (archive endpoint).
            start_block: Inclusive starting block to scan.
            end_block: Inclusive end block to scan.
            allow_rescan: When False (default) the method will never scan below
                the highest cached block.
            chunk_size: Number of blocks per scan batch. Smaller chunks reduce
                timeout risk on archive nodes (default: 600).

        Returns:
            List of TransactionRecord objects that were appended during the call.
        """
        if start_block > end_block:
            return []

        cached_last = self.last_block()
        if cached_last is not None and not allow_rescan:
            start_block = max(start_block, cached_last + 1)

        if start_block > end_block:
            return []

        chunk_size = max(1, int(chunk_size))
        chunks: List[Tuple[int, int]] = []
        for chunk_start in range(start_block, end_block + 1, chunk_size):
            chunk_end = min(end_block, chunk_start + chunk_size - 1)
            chunks.append((chunk_start, chunk_end))

        total_chunks = len(chunks)
        chunk_counter = 0
        scanner = AlphaTransfersScanner(
            subtensor=subtensor,
            dest_coldkey=None,  # filter manually for both inbound/outbound
            allow_batch=True,
            rpc_lock=asyncio.Lock(),
        )

        bt.logging.info(
            f"[treasury-cache] Scanning blocks {start_block} → {end_block} "
            f"for {len(self.treasury_set)} treasury coldkeys."
        )
        new_records: List[TransactionRecord] = []

        progress_bar = None
        if tqdm is not None and total_chunks > 1:
            progress_bar = tqdm(total=total_chunks, desc=progress_label or "Scanning transfers", leave=False)

        for chunk_start, chunk_end in chunks:
            try:
                events = await scanner.scan(chunk_start, chunk_end)
            except Exception as exc:  # pragma: no cover - scanner handles logging
                bt.logging.error(
                    f"[treasury-cache] Scanner error for blocks {chunk_start}-{chunk_end}: {exc}"
                )
                if progress_bar:
                    progress_bar.update(1)
                continue

            chunk_counter += 1
            if progress_callback:
                try:
                    progress_callback(chunk_counter, total_chunks)
                except Exception:
                    pass
            if progress_bar:
                progress_bar.update(1)

            if not events:
                continue

            bt.logging.debug(
                f"[treasury-cache] Chunk {chunk_start}-{chunk_end} yielded {len(events)} events."
            )

            for ev in events:
                if ev.dest_coldkey not in self.treasury_set and ev.src_coldkey not in self.treasury_set:
                    continue

                if ev.dest_coldkey in self.treasury_set and ev.src_coldkey in self.treasury_set:
                    direction: Direction = "internal"
                elif ev.dest_coldkey in self.treasury_set:
                    direction = "in"
                else:
                    direction = "out"

                tx = TransactionRecord(
                    block=int(ev.block),
                    subnet_id=int(ev.subnet_id),
                    amount_rao=int(ev.amount_rao),
                    src=str(ev.src_coldkey),
                    dest=str(ev.dest_coldkey),
                    direction=direction,
                    extrinsic_idx=None,
                )

                key = _tx_key(tx)
                if key in self._seen:
                    continue

                self._records.append(tx)
                self._seen.add(key)
                new_records.append(tx)

        if new_records:
            self._records.sort(key=lambda tx: (tx.block, tx.amount_rao, tx.src, tx.dest))
            self.meta["last_block"] = self._records[-1].block
            self.meta["last_updated"] = datetime.now(tz=timezone.utc).isoformat()
            self.meta["treasuries"] = sorted(self.treasury_set)
            self._flush()

        if progress_bar:
            progress_bar.close()

        return new_records
