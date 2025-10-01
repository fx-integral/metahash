# MetaHash (Subnet 73) Overview

> MetaHash turns subnet-level α demand into a transparent market: validators run deterministic auctions, miners supply α under guardrails, and dTAO holders see where their capital goes. This page keeps the Gitbook version grounded in the live code paths.

## Quick summary: what MetaHash does
- **A liquidity and incentive bridge for Subnet 73** that matches α supply from dTAO holders with demand from miners and subnet operators, while validators enforce deterministic weight setting through an auction → clearing → commitments → settlement pipeline.【F:README.md†L19-L68】【F:neurons/validator.py†L165-L195】
- **Budgets, reputation caps, and slippage protections** are parameterized in `metahash/config.py`, ensuring every bid respects α planck precision, subnet weight haircuts, and per-coldkey limits before it is accepted or cleared.【F:metahash/config.py†L13-L61】【F:metahash/validator/engines/auction.py†L242-L305】
- **Early invoices and α transfer scans** keep payments auditable: winners receive `Win` synapses with explicit payment windows, miners schedule transfers, and validators reconcile receipts before applying weights or burning underfill to UID 0.【F:metahash/protocol.py†L42-L69】【F:neurons/miner.py†L214-L347】【F:metahash/validator/engines/settlement.py†L102-L200】

## Role snapshots
### dTAO holders
- Delegate α into MetaHash’s auction instead of running infrastructure; reputation caps and burn mechanics keep idle budget from diluting returns.【F:README.md†L22-L43】【F:metahash/validator/engines/settlement.py†L169-L199】
- Track allocations through staged commitments and IPFS payloads, with value accounting exposed in μTAO for easy reconciliation.【F:README.md†L55-L67】【F:metahash/validator/engines/clearing.py†L41-L60】

### Miners
- Bid `(subnet_id, α, discount_bps)` during `AuctionStart`; bids respect subnet weights and per-coldkey limits before they are staged for clearing.【F:docs/miner.md†L35-L50】【F:metahash/validator/engines/auction.py†L242-L306】
- Use the [Miner Guide](../miner.md) for CLI flags, payment hotkey routing, and discount strategies; invoices include payment windows so automation can retry safely inside `[as, de]`.【F:docs/miner.md†L54-L118】【F:neurons/miner.py†L214-L347】

### Validators
- Operate the three-phase loop (settle e−2, auction e, publish e−1) with dedicated engines for auction, clearing, commitments, and settlement.【F:docs/validator.md†L16-L33】【F:neurons/validator.py†L165-L195】
- Configure budgets, reputation knobs, and treasury allowlists before broadcasting `AuctionStart` so only reachable miners submit bids and only whitelisted treasuries receive α.【F:metahash/config.py†L35-L52】【F:metahash/validator/engines/auction.py†L242-L415】

### Subnet owners & builders
- Signal α demand through miners without draining local liquidity pools; partial fills and reputation-aware caps let them attract consistent supply.【F:README.md†L22-L43】【F:metahash/validator/engines/clearing.py†L41-L206】
- Reference auction outcomes and settlement burns to plan subnet incentives or diagnose underfilled epochs.【F:README.md†L47-L68】【F:metahash/validator/engines/settlement.py†L102-L200】

## Auction lifecycle (end-to-end)
1. **Auction start broadcast (epoch e)** – Master validators snapshot stake share, filter reachable axons, and send `AuctionStartSynapse` messages containing budget, weights, and treasury details.【F:metahash/validator/engines/auction.py†L320-L409】【F:metahash/protocol.py†L14-L39】
2. **Bid submission & validation** – Miners reply with bids; validators enforce stake minimums, per-subnet weight > 0, per-coldkey subnet limits, and auction budget ceilings before accepting entries into the bid book.【F:metahash/validator/engines/auction.py†L242-L305】
3. **Clearing & early wins** – The clearing engine prices bids in TAO, applies slippage curves and reputation caps, allows partial fills, and stages payloads with `bt_mu`/`bl_mu` plus payment windows for commitments in e+1.【F:metahash/validator/engines/clearing.py†L41-L206】
4. **Payment windows & invoices (epoch e+1)** – Winners receive `WinSynapse` invoices with `[as, de]` block ranges; miners schedule payments with safety delays and retries, paying the mapped validator treasury on the correct subnet.【F:metahash/protocol.py†L42-L69】【F:neurons/miner.py†L214-L347】
5. **α transfer execution** – Validators merge payment windows, scan `transfer_stake` events, and discard cross-subnet or forbidden credits; miners’ automation monitors responses and logs failures for retry.【F:metahash/validator/engines/settlement.py†L149-L170】【F:metahash/validator/alpha_transfers.py†L11-L118】【F:neurons/miner.py†L282-L357】
6. **Settlement & incentive assignment (epoch e+2)** – Paid α pools gate miner scoring, leftover budget is burned to UID 0, and final weights apply unless `TESTING` is enabled; commitments publish CID + payload to IPFS for verifiability.【F:metahash/validator/engines/settlement.py†L102-L204】【F:docs/validator.md†L25-L33】

## Diagram-ready checklist
1. Auction start banner → stake share → filtered axons → broadcast payload.
2. Bid intake table (subnet, α, discount) with validation guardrails.
3. Clearing flow with TAO valuation, reputation caps, partial fills, and staged commitment.
4. Invoice timeline `[as, de]` linking miner scheduler to validator treasury mapping.
5. α transfer scan highlighting subnet matching and burn-to-UID0 safety valve.
6. Settlement outputs: credited value, leftover burn, final weights applied.

## Glossary & constants
- **α planck (`PLANCK`)** – Base unit (1 α = 1e9 planck) used throughout miner invoices and settlement math.【F:metahash/config.py†L22-L61】
- **Auction budget (`AUCTION_BUDGET_ALPHA`)** – Per-master α budget share that bounds accepted bids before slippage/valuation.【F:metahash/config.py†L35-L43】【F:metahash/validator/engines/auction.py†L320-L405】
- **Discount basis points (`discount_bps`)** – Miner-configured max haircut applied after subnet weights; enforced in bid acceptance and clearing.【F:docs/miner.md†L35-L118】【F:metahash/validator/engines/clearing.py†L41-L206】
- **Reputation caps (`REPUTATION_*`)** – Per-coldkey TAO ceilings blended between equal share and historical performance to prevent dominance.【F:metahash/config.py†L46-L61】【F:metahash/validator/engines/clearing.py†L171-L206】
- **Win invoice (`WinSynapse`)** – Validator → miner message describing accepted α, clearing discount, and payment window for the next epoch.【F:metahash/protocol.py†L42-L69】

## Alignment check & open assumptions
- This overview covers **auction start**, **bid validation**, **payment scheduling & α transfers**, and **settlement/incentive assignment** per the Task Master brief.【F:metahash/validator/engines/auction.py†L320-L441】【F:metahash/validator/engines/clearing.py†L41-L206】【F:metahash/validator/engines/settlement.py†L102-L204】
- Validators and miners can drill deeper via the [Validator Guide](../validator.md) and [Miner Guide](../miner.md) for operational playbooks.【F:docs/validator.md†L16-L122】【F:docs/miner.md†L35-L118】
- [TODO] Integrate highlights from the latest MetaHash SN73 announcement (`https://x.com/MetaHashSn73/status/1972740673368727600`) once accessible; current environment receives HTTP 403 so messaging alignment needs confirmation.

## How to iterate on this page
- Capture reader feedback through Gitbook comments or Discord threads, then mirror updates in `.codex/tasks/2f33f6ca-gitbook-overview.md` so future editors can trace rationale.
- When code changes adjust budgets, reputation math, or settlement policy, update the corresponding glossary bullets and lifecycle steps alongside edits to `metahash/config.py`, the validator engines, and the [Miner](../miner.md)/[Validator](../validator.md) guides.【F:metahash/config.py†L35-L68】【F:metahash/validator/engines/clearing.py†L41-L206】【F:metahash/validator/engines/settlement.py†L102-L204】
