"""decide() 的场景测试：纯函数，不起进程、不调模型。每一步之后都检查五条不变量。"""
from __future__ import annotations

from belay.config import RuntimeConfig
from belay.graph.build import initial_state
from belay.graph.invariants import violations
from belay.graph.ledger import statuses
from belay.graph.model import FAIL, MERGED, PASS, RUNNING, Requirement
from belay.runtime.decide import decide
from belay.runtime.messages import (Advance, AuthoredDraft, CallReviewer, CallTestAuthor, CancelJob,
                                    FinalizeWorkspace, Finish, JobFinished, LedgerQuery, Merged, Notify, Reply,
                                    ReportConflict, ReviewDone, RunCheck, Start, StartJob, StartWorker,
                                    StopWorker, Submit, Tick, Wait, WorkerExited)

T0 = 1_000_000.0
W = "tests/test_widget.py"
O = "tests/test_other.py"
BASELINE = {f"{W}::test_1": PASS, f"{W}::test_2": PASS, f"{O}::test_3": FAIL, f"{O}::test_4": PASS}


class Driver:
    def __init__(self, cfg: RuntimeConfig | None = None, test_author: bool = True, reserve: float = 300):
        reqs = [Requirement("R1", "Add a `widget` option to `render`", "Features", "new", 1),
                Requirement("R2", "The default widget size changes from 10 to 12", "Changes", "change", 2)]
        self.state = initial_state(requirements=reqs, baseline=BASELINE, now=T0, deadline_t=T0 + 3600,
                                   reserve_sec=reserve, full_gate_sec=100, base_commit="c0", base_tree="t0",
                                   workspace="/repo", gate_available=True, test_author_available=test_author,
                                   protect_tests=True, test_files=[W, O])
        self.cfg = cfg or RuntimeConfig()
        self.now = T0
        self.log: list = []

    def send(self, msg_cls, *args, dt: float = 1.0, **kw) -> list:
        self.now += dt
        msg = msg_cls(self.now, *args, **kw)
        changes, actions = decide(self.state, msg, self.cfg)
        self.state.apply(changes)
        bad = violations(self.state, self.cfg.gate)
        assert not bad, bad
        self.log += actions
        return actions

    def job(self, actions) -> str:
        starts = [a.job_id for a in actions if isinstance(a, StartJob)]
        assert len(starts) == 1, actions
        return starts[0]


def replies(actions) -> dict[str, Reply]:
    return {a.rid: a for a in actions if isinstance(a, Reply)}


def of(actions, cls) -> list:
    return [a for a in actions if isinstance(a, cls)]


def test_start_dispatches_root_worker():
    d = Driver()
    assert of(d.send(Start), StartWorker)[0].work_id == "W1"


def test_run_check_wait_cache_and_baseline_classification():
    d = Driver()
    acts = d.send(RunCheck, "q1", "W1", "t1", ["pkg/widget.py"])
    job = d.job(acts)
    assert "wait" in replies(acts)["q1"].text and d.state.job[job].selection == [W]
    assert d.send(Wait, "q2", "W1", [job]) == []                      # 挂起
    acts = d.send(JobFinished, job, "DONE", {"status": "ok", "tests": {f"{W}::test_1": "PASSED",
                                                                        f"{W}::test_2": "FAILED"},
                                             "reasons": {f"{W}::test_2": "AssertionError: 12 != 10"}})
    text = replies(acts)["q2"].text
    assert "REGRESSIONS" in text and f"{W}::test_2" in text and "12 != 10" in text
    acts = d.send(RunCheck, "q3", "W1", "t1", ["pkg/widget.py"])      # 同一个树：直接返回缓存
    assert not of(acts, StartJob) and "Cached" in replies(acts)["q3"].text
    assert d.send(JobFinished, job, "DONE", {"status": "ok", "tests": {}}) == []   # 重复的完成消息被忽略
    acts = d.send(RunCheck, "q4", "W1", "t1", ["pkg/unrelated_thing.py"])
    assert replies(acts)["q4"].error                                    # 没有相关测试：提示给参数


def test_parallel_limit_queues_dev_jobs():
    d = Driver(RuntimeConfig(max_parallel_jobs=1))
    j1 = d.job(d.send(RunCheck, "a", "W1", "t1", [], tests=[W]))
    acts = d.send(RunCheck, "b", "W1", "t1", [], tests=[O])
    assert not of(acts, StartJob) and "queued" in replies(acts)["b"].text
    acts = d.send(JobFinished, j1, "DONE", {"status": "ok", "tests": {f"{W}::test_1": "PASSED"}})
    assert len(of(acts, StartJob)) == 1                                  # 排队的作业接着启动


def submit(d: Driver, rid: str, tree: str, final: bool, changed=("pkg/widget.py",), dropped=()) -> list:
    return d.send(Submit, rid, "W1", f"summary {rid}", final, commit="c" + tree[1:], tree=tree,
                  changed=list(changed), changed_since_base=list(changed), dropped_tests=list(dropped))


def gate_ok(d: Driver, job: str, fail: tuple = (), failing_checks: tuple = ()) -> list:
    tests = {t: ("FAILED" if t in fail else "PASSED") for t in BASELINE if t.startswith(W) or d.state.job[job].level
             == "full"}
    tests[f"{O}::test_3"] = "FAILED"
    reasons = {t: "AssertionError: boom" for t in fail}
    for c in d.state.authored():
        for n in c.nodes:
            tests[n] = "FAILED" if c.id in failing_checks else "PASSED"
            if c.id in failing_checks:
                reasons[n] = "assert render() == 'w'"
    return d.send(JobFinished, job, "DONE", {"status": "ok", "tests": tests, "reasons": reasons})


def accept(d: Driver, cid: str) -> list:
    """Test Author 交卷 → 在原始代码上以断言失败 → 收录。"""
    f = d.state.check[cid].selector
    val = d.job(d.send(AuthoredDraft, cid, True, stored_at=f"/opt/belay/checks/{cid}/x.py",
                       content=f"def test_{cid.lower()}(): ..."))
    return d.send(JobFinished, val, "DONE", {"status": "ok", "tests": {f"{f}::test_x": "FAILED"},
                                             "reasons": {f"{f}::test_x": "assert None == 'w'"}})


def test_checkpoint_rejected_then_merged_then_final_finishes():
    d = Driver(test_author=False)
    dev = d.job(d.send(RunCheck, "q", "W1", "t1", ["pkg/widget.py"]))
    acts = submit(d, "s1", "t1", final=False, dropped=["tests/test_widget.py"])
    assert of(acts, CancelJob)[0].job_id == dev                          # 门禁原地运行：先让出工作区
    gate = d.job(acts)
    assert d.state.job[gate].level == "related" and d.state.job[gate].selection == [W]
    assert d.state.work["W1"].state == "VERIFYING"
    acts = gate_ok(d, gate, fail=(f"{W}::test_2",))
    r = replies(acts)["s1"]
    assert "rejected" in r.text and f"{W}::test_2" in r.text and not r.finished
    assert d.state.work["W1"].state == RUNNING and d.state.work["W1"].rejections == 1
    assert d.state.run.head_commit == "c0"                               # 集成分支没动

    gate = d.job(submit(d, "s2", "t2", final=False))
    acts = gate_ok(d, gate)
    adv = of(acts, Advance)[0]
    assert (adv.expected, adv.new) == ("c0", "c2")
    acts = d.send(Merged, adv.candidate_id, True)
    assert "Merged as integration commit #1" in replies(acts)["s2"].text
    assert d.state.run.head_commit == "c2" and d.state.work["W1"].state == RUNNING

    gate = d.job(submit(d, "s3", "t3", final=True))
    assert d.state.job[gate].level == "full"
    adv = of(gate_ok(d, gate), Advance)[0]
    acts = d.send(Merged, adv.candidate_id, True)
    r = replies(acts)["s3"]
    assert r.finished and of(acts, Finish)[0].status == "DONE"
    assert d.state.work["W1"].state == MERGED and d.state.run.phase == "finished"
    assert [i.commit for i in d.state.integration_chain()] == ["c2", "c3"]


def test_start_queues_one_acceptance_test_per_requirement():
    d = Driver(RuntimeConfig(test_author_parallel=1))
    acts = d.send(Start)
    assert [a.check_id for a in of(acts, CallTestAuthor)] == ["T1"]          # 并发 1：先写 R1 的
    assert [(c.id, c.req_id, c.status) for c in d.state.authored(active_only=False)] == [
        ("T1", "R1", "pending"), ("T2", "R2", "queued")]
    acts = accept(d, "T1")
    assert "ready" in of(acts, Notify)[0].text and 'ledger(requirement="R1")' in of(acts, Notify)[0].text
    assert [a.check_id for a in of(acts, CallTestAuthor)] == ["T2"]          # 写完一个，排下一个
    assert [s.status for s in statuses(d.state)] == ["FAILED", "OPEN"]       # 集成分支还是原始代码：R1 的测试失败


def test_acceptance_test_runs_in_every_gate_and_supports_the_requirement():
    d = Driver()
    d.send(Start)
    accept(d, "T1")
    f = d.state.check["T1"].selector
    gate = d.job(submit(d, "s1", "t1", final=False))
    assert f in d.state.job[gate].selection and f in d.state.job[gate].overlay
    acts = gate_ok(d, gate, failing_checks=("T1",))                           # 验收测试失败不挡检查点
    acts = d.send(Merged, of(acts, Advance)[0].candidate_id, True)
    assert "Merged" in replies(acts)["s1"].text and "Acceptance test T1 (R1): FAILS" in replies(acts)["s1"].text
    assert d.state.check["T1"].last_failure
    gate = d.job(submit(d, "s2", "t2", final=False))
    d.send(Merged, of(gate_ok(d, gate), Advance)[0].candidate_id, True)
    assert [s.status for s in statuses(d.state)][0] == "SUPPORTED"


def test_acceptance_test_retried_then_rejected_does_not_block():
    d = Driver(RuntimeConfig(test_author_parallel=1))
    d.send(Start)
    f = d.state.check["T1"].selector
    bad = {"status": "ok", "tests": {f"{f}::t": "FAILED"}, "reasons": {f"{f}::t": "AttributeError: no widget"}}
    val = d.job(d.send(AuthoredDraft, "T1", True, stored_at="/s/x.py", content="c"))
    acts = d.send(JobFinished, val, "DONE", bad)
    assert of(acts, CallTestAuthor)[0].feedback                           # 退回重写一次，附上原因
    val = d.job(d.send(AuthoredDraft, "T1", True, stored_at="/s/x.py", content="c"))
    acts = d.send(JobFinished, val, "DONE", bad)
    assert "No valid acceptance test" in of(acts, Notify)[0].text and d.state.check["T1"].status == "rejected"
    assert of(acts, CallTestAuthor)[0].check_id == "T2"
    assert [s.status for s in statuses(d.state)][0] == "UNKNOWN"


def test_final_submission_waits_for_acceptance_tests_and_bounces_until_they_pass():
    d = Driver()
    d.send(Start)                                                          # T1、T2 在写
    acts = submit(d, "s1", "t1", final=True)
    assert not of(acts, StartJob) and not replies(acts)                    # 等验收测试写完，worker 在 submit 上等
    assert d.state.candidate["C1"].waiting_tests
    assert not of(accept(d, "T1"), StartJob)
    assert d.state.candidate["C1"].waiting_tests                           # T2 还在写
    acts = d.send(AuthoredDraft, "T2", False, error="NOT TESTABLE")         # T2 写不出：不挡完成
    gate = [a.job_id for a in of(acts, StartJob) if d.state.job[a.job_id].purpose == "gate"][0]
    assert d.state.job[gate].level == "full" and d.state.check["T1"].selector in d.state.job[gate].overlay
    acts = d.send(Merged, of(gate_ok(d, gate, failing_checks=("T1",)), Advance)[0].candidate_id, True)
    r = replies(acts)["s1"]
    assert not r.finished and "Not finished" in r.text and "R1" in r.text and "wrong_test" in r.text
    assert d.state.run.head_commit == "c1" and d.state.work["W1"].state == RUNNING   # 进度已经存档
    assert d.state.work["W1"].final_bounces == 1
    gate = d.job(submit(d, "s2", "t2", final=True))
    acts = d.send(Merged, of(gate_ok(d, gate), Advance)[0].candidate_id, True)
    assert replies(acts)["s2"].finished and of(acts, Finish)[0].status == "DONE"
    assert [s.status for s in statuses(d.state)] == ["SUPPORTED", "UNKNOWN"]


def test_wrong_test_appeal_withdraws_the_acceptance_test():
    d = Driver(RuntimeConfig(test_author_parallel=1))
    d.send(Start)
    accept(d, "T1")
    d.send(AuthoredDraft, "T2", False, error="NOT TESTABLE")
    assert replies(d.send(ReportConflict, "p0", "W1", "wrong_test", "R2", [], "x"))["p0"].error   # R2 没有验收测试
    acts = d.send(ReportConflict, "p1", "W1", "wrong_test", "R1", [], "the test expects a string, the task says option")
    call = of(acts, CallReviewer)[0]
    assert d.state.report[call.report_id].check_ids == ["T1"]
    acts = d.send(ReviewDone, call.report_id, True, quote="Add a `widget` option to `render`", reason="ok")
    assert "withdrawn" in replies(acts)["p1"].text and d.state.check["T1"].status == "withdrawn"
    assert [s.status for s in statuses(d.state)][0] == "UNKNOWN"
    gate = d.job(submit(d, "s1", "t1", final=True))
    assert d.state.job[gate].overlay == {}                                  # 作废的测试不再运行
    acts = d.send(Merged, of(gate_ok(d, gate), Advance)[0].candidate_id, True)
    assert of(acts, Finish)[0].status == "DONE"


def test_deadline_releases_a_final_submission_waiting_for_tests():
    d = Driver()
    d.send(Start)
    accept(d, "T1")
    submit(d, "s1", "t1", final=True)
    assert d.state.candidate["C1"].waiting_tests
    d.now = T0 + 3600 - 250
    acts = d.send(Tick)
    assert of(acts, StopWorker) and not d.state.candidate["C1"].waiting_tests
    gate = [a.job_id for a in of(acts, StartJob)][0]
    acts = d.send(Merged, of(gate_ok(d, gate), Advance)[0].candidate_id, True)
    assert of(acts, Finish)[0].status == "INCOMPLETE"                      # T2 没写完，不算完成
    assert d.state.run.head_commit == "c1"


def test_ledger_shows_a_requirement_with_its_acceptance_test():
    d = Driver()
    d.send(Start)
    accept(d, "T1")
    text = replies(d.send(LedgerQuery, "l", "W1", "R1"))["l"].text
    assert "Acceptance test T1: active" in text and "def test_t1" in text and "Add a `widget` option" in text
    assert "Unknown requirement" in replies(d.send(LedgerQuery, "m", "W1", "R9"))["m"].text


def test_deadline_stops_worker_and_finalizes_workspace():
    d = Driver(test_author=False)
    d.now = T0 + 3600 - 250                                                # 已进入预留时间
    acts = d.send(Tick)
    assert of(acts, StopWorker) and d.state.run.phase == "stopping"
    acts = d.send(WorkerExited, "W1", "cancelled")
    assert of(acts, FinalizeWorkspace)[0].reason == "deadline"
    acts = d.send(Submit, None, "W1", "", True, commit="c9", tree="t9", changed=["pkg/widget.py"],
                  changed_since_base=["pkg/widget.py"], by_runtime="deadline")
    gate = d.job(acts)
    assert d.state.job[gate].level == "full"                              # 预留时间够跑全量
    acts = d.send(Merged, of(gate_ok(d, gate), Advance)[0].candidate_id, True)
    assert of(acts, Finish)[0].status == "INCOMPLETE" and d.state.run.head_commit == "c9"


def test_deadline_with_rejected_final_candidate_delivers_previous_head():
    d = Driver(test_author=False)
    d.now = T0 + 3600 - 250
    d.send(Tick)
    d.send(WorkerExited, "W1", "cancelled")
    gate = d.job(d.send(Submit, None, "W1", "", True, commit="c9", tree="t9", changed=["pkg/widget.py"],
                        changed_since_base=["pkg/widget.py"], by_runtime="deadline"))
    acts = gate_ok(d, gate, fail=(f"{W}::test_1",))
    assert of(acts, Finish)[0].status == "INCOMPLETE" and d.state.run.head_commit == "c0"


def test_budget_exhausted_finishes_and_cancels_jobs():
    d = Driver()
    job = d.job(d.send(RunCheck, "q", "W1", "t1", [], full=True))
    d.now = T0 + 3601
    acts = d.send(Tick)
    assert of(acts, Finish)[0].status == "INCOMPLETE" and of(acts, CancelJob)[0].job_id == job
    acts = d.send(RunCheck, "late", "W1", "t2", [], full=True)
    assert replies(acts)["late"].finished


def test_report_needs_verbatim_quote_and_waives_the_test():
    d = Driver()
    t2 = f"{W}::test_2"
    acts = d.send(ReportConflict, "p1", "W1", "test_conflict", "R2", [t2], "R2 changes the default size")
    rep = of(acts, CallReviewer)[0].report_id
    acts = d.send(ReviewDone, rep, True, quote="the default size is now twelve", reason="ok")
    assert "rejected" in replies(acts)["p1"].text                         # 引文不在原文里：runtime 不接受
    acts = d.send(ReportConflict, "p2", "W1", "test_conflict", "R2", [t2], "R2 changes the default size")
    rep = of(acts, CallReviewer)[0].report_id
    acts = d.send(ReviewDone, rep, True, quote="default widget size changes from 10 to 12", reason="explicit")
    assert "approved" in replies(acts)["p2"].text
    gate = d.job(submit(d, "s1", "t1", final=False))
    acts = gate_ok(d, gate, fail=(t2,))                                   # test_2 失败但已豁免：照常合并
    assert of(acts, Advance)
    d.send(Merged, of(acts, Advance)[0].candidate_id, True)
    assert [s.status for s in statuses(d.state)][1] == "WAIVED"


def test_report_validation():
    d = Driver()
    assert replies(d.send(ReportConflict, "a", "W1", "test_conflict", "R9", [f"{W}::test_1"], "x"))["a"].error
    assert replies(d.send(ReportConflict, "b", "W1", "test_conflict", "R1", ["nope::x"], "x"))["b"].error
    assert replies(d.send(ReportConflict, "c", "W1", "test_conflict", "R1", [f"{O}::test_3"], "x"))["c"].error
    assert replies(d.send(ReportConflict, "d", "W1", "bogus", "R1", [], "x"))["d"].error
    assert of(d.send(ReportConflict, "e", "W1", "insufficient_info", "R1", [], "x"), CallReviewer)


def test_advise_mode_merges_with_regressions():
    d = Driver(RuntimeConfig(gate="advise"), test_author=False)
    gate = d.job(submit(d, "s1", "t1", final=False))
    acts = gate_ok(d, gate, fail=(f"{W}::test_1",))
    acts = d.send(Merged, of(acts, Advance)[0].candidate_id, True)
    assert "advisory" in replies(acts)["s1"].text.lower()


def test_gate_off_merges_without_tests():
    d = Driver(RuntimeConfig(gate="off"), test_author=False)
    acts = submit(d, "s1", "t1", final=False)
    assert not of(acts, StartJob) and of(acts, Advance)


def test_worker_exit_without_submit_is_checked_like_a_final_submission():
    d = Driver(test_author=False)
    acts = d.send(WorkerExited, "W1", "no_tool_call")
    assert of(acts, FinalizeWorkspace)[0].reason == "worker_exit"
    gate = d.job(d.send(Submit, None, "W1", "", True, commit="c5", tree="t5", changed=["pkg/widget.py"],
                        changed_since_base=["pkg/widget.py"], by_runtime="worker_exit"))
    acts = d.send(Merged, of(gate_ok(d, gate), Advance)[0].candidate_id, True)
    assert of(acts, Finish)[0].status == "DONE"


def test_ledger_reply_lists_requirements():
    d = Driver()
    text = replies(d.send(LedgerQuery, "l", "W1"))["l"].text
    assert "R1 [new] OPEN" in text and "Pre-existing failures" in text and f"{O}::test_3" in text


def test_resubmitting_a_rejected_tree_reuses_the_gate_result():
    d = Driver(test_author=False)
    gate = d.job(submit(d, "s1", "t1", final=True))
    gate_ok(d, gate, fail=(f"{W}::test_1",))
    acts = submit(d, "s2", "t1", final=True)                              # 同一个树再次提交
    assert not of(acts, StartJob) and "rejected" in replies(acts)["s2"].text
    assert d.state.candidate["C2"].verdict == "rejected"                   # 裁决落在新候选上，而不是旧的


def test_final_submit_without_changes_still_runs_the_full_gate_on_head():
    d = Driver(test_author=False)
    gate = d.job(submit(d, "s1", "t1", final=False))                      # 检查点只过了相关子集
    d.send(Merged, of(gate_ok(d, gate), Advance)[0].candidate_id, True)
    acts = submit(d, "s2", "t1", final=True)                               # 没有新改动的最终提交
    gate = d.job(acts)
    assert d.state.job[gate].level == "full" and d.state.job[gate].tree == "t1"
    acts = gate_ok(d, gate)
    assert not of(acts, Advance)                                           # HEAD 不动，只是复核
    assert of(acts, Finish)[0].status == "DONE" and d.state.candidate["C2"].verdict == "unchanged"
    assert len(d.state.integration) == 1
