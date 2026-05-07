"""
RPC 服务模块 - Web UI 后端支持

模型和服务器类已拆分到 core/rpc/ 
  - core/rpc/models.py: RPCErrorCode, RPCError, RPCRequest, RPCResponse, RPCPermission, RPCMethodRegistry
  - core/rpc/server.py: RPCHTTPHandler, RPCServer, RPCClient

本文件保留 NodeRPCService 业务逻辑实现
外部代码可继续使用`from core.rpc_service import RPCServer` (兼容
"""

import json
import time
import uuid
import hashlib
import os
import re
import datetime
import mimetypes
import logging
from dataclasses import dataclass, field
import inspect
from typing import Dict, List, Optional, Any, Callable, Tuple
from enum import Enum
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

# 从拆分后的模块导入模型类
from core.rpc.models import (
    RPCErrorCode,
    RPCError,
    RPCRequest,
    RPCResponse,
    RPCPermission,
    RPCMethodRegistry,
)

# 兼容性重导出: RPCServer, RPCHTTPHandler, RPCClient
# 注意：RPCServer core/rpc/server.py 中，它会延迟导入 NodeRPCService 以避免循
from core.rpc.server import RPCServer, RPCHTTPHandler, RPCClient

logger = logging.getLogger(__name__)



class NodeRPCService:
    """
    节点 RPC 服务
    
    提供以下能力
    - 交易相关：发送交易、查询交易、获mempool
    - 区块相关：获取区块、获取最新高
    - 账户相关：查询余额、获UTXO
    - 节点相关：节点状态、对等节
    - 算力市场：算力订
    - 见证相关：见证请
    - 治理相关：投票、提
    
    安全特性：
    - 钱包信息使用 AES-256-GCM 加密存储
    - 会话超时自动清除敏感数据
    - 密钥派生使用 PBKDF2 (310000 迭代)
    """
    
    # 安全配置
    WALLET_SESSION_TIMEOUT = 600  # 钱包会话超时（秒）
    
    def __init__(self):
        self.registry = RPCMethodRegistry()
        
        # 节点状态
        self.node_id = str(uuid.uuid4())[:8]
        self.current_height = 0
        self.is_syncing = False
        self.connected_peers = 0
        self._start_time = time.time()  # 启动时间戳，用于计算 uptime
        
        # 依赖组件（可被外部设置）
        self.blockchain = None
        self.mempool = None
        self.tx_store = None
        self.p2p_network = None
        self.compute_market = None
        self.compute_scheduler = None  # 算力调度器（核心任务引擎）
        
        # 共识引擎和板块币账本（新增）
        self.consensus_engine = None
        self.sector_ledger = None
        self.miner_address = None  # 当前矿工地址
        self.utxo_store = None  # UTXO 存储
        self.main_transfer_engine = None  # MAIN 转账引擎（双见证）
        self.mining_mode = 'mine_only'  # 挖矿模式: mine_only / task_only / mine_and_task
        self.accepting_tasks = False  # 是否接受算力订单
        self._scoring_engine = None  # 评分引擎（延迟初始化）
        
        # === 安全钱包存储 ===
        # Security: Per-user wallet isolation wallet_owner tracks who unlocked
        self.wallet_info = None  # 临时存储，会话超时后清除
        self._wallet_session_start = 0  # 钱包会话开始时
        self._wallet_owner = None  # 标记钱包属于哪个用户/session
        self._wallet_session_lock = threading.Lock()  # 保护钱包会话的并发访问
        
        # === 钱包解锁防暴力破解 ===
        self._unlock_attempts: Dict[str, list] = {}  # address -> [timestamp, ...]
        self.MAX_UNLOCK_ATTEMPTS = 5  # 最大尝试次数
        self.UNLOCK_LOCKOUT_SECONDS = 900  # 锁定时间 15 分钟
        
        # === 真实数据存储 ===
        self.tasks: Dict[str, Dict] = {}  # task_id -> task
        self.proposals: Dict[str, Dict] = {}  # proposal_id -> proposal
        self.market_orders: Dict[str, Dict] = {}  # order_id -> order
        self.transactions: List[Dict] = []  # 交易历史
        self.sub_addresses: Dict[str, List[Dict]] = {}  # address -> sub_addresses
        
        # 初始化示例数据
        self._init_sample_data()
        
        # 大文件分块传输管理器
        from .file_transfer import ChunkedFileManager
        self._file_manager = ChunkedFileManager(base_dir="data")
        
        # P2P 加密数据直传通道管理器
        self._p2p_ticket_manager = None  # 延迟初始化
        self._p2p_data_server = None     # 矿工侧 P2P 数据服务器
        
        # 注册默认方法
        self._register_default_methods()
    
    def _check_wallet_session(self):
        """检查钱包会话是否过期，过期则清除敏感数据"""
        with self._wallet_session_lock:
            if self.wallet_info and self._wallet_session_start:
                elapsed = time.time() - self._wallet_session_start
                if elapsed > self.WALLET_SESSION_TIMEOUT:
                    self._clear_wallet_session()
                    return False
            return True
    
    def _clear_wallet_session(self):
        """安全清除钱包会话数据"""
        if self.wallet_info:
            # 尽力覆盖敏感字段（Python 字符串不可变，但可断开引用加速 GC）
            if hasattr(self.wallet_info, 'mnemonic') and self.wallet_info.mnemonic:
                object.__setattr__(self.wallet_info, 'mnemonic', None)
            if hasattr(self.wallet_info, 'master_private_key') and self.wallet_info.master_private_key:
                object.__setattr__(self.wallet_info, 'master_private_key', None)
            self.wallet_info = None
        self._wallet_session_start = 0
        self._wallet_owner = None
        # 触发垃圾回收尽早释放敏感字符串
        import gc
        gc.collect()
    
    def _refresh_wallet_session(self):
        """刷新钱包会话"""
        with self._wallet_session_lock:
            if self.wallet_info:
                self._wallet_session_start = time.time()

    def _get_auth_user(self, auth_context: Optional[Dict], default: str = "anonymous") -> str:
        """从认证上下文提取调用者标识，兼容 user/user_address 两种字段。"""
        if not auth_context:
            return default
        return (
            auth_context.get("user")
            or auth_context.get("user_address")
            or default
        )

    def _rpc_internal_error(
        self,
        method: str,
        error: Exception,
        fallback: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """统一构造 RPC 内部错误响应，避免接口吞异常后假成功。"""
        logger.exception("RPC method failed: %s", method, exc_info=error)
        result: Dict[str, Any] = {
            "success": False,
            "error": "internal_error",
            "errorCode": "INTERNAL_ERROR",
            "method": method,
            "timestamp": time.time(),
        }
        if fallback:
            result.update(fallback)
        return result

    def _rpc_log_exception(self, method: str, error: Exception) -> None:
        """记录异常但保留既有返回结构（用于历史 List/标量接口兼容）。"""
        logger.exception("RPC method failed: %s", method, exc_info=error)

    def _compute_v3_required(self) -> bool:
        """是否强制 compute 写路径必须走 V3（禁用静默回退）。"""
        raw = str(os.getenv("POUW_COMPUTE_V3_REQUIRED", "true")).strip().lower()
        return raw in ("1", "true", "yes", "on")

    def _load_latest_keystore(self, address_hint: str = "") -> Optional[Dict]:
        """加载最新 keystore（可选按地址优先匹配）。"""
        try:
            import json as _json

            wallets_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "wallets",
            )
            if not os.path.isdir(wallets_dir):
                return None

            candidates = []
            for fname in os.listdir(wallets_dir):
                if not (fname.startswith("keystore_") and fname.endswith(".json")):
                    continue
                fpath = os.path.join(wallets_dir, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        ks = _json.load(f)
                except Exception:
                    continue
                if not isinstance(ks, dict) or not ks.get("address"):
                    continue
                created_at = ks.get("created_at", 0)
                if address_hint and ks.get("address") == address_hint:
                    created_at = created_at + 10**12  # 优先当前地址匹配
                candidates.append((created_at, ks))

            if not candidates:
                return None

            candidates.sort(key=lambda x: x[0], reverse=True)
            return candidates[0][1]
        except Exception:
            return None

    def _decrypt_keystore_mnemonic(self, keystore: Dict, password: str) -> Tuple[Optional[str], Optional[str]]:
        """解密 keystore 并返回助记词。

        Returns:
            (mnemonic, error_message) - 成功时 error_message 为 None
        """
        import base64

        try:
            crypto = keystore.get("crypto", {})
            cipher_type = crypto.get("cipher", "")

            salt = base64.b64decode(crypto["kdfparams"]["salt"])
            ciphertext = base64.b64decode(crypto["ciphertext"])
            iterations = crypto["kdfparams"].get("c", 310000)

            from .crypto_utils import derive_key_pbkdf2, aes_gcm_decrypt
            dk = derive_key_pbkdf2(password, salt, iterations)

            if cipher_type == "aes-256-gcm":
                nonce = base64.b64decode(crypto["nonce"])
                tag = base64.b64decode(crypto["tag"])
                try:
                    decrypted = aes_gcm_decrypt(ciphertext, dk, nonce, tag)
                    mnemonic = decrypted.decode("utf-8")
                except Exception:
                    return None, "密码错误或密钥文件已损坏"
            elif cipher_type == "xor-pbkdf2":
                return None, "XOR 加密格式已不再支持导入，请使用 AES-256-GCM"
            else:
                return None, f"不支持的加密格式: {cipher_type}"

            # 尽力覆盖派生密钥引用
            dk = b"\x00" * 32
            return mnemonic, None
        except Exception:
            return None, "密钥文件格式无效或已损坏"
    
    def _save_wallet_to_disk(self, keystore: Dict, address: str):
        """自动将密钥文件保存到 wallets/ 目录，重启后可恢复。
        
        同时保存 keystore 格式（用于导出）和 wallet_ 格式（用于 main.py 启动加载）。
        新钱包会替换旧的 wallet_ 文件，确保重启后加载最新钱包。
        """
        try:
            import json as _json
            wallets_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "wallets")
            os.makedirs(wallets_dir, exist_ok=True)
            
            # 1. 保存 keystore 格式（用于密钥文件导出）
            ks_filename = f"keystore_{address[:20]}.json"
            ks_filepath = os.path.join(wallets_dir, ks_filename)
            with open(ks_filepath, "w", encoding="utf-8") as f:
                _json.dump(keystore, f, indent=2, ensure_ascii=False)
            # 限制密钥文件权限：仅所有者可读写
            try:
                os.chmod(ks_filepath, 0o600)
            except (OSError, PermissionError):
                pass
            
            # 2. 保存 wallet_ 格式（供 main.py 启动时加载）
            #    使用 WalletEncryptor 加密助记词，与 main.py 格式兼容
            if hasattr(self, 'wallet_info') and self.wallet_info and self.wallet_info.mnemonic:
                try:
                    from core.security import WalletEncryptor
                    import time as _time
                    
                    # 保留历史 wallet_*.json，避免多钱包/回滚场景误删
                    # 启动恢复逻辑按 created_at 选择最近钱包
                    
                    # 生成 wallet_id
                    from core.crypto import HashUtils
                    wallet_id = HashUtils.sha256_hex(
                        bytes.fromhex(self.wallet_info.public_keys.get("MAIN", "00"))
                    )[:16]
                    
                    encrypted_mnemonic = WalletEncryptor.encrypt_mnemonic(self.wallet_info.mnemonic)
                    wallet_data = {
                        "wallet_id": wallet_id,
                        "encrypted_mnemonic": encrypted_mnemonic,
                        "address": address,
                        "created_at": _time.time(),
                    }
                    wallet_filename = f"wallet_{wallet_id}.json"
                    wallet_filepath = os.path.join(wallets_dir, wallet_filename)
                    with open(wallet_filepath, "w", encoding="utf-8") as f:
                        _json.dump(wallet_data, f, indent=2, ensure_ascii=False)
                    # 限制钱包文件权限：仅所有者可读写
                    try:
                        os.chmod(wallet_filepath, 0o600)
                    except (OSError, PermissionError):
                        pass
                except Exception:
                    pass  # wallet_ 格式保存失败不影响 keystore
        except Exception:
            pass  # 保存失败不影响钱包使用
    
    def load_wallet_from_disk(self) -> bool:
        """启动时从 wallets/ 目录加载最近的钱包 keystore。
        
        注意：只恢复地址（miner_address），不恢复助记词到内存。
        用户需要通过 wallet_unlock（输入密码）来重新激活完整会话。
        
        Returns:
            True if a wallet address was restored.
        """
        try:
            import json as _json
            wallets_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "wallets")
            if not os.path.isdir(wallets_dir):
                return False
            
            # 找到最新的 keystore 文件
            keystores = []
            for fname in os.listdir(wallets_dir):
                if fname.startswith("keystore_") and fname.endswith(".json"):
                    fpath = os.path.join(wallets_dir, fname)
                    try:
                        with open(fpath, "r", encoding="utf-8") as f:
                            ks = _json.load(f)
                        if ks.get("address"):
                            keystores.append((ks.get("created_at", 0), ks))
                    except Exception:
                        continue
            
            if not keystores:
                return False
            
            # 选择最新创建的
            keystores.sort(key=lambda x: x[0], reverse=True)
            latest_ks = keystores[0][1]
            addr = latest_ks.get("address", "")
            if addr:
                self.miner_address = addr
                # 不设置 wallet_info（助记词不驻留内存）
                # 用户需 wallet_unlock 从 keystore 解密后恢复完整会话
                return True
            return False
        except Exception:
            return False
    
    def _init_sample_data(self):
        """初始化数据存储（使用真实数据）"""
        # 提案存储 - 从数据库加载或初始化为空
        self.proposals = {}
        
        # 市场订单存储 - 从矿工注册信息动态生成
        self.market_orders = {}
        
        # 任务存储 - 从数据库加载或初始化为空
        self.tasks = {}
        
        # 注册的矿工 - P2P网络中注册的其他矿工节点
        self.registered_miners = {}
    
    def _register_default_methods(self):
        """注册默认 RPC 方法（带权限分级）
        
        [M-01] 使用 Handler 拆分模式:
        - wallet_*, dao_* 域已迁移到 core/rpc_handlers/ 下的独立 Handler
        - account_*, task_*, scheduler_*, compute_*, encryptedTask_*, file_*, e2e_*, p2pTunnel_*, pricing_*, budget_*, settlement_*, market_*, queue_*, blockchain_*, order_*, staking_*, rpc_*, tee_*, orderbook_*, futures_*, billing_*, dataLifecycle_*, ephemeralKey_*, p2p_*, did_*, mq_*, redundancy_*, loadTest_*, zk_*, security_*, audit_*, revenue_*, monitor_*, sdk_*, p2pTask_*, governance_*, contrib_*, dashboard_*, miner_*, stats_*, tx_*, mempool_*, block_*, chain_*, sbox_*, mining_*, sector_*, privacy_*, node_*, witness_* 已迁移到对应 handler
        - 注册入口仅保留迁移标记，具体注册由 handler 完成
        - Handler 通过 self.svc 引用 NodeRPCService 状态
        """
        
        # [M-01] 加载已拆分的领域 Handler（wallet, dao）
        try:
            from core.rpc_handlers import load_all_handlers
            self._handlers = load_all_handlers(self)
        except Exception as e:
            self._handlers = []
            print(f"[RPC] Handler 加载失败，回退到内联注册: {e}")
        
        # 所有 RPC 注册已迁移至 core/rpc_handlers/*_handler.py。
        # 下列分组仅作为迁移索引，真实注册在 load_all_handlers() 中完成。
        #
        # 基础域:
        # - dashboard, miner, stats, tx, mempool, block, chain/sbox
        # - mining, sector, privacy, node, witness
        #
        # 核心业务域:
        # - account, wallet, task, scheduler, compute
        # - encrypted_task, file, e2e, p2p_tunnel, p2p, p2p_task
        # - pricing, budget, settlement, market, queue, rpc_meta
        # - blockchain, order, staking, tee, orderbook, futures, billing
        # - data_lifecycle, ephemeral_key, did, mq, redundancy, cluster
        #
        # 治理与安全域:
        # - dao, governance, contrib
        # - load_test, zk, security, audit, revenue, monitor, sdk
        # - frontend_alias

    def _cluster_hardware(self, **kwargs) -> Dict:
        """Called by master to get worker hardware."""
        from core.device_detector import get_local_hardware_info
        return get_local_hardware_info()

    def _cluster_execute(self, **kwargs) -> Dict:
        """Called by master to execute a task on worker."""
        task_dict = kwargs.get("task")
        miner_id = kwargs.get("miner_id")
        
        from core.pouw_executor import PoUWExecutor, RealPoUWTask
        
        # Build RealPoUWTask object from dict
        if task_dict:
            import enum
            from core.pouw_executor import RealTaskType
            # Convert string task_type back to enum
            for task_type_enum in RealTaskType:
                if task_type_enum.value == task_dict.get("task_type"):
                    task_dict["task_type"] = task_type_enum
                    break
            task_obj = RealPoUWTask(**task_dict)
        else:
            return {"error": "Missing task parameter"}

        executor = PoUWExecutor()
        result_obj = executor.execute_task(task_obj, miner_id)
        
        # Convert dataclass result to dict for JSON RPC response
        from dataclasses import asdict
        return asdict(result_obj)

    def handle_request(self, request: RPCRequest, auth_context: Dict = None) -> RPCResponse:
        """
        处理 RPC 请求（带权限验证）
        
        Args:
            request: RPC 请求
            auth_context: 认证上下文，包含 user_address, miner_id, is_admin 
        """
        if not request.method:
            return RPCResponse.make_error(
                RPCErrorCode.INVALID_REQUEST.value,
                "Missing method",
                request.id
            )
        
        handler = self.registry.get(request.method)
        if not handler:
            return RPCResponse.make_error(
                RPCErrorCode.METHOD_NOT_FOUND.value,
                f"Method not found: {request.method}",
                request.id
            )
        
        # === 权限验证 ===
        required_permission = self.registry.get_permission(request.method)
        auth_context = auth_context or {}
        
        if not self._check_permission(required_permission, auth_context):
            return RPCResponse.make_error(
                -32403,  # 自定义权限错误码
                f"Permission denied: {required_permission.value} access required",
                request.id
            )
        
        try:
            # 尝试调用方法 - 支持两种签名风格:
            # 1. 旧风 handler(params: Dict)
            # 2. 新风 handler(**kwargs)
            params = request.params or {}
            
            # 防御性处理：将 [{}] 格式展开为 {}
            if isinstance(params, list):
                if len(params) == 1 and isinstance(params[0], dict):
                    params = params[0]
                elif len(params) == 0:
                    params = {}
                else:
                    # 多个位置参数，尝试合并为 dict（如果第一个是 dict）
                    if isinstance(params[0], dict):
                        params = params[0]
            
            # 参数标准化：自动将 snake_case 参数名转为 camelCase
            # 同时保留原始参数名，以兼容两种风格
            if isinstance(params, dict):
                normalized = {}
                for key, value in params.items():
                    normalized[key] = value
                    # snake_case → camelCase
                    camel = re.sub(r'_([a-z])', lambda m: m.group(1).upper(), key)
                    if camel != key and camel not in params:
                        normalized[camel] = value
                # 数值字符串自动转换（排除密码、助记词等不应转换的字段）
                _NO_CONVERT = {
                    'password', 'mnemonic', 'privateKey', 'private_key',
                    'address', 'toAddress', 'to_address', 'fromAddress', 'from_address',
                    'hash', 'txHash', 'tx_hash', 'blockHash', 'block_hash',
                    'signature', 'publicKey', 'public_key', 'keyId', 'key_id',
                    'nonce', 'salt', 'data', 'code', 'token', 'secret', 'seed',
                    'name', 'title', 'description', 'content', 'message', 'reason',
                    'minerAddress', 'miner_address', 'minerIp', 'miner_ip',
                    'proposalId', 'proposal_id', 'orderId', 'order_id', 'taskId', 'task_id',
                }
                for key, value in list(normalized.items()):
                    if key in _NO_CONVERT:
                        continue
                    if isinstance(value, str) and value.strip():
                        try:
                            if '.' in value:
                                normalized[key] = float(value)
                            elif value.lstrip('-').isdigit():
                                normalized[key] = int(value)
                        except (ValueError, AttributeError):
                            pass
                params = normalized

                # 将认证上下文注入到支持该参数的处理器，避免未授权数据访问
                try:
                    sig = inspect.signature(handler)
                    supports_auth_context = (
                        "auth_context" in sig.parameters
                        or any(
                            p.kind == inspect.Parameter.VAR_KEYWORD
                            for p in sig.parameters.values()
                        )
                    )
                    if supports_auth_context:
                        params["auth_context"] = auth_context
                except (TypeError, ValueError):
                    pass
            
            # 先尝试用 **kwargs 调用，失败则用位置参
            try:
                if isinstance(params, dict):
                    result = handler(**params)
                else:
                    result = handler(params)
            except TypeError:
                # 可能是旧风格方法，用位置参数重试
                result = handler(params)
            
            return RPCResponse.success(result, request.id)
        except RPCError as e:
            return RPCResponse.make_error(e.code, e.message, request.id)
        except Exception as e:
            # 安全加固：不向客户端泄露内部错误详情
            # Security: Do not leak internal error details to clients
            import logging
            logging.getLogger('rpc').error(f"RPC error [{request.method}]: {e}")
            return RPCResponse.make_error(
                RPCErrorCode.INTERNAL_ERROR.value,
                "Internal server error",
                request.id
            )
    
    def _check_permission(self, required: RPCPermission, auth_context: Dict) -> bool:
        """检查权限"""
        if required == RPCPermission.PUBLIC:
            return True
        
        if required == RPCPermission.USER:
            return bool(auth_context.get('user_address'))
        
        if required == RPCPermission.MINER:
            return bool(auth_context.get('miner_id'))
        
        if required == RPCPermission.ADMIN:
            return bool(auth_context.get('is_admin'))
        
        return False
    
    # ============== 交易方法实现 ==============
    
    def _tx_send(self, params: Dict) -> Dict:
        """发送交易 - 验证并加入 mempool
        
        D-05 fix: 必须提供 signature 和 public_key 字段（coinbase 除外）。
        """
        # 支持多种参数格式
        tx_data = params.get('transaction') or params.get('signedTx') or params.get('tx')
        if not tx_data:
            raise RPCError(RPCErrorCode.INVALID_PARAMS.value, "Missing transaction data")
        
        # 确保有 tx_id
        import uuid, hashlib as _hl
        tx_id = tx_data.get('tx_id', tx_data.get('txid', ''))
        if not tx_id:
            tx_id = _hl.sha256(f"tx_{uuid.uuid4().hex}".encode()).hexdigest()
            tx_data['tx_id'] = tx_id
        
        # D-05 fix: 非 coinbase 交易必须有签名
        tx_type = tx_data.get('tx_type', 'transfer')
        if tx_type != 'coinbase':
            if not tx_data.get('signature') or not tx_data.get('public_key'):
                return {
                    "txid": tx_id,
                    "status": "rejected",
                    "error": "Transaction requires signature and public_key fields"
                }
            
            # 验证签名真实性
            try:
                from core.crypto import ECDSASigner
                from_addr = tx_data.get('from', tx_data.get('from_address', ''))
                to_addr = tx_data.get('to', tx_data.get('to_address', ''))
                amount = tx_data.get('amount', 0)
                fee = tx_data.get('fee', 0)
                sig_data = f"{from_addr}{to_addr}{amount}{fee}".encode()
                pub_bytes = bytes.fromhex(tx_data['public_key'])
                sig_bytes = bytes.fromhex(tx_data['signature'])
                if not ECDSASigner.verify(pub_bytes, sig_data, sig_bytes):
                    return {"txid": tx_id, "status": "rejected", "error": "Invalid signature"}
                # 验证公钥与发送地址匹配
                derived_addr = ECDSASigner.public_key_to_address(pub_bytes)
                if from_addr and derived_addr != from_addr:
                    return {"txid": tx_id, "status": "rejected", "error": "Public key does not match sender address"}
            except Exception:
                return {"txid": tx_id, "status": "rejected", "error": "Signature verification failed"}
            
            # 验证余额充足
            if self.utxo_store:
                try:
                    from_addr = tx_data.get('from', tx_data.get('from_address', ''))
                    sector = tx_data.get('sector', 'MAIN')
                    amount = float(tx_data.get('amount', 0))
                    fee = float(tx_data.get('fee', 0))
                    balance = self.utxo_store.get_balance(from_addr, sector)
                    if balance < amount + fee:
                        return {"txid": tx_id, "status": "rejected", "error": f"Insufficient balance: need {amount + fee}, have {balance}"}
                except Exception as e:
                    import logging
                    logging.getLogger('rpc').error(f"Balance check failed for {tx_id}: {e}")
                    return {"txid": tx_id, "status": "rejected", "error": "Balance verification unavailable"}
        
        # 提交到共识引擎 pending 池
        if self.consensus_engine:
            accepted = self.consensus_engine.add_transaction(tx_data)
            if not accepted:
                return {
                    "txid": tx_id,
                    "status": "rejected",
                    "error": "Transaction rejected (duplicate or invalid fields)"
                }
        
        # P2P 广播到其他节点
        if self.p2p_network and hasattr(self.p2p_network, 'broadcast'):
            try:
                from core.tcp_network import MessageType, P2PMessage
                import asyncio
                msg = P2PMessage(
                    msg_type=MessageType.NEW_TX,
                    sender_id=self.p2p_network.node_id,
                    payload=tx_data
                )
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.run_coroutine_threadsafe(self.p2p_network.broadcast(msg), loop)
            except Exception:
                pass  # P2P 广播失败不影响本地提交
        
        return {
            "txid": tx_id,
            "status": "pending",
            "from": tx_data.get("from", tx_data.get("from_address", "")),
            "to": tx_data.get("to", tx_data.get("to_address", "")),
            "amount": tx_data.get("amount", 0),
        }
    
    def _tx_get(self, txid: str = "", **kwargs) -> Optional[Dict]:
        """获取交易详情 - UTXO 存储获取"""
        if not txid:
            return None
        
        # utxo_store 获取交易
        if self.utxo_store:
            try:
                tx = self.utxo_store.get_transaction(txid)
                if tx:
                    return {
                        "txId": tx.txid,
                        "txType": tx.tx_type,
                        "from": tx.from_address,
                        "to": tx.to_address,
                        "amount": tx.amount,
                        "fee": tx.fee,
                        "coinType": tx.sector,
                        "blockHeight": tx.block_height,
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.localtime(tx.timestamp)),
                        "status": tx.status,
                        "inputs": tx.inputs,
                        "outputs": tx.outputs,
                        "memo": tx.memo,
                    }
            except Exception as e:
                print(f"获取交易失败: {e}")
        
        return {"txid": txid, "status": "unknown"}

    def _tx_get_by_address(self, address: str = "", limit: int = 50, **kwargs) -> List[Dict]:
        """按地址获取交易历史"""
        target_address = address or self.miner_address
        if not target_address:
            return []
        
        if self.utxo_store:
            try:
                txs = self.utxo_store.get_transaction_history(target_address, limit)
                result = []
                for tx in txs:
                    result.append({
                        "txId": tx.txid,
                        "txType": tx.tx_type,
                        "from": tx.from_address,
                        "to": tx.to_address,
                        "amount": tx.amount,
                        "coinType": tx.sector,
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.localtime(tx.timestamp)),
                        "status": tx.status,
                    })
                return result
            except Exception as e:
                print(f"获取交易历史失败: {e}")
        
        return []
    
    # ============== 内存池方==============
    
    def _mempool_get_info(self, params: Dict) -> Dict:
        """获取内存池信息"""
        if self.mempool:
            return self.mempool.get_mempool_info()
        return {"size": 0, "bytes": 0}
    
    def _mempool_get_pending(self, params: Dict) -> List[Dict]:
        """获取待处理交易"""
        return []
    
    # ============== 区块方法 ==============
    
    def _block_to_frontend(self, block) -> Dict:
        """将 Block 对象转换为前端 Block 接口格式。"""
        ct = getattr(block, 'consensus_type', None)
        consensus_str = ct.value if hasattr(ct, 'value') else str(ct) if ct else 'POW'
        result = {
            "height": block.height,
            "hash": block.hash,
            "prevHash": block.prev_hash,
            "timestamp": block.timestamp,
            "miner": getattr(block, 'miner_address', '') or getattr(block, 'miner_id', ''),
            "sector": block.sector,
            "txCount": len(block.transactions),
            "reward": block.block_reward,
            "difficulty": block.difficulty,
            "nonce": block.nonce,
            "size": block.get_size() if hasattr(block, 'get_size') else 1024,
            "consensusType": consensus_str,
        }
        # S-Box PoUW 数据
        sbox_hex = getattr(block, 'sbox_hex', '')
        if sbox_hex:
            result["sbox"] = {
                "score": round(getattr(block, 'sbox_score', 0), 4),
                "nonlinearity": getattr(block, 'sbox_nonlinearity', 0),
                "diffUniformity": getattr(block, 'sbox_diff_uniformity', 0),
                "avalanche": round(getattr(block, 'sbox_avalanche', 0), 4),
                "selectedSector": getattr(block, 'sbox_selected_sector', ''),
                "allSectors": getattr(block, 'sbox_all_sectors', []),
                "scoreThreshold": round(getattr(block, 'sbox_score_threshold', 0), 4),
            }
        return result

    def _block_dict_to_frontend(self, block_dict: Dict) -> Dict:
        """将 DB 中的 block_dict 转换为前端 Block 接口格式。"""
        result = {
            "height": block_dict.get('height', 0),
            "hash": block_dict.get('hash', ''),
            "prevHash": block_dict.get('prev_hash', ''),
            "timestamp": block_dict.get('timestamp', 0),
            "miner": block_dict.get('miner_address', '') or block_dict.get('miner_id', ''),
            "sector": block_dict.get('sector', ''),
            "txCount": len(block_dict.get('transactions', [])),
            "reward": block_dict.get('block_reward', 0),
            "difficulty": block_dict.get('difficulty', 4),
            "nonce": block_dict.get('nonce', 0),
            "size": 1024,
            "consensusType": block_dict.get('consensus_type', 'POW'),
        }
        sbox_hex = block_dict.get('sbox_hex', '')
        if sbox_hex:
            result["sbox"] = {
                "score": round(block_dict.get('sbox_score', 0), 4),
                "nonlinearity": block_dict.get('sbox_nonlinearity', 0),
                "diffUniformity": block_dict.get('sbox_diff_uniformity', 0),
                "avalanche": round(block_dict.get('sbox_avalanche', 0), 4),
                "selectedSector": block_dict.get('sbox_selected_sector', ''),
                "allSectors": block_dict.get('sbox_all_sectors', []),
                "scoreThreshold": round(block_dict.get('sbox_score_threshold', 0), 4),
            }
        return result

    def _block_get_latest(self, sector: str = None, limit: int = 20, **kwargs) -> Dict:
        """获取最新区块列表。"""
        blocks = []
        if self.consensus_engine:
            chain = self.consensus_engine.chain
            for i in range(len(chain) - 1, max(-1, len(chain) - limit - 1), -1):
                block = chain[i]
                if sector and block.sector != sector:
                    continue
                blocks.append(self._block_to_frontend(block))
        return {"blocks": blocks, "total": len(blocks)}
    
    def _block_get_by_height(self, height: int = 0, sector: str = None, **kwargs) -> Optional[Dict]:
        """按高度获取区块。"""
        if self.consensus_engine:
            # 先从内存缓存查找
            for b in self.consensus_engine.chain:
                if b.height == height:
                    result = self._block_to_frontend(b)
                    result['transactions'] = b.transactions
                    return result
            # 再从 DB 查找
            try:
                import json as _json
                with self.consensus_engine._db_lock:
                    row = self.consensus_engine._db_conn.execute(
                        "SELECT block_data FROM blocks WHERE height = ?", (height,)
                    ).fetchone()
                if row:
                    block_dict = _json.loads(row['block_data'])
                    result = self._block_dict_to_frontend(block_dict)
                    result['transactions'] = block_dict.get('transactions', [])
                    return result
            except Exception:
                pass
        return {"height": height, "error": "Block not found"}

    def _block_get_by_hash(self, hash: str = "", **kwargs) -> Optional[Dict]:
        """按哈希获取区块。"""
        if self.consensus_engine and hash:
            # 先从内存缓存查找
            for b in self.consensus_engine.chain:
                if b.hash == hash:
                    result = self._block_to_frontend(b)
                    result['transactions'] = b.transactions
                    return result
            # 再从 DB 查找
            try:
                import json as _json
                with self.consensus_engine._db_lock:
                    row = self.consensus_engine._db_conn.execute(
                        "SELECT block_data FROM blocks WHERE hash = ?", (hash,)
                    ).fetchone()
                if row:
                    block_dict = _json.loads(row['block_data'])
                    result = self._block_dict_to_frontend(block_dict)
                    result['transactions'] = block_dict.get('transactions', [])
                    return result
            except Exception:
                pass
        return None
    
    # ============== 链方==============
    
    def _chain_get_height(self, sector: str = None, **kwargs) -> Dict:
        """获取链高- 从共识引擎获取真实高"""
        height = 0
        timestamp = time.time()
        if self.consensus_engine:
            height = self.consensus_engine.get_chain_height()
            if self.consensus_engine.chain:
                timestamp = self.consensus_engine.chain[-1].timestamp
        return {"height": height, "timestamp": timestamp}

    def _chain_get_info(self, sector: str = None, **kwargs) -> Dict:
        """获取链信息 - 从共识引擎获取真实数据"""
        height = 0
        total_transactions = 0
        last_block_time = 0
        difficulty = 4
        
        if self.consensus_engine:
            height = self.consensus_engine.get_chain_height()
            difficulty = self.consensus_engine.current_difficulty
            
            # 计算总交易数
            for block in self.consensus_engine.chain:
                total_transactions += len(block.transactions)
            
            # 获取最新区块时
            if self.consensus_engine.chain:
                last_block_time = self.consensus_engine.chain[-1].timestamp
        
        result = {
            "height": height,
            "sector": sector or (self.consensus_engine.sector if self.consensus_engine else "GENERAL"),
            "totalBlocks": height + 1,
            "totalTransactions": total_transactions,
            "difficulty": difficulty,
            "lastBlockTime": last_block_time,
            "syncing": self.is_syncing,
        }
        # S-Box 挖矿状态
        if self.consensus_engine:
            result["sboxMiningEnabled"] = getattr(self.consensus_engine, '_sbox_mining_enabled', False)
            result["consensusMode"] = getattr(self.consensus_engine, 'consensus_mode', 'mixed')
            result["consensusSboxRatio"] = getattr(self.consensus_engine, 'consensus_sbox_ratio', 0.5)
            result["consensusPouwSupportRatio"] = getattr(
                self.consensus_engine,
                'consensus_pouw_support_ratio',
                0.1,
            )

            # 混用共识观测统计（若共识引擎可提供）
            try:
                chain_info = self.consensus_engine.get_chain_info()
                selected_dist = chain_info.get("consensus_selected_distribution")
                mined_dist = chain_info.get("consensus_mined_distribution")
                mechanism_strategy = chain_info.get("mechanism_strategy")
                if selected_dist is not None:
                    result["consensusSelectedDistribution"] = selected_dist
                if mined_dist is not None:
                    result["consensusMinedDistribution"] = mined_dist
                if mechanism_strategy is not None:
                    result["mechanismStrategy"] = mechanism_strategy
            except Exception:
                pass

            try:
                from core.sbox_engine import get_sbox_library
                sbox_lib = get_sbox_library()
                current = sbox_lib.current
                if current:
                    result["currentSbox"] = {
                        "score": round(current.score, 4),
                        "sector": current.sector,
                        "nonlinearity": current.nonlinearity,
                    }
                result["sboxLibrarySize"] = sbox_lib.size()
            except ImportError:
                pass
        return result

    def _chain_update_mechanism_strategy(self,
                                         version: str = None,
                                         rollout: str = None,
                                         mode: str = None,
                                         sbox_ratio: float = None,
                                         sbox_enabled: bool = None,
                                         work_threshold: float = None,
                                         max_ratio_step: float = None,
                                         rollbackToPrevious: bool = False,
                                         **kwargs) -> Dict:
        """更新机制策略（版本化参数 + 灰度 + 回滚）。"""
        if not self.consensus_engine or not hasattr(self.consensus_engine, "configure_mechanism_strategy"):
            return {
                "status": "failed",
                "message": "consensus_engine_unavailable",
            }

        actor = kwargs.get("auth_context", {}).get("user", "rpc_admin")
        strategy = self.consensus_engine.configure_mechanism_strategy(
            actor_id=actor,
            rollback_to_previous=bool(rollbackToPrevious),
            version=version,
            rollout=rollout,
            mode=mode,
            sbox_ratio=sbox_ratio,
            sbox_enabled=sbox_enabled,
            work_threshold=work_threshold,
            max_ratio_step=max_ratio_step,
        )
        return {
            "status": "success",
            "strategy": strategy,
        }

    def _sbox_get_encryption_policy(self, **kwargs) -> Dict:
        """获取 S-Box 加密策略。"""
        try:
            from core.sbox_crypto import get_sbox_encryption_policy
            return {
                "status": "success",
                "policy": get_sbox_encryption_policy(),
            }
        except Exception as e:
            return self._rpc_internal_error("sbox_getEncryptionPolicy", e, {
                "status": "failed",
                "policy": None,
                "message": f"policy_unavailable:{type(e).__name__}",
            })

    def _sbox_set_encryption_policy(self,
                                    policyVersion: str = None,
                                    defaultLevel: str = None,
                                    enforceEnhancedDefault: bool = None,
                                    allowDowngradeToStandard: bool = None,
                                    downgradeRequiresAudit: bool = None,
                                    maxSessionMessages: int = None,
                                    maxSessionSeconds: int = None,
                                    **kwargs) -> Dict:
        """设置 S-Box 加密策略。"""
        try:
            from core.sbox_crypto import set_sbox_encryption_policy
            policy = set_sbox_encryption_policy(
                policyVersion=policyVersion,
                defaultLevel=defaultLevel,
                enforceEnhancedDefault=enforceEnhancedDefault,
                allowDowngradeToStandard=allowDowngradeToStandard,
                downgradeRequiresAudit=downgradeRequiresAudit,
                maxSessionMessages=maxSessionMessages,
                maxSessionSeconds=maxSessionSeconds,
            )
            return {
                "status": "success",
                "policy": policy,
            }
        except Exception as e:
            return self._rpc_internal_error("sbox_setEncryptionPolicy", e, {
                "status": "failed",
                "policy": None,
                "message": f"policy_update_failed:{type(e).__name__}:{e}",
            })

    def _sbox_get_downgrade_audit(self, limit: int = 200, **kwargs) -> Dict:
        """查询 S-Box 降级审计事件。"""
        try:
            from core.sbox_crypto import get_sbox_downgrade_audit
            n = max(1, min(int(limit), 5000))
            events = get_sbox_downgrade_audit(n)
            return {
                "status": "success",
                "events": events,
                "total": len(events),
            }
        except Exception as e:
            return self._rpc_internal_error("sbox_getDowngradeAudit", e, {
                "status": "failed",
                "events": [],
                "total": 0,
                "message": f"audit_unavailable:{type(e).__name__}",
            })
    
    # ============== 账户方法 ==============
    
    def _account_get_balance(self, address: str = None, sector: str = None, **kwargs) -> Dict:
        """获取余额 - 区分 MAIN 币和板块币
        
        返回:
            mainBalance: 真正MAIN 币余额（通过兑换获得）
            sectorTotal: 板块币总余额（通过挖矿获得，含未成熟）
            balance: 向后兼容，等于 mainBalance
            sectorBalances: 各板块币明细（总余额，含未成熟 coinbase）
            availableSectorBalances: 各板块币可转账余额（仅已成熟的 UTXO）
        """
        target_address = address or self.miner_address
        
        if not target_address:
            return {
                "balance": 0, 
                "mainBalance": 0,
                "sectorTotal": 0,
                "sectorBalances": {}, 
                "availableSectorBalances": {},
                "address": ""
            }
        
        # 1. 获取 MAIN 余额（优先从 UTXO 获取可转账余额）
        main_balance = 0.0
        if self.utxo_store:
            try:
                main_balance = self.utxo_store.get_balance(target_address, "MAIN")
            except Exception:
                pass
        # 回退：如果 UTXO 余额为 0，也检查 exchange 记录
        if main_balance == 0.0:
            try:
                from .dual_witness_exchange import get_exchange_service
                exchange = get_exchange_service()
                main_balance = exchange.get_main_balance(target_address)
            except Exception:
                pass
        
        # 2. 获取板块币余额（综合 UTXO 和 sector_ledger 两个来源）
        sector_total = 0.0
        sector_balances = {}           # 总余额（含未成熟 coinbase）
        available_sector_balances = {} # 可转账余额（仅已成熟 UTXO）
        has_utxo_records = False
        
        if self.utxo_store:
            try:
                # 总余额（含未成熟 coinbase），用于展示
                utxo_total_balances = self.utxo_store.get_all_total_balances(target_address)
                # 可转账余额（仅已成熟），用于转账校验
                utxo_spendable = self.utxo_store.get_all_balances(target_address)
                
                for s, bal in utxo_total_balances.items():
                    if s != "MAIN":
                        if sector and s != sector:
                            continue
                        sector_balances[s] = bal
                        sector_total += bal
                        has_utxo_records = True
                
                for s, bal in utxo_spendable.items():
                    if s != "MAIN":
                        if sector and s != sector:
                            continue
                        available_sector_balances[s] = bal
            except Exception:
                pass
        
        # 回退/补充：仅在 UTXO 完全无记录时从 sector_ledger 获取
        if not has_utxo_records and not sector_balances and self.sector_ledger:
            from .sector_coin import SectorCoinType
            
            if sector:
                # 查询特定板块
                coin_type = SectorCoinType.from_sector(sector)
                bal = self.sector_ledger.get_balance(target_address, coin_type)
                sector_balances[sector] = bal.balance
                available_sector_balances[sector] = bal.balance
                sector_total = bal.balance
            else:
                # 查询所有板块
                all_balances = self.sector_ledger.get_all_balances(target_address)
                for coin_type, bal in all_balances.items():
                    sector_balances[coin_type.sector] = bal.balance
                    available_sector_balances[coin_type.sector] = bal.balance
                    sector_total += bal.balance
        
        return {
            "balance": round(main_balance, 4),  # 向后兼容：真正的 MAIN 余额
            "mainBalance": round(main_balance, 4),  # 真正MAIN 币
            "sectorTotal": round(sector_total, 4),  # 板块币总和
            "sectorBalances": sector_balances,        # 总余额（含未成熟）
            "availableSectorBalances": available_sector_balances,  # 可转账余额
            "address": target_address,
        }
    
    def _account_get_utxos(self, address: str = None, **kwargs) -> List[Dict]:
        """获取指定地址的可UTXO 列表"""
        target_address = address or self.miner_address
        if not target_address:
            return []
        
        if not self.utxo_store:
            return []
        
        try:
            utxos = self.utxo_store.get_spendable_utxos(target_address)
            result = []
            for utxo in utxos:
                result.append({
                    "txId": utxo.txid,
                    "outputIndex": utxo.output_index,
                    "amount": utxo.amount,
                    "coinType": utxo.sector,
                    "sourceType": utxo.source_type,  # coinbase transfer
                    "blockHeight": utxo.block_height,
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.localtime(utxo.created_at)),
                })
            return result
        except Exception as e:
            print(f"获取 UTXO 失败: {e}")
            return []
    
    def _account_trace_utxo(self, txid: str, output_index: int = 0, **kwargs) -> Dict:
        """追溯 UTXO 来源直到 coinbase
        
        参数:
            txid: 交易ID
            output_index: 输出索引（默0
        
        返回:
            完整的资金链路，从当前交易追溯到 coinbase
        """
        if not self.utxo_store:
            return {"success": False, "error": "UTXO 存储未初始化"}
        
        try:
            trace = self.utxo_store.trace_utxo_origin(txid, output_index)
            
            # 格式化追溯结
            formatted_trace = []
            for item in trace:
                formatted_trace.append({
                    "txId": item["txid"],
                    "txType": item["tx_type"],
                    "amount": item["amount"],
                    "coinType": item["sector"],
                    "from": item["from"],
                    "to": item["to"],
                    "blockHeight": item["block_height"],
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.localtime(item["timestamp"])),
                })
            
            return {
                "success": True,
                "trace": formatted_trace,
                "depth": len(formatted_trace),
                "originType": formatted_trace[-1]["txType"] if formatted_trace else None,
                "originTxId": formatted_trace[-1]["txId"] if formatted_trace else None
            }
        except Exception as e:
            print(f"追溯 UTXO 失败: {e}")
            return {"success": False, "error": "internal_error"}
    
    def _account_get_nonce(self, address: str = None, **kwargs) -> int:
        """获取地址的下一个可用 nonce"""
        target_address = address or self.miner_address
        if self.consensus_engine and target_address:
            return self.consensus_engine.get_nonce(target_address)
        return 0
    
    def _account_get_transactions(self, address: str = None, limit: int = 20, **kwargs) -> Dict:
        """获取交易历史（包括板块币交易和兑换交易）"""
        target_address = address or self.miner_address
        txs = []
        
        # 从板块币账本获取交易记录
        if self.sector_ledger and target_address:
            try:
                with self.sector_ledger._conn() as conn:
                    rows = conn.execute("""
                        SELECT tx_id, coin_type, from_address, to_address, amount, timestamp, block_height, tx_type
                        FROM transfers
                        WHERE from_address = ? OR to_address = ?
                        ORDER BY timestamp DESC
                        LIMIT ?
                    """, (target_address, target_address, limit)).fetchall()
                    
                    for row in rows:
                        txs.append({
                            "txId": row["tx_id"],
                            "from": row["from_address"],
                            "to": row["to_address"],
                            "amount": row["amount"],
                            "coin": row["coin_type"],
                            "status": "confirmed",
                            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.localtime(row["timestamp"])),
                            "blockHeight": row["block_height"],
                            "txType": row["tx_type"],
                        })
            except Exception as e:
                print(f"获取交易历史失败: {e}")
        
        # UTXO 存储获取兑换交易（exchange 类型
        if target_address:
            try:
                import sqlite3
                import os
                # 使用代码所在目录的相对路径
                base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                utxo_db_path = os.path.join(base_dir, "data", "utxo.db")
                if os.path.exists(utxo_db_path):
                    conn = sqlite3.connect(utxo_db_path)
                    conn.row_factory = sqlite3.Row
                    cursor = conn.cursor()
                    rows = cursor.execute("""
                        SELECT txid, tx_type, from_address, to_address, amount, timestamp, block_height, memo, status
                        FROM transactions
                        WHERE (from_address = ? OR to_address = ?) AND tx_type = 'exchange'
                        ORDER BY timestamp DESC
                        LIMIT ?
                    """, (target_address, target_address, limit)).fetchall()
                    
                    for row in rows:
                        txs.append({
                            "txId": row["txid"][:16] if row["txid"] else "",
                            "from": row["from_address"],
                            "to": row["to_address"],
                            "amount": row["amount"],
                            "coin": "MAIN",
                            "status": row["status"] or "confirmed",
                            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.localtime(row["timestamp"])),
                            "blockHeight": row["block_height"] or 0,
                            "txType": "EXCHANGE",
                            "memo": row["memo"],
                        })
                    conn.close()
            except Exception as e:
                print(f"获取UTXO交易历史失败: {e}")
        
        # 按时间排
        txs.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        
        return {"transactions": txs[:limit], "total": len(txs)}
    
    def _account_get_sub_addresses(self, address: str = None, **kwargs) -> List[Dict]:
        """获取子地址列表"""
        target_address = address or self.miner_address
        if not target_address:
            return []
        return self.sub_addresses.get(target_address, [])
    
    def _account_create_sub_address(self, label: str = "新地址", **kwargs) -> Dict:
        """创建子地址"""
        if not self.miner_address:
            return {"error": "未连接钱包"}
        
        sub_addr = {
            "address": f"SUB_{uuid.uuid4().hex[:16]}",
            "label": label,
            "balance": 0,
            "usageCount": 0,
            "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        
        if self.miner_address not in self.sub_addresses:
            self.sub_addresses[self.miner_address] = []
        self.sub_addresses[self.miner_address].append(sub_addr)
        
        return sub_addr
    
    # ============== 钱包方法 ==============
    
    def _wallet_create(self, password: str = "", **kwargs) -> Dict:
        """创建新钱包（生成助记词和加密密钥文件）
        
        安全注意事项
        - 助记词仅在创建时返回一次，之后无法再次获取
        - 强烈建议用户立即备份助记词和下载加密keystore 文件
        - 密码用于加密 keystore，不会存储在服务
        - 建议通过 HTTPS 传输
        """
        # 防御性转换：确保 password 为字符串
        password = str(password) if password is not None else ""
        try:
            from .crypto import ProductionWallet
            from .wallet import WalletInfo
            import json
            import base64
            import hashlib
            import os
            
            # 验证密码强度
            if password and len(password) < 8:
                return {
                    "success": False,
                    "error": "weak_password",
                    "message": "密码至少需要8个字符"
                }
            
            # 使用 ProductionWallet 保证地址格式与 main.py 一致
            # 注意: passphrase 别传 password，否则同一助记词 + 不同密码 = 不同地址 (BIP-39 规范)
            # password 仅用于加密 keystore 文件
            prod_wallet = ProductionWallet.create(word_count=12)
            
            # 构造兼容的 WalletInfo（保持 RPC 层接口不变）
            wallet_info = WalletInfo(
                mnemonic=prod_wallet.mnemonic,
                master_private_key=prod_wallet.keypair.private_key.hex(),
                addresses=prod_wallet.addresses,
                public_keys={"MAIN": prod_wallet.keypair.public_key.hex()},
            )
            
            # 保存钱包信息（内存中临时保存，用于后续操作）
            self.wallet_info = wallet_info
            self.miner_address = wallet_info.addresses.get("MAIN", wallet_info.addresses.get("GPU", ""))
            
            # 设置钱包所有者
            auth_context = kwargs.get("auth_context", {})
            self._wallet_owner = self._get_auth_user(auth_context, "local_admin")
            
            # 生成加密的密钥文(Keystore)
            # 使用用户密码加密，即使密钥文件泄露，没有密码也无法解
            keystore = self._create_keystore(wallet_info.mnemonic, password)
            
            # 持久化 keystore 到 wallets/ 目录
            self._save_wallet_to_disk(keystore, self.miner_address)
            
            # 安全警告
            security_warnings = [
                "⚠️ 助记词仅显示一次，请立即备份到安全的离线位置",
                "⚠️ 请下载并保存加密的密钥文(keystore)",
                "⚠️ 切勿将助记词或密钥文件分享给任何人",
                "⚠️ 建议使用强密码保护密钥文件"
            ]
            
            result = {
                "success": True,
                "mnemonic": wallet_info.mnemonic,  # 仅创建时返回一次
                "address": self.miner_address,
                "addresses": wallet_info.addresses,
                "keystore": keystore,  # 加密的密钥文件内
                "keystoreFilename": f"keystore_{self.miner_address[:16]}.json",
                "message": "钱包创建成功！请立即备份助记词并下载密钥文件",
                "securityWarnings": security_warnings,
                "encryptionInfo": keystore.get("security", {})
            }
            
            # 安全: 返回后立即从内存中清除助记词和私钥明文
            # 后续操作只需地址和公钥，不需要助记词
            if self.wallet_info:
                object.__setattr__(self.wallet_info, 'mnemonic', None)
                object.__setattr__(self.wallet_info, 'master_private_key', None)
            
            return result
        except Exception as e:
            import logging
            logging.getLogger('rpc').error(f"Wallet creation error: {e}")
            return {
                "success": False,
                "error": "wallet_creation_failed",
                "message": "钱包创建失败 (wallet creation failed)"
            }
    
    def _create_keystore(self, mnemonic: str, password: str) -> Dict:
        """创建加密的密钥文(Keystore JSON) - 安全增强"""
        import hashlib
        import base64
        import os
        import json
        import time
        
        # M-10: 使用统一加密工具
        from .crypto_utils import aes_gcm_encrypt, derive_key_pbkdf2, random_bytes, get_backend
        
        # 生成盐(32 字节)
        salt = random_bytes(32)
        
        # 增加迭代次数310000 (OWASP 2023 推荐)
        iterations = 310000
        dk = derive_key_pbkdf2(password, salt, iterations)
        
        mnemonic_bytes = mnemonic.encode('utf-8')
        
        if get_backend() != "none":
            # 使用 AES-256-GCM 加密 (推荐)
            ciphertext, nonce, tag = aes_gcm_encrypt(mnemonic_bytes, dk)
            
            crypto_data = {
                "cipher": "aes-256-gcm",
                "ciphertext": base64.b64encode(ciphertext).decode(),
                "nonce": base64.b64encode(nonce).decode(),
                "tag": base64.b64encode(tag).decode(),  # 认证标签
                "kdf": "pbkdf2-sha256",
                "kdfparams": {
                    "dklen": 32,
                    "salt": base64.b64encode(salt).decode(),
                    "c": iterations,
                    "prf": "hmac-sha256"
                }
            }
        else:
            # [SECURITY] 不允许使用 XOR 导出密钥文件
            return {
                "success": False,
                "error": "encryption_unavailable",
                "message": "导出密钥文件需要安pycryptodome  pip install pycryptodome"
            }
        
        # 安全清除派生密钥
        dk = b'\x00' * 32
        
        keystore = {
            "version": 3,  # Keystore V3 格式
            "id": hashlib.sha256(os.urandom(32)).hexdigest()[:36],
            "address": self.miner_address,
            "crypto": crypto_data,
            "created_at": int(time.time()),
            "chain": "POUW-MainNet",
            "security": {
                "encryption": crypto_data.get("cipher", "aes-256-gcm"),
                "kdf_iterations": iterations
            }
        }
        
        return keystore
    
    def _wallet_export_keystore(self, password: str = "", **kwargs) -> Dict:
        """导出加密密钥文件"""
        # 防御性转换：确保 password 为字符串
        password = str(password) if password is not None else ""
        # 验证钱包所有权
        auth_context = kwargs.get("auth_context", {})
        caller = self._get_auth_user(auth_context, "local_admin")
        if hasattr(self, '_wallet_owner') and self._wallet_owner and caller != self._wallet_owner:
            return {"success": False, "error": "permission_denied", "message": "无权导出此钱包"}
        
        if not self.miner_address:
            return {
                "success": False,
                "error": "no_wallet",
                "message": "请先创建或导入钱包"
            }
        
        if not hasattr(self, 'wallet_info') or not self.wallet_info:
            return {
                "success": False,
                "error": "wallet_not_loaded",
                "message": "钱包信息未加载，请重新导入"
            }
        
        try:
            keystore = self._create_keystore(self.wallet_info.mnemonic, password)
            return {
                "success": True,
                "keystore": keystore,
                "filename": f"keystore_{self.miner_address[:16]}.json",
                "message": "密钥文件导出成功"
            }
        except Exception as e:
            import logging
            logging.getLogger('rpc').error(f"Wallet export error: {e}")
            return {
                "success": False,
                "error": "export_failed",
                "message": "导出失败 (export failed)"
            }
    
    def _wallet_import_keystore(self, keystore: Dict, password: str, **kwargs) -> Dict:
        """从加密密钥文件导入钱- 支持 AES-256-GCM 和旧XOR"""
        import hashlib
        import base64
        
        # 防御性转换：确保 password 为字符串（防止数值自动转换）
        password = str(password) if password is not None else ""
        
        try:
            if isinstance(keystore, str):
                import json
                keystore = json.loads(keystore)
            
            mnemonic, decrypt_err = self._decrypt_keystore_mnemonic(keystore, password)
            if decrypt_err:
                return {
                    "success": False,
                    "error": "invalid_password",
                    "message": decrypt_err,
                }
            
            # 安全加固：使用用户提供的密码恢复钱包（不再使用 SSH_PASSWORD） 环境变量
            # Security: Use the user-provided password, not SSH_PASSWORD env var
            return self._wallet_import(mnemonic=mnemonic, password=password, **kwargs)
            
        except UnicodeDecodeError:
            return {
                "success": False,
                "error": "invalid_password",
                "message": "密码错误"
            }
        except Exception as e:
            import logging
            logging.getLogger('rpc').error(f"Keystore import error: {e}")
            return {
                "success": False,
                "error": "import_failed",
                "message": "密钥文件导入失败 (keystore import failed)"
            }
    
    def _wallet_import(self, mnemonic: str, password: str = "", **kwargs) -> Dict:
        """从助记词导入钱包"""
        # 防御性转换：确保 password/mnemonic 为字符串
        password = str(password) if password is not None else ""
        mnemonic = str(mnemonic) if mnemonic is not None else ""
        try:
            from .crypto import ProductionWallet
            from .wallet import WalletInfo
            
            try:
                # passphrase 始终为空，确保同一助记词始终派生相同地址
                # password 仅用于加密 keystore 文件
                prod_wallet = ProductionWallet.from_mnemonic(mnemonic.strip())
            except ValueError:
                return {
                    "success": False,
                    "error": "无效的助记词",
                    "message": "请检查助记词是否正确（12或24个单词）"
                }
            
            # 构造兼容的 WalletInfo
            wallet_info = WalletInfo(
                mnemonic=prod_wallet.mnemonic,
                master_private_key=prod_wallet.keypair.private_key.hex(),
                addresses=prod_wallet.addresses,
                public_keys={"MAIN": prod_wallet.keypair.public_key.hex()},
            )
            
            # 保存钱包信息并启动会
            self.wallet_info = wallet_info
            self.miner_address = wallet_info.addresses.get("MAIN", wallet_info.addresses.get("GPU", ""))
            self._wallet_session_start = time.time()  # 启动会话计时
            # Security: Track who owns this wallet session
            auth_context = kwargs.get("auth_context", {})
            self._wallet_owner = self._get_auth_user(auth_context, "local_admin")
            
            # 持久化 keystore（导入时也保存到磁盘）
            try:
                keystore = self._create_keystore(mnemonic, password)
                self._save_wallet_to_disk(keystore, self.miner_address)
            except Exception:
                pass  # 保存失败不影响导入
            
            return {
                "success": True,
                "address": self.miner_address,
                "addresses": wallet_info.addresses,
                "message": "钱包导入成功",
                "sessionTimeout": self.WALLET_SESSION_TIMEOUT
            }
        except Exception as e:
            import logging
            logging.getLogger('rpc').error(f"Wallet import error: {e}")
            return {
                "success": False,
                "error": "import_failed",
                "message": "钱包导入失败 (wallet import failed)"
            }
    
    def _wallet_get_info(self, **kwargs) -> Dict:
        """获取当前钱包信息"""
        # 检查会话是否过
        self._check_wallet_session()
        
        if not self.miner_address:
            return {
                "connected": False,
                "address": "",
                "addresses": {},
                "message": "未连接钱"
            }
        
        # 刷新会话
        self._refresh_wallet_session()
        
        addresses = {}
        if hasattr(self, 'wallet_info') and self.wallet_info:
            addresses = self.wallet_info.addresses
        else:
            addresses = {"MAIN": self.miner_address}
        
        # 获取余额（区MAIN 和板块币
        balance_info = self._account_get_balance()
        
        # 计算会话剩余时间
        session_remaining = 0
        if self._wallet_session_start:
            elapsed = time.time() - self._wallet_session_start
            session_remaining = max(0, self.WALLET_SESSION_TIMEOUT - elapsed)
        
        return {
            "connected": True,
            "address": self.miner_address,
            "addresses": addresses,
            "balance": balance_info.get("mainBalance", 0),  # 真正MAIN 余额
            "mainBalance": balance_info.get("mainBalance", 0),  # 真正MAIN 币
            "sectorTotal": balance_info.get("sectorTotal", 0),  # 板块币总和
            "sectorBalances": balance_info.get("sectorBalances", {}),
            "availableSectorBalances": balance_info.get("availableSectorBalances", {}),
            "sessionRemaining": int(session_remaining)
        }
    
    def _wallet_unlock(self, password: str, **kwargs) -> Dict:
        """解锁钱包（刷新会话）
        
        安全加固：必须验证密码才能解锁
        Security: Password verification required before unlock.
        """
        # 防御性转换：确保 password 为字符串
        password = str(password) if password is not None else ""
        if not password:
            return {
                "success": False,
                "message": "密码不能为空 / Password required"
            }
        
        if self.miner_address:
            # 防暴力破解：检查解锁尝试频率
            addr = self.miner_address
            now = time.time()
            attempts = self._unlock_attempts.get(addr, [])
            # 清除过期记录
            attempts = [t for t in attempts if now - t < self.UNLOCK_LOCKOUT_SECONDS]
            self._unlock_attempts[addr] = attempts
            if len(attempts) >= self.MAX_UNLOCK_ATTEMPTS:
                remaining = int(self.UNLOCK_LOCKOUT_SECONDS - (now - attempts[0]))
                return {
                    "success": False,
                    "message": f"解锁尝试过多，请 {remaining} 秒后重试 / Too many attempts, retry after {remaining}s"
                }
            auth_context = kwargs.get("auth_context", {})
            caller = self._get_auth_user(auth_context, "local_admin")
            if self._wallet_owner and caller != self._wallet_owner:
                return {
                    "success": False,
                    "message": "当前钱包会话属于其他用户 / Wallet session belongs to another user",
                }

            keystore = self._load_latest_keystore(self.miner_address)
            if not keystore:
                return {
                    "success": False,
                    "message": "未找到可解锁的钱包密钥文件 / No keystore found",
                }

            mnemonic, decrypt_err = self._decrypt_keystore_mnemonic(keystore, password)
            if decrypt_err:
                self._unlock_attempts.setdefault(addr, []).append(now)
                return {
                    "success": False,
                    "message": f"密码验证失败 / {decrypt_err}",
                }

            import_result = self._wallet_import(mnemonic=mnemonic, password=password, **kwargs)
            if not import_result.get("success"):
                return {
                    "success": False,
                    "message": import_result.get("message", "钱包解锁失败 / Wallet unlock failed"),
                }

            self._refresh_wallet_session()
            return {
                "success": True,
                "address": self.miner_address,
                "message": "钱包已解锁",
                "sessionTimeout": self.WALLET_SESSION_TIMEOUT,
            }
        return {
            "success": False,
            "message": "未找到钱/ No wallet found"
        }
    
    def _wallet_lock(self, **kwargs) -> Dict:
        """锁定钱包（清除会话）"""
        address = self.miner_address
        self._clear_wallet_session()
        return {
            "success": True,
            "message": "钱包已锁定，敏感数据已清",
            "previousAddress": address
        }
    
    def _wallet_transfer(
        self,
        toAddress: str,
        amount: float,
        sector: str = "MAIN",
        memo: str = "",
        **kwargs
    ) -> Dict:
        """发送转账交- 使用 UTXO 模型"""
        if not self.miner_address:
            return {
                "success": False,
                "error": "wallet_not_connected",
                "message": "请先创建或导入钱包"
            }
        
        # Security: Verify wallet session ownership
        auth_context = kwargs.get("auth_context", {})
        caller = self._get_auth_user(auth_context, "local_admin")
        if self._wallet_owner and caller != self._wallet_owner:
            return {
                "success": False,
                "error": "wallet_session_mismatch",
                "message": "当前钱包会话属于其他用户 (wallet session belongs to another user)"
            }
        
        if not toAddress:
            return {
                "success": False,
                "error": "invalid_address",
                "message": "收款地址不能为空"
            }
        
        if amount <= 0:
            return {
                "success": False,
                "error": "invalid_amount",
                "message": "转账金额必须大于0"
            }
        
        # 确定币种类型
        if sector == "MAIN":
            coin_type = "MAIN"
        else:
            coin_type = sector  # H100, RTX4090 
        
        # kwargs 中获取签名和公钥（由 RPC 请求参数传入）
        signature = kwargs.get('signature', '')
        public_key = kwargs.get('public_key', '')
        
        # 确定手续费（MAIN 0.01, 板块币 0.001）
        fee = 0.01 if coin_type == "MAIN" else 0.001
        
        # 如果前端未提供签名，服务端使用已解锁钱包的私钥自动签名
        # 生产环境禁用自动签名，要求前端完成签名
        import os
        is_production = os.environ.get('MAINCOIN_PRODUCTION', '').lower() == 'true'
        if (not signature or not public_key) and self.wallet_info and not is_production:
            pk_hex = getattr(self.wallet_info, 'master_private_key', None)
            pub_keys = getattr(self.wallet_info, 'public_keys', None)
            if pk_hex and pub_keys:
                try:
                    from .crypto import ECDSASigner
                    private_key_bytes = bytes.fromhex(pk_hex)
                    tx_message = f"{self.miner_address}{toAddress}{amount}{fee}".encode()
                    sig_bytes = ECDSASigner.sign(private_key_bytes, tx_message)
                    signature = sig_bytes.hex()
                    public_key = pub_keys.get("MAIN", "")
                except Exception as e:
                    import logging
                    logging.getLogger('rpc').warning(f"自动签名失败: {e}")
        
        # MAIN 转账 → 强制走双见证流程 (DR-5)
        if coin_type == "MAIN":
            if not self.main_transfer_engine:
                return {
                    "success": False,
                    "error": "engine_unavailable",
                    "message": "MAIN 转账引擎未初始化，请先启动挖矿"
                }
            try:
                success, message, transfer = self.main_transfer_engine.create_transfer(
                    from_address=self.miner_address,
                    to_address=toAddress,
                    amount=amount,
                    fee=fee,
                    signature=signature,
                    public_key=public_key,
                    memo=memo,
                )
                if success:
                    return {
                        "success": True,
                        "txid": transfer.transfer_id,
                        "from": self.miner_address,
                        "to": toAddress,
                        "amount": amount,
                        "sector": "MAIN",
                        "memo": memo,
                        "timestamp": int(time.time() * 1000),
                        "status": transfer.status.value.lower(),
                        "message": message,
                        "requiredWitnesses": transfer.required_witnesses,
                    }
                else:
                    return {
                        "success": False,
                        "error": "transfer_failed",
                        "message": message,
                    }
            except Exception as e:
                return {
                    "success": False,
                    "error": "transfer_error",
                    "message": "operation_failed"
                }
        
        # 非 MAIN（板块币）转账 → 直接 UTXO
        if self.utxo_store:
            try:
                result = self.utxo_store.create_transfer(
                    from_address=self.miner_address,
                    to_address=toAddress,
                    sector=coin_type,
                    amount=amount,
                    block_height=self.consensus_engine.get_chain_height() if self.consensus_engine else 0,
                    fee=fee,
                    signature=signature,
                    public_key=public_key,
                )
                
                if result["success"]:
                    # 同步 sector_ledger 余额（兑换系统依赖此余额）
                    if hasattr(self, 'sector_ledger') and self.sector_ledger:
                        try:
                            from core.sector_coin import SectorCoinType
                            ct = SectorCoinType.from_sector(coin_type)
                            self.sector_ledger.sync_transfer_from_utxo(
                                self.miner_address, toAddress, ct, amount
                            )
                        except Exception as e:
                            import logging
                            logging.getLogger('rpc').warning(f"sector_ledger 同步失败 (UTXO 已完成): {e}")
                    
                    # 板块币转账记录到板块区块链（无需双见证）
                    if self.consensus_engine:
                        try:
                            nonce = self.consensus_engine.get_nonce(self.miner_address)
                            accepted = self.consensus_engine.add_transaction({
                                "tx_id": result["txid"],
                                "tx_type": "transfer",
                                "from": self.miner_address,
                                "to": toAddress,
                                "amount": amount,
                                "fee": fee,
                                "sector": coin_type,
                                "nonce": nonce,
                                "signature": signature,
                                "public_key": public_key,
                                "memo": memo,
                                "timestamp": time.time(),
                                "inputs": result.get("inputs", []),
                            })
                            if not accepted:
                                import logging
                                logging.getLogger('rpc').warning(f"交易未被共识引擎接受: {result['txid'][:12]}")
                        except Exception as e:
                            import logging
                            logging.getLogger('rpc').warning(f"共识引擎提交失败: {e}")
                    return {
                        "success": True,
                        "txid": result["txid"],
                        "from": self.miner_address,
                        "to": toAddress,
                        "amount": amount,
                        "sector": sector,
                        "memo": memo,
                        "timestamp": int(time.time() * 1000),
                        "status": "confirmed",
                        "message": "转账成功",
                        "inputsUsed": result.get("inputs_used", 0),
                        "changeAmount": result.get("change_amount", 0)
                    }
                else:
                    return {
                        "success": False,
                        "error": "transfer_failed",
                        "message": result.get("error", "转账执行失败")
                    }
            except Exception as e:
                return {
                    "success": False,
                    "error": "transfer_error",
                    "message": "operation_failed"
                }
        
        # 降级sector_ledger（兼容旧代码
        if self.sector_ledger and sector != "MAIN":
            try:
                from .sector_coin import SectorCoinType
                coin_type_enum = SectorCoinType.from_sector(sector)
                success = self.sector_ledger.transfer(
                    self.miner_address,
                    toAddress,
                    coin_type_enum,
                    amount
                )
                if not success:
                    return {
                        "success": False,
                        "error": "transfer_failed",
                        "message": "转账执行失败"
                    }
                txid = f"tx_{uuid.uuid4().hex[:16]}"
            except Exception as e:
                return {
                    "success": False,
                    "error": "transfer_error",
                    "message": "operation_failed"
                }
        else:
            return {
                "success": False,
                "error": "no_transfer_engine",
                "message": "无可用的转账引擎 / No transfer engine available for this sector"
            }
        
        return {
            "success": True,
            "txid": txid,
            "from": self.miner_address,
            "to": toAddress,
            "amount": amount,
            "sector": sector,
            "memo": memo,
            "timestamp": int(time.time() * 1000),
            "status": "pending",
            "message": "交易已提"
        }
    
    def _miner_register(
        self,
        gpuType: str = "GPU",
        gpuCount: int = 1,
        pricePerHour: float = 1.0,
        description: str = "",
        sectors: List[str] = None,
        **kwargs
    ) -> Dict:
        """注册成为矿工/算力提供"""
        if not self.miner_address:
            return {
                "success": False,
                "error": "wallet_not_connected",
                "message": "请先创建或导入钱包"
            }
        
        miner_id = f"miner_{self.node_id}"
        
        # 初始化矿工注册表
        if not hasattr(self, 'registered_miners'):
            self.registered_miners = {}
        
        # 检查是否已注册
        if self.miner_address in self.registered_miners:
            return {
                "success": False,
                "error": "already_registered",
                "message": "该地址已注册为矿工"
            }
        
        # 注册信息
        miner_profile = {
            "minerId": miner_id,
            "address": self.miner_address,
            "gpuType": gpuType,
            "gpuCount": gpuCount,
            "pricePerHour": pricePerHour,
            "description": description,
            "sectors": sectors or ["GPU"],
            "status": "online",
            "registeredAt": int(time.time() * 1000),
            "behaviorScore": 100,
            "totalTasks": 0,
            "completedTasks": 0,
            "totalEarnings": 0.0,
            "acceptanceRate": 100.0,
            "reputationLevel": "bronze",
        }
        
        self.registered_miners[self.miner_address] = miner_profile
        
        return {
            "success": True,
            "minerId": miner_id,
            "address": self.miner_address,
            "profile": miner_profile,
            "message": "注册成功！您现在是算力提供者了"
        }
    
    def _miner_update_profile(
        self,
        gpuType: str = None,
        gpuCount: int = None,
        pricePerHour: float = None,
        description: str = None,
        status: str = None,
        **kwargs
    ) -> Dict:
        """更新矿工资料"""
        if not self.miner_address:
            return {
                "success": False,
                "error": "wallet_not_connected",
                "message": "请先连接钱包"
            }
        
        if not hasattr(self, 'registered_miners'):
            self.registered_miners = {}
        
        if self.miner_address not in self.registered_miners:
            return {
                "success": False,
                "error": "not_registered",
                "message": "您尚未注册为矿工"
            }
        
        profile = self.registered_miners[self.miner_address]
        
        # 更新字段
        if gpuType is not None:
            profile["gpuType"] = gpuType
        if gpuCount is not None:
            profile["gpuCount"] = gpuCount
        if pricePerHour is not None:
            profile["pricePerHour"] = pricePerHour
        if description is not None:
            profile["description"] = description
        if status is not None and status in ["online", "offline", "busy"]:
            profile["status"] = status
        
        profile["updatedAt"] = int(time.time() * 1000)
        
        return {
            "success": True,
            "profile": profile,
            "message": "资料更新成功"
        }
    
    # ============== 区块链查询方==============
    
    def _blockchain_get_height(self, **kwargs) -> Dict:
        """获取当前区块高度
        
        D-18 fix: 使用 get_chain_height() 而非 total_blocks_mined
        """
        height = 0
        if self.consensus_engine:
            height = self.consensus_engine.get_chain_height() if hasattr(self.consensus_engine, 'get_chain_height') else 0
        return {
            "height": height,
            "timestamp": int(time.time() * 1000)
        }
    
    def _blockchain_get_block(self, height: int = None, hash: str = None, **kwargs) -> Dict:
        """获取区块详情 - 从真实区块链读取
        
        D-09 fix: 当区块不在内存缓存中时从数据库查询，而非使用数组下标。
        """
        if height is None and hash is None:
            return {"error": "需要提供 height 或 hash"}
        
        block = None
        
        # 从共识引擎获取真实区块数据
        if self.consensus_engine:
            # 优先从内存缓存中按 height/hash 搜索
            if hasattr(self.consensus_engine, 'chain'):
                chain = self.consensus_engine.chain
                if height is not None:
                    # D-09 fix: 按 height 属性匹配，不用数组下标
                    for b in chain:
                        if hasattr(b, 'height') and b.height == height:
                            block = b
                            break
                elif hash:
                    for b in chain:
                        if hasattr(b, 'hash') and b.hash == hash:
                            block = b
                            break
            
            # D-09 fix: 缓存中找不到时从数据库查询
            if block is None and hasattr(self.consensus_engine, '_db_conn'):
                try:
                    import json as _json
                    if height is not None:
                        row = self.consensus_engine._db_conn.execute(
                            "SELECT block_data FROM blocks WHERE height = ? AND sector = ?",
                            (height, self.consensus_engine.sector)
                        ).fetchone()
                    elif hash:
                        row = self.consensus_engine._db_conn.execute(
                            "SELECT block_data FROM blocks WHERE hash = ? AND sector = ?",
                            (hash, self.consensus_engine.sector)
                        ).fetchone()
                    else:
                        row = None
                    
                    if row:
                        block_dict = _json.loads(row['block_data'])
                        # 返回字典格式的区块数据
                        result = {
                            "height": block_dict.get('height', 0),
                            "hash": block_dict.get('hash', '0x0'),
                            "prevHash": block_dict.get('prev_hash', '0x0'),
                            "timestamp": int(block_dict.get('timestamp', time.time()) * 1000),
                            "miner": block_dict.get('miner_id', self.miner_address or 'MAIN_UNKNOWN'),
                            "txCount": len(block_dict.get('transactions', [])),
                            "size": 1024,
                            "difficulty": block_dict.get('difficulty', 4),
                            "nonce": block_dict.get('nonce', 0),
                            "consensusType": block_dict.get('consensus_type', 'POW'),
                            "reward": block_dict.get('block_reward', 2.5)
                        }
                        # S-Box 数据
                        sbox_hex = block_dict.get('sbox_hex', '')
                        if sbox_hex:
                            result["sbox"] = {
                                "score": round(block_dict.get('sbox_score', 0), 4),
                                "nonlinearity": block_dict.get('sbox_nonlinearity', 0),
                                "diffUniformity": block_dict.get('sbox_diff_uniformity', 0),
                                "avalanche": round(block_dict.get('sbox_avalanche', 0), 4),
                                "selectedSector": block_dict.get('sbox_selected_sector', ''),
                                "allSectors": block_dict.get('sbox_all_sectors', []),
                                "scoreThreshold": round(block_dict.get('sbox_score_threshold', 0), 4),
                            }
                        return result
                except Exception:
                    pass
            
            if block:
                ct = getattr(block, 'consensus_type', None)
                consensus_str = ct.value if hasattr(ct, 'value') else str(ct) if ct else 'POW'
                result = {
                    "height": getattr(block, 'height', 0),
                    "hash": getattr(block, 'hash', '0x0'),
                    "prevHash": getattr(block, 'prev_hash', '0x0'),
                    "timestamp": int(getattr(block, 'timestamp', time.time()) * 1000),
                    "miner": getattr(block, 'miner_id', self.miner_address or 'MAIN_UNKNOWN'),
                    "txCount": len(getattr(block, 'transactions', [])),
                    "size": block.get_size() if hasattr(block, 'get_size') else 1024,
                    "difficulty": getattr(block, 'difficulty', 4),
                    "nonce": getattr(block, 'nonce', 0),
                    "consensusType": consensus_str,
                    "reward": getattr(block, 'block_reward', 2.5)
                }
                # S-Box PoUW 数据
                sbox_hex = getattr(block, 'sbox_hex', '')
                if sbox_hex:
                    result["sbox"] = {
                        "score": round(getattr(block, 'sbox_score', 0), 4),
                        "nonlinearity": getattr(block, 'sbox_nonlinearity', 0),
                        "diffUniformity": getattr(block, 'sbox_diff_uniformity', 0),
                        "avalanche": round(getattr(block, 'sbox_avalanche', 0), 4),
                        "selectedSector": getattr(block, 'sbox_selected_sector', ''),
                        "allSectors": getattr(block, 'sbox_all_sectors', []),
                        "scoreThreshold": round(getattr(block, 'sbox_score_threshold', 0), 4),
                    }
                return result
        
        # 区块不存在
        return {"error": f"区块不存在 height={height}, hash={hash}"}
    
    def _blockchain_get_latest_blocks(self, limit: int = 10, **kwargs) -> Dict:
        """获取最近区块列表
        
        D-12 fix: 从真实区块数据读取，而非伪造哈希/时间戳。
        """
        # Security: cap pagination limit to prevent DoS
        limit = min(limit, 100)
        height = 0
        if self.consensus_engine:
            height = self.consensus_engine.get_chain_height() if hasattr(self.consensus_engine, 'get_chain_height') else 0
        
        blocks = []
        
        # D-12 fix: 从内存缓存读取真实区块
        if self.consensus_engine and hasattr(self.consensus_engine, 'chain'):
            chain = self.consensus_engine.chain
            # 从缓存末尾向前取
            for b in reversed(chain[-limit:]):
                blocks.append({
                    "height": getattr(b, 'height', 0),
                    "hash": getattr(b, 'hash', '0x0'),
                    "timestamp": int(getattr(b, 'timestamp', time.time()) * 1000),
                    "txCount": len(getattr(b, 'transactions', [])),
                    "miner": getattr(b, 'miner_id', self.miner_address or "MAIN_UNKNOWN"),
                })
                if len(blocks) >= limit:
                    break
        
        # 如果缓存不够，从数据库补充
        if len(blocks) < limit and self.consensus_engine and hasattr(self.consensus_engine, '_db_conn'):
            try:
                import json as _json
                already_heights = {b['height'] for b in blocks}
                needed = limit - len(blocks)
                min_cached_height = min((b['height'] for b in blocks), default=height + 1)
                
                rows = self.consensus_engine._db_conn.execute(
                    "SELECT block_data FROM blocks WHERE sector = ? AND height < ? ORDER BY height DESC LIMIT ?",
                    (self.consensus_engine.sector, min_cached_height, needed)
                ).fetchall()
                
                for row in rows:
                    bd = _json.loads(row['block_data'])
                    if bd.get('height') not in already_heights:
                        blocks.append({
                            "height": bd.get('height', 0),
                            "hash": bd.get('hash', '0x0'),
                            "timestamp": int(bd.get('timestamp', time.time()) * 1000),
                            "txCount": len(bd.get('transactions', [])),
                            "miner": bd.get('miner_id', self.miner_address or "MAIN_UNKNOWN"),
                        })
            except Exception:
                pass
        
        return {
            "blocks": blocks,
            "total": height
        }
    
    # ============== 订单查询方法 ==============
    
    def _order_get_list(self, status: str = None, limit: int = 20, offset: int = 0, **kwargs) -> Dict:
        """获取订单列表（从 compute_market 数据库读取真实数据）"""
        limit = min(max(1, limit), 100)  # Cap pagination
        offset = max(0, offset)
        import sqlite3
        import os
        import json
        
        orders = []
        target_address = self.miner_address
        
        # compute_market_v3 数据库读取订
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        market_db_path = os.path.join(base_dir, "data", "compute_market_v3.db")
        
        if os.path.exists(market_db_path):
            try:
                conn = sqlite3.connect(market_db_path)
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                
                # 构建查询
                if target_address:
                    # 只获取当前用户的订单
                    query = """
                        SELECT order_id, order_data, buyer_address, sector, status, created_at
                        FROM orders
                        WHERE buyer_address = ?
                        ORDER BY created_at DESC
                        LIMIT ? OFFSET ?
                    """
                    rows = cursor.execute(query, (target_address, limit, offset)).fetchall()
                else:
                    # 获取所有订
                    query = """
                        SELECT order_id, order_data, buyer_address, sector, status, created_at
                        FROM orders
                        ORDER BY created_at DESC
                        LIMIT ? OFFSET ?
                    """
                    rows = cursor.execute(query, (limit, offset)).fetchall()
                
                for row in rows:
                    try:
                        order_data = json.loads(row["order_data"])
                        orders.append({
                            "id": order_data.get("order_id", row["order_id"]),
                            "type": "buy",
                            "status": order_data.get("status", row["status"]),
                            "gpuType": order_data.get("sector", row["sector"]),
                            "amount": order_data.get("gpu_count", 1),
                            "pricePerHour": order_data.get("max_price", 0),
                            "totalPrice": order_data.get("total_budget", 0),
                            "duration": order_data.get("duration_hours", 1),
                            "createdAt": int(order_data.get("created_at", row["created_at"]) * 1000),
                            "completedAt": int(order_data.get("finished_at", 0) * 1000) if order_data.get("finished_at") else None,
                            "buyer": order_data.get("buyer_address", row["buyer_address"]),
                            "seller": ",".join(order_data.get("assigned_miners", [])) if order_data.get("assigned_miners") else "",
                        })
                    except (json.JSONDecodeError, KeyError):
                        continue
                
                # 获取总数
                if target_address:
                    count_row = cursor.execute("SELECT COUNT(*) as cnt FROM orders WHERE buyer_address = ?", (target_address,)).fetchone()
                else:
                    count_row = cursor.execute("SELECT COUNT(*) as cnt FROM orders").fetchone()
                total = count_row["cnt"] if count_row else len(orders)
                
                conn.close()
            except Exception as e:
                print(f"获取订单列表失败: {e}")
        
        # 按状态筛
        if status and status != "all":
            orders = [o for o in orders if o["status"] == status]
        
        return {
            "orders": orders,
            "total": len(orders),
            "limit": limit,
            "offset": offset
        }
    
    def _order_get_detail(self, orderId: str, **kwargs) -> Dict:
        """获取订单详情 - compute_market_v3.db 读取"""
        if not orderId:
            return {"error": "订单ID不能为空"}
        
        try:
            import sqlite3
            db_path = os.path.join(os.path.dirname(__file__), "..", "data", "compute_market_v3.db")
            if os.path.exists(db_path):
                conn = sqlite3.connect(db_path)
                cursor = conn.cursor()
                cursor.execute("SELECT order_data, status, created_at FROM orders WHERE order_id = ?", (orderId,))
                row = cursor.fetchone()
                conn.close()
                
                if row:
                    order_data = json.loads(row[0]) if row[0] else {}
                    return {
                        "id": orderId,
                        "type": "buy",
                        "status": row[1] or order_data.get("status", "pending"),
                        "gpuType": order_data.get("gpu_type", "RTX4090"),
                        "amount": order_data.get("gpu_count", 1),
                        "pricePerHour": order_data.get("max_price", 1.0),
                        "totalPrice": order_data.get("total_budget", 0),
                        "duration": order_data.get("duration_hours", 1),
                        "createdAt": int(row[2]) if row[2] else int(time.time() * 1000),
                        "completedAt": order_data.get("completed_at"),
                        "buyer": order_data.get("buyer_address", self.miner_address),
                        "seller": order_data.get("seller_address"),
                        "sector": order_data.get("sector", "GPU_COIN"),
                        "assignedMiners": order_data.get("assigned_miners", []),
                        "txHash": f"0x{hashlib.sha256(orderId.encode()).hexdigest()[:64]}",
                    }
            
            return {"error": f"订单 {orderId} 不存在"}
        except Exception as e:
            return self._rpc_internal_error("order_getDetail", e, {
                "id": orderId,
            })
    
    # ============== 质押方法 ==============
    
    def _staking_get_records(self, address: str = None, **kwargs) -> Dict:
        """获取质押记录"""
        addr = address or self.miner_address
        records = []
        
        # 从内存中的 _stakes 读取记录
        if hasattr(self, '_stakes') and self._stakes:
            for stake_id, s in self._stakes.items():
                if s.get("address") == addr:
                    records.append({
                        "id": stake_id,
                        "amount": s.get("amount", 0),
                        "sector": s.get("sector", "MAIN"),
                        "startTime": s.get("startTime", 0),
                        "endTime": s.get("endTime", 0),
                        "status": s.get("status", "active"),
                        "rewards": s.get("rewards", 0),
                        "createdAt": s.get("startTime", int(time.time() * 1000)),
                        "taskId": s.get("taskId"),
                        "rating": s.get("rating"),
                    })
        
        # 同时从 staking_manager 读取（如果有）
        if hasattr(self, 'staking_manager') and self.staking_manager:
            try:
                raw_records = self.staking_manager.get_stakes(addr)
                for r in raw_records:
                    records.append({
                        "id": r.get("stake_id", ""),
                        "amount": r.get("amount", 0),
                        "sector": r.get("sector", "MAIN"),
                        "startTime": r.get("start_time", 0),
                        "endTime": r.get("end_time", 0),
                        "status": r.get("status", "active"),
                        "rewards": r.get("rewards", 0),
                        "createdAt": r.get("created_at", r.get("start_time", int(time.time() * 1000))),
                        "taskId": r.get("task_id"),
                        "rating": r.get("rating"),
                    })
            except Exception:
                pass
        
        return {
            "records": records,
            "totalStaked": sum(r["amount"] for r in records),
            "totalRewards": sum(r.get("rewards", 0) for r in records),
        }
    
    def _staking_stake(self, amount: float, sector: str = "MAIN", duration: int = 30, **kwargs) -> Dict:
        """质押代币"""
        if not self.miner_address:
            return {"success": False, "error": "请先连接钱包"}
        
        if amount <= 0:
            return {"success": False, "error": "质押金额必须大于0"}
        
        # 检查余额是否足够
        try:
            balance_info = self._account_get_balance(address=self.miner_address, sector=sector)
            available = balance_info.get("balance", 0)
            if available < amount:
                return {"success": False, "error": f"余额不足: 可用 {available}, 需要 {amount}"}
        except Exception:
            pass
        
        # 记录质押
        stake_id = f"stake_{uuid.uuid4().hex[:12]}"
        if not hasattr(self, '_stakes'):
            self._stakes = {}
        
        self._stakes[stake_id] = {
            "stakeId": stake_id,
            "address": self.miner_address,
            "amount": amount,
            "sector": sector,
            "duration": duration,
            "startTime": int(time.time() * 1000),
            "endTime": int(time.time() * 1000) + duration * 86400000,
            "status": "active"
        }
        
        return {
            "success": True,
            "stakeId": stake_id,
            "amount": amount,
            "sector": sector,
            "duration": duration,
            "startTime": int(time.time() * 1000),
            "endTime": int(time.time() * 1000) + duration * 86400000,
            "estimatedApy": 12.5,
            "message": f"成功质押 {amount} {sector}"
        }
    
    def _staking_unstake(self, stakeId: str, **kwargs) -> Dict:
        """解除质押"""
        if not self.miner_address:
            return {"success": False, "error": "请先连接钱包"}
        
        if not stakeId:
            return {"success": False, "error": "质押ID不能为空"}
        
        # 查找质押记录
        if not hasattr(self, '_stakes'):
            self._stakes = {}
        
        stake = self._stakes.get(stakeId)
        if not stake:
            return {"success": False, "error": f"未找到质押记录: {stakeId}"}
        
        if stake.get("status") != "active":
            return {"success": False, "error": "该质押已解除"}
        
        # 标记为已解除
        stake["status"] = "unstaked"
        unstaked_amount = stake["amount"]
        
        return {
            "success": True,
            "stakeId": stakeId,
            "unstakedAmount": unstaked_amount,
            "rewards": 0,
            "message": f"解除质押成功，{unstaked_amount} 代币已返还"
        }
    
    # ============== 挖矿方法 ==============
    
    def _mining_get_status(self, **kwargs) -> Dict:
        """获取挖矿状"""
        is_mining = False
        hash_rate = 0
        blocks_mined = 0
        total_rewards = 0.0
        target_address = kwargs.get('address') or self.miner_address or ""
        
        if self.consensus_engine:
            is_mining = self.consensus_engine.is_mining if hasattr(self.consensus_engine, 'is_mining') else False
            blocks_mined = self.consensus_engine.total_blocks_mined if hasattr(self.consensus_engine, 'total_blocks_mined') else 0
            # 估算算力
            if is_mining and blocks_mined > 0:
                uptime = time.time() - (self.consensus_engine.mining_start_time if hasattr(self.consensus_engine, 'mining_start_time') else time.time())
                if uptime > 0:
                    hash_rate = (blocks_mined * 1000000) / uptime  # 估算 H/s
        
        # 计算总奖励（板块币）
        sector_rewards = {}
        if self.sector_ledger and target_address:
            try:
                all_balances = self.sector_ledger.get_all_balances(target_address)
                for coin_type, balance in all_balances.items():
                    sector_rewards[coin_type.sector] = balance.balance
                    total_rewards += balance.balance
            except Exception:
                pass

        miner_identity_set = {target_address, f"miner_{self.node_id}", self.node_id}
        accepted_orders = []
        running_programs = []

        for oid, order in self.market_orders.items():
            if order.get("acceptedBy") in miner_identity_set:
                accepted_orders.append({
                    "orderId": oid,
                    "status": order.get("status", "accepted"),
                    "gpuType": order.get("gpuType", "GPU"),
                    "gpuCount": order.get("gpuCount", 1),
                    "durationHours": order.get("durationHours", 1),
                    "program": order.get("program", ""),
                    "taskId": order.get("taskId", ""),
                })

        for tid, task in self.tasks.items():
            if task.get("minerId") in miner_identity_set and task.get("status") in ("assigned", "running", "completed"):
                running_programs.append({
                    "taskId": tid,
                    "orderId": task.get("orderId", ""),
                    "status": task.get("status", "assigned"),
                    "progress": task.get("progress", 0),
                    "program": task.get("program") or task.get("description", ""),
                    "runtime": task.get("runningTime", "0:00:00"),
                })
        
        return {
            "isMining": is_mining,
            "minerAddress": target_address,
            "hashRate": round(hash_rate, 2),
            "blocksMined": blocks_mined,
            "totalRewards": round(total_rewards, 4),
            "sectorRewards": sector_rewards,  # 各板块币奖励明细
            "sector": self.sector if hasattr(self, 'sector') else "GPU",
            "difficulty": self.consensus_engine.difficulty if self.consensus_engine and hasattr(self.consensus_engine, 'difficulty') else 4,
            "miningMode": self.mining_mode,
            "acceptingTasks": self.accepting_tasks,
            "gpuName": getattr(self, 'detected_gpu_name', 'CPU'),
            "p2pEnabled": self._p2p_data_server is not None,
            "p2pPort": self._p2p_data_server.actual_port if self._p2p_data_server else 0,
            "acceptedOrders": accepted_orders,
            "runningPrograms": running_programs,
            "demoMainBalance": getattr(self, "_demo_main_balances", {}).get(target_address, 0.0),
        }
    
    def _mining_set_mode(self, mode: str = 'mine_only', **kwargs) -> Dict:
        """设置挖矿模式
        
        Args:
            mode: mine_only(只挖, task_only(只接, mine_and_task(挖矿+接单)
        """
        valid_modes = ('mine_only', 'task_only', 'mine_and_task')
        if mode not in valid_modes:
            return {
                "success": False,
                "message": f"无效模式，可 {', '.join(valid_modes)}"
            }
        
        old_mode = self.mining_mode
        self.mining_mode = mode
        
        # 根据模式控制挖矿和接单状
        if mode == 'mine_only':
            self.accepting_tasks = False
            # 如正在挖矿则保持，否则不
        elif mode == 'task_only':
            self.accepting_tasks = True
            # 停止挖矿（如正在运行）
            if self.consensus_engine and hasattr(self.consensus_engine, 'is_mining') and self.consensus_engine.is_mining:
                self.consensus_engine.stop()
        elif mode == 'mine_and_task':
            self.accepting_tasks = True
            # 挖矿状态不
        
        # 注册/注销算力市场
        if self.accepting_tasks and self.compute_scheduler:
            try:
                self.compute_scheduler.register_provider(
                    miner_id=self.miner_address or 'local',
                    sector=getattr(self, 'sector', 'CPU'),
                    gpu_name=getattr(self, 'detected_gpu_name', 'CPU'),
                )
            except Exception:
                pass
        
        mode_names = {
            'mine_only': '只挖矿',
            'task_only': '只接任务',
            'mine_and_task': '挖矿 + 接单'
        }
        
        return {
            "success": True,
            "mode": mode,
            "modeName": mode_names[mode],
            "acceptingTasks": self.accepting_tasks,
            "message": f"已切换到 {mode_names[mode]} 模式"
        }
    
    def _get_scoring_engine(self):
        """获取或初始化评分引擎"""
        if not self._scoring_engine:
            try:
                from core.pouw_scoring import POUWScoringEngine
                self._scoring_engine = POUWScoringEngine()
            except Exception:
                return None
        return self._scoring_engine
    
    def _mining_get_score(self, miner_id: str = None, **kwargs) -> Dict:
        """获取矿工评分详情"""
        mid = miner_id or self.miner_address or 'local'
        
        engine = self._get_scoring_engine()
        if not engine:
            # 无评分引擎时返回默认数据
            return {
                "minerId": mid,
                "objectiveScore": 0.50,
                "feedbackScore": 0.50,
                "priorityScore": 0.50,
                "grade": "B",
                "metrics": {
                    "avgLatencyMs": 0,
                    "completionRate": 0,
                    "uptimeRate": 0,
                    "totalTasks": 0,
                    "blocksMined": 0,
                },
                "feedback": {
                    "rating": 0,
                    "count": 0,
                    "totalTips": 0,
                },
                "weights": {
                    "objectiveWeight": 0.7,
                    "feedbackWeight": 0.3,
                }
            }
        
        try:
            breakdown = engine.get_score_breakdown(mid)
            priority = breakdown['priority_score']
            
            # 将分数映射到等级
            if priority >= 0.9:
                grade = 'S'
            elif priority >= 0.8:
                grade = 'A'
            elif priority >= 0.6:
                grade = 'B'
            elif priority >= 0.4:
                grade = 'C'
            else:
                grade = 'D'
            
            return {
                "minerId": mid,
                "objectiveScore": round(breakdown['objective_score'], 4),
                "feedbackScore": round(breakdown['feedback_score'], 4),
                "priorityScore": round(priority, 4),
                "grade": grade,
                "metrics": {
                    "avgLatencyMs": round(breakdown.get('metrics', {}).get('avg_latency_ms', 0), 1),
                    "completionRate": round(breakdown.get('metrics', {}).get('completion_rate', 0), 4),
                    "uptimeRate": round(breakdown.get('metrics', {}).get('uptime_rate', 0), 4),
                    "totalTasks": breakdown.get('metrics', {}).get('total_tasks', 0),
                    "blocksMined": self.consensus_engine.total_blocks_mined if self.consensus_engine and hasattr(self.consensus_engine, 'total_blocks_mined') else 0,
                },
                "feedback": {
                    "rating": round(breakdown.get('feedback', {}).get('rating', 0), 2),
                    "count": breakdown.get('feedback', {}).get('count', 0),
                    "totalTips": round(engine.feedback.get_miner_total_tips(mid), 4),
                },
                "weights": {
                    "objectiveWeight": breakdown.get('parameters', {}).get('alpha', 0.7),
                    "feedbackWeight": breakdown.get('parameters', {}).get('beta', 0.3),
                }
            }
        except Exception as e:
            return {
                "minerId": mid,
                "objectiveScore": 0.50,
                "feedbackScore": 0.50,
                "priorityScore": 0.50,
                "grade": "B",
                "metrics": {},
                "feedback": {},
                "weights": {},
                "error": "internal_error"
            }
    
    def _mining_start(self, address: str = None, mode: str = None,
                     p2pIp: str = "", p2pPort: int = 0, **kwargs) -> Dict:
        """开始挖矿（自动检测显卡分配板块）"""
        # 如果传入mode，先设置模式
        if mode:
            self._mining_set_mode(mode)
        
        miner_addr = address or self.miner_address
        
        if not miner_addr:
            return {
                "success": False,
                "message": "请先创建或导入钱包"
            }
        
        if not self.consensus_engine:
            return {
                "success": False,
                "message": "共识引擎未初始化"
            }
        
        try:
            # 设置矿工地址
            self.miner_address = miner_addr
            
            # 自动检测显卡并分配板块
            detected_sector = "CPU"  # 默认板块
            gpu_name = "通用GPU"
            try:
                from core.device_detector import auto_assign_sector, get_device_detector
                detector = get_device_detector()
                device_profile = detector.detect_all()
                
                if device_profile.gpu_list:
                    # 取最强的 GPU
                    best_gpu = device_profile.gpu_list[0]
                    gpu_name = best_gpu.name
                    detected_sector = auto_assign_sector()
                    print(f"[POUW] 检测到 GPU: {gpu_name} 分配{detected_sector} 板块")
                else:
                    gpu_name = "CPU"
            except Exception as e:
                print(f"[POUW] 显卡检测失败，使用默认板块: {e}")
                gpu_name = "CPU"
            
            # 设置当前板块和GPU名称
            self.sector = detected_sector
            self.detected_gpu_name = gpu_name
            
            # 定义挖矿回调 - 挖矿奖励是板块币，不是主币！
            def on_block_mined(block):
                # 创建 Coinbase UTXO（可转账的资金来源）
                # 矿工收入 = 区块奖励 + 手续费 - 财库税
                total_income = block.block_reward + getattr(block, 'total_fees', 0)
                treasury_rate = self.consensus_engine.reward_calculator.treasury_rate
                treasury_amount = total_income * treasury_rate
                miner_income = total_income - treasury_amount
                if hasattr(self, 'utxo_store') and self.utxo_store:
                    try:
                        self.utxo_store.create_coinbase_utxo(
                            miner_address=miner_addr,
                            amount=miner_income,
                            sector=self.sector,
                            block_height=block.height,
                            block_hash=block.hash
                        )
                        # 财库份额
                        if treasury_amount > 0:
                            self.utxo_store.create_coinbase_utxo(
                                miner_address='MAIN_TREASURY',
                                amount=treasury_amount,
                                sector=self.sector,
                                block_height=block.height,
                                block_hash=block.hash
                            )
                    except Exception as e:
                        import logging
                        logging.getLogger('rpc').error(f"Coinbase UTXO 创建失败 (block #{block.height}): {e}")
                # 同步写入 sector_ledger（兑换系统 lock/burn 所需）
                if hasattr(self, 'sector_ledger') and self.sector_ledger and self.sector != 'MAIN':
                    try:
                        self.sector_ledger.mint_block_reward(
                            sector=self.sector,
                            miner_address=miner_addr,
                            block_height=block.height
                        )
                    except Exception as e:
                        import logging
                        logging.getLogger('rpc').debug(f"Sector ledger 同步失败: {e}")
            
            # 启动挖矿
            self.consensus_engine.start_mining(miner_addr, on_block=on_block_mined)
            
            mode_names = {
                'mine_only': '只挖',
                'task_only': '只接',
                'mine_and_task': '挖矿 + 接单'
            }
            
            result = {
                "success": True,
                "minerAddress": miner_addr,
                "sector": detected_sector,
                "gpuName": gpu_name,
                "miningMode": self.mining_mode,
                "acceptingTasks": self.accepting_tasks,
                "message": f"已启[{mode_names.get(self.mining_mode, '挖矿')}] 模式，板 {detected_sector}"
            }
            
            # 如果模式包含接单，自动启动 P2P 数据服务器
            if self.accepting_tasks:
                try:
                    p2p_result = self._auto_start_p2p_server(p2pIp, int(p2pPort) if p2pPort else 0)
                    result["p2pEnabled"] = p2p_result.get("success", False)
                    result["p2pPort"] = p2p_result.get("port", 0)
                    result["p2pPublicKey"] = p2p_result.get("publicKey", "")
                    if p2p_result.get("success"):
                        result["message"] += f"，P2P 直连已就绪 (:{p2p_result['port']})"
                except Exception as pe:
                    result["p2pEnabled"] = False
                    print(f"[POUW] P2P 自动启动跳过: {pe}")
            
            return result
        except Exception as e:
            return {
                "success": False,
                "error": "internal_error",
                "message": "启动挖矿失败"
            }
    
    def _auto_start_p2p_server(self, public_ip: str = "", port: int = 0) -> Dict:
        """接单模式自动启动 P2P 数据服务器并注册端点"""
        # 如果已在运行，直接返回
        if self._p2p_data_server is not None:
            actual_port = self._p2p_data_server.actual_port
            pubkey = self._p2p_data_server.public_key
            # 如果传入了新的公网 IP，更新注册
            if public_ip:
                tm = self._get_ticket_manager()
                miner_id = f"miner_{self.node_id}"
                tm.register_miner_direct(miner_id, public_ip, actual_port, pubkey)
            return {
                "success": True,
                "port": actual_port,
                "publicKey": pubkey.hex(),
            }
        
        from .p2p_data_tunnel import P2PDataServer
        self._p2p_data_server = P2PDataServer(
            host="0.0.0.0", port=port, data_dir="data/p2p_recv",
        )
        
        # 连接回调
        self._p2p_data_server.on_transfer_complete = self._on_p2p_transfer_complete
        
        self._p2p_data_server.start()
        
        actual_port = self._p2p_data_server.actual_port
        pubkey = self._p2p_data_server.public_key
        
        # 注册端点（如果矿工提供了公网 IP）
        miner_id = f"miner_{self.node_id}"
        tm = self._get_ticket_manager()
        register_ip = public_ip or "127.0.0.1"
        tm.register_miner_direct(miner_id, register_ip, actual_port, pubkey)
        
        return {
            "success": True,
            "port": actual_port,
            "publicKey": pubkey.hex(),
        }
    
    def _mining_stop(self, **kwargs) -> Dict:
        """停止挖矿"""
        try:
            # 停止共识挖矿线程（若存在）
            if self.consensus_engine and hasattr(self.consensus_engine, 'stop'):
                self.consensus_engine.stop()
                if hasattr(self.consensus_engine, 'is_mining'):
                    self.consensus_engine.is_mining = False

            # 停止接单状态，避免前端仍显示“接单中”
            self.accepting_tasks = False
            self.mining_mode = 'mine_only'

            # 停止 P2P 数据服务（若已启动）
            if self._p2p_data_server is not None:
                try:
                    self._p2p_data_server.stop()
                except Exception:
                    pass
                self._p2p_data_server = None

            return {
                "success": True,
                "message": "挖矿已停止",
                "acceptingTasks": False,
                "miningMode": self.mining_mode,
            }
        except Exception as e:
            return {
                "success": False,
                "error": "internal_error",
                "message": "停止挖矿失败"
            }
    
    def _mining_get_rewards(self, period: str = "all", **kwargs) -> Dict:
        """获取挖矿奖励统计"""
        rewards = []
        total_amount = 0.0
        
        if self.sector_ledger and self.miner_address:
            try:
                with self.sector_ledger._conn() as conn:
                    rows = conn.execute("""
                        SELECT coin_type, amount, timestamp, block_height, tx_type
                        FROM transfers
                        WHERE to_address = ? AND tx_type = 'mint'
                        ORDER BY timestamp DESC
                        LIMIT 100
                    """, (self.miner_address,)).fetchall()
                    
                    for row in rows:
                        rewards.append({
                            "coin": row["coin_type"],
                            "amount": row["amount"],
                            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.localtime(row["timestamp"])),
                            "blockHeight": row["block_height"],
                        })
                        total_amount += row["amount"]
            except Exception as e:
                print(f"获取挖矿奖励失败: {e}")
        
        return {
            "rewards": rewards,
            "totalAmount": round(total_amount, 4),
            "count": len(rewards),
            "minerAddress": self.miner_address or "",
        }
    
    # ============== 板块币兑换方==============
    
    def _sector_get_exchange_rates(self, **kwargs) -> Dict:
        """获取板块币兑换比率"""
        try:
            from core.dual_witness_exchange import get_exchange_service
            exchange = get_exchange_service()
            
            rates = {}
            try:
                from core.sector_coin import get_sector_registry
                sectors = get_sector_registry().get_active_sectors()
            except Exception:
                sectors = ["H100", "RTX4090", "RTX3080", "CPU", "GENERAL"]
            for sector in sectors:
                rates[sector] = {
                    "rate": exchange.get_exchange_rate(sector),
                    "example": f"10 {sector}_COIN {10 * exchange.get_exchange_rate(sector):.2f} MAIN"
                }
            
            return {
                "success": True,
                "rates": rates,
                "message": "板块币兑换比例（板块→MAIN）"
            }
        except Exception as e:
            return {
                "success": False,
                "rates": {},
                "error": "internal_error"
            }
    
    def _sector_request_exchange(self, sector: str = "", amount: float = 0, **kwargs) -> Dict:
        """请求兑换板块币为MAIN（双见证机制）"""
        if not sector or amount <= 0:
            return {
                "success": False,
                "message": "请提供有效的板块和金额"
            }
        
        if not self.miner_address:
            return {
                "success": False,
                "message": "请先连接钱包"
            }
        
        try:
            from core.dual_witness_exchange import get_exchange_service, ExchangeStatus
            exchange = get_exchange_service()
            
            ok, msg, request = exchange.request_exchange(
                self.miner_address,
                sector,
                amount
            )
            
            if ok and request:
                # 安全加固：移除自动见证模
                # Security: auto-witness ONLY in development mode (MAINCOIN_PRODUCTION != "true")
                # 生产环境中，见证由各板块区块生成时自动触发，不可通过 RPC 模拟
                import os
                is_production = os.environ.get("MAINCOIN_PRODUCTION", "").lower() == "true"
                
                if not is_production:
                    # 开测试模式：自动模拟见证完
                    for ws in request.witness_sectors:
                        exchange.add_witness(
                            request.exchange_id,
                            ws,
                            block_height=self.current_height,
                            block_hash=f"0x{request.exchange_id}",
                            signature=f"dev_auto_witness_{ws}_{self.current_height}"
                        )
                # else: 生产环境不自动见证，需等待真实板块区块见证
                
                # 重新获取请求状
                updated_request = exchange.get_exchange_request(request.exchange_id)
                
                # 如果兑换完成，记录到主区块链上（这样区块浏览器能看到）
                if updated_request and updated_request.status == ExchangeStatus.COMPLETED:
                    exchange_recorded = False
                    try:
                        from core.utxo_store import get_utxo_store
                        utxo_store = get_utxo_store()
                        
                        # 创建链上交易记录
                        tx_result = utxo_store.create_exchange_transaction(
                            address=self.miner_address,
                            from_sector=sector,
                            from_amount=amount,
                            to_sector="MAIN",
                            to_amount=request.target_main_amount,
                            exchange_id=request.exchange_id,
                            witness_sectors=request.witness_sectors,
                            block_height=self.current_height,
                            block_hash=f"0x{request.exchange_id}"
                        )
                        
                        if tx_result.get("success"):
                            print(f"[Exchange] 兑换交易已记录 {tx_result.get('txid')}")
                            exchange_recorded = True
                        else:
                            print(f"[Exchange] UTXO 交易失败: {tx_result.get('error')}")
                    except Exception as e:
                        print(f"[Exchange] 记录链上交易失败: {e}")
                    
                    if not exchange_recorded:
                        return {
                            "success": False,
                            "message": "兑换见证已通过，但 UTXO 记录失败，请重试"
                        }
                    
                    # UTXO 操作成功后，同步销毁 sector_ledger 中的余额
                    try:
                        from core.sector_coin import SectorCoinType
                        coin_type = SectorCoinType.from_sector(sector)
                        exchange.sector_ledger.burn_for_exchange(
                            self.miner_address, coin_type, amount,
                            request.exchange_id
                        )
                    except Exception as e:
                        # sector_ledger 同步失败不影响 UTXO 结果，仅记录
                        print(f"[Exchange] sector_ledger 同步销毁失败 (非致命): {e}")
                
                return {
                    "success": True,
                    "exchangeId": request.exchange_id,
                    "fromSector": sector,
                    "fromAmount": amount,
                    "toAmount": request.target_main_amount,
                    "rate": request.exchange_rate,
                    "status": updated_request.status.value if updated_request else request.status.value,
                    "witnesses": request.witness_sectors,
                    "message": msg
                }
            else:
                return {
                    "success": False,
                    "message": msg
                }
        except Exception as e:
            return {
                "success": False,
                "error": "internal_error",
                "message": "operation_failed"
            }
    
    def _sector_get_exchange_history(self, limit: int = 20, **kwargs) -> Dict:
        """获取兑换历史"""
        if not self.miner_address:
            return {
                "success": True,
                "exchanges": [],
                "total": 0
            }
        
        try:
            from core.dual_witness_exchange import get_exchange_service
            exchange = get_exchange_service()
            
            exchanges = []
            with exchange._conn() as conn:
                rows = conn.execute("""
                    SELECT exchange_id, request_data, status, created_at
                    FROM exchange_requests
                    ORDER BY created_at DESC
                    LIMIT ?
                """, (limit,)).fetchall()
                
                for row in rows:
                    import json
                    data = json.loads(row['request_data'])
                    if data.get('requester_address') == self.miner_address:
                        exchanges.append({
                            "exchangeId": row['exchange_id'],
                            "fromSector": data.get('source_sector'),
                            "fromAmount": data.get('source_amount'),
                            "toAmount": data.get('target_main_amount'),
                            "rate": data.get('exchange_rate'),
                            "status": row['status'],
                            "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", 
                                                        time.localtime(row['created_at'])),
                            "witnesses": data.get('witness_sectors', [])
                        })
            
            return {
                "success": True,
                "exchanges": exchanges,
                "total": len(exchanges)
            }
        except Exception as e:
            return {
                "success": True,
                "exchanges": [],
                "total": 0,
                "error": "internal_error"
            }
    
    def _sector_cancel_exchange(self, exchangeId: str = "", **kwargs) -> Dict:
        """取消兑换请求"""
        if not exchangeId:
            return {
                "success": False,
                "message": "请提供兑换ID"
            }
        
        if not self.miner_address:
            return {
                "success": False,
                "message": "请先连接钱包"
            }
        
        try:
            from core.dual_witness_exchange import get_exchange_service
            exchange = get_exchange_service()
            
            ok, msg = exchange.cancel_exchange(exchangeId, self.miner_address)
            
            return {
                "success": ok,
                "message": msg
            }
        except Exception as e:
            return {
                "success": False,
                "message": "operation_failed"
            }
    
    # ============== 隐私方法 ==============
    
    def _privacy_get_status(self, **kwargs) -> Dict:
        """获取隐私状"""
        sub_addrs = self.sub_addresses.get(self.miner_address or "", [])
        total_usage = sum(s.get("usageCount", 0) for s in sub_addrs)
        main_usage = 0  # 主地址使用次数，从交易记录统计
        
        target_address = kwargs.get('address', self.miner_address or "")
        if hasattr(self, 'transactions') and self.miner_address:
            for tx in self.transactions:
                if tx.get('from') == self.miner_address or tx.get('to') == self.miner_address:
                    main_usage += 1
        
        # 根据地址使用情况评估隐私风险
        if total_usage + main_usage < 10:
            risk_level = "low"
        elif total_usage + main_usage < 50:
            risk_level = "medium"
        else:
            risk_level = "high"
        
        # 计算关联地址
        linked_addresses = len(sub_addrs)
        
        # 隐私建议
        suggestions = []
        recommendations = []
        rec_id = 1
        
        if len(sub_addrs) < 3:
            suggestions.append("建议创建更多子地址分散资金")
            recommendations.append({
                "id": rec_id,
                "type": "info",
                "message": f"当前只有 {len(sub_addrs)} 个子地址，建议创建更多子地址分散交易"
            })
            rec_id += 1
            
        if total_usage > 20:
            suggestions.append("建议轮换常用地址以提高隐私")
            recommendations.append({
                "id": rec_id,
                "type": "warning",
                "message": "子地址使用频率过高，建议轮换地址"
            })
            rec_id += 1
            
        if main_usage > 50:
            recommendations.append({
                "id": rec_id,
                "type": "warning",
                "message": f"主地址使用频率过高 ({main_usage} ，建议使用子地址分散交易"
            })
            rec_id += 1
            
        if risk_level == "high":
            suggestions.append("检测到高频使用地址，建议使用一次性地址")
            recommendations.append({
                "id": rec_id,
                "type": "warning",
                "message": f"检测到 {linked_addresses} 个地址可能被关联，建议使用新子地址"
            })
            rec_id += 1
        else:
            recommendations.append({
                "id": rec_id,
                "type": "success",
                "message": "最近24小时内未检测到异常交易模式"
            })
        
        # 主地址风险评估
        main_risk = "high" if main_usage > 100 else ("medium" if main_usage > 30 else "low")
        
        # 构建主地址信息
        main_address_info = {
            "address": self.miner_address[:8] + "..." + self.miner_address[-4:] if self.miner_address else "未连",
            "usageCount": main_usage,
            "riskLevel": main_risk,
            "linkedAddresses": linked_addresses,
            "lastUsed": int(time.time() * 1000),
        }
        
        # 构建子地址信息
        sub_addresses_info = []
        for sub in sub_addrs:
            addr = sub.get("address", "")
            usage = sub.get("usageCount", 0)
            sub_risk = "high" if usage > 80 else ("medium" if usage > 30 else "low")
            sub_addresses_info.append({
                "address": addr[:8] + "..." + addr[-4:] if len(addr) > 12 else addr,
                "usageCount": usage,
                "riskLevel": sub_risk,
                "lastUsed": sub.get("lastUsed", int(time.time() * 1000)),
            })
        
        return {
            "currentLevel": "pseudonymous",  # 当前支持的隐私级
            "riskLevel": risk_level,
            "addressUsageCount": total_usage,
            "subAddressCount": len(sub_addrs),
            "suggestions": suggestions,
            "roadmapPhase": "Phase 1 - 基础隐私",
            "nextPhaseFeatures": [
                "环签名支",
                "隐地址 (Stealth Address)",
                "机密交易 (Confidential Transactions)",
            ],
            "recommendations": recommendations,
            "mainAddressInfo": main_address_info,
            "subAddresses": sub_addresses_info,
        }
    
    def _privacy_rotate_address(self, **kwargs) -> Dict:
        """轮换地址 - 创建新子地址并标记旧地址"""
        if not self.miner_address:
            raise RPCError(RPCErrorCode.INVALID_PARAMS.value, "未连接钱包")
        
        # 创建新地址
        new_addr = f"SUB_{uuid.uuid4().hex[:16]}"
        sub_addr = {
            "address": new_addr,
            "label": "轮换地址",
            "balance": 0,
            "usageCount": 0,
            "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "isPrimary": True,
        }
        
        # 将旧地址标记为非主要
        if self.miner_address in self.sub_addresses:
            for old in self.sub_addresses[self.miner_address]:
                old["isPrimary"] = False
        else:
            self.sub_addresses[self.miner_address] = []
        
        self.sub_addresses[self.miner_address].append(sub_addr)
        
        return {
            "newAddress": new_addr,
            "status": "success",
            "message": "地址已轮换，建议将资金转移到新地址",
        }
    
    # ============== 节点方法 ==============
    
    def _get_peer_count(self) -> int:
        """动态获取对等节点数"""
        peers = 0
        if hasattr(self, 'p2p_node') and self.p2p_node:
            pc = getattr(self.p2p_node, 'peer_count', 0)
            peers = pc() if callable(pc) else pc
        if hasattr(self, 'p2p_network') and self.p2p_network:
            pc = getattr(self.p2p_network, 'peer_count', 0)
            peers = max(peers, pc() if callable(pc) else pc)
        return peers

    def _node_get_info(self, params: Dict) -> Dict:
        """节点信息"""
        uptime_seconds = time.time() - self._start_time if hasattr(self, '_start_time') else 0
        return {
            "node_id": self.node_id,
            "version": "2.0.0",
            "network": "pouw-mainnet",
            "height": self.current_height,
            "syncing": self.is_syncing,
            "peers": self._get_peer_count(),
            "uptime": round(uptime_seconds),
            "capabilities": ["tx", "block", "witness", "compute"]
        }
    
    def _node_get_peers(self, params: Dict) -> List[Dict]:
        """获取对等节点"""
        peers = []
        if hasattr(self, 'p2p_node') and self.p2p_node:
            try:
                peer_list = getattr(self.p2p_node, 'get_peers', lambda: [])()
                for p in peer_list:
                    if isinstance(p, dict):
                        peers.append(p)
                    else:
                        peers.append({"address": str(p), "connected": True})
            except Exception:
                pass
        return peers
    
    def _node_is_syncing(self, params: Dict) -> bool:
        """是否在同步"""
        return self.is_syncing
    
    # ============== 算力市场方法 ==============
    
    def _compute_submit_order(self, gpu_type: str = None, gpu_count: int = 1,
                               price_per_hour: float = 1.0, duration_hours: int = 1,
                               program: str = "", free_order: bool = False,
                               buyer_address: str = None, **kwargs) -> Dict:
        """提交算力订单"""
        # 默认优先走 ComputeMarketV3，旧 market_orders 逻辑仅做兼容兜底。
        if self.compute_market and hasattr(self.compute_market, "create_order"):
            try:
                from .compute_market_v3 import TaskExecutionMode

                execution_mode_raw = str(
                    kwargs.get("executionMode", kwargs.get("execution_mode", "normal"))
                ).lower()
                if execution_mode_raw == "tee":
                    execution_mode = TaskExecutionMode.TEE
                elif execution_mode_raw == "zk":
                    execution_mode = TaskExecutionMode.ZK
                else:
                    execution_mode = TaskExecutionMode.NORMAL

                sector = kwargs.get("sector") or gpu_type or "RTX3080"
                buyer = buyer_address or kwargs.get('buyer_address') or self.miner_address or f"buyer_{self.node_id}"
                unit_price = 0.0 if (free_order or kwargs.get('free_order', False)) else max(0.0, float(price_per_hour or 0.0))
                gpu_count = max(1, int(gpu_count or 1))
                duration_hours = max(1, int(duration_hours or kwargs.get('duration_hours', 1) or 1))

                task_hash = kwargs.get("task_hash") or kwargs.get("taskHash")
                if not task_hash:
                    task_payload = {
                        "program": program,
                        "gpu_type": gpu_type,
                        "gpu_count": gpu_count,
                        "duration_hours": duration_hours,
                        "price_per_hour": unit_price,
                        "buyer": buyer,
                    }
                    task_hash = hashlib.sha256(
                        json.dumps(task_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
                    ).hexdigest()

                order, msg = self.compute_market.create_order(
                    buyer_address=buyer,
                    sector=str(sector),
                    gpu_count=gpu_count,
                    duration_hours=duration_hours,
                    max_price=unit_price,
                    task_hash=str(task_hash),
                    task_encrypted_blob=str(kwargs.get("taskEncryptedBlob", kwargs.get("task_encrypted_blob", ""))),
                    execution_mode=execution_mode,
                    allow_validation=bool(kwargs.get("allowValidation", kwargs.get("allow_validation", True))),
                    tee_node_id=str(kwargs.get("teeNodeId", kwargs.get("tee_node_id", ""))),
                    tee_attestation=kwargs.get("teeAttestation", kwargs.get("tee_attestation", {})) or {},
                )
                if order:
                    return {
                        "orderId": order.order_id,
                        "status": order.status.value,
                        "buyerAddress": order.buyer_address,
                        "sector": order.sector,
                        "gpuCount": order.gpu_count,
                        "durationHours": order.duration_hours,
                        "maxPrice": order.max_price,
                        "totalPrice": order.total_budget,
                        "executionMode": order.execution_mode.value,
                        "message": msg,
                        "createdAt": int(order.created_at),
                        "path": "compute_market_v3",
                    }
            except Exception as e:
                logger.exception("compute_submitOrder V3 path failed")
                if self._compute_v3_required():
                    return {
                        "status": "failed",
                        "error": "compute_v3_required",
                        "message": f"compute_market_v3_error:{type(e).__name__}",
                        "path": "compute_market_v3",
                    }

        if not hasattr(self, "_demo_main_balances"):
            self._demo_main_balances = {}

        buyer = buyer_address or kwargs.get('buyer_address') or self.miner_address or f"buyer_{self.node_id}"
        unit_price = 0.0 if (free_order or kwargs.get('free_order', False)) else max(0.0, float(price_per_hour or 0.0))
        gpu_count = max(1, int(gpu_count or 1))
        duration_hours = max(1, int(duration_hours or kwargs.get('duration_hours', 1) or 1))
        total_price = round(unit_price * gpu_count * duration_hours, 8)

        if buyer not in self._demo_main_balances:
            self._demo_main_balances[buyer] = 1000.0

        if self._demo_main_balances[buyer] < total_price:
            return {
                "status": "failed",
                "error": "insufficient_balance",
                "message": "下单账户余额不足",
                "buyerAddress": buyer,
                "buyerBalance": self._demo_main_balances[buyer],
                "required": total_price,
            }

        self._demo_main_balances[buyer] -= total_price
        order_id = f"order_{uuid.uuid4().hex[:8]}"
        image = kwargs.get("image") or kwargs.get("docker_image") or ""
        input_data_ref = kwargs.get("inputDataRef") or kwargs.get("input_data_ref") or ""
        input_filename = kwargs.get("inputFilename") or kwargs.get("input_filename") or ""

        if input_data_ref and hasattr(self, "_file_manager") and self._file_manager:
            file_info = self._file_manager.get_file_info(input_data_ref)
            if not file_info:
                return {
                    "status": "failed",
                    "error": "invalid_input_data_ref",
                    "message": f"上传文件引用不存在: {input_data_ref}",
                }

        from .fee_config import ComputeMarketFeeRates as _CMF

        order = {
            "orderId": order_id,
            "minerId": "",
            "minerName": "",
            "gpuType": gpu_type or "RTX3080",
            "gpuCount": gpu_count,
            "pricePerHour": unit_price,
            "durationHours": duration_hours,
            "totalPrice": total_price,
            "buyerDebitTotal": total_price,
            "feeRates": {
                "platform": _CMF.PLATFORM,
                "miner": _CMF.MINER,
                "treasury": _CMF.TREASURY,
            },
            "platformFee": round(total_price * _CMF.PLATFORM, 8),
            "treasuryFee": round(total_price * _CMF.TREASURY, 8),
            "minerPayout": round(total_price * _CMF.MINER, 8),
            "coin": f"{gpu_type or 'RTX3080'}_COIN",
            "available": True,
            "status": "open",
            "buyerAddress": buyer,
            "acceptedBy": "",
            "taskId": "",
            "program": program,
            "image": image,
            "inputDataRef": input_data_ref,
            "inputFilename": input_filename,
            "result": "",
            "createdAt": int(time.time() * 1000),
            "buyerBalanceAfter": round(self._demo_main_balances[buyer], 8),
            "rating": 5.0,
            "completedTasks": 0,
            "slaGuarantee": "99% 可用",
            "uptime": 100.0,
        }
        self.market_orders[order_id] = order
        return {
            "orderId": order_id,
            "status": "submitted",
            "buyerAddress": buyer,
            "buyerBalanceAfter": order["buyerBalanceAfter"],
            "totalPrice": total_price,
            "buyerDebitTotal": total_price,
            "platformFee": order["platformFee"],
            "treasuryFee": order["treasuryFee"],
            "minerPayout": order["minerPayout"],
            "feeRates": order["feeRates"],
            "freeOrder": unit_price == 0.0,
        }
    
    def _compute_get_order(self, order_id: str = None, **kwargs) -> Optional[Dict]:
        """获取订单详情"""
        if not order_id:
            return None

        auth_context = kwargs.get("auth_context", {})

        def _can_view_full(owner_addr: str) -> bool:
            caller = self._get_auth_user(auth_context, self.miner_address or "anonymous")
            is_admin = bool(auth_context.get("is_admin", False))
            return bool(owner_addr) and (caller == owner_addr or is_admin)

        if self.compute_market and hasattr(self.compute_market, "get_order"):
            try:
                order = self.compute_market.get_order(order_id)
                if order:
                    summary = {
                        "orderId": order.order_id,
                        "buyerAddress": order.buyer_address,
                        "gpuType": order.sector,
                        "gpuCount": order.gpu_count,
                        "durationHours": order.duration_hours,
                        "pricePerHour": order.max_price,
                        "totalPrice": order.total_budget,
                        "status": order.status.value,
                        "path": "compute_market_v3",
                    }

                    if _can_view_full(order.buyer_address):
                        data = order.to_dict()
                        data.update(summary)
                        data["permissionScope"] = "owner_or_admin"
                        return data

                    summary["permissionScope"] = "public_redacted"
                    return summary
            except Exception:
                logger.exception("compute_getOrder V3 path failed")

        order = self.market_orders.get(order_id)
        if not order:
            return None

        owner_addr = str(order.get("buyerAddress") or order.get("owner") or "")
        if _can_view_full(owner_addr):
            out = dict(order)
            out["permissionScope"] = "owner_or_admin"
            return out

        return {
            "orderId": order.get("orderId", order_id),
            "status": order.get("status", "unknown"),
            "gpuType": order.get("gpuType", ""),
            "gpuCount": order.get("gpuCount", 0),
            "durationHours": order.get("durationHours", 0),
            "pricePerHour": order.get("pricePerHour", 0),
            "totalPrice": order.get("totalPrice", 0),
            "path": "legacy_market_orders",
            "permissionScope": "public_redacted",
        }
    
    def _compute_get_market(self, gpu_type: str = None, **kwargs) -> Dict:
        """获取市场信息 - 真实数据"""
        if self.compute_market and hasattr(self.compute_market, "get_market_stats"):
            try:
                sector = kwargs.get("sector") or gpu_type
                stats = self.compute_market.get_market_stats(sector=sector)
                return {
                    "orders": [],
                    "total": stats.get("active_orders", 0),
                    "totalCapacity": stats.get("total_gpus", 0),
                    "available": stats.get("available_gpus", 0),
                    "v3": stats,
                    "path": "compute_market_v3",
                }
            except Exception:
                logger.exception("compute_getMarket V3 path failed")

        orders = list(self.market_orders.values())
        
        if gpu_type:
            orders = [o for o in orders if o["gpuType"] == gpu_type]
        
        return {
            "orders": orders,
            "total": len(orders),
            "totalCapacity": sum(o["gpuCount"] for o in orders),
            "available": sum(o["gpuCount"] for o in orders if o["available"]),
        }

    def _compute_get_trap_question(self, miner_id: str = None, **kwargs) -> Dict:
        """获取当前 30 分钟窗口的性能陷阱题。"""
        if self.compute_market and hasattr(self.compute_market, "get_performance_trap"):
            identity = (
                miner_id
                or kwargs.get("miner_address")
                or kwargs.get("miner_id")
                or self.miner_address
                or f"miner_{self.node_id}"
            )
            ok, payload = self.compute_market.get_performance_trap(identity)
            return {
                "status": "success" if ok else "failed",
                "minerId": identity,
                "trap": payload,
                "path": "compute_market_v3",
            }
        return {
            "status": "failed",
            "message": "compute_market_trap_unavailable",
        }

    def _compute_submit_trap_answer(self,
                                    challenge_id: str,
                                    answer_hash: str,
                                    miner_id: str = None,
                                    **kwargs) -> Dict:
        """提交性能陷阱题答案并更新评分。"""
        if not challenge_id:
            return {"status": "failed", "message": "challenge_id_required"}
        if not answer_hash:
            return {"status": "failed", "message": "answer_hash_required"}

        if self.compute_market and hasattr(self.compute_market, "submit_performance_trap"):
            identity = (
                miner_id
                or kwargs.get("miner_address")
                or kwargs.get("miner_id")
                or self.miner_address
                or f"miner_{self.node_id}"
            )
            ok, payload = self.compute_market.submit_performance_trap(
                identity,
                challenge_id,
                answer_hash,
            )
            return {
                "status": "success" if ok else "failed",
                "minerId": identity,
                "result": payload,
                "path": "compute_market_v3",
            }
        return {
            "status": "failed",
            "message": "compute_market_trap_unavailable",
        }
    
    def _compute_accept_order(self, order_id: str, task_id: str = None, **kwargs) -> Dict:
        """接受算力订单"""
        if self.compute_market and hasattr(self.compute_market, "start_execution") and hasattr(self.compute_market, "get_order"):
            try:
                v3_order = self.compute_market.get_order(order_id)
                if v3_order:
                    miner_identity = (
                        kwargs.get("miner_id")
                        or kwargs.get("miner_address")
                        or self.miner_address
                        or f"miner_{self.node_id}"
                    )
                    ok, msg = self.compute_market.start_execution(order_id, miner_identity)
                    return {
                        "status": "success" if ok else "failed",
                        "orderId": order_id,
                        "taskId": task_id or f"task_{order_id}",
                        "acceptedBy": miner_identity,
                        "message": msg,
                        "path": "compute_market_v3",
                    }
            except Exception as e:
                logger.exception("compute_acceptOrder V3 path failed")
                if self._compute_v3_required():
                    return {
                        "status": "failed",
                        "orderId": order_id,
                        "message": f"compute_market_v3_error:{type(e).__name__}",
                        "path": "compute_market_v3",
                    }

        if not order_id or order_id not in self.market_orders:
            raise RPCError(RPCErrorCode.INVALID_PARAMS.value, f"订单不存在 {order_id}")
        
        order = self.market_orders[order_id]
        if not order.get("available", False):
            raise RPCError(RPCErrorCode.INVALID_PARAMS.value, "订单不可用")

        miner_identity = kwargs.get("miner_address") or self.miner_address or f"miner_{self.node_id}"
        
        # 标记订单为不可用
        order["available"] = False
        order["acceptedBy"] = miner_identity
        order["minerId"] = miner_identity
        order["minerName"] = miner_identity
        order["status"] = "accepted"
        order["acceptedAt"] = int(time.time() * 1000)
        
        linked_task_id = task_id or order.get("taskId") or f"task_{order_id}"
        order["taskId"] = linked_task_id

        program_code = order.get("program", "") or "print('Hello from MainCoin compute order')"
        docker_image = order.get("image", "") or "python:3.11-slim"
        input_ref = order.get("inputDataRef", "")
        input_name = order.get("inputFilename", "") or "input.data"
        generated_dockerfile = (
            f"FROM {docker_image}\n"
            "WORKDIR /workspace\n"
            "COPY main.py /workspace/main.py\n"
            "COPY requirements.txt /workspace/requirements.txt\n"
            "RUN pip install --no-cache-dir -r /workspace/requirements.txt\n"
            "CMD [\"python\", \"/workspace/main.py\"]\n"
        )
        generated_requirements = "\n".join([
            "requests>=2.31.0",
            "numpy>=1.26.0",
        ])
        generated_main = "\n".join([
            "import json",
            "import time",
            "",
            "print('MainCoin Docker task started')",
            f"print('task_id={linked_task_id}')",
            f"print('order_id={order_id}')",
            "time.sleep(0.1)",
            "result = {'status': 'ok', 'message': 'docker task executed', 'score': 100}",
            "print('RESULT=' + json.dumps(result, ensure_ascii=False))",
            "",
            "# User provided program snippet",
            program_code,
        ])

        task_files = [
            {"name": "main.py", "type": "file", "content": generated_main},
            {"name": "requirements.txt", "type": "file", "content": generated_requirements},
        ]
        if docker_image:
            task_files.append({"name": "Dockerfile", "type": "file", "content": generated_dockerfile})

        if linked_task_id in self.tasks:
            task = self.tasks[linked_task_id]
            task["status"] = "running"
            task["minerId"] = miner_identity
            task["orderId"] = order_id
            task["program"] = order.get("program", task.get("description", ""))
            task["image"] = order.get("image", "")
            task["inputDataRef"] = order.get("inputDataRef", "")
            task["inputFilename"] = order.get("inputFilename", "")
            task["progress"] = max(task.get("progress", 0), 10)
            task["files"] = task_files
        else:
            self.tasks[linked_task_id] = {
                "taskId": linked_task_id,
                "title": f"Compute Order {order_id}",
                "description": order.get("program", "Order program"),
                "taskType": "compute_order",
                "status": "running",
                "priority": "normal",
                "price": order.get("totalPrice", 0),
                "coin": "MAIN",
                "gpuType": order.get("gpuType", "GPU"),
                "gpuCount": order.get("gpuCount", 1),
                "estimatedHours": order.get("durationHours", 1),
                "progress": 10,
                "program": order.get("program", ""),
                "image": order.get("image", ""),
                "inputDataRef": order.get("inputDataRef", ""),
                "inputFilename": order.get("inputFilename", ""),
                "runningTime": "0:00:01",
                "files": task_files,
                "createdAt": datetime.datetime.now().isoformat() + "Z",
                "buyerId": order.get("buyerAddress", ""),
                "minerId": miner_identity,
                "orderId": order_id,
            }

        if not hasattr(self, "_demo_main_balances"):
            self._demo_main_balances = {}
        if not hasattr(self, "_demo_fee_pool"):
            self._demo_fee_pool = {"platform": 0.0, "treasury": 0.0}

        gross = float(order.get("totalPrice", 0.0))
        miner_payout = float(order.get("minerPayout", gross))
        platform_fee = float(order.get("platformFee", 0.0))
        treasury_fee = float(order.get("treasuryFee", 0.0))

        self._demo_main_balances[miner_identity] = self._demo_main_balances.get(miner_identity, 0.0) + miner_payout
        self._demo_fee_pool["platform"] += platform_fee
        self._demo_fee_pool["treasury"] += treasury_fee
        order["minerBalanceAfter"] = round(self._demo_main_balances[miner_identity], 8)
        
        return {
            "status": "success",
            "orderId": order_id,
            "taskId": linked_task_id,
            "acceptedBy": miner_identity,
            "totalPrice": gross,
            "minerPayout": round(miner_payout, 8),
            "platformFee": round(platform_fee, 8),
            "treasuryFee": round(treasury_fee, 8),
            "minerBalanceAfter": order["minerBalanceAfter"],
            "message": "订单已接",
        }

    def _compute_complete_order(self, order_id: str, result_data: str = "", task_id: str = None, **kwargs) -> Dict:
        """完成算力订单并回填结果给下单账户（Demo 流程）"""
        if self.compute_market and hasattr(self.compute_market, "submit_result") and hasattr(self.compute_market, "get_order"):
            try:
                v3_order = self.compute_market.get_order(order_id)
                if v3_order:
                    miner_identity = (
                        kwargs.get("miner_id")
                        or kwargs.get("miner_address")
                        or self.miner_address
                        or (v3_order.assigned_miners[0] if v3_order.assigned_miners else "")
                    )
                    if not miner_identity:
                        return {
                            "status": "failed",
                            "orderId": order_id,
                            "message": "miner_identity_required",
                            "path": "compute_market_v3",
                        }

                    result_hash = kwargs.get("result_hash") or kwargs.get("resultHash")
                    if not result_hash:
                        result_hash = hashlib.sha256(
                            (result_data or f"result::{order_id}").encode("utf-8")
                        ).hexdigest()

                    result_encrypted = kwargs.get("result_encrypted", kwargs.get("resultEncrypted", ""))
                    ok, msg = self.compute_market.submit_result(
                        order_id,
                        miner_identity,
                        result_hash,
                        result_encrypted,
                    )
                    return {
                        "status": "success" if ok else "failed",
                        "orderId": order_id,
                        "taskId": task_id or f"task_{order_id}",
                        "acceptedBy": miner_identity,
                        "resultHash": result_hash,
                        "message": msg,
                        "path": "compute_market_v3",
                    }
            except Exception as e:
                logger.exception("compute_completeOrder V3 path failed")
                if self._compute_v3_required():
                    return {
                        "status": "failed",
                        "orderId": order_id,
                        "message": f"compute_market_v3_error:{type(e).__name__}",
                        "path": "compute_market_v3",
                    }

        if not order_id or order_id not in self.market_orders:
            raise RPCError(RPCErrorCode.INVALID_PARAMS.value, f"订单不存在 {order_id}")

        order = self.market_orders[order_id]
        linked_task_id = task_id or order.get("taskId") or f"task_{order_id}"

        # 优先执行用户代码，保证“改代码 -> 结果变化”可验证。
        program_code = ""
        if linked_task_id in self.tasks:
            program_code = str(self.tasks[linked_task_id].get("program") or "")
        if not program_code:
            program_code = str(order.get("program") or "")

        execution_info = {
            "executed": False,
            "executionMode": "manual_fallback",
            "success": False,
            "error": "",
            "output": None,
        }

        if program_code.strip():
            try:
                from core.sandbox_executor import SandboxExecutor

                if not hasattr(self, "_demo_sandbox_executor"):
                    self._demo_sandbox_executor = SandboxExecutor(
                        log_fn=lambda _msg: None,
                        force_simulate=False,
                        file_manager=getattr(self, "_file_manager", None),
                    )

                # 预置 result，用户代码可覆盖；如果未覆盖则仍有稳定输出。
                wrapped_code = "\n".join([
                    "result = {'status': 'ok', 'message': 'program executed'}",
                    "# User program starts",
                    program_code,
                ])

                task_data_hash = hashlib.sha256(
                    f"{linked_task_id}:{order_id}:{program_code}".encode("utf-8")
                ).hexdigest()
                ctx = self._demo_sandbox_executor.create_context(
                    miner_id=order.get("minerId") or self.miner_address or f"miner_{self.node_id}",
                    job_id=linked_task_id,
                    task_data_hash=task_data_hash,
                    task_code=wrapped_code,
                    task_data={
                        "task_id": linked_task_id,
                        "order_id": order_id,
                        "buyer": order.get("buyerAddress"),
                        "miner": order.get("minerId"),
                    },
                )
                sandbox_result = self._demo_sandbox_executor.execute(
                    ctx.context_id,
                    simulate_computation=False,
                )

                if sandbox_result:
                    execution_info["executed"] = True
                    execution_info["executionMode"] = "sandbox"
                    execution_info["success"] = bool(sandbox_result.success)
                    execution_info["error"] = sandbox_result.error_message or ""
                    execution_info["output"] = sandbox_result.output_data
            except Exception as e:
                execution_info["executed"] = True
                execution_info["executionMode"] = "sandbox_error"
                execution_info["success"] = False
                execution_info["error"] = str(e)

        if execution_info["success"]:
            # 使用真实执行输出
            output = execution_info.get("output")
            if isinstance(output, str):
                final_result = output
            else:
                final_result = json.dumps(output, ensure_ascii=False)
        else:
            # 兜底：使用前端传入结果，保持旧流程兼容。
            final_result = result_data or f"docker_executed_ok::{linked_task_id}"

        if linked_task_id in self.tasks:
            task = self.tasks[linked_task_id]
            task["status"] = "completed"
            task["progress"] = 100
            final_result_hash = hashlib.sha256(final_result.encode()).hexdigest()
            task["finalResult"] = final_result
            task["outputs"] = [
                {
                    "name": "result.json",
                    "size": "1.2 KB",
                    "hash": final_result_hash[:16],
                    "content": json.dumps({
                        "taskId": linked_task_id,
                        "orderId": order_id,
                        "status": "completed",
                        "resultData": final_result,
                        "execution": execution_info,
                        "resultHash": final_result_hash,
                        "resultLength": len(final_result),
                    }, ensure_ascii=False, indent=2),
                }
            ]
            task["completedAt"] = datetime.datetime.now().isoformat() + "Z"

        order["status"] = "completed"
        order["result"] = final_result
        order["finishedAt"] = int(time.time() * 1000)

        return {
            "status": "success",
            "orderId": order_id,
            "taskId": linked_task_id,
            "result": order["result"],
            "execution": execution_info,
            "message": "订单已完成，结果已回传下单账户",
        }

    def _compute_commit_result(self,
                               order_id: str,
                               miner_id: str,
                               commit_hash: str,
                               **kwargs) -> Dict:
        """提交订单结果承诺哈希（ComputeMarketV3）。"""
        if self.compute_market and hasattr(self.compute_market, "commit_result"):
            ok, msg = self.compute_market.commit_result(order_id, miner_id, commit_hash)
            return {
                "status": "success" if ok else "failed",
                "orderId": order_id,
                "minerId": miner_id,
                "message": msg,
            }
        return {
            "status": "failed",
            "orderId": order_id,
            "message": "compute_market_commit_unavailable",
        }

    def _compute_reveal_result(self,
                               order_id: str,
                               miner_id: str,
                               result_hash: str,
                               result_encrypted: str = "",
                               **kwargs) -> Dict:
        """提交订单结果 reveal 并触发结算（ComputeMarketV3）。"""
        if self.compute_market and hasattr(self.compute_market, "reveal_result"):
            ok, msg = self.compute_market.reveal_result(
                order_id,
                miner_id,
                result_hash,
                result_encrypted,
            )
            return {
                "status": "success" if ok else "failed",
                "orderId": order_id,
                "minerId": miner_id,
                "message": msg,
            }
        return {
            "status": "failed",
            "orderId": order_id,
            "message": "compute_market_reveal_unavailable",
        }

    def _compute_get_order_events(self, order_id: str, limit: int = 100, **kwargs) -> Dict:
        """查询订单生命周期交易事件（ComputeMarketV3）。"""
        if self.compute_market and hasattr(self.compute_market, "get_order_events"):
            auth_context = kwargs.get("auth_context", {})
            caller = self._get_auth_user(auth_context, self.miner_address or "anonymous")
            is_admin = bool(auth_context.get("is_admin", False))

            owner_addr = ""
            if hasattr(self.compute_market, "get_order"):
                try:
                    order = self.compute_market.get_order(order_id)
                    owner_addr = str(getattr(order, "buyer_address", "") or "") if order else ""
                except Exception:
                    owner_addr = ""

            can_view_full = bool(owner_addr) and (caller == owner_addr or is_admin)
            events = self.compute_market.get_order_events(order_id, limit)

            if not can_view_full:
                redacted_events = []
                for e in events:
                    if not isinstance(e, dict):
                        continue
                    redacted_events.append({
                        "action": e.get("action", ""),
                        "submitted": bool(e.get("submitted", False)),
                        "created_at": e.get("created_at", 0),
                    })
                return {
                    "status": "success",
                    "orderId": order_id,
                    "events": redacted_events,
                    "total": len(redacted_events),
                    "permissionScope": "public_redacted",
                }

            return {
                "status": "success",
                "orderId": order_id,
                "events": events,
                "total": len(events),
                "permissionScope": "owner_or_admin",
            }
        return {
            "status": "failed",
            "orderId": order_id,
            "events": [],
            "total": 0,
            "message": "compute_market_events_unavailable",
        }
    
    def _compute_cancel_order(self, order_id: str, **kwargs) -> Dict:
        """取消算力订单（需验证所有权）"""
        if self.compute_market and hasattr(self.compute_market, "cancel_order"):
            requester = kwargs.get("buyer_address") or self.miner_address or ""
            reason = kwargs.get("reason", "")
            ok, msg = self.compute_market.cancel_order(order_id, requester=requester, reason=reason)
            if ok:
                return {"status": "success", "orderId": order_id, "message": msg}

        if not order_id or order_id not in self.market_orders:
            raise RPCError(RPCErrorCode.INVALID_PARAMS.value, f"订单不存在 {order_id}")
        
        # Security: verify order ownership
        auth_context = kwargs.get("auth_context", {})
        caller = self._get_auth_user(auth_context, self.miner_address or "anonymous")
        order = self.market_orders[order_id]
        order_owner = order.get("minerId") or order.get("owner")
        if order_owner and order_owner != caller and not auth_context.get("is_admin", False):
            raise RPCError(-32403, "Permission denied: you can only cancel your own orders")
        
        del self.market_orders[order_id]
        return {"status": "success", "orderId": order_id}
    
    # ============== 见证方法 ==============
    
    def _witness_request(self, params: Dict) -> Dict:
        """请求见证"""
        return {"witness_id": str(uuid.uuid4())[:8]}
    
    def _witness_get_status(self, params: Dict) -> Dict:
        """获取见证状"""
        return {"status": "unknown"}
    
    # ============== 治理方法 ==============
    
    def _governance_vote(self, proposal_id: str = None, vote: str = None, **kwargs) -> Dict:
        """投票 - 真实数据 (with duplicate vote prevention)"""
        if not proposal_id or proposal_id not in self.proposals:
            return {"status": "error", "message": "提案不存在"}
        
        proposal = self.proposals[proposal_id]
        if proposal["status"] != "voting":
            return {"status": "error", "message": "提案不在投票中"}
        
        # Security: Prevent duplicate voting per address
        auth_context = kwargs.get("auth_context", {})
        voter_addr = self._get_auth_user(auth_context, self.miner_address or "anonymous")
        
        if "voters" not in proposal:
            proposal["voters"] = set()
        if voter_addr in proposal["voters"]:
            return {"status": "error", "message": "该地址已投票，不能重复投票 (duplicate vote rejected)"}
        proposal["voters"].add(voter_addr)
        
        if vote not in ("for", "against", "abstain"):
            return {"status": "error", "message": "无效的投票选项 (invalid vote option)"}
        
        # 记录投票
        if vote == "for":
            proposal["votesFor"] += 1
        elif vote == "against":
            proposal["votesAgainst"] += 1
        elif vote == "abstain":
            proposal["votesAbstain"] += 1
        
        return {"status": "vote_recorded", "proposalId": proposal_id, "vote": vote}
    
    def _governance_get_proposals(self, status: str = None, **kwargs) -> List[Dict]:
        """获取提案列表 - 真实数据"""
        proposals = list(self.proposals.values())
        
        if status:
            proposals = [p for p in proposals if p["status"] == status]
        
        # 按创建时间排
        proposals.sort(key=lambda x: x.get("createdAt", ""), reverse=True)
        return proposals
    
    def _governance_get_proposal(self, proposal_id: str = None, **kwargs) -> Optional[Dict]:
        """获取单个提案详情"""
        if not proposal_id:
            return None
        return self.proposals.get(proposal_id)
    
    def _governance_create_proposal(
        self,
        title: str = None,
        description: str = None,
        category: str = "parameter",
        fundingAmount: float = None,
        fundingRecipient: str = None,
        **kwargs
    ) -> Dict:
        """创建治理提案"""
        if not title:
            return {"success": False, "message": "提案标题不能为空"}
        
        proposal_id = f"prop_{uuid.uuid4().hex[:8]}"
        now = datetime.datetime.now()
        voting_start = now + datetime.timedelta(hours=1)
        voting_end = voting_start + datetime.timedelta(days=7)
        
        proposal = {
            "proposalId": proposal_id,
            "title": title,
            "description": description or "",
            "category": category,
            "status": "voting",
            "votesFor": 0,
            "votesAgainst": 0,
            "votesAbstain": 0,
            "quorum": 100,
            "threshold": 50,
            "createdAt": now.isoformat() + "Z",
            "votingStartsAt": voting_start.isoformat() + "Z",
            "votingEndsAt": voting_end.isoformat() + "Z",
            "proposerId": self.miner_address or "proposer_local",
        }
        
        if category == "funding" and fundingAmount:
            proposal["fundingAmount"] = fundingAmount
            proposal["fundingRecipient"] = fundingRecipient
        
        self.proposals[proposal_id] = proposal
        
        return {
            "success": True,
            "proposal": proposal,
            "message": "提案创建成功"
        }
    
    # ============== 贡献权重治理方法 (Phase 11) ==============
    
    def _get_contribution_governance(self):
        """获取或创建贡献权重治理引"""
        if not hasattr(self, '_contrib_gov'):
            from .contribution_governance import ContributionGovernance
            self._contrib_gov = ContributionGovernance()
        return self._contrib_gov
    
    def _contrib_create_proposal(
        self,
        proposer: str,
        proposalType: str,
        title: str,
        description: str = "",
        targetParam: str = "",
        oldValue: any = None,
        newValue: any = None,
        changes: Dict = None,
        currentBlock: int = 0,
        **kwargs
    ) -> Dict:
        """创建贡献权重治理提案"""
        gov = self._get_contribution_governance()
        from .contribution_governance import ProposalType
        
        try:
            ptype = ProposalType(proposalType)
        except ValueError:
            return {"status": "error", "message": f"无效的提案类 {proposalType}"}
        
        proposal, msg = gov.create_proposal(
            proposer=proposer,
            proposal_type=ptype,
            title=title,
            description=description,
            target_param=targetParam,
            old_value=oldValue,
            new_value=newValue,
            changes=changes or {},
            current_block=currentBlock
        )
        
        if proposal:
            return {
                "status": "success",
                "proposal": proposal.to_dict(),
                "message": msg
            }
        else:
            return {"status": "error", "message": msg}
    
    def _contrib_vote(
        self,
        proposalId: str,
        voter: str,
        choice: str,
        currentBlock: int = 0,
        **kwargs
    ) -> Dict:
        """贡献权重治理投票"""
        gov = self._get_contribution_governance()
        from .contribution_governance import VoteChoice
        
        try:
            vote_choice = VoteChoice(choice)
        except ValueError:
            return {"status": "error", "message": f"无效的投票选项: {choice}，可 support/oppose/abstain"}
        
        ok, msg = gov.vote(proposalId, voter, vote_choice, currentBlock)
        
        if ok:
            proposal = gov.get_proposal(proposalId)
            return {
                "status": "success",
                "message": msg,
                "proposal": proposal.to_dict() if proposal else None
            }
        else:
            return {"status": "error", "message": msg}
    
    def _contrib_get_proposals(self, status: str = None, limit: int = 50, **kwargs) -> List[Dict]:
        """获取贡献权重治理提案列表"""
        gov = self._get_contribution_governance()
        from .contribution_governance import ProposalStatus
        
        pstatus = None
        if status:
            try:
                pstatus = ProposalStatus(status)
            except ValueError:
                pass
        
        proposals = gov.get_proposals(status=pstatus, limit=limit)
        return [p.to_dict() for p in proposals]
    
    def _contrib_get_proposal(self, proposalId: str, **kwargs) -> Dict:
        """获取单个提案详情"""
        gov = self._get_contribution_governance()
        proposal = gov.get_proposal(proposalId)
        
        if proposal:
            votes = gov.get_votes(proposalId)
            return {
                "proposal": proposal.to_dict(),
                "votes": [v.to_dict() for v in votes],
                "status": "success"
            }
        else:
            return {"status": "error", "message": "提案不存在"}
    
    def _contrib_get_voter_power(self, address: str, **kwargs) -> Dict:
        """获取用户投票权详情"""
        gov = self._get_contribution_governance()
        return gov.get_voter_power(address)
    
    def _contrib_simulate_vote(
        self,
        proposalId: str,
        voter: str,
        choice: str,
        **kwargs
    ) -> Dict:
        """模拟投票影响"""
        gov = self._get_contribution_governance()
        from .contribution_governance import VoteChoice
        
        try:
            vote_choice = VoteChoice(choice)
        except ValueError:
            return {"error": f"无效的投票选项: {choice}"}
        
        return gov.simulate_vote_impact(proposalId, voter, vote_choice)
    
    def _contrib_stake(
        self,
        address: str,
        amount: float,
        lockDays: int,
        **kwargs
    ) -> Dict:
        """锁仓质押"""
        gov = self._get_contribution_governance()
        ok, result = gov.weight_calc.stake(address, amount, lockDays)
        
        if ok:
            power = gov.get_voter_power(address)
            return {
                "status": "success",
                "stakeId": result,
                "voterPower": power
            }
        else:
            return {"status": "error", "message": result}
    
    def _contrib_unstake(self, stakeId: str, address: str, **kwargs) -> Dict:
        """解除锁仓"""
        gov = self._get_contribution_governance()
        ok, msg, amount = gov.weight_calc.unstake(stakeId, address)
        
        if ok:
            power = gov.get_voter_power(address)
            return {
                "status": "success",
                "amount": amount,
                "message": msg,
                "voterPower": power
            }
        else:
            return {"status": "error", "message": msg}
    
    def _contrib_finalize_proposal(self, proposalId: str, **kwargs) -> Dict:
        """结算提案"""
        gov = self._get_contribution_governance()
        ok, msg = gov.finalize_proposal(proposalId)
        
        proposal = gov.get_proposal(proposalId)
        return {
            "status": "success" if ok else "error",
            "message": msg,
            "proposal": proposal.to_dict() if proposal else None
        }
    
    def _contrib_execute_proposal(self, proposalId: str, executor: str = None, **kwargs) -> Dict:
        """执行已通过提案"""
        gov = self._get_contribution_governance()
        ok, msg = gov.execute_proposal(proposalId, executor)
        
        proposal = gov.get_proposal(proposalId)
        return {
            "status": "success" if ok else "error",
            "message": msg,
            "proposal": proposal.to_dict() if proposal else None
        }
    
    def _contrib_get_stats(self, **kwargs) -> Dict:
        """获取治理统计"""
        gov = self._get_contribution_governance()
        return gov.get_stats()
    
    def _contrib_check_proposer_eligibility(self, address: str, **kwargs) -> Dict:
        """
        检查地址是否有资格提交提
        
        返回提案人资格信息：权重、最低要求、是否满足条
        """
        gov = self._get_contribution_governance()
        eligible, reason, proposer_weight, min_required = gov.check_proposer_eligibility(address)
        
        return {
            "address": address,
            "eligible": eligible,
            "reason": reason,
            "proposerWeight": proposer_weight,
            "minRequired": min_required,
            "weightRatio": (proposer_weight / min_required * 100) if min_required > 0 else 0
        }
    
    def _contrib_get_proposal_time_remaining(self, proposalId: str, **kwargs) -> Dict:
        """获取提案剩余时间信息"""
        gov = self._get_contribution_governance()
        return gov.get_proposal_time_remaining(proposalId)
    
    def _contrib_check_expired_proposals(self, **kwargs) -> Dict:
        """检查并标记过期提案"""
        gov = self._get_contribution_governance()
        expired_ids = gov.check_and_expire_proposals()
        
        return {
            "status": "success",
            "expiredCount": len(expired_ids),
            "expiredProposals": expired_ids
        }
    
    def _contrib_get_pass_requirements(self, proposalId: str = None, riskLevel: str = None, **kwargs) -> Dict:
        """
        获取提案通过的所有要
        
        Args:
            proposalId: 可选，指定提案ID获取其具体要求
            riskLevel: 可选，指定风险等级获取通用要求 (low/medium/high)
        """
        from .contribution_governance import GovernanceConfig, ProposalRisk
        
        if proposalId:
            gov = self._get_contribution_governance()
            proposal = gov.get_proposal(proposalId)
            if not proposal:
                return {"status": "error", "message": "提案不存在"}
            
            return {
                "status": "success",
                "proposalId": proposalId,
                "riskLevel": proposal.risk_level.value,
                "requirements": {
                    "quorumPercent": GovernanceConfig.QUORUM_PERCENT,
                    "approvalThreshold": proposal.get_threshold(),
                    "minSupportWeight": proposal.get_min_support_weight(),
                    "supportMustExceedOppose": True,
                    "votingPeriodDays": GovernanceConfig.VOTING_PERIOD_DAYS,
                    "timelockHours": GovernanceConfig.TIMELOCK_HOURS,
                    "expireDays": GovernanceConfig.PROPOSAL_EXPIRE_DAYS
                },
                "currentStatus": proposal.get_pass_status()
            }
        
        # 返回通用要求
        risk = ProposalRisk.LOW
        if riskLevel:
            try:
                risk = ProposalRisk(riskLevel)
            except ValueError:
                pass
        
        min_support = {
            ProposalRisk.LOW: GovernanceConfig.MIN_SUPPORT_WEIGHT,
            ProposalRisk.MEDIUM: GovernanceConfig.MIN_SUPPORT_WEIGHT_FEATURE,
            ProposalRisk.HIGH: GovernanceConfig.MIN_SUPPORT_WEIGHT_STRUCTURAL,
        }.get(risk, GovernanceConfig.MIN_SUPPORT_WEIGHT)
        
        threshold = GovernanceConfig.STRUCTURAL_THRESHOLD if risk == ProposalRisk.HIGH else GovernanceConfig.APPROVAL_THRESHOLD
        
        return {
            "status": "success",
            "riskLevel": risk.value,
            "requirements": {
                "quorumPercent": GovernanceConfig.QUORUM_PERCENT,
                "approvalThreshold": threshold,
                "minSupportWeight": min_support,
                "supportMustExceedOppose": True,
                "votingPeriodDays": GovernanceConfig.VOTING_PERIOD_DAYS,
                "timelockHours": GovernanceConfig.TIMELOCK_HOURS,
                "expireDays": GovernanceConfig.PROPOSAL_EXPIRE_DAYS,
                "cooldownHours": GovernanceConfig.COOLDOWN_HOURS
            },
            "proposerRequirements": {
                "minWeight": GovernanceConfig.MIN_PROPOSER_WEIGHT,
                "minWeightPercent": GovernanceConfig.MIN_PROPOSER_WEIGHT_PERCENT,
                "bondAmount": {
                    "low": GovernanceConfig.BOND_PARAM,
                    "medium": GovernanceConfig.BOND_FEATURE,
                    "high": GovernanceConfig.BOND_STRUCTURAL
                }
            }
        }

    # ============== 加密任务方法实现 ==============
    
    def _get_encrypted_task_manager(self):
        """获取或创建加密任务管理器"""
        if not hasattr(self, '_encrypted_task_manager'):
            from .encrypted_task import EncryptedTaskManager, TaskSettlementContract
            self._encrypted_task_manager = EncryptedTaskManager(log_fn=lambda x: None)
            self._settlement_contract = TaskSettlementContract(log_fn=lambda x: None)
            # 启动上传超时守护线程
            self._start_upload_watchdog()
        return self._encrypted_task_manager, self._settlement_contract
    
    def _encrypted_task_generate_keypair(self, **kwargs) -> Dict:
        """生成密钥"""
        from .encrypted_task import HybridEncryption
        keypair = HybridEncryption.generate_keypair()
        return {
            "keyId": keypair.key_id,
            "publicKey": keypair.public_key_pem,
            "createdAt": keypair.created_at,
            "note": "Private key is stored server-side. Use keyId to reference it.",
        }
    
    def _encrypted_task_register_miner(self, minerId: str, publicKey: str = None, **kwargs) -> Dict:
        """注册矿工公钥"""
        manager, _ = self._get_encrypted_task_manager()
        
        import base64
        public_key_bytes = base64.b64decode(publicKey) if publicKey else None
        keypair = manager.register_miner(minerId, public_key_bytes)
        
        return {
            "minerId": minerId,
            "keyId": keypair.key_id,
            "publicKey": keypair.public_key_pem,
            "registered": True,
        }
    
    def _encrypted_task_create(
        self,
        title: str,
        description: str = "",
        codeData: str = "",
        inputData: str = "",
        inputDataRef: str = "",
        requirements: str = "",
        taskType: str = "compute",
        estimatedHours: float = 1.0,
        budgetPerHour: float = 10.0,
        maxMemoryGb: float = 8.0,
        maxTimeoutHours: float = 0,
        receivers: List[str] = None,
        userPublicKey: str = "",
        **kwargs
    ) -> Dict:
        """创建加密任务
        
        支持两种数据输入方式：
        1. inputData: Base64 内联数据（适合 < 100MB 的小数据）
        2. inputDataRef: 通过 file_initUpload 上传的文件引用 ID（适合大数据集）
        
        资源配置：
        - maxMemoryGb: 容器最大内存（默认 8GB，最大 64GB）
        - maxTimeoutHours: 最大执行时间（默认按 estimatedHours，最大 72h）
        """
        manager, contract = self._get_encrypted_task_manager()
        from .encrypted_task import HybridEncryption, KeyPair
        import base64
        
        user_id = self.miner_address or "user_default"
        
        # 创建或使用提供的密钥
        if userPublicKey:
            user_keypair = KeyPair(
                public_key=base64.b64decode(userPublicKey),
                private_key=b""
            )
        else:
            user_keypair = HybridEncryption.generate_keypair()
        
        # 转换数据
        code_bytes = base64.b64decode(codeData) if codeData else b""
        requirements_bytes = requirements.encode("utf-8") if requirements else b""
        
        # 输入数据：支持内联 base64 或文件引用
        input_bytes = b""
        input_data_ref_path = ""
        if inputDataRef:
            # 大文件模式：验证文件引用有效，稍后直接挂载到容器
            file_info = self._file_manager.get_file_info(inputDataRef)
            if not file_info:
                raise RPCError(
                    RPCErrorCode.INVALID_PARAMS.value,
                    f"inputDataRef 引用的文件不存在: {inputDataRef}"
                )
            input_data_ref_path = self._file_manager.get_file_path(inputDataRef) or ""
        elif inputData:
            input_bytes = base64.b64decode(inputData)
        
        # 代码安全扫描（加密前在用户侧扫描，拒绝恶意代码）
        if code_bytes:
            from .sandbox_executor import CodeScanner
            try:
                code_text = code_bytes.decode("utf-8", errors="replace")
            except Exception:
                raise RPCError(RPCErrorCode.INVALID_PARAMS.value, "代码解码失败：非有效文本文件")
            is_safe, scan_warnings = CodeScanner.scan(code_text)
            if not is_safe:
                raise RPCError(
                    RPCErrorCode.INVALID_PARAMS.value,
                    f"代码安全扫描未通过: {'; '.join(scan_warnings)}"
                )
        
        # 防刷单：禁止用户指定矿工，防止用户和矿工串通刷单套取补偿
        # receivers 参数忽略用户输入，由系统自动分配矿工
        system_receivers = []
        if self.compute_scheduler:
            try:
                available = self.compute_scheduler._get_available_miners(
                    sector=taskType.upper() if taskType else "MAIN"
                )
                if available:
                    import secrets
                    _secure_rng = secrets.SystemRandom()
                    _secure_rng.shuffle(available)
                    system_receivers = [m.miner_id for m in available[:3]]
            except Exception:
                pass
        
        # 确保系统分配的接收者已注册
        for miner_id in system_receivers:
            if miner_id not in manager.node_keys:
                manager.register_miner(miner_id)
        
        task = manager.create_task(
            user_id=user_id,
            user_keypair=user_keypair,
            title=title,
            description=description,
            code_data=code_bytes,
            input_data=input_bytes,
            requirements_data=requirements_bytes,
            task_type=taskType,
            estimated_hours=estimatedHours,
            budget_per_hour=budgetPerHour,
            receivers=system_receivers,
        )
        
        # 附加大文件引用和资源配置到任务元数据
        # （这些数据在任务被调度到矿工时会传递给 SandboxExecutor）
        clamped_mem = max(1.0, min(64.0, maxMemoryGb))
        timeout_hours = maxTimeoutHours if maxTimeoutHours > 0 else estimatedHours
        clamped_timeout = max(0.05, min(72.0, timeout_hours))
        task._extra_meta = {
            "input_data_ref": inputDataRef,
            "input_data_ref_path": input_data_ref_path,
            "max_memory_gb": clamped_mem,
            "timeout_seconds": clamped_timeout * 3600,
            "tmp_size_mb": min(int(clamped_mem * 1024), 10240),
        }
        
        # 预算验证 —— 检查用户真实链上余额是否足够
        required_budget = task.total_budget
        real_balance = 0.0
        try:
            balance_info = self._account_get_balance(address=user_id)
            real_balance = balance_info.get("mainBalance", 0.0)
        except Exception:
            pass
        
        # 合约内已有余额 + 链上余额
        contract_balance = contract.get_balance(user_id)
        available = contract_balance + real_balance
        if available < required_budget:
            raise RPCError(
                RPCErrorCode.INVALID_REQUEST.value,
                f"余额不足: 需要 {required_budget:.4f} MAIN，可用 {available:.4f} MAIN"
            )
        
        # 仅充值差额（而非凭空注入双倍）
        shortfall = max(0, required_budget - contract_balance)
        if shortfall > 0:
            contract.deposit(user_id, shortfall)
        
        if not contract.lock_budget(task.task_id, user_id, required_budget):
            raise RPCError(
                RPCErrorCode.INTERNAL_ERROR.value,
                "预算锁定失败"
            )
        
        # 检查是否可以 P2P 直连（矿工已注册端点时提供票据）
        p2p_info = None
        if system_receivers:
            tm = self._get_ticket_manager()
            first_miner = system_receivers[0]
            if tm.is_miner_p2p_ready(first_miner):
                user_pub = user_keypair.public_key if hasattr(user_keypair, 'public_key') and isinstance(user_keypair.public_key, bytes) else b""
                ticket = tm.create_ticket(
                    task_id=task.task_id,
                    user_id=user_id,
                    miner_id=first_miner,
                    user_pubkey=user_pub,
                )
                if ticket and ticket.transfer_mode == "p2p":
                    p2p_info = {
                        "transferMode": "p2p",
                        "ticket": ticket.to_dict(),
                    }
        
        result = {
            "taskId": task.task_id,
            "title": task.title,
            "status": task.chain_status.value,
            "chainLength": task.chain_length,
            "estimatedBudget": task.total_budget,
            "userPublicKey": user_keypair.public_key_pem,
            "receivers": system_receivers,
            "createdAt": task.created_at,
            "resourceConfig": {
                "maxMemoryGb": clamped_mem,
                "timeoutHours": clamped_timeout,
                "tmpSizeMb": min(int(clamped_mem * 1024), 10240),
            },
        }
        
        if p2p_info:
            result["p2pTransfer"] = p2p_info
        
        return result
    
    def _encrypted_task_submit(self, taskId: str, userPrivateKey: str = "", **kwargs) -> Dict:
        """提交加密任务"""
        manager, _ = self._get_encrypted_task_manager()
        import base64
        
        private_key = base64.b64decode(userPrivateKey) if userPrivateKey else b""
        
        success = manager.encrypt_and_submit(taskId, private_key)
        
        if success:
            task = manager.tasks.get(taskId)
            return {
                "taskId": taskId,
                "status": task.chain_status.value if task else "unknown",
                "submitted": True,
                "submittedAt": task.submitted_at if task else None,
            }
        else:
            raise RPCError(RPCErrorCode.INTERNAL_ERROR.value, "Failed to submit task")
    
    def _encrypted_task_cancel(self, taskId: str, **kwargs) -> Dict:
        """手动取消加密任务 — 退还预算，若矿工已接收数据则从国库补偿"""
        import logging
        from .encrypted_task import TaskChainStatus
        logger = logging.getLogger(__name__)
        
        manager, contract = self._get_encrypted_task_manager()
        task = manager.tasks.get(taskId)
        if not task:
            raise RPCError(RPCErrorCode.INVALID_PARAMS.value, f"任务不存在: {taskId}")
        
        # 只允许取消 CREATED（上传中）或 SUBMITTED（已提交但未开始）的任务
        if task.chain_status not in (TaskChainStatus.CREATED, TaskChainStatus.SUBMITTED):
            raise RPCError(
                RPCErrorCode.INVALID_REQUEST.value,
                f"当前状态不允许取消: {task.chain_status.value}"
            )
        
        self._cancel_upload_timeout_task(task, contract, logger)
        
        return {
            "taskId": taskId,
            "status": task.chain_status.value,
            "refunded": task._extra_meta.get("refunded", 0),
            "minerCompensation": task._extra_meta.get("miner_compensation", 0),
            "cancelReason": "user_cancelled",
        }
    
    def _encrypted_task_get_status(self, taskId: str, **kwargs) -> Dict:
        """获取加密任务状"""
        manager, _ = self._get_encrypted_task_manager()
        return manager.get_task_status(taskId)
    
    def _encrypted_task_get_result(self, taskId: str, userPrivateKey: str = "", **kwargs) -> Dict:
        """获取加密任务结果"""
        manager, _ = self._get_encrypted_task_manager()
        import base64
        
        task = manager.tasks.get(taskId)
        if not task:
            raise RPCError(RPCErrorCode.INVALID_PARAMS.value, "Task not found")
        
        if task.chain_status.value != "completed":
            return {
                "taskId": taskId,
                "status": task.chain_status.value,
                "completed": False,
                "result": None,
            }
        
        if userPrivateKey:
            private_key = base64.b64decode(userPrivateKey)
            result = manager.decrypt_result(taskId, private_key)
            return {
                "taskId": taskId,
                "status": task.chain_status.value,
                "completed": True,
                "result": base64.b64encode(result).decode() if result else None,
                "resultHash": task.final_result_hash,
            }
        else:
            return {
                "taskId": taskId,
                "status": task.chain_status.value,
                "completed": True,
                "resultHash": task.final_result_hash,
                "note": "Provide userPrivateKey to decrypt result",
            }
    
    def _encrypted_task_process(
        self,
        taskId: str,
        nodeId: str,
        minerPrivateKey: str = "",
        **kwargs
    ) -> Dict:
        """处理加密任务（矿工端）"""
        manager, contract = self._get_encrypted_task_manager()
        import base64
        
        private_key = base64.b64decode(minerPrivateKey) if minerPrivateKey else b""
        
        # 实际处理函数：计算输入数据的 SHA-256 哈希并附加处理标记
        def compute_process(data: bytes) -> bytes:
            import hashlib
            result_hash = hashlib.sha256(data).hexdigest()
            return f"PROCESSED:{result_hash}:{len(data)}bytes".encode() + data[:100]
        
        output = manager.process_at_node(taskId, nodeId, private_key, compute_process)
        
        task = manager.tasks.get(taskId)
        
        # 检查是否完
        if task and task.chain_status.value == "completed":
            # 结算
            transactions = contract.settle_task(task)
            
        return {
            "taskId": taskId,
            "nodeId": nodeId,
            "processed": output is not None,
            "taskStatus": task.chain_status.value if task else "unknown",
            "outputHash": output.data_hash if output else None,
        }
    
    def _encrypted_task_billing(self, taskId: str, **kwargs) -> Dict:
        """获取任务计费报告"""
        manager, contract = self._get_encrypted_task_manager()
        
        report = manager.generate_billing_report(taskId)
        
        # 添加余额信息
        task = manager.tasks.get(taskId)
        if task:
            report["userBalance"] = contract.get_balance(task.user_id)
            report["minerBalances"] = {}
            for node in task.chain_nodes:
                report["minerBalances"][node.miner_id] = contract.get_balance(node.miner_id)
        
        return report
    
    # ============== 大文件分块传输方法 ==============
    
    # 简易速率限制：每个方法最近调用时间戳队列
    _file_rate_limits: Dict[str, list] = {}
    _FILE_RATE_WINDOW = 60  # 60秒窗口
    _FILE_RATE_MAX = {
        "upload_chunk": 200,      # 每分钟最多200个分块（~800MB/min）
        "download_chunk": 100,    # 每分钟最多100个分块
        "init_upload": 10,        # 每分钟最多10次初始化
    }
    
    def _check_file_rate(self, action: str):
        """检查文件操作速率限制。"""
        now = time.time()
        max_calls = self._FILE_RATE_MAX.get(action, 100)
        if action not in self._file_rate_limits:
            self._file_rate_limits[action] = []
        
        # 清理过期记录
        window_start = now - self._FILE_RATE_WINDOW
        self._file_rate_limits[action] = [
            t for t in self._file_rate_limits[action] if t > window_start
        ]
        
        if len(self._file_rate_limits[action]) >= max_calls:
            raise RPCError(
                RPCErrorCode.INVALID_REQUEST.value,
                f"速率限制：{action} 每分钟最多 {max_calls} 次"
            )
        self._file_rate_limits[action].append(now)

    def _ensure_file_access(self, file_ref: str, auth_context: Dict) -> Dict:
        """校验文件访问权限：仅文件所有者或管理员可读。"""
        info = self._file_manager.get_file_info(file_ref)
        if not info:
            raise RPCError(RPCErrorCode.INVALID_PARAMS.value, f"文件不存在: {file_ref}")

        owner = str(info.get("owner") or "")
        caller = self._get_auth_user(auth_context, self.miner_address or "anonymous")
        is_admin = bool(auth_context.get("is_admin", False))

        if owner and caller != owner and not is_admin:
            raise RPCError(-32403, "Permission denied: only file owner can access")

        return info

    def _ensure_upload_session_access(self, upload_id: str, auth_context: Dict):
        """校验上传会话访问权限。"""
        owner = self._file_manager.get_upload_owner(upload_id)
        if owner is None:
            raise RPCError(RPCErrorCode.INVALID_PARAMS.value, f"上传会话不存在: {upload_id}")

        caller = self._get_auth_user(auth_context, self.miner_address or "anonymous")
        is_admin = bool(auth_context.get("is_admin", False))
        if owner and caller != owner and not is_admin:
            raise RPCError(-32403, "Permission denied: only uploader can access upload session")

    def _file_init_upload(
        self,
        filename: str,
        totalSize: int,
        checksumSha256: str = None,
        sha256Hash: str = None,
        **kwargs
    ) -> Dict:
        """初始化分块上传。
        
        前端先计算文件 SHA256，然后调用此方法获取 uploadId。
        后续通过 file_uploadChunk 逐块上传。
        """
        self._check_file_rate("init_upload")
        auth_context = kwargs.get("auth_context", {})
        owner = self._get_auth_user(auth_context, self.miner_address or "anonymous")
        checksum = checksumSha256 or sha256Hash
        if not checksum:
            raise RPCError(RPCErrorCode.INVALID_PARAMS.value, "缺少 checksumSha256/sha256Hash")
        return self._file_manager.init_upload(
            filename=filename,
            total_size=totalSize,
            checksum_sha256=checksum,
            owner=owner,
        )
    
    def _file_upload_chunk(
        self,
        uploadId: str,
        chunkIndex: int,
        data: str = None,
        chunkData: str = None,
        **kwargs
    ) -> Dict:
        """上传单个分块（data 为 Base64 编码）。"""
        self._check_file_rate("upload_chunk")
        auth_context = kwargs.get("auth_context", {})
        self._ensure_upload_session_access(uploadId, auth_context)
        payload = data or chunkData
        if not payload:
            raise RPCError(RPCErrorCode.INVALID_PARAMS.value, "缺少 data/chunkData")
        return self._file_manager.upload_chunk(
            upload_id=uploadId,
            chunk_index=chunkIndex,
            chunk_data_b64=payload,
        )
    
    def _file_finalize_upload(self, uploadId: str, **kwargs) -> Dict:
        """完成上传：校验 SHA256 并合并分块，返回 fileRef。"""
        auth_context = kwargs.get("auth_context", {})
        self._ensure_upload_session_access(uploadId, auth_context)
        return self._file_manager.finalize_upload(upload_id=uploadId)
    
    def _file_get_upload_progress(self, uploadId: str, **kwargs) -> Dict:
        """查询上传进度。"""
        auth_context = kwargs.get("auth_context", {})
        self._ensure_upload_session_access(uploadId, auth_context)
        result = self._file_manager.get_upload_progress(uploadId)
        if not result:
            raise RPCError(RPCErrorCode.INVALID_PARAMS.value, f"上传会话不存在: {uploadId}")
        return result
    
    def _file_get_info(self, fileRef: str, **kwargs) -> Dict:
        """获取已上传文件的元数据。"""
        if not re.fullmatch(r"[a-f0-9]{16}", str(fileRef or "")):
            raise RPCError(RPCErrorCode.INVALID_PARAMS.value, "非法 fileRef")
        auth_context = kwargs.get("auth_context", {})
        info = self._ensure_file_access(fileRef, auth_context)
        return info
    
    def _file_download_chunk(
        self,
        fileRef: str,
        offset: int = 0,
        length: int = 4194304,
        **kwargs
    ) -> Dict:
        """分块下载已上传的文件。"""
        if not re.fullmatch(r"[a-f0-9]{16}", str(fileRef or "")):
            raise RPCError(RPCErrorCode.INVALID_PARAMS.value, "非法 fileRef")
        auth_context = kwargs.get("auth_context", {})
        self._ensure_file_access(fileRef, auth_context)
        self._check_file_rate("download_chunk")
        return self._file_manager.download_chunk(
            file_ref=fileRef,
            offset=offset,
            length=length,
        )
    
    def _file_get_task_outputs(self, taskId: str, **kwargs) -> Dict:
        """获取任务输出文件清单（含模型文件）。"""
        auth_context = kwargs.get("auth_context", {})
        self._ensure_task_output_access(taskId, auth_context)
        manifest = self._file_manager.get_task_output_manifest(taskId)
        if not manifest:
            raise RPCError(RPCErrorCode.INVALID_PARAMS.value, f"任务输出不存在: {taskId}")
        return manifest
    
    def _file_download_task_output(
        self,
        taskId: str,
        filename: str,
        offset: int = 0,
        length: int = 4194304,
        **kwargs
    ) -> Dict:
        """分块下载任务输出文件（如训练好的模型权重）。"""
        auth_context = kwargs.get("auth_context", {})
        self._ensure_task_output_access(taskId, auth_context)
        self._check_file_rate("download_chunk")
        return self._file_manager.download_task_output_chunk(
            task_id=taskId,
            filename=filename,
            offset=offset,
            length=length,
        )
    
    def _file_cancel_upload(self, uploadId: str, **kwargs) -> Dict:
        """取消上传并清理临时文件。"""
        auth_context = kwargs.get("auth_context", {})
        self._ensure_upload_session_access(uploadId, auth_context)
        cancelled = self._file_manager.cancel_upload(uploadId)
        if not cancelled:
            raise RPCError(RPCErrorCode.INVALID_PARAMS.value, f"上传会话不存在: {uploadId}")
        return {"cancelled": True, "uploadId": uploadId}
    
    # ============== E2E 端到端加密方法 ==============

    def _e2e_create_session(self, **kwargs) -> Dict:
        """创建 E2E 加密会话。
        
        返回服务端临时 X25519 公钥，客户端用此公钥 + 自己的私钥做 ECDH。
        
        Returns:
            {sessionId, publicKey (hex)}
        """
        return self._file_manager.e2e_create_session()

    def _e2e_handshake(self, sessionId: str, publicKey: str, **kwargs) -> Dict:
        """完成 E2E 密钥协商。
        
        客户端将自己的 X25519 公钥发送过来，
        服务端完成 ECDH 派生出 AES-256 会话密钥。
        
        Args:
            sessionId: e2e_createSession 返回的会话 ID
            publicKey: 客户端 X25519 公钥（hex 编码，64 字符）
        
        Returns:
            {sessionId, ready: True}
        """
        if not sessionId or not publicKey:
            raise RPCError(RPCErrorCode.INVALID_PARAMS.value, "缺少 sessionId 或 publicKey")
        if len(publicKey) != 64:
            raise RPCError(RPCErrorCode.INVALID_PARAMS.value, "公钥格式错误（需要 64 字符 hex）")
        return self._file_manager.e2e_handshake(sessionId, publicKey)

    def _e2e_upload_chunk(
        self,
        uploadId: str,
        sessionId: str,
        chunkIndex: int,
        data: str,
        **kwargs
    ) -> Dict:
        """E2E 加密分块上传。
        
        客户端先用 E2E 会话密钥加密数据块（e2e_encrypt_chunk），
        再 Base64 编码通过此接口上传。
        服务端解密后按正常流程存储。
        传输过程中的数据始终是密文。
        """
        self._check_file_rate("upload_chunk")
        return self._file_manager.e2e_upload_chunk(
            upload_id=uploadId,
            chunk_index=chunkIndex,
            encrypted_data_b64=data,
            session_id=sessionId,
        )

    def _e2e_download_chunk(
        self,
        fileRef: str,
        sessionId: str,
        chunkIndex: int = 0,
        offset: int = 0,
        length: int = 4194304,
        **kwargs
    ) -> Dict:
        """E2E 加密分块下载。
        
        服务端读取文件块后用 E2E 会话密钥加密再返回。
        客户端收到后用自己的会话密钥解密。
        传输过程中的数据始终是密文。
        """
        if not re.fullmatch(r"[a-f0-9]{16}", str(fileRef or "")):
            raise RPCError(RPCErrorCode.INVALID_PARAMS.value, "非法 fileRef")
        auth_context = kwargs.get("auth_context", {})
        self._ensure_file_access(fileRef, auth_context)
        self._check_file_rate("download_chunk")
        return self._file_manager.e2e_encrypt_download_chunk(
            file_ref=fileRef,
            offset=offset,
            length=length,
            session_id=sessionId,
            chunk_index=chunkIndex,
        )

    def _e2e_close_session(self, sessionId: str, **kwargs) -> Dict:
        """关闭 E2E 会话，销毁密钥材料。"""
        return self._file_manager.e2e_close_session(sessionId)

    # ============== P2P 加密直传方法 ==============
    
    def _get_ticket_manager(self):
        """获取（或初始化）P2P 票据管理器"""
        if self._p2p_ticket_manager is None:
            from .p2p_data_tunnel import TicketManager
            self._p2p_ticket_manager = TicketManager()
        return self._p2p_ticket_manager
    
    def _p2p_tunnel_register_endpoint(
        self,
        ip: str,
        port: int,
        publicKey: str = "",
        **kwargs
    ) -> Dict:
        """矿工注册 P2P 数据端点

        矿工在启动 P2P 数据服务器后调用此接口，
        告知平台自己的数据传输 IP:Port 和加密公钥。
        IP 信息只在内存中暂存，生成票据时用用户公钥加密后丢弃。
        """
        if not self.miner_address:
            raise RPCError(RPCErrorCode.INVALID_REQUEST.value, "请先连接钱包")
        
        miner_id = f"miner_{self.node_id}"
        pubkey_bytes = bytes.fromhex(publicKey) if publicKey else b""
        
        tm = self._get_ticket_manager()
        tm.register_miner_direct(miner_id, ip, int(port), pubkey_bytes)
        
        return {
            "success": True,
            "minerId": miner_id,
            "p2pReady": True,
            "message": "P2P 数据端点已注册",
        }
    
    def _p2p_tunnel_request_ticket(
        self,
        taskId: str,
        userPublicKey: str = "",
        **kwargs
    ) -> Dict:
        """用户请求 P2P 连接票据

        返回加密后的矿工连接信息——用户用自己的私钥解密后得到矿工 IP:Port。
        服务器全程不保留 IP 明文。
        """
        if not self.miner_address:
            raise RPCError(RPCErrorCode.INVALID_REQUEST.value, "请先连接钱包")
        
        # 查找任务的分配矿工
        task = self.tasks.get(taskId)
        if not task:
            # 也在加密任务中查找
            try:
                manager, _ = self._get_encrypted_task_manager()
                task = manager.tasks.get(taskId)
            except Exception:
                pass
        
        if not task:
            raise RPCError(RPCErrorCode.INVALID_PARAMS.value, f"任务不存在: {taskId}")
        
        # 获取分配的矿工 ID
        miner_id = None
        if isinstance(task, dict):
            miner_id = task.get("assigned_miner_id") or task.get("minerId")
        else:
            miner_id = getattr(task, "assigned_miner_id", None) or getattr(task, "current_miner", None)
        
        if not miner_id:
            raise RPCError(RPCErrorCode.INVALID_REQUEST.value, "任务尚未分配矿工")
        
        user_id = self.miner_address or "user_default"
        user_pubkey = bytes.fromhex(userPublicKey) if userPublicKey else b""
        
        tm = self._get_ticket_manager()
        
        # 检查矿工是否支持 P2P
        if not tm.is_miner_p2p_ready(miner_id):
            return {
                "success": True,
                "transferMode": "relay",
                "message": "矿工未启用 P2P 直连，将使用服务器中转",
                "taskId": taskId,
            }
        
        relay_ep = f"127.0.0.1:8545"  # 回退中转地址
        ticket = tm.create_ticket(
            task_id=taskId,
            user_id=user_id,
            miner_id=miner_id,
            user_pubkey=user_pubkey,
            relay_endpoint=relay_ep,
        )
        
        if not ticket:
            raise RPCError(RPCErrorCode.INTERNAL_ERROR.value, "票据创建失败")
        
        return {
            "success": True,
            "transferMode": ticket.transfer_mode,
            "ticket": ticket.to_dict(),
        }
    
    def _p2p_tunnel_get_status(self, sessionId: str = "", taskId: str = "", **kwargs) -> Dict:
        """查询 P2P 传输状态"""
        tm = self._get_ticket_manager()
        
        # 按 sessionId 查询
        if sessionId:
            for ticket in tm._tickets.values():
                if ticket.session_id == sessionId:
                    return {
                        "sessionId": sessionId,
                        "taskId": ticket.task_id,
                        "transferMode": ticket.transfer_mode,
                        "expired": ticket.is_expired(),
                        "createdAt": ticket.created_at,
                        "expiresAt": ticket.expires_at,
                    }
        
        # 按 taskId 查询
        if taskId:
            for ticket in tm._tickets.values():
                if ticket.task_id == taskId:
                    return {
                        "sessionId": ticket.session_id,
                        "taskId": taskId,
                        "transferMode": ticket.transfer_mode,
                        "expired": ticket.is_expired(),
                        "createdAt": ticket.created_at,
                        "expiresAt": ticket.expires_at,
                    }
        
        return {"error": "session not found"}
    
    def _p2p_tunnel_start_server(self, host: str = "0.0.0.0", port: int = 0, **kwargs) -> Dict:
        """启动矿工侧 P2P 数据服务器"""
        if self._p2p_data_server is not None:
            return {
                "success": True,
                "port": self._p2p_data_server.actual_port,
                "publicKey": self._p2p_data_server.public_key.hex(),
                "message": "P2P 数据服务器已在运行",
            }
        
        from .p2p_data_tunnel import P2PDataServer
        self._p2p_data_server = P2PDataServer(
            host=host, port=int(port), data_dir="data/p2p_recv",
        )
        
        # 连接回调：文件接收完成时通知任务执行器
        self._p2p_data_server.on_transfer_complete = self._on_p2p_transfer_complete
        
        self._p2p_data_server.start()
        
        actual_port = self._p2p_data_server.actual_port
        pubkey = self._p2p_data_server.public_key
        
        # 自动注册端点
        miner_id = f"miner_{self.node_id}"
        tm = self._get_ticket_manager()
        # 使用 127.0.0.1 作为默认（实际部署时矿工应提供公网 IP）
        tm.register_miner_direct(miner_id, "127.0.0.1", actual_port, pubkey)
        
        return {
            "success": True,
            "port": actual_port,
            "publicKey": pubkey.hex(),
            "message": f"P2P 数据服务器已启动于端口 {actual_port}",
        }
    
    def _p2p_tunnel_get_miner_info(self, minerId: str = "", **kwargs) -> Dict:
        """查询矿工 P2P 可用状态（不暴露 IP）"""
        tm = self._get_ticket_manager()
        
        if not minerId:
            minerId = f"miner_{self.node_id}"
        
        is_ready = tm.is_miner_p2p_ready(minerId)
        pubkey = tm.get_miner_pubkey(minerId)
        
        return {
            "minerId": minerId,
            "p2pReady": is_ready,
            "publicKey": pubkey.hex() if pubkey else "",
            "message": "可以 P2P 直连" if is_ready else "需要服务器中转",
        }
    
    def _on_p2p_transfer_complete(self, task_id: str):
        """P2P 数据传输完成回调 — 标记任务数据就绪，触发执行
        
        由 P2PDataServer.on_transfer_complete 调用。
        当用户通过 P2P 隧道直接向矿工传输完数据后，
        需要通知调度器该任务的数据已到位，可以开始执行。
        """
        import logging
        logger = logging.getLogger(__name__)
        logger.info(f"P2P 传输完成回调: task={task_id}")
        
        # 更新任务元数据，标记 p2p_data_dir
        data_dir = os.path.join("data", "p2p_recv", task_id)
        
        # 尝试更新加密任务管理器中的任务
        try:
            manager, _ = self._get_encrypted_task_manager()
            task = manager.tasks.get(task_id)
            if task and hasattr(task, '_extra_meta'):
                task._extra_meta["p2p_data_dir"] = data_dir
                task._extra_meta["p2p_transfer_complete"] = True
                logger.info(f"任务 {task_id} P2P 数据就绪: {data_dir}")
        except Exception as e:
            logger.warning(f"P2P 回调更新任务失败: {e}")
        
        # 也更新调度器中的任务状态
        if self.compute_scheduler:
            try:
                task_obj = self.compute_scheduler.get_task(task_id)
                if task_obj:
                    task_obj.extra = task_obj.extra or {}
                    task_obj.extra["p2p_data_dir"] = data_dir
                    task_obj.extra["p2p_transfer_complete"] = True
                    self.compute_scheduler._save_task(task_obj)
            except Exception:
                pass
    
    # ============== 上传超时守护 & 国库补偿 ==============
    
    # 上传阶段最大等待时间（秒）：任务创建后若超过此时间仍未 submit，则自动取消
    UPLOAD_TIMEOUT_SECONDS = 2 * 3600      # 2 小时
    # 国库补偿：矿工每接收 1GB 数据的补偿（MAIN 币）
    UPLOAD_COMPENSATION_PER_GB = 0.5
    # 守护扫描间隔（秒）
    UPLOAD_WATCHDOG_INTERVAL = 60
    
    def _start_upload_watchdog(self):
        """启动上传超时守护线程（仅启动一次）"""
        if getattr(self, '_upload_watchdog_running', False):
            return
        self._upload_watchdog_running = True
        t = threading.Thread(
            target=self._upload_watchdog_loop, daemon=True,
            name="upload-timeout-watchdog",
        )
        t.start()
    
    def _upload_watchdog_loop(self):
        """后台守护：扫描长时间未提交的加密任务，自动取消并补偿矿工"""
        import logging
        logger = logging.getLogger(__name__)
        while self._upload_watchdog_running:
            try:
                self._handle_upload_timeouts()
            except Exception as e:
                logger.warning(f"上传超时守护异常: {e}")
            for _ in range(self.UPLOAD_WATCHDOG_INTERVAL):
                if not self._upload_watchdog_running:
                    return
                time.sleep(1)
    
    def _handle_upload_timeouts(self):
        """扫描并处理上传超时的任务"""
        import logging
        from .encrypted_task import TaskChainStatus
        logger = logging.getLogger(__name__)
        now = time.time()
        
        try:
            manager, contract = self._get_encrypted_task_manager()
        except Exception:
            return
        
        expired_tasks = []
        for task_id, task in list(manager.tasks.items()):
            # 只处理 CREATED 状态（已创建但未提交）
            if task.chain_status != TaskChainStatus.CREATED:
                continue
            # 检查是否超时
            age = now - task.created_at
            if age < self.UPLOAD_TIMEOUT_SECONDS:
                continue
            expired_tasks.append(task)
        
        for task in expired_tasks:
            logger.info(f"上传超时自动取消: task={task.task_id}, age={now - task.created_at:.0f}s")
            self._cancel_upload_timeout_task(task, contract, logger)
    
    def _cancel_upload_timeout_task(self, task, contract, logger):
        """取消上传超时任务：退还预算 + 国库补偿矿工带宽"""
        from .encrypted_task import TaskChainStatus
        
        task_id = task.task_id
        user_id = task.user_id
        
        # 1. 释放锁定预算，全额退还用户
        refunded = contract.release_budget(task_id, user_id)
        
        # 2. 检查矿工是否已经接收过数据（P2P 传输部分完成）
        data_dir = os.path.join("data", "p2p_recv", task_id)
        bytes_received = 0
        receiving_miner = ""
        
        if os.path.isdir(data_dir):
            for f in os.listdir(data_dir):
                fpath = os.path.join(data_dir, f)
                if os.path.isfile(fpath):
                    bytes_received += os.path.getsize(fpath)
        
        # 从任务的 receivers 获取矿工 ID
        if task.chain_nodes:
            receiving_miner = task.chain_nodes[0].miner_id
        elif hasattr(task, '_extra_meta') and task._extra_meta:
            receiving_miner = task._extra_meta.get("assigned_miner", "")
        
        # 3. 国库补偿矿工带宽
        compensation = 0.0
        if bytes_received > 0 and receiving_miner:
            gb_received = bytes_received / (1024 ** 3)
            compensation = max(0.01, gb_received * self.UPLOAD_COMPENSATION_PER_GB)
            
            try:
                dao_system = self._get_dao_system()
                governance = dao_system.get("governance")
                treasury_mgr = governance.treasury if governance else None
                if treasury_mgr:
                    result = treasury_mgr.auto_compensate(
                        recipient=receiving_miner,
                        amount=compensation,
                        reason=f"upload_timeout_bandwidth",
                        task_id=task_id,
                    )
                    if "error" not in result and not result.get("deferred"):
                        # 国库扣款成功，给矿工的合约账户打款
                        contract.balances[receiving_miner] = (
                            contract.balances.get(receiving_miner, 0) + compensation
                        )
                        logger.info(
                            f"国库补偿矿工 {receiving_miner}: {compensation:.4f} MAIN "
                            f"(数据 {bytes_received / (1024*1024):.1f} MB)"
                        )
                    elif result.get("deferred"):
                        # 国库余额不足，已记录欠条，后续入账时自动补发
                        logger.info(
                            f"国库余额不足，矿工 {receiving_miner} 补偿 {compensation:.4f} MAIN "
                            f"已记入欠条 {result.get('debt_id')}"
                        )
                    else:
                        logger.warning(f"国库补偿被拒绝: {result['error']}")
                        compensation = 0.0
            except Exception as e:
                logger.warning(f"国库补偿失败: {e}")
                compensation = 0.0
        
        # 4. 标记任务取消
        task.chain_status = TaskChainStatus.CANCELLED
        task.completed_at = time.time()
        if not hasattr(task, '_extra_meta') or not task._extra_meta:
            task._extra_meta = {}
        task._extra_meta["cancel_reason"] = "upload_timeout"
        task._extra_meta["refunded"] = refunded
        task._extra_meta["miner_compensation"] = compensation
        task._extra_meta["bytes_received_by_miner"] = bytes_received
        
        logger.info(
            f"任务 {task_id} 上传超时取消完成: "
            f"退还用户 {refunded:.4f} MAIN, 矿工补偿 {compensation:.4f} MAIN"
        )
    
    # ============== 动态定价方法实(Phase 8) ==============
    
    def _get_pricing_system(self):
        """获取或创建定价系"""
        if not hasattr(self, '_pricing_system'):
            from .dynamic_pricing import get_pricing_system
            self._pricing_system = get_pricing_system()
        return self._pricing_system
    
    def _pricing_get_base_rates(self, **kwargs) -> Dict:
        """获取所有 GPU 基础价格"""
        system = self._get_pricing_system()
        return {
            "prices": system["pricing_engine"].base_price_manager.get_all_prices(),
            "timestamp": time.time(),
        }
    
    def _pricing_get_real_time_price(self, gpuType: str = "RTX3080", **kwargs) -> Dict:
        """获取实时价格"""
        system = self._get_pricing_system()
        return system["pricing_engine"].get_real_time_price(gpuType)
    
    def _pricing_calculate_price(
        self,
        gpuType: str = "RTX3080",
        estimatedHours: float = 1.0,
        strategy: str = "standard",
        **kwargs
    ) -> Dict:
        """计算任务价格"""
        from .dynamic_pricing import PricingStrategy
        
        system = self._get_pricing_system()
        
        # 解析策略
        try:
            pricing_strategy = PricingStrategy(strategy)
        except ValueError:
            pricing_strategy = PricingStrategy.STANDARD
        
        result = system["pricing_engine"].calculate_price(
            gpuType, estimatedHours, pricing_strategy
        )
        
        worst_case = system["pricing_engine"].calculate_worst_case_price(
            gpuType, estimatedHours, pricing_strategy
        )
        
        return {
            "basePrice": result.base_price,
            "marketMultiplier": result.market_multiplier,
            "timeSlotMultiplier": result.time_slot_multiplier,
            "strategyMultiplier": result.strategy_multiplier,
            "finalUnitPrice": result.final_unit_price,
            "estimatedTotal": result.estimated_total,
            "worstCasePrice": worst_case,
            "priceBreakdown": result.price_breakdown,
            "validUntil": result.valid_until,
        }
    
    def _pricing_get_market_state(self, **kwargs) -> Dict:
        """获取市场供需状"""
        system = self._get_pricing_system()
        return system["pricing_engine"].market_calculator.get_market_state()
    
    def _pricing_get_strategies(self, **kwargs) -> List[Dict]:
        """获取所有定价策"""
        from .dynamic_pricing import StrategyCalculator
        return StrategyCalculator.get_all_strategies()
    
    def _pricing_get_time_slot_schedule(self, **kwargs) -> Dict:
        """获取时段价格"""
        from .dynamic_pricing import TimeSlotCalculator
        return {
            "schedule": TimeSlotCalculator.get_schedule(),
            "currentSlot": TimeSlotCalculator.get_current_time_slot().value,
            "currentMultiplier": TimeSlotCalculator.get_multiplier(),
        }
    
    def _pricing_get_price_forecast(
        self,
        gpuType: str = "RTX3080",
        hoursAhead: int = 24,
        **kwargs
    ) -> Dict:
        """获取价格预测"""
        system = self._get_pricing_system()
        forecasts = system["market_monitor"].get_price_forecast(gpuType, hoursAhead)
        return {
            "gpuType": gpuType,
            "forecasts": forecasts,
            "timestamp": time.time(),
        }
    
    # ============== 预算管理方法实现 ==============
    
    def _budget_deposit(self, userId: str = None, amount: float = 0, **kwargs) -> Dict:
        """用户充值（只能为自己充值，除非管理员）"""
        auth_context = kwargs.get("auth_context", {})
        caller = self._get_auth_user(auth_context, self.miner_address or "default_user")
        
        # Security: non-admin users can only deposit for themselves
        target_user = userId or caller
        if target_user != caller and not auth_context.get("is_admin", False):
            raise RPCError(-32403, "Permission denied: can only deposit for yourself")
        
        if amount <= 0:
            raise RPCError(RPCErrorCode.INVALID_PARAMS.value, "Deposit amount must be positive")
        
        system = self._get_pricing_system()
        new_balance = system["budget_manager"].deposit(target_user, amount)
        
        return {
            "userId": target_user,
            "deposited": amount,
            "newBalance": new_balance,
            "timestamp": time.time(),
        }
    
    def _budget_get_balance(self, userId: str = None, **kwargs) -> Dict:
        """获取用户余额"""
        system = self._get_pricing_system()
        user_id = userId or self.miner_address or "default_user"
        
        balance = system["budget_manager"].get_balance(user_id)
        
        return {
            "userId": user_id,
            "balance": balance,
            "timestamp": time.time(),
        }
    
    def _budget_lock_for_task(
        self,
        taskId: str,
        userId: str = None,
        gpuType: str = "RTX3080",
        estimatedHours: float = 1.0,
        strategy: str = "standard",
        **kwargs
    ) -> Dict:
        """为任务锁定预"""
        from .dynamic_pricing import PricingStrategy
        
        system = self._get_pricing_system()
        user_id = userId or self.miner_address or "default_user"
        
        try:
            pricing_strategy = PricingStrategy(strategy)
        except ValueError:
            pricing_strategy = PricingStrategy.STANDARD
        
        lock = system["budget_manager"].lock_budget(
            taskId, user_id, gpuType, estimatedHours, pricing_strategy
        )
        
        if lock:
            return {
                "lockId": lock.lock_id,
                "taskId": lock.task_id,
                "userId": lock.user_id,
                "lockedAmount": lock.locked_amount,
                "worstCasePrice": lock.worst_case_price,
                "status": lock.status,
                "lockedAt": lock.locked_at,
                "expiresAt": lock.expires_at,
                "success": True,
            }
        else:
            return {
                "success": False,
                "error": "Insufficient balance",
                "requiredBalance": system["pricing_engine"].calculate_worst_case_price(
                    gpuType, estimatedHours, pricing_strategy
                ),
                "currentBalance": system["budget_manager"].get_balance(user_id),
            }
    
    def _budget_get_lock_info(self, taskId: str, **kwargs) -> Dict:
        """获取预算锁定信息"""
        system = self._get_pricing_system()
        info = system["budget_manager"].get_lock_info(taskId)
        
        if info:
            return info
        else:
            return {"error": "Lock not found", "taskId": taskId}
    
    # ============== 结算方法实现 ==============
    
    def _settlement_settle_task(
        self,
        taskId: str,
        userId: str = None,
        minerId: str = None,
        **kwargs
    ) -> Dict:
        """结算任务"""
        system = self._get_pricing_system()
        user_id = userId or "default_user"
        miner_id = minerId or self.miner_address or "default_miner"
        
        record = system["settlement_engine"].settle_task(taskId, user_id, miner_id)
        
        if record:
            return {
                "settlementId": record.settlement_id,
                "taskId": record.task_id,
                "userId": record.user_id,
                "minerId": record.miner_id,
                "lockedBudget": record.locked_budget,
                "actualCost": record.actual_cost,
                "refundAmount": record.refund_amount,
                "settledAt": record.settled_at,
                "settlementHash": record.settlement_hash,
                "success": True,
            }
        else:
            return {"success": False, "error": "Settlement failed", "taskId": taskId}
    
    def _settlement_get_record(self, taskId: str, **kwargs) -> Dict:
        """获取结算记录"""
        system = self._get_pricing_system()
        record = system["settlement_engine"].get_settlement_record(taskId)
        
        if record:
            return record
        else:
            return {"error": "Record not found", "taskId": taskId}
    
    def _settlement_get_detailed_bill(self, taskId: str, **kwargs) -> Dict:
        """获取详细账单"""
        system = self._get_pricing_system()
        return system["settlement_engine"].get_detailed_bill(taskId)
    
    def _settlement_get_miner_earnings(self, minerId: str = None, **kwargs) -> Dict:
        """获取矿工收益"""
        system = self._get_pricing_system()
        miner_id = minerId or self.miner_address or "default_miner"
        
        earnings = system["settlement_engine"].get_miner_earnings(miner_id)
        
        return {
            "minerId": miner_id,
            "totalEarnings": earnings,
            "timestamp": time.time(),
        }
    
    # ============== 市场监控方法实现 ==============
    
    def _market_get_quotes(self, task_id: str = None, **kwargs) -> Dict:
        """获取任务报价列表"""
        if not task_id or task_id not in self.tasks:
            return {"quotes": []}

        task = self.tasks[task_id]
        quotes = task.get("_quotes", [])

        # 无报价时返回空列表（不生成模拟报价）
        # 真实报价由矿工通过 market_submitQuote 提交

        # 返回时过滤内部字段
        return {"quotes": quotes}

    def _market_accept_quote(self, quote_id: str = None, **kwargs) -> Dict:
        """接受报价"""
        if not quote_id:
            raise RPCError(RPCErrorCode.INVALID_PARAMS.value, "缺少 quote_id")

        # 在所有任务中搜索匹配的报价
        for task_id, task in self.tasks.items():
            quotes = task.get("_quotes", [])
            for q in quotes:
                if q["quoteId"] == quote_id:
                    if q["status"] != "open":
                        raise RPCError(
                            RPCErrorCode.INVALID_PARAMS.value,
                            f"报价状态不允许接受: {q['status']}"
                        )
                    # 接受此报价，拒绝其他
                    q["status"] = "accepted"
                    for other in quotes:
                        if other["quoteId"] != quote_id and other["status"] == "open":
                            other["status"] = "rejected"
                    # 更新任务状态
                    task["status"] = "assigned"
                    task["minerId"] = q["minerId"]
                    task["acceptedPrice"] = q["price"]
                    return {"status": "accepted", "quoteId": quote_id, "taskId": task_id}

        raise RPCError(RPCErrorCode.INVALID_PARAMS.value, f"报价不存在: {quote_id}")

    def _market_get_dashboard(self, **kwargs) -> Dict:
        """获取市场监控面板"""
        system = self._get_pricing_system()
        return system["market_monitor"].get_dashboard_data()
    
    def _market_get_supply_demand_curve(self, hours: int = 24, **kwargs) -> Dict:
        """获取供需曲线"""
        system = self._get_pricing_system()
        curve = system["market_monitor"].get_supply_demand_curve(hours)
        
        return {
            "hours": hours,
            "dataPoints": curve,
            "timestamp": time.time(),
        }
    
    def _market_get_queue_status(self, **kwargs) -> Dict:
        """获取任务队列状"""
        system = self._get_pricing_system()
        return system["market_monitor"].get_queue_status()
    
    def _market_update_supply_demand(
        self,
        totalSupply: float = 100,
        totalDemand: float = 80,
        activeMiners: int = 10,
        pendingTasks: int = 5,
        **kwargs
    ) -> Dict:
        """更新供需数据"""
        system = self._get_pricing_system()
        system["pricing_engine"].market_calculator.update_market_data(
            totalSupply, totalDemand, activeMiners, pendingTasks
        )
        
        return {
            "updated": True,
            "newMarketState": system["pricing_engine"].market_calculator.get_market_state(),
        }
    
    # ============== 任务队列方法实现 ==============
    
    def _queue_enqueue(
        self,
        taskId: str,
        priority: int = 3,
        gpuType: str = "RTX3080",
        estimatedHours: float = 1.0,
        **kwargs
    ) -> Dict:
        """任务入队"""
        from .dynamic_pricing import TaskPriority
        
        system = self._get_pricing_system()
        
        try:
            task_priority = TaskPriority(priority)
        except ValueError:
            task_priority = TaskPriority.NORMAL
        
        position = system["task_queue"].enqueue(
            taskId,
            task_priority,
            {
                "gpuType": gpuType,
                "estimatedHours": estimatedHours,
            }
        )
        
        return {
            "taskId": taskId,
            "position": position,
            "priority": priority,
            "enqueuedAt": time.time(),
        }
    
    def _queue_get_position(self, taskId: str, **kwargs) -> Dict:
        """获取队列位置"""
        system = self._get_pricing_system()
        position = system["task_queue"].get_position(taskId)
        
        return {
            "taskId": taskId,
            "position": position,
            "found": position is not None,
        }
    
    def _queue_get_estimated_wait_time(self, taskId: str, **kwargs) -> Dict:
        """获取预估等待时间"""
        system = self._get_pricing_system()
        wait_time = system["task_queue"].get_estimated_wait_time(taskId)
        
        return {
            "taskId": taskId,
            "estimatedWaitSeconds": wait_time,
            "estimatedWaitMinutes": round(wait_time / 60, 1),
        }
    
    def _queue_get_stats(self, **kwargs) -> Dict:
        """获取队列统计"""
        system = self._get_pricing_system()
        return system["task_queue"].get_stats()
    
    # ============== 元信==============
    
    def _rpc_list_methods(self, params: Dict) -> List[Dict]:
        """列出方法（仅返回调用者有权限的方法）"""
        return self.registry.list_public_methods()
    
    # ============== Dashboard 方法实现 ==============
    
    def _dashboard_get_stats(self, address: str = None, **kwargs) -> Dict:
        """获取仪表盘统计信- 读取真实数据"""
        target_address = address or self.miner_address

        # 1. 获取UTXO余额（挖矿奖励来源）
        utxo_balance = 0.0
        pending_balance = 0.0
        if hasattr(self, 'utxo_store') and self.utxo_store and target_address:
            try:
                utxo_balance = self.utxo_store.get_balance(target_address)
                # 计算待成熟余额（coinbase未满100确认的部分）
                total_utxo = self.utxo_store.get_total_balance(target_address)
                pending_balance = total_utxo - utxo_balance
            except Exception:
                pass

        # 2. 余额口径与钱包保持一致（复用 account_get_balance）
        main_balance = 0.0
        sector_total = 0.0
        sector_balances = {}
        try:
            bal = self._account_get_balance(address=target_address)
            main_balance = float(bal.get("mainBalance", 0.0) or 0.0)
            sector_total = float(bal.get("sectorTotal", 0.0) or 0.0)
            sector_balances = bal.get("sectorBalances", {}) or {}
        except Exception:
            pass
        
        # 获取真实区块高度
        block_height = 0
        if self.consensus_engine:
            block_height = getattr(self.consensus_engine, '_chain_height', len(self.consensus_engine.chain) - 1)
        elif self.current_height:
            block_height = self.current_height
        
        # 获取统计
        total_blocks_mined = 0
        if self.consensus_engine:
            total_blocks_mined = self.consensus_engine.total_blocks_mined
        
        return {
            "balance": round(utxo_balance + main_balance, 4),  # 可用余额
            "pendingBalance": round(pending_balance, 4),  # 待成熟余额（coinbase需100确认）
            "utxoBalance": round(utxo_balance, 4),  # UTXO可用余额
            "mainBalance": round(main_balance, 4),  # 兑换MAIN币余额
            "sectorTotal": round(sector_total, 4),  # 板块币总和
            "balanceChange": 0.0,  # 暂不计算变化
            "sectorBalances": sector_balances,
            "activeTasks": 0,  # 暂无任务系统
            "completedToday": total_blocks_mined,
            "onlineMiners": self._get_peer_count() + 1,  # 包括自己
            "totalGpuPower": 0,  # 暂无
            "networkUtilization": 0,
            "blockHeight": block_height,
            "totalBlocksMined": total_blocks_mined,
            "minerAddress": target_address or "",
        }
    
    def _dashboard_get_recent_tasks(self, limit: int = 5, **kwargs) -> List[Dict]:
        """获取最近任务 - 从真实数据获取"""
        limit = min(max(1, limit), 50)  # Cap pagination
        # 从共识引擎获取最近区块作为任务
        tasks = []
        
        if self.consensus_engine and len(self.consensus_engine.chain) > 1:
            recent_blocks = self.consensus_engine.chain[-min(limit+1, len(self.consensus_engine.chain)):]
            for i, block in enumerate(reversed(recent_blocks[1:])):  # 跳过创世区块
                task_status = "completed"
                progress = 100
                tasks.append({
                    "id": f"block_{block.height}",
                    "title": f"区块 #{block.height} 挖矿",
                    "status": task_status,
                    "progress": progress,
                    "gpu": self.consensus_engine.sector if hasattr(self.consensus_engine, 'sector') else "GPU",
                    "reward": round(block.block_reward, 4),
                    "timestamp": block.timestamp,
                })
                if len(tasks) >= limit:
                    break
        
        # 如果没有真实数据，返回空列表
        return tasks[:limit]
    
    def _dashboard_get_recent_proposals(self, limit: int = 5, **kwargs) -> List[Dict]:
        """获取最近提- 从真实数据获"""
        proposals = list(self.proposals.values())
        # 按创建时间排
        proposals.sort(key=lambda x: x.get("createdAt", ""), reverse=True)
        
        # 返回简化版
        result = []
        for p in proposals[:limit]:
            result.append({
                "id": p["proposalId"],
                "title": p["title"],
                "status": p["status"],
                "votesFor": p["votesFor"],
                "votesAgainst": p["votesAgainst"],
            })
        return result
    
    def _dashboard_get_block_chart(self, **kwargs) -> Dict:
        """获取出块类型分布图表数据"""
        pow_blocks = 0
        pouw_blocks = 0
        validation_blocks = 0
        
        if self.consensus_engine:
            from .consensus import ConsensusType
            for block in self.consensus_engine.chain:
                if block.consensus_type == ConsensusType.POW:
                    pow_blocks += 1
                else:
                    pouw_blocks += 1
        
        return {
            "data": [
                {"name": "任务区块", "value": pouw_blocks, "color": "#6c5ce7"},
                {"name": "空闲区块", "value": pow_blocks, "color": "#0984e3"},
                {"name": "验证区块", "value": validation_blocks, "color": "#a29bfe"},
            ],
            "total": pow_blocks + pouw_blocks + validation_blocks,
        }
    
    def _dashboard_get_reward_trend(self, **kwargs) -> Dict:
        """获取奖励趋势图表数据"""
        trend_data = []
        
        if self.consensus_engine:
            # 按时间分组统计奖
            blocks = list(self.consensus_engine.chain[1:])  # 跳过创世
            
            # 简单按块号分组
            step = max(len(blocks) // 7, 1)
            for i in range(0, len(blocks), step):
                chunk = blocks[i:i+step]
                total_reward = sum(b.block_reward for b in chunk)
                trend_data.append({
                    "time": f"#{chunk[0].height}" if chunk else f"#{i}",
                    "rewards": round(total_reward, 4),
                })
        
        # 无数据时返回空数组（而不是假数据
        return {"data": trend_data}
    
    # ============== 矿工方法实现 ==============
    
    def _miner_get_list(self, sort_by: str = "rating", limit: int = 20, **kwargs) -> Dict:
        """获取矿工列表 - 包含真实数据"""
        limit = min(max(1, limit), 100)  # Cap pagination
        miners = []
        
        # 添加当前节点自己
        if self.consensus_engine and self.miner_address:
            total_blocks = self.consensus_engine.total_blocks_mined
            total_earnings = 0.0
            if self.sector_ledger:
                from .sector_coin import SectorCoinType
                all_balances = self.sector_ledger.get_all_balances(self.miner_address)
                for coin_type, bal in all_balances.items():
                    total_earnings += bal.balance
            
            # 获取真实GPU名称
            real_gpu_name = getattr(self, 'detected_gpu_name', None)
            if not real_gpu_name:
                # 尝试设备检
                try:
                    from core.device_detector import get_device_detector
                    detector = get_device_detector()
                    device_profile = detector.detect_all()
                    if device_profile.gpu_list:
                        real_gpu_name = device_profile.gpu_list[0].name
                    else:
                        real_gpu_name = "CPU"
                except Exception:
                    real_gpu_name = "通用GPU"
            
            miners.append({
                "minerId": self.node_id,
                "name": f"本地节点 ({self.node_id})",
                "address": self.miner_address[:16] + "..." if self.miner_address else "",
                "status": "online",
                "gpuType": real_gpu_name,
                "gpuCount": 1,
                "behaviorScore": 100,
                "acceptanceRate": 100.0,
                "totalTasks": total_blocks,
                "completedTasks": total_blocks,
                "totalEarnings": round(total_earnings, 4),
                "reputationLevel": "platinum" if total_blocks > 100 else "gold" if total_blocks > 50 else "silver" if total_blocks > 10 else "bronze",
                "schedulingMultiplier": 1.0 + (total_blocks * 0.001),
                "isLocal": True,
            })
        
        # 从注册表获取其他矿工（真实P2P网络中注册的矿工
        if hasattr(self, 'registered_miners') and self.registered_miners:
            for key, miner_data in self.registered_miners.items():
                # 跳过自己（通过地址或minerId判断
                miner_addr = miner_data.get("address", "")
                if key == self.node_id or miner_addr == self.miner_address:
                    continue
                miner_id = miner_data.get("minerId", key)
                miners.append({
                    "minerId": miner_id,
                    "name": miner_data.get("name", f"矿工 {str(miner_id)[:8]}"),
                    "address": miner_addr[:16] + "..." if len(miner_addr) > 16 else miner_addr,
                    "status": miner_data.get("status", "offline"),
                    "gpuType": miner_data.get("gpuType", "GPU"),
                    "gpuCount": miner_data.get("gpuCount", 1),
                    "behaviorScore": miner_data.get("behaviorScore", 50),
                    "acceptanceRate": miner_data.get("acceptanceRate", 0),
                    "totalTasks": miner_data.get("totalTasks", 0),
                    "completedTasks": miner_data.get("completedTasks", 0),
                    "totalEarnings": miner_data.get("totalEarnings", 0),
                    "reputationLevel": miner_data.get("reputationLevel", "bronze"),
                    "schedulingMultiplier": miner_data.get("schedulingMultiplier", 1.0),
                    "isLocal": False,
                })
        
        # 排序
        if sort_by == "earnings":
            miners.sort(key=lambda x: x["totalEarnings"], reverse=True)
        elif sort_by == "rating":
            miners.sort(key=lambda x: x["behaviorScore"], reverse=True)
        
        return {"miners": miners[:limit], "total": len(miners)}
    
    def _miner_get_behavior_report(self, miner_id: str = None, **kwargs) -> Dict:
        """获取矿工行为报告"""
        if not miner_id:
            raise RPCError(RPCErrorCode.INVALID_PARAMS.value, "缺少 miner_id")

        try:
            from core.miner_behavior import get_behavior_analyzer
            analyzer = get_behavior_analyzer()
            report = analyzer.get_miner_report(miner_id)
            return report  # {score: {...}, suggestions: [...], note: str}
        except Exception:
            # 模块不可用时返回默认报告
            return {
                "score": {
                    "miner_id": miner_id,
                    "final_score": 0.5,
                    "acceptance_rate": 0.0,
                    "total_orders": 0,
                    "scheduling_multiplier": 1.0,
                },
                "suggestions": ["暂无足够数据生成行为建议"],
                "note": "矿工有完全的定价自由，此评分仅影响拥堵时的调度优先级",
            }

    def _miner_get_info(self, miner_id: str = None, **kwargs) -> Optional[Dict]:
        """获取矿工详情"""
        if not miner_id:
            return None
        
        # 检查是否是本地节点
        if miner_id == self.node_id:
            total_blocks = self.consensus_engine.total_blocks_mined if self.consensus_engine else 0
            return {
                "minerId": miner_id,
                "name": f"本地节点 ({miner_id})",
                "address": self.miner_address[:16] + "..." if self.miner_address else "",
                "status": "online",
                "gpuType": self.consensus_engine.sector if hasattr(self.consensus_engine, 'sector') else "GPU",
                "gpuCount": 1,
                "behaviorScore": 100,
                "totalTasks": total_blocks,
                "completedTasks": total_blocks,
                "totalEarnings": 0,
                "isLocal": True,
            }
        
        # 从注册表获取矿工信息
        if hasattr(self, 'registered_miners') and miner_id in self.registered_miners:
            miner_data = self.registered_miners[miner_id]
            return {
                "minerId": miner_id,
                "name": miner_data.get("name", f"矿工 {miner_id[:8]}"),
                "address": miner_data.get("address", "0x..."),
                "status": miner_data.get("status", "offline"),
                "gpuType": miner_data.get("gpuType", "GPU"),
                "gpuCount": miner_data.get("gpuCount", 1),
                "behaviorScore": miner_data.get("behaviorScore", 50),
                "totalTasks": miner_data.get("totalTasks", 0),
                "completedTasks": miner_data.get("completedTasks", 0),
                "totalEarnings": miner_data.get("totalEarnings", 0),
                "isLocal": False,
            }
        
        return None
    
    # ============== 统计方法实现 ==============
    
    def _stats_get_network(self, **kwargs) -> Dict:
        """获取网络统计 - 读取真实数据"""
        block_height = 0
        avg_block_time = 0
        difficulty = 4
        total_blocks_mined = 0
        
        if self.consensus_engine:
            block_height = len(self.consensus_engine.chain) - 1
            difficulty = self.consensus_engine.current_difficulty
            total_blocks_mined = self.consensus_engine.total_blocks_mined
            
            # 计算平均出块时间
            stats = self.consensus_engine.difficulty_adjuster.get_stats()
            avg_block_time = stats.get('avg_time', 30.0)
        
        # 计算真实矿工数量
        miner_count = 1  # 至少有本地节
        online_miners = 1
        if hasattr(self, 'registered_miners'):
            miner_count += len(self.registered_miners)
            online_miners += len([m for m in self.registered_miners.values() if m.get('status') == 'online'])
        
        # 计算真实任务数量
        total_tasks = len(self.tasks) if self.tasks else 0
        completed_tasks = len([t for t in self.tasks.values() if t.get('status') == 'completed']) if self.tasks else 0
        success_rate = (completed_tasks / total_tasks * 100) if total_tasks > 0 else 0
        
        return {
            "totalBlocks": block_height,
            "blockHeight": block_height,
            "avgBlockTime": round(avg_block_time, 2),
            "activeMiners": online_miners,
            "totalMiners": miner_count,
            "onlineMiners": online_miners,
            "difficulty": difficulty,
            "totalTasks": total_tasks,
            "successRate": round(success_rate, 1),
            "avgTaskDuration": 0,
            "totalGpuPower": 0,
            "networkUtilization": 0,
            "totalBlocksMined": total_blocks_mined,
        }
    
    def _stats_get_blocks(self, period: str = "7d", **kwargs) -> Dict:
        """获取区块统计 - 读取真实数据"""
        # 从共识引擎获取区块数
        pow_blocks = 0
        pouw_blocks = 0
        total_rewards = 0.0
        daily_data = []
        
        if self.consensus_engine:
            from .consensus import ConsensusType
            
            for block in self.consensus_engine.chain:
                if block.consensus_type == ConsensusType.POW:
                    pow_blocks += 1
                else:
                    pouw_blocks += 1
                total_rewards += block.block_reward
        
        return {
            "taskBlocks": pouw_blocks,
            "idleBlocks": pow_blocks,
            "validationBlocks": 0,
            "totalRewards": round(total_rewards, 4),
            "rewardsByType": {
                "POW": round(total_rewards * pow_blocks / max(pow_blocks + pouw_blocks, 1), 4),
                "POUW": round(total_rewards * pouw_blocks / max(pow_blocks + pouw_blocks, 1), 4),
            },
            "dailyData": daily_data or [
                {"date": "今日", "taskBlocks": pouw_blocks, "idleBlocks": pow_blocks, "validationBlocks": 0},
            ]
        }
    
    def _stats_get_tasks(self, period: str = "7d", **kwargs) -> Dict:
        """获取任务统计 - 从真实存储读"""
        tasks = list(self.tasks.values())
        
        total = len(tasks)
        completed = len([t for t in tasks if t.get("status") == "completed"])
        disputed = len([t for t in tasks if t.get("status") == "disputed"])
        
        # 按类型分
        type_counts = {}
        for t in tasks:
            tt = t.get("taskType", "other")
            type_counts[tt] = type_counts.get(tt, 0) + 1
        
        # 计算平均价格
        prices = [t.get("price", 0) for t in tasks if t.get("price")]
        avg_price = sum(prices) / len(prices) if prices else 0
        
        # 类型分布
        distribution = []
        type_names = {
            "ai_training": "AI 训练",
            "ai_inference": "推理服务",
            "rendering": "视频渲染",
            "scientific": "科学计算",
            "other": "其他",
        }
        for tt, count in type_counts.items():
            distribution.append({
                "type": type_names.get(tt, tt),
                "count": count,
                "percentage": round(count * 100 / total, 1) if total > 0 else 0,
            })
        
        return {
            "totalTasks": total,
            "completedTasks": completed,
            "disputedTasks": disputed,
            "averagePrice": round(avg_price, 2),
            "tasksByType": type_counts,
            "distribution": distribution,
        }
    
    # ============== 任务方法实现 ==============

    def _ensure_task_output_access(self, task_id: str, auth_context: Dict) -> Dict:
        """校验任务输出访问权限：仅任务创建者或管理员可查看。"""
        task = self.tasks.get(task_id)
        if not task:
            raise RPCError(RPCErrorCode.INVALID_PARAMS.value, f"任务不存在: {task_id}")

        caller = self._get_auth_user(auth_context, self.miner_address or "anonymous")
        owner = task.get("buyerId") or task.get("owner") or task.get("creatorId")
        is_admin = bool(auth_context.get("is_admin", False))

        if owner and caller != owner and not is_admin:
            raise RPCError(-32403, "Permission denied: only task owner can access outputs")

        return task

    @staticmethod
    def _format_size_label(size_bytes: int) -> str:
        if size_bytes < 1024:
            return f"{size_bytes} B"
        if size_bytes < 1024 ** 2:
            return f"{size_bytes / 1024:.1f} KB"
        if size_bytes < 1024 ** 3:
            return f"{size_bytes / (1024 ** 2):.1f} MB"
        return f"{size_bytes / (1024 ** 3):.1f} GB"
    
    def _task_get_list(self, status: str = None, task_type: str = None, limit: int = 20, **kwargs) -> Dict:
        """获取任务列表 - 从真实存储读取"""
        limit = min(max(1, limit), 100)  # Cap pagination
        tasks = list(self.tasks.values())
        
        # 过滤
        if status:
            tasks = [t for t in tasks if t.get("status") == status]
        if task_type:
            tasks = [t for t in tasks if t.get("taskType") == task_type]
        
        # 按创建时间排
        tasks.sort(key=lambda x: x.get("createdAt", ""), reverse=True)
        
        return {"tasks": tasks[:limit], "total": len(tasks)}
    
    def _task_get_info(self, task_id: str = None, **kwargs) -> Optional[Dict]:
        """获取任务详情 - 从真实存储读"""
        if not task_id:
            return None
        
        return self.tasks.get(task_id)
    
    def _task_create(self, title: str, description: str = "", task_type: str = "other",
                     priority: str = "normal", gpu_type: str = "RTX4090", gpu_count: int = 1,
                     estimated_hours: int = 1, max_price: float = 10.0,
                     requirements: str = "", **kwargs) -> Dict:
        """创建新任务，同时提交到 ComputeScheduler 进行调度

        Args:
            requirements: requirements.txt 内容，用于在沙箱中预装 Python 依赖
        """
        import datetime
        task_id = f"task_{uuid.uuid4().hex[:8]}"
        
        auth_context = kwargs.get("auth_context", {})
        buyer_id = self._get_auth_user(auth_context, self.miner_address or "buyer_local")

        task = {
            "taskId": task_id,
            "title": title,
            "description": description,
            "taskType": task_type,
            "status": "pending",
            "priority": priority,
            "price": max_price,
            "coin": f"{gpu_type}_COIN",
            "gpuType": gpu_type,
            "gpuCount": gpu_count,
            "estimatedHours": estimated_hours,
            "progress": 0,
            "createdAt": datetime.datetime.now().isoformat() + "Z",
            "buyerId": buyer_id,
        }

        # 保存 requirements（矿工执行时会在构建阶段安装依赖）
        if requirements and requirements.strip():
            task["requirements"] = requirements.strip()
        
        self.tasks[task_id] = task
        
        # ── 提交ComputeScheduler 进行真正的矿工调──
        if self.compute_scheduler:
            try:
                from core.compute_scheduler import ComputeTask
                compute_task = ComputeTask(
                    task_id=task_id,
                    order_id=task_id,
                    buyer_address=buyer_id,
                    task_type=task_type,
                    task_data=json.dumps({"title": title, "description": description,
                                          "gpu_type": gpu_type, "gpu_count": gpu_count}),
                    sector=gpu_type.split("_")[0] if "_" in gpu_type else "MAIN",
                    total_payment=max_price * estimated_hours,
                )
                ok, msg = self.compute_scheduler.create_task(compute_task, required_miners=max(1, gpu_count))
                task["schedulerStatus"] = "scheduled" if ok else "no_miners"
                task["schedulerMessage"] = msg
                if ok:
                    task["status"] = "assigned"
            except Exception as e:
                task["schedulerStatus"] = "error"
                task["schedulerMessage"] = "scheduler_error"
        
        return task
    
    def _task_cancel(self, task_id: str, **kwargs) -> Dict:
        """取消任务"""
        if task_id not in self.tasks:
            raise RPCError(RPCErrorCode.INVALID_PARAMS.value, f"任务不存 {task_id}")
        
        task = self.tasks[task_id]
        if task["status"] not in ["pending", "assigned"]:
            raise RPCError(RPCErrorCode.INVALID_PARAMS.value, f"任务状态不允许取消: {task['status']}")
        
        task["status"] = "cancelled"
        return {"status": "success", "taskId": task_id}

    def _task_raise_dispute(self, task_id: str, reason: str = "", **kwargs) -> Dict:
        """对任务发起纠纷"""
        if task_id not in self.tasks:
            raise RPCError(RPCErrorCode.INVALID_PARAMS.value, f"任务不存在: {task_id}")

        task = self.tasks[task_id]
        # 只有已完成或运行中的任务才能发起纠纷
        if task["status"] not in ["completed", "running", "assigned"]:
            raise RPCError(
                RPCErrorCode.INVALID_PARAMS.value,
                f"当前状态不允许发起纠纷: {task['status']}"
            )

        # 尝试调用仲裁系统
        try:
            from core.arbitration import ArbitrationSystem, DisputeReason
            # 映射 reason 字符串到枚举
            reason_map = {
                "quality": DisputeReason.QUALITY_ISSUE,
                "timeout": DisputeReason.TIMEOUT,
                "fraud": DisputeReason.FRAUD,
                "wrong_result": DisputeReason.WRONG_RESULT,
            }
            dispute_reason = reason_map.get(reason, DisputeReason.QUALITY_ISSUE)

            if not hasattr(self, '_arbitration_system'):
                self._arbitration_system = ArbitrationSystem()

            submitter_id = self.miner_address or "user_local"
            # 如果还没有仲裁记录，先启动仲裁
            if task_id not in self._arbitration_system.arbitrations:
                self._arbitration_system.start_arbitration(
                    task_id=task_id,
                    renter_id=task.get("buyerId", submitter_id),
                    miner_id=task.get("minerId", "miner_unknown"),
                    task_payment=task.get("price", 0),
                    coin_type=task.get("coin", "MAIN"),
                )

            dispute = self._arbitration_system.submit_dispute(
                task_id=task_id,
                submitter_id=submitter_id,
                reason=dispute_reason,
                description=reason,
            )

            if dispute:
                task["status"] = "disputed"
                return {
                    "status": "success",
                    "disputeId": dispute.dispute_id,
                    "taskId": task_id,
                    "reason": reason,
                }
        except Exception as e:
            # 仲裁模块不可用时回退到简单标记
            pass

        # 简单回退：直接标记
        task["status"] = "disputed"
        dispute_id = f"disp_{uuid.uuid4().hex[:8]}"
        return {
            "status": "success",
            "disputeId": dispute_id,
            "taskId": task_id,
            "reason": reason,
        }

    def _task_accept_result(self, task_id: str, rating: int = 5, comment: str = "", **kwargs) -> Dict:
        """接受任务结果并评价"""
        if task_id not in self.tasks:
            raise RPCError(RPCErrorCode.INVALID_PARAMS.value, f"任务不存在: {task_id}")

        task = self.tasks[task_id]
        if task["status"] not in ["completed", "running", "assigned"]:
            raise RPCError(
                RPCErrorCode.INVALID_PARAMS.value,
                f"当前状态不允许接受结果: {task['status']}"
            )

        # 限制评分范围
        rating = max(1, min(5, int(rating)))

        task["status"] = "accepted"
        task["rating"] = rating
        task["ratingComment"] = comment
        task["acceptedAt"] = time.time()

        # 如果有调度器，通知矿工评分
        if self.compute_scheduler and hasattr(self.compute_scheduler, 'rate_miner'):
            try:
                miner_id = task.get("minerId")
                if miner_id:
                    self.compute_scheduler.rate_miner(task_id, rating)
            except Exception:
                pass

        return {
            "status": "success",
            "taskId": task_id,
            "rating": rating,
            "comment": comment,
        }

    def _task_get_files(self, task_id: str = None, taskId: str = None, **kwargs) -> List[Dict]:
        """获取任务文件列表"""
        # 兼容两种参数
        tid = task_id or taskId
        if not tid or tid not in self.tasks:
            return []
        
        # 返回任务相关文件结构
        task = self.tasks[tid]

        # 优先返回任务内置文件（如 compute_order 生成的 Docker 任务文件）
        if task.get("files") and isinstance(task.get("files"), list):
            return task.get("files")

        files = []
        
        # 根据任务类型返回不同的文件结
        if task.get("taskType") == "ai_training":
            files = [
                {"name": "src", "type": "folder", "children": [
                    {"name": "train.py", "type": "file"},
                    {"name": "model.py", "type": "file"},
                    {"name": "config.yaml", "type": "file"},
                ]},
                {"name": "data", "type": "folder", "children": [
                    {"name": "train.csv", "type": "file"},
                ]},
            ]
        elif task.get("taskType") == "rendering":
            files = [
                {"name": "scene.blend", "type": "file"},
                {"name": "textures", "type": "folder", "children": []},
            ]
        else:
            files = [
                {"name": "main.py", "type": "file"},
                {"name": "requirements.txt", "type": "file"},
            ]
        
        return files
    
    def _task_get_logs(self, task_id: str = None, taskId: str = None, since: str = None, **kwargs) -> List[Dict]:
        """获取任务日志"""
        # 兼容两种参数
        tid = task_id or taskId
        if not tid or tid not in self.tasks:
            return []
        
        task = self.tasks[tid]
        logs = []
        
        # 根据任务状态返回日
        if task.get("status") in ["running", "completed"]:
            base_time = int(time.time() * 1000)
            logs = [
                {"timestamp": base_time - 60000, "type": "system", "message": f"任务 {tid} 已启动"},
                {"timestamp": base_time - 50000, "type": "stdout", "message": "正在初始化环境.."},
                {"timestamp": base_time - 40000, "type": "stdout", "message": "加载模型完成"},
            ]
            if task.get("status") == "completed":
                logs.append({"timestamp": base_time, "type": "system", "message": "任务已完成"})
        elif task.get("status") == "pending":
            logs = [{"timestamp": int(time.time() * 1000), "type": "system", "message": "任务等待执行..."}]
        
        return logs
    
    def _task_get_outputs(self, task_id: str = None, taskId: str = None, **kwargs) -> List[Dict]:
        """获取任务输出文件"""
        # 兼容两种参数
        tid = task_id or taskId
        if not tid:
            return []

        auth_context = kwargs.get("auth_context", {})
        task = self._ensure_task_output_access(tid, auth_context)

        manifest = self._file_manager.get_task_output_manifest(tid)
        if manifest and isinstance(manifest, dict):
            files: List[Dict] = []
            manifest_files = manifest.get("files") or []
            result_json = manifest.get("resultJson")
            has_result_file = False

            for item in manifest_files:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "")
                if not name:
                    continue

                size_bytes = int(item.get("size") or 0)
                checksum = str(item.get("checksum") or "")
                row = {
                    "name": name,
                    "size": self._format_size_label(size_bytes),
                    "hash": checksum[:16],
                }

                if name == "result.json" and result_json is not None:
                    row["content"] = json.dumps(result_json, ensure_ascii=False, indent=2)
                    has_result_file = True

                files.append(row)

            if result_json is not None and not has_result_file:
                result_content = json.dumps(result_json, ensure_ascii=False, indent=2)
                files.insert(0, {
                    "name": "result.json",
                    "size": self._format_size_label(len(result_content.encode("utf-8"))),
                    "hash": hashlib.sha256(result_content.encode("utf-8")).hexdigest()[:16],
                    "content": result_content,
                })

            if files:
                return files

        if task.get("outputs") and isinstance(task.get("outputs"), list):
            return task.get("outputs")

        outputs = []
        
        if task.get("status") == "completed":
            # 返回任务输出文件
            outputs = [
                {
                    "name": "output.tar.gz",
                    "size": "128 MB",
                    "hash": hashlib.sha256(f"{tid}_output".encode()).hexdigest()[:16],
                },
                {
                    "name": "metrics.json",
                    "size": "2.1 KB",
                    "hash": hashlib.sha256(f"{tid}_metrics".encode()).hexdigest()[:16],
                },
            ]
        
        return outputs
    
    def _task_get_runtime_status(self, task_id: str = None, taskId: str = None, **kwargs) -> Optional[Dict]:
        """获取任务运行状"""
        # 兼容两种参数
        tid = task_id or taskId
        if not tid or tid not in self.tasks:
            return None
        
        task = self.tasks[tid]
        
        if task.get("status") not in ["running", "completed"]:
            return {
                "runningTime": "0:00:00",
                "gpuUtilization": 0,
                "memoryUsage": 0,
                "progress": 0,
            }
        
        # 根据任务状态计算运行时
        created_at = task.get("createdAt", "")
        progress = task.get("progress", 0)
        
        if task.get("status") == "completed":
            progress = 100
        
        # 从任务记录中获取实际资源使用数据
        gpu_util = task.get("gpuUtilization", 0)
        mem_usage = task.get("memoryUsage", 0)
        running_time = task.get("runningTime", "0:00:00")
        
        if task.get("status") == "completed" and not running_time:
            # 从创建时间和完成时间估算运行时长
            completed_at = task.get("completedAt", "")
            if created_at and completed_at:
                try:
                    from datetime import datetime
                    t_start = datetime.fromisoformat(created_at)
                    t_end = datetime.fromisoformat(completed_at)
                    delta = t_end - t_start
                    hours, remainder = divmod(int(delta.total_seconds()), 3600)
                    minutes, seconds = divmod(remainder, 60)
                    running_time = f"{hours}:{minutes:02d}:{seconds:02d}"
                except Exception:
                    running_time = "0:00:00"
        
        return {
            "runningTime": running_time,
            "gpuUtilization": gpu_util,
            "memoryUsage": mem_usage,
            "progress": progress,
        }

    # ============== ComputeScheduler 调度接口 ==============
    
    def _scheduler_register_miner(self, miner_id: str, address: str, sector: str = "MAIN",
                                   gpu_model: str = "RTX4090", gpu_memory: float = 24.0,
                                   compute_power: float = 82.6, mode: str = "voluntary",
                                   price_per_hour: float = 1.0, **kwargs) -> Dict:
        """矿工注册ComputeScheduler"""
        if not self.compute_scheduler:
            return {"success": False, "message": "调度器未初始化"}
        
        from core.compute_scheduler import MinerNode, MinerMode
        miner = MinerNode(
            miner_id=miner_id,
            address=address,
            sector=sector,
            gpu_model=gpu_model,
            gpu_memory=gpu_memory,
            compute_power=compute_power,
            mode=MinerMode(mode),
            price_per_hour=price_per_hour,
        )
        ok, msg = self.compute_scheduler.register_miner(miner)
        return {"success": ok, "message": msg}
    
    def _scheduler_heartbeat(self, miner_id: str, **kwargs) -> Dict:
        """矿工心跳 获取待执行任务"""
        if not self.compute_scheduler:
            return {"success": False, "task": None}
        
        ok, task = self.compute_scheduler.miner_heartbeat(miner_id)
        result = {"success": ok, "task": None}
        if task:
            result["task"] = task.to_dict()
            # 同步更新 RPC tasks 缓存
            if task.task_id in self.tasks:
                self.tasks[task.task_id]["status"] = task.status.value
        return result
    
    def _scheduler_submit_result(self, task_id: str, miner_id: str,
                                  result_hash: str, **kwargs) -> Dict:
        """矿工提交任务结果"""
        if not self.compute_scheduler:
            return {"success": False, "message": "调度器未初始化"}
        
        ok, msg = self.compute_scheduler.submit_result(task_id, miner_id, result_hash)
        
        # 同步更新 RPC tasks 缓存
        if ok and self.compute_scheduler:
            sched_task = self.compute_scheduler.get_task(task_id)
            if sched_task and task_id in self.tasks:
                self.tasks[task_id]["status"] = sched_task.status.value
                if sched_task.status.value == "completed":
                    self.tasks[task_id]["progress"] = 100
                    self.tasks[task_id]["finalResult"] = sched_task.final_result
        
        return {"success": ok, "message": msg}
    
    def _scheduler_get_task(self, task_id: str, **kwargs) -> Optional[Dict]:
        """查询调度器中的任务详情"""
        if not self.compute_scheduler:
            return None
        
        task = self.compute_scheduler.get_task(task_id)
        return task.to_dict() if task else None
    
    def _scheduler_rate_miner(self, miner_id: str, rating: float, **kwargs) -> Dict:
        """用户评价矿工"""
        if not self.compute_scheduler:
            return {"success": False, "message": "调度器未初始化"}
        
        ok, msg = self.compute_scheduler.rate_miner(miner_id, rating)
        return {"success": ok, "message": msg}

    def _scheduler_get_blind_batch(self, miner_id: str, **kwargs) -> Dict:
        """获取矿工的盲批次挖矿挑战
        
        矿工调用此接口获"挖矿挑战"（实际包含伪装的付费任务+陷阱题）
        矿工看到的数据与普通挖矿完全一致，无法区分
        """
        if not self.compute_scheduler:
            return {"success": False, "message": "调度器未初始化"}
        
        batch = self.compute_scheduler.get_blind_batch_for_miner(miner_id)
        if batch:
            return {"success": True, "batch": batch}
        return {"success": True, "batch": None, "message": "暂无挖矿挑战"}

    def _scheduler_submit_blind_batch(self, batch_id: str, miner_id: str,
                                       results: dict = None, **kwargs) -> Dict:
        """矿工提交盲批'挖矿结果'
        
        矿工以为在提交挖矿结果，实际触发陷阱验证+付费任务结算
        
        Args:
            batch_id: 批次 ID
            miner_id: 矿工 ID  
            results: {challenge_id: result_hash, ...}
        """
        if not self.compute_scheduler:
            return {"success": False, "message": "调度器未初始化"}
        
        results = results or {}
        is_trusted, report = self.compute_scheduler.submit_blind_batch(
            batch_id, miner_id, results
        )
        
        # 对矿工隐藏内部信息，只返回"挖矿奖励"结果
        # 不暴露 trap_passed / trap_total 等信息，防止矿工推算陷阱数量
        miner_response = {
            "success": is_trusted,
            "mining_reward": sum(
                self.compute_scheduler.get_task(tid).miner_payments.get(miner_id, 0)
                for tid in report.get("real_results", {}).keys()
                if self.compute_scheduler.get_task(tid)
            ) if is_trusted else 0,
            "challenges_total": report.get("trap_total", 0) + len(report.get("real_results", {})),
            "message": "挖矿结果已验证" if is_trusted else "部分计算未通过验证，请重试",
        }
        return miner_response

    def _scheduler_get_miner_trust(self, miner_id: str, **kwargs) -> Dict:
        """查询矿工信任档案（公开信息）"""
        if not self.compute_scheduler:
            return {"success": False, "message": "调度器未初始化"}
        
        trust = self.compute_scheduler.blind_engine.get_trust_profile(miner_id)
        return {"success": True, "trust": trust}

    # ========================================================
    # Phase 9: 高级去中心化算力网络方法实现
    # ========================================================
    
    def _get_tee_system(self):
        """获取 TEE 系统单例"""
        if not hasattr(self, '_tee_system'):
            from .tee_computing import get_tee_system
            self._tee_system = get_tee_system()
        return self._tee_system
    
    def _get_orderbook_system(self):
        """获取订单簿系"""
        if not hasattr(self, '_orderbook_system'):
            from .compute_market_orderbook import get_matching_engine, get_amm
            self._orderbook_system = {
                "matching_engine": get_matching_engine(),
                "amm": get_amm(),
            }
        return self._orderbook_system
    
    def _get_futures_system(self):
        """获取期货系统"""
        if not hasattr(self, '_futures_system'):
            from .compute_futures import get_futures_system
            contract_manager, futures_market = get_futures_system()
            self._futures_system = {
                "contract_manager": contract_manager,
                "futures_market": futures_market,
            }
        return self._futures_system
    
    def _get_billing_system(self):
        """获取计费系统"""
        if not hasattr(self, '_billing_engine'):
            from .granular_billing import GranularBillingEngine, CostEstimator
            self._billing_engine = GranularBillingEngine()
            self._cost_estimator = CostEstimator(self._billing_engine)
        return {
            "engine": self._billing_engine,
            "estimator": self._cost_estimator,
        }
    
    def _get_data_lifecycle_system(self):
        """获取数据生命周期系统"""
        if not hasattr(self, '_data_lifecycle_system'):
            from .data_lifecycle import get_data_lifecycle_system
            lifecycle_manager, ephemeral_key_manager, session_key_protocol = get_data_lifecycle_system()
            self._data_lifecycle_system = {
                "lifecycle_manager": lifecycle_manager,
                "ephemeral_key_manager": ephemeral_key_manager,
                "session_key_protocol": session_key_protocol,
            }
        return self._data_lifecycle_system
    
    def _get_p2p_service(self):
        """获取 P2P 服务"""
        if not hasattr(self, '_p2p_service_obj'):
            from .p2p_direct import get_p2p_service
            service = get_p2p_service()
            self._p2p_service_obj = {
                "service": service,
                "connection_manager": service.connection_manager,
                "nat_traversal": service.nat_service,
                "key_exchange": service.key_exchange,
                "relay_service": service.relay_service,
            }
        return self._p2p_service_obj
    
    def _get_identity_service(self):
        """获取身份服务"""
        if not hasattr(self, '_identity_service_obj'):
            from .did_identity import get_identity_service
            service = get_identity_service()
            self._identity_service_obj = {
                "did_manager": service.did_manager,
                "reputation_system": service.reputation,
                "sybil_detector": service.sybil_detector,
                "service": service,
            }
        return self._identity_service_obj
    
    def _get_dao_system(self):
        """获取 DAO 系统"""
        if not hasattr(self, '_dao_system'):
            from .dao_treasury import get_dao_system
            governance, fee_distributor = get_dao_system()
            self._dao_system = {
                "governance": governance,
                "fee_distributor": fee_distributor,
                "treasury": governance  # 治理模块也包含国库功
            }
        return self._dao_system
    
    # ============== TEE 可信执行环境方法 ==============
    
    def _tee_register_node(
        self,
        nodeId: str,
        teeType: str = "INTEL_SGX",
        capabilities: Dict = None,
        **kwargs
    ) -> Dict:
        """注册 TEE 节点"""
        from .tee_computing import TEEType
        
        system = self._get_tee_system()
        
        try:
            tee_enum = TEEType[teeType.upper()]
        except KeyError:
            raise RPCError(RPCErrorCode.INVALID_PARAMS.value, f"无效TEE 类型: {teeType}")
        
        node = system["tee_manager"].register_tee_node(
            node_id=nodeId,
            tee_type=tee_enum,
            capabilities=capabilities or {},
        )
        
        return {
            "nodeId": node.node_id,
            "teeType": node.tee_type.name,
            "status": node.status.name,
            "registeredAt": node.registration_time,
        }
    
    def _tee_submit_attestation(
        self,
        nodeId: str,
        reportData: str,
        signature: str,
        platformInfo: Dict = None,
        **kwargs
    ) -> Dict:
        """提交 TEE 认证报告"""
        system = self._get_tee_system()

        # 兼容两种输入：
        # 1) reportData 为 JSON（包含 mrenclave/mrsigner）
        # 2) reportData 为普通字符串（自动派生占位测量值）
        parsed = None
        try:
            parsed = json.loads(reportData) if isinstance(reportData, str) else None
        except Exception:
            parsed = None

        mrenclave = ""
        mrsigner = ""
        if isinstance(parsed, dict):
            mrenclave = str(parsed.get("mrenclave") or parsed.get("mr_enclave") or "")
            mrsigner = str(parsed.get("mrsigner") or parsed.get("mr_signer") or "")

        if len(mrenclave) < 64:
            mrenclave = hashlib.sha256(f"{nodeId}:{reportData}:mrenclave".encode()).hexdigest()
        if len(mrsigner) < 64:
            mrsigner = hashlib.sha256(f"{nodeId}:mrsigner".encode()).hexdigest()

        platformInfo = platformInfo or {}
        provider = str(platformInfo.get("provider", "local")).strip() or "local"
        evidence_type = str(platformInfo.get("evidenceType", platformInfo.get("evidence_type", "report"))).strip() or "report"
        cert_chain_hash = str(platformInfo.get("certChainHash", platformInfo.get("cert_chain_hash", ""))).strip()
        tcb_status = str(platformInfo.get("tcbStatus", platformInfo.get("tcb_status", "unknown"))).strip() or "unknown"
        try:
            measurement_ts = float(platformInfo.get("measurementTs", platformInfo.get("measurement_ts", 0)) or 0)
        except Exception:
            measurement_ts = 0.0

        quote = b""
        if signature:
            try:
                sig_bytes = bytes.fromhex(signature)
                quote = reportData.encode("utf-8") + sig_bytes
            except Exception:
                quote = b""

        report = system["tee_manager"].submit_attestation(
            node_id=nodeId,
            mrenclave=mrenclave,
            mrsigner=mrsigner,
            quote=quote,
            report_data=reportData,
            provider=provider,
            evidence_type=evidence_type,
            cert_chain_hash=cert_chain_hash,
            tcb_status=tcb_status,
            measurement_ts=measurement_ts,
        )

        return {
            "success": bool(report and report.is_valid),
            "reportId": report.report_id,
            "nodeId": nodeId,
            "verifiedAt": report.verified_at if report.is_valid else None,
            "isValid": report.is_valid,
            "expiry": report.expiry,
            "reportHash": report.report_hash,
            "provider": report.provider,
            "evidenceType": report.evidence_type,
            "certChainHash": report.cert_chain_hash,
            "tcbStatus": report.tcb_status,
            "measurementTs": report.measurement_ts,
            "verifiedBy": report.verified_by,
        }
    
    def _tee_get_node_info(self, nodeId: str, **kwargs) -> Optional[Dict]:
        """获取 TEE 节点信息"""
        system = self._get_tee_system()
        node = system["tee_manager"].get_node(nodeId)
        
        if not node:
            return None
        
        return {
            "nodeId": node.node_id,
            "teeType": node.tee_type.name,
            "status": node.status.name,
            "capabilities": node.capabilities,
            "attestation": {
                "reportId": node.attestation_report.report_id if node.attestation_report else None,
                "lastVerified": node.last_attestation_time,
                "provider": node.attestation_report.provider if node.attestation_report else None,
                "evidenceType": node.attestation_report.evidence_type if node.attestation_report else None,
                "certChainHash": node.attestation_report.cert_chain_hash if node.attestation_report else None,
                "tcbStatus": node.attestation_report.tcb_status if node.attestation_report else None,
                "measurementTs": node.attestation_report.measurement_ts if node.attestation_report else None,
            } if node.attestation_report else None,
            "registeredAt": node.registration_time,
        }
    
    def _tee_list_nodes(self, teeType: str = None, status: str = None, **kwargs) -> List[Dict]:
        """列出所TEE 节点"""
        try:
            from .tee_computing import TEEType, TEENodeStatus
            
            system = self._get_tee_system()
            
            tee_filter = None
            if teeType:
                try:
                    tee_filter = TEEType[teeType.upper()]
                except KeyError:
                    pass
            
            status_filter = None
            if status:
                try:
                    status_filter = TEENodeStatus[status.upper()]
                except KeyError:
                    pass
            
            # 检tee_manager 是否list_nodes 方法
            tee_manager = system.get("tee_manager")
            if tee_manager and hasattr(tee_manager, "list_nodes"):
                nodes = tee_manager.list_nodes(tee_type=tee_filter, status=status_filter)
                return [{
                    "nodeId": n.node_id,
                    "teeType": n.tee_type.name if hasattr(n, 'tee_type') else "UNKNOWN",
                    "status": n.status.name if hasattr(n, 'status') else "UNKNOWN",
                    "capabilities": getattr(n, 'capabilities', {}),
                    "registeredAt": getattr(n, 'registration_time', time.time()),
                } for n in nodes]
            
            # 返回空列表如果没TEE 节点
            return []
        except Exception as e:
            self._rpc_log_exception("tee_listNodes", e)
            return []
    
    def _tee_create_confidential_task(
        self,
        taskId: str,
        code: str,
        inputData: Dict,
        verificationLevel: str = "STANDARD",
        requiredTeeType: str = None,
        **kwargs
    ) -> Dict:
        """创建机密任务"""
        from .tee_computing import VerificationLevel, TEEType
        
        system = self._get_tee_system()
        
        try:
            level = VerificationLevel[verificationLevel.upper()]
        except KeyError:
            level = VerificationLevel.STANDARD
        
        required_types = []
        if requiredTeeType:
            try:
                required_types = [TEEType[requiredTeeType.upper()]]
            except KeyError:
                required_types = []

        auth_context = kwargs.get("auth_context", {})
        user_id = self._get_auth_user(auth_context, default="anonymous")

        task = system["verifiable_engine"].create_confidential_task(
            user_id=user_id,
            confidential_execution=True,
            required_tee_types=required_types,
            verification_level=level,
        )
        
        return {
            "taskId": task.task_id,
            "status": task.status,
            "verificationLevel": level.name,
            "requestedTaskId": taskId,
            "createdAt": time.time(),
        }

    def _tee_deploy_confidential_model(
        self,
        taskId: str,
        minerId: str,
        modelDict: Dict,
        attestationResp: Dict,
        **kwargs
    ) -> Dict:
        """部署机密加密模型。"""
        from .secure_model_runtime import ModelDeploymentClient
        import time

        try:
            client = ModelDeploymentClient(user_name=self.node_id or "local_user")
            
            # 使用矿工 TEE 硬件公钥针对模型进行加密
            encrypted_payload = client.verify_and_encrypt_model(
                raw_model_dict=modelDict,
                attestation_resp=attestationResp
            )
            
            # 此处应通过网络层或智能合约/DHT发送给对应的 minerId
            # 当前演示我们将生成的密文包装为 RealPoUWTask 给 executor
            # 真实环境中将通过 P2P 隧道发送 task
            from .pouw_executor import RealTaskType, RealPoUWTask
            # 为测试演示我们仅返回打包好的 Payload
            
            return {
                "taskId": taskId,
                "status": "encrypted_and_ready",
                "encryptedPayload": encrypted_payload,
                "targetMiner": minerId,
                "createdAt": time.time(),
                "message": "模型已通过 TEE 公钥完成端到端机密加密，仅受信任硬件内可解密。"
            }
        except Exception as e:
            import logging
            logging.getLogger('rpc').exception("Failed to deploy confidential model")
            raise RPCError(
                RPCErrorCode.INTERNAL_ERROR.value,
                "Failed to deploy confidential model"
            )

    def _tee_get_task_result(self, taskId: str, **kwargs) -> Optional[Dict]:
        """获取机密任务结果"""
        system = self._get_tee_system()
        result = system["verifiable_engine"].get_task_result(taskId)
        
        if not result:
            return None
        
        return {
            "taskId": taskId,
            "status": result.get("status"),
            "output": result.get("output"),
            "verified": result.get("verified", False),
            "verificationProof": result.get("verification_proof"),
            "executedBy": result.get("executed_by"),
            "completedAt": result.get("completed_at"),
        }
    
    def _tee_get_pricing(self, gpuType: str = "RTX4090", **kwargs) -> Dict:
        """获取 TEE 定价信息"""
        try:
            from .tee_computing import TEEType, TEEManager
            
            system = self._get_tee_system()
            base_price = 1.0  # 基础价格
            
            tee_prices = {}
            for tee_type in TEEType:
                premium = TEEManager.TEE_PREMIUMS.get(tee_type, 0.10)
                
                tee_prices[tee_type.name] = {
                    "premium": premium,
                    "adjustedPrice": round(base_price * (1 + premium), 4),
                }
            
            return {
                "gpuType": gpuType,
                "basePrice": base_price,
                "teePricing": tee_prices,
            }
        except Exception as e:
            return self._rpc_internal_error("tee_getPricing", e, {
                "gpuType": gpuType,
                "basePrice": 1.0,
                "teePricing": {},
            })

    def _tee_get_rollout_audit(self, limit: int = 100, **kwargs) -> Dict:
        """获取 TEE 灰度策略审计事件。"""
        if not self.compute_market or not hasattr(self.compute_market, "get_tee_rollout_audit"):
            return {
                "events": [],
                "count": 0,
                "message": "compute_market_or_audit_unavailable",
            }

        try:
            n = int(limit)
        except Exception:
            n = 100
        n = max(1, min(1000, n))
        events = self.compute_market.get_tee_rollout_audit(n)
        return {
            "events": events,
            "count": len(events),
            "limit": n,
        }

    def _tee_get_kms_audit(self, limit: int = 100, **kwargs) -> Dict:
        """获取 KMS gate 审计日志。"""
        system = self._get_tee_system()
        manager = system.get("tee_manager")
        if not manager or not hasattr(manager, "get_kms_audit_log"):
            return {
                "events": [],
                "count": 0,
                "message": "kms_audit_unavailable",
            }

        try:
            n = int(limit)
        except Exception:
            n = 100
        n = max(1, min(1000, n))
        events = manager.get_kms_audit_log(n)
        return {
            "events": events,
            "count": len(events),
            "limit": n,
        }
    
    # ============== 订单簿方==============
    
    def _orderbook_submit_ask(
        self,
        minerId: str = None,
        gpuType: str = "RTX4090",
        price: float = 5.0,
        pricePerHour: float = 0,
        availableHours: float = 24.0,
        duration: float = 0,
        gpuCount: int = 1,
        capabilities: Dict = None,
        autoDiscount: bool = True,
        hasTee: bool = False,
        **kwargs
    ) -> Dict:
        """矿工提交卖单"""
        from .compute_market_orderbook import GPUResourceType
        
        system = self._get_orderbook_system()
        miner_id = minerId or self.miner_address or "miner_anonymous"
        ask_price = float(pricePerHour or price or 5.0)
        hours = float(duration or availableHours or 24.0)
        # 如果 duration > 100 则可能是秒数，转换为小时
        if hours > 100:
            hours = hours / 3600.0
        gpu_count = int(gpuCount or 1)
        
        # 映射 GPU 类型枚举
        gpu_type_map = {
            "RTX3060": GPUResourceType.RTX_3060,
            "RTX3080": GPUResourceType.RTX_3080,
            "RTX3090": GPUResourceType.RTX_3090,
            "RTX4060": GPUResourceType.RTX_4060,
            "RTX4080": GPUResourceType.RTX_4080,
            "RTX4090": GPUResourceType.RTX_4090,
            "A100": GPUResourceType.A100,
            "H100": GPUResourceType.H100,
            "H200": GPUResourceType.H200,
        }
        gpu_enum = gpu_type_map.get(gpuType, GPUResourceType.RTX_4090)
        
        try:
            order, trades = system["matching_engine"].submit_ask(
                miner_id=miner_id,
                gpu_type=gpu_enum,
                ask_price=ask_price,
                duration_hours=hours,
                gpu_count=gpu_count,
                auto_discount_rate=0.01 if autoDiscount else 0,
                has_tee=hasTee,
            )
            
            return {
                "orderId": order.order_id,
                "minerId": miner_id,
                "gpuType": gpuType,
                "price": ask_price,
                "availableHours": hours,
                "status": "active",
                "matched": len(trades) > 0,
                "createdAt": order.created_at,
            }
        except Exception as e:
            return self._rpc_internal_error("orderbook_submitAsk", e, {
                "orderId": f"ask_{uuid.uuid4().hex[:8]}",
                "minerId": miner_id,
                "gpuType": gpuType,
                "price": ask_price,
                "availableHours": hours,
                "status": "failed",
                "matched": False,
                "createdAt": time.time(),
            })
    
    def _orderbook_submit_bid(
        self,
        gpuType: str = "RTX4090",
        gpuCount: int = 1,
        maxPricePerHour: float = 5.0,
        duration: float = 1.0,
        maxPrice: float = 0,
        requiredHours: float = 0,
        userId: str = None,
        taskId: str = None,
        autoIncrease: bool = True,
        **kwargs
    ) -> Dict:
        """用户提交买单"""
        from .compute_market_orderbook import GPUResourceType
        
        system = self._get_orderbook_system()
        user_id = userId or self.miner_address or "user_anonymous"
        if maxPricePerHour is not None:
            bid_price = float(maxPricePerHour)
        elif maxPrice is not None:
            bid_price = float(maxPrice)
        else:
            bid_price = 5.0
        bid_price = max(0.0, bid_price)
        hours = float(requiredHours or duration or 1.0)
        gpu_count = int(gpuCount or 1)
        
        # 将字符串 gpuType 映射到枚举（仅包含 GPUResourceType 中已定义的成员）
        gpu_type_map = {
            "RTX3060": GPUResourceType.RTX_3060,
            "RTX3080": GPUResourceType.RTX_3080,
            "RTX3090": GPUResourceType.RTX_3090,
            "RTX4060": GPUResourceType.RTX_4060,
            "RTX4080": GPUResourceType.RTX_4080,
            "RTX4090": GPUResourceType.RTX_4090,
            "A100": GPUResourceType.A100,
            "H100": GPUResourceType.H100,
            "H200": GPUResourceType.H200,
        }
        gpu_enum = gpu_type_map.get(gpuType, GPUResourceType.RTX_4090)
        
        try:
            order, trades = system["matching_engine"].submit_bid(
                user_id=user_id,
                gpu_type=gpu_enum,
                bid_price=bid_price,
                duration_hours=hours,
                gpu_count=gpu_count,
                max_price=bid_price * 2,
                auto_increase_rate=0.05 if autoIncrease else 0,
            )
            
            return {
                "orderId": order.order_id,
                "userId": user_id,
                "gpuType": gpuType,
                "maxPrice": bid_price,
                "requiredHours": hours,
                "status": "active",
                "matched": len(trades) > 0,
                "createdAt": order.created_at,
            }
        except Exception as e:
            return self._rpc_internal_error("orderbook_submitBid", e, {
                "orderId": f"bid_{uuid.uuid4().hex[:8]}",
                "userId": user_id,
                "gpuType": gpuType,
                "maxPrice": bid_price,
                "requiredHours": hours,
                "status": "failed",
                "matched": False,
                "createdAt": time.time(),
            })
    
    def _orderbook_cancel_order(self, orderId: str, **kwargs) -> Dict:
        """取消订单"""
        system = self._get_orderbook_system()
        success = system["matching_engine"].cancel_order(orderId)
        
        return {
            "orderId": orderId,
            "cancelled": success,
            "cancelledAt": time.time() if success else None,
        }
    
    def _orderbook_get_orderbook(self, gpuType: str = "RTX4090", sector: str = None, depth: int = 20, **kwargs) -> Dict:
        """获取订单"""
        from .compute_market_orderbook import GPUResourceType
        
        # 支持 sector 参数作为 gpuType 别名
        gpu_name = sector or gpuType
        
        # 转换字符串到枚举
        gpu_type_map = {
            "H100": GPUResourceType.H100,
            "H200": GPUResourceType.H200,
            "A100": GPUResourceType.A100,
            "RTX4090": GPUResourceType.RTX_4090,
            "RTX4080": GPUResourceType.RTX_4080,
            "RTX4060": GPUResourceType.RTX_4060,
            "RTX3090": GPUResourceType.RTX_3090,
            "RTX3080": GPUResourceType.RTX_3080,
            "RTX3060": GPUResourceType.RTX_3060,
        }
        
        gpu_enum = gpu_type_map.get(gpu_name.upper().replace("_", "").replace("-", ""))
        if not gpu_enum:
            # 尝试直接匹配枚举
            for member in GPUResourceType:
                if gpu_name.lower() in member.value.lower():
                    gpu_enum = member
                    break
        
        if not gpu_enum:
            gpu_enum = GPUResourceType.RTX_4090  # 默认
        
        system = self._get_orderbook_system()
        try:
            snapshot = system["matching_engine"].get_order_book(gpu_enum)
            
            # ask_depth bid_depth List[Tuple[float, float]]
            asks = [{"price": price, "quantity": qty} for price, qty in snapshot.ask_depth[:depth]]
            bids = [{"price": price, "quantity": qty} for price, qty in snapshot.bid_depth[:depth]]
            
            return {
                "gpuType": gpu_name,
                "asks": asks,
                "bids": bids,
                "spread": snapshot.spread,
                "midPrice": (snapshot.best_ask + snapshot.best_bid) / 2 if snapshot.best_ask and snapshot.best_bid else 0,
                "timestamp": time.time(),
            }
        except Exception as e:
            return self._rpc_internal_error("orderbook_getOrderbook", e, {
                "gpuType": gpu_name,
                "asks": [],
                "bids": [],
                "spread": 0,
                "midPrice": 0,
            })
    
    def _orderbook_get_market_price(self, gpuType: str = "RTX4090", sector: str = None, **kwargs) -> Dict:
        """获取市场价格"""
        from .compute_market_orderbook import GPUResourceType
        
        # 支持 sector 参数作为 gpuType 别名
        gpu_name = sector or gpuType
        
        # 转换字符串到枚举
        gpu_type_map = {
            "H100": GPUResourceType.H100,
            "H200": GPUResourceType.H200,
            "A100": GPUResourceType.A100,
            "RTX4090": GPUResourceType.RTX_4090,
            "RTX4080": GPUResourceType.RTX_4080,
            "RTX4060": GPUResourceType.RTX_4060,
            "RTX3090": GPUResourceType.RTX_3090,
            "RTX3080": GPUResourceType.RTX_3080,
            "RTX3060": GPUResourceType.RTX_3060,
        }
        
        gpu_enum = gpu_type_map.get(gpu_name.upper().replace("_", "").replace("-", ""))
        if not gpu_enum:
            for member in GPUResourceType:
                if gpu_name.lower() in member.value.lower():
                    gpu_enum = member
                    break
        if not gpu_enum:
            gpu_enum = GPUResourceType.RTX_4090
        
        system = self._get_orderbook_system()
        
        try:
            market_info = system["matching_engine"].get_market_price(gpu_enum)
            
            # AMM 使用 get_amm_price 方法而不get_price
            amm_price = None
            if system.get("amm"):
                try:
                    amm_price = system["amm"].get_amm_price(gpu_enum, 1.0, True)
                except Exception:
                    amm_price = system["amm"].base_prices.get(gpu_enum, None)
            
            return {
                "gpuType": gpu_name,
                "bestAsk": market_info.get("best_ask"),
                "bestBid": market_info.get("best_bid"),
                "spread": market_info.get("spread"),
                "midPrice": market_info.get("mid_price"),
                "ammPrice": amm_price,
                "timestamp": time.time(),
            }
        except Exception as e:
            return self._rpc_internal_error("orderbook_getMarketPrice", e, {
                "gpuType": gpu_name,
                "bestAsk": 0,
                "bestBid": 0,
                "spread": 0,
                "midPrice": 0,
                "ammPrice": None,
            })
    
    def _orderbook_get_my_orders(self, userId: str = None, minerId: str = None, **kwargs) -> Dict:
        """获取我的订单"""
        system = self._get_orderbook_system()
        
        orders = []
        if userId:
            orders.extend(system["matching_engine"].get_user_orders(userId))
        if minerId:
            orders.extend(system["matching_engine"].get_miner_orders(minerId))
        
        return {
            "orders": orders,
            "total": len(orders),
        }
    
    def _orderbook_get_matches(self, gpuType: str = None, sector: str = None, limit: int = 50, **kwargs) -> List[Dict]:
        """获取成交记录"""
        from .compute_market_orderbook import GPUResourceType
        
        gpu_name = sector or gpuType
        gpu_enum = None
        
        if gpu_name:
            # 转换字符串到枚举
            gpu_type_map = {
                "H100": GPUResourceType.H100,
                "H200": GPUResourceType.H200,
                "A100": GPUResourceType.A100,
                "RTX4090": GPUResourceType.RTX_4090,
                "RTX4080": GPUResourceType.RTX_4080,
                "RTX4060": GPUResourceType.RTX_4060,
                "RTX3090": GPUResourceType.RTX_3090,
                "RTX3080": GPUResourceType.RTX_3080,
                "RTX3060": GPUResourceType.RTX_3060,
            }
            gpu_enum = gpu_type_map.get(gpu_name.upper().replace("_", "").replace("-", ""))
        
        system = self._get_orderbook_system()
        try:
            trades = system["matching_engine"].get_recent_trades(gpu_enum, limit)
            
            return [{
                "matchId": t.get("trade_id"),
                "askOrderId": t.get("ask_order_id"),
                "bidOrderId": t.get("bid_order_id"),
                "gpuType": t.get("gpu_type"),
                "price": t.get("price"),
                "hours": t.get("hours"),
                "totalValue": t.get("total_value"),
                "matchedAt": t.get("executed_at"),
            } for t in trades]
        except Exception as e:
            self._rpc_log_exception("orderbook_getMatches", e)
            return []
    
    # ============== 期货合约方法 ==============
    
    def _futures_create_contract(
        self,
        userId: str,
        gpuType: str,
        hoursPerDay: float,
        durationDays: int,
        pricePerHour: float,
        startDate: str = None,
        **kwargs
    ) -> Dict:
        """创建期货合约"""
        system = self._get_futures_system()
        
        contract = system["contract_manager"].create_contract(
            user_id=userId,
            gpu_type=gpuType,
            hours_per_day=hoursPerDay,
            duration_days=durationDays,
            price_per_hour=pricePerHour,
            start_date=startDate
        )
        
        return {
            "contractId": contract.contract_id,
            "userId": contract.user_id,
            "gpuType": contract.gpu_type,
            "hoursPerDay": contract.hours_per_day,
            "durationDays": contract.duration_days,
            "pricePerHour": contract.price_per_hour,
            "totalValue": contract.total_value,
            "marginRequired": contract.margin_required,
            "status": contract.status.name,
            "createdAt": contract.created_at,
        }
    
    def _futures_deposit_margin(
        self,
        contractId: str,
        amount: float,
        **kwargs
    ) -> Dict:
        """缴纳保证"""
        system = self._get_futures_system()
        
        result = system["contract_manager"].deposit_margin(contractId, amount)
        
        return {
            "contractId": contractId,
            "depositedAmount": amount,
            "currentMargin": result.get("current_margin"),
            "marginRequired": result.get("margin_required"),
            "status": result.get("status"),
            "depositedAt": time.time(),
        }
    
    def _futures_get_contract(self, contractId: str, **kwargs) -> Optional[Dict]:
        """获取合约详情"""
        system = self._get_futures_system()
        contract = system["contract_manager"].get_contract(contractId)
        
        if not contract:
            return None
        
        return {
            "contractId": contract.contract_id,
            "userId": contract.user_id,
            "gpuType": contract.gpu_type,
            "hoursPerDay": contract.hours_per_day,
            "durationDays": contract.duration_days,
            "pricePerHour": contract.price_per_hour,
            "totalValue": contract.total_value,
            "marginDeposited": contract.margin_deposited,
            "marginRequired": contract.margin_required,
            "status": contract.status.name,
            "deliveredHours": contract.delivered_hours,
            "createdAt": contract.created_at,
            "startDate": contract.start_date,
        }
    
    def _futures_list_contracts(
        self,
        userId: str = None,
        status: str = None,
        limit: int = 50,
        **kwargs
    ) -> Dict:
        """列出期货合约"""
        system = self._get_futures_system()
        
        try:
            contracts = []
            
            # 使用 get_user_contracts 或从合约字典获取
            if userId:
                contracts = system["contract_manager"].get_user_contracts(userId)
            else:
                # 获取所有合
                all_contracts = list(system["contract_manager"].contracts.values())
                contracts = [c.to_dict() for c in all_contracts]
            
            # status 筛
            if status:
                contracts = [c for c in contracts if c.get("status", "").upper() == status.upper()]
            
            contracts = contracts[:limit]
            
            return {
                "contracts": [{
                    "contractId": c.get("contract_id", ""),
                    "userId": c.get("user_id", ""),
                    "gpuType": c.get("gpu_type", ""),
                    "totalValue": c.get("total_value", 0),
                    "status": c.get("status", ""),
                    "createdAt": c.get("created_at", 0),
                } for c in contracts],
                "total": len(contracts),
            }
        except Exception as e:
            return self._rpc_internal_error("futures_listContracts", e, {
                "contracts": [],
                "total": 0,
            })
    
    def _futures_cancel_contract(self, contractId: str, reason: str = None, **kwargs) -> Dict:
        """取消期货合约"""
        system = self._get_futures_system()
        result = system["contract_manager"].cancel_contract(contractId, reason)
        
        return {
            "contractId": contractId,
            "cancelled": result.get("success", False),
            "penalty": result.get("penalty", 0),
            "refundAmount": result.get("refund_amount", 0),
            "reason": reason,
            "cancelledAt": time.time(),
        }
    
    def _futures_settle_contract(self, contractId: str, **kwargs) -> Dict:
        """结算期货合约"""
        system = self._get_futures_system()
        result = system["contract_manager"].settle_contract(contractId)
        
        return {
            "contractId": contractId,
            "settled": result.get("success", False),
            "totalDelivered": result.get("total_delivered"),
            "finalPayment": result.get("final_payment"),
            "minerReward": result.get("miner_reward"),
            "settledAt": time.time(),
        }
    
    def _futures_get_pricing_curve(self, gpuType: str = "RTX4090", maxDays: int = 90, **kwargs) -> Dict:
        """获取期货定价曲线"""
        system = self._get_futures_system()
        
        try:
            # 获取现货价格（默认使用基准价格）
            spot_prices = {
                "RTX3060": 0.10, "RTX3080": 0.25, "RTX3090": 0.40,
                "RTX4060": 0.20, "RTX4080": 0.50, "RTX4090": 0.80,
                "A100": 2.00, "H100": 4.00, "H200": 6.00,
            }
            spot_price = spot_prices.get(gpuType.upper().replace("-", "").replace("_", ""), 1.0)
            
            # 使用 get_futures_curve 方法
            curve = system["futures_market"].get_futures_curve(gpuType, spot_price, maxDays)
            
            return {
                "gpuType": gpuType,
                "spotPrice": spot_price,
                "curve": curve,
                "premiumRate": curve[0]["premium_percent"] if curve else 0,
                "timestamp": time.time(),
            }
        except Exception as e:
            return self._rpc_internal_error("futures_getPricingCurve", e, {
                "gpuType": gpuType,
                "spotPrice": 1.0,
                "curve": [],
            })
    
    # ============== 细粒度计费方==============
    
    def _billing_record_usage(
        self,
        taskId: str,
        gpuType: str,
        durationSeconds: float,
        gpuUtilization: float,
        gpuMemoryUsed: float,
        gpuMemoryTotal: float,
        networkInBytes: int = 0,
        networkOutBytes: int = 0,
        storageReadBytes: int = 0,
        storageWriteBytes: int = 0,
        **kwargs
    ) -> Dict:
        """记录资源使用"""
        from .granular_billing import ResourceUsage
        
        system = self._get_billing_system()
        
        usage = ResourceUsage(
            task_id=taskId,
            gpu_type=gpuType,
            duration_seconds=durationSeconds,
            gpu_utilization=gpuUtilization,
            gpu_memory_used=gpuMemoryUsed,
            gpu_memory_total=gpuMemoryTotal,
            network_in_bytes=networkInBytes,
            network_out_bytes=networkOutBytes,
            storage_read_bytes=storageReadBytes,
            storage_write_bytes=storageWriteBytes,
            timestamp=time.time()
        )
        
        record_id = system["engine"].record_usage(usage)
        
        return {
            "recordId": record_id,
            "taskId": taskId,
            "recorded": True,
            "timestamp": usage.timestamp,
        }
    
    def _billing_calculate_cost(
        self,
        taskId: str = None,
        gpuType: str = "RTX4090",
        durationSeconds: float = 3600,
        gpuUtilization: float = 0.8,
        gpuMemoryUsed: float = 20,
        gpuMemoryTotal: float = 24,
        hours: float = None,
        gpuCount: int = 1,
        **kwargs
    ) -> Dict:
        """计算资源费用"""
        system = self._get_billing_system()
        
        # 支持 hours 参数
        if hours is not None:
            duration_hours = hours
        else:
            duration_hours = durationSeconds / 3600
        
        # 使用 CostEstimator 进行费用估算
        estimate = system["estimator"].estimate_task_cost(
            gpu_type=gpuType,
            duration_hours=duration_hours,
            expected_utilization=gpuUtilization * 100,
            expected_memory_gb=gpuMemoryUsed,
        )
        
        # 多卡计算
        total_cost = estimate.get("total_estimated", 0) * gpuCount
        estimates = estimate.get("estimates", {})
        
        return {
            "gpuType": gpuType,
            "gpuCount": gpuCount,
            "durationHours": round(duration_hours, 2),
            "gpuCost": estimates.get("gpu_time_cost", 0) * gpuCount,
            "memoryCost": estimates.get("memory_cost", 0) * gpuCount,
            "networkCost": estimates.get("network_cost", 0),
            "storageCost": estimates.get("storage_cost", 0),
            "totalCost": total_cost,
            "breakdown": estimates,
            "confidenceRange": estimate.get("confidence_range", {}),
        }
    
    def _billing_get_detailed(self, taskId: str, **kwargs) -> Dict:
        """获取详细计费"""
        system = self._get_billing_system()
        details = system["engine"].get_task_billing(taskId)
        
        return {
            "taskId": taskId,
            "usageRecords": details.get("records", []),
            "totalCost": details.get("total_cost"),
            "breakdown": details.get("breakdown"),
            "billingPeriod": details.get("period"),
        }
    
    def _billing_get_rates(self, gpuType: str = None, **kwargs) -> Dict:
        """获取计费费率"""
        system = self._get_billing_system()
        rates = system["engine"].get_rates(gpuType)
        
        return {
            "gpuRates": rates.get("gpu_rates", {}),
            "networkRate": rates.get("network_rate"),
            "storageRate": rates.get("storage_rate"),
            "utilizationCoefficients": rates.get("utilization_coefficients"),
            "memoryCoefficients": rates.get("memory_coefficients"),
        }
    
    def _billing_estimate_task(
        self,
        gpuType: str,
        estimatedHours: float = None,
        durationHours: float = None,
        expectedUtilization: float = 80,
        expectedMemoryUsage: float = 16,
        **kwargs
    ) -> Dict:
        """估算任务费用"""
        system = self._get_billing_system()
        
        # 兼容两种参数
        hours = durationHours or estimatedHours or 1.0
        
        # 如果 utilization 是小数形式（0.8），转换为百分比（如 80
        utilization = expectedUtilization
        if utilization < 1:
            utilization = utilization * 100
        
        try:
            estimate = system["estimator"].estimate_task_cost(
                gpu_type=gpuType,
                duration_hours=hours,
                expected_utilization=utilization,
                expected_memory_gb=expectedMemoryUsage
            )
            
            return {
                "gpuType": gpuType,
                "estimatedHours": hours,
                "estimatedCost": estimate.get("total_estimated"),
                "minCost": estimate.get("confidence_range", {}).get("low"),
                "maxCost": estimate.get("confidence_range", {}).get("high"),
                "assumptions": estimate.get("assumptions"),
            }
        except Exception as e:
            return self._rpc_internal_error("billing_estimateTask", e, {
                "gpuType": gpuType,
                "estimatedHours": hours,
                "estimatedCost": 0,
            })
    
    # ============== 数据生命周期方法 ==============
    
    def _data_register_asset(
        self,
        assetId: str,
        ownerId: str,
        dataHash: str,
        retentionPolicy: str = "TASK_COMPLETE",
        retentionDays: int = None,
        **kwargs
    ) -> Dict:
        """注册数据资产"""
        system = self._get_data_lifecycle_system()
        
        asset = system["lifecycle_manager"].register_asset(
            asset_id=assetId,
            owner_id=ownerId,
            data_hash=dataHash,
            retention_policy=retentionPolicy,
            retention_days=retentionDays
        )
        
        return {
            "assetId": asset.asset_id,
            "ownerId": asset.owner_id,
            "dataHash": asset.data_hash,
            "retentionPolicy": asset.retention_policy,
            "expiresAt": asset.expires_at,
            "createdAt": asset.created_at,
        }
    
    def _data_request_destruction(self, assetId: str, reason: str = None, **kwargs) -> Dict:
        """请求数据销"""
        system = self._get_data_lifecycle_system()
        
        result = system["lifecycle_manager"].request_destruction(assetId, reason)
        
        return {
            "assetId": assetId,
            "destructionRequested": result.get("success", False),
            "certificateId": result.get("certificate_id"),
            "scheduledAt": result.get("scheduled_at"),
        }
    
    def _data_get_destruction_proof(self, assetId: str, **kwargs) -> Optional[Dict]:
        """获取销毁证"""
        system = self._get_data_lifecycle_system()
        
        cert = system["lifecycle_manager"].get_destruction_certificate(assetId)
        
        if not cert:
            return None
        
        return {
            "certificateId": cert.certificate_id,
            "assetId": cert.asset_id,
            "dataHash": cert.original_data_hash,
            "proofHash": cert.proof_hash,
            "destroyedAt": cert.destroyed_at,
            "destroyedBy": cert.destroyed_by,
            "verifiable": True,
        }
    
    def _data_list_assets(self, ownerId: str, status: str = None, **kwargs) -> Dict:
        """列出数据资产"""
        system = self._get_data_lifecycle_system()
        
        try:
            # lifecycle_manager.assets 字典获取资产
            all_assets = list(system["lifecycle_manager"].assets.values())
            
            # ownerId 筛(user_id)
            assets = [a for a in all_assets if a.user_id == ownerId]
            
            # status 筛
            if status:
                if status.lower() == "destroyed":
                    assets = [a for a in assets if a.destroyed]
                elif status.lower() == "active":
                    assets = [a for a in assets if not a.destroyed]
            
            return {
                "assets": [{
                    "assetId": a.asset_id,
                    "dataHash": a.data_hash,
                    "retentionPolicy": a.retention_policy.value if hasattr(a.retention_policy, 'value') else str(a.retention_policy),
                    "status": "destroyed" if a.destroyed else "active",
                    "expiresAt": a.expires_at,
                    "dataType": a.data_type.value if hasattr(a.data_type, 'value') else str(a.data_type),
                } for a in assets],
                "total": len(assets),
            }
        except Exception as e:
            return {
                "assets": [],
                "total": 0,
                "error": "internal_error",
            }
    
    def _ephemeral_create_session(
        self,
        userId: str,
        taskId: str,
        stages: List[str] = None,
        **kwargs
    ) -> Dict:
        """创建临时会话密钥"""
        system = self._get_data_lifecycle_system()
        
        session = system["session_key_protocol"].create_session(
            user_id=userId,
            task_id=taskId,
            stages=stages or ["input", "processing", "output"]
        )
        
        return {
            "sessionId": session.session_id,
            "userId": userId,
            "taskId": taskId,
            "stages": session.stages,
            "createdAt": session.created_at,
            "expiresAt": session.expires_at,
        }
    
    def _ephemeral_get_session_key(
        self,
        sessionId: str,
        stage: str,
        **kwargs
    ) -> Dict:
        """获取会话密钥"""
        system = self._get_data_lifecycle_system()
        
        key_info = system["session_key_protocol"].get_stage_key(sessionId, stage)
        
        return {
            "sessionId": sessionId,
            "stage": stage,
            "keyId": key_info.get("key_id"),
            "publicKey": key_info.get("public_key"),
            "algorithm": key_info.get("algorithm", "AES-256-GCM"),
            "expiresAt": key_info.get("expires_at"),
        }
    
    def _ephemeral_rotate_key(self, sessionId: str, stage: str, **kwargs) -> Dict:
        """轮换会话密钥"""
        system = self._get_data_lifecycle_system()
        
        result = system["session_key_protocol"].rotate_key(sessionId, stage)
        
        return {
            "sessionId": sessionId,
            "stage": stage,
            "newKeyId": result.get("new_key_id"),
            "previousKeyId": result.get("previous_key_id"),
            "rotatedAt": time.time(),
        }
    
    # ============== P2P 直连方法 ==============
    
    def _p2p_setup_connection(
        self,
        userId: str,
        minerId: str,
        protocol: str = "WEBRTC",
        **kwargs
    ) -> Dict:
        """建立 P2P 连接"""
        service = self._get_p2p_service()
        
        session = service["connection_manager"].setup_connection(
            user_id=userId,
            miner_id=minerId,
            protocol=protocol
        )
        
        return {
            "sessionId": session.session_id,
            "userId": userId,
            "minerId": minerId,
            "protocol": protocol,
            "status": session.status,
            "createdAt": session.created_at,
        }
    
    def _p2p_create_offer(self, sessionId: str, **kwargs) -> Dict:
        """创建连接 Offer"""
        service = self._get_p2p_service()
        
        offer = service["connection_manager"].create_offer(sessionId)
        
        return {
            "sessionId": sessionId,
            "offer": offer.get("sdp"),
            "type": "offer",
            "iceCandidates": offer.get("ice_candidates", []),
        }
    
    def _p2p_create_answer(self, sessionId: str, offer: str, **kwargs) -> Dict:
        """创建连接 Answer"""
        service = self._get_p2p_service()
        
        answer = service["connection_manager"].create_answer(sessionId, offer)
        
        return {
            "sessionId": sessionId,
            "answer": answer.get("sdp"),
            "type": "answer",
            "iceCandidates": answer.get("ice_candidates", []),
        }
    
    def _p2p_get_connection_status(self, sessionId: str, **kwargs) -> Dict:
        """获取连接状"""
        service = self._get_p2p_service()
        
        status = service["connection_manager"].get_connection_status(sessionId)
        
        return {
            "sessionId": sessionId,
            "status": status.get("status"),
            "protocol": status.get("protocol"),
            "latency": status.get("latency_ms"),
            "bandwidth": status.get("bandwidth_mbps"),
            "connectedAt": status.get("connected_at"),
        }
    
    def _p2p_list_connections(self, userId: str = None, minerId: str = None, **kwargs) -> Dict:
        """列出活跃连接"""
        service = self._get_p2p_service()
        
        try:
            # sessions 字典获取连接
            conn_manager = service["connection_manager"]
            sessions = list(conn_manager.sessions.values())
            
            # 按用户筛
            if userId:
                sessions = [s for s in sessions if s.user_id == userId]
            if minerId:
                sessions = [s for s in sessions if s.miner_id == minerId]
            
            return {
                "connections": [{
                    "sessionId": s.session_id,
                    "userId": s.user_id,
                    "minerId": s.miner_id,
                    "status": s.status.value if hasattr(s.status, 'value') else str(s.status),
                    "protocol": s.protocol.value if hasattr(s.protocol, 'value') else str(s.protocol),
                    "createdAt": s.created_at,
                } for s in sessions],
                "total": len(sessions),
            }
        except Exception as e:
            return {
                "connections": [],
                "total": 0,
                "error": "internal_error",
            }
    
    def _p2p_close_connection(self, sessionId: str, **kwargs) -> Dict:
        """关闭 P2P 连接"""
        service = self._get_p2p_service()
        
        result = service["connection_manager"].close_connection(sessionId)
        
        return {
            "sessionId": sessionId,
            "closed": result.get("success", False),
            "closedAt": time.time(),
        }
    
    def _p2p_get_nat_info(self, peerId: str = None, **kwargs) -> Dict:
        """获取 NAT 信息"""
        try:
            service = self._get_p2p_service()
            nat_service = service.get("nat_traversal")
            
            if nat_service and peerId:
                nat_type = nat_service.detect_nat_type(peerId)
                return {
                    "natType": nat_type.value if hasattr(nat_type, 'value') else str(nat_type),
                    "publicIp": None,  # 需STUN 获取
                    "publicPort": None,
                    "stunServer": "stun.l.google.com:19302",
                    "traversalPossible": True,
                }
            
            # 返回默认信息
            return {
                "natType": "unknown",
                "publicIp": None,
                "publicPort": None,
                "stunServer": "stun.l.google.com:19302",
                "traversalPossible": True,
            }
        except Exception as e:
            return {
                "natType": "unknown",
                "error": "internal_error"
            }
    
    # ============== DID 身份方法 ==============
    
    def _did_create(
        self,
        walletAddress: str,
        publicKey: str,
        metadata: Dict = None,
        **kwargs
    ) -> Dict:
        """创建 DID 身份"""
        service = self._get_identity_service()
        
        did = service["did_manager"].create_did(
            wallet_address=walletAddress,
            public_key=publicKey,
            metadata=metadata or {}
        )
        
        return {
            "did": did.did,
            "walletAddress": did.wallet_address,
            "publicKey": did.public_key,
            "createdAt": did.created_at,
            "document": did.to_document(),
        }
    
    def _did_resolve(self, did: str, **kwargs) -> Optional[Dict]:
        """解析 DID"""
        service = self._get_identity_service()
        
        document = service["did_manager"].resolve_did(did)
        
        if not document:
            return None
        
        return {
            "did": document.get("id"),
            "publicKey": document.get("publicKey"),
            "authentication": document.get("authentication"),
            "service": document.get("service"),
            "created": document.get("created"),
            "updated": document.get("updated"),
        }
    
    def _did_bind_wallet(self, did: str, walletAddress: str, signature: str, **kwargs) -> Dict:
        """绑定钱包地址"""
        service = self._get_identity_service()
        
        result = service["did_manager"].bind_wallet(did, walletAddress, signature)
        
        return {
            "did": did,
            "walletAddress": walletAddress,
            "bound": result.get("success", False),
            "boundAt": time.time() if result.get("success") else None,
        }
    
    def _did_issue_credential(
        self,
        issuerDid: str,
        subjectDid: str,
        credentialType: str,
        claims: Dict,
        expiresAt: float = None,
        **kwargs
    ) -> Dict:
        """颁发凭证"""
        service = self._get_identity_service()
        
        credential = service["did_manager"].issue_credential(
            issuer_did=issuerDid,
            subject_did=subjectDid,
            credential_type=credentialType,
            claims=claims,
            expires_at=expiresAt
        )
        
        return {
            "credentialId": credential.credential_id,
            "issuer": credential.issuer,
            "subject": credential.subject,
            "type": credential.credential_type,
            "claims": credential.claims,
            "issuedAt": credential.issued_at,
            "expiresAt": credential.expires_at,
        }
    
    def _did_verify_credential(self, credentialId: str, **kwargs) -> Dict:
        """验证凭证"""
        service = self._get_identity_service()
        
        result = service["did_manager"].verify_credential(credentialId)
        
        return {
            "credentialId": credentialId,
            "valid": result.get("valid", False),
            "issuer": result.get("issuer"),
            "subject": result.get("subject"),
            "expired": result.get("expired", False),
            "verifiedAt": time.time(),
        }
    
    def _did_get_reputation(self, did: str, **kwargs) -> Dict:
        """获取信誉"""
        service = self._get_identity_service()
        
        try:
            # 使用 get_benefits 方法获取信誉信息
            benefits = service["reputation_system"].get_benefits(did)
            
            # 获取 DID binding 以获取更多信
            binding = service["did_manager"].bindings.get(did)
            
            if binding:
                return {
                    "did": did,
                    "score": binding.reputation_score,
                    "tier": binding.reputation_tier.value,
                    "tasksCompleted": binding.total_tasks_completed,
                    "successRate": 1.0,  # 默认
                    "totalEarnings": 0,
                    "penalties": 0,
                }
            else:
                return {
                    "did": did,
                    "score": 0,
                    "tier": "newcomer",
                    "tasksCompleted": 0,
                    "successRate": 0,
                    "message": "DID not found",
                }
        except Exception as e:
            return {
                "did": did,
                "score": 0,
                "tier": "unknown",
                "error": "internal_error",
            }
    
    def _did_get_reputation_tier(self, did: str, **kwargs) -> Dict:
        """获取信誉等级"""
        service = self._get_identity_service()
        
        try:
            # 使用 get_benefits 方法
            benefits_info = service["reputation_system"].get_benefits(did)
            binding = service["did_manager"].bindings.get(did)
            
            if binding:
                current_tier = binding.reputation_tier
                current_score = binding.reputation_score
                
                # 计算下一个等
                tier_thresholds = service["reputation_system"].TIER_THRESHOLDS
                tier_benefits = service["reputation_system"].TIER_BENEFITS
                
                next_tier = None
                required_for_next = None
                progress_to_next = 0
                
                # 找到下一个等
                for tier, threshold in sorted(tier_thresholds.items(), key=lambda x: x[1]):
                    if threshold > current_score:
                        next_tier = tier.value
                        required_for_next = threshold - current_score
                        progress_to_next = current_score / threshold if threshold > 0 else 1.0
                        break
                
                return {
                    "did": did,
                    "currentTier": current_tier.value,
                    "tierBenefits": tier_benefits.get(current_tier, {}),
                    "nextTier": next_tier,
                    "progressToNext": progress_to_next,
                    "requiredForNext": required_for_next,
                }
            else:
                return {
                    "did": did,
                    "currentTier": "newcomer",
                    "tierBenefits": {},
                    "nextTier": "bronze",
                    "progressToNext": 0,
                    "message": "DID not found",
                }
        except Exception as e:
            return {
                "did": did,
                "currentTier": "unknown",
                "error": "internal_error",
            }
    
    def _did_check_sybil_risk(self, did: str = None, walletAddress: str = None, **kwargs) -> Dict:
        """检查女巫风"""
        service = self._get_identity_service()
        
        analysis = service["sybil_detector"].analyze_sybil_risk(did=did)
        
        return {
            "did": did,
            "walletAddress": walletAddress,
            "riskLevel": analysis.risk_level.value if hasattr(analysis.risk_level, 'value') else analysis.risk_level,
            "riskScore": analysis.risk_score,
            "riskFactors": analysis.factors,
            "recommendations": [],
            "analyzedAt": analysis.analyzed_at,
        }
    
    # ============== P2P 任务分发方法 ==============
    
    _p2p_task_distributor = None
    _p2p_compute_node = None
    _p2p_task_message_handler = None
    
    def _get_p2p_task_system(self):
        """获取或创P2P 任务系统"""
        if self._p2p_task_distributor is None:
            try:
                from .p2p_task_distributor import (
                    create_p2p_task_system,
                    NodeRole,
                    P2PTaskDistributor,
                    P2PComputeNode,
                    P2PTaskMessageHandler,
                )
                
                # 创建 P2P 任务系统
                self._p2p_task_distributor, self._p2p_compute_node, self._p2p_task_message_handler = \
                    create_p2p_task_system(
                        node_id=self.node_id,
                        p2p_node=self.p2p_network,
                        role=NodeRole.FULL,
                        log_fn=lambda msg: None,  # 静默日志
                    )
                
                # 如果P2P 网络，设置消息处理器
                if self.p2p_network and hasattr(self.p2p_network, 'set_task_handler'):
                    self.p2p_network.set_task_handler(self._p2p_task_message_handler)
                
                # 启动分发
                self._p2p_task_distributor.start()
                
            except Exception as e:
                # 记录错误而非静默吞没
                import logging as _rpc_logging
                _rpc_logging.getLogger(__name__).error(
                    f"[RPC] P2P 任务分发系统初始化失败: {e}"
                )
        
        return {
            "distributor": self._p2p_task_distributor,
            "compute_node": self._p2p_compute_node,
            "message_handler": self._p2p_task_message_handler,
        }
    
    def _p2p_task_create(
        self,
        taskName: str,
        taskType: str = "compute",
        taskData: str = "",
        config: Dict = None,
        gpuCount: int = 1,
        redundancy: int = 1,
        shardCount: int = 1,
        creatorId: str = "",
        **kwargs
    ) -> Dict:
        """创建 P2P 分布式任"""
        system = self._get_p2p_task_system()
        distributor = system.get("distributor")
        
        if not distributor:
            return {
                "success": False,
                "error": "P2P task system not available",
            }
        
        try:
            import base64
            task_data_bytes = base64.b64decode(taskData) if taskData else b""
        except (ValueError, Exception):
            task_data_bytes = taskData.encode() if taskData else b""
        
        task = distributor.create_task(
            task_name=taskName,
            task_type=taskType,
            task_data=task_data_bytes,
            config=config or {},
            gpu_count=gpuCount,
            redundancy=redundancy,
            shard_count=shardCount,
            creator_id=creatorId or self.node_id,
        )
        
        return {
            "success": True,
            "taskId": task.task_id,
            "taskName": task.task_name,
            "taskType": task.task_type,
            "status": task.status.value,
            "shardCount": len(task.shards),
            "createdAt": task.created_at,
        }
    
    def _p2p_task_distribute(self, taskId: str, **kwargs) -> Dict:
        """分发任务到 P2P 网络"""
        import asyncio
        
        system = self._get_p2p_task_system()
        distributor = system.get("distributor")
        
        if not distributor:
            return {
                "success": False,
                "error": "P2P task system not available",
            }
        
        task = distributor.tasks.get(taskId)
        if not task:
            return {
                "success": False,
                "error": f"Task not found: {taskId}",
            }
        
        # 在新事件循环中执行异步分
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            success = loop.run_until_complete(distributor.distribute_task(taskId))
            loop.close()
        except Exception as e:
            return {
                "success": False,
                "error": "internal_error",
            }
        
        return {
            "success": success,
            "taskId": taskId,
            "status": task.status.value,
            "distributedShards": len(task.shards),
            "availableMiners": len(distributor.available_miners),
            "distributedAt": time.time(),
        }
    
    def _p2p_task_get_status(self, taskId: str, **kwargs) -> Dict:
        """获取 P2P 任务状"""
        system = self._get_p2p_task_system()
        distributor = system.get("distributor")
        
        if not distributor:
            return {"error": "P2P task system not available"}
        
        status = distributor.get_task_status(taskId)
        if not status:
            return {"error": f"Task not found: {taskId}"}
        
        return status
    
    def _p2p_task_get_list(
        self,
        status: str = None,
        limit: int = 20,
        offset: int = 0,
        **kwargs
    ) -> Dict:
        """获取所有 P2P 任务列表"""
        system = self._get_p2p_task_system()
        distributor = system.get("distributor")
        
        if not distributor:
            return {"tasks": [], "total": 0}
        
        all_tasks = distributor.get_all_tasks()
        
        # 状态筛
        if status:
            all_tasks = [t for t in all_tasks if t and t.get("status") == status]
        
        total = len(all_tasks)
        tasks = all_tasks[offset:offset + limit]
        
        return {
            "tasks": tasks,
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    
    def _p2p_task_get_stats(self, **kwargs) -> Dict:
        """获取 P2P 任务分发器统"""
        system = self._get_p2p_task_system()
        distributor = system.get("distributor")
        compute_node = system.get("compute_node")
        
        stats = {
            "distributor": distributor.get_stats() if distributor else {},
            "computeNode": compute_node.get_stats() if compute_node else {},
            "p2pConnected": len(self.p2p_network.peers) if self.p2p_network and hasattr(self.p2p_network, 'peers') else 0,
        }
        
        return stats
    
    def _p2p_task_cancel(self, taskId: str, **kwargs) -> Dict:
        """取消 P2P 任务"""
        system = self._get_p2p_task_system()
        distributor = system.get("distributor")
        
        if not distributor:
            return {"success": False, "error": "P2P task system not available"}
        
        task = distributor.tasks.get(taskId)
        if not task:
            return {"success": False, "error": f"Task not found: {taskId}"}
        
        # 更新状态为失败
        try:
            from .p2p_task_distributor import P2PTaskStatus
        except ImportError:
            return {"success": False, "error": "P2P task distributor module not available"}
        
        task.status = P2PTaskStatus.FAILED
        task.completed_at = time.time()
        
        return {
            "success": True,
            "taskId": taskId,
            "status": task.status.value,
            "cancelledAt": time.time(),
        }
    
    def _p2p_task_register_miner(
        self,
        minerId: str,
        address: str = "",
        sector: str = "MAIN",
        gpuCount: int = 1,
        gpuMemoryGb: int = 8,
        **kwargs
    ) -> Dict:
        """注册矿工节点"""
        system = self._get_p2p_task_system()
        distributor = system.get("distributor")
        
        if not distributor:
            return {"success": False, "error": "P2P task system not available"}
        
        distributor.register_miner(minerId, {
            "address": address,
            "sector": sector,
            "gpu_count": gpuCount,
            "gpu_memory_gb": gpuMemoryGb,
            "registered_at": time.time(),
        })
        
        return {
            "success": True,
            "minerId": minerId,
            "registeredAt": time.time(),
            "totalMiners": len(distributor.available_miners),
        }
    
    def _p2p_task_get_miners(self, **kwargs) -> Dict:
        """获取可用矿工列表"""
        system = self._get_p2p_task_system()
        distributor = system.get("distributor")
        
        if not distributor:
            return {"miners": [], "total": 0}
        
        miners = list(distributor.available_miners.values())
        
        # 也包P2P 网络中的在线节点
        if self.p2p_network and hasattr(self.p2p_network, 'peers'):
            for node_id, peer in self.p2p_network.peers.items():
                if node_id not in [m.get("node_id") for m in miners]:
                    miners.append({
                        "node_id": node_id,
                        "address": peer.peer_info.address if hasattr(peer, 'peer_info') else "",
                        "sector": peer.peer_info.sector if hasattr(peer, 'peer_info') else "MAIN",
                        "is_connected": True,
                    })
        
        return {
            "miners": miners,
            "total": len(miners),
        }
    
    def _p2p_task_get_result(self, taskId: str, **kwargs) -> Dict:
        """获取任务计算结果"""
        system = self._get_p2p_task_system()
        distributor = system.get("distributor")
        
        if not distributor:
            return {"error": "P2P task system not available"}
        
        task = distributor.tasks.get(taskId)
        if not task:
            return {"error": f"Task not found: {taskId}"}
        
        try:
            from .p2p_task_distributor import P2PTaskStatus
        except ImportError:
            return {"error": "P2P task distributor module not available"}
        
        if task.status != P2PTaskStatus.COMPLETED:
            return {
                "taskId": taskId,
                "status": task.status.value,
                "progress": task.progress,
                "ready": False,
            }
        
        return {
            "taskId": taskId,
            "status": task.status.value,
            "ready": True,
            "result": task.aggregated_result,
            "resultHash": task.result_hash,
            "completedAt": task.completed_at,
        }
    
    # ============== DAO 国库治理方法 ==============
    
    def _dao_stake(self, userId: str, amount: float, **kwargs) -> Dict:
        """质押代币"""
        system = self._get_dao_system()
        
        result = system["governance"].stake(userId, amount)
        
        return {
            "userId": userId,
            "stakedAmount": amount,
            "totalStaked": result.get("total_staked"),
            "votingPower": result.get("voting_power"),
            "stakedAt": time.time(),
        }
    
    def _dao_unstake(self, userId: str, amount: float, **kwargs) -> Dict:
        """解除质押"""
        system = self._get_dao_system()
        
        result = system["governance"].unstake(userId, amount)
        
        return {
            "userId": userId,
            "unstakedAmount": amount,
            "remainingStake": result.get("remaining_stake"),
            "unlockTime": result.get("unlock_time"),
            "unstakedAt": time.time(),
        }
    
    def _dao_create_proposal(
        self,
        proposerId: str,
        title: str,
        description: str,
        proposalType: str = "PARAMETER",
        parameters: Dict = None,
        executionData: Dict = None,
        **kwargs
    ) -> Dict:
        """创建治理提案"""
        system = self._get_dao_system()
        
        proposal = system["governance"].create_proposal(
            proposer_id=proposerId,
            title=title,
            description=description,
            proposal_type=proposalType,
            parameters=parameters or {},
            execution_data=executionData
        )
        
        return {
            "proposalId": proposal.proposal_id,
            "proposerId": proposal.proposer_id,
            "title": proposal.title,
            "type": proposal.proposal_type,
            "status": proposal.status.name,
            "votingStartsAt": proposal.voting_starts_at,
            "votingEndsAt": proposal.voting_ends_at,
            "createdAt": proposal.created_at,
        }
    
    def _dao_vote(
        self,
        proposalId: str,
        voterId: str,
        support: bool,
        reason: str = None,
        **kwargs
    ) -> Dict:
        """提案投票"""
        system = self._get_dao_system()
        
        result = system["governance"].vote(
            proposal_id=proposalId,
            voter_id=voterId,
            support=support,
            reason=reason
        )
        
        return {
            "proposalId": proposalId,
            "voterId": voterId,
            "support": support,
            "votingPower": result.get("voting_power"),
            "currentYes": result.get("current_yes"),
            "currentNo": result.get("current_no"),
            "votedAt": time.time(),
        }
    
    def _dao_execute_proposal(self, proposalId: str, **kwargs) -> Dict:
        """执行提案"""
        system = self._get_dao_system()
        
        result = system["governance"].execute_proposal(proposalId)
        
        return {
            "proposalId": proposalId,
            "executed": result.get("success", False),
            "executionResult": result.get("result"),
            "error": result.get("error"),
            "executedAt": time.time() if result.get("success") else None,
        }
    
    def _dao_get_proposal_status(self, proposalId: str, **kwargs) -> Optional[Dict]:
        """获取提案状"""
        system = self._get_dao_system()
        
        proposal = system["governance"].get_proposal(proposalId)
        
        if not proposal:
            return None
        
        return {
            "proposalId": proposal.proposal_id,
            "title": proposal.title,
            "status": proposal.status.name,
            "yesVotes": proposal.yes_votes,
            "noVotes": proposal.no_votes,
            "totalVotes": proposal.yes_votes + proposal.no_votes,
            "quorum": proposal.quorum,
            "quorumReached": (proposal.yes_votes + proposal.no_votes) >= proposal.quorum,
            "votingEndsAt": proposal.voting_ends_at,
            "timeRemaining": max(0, proposal.voting_ends_at - time.time()),
        }
    
    def _dao_list_proposals(
        self,
        status: str = None,
        proposalType: str = None,
        limit: int = 50,
        **kwargs
    ) -> Dict:
        """列出所有提"""
        system = self._get_dao_system()
        
        try:
            # 获取活跃提案
            proposals = system["governance"].get_active_proposals()
            
            # 如果指定status，可以筛选（当前只返active
            if status and status.upper() != "ACTIVE":
                # proposals 字典获取所有提案并筛
                all_proposals = list(system["governance"].proposals.values())
                proposals = [
                    p.to_dict() for p in all_proposals
                    if status.upper() == p.status.name
                ][:limit]
            else:
                proposals = proposals[:limit]
            
            return {
                "proposals": [{
                    "proposalId": p.get("proposal_id", ""),
                    "title": p.get("title", ""),
                    "type": p.get("proposal_type", ""),
                    "status": p.get("status", ""),
                    "yesVotes": p.get("votes_for", 0),
                    "noVotes": p.get("votes_against", 0),
                    "createdAt": p.get("created_at", 0),
                } for p in proposals],
                "total": len(proposals),
            }
        except Exception as e:
            return self._rpc_internal_error("dao_listProposals", e, {
                "proposals": [],
                "total": 0,
            })
    
    def _dao_get_treasury(self, **kwargs) -> Dict:
        """获取国库信息"""
        system = self._get_dao_system()
        
        try:
            # TreasuryManager.get_balance() 返回 treasury.to_dict()
            treasury = system["treasury"].get_balance()
            
            return {
                "balance": treasury.get("balance", 0),
                "totalDeposits": treasury.get("total_income", 0),
                "totalWithdrawals": treasury.get("total_spent", 0),
                "pendingWithdrawals": treasury.get("locked_balance", 0),
                "signers": treasury.get("multisig_signers", []),
                "requiredSignatures": treasury.get("multisig_threshold", 3),
                "recentTransactions": system["treasury"].get_transactions(10),
            }
        except Exception as e:
            return self._rpc_internal_error("dao_getTreasury", e, {
                "balance": 0,
                "totalDeposits": 0,
                "totalWithdrawals": 0,
                "pendingWithdrawals": 0,
                "signers": [],
                "requiredSignatures": 0,
                "recentTransactions": [],
            })
    
    def _dao_get_treasury_config(self, **kwargs) -> Dict:
        """获取财库配置（税率、分配规则等）"""
        try:
            result = {
                "blockReward": {
                    "treasuryRate": 0.03,
                    "minerRate": 0.97,
                    "description": "区块奖励: 97% 矿工 + 3% 财库",
                },
                "transactionFee": {
                    "burnRate": 0.005,
                    "minerRate": 0.003,
                    "foundationRate": 0.002,
                    "totalRate": 0.01,
                    "description": "交易 0.5% 销+ 0.3% 矿工 + 0.2% 基金会多",
                },
            }
            
            # 从共识引擎读取实际配
            if hasattr(self, 'consensus_engine') and self.consensus_engine:
                engine = self.consensus_engine
                rate = engine.reward_calculator.treasury_rate
                result["blockReward"]["treasuryRate"] = rate
                result["blockReward"]["minerRate"] = 1.0 - rate
                result["blockReward"]["description"] = (
                    f"区块奖励: {(1-rate)*100:.0f}% 矿工 + {rate*100:.0f}% 财库"
                )
            
            # TreasuryManager 读交易费配置
            try:
                from core.treasury_manager import TreasuryManager
                result["transactionFee"]["platformFeeRate"] = TreasuryManager.PLATFORM_FEE_RATE
                result["transactionFee"]["networkFeeRate"] = TreasuryManager.NETWORK_FEE_RATE
                result["transactionFee"]["treasuryContribution"] = TreasuryManager.TREASURY_CONTRIBUTION
            except Exception:
                pass
            
            return result
        except Exception as e:
            return self._rpc_internal_error("dao_getTreasuryConfig", e, {
                "blockReward": {
                    "treasuryRate": 0.03,
                    "minerRate": 0.97,
                    "description": "区块奖励: 97% 矿工 + 3% 财库",
                },
                "transactionFee": {
                    "burnRate": 0.005,
                    "minerRate": 0.003,
                    "foundationRate": 0.002,
                    "totalRate": 0.01,
                    "description": "交易 0.5% 销+ 0.3% 矿工 + 0.2% 基金会多",
                },
            })
    
    def _dao_set_treasury_rate(self, rate: float = None, **kwargs) -> Dict:
        """
        修改财库税率（管理员接口
        
        参数:
            rate: 新税(0.01 ~ 0.20, 1% ~ 20%)
        """
        if rate is None:
            return {"success": False, "message": "请提供 rate 参数"}
        
        rate = float(rate)
        if rate < 0.01 or rate > 0.20:
            return {
                "success": False,
                "message": f"税率必须1% ~ 20% 之间，当前输 {rate*100:.1f}%"
            }
        
        try:
            old_rate = 0.03
            if hasattr(self, 'consensus_engine') and self.consensus_engine:
                old_rate = self.consensus_engine.reward_calculator.treasury_rate
                self.consensus_engine.reward_calculator.treasury_rate = rate
            
            return {
                "success": True,
                "oldRate": old_rate,
                "newRate": rate,
                "message": f"财库税率已从 {old_rate*100:.1f}% 改为 {rate*100:.1f}%"
            }
        except Exception as e:
            return self._rpc_internal_error("dao_setTreasuryRate", e, {
                "success": False,
                "message": "operation_failed",
            })
    
    # ============== 国库透明度 ==============
    
    def _dao_get_treasury_limits(self, **kwargs) -> Dict:
        """获取国库补偿限制及当前使用情况（对用户完全透明）
        
        让用户清楚知道国库补偿是有限的，不是无穷借贷。
        """
        try:
            system = self._get_dao_system()
            governance = system.get("governance")
            treasury_mgr = governance.treasury if governance else None
            
            if not treasury_mgr:
                return {"error": "Treasury not available"}
            
            with treasury_mgr._lock:
                treasury_mgr._reset_daily_if_needed()
                
                # 基本余额信息
                balance = treasury_mgr.treasury.balance
                locked = treasury_mgr.treasury.locked_balance
                available = balance - locked
                
                # 每日使用情况
                daily_used = treasury_mgr._daily_compensate_total
                daily_cap = treasury_mgr.DAILY_COMPENSATE_CAP
                daily_remaining = max(0, daily_cap - daily_used)
                
                # 待清偿欠条
                pending_count = len(treasury_mgr.pending_debts)
                pending_total = sum(d["amount"] for d in treasury_mgr.pending_debts)
                
                # 各矿工当日使用情况（不泄露其他矿工地址，只返回汇总）
                active_miners_today = len(treasury_mgr._per_miner_daily)
                
            return {
                "treasury": {
                    "balance": round(balance, 4),
                    "available": round(available, 4),
                    "locked": round(locked, 4),
                },
                "compensateLimits": {
                    "perTransaction": treasury_mgr.AUTO_COMPENSATE_MAX,
                    "perMinerDaily": treasury_mgr.PER_MINER_DAILY_CAP,
                    "perMinerDailyCount": treasury_mgr.PER_MINER_DAILY_COUNT,
                    "dailyTotal": daily_cap,
                    "debtRepayFirst": True,
                    "description": (
                        f"自动补偿限制: 单笔≤{treasury_mgr.AUTO_COMPENSATE_MAX} MAIN, "
                        f"每矿工每日≤{treasury_mgr.PER_MINER_DAILY_CAP} MAIN / {treasury_mgr.PER_MINER_DAILY_COUNT}次, "
                        f"全网每日≤{daily_cap} MAIN。"
                        f"矿工必须偿还已有欠条后才能获得下一笔补偿。"
                        f"超限需通过 DAO 多签提案。"
                    ),
                },
                "dailyUsage": {
                    "date": treasury_mgr._daily_compensate_date,
                    "used": round(daily_used, 4),
                    "remaining": round(daily_remaining, 4),
                    "activeMiners": active_miners_today,
                },
                "pendingDebts": {
                    "count": pending_count,
                    "totalAmount": round(pending_total, 4),
                    "note": "欠条将在国库收到新收入时按先进先出顺序自动清偿" if pending_count > 0 else "无待清偿欠条",
                },
                "incomeStreams": {
                    "blockReward": "每个区块奖励的 3% 进入国库",
                    "taskFees": "算力市场结算费的 5% 进入国库",
                    "penalties": "仲裁罚没金进入国库",
                },
            }
        except Exception as e:
            return self._rpc_internal_error("dao_getTreasuryLimits", e, {
                "treasury": {
                    "balance": 0,
                    "available": 0,
                    "locked": 0,
                },
                "compensateLimits": {},
                "dailyUsage": {},
                "pendingDebts": {
                    "count": 0,
                    "totalAmount": 0,
                },
                "incomeStreams": {},
            })
    
    # ============== 板块动态管理 ==============
    
    def _sector_get_list(self, **kwargs) -> Dict:
        """获取所有板块及其状态"""
        try:
            from core.sector_coin import get_sector_registry
            registry = get_sector_registry()
            sectors = registry.get_all_sectors()
            
            # 补充矿工活跃度信息
            for s in sectors:
                count = 0
                for miner in self.registered_miners.values():
                    miner_sectors = miner.get("sectors", [])
                    if s["name"] in miner_sectors:
                        count += 1
                s["active_miners"] = count
                
                # 标记不活跃：活跃状态但 0 矿工
                if s["active"] and count == 0 and not s["builtin"]:
                    s["inactive_warning"] = True
            
            return {
                "sectors": sectors,
                "activeSectors": [s["name"] for s in sectors if s["active"]],
                "totalActive": sum(1 for s in sectors if s["active"]),
                "totalInactive": sum(1 for s in sectors if not s["active"]),
            }
        except Exception as e:
            return self._rpc_internal_error("sector_getList", e, {
                "sectors": [],
                "activeSectors": [],
                "totalActive": 0,
                "totalInactive": 0,
            })
    
    def _sector_add(self, proposerId: str = "", name: str = "", base_reward: float = None,
                    exchange_rate: float = None, max_supply: float = None,
                    gpu_models: list = None, **kwargs) -> Dict:
        """提议新增板块（需社区投票通过）
        
        不再直接执行，而是创建 DAO 提案，社区投票通过后自动执行。
        
        Args:
            proposerId: 提案人 ID（需质押足够）
            name: 板块名称（如 "RTX5090", "H200"）
            base_reward: 每块基础奖励
            exchange_rate: 兑换 MAIN 比率
            max_supply: 最大供应量
            gpu_models: 关联 GPU 型号列表
        """
        if not name or not isinstance(name, str):
            return {"success": False, "error": "板块名称不能为空"}
        if not proposerId:
            return {"success": False, "error": "需提供 proposerId 发起提案"}
        
        import re
        if not re.match(r'^[A-Za-z0-9_]+$', name):
            return {"success": False, "error": "板块名称只允许字母、数字和下划线"}
        
        name = name.upper()
        
        try:
            from core.dao_treasury import ProposalType
            system = self._get_dao_system()
            governance = system["governance"]
            
            proposal = governance.create_proposal(
                proposer=proposerId,
                proposal_type=ProposalType.SECTOR_ADD,
                title=f"新增板块: {name}",
                description=f"提议新增 GPU 板块 {name}，基础奖励={base_reward}, 兑换率={exchange_rate}, 最大供应={max_supply}",
                execution_payload={
                    "sector_name": name,
                    "base_reward": float(base_reward) if base_reward else None,
                    "exchange_rate": float(exchange_rate) if exchange_rate else None,
                    "max_supply": float(max_supply) if max_supply else None,
                    "gpu_models": gpu_models or [],
                },
            )
            return {
                "success": True,
                "proposalId": proposal.proposal_id,
                "status": "voting",
                "sector": name,
                "message": f"板块 {name} 新增提案已创建，需社区投票通过后执行",
                "votingEndsAt": proposal.voting_ends,
            }
        except ValueError:
            return {"success": False, "error": "invalid_sector_parameters"}
        except Exception as e:
            return self._rpc_internal_error("sector_add", e, {
                "success": False,
            })
    
    def _sector_deactivate(self, proposerId: str = "", name: str = "", **kwargs) -> Dict:
        """提议废除板块（需社区投票通过）
        
        不再直接执行，而是创建 DAO 提案，社区投票通过后自动执行。
        """
        if not name:
            return {"success": False, "error": "板块名称不能为空"}
        if not proposerId:
            return {"success": False, "error": "需提供 proposerId 发起提案"}
        
        name = name.upper()
        
        try:
            from core.dao_treasury import ProposalType
            system = self._get_dao_system()
            governance = system["governance"]
            
            proposal = governance.create_proposal(
                proposer=proposerId,
                proposal_type=ProposalType.SECTOR_DEACTIVATE,
                title=f"废除板块: {name}",
                description=f"提议废除不活跃板块 {name}",
                execution_payload={"sector_name": name},
            )
            return {
                "success": True,
                "proposalId": proposal.proposal_id,
                "status": "voting",
                "sector": name,
                "message": f"板块 {name} 废除提案已创建，需社区投票通过后执行",
                "votingEndsAt": proposal.voting_ends,
            }
        except ValueError:
            return {"success": False, "error": "invalid_sector_parameters"}
        except Exception as e:
            return self._rpc_internal_error("sector_deactivate", e, {
                "success": False,
            })
    
    def _dao_get_treasury_report(self, **kwargs) -> Dict:
        """获取财库透明度报"""
        try:
            from core.treasury_manager import TreasuryManager
            manager = TreasuryManager()
            report = manager.generate_report()
            return {"success": True, "report": report}
        except Exception as e:
            # 如果还没TreasuryManager 数据，返回基础信息
            result = {
                "success": True,
                "report": {
                    "generatedAt": time.time(),
                    "taxRate": 0.03,
                    "description": "97% 矿工 + 3% 财库",
                    "controlMechanism": {
                        "proposalSystem": "社区提案投票决定资金使用",
                        "multisig": "大额支出需多签审批 (3/5)",
                        "transparency": "所有资金流向链上可",
                        "maxSingleProposal": "不超过财库余额的 10%",
                    }
                }
            }
            if hasattr(self, 'consensus_engine') and self.consensus_engine:
                rate = self.consensus_engine.reward_calculator.treasury_rate
                result["report"]["taxRate"] = rate
                result["report"]["description"] = f"{(1-rate)*100:.0f}% 矿工 + {rate*100:.0f}% 财库"
            return result
    
    def _dao_create_treasury_proposal(self, title: str = "",
                                       description: str = "",
                                       amount: float = 0,
                                       recipient: str = "",
                                       proposer: str = "", **kwargs) -> Dict:
        """创建财库资金使用提案"""
        if not title or not description or amount <= 0:
            return {
                "success": False,
                "message": "请提供 title, description, amount 参数"
            }
        
        try:
            from core.treasury_manager import TreasuryManager
            manager = TreasuryManager()
            success, msg, proposal = manager.create_proposal(
                title=title,
                description=description,
                proposer_address=proposer or "anonymous",
                requested_amount=amount,
                recipient_address=recipient or proposer or "anonymous",
            )
            
            if success and proposal:
                return {
                    "success": True,
                    "proposalId": proposal.proposal_id,
                    "message": msg,
                }
            return {"success": False, "message": msg}
        except Exception as e:
            return self._rpc_internal_error("dao_createTreasuryProposal", e, {
                "success": False,
                "message": "operation_failed",
            })
    
    def _dao_treasury_withdraw(self, proposalId: str = "",
                                amount: float = 0,
                                recipient: str = "", **kwargs) -> Dict:
        """
        财库提款（需提案通过或管理员多签
        
        控制方式:
        1. 提案通过后自动执
        2. 紧急提款需 3/5 多签
        3. 单笔不超过财库余10%
        """
        if not proposalId:
            return {
                "success": False,
                "message": "请提供已通过proposalId",
                "hint": "使用 dao_createTreasuryProposal 创建提案，投票通过后使用此接口提款"
            }
        
        try:
            from core.treasury_manager import TreasuryManager
            manager = TreasuryManager()
            proposal = manager.get_proposal(proposalId)
            
            if not proposal:
                return {"success": False, "message": "提案不存在"}
            
            if proposal.status.value != "passed":
                return {
                    "success": False,
                    "message": f"提案状态为 {proposal.status.value}，需passed 才能提款"
                }
            
            # 执行提款
            success, msg = manager.execute_proposal(proposalId)
            return {"success": success, "message": msg}
        except Exception as e:
            return self._rpc_internal_error("dao_treasuryWithdraw", e, {
                "success": False,
                "message": "operation_failed",
            })
    
    def _dao_get_governance_params(self, **kwargs) -> Dict:
        """获取治理参数"""
        system = self._get_dao_system()
        
        try:
            # 使用 get_governance_config 方法
            params = system["governance"].get_governance_config()
            
            return {
                "votingPeriod": params.get("voting_period_days", 7) * 86400,  # 转换为秒
                "quorumPercentage": params.get("quorum_percent", 10),
                "approvalThreshold": params.get("approval_threshold", 66),
                "proposalDeposit": params.get("min_stake_to_propose", 1000),
                "executionDelay": params.get("execution_delay_days", 2) * 86400,
                "emergencyVotingPeriod": 24 * 3600,  # 24 小时
                "emergencyThreshold": 75,
            }
        except Exception as e:
            return self._rpc_internal_error("dao_getGovernanceParams", e, {
                "votingPeriod": 7 * 86400,
                "quorumPercentage": 10,
                "approvalThreshold": 66,
                "proposalDeposit": 1000,
                "executionDelay": 2 * 86400,
                "emergencyVotingPeriod": 24 * 3600,
                "emergencyThreshold": 75,
            })
    
    def _dao_get_staking_info(self, userId: str, **kwargs) -> Dict:
        """获取质押信息"""
        system = self._get_dao_system()
        
        try:
            # DAOGovernance 使用 stakes 字典存储质押信息
            governance = system["governance"]
            staked_amount = governance.stakes.get(userId, 0)
            voting_power = governance.get_voting_power(userId)
            
            return {
                "userId": userId,
                "stakedAmount": staked_amount,
                "votingPower": voting_power,
                "lockedUntil": None,
                "pendingRewards": 0,
                "delegatedTo": None,
                "delegatedFrom": [],
            }
        except Exception as e:
            return self._rpc_internal_error("dao_getStakingInfo", e, {
                "userId": userId,
                "stakedAmount": 0,
                "votingPower": 0,
                "lockedUntil": None,
                "pendingRewards": 0,
                "delegatedTo": None,
                "delegatedFrom": [],
            })
    
    # ========================================
    # Phase 10: 主网上线准备接口实现
    # ========================================
    
    def _get_message_broker(self):
        """获取消息队列管理"""
        from core.message_queue import get_message_broker, get_event_bus, get_task_queue
        return {
            "broker": get_message_broker(),
            "event_bus": get_event_bus(),
            "task_queue": get_task_queue(),
        }
    
    def _get_data_redundancy(self):
        """获取数据冗余管理"""
        from core.data_redundancy import get_data_redundancy_manager
        return get_data_redundancy_manager()
    
    def _get_load_test_engine(self):
        """获取负载测试引擎"""
        from core.load_testing import get_load_test_engine
        return get_load_test_engine()
    
    def _get_zk_manager(self):
        """获取 ZK 验证管理"""
        from core.zk_verification import get_zk_verification_manager
        return get_zk_verification_manager()
    
    def _get_attack_prevention(self):
        """获取攻击防范管理"""
        from core.attack_prevention import get_attack_prevention_manager
        return get_attack_prevention_manager()
    
    def _get_contract_audit(self):
        """获取智能合约审计管理"""
        from core.smart_contract_audit import get_smart_contract_audit_manager
        return get_smart_contract_audit_manager()
    
    def _get_revenue_tracking(self):
        """获取收益追踪管理"""
        from core.revenue_tracking import get_revenue_tracking_manager
        return get_revenue_tracking_manager()
    
    def _get_mainnet_monitor(self):
        """获取主网监控"""
        from core.mainnet_monitor import get_mainnet_monitor
        return get_mainnet_monitor()
    
    def _get_sdk_api_manager(self):
        """获取 SDK API 管理"""
        from core.sdk_api import get_sdk_api_manager
        return get_sdk_api_manager()
    
    # === 消息队列方法 ===
    
    def _mq_publish(self, queue: str, message: Dict, priority: int = 5, **kwargs) -> Dict:
        """发布消息到队列"""
        mq = self._get_message_broker()
        broker = mq["broker"]
        
        msg_id = broker.publish(queue, message, priority=priority)
        
        return {
            "success": True,
            "messageId": msg_id,
            "queue": queue,
        }
    
    def _mq_subscribe(self, queue: str, subscriberId: str, **kwargs) -> Dict:
        """订阅消息队列"""
        mq = self._get_message_broker()
        broker = mq["broker"]
        
        broker.subscribe(queue, subscriberId, lambda msg: None)
        
        return {
            "success": True,
            "queue": queue,
            "subscriberId": subscriberId,
        }
    
    def _mq_get_queue_stats(self, queue: str = None, **kwargs) -> Dict:
        """获取队列统计信息"""
        mq = self._get_message_broker()
        broker = mq["broker"]
        
        # 如果指定了队列名，获取该队列统计
        if queue:
            stats = broker.get_queue_stats(queue)
            if stats:
                return {
                    "queueName": queue,
                    "messageCount": stats.get("message_count", 0),
                    "consumerCount": stats.get("consumer_count", 0),
                    "durable": stats.get("durable", False),
                }
            return {"error": f"Queue {queue} not found"}
        
        # 返回所有队列的汇总信
        return {
            "totalQueues": len(broker.queues),
            "totalMessages": sum(len(q.messages) for q in broker.queues.values()),
            "activeSubscribers": len(broker.consumers),
        }
    
    def _mq_emit_event(self, eventType: str, data: Dict, **kwargs) -> Dict:
        """发送事件"""
        mq = self._get_message_broker()
        event_bus = mq["event_bus"]
        
        event_bus.publish(eventType, data)
        
        return {
            "success": True,
            "eventType": eventType,
        }
    
    # === 数据冗余方法 ===
    
    def _redundancy_store_data(self, data: str, ownerId: str, tags: List[str] = None, **kwargs) -> Dict:
        """存储数据（带冗余）"""
        manager = self._get_data_redundancy()
        
        obj = manager.store_data(data.encode() if isinstance(data, str) else data, ownerId, tags or [])
        
        return {
            "success": True,
            "objectId": obj.object_id,
            "ipfsHash": obj.ipfs_hash,
            "shardCount": len(obj.shards),
            "redundancyLevel": obj.redundancy_level,
        }
    
    def _redundancy_retrieve_data(self, objectId: str, **kwargs) -> Dict:
        """检索数据"""
        manager = self._get_data_redundancy()
        
        data = manager.retrieve_data(objectId)
        
        if data:
            return {
                "success": True,
                "objectId": objectId,
                "data": data.decode() if isinstance(data, bytes) else data,
            }
        return {
            "success": False,
            "error": "Data not found",
        }
    
    def _redundancy_create_backup(self, backupType: str = "full", **kwargs) -> Dict:
        """创建备份"""
        from core.data_redundancy import BackupType
        manager = self._get_data_redundancy()
        
        type_map = {e.value: e for e in BackupType}
        backup_type_enum = type_map.get(backupType, BackupType.FULL)
        job = manager.create_backup(backup_type=backup_type_enum)
        
        return {
            "success": True,
            "backupId": job.job_id,
            "type": backupType,
            "createdAt": time.time(),
        }
    
    def _redundancy_get_stats(self, **kwargs) -> Dict:
        """获取冗余统计"""
        manager = self._get_data_redundancy()
        
        stats = manager.get_storage_stats()
        
        return {
            "totalObjects": stats.get("total_objects", 0),
            "totalShards": stats.get("total_shards", 0),
            "storageUsed": stats.get("storage_used", 0),
            "redundancyRatio": stats.get("redundancy_ratio", 1.5),
            "backupCount": stats.get("backup_count", 0),
        }
    
    # === 负载测试方法 ===
    
    def _load_test_run(self, scenarioName: str, concurrentUsers: int = 100, duration: int = 60, **kwargs) -> Dict:
        """运行负载测试场景"""
        from core.load_testing import LoadProfile
        engine = self._get_load_test_engine()
        
        profile = LoadProfile(
            max_users=concurrentUsers,
            duration_seconds=duration,
        )
        report = engine.run_test(
            name=scenarioName,
            scenario_names=[scenarioName],
            load_profile=profile,
        )
        
        return {
            "success": True,
            "scenarioName": scenarioName,
            "testId": report.test_id,
            "status": report.status.value if hasattr(report.status, 'value') else str(report.status),
        }
    
    def _load_test_get_results(self, testId: str, **kwargs) -> Dict:
        """获取测试结果"""
        engine = self._get_load_test_engine()
        
        try:
            # 使用 get_test_status 方法
            result = engine.get_test_status(testId)
            
            if result:
                return {
                    "testId": testId,
                    "status": result.get("status", "unknown"),
                    "totalRequests": result.get("total_requests", 0),
                    "successRate": result.get("success_rate", 0),
                    "avgLatency": result.get("avg_latency", 0),
                    "p99Latency": result.get("p99_latency", 0),
                    "throughput": result.get("throughput", 0),
                }
            else:
                # 尝试reports 获取
                report = engine.reports.get(testId)
                if report:
                    return {
                        "testId": testId,
                        "status": report.status.value if hasattr(report.status, 'value') else "completed",
                        "totalRequests": report.total_requests,
                        "successRate": report.success_rate,
                        "avgLatency": report.performance.avg_latency_ms if report.performance else 0,
                        "p99Latency": report.performance.p99_latency_ms if report.performance else 0,
                        "throughput": report.performance.throughput_rps if report.performance else 0,
                    }
                else:
                    return {
                        "testId": testId,
                        "status": "not_found",
                        "totalRequests": 0,
                        "successRate": 0,
                    }
        except Exception as e:
            return self._rpc_internal_error("load_test_getResults", e, {
                "testId": testId,
                "status": "error",
                "totalRequests": 0,
                "successRate": 0,
                "avgLatency": 0,
                "p99Latency": 0,
                "throughput": 0,
            })
    
    def _load_test_get_metrics(self, **kwargs) -> Dict:
        """获取性能指标"""
        engine = self._get_load_test_engine()
        
        metrics = engine.get_realtime_metrics()
        
        return {
            "currentTPS": metrics.get("current_tps", 0),
            "avgResponseTime": metrics.get("avg_response_time", 0),
            "activeConnections": metrics.get("active_connections", 0),
            "errorRate": metrics.get("error_rate", 0),
        }
    
    # === ZK 验证方法 ===
    
    def _zk_generate_proof(self, proofType: str, taskId: str, witness: Dict, **kwargs) -> Dict:
        """生成零知识证"""
        manager = self._get_zk_manager()
        
        proof = manager.generate_proof(proofType, taskId, witness)
        
        return {
            "success": True,
            "proofId": proof.proof_id,
            "proofType": proof.proof_type.value if hasattr(proof.proof_type, 'value') else proofType,
            "proofHash": proof.proof_hash,
            "createdAt": proof.created_at,
        }
    
    def _zk_verify_proof(self, proofId: str, **kwargs) -> Dict:
        """验证零知识证"""
        manager = self._get_zk_manager()
        
        result = manager.verify_proof(proofId)
        
        return {
            "proofId": proofId,
            "valid": result.get("valid", False),
            "verifiedAt": result.get("verified_at"),
            "verifierCount": result.get("verifier_count", 0),
        }
    
    def _zk_get_proof_stats(self, **kwargs) -> Dict:
        """获取证明统计"""
        manager = self._get_zk_manager()
        
        stats = manager.get_verification_stats()
        
        return {
            "totalProofs": stats.get("total_proofs", 0),
            "verifiedProofs": stats.get("verified_proofs", 0),
            "failedProofs": stats.get("failed_proofs", 0),
            "avgVerificationTime": stats.get("avg_verification_time", 0),
        }
    
    # === 攻击防范方法 ===
    
    def _security_check_request(self, sourceIp: str, requestType: str = "api", **kwargs) -> Dict:
        """检查请求安全"""
        manager = self._get_attack_prevention()
        
        action, reason = manager.process_request(source_ip=sourceIp, endpoint=requestType)
        
        allowed = action.value in ("allow", "throttle")
        throttled = action.value == "throttle"
        return {
            "allowed": allowed,
            "action": action.value,
            "throttled": throttled,
            "reason": reason,
        }
    
    def _security_report_threat(self, threatType: str, sourceIp: str, details: Dict = None, **kwargs) -> Dict:
        """报告威胁"""
        manager = self._get_attack_prevention()
        
        report_id = manager.report_threat(threatType, sourceIp, details or {})
        
        return {
            "success": True,
            "reportId": report_id,
            "threatType": threatType,
        }
    
    def _security_get_stats(self, **kwargs) -> Dict:
        """获取安全统计"""
        manager = self._get_attack_prevention()
        
        stats = manager.get_threat_status()
        
        return {
            "blockedRequests": stats.get("blocked_requests", 0),
            "ddosAttempts": stats.get("ddos_attempts", 0),
            "sybilDetections": stats.get("sybil_detections", 0),
            "activeBans": stats.get("active_bans", 0),
            "threatLevel": stats.get("threat_level", "low"),
        }
    
    def _security_check_sybil(self, addresses: List[str] = None, address: str = None, **kwargs) -> Dict:
        """检Sybil 攻击"""
        try:
            manager = self._get_attack_prevention()
            
            # 支持单个地址或地址列表
            addr_list = addresses or ([address] if address else [])
            
            if hasattr(manager, 'check_sybil'):
                result = manager.check_sybil(addr_list)
                return {
                    "isSybil": result.get("is_sybil", False),
                    "clusterSize": result.get("cluster_size", 0),
                    "confidence": result.get("confidence", 0),
                    "relatedAddresses": result.get("related_addresses", []),
                }
            
            # 返回默认结果
            return {
                "isSybil": False,
                "clusterSize": 1,
                "confidence": 0,
                "relatedAddresses": [],
            }
        except Exception as e:
            return self._rpc_internal_error("security_checkSybil", e, {
                "isSybil": False,
                "clusterSize": 0,
                "confidence": 0,
                "relatedAddresses": [],
            })
    
    # === 智能合约审计方法 ===
    
    def _audit_submit_contract(self, contractCode: str, contractType: str = "settlement", **kwargs) -> Dict:
        """提交合约审计"""
        manager = self._get_contract_audit()
        
        audit_id = manager.submit_audit(contractCode, contractType)
        
        return {
            "success": True,
            "auditId": audit_id,
            "status": "pending",
        }
    
    def _audit_get_report(self, auditId: str, **kwargs) -> Dict:
        """获取审计报告"""
        manager = self._get_contract_audit()
        
        try:
            # 使用 auditor.reports 获取报告
            report = manager.auditor.reports.get(auditId)
            
            if report:
                return {
                    "auditId": auditId,
                    "status": "completed" if report.passed else "failed",
                    "vulnerabilities": [v.to_dict() for v in report.vulnerabilities],
                    "riskLevel": report.risk_level.value if hasattr(report.risk_level, 'value') else report.risk_level,
                    "recommendations": report.recommendations,
                    "score": report.score,
                    "passed": report.passed,
                }
            else:
                return {
                    "auditId": auditId,
                    "status": "not_found",
                    "vulnerabilities": [],
                    "riskLevel": "unknown",
                    "recommendations": [],
                }
        except Exception as e:
            return self._rpc_internal_error("audit_getReport", e, {
                "auditId": auditId,
                "status": "error",
                "vulnerabilities": [],
                "riskLevel": "unknown",
                "recommendations": [],
                "score": 0,
                "passed": False,
            })
    
    def _audit_auto_settle(self, taskId: str, minerId: str, amount: int, **kwargs) -> Dict:
        """自动结算"""
        manager = self._get_contract_audit()
        
        result = manager.auto_settle(taskId, minerId, amount)
        
        return {
            "success": result.get("success", False),
            "settlementId": result.get("settlement_id"),
            "taskId": taskId,
            "amount": amount,
            "timestamp": result.get("timestamp"),
        }
    
    def _audit_get_settlement_history(self, userId: str = None, minerId: str = None, **kwargs) -> Dict:
        """获取结算历史"""
        manager = self._get_contract_audit()
        
        try:
            # settlement_engine 获取结算历史
            settlements = list(manager.settlement_engine.settlements.values())
            
            # 按用户筛
            if userId:
                settlements = [s for s in settlements if s.payer == userId or s.payee == userId]
            if minerId:
                settlements = [s for s in settlements if s.payee == minerId]
            
            history = [{
                "settlementId": s.settlement_id,
                "taskId": s.task_id,
                "payer": s.payer,
                "payee": s.payee,
                "amount": s.total_amount,
                "status": s.status.value if hasattr(s.status, 'value') else s.status,
                "createdAt": s.created_at,
            } for s in settlements]
            
            return {
                "settlements": history,
                "totalCount": len(history),
            }
        except Exception as e:
            return self._rpc_internal_error("audit_getSettlementHistory", e, {
                "settlements": [],
                "totalCount": 0,
            })
    
    # === 收益追踪方法 ===
    
    def _revenue_record_earning(self, minerId: str, amount: int, source: str, taskId: str = None, **kwargs) -> Dict:
        """记录收益"""
        manager = self._get_revenue_tracking()
        
        record_id = manager.record_earning(minerId, amount, source, task_id=taskId)
        
        return {
            "success": True,
            "recordId": record_id,
            "minerId": minerId,
            "amount": amount,
        }
    
    def _revenue_get_miner_stats(self, minerId: str, period: str = "day", **kwargs) -> Dict:
        """获取矿工收益统计"""
        manager = self._get_revenue_tracking()
        
        try:
            stats = manager.get_miner_stats(minerId)
            
            if stats:
                return {
                    "minerId": minerId,
                    "period": period,
                    "totalEarnings": stats.total_revenue,
                    "taskCount": stats.task_count,
                    "avgEarningPerTask": stats.total_revenue / stats.task_count if stats.task_count > 0 else 0,
                    "trend": "stable",
                }
            else:
                return {
                    "minerId": minerId,
                    "period": period,
                    "totalEarnings": 0,
                    "taskCount": 0,
                    "avgEarningPerTask": 0,
                    "trend": "new",
                }
        except Exception as e:
            return self._rpc_internal_error("revenue_getMinerStats", e, {
                "minerId": minerId,
                "period": period,
                "totalEarnings": 0,
                "taskCount": 0,
                "avgEarningPerTask": 0,
                "trend": "unknown",
            })
    
    def _revenue_get_leaderboard(self, limit: int = 10, period: str = "day", **kwargs) -> Dict:
        """获取收益排行"""
        manager = self._get_revenue_tracking()
        
        # get_leaderboard 只接limit 参数
        leaderboard = manager.get_leaderboard(limit)
        
        return {
            "period": period,
            "rankings": leaderboard,
            "updatedAt": time.time(),
        }
    
    def _revenue_get_forecast(self, minerId: str, days: int = 7, **kwargs) -> Dict:
        """获取收益预测"""
        manager = self._get_revenue_tracking()
        
        try:
            forecast = manager.get_revenue_forecast(minerId)
            
            if forecast:
                return {
                    "minerId": minerId,
                    "forecastDays": days,
                    "predictedEarnings": forecast.predicted_amount,
                    "confidence": forecast.confidence,
                    "factors": [],
                }
            else:
                return {
                    "minerId": minerId,
                    "forecastDays": days,
                    "predictedEarnings": 0,
                    "confidence": 0,
                    "factors": [],
                    "message": "No forecast data available",
                }
        except Exception as e:
            return self._rpc_internal_error("revenue_getForecast", e, {
                "minerId": minerId,
                "forecastDays": days,
                "predictedEarnings": 0,
                "confidence": 0,
                "factors": [],
            })
    
    # === 主网监控方法 ===
    
    def _monitor_get_health(self, **kwargs) -> Dict:
        """获取系统健康状"""
        monitor = self._get_mainnet_monitor()
        
        health = monitor.get_health_status()
        
        return {
            "status": health.get("overall", "unknown"),
            "components": health.get("components", {}),
            "uptime": time.time() - monitor.dashboard.updated_at if hasattr(monitor, 'dashboard') else 0,
            "lastCheck": time.time(),
        }
    
    def _monitor_get_dashboard(self, **kwargs) -> Dict:
        """获取监控面板"""
        monitor = self._get_mainnet_monitor()
        
        dashboard = monitor.get_dashboard()
        
        return {
            "systemHealth": dashboard.get("system_health", "healthy"),
            "metrics": dashboard.get("metrics", {}),
            "recentAlerts": dashboard.get("recent_alerts", []),
            "networkStats": dashboard.get("network_stats", {}),
        }
    
    def _monitor_get_alerts(self, severity: str = None, limit: int = 50, **kwargs) -> Dict:
        """获取告警列表"""
        monitor = self._get_mainnet_monitor()
        
        alerts = monitor.get_alerts(severity=severity)[:limit]
        
        return {
            "alerts": alerts,
            "totalCount": len(alerts),
        }
    
    def _monitor_record_metric(self, metricName: str, value: float, labels: Dict = None, **kwargs) -> Dict:
        """记录指标"""
        monitor = self._get_mainnet_monitor()
        
        monitor.record_metric(metricName, value, labels or {})
        
        return {
            "success": True,
            "metricName": metricName,
            "value": value,
        }
    
    # === SDK API 方法 ===
    
    def _sdk_get_openapi_spec(self, **kwargs) -> Dict:
        """获取 OpenAPI 规范"""
        manager = self._get_sdk_api_manager()
        
        spec = manager.get_openapi_spec()
        
        return {
            "spec": spec,
        }
    
    def _sdk_generate_sdk(self, language: str, **kwargs) -> Dict:
        """生成 SDK 代码"""
        manager = self._get_sdk_api_manager()
        
        try:
            code = manager.generate_sdk(language)
            return {
                "success": True,
                "language": language,
                "code": code,
            }
        except ValueError as e:
            return {
                "success": False,
                "error": "internal_error",
            }
    
    def _sdk_get_endpoints(self, **kwargs) -> Dict:
        """获取 API 端点列表"""
        manager = self._get_sdk_api_manager()
        
        endpoints = manager.get_endpoints_list()
        
        return {
            "endpoints": endpoints,
            "totalCount": len(endpoints),
        }
    
    def _sdk_get_examples(self, endpointPath: str = None, **kwargs) -> Dict:
        """获取代码示例"""
        manager = self._get_sdk_api_manager()
        
        examples = manager.get_examples(endpointPath)
        
        return {
            "examples": [e.to_dict() for e in examples],
            "totalCount": len(examples),
        }
    
    # ============== 前端兼容方法实现 ==============
    
    def _frontend_miner_get_sector_list(self, **kwargs) -> Dict:
        """获取板块币列表 - 前端兼容方法（支持动态板块）"""
        try:
            from .sector_coin import SectorCoinType, get_sector_registry
            registry = get_sector_registry()
            sectors = []
            
            # 先加入内置 Enum 板块
            for coin_type in SectorCoinType:
                info = registry.get_sector_info(coin_type.sector)
                sectors.append({
                    "sector": coin_type.sector,
                    "name": coin_type.name,
                    "symbol": coin_type.value,
                    "active": info["active"] if info else True,
                })
            
            # 再加入动态板块
            for info in registry.get_all_sectors():
                if info["name"] not in {ct.sector for ct in SectorCoinType}:
                    sectors.append({
                        "sector": info["name"],
                        "name": f"{info['name']}_COIN",
                        "symbol": f"{info['name']}_COIN",
                        "active": info["active"],
                    })
            
            return {"sectors": sectors, "total": len(sectors)}
        except Exception as e:
            self._rpc_log_exception("frontend_miner_getSectorList", e)
            # SectorCoinType 不可用时，使用真实板块名
            return {
                "sectors": [
                    {"sector": "H100", "name": "H100_COIN", "symbol": "H100", "active": True},
                    {"sector": "RTX4090", "name": "RTX4090_COIN", "symbol": "RTX4090", "active": True},
                    {"sector": "RTX3080", "name": "RTX3080_COIN", "symbol": "RTX3080", "active": True},
                    {"sector": "CPU", "name": "CPU_COIN", "symbol": "CPU", "active": True},
                    {"sector": "GENERAL", "name": "GENERAL_COIN", "symbol": "GENERAL", "active": True},
                ],
                "total": 5
            }
    
    def _frontend_miner_get_gpu_list(self, **kwargs) -> Dict:
        """获取 GPU 列表 - 从定价引擎获取"""
        gpu_types = ["RTX3060", "RTX3080", "RTX3090", "RTX4060", "RTX4080", "RTX4090", "A100", "H100", "H200"]
        gpus = []
        # 检查哪些 GPU 实际有在线矿工
        active_types = set()
        for miner in self.registered_miners.values():
            gpu = miner.get("gpuType") or miner.get("gpu_type")
            if gpu:
                active_types.add(gpu)
        for gpu in gpu_types:
            gpus.append({
                "type": gpu,
                "name": gpu,
                "available": gpu in active_types,
            })
        return {"gpus": gpus, "total": len(gpus)}
    
    def _frontend_dashboard_get_sector_distribution(self, **kwargs) -> Dict:
        """获取板块分布 - 前端兼容方法"""
        distribution = {}
        if self.sector_ledger and self.miner_address:
            try:
                from .sector_coin import SectorCoinType
                all_balances = self.sector_ledger.get_all_balances(self.miner_address)
                for coin_type, bal in all_balances.items():
                    distribution[coin_type.sector] = {
                        "balance": bal.balance,
                        "percentage": 0,
                    }
            except Exception as e:
                self._rpc_log_exception("frontend_dashboard_getSectorDistribution", e)
                pass
        
        # 返回默认分布（使用真实板块名）
        if not distribution:
            distribution = {
                "H100": {"balance": 0, "percentage": 20},
                "RTX4090": {"balance": 0, "percentage": 20},
                "RTX3080": {"balance": 0, "percentage": 20},
                "CPU": {"balance": 0, "percentage": 20},
                "GENERAL": {"balance": 0, "percentage": 20},
            }
        return {"distribution": distribution}
    
    def _frontend_exchange_create_order(
        self, 
        baseCurrency: str = "MC",
        quoteCurrency: str = "USDT",
        side: str = "buy",
        type: str = "limit",
        amount: float = 0,
        price: float = 0,
        **kwargs
    ) -> Dict:
        """创建交易所订单 - 前端兼容方法。

        兼容两条路径：
        1. 老版交易所下单（原样保留）
        2. ComputeMarketV3 下单（含 TEE 参数透传）
        """
        # ---- ComputeMarketV3 路径 ----
        sector = kwargs.get("sector")
        gpu_count = kwargs.get("gpuCount", kwargs.get("gpu_count"))
        duration_hours = kwargs.get("durationHours", kwargs.get("duration_hours"))
        max_price = kwargs.get("maxPrice", kwargs.get("max_price", price))
        task_hash = kwargs.get("taskHash", kwargs.get("task_hash"))

        if self.compute_market and sector and gpu_count is not None and duration_hours is not None and task_hash:
            try:
                from .compute_market_v3 import TaskExecutionMode

                execution_mode_raw = str(kwargs.get("executionMode", kwargs.get("execution_mode", "normal"))).lower()
                if execution_mode_raw == "tee":
                    execution_mode = TaskExecutionMode.TEE
                elif execution_mode_raw == "zk":
                    execution_mode = TaskExecutionMode.ZK
                else:
                    execution_mode = TaskExecutionMode.NORMAL

                tee_attestation = kwargs.get("teeAttestation", kwargs.get("tee_attestation", {}))
                if isinstance(tee_attestation, str):
                    try:
                        tee_attestation = json.loads(tee_attestation)
                    except Exception as e:
                        self._rpc_log_exception("frontend_exchange_createOrder", e)
                        tee_attestation = {}

                buyer_address = kwargs.get("buyerAddress", kwargs.get("buyer_address", self.node_id or "anonymous"))
                order, msg = self.compute_market.create_order(
                    buyer_address=buyer_address,
                    sector=str(sector),
                    gpu_count=int(gpu_count),
                    duration_hours=int(duration_hours),
                    max_price=float(max_price),
                    task_hash=str(task_hash),
                    task_encrypted_blob=str(kwargs.get("taskEncryptedBlob", kwargs.get("task_encrypted_blob", ""))),
                    execution_mode=execution_mode,
                    allow_validation=bool(kwargs.get("allowValidation", kwargs.get("allow_validation", True))),
                    tee_node_id=str(kwargs.get("teeNodeId", kwargs.get("tee_node_id", ""))),
                    tee_attestation=tee_attestation,
                )
                if not order:
                    raise RPCError(RPCErrorCode.INVALID_PARAMS.value, msg)

                return {
                    "orderId": order.order_id,
                    "status": order.status.value,
                    "sector": order.sector,
                    "gpuCount": order.gpu_count,
                    "durationHours": order.duration_hours,
                    "maxPrice": order.max_price,
                    "executionMode": order.execution_mode.value,
                    "teeNodeId": order.tee_node_id,
                    "message": msg,
                    "createdAt": int(order.created_at),
                }
            except RPCError:
                raise
            except Exception as e:
                raise RPCError(RPCErrorCode.INTERNAL_ERROR.value, "compute_market_create_order_failed")

        # ---- 老版交易所路径 ----
        order_id = str(uuid.uuid4())[:8]
        order = {
            "orderId": order_id,
            "baseCurrency": baseCurrency,
            "quoteCurrency": quoteCurrency,
            "side": side,
            "type": type,
            "amount": amount,
            "price": price,
            "status": "pending",
            "createdAt": int(time.time()),
        }
        # 存储订单
        self.market_orders[order_id] = order
        return order
    
    def _frontend_data_lifecycle_get_status(self, dataId: str = None, **kwargs) -> Dict:
        """获取数据生命周期状态"""
        if not dataId:
            return {"status": "unknown", "message": "Data lifecycle service running"}
        # 尝试从真实数据生命周期管理器查询
        try:
            from .data_lifecycle import DataLifecycleManager
            dlm = DataLifecycleManager()
            asset = dlm.get_asset(dataId)
            if asset:
                return {
                    "dataId": dataId,
                    "status": asset.get("status", "unknown"),
                    "createdAt": asset.get("created_at", 0),
                    "expiresAt": asset.get("expires_at", 0),
                    "size": asset.get("size", 0),
                    "replicas": asset.get("replicas", 0),
                }
        except Exception as e:
            self._rpc_log_exception("frontend_data_lifecycle_getStatus", e)
            pass
        return {
            "dataId": dataId,
            "status": "not_found",
            "message": "Data asset not found",
        }
    
    def _frontend_billing_get_detailed(self, taskId: str = None, address: str = None, **kwargs) -> Dict:
        """获取详细计费信息"""
        items = []
        total_cost = 0.0
        # 从任务记录中提取计费信息
        if taskId and taskId in self.tasks:
            task = self.tasks[taskId]
            cost = task.get("actual_cost", task.get("price", 0))
            total_cost = cost
            if cost > 0:
                items.append({
                    "type": "compute",
                    "description": f"GPU {task.get('gpuType', 'N/A')} x {task.get('gpuCount', 1)}",
                    "hours": task.get("estimatedHours", 0),
                    "unitPrice": task.get("price_per_hour", 0),
                    "cost": cost,
                })
        return {
            "taskId": taskId,
            "address": address,
            "items": items,
            "totalCost": round(total_cost, 4),
            "currency": "MAIN",
            "period": {
                "start": int(time.time()) - 86400 * 30,
                "end": int(time.time()),
            },
        }


# RPCHTTPHandler, RPCServer, RPCClient 已移core/rpc/server.py
# 通过文件顶部from core.rpc.server import ... 保持兼容

