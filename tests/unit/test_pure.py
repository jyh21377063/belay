"""纯函数：截断、glob、清理过期结果、行动边界的匹配规则。"""
from __future__ import annotations

import pytest

from belay.tools import Policy, ToolContext, ToolError
from belay.tools.files import glob_to_regex
from belay.tools.output import truncate_output
from belay.worker.context import CLEARED, clear_stale_results
from tests.conftest import tool_use


def test_truncate_keeps_error_lines():
    lines = [f"tests/test_x.py::test_{i} PASSED" for i in range(3000)]
    lines[1500] = "FAILED tests/test_x.py::test_1500 - AssertionError: boom"
    out = truncate_output("\n".join(lines), max_chars=20000)
    assert "FAILED tests/test_x.py::test_1500" in out and "lines omitted" in out and len(out) <= 20000
    assert truncate_output("short") == "short"


def test_glob_to_regex():
    assert glob_to_regex("**/*.py").match("pkg/mod.py")
    assert glob_to_regex("**/*.py").match("top.py")
    assert glob_to_regex("pkg/*.{py,md}").match("pkg/mod.py")
    assert not glob_to_regex("pkg/*.py").match("pkg/sub/mod.py")


def test_clear_stale_results_keeps_pairs():
    msgs = [{"role": "user", "content": "task"}]
    for i in range(3):
        msgs.append({"role": "assistant", "content": [tool_use(f"r{i}", "read_file", file_path="a.py")]})
        msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"r{i}", "content": f"v{i}"}]})
    n = clear_stale_results(msgs)
    contents = [m["content"][0]["content"] for m in msgs[2::2]]
    assert n == 2 and contents == [CLEARED, CLEARED, "v2"]


@pytest.mark.parametrize("cmd,category", [
    ("git -c user.name=x commit -am hack", "git_write"),
    ("git stash", "git_write"),
    ("curl https://github.com/x/y", "network"),
    ("pip download pkg==1.0", "network"),
    ("find / -name '*.py'", "disk_search"),
    ("grep -rn tint_image /usr", "disk_search"),
    ("cat /opt/belay-gate/spec.json", "harness_paths"),
    ("ls /logs/verifier", "harness_paths"),
])
def test_policy_rejects(cmd, category):
    ctx = ToolContext(env=None, workdir="/testbed", policy=Policy.strict())
    with pytest.raises(ToolError):
        ctx.check_command(cmd)
    assert ctx.events[-1]["category"] == category


@pytest.mark.parametrize("cmd", ["git status --short", "git diff HEAD", "git log --oneline -5",
                                 "python -m pytest tests -q", "grep -rn foo src/"])
def test_policy_allows(cmd):
    ctx = ToolContext(env=None, workdir="/testbed", policy=Policy.strict())
    ctx.check_command(cmd)
    assert not ctx.events


def test_policy_audit_records_but_allows():
    ctx = ToolContext(env=None, workdir="/testbed", policy=Policy())
    ctx.check_command("git stash list")
    assert ctx.events[-1]["action"] == "audit"


@pytest.mark.parametrize("cmd", ["echo x > f.txt", "echo \"a\" > f", "cat a >> b", "rm -rf build", "sed -i 's/a/b/' f",
                                 "pip install x", "git checkout -- .", "tee out.txt", "python x.py > out.log", "touch a"])
def test_read_only_rejects_writes(cmd):
    ctx = ToolContext(env=None, workdir="/testbed", read_only=True)
    with pytest.raises(ToolError, match="read-only"):
        ctx.check_read_only(cmd)
    assert ctx.events[-1]["category"] == "read_only"


@pytest.mark.parametrize("cmd", ["ls -la", "cat f 2>/dev/null", "grep -rn foo . 2>&1 | head", "git log --oneline -5",
                                 "python -c 'import x; print(x.__file__)'", "sed -n 1,20p f", "cmd >/dev/null 2>&1",
                                 "cat a | grep 'x -> y'", "grep -n \"a > b\" f.py", "git diff HEAD~1"])
def test_read_only_allows_reads(cmd):
    ctx = ToolContext(env=None, workdir="/testbed", read_only=True)
    ctx.check_read_only(cmd)
    assert not ctx.events
