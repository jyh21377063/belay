"""规划的规则部分：校验规划器（LLM）的提议，以及不调模型的机械切分兜底。

规划器只产出需求清单（没有任务层）：每条需求带一句验收方法（复核者怎么确认它做完了，只是描述）。入图之前必须满足：
  - 每条需求的引文逐字出现在任务原文里（只把连续空白视为相同）；
  - 任务原文的每个实质单元（非标题的行或句子）都被某条引文覆盖；标题、套话、背景可以标成 context，
    只用来覆盖原文，不进入清单；
  - 至少有一条 actionable 需求；
  - 需求的检查必须真实存在（不存在的检查被丢弃并记录，不算失败）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable


_WS = re.compile(r"\s+")
_WORD = re.compile(r"[A-Za-z0-9_一-鿿]")
_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,15}$")
COVER_FRAC = 0.6          # 一个单元的实质字符至少这么多被引文覆盖才算覆盖
MIN_UNIT_WORDS = 3


def normalize_ws(s: str) -> str:
    return _WS.sub(" ", s).strip()


def quote_in_text(quote: str, text: str) -> bool:
    q = normalize_ws(quote)
    return bool(q) and q in normalize_ws(text)


# ---------------------------------------------------------------- 覆盖

def content_units(text: str) -> list[str]:
    """任务原文的实质单元：非空、非标题、非代码围栏的行；过长的行按句子再切。"""
    out = []
    in_fence = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not line or line.startswith("#"):
            continue
        if len(line.split()) < MIN_UNIT_WORDS and len(_WORD.findall(line)) < 12:
            continue
        if len(line) > 400:
            parts = [p.strip() for p in re.split(r"(?<=[.!?。！？])\s+", line) if p.strip()]
            out.extend(p for p in parts if len(p.split()) >= MIN_UNIT_WORDS or len(_WORD.findall(p)) >= 12)
        else:
            out.append(line)
    return out


def _covered_mask(text_n: str, quotes: Iterable[str]) -> list[bool]:
    mask = [False] * len(text_n)
    for q in quotes:
        qn = normalize_ws(q)
        if not qn:
            continue
        start = 0
        while True:
            i = text_n.find(qn, start)
            if i < 0:
                break
            for k in range(i, i + len(qn)):
                mask[k] = True
            start = i + 1
    return mask


def uncovered_units(text: str, quotes: Iterable[str]) -> list[str]:
    text_n = normalize_ws(text)
    mask = _covered_mask(text_n, list(quotes))
    out = []
    pos = 0
    for unit in content_units(text):
        un = normalize_ws(unit)
        i = text_n.find(un, pos)
        if i < 0:
            i = text_n.find(un)
        if i < 0:
            out.append(unit)
            continue
        pos = i + len(un)
        idx = [k for k in range(i, i + len(un)) if _WORD.match(text_n[k])]
        if not idx:
            continue
        if sum(mask[k] for k in idx) / len(idx) < COVER_FRAC:
            out.append(unit)
    return out


# ---------------------------------------------------------------- 校验

@dataclass
class PlanReport:
    ok: bool
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    requirements: list[dict] = field(default_factory=list)


KINDS = ("actionable", "context")


def validate_plan(task_text: str, proposal: dict, known_checks: Iterable[str] = ()) -> PlanReport:
    rep = PlanReport(ok=False)
    known = set(known_checks)
    reqs_in = proposal.get("requirements") if isinstance(proposal, dict) else None
    if not isinstance(reqs_in, list) or not reqs_in:
        rep.problems.append("the proposal has no requirements list")
        return rep

    reqs: dict[str, dict] = {}
    for i, r in enumerate(reqs_in):
        if not isinstance(r, dict):
            rep.problems.append(f"requirement #{i + 1} is not an object")
            continue
        rid, quote = str(r.get("id") or "").strip(), str(r.get("quote") or "")
        if not _ID.match(rid):
            rep.problems.append(f"requirement #{i + 1} has an invalid id {rid!r}")
            continue
        if rid in reqs:
            rep.problems.append(f"duplicate requirement id {rid}")
            continue
        if not quote.strip():
            rep.problems.append(f"{rid}: empty quote")
            continue
        if not quote_in_text(quote, task_text):
            rep.problems.append(f"{rid}: the quote is not verbatim in the task text: {quote[:120]!r}")
            continue
        kind = str(r.get("kind") or "actionable").strip().lower()
        if kind not in KINDS:
            rep.problems.append(f"{rid}: kind must be actionable or context (got {kind!r})")
            continue
        checks = [str(c) for c in (r.get("checks") or [])] if kind == "actionable" else []
        dropped = [c for c in checks if c not in known]
        if dropped:
            rep.warnings.append(f"{rid}: dropped unknown checks {dropped[:5]}")
        reqs[rid] = {"id": rid, "quote": normalize_ws(quote), "summary": str(r.get("summary") or "")[:300],
                     "kind": kind, "checks": [c for c in checks if c in known],
                     "acceptance": str(r.get("acceptance") or "").strip()[:500] if kind == "actionable" else ""}

    missing = uncovered_units(task_text, [r["quote"] for r in reqs.values()])
    for u in missing[:30]:
        rep.problems.append(f"not covered by any requirement quote: {u[:160]!r}")
    if len(missing) > 30:
        rep.problems.append(f"... and {len(missing) - 30} more uncovered lines")
    if reqs and not any(r["kind"] == "actionable" for r in reqs.values()):
        rep.problems.append("no requirement is actionable: mark the changes the task asks for as actionable")

    rep.requirements = list(reqs.values())
    rep.ok = not rep.problems
    return rep


def renumber(report: PlanReport) -> list[dict]:
    """把提议里的 id 规范化为 R1..（保持相对顺序）。"""
    return [{**r, "id": f"R{i + 1}"} for i, r in enumerate(report.requirements)]


def mechanical_plan(task_text: str) -> dict:
    """不调模型的兜底：每个实质单元一条 actionable 需求。覆盖由构造保证。"""
    reqs = []
    for i, unit in enumerate(content_units(task_text)):
        reqs.append({"id": f"R{i + 1}", "quote": unit, "summary": unit[:160], "kind": "actionable"})
    if not reqs:
        text = normalize_ws(task_text)[:2000] or "(empty task)"
        reqs = [{"id": "R1", "quote": text, "summary": text[:160], "kind": "actionable"}]
    return {"requirements": reqs}
