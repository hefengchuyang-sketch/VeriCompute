# -*- coding: utf-8 -*-
"""
MAIN 币转账系统 - 强制双见证

设计约束 (DR-5):
- MAIN 交易必须多板块承载
- 所有 MAIN 转账需至少 2 个板块见证确认
- 单板块确认 → 交易待定
- 双板块确认 → 交易生效

架构:
1. 用户发起 MAIN 转账
2. 交易广播到所有板块
3. 至少 2 个板块的见证节点验证并签名
4. 见证达成后交易生效，写入各板块区块
"""

import time
import json
import hashlib
import sqlite3
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
from contextlib import contextmanager

from core.double_witness import (
    WitnessNode, WitnessRecord, MainTransaction,
    WitnessStatus, TransactionType, DoubleWitnessEngine
)


class TransferStatus(Enum):
    """转账状态"""
    PENDING = "PENDING"              # 待见证
    WITNESSING = "WITNESSING"        # 见证中
    CONFIRMED = "CONFIRMED"          # 已确认（双见证达成）
    FAILED = "FAILED"                # 失败
    REJECTED = "REJECTED"            # 被拒绝


@dataclass
class MainTransfer:
    """MAIN 转账请求"""
    transfer_id: str
    from_address: str
    to_address: str
    amount: float
    fee: float
    
    # 签名
    signature: str = ""
    public_key: str = ""
    
    # 状态
    status: TransferStatus = TransferStatus.PENDING
    created_at: float = 0.0
    confirmed_at: Optional[float] = None
    
    # 双见证
    required_witnesses: int = 2      # DR-5: 至少 2 个板块见证
    witness_sectors: List[str] = field(default_factory=list)
    witnesses: List[Dict] = field(default_factory=list)
    
    # 区块信息
    block_heights: Dict[str, int] = field(default_factory=dict)  # 各板块确认高度
    
    # 备注
    memo: str = ""
    
    def __post_init__(self):
        if not self.transfer_id:
            self.transfer_id = self._generate_id()
        if not self.created_at:
            self.created_at = time.time()
    
    def _generate_id(self) -> str:
        data = f"{self.from_address}{self.to_address}{self.amount}{time.time()}"
        return f"TX_{hashlib.sha256(data.encode()).hexdigest()[:12]}"
    
    def to_dict(self) -> Dict:
        return {
            "transfer_id": self.transfer_id,
            "from_address": self.from_address,
            "to_address": self.to_address,
            "amount": self.amount,
            "fee": self.fee,
            "signature": self.signature,
            "status": self.status.value,
            "created_at": self.created_at,
            "confirmed_at": self.confirmed_at,
            "required_witnesses": self.required_witnesses,
            "witness_sectors": self.witness_sectors,
            "witnesses": self.witnesses,
            "block_heights": self.block_heights,
            "memo": self.memo,
        }


class MainTransferEngine:
    """
    MAIN 转账引擎 - 强制双见证
    
    根据 DR-5，所有 MAIN 转账必须通过双见证验证。
    见证节点来自不同板块，确保交易的多方确认。
    """
    
    # 可用的见证板块（初始内置，运行时从板块注册表动态获取）
    _INITIAL_WITNESS_SECTORS = ["H100", "RTX4090", "RTX3080", "CPU", "GENERAL"]
    
    @property
    def WITNESS_SECTORS(self) -> list:
        """动态获取活跃板块列表"""
        try:
            from core.sector_coin import get_sector_registry
            return get_sector_registry().get_active_sectors()
        except Exception:
            return list(self._INITIAL_WITNESS_SECTORS)
    
    # 配置
    MIN_WITNESSES = 2                # 最少见证数
    LARGE_TRANSFER_THRESHOLD = 1000  # 大额转账阈值
    LARGE_TRANSFER_WITNESSES = 3     # 大额需要更多见证
    WITNESS_TIMEOUT = 300            # 见证超时（秒）— 跨区域需要足够时间
    
    def __init__(self, db_path: str = "data/main_transfers.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        
        # 见证引擎
        self.witness_engine = DoubleWitnessEngine()
        
        # 待处理队列
        self._pending_lock = threading.Lock()
    
    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()
    
    def _init_db(self):
        with self._conn() as conn:
            # 转账记录表
            conn.execute("""
                CREATE TABLE IF NOT EXISTS transfers (
                    transfer_id TEXT PRIMARY KEY,
                    from_address TEXT NOT NULL,
                    to_address TEXT NOT NULL,
                    amount REAL NOT NULL,
                    fee REAL NOT NULL,
                    signature TEXT,
                    public_key TEXT,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    created_at REAL NOT NULL,
                    confirmed_at REAL,
                    required_witnesses INTEGER DEFAULT 2,
                    witness_sectors TEXT,
                    witnesses TEXT,
                    block_heights TEXT,
                    memo TEXT
                )
            """)
            
            # 索引
            conn.execute("CREATE INDEX IF NOT EXISTS idx_transfers_from ON transfers(from_address)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_transfers_to ON transfers(to_address)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_transfers_status ON transfers(status)")
    
    def create_transfer(
        self,
        from_address: str,
        to_address: str,
        amount: float,
        fee: float = 0.01,
        signature: str = "",
        public_key: str = "",
        memo: str = "",
    ) -> Tuple[bool, str, Optional[MainTransfer]]:
        """
        创建 MAIN 转账（启动双见证流程）
        
        Args:
            from_address: 发送方地址
            to_address: 接收方地址
            amount: 转账金额
            fee: 手续费
            signature: 发送方签名
            public_key: 发送方公钥
            memo: 备注
        
        Returns:
            (success, message, transfer)
        """
        # 验证参数
        if amount <= 0:
            return False, "金额必须大于 0", None
        
        if fee < 0:
            return False, "手续费不能为负", None
        
        if from_address == to_address:
            return False, "不能转账给自己", None
        
        # 验证签名（DR-13: 真实 ECDSA 签名验证）
        if not signature:
            return False, "缺少签名", None
        
        if not public_key:
            return False, "缺少公钥", None
        
        # 真实签名验证（使用统一的 DER 编码，与 crypto.py ECDSASigner.sign 一致）
        try:
            from core.crypto import ECDSASigner
            sig_data = f"{from_address}{to_address}{amount}{fee}".encode()
            if not ECDSASigner.verify(
                bytes.fromhex(public_key), sig_data, bytes.fromhex(signature)
            ):
                return False, "签名验证失败", None
        except Exception as e:
            return False, f"签名验证失败: {e}", None
        
        # 预检查余额（避免见证完成后才发现余额不足）
        try:
            from core.utxo_store import get_utxo_store
            utxo_store = get_utxo_store()
            balance = utxo_store.get_balance(from_address, "MAIN")
            required = amount + fee
            if balance < required:
                return False, f"余额不足: 需要 {required} MAIN, 可用 {balance}", None
        except Exception:
            pass  # UTXO 不可用时跳过预检，执行时还会再查
        
        # 确定见证数
        required = self.MIN_WITNESSES
        if amount >= self.LARGE_TRANSFER_THRESHOLD:
            required = self.LARGE_TRANSFER_WITNESSES
        
        # 选择见证板块
        witness_sectors = self._select_witness_sectors(required)
        
        # 创建转账记录
        transfer = MainTransfer(
            transfer_id="",
            from_address=from_address,
            to_address=to_address,
            amount=amount,
            fee=fee,
            signature=signature,
            public_key=public_key,
            status=TransferStatus.WITNESSING,
            required_witnesses=required,
            witness_sectors=witness_sectors,
            memo=memo,
        )
        
        # 保存到数据库
        self._save_transfer(transfer)
        
        # 广播到见证板块
        self._broadcast_to_witnesses(transfer)
        
        return True, f"转账已创建，等待 {required} 个板块见证", transfer
    
    def _select_witness_sectors(self, count: int) -> List[str]:
        """选择见证板块（使用密码学安全随机数）"""
        import secrets
        sectors = self.WITNESS_SECTORS.copy()
        # 使用 Fisher-Yates shuffle with secrets
        for i in range(len(sectors) - 1, 0, -1):
            j = secrets.randbelow(i + 1)
            sectors[i], sectors[j] = sectors[j], sectors[i]
        return sectors[:min(count, len(sectors))]
    
    def _broadcast_to_witnesses(self, transfer: MainTransfer):
        """广播转账到见证板块"""
        # 转换为 MainTransaction 格式
        tx = MainTransaction(
            tx_id=transfer.transfer_id,
            tx_type=TransactionType.TRANSFER,
            from_address=transfer.from_address,
            to_address=transfer.to_address,
            amount=transfer.amount,
            fee=transfer.fee,
            timestamp=transfer.created_at,
            signature=transfer.signature,
            public_key=transfer.public_key,
            witnesses_required=transfer.required_witnesses,
        )
        
        # 提交到双见证引擎
        self.witness_engine.submit_transaction(tx)
    
    def add_witness(
        self,
        transfer_id: str,
        sector: str,
        witness_id: str,
        witness_signature: str,
        block_height: int = 0,
        verified: bool = False,
        reason: str = "",
    ) -> Tuple[bool, str]:
        """
        添加见证记录
        
        Args:
            transfer_id: 转账 ID
            sector: 见证板块
            witness_id: 见证节点 ID
            witness_signature: 见证签名
            block_height: 见证区块高度
            verified: 验证结果
            reason: 拒绝原因（如有）
        
        Returns:
            (success, message)
        """
        transfer = self.get_transfer(transfer_id)
        if not transfer:
            return False, "转账不存在"
        
        if transfer.status not in [TransferStatus.PENDING, TransferStatus.WITNESSING]:
            return False, f"转账状态不允许见证: {transfer.status.value}"
        
        # 检查是否已被此板块见证
        existing_sectors = [w.get("sector") for w in transfer.witnesses]
        if sector in existing_sectors:
            return False, f"板块 {sector} 已见证"
        
        # 添加见证记录
        witness_record = {
            "sector": sector,
            "witness_id": witness_id,
            "signature": witness_signature,
            "block_height": block_height,
            "verified": verified,
            "reason": reason,
            "timestamp": time.time(),
        }
        transfer.witnesses.append(witness_record)
        
        if block_height > 0:
            transfer.block_heights[sector] = block_height
        
        # 检查是否达到双见证
        confirmed_count = sum(1 for w in transfer.witnesses if w.get("verified", False))
        rejected_count = sum(1 for w in transfer.witnesses if not w.get("verified", True))
        
        if confirmed_count >= transfer.required_witnesses:
            transfer.status = TransferStatus.CONFIRMED
            transfer.confirmed_at = time.time()
            self._execute_transfer(transfer)
            # 检查执行结果：_execute_transfer 可能将状态改为 FAILED
            if transfer.status == TransferStatus.FAILED:
                message = "双见证达成但执行失败（余额不足或系统错误）"
            else:
                message = "双见证达成，转账已确认"
        elif rejected_count >= transfer.required_witnesses:
            transfer.status = TransferStatus.REJECTED
            message = "见证被拒绝，转账失败"
        else:
            remaining = transfer.required_witnesses - confirmed_count
            message = f"见证已记录，还需 {remaining} 个板块确认"
        
        self._save_transfer(transfer)
        
        return True, message
    
    def _execute_transfer(self, transfer: MainTransfer):
        """执行转账（双见证后）— 实际完成余额变动"""
        import logging
        logger = logging.getLogger(__name__)
        
        try:
            # 使用 UTXO 存储系统完成实际转账
            from core.utxo_store import get_utxo_store
            utxo_store = get_utxo_store()
            
            # 获取当前链高度，确保 UTXO 以 unspent 状态创建（而非 pending）
            # block_height >= 0 → 状态为 unspent；-1 → pending（永不可花费）
            block_height = 0
            try:
                with utxo_store._conn() as conn:
                    row = conn.execute("SELECT MAX(block_height) FROM utxos").fetchone()
                    if row and row[0] is not None and row[0] >= 0:
                        block_height = row[0]
            except Exception:
                block_height = 0  # 回退到 0，仍然 >= 0 → 状态为 unspent
            
            result = utxo_store.create_transfer(
                from_address=transfer.from_address,
                to_address=transfer.to_address,
                amount=transfer.amount,
                fee=transfer.fee,
                sector="MAIN",
                block_height=block_height,
                signature=getattr(transfer, 'signature', ''),
                public_key=getattr(transfer, 'public_key', ''),
            )
            
            if result.get("success"):
                logger.info(
                    f"[TRANSFER] 转账执行成功: {transfer.transfer_id} "
                    f"{transfer.from_address} -> {transfer.to_address} "
                    f"amount={transfer.amount} fee={transfer.fee} "
                    f"txid={result.get('txid')}"
                )
            else:
                # 余额不足或其他失败 — 标记转账失败
                transfer.status = TransferStatus.FAILED
                logger.error(
                    f"[TRANSFER] 转账执行失败: {transfer.transfer_id} "
                    f"reason={result.get('error', 'unknown')}"
                )
        except ImportError:
            # UTXO 存储不可用时的降级处理
            logger.error(f"[TRANSFER] UTXO store unavailable, transfer {transfer.transfer_id} cannot execute")
            transfer.status = TransferStatus.FAILED
        except Exception as e:
            logger.error(f"[TRANSFER] 转账执行异常: {transfer.transfer_id} error={e}")
            transfer.status = TransferStatus.FAILED
    
    def _save_transfer(self, transfer: MainTransfer):
        """保存转账记录"""
        with self._conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO transfers
                (transfer_id, from_address, to_address, amount, fee,
                 signature, public_key, status, created_at, confirmed_at,
                 required_witnesses, witness_sectors, witnesses, block_heights, memo)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                transfer.transfer_id,
                transfer.from_address,
                transfer.to_address,
                transfer.amount,
                transfer.fee,
                transfer.signature,
                transfer.public_key,
                transfer.status.value,
                transfer.created_at,
                transfer.confirmed_at,
                transfer.required_witnesses,
                json.dumps(transfer.witness_sectors),
                json.dumps(transfer.witnesses),
                json.dumps(transfer.block_heights),
                transfer.memo,
            ))
    
    def get_transfer(self, transfer_id: str) -> Optional[MainTransfer]:
        """获取转账记录"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM transfers WHERE transfer_id = ?", (transfer_id,)
            ).fetchone()
            
            if not row:
                return None
            
            return MainTransfer(
                transfer_id=row['transfer_id'],
                from_address=row['from_address'],
                to_address=row['to_address'],
                amount=row['amount'],
                fee=row['fee'],
                signature=row['signature'] or "",
                public_key=row['public_key'] or "",
                status=TransferStatus(row['status']),
                created_at=row['created_at'],
                confirmed_at=row['confirmed_at'],
                required_witnesses=row['required_witnesses'],
                witness_sectors=json.loads(row['witness_sectors'] or "[]"),
                witnesses=json.loads(row['witnesses'] or "[]"),
                block_heights=json.loads(row['block_heights'] or "{}"),
                memo=row['memo'] or "",
            )
    
    def get_pending_transfers(self) -> List[MainTransfer]:
        """获取待见证转账"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT transfer_id FROM transfers WHERE status IN ('PENDING', 'WITNESSING')"
            ).fetchall()
            
            return [self.get_transfer(row['transfer_id']) for row in rows]
    
    def get_transfers_by_address(self, address: str, 
                                  limit: int = 50) -> List[MainTransfer]:
        """获取地址相关转账"""
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT transfer_id FROM transfers 
                WHERE from_address = ? OR to_address = ?
                ORDER BY created_at DESC
                LIMIT ?
            """, (address, address, limit)).fetchall()
            
            return [self.get_transfer(row['transfer_id']) for row in rows]
    
    def check_timeout(self):
        """检查超时转账"""
        current_time = time.time()
        pending = self.get_pending_transfers()
        
        for transfer in pending:
            if current_time - transfer.created_at > self.WITNESS_TIMEOUT:
                transfer.status = TransferStatus.FAILED
                self._save_transfer(transfer)


# ============== 便捷函数 ==============

_engine: Optional[MainTransferEngine] = None

def get_transfer_engine() -> MainTransferEngine:
    """获取全局转账引擎"""
    global _engine
    if _engine is None:
        _engine = MainTransferEngine()
    return _engine


def transfer_main(
    from_address: str,
    to_address: str,
    amount: float,
    signature: str,
    public_key: str = "",
    fee: float = 0.01,
    memo: str = "",
) -> Tuple[bool, str]:
    """
    MAIN 币转账（强制双见证）
    
    这是推荐的 MAIN 转账接口，自动启动双见证流程。
    
    Args:
        from_address: 发送方地址
        to_address: 接收方地址
        amount: 转账金额
        signature: 发送方签名
        public_key: 发送方公钥
        fee: 手续费（默认 0.01）
        memo: 备注
    
    Returns:
        (success, message)
    
    Example:
        success, msg = transfer_main(
            from_address="MAIN_abc...",
            to_address="MAIN_xyz...",
            amount=100.0,
            signature="<signature>",
            public_key="<public_key>"
        )
    """
    engine = get_transfer_engine()
    success, message, transfer = engine.create_transfer(
        from_address=from_address,
        to_address=to_address,
        amount=amount,
        fee=fee,
        signature=signature,
        public_key=public_key,
        memo=memo,
    )
    return success, message


# ============== 测试 ==============

if __name__ == "__main__":
    import os
    from core.crypto import ECDSASigner

    print("=" * 60)
    print("MAIN Transfer Dual-Witness System Test")
    print("=" * 60)

    # Clean up test data
    test_db = "data/test_main_transfers.db"
    if os.path.exists(test_db):
        os.remove(test_db)

    engine = MainTransferEngine(test_db)
    
    # Generate test keypair
    sender_keypair = ECDSASigner.generate_keypair()
    
    # 1. Create transfer
    print("\n[1] Creating MAIN transfer...")
    from_addr = "MAIN_alice123"
    to_addr = "MAIN_bob456"
    amount = 100.0
    fee = 0.01
    
    # Generate real ECDSA signature
    sig_data = f"{from_addr}{to_addr}{amount}{fee}".encode()
    signature = ECDSASigner.sign(sender_keypair.private_key, sig_data).hex()
    
    success, msg, transfer = engine.create_transfer(
        from_address=from_addr,
        to_address=to_addr,
        amount=amount,
        fee=fee,
        signature=signature,
        public_key=sender_keypair.public_key.hex(),
        memo="Test transfer",
    )
    print(f"    {msg}")
    if transfer:
        print(f"    Transfer ID: {transfer.transfer_id}")
        print(f"    Required witnesses: {transfer.required_witnesses} sectors")
        print(f"    Witness sectors: {transfer.witness_sectors}")
    
    # 2. Add first witness
    print("\n[2] Adding first sector witness...")
    if transfer:
        sector1 = transfer.witness_sectors[0]
        success, msg = engine.add_witness(
            transfer_id=transfer.transfer_id,
            sector=sector1,
            witness_id=f"witness_{sector1}_001",
            witness_signature="witness_sig_1",
            block_height=100,
            verified=True,
        )
        print(f"    Sector {sector1}: {msg}")
        
        # 3. Check status
        transfer = engine.get_transfer(transfer.transfer_id)
        print(f"    Current status: {transfer.status.value}")
        
        # 4. Add second witness
        print("\n[3] Adding second sector witness...")
        sector2 = transfer.witness_sectors[1] if len(transfer.witness_sectors) > 1 else "RTX3080"
        success, msg = engine.add_witness(
            transfer_id=transfer.transfer_id,
            sector=sector2,
            witness_id=f"witness_{sector2}_001",
            witness_signature="witness_sig_2",
            block_height=101,
            verified=True,
        )
        print(f"    Sector {sector2}: {msg}")
        
        # 5. Check final status
        transfer = engine.get_transfer(transfer.transfer_id)
        print(f"\n[4] Final status: {transfer.status.value}")
        print(f"    Confirmed at: {transfer.confirmed_at}")
        print(f"    Block heights: {transfer.block_heights}")
    
    # 6. Large transfer test
    print("\n[5] Large transfer test (requires 3 witnesses)...")
    whale_keypair = ECDSASigner.generate_keypair()
    whale_addr = "MAIN_whale"
    receiver_addr = "MAIN_receiver"
    large_amount = 2000.0
    large_fee = 1.0
    
    large_sig_data = f"{whale_addr}{receiver_addr}{large_amount}{large_fee}".encode()
    large_signature = ECDSASigner.sign(whale_keypair.private_key, large_sig_data).hex()
    
    success, msg, large_transfer = engine.create_transfer(
        from_address=whale_addr,
        to_address=receiver_addr,
        amount=large_amount,
        fee=large_fee,
        signature=large_signature,
        public_key=whale_keypair.public_key.hex(),
        memo="Large transfer test",
    )
    print(f"    {msg}")
    if large_transfer:
        print(f"    Large transfer ID: {large_transfer.transfer_id}")
        print(f"    Required witnesses: {large_transfer.required_witnesses} sectors")
    
    print("\n" + "=" * 60)
    print("Test completed")
    print("=" * 60)
