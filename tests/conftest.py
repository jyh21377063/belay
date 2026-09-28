"""pytest 公共配置。

  tests/unit         纯逻辑，不起进程
  tests/integration  LocalEnv + ScriptedLLM，完整跑一遍，不需要容器和模型
  tests/docker       需要容器，默认跳过：python -m pytest -m docker
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def pytest_collection_modifyitems(config, items):
    if config.getoption("-m") and "docker" in config.getoption("-m"):
        return
    skip = pytest.mark.skip(reason="需要容器：用 -m docker 运行")
    for item in items:
        if "docker" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """一个小型 git 仓库：pkg/mod.py 里的 add() 有 bug。"""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b\n")
    (tmp_path / "pkg" / "util.py").write_text("X = \u201chello\u201d\nX = \u201chello\u201d\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_mod.py").write_text("from pkg.mod import add\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    (tmp_path / "README.md").write_text("demo\n")
    subprocess.run("git init -q && git add -A && git -c user.email=a@b -c user.name=t commit -qm init",
                   shell=True, cwd=tmp_path, check=True)
    return tmp_path


def tool_use(id_: str, name: str, **inp) -> dict:
    return {"type": "tool_use", "id": id_, "name": name, "input": inp}
