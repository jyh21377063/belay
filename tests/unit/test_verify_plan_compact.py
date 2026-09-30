"""纯规则：基线归类、相关测试选择、回归判定、规划校验、压缩（L0/L1/L2）。"""
from __future__ import annotations

import copy

from belay.core.compact import CLEARED_PREFIX, count_results, l0_shrink, l1_clear, l2_rebuild, safe_cut
from belay.core.plan import mechanical_plan, renumber, uncovered_units, validate_plan, validate_split
from belay.core.verify import (classify_baseline, failure_signature, guard_set, is_test_path, regressions,
                               related_units)
from belay.runtime.planner import extract_json

# ======================================================================== verify

def test_classify_baseline():
    r1 = {"a": "PASSED", "b": "FAILED", "c": "PASSED", "d": "SKIPPED", "e": "ERROR", "f": "PASSED"}
    r2 = {"a": "PASSED", "b": "FAILED", "c": "FAILED", "d": "SKIPPED", "e": "FAILED"}
    cls = classify_baseline(r1, r2)
    assert cls == {"a": "pass", "b": "fail", "c": "flaky", "d": "skip", "e": "fail", "f": "flaky"}
    assert guard_set(cls) == {"a"}


def test_regressions_count_missing_and_skipped():
    regs = regressions(["t1", "t2", "t3", "t4"], {"t1": "PASSED", "t2": "FAILED", "t3": "SKIPPED"})
    assert regs == ("t2 (FAILED)", "t3 (SKIPPED)", "t4 (MISSING)")
    assert failure_signature(regs) == failure_signature(("t4 (MISSING)", "t2 (ERROR)", "t3 (FAILED)"))


def test_related_units():
    tests = ["pkg/tests/test_groupby.py", "pkg/tests/test_core.py", "tests/io/test_csv.py", "tests/test_misc.py"]
    assert related_units(["pkg/groupby.py"], tests)[0] == ("pkg/tests/test_groupby.py",)
    assert related_units(["pkg/io/csv.py"], tests)[0] == ("tests/io/test_csv.py",)
    # 通用名（core）不按名字匹配，退到同目录
    assert set(related_units(["pkg/core.py"], tests)[0]) == {"pkg/tests/test_groupby.py", "pkg/tests/test_core.py"}
    assert related_units(["setup.py"], tests)[0] is None                 # 全局文件 → 全量
    assert related_units(["pkg/ext.c"], tests)[0] is None                # 非 Python 源文件 → 全量
    assert related_units(["other/zzz.py"], tests)[0] is None             # 找不到相关测试 → 全量
    assert related_units(["README.md", "pkg/tests/test_core.py"], tests)[0] == ()   # 文档与测试改动不选
    assert is_test_path("a/tests/x.py") and is_test_path("test_a.py") and not is_test_path("pkg/testing_utils.py")


# ======================================================================== plan

TASK = """# Release notes

- Fix add so that add(1, 2) returns 3.
- Add a sub function that returns a minus b.

## Other

Deprecate the old mul_legacy helper and emit a warning."""


def _plan(**over):
    p = {"requirements": [{"id": "A", "quote": "Fix add so that add(1, 2) returns 3.", "summary": "add"},
                          {"id": "B", "quote": "Add a sub function that returns a minus b.", "summary": "sub"},
                          {"id": "C", "quote": "Deprecate the old mul_legacy helper and emit a warning.",
                           "summary": "deprecate"}],
         "tasks": [{"id": "x", "title": "add", "links": ["A"], "checks": ["tests/t.py::test_add", "nope"]},
                   {"id": "y", "title": "sub+dep", "links": ["B", "C"], "blocked_by": ["x"]}]}
    p.update(over)
    return p


def test_validate_plan_ok_and_renumber():
    rep = validate_plan(TASK, _plan(), known_checks=["tests/t.py::test_add"])
    assert rep.ok, rep.problems
    assert any("nope" in w for w in rep.warnings)
    reqs, tasks = renumber(rep)
    assert [r["id"] for r in reqs] == ["R1", "R2", "R3"]
    assert tasks[0]["id"] == "T1" and tasks[0]["checks"] == ["tests/t.py::test_add"]
    assert tasks[1]["blocked_by"] == ["T1"] and tasks[1]["links"] == ["R2", "R3"]


def test_validate_plan_rejects_non_verbatim_uncovered_unlinked_cycles():
    p = _plan()
    p["requirements"][0]["quote"] = "Fix add so it returns the sum."             # 不是原文
    rep = validate_plan(TASK, p)
    assert not rep.ok and any("not verbatim" in x for x in rep.problems)
    assert any("not covered" in x and "Fix add" in x for x in rep.problems)
    p = _plan()
    p["tasks"][1]["links"] = ["B"]                                                # C 没有任务链接
    assert any("C is not linked" in x for x in validate_plan(TASK, p).problems)
    p = _plan()
    p["tasks"][0]["blocked_by"] = ["y"]
    assert any("cycle" in x for x in validate_plan(TASK, p).problems)
    # 空白差异不影响“逐字”
    p = _plan()
    p["requirements"][1]["quote"] = "Add a sub   function that\nreturns a minus b."
    assert validate_plan(TASK, p).ok


def test_uncovered_units_skip_headings_and_mechanical_plan_covers():
    assert uncovered_units(TASK, []) == ["- Fix add so that add(1, 2) returns 3.",
                                        "- Add a sub function that returns a minus b.",
                                        "Deprecate the old mul_legacy helper and emit a warning."]
    mp = mechanical_plan(TASK)
    assert validate_plan(TASK, mp).ok and len(mp["requirements"]) == 3


def test_validate_split():
    kids, problems = validate_split(["R1", "R2"], [{"title": "a", "links": ["R1"]}, {"title": "b", "links": ["R9"]}],
                                    ["R1", "R2"])
    assert problems and "R2" in problems[0]
    kids, problems = validate_split(["R1", "R2"], [{"title": "a", "links": ["R1"]}, {"title": "b", "links": ["R2"]}],
                                    ["R1", "R2"])
    assert not problems and len(kids) == 2


def test_extract_json():
    assert extract_json('noise ```json\n{"a": 1}\n``` tail') == {"a": 1}
    assert extract_json('prefix {"b": [1, 2]} suffix') == {"b": [1, 2]}
    assert extract_json("no json here") is None


# ======================================================================== compact

def _conv(n: int, big: int = 10) -> list[dict]:
    msgs = [{"role": "user", "content": "opening"}]
    for i in range(n):
        msgs.append({"role": "assistant", "content": [{"type": "thinking", "thinking": "hm", "signature": "s"},
                                                      {"type": "tool_use", "id": f"u{i}", "name": "bash",
                                                       "input": {"command": f"pytest -k t{i}"}}]})
        msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"u{i}",
                                                  "content": ("x" * big) + f"\n\n[exit code {i % 2}]"}]})
    return msgs


def _pairing_ok(msgs: list[dict]) -> bool:
    for i, m in enumerate(msgs):
        if m["role"] == "user" and isinstance(m["content"], list):
            ids = {b["tool_use_id"] for b in m["content"] if b.get("type") == "tool_result"}
            if ids:
                prev = msgs[i - 1]
                if prev["role"] != "assistant":
                    return False
                uses = {b["id"] for b in prev["content"] if b.get("type") == "tool_use"}
                if ids != uses:
                    return False
    return all(msgs[i]["role"] != msgs[i + 1]["role"] for i in range(len(msgs) - 1))


def test_l0_shrink_keeps_head_errors_tail_and_path():
    lines = [f"line {i}" for i in range(2000)]
    lines[1000] = "E   AssertionError: boom"
    out = l0_shrink("\n".join(lines), "/runs/blob1.txt", max_chars=5000, head=10, tail=10)
    assert "line 0" in out and "line 1999" in out and "AssertionError: boom" in out
    assert "/runs/blob1.txt" in out and len(out) < 6000
    assert l0_shrink("short", "/p", 100) == "short"


def test_l1_clear_keeps_recent_and_does_not_mutate():
    msgs = _conv(20)
    orig = copy.deepcopy(msgs)
    new, n = l1_clear(msgs, keep_recent=5, meta={"u0": {"path": "/blobs/u0.txt"}})
    assert msgs == orig                                   # 纯函数
    assert n == 15 and count_results(new) == 5
    first = new[2]["content"][0]["content"]
    assert first.startswith(CLEARED_PREFIX) and "pytest -k t0" in first and "/blobs/u0.txt" in first
    assert "exit 1" in new[4]["content"][0]["content"]    # u1 的退出码
    assert _pairing_ok(new)
    assert l1_clear(new, keep_recent=5) == (new, 0)


def test_l2_rebuild_cuts_at_assistant_and_keeps_pairs():
    msgs = _conv(30, big=400)
    cut = safe_cut(msgs, keep_tokens=500)
    assert msgs[cut]["role"] == "assistant"
    new = l2_rebuild("REBUILT OPENING", msgs, keep_tokens=500, reread="### f.py")
    assert new[0]["role"] == "user" and new[0]["content"].startswith("REBUILT OPENING") and "### f.py" in new[0]["content"]
    assert _pairing_ok(new) and len(new) < len(msgs)
    # 预算太小也至少保留最后一对
    tiny = l2_rebuild("O", msgs, keep_tokens=1)
    assert len(tiny) == 3 and tiny[1]["role"] == "assistant" and _pairing_ok(tiny)
