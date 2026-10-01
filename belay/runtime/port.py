"""WorkerPort：Belay 工具与 runtime 之间的接口（belay.tools.base.RuntimeClient 的实现）。

每个请求 = 外壳先观察（需要时给工作区拍快照）→ Runtime.submit(规则) → 需要等结果的请求（checkpoint、
ready_for_review、wait、rollback）等图满足条件 → 渲染给模型看的文字。规则拒绝的请求以 {"error": True} 返回。

通知（原则 6：只推 worker 能据此行动的信息）：worker 声明完成的存档（手动存档、review）被降级且问题仍在、
定位结果、诊断结论、复查者重开任务、任务被拆分、同一回归反复被拒。步骤锚点与交接快照在后台验证，被拒只是链头
不动，记在图里（board 可见），不通知：中间态测不过是常态。
通知不含剩余时间或已用时间：时间只由 runtime 用来决定何时收尾；也没有按时间提醒存档。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from belay.core import rules as R
from belay.core.model import ATT_PENDING, ATT_ADVANCING, JOB_FINISHED, JOB_RUNNING, REVIEW
from belay.core.render import (checkpoint_line, render_attempt, render_board, render_diagnosis, render_history,
                               render_job, render_located, render_task, task_line)
from belay.core.rules import Rejected
from belay.runtime.verifier import extract_failure

if TYPE_CHECKING:
    from belay.runtime.driver import BelayRun


class WorkerPort:
    def __init__(self, run: "BelayRun", worker: str):
        self.run = run
        self.w = worker
        self._notices: list[str] = []
        self._returned_locates: set[str] = set()      # 已经随拒绝消息返回过的定位
        run.rt.listeners.append(self._on_events)

    def close(self) -> None:
        if self._on_events in self.run.rt.listeners:
            self.run.rt.listeners.remove(self._on_events)

    # ---------------------------------------------------------------- 通知
    def _on_events(self, events, g) -> None:
        for e in events:
            t = e.type
            if (t == "stall_detected" and e.get("worker") == self.w and e.get("action") != "stop"
                    and e.get("kind") == "repeated_failure"):
                self._notices.append(f"Your recent checkpoint attempts were all rejected for the same reason "
                                     f"({e.get('detail')}). If your current approach is not converging, it may "
                                     "help to look at those regressions from a different angle, split the work "
                                     "(add_task), or undo the change that introduced them." +
                                     (" The planner has been asked to propose a split." if e.get("action") == "replan"
                                      else ""))
            elif t == "task_split":
                self._notices.append(f"Task {e.get('task')} was split into "
                                     f"{', '.join(c['id'] for c in e.get('children'))}; claim the one you work on.")
            elif t == "persistent_regression" and e.get("trigger") == "demoted":
                tests = ", ".join(e.get("tests")[:5])
                cp = g.checkpoints.get(e.get("checkpoint"))
                self._notices.append(f"Checkpoint {e.get('checkpoint')} failed the full test suite on {tests} "
                                     "(the related tests did not select them). Your current changes still make "
                                     f"{tests} fail. The harness is locating where it started; "
                                     + ("the checkpoint is no longer part of what would be delivered."
                                        if cp is not None and cp.demoted else ""))
            elif t == "regression_located":
                loc = g.locates.get(e.get("locate"))
                if loc is None or loc.id in self._returned_locates or loc.epoch != g.epoch:
                    continue
                self._notices.append(render_located(g, loc.id, e.get("group")))
            elif t == "diagnosis_recorded" and not e.get("failed"):
                txt = render_diagnosis(g, e.get("diagnosis"))
                if txt:
                    self._notices.append(txt)
            elif t == "task_reopened" and e.get("reason") in ("review_missing", "review_reading"):
                why = "; ".join(e.get("failures")[:5])
                self._notices.append(f"A reviewer reopened {e.get('task')}: {why}. Claim it again to finish it.")

    def drain_notices(self) -> list[str]:
        out, self._notices = [n for n in self._notices if n], []
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

    async def _r_board(self, status=None, requirement=None, task=None, view=None, page: int = 1) -> str:
        rt = self.run.rt
        return render_board(rt.graph, self.w, rt.now(), self.run.cfg, status=status, requirement=requirement,
                            task=task, view=view, page=page)

    async def _r_task(self, task: str) -> str:
        return render_task(self.run.rt.graph, task)

    async def _r_claim(self, task: str) -> str:
        r = await self._submit(R.claim, self.w, task)
        g = self.run.rt.graph
        t = g.tasks[task]
        lines = [f"You already hold {task}." if r == "already" else f"Claimed {task}; it is your current focus.",
                 task_line(g, task)]
        hint = R.claim_hint(g, task)
        if hint:
            lines.append(hint + " You can still work on it; it is only an ordering hint.")
        if t.description:
            lines.append(t.description)
        for rid in t.links:
            lines.append(f"{rid} (task text): \"{g.requirements[rid].quote[:600]}\"")
        if t.checks:
            lines.append("Checks that decide when it is done: " + ", ".join(t.checks[:20]))
        else:
            lines.append("It has no checks: when finished it becomes done_unverified once its work is in a checkpoint.")
        lines.append("Write your plan for it with todo_write; it becomes the task's step list.")
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

    async def _r_waive_check(self, task: str, tests: list, quote: str, reason: str) -> str:
        waived = await self._submit(R.waive_checks, self.w, task, tests, quote, reason)
        return (f"Waived {len(waived)} check(s) from the regression gate: {', '.join(waived[:10])}. They are listed "
                "in the final report. Checkpoint again (checkpoint or ready_for_review) to save your change.")

    async def _wait_attempt(self, aid: str) -> None:
        await self.run.rt.wait_until(lambda g: g.attempts[aid].status not in (ATT_PENDING, ATT_ADVANCING))

    async def _with_located(self, aid) -> str:
        """被拒时最多等 locate_wait_sec，让定位结果随拒绝消息一起返回。"""
        if aid is None:
            return ""
        g = self.run.rt.graph
        locs = [l for l in g.locates.values() if l.ref == aid]
        if not locs:
            return ""
        loc = locs[0]
        def located(g) -> bool:
            L = g.locates[loc.id]
            return L.status == "concluded" and len(L.results) >= len(L.groups)
        await self.run.rt.wait_until(located, timeout=self.run.cfg.locate_wait_sec)
        g = self.run.rt.graph
        if g.locates[loc.id].results:
            self._returned_locates.add(loc.id)
            return "\n" + render_located(g, loc.id)
        return "\nThe harness is locating where this started; the result will be reported to you."

    async def _r_checkpoint(self, summary: str = "") -> str:
        n = await self.run.take_snapshot("checkpoint")
        aid = await self._submit(R.request_checkpoint, self.w, n, "worker", summary=summary)
        if aid is not None:
            await self._wait_attempt(aid)
        return render_attempt(self.run.rt.graph, aid) + await self._with_located(aid)

    async def _r_ready_for_review(self, task: str, summary: str = "") -> str:
        n = await self.run.take_snapshot("review")
        aid = await self._submit(R.request_review, self.w, task, n, summary=summary)
        await self.run.rt.wait_until(lambda g: g.tasks[task].status != REVIEW)
        return render_attempt(self.run.rt.graph, aid, task) + await self._with_located(aid)

    async def _r_step_done(self, summary: str = "") -> str:
        n = await self.run.take_snapshot("step_done")
        files = await self.run.step_files(n)
        sid = await self._submit(R.step_done, self.w, n, summary, files)
        g = self.run.rt.graph
        st = g.steps[sid]
        nxt = R.current_step(g, st.task)
        state = (f"it is already in checkpoint {st.checkpoint}" if st.checkpoint is not None else
                 "the harness verifies it in the background")
        return (f"Step {sid} ({st.title}) is recorded as finished; {state}." +
                (f" Next step: {nxt.id} {nxt.title}." if nxt else " That was the last step in your plan."))

    async def _r_run_check(self, tests: list = (), full: bool = False, as_gate: bool = False) -> str:
        if as_gate:
            n = await self.run.take_snapshot("gate")
            jid = await self._submit(R.gate_check, self.w, n, tests, full)
        else:
            raw, changed = await self.run.observe_raw(self.w)
            jid = await self._submit(R.run_check, self.w, raw, tests, full, changed)
        job = self.run.rt.graph.jobs[jid]
        if job.state != JOB_RUNNING:
            return "(cached: this tree was already checked with this selection)\n" + render_job(self.run.rt.graph, jid)
        what = "the full suite" if job.selection is None else f"{len(job.selection)} test target(s)"
        how = " as the gate runs it" if as_gate else ""
        return (f"Started job {jid} ({what}{how}). Keep working; call wait(jobs=[\"{jid}\"]) when you need the "
                "result.")

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
        return (f"Working tree restored to checkpoint {checkpoint_line(g, to)}." +
                (f" Reopened: {', '.join(reopened)}." if reopened else "") +
                " Files you read before may have changed: read them again before editing.")

    async def _r_failure_log(self, test: str) -> str:
        g = self.run.rt.graph
        if self.run.verifier is None:
            raise Rejected("No checks are configured for this task.")
        jobs = sorted((j for j in g.jobs.values() if j.state == JOB_FINISHED and test in j.results),
                      key=lambda j: j.started_seq, reverse=True)
        if not jobs:
            raise Rejected(f"No harness run has a result for {test} yet. run_check(tests=[\"{test}\"], as_gate=true) "
                           "runs it the way the gate does.")
        j = jobs[0]
        log = await self.run.verifier.read_log(j.id)
        seg = extract_failure(log, test) if log else ""
        head = f"{test} was {j.results[test]} in job {j.id} ({j.purpose}{', your working tree' if j.live else ''})."
        if not seg:
            why = j.reasons.get(test)
            return head + (f"\nReason: {why}" if why else "\n(no traceback found in the log)")
        return head + "\n" + seg

    async def _r_revert_change(self, located: str) -> str:
        lid, _, grp = located.partition("#")
        g = self.run.rt.graph
        loc = g.locates.get(lid.strip())
        if loc is None:
            raise Rejected(f"Unknown located regression {located}.")
        recs = [r for r in loc.results if str(r.get("group", 0)) == (grp.strip() or "0")]
        if not recs:
            raise Rejected(f"{located} has no located change (yet).")
        rec = recs[0]
        paths = [f[0] for f in rec.get("files") or []]
        if not paths:
            raise Rejected("The located change touches no file outside test paths; nothing to revert.")
        ok, detail = await self.run.repo.revert_files(rec["good"]["tree"], rec["bad"]["tree"], paths)
        if not ok:
            raise Rejected(f"Nothing was changed: {detail}. Undo it by hand, or roll back.")
        await self.run.take_snapshot("revert")
        return (f"Reverted the change between {rec['good'].get('id')} and {rec['bad'].get('id')} in "
                f"{', '.join(paths[:10])} ({detail}). Read these files again before editing them.")

    async def _r_history(self, a=None, b=None) -> str:
        g = self.run.rt.graph
        if a is None or b is None:
            return render_history(g)
        for x in (a, b):
            if x not in g.checkpoints:
                raise Rejected(f"Unknown checkpoint {x}.")
        ta, tb = g.checkpoints[a].tree, g.checkpoints[b].tree
        files = await self.run.repo.numstat(ta, tb)
        diff = await self.run.repo.diff(ta, tb)
        path = self.run.store.put_blob(diff, ".diff") if diff.strip() else None
        body = diff if len(diff) <= 12000 else diff[:12000] + f"\n[... truncated; the full diff is at {path}]"
        return (f"Checkpoint {a} -> {b}: " + ", ".join(f"{p} (+{x} -{y})" for p, x, y in files[:40]) +
                f"\n```diff\n{body}\n```")
