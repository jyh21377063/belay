"""worker 的上下文管理：清理过期的工具结果、交接说明的提示词。

不做原地摘要压缩：上下文过长时由 worker 结束当前上下文，让模型写一份交接说明，
再从任务原文 + 交接说明 + 仓库当前状态重建（见 worker.py 的 _reset_context）。
清理的思路参考 mini_claude（MIT）。工具输出的截断在 belay/tools/output.py。
"""
from __future__ import annotations

CLEARED = "[Old tool result cleared: the file was read or modified again later, or the result is stale. Re-run the tool if you need it.]"


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


HANDOFF_REQUEST = """The work will now continue in a fresh context. Write a handoff note that lets you resume it \
efficiently there. After the reset you will see only the original task \
statement, this note, your current todo list, and the repository state (`git status` and the full `git diff` of your \
changes). Nothing else from this conversation survives, including the contents of files you read.

Write these sections, concise but complete. Err on the side of including anything that prevents duplicate work or \
repeated mistakes.

1. Task status: which requirements are done and verified, which are partly done, which are not started. Say how \
each "done" item was verified.
2. Code map: the files, functions and tests that matter for the remaining work, with paths and line numbers, and \
what each one does. The diff already shows your own edits; describe the code around them that you will need again.
3. Discoveries: constraints and conventions you found, decisions you made and why, errors you hit and how you fixed \
them, approaches that did not work and why.
4. Verification: the exact commands that run the relevant tests, and their latest results (which tests pass, which \
fail and why, which failures already existed before your changes).
5. Next steps: the specific actions left, in order, including the one you were in the middle of.

Output only the handoff note. Do not call any tools."""
