# metahash/finance/portfolio_service.py
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import bittensor as bt
from tqdm import tqdm
from metahash.utils.pretty_logs import pretty
from metahash.config import PLANCK

from .cache_layer import CacheLayer, CacheEntry


@dataclass
class PortfolioState:
    """Portfolio state at a specific block."""
    block_number: int
    timestamp: str
    total_tao: float
    total_alpha: float
    subnet_allocations: Dict[int, Dict[str, float]]  # subnet_id -> {tao, alpha, percentage}
    miner_balances: Dict[str, Dict[str, float]]  # coldkey -> {tao, alpha}
    treasury_balances: Dict[str, Dict[str, float]]  # treasury_coldkey -> {tao, alpha}
    total_budget: float
    used_budget: float
    available_budget: float
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for caching."""
        return {
            'block_number': self.block_number,
            'timestamp': self.timestamp,
            'total_tao': self.total_tao,
            'total_alpha': self.total_alpha,
            'subnet_allocations': self.subnet_allocations,
            'miner_balances': self.miner_balances,
            'treasury_balances': self.treasury_balances,
            'total_budget': self.total_budget,
            'used_budget': self.used_budget,
            'available_budget': self.available_budget
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'PortfolioState':
        """Create from dictionary."""
        return cls(
            block_number=data['block_number'],
            timestamp=data['timestamp'],
            total_tao=data['total_tao'],
            total_alpha=data['total_alpha'],
            subnet_allocations=data['subnet_allocations'],
            miner_balances=data['miner_balances'],
            treasury_balances=data['treasury_balances'],
            total_budget=data['total_budget'],
            used_budget=data['used_budget'],
            available_budget=data['available_budget']
        )


class PortfolioService:
    """
    Service for calculating and caching portfolio states.
    
    This service calculates portfolio states by analyzing:
    - Miner balances and allocations
    - Treasury balances
    - Subnet allocations
    - Budget utilization
    """
    
    def __init__(self, cache_dir: Path, subtensor: bt.Subtensor, tolerance_blocks: int = 360):
        self.cache = CacheLayer[PortfolioState](
            cache_dir=cache_dir,
            cache_name="portfolio",
            tolerance_blocks=tolerance_blocks
        )
        self.subtensor = subtensor
        self._rpc_lock = asyncio.Lock()
    
    async def get_portfolio_state(self, block_number: int, force_recalculate: bool = False) -> Optional[PortfolioState]:
        """
        Get portfolio state for a specific block number.
        
        Args:
            block_number: Block number to get portfolio state for
            force_recalculate: Force recalculation even if cached
            
        Returns:
            PortfolioState if found/calculated, None otherwise
        """
        bt.logging.info(f"🔍 Getting portfolio state for block {block_number}")
        
        # Check cache first
        if not force_recalculate:
            cached_data = self.cache.get(block_number)
            if cached_data:
                bt.logging.info(f"✅ Portfolio state found in cache for block {block_number}")
                return cached_data
        
        bt.logging.info(f"🔄 Calculating new portfolio state for block {block_number}")
        # Calculate new portfolio state
        portfolio_state = await self._calculate_portfolio_state(block_number)
        if portfolio_state:
            self.cache.put(block_number, portfolio_state)
            bt.logging.info(f"💾 Portfolio state cached for block {block_number}")
        else:
            bt.logging.warning(f"❌ Failed to calculate portfolio state for block {block_number}")
        
        return portfolio_state
    
    async def _calculate_portfolio_state(self, block_number: int) -> Optional[PortfolioState]:
        """
        Calculate portfolio state for a specific block.
        
        This is a simplified implementation that would need to be enhanced
        based on the actual portfolio calculation logic from the settlement engine.
        """
        try:
            async with self._rpc_lock:
                bt.logging.info(f"🔗 Acquiring RPC lock for portfolio calculation...")
                
                # Get current metagraph state
                bt.logging.info(f"📊 Fetching metagraph state...")
                metagraph = self.subtensor.metagraph(netuid=1)
                
                # Get current block info
                bt.logging.info(f"🔢 Getting current block info...")
                current_block = self.subtensor.get_current_block()
                
                # Calculate basic portfolio metrics
                total_tao = 0.0
                total_alpha = 0.0
                subnet_allocations = {}
                miner_balances = {}
                treasury_balances = {}
                
                # Get miner balances (simplified - would need actual balance queries)
                serving_axons = [axon for axon in metagraph.axons if axon.is_serving]
                bt.logging.info(f"💰 Processing {len(serving_axons)} serving miners out of {len(metagraph.axons)} total axons...")
                
                if serving_axons:
                    with tqdm(total=len(serving_axons), desc="Processing miners", unit="miner") as pbar:
                        for uid, axon in enumerate(metagraph.axons):
                            if axon.is_serving:
                                coldkey = axon.coldkey
                                # This would need actual balance queries from the blockchain
                                # For now, using placeholder values
                                tao_balance = 0.0  # Would query actual TAO balance
                                alpha_balance = 0.0  # Would query actual alpha balance
                                
                                miner_balances[coldkey] = {
                                    'tao': tao_balance,
                                    'alpha': alpha_balance
                                }
                                
                                total_tao += tao_balance
                                total_alpha += alpha_balance
                                
                                pbar.update(1)
                                pbar.set_postfix({
                                    'tao': f"{total_tao:.2f}",
                                    'alpha': f"{total_alpha:.2f}",
                                    'miners': len(miner_balances)
                                })
                else:
                    bt.logging.info("⚠️ No serving miners found in metagraph")
                
                bt.logging.info(f"📈 Calculating subnet allocations...")
                # Calculate subnet allocations (simplified)
                # This would need to be calculated based on actual subnet participation
                subnet_allocations = {
                    1: {  # Example subnet
                        'tao': total_tao * 0.5,
                        'alpha': total_alpha * 0.5,
                        'percentage': 50.0
                    }
                }
                
                bt.logging.info(f"💼 Calculating budget metrics...")
                # Calculate budget metrics (simplified)
                total_budget = total_tao * 0.1  # 10% of total TAO as budget
                used_budget = total_budget * 0.3  # 30% used
                available_budget = total_budget - used_budget
                
                from datetime import datetime
                portfolio_state = PortfolioState(
                    block_number=block_number,
                    timestamp=datetime.now().isoformat(),
                    total_tao=total_tao,
                    total_alpha=total_alpha,
                    subnet_allocations=subnet_allocations,
                    miner_balances=miner_balances,
                    treasury_balances=treasury_balances,
                    total_budget=total_budget,
                    used_budget=used_budget,
                    available_budget=available_budget
                )
                
                bt.logging.info(f"✅ Portfolio state calculated for block {block_number}: TAO={total_tao:.6f}, Alpha={total_alpha:.6f}, Miners={len(miner_balances)}")
                return portfolio_state
                
        except Exception as e:
            bt.logging.error(f"❌ Failed to calculate portfolio state for block {block_number}: {e}")
            return None
    
    async def get_portfolio_range(self, start_block: int, end_block: int) -> List[PortfolioState]:
        """
        Get portfolio states for a range of blocks.
        
        Args:
            start_block: Start block number
            end_block: End block number
            
        Returns:
            List of portfolio states in the range
        """
        cached_entries = self.cache.get_range(start_block, end_block)
        portfolio_states = []
        
        # Convert cached entries to portfolio states
        for entry in cached_entries:
            if isinstance(entry.data, dict):
                portfolio_state = PortfolioState.from_dict(entry.data)
            else:
                portfolio_state = entry.data
            portfolio_states.append(portfolio_state)
        
        # Fill in missing blocks
        for block_num in range(start_block, end_block + 1):
            if not any(ps.block_number == block_num for ps in portfolio_states):
                portfolio_state = await self.get_portfolio_state(block_num)
                if portfolio_state:
                    portfolio_states.append(portfolio_state)
        
        # Sort by block number
        portfolio_states.sort(key=lambda x: x.block_number)
        return portfolio_states
    
    def get_cached_portfolio(self, block_number: int) -> Optional[PortfolioState]:
        """Get cached portfolio state without calculation."""
        cached_data = self.cache.get(block_number)
        if cached_data:
            if isinstance(cached_data, dict):
                return PortfolioState.from_dict(cached_data)
            elif isinstance(cached_data, PortfolioState):
                return cached_data
        return None
    
    def get_cache_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        return self.cache.get_stats()
    
    def clear_cache(self) -> None:
        """Clear all cached portfolio states."""
        self.cache.clear()
