"""需求拆解：一个模型拆，一个模型审，runtime 做两项客观检查；都在 worker 开始之前完成，结果冻结。

  1. 拆解模型（SPLITTER_SYSTEM）输出需求草稿：自由表述 statement + 逐字原文 quotes + kind
  2. 检查：runtime 的 check_drafts（引文逐字存在、原文每行被覆盖）+ 审阅模型（SPLIT_REVIEW_SYSTEM，粒度、
     表述、类型、遗漏）
  3. 有问题 → 把两边的意见一起交回拆解模型重做，最多 rounds 轮；仍有问题也照常使用，问题记进 notes
  4. 模型调用失败或答案无法解析 → 退回规则切分（extract_requirements），并记录原因

所有检查都不会让运行失败。需求定义了"什么算完成"，所以在 worker 开始前冻结，之后不可改（不变量 1）。
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field

from belay.graph.model import Requirement
from belay.graph.requirements import (Draft, check_drafts, drafts_to_requirements, extract_requirements,
                                      parse_drafts, parse_json_object)
from belay.runtime.prompts import SPLIT_REVIEW_SYSTEM, SPLITTER_SYSTEM, split_review_message, splitter_message


@dataclass
class PlanResult:
    requirements: list[Requirement]
    source: str                                     # llm | rules
    rounds: int = 0
    notes: list[str] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps({"source": self.source, "rounds": self.rounds, "notes": self.notes,
                           "requirements": [asdict(r) for r in self.requirements], "history": self.history},
                          ensure_ascii=False, indent=1)


def _dump(drafts: list[Draft]) -> str:
    return json.dumps({"requirements": [asdict(d) for d in drafts]}, ensure_ascii=False, indent=1)


async def _ask(llm, system: str, content: str, parse, record, tag: str, retries: int = 1):
    messages = [{"role": "user", "content": content}]
    for attempt in range(retries + 1):
        resp = await llm.call(system, [], messages)
        if record:
            record({"t": time.time(), "call": tag, "attempt": attempt, "answer": resp.text[:20000]})
        try:
            return parse(resp.text)
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as e:
            messages = messages + [{"role": "assistant", "content": resp.content or [{"type": "text", "text": "."}]},
                                   {"role": "user", "content": f"Your answer could not be parsed ({e}). Reply with "
                                                               "the JSON object only."}]
    raise ValueError(f"unparseable answer from {tag}")


def _parse_review(text: str) -> tuple[bool, list[str]]:
    data = parse_json_object(text)
    issues = [str(i) for i in (data.get("issues") or []) if str(i).strip()]
    return bool(data.get("ok")) and not issues, issues


async def plan_requirements(llm, instruction: str, *, rounds: int = 2, review: bool = True, record=None,
                            log=print) -> PlanResult:
    history: list[dict] = []
    try:
        drafts = await _ask(llm, SPLITTER_SYSTEM, splitter_message(instruction), parse_drafts, record, "split")
        used = 0
        review_issues: list[str] = []
        for r in range(rounds + 1):
            chk = check_drafts(instruction, drafts)
            issues = chk.feedback()
            review_issues: list[str] = []
            if review:
                try:
                    ok, review_issues = await _ask(llm, SPLIT_REVIEW_SYSTEM,
                                                   split_review_message(instruction, _dump(drafts)),
                                                   _parse_review, record, "review")
                except ValueError as e:
                    review_issues = []
                    history.append({"round": r, "review_error": str(e)})
            history.append({"round": r, "requirements": len(drafts), "check_issues": issues,
                            "review_issues": review_issues})
            if (not issues and not review_issues) or r == rounds:
                break
            used = r + 1
            drafts = await _ask(llm, SPLITTER_SYSTEM,
                                splitter_message(instruction, _dump(drafts), issues + review_issues),
                                parse_drafts, record, f"split-{r + 1}")
        final = check_drafts(instruction, drafts)
        notes = [f"requirement split: {f}" for f in final.feedback()]
        notes += [f"requirement split review: {i}" for i in review_issues]   # 最后一轮仍未解决的审阅意见
        reqs = drafts_to_requirements(instruction, drafts)
        log(f"[belay] 需求拆解：{len(reqs)} 条，{used} 轮修改，剩余问题 {len(notes)} 个")
        return PlanResult(reqs, "llm", used, notes, history)
    except Exception as e:                           # noqa: BLE001 — 拆解失败不影响运行：退回规则切分
        reqs = extract_requirements(instruction)
        note = f"LLM requirement split failed ({type(e).__name__}: {str(e)[:200]}); used the rule-based split"
        log(f"[belay] {note}")
        return PlanResult(reqs, "rules", 0, [note], history)
