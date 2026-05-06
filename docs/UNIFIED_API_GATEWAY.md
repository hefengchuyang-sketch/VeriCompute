# POUW-Chain 统一API文档

> 整合所有API接口的统一网关
> 更新日期：2026-05-06

---

## 📋 概述

统一API网关整合了POUW-Chain的所有API接口：

1. **RPC服务**（现有）- JSON-RPC 2.0协议
2. **V3.0 REST API**（新）- RESTful风格
3. **统一查询接口** - 统一的查询入口

---

## 🚀 快速开始

### 启动网关

```bash
# 推荐使用独立启动脚本
python scripts/start_unified_gateway.py --host 0.0.0.0 --port 8000

# 调试时也可以直接通过模块启动
python api/unified_gateway.py
```

```python
from api.unified_gateway import start_unified_gateway

# 启动网关
start_unified_gateway(
    host='0.0.0.0',
    port=8000,
    blockchain=blockchain,
    mempool=mempool,
    # ... 其他依赖
)
```

### 访问API

```bash
# 健康检查
curl http://localhost:8000/health

# 获取统计
curl http://localhost:8000/api/stats

# API文档
curl http://localhost:8000/api/docs
```

---

## 📡 API接口

### 1. 健康检查

```http
GET /health
```

**响应**：
```json
{
  "status": "healthy",
  "version": "unified-gateway-v1.0",
  "timestamp": 1715000000.0
}
```

### 2. 网关统计

```http
GET /api/stats
```

**响应**：
```json
{
  "request_count": 1000,
  "error_count": 5,
  "error_rate": 0.005,
  "uptime": 3600.0,
  "rpc_available": true,
  "v3_available": true
}
```

---

## 🔧 RPC接口（现有）

### 端点

```http
POST /rpc
```

### 请求格式

```json
{
  "jsonrpc": "2.0",
  "method": "方法名",
  "params": {参数},
  "id": 1
}
```

### 响应格式

```json
{
  "jsonrpc": "2.0",
  "result": {结果},
  "error": null,
  "id": 1
}
```

### 示例：获取区块链信息

**请求**：
```bash
curl -X POST http://localhost:8000/rpc \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "method": "getBlockchainInfo",
    "params": {},
    "id": 1
  }'
```

**响应**：
```json
{
  "jsonrpc": "2.0",
  "result": {
    "chain": "main",
    "blocks": 12345,
    "headers": 12345,
    "bestblockhash": "0x...",
    "difficulty": 1000000,
    "mediantime": 1715000000
  },
  "error": null,
  "id": 1
}
```

### 常用RPC方法

| 方法 | 描述 | 参数 |
|------|------|------|
| `getBlockchainInfo` | 获取区块链信息 | 无 |
| `getBlock` | 获取区块 | `{"hash": "0x..."}` |
| `getTransaction` | 获取交易 | `{"txid": "0x..."}` |
| `sendTransaction` | 发送交易 | `{"tx": {...}}` |
| `getBalance` | 获取余额 | `{"address": "..."}` |
| `listPeers` | 获取对等节点 | 无 |

---

## 🆕 V3.0 REST API

### 端点格式

```http
GET/POST /api/v3/<endpoint>
```

### Layer 1 API（共识层）

#### 1. 注册验证者

```http
POST /api/v3/validator/register
```

**请求**：
```json
{
  "validator_id": "validator_001",
  "address": "MAIN_xxx",
  "stake": 1000.0
}
```

**响应**：
```json
{
  "success": true,
  "message": "Validator registered"
}
```

#### 2. 获取验证者列表

```http
GET /api/v3/validator/list
```

**响应**：
```json
{
  "validators": [
    {
      "id": "validator_001",
      "address": "MAIN_xxx",
      "stake": 1000.0,
      "voting_power": 1000.0,
      "status": "active",
      "blocks_produced": 10
    }
  ],
  "total": 1
}
```

#### 3. 获取最新区块

```http
GET /api/v3/block/latest
```

**响应**：
```json
{
  "height": 100,
  "hash": "0x...",
  "parent_hash": "0x...",
  "proposer": "validator_001",
  "timestamp": 1715000000.0,
  "task_count": 5
}
```

### Layer 2 API（计算层）

#### 1. 提交任务

```http
POST /api/v3/task/submit
```

**请求**：
```json
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
```

**响应**：
```json
{
  "success": true,
  "message": "Task submitted"
}
```

#### 2. 接受任务

```http
POST /api/v3/task/accept
```

**请求**：
```json
{
  "task_id": "task_001",
  "worker": "worker_001",
  "worker_stake": 5.0
}
```

**响应**：
```json
{
  "success": true,
  "message": "Task accepted"
}
```

#### 3. 提交结果

```http
POST /api/v3/task/submit_result
```

**请求**：
```json
{
  "task_id": "task_001",
  "worker": "worker_001",
  "result_hash": "0x...",
  "proof": "zk_proof_or_empty"
}
```

**响应**：
```json
{
  "success": true,
  "message": "Result submitted, challenge window: 10 blocks"
}
```

#### 4. 提交挑战

```http
POST /api/v3/task/challenge
```

**请求**：
```json
{
  "task_id": "task_001",
  "challenger": "challenger_001",
  "reason": "Result incorrect",
  "evidence": {
    "expected": "0x...",
    "actual": "0x..."
  },
  "stake": 2.0
}
```

**响应**：
```json
{
  "success": true,
  "message": "Challenge submitted"
}
```

#### 5. 获取任务详情

```http
GET /api/v3/task/get?task_id=task_001
```

**响应**：
```json
{
  "task_id": "task_001",
  "client": "client_001",
  "worker": "worker_001",
  "compute_type": "AI_INFERENCE",
  "reward": 50.0,
  "status": "finalized",
  "verification_type": "challenge",
  "privacy_mode": "tee",
  "result_hash": "0x...",
  "created_at": 1715000000.0,
  "finalized_at": 1715001000.0
}
```

#### 6. 获取任务列表

```http
GET /api/v3/task/list?status=submitted&limit=10
```

**响应**：
```json
{
  "tasks": [
    {
      "task_id": "task_001",
      "client": "client_001",
      "worker": "",
      "compute_type": "AI_INFERENCE",
      "reward": 50.0,
      "status": "submitted",
      "created_at": 1715000000.0
    }
  ],
  "total": 1
}
```

#### 7. 获取统计信息

```http
GET /api/v3/stats/overview
```

**响应**：
```json
{
  "layer1": {
    "current_height": 100,
    "validators": 5,
    "active_validators": 5
  },
  "layer2": {
    "total_tasks": 100,
    "pending_tasks": 10,
    "finalized_tasks": 80,
    "total_challenges": 5
  }
}
```

---

## 🔀 统一查询接口

### 端点

```http
POST /api/unified/query
```

### 请求格式

```json
{
  "service": "rpc" | "v3",
  "method": "方法名",
  "params": {参数}
}
```

### 示例1：通过统一接口调用RPC

**请求**：
```bash
curl -X POST http://localhost:8000/api/unified/query \
  -H "Content-Type: application/json" \
  -d '{
    "service": "rpc",
    "method": "getBlockchainInfo",
    "params": {}
  }'
```

### 示例2：通过统一接口调用V3.0 API

**请求**：
```bash
curl -X POST http://localhost:8000/api/unified/query \
  -H "Content-Type: application/json" \
  -d '{
    "service": "v3",
    "method": "validator/list",
    "params": {}
  }'
```

---

## 📝 完整使用示例

### Python客户端

```python
import requests
import json

class POUWClient:
    def __init__(self, base_url="http://localhost:8000"):
        self.base_url = base_url
    
    # RPC方法
    def rpc_call(self, method, params=None):
        response = requests.post(
            f"{self.base_url}/rpc",
            json={
                "jsonrpc": "2.0",
                "method": method,
                "params": params or {},
                "id": 1
            }
        )
        return response.json()
    
    # V3.0方法
    def v3_call(self, endpoint, data=None, method="POST"):
        if method == "GET":
            response = requests.get(
                f"{self.base_url}/api/v3/{endpoint}",
                params=data
            )
        else:
            response = requests.post(
                f"{self.base_url}/api/v3/{endpoint}",
                json=data
            )
        return response.json()
    
    # 统一查询
    def unified_query(self, service, method, params=None):
        response = requests.post(
            f"{self.base_url}/api/unified/query",
            json={
                "service": service,
                "method": method,
                "params": params or {}
            }
        )
        return response.json()

# 使用示例
client = POUWClient()

# 1. 获取区块链信息（RPC）
info = client.rpc_call("getBlockchainInfo")
print(f"Current height: {info['result']['blocks']}")

# 2. 注册验证者（V3.0）
result = client.v3_call("validator/register", {
    "validator_id": "validator_001",
    "address": "MAIN_xxx",
    "stake": 1000.0
})
print(f"Register result: {result}")

# 3. 提交任务（V3.0）
result = client.v3_call("task/submit", {
    "task_id": "task_001",
    "client": "client_001",
    "encrypted_data": "dGVzdCBkYXRh",  # base64
    "compute_type": "AI_INFERENCE",
    "reward": 50.0,
    "client_bond": 10.0
})
print(f"Submit task result: {result}")

# 4. 统一查询
result = client.unified_query("v3", "stats/overview")
print(f"Stats: {result}")
```

### JavaScript客户端

```javascript
class POUWClient {
    constructor(baseUrl = 'http://localhost:8000') {
        this.baseUrl = baseUrl;
    }

    // RPC方法
    async rpcCall(method, params = {}) {
        const response = await fetch(`${this.baseUrl}/rpc`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                jsonrpc: '2.0',
                method: method,
                params: params,
                id: 1
            })
        });
        return await response.json();
    }

    // V3.0方法
    async v3Call(endpoint, data = null, method = 'POST') {
        const url = `${this.baseUrl}/api/v3/${endpoint}`;
        const options = {
            method: method,
            headers: {'Content-Type': 'application/json'}
        };
        
        if (method === 'POST' && data) {
            options.body = JSON.stringify(data);
        }
        
        const response = await fetch(url, options);
        return await response.json();
    }

    // 统一查询
    async unifiedQuery(service, method, params = {}) {
        const response = await fetch(`${this.baseUrl}/api/unified/query`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                service: service,
                method: method,
                params: params
            })
        });
        return await response.json();
    }
}

// 使用示例
const client = new POUWClient();

// 获取区块链信息
const info = await client.rpcCall('getBlockchainInfo');
console.log('Current height:', info.result.blocks);

// 注册验证者
const result = await client.v3Call('validator/register', {
    validator_id: 'validator_001',
    address: 'MAIN_xxx',
    stake: 1000.0
});
console.log('Register result:', result);
```

---

## 🔒 安全性

### 认证

```python
# TODO: 添加API密钥认证
headers = {
    "X-API-Key": "your_api_key"
}
```

### 速率限制

```python
# TODO: 添加速率限制
# 默认：100请求/分钟
```

### CORS

已启用CORS，允许跨域请求。

---

## 📊 监控

### 获取网关统计

```bash
curl http://localhost:8000/api/stats
```

### 响应

```json
{
  "request_count": 1000,
  "error_count": 5,
  "error_rate": 0.005,
  "uptime": 3600.0,
  "rpc_available": true,
  "v3_available": true
}
```

---

## 🐛 错误处理

### RPC错误

```json
{
  "jsonrpc": "2.0",
  "error": {
    "code": -32600,
    "message": "Invalid Request"
  },
  "id": null
}
```

### V3.0错误

```json
{
  "success": false,
  "error": "Error message"
}
```

---

## 📚 相关文档

1. **[统一网关实现](../api/unified_gateway.py)**
2. **[RPC服务文档](../core/rpc_service.py)**
3. **[V3.0 API文档](POUW_V3_COMPLETE_TECHNICAL_DOC.md)**
4. **[SDK API文档](../core/sdk_api.py)**

---

*Funded by Thiel Fellowship*
