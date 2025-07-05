# MetaHash Miner Setup Guide (Subnet 73)

## Quick Start Summary

**MetaHash miners do NOT run a miner neuron.** You only need to:
1. Register your miner on Subnet 73
2. Send alpha tokens from other subnets to participate in auctions
3. Compete for SN73 alpha rewards

---

## What is MetaHash?

MetaHash is a decentralized OTC (Over-The-Counter) alpha acquisition system. You trade alpha tokens from other subnets in exchange for discounted SN73 incentives through competitive auctions.

**Key Rules:**
- ⚠️ **FORBIDDEN:** Cannot send SN73 alpha tokens to auctions
- ✅ **ALLOWED:** Only alpha from other subnets (SN33, SN1, etc.)
- 🎯 **GOAL:** Maximize post-slippage value of alpha sent

---

## Prerequisites

- Python 3.10+
- Git
- btcli (Bittensor CLI) configured with your wallet
- Registered miner on at least one subnet to obtain alpha tokens

---

## Setup

```bash
# Clone and setup environment
git clone https://github.com/fx-integral/metahash/ && cd metahash
python3 -m venv .venv
source .venv/bin/activate
pip install uv
uv pip install -e .

# Register your miner (ONE per coldkey only)
btcli s register --netuid 73 --wallet.name YOUR_WALLET_NAME --wallet.hotkey YOUR_HOTKEY_NAME
```

**⚠️ CRITICAL:** Only register ONE miner per coldkey. Multiple registrations will result in only one receiving incentives.

---

## How Auctions Work

- **Frequency:** Every 361 blocks (~1 epoch)
- **Supply:** 148 SN73 alpha tokens per auction
- **Valuation:** Based on post-slippage TAO value
- **Competition:** Miners compete for highest value contributions
- **Rewards:** Distributed based on total value sent relative to other miners

---

## Mining Options

### Option A: Manual Trading
Send alpha to treasury manually using btcli or custom scripts for maximum control and competitive advantage.

### Option B: Automated Auction Watcher (Recommended)

```bash
python scripts/miner/auction_watch.py \
    --target-netuid 33 \
    --validator-hotkey <VALIDATOR_HOTKEY> \
    --wallet.name <WALLET_NAME> \
    --wallet.hotkey <HOTKEY_NAME> \
    --max-alpha 30 \
    --step-alpha 5 \
    --max-discount 20
```

**Parameters:**
- `--netuid`: Source subnet for alpha tokens (cannot be 73)
- `--validator-hotkey`: Validator hotkey where your alpha is staked
- `--max-alpha`: Maximum alpha to bid per auction
- `--step-alpha`: Incremental bidding steps
- `--max-discount`: Stop bidding if discount falls below this threshold

**Script Behavior:**
1. Monitors auctions in real-time
2. Bids alpha in increments up to your maximum
3. Stops when discount threshold is reached
4. Automatically stops if auction becomes over-subscribed

---

## Strategy & Best Practices

### Competitive Advantages
- **Early bidding:** Higher discounts, better deals
- **Efficient timing:** Beat competitors with better acquisition rates
- **Conservative thresholds:** Avoid unfavorable deals
- **Diversification:** Spread across multiple auctions

### Risk Management
- ⚠️ **Stop if over-subscribed:** Zero value for excess contributions
- ⚠️ **Monitor discounts:** Don't accept poor deals
- ⚠️ **Use surplus alpha only:** Don't compromise home subnet operations
- ⚠️ **Track performance:** Optimize future strategies

---

## Resources

- [MetaHash GitHub](https://github.com/fx-integral/metahash/)
- [Bittensor Documentation](https://docs.bittensor.com/)
- [SN73 Specifications](https://github.com/fx-integral/metahash/blob/main/docs/sn73-specs.md)