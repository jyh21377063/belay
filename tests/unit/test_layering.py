"""依赖规则检查：违反即失败，比靠自觉可靠。

  belay/core     纯函数：不依赖 runtime / worker / tools / llm / env，不 import 任何做 IO 或读时钟的模块
  belay/tools    不认识 runtime 与 core 的内部实现（只经由 ToolContext.runtime 提请求）
  belay/worker   B 组的 worker：不依赖 Belay 的 runtime 与 core（对照组干净）
  belay          不依赖 eval
  belay/container 只用标准库（上传到容器里执行）
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

FORBIDDEN = {
    "belay": ["eval"],
    "belay/core": ["belay.runtime", "belay.worker", "belay.tools", "belay.llm", "belay.env", "belay.cli"],
    "belay/tools": ["belay.runtime", "belay.worker", "belay.core"],
    "belay/worker": ["belay.runtime", "belay.core"],
}
IO_OR_CLOCK = {"asyncio", "subprocess", "sqlite3", "os", "anthropic", "socket", "shutil", "time", "random",
               "pathlib", "threading", "datetime"}


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


def test_core_is_pure():
    bad = [f"{f.relative_to(ROOT)} imports {n}" for f in files_under("belay/core")
           for n in imports(f) if n.split(".")[0] in IO_OR_CLOCK]
    assert not bad, "belay/core 必须是纯函数：\n" + "\n".join(bad)


def test_container_scripts_are_stdlib_only():
    stdlib = set(sys.stdlib_module_names)
    bad = [f"{f.relative_to(ROOT)} imports {n}" for f in files_under("belay/container") if f.name != "__init__.py"
           for n in imports(f) if n.split(".")[0] not in stdlib and n != "__future__"]
    assert not bad, "\n".join(bad)


def test_test_path_rule_is_shared():
    """runner.py（容器内）与 core/verify.py 的测试路径规则必须一致。"""
    runner = (ROOT / "belay/container/runner.py").read_text(encoding="utf-8")
    verify = (ROOT / "belay/core/verify.py").read_text(encoding="utf-8")
    pat = re.compile(r'TEST_PATH = re\.compile\((r".*?")\)')
    assert pat.search(runner).group(1) == pat.search(verify).group(1)
