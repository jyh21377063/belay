"""上下文管理的三个小工具：输出截断、清理过期的工具结果、交接说明的提示词。

不做原地摘要压缩：上下文过长时由 worker 结束当前上下文，让模型写一份交接说明，
再从任务原文 + 交接说明 + 仓库当前状态重建（见 worker.py 的 _reset_context）。
截断与清理的思路参考 mini_claude（MIT），测试日志的截断改为保留报错块。
"""
from __future__ import annotations

import re

# 测试与构建日志里值得保留的行
_SIGNAL = re.compile(
    r"(FAILED|FAIL\b|ERROR|Error|error:|Exception|Traceback|AssertionError|assert |panicked|"
    r"^E\s|^>\s|\bfailed\b|short test summary|passed|warning:)"
)

CLEARED = "[Old tool result cleared: the file was read or modified again later, or the result is stale. Re-run the tool if you need it.]"


def truncate_output(text: str, max_chars: int = 30000, head: int = 60, tail: int = 120,
                    max_signal: int = 120) -> str:
    """保留开头、结尾，以及中间的报错相关行；其余折叠为“省略 N 行”。"""
    if len(text) <= max_chars:
        return text
    lines = text.split("\n")
    if len(lines) <= head + tail:
        keep = (max_chars - 100) // 2
        return f"{text[:keep]}\n\n[... {len(text) - 2 * keep} characters omitted ...]\n\n{text[-keep:]}"

    middle = lines[head:len(lines) - tail]
    out = lines[:head]
    skipped = 0
    picked = 0
    for line in middle:
        if picked < max_signal and _SIGNAL.search(line):
            if skipped:
                out.append(f"[... {skipped} lines omitted ...]")
                skipped = 0
            out.append(line[:500])
            picked += 1
        else:
            skipped += 1
    if skipped:
        out.append(f"[... {skipped} lines omitted ...]")
    out.extend(lines[len(lines) - tail:])
    result = "\n".join(out)
    if len(result) > max_chars:                       # 单行极长等情况的兜底
        keep = (max_chars - 100) // 2
        result = f"{result[:keep]}\n\n[... {len(result) - 2 * keep} characters omitted ...]\n\n{result[-keep:]}"
    return result


def clear_stale_results(messages: list[dict], keep_recent: int = 12) -> int:
    """清理过期的工具结果，返回清理的条数。

    过期指：同一文件后来又被读取或修改过的 read_file 结果；以及最近 keep_recent 条之前的
    list_files / grep_search / bash 结果。只改写 tool_result 的内容，不删消息，
    因此 tool_use 与 tool_result 的配对始终有效。
    """
    uses: dict[str, dict] = {}
    for m in messages:
        if m["role"] == "assistant" and isinstance(m["content"], list):
            for b in m["content"]:
                if b.get("type") == "tool_use":
                    uses[b["id"]] = b

    results: list[tuple[dict, dict]] = []        # (tool_result 块, 对应的 tool_use)
    for m in messages:
        if m["role"] == "user" and isinstance(m["content"], list):
            for b in m["content"]:
                if b.get("type") == "tool_result" and b.get("tool_use_id") in uses and b.get("content") != CLEARED:
                    results.append((b, uses[b["tool_use_id"]]))

    stale: set[int] = set()
    last_touch: dict[str, int] = {}
    for i, (_, use) in enumerate(results):
        path = (use.get("input") or {}).get("file_path")
        if use["name"] in ("read_file", "edit_file", "write_file") and path:
            if path in last_touch and results[last_touch[path]][1]["name"] == "read_file":
                stale.add(last_touch[path])
            last_touch[path] = i
    cutoff = len(results) - keep_recent
    for i, (_, use) in enumerate(results[:max(0, cutoff)]):
        if use["name"] in ("list_files", "grep_search", "bash"):
            stale.add(i)

    for i in stale:
        results[i][0]["content"] = CLEARED
    return len(stale)


HANDOFF_REQUEST = """Your context window is almost full, so this session will be reset. Write a handoff note for yourself. \
After the reset you will only see the original task, this note, and the current state of the repository.

Include:
1. Changes already made (files and key points).
2. Conclusions you have confirmed, including approaches you ruled out.
3. Open problems and failing tests, with their causes if known.
4. What to do next, in order.

Output only the handoff note. Do not call any tools."""
