"""工具输出的截断：保留开头、结尾和中间的报错行。

所有工具共用（bash 等）。思路参考 mini_claude（MIT），测试日志改为保留报错块。
"""
from __future__ import annotations

import re

# 测试与构建日志里值得保留的行
_SIGNAL = re.compile(
    r"(FAILED|FAIL\b|ERROR|Error|error:|Exception|Traceback|AssertionError|assert |panicked|"
    r"^E\s|^>\s|\bfailed\b|short test summary|passed|warning:)"
)


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
