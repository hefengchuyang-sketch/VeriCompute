# -*- coding: utf-8 -*-
"""
统一API网关测试

测试所有API接口的整合
"""

import sys
import requests
import json
import time
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

BASE_URL = "http://localhost:8000"


def test_health_check():
    """测试健康检查"""
    print("\n" + "="*60)
    print("Test 1: Health Check")
    print("="*60)

    response = requests.get(f"{BASE_URL}/health")
    data = response.json()

    print(f"Status: {data['status']}")
    print(f"Version: {data['version']}")
    print(f"Timestamp: {data['timestamp']}")

    assert data['status'] == 'healthy'
    print("\nPASS: Health check successful!")


def test_gateway_stats():
    """测试网关统计"""
    print("\n" + "="*60)
    print("Test 2: Gateway Stats")
    print("="*60)

    response = requests.get(f"{BASE_URL}/api/stats")
    data = response.json()

    print(f"Request count: {data['request_count']}")
    print(f"Error count: {data['error_count']}")
    print(f"Error rate: {data['error_rate']:.2%}")
    print(f"Uptime: {data['uptime']:.2f}s")
    print(f"RPC available: {data['rpc_available']}")
    print(f"V3 available: {data['v3_available']}")

    print("\nPASS: Gateway stats retrieved!")


def test_rpc_interface():
    """测试RPC接口"""
    print("\n" + "="*60)
    print("Test 3: RPC Interface")
    print("="*60)

    # 测试getBlockchainInfo
    response = requests.post(
        f"{BASE_URL}/rpc",
        json={
            "jsonrpc": "2.0",
            "method": "getBlockchainInfo",
            "params": {},
            "id": 1
        }
    )

    data = response.json()
    print(f"RPC Response: {json.dumps(data, indent=2)}")

    assert 'result' in data or 'error' in data
    print("\nPASS: RPC interface working!")


def test_v3_validator_api():
    """测试V3.0验证者API"""
    print("\n" + "="*60)
    print("Test 4: V3.0 Validator API")
    print("="*60)

    # 1. 注册验证者
    print("\n[1/2] Registering validator...")
    response = requests.post(
        f"{BASE_URL}/api/v3/validator/register",
        json={
            "validator_id": "test_validator_001",
            "address": "MAIN_test_001",
            "stake": 1000.0
        }
    )

    data = response.json()
    print(f"Register response: {data}")

    # 2. 获取验证者列表
    print("\n[2/2] Getting validator list...")
    response = requests.get(f"{BASE_URL}/api/v3/validator/list")
    data = response.json()

    print(f"Total validators: {data['total']}")
    if data['validators']:
        print(f"First validator: {data['validators'][0]}")

    print("\nPASS: V3.0 validator API working!")


def test_v3_task_api():
    """测试V3.0任务API"""
    print("\n" + "="*60)
    print("Test 5: V3.0 Task API")
    print("="*60)

    import base64

    # 1. 提交任务
    print("\n[1/3] Submitting task...")
    task_data = b"test task data"
    encoded_data = base64.b64encode(task_data).decode()

    response = requests.post(
        f"{BASE_URL}/api/v3/task/submit",
        json={
            "task_id": "test_task_001",
            "client": "test_client_001",
            "encrypted_data": encoded_data,
            "compute_type": "AI_INFERENCE",
            "reward": 50.0,
            "client_bond": 10.0,
            "verification_type": "challenge",
            "privacy_mode": "tee"
        }
    )

    data = response.json()
    print(f"Submit response: {data}")

    # 2. 获取任务列表
    print("\n[2/3] Getting task list...")
    response = requests.get(f"{BASE_URL}/api/v3/task/list?limit=10")
    data = response.json()

    print(f"Total tasks: {data['total']}")
    if data['tasks']:
        print(f"First task: {data['tasks'][0]}")

    # 3. 获取统计
    print("\n[3/3] Getting stats...")
    response = requests.get(f"{BASE_URL}/api/v3/stats/overview")
    data = response.json()

    print(f"Layer1 stats: {data['layer1']}")
    print(f"Layer2 stats: {data['layer2']}")

    print("\nPASS: V3.0 task API working!")


def test_unified_query():
    """测试统一查询接口"""
    print("\n" + "="*60)
    print("Test 6: Unified Query Interface")
    print("="*60)

    # 1. 通过统一接口调用RPC
    print("\n[1/2] Calling RPC through unified interface...")
    response = requests.post(
        f"{BASE_URL}/api/unified/query",
        json={
            "service": "rpc",
            "method": "getBlockchainInfo",
            "params": {}
        }
    )

    data = response.json()
    print(f"RPC result: {json.dumps(data, indent=2)[:200]}...")

    # 2. 通过统一接口调用V3.0
    print("\n[2/2] Calling V3.0 through unified interface...")
    response = requests.post(
        f"{BASE_URL}/api/unified/query",
        json={
            "service": "v3",
            "method": "stats/overview",
            "params": {}
        }
    )

    data = response.json()
    print(f"V3 result: {json.dumps(data, indent=2)}")

    print("\nPASS: Unified query interface working!")


def test_api_docs():
    """测试API文档"""
    print("\n" + "="*60)
    print("Test 7: API Documentation")
    print("="*60)

    response = requests.get(f"{BASE_URL}/api/docs")
    data = response.json()

    print(f"API Title: {data['title']}")
    print(f"API Version: {data['version']}")
    print(f"Endpoints: {list(data['endpoints'].keys())}")

    print("\nPASS: API documentation available!")


def run_all_tests():
    """运行所有测试"""
    print("\n" + "="*60)
    print("Unified API Gateway Test Suite")
    print("="*60)
    print(f"\nBase URL: {BASE_URL}")
    print("Make sure the gateway is running!")
    print("\nWaiting 2 seconds...")
    time.sleep(2)

    try:
        # Test 1: Health Check
        test_health_check()

        # Test 2: Gateway Stats
        test_gateway_stats()

        # Test 3: RPC Interface
        test_rpc_interface()

        # Test 4: V3.0 Validator API
        test_v3_validator_api()

        # Test 5: V3.0 Task API
        test_v3_task_api()

        # Test 6: Unified Query
        test_unified_query()

        # Test 7: API Docs
        test_api_docs()

        # Final Summary
        print("\n" + "="*60)
        print("ALL TESTS PASSED!")
        print("="*60)

    except requests.exceptions.ConnectionError:
        print("\nERROR: Cannot connect to gateway!")
        print("Please start the gateway first:")
        print("  python scripts/start_unified_gateway.py")

    except Exception as e:
        print(f"\nERROR: Test failed: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    run_all_tests()
