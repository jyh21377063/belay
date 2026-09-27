"""文件工具：读、写、编辑、按名查找、按内容搜索。全部经由 Env 在容器内执行。

语义参考 Claude Code 与 mini_claude（MIT）：
  - read_file 带行号，默认最多 2000 行，可用 offset / limit 分段读；
  - 编辑或覆盖已存在的文件之前必须先读过它；
  - edit_file 要求 old_string 唯一匹配（或显式 replace_all），并容忍弯引号差异。
"""
from __future__ import annotations

import posixpath
import re
import shlex

from belay.env import FileMissing
from belay.tools.base import Tool, ToolContext, ToolError

MAX_LINES = 2000
MAX_LINE_CHARS = 2000
MAX_LIST = 200
MAX_GREP_LINES = 200


# ---- read_file ------------------------------------------------------------------

async def read_file(inp: dict, ctx: ToolContext) -> str:
    path = ctx.resolve(inp.get("file_path", ""))
    offset = max(1, int(inp.get("offset") or 1))
    limit = max(1, min(int(inp.get("limit") or MAX_LINES), MAX_LINES))
    try:
        total, text = await ctx.env.read_lines(path, offset, limit)
    except FileMissing:
        raise ToolError(f"File does not exist: {path}")
    if "\x00" in text:
        raise ToolError(f"{path} is a binary file and cannot be read as text.")
    ctx.read_files.add(path)
    if not text:
        return f"({path} is empty)" if total == 0 else f"({path} has {total} lines; nothing after line {offset})"
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    out = []
    for i, line in enumerate(lines):
        if len(line) > MAX_LINE_CHARS:
            line = line[:MAX_LINE_CHARS] + " [...line truncated]"
        out.append(f"{offset + i:6d}\t{line}")
    shown_end = offset + len(lines) - 1
    note = f"\n\n({total} lines total, showing {offset}-{shown_end}; use offset to read more)" if shown_end < total else ""
    return "\n".join(out) + note


# ---- write_file -----------------------------------------------------------------

async def write_file(inp: dict, ctx: ToolContext) -> str:
    path = ctx.resolve(inp.get("file_path", ""))
    content = inp.get("content")
    if content is None:
        raise ToolError("Missing content")
    if path not in ctx.read_files and await ctx.env.exists(path):
        raise ToolError(f"{path} already exists. Read it with read_file before overwriting it; use edit_file for partial changes.")
    await ctx.env.write_text(path, content)
    ctx.read_files.add(path)
    return f"Wrote {path} ({content.count(chr(10)) + 1} lines)"


# ---- edit_file ------------------------------------------------------------------

def _normalize_quotes(s: str) -> str:
    return re.sub("[\u2018\u2019\u2032]", "'", re.sub("[\u201c\u201d\u2033]", '"', s))


def _find_actual(content: str, needle: str) -> str | None:
    """先精确匹配；失败时把弯引号统一成直引号再找，返回文件中实际的那段文本。"""
    if needle in content:
        return needle
    norm_c, norm_n = _normalize_quotes(content), _normalize_quotes(needle)
    idx = norm_c.find(norm_n)
    if idx >= 0 and len(norm_c) == len(content):
        return content[idx:idx + len(needle)]
    return None


def _snippet(content: str, start: int, length: int, context: int = 4) -> str:
    before = content[:start].count("\n")
    lines = content.split("\n")
    first = max(0, before - context)
    last = min(len(lines), before + length + context)
    return "\n".join(f"{i + 1:6d}\t{lines[i]}" for i in range(first, last))


async def edit_file(inp: dict, ctx: ToolContext) -> str:
    path = ctx.resolve(inp.get("file_path", ""))
    old, new = inp.get("old_string"), inp.get("new_string")
    if old is None or new is None:
        raise ToolError("Missing old_string or new_string")
    if old == new:
        raise ToolError("old_string and new_string are identical; nothing to change")
    if old == "":
        raise ToolError("old_string is empty. Use write_file to create a new file")
    if path not in ctx.read_files:
        raise ToolError(f"Read {path} with read_file before editing it")
    try:
        content = await ctx.env.read_text(path)
    except FileMissing:
        raise ToolError(f"File does not exist: {path}")
    actual = _find_actual(content, old)
    if actual is None:
        raise ToolError(f"old_string not found in {path} (it must match exactly, including indentation and whitespace)")
    count = content.count(actual)
    replace_all = bool(inp.get("replace_all"))
    if count > 1 and not replace_all:
        raise ToolError(f"old_string occurs {count} times in {path}. Add surrounding context to make it unique, or set replace_all")
    start = content.find(actual)
    new_content = content.replace(actual, new) if replace_all else content.replace(actual, new, 1)
    await ctx.env.write_text(path, new_content)
    note = " (matched after normalizing quotes)" if actual != old else ""
    head = f"Edited {path}{note}" + (f", replaced {count} occurrences" if replace_all and count > 1 else "")
    return f"{head}\n\nSnippet after the edit:\n{_snippet(new_content, start, new.count(chr(10)) + 1)}"


# ---- list_files -----------------------------------------------------------------

def glob_to_regex(pattern: str) -> re.Pattern:
    """支持 **、*、?、[...]、{a,b}。匹配相对于搜索目录的路径。"""
    i, out = 0, []
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = pattern.find("]", i + 1)
            if j < 0:
                out.append(re.escape(c))
                i += 1
            else:
                body = pattern[i + 1:j]
                out.append("[" + ("^" + body[1:] if body.startswith("!") else body) + "]")
                i = j + 1
        elif c == "{":
            j = pattern.find("}", i + 1)
            if j < 0:
                out.append(re.escape(c))
                i += 1
            else:
                alts = pattern[i + 1:j].split(",")
                out.append("(?:" + "|".join(glob_to_regex(a).pattern[:-2] for a in alts) + ")")
                i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("".join(out) + r"\Z")


async def list_files(inp: dict, ctx: ToolContext) -> str:
    pattern = (inp.get("pattern") or "").strip()
    if not pattern:
        raise ToolError("Missing pattern")
    base = ctx.resolve(inp.get("path") or ctx.workdir)
    # 用模式中第一个通配符之前的目录缩小 find 的范围
    literal = re.split(r"[*?\[{]", pattern, maxsplit=1)[0]
    sub = literal.rsplit("/", 1)[0] if "/" in literal else ""
    root = posixpath.join(base, sub) if sub else base
    cmd = (f"find {shlex.quote(root)} -type f -not -path '*/.git/*' -not -path '*/node_modules/*' "
           f"-not -path '*/__pycache__/*' 2>/dev/null | head -200000")
    res = await ctx.env.run(cmd, timeout=60, cwd=base)
    rx = glob_to_regex(pattern)
    prefix = base.rstrip("/") + "/"
    matches = sorted(p[len(prefix):] for p in res.output.splitlines()
                     if p.startswith(prefix) and rx.match(p[len(prefix):]))
    if not matches:
        return "No files matched"
    extra = len(matches) - MAX_LIST
    body = "\n".join(matches[:MAX_LIST])
    return body + (f"\n... and {extra} more matches not shown; narrow the pattern" if extra > 0 else "")


# ---- grep_search ----------------------------------------------------------------

async def grep_search(inp: dict, ctx: ToolContext) -> str:
    pattern = inp.get("pattern") or ""
    if not pattern:
        raise ToolError("Missing pattern")
    path = ctx.resolve(inp.get("path") or ctx.workdir)
    include = inp.get("include")
    flags = "-i" if inp.get("ignore_case") else ""
    files_only = bool(inp.get("files_only"))
    if not hasattr(ctx.env, "_has_rg"):
        ctx.env._has_rg = (await ctx.env.run("command -v rg >/dev/null", timeout=10)).return_code == 0
    if ctx.env._has_rg:
        cmd = (f"rg --no-heading --color never -n {flags} {'-l' if files_only else ''} "
               f"{'-g ' + shlex.quote(include) if include else ''} -e {shlex.quote(pattern)} {shlex.quote(path)}")
    else:
        cmd = (f"grep -rnIE --color=never {flags} {'-l' if files_only else ''} --exclude-dir=.git "
               f"{'--include=' + shlex.quote(include) if include else ''} -e {shlex.quote(pattern)} {shlex.quote(path)}")
    res = await ctx.env.run(cmd + f" | head -{MAX_GREP_LINES + 1}", timeout=60)
    lines = [ln[:300] for ln in res.output.splitlines() if ln.strip()]
    if not lines:
        return "No matches"
    body = "\n".join(lines[:MAX_GREP_LINES])
    return body + ("\n... too many matches, showing the first 200 lines; narrow the search" if len(lines) > MAX_GREP_LINES else "")


TOOLS = [
    Tool("read_file",
         "Read a file and return its contents with line numbers. Returns up to 2000 lines by default; for large files "
         "use offset (1-based start line) and limit to read in chunks. You must read an existing file before modifying it.",
         {"type": "object", "properties": {
             "file_path": {"type": "string", "description": "Absolute path, or path relative to the repository root"},
             "offset": {"type": "integer", "description": "Line number to start from (1-based)"},
             "limit": {"type": "integer", "description": "Number of lines to read"}},
          "required": ["file_path"]},
         read_file, read_only=True),
    Tool("write_file",
         "Write a whole file: creates it if missing, overwrites it otherwise (read it first before overwriting). "
         "Use edit_file for partial changes.",
         {"type": "object", "properties": {
             "file_path": {"type": "string"}, "content": {"type": "string", "description": "The complete file content"}},
          "required": ["file_path", "content"]},
         write_file),
    Tool("edit_file",
         "Replace old_string with new_string in a file. old_string must match exactly (including indentation) and be "
         "unique in the file; set replace_all to replace every occurrence. Read the file before editing it.",
         {"type": "object", "properties": {
             "file_path": {"type": "string"}, "old_string": {"type": "string"},
             "new_string": {"type": "string"}, "replace_all": {"type": "boolean"}},
          "required": ["file_path", "old_string", "new_string"]},
         edit_file),
    Tool("list_files",
         "Find files by glob pattern (e.g. \"**/*.py\", \"src/**/test_*.py\"). Returns paths relative to path.",
         {"type": "object", "properties": {
             "pattern": {"type": "string"},
             "path": {"type": "string", "description": "Directory to search from; defaults to the repository root"}},
          "required": ["pattern"]},
         list_files, read_only=True),
    Tool("grep_search",
         "Search file contents with a regular expression; returns file:line:content. Use include to filter files "
         "(e.g. \"*.py\") and files_only to return only file names.",
         {"type": "object", "properties": {
             "pattern": {"type": "string"}, "path": {"type": "string"}, "include": {"type": "string"},
             "ignore_case": {"type": "boolean"}, "files_only": {"type": "boolean"}},
          "required": ["pattern"]},
         grep_search, read_only=True),
]
