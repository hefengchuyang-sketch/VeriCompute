# -*- coding: utf-8 -*-
"""
算力调度器 - 强制模式 / 自主模式 / 混合模式

设计目标：
1. 高可用算力 - 确保用户算力订单及时执行
2. 矿工安全与自主性 - 不影响原有挖矿任务和硬件安全
3. 公平激励机制 - POUW + 用户评分综合评价贡献
4. 异构与分布式支持 - 同板块异构矿机协作

调度模式：
- 强制模式（FORCED）：系统自动调度可用矿机
- 自主模式（VOLUNTARY）：矿工自己上线接单
- 混合模式（HYBRID）：优先自主，不足时强制调度
"""

import time
import json
import hashlib
import sqlite3
import os
import logging
from typing import Dict, List, Optional, Tuple, Any

from core import db
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from contextlib import contextmanager
import threading
import random
import base64
import hmac as _hmac
import secrets

logger = logging.getLogger(__name__)

from .blind_task_engine import BlindTaskEngine, BlindBatch, TrapGenerator

# 导入 ECDSA 签名器（若可用）
try:
    from .crypto import ECDSASigner, HAS_ECDSA as HAS_CRYPTO_ECDSA
except ImportError:
    HAS_CRYPTO_ECDSA = False


# ============== 枚举定义 ==============

class ScheduleMode(Enum):
    """调度模式"""
    FORCED = "forced"           # 强制模式：系统优先调度
    VOLUNTARY = "voluntary"     # 自主模式：卖家自上线
    HYBRID = "hybrid"           # 混合模式：优先自主，不足时强制
    BLIND = "blind"             # 盲调度模式：矿工不知道在执行付费任务


class MinerStatus(Enum):
    """矿机状态"""
    ONLINE = "online"           # 在线（空闲，可接单）
    BUSY = "busy"               # 忙碌（执行任务中）
    OFFLINE = "offline"         # 离线
    MAINTENANCE = "maintenance" # 维护中
    MINING = "mining"           # PoUW 挖矿中


class MinerMode(Enum):
    """矿工接单模式"""
    VOLUNTARY = "voluntary"     # 自主接单（需要手动上线）
    FORCED = "forced"           # 强制接单（系统可调度）
    DISABLED = "disabled"       # 禁用接单（只挖矿）
    # 统一模式（与 UnifiedConsensus 对齐）
    MINING_ONLY = "mining_only"         # 纯挖矿铸币
    TASK_ONLY = "task_only"             # 纯接单
    MINING_AND_TASK = "mining_and_task"  # 挖矿+接单


class TaskStatus(Enum):
    """任务状态"""
    PENDING = "pending"         # 待分配
    ASSIGNED = "assigned"       # 已分配（等待执行）
    RUNNING = "running"         # 执行中
    COMPLETED = "completed"     # 完成
    FAILED = "failed"           # 失败
    TIMEOUT = "timeout"         # 超时
    VERIFYING = "verifying"     # 验证中


class SettlementType(Enum):
    """结算类型"""
    SECTOR_COIN = "sector_coin"   # 同板块：板块币结算
    MAIN_COIN = "main_coin"       # 跨板块：主币结算


@dataclass
class Reputation:
    """Beta 分布信誉模型。"""
    alpha: float = 1.0
    beta: float = 1.0

    def update(self, success: bool):
        if success:
            self.alpha += 1.0
        else:
            self.beta += 1.0

    def decay(self, factor: float = 0.99):
        self.alpha = max(1.0, self.alpha * factor)
        self.beta = max(1.0, self.beta * factor)

    @property
    def mean(self) -> float:
        total = self.alpha + self.beta
        return self.alpha / total if total > 0 else 0.5

    @property
    def uncertainty(self) -> float:
        total = self.alpha + self.beta
        return 1.0 / total if total > 0 else 1.0

    @property
    def score(self) -> float:
        return self.mean - 1.5 * self.uncertainty


@dataclass
class ExecutionResult:
    """执行层结果结构（兼容现有 result_hash 管线）。"""
    task_id: str
    node_id: str
    output: str
    timestamp: float = field(default_factory=time.time)
    partial_hash: str = ""


# ============== 数据结构 ==============

@dataclass
class MinerNode:
    """矿机节点"""
    miner_id: str
    address: str                  # 钱包地址
    sector: str                   # 所属板块
    gpu_model: str                # GPU 型号
    gpu_memory: float             # 显存 (GB)
    compute_power: float          # 算力 (TFLOPs)
    bandwidth: float = 1.0        # 网络带宽 (相对值)
    latency: float = 0.0          # 网络延迟惩罚项
    stake: float = 0.0            # 节点质押
    
    # 状态
    status: MinerStatus = MinerStatus.OFFLINE
    mode: MinerMode = MinerMode.VOLUNTARY  # 接单模式
    current_task_id: Optional[str] = None
    
    # 调度属性
    price_per_hour: float = 0.0   # 自主模式价格
    available_hours: List[int] = field(default_factory=list)  # 可接单时间段
    
    # 评分
    pouw_score: float = 1.0       # POUW 客观评分
    user_rating: float = 5.0      # 用户评分 (1-5)
    combined_score: float = 1.0   # 综合评分
    rep_alpha: float = 1.0        # Beta reputation α
    rep_beta: float = 1.0         # Beta reputation β
    
    # 统计
    tasks_completed: int = 0
    tasks_failed: int = 0
    total_earnings: float = 0.0
    avg_response_time: float = 0.0
    
    # 时间
    last_heartbeat: float = field(default_factory=time.time)
    registered_at: float = field(default_factory=time.time)
    
    def to_dict(self) -> Dict:
        return {
            "miner_id": self.miner_id,
            "address": self.address,
            "sector": self.sector,
            "gpu_model": self.gpu_model,
            "gpu_memory": self.gpu_memory,
            "compute_power": self.compute_power,
            "bandwidth": self.bandwidth,
            "latency": self.latency,
            "stake": self.stake,
            "status": self.status.value,
            "mode": self.mode.value,
            "current_task_id": self.current_task_id,
            "price_per_hour": self.price_per_hour,
            "available_hours": self.available_hours,
            "pouw_score": self.pouw_score,
            "user_rating": self.user_rating,
            "combined_score": self.combined_score,
            "rep_alpha": self.rep_alpha,
            "rep_beta": self.rep_beta,
            "tasks_completed": self.tasks_completed,
            "tasks_failed": self.tasks_failed,
            "total_earnings": self.total_earnings,
            "avg_response_time": self.avg_response_time,
            "last_heartbeat": self.last_heartbeat,
            "registered_at": self.registered_at,
        }
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'MinerNode':
        return cls(
            miner_id=data['miner_id'],
            address=data['address'],
            sector=data['sector'],
            gpu_model=data.get('gpu_model', 'Unknown'),
            gpu_memory=data.get('gpu_memory', 0),
            compute_power=data.get('compute_power', 0),
            bandwidth=data.get('bandwidth', 1.0),
            latency=data.get('latency', 0.0),
            stake=data.get('stake', 0.0),
            status=MinerStatus(data.get('status', 'offline')),
            mode=MinerMode(data.get('mode', 'voluntary')),
            current_task_id=data.get('current_task_id'),
            price_per_hour=data.get('price_per_hour', 0.0),
            available_hours=data.get('available_hours', []),
            pouw_score=data.get('pouw_score', 1.0),
            user_rating=data.get('user_rating', 5.0),
            combined_score=data.get('combined_score', 1.0),
            rep_alpha=data.get('rep_alpha', 1.0),
            rep_beta=data.get('rep_beta', 1.0),
            tasks_completed=data.get('tasks_completed', 0),
            tasks_failed=data.get('tasks_failed', 0),
            total_earnings=data.get('total_earnings', 0.0),
            avg_response_time=data.get('avg_response_time', 0.0),
            last_heartbeat=data.get('last_heartbeat', time.time()),
            registered_at=data.get('registered_at', time.time()),
        )
    
    def update_combined_score(self, pouw_weight: float = 0.6, user_weight: float = 0.4):
        """更新综合评分 (POUW + 用户评分)"""
        # POUW 评分归一化 (0-1)
        pouw_normalized = min(self.pouw_score, 2.0) / 2.0
        # 用户评分归一化 (0-1)
        user_normalized = self.user_rating / 5.0
        # 综合评分
        self.combined_score = pouw_weight * pouw_normalized + user_weight * user_normalized

    @property
    def reputation_mean(self) -> float:
        rep = Reputation(self.rep_alpha, self.rep_beta)
        return rep.mean

    @property
    def reputation_uncertainty(self) -> float:
        rep = Reputation(self.rep_alpha, self.rep_beta)
        return rep.uncertainty

    @property
    def reputation_score(self) -> float:
        rep = Reputation(self.rep_alpha, self.rep_beta)
        return rep.score

    def update_reputation(self, success: bool):
        rep = Reputation(self.rep_alpha, self.rep_beta)
        rep.update(success)
        self.rep_alpha = rep.alpha
        self.rep_beta = rep.beta

    def decay_reputation(self, factor: float = 0.99):
        rep = Reputation(self.rep_alpha, self.rep_beta)
        rep.decay(factor)
        self.rep_alpha = rep.alpha
        self.rep_beta = rep.beta


@dataclass
class ComputeTask:
    """计算任务"""
    task_id: str
    order_id: str                 # 所属订单
    buyer_address: str
    
    # 任务内容
    task_type: str                # 任务类型
    task_data: str                # 任务数据
    sector: str                   # 目标板块
    payload: Any = None
    security_tier: int = 0
    verification_mode: str = "none"   # none | sampling | consensus
    random_seed: int = 0
    compute_required: float = 1.0
    memory_required: float = 1.0
    deadline: float = 0.0
    
    # 分配
    assigned_miners: List[str] = field(default_factory=list)  # 分配的矿工
    redundancy: int = 2           # 冗余节点数
    
    # 结果
    results: Dict[str, str] = field(default_factory=dict)  # miner_id -> result_hash
    execution_results: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    final_result: Optional[str] = None
    
    # 状态
    status: TaskStatus = TaskStatus.PENDING
    
    # 结算
    settlement_type: SettlementType = SettlementType.SECTOR_COIN
    total_payment: float = 0.0
    miner_payments: Dict[str, float] = field(default_factory=dict)
    fee_breakdown: Dict[str, Any] = field(default_factory=dict)
    
    # 重试
    retry_count: int = 0              # 已重试次数
    max_retries: int = 3              # 最大重试次数
    
    # 执行时长
    duration_seconds: int = 0         # 买家指定的执行时长（秒），0=使用默认超时
    
    # 进度报告
    progress: float = 0.0             # 任务进度 0.0~1.0
    progress_message: str = ""        # 进度描述
    last_progress_at: float = 0.0     # 上次进度报告时间
    
    # 时间
    created_at: float = field(default_factory=time.time)
    assigned_at: float = 0.0
    started_at: float = 0.0
    completed_at: float = 0.0
    timeout_at: float = 0.0       # 超时时间
    challenge_window_end: float = 0.0 # 【V1.0】争议公示期结束时间
    
    def to_dict(self) -> Dict:
        return {
            "task_id": self.task_id,
            "order_id": self.order_id,
            "buyer_address": self.buyer_address,
            "task_type": self.task_type,
            "task_data": self.task_data,
            "sector": self.sector,
            "payload": self.payload,
            "security_tier": self.security_tier,
            "verification_mode": self.verification_mode,
            "random_seed": self.random_seed,
            "compute_required": self.compute_required,
            "memory_required": self.memory_required,
            "deadline": self.deadline,
            "assigned_miners": self.assigned_miners,
            "redundancy": self.redundancy,
            "results": self.results,
            "execution_results": self.execution_results,
            "final_result": self.final_result,
            "status": self.status.value,
            "settlement_type": self.settlement_type.value,
            "total_payment": self.total_payment,
            "miner_payments": self.miner_payments,
            "fee_breakdown": self.fee_breakdown,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "duration_seconds": self.duration_seconds,
            "progress": self.progress,
            "progress_message": self.progress_message,
            "last_progress_at": self.last_progress_at,
            "created_at": self.created_at,
            "assigned_at": self.assigned_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "timeout_at": self.timeout_at,
            "challenge_window_end": self.challenge_window_end,
        }


# ============== 任务数据验证器 ==============

class TaskDataValidator:
    """任务数据安全验证
    
    防护措施：
    1. 大小限制 — 防止超大 payload 拒绝服务
    2. 结构校验 — 确保 task_data 是有效 JSON
    3. 代码签名验证 — 买家签名 + 哈希完整性
    4. 危险模式检测 — 基本的恶意 payload 检测
    """
    
    MAX_TASK_DATA_SIZE = 10 * 1024 * 1024  # 10 MB
    MAX_TASK_TYPE_LEN = 64
    
    # 危险模式黑名单（基本防护，不替代容器沙箱）
    DANGEROUS_PATTERNS = [
        "rm -rf /",
        "mkfs.",
        "dd if=/dev/zero",
        ":(){:|:&};:",      # fork bomb
        "chmod 777 /",
        "curl | sh",
        "wget | bash",
    ]
    
    @classmethod
    def validate(cls, task: 'ComputeTask') -> Tuple[bool, str]:
        """验证任务数据的安全性和完整性
        
        Returns:
            (is_valid, error_message)
        """
        # 1. 大小检查
        if len(task.task_data) > cls.MAX_TASK_DATA_SIZE:
            return False, f"任务数据超出大小限制: {len(task.task_data)} > {cls.MAX_TASK_DATA_SIZE}"
        
        # 2. task_type 合法性
        if not task.task_type or len(task.task_type) > cls.MAX_TASK_TYPE_LEN:
            return False, f"任务类型无效或过长 (max {cls.MAX_TASK_TYPE_LEN})"
        
        # 3. JSON 结构验证
        try:
            parsed = json.loads(task.task_data) if task.task_data else {}
            if not isinstance(parsed, dict):
                return False, "task_data 必须是 JSON 对象（非数组或标量）"
        except (json.JSONDecodeError, TypeError):
            # 允许纯文本任务数据（如哈希计算挑战）
            pass
        
        # 4. 危险模式检测
        task_data_lower = task.task_data.lower()
        for pattern in cls.DANGEROUS_PATTERNS:
            if pattern.lower() in task_data_lower:
                return False, f"任务数据包含危险模式: {pattern}"
        
        # 5. buyer_address 合法性
        if not task.buyer_address or len(task.buyer_address) < 2:
            return False, "买家地址无效"
        
        # 6. 数据完整性哈希（可选 — 如果 fee_breakdown 中有签名）
        sig = task.fee_breakdown.get("task_data_signature")
        expected_hash = task.fee_breakdown.get("task_data_hash")
        if expected_hash:
            actual_hash = hashlib.sha256(task.task_data.encode()).hexdigest()
            if actual_hash != expected_hash:
                return False, "任务数据完整性校验失败: 哈希不匹配（数据可能被篡改）"
        
        # 7. ECDSA 签名验证（可选 — 如果买家提供了签名 + 公钥）
        buyer_pubkey = task.fee_breakdown.get("buyer_public_key")
        if sig and buyer_pubkey and HAS_CRYPTO_ECDSA:
            try:
                msg_bytes = task.task_data.encode()
                sig_bytes = bytes.fromhex(sig)
                pubkey_bytes = bytes.fromhex(buyer_pubkey)
                if not ECDSASigner.verify(pubkey_bytes, msg_bytes, sig_bytes):
                    return False, "买家 ECDSA 签名验证失败: 任务数据可能被篡改"
            except Exception as e:
                return False, f"签名验证异常: {e}"
        
        return True, ""
    
    @staticmethod
    def compute_task_hash(task_data: str) -> str:
        """计算任务数据的 SHA-256 哈希，用于签名和完整性校验"""
        return hashlib.sha256(task_data.encode()).hexdigest()


# ============== 算力调度器 ==============

class ComputeScheduler:
    """算力调度器 - 支持强制/自主/混合模式"""
    
    # 配置
    DEFAULT_REDUNDANCY = 2        # 默认执行节点数（传统模式强制双节点）
    MAX_REDUNDANCY = 5            # 最大冗余节点数（向后兼容）
    TASK_TIMEOUT = 3600           # 默认任务超时（秒），可被 duration_seconds 覆盖
    MAX_TASK_DURATION = 720 * 3600 # 最大任务时长（30 天 = 2592000 秒）
    HEARTBEAT_TIMEOUT = 60        # 心跳超时（秒）
    HEARTBEAT_EXTEND_GRACE = 120  # 心跳延展宽限期（秒）：矿工存活时额外延长超时
    WATCHDOG_INTERVAL = 30        # 守护巡检间隔（秒）
    MAX_TASK_RETRIES = 3          # 任务最大重试次数
    
    # 【V1.0 - 挑战期与资金托管时长】
    DISPUTE_WINDOW_SECONDS = 3600 # 结果提交后的锁定期，1小时内允许别的节点挑战
    
    HIGH_VALUE_THRESHOLD = 10.0
    LOW_REP_THRESHOLD = 0.35
    CONSENSUS_VARIANCE_THRESHOLD = 0.0
    SAMPLING_RATIO = 0.2
    MIN_SAMPLING_POINTS = 1
    DECAY_INTERVAL_SECONDS = 300
    REPUTATION_DECAY_FACTOR = 0.99
    SLASH_RATIO = 0.3

    TIER_CONFIG = {
        0: {"k": 1, "verify": "none", "cost": 1.0},
        1: {"k": 2, "verify": "consensus", "cost": 1.2},
        2: {"k": 2, "verify": "sampling", "cost": 1.4},
        3: {"k": 3, "verify": "consensus", "cost": 1.6},
    }
    
    # 评分权重（治理层可调整）
    POUW_WEIGHT = 0.6             # POUW 评分权重
    USER_RATING_WEIGHT = 0.4      # 用户评分权重
    
    # 费率（去中心化费用分配）
    # 总费率 1.0% = 0.5% 销毁 + 0.3% 矿工激励 + 0.2% 基金会
    TOTAL_FEE_RATE = 0.01         # 1% 总费率
    BURN_FEE_RATE = 0.005         # 0.5% 直接销毁（通缩）
    MINER_FEE_RATE = 0.003        # 0.3% 矿工激励（区块矿工）
    FOUNDATION_FEE_RATE = 0.002   # 0.2% 基金会运维（多签钱包）
    
    # 基金会多签地址（链上治理可变更）
    FOUNDATION_MULTISIG = "MAIN_FOUNDATION_MULTISIG_001"
    
    def __init__(self, db_path: str = "data/compute_scheduler.db",
                 mode: ScheduleMode = ScheduleMode.BLIND):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.schedule_mode = mode
        self._init_db()
        
        # 调度锁
        self._lock = threading.Lock()
        self._last_decay_at = 0.0
        
        # 盲任务引擎（矿工无感知的算力租用系统）
        self.blind_engine = BlindTaskEngine()
        
        # 守护线程 — 巡检超时任务和离线矿工
        self._watchdog_running = True
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop, daemon=True, name="scheduler-watchdog"
        )
        self._watchdog_thread.start()
    
    @contextmanager
    def _conn(self):
        conn = db.connect(str(self.db_path))
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()
    
    def _init_db(self):
        with self._conn() as conn:
            # 矿机表
            conn.execute("""
                CREATE TABLE IF NOT EXISTS miners (
                    miner_id TEXT PRIMARY KEY,
                    miner_data TEXT NOT NULL,
                    sector TEXT NOT NULL,
                    status TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    compute_power REAL NOT NULL,
                    combined_score REAL NOT NULL,
                    last_heartbeat REAL NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            
            # 任务表
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    task_data TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    sector TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            
            # 索引
            conn.execute("CREATE INDEX IF NOT EXISTS idx_miners_sector ON miners(sector)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_miners_status ON miners(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)")
    
    # ============== 矿机管理 ==============
    
    def register_miner(self, miner: MinerNode) -> Tuple[bool, str]:
        """注册矿机"""
        try:
            with self._conn() as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO miners
                    (miner_id, miner_data, sector, status, mode, compute_power, 
                     combined_score, last_heartbeat, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    miner.miner_id,
                    json.dumps(miner.to_dict()),
                    miner.sector,
                    miner.status.value,
                    miner.mode.value,
                    miner.compute_power,
                    miner.combined_score,
                    miner.last_heartbeat,
                    miner.registered_at,
                    time.time()
                ))
            return True, f"矿机注册成功: {miner.miner_id}"
        except Exception as e:
            return False, str(e)
    
    def get_miner(self, miner_id: str) -> Optional[MinerNode]:
        """获取矿机信息"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT miner_data FROM miners WHERE miner_id = ?",
                (miner_id,)
            ).fetchone()
            return MinerNode.from_dict(json.loads(row['miner_data'])) if row else None
    
    def update_miner(self, miner: MinerNode):
        """更新矿机"""
        with self._conn() as conn:
            conn.execute("""
                UPDATE miners 
                SET miner_data = ?, status = ?, mode = ?, combined_score = ?,
                    last_heartbeat = ?, updated_at = ?
                WHERE miner_id = ?
            """, (
                json.dumps(miner.to_dict()),
                miner.status.value,
                miner.mode.value,
                miner.combined_score,
                miner.last_heartbeat,
                time.time(),
                miner.miner_id
            ))
    
    def set_miner_mode(self, miner_id: str, mode: MinerMode) -> Tuple[bool, str]:
        """设置矿工接单模式"""
        miner = self.get_miner(miner_id)
        if not miner:
            return False, "矿机不存在"
        
        miner.mode = mode
        self.update_miner(miner)
        return True, f"接单模式已设置为: {mode.value}"
    
    def miner_heartbeat(self, miner_id: str) -> Tuple[bool, Optional[ComputeTask]]:
        """矿工心跳 - 返回待执行任务
        
        盲模式下：矿工看到的是 "mining_challenge"，不知道是付费任务
        """
        miner = self.get_miner(miner_id)
        if not miner:
            return False, None
        
        # 更新心跳
        miner.last_heartbeat = time.time()
        
        # 如果当前有任务，保持状态
        if miner.current_task_id:
            task = self.get_task(miner.current_task_id)
            if task and task.status in (TaskStatus.ASSIGNED, TaskStatus.RUNNING):
                # 矿工存活 → 延展任务超时（防止长任务被意外杀死）
                now = time.time()
                min_timeout = now + self.HEARTBEAT_EXTEND_GRACE
                # 绝对上限：不得超过创建时间 + MAX_TASK_DURATION
                absolute_deadline = task.created_at + self.MAX_TASK_DURATION
                min_timeout = min(min_timeout, absolute_deadline)
                if task.timeout_at < min_timeout:
                    task.timeout_at = min_timeout
                    self._save_task(task)
                # 盲模式下保持 MINING 状态（不暴露给矿工）
                if self.schedule_mode == ScheduleMode.BLIND:
                    miner.status = MinerStatus.MINING
                else:
                    miner.status = MinerStatus.BUSY
                self.update_miner(miner)
                return True, task
            else:
                # 任务已完成，清除
                miner.current_task_id = None
        
        # 检查是否有新分配的任务
        task = self._get_assigned_task_for_miner(miner_id)
        if task:
            if self.schedule_mode == ScheduleMode.BLIND:
                miner.status = MinerStatus.MINING  # 矿工以为在挖矿
            else:
                miner.status = MinerStatus.BUSY
            miner.current_task_id = task.task_id
            self.update_miner(miner)
            return True, task
        
        # 无任务，设置为在线/挖矿
        if miner.mode == MinerMode.DISABLED:
            miner.status = MinerStatus.MINING
        else:
            miner.status = MinerStatus.ONLINE
        self.update_miner(miner)
        
        return True, None
    
    def get_blind_batch_for_miner(self, miner_id: str) -> Optional[Dict]:
        """获取矿工的盲批次工作包（矿工看到的是挖矿挑战列表）
        
        Returns:
            矿工视图的批次数据（不含任何泄露信息）
        """
        for batch in self.blind_engine.pending_batches.values():
            if batch.miner_id == miner_id and batch.status == "pending":
                return batch.to_miner_view()
        return None
    
    def _get_assigned_task_for_miner(self, miner_id: str) -> Optional[ComputeTask]:
        """获取分配给矿工的任务"""
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT task_id, task_data FROM tasks 
                WHERE status = 'assigned' 
                ORDER BY created_at ASC
            """).fetchall()
            
            for row in rows:
                raw = row['task_data']
                decrypted = self._decrypt_at_rest(raw, row['task_id'])
                task_data = json.loads(decrypted)
                if miner_id in task_data.get('assigned_miners', []):
                    task_data['status'] = TaskStatus(task_data['status'])
                    task_data['settlement_type'] = SettlementType(task_data.get('settlement_type', 'sector_coin'))
                    return ComputeTask(**{k: v for k, v in task_data.items() 
                                          if k in ComputeTask.__dataclass_fields__})
            return None
    
    # ============== 守护巡检 ==============
    
    def _watchdog_loop(self):
        """后台守护线程 — 定时巡检超时任务和离线矿工
        
        解决矿工断电/掉线后任务永久卡住的问题：
        1. 扫描 ASSIGNED/RUNNING 状态中已超时的任务 → 重新调度或标记失败
        2. 扫描心跳超时的矿工 → 标记离线，释放其持有的任务
        """
        while self._watchdog_running:
            try:
                self._handle_timed_out_tasks()
                self._handle_offline_miners()
                self._maybe_decay_reputation()
            except Exception as e:
                # 守护线程不能崩溃
                pass
            
            # 等待下次巡检（可被 close() 中断）
            for _ in range(self.WATCHDOG_INTERVAL):
                if not self._watchdog_running:
                    return
                time.sleep(1)
    
    def _handle_timed_out_tasks(self):
        """扫描并处理超时任务"""
        now = time.time()
        with self._conn() as conn:
            # 查找所有 ASSIGNED/RUNNING 且已超时的任务
            rows = conn.execute("""
                SELECT task_id, task_data FROM tasks
                WHERE status IN ('assigned', 'running')
            """).fetchall()
        
        for row in rows:
            raw = row['task_data']
            decrypted = self._decrypt_at_rest(raw, row['task_id'])
            task_data = json.loads(decrypted)
            timeout_at = task_data.get('timeout_at', 0)
            if timeout_at <= 0 or now < timeout_at:
                continue
            
            # 此任务已超时
            task_data['status'] = TaskStatus(task_data['status'])
            task_data['settlement_type'] = SettlementType(task_data.get('settlement_type', 'sector_coin'))
            task = ComputeTask(**{k: v for k, v in task_data.items()
                                  if k in ComputeTask.__dataclass_fields__})
            
            retry_count = task.retry_count + 1
            
            if retry_count <= self.MAX_TASK_RETRIES:
                # 还有重试机会 → 释放矿工，重置为 PENDING 等待重新调度
                self._release_task_miners(task)
                task.status = TaskStatus.PENDING
                task.assigned_miners = []
                task.results = {}
                task.retry_count = retry_count
                task.timeout_at = 0  # 重新调度时会设置新的 timeout
                self._save_task(task)
                
                # 立即尝试重新调度
                with self._lock:
                    if self.schedule_mode == ScheduleMode.BLIND and task.redundancy == 1:
                        self._create_task_blind(task)
                    else:
                        self._create_task_legacy(task)
            else:
                # 超过最大重试 → 标记失败，买家可申请退款
                self._release_task_miners(task)
                task.status = TaskStatus.FAILED
                task.retry_count = retry_count
                task.fee_breakdown["failure_reason"] = "max_retries_exceeded"
                task.fee_breakdown["refund_eligible"] = True
                self._save_task(task)
    
    def _handle_offline_miners(self):
        """扫描心跳超时的矿工，标记离线并释放其持有的任务"""
        now = time.time()
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT miner_data FROM miners
                WHERE status IN ('online', 'busy', 'mining')
            """).fetchall()
        
        for row in rows:
            miner = MinerNode.from_dict(json.loads(row['miner_data']))
            if now - miner.last_heartbeat <= self.HEARTBEAT_TIMEOUT:
                continue
            
            # 矿工心跳超时 → 标记离线
            miner.status = MinerStatus.OFFLINE
            
            # 如果矿工持有任务，不在这里处理（由 _handle_timed_out_tasks 统一处理）
            # 只清理矿工状态
            if miner.current_task_id:
                miner.current_task_id = None
            
            self.update_miner(miner)
    
    def _release_task_miners(self, task: ComputeTask):
        """释放任务关联的矿工资源"""
        for miner_id in task.assigned_miners:
            miner = self.get_miner(miner_id)
            if miner and miner.current_task_id == task.task_id:
                miner.current_task_id = None
                # 只有心跳正常的矿工才恢复为 ONLINE
                if time.time() - miner.last_heartbeat < self.HEARTBEAT_TIMEOUT:
                    miner.status = MinerStatus.ONLINE
                else:
                    miner.status = MinerStatus.OFFLINE
                self.update_miner(miner)

    def reassign(self, task_id: str) -> Tuple[bool, str]:
        """v2 公开接口：任务重调度。"""
        task = self.get_task(task_id)
        if not task:
            return False, "任务不存在"

        self._release_task_miners(task)
        task.status = TaskStatus.PENDING
        task.assigned_miners = []
        task.results = {}
        task.execution_results = {}
        task.retry_count += 1
        task.timeout_at = 0
        self._save_task(task)

        with self._lock:
            if self.schedule_mode == ScheduleMode.BLIND and task.redundancy == 1:
                return self._create_task_blind(task)
            return self._create_task_legacy(task)

    def monitor_execution(self, task_id: str) -> Tuple[bool, str]:
        """v2 公开接口：监控任务是否超时/失败并触发重调度。"""
        task = self.get_task(task_id)
        if not task:
            return False, "任务不存在"

        now = time.time()
        if task.status in (TaskStatus.FAILED, TaskStatus.TIMEOUT):
            return self.reassign(task_id)
        if task.timeout_at > 0 and now >= task.timeout_at:
            task.status = TaskStatus.TIMEOUT
            self._save_task(task)
            return self.reassign(task_id)
        return True, "任务执行正常"
    
    def close(self):
        """停止守护线程"""
        self._watchdog_running = False
        if self._watchdog_thread.is_alive():
            self._watchdog_thread.join(timeout=5)
    
    # ============== 任务进度报告 ==============
    
    def report_progress(self, task_id: str, miner_id: str,
                        progress: float, message: str = "") -> Tuple[bool, str]:
        """矿工报告任务执行进度
        
        功能：
        1. 更新任务进度（0.0~1.0）
        2. 只要进度有更新，自动延展 timeout（防止长任务被错杀）
        3. 记录最后一次进度时间（watchdog 可据此判断"卡死"）
        
        Args:
            task_id: 任务 ID
            miner_id: 矿工 ID（必须是被分配的矿工）
            progress: 进度值 0.0~1.0
            message: 可选的进度描述（如 "Epoch 5/100"）
        """
        task = self.get_task(task_id)
        if not task:
            return False, "任务不存在"
        if task.status not in (TaskStatus.ASSIGNED, TaskStatus.RUNNING):
            return False, f"任务状态不允许报告进度: {task.status.value}"
        if miner_id not in task.assigned_miners:
            return False, "你不是该任务的指定矿工"
        
        progress = max(0.0, min(1.0, progress))
        now = time.time()
        
        # 更新进度
        task.progress = progress
        task.progress_message = message[:256]  # 限制长度
        task.last_progress_at = now
        
        # 如果是首次报告进度，标记为 RUNNING
        if task.status == TaskStatus.ASSIGNED:
            task.status = TaskStatus.RUNNING
            task.started_at = now
        
        # 进度推进 → 延展超时（至少续命 HEARTBEAT_EXTEND_GRACE 秒）
        min_timeout = now + self.HEARTBEAT_EXTEND_GRACE
        if task.timeout_at < min_timeout:
            # 根据 duration_seconds 计算剩余时间，不超过原始允许的最大时长
            if task.duration_seconds > 0:
                max_deadline = task.created_at + task.duration_seconds + 300  # +5min 宽限
                task.timeout_at = min(min_timeout, max_deadline)
            else:
                task.timeout_at = min_timeout
        
        self._save_task(task)
        return True, f"进度已更新: {progress*100:.1f}%"

    def _resolve_task_profile(self, task: ComputeTask):
        """统一计算 tier/redundancy/验证模式，保证高价值任务不可无验证。"""
        # 防止未初始化随机性导致可预测采样
        if task.random_seed == 0:
            task.random_seed = secrets.randbits(64)

        # 规则1: 高价值任务最低 Tier 1
        if task.total_payment > self.HIGH_VALUE_THRESHOLD:
            task.security_tier = max(task.security_tier, 1)

        # 规则2: 低信誉买家最低 Tier 1（从 fee_breakdown 可选注入）
        buyer_rep = float(task.fee_breakdown.get("buyer_reputation", 1.0))
        if buyer_rep < self.LOW_REP_THRESHOLD:
            task.security_tier = max(task.security_tier, 1)

        task.security_tier = min(max(int(task.security_tier), 0), 3)
        cfg = self.TIER_CONFIG[task.security_tier]
        task.verification_mode = cfg["verify"]
        task.redundancy = int(cfg["k"])

        # 兼容旧字段 redundancy（只允许提高，不允许降低安全配置）
        if task.fee_breakdown.get("requested_redundancy"):
            requested = int(task.fee_breakdown["requested_redundancy"])
            task.redundancy = max(task.redundancy, requested)

        # BLIND 模式仍保留单节点，但在高 tier 强制回退到传统多节点
        if self.schedule_mode == ScheduleMode.BLIND and task.security_tier == 0:
            task.redundancy = 1

    def _compute_fee_with_tier(self, task: ComputeTask):
        base_fee = float(task.fee_breakdown.get("base_fee", task.total_payment))
        cfg = self.TIER_CONFIG.get(task.security_tier, self.TIER_CONFIG[0])
        task.fee_breakdown["tier_multiplier"] = cfg["cost"]
        task.fee_breakdown["tier_fee"] = base_fee * cfg["cost"]

    def _match_score(self, miner: MinerNode, task: ComputeTask) -> float:
        compute_denom = max(task.compute_required, 1e-9)
        memory_denom = max(task.memory_required, 1e-9)
        compute_score = miner.compute_power / compute_denom
        memory_score = miner.gpu_memory / memory_denom
        reliability = miner.reputation_mean - 1.5 * miner.reputation_uncertainty
        return (
            0.4 * compute_score +
            0.3 * memory_score +
            0.3 * reliability -
            0.2 * miner.latency
        )

    def _generate_sampling_indices(self, seed: int, size: int) -> List[int]:
        if size <= 0:
            return []
        rng = random.Random(seed)
        k = max(self.MIN_SAMPLING_POINTS, int(size * self.SAMPLING_RATIO))
        k = min(k, size)
        return rng.sample(range(size), k=k)

    def _verify_sampling(self, outputs: List[str], seed: int) -> bool:
        if len(outputs) < 2:
            return False
        sample_source = outputs[0]
        indices = self._generate_sampling_indices(seed, len(sample_source))
        if not indices:
            return False
        baseline = "".join(sample_source[i] for i in indices)
        for out in outputs[1:]:
            if len(out) != len(sample_source):
                return False
            if "".join(out[i] for i in indices) != baseline:
                return False
        return True

    def _consensus_variance(self, outputs: List[str]) -> float:
        # 字符串结果下采用离散一致性度量：1 - max_freq / n
        n = len(outputs)
        if n <= 1:
            return 1.0
        counts: Dict[str, int] = {}
        for out in outputs:
            counts[out] = counts.get(out, 0) + 1
        max_freq = max(counts.values())
        return 1.0 - (max_freq / n)

    def _slash_miner(self, miner_id: str, amount: float, reason: str):
        miner = self.get_miner(miner_id)
        if not miner:
            return
        miner.stake = max(0.0, miner.stake - max(amount, 0.0))
        miner.tasks_failed += 1
        miner.update_reputation(False)
        miner.update_combined_score(self.POUW_WEIGHT, self.USER_RATING_WEIGHT)
        self.update_miner(miner)

    def _reward_dispute_winner(self, miner_id: str, bonus: float):
        miner = self.get_miner(miner_id)
        if not miner:
            return
        miner.total_earnings += max(bonus, 0.0)
        miner.update_reputation(True)
        miner.update_combined_score(self.POUW_WEIGHT, self.USER_RATING_WEIGHT)
        self.update_miner(miner)

    def _maybe_decay_reputation(self):
        now = time.time()
        if self._last_decay_at and now - self._last_decay_at < self.DECAY_INTERVAL_SECONDS:
            return

        with self._conn() as conn:
            rows = conn.execute("SELECT miner_data FROM miners").fetchall()

        for row in rows:
            miner = MinerNode.from_dict(json.loads(row['miner_data']))
            miner.decay_reputation(self.REPUTATION_DECAY_FACTOR)
            miner.update_combined_score(self.POUW_WEIGHT, self.USER_RATING_WEIGHT)
            self.update_miner(miner)

        self._last_decay_at = now
    
    # ============== 任务调度 ==============
    
    def create_task(self, task: ComputeTask, required_miners: int = 1) -> Tuple[bool, str]:
        """创建任务并调度矿机
        
        盲调度模式（BLIND）：
            - 只需 1 个矿工执行（无冗余浪费）
            - 任务伪装为挖矿挑战推送给矿工
            - 通过陷阱题验证矿工诚实性
        
        传统模式（HYBRID/FORCED/VOLUNTARY）：
            - 向后兼容：多矿工冗余执行 + 多数派共识
        """
        with self._lock:
            # 安全验证：检查任务数据合法性
            valid, err = TaskDataValidator.validate(task)
            if not valid:
                return False, f"任务数据验证失败: {err}"

            # v2 任务约束与验证配置
            task.fee_breakdown["requested_redundancy"] = required_miners
            self._resolve_task_profile(task)
            self._compute_fee_with_tier(task)
            
            # 记录任务数据哈希（用于后续完整性审计）
            task.fee_breakdown["task_data_hash"] = TaskDataValidator.compute_task_hash(task.task_data)
            
            # 动态超时：优先使用买家指定的执行时长，否则用默认值
            effective_timeout = self.TASK_TIMEOUT
            if task.duration_seconds > 0:
                effective_timeout = min(task.duration_seconds, self.MAX_TASK_DURATION)
            task.timeout_at = time.time() + effective_timeout
            
            if self.schedule_mode == ScheduleMode.BLIND:
                # ---- 盲调度模式：单矿工 + 陷阱验证 ----
                if task.redundancy == 1:
                    return self._create_task_blind(task)
                # 高安全级任务回退到传统多节点验证
                return self._create_task_legacy(task)
            else:
                # ---- 传统模式：多矿工冗余 ----
                task.redundancy = min(task.redundancy, self.MAX_REDUNDANCY)
                return self._create_task_legacy(task)

    def _create_task_blind(self, task: ComputeTask) -> Tuple[bool, str]:
        """盲调度 - 矿工以为自己在挖矿

        流程：
            1. 选择 1 个最优矿工
            2. 将任务伪装成 mining_challenge 
            3. 创建包含陷阱题的 BlindBatch
            4. 矿工心跳时收到伪装的挖矿挑战
        """
        sector = task.sector
        
        # 解析任务的内存需求
        min_memory_gb = 0.0
        try:
            task_info = json.loads(task.task_data)
            if isinstance(task_info, dict):
                min_memory_gb = float(task_info.get("maxMemoryGb", 0.0))
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

        # 获取所有可用矿工，应用惩罚穿透安全限额 (Slashing Bound)
        available = self._get_available_miners(sector, min_memory_gb=min_memory_gb, task_value=task.total_payment)
        if not available:
            return False, "当前无可用矿工可执行盲任务"
        # 优先选择高信任度矿工（陷阱开销更低）
        def blind_sort_key(m):
            trust = self.blind_engine.get_trust_profile(m.miner_id)
            return (trust.get("trust_score", 0.5), m.combined_score)
        
        available.sort(key=blind_sort_key, reverse=True)
        selected_miner = available[0]
        
        # 解析任务数据
        try:
            task_data_dict = json.loads(task.task_data) if isinstance(task.task_data, str) else task.task_data
        except (json.JSONDecodeError, TypeError):
            task_data_dict = {"raw": str(task.task_data)}
        
        # 创建盲批次（真实任务 + 陷阱题）
        blind_batch = self.blind_engine.create_blind_batch(
            miner_id=selected_miner.miner_id,
            real_tasks=[(task.task_id, task_data_dict)],
        )
        
        # 记录批次 ID 到任务
        task.assigned_miners = [selected_miner.miner_id]
        task.status = TaskStatus.ASSIGNED
        task.assigned_at = time.time()
        
        # 在 fee_breakdown 中记录盲调度信息
        task.fee_breakdown["blind_batch_id"] = blind_batch.batch_id
        task.fee_breakdown["blind_mode"] = True
        task.fee_breakdown["trap_count"] = len(blind_batch._trap_ids)
        
        self._save_task(task)
        
        # 更新矿工状态 - 注意：矿工状态仍显示 MINING（不暴露任务）
        selected_miner.current_task_id = task.task_id
        # 关键：不改变矿工状态为 BUSY，保持 MINING 状态
        # 矿工自己看到的是 "正在挖矿"
        if selected_miner.status == MinerStatus.ONLINE:
            selected_miner.status = MinerStatus.MINING
        self.update_miner(selected_miner)
        
        return True, f"盲调度成功：矿工 {selected_miner.miner_id} 将以挖矿方式执行任务（含 {len(blind_batch._trap_ids)} 个陷阱题）"

    def _create_task_legacy(self, task: ComputeTask) -> Tuple[bool, str]:
        """传统调度（向后兼容 - 多矿工冗余 + 多数派共识）"""
        assigned = self._schedule_miners(task)
        
        if len(assigned) < task.redundancy:
            return False, f"可用矿机不足，需要 {task.redundancy}，仅有 {len(assigned)}"
        
        task.assigned_miners = assigned
        task.status = TaskStatus.ASSIGNED
        task.assigned_at = time.time()
        
        self._save_task(task)
        
        for miner_id in assigned:
            miner = self.get_miner(miner_id)
            if miner:
                miner.status = MinerStatus.BUSY
                miner.current_task_id = task.task_id
                self.update_miner(miner)
        
        return True, f"任务已创建并分配到 {len(assigned)} 个矿机"
    
    def _schedule_miners(self, task: ComputeTask) -> List[str]:
        """根据调度模式选择矿机"""
        sector = task.sector
        required = task.redundancy
        
        # 解析任务的内存需求与价值 (Slashing Bounds)
        min_memory_gb = 0.0
        try:
            task_info = json.loads(task.task_data)
            if isinstance(task_info, dict):
                min_memory_gb = float(task_info.get("maxMemoryGb", 0.0))
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

        # 获取所有可用矿机，应用惩罚穿透安全限额 (Slashing Bound)
        voluntary_miners = self._get_available_miners(
            sector, mode=MinerMode.VOLUNTARY, min_memory_gb=min_memory_gb, task_value=task.total_payment)
        forced_miners = self._get_available_miners(
            sector, mode=MinerMode.FORCED, min_memory_gb=min_memory_gb, task_value=task.total_payment)
        all_miners = voluntary_miners + forced_miners

        # v2 调度评分：异构能力 + 信誉置信惩罚 + 延迟惩罚
        ranked_all = sorted(
            all_miners,
            key=lambda m: self._match_score(m, task),
            reverse=True,
        )

        if self.schedule_mode == ScheduleMode.VOLUNTARY:
            # 自主模式：只用自主矿工
            selected = self._select_best_miners(voluntary_miners, required)
            
        elif self.schedule_mode == ScheduleMode.FORCED:
            # 强制模式：系统全权调度
            selected = [m.miner_id for m in ranked_all[:required]]
            
        elif self.schedule_mode == ScheduleMode.HYBRID:
            # 混合模式：优先自主，不足时强制
            ranked_voluntary = sorted(
                voluntary_miners,
                key=lambda m: self._match_score(m, task),
                reverse=True,
            )
            selected = [m.miner_id for m in ranked_voluntary[:required]]
            
            if len(selected) < required:
                # 自主矿工不足，补充强制矿工
                remaining = required - len(selected)
                ranked_forced = sorted(
                    forced_miners,
                    key=lambda m: self._match_score(m, task),
                    reverse=True,
                )
                forced_selected = [m.miner_id for m in ranked_forced[:remaining]]
                selected.extend(forced_selected)
        else:
            selected = [m.miner_id for m in ranked_all[:required]]
        
        return selected
    
    def _get_available_miners(self, sector: str, mode: Optional[MinerMode] = None, min_memory_gb: float = 0.0, task_value: float = 0.0) -> List[MinerNode]:
        """获取可用矿机列表，包含安全风控(Slashing Bounds)过滤"""
        with self._conn() as conn:
            query = """
                SELECT miner_data FROM miners
                WHERE sector = ?
                AND status IN ('online', 'mining')
                AND mode != 'disabled'
            """
            params = [sector]

            if mode:
                query += " AND mode = ?"
                params.append(mode.value)

            rows = conn.execute(query, params).fetchall()

            miners = []
            for row in rows:
                miner = MinerNode.from_dict(json.loads(row['miner_data']))
                # 检查内存是否满足
                if miner.gpu_memory < min_memory_gb:
                    continue
                # 【V1.0 - 惩罚穿透安全限额 (Slashing Bound) 过滤】
                # 若任务价值过高而节点质押不足，则不允许分配
                if task_value > 0 and task_value > (miner.stake * self.SLASH_RATIO):
                    logger.debug(f"[风控] 矿工 {miner.miner_id} 质押 {miner.stake} 不足承接价值 {task_value} 的任务.")
                    continue
                # 检查心跳超时
                if time.time() - miner.last_heartbeat < self.HEARTBEAT_TIMEOUT:
                    miners.append(miner)
            
            return miners
    
    def _select_best_miners(self, miners: List[MinerNode], count: int) -> List[str]:
        """按综合评分+随机性选择矿机（防刷单）
        
        使用加权随机抽样而非纯排名，防止用户与矿工串通刷单。
        高分矿工仍有更高概率被选中，但不是确定性的。
        """
        if not miners:
            return []
        
        import secrets
        secure_rng = secrets.SystemRandom()
        
        if len(miners) <= count:
            selected = [m.miner_id for m in miners]
            secure_rng.shuffle(selected)
            return selected
        
        # 加权随机抽样：分数作为权重，给低分者也留机会
        weights = [max(m.combined_score, 0.01) for m in miners]
        selected_miners = secure_rng.choices(miners, weights=weights, k=count * 2)
        # 去重并截取
        seen = set()
        selected = []
        for m in selected_miners:
            if m.miner_id not in seen:
                seen.add(m.miner_id)
                selected.append(m.miner_id)
            if len(selected) >= count:
                break
        
        # 万一去重后不够，补充
        if len(selected) < count:
            for m in miners:
                if m.miner_id not in seen:
                    selected.append(m.miner_id)
                    seen.add(m.miner_id)
                if len(selected) >= count:
                    break
        
        return selected
    
    # ============== 存储加密 ==============
    
    # 静态密钥种子（生产环境应从安全配置/HSM 加载）
    _STORAGE_KEY_SEED = os.environ.get("POUW_STORAGE_KEY", "POUW_SCHEDULER_STORAGE_KEY_2026")
    
    def _derive_storage_key(self, context: str) -> bytes:
        """为指定上下文派生存储加密密钥"""
        return hashlib.pbkdf2_hmac(
            'sha256',
            self._STORAGE_KEY_SEED.encode(),
            context.encode(),
            iterations=100_000
        )
    
    def _encrypt_at_rest(self, plaintext: str, context: str) -> str:
        """静态加密（存储时）
        
        优先使用 AES-256-GCM（如果加密库可用），
        否则使用 HMAC-SHA256 密钥流做 XOR + 完整性标签。
        返回带前缀的 base64 字符串。
        """
        key = self._derive_storage_key(context)
        data = plaintext.encode('utf-8')
        
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            import secrets as _secrets
            nonce = _secrets.token_bytes(12)
            aesgcm = AESGCM(key)
            ct = aesgcm.encrypt(nonce, data, context.encode())
            return "AES:" + base64.b64encode(nonce + ct).decode()
        except ImportError:
            pass
        
        try:
            from Crypto.Cipher import AES as _AES
            import secrets as _secrets
            nonce = _secrets.token_bytes(12)
            cipher = _AES.new(key, _AES.MODE_GCM, nonce=nonce)
            ct, tag = cipher.encrypt_and_digest(data)
            return "AES:" + base64.b64encode(nonce + tag + ct).decode()
        except ImportError:
            pass
        
        # 回退: HMAC 密钥流 XOR + 完整性标签
        key_stream = b''
        for i in range((len(data) // 32) + 1):
            key_stream += _hmac.new(key, f"{i}".encode(), hashlib.sha256).digest()
        key_stream = key_stream[:len(data)]
        ct = bytes(a ^ b for a, b in zip(data, key_stream))
        tag = _hmac.new(key, ct, hashlib.sha256).hexdigest()[:16]
        return "XOR:" + tag + ":" + base64.b64encode(ct).decode()
    
    def _decrypt_at_rest(self, ciphertext: str, context: str) -> str:
        """解密存储数据（自动识别格式，向后兼容明文）"""
        key = self._derive_storage_key(context)
        
        if ciphertext.startswith("AES:"):
            raw = base64.b64decode(ciphertext[4:])
            try:
                from cryptography.hazmat.primitives.ciphers.aead import AESGCM
                nonce, ct = raw[:12], raw[12:]
                aesgcm = AESGCM(key)
                return aesgcm.decrypt(nonce, ct, context.encode()).decode('utf-8')
            except ImportError:
                pass
            try:
                from Crypto.Cipher import AES as _AES
                nonce, tag, ct = raw[:12], raw[12:28], raw[28:]
                cipher = _AES.new(key, _AES.MODE_GCM, nonce=nonce)
                return cipher.decrypt_and_verify(ct, tag).decode('utf-8')
            except ImportError:
                raise RuntimeError("无法解密 AES 存储数据：缺少加密库")
        
        elif ciphertext.startswith("XOR:"):
            parts = ciphertext[4:].split(":", 1)
            tag_stored, b64_ct = parts[0], parts[1]
            ct = base64.b64decode(b64_ct)
            tag_check = _hmac.new(key, ct, hashlib.sha256).hexdigest()[:16]
            if not _hmac.compare_digest(tag_stored, tag_check):
                raise ValueError("存储数据完整性校验失败：数据可能被篡改")
            key_stream = b''
            for i in range((len(ct) // 32) + 1):
                key_stream += _hmac.new(key, f"{i}".encode(), hashlib.sha256).digest()
            key_stream = key_stream[:len(ct)]
            return bytes(a ^ b for a, b in zip(ct, key_stream)).decode('utf-8')
        
        else:
            # 向后兼容：未加密的旧数据（纯 JSON）
            return ciphertext
    
    # ============== 任务持久化 ==============
    
    def _save_task(self, task: ComputeTask):
        """保存任务（敏感字段加密存储）"""
        task_dict = task.to_dict()
        serialized = json.dumps(task_dict)
        encrypted = self._encrypt_at_rest(serialized, task.task_id)
        
        with self._conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO tasks
                (task_id, task_data, order_id, sector, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                task.task_id,
                encrypted,
                task.order_id,
                task.sector,
                task.status.value,
                task.created_at,
                time.time()
            ))
    
    def get_task(self, task_id: str) -> Optional[ComputeTask]:
        """获取任务（自动解密）"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT task_data FROM tasks WHERE task_id = ?",
                (task_id,)
            ).fetchone()
            if not row:
                return None
            
            raw = row['task_data']
            decrypted = self._decrypt_at_rest(raw, task_id)
            
            data = json.loads(decrypted)
            data['status'] = TaskStatus(data['status'])
            data['settlement_type'] = SettlementType(data.get('settlement_type', 'sector_coin'))
            return ComputeTask(**{k: v for k, v in data.items() 
                                  if k in ComputeTask.__dataclass_fields__})
    
    # ============== 结果提交与验证 ==============
    
    def submit_result(self, task_id: str, miner_id: str, result_hash: str) -> Tuple[bool, str]:
        """矿工提交任务结果"""
        task = self.get_task(task_id)
        if not task:
            return False, "任务不存在"
        
        if miner_id not in task.assigned_miners:
            return False, "该矿工未分配此任务"
        
        # 记录结果
        task.results[miner_id] = result_hash
        task.execution_results[miner_id] = ExecutionResult(
            task_id=task_id,
            node_id=miner_id,
            output=result_hash,
            partial_hash=hashlib.sha256(result_hash.encode()).hexdigest()[:16],
        ).__dict__
        
        if self.schedule_mode == ScheduleMode.BLIND:
            # ---- 盲调度模式：直接进入盲验证 ----
            task.status = TaskStatus.VERIFYING
            self._save_task(task)
            self._verify_blind(task, miner_id, result_hash)
        else:
            # ---- 传统模式：多数派共识 ----
            if len(task.results) >= task.redundancy:
                task.status = TaskStatus.VERIFYING
                self._save_task(task)
                self._verify_results_legacy(task)
            else:
                task.status = TaskStatus.RUNNING
                self._save_task(task)
        
        return True, "结果已提交"
    
    def submit_blind_batch(self, batch_id: str, miner_id: str,
                            results: Dict[str, str]) -> Tuple[bool, Dict]:
        """矿工提交盲批次结果（矿工以为是提交挖矿结果）
        
        Args:
            batch_id: 批次 ID
            miner_id: 矿工 ID
            results: {challenge_id: result_hash, ...}
            
        Returns:
            (success, report)
        """
        # 通过 BlindTaskEngine 验证陷阱 + 提取真实结果
        is_trusted, report = self.blind_engine.verify_batch(batch_id, results)
        
        if is_trusted:
            # 陷阱通过 → 接受真实结果
            real_results = report.get("real_results", {})
            for original_task_id, result_hash in real_results.items():
                task = self.get_task(original_task_id)
                if task:
                    task.results[miner_id] = result_hash
                    task.final_result = result_hash
                    task.status = TaskStatus.COMPLETED
                    task.completed_at = time.time()
                    task.challenge_window_end = time.time() + self.DISPUTE_WINDOW_SECONDS
                    
                    # 提升矿工评分
                    miner = self.get_miner(miner_id)
                    if miner:
                        miner.pouw_score = min(miner.pouw_score + 0.01, 2.0)
                        miner.tasks_completed += 1
                        miner.update_combined_score(self.POUW_WEIGHT, self.USER_RATING_WEIGHT)
                        miner.current_task_id = None
                        miner.status = MinerStatus.ONLINE
                        self.update_miner(miner)
                    
                    self._settle_task(task)
                    self._save_task(task)
        else:
            # 陷阱失败 → 拒绝结果，重新调度
            original_task_ids = report.get("original_task_ids", [])
            for original_task_id in original_task_ids:
                task = self.get_task(original_task_id)
                if task:
                    task.status = TaskStatus.PENDING
                    task.assigned_miners = []
                    task.results = {}
                    self._save_task(task)
            
            # 惩罚矿工
            miner = self.get_miner(miner_id)
            if miner:
                miner.pouw_score = max(miner.pouw_score - 0.10, 0.1)
                miner.tasks_failed += 1
                miner.update_combined_score(self.POUW_WEIGHT, self.USER_RATING_WEIGHT)
                miner.current_task_id = None
                miner.status = MinerStatus.ONLINE
                self.update_miner(miner)
        
        return is_trusted, report
    
    def _verify_blind(self, task: ComputeTask, miner_id: str, result_hash: str):
        """盲模式验证（单结果提交时）
        
        盲调度模式下，矿工必须通过 submit_blind_batch 提交（含陷阱验证）。
        通过传统 submit_result 提交的结果在盲模式下被拒绝，
        防止矿工绕过陷阱验证直接获得报酬。
        """
        # 拒绝非 batch 方式的提交 — 盲模式必须走 submit_blind_batch
        logger.warning(
            f"[调度器] 盲模式下拒绝非batch提交: task={task.task_id}, miner={miner_id}"
        )
        task.status = TaskStatus.RUNNING  # 回退状态，等待 batch 提交
        self._save_task(task)
    
    def _verify_results_legacy(self, task: ComputeTask):
        """v2 验证层（none/consensus/sampling + 仲裁）。"""
        outputs = list(task.results.values())
        if not outputs:
            task.status = TaskStatus.FAILED
            task.fee_breakdown["verification_fail_reason"] = "empty_results"
            self._save_task(task)
            return

        result_counts: Dict[str, int] = {}
        for out in outputs:
            result_counts[out] = result_counts.get(out, 0) + 1

        majority_result = max(result_counts.items(), key=lambda item: item[1])[0]
        majority_count = result_counts[majority_result]
        verification_ok = False
        verification_mode = (task.verification_mode or "consensus").lower()

        if verification_mode == "none":
            verification_ok = True
        elif verification_mode == "sampling":
            verification_ok = self._verify_sampling(outputs, task.random_seed)
        else:
            variance = self._consensus_variance(outputs)
            threshold = float(task.fee_breakdown.get(
                "consensus_variance_threshold", self.CONSENSUS_VARIANCE_THRESHOLD
            ))
            verification_ok = variance <= threshold and majority_count >= (task.redundancy // 2) + 1

        if verification_ok:
            task.final_result = majority_result
            task.status = TaskStatus.COMPLETED
            task.completed_at = time.time()
            task.challenge_window_end = time.time() + self.DISPUTE_WINDOW_SECONDS
            task.fee_breakdown["challenge_window"] = self.DISPUTE_WINDOW_SECONDS

            # 经济分配：k=1 全额给执行者；k>=2 按 70/30 给执行者和验证者
            correct_miners = [mid for mid, result in task.results.items() if result == majority_result]
            verifier_miners = [mid for mid, result in task.results.items() if result != majority_result]
            if task.redundancy == 1:
                task.fee_breakdown["reward_scheme"] = "single_executor_full_reward"
            elif task.redundancy >= 2:
                task.fee_breakdown["reward_scheme"] = "executor_70_verifier_30"
                task.fee_breakdown["executor_share_ratio"] = 0.7
                task.fee_breakdown["verifier_share_ratio"] = 0.3

            for miner_id in task.assigned_miners:
                miner = self.get_miner(miner_id)
                if not miner:
                    continue
                if miner_id in correct_miners:
                    miner.pouw_score = min(miner.pouw_score + 0.01, 2.0)
                    miner.tasks_completed += 1
                    miner.update_reputation(True)
                else:
                    miner.pouw_score = max(miner.pouw_score - 0.05, 0.1)
                    miner.tasks_failed += 1
                    miner.update_reputation(False)

                miner.update_combined_score(self.POUW_WEIGHT, self.USER_RATING_WEIGHT)
                miner.current_task_id = None
                miner.status = MinerStatus.ONLINE
                self.update_miner(miner)

            self._settle_task(task)
            self._save_task(task)
            return

        # 验证失败：触发仲裁并执行经济惩罚
        task.fee_breakdown["verification_failed"] = True
        task.fee_breakdown["verification_mode"] = verification_mode
        resolved = self._resolve_dispute(task, majority_result)
        if not resolved:
            # 仲裁无法完成时执行重调度
            task.status = TaskStatus.PENDING
            task.retry_count += 1
            task.assigned_miners = []
            task.results = {}
            task.execution_results = {}
            task.timeout_at = 0
        self._save_task(task)

    def _resolve_dispute(self, task: ComputeTask, majority_result: str) -> bool:
        """争议仲裁：引入第三节点做多数裁决，失败则重调度。"""
        # 选择不在已分配矿工中的仲裁节点
        available = self._get_available_miners(task.sector, min_memory_gb=task.memory_required)
        arbitrators = [m for m in available if m.miner_id not in task.assigned_miners]
        arbitrators = sorted(arbitrators, key=lambda m: self._match_score(m, task), reverse=True)
        if not arbitrators:
            task.fee_breakdown["dispute_resolution"] = "no_arbitrator_available"
            return False

        arbiter = arbitrators[0]
        # 当前版本无法在调度器内主动执行真实计算，采用密码学随机 tie-break challenge。
        # 约束：不可预测随机，用 random_seed + 节点id 生成可审计仲裁输出。
        arbitration_result = hashlib.sha256(
            f"{task.random_seed}:{task.task_id}:{arbiter.miner_id}".encode()
        ).hexdigest()

        votes = list(task.results.values()) + [arbitration_result]
        counts: Dict[str, int] = {}
        for out in votes:
            counts[out] = counts.get(out, 0) + 1
        winner = max(counts.items(), key=lambda item: item[1])[0]

        if counts[winner] < 2:
            task.fee_breakdown["dispute_resolution"] = "unresolved"
            return False

        task.final_result = winner
        task.status = TaskStatus.COMPLETED
        task.completed_at = time.time()
        task.fee_breakdown["dispute_resolution"] = "resolved_by_arbiter"
        task.fee_breakdown["arbiter_node"] = arbiter.miner_id

        for miner_id, result in task.results.items():
            miner = self.get_miner(miner_id)
            if not miner:
                continue
            if result == winner:
                miner.update_reputation(True)
                self._reward_dispute_winner(miner_id, float(task.fee_breakdown.get("dispute_bonus", 0.0)))
            else:
                slash_amount = miner.stake * self.SLASH_RATIO
                self._slash_miner(miner_id, slash_amount, "verification_fail")
            miner.current_task_id = None
            miner.status = MinerStatus.ONLINE
            self.update_miner(miner)

        self._settle_task(task)
        return True
    
    def _settle_task(self, task: ComputeTask):
        """任务结算（去中心化费用分配）
        
        费用分配：
        - 0.5% 直接销毁（通缩机制）
        - 0.3% 区块矿工激励
        - 0.2% 基金会多签钱包（运维服务）
        注意：实际结算应在 block_chain 层面等待 challenge_window_end 公示期结束后进行。此处仅更新资产凭证以防超前消费。
        """
        if task.total_payment <= 0:
            return
        
        # 计算各项费用
        burn_fee = task.total_payment * self.BURN_FEE_RATE       # 0.5% 销毁
        miner_fee = task.total_payment * self.MINER_FEE_RATE     # 0.3% 矿工
        foundation_fee = task.total_payment * self.FOUNDATION_FEE_RATE  # 0.2% 基金会
        total_fee = burn_fee + miner_fee + foundation_fee        # 1.0% 总费
        
        distributable = task.total_payment - total_fee
        
        # 记录费用分配（使用 update 保留已有的审计字段如 task_data_hash、buyer_public_key 等）
        task.fee_breakdown.update({
            "burn": burn_fee,
            "miner_incentive": miner_fee,
            "foundation": foundation_fee,
            "total_fee": total_fee,
        })
        
        # 按贡献分配（v2：支持 k=2 执行者/验证者分成）
        correct_miners = [mid for mid, result in task.results.items()
                          if result == task.final_result]
        verifier_miners = [mid for mid, result in task.results.items()
                           if result != task.final_result]

        scheme = task.fee_breakdown.get("reward_scheme", "equal_split")
        if scheme == "executor_70_verifier_30" and correct_miners:
            executor_pool = distributable * 0.7
            verifier_pool = distributable * 0.3
            per_executor = executor_pool / len(correct_miners)
            for miner_id in correct_miners:
                task.miner_payments[miner_id] = task.miner_payments.get(miner_id, 0.0) + per_executor

            if verifier_miners:
                per_verifier = verifier_pool / len(verifier_miners)
                for miner_id in verifier_miners:
                    task.miner_payments[miner_id] = task.miner_payments.get(miner_id, 0.0) + per_verifier
            elif correct_miners:
                # 没有独立验证者时，剩余验证池返还给执行者
                per_executor_bonus = verifier_pool / len(correct_miners)
                for miner_id in correct_miners:
                    task.miner_payments[miner_id] += per_executor_bonus
        elif correct_miners:
            per_miner = distributable / len(correct_miners)
            for miner_id in correct_miners:
                task.miner_payments[miner_id] = task.miner_payments.get(miner_id, 0.0) + per_miner

        for miner_id, amount in task.miner_payments.items():
            miner = self.get_miner(miner_id)
            if miner:
                miner.total_earnings += amount
                self.update_miner(miner)
        
        # 链上交易通过共识层提交（结算记录先持久化，再由出块矿工打包）
        # 结算事件由 RPC 层的结算钩子（settlement_hook）触发链上交易创建：
        #   - 销毁交易: burn_fee 被永久移除
        #   - 矿工激励: miner_fee 分配给出块矿工
        #   - 基金会转账: foundation_fee 转入 DAO 国库
        # 此处先写入本地数据库，确保即使节点重启也不丢失结算数据
        
        # ── 结算记录持久化（链上结算通过共识层完成） ──
        settlement_record = {
            "task_id": task.task_id,
            "settled_at": time.time(),
            "total_payment": task.total_payment,
            "burn_amount": burn_fee,
            "miner_incentive": miner_fee,
            "foundation_fee": foundation_fee,
            "miner_payments": task.miner_payments,
            "settlement_type": task.settlement_type.value,
        }
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS settlements (
                    task_id TEXT PRIMARY KEY,
                    settlement_data TEXT NOT NULL,
                    settled_at REAL NOT NULL
                )
            """)
            conn.execute(
                "INSERT OR REPLACE INTO settlements (task_id, settlement_data, settled_at) VALUES (?, ?, ?)",
                (task.task_id, json.dumps(settlement_record), time.time())
            )
        
        task.fee_breakdown["settlement_status"] = "recorded"
        
        self._save_task(task)
    
    # ============== 用户评分 ==============

    def compute_fee(self, base_fee: float, tier: int) -> float:
        tier = min(max(int(tier), 0), 3)
        return float(base_fee) * float(self.TIER_CONFIG[tier]["cost"])

    def distribute_reward(self, task_id: str) -> Tuple[bool, str]:
        task = self.get_task(task_id)
        if not task:
            return False, "任务不存在"
        if task.status != TaskStatus.COMPLETED:
            return False, "任务尚未完成"
        self._settle_task(task)
        return True, "奖励分配完成"

    def slash(self, miner_id: str, amount: float) -> Tuple[bool, str]:
        miner = self.get_miner(miner_id)
        if not miner:
            return False, "矿工不存在"
        self._slash_miner(miner_id, amount, "manual_slash")
        return True, f"已对矿工 {miner_id} 执行惩罚 {amount:.6f}"
    
    def rate_miner(self, miner_id: str, rating: float, 
                   rating_fee: float = 0.001) -> Tuple[bool, str]:
        """用户评分（需要小额绑定费用防刷分）
        
        Args:
            miner_id: 矿工ID
            rating: 评分 (1-5)
            rating_fee: 评分费用（防刷分）
        """
        if not 1.0 <= rating <= 5.0:
            return False, "评分必须在 1-5 之间"
        
        miner = self.get_miner(miner_id)
        if not miner:
            return False, "矿工不存在"
        
        # 更新用户评分（指数移动平均）
        alpha = 0.1  # 新评分权重
        miner.user_rating = alpha * rating + (1 - alpha) * miner.user_rating
        
        # 更新综合评分
        miner.update_combined_score(self.POUW_WEIGHT, self.USER_RATING_WEIGHT)
        self.update_miner(miner)
        
        return True, f"评分成功: {rating:.1f}"
    
    # ============== 统计 ==============
    
    def get_sector_stats(self, sector: str) -> Dict:
        """获取板块统计"""
        with self._conn() as conn:
            miners = conn.execute("""
                SELECT 
                    COUNT(*) as total_miners,
                    SUM(CASE WHEN status = 'online' THEN 1 ELSE 0 END) as online_miners,
                    SUM(CASE WHEN status = 'busy' THEN 1 ELSE 0 END) as busy_miners,
                    SUM(CASE WHEN mode = 'voluntary' THEN 1 ELSE 0 END) as voluntary_miners,
                    SUM(CASE WHEN mode = 'forced' THEN 1 ELSE 0 END) as forced_miners,
                    SUM(compute_power) as total_power,
                    AVG(combined_score) as avg_score
                FROM miners WHERE sector = ?
            """, (sector,)).fetchone()
            
            tasks = conn.execute("""
                SELECT 
                    COUNT(*) as total_tasks,
                    SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending_tasks,
                    SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) as running_tasks,
                    SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed_tasks
                FROM tasks WHERE sector = ?
            """, (sector,)).fetchone()
            
            return {
                "sector": sector,
                "miners": {
                    "total": miners['total_miners'] or 0,
                    "online": miners['online_miners'] or 0,
                    "busy": miners['busy_miners'] or 0,
                    "voluntary": miners['voluntary_miners'] or 0,
                    "forced": miners['forced_miners'] or 0,
                    "total_power": miners['total_power'] or 0,
                    "avg_score": miners['avg_score'] or 0,
                },
                "tasks": {
                    "total": tasks['total_tasks'] or 0,
                    "pending": tasks['pending_tasks'] or 0,
                    "running": tasks['running_tasks'] or 0,
                    "completed": tasks['completed_tasks'] or 0,
                },
                "blind_engine": self.blind_engine.get_stats() if self.schedule_mode == ScheduleMode.BLIND else None,
            }


# ============== 单例 ==============

_scheduler: Optional[ComputeScheduler] = None

def get_compute_scheduler(mode: ScheduleMode = ScheduleMode.BLIND) -> ComputeScheduler:
    """获取调度器单例"""
    global _scheduler
    if _scheduler is None:
        _scheduler = ComputeScheduler(mode=mode)
    return _scheduler


# ============== 测试 ==============

if __name__ == "__main__":
    import os
    
    print("=" * 60)
    print("算力调度器测试 - 盲调度模式（矿工无感知）")
    print("=" * 60)
    
    # 清理测试数据
    test_db = "data/test_scheduler.db"
    if os.path.exists(test_db):
        os.remove(test_db)
    
    scheduler = ComputeScheduler(db_path=test_db, mode=ScheduleMode.BLIND)
    
    # 1. 注册矿机
    print("\n[1] 注册矿机（矿工以为只是注册挖矿）...")
    
    miners = [
        MinerNode(
            miner_id="miner_001",
            address="MAIN_addr1",
            sector="RTX3080",
            gpu_model="RTX 4060",
            gpu_memory=8.0,
            compute_power=73.0,
            mode=MinerMode.VOLUNTARY,
            status=MinerStatus.ONLINE,
        ),
        MinerNode(
            miner_id="miner_002",
            address="MAIN_addr2",
            sector="RTX3080",
            gpu_model="RTX 3080",
            gpu_memory=10.0,
            compute_power=119.0,
            mode=MinerMode.FORCED,
            status=MinerStatus.ONLINE,
        ),
        MinerNode(
            miner_id="miner_003",
            address="MAIN_addr3",
            sector="RTX3080",
            gpu_model="RTX 3070",
            gpu_memory=8.0,
            compute_power=81.0,
            mode=MinerMode.VOLUNTARY,
            status=MinerStatus.ONLINE,
        ),
    ]
    
    for miner in miners:
        ok, msg = scheduler.register_miner(miner)
        print(f"    {miner.miner_id} ({miner.mode.value}): {msg}")
    
    # 2. 用户租用算力（盲调度 - 只需 1 个矿工）
    print("\n[2] 用户提交任务（盲调度 - 单矿工执行）...")
    
    task = ComputeTask(
        task_id="task_001",
        order_id="order_001",
        buyer_address="BUYER_001",
        task_type="training",
        task_data='{"model": "llama", "epochs": 10}',
        sector="RTX3080",
        total_payment=1.0,
    )
    
    ok, msg = scheduler.create_task(task, required_miners=1)
    print(f"    {msg}")
    
    # 3. 查看任务状态
    print("\n[3] 任务状态...")
    task = scheduler.get_task("task_001")
    if task is None:
        raise RuntimeError("任务不存在: task_001")
    print(f"    分配矿工: {task.assigned_miners}")
    print(f"    盲模式: {task.fee_breakdown.get('blind_mode', False)}")
    print(f"    陷阱数: {task.fee_breakdown.get('trap_count', 0)}")
    
    # 4. 矿工心跳（以为在挖矿）
    print("\n[4] 矿工心跳（矿工状态显示: MINING）...")
    miner_id = task.assigned_miners[0]
    has_task, t = scheduler.miner_heartbeat(miner_id)
    miner = scheduler.get_miner(miner_id)
    print(f"    {miner_id}: 状态={miner.status.value}（矿工以为在挖矿！）")
    
    # 5. 获取盲批次（矿工看到的是 "mining_challenge"）
    print("\n[5] 矿工获取挖矿挑战（实际是伪装的付费任务+陷阱题）...")
    batch_view = scheduler.get_blind_batch_for_miner(miner_id)
    if batch_view:
        print(f"    矿工视角 - 收到 {batch_view['total_challenges']} 个挖矿挑战")
        for c in batch_view['challenges']:
            print(f"      挑战 {c['challenge_id'][:12]}... 类型={c['type']} 难度={c['difficulty']}")
    
    # 6. 提交单结果（兼容原接口）
    print("\n[6] 矿工提交 '挖矿结果'（实际是付费任务结果）...")
    result_hash = "task_result_abc123"
    ok, msg = scheduler.submit_result("task_001", miner_id, result_hash)
    print(f"    {msg}")
    
    # 7. 验证后状态
    print("\n[7] 验证后状态...")
    task = scheduler.get_task("task_001")
    if task is None:
        raise RuntimeError("任务不存在: task_001")
    print(f"    状态: {task.status.value}")
    print(f"    最终结果: {task.final_result}")
    print(f"    矿工收益: {task.miner_payments}")
    
    # 8. 盲引擎统计
    print("\n[8] 盲引擎统计...")
    stats = scheduler.blind_engine.get_stats()
    print(f"    {stats}")
    
    # 9. 板块统计（含盲引擎信息）
    print("\n[9] 板块统计...")
    stats = scheduler.get_sector_stats("RTX3080")
    print(f"    矿机: {stats['miners']}")
    print(f"    任务: {stats['tasks']}")
    if stats.get('blind_engine'):
        print(f"    盲引擎: {stats['blind_engine']}")
    
    print("\n" + "=" * 60)
    print("盲调度测试完成！矿工全程以为自己在挖矿。")
    print("=" * 60)
