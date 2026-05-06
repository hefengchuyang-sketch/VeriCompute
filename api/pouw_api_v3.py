# -*- coding: utf-8 -*-
"""
POUW-Chain V3.0 API 接口

提供完整的REST API接口，符合技术白皮书规范
"""

from flask import Flask, request, jsonify
from typing import Dict, Any
import logging

from core.pouw_chain_v3 import (
    get_pouw_chain,
    VerificationType,
    PrivacyMode
)

logger = logging.getLogger(__name__)

app = Flask(__name__)
chain = get_pouw_chain()


# ============== 1. Layer 1 API ==============

@app.route('/api/v3/validator/register', methods=['POST'])
def register_validator():
    """
    注册验证者

    POST /api/v3/validator/register
    {
        "validator_id": "validator_001",
        "address": "MAIN_xxx",
        "stake": 1000.0
    }
    """
    data = request.json

    success, msg = chain.layer1.register_validator(
        validator_id=data['validator_id'],
        address=data['address'],
        stake=data['stake']
    )

    return jsonify({
        "success": success,
        "message": msg
    })


@app.route('/api/v3/validator/list', methods=['GET'])
def list_validators():
    """
    获取验证者列表

    GET /api/v3/validator/list
    """
    validators = []
    for vid, validator in chain.layer1.validators.items():
        validators.append({
            "id": validator.id,
            "address": validator.address,
            "stake": validator.stake,
            "voting_power": validator.voting_power,
            "status": validator.status.value,
            "blocks_produced": validator.blocks_produced
        })

    return jsonify({
        "validators": validators,
        "total": len(validators)
    })


@app.route('/api/v3/block/latest', methods=['GET'])
def get_latest_block():
    """
    获取最新区块

    GET /api/v3/block/latest
    """
    if chain.layer1.current_height == 0:
        return jsonify({"error": "No blocks yet"}), 404

    block = chain.layer1.blocks[chain.layer1.current_height]

    return jsonify({
        "height": block.height,
        "hash": block.hash,
        "parent_hash": block.parent_hash,
        "proposer": block.proposer,
        "timestamp": block.timestamp,
        "task_count": len(block.task_commitments)
    })


# ============== 2. Layer 2 API ==============

@app.route('/api/v3/task/submit', methods=['POST'])
def submit_task():
    """
    提交任务

    POST /api/v3/task/submit
    {
        "task_id": "task_001",
        "client": "client_001",
        "encrypted_data": "base64_encoded_data",
        "compute_type": "AI_INFERENCE",
        "reward": 50.0,
        "client_bond": 10.0,
        "verification_type": "challenge",
        "privacy_mode": "tee"
    }
    """
    data = request.json

    # 解码数据
    import base64
    encrypted_data = base64.b64decode(data['encrypted_data'])

    # 转换枚举
    verification_type = VerificationType[data.get('verification_type', 'CHALLENGE').upper()]
    privacy_mode = PrivacyMode[data.get('privacy_mode', 'TEE').upper()]

    success, msg = chain.layer2.submit_task(
        task_id=data['task_id'],
        client=data['client'],
        encrypted_data=encrypted_data,
        compute_type=data['compute_type'],
        reward=data['reward'],
        client_bond=data['client_bond'],
        verification_type=verification_type,
        privacy_mode=privacy_mode
    )

    return jsonify({
        "success": success,
        "message": msg
    })


@app.route('/api/v3/task/accept', methods=['POST'])
def accept_task():
    """
    接受任务

    POST /api/v3/task/accept
    {
        "task_id": "task_001",
        "worker": "worker_001",
        "worker_stake": 5.0
    }
    """
    data = request.json

    success, msg = chain.layer2.accept_task(
        task_id=data['task_id'],
        worker=data['worker'],
        worker_stake=data['worker_stake']
    )

    return jsonify({
        "success": success,
        "message": msg
    })


@app.route('/api/v3/task/submit_result', methods=['POST'])
def submit_result():
    """
    提交结果

    POST /api/v3/task/submit_result
    {
        "task_id": "task_001",
        "worker": "worker_001",
        "result_hash": "hash_of_result",
        "proof": "zk_proof_or_empty"
    }
    """
    data = request.json

    success, msg = chain.layer2.submit_result(
        task_id=data['task_id'],
        worker=data['worker'],
        result_hash=data['result_hash'],
        proof=data.get('proof', '')
    )

    return jsonify({
        "success": success,
        "message": msg
    })


@app.route('/api/v3/task/challenge', methods=['POST'])
def submit_challenge():
    """
    提交挑战

    POST /api/v3/task/challenge
    {
        "task_id": "task_001",
        "challenger": "challenger_001",
        "reason": "Result incorrect",
        "evidence": {"expected": "...", "actual": "..."},
        "stake": 2.0
    }
    """
    data = request.json

    success, msg = chain.layer2.submit_challenge(
        task_id=data['task_id'],
        challenger=data['challenger'],
        reason=data['reason'],
        evidence=data['evidence'],
        stake=data['stake']
    )

    return jsonify({
        "success": success,
        "message": msg
    })


@app.route('/api/v3/task/get/<task_id>', methods=['GET'])
def get_task(task_id: str):
    """
    获取任务详情

    GET /api/v3/task/get/<task_id>
    """
    if task_id not in chain.layer2.tasks:
        return jsonify({"error": "Task not found"}), 404

    task = chain.layer2.tasks[task_id]

    return jsonify({
        "task_id": task.task_id,
        "client": task.client,
        "worker": task.worker,
        "compute_type": task.compute_type,
        "reward": task.reward,
        "status": task.status.value,
        "verification_type": task.verification_type.value,
        "privacy_mode": task.privacy_mode.value,
        "result_hash": task.result_hash,
        "created_at": task.created_at,
        "finalized_at": task.finalized_at
    })


@app.route('/api/v3/task/list', methods=['GET'])
def list_tasks():
    """
    获取任务列表

    GET /api/v3/task/list?status=submitted&limit=10
    """
    status_filter = request.args.get('status')
    limit = int(request.args.get('limit', 100))

    tasks = []
    for task_id, task in chain.layer2.tasks.items():
        if status_filter and task.status.value != status_filter:
            continue

        tasks.append({
            "task_id": task.task_id,
            "client": task.client,
            "worker": task.worker,
            "compute_type": task.compute_type,
            "reward": task.reward,
            "status": task.status.value,
            "created_at": task.created_at
        })

        if len(tasks) >= limit:
            break

    return jsonify({
        "tasks": tasks,
        "total": len(tasks)
    })


# ============== 3. 统计 API ==============

@app.route('/api/v3/stats/overview', methods=['GET'])
def get_stats():
    """
    获取系统统计

    GET /api/v3/stats/overview
    """
    return jsonify({
        "layer1": {
            "current_height": chain.layer1.current_height,
            "validators": len(chain.layer1.validators),
            "active_validators": len(chain.layer1.active_set)
        },
        "layer2": {
            "total_tasks": len(chain.layer2.tasks),
            "pending_tasks": sum(1 for t in chain.layer2.tasks.values() if t.status.value == "submitted"),
            "finalized_tasks": sum(1 for t in chain.layer2.tasks.values() if t.status.value == "finalized"),
            "total_challenges": len(chain.layer2.challenges)
        }
    })


# ============== 4. 健康检查 ==============

@app.route('/health', methods=['GET'])
def health_check():
    """健康检查"""
    return jsonify({
        "status": "healthy",
        "version": "3.0",
        "running": chain.running
    })


# ============== 5. 启动服务 ==============

def start_api_server(host: str = '0.0.0.0', port: int = 8080):
    """启动API服务器"""
    # 启动区块链
    chain.start()

    # 启动API服务
    logger.info(f"Starting API server on {host}:{port}")
    app.run(host=host, port=port, debug=False)


if __name__ == '__main__':
    start_api_server()
