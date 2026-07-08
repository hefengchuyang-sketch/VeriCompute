# VeriCompute

**A privacy-first settlement protocol for verifiable compute.**

![VeriCompute banner](docs/images/vericompute-banner.png)

VeriCompute turns real workloads into blockchain-secured value. Instead of relying only on empty hash puzzles, the system coordinates useful computation, verifies delivery, protects sensitive task data, and routes rewards through an auditable on-chain economy.

The repository contains a Python node implementation, a unified RPC/REST API gateway, a React dashboard, protocol modules for PoUW consensus and compute markets, and validation scripts for security and launch readiness.

## Why It Matters

Cloud compute marketplaces can match buyers and providers, but they rarely solve the trust layer: whether a provider ran the task correctly, preserved privacy, returned the result, and accepted accountable settlement.

VeriCompute focuses on that missing layer. Demand-side users submit encrypted compute tasks, supply-side miners and providers contribute capacity, and the protocol records task execution, verification, disputes, rewards, fees, and treasury routing.

## Project Thesis

Modern compute is abundant but fragmented. AI researchers, small labs, and independent teams often need reliable GPU capacity, while many operators own underused hardware that still carries fixed cost. VeriCompute is designed as a privacy-first compute bank and settlement chain for verifiable outsourced work.

Core principles:

- Useful work should create network value.
- Verification cost must stay lower than computation cost.
- Private task data should not be exposed to untrusted providers by default.
- Compute markets need settlement, dispute resolution, and auditability, not only matching.
- Economic flows should be transparent enough for governance and long-term operations.

## What Is Implemented

This codebase is a broad prototype of a PoUW-based compute network. The main implementation areas are:

- **Full node runtime**: P2P networking, storage, wallet, RPC service, mining loop, and configurable node roles.
- **PoUW and S-Box mining**: useful-work scoring, hybrid consensus modes, S-Box generation, fallback policies, and sector-aware rewards.
- **Dual-layer consensus**: Layer 1 security functions plus Layer 2 compute task lifecycle and verification.
- **Compute market**: task submission, task acceptance, result submission, challenge flow, pricing, billing, futures, orderbook, and provider-side execution modules.
- **Privacy and security**: encrypted task handling, end-to-end task channels, access controls, TEE-oriented verification, zero-knowledge hooks, audit trails, and security baseline tests.
- **Economics and governance**: MAIN token flow, sector coins, dual-witness exchange, treasury routing, proposals, contribution governance, and fee configuration.
- **Unified API gateway**: a Flask gateway that exposes JSON-RPC, V3 REST endpoints, stats, health checks, and a unified query entrypoint.
- **Frontend dashboard**: a Vite + React + TypeScript interface for wallet, mining, tasks, market, governance, explorer, provider, and settings workflows.

## High-Level Architecture

```text
Users / Researchers
        |
        v
Encrypted Compute Tasks
        |
        v
Compute Market + Scheduler
        |
        v
Miners / Providers / Sector Pools
        |
        v
PoUW, S-Box PoUW, Witness Verification
        |
        v
Consensus + Ledger + Settlement
        |
        v
Rewards, Fees, Treasury, Governance
```

Core task lifecycle:

```text
Client submits encrypted task
        |
        v
Provider accepts and executes work
        |
        v
Result hash / proof is submitted
        |
        v
Challenge window opens
        |
        v
Settlement finalizes on-chain
        |
        v
Rewards, fees, burns, and treasury routes are recorded
```

The system is split into two logical layers:

| Layer | Purpose | Main Capabilities |
| --- | --- | --- |
| Layer 1: Security | Maintain chain state and validator security | PoS/DPoS-style validator registration, VRF-oriented selection, BFT-style finality hooks, slashing, block production |
| Layer 2: Compute Value | Coordinate useful computation | task submission, worker acceptance, encrypted results, challenge games, Merkle-style commitments, reward settlement |

Privacy features sit across both layers through encrypted task payloads, secure task channels, TEE-oriented attestation modules, zero-knowledge verification hooks, and explicit audit records.

## Consensus Modes

VeriCompute supports multiple consensus profiles through `config.yaml`:

| Mode | Description |
| --- | --- |
| `sbox_primary` | Default profile. S-Box PoUW is the primary path with a smaller classic PoUW support ratio. |
| `mixed` | Targets a configured ratio between S-Box PoUW and classic PoUW. |
| `sbox_only` | Runs only the S-Box PoUW path, including periodic scoring quizzes. |
| `pouw_only` | Runs the classic Proof of Useful Work path. |

Important configuration keys:

```yaml
consensus:
  mode: sbox_primary
  sbox_ratio: 0.50
  pouw_support_ratio: 0.10
  sbox_enabled: true
  fallback_policy: idle_block_only
  target_block_time: 30
  required_witnesses: 2
```

## Repository Layout

```text
.
|-- main.py                         # Full node entrypoint
|-- config.yaml                     # Default node configuration
|-- config.mainnet.yaml             # Mainnet-oriented configuration
|-- genesis_block.json              # Genesis data
|-- requirements.txt                # Python dependencies
|-- api/
|   |-- unified_gateway.py          # Unified RPC/REST gateway
|   `-- pouw_api_v3.py              # V3 API support
|-- core/
|   |-- consensus.py                # Base consensus engine
|   |-- unified_consensus.py        # Integrated consensus entrypoint
|   |-- pouw_chain_v3.py            # V3 dual-layer chain implementation
|   |-- sbox_engine.py              # S-Box PoUW engine
|   |-- compute_market_v3.py        # Compute task market
|   |-- encrypted_task.py           # Encrypted task lifecycle
|   |-- rpc_service.py              # JSON-RPC service and method registry
|   |-- rpc_handlers/               # Modular RPC handlers
|   `-- ...                         # Wallet, storage, governance, pricing, security, P2P modules
|-- frontend/
|   |-- src/                        # React dashboard source
|   |-- package.json                # Frontend scripts and dependencies
|   `-- vite.config.ts              # Vite configuration
|-- scripts/
|   |-- start_unified_gateway.py    # Recommended API gateway startup
|   |-- start_nodes.py              # Multi-node startup helper
|   |-- unified_smoke.py            # Smoke validation
|   `-- ...                         # Deployment, validation, benchmark scripts
|-- tests/
|   |-- test_pouw_v3_complete.py
|   |-- test_unified_gateway.py
|   |-- test_unified_consensus.py
|   |-- integration/
|   `-- ...
`-- docs/                           # Technical docs, audits, deployment guides, reports
```

## Requirements

- Python 3.9+
- pip
- Node.js 18+ and npm for the frontend
- LevelDB development libraries may be required by `plyvel` on some systems
- Windows, Linux, or macOS

For a clean Python environment:

```bash
python -m venv .venv

# Windows PowerShell
.\.venv\Scripts\Activate.ps1

# Linux / macOS
source .venv/bin/activate

pip install -r requirements.txt
```

## Quick Start

### Fastest Local Demo Path

For a quick review, start the unified gateway, open the frontend dashboard, and use the demo flow to submit, execute, query, and settle a compute task locally.

```bash
python scripts/start_unified_gateway.py --host 127.0.0.1 --port 8000

cd frontend
npm install
npm run dev
```

### 1. Install Backend Dependencies

```bash
pip install -r requirements.txt
```

### 2. Start the Unified API Gateway

The gateway is the easiest way to inspect the system because it exposes health checks, JSON-RPC, V3 REST endpoints, and API documentation from one process.

```bash
python scripts/start_unified_gateway.py --host 127.0.0.1 --port 8000
```

Useful endpoints:

```text
GET  http://127.0.0.1:8000/health
GET  http://127.0.0.1:8000/api/stats
GET  http://127.0.0.1:8000/api/docs
POST http://127.0.0.1:8000/rpc
POST http://127.0.0.1:8000/api/unified/query
```

### 3. Start a Full Node

For the complete node runtime:

```bash
python main.py --config config.yaml
```

Common options:

```bash
python main.py --rpc-port 8545
python main.py --role miner --mining
python main.py --role provider --data-dir ./data_provider
python main.py --testnet
```

### 4. Start the Frontend

```bash
cd frontend
npm install
npm run dev
```

The frontend expects a backend JSON-RPC service behind `/rpc` through the Vite proxy configuration.

## API Examples

### Health Check

```bash
curl http://127.0.0.1:8000/health
```

### Register a Validator

```bash
curl -X POST http://127.0.0.1:8000/api/v3/validator/register \
  -H "Content-Type: application/json" \
  -d '{
    "validator_id": "validator_001",
    "address": "MAIN_validator_address",
    "stake": 1000.0
  }'
```

### Submit an Encrypted Compute Task

```bash
curl -X POST http://127.0.0.1:8000/api/v3/task/submit \
  -H "Content-Type: application/json" \
  -d '{
    "task_id": "task_001",
    "client": "client_001",
    "encrypted_data": "YmFzZTY0X2VuY29kZWRfZGF0YQ==",
    "compute_type": "AI_INFERENCE",
    "reward": 50.0,
    "client_bond": 10.0,
    "verification_type": "CHALLENGE",
    "privacy_mode": "TEE"
  }'
```

### Accept a Task

```bash
curl -X POST http://127.0.0.1:8000/api/v3/task/accept \
  -H "Content-Type: application/json" \
  -d '{
    "task_id": "task_001",
    "worker": "miner_001",
    "worker_stake": 20.0
  }'
```

### Submit a Result

```bash
curl -X POST http://127.0.0.1:8000/api/v3/task/submit_result \
  -H "Content-Type: application/json" \
  -d '{
    "task_id": "task_001",
    "worker": "miner_001",
    "result_hash": "sha256_result_hash",
    "proof": "proof_payload"
  }'
```

### JSON-RPC Request

```bash
curl -X POST http://127.0.0.1:8000/rpc \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "wallet_getInfo",
    "params": {}
  }'
```

## Frontend Features

The React dashboard includes pages for:

- account and wallet management
- dashboard statistics
- explorer views
- mining status
- task submission and task detail views
- compute market and order views
- provider workflows
- governance proposals
- privacy and settings pages
- demo flows and help pages

Primary frontend source files are under `frontend/src/pages`, `frontend/src/components`, `frontend/src/api`, and `frontend/src/store`.

## Testing

Run the full Python test suite:

```bash
python -m pytest
```

Run focused tests:

```bash
python -m pytest tests/test_pouw_v3_complete.py -q
python -m pytest tests/test_unified_gateway.py -q
python -m pytest tests/test_unified_consensus.py -q
python -m pytest tests/test_security_regression_access.py -q
```

Run integration tests:

```bash
python -m pytest tests/integration -q
```

Run a smoke check:

```bash
python scripts/unified_smoke.py
```

## Configuration

The default configuration lives in `config.yaml`.

Key sections:

| Section | Purpose |
| --- | --- |
| `network` | chain id, P2P host/port, bootstrap nodes, RPC host/port, CORS, rate limits |
| `node` | node id, display name, hardware sector, role |
| `mining` | mining enablement, miner address, accepted task types, task concurrency |
| `storage` | data directory, backend, cache, compression |
| `wallet` | wallet directory, default wallet behavior, mnemonic length |
| `consensus` | consensus mode, S-Box ratios, fallback policy, block timing, witness count |
| `api` | admin key, websocket options |
| `dev` | debug and simulation settings |

Environment overrides used by the runtime include:

```text
POUW_RPC_HOST
POUW_ADMIN_KEY
```

## Documentation Map

Start with these documents depending on what you want to understand:

| Goal | Documents |
| --- | --- |
| Run the project | `docs/QUICKSTART.md`, `docs/USER_GUIDE.md`, `docs/OPERATIONS.md` |
| Understand APIs | `docs/API.md`, `docs/UNIFIED_API_GATEWAY.md` |
| Understand consensus | `docs/CONSENSUS.md`, `docs/DUAL_LAYER_CONSENSUS.md`, `docs/THREE_LAYER_CONSENSUS.md`, `docs/POUW_V3_COMPLETE_TECHNICAL_DOC.md` |
| Understand security | `docs/SECURITY_ARCHITECTURE.md`, `docs/SECURITY_HARDENING.md`, `docs/SECURITY_BASELINE_CHECKLIST.md`, `docs/RPC_PERMISSION_BASELINE.md` |
| Understand economics | `docs/FEE_MECHANISM.md`, `docs/DYNAMIC_PRICING_IMPLEMENTATION.md`, `docs/GOVERNANCE_VOTING.md` |
| Prepare deployment | `docs/DEPLOYMENT.md`, `docs/MAINNET_DEPLOY.md`, `docs/PRODUCTION_READINESS_REPORT.md` |
| Review validation evidence | `docs/reports/`, `docs/SECURITY_AUDIT*.md`, `docs/TECHNICAL_REVIEW_AND_CONSENSUS_RECOMMENDATIONS_2026-05-09.md` |

The docs home also provides role-based reading paths:

```text
docs/README.md
```

## Development Workflow

Suggested backend loop:

```bash
python -m py_compile main.py api/unified_gateway.py core/pouw_chain_v3.py
python -m pytest tests/test_unified_gateway.py -q
python scripts/start_unified_gateway.py --host 127.0.0.1 --port 8000
```

Suggested frontend loop:

```bash
cd frontend
npm install
npm run dev
npm run build
```

## Security Notes

This repository includes security and production-readiness work, but it should still be treated as an experimental protocol implementation unless it has passed an independent audit and a controlled deployment review.

Before production deployment:

- configure non-empty admin secrets and RPC permissions
- restrict CORS and exposed RPC methods
- disable automatic wallet creation in production
- confirm bootstrap nodes and peer policies
- run the security regression and baseline tests
- review `docs/SECURITY_HARDENING.md`
- review `docs/PRODUCTION_READINESS_REPORT.md`

## Current Status

The project is organized as a research-to-prototype implementation with substantial module coverage across consensus, compute markets, privacy, governance, API access, and frontend operations. The current goal is local experimentation, protocol review, architecture discussion, and controlled demos, not permissionless production deployment. Production use requires hardening, external review, deployment discipline, and real-world adversarial testing.

## License

License pending. All rights reserved until a license file is added.
