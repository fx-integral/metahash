<div align="center">
<picture>
  <source srcset="image.png" media="(prefers-color-scheme: dark)">
  <source srcset="image.png" media="(prefers-color-scheme: light)">
  <img src="image.png" width="96">
</picture>

# **MetaHash Group | Treasury Subnet (SN73)**

[![Discord Chat](https://img.shields.io/discord/308323056592486420.svg)](https://discord.gg/bittensor)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Twitter Follow](https://img.shields.io/twitter/follow/MetaHashSn73?style=social)](https://twitter.com/MetaHashSn73)

🌐 [Website](https://metahash73.com) • ⛏️ [Miner Guide](https://docs.metahash73.com/MinerSetupSimple) • 🧪 [Validator Guide](https://docs.metahash73.com/ValidatorSetupSimple)
</div>

---

## Overview

**MetaHash Group** is a treasury-driven governing entity within the Bittensor ecosystem.

It operates **Subnet 73 (SN73)** as its on-chain treasury and coordination layer, while expanding its scope to **acquire, govern, and support a portfolio of subnets** across multiple functional domains.

SN73 continues to function as a decentralized **alpha acquisition and settlement subnet**, but its role is now situated within a broader group-level strategy focused on capital allocation, subnet ownership, and long-term ecosystem development.

---

## MetaHash Group Structure

### Group-Level Role

MetaHash Group acts as:
- A **capital allocator** and treasury operator
- A **governance and coordination layer** for aligned subnets
- A long-term steward of infrastructure within Bittensor

The group treasury is anchored to **SN73**, which executes auctions, commitments, and settlements on-chain.

---

## Domain-Based Subnet Portfolio

MetaHash Group organizes subnet activity across five functional domains:

### Compute
- Inference and training workloads  
- GPU capacity provisioning  
- Core infrastructure primitives  

### Robotics
- Control systems and simulation  
- Embodied AI workloads  
- Physical-world inference loops  

### Data & Signals
- Data feeds and telemetry  
- Market and web signals  
- Real-time and batch signal processing  

### Agents & Automation
- Orchestration of models and tools  
- Operational and application agents  
- Multi-agent systems  

### Advertising
- Creative generation  
- Targeting and optimization  
- Spend allocation and performance analytics  

Each domain may include one or more subnets, operated either directly by MetaHash Group or by independent teams under aligned incentive and governance frameworks.

---

## Role of Subnet 73 (SN73)

### What Remains Unchanged
- SN73 continues to run **alpha acquisition auctions**
- SN73 continues to act as a **treasury**
- Auction, clearing, commitment, and settlement mechanics remain intact
- Validators continue to set weights deterministically based on delivered alpha

### What Has Evolved
SN73 is no longer positioned as a single-purpose liquidity venue.

Instead, it serves as:
- The **treasury subnet** of MetaHash Group
- A capital formation layer for subnet acquisition and support
- A coordination point for group-level strategy

In short:

> **SN73 is the execution and treasury layer of MetaHash Group.**

---

## How the Treasury Mechanism Works

MetaHash validators operate a deterministic, multi-epoch pipeline:

### Epoch *e*: Auction & Clearing
1. Validator broadcasts `AuctionStart`
2. Miners submit bids: `(subnet_id, alpha_amount, discount_bps)`
3. Bids are valued using slippage-aware pricing and optional reputation caps
4. Winners receive `Win` invoices with a payment window in epoch *e+1*
5. Auction results are snapshotted locally

### Epoch *e+1*: Commitments
- Validator publishes the snapshot:
  - **CID only** on-chain (v4 commitments)
  - Full JSON payload to IPFS
- Strict publishing order: only *e−1* is committed

### Epoch *e+2*: Settlement
- Validator scans on-chain alpha transfers during payment windows
- Credits delivered alpha
- **Burns underfilled amounts to UID 0**
- Computes miner incentives and sets weights
- Optional preview-only mode when `TESTING=true`

---

## Key Properties

- Deterministic **Auction → Clearing → Commitment → Settlement** pipeline
- Slippage-aware alpha valuation
- Reputation caps enforced per coldkey
- Budget signaling to enforce capital discipline
- Whitelisted treasury enforcement for miner payments
- Transparent, auditable on-chain commitments

---

## Community & Subnet Expansion

MetaHash Group actively engages with the community to:
- Identify capable and motivated subnet teams
- Acquire subnet slots
- Provide capital, coordination, and long-term alignment
- Support meaningful contributions to the Bittensor ecosystem

This model emphasizes **alignment over control** and **capital efficiency over fragmentation**.

---

## License

MIT License. See `LICENSE` for details.
