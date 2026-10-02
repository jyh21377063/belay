"""复核者（模块 F）：每个复核开一个带工具的短会话，在复核目录里检出被复核的快照后工作。

复用 worker 的循环（belay/worker/loop.py）：同一个模型、同一套读文件工具；另有
  run          在复核目录里执行命令（构建、运行程序、检查产物），编号 X1、X2……，退出码记进日志（E2 的依据）
  run_tests    让验证器在独立目录里用原始测试文件跑指定的测试（与回归门同一种跑法；E3 的依据）
  run_gate     回归门在这个快照上的结果（由 runtime 在复核之前跑完）
  locate       在快照之间二分，找出某个测试从哪一次改动开始失败
  verdict      结构化结论：复核者唯一的写出口
复核者看不到 worker 的上下文，不修改 worker 的工作区（工作区路径与 harness 的状态目录都在保护名单里），
轮数与时间有上限。它的结论原样记为 merge_reviewed，由规则校验后才改变账本与合并链（rules.record_review）。
"""
from __future__ import annotations

import asyncio
import json
import shlex
import time
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from belay.core import rules as R
from belay.core.config import BelayConfig
from belay.core.model import ACTIONABLE, BG_TRIGGERS, JOB_RUNNING, REQ_DONE, REQ_OPEN, REV_DECIDED, REV_RUNNING
from belay.core.queries import actionable, last_score, latest_handoff_summary, todos_in_order
from belay.core.render import checkpoint_line, render_job, render_located, requirement_state
from belay.core.verify import (B_PASS, PASSED, PT_FAIL, active_guard, guard_set, point_status, reasons_for_tree,
                               regression_ids, results_for_tree, units)
from belay.env import Env, ExecOutput
from belay.llm import Usage
from belay.runtime.planner import extract_json
from belay.runtime.prompts import REVIEWER_SYSTEM
from belay.runtime.review import diff_digest
from belay.tools import Policy, Tool, ToolContext, ToolError
from belay.tools import files as F
from belay.tools.output import truncate_output
from belay.worker.transcript import Transcript

if TYPE_CHECKING:
    from belay.runtime.driver import BelayRun

# 不提时间、轮数或 token（提了模型会敷衍）；上限只由 runtime 执行
WRAPUP = ("Stop reviewing now and call verdict with what you have found (merge, the changes to existing behaviour with "
          "their quotes, the requirements you judged with their evidence levels, feedback). Do not call any other "
          "tool.")
TRIGGER_TEXT = {
    "auto": "a background check of the agent's latest snapshot (the agent keeps working meanwhile)",
    "todo": "a background check right after the agent ticked off a todo item",
    "handoff": "a check at a session handoff",
    "session_end": "a check at the end of a session",
    "submit": "the agent called submit: it believes the requirements are done and waits for your verdict",
    "final": "the final check before delivery",
    "deadline": "the final check before delivery",
    "judge": "the agent called submit without new changes: judge the requirements on the latest merge point",
}


def review_limits(cfg: BelayConfig, trigger: str) -> tuple[int, float]:
    """(轮数, 秒)：后台复核只看增量，预算小；提交、收尾、只判定的复核预算大。"""
    if trigger in BG_TRIGGERS:
        return cfg.review_bg_max_turns, cfg.review_bg_max_sec
    return cfg.review_max_turns, cfg.review_max_sec


class SubdirEnv(Env):
    """同一个容器，工作目录换成复核目录。"""

    def __init__(self, base: Env, workdir: str):
        self.base = base
        self.workdir = workdir

    async def _exec(self, command: str, timeout: float) -> ExecOutput:
        return await self.base._exec(command, timeout)


class _State:
    def __init__(self):
        self.runs: list[dict] = []
        self.verdict: Optional[dict] = None


VERDICT_SCHEMA = {"type": "object", "properties": {
    "merge": {"type": "boolean", "description": "Whether this snapshot becomes the next merge point (for a judge-only "
                                                "review, false)"},
    "reason": {"type": "string", "description": "Why it is (not) merged, one or two sentences"},
    "summary": {"type": "string", "description": "One line: what this change set does (the merge point's label)"},
    "requirements": {"type": "array", "items": {"type": "object", "properties": {
        "id": {"type": "string"},
        "status": {"type": "string", "enum": ["done", "partial", "not_done", "blocked"]},
        "level": {"type": "string", "enum": ["E3", "E2", "E1", "E0"]},
        "evidence": {"type": "array", "items": {"type": "string"}, "description": "Short evidence lines"},
        "tests": {"type": "array", "items": {"type": "string"}, "description": "Test ids (E3)"},
        "runs": {"type": "array", "items": {"type": "string"}, "description": "Run ids such as X2 (E2)"},
        "missing": {"type": "array", "items": {"type": "string"}, "description": "What is missing, concretely"},
        "regressed": {"type": "boolean", "description": "A requirement that was done is broken by this change"},
        "reason": {"type": "string"}},
        "required": ["id", "status", "level"]}},
    "behavior_changes": {"type": "array", "description": "Every way the changes since the previous merge point alter "
                         "what EXISTING code already did (a different result, precedence, default, error or output "
                         "for inputs the old code already handled). Purely new behaviour for inputs the old code did "
                         "not handle is not listed. Empty when there is none.",
                         "items": {"type": "object", "properties": {
                             "what": {"type": "string", "description": "Old behaviour -> new behaviour"},
                             "quote": {"type": "string", "description": "Verbatim task text that demands exactly this "
                                                                        "change; empty if there is none"},
                             "requirement": {"type": "string"}},
                             "required": ["what", "quote"]}},
    "waivers": {"type": "array", "items": {"type": "object", "properties": {
        "tests": {"type": "array", "items": {"type": "string"}},
        "quote": {"type": "string", "description": "Verbatim task text that asks for the new behaviour"},
        "reason": {"type": "string"}, "requirement": {"type": "string"}},
        "required": ["tests", "quote", "reason"]}},
    "score": {"type": ["number", "null"], "description": "Measured objective, higher is better; null if none"},
    "score_note": {"type": "string", "description": "How the score was measured"},
    "feedback": {"type": "string", "description": "For the agent: concrete missing items, failing tests, commands"}},
    "required": ["merge", "reason", "requirements", "behavior_changes", "feedback"]}


class Reviewer:
    def __init__(self, run: "BelayRun"):
        self.run = run
        self.tasks: dict[str, asyncio.Task] = {}
        self.usage = Usage()

    @property
    def review_dir(self) -> str:
        return self.run.review_dir

    def policy(self) -> Policy:
        r = self.run
        prot = (r.s.git_dir, r.s.jobs_dir, "/logs", r.env.workdir) + \
            ((r.verifier.verify_dir,) if r.verifier is not None else ())
        return Policy(git_write="deny", network="deny", disk_search="deny", harness_paths="deny",
                      protected_prefixes=tuple(p for p in prot if p))

    def cancel(self, vid: str) -> None:
        t = self.tasks.get(vid)
        if t is not None and not t.done():
            t.cancel()

    # ---------------------------------------------------------------- 会话
    async def review(self, vid: str) -> None:
        run, rt, cfg = self.run, self.run.rt, self.run.cfg
        v = rt.graph.reviews.get(vid)
        if v is None or v.status != REV_RUNNING:
            return
        self.tasks[vid] = asyncio.current_task()
        tpath = str(Path(run.s.run_dir) / "reviews" / f"{vid}.jsonl")
        state = _State()
        worker = None
        error = ""
        try:
            if run.aux_llm is None:
                raise RuntimeError("no model for the reviewer")
            await run.repo.export_to(self.review_dir, v.tree, seed_index=run.repo.index(run.w))
            opening = await self.opening(vid)
            turns, sec = review_limits(cfg, v.trigger)
            from belay.worker.loop import Worker, WorkerConfig
            worker = Worker(run.aux_llm, SubdirEnv(run.env, self.review_dir), tools=self.tools(vid, state),
                            config=WorkerConfig(max_turns=turns, clear_tokens=cfg.l1_trigger_tokens,
                                                reset_tokens=10 ** 12, deadline=time.monotonic() + sec),
                            policy=self.policy(), transcript=Transcript(tpath), role="main",
                            system_prompt=REVIEWER_SYSTEM)
            res = await worker.run(opening)
            if state.verdict is None and res.status in ("no_tool_call", "max_turns"):
                state.verdict = self._json_verdict(worker.final_text)
                if state.verdict is None:                 # 再给一次机会：只要求给出结论
                    last = worker.messages[-1]
                    if last["role"] == "user" and isinstance(last["content"], list):
                        last["content"] = list(last["content"]) + [{"type": "text", "text": WRAPUP}]
                    else:
                        worker.messages.append({"role": "user", "content": WRAPUP})
                    worker.config.max_turns = worker.turns + 2
                    worker.config.deadline = time.monotonic() + 300
                    await worker._loop()
                    if state.verdict is None:
                        state.verdict = self._json_verdict(worker.final_text)
            if state.verdict is None:
                error = f"no verdict ({res.status} after {worker.turns} turns)"
        except asyncio.CancelledError:
            raise
        except Exception as e:                            # noqa: BLE001 — 复核者出故障：记为失败，由规则决定重试或降级
            error = f"{type(e).__name__}: {e}"
            run.log(f"review {vid} failed: {error}")
        finally:
            self.tasks.pop(vid, None)
            if worker is not None:
                self.usage.add(worker.usage)
            await self._cleanup()
        await rt.submit(R.record_review, vid, state.verdict, state.runs, state.verdict is None, error, tpath)

    @staticmethod
    def _json_verdict(text: str) -> Optional[dict]:
        data = extract_json(text or "", "merge") or extract_json(text or "", "requirements")
        return data if isinstance(data, dict) else None

    async def _cleanup(self) -> None:
        """复核结束：杀掉工作目录还在复核目录里的进程（复核者启动的服务、后台进程）。尽力而为。"""
        d = self.review_dir.rstrip("/")
        try:
            await self.run.env.run(
                f"for p in /proc/[0-9]*; do c=$(readlink $p/cwd 2>/dev/null) || continue; "
                f"case \"$c\" in {shlex.quote(d)}|{shlex.quote(d)}/*) kill -9 ${{p#/proc/}} 2>/dev/null;; esac; "
                f"done; true", timeout=60, cwd="/")
        except Exception:                                 # noqa: BLE001
            pass

    # ---------------------------------------------------------------- 开场
    async def opening(self, vid: str) -> str:
        run = self.run
        g = run.rt.graph
        cfg = run.cfg
        v = g.reviews[vid]
        base = g.checkpoints.get(v.base) if v.base is not None else g.head_cp
        parts = [f"<task>\n{g.run.task.strip()}\n</task>"]
        if v.attempt is not None:
            parts.append(f"## Merge request\nThis review is {TRIGGER_TEXT.get(v.trigger, v.trigger)}. Snapshot "
                         f"s{v.snapshot} against the previous merge point {checkpoint_line(g, base.id)}.")
        else:
            parts.append(f"## Judge only\nThis review is {TRIGGER_TEXT['judge']} ({checkpoint_line(g, base.id)}). "
                         "There is nothing to merge: give merge=false and judge the requirements in focus.")
        score, note, at = last_score(g)
        if score is not None:
            parts.append(f"Score at merge point {at}: {score:g}, measured as: {note or '(not described)'}. Measure "
                         "it the same way.")
        parts.append("## Regression gate (run by the harness on this snapshot)\n" + self._gate_text(vid))
        parts.append("## Requirements\nStatus is the ledger before this review. Focus: "
                     + (", ".join(v.focus) or "(none open)") + "\n" + self._requirements_text(vid))
        parts.append(self._claims_text(vid))
        prev = [x for x in sorted(g.reviews.values(), key=lambda x: x.seq)
                if x.id != vid and x.status == REV_DECIDED][-1:]
        for p in prev:
            d = p.decision
            parts.append(f"## The previous review ({p.id}, s{p.snapshot}, "
                         + ("merged" if d.get("merge") else "not merged" if d.get("merge") is False else "judge only")
                         + ")\n" + "\n".join(f"- {x[:300]}" for x in (d.get("reasons") or [])[:5])
                         + (f"\nFeedback given: {d.get('feedback', '')[:1200]}" if d.get("feedback") else ""))
        try:
            files = await run.repo.numstat(base.tree, v.tree) if base.tree != v.tree else []
            diff = await run.repo.diff(base.tree, v.tree) if files else ""
        except Exception as e:                            # noqa: BLE001
            files, diff = [], ""
            run.log(f"review {vid}: diff failed: {e}")
        reqs = [g.requirements[r] for r in v.focus if r in g.requirements] or actionable(g)
        parts.append(f"## Changes since merge point {base.id}\n" + diff_digest(reqs, diff, files,
                                                                             cfg.review_input_chars))
        snap = g.snapshots.get(v.snapshot)
        if snap is not None and snap.dropped:
            parts.append("The agent also changed these test files; they are not part of the snapshot (tests always "
                         "run in their original version): " + ", ".join(snap.dropped[:20]))
        spec = run.spec
        how = [f"Your working directory is {self.review_dir}: a copy of the snapshot, yours to build and run in."]
        if spec.test_cmd:
            how.append(f"The harness runs tests as: {spec.test_cmd}" + (f" (after: {spec.prelude})" if spec.prelude
                                                                         else "")
                       + ". run_tests runs chosen tests exactly that way, with the original test files.")
        else:
            how.append("No test command is configured for this task: verify by running the code (run) and reading "
                       "it.")
        parts.append("## How to work\n" + "\n".join(how))
        parts.append(self._scope_text(vid, base.id))
        return "\n\n".join(parts)

    def _scope_text(self, vid: str, base: int) -> str:
        g = self.run.rt.graph
        v = g.reviews[vid]
        done = [r.id for r in actionable(g) if r.status == REQ_DONE]
        lines = ["## Scope"]                       # 只说范围；轮数与时间的上限由 runtime 执行，从不写进提示词
        if v.trigger in BG_TRIGGERS:
            lines.append(f"This is a background check of the changes since merge point {base}, not a full audit. "
                         "Decide the merge from the gate result and these changes. Judge only the requirements in "
                         "focus that these changes work on; leave the others out of the verdict (their status stays "
                         "as it is). Verify what these changes do with targeted commands rather than re-exploring the "
                         "project.")
        elif v.attempt is not None:
            lines.append(f"Decide the merge from the gate result and the changes since merge point {base}, and judge "
                         "every requirement in focus.")
        if done:
            lines.append("Already done (do not re-verify them; check one only if these changes touch the code it "
                         "relies on, and then report it only if it broke): " + ", ".join(done))
        return "\n".join(lines)

    def _gate_text(self, vid: str) -> str:
        g = self.run.rt.graph
        v = g.reviews[vid]
        if not g.baseline:
            return "Not available: no tests are configured for this task."
        res = results_for_tree(g, v.tree)
        guard = active_guard(g)
        regs = v.gate.get("regressions") or []
        lines = [f"{len(guard)} checks passed on the original code" + (f" ({len(g.waived)} waived earlier)"
                                                                       if g.waived else "") + "."]
        if regs:
            reasons = reasons_for_tree(g, v.tree)
            lines.append(f"{len(regs)} of them do NOT pass on this snapshot. The agent proposed waivers for some "
                         "(below); grant a waiver only for tests that contradict explicit task text, and do not merge "
                         "otherwise:")
            for r in regs[:30]:
                why = reasons.get(regression_ids([r])[0])
                lines.append(f"  - {r}" + (f": {why[:300]}" if why else ""))
        elif v.attempt is not None:
            lines.append("All of them pass on this snapshot.")
        fixed = sorted(t for t, c in g.baseline.items() if c != B_PASS and res.get(t) == PASSED)
        if fixed:
            lines.append(f"Tests that failed on the original code and pass now ({len(fixed)}): "
                         + ", ".join(fixed[:30]) + (" ..." if len(fixed) > 30 else ""))
        return "\n".join(lines)

    def _requirements_text(self, vid: str) -> str:
        g = self.run.rt.graph
        v = g.reviews[vid]
        res = results_for_tree(g, v.tree)
        out = []
        for r in actionable(g):
            mark = "*" if r.id in v.focus else " "
            line = f"{mark} {r.id} [{requirement_state(r)}" + (f", merge point {r.checkpoint}" if r.status != REQ_OPEN
                                                              and r.checkpoint is not None else "") + \
                f"] \"{r.quote[:150 if r.status == REQ_DONE else 600]}\""
            extra = []
            if r.acceptance:
                extra.append(f"acceptance: {r.acceptance[:300]}")
            checks = [c for c in r.checks if g.baseline.get(c) != B_PASS]
            if checks:
                extra.append("linked tests: " + ", ".join(f"{c} ({res.get(c, 'not run')})" for c in checks[:8]))
            if r.status == REQ_DONE and r.evidence:
                extra.append("evidence: " + "; ".join(r.evidence[:2])[:300])
            if r.status == REQ_OPEN and r.missing:
                extra.append("missing last time: " + "; ".join(r.missing[:3])[:300])
            if r.status == "blocked":
                extra.append(f"blocked ({r.blocked_kind}): {(r.blocked_reason or '')[:200]}")
            out.append(line + "".join(f"\n    {x}" for x in extra))
        return "\n".join(out)

    def _claims_text(self, vid: str) -> str:
        g = self.run.rt.graph
        v = g.reviews[vid]
        lines = ["## What the agent says (self-reported, not verified)"]
        s = g.submits.get(v.submit) if v.submit else None
        if s is not None:
            lines.append(f"Submit summary: {s.summary[:3000] or '(empty)'}")
            for b in s.blocked:
                lines.append(f"Declared blocked: {b.get('requirement')} ({b.get('kind')}): {b.get('reason')}"
                             + (f" — quote: \"{b.get('quote')}\"" if b.get("quote") else ""))
            for w in s.waivers:
                lines.append(f"Proposed waiver of {', '.join(w.get('tests', [])[:10])}: \"{w.get('quote')}\" — "
                             f"{w.get('reason')}")
        todos = todos_in_order(g)
        if todos:
            mark = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]", "anchored": "[x]"}
            lines.append("Todo list:\n" + "\n".join(f"  {mark.get(t.status, '[ ]')} {t.title[:200]}"
                                                     for t in todos[:60]))
        notes = latest_handoff_summary(g, self.run.w)
        if notes:
            lines.append("Notes from its last handoff:\n" + notes[:2000])
        return "\n".join(lines) if len(lines) > 1 else lines[0] + "\n(nothing)"

    # ---------------------------------------------------------------- 工具
    def tools(self, vid: str, state: _State) -> list[Tool]:
        run, cfg = self.run, self.run.cfg
        rt = run.rt

        async def run_cmd(inp: dict, ctx: ToolContext) -> str:
            cmd = str(inp.get("command") or "").strip()
            if not cmd:
                raise ToolError("command is required")
            ctx.check_command(cmd)
            timeout = int(min(max(1, int(inp.get("timeout") or 300)), cfg.review_run_timeout_sec))
            rid = f"X{len(state.runs) + 1}"
            res = await ctx.env.run(cmd, timeout=timeout)
            state.runs.append({"id": rid, "cmd": cmd[:500], "rc": res.return_code})
            out = truncate_output(res.output.rstrip("\n"))
            note = " (timed out and killed)" if res.timed_out else ""
            return f"[run {rid}] exit code {res.return_code}{note}\n{out or '(no output)'}"

        async def run_tests(inp: dict, ctx: ToolContext) -> str:
            tests = [str(t).strip() for t in (inp.get("tests") or []) if str(t).strip()]
            if not tests:
                raise ToolError("tests is required: test ids or test files")
            if run.verifier is None or not run.spec.test_cmd:
                raise ToolError("No test command is configured for this task; use run instead.")
            v = rt.graph.reviews[vid]
            jid = await rt.submit(R.ensure_job, v.tree, units(tests), "review", tag="review")
            await rt.wait_until(lambda g: g.jobs[jid].state != JOB_RUNNING, timeout=cfg.review_run_timeout_sec * 2)
            g = rt.graph
            if g.jobs[jid].state == JOB_RUNNING:
                return f"Job {jid} is still running; call run_tests again later to read its result."
            return render_job(g, jid, only=tests)

        async def run_gate(inp: dict, ctx: ToolContext) -> str:
            return self._gate_text(vid)

        async def locate(inp: dict, ctx: ToolContext) -> str:
            test = str(inp.get("test") or "").strip()
            if not test:
                raise ToolError("test is required")
            g = rt.graph
            v = g.reviews[vid]
            if point_status(g, v.tree, test) != PT_FAIL:
                raise ToolError(f"{test} has no failing result on this snapshot (run it with run_tests first).")
            lid = await rt.submit(R.start_locate, [test], {"tree": v.tree, "snapshot": v.snapshot}, "review",
                                  ref=vid)
            if lid is None:
                return "Locating is not available here (no tests, locating disabled, or the run is finishing)."

            def done(g) -> bool:
                L = g.locates[lid]
                return L.status == "concluded" and len(L.results) >= len(L.groups)
            await rt.wait_until(done, timeout=cfg.review_locate_wait_sec)
            text = render_located(rt.graph, lid)
            return text or f"Still locating ({lid}); the result is not ready yet."

        async def verdict(inp: dict, ctx: ToolContext) -> str:
            if not isinstance(inp, dict) or "merge" not in inp:
                raise ToolError("verdict needs at least merge, reason, requirements and feedback")
            state.verdict = json.loads(json.dumps(inp))
            ctx.submitted = True
            return "Verdict recorded."

        by_name = {t.name: t for t in F.TOOLS}
        return [by_name["read_file"], by_name["list_files"], by_name["grep_search"],
                Tool("run", "Run a shell command in your working directory (the snapshot) and return its output and "
                            "exit code. Each call is a fresh shell; chain commands with &&. Every call gets an id "
                            "(X1, X2, ...) to cite as E2 evidence. Default timeout 300 s.",
                     {"type": "object", "properties": {"command": {"type": "string"},
                                                       "timeout": {"type": "integer"}}, "required": ["command"]},
                     run_cmd),
                Tool("run_tests", "Run chosen tests the way the regression gate does (original test files, separate "
                                  "directory) on this snapshot and return their results. Results are evidence (E3).",
                     {"type": "object", "properties": {"tests": {"type": "array", "items": {"type": "string"},
                                                                 "description": "Test ids or test files"}},
                      "required": ["tests"]}, run_tests),
                Tool("run_gate", "Show the regression gate's result on this snapshot again.",
                     {"type": "object", "properties": {}}, run_gate, read_only=True),
                Tool("locate", "Find the change after which a test started failing, by bisecting the agent's "
                               "snapshots (the test must fail on this snapshot).",
                     {"type": "object", "properties": {"test": {"type": "string"}}, "required": ["test"]}, locate),
                Tool("verdict", "Give your verdict. Call it exactly once, at the end.", VERDICT_SCHEMA, verdict)]
