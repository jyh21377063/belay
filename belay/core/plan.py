"""规划的规则部分：校验规划器（LLM）的提议，以及不调模型的机械切分兜底。

规划器的产出只是提议。入图之前必须满足：
  - 每条需求的引文逐字出现在任务原文里（只把连续空白视为相同）；
  - 任务原文的每个实质单元（非标题的行或句子）都被某条引文覆盖；
  - 每条需求至少被一个任务链接；链接、依赖都指向存在的对象；依赖无环；
  - 任务链接的检查必须真实存在（不存在的检查被丢弃并记录，不算失败）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from belay.core.queries import has_cycle, num

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
    tasks: list[dict] = field(default_factory=list)      # 已按依赖拓扑排序


def validate_plan(task_text: str, proposal: dict, known_checks: Iterable[str] = ()) -> PlanReport:
    rep = PlanReport(ok=False)
    known = set(known_checks)
    reqs_in = proposal.get("requirements") if isinstance(proposal, dict) else None
    tasks_in = proposal.get("tasks") if isinstance(proposal, dict) else None
    if not isinstance(reqs_in, list) or not reqs_in:
        rep.problems.append("the proposal has no requirements list")
        return rep
    if not isinstance(tasks_in, list) or not tasks_in:
        rep.problems.append("the proposal has no tasks list")
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
        reqs[rid] = {"id": rid, "quote": normalize_ws(quote), "summary": str(r.get("summary") or "")[:300]}

    missing = uncovered_units(task_text, [r["quote"] for r in reqs.values()])
    for u in missing[:30]:
        rep.problems.append(f"not covered by any requirement quote: {u[:160]!r}")
    if len(missing) > 30:
        rep.problems.append(f"... and {len(missing) - 30} more uncovered lines")

    tasks: dict[str, dict] = {}
    for i, t in enumerate(tasks_in):
        if not isinstance(t, dict):
            rep.problems.append(f"task #{i + 1} is not an object")
            continue
        tid = str(t.get("id") or "").strip()
        if not _ID.match(tid) or tid in tasks:
            rep.problems.append(f"task #{i + 1} has an invalid or duplicate id {tid!r}")
            continue
        title = str(t.get("title") or "").strip()
        if not title:
            rep.problems.append(f"{tid}: empty title")
            continue
        links = [str(x) for x in (t.get("links") or [])]
        bad = [x for x in links if x not in reqs]
        if bad:
            rep.problems.append(f"{tid}: links unknown requirements {bad}")
        links = [x for x in links if x in reqs]
        if not links:
            rep.problems.append(f"{tid}: links no requirement")
        checks = [str(c) for c in (t.get("checks") or [])]
        dropped = [c for c in checks if c not in known]
        if dropped:
            rep.warnings.append(f"{tid}: dropped unknown checks {dropped[:5]}")
        try:
            priority = int(t.get("priority") or 0)
        except (TypeError, ValueError):
            priority = 0
        tasks[tid] = {"id": tid, "title": title[:200], "description": str(t.get("description") or "")[:2000],
                      "links": links, "blocked_by": [str(x) for x in (t.get("blocked_by") or [])],
                      "priority": priority, "checks": [c for c in checks if c in known]}
    for tid, t in tasks.items():
        bad = [d for d in t["blocked_by"] if d not in tasks]
        if bad:
            rep.problems.append(f"{tid}: blocked_by unknown tasks {bad}")
            t["blocked_by"] = [d for d in t["blocked_by"] if d in tasks]
    cyc = has_cycle({tid: tuple(t["blocked_by"]) for tid, t in tasks.items()})
    if cyc:
        rep.problems.append(f"dependency cycle: {' -> '.join(cyc)}")
    for rid in reqs:
        if not any(rid in t["links"] for t in tasks.values()):
            rep.problems.append(f"{rid} is not linked by any task")

    rep.requirements = list(reqs.values())
    rep.tasks = [] if cyc else topo_order(tasks)
    rep.ok = not rep.problems
    return rep


def topo_order(tasks: dict[str, dict]) -> list[dict]:
    done: set[str] = set()
    out: list[dict] = []
    pending = sorted(tasks, key=num)
    while pending:
        progress = False
        for tid in list(pending):
            if all(d in done for d in tasks[tid]["blocked_by"]):
                out.append(tasks[tid])
                done.add(tid)
                pending.remove(tid)
                progress = True
        if not progress:
            raise ValueError("cycle")
    return out


def renumber(report: PlanReport, first_task: int = 1) -> tuple[list[dict], list[dict]]:
    """把提议里的 id 规范化为 R1.. / T1..（保持相对顺序），返回（需求，任务）。"""
    rmap = {r["id"]: f"R{i + 1}" for i, r in enumerate(report.requirements)}
    tmap = {t["id"]: f"T{first_task + i}" for i, t in enumerate(report.tasks)}
    reqs = [{**r, "id": rmap[r["id"]]} for r in report.requirements]
    tasks = [{**t, "id": tmap[t["id"]], "links": [rmap[x] for x in t["links"]],
              "blocked_by": [tmap[x] for x in t["blocked_by"]]} for t in report.tasks]
    return reqs, tasks


def mechanical_plan(task_text: str) -> dict:
    """不调模型的兜底：每个实质单元一条需求、一个任务。覆盖由构造保证。"""
    reqs, tasks = [], []
    for i, unit in enumerate(content_units(task_text)):
        rid, tid = f"R{i + 1}", f"T{i + 1}"
        reqs.append({"id": rid, "quote": unit, "summary": unit[:160]})
        tasks.append({"id": tid, "title": unit[:120], "description": "", "links": [rid]})
    if not reqs:
        text = normalize_ws(task_text)[:2000] or "(empty task)"
        reqs = [{"id": "R1", "quote": text, "summary": text[:160]}]
        tasks = [{"id": "T1", "title": text[:120], "description": "", "links": ["R1"]}]
    return {"requirements": reqs, "tasks": tasks}


def validate_split(parent_links: Iterable[str], children: list[dict], requirements: Iterable[str],
                   known_checks: Iterable[str] = ()) -> tuple[list[dict], list[str]]:
    """拆分的校验：子任务合起来覆盖父任务的所有需求链接；链接必须存在。返回（清洗后的子任务，问题）。"""
    reqs = set(requirements)
    known = set(known_checks)
    problems, out = [], []
    if not isinstance(children, list) or len(children) < 2:
        return [], ["a split needs at least two children"]
    for i, c in enumerate(children):
        if not isinstance(c, dict) or not str(c.get("title") or "").strip():
            problems.append(f"child #{i + 1} has no title")
            continue
        links = [str(x) for x in (c.get("links") or []) if str(x) in reqs]
        out.append({"title": str(c["title"])[:200], "description": str(c.get("description") or "")[:2000],
                    "links": links, "checks": [x for x in (c.get("checks") or []) if x in known],
                    "priority": int(c.get("priority") or 0) if str(c.get("priority") or "0").lstrip("-").isdigit()
                    else 0})
    covered = {x for c in out for x in c["links"]}
    lost = sorted(set(parent_links) - covered)
    if lost:
        problems.append(f"the children do not cover requirements {lost}")
    return out, problems
