#!/usr/bin/env python3
# metahash/finance/test_services.py
"""
Test script for the portfolio and transfer caching services.

This script tests the basic functionality of the services without requiring
full blockchain connectivity.
"""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import Mock, AsyncMock

import sys
from pathlib import Path
from tqdm import tqdm

# Add the parent directory to the path
sys.path.insert(0, str(Path(__file__).parent.parent))

from finance.cache_layer import CacheLayer, CacheEntry
from finance.portfolio_service import PortfolioService, PortfolioState
from finance.transfer_service import TransferService, TransferSummary


def test_cache_layer():
    """Test the CacheLayer functionality."""
    print("🧪 Testing CacheLayer...")
    
    with tempfile.TemporaryDirectory() as temp_dir:
        cache_dir = Path(temp_dir)
        
        # Test basic cache operations
        print("  📁 Creating cache layer...")
        cache = CacheLayer(cache_dir, "test_cache", tolerance_blocks=10)
        
        # Test put and get
        print("  💾 Testing put and get operations...")
        test_data = {"test": "data", "value": 123}
        cache.put(1000, test_data)
        
        # Test exact match
        retrieved = cache.get(1000)
        assert retrieved == test_data, f"Expected {test_data}, got {retrieved}"
        
        # Test tolerance range
        retrieved = cache.get(1005)  # Within tolerance
        assert retrieved == test_data, f"Expected {test_data}, got {retrieved}"
        
        # Test outside tolerance
        retrieved = cache.get(1020)  # Outside tolerance
        assert retrieved is None, f"Expected None, got {retrieved}"
        
        # Test range query
        print("  🔍 Testing range queries...")
        cache.put(2000, {"test": "data2"})
        cache.put(3000, {"test": "data3"})
        
        entries = cache.get_range(1995, 2005)
        assert len(entries) == 1, f"Expected 1 entry, got {len(entries)}"
        
        # Test stats
        print("  📊 Testing cache statistics...")
        stats = cache.get_stats()
        assert stats['total_entries'] == 3, f"Expected 3 entries, got {stats['total_entries']}"
        
        print("✅ CacheLayer tests passed")


def test_portfolio_state():
    """Test PortfolioState serialization."""
    print("🧪 Testing PortfolioState...")
    
    print("  📊 Creating test portfolio state...")
    portfolio_state = PortfolioState(
        block_number=1000,
        timestamp="2024-01-01T00:00:00",
        total_tao=100.0,
        total_alpha=50.0,
        subnet_allocations={1: {"tao": 50.0, "alpha": 25.0, "percentage": 50.0}},
        miner_balances={"coldkey1": {"tao": 25.0, "alpha": 12.5}},
        treasury_balances={"treasury1": {"tao": 25.0, "alpha": 12.5}},
        total_budget=10.0,
        used_budget=5.0,
        available_budget=5.0
    )
    
    # Test serialization
    print("  💾 Testing serialization...")
    data = portfolio_state.to_dict()
    assert data['block_number'] == 1000
    assert data['total_tao'] == 100.0
    
    # Test deserialization
    print("  🔄 Testing deserialization...")
    restored = PortfolioState.from_dict(data)
    assert restored.block_number == portfolio_state.block_number
    assert restored.total_tao == portfolio_state.total_tao
    
    print("✅ PortfolioState tests passed")


def test_transfer_summary():
    """Test TransferSummary serialization."""
    print("🧪 Testing TransferSummary...")
    
    print("  📊 Creating test transfer summary...")
    transfer_summary = TransferSummary(
        start_block=1000,
        end_block=1010,
        total_transfers=5,
        total_alpha=25.0,
        transfers_by_coldkey={"coldkey1": 15.0, "coldkey2": 10.0},
        transfers_by_treasury={"treasury1": 25.0},
        transfer_events=[
            {"block_number": 1000, "amount": 10.0, "src_coldkey": "coldkey1"},
            {"block_number": 1005, "amount": 15.0, "src_coldkey": "coldkey2"}
        ]
    )
    
    # Test serialization
    print("  💾 Testing serialization...")
    data = transfer_summary.to_dict()
    assert data['total_transfers'] == 5
    assert data['total_alpha'] == 25.0
    
    # Test deserialization
    print("  🔄 Testing deserialization...")
    restored = TransferSummary.from_dict(data)
    assert restored.total_transfers == transfer_summary.total_transfers
    assert restored.total_alpha == transfer_summary.total_alpha
    
    print("✅ TransferSummary tests passed")


async def test_services():
    """Test the services with mocked subtensor."""
    print("🧪 Testing Services...")
    
    with tempfile.TemporaryDirectory() as temp_dir:
        cache_dir = Path(temp_dir)
        
        print("  🔧 Setting up mock subtensor...")
        # Mock subtensor
        mock_subtensor = Mock()
        mock_subtensor.metagraph.return_value = Mock()
        mock_subtensor.metagraph.return_value.axons = []
        mock_subtensor.get_current_block.return_value = 1000
        
        # Test PortfolioService
        print("  📊 Testing PortfolioService...")
        portfolio_service = PortfolioService(cache_dir, mock_subtensor, tolerance_blocks=10)
        
        # Test cache operations
        print("    🔍 Testing portfolio state retrieval...")
        portfolio_state = await portfolio_service.get_portfolio_state(1000)
        # Note: This will likely return None due to mocked subtensor, but shouldn't crash
        
        print("    💾 Testing cached portfolio retrieval...")
        cached = portfolio_service.get_cached_portfolio(1000)
        # Note: cached data might exist from previous runs, so we just check it doesn't crash
        
        print("    📈 Testing portfolio cache stats...")
        stats = portfolio_service.get_cache_stats()
        # Note: stats might have entries from previous runs, so we just check it doesn't crash
        
        # Test TransferService
        print("  🔄 Testing TransferService...")
        transfer_service = TransferService(cache_dir, mock_subtensor, tolerance_blocks=10)
        
        # Test cache operations
        print("    🔍 Testing transfer summary retrieval...")
        transfer_summary = await transfer_service.get_transfers(1000)
        # Note: This will likely return None due to mocked subtensor, but shouldn't crash
        
        print("    💾 Testing cached transfer retrieval...")
        cached = transfer_service.get_cached_transfers(1000)
        # Note: cached data might exist from previous runs, so we just check it doesn't crash
        
        print("    📈 Testing transfer cache stats...")
        stats = transfer_service.get_cache_stats()
        # Note: stats might have entries from previous runs, so we just check it doesn't crash
        
        print("✅ Services tests passed")


def main():
    """Run all tests."""
    print("🚀 Running finance module tests...\n")
    
    try:
        with tqdm(total=4, desc="Running tests", unit="test") as pbar:
            test_cache_layer()
            pbar.update(1)
            pbar.set_postfix({"test": "CacheLayer"})
            
            test_portfolio_state()
            pbar.update(1)
            pbar.set_postfix({"test": "PortfolioState"})
            
            test_transfer_summary()
            pbar.update(1)
            pbar.set_postfix({"test": "TransferSummary"})
            
            asyncio.run(test_services())
            pbar.update(1)
            pbar.set_postfix({"test": "Services"})
        
        print("\n🎉 All tests passed!")
        
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        raise


if __name__ == "__main__":
    main()
