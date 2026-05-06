# -*- coding: utf-8 -*-
"""
初始币产生机制详解

问题：区块链启动时，没有任何币存在，如何产生第一批币？

解决方案：三种方式组合
1. 创世区块铸币（Genesis Block Minting）
2. 早期挖矿奖励（Early Mining Rewards）
3. 预挖矿分配（Pre-mining Allocation）
"""

import time
import json
from typing import Dict, List
from pathlib import Path


class InitialCoinGeneration:
    """
    初始币产生机制

    三阶段产生：
    1. 创世区块：直接铸造初始供应量的25%
    2. 早期挖矿：前1000个区块奖励翻倍
    3. 正常挖矿：按标准减半机制
    """

    def __init__(self, sector: str):
        self.sector = sector
        self.max_supply = 21_000_000  # 最大供应量

    def generate_genesis_coins(self) -> Dict[str, float]:
        """
        创世区块铸币

        分配方案：
        - 开发团队：10%（2,100,000币）
        - 早期测试者：5%（1,050,000币）
        - 社区基金：10%（2,100,000币）
        - 挖矿奖励池：75%（15,750,000币）

        总计：25%在创世区块产生，75%通过挖矿释放
        """
        genesis_supply = self.max_supply * 0.25  # 25%在创世区块

        allocations = {
            # 开发团队（锁仓1年）
            f"{self.sector}_DEV_TEAM": {
                "amount": genesis_supply * 0.40,  # 10% of total = 40% of genesis
                "vesting": {
                    "type": "linear",
                    "duration": 365 * 86400,  # 1年
                    "cliff": 90 * 86400  # 3个月cliff
                }
            },

            # 早期测试者（100人，每人10,500币）
            f"{self.sector}_EARLY_TESTERS": {
                "amount": genesis_supply * 0.20,  # 5% of total = 20% of genesis
                "count": 100,
                "per_tester": (genesis_supply * 0.20) / 100,
                "vesting": {
                    "type": "immediate",  # 立即解锁
                    "duration": 0
                }
            },

            # 社区基金（DAO管理）
            f"{self.sector}_COMMUNITY_FUND": {
                "amount": genesis_supply * 0.40,  # 10% of total = 40% of genesis
                "vesting": {
                    "type": "dao_controlled",  # DAO投票释放
                    "duration": 0
                }
            }
        }

        return allocations

    def calculate_early_mining_reward(self, block_height: int) -> float:
        """
        早期挖矿奖励（前1000个区块翻倍）

        目的：激励早期矿工参与

        奖励曲线：
        - 区块0-100：基础奖励 × 3（超级奖励期）
        - 区块100-500：基础奖励 × 2（高奖励期）
        - 区块500-1000：基础奖励 × 1.5（过渡期）
        - 区块1000+：基础奖励 × 1（正常期）
        """
        # 基础奖励（根据扇区不同）
        base_rewards = {
            "H100": 10.0,
            "RTX4090": 5.0,
            "RTX3080": 2.5,
            "CPU": 1.0,
            "GENERAL": 1.0
        }

        base_reward = base_rewards.get(self.sector, 1.0)

        # 早期奖励倍数
        if block_height < 100:
            multiplier = 3.0  # 前100个区块3倍
        elif block_height < 500:
            multiplier = 2.0  # 100-500区块2倍
        elif block_height < 1000:
            multiplier = 1.5  # 500-1000区块1.5倍
        else:
            multiplier = 1.0  # 正常奖励

        return base_reward * multiplier

    def calculate_total_genesis_supply(self) -> Dict:
        """计算创世总供应量"""
        allocations = self.generate_genesis_coins()

        total_genesis = 0.0
        breakdown = {}

        for key, value in allocations.items():
            amount = value["amount"]
            total_genesis += amount
            breakdown[key] = amount

        # 挖矿池（75%）
        mining_pool = self.max_supply * 0.75

        return {
            "max_supply": self.max_supply,
            "genesis_supply": total_genesis,
            "genesis_percentage": (total_genesis / self.max_supply) * 100,
            "mining_pool": mining_pool,
            "mining_percentage": 75.0,
            "breakdown": breakdown
        }


class GenesisBlockGenerator:
    """
    创世区块生成器

    创世区块是区块链的第0个区块，包含：
    1. 初始币分配
    2. 系统参数
    3. 创世时间戳
    """

    def __init__(self):
        self.genesis_time = time.time()
        self.sectors = ["H100", "RTX4090", "RTX3080", "CPU", "GENERAL"]

    def generate_genesis_block(self) -> Dict:
        """生成创世区块"""

        genesis_block = {
            "version": "1.0",
            "block_height": 0,
            "timestamp": self.genesis_time,
            "prev_hash": "0" * 64,  # 创世区块没有前一个区块
            "merkle_root": "",
            "difficulty": 4,
            "nonce": 0,

            # 创世分配
            "genesis_allocations": {},

            # 系统参数
            "chain_params": {
                "target_block_time": 30,
                "max_supply_per_sector": 21_000_000,
                "halving_interval": 10000,
                "treasury_rate": 0.03
            },

            # 创世消息
            "genesis_message": "POUW-Chain Genesis Block - Proof of Useful Work"
        }

        # 为每个扇区生成初始分配
        for sector in self.sectors:
            generator = InitialCoinGeneration(sector)
            allocations = generator.generate_genesis_coins()

            genesis_block["genesis_allocations"][sector] = allocations

            # 计算总供应
            supply_info = generator.calculate_total_genesis_supply()
            genesis_block["genesis_allocations"][sector]["supply_info"] = supply_info

        return genesis_block

    def save_genesis_block(self, filepath: str):
        """保存创世区块到文件"""
        genesis_block = self.generate_genesis_block()

        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(genesis_block, f, indent=2, ensure_ascii=False)

        print(f"Genesis block saved to: {filepath}")
        return genesis_block

    def print_genesis_summary(self):
        """打印创世区块摘要"""
        genesis_block = self.generate_genesis_block()

        print("="*60)
        print("POUW-Chain Genesis Block Summary")
        print("="*60)
        print(f"Genesis Time: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.genesis_time))}")
        print(f"Sectors: {len(self.sectors)}")
        print()

        for sector in self.sectors:
            supply_info = genesis_block["genesis_allocations"][sector]["supply_info"]

            print(f"{sector} Sector:")
            print(f"  Max Supply: {supply_info['max_supply']:,.0f} {sector}_COIN")
            print(f"  Genesis Supply: {supply_info['genesis_supply']:,.0f} ({supply_info['genesis_percentage']:.1f}%)")
            print(f"  Mining Pool: {supply_info['mining_pool']:,.0f} ({supply_info['mining_percentage']:.1f}%)")
            print()

            print(f"  Genesis Breakdown:")
            for key, amount in supply_info['breakdown'].items():
                percentage = (amount / supply_info['max_supply']) * 100
                print(f"    {key}: {amount:,.0f} ({percentage:.1f}%)")
            print()


class VestingSchedule:
    """
    锁仓释放计划

    开发团队的币需要锁仓，防止砸盘
    """

    def __init__(self, total_amount: float, vesting_type: str, duration: int, cliff: int = 0):
        """
        Args:
            total_amount: 总锁仓金额
            vesting_type: 释放类型 (linear/cliff/milestone)
            duration: 释放周期（秒）
            cliff: 悬崖期（秒）
        """
        self.total_amount = total_amount
        self.vesting_type = vesting_type
        self.duration = duration
        self.cliff = cliff
        self.start_time = time.time()

    def get_unlocked_amount(self, current_time: float = None) -> float:
        """获取已解锁金额"""
        if current_time is None:
            current_time = time.time()

        elapsed = current_time - self.start_time

        # 悬崖期内，0解锁
        if elapsed < self.cliff:
            return 0.0

        # 线性释放
        if self.vesting_type == "linear":
            if elapsed >= self.duration:
                return self.total_amount

            # 线性计算
            unlocked_ratio = (elapsed - self.cliff) / (self.duration - self.cliff)
            return self.total_amount * unlocked_ratio

        # 立即释放
        elif self.vesting_type == "immediate":
            return self.total_amount

        # DAO控制
        elif self.vesting_type == "dao_controlled":
            # 需要DAO投票才能释放
            return 0.0

        return 0.0

    def get_locked_amount(self, current_time: float = None) -> float:
        """获取仍锁定的金额"""
        unlocked = self.get_unlocked_amount(current_time)
        return self.total_amount - unlocked


# ============== 实际使用示例 ==============

def create_genesis_block():
    """创建创世区块"""

    generator = GenesisBlockGenerator()

    # 打印摘要
    generator.print_genesis_summary()

    # 保存到文件
    genesis_block = generator.save_genesis_block("genesis_block.json")

    return genesis_block


def simulate_early_mining():
    """模拟早期挖矿"""

    print("\n" + "="*60)
    print("Early Mining Simulation")
    print("="*60)

    generator = InitialCoinGeneration("H100")

    # 模拟前1500个区块的奖励
    milestones = [0, 50, 100, 250, 500, 750, 1000, 1500]

    print("\nBlock Rewards (H100 Sector):")
    print(f"{'Block':<10} {'Reward':<15} {'Multiplier':<15}")
    print("-" * 40)

    for block_height in milestones:
        reward = generator.calculate_early_mining_reward(block_height)
        base_reward = 10.0
        multiplier = reward / base_reward

        print(f"{block_height:<10} {reward:<15.2f} {multiplier:<15.1f}x")


def simulate_vesting():
    """模拟锁仓释放"""

    print("\n" + "="*60)
    print("Vesting Schedule Simulation")
    print("="*60)

    # 开发团队锁仓：2,100,000币，1年线性释放，3个月cliff
    total_amount = 2_100_000
    duration = 365 * 86400  # 1年
    cliff = 90 * 86400  # 3个月

    vesting = VestingSchedule(
        total_amount=total_amount,
        vesting_type="linear",
        duration=duration,
        cliff=cliff
    )

    # 模拟不同时间点的解锁情况
    time_points = [
        (0, "Day 0 (Genesis)"),
        (30 * 86400, "Day 30 (1 month)"),
        (90 * 86400, "Day 90 (3 months, cliff end)"),
        (180 * 86400, "Day 180 (6 months)"),
        (365 * 86400, "Day 365 (1 year, fully vested)")
    ]

    print(f"\nDev Team Vesting (Total: {total_amount:,.0f} coins)")
    print(f"{'Time':<30} {'Unlocked':<20} {'Locked':<20} {'%':<10}")
    print("-" * 80)

    for elapsed, label in time_points:
        current_time = vesting.start_time + elapsed
        unlocked = vesting.get_unlocked_amount(current_time)
        locked = vesting.get_locked_amount(current_time)
        percentage = (unlocked / total_amount) * 100

        print(f"{label:<30} {unlocked:>15,.0f} {locked:>15,.0f} {percentage:>8.1f}%")


if __name__ == "__main__":
    # 1. 创建创世区块
    create_genesis_block()

    # 2. 模拟早期挖矿
    simulate_early_mining()

    # 3. 模拟锁仓释放
    simulate_vesting()

    print("\n" + "="*60)
    print("Summary:")
    print("="*60)
    print("✅ Genesis block created with 25% initial supply")
    print("✅ Early mining rewards (3x → 2x → 1.5x → 1x)")
    print("✅ Dev team vesting (1 year linear, 3 months cliff)")
    print("✅ 75% supply released through mining over time")
