# -*- coding: utf-8 -*-
"""
仲裁机制模块 - PRD v0.9

任务纠纷处理：
1. 仲裁期：任务提交后进入质疑窗口期
2. 双方质押：租用方和矿工都需要质押保证金
3. 随机验证节点裁决：纠纷由随机选取的验证节点进行裁决

设计约束：
- 仲裁期间资金锁定
- 恶意方扣除质押金
- 验证节点获得仲裁奖励
"""

import uuid
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Callable, Any
from enum import Enum


class DisputeStatus(Enum):
    """纠纷状态。"""
    # 正常流程
    PENDING = "PENDING"           # 等待仲裁期结束
    COMPLETED = "COMPLETED"       # 无纠纷完成
    
    # 纠纷流程
    DISPUTED = "DISPUTED"         # 有纠纷
    VOTING = "VOTING"             # 验证节点投票中
    RESOLVED_RENTER = "RESOLVED_RENTER"   # 裁决：租用方胜
    RESOLVED_MINER = "RESOLVED_MINER"     # 裁决：矿工胜
    RESOLVED_SPLIT = "RESOLVED_SPLIT"     # 裁决：各退一半


class DisputeReason(Enum):
    """纠纷原因。"""
    RESULT_INCORRECT = "RESULT_INCORRECT"     # 计算结果不正确
    TASK_NOT_COMPLETED = "TASK_NOT_COMPLETED" # 任务未完成
    QUALITY_ISSUE = "QUALITY_ISSUE"           # 质量问题
    TIMEOUT = "TIMEOUT"                       # 超时
    OTHER = "OTHER"                           # 其他


@dataclass
class Dispute:
    """纠纷记录。"""
    dispute_id: str
    task_id: str
    renter_id: str              # 租用方（任务发布者）
    miner_id: str               # 矿工
    reason: DisputeReason
    description: str
    evidence: Dict[str, Any]    # 证据（哈希、日志等）
    created_at: float = field(default_factory=time.time)
    resolved_at: float = 0.0
    status: DisputeStatus = DisputeStatus.DISPUTED
    votes: Dict[str, str] = field(default_factory=dict)  # validator_id -> vote
    selected_validators: List[str] = field(default_factory=list)
    resolution: str = ""


@dataclass
class TaskArbitration:
    """任务仲裁记录。
    
    每个任务在完成后进入仲裁期，期间可以发起纠纷。
    """
    task_id: str
    renter_id: str              # 租用方
    miner_id: str               # 矿工
    renter_stake: float         # 租用方质押
    miner_stake: float          # 矿工质押
    task_payment: float         # 任务报酬
    coin_type: str              # 支付币种（板块币或 MAIN）
    arbitration_start: float = field(default_factory=time.time)
    arbitration_end: float = 0.0       # 仲裁期结束时间
    status: DisputeStatus = DisputeStatus.PENDING
    dispute: Optional[Dispute] = None
    
    # 仲裁期设置
    ARBITRATION_PERIOD: float = 3600 * 24  # 24 小时仲裁期
    
    def __post_init__(self):
        if self.arbitration_end == 0.0:
            self.arbitration_end = self.arbitration_start + self.ARBITRATION_PERIOD
    
    def is_in_arbitration(self) -> bool:
        """是否在仲裁期内。"""
        return time.time() < self.arbitration_end
    
    def time_remaining(self) -> float:
        """剩余仲裁时间（秒）。"""
        return max(0, self.arbitration_end - time.time())


class ArbitrationSystem:
    """仲裁系统（PRD v0.9）。
    
    功能：
    1. 管理任务仲裁期
    2. 处理纠纷提交
    3. 协调验证节点投票
    4. 执行裁决结果
    
    规则：
    - 双方质押：租用方质押 10% 任务报酬，矿工质押 10% 任务报酬
    - 仲裁期：24 小时（可配置）
    - 裁决需要 3 个以上验证节点投票
    - 获胜方取回质押 + 获得对方质押的一部分
    - 验证节点获得仲裁奖励
    """
    
    # 质押比例（PRD v0.9：控制在合理范围）
    STAKE_RATIO = 0.05  # 5% 任务报酬作为质押（不能太高）
    
    # 投票要求
    MIN_VALIDATORS = 3  # 最少验证节点数
    VOTE_THRESHOLD = 0.67  # 67% 多数裁决
    
    # 罚没比例（PRD v0.9：控制在合理范围）
    PENALTY_RATIO = 0.50  # 败方质押的 50% 给胜方（不能太高）
    VALIDATOR_REWARD_RATIO = 0.05  # 5% 给验证节点
    # 剩余 45% 进入财政
    
    def __init__(
        self,
        validator_pool: List[str] = None,
        treasury: Any = None,
        arbitration_period: float = 3600 * 24,
        log_fn: Callable[[str], None] = print,
    ):
        self.validator_pool = validator_pool or []
        self.treasury = treasury
        self.arbitration_period = arbitration_period
        self.log = log_fn
        
        # 仲裁记录
        self.arbitrations: Dict[str, TaskArbitration] = {}  # task_id -> arbitration
        self.disputes: Dict[str, Dispute] = {}  # dispute_id -> dispute
        
        # 账户系统（用于质押/结算）
        self._accounts: Dict[str, Any] = {}
    
    def register_account(self, account_id: str, account: Any):
        """注册账户（用于质押/结算）。"""
        self._accounts[account_id] = account
    
    def start_arbitration(
        self,
        task_id: str,
        renter_id: str,
        miner_id: str,
        task_payment: float,
        coin_type: str,
    ) -> Optional[TaskArbitration]:
        """开始任务仲裁期。
        
        任务完成后调用，双方质押并进入仲裁期。
        """
        # 计算质押金额
        stake_amount = task_payment * self.STAKE_RATIO
        
        # 扣除双方质押
        renter_account = self._accounts.get(renter_id)
        miner_account = self._accounts.get(miner_id)
        
        if renter_account and hasattr(renter_account, 'stake'):
            if not renter_account.stake(stake_amount, f"arbitration_{task_id}"):
                self.log(f"❌ [ARBITRATION] Renter {renter_id} insufficient balance for stake")
                return None
        
        if miner_account and hasattr(miner_account, 'stake'):
            if not miner_account.stake(stake_amount, f"arbitration_{task_id}"):
                self.log(f"❌ [ARBITRATION] Miner {miner_id} insufficient balance for stake")
                # 退还租用方质押
                if renter_account:
                    renter_account.unstake(stake_amount, f"arbitration_{task_id}")
                return None
        
        # 创建仲裁记录
        arbitration = TaskArbitration(
            task_id=task_id,
            renter_id=renter_id,
            miner_id=miner_id,
            renter_stake=stake_amount,
            miner_stake=stake_amount,
            task_payment=task_payment,
            coin_type=coin_type,
        )
        arbitration.ARBITRATION_PERIOD = self.arbitration_period
        arbitration.arbitration_end = time.time() + self.arbitration_period
        
        self.arbitrations[task_id] = arbitration
        
        self.log(f"⚖️ [ARBITRATION] Started for task {task_id}: "
                 f"renter={renter_id}, miner={miner_id}, stake={stake_amount} {coin_type}")
        
        return arbitration
    
    def submit_dispute(
        self,
        task_id: str,
        submitter_id: str,
        reason: DisputeReason,
        description: str,
        evidence: Dict[str, Any] = None,
    ) -> Optional[Dispute]:
        """提交纠纷。
        
        只能在仲裁期内提交。
        """
        arbitration = self.arbitrations.get(task_id)
        if not arbitration:
            self.log(f"❌ [DISPUTE] Task {task_id} not found")
            return None
        
        # 检查仲裁期
        if not arbitration.is_in_arbitration():
            self.log(f"❌ [DISPUTE] Arbitration period ended for task {task_id}")
            return None
        
        # 检查提交者是否为相关方
        if submitter_id not in [arbitration.renter_id, arbitration.miner_id]:
            self.log(f"❌ [DISPUTE] {submitter_id} is not a party to task {task_id}")
            return None
        
        # 创建纠纷
        dispute = Dispute(
            dispute_id=uuid.uuid4().hex[:12],
            task_id=task_id,
            renter_id=arbitration.renter_id,
            miner_id=arbitration.miner_id,
            reason=reason,
            description=description,
            evidence=evidence or {},
        )
        
        self.disputes[dispute.dispute_id] = dispute
        arbitration.dispute = dispute
        arbitration.status = DisputeStatus.DISPUTED
        
        self.log(f"⚠️ [DISPUTE] Submitted for task {task_id} by {submitter_id}: {reason.value}")
        
        # 选择验证节点进行投票
        self._initiate_voting(dispute)
        
        return dispute
    
    def _initiate_voting(self, dispute: Dispute):
        """发起验证节点投票。"""
        if len(self.validator_pool) < self.MIN_VALIDATORS:
            self.log(f"⚠️ [DISPUTE] Not enough validators ({len(self.validator_pool)} < {self.MIN_VALIDATORS})")
            return
        
        # 随机选择验证节点（排除相关方）
        eligible = [v for v in self.validator_pool 
                    if v not in [dispute.renter_id, dispute.miner_id]]
        
        if len(eligible) < self.MIN_VALIDATORS:
            self.log(f"⚠️ [DISPUTE] Not enough eligible validators")
            return
        
        # 使用密码学安全随机选择，混入不可预测因素防止提前贼赂验证者
        import secrets
        _secure_rng = secrets.SystemRandom()
        selected = _secure_rng.sample(eligible, min(len(eligible), self.MIN_VALIDATORS * 2))
        dispute.selected_validators = list(selected)
        
        dispute.status = DisputeStatus.VOTING
        
        self.log(f"🗳️ [DISPUTE] Voting initiated with {len(selected)} validators")
    
    def cast_vote(
        self,
        dispute_id: str,
        validator_id: str,
        vote: str,  # "RENTER", "MINER", "SPLIT"
    ) -> bool:
        """验证节点投票。"""
        dispute = self.disputes.get(dispute_id)
        if not dispute:
            return False
        
        if dispute.status != DisputeStatus.VOTING:
            self.log(f"❌ [VOTE] Dispute {dispute_id} not in voting phase")
            return False
        
        if validator_id not in self.validator_pool:
            self.log(f"❌ [VOTE] {validator_id} is not a validator")
            return False

        if dispute.selected_validators and validator_id not in dispute.selected_validators:
            self.log(f"❌ [VOTE] {validator_id} was not selected for dispute {dispute_id}")
            return False
        
        if validator_id in [dispute.renter_id, dispute.miner_id]:
            self.log(f"❌ [VOTE] {validator_id} cannot vote on own dispute")
            return False
        
        dispute.votes[validator_id] = vote
        self.log(f"🗳️ [VOTE] {validator_id} voted {vote} on dispute {dispute_id}")
        
        # 检查是否达到投票阈值
        if len(dispute.votes) >= self.MIN_VALIDATORS:
            self._resolve_dispute(dispute)
        
        return True
    
    def _resolve_dispute(self, dispute: Dispute):
        """解决纠纷。"""
        votes = list(dispute.votes.values())
        
        renter_votes = votes.count("RENTER")
        miner_votes = votes.count("MINER")
        split_votes = votes.count("SPLIT")
        
        total = len(votes)
        
        if total == 0:
            dispute.status = DisputeStatus.RESOLVED_SPLIT
            dispute.resolved_at = time.time()
            dispute.resolution = "No votes received"
            return
        
        # 确定结果
        if renter_votes / total >= self.VOTE_THRESHOLD:
            dispute.status = DisputeStatus.RESOLVED_RENTER
            winner = "RENTER"
        elif miner_votes / total >= self.VOTE_THRESHOLD:
            dispute.status = DisputeStatus.RESOLVED_MINER
            winner = "MINER"
        else:
            dispute.status = DisputeStatus.RESOLVED_SPLIT
            winner = "SPLIT"
        
        dispute.resolved_at = time.time()
        dispute.resolution = f"Votes: RENTER={renter_votes}, MINER={miner_votes}, SPLIT={split_votes}"
        
        self.log(f"⚖️ [DISPUTE] Resolved: {winner} ({dispute.resolution})")
        
        # 执行裁决
        self._execute_resolution(dispute)
    
    def _execute_resolution(self, dispute: Dispute):
        """执行裁决结果（分配质押金）。"""
        arbitration = self.arbitrations.get(dispute.task_id)
        if not arbitration:
            return
        
        renter_account = self._accounts.get(arbitration.renter_id)
        miner_account = self._accounts.get(arbitration.miner_id)
        
        renter_stake = arbitration.renter_stake
        miner_stake = arbitration.miner_stake
        
        if dispute.status == DisputeStatus.RESOLVED_RENTER:
            # 租用方胜：取回自己质押 + 获得矿工质押的一部分
            penalty = miner_stake * self.PENALTY_RATIO
            validator_reward = miner_stake * self.VALIDATOR_REWARD_RATIO
            treasury_share = miner_stake - penalty - validator_reward
            
            if renter_account and hasattr(renter_account, 'unstake'):
                renter_account.unstake(renter_stake, f"arbitration_{dispute.task_id}")
                renter_account.credit_main(penalty)
            
            # 矿工损失质押
            self.log(f"💸 [RESOLUTION] Renter wins: +{penalty}, Miner loses stake")
            
        elif dispute.status == DisputeStatus.RESOLVED_MINER:
            # 矿工胜：取回自己质押 + 获得租用方质押的一部分
            penalty = renter_stake * self.PENALTY_RATIO
            validator_reward = renter_stake * self.VALIDATOR_REWARD_RATIO
            treasury_share = renter_stake - penalty - validator_reward
            
            if miner_account and hasattr(miner_account, 'unstake'):
                miner_account.unstake(miner_stake, f"arbitration_{dispute.task_id}")
                miner_account.credit_main(penalty)
            
            self.log(f"💸 [RESOLUTION] Miner wins: +{penalty}, Renter loses stake")
            
        else:  # SPLIT
            # 各退一半
            if renter_account and hasattr(renter_account, 'unstake'):
                renter_account.unstake(renter_stake * 0.5, f"arbitration_{dispute.task_id}")
            if miner_account and hasattr(miner_account, 'unstake'):
                miner_account.unstake(miner_stake * 0.5, f"arbitration_{dispute.task_id}")
            
            treasury_share = (renter_stake + miner_stake) * 0.5
            validator_reward = 0
            
            self.log(f"💸 [RESOLUTION] Split: both parties get 50% back")
        
        # 奖励验证节点
        if dispute.votes and validator_reward > 0:
            reward_per_validator = validator_reward / len(dispute.votes)
            for validator_id in dispute.votes:
                validator_account = self._accounts.get(validator_id)
                if validator_account and hasattr(validator_account, 'credit_main'):
                    validator_account.credit_main(reward_per_validator)
            self.log(f"🎁 [RESOLUTION] Validators rewarded: {reward_per_validator} each")
        
        # 财政收取
        if self.treasury and treasury_share > 0:
            self.treasury.balance += treasury_share
            self.log(f"💰 [RESOLUTION] Treasury received: {treasury_share}")
        
        arbitration.status = dispute.status
    
    def finalize_arbitration(self, task_id: str) -> bool:
        """仲裁期结束，无纠纷则正常完成。
        
        仲裁期结束后调用，如果没有纠纷则释放质押，完成支付。
        """
        arbitration = self.arbitrations.get(task_id)
        if not arbitration:
            return False
        
        # 仍在仲裁期内
        if arbitration.is_in_arbitration():
            self.log(f"⏳ [ARBITRATION] Task {task_id} still in arbitration period "
                     f"({arbitration.time_remaining():.0f}s remaining)")
            return False
        
        # 有纠纷，等待裁决
        if arbitration.status == DisputeStatus.DISPUTED or arbitration.status == DisputeStatus.VOTING:
            self.log(f"⚠️ [ARBITRATION] Task {task_id} has pending dispute")
            return False
        
        # 已解决的纠纷
        if arbitration.status in [DisputeStatus.RESOLVED_RENTER, 
                                   DisputeStatus.RESOLVED_MINER, 
                                   DisputeStatus.RESOLVED_SPLIT]:
            return True
        
        # 无纠纷，正常完成
        arbitration.status = DisputeStatus.COMPLETED
        
        # 退还双方质押
        renter_account = self._accounts.get(arbitration.renter_id)
        miner_account = self._accounts.get(arbitration.miner_id)
        
        if renter_account and hasattr(renter_account, 'unstake'):
            renter_account.unstake(arbitration.renter_stake, f"arbitration_{task_id}")
        if miner_account and hasattr(miner_account, 'unstake'):
            miner_account.unstake(arbitration.miner_stake, f"arbitration_{task_id}")
        
        # 支付任务报酬给矿工
        if miner_account and hasattr(miner_account, 'credit_main'):
            miner_account.credit_main(arbitration.task_payment)
        
        self.log(f"✅ [ARBITRATION] Task {task_id} completed without dispute. "
                 f"Payment {arbitration.task_payment} {arbitration.coin_type} released to miner.")
        
        return True
    
    def get_arbitration_status(self, task_id: str) -> Optional[Dict]:
        """获取仲裁状态。"""
        arbitration = self.arbitrations.get(task_id)
        if not arbitration:
            return None
        
        return {
            "task_id": task_id,
            "status": arbitration.status.value,
            "renter_id": arbitration.renter_id,
            "miner_id": arbitration.miner_id,
            "renter_stake": arbitration.renter_stake,
            "miner_stake": arbitration.miner_stake,
            "task_payment": arbitration.task_payment,
            "coin_type": arbitration.coin_type,
            "in_arbitration": arbitration.is_in_arbitration(),
            "time_remaining": arbitration.time_remaining(),
            "has_dispute": arbitration.dispute is not None,
            "dispute_id": arbitration.dispute.dispute_id if arbitration.dispute else None,
        }


# 测试
if __name__ == "__main__":
    print("=" * 60)
    print("仲裁系统测试 (PRD v0.9)")
    print("=" * 60)
    
    # 创建仲裁系统
    arb = ArbitrationSystem(
        validator_pool=["validator_1", "validator_2", "validator_3", "validator_4", "validator_5"],
        arbitration_period=5,  # 5秒仲裁期（测试用）
    )
    
    # Create real accounts instead of mock accounts
    from core.account import Account
    
    renter = Account(address="renter_1", balance=100.0)
    miner = Account(address="miner_1", balance=50.0)
    
    arb.register_account("renter_1", renter)
    arb.register_account("miner_1", miner)
    
    # Start arbitration
    arb_record = arb.start_arbitration(
        task_id="task_001",
        renter_id="renter_1",
        miner_id="miner_1",
        task_payment=10.0,
        coin_type="H100_COIN",
    )
    
    print(f"\nArbitration status: {arb.get_arbitration_status('task_001')}")
    print(f"Renter balance: {renter.balance}, Staked: {renter.staked if hasattr(renter, 'staked') else 0}")
    print(f"Miner balance: {miner.balance}, Staked: {miner.staked if hasattr(miner, 'staked') else 0}")
    
    # 等待仲裁期结束
    print("\n等待仲裁期结束...")
    time.sleep(6)
    
    # 无纠纷完成
    arb.finalize_arbitration("task_001")
    
    print(f"\n最终状态: {arb.get_arbitration_status('task_001')}")
    print(f"租用方余额: {renter.balance}, 质押: {renter.staked}")
    print(f"矿工余额: {miner.balance}, 质押: {miner.staked}")
