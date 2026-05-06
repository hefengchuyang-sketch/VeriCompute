# -*- coding: utf-8 -*-
"""
在线奖励池 V2 - 板块币激励机制

核心设计原则：
1. 只发放板块币（不发MAIN）
2. 资金来源可追溯（国库批准或板块启动资金）
3. 接任务收益 >> 在线奖励（10倍差距）
4. 防止女巫攻击（最低质押）

收益对比：
- 接任务执行：10-100 板块币/小时
- 在线奖励池：0.5-2 板块币/小时（仅保底）

资金来源：
1. 板块启动资金（前3个月，每小时50板块币）
2. 区块奖励的2%（长期可持续）
3. 国库特批补贴（需DAO投票）
"""

import time
import threading
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
import sqlite3
from pathlib import Path
import json

logger = logging.getLogger(__name__)


@dataclass
class MinerOnlineRecord:
    """矿工在线记录"""
    miner_id: str
    address: str
    sector: str

    # 算力信息
    compute_power: float  # TFLOPs
    gpu_count: int = 1

    # 在线信息
    online_start: float = 0.0
    last_heartbeat: float = 0.0
    total_online_time: float = 0.0

    # 质押信息（板块币）
    stake_amount: float = 0.0

    # 收益信息（板块币）
    total_earned: float = 0.0
    last_reward_time: float = 0.0


@dataclass
class FundingSource:
    """资金来源记录（可追溯）"""
    source_id: str
    source_type: str  # "startup_fund", "block_reward", "treasury_grant"
    sector: str
    amount: float
    approved_by: str  # DAO提案ID或"GENESIS"
    timestamp: float
    memo: str = ""


class SectorOnlineRewardPool:
    """
    板块在线奖励池

    每个板块独立运营，发放本板块币
    """

    # 配置参数
    EPOCH_DURATION = 3600  # 1小时结算
    HEARTBEAT_INTERVAL = 60
    HEARTBEAT_TIMEOUT = 180

    # 资金来源比例
    BLOCK_REWARD_RATIO = 0.02  # 区块奖励2%（降低，确保任务收益更高）

    def __init__(self, sector: str, data_dir: str = "./data", genesis_time: float = None):
        # 冷启动支持
        if genesis_time is None:
            genesis_time = time.time()

        from core.cold_start import ColdStartManager
        self.cold_start = ColdStartManager(genesis_time)

    # 启动资金（前3个月）
    STARTUP_FUND_EPOCHS = 2160  # 90天 = 2160小时
    STARTUP_FUND_PER_EPOCH = 50.0  # 每小时50板块币

    def __init__(self, sector: str, data_dir: str = "./data", genesis_time: float = None):
        self.sector = sector
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(exist_ok=True)

        # 冷启动支持
        if genesis_time is None:
            genesis_time = time.time()

        try:
            from core.cold_start import ColdStartManager
            self.cold_start = ColdStartManager(genesis_time)
        except ImportError:
            self.cold_start = None
            logger.warning("ColdStartManager not available, using default stake requirements")

        self.db_path = self.data_dir / f"reward_pool_{sector.lower()}.db"
        self._init_db()

        self.current_epoch_id = 0
        self.current_epoch_start = 0.0
        self.current_pool_balance = 0.0

        self.online_miners: Dict[str, MinerOnlineRecord] = {}
        self.lock = threading.RLock()

        self._load_state()
        self._start_background_tasks()

    def _init_db(self):
        """初始化数据库"""
        conn = sqlite3.connect(str(self.db_path))

        # 矿工表
        conn.execute("""
            CREATE TABLE IF NOT EXISTS miners (
                miner_id TEXT PRIMARY KEY,
                address TEXT NOT NULL,
                sector TEXT NOT NULL,
                compute_power REAL DEFAULT 0,
                gpu_count INTEGER DEFAULT 1,
                stake_amount REAL DEFAULT 0,
                total_online_time REAL DEFAULT 0,
                total_earned REAL DEFAULT 0,
                registered_at REAL DEFAULT 0,
                last_seen REAL DEFAULT 0
            )
        """)

        # 资金来源表（可追溯）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS funding_sources (
                source_id TEXT PRIMARY KEY,
                source_type TEXT NOT NULL,
                sector TEXT NOT NULL,
                amount REAL NOT NULL,
                approved_by TEXT NOT NULL,
                timestamp REAL NOT NULL,
                memo TEXT DEFAULT ''
            )
        """)

        # 周期表
        conn.execute("""
            CREATE TABLE IF NOT EXISTS epochs (
                epoch_id INTEGER PRIMARY KEY,
                sector TEXT NOT NULL,
                start_time REAL NOT NULL,
                end_time REAL NOT NULL,
                total_pool REAL DEFAULT 0,
                participants_count INTEGER DEFAULT 0,
                distributed BOOLEAN DEFAULT 0,
                distribution_time REAL DEFAULT 0
            )
        """)

        # 奖励记录表
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rewards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                epoch_id INTEGER NOT NULL,
                miner_id TEXT NOT NULL,
                reward_amount REAL NOT NULL,
                online_time REAL NOT NULL,
                contribution_score REAL NOT NULL,
                timestamp REAL NOT NULL
            )
        """)

        # 状态表
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pool_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                sector TEXT NOT NULL,
                current_epoch_id INTEGER DEFAULT 0,
                current_epoch_start REAL DEFAULT 0,
                current_pool_balance REAL DEFAULT 0,
                total_distributed REAL DEFAULT 0,
                last_updated REAL DEFAULT 0
            )
        """)

        conn.commit()
        conn.close()

    def _load_state(self):
        """加载状态"""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.execute("""
            SELECT current_epoch_id, current_epoch_start, current_pool_balance
            FROM pool_state WHERE id = 1
        """)
        row = cursor.fetchone()

        if row:
            self.current_epoch_id = row[0]
            self.current_epoch_start = row[1]
            self.current_pool_balance = row[2]
        else:
            # 初始化状态
            conn.execute("""
                INSERT INTO pool_state
                (id, sector, current_epoch_id, current_epoch_start,
                 current_pool_balance, total_distributed, last_updated)
                VALUES (1, ?, 0, ?, 0, 0, ?)
            """, (self.sector, time.time(), time.time()))
            conn.commit()

        conn.close()

    def _save_state(self):
        """保存状态"""
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("""
            UPDATE pool_state
            SET current_epoch_id = ?,
                current_epoch_start = ?,
                current_pool_balance = ?,
                last_updated = ?
            WHERE id = 1
        """, (self.current_epoch_id, self.current_epoch_start,
              self.current_pool_balance, time.time()))
        conn.commit()
        conn.close()

    def add_funding(
        self,
        amount: float,
        source_type: str,
        approved_by: str,
        memo: str = ""
    ) -> str:
        """
        添加资金（可追溯）

        Args:
            amount: 金额（板块币）
            source_type: 来源类型
            approved_by: 批准者（DAO提案ID或"GENESIS"）
            memo: 备注

        Returns:
            source_id: 资金来源ID
        """
        import uuid
        source_id = f"FUND_{self.sector}_{int(time.time())}_{uuid.uuid4().hex[:8]}"

        with self.lock:
            conn = sqlite3.connect(str(self.db_path))

            # 记录资金来源
            conn.execute("""
                INSERT INTO funding_sources
                (source_id, source_type, sector, amount, approved_by, timestamp, memo)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (source_id, source_type, self.sector, amount, approved_by,
                  time.time(), memo))

            # 增加池余额
            self.current_pool_balance += amount

            conn.commit()
            conn.close()

            self._save_state()

            logger.info(f"[{self.sector}] Added funding: {amount} from {source_type}, approved by {approved_by}")
            return source_id

    def register_miner(
        self,
        miner_id: str,
        address: str,
        compute_power: float,
        gpu_count: int = 1,
        stake_amount: float = 0.0
    ) -> Tuple[bool, str]:
        """注册矿工"""
        # 动态获取最低质押要求（支持冷启动）
        if self.cold_start:
            min_stake = self.cold_start.get_min_stake_requirement(self.sector)
        else:
            min_stake = 5.0  # 默认值

        if stake_amount < min_stake:
            phase = self.cold_start.get_current_phase() if self.cold_start else 3
            return False, f"Minimum stake is {min_stake} {self.sector}_COIN (Phase {phase})"

        with self.lock:
            conn = sqlite3.connect(str(self.db_path))

            try:
                conn.execute("""
                    INSERT OR REPLACE INTO miners
                    (miner_id, address, sector, compute_power, gpu_count,
                     stake_amount, registered_at, last_seen)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (miner_id, address, self.sector, compute_power, gpu_count,
                      stake_amount, time.time(), time.time()))
                conn.commit()

                self.online_miners[miner_id] = MinerOnlineRecord(
                    miner_id=miner_id,
                    address=address,
                    sector=self.sector,
                    compute_power=compute_power,
                    gpu_count=gpu_count,
                    stake_amount=stake_amount,
                    online_start=time.time(),
                    last_heartbeat=time.time()
                )

                logger.info(f"[{self.sector}] Miner {miner_id} registered")
                return True, "Registration successful"

            except Exception as e:
                logger.error(f"Failed to register miner: {e}")
                return False, str(e)
            finally:
                conn.close()

    def heartbeat(self, miner_id: str) -> Tuple[bool, str]:
        """矿工心跳"""
        with self.lock:
            if miner_id not in self.online_miners:
                return False, "Miner not registered"

            miner = self.online_miners[miner_id]
            now = time.time()

            if miner.last_heartbeat > 0:
                duration = now - miner.last_heartbeat
                if duration < self.HEARTBEAT_TIMEOUT:
                    miner.total_online_time += duration

            miner.last_heartbeat = now

            conn = sqlite3.connect(str(self.db_path))
            conn.execute("""
                UPDATE miners
                SET last_seen = ?, total_online_time = ?
                WHERE miner_id = ?
            """, (now, miner.total_online_time, miner_id))
            conn.commit()
            conn.close()

            return True, "OK"

    def _distribute_rewards(self):
        """分配奖励"""
        with self.lock:
            if self.current_pool_balance <= 0:
                logger.warning(f"[{self.sector}] No funds to distribute")
                return

            # 清理离线矿工
            self._cleanup_offline_miners()

            if not self.online_miners:
                logger.info(f"[{self.sector}] No online miners")
                return

            # 计算贡献分数
            contributions = {}
            total_contribution = 0.0

            for miner_id, miner in self.online_miners.items():
                online_hours = miner.total_online_time / 3600.0
                stake_weight = self._calculate_stake_weight(miner.stake_amount)

                contribution = miner.compute_power * online_hours * stake_weight
                contributions[miner_id] = contribution
                total_contribution += contribution

            if total_contribution == 0:
                return

            # 分配奖励
            conn = sqlite3.connect(str(self.db_path))

            for miner_id, contribution in contributions.items():
                miner = self.online_miners[miner_id]

                reward = self.current_pool_balance * (contribution / total_contribution)

                conn.execute("""
                    INSERT INTO rewards
                    (epoch_id, miner_id, reward_amount, online_time,
                     contribution_score, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (self.current_epoch_id, miner_id, reward,
                      miner.total_online_time, contribution, time.time()))

                miner.total_earned += reward
                miner.last_reward_time = time.time()

                conn.execute("""
                    UPDATE miners
                    SET total_earned = total_earned + ?
                    WHERE miner_id = ?
                """, (reward, miner_id))

                logger.info(f"[{self.sector}] Miner {miner_id} earned {reward:.4f} {self.sector}_COIN")

            # 记录周期
            conn.execute("""
                INSERT INTO epochs
                (epoch_id, sector, start_time, end_time, total_pool,
                 participants_count, distributed, distribution_time)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?)
            """, (self.current_epoch_id, self.sector, self.current_epoch_start,
                  time.time(), self.current_pool_balance, len(contributions),
                  time.time()))

            conn.commit()
            conn.close()

            # 重置池余额
            self.current_pool_balance = 0.0
            self._save_state()

            logger.info(f"[{self.sector}] Epoch {self.current_epoch_id} distributed to {len(contributions)} miners")

    def _calculate_stake_weight(self, stake_amount: float) -> float:
        """计算质押权重"""
        if stake_amount < self.MIN_STAKE:
            return 0.0

        import math
        return 1.0 + math.log(1 + stake_amount / self.MIN_STAKE) * 0.2

    def _cleanup_offline_miners(self):
        """清理离线矿工"""
        now = time.time()
        offline = [
            mid for mid, m in self.online_miners.items()
            if now - m.last_heartbeat > self.HEARTBEAT_TIMEOUT
        ]

        for mid in offline:
            del self.online_miners[mid]
            logger.info(f"[{self.sector}] Miner {mid} offline")

    def _start_background_tasks(self):
        """启动后台任务"""
        def epoch_manager():
            while True:
                time.sleep(60)

                with self.lock:
                    now = time.time()

                    if self.current_epoch_start == 0:
                        self.current_epoch_start = now
                        self.current_epoch_id += 1
                        self._save_state()

                    # 检查是否需要结算
                    if now - self.current_epoch_start >= self.EPOCH_DURATION:
                        self._distribute_rewards()
                        self.current_epoch_start = now
                        self.current_epoch_id += 1
                        self._save_state()

                    # 启动资金注入
                    if self.current_epoch_id <= self.STARTUP_FUND_EPOCHS:
                        self.add_funding(
                            amount=self.STARTUP_FUND_PER_EPOCH,
                            source_type="startup_fund",
                            approved_by="GENESIS",
                            memo=f"Startup fund for epoch {self.current_epoch_id}"
                        )

        thread = threading.Thread(target=epoch_manager, daemon=True)
        thread.start()

    def get_funding_history(self, limit: int = 100) -> List[Dict]:
        """获取资金来源历史（可追溯）"""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.execute("""
            SELECT source_id, source_type, amount, approved_by, timestamp, memo
            FROM funding_sources
            ORDER BY timestamp DESC
            LIMIT ?
        """, (limit,))

        history = []
        for row in cursor.fetchall():
            history.append({
                "source_id": row[0],
                "source_type": row[1],
                "amount": row[2],
                "approved_by": row[3],
                "timestamp": row[4],
                "memo": row[5]
            })

        conn.close()
        return history

    def get_miner_stats(self, miner_id: str) -> Optional[Dict]:
        """获取单个矿工的奖励池统计信息。"""
        with self.lock:
            miner = self.online_miners.get(miner_id)
            if miner:
                return {
                    "miner_id": miner.miner_id,
                    "address": miner.address,
                    "sector": miner.sector,
                    "compute_power": miner.compute_power,
                    "gpu_count": miner.gpu_count,
                    "stake_amount": miner.stake_amount,
                    "total_online_time": miner.total_online_time,
                    "total_earned": miner.total_earned,
                    "last_heartbeat": miner.last_heartbeat,
                }

            conn = sqlite3.connect(str(self.db_path))
            try:
                row = conn.execute(
                    """
                    SELECT miner_id, address, sector, compute_power, gpu_count,
                           stake_amount, total_online_time, total_earned, last_seen
                    FROM miners
                    WHERE miner_id = ?
                    """,
                    (miner_id,)
                ).fetchone()
                if not row:
                    return None

                return {
                    "miner_id": row[0],
                    "address": row[1],
                    "sector": row[2],
                    "compute_power": row[3],
                    "gpu_count": row[4],
                    "stake_amount": row[5],
                    "total_online_time": row[6],
                    "total_earned": row[7],
                    "last_heartbeat": row[8],
                }
            finally:
                conn.close()

    def get_miner_stats(self, miner_id: str) -> Optional[Dict]:
        """获取单个矿工的奖励池统计信息。"""
        with self.lock:
            miner = self.online_miners.get(miner_id)
            if miner:
                return {
                    "miner_id": miner.miner_id,
                    "address": miner.address,
                    "sector": miner.sector,
                    "compute_power": miner.compute_power,
                    "gpu_count": miner.gpu_count,
                    "stake_amount": miner.stake_amount,
                    "total_online_time": miner.total_online_time,
                    "total_earned": miner.total_earned,
                    "last_heartbeat": miner.last_heartbeat,
                }

            conn = sqlite3.connect(str(self.db_path))
            try:
                row = conn.execute(
                    """
                    SELECT miner_id, address, sector, compute_power, gpu_count,
                           stake_amount, total_online_time, total_earned, last_seen
                    FROM miners
                    WHERE miner_id = ?
                    """,
                    (miner_id,)
                ).fetchone()
                if not row:
                    return None

                return {
                    "miner_id": row[0],
                    "address": row[1],
                    "sector": row[2],
                    "compute_power": row[3],
                    "gpu_count": row[4],
                    "stake_amount": row[5],
                    "total_online_time": row[6],
                    "total_earned": row[7],
                    "last_heartbeat": row[8],
                }
            finally:
                conn.close()


# ============== 全局管理器 ==============

class OnlineRewardPoolManager:
    """在线奖励池管理器（多板块）"""

    def __init__(self, data_dir: str = "./data"):
        self.data_dir = data_dir
        self.pools: Dict[str, SectorOnlineRewardPool] = {}
        self.lock = threading.RLock()

    def get_pool(self, sector: str) -> SectorOnlineRewardPool:
        """获取板块奖励池"""
        with self.lock:
            if sector not in self.pools:
                self.pools[sector] = SectorOnlineRewardPool(sector, self.data_dir)
            return self.pools[sector]

    def add_block_reward_contribution(self, sector: str, block_reward: float):
        """从区块奖励注入资金"""
        pool = self.get_pool(sector)
        contribution = block_reward * pool.BLOCK_REWARD_RATIO

        pool.add_funding(
            amount=contribution,
            source_type="block_reward",
            approved_by="PROTOCOL",
            memo=f"2% of block reward {block_reward}"
        )


_manager: Optional[OnlineRewardPoolManager] = None

def get_reward_pool_manager(data_dir: str = "./data") -> OnlineRewardPoolManager:
    """获取全局管理器"""
    global _manager
    if _manager is None:
        _manager = OnlineRewardPoolManager(data_dir)
    return _manager
