"""WorkerPort：Belay 工具与 runtime 之间的接口（belay.tools.base.RuntimeClient 的实现）。

每个请求 = 外壳先观察（需要时给工作区拍快照）→ Runtime.submit(规则) → 需要等结果的请求（checkpoint、
ready_for_review、wait、rollback）等图满足条件 → 渲染给模型看的文字。规则拒绝的请求以 {"error": True} 返回。
runtime 发给这个 worker 的通知（反复被同一回归拒绝、任务被拆分……）由事件监听收集，在下一轮工具结果后注入。
通知只陈述模型自己看不到的事实，不含剩余时间或已用时间：时间只由 runtime 用来决定何时收尾。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from belay.core import rules as R
from belay.core.model import ATT_CREATED, ATT_REJECTED, JOB_RUNNING, REVIEW
from belay.core.queries import last_checkpoint_t
from belay.core.render import render_attempt, render_board, render_job, task_line
from belay.core.rules import Rejected

if TYPE_CHECKING:
    from belay.runtime.driver import BelayRun


class WorkerPort:
    def __init__(self, run: "BelayRun", worker: str):
        self.run = run
        self.w = worker
        self._notices: list[str] = []
        self._last_reminder = run.rt.now()
        run.rt.listeners.append(self._on_events)

    def close(self) -> None:
        if self._on_events in self.run.rt.listeners:
            self.run.rt.listeners.remove(self._on_events)

    # ---------------------------------------------------------------- 通知
    def _on_events(self, events, g) -> None:
        for e in events:
            # 按时长判定的停滞（no_progress）只记入日志、驱动规划器，不告诉模型：那等于按时间催促。
            if (e.type == "stall_detected" and e.get("worker") == self.w and e.get("action") != "stop"
                    and e.get("kind") == "repeated_failure"):
                self._notices.append(f"Your recent checkpoint attempts were all rejected for the same reason "
                                     f"({e.get('detail')}). If your current approach is not converging, it may "
                                     "help to look at those regressions from a different angle, split the work "
                                     "(add_task), or roll back to the last checkpoint." +
                                     (" The planner has been asked to propose a split." if e.get("action") == "replan"
                                      else ""))
            elif e.type == "task_split":
                self._notices.append(f"Task {e.get('task')} was split into "
                                     f"{', '.join(c['id'] for c in e.get('children'))}; claim the one you work on.")
            elif e.type == "lease_expired" and e.get("worker") == self.w:
                self._notices.append(f"Your lease on {e.get('task')} expired; claim it again if you are still on it.")

    def drain_notices(self) -> list[str]:
        g, now, cfg = self.run.rt.graph, self.run.rt.now(), self.run.cfg
        wip = g.wips.get(self.w)
        if (wip and wip.files and now - max(last_checkpoint_t(g), self._last_reminder) > cfg.checkpoint_reminder_sec):
            self._last_reminder = now
            self._notices.append("You have changes that are not in any checkpoint yet. Only checkpointed work is "
                                 "delivered, so call checkpoint once your work is in a sound state.")
        out, self._notices = self._notices, []
        return out

    # ---------------------------------------------------------------- 请求
    async def request(self, name: str, /, **p: Any) -> dict:
        handler = getattr(self, f"_r_{name}", None)
        if handler is None:
            return {"text": f"Unknown request {name}", "error": True}
        try:
            return {"text": await handler(**p), "error": False}
        except Rejected as e:
            return {"text": str(e), "error": True}

    async def _submit(self, rule, *args, **kw):
        return await self.run.rt.submit(rule, *args, **kw)

    async def _r_board(self) -> str:
        rt = self.run.rt
        return render_board(rt.graph, self.w, rt.now(), self.run.cfg)

    async def _r_claim(self, task: str) -> str:
        r = await self._submit(R.claim, self.w, task)
        g = self.run.rt.graph
        t = g.tasks[task]
        lines = [f"You already hold {task}." if r == "already" else f"Claimed {task}.", task_line(g, task)]
        if t.description:
            lines.append(t.description)
        for rid in t.links:
            lines.append(f"{rid} (task text): \"{g.requirements[rid].quote[:600]}\"")
        if t.checks:
            lines.append("Checks that decide when it is done: " + ", ".join(t.checks[:20]))
        else:
            lines.append("It has no checks: when finished it becomes done_unverified once its work is in a checkpoint.")
        return "\n".join(lines)

    async def _r_release(self, task: str, note: str = "") -> str:
        await self._submit(R.release, self.w, task, note)
        return f"Released {task}."

    async def _r_add_task(self, title: str, links: list, description: str = "", blocked_by: list = (),
                          checks: list = ()) -> str:
        tid = await self._submit(R.add_task, self.w, title, links, description, blocked_by, None, checks)
        return f"Added {tid}: " + task_line(self.run.rt.graph, tid) + ". Claim it when you work on it."

    async def _r_note(self, text: str) -> str:
        await self._submit(R.note, self.w, text)
        return "Noted."

    async def _r_report_blocked(self, task: str, kind: str, reason: str, quote=None) -> str:
        await self._submit(R.report_blocked, self.w, task, kind, reason, quote)
        return (f"{task} is recorded as blocked ({kind}). It will be listed in the final report. You can work on "
                "other tasks.")

    async def _r_checkpoint(self, summary: str = "") -> str:
        obs = await self.run.observe(self.w)
        aid = await self._submit(R.request_checkpoint, self.w, obs, "worker", summary=summary)
        if aid is not None:
            await self.run.rt.wait_until(lambda g: g.attempts[aid].status in (ATT_CREATED, ATT_REJECTED))
        return render_attempt(self.run.rt.graph, aid)

    async def _r_ready_for_review(self, task: str, summary: str = "") -> str:
        obs = await self.run.observe(self.w)
        aid = await self._submit(R.request_review, self.w, task, obs, summary=summary)
        await self.run.rt.wait_until(lambda g: g.tasks[task].status != REVIEW)
        return render_attempt(self.run.rt.graph, aid, task)

    async def _r_run_check(self, tests: list = (), full: bool = False) -> str:
        raw, changed = await self.run.observe_raw(self.w)
        jid = await self._submit(R.run_check, self.w, raw, tests, full, changed)
        job = self.run.rt.graph.jobs[jid]
        if job.state != JOB_RUNNING:
            return "(cached: this working tree was already checked with this selection)\n" + \
                render_job(self.run.rt.graph, jid)
        what = "the full suite" if job.selection is None else f"{len(job.selection)} test target(s)"
        return f"Started job {jid} ({what}). Keep working; call wait(jobs=[\"{jid}\"]) when you need the result."

    async def _r_wait(self, jobs: list, timeout: float = 600) -> str:
        g = self.run.rt.graph
        unknown = [j for j in jobs if j not in g.jobs]
        if unknown:
            raise Rejected(f"Unknown job(s) {unknown}.")
        await self.run.rt.wait_until(lambda g: all(g.jobs[j].state != JOB_RUNNING for j in jobs),
                                     timeout=max(1.0, min(float(timeout), 1800.0)))
        g = self.run.rt.graph
        return "\n\n".join(render_job(g, j) for j in jobs)

    async def _r_rollback(self, checkpoint=None) -> str:
        self.run.restored.clear()
        before = {t.id: t.status for t in self.run.rt.graph.tasks.values()}
        to = await self._submit(R.rollback, self.w, checkpoint)
        await self.run.restored.wait()
        g = self.run.rt.graph
        reopened = [tid for tid, st in before.items() if g.tasks[tid].status != st]
        return (f"Working tree restored to checkpoint {to}." +
                (f" Reopened: {', '.join(reopened)}." if reopened else "") +
                " Files you read before may have changed: read them again before editing.")
