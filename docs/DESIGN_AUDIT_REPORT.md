# POUW 多板块区块链 — 设计审计报告

> **审计类型**: 仅研究型（RESEARCH-ONLY）— 未修改任何源文件  
> **审计范围**: `c:\Users\17006\Desktop\maincoin` 全目录  
> **严重性等级**: CRITICAL / MAJOR / MODERATE / MINOR  
> **审计日期**: 2025-01

---

## 目录

1. [共识机制](#1-共识机制-consensus)
2. [经济模型](#2-经济模型-economic-model)
3. [P2P 网络层](#3-p2p-网络层)
4. [RPC 与 API 安全](#4-rpc-与-api-安全)
5. [密码学安全](#5-密码学安全)
6. [状态管理](#6-状态管理)
7. [治理系统](#7-治理系统)
8. [主入口与初始化](#8-主入口与初始化)
9. [摘要统计](#9-摘要统计)

---

## 1. 共识机制 (Consensus)

### D-01 | CRITICAL | POUW 出块跳过难度验证

| 字段 | 内容 |
|------|------|
| **Category** | Consensus Security |
| **File/Line** | `core/consensus.py` L802-843 |
| **Description** | `mine_pouw()` 仅检查 POUW 证明的工作量总和是否 ≥ 50，然后给 `block.nonce` 赋随机值并计算哈希。**没有对哈希做任何难度前缀检查**。这意味着 POUW 区块不受 `current_difficulty` 约束，攻击者只需伪造足够工作量分数即可以任意速度出块。 |

**Code Evidence** (`core/consensus.py` L835-842):
```python
# POUW 区块不需要复杂的 nonce 计算
block.nonce = random.randint(0, 1000000)
block.hash = block.compute_hash()
```

**Fix Approach**: POUW 出块也应满足一个（可降低的）难度条件，或者对 POUW 证明进行链上可验证的密码学绑定（如 VDF、承诺方案），使远程节点可以独立验证工作量真实性。

---

### D-02 | CRITICAL | POUW 证明缺乏密码学不可伪造性

| 字段 | 内容 |
|------|------|
| **Category** | Consensus Security |
| **File/Line** | `core/consensus.py` L726-800 (自动生成) + L802-843 (mine_pouw) |
| **Description** | `POUWProof` 的 `compute_work_score()` 基于自我报告的 `result_hash`、`execution_time` 和 `quality_score`。没有密码学承诺（commitment）、零知识证明或远程可验证机制。恶意矿工可以构造任意 `quality_score=1.0, execution_time=0.01` 的虚假证明来满足 `min_threshold=50`。 |

**Fix Approach**: 
1. 引入 VDF（可验证延迟函数）或承诺-揭露方案绑定计算结果。
2. 要求多节点交叉验证（至少随机抽样验证一部分 POUW 证明）。
3. 将 POUW 任务输入的哈希作为承诺，结果必须与承诺匹配。

---

### D-03 | MAJOR | add_transaction 不验证交易签名

| 字段 | 内容 |
|------|------|
| **Category** | Consensus Security |
| **File/Line** | `core/consensus.py` L666-696 |
| **Description** | `add_transaction()` 只做字段存在性检查（`tx_id`, `from_addr`, `to_addr`, `amount > 0`），**不验证交易的 ECDSA 签名**。未签名交易可进入 mempool 并被打包进区块。签名验证推迟到了 `_validate_block_transactions()` 中检查 UTXO 时才做，但 RPC 层的 `_tx_send()` 直接调用 `add_transaction()` 后就广播到 P2P 网络。 |

**Code Evidence** (`core/consensus.py` L666-696):
```python
def add_transaction(self, tx: Dict) -> bool:
    tx_id = tx.get('tx_id', tx.get('txid', ''))
    from_addr = tx.get('from', tx.get('from_address', ''))
    to_addr = tx.get('to', tx.get('to_address', ''))
    amount = tx.get('amount', 0)
    
    if not tx_id or not from_addr or not to_addr:
        return False
    if not isinstance(amount, (int, float)) or amount <= 0:
        return False
    # ... 无签名验证 ...
    self.pending_transactions.append(tx)
    return True
```

**Fix Approach**: 在 `add_transaction()` 入口处立即验证签名。拒绝未签名或签名无效的交易进入 mempool。

---

### D-04 | MAJOR | 单 threading.Lock + check_same_thread=False 的 SQLite 并发风险

| 字段 | 内容 |
|------|------|
| **Category** | Consensus / State Integrity |
| **File/Line** | `core/consensus.py` L448, L462 |
| **Description** | 共识引擎使用单个 `threading.Lock()` 保护内存状态，但同时使用 `check_same_thread=False` 共享单个 SQLite 连接。如果任何代码路径在未持有锁的情况访问 `self._db_conn`，或者有 asyncio 协程交错访问，将导致 SQLite 数据损坏。此模式在高并发场景下需极其谨慎。 |

**Code Evidence** (`core/consensus.py` L448, L462):
```python
self._lock = threading.Lock()
# ...
self._db_conn = sqlite3.connect(self.db_path, check_same_thread=False)
```

**Fix Approach**: 改用线程局部连接（`threading.local()` + 每线程独立 `sqlite3.connect()`），或将所有 DB 操作序列化到单一工作线程，或使用 WAL 模式 + 连接池。

---

### D-05 | MODERATE | MAX_REORG_DEPTH=100 过于宽松

| 字段 | 内容 |
|------|------|
| **Category** | Consensus / Chain Safety |
| **File/Line** | `core/consensus.py` (常量定义处) |
| **Description** | 最大重组深度 100 块在 30 秒出块间隔下意味着允许约 50 分钟的链重组。这大大超出了多数公链的安全阈值（比特币约 6 块，以太坊 finality ≈ 13 分钟）。深度重组将导致已确认交易和板块币铸造被大规模回滚。 |

**Fix Approach**: 将 `MAX_REORG_DEPTH` 降低到 20-30 块，并结合 finality threshold (当前为 20) 做强制 checkpoint。

---

### D-06 | MODERATE | 自动生成 POUW 基准任务可被恶意利用

| 字段 | 内容 |
|------|------|
| **Category** | Consensus Design |
| **File/Line** | `core/consensus.py` L726-800 (`_auto_generate_pouw`) |
| **Description** | 当 pending POUW 队列为空时，节点自动生成基准任务（矩阵乘法等）并立即"执行"它们。这些任务全在本地自我验证。攻击者可以操控输入参数或直接伪造 `quality_score`，因为没有外部验证者。 |

**Fix Approach**: 基准任务应使用确定性种子（来自上一区块哈希），结果必须能被其他节点独立验证。

---

## 2. 经济模型 (Economic Model)

### D-07 | CRITICAL | 见证签名验证为结构性检查而非密码学验证

| 字段 | 内容 |
|------|------|
| **Category** | Economic / Exchange Security |
| **File/Line** | `core/dual_witness_exchange.py` L360-385 (`add_witness`) |
| **Description** | 双见证兑换中的 `add_witness()` 方法虽然要求签名参数，但实际只做长度检查（`len(sig_bytes) < 64`），**没有验证签名的密码学有效性**。代码中有明确的 TODO 注释表明公钥注册表尚未实现。这意味着任何人只要提供足够长度的随机字节串即可充当"见证者"，绕过双见证安全机制。 |

**Code Evidence** (`core/dual_witness_exchange.py` L375-385):
```python
# TODO: 实现见证板块公钥注册表，从注册表获取 witness_sector 的授权公钥
sig_bytes = bytes.fromhex(signature)
payload_bytes = witness_payload.encode()
# 基本结构验证：DER 签名至少 70 字节
if len(sig_bytes) < 64:
    return False, f"见证签名长度无效..."
```

**Fix Approach**: 实现见证板块公钥注册表。每个板块必须有注册的授权签名公钥，`add_witness()` 必须用注册公钥验证 ECDSA 签名的密码学有效性。

---

### D-08 | MAJOR | dual_witness_exchange 中 MAIN 铸造无全局原子性保证

| 字段 | 内容 |
|------|------|
| **Category** | Economic / Atomicity |
| **File/Line** | `core/dual_witness_exchange.py` L470-530 (`_complete_exchange`) |
| **Description** | 兑换完成时先调用 `sector_ledger.burn_for_exchange()`（操作 sector_coin.db），再调用 `_mint_main()`（操作 exchange.db）。这是两个不同 SQLite 数据库的操作，**没有分布式事务或两阶段提交**。如果销毁成功但铸造前崩溃，用户板块币已销毁但 MAIN 未到账。 |

**Code Evidence** (`core/dual_witness_exchange.py` L473-484):
```python
def _complete_exchange(self, conn, request):
    # 销毁板块币 (sector_coin.db)
    ok, msg = self.sector_ledger.burn_for_exchange(...)
    if not ok:
        request.status = ExchangeStatus.FAILED
        ...
    # 铸造 MAIN (exchange.db)
    self._mint_main(conn, ...)
```

**Fix Approach**: 采用 crash journal 模式（项目中已有 `crash_journal` 但未用于此路径），或先记录"待完成铸造"事务，启动时自动补偿。

---

### D-09 | MAJOR | 浮点数用于货币计算

| 字段 | 内容 |
|------|------|
| **Category** | Economic / Precision |
| **File/Line** | 多处：`core/sector_coin.py` balance 字段、`core/utxo_store.py` amount 字段、`core/dao_treasury.py` balance 字段 |
| **Description** | 所有余额和金额均使用 Python `float`（IEEE 754 双精度），存入 SQLite 的 `REAL` 列。虽然项目引入了 `core/precision.py` 的 `safe_mul()` / `to_display()` 在少数路径使用，但绝大多数路径仍直接使用 `float` 算术。`0.1 + 0.2 ≠ 0.3` 的经典问题会在大量交易累积后导致不可忽略的精度偏差。 |

**Fix Approach**: 将所有货币值统一为整数单位（如 satoshi/wei 模式），或全面使用 `Decimal` 类型。数据库列应为 `INTEGER`（存储最小单位）。

---

### D-10 | MODERATE | Testnet 模式兼容参数仍需谨慎

| 字段 | 内容 |
|------|------|
| **Category** | Economic / Configuration |
| **File/Line** | `core/dual_witness_exchange.py` 初始化处 |
| **Description** | 兑换模块保留了 `testnet` 兼容参数，并会在测试环境下将 `required_witnesses` 降为 1。生产环境如果误用该参数，仍然会削弱双见证约束。 |

**Fix Approach**: 生产构建应通过编译期/环境变量硬编码禁止 testnet 模式，或至少在启动时有明显的安全警告和确认步骤。

---

### D-11 | MODERATE | 板块币兑换率硬编码 base_rate=0.5

| 字段 | 内容 |
|------|------|
| **Category** | Economic Design |
| **File/Line** | `core/dual_witness_exchange.py` L280-290 |
| **Description** | 所有板块（H100、RTX4090、RTX3080、CPU、GENERAL）的基础兑换率均为 0.5（1 板块币 = 0.5 MAIN）。这不反映不同 GPU 算力的真实价值差异。虽然 `DynamicExchangeRate` 引擎可选启用，但默认未启用。 |

**Fix Approach**: 为不同板块设定差异化基础兑换率，并默认启用动态兑换率引擎以反映市场供需。

---

## 3. P2P 网络层

### D-12 | CRITICAL | P2P 身份认证使用 TOFU 模型（无 PKI）

| 字段 | 内容 |
|------|------|
| **Category** | Network Security |
| **File/Line** | `core/tcp_network.py` 全文 (HMAC challenge-response) |
| **Description** | P2P 节点身份基于 `_network_secret` 的 HMAC challenge-response，节点"公钥"是 `SHA256(secret)` 的截断。这是 Trust-On-First-Use (TOFU) 模型，**没有 PKI 或 CA 基础设施**。中间人攻击者在首次连接时可以冒充任何节点。一旦网络密钥泄露，所有节点身份可被伪造。 |

**Fix Approach**: 
1. 每个节点应使用 ECDSA 密钥对进行身份认证。
2. 引入种子节点签名的对等节点证书或基于区块链的身份注册。
3. HMAC-based 认证应至少使用节点私有密钥而非共享密钥。

---

### D-13 | MAJOR | P2P 消息无源认证

| 字段 | 内容 |
|------|------|
| **Category** | Network Security |
| **File/Line** | `core/tcp_network.py` 消息处理部分 |
| **Description** | 认证后的消息（NEW_BLOCK、NEW_TX 等）不包含发送者签名。任何已连接的对等节点可以伪造任意 `sender_id` 的消息。`seen_messages` 集合仅用于去重，不验证消息完整性或来源。 |

**Fix Approach**: 所有非匿名消息应包含发送者的 ECDSA 签名，接收方验证签名后才处理。

---

### D-14 | MODERATE | 消息大小限制 2MB 可能被滥用

| 字段 | 内容 |
|------|------|
| **Category** | Network / DoS |
| **File/Line** | `core/tcp_network.py` MAX_MESSAGE_SIZE |
| **Description** | 2MB 的消息大小限制意味着每个对等节点可以以速率限制允许的频率发送 2MB 消息（200/分钟），理论上可消耗 400MB/min 的带宽。对于区块链协议，正常消息通常远小于此。 |

**Fix Approach**: 按消息类型设定不同的大小限制（如 BLOCK 可以更大，PING/PONG 应限制在 1KB 内）。

---

### D-15 | MINOR | seen_messages 集合无过期机制

| 字段 | 内容 |
|------|------|
| **Category** | Network / Memory |
| **File/Line** | `core/tcp_network.py` seen_messages LRU Set (10000) |
| **Description** | `seen_messages` 是一个最大 10000 项的集合，用于消息去重。虽然有大小限制，但旧消息的淘汰意味着相同消息可能在足够久之后被重新处理（replay）。 |

**Fix Approach**: 结合 TTL（如 10 分钟）+ 大小限制的双重淘汰策略。消息应包含时间戳，超过窗口期的消息直接拒绝。

---

## 4. RPC 与 API 安全

### D-16 | CRITICAL | 双重权限体系相互矛盾

| 字段 | 内容 |
|------|------|
| **Category** | API Security |
| **File/Line** | `core/rpc_service.py` (RPCPermission 注册) + `core/security.py` L500-586 (PUBLIC_RPC_METHODS / AUTHENTICATED_WRITE_METHODS) + `core/rpc/server.py` L200-225 |
| **Description** | 项目存在**三套独立的权限控制**：(1) `RPCMethodRegistry` 的 `RPCPermission` 枚举注册；(2) `security.py` 中 `PUBLIC_RPC_METHODS` 硬编码集合；(3) `server.py` 中 `AUTHENTICATED_WRITE_METHODS` 硬编码集合。三套系统中方法分类不一致：例如 `wallet_create` 和 `wallet_import` 在 `PUBLIC_RPC_METHODS` 中（无需认证），但其 handler 内部通过 `auth_context` 获取用户信息并默认为 `"local_admin"`。新增方法需同步维护三个位置，极易遗漏。 |

**Fix Approach**: 统一为单一权限源：仅使用 `RPCMethodRegistry` 的 `RPCPermission` 注册，`server.py` 直接查询注册表获取权限，消除硬编码集合。

---

### D-17 | MAJOR | Localhost 自动获得完全管理员权限

| 字段 | 内容 |
|------|------|
| **Category** | API Security |
| **File/Line** | `core/rpc/server.py` L121-133 (`_extract_auth_context`) |
| **Description** | 所有来自 `127.0.0.1` / `::1` 的请求自动获得 `is_admin=True, user_address='local_admin'`，无需任何密钥或令牌。在 Docker/Kubernetes 环境中，容器内的任何进程都可以通过回环地址获得管理员权限。SSRF 漏洞也可通过此路径提权。 |

**Code Evidence** (`core/rpc/server.py` L126-133):
```python
if client_ip in ('127.0.0.1', '::1', 'localhost'):
    return {
        'role': 'local',
        'user_address': 'local_admin',
        'is_admin': True,
        'is_local': True,
    }
```

**Fix Approach**: Localhost 应仅授予 USER 权限。ADMIN 操作始终要求 API Key。提供环境变量选项禁用 localhost 自动信任。

---

### D-18 | MAJOR | RPC _tx_send 不验证交易签名即广播

| 字段 | 内容 |
|------|------|
| **Category** | API Security |
| **File/Line** | `core/rpc_service.py` L1635-1680 (`_tx_send`) |
| **Description** | `_tx_send()` 接收外部交易数据，如果缺少 `tx_id` 则**自动生成**（而非拒绝），然后提交到 consensus pending 池并通过 P2P 广播。既不验证交易签名，也不验证发送者是否有足够余额。这允许任意垃圾数据污染 mempool 和 P2P 网络。 |

**Code Evidence** (`core/rpc_service.py` L1647-1650):
```python
if not tx_id:
    tx_id = _hl.sha256(f"tx_{uuid.uuid4().hex}".encode()).hexdigest()
    tx_data['tx_id'] = tx_id
```

**Fix Approach**: 必须验证签名有效性后才允许进入 mempool 和广播。不应为客户端自动生成 tx_id。

---

### D-19 | MODERATE | API 管理密钥自动生成后仅打印到 stdout

| 字段 | 内容 |
|------|------|
| **Category** | API Security / Operational |
| **File/Line** | `core/rpc/server.py` L293-295 (RPCServer.start) + `core/security.py` L149-153 (APIKeyAuth.__init__) |
| **Description** | 未配置 `admin_key` 时自动生成 64 字符十六进制令牌，仅打印到控制台。日志容易被截获。无速率限制的管理员 key 尝试机制（整体 rate limit 按 IP 不按 key）。 |

**Fix Approach**: 自动生成的密钥应写入受权限保护的文件（如 `~/.pouw/admin.key`，chmod 600），并在控制台提示密钥文件位置而非密钥值。

---

### D-20 | MODERATE | 请求体限制 1MB 但无方法级负载限制

| 字段 | 内容 |
|------|------|
| **Category** | API / DoS |
| **File/Line** | `core/rpc/server.py` L183-186 (do_POST) |
| **Description** | 全局请求体限制 1MB (`content_length > 1048576`)。但某些方法（如 `tx_send`、`task_create`）的合理负载可能远小于此，而恶意用户可以在 1MB 限制下构造大量垃圾参数。缺少方法级参数模式校验。 |

**Fix Approach**: 为每个 RPC 方法定义参数 JSON Schema，在 `handle_request()` 入口进行模式校验。

---

### D-21 | MINOR | wallet_create/wallet_import 列入 PUBLIC_RPC_METHODS

| 字段 | 内容 |
|------|------|
| **Category** | API Design |
| **File/Line** | `core/security.py` L555-556 |
| **Description** | `wallet_create` 和 `wallet_import` 被列为公开方法（无需认证）。虽然创建钱包本身不直接构成安全威胁，但如果创建钱包触发磁盘 I/O（keystore 文件写入），则可被滥用为磁盘耗尽攻击向量。 |

**Fix Approach**: 对公开的钱包创建方法增加 rate limit（如每 IP 每小时 5 次），或需要 CAPTCHA。

---

## 5. 密码学安全

### D-22 | MAJOR | PBKDF2 迭代次数不一致（100,000 vs 310,000）

| 字段 | 内容 |
|------|------|
| **Category** | Cryptographic Security |
| **File/Line** | `core/crypto.py` AESCipher.derive_key (100,000 iterations) vs `core/crypto_utils.py` derive_key (310,000 iterations) |
| **Description** | 项目中有两个独立的 AES 加密模块：`crypto.py` 的 `AESCipher` 使用 100,000 次 PBKDF2 迭代，`crypto_utils.py` 使用 310,000 次。`crypto.py` 的 100,000 次低于 OWASP 2023 建议的 310,000 次迭代标准。哪个模块被调用取决于代码路径，导致安全性不一致。 |

**Fix Approach**: 统一为单一加密模块，使用 OWASP 推荐的 310,000+ 次 PBKDF2 迭代。废弃低迭代次数的旧模块并添加迁移路径。

---

### D-23 | MAJOR | BIP39 后备词表仅 256 个真实单词

| 字段 | 内容 |
|------|------|
| **Category** | Cryptographic Security |
| **File/Line** | `core/crypto.py` BIP39 fallback wordlist |
| **Description** | 当 `bip39` 库不可用时，代码回退到内嵌词表。该词表仅包含 256 个真实英文单词，其余用 `"wordN"` 形式的占位符填充至 2048 个。12 个助记词从 256 个真实词中选取意味着仅有 $256^{12} = 2^{96}$ 种组合（标准 BIP39 为 $2048^{12} ≈ 2^{132}$），严重降低助记词熵。 |

**Fix Approach**: 要么始终要求安装 `bip39` 库（启动时检查），要么内嵌完整的 2048 个 BIP39 标准英文词表。

---

### D-24 | MODERATE | ast.literal_eval 作为钱包加载后备

| 字段 | 内容 |
|------|------|
| **Category** | Cryptographic Security / Input Safety |
| **File/Line** | `core/crypto.py` ProductionWallet.load_encrypted |
| **Description** | 钱包加载中使用 `ast.literal_eval()` 作为 JSON 解析失败时的后备方案。虽然 `literal_eval` 比 `eval` 安全得多（仅允许字面量），但仍扩大了攻击面——恶意构造的 keystore 文件可能包含复杂的嵌套字面量导致内存耗尽。 |

**Fix Approach**: 删除 `ast.literal_eval()` 后备。如果 JSON 解析失败，应直接报错。

---

### D-25 | MODERATE | 板块币转账地址验证使用 SHA256 截断

| 字段 | 内容 |
|------|------|
| **Category** | Cryptographic Security |
| **File/Line** | `core/sector_coin.py` L383-388 (`transfer` 方法) |
| **Description** | 板块币转账的公钥→地址验证使用 `hashlib.sha256(public_key.encode()).hexdigest()[:40]`，即 SHA256 的前 160 位。但 `core/crypto.py` 中的正式地址生成使用 RIPEMD160 + Base32 编码。两个地址方案不兼容，导致正式钱包地址无法通过板块币转账的地址验证。 |

**Code Evidence** (`core/sector_coin.py` L385-387):
```python
derived_address = hashlib.sha256(public_key.encode()).hexdigest()[:40]
if derived_address != from_address and public_key != from_address:
    return False, "公钥与发送地址不匹配"
```

**Fix Approach**: 统一使用 `core/crypto.py` 中的 `generate_address()` 函数进行公钥到地址的转换。

---

### D-26 | MINOR | TLS 客户端使用 CERT_OPTIONAL 模式

| 字段 | 内容 |
|------|------|
| **Category** | Transport Security |
| **File/Line** | `core/security.py` L134-142 (`create_ssl_context`) |
| **Description** | 未配置 CA 证书时，TLS 客户端使用 `CERT_OPTIONAL`，不验证服务端证书。这使 P2P 连接容易受到 MITM 攻击。代码已有日志警告，但生产环境不应允许此回退。 |

**Fix Approach**: 生产模式下，缺少 CA 证书应拒绝连接而非降级到 CERT_OPTIONAL。

---

## 6. 状态管理

### D-27 | MAJOR | 多数据库跨库操作缺乏原子性

| 字段 | 内容 |
|------|------|
| **Category** | State Management |
| **File/Line** | `main.py` L1000-1080 (mining callback), `core/dual_witness_exchange.py` L470-530 |
| **Description** | 系统使用至少 4 个独立 SQLite 数据库：`chain.db`（共识）、`utxo.db`（UTXO）、`sector_coin.db`（板块币）、`exchange.db`（兑换）。挖矿回调和兑换流程需要跨多个数据库原子写入。虽然 `main.py` 引入了 `crash_journal` 用于挖矿回调，但兑换路径（D-08）和其他跨库操作未使用 crash journal。 |

**Fix Approach**: 
1. 所有跨库操作均使用 crash journal 记录操作序列。
2. 启动时检查未完成的 journal 条目并自动补偿/回滚。
3. 考虑合并为单一数据库以简化事务管理。

---

### D-28 | MAJOR | 板块币回滚重算余额可能与 locked 金额不一致

| 字段 | 内容 |
|------|------|
| **Category** | State Integrity |
| **File/Line** | `core/sector_coin.py` L625-710 (`rollback_to_height`) |
| **Description** | 回滚时重算余额（`balance = received - sent`），但保留原始的 `locked` 值（`COALESCE((SELECT locked FROM balances WHERE...)`）。如果在被回滚的区块高度范围内有兑换锁定操作，回滚后 `locked` 仍反映旧值，可能出现 `locked > balance` 的非法状态。 |

**Fix Approach**: 回滚时同时清理/重算受影响地址的 `locked` 金额。

---

### D-29 | MODERATE | 全局单例模式导致测试困难和状态泄漏

| 字段 | 内容 |
|------|------|
| **Category** | State Management / Modularity |
| **File/Line** | `core/sector_coin.py` L720 (`get_sector_ledger`), `core/utxo_store.py` L970 (`get_utxo_store`), `core/dual_witness_exchange.py` L618 (`get_exchange_service`) |
| **Description** | 多个核心组件使用模块级全局变量 + `get_xxx()` 工厂的单例模式。这使得单元测试时难以隔离状态，集成测试间可能出现状态泄漏。且首次初始化的参数被永久固化。 |

**Fix Approach**: 改用依赖注入模式。主入口显式创建所有组件实例并注入依赖关系。

---

### D-30 | MINOR | DAO 治理状态既在内存也在 SQLite，一致性靠手动同步

| 字段 | 内容 |
|------|------|
| **Category** | State Consistency |
| **File/Line** | `core/dao_treasury.py` L430-500 (DAOGovernance) |
| **Description** | `DAOGovernance` 在内存中维护 `proposals`, `votes`, `stakes` 字典，同时持久化到 SQLite。每次修改需手动调用 `_save_xxx()`。如果任何写入路径遗漏了持久化调用，内存和磁盘状态将不一致。重启后从 DB 恢复可能丢失未持久化的变更。 |

**Fix Approach**: 采用 write-through 模式（所有写入先到 DB，内存作为缓存），或使用 ORM。

---

## 7. 治理系统

### D-31 | MAJOR | DAO 多签密钥自动生成（占位实现）

| 字段 | 内容 |
|------|------|
| **Category** | Governance Security |
| **File/Line** | `core/dao_treasury.py` L200-280 (TreasuryManager 初始化) |
| **Description** | DAO 国库的 3/5 多签密钥在初始化时自动生成 ECDSA 密钥对。这意味着单个节点进程持有所有 5 个签名者的私钥，违背了多签的根本安全前提（密钥分散持有）。代码注释标记为"production should use real distributed signers"。 |

**Fix Approach**: 多签公钥应通过配置文件注入（来自 HSM 或物理分离的签名设备），节点进程不应持有任何签名者私钥。

---

### D-32 | MAJOR | protocol_fee_pool 的 execute_spending 多签验证仅检查数量

| 字段 | 内容 |
|------|------|
| **Category** | Governance Security |
| **File/Line** | `core/protocol_fee_pool.py` L450-470 (`execute_spending`) |
| **Description** | `execute_spending()` 仅检查 `len(executors) < 2`（至少 2 个执行者 ID），不验证签名。注释明确说"这里简化处理，实际应该验证多签"。 |

**Code Evidence** (`core/protocol_fee_pool.py` ~L460):
```python
# 验证执行者（多签）
# 这里简化处理，实际应该验证多签
if len(executors) < 2:
    return False, "需要至少 2 个签名"
```

**Fix Approach**: 实现真正的 ECDSA 多签验证，与 `dao_treasury.py` 的 `execute_withdrawal()` 相同标准。

---

### D-33 | MODERATE | 治理投票权重计算无上限有效限制

| 字段 | 内容 |
|------|------|
| **Category** | Governance Design |
| **File/Line** | `core/governance_enhanced.py` L607-645 (`_calculate_vote_weight`) |
| **Description** | 投票权重公式为 `base × role_mult × (1 + contribution_bonus) × (1 + stake_bonus)`。其中 `stake_bonus` 上限 3.0，`contribution_bonus` 上限 0.5，`role_mult` 对核心开发者为 3.0。理论最大权重约 `1 × 3.0 × 1.5 × 4.0 = 18.0`。但角色分配没有去中心化机制——任何拥有 `user_roles` 写入权限的管理员可以自封为核心开发者获得 3x 权重倍数。 |

**Fix Approach**: 角色分配应通过治理提案进行，不允许单方面修改。`ROLE_WEIGHT_MULTIPLIER` 的最大值应进一步降低或引入上限校验。

---

### D-34 | MODERATE | 紧急提案跳过讨论期直接进入投票

| 字段 | 内容 |
|------|------|
| **Category** | Governance Design |
| **File/Line** | `core/governance_enhanced.py` L870-890 (`create_emergency_proposal`) |
| **Description** | `create_emergency_proposal()` 跳过 DRAFT 和 DISCUSSION 阶段直接进入 VOTING。虽然仅守护者可创建，但与 `emergency_multisig_execute` 组合，少数守护者可以跳过社区讨论直接执行提案（emergency 类别投票期仅 24 小时、法定人数仅 10%）。 |

**Fix Approach**: 紧急提案应有更高的法定人数要求，或发布后有一个最低冷静期（如 6 小时），或紧急执行后需社区追认。

---

## 8. 主入口与初始化

### D-35 | MAJOR | 出块回调中的 print 和异常处理可能丢失区块

| 字段 | 内容 |
|------|------|
| **Category** | Reliability |
| **File/Line** | `main.py` L1000-1080 (mining_callback) |
| **Description** | 挖矿回调（`_on_block_mined`）在写入板块币和 UTXO 后通过 P2P 广播区块。如果广播前的任何步骤抛出异常（如 sector_ledger 写入失败），外层的 `try/except` 打印错误后返回——区块已被共识引擎记录但配套经济数据未写入，导致链上/UTXO/板块币状态不一致。虽然 crash journal 记录了部分操作，但恢复逻辑不完整。 |

**Fix Approach**: 采用全有或全无策略：在所有配套写入成功前，不应将区块标记为最终确认。可以使用 "tentative" 状态 + 定期 finalize。

---

### D-36 | MODERATE | 崩溃重试最多 3 次后静默退出

| 字段 | 内容 |
|------|------|
| **Category** | Reliability |
| **File/Line** | `main.py` L1150-1170 (`run` 方法) |
| **Description** | 主运行循环有 `max_retries=3` 的崩溃重试。达到上限后，进程静默退出（仅打印日志）。在生产环境中，这意味着节点可能在无人注意的情况下停止运行。没有与外部进程管理器（systemd、supervisor、k8s liveness probe）的集成。 |

**Fix Approach**: 
1. 达到重试上限后以非零退出码退出，让进程管理器决定是否重启。
2. 集成健康检查端点供容器编排探测。

---

### D-37 | MINOR | 配置依赖硬编码默认值，无配置验证

| 字段 | 内容 |
|------|------|
| **Category** | Operational |
| **File/Line** | `main.py` 全文 CLI 参数和默认值 |
| **Description** | 关键参数（如 RPC 绑定地址、端口、挖矿板块）有硬编码默认值但无统一的配置验证。虽然支持 `config.yaml`，但配置文件中的无效值（如 `sector: "INVALID"` 或负数端口）不会在启动时被验证和拒绝。 |

**Fix Approach**: 在启动时实现配置模式验证（如使用 pydantic 或 jsonschema），对无效配置快速失败并给出明确的错误消息。

---

## 9. 摘要统计

| 严重性 | 数量 | Issue IDs |
|--------|------|-----------|
| **CRITICAL** | 5 | D-01, D-02, D-07, D-12, D-16 |
| **MAJOR** | 13 | D-03, D-04, D-08, D-09, D-13, D-17, D-18, D-22, D-23, D-27, D-28, D-31, D-32, D-35 |
| **MODERATE** | 12 | D-05, D-06, D-10, D-11, D-14, D-19, D-20, D-24, D-25, D-29, D-30, D-33, D-34, D-36 |
| **MINOR** | 4 | D-15, D-21, D-26, D-37 |
| **合计** | **34** | |

### 按审计领域分布

| 领域 | CRITICAL | MAJOR | MODERATE | MINOR | 小计 |
|------|----------|-------|----------|-------|------|
| 1. 共识机制 | 2 | 2 | 2 | 0 | 6 |
| 2. 经济模型 | 1 | 2 | 2 | 0 | 5 |
| 3. P2P 网络 | 1 | 1 | 1 | 1 | 4 |
| 4. RPC/API 安全 | 1 | 2 | 2 | 1 | 6 |
| 5. 密码学安全 | 0 | 2 | 2 | 1 | 5 |
| 6. 状态管理 | 0 | 2 | 2 | 0 | 4 |
| 7. 治理系统 | 0 | 2 | 2 | 0 | 4 |
| 8. 主入口/初始化 | 0 | 0 | 1 | 1 | 2 |

### 关键结论

1. **共识安全是最紧迫的风险**：POUW 证明缺乏密码学不可伪造性（D-01, D-02），且 POUW 出块完全绕过难度检查，这是区块链的根基问题。
2. **双见证机制名存实亡**：由于见证签名验证仅做长度检查（D-07），整个板块币→MAIN 的兑换保护形同虚设。
3. **P2P 层缺乏密码学身份**：TOFU 模型（D-12）在对抗性网络环境中不足以防御 Sybil/Eclipse 攻击。
4. **权限系统碎片化**（D-16）增加了安全配置出错的概率。
5. **浮点精度**（D-09）、**跨库原子性**（D-27）、**多签占位实现**（D-31, D-32）需要在主网上线前修复。

---

> *本报告仅基于代码静态审查，未修改任何源文件。建议在解决 CRITICAL 和 MAJOR 级别问题前勿部署到生产环境。*
