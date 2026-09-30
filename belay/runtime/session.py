"""Belay 的会话循环：调模型 → 执行工具 → 把结果交回模型，并在每一轮之前做分层的上下文管理。

与 B 组 worker（belay/worker/loop.py）共用工具实现、模型客户端和系统提示主体；差别是：
  - 会话由 runtime 开启和结束，开场上下文来自 build_context；
  - 分层压缩：L0 大输出落盘、L1 清理过期结果、L2 用图替换旧对话、L3 模型只写图里没有的东西、L4 交接；
  - 每个工具调用都是心跳（租约续期）；runtime 的通知以 <system-reminder> 注入。
会话返回时给出结束原因：done（模型不再调用工具）| handoff（L4）| deadline（runtime 要求停止）| max_turns。
崩溃（模型接口多次重试仍失败等）以异常的形式交给驱动，记为 crash。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional, Protocol

from belay.core.compact import count_results, l0_shrink, l1_clear, l2_rebuild, messages_tokens
from belay.core.config import BelayConfig
from belay.env import Env
from belay.llm import Response, Usage
from belay.runtime.prompts import FULL_SUMMARY_PROMPT, L3_PROMPT
from belay.tools import Policy, Tool, ToolContext, ToolError
from belay.worker.transcript import Transcript


class SessionHooks(Protocol):
    def activity(self, busy: int = 0) -> None: ...     # busy=+1/-1：工具调用开始 / 结束（进行中的工具调用不算卡死）
    def heartbeat(self) -> None: ...
    def notices(self) -> list[str]: ...
    def should_stop(self) -> bool: ...
    def store_blob(self, text: str) -> str: ...
    async def compaction_opening(self) -> str: ...
    async def record_compaction(self, level: int, before: int, after: int, summary: Optional[str] = None) -> None: ...


@dataclass
class SessionOutcome:
    reason: str
    turns: int = 0
    peak_context: int = 0
    usage: Usage = field(default_factory=Usage)
    todos: list[dict] = field(default_factory=list)
    compactions: list[tuple[int, int, int]] = field(default_factory=list)
    final_text: str = ""


class BelaySession:
    def __init__(self, llm, env: Env, tools: list[Tool], system: str, opening: str, hooks: SessionHooks,
                 cfg: BelayConfig, runtime_client=None, policy: Optional[Policy] = None,
                 transcript: Optional[Transcript] = None,
                 subagent: Optional[Callable[[str, str], Awaitable[str]]] = None):
        self.llm = llm
        self.env = env
        self.tools = {t.name: t for t in tools}
        self.schemas = [t.schema() for t in tools]
        self.system = system
        self.hooks = hooks
        self.cfg = cfg
        self.ctx = ToolContext(env=env, workdir=env.workdir, policy=policy or Policy(), runtime=runtime_client)
        if subagent is not None and "explore" in self.tools:
            self.ctx.subagent = subagent
        self.transcript = transcript or Transcript(None)
        self.messages: list[dict] = [{"role": "user", "content": opening}]
        self.turns = 0
        self.usage = Usage()
        self.last_context = messages_tokens(system, self.messages, cfg.chars_per_token)
        self.peak_context = 0
        self.compactions: list[tuple[int, int, int]] = []
        self.result_meta: dict[str, dict] = {}        # tool_use id → {exit, path}（L1 占位用）
        self.modified: list[str] = []                 # 本会话改过的文件，最近的在后（L2 重读）
        self.final_text = ""

    # ---------------------------------------------------------------- 主循环
    async def run(self) -> SessionOutcome:
        self.transcript.write("start", system=self.system, tools=list(self.tools), first_message=self.messages[0]["content"])
        reason = await self._loop()
        self.transcript.write("end", reason=reason, turns=self.turns, usage=self.usage.__dict__)
        return SessionOutcome(reason, self.turns, self.peak_context, self.usage, list(self.ctx.todos),
                              list(self.compactions), self.final_text)

    async def _loop(self) -> str:
        while True:
            if self.hooks.should_stop():
                return "deadline"
            if self.turns >= self.cfg.max_turns_per_session:
                return "max_turns"
            if await self._manage_context():
                return "handoff"
            resp = await self._call()
            self.turns += 1
            self.hooks.activity()
            tool_uses = resp.tool_uses
            if resp.stop_reason == "max_tokens" and tool_uses:
                kept = [b for b in resp.content if b.get("type") != "tool_use"] or [{"type": "text", "text": "(truncated)"}]
                self.messages.append({"role": "assistant", "content": kept})
                self.messages.append({"role": "user", "content": "Your previous response was cut off before its tool "
                                      "calls were complete, so none of them ran. Continue from there; if a single "
                                      "call is very large (for example writing a big file), split it into several."})
                continue
            self.messages.append({"role": "assistant", "content": resp.content or [{"type": "text", "text": "(empty)"}]})
            if not tool_uses:
                self.final_text = resp.text
                return "done"
            results = await self._execute(tool_uses)
            content: list[dict] = []
            for tu, (out, err) in zip(tool_uses, results):
                content.append({"type": "tool_result", "tool_use_id": tu["id"], "content": self._l0(tu, out),
                                "is_error": err})
            notes = self.hooks.notices()
            if notes:
                content.append({"type": "text", "text": "".join(f"<system-reminder>{n}</system-reminder>" for n in notes)})
            self.messages.append({"role": "user", "content": content})
            self.transcript.write("tool_result", results=[{"id": tu["id"], "name": tu["name"], "error": err,
                                                           "output": out} for tu, (out, err) in zip(tool_uses, results)],
                                  notices=notes)

    async def _call(self, tool_choice: Optional[dict] = None, messages: Optional[list[dict]] = None,
                    purpose: str = "turn") -> Response:
        resp = await self.llm.call(self.system, self.schemas, messages or self.messages, tool_choice=tool_choice)
        self.usage.add(resp.usage)
        if purpose == "turn":
            self.last_context = resp.usage.context_tokens + resp.usage.output_tokens
            self.peak_context = max(self.peak_context, resp.usage.context_tokens)
        self.transcript.write("assistant", content=resp.content, stop_reason=resp.stop_reason,
                              usage=resp.usage.__dict__, context=self.last_context, purpose=purpose)
        return resp

    # ---------------------------------------------------------------- 工具
    async def _execute(self, tool_uses: list[dict]) -> list[tuple[str, bool]]:
        results: list = [None] * len(tool_uses)
        i = 0
        while i < len(tool_uses):
            j = i
            while j < len(tool_uses) and self._read_only(tool_uses[j]):
                j += 1
            if j > i:
                results[i:j] = await asyncio.gather(*(self._run_tool(tu) for tu in tool_uses[i:j]))
                i = j
            else:
                results[i] = await self._run_tool(tool_uses[i])
                i += 1
        return results

    def _read_only(self, tu: dict) -> bool:
        t = self.tools.get(tu["name"])
        return bool(t and t.read_only)

    async def _run_tool(self, tu: dict) -> tuple[str, bool]:
        self.hooks.heartbeat()
        tool = self.tools.get(tu["name"])
        if tool is None:
            return f"Error: unknown tool {tu['name']}", True
        inp = tu.get("input") or {}
        self.hooks.activity(+1)
        try:
            out = await tool.handler(inp, self.ctx), False
        except ToolError as e:
            out = f"Error: {e}", True
        except asyncio.CancelledError:
            raise
        except Exception as e:
            out = f"Error: {type(e).__name__}: {e}", True
        finally:
            self.hooks.activity(-1)
        if tu["name"] in ("edit_file", "write_file") and not out[1] and inp.get("file_path"):
            path = self.ctx.resolve(inp["file_path"])
            if path in self.modified:
                self.modified.remove(path)
            self.modified.append(path)
        return out

    def _l0(self, tu: dict, out: str) -> str:
        """L0：单个工具结果超过阈值时全文落盘，上下文保留开头、报错行、结尾和路径。"""
        if len(out) <= self.cfg.l0_chars:
            return out
        path = self.hooks.store_blob(out)
        self.result_meta[tu["id"]] = {"path": path}
        return l0_shrink(out, path, self.cfg.l0_chars, self.cfg.l0_head_lines, self.cfg.l0_tail_lines,
                         self.cfg.l0_signal_lines)

    # ---------------------------------------------------------------- 分层的上下文管理
    def _estimate(self, messages: Optional[list[dict]] = None) -> int:
        return messages_tokens(self.system, messages if messages is not None else self.messages,
                               self.cfg.chars_per_token)

    async def _manage_context(self) -> bool:
        """每轮调模型之前执行。返回 True 表示应当交接（L4）。"""
        cfg = self.cfg
        if len(self.messages) < 3:
            return False
        before = self.last_context
        big = [c for c in self.compactions if c[0] >= 2]
        need_l2 = before >= cfg.l2_tokens
        if before >= cfg.l4_tokens or (need_l2 and len(big) >= cfg.l4_max_compactions):
            if cfg.handoff_summary and cfg.l3_mode != "off" and cfg.graph_context:
                summary = await self._summary(L3_PROMPT)
                if summary:
                    await self.hooks.record_compaction(4, before, 0, summary)
            self.transcript.write("handoff", context=before, compactions=len(self.compactions))
            return True
        if need_l2:
            await self._l2_l3(before)
            return False
        if count_results(self.messages) > cfg.l1_trigger_results or before >= cfg.l1_trigger_tokens:
            new, n = l1_clear(self.messages, cfg.l1_keep_recent, self.result_meta)
            if n:
                self.messages = new
                after = self._estimate()
                self.last_context = min(self.last_context, after)
                self.compactions.append((1, before, after))
                await self.hooks.record_compaction(1, before, after)
                self.transcript.write("compact", level=1, cleared=n, before=before, after=after)
        return False

    async def _l2_l3(self, before: int) -> None:
        cfg = self.cfg
        cpt = cfg.chars_per_token
        if not cfg.graph_context:                   # 消融：旧对话换成模型写的完整摘要（不用图）
            summary = await self._summary(FULL_SUMMARY_PROMPT)
            opening = (self.messages[0]["content"] if isinstance(self.messages[0]["content"], str) else "")
            task = opening.split("</task>")[0] + "</task>" if "</task>" in opening else opening[:4000]
            new = l2_rebuild(f"{task}\n\n## Summary of the conversation so far\n{summary}", self.messages,
                             cfg.l3_keep_recent_tokens, "", cpt)
            self._install(new, [])
            after = self._estimate()
            self.compactions.append((3, before, after))
            await self.hooks.record_compaction(3, before, after, summary)
            self.transcript.write("compact", level=3, before=before, after=after, mode="summary")
            return
        opening = await self.hooks.compaction_opening()
        reread, paths = await self._reread()
        new = l2_rebuild(opening, self.messages, cfg.l2_keep_recent_tokens, reread, cpt)
        level, summary = 2, None
        over = self._estimate(new) > cfg.l2_tokens * cfg.l2_target_frac
        if cfg.l3_mode == "always" or (cfg.l3_mode == "overflow" and over):
            summary = await self._summary(L3_PROMPT)
            if summary:
                level = 3
                opening += ("\n## Summary of your reasoning before this compaction\n(model-written summary, may be "
                            f"incomplete)\n{summary.strip()}\n")
                new = l2_rebuild(opening, self.messages, cfg.l3_keep_recent_tokens, reread, cpt)
        self._install(new, paths)
        after = self._estimate()
        self.compactions.append((level, before, after))
        await self.hooks.record_compaction(level, before, after, summary)
        self.transcript.write("compact", level=level, before=before, after=after, reread=paths)

    def _install(self, messages: list[dict], keep_digests: list[str]) -> None:
        self.messages = messages
        self.ctx.file_digests = {p: d for p, d in self.ctx.file_digests.items() if p in keep_digests}
        self.last_context = self._estimate()

    async def _summary(self, prompt: str) -> str:
        msgs = [dict(m) for m in self.messages]
        last = msgs[-1]
        if last["role"] == "user" and isinstance(last["content"], list):
            msgs[-1] = {"role": "user", "content": list(last["content"]) + [{"type": "text", "text": prompt}]}
        elif last["role"] == "user":
            msgs[-1] = {"role": "user", "content": f"{last['content']}\n\n{prompt}"}
        else:
            msgs.append({"role": "user", "content": prompt})
        try:
            resp = await self._call(tool_choice={"type": "none"}, messages=msgs, purpose="summary")
        except asyncio.CancelledError:
            raise
        except Exception as e:                        # 摘要失败不影响正确性：事实都在图里
            self.transcript.write("summary_failed", error=f"{type(e).__name__}: {e}")
            return ""
        return resp.text.strip()

    async def _reread(self) -> tuple[str, list[str]]:
        """L2：重读最近改过的几个文件，并记下它们的 sha256（这样不必再读一遍就能编辑）。"""
        out, paths = [], []
        for path in reversed(self.modified[-self.cfg.l2_reread_files:]):
            try:
                total, text, digest = await self.env.read_lines(path, 1, 100000)
            except Exception:
                continue
            if "\x00" in text:
                continue
            lines = text.split("\n")
            body = "\n".join(f"{i + 1:6d}\t{line}" for i, line in enumerate(lines) if i < total)
            if len(body) > self.cfg.l2_reread_chars:
                body = body[:self.cfg.l2_reread_chars] + "\n[... truncated; read_file with offset for the rest]"
                # 截断的文件不记录 digest：编辑前必须重新读取
            else:
                self.ctx.file_digests[path] = digest
                paths.append(path)
            out.append(f"### {path}\n```\n{body}\n```")
        return "\n".join(out), paths
