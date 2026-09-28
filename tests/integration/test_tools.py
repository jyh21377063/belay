"""文件工具与 bash：在本机目录上经由 LocalEnv 执行，与容器内走同一套读写逻辑。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from belay.env import LocalEnv
from belay.tools import Policy, ToolContext, ToolError
from belay.tools.files import edit_file, grep_search, list_files, read_file, write_file
from belay.tools.shell import bash


def run(coro):
    return asyncio.run(coro)


def ctx_for(repo: Path, policy: Policy | None = None) -> ToolContext:
    env = LocalEnv(str(repo))
    return ToolContext(env=env, workdir=env.workdir, policy=policy or Policy())


def test_read_with_line_numbers_and_offset(repo):
    ctx = ctx_for(repo)
    out = run(read_file({"file_path": "pkg/mod.py"}, ctx))
    assert "     1\tdef add(a, b):" in out and "     2\t    return a - b" in out
    out = run(read_file({"file_path": "pkg/mod.py", "offset": 5, "limit": 1}, ctx))
    assert out.startswith("     5\tdef mul") and "use offset" in out


def test_edit_requires_read_and_unique_match(repo):
    ctx = ctx_for(repo)
    with pytest.raises(ToolError, match="before editing"):
        run(edit_file({"file_path": "pkg/mod.py", "old_string": "a - b", "new_string": "a + b"}, ctx))
    run(read_file({"file_path": "pkg/mod.py"}, ctx))
    out = run(edit_file({"file_path": "pkg/mod.py", "old_string": "a - b", "new_string": "a + b"}, ctx))
    assert "Edited" in out and "a + b" in out
    assert "return a + b" in (repo / "pkg" / "mod.py").read_text()
    with pytest.raises(ToolError, match="occurs 2 times"):
        run(edit_file({"file_path": "pkg/mod.py", "old_string": "(a, b)", "new_string": "(x, y)"}, ctx))
    run(edit_file({"file_path": "pkg/mod.py", "old_string": "(a, b)", "new_string": "(x, y)", "replace_all": True}, ctx))
    assert (repo / "pkg" / "mod.py").read_text().count("(x, y)") == 2


def test_edit_quote_normalization(repo):
    ctx = ctx_for(repo)
    run(read_file({"file_path": "pkg/util.py"}, ctx))
    out = run(edit_file({"file_path": "pkg/util.py", "old_string": 'X = "hello"', "new_string": "X = 'hi'",
                         "replace_all": True}, ctx))
    assert "normalizing quotes" in out
    assert (repo / "pkg" / "util.py").read_text() == "X = 'hi'\nX = 'hi'\n"


def test_write_new_and_overwrite_rules(repo):
    ctx = ctx_for(repo)
    run(write_file({"file_path": "pkg/new/deep.py", "content": "print('ok')\n"}, ctx))
    assert (repo / "pkg" / "new" / "deep.py").read_text() == "print('ok')\n"
    with pytest.raises(ToolError, match="already exists"):
        run(write_file({"file_path": "README.md", "content": "x"}, ctx))


def test_write_preserves_non_utf8_bytes_and_large_files(repo):
    ctx = ctx_for(repo)
    raw = b"# \xff\xfe latin\nvalue = 1\n"
    (repo / "legacy.py").write_bytes(raw)
    run(read_file({"file_path": "legacy.py"}, ctx))
    run(edit_file({"file_path": "legacy.py", "old_string": "value = 1", "new_string": "value = 2"}, ctx))
    assert (repo / "legacy.py").read_bytes() == raw.replace(b"value = 1", b"value = 2")
    big = "\n".join(f"line {i} " + "x" * 80 for i in range(5000))       # 约 450KB，跨多个写入分块
    run(write_file({"file_path": "big.txt", "content": big}, ctx))
    assert (repo / "big.txt").read_text() == big


def test_glob_and_list_files(repo):
    ctx = ctx_for(repo)
    out = run(list_files({"pattern": "**/*.py"}, ctx))
    assert out.splitlines() == ["pkg/mod.py", "pkg/util.py", "tests/test_mod.py"]
    assert run(list_files({"pattern": "tests/test_*.py"}, ctx)) == "tests/test_mod.py"


def test_grep(repo):
    ctx = ctx_for(repo)
    out = run(grep_search({"pattern": "def (add|mul)", "include": "*.py"}, ctx))
    assert "mod.py:1:def add" in out and "mod.py:5:def mul" in out
    assert run(grep_search({"pattern": "nothing_here"}, ctx)) == "No matches"


def test_bash_exit_code_and_timeout(repo):
    ctx = ctx_for(repo)
    assert run(bash({"command": "echo hi"}, ctx)) == "hi"
    assert "[exit code 3]" in run(bash({"command": "echo boom; exit 3"}, ctx))
    out = run(bash({"command": "sleep 5", "timeout": 1}, ctx))
    assert "timed out after 1s" in out


def test_policy_audit_vs_deny(repo):
    ctx = ctx_for(repo)                                   # 默认 audit：放行但记录
    run(bash({"command": "git stash list"}, ctx))
    assert ctx.events and ctx.events[-1]["category"] == "git_write" and ctx.events[-1]["action"] == "audit"
    strict = ctx_for(repo, Policy.strict())
    with pytest.raises(ToolError, match="Git write"):
        run(bash({"command": "git -c user.name=x commit -am hack"}, strict))
    with pytest.raises(ToolError, match="offline"):
        run(bash({"command": "curl https://github.com/x/y"}, strict))
    with pytest.raises(ToolError, match="task repository"):
        run(bash({"command": "find / -name '*.py'"}, strict))
    with pytest.raises(ToolError, match="evaluation harness"):
        run(bash({"command": "cat /opt/belay-gate/spec.json"}, strict))
    with pytest.raises(ToolError, match="evaluation harness"):
        run(read_file({"file_path": "/logs/verifier/reward.json"}, ctx))
    assert run(bash({"command": "git status --short"}, strict)) is not None     # 只读 git 命令放行


# ---- 读后被改检测 -----------------------------------------------------------------

def test_stale_edit_after_external_change(repo):
    ctx = ctx_for(repo)
    run(read_file({"file_path": "pkg/mod.py"}, ctx))
    (repo / "pkg" / "mod.py").write_text("def add(a, b):\n    return a - b  # changed elsewhere\n")
    with pytest.raises(ToolError, match="modified since you last read"):
        run(edit_file({"file_path": "pkg/mod.py", "old_string": "a - b", "new_string": "a + b"}, ctx))
    assert ctx.events[-1]["kind"] == "stale_edit"
    run(read_file({"file_path": "pkg/mod.py"}, ctx))                     # 重新读取后可以编辑
    run(edit_file({"file_path": "pkg/mod.py", "old_string": "a - b", "new_string": "a + b"}, ctx))
    assert "a + b  # changed elsewhere" in (repo / "pkg" / "mod.py").read_text()


def test_stale_edit_after_own_bash_command(repo):
    ctx = ctx_for(repo)
    run(read_file({"file_path": "pkg/mod.py"}, ctx))
    run(bash({"command": "sed -i 's/a \\* b/b * a/' pkg/mod.py"}, ctx))
    with pytest.raises(ToolError, match="modified since you last read"):
        run(edit_file({"file_path": "pkg/mod.py", "old_string": "a - b", "new_string": "a + b"}, ctx))


def test_own_edits_do_not_require_rereading(repo):
    ctx = ctx_for(repo)
    run(read_file({"file_path": "pkg/mod.py", "offset": 5, "limit": 1}, ctx))   # 只读了一部分也算读过
    run(edit_file({"file_path": "pkg/mod.py", "old_string": "a - b", "new_string": "a + b"}, ctx))
    run(edit_file({"file_path": "pkg/mod.py", "old_string": "a * b", "new_string": "b * a"}, ctx))
    run(write_file({"file_path": "pkg/mod.py", "content": "X = 1\n"}, ctx))
    assert (repo / "pkg" / "mod.py").read_text() == "X = 1\n"


def test_overwrite_checks_freshness(repo):
    ctx = ctx_for(repo)
    run(read_file({"file_path": "README.md"}, ctx))
    (repo / "README.md").write_text("someone else\n")
    with pytest.raises(ToolError, match="modified since you last read"):
        run(write_file({"file_path": "README.md", "content": "mine\n"}, ctx))
    assert (repo / "README.md").read_text() == "someone else\n"


def test_edit_refuses_files_too_large_to_round_trip(repo):
    ctx = ctx_for(repo)
    (repo / "huge.txt").write_text("x" * (9 * 1024 * 1024))
    run(read_file({"file_path": "huge.txt", "limit": 1}, ctx))
    with pytest.raises(ToolError, match="cannot be edited"):
        run(edit_file({"file_path": "huge.txt", "old_string": "xxx", "new_string": "y"}, ctx))
    assert (repo / "huge.txt").stat().st_size == 9 * 1024 * 1024        # 没有被截断
