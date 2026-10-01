"""分层开场上下文 build_context：每次开会话（首次、交接、恢复、崩溃后、容器重建）以及 L2 压缩都用它。

原则（模块 I）：
  - 受保护的段，规模只取决于任务本身：任务原文、需求索引、待处理的问题、todo、交接摘要、工作区；它们不会被去掉。
  - 随运行增长的内容一律折叠，每个折叠都留下查询入口（board / failure_log）。
  - 稳定的在前，易变的在后：前缀是任务原文 + 需求索引（冻结后逐字不变），状态都放在后面，前缀缓存整次运行都能命中。
  - 每段有自己的上限（cfg.opening_caps），不再用一个总预算从下往上裁。
  - 每段标明来源；仍是纯函数：blobs 是调用方读出的附件（diff、离开期间工作区的变化），away 是上个会话之后的事件。
v7：没有“当前焦点”和“建议顺序”。恢复时给出：需求状态、worker 自己的 todo、它写的交接摘要、链头以来的改动。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional

from belay.core.config import BelayConfig
from belay.core.events import Event
from belay.core.model import (CONFIRMED, REQ_BLOCKED, REQ_FINISHED, REQ_OPEN, REQ_SUBMITTED, REQ_VERIFIED,
                              TODO_ACTIVE, TODO_ANCHORED, TODO_COMPLETED, Graph)
from belay.core.queries import (actionable, chain, id_ranges, latest_handoff_summary, num, open_persistent,
                                todos_in_order)
from belay.core.render import checkpoint_line, render_diagnosis, render_located, requirement_line
from belay.core.verify import (B_FAIL, B_FLAKY, active_guard, check_unit, reasons_for_tree, regression_ids,
                               related_units, test_files_of)

LABEL = {"original": "task statement, verbatim", "rule": "derived by the harness from its task graph",
         "observed": "observed by the harness (git / test runs)", "self_report": "your own earlier list, "
         "self-reported", "llm": "model-written, may be incomplete",
         "mixed": "from the task graph; parts marked (self-reported) or (model-written) are not verified"}

INTRO = {
    "first": "You are starting work on the task below. The harness keeps a record of this run: the requirements "
             "extracted from the task, verified checkpoints of your work and your todo list. This opening context "
             "was generated from that record.",
    "resume": "You are continuing work in a new session. Nothing from earlier sessions is in your context except "
              "what is below, which the harness rebuilt from its record. Files you read before are not in context: "
              "read a file again before editing it. Your earlier changes are still in the working tree.",
    "compaction": "Your earlier conversation in this session was replaced with the context below, rebuilt by the "
                  "harness from its record, followed by your most recent messages. Files you read earlier are no "
                  "longer in context unless re-read below: read a file again before editing it.",
}
PROTECTED = ("task", "requirements", "pending", "todos", "summary", "workspace")
SUBMIT_LINE = ("When you believe every requirement on the checklist is done, call submit: the harness tests your "
               "work, checks each requirement and tells you what is still missing.")


@dataclass(frozen=True)
class Section:
    key: str
    title: str
    source: str
    text: str
    protected: bool = False


@dataclass(frozen=True)
class Context:
    text: str
    tokens: int
    sections: tuple[tuple[str, int], ...] = ()      # (key, tokens)，按顺序
    trimmed: tuple[str, ...] = ()                   # 被截短的段
    dropped: tuple[str, ...] = ()

    def summary(self) -> dict:
        return {"tokens": self.tokens, "sections": dict(self.sections), "trimmed": list(self.trimmed),
                "dropped": list(self.dropped)}

    @property
    def protected_tokens(self) -> int:
        return sum(t for k, t in self.sections if k in PROTECTED)


def estimate_tokens(text: str, cpt: float = 4.0) -> int:
    return int(math.ceil(len(text) / cpt)) if text else 0


def _render(s: Section) -> str:
    return f"## {s.title}\n({LABEL[s.source]})\n{s.text.strip()}\n"


def _clip(text: str, max_chars: int, entry: str = "") -> str:
    if len(text) <= max_chars:
        return text
    tail = f"\n[... folded; {entry}]" if entry else "\n[... truncated]"
    return text[:max(0, max_chars - len(tail))].rstrip() + tail


def _fmt_files(files, limit: int = 20) -> str:
    files = list(files)
    lines = [f"  {p} (+{a} -{d})" for p, a, d in files[:limit]]
    if len(files) > limit:
        lines.append(f"  ... and {len(files) - limit} more files")
    return "\n".join(lines)


# ---------------------------------------------------------------- 1、2：稳定的前缀

def _requirement_index(g: Graph) -> str:
    """需求清单的索引：actionable 需求的 id + 一行摘要，不带状态（状态在后面），冻结后逐字不变。"""
    lines = []
    for r in actionable(g):
        s = " ".join((r.summary or r.quote).split())
        lines.append(f"- {r.id} {s[:100]}")
    if len(lines) < len(g.requirements):
        lines.append("(Headings and background lines of the task text are not on the checklist.)")
    return "\n".join(lines) or "(requirements are not frozen yet)"


# ---------------------------------------------------------------- 3：待处理的问题

def open_problem_tests(g: Graph) -> set[str]:
    return {t for t in g.persistent if open_persistent(g, t)}


def _pending(g: Graph, worker: str) -> str:
    out = []
    open_tests = open_problem_tests(g)
    if open_tests:
        recs = sorted((g.persistent[t] for t in open_tests), key=lambda r: r.seq)
        by_trigger: dict[str, list[str]] = {}
        for r in recs:
            by_trigger.setdefault(r.trigger, []).append(r.test)
        for trig, tests in by_trigger.items():
            why = {"demoted": "failed the full suite on a checkpoint and still fails on your latest snapshot"
                   }.get(trig, trig)
            out.append(f"- Persistent regression ({why}): " + ", ".join(tests[:8])
                       + (f" (+{len(tests) - 8} more)" if len(tests) > 8 else ""))
    for cp in chain(g):
        if cp.demoted:
            regs = [r for r in cp.demote_regressions if regression_ids([r])[0] in open_tests]
            if regs:
                out.append(f"- Checkpoint {cp.id} was demoted: {'; '.join(regs[:4])} fail(s) in the full suite "
                           "(the related tests did not select them).")
    shown = 0
    for loc in sorted(g.locates.values(), key=lambda l: -l.started_seq):
        if loc.epoch != g.epoch or not loc.results or not (set(loc.tests) & open_tests or loc.trigger == "rejected"):
            continue
        txt = render_located(g, loc.id)
        if txt:
            out.append("- Located: " + txt.replace("\n", "\n  "))
            shown += 1
        if shown >= 2:
            break
    diags = [d for d in sorted(g.diagnoses.values(), key=lambda d: -d.seq) if d.status == "recorded" and
             (set(d.tests) & open_tests or d.trigger in ("rejected", "repeated"))][:2]
    for d in diags:
        out.append("- " + render_diagnosis(g, d.id).replace("\n", "\n  ") + " (model-written)")
    w = g.wips.get(worker)
    rej = w.last_rejection if w else None
    if rej:
        regs = rej.get("regressions") or []
        extra = f" (+{rej['n_regressions'] - len(regs)} more)" if rej.get("n_regressions", 0) > len(regs) else ""
        a = g.attempts.get(rej.get("attempt"))
        reasons = reasons_for_tree(g, a.tree) if a else {}
        lines = []
        for r in regs[:8]:
            why = reasons.get(regression_ids([r])[0])
            lines.append(f"{r}" + (f" — {why[:160]}" if why else ""))
        out.append(f"- Your last submit was rejected (attempt {rej.get('attempt')}, {rej.get('reason')}): "
                   + ("; ".join(lines) + extra if lines else rej.get("detail", "")))
    return "\n".join(out)


# ---------------------------------------------------------------- 5：离开期间

_IMPORTANCE = {"checkpoint_demoted": 0, "persistent_regression": 0, "regression_located": 0,
               "diagnosis_recorded": 0, "requirement_reopened": 0, "rollback": 0, "submit_updated": 0,
               "review_recorded": 1, "checkpoint_confirmed": 1, "checkpoint_rejected": 1, "todo_anchored": 2,
               "requirement_verified": 1, "checkpoint_created": 2, "job_finished": 3, "runtime_recovered": 1}


def _away_line(g: Graph, e: Event) -> Optional[str]:
    t = e.type
    if t == "checkpoint_created":
        cid = int(e.get("checkpoint"))
        return f"checkpoint {checkpoint_line(g, cid)} was created" if cid in g.checkpoints else None
    if t == "checkpoint_confirmed":
        return f"checkpoint {e.get('checkpoint')} passed the full suite (confirmed)"
    if t == "checkpoint_demoted":
        return f"checkpoint {e.get('checkpoint')} failed the full suite: {'; '.join(e.get('regressions')[:3])}"
    if t == "checkpoint_rejected":
        a = g.attempts.get(e.get("attempt"))
        if a is None or (a.lane != "fg" and a.kind != "handoff") or not e.get("regressions"):
            return None
        return f"checkpoint attempt {a.id} ({a.kind}) was rejected: {'; '.join(e.get('regressions')[:3])}"
    if t == "persistent_regression":
        return f"persistent regression: {', '.join(e.get('tests')[:4])}"
    if t == "regression_located":
        return f"regression located: {', '.join(e.get('tests')[:3])} first failed at {e.get('bad', {}).get('id')}"
    if t == "diagnosis_recorded":
        return f"diagnosis {e.get('diagnosis')} recorded (see open problems)"
    if t == "requirement_reopened":
        return f"{e.get('requirement')} was reopened ({e.get('reason')})"
    if t == "requirement_verified":
        return f"{e.get('requirement')} is verified by its checks on checkpoint {e.get('checkpoint')}"
    if t == "submit_updated" and e.get("status") in ("accepted", "returned"):
        return f"submit {e.get('submit')} was {e.get('status')}" + \
            (f"; still open: {id_ranges(e.get('open'))}" if e.get("open") else "")
    if t == "todo_anchored":
        td = g.todos.get(e.get("todo"))
        return f"todo \"{td.title[:60] if td else e.get('todo')}\" is in checkpoint {e.get('checkpoint')}"
    if t == "review_recorded":
        res = e.get("results") or {}
        return "reviewer: " + ", ".join(f"{k}={v.get('implemented')}" for k, v in sorted(res.items()))
    if t == "rollback":
        return f"the working tree was rolled back to checkpoint {e.get('to')}"
    if t == "job_finished" and e.get("state") == "finished":
        j = g.jobs.get(e.get("job"))
        if j is None or j.live:
            return None
        return f"job {j.id} ({j.purpose}) finished: {sum(1 for v in j.results.values() if v == 'PASSED')}/" \
               f"{len(j.results)} passed"
    if t == "runtime_recovered" and e.get("rebuilt"):
        return "the container was rebuilt from the harness's records"
    return None


def _away(g: Graph, away: Iterable[Event], blobs: Mapping[str, str], cfg: BelayConfig) -> str:
    items = []
    counts: dict[str, int] = {}
    for e in away:
        line = _away_line(g, e)
        if line is None:
            continue
        items.append((_IMPORTANCE.get(e.type, 3), -e.seq, e.seq, line, e.type))
    items.sort()
    top = sorted(items[:cfg.away_top], key=lambda x: x[2])
    for x in items[cfg.away_top:]:
        counts[x[4]] = counts.get(x[4], 0) + 1
    out = [f"- {line}" for _, _, _, line, _ in top]
    if counts:
        out.append("- and " + ", ".join(f"{v} more {k.replace('_', ' ')}" for k, v in sorted(counts.items())))
    changed = blobs.get("away_files")
    if changed:
        out.append("The working tree changed since your previous session ended (read files again before editing):\n"
                   + _clip(changed, 1500))
    return "\n".join(out)


# ---------------------------------------------------------------- 状态、todo、摘要、工作区

def _progress(g: Graph, cap_chars: int) -> str:
    reqs = actionable(g)
    counts: dict[str, int] = {}
    for r in reqs:
        counts[r.status] = counts.get(r.status, 0) + 1
    out = ["Checklist: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))]
    ver = [r.id for r in reqs if r.status == REQ_VERIFIED]
    sub = [r.id for r in reqs if r.status == REQ_SUBMITTED]
    if ver:
        out.append(f"Verified by their checks: {id_ranges(ver)}")
    if sub:
        out.append(f"Submitted earlier: {id_ranges(sub)}")
    rest = [r for r in reqs if r.status in (REQ_OPEN, REQ_BLOCKED)]
    lines = [f"- {requirement_line(g, r.id, 90)}" for r in rest]
    body = "\n".join(lines)
    if len(body) > cap_chars:                       # 太多：重开过的、受阻的优先，其余只列编号
        first = [x for x, r in zip(lines, rest) if r.reopen_count or r.status == REQ_BLOCKED][:20]
        others = [r.id for r in rest if not (r.reopen_count or r.status == REQ_BLOCKED)]
        body = "\n".join(first) + (f"\n- not done yet: {id_ranges(others)} (board(status=\"open\"))" if others else "")
    if rest:
        out.append("Not done yet:\n" + body)
    return "\n".join(out)


def _todos(g: Graph) -> str:
    items = todos_in_order(g)
    if not items:
        return ""
    mark = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]", "anchored": "[x]"}
    out = []
    for t in items:
        line = f"{mark.get(t.status, '[ ]')} {t.title}"
        if t.status == TODO_ANCHORED and t.checkpoint is not None:
            line += f"  (in checkpoint {t.checkpoint})"
        elif t.status == TODO_COMPLETED:
            line += "  (not yet in a checkpoint)"
        elif t.status == TODO_ACTIVE:
            line += "  <- in progress"
        out.append(line)
    return "\n".join(out)


def _summary(g: Graph, worker: str, recent_calls: Iterable[str]) -> str:
    out = []
    summ = latest_handoff_summary(g, worker)
    if summ:
        out.append("What you wrote at the last compaction or handoff (model-written):\n" + _clip(summ, 8000))
    calls = list(recent_calls)
    if calls:
        out.append("Your last actions before the interruption (already done; do not repeat them blindly):")
        out.extend(f"- {_clip(c, 300)}" for c in calls[-6:])
    return "\n".join(out)


def _workspace(g: Graph, worker: str, blobs: Mapping[str, str], cfg: BelayConfig, mode: str) -> str:
    cp = g.head_cp
    if cp is None:
        return "(no checkpoint yet)"
    lines = [f"Latest checkpoint: {checkpoint_line(g, cp.id)}"]
    conf = g.confirmed
    if conf is not None and conf != cp.id:
        ids = [c.id for c in chain(g)]
        behind = ids.index(conf) if conf in ids else "?"
        lines.append(f"Latest confirmed checkpoint (what would be delivered now): {conf}, {behind} checkpoint(s) "
                     "behind; provisional checkpoints are confirmed by the full suite in the background.")
    w = g.wips.get(worker)
    if w is not None and w.dropped:
        lines.append("Changes under test paths (never delivered; checks run against the original test files): "
                     + ", ".join(w.dropped[:15]) + (" ..." if len(w.dropped) > 15 else ""))
    partial = blobs.get("partial_diff")
    if partial and mode != "first":
        lines.append(f"Your changes since checkpoint {cp.id}, kept in your working tree (nothing was rolled back; "
                     "the harness verifies them in the background):")
        lines.append("```diff\n" + _clip(partial, cfg.context_diff_chars, "the rest is in your working tree")
                     + "\n```")
    elif w is not None and w.base == cp.id and not w.files and mode != "first":
        lines.append("Your working tree has no changes relative to it.")
    return "\n".join(lines)


def _gate(g: Graph, worker: str) -> str:
    if not g.baseline_ready:
        return "(baseline not recorded yet)"
    if not g.baseline:
        return "No test results are available for this task, so checkpoints are not verified by tests."
    fails = sorted(t for t, c in g.baseline.items() if c == B_FAIL)
    flaky = sorted(t for t, c in g.baseline.items() if c == B_FLAKY)
    lines = [f"{len(active_guard(g))} checks passed twice on the original code: these form the regression "
             "gate. A checkpoint is accepted only if none of them fails, errors, is skipped or goes missing."]
    if g.waived:
        lines.append(f"{len(g.waived)} waived (the task asks for behaviour they contradict): "
                     + ", ".join(sorted(g.waived)[:10]) + (" ..." if len(g.waived) > 10 else ""))
    if g.degraded:
        lines.append("(The tests cannot run outside the working tree here, so checks run only when you submit "
                     "or a session ends.)")
    w = g.wips.get(worker)
    files = [p for p, _, _ in w.files] if w else []
    related = []
    if files and fails:
        sel, _ = related_units(files, test_files_of(g.baseline), g.relations)
        if sel:
            related = [t for t in fails if check_unit(t) in set(sel)]
    if fails:
        lines.append(f"{len(fails)} already fail on the original code (no need to investigate unless the task asks "
                     "for them)" + ("; related to your files: " + ", ".join(related[:15]) if related else "")
                     + "; board(view=\"failures\") lists them all.")
    if flaky:
        lines.append(f"{len(flaky)} are flaky on the original code.")
    return "\n".join(lines)


# ---------------------------------------------------------------- 组装

def build_context(g: Graph, worker: str, budget_tokens: int, now: float, cfg: BelayConfig,
                  blobs: Optional[Mapping[str, str]] = None, mode: str = "first", away: Iterable[Event] = (),
                  recent_calls: Iterable[str] = ()) -> Context:
    """mode：first | resume（交接、崩溃、恢复、容器重建）| compaction（会话内 L2）。now 不进入给模型的文字。"""
    blobs = blobs or {}
    cpt = cfg.chars_per_token
    fresh = mode == "first"
    secs = [
        Section("task", "Task", "original", f"<task>\n{g.run.task.strip()}\n</task>" if g.run else "", True),
        Section("requirements", "Requirements checklist (frozen index; status is below)", "rule",
                _requirement_index(g), True),
        Section("pending", "Open problems", "observed", "" if fresh else _pending(g, worker), True),
        Section("progress", "Requirement status", "rule", _progress(g, int(cfg.cap("progress") * cpt * 0.8))),
        Section("todos", "Your todo list", "self_report", _todos(g), True),
        Section("summary", "Your notes from earlier", "llm",
                "" if fresh else _summary(g, worker, recent_calls if mode == "resume" else ()), True),
        Section("workspace", "Working tree", "observed", _workspace(g, worker, blobs, cfg, mode), True),
        Section("away", "While you were away", "observed", _away(g, away, blobs, cfg) if mode == "resume" else ""),
        Section("gate", "Regression gate", "observed", _gate(g, worker)),
        Section("next", "Finishing", "rule", SUBMIT_LINE),
    ]
    entries = {"pending": "board() and failure_log(test=...)", "progress": "board(status=...)", "todos": "",
               "summary": "", "workspace": "board()", "away": "board()", "gate": "board(view=\"failures\")"}
    secs = [s for s in secs if s.text.strip()]
    trimmed = []
    capped = []
    for s in secs:
        cap = None if s.key in ("task", "requirements", "next") else cfg.cap(s.key)
        if cap is not None and estimate_tokens(s.text, cpt) > cap:
            s = Section(s.key, s.title, s.source, _clip(s.text, int(cap * cpt), entries.get(s.key, "")), s.protected)
            trimmed.append(s.key)
        capped.append(s)
    intro = INTRO.get(mode, INTRO["first"])
    rendered = [_render(s) for s in capped]
    total = estimate_tokens(intro, cpt) + sum(estimate_tokens(r, cpt) for r in rendered)
    dropped = []
    for i in range(len(capped) - 1, -1, -1):        # 兜底：只有任务原文本身超大时才会发生
        if total <= budget_tokens:
            break
        if capped[i].protected or capped[i].key == "next":
            continue
        total -= estimate_tokens(rendered[i], cpt)
        rendered[i] = ""
        dropped.append(capped[i].key)
    kept = [(s.key, estimate_tokens(r, cpt)) for s, r in zip(capped, rendered) if r]
    text = intro + "\n\n" + "\n".join(r for r in rendered if r)
    return Context(text=text, tokens=estimate_tokens(text, cpt), sections=tuple(kept), trimmed=tuple(trimmed),
                   dropped=tuple(dropped))


def resume_reminder(g: Graph, worker: str, now: float, cfg: BelayConfig, blobs: Optional[Mapping[str, str]] = None,
                    away: Iterable[Event] = ()) -> str:
    """原样接上对话时（G2），把待处理的问题、需求状态与离开期间的变化作为 system-reminder 追加在对话之后。"""
    blobs = blobs or {}
    parts = ["The session was interrupted and has been resumed with your conversation intact. The facts below come "
             "from the harness's record and take precedence over what the conversation says. Read files again "
             "before editing them."]
    for key, title, text in (("pending", "Open problems", _pending(g, worker)),
                             ("progress", "Requirement status", _progress(g, int(cfg.cap("progress") * 3))),
                             ("away", "While you were away", _away(g, away, blobs, cfg))):
        if text.strip():
            parts.append(f"## {title}\n" + _clip(text, int(cfg.cap(key) * cfg.chars_per_token)))
    return "\n\n".join(parts)
