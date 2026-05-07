#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
启动统一API网关

整合所有API接口的统一入口
"""

import argparse
import sys
import logging
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)


def main():
    """主函数"""
    parser = argparse.ArgumentParser(description="Start the unified POUW API gateway")
    parser.add_argument("--host", default="0.0.0.0", help="Gateway listen host")
    parser.add_argument("--port", type=int, default=8000, help="Gateway listen port")
    parser.add_argument("--data-dir", default="./data_v3", help="V3 chain data directory")
    parser.add_argument(
        "--cors-origins",
        nargs="*",
        default=["http://localhost:3000", "http://127.0.0.1:3000"],
        help="Allowed CORS origin(s) for the gateway"
    )
    args = parser.parse_args()

    print("="*60)
    print("POUW-Chain Unified API Gateway")
    print("="*60)
    print()

    from api.unified_gateway import start_unified_gateway

    logger.info(f"Starting unified gateway on {args.host}:{args.port}")
    start_unified_gateway(
        host=args.host,
        port=args.port,
        v3_data_dir=args.data_dir,
        cors_origins=args.cors_origins
    )


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nShutting down gateway...")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Failed to start gateway: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
