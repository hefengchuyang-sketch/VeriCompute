"""
安全模块 - 生产环境安全加固

提供：
1. TLS 证书自动生成
2. API Key 认证
3. IP 速率限制
4. 钱包助记词 AES-GCM 加密
5. HMAC 消息签名
"""

import os
import ssl
import time
import json
import hashlib
import hmac
import base64
import secrets
import threading
from typing import Dict, Optional, Tuple
from pathlib import Path


# ============== 运行环境判定 ==============

def get_runtime_environment() -> str:
    """统一运行环境判定。

    优先级:
    1) APP_ENV / MAINCOIN_ENV / POUW_ENV
    2) MAINCOIN_PRODUCTION=true 视为 production
    3) 默认 development
    """
    for key in ("APP_ENV", "MAINCOIN_ENV", "POUW_ENV"):
        value = os.environ.get(key, "").strip().lower()
        if value:
            return value

    production_flag = os.environ.get("MAINCOIN_PRODUCTION", "").strip().lower()
    if production_flag in ("1", "true", "yes", "on"):
        return "production"

    return "development"


def is_production_mode() -> bool:
    """是否处于生产模式。"""
    return get_runtime_environment() in ("production", "prod", "mainnet")


# ============== TLS 证书管理 ==============

def generate_self_signed_cert(cert_dir: str) -> Tuple[str, str]:
    """生成自签名 TLS 证书用于 P2P 和 RPC。
    
    Returns:
        (cert_path, key_path)
    """
    cert_path = os.path.join(cert_dir, "node.crt")
    key_path = os.path.join(cert_dir, "node.key")
    
    if os.path.exists(cert_path) and os.path.exists(key_path):
        return cert_path, key_path
    
    os.makedirs(cert_dir, exist_ok=True)
    
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        import datetime
        
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        
        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "POUW Chain Node"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "POUW Network"),
        ])
        
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.datetime.utcnow())
            .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=3650))
            .add_extension(
                x509.SubjectAlternativeName([
                    x509.DNSName("localhost"),
                    x509.IPAddress(
                        __import__('ipaddress').ip_address("127.0.0.1")
                    ),
                ]),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )
        
        with open(cert_path, "wb") as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))
        with open(key_path, "wb") as f:
            f.write(key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            ))
        
        # 设置密钥文件权限
        try:
            os.chmod(key_path, 0o600)
        except (OSError, PermissionError):
            import logging as _log
            _log.getLogger(__name__).warning(f"无法设置密钥文件权限 0600: {key_path}")
        
        return cert_path, key_path
        
    except ImportError:
        # 无 cryptography 库，用 openssl 命令行
        import subprocess
        try:
            subprocess.run([
                "openssl", "req", "-x509", "-newkey", "rsa:2048",
                "-keyout", key_path, "-out", cert_path,
                "-days", "3650", "-nodes",
                "-subj", "/CN=POUW Chain Node/O=POUW Network"
            ], check=True, capture_output=True)
            return cert_path, key_path
        except Exception:
            # 无法生成证书，返回 None
            return None, None


def create_ssl_context(cert_path: str, key_path: str, server: bool = True) -> Optional[ssl.SSLContext]:
    """创建 SSL 上下文。"""
    if not cert_path or not key_path:
        return None
    if not os.path.exists(cert_path) or not os.path.exists(key_path):
        return None
    
    if server:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert_path, key_path)
    else:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        # P2P 网络使用自签名证书时，加载 CA 证书进行验证
        # 生产环境应配置 CA 证书路径
        ca_path = os.environ.get("MAINCOIN_CA_CERT", "")
        if ca_path and os.path.exists(ca_path):
            ctx.load_verify_locations(ca_path)
            ctx.check_hostname = True
            ctx.verify_mode = ssl.CERT_REQUIRED
        else:
            if is_production_mode():
                raise RuntimeError(
                    "生产环境必须配置 MAINCOIN_CA_CERT 且启用证书校验，"
                    "禁止 TLS 客户端使用 CERT_NONE。"
                )

            # 开发环境允许降级，但明确告警。
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            import logging
            logging.getLogger(__name__).warning(
                "开发环境: 未配置 CA 证书 (MAINCOIN_CA_CERT)，"
                "TLS 客户端已降级为 CERT_NONE。"
            )
    
    # 优先 TLS 1.3，不支持时回退到 1.2
    try:
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    except (AttributeError, ValueError):
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


# ============== API Key 认证 ==============

class APIKeyAuth:
    """API Key 认证管理器。"""
    
    def __init__(self, admin_key: str = ""):
        # 优先级：构造参数 > POUW_ADMIN_KEY 环境变量 > 持久化文件 > 自动生成
        if not admin_key:
            admin_key = os.environ.get("POUW_ADMIN_KEY", "")
        if not admin_key:
            admin_key = self._load_or_generate_admin_key()
            self._auto_generated = True
        else:
            self._auto_generated = False
        
        self._admin_key = admin_key
        self._api_keys: Dict[str, dict] = {}  # key -> {role, address, created_at}
        
        # 管理员 key
        self._api_keys[admin_key] = {
            "role": "admin",
            "address": "admin",
            "created_at": time.time(),
        }
    
    @property
    def admin_key(self) -> str:
        return self._admin_key
    
    @property
    def auto_generated(self) -> bool:
        return self._auto_generated
    
    @staticmethod
    def _load_or_generate_admin_key() -> str:
        """从持久化文件加载或生成新的 admin key。"""
        key_file = os.path.join(os.path.expanduser("~"), ".pouw_admin_key")
        try:
            if os.path.exists(key_file):
                with open(key_file, 'r') as f:
                    key = f.read().strip()
                if len(key) == 64:  # 有效的 hex(32) 密钥
                    return key
        except Exception:
            pass
        
        # 生成新密钥并持久化
        key = secrets.token_hex(32)
        try:
            with open(key_file, 'w') as f:
                f.write(key)
            os.chmod(key_file, 0o600)
        except (OSError, PermissionError):
            import logging
            logging.getLogger(__name__).warning(
                f"无法持久化 admin key 到 {key_file}，重启后将生成新密钥"
            )
        return key
    
    def create_key(self, role: str = "user", address: str = "") -> str:
        """创建新 API Key。"""
        key = secrets.token_hex(32)
        self._api_keys[key] = {
            "role": role,
            "address": address,
            "created_at": time.time(),
        }
        return key
    
    def validate(self, token: str) -> Optional[dict]:
        """验证 token，返回角色信息或 None。"""
        if not token:
            return None
        
        # 直接 API Key 匹配
        if token in self._api_keys:
            return self._api_keys[token].copy()
        
        # HMAC 签名验证（Bearer hmac:timestamp:signature 格式）
        if token.startswith("hmac:"):
            parts = token.split(":", 2)
            if len(parts) == 3:
                _, ts, sig = parts
                try:
                    # 检查时间戳（5分钟有效期）
                    if abs(time.time() - float(ts)) > 300:
                        return None
                    expected = hmac.new(
                        self._admin_key.encode(),
                        ts.encode(),
                        hashlib.sha256,
                    ).hexdigest()
                    if hmac.compare_digest(sig, expected):
                        return {"role": "admin", "address": "admin"}
                except Exception:
                    pass
        
        return None
    
    def authenticate_request(self, headers: dict) -> Dict:
        """从 HTTP 请求头认证。
        
        支持：
        - Authorization: Bearer <api_key>
        - X-API-Key: <api_key>
        - 无认证时返回 guest 角色（只能访问公开方法）
        """
        auth_context = {"role": "guest"}
        
        # X-API-Key 头
        api_key = headers.get("X-API-Key", "")
        if api_key:
            info = self.validate(api_key)
            if info:
                auth_context = {
                    "role": info["role"],
                    "user": info.get("address", ""),
                    "user_address": info.get("address", ""),
                    "is_admin": info["role"] == "admin",
                }
                return auth_context
        
        # Authorization: Bearer 头
        auth_header = headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            info = self.validate(token)
            if info:
                auth_context = {
                    "role": info["role"],
                    "user": info.get("address", ""),
                    "user_address": info.get("address", ""),
                    "is_admin": info["role"] == "admin",
                }
                return auth_context
        
        # X-Auth-User 头不再作为独立认证手段
        # 必须先通过 API Key 或 Bearer Token 认证
        # X-Auth-User 仅在已认证的情况下作为补充标识
        
        return auth_context


# ============== IP 速率限制 ==============

class RateLimiter:
    """IP 速率限制器 - 滑动窗口算法。"""
    
    # 最大跟踪 IP 数量，防止内存耗尽攻击
    MAX_TRACKED_IPS = 10000
    
    def __init__(self, max_requests: int = 100, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window = window_seconds
        self._requests: Dict[str, list] = {}  # ip -> [timestamps]
        self._lock = threading.Lock()
        self._blocked: Dict[str, float] = {}  # ip -> blocked_until
        
        # 白名单
        self._whitelist = {"127.0.0.1", "::1", "localhost"}
    
    def add_whitelist(self, ip: str):
        self._whitelist.add(ip)
    
    def is_allowed(self, ip: str) -> Tuple[bool, int]:
        """检查 IP 是否允许请求。
        
        Returns:
            (allowed, remaining_requests)
        """
        if ip in self._whitelist:
            return True, self.max_requests
        
        now = time.time()
        
        with self._lock:
            # 内存保护：如果跟踪的 IP 数量过多，清理最旧的
            if len(self._requests) >= self.MAX_TRACKED_IPS:
                self._evict_oldest()
            
            # 检查是否被临时封禁
            if ip in self._blocked:
                if now < self._blocked[ip]:
                    return False, 0
                else:
                    del self._blocked[ip]
            
            # 清理过期记录
            if ip in self._requests:
                self._requests[ip] = [
                    t for t in self._requests[ip] if now - t < self.window
                ]
            else:
                self._requests[ip] = []
            
            count = len(self._requests[ip])
            
            if count >= self.max_requests:
                # 超限，临时封禁 60 秒
                self._blocked[ip] = now + 60
                return False, 0
            
            self._requests[ip].append(now)
            return True, self.max_requests - count - 1
    
    def _evict_oldest(self):
        """驱逐最旧的 IP 记录（在锁内调用）。"""
        if not self._requests:
            return
        # 按最后一次请求时间排序，删除最旧的 20%
        evict_count = max(1, len(self._requests) // 5)
        sorted_ips = sorted(
            self._requests.items(),
            key=lambda x: max(x[1]) if x[1] else 0
        )
        for ip, _ in sorted_ips[:evict_count]:
            del self._requests[ip]
    
    def cleanup(self):
        """定期清理过期数据。"""
        now = time.time()
        with self._lock:
            expired_ips = [
                ip for ip, times in self._requests.items()
                if not times or now - max(times) > self.window * 2
            ]
            for ip in expired_ips:
                del self._requests[ip]
            
            expired_blocks = [
                ip for ip, until in self._blocked.items() if now > until
            ]
            for ip in expired_blocks:
                del self._blocked[ip]


# ============== 钱包助记词加密 ==============

class WalletEncryptor:
    """钱包助记词 AES-GCM 加密。"""
    
    # 使用机器指纹作为加密因子
    _machine_key = None
    
    @classmethod
    def _get_machine_key(cls) -> bytes:
        """获取基于机器指纹的加密密钥（即使没有密码也提供基本保护）。"""
        if cls._machine_key:
            return cls._machine_key
        
        import socket
        import platform
        
        # 组合多种机器信息
        fingerprint = f"{socket.gethostname()}:{platform.node()}:{platform.machine()}"
        
        # 加入持久化的随机种子
        seed_file = os.path.join(os.path.expanduser("~"), ".pouw_seed")
        try:
            if os.path.exists(seed_file):
                with open(seed_file, 'r') as f:
                    seed = f.read().strip()
            else:
                seed = secrets.token_hex(32)
                with open(seed_file, 'w') as f:
                    f.write(seed)
                try:
                    os.chmod(seed_file, 0o600)
                except (OSError, PermissionError):
                    import logging as _wlog
                    _wlog.getLogger(__name__).warning(
                        f"无法设置种子文件权限 0600: {seed_file}"
                    )
        except Exception:
            # H-3 fix: 种子文件创建失败时，生成临时内存随机种子（而非硬编码字符串）
            import logging
            seed = secrets.token_hex(32)
            logging.getLogger(__name__).warning(
                "无法读写 .pouw_seed 文件，已生成临时随机种子，"
                "重启后将无法解密旧的加密钱包。"
            )
        
        fingerprint += f":{seed}"
        
        cls._machine_key = hashlib.pbkdf2_hmac(
            'sha256', fingerprint.encode(), b'POUW_WALLET_v1', 100000
        )
        return cls._machine_key
    
    @classmethod
    def encrypt_mnemonic(cls, mnemonic: str) -> str:
        """加密助记词，返回 base64 编码的密文。"""
        try:
            key = cls._get_machine_key()
            nonce = os.urandom(12)
            
            # AES-GCM 加密
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            aesgcm = AESGCM(key)
            ct = aesgcm.encrypt(nonce, mnemonic.encode('utf-8'), None)
            
            # nonce + ciphertext
            return base64.b64encode(nonce + ct).decode('ascii')
        except ImportError:
            # [SECURITY] 必须安装 cryptography 库才能加密助记词
            # XOR 不提供真正的安全性，不允许用于助记词加密
            raise RuntimeError(
                "cryptography library is required for mnemonic encryption. "
                "Install with: pip install cryptography"
            )
    
    @classmethod
    def decrypt_mnemonic(cls, encrypted: str) -> str:
        """解密助记词。"""
        # 向后兼容：如果之前用 XOR 加密的，仍能解密并提示重新加密
        if encrypted.startswith("xor:"):
            import logging
            logging.getLogger(__name__).warning(
                "[SECURITY] 检测到使用不安全的 XOR 加密的助记词，请重新加密"
            )
            return cls._simple_decrypt(encrypted)
        
        try:
            key = cls._get_machine_key()
            raw = base64.b64decode(encrypted)
            nonce = raw[:12]
            ct = raw[12:]
            
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            aesgcm = AESGCM(key)
            plaintext = aesgcm.decrypt(nonce, ct, None)
            return plaintext.decode('utf-8')
        except ImportError:
            raise RuntimeError(
                "cryptography library is required for mnemonic decryption. "
                "Install with: pip install cryptography"
            )
    
    @classmethod
    def _simple_encrypt(cls, text: str) -> str:
        """简单 XOR 混淆（降级方案）。"""
        key = cls._get_machine_key()
        data = text.encode('utf-8')
        encrypted = bytes(b ^ key[i % len(key)] for i, b in enumerate(data))
        return "xor:" + base64.b64encode(encrypted).decode('ascii')
    
    @classmethod
    def _simple_decrypt(cls, encrypted: str) -> str:
        """简单 XOR 解混淆。"""
        if encrypted.startswith("xor:"):
            encrypted = encrypted[4:]
        key = cls._get_machine_key()
        data = base64.b64decode(encrypted)
        decrypted = bytes(b ^ key[i % len(key)] for i, b in enumerate(data))
        return decrypted.decode('utf-8')


# ============== 交易确认数 ==============

# 生产环境交易最终性参数
CONFIRMATION_REQUIREMENTS = {
    "coinbase": 100,      # Coinbase 交易成熟度
    "standard": 6,        # 普通交易确认数
    "high_value": 12,     # 高价值交易（>1000 币）
    "exchange": 20,       # 交易所充值确认数
}

def get_required_confirmations(tx_type: str = "standard", amount: float = 0) -> int:
    """根据交易类型和金额返回需要的确认数。"""
    if tx_type == "coinbase":
        return CONFIRMATION_REQUIREMENTS["coinbase"]
    if amount > 1000:
        return CONFIRMATION_REQUIREMENTS["high_value"]
    return CONFIRMATION_REQUIREMENTS["standard"]


# ============== P2P 消息签名 ==============

class MessageSigner:
    """P2P 消息 HMAC 签名。"""
    
    def __init__(self, secret: str = ""):
        if not secret:
            secret = secrets.token_hex(32)
        self._secret = secret.encode('utf-8')
    
    def sign(self, data: bytes) -> str:
        """对数据签名。"""
        return hmac.new(self._secret, data, hashlib.sha256).hexdigest()
    
    def verify(self, data: bytes, signature: str) -> bool:
        """验证签名。"""
        expected = hmac.new(self._secret, data, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)


# ============== 公开方法列表（无需认证） ==============

PUBLIC_RPC_METHODS = {
    # ---------- 只读 / 查询类 ----------
    # 节点信息
    "node_getInfo", "node_getVersion",
    # 链信息
    "chain_getInfo", "chain_getHeight", "chain_getConsensusStatus",
    "block_getLatest", "block_getByHeight", "block_getByHash",
    "blockchain_getHeight", "blockchain_getBlock", "blockchain_getLatestBlocks",
    # 交易查询
    "tx_get", "tx_getStatus",
    # 账户查询（敏感流水/子地址需认证）
    "account_getBalance", "account_traceUTXO", "account_getUTXOs",
    # 网络状态
    "network_getStatus", "network_getPeerList",
    # 统计 / 仪表盘
    "stats_getNetwork", "stats_getBlocks", "stats_getTasks",
    "dashboard_getStats", "dashboard_getRecentTasks", "dashboard_getRewardTrend",
    "dashboard_getRecentProposals",
    # 挖矿状态（只读）
    "mining_getStatus", "mining_getRewards", "mining_getScore",
    # 矿工列表
    "miner_getList", "miner_getInfo",
    # 治理（只读）
    "governance_getProposals", "governance_getProposal",
    # 市场 / 定价 / 订单簿
    "sector_getExchangeRates", "sector_getExchangeHistory",
    "pricing_getBaseRates", "pricing_getRealTimePrice", "pricing_getPriceForecast",
    "queue_getPosition",
    "compute_getMarket", "compute_getOrder",
    # 订单
    "order_getList", "order_getDetail",
    # 隐私状态
    "privacy_getStatus",
    # 质押（只读）
    "staking_getRecords",
    # P2P任务（只读）
    "p2pTask_getList", "p2pTask_getStats", "p2pTask_getMiners", "p2pTask_getStatus",
    # 任务（只读）
    "task_getList", "task_getInfo", "task_getFiles", "task_getLogs",
    "task_getOutputs", "task_getRuntimeStatus",
    # 加密任务（查询）
    "encryptedTask_getStatus",
    # 结算
    "settlement_getRecord",
    # 监控
    "monitor_getHealth",
    # RPC 元信息（仅返回公开方法列表）
    "rpc_listMethods",
    # 钱包只读查询（含地址与余额，需认证）
    # 矿工行为报告（只读）
    "miner_getBehaviorReport",
    # 市场报价查询
    "market_getQuotes",
}

# ============== 需认证的写操作（需 API Key / Bearer Token） ==============
# 所有写操作必须通过 API Key 或 Bearer Token 认证。
# 敏感操作（如 wallet_transfer）还会额外校验钱包解锁状态。

AUTHENTICATED_WRITE_METHODS = {
    # 钱包操作（创建/导入/转账/导出 — 需要认证或本地访问）
    "wallet_create", "wallet_import", "wallet_importKeystore",
    "wallet_unlock", "wallet_lock",
    "wallet_transfer", "wallet_exportKeystore",
    # 账户
    "account_createSubAddress",
    # 挖矿控制（需要认证，防止远程操控）
    "mining_start", "mining_stop", "mining_setMode",
    # 任务
    "task_create", "task_cancel", "task_raiseDispute", "task_acceptResult",
    # 加密任务
    "encryptedTask_create", "encryptedTask_generateKeypair", "encryptedTask_submit",
    # 治理
    "governance_createProposal", "governance_vote",
    # 兑换
    "sector_requestExchange", "sector_cancelExchange",
    # 隐私
    "privacy_rotateAddress",
    # 质押
    "staking_stake", "staking_unstake",
    # 算力市场
    "compute_acceptOrder", "compute_cancelOrder",
    "market_acceptQuote",
}
