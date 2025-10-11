# metahash/finance/cache_layer.py
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, TypeVar, Generic
from datetime import datetime

import bittensor as bt
from metahash.utils.pretty_logs import pretty

T = TypeVar('T')


@dataclass
class CacheEntry(Generic[T]):
    """Generic cache entry with block number and timestamp."""
    block_number: int
    timestamp: datetime
    data: T
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        # Handle data serialization based on type
        if hasattr(self.data, 'to_dict'):
            serialized_data = self.data.to_dict()
        else:
            serialized_data = self.data
            
        return {
            'block_number': self.block_number,
            'timestamp': self.timestamp.isoformat(),
            'data': serialized_data
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'CacheEntry[T]':
        """Create from dictionary."""
        return cls(
            block_number=data['block_number'],
            timestamp=datetime.fromisoformat(data['timestamp']),
            data=data['data']  # Data will be deserialized by the service layer
        )


class CacheLayer(Generic[T]):
    """
    Base cache layer for storing block-based data with efficient retrieval.
    
    Features:
    - Thread-safe operations
    - Block-based caching with tolerance range
    - Persistent storage to disk
    - Efficient range queries
    """
    
    def __init__(self, cache_dir: Path, cache_name: str, tolerance_blocks: int = 360):
        self.cache_dir = Path(cache_dir)
        self.cache_name = cache_name
        self.tolerance_blocks = tolerance_blocks
        self.cache_file = self.cache_dir / f"{cache_name}_cache.json"
        
        # Thread safety
        self._lock = threading.Lock()
        
        # In-memory cache: block_number -> CacheEntry
        self._cache: Dict[int, CacheEntry[T]] = {}
        
        # Ensure cache directory exists
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        
        # Load existing cache
        self._load_cache()
    
    def _load_cache(self) -> None:
        """Load cache from disk."""
        if not self.cache_file.exists():
            return
        
        try:
            with open(self.cache_file, 'r') as f:
                data = json.load(f)
            
            with self._lock:
                self._cache.clear()
                for block_num_str, entry_data in data.items():
                    block_num = int(block_num_str)
                    entry = CacheEntry.from_dict(entry_data)
                    self._cache[block_num] = entry
                    
            bt.logging.info(f"Loaded {len(self._cache)} entries from {self.cache_name} cache")
            
        except Exception as e:
            bt.logging.error(f"Failed to load {self.cache_name} cache: {e}")
            self._cache.clear()
    
    def _save_cache(self) -> None:
        """Save cache to disk."""
        try:
            with self._lock:
                cache_data = {}
                for block_num, entry in self._cache.items():
                    # Always use the entry's to_dict method which handles the data serialization
                    cache_data[str(block_num)] = entry.to_dict()
            
            # Atomic write
            temp_file = self.cache_file.with_suffix('.tmp')
            with open(temp_file, 'w') as f:
                json.dump(cache_data, f, indent=2)
            
            temp_file.replace(self.cache_file)
            
        except Exception as e:
            bt.logging.error(f"Failed to save {self.cache_name} cache: {e}")
    
    def get(self, block_number: int) -> Optional[T]:
        """
        Get cached data for a block number within tolerance range.
        
        Args:
            block_number: Target block number
            
        Returns:
            Cached data if found within tolerance, None otherwise
        """
        with self._lock:
            # Check exact match first
            if block_number in self._cache:
                return self._cache[block_number].data
            
            # Check within tolerance range
            for cached_block, entry in self._cache.items():
                if abs(cached_block - block_number) <= self.tolerance_blocks:
                    bt.logging.debug(f"Cache hit for block {block_number} using cached block {cached_block}")
                    return entry.data
            
            return None
    
    def put(self, block_number: int, data: T) -> None:
        """
        Store data in cache for a specific block number.
        
        Args:
            block_number: Block number to cache
            data: Data to cache
        """
        entry = CacheEntry(
            block_number=block_number,
            timestamp=datetime.now(),
            data=data
        )
        
        with self._lock:
            self._cache[block_number] = entry
        
        # Save to disk
        self._save_cache()
        
        bt.logging.debug(f"Cached {self.cache_name} data for block {block_number}")
    
    def get_range(self, start_block: int, end_block: int) -> List[CacheEntry[T]]:
        """
        Get all cached entries within a block range.
        
        Args:
            start_block: Start block number (inclusive)
            end_block: End block number (inclusive)
            
        Returns:
            List of cache entries in the range
        """
        with self._lock:
            entries = []
            for block_num, entry in self._cache.items():
                if start_block <= block_num <= end_block:
                    entries.append(entry)
            
            # Sort by block number
            entries.sort(key=lambda x: x.block_number)
            return entries
    
    def get_latest(self) -> Optional[CacheEntry[T]]:
        """Get the latest cached entry."""
        with self._lock:
            if not self._cache:
                return None
            
            latest_block = max(self._cache.keys())
            return self._cache[latest_block]
    
    def clear(self) -> None:
        """Clear all cached data."""
        with self._lock:
            self._cache.clear()
        
        # Remove cache file
        if self.cache_file.exists():
            self.cache_file.unlink()
        
        bt.logging.info(f"Cleared {self.cache_name} cache")
    
    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        with self._lock:
            if not self._cache:
                return {
                    'total_entries': 0,
                    'block_range': None,
                    'latest_block': None,
                    'oldest_block': None
                }
            
            blocks = list(self._cache.keys())
            return {
                'total_entries': len(self._cache),
                'block_range': (min(blocks), max(blocks)),
                'latest_block': max(blocks),
                'oldest_block': min(blocks),
                'tolerance_blocks': self.tolerance_blocks
            }
