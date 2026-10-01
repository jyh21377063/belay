"""规划器：开工时把任务原文拆成需求清单（actionable / context，可带已有测试作为检查项）。

LLM 的产出只是提议，每一轮都记一条 plan_proposed（含校验报告）；校验不通过就把问题交回去重做，
几轮之后仍不通过则退回机械切分（每个实质行一条需求，覆盖由构造保证）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from belay.core.events import LLM, RULE
from belay.core.plan import mechanical_plan, renumber, validate_plan
from belay.runtime.prompts import PLANNER_RETRY, PLANNER_SYSTEM


@dataclass
class PlanRound:
    proposal: dict
    valid: bool
    problems: list[str]
    warnings: list[str] = field(default_factory=list)
    source: str = LLM


@dataclass
class PlanOutcome:
    rounds: list[PlanRound]
    requirements: list[dict]
    source: str                      # llm | rule（机械切分）


def extract_json(text: str) -> Optional[dict]:
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = [m.group(1)] if m else []
    i, j = text.find("{"), text.rfind("}")
    if i >= 0 and j > i:
        candidates.append(text[i:j + 1])
    for c in candidates:
        try:
            v = json.loads(c)
            if isinstance(v, dict):
                return v
        except ValueError:
            continue
    return None


def _user_message(task_text: str, test_files: list[str], checks: list[str]) -> str:
    parts = [f"<task>\n{task_text.strip()}\n</task>"]
    if checks:
        shown = checks[:400]
        parts.append("Existing tests (node ids you may use in checks):\n" + "\n".join(shown)
                     + (f"\n... and {len(checks) - len(shown)} more" if len(checks) > len(shown) else ""))
    elif test_files:
        parts.append("Existing test files (checks must be pytest node ids inside them, e.g. "
                     "tests/test_x.py::test_name):\n" + "\n".join(test_files[:200]))
    else:
        parts.append("No existing tests are known; leave checks empty.")
    return "\n\n".join(parts)


async def plan(llm, task_text: str, known_checks: list[str], rounds: int = 3,
               log: Callable[[str], None] = lambda m: None, test_files: Optional[list[str]] = None) -> PlanOutcome:
    """known_checks 为空时（基线与规划并行）只给规划器测试文件列表；检查的链接在基线之后再校验一次。"""
    history: list[PlanRound] = []
    if llm is not None:
        files = sorted(test_files or {c.split("::")[0] for c in known_checks})
        messages = [{"role": "user", "content": _user_message(task_text, files, sorted(known_checks))}]
        for r in range(rounds):
            try:
                resp = await llm.call(PLANNER_SYSTEM, [], messages)
            except Exception as e:                        # 规划器不可用：退回机械切分
                log(f"planner call failed: {type(e).__name__}: {e}")
                break
            proposal = extract_json(resp.text) or {}
            rep = validate_plan(task_text, proposal, known_checks or _mentioned_checks(proposal))
            history.append(PlanRound(proposal, rep.ok, rep.problems if proposal else ["reply is not a JSON object"],
                                     rep.warnings))
            if rep.ok:
                return PlanOutcome(history, renumber(rep), LLM)
            messages += [{"role": "assistant", "content": resp.content or [{"type": "text", "text": resp.text}]},
                         {"role": "user", "content": PLANNER_RETRY.format(problems="\n".join(
                             f"- {p}" for p in history[-1].problems[:40]))}]
    proposal = mechanical_plan(task_text)
    rep = validate_plan(task_text, proposal, known_checks)
    history.append(PlanRound(proposal, rep.ok, rep.problems, rep.warnings, RULE))
    if not rep.ok:                                        # 机械切分按构造覆盖；到这里说明原文本身异常
        log(f"mechanical plan problems: {rep.problems[:5]}")
    reqs = renumber(rep) if rep.ok else proposal["requirements"]
    return PlanOutcome(history, reqs, RULE)


def _mentioned_checks(proposal: dict) -> list[str]:
    """基线还没有时暂时接受提议里的检查名（基线之后由 driver 再校验）。"""
    out = []
    for r in proposal.get("requirements") or [] if isinstance(proposal, dict) else []:
        if isinstance(r, dict):
            out += [str(c) for c in (r.get("checks") or [])]
    return out
