"""after_accept=polish：需求都做完后换新会话进入 POLISH（IMPROVE / VERIFY）；打转时换新会话；复核者的复现命令。

经由模拟器驱动（纯函数规则，每个事务之后检查不变量）。会话的结束与开场理由在这里按规则层的约定模拟：
port 在 submit 的回复之后结束会话（phase / stuck_handoff），驱动随后按 next_step 开新会话。
"""
from __future__ import annotations

from types import SimpleNamespace

from belay.core import rules as R
from belay.core.config import BelayConfig
from belay.core.context import VERIFY_LINE, build_context
from belay.core.invariants import check_log, llm_effects
from belay.core.model import REQ_DONE, REQ_OPEN
from belay.core.queries import (improvement_items, improving, remaining_sec, reserve_sec, sessions_without_progress,
                                verifying)
from belay.core.reduce import replay
from belay.core.render import render_submit
from tests.sim import Sim
from tests.unit.test_improve import ALL_DONE, Lead, verdict
from tests.unit.test_rules import ADD, BASE, MUL, MUL_QUOTE, PLAN, TASK

GAP_RUNS = [{"id": "X1", "cmd": "python -c 'import pkg.mod'", "rc": 0, "tail": "ok"},
            {"id": "X2", "cmd": "python -c 'import pkg.mod as m; print(m.__doc__)'", "rc": 1,
             "tail": "Traceback ...\nAssertionError: the module docstring does not describe add"}]
R3_GAP = {"id": "R3", "status": "not_done", "level": "E2", "runs": ["X2"],
          "missing": ["the docstring does not describe add: python -c 'import pkg.mod as m; print(m.__doc__)'"]}


def cfg(**kw) -> BelayConfig:
    kw.setdefault("background", "off")
    kw.setdefault("after_accept", "polish")
    return BelayConfig(**kw)


def sim(reviewer, runs=None, **kw) -> Sim:
    s = Sim(BASE, cfg=cfg(**kw), reviewer=reviewer, runs=runs)
    s.setup(TASK, PLAN)
    s.do(R.start_session, "w1", "first", {})
    return s


def submit(s: Sim, tree: str, **kw) -> str:
    s.world.define(tree, {ADD: "PASSED"})
    return s.submit(tree, summary="done", **kw)


def new_session(s: Sim, end: str) -> str:
    s.do(R.end_session, "w1", end)
    action, reason = R.next_step(s.g, "w1", s.now, s.cfg)
    assert action == "start_session", (action, reason)
    s.do(R.start_session, "w1", reason, {})
    return reason


def assert_log(s: Sim) -> None:
    assert not check_log(s.log) and not llm_effects(s.log) and replay(s.log) == s.g


# ======================================================================== 配置

def test_polish_config():
    c = BelayConfig(after_accept="polish")
    assert c.improve and c.polish
    assert not BelayConfig(after_accept="polish", reviewer=False).improve
    assert BelayConfig(after_accept="improve").improve and not BelayConfig(after_accept="improve").polish
    for bad in ({"after_accept": "x"}, {"polish_mode": "x"}, {"verify_rounds": 0}):
        try:
            BelayConfig.from_dict(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


# ======================================================================== VERIFY：复审

def test_verify_audit_reopens_a_gap_and_the_new_session_fixes_it():
    lead = Lead(submit=lambda s, v: verdict(v, ALL_DONE),
                verify=lambda s, v: verdict(v, [{"id": "R2", "status": "done", "level": "E2", "runs": ["X1"]},
                                               R3_GAP], feedback="R3: the docstring is missing"))
    s = sim(lead, runs=GAP_RUNS)
    sid = submit(s, "t1")
    g = s.g
    assert lead.calls == ["submit", "verify"]
    assert g.run.improving and g.run.polish_mode == "verify" and verifying(g, s.cfg)
    assert not improvement_items(g, s.cfg)
    audit = g.reviews["V2"]
    assert audit.trigger == "verify" and audit.attempt is None and audit.focus == ("R2", "R3")   # E1 在前，E3 不复审
    assert g.submits[sid].status == "returned" and g.submits[sid].open == ("R3",)
    r3 = g.requirements["R3"]
    assert r3.status == REQ_OPEN and r3.reason == "reassessed"
    assert g.requirements["R2"].level == "E2"                                   # 证据升级
    text = render_submit(g, sid)
    assert "R3: not done" in text and "a check run by the reviewer shows it does not work" in text
    assert "[V2 X2] exit code 1" in text and "AssertionError: the module docstring" in text
    # 换新会话进入 POLISH
    assert new_session(s, "phase") == "phase"
    ctx = build_context(s.g, "w1", 24000, s.now, s.cfg, mode="resume", reason="phase")
    assert ctx.text.startswith("You are starting a new session: every requirement on the checklist has been accepted")
    assert VERIFY_LINE in ctx.text and "Improvement phase" not in ctx.text
    # 只读代码又判完成：被退回过的需求要 E2 / E3
    lead.by["submit"] = lambda s, v: verdict(v, [{"id": "R3", "status": "done", "level": "E1"}])
    sid2 = submit(s, "t2")
    assert s.g.submits[sid2].status == "returned" and s.g.requirements["R3"].status == REQ_OPEN
    assert any("needs E2 or E3" in n for n in s.g.reviews["V3"].decision["notes"])
    # 跑过了：完成；第二轮复审什么都没退回 → POLISH 结束、接受、收尾
    lead.by["submit"] = lambda s, v: verdict(v, [{"id": "R3", "status": "done", "level": "E2", "runs": ["X2"]}])
    lead.by["verify"] = lambda s, v: verdict(v, [{"id": "R3", "status": "done", "level": "E2", "runs": ["X1"]}])
    sid3 = submit(s, "t3")
    g = s.g
    assert lead.calls[-2:] == ["submit", "verify"]
    assert g.submits[sid3].status == "accepted" and g.run.improve_closed.startswith("the audit found no gap")
    assert not improving(g, s.cfg)
    s.do(R.end_session, "w1", "submitted")
    assert R.next_step(s.g, "w1", s.now, s.cfg) == ("finalize", "complete")
    assert_log(s)


def test_verify_audit_without_a_gap_ends_the_run():
    lead = Lead(submit=lambda s, v: verdict(v, ALL_DONE),
                verify=lambda s, v: verdict(v, [{"id": "R2", "status": "done", "level": "E2", "runs": ["X1"]},
                                               {"id": "R3", "status": "not_done", "level": "E1",
                                                "missing": ["I doubt the docstring"]}]))
    s = sim(lead, runs=GAP_RUNS)
    sid = submit(s, "t1")
    g = s.g
    assert g.submits[sid].status == "accepted"                     # 只读代码的怀疑不退回需求
    assert g.requirements["R3"].status == REQ_DONE and g.run.improve_closed
    assert any("stays done" in n for n in g.reviews["V2"].decision["notes"])
    s.do(R.end_session, "w1", "submitted")
    assert R.next_step(s.g, "w1", s.now, s.cfg) == ("finalize", "complete")
    assert_log(s)


def test_verify_rounds_are_capped_and_a_failing_auditor_is_retried_then_skipped():
    lead = Lead(submit=lambda s, v: verdict(v, ALL_DONE), verify=lambda s, v: verdict(v, [R3_GAP]))
    s = sim(lead, runs=GAP_RUNS, verify_rounds=1)
    submit(s, "t1")
    assert s.g.requirements["R3"].status == REQ_OPEN
    lead.by["submit"] = lambda s, v: verdict(v, [{"id": "R3", "status": "done", "level": "E2", "runs": ["X2"]}])
    sid = submit(s, "t2")
    assert s.g.submits[sid].status == "accepted" and "the audit ran 1 time" in s.g.run.improve_closed
    assert lead.calls.count("verify") == 1
    # 复核者给不出复审结论：重试一次，仍不行就结束 POLISH
    lead2 = Lead(submit=lambda s, v: verdict(v, ALL_DONE), verify=lambda s, v: {"merge": False, "requirements": []})
    s2 = sim(lead2)
    sid2 = submit(s2, "t1")
    assert lead2.calls == ["submit", "verify", "verify"]
    assert s2.g.submits[sid2].status == "accepted" and "could not audit" in s2.g.run.improve_closed
    assert_log(s2)


def test_nothing_to_audit_when_every_done_requirement_has_tests():
    plan = {"requirements": [PLAN["requirements"][0]]}
    lead = Lead(submit=lambda s, v: verdict(v, [ALL_DONE[0]]))
    s = Sim(BASE, cfg=cfg(), reviewer=lead)
    s.setup("Fix the add function so that it returns the sum.", plan)
    s.do(R.start_session, "w1", "first", {})
    sid = submit(s, "t1")
    assert s.g.submits[sid].status == "accepted" and "nothing to audit" in s.g.run.improve_closed
    assert lead.calls == ["submit"]


def test_no_polish_when_too_little_time_is_left():
    lead = Lead(submit=lambda s, v: verdict(v, ALL_DONE))
    s = sim(lead)
    left = remaining_sec(s.g, s.now) - reserve_sec(s.g, s.cfg)
    s.advance(left - s.cfg.new_session_min_sec + 60)
    sid = submit(s, "t1")
    assert s.g.submits[sid].status == "accepted" and not s.g.run.improving and lead.calls == ["submit"]
    s.do(R.end_session, "w1", "submitted")
    assert R.next_step(s.g, "w1", s.now, s.cfg) == ("finalize", "complete")


# ======================================================================== IMPROVE：链上测过分数

def test_auto_mode_picks_improve_when_a_score_was_measured():
    proposals = [{"title": "Cover negative zero in mul", "why": "edge case", "quote": MUL_QUOTE}]
    lead = Lead(submit=lambda s, v: verdict(v, ALL_DONE, score=0.8, new=proposals),
                improve=lambda s, v: verdict(v, new=proposals, score=0.8))
    s = sim(lead, runs=GAP_RUNS)
    sid = submit(s, "t1")
    g = s.g
    assert g.run.polish_mode == "improve" and improvement_items(g, s.cfg)
    # 合并复核（POLISH 开始之前）提的改进项被忽略：统一由 POLISH 开始时那次专门的复核提出
    assert lead.calls == ["submit", "improve"]
    assert g.reviews["V1"].decision["improvements"] is None
    assert list(g.improvements) == ["I1"] and g.improvements["I1"].review == "V2"
    assert g.submits[sid].status == "accepted" and improving(g, s.cfg)
    assert new_session(s, "phase") == "phase"
    ctx = build_context(s.g, "w1", 24000, s.now, s.cfg, mode="resume", reason="phase")
    assert "I1 [open] Cover negative zero" in ctx.text and "continues to improve the delivered version" in ctx.text
    assert "time" not in ctx.text.split("## Finishing")[1]
    assert_log(s)


def test_forced_verify_mode_ignores_the_score():
    lead = Lead(submit=lambda s, v: verdict(v, ALL_DONE, score=0.8), verify=lambda s, v: verdict(v, ALL_DONE[1:]))
    s = sim(lead, runs=GAP_RUNS, polish_mode="verify")
    submit(s, "t1")
    assert s.g.run.polish_mode == "verify" and lead.calls == ["submit", "verify"]


def test_legacy_improve_mode_is_unchanged_by_polish():
    lead = Lead(submit=lambda s, v: verdict(v, ALL_DONE, new=[{"title": "x", "why": "y", "quote": MUL_QUOTE}]))
    s = Sim(BASE, cfg=BelayConfig(background="off", after_accept="improve"), reviewer=lead)
    s.setup(TASK, PLAN)
    s.do(R.start_session, "w1", "first", {})
    submit(s, "t1")
    assert lead.calls == ["submit"] and list(s.g.improvements) == ["I1"] and s.g.run.polish_mode == "improve"


# ======================================================================== 打转换人

def stuck_lead(missing="the docstring still does not describe add"):
    first = verdict
    return Lead(submit=lambda s, v: first(v, ALL_DONE[:2] + [{"id": "R3", "status": "not_done", "level": "E2",
                                                               "runs": ["X2"], "missing": [missing]}]))


def test_the_same_requirement_failing_on_submits_hints_then_hands_off_once():
    s = sim(stuck_lead(), runs=GAP_RUNS, after_accept="finalize")
    sid1 = submit(s, "t1")
    assert s.g.submits[sid1].status == "returned" and not s.g.stalls
    sid2 = submit(s, "t2")
    st = s.g.stalls[-1]
    assert st.kind == "requirement_misses" and st.action == "hint" and st.sig == "req:R3"
    assert "R3 was judged not done on 2 submits in a row" in st.detail
    assert R.handoff_requested(s.g, "w1", s.g.submits[sid2].seq) is None
    sid3 = submit(s, "t3")
    st = R.handoff_requested(s.g, "w1", s.g.submits[sid3].seq)
    assert st is not None and st.kind == "requirement_misses" and st.sig == "req:R3"
    # 新会话：开场说明为什么换、复核者的复现命令、受阻的出路；换出去的会话不计入连续无进展
    assert new_session(s, "stuck_handoff") == "fresh"
    ctx = build_context(s.g, "w1", 24000, s.now, s.cfg, mode="resume", reason="fresh")
    assert ctx.text.startswith("You are taking over from a previous session")
    assert "## Why a new session" in ctx.text and "R3 was judged not done on 3 submits" in ctx.text
    assert "exit code 1" in ctx.text and "submit(blocked=[{requirement: \"R3\"" in ctx.text
    # 新会话里同一个问题：只提醒，不再换
    submit(s, "t4")
    assert s.g.stalls[-1].action == "hint"
    sid5 = submit(s, "t5")
    assert R.handoff_requested(s.g, "w1", s.g.submits[sid5].seq) is None
    assert_log(s)


def test_no_handoff_when_disabled_or_without_enough_time():
    s = sim(stuck_lead(), runs=GAP_RUNS, after_accept="finalize", stuck_handoff=False)
    for t in ("t1", "t2", "t3", "t4"):
        submit(s, t)
    assert R.handoff_requested(s.g, "w1", 0) is None and s.g.stalls[-1].action == "hint"
    s = sim(stuck_lead(), runs=GAP_RUNS, after_accept="finalize")
    submit(s, "t1")
    submit(s, "t2")
    s.advance(remaining_sec(s.g, s.now) - reserve_sec(s.g, s.cfg) - s.cfg.new_session_min_sec + 60)
    submit(s, "t3")
    assert R.handoff_requested(s.g, "w1", 0) is None


def test_progress_after_the_hint_blocks_the_handoff():
    s = sim(Lead(submit=lambda s, v: verdict(v, [ALL_DONE[0], {"id": "R3", "status": "not_done", "level": "E2",
                                                                "runs": ["X2"], "missing": ["x"]}])),
            runs=GAP_RUNS, after_accept="finalize")
    submit(s, "t1")
    submit(s, "t2")
    assert s.g.stalls[-1].action == "hint"
    s.reviewer.by["submit"] = lambda s, v: verdict(v, [{"id": "R2", "status": "done", "level": "E1"},
                                                       {"id": "R3", "status": "not_done", "level": "E2",
                                                        "runs": ["X2"], "missing": ["x"]}])
    sid = submit(s, "t3")                                             # R2 在这次完成：提醒之后有进展
    assert s.g.requirements["R2"].status == REQ_DONE
    assert R.handoff_requested(s.g, "w1", s.g.submits[sid].seq) is None


def test_resubmitting_without_changes_counts_as_another_miss():
    s = sim(stuck_lead(), runs=GAP_RUNS, after_accept="finalize")
    submit(s, "t1")
    head_tree = s.g.head_cp.tree
    sid = s.submit(head_tree, summary="done, really")                # 什么都没改又提交
    assert s.g.submits[sid].attempt is None and s.g.submits[sid].status == "returned"
    assert R.submit_miss_streak(s.g, "w1", "R3") == 2 and s.g.stalls[-1].sig == "req:R3"


def test_the_same_regression_rejecting_submits_hints_then_hands_off():
    s = sim(Lead(), after_accept="finalize")
    for i in range(3):
        s.world.define(f"b{i}", {ADD: "PASSED", MUL: "FAILED"})
        s.submit(f"b{i}")
    st = [x for x in s.g.stalls if x.sig.startswith("reg:")]
    assert len(st) == 1 and st[0].kind == "repeated_failure" and st[0].action == "hint"
    s.world.define("b3", {ADD: "PASSED", MUL: "FAILED"})
    sid = s.submit("b3")
    h = R.handoff_requested(s.g, "w1", s.g.submits[sid].seq)
    assert h is not None and h.kind == "repeated_failure" and h.sig == st[0].sig
    s.tick()                                                          # tick 的同类提醒不重复
    assert len([x for x in s.g.stalls if x.kind == "repeated_failure" and x.action == "hint"]) == 1
    assert_log(s)


def test_stuck_sessions_do_not_count_as_idle():
    s = sim(Lead(), after_accept="finalize")
    s.do(R.end_session, "w1", "stuck_handoff")
    s.do(R.start_session, "w1", "fresh", {})
    s.do(R.end_session, "w1", "done")
    assert sessions_without_progress(s.g, "w1") == 1
    assert R.next_step(s.g, "w1", s.now, s.cfg)[0] == "start_session"
    s.do(R.start_session, "w1", "restart", {})
    s.do(R.end_session, "w1", "done")
    assert R.next_step(s.g, "w1", s.now, s.cfg) == ("finalize", "no_progress")


# ======================================================================== 复核者命令的输出尾部

def test_only_cited_or_failing_run_tails_are_kept():
    runs = [{"id": f"X{i}", "cmd": f"c{i}", "rc": 0 if i % 2 else 1, "tail": "y" * 3000} for i in range(1, 21)]
    out = R._clean_runs(runs, {"requirements": [{"id": "R1", "runs": ["X1", "X3"]}]})
    kept = [x["id"] for x in out if "tail" in x]
    assert len(kept) == R.RUN_TAILS_KEPT and "X1" in kept and "X3" in kept
    assert all(len(x["tail"]) == R.RUN_TAIL_CHARS for x in out if "tail" in x)
    assert all(x["rc"] == 1 for x in out if "tail" in x and x["id"] not in ("X1", "X3"))
    assert len(out) == 20 and all({"id", "cmd", "rc"} <= set(x) for x in out)


def test_reviewer_history_shows_earlier_judgements_of_an_open_requirement():
    from belay.runtime.reviewer import Reviewer
    s = sim(stuck_lead(), runs=GAP_RUNS, after_accept="finalize", stuck_handoff=False)
    for t in ("t1", "t2", "t3"):
        submit(s, t)
    run = SimpleNamespace(rt=SimpleNamespace(graph=s.g), cfg=s.cfg)
    hist = Reviewer(run)._history("R3", "V-none")
    assert len(hist) == 3 and hist[0].startswith("V1 (s") and "not done; missing: the docstring" in hist[0]
    assert Reviewer(SimpleNamespace(rt=run.rt, cfg=s.cfg.with_(review_history=2)))._history("R3", "V3") == hist[:2]
