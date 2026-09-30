"""验证相关的纯规则：基线归类、守护集合、相关测试的选择、回归判定、按树合并作业结果。

检查 id 两种：pytest 的 node id（`tests/test_x.py::test_y`）与公开检查 `cmd:<name>`。
作业的 selection 是测试文件或 `cmd:<name>` 的元组；None 表示全量。
"""
from __future__ import annotations

import hashlib
import json
import posixpath
import re
from typing import Iterable, Optional

from belay.core.model import JOB_FINISHED, JOB_RUNNING, Graph

# 与 belay/container/runner.py 的 TEST_PATH 保持一致（tests/unit/test_verify.py 检查二者相同）
TEST_PATH = re.compile(r"(^|/)(tests?|testing|__tests__)/|(^|/)test_[^/]*\.py$|_test\.py$|(^|/)conftest\.py$")

PASSED, FAILED, ERROR, SKIPPED, XFAIL, MISSING = "PASSED", "FAILED", "ERROR", "SKIPPED", "XFAIL", "MISSING"
# 基线归类
B_PASS, B_FAIL, B_FLAKY, B_SKIP = "pass", "fail", "flaky", "skip"

# 这些文件的改动可能影响任何测试：直接升级为全量
_GLOBAL_FILES = {"conftest.py", "setup.py", "setup.cfg", "pyproject.toml", "tox.ini", "pytest.ini", "__init__.py",
                 "requirements.txt", "Makefile", "CMakeLists.txt", "Cargo.toml", "package.json"}
_DOC_EXT = (".md", ".rst", ".txt", ".adoc")
_GENERIC_STEMS = {"__init__", "utils", "util", "core", "base", "common", "helpers", "compat", "types", "main"}


def is_test_path(path: str) -> bool:
    return bool(TEST_PATH.search(path))


def is_cmd(check: str) -> bool:
    return check.startswith("cmd:")


def check_unit(check: str) -> str:
    """检查所在的选择单元：测试文件，或 cmd:<name> 本身。"""
    return check if is_cmd(check) else check.split("::", 1)[0]


def units(checks: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({check_unit(c) for c in checks}))


def job_key(tree: str, selection: Optional[tuple[str, ...]], tag: str = "") -> str:
    body = json.dumps([tree, None if selection is None else sorted(selection), tag])
    return hashlib.sha1(body.encode()).hexdigest()[:16]


def covers(selection: Optional[tuple[str, ...]], needed: Iterable[str]) -> bool:
    if selection is None:
        return True
    have = set(selection)
    return all(u in have for u in needed)


# ---------------------------------------------------------------- 基线与守护集合

def classify_baseline(run1: dict[str, str], run2: dict[str, str]) -> dict[str, str]:
    """原始代码上两次运行：两次都通过 → pass；两次都失败 → fail；不一致 → flaky；跳过 / xfail → skip。"""
    out = {}
    for tid in sorted(set(run1) | set(run2)):
        a, b = run1.get(tid, MISSING), run2.get(tid, MISSING)
        if a == PASSED and b == PASSED:
            out[tid] = B_PASS
        elif a in (SKIPPED, XFAIL) and b in (SKIPPED, XFAIL):
            out[tid] = B_SKIP
        elif a == b or {a, b} <= {FAILED, ERROR}:
            out[tid] = B_FAIL
        else:
            out[tid] = B_FLAKY
    return out


def guard_set(baseline: dict[str, str]) -> frozenset[str]:
    return frozenset(t for t, c in baseline.items() if c == B_PASS)


def guard_in_selection(guard: Iterable[str], selection: Optional[tuple[str, ...]]) -> list[str]:
    if selection is None:
        return sorted(guard)
    sel = set(selection)
    return sorted(t for t in guard if check_unit(t) in sel)


def regressions(expected: Iterable[str], results: dict[str, str]) -> tuple[str, ...]:
    """守护测试在候选上失败、出错、被跳过、漏跑都算回归。返回 'id (STATUS)' 形式便于阅读和签名。"""
    out = []
    for tid in sorted(expected):
        st = results.get(tid, MISSING)
        if st != PASSED:
            out.append(f"{tid} ({st})")
    return tuple(out)


def regression_ids(regs: Iterable[str]) -> list[str]:
    return [r.rsplit(" (", 1)[0] for r in regs]


def failure_signature(regs: Iterable[str]) -> str:
    return hashlib.sha1("\n".join(sorted(regression_ids(regs))).encode()).hexdigest()[:12]


# ---------------------------------------------------------------- 相关测试的选择（宁多勿少）

def _stem(path: str) -> str:
    base = posixpath.basename(path)
    return base[:-3] if base.endswith(".py") else base


def _tokens(test_file: str) -> set[str]:
    stem = _stem(test_file)
    if stem.startswith("test_"):
        stem = stem[5:]
    if stem.endswith("_test"):
        stem = stem[:-5]
    toks = {stem} | set(stem.split("_"))
    return {t for t in toks if t}


def related_units(changed: Iterable[str], test_files: Iterable[str]) -> tuple[Optional[tuple[str, ...]], str]:
    """按文件路径的通用规则选相关测试文件。

    返回 (选择, 理由)：选择为 None 表示应当跑全量。规则：
      - 文档类文件（.md/.rst/.txt）不影响测试；测试路径下的改动会被剔除，也不参与选择；
      - 非 Python 源文件、全局配置（conftest、setup、pyproject、__init__ 等）→ 全量；
      - Python 源文件：测试文件名的词里含有源文件名 → 相关；否则同目录 / 镜像目录下的测试 → 相关；
      - 任何一个源文件找不到相关测试 → 全量。
    """
    tests = sorted(set(test_files))
    chosen: set[str] = set()
    for path in changed:
        if is_test_path(path) or path.endswith(_DOC_EXT):
            continue
        base = posixpath.basename(path)
        if base in _GLOBAL_FILES or not path.endswith(".py"):
            return None, f"{path} may affect any test"
        stem = _stem(path)
        hits = [] if stem in _GENERIC_STEMS else [t for t in tests if stem in _tokens(t)]
        if not hits:
            d = posixpath.dirname(path)
            name = posixpath.basename(d)
            hits = [t for t in tests if d and (t.startswith(d + "/") or f"/{name}/" in f"/{t}")]
        if not hits:
            return None, f"no test file is related to {path}"
        chosen.update(hits)
    return tuple(sorted(chosen)), f"{len(chosen)} related test file(s)"


def test_files_of(baseline: dict[str, str]) -> list[str]:
    return sorted({check_unit(t) for t in baseline if not is_cmd(t)})


# ---------------------------------------------------------------- 按树合并作业结果

def results_for_tree(g: Graph, tree: str, include_live: bool = False) -> dict[str, str]:
    """一棵树上所有已完成作业的结果合并（按作业开始的顺序，后面的覆盖前面的：确认重跑的结果生效）。"""
    out: dict[str, str] = {}
    for job in _jobs_on(g, tree, include_live):
        if job.state == JOB_FINISHED:
            out.update(job.results)
    return out


def results_of_jobs(g: Graph, job_ids: Iterable[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for jid in job_ids:
        job = g.jobs.get(jid)
        if job is not None and job.state == JOB_FINISHED:
            out.update(job.results)
    return out


def _jobs_on(g: Graph, tree: str, include_live: bool):
    return sorted((j for j in g.jobs.values() if j.tree == tree and (include_live or not j.live)),
                  key=lambda j: _num(j.id))


def _num(ident: str) -> int:
    digits = "".join(ch for ch in ident if ch.isdigit())
    return int(digits) if digits else 0


def finished_covers(g: Graph, tree: str, needed: Iterable[str]) -> bool:
    """这棵树上已经有完成的（非 live）作业覆盖了这些选择单元。"""
    needed = list(needed)
    return any(j.state == JOB_FINISHED and covers(j.selection, needed) for j in _jobs_on(g, tree, False))


def running_covers(g: Graph, tree: str, needed: Iterable[str]) -> bool:
    needed = list(needed)
    return any(j.state == JOB_RUNNING and covers(j.selection, needed) for j in _jobs_on(g, tree, False))


def full_verified(g: Graph, tree: str) -> bool:
    return any(j.state == JOB_FINISHED and j.selection is None for j in _jobs_on(g, tree, False))


def tree_regressions(g: Graph, tree: str) -> tuple[str, ...]:
    """全量结果上的回归（需要这棵树已有全量作业）。"""
    return regressions(guard_set(g.baseline), results_for_tree(g, tree))


def head_full_ok(g: Graph) -> bool:
    """链头这棵树有全量结果且没有回归；没有任何可用检查时视为通过（账本里会注明未验证）。"""
    cp = g.head_cp
    if cp is None:
        return False
    if cp.id == 0 or not guard_set(g.baseline):
        return True
    return full_verified(g, cp.tree) and not tree_regressions(g, cp.tree)
