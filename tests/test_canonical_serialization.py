# -*- coding: utf-8 -*-
"""
canonical 序列化与 hash 测试。

参考: docs/TECHNICAL_REVIEW_AND_CONSENSUS_RECOMMENDATIONS_2026-05-09.md §8.4
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.serialization import (  # noqa: E402
    canonical_block_hash,
    canonical_block_header,
    canonical_json,
    hash_canonical,
)


def test_canonical_json_is_sorted_and_compact() -> None:
    a = canonical_json({"b": 2, "a": 1, "c": 3})
    b = canonical_json({"c": 3, "a": 1, "b": 2})
    assert a == b
    assert b" " not in a  # separators=(",", ":") 不留空白
    parsed = json.loads(a.decode("utf-8"))
    assert parsed == {"a": 1, "b": 2, "c": 3}


def test_hash_canonical_field_order_independent() -> None:
    h1 = hash_canonical({"height": 1, "prev_hash": "abc", "nonce": 5})
    h2 = hash_canonical({"nonce": 5, "height": 1, "prev_hash": "abc"})
    assert h1 == h2


def test_hash_canonical_changes_when_value_changes() -> None:
    h1 = hash_canonical({"height": 1, "prev_hash": "abc"})
    h2 = hash_canonical({"height": 2, "prev_hash": "abc"})
    h3 = hash_canonical({"height": 1, "prev_hash": "abd"})
    assert len({h1, h2, h3}) == 3


def test_hash_canonical_distinguishes_field_split() -> None:
    """报告 §8.4: 字符串拼接 hash 字段边界不强 - canonical 应能区分。"""
    # 旧 f"{a}{b}" 风格无法区分 ("ab", "c") 与 ("a", "bc")
    h1 = hash_canonical({"x": "ab", "y": "c"})
    h2 = hash_canonical({"x": "a", "y": "bc"})
    assert h1 != h2


class _FakeBlock:
    """Block 字段子集，避免依赖完整 ConsensusEngine。"""

    def __init__(self) -> None:
        self.version = 1
        self.height = 100
        self.prev_hash = "p" * 64
        self.timestamp = 1_700_000_000.0
        self.merkle_root = "m" * 64
        self.consensus_type = type("CT", (), {"value": "POUW"})()
        self.miner_id = "node_a"
        self.miner_address = "addr_a"
        self.difficulty = 4
        self.nonce = 7
        self.sector = "MAIN"
        self.block_type = "task_block"
        self.sbox_hex = ""
        self.sbox_score = 0.0


def test_canonical_block_header_includes_required_fields() -> None:
    header = canonical_block_header(_FakeBlock())
    required = {
        "version", "height", "prev_hash", "timestamp",
        "merkle_root", "consensus_type", "proposer",
        "proposer_address", "difficulty", "nonce", "sector",
        "block_type",
    }
    assert required.issubset(header.keys())


def test_canonical_block_hash_stable_for_same_block() -> None:
    b1 = _FakeBlock()
    b2 = _FakeBlock()
    assert canonical_block_hash(b1) == canonical_block_hash(b2)


def test_canonical_block_hash_changes_with_nonce() -> None:
    b = _FakeBlock()
    h_before = canonical_block_hash(b)
    b.nonce = 8
    h_after = canonical_block_hash(b)
    assert h_before != h_after


def test_canonical_block_hash_includes_sbox_when_present() -> None:
    b1 = _FakeBlock()
    h_no_sbox = canonical_block_hash(b1)
    b2 = _FakeBlock()
    b2.sbox_hex = "deadbeef"
    b2.sbox_score = 0.875
    h_with_sbox = canonical_block_hash(b2)
    assert h_no_sbox != h_with_sbox
