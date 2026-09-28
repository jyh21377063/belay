"""需求抽取：把任务原文按条目机械切分为需求节点（纯函数，不调用模型）。

SWE-EVO：release notes 的每个顶层列表项是一条需求；嵌套列表项与续行并入上一条。
小节标题（Markdown 标题、单独一行的粗体、RST 下划线标题、以冒号结尾的短行）决定需求类型，
条目自身的前缀（Feature: / Fix: …）优先。原文里没有列表时（例如 LHTB），整个任务是一条需求。

需求在初始化后冻结（不变量 1）。每条需求的 text 由原文的行照录而成，reviewer 的引文按它校验。
"""
from __future__ import annotations

import re

from belay.graph.model import Requirement

_BULLET = re.compile(r"^(\s*)(?:[-*+•]|\d{1,3}[.)])\s+(.*\S)\s*$")
_MD_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_BOLD_HEADING = re.compile(r"^\s*(?:\*\*|__)(.+?)(?:\*\*|__)\s*:?\s*$")
_RST_UNDERLINE = re.compile(r"^\s*([=\-~^*+#`'\"])\1{2,}\s*$")
_BLOCK = re.compile(r"<release_notes>\s*(.*?)\s*</release_notes>", re.S)

_SKIP_SECTIONS = ("new contributor", "contributors", "full changelog")
_SKIP_ITEMS = re.compile(r"made their first contribution|^full changelog", re.I)

_SECTION_KINDS = [
    (("doc",), "docs"),
    (("maintenance", "internal", "chore", "infrastructure", "build", "packaging", "dependenc", "ci ", "testing"),
     "maintenance"),
    (("feature", "new", "added", "enhancement", "improvement", "addition"), "new"),
    (("fix", "bug", "deprecat", "breaking", "remov", "change"), "change"),
]
_PREFIX_KINDS = [
    (re.compile(r"^(feature|feat|new|add(ed)?)\b", re.I), "new"),
    (re.compile(r"^(fix(ed)?|bugfix|bug)\b", re.I), "change"),
    (re.compile(r"^(docs?)\b", re.I), "docs"),
]


def _section_kind(section: str) -> str | None:
    s = " " + section.lower() + " "
    for words, kind in _SECTION_KINDS:
        if any(w in s for w in words):
            return kind
    return None


def _item_kind(text: str, section: str) -> str:
    for pat, kind in _PREFIX_KINDS:
        if pat.match(text.strip("*_ ")):
            return kind
    return _section_kind(section) or "change"


def _heading(lines: list[str], i: int) -> tuple[str | None, int]:
    """第 i 行是否是标题；返回（标题文字，消耗的行数）。"""
    line = lines[i]
    m = _MD_HEADING.match(line)
    if m:
        return m.group(1).strip(), 1
    if _BULLET.match(line):
        return None, 0
    m = _BOLD_HEADING.match(line)
    if m:
        return m.group(1).strip(), 1
    stripped = line.strip()
    if stripped and i + 1 < len(lines) and _RST_UNDERLINE.match(lines[i + 1]) and len(stripped) < 80:
        return stripped, 2
    if stripped.endswith(":") and len(stripped) <= 60 and not line.startswith((" ", "\t")):
        return stripped[:-1].strip(), 1
    return None, 0


def notes_block(instruction: str) -> str:
    m = _BLOCK.search(instruction)
    return m.group(1) if m else instruction


def extract_requirements(instruction: str) -> list[Requirement]:
    text = notes_block(instruction)
    lines = text.splitlines()
    items: list[dict] = []
    section = ""
    skip_section = False
    base_indent: int | None = None
    current: dict | None = None
    blank = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            blank = True
            i += 1
            continue
        title, used = _heading(lines, i)
        if title is not None:
            section = title
            skip_section = any(s in title.lower() for s in _SKIP_SECTIONS)
            base_indent = None
            current = None
            blank = False
            i += used
            continue
        m = _BULLET.match(line)
        if m:
            indent = len(m.group(1).expandtabs(4))
            if current is not None and base_indent is not None and indent > base_indent:
                current["lines"].append(m.group(2).strip())          # 嵌套列表项并入上一条
            elif not skip_section and not _SKIP_ITEMS.search(m.group(2)):
                base_indent = indent if base_indent is None or current is None else min(base_indent, indent)
                current = {"lines": [m.group(2).strip()], "section": section}
                items.append(current)
            else:
                current = None
            blank = False
            i += 1
            continue
        # 续行：紧跟在列表项之后，或者缩进的段落
        if current is not None and (not blank or line.startswith((" ", "\t"))):
            current["lines"].append(line.strip())
        else:
            current = None
        blank = False
        i += 1

    if not items:
        body = text.strip()
        return [Requirement(id="R1", text=body, section="", kind="change", order=1)] if body else []
    reqs = []
    for n, it in enumerate(items, 1):
        body = "\n".join(it["lines"])
        reqs.append(Requirement(id=f"R{n}", text=body, section=it["section"],
                                kind=_item_kind(it["lines"][0], it["section"]), order=n))
    return reqs


def normalize_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def quote_in(quote: str, text: str, min_len: int = 8) -> bool:
    """引文是否逐字（忽略空白差异与大小写）出现在原文中。"""
    q = normalize_ws(quote.strip().strip('"“”\''))
    return len(q) >= min(min_len, len(normalize_ws(text))) and q in normalize_ws(text)
