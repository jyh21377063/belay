"""初始图的构建（纯函数）：需求、基线检查、根工作节点、Run。"""
from __future__ import annotations

from belay.graph.evidence import is_test_path
from belay.graph.model import RUNNING, Check, GraphState, Requirement, Run, Work


def initial_state(*, requirements: list[Requirement], baseline: dict[str, str], now: float, deadline_t: float,
                  reserve_sec: float, full_gate_sec: float, base_commit: str, base_tree: str, workspace: str,
                  gate_available: bool, test_author_available: bool, protect_tests: bool,
                  test_files: list[str], notes: list[str] | None = None) -> GraphState:
    run = Run(started_t=now, deadline_t=deadline_t, reserve_sec=reserve_sec, full_gate_sec=full_gate_sec,
              base_commit=base_commit, base_tree=base_tree, head_commit=base_commit, head_tree=base_tree,
              workspace=workspace, gate_available=gate_available, test_author_available=test_author_available,
              protect_tests=protect_tests, test_files=[t for t in test_files if is_test_path(t) or t.endswith(".py")],
              req_ids=[r.id for r in requirements], notes=list(notes or []))
    state = GraphState(run=run)
    for r in requirements:
        state.requirement[r.id] = r
    for i, (test, status) in enumerate(sorted(baseline.items()), 1):
        state.check[f"B{i}"] = Check(id=f"B{i}", source="existing_test", selector=test, baseline=status)
    state.work["W1"] = Work(id="W1", covers=[r.id for r in requirements], state=RUNNING, workspace=workspace,
                            base_commit=base_commit)
    run.counters = {"W": 1, "B": len(baseline)}
    return state
