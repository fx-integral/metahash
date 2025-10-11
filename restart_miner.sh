#!/bin/bash

echo "🔄 Restarting miner with fresh state and debug logging disabled..."
pm2 stop autoppia
pm2 delete autoppia

echo "🚀 Starting miner with --fresh flag to clear all state..."
pm2 start neurons/miner.py --name autoppia --interpreter /home/usuario1/miniconda3/envs/metahash/bin/python -- \
  --wallet.name owner \
  --wallet.hotkey miner3 \
  --netuid 348 \
  --subtensor.network ws://128.140.68.225:11144 \
  --subtensor.chain_endpoint ws://128.140.68.225:11144 \
  --miner.bids.netuids 136 \
  --miner.bids.amounts 10.0 \
  --miner.bids.discounts 1000 \
  --miner.bids.validators 5HbYLLdo1eqXoBtUBZ4mcKTSaM7Scgv6At96GEqfmPnHSSxh \
  --axon.port 9001 \
  --fresh

echo "📊 Checking status..."
pm2 status

echo "📋 Recent logs (last 15 lines):"
pm2 logs autoppia --lines 15

echo "✅ Miner restarted with fresh state and reduced logging!"
echo ""
echo "Monitor with: pm2 logs autoppia --follow"
