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


_ESCAPE = re.compile(r'\\(.)', re.S)


def _fix_escapes(s: str) -> str:
    """模型常把正则 / Windows 路径里的反斜杠原样写进 JSON 字符串：把不合法的转义改成字面反斜杠。"""
    return _ESCAPE.sub(lambda m: m.group(0) if m.group(1) in '"\\/bfnrtu' else "\\\\" + m.group(1), s)


def _loads_lenient(s: str):
    try:
        return json.loads(s)
    except ValueError:
        return json.loads(_fix_escapes(s))


def extract_json(text: str, key: Optional[str] = None) -> Optional[dict]:
    """从模型回复里取出 JSON 对象：先看代码块，再看整段首尾花括号，最后从每个 '{' 起逐个尝试解码。
    给了 key 时优先返回含这个键的对象（回复前面的说明文字里常有别的花括号）。"""
    text = text or ""
    candidates = [m.group(1) for m in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)]
    i, j = text.find("{"), text.rfind("}")
    if i >= 0 and j > i:
        candidates.append(text[i:j + 1])
    for c in candidates:
        try:
            v = _loads_lenient(c)
        except ValueError:
            continue
        if isinstance(v, dict):
            if key is None or key in v:
                return v
    dec = json.JSONDecoder()
    starts = [m.start() for m in re.finditer(r"\{", text)][:300]
    for s in starts:
        for fix in (False, True):
            src = _fix_escapes(text[s:]) if fix else text[s:]
            try:
                v, _ = dec.raw_decode(src)
            except ValueError:
                continue
            if isinstance(v, dict):
                if key is None or key in v:
                    return v
            break
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
