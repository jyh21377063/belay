"""外壳里的纯文本 / 纯计算辅助函数，以及开场上下文里“离开期间”一段的上限。"""
from __future__ import annotations

from belay.core import rules as R
from belay.core.compact import CLEARED_PREFIX, l1_clear
from belay.core.config import BelayConfig
from belay.core.context import build_context
from belay.env import LocalEnv
from belay.runtime.session import ModelCallFailed, load_transcript_messages
from belay.runtime.verifier import RunnerVerifier, VerifierSpec, extract_failure
from tests.sim import Sim

LOG = """============================= test session starts ==============================
collected 3 items

tests/test_mod.py .F                                                     [100%]

=================================== FAILURES ===================================
__________________________________ test_mul ___________________________________

    def test_mul():
>       assert mul(2, 3) == 6
E       assert 5 == 6

tests/test_mod.py:9: AssertionError
______________________________ TestK.test_other _______________________________

    def test_other(self):
>       assert False
E       assert False
=========================== short test summary info ============================
FAILED tests/test_mod.py::test_mul - assert 5 == 6
"""


def test_extract_failure_finds_the_traceback_of_one_test():
    seg = extract_failure(LOG, "tests/test_mod.py::test_mul")
    assert "assert 5 == 6" in seg and "test_other" not in seg
    seg2 = extract_failure(LOG, "tests/test_mod.py::TestK::test_other")
    assert "assert False" in seg2 and "short test summary" not in seg2


def test_map_sys_path_keeps_workspace_entries_in_order():
    v = RunnerVerifier(LocalEnv("/ws"), VerifierSpec(test_cmd="pytest"), "/ws", "/g", "/j")
    rel = v.map_sys_path(["", "/usr/lib/python3", "/ws/src", "/ws", "/ws/vendor/lib", "/other"])
    assert rel == ["src", ".", "vendor/lib"]
    v.pythonpath_rel = rel
    assert v.slot_pythonpath(v.slots[0])[:2] == [v.slots[0].dir + "/src", v.slots[0].dir]


def test_hard_coded_workspace_paths_are_detected():
    spec = VerifierSpec(test_cmd="pytest /testbed/tests", prelude="source /opt/env", commands=["cd /testbed2"])
    assert spec.mentions("/testbed") == ["test_cmd"]
    assert VerifierSpec(test_cmd="pytest tests").mentions("/testbed") == []


def test_l1_keeps_read_file_results_on_the_first_pass():
    msgs = [{"role": "user", "content": "start"}]
    for i, name in enumerate(["read_file", "bash", "read_file", "bash", "bash"]):
        msgs.append({"role": "assistant", "content": [{"type": "tool_use", "id": f"u{i}", "name": name,
                                                        "input": {"file_path": "a.py", "command": "pytest"}}]})
        msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"u{i}",
                                                   "content": f"output {i} " * 50}]})
    new, n = l1_clear(msgs, keep_recent=1, keep_reads=True)
    cleared = [b["tool_use_id"] for m in new if m["role"] == "user" and isinstance(m["content"], list)
               for b in m["content"] if str(b["content"]).startswith(CLEARED_PREFIX)]
    assert n == 2 and cleared == ["u1", "u3"]
    new2, n2 = l1_clear(new, keep_recent=1)
    assert n2 == 2                                                      # 第二遍连 read_file 一起清


def test_model_failure_classification():
    e = RuntimeError("prompt is too long")
    assert ModelCallFailed(e).context_problem
    assert not ModelCallFailed(ConnectionError("reset by peer")).context_problem


def test_load_transcript_messages_adds_interrupted_results(tmp_path):
    import json
    blob = tmp_path / "m.json"
    blob.write_text(json.dumps([{"role": "user", "content": "hi"}]))
    tr = tmp_path / "t.jsonl"
    recs = [{"type": "messages_checkpoint", "path": str(blob)},
            {"type": "message", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "x", "name": "bash", "input": {"command": "make"}}]}}]
    tr.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    msgs = load_transcript_messages(str(tr), lambda p: open(p).read())
    assert msgs[-1]["content"][0]["tool_use_id"] == "x" and "interrupted" in msgs[-1]["content"][0]["content"]
    recs.append({"type": "message", "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]}})
    tr.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    assert load_transcript_messages(str(tr), lambda p: open(p).read()) is None     # 会话已经结束


def test_away_section_keeps_the_top_events_and_counts_the_rest():
    base = {"tests/test_a.py::test_a": "PASSED"}
    task = "Implement feature one in the core module now."
    plan = {"requirements": [{"id": "r", "quote": task, "summary": "one"}]}
    cfg = BelayConfig(away_top=3, confirm_regressions=False)
    s = Sim(base, cfg=cfg)
    s.setup(task, plan)
    s.do(R.start_session, "w1", "first", {})
    s.do(R.end_session, "w1", "handoff")
    mark = s.g.seq
    for i in range(8):
        s.world.define(f"x{i}", {})
        s.snap(f"x{i}", files=[("pkg/a.py", 1, 1)], reason="session_end")
    away = [e for e in s.log if e.seq > mark]
    ctx = build_context(s.g, "w1", 50_000, s.now, cfg, mode="resume", away=away,
                        blobs={"away_files": "  pkg/a.py (+1 -1)"})
    sec = ctx.text.split("## While you were away")[1].split("## ")[0]
    assert sec.count("\n- ") <= 4 and "and " in sec and "more" in sec
    assert "The working tree changed since your previous session" in sec


def test_reply_json_reads_the_text_then_the_thinking_block():
    from belay.llm import Response
    from belay.runtime.driver import _reply_json
    body = '{"requirements": [{"id": "R1", "implemented": "yes"}]}'
    r = Response([{"type": "thinking", "thinking": "the {x} part"}, {"type": "text", "text": "Here: " + body}], "end_turn")
    assert _reply_json(r, "requirements")["requirements"][0]["id"] == "R1"
    r = Response([{"type": "thinking", "thinking": "answer: " + body}, {"type": "text", "text": ""}], "end_turn")
    assert _reply_json(r, "requirements")["requirements"][0]["implemented"] == "yes"
    r = Response([{"type": "text", "text": '{"requirements": [{"id": "R1", "impl'}], "max_tokens")
    assert _reply_json(r, "requirements") is None


# ---------------------------------------------------------------- 跑通过的快照（driver 的判定）

def _bash(cmd: str, **kw) -> dict:
    return {"name": "bash", "input": {"command": cmd, **kw}}


def _edit() -> dict:
    return {"name": "edit_file", "input": {"file_path": "a.py"}}


def test_driver_marks_only_passing_test_or_run_commands_after_the_last_edit():
    from types import SimpleNamespace
    from belay.runtime.driver import _Hooks

    def passed(batch, cfg=None) -> bool:
        hooks = _Hooks(SimpleNamespace(cfg=cfg or BelayConfig()), None)
        return hooks._passed_run([tu for tu, _r in batch], [r for _tu, r in batch])

    ok = ("1 passed", False)
    assert passed([(_edit(), ("ok", False)), (_bash("python -m pytest -q"), ok)])
    assert passed([(_bash("cd /app && simulate score"), ("score 0.98", False))])
    assert not passed([(_bash("cat pytest.ini"), ok)])                          # 只读
    assert not passed([(_bash("pip install pytest"), ok)])                      # 安装
    assert not passed([(_bash("pytest -x"), ("1 failed\n\n[exit code 1]", False))])
    assert not passed([(_bash("pytest -x"), ("[Command timed out after 120s and was killed.]", False))])
    assert not passed([(_bash("pytest -x", run_in_background=True), ("Started in background", False))])
    assert not passed([(_bash("pytest -x"), ok), (_edit(), ("ok", False))])    # 跑完又改了
    assert not passed([(_bash("simulate score"), ok)], BelayConfig(merge_stable_generic=False))
    assert passed([(_bash("pytest"), ok)], BelayConfig(merge_stable_generic=False))
    assert not passed([(_bash("pytest"), ok)], BelayConfig(background="handoff"))
