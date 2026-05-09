# Maincoin Technical Review and Verifiable Compute Market Execution Plan

Review date: 2026-05-09
Audience: backend, protocol, frontend, and security engineers
Scope: `main.py`, `core/consensus.py`, `core/proposer_selection.py`, `core/serialization.py`, `core/task_encryption.py`, `core/compute_market_v3.py`, `core/utxo_store.py`, `core/rpc/server.py`, `core/rpc_service.py`, `frontend/src/*`

## 1. Product Direction

Maincoin should not be positioned as another generic chain. Its strongest direction is:

> A verifiable compute marketplace and settlement chain for AI and general-purpose compute tasks.

The core value is not transfer payments alone. The core value is the full loop:

```text
client creates a task
  -> client locks budget
  -> task enters the compute market
  -> worker submits a quote
  -> client accepts the quote
  -> worker executes the task
  -> worker submits result commitment and proof
  -> challenge window opens
  -> result is verified or challenged
  -> finalized block unlocks settlement
  -> worker reputation is updated
```

This loop should take priority over additional token features, more pages, or extra governance modules.

## 2. Current Implementation Status

| Area | Status | Current evidence | Remaining work |
|---|---|---|---|
| Single production consensus entry | Partial | `main.py` uses `core.consensus.ConsensusEngine` | Isolate experimental engines and add import-guard tests |
| Deterministic proposer selection | Implemented | `core/proposer_selection.py`, `tests/test_proposer_selection.py` | Replace all future proposer logic through this module; later upgrade to real VRF |
| Canonical serialization/hash | Implemented | `core/serialization.py`, `tests/test_canonical_serialization.py`; `Block.compute_hash()` uses `canonical_block_hash()` | Migrate all protocol objects to canonical hashing |
| Finality API | Partial | `ConsensusEngine.finalize_blocks()`, `get_finalized_height()`, and `chain_getConsensusStatus` exist | Connect reward release to finality and add frontend consumption |
| PoUW proof boundary | Partial | Existing PoUW execution/proof logic exists | Add explicit proof states: `VALID`, `PENDING_CHALLENGE`, `INVALID` |
| Automatic PoW fallback control | Not implemented | `select_consensus()` still needs explicit policy hardening | Add `fallback_policy`, default to `idle_block_only` |
| Task lifecycle store | Not implemented | No dedicated `TaskStateStore` | Add durable lifecycle tables and event log |
| Budget locking / reward ledger | Not implemented | UTXO store exists, but task budget lock flow is not formalized | Add `RewardLedger` and task budget/bond locking |
| Task payload privacy | Implemented foundation | `core/task_encryption.py`, `tests/test_task_encryption.py` | Integrate with `task_create`, result download, worker grants |
| RPC permission model | Partial | Existing `PUBLIC_RPC_METHODS` / `AUTHENTICATED_WRITE_METHODS` | Collapse to one permission source and add owner-only checks |
| Frontend compute-market UX | Partial | Existing pages and API client exist; backend now exposes `chain_getConsensusStatus` | Add lifecycle timeline, proof status, challenge window, settlement state |
| Folder structure cleanup | Not implemented | Current repo still has mixed production/experimental/runtime files | Apply staged migration with compatibility shims |

## 3. Immediate Engineering Fixes

### 3.1 Fix compilation blockers

Run:

```powershell
py -3 -m compileall -q core api main.py
```

Any syntax or indentation failure must be fixed before protocol work continues.

Acceptance criteria:

- `compileall` passes.
- No manual demo fragments remain in production modules.

### 3.2 Standardize Python launcher

On this machine, bare `python` may resolve to the MySQL Workbench interpreter. Use:

```powershell
py -3 -m pytest
py -3 -m compileall -q core api main.py
```

Acceptance criteria:

- All scripts and docs use `py -3` or an explicit `.venv\Scripts\python.exe`.
- CI logs `sys.executable` and `sys.version`.

## 4. Code Format and Protocol Object Rules

### 4.1 Python style

| Item | Rule |
|---|---|
| Python version | Python 3.11+ |
| Encoding | UTF-8 |
| Indentation | 4 spaces |
| Max line length | 100 preferred |
| Public functions | Must include type hints |
| Protocol data | Prefer `@dataclass` |
| On-chain time | Prefer block height over timestamp |
| Ledger amounts | Avoid long-term `float`; use integer smallest units |

Recommended `pyproject.toml`:

```toml
[tool.black]
line-length = 100
target-version = ["py311"]

[tool.ruff]
line-length = 100
target-version = "py311"
select = ["E", "F", "I", "UP", "B", "SIM"]

[tool.mypy]
python_version = "3.11"
ignore_missing_imports = true
check_untyped_defs = true
```

### 4.2 Naming rules

| Object | Rule |
|---|---|
| Python file | `snake_case.py` |
| Class | `PascalCase` |
| Function | `snake_case` |
| Constant | `UPPER_SNAKE_CASE` |
| Database table | `snake_case` |
| RPC method | `domain_actionObject`, for example `task_create`, `market_submitQuote` |
| Frontend component | `PascalCase.tsx` |

All protocol status enum values must be uppercase:

```python
class TaskLifecycleStatus(Enum):
    CREATED = "CREATED"
    FUNDED = "FUNDED"
    LISTED = "LISTED"
```

### 4.3 Canonical hash rule

Never hash protocol objects with boundary-unsafe string concatenation:

```python
hashlib.sha256(f"{a}{b}{c}".encode()).hexdigest()
```

Use:

```python
from core.serialization import hash_canonical

hash_canonical({
    "field_a": a,
    "field_b": b,
    "field_c": c,
})
```

Implemented:

- `core/serialization.py`
- `tests/test_canonical_serialization.py`

Remaining:

- Move every block, task, proof, settlement, and challenge hash onto canonical serialization.

## 5. Target Folder Structure

The repository should converge toward:

```text
maincoin/
  main.py
  pyproject.toml
  requirements.txt
  README.md
  config/
    config.yaml
    config.mainnet.yaml
    config.local.peer2.yaml
    genesis.mainnet.json
  core/
    consensus/
      engine.py
      block.py
      finality.py
      proposer_selection.py
      serialization.py
      rewards.py
    compute/
      market.py
      scheduler.py
      pouw_executor.py
      pouw_proof.py
      task_state_store.py
      task_challenge.py
    ledger/
      utxo_store.py
      transaction.py
      wallet.py
      reward_ledger.py
    security/
      crypto.py
      crypto_utils.py
      encrypted_task.py
      task_encryption.py
      tee.py
      zk.py
    rpc/
      server.py
      models.py
      handlers/
    governance/
      dao_treasury.py
      contribution_governance.py
    experimental/
      unified_consensus.py
      dual_layer_consensus.py
      pouw_chain_v3.py
  api/
  frontend/
  tests/
    unit/
    integration/
    e2e/
    security/
  scripts/
    dev/
    ops/
    validation/
  docs/
    architecture/
    audits/
    product/
    operations/
    reports/
  runtime/
    data/
    logs/
    wallets/
```

Current compatibility note:

- `core/task_encryption.py` is implemented in the current flat layout.
- After folder migration, move it to `core/security/task_encryption.py` and leave a compatibility shim.

Migration phases:

1. Add new folders and shim imports.
2. Move implementations behind old import shims.
3. Delete old shims after `rg "from core.old_path|import core.old_path"` finds no production references.

Acceptance criteria:

```powershell
py -3 -m compileall -q core api main.py
py -3 -m pytest tests
```

## 6. Security and Privacy Design

The chain must store verifiable commitments and lifecycle state, not raw private compute data.

### 6.1 Security goals

| Goal | Meaning | Engineering requirement |
|---|---|---|
| Data confidentiality | Workers and validators should not freely read raw task data | Client-side encryption; chain stores commitments only |
| Result verifiability | Clients can verify that a result belongs to a submitted task | Result commitment, proof, challenge window |
| Accountability | Worker/client/challenger actions are auditable | Addresses, public keys, signatures, task events |
| Payment safety | Budget, bonds, and rewards cannot be double-spent | UTXO / reward ledger locking |
| Minimum disclosure | Chain avoids raw inputs, outputs, logs, and secrets | Hash/CID/commitment only |
| Production fail-closed | Simulated crypto/proofs cannot pass in production | Startup safety checks |

### 6.2 Implemented privacy foundation

Implemented in:

- `core/task_encryption.py`
- `tests/test_task_encryption.py`

Available primitives:

- `generate_data_key()`
- `encrypt_payload()` / `decrypt_payload()`
- `create_input_commitment()`
- `create_result_commitment()`
- `EncryptedTaskInput`
- `EncryptedTaskOutput`
- `generate_x25519_keypair()`
- `create_worker_key_grant()`
- `open_worker_key_grant()`
- `redact_secret()`

Acceptance already covered by tests:

- AES-GCM payloads roundtrip.
- Tampered encrypted payload hashes are rejected.
- input/output metadata does not include plaintext.
- Worker key grants only open with the intended worker private key.
- Secret redaction masks sensitive strings.

### 6.3 Required integration work

Integrate `core/task_encryption.py` into the task lifecycle:

1. `task_create` must accept `input_uri`, `input_commitment`, and `encrypted_payload_hash`, not raw plaintext input.
2. `task_grantWorkerKey` must create a `WorkerKeyGrant` for the selected worker.
3. `task_submitResult` must store `result_commitment`, `encrypted_output_hash`, and `output_uri`.
4. `task_downloadResult` must be owner-only or explicitly authorized.
5. Explorer must display commitments and hashes, not plaintext payloads.

### 6.4 RPC access control

Roles:

| Role | Capabilities |
|---|---|
| guest | Public chain and public market reads |
| client | Create tasks, fund tasks, download owned results |
| worker | Register, quote, accept assignments, submit results |
| challenger | Submit and inspect challenges |
| validator | Submit validation/finality records |
| admin | Operations, parameter updates, emergency pause |

Required work:

- Collapse `PUBLIC_RPC_METHODS`, `AUTHENTICATED_WRITE_METHODS`, and handler-level registrations into one permission source.
- Every write RPC must require authentication.
- Owner-only RPCs must compare `auth_context.user_address` with the resource owner.
- Localhost must not imply admin in production.

### 6.5 Sandbox and runtime privacy

Default worker execution policy:

| Item | Default |
|---|---|
| Network | Disabled |
| Root filesystem | Read-only |
| Working directory | Temporary task-scoped directory |
| CPU/GPU | Limited |
| Memory | Limited |
| Timeout | Enforced |
| Output path | Explicit allowlist |

Required work:

- Production task execution must use Docker/container isolation.
- If Docker is unavailable, production mode must reject execution instead of silently simulating.
- Task code must not access wallet directories, logs, or node secrets.

## 7. Consensus Design

### 7.1 Layering

```text
Layer 1: security consensus
  - proposer selection
  - block validity
  - block status transitions
  - finality
  - slashing

Layer 2: useful-work layer
  - task submission
  - execution proof
  - challenge window
  - task reward calculation
  - worker reputation
```

Principles:

- PoUW can affect rewards and reputation.
- PoUW must not bypass Layer 1 block validity.
- Task failures must not halt chain liveness.

### 7.2 Proposer selection

Implemented:

- `core/proposer_selection.py`
- deterministic weighted proposer selection
- no global `random.seed()`
- `tests/test_proposer_selection.py`

Current short-term interface:

```python
select_weighted_proposer(
    candidates,
    height,
    epoch_seed,
    parent_hash,
)
```

Remaining:

- Replace any new proposer logic with this module.
- Later replace deterministic selection with real VRF while keeping a compatible interface.

### 7.3 Block lifecycle and finality

Target lifecycle:

```text
PROPOSED -> ACCEPTED -> FINALIZED
                   \-> REJECTED
ACCEPTED -> ORPHANED on reorg before finality
```

Current status:

- `ConsensusEngine.finalize_blocks()` exists.
- `ConsensusEngine.get_finalized_height()` exists.
- `chain_getConsensusStatus` exists and returns dashboard-ready finality/consensus fields.

Remaining:

- Prevent reward release before finality.
- Make block status transitions explicit in tests.

### 7.4 Fallback policy

Current concern:

- `select_consensus()` can still fall back too loosely when useful-work paths are unavailable.

Target configuration:

```yaml
consensus:
  fallback_policy: idle_block_only
  max_idle_blocks: 20
  allow_pow_fallback: false
  emergency_pow_enabled: false
```

Default production policy:

- No silent PoW fallback.
- Use idle/liveness blocks when the task pool is empty.
- Emergency PoW requires explicit operator configuration and logging.

## 8. Verifiable Compute Market Design

### 8.1 Task lifecycle

Target status enum:

```python
class TaskLifecycleStatus(Enum):
    CREATED = "CREATED"
    FUNDED = "FUNDED"
    LISTED = "LISTED"
    ASSIGNED = "ASSIGNED"
    RUNNING = "RUNNING"
    RESULT_SUBMITTED = "RESULT_SUBMITTED"
    CHALLENGE_WINDOW = "CHALLENGE_WINDOW"
    CHALLENGED = "CHALLENGED"
    VERIFIED = "VERIFIED"
    SETTLED = "SETTLED"
    FAILED = "FAILED"
    REFUNDED = "REFUNDED"
```

Required module:

```text
core/task_state_store.py
```

Required tables:

```sql
CREATE TABLE tasks (
    task_id TEXT PRIMARY KEY,
    client_address TEXT NOT NULL,
    task_type TEXT NOT NULL,
    input_commitment TEXT NOT NULL,
    input_uri TEXT NOT NULL,
    budget_main REAL NOT NULL,
    verification_policy TEXT NOT NULL,
    status TEXT NOT NULL,
    created_height INTEGER NOT NULL,
    deadline_height INTEGER NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE task_events (
    event_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    block_height INTEGER,
    created_at REAL NOT NULL
);
```

### 8.2 Budget locking and settlement

Required module:

```text
core/reward_ledger.py
```

Required APIs:

```python
lock_task_budget(task_id, client_address, amount)
lock_worker_bond(task_id, worker_address, amount)
release_task_reward(task_id, worker_address, amount)
slash_worker_bond(task_id, worker_address, amount, reason)
refund_task_budget(task_id, client_address, amount)
```

Rules:

- `FUNDED` requires locked client budget.
- `ASSIGNED` requires locked worker bond.
- `SETTLED` requires block finality.
- Failed or challenged tasks must resolve budget and bonds through explicit settlement events.

### 8.3 Quotes and assignment

Required market flow:

```text
worker_register
worker_submitResourceDeclaration
market_submitQuote
market_listQuotes
market_acceptQuote
worker_getAssignments
```

Quote structure:

```python
@dataclass
class ComputeQuote:
    quote_id: str
    task_type: str
    worker_id: str
    worker_address: str
    price_main: float
    estimated_duration_seconds: int
    hardware_class: str
    reputation_score: float
    expires_at: float
    signature: str
```

### 8.4 Result proof and challenge

Minimum proof structure:

```python
@dataclass
class TaskResultProof:
    task_id: str
    worker_id: str
    input_commitment: str
    result_commitment: str
    output_uri: str
    proof_type: str
    proof_payload_hash: str
    submitted_height: int
    worker_signature: str
```

Proof statuses:

```python
class ProofStatus(Enum):
    VALID = "VALID"
    PENDING_CHALLENGE = "PENDING_CHALLENGE"
    INVALID = "INVALID"
```

Challenge rules:

- Only `CHALLENGE_WINDOW` tasks can be challenged.
- Challengers must lock a challenge bond.
- Successful challenge slashes worker bond.
- Failed challenge slashes challenge bond.

## 9. RPC/API Delivery List

### 9.1 Client RPCs

```text
task_create
task_fund
task_list
task_get
task_cancel
task_downloadResult
task_grantWorkerKey
```

### 9.2 Worker RPCs

```text
worker_register
worker_submitResourceDeclaration
market_submitQuote
market_acceptAssignment
task_submitResult
worker_getAssignments
```

### 9.3 Challenge RPCs

```text
task_submitChallenge
task_getChallengeWindow
task_resolveChallenge
task_getProofStatus
```

### 9.4 Chain status RPCs

```text
chain_getConsensusStatus
chain_getFinalizedHeight
chain_getTaskRoot
chain_getStateRoot
```

## 10. Frontend Delivery

### 10.1 Dashboard

Must show:

- current height
- finalized height
- consensus engine
- fallback policy
- pending tasks
- pending challenges
- current proposer
- last finalized hash

### 10.2 Task market page

Must show:

- task lifecycle state
- budget lock status
- assigned worker
- proof status
- challenge window remaining
- settlement state

### 10.3 Worker page

Must support:

- worker registration
- resource declaration
- quote submission
- assignment view
- result/proof submission

### 10.4 Explorer

Must support search/filter by:

- block status
- task id
- proof status
- settlement status
- worker address

## 11. Tests

Implemented:

```text
tests/test_canonical_serialization.py
tests/test_proposer_selection.py
tests/test_task_encryption.py
```

Required next tests:

```text
tests/test_finality_state_machine.py
tests/test_consensus_fallback_policy.py
tests/integration/test_consensus_status_rpc.py
tests/integration/test_verifiable_compute_market_e2e.py
tests/security/test_task_data_not_on_chain.py
tests/security/test_rpc_permission_matrix.py
tests/security/test_secret_redaction.py
tests/security/test_production_mode_fail_closed.py
tests/security/test_sandbox_isolation.py
tests/security/test_task_key_grant.py
```

## 12. Implementation Roadmap

### Sprint 1: stabilize protocol foundations

1. Keep `core/serialization.py` and migrate protocol hashes to it.
2. Keep `core/proposer_selection.py` as the only short-term proposer selector.
3. Add `get_finalized_height()`.
4. Add `chain_getConsensusStatus`.

### Sprint 2: privacy integration

1. Integrate `core/task_encryption.py` with `task_create`.
2. Add `task_grantWorkerKey`.
3. Ensure task inputs/outputs never enter block or event storage as plaintext.
4. Add owner-only result download checks.

### Sprint 3: task lifecycle MVP

1. Add `TaskStateStore`.
2. Add durable task lifecycle events.
3. Connect market quote acceptance to `ASSIGNED`.
4. Connect result submission to `CHALLENGE_WINDOW`.

### Sprint 4: settlement and finality

1. Add `RewardLedger`.
2. Lock task budgets and worker bonds.
3. Release rewards only after finality.
4. Add challenge resolution and slashing events.

### Sprint 5: frontend productization

1. Add lifecycle timeline in task detail.
2. Add worker assignment and proof submission UI.
3. Add consensus status dashboard.
4. Add explorer filters for task/proof/settlement.

## 13. Minimum Delivery Criteria

Consensus foundation:

1. Repository compiles.
2. Production entry uses one consensus engine.
3. Proposer selection is deterministic and does not touch global random state.
4. Block hashes use canonical serialization.
5. Finalized height is queryable.
6. Rewards are not released before finality.
7. Default production config does not silently fall back to PoW.

Verifiable compute market MVP:

1. Clients can create encrypted tasks.
2. Task budget can be locked.
3. Workers can quote and accept tasks.
4. Workers submit result commitments and proof metadata.
5. Challenge window exists.
6. Settlement waits for finality.
7. Worker reputation changes after task outcome.
8. Explorer shows task, proof, challenge, and settlement state.
9. Frontend does not mock core task lifecycle state.

Security and privacy:

1. Raw task input/output is not stored on-chain.
2. Worker data access requires a key grant.
3. Owner-only downloads are enforced.
4. Secrets are redacted from logs.
5. Simulated crypto/TEE cannot pass as production proof.
6. Sandbox execution is isolated by default.
