"""五条不变量（v4）的断言。每次状态转换后由 Orchestrator 检查；单元测试里每一步也检查。

  1 需求冻结         需求集合初始化后不变；已收录的独立检查定义不变（按摘要校验）
  2 完成权           Work 只有在候选合并后才是 MERGED；运行终态 DONE 要求最后一次合并来自最终提交
  3 验收基准不可改    启用测试保护时，集成链上没有任何测试路径下的改动
  4 集成分支只经门禁推进  集成链连续；每个集成提交都来自一个 merged 候选；阻断模式下该候选没有未豁免的回归；
                     Run.head 等于链尾
  5 事实只追加且幂等  同一个去重键最多只有一个有效（排队、运行或完成）的作业；一个候选最多合并一次
"""
from __future__ import annotations

from belay.graph.evidence import is_test_path
from belay.graph.model import CANCELLED, ERROR, MERGED, RUN_DONE, TIMEOUT, GraphState, check_digest


def violations(state: GraphState, gate_mode: str = "block") -> list[str]:
    out: list[str] = []
    run = state.run
    # 1
    if sorted(state.requirement) != sorted(run.req_ids):
        out.append("requirements changed after freeze")
    for c in state.check.values():
        if c.source == "authored" and c.status == "active" and c.digest != check_digest(c):
            out.append(f"check {c.id} definition changed after it was accepted")
    # 2
    merged_cands = {c.id for c in state.candidate.values() if c.verdict == "merged"}
    for w in state.work.values():
        if w.state == MERGED and not any(c.work_id == w.id and c.verdict in ("merged", "unchanged") and c.final
                                          for c in state.candidate.values()):
            out.append(f"work {w.id} is MERGED without a merged final candidate")
    chain = state.integration_chain()
    if run.status == RUN_DONE and not any(c.final and c.verdict in ("merged", "unchanged")
                                          for c in state.candidate.values()):
        out.append("run is DONE without an accepted final submission")
    # 3
    if run.protect_tests:
        for i in chain:
            cand = state.candidate.get(i.candidate_id)
            bad = [p for p in (cand.changed if cand else []) if is_test_path(p)]
            if bad:
                out.append(f"integration #{i.seq} changes test paths: {bad[:3]}")
    # 4
    for n, i in enumerate(chain, 1):
        if i.seq != n:
            out.append(f"integration chain is not contiguous at #{i.seq}")
        if i.candidate_id not in merged_cands:
            out.append(f"integration #{i.seq} does not come from a merged candidate")
        cand = state.candidate.get(i.candidate_id)
        if gate_mode == "block" and cand and cand.regressions:
            out.append(f"integration #{i.seq} was merged with regressions {cand.regressions[:3]}")
    if chain and (run.head_commit != chain[-1].commit or run.head_tree != chain[-1].tree):
        out.append("run head does not match the end of the integration chain")
    if not chain and run.head_commit != run.base_commit:
        out.append("run head moved without an integration commit")
    # 5
    keys: dict[str, str] = {}
    for j in state.job.values():
        if j.state in (CANCELLED, ERROR, TIMEOUT):           # 失败的作业可以用同一个键重跑
            continue
        if j.key in keys:
            out.append(f"jobs {keys[j.key]} and {j.id} share the key {j.key}")
        keys[j.key] = j.id
    merged_by_cand: dict[str, int] = {}
    for i in chain:
        merged_by_cand[i.candidate_id] = merged_by_cand.get(i.candidate_id, 0) + 1
    out += [f"candidate {c} merged {n} times" for c, n in merged_by_cand.items() if n > 1]
    return out
