# -*- coding: utf-8 -*-
"""
双见证兑换系统 V2 - 乐观确认优化

改进：
1. 乐观确认：立即铸造MAIN，异步验证见证
2. 风险分级：根据金额大小选择确认策略
3. 财库补偿：见证失败时从财库补偿
4. 性能提升：延迟从24小时降至30秒

风险策略：
- 小额(<100 MAIN): 立即确认，无需等待
- 中额(100-1000 MAIN): 等待1个见证(~30s)
- 大额(>1000 MAIN): 等待2个见证(~60s)
"""

import sqlite3
import time
import json
import hashlib
import threading
import logging
import uuid
from pathlib import Path
from typing import Dict, Optional, Tuple, List, Set
from dataclasses import dataclass, field
from enum import Enum
from contextlib import contextmanager

logger = logging.getLogger(__name__)

from core.sector_coin import SectorCoinType, SectorCoinLedger, get_sector_ledger
from core.crypto import ECDSASigner


class ExchangeStatus(Enum):
    """兑换状态"""
    PENDING = "PENDING"
    WITNESSING = "WITNESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class RiskLevel(Enum):
    """风险等级"""
    LOW = "low"      # <100 MAIN
    MEDIUM = "medium"  # 100-1000 MAIN
    HIGH = "high"    # >1000 MAIN


@dataclass
class WitnessRecord:
    """见证记录"""
    witness_sector: str
    witness_block_height: int
    witness_block_hash: str
    witness_time: float
    witness_signature: str = ""


@dataclass
class ExchangeRequest:
    """兑换请求"""
    exchange_id: str
    requester_address: str
    source_sector: str
    source_coin_type: SectorCoinType
    source_amount: float
    target_main_amount: float
    exchange_rate: float

    status: ExchangeStatus = ExchangeStatus.PENDING
    created_at: float = 0.0
    completed_at: Optional[float] = None

    # 风险等级
    risk_level: RiskLevel = RiskLevel.LOW

    # 见证信息
    required_witnesses: int = 2
    witness_sectors: List[str] = field(default_factory=list)
    witnesses: List[WitnessRecord] = field(default_factory=list)

    # 乐观确认
    optimistic_confirmed: bool = False
    optimistic_confirm_time: float = 0.0

    def to_dict(self) -> Dict:
        return {
            "exchange_id": self.exchange_id,
            "requester_address": self.requester_address,
            "source_sector": self.source_sector,
            "source_coin_type": self.source_coin_type.value,
            "source_amount": self.source_amount,
            "target_main_amount": self.target_main_amount,
            "exchange_rate": self.exchange_rate,
            "status": self.status.value,
            "risk_level": self.risk_level.value,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "optimistic_confirmed": self.optimistic_confirmed,
            "required_witnesses": self.required_witnesses,
            "witness_count": len(self.witnesses)
        }


class OptimisticDualWitnessExchange:
    """
    乐观双见证兑换系统

    核心改进：
    1. 小额交易立即确认
    2. 中额交易等待1个见证
    3. 大额交易等待2个见证
    4. 异步验证，不阻塞用户
    """

    # 风险阈值
    LOW_RISK_THRESHOLD = 100.0    # <100 MAIN
    MEDIUM_RISK_THRESHOLD = 1000.0  # <1000 MAIN

    # 见证超时
    WITNESS_TIMEOUT = 300  # 5分钟

    # 财库补偿地址
    TREASURY_ADDRESS = "MAIN_TREASURY"

    def __init__(self, db_path: str = None, testnet: bool = False, data_dir: str = None, dynamic_rate_engine=None):
        self.testnet = testnet
        self.required_witnesses = 1 if testnet else 2
        self.dynamic_rate_engine = dynamic_rate_engine

        if data_dir is not None:
            self.data_dir = Path(data_dir)
            self.db_path = Path(db_path) if db_path else self.data_dir / "dual_witness_exchange.db"
        elif db_path is not None:
            candidate_path = Path(db_path)
            if candidate_path.suffix.lower() == ".db":
                self.db_path = candidate_path
                self.data_dir = candidate_path.parent
            else:
                self.data_dir = candidate_path
                self.db_path = self.data_dir / "dual_witness_exchange.db"
        else:
            self.data_dir = Path("./data")
            self.db_path = self.data_dir / "dual_witness_exchange.db"

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()

        self.pending_exchanges: Dict[str, ExchangeRequest] = {}
        self.lock = threading.RLock()

        # 见证板块公钥注册表 {sector: public_key_hex}
        self.witness_public_keys: Dict[str, str] = {}
        self._load_witness_keys()

        self._start_background_verifier()

    @property
    def WITNESS_SECTORS(self) -> List[str]:
        return ["H100", "RTX4090", "RTX3080", "CPU", "GENERAL"]

    @property
    def BASE_EXCHANGE_RATES(self) -> Dict[str, float]:
        return {sector: self.get_exchange_rate(sector) for sector in self.WITNESS_SECTORS}

    def get_exchange_rate(self, sector: str) -> float:
        if self.dynamic_rate_engine is not None:
            try:
                rate = self.dynamic_rate_engine.get_rate(sector)
                if rate and rate > 0:
                    return rate
            except Exception:
                pass

        try:
            from core.sector_coin import get_sector_registry
            return get_sector_registry().get_exchange_rate(sector)
        except Exception:
            return 0.5

    def calculate_main_amount(self, sector: str, sector_coin_amount: float) -> float:
        return sector_coin_amount * self.get_exchange_rate(sector)

    def register_witness_public_key(self, sector: str, public_key_hex: str) -> bool:
        """注册见证板块的公钥"""
        try:
            # 验证公钥格式（64字节ECDSA公钥）
            if len(public_key_hex) != 128 or not all(c in '0123456789abcdefABCDEF' for c in public_key_hex):
                return False

            with self.lock:
                self.witness_public_keys[sector] = public_key_hex

                # 保存到数据库
                conn = sqlite3.connect(str(self.db_path))
                conn.execute("""
                    INSERT OR REPLACE INTO witness_keys
                    (sector, public_key, registered_at)
                    VALUES (?, ?, ?)
                """, (sector, public_key_hex, time.time()))
                conn.commit()
                conn.close()

                logger.info(f"Registered public key for witness sector {sector}")
                return True

        except Exception as e:
            logger.error(f"Failed to register witness public key: {e}")
            return False

    def _load_witness_keys(self):
        """加载见证公钥"""
        try:
            conn = sqlite3.connect(str(self.db_path))
            cursor = conn.execute("SELECT sector, public_key FROM witness_keys")
            for row in cursor.fetchall():
                self.witness_public_keys[row[0]] = row[1]
            conn.close()
        except Exception:
            # 表不存在时忽略
            pass

    def _init_db(self):
        """初始化数据库"""
        conn = sqlite3.connect(str(self.db_path))

        conn.execute("""
            CREATE TABLE IF NOT EXISTS exchanges (
                exchange_id TEXT PRIMARY KEY,
                requester_address TEXT NOT NULL,
                source_sector TEXT NOT NULL,
                source_coin_type TEXT NOT NULL,
                source_amount REAL NOT NULL,
                target_main_amount REAL NOT NULL,
                exchange_rate REAL NOT NULL,
                status TEXT NOT NULL,
                risk_level TEXT NOT NULL,
                required_witnesses INTEGER DEFAULT 2,
                witness_count INTEGER DEFAULT 0,
                optimistic_confirmed BOOLEAN DEFAULT 0,
                optimistic_confirm_time REAL DEFAULT 0,
                created_at REAL NOT NULL,
                completed_at REAL DEFAULT 0
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS witnesses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                exchange_id TEXT NOT NULL,
                witness_sector TEXT NOT NULL,
                witness_block_height INTEGER NOT NULL,
                witness_block_hash TEXT NOT NULL,
                witness_time REAL NOT NULL,
                FOREIGN KEY (exchange_id) REFERENCES exchanges(exchange_id)
            )
        """)

        # 见证公钥表
        conn.execute("""
            CREATE TABLE IF NOT EXISTS witness_keys (
                sector TEXT PRIMARY KEY,
                public_key TEXT NOT NULL,
                registered_at REAL NOT NULL
            )
        """)

        # 补偿记录表
        conn.execute("""
            CREATE TABLE IF NOT EXISTS compensations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                exchange_id TEXT NOT NULL,
                amount REAL NOT NULL,
                reason TEXT NOT NULL,
                timestamp REAL NOT NULL,
                FOREIGN KEY (exchange_id) REFERENCES exchanges(exchange_id)
            )
        """)

        conn.commit()
        conn.close()

    def request_exchange(
        self,
        requester_address: str,
        source_sector: str,
        source_amount: float,
        exchange_rate: float = 0.5
    ) -> Tuple[bool, str, Optional[str]]:
        """
        请求兑换（乐观确认）

        Returns:
            (success, message, exchange_id)
        """
        # 计算目标MAIN数量
        target_main_amount = source_amount * exchange_rate

        # 确定风险等级
        if target_main_amount < self.LOW_RISK_THRESHOLD:
            risk_level = RiskLevel.LOW
            required_witnesses = 0  # 无需见证
        elif target_main_amount < self.MEDIUM_RISK_THRESHOLD:
            risk_level = RiskLevel.MEDIUM
            required_witnesses = 1  # 1个见证
        else:
            risk_level = RiskLevel.HIGH
            required_witnesses = 2  # 2个见证

        # 创建兑换请求
        exchange_id = f"EX_{int(time.time())}_{uuid.uuid4().hex[:8]}"

        request = ExchangeRequest(
            exchange_id=exchange_id,
            requester_address=requester_address,
            source_sector=source_sector,
            source_coin_type=SectorCoinType(f"{source_sector}_COIN"),
            source_amount=source_amount,
            target_main_amount=target_main_amount,
            exchange_rate=exchange_rate,
            risk_level=risk_level,
            required_witnesses=required_witnesses,
            created_at=time.time()
        )

        with self.lock:
            # 锁定板块币
            sector_ledger = get_sector_ledger()
            success, msg = sector_ledger.lock_for_exchange(
                requester_address,
                request.source_coin_type,
                source_amount
            )

            if not success:
                return False, f"Failed to lock balance: {msg}", None

            # 保存到数据库
            conn = sqlite3.connect(str(self.db_path))
            conn.execute("""
                INSERT INTO exchanges
                (exchange_id, requester_address, source_sector, source_coin_type,
                 source_amount, target_main_amount, exchange_rate, status,
                 risk_level, required_witnesses, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                exchange_id, requester_address, source_sector,
                request.source_coin_type.value, source_amount, target_main_amount,
                exchange_rate, ExchangeStatus.PENDING.value, risk_level.value,
                required_witnesses, request.created_at
            ))
            conn.commit()
            conn.close()

            # 根据风险等级处理
            if risk_level == RiskLevel.LOW:
                # 低风险：立即确认
                self._execute_exchange_immediately(request)
                return True, "Exchange completed immediately", exchange_id

            elif risk_level == RiskLevel.MEDIUM:
                # 中风险：等待1个见证
                self.pending_exchanges[exchange_id] = request
                self._request_witnesses(request, count=1)
                return True, "Waiting for 1 witness (~30s)", exchange_id

            else:
                # 高风险：等待2个见证
                self.pending_exchanges[exchange_id] = request
                self._request_witnesses(request, count=2)
                return True, "Waiting for 2 witnesses (~60s)", exchange_id

    def _execute_exchange_immediately(self, request: ExchangeRequest):
        """立即执行兑换（低风险）"""
        try:
            # 销毁板块币
            sector_ledger = get_sector_ledger()
            sector_ledger.burn_for_exchange(
                request.requester_address,
                request.source_coin_type,
                request.source_amount,
                f"exchange_{request.exchange_id}"
            )

            # 铸造MAIN
            from core.utxo_store import get_utxo_store
            utxo_store = get_utxo_store()
            utxo_store.mint_main(
                request.requester_address,
                request.target_main_amount,
                f"exchange_{request.exchange_id}"
            )

            # 更新状态
            request.status = ExchangeStatus.COMPLETED
            request.optimistic_confirmed = True
            request.optimistic_confirm_time = time.time()
            request.completed_at = time.time()

            self._update_exchange_status(request)

            logger.info(f"Exchange {request.exchange_id} completed immediately (low risk)")

        except Exception as e:
            logger.error(f"Failed to execute exchange: {e}")
            request.status = ExchangeStatus.FAILED
            self._update_exchange_status(request)

    def _request_witnesses(self, request: ExchangeRequest, count: int):
        """请求见证"""
        # 选择见证板块（排除源板块）
        all_sectors = ["H100", "RTX4090", "RTX3080", "CPU", "GENERAL"]
        available_sectors = [s for s in all_sectors if s != request.source_sector]

        import random
        request.witness_sectors = random.sample(available_sectors, min(count, len(available_sectors)))

        logger.info(f"Exchange {request.exchange_id} requesting {count} witnesses from {request.witness_sectors}")

    def add_witness(
        self,
        exchange_id: str,
        witness_sector: str,
        block_height: int,
        block_hash: str,
        signature: str
    ) -> Tuple[bool, str]:
        """添加见证（带签名验证）"""
        with self.lock:
            if exchange_id not in self.pending_exchanges:
                return False, "Exchange not found"

            request = self.pending_exchanges[exchange_id]

            # 验证签名
            if not self._verify_witness_signature(witness_sector, exchange_id, block_height, block_hash, signature):
                return False, "Invalid witness signature"

            # 检查超时（24小时）
            if time.time() - request.created_at > 86400:
                return False, "Exchange timeout"

            # 添加见证记录
            witness = WitnessRecord(
                witness_sector=witness_sector,
                witness_block_height=block_height,
                witness_block_hash=block_hash,
                witness_time=time.time()
            )
            request.witnesses.append(witness)

            # 保存到数据库
            conn = sqlite3.connect(str(self.db_path))
            conn.execute("""
                INSERT INTO witnesses
                (exchange_id, witness_sector, witness_block_height,
                 witness_block_hash, witness_time)
                VALUES (?, ?, ?, ?, ?)
            """, (exchange_id, witness_sector, block_height, block_hash, witness.witness_time))
            conn.commit()
            conn.close()

            logger.info(f"Exchange {exchange_id} received witness from {witness_sector}")

            # 检查是否满足条件
            if len(request.witnesses) >= request.required_witnesses:
                self._execute_exchange_optimistic(request)
                return True, "Exchange completed"

            return True, f"Witness added ({len(request.witnesses)}/{request.required_witnesses})"

    def _verify_witness_signature(
        self,
        witness_sector: str,
        exchange_id: str,
        block_height: int,
        block_hash: str,
        signature: str
    ) -> bool:
        """验证见证签名"""
        try:
            # 获取见证板块的公钥
            public_key_hex = self.witness_public_keys.get(witness_sector)
            if not public_key_hex:
                logger.warning(f"No public key registered for witness sector {witness_sector}")
                return False

            # 验证签名长度
            if len(signature) < 64 or not all(c in '0123456789abcdefABCDEF' for c in signature):
                return False

            # 构造消息
            message = f"{exchange_id}:{block_height}:{block_hash}"
            message_bytes = message.encode()

            # 真实 ECDSA 验证
            public_key_bytes = bytes.fromhex(public_key_hex)
            signature_bytes = bytes.fromhex(signature)
            return ECDSASigner.verify(public_key_bytes, message_bytes, signature_bytes)

        except Exception as e:
            logger.error(f"Witness signature verification failed: {e}")
            return False

    def _execute_exchange_optimistic(self, request: ExchangeRequest):
        """乐观执行兑换"""
        try:
            # 销毁板块币
            sector_ledger = get_sector_ledger()
            sector_ledger.burn_for_exchange(
                request.requester_address,
                request.source_coin_type,
                request.source_amount,
                f"exchange_{request.exchange_id}"
            )

            # 铸造MAIN
            from core.utxo_store import get_utxo_store
            utxo_store = get_utxo_store()
            utxo_store.mint_main(
                request.requester_address,
                request.target_main_amount,
                f"exchange_{request.exchange_id}"
            )

            # 更新状态
            request.status = ExchangeStatus.COMPLETED
            request.optimistic_confirmed = True
            request.optimistic_confirm_time = time.time()
            request.completed_at = time.time()

            self._update_exchange_status(request)

            # 从待处理列表移除
            if request.exchange_id in self.pending_exchanges:
                del self.pending_exchanges[request.exchange_id]

            logger.info(f"Exchange {request.exchange_id} completed optimistically")

        except Exception as e:
            logger.error(f"Failed to execute exchange: {e}")
            request.status = ExchangeStatus.FAILED
            self._update_exchange_status(request)

    def _update_exchange_status(self, request: ExchangeRequest):
        """更新兑换状态"""
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("""
            UPDATE exchanges
            SET status = ?,
                witness_count = ?,
                optimistic_confirmed = ?,
                optimistic_confirm_time = ?,
                completed_at = ?
            WHERE exchange_id = ?
        """, (
            request.status.value,
            len(request.witnesses),
            1 if request.optimistic_confirmed else 0,
            request.optimistic_confirm_time,
            request.completed_at or 0,
            request.exchange_id
        ))
        conn.commit()
        conn.close()

    def _start_background_verifier(self):
        """启动后台验证器"""
        def verifier():
            while True:
                time.sleep(30)  # 每30秒检查一次

                with self.lock:
                    now = time.time()
                    timeout_exchanges = []

                    for exchange_id, request in self.pending_exchanges.items():
                        # 检查超时
                        if now - request.created_at > self.WITNESS_TIMEOUT:
                            timeout_exchanges.append(exchange_id)

                    # 处理超时
                    for exchange_id in timeout_exchanges:
                        request = self.pending_exchanges[exchange_id]
                        logger.warning(f"Exchange {exchange_id} timeout, compensating from treasury")

                        # 财库补偿
                        self._treasury_compensate(request)

                        # 标记失败
                        request.status = ExchangeStatus.FAILED
                        self._update_exchange_status(request)

                        del self.pending_exchanges[exchange_id]

        thread = threading.Thread(target=verifier, daemon=True)
        thread.start()

    def _treasury_compensate(self, request: ExchangeRequest):
        """财库补偿"""
        try:
            from core.utxo_store import get_utxo_store
            utxo_store = get_utxo_store()

            # 从财库转账给用户
            utxo_store.transfer(
                from_address=self.TREASURY_ADDRESS,
                to_address=request.requester_address,
                amount=request.target_main_amount,
                memo=f"Compensation for failed exchange {request.exchange_id}"
            )

            # 记录补偿
            conn = sqlite3.connect(str(self.db_path))
            conn.execute("""
                INSERT INTO compensations
                (exchange_id, amount, reason, timestamp)
                VALUES (?, ?, ?, ?)
            """, (
                request.exchange_id,
                request.target_main_amount,
                "Witness timeout",
                time.time()
            ))
            conn.commit()
            conn.close()

            logger.info(f"Treasury compensated {request.target_main_amount} MAIN for exchange {request.exchange_id}")

        except Exception as e:
            logger.error(f"Failed to compensate from treasury: {e}")

    def get_exchange_status(self, exchange_id: str) -> Optional[Dict]:
        """获取兑换状态"""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.execute("""
            SELECT * FROM exchanges WHERE exchange_id = ?
        """, (exchange_id,))

        row = cursor.fetchone()
        conn.close()

        if not row:
            return None

        return {
            "exchange_id": row[0],
            "requester_address": row[1],
            "source_sector": row[2],
            "source_amount": row[4],
            "target_main_amount": row[5],
            "status": row[7],
            "risk_level": row[8],
            "witness_count": row[10],
            "optimistic_confirmed": bool(row[11]),
            "created_at": row[13],
            "completed_at": row[14]
        }


class DualWitnessExchange(OptimisticDualWitnessExchange):
    """Backward-compatible alias for older callers and tests."""
    pass


# ============== 全局实例 ==============

_exchange: Optional[OptimisticDualWitnessExchange] = None

def get_dual_witness_exchange(data_dir: str = "./data") -> OptimisticDualWitnessExchange:
    """获取双见证兑换系统"""
    global _exchange
    if _exchange is None:
        _exchange = OptimisticDualWitnessExchange(data_dir)
    return _exchange


get_exchange_service = get_dual_witness_exchange
