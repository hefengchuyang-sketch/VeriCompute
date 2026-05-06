# POUW-Chain V3.0 Integration Guide

> How to integrate V3.0 improvements into the existing project
> Updated: 2026-05-06

---

## 📋 Integration Overview

V3.0 represents a major architectural upgrade to the existing POUW-Chain project, using a **progressive integration strategy**:

1. ✅ **Preserve existing functionality**: All existing modules continue to work
2. ✅ **Add V3.0 modules**: Coexist as independent modules
3. ✅ **Provide migration paths**: Gradually migrate from old architecture to new architecture

---

## 🗂️ File Structure

### Existing Files (Preserved)

```
maincoin/
├── core/
│   ├── consensus.py                    # Original consensus (preserved)
│   ├── pouw_executor.py                # POUW executor (preserved)
│   ├── pouw_scoring.py                 # POUW scoring (preserved)
│   ├── online_reward_pool.py           # Online reward pool (preserved)
│   ├── dual_witness_exchange.py        # Optimistic exchange (preserved)
│   ├── cold_start.py                   # Cold start (preserved)
│   ├── pouw_task_selection.py          # Task selection (preserved)
│   ├── initial_coin_generation.py      # Initial coin generation (preserved)
│   └── ... (85+ other files)
│
├── docs/
│   ├── README.md                       # Original docs (preserved)
│   ├── API.md                          # API documentation (preserved)
│   ├── CONSENSUS.md                    # Consensus docs (preserved)
│   └── ... (other docs)
│
└── README.md                           # Project description (updated)
```

### New Files (V3.0)

```
maincoin/
├── core/
│   ├── pouw_chain_v3.py               # V3.0 complete implementation ⭐
│   └── dual_layer_consensus.py         # Dual-layer consensus (legacy)
│
├── api/
│   └── unified_gateway.py              # Unified API gateway ⭐
│
├── tests/
│   ├── test_pouw_v3_complete.py       # V3.0 comprehensive tests ⭐
│   ├── test_unified_gateway.py        # Gateway tests ⭐
│   └── test_unified_consensus.py      # Consensus tests ⭐
│
├── docs/
│   ├── POUW_V3_COMPLETE_TECHNICAL_DOC.md  # Complete technical documentation ⭐
│   └── UNIFIED_API_GATEWAY.md             # API gateway documentation ⭐
│
├── scripts/
│   └── start_unified_gateway.py       # Unified gateway launcher ⭐
│
└── V3_INTEGRATION_GUIDE.md             # This guide
```

---

## 🔄 Integration Strategy

### Phase 1: Parallel Operation (Current)

```
Legacy System (continues to run)
  ├── consensus.py
  ├── pouw_executor.py
  └── ... (85+ files)

V3.0 System (independent operation)
  ├── pouw_chain_v3.py
  ├── unified_gateway.py
  └── test_pouw_v3_complete.py
```

**Benefits**:
- ✅ Existing functionality unaffected
- ✅ V3.0 can be independently tested
- ✅ Gradual migration with low risk

### Phase 2: Selective Migration

Gradually migrate existing functionality to V3.0 architecture as needed:

```python
# Option A: Use V3.0's Layer 1 consensus
from core.pouw_chain_v3 import Layer1Consensus

# Option B: Use V3.0's Layer 2 compute market
from core.pouw_chain_v3 import Layer2ComputeMarket

# Option C: Use complete V3.0 system
from core.pouw_chain_v3 import POUWChainV3
```

### Phase 3: Full Migration

Eventually migrate all functionality to V3.0 architecture.

---

## 📊 Feature Comparison

| Feature | Legacy Implementation | V3.0 Implementation | Recommendation |
|---------|---------------------|-------------------|----------------|
| **Consensus Mechanism** | consensus.py | pouw_chain_v3.py (Layer1) | Gradual migration |
| **Task Execution** | pouw_executor.py | pouw_chain_v3.py (Layer2) | Gradual migration |
| **Task Verification** | Duplicate computation | zk-proof + Challenge Game | New feature |
| **Privacy Protection** | None | TEE/zk/MPC | New feature |
| **Online Rewards** | online_reward_pool.py | Preserved | Preserve |
| **Optimistic Exchange** | dual_witness_exchange.py | Preserved | Preserve |
| **Cold Start** | cold_start.py | Preserved | Preserve |

---

## 🔧 How to Use V3.0

### 1. Run V3.0 Independently

```python
# Start V3.0 system
from core.pouw_chain_v3 import get_pouw_chain

chain = get_pouw_chain("./data_v3")
chain.start()
```

### 2. Start Unified API Gateway

```python
# Start API service
from api.unified_gateway import create_app

app = create_app()
app.run(host='0.0.0.0', port=8000, debug=False)
```

### 3. Run V3.0 Tests

```bash
python tests/test_pouw_v3_complete.py
python tests/test_unified_gateway.py
```

### 4. Run Parallel with Legacy System

```python
# Run both legacy and V3.0 systems simultaneously
from core.consensus import ConsensusEngine  # Legacy
from core.pouw_chain_v3 import POUWChainV3  # V3.0

# Legacy system
old_chain = ConsensusEngine()
old_chain.start()

# V3.0 system
new_chain = POUWChainV3("./data_v3")
new_chain.start()
```

---

## 📝 Updated Documentation

### 1. README.md

Updated to V3.0, highlighting dual-layer consensus architecture and privacy computing.

### 2. New Technical Documentation

- **POUW_V3_COMPLETE_TECHNICAL_DOC.md**: Complete technical documentation (600+ lines)
- **UNIFIED_API_GATEWAY.md**: API gateway documentation with examples (700+ lines)
- **V3_INTEGRATION_GUIDE.md**: This integration guide

### 3. Preserved Documentation

All existing documentation is preserved, including:
- API.md
- CONSENSUS.md
- DEPLOYMENT.md
- ... (30+ other documents)

---

## 🎯 Migration Recommendations

### Short Term (1-2 weeks)

1. ✅ Familiarize with V3.0 architecture
2. ✅ Run V3.0 tests
3. ✅ Try V3.0 API endpoints

### Medium Term (1-2 months)

1. ⏳ Submit new tasks to V3.0 system
2. ⏳ Test Challenge Game mechanism
3. ⏳ Evaluate privacy computing features

### Long Term (3-6 months)

1. ⏳ Gradually migrate existing tasks
2. ⏳ Fully switch to V3.0 architecture
3. ⏳ Deprecate legacy code

---

## ⚠️ Important Notes

### 1. Data Compatibility

```
Legacy data: ./data/
V3.0 data: ./data_v3/

Recommendation: Use separate data directories to avoid conflicts
```

### 2. API Ports

```
Legacy API: Typically port 8000
V3.0 API: Port 8000 (unified gateway)

Recommendation: Use unified gateway on standard port, migrate gradually
```

### 3. Configuration Files

```
Legacy config: config.yaml
V3.0 config: Can use same or separate config

Recommendation: Use unified config.yaml, test thoroughly before full migration
```

---

## 🚀 Quick Start

### 1. Test V3.0

```bash
# Run comprehensive tests
cd maincoin
python tests/test_pouw_v3_complete.py
```

### 2. Start V3.0 Node

```python
# Create start_v3.py
from core.pouw_chain_v3 import get_pouw_chain

chain = get_pouw_chain("./data_v3")
chain.start()

print("V3.0 node started!")
```

### 3. Start Unified API Gateway

```bash
# Canonical startup method
python scripts/start_unified_gateway.py --port 8000
```

### 4. Call V3.0 API Endpoints

```bash
# Health check
curl http://localhost:8000/health

# Get system stats
curl http://localhost:8000/api/stats

# Register validator
curl -X POST http://localhost:8000/api/v3/validator/register \
  -H "Content-Type: application/json" \
  -d '{
    "validator_id": "validator_001",
    "address": "MAIN_xxx",
    "stake": 1000.0
  }'

# Submit task
curl -X POST http://localhost:8000/api/v3/task/submit \
  -H "Content-Type: application/json" \
  -d '{
    "task_id": "task_001",
    "client": "client_001",
    "encrypted_data": "dGVzdCBkYXRh",
    "compute_type": "AI_INFERENCE",
    "reward": 50.0,
    "client_bond": 10.0
  }'
```

---

## 📚 Documentation Index

### V3.0 Core Documentation

1. **[V3.0 Complete Technical Documentation](docs/POUW_V3_COMPLETE_TECHNICAL_DOC.md)** ← Must read
2. **[Unified API Gateway](docs/UNIFIED_API_GATEWAY.md)** ← API reference
3. **[README](README.md)** ← Project overview

### Legacy Documentation (Preserved)

1. **[API Documentation](docs/API.md)**
2. **[Consensus Documentation](docs/CONSENSUS.md)**
3. **[Deployment Guide](docs/DEPLOYMENT.md)**

---

## 🎉 Summary

V3.0 uses a **progressive integration strategy**:

1. ✅ **Existing functionality continues to work**
2. ✅ **V3.0 modules operate independently**
3. ✅ **Clear migration paths provided**
4. ✅ **Comprehensive documentation and tests**

**You can safely use V3.0 features in your existing project!** 🚀

---

*Funded by Thiel Fellowship*
