"""分层开场上下文 build_context：每次开会话（首次、交接、恢复、崩溃后、容器重建）以及 L2 压缩都用它。

原则（模块 I）：
  - 受保护的段，规模只取决于任务本身：任务原文、需求索引、待处理的问题、todo、交接摘要、工作区；它们不会被去掉。
  - 随运行增长的内容一律折叠，每个折叠都留下查询入口（board / failure_log）。
  - 稳定的在前，易变的在后：前缀是任务原文 + 需求索引（冻结后逐字不变），状态都放在后面，前缀缓存整次运行都能命中。
  - 每段有自己的上限（cfg.opening_caps），不再用一个总预算从下往上裁。
  - 每段标明来源；仍是纯函数：blobs 是调用方读出的附件（diff、离开期间工作区的变化），away 是上个会话之后的事件。
v8：需求状态只来自合并时复核者的判定（带证据等级与缺失项）；恢复时给出：需求状态、worker 自己的 todo、它写的交接摘要、
最新合并点以来的改动、离开期间的复核结论。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional

from belay.core.config import BelayConfig
from belay.core.events import Event
from belay.core.model import (E0, REQ_BLOCKED, REQ_DONE, REQ_OPEN, TODO_ACTIVE, TODO_ANCHORED, TODO_COMPLETED,
                              Graph)
from belay.core.queries import (actionable, chain, id_ranges, improving, last_score, latest_handoff_summary,
                                latest_submit, num, open_persistent, todos_in_order, verifying)
from belay.core.render import (submit_reviews, checkpoint_line, improvement_lines, render_diagnosis, render_located,
                               requirement_line, requirement_state, review_command_lines)
from belay.core.verify import (B_FAIL, B_FLAKY, active_guard, check_unit, reasons_for_tree, regression_ids,
                               related_units, test_files_of)

LABEL = {"original": "task statement, verbatim", "rule": "derived by the harness from its task graph",
         "observed": "observed by the harness (git / test runs)", "self_report": "your own earlier list, "
         "self-reported", "llm": "model-written, may be incomplete",
         "mixed": "from the task graph; parts marked (self-reported) or (model-written) are not verified"}

INTRO = {
    "first": "You are starting work on the task below. The harness keeps a record of this run: the requirements "
             "extracted from the task, the merge points of your work (each one reviewed) and your todo list. This "
             "opening context was generated from that record.",
    "resume": "You are continuing work in a new session. Nothing from earlier sessions is in your context except "
              "what is below, which the harness rebuilt from its record. Files you read before are not in context: "
              "read a file again before editing it. Your earlier changes are still in the working tree.",
    "compaction": "Your earlier conversation in this session was replaced with the context below, rebuilt by the "
                  "harness from its record, followed by your most recent messages. Files you read earlier are no "
                  "longer in context unless re-read below: read a file again before editing it.",
    # 开场理由 phase / fresh（after_accept=polish、打转换人）：只换开头这一段，其余内容按图的状态生成
    "phase": "You are starting a new session: every requirement on the checklist has been accepted, and the run "
             "continues on the delivered version (see Finishing below). Nothing from earlier sessions is in your "
             "context except what is below, which the harness rebuilt from its record. Read a file before editing it.",
    "fresh": "You are taking over from a previous session that kept failing on the same problem (see Why a new "
             "session below). Nothing from earlier sessions is in your context except what is below, which the "
             "harness rebuilt from its record. Form your own view of the problem; read a file before editing it.",
}
PROTECTED = ("task", "requirements", "why", "pending", "todos", "summary", "workspace")
SUBMIT_LINE = ("When you believe every requirement on the checklist is done, call submit: the harness tests your "
               "work, a reviewer checks each requirement and you get back what is still missing.")
IMPROVE_LINE = ("Every requirement on the checklist is done; the run continues to improve the delivered version until "
                "the reviewer finds nothing more worth doing. Work on the open improvement items; "
                "tick a todo item or call submit to get your work reviewed and merged. Only merged work is delivered, "
                "so nothing that already works may break.")
VERIFY_LINE = ("Every requirement on the checklist was accepted, most of them on reading the code. The reviewer then "
               "audited them by running checks: the requirements it reopened are listed above, each with what fails "
               "and the command that shows it. For each one, reproduce the gap first, then fix only that gap; do not "
               "refactor or change behaviour that already works. Only merged work is delivered. When none is open, "
               "call submit: the reviewer may audit again.")


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
        tests = sorted(open_tests)
        out.append("- Persistent regression (failed the regression gate on two background snapshots in a row): "
                   + ", ".join(tests[:8]) + (f" (+{len(tests) - 8} more)" if len(tests) > 8 else ""))
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
        if rej.get("reason") == "review":
            v = g.reviews.get(rej.get("review") or "")
            d = v.decision if v is not None else {}
            if d.get("blocks") is not False:        # 复核者没给出阻断原因的不批准：不当作待处理的问题
                line = (f"- The reviewer did not merge your snapshot s{rej.get('snapshot')} ({rej.get('trigger')}): "
                        + "; ".join((d.get("reasons") or [str(rej.get("detail"))])[:4])[:800])
                if d.get("blocking"):
                    line += f"\n  What blocks the merge: {d['blocking'][:1500]}"
                elif d.get("feedback"):
                    line += f"\n  Reviewer's feedback: {d['feedback'][:1500]}"
                out.append(line)
        else:
            reasons = reasons_for_tree(g, a.tree) if a else {}
            lines = []
            for r in regs[:8]:
                why = reasons.get(regression_ids([r])[0])
                lines.append(f"{r}" + (f" — {why[:160]}" if why else ""))
            out.append(f"- Your last submit was rejected (merge request {rej.get('attempt')}, {rej.get('reason')}): "
                       + ("; ".join(lines) + extra if lines else rej.get("detail", "")))
    return "\n".join(out)


# ---------------------------------------------------------------- 5：离开期间

_IMPORTANCE = {"persistent_regression": 0, "regression_located": 0, "diagnosis_recorded": 0,
               "requirement_judged": 0, "rollback": 0, "submit_updated": 0, "review_decided": 0,
               "improvement_proposed": 0, "improvement_judged": 0, "improve_closed": 0, "improve_started": 0,
               "merge_rejected": 1, "merged": 1, "todo_anchored": 2, "job_finished": 3, "runtime_recovered": 1}


def _away_line(g: Graph, e: Event) -> Optional[str]:
    t = e.type
    if t == "merged":
        cid = int(e.get("checkpoint"))
        return f"merge point {checkpoint_line(g, cid)} was created" if cid in g.checkpoints and cid else None
    if t == "merge_rejected":
        a = g.attempts.get(e.get("attempt"))
        if a is None:
            return None
        if e.get("reason") == "review":
            v = g.reviews.get(a.review) if a.review else None
            if v is not None and v.decision.get("blocks") is False:
                return None
            return f"merge request {a.id} (s{a.snapshot}) was not approved by the reviewer: {str(e.get('detail'))[:200]}"
        if (a.lane != "fg" and a.trigger != "handoff") or not e.get("regressions"):
            return None
        return f"merge request {a.id} ({a.trigger}) was rejected: {'; '.join(e.get('regressions')[:3])}"
    if t == "review_decided":
        fb = e.get("feedback") or ""
        return f"reviewer's feedback ({e.get('review')}): {fb[:300]}" if fb and e.get("merge") is not False else None
    if t == "persistent_regression":
        return f"persistent regression: {', '.join(e.get('tests')[:4])}"
    if t == "regression_located":
        return f"regression located: {', '.join(e.get('tests')[:3])} first failed at {e.get('bad', {}).get('id')}"
    if t == "diagnosis_recorded":
        return f"diagnosis {e.get('diagnosis')} recorded (see open problems)"
    if t == "requirement_judged":
        st = e.get("status")
        lv = f" ({e.get('level')})" if st == REQ_DONE and e.get("level") else ""
        miss = e.get("missing") or []
        return f"{e.get('requirement')} was judged {e.get('judgement') or st}{lv} on merge point " \
               f"{e.get('checkpoint')}" + (f"; missing: {'; '.join(miss[:2])[:200]}" if miss and st == REQ_OPEN
                                          else "")
    if t == "submit_updated" and e.get("status") in ("accepted", "returned"):
        return f"submit {e.get('submit')} was {e.get('status')}" + \
            (f"; still open: {id_ranges(e.get('open'))}" if e.get("open") else "")
    if t == "improvement_proposed":
        return f"the reviewer proposed improvement {e.get('improvement')}: {str(e.get('title'))[:160]}"
    if t == "improvement_judged" and e.get("status") != "open":
        return f"improvement {e.get('improvement')} was judged {e.get('status')}" + \
            (f" ({e.get('level')})" if e.get("level") else "")
    if t == "improve_closed":
        return f"the improvement phase is over: {str(e.get('reason'))[:200]}"
    if t == "improve_started":
        return "every requirement on the checklist is done: the improvement phase started"
    if t == "todo_anchored":
        td = g.todos.get(e.get("todo"))
        return f"todo \"{td.title[:60] if td else e.get('todo')}\" is in merge point {e.get('checkpoint')}"
    if t == "rollback":
        return f"the working tree was rolled back to merge point {e.get('to')}"
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
        k = r.status + (f" {r.level}" if r.status == REQ_DONE else "")
        counts[k] = counts.get(k, 0) + 1
    out = ["Checklist (judged by the reviewer when your work is merged): "
           + ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))]
    done = [r for r in reqs if r.status == REQ_DONE]
    by_level: dict[str, list[str]] = {}
    for r in done:
        by_level.setdefault(r.level or "?", []).append(r.id)
    for lv in sorted(by_level, reverse=True):
        out.append(f"Done ({lv}{', self-reported' if lv == E0 else ''}): {id_ranges(by_level[lv])}")
    rest = [r for r in reqs if r.status in (REQ_OPEN, REQ_BLOCKED)]
    lines = [f"- {requirement_line(g, r.id, 90)}" for r in rest]
    body = "\n".join(lines)
    if len(body) > cap_chars:                       # 太多：判过没做完的、受阻的优先，其余只列编号
        first = [x for x, r in zip(lines, rest) if r.missing or r.status == REQ_BLOCKED][:20]
        others = [r.id for r in rest if not (r.missing or r.status == REQ_BLOCKED)]
        body = "\n".join(first) + (f"\n- not done yet: {id_ranges(others)} (board(status=\"open\"))" if others else "")
    if rest:
        out.append("Not done yet:\n" + body)
    return "\n".join(out)


def _improvements(g: Graph, cfg: BelayConfig) -> str:
    """改进阶段（after_accept=improve，或 polish 的 IMPROVE 模式）：复核者提出的改进项。还没开始、也没有改进项时为空；
    VERIFY 模式没有改进项（复审退回的需求列在需求状态里）。"""
    if g.run is not None and g.run.polish_mode == "verify":
        return ""
    if not g.improvements and not improving(g, cfg):
        return ""
    run = g.run
    if improving(g, cfg):
        head = "Improvement phase: in progress. The reviewer's items (open first):"
    elif run is not None and run.improve_closed:
        head = f"Improvement phase: over ({run.improve_closed[:300]})."
    else:
        head = "Improvement items proposed by the reviewer:"
    lines = improvement_lines(g)
    if improving(g, cfg) and not any(i.status == "open" for i in g.improvements.values()):
        lines.append("  (none open right now; the reviewer proposes more when it reviews your next change)")
    return "\n".join([head] + lines)


def _todos(g: Graph) -> str:
    items = todos_in_order(g)
    if not items:
        return ""
    mark = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]", "anchored": "[x]"}
    out = []
    for t in items:
        line = f"{mark.get(t.status, '[ ]')} {t.title}"
        if t.status == TODO_ANCHORED and t.checkpoint is not None:
            line += f"  (in merge point {t.checkpoint})"
        elif t.status == TODO_COMPLETED:
            line += "  (not merged yet)"
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


def _workspace(g: Graph, worker: str, blobs: Mapping[str, str], cfg: BelayConfig, mode: str,
               reason: str = "") -> str:
    cp = g.head_cp
    if cp is None:
        return "(no merge point yet)"
    lines = [f"Latest merge point (what would be delivered now): {checkpoint_line(g, cp.id)}"]
    score, note, at = last_score(g)
    if score is not None and at != cp.id:
        lines.append(f"Latest measured score: {score:g} at merge point {at} ({note[:200]})")
    w = g.wips.get(worker)
    if w is not None and w.dropped:
        lines.append("Changes under test paths (never delivered; checks run against the original test files): "
                     + ", ".join(w.dropped[:15]) + (" ..." if len(w.dropped) > 15 else ""))
    delivered = blobs.get("delivered_files")
    if delivered and g.run is not None and g.run.improving:
        lines.append("Files the delivered version changes relative to the original code:\n" + _clip(delivered, 2500))
    partial = blobs.get("partial_diff")
    if partial and mode != "first" and reason == "fresh":
        lines.append(f"Changes since merge point {cp.id} left in your working tree by the previous session; they "
                     "were not approved. Keep what is useful, or undo what is not:")
        lines.append("```diff\n" + _clip(partial, cfg.context_diff_chars, "the rest is in your working tree")
                     + "\n```")
    elif partial and mode != "first":
        lines.append(f"Your changes since merge point {cp.id}, kept in your working tree (nothing was rolled back; "
                     "the harness reviews them in the background):")
        lines.append("```diff\n" + _clip(partial, cfg.context_diff_chars, "the rest is in your working tree")
                     + "\n```")
    elif w is not None and w.base == cp.id and not w.files and mode != "first":
        lines.append("Your working tree has no changes relative to it.")
    return "\n".join(lines)


def _gate(g: Graph, worker: str) -> str:
    if not g.baseline_ready:
        return "(baseline not recorded yet)"
    if not g.baseline:
        return ("No tests are available for this task: a reviewer checks each merge of your work by reading and "
                "running it.")
    fails = sorted(t for t, c in g.baseline.items() if c == B_FAIL)
    flaky = sorted(t for t, c in g.baseline.items() if c == B_FLAKY)
    lines = [f"{len(active_guard(g))} checks passed twice on the original code: these form the regression "
             "gate. Your work is merged only if none of them fails, errors, is skipped or goes missing."]
    if g.waived:
        lines.append(f"{len(g.waived)} waived by the reviewer (the task asks for behaviour they contradict): "
                     + ", ".join(sorted(g.waived)[:10]) + (" ..." if len(g.waived) > 10 else ""))
    if g.degraded:
        lines.append("(The tests cannot run outside the working tree here, so merges happen only when you submit "
                     "or a session ends.)")
    w = g.wips.get(worker)
    files = [p for p, _, _ in w.files] if w else []
    related = []
    if files and fails:
        sel, _ = related_units(files, test_files_of(g.baseline))
        if sel:
            related = [t for t in fails if check_unit(t) in set(sel)]
    if fails:
        lines.append(f"{len(fails)} already fail on the original code (no need to investigate unless the task asks "
                     "for them)" + ("; related to your files: " + ", ".join(related[:15]) if related else "")
                     + "; board(view=\"failures\") lists them all.")
    if flaky:
        lines.append(f"{len(flaky)} are flaky on the original code.")
    return "\n".join(lines)


def _why_fresh(g: Graph, worker: str, cfg: BelayConfig) -> str:
    """开场理由 fresh：上一个会话为什么被换下（停滞信号）、复核者能复现问题的命令、受阻的出路。"""
    st = next((x for x in reversed(g.stalls) if x.worker == worker and x.action == "handoff"), None)
    if st is None:
        return ""
    out = [f"The previous session kept failing on this: {st.detail[:800]}"]
    s = latest_submit(g, worker)
    if s is not None:
        out += review_command_lines(g, submit_reviews(g, s.id), cfg.review_commands)
    if st.sig.startswith("req:"):
        rid = st.sig[4:]
        out.append(f"If {rid} really cannot be done here, declare it in submit(blocked=[{{requirement: \"{rid}\", kind, "
                   "reason}]) and the reviewer decides; other open requirements can be worked on first.")
    return "\n".join(out)


# ---------------------------------------------------------------- 组装

def finishing_line(g: Graph, cfg: BelayConfig) -> str:
    """“收尾”一段按图的状态给（不看开场理由：POLISH 里交接、崩溃之后开的会话也拿到同样的说明）。"""
    if verifying(g, cfg):
        return VERIFY_LINE
    return IMPROVE_LINE if improving(g, cfg) else SUBMIT_LINE


def build_context(g: Graph, worker: str, budget_tokens: int, now: float, cfg: BelayConfig,
                  blobs: Optional[Mapping[str, str]] = None, mode: str = "first", away: Iterable[Event] = (),
                  recent_calls: Iterable[str] = (), reason: str = "") -> Context:
    """mode：first | resume（交接、崩溃、恢复、容器重建）| compaction（会话内 L2）。reason 是开场理由：phase / fresh 时
    换开头一段（fresh 另有“为什么换新会话”一段），其余内容按图的状态生成。now 不进入给模型的文字。"""
    blobs = blobs or {}
    cpt = cfg.chars_per_token
    fresh = mode == "first"
    secs = [
        Section("task", "Task", "original", f"<task>\n{g.run.task.strip()}\n</task>" if g.run else "", True),
        Section("requirements", "Requirements checklist (frozen index; status is below)", "rule",
                _requirement_index(g), True),
        Section("why", "Why a new session", "rule", _why_fresh(g, worker, cfg) if reason == "fresh" else "", True),
        Section("pending", "Open problems", "observed", "" if fresh else _pending(g, worker), True),
        Section("progress", "Requirement status", "rule", _progress(g, int(cfg.cap("progress") * cpt * 0.8))),
        Section("improvements", "Improvements", "rule", _improvements(g, cfg)),
        Section("todos", "Your todo list", "self_report", _todos(g), True),
        Section("summary", "Your notes from earlier", "llm",
                "" if fresh else _summary(g, worker, recent_calls if mode == "resume" else ()), True),
        Section("workspace", "Working tree", "observed", _workspace(g, worker, blobs, cfg, mode, reason), True),
        Section("away", "While you were away", "observed", _away(g, away, blobs, cfg) if mode == "resume" else ""),
        Section("gate", "Regression gate", "observed", _gate(g, worker)),
        Section("next", "Finishing", "rule", finishing_line(g, cfg)),
    ]
    entries = {"why": "", "pending": "board() and failure_log(test=...)", "progress": "board(status=...)", "todos": "",
               "improvements": "board()",
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
    intro = INTRO.get(reason if mode == "resume" and reason in ("phase", "fresh") else mode, INTRO["first"])
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
                             ("improvements", "Improvements", _improvements(g, cfg)),
                             ("away", "While you were away", _away(g, away, blobs, cfg))):
        if text.strip():
            parts.append(f"## {title}\n" + _clip(text, int(cfg.cap(key) * cfg.chars_per_token)))
    return "\n\n".join(parts)
