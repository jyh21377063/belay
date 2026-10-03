"""after_accept=improve：需求都做完之后，复核者提出改进项、判定改进项，worker 继续加强已交付的版本。

经由模拟器驱动（纯函数规则，每个事务之后检查不变量）。默认 finalize 的行为不变也在这里确认。
"""
from __future__ import annotations

from belay.core import rules as R
from belay.core.config import BelayConfig
from belay.core.context import build_context
from belay.core.invariants import check_log, llm_effects
from belay.core.model import IMP_DONE, IMP_DROPPED, IMP_OPEN, REQ_DONE
from belay.core.queries import improve_idle_sessions, improving, open_improvements
from belay.core.reduce import replay
from belay.core.render import ledger, ledger_markdown, render_board, render_submit
from tests.sim import MANUAL, Sim
from tests.unit.test_rules import ADD, BASE, MUL_QUOTE, PLAN, TASK

ALL_DONE = [{"id": "R1", "status": "done", "level": "E3", "tests": [ADD]},
            {"id": "R2", "status": "done", "level": "E1"},
            {"id": "R3", "status": "done", "level": "E1"}]
DOC_QUOTE = "Document the new behaviour in the module docstring"
RUN = [{"id": "X1", "cmd": "python bench.py", "rc": 0}]


def verdict(v, reqs=(), improvements=(), new=(), no_more=None, merge=True, score=None, **extra) -> dict:
    out = {"merge": merge if v.attempt is not None else False, "reason": "ok", "summary": f"change at s{v.snapshot}",
           "requirements": list(reqs), "waivers": [], "score": score, "score_note": "python bench.py",
           "feedback": extra.get("feedback", "")}
    if improvements:
        out["improvements"] = list(improvements)
    if new:
        out["new_improvements"] = list(new)
    if no_more is not None:
        out["no_more_improvements"] = no_more
    return out


class Lead:
    """复核者脚本：按触发（submit / improve / 后台）给出不同的结论；calls 记下每次复核的触发。"""

    def __init__(self, **by_trigger):
        self.by = by_trigger
        self.calls: list[str] = []

    def __call__(self, sim, v):
        self.calls.append(v.trigger)
        fn = self.by.get(v.trigger) or self.by.get("default")
        return fn(sim, v) if fn else verdict(v)


def cfg(**kw) -> BelayConfig:
    kw.setdefault("background", "off")
    kw.setdefault("after_accept", "improve")
    return BelayConfig(**kw)


def sim(reviewer, **kw) -> Sim:
    s = Sim(BASE, cfg=cfg(**kw), reviewer=reviewer)
    s.setup(TASK, PLAN)
    s.do(R.start_session, "w1", "first", {})
    return s


def finish_all(s: Sim, tree: str = "t1") -> str:
    s.world.define(tree, {ADD: "PASSED"})
    return s.submit(tree, summary="all done")


def imp(s: Sim) -> dict:
    return {i.id: (i.status, i.level) for i in s.g.improvements.values()}


# ======================================================================== 默认 finalize 不变

def test_finalize_mode_is_unchanged():
    lead = Lead(submit=lambda s, v: verdict(v, ALL_DONE, new=[{"title": "x", "why": "y", "quote": DOC_QUOTE}]))
    s = Sim(BASE, cfg=BelayConfig(background="off"), reviewer=lead)
    s.setup(TASK, PLAN)
    s.do(R.start_session, "w1", "first", {})
    sid = finish_all(s)
    assert s.g.submits[sid].status == "accepted" and lead.calls == ["submit"]
    assert not s.g.improvements and not s.g.run.improving
    assert "you can stop" in render_submit(s.g, sid)
    d = s.g.reviews["V1"].decision
    assert d["improvements"] is None and d["improved"] is False
    s.do(R.end_session, "w1", "submitted")
    assert R.next_step(s.g, "w1", s.now, s.cfg) == ("finalize", "complete")


def test_improve_needs_a_reviewer():
    assert not BelayConfig(after_accept="improve", reviewer=False).improve
    s = Sim(BASE, cfg=BelayConfig(background="off", after_accept="improve", reviewer=False))
    s.setup(TASK, PLAN)
    s.do(R.start_session, "w1", "first", {})
    sid = finish_all(s)                                        # 没有复核者：按自述记下，接受后收尾
    assert s.g.submits[sid].status == "accepted" and not s.g.run.improving
    s.do(R.end_session, "w1", "submitted")
    assert R.next_step(s.g, "w1", s.now, s.cfg) == ("finalize", "complete")


# ======================================================================== 接受之后：改进阶段开始

def test_accept_starts_the_phase_and_the_reviewer_is_asked_for_directions():
    lead = Lead(submit=lambda s, v: verdict(v, ALL_DONE),
                improve=lambda s, v: verdict(v, new=[{"title": "Cover negative zero in mul", "why": "edge case",
                                                      "quote": MUL_QUOTE},
                                                     {"title": "Document add's overflow", "why": "the docs ask",
                                                      "quote": DOC_QUOTE}]))
    s = sim(lead)
    sid = finish_all(s)
    g = s.g
    assert lead.calls == ["submit", "improve"]                  # 合并复核没提改进项：再请复核者在链头上提
    assert g.run.improving and g.submits[sid].status == "accepted"
    assert imp(s) == {"I1": (IMP_OPEN, None), "I2": (IMP_OPEN, None)}
    assert g.improvements["I1"].quote == MUL_QUOTE and g.improvements["I1"].review == "V2"
    v2 = g.reviews["V2"]
    assert v2.attempt is None and v2.checkpoint == g.head and v2.submit == sid and v2.focus == ()
    text = render_submit(g, sid)
    assert "accepted" in text and "does not stop here" in text and "I1 [open] Cover negative zero" in text
    assert "you can stop" not in text
    assert improving(g, s.cfg)
    s.do(R.end_session, "w1", "submitted")
    g = s.g
    assert R.next_step(g, "w1", s.now, s.cfg) == ("start_session", "restart")   # 不收尾
    ctx = build_context(g, "w1", 24000, s.now, s.cfg, mode="resume")
    assert "Improvement phase: in progress" in ctx.text and "I2 [open] Document add's overflow" in ctx.text
    assert "continues to improve the delivered version" in ctx.text
    assert "Improvement phase: in progress" in render_board(g, "w1", s.now, s.cfg)
    assert not check_log(s.log) and not llm_effects(s.log) and replay(s.log) == g


def test_directions_given_with_the_accepting_review_need_no_extra_review():
    lead = Lead(submit=lambda s, v: verdict(v, ALL_DONE, new=[{"title": "Handle huge ints", "why": "w",
                                                               "quote": MUL_QUOTE}]))
    s = sim(lead)
    sid = finish_all(s)
    assert lead.calls == ["submit"] and s.g.submits[sid].status == "accepted"
    assert list(s.g.improvements) == ["I1"] and s.g.improvements["I1"].proposed_checkpoint == 1


def test_proposals_are_ignored_while_requirements_are_open():
    lead = Lead(submit=lambda s, v: verdict(v, ALL_DONE[:2], new=[{"title": "t", "why": "w", "quote": MUL_QUOTE}]))
    s = sim(lead)
    sid = finish_all(s)
    assert s.g.submits[sid].status == "returned" and not s.g.improvements and not s.g.run.improving
    assert any("only once every requirement" in n for n in s.g.reviews["V1"].decision["notes"])


def test_proposals_must_be_tied_to_the_task_text_or_a_measured_score_and_are_capped():
    new = [{"title": "Speed it up", "why": "w", "objective": True},              # 没有测过分数：挂不上
           {"title": "Something nice", "why": "w", "quote": "make it nicer overall please"},   # 不是原文
           {"title": "Mul negatives tests", "why": "w", "quote": MUL_QUOTE},
           {"title": "  mul NEGATIVES tests ", "why": "dup", "quote": MUL_QUOTE},            # 重复
           {"title": "Docs A", "why": "w", "quote": DOC_QUOTE},
           {"title": "Docs B", "why": "w", "quote": DOC_QUOTE}]
    lead = Lead(submit=lambda s, v: verdict(v, ALL_DONE, new=new))
    s = sim(lead, improve_max_open=2)
    finish_all(s)
    assert [i.title for i in s.g.improvements.values()] == ["Mul negatives tests", "Docs A"]
    notes = s.g.reviews["V1"].decision["notes"]
    assert sum("tied neither" in n for n in notes) == 2
    assert any("already on the list" in n for n in notes) and any("at most 2" in n for n in notes)


def test_objective_items_need_a_measured_score():
    lead = Lead(submit=lambda s, v: verdict(v, ALL_DONE, score=0.6,
                                            new=[{"title": "Speed it up", "why": "w", "objective": True}]))
    s = sim(lead)
    finish_all(s)
    i = s.g.improvements["I1"]
    assert i.objective and not i.quote and s.g.checkpoints[1].score == 0.6


# ======================================================================== 判定改进项、进展

def _phase(s_kw=None, **lead_kw):
    lead = Lead(submit=lambda s, v: verdict(v, ALL_DONE, score=lead_kw.get("score0"),
                                            new=[{"title": "Mul edge cases", "why": "w", "quote": MUL_QUOTE},
                                                 {"title": "Doc examples", "why": "w", "quote": DOC_QUOTE}]))
    s = sim(lead, **(s_kw or {}))
    sid = finish_all(s)
    assert s.g.submits[sid].status == "accepted" and len(s.g.improvements) == 2
    return s, lead


def test_done_needs_evidence_and_counts_as_progress():
    s, lead = _phase()
    s.do(R.end_session, "w1", "submitted")
    s.do(R.start_session, "w1", "restart", {})                 # 改进阶段开始之后的第一个会话
    lead.by["submit"] = lambda s, v: verdict(v, improvements=[
        {"id": "I1", "status": "done", "level": "E0"},                                   # 只有自述：partial
        {"id": "I2", "status": "done", "level": "E2", "runs": ["X1"], "evidence": ["ran the example"]}])
    s.world.define("t2", {ADD: "PASSED"})
    sid = s.submit("t2")
    g = s.g
    assert imp(s) == {"I1": (IMP_OPEN, None), "I2": (IMP_DONE, "E2")}
    assert g.improvements["I1"].judgement == "partial" and g.improvements["I2"].checkpoint == 2
    assert g.sessions["S2"].progress                           # 改进项完成算进展
    text = render_submit(g, sid)
    assert "I2: done (E2)" in text and "I1 [open, judged partial]" in text
    assert g.submits[sid].status == "accepted" and improving(g, s.cfg)


def test_with_a_measured_score_done_needs_a_command():
    s, lead = _phase(score0=0.5)
    lead.by["submit"] = lambda s, v: verdict(v, score=0.5, improvements=[{"id": "I1", "status": "done", "level": "E1"}])
    s.world.define("t2", {ADD: "PASSED"})
    s.submit("t2")
    assert imp(s)["I1"] == (IMP_OPEN, None)
    assert any("needs E2 or better" in n for n in s.g.reviews["V2"].decision["notes"])


def test_a_higher_score_counts_as_progress_only_in_improve_mode():
    s, lead = _phase(score0=0.5)
    s.do(R.end_session, "w1", "submitted")
    s.do(R.start_session, "w1", "restart", {})
    lead.by["submit"] = lambda s, v: verdict(v, score=0.505)  # 在容差（2%）以内：不算
    s.world.define("t2", {ADD: "PASSED"})
    s.submit("t2")
    assert s.g.reviews["V2"].decision["improved"] is False and not s.g.sessions["S2"].progress
    lead.by["submit"] = lambda s, v: verdict(v, score=0.6)
    s.world.define("t3", {ADD: "PASSED"})
    s.submit("t3")
    assert s.g.reviews["V3"].decision["improved"] is True and s.g.sessions["S2"].progress
    s.do(R.end_session, "w1", "done")
    assert improve_idle_sessions(s.g, "w1") == 0


def test_idle_sessions_after_the_start_end_the_phase():
    s, lead = _phase()
    s.do(R.end_session, "w1", "submitted")                     # 开始改进时的那个会话不算
    assert improve_idle_sessions(s.g, "w1") == 0
    assert R.next_step(s.g, "w1", s.now, s.cfg) == ("start_session", "restart")
    for k in range(2):
        s.do(R.start_session, "w1", "restart", {})
        s.do(R.end_session, "w1", "done")
        assert improve_idle_sessions(s.g, "w1") == k + 1
    assert R.next_step(s.g, "w1", s.now, s.cfg) == ("finalize", "improve_idle")


def test_drop_then_no_more_improvements_closes_the_phase():
    s, lead = _phase()
    lead.by["submit"] = lambda s, v: verdict(v, no_more="nothing left")            # 还有 open 的：不算
    s.world.define("t2", {ADD: "PASSED"})
    sid = s.submit("t2")
    assert not s.g.run.improve_closed and s.g.submits[sid].status == "accepted"
    assert any("I1, I2 are still open" in n for n in s.g.reviews["V2"].decision["notes"])
    lead.by["submit"] = lambda s, v: verdict(v, improvements=[
        {"id": "I1", "status": "dropped", "reason": "mul already covers it"},
        {"id": "I2", "status": "done", "level": "E1"}], no_more="the docs and mul are complete")
    s.world.define("t3", {ADD: "PASSED"})
    sid = s.submit("t3")
    g = s.g
    assert imp(s) == {"I1": (IMP_DROPPED, None), "I2": (IMP_DONE, "E1")}
    assert g.run.improve_closed == "the docs and mul are complete" and not improving(g, s.cfg)
    assert g.submits[sid].status == "accepted" and "you can stop" in render_submit(g, sid)
    s.do(R.end_session, "w1", "submitted")
    assert R.next_step(s.g, "w1", s.now, s.cfg) == ("finalize", "complete")
    s.do(R.begin_finalize, "complete")
    assert s.do(R.deliver, "complete") == "DONE"               # 改进项不影响 DONE
    L = ledger(s.g)
    assert L["improve"]["closed"] and [i["status"] for i in L["improve"]["items"]] == ["dropped", "done"]
    assert "## Improvement phase" in ledger_markdown(s.g)
    s.check_log()
    assert replay(s.log) == s.g


def test_a_closing_claim_needs_the_score_measured_when_the_task_has_one():
    s, lead = _phase(score0=0.5)
    lead.by["submit"] = lambda s, v: verdict(v, improvements=[
        {"id": "I1", "status": "dropped", "reason": "r"}, {"id": "I2", "status": "dropped", "reason": "r"}],
        no_more="done")
    lead.by["improve"] = lambda s, v: verdict(v, score=0.5, new=[{"title": "Faster mul", "why": "w",
                                                                  "objective": True}])
    s.world.define("t2", {ADD: "PASSED"})
    sid = s.submit("t2")
    assert any("measure it in this review" in n for n in s.g.reviews["V2"].decision["notes"])
    # 两项都放弃了、宣布无效：没有 open 的改进项，于是请复核者再提方向
    assert lead.calls[-1] == "improve" and not s.g.run.improve_closed
    assert imp(s)["I3"] == (IMP_OPEN, None) and s.g.submits[sid].status == "accepted"


def test_no_usable_direction_after_a_retry_closes_the_phase():
    lead = Lead(submit=lambda s, v: verdict(v, ALL_DONE),
                improve=lambda s, v: verdict(v, new=[{"title": "Rewrite in rust", "why": "fast",
                                                      "quote": "nothing like this is in the task"}]))
    s = sim(lead)
    sid = finish_all(s)
    assert lead.calls == ["submit", "improve", "improve"]       # 重试一次（review_retries=1）
    g = s.g
    assert g.reviews["V3"].retry_of == "V2"
    assert g.run.improving and "no improvement tied" in g.run.improve_closed
    assert g.submits[sid].status == "accepted" and "you can stop" in render_submit(g, sid)


def test_an_improve_review_without_any_answer_counts_as_failed():
    lead = Lead(submit=lambda s, v: verdict(v, ALL_DONE), improve=lambda s, v: verdict(v))
    s = sim(lead, review_retries=0)
    sid = finish_all(s)
    g = s.g
    assert g.reviews["V2"].status == "failed" and "could not propose" in g.run.improve_closed
    assert g.submits[sid].status == "accepted"


def test_finalizing_cancels_the_improve_review_and_accepts_the_submit():
    lead = Lead(submit=lambda s, v: verdict(v, ALL_DONE), improve=MANUAL)
    s = sim(lead)
    sid = finish_all(s)
    assert s.g.submits[sid].status == "pending" and s.running_review() == "V2"
    s.do(R.begin_finalize, "deadline")
    assert s.g.reviews["V2"].status == "cancelled" and s.g.submits[sid].status == "accepted"
    s.do(R.deliver, "deadline")
    s.check_log()


def test_background_reviews_judge_and_propose_during_the_phase():
    s, lead = _phase(s_kw={"background": "latest", "merge_min_interval_sec": 0, "merge_todo_interval_sec": 0})
    lead.by["default"] = lambda s, v: verdict(v, improvements=[{"id": "I1", "status": "done", "level": "E1"}],
                                              new=[{"title": "Doc the edge cases", "why": "w", "quote": DOC_QUOTE}])
    s.world.define("t2", {ADD: "PASSED"})
    s.snap("t2", reason="handoff")
    g = s.g
    assert lead.calls[-1] == "handoff" and imp(s) == {"I1": (IMP_DONE, "E1"), "I2": (IMP_OPEN, None),
                                                       "I3": (IMP_OPEN, None)}
    assert len(open_improvements(g)) == 2


def test_a_rejected_merge_records_no_improvement():
    s, lead = _phase()
    lead.by["submit"] = lambda s, v: verdict(v, merge=False, improvements=[{"id": "I1", "status": "done",
                                                                             "level": "E1"}],
                                             new=[{"title": "More docs", "why": "w", "quote": DOC_QUOTE}])
    s.world.define("t2", {ADD: "PASSED"})
    sid = s.submit("t2")
    assert s.g.submits[sid].status == "rejected" and imp(s) == {"I1": (IMP_OPEN, None), "I2": (IMP_OPEN, None)}
    assert R.next_step(s.g, "w1", s.now, s.cfg) == ("resume_session", "S1")    # 改进阶段里被拒：接着干


def test_rollback_reopens_improvements_done_on_abandoned_merge_points():
    s, lead = _phase()
    lead.by["submit"] = lambda s, v: verdict(v, improvements=[{"id": "I1", "status": "done", "level": "E1"}])
    s.world.define("t2", {ADD: "PASSED"})
    s.submit("t2")
    assert imp(s)["I1"] == (IMP_DONE, "E1") and s.g.head == 2
    s.do(R.rollback, "w1", 1)
    i = s.g.improvements["I1"]
    assert i.status == IMP_OPEN and i.reason == "rolled_back" and i.checkpoint == 1
    assert all(r.status == REQ_DONE for r in s.g.requirements.values() if r.kind == "actionable")
    s.check_log()
