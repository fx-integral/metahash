# MetaHash FAQ (Subnet 73)

Use this FAQ alongside the [MetaHash overview](./overview.md) when onboarding new dTAO holders, miners, and validators.

## Quick facts

### What problem does MetaHash solve?
- MetaHash runs Subnet 73 as a liquidity and incentive layer that pairs dTAO holders willing to supply α with subnet operators that need it, while validators allocate weights through a deterministic auction pipeline.【F:README.md†L17-L58】

### Where can I see the official lifecycle?
- Validators operate a three-epoch loop: auction and clearing in epoch *e*, commitments published in *e+1*, and settlement plus weight setting in *e+2*, with CID metadata on-chain and full payloads hosted in IPFS for transparency.【F:README.md†L40-L74】【F:docs/validator.md†L13-L62】

### Which assets and treasuries are involved?
- Miners always transfer α to validator-controlled treasuries that are explicitly whitelisted in `metahash/treasuries.py`, so any payment outside that list is ignored during settlement.【F:metahash/treasuries.py†L1-L6】【F:metahash/validator/engines/settlement.py†L50-L79】

> TODO: Incorporate the latest subnet messaging thread once the community publishes finalized collateral targets for upcoming auctions.

## Supply & demand clarity

### Where is demand for α coming from, who supplies the TAO, and do I need to run a miner?
- Master validators snapshot their stake share each epoch and turn their portion of the subnet-73 emission budget (`AUCTION_BUDGET_ALPHA`) into a TAO-denominated auction budget using the live α price before clearing bids.【F:metahash/validator/engines/auction.py†L150-L195】【F:metahash/validator/engines/clearing.py†L258-L288】
- Demand is therefore limited by that TAO budget—clearing only accepts bids until the budget is spent—so the TAO ultimately originates from the same SN73 emissions that validators control via weights.【F:metahash/validator/engines/clearing.py†L308-L373】
- dTAO holders can contribute α without operating infrastructure; MetaHash explicitly lets you earn without running a miner yourself.【F:README.md†L29-L36】

### Does the TAO paid to miners come from subnet emissions tied to the α they sell?
- Yes. Each master’s TAO budget is the subnet emission (`AUCTION_BUDGET_ALPHA`) scaled by its stake share, so payouts are bounded by the emissions SN73 already allocates for incentives.【F:metahash/config.py†L24-L43】【F:metahash/validator/engines/auction.py†L150-L195】
- If that budget is not fully matched with paid α, settlement deterministically burns the remainder to UID 0, preventing rewards beyond the emission cap.【F:metahash/validator/engines/settlement.py†L170-L199】

### How does MetaHash make sure the TAO you receive equals the priced value of the α you sold?
- Clearing prices every accepted line with the effective TAO valuation, multiplying slippage-adjusted α price by subnet weight and your discount cap, then stores the result as `value_mu` in the win payload.【F:metahash/utils/valuation.py†L14-L36】【F:metahash/validator/engines/clearing.py†L308-L361】【F:metahash/validator/engines/clearing.py†L524-L552】
- Settlement only credits those recorded values after it verifies the matching α transfer landed in the correct treasury and subnet, so your credited TAO mirrors the price agreed during clearing.【F:metahash/validator/engines/settlement.py†L504-L615】

### If α sits in subnet treasuries and is staked under SN73, who creates demand and why would other miners sell my α?
- Wins are tied to validator treasuries from the allowlist; miners pay α into that treasury and the validator stakes it on SN73 before settlement, so the α backing demand lives with the subnet owner’s validator hotkey.【F:metahash/treasuries.py†L1-L6】【F:docs/miner.md†L45-L62】
- Subnet owners and builders signal demand by directing miners toward their subnet IDs; validators focus the TAO budget on those subnets via the weight map and the clearing engine’s ranking logic.【F:docs/gitbook/overview.md†L18-L38】【F:metahash/validator/engines/clearing.py†L308-L373】
- Other miners are incentivized to sell α because settlement converts credited TAO into on-chain weights for their UIDs; providing α is the mechanism that yields rewards in the MetaHash market.【F:metahash/validator/engines/settlement.py†L480-L529】【F:metahash/validator/engines/settlement.py†L712-L736】

### What keeps ongoing demand for my α instead of just offers?
- Validators continuously reprice bids against live α markets and subnet-specific weights, so they prefer bids that maximize TAO value for the emission budget they control.【F:metahash/validator/engines/clearing.py†L258-L361】
- Auction intake filters bids that don’t match subnet weights or exceed discount limits, and the shared weight map keeps demand focused on subnets that SN73 actually wants to fund.【F:metahash/validator/engines/auction.py†L240-L310】【F:weights.yml†L1-L138】
- Because unpaid α is burned to UID 0 rather than rewarded, validators need reliable α sellers to avoid wasting their emission share, which sustains real demand over time.【F:metahash/validator/engines/settlement.py†L170-L199】

## Auction & bidding

### Who is allowed to run the auction each epoch?
- Only validators whose stake meets the master threshold (`S_MIN_MASTER_VALIDATOR`) and whose treasury is listed in `VALIDATOR_TREASURIES` broadcast `AuctionStart`; everyone else silently skips the phase.【F:metahash/validator/engines/auction.py†L90-L139】【F:metahash/treasuries.py†L1-L6】

### How are miner bids evaluated?
- Accepted bids must come from non-jailed coldkeys with sufficient stake (`S_MIN_ALPHA_MINER`), target a weighted subnet, and remain within the configured per-coldkey bid limit (`MAX_BIDS_PER_MINER`); the clearing engine then ranks remaining lines by TAO value with slippage awareness and optional reputation caps before issuing win invoices.【F:metahash/validator/engines/auction.py†L222-L310】【F:metahash/validator/engines/clearing.py†L1-L119】【F:metahash/validator/engines/clearing.py†L260-L309】

### Why might a bid be rejected even if I send it?
- The validator drops bids targeting forbidden subnets, bids submitted by jail-listed coldkeys, or entries exceeding the per-subnet quota per coldkey; it also blocks UID 0 and miners below the α stake threshold, and it never runs the auction before the configured v3 start block.【F:metahash/validator/engines/auction.py†L226-L289】【F:metahash/validator/engines/clearing.py†L285-L297】

## Payments & settlement

### When do invoices arrive and how long do I have to pay?
- Winning bids receive `Win` invoices during the auction epoch (e) with an explicit payment window `[as, de]` that opens in epoch e+1, matching the miner guide’s timeline for submitting α transfers during that next epoch.【F:README.md†L44-L66】【F:docs/miner.md†L28-L57】

### How do validators verify that my payment counted?
- Settlement waits for the merged payment window to close, scans chain events for α transfers using `AlphaTransfersScanner`, filters out cross-subnet payments, and then credits miners proportionally against the staged budget before burning any underfill to UID 0.【F:metahash/validator/engines/settlement.py†L40-L124】【F:metahash/validator/engines/settlement.py†L175-L230】【F:metahash/validator/alpha_transfers.py†L1-L82】

### What happens if I pay late or on the wrong subnet?
- The scanner enforces per-subnet matching (`STRICT_PER_SUBNET`) and settlement ignores lines that miss the payment window or treasury mapping, so unpaid value is treated as a deficit and burned rather than carried forward.【F:metahash/validator/engines/settlement.py†L40-L124】【F:metahash/validator/engines/settlement.py†L175-L219】

> TODO: Add a worked α transfer example once community reviewers contribute anonymized transaction hashes.

## Validator operations

### What configuration should masters review before going live?
- Core toggles live in `metahash/config.py`, including auction budgets, reputation caps, per-subnet strictness, jail durations, and the global `TESTING` preview flag; aligning these values with subnet policy keeps master validators in sync.【F:metahash/config.py†L7-L89】

### How are commitments published and where?
- Validators stage the winning snapshot locally, then publish CID-only commitments on-chain while uploading the full JSON payload to IPFS during epoch e+1, ensuring anyone can audit settlement inputs later.【F:README.md†L56-L74】【F:docs/validator.md†L13-L62】

### What local state should operators monitor?
- The validator maintains JSON caches for validated epochs, jailed coldkeys, reputation scores, and pending commitments under the `StateStore`; operators can wipe or inspect these files when diagnosing sync issues.【F:metahash/validator/state.py†L1-L88】

## Safety & troubleshooting

### How are miner payments guarded against mistakes?
- The miner client only schedules transfers to validators in the allowlist (`metahash/treasuries.py`) and rechecks windows before submitting extrinsics, while settlement filters events to Subtensor `transfer_stake` calls (plus optionally Utility batches) to avoid counting unrelated transactions.【F:neurons/miner.py†L184-L266】【F:metahash/validator/alpha_transfers.py†L1-L82】

### What happens if a miner underpays repeatedly?
- Auction intake tracks coldkeys with outstanding jail epochs, and settlement can extend jail durations via the state store; miners below the stake floor or still jailed simply cannot submit bids until the epoch gate clears.【F:metahash/validator/engines/auction.py†L240-L269】【F:metahash/validator/state.py†L1-L88】

### Where should contributors look before escalating an incident?
- Check the Gitbook overview, validator logs (including commitment and settlement banners), and the cached state files; if context is missing, escalate through community channels and annotate this FAQ with a TODO so future editors can merge the clarified response.【F:docs/gitbook/overview.md†L1-L120】【F:metahash/validator/engines/settlement.py†L40-L124】【F:metahash/validator/state.py†L1-L88】

> TODO: Document the preferred escalation channel for emergency subnet coordination once the core team finalizes the rotation schedule.
