"""需求拆解：拆 → 检查 + 审 → 反馈重做；失败退回规则切分。"""
from __future__ import annotations

import asyncio
import json

from belay.llm import ScriptedLLM
from belay.runtime.planner import plan_requirements

TASK = """Implement the changes below.
<release_notes>
### Features
* Add a `sub(a, b)` function to `pkg.calc`
* `mul(a, b)` now returns `a * b + 1`
</release_notes>"""


def answer(obj) -> list[dict]:
    return [{"type": "text", "text": obj if isinstance(obj, str) else json.dumps(obj)}]


def split(*items) -> list[dict]:
    return answer({"requirements": [{"statement": s, "quotes": q, "kind": k} for s, q, k in items]})


OK = answer({"ok": True, "issues": []})
SUB = ("pkg.calc gets sub(a, b).", ["Add a `sub(a, b)` function to `pkg.calc`"], "new")
MUL = ("mul(a, b) returns a * b + 1.", ["`mul(a, b)` now returns `a * b + 1`"], "change")


def plan(script, **kw):
    llm = ScriptedLLM(script)
    records = []
    result = asyncio.run(plan_requirements(llm, TASK, record=records.append, log=lambda m: None, **kw))
    return result, llm, records


def test_clean_split_is_used_after_one_review():
    result, llm, records = plan([split(SUB, MUL), OK])
    assert result.source == "llm" and result.rounds == 0 and result.notes == []
    assert [(r.id, r.kind, r.text) for r in result.requirements] == [("R1", "new", SUB[0]), ("R2", "change", MUL[0])]
    assert [r["call"] for r in records] == ["split", "review"]


def test_runtime_check_and_reviewer_feedback_go_back_to_the_splitter():
    bad = ("mul changes.", ["mul now returns a*b+1"], "change")            # 引文不是逐字的
    script = [split(SUB, bad),
              answer({"ok": False, "issues": ["Requirement 1: the statement should name pkg.calc."]}),
              split(SUB, MUL), OK]
    result, llm, _ = plan(script)
    assert result.rounds == 1 and result.notes == []
    retry = llm.requests[2]["messages"][0]["content"]
    assert "<previous_answer>" in retry and "does not appear verbatim" in retry and "should name pkg.calc" in retry
    assert result.requirements[1].quotes == MUL[1]


def test_unresolved_issues_are_kept_as_notes_not_failures():
    script = [split(SUB), OK, split(SUB), OK, split(SUB), OK]             # 一直漏掉 mul 那一行
    result, _, _ = plan(script, rounds=2)
    assert result.source == "llm" and result.rounds == 2 and len(result.requirements) == 1
    assert any("not covered" in n for n in result.notes)


def test_unparseable_answers_fall_back_to_the_rule_split():
    result, _, _ = plan([answer("I think there are two requirements."), answer("still not json")])
    assert result.source == "rules" and [r.id for r in result.requirements] == ["R1", "R2"]
    assert "rule-based split" in result.notes[0]


def test_one_bad_answer_is_retried():
    result, llm, _ = plan([answer("oops"), split(SUB, MUL), OK])
    assert result.source == "llm" and len(result.requirements) == 2
    assert "could not be parsed" in llm.requests[1]["messages"][-1]["content"]


def test_review_can_be_turned_off():
    result, llm, _ = plan([split(SUB, MUL)], review=False)
    assert result.source == "llm" and len(llm.requests) == 1
