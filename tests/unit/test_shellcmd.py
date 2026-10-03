"""bash 命令分类：只看引号外切出的每一段的命令词。"""
from __future__ import annotations

import pytest

from belay.runtime.shellcmd import bash_passed, is_read_only, is_run_command, is_test_command

CASES = [
    # 命令, 测试?, 只读?, 运行?
    ("pytest -x tests/", True, False, True),
    ("cd repo && PYTHONPATH=. timeout 600 python -m pytest -q", True, False, True),
    ("make test", True, False, True),
    ("npm test", True, False, True),
    ("npm run test:unit", True, False, True),
    ("cargo test", True, False, True),
    ("go test ./...", True, False, True),
    ("./gradlew test", True, False, True),
    ("uv run pytest -q", True, False, True),
    ("nice -n 10 python -m unittest discover", True, False, True),
    ("pip3 install -e . && pytest", True, False, True),
    ("cat pytest.ini", False, True, False),
    ('grep -rn "pytest\\|unittest" .', False, True, False),
    ('cd /testbed && grep -rn "filter_rrevs\\|filter_prevs" conans/', False, True, False),
    ("pip install pytest", False, False, False),
    ("ps aux | grep pytest", False, True, False),
    ("tail -n 50 log.txt; ps aux | grep python", False, True, False),
    ('tail -c 300 /tmp/bigtest.log; echo; ps aux | grep -c "[p]ytest"', False, True, False),
    ("git diff HEAD~1 | head", False, True, False),
    ("sed -n '1,20p' a.py", False, True, False),
    ("xargs grep foo", False, True, False),
    ("cat > a.py <<EOF\npytest -x\nEOF", False, True, False),           # heredoc 正文不是命令
    ("python simulate.py --score", False, False, True),
    ("cd /testbed && python - <<'EOF'\nimport pytest\nprint(1)\nEOF", False, False, True),
    ("cd /app && time simulate score && simulate analyze --traces x", False, False, True),
    ("cd /app && rm -rf workspace/__pycache__ && simulate score 2>&1 | tail -4", False, False, True),
    ("cd /app && POLICY_PATH=$PWD/p.py OUTPUT_DIR=$PWD/o simulate run", False, False, True),
    ("git commit -am x", False, False, False),
    ("sed -i 's/a/b/' a.py", False, False, False),
    ("mkdir -p x && cp a b", False, False, False),
    ("find . -name '*.pyc' | xargs rm -f", False, False, False),
    ("echo 'unterminated", False, False, False),                       # 解析失败：保守
]


@pytest.mark.parametrize("cmd,test,ro,run", CASES)
def test_command_classification(cmd, test, ro, run):
    assert (is_test_command(cmd), is_read_only(cmd), is_run_command(cmd)) == (test, ro, run)


def test_generic_runs_can_be_switched_off():
    assert is_run_command("python simulate.py") and not is_run_command("python simulate.py", generic=False)
    assert is_run_command("pytest -q", generic=False)


def test_bash_passed_reads_the_shell_tool_output():
    assert bash_passed({}, "ok") and bash_passed({}, "(no output)")
    assert not bash_passed({}, "boom\n\n[exit code 1]")
    assert not bash_passed({}, "[exit code 2, no output]")
    assert not bash_passed({}, "x\n\n[Command timed out after 120s and was killed. Increase timeout or run long "
                               "commands in the background]")
    assert not bash_passed({"run_in_background": True}, "Started in background (id=1)")
