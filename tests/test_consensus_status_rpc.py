# -*- coding: utf-8 -*-
"""Unit tests for chain_getConsensusStatus."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.rpc.models import RPCPermission  # noqa: E402
from core.rpc_service import NodeRPCService  # noqa: E402


class _FakeBlock:
    def __init__(self, height: int, hash_value: str, miner_id: str = "node_a") -> None:
        self.height = height
        self.hash = hash_value
        self.timestamp = 1_700_000_000 + height
        self.miner_id = miner_id

    def to_dict(self) -> dict:
        return {"height": self.height, "hash": self.hash, "miner_id": self.miner_id}


class _FakeConsensusEngine:
    FINALITY_THRESHOLD = 20

    def __init__(self) -> None:
        self.sector = "MAIN"
        self.consensus_mode = "sbox_primary"
        self.current_difficulty = 4
        self._sbox_mining_enabled = True
        self.pending_pouw = [{"task_id": "p1"}, {"task_id": "p2"}]
        self.pending_challenges = [{"challenge_id": "c1"}]
        self.chain = [_FakeBlock(i, f"hash_{i}") for i in range(0, 31)]

    def get_chain_height(self) -> int:
        return 30

    def get_finalized_height(self) -> int:
        return 10

    def get_latest_block(self) -> _FakeBlock:
        return self.chain[-1]

    def get_block_by_height(self, height: int) -> _FakeBlock:
        return self.chain[height]

    def get_chain_info(self) -> dict:
        return {
            "consensus_selected_distribution": {"SBOX_POUW": 3},
            "consensus_mined_distribution": {"SBOX_POUW": 2},
        }


def test_chain_get_consensus_status_registered_public() -> None:
    svc = NodeRPCService()
    assert svc.registry.has("chain_getConsensusStatus")
    assert svc.registry.get_permission("chain_getConsensusStatus") == RPCPermission.PUBLIC


def test_chain_get_consensus_status_fields() -> None:
    svc = NodeRPCService()
    svc.consensus_engine = _FakeConsensusEngine()

    result = svc._chain_get_consensus_status()

    assert result["height"] == 30
    assert result["finalizedHeight"] == 10
    assert result["consensusEngine"] == "_FakeConsensusEngine"
    assert result["consensusMode"] == "sbox_primary"
    assert result["fallbackPolicy"] == "legacy_guarded"
    assert result["currentProposer"] == "node_a"
    assert result["pendingTaskCount"] == 2
    assert result["pendingChallengeCount"] == 1
    assert result["lastFinalizedHash"] == "hash_10"
    assert result["lastBlockTime"] == 1_700_000_030
    assert result["sector"] == "MAIN"
    assert result["consensusSelectedDistribution"] == {"SBOX_POUW": 3}


def test_chain_get_consensus_status_without_engine() -> None:
    svc = NodeRPCService()
    svc.consensus_engine = None

    result = svc._chain_get_consensus_status()

    assert result["height"] == 0
    assert result["finalizedHeight"] == 0
    assert result["consensusEngine"] == ""
    assert result["lastFinalizedHash"] == ""
