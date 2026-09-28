"""证据规则（纯函数）：基线、结果归类、相关测试、独立测试的收录。"""
from __future__ import annotations

from belay.graph.evidence import (baseline_status, classify, is_test_path, judge_fail_before, normalize_error,
                                  related_test_files, signature_id)
from belay.graph.model import FAIL, FLAKY, NONE, PASS


def test_baseline_status_from_two_runs():
    runs = [{"a": "PASSED", "b": "FAILED", "c": "PASSED", "d": "SKIPPED", "e": "XFAIL"},
            {"a": "PASSED", "b": "FAILED", "c": "FAILED", "d": "SKIPPED", "e": "XFAIL"}]
    assert baseline_status(runs) == {"a": PASS, "b": FAIL, "c": FLAKY, "d": NONE, "e": PASS}


def test_classify_against_baseline():
    base = {"t.py::ok": PASS, "t.py::broken": PASS, "t.py::old": FAIL, "t.py::fl": FLAKY, "t.py::gone": PASS,
            "u.py::other": PASS, "t.py::waived": PASS}
    res = {"t.py::ok": "PASSED", "t.py::broken": "FAILED", "t.py::old": "FAILED", "t.py::fl": "FAILED",
           "t.py::new": "FAILED", "t.py::waived": "FAILED"}
    c = classify(base, res, ["t.py"], waived={"t.py::waived"})
    assert c.regressions == ["t.py::broken", "t.py::gone"]      # gone：所选文件里基线通过、这次没跑出来
    assert c.waived == ["t.py::waived"]
    assert c.known == ["t.py::old"] and c.flaky == ["t.py::fl"] and c.new_failed == ["t.py::new"]
    assert "u.py::other" not in c.regressions                  # 没选的文件不算
    full = classify(base, res, None)
    assert "u.py::other" in full.regressions                   # 全量运行时都算


def test_skipping_a_test_that_passed_on_the_original_code_is_a_regression():
    base = {"t.py::a": PASS, "t.py::b": NONE}
    c = classify(base, {"t.py::a": "SKIPPED", "t.py::b": "SKIPPED"}, ["t.py"])
    assert c.regressions == ["t.py::a"]                        # b 在原始代码上就被跳过：不算


def test_classify_node_selection_only_checks_selected_nodes():
    base = {"t.py::a": PASS, "t.py::b": PASS}
    c = classify(base, {"t.py::a": "PASSED"}, ["t.py::a"])
    assert c.regressions == []


def test_related_test_files():
    tests = ["conans/test/integration/cache/test_cache_clean.py", "conans/test/unittests/client/test_api.py",
             "dask/array/tests/test_core.py", "tests/test_widget.py", "tests/test_other.py"]
    assert related_test_files(["conans/client/cache/cache_clean.py"], tests) == [
        "conans/test/integration/cache/test_cache_clean.py"]
    assert related_test_files(["dask/array/core.py"], tests) == ["dask/array/tests/test_core.py"]  # 同包 tests/
    assert related_test_files(["pkg/widget.py"], tests) == ["tests/test_widget.py"]
    assert related_test_files(["pkg/utils.py"], tests) == []    # 通用名不做分词匹配
    assert related_test_files(["tests/conftest.py"], tests) == ["tests/test_other.py", "tests/test_widget.py"]


def test_test_paths():
    assert is_test_path("tests/test_x.py") and is_test_path("pkg/tests/data.json") and is_test_path("conftest.py")
    assert is_test_path("a/b_test.py") and not is_test_path("pkg/testing_utils.py") and not is_test_path("pkg/a.py")


def test_fail_before_rules():
    f = ".belay_checks/test_belay_t1.py"
    ok = judge_fail_before({f"{f}::test_a": "FAILED", f"{f}::test_b": "PASSED", f"{f}::test_c": "FAILED"},
                           {f"{f}::test_a": "assert 1 == 2", f"{f}::test_c": "AttributeError: no attribute 'x'"}, f)
    assert ok.verdict == "accepted" and ok.nodes == [f"{f}::test_a"]
    assert judge_fail_before({f"{f}::t": "FAILED"}, {f"{f}::t": "ImportError: cannot import name"}, f).verdict == \
        "not_assertion"
    assert judge_fail_before({f"{f}::t": "PASSED"}, {}, f).verdict == "passes_on_original"
    assert judge_fail_before({}, {}, f).verdict == "invalid"
    assert judge_fail_before({f"{f}::t": "ERROR"}, {f"{f}::t": "fixture 'db' not found"}, f).verdict == "invalid"
    assert judge_fail_before({f"{f}::t": "FAILED"}, {f"{f}::t": "Failed: DID NOT RAISE <class 'ValueError'>"},
                             f).verdict == "accepted"


def test_failure_signatures_are_normalized():
    a = normalize_error("AssertionError: expected 3 got 4 at 0x7f00aa /tmp/pytest-12/x")
    b = normalize_error("AssertionError: expected 5 got 6 at 0x7f11bb /tmp/pytest-99/y")
    assert a == b and a.startswith("AssertionError")
    assert signature_id("t", a) == signature_id("t", b) != signature_id("u", a)
