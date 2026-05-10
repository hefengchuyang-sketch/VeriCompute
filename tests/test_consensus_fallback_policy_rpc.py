# -*- coding: utf-8 -*-
"""Tests for consensus fallback policy admin RPCs."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.rpc.models import RPCPermission  # noqa: E402
from core.rpc_service import NodeRPCService  # noqa: E402


class _FakeConsensusEngine:
    def __init__(self) -> None:
        self.fallback_policy = "idle_block_only"
        self.allow_pow_fallback = True
        self._audit = []

    def record_fallback_policy_change(self, actor: str, policy: str, reason: str = "") -> dict:
        event = {
            "actor": actor,
            "policy": policy,
            "reason": reason,
            "source": "rpc",
        }
        self._audit.append(event)
        self.fallback_policy = policy
        self.allow_pow_fallback = policy != "disabled"
        return event

    def get_fallback_policy_audit(self, limit: int = 50):
        return list(self._audit)[-limit:]


def test_chain_fallback_rpc_registration() -> None:
    svc = NodeRPCService()
    assert svc.registry.has("chain_setConsensusFallbackPolicy")
    assert svc.registry.get_permission("chain_setConsensusFallbackPolicy") == RPCPermission.ADMIN
    assert svc.registry.has("chain_getConsensusFallbackAudit")
    assert svc.registry.get_permission("chain_getConsensusFallbackAudit") == RPCPermission.ADMIN


def test_chain_set_fallback_policy_updates_audit() -> None:
    svc = NodeRPCService()
    svc.consensus_engine = _FakeConsensusEngine()

    result = svc._chain_set_consensus_fallback_policy(
        policy="emergency_pow",
        reason="incident response",
        auth_context={"user": "ops_admin", "is_admin": True},
    )

    assert result["status"] == "success"
    assert result["fallbackPolicy"] == "emergency_pow"
    assert result["allowPowFallback"] is True
    assert result["auditEvent"]["actor"] == "ops_admin"
    assert result["auditEvent"]["reason"] == "incident response"
    assert svc.consensus_engine.get_fallback_policy_audit()[-1]["policy"] == "emergency_pow"


def test_chain_set_fallback_policy_rejects_invalid_values() -> None:
    svc = NodeRPCService()
    svc.consensus_engine = _FakeConsensusEngine()

    result = svc._chain_set_consensus_fallback_policy(
        policy="maybe_pow",
        auth_context={"user": "ops_admin", "is_admin": True},
    )

    assert result["status"] == "failed"
    assert result["message"] == "invalid_fallback_policy"


def test_chain_set_emergency_pow_requires_reason() -> None:
    svc = NodeRPCService()
    svc.consensus_engine = _FakeConsensusEngine()

    result = svc._chain_set_consensus_fallback_policy(
        policy="emergency_pow",
        reason="",
        auth_context={"user": "ops_admin", "is_admin": True},
    )

    assert result["status"] == "failed"
    assert result["message"] == "reason_required_for_emergency_pow"


def test_chain_get_fallback_audit_returns_recent_events() -> None:
    svc = NodeRPCService()
    svc.consensus_engine = _FakeConsensusEngine()
    svc.consensus_engine.record_fallback_policy_change("ops_admin", "disabled", "maintenance")

    result = svc._chain_get_consensus_fallback_audit(limit=10)

    assert result["status"] == "success"
    assert result["count"] == 1
    assert result["events"][0]["policy"] == "disabled"
