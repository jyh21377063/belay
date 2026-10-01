"""纯规则：基线归类、相关测试选择、回归判定、规划校验、压缩（L0/L1/L2）。"""
from __future__ import annotations

import copy

from belay.core.compact import CLEARED_PREFIX, count_results, l0_shrink, l1_clear, l2_rebuild, safe_cut
from belay.core.plan import mechanical_plan, renumber, uncovered_units, validate_plan
from belay.core.verify import (classify_baseline, failure_signature, guard_set, is_test_path, regressions,
                               related_units, suite_layout_of)
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

The code is at /testbed and is version 1.0.
- Fix add so that add(1, 2) returns 3.
- Add a sub function that returns a minus b.

## Other

Deprecate the old mul_legacy helper and emit a warning."""


def _plan(**over):
    p = {"requirements": [{"id": "Z", "kind": "context", "quote": "The code is at /testbed and is version 1.0.",
                           "summary": "where the code is"},
                          {"id": "A", "quote": "Fix add so that add(1, 2) returns 3.", "summary": "add",
                           "checks": ["tests/t.py::test_add", "nope"]},
                          {"id": "B", "kind": "actionable", "quote": "Add a sub function that returns a minus b.",
                           "summary": "sub"},
                          {"id": "C", "quote": "Deprecate the old mul_legacy helper and emit a warning.",
                           "summary": "deprecate"}]}
    p.update(over)
    return p


def test_suite_layout_keeps_source_packages_named_like_test_dirs():
    """测试目录按基线实际收集到的测试判断：django/test/、numpy/testing/ 是源码，改动要交付。"""
    files = ["tests/admin_views/tests.py", "tests/test_sqlite.py", "numpy/testing/tests/test_utils.py",
             "pkg/test_core.py"]
    layout = suite_layout_of(files)
    assert layout.roots == ("numpy/testing/tests", "tests")
    for p in ("django/test/utils.py", "django/test/testcases.py", "numpy/testing/_private/utils.py", "pkg/core.py"):
        assert not is_test_path(p, layout), p
    for p in ("tests/admin_views/models.py", "tests/data/x.json", "numpy/testing/tests/data/a.txt",
              "pkg/conftest.py", "pkg/test_new.py", "other/helper_test.py", "pkg/test_core.py"):
        assert is_test_path(p, layout), p
    assert is_test_path("django/test/utils.py")                          # 没有布局（基线之前）：仍按名字
    assert suite_layout_of([]) is None and suite_layout_of(["cmd:build"]) is None
    assert related_units(["django/test/utils.py"], files)[0] is None     # 是源码：参与选择（找不到相关测试 → 全量）
    assert related_units(["tests/admin_views/models.py"], files)[0] == ()


def test_validate_plan_ok_and_renumber():
    rep = validate_plan(TASK, _plan(), known_checks=["tests/t.py::test_add"])
    assert rep.ok, rep.problems
    assert any("nope" in w for w in rep.warnings)
    reqs = renumber(rep)
    assert [r["id"] for r in reqs] == ["R1", "R2", "R3", "R4"]
    assert [r["kind"] for r in reqs] == ["context", "actionable", "actionable", "actionable"]
    assert reqs[1]["checks"] == ["tests/t.py::test_add"] and reqs[0]["checks"] == []


def test_validate_plan_rejects_non_verbatim_uncovered_bad_kind_and_all_context():
    p = _plan()
    p["requirements"][1]["quote"] = "Fix add so it returns the sum."             # 不是原文
    rep = validate_plan(TASK, p)
    assert not rep.ok and any("not verbatim" in x for x in rep.problems)
    assert any("not covered" in x and "Fix add" in x for x in rep.problems)
    p = _plan()
    p["requirements"][2]["kind"] = "background"
    assert any("kind must be" in x for x in validate_plan(TASK, p).problems)
    p = _plan()
    for r in p["requirements"]:
        r["kind"] = "context"
    assert any("no requirement is actionable" in x for x in validate_plan(TASK, p).problems)
    # 空白差异不影响“逐字”；旧格式里的 tasks 被忽略
    p = _plan(tasks=[{"id": "x"}])
    p["requirements"][2]["quote"] = "Add a sub   function that\nreturns a minus b."
    assert validate_plan(TASK, p).ok


def test_uncovered_units_skip_headings_and_mechanical_plan_covers():
    assert uncovered_units(TASK, []) == ["The code is at /testbed and is version 1.0.",
                                        "- Fix add so that add(1, 2) returns 3.",
                                        "- Add a sub function that returns a minus b.",
                                        "Deprecate the old mul_legacy helper and emit a warning."]
    mp = mechanical_plan(TASK)
    rep = validate_plan(TASK, mp)
    assert rep.ok and len(mp["requirements"]) == 4 and all(r["kind"] == "actionable" for r in rep.requirements)


def test_extract_json():
    assert extract_json('noise ```json\n{"a": 1}\n``` tail') == {"a": 1}
    assert extract_json('prefix {"b": [1, 2]} suffix') == {"b": [1, 2]}
    assert extract_json("no json here") is None
    # 说明文字里有别的花括号、字符串里有不合法的反斜杠转义：按键找、宽松解码
    text = 'The {field} default matters.\n{"requirements": [{"id": "R1", "evidence": ["re \\d+ in C:\\x"]}]} ok'
    assert extract_json(text, "requirements") == {"requirements": [{"id": "R1", "evidence": ["re \\d+ in C:\\x"]}]}
    assert extract_json('{"a": 1} then {"requirements": []}', "requirements") == {"requirements": []}
    assert extract_json('{"a": 1}', "requirements") is None
    assert extract_json('{"requirements": [{"id": "R1"', "requirements") is None      # 截断：拿不到
    assert extract_json('{"ok": "a\\\\b"}') == {"ok": "a\\b"}                     # 合法的转义不动


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
