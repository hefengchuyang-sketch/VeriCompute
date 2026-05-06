"""上线前全面检查脚本"""
import os, sys, py_compile, importlib, yaml, traceback

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
os.chdir(PROJECT_ROOT)
sys.path.insert(0, PROJECT_ROOT)

print("=" * 60)
print("上线前全面检查")
print("=" * 60)

passed = 0
failed = 0
warnings = []

def check(name, ok, detail=""):
    global passed, failed
    if ok:
        print(f"  [PASS] {name}")
        passed += 1
    else:
        print(f"  [FAIL] {name} — {detail}")
        failed += 1

def warn(msg):
    warnings.append(msg)
    print(f"  [WARN] {msg}")

# ========== 1. 语法检查 ==========
print("\n--- 1. 语法检查 (core/*.py) ---")
core_files = sorted([f for f in os.listdir("core") if f.endswith(".py")])
syntax_errors = []
for f in core_files:
    path = os.path.join("core", f)
    try:
        py_compile.compile(path, doraise=True)
    except py_compile.PyCompileError as e:
        syntax_errors.append((f, str(e)))
check(f"core/ 下 {len(core_files)} 个文件语法", len(syntax_errors) == 0,
      f"{len(syntax_errors)} 个错误: {syntax_errors}")

# 根目录关键文件
root_files = ["main.py", "config.yaml"]
for rf in root_files:
    if rf.endswith(".py"):
        try:
            py_compile.compile(rf, doraise=True)
            check(f"{rf} 语法", True)
        except Exception as e:
            check(f"{rf} 语法", False, str(e))

# ========== 2. 关键模块导入 ==========
print("\n--- 2. 关键模块导入 ---")
critical_modules = [
    "core.consensus",
    "core.crypto",
    "core.unified_consensus",
    "core.pouw_chain_v3",
    "core.pouw_executor",
    "core.rpc_service",
    "core.treasury_manager",
    "core.dao_treasury",
    "core.pouw_scoring",
    "core.sector_coin",
    "core.compute_market_v3",
    "core.account",
    "core.wallet",
    "core.security",
]
import_failures = []
for mod in critical_modules:
    try:
        importlib.import_module(mod)
        check(f"import {mod}", True)
    except Exception as e:
        check(f"import {mod}", False, str(e)[:100])
        import_failures.append(mod)

# ========== 3. 配置文件检查 ==========
print("\n--- 3. 配置文件完整性 ---")
try:
    with open("config.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    check("config.yaml 可解析", True)
    
    # 关键字段
    required_sections = ["network", "consensus", "mining", "wallet", "storage"]
    for sec in required_sections:
        check(f"config.yaml 包含 [{sec}]", sec in cfg, "缺失")
    
    # 网络类型
    net_type = cfg.get("network", {}).get("type", "")
    check(f"网络类型: {net_type}", net_type in ("mainnet", "testnet"), "未知类型")
    
    # 财库税率
    tr = cfg.get("consensus", {}).get("treasury_rate")
    check(f"财库税率配置: {tr}", tr is not None and 0 < tr < 1, "未配置或范围异常")
    
    # RPC 安全
    rpc_cfg = cfg.get("rpc", {}) or cfg.get("network", {}).get("rpc", {})
    rpc_host = rpc_cfg.get("host", "0.0.0.0")
    rpc_port = rpc_cfg.get("port", 0)
    check(f"RPC 端口: {rpc_port}", rpc_port > 0, "未配置")
    if rpc_host == "0.0.0.0":
        warn("RPC host=0.0.0.0 对外暴露，确认是否需要限制为 127.0.0.1")
    
    # API Key
    api_key = rpc_cfg.get("api_key", "")
    if not api_key or api_key == "your_secret_api_key":
        warn("RPC api_key 未设置或使用默认值，上线前必须修改!")
    else:
        check("RPC api_key 已配置", True)
    
    # P2P
    p2p = cfg.get("p2p", cfg.get("network", {}))
    p2p_port = p2p.get("port", p2p.get("p2p_port", 0))
    if p2p_port:
        check(f"P2P 端口: {p2p_port}", True)
    
except Exception as e:
    check("config.yaml 加载", False, str(e))

# ========== 4. 安全检查 ==========
print("\n--- 4. 安全检查 ---")
# 检查是否有硬编码的私钥/密码
import re
danger_patterns = [
    (r'private_key\s*=\s*["\'][0-9a-fA-F]{64}', "硬编码私钥"),
    (r'password\s*=\s*["\'](?!{).{8,}["\']', "硬编码密码"),
]
danger_files = []
for dirpath, dirs, files in os.walk("core"):
    dirs[:] = [d for d in dirs if d != "__pycache__"]
    for fname in files:
        if not fname.endswith(".py"):
            continue
        fpath = os.path.join(dirpath, fname)
        try:
            with open(fpath, "r", encoding="utf-8", errors="ignore") as fh:
                content = fh.read()
            for pat, desc in danger_patterns:
                if re.search(pat, content):
                    danger_files.append((fpath, desc))
        except:
            pass
if danger_files:
    for df, desc in danger_files:
        warn(f"{df}: {desc}")
else:
    check("无硬编码密钥/密码", True)

# 检查 DEBUG 模式
try:
    with open("config.yaml", "r", encoding="utf-8") as f:
        raw = f.read()
    if "debug: true" in raw.lower() or "debug_mode: true" in raw.lower():
        warn("config.yaml 中 debug=true，上线前应关闭")
    else:
        check("DEBUG 模式关闭", True)
except:
    pass

# ========== 5. 数据目录 ==========
print("\n--- 5. 数据目录 ---")
data_dir = os.path.join(PROJECT_ROOT, "data")
if os.path.isdir(data_dir):
    db_files = [f for f in os.listdir(data_dir) if f.endswith(".db")]
    check(f"data/ 目录存在 ({len(db_files)} 个数据库)", True)
    for db in db_files:
        size = os.path.getsize(os.path.join(data_dir, db))
        print(f"        {db}: {size/1024:.1f} KB")
else:
    warn("data/ 目录不存在，首次启动会自动创建")

# ========== 6. 依赖检查 ==========
print("\n--- 6. 关键依赖版本 ---")
deps = ["yaml", "hashlib", "ecdsa", "flask", "requests", "sqlite3"]
for dep in deps:
    try:
        m = importlib.import_module(dep)
        ver = getattr(m, "__version__", getattr(m, "version", "内置"))
        check(f"{dep} ({ver})", True)
    except ImportError:
        check(f"{dep}", False, "未安装")

# ========== 7. 一致性检查 ==========
print("\n--- 7. 一致性检查 ---")
try:
    from core.consensus import ChainParams, RewardCalculator
    tr_yaml = cfg.get("consensus", {}).get("treasury_rate", -1)
    tr_code = ChainParams.TREASURY_RATE
    tr_calc = RewardCalculator().treasury_rate
    check(f"财库税率一致 (config={tr_yaml}, code={tr_code}, calc={tr_calc})",
          tr_yaml == tr_code == tr_calc)
except Exception as e:
    check("财库税率一致性", False, str(e))

# 检查创世块
genesis_files = [f for f in os.listdir(PROJECT_ROOT) if f.startswith("genesis") and f.endswith(".json")]
for gf in genesis_files:
    check(f"创世块文件: {gf}", True)

# ========== 8. Docker / 部署 ==========
print("\n--- 8. 部署文件 ---")
deploy_files = ["Dockerfile", "docker-compose.yml", "requirements.txt", "start.bat", "start.ps1"]
for df in deploy_files:
    check(f"{df}", os.path.exists(os.path.join(PROJECT_ROOT, df)))

# ========== 9. main.py 入口检查 ==========
print("\n--- 9. main.py 入口 ---")
try:
    with open(os.path.join(PROJECT_ROOT, "main.py"), "r", encoding="utf-8") as f:
        main_content = f.read()
    check("main.py 存在且可读", True)
    check("包含 POUWNode 类", "class POUWNode" in main_content)
    check("包含 main()", "def main(" in main_content or "async def main(" in main_content)
    check("包含优雅关闭", "graceful" in main_content.lower() or "shutdown" in main_content.lower())
    check("包含扩展模块初始化", "_init_extended_modules" in main_content)
except Exception as e:
    check("main.py", False, str(e))

# ========== 10. 前端构建 ==========
print("\n--- 10. 前端 ---")
frontend_dist = os.path.join(PROJECT_ROOT, "frontend", "dist")
if os.path.isdir(frontend_dist):
    html = os.path.join(frontend_dist, "index.html")
    check("前端已构建 (dist/)", os.path.exists(html))
else:
    warn("frontend/dist/ 不存在，需执行 npm run build")

# ========== 汇总 ==========
print("\n" + "=" * 60)
print(f"检查结果: {passed} 通过, {failed} 失败, {len(warnings)} 警告")
print("=" * 60)

if warnings:
    print("\n⚠️  警告事项:")
    for i, w in enumerate(warnings, 1):
        print(f"  {i}. {w}")

if failed > 0:
    print(f"\n❌ 有 {failed} 项失败，需修复后再上线")
elif warnings:
    print(f"\n⚠️  无失败，但有 {len(warnings)} 个警告需确认")
else:
    print("\n✅ 所有检查通过，可以上线!")
