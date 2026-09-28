"""worker 主循环：调模型 → 执行工具 → 把结果交回模型，直到 submit 或没有工具调用。

与 mini_claude（MIT）的主要差别：
  - 工具经由 Env 在容器内执行，循环跑在宿主机；
  - 保留 thinking 块（DeepSeek 思考模式下的工具调用要求带回）；
  - 上下文过长时不做原地摘要压缩，而是让模型写交接说明后重建上下文；
  - 每一步写入只追加的轨迹，被取消时不丢记录。
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
from belay.worker.prompts import initial_message, system_prompt
from belay.tools import Policy, RuntimeClient, Tool, ToolContext, ToolError, get_tools
from belay.worker.transcript import Transcript


@dataclass
class WorkerConfig:
    max_turns: int = 2000
    clear_tokens: int = 120_000          # 上下文超过该值后开始清理过期的工具结果
    reset_tokens: int = 250_000          # 上下文超过该值时写交接说明并重建
    deadline: float | None = None        # time.monotonic() 表示的截止时间
    time_reminders: bool = False         # 是否在工具结果中提示剩余时间（B 组关闭，与 Claude Code 一致）
    extra_rules: str = ""


@dataclass
class WorkerResult:
    status: str                          # submitted | no_tool_call | max_turns | deadline
    summary: str = ""
    turns: int = 0
    resets: int = 0
    usage: Usage = field(default_factory=Usage)
    peak_context: int = 0
    events: list[dict] = field(default_factory=list)


class Worker:
    def __init__(self, llm, env: Env, tools: list[Tool] | None = None, config: WorkerConfig | None = None,
                 policy: Policy | None = None, transcript: Transcript | None = None,
                 on_progress: Callable[["Worker"], None] | None = None,
                 runtime: RuntimeClient | None = None, work_id: str | None = None):
        self.llm = llm
        self.env = env
        self.tools = {t.name: t for t in (tools or get_tools())}
        self.config = config or WorkerConfig()
        self.ctx = ToolContext(env=env, workdir=env.workdir, policy=policy or Policy(),
                               runtime=runtime, work_id=work_id)
        self.transcript = transcript or Transcript(None)
        self.on_progress = on_progress
        self.usage = Usage()
        self.turns = 0
        self.resets = 0
        self.peak_context = 0
        self.last_context = 0
        self.messages: list[dict] = []
        self._events_written = 0

    # ---- 对外入口
    async def run(self, task: str) -> WorkerResult:
        platform = (await self.env.run("uname -sm", timeout=30)).output.strip()
        self.system = system_prompt(self.env.workdir, platform, self.config.extra_rules)
        self.schemas = [t.schema() for t in self.tools.values()]
        self.task = task
        self.messages = [{"role": "user", "content": initial_message(task, await self._snapshot())}]
        self.transcript.write("start", system=self.system, tools=[t.name for t in self.tools.values()],
                              first_message=self.messages[0]["content"])
        status = await self._loop()
        result = WorkerResult(status, self.ctx.summary, self.turns, self.resets, self.usage,
                              self.peak_context, list(self.ctx.events))
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
                return "no_tool_call"

            results = await self._execute(tool_uses)
            content: list[dict] = [{"type": "tool_result", "tool_use_id": tu["id"], "content": out, "is_error": err}
                                   for tu, (out, err) in zip(tool_uses, results)]
            reminder = self._reminder()
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

    async def _call(self, tool_choice: dict | None = None) -> Response:
        resp = await self.llm.call(self.system, self.schemas, self.messages, tool_choice=tool_choice)
        self.usage.add(resp.usage)
        self.last_context = resp.usage.context_tokens + resp.usage.output_tokens
        self.peak_context = max(self.peak_context, resp.usage.context_tokens)
        self.transcript.write("assistant", content=resp.content, stop_reason=resp.stop_reason,
                              usage=resp.usage.__dict__, context=self.last_context)
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

    def _reminder(self) -> str:
        if not (self.config.time_reminders and self.config.deadline):
            return ""
        left = (self.config.deadline - time.monotonic()) / 60
        if self.turns % 20 == 0 or left < 15:
            return f"<system-reminder>About {max(0, left):.0f} minutes of the time budget remain.</system-reminder>"
        return ""

    def _flush_events(self) -> None:
        for e in self.ctx.events[self._events_written:]:
            self.transcript.write("event", **e)
        self._events_written = len(self.ctx.events)

    # ---- 上下文重建
    async def _snapshot(self) -> str:
        res = await self.env.run("git rev-parse --is-inside-work-tree >/dev/null 2>&1 && "
                                 "{ echo '$ git status --short'; git status --short | head -40; "
                                 "echo; echo '$ git diff --stat'; git diff --stat | tail -40; } "
                                 "|| { echo '$ ls'; ls -la | head -60; }", timeout=60)
        return f"Repository root: {self.env.workdir}\n{res.output.strip()}"

    async def _reset(self) -> None:
        last = self.messages[-1]
        last["content"] = list(last["content"]) + [{"type": "text", "text": HANDOFF_REQUEST}]
        resp = await self._call(tool_choice={"type": "none"})
        handoff = resp.text or "(no handoff note was written)"
        self.resets += 1
        self.ctx.read_files.clear()                  # 上下文已丢失，编辑前需要重新读取
        self.messages = [{"role": "user", "content": initial_message(self.task, await self._snapshot(), handoff)}]
        self.last_context = 0
        self.transcript.write("reset", handoff=handoff, resets=self.resets)
