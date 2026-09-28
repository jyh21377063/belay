"""worker 主循环：调模型 → 执行工具 → 把结果交回模型，直到 submit 或没有工具调用。

与 mini_claude（MIT）的主要差别：
  - 工具经由 Env 在容器内执行，循环跑在宿主机；
  - 保留 thinking 块（DeepSeek 思考模式下的工具调用要求带回）；
  - 上下文过长时不做原地摘要压缩，而是让模型写交接说明后重建上下文；
  - 每一步写入只追加的轨迹，被取消时不丢记录；
  - 同一个 Worker 类也用来跑只读的探索子 agent（role="explore"），由 explore 工具调起。
M2 起，run_check / submit 等工具会把请求投递给 Orchestrator；循环本身不变。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Callable

from belay.worker.context import HANDOFF_REQUEST, clear_stale_results
from belay.env import Env
from belay.llm import Response, Usage
from belay.worker.prompts import (EXPLORE_WRAPUP, explore_message, explore_system_prompt, initial_message,
                                  system_prompt)
from belay.tools import EXPLORE_TOOLS, Policy, RuntimeClient, Tool, ToolContext, ToolError, get_tools
from belay.worker.transcript import Transcript


@dataclass
class WorkerConfig:
    max_turns: int = 2000
    # 上下文阈值与 A 组对齐：Claude Code（deepseek-flash[1m]）约在 78.6 万 token 压缩，baseline 峰值 58 万从未触发。
    # 过早重建会让 B 组与 A 组的差异混入上下文策略；更积极的重建留给 Belay（M2 起从证据图重建）。
    clear_tokens: int = 500_000          # 上下文超过该值后开始清理过期的工具结果（改写旧消息会让前缀缓存失效）
    reset_tokens: int = 750_000          # 上下文超过该值时写交接说明并重建
    reset_diff_chars: int = 60_000       # 重建时附上的 git diff 长度上限
    todo_reminder_turns: int = 30        # 这么多轮没更新任务清单时，提醒一次（Claude Code 也有同样的提醒）
    deadline: float | None = None        # time.monotonic() 表示的截止时间
    time_reminders: bool = False         # 是否在工具结果中提示剩余时间（B 组关闭，与 Claude Code 一致）
    extra_rules: str = ""
    explore_max_turns: int = 40          # 探索子 agent 的轮数上限，到达后要求它根据已有发现写报告
    explore_report_chars: int = 20_000   # 返回给主 worker 的报告长度上限


@dataclass
class WorkerResult:
    status: str                          # submitted | no_tool_call | max_turns | deadline
    summary: str = ""
    turns: int = 0
    resets: int = 0
    usage: Usage = field(default_factory=Usage)
    peak_context: int = 0
    events: list[dict] = field(default_factory=list)
    final_text: str = ""                 # 最后一次没有工具调用时的回复（探索子 agent 的报告）


class Worker:
    def __init__(self, llm, env: Env, tools: list[Tool] | None = None, config: WorkerConfig | None = None,
                 policy: Policy | None = None, transcript: Transcript | None = None,
                 on_progress: Callable[["Worker"], None] | None = None,
                 runtime: RuntimeClient | None = None, work_id: str | None = None, role: str = "main"):
        self.llm = llm
        self.env = env
        self.role = role                                  # main | explore
        self.tools = {t.name: t for t in (tools if tools is not None else get_tools())}
        if role == "explore":                             # 子 agent 不能再开子 agent，也不能提交
            self.tools = {n: t for n, t in self.tools.items() if n in EXPLORE_TOOLS}
        self.config = config or WorkerConfig()
        self.ctx = ToolContext(env=env, workdir=env.workdir, policy=policy or Policy(),
                               runtime=runtime, work_id=work_id, read_only=(role == "explore"))
        if "explore" in self.tools:
            self.ctx.subagent = self._run_explorer
        self.transcript = transcript or Transcript(None)
        self.on_progress = on_progress
        self.usage = Usage()
        self.turns = 0
        self.resets = 0
        self.peak_context = 0
        self.last_context = 0
        self.messages: list[dict] = []
        self.final_text = ""
        self._last_todo_turn = 0
        self._events_written = 0
        self._explorers = 0

    # ---- 对外入口
    async def run(self, task: str) -> WorkerResult:
        platform = (await self.env.run("uname -sm", timeout=30)).output.strip()
        self.schemas = [t.schema() for t in self.tools.values()]
        self.task = task
        if self.role == "explore":
            self.system = explore_system_prompt(self.env.workdir, platform)
            first = explore_message(task)
        else:
            self.system = system_prompt(self.env.workdir, platform, self.config.extra_rules,
                                        has_explore="explore" in self.tools)
            first = initial_message(task, await self._snapshot(with_diff=False))
        self.messages = [{"role": "user", "content": first}]
        self.transcript.write("start", system=self.system, tools=[t.name for t in self.tools.values()],
                              first_message=self.messages[0]["content"])
        status = await self._loop()
        result = WorkerResult(status, self.ctx.summary, self.turns, self.resets, self.usage,
                              self.peak_context, list(self.ctx.events), self.final_text)
        self.transcript.write("end", status=status, summary=self.ctx.summary, turns=self.turns,
                              resets=self.resets, usage=self.usage.__dict__)
        return result

    # ---- 主循环
    async def _loop(self) -> str:
        while True:
            if self.turns >= self.config.max_turns:
                return "max_turns"
            if self.config.deadline and time.monotonic() >= self.config.deadline:
                return "deadline"
            if self.last_context > self.config.clear_tokens:
                n = clear_stale_results(self.messages)
                if n:
                    self.transcript.write("clear", cleared=n, context=self.last_context)

            resp = await self._call()
            self.turns += 1
            tool_uses = resp.tool_uses

            if resp.stop_reason == "max_tokens" and tool_uses:
                # 输出被截断时，最后的工具调用参数可能不完整：丢掉工具调用，提示模型重来
                kept = [b for b in resp.content if b.get("type") != "tool_use"] or [{"type": "text", "text": "(truncated)"}]
                self.messages.append({"role": "assistant", "content": kept})
                self.messages.append({"role": "user", "content": "Your previous response hit the output token limit "
                                      "and its tool calls were dropped. Continue, keeping each response shorter."})
                continue

            self.messages.append({"role": "assistant", "content": resp.content or [{"type": "text", "text": "(empty)"}]})
            if not tool_uses:
                self.final_text = resp.text
                return "no_tool_call"

            results = await self._execute(tool_uses)
            content: list[dict] = [{"type": "tool_result", "tool_use_id": tu["id"], "content": out, "is_error": err}
                                   for tu, (out, err) in zip(tool_uses, results)]
            reminder = self._reminder(tool_uses)
            if reminder:
                content.append({"type": "text", "text": reminder})
            self.messages.append({"role": "user", "content": content})
            self.transcript.write("tool_result", results=[{"id": tu["id"], "name": tu["name"], "error": err,
                                                           "output": out} for tu, (out, err) in zip(tool_uses, results)])
            self._flush_events()
            if self.on_progress:
                self.on_progress(self)

            if self.ctx.submitted:
                return "submitted"
            if self.last_context > self.config.reset_tokens:
                await self._reset()

    async def _call(self, tool_choice: dict | None = None, purpose: str = "turn") -> Response:
        """purpose 写进轨迹：turn 为正常的一轮；handoff、wrapup 为不计轮数的收尾调用。"""
        resp = await self.llm.call(self.system, self.schemas, self.messages, tool_choice=tool_choice)
        self.usage.add(resp.usage)
        self.last_context = resp.usage.context_tokens + resp.usage.output_tokens
        self.peak_context = max(self.peak_context, resp.usage.context_tokens)
        self.transcript.write("assistant", content=resp.content, stop_reason=resp.stop_reason,
                              usage=resp.usage.__dict__, context=self.last_context, purpose=purpose)
        return resp

    # ---- 工具执行：连续的只读调用并行，其余按顺序
    async def _execute(self, tool_uses: list[dict]) -> list[tuple[str, bool]]:
        results: list[tuple[str, bool] | None] = [None] * len(tool_uses)
        i = 0
        while i < len(tool_uses):
            j = i
            while j < len(tool_uses) and self._is_read_only(tool_uses[j]):
                j += 1
            if j > i:
                outs = await asyncio.gather(*(self._run_tool(tu) for tu in tool_uses[i:j]))
                results[i:j] = outs
                i = j
            else:
                results[i] = await self._run_tool(tool_uses[i])
                i += 1
        return results  # type: ignore[return-value]

    def _is_read_only(self, tu: dict) -> bool:
        tool = self.tools.get(tu["name"])
        return bool(tool and tool.read_only)

    async def _run_tool(self, tu: dict) -> tuple[str, bool]:
        tool = self.tools.get(tu["name"])
        if tool is None:
            return f"Error: unknown tool {tu['name']}", True
        try:
            return await tool.handler(tu.get("input") or {}, self.ctx), False
        except ToolError as e:
            return f"Error: {e}", True
        except asyncio.CancelledError:
            raise
        except Exception as e:                        # 工具实现的意外错误也交给模型，不中断循环
            self.ctx.event("tool_crash", tool=tu["name"], error=f"{type(e).__name__}: {e}")
            return f"Error: {type(e).__name__}: {e}", True

    def _reminder(self, tool_uses: list[dict]) -> str:
        notes = []
        if any(tu["name"] == "todo_write" for tu in tool_uses):
            self._last_todo_turn = self.turns
        elif ("todo_write" in self.tools and self.role == "main"
              and self.turns - self._last_todo_turn >= self.config.todo_reminder_turns):
            self._last_todo_turn = self.turns
            notes.append("The todo list has not been updated recently. If it no longer matches what you are doing, "
                         "update it; if it is not useful for this task, ignore this note.")
        if self.config.time_reminders and self.config.deadline:
            left = (self.config.deadline - time.monotonic()) / 60
            if self.turns % 20 == 0 or left < 15:
                notes.append(f"About {max(0, left):.0f} minutes of the time budget remain.")
        return "".join(f"<system-reminder>{n}</system-reminder>" for n in notes)

    def _flush_events(self) -> None:
        for e in self.ctx.events[self._events_written:]:
            self.transcript.write("event", **e)
        self._events_written = len(self.ctx.events)

    # ---- 上下文重建
    async def _snapshot(self, with_diff: bool) -> str:
        """仓库状态。首次只给 status 与 stat；重建时附上完整 diff，让模型看到自己改成了什么。"""
        diff = (f"echo; echo '$ git --no-pager diff'; git --no-pager diff | head -c {self.config.reset_diff_chars}; "
                f"[ $(git --no-pager diff | wc -c) -gt {self.config.reset_diff_chars} ] && "
                "echo && echo '[... diff truncated; run git diff to see the rest]'; ") if with_diff else ""
        res = await self.env.run("git rev-parse --is-inside-work-tree >/dev/null 2>&1 && "
                                 "{ echo '$ git status --short'; git status --short | head -60; "
                                 "echo; echo '$ git diff --stat'; git --no-pager diff --stat | tail -40; "
                                 f"{diff}true; }} "
                                 "|| { echo '$ ls'; ls -la | head -60; }", timeout=120)
        return f"Repository root: {self.env.workdir}\n{res.output.strip()}"

    async def _reset(self) -> None:
        last = self.messages[-1]
        last["content"] = list(last["content"]) + [{"type": "text", "text": HANDOFF_REQUEST}]
        resp = await self._call(tool_choice={"type": "none"}, purpose="handoff")
        handoff = resp.text or "(no handoff note was written)"
        self.resets += 1
        self.ctx.file_digests.clear()                # 上下文已丢失，编辑前需要重新读取
        self.messages = [{"role": "user", "content": initial_message(self.task, await self._snapshot(with_diff=True),
                                                                     handoff, self.ctx.todos)}]
        self._last_todo_turn = self.turns                # 任务清单已在首条消息里，不必马上提醒
        self.last_context = 0
        self.transcript.write("reset", handoff=handoff, resets=self.resets)

    # ---- 收尾：要求模型不调用工具、直接给出文字（探索子 agent 到达轮数上限时用）
    async def conclude(self, prompt: str) -> str:
        last = self.messages[-1]
        if last["role"] == "user" and isinstance(last["content"], list):
            last["content"] = list(last["content"]) + [{"type": "text", "text": prompt}]
        else:
            self.messages.append({"role": "user", "content": prompt})
        resp = await self._call(tool_choice={"type": "none"}, purpose="wrapup")
        self.final_text = resp.text
        return resp.text

    # ---- 只读探索子 agent（explore 工具经 ToolContext.subagent 调到这里）
    async def _run_explorer(self, description: str, question: str) -> str:
        self._explorers += 1
        n = self._explorers
        before = await self._worktree_fingerprint()
        child = Worker(self.llm, self.env, tools=get_tools(EXPLORE_TOOLS), role="explore", policy=self.ctx.policy,
                       config=WorkerConfig(max_turns=self.config.explore_max_turns,
                                           clear_tokens=self.config.clear_tokens,
                                           reset_tokens=10 ** 12,            # 子 agent 不重建上下文
                                           deadline=self.config.deadline),
                       transcript=self.transcript.sibling(f"explore-{n}"))
        self.transcript.write("subagent_start", id=n, description=description, question=question)
        try:
            res = await child.run(question)
            report = res.final_text
            if res.status == "max_turns":
                report = await child.conclude(EXPLORE_WRAPUP)
            elif res.status == "deadline":
                report = report or "(Exploration stopped: the time budget ran out before a report was written.)"
        finally:
            self.usage.add(child.usage)                   # 子 agent 的用量计入本 worker
            for e in child.ctx.events:                    # 越界事件带上来源，一并统计
                self.ctx.event(e["kind"], **{k: v for k, v in e.items() if k not in ("kind", "t")},
                               source=f"explore-{n}")

        after = await self._worktree_fingerprint()
        note = ""
        if before is not None and after != before:
            # 正则只能挡住明显的写命令；这里兜底检查工作区是否被改动（并行时可能来自同一时间的其他调用）
            self.ctx.event("explore_modified_worktree", source=f"explore-{n}")
            note = ("\n\n[Warning: the working tree changed while this exploration ran. The subagent is supposed to "
                    "be read-only; check `git status` before relying on your view of the files.]")
        self.transcript.write("subagent_end", id=n, status=res.status, turns=child.turns,
                              usage=child.usage.__dict__, report_chars=len(report), modified=bool(note))
        report = (report or "(The exploration returned no report.)").strip()
        if len(report) > self.config.explore_report_chars:
            report = report[:self.config.explore_report_chars] + "\n[... report truncated]"
        return report + note

    async def _worktree_fingerprint(self) -> str | None:
        """工作区状态的指纹（未跟踪文件列表 + 相对 HEAD 的 diff）；不是 git 仓库时返回 None。"""
        res = await self.env.run("git rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 3; "
                                 "{ git status --porcelain -uall; git diff HEAD; } | sha256sum | cut -c1-64", timeout=120)
        return res.output.strip() if res.return_code == 0 else None
