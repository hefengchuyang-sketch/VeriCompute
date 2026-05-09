# maincoin 可验证算力市场技术改造执行文档

审查日期：2026-05-09  
目标读者：后端/协议/前端开发者  
审查范围：`main.py`、`core/consensus.py`、`core/unified_consensus.py`、`core/dual_layer_consensus.py`、`core/pouw_chain_v3.py`、`core/utxo_store.py`、`core/rpc/server.py`、`frontend/src/*`

## 1. 总体结论

当前项目最大的问题不是功能少，而是**生产共识路径、实验共识路径、文档描述三者不一致**。

现在 `main.py` 实际使用 `core.consensus.ConsensusEngine`。`core/unified_consensus.py` 自己标注了未集成，`core/dual_layer_consensus.py` 和 `core/pouw_chain_v3.py` 又各自实现了一套 Layer1/Layer2 模型。程序员后续开发时很难判断“到底哪一套规则才算链上规则”。

本文件建议先做一次小型架构收敛：**保留一个生产共识入口，把其他共识原型降级为实验模块或迁移参考；再在生产入口中补齐 proposer 选择、区块状态机、finality、PoUW 证明边界和 fallback 规则。**

同时，本项目最有竞争力的方向不应是“又一条通用链”，而应明确定位为：

> 面向 AI/通用计算任务的 PoUW 可验证算力市场与结算链。

也就是说，链的核心价值不是转账本身，而是把“用户提交任务 -> 矿工/算力节点执行 -> 系统验证结果 -> 链上结算和声誉沉淀”做成可信闭环。

## 2. 产品与协议定位

### 2.1 一句话定位

Maincoin 应定位为：

```text
一个用区块链结算、用 PoUW/TEE/ZK/挑战机制验证、用市场撮合分配的去中心化算力任务网络。
```

不要把产品讲成普通公链。更准确的表达是：

- 对用户：提交计算任务，获得可验证结果。
- 对矿工：贡献算力，完成真实任务，获得奖励。
- 对协议：把真实计算贡献、任务质量、验证结果、结算记录写入链上。
- 对生态：形成算力供给、任务需求、验证者、仲裁者、治理者的闭环。

### 2.2 核心闭环

MVP 必须跑通以下流程：

```text
用户创建任务
  -> 锁定预算
  -> 任务进入市场
  -> 节点接单
  -> 节点执行任务
  -> 提交 result_commitment 和 proof
  -> 系统进入挑战期
  -> 无挑战或挑战失败
  -> 区块 finality 后释放奖励
  -> 更新矿工声誉和任务记录
```

这条链路优先级高于新增更多页面、更多币种、更多治理功能。

### 2.3 竞争优势应围绕四件事打造

| 优势方向 | 要证明什么 | 对应工程交付 |
|---|---|---|
| 真实有用工作 | 矿工不是空转挖矿，而是在执行任务 | PoUW proof、task_root、任务结果验证 |
| 可验证结算 | 用户能知道任务结果是否可信 | challenge window、TEE/ZK/sample 验证 |
| 市场撮合 | 算力节点和用户可以形成订单市场 | orderbook、quote、matching、settlement |
| 链上声誉 | 好节点长期获得更多任务和更低押金 | reputation、slashing、任务完成率 |

## 3. 目标系统边界

### 3.1 必须保留在链上的状态

以下状态必须链上可审计：

| 状态 | 说明 | 最低字段 |
|---|---|---|
| `TaskOrder` | 用户提交的计算订单 | `task_id`、`client`、`budget`、`input_commitment`、`deadline` |
| `TaskAssignment` | 谁接了单 | `task_id`、`worker_id`、`worker_bond`、`assigned_at` |
| `TaskResult` | 执行结果承诺 | `task_id`、`result_commitment`、`proof_type`、`proof_hash` |
| `Challenge` | 挑战记录 | `challenge_id`、`task_id`、`challenger`、`evidence_hash` |
| `Settlement` | 结算记录 | `task_id`、`worker_reward`、`validator_reward`、`slashed_amount` |
| `ReputationDelta` | 声誉变化 | `worker_id`、`reason`、`delta`、`reference_id` |

### 3.2 不应直接塞进链上的内容

以下内容不应直接进区块主体：

- 原始用户数据
- 大模型权重
- 大文件输入
- 原始运行日志
- 完整输出文件

处理方式：

- 大数据走对象存储/IPFS/本地加密存储。
- 链上只放 commitment/hash/CID。
- 权限控制由加密密钥和访问授权管理。

### 3.3 核心角色

| 角色 | 职责 | 需要的模块 |
|---|---|---|
| Client | 发布任务、锁定预算、接收结果 | `TaskStateStore`、RPC、前端任务页 |
| Worker/Miner | 报价、接单、执行、提交 proof | `compute_market_v3.py`、PoUW executor |
| Validator | 出块、验证区块、finality | `consensus.py`、`finality.py` |
| Challenger | 挑战错误结果 | challenge module、仲裁 |
| Arbitrator | 处理复杂争议 | `arbitration.py` |
| Treasury/Governance | 参数治理和资金池 | `dao_treasury.py`、治理模块 |

## 4. 程序格式与代码规范

本项目后续要避免继续变成“功能堆叠仓库”，必须先统一代码格式、命名、模块边界和数据结构格式。

### 4.1 Python 代码格式

统一要求：

| 项 | 规范 |
|---|---|
| Python 版本 | Python 3.11+ |
| 文件编码 | UTF-8 |
| 缩进 | 4 spaces |
| 最大行宽 | 建议 100 |
| 类型标注 | 新增公共函数必须写类型标注 |
| 数据结构 | 协议对象优先使用 `@dataclass` |
| 时间字段 | 链上状态优先使用 block height，日志/展示才使用 timestamp |
| 金额字段 | 长期账本禁止使用 `float`，改用整数最小单位 |

建议新增：

```text
pyproject.toml
```

建议内容：

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

执行命令：

```powershell
py -3 -m ruff check core api tests
py -3 -m black core api tests
py -3 -m compileall -q core api main.py
```

验收标准：

- 新增代码能通过 `ruff`、`black`、`compileall`。
- 不再出现乱码注释、残留测试代码、无边界字符串拼接 hash。

### 4.2 命名规范

| 对象 | 命名 |
|---|---|
| Python 文件 | `snake_case.py` |
| Python 类 | `PascalCase` |
| Python 函数 | `snake_case` |
| 常量 | `UPPER_SNAKE_CASE` |
| 数据库表 | `snake_case` |
| RPC 方法 | `domain_actionObject`，例如 `task_create`、`market_submitQuote` |
| 前端组件 | `PascalCase.tsx` |
| 前端 API 文件 | 按 domain 分组，例如 `task.ts`、`market.ts` |

所有状态枚举使用大写字符串：

```python
class TaskLifecycleStatus(Enum):
    CREATED = "CREATED"
    FUNDED = "FUNDED"
    LISTED = "LISTED"
```

禁止混用 `pending`、`PENDING`、`Pending`。

### 4.3 协议对象格式

所有需要上链、签名、hash、写入事件日志的对象，必须提供：

```python
def to_dict(self) -> dict:
    ...

@classmethod
def from_dict(cls, data: dict):
    ...

def canonical_hash(self) -> str:
    ...
```

禁止：

```python
hashlib.sha256(f"{a}{b}{c}".encode()).hexdigest()
```

统一使用：

```python
hash_canonical({
    "field_a": a,
    "field_b": b,
    "field_c": c,
})
```

### 4.4 错误返回格式

内部服务函数统一使用结果对象：

```python
@dataclass
class ServiceResult(Generic[T]):
    ok: bool
    message: str
    data: Optional[T] = None
    error_code: str = ""
```

RPC 层负责把 `ServiceResult` 转成 JSON-RPC response。`core` 模块不要直接拼 RPC response。

### 4.5 日志格式

生产日志必须结构化：

```python
logger.info(
    "task_state_changed",
    extra={
        "task_id": task_id,
        "from_status": old_status,
        "to_status": new_status,
        "block_height": height,
    },
)
```

禁止：

- 大量 emoji 日志进入生产路径。
- 捕获 `Exception` 后静默 `pass`。
- 核心账本逻辑只 `print()` 不记录日志。

### 4.6 前端格式

前端统一：

| 项 | 规范 |
|---|---|
| 语言 | TypeScript |
| 页面 | `src/pages/` |
| 可复用组件 | `src/components/` |
| API 调用 | `src/api/` |
| 类型定义 | `src/types/` |
| 状态管理 | `src/store/` |

建议目录：

```text
frontend/src/
  api/
    chain.ts
    task.ts
    market.ts
    worker.ts
  components/
  pages/
  store/
  types/
    chain.ts
    task.ts
    market.ts
```

验收标准：

- 页面不能直接写 fetch 细节，统一通过 `src/api/*`。
- 任务状态、proof 状态、区块状态与后端枚举一致。
- 核心页面不得使用 mock 数据冒充链上状态。

## 5. 文件夹整理与模块分布

### 5.1 整理原则

当前仓库中生产代码、实验代码、历史文档、演示材料、运行数据混在一起。整理目标：

1. 生产代码清晰。
2. 实验原型隔离。
3. 文档按用途分类。
4. 运行数据不进入源码目录。
5. 测试与脚本可被 CI 稳定调用。

### 5.2 目标目录结构

建议最终整理为：

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
      __init__.py
      engine.py
      block.py
      finality.py
      proposer_selection.py
      serialization.py
      rewards.py
    compute/
      __init__.py
      market.py
      scheduler.py
      pouw_executor.py
      pouw_proof.py
      task_state_store.py
      task_challenge.py
    ledger/
      __init__.py
      utxo_store.py
      transaction.py
      wallet.py
      reward_ledger.py
    security/
      __init__.py
      crypto.py
      crypto_utils.py
      encrypted_task.py
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

目录职责：

- `core/consensus/`：底层共识、区块、finality、proposer 选择。
- `core/compute/`：可验证算力市场、任务状态机、PoUW proof、挑战机制。
- `core/ledger/`：UTXO、交易、钱包、奖励账本。
- `core/security/`：密码学、加密任务、TEE/ZK。
- `core/experimental/`：未接入生产路径的原型。
- `runtime/`：本地运行数据，必须加入 `.gitignore`。

### 5.3 当前文件迁移映射

| 当前文件 | 目标位置 | 处理方式 |
|---|---|---|
| `core/consensus.py` | `core/consensus/engine.py` | 拆分为 engine/block/finality/rewards |
| `core/pouw_block_types.py` | `core/consensus/block.py` | 保留区块类型定义 |
| `core/pouw_executor.py` | `core/compute/pouw_executor.py` | 移动 |
| `core/pouw_scoring.py` | `core/compute/pouw_scoring.py` | 移动 |
| `core/compute_market_v3.py` | `core/compute/market.py` | 收敛为主市场模块 |
| `core/utxo_store.py` | `core/ledger/utxo_store.py` | 移动并保留兼容 import |
| `core/transaction.py` | `core/ledger/transaction.py` | 移动 |
| `core/wallet.py` | `core/ledger/wallet.py` | 移动 |
| `core/crypto.py` | `core/security/crypto.py` | 移动 |
| `core/crypto_utils.py` | `core/security/crypto_utils.py` | 移动 |
| `core/unified_consensus.py` | `core/experimental/unified_consensus.py` | 标记实验 |
| `core/dual_layer_consensus.py` | `core/experimental/dual_layer_consensus.py` | 标记实验 |
| `core/pouw_chain_v3.py` | `core/experimental/pouw_chain_v3.py` | 标记实验 |

### 5.4 分阶段迁移方式

不要一次性大搬家，按三阶段迁移。

阶段 A：只加新目录和兼容 import。

```python
# core/ledger/utxo_store.py
from core.utxo_store import *
```

阶段 B：移动实现，旧路径变 shim。

```python
# core/utxo_store.py
from core.ledger.utxo_store import *
```

阶段 C：删除旧 shim。

删除条件：

- `rg "from core.utxo_store|import core.utxo_store"` 找不到生产引用。
- 测试全部通过。
- 文档已更新。

### 5.5 文档目录整理

建议：

```text
docs/
  architecture/
    consensus.md
    verifiable_compute_market.md
    ledger.md
    rpc.md
  audits/
    security_audit.md
    design_audit.md
    code_review.md
  product/
    positioning.md
    user_flows.md
    frontend_spec.md
  operations/
    deployment.md
    production_checklist.md
    monitoring.md
  reports/
    validation/
    benchmarks/
```

迁移规则：

- 审计报告放 `docs/audits/`。
- 面向用户/投资人的产品材料放 `docs/product/`。
- 技术架构文档放 `docs/architecture/`。
- 验证结果和 benchmark 放 `docs/reports/`。
- 部署、监控、运维放 `docs/operations/`。

### 5.6 运行数据目录整理

统一运行数据目录：

```text
runtime/
  data/
    node1/
    node2/
  wallets/
    node1/
    node2/
  logs/
```

配置项：

```yaml
runtime:
  base_dir: runtime
  data_dir: runtime/data/node1
  wallet_dir: runtime/wallets/node1
  log_dir: runtime/logs
```

`.gitignore` 必须包含：

```text
runtime/
data/
data_peer*/
wallets/
wallets_peer*/
logs/
*.db
*.sqlite
```

### 5.7 scripts 目录整理

建议：

```text
scripts/
  dev/
    compile_check.py
    start_local_node.ps1
  ops/
    deploy_to_servers.ps1
    update_and_restart.py
  validation/
    run_public_dataset_demo.py
    run_adversarial_access_tests.py
    benchmark_sbox_score.py
```

规则：

- 开发辅助脚本放 `scripts/dev/`。
- 运维部署脚本放 `scripts/ops/`。
- 验证/benchmark 脚本放 `scripts/validation/`。
- 临时脚本放 `scripts/scratch/`，不允许生产文档引用。

### 5.8 整理验收标准

完成目录整理后必须满足：

```powershell
py -3 -m compileall -q core api main.py
py -3 -m pytest tests
```

并且：

- `main.py` 仍能启动。
- 前端 build 不受影响。
- 旧 import 至少在一个版本周期内兼容。
- `core/experimental/` 不被生产入口导入。
- `runtime/` 数据不进入 git。

## 6. 安全与隐私设计

Maincoin 的核心方向是“可验证算力市场”。这意味着系统会处理用户任务输入、执行结果、算力节点身份、支付结算和争议证据。安全与隐私设计必须围绕一个原则展开：

> 链上只记录可验证承诺和状态，不暴露原始任务数据、用户隐私数据、密钥材料和完整执行输出。

### 6.1 安全目标

| 目标 | 含义 | 工程要求 |
|---|---|---|
| 数据保密 | worker/validator 不能随意读取用户原始任务数据 | 任务输入加密，链上只放 commitment |
| 结果可验证 | 用户能判断结果是否由指定任务产生 | result commitment、proof、challenge window |
| 身份可追责 | worker、client、challenger 行为可审计 | 地址、公钥、签名、事件日志 |
| 支付安全 | 预算、押金、奖励不能被重复花费 | UTXO/RewardLedger 原子锁定与释放 |
| 最小披露 | 链上不放原始数据和敏感日志 | CID/hash/commitment 替代明文 |
| 生产隔离 | 开发模式、模拟模式不能进入生产路径 | 启动前安全基线检查 |

### 6.2 隐私分层

建议把隐私保护分为四层：

```text
Layer A: 数据传输隐私
  - TLS
  - API key / bearer token
  - 请求体大小限制

Layer B: 任务数据隐私
  - client-side encryption
  - input_commitment
  - encrypted input URI

Layer C: 执行环境隐私
  - Docker sandbox
  - TEE optional
  - no network by default
  - resource limits

Layer D: 链上隐私
  - hash/CID only
  - no raw input/output
  - no secret in event logs
```

### 6.3 任务数据加密方案

任务输入必须在客户端侧加密，worker 只在被授权后拿到解密材料。

建议任务创建流程：

```text
client 生成 data_key
  -> 用 data_key 加密 input payload
  -> 上传 encrypted payload 到对象存储/IPFS/local storage
  -> 计算 input_commitment = sha256(original_input)
  -> 计算 encrypted_payload_hash = sha256(encrypted_payload)
  -> 链上写入 input_commitment、encrypted_payload_hash、input_uri
  -> worker 接单后，client 为 worker 公钥加密 data_key
```

新增数据结构：

```python
@dataclass
class EncryptedTaskInput:
    task_id: str
    input_uri: str
    input_commitment: str
    encrypted_payload_hash: str
    encryption_scheme: str  # AES_256_GCM
    key_exchange_scheme: str  # X25519 or secp256k1_ecdh
    client_public_key: str
```

worker 授权密钥：

```python
@dataclass
class WorkerKeyGrant:
    task_id: str
    worker_id: str
    worker_public_key: str
    encrypted_data_key: str
    grant_signature: str
    expires_height: int
```

执行任务：

1. 新增 `core/security/task_encryption.py`。
2. `task_create` 不接收原始明文，只接收 `input_uri` 和 commitment。
3. `task_grantWorkerKey` 负责给接单 worker 发放加密后的 data key。
4. worker 只能拿到自己公钥可解的 data key。

验收标准：

- 链数据库中不能查到原始输入。
- 日志中不能出现原始输入。
- 没有 key grant 的 worker 不能解密任务。

### 6.4 输出结果隐私

worker 输出也不应直接上链。

结果提交结构：

```python
@dataclass
class EncryptedTaskOutput:
    task_id: str
    output_uri: str
    result_commitment: str
    encrypted_output_hash: str
    encryption_scheme: str
    recipient_public_key: str
```

规则：

1. 原始输出加密后存储。
2. 链上只放 `result_commitment`、`encrypted_output_hash`、`output_uri`。
3. 用户下载结果后本地解密。
4. 如需挑战，挑战者只能访问必要的可验证证据，不默认访问完整原始数据。

验收标准：

- Explorer 不显示明文输出。
- RPC 默认不返回明文输出。
- `task_downloadResult` 只对任务 owner 或授权 reviewer 开放。

### 6.5 TEE / ZK / Challenge 的隐私边界

三种验证方式不要混成一个概念，应明确各自解决的问题。

| 验证方式 | 解决什么 | 不解决什么 | MVP 优先级 |
|---|---|---|---|
| Challenge Game | 通过争议期发现错误结果 | 不自动保护输入隐私 | 必须 |
| Sampling | 对部分可复算任务抽样校验 | 不适合所有 AI 推理任务 | 必须 |
| TEE Attestation | 证明代码在可信环境中执行 | 依赖硬件和证明服务 | 可选 |
| ZK Proof | 证明计算正确性且少披露 | 开发复杂、成本高 | 后续 |

MVP 建议：

```text
默认：commitment + sampling + challenge window
增强：TEE attestation
未来：ZK proof
```

TEE proof 最小字段：

```python
@dataclass
class TEEAttestationRecord:
    task_id: str
    worker_id: str
    measurement_hash: str
    enclave_public_key: str
    quote_hash: str
    verifier_id: str
    verified_at_height: int
    expires_height: int
```

验收标准：

- 没有真实 attestation verifier 时，TEE 状态只能是 `UNVERIFIED` 或 `SIMULATED`。
- 生产模式禁止把 `SIMULATED` TEE 当作有效 proof。

### 6.6 访问控制与权限模型

RPC 权限按角色划分：

| 角色 | 能做什么 |
|---|---|
| guest | 查询公开链状态、公开任务列表 |
| client | 创建任务、锁定预算、下载自己的结果 |
| worker | 注册、报价、接单、提交结果 |
| challenger | 提交挑战、查看公开证据 |
| validator | 提交验证记录、参与 finality |
| admin | 运维、参数更新、紧急暂停 |

建议新增权限枚举：

```python
class Permission(Enum):
    PUBLIC_READ = "PUBLIC_READ"
    TASK_CREATE = "TASK_CREATE"
    TASK_OWNER_READ = "TASK_OWNER_READ"
    WORKER_WRITE = "WORKER_WRITE"
    CHALLENGE_WRITE = "CHALLENGE_WRITE"
    VALIDATOR_WRITE = "VALIDATOR_WRITE"
    ADMIN_WRITE = "ADMIN_WRITE"
```

RPC handler 必须显式声明权限：

```python
register(
    method_name="task_downloadResult",
    handler=self.task_download_result,
    permission=Permission.TASK_OWNER_READ,
)
```

验收标准：

- 写操作必须认证。
- owner-only 数据必须校验 `auth_context.user_address`。
- 本地请求不能默认等于 admin，除非开发模式显式开启。

### 6.7 密钥管理

密钥类型：

| 密钥 | 用途 | 存储要求 |
|---|---|---|
| wallet private key | 资产签名 | 加密 keystore |
| API admin key | RPC 管理 | 环境变量/secret manager |
| task data key | 单任务数据加密 | 不上链，只加密分发 |
| worker key pair | 接收任务密钥 | 本地 keystore |
| TEE key | attestation 或 enclave 通信 | 硬件/受保护存储 |

规则：

1. 私钥不得写入日志。
2. 明文助记词不得落盘。
3. 任务 data key 不得进入区块、数据库事件、RPC 响应。
4. 生产环境必须设置固定 admin key，不允许自动生成后长期使用。

启动前检查：

```text
MAINCOIN_PRODUCTION=true 时必须检查：
  - POUW_ADMIN_KEY 或 MAINCOIN_ADMIN_KEY 存在
  - REQUIRE_LOCAL_AUTH=true
  - ALLOW_AUTH_USER_OVERRIDE=false
  - 加密库 cryptography/ecdsa/mnemonic 可用
  - 禁止 XOR fallback
  - 禁止 simulated TEE 作为有效证明
```

### 6.8 日志与隐私脱敏

禁止记录：

- mnemonic
- private key
- data key
- encrypted_data_key 明文解密结果
- 原始任务输入
- 原始任务输出
- Authorization header
- API key

日志脱敏函数：

```python
def redact_secret(value: str, prefix: int = 6, suffix: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= prefix + suffix:
        return "***"
    return f"{value[:prefix]}...{value[-suffix:]}"
```

验收标准：

- 搜索 `mnemonic`、`private_key`、`data_key` 不应出现在日志输出路径。
- 安全测试应检查敏感字段不会被 RPC error 返回。

### 6.9 数据保留与删除策略

任务数据需要生命周期管理。

建议状态：

```python
class DataRetentionStatus(Enum):
    ACTIVE = "ACTIVE"
    RESULT_DELIVERED = "RESULT_DELIVERED"
    RETENTION_EXPIRED = "RETENTION_EXPIRED"
    DELETION_REQUESTED = "DELETION_REQUESTED"
    DELETED = "DELETED"
```

规则：

1. 原始加密输入在任务结算后保留固定周期。
2. 用户可请求删除加密 payload。
3. 链上 commitment 不删除，只保留审计最小信息。
4. 删除操作写入 `data_deletion_events`。

验收标准：

- 删除数据不破坏链上审计。
- 删除后不能再下载原始 payload。
- Explorer 仍能看到任务状态和 commitment。

### 6.10 网络与沙箱安全

worker 执行任务必须默认隔离：

| 项 | 默认策略 |
|---|---|
| 网络访问 | 默认关闭 |
| 文件系统 | 只读 rootfs + 临时工作目录 |
| CPU/GPU | 限额 |
| 内存 | 限额 |
| 超时 | 强制 kill |
| 输出 | 只允许写到指定目录 |

执行任务：

1. `sandbox_executor.py` 生产模式必须使用 Docker/容器隔离。
2. 无 Docker 时只能进入开发模拟模式。
3. 生产模式中 `simulate=True` 必须拒绝。
4. 任务代码执行前做 allowlist/denylist 检查。

验收标准：

- 任务不能访问宿主机钱包目录。
- 任务不能默认访问网络。
- 超时任务会失败并释放/惩罚状态。

### 6.11 安全测试清单

新增测试：

```text
tests/security/test_task_data_not_on_chain.py
tests/security/test_rpc_permission_matrix.py
tests/security/test_secret_redaction.py
tests/security/test_production_mode_fail_closed.py
tests/security/test_sandbox_isolation.py
tests/security/test_task_key_grant.py
```

必须覆盖：

1. 创建任务后，数据库和区块中没有原始输入。
2. 非 owner 不能下载结果。
3. 未认证用户不能调用写 RPC。
4. `MAINCOIN_PRODUCTION=true` 时缺少 admin key 拒绝启动。
5. simulated TEE 不能在生产模式通过。
6. 日志不泄露 API key、private key、data key。

### 6.12 安全验收标准

项目进入可验证算力市场 MVP 前，安全隐私至少满足：

1. 任务输入和输出不上链，只上 commitment/hash/CID。
2. 任务数据使用 client-side encryption。
3. worker 通过 key grant 获取任务解密能力。
4. RPC 权限按角色控制。
5. owner-only 数据不能被其他地址读取。
6. 生产模式 fail-closed。
7. 日志脱敏。
8. sandbox 默认隔离网络、文件系统和资源。
9. simulated crypto/TEE 不能作为生产证明。
10. 有安全测试覆盖上述规则。

## 7. 立即修复项

### 7.1 修复编译错误

问题位置：`core/main_transfer.py:691-696`

现象：

```powershell
py -3 -m compileall -q core api main.py
```

会失败，错误为：

```text
IndentationError: unexpected indent (main_transfer.py, line 691)
```

执行方式：

1. 删除 `core/main_transfer.py:691-696` 的残留缩进代码。
2. 如果这段代码是手工测试逻辑，迁移到 `tests/test_main_transfer.py`。
3. 执行 `py -3 -m compileall -q core api main.py`。

验收标准：

- `compileall` 通过。
- 不新增运行时副作用。

### 7.2 固定本机 Python 启动方式

本机 `python` 当前指向 MySQL Workbench 自带解释器，无法加载标准库 `encodings`。开发/CI 脚本建议统一使用：

```powershell
py -3 -m pytest
py -3 -m compileall -q core api main.py
```

执行方式：

1. 修改 README/开发脚本中所有裸 `python` 为 `py -3`，或显式使用 `.venv\Scripts\python.exe`。
2. 在 CI 中打印 `sys.executable` 和 `sys.version`。

验收标准：

- 编译、测试、脚本运行使用同一个 Python 解释器。

## 8. 生产共识入口收敛

### 8.1 当前状态

| 模块 | 当前用途 | 建议处理 |
|---|---|---|
| `core/consensus.py` | `main.py` 实际使用 | 保留为短期生产入口 |
| `core/unified_consensus.py` | 标注未集成 | 保留为参考，禁止生产导入 |
| `core/dual_layer_consensus.py` | Layer1/Layer2 原型 | 迁移到 `core/experimental/` |
| `core/pouw_chain_v3.py` | V3 完整原型 | 迁移到 `core/experimental/` 或拆出可复用结构 |

### 8.2 执行任务

任务 A：标记生产入口。

- 在 `core/consensus.py` 顶部文档写明：这是当前唯一生产共识入口。
- 在 `main.py:_init_consensus()` 加注释，禁止切换到未集成引擎。

任务 B：隔离实验模块。

- 新建目录：`core/experimental/`
- 将 `dual_layer_consensus.py`、`pouw_chain_v3.py` 迁移或在文件顶部加明确警告：
  - `EXPERIMENTAL_ONLY = True`
  - 不允许被 `main.py`、`rpc_service.py` 直接导入

任务 C：加导入保护测试。

新增测试：`tests/test_production_consensus_entrypoint.py`

测试内容：

```python
def test_main_uses_single_consensus_engine():
    import inspect
    import main
    source = inspect.getsource(main.MainCoinNode._init_consensus)
    assert "from core.consensus import ConsensusEngine" in source
    assert "UnifiedConsensus" not in source
    assert "POUWChainV3" not in source
```

验收标准：

- 生产启动路径只有一个共识引擎。
- 实验模块不能被生产入口意外导入。

## 9. 共识机制设计

### 9.1 分层目标

建议采用“两层但不互相替代”的结构：

```text
Layer 1: 安全共识层
  - proposer 选择
  - 区块合法性验证
  - 区块状态流转
  - finality
  - slashing

Layer 2: PoUW 有用工作层
  - 任务提交
  - 任务执行证明
  - 结果挑战
  - 任务奖励计算
  - 任务声誉
```

核心原则：

- PoUW 可以影响奖励和任务声誉。
- PoUW 不应该单独决定底层链安全。
- 出块权应由 Layer 1 的验证者/矿工选择规则决定。
- 任务失败不应拖垮区块链 liveness。

### 9.2 区块生命周期

新增或明确区块状态：

```python
class BlockStatus(Enum):
    PROPOSED = "PROPOSED"
    ACCEPTED = "ACCEPTED"
    FINALIZED = "FINALIZED"
    REJECTED = "REJECTED"
    ORPHANED = "ORPHANED"
```

状态流转：

```text
create_block()
  -> PROPOSED

validate_block()
  -> ACCEPTED 或 REJECTED

finalize_block()
  -> FINALIZED

reorg_detected()
  -> ORPHANED
```

执行任务：

1. 在 `core/consensus.py` 中把 `BlockStatus.PENDING` 改为 `PROPOSED`，或至少新增兼容映射。
2. `mine_block()` 只负责产出 `PROPOSED` block。
3. `add_block()` 负责从 `PROPOSED` 到 `ACCEPTED`。
4. 新增 `finalize_blocks()`，按确认深度或 BFT 投票规则把块推进到 `FINALIZED`。
5. 奖励发放、UTXO coinbase 成熟、PoUW 奖励结算只允许读取 `FINALIZED` block。

建议接口：

```python
def validate_block(self, block: Block) -> tuple[bool, str]:
    ...

def accept_block(self, block: Block) -> tuple[bool, str]:
    ...

def finalize_blocks(self) -> list[Block]:
    ...

def get_finalized_height(self) -> int:
    ...
```

验收标准：

- `mine_block()` 不直接意味着奖励可花费。
- `add_block()` 不直接意味着 finality。
- `get_finalized_height()` 可被 RPC 和前端展示。

### 9.3 proposer 选择机制

当前问题：`random.seed()` 不是 VRF，会污染全局随机状态，且可预测。

短期可执行版本：先实现确定性、局部、可重放的选择器，不再污染全局随机。

新增文件建议：`core/proposer_selection.py`

接口：

```python
@dataclass(frozen=True)
class ProposerCandidate:
    node_id: str
    address: str
    weight: int

@dataclass(frozen=True)
class ProposerSelectionResult:
    selected_node_id: str
    seed: str
    score: int
    total_weight: int

def select_weighted_proposer(
    candidates: list[ProposerCandidate],
    height: int,
    epoch_seed: str,
    parent_hash: str,
) -> ProposerSelectionResult:
    ...
```

算法要求：

1. 输入必须包含 `height`、`epoch_seed`、`parent_hash`、候选者列表。
2. 候选者排序必须固定：按 `node_id` 升序。
3. 不使用 `random.seed()`。
4. 用 `sha256(seed_material)` 产生局部随机数。
5. 所有节点给定同一输入必须选出同一 proposer。

伪代码：

```python
seed_material = f"{height}:{epoch_seed}:{parent_hash}".encode()
draw = int(hashlib.sha256(seed_material).hexdigest(), 16) % total_weight

cumsum = 0
for candidate in sorted(candidates, key=lambda c: c.node_id):
    cumsum += candidate.weight
    if draw < cumsum:
        return candidate
```

中期版本：替换为真正 VRF。

VRF 设计要求：

- proposer 对 `(height, epoch_seed, parent_hash)` 签名或生成 VRF proof。
- 区块头包含 `vrf_public_key`、`vrf_proof`、`vrf_output`。
- `validate_block()` 必须验证 proof。

验收测试：

```python
def test_selection_is_deterministic():
    result1 = select_weighted_proposer(candidates, 100, seed, parent)
    result2 = select_weighted_proposer(candidates, 100, seed, parent)
    assert result1 == result2

def test_selection_does_not_touch_global_random():
    import random
    random.seed(123)
    before = random.random()
    select_weighted_proposer(candidates, 100, seed, parent)
    random.seed(123)
    assert random.random() == before
```

### 9.4 区块头设计

当前 `Block.compute_hash()` 以字符串拼接生成 hash，字段边界不强。

建议改为 canonical header：

```python
def canonical_block_header(block: Block) -> dict:
    return {
        "version": block.version,
        "height": block.height,
        "prev_hash": block.prev_hash,
        "timestamp": int(block.timestamp),
        "merkle_root": block.merkle_root,
        "state_root": block.state_root,
        "task_root": block.task_root,
        "consensus_type": block.consensus_type.value,
        "proposer": block.miner_id,
        "proposer_address": block.miner_address,
        "difficulty": block.difficulty,
        "nonce": block.nonce,
        "sector": block.sector,
        "block_type": block.block_type,
    }
```

新增工具：

```python
def canonical_json(data: dict) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

def hash_canonical(data: dict) -> str:
    return hashlib.sha256(canonical_json(data)).hexdigest()
```

执行任务：

1. 新增 `core/serialization.py`。
2. `Block.compute_hash()` 改为使用 `hash_canonical(canonical_block_header(self))`。
3. 保留旧 hash 兼容路径，只用于读取旧区块，不用于新区块。

验收标准：

- 同一 block 在不同 Python 版本上 hash 一致。
- 字段边界明确，不依赖字符串拼接。

### 9.5 PoUW 机制边界

PoUW 应当是“任务证明和奖励输入”，不是“唯一出块安全来源”。

建议新增两个 root：

```python
block.task_root      # 本块包含的 PoUW 任务/证明 Merkle root
block.state_root     # 账本状态 root
```

PoUW proof 最小结构：

```python
@dataclass
class PoUWProof:
    task_id: str
    worker_id: str
    input_commitment: str
    result_commitment: str
    execution_commitment: str
    verification_type: str  # tee | zk | challenge | sample
    verifier_set_hash: str
    score: float
    signature: str
```

验证规则：

1. `task_id` 必须存在于任务池或任务状态树。
2. `input_commitment` 必须等于任务提交时的输入承诺。
3. `result_commitment` 必须可通过 TEE/ZK/抽样/挑战验证。
4. `score` 只能影响奖励系数，不能直接绕过 Layer 1 proposer 规则。

执行任务：

- `core/consensus.py:mine_pouw()` 只生成 proof，不直接决定 finality。
- `validate_block()` 中加入 `validate_pouw_proofs(block)`。
- `validate_pouw_proofs()` 返回三态：
  - `VALID`
  - `PENDING_CHALLENGE`
  - `INVALID`

处理方式：

| PoUW 状态 | 区块是否可接受 | 奖励是否可结算 |
|---|---|---|
| `VALID` | 可以 | 可以在 finality 后结算 |
| `PENDING_CHALLENGE` | 可以，但任务奖励冻结 | 不可以 |
| `INVALID` | 区块拒绝或 PoUW 奖励归零 | 不可以 |

### 9.6 fallback 机制

当前 `select_consensus()` 会在任务不足时回退 PoW。建议改成显式策略。

新增配置：

```yaml
consensus:
  fallback_policy: idle_block_only
  max_idle_blocks: 20
  allow_pow_fallback: false
  emergency_pow_enabled: false
```

策略定义：

| 策略 | 行为 | 适用场景 |
|---|---|---|
| `idle_block_only` | 无任务时只出维护块 | 默认生产 |
| `wait_for_tasks` | 无任务时暂停出块 | 任务链/测试 |
| `emergency_pow` | 紧急情况下允许 PoW | 手动运维开关 |

执行任务：

1. 改造 `ConsensusEngine.select_consensus()`。
2. 默认禁止自动 PoW fallback。
3. `emergency_pow` 必须要求配置和日志双重确认。
4. 在 RPC 暴露当前 fallback 状态。

验收标准：

- 任务池为空时不会静默变成 PoW 链。
- 前端能显示当前 fallback 策略。

## 10. 可验证算力市场执行设计

### 10.1 MVP 模块拆分

为了服务“可验证算力市场”方向，建议把 MVP 拆成 8 个明确模块。

| 模块 | 文件建议 | 目标 |
|---|---|---|
| 任务状态机 | `core/task_state_store.py` | 管理任务从创建到结算的生命周期 |
| 算力市场 | `core/compute_market_v3.py` | 管理报价、接单、订单状态 |
| 执行证明 | `core/pouw_proof.py` | 定义 proof 数据结构和验证入口 |
| 挑战机制 | `core/task_challenge.py` | 管理挑战期和证据 |
| 结算账本 | `core/reward_ledger.py` | 冻结、释放、惩罚奖励 |
| 共识 finality | `core/finality.py` | 决定何时释放链上奖励 |
| RPC API | `core/rpc_handlers/task_handler.py` | 对外提供任务市场接口 |
| 前端任务台 | `frontend/src/pages/Tasks.tsx` | 展示任务、接单、验证、结算 |

### 10.2 任务状态机

新增状态：

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

状态流转：

```text
CREATED
  -> FUNDED
  -> LISTED
  -> ASSIGNED
  -> RUNNING
  -> RESULT_SUBMITTED
  -> CHALLENGE_WINDOW
  -> VERIFIED
  -> SETTLED

CHALLENGE_WINDOW
  -> CHALLENGED
  -> VERIFIED 或 FAILED

FAILED
  -> REFUNDED 或重新 LISTED
```

新增数据结构：

```python
@dataclass
class TaskOrder:
    task_id: str
    client_address: str
    task_type: str
    input_commitment: str
    input_uri: str
    budget_main: float
    max_duration_seconds: int
    verification_policy: str
    status: TaskLifecycleStatus
    created_height: int
    deadline_height: int
```

执行任务：

1. 新增 `core/task_state_store.py`。
2. 所有任务状态写入 SQLite。
3. 禁止 RPC handler 用内存 dict 作为唯一状态。
4. 每次状态变化写入 `task_events` 表。

数据库表：

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

验收标准：

- 任意任务都能查询完整状态历史。
- 服务重启后任务状态不丢失。

### 10.3 预算锁定与结算

任务发布时，预算不能只是前端数字，必须进入锁定状态。

新增模块：`core/reward_ledger.py`

核心接口：

```python
def lock_task_budget(task_id: str, client_address: str, amount: float) -> tuple[bool, str]:
    ...

def lock_worker_bond(task_id: str, worker_address: str, amount: float) -> tuple[bool, str]:
    ...

def release_task_reward(task_id: str, worker_address: str, amount: float) -> tuple[bool, str]:
    ...

def slash_worker_bond(task_id: str, worker_address: str, amount: float, reason: str) -> tuple[bool, str]:
    ...

def refund_task_budget(task_id: str, client_address: str, amount: float) -> tuple[bool, str]:
    ...
```

执行规则：

1. `FUNDED` 前必须锁定用户预算。
2. `ASSIGNED` 前必须锁定 worker bond。
3. `SETTLED` 只能在区块 `FINALIZED` 后执行。
4. `FAILED` 时按争议结果退款或罚没。

验收标准：

- 任务预算不能被重复消费。
- worker 未提交结果时可以按规则罚没 bond。
- 未 finality 前不能释放奖励。

### 10.4 算力报价与接单

当前项目已有 `compute_market_v3.py`，建议把它收敛为订单市场主模块。

新增/统一数据结构：

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

接单规则：

1. worker 必须注册。
2. worker 必须提供资源声明。
3. worker 报价必须签名。
4. 用户选择报价后，系统生成 `TaskAssignment`。

RPC：

```text
market_submitQuote
market_listQuotes
market_acceptQuote
market_getOrderbook
```

验收标准：

- 前端能看到真实报价列表。
- 选中报价后任务进入 `ASSIGNED`。
- 报价过期后不能接单。

### 10.5 结果提交与 proof

新增文件：`core/pouw_proof.py`

证明类型：

| 类型 | 说明 | MVP 是否必须 |
|---|---|---|
| `hash_commitment` | 输入/输出 hash 承诺 | 必须 |
| `sample_verification` | 抽样复算 | 必须 |
| `tee_attestation` | TEE 证明 | 可选 |
| `zk_proof` | ZK 证明 | 后续 |
| `challenge_game` | 挑战期验证 | 必须 |

MVP proof：

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

验证入口：

```python
def verify_task_result_proof(task: TaskOrder, proof: TaskResultProof) -> ProofVerificationResult:
    ...
```

结果：

```python
class ProofStatus(Enum):
    VALID = "VALID"
    PENDING_CHALLENGE = "PENDING_CHALLENGE"
    INVALID = "INVALID"
```

验收标准：

- 无 proof 不能进入 `RESULT_SUBMITTED`。
- proof 与 `input_commitment` 不匹配时直接 `INVALID`。
- MVP 中默认进入 `CHALLENGE_WINDOW`，挑战期过后才可结算。

### 10.6 挑战机制

新增文件：`core/task_challenge.py`

挑战流程：

```text
RESULT_SUBMITTED
  -> CHALLENGE_WINDOW
  -> 没有挑战：VERIFIED
  -> 有挑战：CHALLENGED
  -> 仲裁/复算：VERIFIED 或 FAILED
```

挑战数据：

```python
@dataclass
class TaskChallenge:
    challenge_id: str
    task_id: str
    challenger_address: str
    evidence_hash: str
    evidence_uri: str
    challenge_bond: float
    status: str
    created_height: int
```

规则：

1. 只有 `CHALLENGE_WINDOW` 状态能被挑战。
2. 挑战者必须锁定 challenge bond。
3. 挑战成功：worker bond 被罚没，挑战者获得奖励。
4. 挑战失败：challenge bond 被罚没，worker 正常结算。

RPC：

```text
task_submitChallenge
task_getChallenges
task_resolveChallenge
```

验收标准：

- 挑战期内可提交挑战。
- 挑战期结束后不能新增挑战。
- 挑战结果会改变结算路径。

### 10.7 声誉系统

声誉不应参与底层出块安全，但可以影响市场排序和押金比例。

新增接口：

```python
def record_task_success(worker_id: str, task_id: str, score: float) -> None:
    ...

def record_task_failure(worker_id: str, task_id: str, reason: str) -> None:
    ...

def get_worker_reputation(worker_id: str) -> float:
    ...

def get_required_bond(worker_id: str, base_bond: float) -> float:
    ...
```

规则：

- 任务成功提高声誉。
- 挑战失败降低挑战者声誉。
- 挑战成功降低 worker 声誉。
- 声誉只影响排序、押金、接单额度，不影响 Layer 1 proposer 选择。

验收标准：

- 高声誉 worker 在报价列表中排序更靠前。
- 低声誉 worker 需要更高 bond。

## 11. 账本与状态设计

### 11.1 唯一事实源

建议确定一套主状态：

| 状态 | 主模块 | 禁止行为 |
|---|---|---|
| 区块 | `ConsensusEngine`/链数据库 | 其他模块平行写 block |
| UTXO | `core/utxo_store.py` | RPC 直接改余额 |
| 任务 | 新增 `TaskStateStore` | 共识层直接存业务细节 |
| 奖励 | `RewardLedger` | 挖出块时立即可花费 |
| finality | `ConsensusEngine` | 前端/RPC 自己计算 |

### 11.2 建议新增模块

```text
core/
  consensus.py                 # 生产共识入口
  proposer_selection.py         # proposer 选择
  serialization.py              # canonical json/hash
  finality.py                   # finality 状态机
  task_state_store.py           # PoUW 任务状态
  reward_ledger.py              # 奖励冻结/释放
```

最小实现顺序：

1. `serialization.py`
2. `proposer_selection.py`
3. `finality.py`
4. `reward_ledger.py`
5. `task_state_store.py`

## 12. RPC 与 API 交付清单

### 12.1 用户侧 RPC

```text
task_create
task_fund
task_list
task_get
task_cancel
task_downloadResult
```

字段要求：

- `task_create` 必须接收 `task_type`、`input_commitment`、`input_uri`、`budget_main`、`verification_policy`。
- `task_fund` 必须锁定预算。
- `task_get` 必须返回完整 lifecycle 状态。

### 12.2 Worker 侧 RPC

```text
worker_register
worker_submitResourceDeclaration
market_submitQuote
market_acceptAssignment
task_submitResult
worker_getAssignments
```

字段要求：

- worker 注册必须包含地址、公钥、硬件类型。
- 资源声明必须签名。
- result 必须包含 `result_commitment`、`output_uri`、`proof_payload_hash`。

### 12.3 验证/挑战 RPC

```text
task_submitChallenge
task_getChallengeWindow
task_resolveChallenge
task_getProofStatus
```

### 12.4 链状态 RPC

```text
chain_getConsensusStatus
chain_getFinalizedHeight
chain_getTaskRoot
chain_getStateRoot
```

验收标准：

- 前端所有核心页面都只调用真实 RPC。
- 不允许用硬编码 mock 数据冒充链状态。

## 13. 前端改造建议

前端应从“功能入口集合”改为“链运行仪表盘”。

### 13.1 Dashboard 必备字段

后端新增 RPC：

```text
chain_getConsensusStatus
```

返回：

```json
{
  "height": 123,
  "finalizedHeight": 117,
  "consensusEngine": "ConsensusEngine",
  "consensusMode": "sbox_primary",
  "fallbackPolicy": "idle_block_only",
  "currentProposer": "node_abc",
  "validatorCount": 21,
  "pendingTaskCount": 8,
  "pendingChallengeCount": 2,
  "lastFinalizedHash": "...",
  "lastBlockTime": 30.2
}
```

前端展示：

- 当前高度
- finalized 高度
- 当前 proposer
- fallback 策略
- pending tasks
- pending challenges
- 最近 20 个 block 状态

### 13.2 共识页面

新增或改造 `frontend/src/pages/Statistics.tsx` / `Explorer.tsx`：

1. 增加 “Consensus” tab。
2. 用表格展示 validator/proposer。
3. 用状态徽标展示 `PROPOSED`、`ACCEPTED`、`FINALIZED`。
4. 显示 PoUW proof 状态：`VALID`、`PENDING_CHALLENGE`、`INVALID`。

验收标准：

- 用户一眼能看到链是不是在 fallback。
- 用户能区分“已出块”和“已最终确认”。

### 13.3 任务市场页面

改造 `frontend/src/pages/Tasks.tsx`：

必须展示：

- 任务状态
- 预算是否锁定
- 已接单 worker
- proof 状态
- 挑战期剩余区块数
- 结算状态

任务详情页必须展示完整时间线：

```text
CREATED -> FUNDED -> LISTED -> ASSIGNED -> RUNNING -> RESULT_SUBMITTED -> CHALLENGE_WINDOW -> VERIFIED -> SETTLED
```

### 13.4 Worker 页面

新增或改造 `frontend/src/pages/Provider.tsx` / `Mining.tsx`：

必须支持：

1. 注册 worker。
2. 提交资源声明。
3. 查看可报价任务。
4. 提交报价。
5. 查看已分配任务。
6. 提交执行结果和 proof。

### 13.5 Explorer 页面

改造 `frontend/src/pages/Explorer.tsx`：

新增筛选：

- block status
- task id
- proof status
- settlement status
- worker address

## 14. 测试计划

### 14.1 编译测试

```powershell
py -3 -m compileall -q core api main.py
```

### 14.2 共识确定性测试

新增：

```text
tests/test_proposer_selection.py
tests/test_finality_state_machine.py
tests/test_consensus_fallback_policy.py
tests/test_canonical_serialization.py
```

必须覆盖：

1. 同输入得到同 proposer。
2. proposer 选择不污染全局 random。
3. 区块必须从 `PROPOSED` 到 `ACCEPTED` 再到 `FINALIZED`。
4. 默认配置下不能自动 PoW fallback。
5. canonical hash 跨字段顺序稳定。

### 14.3 RPC 集成测试

新增：

```text
tests/integration/test_consensus_status_rpc.py
```

测试：

- `chain_getConsensusStatus` 返回 `height`
- 返回 `finalizedHeight`
- 返回 `fallbackPolicy`
- 返回 `consensusEngine`

### 14.4 任务市场端到端测试

新增：

```text
tests/integration/test_verifiable_compute_market_e2e.py
```

必须覆盖：

1. 用户创建任务。
2. 用户锁定预算。
3. worker 报价。
4. 用户接受报价。
5. worker 提交结果 proof。
6. 进入挑战期。
7. 挑战期结束。
8. 区块 finality 后结算。
9. worker 声誉更新。

## 15. 开发排期建议

### Sprint 1：稳定生产入口

1. 修复 `main_transfer.py` 编译错误。
2. 加 `tests/test_production_consensus_entrypoint.py`。
3. 明确生产共识只走 `core/consensus.py`。
4. 把实验共识模块标记为 experimental。

### Sprint 2：共识基础设施

1. 新增 `core/serialization.py`。
2. 新增 `core/proposer_selection.py`。
3. 改掉全局 `random.seed()`。
4. 新增 proposer 选择测试。

### Sprint 3：finality 与奖励

1. 新增 `finalize_blocks()`。
2. 新增 `get_finalized_height()`。
3. 奖励只在 finality 后释放。
4. 前端显示 finalized height。

### Sprint 4：PoUW 边界与 fallback

1. 增加 PoUW proof 三态。
2. 默认禁用自动 PoW fallback。
3. 增加 `chain_getConsensusStatus`。
4. 前端展示 fallback 和 proof 状态。

### Sprint 5：可验证任务市场 MVP

1. 新增 `TaskStateStore`。
2. 新增任务 lifecycle 表。
3. 新增预算锁定和 worker bond。
4. 打通 `task_create -> market_submitQuote -> task_submitResult -> challenge -> settlement`。
5. 增加任务市场端到端测试。

### Sprint 6：前端产品化

1. Dashboard 展示链状态和任务市场状态。
2. Tasks 页面展示任务生命周期。
3. Provider 页面支持 worker 接单和提交结果。
4. Explorer 支持 task/proof/settlement 查询。

## 16. 最小可交付标准

当以下条件全部满足，才可以说共识层进入“可继续扩展”的状态：

1. 仓库可以完整编译。
2. 生产入口只有一个共识引擎。
3. proposer 选择确定、可重放、不污染全局随机。
4. block 有明确状态机。
5. finality 可查询。
6. 奖励不在未 finality 前释放。
7. PoUW 证明不再直接替代底层共识安全。
8. 默认不会静默 fallback 到 PoW。
9. 前端能展示高度、finalized 高度、fallback 策略和 proof 状态。

当以下条件全部满足，才可以说项目方向进入“可验证算力市场 MVP”：

1. 用户能提交真实任务。
2. 任务预算能被锁定。
3. worker 能报价和接单。
4. worker 能提交 result commitment 和 proof。
5. 系统有挑战期。
6. 挑战期结束后才能结算。
7. 结算依赖 finalized block。
8. 任务、proof、challenge、settlement 都能在 Explorer 查询。
9. worker 声誉会根据任务结果变化。
10. 前端不再用 mock 数据展示核心任务状态。
