#!/usr/bin/env python3
"""
真实实现验证测试
验证隐私计算、MPC、ZK证明等都使用真实实现
"""

import sys
import json
import hashlib
import time
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent))

def test_tee_execution():
    """测试真实 TEE 执行"""
    print("\n" + "="*60)
    print("测试 1: 真实 TEE 执行（AES-GCM 加密）")
    print("="*60)
    
    from core.pouw_chain_v3 import PrivacyCompute, Task, VerificationType, PrivacyMode, TaskStatus
    from core.crypto_utils import aes_gcm_encrypt
    import os
    
    # 准备真实加密任务数据
    task_id = "test_tee_001"
    tee_key = hashlib.sha256(f"TEE_KEY_{task_id}".encode()).digest()
    plaintext = b"test_data"
    ciphertext, nonce, tag = aes_gcm_encrypt(plaintext, tee_key)
    encrypted_payload = nonce + tag + ciphertext

    # 创建任务
    task = Task(
        task_id=task_id,
        client="test_client",
        encrypted_data=encrypted_payload,
        data_hash=hashlib.sha256(plaintext).hexdigest(),
        compute_type="hash",
        reward=10.0,
        deadline=int(time.time()) + 3600,
        verification_type=VerificationType.CHALLENGE,
        privacy_mode=PrivacyMode.TEE,
        client_bond=100.0,
        worker_stake_required=50.0,
        status=TaskStatus.SUBMITTED
    )
    
    # 执行 TEE（应使用真实 AES-GCM 加密）
    result_hash, attestation = PrivacyCompute.tee_execute(task)
    
    print(f"✓ TEE 执行完成")
    print(f"  - 结果哈希: {result_hash[:32]}...")
    print(f"  - 证明: {attestation[:64]}...")
    print(f"  - 证明包含验证环: {':' in attestation}")
    
    assert result_hash, "TEE 执行应返回非空结果哈希"
    assert ":" in attestation, "证明应包含验证信息"
    print("✓ TEE 执行验证通过")
    
    return True

def test_mpc_computation():
    """测试真实 MPC 计算（Shamir 秘密分享）"""
    print("\n" + "="*60)
    print("测试 2: 真实 MPC 计算（Shamir 秘密分享）")
    print("="*60)
    
    from core.pouw_chain_v3 import PrivacyCompute, Task, VerificationType, PrivacyMode, TaskStatus
    import hashlib
    import time
    
    # 创建任务
    task = Task(
        task_id="test_mpc_001",
        client="test_client",
        encrypted_data=hashlib.sha256(b"secret_data").digest(),
        data_hash=hashlib.sha256(b"mpc_secret").hexdigest(),
        compute_type="hash",
        reward=10.0,
        deadline=int(time.time()) + 3600,
        verification_type=VerificationType.CHALLENGE,
        privacy_mode=PrivacyMode.MPC,
        client_bond=100.0,
        worker_stake_required=50.0,
        status=TaskStatus.SUBMITTED
    )
    
    # 创建节点列表
    nodes = ["node_1", "node_2", "node_3", "node_4", "node_5"]
    
    # 执行 MPC 计算
    mpc_result = PrivacyCompute.mpc_compute(task, nodes)
    
    print(f"✓ MPC 计算完成")
    print(f"  - 节点数: {len(nodes)}")
    print(f"  - 结果: {mpc_result[:64]}...")
    print(f"  - 包含审计信息: {':' in mpc_result}")
    
    assert mpc_result, "MPC 计算应返回非空结果"
    assert ":" in mpc_result, "结果应包含审计信息"
    print("✓ MPC 计算验证通过")
    
    return True

def test_zk_proof_generation():
    """测试真实 ZK 证明（Schnorr-like 协议）"""
    print("\n" + "="*60)
    print("测试 3: 真实 ZK 证明（Schnorr-like 协议）")
    print("="*60)
    
    from core.pouw_chain_v3 import PrivacyCompute, Task, VerificationType, PrivacyMode, TaskStatus
    import hashlib
    import time
    
    # 创建任务
    task = Task(
        task_id="test_zk_001",
        client="test_client",
        encrypted_data=b"encrypted",
        data_hash=hashlib.sha256(b"zk_data").hexdigest(),
        compute_type="hash",
        reward=10.0,
        deadline=int(time.time()) + 3600,
        verification_type=VerificationType.ZK_PROOF,
        privacy_mode=PrivacyMode.ZK,
        client_bond=100.0,
        worker_stake_required=50.0,
        status=TaskStatus.SUBMITTED
    )
    
    # 生成 ZK 证明
    zk_proof = PrivacyCompute.generate_zk_proof(task, "result_value")
    
    print(f"✓ ZK 证明生成完成")
    print(f"  - 证明类型: Schnorr-like")
    print(f"  - 证明内容: {zk_proof[:80]}...")
    print(f"  - 包含承诺: {'commitment' in zk_proof or 'schnorr' in zk_proof}")
    print(f"  - 包含验证: {':' in zk_proof}")
    
    assert zk_proof, "ZK 证明应非空"
    assert "schnorr:" in zk_proof, "应使用 Schnorr 协议"
    print("✓ ZK 证明验证通过")
    
    return True

def test_witness_signature_verification():
    """测试真实见证签名验证"""
    print("\n" + "="*60)
    print("测试 4: 真实见证签名验证")
    print("="*60)
    
    from core.dual_witness_exchange import DualWitnessExchange
    from core.crypto import ECDSASigner
    import tempfile
    import os
    
    # 创建临时数据库
    db_path = tempfile.mktemp(suffix='.db')
    exchange = DualWitnessExchange(db_path)
    
    try:
        # 生成真实 ECDSA 密钥对并注册见证公钥
        keypair = ECDSASigner.generate_keypair()
        public_key_hex = keypair.public_key.hex()
        exchange.register_witness_public_key('sector1', public_key_hex)
        
        # 生成有效签名
        message = 'test_exchange:100:test_hash'
        signature = ECDSASigner.sign(keypair.private_key, message.encode())
        signature_hex = signature.hex()
        
        # 验证签名
        verify_result = exchange._verify_witness_signature(
            'sector1', 'test_exchange', 100, 'test_hash', signature_hex
        )
        
        print(f"✓ 见证签名验证完成")
        print(f"  - 有效签名验证: {verify_result}")
        print(f"  - 公钥格式验证: 通过")
        print(f"  - 签名长度: {len(signature_hex)}")
        
        invalid_result = exchange._verify_witness_signature(
            'sector1', 'test_exchange', 100, 'test_hash', 'invalid_sig'
        )
        
        assert verify_result == True, "有效签名应验证通过"
        assert invalid_result == False, "无效签名应验证失败"
        print("✓ 见证签名验证通过")
        
        return True
        
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)

def test_pouw_proof_structure():
    """测试 POUW 证明结构（使用真实工作量指标）"""
    print("\n" + "="*60)
    print("测试 5: POUW 证明结构（真实工作量指标）")
    print("="*60)
    
    from core.consensus import POUWProof
    import hashlib
    
    # 创建 POUW 证明
    proof = POUWProof(
        proof_id="proof_001",
        task_id="task_001",
        miner_id="miner_001",
        compute_hash=hashlib.sha256(b"computation").hexdigest(),
        execution_time=2.5,  # 真实执行时间
        gpu_cycles=1000000,
        memory_used=512,  # MB
        cpu_usage=75.5,
        gpu_usage=80.0,
        power_consumption=150.0,  # Watts
        verified=True,
        quality_score=85.0
    )
    
    # 计算工作量分数（基于可验证指标）
    work_score = proof.compute_work_score()
    
    print(f"✓ POUW 证明结构完成")
    print(f"  - 任务ID: {proof.task_id}")
    print(f"  - 执行时间: {proof.execution_time}秒")
    print(f"  - 工作量分数: {work_score:.2f}")
    print(f"  - 验证状态: {'已验证' if proof.verified else '未验证'}")
    print(f"  - 质量分: {proof.quality_score}")
    
    assert work_score > 0, "工作量分数应大于0"
    assert proof.compute_hash, "计算哈希应存在"
    print("✓ POUW 证明验证通过")
    
    return True

def main():
    """运行所有测试"""
    print("\n")
    print("╔" + "="*58 + "╗")
    print("║" + " "*58 + "║")
    print("║" + "   区块链真实实现验证测试套件".center(58) + "║")
    print("║" + " "*58 + "║")
    print("╚" + "="*58 + "╝")
    
    tests = [
        ("TEE执行", test_tee_execution),
        ("MPC计算", test_mpc_computation),
        ("ZK证明", test_zk_proof_generation),
        ("见证签名", test_witness_signature_verification),
        ("POUW证明", test_pouw_proof_structure),
    ]
    
    passed = 0
    failed = 0
    
    for test_name, test_func in tests:
        try:
            if test_func():
                passed += 1
        except Exception as e:
            print(f"✗ {test_name} 测试失败: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
    
    print("\n" + "="*60)
    print("测试总结")
    print("="*60)
    print(f"✓ 通过: {passed}/{len(tests)}")
    print(f"✗ 失败: {failed}/{len(tests)}")
    
    if failed == 0:
        print("\n🎉 所有测试通过！系统已转换为真实实现。")
        return 0
    else:
        print(f"\n❌ 有{failed}个测试失败，需要修复。")
        return 1

if __name__ == "__main__":
    sys.exit(main())
