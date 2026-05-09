#!/usr/bin/env python3
"""
POUW Multi-Sector Chain - 主启动脚本

一键启动完整节点，包括：
- P2P 网络
- 区块链同步
- RPC 服务
- 挖矿（可选）
- Web UI（可选）

用法：
    python main.py                  # 使用默认配置启动
    python main.py --config custom.yaml
    python main.py --port 9334
    python main.py --mining         # 启用挖矿
    python main.py --ui             # 启动 Web UI
"""

import os
import sys
import time
import copy
import signal
import asyncio
import argparse
import threading
from pathlib import Path
from typing import Optional, Dict, Any

# 确保可以导入 core 模块
sys.path.insert(0, str(Path(__file__).parent))

# 尝试导入依赖
try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False
    print("[WARN] pyyaml not installed, using default config")

try:
    from colorama import init, Fore, Style
    init()
    HAS_COLOR = True
except ImportError:
    HAS_COLOR = False
    class Fore:
        GREEN = YELLOW = RED = CYAN = MAGENTA = RESET = ""
    class Style:
        BRIGHT = RESET_ALL = ""

# ============== 强制依赖检查 ==============

def _check_critical_dependencies():
    """检查必须的安全依赖，缺失则拒绝启动"""
    missing = []
    try:
        import ecdsa  # noqa: F401
    except ImportError:
        missing.append("ecdsa (pip install ecdsa)")
    try:
        import hashlib
        hashlib.new('ripemd160')
    except (ValueError, Exception):
        missing.append("ripemd160 support (install openssl with ripemd160)")
    if missing:
        print(f"\n[FATAL] 缺少关键安全依赖，拒绝启动:")
        for dep in missing:
            print(f"  - {dep}")
        print(f"\n请运行: pip install -r requirements.txt\n")
        sys.exit(1)

_check_critical_dependencies()


# ============== 日志 ==============

class Logger:
    """结构化日志 - 同时输出控制台彩色 + 文件日志。"""
    
    def __init__(self, name: str = "POUW"):
        self.name = name
        import logging
        self._logger = logging.getLogger(name)
        self._logger.setLevel(logging.DEBUG)
        
        # 文件日志（结构化 JSON 行）
        try:
            os.makedirs("logs", exist_ok=True)
            fh = logging.FileHandler("logs/node.log", encoding='utf-8')
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(logging.Formatter(
                '{"time":"%(asctime)s","level":"%(levelname)s","name":"%(name)s","msg":"%(message)s"}'
            ))
            self._logger.addHandler(fh)
        except Exception as e:
            # 日志文件创建失败不应阻断启动，但记录警告
            import sys
            print(f"Warning: Failed to create log file: {e}", file=sys.stderr)
    
    def _time(self) -> str:
        return time.strftime("%H:%M:%S")
    
    def _safe_print(self, msg: str):
        """安全打印，处理编码问题"""
        import re
        # 先移除可能导致编码问题的字符
        safe_msg = re.sub(r'[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF\U0001F1E0-\U0001F1FF\u2600-\u26FF\u2700-\u27BF\u2300-\u23FF\u2B50\u2705\u274C\u26A0\u2699\u26D4\u2B06\u2B07\u27A1\u2B05\u23F3\u231B\u23F0\u23F1\u23F2\u23F8\u23E9\u23EA\u23EB\u23EC\u25B6\u25C0\u23FA\U0001F4B0\U0001F4BC\U0001F528\U0001F517\U0001F4E1\U00002699\U0001F5A5\U000026CF\uFE0E\uFE0F]', '', msg)
        try:
            print(safe_msg)
        except Exception:
            # 最后的备用方案：强制 ASCII
            print(safe_msg.encode('ascii', 'replace').decode('ascii'))
    
    def info(self, msg: str):
        self._safe_print(f"{Fore.GREEN}[{self._time()}] [{self.name}] {msg}{Style.RESET_ALL}")
        self._logger.info(msg)
    
    def warn(self, msg: str):
        self._safe_print(f"{Fore.YELLOW}[{self._time()}] [{self.name}] [WARN] {msg}{Style.RESET_ALL}")
        self._logger.warning(msg)

    def warning(self, msg: str):
        """兼容标准 logging 接口命名。"""
        self.warn(msg)
    
    def error(self, msg: str):
        self._safe_print(f"{Fore.RED}[{self._time()}] [{self.name}] [ERROR] {msg}{Style.RESET_ALL}")
        self._logger.error(msg)
    
    def debug(self, msg: str):
        self._safe_print(f"{Fore.CYAN}[{self._time()}] [{self.name}] [DEBUG] {msg}{Style.RESET_ALL}")
        self._logger.debug(msg)
    
    def success(self, msg: str):
        self._safe_print(f"{Fore.GREEN}{Style.BRIGHT}[{self._time()}] [{self.name}] [OK] {msg}{Style.RESET_ALL}")
        self._logger.info(f"[OK] {msg}")


log = Logger()


# ============== 配置加载 ==============

DEFAULT_CONFIG = {
    "network": {
        "type": "testnet",
        "chain_id": 9333,
        "p2p": {
            "host": "0.0.0.0",
            "port": 9333,
            "max_peers": 50,
            "bootstrap_nodes": [],
        },
        "rpc": {
            "enabled": True,
            "host": "127.0.0.1",
            "port": 8545,
        }
    },
    "node": {
        "id": "",
        "name": "POUW-Node",
        "sector": "auto",
        "role": "full",
    },
    "mining": {
        "enabled": False,
        "miner_address": "",
        "threads": 0,
    },
    "storage": {
        "data_dir": "./data",
        "backend": "sqlite",
    },
    "wallet": {
        "wallet_dir": "./wallets",
        "auto_create": True,
    },
    "logging": {
        "level": "INFO",
        "console": True,
    },
}


def load_config(config_path: str = None) -> Dict:
    """加载配置文件。"""
    # 使用深拷贝避免嵌套 dict 共享引用，防止配置污染默认模板
    config = copy.deepcopy(DEFAULT_CONFIG)
    
    if config_path and os.path.exists(config_path):
        if HAS_YAML:
            with open(config_path, encoding='utf-8') as f:
                user_config = yaml.safe_load(f)
                if user_config:
                    # 深度合并
                    def merge(base, override):
                        for k, v in override.items():
                            if k in base and isinstance(base[k], dict) and isinstance(v, dict):
                                merge(base[k], v)
                            else:
                                base[k] = v
                    merge(config, user_config)
            log.info(f"加载配置: {config_path}")
        else:
            log.warn("无法加载 YAML 配置，使用默认配置")

    # 环境变量覆盖（适配 Docker 部署）
    rpc_host_env = os.environ.get("POUW_RPC_HOST")
    if rpc_host_env:
        config.setdefault("network", {}).setdefault("rpc", {})["host"] = rpc_host_env
    
    return config


# ============== 节点类 ==============

class POUWNode:
    """POUW 完整节点。"""
    
    def __init__(self, config: Dict):
        self.config = config
        self.is_running = False
        
        # 核心组件
        self.storage = None
        self.wallet_manager = None
        self.p2p_node = None
        self.consensus_engine = None
        self.rpc_server = None
        
        # 扩展模块
        self.audit_engine = None
        self.compliance_engine = None
        self.contract_audit = None
        self.miner_security = None
        self.compute_economy = None
        self.privacy_manager = None
        self.cross_scheduler = None
        self.compute_scheduler = None
        self.governance_engine = None
        self.compute_market = None
        self.cloud_manager = None
        self.auto_scaler = None
        self.perf_cache = None
        self.batch_processor = None
        self.global_coordinator = None
        self.i18n = None
        self.notification_service = None
        self.log_viewer = None
        self.node_selector = None
        
        # 统计
        self.start_time = 0
        self.blocks_synced = 0
        self.txs_processed = 0
    
    def _init_storage(self):
        """初始化存储。"""
        from core.storage import StorageManager
        
        data_dir = self.config["storage"]["data_dir"]
        self.storage = StorageManager(data_dir)
        log.info(f"存储初始化: {data_dir}")
    
    def _init_tls(self):
        """初始化 TLS 证书（P2P + RPC 共用）。"""
        try:
            from core.security import generate_self_signed_cert
            cert_dir = os.path.join(self.config["storage"]["data_dir"], "certs")
            self.ssl_cert, self.ssl_key = generate_self_signed_cert(cert_dir)
            if not self.ssl_cert or not self.ssl_key:
                raise RuntimeError("TLS certificate generation returned empty paths")
            log.info(f"TLS 证书就绪: {cert_dir}")
        except Exception as e:
            from core.security import is_production_mode
            if is_production_mode():
                raise RuntimeError(
                    "生产环境 TLS 初始化失败，已拒绝启动。"
                    f" 详情: {e}"
                )
            log.warn(f"TLS 证书生成失败: {e}，开发环境允许降级为明文通信")
            self.ssl_cert = None
            self.ssl_key = None

    def _run_security_preflight(self):
        """启动前安全基线检查（生产环境 fail-closed）。"""
        from core.security import is_production_mode, get_runtime_environment

        runtime_env = get_runtime_environment()
        log.info(f"运行环境: {runtime_env}")

        if not is_production_mode():
            return

        issues = []

        admin_key = (
            os.environ.get("POUW_ADMIN_KEY", "")
            or os.environ.get("MAINCOIN_ADMIN_KEY", "")
            or self.config.get("api", {}).get("admin_key", "")
        )
        if not admin_key:
            issues.append("缺少固定管理密钥: 请设置 POUW_ADMIN_KEY 或 MAINCOIN_ADMIN_KEY")

        require_local_auth = os.environ.get("REQUIRE_LOCAL_AUTH", "true").lower() == "true"
        if not require_local_auth:
            issues.append("REQUIRE_LOCAL_AUTH=false 会导致本地请求自动信任")

        allow_user_override = os.environ.get("ALLOW_AUTH_USER_OVERRIDE", "false").lower() == "true"
        if allow_user_override:
            issues.append("ALLOW_AUTH_USER_OVERRIDE=true 允许请求头覆盖认证主体")

        ca_path = os.environ.get("MAINCOIN_CA_CERT", "")
        if not ca_path:
            issues.append("缺少 MAINCOIN_CA_CERT，TLS 客户端无法完成证书校验")

        if issues:
            raise RuntimeError(
                "生产环境安全基线检查未通过:\n- " + "\n- ".join(issues)
            )
    
    def _init_wallet(self):
        """初始化钱包。"""
        from core.crypto import ProductionWallet
        import json as json_mod
        from core.security import WalletEncryptor
        
        wallet_dir = Path(self.config["wallet"]["wallet_dir"])
        wallet_dir.mkdir(parents=True, exist_ok=True)
        
        # 检查是否有现有钱包（优先 wallet_*.json，其次 keystore_*.json）
        wallet_files_pref = sorted(wallet_dir.glob("wallet_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        wallet_files_all = list(wallet_dir.glob("*.json"))
        wallet_files = wallet_files_pref or wallet_files_all
        
        if wallet_files:
            # 加载已有钱包（优先最新的 wallet_ 文件）
            wallet_path = wallet_files[0]
            log.info(f"加载已有钱包: {wallet_path.name}")
            try:
                with open(wallet_path, 'r') as f:
                    wallet_data = json_mod.load(f)
                
                mnemonic = None
                if 'encrypted_mnemonic' in wallet_data and wallet_data['encrypted_mnemonic']:
                    # 解密助记词
                    try:
                        mnemonic = WalletEncryptor.decrypt_mnemonic(wallet_data['encrypted_mnemonic'])
                        log.info("助记词已解密恢复")
                    except Exception as de:
                        log.warn(f"助记词解密失败: {de}")
                
                if not mnemonic and 'mnemonic' in wallet_data and wallet_data['mnemonic']:
                    # 兼容旧版明文助记词 → 自动迁移加密
                    mnemonic = wallet_data['mnemonic']
                    log.warn("检测到明文助记词，正在迁移为加密存储...")
                    try:
                        wallet_data['encrypted_mnemonic'] = WalletEncryptor.encrypt_mnemonic(mnemonic)
                        del wallet_data['mnemonic']
                        with open(wallet_path, 'w') as f:
                            json_mod.dump(wallet_data, f, indent=2)
                        log.success("助记词已迁移为加密存储")
                    except Exception as me:
                        log.warn(f"助记词加密迁移失败: {me}")
                
                if mnemonic:
                    wallet = ProductionWallet.from_mnemonic(mnemonic)
                    self.default_wallet = wallet
                    log.success(f"钱包已恢复: {wallet.wallet_id}")
                    log.info(f"主地址: {wallet.addresses['MAIN']}")
                else:
                    log.warn(f"钱包文件缺少助记词，将创建新钱包")
                    wallet_files = []  # 跳到创建逻辑
            except Exception as e:
                log.error(f"加载钱包失败: {e}，将创建新钱包")
                wallet_files = []  # 跳到创建逻辑
        
        if not wallet_files:
            # 没有钱包 — 提示用户通过前端创建
            log.warn("未检测到钱包，请通过前端页面创建或导入钱包")
            log.info("前端地址: http://localhost:3000/connect")
    
    def _init_p2p(self):
        """初始化 P2P 网络。"""
        p2p_config = self.config["network"]["p2p"]
        role = self.config["node"].get("role", "light")
        
        # 检测板块 - 只有矿工和提供者角色需要
        sector = self.config["node"]["sector"]
        if sector == "auto":
            if role in ["miner", "provider", "full"]:
                try:
                    from core.device_detector import auto_assign_sector
                    sector = auto_assign_sector()
                    log.info(f"自动检测板块: {sector}")
                except Exception as e:
                    log.warning(f"设备检测失败，使用默认板块 MAIN: {e}")
                    sector = "MAIN"
            else:
                # 轻节点不需要板块
                sector = "MAIN"
        
        log.info(f"P2P 配置: {p2p_config['host']}:{p2p_config['port']}")
        log.info(f"Bootstrap 节点: {len(p2p_config.get('bootstrap_nodes', []))} 个")
        
        # 注意：真正启动需要 asyncio
        self.p2p_config = p2p_config
        self.sector = sector
    
    def _init_consensus(self):
        """初始化共识引擎。

        生产共识入口约束：
        本方法必须且只能从 core.consensus 导入 ConsensusEngine。
        禁止在此处直接导入或实例化以下实验模块：
          - core.unified_consensus.UnifiedConsensus
          - core.dual_layer_consensus.*
          - core.pouw_chain_v3.POUWChainV3
        如需切换或并存共识引擎，必须先更新
        tests/test_production_consensus_entrypoint.py 的守护断言并经过架构评审。
        参考：docs/TECHNICAL_REVIEW_AND_CONSENSUS_RECOMMENDATIONS_2026-05-09.md §7
        """
        from core.consensus import ConsensusEngine

        node_id = self.config["node"]["id"] or f"node_{int(time.time())}"
        network_type = self.config.get("network", {}).get("type", "mainnet")
        
        self.consensus_engine = ConsensusEngine(
            node_id=node_id,
            sector=self.sector,
            log_fn=log.info,
        )
        
        # 应用共识配置
        consensus_cfg = self.config.get("consensus", {})
        if consensus_cfg.get("initial_difficulty"):
            self.consensus_engine.current_difficulty = consensus_cfg["initial_difficulty"]
        # 注意：base_reward 不在此处覆盖，使用板块特定的基础奖励（ChainParams）
        if consensus_cfg.get("halving_interval"):
            self.consensus_engine.reward_calculator.halving_interval = consensus_cfg["halving_interval"]
        if consensus_cfg.get("treasury_rate") is not None:
            rate = float(consensus_cfg["treasury_rate"])
            self.consensus_engine.reward_calculator.treasury_rate = rate
            ChainParams = type(self.consensus_engine).TREASURY_RATE if hasattr(type(self.consensus_engine), 'TREASURY_RATE') else None
            log.info(f"财库税率已配置: {rate*100:.1f}%")

        # 配置混用共识模式
        consensus_mode = consensus_cfg.get("mode", "sbox_primary")
        sbox_ratio = float(consensus_cfg.get("sbox_ratio", 0.5))
        pouw_support_ratio = float(consensus_cfg.get("pouw_support_ratio", 0.1))
        sbox_enabled = consensus_cfg.get("sbox_enabled", True)
        self.consensus_engine.configure_consensus_mode(
            mode=consensus_mode,
            sbox_ratio=sbox_ratio,
            pouw_support_ratio=pouw_support_ratio,
            sbox_enabled=sbox_enabled,
        )
        
        # 网络类型标记
        self.consensus_engine.network_type = network_type
        
        log.info(
            f"共识引擎初始化: {node_id} (网络={network_type}, mode={self.consensus_engine.consensus_mode}, "
            f"sbox_ratio={self.consensus_engine.consensus_sbox_ratio:.2f}, "
            f"pouw_support_ratio={self.consensus_engine.consensus_pouw_support_ratio:.2f})"
        )
    
    def _init_sector_ledger(self):
        """初始化板块币账本。"""
        from core.sector_coin import SectorCoinLedger
        
        data_dir = self.config["storage"]["data_dir"]
        db_path = os.path.join(data_dir, "sector_coins.db")
        self.sector_ledger = SectorCoinLedger(db_path)
        log.info(f"板块币账本初始化: {db_path}")
    
    def _init_utxo_store(self):
        """初始化 UTXO 存储。"""
        from core.utxo_store import UTXOStore
        
        data_dir = self.config["storage"]["data_dir"]
        db_path = os.path.join(data_dir, "utxo.db")
        self.utxo_store = UTXOStore(db_path)
        log.info(f"UTXO 存储初始化: {db_path}")
    
    def _init_rpc(self):
        """初始化 RPC 服务。"""
        rpc_config = self.config["network"]["rpc"]
        
        if rpc_config["enabled"]:
            from core.rpc_service import RPCServer
            import os
            
            # 检测前端静态文件目录
            base_dir = os.path.dirname(os.path.abspath(__file__))
            static_dir = os.path.join(base_dir, 'frontend', 'dist')
            if not os.path.isdir(static_dir):
                static_dir = None
                log.info("未检测到前端文件，仅启动 RPC API")
            else:
                log.info(f"前端静态文件: {static_dir}")
            
            # 从配置读取安全参数（优先环境变量）
            admin_key = (
                os.environ.get("POUW_ADMIN_KEY", "")
                or os.environ.get("MAINCOIN_ADMIN_KEY", "")
                or self.config.get("api", {}).get("admin_key", "")
            )
            cors_origins = rpc_config.get("cors_origins", [])
            rate_limit = rpc_config.get("rate_limit", 200)
            allowed_methods = rpc_config.get("allowed_methods", [])
            
            self.rpc_server = RPCServer(
                host=rpc_config["host"],
                port=rpc_config["port"],
                static_dir=static_dir,
                ssl_cert=getattr(self, 'ssl_cert', None),
                ssl_key=getattr(self, 'ssl_key', None),
                admin_key=admin_key,
                cors_origins=cors_origins,
                rate_limit=rate_limit,
                allowed_methods=allowed_methods,
            )
            
            # 注入核心依赖
            self.rpc_server.rpc_service.consensus_engine = self.consensus_engine
            self.rpc_server.rpc_service.sector_ledger = getattr(self, 'sector_ledger', None)
            self.rpc_server.rpc_service.utxo_store = getattr(self, 'utxo_store', None)
            
            # 注入 MAIN 转账引擎（双见证）
            try:
                from core.main_transfer import MainTransferEngine
                self.rpc_server.rpc_service.main_transfer_engine = MainTransferEngine()
            except Exception as e:
                log.warn(f"MAIN 转账引擎初始化失败，将禁用 MAIN 转账: {e}")
            
            # 注入扩展模块
            svc = self.rpc_server.rpc_service
            if self.audit_engine:
                svc.audit_engine = self.audit_engine
            if self.compliance_engine:
                svc.compliance_engine = self.compliance_engine
            if self.miner_security:
                svc.miner_security = self.miner_security
            if self.compute_economy:
                svc.compute_economy = self.compute_economy
            if self.compute_scheduler:
                svc.compute_scheduler = self.compute_scheduler
            if self.privacy_manager:
                svc.privacy_manager = self.privacy_manager
            if self.cross_scheduler:
                svc.cross_scheduler = self.cross_scheduler
            if self.governance_engine:
                svc.governance_engine = self.governance_engine
            if self.compute_market:
                svc.compute_market = self.compute_market
            if self.cloud_manager:
                svc.cloud_manager = self.cloud_manager
            
            # 启动 RPC 服务器
            self.rpc_server.start()
            log.info(f"RPC 服务: http://{rpc_config['host']}:{rpc_config['port']}")
    
    def _init_extended_modules(self):
        """初始化扩展模块（审计、安全、经济、隐私、调度、治理、基础设施、用户体验）。"""
        data_dir = self.config["storage"]["data_dir"]
        
        # 1. 审计引擎 (其他模块的依赖)
        try:
            from core.audit_compliance import AuditTrailEngine, ComplianceEngine, ContractAuditSystem
            self.audit_engine = AuditTrailEngine(
                db_path=os.path.join(data_dir, "audit_trail.db")
            )
            self.compliance_engine = ComplianceEngine(
                audit_engine=self.audit_engine,
                db_path=os.path.join(data_dir, "compliance.db")
            )
            self.contract_audit = ContractAuditSystem(audit_engine=self.audit_engine)
            log.info("审计合规模块初始化完成")
        except Exception as e:
            log.warn(f"审计合规模块加载失败: {e}")
        
        # 2. 矿工安全
        try:
            from core.miner_security_manager import MinerSecurityManager
            self.miner_security = MinerSecurityManager(
                db_path=os.path.join(data_dir, "miner_security.db")
            )
            log.info("矿工安全模块初始化完成")
        except Exception as e:
            log.warn(f"矿工安全模块加载失败: {e}")
        
        # 3. 基础设施 (多云, 自动扩缩, 缓存, 批处理)
        try:
            from core.infrastructure import (
                MultiCloudManager, AutoScaler,
                PerformanceCache, BatchProcessor, GlobalNodeCoordinator
            )
            self.cloud_manager = MultiCloudManager(
                db_path=os.path.join(data_dir, "multi_cloud.db")
            )
            self.auto_scaler = AutoScaler(cloud_manager=self.cloud_manager)
            self.perf_cache = PerformanceCache()
            self.batch_processor = BatchProcessor()
            self.global_coordinator = GlobalNodeCoordinator()
            log.info("基础设施模块初始化完成")
        except Exception as e:
            log.warn(f"基础设施模块加载失败: {e}")
        
        # 4. 加密计算 / 隐私保护
        try:
            from core.privacy_enhanced import EncryptedComputeManager
            self.privacy_manager = EncryptedComputeManager(
                db_path=os.path.join(data_dir, "encrypted_compute.db")
            )
            log.info("隐私计算模块初始化完成")
        except Exception as e:
            log.warn(f"隐私计算模块加载失败: {e}")
        
        # 5. 算力经济
        try:
            from core.compute_economy import ComputeEconomyEngine
            self.compute_economy = ComputeEconomyEngine(
                db_path=os.path.join(data_dir, "compute_economy.db")
            )
            log.info("算力经济模块初始化完成")
        except Exception as e:
            log.warn(f"算力经济模块加载失败: {e}")
        
        # 5b. 算力调度器（核心任务引擎）
        try:
            from core.compute_scheduler import ComputeScheduler
            self.compute_scheduler = ComputeScheduler(
                db_path=os.path.join(data_dir, "compute_scheduler.db")
            )
            log.info("算力调度器初始化完成")
        except Exception as e:
            self.compute_scheduler = None
            log.warn(f"算力调度器加载失败: {e}")
        
        # 6. 跨区域调度
        try:
            from core.cross_region_scheduler import CrossRegionScheduler
            self.cross_scheduler = CrossRegionScheduler(
                db_path=os.path.join(data_dir, "cross_region_scheduler.db")
            )
            log.info("跨区域调度模块初始化完成")
        except Exception as e:
            log.warn(f"跨区域调度模块加载失败: {e}")
        
        # 7. 增强治理
        try:
            from core.governance_enhanced import EnhancedGovernanceEngine
            self.governance_engine = EnhancedGovernanceEngine(
                db_path=os.path.join(data_dir, "governance_enhanced.db")
            )
            log.info("增强治理模块初始化完成")
        except Exception as e:
            log.warn(f"增强治理模块加载失败: {e}")
        
        # 8. 用户体验
        try:
            from core.user_experience import (
                I18nManager, NotificationService, LogViewer, NodeSelectionController
            )
            self.i18n = I18nManager()
            self.notification_service = NotificationService(
                db_path=os.path.join(data_dir, "notifications.db")
            )
            self.log_viewer = LogViewer(max_logs=5000)
            self.node_selector = NodeSelectionController()
            log.info("用户体验模块初始化完成")
        except Exception as e:
            log.warn(f"用户体验模块加载失败: {e}")
        
        # 9. [H-06] 动态汇率引擎 — 注入兑换服务
        try:
            from core.exchange_rate import DynamicExchangeRate
            self.dynamic_rate_engine = DynamicExchangeRate()
            log.info("动态汇率引擎初始化完成")
            
            # 注入到双见证兑换服务
            try:
                from core.dual_witness_exchange import get_exchange_service
                exchange_svc = get_exchange_service()
                exchange_svc.dynamic_rate_engine = self.dynamic_rate_engine
                log.info("动态汇率引擎已注入兑换服务")
            except Exception as e:
                log.warn(f"动态汇率引擎注入兑换服务失败: {e}")
        except Exception as e:
            self.dynamic_rate_engine = None
            log.warn(f"动态汇率引擎加载失败: {e}")
    
    def _init_compute_market(self):
        """初始化算力市场 V3。"""
        try:
            from core.compute_market_v3 import ComputeMarketV3
            data_dir = self.config["storage"]["data_dir"]
            self.compute_market = ComputeMarketV3(
                db_path=os.path.join(data_dir, "compute_market_v3.db")
            )
            log.info("算力市场 V3 初始化完成")
        except Exception as e:
            log.warn(f"算力市场 V3 加载失败: {e}")
    
    def initialize(self):
        """初始化所有组件。"""
        log.info("=" * 50)
        log.info("POUW Multi-Sector Chain 节点启动中...")
        log.info("=" * 50)

        self._run_security_preflight()
        
        self._init_storage()
        self._init_tls()
        self._init_wallet()
        self._init_p2p()
        self._init_consensus()
        self._init_sector_ledger()  # 初始化板块币账本
        self._init_utxo_store()     # 初始化 UTXO 存储
        
        # 将 UTXO 存储注入共识引擎（用于区块交易验证）
        if self.consensus_engine and self.utxo_store:
            self.consensus_engine.utxo_store = self.utxo_store
        
        # 将板块币账本注入共识引擎（用于 reorg 回滚）
        if self.consensus_engine and self.sector_ledger:
            self.consensus_engine.sector_ledger = self.sector_ledger
        
        # 将 StorageManager 的 BlockStore 注入共识引擎（保持 chain.db 同步）
        if self.consensus_engine and self.storage and hasattr(self.storage, 'blocks'):
            self.consensus_engine.block_store = self.storage.blocks
        
        # 扩展模块
        self._init_extended_modules()
        self._init_compute_market()
        
        self._init_rpc()
        
        # 事件循环引用（用于线程安全的 P2P 广播）
        self._event_loop = None
        
        log.success("所有组件初始化完成")
    
    async def run_async(self):
        """异步运行节点。"""
        self.is_running = True
        self.start_time = time.time()
        
        # 检测端口是否被占用（尝试连接检查是否有服务在监听）
        import socket
        p2p_port = self.p2p_config["port"]
        port_in_use = False
        try:
            test_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            test_sock.settimeout(0.5)
            result = test_sock.connect_ex(('127.0.0.1', p2p_port))
            test_sock.close()
            if result == 0:
                # 端口有服务在监听，真正被占用
                port_in_use = True
        except (OSError, socket.error) as e:
            # socket 操作失败，假设端口可用
            pass
        
        if port_in_use:
            log.warn(f"P2P 端口 {p2p_port} 已被占用，将以 RPC-only 模式运行")
            log.info("提示: 使用 'netstat -ano | findstr :9333' 查找占用进程")
            self.p2p_node = None
            # 跳到 RPC-only 保活循环
            log.warn("P2P 未启用，节点仅提供 RPC 服务")
            try:
                while self.is_running:
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                pass
            return
        
        # 启动 P2P
        from core.tcp_network import P2PNode
        
        try:
            self.p2p_node = P2PNode(
                host=self.p2p_config["host"],
                port=self.p2p_config["port"],
                sector=self.sector,
                bootstrap_nodes=self.p2p_config.get("bootstrap_nodes", []),
                log_fn=log.info,
                ssl_cert=getattr(self, 'ssl_cert', None),
                ssl_key=getattr(self, 'ssl_key', None),
                max_peers=self.p2p_config.get("max_peers", 50),
            )
        except Exception as e:
            log.error(f"P2P 启动失败: {e}")
            log.warn("P2P 功能将被禁用，继续以 RPC-only 模式运行")
            self.p2p_node = None
        
        # 注册消息处理器
        from core.tcp_network import MessageType, P2PMessage
        
        async def handle_new_block(peer, message):
            """P2P 收到新区块 → 验证并加入链。"""
            block_dict = message.payload
            success, msg = self.consensus_engine.receive_block_from_peer(block_dict)
            if success:
                log.info(f"接受区块 #{block_dict.get('height', '?')}: {msg}")
                self.blocks_synced += 1
                # 为该区块的 coinbase 创建 UTXO 记录（含手续费）
                if self.utxo_store and 'miner_address' in block_dict:
                    blk_sector = block_dict.get('sector', self.sector)
                    blk_reward = block_dict.get('block_reward', 0)
                    blk_fees = block_dict.get('total_fees', block_dict.get('fees', 0))
                    miner_income = blk_reward + blk_fees
                    treasury_rate = getattr(self.consensus_engine.reward_calculator, 'treasury_rate', 0.03)
                    treasury_amount = miner_income * treasury_rate
                    miner_net = miner_income - treasury_amount
                    try:
                        self.utxo_store.create_coinbase_utxo(
                            miner_address=block_dict['miner_address'],
                            amount=miner_net,
                            sector=blk_sector,
                            block_height=block_dict.get('height', 0),
                            block_hash=block_dict.get('hash', '')
                        )
                        if treasury_amount > 0:
                            self.utxo_store.create_coinbase_utxo(
                                miner_address='MAIN_TREASURY',
                                amount=treasury_amount,
                                sector=blk_sector,
                                block_height=block_dict.get('height', 0),
                                block_hash=block_dict.get('hash', '')
                            )
                    except Exception as e:
                        log.error(f"CRITICAL: Coinbase UTXO 创建失败 (block #{block_dict.get('height')}): {e}")
                    # 同步写入 sector_ledger（兑换系统所需）
                    if hasattr(self, 'sector_ledger') and self.sector_ledger and blk_sector != 'MAIN':
                        try:
                            self.sector_ledger.mint_block_reward(
                                sector=blk_sector,
                                miner_address=block_dict['miner_address'],
                                block_height=block_dict.get('height', 0)
                            )
                        except Exception as e:
                            log.debug(f"P2P sector_ledger 同步失败: {e}")
                
                # 重放区块内的转账交易到 UTXO 状态
                if self.utxo_store:
                    blk_height = block_dict.get('height', 0)
                    blk_hash = block_dict.get('hash', '')
                    for tx in block_dict.get('transactions', []):
                        try:
                            self.utxo_store.replay_transfer_from_block(
                                tx, blk_height, blk_hash
                            )
                        except Exception as e:
                            log.debug(f"重放交易失败: {e}")
            else:
                log.debug(f"拒绝区块: {msg}")
                # B-2: 如果是 "too new"（对方高度超前），主动请求同步
                if 'too new' in msg.lower() or 'sync needed' in msg.lower():
                    our_height = self.consensus_engine.get_chain_height()
                    log.info(f"检测到高度差距，触发同步 (本地 #{our_height})")
                    try:
                        sync_req = P2PMessage(
                            msg_type=MessageType.GET_BLOCKS,
                            sender_id=self.p2p_node.node_id,
                            payload={"start_height": our_height + 1}
                        )
                        await peer.send(sync_req)
                    except Exception as e:
                        log.debug(f"同步请求失败: {e}")
        
        async def handle_new_tx(peer, message):
            """P2P 收到新交易 → 验证基本字段后存入 mempool 并转发。"""
            tx = message.payload
            tx_id = tx.get('tx_id', tx.get('txid', ''))[:64]
            if not tx_id:
                log.debug("收到无效交易: 缺少 tx_id")
                return
            
            # 去重: 如果已在 pending 列表中则跳过
            with self.consensus_engine._lock:
                for existing in self.consensus_engine.pending_transactions:
                    if existing.get('tx_id', existing.get('txid', '')) == tx_id:
                        log.debug(f"交易已存在，跳过: {tx_id[:12]}")
                        return
            
            # 基本字段验证
            required = ['from', 'to', 'amount']
            from_addr = tx.get('from', tx.get('from_address', ''))
            to_addr = tx.get('to', tx.get('to_address', ''))
            amount = tx.get('amount', 0)
            if not from_addr or not to_addr or amount <= 0:
                log.debug(f"交易字段无效: {tx_id[:12]}")
                return
            
            # 加入共识引擎的 pending 交易池
            self.consensus_engine.add_transaction(tx)
            self.txs_processed += 1
            log.debug(f"接受交易: {tx_id[:12]} ({from_addr[:8]}→{to_addr[:8]} {amount})")
            
            # 转发给其他节点（排除来源节点）
            if self.p2p_node and len(self.p2p_node.peers) > 1:
                try:
                    fwd_msg = P2PMessage(
                        msg_type=MessageType.NEW_TX,
                        sender_id=self.p2p_node.node_id,
                        payload=tx
                    )
                    for pid, p in self.p2p_node.peers.items():
                        if p != peer:
                            try:
                                await p.send(fwd_msg)
                            except Exception:
                                pass
                except Exception as e:
                    log.debug(f"交易转发失败: {e}")
        
        async def handle_get_blocks(peer, message):
            """处理区块同步请求。"""
            start = message.payload.get('start_height', 0)
            count = min(message.payload.get('count', 50), 50)  # 限制单次最多 50 块
            blocks = self.consensus_engine.get_blocks_range(start, max_count=count)
            resp = P2PMessage(
                msg_type=MessageType.BLOCKS,
                sender_id=self.p2p_node.node_id,
                payload={"blocks": blocks, "height": self.consensus_engine.get_chain_height()}
            )
            await peer.send(resp)
            log.debug(f"发送 {len(blocks)} 个区块给 {peer.peer_info.node_id[:8]}")
        
        async def handle_blocks(peer, message):
            """处理收到的同步区块（使用完整验证）。"""
            blocks = message.payload.get('blocks', [])
            peer_height = message.payload.get('height', 0)
            added = 0
            for block_dict in blocks:
                try:
                    block = self.consensus_engine._dict_to_block(block_dict)
                    if block.height <= self.consensus_engine.get_chain_height():
                        continue
                    # 使用完整验证（add_block）而非 add_block_no_validate
                    # 对于历史同步，至少验证 hash/PoW/高度连续性
                    if self.consensus_engine.add_block(block):
                        added += 1
                        self.blocks_synced += 1
                    else:
                        log.warning(f"同步区块 #{block.height} 验证失败，停止同步")
                        break
                except Exception as e:
                    log.debug(f"同步区块失败: {e}")
                    break
            if added > 0:
                log.info(f"同步了 {added} 个区块，当前高度 #{self.consensus_engine.get_chain_height()}")
            # 如果对方还有更多区块，继续请求
            our_height = self.consensus_engine.get_chain_height()
            if our_height < peer_height:
                req = P2PMessage(
                    msg_type=MessageType.GET_BLOCKS,
                    sender_id=self.p2p_node.node_id,
                    payload={"start_height": our_height + 1}
                )
                await peer.send(req)
        
        if self.p2p_node:
            self.p2p_node.register_handler(MessageType.NEW_BLOCK, handle_new_block)
            self.p2p_node.register_handler(MessageType.NEW_TX, handle_new_tx)
            self.p2p_node.register_handler(MessageType.GET_BLOCKS, handle_get_blocks)
            self.p2p_node.register_handler(MessageType.BLOCKS, handle_blocks)
            
            # 注入 P2P 节点到 RPC 服务（用于算力共享任务分发）
            if hasattr(self, 'rpc_server') and self.rpc_server:
                self.rpc_server.rpc_service.p2p_network = self.p2p_node
        
        role = self.config["node"].get("role", "light")
        role_names = {
            "light": "💼 轻节点(仅钱包)",
            "full": "🔗 完整节点",
            "miner": "⛏️ 矿工节点",
            "provider": "🖥️ 算力提供者"
        }
        
        log.success("节点启动成功!")
        log.info(f"角色: {role_names.get(role, role)}")
        if self.p2p_node:
            log.info(f"节点 ID: {self.p2p_node.node_id}")
        log.info(f"板块: {self.sector}")
        log.info(f"P2P: {self.p2p_config['host']}:{self.p2p_config['port']}")
        
        # 无论是否挖矿，都将钱包地址设置到 RPC 服务
        wallet_address = self.config["mining"].get("miner_address", "")
        if not wallet_address and hasattr(self, 'default_wallet'):
            wallet_address = self.default_wallet.addresses["MAIN"]
        if wallet_address and hasattr(self, 'rpc_server') and self.rpc_server:
            self.rpc_server.rpc_service.miner_address = wallet_address
            log.info(f"钱包地址: {wallet_address}")
        
        # 如果配置中没有钱包地址，尝试从磁盘加载上次使用的钱包
        if not wallet_address and hasattr(self, 'rpc_server') and self.rpc_server:
            if self.rpc_server.rpc_service.load_wallet_from_disk():
                wallet_address = self.rpc_server.rpc_service.miner_address
                log.info(f"从磁盘恢复钱包地址: {wallet_address}")
        
        # 启动挖矿（如果配置）
        if self.config["mining"]["enabled"]:
            log.info("🔨 挖矿已启用")
            # 启动挖矿线程
            miner_address = wallet_address
            
            if miner_address:
                # 定义挖矿回调 - 写入板块币奖励和 UTXO
                # [C-10] 使用 crash journal 保证跨库原子性
                from core.crash_journal import get_mining_journal
                mining_journal = get_mining_journal()
                
                # 启动时恢复上次崩溃的未完成事务
                mining_journal.recover_pending(
                    getattr(self, 'sector_ledger', None),
                    getattr(self, 'utxo_store', None),
                    log_fn=lambda msg: log.info(f"[CrashRecovery] {msg}")
                )
                
                def on_block_mined(block):
                    journal_id = None
                    
                    # Step 0: 写入 journal（PENDING）
                    try:
                        journal_id = mining_journal.begin(
                            block_height=block.height,
                            block_hash=block.hash,
                            miner_address=miner_address,
                            sector=self.sector,
                            block_reward=block.block_reward
                        )
                    except Exception as e:
                        log.error(f"Journal 写入失败: {e}")
                    
                    # Step 1: 创建 Coinbase UTXO（转账资金来源）
                    # 矿工收入 = 区块奖励 + 手续费 - 财库税
                    total_income = block.block_reward + getattr(block, 'total_fees', 0)
                    treasury_rate = self.consensus_engine.reward_calculator.treasury_rate
                    treasury_amount = total_income * treasury_rate
                    miner_income = total_income - treasury_amount
                    if hasattr(self, 'utxo_store') and self.utxo_store:
                        txid, utxo = self.utxo_store.create_coinbase_utxo(
                            miner_address=miner_address,
                            amount=miner_income,
                            sector=self.sector,
                            block_height=block.height,
                            block_hash=block.hash
                        )
                        log.info(f"Coinbase UTXO: {txid[:12]}... ({miner_income} {self.sector}, treasury={treasury_amount}, fees={getattr(block, 'total_fees', 0)})")
                        # 财库份额
                        if treasury_amount > 0:
                            self.utxo_store.create_coinbase_utxo(
                                miner_address='MAIN_TREASURY',
                                amount=treasury_amount,
                                sector=self.sector,
                                block_height=block.height,
                                block_hash=block.hash
                            )

                    # Step 2: 同步写入 sector_ledger（兑换系统 lock/burn 所需）
                    if hasattr(self, 'sector_ledger') and self.sector_ledger and self.sector != 'MAIN':
                        try:
                            self.sector_ledger.mint_block_reward(
                                sector=self.sector,
                                miner_address=miner_address,
                                block_height=block.height
                            )
                        except Exception as e:
                            log.debug(f"Sector ledger 同步失败: {e}")
                    
                    # 标记全部完成（COMMITTED）
                    if journal_id:
                        try:
                            mining_journal.commit(journal_id)
                        except Exception:
                            pass
                    
                    # 3. P2P 广播新区块到所有节点
                    if hasattr(self, 'p2p_node') and self.p2p_node and self.p2p_node.peers:
                        try:
                            block_dict = self.consensus_engine._block_to_dict(block)
                            msg = P2PMessage(
                                msg_type=MessageType.NEW_BLOCK,
                                sender_id=self.p2p_node.node_id,
                                payload=block_dict
                            )
                            if self._event_loop and self._event_loop.is_running():
                                import asyncio
                                asyncio.run_coroutine_threadsafe(
                                    self.p2p_node.broadcast(msg), self._event_loop
                                )
                                log.debug(f"广播区块 #{block.height} 到 {len(self.p2p_node.peers)} 个节点")
                        except Exception as e:
                            log.debug(f"区块广播失败: {e}")
                
                self.consensus_engine.start_mining(miner_address, on_block=on_block_mined)
        
        # 保存事件循环引用（用于挖矿线程安全广播）
        self._event_loop = asyncio.get_event_loop()
        
        # 运行 P2P（如果可用）
        if self.p2p_node:
            # 创建初始同步任务
            async def initial_sync():
                """P2P 连接建立后同步区块。"""
                await asyncio.sleep(8)  # 等待 bootstrap 连接建立
                if self.p2p_node and self.p2p_node.peers:
                    our_height = self.consensus_engine.get_chain_height()
                    log.info(f"开始区块同步，当前高度 #{our_height}")
                    peer = list(self.p2p_node.peers.values())[0]
                    req = P2PMessage(
                        msg_type=MessageType.GET_BLOCKS,
                        sender_id=self.p2p_node.node_id,
                        payload={"start_height": our_height + 1}
                    )
                    await peer.send(req)
                else:
                    log.info("暂无 P2P 节点可同步，等待连接...")
            
            asyncio.ensure_future(initial_sync())
            await self.p2p_node.start()
        else:
            log.warn("P2P 未启用，节点仅提供 RPC 服务")
            # 保持进程运行
            try:
                while self.is_running:
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                pass
    
    def run(self):
        """运行节点（阻塞），带崩溃恢复。"""
        max_retries = 3
        retry_count = 0
        while retry_count < max_retries:
            try:
                asyncio.run(self.run_async())
                break  # 正常退出
            except KeyboardInterrupt:
                self.stop()
                break
            except Exception as e:
                retry_count += 1
                log.error(f"节点异常退出 ({retry_count}/{max_retries}): {e}")
                if retry_count < max_retries:
                    log.info(f"5秒后重启...")
                    time.sleep(5)
                    # 重新初始化
                    try:
                        self.initialize()
                    except Exception as ie:
                        log.error(f"重启初始化失败: {ie}")
                        break
                else:
                    log.error("达到最大重试次数，节点停止")
                    self.stop()
    
    def stop(self):
        """优雅停止节点，按反序关闭所有组件。"""
        log.info("正在停止节点...")
        self.is_running = False
        
        # 1. 停止挖矿
        if self.consensus_engine:
            self.consensus_engine.stop()
        
        # 2. 停止 RPC 服务器
        if self.rpc_server:
            try:
                self.rpc_server.stop()
                log.debug("RPC 服务已停止")
            except Exception as e:
                log.debug(f"RPC 停止异常: {e}")
        
        # 3. 停止 P2P 网络
        if self.p2p_node:
            try:
                import asyncio
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        loop.create_task(self.p2p_node.stop())
                    else:
                        loop.run_until_complete(self.p2p_node.stop())
                except RuntimeError:
                    asyncio.run(self.p2p_node.stop())
                log.debug("P2P 网络已停止")
            except Exception as e:
                log.debug(f"P2P 停止异常: {e}")
        
        # 4. 关闭扩展模块 (反序)
        for name, mod in [
            ("compute_market", self.compute_market),
            ("governance_engine", self.governance_engine),
            ("cross_scheduler", self.cross_scheduler),
            ("compute_economy", self.compute_economy),
            ("privacy_manager", self.privacy_manager),
            ("miner_security", self.miner_security),
            ("cloud_manager", self.cloud_manager),
            ("compliance_engine", self.compliance_engine),
            ("audit_engine", self.audit_engine),
            ("notification_service", self.notification_service),
        ]:
            if mod and hasattr(mod, 'close'):
                try:
                    mod.close()
                except Exception:
                    pass
        
        # 5. 关闭存储
        if self.storage:
            self.storage.close()
        
        uptime = time.time() - self.start_time
        log.success(f"节点已优雅停止 (运行 {uptime:.1f} 秒)")
    
    def get_status(self) -> Dict:
        """获取节点状态。"""
        return {
            "is_running": self.is_running,
            "uptime": time.time() - self.start_time if self.start_time else 0,
            "sector": self.sector,
            "blocks_synced": self.blocks_synced,
            "txs_processed": self.txs_processed,
            "peers": self.p2p_node.get_stats() if self.p2p_node else {},
        }


# ============== CLI ==============

def print_banner():
    """打印启动横幅。"""
    banner = """
╔═══════════════════════════════════════════════════════════╗
║                                                           ║
║   ██████╗  ██████╗ ██╗   ██╗██╗    ██╗                   ║
║   ██╔══██╗██╔═══██╗██║   ██║██║    ██║                   ║
║   ██████╔╝██║   ██║██║   ██║██║ █╗ ██║                   ║
║   ██╔═══╝ ██║   ██║██║   ██║██║███╗██║                   ║
║   ██║     ╚██████╔╝╚██████╔╝╚███╔███╔╝                   ║
║   ╚═╝      ╚═════╝  ╚═════╝  ╚══╝╚══╝                    ║
║                                                           ║
║   Multi-Sector Chain - Proof of Useful Work              ║
║   Version 2.0.0                                          ║
║                                                           ║
╚═══════════════════════════════════════════════════════════╝
"""
    print(Fore.CYAN + banner + Style.RESET_ALL)


def parse_args():
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description="POUW Multi-Sector Chain 节点",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python main.py                    启动默认节点
  python main.py --port 9334        指定端口
  python main.py --mining           启用挖矿
  python main.py --config my.yaml   使用自定义配置
  python main.py --testnet          连接测试网
        """
    )
    
    parser.add_argument(
        "--config", "-c",
        default="config.yaml",
        help="配置文件路径 (默认: config.yaml)"
    )
    
    parser.add_argument(
        "--port", "-p",
        type=int,
        help="P2P 端口"
    )
    
    parser.add_argument(
        "--rpc-port",
        type=int,
        help="RPC 端口"
    )
    
    parser.add_argument(
        "--data-dir", "-d",
        help="数据目录"
    )
    
    parser.add_argument(
        "--mining", "-m",
        action="store_true",
        help="启用挖矿"
    )
    
    parser.add_argument(
        "--role", "-r",
        choices=["light", "full", "miner", "provider"],
        help="节点角色: light(轻节点), full(完整节点), miner(矿工), provider(算力提供者)"
    )
    
    parser.add_argument(
        "--miner-address",
        help="矿工地址"
    )
    
    parser.add_argument(
        "--bootstrap", "-b",
        action="append",
        help="Bootstrap 节点 (可多次指定)"
    )
    
    parser.add_argument(
        "--testnet",
        action="store_true",
        help="连接测试网"
    )
    
    parser.add_argument(
        "--ui",
        action="store_true",
        help="启动 Web UI"
    )
    
    parser.add_argument(
        "--debug",
        action="store_true",
        help="调试模式"
    )
    
    parser.add_argument(
        "--version", "-v",
        action="version",
        version="POUW Chain v2.0.0"
    )
    
    return parser.parse_args()


def main():
    """主入口。"""
    # 解析参数
    args = parse_args()
    
    # 打印横幅
    print_banner()
    
    # 加载配置
    config = load_config(args.config)
    
    # 命令行覆盖
    if args.port:
        config["network"]["p2p"]["port"] = args.port
    
    if args.rpc_port:
        config["network"]["rpc"]["port"] = args.rpc_port
    
    if args.data_dir:
        config["storage"]["data_dir"] = args.data_dir
    
    if args.role:
        config["node"]["role"] = args.role
        # 矿工和提供者角色自动启用挖矿
        if args.role in ["miner", "provider"]:
            config["mining"]["enabled"] = True
    
    if args.mining:
        config["mining"]["enabled"] = True
    
    if args.miner_address:
        config["mining"]["miner_address"] = args.miner_address
    
    if args.bootstrap:
        config["network"]["p2p"]["bootstrap_nodes"].extend(args.bootstrap)
    
    if args.testnet:
        config["network"]["type"] = "testnet"
        config["network"]["chain_id"] = 9334
        log.info("使用测试网配置")
    
    if args.debug:
        config["logging"]["level"] = "DEBUG"
    
    # 创建并启动节点
    node = POUWNode(config)
    
    # 注册信号处理 - 只在明确请求退出时才退出
    shutdown_requested = False
    def signal_handler(sig, frame):
        nonlocal shutdown_requested
        if shutdown_requested:
            # 第二次按 Ctrl+C，立即退出
            log.warn("强制退出...")
            sys.exit(1)
        shutdown_requested = True
        log.warn("收到退出信号，再次按 Ctrl+C 强制退出...")
        node.stop()
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    # Windows 不支持 SIGTERM
    if hasattr(signal, 'SIGTERM'):
        try:
            signal.signal(signal.SIGTERM, signal_handler)
        except (OSError, ValueError) as e:
            # 信号注册失败（可能在线程中调用）
            pass
    
    # 初始化
    node.initialize()
    
    # 启动 UI（如果请求）
    if args.ui:
        log.info("启动 Web UI...")
        # 前端已迁移到 React SPA，通过 RPC Server 提供静态文件服务
        rpc_port = config.get('network', {}).get('rpc', {}).get('port', 8545)
        log.info(f"Web UI: http://localhost:{rpc_port}")
    
    # 运行
    node.run()


if __name__ == "__main__":
    main()
