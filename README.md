# POUW-Chain

**Proof of Useful Work — Privacy-Preserving Verifiable Compute Network**

A revolutionary decentralized blockchain platform that transforms real computation tasks into a consensus mechanism with privacy protection.

> **🎉 V3.0 Complete Implementation (2026-05-06)**: 
> 
> **核心突破：完整双层共识架构 + 隐私计算**
> 
> **Layer 1（安全层）**：
> - ✅ PoS/DPoS 共识 - 负责出块、防攻击、状态一致性
> - ✅ VRF 随机性 - 公平选举，防止操纵
> - ✅ Slashing 机制 - 自动惩罚，保证安全
> - ✅ BFT 最终性 - 快速确认
> 
> **Layer 2（价值层）**：
> - ✅ PoUW 任务市场 - 任务提交、执行、验证、奖励
> - ✅ Challenge Game - 防作弊，类似 Truebit
> - ✅ 可验证计算 - zk-proof 验证成本降低99%
> - ✅ 状态提交 - Rollup 模型，Merkle Tree
> 
> **隐私计算**：
> - ✅ TEE 模式 - Intel SGX / AMD SEV
> - ✅ zk 模式 - 零知识证明
> - ✅ MPC 模式 - 多方安全计算
> 
> **经济优化**（保留）：
> - ✅ 在线奖励池 - 小矿工稳定收益
> - ✅ 秒级兑换 - 乐观确认
> - ✅ 冷启动解决 - 渐进式质押
> - ✅ 资金可追溯 - 完全透明
> 
> **关键原则**：
> - ✅ 共识 ≠ 计算
> - ✅ 验证成本 < 计算成本
> - ✅ 默认不信任（Trustless）
> - ✅ 隐私优先（Privacy by design）
> 
> 详见 [V3.0完整技术文档](docs/POUW_V3_COMPLETE_TECHNICAL_DOC.md) | [技术白皮书](docs/TECHNICAL_WHITEPAPER.md)

## 1. Vision & Thesis

### Project Thesis

POUW-Chain is built on a simple thesis:

> Global compute is abundant but fragmented, while small teams with meaningful problems cannot access reliable and verifiable compute delivery.

Inspired by early Ethereum-style application narratives, this project is not only a technical prototype. It is a protocol-level attempt to turn compute from an opaque service into a transparent, verifiable, and economically coordinated public utility.

Core claims:

- Useful work should secure the chain, not waste energy on meaningless hashes.
- Compute markets need verification and dispute resolution, not only matching and pricing.
- Economic incentives must be transparent and governable from day one.

---

### Founder Context

This project is driven by a practical founder-level pain point from AI research:

- I need to run real workloads but often do not have enough local GPU capacity.
- Renting third-party compute is convenient but raises data leakage and accountability concerns.
- At the same time, many small teams own underutilized cards that still incur fixed cost.

POUW-Chain is built to close this gap: verifiable access for demand-side researchers, and measurable utilization for supply-side small operators.

---

### Privacy-First Compute Bank Thesis

POUW-Chain treats privacy and capacity as first-class protocol concerns.

### Data Privacy Problem

In AI research, people often rent external GPUs because they have no local cards. That creates a practical risk: sensitive datasets and intermediate artifacts may leak across untrusted infrastructure.

POUW-Chain addresses this with layered protections:

- End-to-end encrypted task data channels
- Optional S-Box-enhanced transport path
- Explicit dispute and audit trails for delivery integrity
- Multi-level compute security roadmap (standard/enhanced/confidential)

### Compute Bank Attribute

Small companies and labs frequently hold idle GPU capacity that turns into sunk cost.

POUW-Chain positions this idle capacity as a **compute bank**:

- Idle cards can be deposited as productive supply
- Demand-side users access verifiable compute without owning expensive hardware
- On-chain economics route value between users, miners, and treasury transparently

This is not only a marketplace; it is a mechanism for turning idle hardware into a trustable financial-technical utility.

---

### Why This Matters for Human Progress

This project targets a structural bottleneck:

- Small companies and independent labs often have strong ideas but insufficient compute access.
- Existing compute markets optimize speed and price, but underinvest in verifiability and accountability.

POUW-Chain aims to make compute:

- Accessible: organize fragmented resources into usable capacity
- Trustable: prove delivery quality instead of relying on opaque vendor promises
- Meaningful: direct compute toward scientific, engineering, and public-value workloads

In short, this is an attempt to make compute economically useful and socially meaningful at the same time.

---

### System Design Philosophy Map

POUW-Chain is built around one principle: **turn fragmented compute into trustable, accountable infrastructure**.

At a system level, the design couples five layers instead of optimizing any single layer in isolation:

1. **Useful-work consensus layer**
    Real workloads (not empty hashes) participate in security and rewards.
2. **Verification and dispute layer**
    Multi-witness checks, arbitration, and compensation routes make delivery quality auditable.
3. **Privacy and safety layer**
    Encrypted transport, S-Box-enhanced paths, and TEE-oriented controls protect data and execution.
4. **Governance and treasury layer**
    Strategy updates, sector evolution, and treasury routing are governable and rollback-aware.
5. **Commercial execution layer**
    Pricing, billing, and service modules convert protocol capability into sustainable operations.

ASCII system map:

```text
Fragmented Compute Supply + Real Demand
                     |
                     v
        Useful-Work Consensus (POUW/S-Box)
                     |
                     v
 Verification + Dispute + Compensation
                     |
                     v
    Privacy/Security Controls (E2E/S-Box/TEE)
                     |
                     v
 Governance + Treasury + Economic Routing
                     |
                     v
  Sustainable Service Layer (Pricing/Billing/Membership)
```

Why this matters: the protocol is not only a chain mechanism; it is a full trust-cost reduction stack for real compute transactions.

---

## 2. Architecture & Core Mechanisms

### System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     POUW-Chain                               │
├─────────────┬─────────────┬─────────────┬─────────────┬─────┤
│   H100      │    A100     │  RTX4090    │  RTX4080    │ ... │
│   Sector    │   Sector    │   Sector    │   Sector    │     │
├─────────────┴─────────────┴─────────────┴─────────────┴─────┤
│                 Dual-Witness Layer (Multi-Witness)           │
├─────────────────────────────────────────────────────────────┤
│              S-Box PoUW Layer (Cryptographic Output)         │
├─────────────────────────────────────────────────────────────┤
│                    MAIN Chain Ledger                         │
├─────────────────────────────────────────────────────────────┤
│  Compute    │  Governance │ Sector Coin  │  Nonce            │
│  Market     │  System     │  Exchange    │  Manager          │
└─────────────────────────────────────────────────────────────┘
```

### Core Flow

```
Mining → S-Box Generation + POUW Task → Sector Coin Reward → Dual-Witness Exchange → MAIN Token
                  ↓                                  ↓
       S-Box Library (Encryption)         Burn Sector Coin + Mint MAIN
```

---

### Key Features

### POUW Consensus
- Earn block rewards by executing real, useful computation tasks
- Real workloads: AI inference, numerical optimization, hash computation, etc.
- Scoring weights: Completion rate 30% + Latency 25% + Online stability 25% + Block participation 20%
- Dual-channel difficulty is active in code:
    - Hash difficulty channel (`current_difficulty`)
    - Work-threshold channel (`current_work_threshold`, auto-adjusted by recent mining observations)
- Structured process proof (`proof_json`) is validated on-chain (proof hash, challenge commitment, anti-duplicate checks)
- Idle-window anti-speculation is enabled: long consecutive idle blocks receive additional reward decay

### S-Box PoUW — Cryptographic Proof of Useful Work
- Each block produces a verified **S-Box** (256-byte substitution box, fundamental to AES-class ciphers)
- Quality scored by: **Nonlinearity** (Walsh-Hadamard) + **Differential Uniformity** + **Avalanche Effect**
- Mined S-Boxes feed directly into P2P encryption — zero waste consensus
- Multi-sector VRF selection: all sectors mine independently, one winner per block
- Chain data compression: compact mode stores only S-Box hash (~250 bytes vs ~700 bytes/block)

### Hybrid Consensus Mode (POUW + S-Box PoUW)
- Built-in mixed mode supports both POUW and SBOX_POUW in the same network
- `consensus.mode` controls strategy: `sbox_primary`, `mixed`, `sbox_only`, `pouw_only`
- `consensus.sbox_ratio` controls SBOX_POUW target share in mixed mode (0.0 - 1.0)
- `consensus.pouw_support_ratio` controls classic POUW injection share in `sbox_primary` mode (0.0 - 1.0)
- Default deployment profile now uses `sbox_primary` (S-Box primary path + low-ratio POUW support)
- In `sbox_only`, each miner receives a deterministic random S-Box scoring quiz every 30 minutes:
    - quiz changes score weights + score threshold + hash difficulty in the current window
    - quiz id and window range are recorded in block `extra_data` for auditability
- Automatic fallback keeps liveness: S-Box unavailable -> POUW, both unavailable -> PoW fallback
- Strategy governance is versioned and rollbackable:
    - `chain_updateMechanismStrategy` supports version/rollout/max_ratio_step updates
    - `chain_getInfo` returns `mechanismStrategy`

### Multi-Sector Architecture
- Sectors divided by hardware type: H100, RTX4090, RTX3080, CPU, GENERAL
- Each sector produces blocks and halves independently
- Sector coins are separate from the MAIN token
- Community can add/remove sectors via DAO voting

### Dual-Token Economic Model
| Token | Description |
|-------|-------------|
| **MAIN** | Primary token, cannot be mined, only obtained by exchanging sector coins. Max supply: 100 million |
| **Sector Coins** | H100_COIN, RTX4090_COIN, etc. Mined directly, each sector capped at 21 million |

### Security

#### Transaction-Level Security
- ECDSA secp256k1 signature verification
- Dual-witness mechanism (MAIN transfers require 2+ sector confirmations)
- Account nonce for double-spend prevention
- BIP-39 mnemonic wallet + AES-256-GCM encrypted storage

#### Compute Task Security
- **Standard Mode** ★★★☆☆: Container isolation + end-to-end encryption (~8% overhead)
- **Enhanced Mode** ★★★★☆: Task sharding + redundant verification + S-Box SubBytes (~12% overhead)
- **Confidential Mode** ★★★★★: software closed-loop integrated (attestation pre-check + KMS gate + rollout audit), hardware trust-chain rollout pending (~30% overhead target)

#### P2P Encryption
- ECDH (X25519) key agreement → AES-256-GCM + S-Box SubBytes stacking
- Ephemeral session keys: one-time use, forward secrecy guaranteed
- S-Box snapshot-locked per session: no mid-task cipher switching
- Fixed-address operations (wallet transfers): pure AES-256-GCM, no S-Box needed

> **Details**: See [Security Architecture](docs/SECURITY_ARCHITECTURE.md)  
> **Threat Model**: Standard mode defends against semi-honest miners; TEE mode defends against malicious root administrators

### 💸 Decentralized Fees
- 0.5% burned (deflationary)
- 0.3% miner incentive
- 0.2% foundation (multi-sig)

### 🏦 Block Reward Distribution
- 97% to block-producing miner
- 3% auto-transferred to DAO treasury (MAIN_TREASURY)

---

### Core Mechanisms

### 1. MAIN Cannot Be Mined (DR-1)

The MAIN token can only be obtained by exchanging sector coins — it cannot be mined directly:

```python
# blockchain_service.py
def get_block_reward(sector: str) -> Tuple[float, str]:
    if sector == "MAIN":
        return 0, "MAIN"  # MAIN produces no reward
    return SECTOR_BASE_REWARDS.get(sector, (0, sector))
```

Evidence links:

- Code: `core/sector_coin.py` (`get_block_reward`, MAIN returns `0.0`)
- API example: `sector_getExchangeRates`, `sector_requestExchange`, `account_getBalance`

### 2. Dual-Witness Mechanism (DR-5/DR-6)

All MAIN-related operations require multi-sector confirmation:

```python
# main_transfer.py
class MainTransferEngine:
    MIN_WITNESSES = 2           # Standard transfers need 2 witnesses
    LARGE_TRANSFER_WITNESSES = 3  # Large transfers (≥1000) need 3 witnesses
    WITNESS_TIMEOUT = 60        # 60-second timeout
```

Evidence links:

- Code: `core/main_transfer.py` (`MIN_WITNESSES`, `LARGE_TRANSFER_WITNESSES`)
- API example: `tx_send`, `account_getTransactions`

### 3. Sector Coin Exchange (DR-9)

Sector coin → MAIN uses a burn-and-mint model:

```python
# dual_witness_exchange.py
def execute_exchange(request):
    # 1. Burn sector coins
    sector_ledger.burn(request.from_address, request.amount)
    
    # 2. Wait for dual witness
    witnesses = collect_witnesses(request, min_count=2)
    
    # 3. Mint MAIN
    main_ledger.mint(request.to_address, main_amount)
```

Evidence links:

- Code: `core/dual_witness_exchange.py`, `core/sector_coin.py`
- API example: `sector_requestExchange`, `sector_getExchangeHistory`, `sector_cancelExchange`

### 4. Double-Spend Prevention (Nonce)

Each account maintains an incrementing nonce:

```python
# transaction_v2.py
class AccountNonceManager:
    def validate_nonce(self, address, nonce, txid):
        current = self.get_nonce(address)
        if nonce < current:
            return False, f"Nonce too low: {nonce} < {current}"
        return True, "OK"
```

Evidence links:

- Code: `core/utxo_store.py` (nonce retrieval/updates), `core/transaction.py`
- API example: `account_getNonce`, `tx_send`

### 5. Fee Distribution

1% transaction fee distributed in a decentralized manner:

| Ratio | Purpose | Implementation |
|-------|---------|----------------|
| 0.5% | Burn | `BURN_ADDRESS` |
| 0.3% | Miner | Block-producing miner address |
| 0.2% | Foundation | Multi-sig address |

Evidence links:

- Code: `core/fee_config.py`, `core/protocol_fee_pool.py`, `core/revenue_tracking.py`
- API example: `chain_getInfo`, `account_getBalance`

### 6. Hybrid Consensus Policy

`consensus.mode`, `consensus.sbox_ratio`, and `consensus.pouw_support_ratio` let operators tune production behavior:

- `sbox_primary`: prioritize SBOX_POUW and inject low-ratio POUW as market-stability support
- `mixed`: deterministic ratio-based mix of POUW and SBOX_POUW
- `sbox_only`: prioritize SBOX_POUW, fallback to POUW when unavailable
- `pouw_only`: run classic POUW path only

Example:

```yaml
consensus:
    mode: sbox_primary
    sbox_ratio: 0.50
    pouw_support_ratio: 0.10
    sbox_enabled: true
```

In `sbox_only`, each miner user receives one deterministic performance trap question every 50 blocks in their mining sector.
The trap question is deterministic per miner + sector window, and its result is connected to dispatch scoring:
- trap score is fused into miner `system_score`
- unsubmitted trap in current window applies dispatch weight penalty
- submitted trap in current window applies score-based dispatch multiplier

RPC for trap lifecycle:
- `compute_getTrapQuestion`
- `compute_submitTrapAnswer`

Evidence links:

- Code: `core/consensus.py` (`consensus_mode`, `consensus_sbox_ratio`)
- API example: `chain_getInfo` (`consensusMode`, `consensusSboxRatio`, `mechanismStrategy`, distribution fields)

### 7. Mining Mechanism Update (2026-04)

Current mining path includes the following implementation-level updates:

- Dynamic dual-threshold mining:
    - Hash target and PoUW work-threshold adjust independently using recent block observations.
- Deterministic, auditable dispatch challenge seed:
    - Default source is chain-style seed (`POUW_DISPATCH_CHALLENGE_SOURCE=chain`),
      with compatibility fallback to time-window mode.
    - `compute_getOrderEvents` includes `challenge_seed` / `challenge_source` in owner/admin scope.
- Stronger runtime behavior in compute write path:
    - `POUW_COMPUTE_V3_REQUIRED=true` (default) prevents silent fallback when V3 write path fails.

Recommended production settings:

```bash
POUW_COMPUTE_V3_REQUIRED=true
POUW_DISPATCH_CHALLENGE_SOURCE=chain
```

---

## 3. Economic & Commercial Layers

### Business Model

POUW-Chain is designed as a verifiable compute marketplace with layered monetization:

- Platform take-rate charged by task or compute-time usage
- Premium verification layer for enterprise-grade auditability and risk controls
- Governance + treasury flywheel that improves pricing and scheduling policies over time

Long-term value comes from reducing trust costs in distributed compute transactions.

---

### Governance & Tokenomics Overview

This section consolidates protocol governance and economics into one reviewer-facing view.

### 1) Governance Structure (Protocol-Level)

- **Parameter governance**: consensus strategy and rollout controls are updatable with versioned, rollbackable policy (`chain_updateMechanismStrategy`).
- **Sector governance**: hardware sectors can be added/removed through DAO process, so network capacity composition is community-steered.
- **Treasury governance**: treasury routing and spending are managed through governance modules (`core/dao_treasury.py`, `core/governance_enhanced.py`, `core/treasury_manager.py`).

### 2) Economic Model (Dual Token + Fee/Reward Routing)

- **Dual-token model**:
    - `MAIN`: base network value token (not directly mined).
    - `Sector Coins`: mined by useful work within each hardware sector.
- **Fee routing**: protocol fees are split into burn + miner incentive + foundation share.
- **Block reward routing**: currently 97% to block producer and 3% to DAO treasury (`MAIN_TREASURY`).
- **Exchange path**: sector coin value is routed toward MAIN through burn/mint exchange flow.

### 3) Quality Convergence Path (Low-Quality -> High-Quality)

The design intent is to make low-quality capacity economically less attractive over time:

- **Performance-weighted earning**: completion, latency, stability, and participation are explicit scoring dimensions in POUW.
- **Sector-isolated issuance**: each sector has independent cap/halving behavior, so weak sectors cannot infinitely dilute strong-sector contribution.
- **Mechanism steering**: `sbox_primary` + configurable `pouw_support_ratio` keeps high-verifiability path as default while retaining liveness support.
- **Audit/dispute pressure**: failed delivery and disputes increase economic cost for unstable providers.

Expected long-run tendency (not a guaranteed promise): sectors with persistently low quality should face weaker effective earnings and reduced strategic weight, while high-quality sectors retain stronger utility and payout sustainability.

### 4) Upload Failure Compensation (Implemented)

- If upload remains in `CREATED` beyond timeout, watchdog auto-cancels and refunds user locked budget.
- If miner already received data, miner gets treasury-funded bandwidth compensation.
- Current constants include:
    - `UPLOAD_TIMEOUT_SECONDS = 2 * 3600`
    - `UPLOAD_COMPENSATION_PER_GB = 0.5`
    - minimum compensation floor `0.01 MAIN`
- If treasury balance is temporarily insufficient, deferred debt is recorded and auto-replayed after refill.

### 5) Big/Small Treasury Model (Current Mapping)

For operational clarity, treasury logic can be understood as two layers:

- **Big Treasury (Strategic Pool)**:
    - DAO-level treasury (e.g., block reward treasury inflow), used for governance-approved long-cycle spending.
- **Small Treasury (Operational Buffer)**:
    - protocol fee/compensation-side pool used for near-term operational payouts (e.g., upload-timeout compensation, penalty/distribution-side buffering).

This two-layer interpretation helps separate strategic capital allocation from short-cycle reliability guarantees.

### Commercial Execution Layer

Commercialization is not only treasury/fee theory; it is also implemented through operational product modules.

### 1) Pricing, Billing, and Financial Products

- **Dynamic pricing** (`core/dynamic_pricing.py`) and **granular billing** (`core/granular_billing.py`) support differentiated monetization under heterogeneous workloads.
- **Orderbook and futures pathways** (`core/compute_market_orderbook.py`, `core/compute_futures.py`) support forward-looking capacity commitments and price discovery.
- **Exchange-rate and precision controls** (`core/exchange_rate.py`, `core/precision.py`) reduce settlement drift in mixed-asset accounting paths.

### 2) Enterprise-Grade Service Surface

- **SDK/API entry layer** (`core/sdk_api.py`, `core/rpc_service.py`, `core/rpc_handlers/`) supports external integration for member/enterprise users.
- **Secure compute service modules** (`core/secure_compute_market.py`, `core/secure_model_runtime.py`) provide a path to premium verification and higher-assurance execution modes.

### 3) Membership + Treasury Synergy

- **Treasury path** funds long-cycle protocol evolution (governance-approved strategic allocation).
- **Membership/service path** (priority scheduling, higher assurance, enterprise support) is designed to generate recurring operating cash flow.
- **Combined effect**: treasury supports expansion and hardening; recurring service revenue supports day-to-day reliability and customer retention.

---

### Production Intelligence Layer

Beyond consensus and token flow, POUW-Chain includes a production-oriented control layer that improves routing quality, operational resilience, and reviewability.

### 1) Identity, Trust, and Reputation

- **Identity binding**: on-chain/account-level identity flow is supported by DID-oriented modules (`core/did_identity.py`) and account primitives.
- **Reputation loop**: miner/provider quality can be tracked through behavior and reputation modules (`core/miner_behavior.py`, `core/reputation_engine.py`, `core/miner_registry.py`).
- **Design intent**: execution rights and economic outcomes increasingly align with long-run reliability, not short-run opportunistic participation.

### 2) Multi-Region and Cluster Scheduling

- **Cross-region scheduling path** (`core/cross_region_scheduler.py`) supports geographically distributed execution routing.
- **Cluster-level orchestration path** (`core/cluster_manager.py`) supports group-level resource coordination.
- **Market + scheduler coupling** (`core/compute_scheduler.py`, `core/compute_market_v3.py`, `core/compute_market_orderbook.py`) enables dispatch decisions that are not only price-driven, but quality/availability-aware.

### 3) Reliability and Recoverability

- **Crash journaling** (`core/crash_journal.py`) provides failure replay and post-mortem traceability hooks.
- **Runtime observability** (`core/monitor.py`, `core/mainnet_monitor.py`, `core/load_testing.py`) supports stability checks under sustained and adversarial conditions.
- **Data continuity controls** (`core/data_lifecycle.py`, `core/data_redundancy.py`) reduce operational loss risk during long-lived task flows.

### 4) Compliance and Audit Readiness

- **Audit/compliance modules** (`core/audit_compliance.py`, `core/smart_contract_audit.py`) and RPC audit paths make governance and dispute outcomes inspectable.
- **TEE verification integration path** (`core/tee_computing.py`, `core/tee_verifier_client.py`) extends software-layer security toward stronger trust boundaries.

---

### Implemented Compensation & Protection Mechanisms

This section highlights mechanisms that are already implemented in code but were previously easy to miss in high-level descriptions.

### 1) Upload Timeout Auto-Cancel + Treasury Compensation (Implemented)

- If encrypted-task upload stays in `CREATED` status beyond timeout, a watchdog auto-cancels the task.
- User locked budget is refunded automatically.
- If miner has already received data, miner receives treasury bandwidth compensation.
- Parameters currently implemented:
    - Upload timeout: `2 hours` (`UPLOAD_TIMEOUT_SECONDS = 2 * 3600`)
    - Compensation baseline: `0.5 MAIN / GB` (`UPLOAD_COMPENSATION_PER_GB = 0.5`)
    - Minimum compensation floor: `0.01 MAIN`
    - Treasury insufficient-balance path: deferred debt record, auto-paid later when treasury refills
- Code reference: `core/rpc_service.py` (`_handle_upload_timeouts`, `_cancel_upload_timeout_task`, `UPLOAD_*` constants)

### 2) Arbitration Staking + Penalty Distribution (Implemented)

- Both renter and miner stake before arbitration period starts.
- Voting requires minimum validator participation and threshold majority.
- Penalty and reward ratios are explicit and enforced:
    - `STAKE_RATIO = 5%`
    - `PENALTY_RATIO = 50%` of losing-side stake to winner
    - `VALIDATOR_REWARD_RATIO = 5%`
    - Remaining share routed to treasury
- Code reference: `core/arbitration.py`

### 3) Contract Default Compensation (Implemented)

- On compute-contract default, defaulting side margin is penalized.
- Counterparty compensation is automatically computed from penalty.
- Current implemented policy:
    - `penalty_rate` default: `0.2`
    - compensation to counterparty: `80%` of penalty
- Code reference: `core/compute_economy.py` (`handle_default`)

### 4) Budget Lock + Overpayment Refund (Implemented)

- Budget is locked conservatively at worst-case estimate to guarantee task completion.
- After settlement, excess locked amount is automatically refunded.
- Documentation reference: `docs/USER_GUIDE.md`

### 5) FUSE Fault Compensation Framework (Documented + Integrated Interfaces)

- Fault compensation and multi-layer fuse rules are documented and aligned with task/treasury/arbitration flows.
- Documentation reference: `docs/FUSE_MECHANISM.md`

---

## 4. Latest Updates & Hardening

### Latest Security & Architecture Enhancements (V2.1 - 2026-04-27)

Following the latest technical specs, the POUW-Chain scheduler and TEE systems have been hardened:

1. **Dispute Windows (挑战期):** A time-lock window (`DISPUTE_WINDOW_SECONDS`) before task settlement, enabling third-party verifiable dispute resolution. 
2. **WASM Determinism (确定性硬件执行):** Enforced `wasm_wasi` deterministic runtime requirements for verifiable TEE executed payloads.
3. **Slashing Bounds (抵押边界限制):** Dynamic `max_task_value_ratio` that bounds allocated tasks to safe risk limits, mitigating nothing-at-stake vulnerabilities during high-value assignments.
4. **TCB Revocation (TCB吊销机制):** Dynamic hardware certificate revocation checks (`_revoked_tcbs`) rejecting compromised microcode architectures (e.g., vulnerable SGX firmware) from the compute cluster.

For deep-dive documentation into V2 implementation details, refer to [TECH_SPEC_V2_IMPLEMENTATION.md](docs/TECH_SPEC_V2_IMPLEMENTATION.md).

---

### Mechanism Hardening Update (2026-04)

This repository now includes a round of system-level hardening focused on coupling boundaries, incentive alignment, and stress stability.

### 1) Order Allocation Hardening

- **Dynamic forced pool correction**
    - Effective forced ratio is no longer treated as a static declaration only.
    - Runtime scheduling applies correction based on miner quality and idle utilization:
        - `effective_forced_ratio = declared_ratio × reputation_factor × utilization_factor`
    - HYBRID mode now explicitly operates on `voluntary_pool + dynamic_forced_pool`.

- **Task topology-aware scheduling**
    - New topology model for multi-GPU jobs:
        - `SINGLE`: must fit on one miner.
        - `DISTRIBUTED`: can split across miners.
        - `SYNC`: requires sync-capable miners and single-node fit by default.

- **Anti-monopoly + exploration**
    - Dispatch weight now includes anti-monopoly suppression (log-scaled by historical assignment load).
    - Deterministic weighted selection includes epsilon exploration (`ε-greedy`) to prevent permanent head concentration.

- **Market-strengthened scoring**
    - Allocation scoring now combines quality, price competitiveness, and timeliness in one dispatch function.

- **Replication-ready validation path**
    - A bounded portion of orders can attach replication validators for cross-checking (without switching to full redundant execution by default).

### 2) Consensus Hardening

- **Explicit PoUW/PoW switching guardrails**
    - Added task-pool threshold for PoW fallback.
    - Added max consecutive PoW blocks guardrail.
    - Added minimum recent PoUW ratio guardrail to avoid prolonged degradation from useful-work consensus.

- **Three-level validation signal for PoUW proofs**
    - PoUW proofs now carry a confidence score derived from:
        - quick checks,
        - sampled/verified checks,
        - provable-structure signal.
    - Block acceptance checks confidence-weighted work sum against threshold.

- **Useful-work reward binding**
    - Block reward now supports useful-work bonus term:
        - `reward = base_reward + λ × useful_work_score`
    - Validation rules were updated accordingly to keep reward-cap checks consistent.

### 3) Stability Goals of This Update

- Reduce declaration-execution mismatch gaming.
- Reduce long-tail centralization drift in dispatch.
- Keep chain liveness while preserving useful-work share.
- Improve confidence in PoUW correctness under adversarial conditions.

---

### Tech Spec v2 Implementation

The v2 anti-adversarial upgrade is implemented in runnable form with backward compatibility.

- Core implementation: `core/compute_scheduler.py`
- Structured mapping: [docs/TECH_SPEC_V2_IMPLEMENTATION.md](docs/TECH_SPEC_V2_IMPLEMENTATION.md)

Implemented highlights:

- Task-layer tier constraints, verification mode, and unpredictability seed
- Heterogeneous scheduling score with reliability uncertainty penalty
- Verification layer with `none` / `consensus` / `sampling` and dispute fallback
- Beta reputation update + periodic decay
- Incentive hooks including tier fee multiplier and slash path

Security regression quick check:

```bash
python -m pytest tests/test_security_regression_access.py -q
python -m pytest tests/test_tee_closed_loop.py -q
python -m pytest tests/test_rpc_permission_baseline.py -q
```

Reviewer evidence reports:

- [Reviewer Evidence Summary](docs/reports/REVIEWER_EVIDENCE_SUMMARY.md)
- [Main Flow Review (2026-04-09)](docs/reports/MAIN_FLOW_REVIEW_2026-04-09.md)
- [Public Dataset Validation (Iris)](docs/reports/public_dataset_validation_iris.md)
- [Public Dataset Validation (Digits)](docs/reports/public_dataset_validation_digits.md)
- [Adversarial Access Control Report](docs/reports/adversarial_access_report.md)
- [Large Chunk Integrity Report](docs/reports/large_chunk_integrity_report.md)
- [Short Reliability Validation Report](docs/reports/short_reliability_report.md)
- [Provider No-Leakage Write Test Report](docs/reports/provider_no_leakage_report.md)
- [Full Validation Summary](docs/reports/full_validation_summary.md)

### Validation Snapshot (Local)

Based on the latest local evidence package:

- Full validation summary: `23/23` checks passed (`100.0%`)
- Public dataset end-to-end success: Iris `100.0%`, Digits `100.0%`
- Adversarial authorization controls: key deny/allow checks `100.0%`
- Large-chunk integrity and malformed input rejection: `100.0%`
- Short reliability suite (concurrency/restart/reproducibility): all core checks passed
- Provider no-leakage write test: scanner/runtime checks `100.0%`
- TEE closed-loop tests: `10/10` passed (`tests/test_tee_closed_loop.py`)

### TEE Closed-Loop Status

- Software-layer closed: implemented and tested
    - TEE attestation is bound to order node identity and strict consistency checks.
    - TEE order creation now performs pre-check (evidence age / whitelist / node binding).
    - TEE orders enforce attestation validation policy automatically.
    - Result submission in TEE mode blocks settlement unless attestation verification passes.
    - Model/task decryption path is gated by KMS policy (attestation + policy required).
    - Verifier forwarding is integrated with local fallback when third-party verifier is unavailable.
    - Rollout and failure reasons are queryable via RPC audit interfaces.
- Deployment-layer pending:
    - Vendor-grade hardware trust chain integration (DCAP/SEV-SNP/cloud verifier)
    - Certificate chain / revocation / production key management operations

### Critical Reviewer Questions (And Current Answers)

1) Will low-quality compute providers flood the network?

- Current answer: partially mitigated, not eliminated.
- Evidence today: owner-only access controls, adversarial denial checks, and task-result integrity checks are strong.
- Remaining risk: quality-level differentiation is still not fully stress-tested under long-running, heterogeneous, multi-tenant loads.
- Next validation priority: sustained quality scoring and slashing behavior under noisy/minimally-performing providers.

2) Can high-quality compute be reliably delivered and settled?

- Current answer: protocol path is validated, production-grade SLO still pending.
- Evidence today: end-to-end task path and restart/reproducibility tests pass locally; large-file transfer integrity is stable.
- Remaining risk: no multi-region, customer-grade long-window benchmark yet.
- Next validation priority: external workload replay and duration-based fulfillment SLO tracking.

3) Is there a risk of economic model collapse?

- Current answer: collapse risk is reduced by explicit penalties/treasury routes, but not fully ruled out.
- Evidence today: default compensation and arbitration penalty routes are implemented in code.
- Remaining risk: treasury stress, correlated defaults, and extreme pricing feedback loops need simulation or live-paper trading data.
- Next validation priority: treasury stress scenarios and adverse-demand Monte Carlo backtests.

4) Does the token have guaranteed value preservation?

- Current answer: no guarantee.
- Position: this repository demonstrates protocol mechanics, not a financial guarantee product.
- Evidence today: transparent fee/reward routing and burn/treasury logic exist.
- Remaining risk: long-term value depends on real demand, governance quality, and market structure, not code alone.

5) Is privacy fully guaranteed?

- Current answer: no, privacy is strong but not absolute.
- Evidence today: encrypted channels, owner-only access controls, and provider-side no-leakage write tests pass.
- Remaining risk: advanced side-channel and host-level attacks are out of current local test scope.
- Next validation priority: production hardware attestation integration and third-party security review.

### Scope Boundary

The current evidence package is local and protocol-focused. It supports engineering credibility, but should not be over-claimed as:

- full production readiness certification,
- guaranteed token value preservation,
- or absolute privacy/security proof.

---

## 5. Implementation & Validation Status

### Current Stage & Validation Needs

### R&D Stage: Early-Formed, Pre-Scale Validation

The core protocol and end-to-end demo flow are implemented:

- Order submission -> miner execution -> result return -> settlement visualization
- Sector-based mining and hybrid consensus policies
- Security controls (multi-witness, nonce, encrypted channels)

The project is now in a critical phase: real-world validation and debug convergence, not feature inflation.

### What We Need Next

- 2-3 real customer workload scenarios (anonymous acceptable)
- GPU/CPU heterogeneous hardware environments for tuning and fault injection
- Continuous observation windows to measure SLA, fulfillment quality, and dispute rate

This phase is where protocol assumptions meet production reality.

---

### Implementation Status & Known Gaps

To keep this README accurate for reviewers, the following status reflects current repository reality.

### Implemented and Demo-Ready

- Core flow: order -> execution -> result return -> settlement visualization
- Hybrid consensus controls (`mixed`, `sbox_only`, `pouw_only`)
- Dual-witness transfer/exchange paths
- Upload-timeout auto-cancel + treasury compensation
- Arbitration staking, penalty routing, and validator voting

### Implemented but Needs Production Validation

- Privacy/security stack under heterogeneous real-world workloads
- Dynamic pricing behavior under sustained multi-tenant traffic
- Compensation/debt replay behavior under treasury stress conditions

### Deployment Items Still Pending

- Confidential mode hardware trust-chain integration (Intel DCAP / AMD SEV-SNP / cloud attestation service)
- Larger-scale benchmark evidence with external customer workloads
- Operational hardening under long-running, mixed hardware clusters

### Current Validation Boundary

Current evidence demonstrates protocol correctness and local operational stability. It is not equivalent to full production certification.

---

### Implemented Capability Coverage Matrix

To make implementation scope auditable for reviewers, below is a capability-level map of modules already present in the repository.

| Capability Domain | Representative Implemented Modules |
|---|---|
| Consensus & Chain | `core/consensus.py`, `core/unified_consensus.py`, `core/sbox_miner.py`, `core/pouw_scoring.py` |
| Compute Market & Pricing | `core/compute_scheduler.py`, `core/dynamic_pricing.py`, `core/compute_market_orderbook.py`, `core/compute_economy.py` |
| Privacy & Security | `core/e2e_encryption.py`, `core/sbox_crypto.py`, `core/security.py`, `core/attack_prevention.py`, `core/miner_security_manager.py` |
| Task Lifecycle & Acceptance | `core/encrypted_task.py`, `core/task_acceptance.py`, `core/rpc_service.py`, `core/sandbox_executor.py` |
| Arbitration & Compensation | `core/arbitration.py`, `core/compute_economy.py`, `core/protocol_fee_pool.py`, `core/dao_treasury.py` |
| Treasury & Governance | `core/dao_treasury.py`, `core/contribution_governance.py`, `core/governance_enhanced.py`, `core/treasury_manager.py` |
| Network & Data Transport | `core/message_queue.py`, `core/p2p_data_tunnel.py`, `core/p2p_task_distributor.py`, `core/tcp_network.py` |
| Wallet & Settlement | `core/wallet.py`, `core/main_transfer.py`, `core/dual_witness_exchange.py`, `core/utxo_store.py` |

For deeper verification, see `docs/CONSENSUS.md`, `docs/SECURITY_ARCHITECTURE.md`, `docs/CONTRACT_SYSTEM.md`, and `docs/API.md`.

---

## 6. Getting Started & Development

### Quick Start

### Requirements

- Python 3.9+
- 8GB RAM (recommended)
- 10GB+ disk space

### Installation

```bash
# Clone the repository
git clone https://github.com/hefengchuyang-sketch/POUW-Chain.git
cd POUW-Chain

# Install dependencies
pip install -r requirements.txt

# Required for production
pip install ecdsa mnemonic
```

### Start a Node

```bash
# Default start
python main.py

# Start with mainnet configuration
python main.py --config config.mainnet.yaml

# Start with mining enabled
python main.py --config config.mainnet.yaml --mining

# Windows quick start
.\start.ps1
```

### Start the Unified API Gateway

```bash
# Preferred launcher for the integrated RPC + V3 API gateway
python scripts/start_unified_gateway.py --host 0.0.0.0 --port 8000

# Direct module entry also works
python api/unified_gateway.py
```

### Docker Deployment

```bash
powershell -ExecutionPolicy Bypass -File .\\scripts\\deploy_preflight.ps1 -Strict
docker compose up -d --build
docker compose ps
```

If preflight reports "Docker daemon is not running", start Docker Desktop first and rerun the command.

---

### Project Structure

```
POUW-Chain/
├── main.py                  # Node entry point
├── api/unified_gateway.py   # Unified API gateway implementation
├── config.yaml              # Default/dev configuration
├── config.mainnet.yaml      # Mainnet production config
├── genesis.mainnet.json     # Genesis block config
├── requirements.txt         # Python dependencies
├── docker-compose.yml       # Docker orchestration
├── Dockerfile               # Docker build
├── start.ps1 / stop.ps1     # Windows start/stop scripts
├── scripts/start_unified_gateway.py  # Gateway launcher
├── core/                    # Core modules
│   ├── consensus.py         #   Consensus mechanism (POUW + S-Box PoUW)
│   ├── unified_consensus.py #   Unified consensus engine
│   ├── pouw_executor.py     #   POUW task execution
│   ├── pouw_block_types.py  #   POUW block typing
│   ├── pouw_scoring.py      #   POUW scoring system
│   ├── sbox_engine.py       #   S-Box evaluation & genetic optimization
│   ├── sbox_miner.py        #   Multi-sector S-Box mining & VRF
│   ├── sbox_crypto.py       #   S-Box encryption layers (STANDARD/ENHANCED/MAXIMUM)
│   ├── transaction.py       #   Transaction management
│   ├── sector_coin.py       #   Sector coin ledger + registry
│   ├── main_transfer.py     #   MAIN transfer + dual witness
│   ├── dual_witness_exchange.py  # Sector coin exchange
│   ├── dao_treasury.py      #   DAO governance + treasury
│   ├── fee_config.py        #   Fee configuration (immutable)
│   ├── protocol_fee_pool.py #   Protocol fee pool
│   ├── blind_task_engine.py #   Blind dispatch + trap verification
│   ├── compute_scheduler.py #   Compute scheduling
│   ├── dynamic_pricing.py   #   Dynamic pricing engine
│   ├── rpc_service.py       #   RPC service entry
│   ├── rpc_handlers/        #   RPC handler modules
│   ├── security.py          #   Security infrastructure
│   ├── crypto.py            #   Cryptographic utilities
│   ├── wallet.py            #   Wallet management
│   ├── message_queue.py     #   P2P messaging queue
│   ├── tcp_network.py       #   TLS P2P transport
│   └── ...                  #   And 60+ other modules
├── tests/                   # Test suite (298 tests passed as of 2026-04-09)
├── docs/                    # Documentation
│   ├── USER_GUIDE.md        #   User guide
│   ├── CONSENSUS.md         #   Consensus whitepaper
│   ├── API.md               #   API reference
│   ├── FEE_MECHANISM.md     #   Fee mechanism
│   ├── SECURITY_ARCHITECTURE.md  # Security architecture
│   ├── GOVERNANCE_VOTING.md #   Governance & voting
│   └── ...                  #   And other audit/report docs
├── frontend/                # Frontend (Vue + Vite)
├── scripts/                 # Deployment/ops scripts
│   ├── rpc_smoke_compute_v3.py  # Runtime smoke test for compute V3 path
│   ├── scratch/                 # Archived one-off debug scripts
│   └── ...
├── logs/                    # Runtime logs
│   ├── node.log             # Standard runtime log
│   ├── archive/             # Archived local outputs / historical logs
│   └── ...
├── deploy/                  # Multi-node deployment configs
├── data/                    # Runtime data (not committed)
└── wallets/                 # Wallet files (not committed)
```

---

### API Documentation

### RPC Endpoint

Default port: `8545`

### Common Methods

| Method | Description |
|--------|-------------|
| `wallet_create` | Create wallet |
| `wallet_unlock` | Unlock wallet |
| `account_getBalance` | Query balance |
| `tx_send` | Send transaction |
| `mining_start` / `mining_stop` | Start/stop mining |
| `mining_getStatus` | Mining status |
| `sector_getExchangeRates` | Sector coin exchange rates |
| `sector_requestExchange` | Sector coin → MAIN exchange |
| `task_create` | Publish compute task |
| `governance_createProposal` | Create governance proposal |
| `governance_vote` | Vote |
| `staking_stake` / `staking_unstake` | Stake / Unstake |
| `chain_getInfo` | Chain status |
| `rpc_listMethods` | List all RPC methods |

`chain_getInfo` now includes mixed-consensus observability fields:

- `consensusMode`: `sbox_primary` / `mixed` / `sbox_only` / `pouw_only`
- `consensusSboxRatio`: configured SBOX_POUW target ratio
- `consensusPouwSupportRatio`: configured POUW support ratio for `sbox_primary`
- `consensusSelectedDistribution`: rolling window stats for selected consensus type
- `consensusMinedDistribution`: rolling window stats for successfully mined consensus type

### Examples

```bash
# View chain info (-k to skip self-signed cert verification)
curl -k -X POST https://127.0.0.1:8545 \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"chain_getInfo","params":{},"id":1}'

# Check balance
curl -k -X POST https://127.0.0.1:8545 \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"account_getBalance","params":{"address":"MAIN_xxx"},"id":1}'
```

For the full RPC reference, see [docs/API.md](docs/API.md) and [docs/USER_GUIDE.md](docs/USER_GUIDE.md) appendix

---

### Verification Checklist

Use this checklist to quickly reproduce key capabilities during review.

### A) Node and RPC Baseline

- [ ] Start node: `python main.py --config config.mainnet.yaml`
- [ ] Verify RPC method list: `rpc_listMethods`
- [ ] Verify chain health: `chain_getInfo`, `chain_getHeight`

### B) Wallet, Transfer, and Nonce Safety

- [ ] Create/import wallet and query balance: `wallet_create`, `account_getBalance`
- [ ] Send transfer and verify nonce progression: `tx_send`, `account_getNonce`
- [ ] Confirm transaction in history: `account_getTransactions`

### C) Sector Coin Exchange and MAIN Path

- [ ] Read exchange rates: `sector_getExchangeRates`
- [ ] Submit exchange request: `sector_requestExchange`
- [ ] Validate exchange history and MAIN settlement visibility

### D) Compute Task Lifecycle

- [ ] Create compute task: `task_create`
- [ ] Verify scheduling and execution status transitions
- [ ] Confirm settlement/refund records after completion

### E) Arbitration and Compensation

- [ ] Trigger/inspect dispute path (arbitration period and votes)
- [ ] Verify timeout cancel path and budget refund
- [ ] Verify treasury compensation behavior (including deferred path)

---

### Development Guide

### Demo Package (One-Click)

The repository now includes a complete runnable demo package in `Demo/`.

- One-click start: `Demo/start-demo.bat`
- One-click stop and cleanup: `Demo/stop-demo.bat`
- Demo script: `Demo/demo_runner.py`
- Docker setup: `Demo/docker-compose.demo.yml`

The demo validates:

- Two-account workflow (Order Account + Mining Account)
- Free order placement (`0 MAIN`)
- Mining account visibility of accepted orders and running programs
- Order completion and result return to the order account
- Additional feature checks (`chain_getInfo`, `blockchain_getHeight`, `orderbook_submitBid`)

### Running Tests

```bash
python -m pytest tests/ --tb=short
```

### Code Standards

- Python 3.9+ type annotations
- English variable names

### Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/xxx`)
3. Commit your changes (`git commit -m 'Add xxx'`)
4. Push the branch (`git push origin feature/xxx`)
5. Create a Pull Request

### Documentation

- [User Guide](docs/USER_GUIDE.md) — Complete user tutorial
- [Consensus Whitepaper](docs/CONSENSUS.md) — POUW + S-Box PoUW technical details ⭐
- [Security Architecture](docs/SECURITY_ARCHITECTURE.md) — Security levels & threat model
- [API Reference](docs/API.md) — RPC interface documentation
- [Contract System](docs/CONTRACT_SYSTEM.md) — Compute contracts, futures & settlement
- [Fee Mechanism](docs/FEE_MECHANISM.md) — Fee distribution details
- [Governance & Voting](docs/GOVERNANCE_VOTING.md) — DAO governance mechanism
- [Operations Manual](docs/OPERATIONS.md) — Deployment & operations guide
- [Dynamic Pricing](docs/DYNAMIC_PRICING_IMPLEMENTATION.md) — Compute market pricing
- [Security Audit](docs/SECURITY_AUDIT.md) — Security vulnerability fix records
- [FUSE Mechanism](docs/FUSE_MECHANISM.md) — Fault handling and compensation rules

---

### Documentation Hub

Use the role-based docs index as entry point:

- [Docs Home (Reviewer / Investor / Developer paths)](docs/README.md)
- [Security Baseline Checklist](docs/SECURITY_BASELINE_CHECKLIST.md)
- [Codebase Review (2026-04-10)](docs/CODEBASE_REVIEW_2026-04-10.md)
- [RPC Permission Baseline](docs/RPC_PERMISSION_BASELINE.md)
- [Tech Spec v2 Implementation Notes](docs/TECH_SPEC_V2_IMPLEMENTATION.md)

## 7. Miscellaneous

### Competitive Landscape

| Dimension | **POUW-Chain** | Bitcoin | Ethereum (PoS) | Filecoin | Golem/Render |
|-----------|---------------|---------|----------------|----------|--------------|
| Consensus work is useful? | **Yes** (S-Box + compute) | No | N/A (staking) | Partial (storage) | No own chain |
| Cryptographic output? | **Yes** (S-Box primitives) | No | No | No | No |
| Hardware fair? | **Yes** (multi-sector) | No (ASIC) | No (whale) | No (storage) | Partial |
| Built-in compute market? | **Yes** | No | Via contracts | Storage only | **Yes** |
| Anti-fraud mechanism? | **Blind task + traps** | N/A | Slashing | Fault proofs | Reputation |
| Dual-token deflation? | **Yes** | No | No | Partial | No |

> For detailed comparison and project outlook, see [Consensus Whitepaper §13](docs/CONSENSUS.md#13-system-advantages-limitations-and-outlook)

---

### License

MIT License

Copyright (c) 2026 POUW-Chain

---

### Contact

- GitHub Issues: [Submit an issue](https://github.com/hefengchuyang-sketch/POUW-Chain/issues/new/choose)
- Email: yuhanliu050128@gmail.com

---

*Last updated: 2026-04-07*

## 8. Other Sections

### ## Project Visual Preview

![POUW-Chain Project Overview](docs/images/pouw-chain-banner.png)

[![Version](https://img.shields.io/badge/version-2.0.0-blue.svg)](https://github.com/hefengchuyang-sketch/POUW-Chain)
[![Python](https://img.shields.io/badge/python-3.9+-green.svg)](https://python.org)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

---

