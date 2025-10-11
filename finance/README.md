# Finance Module - Portfolio and Transfer Caching Services

This module provides caching services for portfolio states and transfer events in the MetaHash network. It allows efficient retrieval of historical portfolio and transfer data by caching calculations and scans.

## Features

- **PortfolioService**: Calculates and caches portfolio states including TAO/Alpha balances, subnet allocations, and budget utilization
- **TransferService**: Scans and caches transfer events using the existing AlphaTransfersScanner
- **CacheLayer**: Generic caching layer with block-based keys and tolerance ranges
- **Efficient Retrieval**: Fast lookups using cached data with configurable tolerance ranges

## Components

### CacheLayer
Base caching layer that provides:
- Thread-safe operations
- Block-based caching with tolerance ranges
- Persistent storage to disk
- Efficient range queries

### PortfolioService
Service for calculating and caching portfolio states:
- Calculates portfolio metrics (TAO/Alpha balances, subnet allocations, budget utilization)
- Caches results for efficient retrieval
- Supports range queries and tolerance-based lookups

### TransferService
Service for scanning and caching transfer events:
- Uses existing AlphaTransfersScanner for transfer detection
- Caches transfer summaries with statistics
- Supports range scanning with configurable step sizes

## Usage

### Basic Usage

```python
import asyncio
from pathlib import Path
import bittensor as bt
from metahash.finance import PortfolioService, TransferService

async def main():
    # Initialize bittensor
    subtensor = bt.subtensor(network="finney")
    
    # Create cache directory
    cache_dir = Path("./cache")
    
    # Initialize services
    portfolio_service = PortfolioService(cache_dir, subtensor, tolerance_blocks=360)
    transfer_service = TransferService(cache_dir, subtensor, tolerance_blocks=360)
    
    # Get portfolio state for a specific block
    target_block = 6633585
    portfolio_state = await portfolio_service.get_portfolio_state(target_block)
    
    # Get transfers for a specific block
    transfer_summary = await transfer_service.get_transfers(target_block)
    
    # Get cached data (fast retrieval)
    cached_portfolio = portfolio_service.get_cached_portfolio(target_block)
    cached_transfers = transfer_service.get_cached_transfers(target_block)

asyncio.run(main())
```

### Processing Block Ranges

Use the `process_blocks.py` script to process large block ranges:

```bash
# Process last 7200 blocks with 360-block intervals
python -m metahash.finance.process_blocks --start-block 6626385 --end-block 6633585 --step 360

# Generate summary report only
python -m metahash.finance.process_blocks --start-block 6626385 --end-block 6633585 --summary-only
```

### Command Line Options

- `--start-block`: Starting block number (default: 6633585 - 7200)
- `--end-block`: Ending block number (default: 6633585)
- `--step`: Step size between blocks (default: 360)
- `--cache-dir`: Cache directory (default: ./cache)
- `--network`: Bittensor network (default: finney)
- `--netuid`: Network UID (default: 1)
- `--summary-only`: Only generate summary report

## Cache Structure

The cache stores data in JSON format with the following structure:

```
cache/
├── portfolio_cache.json    # Portfolio state cache
└── transfers_cache.json    # Transfer summary cache
```

Each cache entry includes:
- Block number
- Timestamp
- Cached data (portfolio state or transfer summary)

## Tolerance Range

The caching system uses a tolerance range to find cached data near the requested block. For example, if you request block 6633585 with a tolerance of 360 blocks, it will return cached data from any block between 6633225 and 6633945.

## Performance

- **Cache Hits**: Near-instant retrieval of cached data
- **Cache Misses**: Full calculation/scanning required
- **Range Queries**: Efficient retrieval of multiple cached entries
- **Persistent Storage**: Data survives application restarts

## Error Handling

The services include robust error handling:
- Graceful handling of RPC failures
- Automatic retry mechanisms
- Detailed logging of errors and statistics
- Fallback to cached data when available

## Example Output

```
[PROCESSING BLOCK RANGE]
Processing blocks from 6626385 to 6633585 with step 360

Progress: 10 blocks processed, 5 portfolio states calculated, 3 transfers scanned, 0 errors
Progress: 20 blocks processed, 8 portfolio states calculated, 6 transfers scanned, 0 errors

[PROCESSING COMPLETE]
Blocks processed: 20
Portfolio states calculated: 8
Portfolio states from cache: 12
Transfers scanned: 6
Transfers from cache: 14
Errors: 0
Portfolio cache entries: 8
Transfer cache entries: 6
```

## Dependencies

- bittensor
- asyncio
- pathlib
- dataclasses
- typing
- json
- threading

## Notes

- The portfolio calculation is currently simplified and may need enhancement based on actual settlement engine logic
- Transfer scanning uses the existing AlphaTransfersScanner with proper RPC locking
- Cache files are stored in JSON format for easy inspection and debugging
- All operations are thread-safe and can be used in concurrent environments
