"""纯函数核心的模拟器：用规则驱动图，副作用（作业、git）由一个假的“世界”立即或延后完成。

单元测试与重放一致性测试共用。每追加一条事件都检查不变量。
"""
from __future__ import annotations

import hashlib
from collections import deque
from typing import Callable, Optional

from belay.core import rules as R
from belay.core.config import BelayConfig
from belay.core.effects import effects_for
from belay.core.invariants import check, check_log
from belay.core.model import Graph
from belay.core.reduce import apply
from belay.core.rules import TreeObs, Tx
from belay.core.verify import check_unit


def h(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()[:12]


class World:
    """假的世界：树 → 每个检查的结果。未登记的检查默认沿用基线（原始代码）的结果。"""

    def __init__(self, base_results: dict[str, str]):
        self.base = dict(base_results)
        self.trees: dict[str, dict[str, str]] = {}
        self.flaky_once: set[tuple[str, str]] = set()      # (树, 检查)：第一次失败，之后通过

    def define(self, tree: str, overrides: dict[str, str]) -> str:
        self.trees[tree] = {**self.base, **overrides}
        return tree

    def run(self, tree: str, selection) -> dict[str, str]:
        res = self.trees.get(tree, self.base)
        out = {}
        for tid, st in res.items():
            if selection is None or check_unit(tid) in selection or tid in selection:
                if (tree, tid) in self.flaky_once:
                    self.flaky_once.discard((tree, tid))
                    st = "FAILED"
                out[tid] = st
        return out


class Sim:
    def __init__(self, base_results: dict[str, str], cfg: Optional[BelayConfig] = None, t0: float = 1000.0,
                 auto_jobs: bool = True, check_each: bool = True):
        self.cfg = cfg or BelayConfig()
        self.g = Graph()
        self.log = []
        self.now = t0
        self.world = World(base_results)
        self.auto_jobs = auto_jobs
        self.check_each = check_each
        self.pending_jobs: deque[str] = deque()
        self.pending_refs: deque[str] = deque()
        self.ref_ok = True
        self.effects = []

    # ---- 核心：在 Tx 里执行规则，提交事件，执行副作用
    def do(self, fn: Callable, *args, **kw):
        tx = Tx(self.g, self.now, self.cfg)
        result = fn(tx, *args, **kw)
        self._commit(tx)
        return result

    def _commit(self, tx: Tx) -> None:
        if self.check_each and tx.events:           # 不变量在事务边界上成立（与 runtime 一致）
            g = self.g
            for e in tx.events:
                g = apply(g, e)
            bad = check(g)
            assert not bad, f"invariants violated after {[e.type for e in tx.events]}: {bad}"
        self.log.extend(tx.events)
        self.g = tx.g
        effs = effects_for(tx.events, self.g)
        self.effects.extend(effs)
        for eff in effs:
            if eff.kind == "launch_job":
                self.pending_jobs.append(eff.args["job"])
            elif eff.kind == "advance_ref":
                self.pending_refs.append(eff.args["attempt"])
        self.pump()

    def pump(self) -> None:
        """立即完成排队的副作用（auto_jobs=False 时作业留在队列里，由测试决定何时完成）。"""
        while self.pending_refs:
            aid = self.pending_refs.popleft()
            a = self.g.attempts[aid]
            files = [("pkg/mod.py", 1, 1)]
            self.do(R.ref_advanced, aid, self.ref_ok, commit="c" + h(a.tree + str(a.date)), files=files)
        while self.auto_jobs and self.pending_jobs:
            self.finish_job(self.pending_jobs.popleft())

    def finish_job(self, jid: str, state: str = "finished", results: Optional[dict] = None, sec: float = 5.0):
        j = self.g.jobs[jid]
        if results is None:
            results = self.world.run(j.tree, j.selection) if state == "finished" else {}
        if jid in self.pending_jobs:
            self.pending_jobs.remove(jid)
        self.do(R.job_finished, jid, state, results, sec)

    # ---- 便捷操作
    def setup(self, task: str, plan: dict, budget: float = 5400, public_checks=()):
        from belay.core.plan import renumber, validate_plan
        self.do(R.start_run, "run1", task, budget, public_checks=public_checks)
        self.do(R.create_base, "c0", "t0")
        j1 = self.do(R.ensure_job, "t0", None, "baseline", tag="baseline#1")
        j2 = self.do(R.ensure_job, "t0", None, "baseline", tag="baseline#2")
        for j in (j1, j2):
            if self.g.jobs[j].state == "running":
                self.finish_job(j, sec=30.0)
        self.do(R.record_baseline, j1, j2)
        rep = validate_plan(task, plan, R.known_checks(self.g))
        assert rep.ok, rep.problems
        reqs, tasks = renumber(rep)
        self.do(R.propose_plan, 1, plan, True, [])
        self.do(R.freeze_plan, reqs, tasks)

    def obs(self, tree: str, files=(("pkg/mod.py", 1, 1),), dropped=(), raw: Optional[str] = None) -> TreeObs:
        return TreeObs(tree=tree, raw_tree=raw or tree, files=tuple(files), dropped=tuple(dropped))

    def advance(self, sec: float) -> None:
        self.now += sec

    def check_log(self):
        assert not check_log(self.log), check_log(self.log)
