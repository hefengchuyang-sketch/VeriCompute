# -*- coding: utf-8 -*-
"""
POUW-Chain 统一API网关

整合所有API接口：
1. 现有RPC服务（core/rpc_service.py）
2. V3.0 REST API（api/pouw_api_v3.py）
3. SDK API（core/sdk_api.py）

提供统一的访问入口和路由
"""

from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import json
import logging
from typing import Dict, Any, Optional
import time

# 导入现有服务
from core.rpc_service import NodeRPCService, RPCRequest, RPCResponse
from core.sdk_api import APIVersion

# 导入V3.0服务
try:
    from core.pouw_chain_v3 import get_pouw_chain, VerificationType, PrivacyMode
    HAS_V3 = True
except ImportError:
    HAS_V3 = False

logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)  # 启用CORS


# ============== 统一API网关 ==============

class UnifiedAPIGateway:
    """
    统一API网关

    整合：
    - RPC服务（现有）
    - V3.0 REST API（新）
    - SDK API（现有）
    """

    def __init__(self):
        # 现有服务
        self.rpc_service = NodeRPCService()

        # V3.0服务
        if HAS_V3:
            self.v3_chain = get_pouw_chain("./data_v3")
        else:
            self.v3_chain = None

        # 统计
        self.request_count = 0
        self.error_count = 0
        self.start_time = time.time()

    def set_dependencies(self, **kwargs):
        """设置依赖组件"""
        for key, value in kwargs.items():
            if hasattr(self.rpc_service, key):
                setattr(self.rpc_service, key, value)

    def handle_rpc_request(self, method: str, params: Dict) -> Dict:
        """处理RPC请求"""
        try:
            self.request_count += 1

            # 创建RPC请求
            rpc_request = RPCRequest(
                method=method,
                params=params,
                id=str(int(time.time() * 1000))
            )

            # 调用RPC服务
            response = self.rpc_service.handle_request(rpc_request)

            return {
                "jsonrpc": "2.0",
                "result": response.result,
                "error": response.error.to_dict() if response.error else None,
                "id": response.id
            }

        except Exception as e:
            self.error_count += 1
            logger.error(f"RPC request failed: {e}")
            return {
                "jsonrpc": "2.0",
                "error": {
                    "code": -32603,
                    "message": str(e)
                },
                "id": None
            }

    def handle_v3_request(self, endpoint: str, data: Dict) -> Dict:
        """处理V3.0请求"""
        if not HAS_V3 or not self.v3_chain:
            return {
                "success": False,
                "error": "V3.0 not available"
            }

        try:
            self.request_count += 1

            # 路由到对应的V3.0方法
            if endpoint == "validator/register":
                return self._v3_register_validator(data)
            elif endpoint == "validator/list":
                return self._v3_list_validators()
            elif endpoint == "block/latest":
                return self._v3_get_latest_block()
            elif endpoint == "task/submit":
                return self._v3_submit_task(data)
            elif endpoint == "task/accept":
                return self._v3_accept_task(data)
            elif endpoint == "task/submit_result":
                return self._v3_submit_result(data)
            elif endpoint == "task/challenge":
                return self._v3_submit_challenge(data)
            elif endpoint == "task/get":
                return self._v3_get_task(data)
            elif endpoint == "task/list":
                return self._v3_list_tasks(data)
            elif endpoint == "stats/overview":
                return self._v3_get_stats()
            else:
                return {
                    "success": False,
                    "error": f"Unknown endpoint: {endpoint}"
                }

        except Exception as e:
            self.error_count += 1
            logger.error(f"V3 request failed: {e}")
            return {
                "success": False,
                "error": str(e)
            }

    # V3.0 方法实现

    def _v3_register_validator(self, data: Dict) -> Dict:
        """注册验证者"""
        success, msg = self.v3_chain.layer1.register_validator(
            validator_id=data['validator_id'],
            address=data['address'],
            stake=data['stake']
        )
        return {"success": success, "message": msg}

    def _v3_list_validators(self) -> Dict:
        """获取验证者列表"""
        validators = []
        for vid, validator in self.v3_chain.layer1.validators.items():
            validators.append({
                "id": validator.id,
                "address": validator.address,
                "stake": validator.stake,
                "voting_power": validator.voting_power,
                "status": validator.status.value,
                "blocks_produced": validator.blocks_produced
            })
        return {"validators": validators, "total": len(validators)}

    def _v3_get_latest_block(self) -> Dict:
        """获取最新区块"""
        if self.v3_chain.layer1.current_height == 0:
            return {"error": "No blocks yet"}

        block = self.v3_chain.layer1.blocks[self.v3_chain.layer1.current_height]
        return {
            "height": block.height,
            "hash": block.hash,
            "parent_hash": block.parent_hash,
            "proposer": block.proposer,
            "timestamp": block.timestamp,
            "task_count": len(block.task_commitments)
        }

    def _v3_submit_task(self, data: Dict) -> Dict:
        """提交任务"""
        import base64
        encrypted_data = base64.b64decode(data['encrypted_data'])

        verification_type = VerificationType[data.get('verification_type', 'CHALLENGE').upper()]
        privacy_mode = PrivacyMode[data.get('privacy_mode', 'TEE').upper()]

        success, msg = self.v3_chain.layer2.submit_task(
            task_id=data['task_id'],
            client=data['client'],
            encrypted_data=encrypted_data,
            compute_type=data['compute_type'],
            reward=data['reward'],
            client_bond=data['client_bond'],
            verification_type=verification_type,
            privacy_mode=privacy_mode
        )
        return {"success": success, "message": msg}

    def _v3_accept_task(self, data: Dict) -> Dict:
        """接受任务"""
        success, msg = self.v3_chain.layer2.accept_task(
            task_id=data['task_id'],
            worker=data['worker'],
            worker_stake=data['worker_stake']
        )
        return {"success": success, "message": msg}

    def _v3_submit_result(self, data: Dict) -> Dict:
        """提交结果"""
        success, msg = self.v3_chain.layer2.submit_result(
            task_id=data['task_id'],
            worker=data['worker'],
            result_hash=data['result_hash'],
            proof=data.get('proof', '')
        )
        return {"success": success, "message": msg}

    def _v3_submit_challenge(self, data: Dict) -> Dict:
        """提交挑战"""
        success, msg = self.v3_chain.layer2.submit_challenge(
            task_id=data['task_id'],
            challenger=data['challenger'],
            reason=data['reason'],
            evidence=data['evidence'],
            stake=data['stake']
        )
        return {"success": success, "message": msg}

    def _v3_get_task(self, data: Dict) -> Dict:
        """获取任务详情"""
        task_id = data.get('task_id')
        if task_id not in self.v3_chain.layer2.tasks:
            return {"error": "Task not found"}

        task = self.v3_chain.layer2.tasks[task_id]
        return {
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
        }

    def _v3_list_tasks(self, data: Dict) -> Dict:
        """获取任务列表"""
        status_filter = data.get('status')
        limit = data.get('limit', 100)

        tasks = []
        for task_id, task in self.v3_chain.layer2.tasks.items():
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

        return {"tasks": tasks, "total": len(tasks)}

    def _v3_get_stats(self) -> Dict:
        """获取统计信息"""
        return {
            "layer1": {
                "current_height": self.v3_chain.layer1.current_height,
                "validators": len(self.v3_chain.layer1.validators),
                "active_validators": len(self.v3_chain.layer1.active_set)
            },
            "layer2": {
                "total_tasks": len(self.v3_chain.layer2.tasks),
                "pending_tasks": sum(1 for t in self.v3_chain.layer2.tasks.values() if t.status.value == "submitted"),
                "finalized_tasks": sum(1 for t in self.v3_chain.layer2.tasks.values() if t.status.value == "finalized"),
                "total_challenges": len(self.v3_chain.layer2.challenges)
            }
        }

    def get_gateway_stats(self) -> Dict:
        """获取网关统计"""
        uptime = time.time() - self.start_time
        return {
            "request_count": self.request_count,
            "error_count": self.error_count,
            "error_rate": self.error_count / max(self.request_count, 1),
            "uptime": uptime,
            "rpc_available": True,
            "v3_available": HAS_V3 and self.v3_chain is not None
        }


# ============== 全局实例 ==============

gateway = UnifiedAPIGateway()


# ============== Flask 路由 ==============

@app.route('/health', methods=['GET'])
def health_check():
    """健康检查"""
    return jsonify({
        "status": "healthy",
        "version": "unified-gateway-v1.0",
        "timestamp": time.time()
    })


@app.route('/api/stats', methods=['GET'])
def get_stats():
    """获取网关统计"""
    return jsonify(gateway.get_gateway_stats())


# ============== RPC 接口 ==============

@app.route('/rpc', methods=['POST'])
def rpc_endpoint():
    """
    RPC接口（现有）

    POST /rpc
    {
        "jsonrpc": "2.0",
        "method": "getBlockchainInfo",
        "params": {},
        "id": 1
    }
    """
    data = request.json

    if not data or 'method' not in data:
        return jsonify({
            "jsonrpc": "2.0",
            "error": {
                "code": -32600,
                "message": "Invalid Request"
            },
            "id": None
        }), 400

    response = gateway.handle_rpc_request(
        method=data['method'],
        params=data.get('params', {})
    )

    return jsonify(response)


# ============== V3.0 REST API ==============

@app.route('/api/v3/<path:endpoint>', methods=['GET', 'POST'])
def v3_endpoint(endpoint: str):
    """
    V3.0 REST API

    GET/POST /api/v3/<endpoint>
    """
    if request.method == 'GET':
        data = request.args.to_dict()
    else:
        data = request.json or {}

    response = gateway.handle_v3_request(endpoint, data)
    return jsonify(response)


# ============== 统一查询接口 ==============

@app.route('/api/unified/query', methods=['POST'])
def unified_query():
    """
    统一查询接口

    POST /api/unified/query
    {
        "service": "rpc" | "v3",
        "method": "...",
        "params": {...}
    }
    """
    data = request.json

    if not data or 'service' not in data or 'method' not in data:
        return jsonify({
            "success": False,
            "error": "Invalid request format"
        }), 400

    service = data['service']
    method = data['method']
    params = data.get('params', {})

    if service == 'rpc':
        response = gateway.handle_rpc_request(method, params)
        return jsonify(response)

    elif service == 'v3':
        response = gateway.handle_v3_request(method, params)
        return jsonify(response)

    else:
        return jsonify({
            "success": False,
            "error": f"Unknown service: {service}"
        }), 400


# ============== API文档 ==============

@app.route('/api/docs', methods=['GET'])
def api_docs():
    """API文档"""
    docs = {
        "title": "POUW-Chain Unified API Gateway",
        "version": "1.0",
        "description": "Unified API gateway integrating RPC and V3.0 REST API",
        "endpoints": {
            "health": {
                "path": "/health",
                "method": "GET",
                "description": "Health check"
            },
            "stats": {
                "path": "/api/stats",
                "method": "GET",
                "description": "Gateway statistics"
            },
            "rpc": {
                "path": "/rpc",
                "method": "POST",
                "description": "RPC endpoint (existing)",
                "example": {
                    "jsonrpc": "2.0",
                    "method": "getBlockchainInfo",
                    "params": {},
                    "id": 1
                }
            },
            "v3": {
                "path": "/api/v3/<endpoint>",
                "method": "GET/POST",
                "description": "V3.0 REST API",
                "endpoints": [
                    "validator/register",
                    "validator/list",
                    "block/latest",
                    "task/submit",
                    "task/accept",
                    "task/submit_result",
                    "task/challenge",
                    "task/get",
                    "task/list",
                    "stats/overview"
                ]
            },
            "unified": {
                "path": "/api/unified/query",
                "method": "POST",
                "description": "Unified query interface",
                "example": {
                    "service": "rpc",
                    "method": "getBlockchainInfo",
                    "params": {}
                }
            }
        }
    }

    return jsonify(docs)


# ============== 启动服务 ==============

def start_unified_gateway(
    host: str = '0.0.0.0',
    port: int = 8000,
    **dependencies
):
    """
    启动统一API网关

    Args:
        host: 监听地址
        port: 监听端口
        **dependencies: 依赖组件（blockchain, mempool等）
    """
    # 设置依赖
    gateway.set_dependencies(**dependencies)

    # 启动V3.0链（如果可用）
    if gateway.v3_chain:
        gateway.v3_chain.start()

    # 启动Flask服务
    logger.info(f"Starting Unified API Gateway on {host}:{port}")
    app.run(host=host, port=port, debug=False)


if __name__ == '__main__':
    start_unified_gateway()
