# -*- coding: utf-8 -*-
"""
冷启动机制 - 解决前期启动问题

问题：
1. 前期没人持币 → 无法质押 → 无法参与
2. 没有矿工 → 没有算力 → 链无法运行

解决方案：
1. 创世区块预分配（给早期参与者）
2. 启动期免质押（前30天）
3. 渐进式质押要求（30天后逐步提高）
4. 测试网水龙头（免费领取测试币）
"""

import time
import json
from typing import Dict, List
from pathlib import Path


class ColdStartManager:
    """
    冷启动管理器

    阶段划分：
    - 阶段0（0-7天）：完全免质押，任何人可参与
    - 阶段1（7-30天）：低质押（1币），鼓励参与
    - 阶段2（30-90天）：正常质押（5币），逐步规范
    - 阶段3（90天+）：完全质押（5币+），成熟运营
    """

    # 阶段配置
    PHASE_0_DURATION = 7 * 86400      # 7天
    PHASE_1_DURATION = 30 * 86400     # 30天
    PHASE_2_DURATION = 90 * 86400     # 90天

    # 质押要求
    PHASE_0_MIN_STAKE = 0.0    # 免质押
    PHASE_1_MIN_STAKE = 1.0    # 1币
    PHASE_2_MIN_STAKE = 3.0    # 3币
    PHASE_3_MIN_STAKE = 5.0    # 5币（正常）

    def __init__(self, genesis_time: float):
        self.genesis_time = genesis_time

    def get_current_phase(self) -> int:
        """获取当前阶段"""
        elapsed = time.time() - self.genesis_time

        if elapsed < self.PHASE_0_DURATION:
            return 0
        elif elapsed < self.PHASE_1_DURATION:
            return 1
        elif elapsed < self.PHASE_2_DURATION:
            return 2
        else:
            return 3

    def get_min_stake_requirement(self, sector: str) -> float:
        """获取当前最低质押要求"""
        phase = self.get_current_phase()

        if phase == 0:
            return self.PHASE_0_MIN_STAKE
        elif phase == 1:
            return self.PHASE_1_MIN_STAKE
        elif phase == 2:
            return self.PHASE_2_MIN_STAKE
        else:
            return self.PHASE_3_MIN_STAKE

    def is_bootstrap_phase(self) -> bool:
        """是否处于启动阶段"""
        return self.get_current_phase() <= 1


class GenesisAllocation:
    """
    创世区块分配

    目的：给早期参与者分配初始代币，解决冷启动问题
    """

    def __init__(self):
        self.allocations: Dict[str, Dict[str, float]] = {}

    def add_allocation(
        self,
        address: str,
        sector: str,
        amount: float,
        reason: str
    ):
        """添加创世分配"""
        if address not in self.allocations:
            self.allocations[address] = {}

        if sector not in self.allocations[address]:
            self.allocations[address][sector] = 0.0

        self.allocations[address][sector] += amount

    def generate_genesis_allocations(self) -> Dict:
        """
        生成创世分配方案

        分配策略：
        1. 开发团队：10%（用于开发和运营）
        2. 早期测试者：5%（奖励早期参与）
        3. 社区基金：10%（用于生态建设）
        4. 挖矿奖励池：75%（通过挖矿释放）
        """
        allocations = {}

        # 每个扇区的总供应量
        SECTOR_MAX_SUPPLY = 21_000_000

        sectors = ["H100", "RTX4090", "RTX3080", "CPU", "GENERAL"]

        for sector in sectors:
            # 开发团队 10%
            self.add_allocation(
                address=f"{sector}_DEV_TEAM",
                sector=sector,
                amount=SECTOR_MAX_SUPPLY * 0.10,
                reason="Development and operations"
            )

            # 早期测试者 5%（分配给前100个测试者）
            early_tester_total = SECTOR_MAX_SUPPLY * 0.05
            per_tester = early_tester_total / 100

            for i in range(100):
                self.add_allocation(
                    address=f"{sector}_TESTER_{i:03d}",
                    sector=sector,
                    amount=per_tester,
                    reason="Early tester reward"
                )

            # 社区基金 10%
            self.add_allocation(
                address=f"{sector}_COMMUNITY_FUND",
                sector=sector,
                amount=SECTOR_MAX_SUPPLY * 0.10,
                reason="Community development"
            )

        return self.allocations

    def save_to_file(self, filepath: str):
        """保存到文件"""
        data = {
            "version": "1.0",
            "timestamp": time.time(),
            "allocations": self.allocations
        }

        with open(filepath, 'w') as f:
            json.dump(data, f, indent=2)

    @classmethod
    def load_from_file(cls, filepath: str) -> 'GenesisAllocation':
        """从文件加载"""
        with open(filepath, 'r') as f:
            data = json.load(f)

        allocation = cls()
        allocation.allocations = data['allocations']
        return allocation


class TestnetFaucet:
    """
    测试网水龙头

    功能：
    - 用户可以免费领取测试币
    - 每个地址每24小时可领取一次
    - 每次领取固定数量
    """

    FAUCET_AMOUNT = 100.0  # 每次领取100币
    COOLDOWN_PERIOD = 86400  # 24小时冷却

    def __init__(self, data_dir: str = "./data"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(exist_ok=True)

        self.claim_history: Dict[str, float] = {}  # address -> last_claim_time

    def can_claim(self, address: str) -> bool:
        """检查是否可以领取"""
        if address not in self.claim_history:
            return True

        last_claim = self.claim_history[address]
        return time.time() - last_claim >= self.COOLDOWN_PERIOD

    def claim(self, address: str, sector: str) -> tuple[bool, str, float]:
        """
        领取测试币

        Returns:
            (success, message, amount)
        """
        if not self.can_claim(address):
            last_claim = self.claim_history[address]
            remaining = self.COOLDOWN_PERIOD - (time.time() - last_claim)
            return False, f"Please wait {remaining/3600:.1f} hours", 0.0

        # 记录领取
        self.claim_history[address] = time.time()

        # 发放代币（实际实现需要调用sector_ledger）
        return True, f"Claimed {self.FAUCET_AMOUNT} {sector}_COIN", self.FAUCET_AMOUNT


# ============== 使用示例 ==============

def setup_cold_start():
    """设置冷启动机制"""

    # 1. 生成创世分配
    genesis = GenesisAllocation()
    allocations = genesis.generate_genesis_allocations()
    genesis.save_to_file("genesis_allocation.json")

    print("Genesis allocation created:")
    print(f"  Total addresses: {len(allocations)}")

    # 2. 创建冷启动管理器
    genesis_time = time.time()
    cold_start = ColdStartManager(genesis_time)

    print(f"\nCold start phases:")
    print(f"  Phase 0 (0-7 days): No stake required")
    print(f"  Phase 1 (7-30 days): {cold_start.PHASE_1_MIN_STAKE} coin stake")
    print(f"  Phase 2 (30-90 days): {cold_start.PHASE_2_MIN_STAKE} coin stake")
    print(f"  Phase 3 (90+ days): {cold_start.PHASE_3_MIN_STAKE} coin stake")

    # 3. 设置测试网水龙头
    faucet = TestnetFaucet()

    print(f"\nTestnet faucet:")
    print(f"  Amount per claim: {faucet.FAUCET_AMOUNT} coins")
    print(f"  Cooldown: {faucet.COOLDOWN_PERIOD/3600} hours")

    return genesis, cold_start, faucet


if __name__ == "__main__":
    setup_cold_start()
