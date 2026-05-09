# -*- coding: utf-8 -*-
"""
确定性 proposer 选择器测试。

覆盖报告 §8.3 的两条核心验收:
  1. 同输入得到同 proposer (确定性)
  2. proposer 选择不污染全局 random 状态

参考: docs/TECHNICAL_REVIEW_AND_CONSENSUS_RECOMMENDATIONS_2026-05-09.md §8.3
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.proposer_selection import (  # noqa: E402
    ProposerCandidate,
    ProposerSelectionResult,
    select_weighted_proposer,
)


def _candidates() -> list[ProposerCandidate]:
    return [
        ProposerCandidate(node_id="node_a", address="addr_a", weight=10),
        ProposerCandidate(node_id="node_b", address="addr_b", weight=20),
        ProposerCandidate(node_id="node_c", address="addr_c", weight=30),
    ]


def test_selection_is_deterministic() -> None:
    r1 = select_weighted_proposer(_candidates(), height=100, epoch_seed="e", parent_hash="p")
    r2 = select_weighted_proposer(_candidates(), height=100, epoch_seed="e", parent_hash="p")
    assert r1 == r2
    assert r1.total_weight == 60
    assert 0 <= r1.score < 60


def test_selection_does_not_touch_global_random() -> None:
    random.seed(123)
    expected = random.random()
    select_weighted_proposer(_candidates(), height=100, epoch_seed="e", parent_hash="p")
    random.seed(123)
    actual = random.random()
    assert actual == expected


def test_input_order_does_not_change_result() -> None:
    forward = _candidates()
    reverse = list(reversed(_candidates()))
    r1 = select_weighted_proposer(forward, height=42, epoch_seed="s", parent_hash="ph")
    r2 = select_weighted_proposer(reverse, height=42, epoch_seed="s", parent_hash="ph")
    assert r1 == r2


def test_different_height_can_change_proposer() -> None:
    """统计验证：扫一段 height 区间，应能选中至少 2 个不同候选者。"""
    seen: set[str] = set()
    for height in range(0, 200):
        r = select_weighted_proposer(
            _candidates(),
            height=height,
            epoch_seed="diversity_check",
            parent_hash="ph",
        )
        seen.add(r.selected_node_id)
    assert len(seen) >= 2


def test_weight_zero_candidate_rejected() -> None:
    with pytest.raises(ValueError):
        ProposerCandidate(node_id="x", address="a", weight=0)


def test_negative_weight_rejected() -> None:
    with pytest.raises(ValueError):
        ProposerCandidate(node_id="x", address="a", weight=-1)


def test_float_weight_rejected() -> None:
    with pytest.raises(TypeError):
        ProposerCandidate(node_id="x", address="a", weight=1.5)  # type: ignore[arg-type]


def test_empty_candidates_returns_none() -> None:
    r = select_weighted_proposer([], height=1, epoch_seed="e", parent_hash="p")
    assert r is None


def test_single_candidate_always_wins() -> None:
    only = [ProposerCandidate(node_id="solo", address="addr", weight=7)]
    for h in (0, 1, 999):
        r = select_weighted_proposer(only, height=h, epoch_seed="e", parent_hash="p")
        assert r.selected_node_id == "solo"
        assert r.selected_address == "addr"


def test_seed_string_appears_in_result() -> None:
    r = select_weighted_proposer(
        _candidates(), height=99, epoch_seed="my_epoch", parent_hash="my_parent"
    )
    assert "99" in r.seed
    assert "my_epoch" in r.seed
    assert "my_parent" in r.seed


def test_weight_distribution_is_proportional() -> None:
    """大权重候选者应在长程序内胜出更多次。"""
    cands = [
        ProposerCandidate(node_id="small", address="a", weight=1),
        ProposerCandidate(node_id="big", address="b", weight=99),
    ]
    counts = {"small": 0, "big": 0}
    for h in range(2000):
        r = select_weighted_proposer(cands, height=h, epoch_seed="e", parent_hash="p")
        counts[r.selected_node_id] += 1
    # big 权重 99/100，至少应占绝大多数；保留宽容区间避免偶发失败
    assert counts["big"] > counts["small"] * 10
