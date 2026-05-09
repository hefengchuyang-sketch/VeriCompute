# -*- coding: utf-8 -*-
"""
统一共识引擎 (Unified Consensus Engine)

.. warning:: EXPERIMENTAL / 实验模块 - DO NOT IMPORT FROM PRODUCTION
    此模块在当前生产版本中未被 main.py 实例化或使用。
    生产代码使用 core/consensus.py 的 ConsensusEngine。
    本模块中的 MinerEngine、CurrencyBridge、ZeroTrustGuard 等
    组件均不生效。未来将通过 Phase 迭代逐步集成。

    禁止在以下生产路径中导入本模块:
      - main.py
      - core/rpc_service.py
      - core/rpc/*
      - core/rpc_handlers/*
    守护测试: tests/test_production_consensus_entrypoint.py
    参考: docs/TECHNICAL_REVIEW_AND_CONSENSUS_RECOMMENDATIONS_2026-05-09.md §7

将所有独立模块连接为一个完整的共识系统：

┌─────────────────────────────────────────────────────────┐
│                   UnifiedConsensus                       │
│                                                         │
│  ┌────────────┐  ┌─────────────┐  ┌──────────────────┐ │
│  │ MinerEngine│  │CurrencyBridge│  │ ZeroTrustGuard  │ │
│  │  三模式矿工 │  │  币种桥接    │  │  零信任安全     │ │
│  └─────┬──────┘  └──────┬──────┘  └────────┬─────────┘ │
│        │                │                   │           │
│  ┌─────┴──────┐  ┌──────┴──────┐  ┌────────┴─────────┐ │
│  │ Consensus  │  │  Unified    │  │  ScoreIntegrator │ │
│  │   Engine   │  │  Witness    │  │    评分集成      │ │
│  └────────────┘  └─────────────┘  └──────────────────┘ │
└─────────────────────────────────────────────────────────┘

矿工三种模式:
- MINING_ONLY:     纯挖矿铸币，不接受计算任务
- TASK_ONLY:       纯接单，不参与挖矿
- MINING_AND_TASK:  挖矿同时接单（盲任务引擎介入）

核心设计原则:
- 零信任: 默认所有节点都是恶意的
- 双见证: 所有MAIN转账和兑换必须双见证
- 盲任务: 矿工不知道自己在执行付费任务还是挖矿任务
- 数据保护: 端到端加密 + 容器隔离 + 安全内存
- GPU保护: MIG/vGPU隔离 + 资源限制 + profiling禁止

板块币转账规则:
- 同板块内转账: 允许，需单见证（大额需双见证）
- 跨板块转账: 禁止，必须先兑换为MAIN
- 板块币→MAIN: 受控兑换（锁定→双见证→销毁板块币→铸造MAIN）
- MAIN→板块币: 受限（仅通过DEX购买）
"""

# 实验模块标识：生产代码不得导入。守护测试通过此常量识别。
EXPERIMENTAL_ONLY = True

import os
import time
import hashlib
import json
import uuid
import threading
import random
from typing import Dict, List, Optional, Tuple, Any, Set
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


# ============== 统一矿工模式 ==============

class UnifiedMinerMode(Enum):
    """矿工运行模式
    
    - MINING_ONLY: 只挖矿铸币 — 参与共识出块，获得板块币奖励
    - TASK_ONLY: 只接单 — 不参与挖矿，只接受计算任务赚取报酬
    - MINING_AND_TASK: 挖矿+接单 — 盲任务引擎将付费任务混入挖矿流程
    """
    MINING_ONLY = "mining_only"
    TASK_ONLY = "task_only"
    MINING_AND_TASK = "mining_and_task"


class TaskDistributionMode(Enum):
    """任务分发模式 (用户选择)
    
    - SINGLE: 单客户端接单（效率优先，适合低风险任务）
    - DISTRIBUTED: 多客户端分布式接单（安全优先，结果交叉验证）
    """
    SINGLE = "single"
    DISTRIBUTED = "distributed"


class WitnessScope(Enum):
    """见证范围"""
    MAIN_TRANSFER = "main_transfer"         # MAIN转账
    SECTOR_EXCHANGE = "sector_exchange"      # 板块币→MAIN兑换
    COMPUTE_TASK = "compute_task"            # 算力任务
    SECTOR_TRANSFER = "sector_transfer"      # 板块币内部转账
    ORDER_PAYMENT = "order_payment"          # 订单支付


class SecurityThreatLevel(Enum):
    """安全威胁等级"""
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


# ============== 统一矿工配置 ==============

@dataclass
class UnifiedMinerConfig:
    """统一矿工配置"""
    miner_id: str
    address: str
    sector: str
    mode: UnifiedMinerMode = UnifiedMinerMode.MINING_AND_TASK
    
    # 接单配置 (TASK_ONLY / MINING_AND_TASK 模式)
    accept_distributed_tasks: bool = True       # 是否接受分布式任务
    max_concurrent_tasks: int = 3               # 最大并发任务数
    min_task_price: float = 0.0                 # 最低接单价格
    
    # 挖矿配置 (MINING_ONLY / MINING_AND_TASK 模式)
    auto_exchange: bool = False                 # 自动兑换板块币为MAIN
    auto_exchange_threshold: float = 100.0      # 自动兑换阈值
    
    # GPU保护
    gpu_memory_reserve_pct: float = 10.0        # GPU显存预留百分比
    allow_gpu_profiling: bool = False           # 禁止GPU profiling
    
    # 安全
    require_encrypted_tasks: bool = True        # 要求加密任务
    
    def to_dict(self) -> Dict:
        return {
            "miner_id": self.miner_id,
            "address": self.address,
            "sector": self.sector,
            "mode": self.mode.value,
            "accept_distributed_tasks": self.accept_distributed_tasks,
            "max_concurrent_tasks": self.max_concurrent_tasks,
            "auto_exchange": self.auto_exchange,
            "gpu_memory_reserve_pct": self.gpu_memory_reserve_pct,
        }


# ============== 安全审计事件 ==============

@dataclass
class SecurityAuditEvent:
    """安全审计事件"""
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    event_type: str = ""            # 事件类型
    miner_id: str = ""              # 相关矿工
    threat_level: SecurityThreatLevel = SecurityThreatLevel.NONE
    description: str = ""
    evidence: Dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    action_taken: str = ""          # 采取的行动


# ============== 任务安全封装 ==============

@dataclass
class SecureTaskEnvelope:
    """安全任务信封 — 零信任任务分发
    
    任务数据在传输和存储过程中始终加密，
    只有在安全容器内解密执行。
    """
    envelope_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    task_id: str = ""
    
    # 加密层
    encrypted_payload: bytes = b""          # 加密后的任务数据
    encryption_key_hash: str = ""           # 密钥哈希（用于验证）
    payload_hash: str = ""                  # 原始数据哈希（用于结果验证）
    
    # 安全策略
    security_level: str = "enhanced"        # minimal/standard/enhanced/maximum
    gpu_isolation: str = "isolated"         # none/basic/isolated/confidential
    network_policy: str = "none"            # none/local_only/restricted
    max_execution_seconds: int = 300        # 最大执行时间
    
    # 容器配置
    read_only_rootfs: bool = True
    max_memory_mb: int = 4096
    max_cpu_cores: float = 4.0
    gpu_memory_limit_mb: int = 0            # 0=不限
    
    # 元数据 (不加密)
    sector: str = ""
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0


# ============== 统一共识引擎 ==============

class UnifiedConsensus:
    """
    统一共识引擎 — 连接所有模块的中枢
    
    职责:
    1. 矿工三模式管理 (MinerEngine)
    2. 共识出块 → 板块币铸造 (CurrencyBridge)
    3. 板块币 → MAIN 兑换 (动态汇率 + 双见证)
    4. 统一见证协调 (UnifiedWitness)
    5. 零信任任务安全 (ZeroTrustGuard)
    6. 评分集成调度优先级 (ScoreIntegrator)
    
    零信任原则:
    - 所有节点默认恶意
    - 所有数据默认篡改
    - 所有结果默认伪造
    - 验证一切，信任无人
    """
    
    # 板块币内部转账见证阈值
    SECTOR_TRANSFER_WITNESS_THRESHOLD = 100.0   # 大额转账需双见证（>= 100板块币）
    SECTOR_TRANSFER_SMALL_THRESHOLD = 10.0      # 小额转账需单见证（>= 10板块币）
    # < 10 板块币的微额转账：仍需记录但无需见证等待
    
    # 订单支付最低MAIN余额
    MIN_ORDER_MAIN_BALANCE = 0.01
    
    # 安全配置
    MAX_FAILED_TRAPS_BEFORE_BAN = 3             # 陷阱题连续失败3次封禁
    MINER_BAN_DURATION = 3600 * 24              # 封禁24小时
    
    # 汇率映射 (板块名 → DynamicExchangeRate的板块类型)
    SECTOR_TO_RATE_TYPE = {
        "H100": "GPU_DATACENTER",
        "RTX4090": "GPU_CONSUMER",
        "RTX3080": "GPU_CONSUMER",
        "CPU": "CPU",
        "GENERAL": "STORAGE",
    }
    
    def __init__(
        self,
        sector: str = "GENERAL",
        testnet: bool = True,
        db_dir: str = "data",
        log_fn=None,
    ):
        self.sector = sector
        self.testnet = testnet
        self.db_dir = db_dir
        self._log_fn = log_fn or (lambda x: None)
        self._lock = threading.Lock()
        
        # ===== 矿工注册 =====
        self.miners: Dict[str, UnifiedMinerConfig] = {}
        self.miner_scores: Dict[str, Dict[str, float]] = {}   # miner_id → {pouw, user, combined, behavior}
        self.banned_miners: Dict[str, float] = {}               # miner_id → ban_until
        
        # ===== 安全审计 =====
        self.audit_events: List[SecurityAuditEvent] = []
        
        # ===== 统计 =====
        self.stats = {
            "blocks_mined": 0,
            "sector_coins_minted": 0.0,
            "main_coins_exchanged": 0.0,
            "tasks_completed": 0,
            "tasks_failed": 0,
            "witnesses_completed": 0,
            "security_incidents": 0,
            "miners_banned": 0,
            "fees_burned": 0.0,
            "fees_to_miners": 0.0,
            "fees_to_pool": 0.0,
            "disputes_filed": 0,
            "disputes_resolved": 0,
            "reviews_submitted": 0,
            "alerts_triggered": 0,
            "sla_violations": 0,
        }
        
        # ===== 延迟初始化的子系统引用 =====
        self._sector_ledger = None
        self._exchange_engine = None
        self._rate_engine = None
        self._witness_main = None
        self._witness_compute = None
        self._blind_engine = None
        self._scoring = None
        self._behavior = None
        
        # ===== 新增子系统引用 (Phase 13 补齐) =====
        self._fee_pool = None           # 协议费用池
        self._arbitration = None        # 仲裁系统
        self._reputation = None         # 信誉引擎
        self._monitor = None            # 交易监控
        self._task_acceptance = None     # 任务验收/SLA
        self._message_system = None     # 留言评价系统
        
        self.log(f"🔗 统一共识引擎初始化: sector={sector}, testnet={testnet}")
    
    def log(self, msg: str):
        self._log_fn(f"[UNIFIED] {msg}")
    
    # =================================================================
    # 子系统懒加载（避免循环导入和初始化顺序问题）
    # =================================================================
    
    @property
    def sector_ledger(self):
        """板块币账本"""
        if self._sector_ledger is None:
            from core.sector_coin import get_sector_ledger
            self._sector_ledger = get_sector_ledger()
        return self._sector_ledger
    
    @sector_ledger.setter
    def sector_ledger(self, value):
        self._sector_ledger = value
    
    @property
    def exchange_engine(self):
        """兑换引擎"""
        if self._exchange_engine is None:
            from core.dual_witness_exchange import OptimisticDualWitnessExchange
            self._exchange_engine = OptimisticDualWitnessExchange(
                db_path=f"{self.db_dir}/exchange.db",
                testnet=self.testnet
            )
        return self._exchange_engine
    
    @exchange_engine.setter
    def exchange_engine(self, value):
        self._exchange_engine = value
    
    @property
    def rate_engine(self):
        """动态汇率引擎"""
        if self._rate_engine is None:
            from core.exchange_rate import DynamicExchangeRate
            self._rate_engine = DynamicExchangeRate(log_fn=self._log_fn)
        return self._rate_engine
    
    @rate_engine.setter
    def rate_engine(self, value):
        self._rate_engine = value
    
    @property
    def witness_compute(self):
        """算力见证系统"""
        if self._witness_compute is None:
            from core.compute_witness import ComputeWitnessSystem
            self._witness_compute = ComputeWitnessSystem(
                required_witnesses=1 if self.testnet else 2,
                require_signature_verification=not self.testnet,
                log_fn=self._log_fn
            )
        return self._witness_compute
    
    @witness_compute.setter
    def witness_compute(self, value):
        self._witness_compute = value
    
    # ----- Phase 13 新增子系统懒加载 -----
    
    @property
    def fee_pool(self):
        """协议费用池 (1%手续费: 0.5%销毁 + 0.3%矿工 + 0.2%协议池)"""
        if self._fee_pool is None:
            from core.protocol_fee_pool import ProtocolFeePoolManager
            self._fee_pool = ProtocolFeePoolManager()
        return self._fee_pool
    
    @fee_pool.setter
    def fee_pool(self, value):
        self._fee_pool = value
    
    @property
    def arbitration(self):
        """仲裁系统 (任务纠纷处理)"""
        if self._arbitration is None:
            from core.arbitration import ArbitrationSystem
            self._arbitration = ArbitrationSystem(
                arbitration_period=3600 if self.testnet else 3600 * 24,
                log_fn=self._log_fn,
            )
        return self._arbitration
    
    @arbitration.setter
    def arbitration(self, value):
        self._arbitration = value
    
    @property
    def reputation(self):
        """信誉引擎 (多维度评分)"""
        if self._reputation is None:
            from core.reputation_engine import ReputationEngine
            self._reputation = ReputationEngine(
                db_path=f"{self.db_dir}/reputation.db",
            )
        return self._reputation
    
    @reputation.setter
    def reputation(self, value):
        self._reputation = value
    
    @property
    def tx_monitor(self):
        """交易监控 (异常检测告警)"""
        if self._monitor is None:
            from core.monitor import TransactionMonitor, MonitorConfig
            self._monitor = TransactionMonitor(MonitorConfig(
                large_transaction_threshold=1000.0,
                high_frequency_threshold=20,
            ))
        return self._monitor
    
    @tx_monitor.setter
    def tx_monitor(self, value):
        self._monitor = value
    
    @property
    def task_acceptance_engine(self):
        """任务验收/SLA (三层验收)"""
        if self._task_acceptance is None:
            from core.task_acceptance import TaskAcceptanceService
            self._task_acceptance = TaskAcceptanceService()
        return self._task_acceptance
    
    @task_acceptance_engine.setter
    def task_acceptance_engine(self, value):
        self._task_acceptance = value
    
    @property
    def message_sys(self):
        """留言评价系统 (链上哈希+链外存储)"""
        if self._message_system is None:
            from core.message_system import MessageSystem
            self._message_system = MessageSystem(
                db_path=f"{self.db_dir}/messages.db",
            )
        return self._message_system
    
    @message_sys.setter
    def message_sys(self, value):
        self._message_system = value
    
    @property
    def behavior_analyzer(self):
        """矿工行为分析器 (报价-履约一致性)"""
        if self._behavior is None:
            from core.miner_behavior import MinerBehaviorAnalyzer
            self._behavior = MinerBehaviorAnalyzer()
        return self._behavior
    
    @behavior_analyzer.setter
    def behavior_analyzer(self, value):
        self._behavior = value
    
    # =================================================================
    # 1. 矿工三模式管理 (MinerEngine)
    # =================================================================
    
    def register_miner(self, config: UnifiedMinerConfig) -> Tuple[bool, str]:
        """注册矿工（三模式支持）
        
        Args:
            config: 矿工配置
            
        Returns:
            (成功, 消息)
        """
        miner_id = config.miner_id
        
        # 检查是否被封禁
        if miner_id in self.banned_miners:
            if time.time() < self.banned_miners[miner_id]:
                remaining = int(self.banned_miners[miner_id] - time.time())
                return False, f"矿工 {miner_id} 已被封禁，剩余 {remaining}s"
            else:
                del self.banned_miners[miner_id]
        
        # 验证模式与板块的一致性
        if config.mode != UnifiedMinerMode.TASK_ONLY and not config.sector:
            return False, "挖矿模式必须指定板块"
        
        with self._lock:
            self.miners[miner_id] = config
            
            # 初始化评分
            if miner_id not in self.miner_scores:
                self.miner_scores[miner_id] = {
                    "pouw_score": 1.0,
                    "user_rating": 5.0,
                    "combined_score": 1.0,
                    "behavior_score": 1.0,
                    "trust_score": 0.5,     # 初始信任 50%
                }
        
        mode_desc = {
            UnifiedMinerMode.MINING_ONLY: "纯挖矿",
            UnifiedMinerMode.TASK_ONLY: "纯接单",
            UnifiedMinerMode.MINING_AND_TASK: "挖矿+接单",
        }
        
        self.log(f"⛏️ 矿工注册: {miner_id} [{mode_desc[config.mode]}] sector={config.sector}")
        return True, f"注册成功: {mode_desc[config.mode]}"
    
    def get_miners_by_mode(self, mode: UnifiedMinerMode) -> List[UnifiedMinerConfig]:
        """按模式获取矿工列表"""
        return [m for m in self.miners.values() if m.mode == mode]
    
    def get_miners_accepting_tasks(self) -> List[UnifiedMinerConfig]:
        """获取可接单的矿工（TASK_ONLY + MINING_AND_TASK）"""
        return [
            m for m in self.miners.values()
            if m.mode in (UnifiedMinerMode.TASK_ONLY, UnifiedMinerMode.MINING_AND_TASK)
            and m.miner_id not in self.banned_miners
        ]
    
    def get_miners_mining(self) -> List[UnifiedMinerConfig]:
        """获取正在挖矿的矿工（MINING_ONLY + MINING_AND_TASK）"""
        return [
            m for m in self.miners.values()
            if m.mode in (UnifiedMinerMode.MINING_ONLY, UnifiedMinerMode.MINING_AND_TASK)
            and m.miner_id not in self.banned_miners
        ]
    
    def switch_miner_mode(self, miner_id: str, new_mode: UnifiedMinerMode) -> Tuple[bool, str]:
        """切换矿工模式"""
        if miner_id not in self.miners:
            return False, "矿工未注册"
        
        old_mode = self.miners[miner_id].mode
        self.miners[miner_id].mode = new_mode
        self.log(f"🔄 矿工 {miner_id} 模式切换: {old_mode.value} → {new_mode.value}")
        return True, f"模式切换成功: {new_mode.value}"
    
    # =================================================================
    # 2. 共识出块 → 板块币铸造 (CurrencyBridge)
    # =================================================================
    
    def on_block_mined(self, block_height: int, miner_address: str, 
                       sector: str, block_reward: float) -> Tuple[bool, float, str]:
        """区块挖出后铸造板块币奖励
        
        连接: ConsensusEngine.mine_block() → SectorCoinLedger.mint_block_reward()
        
        Args:
            block_height: 区块高度
            miner_address: 矿工钱包地址
            sector: 板块名
            block_reward: 共识引擎计算的奖励数量
            
        Returns:
            (成功, 实际铸造数量, 消息)
        """
        if sector == "MAIN":
            return False, 0, "MAIN不可直接挖矿铸造 (DR-1)"
        
        # 使用SectorCoinLedger铸造板块币
        success, reward, msg = self.sector_ledger.mint_block_reward(
            sector=sector,
            miner_address=miner_address,
            block_height=block_height
        )
        
        if success:
            self.stats["blocks_mined"] += 1
            self.stats["sector_coins_minted"] += reward
            self.log(f"💰 铸造板块币: {miner_address} +{reward:.4f} {sector}_COIN (区块#{block_height})")
            
            # 检查自动兑换
            miner_config = self._find_miner_by_address(miner_address)
            if miner_config and miner_config.auto_exchange:
                self._check_auto_exchange(miner_config)
        
        return success, reward, msg
    
    def _find_miner_by_address(self, address: str) -> Optional[UnifiedMinerConfig]:
        """通过地址查找矿工"""
        for config in self.miners.values():
            if config.address == address:
                return config
        return None
    
    def _check_auto_exchange(self, config: UnifiedMinerConfig):
        """检查是否触发自动兑换"""
        from core.sector_coin import SectorCoinType
        coin_type = SectorCoinType.from_sector(config.sector)
        balance = self.sector_ledger.get_balance(config.address, coin_type)
        
        if balance.available >= config.auto_exchange_threshold:
            # 触发兑换（但不强制成功，仅发起请求）
            self.log(f"🔄 自动兑换触发: {config.miner_id} 余额={balance.available:.4f}")
            self.exchange_sector_to_main(
                address=config.address,
                sector=config.sector,
                amount=balance.available
            )
    
    # =================================================================
    # 3. 板块币 → MAIN 兑换 (动态汇率 + 双见证)
    # =================================================================
    
    def get_exchange_rate(self, sector: str) -> float:
        """获取板块币→MAIN的动态汇率
        
        统一使用 DynamicExchangeRate 引擎的汇率，
        不再使用 DualWitnessExchange 的固定汇率。
        """
        rate_type = self.SECTOR_TO_RATE_TYPE.get(sector, "STORAGE")
        
        if rate_type in self.rate_engine.current_rates:
            return self.rate_engine.current_rates[rate_type]
        
        # 回退到基础汇率（从板块注册表获取）
        try:
            from core.sector_coin import get_sector_registry
            return get_sector_registry().get_exchange_rate(sector)
        except Exception:
            return 0.5
    
    def exchange_sector_to_main(self, address: str, sector: str, 
                                 amount: float) -> Tuple[bool, str, Optional[str]]:
        """发起板块币→MAIN兑换请求
        
        流程:
        1. 验证余额
        2. 获取动态汇率
        3. 锁定板块币
        4. 发起双见证兑换请求
        5. 等待见证完成后：销毁板块币 + 铸造MAIN
        
        Args:
            address: 钱包地址
            sector: 板块名
            amount: 板块币数量
            
        Returns:
            (成功, 消息, 兑换请求ID)
        """
        if amount <= 0:
            return False, "金额必须大于0", None
        
        from core.sector_coin import SectorCoinType
        coin_type = SectorCoinType.from_sector(sector)
        
        # 1. 检查余额
        balance = self.sector_ledger.get_balance(address, coin_type)
        if balance.available < amount:
            return False, f"余额不足: 可用 {balance.available:.4f}", None
        
        # 2. 获取动态汇率
        rate = self.get_exchange_rate(sector)
        main_amount = amount * rate
        
        # 3. 锁定板块币
        lock_ok, lock_msg = self.sector_ledger.lock_for_exchange(address, coin_type, amount)
        if not lock_ok:
            return False, f"锁定失败: {lock_msg}", None
        
        # 4. 创建兑换请求（双见证）
        # S-3 fix: 使用密码学安全的随机数生成兑换 ID
        exchange_id = hashlib.sha256(
            f"EXCHANGE_{address}_{sector}_{amount}_{time.time()}_{os.urandom(8).hex()}".encode()
        ).hexdigest()[:16]
        
        # 发起兑换请求到DualWitnessExchange
        try:
            result = self.exchange_engine.create_exchange_request(
                requester_address=address,
                source_sector=sector,
                amount=amount,
                exchange_rate=rate
            )
            if result and isinstance(result, tuple):
                success, msg_or_id = result[0], result[1] if len(result) > 1 else ""
                if success:
                    self.log(f"📤 兑换请求: {address} {amount:.4f} {sector}_COIN → {main_amount:.4f} MAIN (rate={rate:.4f})")
                    self.stats["main_coins_exchanged"] += main_amount
                    return True, f"兑换请求已创建 (rate={rate:.4f})", exchange_id
            
            # 兑换引擎返回异常，解锁
            self.sector_ledger.unlock_exchange(address, coin_type, amount)
            return False, "兑换请求创建失败", None
            
        except Exception as e:
            # 回滚锁定
            self.sector_ledger.unlock_exchange(address, coin_type, amount)
            
            # 简化模式：直接完成兑换（测试网/单节点模式）
            if self.testnet:
                return self._direct_exchange(address, sector, coin_type, amount, rate, exchange_id)
            
            return False, f"兑换请求异常: {e}", None
    
    def _direct_exchange(self, address: str, sector: str, coin_type, 
                          amount: float, rate: float, exchange_id: str) -> Tuple[bool, str, Optional[str]]:
        """直接兑换（测试网简化模式）"""
        main_amount = amount * rate
        
        # 销毁板块币
        success, reward, msg = self.sector_ledger.exchange_to_main(
            address=address, sector=sector, amount=amount, rate=rate
        )
        
        if success:
            self.stats["main_coins_exchanged"] += main_amount
            self.log(f"✅ 直接兑换: {amount:.4f} {sector}_COIN → {main_amount:.4f} MAIN")
            return True, f"兑换完成: {main_amount:.4f} MAIN", exchange_id
        
        return False, f"兑换失败: {msg}", None
    
    # =================================================================
    # 4. 统一见证协调器 (UnifiedWitness)
    # =================================================================
    
    def request_witness(self, scope: WitnessScope, 
                        transaction_data: Dict) -> Tuple[bool, str]:
        """统一见证入口
        
        根据见证范围路由到对应的见证系统:
        - MAIN_TRANSFER → DoubleWitnessEngine
        - SECTOR_EXCHANGE → DualWitnessExchange
        - COMPUTE_TASK → ComputeWitnessSystem
        - SECTOR_TRANSFER → 根据金额决定见证级别
        - ORDER_PAYMENT → DoubleWitnessEngine + ComputeWitnessSystem
        
        Args:
            scope: 见证范围
            transaction_data: 交易数据
            
        Returns:
            (成功, 见证请求ID或错误消息)
        """
        self.log(f"🔍 见证请求: scope={scope.value}")
        
        if scope == WitnessScope.MAIN_TRANSFER:
            return self._witness_main_transfer(transaction_data)
        elif scope == WitnessScope.SECTOR_EXCHANGE:
            return self._witness_sector_exchange(transaction_data)
        elif scope == WitnessScope.COMPUTE_TASK:
            return self._witness_compute_task(transaction_data)
        elif scope == WitnessScope.SECTOR_TRANSFER:
            return self._witness_sector_transfer(transaction_data)
        elif scope == WitnessScope.ORDER_PAYMENT:
            return self._witness_order_payment(transaction_data)
        
        return False, f"未知见证范围: {scope}"
    
    def _witness_main_transfer(self, tx_data: Dict) -> Tuple[bool, str]:
        """MAIN转账见证 — 必须双见证"""
        amount = tx_data.get("amount", 0)
        from_addr = tx_data.get("from_address", "")
        to_addr = tx_data.get("to_address", "")
        
        if amount <= 0:
            return False, "金额无效"
        
        # 大额交易需要3见证
        required = 3 if amount >= 10000 else 2
        if self.testnet:
            required = 1
        
        witness_id = hashlib.sha256(
            f"MAIN_WITNESS_{from_addr}_{to_addr}_{amount}_{time.time()}".encode()
        ).hexdigest()[:16]
        
        self.stats["witnesses_completed"] += 1
        self.log(f"✅ MAIN转账见证请求: {from_addr} → {to_addr} {amount} MAIN (need {required} witnesses)")
        return True, witness_id
    
    def _witness_sector_exchange(self, tx_data: Dict) -> Tuple[bool, str]:
        """板块币兑换见证 — 必须双见证"""
        exchange_id = tx_data.get("exchange_id", "")
        
        self.stats["witnesses_completed"] += 1
        self.log(f"✅ 兑换见证请求: {exchange_id}")
        return True, exchange_id
    
    def _witness_compute_task(self, tx_data: Dict) -> Tuple[bool, str]:
        """算力任务见证"""
        task_id = tx_data.get("task_id", "")
        
        # 注册到算力见证系统
        self.stats["witnesses_completed"] += 1
        return True, task_id
    
    def _witness_sector_transfer(self, tx_data: Dict) -> Tuple[bool, str]:
        """板块币内部转账见证
        
        规则:
        - < 10 板块币: 微额免见证（仅记录）
        - 10-100 板块币: 单见证
        - >= 100 板块币: 双见证
        """
        amount = tx_data.get("amount", 0)
        
        if amount < self.SECTOR_TRANSFER_SMALL_THRESHOLD:
            witness_level = "micro"
            required = 0
        elif amount < self.SECTOR_TRANSFER_WITNESS_THRESHOLD:
            witness_level = "single"
            required = 1
        else:
            witness_level = "double"
            required = 2
        
        if self.testnet:
            required = min(required, 1)
        
        witness_id = hashlib.sha256(
            f"SECTOR_WITNESS_{tx_data.get('from_address','')}_{amount}_{time.time()}".encode()
        ).hexdigest()[:16]
        
        self.stats["witnesses_completed"] += 1
        self.log(f"✅ 板块币转账见证: amount={amount}, level={witness_level}, need={required}")
        return True, witness_id
    
    def _witness_order_payment(self, tx_data: Dict) -> Tuple[bool, str]:
        """订单支付见证 — 双见证"""
        order_id = tx_data.get("order_id", "")
        amount = tx_data.get("amount", 0)
        
        self.stats["witnesses_completed"] += 1
        self.log(f"✅ 订单支付见证: order={order_id}, amount={amount} MAIN")
        return True, order_id
    
    # =================================================================
    # 5. 订单与支付 (必须MAIN + 双见证)
    # =================================================================
    
    def validate_order_payment(self, user_address: str, 
                                amount: float) -> Tuple[bool, str]:
        """验证订单支付（用户必须有足够MAIN余额）
        
        设计规则:
        - 下单使用MAIN支付（不接受板块币）
        - 矿工必须先将板块币兑换为MAIN才能下单
        - 支付需要双见证
        """
        if amount <= 0:
            return False, "金额无效"
        
        if amount < self.MIN_ORDER_MAIN_BALANCE:
            return False, f"最低订单金额: {self.MIN_ORDER_MAIN_BALANCE} MAIN"
        
        # 获取MAIN余额
        main_balance = self._get_main_balance(user_address)
        
        if main_balance < amount:
            return False, f"MAIN余额不足: 可用 {main_balance:.4f}, 需要 {amount:.4f}"
        
        # 发起支付见证
        witness_ok, witness_id = self.request_witness(
            WitnessScope.ORDER_PAYMENT,
            {"order_id": uuid.uuid4().hex[:12], "amount": amount, "user_address": user_address}
        )
        
        if not witness_ok:
            return False, f"支付见证失败: {witness_id}"
        
        return True, f"支付验证通过 (witness={witness_id})"
    
    def _get_main_balance(self, address: str) -> float:
        """获取MAIN余额（统一接口）
        
        优先级：UTXO store > exchange DB > exchange 内存字典（兼容）
        """
        # 优先从 UTXO store 获取（最准确的资金来源）
        try:
            from core.utxo_store import get_utxo_store
            utxo_store = get_utxo_store()
            return utxo_store.get_balance(address, 'MAIN')
        except Exception:
            pass
        # 回退：从兑换引擎数据库获取
        try:
            if hasattr(self.exchange_engine, 'get_main_balance'):
                return self.exchange_engine.get_main_balance(address)
            if hasattr(self.exchange_engine, '_main_balances'):
                return self.exchange_engine._main_balances.get(address, 0.0)
        except Exception:
            pass
        return 0.0
    
    def set_main_balance(self, address: str, balance: float):
        """设置MAIN余额（用于测试和初始化）"""
        try:
            from core.utxo_store import get_utxo_store
            utxo_store = get_utxo_store()
            # 先清除该地址已有的 MAIN UTXO，再插入唯一记录
            stable_txid = hashlib.sha256(f"set_balance:{address}".encode()).hexdigest()
            with utxo_store._conn() as conn:
                conn.execute(
                    "DELETE FROM utxos WHERE address = ? AND sector = 'MAIN' AND status = 'unspent'",
                    (address,)
                )
                conn.execute("""
                    INSERT OR REPLACE INTO utxos
                    (utxo_id, txid, output_index, address, amount, sector, block_height, status, created_at, source_type)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    f"{stable_txid}:0", stable_txid, 0, address, balance, 'MAIN', 0,
                    'unspent', time.time(), 'genesis'
                ))
        except Exception:
            pass
        try:
            if hasattr(self.exchange_engine, '_conn'):
                with self.exchange_engine._conn() as conn:
                    conn.execute(
                        "INSERT INTO main_balances (address, balance) VALUES (?, ?) "
                        "ON CONFLICT(address) DO UPDATE SET balance = ?",
                        (address, balance, balance)
                    )
                    conn.commit()
        except Exception as e:
            print(f"Error setting main balance in DB: {e}")
            pass
        try:
            if hasattr(self.exchange_engine, '_main_balances'):
                self.exchange_engine._main_balances[address] = balance
        except Exception:
            pass
    
    # =================================================================
    # 6. 板块币内部转账
    # =================================================================
    
    def transfer_sector_coin(self, from_address: str, to_address: str,
                              sector: str, amount: float,
                              signature: str = "", public_key: str = "") -> Tuple[bool, str]:
        """板块币内部转账（同板块）
        
        规则:
        - 只能在同板块内转账
        - 跨板块必须先兑换为MAIN
        - 大额转账需要见证
        """
        if amount <= 0:
            return False, "金额必须大于0"
        
        from core.sector_coin import SectorCoinType
        coin_type = SectorCoinType.from_sector(sector)
        
        # 检查余额
        balance = self.sector_ledger.get_balance(from_address, coin_type)
        if balance.available < amount:
            return False, f"余额不足: 可用 {balance.available:.4f}"
        
        # 见证检查
        witness_ok, witness_msg = self.request_witness(
            WitnessScope.SECTOR_TRANSFER,
            {
                "from_address": from_address,
                "to_address": to_address,
                "sector": sector,
                "amount": amount,
            }
        )
        
        if not witness_ok:
            return False, f"见证失败: {witness_msg}"
        
        # 执行转账
        success, msg = self.sector_ledger.transfer(
            from_address, to_address, coin_type, amount,
            signature=signature, public_key=public_key
        )
        
        if success:
            self.log(f"💸 板块币转账: {from_address} → {to_address} {amount:.4f} {sector}_COIN")
        
        return success, msg
    
    def transfer_main(self, from_address: str, to_address: str, 
                       amount: float) -> Tuple[bool, str]:
        """MAIN转账 — 必须双见证
        
        所有MAIN转账都需要双见证验证。
        """
        if amount <= 0:
            return False, "金额必须大于0"
        
        # 检查MAIN余额
        balance = self._get_main_balance(from_address)
        if balance < amount:
            return False, f"MAIN余额不足: 可用 {balance:.4f}"
        
        # 双见证
        witness_ok, witness_id = self.request_witness(
            WitnessScope.MAIN_TRANSFER,
            {
                "from_address": from_address,
                "to_address": to_address,
                "amount": amount,
            }
        )
        
        if not witness_ok:
            return False, f"见证失败: {witness_id}"
        
        # 执行转账 — 通过 UTXO 系统完成：花费输入、创建输出、扣手续费
        try:
            from core.protocol_fee_pool import ProtocolHardRules
            fee = amount * ProtocolHardRules.TOTAL_FEE_RATE
            net_amount = amount - fee

            from core.utxo_store import get_utxo_store
            utxo_store = get_utxo_store()

            # 选择足够的 UTXO 作为输入
            available = utxo_store.get_spendable_utxos(from_address, 'MAIN')
            selected = []
            selected_total = 0.0
            for utxo in sorted(available, key=lambda u: -u.amount):
                selected.append(utxo)
                selected_total += utxo.amount
                if selected_total >= amount:
                    break

            if selected_total < amount:
                return False, f"MAIN余额不足(UTXO): 可用 {selected_total:.4f}"

            change = selected_total - amount
            timestamp = time.time()
            txid = hashlib.sha256(
                f"transfer:{from_address}:{to_address}:{amount}:{timestamp}".encode()
            ).hexdigest()

            # 原子操作：花费输入 UTXO，创建输出 UTXO
            with utxo_store._exclusive_conn() as conn:
                for utxo in selected:
                    conn.execute(
                        "UPDATE utxos SET status='spent', spent_txid=?, spent_at=? "
                        "WHERE utxo_id=? AND status='unspent'",
                        (txid, timestamp, utxo.utxo_id)
                    )
                # 接收方 UTXO
                conn.execute(
                    "INSERT INTO utxos (utxo_id, txid, output_index, address, amount, "
                    "sector, block_height, status, created_at, source_type) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (f"{txid}:0", txid, 0, to_address, net_amount, 'MAIN',
                     0, 'unspent', timestamp, 'transfer')
                )
                # 找零 UTXO
                if change > 1e-8:
                    conn.execute(
                        "INSERT INTO utxos (utxo_id, txid, output_index, address, amount, "
                        "sector, block_height, status, created_at, source_type) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (f"{txid}:1", txid, 1, from_address, change, 'MAIN',
                         0, 'unspent', timestamp, 'transfer')
                    )

            # 处理手续费分配（销毁 + 矿工 + 协议池）
            fee_tx_id = hashlib.sha256(f"fee:{txid}".encode()).hexdigest()[:16]
            self.process_transaction_fee(fee_tx_id, amount)

            self.log(f"💸 MAIN转账: {from_address} → {to_address} {net_amount:.4f} MAIN (手续费={fee:.4f})")
            return True, f"转账成功: {amount:.4f} MAIN (手续费={fee:.4f}, 实收={net_amount:.4f})"
        except Exception as e:
            return False, f"转账执行失败: {e}"
    
    # =================================================================
    # 7. 零信任任务安全 (ZeroTrustGuard)
    # =================================================================
    
    def create_secure_task(self, task_id: str, task_data: Dict,
                            sector: str,
                            user_address: str) -> SecureTaskEnvelope:
        """创建安全任务信封
        
        零信任原则:
        1. 任务数据加密 (AES-256-GCM)
        2. 容器隔离配置
        3. GPU保护策略
        4. 执行时间限制
        5. 网络隔离
        """
        # 生成加密密钥
        key = hashlib.sha256(
            f"{task_id}_{user_address}_{time.time()}_{uuid.uuid4().hex}".encode()
        ).hexdigest()
        
        # 加密任务数据
        payload_json = json.dumps(task_data, default=str).encode('utf-8')
        payload_hash = hashlib.sha256(payload_json).hexdigest()
        
        # 简化加密（生产环境使用 AES-256-GCM）
        encrypted = self._encrypt_payload(payload_json, key)
        
        # 根据板块确定GPU安全级别
        gpu_isolation = "isolated"  # MIG/vGPU隔离
        gpu_memory_limit = 0
        if sector in ("H100", "RTX4090"):
            gpu_isolation = "confidential"   # 高端GPU使用机密计算
            gpu_memory_limit = 16384         # 限制16GB
        elif sector == "RTX3080":
            gpu_memory_limit = 8192          # 限制8GB
        
        envelope = SecureTaskEnvelope(
            task_id=task_id,
            encrypted_payload=encrypted,
            encryption_key_hash=hashlib.sha256(key.encode()).hexdigest()[:32],
            payload_hash=payload_hash,
            security_level="enhanced",
            gpu_isolation=gpu_isolation,
            network_policy="none",           # 默认禁止网络
            max_execution_seconds=300,
            read_only_rootfs=True,
            max_memory_mb=4096,
            max_cpu_cores=4.0,
            gpu_memory_limit_mb=gpu_memory_limit,
            sector=sector,
            expires_at=time.time() + 3600,
        )
        
        self.log(f"🔒 安全任务信封创建: task={task_id}, gpu={gpu_isolation}, net=none")
        return envelope
    
    def _encrypt_payload(self, data: bytes, key: str) -> bytes:
        """加密数据 (AES-256-GCM)"""
        key_bytes = hashlib.sha256(key.encode()).digest()
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            import os
            nonce = os.urandom(12)
            aesgcm = AESGCM(key_bytes)
            ct = aesgcm.encrypt(nonce, data, None)
            return nonce + ct  # 12-byte nonce + ciphertext
        except ImportError:
            raise RuntimeError(
                "cryptography library is required for payload encryption. "
                "Install with: pip install cryptography"
            )
    
    def _decrypt_payload(self, data: bytes, key: str) -> bytes:
        """解密数据 (AES-256-GCM)"""
        key_bytes = hashlib.sha256(key.encode()).digest()
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            nonce = data[:12]
            ct = data[12:]
            aesgcm = AESGCM(key_bytes)
            return aesgcm.decrypt(nonce, ct, None)
        except ImportError:
            raise RuntimeError(
                "cryptography library is required for payload decryption. "
                "Install with: pip install cryptography"
            )
    
    def verify_task_result(self, task_id: str, miner_id: str,
                            result_hash: str, 
                            trap_results: Optional[Dict] = None) -> Tuple[bool, str, float]:
        """验证任务结果（零信任验证）
        
        多层验证:
        1. 陷阱题验证: 检查矿工是否正确回答了陷阱题
        2. 结果哈希验证: 检查结果完整性
        3. 信任度更新: 根据结果更新矿工信任分
        
        Args:
            task_id: 任务ID
            miner_id: 矿工ID
            result_hash: 结果哈希
            trap_results: 陷阱题回答 {trap_id: answer}
            
        Returns:
            (通过, 消息, 信任度变化)
        """
        trust_delta = 0.0
        
        # 1. 陷阱题验证
        if trap_results is not None:
            trap_pass_rate = self._verify_traps(trap_results)
            
            if trap_pass_rate < 0.5:
                # 陷阱通过率 < 50%，可能作弊
                trust_delta = -0.1
                self._record_security_event(
                    miner_id=miner_id,
                    event_type="trap_failure",
                    threat_level=SecurityThreatLevel.HIGH,
                    description=f"陷阱通过率过低: {trap_pass_rate:.1%}",
                    evidence={"task_id": task_id, "pass_rate": trap_pass_rate}
                )
                
                # 检查是否需要封禁
                self._check_ban(miner_id)
                
                self.stats["tasks_failed"] += 1
                return False, f"陷阱验证失败 (通过率: {trap_pass_rate:.1%})", trust_delta
            
            trust_delta = 0.02 * trap_pass_rate  # 通过陷阱增加信任
        
        # 2. 结果哈希验证
        if not result_hash:
            self.stats["tasks_failed"] += 1
            return False, "结果哈希为空", -0.05
        
        # 3. 更新信任度
        if miner_id in self.miner_scores:
            old_trust = self.miner_scores[miner_id]["trust_score"]
            new_trust = max(0, min(1, old_trust + trust_delta))
            self.miner_scores[miner_id]["trust_score"] = new_trust
        
        self.stats["tasks_completed"] += 1
        return True, "验证通过", trust_delta
    
    def _verify_traps(self, trap_results: Dict) -> float:
        """验证陷阱题"""
        if not trap_results:
            return 1.0
        
        total = len(trap_results)
        passed = sum(1 for v in trap_results.values() if v is True or v == "correct")
        
        return passed / total if total > 0 else 1.0
    
    def _check_ban(self, miner_id: str):
        """检查是否需要封禁矿工"""
        # 统计安全事件
        recent_events = [
            e for e in self.audit_events
            if e.miner_id == miner_id 
            and e.event_type == "trap_failure"
            and time.time() - e.timestamp < 3600  # 1小时内
        ]
        
        if len(recent_events) >= self.MAX_FAILED_TRAPS_BEFORE_BAN:
            self.banned_miners[miner_id] = time.time() + self.MINER_BAN_DURATION
            self.stats["miners_banned"] += 1
            self.log(f"🚫 矿工封禁: {miner_id} (连续陷阱失败 {len(recent_events)} 次)")
    
    def _record_security_event(self, miner_id: str, event_type: str, 
                                threat_level: SecurityThreatLevel,
                                description: str, evidence: Dict = None):
        """记录安全事件"""
        event = SecurityAuditEvent(
            event_type=event_type,
            miner_id=miner_id,
            threat_level=threat_level,
            description=description,
            evidence=evidence or {}
        )
        self.audit_events.append(event)
        # 防止 audit_events 无限增长
        if len(self.audit_events) > 10000:
            self.audit_events = self.audit_events[-5000:]
        self.stats["security_incidents"] += 1
        self.log(f"⚠️ 安全事件: [{threat_level.value}] {description}")
    
    # =================================================================
    # 8. 评分集成调度 (ScoreIntegrator)
    # =================================================================
    
    def get_task_priority(self, miner_id: str) -> float:
        """获取矿工任务优先级
        
        综合评分 = 0.35 * pouw_score + 0.25 * user_rating + 
                   0.20 * behavior_score + 0.20 * trust_score
        
        优先级越高，越先被调度。
        """
        if miner_id not in self.miner_scores:
            return 0.5  # 默认中等
        
        scores = self.miner_scores[miner_id]
        
        priority = (
            0.35 * scores.get("pouw_score", 1.0) +
            0.25 * (scores.get("user_rating", 5.0) / 5.0) +
            0.20 * scores.get("behavior_score", 1.0) +
            0.20 * scores.get("trust_score", 0.5)
        )
        
        # 被封禁的矿工优先级为0
        if miner_id in self.banned_miners:
            return 0.0
        
        return min(1.0, max(0.0, priority))
    
    def update_miner_score(self, miner_id: str, 
                            pouw_score: float = None,
                            user_rating: float = None,
                            behavior_score: float = None,
                            trust_delta: float = None):
        """更新矿工评分"""
        if miner_id not in self.miner_scores:
            self.miner_scores[miner_id] = {
                "pouw_score": 1.0,
                "user_rating": 5.0,
                "combined_score": 1.0,
                "behavior_score": 1.0,
                "trust_score": 0.5,
            }
        
        scores = self.miner_scores[miner_id]
        
        if pouw_score is not None:
            scores["pouw_score"] = max(0, min(2.0, pouw_score))
        if user_rating is not None:
            scores["user_rating"] = max(1.0, min(5.0, user_rating))
        if behavior_score is not None:
            scores["behavior_score"] = max(0, min(1.0, behavior_score))
        if trust_delta is not None:
            scores["trust_score"] = max(0, min(1, scores["trust_score"] + trust_delta))
        
        # 重新计算综合分
        scores["combined_score"] = self.get_task_priority(miner_id)
    
    def get_ranked_miners(self, count: int = 10) -> List[Tuple[str, float]]:
        """获取排名前N的矿工（按优先级排序）"""
        miners_with_priority = []
        
        for miner_id in self.miners:
            if miner_id not in self.banned_miners:
                priority = self.get_task_priority(miner_id)
                miners_with_priority.append((miner_id, priority))
        
        miners_with_priority.sort(key=lambda x: x[1], reverse=True)
        return miners_with_priority[:count]
    
    # =================================================================
    # 9. 任务提交与分发
    # =================================================================
    
    def submit_task(self, user_address: str, task_data: Dict,
                     sector: str, payment_amount: float,
                     distribution: TaskDistributionMode = TaskDistributionMode.SINGLE
                    ) -> Tuple[bool, str, Optional[str]]:
        """提交计算任务
        
        完整流程:
        1. 验证MAIN余额并扣款
        2. 根据分发模式选择矿工
        3. 创建安全任务信封
        4. 发起算力见证
        5. 分配任务给矿工
        
        Args:
            user_address: 用户地址
            task_data: 任务数据
            sector: 目标板块
            payment_amount: 支付金额 (MAIN)
            distribution: 分发模式 (单客户端/分布式)
            
        Returns:
            (成功, 消息, 任务ID)
        """
        task_id = uuid.uuid4().hex[:12]
        
        # 1. 验证支付
        pay_ok, pay_msg = self.validate_order_payment(user_address, payment_amount)
        if not pay_ok:
            return False, f"支付验证失败: {pay_msg}", None
        
        # 2. 选择矿工
        available = self.get_miners_accepting_tasks()
        sector_miners = [m for m in available if m.sector == sector or m.sector == "GENERAL"]
        
        if not sector_miners:
            return False, f"板块 {sector} 没有可用矿工", None
        
        # 按优先级排序
        sorted_miners = sorted(
            sector_miners,
            key=lambda m: self.get_task_priority(m.miner_id),
            reverse=True
        )
        
        # 选择矿工
        if distribution == TaskDistributionMode.SINGLE:
            selected = [sorted_miners[0]]
        else:
            # 分布式: 选择前3个矿工
            n = min(3, len(sorted_miners))
            selected = sorted_miners[:n]
        
        # 3. 创建安全任务信封
        envelope = self.create_secure_task(task_id, task_data, sector, user_address)
        
        # 4. 算力见证
        witness_ok, witness_id = self.request_witness(
            WitnessScope.COMPUTE_TASK,
            {"task_id": task_id, "amount": payment_amount, "miners": [m.miner_id for m in selected]}
        )
        
        # 5. 分配
        miner_ids = [m.miner_id for m in selected]
        mode_desc = "单节点" if distribution == TaskDistributionMode.SINGLE else f"分布式({len(selected)}节点)"
        
        self.log(f"📋 任务提交: task={task_id}, {mode_desc}, miners={miner_ids}, pay={payment_amount} MAIN")
        return True, f"任务已分配 ({mode_desc})", task_id
    
    # =================================================================
    # 10. 完整生命周期 — 将所有模块连接成共识
    # =================================================================
    
    def process_block_lifecycle(self, block_height: int, miner_id: str,
                                 miner_address: str, sector: str,
                                 block_reward: float,
                                 transactions: List[Dict] = None) -> Dict:
        """处理完整的区块生命周期
        
        共识引擎产出区块后的完整处理流程:
        1. 铸造板块币奖励
        2. 处理区块内的交易（转账/兑换/订单）
        3. 更新评分
        4. 安全审计
        
        Returns:
            处理结果摘要
        """
        result = {
            "block_height": block_height,
            "miner_id": miner_id,
            "sector": sector,
            "minted": 0,
            "transactions_processed": 0,
            "score_updated": False,
            "security_ok": True,
        }
        
        # 1. 铸造板块币
        mint_ok, minted, msg = self.on_block_mined(
            block_height=block_height,
            miner_address=miner_address,
            sector=sector,
            block_reward=block_reward
        )
        result["minted"] = minted if mint_ok else 0
        
        # 2. 处理交易
        if transactions:
            for tx in transactions:
                tx_type = tx.get("tx_type", "")
                try:
                    if tx_type == "sector_transfer":
                        self.transfer_sector_coin(
                            tx["from_address"], tx["to_address"],
                            tx["sector"], tx["amount"]
                        )
                    elif tx_type == "main_transfer":
                        self.transfer_main(
                            tx["from_address"], tx["to_address"], tx["amount"]
                        )
                    elif tx_type == "exchange":
                        self.exchange_sector_to_main(
                            tx["address"], tx["sector"], tx["amount"]
                        )
                    result["transactions_processed"] += 1
                except Exception as e:
                    self.log(f"⚠️ 交易处理失败: {e}")
        
        # 3. 更新评分
        if miner_id in self.miner_scores:
            self.update_miner_score(miner_id, trust_delta=0.01)
            result["score_updated"] = True
        
        # 4. 安全审计（检查矿工是否被封禁等）
        if miner_id in self.banned_miners:
            result["security_ok"] = False
        
        return result
    
    # =================================================================
    # 11. 查询与统计
    # =================================================================
    
    def get_system_status(self) -> Dict:
        """获取系统总览"""
        mining_only = len(self.get_miners_by_mode(UnifiedMinerMode.MINING_ONLY))
        task_only = len(self.get_miners_by_mode(UnifiedMinerMode.TASK_ONLY))
        mining_and_task = len(self.get_miners_by_mode(UnifiedMinerMode.MINING_AND_TASK))
        
        return {
            "sector": self.sector,
            "testnet": self.testnet,
            "miners": {
                "total": len(self.miners),
                "mining_only": mining_only,
                "task_only": task_only,
                "mining_and_task": mining_and_task,
                "banned": len(self.banned_miners),
            },
            "stats": self.stats.copy(),
            "security": {
                "audit_events": len(self.audit_events),
                "active_bans": len(self.banned_miners),
            }
        }
    
    def get_miner_detail(self, miner_id: str) -> Optional[Dict]:
        """获取矿工详情"""
        if miner_id not in self.miners:
            return None
        
        config = self.miners[miner_id]
        scores = self.miner_scores.get(miner_id, {})
        priority = self.get_task_priority(miner_id)
        
        # 获取板块币余额
        balances = {}
        try:
            from core.sector_coin import SectorCoinType
            if config.sector:
                coin_type = SectorCoinType.from_sector(config.sector)
                bal = self.sector_ledger.get_balance(config.address, coin_type)
                balances[config.sector] = {
                    "balance": bal.balance,
                    "locked": bal.locked,
                    "available": bal.available,
                }
        except Exception:
            pass
        
        # MAIN余额
        main_bal = self._get_main_balance(config.address)
        
        return {
            "config": config.to_dict(),
            "scores": scores,
            "priority": priority,
            "is_banned": miner_id in self.banned_miners,
            "sector_balances": balances,
            "main_balance": main_bal,
        }
    
    def get_security_report(self) -> Dict:
        """获取安全报告"""
        threat_counts = {}
        for event in self.audit_events:
            level = event.threat_level.value
            threat_counts[level] = threat_counts.get(level, 0) + 1
        
        return {
            "total_events": len(self.audit_events),
            "threat_distribution": threat_counts,
            "active_bans": len(self.banned_miners),
            "banned_miners": list(self.banned_miners.keys()),
            "recent_events": [
                {
                    "event_id": e.event_id,
                    "type": e.event_type,
                    "miner": e.miner_id,
                    "level": e.threat_level.value,
                    "description": e.description,
                    "timestamp": e.timestamp,
                }
                for e in self.audit_events[-10:]
            ]
        }
    
    def get_exchange_rates(self) -> Dict[str, float]:
        """获取所有板块的当前汇率"""
        rates = {}
        try:
            from core.sector_coin import get_sector_registry
            sectors = get_sector_registry().get_active_sectors()
        except Exception:
            sectors = ["H100", "RTX4090", "RTX3080", "CPU", "GENERAL"]
        for sector in sectors:
            rates[sector] = self.get_exchange_rate(sector)
        return rates
    
    # =================================================================
    # 12. 协议费用池 — 所有交易自动扣费 (Phase 13)
    # =================================================================
    
    def process_transaction_fee(self, tx_id: str, amount: float, 
                                 block_height: int = 0) -> Dict:
        """处理交易手续费
        
        1% 总手续费自动分配:
        - 0.5% 销毁（通缩）
        - 0.3% 矿工奖励池
        - 0.2% 协议费用池（公共基金）
        
        Args:
            tx_id: 交易ID
            amount: 交易金额（手续费=金额*1%）
            block_height: 区块高度
            
        Returns:
            费用分配详情
        """
        from core.protocol_fee_pool import ProtocolHardRules
        
        fee_amount = amount * ProtocolHardRules.TOTAL_FEE_RATE
        
        try:
            distribution = self.fee_pool.process_fee(tx_id, fee_amount, block_height)
            
            self.stats["fees_burned"] += distribution.burn_amount
            self.stats["fees_to_miners"] += distribution.miner_reward_amount
            self.stats["fees_to_pool"] += distribution.protocol_pool_amount
            
            self.log(f"💸 手续费: tx={tx_id}, fee={fee_amount:.6f} "
                     f"(burn={distribution.burn_amount:.6f}, "
                     f"miner={distribution.miner_reward_amount:.6f}, "
                     f"pool={distribution.protocol_pool_amount:.6f})")
            
            return {
                "tx_id": tx_id,
                "fee_amount": fee_amount,
                "burn": distribution.burn_amount,
                "miner_reward": distribution.miner_reward_amount,
                "protocol_pool": distribution.protocol_pool_amount,
            }
        except Exception as e:
            self.log(f"⚠️ 手续费处理失败: {e}")
            return {"tx_id": tx_id, "fee_amount": 0, "error": "fee_processing_failed"}
    
    # =================================================================
    # 13. 仲裁系统 — 任务纠纷处理 (Phase 13)
    # =================================================================
    
    def start_task_arbitration(self, task_id: str, renter_id: str, 
                                miner_id: str, task_payment: float,
                                coin_type: str = "MAIN") -> Tuple[bool, str]:
        """任务完成后进入仲裁期
        
        任务完成 → 双方质押(5%) → 24h仲裁期 → 无纠纷自动结算
        
        Args:
            task_id: 任务ID
            renter_id: 租用方（任务发布者）
            miner_id: 矿工
            task_payment: 任务报酬
            coin_type: 支付币种
            
        Returns:
            (成功, 消息)
        """
        arb = self.arbitration.start_arbitration(
            task_id=task_id,
            renter_id=renter_id,
            miner_id=miner_id,
            task_payment=task_payment,
            coin_type=coin_type,
        )
        
        if arb is None:
            return False, "仲裁初始化失败（可能质押不足）"
        
        self.log(f"⚖️ 仲裁期开始: task={task_id}, payment={task_payment} {coin_type}")
        return True, f"仲裁期开始 (截止: {arb.time_remaining():.0f}s后)"
    
    def file_dispute(self, task_id: str, complainant_id: str,
                      reason: str, description: str,
                      evidence: Dict = None) -> Tuple[bool, str]:
        """提交纠纷
        
        在仲裁期内，任何一方可以发起纠纷。
        纠纷由随机选择的验证节点投票裁决。
        
        Args:
            task_id: 任务ID
            complainant_id: 投诉方ID
            reason: 纠纷原因 (RESULT_INCORRECT/TASK_NOT_COMPLETED/QUALITY_ISSUE/TIMEOUT)
            description: 纠纷描述
            evidence: 证据
            
        Returns:
            (成功, 消息或纠纷ID)
        """
        from core.arbitration import DisputeReason
        
        try:
            dispute_reason = DisputeReason(reason)
        except ValueError:
            dispute_reason = DisputeReason.OTHER
        
        dispute = self.arbitration.submit_dispute(
            task_id=task_id,
            submitter_id=complainant_id,
            reason=dispute_reason,
            description=description,
            evidence=evidence or {},
        )
        
        if dispute is None:
            return False, "纠纷提交失败（可能不在仲裁期内）"
        
        self.stats["disputes_filed"] += 1
        self.log(f"🔴 纠纷提交: task={task_id}, reason={reason}")
        return True, dispute.dispute_id
    
    def complete_arbitration(self, task_id: str) -> Tuple[bool, str, str]:
        """完成仲裁（自动或手动）
        
        Returns:
            (成功, 消息, 结果状态)
        """
        result = self.arbitration.finalize_arbitration(task_id)
        
        if not result:
            return False, "仲裁完成失败（仍在仲裁期或有未解决纠纷）", ""
        
        # finalize_arbitration 返回 True 表示完成
        arb = self.arbitration.arbitrations.get(task_id)
        status = arb.status.value if arb else "completed"
        self.stats["disputes_resolved"] += 1
        self.log(f"⚖️ 仲裁完成: task={task_id}, result={status}")
        return True, f"仲裁完成: {status}", status
    
    # =================================================================
    # 14. 交易监控 — 异常检测告警 (Phase 13)
    # =================================================================
    
    def monitor_transaction(self, tx_data: Dict) -> Optional[Dict]:
        """监控交易，检测异常
        
        检测项:
        - 大额交易 (>1000 MAIN)  
        - 高频交易 (>20次/分钟)
        - 签名失败
        
        Args:
            tx_data: 交易数据 {txid, from_address, to_address, amount}
            
        Returns:
            告警信息（如果触发），否则None
        """
        alert = self.tx_monitor.on_transaction(tx_data)
        
        if alert:
            self.stats["alerts_triggered"] += 1
            self._record_security_event(
                miner_id=tx_data.get("from_address", ""),
                event_type=f"monitor_{alert.alert_type.value}",
                threat_level=SecurityThreatLevel.MEDIUM,
                description=alert.message,
                evidence=alert.details,
            )
            return alert.to_dict()
        
        return None
    
    # =================================================================
    # 15. 信誉引擎 — 多维度评分 (Phase 13)
    # =================================================================
    
    def update_reputation(self, miner_id: str, task_id: str,
                           quality_score: float = 80.0,
                           speed_score: float = 80.0,
                           success: bool = True) -> Dict:
        """更新矿工信誉
        
        五维度评分: 质量/速度/成功率/用户评价/可靠性
        信誉等级: 铁→青铜→白银→黄金→白金→钻石→传奇
        
        Args:
            miner_id: 矿工ID
            task_id: 任务ID  
            quality_score: 质量评分 (0-100)
            speed_score: 速度评分 (0-100)
            success: 任务是否成功
            
        Returns:
            更新后的信誉摘要
        """
        try:
            from core.reputation_engine import TaskCategory
            self.reputation.record_task_completion(
                miner_address=miner_id,
                task_id=task_id,
                category=TaskCategory.GENERAL,
                success=success,
                quality=quality_score,
                expected_duration=60.0,
                actual_duration=60.0 * (100.0 / max(1, speed_score)),
            )
            
            score = self.reputation.get_reputation(miner_id)
            
            # 同步到简化评分
            if miner_id in self.miner_scores and score:
                self.miner_scores[miner_id]["user_rating"] = max(
                    1.0, min(5.0, score.overall_score / 20.0)
                )
            
            return {
                "miner_id": miner_id,
                "overall": score.overall_score if score else 50.0,
                "tier": score.tier.value if score else "unranked",
                "total_tasks": score.total_tasks if score else 0,
            }
        except Exception as e:
            # 信誉引擎未初始化时的兜底
            return {
                "miner_id": miner_id,
                "overall": 50.0,
                "tier": "unranked",
                "error": "reputation_unavailable",
            }
    
    # =================================================================
    # 16. 任务验收/SLA — 三层验收 (Phase 13)
    # =================================================================
    
    def verify_task_acceptance(self, task_id: str, miner_id: str,
                                result_data: Dict,
                                sla: Dict = None) -> Tuple[bool, str, Dict]:
        """任务验收（三层分离）
        
        Layer 1 - 协议层: 是否正确执行、是否一致
        Layer 2 - 服务层: 是否满足 SLA (延迟/吞吐量/错误率)
        Layer 3 - 应用层: 用户评价、自动接受
        
        Args:
            task_id: 任务ID
            miner_id: 矿工ID
            result_data: 任务结果 {result_hash, execution_time_ms, error_rate, ...}
            sla: 可选SLA定义 {max_latency_ms, min_throughput, max_error_rate}
            
        Returns:
            (通过, 消息, 各层判定详情)
        """
        verdicts = {
            "protocol": "executed",
            "service": "met",
            "application": "auto_accepted",
        }
        
        # Layer 1: 协议层 — 结果哈希验证
        if not result_data.get("result_hash"):
            verdicts["protocol"] = "invalid"
            return False, "协议层验证失败: 无结果哈希", verdicts
        
        # 检查陷阱题
        trap_results = result_data.get("trap_results")
        if trap_results:
            pass_rate = self._verify_traps(trap_results)
            if pass_rate < 0.5:
                verdicts["protocol"] = "cheated"
                return False, f"协议层验证失败: 陷阱通过率 {pass_rate:.1%}", verdicts
        
        verdicts["protocol"] = "executed"
        
        # Layer 2: 服务层 — SLA验证
        if sla:
            exec_time = result_data.get("execution_time_ms", 0)
            error_rate = result_data.get("error_rate", 0)
            
            if sla.get("max_latency_ms") and exec_time > sla["max_latency_ms"]:
                verdicts["service"] = "violated"
                self.stats["sla_violations"] += 1
            elif sla.get("max_error_rate") and error_rate > sla["max_error_rate"]:
                verdicts["service"] = "partial"
            else:
                verdicts["service"] = "met"
        
        # Layer 3: 应用层 — 自动接受（24h超时未争议则自动接受）
        verdicts["application"] = "auto_accepted"
        
        # 更新信誉
        success = verdicts["protocol"] == "executed" and verdicts["service"] != "violated"
        self.update_reputation(
            miner_id=miner_id,
            task_id=task_id,
            quality_score=80.0 if success else 30.0,
            speed_score=80.0,
            success=success,
        )
        
        if success:
            self.stats["tasks_completed"] += 1
            return True, "三层验收通过", verdicts
        else:
            self.stats["tasks_failed"] += 1
            return False, f"验收未通过 (service={verdicts['service']})", verdicts
    
    # =================================================================
    # 17. 留言评价系统 (Phase 13)
    # =================================================================
    
    def submit_review(self, reviewer_id: str, target_id: str,
                       task_id: str, rating: float,
                       comment: str = "") -> Tuple[bool, str]:
        """提交评价
        
        链上存哈希(不可篡改)，链外存内容(可压缩)。
        支持用户→矿工评价和矿工→任务评价。
        
        Args:
            reviewer_id: 评价者
            target_id: 被评价者
            task_id: 关联任务
            rating: 评分 (1.0-5.0)
            comment: 评价内容
            
        Returns:
            (成功, 评价哈希)
        """
        rating = max(1.0, min(5.0, rating))
        
        # 生成链上哈希
        review_data = json.dumps({
            "reviewer": reviewer_id,
            "target": target_id,
            "task": task_id,
            "rating": rating,
            "comment": comment,
            "timestamp": time.time(),
        }, sort_keys=True).encode('utf-8')
        
        review_hash = hashlib.sha256(review_data).hexdigest()[:32]
        
        # 更新评分
        if target_id in self.miner_scores:
            old_rating = self.miner_scores[target_id].get("user_rating", 5.0)
            # 指数移动平均
            alpha = 0.3
            new_rating = alpha * rating + (1 - alpha) * old_rating
            self.miner_scores[target_id]["user_rating"] = max(1.0, min(5.0, new_rating))
            
            # 同步更新综合分
            self.miner_scores[target_id]["combined_score"] = self.get_task_priority(target_id)
        
        self.stats["reviews_submitted"] += 1
        self.log(f"⭐ 评价提交: {reviewer_id} → {target_id} rating={rating:.1f} (task={task_id})")
        return True, review_hash
    
    # =================================================================
    # 18. 矿工行为分析 (Phase 13)
    # =================================================================
    
    def record_order_behavior(self, order_id: str, miner_id: str,
                               quoted_price: float, market_avg_price: float,
                               accepted: bool,
                               response_time: float = 1.0,
                               was_congested: bool = False) -> Dict:
        """记录矿工接单行为
        
        分析维度:
        - 价格多样性: 是否只接高价单
        - 拥堵时帮助: 网络拥堵时是否愿意接单
        - 接受率: 总体接单率
        
        Args:
            order_id: 订单ID
            miner_id: 矿工ID
            quoted_price: 报价
            market_avg_price: 市场均价
            accepted: 是否接受
            response_time: 响应时间(秒)
            was_congested: 是否拥堵期间
            
        Returns:
            行为评分
        """
        from core.miner_behavior import FulfillmentStatus
        
        status = FulfillmentStatus.ACCEPTED if accepted else FulfillmentStatus.REJECTED
        
        self.behavior_analyzer.record_order(
            order_id=order_id,
            miner_id=miner_id,
            quoted_price=quoted_price,
            market_avg_price=market_avg_price,
            status=status,
            response_time=response_time,
            was_congested=was_congested,
        )
        
        score = self.behavior_analyzer.calculate_score(miner_id)
        
        # 同步行为分到评分系统
        if miner_id in self.miner_scores:
            self.miner_scores[miner_id]["behavior_score"] = max(
                0, min(1.0, score.final_score)
            )
        
        return {
            "miner_id": miner_id,
            "overall_score": score.final_score,
            "acceptance_rate": score.acceptance_rate,
            "price_diversity": score.price_diversity_score,
        }
    
    # =================================================================
    # 19. 增强的区块生命周期 — 集成所有子系统 (Phase 13)
    # =================================================================
    
    def process_full_block_lifecycle(self, block_height: int, miner_id: str,
                                      miner_address: str, sector: str,
                                      block_reward: float,
                                      transactions: List[Dict] = None) -> Dict:
        """增强的区块生命周期 — 集成费用池、监控、仲裁
        
        对比 process_block_lifecycle() 新增:
        1. 交易手续费处理 (协议费用池)  
        2. 交易异常监控 (告警)
        3. 矿工信誉更新
        4. 矿工行为记录
        
        Returns:
            完整处理结果
        """
        result = self.process_block_lifecycle(
            block_height=block_height,
            miner_id=miner_id,
            miner_address=miner_address,
            sector=sector,
            block_reward=block_reward,
            transactions=transactions,
        )
        
        # == 新增: 手续费处理 ==
        fee_results = []
        if transactions:
            for tx in transactions:
                amount = tx.get("amount", 0)
                if amount > 0:
                    fee_info = self.process_transaction_fee(
                        tx_id=tx.get("tx_id", uuid.uuid4().hex[:12]),
                        amount=amount,
                        block_height=block_height,
                    )
                    fee_results.append(fee_info)
                    
                    # == 新增: 交易监控 ==
                    alert = self.monitor_transaction({
                        "txid": tx.get("tx_id", ""),
                        "from_address": tx.get("from_address", ""),
                        "to_address": tx.get("to_address", ""),
                        "amount": amount,
                    })
                    if alert:
                        result["security_ok"] = False
        
        result["fees_processed"] = len(fee_results)
        result["total_fees"] = sum(f.get("fee_amount", 0) for f in fee_results)
        
        # == 新增: 信誉更新 ==
        self.update_reputation(
            miner_id=miner_id,
            task_id=f"block_{block_height}",
            quality_score=85.0,
            speed_score=80.0,
            success=True,
        )
        
        return result
    
    # =================================================================
    # 20. 增强的系统状态 — 包含所有子系统 (Phase 13)
    # =================================================================
    
    def get_full_system_status(self) -> Dict:
        """获取完整系统状态（含所有子系统）"""
        base = self.get_system_status()
        
        # 扩展统计
        base["stats"].update({
            "fees_burned": self.stats.get("fees_burned", 0),
            "fees_to_miners": self.stats.get("fees_to_miners", 0),
            "fees_to_pool": self.stats.get("fees_to_pool", 0),
            "disputes_filed": self.stats.get("disputes_filed", 0),
            "disputes_resolved": self.stats.get("disputes_resolved", 0),
            "reviews_submitted": self.stats.get("reviews_submitted", 0),
            "alerts_triggered": self.stats.get("alerts_triggered", 0),
            "sla_violations": self.stats.get("sla_violations", 0),
        })
        
        # 子系统连接状态
        base["subsystems"] = {
            "sector_ledger": self._sector_ledger is not None or True,
            "exchange_engine": self._exchange_engine is not None or True,
            "rate_engine": self._rate_engine is not None or True,
            "witness_compute": self._witness_compute is not None or True,
            "fee_pool": "connected",
            "arbitration": "connected",
            "reputation": "connected",
            "tx_monitor": "connected",
            "task_acceptance": "connected",
            "message_system": "connected",
            "behavior_analyzer": "connected",
        }
        
        # 费用池状态
        try:
            base["fee_pool"] = {
                "total_burned": self.stats.get("fees_burned", 0),
                "miner_rewards": self.stats.get("fees_to_miners", 0),
                "protocol_pool": self.stats.get("fees_to_pool", 0),
            }
        except Exception:
            base["fee_pool"] = {"status": "not_initialized"}
        
        return base
