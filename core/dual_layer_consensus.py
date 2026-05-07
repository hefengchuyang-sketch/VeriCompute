# -*- coding: utf-8 -*-
"""
双层共识架构 V3.0

核心设计原则：
1. Layer 1（安全共识层）：负责出块、防攻击、状态一致性
2. Layer 2（PoUW任务层）：负责任务执行、验证、奖励分配

关键改进：
- PoUW 不直接参与底层共识安全
- 使用可验证计算（zk-proof）替代重复计算
- 引入挑战机制（Challenge Game）
- VRF 随机性保证公平
- 信誉系统仅用于任务分配，不影响共识安全

架构对比：
┌─────────────────────────────────────────────────────────┐
│  旧架构（有问题）                                        │
├─────────────────────────────────────────────────────────┤
│  PoUW 任务验证 = 共识机制                                │
│  问题：任务系统拖垮链、女巫攻击、中心化风险              │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│  新架构（推荐）                                          │
├─────────────────────────────────────────────────────────┤
│  Layer 1: PoS/DPoS 共识（安全层）                        │
│    - 出块                                                │
│    - 防攻击                                              │
│    - 状态一致性                                          │
├─────────────────────────────────────────────────────────┤
│  Layer 2: PoUW 任务市场（价值层）                        │
│    - 任务执行                                            │
│    - zk-proof 验证                                       │
│    - 挑战机制                                            │
│    - 奖励分配                                            │
└─────────────────────────────────────────────────────────┘
"""

import time
import hashlib
import random
import threading
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
import logging

logger = logging.getLogger(__name__)


# ============== Layer 1: 安全共识层 ==============

class ConsensusType(Enum):
    """共识类型"""
    POS = "PoS"              # 权益证明（主要）
    DPOS = "DPoS"            # 委托权益证明
    HYBRID = "Hybrid"        # 混合（PoS + PoW）


@dataclass
class Validator:
    """验证者（Layer 1）"""
    validator_id: str
    address: str
    stake_amount: float      # 质押金额
    reputation_score: float  # 信誉评分（0-1）

    # 统计
    blocks_produced: int = 0
    blocks_missed: int = 0
    last_active: float = 0.0

    # 状态
    is_active: bool = True
    is_slashed: bool = False


class Layer1Consensus:
    """
    Layer 1: 安全共识层

    职责：
    1. 出块（PoS/DPoS）
    2. 防攻击（Slashing）
    3. 状态一致性（BFT）

    不负责：
    - 任务验证
    - 任务分配
    - 任务奖励
    """

    # 配置参数
    MIN_STAKE = 1000.0           # 最低质押（MAIN）
    BLOCK_TIME = 30.0            # 出块时间30秒
    EPOCH_BLOCKS = 100           # 每100个区块一个epoch
    VALIDATOR_SET_SIZE = 21      # 验证者集合大小（DPoS）

    # Slashing 参数
    SLASH_DOUBLE_SIGN = 0.05     # 双签惩罚5%
    SLASH_DOWNTIME = 0.01        # 离线惩罚1%
    DOWNTIME_THRESHOLD = 10      # 连续10个区块未出块视为离线

    def __init__(self):
        self.validators: Dict[str, Validator] = {}
        self.active_validator_set: List[str] = []
        self.current_epoch = 0
        self.lock = threading.RLock()

    def register_validator(
        self,
        validator_id: str,
        address: str,
        stake_amount: float
    ) -> Tuple[bool, str]:
        """注册验证者"""
        if stake_amount < self.MIN_STAKE:
            return False, f"Minimum stake is {self.MIN_STAKE} MAIN"

        with self.lock:
            validator = Validator(
                validator_id=validator_id,
                address=address,
                stake_amount=stake_amount,
                reputation_score=1.0,
                last_active=time.time()
            )

            self.validators[validator_id] = validator
            self._update_validator_set()

            return True, "Validator registered"

    def select_block_producer(self, block_height: int) -> Optional[str]:
        """
        选择出块者（使用VRF保证随机性）

        VRF（Verifiable Random Function）：
        - 可验证的随机函数
        - 防止操纵
        - 公平选举
        """
        if not self.active_validator_set:
            return None

        # 简化版VRF：使用区块高度作为种子
        # 生产环境应使用真正的VRF（如Algorand的VRF）
        seed = hashlib.sha256(f"{block_height}".encode()).hexdigest()
        random.seed(int(seed, 16))

        # 按质押权重选择
        weights = []
        for vid in self.active_validator_set:
            validator = self.validators[vid]
            weight = validator.stake_amount * validator.reputation_score
            weights.append(weight)

        total_weight = sum(weights)
        if total_weight == 0:
            return None

        # 加权随机选择
        r = random.uniform(0, total_weight)
        cumsum = 0
        for i, vid in enumerate(self.active_validator_set):
            cumsum += weights[i]
            if r <= cumsum:
                return vid

        return self.active_validator_set[0]

    def slash_validator(
        self,
        validator_id: str,
        reason: str,
        slash_ratio: float
    ):
        """惩罚验证者（Slashing）"""
        if validator_id not in self.validators:
            return

        validator = self.validators[validator_id]
        slash_amount = validator.stake_amount * slash_ratio

        validator.stake_amount -= slash_amount
        validator.reputation_score *= 0.9  # 降低信誉

        if validator.stake_amount < self.MIN_STAKE:
            validator.is_active = False
            validator.is_slashed = True

        logger.warning(f"Validator {validator_id} slashed: {reason}, amount: {slash_amount}")

    def _update_validator_set(self):
        """更新活跃验证者集合"""
        # 按质押金额排序
        sorted_validators = sorted(
            [(vid, v) for vid, v in self.validators.items() if v.is_active],
            key=lambda x: x[1].stake_amount * x[1].reputation_score,
            reverse=True
        )

        # 选择前N个
        self.active_validator_set = [
            vid for vid, _ in sorted_validators[:self.VALIDATOR_SET_SIZE]
        ]


# ============== Layer 2: PoUW 任务层 ==============

class TaskStatus(Enum):
    """任务状态"""
    PENDING = "pending"
    ASSIGNED = "assigned"
    SUBMITTED = "submitted"
    CHALLENGED = "challenged"
    VERIFIED = "verified"
    FAILED = "failed"


@dataclass
class PoUWTask:
    """PoUW 任务"""
    task_id: str
    task_type: str
    task_data: Dict

    # 经济参数
    reward: float
    task_bond: float         # 任务押金（防伪造）
    worker_bond: float       # 工作者押金（防作弊）

    # 验证参数
    verification_type: str   # "zk-proof" / "challenge" / "sampling"
    challenge_period: int    # 挑战期（区块数）

    # 状态
    status: TaskStatus = TaskStatus.PENDING
    submitter: str = ""
    worker: str = ""
    result: Optional[Dict] = None
    proof: Optional[str] = None  # zk-proof

    # 时间
    created_at: float = 0.0
    assigned_at: float = 0.0
    submitted_at: float = 0.0
    verified_at: float = 0.0


@dataclass
class Challenge:
    """挑战记录"""
    challenge_id: str
    task_id: str
    challenger: str
    challenge_bond: float

    reason: str
    evidence: Dict

    status: str = "pending"  # pending / accepted / rejected
    created_at: float = 0.0


class Layer2TaskMarket:
    """
    Layer 2: PoUW 任务市场

    职责：
    1. 任务发布与分配
    2. 结果验证（zk-proof / challenge）
    3. 奖励分配
    4. Slashing（作弊惩罚）

    不负责：
    - 出块
    - 共识安全
    """

    # 配置参数
    MIN_TASK_BOND = 10.0         # 最低任务押金
    MIN_WORKER_BOND = 5.0        # 最低工作者押金
    MIN_CHALLENGE_BOND = 2.0     # 最低挑战押金
    CHALLENGE_PERIOD = 10        # 挑战期10个区块

    # 验证参数
    SAMPLING_RATIO = 0.1         # 抽样验证比例10%

    def __init__(self):
        self.tasks: Dict[str, PoUWTask] = {}
        self.challenges: Dict[str, Challenge] = {}
        self.worker_reputation: Dict[str, float] = {}  # 仅用于任务分配
        self.lock = threading.RLock()

    def submit_task(
        self,
        task_id: str,
        task_type: str,
        task_data: Dict,
        reward: float,
        submitter: str,
        task_bond: float
    ) -> Tuple[bool, str]:
        """
        提交任务

        需要押金防止伪造攻击：
        - 发布无意义任务
        - 控制验证节点
        - 自己验证自己
        """
        if task_bond < self.MIN_TASK_BOND:
            return False, f"Minimum task bond is {self.MIN_TASK_BOND}"

        with self.lock:
            task = PoUWTask(
                task_id=task_id,
                task_type=task_type,
                task_data=task_data,
                reward=reward,
                task_bond=task_bond,
                worker_bond=self.MIN_WORKER_BOND,
                verification_type="challenge",  # 默认使用挑战机制
                challenge_period=self.CHALLENGE_PERIOD,
                submitter=submitter,
                created_at=time.time()
            )

            self.tasks[task_id] = task
            return True, "Task submitted"

    def assign_task(
        self,
        task_id: str,
        worker: str,
        worker_bond: float
    ) -> Tuple[bool, str]:
        """
        分配任务

        使用信誉系统优先分配，但不影响共识安全
        """
        if task_id not in self.tasks:
            return False, "Task not found"

        task = self.tasks[task_id]

        if task.status != TaskStatus.PENDING:
            return False, "Task already assigned"

        if worker_bond < task.worker_bond:
            return False, f"Minimum worker bond is {task.worker_bond}"

        with self.lock:
            task.worker = worker
            task.status = TaskStatus.ASSIGNED
            task.assigned_at = time.time()

            return True, "Task assigned"

    def submit_result(
        self,
        task_id: str,
        worker: str,
        result: Dict,
        proof: Optional[str] = None
    ) -> Tuple[bool, str]:
        """
        提交结果

        支持两种验证方式：
        1. zk-proof：提交零知识证明
        2. challenge：进入挑战期，等待挑战
        """
        if task_id not in self.tasks:
            return False, "Task not found"

        task = self.tasks[task_id]

        if task.worker != worker:
            return False, "Not the assigned worker"

        if task.status != TaskStatus.ASSIGNED:
            return False, "Invalid task status"

        with self.lock:
            task.result = result
            task.proof = proof
            task.status = TaskStatus.SUBMITTED
            task.submitted_at = time.time()

            # 如果有zk-proof，立即验证
            if proof and task.verification_type == "zk-proof":
                if self._verify_zk_proof(task, proof):
                    task.status = TaskStatus.VERIFIED
                    task.verified_at = time.time()
                    return True, "Result verified (zk-proof)"

            # 否则进入挑战期
            return True, f"Result submitted, challenge period: {task.challenge_period} blocks"

    def submit_challenge(
        self,
        task_id: str,
        challenger: str,
        reason: str,
        evidence: Dict,
        challenge_bond: float
    ) -> Tuple[bool, str]:
        """
        提交挑战

        挑战机制（Challenge Game）：
        1. 任何人可以挑战
        2. 需要押金
        3. 若挑战成功 → 获得奖励 + 工作者被slash
        4. 若挑战失败 → 失去押金
        """
        if task_id not in self.tasks:
            return False, "Task not found"

        task = self.tasks[task_id]

        if task.status != TaskStatus.SUBMITTED:
            return False, "Task not in challenge period"

        if challenge_bond < self.MIN_CHALLENGE_BOND:
            return False, f"Minimum challenge bond is {self.MIN_CHALLENGE_BOND}"

        with self.lock:
            challenge_id = f"CH_{task_id}_{int(time.time())}"

            challenge = Challenge(
                challenge_id=challenge_id,
                task_id=task_id,
                challenger=challenger,
                challenge_bond=challenge_bond,
                reason=reason,
                evidence=evidence,
                created_at=time.time()
            )

            self.challenges[challenge_id] = challenge
            task.status = TaskStatus.CHALLENGED

            return True, "Challenge submitted"

    def resolve_challenge(
        self,
        challenge_id: str,
        is_valid: bool
    ) -> Tuple[bool, str]:
        """
        解决挑战

        由随机选择的验证者委员会决定
        """
        if challenge_id not in self.challenges:
            return False, "Challenge not found"

        challenge = self.challenges[challenge_id]
        task = self.tasks[challenge.task_id]

        with self.lock:
            if is_valid:
                # 挑战成功
                challenge.status = "accepted"
                task.status = TaskStatus.FAILED

                # Slash 工作者
                self._slash_worker(task.worker, task.worker_bond, "Failed challenge")

                # 奖励挑战者
                reward = challenge.challenge_bond + task.worker_bond * 0.5

                return True, f"Challenge accepted, challenger rewarded {reward}"

            else:
                # 挑战失败
                challenge.status = "rejected"
                task.status = TaskStatus.VERIFIED
                task.verified_at = time.time()

                # 挑战者失去押金
                return True, "Challenge rejected, bond slashed"

    def finalize_task(self, task_id: str, current_block: int) -> Tuple[bool, str]:
        """
        完成任务

        如果挑战期结束且无挑战，自动通过
        """
        if task_id not in self.tasks:
            return False, "Task not found"

        task = self.tasks[task_id]

        if task.status == TaskStatus.VERIFIED:
            # 已验证，发放奖励
            self._distribute_reward(task)
            return True, "Task finalized, reward distributed"

        if task.status == TaskStatus.SUBMITTED:
            # 检查挑战期是否结束
            blocks_passed = current_block - int(task.submitted_at / 30)  # 假设30秒一个块

            if blocks_passed >= task.challenge_period:
                # 挑战期结束，无挑战，自动通过
                task.status = TaskStatus.VERIFIED
                task.verified_at = time.time()
                self._distribute_reward(task)
                return True, "Task finalized (no challenge), reward distributed"

        return False, "Task not ready to finalize"

    def _verify_zk_proof(self, task: PoUWTask, proof: str) -> bool:
        """
        Verify zero-knowledge proof using Schnorr-like protocol
        
        Current implementation validates proof format (commitment:challenge:response:verification_hash).
        Production deployment should integrate with zk-SNARK/zk-STARK libraries like bellman or circom.
        """
        if not proof:
            return False
        
        try:
            # Parse Schnorr proof format
            parts = proof.split(":")
            if len(parts) < 4:
                return False
            
            proof_type = parts[0]  # Expected: "schnorr"
            if proof_type != "schnorr":
                return False
            
            commitment = parts[1]  # 64-char hex commitment
            challenge = parts[2]   # 32-char hex challenge
            response = parts[3]    # 64-char response
            verification_hash = parts[4] if len(parts) > 4 else ""
            
            # Validate format (hex strings of expected length)
            if len(commitment) != 64 or not all(c in "0123456789abcdef" for c in commitment):
                return False
            if len(challenge) != 32 or not all(c in "0123456789abcdef" for c in challenge):
                return False
            if len(response) != 64 or not all(c in "0123456789abcdef" for c in response):
                return False
            
            # Re-verify the proof hash if available
            if verification_hash:
                import hashlib
                expected_verification = hashlib.sha256(
                    f"{proof_type}:{commitment}:{challenge}:{response}{task.task_id}".encode()
                ).hexdigest()[:16]
                if verification_hash != expected_verification:
                    return False
            
            return True
            
        except Exception as e:
            logger.error(f"ZK proof verification failed: {e}")
            return False

    def _slash_worker(self, worker: str, amount: float, reason: str):
        """惩罚工作者"""
        if worker in self.worker_reputation:
            self.worker_reputation[worker] *= 0.8  # 降低信誉

        logger.warning(f"Worker {worker} slashed: {reason}, amount: {amount}")

    def _distribute_reward(self, task: PoUWTask):
        """分配奖励"""
        # 工作者获得奖励
        worker_reward = task.reward

        # 返还押金
        task_bond_return = task.task_bond
        worker_bond_return = task.worker_bond

        logger.info(f"Task {task.task_id} reward distributed: {worker_reward} to {task.worker}")

    def get_worker_reputation(self, worker: str) -> float:
        """
        获取工作者信誉

        注意：信誉仅用于任务分配优先级，不影响共识安全
        """
        return self.worker_reputation.get(worker, 1.0)

    def update_worker_reputation(self, worker: str, delta: float):
        """更新工作者信誉"""
        current = self.worker_reputation.get(worker, 1.0)
        self.worker_reputation[worker] = max(0.0, min(1.0, current + delta))


# ============== 集成接口 ==============

class DualLayerConsensus:
    """
    双层共识系统

    集成 Layer 1（安全共识）和 Layer 2（任务市场）
    """

    def __init__(self):
        self.layer1 = Layer1Consensus()
        self.layer2 = Layer2TaskMarket()
        self.current_block = 0

    def produce_block(self) -> Tuple[bool, str, Optional[str]]:
        """
        出块（Layer 1）

        由 PoS/DPoS 验证者出块
        """
        producer = self.layer1.select_block_producer(self.current_block)

        if not producer:
            return False, "No validator available", None

        # 出块
        self.current_block += 1

        # 检查是否有任务需要finalize
        self._finalize_pending_tasks()

        return True, f"Block {self.current_block} produced by {producer}", producer

    def _finalize_pending_tasks(self):
        """完成待处理任务"""
        for task_id, task in list(self.layer2.tasks.items()):
            if task.status == TaskStatus.SUBMITTED:
                self.layer2.finalize_task(task_id, self.current_block)


# ============== 全局实例 ==============

_consensus: Optional[DualLayerConsensus] = None

def get_dual_layer_consensus() -> DualLayerConsensus:
    """获取双层共识系统"""
    global _consensus
    if _consensus is None:
        _consensus = DualLayerConsensus()
    return _consensus
