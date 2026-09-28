"""decide(state, msg, cfg) -> (changes, actions)：Orchestrator 的全部判断逻辑，纯函数。

不做 IO、不调模型、不执行命令；只读 GraphState，产出变更（Put / Delete / Event）与动作
（StartJob、Reply、Advance……）。依赖规则见 tests/unit/test_layering.py。

主要流程：
  run_check    按（工作区、树、测试选择）去重；命中缓存直接回复，否则排队；结果按基线归类
  wait         作业都结束就回复，否则挂起到作业完成或超时
  submit       候选 → 门禁作业（检查点跑相关子集，最终跑全量）→ 无回归则 Advance（比较并交换）
               → Merged 后：检查点继续工作；最终提交按账本决定结束或退回
  request_test Test Author 写测试 → 在原始代码上验证"断言层面失败" → 收录为独立检查
  report_conflict  reviewer 裁决 → runtime 校验引文逐字存在 → 写入账本
  截止保护     剩余时间不足预留时停止 worker，把当前工作区作为最终候选跑一次门禁；到点交付 HEAD
"""
from __future__ import annotations

from dataclasses import replace

from belay.graph.evidence import (authored_status, classify, environment_like, interface_mismatch, judge_fail_before,
                                  normalize_error, related_test_files, signature_id, test_file)
from belay.graph.ledger import counts, job_text, ledger_text, rejection_text, statuses
from belay.graph.model import (CANCELLED, DONE, FAIL, FAILED, JOB_FINAL, JOB_RUNNING, MERGED, OPEN, PASS, QUEUED,
                               RUN_DONE, RUN_INCOMPLETE, RUNNING, VERIFYING, Candidate, Check, FailureSignature,
                               GraphState, Integration, Job, Report, Tx, Waiter, check_digest)
from belay.graph.requirements import quote_in
from belay.runtime.messages import (Advance, AuthoredDraft, CallReviewer, CallTestAuthor, CancelJob,
                                    FinalizeWorkspace, Finish, JobFinished, LedgerQuery, Merged, Message, Notify,
                                    Reply, ReportConflict, RequestTest, ReviewDone, RunCheck, Start, StartJob,
                                    StartWorker, Stop, StopWorker, Submit, Tick, Wait, WorkerExited)

REPORT_KINDS = ("test_conflict", "insufficient_info", "environment")
ORIG = "@orig"                      # validate 作业的工作区占位符，由 effects 解析为原始代码目录
MAX_SIGNATURES_PER_JOB = 200


def decide(state: GraphState, msg: Message, cfg) -> tuple[list, list]:
    tx = Tx(state, msg.now)
    handler = HANDLERS.get(type(msg))
    if handler is None:
        raise TypeError(f"unknown message {type(msg).__name__}")
    handler(tx, msg, cfg)
    return tx.changes, tx.actions


# ---- 小工具 -----------------------------------------------------------------------

def _left(tx: Tx) -> float:
    return tx.run.deadline_t - tx.now


def _reply(tx: Tx, rid: str | None, text: str, **kw) -> None:
    if rid:
        tx.act(Reply(rid, text, **kw))


def _finished(tx: Tx, rid: str | None) -> bool:
    if tx.run.phase == "finished":
        _reply(tx, rid, "The run has already finished; nothing more is accepted.", finished=True)
        return True
    return False


def _job_key(purpose: str, workspace: str, tree: str, selection: list[str], command: str | None,
             extra: str = "") -> str:
    return "|".join([purpose, workspace, tree, ",".join(sorted(selection)), command or "", extra])


def _find_job(tx: Tx, key: str) -> Job | None:
    for j in tx.all("job"):
        if j.key == key and j.state in (QUEUED, JOB_RUNNING, DONE):
            return j
    return None


def _new_job(tx: Tx, **kw) -> Job:
    job = Job(id=tx.next_id("J"), created_t=tx.now, **kw)
    tx.put(job)
    tx.event("job_created", job=job.id, purpose=job.purpose, level=job.level, selection=job.selection[:50],
             command=job.command, tree=job.tree)
    return job


def _start(tx: Tx, job: Job) -> None:
    tx.update(job, state=JOB_RUNNING)
    tx.act(StartJob(job.id))


def _schedule(tx: Tx, cfg) -> None:
    """开发检查按创建顺序启动，受并发上限约束；门禁运行期间同一工作区的开发检查不启动。"""
    jobs = tx.all("job")
    gate_ws = {j.workspace for j in jobs if j.purpose == "gate" and j.state == JOB_RUNNING}
    running = sum(1 for j in jobs if j.purpose == "dev" and j.state == JOB_RUNNING)
    for j in sorted((j for j in jobs if j.purpose == "dev" and j.state == QUEUED), key=lambda j: _num(j.id)):
        if running >= cfg.max_parallel_jobs:
            break
        if j.workspace in gate_ws:
            continue
        _start(tx, j)
        running += 1


def _num(id_: str) -> int:
    d = "".join(c for c in id_ if c.isdigit())
    return int(d) if d else 0


def _overlay_ignore(job: Job, tests: dict) -> set[str]:
    return {t for t in tests if test_file(t) in job.overlay}


def _classify_job(state: GraphState, job: Job):
    tests = (job.result or {}).get("tests") or {}
    sel = [s for s in job.selection if s not in job.overlay]
    selected = None if job.level == "full" or not sel else sel
    return classify(state.baseline(), tests, selected, state.waived_tests(), _overlay_ignore(job, tests))


def _job_summary(state: GraphState, job: Job) -> str:
    if job.state == CANCELLED:
        return f"Job {job.id} was cancelled ({(job.result or {}).get('error') or 'the workspace was needed'})."
    if job.state not in JOB_FINAL:
        return f"Job {job.id} is still {job.state.lower()}."
    if job.command:
        return job_text(job, None)
    if (job.result or {}).get("status") not in ("ok",):
        return job_text(job, None)
    return job_text(job, _classify_job(state, job))


def _resolve_waiters(tx: Tx) -> None:
    view = tx.view()
    for w in tx.all("waiter"):
        jobs = [view.job.get(j) for j in w.job_ids]
        if all(j is not None and j.state in JOB_FINAL for j in jobs):
            _reply(tx, w.id, "\n\n".join(_job_summary(view, j) for j in jobs))
            tx.delete("waiter", w.id)


def _cancel_job(tx: Tx, job: Job, why: str) -> None:
    if job.state == QUEUED:
        tx.update(job, state=CANCELLED, finished_t=tx.now, result={"error": why})
    elif job.state == JOB_RUNNING:
        tx.update(job, result={**(job.result or {}), "cancel_reason": why})
        tx.act(CancelJob(job.id))


def _record_signatures(tx: Tx, job: Job) -> None:
    res = job.result or {}
    tests, reasons = res.get("tests") or {}, res.get("reasons") or {}
    base = tx.state.baseline()
    n = 0
    for t, s in sorted(tests.items()):
        if s not in ("FAILED", "ERROR") or test_file(t) in job.overlay:
            continue
        err = normalize_error(reasons.get(t, ""))
        sid = signature_id(t, err)
        sig = tx.get("signature", sid)
        if sig is None:
            klass = "baseline" if base.get(t) == FAIL else ("environment" if environment_like(reasons.get(t, ""))
                                                            else "new")
            tx.put(FailureSignature(sid, t, err, klass, job.id))
        else:
            tx.update(sig, count=sig.count + 1)
        n += 1
        if n >= MAX_SIGNATURES_PER_JOB:
            break


# ---- 运行控制 ---------------------------------------------------------------------

def on_start(tx: Tx, msg: Start, cfg) -> None:
    tx.event("start", deadline_t=tx.run.deadline_t, reserve_sec=tx.run.reserve_sec)
    for w in tx.all("work"):
        if w.state == RUNNING:
            tx.act(StartWorker(w.id))


def on_tick(tx: Tx, msg: Tick, cfg) -> None:
    view = None
    for w in tx.all("waiter"):
        if tx.now >= w.until_t:
            view = view or tx.view()
            _reply(tx, w.id, "Wait timed out; job status:\n" +
                   "\n".join(_job_summary(view, view.job[j]) for j in w.job_ids if j in view.job))
            tx.delete("waiter", w.id)
    run = tx.run
    if run.phase == "finished":
        return
    left = _left(tx)
    if left <= 0:
        _finish(tx, cfg, RUN_INCOMPLETE, "budget exhausted; delivering the integration branch HEAD", stop=True)
        return
    if run.phase == "working" and left <= run.reserve_sec:
        tx.update(run, phase="stopping")
        tx.event("deadline_reserve", left=left)
        for w in tx.all("work"):
            if w.state in (RUNNING, VERIFYING):
                tx.act(StopWorker(w.id, "deadline"))


def on_stop(tx: Tx, msg: Stop, cfg) -> None:
    _finish(tx, cfg, RUN_INCOMPLETE, msg.reason, stop=True)


def _finish(tx: Tx, cfg, status: str, reason: str, rid: str | None = None, text: str = "",
            stop: bool = False) -> None:
    run = tx.run
    if run.phase == "finished":
        return
    tx.update(run, phase="finished", status=status, finish_reason=reason)
    for j in tx.all("job"):
        if j.state in (QUEUED, JOB_RUNNING):
            _cancel_job(tx, j, "the run finished")
    for w in tx.all("waiter"):
        _reply(tx, w.id, "The run has finished.", finished=True)
        tx.delete("waiter", w.id)
    for c in tx.all("candidate"):
        if c.verdict == "pending":
            tx.update(c, verdict="error")
            if c.rid != rid:
                _reply(tx, c.rid, "The run has finished before this candidate was decided.", finished=True)
    for r in tx.all("report"):
        if r.verdict == "pending":
            _reply(tx, r.rid, "The run has finished before the report was reviewed.", finished=True)
    if rid:
        tx.act(Reply(rid, (text + "\n\n" if text else "") + f"The run is finished ({status}).", finished=True))
    if stop:
        for w in tx.all("work"):
            if w.state in (RUNNING, VERIFYING):
                tx.act(StopWorker(w.id, "finished"))
    sts = statuses(tx.view(), finished=True)
    tx.event("finish", status=status, reason=reason, ledger=counts(sts),
             merges=len(tx.view().integration))
    tx.act(Finish(status, reason))


# ---- 检查与等待 -------------------------------------------------------------------

def on_run_check(tx: Tx, msg: RunCheck, cfg) -> None:
    if _finished(tx, msg.rid):
        return
    run = tx.run
    work = tx.get("work", msg.work_id)
    level = ""
    if msg.command:
        selection: list[str] = []
    elif not run.gate_available:
        _reply(tx, msg.rid, "This task has no known test suite, so run_check needs a shell command: "
                            "run_check(command=\"...\").", error=True)
        return
    elif msg.full:
        selection, level = [], "full"
    elif msg.tests:
        selection, level = [str(t) for t in msg.tests], "selected"
    else:
        selection, level = related_test_files(msg.changed, run.test_files), "related"
        if not selection:
            _reply(tx, msg.rid, "No test files look related to your changes"
                                f" ({', '.join(msg.changed[:8]) or 'no changes yet'}). Pass tests=[...] with test "
                                "files or node ids, or full=true.", error=True)
            return
    key = _job_key("dev", work.workspace, msg.tree, selection, msg.command, level)
    job = _find_job(tx, key)
    if job is not None and job.state == DONE:
        tx.event("job_reused", job=job.id)
        _reply(tx, msg.rid, "Cached result for this exact working tree:\n" + _job_summary(tx.state, job))
        return
    if job is not None:
        _reply(tx, msg.rid, f"Job {job.id} with the same tree and tests is already {job.state.lower()}; "
                            f"use wait([\"{job.id}\"]).")
        return
    job = _new_job(tx, key=key, purpose="dev", workspace=work.workspace, tree=msg.tree, selection=selection,
                   command=msg.command, level=level, work_id=work.id)
    _schedule(tx, cfg)
    what = f"command {msg.command!r}" if msg.command else (
        "the full test suite" if level == "full" else f"{len(selection)} test target(s): " + ", ".join(selection[:8])
        + (" ..." if len(selection) > 8 else ""))
    state = tx.get("job", job.id).state.lower()
    _reply(tx, msg.rid, f"Job {job.id} {state} ({what}). Keep working and call wait([\"{job.id}\"]) when you need "
                        "the result; edits you make while it runs may or may not be picked up.",
           data={"job_id": job.id})


def on_wait(tx: Tx, msg: Wait, cfg) -> None:
    unknown = [j for j in msg.job_ids if tx.get("job", j) is None]
    if unknown or not msg.job_ids:
        _reply(tx, msg.rid, f"Unknown job id(s): {unknown or msg.job_ids}.", error=True)
        return
    jobs = [tx.get("job", j) for j in msg.job_ids]
    if all(j.state in JOB_FINAL for j in jobs):
        _reply(tx, msg.rid, "\n\n".join(_job_summary(tx.state, j) for j in jobs))
        return
    until = tx.now + max(1.0, min(float(msg.timeout or 600), cfg.wait_max_sec, max(1.0, _left(tx))))
    tx.put(Waiter(msg.rid, msg.work_id, list(msg.job_ids), until))


def on_ledger(tx: Tx, msg: LedgerQuery, cfg) -> None:
    _reply(tx, msg.rid, ledger_text(tx.state, tx.now))


def on_job_finished(tx: Tx, msg: JobFinished, cfg) -> None:
    job = tx.get("job", msg.job_id)
    if job is None or job.state in JOB_FINAL:          # 重复的完成消息不重复计账（不变量 5）
        return
    result = dict(msg.result or {})
    if job.result.get("cancel_reason") and msg.state == CANCELLED:
        result.setdefault("error", job.result["cancel_reason"])
    job = tx.update(job, state=msg.state, result=result, sec=msg.sec, log=msg.log, finished_t=tx.now)
    tx.event("job_finished", job=job.id, purpose=job.purpose, state=job.state, sec=msg.sec,
             status=result.get("status"), n_tests=len(result.get("tests") or {}))
    if job.purpose in ("dev", "gate") and not job.command:
        _record_signatures(tx, job)
    if job.purpose == "gate":
        _on_gate_done(tx, cfg, job)
    elif job.purpose == "validate":
        _on_validate_done(tx, cfg, job)
    _resolve_waiters(tx)
    _schedule(tx, cfg)


# ---- 提交与门禁 -------------------------------------------------------------------

def _gate_level(tx: Tx, cfg, final: bool) -> str:
    run = tx.run
    if cfg.gate == "off" or not run.gate_available:
        return "none"
    level = cfg.final_gate if final else cfg.checkpoint_gate
    if level == "full" and final and _left(tx) < run.full_gate_sec * cfg.gate_reserve_factor + 30:
        level = "related"                           # 剩余时间不够跑全量
    return level


def on_submit(tx: Tx, msg: Submit, cfg) -> None:
    if _finished(tx, msg.rid):
        return
    run = tx.run
    work = tx.get("work", msg.work_id)
    if msg.error:
        _reply(tx, msg.rid, f"Could not snapshot your working tree: {msg.error}", error=True)
        if msg.by_runtime:
            _finish(tx, cfg, RUN_INCOMPLETE, f"final snapshot failed: {msg.error}", stop=True)
        return
    pending = [c for c in tx.all("candidate") if c.work_id == work.id and c.verdict == "pending"]
    if pending:
        _reply(tx, msg.rid, f"Candidate {pending[0].id} is still being checked.", error=True)
        return
    cand = Candidate(id=tx.next_id("C"), work_id=work.id, final=msg.final, commit=msg.commit, tree=msg.tree,
                     base_commit=run.head_commit, changed=list(msg.changed_since_base),
                     dropped_tests=list(msg.dropped_tests), summary=msg.summary, by_runtime=msg.by_runtime,
                     rid=msg.rid, created_t=tx.now)
    tx.event("submit", candidate=cand.id, final=msg.final, by_runtime=msg.by_runtime, changed=len(msg.changed),
             dropped_tests=msg.dropped_tests[:20])
    if msg.tree == tx.run.head_tree:
        tx.put(replace(cand, verdict="unchanged"))
        if msg.final or msg.by_runtime:
            _after_final(tx, cfg, tx.get("candidate", cand.id), work, "No changes since the last merge.")
        else:
            _reply(tx, msg.rid, "No changes since the last merge; nothing to check.")
        return

    level = _gate_level(tx, cfg, msg.final or bool(msg.by_runtime))
    authored = tx.state.authored() if level != "none" else []
    overlay = {c.selector: c.stored_at for c in authored}
    if level == "full":
        selection = []
    elif level == "related":
        selection = related_test_files(msg.changed, run.test_files)
    else:
        selection = []
    if level == "related" and not selection and not overlay:
        level = "none"
    tx.put(replace(cand, level=level))
    tx.update(work, state=VERIFYING, summary=msg.summary)
    if level == "none":
        tx.act(Advance(cand.id, run.head_commit, cand.commit))
        return
    # 门禁在规范工作区原地执行（worker 在 submit 期间阻塞），先让出工作区
    for j in tx.all("job"):
        if j.purpose == "dev" and j.workspace == work.workspace and j.state in (QUEUED, JOB_RUNNING):
            _cancel_job(tx, j, "a gate run needed the workspace")
    sel = sorted(set(selection) | set(overlay)) if level == "related" else sorted(overlay)
    key = _job_key("gate", work.workspace, cand.tree, sel, None, level)
    job = _find_job(tx, key)
    if job is not None and job.state == DONE:
        tx.put(replace(tx.get("candidate", cand.id), job_id=job.id))
        tx.event("job_reused", job=job.id, candidate=cand.id)
        _on_gate_done(tx, cfg, job, cand.id)
        _resolve_waiters(tx)
        return
    job = _new_job(tx, key=key, purpose="gate", workspace=work.workspace, tree=cand.tree, selection=sel,
                   level=level, work_id=work.id, candidate_id=cand.id, overlay=overlay)
    tx.put(replace(tx.get("candidate", cand.id), job_id=job.id))
    _start(tx, job)
    _resolve_waiters(tx)


def _authored_results(tx: Tx, job: Job, tree: str) -> dict[str, str]:
    tests = (job.result or {}).get("tests") or {}
    out = {}
    for c in tx.state.authored():
        if c.selector in job.overlay:
            st = authored_status(tests, c.nodes)
            out[c.id] = st
            c2 = tx.get("check", c.id)
            tx.update(c2, results={**c2.results, tree: st})
    return out


def _authored_note(tx: Tx, job: Job, results: dict[str, str]) -> str:
    reasons = (job.result or {}).get("reasons") or {}
    lines = []
    for cid, st in results.items():
        c = tx.get("check", cid)
        if st == PASS:
            lines.append(f"Independent test {cid} ({c.req_id}): passes.")
        else:
            why = "; ".join(f"{n}: {reasons.get(n, '')[:200]}" for n in c.nodes[:3])
            hint = (" This looks like an interface mismatch (import/name/signature), not wrong behaviour; check the "
                    f"interface you declared: {c.interface or '(none)'}." if interface_mismatch(reasons, c.nodes)
                    else "")
            lines.append(f"Independent test {cid} ({c.req_id}): FAILS — {why}.{hint}")
    return "\n".join(lines)


def _on_gate_done(tx: Tx, cfg, job: Job, candidate_id: str | None = None) -> None:
    """门禁作业结束（或复用了同一个树上已完成的门禁作业）→ 裁决候选。"""
    cand = tx.get("candidate", candidate_id or job.candidate_id)
    if cand is None or cand.verdict != "pending":
        return
    work = tx.get("work", cand.work_id)
    run = tx.run
    res = job.result or {}
    if job.state != DONE or res.get("status") != "ok":
        tx.update(cand, verdict="error")
        tx.update(work, state=RUNNING)
        text = f"The gate could not run ({job.state.lower()}: {str(res.get('error', ''))[:300]})."
        tx.event("gate_error", candidate=cand.id, job=job.id, error=text)
        if run.phase != "working" or cand.by_runtime:
            _finish(tx, cfg, RUN_INCOMPLETE, "the final gate could not run; delivering the last verified state",
                    rid=cand.rid, text=text, stop=True)
        else:
            _reply(tx, cand.rid, text + " Your candidate was not merged; you can submit again.", error=True)
        return
    authored = _authored_results(tx, job, cand.tree)
    cl = _classify_job(tx.state, job)
    reasons = res.get("reasons") or {}
    note = _authored_note(tx, job, authored)
    if cl.regressions and cfg.gate == "block":
        text = rejection_text(cl, reasons, job.level) + ("\n" + note if note else "")
        tx.update(cand, verdict="rejected", regressions=cl.regressions)
        tx.update(work, state=RUNNING, rejections=work.rejections + 1, last_rejection=text)
        tx.event("gate_rejected", candidate=cand.id, job=job.id, level=job.level, regressions=cl.regressions[:50],
                 n_regressions=len(cl.regressions))
        if run.phase != "working" or cand.by_runtime:
            _finish(tx, cfg, RUN_INCOMPLETE, "the final candidate was rejected by the gate; delivering the last "
                    "verified state", rid=cand.rid, text=text, stop=True)
        else:
            _reply(tx, cand.rid, text)
        return
    extra = ""
    if cl.regressions:                                  # advise 模式：只提示
        extra = "\nAdvisory: " + rejection_text(cl, reasons, job.level)
    gate_note = f"Gate ({job.level}): {cl.total} tests ran, no regressions." if not cl.regressions else \
        f"Gate ({job.level}): {len(cl.regressions)} regression(s), merged anyway (advisory mode)."
    tx.update(cand, regressions=cl.regressions if cfg.gate != "block" else [],
              gate_note=gate_note + ("\n" + note if note else "") + extra)
    tx.event("gate_passed", candidate=cand.id, job=job.id, level=job.level, tests=cl.total,
             advisory_regressions=len(cl.regressions))
    tx.act(Advance(cand.id, run.head_commit, cand.commit))


def on_merged(tx: Tx, msg: Merged, cfg) -> None:
    cand = tx.get("candidate", msg.candidate_id)
    if cand is None or cand.verdict != "pending":
        return
    work = tx.get("work", cand.work_id)
    run = tx.run
    if not msg.ok:
        tx.update(cand, verdict="rejected")
        tx.update(work, state=RUNNING)
        text = f"Could not advance the integration branch ({msg.error or 'it moved'}); submit again."
        tx.event("merge_failed", candidate=cand.id, error=msg.error)
        if run.phase != "working" or cand.by_runtime:
            _finish(tx, cfg, RUN_INCOMPLETE, "the final merge failed", rid=cand.rid, text=text, stop=True)
        else:
            _reply(tx, cand.rid, text, error=True)
        return
    seq = len(tx.all("integration")) + 1
    tx.put(Integration(id=f"I{seq}", seq=seq, commit=cand.commit, tree=cand.tree, candidate_id=cand.id,
                       level=cand.level or "none", t=tx.now))
    cand = tx.update(cand, verdict="merged")
    run = tx.update(run, head_commit=cand.commit, head_tree=cand.tree)
    tx.event("merged", candidate=cand.id, seq=seq, commit=cand.commit, level=cand.level, final=cand.final)
    prefix = f"Merged as integration commit #{seq} (gate: {cand.level or 'none'})."
    if cand.gate_note:
        prefix += "\n" + cand.gate_note
    if cand.dropped_tests:
        prefix += (f"\nChanges to {len(cand.dropped_tests)} test file(s) were left out of the deliverable: "
                   + ", ".join(cand.dropped_tests[:8]) + ".")
    if cand.final or cand.by_runtime or run.phase != "working":
        _after_final(tx, cfg, cand, work, prefix)
        return
    tx.update(work, state=RUNNING)
    sts = statuses(tx.view())
    cnt = counts(sts)
    _reply(tx, cand.rid, prefix + " Your progress is safe on the integration branch; keep working. Requirements: "
           + ", ".join(f"{k} {v}" for k, v in cnt.items() if v) + ".")


def _after_final(tx: Tx, cfg, cand: Candidate, work, prefix: str) -> None:
    """最终提交已合并（或没有改动）：按账本决定结束，还是把信息退回给 worker。"""
    run = tx.run
    view = tx.view()
    sts = statuses(view, finished=False)
    failed = [s for s in sts if s.status == FAILED]
    open_ = [s for s in sts if s.status == OPEN]
    left = _left(tx)
    by_runtime = bool(cand.by_runtime) or run.phase != "working"
    if not by_runtime:
        can_continue = left > run.reserve_sec + 300
        if failed and can_continue and work.final_bounces < cfg.max_final_bounces:
            tx.update(work, state=RUNNING, final_bounces=work.final_bounces + 1)
            tx.event("final_bounced", candidate=cand.id, why="failed", reqs=[s.id for s in failed])
            _reply(tx, cand.rid, prefix + f"\nNot finished: {len(failed)} requirement(s) fail their independent tests:\n"
                   + "\n".join(f"  {s.id}: {s.why}" for s in failed[:20])
                   + "\nFix them (or call report_conflict if a test contradicts the task), then submit(final=true) "
                     "again.")
            return
        quota_left = cfg.test_author_quota - len(view.authored(active_only=False))
        if (open_ and cfg.final_info_bounce and not work.info_bounced and run.test_author_available
                and cfg.test_author and quota_left > 0 and left / 60 > cfg.final_info_min_left_min):
            tx.update(work, state=RUNNING, info_bounced=True)
            tx.event("final_bounced", candidate=cand.id, why="no_evidence", reqs=[s.id for s in open_])
            _reply(tx, cand.rid, prefix + f"\nBefore you finish: {len(open_)} of {len(sts)} requirements have no "
                   f"independent evidence and will be reported as UNKNOWN ({left / 60:.0f} min left, "
                   f"{quota_left} independent test(s) available): "
                   + ", ".join(s.id for s in open_[:40])
                   + ".\nIf you want evidence for some of them, call request_test; otherwise call submit(final=true) "
                     "again and the run ends.")
            return
    tx.update(work, state=MERGED)
    status = RUN_DONE if not failed and cand.by_runtime != "deadline" else RUN_INCOMPLETE
    reason = {"deadline": "deadline reached; the final working tree was checked and merged",
              "worker_exit": "the worker stopped; its final working tree was checked and merged"}.get(
        cand.by_runtime, "final submission accepted")
    cnt = counts(statuses(tx.view(), finished=True))
    _finish(tx, cfg, status, reason, rid=cand.rid,
            text=prefix + " Requirements: " + ", ".join(f"{k} {v}" for k, v in cnt.items() if v) + ".")


def on_worker_exited(tx: Tx, msg: WorkerExited, cfg) -> None:
    work = tx.get("work", msg.work_id)
    if work is None:
        return
    tx.event("worker_exited", work=work.id, status=msg.status)
    run = tx.run
    if run.phase == "finished" or msg.status == "submitted":
        return
    pending = [c for c in tx.all("candidate") if c.work_id == work.id and c.verdict == "pending"]
    reason = "deadline" if run.phase == "stopping" or msg.status == "deadline" else "worker_exit"
    tx.update(run, phase="finalizing")
    if pending:                                     # 正在检查的候选决定结果
        return
    tx.act(FinalizeWorkspace(work.id, reason))


# ---- 独立测试 ---------------------------------------------------------------------

def on_request_test(tx: Tx, msg: RequestTest, cfg) -> None:
    if _finished(tx, msg.rid):
        return
    run = tx.run
    if not cfg.test_author or not run.test_author_available:
        _reply(tx, msg.rid, "Independent tests are not available for this task.", error=True)
        return
    req = tx.get("requirement", msg.req_id)
    if req is None:
        _reply(tx, msg.rid, f"Unknown requirement {msg.req_id!r}; see ledger for the ids.", error=True)
        return
    mine = [c for c in tx.state.authored(req.id, active_only=False) if c.status != "rejected"]
    if mine:
        _reply(tx, msg.rid, f"{req.id} already has independent test {mine[0].id} ({mine[0].status}).", error=True)
        return
    everything = tx.state.authored(active_only=False)
    if len(everything) >= cfg.test_author_quota:
        _reply(tx, msg.rid, f"The quota of {cfg.test_author_quota} independent tests is used up.", error=True)
        return
    if sum(c.status == "pending" for c in everything) >= cfg.test_author_parallel:
        _reply(tx, msg.rid, f"{cfg.test_author_parallel} independent tests are already being written; request this "
                            "one after one of them is done.", error=True)
        return
    cid = tx.next_id("T")
    tx.put(Check(id=cid, source="authored", selector=f".belay_checks/test_belay_{cid.lower()}.py", req_id=req.id,
                 status="pending", attempts=1, requested_by=msg.work_id, interface=msg.interface))
    tx.event("test_requested", check=cid, req=req.id, interface=bool(msg.interface))
    tx.act(CallTestAuthor(cid, req.id, msg.interface))
    _reply(tx, msg.rid, f"An independent Test Author is writing a test for {req.id} (check {cid}). It sees the "
                        "requirement, the original code and your declared interface, not your implementation. You "
                        "will be notified when the test is accepted or rejected; keep working.")


def on_authored_draft(tx: Tx, msg: AuthoredDraft, cfg) -> None:
    c = tx.get("check", msg.check_id)
    if c is None or c.status != "pending":
        return
    if not msg.ok:
        _reject_test(tx, c, f"the Test Author did not produce a test ({msg.error})")
        return
    c = tx.update(c, stored_at=msg.stored_at, content=msg.content)
    run = tx.run
    key = _job_key("validate", ORIG, run.base_tree, [c.selector], None, f"{c.id}#{c.attempts}")
    job = _new_job(tx, key=key, purpose="validate", workspace=ORIG, tree=run.base_tree, selection=[c.selector],
                   check_id=c.id, overlay={c.selector: msg.stored_at}, work_id=c.requested_by)
    _start(tx, job)


def _reject_test(tx: Tx, c: Check, why: str) -> None:
    tx.update(c, status="rejected", note=why)
    tx.event("test_rejected", check=c.id, req=c.req_id, why=why)
    tx.act(Notify(c.requested_by or "W1", f"The independent test {c.id} for {c.req_id} was rejected: {why}. "
                  f"{c.req_id} has no independent evidence and will stay UNKNOWN unless another test is accepted."))


def _on_validate_done(tx: Tx, cfg, job: Job) -> None:
    c = tx.get("check", job.check_id)
    if c is None or c.status != "pending":
        return
    res = job.result or {}
    if job.state != DONE or res.get("status") != "ok":
        verdict, nodes, detail = "invalid", [], f"the test could not run on the original code: " \
                                                f"{job.state.lower()} {str(res.get('error', ''))[:300]}"
    else:
        fb = judge_fail_before(res.get("tests") or {}, res.get("reasons") or {}, c.selector)
        verdict, nodes, detail = fb.verdict, fb.nodes, fb.detail
    tx.event("test_validated", check=c.id, req=c.req_id, verdict=verdict, nodes=nodes[:20])
    if verdict == "accepted":
        c = replace(c, status="active", nodes=nodes, note=detail)
        c = tx.put(replace(c, digest=check_digest(c)))
        code = c.content if len(c.content) <= 6000 else c.content[:6000] + "\n# ... truncated"
        tx.act(Notify(c.requested_by or "W1",
                      f"Independent test {c.id} for {c.req_id} was accepted ({detail}). It runs in every gate from "
                      f"now on; {c.req_id} counts as SUPPORTED once it passes on the integration branch. It is kept "
                      f"outside your working tree; for reference, its content is:\n```python\n{code}\n```"))
        return
    if verdict in ("invalid", "not_assertion") and c.attempts <= cfg.test_author_retries:
        tx.update(c, attempts=c.attempts + 1)
        tx.act(CallTestAuthor(c.id, c.req_id, c.interface, feedback=detail))
        return
    _reject_test(tx, c, detail)


# ---- 上报 -------------------------------------------------------------------------

def on_report(tx: Tx, msg: ReportConflict, cfg) -> None:
    if _finished(tx, msg.rid):
        return
    if not cfg.reports:
        _reply(tx, msg.rid, "Reports are not available in this run.", error=True)
        return
    if msg.kind not in REPORT_KINDS:
        _reply(tx, msg.rid, f"kind must be one of {', '.join(REPORT_KINDS)}.", error=True)
        return
    req = tx.get("requirement", msg.req_id) if msg.req_id else None
    if msg.kind in ("test_conflict", "insufficient_info") and req is None:
        _reply(tx, msg.rid, f"A {msg.kind} report must name a requirement id (see ledger).", error=True)
        return
    base = tx.state.baseline()
    checks = [str(c) for c in msg.check_ids]
    if msg.kind in ("test_conflict", "environment"):
        if not checks:
            _reply(tx, msg.rid, "Name the failing test(s) in checks, using the ids exactly as run_check reported "
                                "them.", error=True)
            return
        unknown = [c for c in checks if c not in base]
        if unknown:
            _reply(tx, msg.rid, f"Unknown test id(s) {unknown[:5]}: use the ids exactly as run_check or the gate "
                                "reported them.", error=True)
            return
    if msg.kind == "test_conflict":
        not_pass = [c for c in checks if base.get(c) != PASS]
        if not_pass:
            _reply(tx, msg.rid, f"{not_pass[:5]} did not pass on the original code, so they are not counted as "
                                "regressions and need no report.", error=True)
            return
    dup = [r for r in tx.all("report") if r.verdict == "pending" and r.req_id == msg.req_id and r.kind == msg.kind
           and r.check_ids == checks]
    if dup:
        _reply(tx, msg.rid, f"Report {dup[0].id} with the same content is already under review.", error=True)
        return
    rep = Report(id=tx.next_id("P"), kind=msg.kind, work_id=msg.work_id, req_id=msg.req_id, check_ids=checks,
                 reason=msg.reason, rid=msg.rid, created_t=tx.now)
    tx.put(rep)
    tx.event("report", report=rep.id, kind=rep.kind, req=rep.req_id, checks=checks[:20])
    tx.act(CallReviewer(rep.id))


def on_review_done(tx: Tx, msg: ReviewDone, cfg) -> None:
    rep = tx.get("report", msg.report_id)
    if rep is None or rep.verdict != "pending":
        return
    req = tx.get("requirement", rep.req_id) if rep.req_id else None
    if msg.error:
        verdict, review = "rejected", f"the reviewer failed ({msg.error}); the report is not approved"
    elif not msg.approved:
        verdict, review = "rejected", msg.reason
    else:
        source = msg.failure_text if rep.kind == "environment" else (req.text if req else "")
        where = "the failure output" if rep.kind == "environment" else f"the text of {rep.req_id}"
        if not msg.quote or not quote_in(msg.quote, source):
            verdict = "rejected"
            review = (f"the reviewer approved, but its quote does not appear verbatim in {where}, so the runtime "
                      f"does not accept the approval (quote: {msg.quote[:200]!r})")
        else:
            verdict, review = "approved", msg.reason
    tx.update(rep, verdict=verdict, quote=msg.quote, review=review)
    tx.event("report_decided", report=rep.id, kind=rep.kind, verdict=verdict)
    if verdict == "approved":
        effect = {"test_conflict": f"The gate no longer counts {', '.join(rep.check_ids[:10])} as regressions, and "
                                   f"{rep.req_id} is recorded as WAIVED unless an independent test decides it.",
                  "insufficient_info": f"{rep.req_id} is recorded as UNKNOWN with your reason.",
                  "environment": f"The gate no longer counts {', '.join(rep.check_ids[:10])} as regressions."}[rep.kind]
        text = f"Report {rep.id} approved: {review}\nQuote: \"{msg.quote}\"\n{effect}"
    else:
        text = (f"Report {rep.id} rejected: {review}\nThe checks still count. Fix the code so they pass, or report "
                "again with a quote from the requirement that explicitly asks for the change.")
    _reply(tx, rep.rid, text)


HANDLERS = {
    Start: on_start, Tick: on_tick, Stop: on_stop, RunCheck: on_run_check, Wait: on_wait, LedgerQuery: on_ledger,
    JobFinished: on_job_finished, Submit: on_submit, Merged: on_merged, WorkerExited: on_worker_exited,
    RequestTest: on_request_test, AuthoredDraft: on_authored_draft, ReportConflict: on_report,
    ReviewDone: on_review_done,
}
