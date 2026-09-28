"""证据的计算规则（纯函数）：按基线归类测试结果、选相关测试、失败签名、独立测试的收录判定。

decide() 只通过这里和 ledger.py 解释作业结果，所以"什么算回归、什么算证据"都集中在这一个文件里，
可以直接单元测试。测试结果的格式来自容器内的 runner.py：{node_id: PASSED | FAILED | ERROR | SKIPPED | XFAIL}。
"""
from __future__ import annotations

import hashlib
import posixpath
import re
from dataclasses import dataclass, field

from belay.graph.model import FAIL, FLAKY, NONE, PASS

TEST_PATH = re.compile(r"(^|/)(tests?|testing|__tests__)/|(^|/)test_[^/]*\.py$|_test\.py$|(^|/)conftest\.py$")
OK = ("PASSED", "XFAIL")
FAILING = ("FAILED", "ERROR")
# 在原始代码上失败但不算"断言层面"的异常：导入失败、名字不存在（v4）
NOT_ASSERTION = re.compile(r"\b(ImportError|ModuleNotFoundError|AttributeError|NameError|SyntaxError|"
                           r"IndentationError|fixture '[^']+' not found)\b")
_GENERIC_STEMS = {"__init__", "utils", "util", "base", "core", "main", "common", "api", "helpers", "compat",
                  "types", "errors", "exceptions", "config", "conftest", "setup", "version", "_version"}


def is_test_path(path: str) -> bool:
    return bool(TEST_PATH.search(path))


def test_file(node_id: str) -> str:
    return node_id.split("::", 1)[0]


# ---- 基线 -------------------------------------------------------------------------

def baseline_status(runs: list[dict[str, str]]) -> dict[str, str]:
    """两次（或多次）原始代码上的运行 → 每个测试的基线：全部通过 PASS，全部失败 FAIL，不一致 FLAKY。
    都被跳过的测试记为 NONE（不参与判定）。"""
    names = set().union(*runs) if runs else set()
    out = {}
    for t in names:
        states = [r.get(t) for r in runs]
        oks = [s in OK for s in states]
        if all(oks):
            out[t] = PASS
        elif any(oks):
            out[t] = FLAKY
        elif all(s in FAILING for s in states):
            out[t] = FAIL
        elif any(s in FAILING for s in states) and any(s is None for s in states):
            out[t] = FLAKY
        else:
            out[t] = NONE
    return out


# ---- 结果归类 ---------------------------------------------------------------------

@dataclass
class Classified:
    total: int = 0
    passed: int = 0
    regressions: list[str] = field(default_factory=list)      # 基线通过、现在失败或没跑出来
    waived: list[str] = field(default_factory=list)           # 回归但已获批例外
    known: list[str] = field(default_factory=list)            # 基线就失败
    flaky: list[str] = field(default_factory=list)            # 基线不稳定，现在失败
    new_failed: list[str] = field(default_factory=list)       # 基线中没有的测试，现在失败
    new_passed: list[str] = field(default_factory=list)
    fixed: list[str] = field(default_factory=list)            # 基线失败、现在通过


def classify(baseline: dict[str, str], results: dict[str, str], selected_files: list[str] | None,
             waived: set[str] | None = None, ignore: set[str] | None = None) -> Classified:
    """把一次运行的结果与基线比较。

    selected_files 为 None 表示全量运行：基线通过、这次却没有结果的测试也算回归（通常是收集失败）。
    否则只对所选文件中的测试做这项检查。ignore 中的测试（如 authored 检查）不参与归类。
    """
    waived = waived or set()
    ignore = ignore or set()
    c = Classified()
    for t, s in results.items():
        if t in ignore:
            continue
        c.total += 1
        b = baseline.get(t)
        if s in OK:
            c.passed += 1
            if b is None:
                c.new_passed.append(t)
            elif b == FAIL:
                c.fixed.append(t)
            continue
        if s not in FAILING:
            continue                                   # SKIPPED 等
        if b == PASS:
            (c.waived if t in waived else c.regressions).append(t)
        elif b == FAIL:
            c.known.append(t)
        elif b == FLAKY:
            c.flaky.append(t)
        elif b is None:
            c.new_failed.append(t)
    files = None if selected_files is None else {x for x in selected_files if "::" not in x}
    nodes = set() if selected_files is None else {x for x in selected_files if "::" in x}
    for t, b in baseline.items():
        if b != PASS or t in results or t in ignore:
            continue
        if files is None or test_file(t) in files or t in nodes:
            (c.waived if t in waived else c.regressions).append(t)
    for lst in (c.regressions, c.waived, c.known, c.flaky, c.new_failed, c.new_passed, c.fixed):
        lst.sort()
    return c


# ---- 相关测试 ---------------------------------------------------------------------

def _tokens(path: str) -> set[str]:
    return {t for t in re.split(r"[/_.\-]+", path.lower()) if t}


def related_test_files(changed: list[str], test_files: list[str]) -> list[str]:
    """检查点门禁的子集：与改动文件相关的测试文件（粗粒度，宁多勿少）。

    规则：test_<stem>.py / <stem>_test.py；测试路径的分词里含有改动文件的主干名（非通用名、长度 ≥ 4）；
    或者测试文件位于改动文件所在包的 tests 子目录下；改动的是测试配置（conftest）时选其目录下的全部测试。
    """
    picked: set[str] = set()
    for p in changed:
        stem = posixpath.splitext(posixpath.basename(p))[0].lower()
        d = posixpath.dirname(p)
        for t in test_files:
            tb = posixpath.basename(t).lower()
            if posixpath.basename(p) == "conftest.py":
                if t.startswith(d + "/") or not d:
                    picked.add(t)
                continue
            if t == p:
                picked.add(t)
            elif tb in (f"test_{stem}.py", f"{stem}_test.py", f"test_{stem}s.py"):
                picked.add(t)
            elif stem not in _GENERIC_STEMS and len(stem) >= 4 and stem in _tokens(t):
                picked.add(t)
            elif d and (t.startswith(d + "/tests/") or t.startswith(d + "/test/")):
                picked.add(t)
    return sorted(picked)


# ---- 失败签名 ---------------------------------------------------------------------

_ERR_TYPE = re.compile(r"\b([A-Z][A-Za-z0-9_]*(?:Error|Exception|Exit|Interrupt|Warning)|Failed|assert)\b")


def normalize_error(reason: str) -> str:
    """失败原因归一化：异常类型 + 去掉数字、地址、临时路径后的消息前 120 个字符。"""
    r = (reason or "").strip()
    m = _ERR_TYPE.search(r)
    etype = m.group(1) if m else "failure"
    msg = re.sub(r"0x[0-9a-fA-F]+", "0x?", r)
    msg = re.sub(r"/tmp/[^\s'\"]+", "/tmp/?", msg)
    msg = re.sub(r"\d+", "N", msg)
    return f"{etype}: {msg[:120]}"


def signature_id(test: str, error: str) -> str:
    return "S" + hashlib.sha1(f"{test}|{error}".encode()).hexdigest()[:10]


def environment_like(reason: str) -> bool:
    return bool(re.search(r"(ConnectionError|ConnectionRefused|Name or service not known|Temporary failure in name "
                          r"resolution|Network is unreachable|PermissionError|Permission denied|No space left)",
                          reason or ""))


# ---- 独立测试的收录 ----------------------------------------------------------------

@dataclass
class FailBefore:
    verdict: str                    # accepted | invalid | passes_on_original | not_assertion
    nodes: list[str] = field(default_factory=list)
    detail: str = ""


def judge_fail_before(results: dict[str, str], reasons: dict[str, str], file: str) -> FailBefore:
    """Test Author 的测试在原始代码上跑出的结果 → 能否收录。

    收录：至少一个测试以断言层面的失败（不是导入失败、名字不存在）在原始代码上失败；
    只收录这些测试作为证据（同一文件里原本就通过的测试不能证明变化）。
    """
    mine = {t: s for t, s in results.items() if test_file(t) == file}
    if not mine:
        return FailBefore("invalid", detail="no test was collected from the file (syntax or collection error)")
    errors = [t for t, s in mine.items() if s == "ERROR"]
    failed = [t for t, s in mine.items() if s == "FAILED"]
    good = sorted(t for t in failed if not NOT_ASSERTION.search(reasons.get(t, "")))
    bad = sorted(t for t in failed if NOT_ASSERTION.search(reasons.get(t, "")))
    if good:
        detail = f"{len(good)} test(s) fail on the original code at the assertion level"
        if bad or errors:
            detail += f"; ignored {len(bad) + len(errors)} that fail with import/name/setup errors"
        return FailBefore("accepted", good, detail)
    if errors and not failed:
        return FailBefore("invalid", detail="tests error during setup: " +
                          "; ".join(f"{t}: {reasons.get(t, '')[:150]}" for t in errors[:3]))
    if bad:
        return FailBefore("not_assertion", detail="tests fail on the original code only with import/name errors, "
                          "not at the assertion level: " +
                          "; ".join(f"{t}: {reasons.get(t, '')[:150]}" for t in bad[:3]))
    return FailBefore("passes_on_original", detail="all tests pass on the original code, so they cannot show that "
                      "the requirement changed anything")


def authored_status(results: dict[str, str], nodes: list[str]) -> str:
    """authored 检查在某个树上的结论：收录的测试全部通过为 PASS，否则 FAIL。"""
    if not nodes:
        return FAIL
    return PASS if all(results.get(n) in OK for n in nodes) else FAIL


def interface_mismatch(reasons: dict[str, str], nodes: list[str]) -> bool:
    """候选上的失败是否是接口不一致（导入失败、名字不存在、调用签名不符），而不是行为错误。"""
    rs = [reasons.get(n, "") for n in nodes]
    return bool(rs) and all(NOT_ASSERTION.search(r) or re.search(r"TypeError: .*(argument|positional)", r)
                            for r in rs if r)
