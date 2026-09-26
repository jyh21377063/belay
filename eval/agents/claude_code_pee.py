"""PEE 组：基于 Claude Code 的朴素 planner → executor → evaluator（对应 Anthropic 的三 agent harness）。

流程（全部是同一容器内的 `claude -p` 会话，环境变量、模型配置、网络白名单与 A 组完全相同）：
  1. Planner：只读，列出需求清单与验证方式；
  2. Executor：按任务与清单实现；
  3. Evaluator：每轮开一个全新会话，只读，可跑测试，最后输出 {"pass": bool, "issues": [...]}；
  4. 未通过：把 issues 交回 Executor（--resume 延续其会话）继续修改；
     直到 Evaluator 通过、达到 max_rounds，或 90 分钟预算用完（由 Pier 终止，改动照常导出）。

实现方式：复用 Pier ClaudeCode.run 拼好的启动命令，只在 exec_as_agent 这一层追加参数（--resume、禁用编辑工具）
并把每个会话的输出写到 /logs/agent/pee/<序号>-<角色>.jsonl，同时追加到 claude-code.txt，
便于 eval.watch / eval.analyze 查看。只读角色通过 --disallowedTools 去掉编辑工具，并在提示中要求不修改文件。
"""
from __future__ import annotations

import asyncio
import json
import re
import shlex
from pathlib import Path

from pier.agents.installed.claude_code import ClaudeCode

from eval.agents.claude_code import ClaudeCodeWithPatch

READONLY_FLAGS = '--disallowedTools "Edit,Write,MultiEdit,NotebookEdit"'

PLANNER = """You are the PLANNER in a planner / executor / evaluator team. Do not modify any files.

Read the task below and inspect the repository as needed. Produce a numbered checklist of every concrete requirement
that must be satisfied for the task to be complete. For each item, state how it can be verified (which command,
test, or observable behavior). Cover everything the task asks for, including items that look minor.
Output only the checklist.

<task>
{task}
</task>"""

EXECUTOR = """{task}

---
A planner has broken this task into the following checklist. Implement every item and verify your work before finishing.

<checklist>
{plan}
</checklist>"""

EVALUATOR = """You are an independent EVALUATOR. Do not modify, create, or delete any files, and do not commit.

Another agent was given the task below and claims to have completed it. Inspect the current state of the repository
(for example `git status` and `git diff`), read the relevant code, and run tests or commands to check the work.
Decide whether the task is fully and correctly completed, with every checklist item satisfied and no existing
behavior broken.

<task>
{task}
</task>

<checklist>
{plan}
</checklist>

End your answer with a single JSON object on its own line, exactly in this form:
{{"pass": true or false, "issues": ["concrete, actionable problem 1", "..."]}}"""

FIX = """An independent evaluator reviewed your work and found these problems:

{issues}

Address all of them, then verify your work before finishing."""


def parse_verdict(text: str) -> tuple[bool, list[str], bool]:
    """返回 (pass, issues, 是否成功解析)。取最后一个包含 "pass" 的 JSON 对象。"""
    for m in reversed(list(re.finditer(r"\{[^{}]*\"pass\"[^{}]*\}", text or "", re.S))):
        try:
            v = json.loads(m.group(0))
            issues = [str(x) for x in (v.get("issues") or [])]
            return bool(v.get("pass")), issues, True
        except json.JSONDecodeError:
            continue
    return False, [(text or "(evaluator produced no output)")[-2000:]], False


class ClaudeCodePEE(ClaudeCodeWithPatch):
    def __init__(self, *args, max_rounds: int = 5, **kwargs):
        self.max_rounds = int(max_rounds)
        self._pee_role: str | None = None
        self._pee_flags = ""
        self._pee_seq = 0
        self._pee_results: list[dict] = []
        super().__init__(*args, **kwargs)

    # ---- 改写 Pier 拼好的 claude 启动命令
    async def exec_as_agent(self, environment, command, env=None, cwd=None, timeout_sec=None):
        if self._pee_role and "claude --verbose" in command:
            log = f"/logs/agent/pee/{self._pee_role}.jsonl"
            command = command.replace("--print --", f"{self._pee_flags} --print --", 1)
            command = command.replace("tee /logs/agent/claude-code.txt", f"tee -a {log} /logs/agent/claude-code.txt", 1)
        return await super().exec_as_agent(environment, command=command, env=env, cwd=cwd, timeout_sec=timeout_sec)

    async def _session(self, role: str, prompt: str, environment, context, resume: str | None = None,
                       readonly: bool = False) -> dict:
        self._pee_seq += 1
        name = f"{self._pee_seq:02d}-{role}"
        flags = []
        if resume:
            flags.append(f"--resume {shlex.quote(resume)}")
        if readonly:
            flags.append(READONLY_FLAGS)
        self._pee_role, self._pee_flags = name, " ".join(flags)
        error = None
        try:
            await ClaudeCode.run(self, prompt, environment, context)     # 不经过 ClaudeCodeWithPatch.run（不导出补丁）
        except asyncio.CancelledError:
            raise
        except Exception as e:                                          # 单个会话失败不中断整体流程
            error = f"{type(e).__name__}: {str(e)[:300]}"
        finally:
            self._pee_role, self._pee_flags = None, ""
        info = self._read_session(name)
        info.update(role=role, name=name, error=error)
        self._pee_results.append(info)
        self._write_summary()
        return info

    def _read_session(self, name: str) -> dict:
        path = Path(self.logs_dir) / "pee" / f"{name}.jsonl"
        info: dict = {"session_id": None, "result": "", "usage": {}, "cost_usd": 0.0, "num_turns": 0}
        if not path.exists():
            return info
        for line in path.read_text(errors="replace").splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("type") == "system" and e.get("session_id"):
                info["session_id"] = info["session_id"] or e["session_id"]
            elif e.get("type") == "result":
                info["result"] = e.get("result") or ""
                info["usage"] = e.get("usage") or {}
                info["cost_usd"] = e.get("total_cost_usd") or 0.0
                info["num_turns"] = e.get("num_turns") or 0
                info["session_id"] = e.get("session_id") or info["session_id"]
        return info

    def _write_summary(self) -> None:
        out = [{k: v for k, v in r.items() if k != "result"} | {"result_head": (r.get("result") or "")[:500]}
               for r in self._pee_results]
        (Path(self.logs_dir) / "pee").mkdir(parents=True, exist_ok=True)
        (Path(self.logs_dir) / "pee" / "summary.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))

    # ---- 主流程
    async def run(self, instruction, environment, context) -> None:
        agent_dir = environment.env_paths.agent_dir
        await self._belay_exec(environment, f"mkdir -p {agent_dir}/pee && chmod 777 {agent_dir}/pee", check=False)
        try:
            plan = await self._session("planner", PLANNER.format(task=instruction), environment, context, readonly=True)
            checklist = plan["result"].strip() or "(the planner produced no checklist; follow the task description)"

            ex = await self._session("executor", EXECUTOR.format(task=instruction, plan=checklist), environment, context)
            exec_sid = ex["session_id"]
            for round_ in range(1, self.max_rounds + 1):
                ev = await self._session("evaluator", EVALUATOR.format(task=instruction, plan=checklist),
                                         environment, context, readonly=True)
                passed, issues, parsed = parse_verdict(ev["result"])
                self._pee_results[-1].update(verdict_pass=passed, verdict_parsed=parsed, issues=issues[:20], round=round_)
                self._write_summary()
                if passed or not exec_sid:
                    break
                fix = FIX.format(issues="\n".join(f"- {i}" for i in issues[:20]))
                ex = await self._session("executor", fix, environment, context, resume=exec_sid)
                exec_sid = ex["session_id"] or exec_sid
        finally:
            await asyncio.shield(self._belay_export_patch(environment, kill_pattern="claude"))

    def populate_context_post_run(self, context) -> None:
        """按各会话 result 事件中的用量汇总（Pier 默认只解析单个会话）。"""
        inp = cache = out = turns = 0
        cost = 0.0
        for r in self._pee_results:
            u = r.get("usage") or {}
            cache += u.get("cache_read_input_tokens", 0) or 0
            inp += (u.get("input_tokens", 0) or 0) + (u.get("cache_read_input_tokens", 0) or 0) + \
                   (u.get("cache_creation_input_tokens", 0) or 0)
            out += u.get("output_tokens", 0) or 0
            cost += r.get("cost_usd") or 0.0
            turns += r.get("num_turns") or 0
        context.n_input_tokens, context.n_cache_tokens, context.n_output_tokens = inp, cache, out
        context.cost_usd, context.n_agent_steps = cost, turns
        context.metadata = dict(context.metadata or {}, pee_sessions=len(self._pee_results),
                                pee_rounds=max((r.get("round", 0) for r in self._pee_results), default=0),
                                pee_final_pass=next((r.get("verdict_pass") for r in reversed(self._pee_results)
                                                     if "verdict_pass" in r), None))
