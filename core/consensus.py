"""
Consensus 共识模块 - POUW + PoW 混合共识

============================================================
PRODUCTION CONSENSUS ENTRYPOINT / 生产共识唯一入口
============================================================
本模块是 main.py 实际加载并运行的共识引擎，是当前 maincoin
项目的【唯一】生产共识入口。修改本文件需要谨慎评估对链上
出块、奖励、finality、UTXO 状态的影响。

不要在 main.py 或 core/rpc_service.py 中直接导入：
- core.unified_consensus       (实验性，未集成)
- core.dual_layer_consensus    (实验性，未集成)
- core.pouw_chain_v3           (实验性，未集成)

如需切换共识引擎，必须先经过架构评审，并更新
tests/test_production_consensus_entrypoint.py 中的守护断言。
参考：docs/TECHNICAL_REVIEW_AND_CONSENSUS_RECOMMENDATIONS_2026-05-09.md §7

实现功能：
1. POUW 任务执行与客观观测
2. PoW 作为无任务时的保底
3. 动态难度调整（每板块独立）
4. 区块生产（目标 30s）
5. 奖励分配（3% 进入 MAIN 财库）
6. 多板块同步机制（各板块独立难度调整）

区块链参数：
- 目标出块时间: 30 秒
- 最大区块大小: 1 MB
- 最大交易数: 2000 笔
- 难度调整周期: 每 10 个区块
"""

# 生产共识标识：测试与导入守护使用此常量识别唯一生产入口
IS_PRODUCTION_CONSENSUS_ENTRYPOINT = True

import time
import hashlib
import threading
import uuid
import json
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Callable, Tuple, Any
from enum import Enum

# PoUW 执行器
from core.pouw_executor import PoUWExecutor, RealTaskType, RealPoUWResult

# PoUW 区块类型与奖励衰减
from core.pouw_block_types import (
    BlockType as PoUWBlockType,
    BlockTypeSelector,
    RewardDecayRules,
    LivenessConstraints,
)

# PoUW 评分系统
from core.pouw_scoring import ObjectiveMetricsCollector

# 系统资源监控（可选）
try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False


# ============== 区块链核心参数 ==============
class ChainParams:
    """区块链核心参数。"""
    # 出块时间
    TARGET_BLOCK_TIME = 30.0        # 目标 30 秒
    MIN_BLOCK_TIME = 10.0           # 最小 10 秒
    MAX_BLOCK_TIME = 120.0          # 最大 2 分钟
    
    # 区块大小
    MAX_BLOCK_SIZE = 1 * 1024 * 1024  # 1 MB
    MAX_BLOCK_HEADER_SIZE = 1024       # 1 KB
    MAX_TX_PER_BLOCK = 2000            # 最多 2000 笔交易
    MAX_POUW_PER_BLOCK = 50            # 最多 50 个 POUW 证明
    
    # 难度
    DIFFICULTY_ADJUSTMENT_INTERVAL = 10  # 每 10 个区块调整
    MIN_DIFFICULTY = 2
    MAX_DIFFICULTY = 32
    INITIAL_DIFFICULTY = 4
    
    # 奖励 (DR-7: 板块币奖励，不是 MAIN)
    # 注意: 这些是内置板块币奖励初始值，动态板块从 SectorRegistry 获取
    _BUILTIN_SECTOR_REWARDS = {
        "H100": 10.0,       # 10 H100_COIN per block
        "RTX4090": 5.0,     # 5 RTX4090_COIN per block
        "RTX3080": 2.5,     # 2.5 RTX3080_COIN per block
        "CPU": 1.0,         # 1 CPU_COIN per block
        "GENERAL": 1.0,     # 1 GENERAL_COIN per block
    }
    
    @classmethod
    def get_sector_base_rewards(cls) -> dict:
        """动态获取所有活跃板块的基础奖励（内置 + 动态板块）"""
        rewards = dict(cls._BUILTIN_SECTOR_REWARDS)
        try:
            from core.sector_coin import get_sector_registry
            registry = get_sector_registry()
            for sector in registry.get_active_sectors():
                if sector not in rewards:
                    rewards[sector] = registry.get_base_reward(sector)
        except Exception:
            pass
        return rewards

    # 向后兼容：SECTOR_BASE_REWARDS 保留为属性引用
    SECTOR_BASE_REWARDS = _BUILTIN_SECTOR_REWARDS
    BASE_BLOCK_REWARD = 50.0         # 仅用于兼容旧代码
    HALVING_INTERVAL = 10000         # 每 10000 块减半（约 3.5 天 @30s/块）
    TREASURY_RATE = 0.03             # 3% 进入财库
    SECTOR_TO_MAIN_RATE = 0.10       # 板块 10% 流入 MAIN (仅适用于跨板块交易)
    
    # POUW
    MIN_POUW_SCORE = 50              # POUW 出块最低分数
    TASK_POOL_SWITCH_THRESHOLD = 3    # 任务池低于该阈值时可切换 PoW 保底
    MIN_POUW_RATIO = 0.35             # 最近窗口内最低 PoUW 占比
    MAX_CONSECUTIVE_POW_BLOCKS = 6    # 最多连续 PoW 块数
    POUW_CONFIDENCE_THRESHOLD = 12.0  # Σ(confidence*work) 接受阈值
    USEFUL_WORK_REWARD_LAMBDA = 0.05  # 有用工作奖励系数
    POUW_SCORE_WEIGHTS = {
        "time": 0.3,
        "resource": 0.3,
        "quality": 0.4,
    }
    
    # D-15 fix: UTXO 成熟度参数（可配置替代硬编码）
    COINBASE_MATURITY_DEPTH = 100    # Coinbase 成熟需要 100 个确认
    TX_CONFIRMATION_DEPTH = 6        # 普通交易需要 6 个确认


class ConsensusType(Enum):
    """共识类型。"""
    POUW = "POUW"          # 有用工作量证明
    POW = "POW"            # 传统工作量证明（保底）
    HYBRID = "HYBRID"      # 混合
    SBOX_POUW = "SBOX_POUW"  # S-Box PoUW 挖矿（密码学有用工作量）


class BlockStatus(Enum):
    """区块状态。"""
    PENDING = "PENDING"
    VALIDATED = "VALIDATED"
    FINALIZED = "FINALIZED"
    ORPHANED = "ORPHANED"


@dataclass
class Block:
    """区块。"""
    # 基本信息
    height: int = 0
    hash: str = ""
    prev_hash: str = ""
    merkle_root: str = ""
    
    # 时间
    timestamp: float = field(default_factory=time.time)
    
    # 共识
    consensus_type: ConsensusType = ConsensusType.POUW
    difficulty: int = 4  # 前导零数量
    nonce: int = 0
    
    # 矿工
    miner_id: str = ""
    miner_address: str = ""
    
    # 内容
    transactions: List[Dict] = field(default_factory=list)
    pouw_proofs: List[Dict] = field(default_factory=list)
    
    # 奖励
    block_reward: float = 50.0
    total_fees: float = 0.0
    
    # POUW 区块类型
    block_type: str = "task_block"  # task_block / idle_block / validation_block
    
    # 元数据
    sector: str = "MAIN"
    version: int = 1
    extra_data: str = ""
    status: BlockStatus = BlockStatus.PENDING
    
    # S-Box PoUW 字段
    sbox_hex: str = ""                # 挖矿产出的 S-Box (hex 编码)
    sbox_score: float = 0.0           # S-Box 综合安全评分
    sbox_nonlinearity: int = 0        # 非线性度
    sbox_diff_uniformity: int = 0     # 差分均匀性
    sbox_avalanche: float = 0.0       # 雪崩效应
    sbox_score_weights: Dict = field(default_factory=dict)  # 评分权重
    sbox_score_threshold: float = 0.0 # 当前评分阈值
    sbox_selected_sector: str = ""    # 被 VRF 选中公布的板块
    sbox_all_sectors: List[str] = field(default_factory=list)  # 所有参与板块
    
    def compute_hash(self) -> str:
        """计算区块哈希（历史路径，链上 hash 仍以本方法为准）。"""
        # D-16 fix: 包含 consensus_type 防止共识类型篡改
        # D-17 fix: 包含 block_reward/total_fees/miner_address/sector/block_type 防止经济字段篡改
        header = (f"{self.height}{self.prev_hash}{self.merkle_root}{self.timestamp}"
                  f"{self.difficulty}{self.nonce}{self.miner_id}{self.consensus_type.value}"
                  f"{self.block_reward}{self.total_fees}{self.sector}"
                  f"{self.miner_address}{self.block_type}")
        # S-Box PoUW: 包含 S-Box 数据防止篡改
        if self.sbox_hex:
            header += f"{self.sbox_hex}{self.sbox_score:.6f}"
        return hashlib.sha256(header.encode()).hexdigest()

    def canonical_hash(self) -> str:
        """canonical 区块头哈希（新协议路径，可选）。

        与 ``compute_hash`` 不兼容，不替换链上 hash。
        作为新模块计算 task_root/state_root/header digest 时的稳定输入。
        参考: docs/TECHNICAL_REVIEW_AND_CONSENSUS_RECOMMENDATIONS_2026-05-09.md §8.4
        """
        # 局部 import 避免文件被早期导入时的潜在循环依赖
        from core.serialization import canonical_block_hash
        return canonical_block_hash(self)

    def compute_merkle_root(self) -> str:
        """M-2 fix: 计算标准的两两配对默克尔根。
        
        使用标准 Merkle Tree 构造：
        1. 每个交易先独立 SHA256 得到叶子哈希
        2. 相邻叶子两两拼接后 SHA256
        3. 奇数节点复制末尾对齐
        4. 重复直到剩一个根哈希
        """
        if not self.transactions:
            return hashlib.sha256(b"empty").hexdigest()
        
        # 叶子层：每个交易取 SHA256
        import json
        leaves = []
        for tx in self.transactions:
            if isinstance(tx, dict):
                tx_bytes = json.dumps(tx, sort_keys=True, separators=(',', ':')).encode()
            else:
                tx_bytes = str(tx).encode()
            leaves.append(hashlib.sha256(tx_bytes).hexdigest())
        
        # 两两配对构造 Merkle Tree
        level = leaves
        while len(level) > 1:
            next_level = []
            for i in range(0, len(level), 2):
                left = level[i]
                # 奇数节点：复制末尾
                right = level[i + 1] if i + 1 < len(level) else level[i]
                combined = hashlib.sha256((left + right).encode()).hexdigest()
                next_level.append(combined)
            level = next_level
        
        return level[0]
    
    def is_valid_pow(self) -> bool:
        """验证 PoW（含哈希重算校验）。
        Verify PoW: recompute hash and check it matches + meets difficulty target.
        """
        # 安全加固：必须重算哈希并与声明的 hash 对比
        # Security: recompute hash and compare against claimed hash
        recomputed = self.compute_hash()
        if self.hash != recomputed:
            return False
        target = "0" * self.difficulty
        return self.hash.startswith(target)
    
    def get_size(self) -> int:
        """计算区块大小（字节）。"""
        # 区块头固定部分
        header_size = 200  # 基础头部约 200 字节
        
        # 交易大小（每笔约 250 字节）
        tx_size = len(json.dumps(self.transactions)) if self.transactions else 0
        
        # POUW 证明大小
        pouw_size = len(json.dumps(self.pouw_proofs)) if self.pouw_proofs else 0
        
        # 额外数据
        extra_size = len(self.extra_data.encode())
        
        return header_size + tx_size + pouw_size + extra_size
    
    def is_within_size_limit(self) -> bool:
        """检查区块是否在大小限制内。"""
        return self.get_size() <= ChainParams.MAX_BLOCK_SIZE
    
    def to_dict(self) -> Dict:
        d = {
            "height": self.height,
            "hash": self.hash,
            "prev_hash": self.prev_hash[:16] + "..." if self.prev_hash else "",
            "timestamp": self.timestamp,
            "consensus": self.consensus_type.value,
            "difficulty": self.difficulty,
            "miner_id": self.miner_id,
            "tx_count": len(self.transactions),
            "pouw_count": len(self.pouw_proofs),
            "reward": self.block_reward,
            "fees": self.total_fees,
            "block_type": self.block_type,
            "sector": self.sector,
        }
        # S-Box PoUW 信息
        if self.sbox_hex:
            d["sbox_score"] = round(self.sbox_score, 6)
            d["sbox_nonlinearity"] = self.sbox_nonlinearity
            d["sbox_diff_uniformity"] = self.sbox_diff_uniformity
            d["sbox_avalanche"] = round(self.sbox_avalanche, 4)
            d["sbox_selected_sector"] = self.sbox_selected_sector
        return d


@dataclass
class POUWProof:
    """POUW 工作量证明。"""
    proof_id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    task_id: str = ""
    miner_id: str = ""
    
    # 执行信息
    compute_hash: str = ""  # 计算结果哈希
    execution_time: float = 0.0
    gpu_cycles: int = 0
    memory_used: int = 0  # MB
    
    # 客观观测（由系统收集）
    cpu_usage: float = 0.0  # 百分比
    gpu_usage: float = 0.0
    power_consumption: float = 0.0  # Watts
    
    # 验证
    verified: bool = False
    verifier_id: str = ""
    verification_time: float = 0.0
    
    # 质量评分（0-100）
    quality_score: float = 0.0
    
    timestamp: float = field(default_factory=time.time)
    
    def compute_work_score(self) -> float:
        """计算工作量分数。
        
        D-06 fix: 基于可验证的客观指标，而非自报告的 cpu/gpu 使用率。
        - execution_time: 执行时间（可观测）
        - compute_hash: 计算结果哈希（可重现验证）
        - verified: 是否通过验证（可核实）
        - quality_score: 任务质量分（由验证器评定）
        """
        time_score = min(100, self.execution_time * 10)
        # D-06: 用计算结果哈希存在性代替自报告资源使用率
        hash_score = 50.0 if self.compute_hash else 0.0
        verified_bonus = 30.0 if self.verified else 0.0
        quality_factor = max(0.1, min(1.0, self.quality_score / 100.0))
        
        # 综合分数：基于可验证的指标
        work_score = (
            time_score * 0.4 +
            hash_score * 0.3 +
            verified_bonus * 0.3
        ) * quality_factor
        
        return work_score


class DifficultyAdjuster:
    """动态难度调整器。"""
    
    def __init__(
        self,
        target_block_time: float = 30.0,  # 目标 30 秒
        adjustment_interval: int = 10,     # 每 10 个区块调整
        min_difficulty: int = 2,
        max_difficulty: int = 32,          # 与 ChainParams.MAX_DIFFICULTY 保持一致
    ):
        self.target_block_time = target_block_time
        self.adjustment_interval = adjustment_interval
        self.min_difficulty = min_difficulty
        self.max_difficulty = max_difficulty
        
        # 历史记录
        self.block_times: List[float] = []
    
    def record_block(self, block_time: float):
        """记录区块时间。"""
        self.block_times.append(block_time)
        
        # 只保留最近的
        if len(self.block_times) > self.adjustment_interval * 2:
            self.block_times = self.block_times[-self.adjustment_interval:]
    
    def should_adjust(self, block_height: int) -> bool:
        """是否应该调整。"""
        return block_height % self.adjustment_interval == 0
    
    def calculate_new_difficulty(self, current_difficulty: int) -> int:
        """计算新难度。"""
        if len(self.block_times) < self.adjustment_interval:
            return current_difficulty
        
        # 计算平均出块时间
        recent_times = self.block_times[-self.adjustment_interval:]
        avg_time = sum(recent_times) / len(recent_times)
        
        # 调整因子
        ratio = self.target_block_time / avg_time if avg_time > 0 else 1.0
        
        # 限制调整幅度（最多 ±1 级难度）
        if ratio > 1.1:
            new_difficulty = current_difficulty + 1
        elif ratio < 0.9:
            new_difficulty = current_difficulty - 1
        else:
            new_difficulty = current_difficulty
        
        # 边界检查
        new_difficulty = max(self.min_difficulty, min(self.max_difficulty, new_difficulty))
        
        return new_difficulty
    
    def get_stats(self) -> Dict:
        """获取统计。"""
        if not self.block_times:
            return {"avg_time": 0, "block_count": 0}
        
        return {
            "avg_time": sum(self.block_times) / len(self.block_times),
            "min_time": min(self.block_times),
            "max_time": max(self.block_times),
            "block_count": len(self.block_times),
        }


class WorkDifficultyAdjuster:
    """工作量难度调节器（双通道中的 work 通道）。"""

    def __init__(
        self,
        target_block_time: float = 30.0,
        adjustment_interval: int = 10,
        min_threshold: float = 20.0,
        max_threshold: float = 180.0,
        max_step: float = 8.0,
    ):
        self.target_block_time = target_block_time
        self.adjustment_interval = adjustment_interval
        self.min_threshold = min_threshold
        self.max_threshold = max_threshold
        self.max_step = max_step

        self.observations: deque = deque(maxlen=240)

    def record_observation(
        self,
        mining_time: float,
        avg_quality: float,
        has_real_orders: bool,
        hash_difficulty: int,
    ):
        self.observations.append({
            "mining_time": float(max(mining_time, 0.0001)),
            "avg_quality": float(max(avg_quality, 0.0)),
            "has_real_orders": bool(has_real_orders),
            "hash_difficulty": int(max(hash_difficulty, 1)),
        })

    def should_adjust(self, block_height: int) -> bool:
        return block_height % self.adjustment_interval == 0 and len(self.observations) >= self.adjustment_interval

    def calculate_new_threshold(self, current_threshold: float) -> float:
        if len(self.observations) < self.adjustment_interval:
            return float(current_threshold)

        recent = list(self.observations)[-self.adjustment_interval:]
        avg_time = sum(x["mining_time"] for x in recent) / len(recent)
        avg_quality = sum(x["avg_quality"] for x in recent) / len(recent)
        avg_hash_diff = sum(x["hash_difficulty"] for x in recent) / len(recent)
        real_ratio = sum(1 for x in recent if x["has_real_orders"]) / len(recent)

        # 目标：时间过快/质量过高/算力高时提高工作量门槛；反之降低。
        time_factor = (self.target_block_time / avg_time) if avg_time > 0 else 1.0
        quality_factor = 1.0 + max(-0.2, min(0.2, (avg_quality - 70.0) / 250.0))
        hash_factor = 1.0 + max(-0.15, min(0.15, (avg_hash_diff - 4.0) / 32.0))
        demand_factor = 0.95 + 0.15 * real_ratio  # 真实订单占比高时略增门槛

        desired = current_threshold * time_factor * quality_factor * hash_factor * demand_factor
        desired = max(self.min_threshold, min(self.max_threshold, desired))

        # 限速：每轮最多变化 max_step，抑制震荡。
        delta = desired - current_threshold
        if delta > self.max_step:
            desired = current_threshold + self.max_step
        elif delta < -self.max_step:
            desired = current_threshold - self.max_step

        return round(max(self.min_threshold, min(self.max_threshold, desired)), 2)


class RewardCalculator:
    """奖励计算器。
    
    使用指数递减模型：每过 halving_interval 个区块奖励减半，
    最终递减至零（不设最小奖励）。
    """
    
    def __init__(
        self,
        base_reward: float = 50.0,
        halving_interval: int = 10000,   # 每 10000 块减半（约 3.5 天 @30s/块）
        treasury_rate: float = 0.03,      # 3% 进入财库
    ):
        self.base_reward = base_reward
        self.halving_interval = halving_interval
        self.treasury_rate = treasury_rate
    
    def get_block_reward(self, height: int) -> float:
        """获取区块奖励（递减至零）。"""
        if self.halving_interval <= 0:
            return self.base_reward
        halvings = height // self.halving_interval
        reward = self.base_reward / (2 ** halvings)
        # 奖励低于 0.0001 时归零
        if reward < 0.0001:
            return 0.0
        return reward
    
    def calculate_distribution(
        self,
        height: int,
        miner_pouw_score: float,
        total_pouw_score: float,
        fees: float = 0.0,
    ) -> Dict[str, float]:
        """计算奖励分配。"""
        block_reward = self.get_block_reward(height)
        total_reward = block_reward + fees
        
        # 财库部分
        treasury_amount = total_reward * self.treasury_rate
        miner_pool = total_reward - treasury_amount
        
        # POUW 加权
        if total_pouw_score > 0:
            pouw_bonus = min(miner_pouw_score / total_pouw_score * 0.2, 0.2)  # 最多 +20%
        else:
            pouw_bonus = 0
        
        miner_share = min(miner_pool * (1 + pouw_bonus), total_reward - treasury_amount)
        
        return {
            "total": total_reward,
            "block_reward": block_reward,
            "fees": fees,
            "treasury": treasury_amount,
            "miner": miner_share,
            "pouw_bonus": pouw_bonus * 100,  # 百分比
        }


class ConsensusEngine:
    """共识引擎。
    
    管理区块生产、验证、共识切换。
    支持区块链持久化到数据库。
    """
    
    def __init__(
        self,
        node_id: str,
        sector: str = "MAIN",
        log_fn: Callable = print,
        db_path: str = None,
    ):
        self.node_id = node_id
        self.sector = sector
        self.log = log_fn
        self.db_path = db_path or f"data/chain_{sector.lower()}.db"
        
        # 状态
        self.current_difficulty = 4
        self.current_consensus = ConsensusType.POUW
        self.is_mining = False
        self.stop_mining = False
        
        # 区块链（仅缓存最近区块，其余从 DB 查询）
        self.chain: List[Block] = []
        self._chain_height: int = -1
        self._max_cache_size: int = 200
        self.pending_blocks: Dict[str, Block] = {}
        
        # UTXO 存储（外部注入，用于交易验证）
        self.utxo_store = None
        # 板块币账本（外部注入，用于 reorg 回滚）
        self.sector_ledger = None
        
        # 组件
        self.difficulty_adjuster = DifficultyAdjuster()
        self.work_difficulty_adjuster = WorkDifficultyAdjuster(
            target_block_time=ChainParams.TARGET_BLOCK_TIME,
            adjustment_interval=ChainParams.DIFFICULTY_ADJUSTMENT_INTERVAL,
            min_threshold=20.0,
            max_threshold=180.0,
            max_step=8.0,
        )
        self.current_work_threshold = 50.0
        self._idle_penalty_window = 6
        self._consecutive_benchmark_blocks = 0
        # 使用板块对应的基础奖励（而非默认 50）
        sector_base = ChainParams.get_sector_base_rewards().get(sector, 1.0)
        self.reward_calculator = RewardCalculator(base_reward=sector_base)
        self.pouw_executor = PoUWExecutor(min_score_threshold=0.5)
        
        # POUW 任务轮转表（循环使用不同任务类型）
        self._pouw_task_types = [
            RealTaskType.MATRIX_MULTIPLY,
            RealTaskType.LINEAR_REGRESSION,
            RealTaskType.GRADIENT_DESCENT,
            RealTaskType.HASH_SEARCH,
        ]
        self._pouw_task_index = 0
        
        # 待处理
        self.pending_transactions: List[Dict] = []
        self.pending_pouw: List[POUWProof] = []
        
        # 地址 nonce 跟踪（address -> last confirmed nonce）
        self._address_nonces: Dict[str, int] = {}
        
        # 评分采集器
        self.metrics_collector = ObjectiveMetricsCollector()
        
        # 区块类型跟踪
        self._consecutive_idle = 0
        self._consecutive_validation = 0
        self._last_block_time = time.time()
        
        # 统计
        self.total_blocks_mined = 0
        self.total_pouw_proofs = 0
        
        # 外部区块存储（StorageManager 的 BlockStore，可选注入）
        self.block_store = None
        
        # S-Box PoUW 挖矿引擎
        self._sbox_miner = None  # 延迟初始化（需要知道活跃板块列表）
        self._sbox_mining_enabled = True  # 默认启用 S-Box 挖矿
        self.consensus_mode = "sbox_primary"  # sbox_primary | mixed | sbox_only | pouw_only
        self.consensus_sbox_ratio = 0.5  # mixed 模式下 SBOX_POUW 占比
        self.consensus_pouw_support_ratio = 0.1  # sbox_primary 模式下 POUW 辅助占比
        self._consensus_round = 0
        self._recent_consensus_selected = deque(maxlen=200)
        self._recent_consensus_mined = deque(maxlen=200)
        self._consecutive_pow_blocks = 0
        self._min_pouw_ratio = ChainParams.MIN_POUW_RATIO
        self._max_consecutive_pow_blocks = ChainParams.MAX_CONSECUTIVE_POW_BLOCKS
        self._sbox_quiz_interval_seconds = 1800  # 每 30 分钟更新一次评分题
        self._sbox_quiz_window_id = -1
        self._sbox_quiz_payload: Dict[str, Any] = {}

        # 机制策略（版本化 + 灰度/回滚）
        self._mechanism_strategy = {
            "version": "v2.0",
            "rollout": "ga",
            "max_ratio_step": 1.0,
            "updated_at": time.time(),
        }
        self._strategy_history: List[Dict[str, Any]] = []
        
        # 线程安全锁 — 保护共享状态
        self._lock = threading.Lock()
        
        # 初始化存储并加载区块链
        self._init_storage()
        self._load_or_create_genesis()
    
    def _init_storage(self):
        """初始化 SQLite 存储。"""
        import sqlite3
        from pathlib import Path
        
        # 确保目录存在
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        
        self._db_conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._db_conn.row_factory = sqlite3.Row
        self._db_conn.execute("PRAGMA journal_mode=WAL")
        self._db_lock = threading.Lock()
        
        # 创建区块表
        self._db_conn.execute("""
            CREATE TABLE IF NOT EXISTS blocks (
                height INTEGER PRIMARY KEY,
                hash TEXT UNIQUE NOT NULL,
                prev_hash TEXT NOT NULL,
                timestamp REAL NOT NULL,
                block_data TEXT NOT NULL,
                sector TEXT NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        self._db_conn.execute("CREATE INDEX IF NOT EXISTS idx_block_hash ON blocks(hash)")
        self._db_conn.execute("CREATE INDEX IF NOT EXISTS idx_block_sector ON blocks(sector)")
        
        # 迁移：添加 status 列（如果不存在）
        try:
            self._db_conn.execute("SELECT status FROM blocks LIMIT 1")
        except Exception:
            try:
                self._db_conn.execute("ALTER TABLE blocks ADD COLUMN status TEXT DEFAULT 'FINALIZED'")
            except Exception:
                pass
        
        self._db_conn.commit()
    
    def _load_or_create_genesis(self):
        """从数据库加载区块链（仅缓存最近区块），或创建创世区块。"""
        # 查询链高度
        row = self._db_conn.execute(
            "SELECT MAX(height) as max_h, COUNT(*) as cnt FROM blocks WHERE sector = ?",
            (self.sector,)
        ).fetchone()
        
        total_blocks = row['cnt'] if row and row['cnt'] else 0
        
        if total_blocks > 0:
            self._chain_height = row['max_h']
            
            # 只加载最近 N 个区块到内存缓存
            cache_start = max(0, self._chain_height - self._max_cache_size + 1)
            cursor = self._db_conn.execute(
                "SELECT block_data FROM blocks WHERE sector = ? AND height >= ? ORDER BY height ASC",
                (self.sector, cache_start)
            )
            for r in cursor.fetchall():
                block_dict = json.loads(r['block_data'])
                block = self._dict_to_block(block_dict)
                self.chain.append(block)
            
            self.log(f"从数据库加载链高度 #{self._chain_height}，缓存最近 {len(self.chain)} 个区块")
        else:
            # 创建创世区块
            self._create_genesis()
            self._chain_height = 0
            # 保存到数据库
            self._save_block(self.chain[0])
    
    def _dict_to_block(self, data: Dict) -> Block:
        """从字典恢复区块对象。"""
        # 处理旧版本数据中可能不存在的 SBOX_POUW 类型
        consensus_str = data.get('consensus_type', 'POW')
        try:
            consensus_type = ConsensusType(consensus_str)
        except ValueError:
            consensus_type = ConsensusType.POW
        
        block = Block(
            height=data.get('height', 0),
            hash=data.get('hash', ''),
            prev_hash=data.get('prev_hash', ''),
            merkle_root=data.get('merkle_root', ''),
            timestamp=data.get('timestamp', 0),
            consensus_type=consensus_type,
            difficulty=data.get('difficulty', 4),
            nonce=data.get('nonce', 0),
            miner_id=data.get('miner_id', ''),
            miner_address=data.get('miner_address', ''),
            transactions=data.get('transactions', []),
            pouw_proofs=data.get('pouw_proofs', []),
            block_reward=data.get('block_reward', 0),
            total_fees=data.get('total_fees', 0),
            block_type=data.get('block_type', 'task_block'),
            sector=data.get('sector', 'MAIN'),
            version=data.get('version', 1),
            extra_data=data.get('extra_data', ''),
            status=BlockStatus(data.get('status', 'FINALIZED')),
            # S-Box PoUW 字段
            sbox_hex=data.get('sbox_hex', ''),
            sbox_score=data.get('sbox_score', 0.0),
            sbox_nonlinearity=data.get('sbox_nonlinearity', 0),
            sbox_diff_uniformity=data.get('sbox_diff_uniformity', 0),
            sbox_avalanche=data.get('sbox_avalanche', 0.0),
            sbox_score_weights=data.get('sbox_score_weights', {}),
            sbox_score_threshold=data.get('sbox_score_threshold', 0.0),
            sbox_selected_sector=data.get('sbox_selected_sector', ''),
            sbox_all_sectors=data.get('sbox_all_sectors', []),
        )
        # 紧凑模式恢复: 如果只有 sbox_hash_ref 而无 sbox_hex, 从 Library 查出全量
        if not block.sbox_hex and data.get('sbox_hash_ref'):
            try:
                from core.sbox_engine import get_sbox_library
                lib = get_sbox_library()
                entry = lib.get(data['sbox_hash_ref'])
                if entry and entry.sbox:
                    block.sbox_hex = bytes(entry.sbox).hex()
            except Exception:
                pass
        return block
    
    def _block_to_dict(self, block: Block, compact: bool = False) -> Dict:
        """将区块转为字典（用于序列化）。

        Args:
            compact: 紧凑模式。True 时 S-Box 仅存 hash 引用 (节省 ~450 bytes/block)，
                     全量 S-Box 由 SBoxLibrary 管理。
        """
        d = {
            'height': block.height,
            'hash': block.hash,
            'prev_hash': block.prev_hash,
            'merkle_root': block.merkle_root,
            'timestamp': block.timestamp,
            'consensus_type': block.consensus_type.value,
            'difficulty': block.difficulty,
            'nonce': block.nonce,
            'miner_id': block.miner_id,
            'miner_address': block.miner_address,
            'transactions': block.transactions,
            'pouw_proofs': [p.__dict__ if hasattr(p, '__dict__') else p for p in block.pouw_proofs],
            'block_reward': block.block_reward,
            'total_fees': block.total_fees,
            'block_type': block.block_type,
            'sector': block.sector,
            'version': block.version,
            'extra_data': block.extra_data,
            'status': block.status.value,
        }
        # S-Box PoUW 字段
        if block.sbox_hex:
            if compact:
                # 紧凑模式: 仅存 SHA-256 哈希引用 (64 chars vs 512 chars)
                sbox_bytes = bytes.fromhex(block.sbox_hex)
                d['sbox_hash_ref'] = hashlib.sha256(sbox_bytes).hexdigest()
            else:
                d['sbox_hex'] = block.sbox_hex
            d['sbox_score'] = block.sbox_score
            d['sbox_nonlinearity'] = block.sbox_nonlinearity
            d['sbox_diff_uniformity'] = block.sbox_diff_uniformity
            d['sbox_avalanche'] = block.sbox_avalanche
            d['sbox_score_weights'] = block.sbox_score_weights
            d['sbox_score_threshold'] = block.sbox_score_threshold
            d['sbox_selected_sector'] = block.sbox_selected_sector
            d['sbox_all_sectors'] = block.sbox_all_sectors
        return d
    
    def _save_block(self, block: Block) -> bool:
        """保存区块到数据库。"""
        try:
            block_dict = self._block_to_dict(block)
            block_data = json.dumps(block_dict)
            with self._db_lock:
                self._db_conn.execute("""
                    INSERT OR REPLACE INTO blocks (height, hash, prev_hash, timestamp, block_data, sector, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (block.height, block.hash, block.prev_hash, block.timestamp, block_data, block.sector, time.time()))
                self._db_conn.commit()
            
            # 同步写入外部 BlockStore（StorageManager），保持 chain.db 一致
            if self.block_store:
                try:
                    self.block_store.save_block(block_dict)
                except Exception as e:
                    self.log(f"外部 BlockStore 写入失败 (height={block.height}): {e}")
            
            return True
        except Exception as e:
            self.log(f"保存区块失败: {e}")
            return False
    
    def get_chain_height(self) -> int:
        """获取当前链高度。D-19 fix: 线程安全。"""
        with self._lock:
            if self._chain_height >= 0:
                return self._chain_height
            if self.chain:
                return self.chain[-1].height
            return 0
    
    def _create_genesis(self):
        """创建创世区块（从 genesis.mainnet.json 加载确定性创世块）。
        
        D-11 fix: 如果链中已有区块则拒绝重新创建创世块。
        """
        # D-11: 防止已有链时重新创建创世
        if self.chain:
            self.log("⚠️ 链已有区块，跳过创世块创建")
            return
        
        # 检查数据库中是否已有创世块
        row = self._db_conn.execute(
            "SELECT COUNT(*) as cnt FROM blocks WHERE sector = ? AND height = 0",
            (self.sector,)
        ).fetchone()
        if row and row['cnt'] > 0:
            self.log("⚠️ 数据库已有创世块，跳过创世块创建")
            return
        
        import os
        genesis_file = None
        network = getattr(self, 'network_type', 'mainnet')
        
        # 按网络类型查找创世文件
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        candidates = [
            os.path.join(base_dir, f"genesis.{network}.json"),
            os.path.join(base_dir, "genesis.mainnet.json"),
            os.path.join(base_dir, "genesis.testnet.json"),
        ]
        for path in candidates:
            if os.path.exists(path):
                genesis_file = path
                break
        
        if genesis_file:
            try:
                with open(genesis_file, 'r', encoding='utf-8') as f:
                    gdata = json.load(f)
                genesis = Block(
                    height=0,
                    prev_hash=gdata.get("prev_hash", "0" * 64),
                    timestamp=gdata.get("timestamp", 1740182400.0),
                    miner_id=gdata.get("miner_id", "genesis"),
                    miner_address=gdata.get("miner_address", ""),
                    consensus_type=ConsensusType.POW,
                    difficulty=gdata.get("difficulty", 1),
                    nonce=gdata.get("nonce", 0),
                    block_reward=gdata.get("block_reward", 50.0),
                    block_type=gdata.get("block_type", "task_block"),
                    sector=gdata.get("sector", self.sector),
                    extra_data=gdata.get("extra_data", "POUW Genesis"),
                )
                genesis.merkle_root = genesis.compute_merkle_root()
                genesis.hash = genesis.compute_hash()
                genesis.status = BlockStatus.FINALIZED
                self.chain.append(genesis)
                self.log(f"⛓️ 创世区块(from {os.path.basename(genesis_file)}): {genesis.hash[:16]}...")
                return
            except Exception as e:
                self.log(f"⚠️ 创世文件加载失败: {e}，使用内置创世")
        
        # 回退：内置确定性创世（固定时间戳）
        genesis = Block(
            height=0,
            prev_hash="0" * 64,
            timestamp=1740182400.0,  # 2025-02-22 00:00:00 UTC 固定
            miner_id="genesis",
            consensus_type=ConsensusType.POW,
            difficulty=1,
            sector=self.sector,
            extra_data="POUW Multi-Sector Chain Genesis",
        )
        genesis.merkle_root = genesis.compute_merkle_root()
        genesis.hash = genesis.compute_hash()
        genesis.status = BlockStatus.FINALIZED
        
        self.chain.append(genesis)
        self.log(f"⛓️ 创世区块: {genesis.hash[:16]}...")
    
    def get_latest_block(self) -> Block:
        """获取最新区块。D-19 fix: 线程安全访问。"""
        with self._lock:
            return self.chain[-1] if self.chain else None
    
    def add_transaction(self, tx: Dict) -> bool:
        """添加待处理交易（含基本验证 + ECDSA 签名验证）。
        
        D-03 fix: 非 coinbase 交易必须提供有效的 ECDSA 签名。
        
        Returns:
            True 如果交易被接受, False 如果被拒绝
        """
        tx_id = tx.get('tx_id', tx.get('txid', ''))
        from_addr = tx.get('from', tx.get('from_address', ''))
        to_addr = tx.get('to', tx.get('to_address', ''))
        amount = tx.get('amount', 0)
        tx_type = tx.get('tx_type', 'transfer')
        
        # 基本字段检查
        if not tx_id or not from_addr or not to_addr:
            return False
        if not isinstance(amount, (int, float)) or amount <= 0:
            return False
        
        # D-03: ECDSA 签名验证（coinbase 交易豁免）
        if tx_type != 'coinbase':
            signature = tx.get('signature', '')
            public_key = tx.get('public_key', '')
            
            if not signature or not public_key:
                self.log(f"❌ 交易 {tx_id[:12]} 被拒绝：缺少签名或公钥")
                return False
            
            try:
                from core.crypto import ECDSASigner
                # 统一签名格式：与 rpc_service / main_transfer / double_witness 一致
                fee = tx.get('fee', 0)
                sig_data = f"{from_addr}{to_addr}{amount}{fee}".encode()
                
                sig_bytes = bytes.fromhex(signature)
                pub_bytes = bytes.fromhex(public_key)
                
                if not ECDSASigner.verify(pub_bytes, sig_data, sig_bytes):
                    self.log(f"❌ 交易 {tx_id[:12]} 签名验证失败")
                    return False
                
                # 验证公钥是否匹配发送地址
                derived_addr = ECDSASigner.public_key_to_address(pub_bytes)
                if derived_addr != from_addr:
                    # 兼容简化地址格式：SHA256 截断
                    alt_addr = hashlib.sha256(pub_bytes).hexdigest()[:40]
                    if alt_addr != from_addr:
                        self.log(f"❌ 交易 {tx_id[:12]} 公钥与发送地址不匹配")
                        return False
            except ImportError:
                # ecdsa 是必需依赖，缺失时必须拒绝交易
                self.log(f"❌ 交易 {tx_id[:12]} 拒绝：缺少 ecdsa 库（pip install ecdsa）")
                return False
            except (ValueError, Exception) as e:
                self.log(f"❌ 交易 {tx_id[:12]} 签名验证异常: {e}")
                return False
        
        with self._lock:
            # 去重检查
            for existing in self.pending_transactions:
                if existing.get('tx_id', existing.get('txid', '')) == tx_id:
                    return False  # 已存在
            
            # Nonce 递增检查（防止重放攻击）
            if tx_type != 'coinbase':
                tx_nonce = tx.get('nonce', None)
                if tx_nonce is not None:
                    tx_nonce = int(tx_nonce)
                    last_nonce = self._address_nonces.get(from_addr, -1)
                    # 也检查 pending 池中同地址的最大 nonce
                    for pending_tx in self.pending_transactions:
                        p_from = pending_tx.get('from', pending_tx.get('from_address', ''))
                        if p_from == from_addr:
                            p_nonce = pending_tx.get('nonce', -1)
                            if isinstance(p_nonce, int) and p_nonce > last_nonce:
                                last_nonce = p_nonce
                    if tx_nonce <= last_nonce:
                        self.log(f"❌ 交易 {tx_id[:12]} nonce 过低: got {tx_nonce}, expected > {last_nonce}")
                        return False
            
            # 限制 pending 池大小（防止内存耗尽）
            MAX_PENDING = 10000
            if len(self.pending_transactions) >= MAX_PENDING:
                return False
            
            self.pending_transactions.append(tx)
            return True
    
    def _update_nonces_from_block(self, block: Block):
        """从已确认区块更新地址 nonce 跟踪（需持有 _lock）。"""
        for tx in (block.transactions or []):
            tx_type = tx.get('tx_type', 'transfer')
            if tx_type == 'coinbase':
                continue
            from_addr = tx.get('from', tx.get('from_address', ''))
            tx_nonce = tx.get('nonce', None)
            if from_addr and tx_nonce is not None:
                tx_nonce = int(tx_nonce)
                current = self._address_nonces.get(from_addr, -1)
                if tx_nonce > current:
                    self._address_nonces[from_addr] = tx_nonce

    def get_nonce(self, address: str) -> int:
        """获取地址的下一个可用 nonce。
        
        返回值 = 已确认的最大 nonce + 1（若地址无历史交易则为 0）。
        """
        with self._lock:
            last = self._address_nonces.get(address, -1)
            # 也考虑 pending 池中的 nonce
            for tx in self.pending_transactions:
                from_addr = tx.get('from', tx.get('from_address', ''))
                if from_addr == address:
                    n = tx.get('nonce', -1)
                    if isinstance(n, int) and n > last:
                        last = n
            return last + 1

    def add_pouw_proof(self, proof: POUWProof):
        """添加 POUW 证明。"""
        # 仅接受通过验证且带有计算证明的任务，避免低质量/伪造证明进入队列。
        if not proof.verified or not proof.compute_hash:
            return
        if proof.execution_time < 0:
            return
        with self._lock:
            self.pending_pouw.append(proof)
            self.total_pouw_proofs += 1
    
    def has_pouw_tasks(self) -> bool:
        """是否有 POUW 任务。"""
        with self._lock:
            return len(self.pending_pouw) > 0
    
    def select_consensus(self) -> ConsensusType:
        """选择共识类型。
        
        支持四种策略：
        - sbox_primary: S-Box 主路径，按辅助比例注入少量 POUW
        - sbox_only: 优先并固定使用 SBOX_POUW（不可用则回退 POUW）
        - pouw_only: 固定使用 POUW（异常时回退 PoW）
        - mixed: 按配置比例在 SBOX_POUW/POUW 之间混用
        """
        mode = (self.consensus_mode or "mixed").lower()
        if mode not in {"sbox_primary", "mixed", "sbox_only", "pouw_only"}:
            mode = "mixed"

        # 保证 POUW 路径有任务可执行
        if not self.has_pouw_tasks():
            self._auto_generate_pouw()
        has_pouw = self.has_pouw_tasks()
        task_pool_size = self._current_task_pool_size()
        pow_fallback_allowed = task_pool_size < ChainParams.TASK_POOL_SWITCH_THRESHOLD

        sbox_available = False
        if self._sbox_mining_enabled:
            miner = self._get_sbox_miner()
            sbox_available = miner is not None

        # 守护策略：最近窗口 PoUW 占比过低或连续 PoW 过多时，强制回到有用工作路径。
        if self._should_force_pouw_guardrail(has_pouw):
            if sbox_available:
                selected = ConsensusType.SBOX_POUW
            else:
                selected = ConsensusType.POUW
            self._record_selected_consensus(selected)
            return selected

        if mode == "pouw_only":
            if has_pouw:
                selected = ConsensusType.POUW
                self._record_selected_consensus(selected)
                return selected
            if not pow_fallback_allowed:
                self._auto_generate_pouw(count=6)
                if self.has_pouw_tasks():
                    selected = ConsensusType.POUW
                    self._record_selected_consensus(selected)
                    return selected
            self.log("⚠️ POUW 任务不足，回退 PoW 保活")
            selected = ConsensusType.POW
            self._record_selected_consensus(selected)
            return selected

        if mode == "sbox_only":
            if sbox_available:
                selected = ConsensusType.SBOX_POUW
                self._record_selected_consensus(selected)
                return selected
            if has_pouw:
                selected = ConsensusType.POUW
                self._record_selected_consensus(selected)
                return selected
            if not pow_fallback_allowed:
                self._auto_generate_pouw(count=4)
                if self.has_pouw_tasks():
                    selected = ConsensusType.POUW
                    self._record_selected_consensus(selected)
                    return selected
            self.log("⚠️ S-Box/POUW 均不可用，回退 PoW 保活")
            selected = ConsensusType.POW
            self._record_selected_consensus(selected)
            return selected

        if mode == "sbox_primary":
            if sbox_available and has_pouw:
                latest = self.get_latest_block()
                seed = f"{latest.hash if latest else 'genesis'}:{self.node_id}:{self._consensus_round}".encode()
                roll = int(hashlib.sha256(seed).hexdigest()[:8], 16) / 0xFFFFFFFF
                self._consensus_round += 1
                if roll < self.consensus_pouw_support_ratio:
                    selected = ConsensusType.POUW
                    self._record_selected_consensus(selected)
                    return selected
                selected = ConsensusType.SBOX_POUW
                self._record_selected_consensus(selected)
                return selected

            if sbox_available:
                selected = ConsensusType.SBOX_POUW
                self._record_selected_consensus(selected)
                return selected
            if has_pouw:
                selected = ConsensusType.POUW
                self._record_selected_consensus(selected)
                return selected

            if not pow_fallback_allowed:
                self._auto_generate_pouw(count=4)
                if self.has_pouw_tasks():
                    selected = ConsensusType.POUW
                    self._record_selected_consensus(selected)
                    return selected
            self.log("⚠️ S-Box/POUW 均不可用，回退 PoW 保活")
            selected = ConsensusType.POW
            self._record_selected_consensus(selected)
            return selected

        # mixed 模式：按确定性比例混用
        if sbox_available and has_pouw:
            latest = self.get_latest_block()
            seed = f"{latest.hash if latest else 'genesis'}:{self.node_id}:{self._consensus_round}".encode()
            roll = int(hashlib.sha256(seed).hexdigest()[:8], 16) / 0xFFFFFFFF
            self._consensus_round += 1
            if roll < self.consensus_sbox_ratio:
                selected = ConsensusType.SBOX_POUW
                self._record_selected_consensus(selected)
                return selected
            selected = ConsensusType.POUW
            self._record_selected_consensus(selected)
            return selected

        if sbox_available:
            selected = ConsensusType.SBOX_POUW
            self._record_selected_consensus(selected)
            return selected
        if has_pouw:
            selected = ConsensusType.POUW
            self._record_selected_consensus(selected)
            return selected

        if not pow_fallback_allowed:
            self._auto_generate_pouw(count=4)
            if self.has_pouw_tasks():
                selected = ConsensusType.POUW
                self._record_selected_consensus(selected)
                return selected
        self.log("⚠️ S-Box/POUW 均不可用，回退 PoW 保活")
        selected = ConsensusType.POW
        self._record_selected_consensus(selected)
        return selected

    def _record_selected_consensus(self, consensus: ConsensusType):
        """记录最近选择的共识类型。"""
        try:
            self._recent_consensus_selected.append(consensus.value)
        except Exception:
            pass

    def _record_mined_consensus(self, consensus: ConsensusType):
        """记录最近成功出块的共识类型。"""
        try:
            self._recent_consensus_mined.append(consensus.value)
            if consensus == ConsensusType.POW:
                self._consecutive_pow_blocks += 1
            else:
                self._consecutive_pow_blocks = 0
        except Exception:
            pass

    def _current_task_pool_size(self) -> int:
        """估算任务池规模（POUW 证明 + 待处理交易）。"""
        return int(len(self.pending_pouw) + len(self.pending_transactions))

    def _current_pouw_ratio(self) -> float:
        """计算最近窗口内 PoUW/SBOX_POUW 占比。"""
        values = list(self._recent_consensus_mined)
        if not values:
            return 1.0
        pouw_like = sum(
            1
            for x in values
            if x in (ConsensusType.POUW.value, ConsensusType.SBOX_POUW.value)
        )
        return pouw_like / len(values)

    def _should_force_pouw_guardrail(self, has_pouw: bool) -> bool:
        """活性与占比守护：避免长时间退化为 PoW。"""
        if not has_pouw:
            return False
        if self._consecutive_pow_blocks >= self._max_consecutive_pow_blocks:
            return True
        if self._current_pouw_ratio() < self._min_pouw_ratio:
            return True
        return False

    def _build_consensus_distribution(self, values: List[str]) -> Dict[str, Any]:
        """构建共识分布统计。"""
        total = len(values)
        if total == 0:
            return {
                "window": 0,
                "counts": {"POUW": 0, "SBOX_POUW": 0, "POW": 0},
                "sbox_ratio": 0.0,
                "pouw_ratio": 0.0,
                "pow_ratio": 0.0,
            }

        count_pouw = sum(1 for x in values if x == ConsensusType.POUW.value)
        count_sbox = sum(1 for x in values if x == ConsensusType.SBOX_POUW.value)
        count_pow = sum(1 for x in values if x == ConsensusType.POW.value)
        return {
            "window": total,
            "counts": {
                "POUW": count_pouw,
                "SBOX_POUW": count_sbox,
                "POW": count_pow,
            },
            "sbox_ratio": round(count_sbox / total, 4),
            "pouw_ratio": round(count_pouw / total, 4),
            "pow_ratio": round(count_pow / total, 4),
        }

    def configure_consensus_mode(
        self,
        mode: str = "mixed",
        sbox_ratio: float = 0.5,
        pouw_support_ratio: float = 0.1,
        sbox_enabled: Optional[bool] = None,
    ):
        """配置共识模式。"""
        mode = (mode or "mixed").lower().strip()
        if mode not in {"sbox_primary", "mixed", "sbox_only", "pouw_only"}:
            mode = "mixed"

        ratio = max(0.0, min(1.0, float(sbox_ratio)))
        support_ratio = max(0.0, min(1.0, float(pouw_support_ratio)))

        # 治理保护：限制单次 ratio 变化幅度，防止参数瞬时漂移。
        max_ratio_step = float(self._mechanism_strategy.get("max_ratio_step", 1.0))
        if 0.0 < max_ratio_step < 1.0:
            lower = max(0.0, self.consensus_sbox_ratio - max_ratio_step)
            upper = min(1.0, self.consensus_sbox_ratio + max_ratio_step)
            ratio = max(lower, min(upper, ratio))

        self.consensus_mode = mode
        self.consensus_sbox_ratio = ratio
        self.consensus_pouw_support_ratio = support_ratio
        if sbox_enabled is not None:
            self._sbox_mining_enabled = bool(sbox_enabled)

        self.log(
            f"⚙️ 共识模式已配置: mode={self.consensus_mode}, "
            f"sbox_ratio={self.consensus_sbox_ratio:.2f}, "
            f"pouw_support_ratio={self.consensus_pouw_support_ratio:.2f}, "
            f"sbox_enabled={self._sbox_mining_enabled}"
        )

    def get_mechanism_strategy(self) -> Dict[str, Any]:
        return dict(self._mechanism_strategy)

    def configure_mechanism_strategy(
        self,
        actor_id: str = "system",
        rollback_to_previous: bool = False,
        **kwargs,
    ) -> Dict[str, Any]:
        """更新机制策略（版本化参数），支持一键回滚到上一版本。"""
        if rollback_to_previous and self._strategy_history:
            previous = self._strategy_history.pop()
            self._mechanism_strategy = dict(previous)
            self._mechanism_strategy["updated_at"] = time.time()
            self.log(f"🔁 机制策略已回滚: by={actor_id}, version={self._mechanism_strategy.get('version')}")
            return self.get_mechanism_strategy()

        self._strategy_history.append(dict(self._mechanism_strategy))
        if len(self._strategy_history) > 100:
            self._strategy_history = self._strategy_history[-100:]

        if kwargs.get("version"):
            self._mechanism_strategy["version"] = str(kwargs["version"])
        if kwargs.get("rollout"):
            self._mechanism_strategy["rollout"] = str(kwargs["rollout"])
        if kwargs.get("max_ratio_step") is not None:
            mrs = float(kwargs["max_ratio_step"])
            self._mechanism_strategy["max_ratio_step"] = max(0.01, min(1.0, mrs))

        mode = kwargs.get("mode")
        ratio = kwargs.get("sbox_ratio")
        support_ratio = kwargs.get("pouw_support_ratio")
        sbox_enabled = kwargs.get("sbox_enabled")
        if mode is not None or ratio is not None or support_ratio is not None or sbox_enabled is not None:
            self.configure_consensus_mode(
                mode=mode if mode is not None else self.consensus_mode,
                sbox_ratio=float(ratio) if ratio is not None else self.consensus_sbox_ratio,
                pouw_support_ratio=(
                    float(support_ratio)
                    if support_ratio is not None
                    else self.consensus_pouw_support_ratio
                ),
                sbox_enabled=sbox_enabled,
            )

        if kwargs.get("work_threshold") is not None:
            wt = float(kwargs["work_threshold"])
            self.current_work_threshold = max(20.0, min(180.0, wt))

        self._mechanism_strategy["updated_at"] = time.time()
        self.log(
            f"🧭 机制策略已更新: by={actor_id}, version={self._mechanism_strategy.get('version')}, "
            f"rollout={self._mechanism_strategy.get('rollout')}"
        )
        return self.get_mechanism_strategy()
    
    def _auto_generate_pouw(self, count: int = 4):
        """自动生成基准 POUW 任务并执行。
        
        当没有用户提交的计算任务时，节点自动执行基准计算（矩阵乘法、
        线性回归、梯度下降、哈希搜索），确保每个区块都包含真实有用工作。
        
        Args:
            count: 生成的任务数量（默认 4 个）
        """
        try:
            cpu_before = psutil.cpu_percent(interval=None) if HAS_PSUTIL else 0.0
        except Exception:
            cpu_before = 0.0
        
        for _ in range(count):
            # 任务类型选择引入高熵，避免固定轮转被提前预测。
            latest = self.get_latest_block()
            latest_hash = latest.hash if latest else "genesis"
            selector_seed = (
                f"{latest_hash}:{self.node_id}:{time.time_ns()}:"
                f"{uuid.uuid4().hex}:{self._pouw_task_index}"
            )
            challenge_window = int(time.time() // 30)
            challenge_window_start_ms = challenge_window * 30000
            challenge_window_end_ms = challenge_window_start_ms + 30000
            selector = int(hashlib.sha256(selector_seed.encode()).hexdigest()[:8], 16)
            task_type = self._pouw_task_types[selector % len(self._pouw_task_types)]
            self._pouw_task_index += 1
            
            try:
                # 基准任务难度跟随链上难度动态变化。
                # 适度抬升上限，降低低负载阶段“过易出块”概率。
                baseline_difficulty = max(3, min(6, int(self.current_difficulty)))
                task_seed = hashlib.sha256(
                    f"{selector_seed}:{task_type.value}".encode()
                ).hexdigest()
                task = self.pouw_executor.generate_task(
                    task_type,
                    difficulty=baseline_difficulty,
                    task_seed=task_seed,
                    entropy=selector_seed,
                    prev_hash=latest_hash,
                    block_height=(latest.height + 1) if latest else 0,
                    miner_id=self.node_id,
                    challenge_window=challenge_window,
                    challenge_window_start_ms=challenge_window_start_ms,
                    challenge_window_end_ms=challenge_window_end_ms,
                )
                
                # 执行真实计算
                result: RealPoUWResult = self.pouw_executor.execute_task(task, self.node_id)
                
                # 采集执行后资源
                try:
                    cpu_after = psutil.cpu_percent(interval=None) if HAS_PSUTIL else 0.0
                except Exception:
                    cpu_after = 0.0
                cpu_usage = max(cpu_before, cpu_after, 30.0)  # 至少 30%
                
                # 将 RealPoUWResult → POUWProof
                proof = POUWProof(
                    task_id=result.task_id,
                    miner_id=result.miner_id,
                    compute_hash=result.computation_proof,
                    execution_time=result.execution_time,
                    gpu_cycles=int(result.execution_time * 1e6),  # 估算
                    memory_used=64,  # MB 估算
                    cpu_usage=cpu_usage,
                    gpu_usage=0.0,  # CPU 任务无 GPU
                    verified=result.verified,
                    quality_score=result.score * 100,  # 0-1 → 0-100
                )
                
                self.add_pouw_proof(proof)
                
            except Exception as e:
                self.log(f"POUW 任务执行异常: {e}")
                continue

    @staticmethod
    def _extract_structured_proof(compute_hash: str) -> Optional[Dict[str, Any]]:
        """从 computation_proof 提取结构化 proof_json 载荷。"""
        if not isinstance(compute_hash, str) or not compute_hash.startswith("proof_json="):
            return None
        try:
            return json.loads(compute_hash[len("proof_json="):])
        except Exception:
            return None

    def _compute_proof_confidence(self, proof: POUWProof) -> float:
        """三层校验置信度：快速校验 + 抽样 + 可证明执行。"""
        quick = 0.4 if proof.compute_hash else 0.0
        sample = 0.35 if proof.verified else 0.0
        # 使用结构化证明作为可证明计算代理信号（兼容无 TEE/ZK 场景）
        provable = 0.25 if self._extract_structured_proof(proof.compute_hash) else 0.0
        return round(max(0.0, min(1.0, quick + sample + provable)), 4)

    def _calculate_useful_work_bonus(self, block: Block) -> float:
        """有用工作奖励：reward = base + λ * useful_work_score。"""
        if block.consensus_type not in (ConsensusType.POUW, ConsensusType.SBOX_POUW):
            return 0.0
        proofs = block.pouw_proofs or []
        if not proofs:
            return 0.0
        useful_work_score = 0.0
        for p in proofs:
            work = float(p.get("work_amount", p.get("work_score", 0.0)))
            confidence = float(p.get("confidence_score", 0.0))
            useful_work_score += max(0.0, work) * max(0.0, min(1.0, confidence))
        return round(max(0.0, useful_work_score) * ChainParams.USEFUL_WORK_REWARD_LAMBDA, 8)

    def _get_dynamic_work_threshold(self, block: Block) -> float:
        """计算动态工作量门槛（含无单场景扰动）。"""
        threshold = float(self.current_work_threshold)

        # 无单场景：使用更高地板 + 可验证扰动，降低模板化与低负载过易出块风险。
        has_real_orders = len(block.transactions or []) > 0
        if not has_real_orders:
            latest = self.get_latest_block()
            seed = f"{latest.hash if latest else 'genesis'}:{block.height}:{self.node_id}:idle"
            jitter_src = int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16)
            jitter = ((jitter_src % 1000) / 1000.0 - 0.5) * 8.0  # [-4, +4]
            threshold = max(35.0, min(threshold, 68.0)) + jitter

            # 连续基准块惩罚：无真实订单持续越久，门槛越高，避免长期“顺滑出块”。
            streak = max(0, int(getattr(self, "_consecutive_benchmark_blocks", 0)))
            if streak > 0:
                threshold *= min(1.35, 1.0 + 0.04 * streak)

        if block.block_type == PoUWBlockType.VALIDATION_BLOCK.value:
            threshold *= 0.90
        elif block.block_type == PoUWBlockType.IDLE_BLOCK.value:
            threshold *= 0.95

        return round(max(20.0, min(180.0, threshold)), 2)

    def _record_work_observation(self, block: Block, mining_time: float):
        """记录工作量难度调节观测数据。"""
        if block.consensus_type not in (ConsensusType.POUW, ConsensusType.SBOX_POUW):
            return
        proofs = block.pouw_proofs or []
        if not proofs:
            return
        avg_quality = sum(float(p.get("quality_score", 0.0)) for p in proofs) / max(len(proofs), 1)
        has_real_orders = len(block.transactions or []) > 0
        self.work_difficulty_adjuster.record_observation(
            mining_time=mining_time,
            avg_quality=avg_quality,
            has_real_orders=has_real_orders,
            hash_difficulty=self.current_difficulty,
        )
    
    def mine_pow(self, block: Block, max_iterations: int = 0) -> bool:
        """执行 PoW 挖矿。
        
        max_iterations=0 表示无限制，持续挖直到成功或 stop_mining。
        """
        target = "0" * block.difficulty
        nonce = 0
        limit = max_iterations if max_iterations > 0 else float('inf')
        
        while nonce < limit:
            if self.stop_mining:
                return False
            
            block.nonce = nonce
            block.hash = block.compute_hash()
            
            if block.hash.startswith(target):
                return True
            
            nonce += 1
        
        return False
    
    def mine_pouw(self, block: Block) -> bool:
        """执行 POUW 挖矿。
        
        通过执行真实计算任务获得出块权。
        每个区块包含 POUW 证明，证明矿工执行了真实有用计算。
        """
        with self._lock:
            if not self.pending_pouw:
                return False
            
            # 收集 POUW 证明
            proofs = self.pending_pouw[:10]  # 最多 10 个
            self.pending_pouw = self.pending_pouw[10:]
        
        # 计算总工作量
        total_work = sum(p.compute_work_score() for p in proofs)
        confidence_weighted_work = sum(
            p.compute_work_score() * self._compute_proof_confidence(p)
            for p in proofs
        )
        
        # 工作量通道动态难度阈值（与 hash 难度并行调节）。
        min_threshold = self._get_dynamic_work_threshold(block)
        
        if total_work >= min_threshold and confidence_weighted_work >= ChainParams.POUW_CONFIDENCE_THRESHOLD:
            block.pouw_proofs = [
                {
                    "proof_id": p.proof_id,
                    "task_id": p.task_id,
                    "task_type": "benchmark",
                    "compute_hash": p.compute_hash,
                    "work_amount": round(p.compute_work_score(), 2),
                    "work_score": round(p.compute_work_score(), 2),  # alias for backward compat
                    "execution_time": round(max(p.execution_time, 0.0001), 6),
                    "quality_score": round(p.quality_score, 2),
                    "confidence_score": self._compute_proof_confidence(p),
                    "miner_id": p.miner_id,
                    "verified": p.verified,
                    "proof_meta": self._extract_structured_proof(p.compute_hash) or {},
                }
                for p in proofs
            ]
            
            # D-02 fix: POUW 区块仍需要轻量级 PoW（至少 1 个前导零）防止平凡出块
            target = "0"  # 1 个前导零
            for nonce_attempt in range(200000):
                block.nonce = nonce_attempt
                candidate_hash = block.compute_hash()
                if candidate_hash.startswith(target):
                    block.hash = candidate_hash
                    break
            else:
                # 无法找到有效 nonce (极端情况)
                # M-1 fix: 使用密码学安全的随机数
                import secrets as _secrets
                block.nonce = _secrets.randbelow(1000000)
                block.hash = block.compute_hash()
            
            self.log(f"🔬 POUW 出块: {len(proofs)} 个证明, 总工作量={total_work:.1f}")
            return True
        
        # 工作量不足，将证明放回队列避免浪费
        with self._lock:
            self.pending_pouw = proofs + self.pending_pouw
        self.log(
            f"⚠️ POUW 工作量/置信度不足: work={total_work:.1f}<{min_threshold} "
            f"or confidence_work={confidence_weighted_work:.2f}<{ChainParams.POUW_CONFIDENCE_THRESHOLD}"
        )
        return False
    
    def _get_sbox_miner(self):
        """获取或初始化 S-Box 矿工。"""
        if self._sbox_miner is None:
            try:
                from core.sbox_miner import MultiSectorSBoxMiner, SBoxMiningParams
                from core.sbox_engine import get_sbox_library
                
                # 获取活跃板块列表
                try:
                    from core.sector_coin import get_sector_registry
                    registry = get_sector_registry()
                    sectors = registry.get_active_sectors()
                except Exception:
                    sectors = ["H100", "RTX4090", "RTX3080", "CPU", "GENERAL"]
                
                params = SBoxMiningParams(
                    score_threshold=0.55,
                    hash_difficulty=max(2, self.current_difficulty),
                    target_block_time=ChainParams.TARGET_BLOCK_TIME,
                    adjustment_interval=ChainParams.DIFFICULTY_ADJUSTMENT_INTERVAL,
                )
                
                self._sbox_miner = MultiSectorSBoxMiner(
                    miner_id=self.node_id,
                    sectors=sectors,
                    params=params,
                    log_fn=self.log,
                )
                self.log(f"S-Box 挖矿引擎初始化: {len(sectors)} 个板块")
            except Exception as e:
                self.log(f"S-Box 挖矿引擎初始化失败: {e}")
                self._sbox_mining_enabled = False
        return self._sbox_miner

    @staticmethod
    def _normalize_sbox_weights(raw_weights: Dict[str, float]) -> Dict[str, float]:
        """归一化评分权重，确保和为 1。"""
        total = sum(max(0.01, float(v)) for v in raw_weights.values())
        if total <= 0:
            return {"nonlinearity": 0.34, "diff_uniformity": 0.33, "avalanche": 0.33}
        return {k: round(max(0.01, float(v)) / total, 6) for k, v in raw_weights.items()}

    def _apply_sbox_halfhour_quiz(self, miner, block: Block, prev_hash: str) -> Dict[str, Any]:
        """应用每 30 分钟一次的随机评分题（按矿机确定性生成）。"""
        now_ts = int(time.time())
        window_id = now_ts // self._sbox_quiz_interval_seconds
        if window_id == self._sbox_quiz_window_id and self._sbox_quiz_payload:
            return dict(self._sbox_quiz_payload)

        window_start = window_id * self._sbox_quiz_interval_seconds
        window_end = window_start + self._sbox_quiz_interval_seconds
        seed_input = f"{prev_hash}:{self.node_id}:{window_id}:{block.height}"
        seed_hex = hashlib.sha256(seed_input.encode()).hexdigest()

        # 本窗口随机评分题：动态调整 3 个评分权重。
        w_non = 20 + (int(seed_hex[0:4], 16) % 41)   # [20,60]
        w_du = 20 + (int(seed_hex[4:8], 16) % 41)    # [20,60]
        w_ae = 20 + (int(seed_hex[8:12], 16) % 41)   # [20,60]
        quiz_weights = self._normalize_sbox_weights({
            "nonlinearity": float(w_non),
            "diff_uniformity": float(w_du),
            "avalanche": float(w_ae),
        })

        base_threshold = float(getattr(miner.params, "score_threshold", 0.55))
        threshold_bias = ((int(seed_hex[12:16], 16) % 9) - 4) * 0.01  # [-0.04, +0.04]
        threshold = max(
            miner.params.min_score_threshold,
            min(miner.params.max_score_threshold, base_threshold + threshold_bias),
        )

        base_hash_diff = int(max(2, getattr(miner.params, "hash_difficulty", self.current_difficulty)))
        hash_bonus = int(seed_hex[16:18], 16) % 2  # 0 or 1
        hash_difficulty = max(
            miner.params.min_hash_difficulty,
            min(miner.params.max_hash_difficulty, base_hash_diff + hash_bonus),
        )

        # 同步本窗口评分题到所有板块矿工，确保同一矿机在窗口内题目一致。
        miner.params.score_weights = dict(quiz_weights)
        miner.params.score_threshold = float(threshold)
        miner.params.hash_difficulty = int(hash_difficulty)
        for sector_miner in miner.sector_miners.values():
            sector_miner.params.score_weights = dict(quiz_weights)
            sector_miner.params.score_threshold = float(threshold)
            sector_miner.params.hash_difficulty = int(hash_difficulty)

        payload = {
            "quiz_id": seed_hex[:16],
            "window_id": int(window_id),
            "window_start": int(window_start),
            "window_end": int(window_end),
            "weights": dict(quiz_weights),
            "score_threshold": round(float(threshold), 6),
            "hash_difficulty": int(hash_difficulty),
        }
        self._sbox_quiz_window_id = int(window_id)
        self._sbox_quiz_payload = dict(payload)
        self.log(
            f"🧩 S-Box 评分题更新: quiz={payload['quiz_id']}, "
            f"window={payload['window_start']}~{payload['window_end']}, "
            f"threshold={payload['score_threshold']:.4f}, hash_diff={payload['hash_difficulty']}"
        )
        return payload
    
    def mine_sbox_pouw(self, block: Block) -> bool:
        """执行 S-Box PoUW 挖矿。
        
        所有板块并行挖矿产出 S-Box，VRF 随机选取一个板块公布。
        区块包含: S-Box 数据 + 安全评分 + 传统 POUW 证明。
        
        流程:
        1. 各板块并行生成高质量 S-Box (遗传优化)
        2. 验证 S-Box 评分 >= 阈值
        3. VRF 随机选取一个板块的 S-Box
        4. 选中的 S-Box 写入区块并全网公布
        5. 同时执行传统 POUW 任务
        6. 区块 hash 包含 S-Box 数据
        """
        miner = self._get_sbox_miner()
        if miner is None:
            # S-Box 挖矿不可用，回退到传统 POUW
            block.consensus_type = ConsensusType.POUW
            return self.mine_pouw(block)
        
        latest = self.get_latest_block()
        if not latest:
            return False

        quiz_payload = self._apply_sbox_halfhour_quiz(miner, block, latest.hash)
        
        # 1. 多板块并行 S-Box 挖矿
        selected_block, all_blocks, selected_sector = miner.mine_parallel(
            prev_hash=latest.hash,
            block_height=block.height,
            timeout=ChainParams.TARGET_BLOCK_TIME * 2,
        )
        
        if not selected_block:
            self.log("S-Box 挖矿超时，回退 POUW")
            block.consensus_type = ConsensusType.POUW
            return self.mine_pouw(block)
        
        # 2. 将 S-Box 数据写入区块
        block.consensus_type = ConsensusType.SBOX_POUW
        block.sbox_hex = selected_block.sbox_hex
        block.sbox_score = selected_block.score
        block.sbox_nonlinearity = selected_block.nonlinearity
        block.sbox_diff_uniformity = selected_block.diff_uniformity
        block.sbox_avalanche = selected_block.avalanche
        block.sbox_score_weights = selected_block.score_weights
        block.sbox_score_threshold = selected_block.score_threshold
        block.sbox_selected_sector = selected_sector
        block.sbox_all_sectors = [b.sector for b in all_blocks]
        block.extra_data = json.dumps(
            {
                "sbox_quiz": {
                    "quiz_id": quiz_payload.get("quiz_id", ""),
                    "window_start": quiz_payload.get("window_start", 0),
                    "window_end": quiz_payload.get("window_end", 0),
                    "score_threshold": quiz_payload.get("score_threshold", 0.0),
                    "hash_difficulty": quiz_payload.get("hash_difficulty", 0),
                }
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        
        # 3. 同时收集传统 POUW 证明（如果有）
        with self._lock:
            if self.pending_pouw:
                proofs = self.pending_pouw[:5]
                self.pending_pouw = self.pending_pouw[5:]
                block.pouw_proofs = [
                    {
                        "proof_id": p.proof_id,
                        "task_id": p.task_id,
                        "task_type": "benchmark",
                        "compute_hash": p.compute_hash,
                        "work_amount": round(p.compute_work_score(), 2),
                        "work_score": round(p.compute_work_score(), 2),
                        "execution_time": round(max(p.execution_time, 0.0001), 6),
                        "quality_score": round(p.quality_score, 2),
                        "confidence_score": self._compute_proof_confidence(p),
                        "miner_id": p.miner_id,
                        "verified": p.verified,
                        "proof_meta": self._extract_structured_proof(p.compute_hash) or {},
                    }
                    for p in proofs
                ]
        
        # 4. S-Box 区块也需要满足 Hash 难度
        hash_target = "0" * max(1, min(block.difficulty, selected_block.hash_difficulty))
        for nonce_attempt in range(500000):
            if self.stop_mining:
                return False
            block.nonce = nonce_attempt
            candidate_hash = block.compute_hash()
            if candidate_hash.startswith(hash_target):
                block.hash = candidate_hash
                break
        else:
            import secrets as _secrets
            block.nonce = _secrets.randbelow(1000000)
            block.hash = block.compute_hash()
        
        # 5. 更新 S-Box 挖矿难度
        miner.update_params(block.height, 0, selected_block.score)
        
        # 6. 更新全网 S-Box 并触发加密刷新
        try:
            from core.sbox_engine import get_sbox_library, BlockSBox, sbox_hash
            from core.sbox_engine import hex_to_sbox
            
            sbox_lib = get_sbox_library()
            block_sbox = BlockSBox(
                sbox=hex_to_sbox(selected_block.sbox_hex),
                sbox_hash=sbox_hash(hex_to_sbox(selected_block.sbox_hex)),
                score=selected_block.score,
                nonlinearity=selected_block.nonlinearity,
                diff_uniformity=selected_block.diff_uniformity,
                avalanche=selected_block.avalanche,
                weights=selected_block.score_weights,
                miner_id=self.node_id,
                sector=selected_sector,
            )
            sbox_lib.set_current(block_sbox)
        except Exception as e:
            self.log(f"S-Box 库更新失败(非致命): {e}")
        
        self.log(
            f"S-Box PoUW 出块: 板块={selected_sector}, "
            f"score={selected_block.score:.4f}, "
            f"NL={selected_block.nonlinearity}, "
            f"DU={selected_block.diff_uniformity}, "
            f"AE={selected_block.avalanche:.2f}, "
            f"参与板块={len(all_blocks)}"
        )
        return True
    
    def create_block(
        self,
        miner_address: str,
        max_txs: int = 100,
    ) -> Optional[Block]:
        """创建新区块。"""
        latest = self.get_latest_block()
        if not latest:
            return None
        
        # 选择共识
        consensus = self.select_consensus()
        
        # === 区块类型选择 ===
        has_pending = len(self.pending_pouw) > 0 or len(self.pending_transactions) > 0
        seconds_since_last = time.time() - self._last_block_time
        
        # 活跃性检查：超时强制出空块
        should_force, force_type = LivenessConstraints.should_force_block(
            seconds_since_last, len(self.pending_pouw)
        )
        
        if should_force:
            block_type = force_type
            bt_reason = "超时强制出块"
        else:
            block_type, bt_reason = BlockTypeSelector.select(
                has_pending_tasks=has_pending,
                miner_is_online=True,
                miner_is_witness=False,
                consecutive_idle_blocks=self._consecutive_idle,
                consecutive_validation_blocks=self._consecutive_validation,
            )
        
        # 如果选择器返回 None（条件不满足），默认用 IDLE_BLOCK
        if block_type is None:
            block_type = PoUWBlockType.IDLE_BLOCK
            bt_reason = "默认空闲块"
        
        # 创建区块
        block = Block(
            height=latest.height + 1,
            prev_hash=latest.hash,
            timestamp=time.time(),
            miner_id=self.node_id,
            miner_address=miner_address,
            consensus_type=consensus,
            difficulty=self.current_difficulty,
            sector=self.sector,
            block_type=block_type.value,
        )
        
        # 更新连续计数器
        if block_type == PoUWBlockType.IDLE_BLOCK:
            self._consecutive_idle += 1
            self._consecutive_validation = 0
        elif block_type == PoUWBlockType.VALIDATION_BLOCK:
            self._consecutive_validation += 1
            self._consecutive_idle = 0
        else:
            self._consecutive_idle = 0
            self._consecutive_validation = 0
        
        # 添加交易
        txs = self.pending_transactions[:max_txs]
        block.transactions = txs
        block.merkle_root = block.compute_merkle_root()
        
        # 计算手续费
        block.total_fees = sum(tx.get("fee", 0) for tx in txs)
        
        # 计算奖励 (含类型衰减)
        base_reward = self.reward_calculator.get_block_reward(block.height)
        if block_type == PoUWBlockType.IDLE_BLOCK:
            block.block_reward = RewardDecayRules.calculate_reward(
                PoUWBlockType.IDLE_BLOCK, base_reward, self._consecutive_idle
            )
            # 无单惩罚窗口：连续空闲块过多时陡峭衰减，避免节点长期等待基准任务。
            if self._consecutive_idle > self._idle_penalty_window:
                overflow = self._consecutive_idle - self._idle_penalty_window
                penalty = max(0.35, 1.0 - 0.18 * overflow)
                block.block_reward = round(block.block_reward * penalty, 8)
        elif block_type == PoUWBlockType.VALIDATION_BLOCK:
            block.block_reward = RewardDecayRules.calculate_reward(
                PoUWBlockType.VALIDATION_BLOCK, base_reward, self._consecutive_validation
            )
        else:
            block.block_reward = base_reward
        
        self.log(f"📦 区块类型: {block_type.value} ({bt_reason})")
        
        return block
    
    def mine_block(self, miner_address: str) -> Optional[Block]:
        """挖矿产出区块。"""
        block = self.create_block(miner_address)
        if not block:
            return None
        
        self.log(f"⛏️ 开始挖矿 #{block.height} ({block.consensus_type.value})")
        
        start_time = time.time()
        success = False
        
        if block.consensus_type == ConsensusType.POUW:
            success = self.mine_pouw(block)
        elif block.consensus_type == ConsensusType.SBOX_POUW:
            success = self.mine_sbox_pouw(block)
        else:
            success = self.mine_pow(block)
        
        mining_time = time.time() - start_time
        
        if success:
            useful_bonus = self._calculate_useful_work_bonus(block)
            if useful_bonus > 0:
                block.block_reward = round(block.block_reward + useful_bonus, 8)

            # 清理已打包交易
            packed_count = len(block.transactions)
            self.pending_transactions = self.pending_transactions[packed_count:]

            # 连续基准块计数：用于低负载阶段的动态门槛惩罚。
            if len(block.transactions or []) > 0:
                self._consecutive_benchmark_blocks = 0
            else:
                self._consecutive_benchmark_blocks = min(
                    self._consecutive_benchmark_blocks + 1,
                    50,
                )
            
            # 记录
            self.difficulty_adjuster.record_block(mining_time)
            self.total_blocks_mined += 1
            self._last_block_time = time.time()
            self._record_mined_consensus(block.consensus_type)
            
            # 评分系统记录
            try:
                self.metrics_collector.record_block(self.node_id)
                if block.consensus_type in (ConsensusType.POUW, ConsensusType.SBOX_POUW) and block.pouw_proofs:
                    for proof in block.pouw_proofs:
                        proof_data = proof if isinstance(proof, dict) else proof.__dict__
                        self.metrics_collector.record_task(
                            self.node_id,
                            success=proof_data.get('verified', True),
                            response_time_ms=proof_data.get('execution_time', 0) * 1000
                        )
            except Exception as e:
                self.log(f"⚠️ 评分记录失败(非致命): {e}")

            # 工作量难度观测与调节（双通道动态难度）。
            self._record_work_observation(block, mining_time)
            
            # 难度调整
            if self.difficulty_adjuster.should_adjust(block.height):
                new_diff = self.difficulty_adjuster.calculate_new_difficulty(
                    self.current_difficulty
                )
                if new_diff != self.current_difficulty:
                    self.log(f"📊 难度调整: {self.current_difficulty} -> {new_diff}")
                    self.current_difficulty = new_diff

            if self.work_difficulty_adjuster.should_adjust(block.height):
                new_work_threshold = self.work_difficulty_adjuster.calculate_new_threshold(
                    self.current_work_threshold
                )
                if new_work_threshold != self.current_work_threshold:
                    self.log(
                        f"📈 工作量阈值调整: {self.current_work_threshold:.2f} -> {new_work_threshold:.2f}"
                    )
                    self.current_work_threshold = new_work_threshold
            
            self.log(
                f"✅ 区块 #{block.height} [{block.block_type}] 挖出 "
                f"({mining_time:.2f}s, 奖励={block.block_reward:.4f}, useful_bonus={useful_bonus:.4f})"
            )
            
            return block
        
        return None
    
    def validate_block(self, block: Block) -> Tuple[bool, str]:
        """验证区块（含安全加固：时间戳/Merkle/Coinbase/UTXO/POUW绑定）。"""
        expected_height = self.get_chain_height() + 1
        
        # 高度检查
        if block.height != expected_height:
            return False, f"Invalid height: expected {expected_height}, got {block.height}"
        
        # 前哈希检查
        latest = self.get_latest_block()
        if not latest:
            return False, "No latest block"
        if block.prev_hash != latest.hash:
            return False, "Invalid prev_hash"
        
        # D-14 fix: MTP (Median Time Past) 检查
        # 区块时间戳必须大于最近 11 个区块的中位时间
        recent_timestamps = []
        with self._lock:
            for b in self.chain[-11:]:
                recent_timestamps.append(b.timestamp)
        if len(recent_timestamps) >= 3:
            sorted_ts = sorted(recent_timestamps)
            mtp = sorted_ts[len(sorted_ts) // 2]
            if block.timestamp <= mtp:
                return False, f"Block timestamp {block.timestamp:.0f} <= MTP {mtp:.0f}"
        else:
            # 链太短时回退到简单检查
            if block.timestamp < latest.timestamp:
                return False, "Block timestamp before parent"
        
        # 安全加固：未来时间戳限制（最多允许 2 小时偏差）
        MAX_FUTURE_DRIFT = 7200  # 2 hours
        if block.timestamp > time.time() + MAX_FUTURE_DRIFT:
            return False, f"Block timestamp too far in future (max drift {MAX_FUTURE_DRIFT}s)"
        
        # 区块大小检查
        if not block.is_within_size_limit():
            return False, "Block exceeds size limit"
        
        # 安全加固：Merkle Root 验证
        if hasattr(block, 'compute_merkle_root'):
            expected_merkle = block.compute_merkle_root()
            if block.merkle_root and expected_merkle and block.merkle_root != expected_merkle:
                return False, f"Invalid merkle_root: expected {expected_merkle[:16]}, got {block.merkle_root[:16]}"
        
        # 安全加固：Coinbase 奖励金额验证
        if block.block_reward > 0:
            expected_reward = self.reward_calculator.get_block_reward(block.height)
            useful_bonus = self._calculate_useful_work_bonus(block)
            # 独立验证 total_fees（不信任区块自报的 total_fees）
            actual_fees = sum(tx.get("fee", 0) for tx in (block.transactions or [])
                             if tx.get("tx_type", "transfer") != "coinbase")
            if abs(block.total_fees - actual_fees) > 0.00000001:
                return False, f"total_fees mismatch: declared {block.total_fees}, actual {actual_fees}"
            max_allowed = expected_reward + useful_bonus + actual_fees + 0.00000001
            if block.block_reward > max_allowed:
                return False, f"Coinbase reward too high: {block.block_reward} > max {max_allowed}"
        
        # 安全加固：难度验证（区块难度不能低于引擎当前难度）
        if block.difficulty < self.current_difficulty:
            return False, f"Block difficulty {block.difficulty} < required {self.current_difficulty}"
        
        # 共识验证 / Consensus validation
        if block.consensus_type == ConsensusType.POW:
            if not block.is_valid_pow():
                return False, "Invalid PoW"
        elif block.consensus_type == ConsensusType.POUW:
            # 安全加固：POUW 证明必须存在且内容有效
            # Security: POUW proofs must exist AND contain valid content
            if not block.pouw_proofs:
                return False, "Missing POUW proofs"
            
            # D-02 fix: POUW 区块也需要满足最低 PoW 难度（1 个前导零）
            if not block.hash.startswith("0"):
                return False, "POUW block hash does not meet minimum difficulty (1 leading zero)"
            # 验证区块哈希正确性
            recomputed = block.compute_hash()
            if block.hash != recomputed:
                return False, "POUW block hash mismatch on recomputation"
            
            # 验证每个证明的结构完整性
            # Validate structural integrity of each proof
            seen_task_ids = set()
            seen_proof_hashes = set()
            confidence_work_sum = 0.0
            for i, proof in enumerate(block.pouw_proofs):
                if not isinstance(proof, dict):
                    return False, f"POUW proof #{i} is not a dict"
                # 证明必须包含关键字段
                required_fields = {'task_id', 'miner_id', 'compute_hash', 'verified', 'execution_time'}
                missing = required_fields - set(proof.keys())
                if missing:
                    return False, f"POUW proof #{i} missing fields: {missing}"
                # work_amount 或 work_score 必须为正数（兼容两种字段名）
                work = proof.get('work_amount', proof.get('work_score', 0))
                if not isinstance(work, (int, float)) or work <= 0:
                    return False, f"POUW proof #{i} has invalid work_amount: {work}"

                confidence = proof.get('confidence_score', 0.4 if proof.get('verified') else 0.0)
                if not isinstance(confidence, (int, float)):
                    return False, f"POUW proof #{i} invalid confidence_score: {confidence}"
                confidence = max(0.0, min(1.0, float(confidence)))
                confidence_work_sum += float(work) * confidence

                # 计算证明必须存在且格式合理（前缀=值）
                compute_hash = proof.get('compute_hash', '')
                if not isinstance(compute_hash, str) or '=' not in compute_hash:
                    return False, f"POUW proof #{i} invalid compute_hash format"

                # 新版 proof_json 结构化校验（兼容旧版证明）。
                structured = self._extract_structured_proof(compute_hash)
                if structured is not None:
                    required_structured = {
                        'task_id', 'task_type', 'input_digest', 'challenge', 'challenge_commitment',
                        'challenge_reveal', 'trace_digest', 'output_digest', 'timestamp_ms', 'proof_hash'
                    }
                    missing_structured = required_structured - set(structured.keys())
                    if missing_structured:
                        return False, f"POUW proof #{i} missing structured fields: {missing_structured}"

                    payload_copy = dict(structured)
                    proof_hash = payload_copy.pop('proof_hash', '')
                    expected_proof_hash = hashlib.sha256(
                        json.dumps(payload_copy, sort_keys=True, separators=(',', ':'), ensure_ascii=False, default=str).encode()
                    ).hexdigest()
                    if proof_hash != expected_proof_hash:
                        return False, f"POUW proof #{i} proof_hash mismatch"

                    if proof_hash in seen_proof_hashes:
                        return False, f"POUW proof #{i} duplicate proof_hash"
                    seen_proof_hashes.add(proof_hash)

                    challenge = structured.get('challenge', '')
                    reveal = structured.get('challenge_reveal', '')
                    commitment = structured.get('challenge_commitment', '')
                    if challenge and reveal and commitment:
                        expected_commitment = hashlib.sha256(f"{challenge}:{reveal}".encode()).hexdigest()
                        if expected_commitment != commitment:
                            return False, f"POUW proof #{i} challenge commitment mismatch"

                    ts_ms = structured.get('timestamp_ms', 0)
                    start_ms = structured.get('window_start_ms', 0)
                    end_ms = structured.get('window_end_ms', 0)
                    if isinstance(ts_ms, int) and isinstance(start_ms, int) and isinstance(end_ms, int):
                        if start_ms > 0 and end_ms > 0 and not (start_ms <= ts_ms <= end_ms + 30000):
                            return False, f"POUW proof #{i} timestamp outside challenge window"

                # 证明必须标记为已验证
                if proof.get('verified') is not True:
                    return False, f"POUW proof #{i} is not verified"

                exec_time = proof.get('execution_time', 0)
                if not isinstance(exec_time, (int, float)) or exec_time <= 0:
                    return False, f"POUW proof #{i} invalid execution_time: {exec_time}"
                
                # D-01 fix: 防止证明重复（同一 task_id 不能出现两次）
                task_id = proof.get('task_id', '')
                if task_id in seen_task_ids:
                    return False, f"POUW proof #{i} duplicate task_id: {task_id}"
                seen_task_ids.add(task_id)
                
                # D-01 fix: 证明必须绑定到出块矿工
                if proof.get('miner_id', '') != block.miner_id:
                    return False, f"POUW proof #{i} miner_id ({proof.get('miner_id', '')}) != block miner ({block.miner_id})"

            if confidence_work_sum < ChainParams.POUW_CONFIDENCE_THRESHOLD:
                return False, (
                    f"POUW confidence-weighted work too low: "
                    f"{confidence_work_sum:.2f} < {ChainParams.POUW_CONFIDENCE_THRESHOLD}"
                )
        elif block.consensus_type == ConsensusType.SBOX_POUW:
            # S-Box PoUW 验证
            if not block.sbox_hex:
                return False, "Missing S-Box data in SBOX_POUW block"
            
            # 验证 S-Box 格式（256 字节 = 512 hex 字符）
            if len(block.sbox_hex) != 512:
                return False, f"Invalid S-Box hex length: {len(block.sbox_hex)}"
            try:
                sbox_bytes = bytes.fromhex(block.sbox_hex)
            except ValueError:
                return False, "Invalid S-Box hex encoding"
            if len(sbox_bytes) != 256:
                return False, "Invalid S-Box size"
            # 双射性检查
            if len(set(sbox_bytes)) != 256:
                return False, "S-Box is not a bijection"
            
            # 验证评分
            try:
                from core.sbox_engine import verify_sbox_submission
                valid_sbox, reason, _metrics = verify_sbox_submission(
                    sbox=list(sbox_bytes),
                    claimed_score=block.sbox_score,
                    weights=block.sbox_score_weights or {},
                    score_threshold=block.sbox_score_threshold or 0.0,
                )
                if not valid_sbox:
                    return False, f"S-Box verification failed: {reason}"
            except ImportError:
                pass  # sbox_engine 不可用时跳过深度验证
            
            # 验证区块 hash 正确性
            recomputed = block.compute_hash()
            if block.hash != recomputed:
                return False, "SBOX_POUW block hash mismatch on recomputation"
            
            # S-Box 区块也需要满足最低 PoW 难度
            if not block.hash.startswith("0"):
                return False, "SBOX_POUW block hash does not meet minimum difficulty"
            
            # 验证 POUW 证明（如果有）
            if block.pouw_proofs:
                confidence_work_sum = 0.0
                for i, proof in enumerate(block.pouw_proofs):
                    if not isinstance(proof, dict):
                        return False, f"SBOX_POUW proof #{i} is not a dict"
                    if proof.get('verified') is not True:
                        return False, f"SBOX_POUW proof #{i} is not verified"
                    work = float(proof.get('work_amount', proof.get('work_score', 0.0)))
                    confidence = float(proof.get('confidence_score', 0.4))
                    confidence_work_sum += max(0.0, work) * max(0.0, min(1.0, confidence))
                if confidence_work_sum < (ChainParams.POUW_CONFIDENCE_THRESHOLD * 0.6):
                    return False, (
                        f"SBOX_POUW confidence-weighted work too low: {confidence_work_sum:.2f}"
                    )
        
        # UTXO 交易验证
        if self.utxo_store and block.transactions:
            valid, msg = self._validate_block_transactions(block)
            if not valid:
                return False, msg
        
        return True, "OK"
    
    def _validate_block_transactions(self, block: Block) -> Tuple[bool, str]:
        """验证区块中所有交易的 UTXO 有效性。"""
        spent_in_block: set = set()  # 块内双花检测
        
        for tx in block.transactions:
            tx_type = tx.get("tx_type", "transfer")
            
            # coinbase 交易无需检查输入
            if tx_type == "coinbase":
                continue
            
            inputs = tx.get("inputs", [])
            outputs = tx.get("outputs", [])
            
            if not inputs:
                continue  # 无输入的交易跳过（兼容旧数据）
            
            total_input = 0.0
            
            for inp in inputs:
                utxo_ref = f"{inp.get('txid', '')}:{inp.get('index', 0)}"
                
                # 块内双花检测
                if utxo_ref in spent_in_block:
                    return False, f"Double spend in block: {utxo_ref}"
                spent_in_block.add(utxo_ref)
                
                # 检查 UTXO 是否存在且未花费
                utxo = self.utxo_store.get_utxo(inp.get('txid', ''), inp.get('index', 0))
                if not utxo:
                    return False, f"UTXO not found: {utxo_ref}"
                if utxo.status.value != 'unspent':
                    return False, f"UTXO already spent: {utxo_ref}"
                
                # D-15 fix: Coinbase 成熟度检查（使用可配置参数）
                if utxo.source_type == 'coinbase':
                    confirmations = block.height - utxo.block_height
                    if confirmations < ChainParams.COINBASE_MATURITY_DEPTH:
                        return False, f"Coinbase not mature: {confirmations}/{ChainParams.COINBASE_MATURITY_DEPTH}"
                else:
                    # 普通交易确认检查
                    confirmations = block.height - utxo.block_height
                    if confirmations < ChainParams.TX_CONFIRMATION_DEPTH:
                        return False, f"TX not confirmed: {confirmations}/{ChainParams.TX_CONFIRMATION_DEPTH}"
                
                total_input += utxo.amount
            
            # 输入 >= 输出 检查
            total_output = sum(o.get('amount', 0) for o in outputs)
            fee = tx.get('fee', 0)
            if total_input < total_output + fee - 0.00000001:
                return False, f"Input ({total_input}) < Output ({total_output}) + Fee ({fee})"
        
        return True, "OK"
    
    def add_block(self, block: Block) -> bool:
        """添加区块到链并持久化。"""
        valid, msg = self.validate_block(block)
        if not valid:
            self.log(f"区块 #{block.height} 无效: {msg}")
            return False
        
        with self._lock:
            block.status = BlockStatus.VALIDATED
            self.chain.append(block)
            self._chain_height = block.height
            # 更新地址 nonce 追踪
            self._update_nonces_from_block(block)
        
        # 持久化到数据库
        self._save_block(block)
        
        # 自动标记已深埋区块为 FINALIZED
        self.finalize_blocks()
        
        with self._lock:
            # 裁剪内存缓存，防止 OOM
            if len(self.chain) > self._max_cache_size:
                self.chain = self.chain[-self._max_cache_size:]
        
        self.log(f"区块 #{block.height} 已添加并保存")
        return True
    
    def add_block_no_validate(self, block: Block) -> bool:
        """添加区块（同步历史区块 — 含安全关键验证）。
        Add block for historical sync — still enforces critical security checks.
        
        验证项 / Checks performed:
        - 高度必须连续 / Height must be sequential
        - prev_hash 必须匹配 / prev_hash must match
        - 区块 hash 必须重算一致 / Block hash must match recomputed hash
        - PoW 区块必须满足难度要求 / PoW blocks must meet difficulty target
        - POUW 区块必须有证明 / POUW blocks must have proofs
        """
        with self._lock:
            # 基本结构验证
            expected_height = self._chain_height + 1
            if block.height != expected_height:
                self.log(f"[SYNC] 区块高度不连续: 期望 {expected_height}, 实际 {block.height}")
                return False
            
            # prev_hash 验证
            if self.chain:
                last_block = self.chain[-1]
                if block.prev_hash != last_block.hash:
                    self.log(f"[SYNC] prev_hash 不匹配: 区块声称 {block.prev_hash[:16]}..., 链尾 {last_block.hash[:16]}...")
                    return False
            
            # 安全加固：hash 重算校验（防止伪造 hash 字段）
            # Security: recompute hash to prevent forged hash field
            if not block.hash or len(block.hash) < 32:
                self.log(f"[SYNC] 区块 hash 无效")
                return False
            
            recomputed_hash = block.compute_hash()
            if block.hash != recomputed_hash:
                self.log(f"[SYNC] 区块 hash 校验失败: 声称 {block.hash[:16]}..., 重算 {recomputed_hash[:16]}...")
                return False
            
            # 安全加固：PoW 难度验证
            # Security: verify PoW difficulty requirement
            if block.consensus_type == ConsensusType.POW:
                if not block.is_valid_pow():
                    self.log(f"[SYNC] 区块 PoW 难度不满足")
                    return False
            elif block.consensus_type == ConsensusType.POUW:
                if not block.pouw_proofs:
                    self.log(f"[SYNC] POUW 区块缺少证明")
                    return False
            elif block.consensus_type == ConsensusType.SBOX_POUW:
                if not block.sbox_hex or len(block.sbox_hex) != 512:
                    self.log(f"[SYNC] SBOX_POUW 区块缺少有效 S-Box 数据")
                    return False
                try:
                    sbox_bytes = bytes.fromhex(block.sbox_hex)
                    if len(set(sbox_bytes)) != 256:
                        self.log(f"[SYNC] S-Box 不是双射")
                        return False
                except ValueError:
                    self.log(f"[SYNC] S-Box hex 编码无效")
                    return False
            
            # 安全加固：Coinbase 奖励金额上限验证（防止伪造膨胀奖励）
            if block.block_reward > 0:
                expected_reward = self.reward_calculator.get_block_reward(block.height)
                useful_bonus = self._calculate_useful_work_bonus(block)
                actual_fees = sum(tx.get("fee", 0) for tx in (block.transactions or [])
                                 if tx.get("tx_type", "transfer") != "coinbase")
                max_allowed = expected_reward + useful_bonus + actual_fees + 0.00000001
                if block.block_reward > max_allowed:
                    self.log(f"[SYNC] Coinbase 奖励超限: {block.block_reward} > max {max_allowed}")
                    return False
            
            # 安全加固：Merkle Root 验证
            if hasattr(block, 'compute_merkle_root'):
                expected_merkle = block.compute_merkle_root()
                if block.merkle_root and expected_merkle and block.merkle_root != expected_merkle:
                    self.log(f"[SYNC] Merkle root 不匹配")
                    return False
            
            # 安全加固：时间戳合理性（不超过未来 2 小时）
            import time as _time
            if block.timestamp > _time.time() + 7200:
                self.log(f"[SYNC] 区块时间戳过于超前")
                return False
            
            block.status = BlockStatus.VALIDATED
            self.chain.append(block)
            self._chain_height = block.height
        
        self._save_block(block)
        
        with self._lock:
            if len(self.chain) > self._max_cache_size:
                self.chain = self.chain[-self._max_cache_size:]
        
        return True
    
    def receive_block_from_peer(self, block_dict: Dict) -> Tuple[bool, str]:
        """接收并验证来自 P2P 网络的区块。"""
        try:
            block = self._dict_to_block(block_dict)
        except Exception as e:
            return False, f"Block deserialization failed: {e}"
        
        expected = self.get_chain_height() + 1
        if block.height < expected:
            return False, f"Block too old: #{block.height}"
        if block.height > expected:
            # 分叉检测：对端高度超前，可能需要同步或重组
            # Fork detected: peer is ahead, may need sync or reorg
            return False, f"Block too new: #{block.height}, need #{expected} (sync needed, use handle_fork_detection)"
        
        # 检查是否已有此区块
        existing = self.get_block_by_hash(block.hash)
        if existing:
            return False, "Block already exists"
        
        # 中断当前挖矿（别的矿工已经挖出了）
        self.stop_mining = True
        
        success = self.add_block(block)
        if success:
            # 通过难度调节器计算新难度，不盲目采用对端区块的 difficulty
            self.current_difficulty = self.difficulty_adjuster.calculate_new_difficulty(
                self.current_difficulty
            )
            
            # S-Box PoUW 区块：更新本地 S-Box 库
            if block.consensus_type == ConsensusType.SBOX_POUW and block.sbox_hex:
                try:
                    from core.sbox_engine import get_sbox_library, BlockSBox, sbox_hash, hex_to_sbox
                    sbox_lib = get_sbox_library()
                    sbox_data = hex_to_sbox(block.sbox_hex)
                    block_sbox = BlockSBox(
                        sbox=sbox_data,
                        sbox_hash=sbox_hash(sbox_data),
                        score=block.sbox_score,
                        nonlinearity=block.sbox_nonlinearity,
                        diff_uniformity=block.sbox_diff_uniformity,
                        avalanche=block.sbox_avalanche,
                        weights=block.sbox_score_weights or {},
                        miner_id=block.miner_id,
                        sector=block.sbox_selected_sector or block.sector,
                    )
                    sbox_lib.set_current(block_sbox)
                except Exception:
                    pass  # 非致命
            
            # 恢复挖矿
            self.stop_mining = False
            return True, f"Block #{block.height} accepted"
        
        self.stop_mining = False
        return False, f"Block #{block.height} rejected"
    
    def get_blocks_range(self, start_height: int, end_height: int = None, max_count: int = 50) -> List[Dict]:
        """获取指定高度范围的区块（用于同步）。"""
        if end_height is None:
            end_height = start_height + max_count
        end_height = min(end_height, start_height + max_count, self.get_chain_height() + 1)
        
        blocks = []
        for h in range(start_height, end_height):
            block = self.get_block_by_height(h)
            if block:
                blocks.append(self._block_to_dict(block))
        return blocks
    
    def start_mining(self, miner_address: str, on_block: Callable = None):
        """开始持续挖矿。"""
        self.is_mining = True
        self.stop_mining = False
        self._last_block_time = time.time()
        
        def mining_loop():
            uptime_accumulator = 0.0
            last_uptime_record = time.time()
            
            while not self.stop_mining:
                block = self.mine_block(miner_address)
                if block:
                    added = self.add_block(block)
                    if added and on_block:
                        on_block(block)
                
                # 定期记录在线时间 (每 300 秒)
                now = time.time()
                uptime_accumulator += now - last_uptime_record
                last_uptime_record = now
                if uptime_accumulator >= 300:
                    try:
                        hours = uptime_accumulator / 3600
                        self.metrics_collector.record_uptime(self.node_id, hours)
                    except Exception:
                        pass
                    uptime_accumulator = 0.0
                
                time.sleep(1)  # 避免 CPU 过载
            
            self.is_mining = False
        
        thread = threading.Thread(target=mining_loop, daemon=True)
        thread.start()
        
        sbox_status = "✅ S-Box PoUW 已启用" if self._sbox_mining_enabled else "❌ S-Box PoUW 未启用"
        self.log(
            f"⛏️ 挖矿线程已启动 (区块类型: task/idle/validation, mode={self.consensus_mode}, "
            f"sbox_ratio={self.consensus_sbox_ratio:.2f}, "
            f"pouw_support_ratio={self.consensus_pouw_support_ratio:.2f}) | {sbox_status}"
        )
    
    def stop(self):
        """停止挖矿。"""
        self.stop_mining = True
        self.log("🛑 挖矿已停止")
    
    def get_chain_info(self) -> Dict:
        """获取链信息。"""
        info = {
            "height": self.get_chain_height(),
            "difficulty": self.current_difficulty,
            "consensus": self.current_consensus.value,
            "consensus_mode": self.consensus_mode,
            "consensus_sbox_ratio": self.consensus_sbox_ratio,
            "consensus_pouw_support_ratio": self.consensus_pouw_support_ratio,
            "pending_txs": len(self.pending_transactions),
            "pending_pouw": len(self.pending_pouw),
            "total_blocks_mined": self.total_blocks_mined,
            "total_pouw_proofs": self.total_pouw_proofs,
            "latest_block": self.get_latest_block().to_dict() if self.chain else None,
            "sbox_mining_enabled": self._sbox_mining_enabled,
            "consensus_selected_distribution": self._build_consensus_distribution(
                list(self._recent_consensus_selected)
            ),
            "consensus_mined_distribution": self._build_consensus_distribution(
                list(self._recent_consensus_mined)
            ),
            "mechanism_strategy": self.get_mechanism_strategy(),
        }
        # S-Box 挖矿信息
        if self._sbox_miner:
            info["sbox_stats"] = self._sbox_miner.global_adjuster.get_stats()
        try:
            from core.sbox_engine import get_sbox_library
            sbox_lib = get_sbox_library()
            current = sbox_lib.current
            if current:
                info["current_sbox"] = {
                    "score": round(current.score, 4),
                    "sector": current.sector,
                    "nonlinearity": current.nonlinearity,
                }
            info["sbox_library_size"] = sbox_lib.size()
        except ImportError:
            pass
        return info
    
    def get_block_by_height(self, height: int) -> Optional[Block]:
        """按高度获取区块（优先缓存，回退 DB）。D-19 fix: 线程安全。"""
        # 先查缓存
        with self._lock:
            for block in self.chain:
                if block.height == height:
                    return block
        
        # 回退到数据库
        try:
            with self._db_lock:
                row = self._db_conn.execute(
                    "SELECT block_data FROM blocks WHERE height = ? AND sector = ?",
                    (height, self.sector)
                ).fetchone()
            if row:
                block_dict = json.loads(row['block_data'])
                return self._dict_to_block(block_dict)
        except Exception:
            pass
        return None
    
    def get_block_by_hash(self, block_hash: str) -> Optional[Block]:
        """按哈希获取区块（优先缓存，回退 DB）。线程安全。"""
        # 先查缓存（需要加锁防止并发修改）
        with self._lock:
            for block in self.chain:
                if block.hash == block_hash:
                    return block
        
        # 回退到数据库
        try:
            with self._db_lock:
                row = self._db_conn.execute(
                    "SELECT block_data FROM blocks WHERE hash = ?",
                    (block_hash,)
                ).fetchone()
            if row:
                block_dict = json.loads(row['block_data'])
                return self._dict_to_block(block_dict)
        except Exception:
            pass
        return None

    # ============== 区块确认与终局性 / Block Finality ==============
    
    # 确认阈值常量（与 UTXO 花费检查保持一致）
    COINBASE_MATURITY = 100      # coinbase 交易成熟度
    STANDARD_CONFIRMATIONS = 6   # 标准交易最终确认数
    EXCHANGE_CONFIRMATIONS = 20  # 交易所级别确认数
    FINALITY_THRESHOLD = 20      # 区块终局性阈值
    
    def get_confirmations(self, block_height: int) -> int:
        """获取指定高度区块的确认数。"""
        current = self.get_chain_height()
        if block_height < 0 or block_height > current:
            return 0
        return current - block_height
    
    def is_finalized(self, block_height: int) -> bool:
        """判断指定高度的区块是否已达到终局性（不可回滚）。
        
        终局性意味着该区块不会因为分叉被回滚。
        """
        return self.get_confirmations(block_height) >= self.FINALITY_THRESHOLD

    def get_finalized_height(self) -> int:
        """Return the highest height that has reached finality.

        Finality is depth-based in the current implementation. A block is
        finalized once it is buried by ``FINALITY_THRESHOLD`` later blocks.
        Returns 0 for short chains so callers can safely display genesis as the
        only finalized anchor until the threshold is reached.
        """
        current = self.get_chain_height()
        if current <= 0:
            return 0
        return max(0, current - self.FINALITY_THRESHOLD)
    
    def is_tx_confirmed(self, block_height: int, is_coinbase: bool = False) -> bool:
        """判断交易是否已达到足够确认数。
        
        Args:
            block_height: 交易所在区块高度
            is_coinbase: 是否为 coinbase 交易（需要更多确认）
        """
        confirmations = self.get_confirmations(block_height)
        if is_coinbase:
            return confirmations >= self.COINBASE_MATURITY
        return confirmations >= self.STANDARD_CONFIRMATIONS
    
    def finalize_blocks(self):
        """将超过终局阈值的区块标记为 FINALIZED。
        
        可由定期任务调用（如每出块时一次），保证已深埋的区块不再参与分叉。
        """
        current = self.get_chain_height()
        cutoff = current - self.FINALITY_THRESHOLD
        if cutoff < 0:
            return
        
        try:
            with self._db_lock:
                self._db_conn.execute(
                    "UPDATE blocks SET status = ? WHERE height <= ? AND sector = ? AND status != ?",
                    (BlockStatus.FINALIZED.value, cutoff, self.sector, BlockStatus.FINALIZED.value)
                )
                self._db_conn.commit()
        except Exception:
            pass
    
    # ============== 分叉解决机制 / Fork Resolution ==============
    
    def get_cumulative_difficulty(self, up_to_height: int = None) -> float:
        """计算累计难度（链的总工作量）。
        Compute cumulative difficulty (total work) of the chain.
        
        累计难度 = sum(2^difficulty) 对每个区块。
        使用 2^difficulty 而非 difficulty 本身，因为难度每增加 1，
        工作量翻倍。这确保最长链选择基于实际工作量。
        """
        if up_to_height is None:
            up_to_height = self.get_chain_height()
        
        total_work = 0.0
        
        # 从数据库计算（覆盖完整历史）
        try:
            rows = self._db_conn.execute(
                "SELECT block_data FROM blocks WHERE sector = ? AND height <= ? ORDER BY height ASC",
                (self.sector, up_to_height)
            ).fetchall()
            
            for row in rows:
                block_dict = json.loads(row['block_data'])
                difficulty = block_dict.get('difficulty', 1)
                total_work += 2 ** difficulty
        except Exception as e:
            self.log(f"[FORK] 计算累计难度失败: {e}")
            # 回退：使用缓存中的区块
            for block in self.chain:
                if block.height <= up_to_height:
                    total_work += 2 ** block.difficulty
        
        return total_work
    
    def find_fork_point(self, peer_blocks: List[Dict]) -> int:
        """找到与对端链的分叉点。
        Find the fork point with a peer's chain.
        
        比较对端区块的 prev_hash 与本地区块的 hash，
        找到最后一个匹配的高度即为分叉点。
        
        Args:
            peer_blocks: 对端区块列表，按高度升序排列
            
        Returns:
            分叉点高度，-1 表示无共同祖先
        """
        if not peer_blocks:
            return -1
        
        for pb in peer_blocks:
            height = pb.get('height', 0)
            local_block = self.get_block_by_height(height)
            if local_block and local_block.hash == pb.get('hash', ''):
                continue  # 此高度区块相同
            elif height > 0:
                # 检查 prev_hash 是否匹配
                prev_local = self.get_block_by_height(height - 1)
                if prev_local and prev_local.hash == pb.get('prev_hash', ''):
                    return height - 1  # 分叉点在上一个区块
        
        # 如果第一个对端区块的 prev_hash 匹配本地区块
        first_peer = peer_blocks[0]
        first_height = first_peer.get('height', 0)
        if first_height > 0:
            prev_local = self.get_block_by_height(first_height - 1)
            if prev_local and prev_local.hash == first_peer.get('prev_hash', ''):
                return first_height - 1
        
        # 创世区块相同则返回 0
        local_genesis = self.get_block_by_height(0)
        if local_genesis and first_height == 0 and local_genesis.hash == first_peer.get('hash', ''):
            return 0
        
        return -1
    
    def evaluate_fork(self, peer_blocks: List[Dict]) -> Dict:
        """评估是否应该切换到对端的分叉链。
        Evaluate whether we should switch to a peer's fork.
        
        决策标准（按优先级）：
        1. 累计难度更高的链胜出（最重链规则）
        2. 同等难度时，保持当前链（stability preference）
        3. 分叉深度不能超过最大重组深度（安全限制）
        
        Args:
            peer_blocks: 从分叉点开始的对端区块列表
            
        Returns:
            Dict with: should_reorg, fork_point, local_work, peer_work, reason
        """
        MAX_REORG_DEPTH = 100  # 最大重组深度，防止深度重组攻击
        
        if not peer_blocks:
            return {
                "should_reorg": False,
                "reason": "No peer blocks provided"
            }
        
        # 找分叉点
        fork_point = self.find_fork_point(peer_blocks)
        if fork_point < 0:
            return {
                "should_reorg": False,
                "fork_point": -1,
                "reason": "No common ancestor found / 无共同祖先"
            }
        
        # 检查重组深度
        reorg_depth = self.get_chain_height() - fork_point
        if reorg_depth > MAX_REORG_DEPTH:
            return {
                "should_reorg": False,
                "fork_point": fork_point,
                "reorg_depth": reorg_depth,
                "reason": f"Reorg too deep: {reorg_depth} > {MAX_REORG_DEPTH} / 重组深度超限"
            }
        
        # 计算本地链从分叉点之后的累计难度
        local_work_after_fork = 0.0
        for h in range(fork_point + 1, self.get_chain_height() + 1):
            block = self.get_block_by_height(h)
            if block:
                local_work_after_fork += 2 ** block.difficulty
        
        # 计算对端链的累计难度
        peer_work_after_fork = 0.0
        for pb in peer_blocks:
            if pb.get('height', 0) > fork_point:
                peer_work_after_fork += 2 ** pb.get('difficulty', 1)
        
        should_reorg = peer_work_after_fork > local_work_after_fork
        
        return {
            "should_reorg": should_reorg,
            "fork_point": fork_point,
            "reorg_depth": reorg_depth,
            "local_work": local_work_after_fork,
            "peer_work": peer_work_after_fork,
            "local_height": self.get_chain_height(),
            "peer_height": max((pb.get('height', 0) for pb in peer_blocks), default=0),
            "reason": "Peer has more cumulative work / 对端链工作量更大" if should_reorg 
                       else "Local chain has equal or greater work / 本地链工作量不低于对端"
        }
    
    def perform_reorg(self, peer_blocks: List[Dict]) -> Tuple[bool, str]:
        """执行链重组（分叉切换）。
        Perform chain reorganization (fork switch).
        
        步骤：
        1. 验证所有对端区块的有效性（hash、PoW/POUW）
        2. 找到分叉点
        3. 回滚本地链到分叉点
        4. 逐个应用对端区块
        5. 如果失败，回滚到原始状态
        
        Args:
            peer_blocks: 从分叉点+1开始的对端区块列表，按高度升序
            
        Returns:
            (success, message)
        """
        with self._lock:
            # Step 1: 评估是否应该重组
            eval_result = self.evaluate_fork(peer_blocks)
            if not eval_result.get("should_reorg", False):
                return False, f"Reorg rejected: {eval_result.get('reason', 'unknown')}"
            
            fork_point = eval_result["fork_point"]
            current_height = self.get_chain_height()
            
            # 终局性保护：不允许回滚已终局化的区块
            if self.is_finalized(fork_point):
                return False, f"Reorg rejected: fork point {fork_point} is finalized (depth >= {self.FINALITY_THRESHOLD})"
            
            self.log(f"[FORK] 开始链重组: fork_point={fork_point}, "
                     f"本地高度={current_height}, 对端高度={eval_result.get('peer_height', 0)}")
            
            # Step 2: 备份被回滚的区块（以防重组失败需要恢复）
            rolled_back_blocks = []
            for h in range(fork_point + 1, current_height + 1):
                block = self.get_block_by_height(h)
                if block:
                    rolled_back_blocks.append(self._block_to_dict(block))
            
            # Step 3: 验证所有对端区块的基本有效性
            for pb_dict in peer_blocks:
                if pb_dict.get('height', 0) <= fork_point:
                    continue  # 分叉点及之前的区块跳过
                try:
                    pb = self._dict_to_block(pb_dict)
                    # 验证 hash 重算
                    recomputed = pb.compute_hash()
                    if pb.hash != recomputed:
                        return False, f"Peer block #{pb.height} hash mismatch / 对端区块 hash 不匹配"
                    # 验证 PoW/POUW
                    if pb.consensus_type == ConsensusType.POW:
                        if not pb.is_valid_pow():
                            return False, f"Peer block #{pb.height} invalid PoW / 对端区块 PoW 无效"
                    elif pb.consensus_type == ConsensusType.POUW:
                        if not pb.pouw_proofs:
                            return False, f"Peer block #{pb.height} missing POUW proofs / 对端区块缺少 POUW 证明"
                except Exception as e:
                    return False, f"Peer block validation failed: {e}"
            
            # Step 4: 从数据库和缓存中回滚到分叉点
            try:
                self._db_conn.execute(
                    "DELETE FROM blocks WHERE sector = ? AND height > ?",
                    (self.sector, fork_point)
                )
                self._db_conn.commit()
                
                # 回滚 UTXO 状态（恢复被花费的 UTXO、删除新产生的 UTXO）
                if self.utxo_store and hasattr(self.utxo_store, 'rollback_to_height'):
                    try:
                        utxo_rolled = self.utxo_store.rollback_to_height(fork_point)
                        self.log(f"[FORK] UTXO 回滚: {utxo_rolled} 条记录")
                    except Exception as ue:
                        self.log(f"[FORK] UTXO 回滚失败: {ue}")
                
                # 回滚板块币余额（撤销铸造/转账/销毁）
                if self.sector_ledger and hasattr(self.sector_ledger, 'rollback_to_height'):
                    try:
                        sector_rolled = self.sector_ledger.rollback_to_height(self.sector, fork_point)
                        self.log(f"[FORK] 板块币回滚: {sector_rolled} 条记录")
                    except Exception as se:
                        self.log(f"[FORK] 板块币回滚失败: {se}")
                
                # D-07 fix: 回滚国库（撤销回滚区块中的国库收入）
                if hasattr(self, 'treasury_manager') and self.treasury_manager:
                    try:
                        if hasattr(self.treasury_manager, 'rollback_to_height'):
                            treasury_rolled = self.treasury_manager.rollback_to_height(fork_point)
                            self.log(f"[FORK] 国库回滚: {treasury_rolled} 条记录")
                    except Exception as te:
                        self.log(f"[FORK] 国库回滚失败: {te}")
                
                # D-07 fix: 回滚 DAO 提案状态（撤销回滚区块中执行的提案）
                if hasattr(self, 'dao_manager') and self.dao_manager:
                    try:
                        if hasattr(self.dao_manager, 'rollback_to_height'):
                            dao_rolled = self.dao_manager.rollback_to_height(fork_point)
                            self.log(f"[FORK] DAO 回滚: {dao_rolled} 条记录")
                    except Exception as de:
                        self.log(f"[FORK] DAO 回滚失败: {de}")
                
                # 回滚 BlockStore (chain.db) 中的同步数据
                if self.block_store and hasattr(self.block_store, '_conn'):
                    try:
                        self.block_store._conn.execute(
                            "DELETE FROM blocks WHERE height > ?", (fork_point,)
                        )
                        self.block_store._conn.commit()
                    except Exception:
                        pass
                
                # 重建内存缓存
                self.chain = [b for b in self.chain if b.height <= fork_point]
                self._chain_height = fork_point
                
            except Exception as e:
                self.log(f"[FORK] 回滚失败: {e}")
                return False, f"Rollback failed: {e}"
            
            # Step 5: 逐个应用对端区块
            applied = 0
            for pb_dict in sorted(peer_blocks, key=lambda x: x.get('height', 0)):
                if pb_dict.get('height', 0) <= fork_point:
                    continue
                
                try:
                    pb = self._dict_to_block(pb_dict)
                    if not self.add_block_no_validate(pb):
                        raise Exception(f"Block #{pb.height} rejected by add_block_no_validate")
                    applied += 1
                except Exception as e:
                    # 应用失败 — 回滚到原始状态
                    self.log(f"[FORK] 应用对端区块失败: {e}，正在恢复...")
                    try:
                        self._db_conn.execute(
                            "DELETE FROM blocks WHERE sector = ? AND height > ?",
                            (self.sector, fork_point)
                        )
                        self._db_conn.commit()
                        self.chain = [b for b in self.chain if b.height <= fork_point]
                        self._chain_height = fork_point
                        
                        # 恢复原始区块
                        for rb_dict in rolled_back_blocks:
                            rb = self._dict_to_block(rb_dict)
                            self.add_block_no_validate(rb)
                    except Exception as restore_err:
                        self.log(f"[FORK] 恢复失败（严重错误）: {restore_err}")
                    
                    return False, f"Reorg failed at block #{pb_dict.get('height', 0)}: {e}"
            
            self.log(f"[FORK] 链重组完成: 回滚 {len(rolled_back_blocks)} 个区块, "
                     f"应用 {applied} 个新区块, 新高度 #{self.get_chain_height()}")
            
            return True, f"Reorg success: {applied} blocks applied, new height #{self.get_chain_height()}"
    
    def handle_fork_detection(self, peer_height: int, peer_blocks_provider) -> Tuple[bool, str]:
        """处理分叉检测（当对端宣称更高高度时调用）。
        Handle fork detection when a peer claims higher height.
        
        这是 receive_block_from_peer 中 "sync needed" 情况的完整处理。
        
        Args:
            peer_height: 对端宣称的链高度
            peer_blocks_provider: 获取对端区块的回调函数
                                  签名: (start_height, count) -> List[Dict]
                                  
        Returns:
            (success, message)
        """
        local_height = self.get_chain_height()
        
        if peer_height <= local_height:
            return False, "Peer height not greater than local / 对端高度不高于本地"
        
        # 获取对端区块（从本地高度往回多取一些以找分叉点）
        fetch_start = max(0, local_height - 10)
        try:
            peer_blocks = peer_blocks_provider(fetch_start, peer_height - fetch_start + 1)
        except Exception as e:
            return False, f"Failed to fetch peer blocks: {e}"
        
        if not peer_blocks:
            return False, "Peer returned no blocks / 对端未返回区块"
        
        # 评估分叉
        eval_result = self.evaluate_fork(peer_blocks)
        
        if eval_result.get("should_reorg", False):
            self.log(f"[FORK] 检测到更强分叉: 对端工作量={eval_result['peer_work']:.0f}, "
                     f"本地工作量={eval_result['local_work']:.0f}")
            return self.perform_reorg(peer_blocks)
        else:
            # 对端链不比本地强，但可能是简单的高度追赶（非分叉）
            # 检查是否为简单的链延长（对端链是本地链的延续）
            fork_point = eval_result.get("fork_point", -1)
            if fork_point == local_height:
                # 不是分叉，而是对端有我们没有的新区块
                applied = 0
                for pb_dict in sorted(peer_blocks, key=lambda x: x.get('height', 0)):
                    if pb_dict.get('height', 0) <= local_height:
                        continue
                    try:
                        pb = self._dict_to_block(pb_dict)
                        if self.add_block(pb):
                            applied += 1
                        else:
                            break
                    except Exception:
                        break
                
                if applied > 0:
                    return True, f"Chain extended by {applied} blocks / 链延长 {applied} 个区块"
            
            return False, f"No reorg needed: {eval_result.get('reason', 'unknown')}"


class SectorConsensus(ConsensusEngine):
    """板块共识引擎。
    
    继承基础共识，添加板块特定逻辑。
    """
    
    def __init__(
        self,
        node_id: str,
        sector: str,
        main_chain_ref: Optional["ConsensusEngine"] = None,
        log_fn: Callable = print,
    ):
        super().__init__(node_id, sector, log_fn)
        self.main_chain_ref = main_chain_ref
        
        # 板块奖励流向 MAIN 的比例
        self.main_pool_rate = ChainParams.SECTOR_TO_MAIN_RATE
    
    def mine_block(self, miner_address: str) -> Optional[Block]:
        """板块挖矿，部分奖励流向 MAIN。"""
        block = super().mine_block(miner_address)
        
        if block:
            # 计算流向 MAIN 的部分
            main_share = block.block_reward * self.main_pool_rate
            block.extra_data = f"main_pool:{main_share:.4f}"
            
            self.log(f"💰 {main_share:.4f} {self.sector} -> MAIN 池")
        
        return block


class MultiSectorCoordinator:
    """多板块协调器。
    
    核心功能：
    1. 统一出块时间目标（每板块独立难度调整）
    2. 跨板块区块同步
    3. 难度基准协调
    
    设计原理：
    - 每个板块有独立的难度和共识引擎
    - 各板块独立调整难度以达到目标出块时间
    - 高算力板块 → 高难度
    - 低算力板块 → 低难度
    - 结果：所有板块都趋向于 30 秒出块
    """
    
    def __init__(self, log_fn: Callable = print):
        self.log = log_fn
        
        # 板块引擎
        self.sector_engines: Dict[str, ConsensusEngine] = {}
        
        # 全局参数
        self.target_block_time = ChainParams.TARGET_BLOCK_TIME
        
        # 板块统计
        self.sector_stats: Dict[str, Dict] = {}
        
        # 同步状态
        self.is_running = False
        self.sync_thread = None
    
    def register_sector(self, sector_id: str, engine: ConsensusEngine):
        """注册板块引擎。"""
        self.sector_engines[sector_id] = engine
        self.sector_stats[sector_id] = {
            "blocks": 0,
            "avg_block_time": 0.0,
            "difficulty": engine.current_difficulty,
            "last_block_time": time.time(),
            "hashrate_estimate": 0.0,
        }
        self.log(f"📊 注册板块: {sector_id}")
    
    def update_sector_stats(self, sector_id: str, block_time: float):
        """更新板块统计。"""
        if sector_id not in self.sector_stats:
            return
        
        stats = self.sector_stats[sector_id]
        stats["blocks"] += 1
        
        # 计算移动平均
        alpha = 0.2  # 平滑因子
        stats["avg_block_time"] = (
            alpha * block_time + (1 - alpha) * stats["avg_block_time"]
            if stats["avg_block_time"] > 0 else block_time
        )
        stats["last_block_time"] = time.time()
        
        # 估算算力
        engine = self.sector_engines.get(sector_id)
        if engine:
            stats["difficulty"] = engine.current_difficulty
            # 算力 ≈ 2^difficulty / block_time
            if block_time > 0:
                stats["hashrate_estimate"] = (2 ** stats["difficulty"]) / block_time
    
    def get_sector_performance(self, sector_id: str) -> Dict:
        """获取板块性能。"""
        if sector_id not in self.sector_stats:
            return {}
        
        stats = self.sector_stats[sector_id]
        target = self.target_block_time
        avg = stats["avg_block_time"]
        
        return {
            "sector_id": sector_id,
            "avg_block_time": avg,
            "target_block_time": target,
            "deviation": ((avg - target) / target * 100) if avg > 0 else 0,
            "difficulty": stats["difficulty"],
            "total_blocks": stats["blocks"],
            "hashrate": stats["hashrate_estimate"],
            "status": "OPTIMAL" if abs(avg - target) < 5 else ("FAST" if avg < target else "SLOW"),
        }
    
    def get_all_performance(self) -> List[Dict]:
        """获取所有板块性能。"""
        return [self.get_sector_performance(s) for s in self.sector_engines.keys()]
    
    def get_global_stats(self) -> Dict:
        """获取全局统计。"""
        if not self.sector_engines:
            return {}
        
        all_stats = self.get_all_performance()
        
        total_blocks = sum(s["total_blocks"] for s in all_stats)
        total_hashrate = sum(s["hashrate"] for s in all_stats)
        avg_deviation = sum(abs(s["deviation"]) for s in all_stats) / len(all_stats) if all_stats else 0
        
        return {
            "sector_count": len(self.sector_engines),
            "total_blocks": total_blocks,
            "total_hashrate": total_hashrate,
            "avg_deviation": avg_deviation,
            "target_block_time": self.target_block_time,
            "sectors": all_stats,
        }
    
    def sync_difficulty_hint(self, sector_id: str) -> Optional[int]:
        """提供难度调整建议。
        
        根据历史数据建议新板块的初始难度。
        """
        if not self.sector_stats:
            return ChainParams.INITIAL_DIFFICULTY
        
        # 找相似算力的板块作为参考
        similar_sectors = [
            (sid, stats) for sid, stats in self.sector_stats.items()
            if stats["blocks"] >= 10 and sid != sector_id
        ]
        
        if not similar_sectors:
            return ChainParams.INITIAL_DIFFICULTY
        
        # 使用中位数难度
        difficulties = sorted([s["difficulty"] for _, s in similar_sectors])
        median_idx = len(difficulties) // 2
        
        return difficulties[median_idx]


# ============== 快捷创建函数 ==============
def create_consensus_engine(
    node_id: str,
    sector: str = "MAIN",
    log_fn: Callable = print,
) -> ConsensusEngine:
    """创建共识引擎。"""
    if sector == "MAIN":
        return ConsensusEngine(node_id, sector, log_fn)
    else:
        return SectorConsensus(node_id, sector, log_fn=log_fn)


def create_coordinator(log_fn: Callable = print) -> MultiSectorCoordinator:
    """创建多板块协调器。"""
    return MultiSectorCoordinator(log_fn)
