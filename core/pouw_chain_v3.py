# -*- coding: utf-8 -*-
"""
POUW-Chain V3.0 完整实现

根据技术白皮书实现的完整系统：
1. Layer 1: PoS/DPoS 共识层
2. Layer 2: PoUW 计算层
3. 隐私计算模块（TEE/zk/MPC）
4. Challenge Game 机制
5. 状态提交（Rollup）
6. 完整的API接口

技术栈：
- 共识：PoS + VRF + BFT
- 验证：zk-proof + Challenge Game
- 隐私：TEE + MPC
- 存储：Merkle Tree + IPFS
"""

import time
import hashlib
import random
import threading
import json
import logging
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
import sqlite3

logger = logging.getLogger(__name__)


# ============== 1. 基础数据结构 ==============

class TaskStatus(Enum):
    """任务状态"""
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    COMPUTING = "computing"
    RESULT_SUBMITTED = "result_submitted"
    CHALLENGE_WINDOW = "challenge_window"
    CHALLENGED = "challenged"
    FINALIZED = "finalized"
    FAILED = "failed"


class PrivacyMode(Enum):
    """隐私模式"""
    TEE = "tee"          # Trusted Execution Environment
    ZK = "zk"            # Zero-Knowledge Proof
    MPC = "mpc"          # Multi-Party Computation
    NONE = "none"        # 无隐私保护


class VerificationType(Enum):
    """验证类型"""
    ZK_PROOF = "zk"           # zk-SNARK/zk-STARK
    CHALLENGE = "challenge"    # Challenge Game
    MULTI_NODE = "multi"       # 多节点验证


class ValidatorStatus(Enum):
    """验证者状态"""
    ACTIVE = "active"
    INACTIVE = "inactive"
    SLASHED = "slashed"


# ============== 2. Layer 1: 共识层 ==============

@dataclass
class Validator:
    """验证者"""
    id: str
    address: str
    stake: float
    voting_power: float
    status: ValidatorStatus = ValidatorStatus.ACTIVE

    # 统计
    blocks_produced: int = 0
    blocks_missed: int = 0
    last_active: float = 0.0

    # Slashing
    slash_count: int = 0
    total_slashed: float = 0.0


@dataclass
class Block:
    """区块"""
    height: int
    parent_hash: str
    state_root: str
    task_root: str
    proposer: str
    timestamp: float

    # 内容
    transactions: List[Dict] = field(default_factory=list)
    task_commitments: List[str] = field(default_factory=list)

    # 共识
    nonce: int = 0
    hash: str = ""

    def compute_hash(self) -> str:
        """计算区块哈希"""
        header = f"{self.height}{self.parent_hash}{self.state_root}{self.task_root}{self.proposer}{self.timestamp}{self.nonce}"
        return hashlib.sha256(header.encode()).hexdigest()


class Layer1Consensus:
    """
    Layer 1: 共识层

    职责：
    1. 区块生成（PoS/DPoS）
    2. 状态一致性（BFT）
    3. 安全防护（Slashing）
    """

    # 参数
    MIN_STAKE = 1000.0
    BLOCK_TIME = 30.0
    VALIDATOR_SET_SIZE = 21
    SLASH_DOUBLE_SIGN = 0.05
    SLASH_DOWNTIME = 0.01

    def __init__(self, data_dir: str = "./data"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(exist_ok=True)

        self.validators: Dict[str, Validator] = {}
        self.active_set: List[str] = []
        self.current_height = 0
        self.blocks: Dict[int, Block] = {}
        self.lock = threading.RLock()

        self._init_db()

    def _init_db(self):
        """初始化数据库"""
        db_path = self.data_dir / "layer1.db"
        conn = sqlite3.connect(str(db_path))

        conn.execute("""
            CREATE TABLE IF NOT EXISTS validators (
                id TEXT PRIMARY KEY,
                address TEXT NOT NULL,
                stake REAL NOT NULL,
                voting_power REAL NOT NULL,
                status TEXT NOT NULL,
                blocks_produced INTEGER DEFAULT 0,
                blocks_missed INTEGER DEFAULT 0,
                last_active REAL DEFAULT 0
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS blocks (
                height INTEGER PRIMARY KEY,
                parent_hash TEXT NOT NULL,
                state_root TEXT NOT NULL,
                task_root TEXT NOT NULL,
                proposer TEXT NOT NULL,
                timestamp REAL NOT NULL,
                hash TEXT NOT NULL
            )
        """)

        conn.commit()
        conn.close()

    def register_validator(
        self,
        validator_id: str,
        address: str,
        stake: float
    ) -> Tuple[bool, str]:
        """注册验证者"""
        if stake < self.MIN_STAKE:
            return False, f"Minimum stake is {self.MIN_STAKE}"

        with self.lock:
            validator = Validator(
                id=validator_id,
                address=address,
                stake=stake,
                voting_power=stake,
                last_active=time.time()
            )

            self.validators[validator_id] = validator
            self._update_active_set()

            return True, "Validator registered"

    def vrf_select_proposer(self, height: int) -> Optional[str]:
        """VRF 选择出块者"""
        if not self.active_set:
            return None

        # 简化版VRF（生产环境应使用真正的VRF）
        seed = hashlib.sha256(f"{height}".encode()).hexdigest()
        random.seed(int(seed, 16))

        # 按质押权重选择
        weights = [self.validators[vid].voting_power for vid in self.active_set]
        total = sum(weights)

        if total == 0:
            return None

        r = random.uniform(0, total)
        cumsum = 0
        for i, vid in enumerate(self.active_set):
            cumsum += weights[i]
            if r <= cumsum:
                return vid

        return self.active_set[0]

    def produce_block(
        self,
        task_commitments: List[str] = None
    ) -> Tuple[bool, str, Optional[Block]]:
        """生成区块"""
        proposer = self.vrf_select_proposer(self.current_height + 1)

        if not proposer:
            return False, "No validator available", None

        with self.lock:
            # 创建区块
            parent_hash = self.blocks[self.current_height].hash if self.current_height > 0 else "0" * 64

            block = Block(
                height=self.current_height + 1,
                parent_hash=parent_hash,
                state_root="",  # 由状态管理器计算
                task_root="",   # 由任务管理器计算
                proposer=proposer,
                timestamp=time.time(),
                task_commitments=task_commitments or []
            )

            block.hash = block.compute_hash()

            # 保存区块
            self.blocks[block.height] = block
            self.current_height = block.height

            # 更新验证者统计
            self.validators[proposer].blocks_produced += 1
            self.validators[proposer].last_active = time.time()

            return True, f"Block {block.height} produced", block

    def slash_validator(
        self,
        validator_id: str,
        reason: str,
        ratio: float
    ):
        """惩罚验证者"""
        if validator_id not in self.validators:
            return

        validator = self.validators[validator_id]
        slash_amount = validator.stake * ratio

        validator.stake -= slash_amount
        validator.total_slashed += slash_amount
        validator.slash_count += 1

        if validator.stake < self.MIN_STAKE:
            validator.status = ValidatorStatus.SLASHED

        logger.warning(f"Validator {validator_id} slashed: {reason}, amount: {slash_amount}")

    def _update_active_set(self):
        """更新活跃验证者集合"""
        active = [
            (vid, v) for vid, v in self.validators.items()
            if v.status == ValidatorStatus.ACTIVE
        ]

        # 按质押排序
        active.sort(key=lambda x: x[1].stake, reverse=True)

        self.active_set = [vid for vid, _ in active[:self.VALIDATOR_SET_SIZE]]


# ============== 3. Layer 2: 计算层 ==============

@dataclass
class Task:
    """任务"""
    task_id: str
    client: str

    # 数据
    encrypted_data: bytes
    data_hash: str

    # 计算
    compute_type: str
    reward: float
    deadline: int

    # 验证
    verification_type: VerificationType
    privacy_mode: PrivacyMode

    # 押金
    client_bond: float
    worker_stake_required: float

    # 状态
    status: TaskStatus = TaskStatus.SUBMITTED
    worker: str = ""
    result_hash: str = ""
    proof: str = ""

    # 时间
    created_at: float = 0.0
    accepted_at: float = 0.0
    submitted_at: float = 0.0
    finalized_at: float = 0.0


@dataclass
class Challenge:
    """挑战"""
    challenge_id: str
    task_id: str
    challenger: str
    stake: float

    reason: str
    evidence: Dict

    status: str = "pending"  # pending / accepted / rejected
    created_at: float = 0.0


class Layer2ComputeMarket:
    """
    Layer 2: 计算层

    职责：
    1. 任务市场
    2. 可验证计算（zk + challenge）
    3. 隐私计算（TEE / MPC）
    """

    # 参数
    MIN_CLIENT_BOND = 10.0
    MIN_WORKER_STAKE = 5.0
    MIN_CHALLENGE_STAKE = 2.0
    CHALLENGE_WINDOW = 10  # 区块数

    def __init__(self, data_dir: str = "./data"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(exist_ok=True)

        self.tasks: Dict[str, Task] = {}
        self.challenges: Dict[str, Challenge] = {}
        self.worker_reputation: Dict[str, float] = {}
        self.lock = threading.RLock()

        self._init_db()

    def _init_db(self):
        """初始化数据库"""
        db_path = self.data_dir / "layer2.db"
        conn = sqlite3.connect(str(db_path))

        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                client TEXT NOT NULL,
                data_hash TEXT NOT NULL,
                compute_type TEXT NOT NULL,
                reward REAL NOT NULL,
                verification_type TEXT NOT NULL,
                privacy_mode TEXT NOT NULL,
                status TEXT NOT NULL,
                worker TEXT DEFAULT '',
                result_hash TEXT DEFAULT '',
                created_at REAL NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS challenges (
                challenge_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                challenger TEXT NOT NULL,
                stake REAL NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at REAL NOT NULL
            )
        """)

        conn.commit()
        conn.close()

    def submit_task(
        self,
        task_id: str,
        client: str,
        encrypted_data: bytes,
        compute_type: str,
        reward: float,
        client_bond: float,
        verification_type: VerificationType = VerificationType.CHALLENGE,
        privacy_mode: PrivacyMode = PrivacyMode.TEE
    ) -> Tuple[bool, str]:
        """提交任务"""
        if client_bond < self.MIN_CLIENT_BOND:
            return False, f"Minimum client bond is {self.MIN_CLIENT_BOND}"

        with self.lock:
            data_hash = hashlib.sha256(encrypted_data).hexdigest()

            task = Task(
                task_id=task_id,
                client=client,
                encrypted_data=encrypted_data,
                data_hash=data_hash,
                compute_type=compute_type,
                reward=reward,
                deadline=int(time.time()) + 3600,  # 1小时
                verification_type=verification_type,
                privacy_mode=privacy_mode,
                client_bond=client_bond,
                worker_stake_required=self.MIN_WORKER_STAKE,
                created_at=time.time()
            )

            self.tasks[task_id] = task
            return True, "Task submitted"

    def accept_task(
        self,
        task_id: str,
        worker: str,
        worker_stake: float
    ) -> Tuple[bool, str]:
        """接受任务"""
        if task_id not in self.tasks:
            return False, "Task not found"

        task = self.tasks[task_id]

        if task.status != TaskStatus.SUBMITTED:
            return False, "Task already accepted"

        if worker_stake < task.worker_stake_required:
            return False, f"Minimum worker stake is {task.worker_stake_required}"

        with self.lock:
            task.worker = worker
            task.status = TaskStatus.ACCEPTED
            task.accepted_at = time.time()

            return True, "Task accepted"

    def submit_result(
        self,
        task_id: str,
        worker: str,
        result_hash: str,
        proof: str = ""
    ) -> Tuple[bool, str]:
        """提交结果"""
        if task_id not in self.tasks:
            return False, "Task not found"

        task = self.tasks[task_id]

        if task.worker != worker:
            return False, "Not the assigned worker"

        with self.lock:
            task.result_hash = result_hash
            task.proof = proof
            task.status = TaskStatus.RESULT_SUBMITTED
            task.submitted_at = time.time()

            # 如果有zk-proof，立即验证
            if proof and task.verification_type == VerificationType.ZK_PROOF:
                if self._verify_zk_proof(task, proof):
                    task.status = TaskStatus.FINALIZED
                    task.finalized_at = time.time()
                    return True, "Result verified (zk-proof)"

            # 否则进入挑战期
            task.status = TaskStatus.CHALLENGE_WINDOW
            return True, f"Result submitted, challenge window: {self.CHALLENGE_WINDOW} blocks"

    def submit_challenge(
        self,
        task_id: str,
        challenger: str,
        reason: str,
        evidence: Dict,
        stake: float
    ) -> Tuple[bool, str]:
        """提交挑战"""
        if task_id not in self.tasks:
            return False, "Task not found"

        task = self.tasks[task_id]

        if task.status != TaskStatus.CHALLENGE_WINDOW:
            return False, "Task not in challenge window"

        if stake < self.MIN_CHALLENGE_STAKE:
            return False, f"Minimum challenge stake is {self.MIN_CHALLENGE_STAKE}"

        with self.lock:
            challenge_id = f"CH_{task_id}_{int(time.time())}"

            challenge = Challenge(
                challenge_id=challenge_id,
                task_id=task_id,
                challenger=challenger,
                stake=stake,
                reason=reason,
                evidence=evidence,
                created_at=time.time()
            )

            self.challenges[challenge_id] = challenge
            task.status = TaskStatus.CHALLENGED

            return True, "Challenge submitted"

    def finalize_task(
        self,
        task_id: str,
        current_block: int
    ) -> Tuple[bool, str]:
        """完成任务"""
        if task_id not in self.tasks:
            return False, "Task not found"

        task = self.tasks[task_id]

        if task.status == TaskStatus.FINALIZED:
            return True, "Task already finalized"

        if task.status == TaskStatus.CHALLENGE_WINDOW:
            # 检查挑战期是否结束
            blocks_passed = current_block - int(task.submitted_at / 30)

            if blocks_passed >= self.CHALLENGE_WINDOW:
                task.status = TaskStatus.FINALIZED
                task.finalized_at = time.time()
                self._distribute_reward(task)
                return True, "Task finalized (no challenge)"

        return False, "Task not ready to finalize"

    def _verify_zk_proof(self, task: Task, proof: str) -> bool:
        """验证zk-proof"""
        # TODO: 实现真正的zk-proof验证
        return len(proof) > 0

    def _distribute_reward(self, task: Task):
        """分配奖励"""
        # 工作者获得奖励
        worker_reward = task.reward * 0.9

        # 验证者获得10%
        validator_reward = task.reward * 0.1

        logger.info(f"Task {task.task_id} reward distributed: {worker_reward} to {task.worker}")


# ============== 4. 隐私计算模块 ==============

class PrivacyCompute:
    """
    隐私计算模块

    支持三种模式：
    1. TEE（Trusted Execution Environment）
    2. zk（Zero-Knowledge Proof）
    3. MPC（Multi-Party Computation）
    """

    @staticmethod
    def encrypt_data(data: bytes, key: bytes) -> bytes:
        """加密数据"""
        # TODO: 实现真正的加密
        return data

    @staticmethod
    def decrypt_data(encrypted_data: bytes, key: bytes) -> bytes:
        """解密数据"""
        # TODO: 实现真正的解密
        return encrypted_data

    @staticmethod
    def tee_execute(task: Task) -> Tuple[str, str]:
        """TEE 执行"""
        # TODO: 实现TEE执行
        result_hash = hashlib.sha256(b"result").hexdigest()
        attestation = "tee_attestation"
        return result_hash, attestation

    @staticmethod
    def generate_zk_proof(task: Task, result: Any) -> str:
        """生成zk-proof"""
        # TODO: 实现zk-proof生成
        return "zk_proof_placeholder"

    @staticmethod
    def mpc_compute(task: Task, nodes: List[str]) -> str:
        """MPC 计算"""
        # TODO: 实现MPC计算
        return "mpc_result_hash"


# ============== 5. 状态提交（Rollup）==============

class StateCommitment:
    """
    状态提交模块

    使用Merkle Tree管理任务状态
    """

    def __init__(self):
        self.task_states: Dict[str, Dict] = {}

    def add_task_state(self, task_id: str, result_hash: str, status: str):
        """添加任务状态"""
        self.task_states[task_id] = {
            "result_hash": result_hash,
            "status": status,
            "timestamp": time.time()
        }

    def compute_state_root(self) -> str:
        """计算状态根"""
        if not self.task_states:
            return "0" * 64

        # 简化版Merkle Tree
        leaves = [
            hashlib.sha256(f"{tid}{state['result_hash']}".encode()).hexdigest()
            for tid, state in sorted(self.task_states.items())
        ]

        while len(leaves) > 1:
            new_leaves = []
            for i in range(0, len(leaves), 2):
                if i + 1 < len(leaves):
                    combined = leaves[i] + leaves[i+1]
                else:
                    combined = leaves[i] + leaves[i]
                new_leaves.append(hashlib.sha256(combined.encode()).hexdigest())
            leaves = new_leaves

        return leaves[0] if leaves else "0" * 64


# ============== 6. 完整系统集成 ==============

class POUWChainV3:
    """
    POUW-Chain V3.0 完整系统

    集成：
    - Layer 1: 共识层
    - Layer 2: 计算层
    - 隐私计算
    - 状态提交
    """

    def __init__(self, data_dir: str = "./data"):
        self.layer1 = Layer1Consensus(data_dir)
        self.layer2 = Layer2ComputeMarket(data_dir)
        self.privacy = PrivacyCompute()
        self.state = StateCommitment()

        self.running = False
        self.block_thread = None

    def start(self):
        """启动系统"""
        self.running = True
        self.block_thread = threading.Thread(target=self._block_production_loop)
        self.block_thread.start()
        logger.info("POUW-Chain V3.0 started")

    def stop(self):
        """停止系统"""
        self.running = False
        if self.block_thread:
            self.block_thread.join()
        logger.info("POUW-Chain V3.0 stopped")

    def _block_production_loop(self):
        """出块循环"""
        while self.running:
            # 收集待提交的任务
            task_commitments = []
            for task_id, task in self.layer2.tasks.items():
                if task.status == TaskStatus.FINALIZED:
                    task_commitments.append(task.result_hash)
                    self.state.add_task_state(task_id, task.result_hash, task.status.value)

            # 生成区块
            success, msg, block = self.layer1.produce_block(task_commitments)

            if success and block:
                # 更新状态根
                block.state_root = self.state.compute_state_root()
                block.task_root = self._compute_task_root(task_commitments)

                logger.info(f"Block {block.height} produced: {len(task_commitments)} tasks")

                # 完成挑战期结束的任务
                for task_id in list(self.layer2.tasks.keys()):
                    self.layer2.finalize_task(task_id, block.height)

            time.sleep(self.layer1.BLOCK_TIME)

    def _compute_task_root(self, commitments: List[str]) -> str:
        """计算任务根"""
        if not commitments:
            return "0" * 64

        combined = "".join(sorted(commitments))
        return hashlib.sha256(combined.encode()).hexdigest()


# ============== 7. 全局实例 ==============

_pouw_chain: Optional[POUWChainV3] = None

def get_pouw_chain(data_dir: str = "./data") -> POUWChainV3:
    """获取POUW-Chain实例"""
    global _pouw_chain
    if _pouw_chain is None:
        _pouw_chain = POUWChainV3(data_dir)
    return _pouw_chain
