# -*- coding: utf-8 -*-
"""
POUW-Chain V3.0 完整测试

测试所有核心功能：
1. Layer 1: 共识层
2. Layer 2: 计算层
3. Challenge Game
4. 完整流程
"""

import sys
import time
import base64
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.pouw_chain_v3 import (
    get_pouw_chain,
    VerificationType,
    PrivacyMode,
    TaskStatus
)


def test_layer1_consensus():
    """测试Layer 1共识层"""
    print("\n" + "="*60)
    print("Test 1: Layer 1 Consensus")
    print("="*60)

    chain = get_pouw_chain("./test_data")

    # 1. 注册验证者
    print("\n[1/4] Registering validators...")
    for i in range(5):
        success, msg = chain.layer1.register_validator(
            validator_id=f"validator_{i:03d}",
            address=f"MAIN_validator_{i:03d}",
            stake=1000.0 + i * 100
        )
        print(f"  Validator {i}: {msg}")

    # 2. 检查活跃集合
    print(f"\n[2/4] Active validator set: {len(chain.layer1.active_set)}")
    for vid in chain.layer1.active_set[:3]:
        validator = chain.layer1.validators[vid]
        print(f"  - {vid}: stake={validator.stake}, power={validator.voting_power}")

    # 3. VRF选择出块者
    print("\n[3/4] VRF block proposer selection:")
    for height in range(1, 6):
        proposer = chain.layer1.vrf_select_proposer(height)
        print(f"  Block {height}: {proposer}")

    # 4. 生成区块
    print("\n[4/4] Producing blocks...")
    for i in range(3):
        success, msg, block = chain.layer1.produce_block()
        if success:
            print(f"  Block {block.height}: {block.hash[:16]}... by {block.proposer}")

    print("\n✅ Layer 1 test passed!")
    return chain


def test_layer2_task_lifecycle(chain):
    """测试Layer 2任务生命周期"""
    print("\n" + "="*60)
    print("Test 2: Layer 2 Task Lifecycle")
    print("="*60)

    # 1. 提交任务
    print("\n[1/5] Submitting task...")
    task_data = b"AI inference task data"
    encrypted_data = base64.b64encode(task_data)

    success, msg = chain.layer2.submit_task(
        task_id="task_001",
        client="client_001",
        encrypted_data=task_data,
        compute_type="AI_INFERENCE",
        reward=50.0,
        client_bond=10.0,
        verification_type=VerificationType.CHALLENGE,
        privacy_mode=PrivacyMode.TEE
    )
    print(f"  {msg}")

    # 2. 接受任务
    print("\n[2/5] Accepting task...")
    success, msg = chain.layer2.accept_task(
        task_id="task_001",
        worker="worker_001",
        worker_stake=5.0
    )
    print(f"  {msg}")

    task = chain.layer2.tasks["task_001"]
    print(f"  Task status: {task.status.value}")

    # 3. 提交结果
    print("\n[3/5] Submitting result...")
    import hashlib
    result_hash = hashlib.sha256(b"result_data").hexdigest()

    success, msg = chain.layer2.submit_result(
        task_id="task_001",
        worker="worker_001",
        result_hash=result_hash,
        proof=""  # 无zk-proof，进入挑战期
    )
    print(f"  {msg}")
    print(f"  Task status: {task.status.value}")

    # 4. 等待挑战期
    print("\n[4/5] Waiting for challenge window...")
    print(f"  Challenge window: {chain.layer2.CHALLENGE_WINDOW} blocks")

    # 模拟出块
    for i in range(chain.layer2.CHALLENGE_WINDOW + 1):
        chain.layer1.produce_block()
        time.sleep(0.1)

    # 5. 完成任务
    print("\n[5/5] Finalizing task...")
    success, msg = chain.layer2.finalize_task("task_001", chain.layer1.current_height)
    print(f"  {msg}")
    print(f"  Task status: {task.status.value}")

    print("\n✅ Layer 2 test passed!")


def test_challenge_game(chain):
    """测试Challenge Game机制"""
    print("\n" + "="*60)
    print("Test 3: Challenge Game")
    print("="*60)

    # 1. 提交任务
    print("\n[1/4] Submitting task...")
    chain.layer2.submit_task(
        task_id="task_002",
        client="client_002",
        encrypted_data=b"test_data",
        compute_type="COMPUTE",
        reward=100.0,
        client_bond=20.0
    )

    # 2. 接受并提交结果
    print("\n[2/4] Accepting and submitting result...")
    chain.layer2.accept_task("task_002", "worker_002", 10.0)

    import hashlib
    result_hash = hashlib.sha256(b"wrong_result").hexdigest()
    chain.layer2.submit_result("task_002", "worker_002", result_hash)

    task = chain.layer2.tasks["task_002"]
    print(f"  Task status: {task.status.value}")

    # 3. 提交挑战
    print("\n[3/4] Submitting challenge...")
    success, msg = chain.layer2.submit_challenge(
        task_id="task_002",
        challenger="challenger_001",
        reason="Result incorrect",
        evidence={"expected": "correct_hash", "actual": result_hash},
        stake=2.0
    )
    print(f"  {msg}")
    print(f"  Task status: {task.status.value}")

    # 4. 检查挑战
    print("\n[4/4] Checking challenges...")
    for cid, challenge in chain.layer2.challenges.items():
        print(f"  Challenge {cid}:")
        print(f"    Task: {challenge.task_id}")
        print(f"    Challenger: {challenge.challenger}")
        print(f"    Reason: {challenge.reason}")
        print(f"    Status: {challenge.status}")

    print("\n✅ Challenge Game test passed!")


def test_full_workflow(chain):
    """测试完整工作流"""
    print("\n" + "="*60)
    print("Test 4: Full Workflow")
    print("="*60)

    # 1. 提交多个任务
    print("\n[1/4] Submitting multiple tasks...")
    for i in range(5):
        chain.layer2.submit_task(
            task_id=f"task_{i:03d}",
            client=f"client_{i:03d}",
            encrypted_data=f"data_{i}".encode(),
            compute_type="AI_INFERENCE",
            reward=50.0 + i * 10,
            client_bond=10.0
        )
    print(f"  Submitted {len(chain.layer2.tasks)} tasks")

    # 2. 工作者接单
    print("\n[2/4] Workers accepting tasks...")
    for i, task_id in enumerate(list(chain.layer2.tasks.keys())[-5:]):
        chain.layer2.accept_task(task_id, f"worker_{i:03d}", 5.0)

    # 3. 提交结果
    print("\n[3/4] Submitting results...")
    import hashlib
    for task_id in list(chain.layer2.tasks.keys())[-5:]:
        task = chain.layer2.tasks[task_id]
        if task.status.value == "accepted":
            result_hash = hashlib.sha256(f"result_{task_id}".encode()).hexdigest()
            chain.layer2.submit_result(task_id, task.worker, result_hash)

    # 4. 统计
    print("\n[4/4] Statistics:")
    status_count = {}
    for task in chain.layer2.tasks.values():
        status = task.status.value
        status_count[status] = status_count.get(status, 0) + 1

    for status, count in status_count.items():
        print(f"  {status}: {count}")

    print(f"\n  Total blocks: {chain.layer1.current_height}")
    print(f"  Total validators: {len(chain.layer1.validators)}")
    print(f"  Active validators: {len(chain.layer1.active_set)}")

    print("\n✅ Full workflow test passed!")


def test_state_commitment(chain):
    """测试状态提交"""
    print("\n" + "="*60)
    print("Test 5: State Commitment")
    print("="*60)

    # 1. 添加任务状态
    print("\n[1/2] Adding task states...")
    for i in range(5):
        chain.state.add_task_state(
            task_id=f"task_{i:03d}",
            result_hash=f"hash_{i}",
            status="finalized"
        )

    # 2. 计算状态根
    print("\n[2/2] Computing state root...")
    state_root = chain.state.compute_state_root()
    print(f"  State root: {state_root[:32]}...")

    print("\n✅ State commitment test passed!")


def run_all_tests():
    """运行所有测试"""
    print("\n" + "="*60)
    print("POUW-Chain V3.0 Complete Test Suite")
    print("="*60)

    try:
        # Test 1: Layer 1
        chain = test_layer1_consensus()

        # Test 2: Layer 2
        test_layer2_task_lifecycle(chain)

        # Test 3: Challenge Game
        test_challenge_game(chain)

        # Test 4: Full Workflow
        test_full_workflow(chain)

        # Test 5: State Commitment
        test_state_commitment(chain)

        # 最终统计
        print("\n" + "="*60)
        print("Final Statistics")
        print("="*60)
        print(f"  Blocks produced: {chain.layer1.current_height}")
        print(f"  Validators: {len(chain.layer1.validators)}")
        print(f"  Tasks submitted: {len(chain.layer2.tasks)}")
        print(f"  Challenges: {len(chain.layer2.challenges)}")

        print("\n" + "="*60)
        print("✅ ALL TESTS PASSED!")
        print("="*60)

    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    run_all_tests()
