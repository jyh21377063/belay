"""会话内的打转断路器（belay.core.loops）：只抓逐字的循环，每段连续只在正好达到阈值时报一次。"""
from __future__ import annotations

from belay.core import loops
from belay.core.loops import ALTERNATE, ERROR_REPEAT, REPEAT, check, reminder, step


def run(steps):
    """逐步喂给 check，返回每一步的命中（None 或 Loop）。"""
    out, hist = [], []
    for s in steps:
        hist.append(s)
        del hist[:-loops.KEEP]
        out.append(check(hist))
    return out


def kinds(hits):
    return [(i, h.kind, h.n) for i, h in enumerate(hits) if h is not None]


READ_A = step("read_file", {"file_path": "a.py"}, "1\tdef f(): pass", False)
READ_B = step("read_file", {"file_path": "b.py"}, "1\tdef g(): pass", False)
EDIT = step("edit_file", {"file_path": "a.py", "old_string": "x", "new_string": "y"}, "edited", False)


def pytest_out(dur: str, rc: int = 1) -> str:
    return f"FAILED tests/test_a.py::test_x - assert 1 == 2\n1 failed in {dur}s\n\n[exit code {rc}]"


def test_same_call_same_result_four_times_is_reported_once():
    hits = run([READ_A] * 7)
    assert kinds(hits) == [(3, "repeat", 4)]
    assert "read_file" in hits[3].detail and hits[3].sig.startswith("loop:repeat:")
    assert reminder(hits[3]).startswith("Loop check:")


def test_a_different_call_in_between_breaks_the_streak():
    assert kinds(run([READ_A, READ_A, READ_A, READ_B, READ_A, READ_A, READ_A])) == []


def test_timings_do_not_make_outputs_different_but_real_changes_do():
    same = [step("bash", {"command": "pytest -q"}, pytest_out(d, 0).replace("FAILED", "ok"), False)
            for d in ("0.12", "0.40", "1.03", "0.20")]
    assert kinds(run(same)) == [(3, "repeat", 4)]
    changing = [step("bash", {"command": "cat log"}, f"line {i}\n[exit code 0]", False) for i in range(6)]
    assert kinds(run(changing)) == []


def test_the_same_failing_command_three_times_is_an_error_loop_and_not_also_a_repeat():
    fail = [step("bash", {"command": "pytest tests/test_a.py"}, pytest_out(d), False)
            for d in ("0.1", "0.2", "0.3", "0.4", "0.5")]
    assert all(s.error for s in fail)                    # 退出码非 0：工具本身没报错也算出错
    hits = run(fail)
    assert kinds(hits) == [(2, "error", 3)]
    assert "pytest tests/test_a.py" in hits[2].detail


def test_edits_between_failing_runs_are_not_an_error_loop():
    fail = step("bash", {"command": "pytest tests/test_a.py"}, pytest_out("0.1"), False)
    edits = [step("edit_file", {"file_path": "a.py", "old_string": f"v{i}", "new_string": f"v{i + 1}"}, "edited",
                  False) for i in range(3)]
    assert kinds(run([fail, edits[0], fail, edits[1], fail, edits[2], fail])) == []
    # 同一处改动反复写回、测试结果一样：这是来回打转
    assert kinds(run([fail, EDIT] * 3)) == [(5, "alternate", 6)]


def test_tool_errors_and_timeouts_count_as_errors():
    t = step("bash", {"command": "make build"}, "partial\n[Command timed out after 600s]", False)
    assert t.error
    e = step("read_file", {"file_path": "missing.py"}, "File not found", True)
    assert kinds(run([e, e, e])) == [(2, "error", 3)]


def test_two_actions_alternating_with_the_same_results():
    hits = run([READ_A, READ_B] * 5)
    assert kinds(hits) == [(ALTERNATE - 1, "alternate", ALTERNATE)]
    assert "a.py" in hits[ALTERNATE - 1].detail and "b.py" in hits[ALTERNATE - 1].detail
    # 前面有别的调用不影响；结果变了就不是来回交替
    assert kinds(run([EDIT] + [READ_A, READ_B] * 3)) == [(ALTERNATE, "alternate", ALTERNATE)]
    b2 = step("read_file", {"file_path": "b.py"}, "1\tdef g(): return 1", False)
    assert kinds(run([READ_A, READ_B, READ_A, b2, READ_A, READ_B])) == []


def test_normal_work_is_quiet():
    work = [READ_A, READ_B, EDIT,
            step("bash", {"command": "pytest -q"}, pytest_out("0.2"), False),
            step("edit_file", {"file_path": "a.py", "old_string": "y", "new_string": "z"}, "edited", False),
            step("bash", {"command": "pytest -q"}, "1 passed in 0.1s\n\n[exit code 0]", False),
            step("todo_write", {"todos": [{"content": "fix", "status": "completed"}]}, "ok", False),
            step("submit", {"summary": "done"}, "accepted", False)]
    assert kinds(run(work * 2)) == []


def test_thresholds():
    assert (REPEAT, ERROR_REPEAT, ALTERNATE) == (4, 3, 6) and loops.KEEP >= ALTERNATE
