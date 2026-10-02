"""纯函数核心的模拟器：用规则驱动图，副作用（作业、git、定位 diff、复核会话）由一个假的“世界”立即或延后完成。

单元测试与重放一致性测试共用。每个事务之后都检查不变量。

复核者：reviewer 是一个函数 (sim, review) -> verdict | None。返回 None 时复核留在 pending_reviews 里，由测试手动
s.review(vid, verdict, runs) 给出结论；默认 APPROVE（批准合并，不判定任何需求）。
"""
from __future__ import annotations

import hashlib
from collections import deque
from typing import Callable, Optional

from belay.core import rules as R
from belay.core.config import BelayConfig
from belay.core.effects import effects_for
from belay.core.invariants import check, check_log
from belay.core.model import Graph, Review
from belay.core.reduce import apply
from belay.core.rules import SnapObs, Tx
from belay.core.verify import check_unit


def h(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()[:12]


def APPROVE(sim: "Sim", v: Review) -> dict:
    return {"merge": v.attempt is not None, "reason": "fine", "summary": f"change at s{v.snapshot}",
            "requirements": [], "feedback": ""}


def MANUAL(sim: "Sim", v: Review) -> None:
    return None


def judge(merge: bool = True, reqs: Optional[dict] = None, **extra) -> Callable[["Sim", Review], dict]:
    """固定结论的复核者：reqs = {需求: (status, level) 或 {status, level, ...}}。"""
    def fn(sim: "Sim", v: Review) -> dict:
        items = []
        for rid, x in (reqs or {}).items():
            if isinstance(x, tuple):
                x = {"status": x[0], "level": x[1] if len(x) > 1 else "E1"}
            items.append({"id": rid, **x})
        return {"merge": merge if v.attempt is not None else False, "reason": extra.get("reason", "ok"),
                "summary": extra.get("summary", "a change"), "requirements": items,
                "waivers": extra.get("waivers", []), "behavior_changes": extra.get("behavior_changes", []),
                "score": extra.get("score"),
                "score_note": extra.get("score_note", ""), "feedback": extra.get("feedback", "")}
    return fn


class World:
    """假的世界：树 → 每个检查的结果。未登记的检查默认沿用基线（原始代码）的结果。"""

    def __init__(self, base_results: dict[str, str]):
        self.base = dict(base_results)
        self.trees: dict[str, dict[str, str]] = {}
        self.flaky_once: set[tuple[str, str]] = set()

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
                 auto_jobs: bool = True, check_each: bool = True, auto_located: bool = True,
                 reviewer: Callable = APPROVE, runs: Optional[list[dict]] = None):
        self.cfg = cfg or BelayConfig(merge_min_interval_sec=0, merge_todo_interval_sec=0)
        self.g = Graph()
        self.log = []
        self.now = t0
        self.world = World(base_results)
        self.auto_jobs = auto_jobs
        self.auto_located = auto_located
        self.check_each = check_each
        self.reviewer = reviewer
        self.review_runs = runs if runs is not None else [{"id": "X1", "cmd": "python -c 1", "rc": 0}]
        self.pending_jobs: deque[str] = deque()
        self.pending_refs: deque[str] = deque()
        self.pending_effects: list = []
        self.pending_reviews: list[str] = []
        self.cancelled_reviews: list[str] = []
        self.ref_ok = True
        self.effects = []
        self._pumping = False

    # ---- 核心
    def do(self, fn: Callable, *args, **kw):
        tx = Tx(self.g, self.now, self.cfg)
        result = fn(tx, *args, **kw)
        self._commit(tx)
        return result

    def _commit(self, tx: Tx) -> None:
        if self.check_each and tx.events:
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
            elif eff.kind in ("locate_diff", "cancel_orphans"):
                self.pending_effects.append(eff)
            elif eff.kind == "review":
                self.pending_reviews.append(eff.args["review"])
            elif eff.kind == "cancel_review":
                vid = eff.args["review"]
                if vid in self.pending_reviews:
                    self.pending_reviews.remove(vid)
                self.cancelled_reviews.append(vid)
        self.pump()

    def pump(self) -> None:
        if self._pumping:
            return
        self._pumping = True
        try:
            progress = True
            while progress:
                progress = False
                while self.pending_refs:
                    aid = self.pending_refs.popleft()
                    a = self.g.attempts[aid]
                    self.do(R.ref_advanced, aid, self.ref_ok, commit="c" + h(a.tree + str(a.date)),
                            files=[("pkg/mod.py", 1, 1)])
                    progress = True
                while self.pending_effects:
                    eff = self.pending_effects.pop(0)
                    if eff.kind == "locate_diff" and self.auto_located:
                        for i in range(eff.args["groups"]):
                            self.do(R.record_located, eff.args["locate"], i, [("pkg/mod.py", 2, 1)], None)
                    elif eff.kind == "cancel_orphans":
                        self.cancel_orphans(eff.args["attempt"])
                    progress = True
                while self.auto_jobs and self.pending_jobs:
                    self.finish_job(self.pending_jobs.popleft())
                    progress = True
                for vid in list(self.pending_reviews):
                    v = self.g.reviews.get(vid)
                    if v is None or v.status != "running":
                        self.pending_reviews.remove(vid)
                        continue
                    verdict = self.reviewer(self, v)
                    if verdict is not None:
                        self.pending_reviews.remove(vid)
                        self.do(R.record_review, vid, verdict, self.review_runs)
                        progress = True
        finally:
            self._pumping = False

    def cancel_orphans(self, aid: str) -> None:
        a = self.g.attempts.get(aid)
        if a is None:
            return
        for jid in a.jobs:
            j = self.g.jobs.get(jid)
            if j is None or j.state != "running":
                continue
            used = any(o.tree == j.tree for o in self.g.attempts.values() if o.id != aid and o.status == "pending")
            if not used:
                if jid in self.pending_jobs:
                    self.pending_jobs.remove(jid)
                self.do(R.job_finished, jid, "cancelled", {}, 0.0, "superseded")

    def finish_job(self, jid: str, state: str = "finished", results: Optional[dict] = None, sec: float = 5.0):
        j = self.g.jobs[jid]
        if j.state != "running":
            return
        if results is None:
            results = self.world.run(j.tree, j.selection) if state == "finished" else {}
        if jid in self.pending_jobs:
            self.pending_jobs.remove(jid)
        reasons = {t: f"assert failure in {t}" for t, st in results.items() if st in ("FAILED", "ERROR")}
        self.do(R.job_finished, jid, state, results, sec, "", reasons)

    # ---- 便捷操作
    def setup(self, task: str, plan: dict, budget: float = 5400, public_checks=(), isolation=None,
              verifier: bool = True):
        from belay.core.plan import renumber, validate_plan
        self.do(R.start_run, "run1", task, budget, public_checks=public_checks, verifier=verifier)
        self.do(R.create_base, "c0", "t0")
        if verifier:
            j1 = self.do(R.ensure_job, "t0", None, "baseline", tag="baseline#1", where="workspace")
            j2 = self.do(R.ensure_job, "t0", None, "baseline", tag="baseline#2")
            for j in (j1, j2):
                if self.g.jobs[j].state == "running":
                    self.finish_job(j, sec=30.0)
            self.do(R.record_baseline, j1, j2, isolation=isolation or {"valid": True})
        else:
            self.do(R.record_baseline, None, None, reason="no checks", isolation={"valid": True})
        rep = validate_plan(task, plan, R.known_checks(self.g))
        assert rep.ok, rep.problems
        self.do(R.propose_plan, 1, plan, True, [])
        self.do(R.freeze_plan, renumber(rep))

    def snap(self, tree: str, files=(("pkg/mod.py", 1, 1),), dropped=(), raw: Optional[str] = None,
             testable: bool = True, reason: str = "writes", worker: str = "w1") -> int:
        obs = SnapObs(tree=tree, raw_tree=raw or tree, files=tuple(files), dropped=tuple(dropped), testable=testable,
                      commit="s" + h(tree))
        return self.do(R.record_snapshot, worker, obs, reason)

    def submit(self, tree: str, files=(("pkg/mod.py", 1, 1),), summary: str = "done", blocked=(), waivers=(),
               **kw) -> str:
        n = self.snap(tree, files, reason="submit", **kw)
        return self.do(R.request_submit, "w1", n, summary, list(blocked), False, list(waivers))

    def review(self, vid: str, verdict: Optional[dict], runs: Optional[list] = None, failed: bool = False) -> None:
        if vid in self.pending_reviews:
            self.pending_reviews.remove(vid)
        self.do(R.record_review, vid, verdict, self.review_runs if runs is None else runs, failed)

    def running_review(self) -> Optional[str]:
        v = next((v for v in self.g.reviews.values() if v.status == "running"), None)
        return v.id if v else None

    def advance(self, sec: float) -> None:
        self.now += sec

    def tick(self) -> None:
        self.do(R.tick)

    def check_log(self):
        assert not check_log(self.log), check_log(self.log)
