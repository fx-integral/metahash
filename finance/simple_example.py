#!/usr/bin/env python3
# metahash/finance/simple_example.py
"""
Simple example demonstrating the portfolio and transfer caching services.
"""

import asyncio
import sys
from pathlib import Path

# Add the parent directory to the path
sys.path.insert(0, str(Path(__file__).parent.parent))

import bittensor as bt
from finance.portfolio_service import PortfolioService
from finance.transfer_service import TransferService
from metahash.utils.pretty_logs import pretty


async def main():
    """Simple example of using the caching services."""
    
    # Initialize bittensor with archive network for old blocks
    config = bt.config()
    config.network = "archive"
    config.netuid = 1
    
    subtensor = bt.subtensor(network=config.network)
    
    # Create cache directory
    cache_dir = Path("./cache")
    cache_dir.mkdir(parents=True, exist_ok=True)
    
    # Initialize services
    portfolio_service = PortfolioService(cache_dir, subtensor, tolerance_blocks=360)
    transfer_service = TransferService(cache_dir, subtensor, tolerance_blocks=360)
    
    pretty.rule("[bold cyan]PORTFOLIO AND TRANSFER CACHING EXAMPLE[/bold cyan]")
    
    # Example: Process a few blocks from the specified range
    start_block = 6626385
    end_block = 6627105  # Just a few blocks for demo
    step = 360
    
    pretty.log(f"Processing blocks from {start_block} to {end_block} with step {step}")
    
    for block_num in range(start_block, end_block + 1, step):
        try:
            pretty.log(f"Processing block {block_num}")
            
            # Get portfolio state
            portfolio_state = await portfolio_service.get_portfolio_state(block_num)
            if portfolio_state:
                pretty.log(f"  Portfolio: TAO={portfolio_state.total_tao:.6f}, Alpha={portfolio_state.total_alpha:.6f}")
            else:
                pretty.log(f"  Portfolio: No data available")
            
            # Get transfers
            transfer_summary = await transfer_service.get_transfers(block_num)
            if transfer_summary:
                pretty.log(f"  Transfers: {transfer_summary.total_transfers} transfers, {transfer_summary.total_alpha:.6f} alpha")
            else:
                pretty.log(f"  Transfers: No data available")
                
        except Exception as e:
            pretty.log(f"  Error processing block {block_num}: {e}")
    
    # Show cache statistics
    pretty.rule("[bold green]CACHE STATISTICS[/bold green]")
    
    portfolio_stats = portfolio_service.get_cache_stats()
    transfer_stats = transfer_service.get_cache_stats()
    
    pretty.log(f"Portfolio cache: {portfolio_stats.get('total_entries', 0)} entries")
    pretty.log(f"Transfer cache: {transfer_stats.get('total_entries', 0)} entries")
    
    # Test cache retrieval
    pretty.rule("[bold blue]CACHE RETRIEVAL TEST[/bold blue]")
    
    test_block = start_block
    cached_portfolio = portfolio_service.get_cached_portfolio(test_block)
    cached_transfers = transfer_service.get_cached_transfers(test_block)
    
    if cached_portfolio:
        pretty.log(f"Cached portfolio for block {test_block}: TAO={cached_portfolio.total_tao:.6f}")
    else:
        pretty.log(f"No cached portfolio for block {test_block}")
    
    if cached_transfers:
        pretty.log(f"Cached transfers for block {test_block}: {cached_transfers.total_transfers} transfers")
    else:
        pretty.log(f"No cached transfers for block {test_block}")


if __name__ == "__main__":
    asyncio.run(main())
