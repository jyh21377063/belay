"""规划器：LLM 提议 → 规则校验 → 把问题交回重做 → 仍不通过则机械切分。"""
from __future__ import annotations

import asyncio
import json

from belay.llm import ScriptedLLM
from belay.runtime.planner import plan

TASK = "Fix add so that add(1, 2) returns 3.\nAdd a sub function that returns a minus b."
GOOD = {"requirements": [{"id": "A", "kind": "actionable", "quote": "Fix add so that add(1, 2) returns 3.",
                          "summary": "add", "checks": ["tests/t.py::test_add"]},
                         {"id": "B", "kind": "actionable", "quote": "Add a sub function that returns a minus b.",
                          "summary": "sub"}]}


def say(t):
    return [{"type": "text", "text": t}]


def test_planner_retries_with_validator_feedback():
    bad = {"requirements": [{"id": "A", "quote": "Fix add please", "summary": "add"}]}
    llm = ScriptedLLM([say(json.dumps(bad)), say("```json\n" + json.dumps(GOOD) + "\n```")])
    out = asyncio.run(plan(llm, TASK, known_checks=["tests/t.py::test_add"]))
    assert out.source == "llm" and len(out.rounds) == 2 and not out.rounds[0].valid
    assert "not verbatim" in llm.requests[1]["messages"][-1]["content"]
    assert [r["id"] for r in out.requirements] == ["R1", "R2"]
    assert out.requirements[0]["checks"] == ["tests/t.py::test_add"]


def test_planner_falls_back_to_mechanical_split():
    llm = ScriptedLLM([say("no json"), say("{}"), say("still nothing")])
    out = asyncio.run(plan(llm, TASK, known_checks=[], rounds=3))
    assert out.source == "rule" and len(out.rounds) == 4
    assert len(out.requirements) == 2 and all(r["kind"] == "actionable" for r in out.requirements)
    out = asyncio.run(plan(None, TASK, known_checks=[]))
    assert out.source == "rule"
