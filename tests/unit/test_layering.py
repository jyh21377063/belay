"""依赖规则检查（见 docs/architecture.md）：违反即失败，比靠自觉可靠。"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "belay"

# 目录 → 不允许 import 的前缀
FORBIDDEN = {
    "belay": ["eval"],                                            # eval 可以依赖 belay，反过来不行
    "belay/tools": ["belay.worker", "belay.runtime", "belay.graph"],
    "belay/worker": ["belay.runtime", "belay.graph"],             # 与 Orchestrator 只经由 ToolContext.runtime
    "belay/graph": ["belay.runtime", "belay.worker", "belay.tools", "belay.llm", "belay.env"],
}
# decide.py 只允许依赖图的纯函数部分与消息定义（不能依赖 store：那是 IO）
PURE_DECIDE = {"belay.graph.model", "belay.graph.evidence", "belay.graph.ledger", "belay.graph.requirements",
               "belay.runtime.messages"}
PURE_GRAPH = ["model.py", "evidence.py", "ledger.py", "requirements.py", "build.py", "invariants.py"]
IO_MODULES = {"asyncio", "subprocess", "sqlite3", "os", "anthropic", "socket", "shutil"}


def imports(path: Path) -> list[str]:
    names = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.append(node.module)
    return names


def files_under(rel: str) -> list[Path]:
    base = ROOT / rel
    return sorted(base.rglob("*.py")) if base.exists() else []


def test_forbidden_imports():
    bad = []
    for rel, prefixes in FORBIDDEN.items():
        for f in files_under(rel):
            for name in imports(f):
                if any(name == p or name.startswith(p + ".") for p in prefixes):
                    bad.append(f"{f.relative_to(ROOT)} imports {name}")
    assert not bad, "\n".join(bad)


def test_container_scripts_are_stdlib_only():
    stdlib = set(sys.stdlib_module_names)
    bad = [f"{f.relative_to(ROOT)} imports {n}" for f in files_under("belay/container") if f.name != "__init__.py"
           for n in imports(f) if n.split(".")[0] not in stdlib and n != "__future__"]
    assert not bad, "\n".join(bad)


def test_decide_is_pure():
    f = PKG / "runtime" / "decide.py"
    if not f.exists():
        return
    bad = [n for n in imports(f) if n.startswith("belay") and n not in PURE_DECIDE]
    bad += [n for n in imports(f) if n.split(".")[0] in IO_MODULES]
    assert not bad, f"decide.py 必须是纯函数，不能依赖：{bad}"


def test_graph_logic_is_pure():
    bad = []
    for name in PURE_GRAPH:
        f = PKG / "graph" / name
        bad += [f"{name} imports {n}" for n in imports(f) if n.split(".")[0] in IO_MODULES or n == "belay.graph.store"]
    assert not bad, "\n".join(bad)
