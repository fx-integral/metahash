#!/usr/bin/env python3
# metahash/finance/example_usage.py
"""
Example usage of the portfolio and transfer caching services.

This script demonstrates how to use the PortfolioService and TransferService
to cache and retrieve portfolio states and transfer data.
"""

import asyncio
from pathlib import Path

import bittensor as bt
from metahash.utils.pretty_logs import pretty

from .portfolio_service import PortfolioService
from .transfer_service import TransferService


async def example_usage():
    """Example usage of the caching services."""
    
    # Initialize bittensor
    config = bt.config()
    config.network = "finney"
    config.netuid = 1
    
    subtensor = bt.subtensor(network=config.network)
    
    # Create cache directory
    cache_dir = Path("./cache")
    cache_dir.mkdir(parents=True, exist_ok=True)
    
    # Initialize services
    portfolio_service = PortfolioService(cache_dir, subtensor, tolerance_blocks=360)
    transfer_service = TransferService(cache_dir, subtensor, tolerance_blocks=360)
    
    pretty.rule("[bold cyan]PORTFOLIO SERVICE EXAMPLE[/bold cyan]")
    
    # Example 1: Get portfolio state for a specific block
    target_block = 6633585
    portfolio_state = await portfolio_service.get_portfolio_state(target_block)
    
    if portfolio_state:
        pretty.log(f"Portfolio state for block {target_block}:")
        pretty.log(f"  Total TAO: {portfolio_state.total_tao:.6f}")
        pretty.log(f"  Total Alpha: {portfolio_state.total_alpha:.6f}")
        pretty.log(f"  Total Budget: {portfolio_state.total_budget:.6f}")
        pretty.log(f"  Available Budget: {portfolio_state.available_budget:.6f}")
    else:
        pretty.log(f"No portfolio state found for block {target_block}")
    
    # Example 2: Get cached portfolio state (should be fast)
    cached_portfolio = portfolio_service.get_cached_portfolio(target_block)
    if cached_portfolio:
        pretty.log(f"Cached portfolio state retrieved for block {target_block}")
    
    # Example 3: Get portfolio states for a range
    start_block = 6633585 - 7200
    end_block = 6633585
    portfolio_states = await portfolio_service.get_portfolio_range(start_block, end_block)
    pretty.log(f"Retrieved {len(portfolio_states)} portfolio states for range {start_block}-{end_block}")
    
    pretty.rule("[bold cyan]TRANSFER SERVICE EXAMPLE[/bold cyan]")
    
    # Example 4: Get transfers for a specific block
    transfer_summary = await transfer_service.get_transfers(target_block)
    
    if transfer_summary:
        pretty.log(f"Transfer summary for block {target_block}:")
        pretty.log(f"  Total transfers: {transfer_summary.total_transfers}")
        pretty.log(f"  Total alpha: {transfer_summary.total_alpha:.6f}")
        pretty.log(f"  Unique coldkeys: {len(transfer_summary.transfers_by_coldkey)}")
        pretty.log(f"  Unique treasuries: {len(transfer_summary.transfers_by_treasury)}")
    else:
        pretty.log(f"No transfer summary found for block {target_block}")
    
    # Example 5: Get cached transfer summary
    cached_transfers = transfer_service.get_cached_transfers(target_block)
    if cached_transfers:
        pretty.log(f"Cached transfer summary retrieved for block {target_block}")
    
    # Example 6: Scan transfers for a range with step size
    transfer_summaries = await transfer_service.scan_block_range(
        start_block, end_block, step=360
    )
    pretty.log(f"Scanned {len(transfer_summaries)} transfer summaries for range {start_block}-{end_block}")
    
    pretty.rule("[bold cyan]CACHE STATISTICS[/bold cyan]")
    
    # Example 7: Get cache statistics
    portfolio_stats = portfolio_service.get_cache_stats()
    transfer_stats = transfer_service.get_cache_stats()
    
    pretty.log("Portfolio cache stats:")
    pretty.log(f"  Total entries: {portfolio_stats.get('total_entries', 0)}")
    pretty.log(f"  Block range: {portfolio_stats.get('block_range', 'N/A')}")
    pretty.log(f"  Latest block: {portfolio_stats.get('latest_block', 'N/A')}")
    
    pretty.log("Transfer cache stats:")
    pretty.log(f"  Total entries: {transfer_stats.get('total_entries', 0)}")
    pretty.log(f"  Block range: {transfer_stats.get('block_range', 'N/A')}")
    pretty.log(f"  Latest block: {transfer_stats.get('latest_block', 'N/A')}")


if __name__ == "__main__":
    asyncio.run(example_usage())
