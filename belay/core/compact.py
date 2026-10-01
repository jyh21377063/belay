"""压缩的规则部分（L0 / L1 / L2）：对消息列表的纯变换，不调模型、不做 IO。

  L0  l0_shrink    单个工具结果过长：保留开头、报错行、结尾和全文路径
  L1  l1_clear     最近 N 个工具结果保留完整，更早的换成一行占位（工具、参数、退出码、全文路径）
  L2  l2_rebuild   旧对话 → build_context 的输出 + 最近一段原文（从 assistant 消息处切，tool_use / tool_result
                   的配对不被拆开）+ 重读的文件
L3（模型摘要）与 L4（交接）需要调模型或结束会话，在 runtime/session.py；它们也复用这里的函数。
"""
from __future__ import annotations

import copy
import json
import math
import re
from typing import Mapping, Optional

_SIGNAL = re.compile(r"(FAILED|FAIL\b|ERROR|Error|error:|Exception|Traceback|AssertionError|assert |panicked|"
                     r"^E\s|^>\s|\bfailed\b|short test summary|warning:)")
_EXIT = re.compile(r"\[exit code (-?\d+)")
CLEARED_PREFIX = "[cleared by the harness"


def estimate_tokens(obj, cpt: float = 4.0) -> int:
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, default=str)
    return int(math.ceil(len(text) / cpt)) if text else 0


def messages_tokens(system: str, messages: list[dict], cpt: float = 4.0) -> int:
    return estimate_tokens(system, cpt) + estimate_tokens(messages, cpt)


# ---------------------------------------------------------------- L0

def l0_shrink(text: str, path: str, max_chars: int, head: int = 40, tail: int = 80, signal: int = 80) -> str:
    if len(text) <= max_chars:
        return text
    lines = text.split("\n")
    note = (f"[harness: this output was {len(text)} characters and {len(lines)} lines; the full text is saved at "
            f"{path} — read it with read_file (offset/limit) or grep_search if you need more]")
    if len(lines) <= head + tail:
        keep = max(1000, (max_chars - len(note)) // 2)
        return f"{text[:keep]}\n[... {len(text) - 2 * keep} characters omitted ...]\n{text[-keep:]}\n{note}"
    out = lines[:head]
    skipped = picked = 0
    for line in lines[head:len(lines) - tail]:
        if picked < signal and _SIGNAL.search(line):
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
    if len(result) > max_chars:
        keep = max(1000, (max_chars - len(note)) // 2)
        result = f"{result[:keep]}\n[... omitted ...]\n{result[-keep:]}"
    return result + "\n" + note


# ---------------------------------------------------------------- L1

def _tool_uses(messages: list[dict]) -> dict[str, dict]:
    uses = {}
    for m in messages:
        if m["role"] == "assistant" and isinstance(m["content"], list):
            for b in m["content"]:
                if b.get("type") == "tool_use":
                    uses[b["id"]] = b
    return uses


def _result_text(block: dict) -> str:
    c = block.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
    return ""


def placeholder(use: dict, text: str, meta: Optional[dict] = None) -> str:
    inp = use.get("input") or {}
    name = use.get("name", "?")
    if name == "bash":
        arg = (inp.get("command") or "")[:160]
    elif name in ("read_file", "write_file", "edit_file"):
        arg = inp.get("file_path", "")
        if name == "read_file" and (inp.get("offset") or inp.get("limit")):
            arg += f" offset={inp.get('offset') or 1} limit={inp.get('limit') or ''}"
    else:
        arg = json.dumps(inp, ensure_ascii=False)[:160]
    m = _EXIT.search(text[-300:])
    code = (meta or {}).get("exit") or (m.group(1) if m else ("error" if text.startswith("Error:") else "0"))
    path = (meta or {}).get("path")
    tail = f"; full output: {path}" if path else "; re-run the tool if you need it"
    return f"{CLEARED_PREFIX}: {name} {arg!s} -> exit {code}, {len(text)} chars{tail}]"


def count_results(messages: list[dict]) -> int:
    n = 0
    for m in messages:
        if m["role"] == "user" and isinstance(m["content"], list):
            n += sum(1 for b in m["content"] if b.get("type") == "tool_result"
                     and not _result_text(b).startswith(CLEARED_PREFIX))
    return n


def l1_clear(messages: list[dict], keep_recent: int, meta: Optional[Mapping[str, dict]] = None,
             keep_reads: bool = False) -> tuple[list[dict], int]:
    """返回（新消息列表，清理的条数）。不修改输入；只改写 tool_result 的内容，不删消息，配对始终有效。

    keep_reads=True 时先只清命令、测试之类的输出，保留 read_file 的结果（它们通常是编辑所依据的内容）。
    """
    meta = meta or {}
    uses = _tool_uses(messages)
    positions = []
    for mi, m in enumerate(messages):
        if m["role"] == "user" and isinstance(m["content"], list):
            for bi, b in enumerate(m["content"]):
                if b.get("type") == "tool_result" and not _result_text(b).startswith(CLEARED_PREFIX):
                    positions.append((mi, bi))
    stale = positions[:max(0, len(positions) - keep_recent)]
    if keep_reads:
        stale = [(mi, bi) for mi, bi in stale
                 if uses.get(messages[mi]["content"][bi].get("tool_use_id"), {}).get("name") != "read_file"]
    if not stale:
        return messages, 0
    out = list(messages)
    touched: dict[int, dict] = {}
    for mi, bi in stale:
        if mi not in touched:
            touched[mi] = copy.deepcopy(messages[mi])
            out[mi] = touched[mi]
        b = touched[mi]["content"][bi]
        use = uses.get(b.get("tool_use_id"), {"name": "?", "input": {}})
        b["content"] = placeholder(use, _result_text(b), meta.get(b.get("tool_use_id")))
    return out, len(stale)


# ---------------------------------------------------------------- L2

def safe_cut(messages: list[dict], keep_tokens: int, cpt: float = 4.0) -> int:
    """从尾部往前数，找一个 assistant 消息作为保留段的起点，使保留段不超过 keep_tokens。

    至少保留最后一对（assistant + 它的工具结果），这样 tool_use / tool_result 的配对始终完整。
    没有 assistant 消息时返回 len(messages)。
    """
    idx = [i for i, m in enumerate(messages) if m["role"] == "assistant" and i > 0]
    if not idx:
        return len(messages)
    best = idx[-1]
    for i in reversed(idx):
        if estimate_tokens(messages[i:], cpt) <= keep_tokens:
            best = i
        else:
            break
    return best


def l2_rebuild(opening: str, messages: list[dict], keep_tokens: int, reread: str = "",
               cpt: float = 4.0) -> list[dict]:
    cut = safe_cut(messages, keep_tokens, cpt)
    tail = copy.deepcopy(messages[cut:])
    first = opening + (f"\n\n## Recently modified files (re-read by the harness)\n{reread}" if reread else "")
    if cut < len(messages):
        first += "\n\n(Your most recent messages follow.)"
    return [{"role": "user", "content": first}] + tail
