# -*- coding: utf-8 -*-
"""
生产共识入口守护测试 (Production Consensus Entrypoint Guard)

目的:
    保证 maincoin 项目只有一个生产共识入口 ``core.consensus.ConsensusEngine``。
    防止实验模块 (unified_consensus / dual_layer_consensus / pouw_chain_v3)
    被意外导入到 main.py 或 RPC 服务等生产路径。

参考:
    docs/TECHNICAL_REVIEW_AND_CONSENSUS_RECOMMENDATIONS_2026-05-09.md §7
"""

import ast
import inspect
import re
import textwrap
import unittest
from pathlib import Path

# 项目根目录: tests/.. -> maincoin/
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 实验模块清单：这些模块绝不允许被生产路径直接导入
EXPERIMENTAL_MODULES = (
    "core.unified_consensus",
    "core.dual_layer_consensus",
    "core.pouw_chain_v3",
)

# 实验模块对应的类/工厂名（用于扫描裸 import 之外的引用）
EXPERIMENTAL_SYMBOLS = (
    "UnifiedConsensus",
    "POUWChainV3",
    "DualLayerConsensus",
)

# 生产路径白名单：以下文件被视为生产代码，不允许导入实验模块
PRODUCTION_PATHS = (
    "main.py",
    "core/rpc_service.py",
)

# 生产路径目录：以下目录下所有 .py 文件被视为生产代码
PRODUCTION_DIRS = (
    "core/rpc",
    "core/rpc_handlers",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def _iter_production_files():
    for rel in PRODUCTION_PATHS:
        p = PROJECT_ROOT / rel
        if p.exists():
            yield p
    for rel in PRODUCTION_DIRS:
        d = PROJECT_ROOT / rel
        if d.exists():
            for p in d.rglob("*.py"):
                yield p


class TestProductionConsensusEntrypoint(unittest.TestCase):
    """守护 main.py 与生产 RPC 路径，确保只走 core.consensus.ConsensusEngine。"""

    def test_main_uses_single_consensus_engine(self):
        """main.py 的 _init_consensus 必须只导入 core.consensus.ConsensusEngine。

        审查报告 §7.2 任务 C 指定的最小断言。
        """
        import main  # noqa: WPS433  (导入位置：测试需要在断言点导入)

        # main.py 实际类名为 POUWNode，审查报告样例用的是占位名 MainCoinNode
        node_cls = getattr(main, "POUWNode", None) or getattr(main, "MainCoinNode", None)
        self.assertIsNotNone(
            node_cls,
            "main.py 未导出生产节点类（POUWNode 或 MainCoinNode）",
        )
        full_source = inspect.getsource(node_cls._init_consensus)

        # 报告样例直接对完整源码做 assertIn —— 简单但会把 docstring 里的
        # 警告文本（"禁止...UnifiedConsensus"）误判为引用。
        # 这里通过 AST 剥离 docstring，只对真正的代码体断言。
        tree = ast.parse(textwrap.dedent(full_source))
        func_def = tree.body[0]
        body_nodes = func_def.body
        if (
            body_nodes
            and isinstance(body_nodes[0], ast.Expr)
            and isinstance(body_nodes[0].value, ast.Constant)
            and isinstance(body_nodes[0].value.value, str)
        ):
            body_nodes = body_nodes[1:]
        code_body = "\n".join(ast.unparse(node) for node in body_nodes)

        self.assertIn(
            "from core.consensus import ConsensusEngine",
            code_body,
            "main._init_consensus 必须显式从 core.consensus 导入 ConsensusEngine",
        )

        for symbol in EXPERIMENTAL_SYMBOLS:
            self.assertNotIn(
                symbol,
                code_body,
                f"main._init_consensus 不允许在代码体中引用实验类 {symbol}",
            )

    def test_consensus_module_marked_as_production_entrypoint(self):
        """core/consensus.py 必须显式标记为生产入口。"""
        from core import consensus

        self.assertTrue(
            getattr(consensus, "IS_PRODUCTION_CONSENSUS_ENTRYPOINT", False),
            "core.consensus 必须设置 IS_PRODUCTION_CONSENSUS_ENTRYPOINT=True 标识",
        )

    def test_experimental_modules_marked_experimental_only(self):
        """三个实验模块必须显式声明 EXPERIMENTAL_ONLY=True。"""
        import importlib

        for mod_name in EXPERIMENTAL_MODULES:
            with self.subTest(module=mod_name):
                try:
                    mod = importlib.import_module(mod_name)
                except Exception as exc:  # 实验模块依赖可能缺失，跳过
                    self.skipTest(f"无法导入 {mod_name}: {exc}")
                    continue
                self.assertTrue(
                    getattr(mod, "EXPERIMENTAL_ONLY", False),
                    f"{mod_name} 必须声明 EXPERIMENTAL_ONLY=True",
                )

    def test_production_files_do_not_import_experimental_modules(self):
        """扫描所有生产路径的 .py 源码，禁止出现 import core.unified_consensus 等。

        覆盖以下写法：
          - import core.unified_consensus
          - import core.unified_consensus as uc
          - from core.unified_consensus import X
          - from core import unified_consensus
        """
        offenders = []

        # 三段式：检测 core.<module>
        dotted = "|".join(re.escape(m.split(".", 1)[1]) for m in EXPERIMENTAL_MODULES)
        pat_import_dotted = re.compile(
            rf"^\s*import\s+core\.({dotted})(\s|$|,)",
            re.MULTILINE,
        )
        pat_from_dotted = re.compile(
            rf"^\s*from\s+core\.({dotted})\s+import\b",
            re.MULTILINE,
        )
        # from core import unified_consensus[, ...]
        pat_from_core = re.compile(
            rf"^\s*from\s+core\s+import\s+([^\n#]+)",
            re.MULTILINE,
        )

        forbidden_names = {m.split(".", 1)[1] for m in EXPERIMENTAL_MODULES}

        for path in _iter_production_files():
            text = _read(path)

            for m in pat_import_dotted.finditer(text):
                offenders.append((str(path.relative_to(PROJECT_ROOT)), m.group(0).strip()))
            for m in pat_from_dotted.finditer(text):
                offenders.append((str(path.relative_to(PROJECT_ROOT)), m.group(0).strip()))
            for m in pat_from_core.finditer(text):
                imported = m.group(1)
                # 拆出 import 列表中的名字（忽略 as 别名、括号、空白）
                cleaned = re.sub(r"[()]", " ", imported)
                names = [n.strip().split(" as ")[0].strip() for n in cleaned.split(",")]
                hit = forbidden_names.intersection(names)
                if hit:
                    offenders.append(
                        (str(path.relative_to(PROJECT_ROOT)), m.group(0).strip())
                    )

        self.assertEqual(
            offenders,
            [],
            "以下生产文件违规导入了实验共识模块：\n  "
            + "\n  ".join(f"{p}: {line}" for p, line in offenders),
        )


if __name__ == "__main__":
    unittest.main()
