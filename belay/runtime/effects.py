"""执行 decide() 产出的动作：启动与取消作业、回复工具请求、推进集成分支、调用 Test Author / Reviewer、
启动与停止 worker。所有耗时的动作都作为后台任务运行，结果以新消息投回收件箱，Orchestrator 的循环从不阻塞。
"""
from __future__ import annotations

import asyncio
import posixpath
import re
import time
from typing import TYPE_CHECKING

from belay.runtime import judges
from belay.runtime.bootstrap import pytest_spec
from belay.runtime.decide import ORIG
from belay.runtime.jobs import JobSpec
from belay.runtime.messages import (Advance, AuthoredDraft, CallReviewer, CallTestAuthor, CancelJob,
                                    FinalizeWorkspace, Finish, JobFinished, Merged, Notify, Reply, ReviewDone,
                                    StartJob, StartWorker, StopWorker, Submit, WorkerExited)
from belay.runtime.prompts import worker_task
from belay.worker.transcript import Transcript

if TYPE_CHECKING:                   # pragma: no cover
    from belay.runtime.orchestrator import Orchestrator


class Effects:
    def __init__(self, orch: "Orchestrator"):
        self.o = orch
        self.job_specs: dict[str, JobSpec] = {}
        self.job_tasks: dict[str, asyncio.Task] = {}

    def execute(self, action) -> None:
        handler = getattr(self, "do_" + type(action).__name__, None)
        if handler is None:
            raise TypeError(f"unknown action {type(action).__name__}")
        handler(action)

    # ---- 回复与通知（同步）
    def do_Reply(self, a: Reply) -> None:
        fut = self.o.futures.pop(a.rid, None)
        if fut is not None and not fut.done():
            fut.set_result({"text": a.text, "finished": a.finished, "error": a.error, **a.data})

    def do_Notify(self, a: Notify) -> None:
        self.o.notices.setdefault(a.work_id, []).append(a.text)
        self.o.log_event("notify", work=a.work_id, text=a.text[:500])

    def do_Finish(self, a: Finish) -> None:
        self.o.final_status = a.status
        self.o.final_reason = a.reason
        self.o.finished.set()

    # ---- 作业
    def _job_spec(self, job) -> tuple[JobSpec, object]:
        o = self.o
        paths, spec = o.paths, o.spec
        if job.purpose == "dev":
            runner, d = o.dev_jobs, posixpath.join(paths.dev_jobs, job.id)
        else:
            runner, d = o.root_jobs, posixpath.join(paths.gate_jobs, job.id)
        workspace = paths.orig if job.workspace == ORIG else job.workspace
        timeout = int((spec or {}).get("timeout_sec") or 3600)
        if job.command:
            left = int(max(60.0, o.state.run.deadline_t - time.time()))
            return JobSpec(job.id, d, workspace, left, command=job.command), runner
        overlay_keys = set(job.overlay)
        if job.purpose == "validate":
            kw = dict(select=list(job.selection), overlay=dict(job.overlay), pythonpath=True)
        elif job.purpose == "gate":
            kw = dict(select=[s for s in job.selection if s not in overlay_keys], extra=sorted(overlay_keys),
                      overlay=dict(job.overlay), tree=job.tree, git_dir=paths.git_dir,
                      chown=o.agent_user if o.isolation else None)
        else:
            kw = dict(select=list(job.selection))
        rs = pytest_spec(spec, workspace, **kw)
        return JobSpec(job.id, d, workspace, timeout, runner=paths.runner, spec=rs), runner

    def do_StartJob(self, a: StartJob) -> None:
        job = self.o.state.job[a.job_id]
        js, runner = self._job_spec(job)
        self.job_specs[job.id] = js

        async def go():
            t0 = time.time()
            try:
                await runner.launch(js)
                out = await runner.wait(js, t0)
                self.o.post(JobFinished(time.time(), job.id, out.state, out.result, out.sec, out.log))
            except asyncio.CancelledError:
                raise
            except Exception as e:                  # noqa: BLE001
                self.o.post(JobFinished(time.time(), job.id, "ERROR", {"status": "error", "error": f"{e}"[:800]},
                                        time.time() - t0))
        self.job_tasks[job.id] = self.o.spawn(go(), f"job {job.id}")

    def do_CancelJob(self, a: CancelJob) -> None:
        task = self.job_tasks.pop(a.job_id, None)
        js = self.job_specs.get(a.job_id)
        job = self.o.state.job.get(a.job_id)
        runner = self.o.dev_jobs if job and job.purpose == "dev" else self.o.root_jobs

        async def go():
            if task is not None:
                task.cancel()
            if js is not None:
                try:
                    await runner.cancel(js)
                except Exception as e:              # noqa: BLE001
                    self.o.log(f"[belay] 取消作业 {a.job_id} 失败：{e}")
            reason = (job.result or {}).get("cancel_reason", "cancelled") if job else "cancelled"
            self.o.post(JobFinished(time.time(), a.job_id, "CANCELLED", {"status": "error", "error": reason}))
        self.o.spawn(go(), f"cancel {a.job_id}")

    async def cancel_running_jobs(self) -> None:
        """结束时：停止所有仍在运行的作业。按状态表找（包括已经发出 CancelJob、还没处理完的），
        一律先 TERM，让 runner 恢复工作区（门禁可能临时改过测试文件）。"""
        for task in list(self.job_tasks.values()):
            task.cancel()
        for job in list(self.o.state.job.values()):
            js = self.job_specs.get(job.id)
            if js is None or job.state != "RUNNING":
                continue
            runner = self.o.dev_jobs if job.purpose == "dev" else self.o.root_jobs
            try:
                await runner.cancel(js)
            except Exception as e:                  # noqa: BLE001
                self.o.log(f"[belay] 结束时取消作业 {job.id} 失败：{e}")

    # ---- 集成分支
    def do_Advance(self, a: Advance) -> None:
        async def go():
            try:
                ok = await self.o.repo.advance(a.new, a.expected)
                self.o.post(Merged(time.time(), a.candidate_id, ok, "" if ok else "compare-and-swap failed"))
            except Exception as e:                  # noqa: BLE001
                self.o.post(Merged(time.time(), a.candidate_id, False, str(e)[:300]))
        self.o.spawn(go(), f"advance {a.candidate_id}")

    # ---- worker
    def do_StartWorker(self, a: StartWorker) -> None:
        o = self.o
        client = o.client(a.work_id)
        remaining = o.state.run.deadline_t - time.time() - o.state.run.reserve_sec
        deadline = time.monotonic() + max(0.0, remaining)

        async def refresh() -> str:
            from belay.runtime.prompts import refresh_task
            return refresh_task(o.instruction, o.state, time.time(), a.work_id)

        worker = o.worker_factory(a.work_id, client, refresh, deadline)
        task_text = worker_task(o.instruction, o.state, time.time())

        async def go():
            status, summary = "error", ""
            try:
                res = await worker.run(task_text)
                status, summary = res.status, res.summary
            except asyncio.CancelledError:
                status = "cancelled"
            except Exception as e:                  # noqa: BLE001
                status = "error"
                o.log(f"[belay] worker {a.work_id} 出错：{type(e).__name__}: {e}")
            finally:
                o.post(WorkerExited(time.time(), a.work_id, status, summary))
        o.workers[a.work_id] = (worker, o.spawn(go(), f"worker {a.work_id}", track=False))

    def do_StopWorker(self, a: StopWorker) -> None:
        entry = self.o.workers.get(a.work_id)
        if entry is None:
            return
        worker, task = entry
        worker.config.deadline = time.monotonic()        # 下一轮开始前自己停下
        grace = 2.0 if a.reason == "finished" else self.o.cfg.stop_grace_sec

        async def go():
            try:
                await asyncio.wait_for(asyncio.shield(task), grace)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
        self.o.spawn(go(), f"stop {a.work_id}")

    def do_FinalizeWorkspace(self, a: FinalizeWorkspace) -> None:
        async def go():
            try:
                facts = await self.o.candidate_facts(a.work_id)
                self.o.post(Submit(time.time(), None, a.work_id, f"(submitted by the runtime: {a.reason})", True,
                                   by_runtime=a.reason, **facts))
            except Exception as e:                  # noqa: BLE001
                self.o.post(Submit(time.time(), None, a.work_id, "", True, by_runtime=a.reason, error=str(e)[:300]))
        self.o.spawn(go(), f"finalize {a.work_id}")

    # ---- Test Author 与 Reviewer
    def do_CallTestAuthor(self, a: CallTestAuthor) -> None:
        o = self.o
        check = o.state.check[a.check_id]
        req = o.state.requirement[a.req_id]

        async def go():
            try:
                deadline = time.monotonic() + max(60.0, o.state.run.deadline_t - time.time() - o.state.run.reserve_sec)
                code, err = await judges.write_test(
                    o.llm, o.root_env, orig=o.paths.orig, selector=check.selector, req_id=req.id, req_text=req.text,
                    section=req.section, task_context=o.task_context(), interface=a.interface, feedback=a.feedback,
                    max_turns=o.cfg.test_author_max_turns, deadline=deadline,
                    transcript=Transcript(o.out_dir / f"test_author-{check.id}-{check.attempts}.jsonl"))
                if code is None:
                    o.post(AuthoredDraft(time.time(), check.id, False, error=err))
                    return
                path = posixpath.join(o.paths.checks, check.id, posixpath.basename(check.selector))
                await o.root_env.write_text(path, code)
                o.post(AuthoredDraft(time.time(), check.id, True, stored_at=path, content=code))
            except asyncio.CancelledError:
                raise
            except Exception as e:                  # noqa: BLE001
                o.post(AuthoredDraft(time.time(), check.id, False, error=f"{type(e).__name__}: {e}"[:300]))
        o.spawn(go(), f"test author {a.check_id}")

    def do_CallReviewer(self, a: CallReviewer) -> None:
        o = self.o
        rep = o.state.report[a.report_id]
        req = o.state.requirement.get(rep.req_id) if rep.req_id else None

        async def go():
            failure = ""
            try:
                work = o.state.work[rep.work_id]
                tree = await o.repo.snapshot(work.workspace, o.index_name(work.id))
                diff = await o.repo.diff(o.state.run.base_tree, tree, max_chars=40000)
                failure = o.failure_text(rep.check_ids)
                source = "\n\n".join(filter(None, [await self._test_source(c) for c in rep.check_ids[:5]]))
                verdict = await judges.review(o.llm, kind=rep.kind, req_id=rep.req_id, req_text=req.text if req else "",
                                              checks=rep.check_ids, reason=rep.reason, diff=diff, failure=failure,
                                              test_source=source, record=o.review_record)
                o.post(ReviewDone(time.time(), rep.id, verdict["approved"], verdict["quote"], verdict["reason"],
                                  failure_text=failure))
            except asyncio.CancelledError:
                raise
            except Exception as e:                  # noqa: BLE001
                o.post(ReviewDone(time.time(), rep.id, False, failure_text=failure,
                                  error=f"{type(e).__name__}: {e}"[:300]))
        o.spawn(go(), f"review {a.report_id}")

    async def _test_source(self, node_id: str, max_chars: int = 6000) -> str:
        path, _, rest = node_id.partition("::")
        text = await self.o.repo.show(self.o.state.run.base_tree, path, max_chars=400_000)
        if not text:
            return ""
        name = re.sub(r"\[.*$", "", rest.split("::")[-1]) if rest else ""
        if name:
            m = re.search(rf"^([ \t]*)(async\s+)?def {re.escape(name)}\b", text, re.M)
            if m:
                indent = len(m.group(1))
                lines = text[m.start():].split("\n")
                body = [lines[0]]
                for line in lines[1:]:
                    if line.strip() and (len(line) - len(line.lstrip())) <= indent and not line.lstrip().startswith(
                            ("#", ")", "]", "}")):
                        break
                    body.append(line)
                return f"# {path}\n" + "\n".join(body)[:max_chars]
        return f"# {path}\n" + text[:max_chars]
