# 项目整理与链路总览（POUW-Chain）

本文档用于统一项目层次、关键程序链路与可执行检查步骤，确保开发/测试/发布路径清晰可追踪。

## 1. 分层结构

- 入口层
  - `main.py`: 节点启动主入口，负责安全预检、组件装配、RPC 启动。
  - `start.bat` / `start.ps1`: 本地启动脚本。
  - `docker-compose.yml` / `Dockerfile`: 容器化运行入口。
  - `scripts/start_unified_gateway.py`: 统一 RPC + V3 API 网关的推荐启动器。
  - `scripts/pre_launch_check.py`: 上线前统一检查脚本，验证语法、依赖、配置和目录结构。

- 服务层
  - `core/rpc_service.py`: NodeRPCService 业务实现（含订单簿、期货、计费、TEE 等方法）。
  - `core/rpc/`: RPC 基础模型与服务端封装。
  - `core/rpc_handlers/`: 各业务域 RPC 注册与方法拆分（权限与路由汇总）。
  - `api/unified_gateway.py`: 统一 API 网关实现，整合 RPC 与 V3 REST 接口。

- 领域层
  - 共识与链: `core/consensus.py`, `core/unified_consensus.py`, `core/transaction.py`, `core/utxo_store.py`
  - 算力与调度: `core/compute_scheduler.py`, `core/compute_market_v3.py`, `core/compute_market_orderbook.py`
  - 安全与隐私: `core/security.py`, `core/e2e_encryption.py`, `core/encrypted_task.py`, `core/tee_computing.py`
  - 执行引擎: `core/pouw_executor.py`, `core/sandbox_executor.py`

- 质量层
  - 单测: `tests/`
  - 安全回归工作流: `.github/workflows/security-regression.yml`

## 2. 关键程序链路（完整可用路径）

### 2.1 启动链路

1. 启动脚本进入 `main.py`
2. `POUWNode.initialize()` 先执行安全预检（生产模式 fail-closed）
3. 初始化存储、TLS、钱包、共识、RPC
4. 对外暴露 RPC 与前端静态资源

统一网关的独立启动链路：

1. 执行 `scripts/start_unified_gateway.py`
2. 脚本导入 `api/unified_gateway.py`
3. Flask 网关暴露 `/rpc`、`/api/v3/*`、`/api/unified/query`
4. 测试脚本通过 `tests/test_unified_gateway.py` 回归关键接口

关键目标:
- 生产环境证书与安全基线不满足时拒绝启动。
- 非生产环境允许受控降级并明确日志告警。

### 2.2 RPC 链路

1. HTTP 请求进入 RPC Server
2. 路由到 `NodeRPCService.handle_request()`
3. 权限校验（PUBLIC/USER/MINER/ADMIN）
4. 调用对应 handler
5. 统一响应返回

关键目标:
- 所有敏感接口必须有权限控制。
- 异常路径不能伪装为“成功/待处理”。
- 旧接口返回类型需保持兼容（例如部分列表接口保持 `[]` 合约）。

### 2.3 测试链路

- 快速安全回归:
  - `py -3 -m pytest tests/test_security_baseline_enforcement.py -q`
  - `py -3 -m pytest tests/test_security_regression_access.py -q`

- RPC 契约回归:
  - `py -3 -m pytest tests/test_rpc_error_contract.py -q`
  - `py -3 -m pytest tests/test_rpc*.py -q`

- 统一 smoke（启动检查 + 关键 RPC + 测试摘要）:
  - `py -3 scripts/unified_smoke.py`
  - 若本机未启动 RPC，可先跳过 RPC 阶段:
  - `py -3 scripts/unified_smoke.py --skip-rpc`

## 3. 本轮修复后的约定

- 生产模式统一判定来源:
  - `APP_ENV` / `MAINCOIN_ENV` / `POUW_ENV`
  - `MAINCOIN_PRODUCTION=true` 作为后备生产标记

- RPC 异常返回契约（适用于 Dict 型接口）:
  - `success: false`
  - `error: internal_error`
  - `errorCode: INTERNAL_ERROR`
  - `method: <method_name>`
  - `timestamp: <unix_ts>`

- 历史列表接口兼容策略:
  - 保持返回类型不变（失败仍可返回 `[]`）
  - 必须记录异常日志，避免静默吞错

## 4. 发布前检查清单

1. 安全基线
- 生产环境必须配置 `MAINCOIN_CA_CERT`
- 生产环境必须设置固定管理密钥（`POUW_ADMIN_KEY` 或 `MAINCOIN_ADMIN_KEY`）
- 检查是否误开启危险开关（例如自定义代码执行）

2. 回归测试
- 安全回归通过
- RPC 契约回归通过
- `scripts/unified_smoke.py` 全部阶段通过（或在离线场景显式 `--skip-rpc`）
- 核心链路无新增异常

3. 运行验证
- 节点可正常启动
- 关键 RPC 方法可访问且返回结构符合预期
- 日志中无高频未处理异常

## 5. 后续改进建议（按优先级）

1. 将 `core/rpc_service.py` 中剩余 broad-except 分批迁移到统一契约与日志策略。
2. 给更多历史接口补“返回契约测试”，防止后续重构破坏前后端联调。
3. 增加一个统一的 smoke 脚本，串联启动检查、关键 RPC 调用和测试摘要输出。
