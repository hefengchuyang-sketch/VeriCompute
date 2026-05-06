# POUW-Chain Quick Start Guide

## 5-Minute Setup

### Step 1: Clone and Install (2 min)

```bash
git clone https://github.com/your-org/pouw-chain.git
cd pouw-chain
pip install -r requirements.txt
```

### Step 2: Start the Gateway (30 sec)

```bash
python scripts/start_unified_gateway.py
```

Output should show:
```
============================================================
POUW-Chain Unified API Gateway
============================================================

INFO: Starting unified gateway on 0.0.0.0:8000
```

### Step 3: Verify it Works (1 min)

In another terminal:

```bash
# Check health
curl http://localhost:8000/health

# Get API stats
curl http://localhost:8000/api/stats

# View API documentation
curl http://localhost:8000/api/docs
```

## Common Tasks

### Register as a Validator

```bash
curl -X POST http://localhost:8000/api/v3/validator/register \
  -H "Content-Type: application/json" \
  -d '{
    "validator_id": "validator_001",
    "address": "MAIN_initial_stake_validator",
    "stake": 1000.0
  }'
```

### Submit a Compute Task

```bash
curl -X POST http://localhost:8000/api/v3/task/submit \
  -H "Content-Type: application/json" \
  -d '{
    "task_id": "task_001",
    "client": "client_001",
    "encrypted_data": "base64_encoded_data",
    "compute_type": "AI_INFERENCE",
    "reward": 50.0,
    "client_bond": 10.0
  }'
```

### Get Task List

```bash
curl http://localhost:8000/api/v3/task/list
```

### Accept a Task

```bash
curl -X POST http://localhost:8000/api/v3/task/accept \
  -H "Content-Type: application/json" \
  -d '{
    "task_id": "task_001",
    "miner_id": "miner_001"
  }'
```

### Submit Task Result

```bash
curl -X POST http://localhost:8000/api/v3/task/submit_result \
  -H "Content-Type: application/json" \
  -d '{
    "task_id": "task_001",
    "miner_id": "miner_001",
    "encrypted_result": "base64_encoded_result",
    "computation_proof": "proof_data"
  }'
```

## Run Tests

```bash
# Comprehensive tests
python tests/test_pouw_v3_complete.py

# API gateway tests
python tests/test_unified_gateway.py

# Consensus tests
python tests/test_unified_consensus.py
```

## Configuration

### Custom Port

```bash
python scripts/start_unified_gateway.py --port 9000
```

### Custom Host

```bash
python scripts/start_unified_gateway.py --host 127.0.0.1
```

### Combined

```bash
python scripts/start_unified_gateway.py --host 127.0.0.1 --port 9000
```

## Python Client Example

```python
import requests

class POUWClient:
    def __init__(self, base_url="http://localhost:8000"):
        self.base_url = base_url
    
    def health(self):
        return requests.get(f"{self.base_url}/health").json()
    
    def register_validator(self, validator_id, address, stake):
        return requests.post(
            f"{self.base_url}/api/v3/validator/register",
            json={
                "validator_id": validator_id,
                "address": address,
                "stake": stake
            }
        ).json()
    
    def submit_task(self, task_id, client, encrypted_data, reward):
        return requests.post(
            f"{self.base_url}/api/v3/task/submit",
            json={
                "task_id": task_id,
                "client": client,
                "encrypted_data": encrypted_data,
                "compute_type": "AI_INFERENCE",
                "reward": reward,
                "client_bond": reward * 0.1
            }
        ).json()

# Usage
client = POUWClient()

# Check health
health = client.health()
print(f"Status: {health['status']}")

# Register validator
result = client.register_validator("v001", "MAIN_xxx", 1000.0)
print(f"Registered: {result}")

# Submit task
task = client.submit_task("task_001", "client_001", "data", 50.0)
print(f"Task ID: {task['task_id']}")
```

## JavaScript/Node.js Example

```javascript
const axios = require('axios');

class POUWClient {
    constructor(baseUrl = 'http://localhost:8000') {
        this.baseUrl = baseUrl;
        this.client = axios.create({ baseURL: baseUrl });
    }

    async health() {
        return (await this.client.get('/health')).data;
    }

    async registerValidator(validatorId, address, stake) {
        return (await this.client.post('/api/v3/validator/register', {
            validator_id: validatorId,
            address: address,
            stake: stake
        })).data;
    }

    async submitTask(taskId, client, encryptedData, reward) {
        return (await this.client.post('/api/v3/task/submit', {
            task_id: taskId,
            client: client,
            encrypted_data: encryptedData,
            compute_type: 'AI_INFERENCE',
            reward: reward,
            client_bond: reward * 0.1
        })).data;
    }
}

// Usage
const client = new POUWClient();

// Check health
const health = await client.health();
console.log('Status:', health.status);

// Register validator
const result = await client.registerValidator('v001', 'MAIN_xxx', 1000.0);
console.log('Registered:', result);

// Submit task
const task = await client.submitTask('task_001', 'client_001', 'data', 50.0);
console.log('Task ID:', task.task_id);
```

## Troubleshooting

### Port Already in Use

```bash
# Use different port
python scripts/start_unified_gateway.py --port 8001
```

### Module Import Errors

```bash
# Reinstall dependencies
pip install -r requirements.txt --force-reinstall
```

### Connection Refused

```bash
# Check if gateway is running
curl http://localhost:8000/health

# If not running, start it
python scripts/start_unified_gateway.py
```

### Tests Failing

```bash
# Install test dependencies
pip install pytest

# Run with verbose output
python tests/test_pouw_v3_complete.py -v
```

## Next Steps

1. **Read Documentation**: See [docs/POUW_V3_COMPLETE_TECHNICAL_DOC.md](docs/POUW_V3_COMPLETE_TECHNICAL_DOC.md)
2. **Explore API**: Visit `http://localhost:8000/api/docs`
3. **Run Tests**: `python tests/test_pouw_v3_complete.py`
4. **Join Community**: Check out discussions and issues
5. **Deploy**: Use Docker or cloud deployment options

## Support

- 📚 [Complete Documentation](docs/)
- 🐛 [Report Issues](https://github.com/your-org/pouw-chain/issues)
- 💬 [Join Discussions](https://github.com/your-org/pouw-chain/discussions)
- 📧 Email: contact@pouw-chain.org

---

**Ready to get started? Run this now:**

```bash
git clone https://github.com/your-org/pouw-chain.git && cd pouw-chain && pip install -r requirements.txt && python scripts/start_unified_gateway.py
```

🚀 Welcome to POUW-Chain!
