"""复查者的输入：需求原文 + 按需求筛过的相关改动 + 全部改动文件的列表（纯文本处理，不做 IO）。

在 SWE-EVO 这类题上，需求大多没有可用的测试，提交时由复查者判断“这条需求在改动里有没有做”。整个 diff 往往很大
（几十个文件），直接截断会让排在后面的需求看不到自己的改动。所以先按每条需求原文里的名字（标识符、带点的名字、
反引号里的代码、文件路径、PR 号）给 diff 的每个 hunk 打分，把最相关的 hunk 放在前面；剩下的预算再给完整 diff 的开头。
"""
from __future__ import annotations

import re
from typing import Iterable

_FILE = re.compile(r"^diff --git a/(\S+) b/(\S+)$", re.M)
_CODE = re.compile(r"`([^`]{2,80})`")
_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
_PATH = re.compile(r"[\w.-]+/[\w./-]+|[\w-]+\.(?:py|rs|go|java|js|ts|yml|yaml|toml|cfg|ini|rst|md)\b")
_NUM = re.compile(r"(?<![\w.])\d{3,6}(?!\w|\.\d)")
_STOP = {"self", "None", "True", "False", "return", "import", "from", "def", "class", "the", "and"}


def keywords(text: str) -> list[str]:
    """需求原文里像代码的名字：反引号里的、标识符（下划线 / 驼峰 / 带点）、文件路径、PR 号。"""
    out: list[str] = []
    for m in _CODE.finditer(text):
        out.append(m.group(1).strip("() "))
    for m in _TOKEN.finditer(text):
        t = m.group(0)
        if "." in t or "_" in t.strip("_") or re.search(r"[a-z][A-Z]", t) or \
                (len(t) >= 4 and re.search(r"[A-Za-z]\d|\d[A-Za-z]", t)):
            out.append(t)
    for rx in (_PATH, _NUM):
        out.extend(m.group(0) for m in rx.finditer(text))
    words = []
    for w in out:
        w = w.strip(".,;:()[]{}'\"")
        if len(w) < 3 or w in _STOP or w in words:
            continue
        words.append(w)
        if "." in w and not w.endswith((".py", ".rst", ".md")):          # a.b.c 也按最后一段匹配
            tail = w.rsplit(".", 1)[-1]
            if len(tail) >= 3 and tail not in words and tail not in _STOP:
                words.append(tail)
    return words[:30]


def split_hunks(diff: str) -> list[tuple[str, str]]:
    """diff → [(文件, hunk 文本（带文件头）)]。"""
    out: list[tuple[str, str]] = []
    starts = [m.start() for m in _FILE.finditer(diff)] + [len(diff)]
    for a, b in zip(starts, starts[1:]):
        block = diff[a:b]
        m = _FILE.match(block)
        path = m.group(2) if m else "?"
        head_end = block.find("\n@@")
        if head_end < 0:
            out.append((path, block))
            continue
        head = block[:head_end + 1]
        body = block[head_end + 1:]
        parts = re.split(r"(?m)^(?=@@)", body)
        for p in parts:
            if p.strip():
                out.append((path, head + p))
    return out


def _score(path: str, hunk: str, words: Iterable[str]) -> int:
    low = hunk.lower()
    score = 0
    for w in words:
        wl = w.lower()
        if ("/" in w or w.endswith((".py", ".rs", ".go", ".java", ".js", ".ts", ".yml", ".yaml", ".toml"))) and \
                wl in path.lower():
            score += 3
        else:
            score += min(3, low.count(wl))
    return score


def review_input(reqs, diff: str, files: list, summary: str = "", todos: Iterable[str] = (), notes: str = "",
                 budget: int = 60_000) -> str:
    """reqs：Requirement 列表（有 id、quote）；files：[(路径, 增, 删)]。返回给复查者的整段文字（不超过 budget）。"""
    parts = ["Requirements to review:"]
    for r in reqs:
        parts.append(f"- {r.id}: \"{r.quote}\"")
    if summary.strip():
        parts.append("The agent's submit summary (self-reported):\n" + summary.strip()[:3000])
    todos = [t for t in todos if t.strip()]
    if todos:
        parts.append("The agent's todo items (self-reported):\n" + "\n".join(f"- {t[:200]}" for t in todos[:60]))
    if notes.strip():
        parts.append("The agent's notes from its last handoff (self-reported):\n" + notes.strip()[:2000])
    if files:
        parts.append("All changed files (+added -deleted lines):\n" + "\n".join(
            f"  {p} (+{a} -{d})" for p, a, d in files[:200]) + (f"\n  ... {len(files) - 200} more" if
                                                                len(files) > 200 else ""))
    else:
        parts.append("The changes are empty: nothing outside test paths was changed.")
    head = "\n\n".join(parts)
    left = max(2000, budget - len(head))
    hunks = split_hunks(diff) if diff else []
    per_req = max(1500, int(left * 0.7) // max(1, len(reqs)))
    used: set[int] = set()
    sections = []
    for r in reqs:
        words = keywords(r.quote)
        scored = sorted(((_score(p, h, words), i) for i, (p, h) in enumerate(hunks)), reverse=True)
        picked, size = [], 0
        for sc, i in scored:
            if sc <= 0:
                break
            h = hunks[i][1]
            if size + len(h) > per_req and picked:
                break
            picked.append(i)
            size += len(h)
        if picked:
            body = "".join(hunks[i][1][:per_req] for i in sorted(picked))
            used.update(picked)
            sections.append(f"## Changes that look related to {r.id} (matched names: {', '.join(words[:8])})\n"
                            f"```diff\n{body}\n```")
        else:
            sections.append(f"## {r.id}: no part of the diff mentions the names in its text "
                            f"({', '.join(words[:8]) or 'no code-like names'}); judge from the file list and the "
                            "rest of the diff.")
    text = head + "\n\n" + "\n\n".join(sections)
    rest = "".join(h for i, (_p, h) in enumerate(hunks) if i not in used)
    room = budget - len(text) - 200
    if rest and room > 1000:
        clipped = rest[:room]
        text += "\n\n## The rest of the diff" + (" (truncated)" if len(rest) > room else "") + \
            f"\n```diff\n{clipped}\n```"
    return text[:budget]
