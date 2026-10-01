"""分层开场上下文 build_context：每次开会话（首次、交接、恢复、崩溃后、容器重建）以及 L2 压缩都用它。

原则（模块 I）：
  - 受保护的段，规模只取决于任务本身：任务原文、需求索引、当前焦点、待处理的问题；它们不会被去掉。
  - 随运行增长的内容一律折叠，每个折叠都留下查询入口（board / task / history / failure_log）。
  - 稳定的在前，易变的在后：前缀是任务原文 + 需求索引（冻结后逐字不变），状态都放在后面，前缀缓存整次运行都能命中。
  - 每段有自己的上限（cfg.opening_caps），不再用一个总预算从下往上裁。
  - 每段标明来源；仍是纯函数：blobs 是调用方读出的附件（diff、离开期间工作区的变化），away 是上个会话之后的事件。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional

from belay.core.config import BelayConfig
from belay.core.events import Event
from belay.core.model import (ACTIVE, BLOCKED, CONFIRMED, DONE, DONE_UNVERIFIED, OPEN, REVIEW, STEP_ANCHORED,
                              STEP_DECLARED, Graph)
from belay.core.queries import (chain, focus_task, held_tasks, id_ranges, latest_handoff_summary, notes_of_task,
                                num, open_persistent, requirement_status, resume_point, steps_of, task_files)
from belay.core.render import checkpoint_line, render_diagnosis, render_located
from belay.core.suggest import suggest
from belay.core.verify import (B_FAIL, B_FLAKY, check_unit, guard_set, reasons_for_tree, related_units,
                               regression_ids, results_for_tree, test_files_of)

LABEL = {"original": "task statement, verbatim", "rule": "derived by the harness from its task graph",
         "observed": "observed by the harness (git / test runs)", "self_report": "your own earlier notes, "
         "self-reported and unverified", "llm": "model-written, may be incomplete",
         "mixed": "from the task graph; parts marked (self-reported) or (model-written) are not verified"}

INTRO = {
    "first": "You are starting work on the task below. The harness keeps a task graph for this run: requirements, "
             "tasks, steps, verified checkpoints and your notes. This opening context was generated from that graph.",
    "resume": "You are continuing work in a new session. Nothing from earlier sessions is in your context except "
              "what is below, which the harness rebuilt from its task graph. Files you read before are not in "
              "context: read a file again before editing it. You do not need to re-read the code of steps that are "
              "already done; focus on the current step.",
    "compaction": "Your earlier conversation in this session was replaced with the context below, rebuilt by the "
                  "harness from its task graph, followed by your most recent messages. Files you read earlier are "
                  "no longer in context unless re-read below: read a file again before editing it.",
}
PROTECTED = ("task", "requirements", "focus", "pending")


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
    """需求索引：id + 一行摘要，不带状态（状态在“进度总览”里），冻结后逐字不变。"""
    lines = []
    for rid in sorted(g.requirements, key=num):
        r = g.requirements[rid]
        s = " ".join((r.summary or r.quote).split())
        lines.append(f"- {rid} {s[:80]}")
    return "\n".join(lines) or "(requirements are not frozen yet)"


# ---------------------------------------------------------------- 3：当前焦点（恢复点）

def _evidence_line(g: Graph, t) -> str:
    if not t.checks:
        return "no checks: when you finish, it becomes done_unverified once your work is in a checkpoint"
    cp = g.head_cp
    res = results_for_tree(g, cp.tree) if cp else {}
    parts = [f"{c}={res.get(c, 'not run on checkpoint ' + str(cp.id if cp else '-'))}" for c in t.checks[:10]]
    more = f" (+{len(t.checks) - 10} more; task(id=\"{t.id}\"))" if len(t.checks) > 10 else ""
    return "checks on the latest checkpoint: " + "; ".join(parts) + more


def _focus(g: Graph, worker: str, blobs: Mapping[str, str], cfg: BelayConfig, mode: str,
           recent_calls: Iterable[str]) -> str:
    t = focus_task(g, worker)
    held = held_tasks(g, worker)
    if t is None:
        return "You hold no task. Pick one with claim (see Next below, or call board)."
    out = [f"### {t.id} [{t.status}] {t.title}"]
    if t.description:
        out.append(_clip(t.description.strip(), 1500, f"task(id=\"{t.id}\")"))
    for rid in t.links[:5]:
        r = g.requirements.get(rid)
        if r:
            out.append(f"- {rid} (task text): \"{_clip(r.quote, 600)}\"")
    if len(t.links) > 5:
        out.append(f"- ... {len(t.links) - 5} more linked requirements: task(id=\"{t.id}\")")
    out.append(f"- evidence: {_evidence_line(g, t)}")
    if t.reopen_count and t.reopen_reason:
        fail = "; ".join(t.last_failure[:8])
        out.append(f"- reopened ({t.reopen_reason})" + (f": {fail}" if fail else ""))
    if t.status == REVIEW:
        out.append("- under review: the harness is checking it")
    others = [x for x in held if x.id != t.id]
    for x in others:
        out.append(f"- you also hold {x.id} [{x.status}] {x.title}")
    # 步骤：计划做到了哪一步、还剩什么
    steps = steps_of(g, t.id)
    rp = resume_point(g, worker)
    if steps:
        out.append("Steps (your plan, kept by the harness; update it with todo_write, mark a step finished with "
                   "step_done):")
        for s in steps:
            mark = {"planned": "[ ]", "active": "[~]", "declared": "[x]", "anchored": "[x]"}.get(s.status, "[ ]")
            line = f"  {mark} {s.id} {s.title}"
            if s.status in (STEP_DECLARED, STEP_ANCHORED):
                cpv = "not yet in a checkpoint"
                if s.status == STEP_ANCHORED and s.checkpoint in g.checkpoints:
                    c = g.checkpoints[s.checkpoint]
                    state = ("demoted: it failed the full suite" if c.demoted else
                             "not yet confirmed by the full suite" if c.level != CONFIRMED else "confirmed")
                    cpv = f"in checkpoint {s.checkpoint} ({state})"
                line += f" — done, {cpv}"
                if s.summary:
                    line += f": {s.summary[:160]} (self-reported)"
                if s.files:
                    line += "; files: " + ", ".join(f"{p} (+{a} -{d})" for p, a, d in s.files[:5])
            elif s.id == rp.get("step"):
                line += "  <- current step"
            out.append(line)
    elif mode != "first":
        labels = [c for c in chain(g) if c.label and t.id in c.tasks][:5]
        if labels:
            out.append("What earlier checkpoints of this task did (model-written labels):")
            out.extend(f"  checkpoint {c.id}: {c.label}" for c in reversed(labels))
    # 部分改动（当前步骤里还没进存档的工作）
    partial = blobs.get("partial_diff")
    if partial and mode != "first":
        base = rp.get("base")
        out.append(f"Partial changes since checkpoint {base} ({rp.get('base_reason')}), kept in your working tree "
                   "(undo them yourself if they are wrong; nothing was rolled back):")
        out.append("```diff\n" + _clip(partial, cfg.context_diff_chars, "history(a=..., b=...)") + "\n```")
    notes = [n for n in notes_of_task(g, t.id, worker) if n.kind in ("note", "released")][-8:]
    if notes:
        out.append("Your notes on this task (self-reported):")
        out.extend(f"- {_clip(n.text, 600)}" for n in notes)
    if mode != "first":
        summ = latest_handoff_summary(g, worker)
        if summ:
            out.append("Summary you wrote at the last compaction or handoff (model-written):\n" + _clip(summ, 4000))
        ps = [s for s in g.summaries if s.get("worker") == worker]
        if ps:
            out.append("Summary of the conversation before the interruption (model-written):\n"
                       + _clip(ps[-1]["text"], 2000))
    calls = list(recent_calls)
    if calls:
        out.append("Your last actions before the interruption (already done; do not repeat them blindly):")
        out.extend(f"- {_clip(c, 300)}" for c in calls[-6:])
    return "\n".join(out)


# ---------------------------------------------------------------- 4：待处理的问题

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
            why = {"demoted": "failed the full suite on a checkpoint and still fails on your latest snapshot",
                   "background": "failed on several consecutive snapshots",
                   "dev_check": "fails in the background checks and in your run_check"}.get(trig, trig)
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
        if loc.epoch != g.epoch or not loc.results or not (set(loc.tests) & open_tests or loc.trigger in
                                                            ("rejected", "step")):
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
        out.append(f"- Last rejected checkpoint attempt {rej.get('attempt')} ({rej.get('kind', '')}, "
                   f"{rej.get('reason')}): " + ("; ".join(lines) + extra if lines else rej.get("detail", "")))
    return "\n".join(out)


# ---------------------------------------------------------------- 5：离开期间

_IMPORTANCE = {"checkpoint_demoted": 0, "persistent_regression": 0, "regression_located": 0,
               "diagnosis_recorded": 0, "task_reopened": 0, "task_split": 0, "rollback": 0,
               "review_recorded": 1, "checkpoint_confirmed": 1, "checkpoint_rejected": 1, "step_anchored": 1,
               "task_done": 1, "checkpoint_created": 2, "job_finished": 3, "runtime_recovered": 1}


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
        if a is None or (a.lane != "fg" and a.kind not in ("step", "handoff")) or not e.get("regressions"):
            return None
        return f"checkpoint attempt {a.id} ({a.kind}) was rejected: {'; '.join(e.get('regressions')[:3])}"
    if t == "persistent_regression":
        return f"persistent regression: {', '.join(e.get('tests')[:4])}"
    if t == "regression_located":
        return f"regression located: {', '.join(e.get('tests')[:3])} first failed at {e.get('bad', {}).get('id')}"
    if t == "diagnosis_recorded":
        return f"diagnosis {e.get('diagnosis')} recorded (see open problems)"
    if t == "task_reopened":
        return f"{e.get('task')} was reopened ({e.get('reason')})"
    if t == "task_done":
        return f"{e.get('task')} is {'done' if e.get('verified') else 'done_unverified'} on checkpoint " \
               f"{e.get('checkpoint')}"
    if t == "task_split":
        return f"{e.get('task')} was split into {', '.join(c['id'] for c in e.get('children'))}"
    if t == "step_anchored":
        return f"step {e.get('step')} is in checkpoint {e.get('checkpoint')}"
    if t == "review_recorded":
        return f"reviewer on {e.get('task')}: implemented={e.get('implemented')}"
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


# ---------------------------------------------------------------- 6–9

def _workspace(g: Graph, worker: str) -> str:
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
    if w is None or w.base != cp.id:
        lines.append("Your working tree has changes that are not in a checkpoint yet (the harness snapshots and "
                     "checkpoints it in the background).")
    elif not w.files and not w.dropped:
        lines.append("Your working tree has no changes relative to it.")
    else:
        if w.files:
            lines.append(f"Changes not in a checkpoint yet ({len(w.files)} file(s); the harness checkpoints them in "
                         f"the background):\n{_fmt_files(w.files, 20)}")
        if w.dropped:
            lines.append("Changes under test paths (never delivered; checks run against the original test files): "
                         + ", ".join(w.dropped[:15]) + (" ..." if len(w.dropped) > 15 else ""))
    return "\n".join(lines)


def _progress(g: Graph, worker: str, cap_chars: int) -> str:
    counts: dict[str, int] = {}
    by_status: dict[str, list[str]] = {}
    for rid in sorted(g.requirements, key=num):
        st = requirement_status(g, rid)
        counts[st] = counts.get(st, 0) + 1
        by_status.setdefault(st, []).append(rid)
    out = ["Requirements: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))]
    done = by_status.get("done", []) + by_status.get("done_unverified", [])
    if done:
        out.append(f"Finished: {id_ranges(done)}")
    rest = [t for t in sorted(g.tasks.values(), key=lambda t: num(t.id)) if t.status in (OPEN, ACTIVE, REVIEW, BLOCKED)]
    lines = []
    for t in rest:
        extra = f" ({t.blocked_kind})" if t.status == BLOCKED else ""
        lines.append(f"- {t.id} [{t.status}]{extra} {t.title[:80]} -> {', '.join(t.links[:4])}")
    body = "\n".join(lines)
    if len(body) > cap_chars:                       # 太多：只展开与当前任务相关的
        f = focus_task(g, worker)
        rel = set(f.links) if f else set()
        near = [x for x, t in zip(lines, rest) if set(t.links) & rel or (f and t.id in f.blocked_by)]
        body = "\n".join(near[:20]) + f"\n- ... {len(rest)} unfinished or blocked task(s) in all: " \
                                      "board(status=\"unfinished\"), board(status=\"blocked\")"
    if rest:
        out.append("Unfinished and blocked tasks:\n" + body)
    done_t = [t.id for t in g.tasks.values() if t.status in (DONE, DONE_UNVERIFIED)]
    if done_t:
        out.append(f"Finished tasks: {id_ranges(done_t)} (board(status=\"done\"))")
    return "\n".join(out)


def _next(g: Graph, worker: str, now: float, cfg: BelayConfig) -> str:
    # 剩余时间只由 runtime 用来决定何时收尾，不写进给模型的文字。
    ss = suggest(g, worker, now, cfg)[:3]
    if not ss:
        return ""
    lines = ["Suggested order (you may claim any open task):"]
    for s in ss:
        t = g.tasks.get(s.task) if s.task else None
        lines.append(f"{s.rank}. " + (f"{t.id} {t.title} — {s.reason}" if t else s.reason))
    return "\n".join(lines)


def _gate(g: Graph, worker: str) -> str:
    if not g.baseline_ready:
        return "(baseline not recorded yet)"
    if not g.baseline:
        return "No test results are available for this task, so checkpoints are not verified by tests."
    fails = sorted(t for t, c in g.baseline.items() if c == B_FAIL)
    flaky = sorted(t for t, c in g.baseline.items() if c == B_FLAKY)
    lines = [f"{len(guard_set(g.baseline))} checks passed twice on the original code: these form the regression "
             "gate. A checkpoint is accepted only if none of them fails, errors, is skipped or goes missing."]
    if g.degraded:
        lines.append("(The tests cannot run outside the working tree here, so checks run only when you "
                     "checkpoint, finish a task or a session ends.)")
    f = focus_task(g, worker)
    files = [p for p, _, _ in task_files(g, f.id)] if f else []
    w = g.wips.get(worker)
    if w:
        files += [p for p, _, _ in w.files]
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
    """mode：first | resume（交接、崩溃、恢复、容器重建）| compaction（会话内 L2）。"""
    blobs = blobs or {}
    cpt = cfg.chars_per_token
    fresh = mode == "first"
    secs = [
        Section("task", "Task", "original", f"<task>\n{g.run.task.strip()}\n</task>" if g.run else "", True),
        Section("requirements", "Requirements (frozen index; status is in the progress overview)", "rule",
                _requirement_index(g), True),
        Section("focus", "Current focus", "mixed",
                _focus(g, worker, blobs, cfg, mode, recent_calls if mode == "resume" else ()), True),
        Section("pending", "Open problems", "observed", "" if fresh else _pending(g, worker), True),
        Section("away", "While you were away", "observed", _away(g, away, blobs, cfg) if mode == "resume" else ""),
        Section("workspace", "Working tree", "observed", _workspace(g, worker)),
        Section("progress", "Progress overview", "rule", _progress(g, worker, int(cfg.cap("progress") * cpt * 0.8))),
        Section("next", "Next", "rule", _next(g, worker, now, cfg)),
        Section("gate", "Regression gate", "observed", _gate(g, worker)),
    ]
    entries = {"focus": "task(id=...) has the rest", "pending": "board() and failure_log(test=...)",
               "away": "board()", "workspace": "board()", "progress": "board(status=...)", "next": "board()",
               "gate": "board(view=\"failures\")"}
    secs = [s for s in secs if s.text.strip()]
    trimmed = []
    capped = []
    for s in secs:
        cap = None if s.key in ("task", "requirements") else cfg.cap(s.key)
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
        if capped[i].protected:
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
    """原样接上对话时（G2），把恢复点、待处理的问题与离开期间的变化作为 system-reminder 追加在对话之后。"""
    blobs = blobs or {}
    parts = ["The session was interrupted and has been resumed with your conversation intact. The facts below come "
             "from the harness's task graph and take precedence over what the conversation says. Read files again "
             "before editing them."]
    for key, title, text in (("focus", "Current focus", _focus(g, worker, blobs, cfg, "resume", ())),
                             ("pending", "Open problems", _pending(g, worker)),
                             ("away", "While you were away", _away(g, away, blobs, cfg))):
        if text.strip():
            parts.append(f"## {title}\n" + _clip(text, int(cfg.cap(key) * cfg.chars_per_token)))
    return "\n\n".join(parts)
