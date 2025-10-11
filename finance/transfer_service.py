# metahash/finance/transfer_service.py
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import bittensor as bt
from tqdm import tqdm
from metahash.utils.pretty_logs import pretty
from metahash.validator.alpha_transfers import AlphaTransfersScanner, TransferEvent

from .cache_layer import CacheLayer, CacheEntry


@dataclass
class TransferSummary:
    """Summary of transfers for a specific block range."""
    start_block: int
    end_block: int
    total_transfers: int
    total_alpha: float
    transfers_by_coldkey: Dict[str, float]  # coldkey -> total_alpha
    transfers_by_treasury: Dict[str, float]  # treasury -> total_alpha
    transfer_events: List[Dict[str, Any]]  # Raw transfer events
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for caching."""
        return {
            'start_block': self.start_block,
            'end_block': self.end_block,
            'total_transfers': self.total_transfers,
            'total_alpha': self.total_alpha,
            'transfers_by_coldkey': self.transfers_by_coldkey,
            'transfers_by_treasury': self.transfers_by_treasury,
            'transfer_events': self.transfer_events
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'TransferSummary':
        """Create from dictionary."""
        return cls(
            start_block=data['start_block'],
            end_block=data['end_block'],
            total_transfers=data['total_transfers'],
            total_alpha=data['total_alpha'],
            transfers_by_coldkey=data['transfers_by_coldkey'],
            transfers_by_treasury=data['transfers_by_treasury'],
            transfer_events=data['transfer_events']
        )


class TransferService:
    """
    Service for scanning and caching transfer events.
    
    This service uses the existing AlphaTransfersScanner to scan for transfer events
    and caches the results for efficient retrieval.
    """
    
    def __init__(self, cache_dir: Path, subtensor: bt.Subtensor, tolerance_blocks: int = 360):
        self.cache = CacheLayer[TransferSummary](
            cache_dir=cache_dir,
            cache_name="transfers",
            tolerance_blocks=tolerance_blocks
        )
        self.subtensor = subtensor
        self._rpc_lock = asyncio.Lock()
        self._scanner: Optional[AlphaTransfersScanner] = None
    
    async def get_transfers(self, block_number: int, force_rescan: bool = False) -> Optional[TransferSummary]:
        """
        Get transfer summary for a specific block number.
        
        Args:
            block_number: Block number to get transfers for
            force_rescan: Force rescanning even if cached
            
        Returns:
            TransferSummary if found/scanned, None otherwise
        """
        bt.logging.info(f"🔍 Getting transfer summary for block {block_number}")
        
        # Check cache first
        if not force_rescan:
            cached_data = self.cache.get(block_number)
            if cached_data:
                bt.logging.info(f"✅ Transfer summary found in cache for block {block_number}")
                return cached_data
        
        # Scan for transfers around this block
        # Use a small range around the target block for scanning
        scan_range = 10  # Scan 10 blocks around the target
        start_block = max(1, block_number - scan_range)
        end_block = block_number + scan_range
        
        bt.logging.info(f"🔄 Scanning transfers from block {start_block} to {end_block}")
        transfer_summary = await self._scan_transfers(start_block, end_block, target_block=block_number)
        if transfer_summary:
            self.cache.put(block_number, transfer_summary)
            bt.logging.info(f"💾 Transfer summary cached for block {block_number}")
        else:
            bt.logging.warning(f"❌ Failed to scan transfers for block {block_number}")
        
        return transfer_summary
    
    async def _scan_transfers(self, start_block: int, end_block: int, target_block: int) -> Optional[TransferSummary]:
        """
        Scan for transfer events in a block range.
        
        Args:
            start_block: Start block for scanning
            end_block: End block for scanning
            target_block: Target block for caching
            
        Returns:
            TransferSummary if successful, None otherwise
        """
        try:
            async with self._rpc_lock:
                bt.logging.info(f"🔗 Acquiring RPC lock for transfer scanning...")
                
                # Initialize scanner if needed
                if self._scanner is None:
                    bt.logging.info(f"🔧 Initializing AlphaTransfersScanner...")
                    self._scanner = AlphaTransfersScanner(
                        subtensor=self.subtensor,
                        dest_coldkey=None,  # Scan all transfers
                        allow_batch=True,
                        rpc_lock=self._rpc_lock
                    )
                
                # Scan for transfer events
                bt.logging.info(f"🔍 Scanning transfers from block {start_block} to {end_block}")
                events = await self._scanner.scan(start_block, end_block)
                
                if events is None:
                    bt.logging.warning(f"⚠️ No transfer events found in range {start_block}-{end_block}")
                    return None
                
                bt.logging.info(f"📊 Processing {len(events)} transfer events...")
                
                # Process transfer events
                total_alpha = 0.0
                transfers_by_coldkey = {}
                transfers_by_treasury = {}
                transfer_events = []
                
                if events:
                    with tqdm(total=len(events), desc="Processing transfers", unit="transfer") as pbar:
                        for event in events:
                            if not isinstance(event, TransferEvent):
                                pbar.update(1)
                                continue
                            
                            # Extract event data
                            alpha_amount = getattr(event, 'amount', 0.0)
                            src_coldkey = getattr(event, 'src_coldkey', 'unknown')
                            dest_coldkey = getattr(event, 'dest_coldkey', 'unknown')
                            
                            total_alpha += alpha_amount
                            
                            # Track by source coldkey
                            if src_coldkey not in transfers_by_coldkey:
                                transfers_by_coldkey[src_coldkey] = 0.0
                            transfers_by_coldkey[src_coldkey] += alpha_amount
                            
                            # Track by destination treasury
                            if dest_coldkey not in transfers_by_treasury:
                                transfers_by_treasury[dest_coldkey] = 0.0
                            transfers_by_treasury[dest_coldkey] += alpha_amount
                            
                            # Store raw event data
                            event_data = {
                                'block_number': getattr(event, 'block_number', 0),
                                'extrinsic_idx': getattr(event, 'extrinsic_idx', 0),
                                'src_coldkey': src_coldkey,
                                'dest_coldkey': dest_coldkey,
                                'amount': alpha_amount,
                                'timestamp': getattr(event, 'timestamp', ''),
                                'subnet_id': getattr(event, 'subnet_id', 0)
                            }
                            transfer_events.append(event_data)
                            
                            pbar.update(1)
                            pbar.set_postfix({
                                'alpha': f"{total_alpha:.2f}",
                                'transfers': len(transfer_events),
                                'coldkeys': len(transfers_by_coldkey)
                            })
                else:
                    bt.logging.info("⚠️ No transfer events to process")
                
                transfer_summary = TransferSummary(
                    start_block=start_block,
                    end_block=end_block,
                    total_transfers=len(transfer_events),
                    total_alpha=total_alpha,
                    transfers_by_coldkey=transfers_by_coldkey,
                    transfers_by_treasury=transfers_by_treasury,
                    transfer_events=transfer_events
                )
                
                bt.logging.info(f"✅ Scanned {len(transfer_events)} transfers totaling {total_alpha:.6f} alpha across {len(transfers_by_coldkey)} coldkeys")
                return transfer_summary
                
        except Exception as e:
            bt.logging.error(f"❌ Failed to scan transfers for blocks {start_block}-{end_block}: {e}")
            return None
    
    async def get_transfers_range(self, start_block: int, end_block: int) -> List[TransferSummary]:
        """
        Get transfer summaries for a range of blocks.
        
        Args:
            start_block: Start block number
            end_block: End block number
            
        Returns:
            List of transfer summaries in the range
        """
        cached_entries = self.cache.get_range(start_block, end_block)
        transfer_summaries = []
        
        # Convert cached entries to transfer summaries
        for entry in cached_entries:
            if isinstance(entry.data, dict):
                transfer_summary = TransferSummary.from_dict(entry.data)
            else:
                transfer_summary = entry.data
            transfer_summaries.append(transfer_summary)
        
        # Fill in missing blocks
        for block_num in range(start_block, end_block + 1):
            if not any(ts.start_block <= block_num <= ts.end_block for ts in transfer_summaries):
                transfer_summary = await self.get_transfers(block_num)
                if transfer_summary:
                    transfer_summaries.append(transfer_summary)
        
        # Sort by start block
        transfer_summaries.sort(key=lambda x: x.start_block)
        return transfer_summaries
    
    async def scan_block_range(self, start_block: int, end_block: int, step: int = 360) -> List[TransferSummary]:
        """
        Scan transfers for a range of blocks with specified step size.
        
        Args:
            start_block: Start block number
            end_block: End block number
            step: Step size between scans
            
        Returns:
            List of transfer summaries
        """
        transfer_summaries = []
        
        for block_num in range(start_block, end_block + 1, step):
            bt.logging.info(f"Scanning transfers for block {block_num}")
            transfer_summary = await self.get_transfers(block_num)
            if transfer_summary:
                transfer_summaries.append(transfer_summary)
        
        return transfer_summaries
    
    def get_cached_transfers(self, block_number: int) -> Optional[TransferSummary]:
        """Get cached transfer summary without scanning."""
        cached_data = self.cache.get(block_number)
        if cached_data:
            if isinstance(cached_data, dict):
                return TransferSummary.from_dict(cached_data)
            elif isinstance(cached_data, TransferSummary):
                return cached_data
        return None
    
    def get_cache_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        return self.cache.get_stats()
    
    def clear_cache(self) -> None:
        """Clear all cached transfer summaries."""
        self.cache.clear()
