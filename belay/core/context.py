"""上下文构建 build_context(graph, worker, budget)：每次开会话（首次、交接、恢复、崩溃后）以及 L2 压缩都用它。

按顺序填充 9 段，超出预算时从下往上裁（先截短，太短就整段去掉）；前三段永远不裁。
事实部分（1–6）全部来自图；模型写的部分（7 笔记、8 压缩摘要）只是补充，并明确标注来源。
纯函数：blobs 是调用方按附件路径读出的内容（例如 WIP diff），函数本身不做 IO。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Optional

from belay.core.config import BelayConfig
from belay.core.model import DONE, DONE_UNVERIFIED, REVIEW, Graph
from belay.core.queries import (compactions_of_session, held_tasks, last_session, notes_of_session, num,
                                remaining_sec, requirement_status, reserve_sec, sessions_of, task_files)
from belay.core.suggest import suggest
from belay.core.verify import B_FAIL, B_FLAKY, guard_set, results_for_tree

LABEL = {"original": "task statement, verbatim", "rule": "derived by the harness from its task graph",
         "observed": "observed by the harness (git / test runs)", "self_report": "your own earlier notes, "
         "self-reported and unverified", "llm": "model-written summary, may be incomplete"}

INTRO = {
    "first": "You are starting work on the task below. The harness keeps a task graph for this run: requirements, "
             "tasks, verified checkpoints and your notes. This opening context was generated from that graph.",
    "resume": "You are continuing work in a new session. Nothing from earlier sessions is in your context except "
              "what is below, which the harness rebuilt from its task graph. Files you read before are not in "
              "context: read a file again before editing it.",
    "compaction": "Your earlier conversation in this session was replaced with the context below, rebuilt by the "
                  "harness from its task graph, followed by your most recent messages. Files you read earlier are "
                  "no longer in context unless re-read below: read a file again before editing it.",
}


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
    trimmed: tuple[str, ...] = ()                   # 被截短或去掉的段
    dropped: tuple[str, ...] = ()

    def summary(self) -> dict:
        return {"tokens": self.tokens, "sections": dict(self.sections), "trimmed": list(self.trimmed),
                "dropped": list(self.dropped)}


def estimate_tokens(text: str, cpt: float = 4.0) -> int:
    return int(math.ceil(len(text) / cpt)) if text else 0


def _render(s: Section) -> str:
    return f"## {s.title}\n({LABEL[s.source]})\n{s.text.strip()}\n"


def _clip(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max(0, max_chars - 60)].rstrip() + "\n[... truncated to fit the context budget]"


def _fmt_files(files, limit: int = 40) -> str:
    lines = [f"  {p} (+{a} -{d})" for p, a, d in list(files)[:limit]]
    if len(files) > limit:
        lines.append(f"  ... and {len(files) - limit} more files")
    return "\n".join(lines)


def _minutes(sec: float) -> str:
    return f"{max(0, sec) / 60:.0f} min"


# ---------------------------------------------------------------- 各段

def _requirements(g: Graph) -> str:
    lines = []
    for rid in sorted(g.requirements, key=num):
        r = g.requirements[rid]
        st = requirement_status(g, rid)
        tasks = [t for t in g.tasks.values() if rid in t.links and t.status != "split"]
        tl = ", ".join(f"{t.id}:{t.status}" for t in sorted(tasks, key=lambda t: num(t.id)))
        lines.append(f"- {rid} [{st}] {r.summary or r.quote[:200]}" + (f"  (tasks: {tl})" if tl else ""))
    return "\n".join(lines) or "(requirements are not frozen yet)"


def _evidence_line(g: Graph, t) -> str:
    if not t.checks:
        return "no checks: when you finish, it becomes done_unverified once your work is in a checkpoint"
    cp = g.head_cp
    res = results_for_tree(g, cp.tree) if cp else {}
    parts = [f"{c}={res.get(c, 'not run on checkpoint ' + str(cp.id if cp else '-'))}" for c in t.checks[:10]]
    more = f" (+{len(t.checks) - 10} more)" if len(t.checks) > 10 else ""
    return "checks on the latest checkpoint: " + "; ".join(parts) + more


def _my_tasks(g: Graph, worker: str) -> str:
    held = held_tasks(g, worker)
    if not held:
        return "You hold no task. Pick one with claim (see the suggestions below or call board)."
    out = []
    for t in held:
        out.append(f"### {t.id} [{t.status}] {t.title}")
        if t.description:
            out.append(t.description.strip())
        for rid in t.links:
            r = g.requirements.get(rid)
            if r:
                out.append(f"- {rid} (task text): \"{r.quote[:600]}\"")
        out.append(f"- evidence: {_evidence_line(g, t)}")
        if t.reopen_count and t.reopen_reason:
            fail = "; ".join(t.last_failure[:8])
            out.append(f"- reopened ({t.reopen_reason})" + (f": {fail}" if fail else ""))
        if t.status == REVIEW:
            out.append("- under review: the harness is checking it")
    return "\n".join(out)


def _workspace(g: Graph, worker: str, blobs: Mapping[str, str], cfg: BelayConfig) -> str:
    cp = g.head_cp
    if cp is None:
        return "(no checkpoint yet)"
    head = (f"Latest checkpoint: {cp.id}" + (" (the original code)" if cp.id == 0 else
                                             f" (created by {cp.trigger}, verified at tier {cp.tier})"))
    lines = [head]
    w = g.wips.get(worker)
    if w is None or w.base != cp.id:
        lines.append("No observation of your working tree since this checkpoint yet (run `git status` to see it).")
    elif not w.files and not w.dropped:
        lines.append("Your working tree has no changes relative to it.")
    else:
        if w.files:
            lines.append(f"Unverified changes relative to it (not delivered until checkpointed):\n{_fmt_files(w.files)}")
        if w.dropped:
            lines.append("Changes under test paths (never delivered; checks run against the original test files): "
                         + ", ".join(w.dropped[:20]))
        diff = blobs.get(w.diff) if w.diff else None
        if diff:
            lines.append("Diff of the unverified changes:\n```diff\n" + _clip(diff, cfg.context_diff_chars) + "\n```")
    rej = w.last_rejection if w else None
    if rej:
        regs = rej.get("regressions") or []
        extra = f" (+{rej['n_regressions'] - len(regs)} more)" if rej.get("n_regressions", 0) > len(regs) else ""
        lines.append(f"Last checkpoint attempt {rej.get('attempt')} was rejected ({rej.get('reason')}): "
                     + ("; ".join(regs[:15]) + extra if regs else rej.get("detail", "")))
    return "\n".join(lines)


def _prereq_files(g: Graph, worker: str) -> str:
    held = held_tasks(g, worker)
    prereq = {d for t in held for d in t.blocked_by}
    done = [t for t in g.tasks.values() if t.status in (DONE, DONE_UNVERIFIED)]
    picked = [t for t in done if t.id in prereq] + [t for t in sorted(done, key=lambda t: -num(t.id))
                                                     if t.id not in prereq][:6]
    out = []
    for t in picked:
        files = task_files(g, t.id)
        if files:
            tag = "prerequisite of your task" if t.id in prereq else t.status
            out.append(f"- {t.id} [{tag}] {t.title}\n{_fmt_files(files, 15)}")
    return "\n".join(out)


def _known_failures(g: Graph) -> str:
    fails = sorted(t for t, c in g.baseline.items() if c == B_FAIL)
    flaky = sorted(t for t, c in g.baseline.items() if c == B_FLAKY)
    if not g.baseline_ready:
        return "(baseline not recorded yet)"
    if not g.baseline:
        return "No test results are available for this task, so checkpoints are not verified by tests."
    lines = [f"{len(guard_set(g.baseline))} checks passed twice on the original code: these form the regression "
             "gate. A checkpoint is accepted only if none of them fails, errors, is skipped or goes missing."]
    if fails:
        lines.append(f"{len(fails)} already fail on the original code (no need to investigate unless the task asks "
                     f"for them): " + ", ".join(fails[:25]) + (" ..." if len(fails) > 25 else ""))
    if flaky:
        lines.append(f"{len(flaky)} are flaky on the original code: " + ", ".join(flaky[:15]))
    return "\n".join(lines)


def _prev_session_id(g: Graph, worker: str, mode: str) -> Optional[str]:
    if mode == "compaction":
        s = last_session(g, worker)
        return s.id if s else None
    ended = [s for s in sessions_of(g, worker) if s.ended_t is not None]
    return ended[-1].id if ended else None


def _notes(g: Graph, worker: str, mode: str) -> str:
    sid = _prev_session_id(g, worker, mode)
    notes = [n for n in notes_of_session(g, sid) if n.worker == worker] if sid else []
    if not notes:                     # 上一个会话没留笔记时，退回到最近的几条
        notes = [n for n in g.notes if n.worker == worker][-5:]
    todos = [n for n in notes if n.kind == "todos"][-1:]
    plain = [n for n in notes if n.kind != "todos"][-12:]
    out = [f"- {n.text}" for n in plain]
    if todos:
        out.append("Todo list at the end of that session:\n" + todos[0].text)
    return "\n".join(out)


def _summaries(g: Graph, worker: str, mode: str) -> str:
    sid = _prev_session_id(g, worker, mode)
    cs = [c for c in compactions_of_session(g, sid) if c.summary] if sid else []
    return "\n\n".join(c.summary.strip() for c in cs[-2:])


def _suggestions(g: Graph, worker: str, now: float, cfg: BelayConfig) -> str:
    left = remaining_sec(g, now)
    lines = [f"Time left: about {_minutes(left)} (the last {_minutes(reserve_sec(g, cfg))} are reserved for final "
             "verification)."]
    ss = suggest(g, worker, now, cfg)
    if ss:
        lines.append("Suggested order (you may claim any ready task):")
        for s in ss:
            t = g.tasks.get(s.task) if s.task else None
            lines.append(f"{s.rank}. " + (f"{t.id} {t.title} — {s.reason}" if t else s.reason))
    return "\n".join(lines)


# ---------------------------------------------------------------- 组装

def build_context(g: Graph, worker: str, budget_tokens: int, now: float, cfg: BelayConfig,
                  blobs: Optional[Mapping[str, str]] = None, mode: str = "first") -> Context:
    blobs = blobs or {}
    cpt = cfg.chars_per_token
    secs = [
        Section("task", "Task", "original", f"<task>\n{g.run.task.strip()}\n</task>" if g.run else "", True),
        Section("requirements", "Requirements (frozen) and their status", "rule", _requirements(g), True),
        Section("my_tasks", "Tasks you hold", "rule", _my_tasks(g, worker), True),
        Section("workspace", "Working tree", "observed", _workspace(g, worker, blobs, cfg)),
        Section("prereq_files", "Files changed by finished tasks (in checkpoints)", "observed",
                _prereq_files(g, worker)),
        Section("known_failures", "Known failures and the regression gate", "observed", _known_failures(g)),
        Section("notes", "Your notes in this session" if mode == "compaction" else "Notes from your previous session",
                "self_report", _notes(g, worker, mode)),
        Section("summary", "Summary of your earlier reasoning", "llm", _summaries(g, worker, mode)),
        Section("suggestions", "Suggestions and time", "rule", _suggestions(g, worker, now, cfg)),
    ]
    secs = [s for s in secs if s.text.strip()]
    intro = INTRO.get(mode, INTRO["first"])
    rendered = [_render(s) for s in secs]
    total = estimate_tokens(intro, cpt) + sum(estimate_tokens(r, cpt) for r in rendered)
    trimmed, dropped = [], []
    i = len(secs) - 1
    while total > budget_tokens and i >= 0:
        s = secs[i]
        if s.protected:
            i -= 1
            continue
        cur = estimate_tokens(rendered[i], cpt)
        over = total - budget_tokens
        keep_tokens = cur - over
        if keep_tokens >= 200:
            head = len(_render(Section(s.key, s.title, s.source, "")))
            new = _render(Section(s.key, s.title, s.source, _clip(s.text, int(keep_tokens * cpt) - head)))
            rendered[i] = new
            trimmed.append(s.key)
            total = total - cur + estimate_tokens(new, cpt)
        else:
            rendered[i] = ""
            dropped.append(s.key)
            total -= cur
        i -= 1
    kept = [(s.key, estimate_tokens(r, cpt)) for s, r in zip(secs, rendered) if r]
    text = intro + "\n\n" + "\n".join(r for r in rendered if r)
    return Context(text=text, tokens=estimate_tokens(text, cpt), sections=tuple(kept), trimmed=tuple(trimmed),
                   dropped=tuple(dropped))
