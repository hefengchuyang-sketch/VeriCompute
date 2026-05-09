# -*- coding: utf-8 -*-
"""
Canonical 序列化与哈希工具

为协议对象提供稳定的 JSON 序列化和 SHA-256 哈希。
解决报告 §8.4 指出的字符串拼接 hash 字段边界不强问题。

使用规则:
- 任何需要上链、签名、写入事件日志的对象都应通过本模块计算 hash。
- 禁止直接 ``hashlib.sha256(f"{a}{b}{c}".encode()).hexdigest()``。
- 字段顺序与字段边界由 ``json.dumps(sort_keys=True, separators=(",", ":"))`` 强制。

参考: docs/TECHNICAL_REVIEW_AND_CONSENSUS_RECOMMENDATIONS_2026-05-09.md §8.4
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


def canonical_json(data: Mapping[str, Any]) -> bytes:
    """把 dict 序列化为稳定字节序列。

    - sort_keys=True 保证字段顺序与定义顺序无关
    - separators=(",", ":") 保证空白字符不影响 hash
    - ensure_ascii=False 允许非 ASCII 字段不被转义膨胀
    - encode("utf-8") 强制字节边界
    """
    return json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def hash_canonical(data: Mapping[str, Any]) -> str:
    """对 dict 计算稳定 SHA-256 十六进制摘要。"""
    return hashlib.sha256(canonical_json(data)).hexdigest()


def canonical_block_header(block: Any) -> dict:
    """从 Block 对象抽取标准头字段。

    报告 §8.4 推荐的 canonical header 字段集。
    Block 上不存在的字段以默认值占位，便于历史区块兼容。
    """
    consensus_type_value = getattr(block.consensus_type, "value", str(block.consensus_type))

    header = {
        "version": getattr(block, "version", 1),
        "height": int(block.height),
        "prev_hash": str(block.prev_hash),
        "timestamp": int(block.timestamp),
        "merkle_root": str(block.merkle_root),
        "state_root": getattr(block, "state_root", ""),
        "task_root": getattr(block, "task_root", ""),
        "consensus_type": consensus_type_value,
        "proposer": getattr(block, "miner_id", ""),
        "proposer_address": getattr(block, "miner_address", ""),
        "difficulty": int(getattr(block, "difficulty", 0)),
        "nonce": int(getattr(block, "nonce", 0)),
        "sector": getattr(block, "sector", "MAIN"),
        "block_type": getattr(block, "block_type", ""),
    }
    # S-Box 字段非空时纳入 hash，保持与旧 compute_hash 同等防篡改语义
    sbox_hex = getattr(block, "sbox_hex", "")
    if sbox_hex:
        header["sbox_hex"] = sbox_hex
        # 旧路径用 ":.6f" 格式化，这里用 round 保证 canonical_json 跨平台稳定
        header["sbox_score"] = round(float(getattr(block, "sbox_score", 0.0)), 6)
    return header


def canonical_block_hash(block: Any) -> str:
    """计算区块的 canonical hash（新版）。

    与 ``Block.compute_hash`` 不兼容，仅作为新协议路径使用。
    历史区块的链上 hash 仍由 ``compute_hash`` 决定。
    """
    return hash_canonical(canonical_block_header(block))
