# -*- coding: utf-8 -*-
"""Consensus fallback policy tests."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.consensus import ChainParams, ConsensusEngine, ConsensusType  # noqa: E402


def _engine(tmp_path: Path) -> ConsensusEngine:
    engine = ConsensusEngine(
        node_id="test_node",
        sector="MAIN",
        log_fn=lambda *_args, **_kwargs: None,
        db_path=str(tmp_path / "chain.db"),
    )
    engine.configure_consensus_mode(mode="pouw_only", sbox_enabled=False)
    engine.pending_pouw.clear()
    engine.pending_transactions.clear()
    return engine


def test_idle_block_policy_allows_pow_when_pool_is_idle(tmp_path, monkeypatch) -> None:
    engine = _engine(tmp_path)
    engine.configure_fallback_policy("idle_block_only")
    monkeypatch.setattr(engine, "_auto_generate_pouw", lambda count=4: None)

    assert engine.select_consensus() == ConsensusType.POW
    assert engine.fallback_policy == "idle_block_only"


def test_idle_block_policy_blocks_pow_when_task_pool_is_not_idle(tmp_path, monkeypatch) -> None:
    engine = _engine(tmp_path)
    engine.configure_fallback_policy("idle_block_only")
    monkeypatch.setattr(engine, "_auto_generate_pouw", lambda count=4: None)
    engine.pending_transactions = [
        {"id": f"tx_{i}", "from": "a", "to": "b", "amount": 1}
        for i in range(ChainParams.TASK_POOL_SWITCH_THRESHOLD)
    ]

    assert engine.select_consensus() == ConsensusType.POUW


def test_disabled_policy_never_selects_pow(tmp_path, monkeypatch) -> None:
    engine = _engine(tmp_path)
    engine.configure_fallback_policy("disabled")
    monkeypatch.setattr(engine, "_auto_generate_pouw", lambda count=4: None)

    assert engine.select_consensus() == ConsensusType.POUW
    assert engine.allow_pow_fallback is False


def test_emergency_policy_allows_pow_even_when_pool_is_not_idle(tmp_path, monkeypatch) -> None:
    engine = _engine(tmp_path)
    engine.configure_fallback_policy("emergency_pow")
    monkeypatch.setattr(engine, "_auto_generate_pouw", lambda count=4: None)
    engine.pending_transactions = [
        {"id": f"tx_{i}", "from": "a", "to": "b", "amount": 1}
        for i in range(ChainParams.TASK_POOL_SWITCH_THRESHOLD)
    ]

    assert engine.select_consensus() == ConsensusType.POW
    assert engine.allow_pow_fallback is True


def test_invalid_policy_normalizes_to_idle_block_only(tmp_path) -> None:
    engine = _engine(tmp_path)

    assert engine.configure_fallback_policy("unknown") == "idle_block_only"
