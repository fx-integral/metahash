#!/usr/bin/env python3
# metahash/finance/process_blocks.py
"""
Script to process portfolio states and transfers for the last 7200 blocks.

This script:
1. Calculates portfolio states every 360 blocks starting from block 6633585 - 7200
2. Scans transfers for the same block intervals
3. Caches results for efficient retrieval
4. Provides summary statistics

Usage:
    python process_blocks.py [--start-block START] [--end-block END] [--step STEP] [--cache-dir CACHE_DIR]
"""

import argparse
import asyncio
import sys
from pathlib import Path
from typing import List, Dict, Any

import bittensor as bt
from tqdm import tqdm
from metahash.utils.pretty_logs import pretty

import sys
from pathlib import Path

# Add the parent directory to the path
sys.path.insert(0, str(Path(__file__).parent.parent))

from finance.portfolio_service import PortfolioService, PortfolioState
from finance.transfer_service import TransferService, TransferSummary


class BlockProcessor:
    """Main processor for handling portfolio and transfer calculations."""
    
    def __init__(self, cache_dir: Path, subtensor: bt.Subtensor, step: int = 360):
        self.cache_dir = Path(cache_dir)
        self.step = step
        self.portfolio_service = PortfolioService(self.cache_dir, subtensor, tolerance_blocks=step)
        self.transfer_service = TransferService(self.cache_dir, subtensor, tolerance_blocks=step)
        
        # Statistics
        self.stats = {
            'blocks_processed': 0,
            'portfolio_states_calculated': 0,
            'portfolio_states_cached': 0,
            'transfers_scanned': 0,
            'transfers_cached': 0,
            'errors': 0
        }
    
    async def process_block_range(self, start_block: int, end_block: int) -> Dict[str, Any]:
        """
        Process a range of blocks for portfolio states and transfers.
        
        Args:
            start_block: Starting block number
            end_block: Ending block number
            
        Returns:
            Dictionary with processing results and statistics
        """
        pretty.rule("[bold cyan]PROCESSING BLOCK RANGE[/bold cyan]")
        pretty.log(f"🚀 Processing blocks from {start_block} to {end_block} with step {self.step}")
        
        portfolio_states = []
        transfer_summaries = []
        
        # Calculate total blocks to process
        total_blocks = len(range(start_block, end_block + 1, self.step))
        
        # Process blocks in steps with progress bar
        with tqdm(total=total_blocks, desc="Processing blocks", unit="block") as pbar:
            for block_num in range(start_block, end_block + 1, self.step):
                try:
                    bt.logging.info(f"🔄 Processing block {block_num}")
                    
                    # Process portfolio state
                    portfolio_state = await self.portfolio_service.get_portfolio_state(block_num)
                    if portfolio_state:
                        portfolio_states.append(portfolio_state)
                        self.stats['portfolio_states_calculated'] += 1
                    else:
                        self.stats['portfolio_states_cached'] += 1
                    
                    # Process transfers
                    transfer_summary = await self.transfer_service.get_transfers(block_num)
                    if transfer_summary:
                        transfer_summaries.append(transfer_summary)
                        self.stats['transfers_scanned'] += 1
                    else:
                        self.stats['transfers_cached'] += 1
                    
                    self.stats['blocks_processed'] += 1
                    
                    # Update progress bar
                    pbar.update(1)
                    pbar.set_postfix({
                        'portfolio': self.stats['portfolio_states_calculated'],
                        'transfers': self.stats['transfers_scanned'],
                        'errors': self.stats['errors']
                    })
                    
                    # Progress update every 5 blocks
                    if self.stats['blocks_processed'] % 5 == 0:
                        self._log_progress()
                    
                except Exception as e:
                    bt.logging.error(f"❌ Error processing block {block_num}: {e}")
                    self.stats['errors'] += 1
                    pbar.update(1)
                    pbar.set_postfix({
                        'portfolio': self.stats['portfolio_states_calculated'],
                        'transfers': self.stats['transfers_scanned'],
                        'errors': self.stats['errors']
                    })
        
        # Final statistics
        self._log_final_stats()
        
        return {
            'portfolio_states': portfolio_states,
            'transfer_summaries': transfer_summaries,
            'stats': self.stats.copy()
        }
    
    def _log_progress(self) -> None:
        """Log current progress."""
        bt.logging.info(
            f"📊 Progress: {self.stats['blocks_processed']} blocks processed, "
            f"{self.stats['portfolio_states_calculated']} portfolio states calculated, "
            f"{self.stats['transfers_scanned']} transfers scanned, "
            f"{self.stats['errors']} errors"
        )
    
    def _log_final_stats(self) -> None:
        """Log final statistics."""
        pretty.rule("[bold green]PROCESSING COMPLETE[/bold green]")
        pretty.log(f"✅ Blocks processed: {self.stats['blocks_processed']}")
        pretty.log(f"📊 Portfolio states calculated: {self.stats['portfolio_states_calculated']}")
        pretty.log(f"💾 Portfolio states from cache: {self.stats['portfolio_states_cached']}")
        pretty.log(f"🔄 Transfers scanned: {self.stats['transfers_scanned']}")
        pretty.log(f"💾 Transfers from cache: {self.stats['transfers_cached']}")
        pretty.log(f"❌ Errors: {self.stats['errors']}")
        
        # Cache statistics
        portfolio_stats = self.portfolio_service.get_cache_stats()
        transfer_stats = self.transfer_service.get_cache_stats()
        
        pretty.log(f"📁 Portfolio cache entries: {portfolio_stats.get('total_entries', 0)}")
        pretty.log(f"📁 Transfer cache entries: {transfer_stats.get('total_entries', 0)}")
    
    async def get_summary_report(self, start_block: int, end_block: int) -> Dict[str, Any]:
        """
        Generate a summary report for the processed blocks.
        
        Args:
            start_block: Starting block number
            end_block: Ending block number
            
        Returns:
            Summary report dictionary
        """
        # Get cached data
        portfolio_states = self.portfolio_service.cache.get_range(start_block, end_block)
        transfer_summaries = self.transfer_service.cache.get_range(start_block, end_block)
        
        # Calculate summary statistics
        total_tao = sum(ps.data.total_tao if hasattr(ps.data, 'total_tao') else 0 for ps in portfolio_states)
        total_alpha = sum(ps.data.total_alpha if hasattr(ps.data, 'total_alpha') else 0 for ps in portfolio_states)
        total_transfers = sum(ts.data.total_transfers if hasattr(ts.data, 'total_transfers') else 0 for ts in transfer_summaries)
        total_transfer_alpha = sum(ts.data.total_alpha if hasattr(ts.data, 'total_alpha') else 0 for ts in transfer_summaries)
        
        return {
            'block_range': (start_block, end_block),
            'step_size': self.step,
            'portfolio_states_count': len(portfolio_states),
            'transfer_summaries_count': len(transfer_summaries),
            'total_tao': total_tao,
            'total_alpha': total_alpha,
            'total_transfers': total_transfers,
            'total_transfer_alpha': total_transfer_alpha,
            'cache_stats': {
                'portfolio': self.portfolio_service.get_cache_stats(),
                'transfers': self.transfer_service.get_cache_stats()
            }
        }


async def main():
    """Main function."""
    parser = argparse.ArgumentParser(description="Process portfolio states and transfers for block ranges")
    parser.add_argument("--start-block", type=int, default=6633585 - 7200, help="Starting block number")
    parser.add_argument("--end-block", type=int, default=6633585, help="Ending block number")
    parser.add_argument("--step", type=int, default=360, help="Step size between blocks")
    parser.add_argument("--cache-dir", type=str, default="./cache", help="Cache directory")
    parser.add_argument("--network", type=str, default="archive", help="Bittensor network")
    parser.add_argument("--netuid", type=int, default=1, help="Network UID")
    parser.add_argument("--summary-only", action="store_true", help="Only generate summary report")
    
    args = parser.parse_args()
    
    # Initialize bittensor
    bt.logging.set_trace(True)
    config = bt.config()
    config.network = args.network
    config.netuid = args.netuid
    
    subtensor = bt.subtensor(network=config.network)
    
    # Create cache directory
    cache_dir = Path(args.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    
    # Initialize processor
    processor = BlockProcessor(cache_dir, subtensor, step=args.step)
    
    try:
        if args.summary_only:
            # Generate summary report only
            pretty.rule("[bold blue]GENERATING SUMMARY REPORT[/bold blue]")
            report = await processor.get_summary_report(args.start_block, args.end_block)
            
            pretty.log(f"Block range: {report['block_range'][0]} - {report['block_range'][1]}")
            pretty.log(f"Step size: {report['step_size']}")
            pretty.log(f"Portfolio states: {report['portfolio_states_count']}")
            pretty.log(f"Transfer summaries: {report['transfer_summaries_count']}")
            pretty.log(f"Total TAO: {report['total_tao']:.6f}")
            pretty.log(f"Total Alpha: {report['total_alpha']:.6f}")
            pretty.log(f"Total transfers: {report['total_transfers']}")
            pretty.log(f"Total transfer alpha: {report['total_transfer_alpha']:.6f}")
            
        else:
            # Process blocks
            results = await processor.process_block_range(args.start_block, args.end_block)
            
            # Generate summary report
            report = await processor.get_summary_report(args.start_block, args.end_block)
            
            # Save results to file
            import json
            results_file = cache_dir / "processing_results.json"
            with open(results_file, 'w') as f:
                json.dump({
                    'results': results,
                    'report': report
                }, f, indent=2, default=str)
            
            pretty.log(f"Results saved to {results_file}")
    
    except KeyboardInterrupt:
        bt.logging.info("Processing interrupted by user")
    except Exception as e:
        bt.logging.error(f"Processing failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
