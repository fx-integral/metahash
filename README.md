<div align="center">
<picture>
  <source srcset="image.png" media="(prefers-color-scheme: dark)">
  <source srcset="image.png" media="(prefers-color-scheme: light)">
  <img src="image.png" width="96">
</picture>

# **MetaHash | Subnet 73**

[![Discord Chat](https://img.shields.io/discord/308323056592486420.svg)](https://discord.gg/bittensor)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Twitter Follow](https://img.shields.io/twitter/follow/MetaHashSn73?style=social)](https://twitter.com/MetaHashSn73)

🌐 [Website](https://metahash73.com) • ⛏️ [Miner Guide](docs/miner.md) • 🧪 [Validator Guide](docs/validator.md)
</div>

---

## 🚀 Overview
**MetaHash (Subnet 73)** is a decentralized liquidity and incentive layer on the Bittensor network.

It is designed to:
- Give **dTAO holders** a way to put α to work across subnets,  
- Allow **miners and subnet owners** to access α without destabilizing their own liquidity pools,  
- Enable **validators** to allocate weights in a transparent, market-driven way.  

In short: MetaHash connects α supply and demand while minimizing slippage, improving capital efficiency, and strengthening subnet economics.

---

## 🔥 Value Proposition

### 🧑‍🌾 For dTAO Holders
- Open participation – you don’t need to be a miner to earn.  
- Convert **α → MetaHash** exposure seamlessly.  
- Deploy α across subnets without causing slippage in your origin pools.  
- Receive transparent accounting of how your α is allocated.  

### 🧍‍♀️ For Subnet 73
- Acts as a **liquidity hub** where α demand meets α supply.  
- Validator weights are allocated by a **fair, deterministic auction**, not subjective heuristics.  
- **Budget signaling and burns** ensure unused α is never misallocated.  
- Strengthens SN73’s role as a backbone for cross-subnet liquidity.

---

## 🔁 How It Works (Epoch Lifecycle)

MetaHash validators run a three-epoch pipeline:

### **Epoch e: Auction & Clearing**
1. **AuctionStart** — validator broadcasts start of auction.  
2. **Bids** — miners submit `(subnet_id, α, discount_bps)`.  
3. **Clearing** — bids are ranked by **TAO value** with slippage and optional reputation caps; partial fills allowed.  
4. **Early Wins** — winners are notified with a `Win` invoice, including the **payment window** `[as, de]` in e+1.  
5. **Stage Commitment** — snapshot of winners + budget signals (`bt_mu`, `bl_mu`) saved locally.

### **Epoch e+1: Commitments**
- Validator publishes e’s snapshot:  
  - **CID-only** on-chain (v4 commitments)  
  - Full JSON payload to IPFS  
- Strict publisher: **only e−1 is published**, no catch-up.

### **Epoch e+2: Settlement**
- Merge payment windows, scan on-chain α transfers.  
- Apply `STRICT_PER_SUBNET` rules (if enabled).  
- Compute miner scores, **burn underfill to UID 0**, and set weights.  
- If `TESTING=true`, preview only (no on-chain weights).

---

## 🧠 Key Features
- **Auction → Clearing → Commitments → Settlement** pipeline.  
- **Slippage-aware valuation** of α bids (`K_SLIP`, `SLIP_TOLERANCE`).  
- **Reputation caps** per coldkey (baseline & max fractions).  
- **Budget signaling** (`bt_mu`, `bl_mu`) to enforce deterministic burns.  
- **Strict publisher**: CID on-chain, JSON in IPFS.  
- **Safety**: miners only pay to whitelisted treasuries (`metahash/treasuries.py`).  

---
