<div align="center">
<picture>
  <source srcset="image.png" media="(prefers-color-scheme: dark)">
  <source srcset="image.png" media="(prefers-color-scheme: light)">
  <img src="image.png" width="96">
</picture>

# **Metahash | Subnet 73** <!-- omit in toc -->

[![Discord Chat](https://img.shields.io/discord/308323056592486420.svg)](https://discord.gg/bittensor)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Twitter Follow](https://img.shields.io/twitter/follow/MetaHashSn73?style=social)](https://twitter.com/MetaHashSn73)

[Website](https://metahash73.com) • [Mining Guide](docs/miner.md) • [Validator Guide](docs/validator.md)
</div>

---
## Overview
**Metahash (Subnet 73)** is a decentralized over-the-counter (OTC) layer that allows dTAO holders swap **$ALPHA** directly for **$META**, eliminating slippage and price impact on their native subnets.

All incoming **$ALPHA** is routed to the **Treasury**, where it can be used to  

1. provide liquidity to other subnets (Uniswap V3)  
2. execute external OTC trades for a margin  
3. consume digital commodities  
4. accrue yield (APR)  
5. serve as a liquidity layer for derivative platforms in Bittensor
6. Others...


---

## Value Proposition
### For DTAO Holders
- Participation open to **anyone**, not just "miners" of other subnets.
- Specially usefull for **miners** and **subnet owners**.
- Swap $ALPHA for $META via seamless OTC trades.  
- Dodge slippage and pool price impact on origin subnets in exchange of a **discount**.

### For SN73 Holders  
- Exposure to a decentralized suite of OTC strategies.  
- Portfolio of diversified, discounted $ALPHA.

### For the Network  
- Provides liquidity across Bittensor.
- Serves as a “stimulus check” that allows Bittensor to bet on itself.


## How It Works

### Epoch Lifecycle

Metahash operates on a structured, three-phase cycle that starts at the beginning of each epoch.
Each epoch functions like an **“auction”** for $META, in which miners compete to acquire it.

#### Phase 1: Reward Calculations For Previous Auction
- Track alpha transfers from the previous epoch
- Aggregate total per miner
- Apply slippage-adjusted to valuation
- Adjust base on bond curve
- Give miners their corresponding $META by setting weights

#### Phase 2: Preparation (Blocks 1–49)
- Miners prepare for the auction
- Validators finalize reward calculations and weight setting of previous epoch
- In future iterations, this period will be use to notify miner of auction details

> Auction opens at block 50 each epoch.

#### Phase 3: Dutch Auction
- Discount miners give starts low and increases as alpha reach the treasury
- Bonding curve determines the discount each miner is accepting when transfering the alpha
- Early miners receive higher rewards
- Auction state (alpha already sent) is on-chain and miners can track it
- Auction ends at last epoch block

---

## Auction Flow

### Budget

Each epoch, Metahash distributes its mining emissions via 'auction':

> **B = 0.41 × 361 = ~148 $META per epoch**

Where `361` is the number of blocks per epoch, and `0.41` is the mining emissions

---
## Alpha Valuation Design

### 1 · The Problem
- **Miners accept the OTC deal** by doing an alpha transfer on-chain"
- **Random ordering** inside each block makes miner rewards unpredictable.  
- **Equal reward across the epoch** leads to last-second bidding wars.
- Miners sending alpha on early blocks depend on late block activity.

---

### 2 · Solution — Dutch Auction + Bonding Curve
| Benefit          | Why it Matters                                                     |
|------------------|---------------------------------------------------------------------|
| **Early rewards**| Higher value for alpha submitted early.                            |
| **Known discount**| Miners can see the live discount at any moment.                      |
| **Smooth path**  | Block-level randomness remains, but the overall trend is predictable.|

---

### 3 · Pricing Models Compared
|                   | Uniform Price                                   | Bond-Curve Price                                        |
|-------------------|-------------------------------------------------|---------------------------------------------------------|
| **Mechanics**     | Same reward for every alpha unit.               | Value of each alpha decreases as more alpha arrives     |
| **Miner incentives**| Favors guessing & collusion.                  | Rewards speed & individual optimisation.                |
| **Subnet risk**   | Unlimited volatility.                           | Upside and downside capped; unused budget burns.        |

Uniform Valuation of alpha creates a dynamic where miners compete **TOGETHER** agains the subnet trying to make the subnet over pay.

Bond Curve valuation limits upside and downside. Subnet now is limited on how much "profit" it can generate but it makes miners compete against **EACH OTHER** and subnet neves losses money as $META is burned in the case of under-subscription of the auction (not enough alpha was sent to compensate mining emissions value)

### Reward Rate Formula

```math
rate(m) = max(r_min, C₀ / (1 + β · m))
```
> Any unspent SN73 is burned to preserve token value.

| Symbol | Description |
|--------|-------------|
| `m`    | SN73 tokens already issued in epoch |
| `C₀`   | Starting rate (top of bonding curve) |
| `β`    | Decay factor |
| `r_min`| Minimum rate floor |


---

## Risk Mitigation

Several attack vectors have already been assessed:

- **Sending $META is Forbidden:** Cool concept but ponzi. Forbidden. 
- **Slippage Guards:** Excessive slippage zeroes the alpha value
- **Anti-Timing Attacks:** Epoch-average calculation in pricing and slippage
- **Undersubcription in Auctions:** Unused SN73 is permanently burned

---


## Proposals

To know more about the future of Metahash and next iterations pls check this!

[View our proposals](proposals.md)

---


## Welcome to MetaHash — The Evolution of Merit

Join us in building a decentralized OTC infrastructure for Bittensor.  
Trade alpha, manage treasury, and create the next generation of market mechanisms.

---


