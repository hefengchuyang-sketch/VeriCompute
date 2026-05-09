# -*- coding: utf-8 -*-
"""
确定性加权 Proposer 选择器

替代 ``random.seed(...)`` + ``random.uniform(...)`` 的写法。
旧写法两个问题:
  1. ``random.seed`` 污染全局随机状态，影响其它线程/库
  2. 给定相同 height 时仍会因为浮点 uniform 顺序与候选者排序不稳而漂移

本模块给定相同 ``(candidates, height, epoch_seed, parent_hash)``
必然返回相同结果，且不触碰 ``random`` 全局状态。

短期定位是确定性选择器；中期可被 VRF 选择器在保持相同接口下替换。

参考: docs/TECHNICAL_REVIEW_AND_CONSENSUS_RECOMMENDATIONS_2026-05-09.md §8.3
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence


@dataclass(frozen=True)
class ProposerCandidate:
    """候选 proposer 的最小描述。

    weight 必须为正整数。浮点权重必须先量化为整数（例如乘 1e6 后取整），
    以避免跨平台浮点累加误差导致选择漂移。
    """

    node_id: str
    address: str
    weight: int

    def __post_init__(self) -> None:
        if not isinstance(self.weight, int):
            raise TypeError(
                f"ProposerCandidate.weight 必须是整数，收到 {type(self.weight).__name__}"
            )
        if self.weight <= 0:
            raise ValueError(
                f"ProposerCandidate.weight 必须为正，node_id={self.node_id!r} weight={self.weight}"
            )
        if not self.node_id:
            raise ValueError("ProposerCandidate.node_id 不能为空")


@dataclass(frozen=True)
class ProposerSelectionResult:
    """选择结果。

    seed: 实际进入 sha256 的 seed_material 字符串，便于审计/重放。
    score: sha256(seed_material) 取整后落在 [0, total_weight) 的命中点。
    """

    selected_node_id: str
    selected_address: str
    seed: str
    score: int
    total_weight: int


def _sorted_candidates(candidates: Iterable[ProposerCandidate]) -> list[ProposerCandidate]:
    """按 node_id 升序排序，确保不同节点构造的列表得到同一顺序。

    报告要求: 候选者排序必须固定（按 node_id 升序）。
    """
    return sorted(candidates, key=lambda c: c.node_id)


def _build_seed_material(height: int, epoch_seed: str, parent_hash: str) -> str:
    """生成 seed 字符串。所有字段强类型化，避免隐式转换出错。"""
    if height < 0:
        raise ValueError(f"height 必须非负，收到 {height}")
    return f"{int(height)}:{str(epoch_seed)}:{str(parent_hash)}"


def select_weighted_proposer(
    candidates: Sequence[ProposerCandidate],
    height: int,
    epoch_seed: str,
    parent_hash: str,
) -> Optional[ProposerSelectionResult]:
    """根据权重确定性选出一个 proposer。

    返回 None 当且仅当 candidates 为空。
    候选者按 node_id 升序遍历，第一个使累计权重超过 ``score`` 的胜出。

    本函数不调用 ``random.*``，对全局 random 状态完全无副作用。
    """
    if not candidates:
        return None

    ordered = _sorted_candidates(candidates)
    total_weight = sum(c.weight for c in ordered)
    if total_weight <= 0:
        return None

    seed_material = _build_seed_material(height, epoch_seed, parent_hash)
    digest = hashlib.sha256(seed_material.encode("utf-8")).hexdigest()
    # 用整数模运算保证跨平台一致；不依赖浮点
    score = int(digest, 16) % total_weight

    cumsum = 0
    for candidate in ordered:
        cumsum += candidate.weight
        if score < cumsum:
            return ProposerSelectionResult(
                selected_node_id=candidate.node_id,
                selected_address=candidate.address,
                seed=seed_material,
                score=score,
                total_weight=total_weight,
            )

    # 数学上不会走到这里（cumsum 终值 == total_weight，score < total_weight）
    # 仅作为对数值/排序异常的兜底，避免返回 None 误导上层
    last = ordered[-1]
    return ProposerSelectionResult(
        selected_node_id=last.node_id,
        selected_address=last.address,
        seed=seed_material,
        score=score,
        total_weight=total_weight,
    )
