"""需求：规则切分、LLM 拆解结果的检查、引文校验（纯函数，不调用模型）。

需求有两条来源（runtime/planner.py 决定用哪条）：
  LLM 拆解   一个模型拆、一个模型审，runtime 只做两项客观检查（check_drafts）：
             每条需求的引文逐字出现在原文里；原文里每一行实质内容都被某条引文覆盖。
             检查不通过只作为反馈交回拆解者重做，不会让运行失败。
  规则切分   extract_requirements：LLM 不可用或拆解失败时的退路。

规则切分：release notes 的每个顶层列表项是一条需求；嵌套列表项与续行并入上一条。
小节标题（Markdown 标题、单独一行的粗体、RST 下划线标题、以冒号结尾的短行）决定需求类型，
条目自身的前缀（Feature: / Fix: …）优先。原文里没有列表时（例如 LHTB），整个任务是一条需求。

需求在初始化后冻结（不变量 1）。每条需求的 text 由原文的行照录而成，reviewer 的引文按它校验。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

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
        return [Requirement(id="R1", text=body, section="", kind="change", order=1, quotes=[body])] if body else []
    reqs = []
    for n, it in enumerate(items, 1):
        body = "\n".join(it["lines"])
        reqs.append(Requirement(id=f"R{n}", text=body, section=it["section"],
                                kind=_item_kind(it["lines"][0], it["section"]), order=n, quotes=list(it["lines"])))
    return reqs


def normalize_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def quote_in(quote: str, text: str, min_len: int = 8) -> bool:
    """引文是否逐字（忽略空白差异与大小写）出现在原文中。"""
    q = normalize_ws(quote.strip().strip('"“”\''))
    return len(q) >= min(min_len, len(normalize_ws(text))) and q in normalize_ws(text)


# ---- LLM 拆解结果的检查 -------------------------------------------------------------

@dataclass
class Draft:
    """拆解模型提出的一条需求：statement 自由表述，quotes 必须是原文片段。"""
    statement: str
    quotes: list[str]
    kind: str = "change"
    section: str = ""


@dataclass
class DraftCheck:
    bad_quotes: list[tuple[int, str]] = field(default_factory=list)   # （草稿序号，从 1 开始；引文）
    no_quote: list[int] = field(default_factory=list)
    bad_kind: list[int] = field(default_factory=list)
    uncovered: list[str] = field(default_factory=list)                # 没被任何引文覆盖的原文行

    @property
    def ok(self) -> bool:
        return not (self.bad_quotes or self.no_quote or self.bad_kind or self.uncovered)

    def feedback(self) -> list[str]:
        out = []
        for i, q in self.bad_quotes:
            out.append(f"Requirement {i}: the quote {q[:160]!r} does not appear verbatim in the task text. Copy "
                       "quotes exactly from the task text.")
        for i in self.no_quote:
            out.append(f"Requirement {i} has no quote from the task text.")
        for i in self.bad_kind:
            out.append(f"Requirement {i}: kind must be one of {', '.join(DRAFT_KINDS)}.")
        for line in self.uncovered[:30]:
            out.append(f"This part of the task text is not covered by any requirement: {line[:200]!r}. Add a "
                       "requirement for it, or quote it in the requirement it belongs to.")
        return out


DRAFT_KINDS = ("change", "new", "docs", "maintenance", "keep")
_WORD = re.compile(r"[A-Za-z\u4e00-\u9fff]{2,}")


def source_lines(instruction: str) -> list[str]:
    """原文中需要被需求覆盖的行：去掉标题、空行、贡献者名单与 changelog 链接，去掉列表符号。"""
    lines = notes_block(instruction).splitlines()
    out, skip, i = [], False, 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        title, used = _heading(lines, i)
        if title is not None:
            skip = any(t in title.lower() for t in _SKIP_SECTIONS)
            i += used
            continue
        m = _BULLET.match(line)
        body = (m.group(2) if m else line).strip()
        if not skip and not _SKIP_ITEMS.search(body) and len(_WORD.findall(body)) >= 3:
            out.append(body)
        i += 1
    return out


def check_drafts(instruction: str, drafts: list[Draft]) -> DraftCheck:
    """两项客观检查：引文逐字存在；原文每一行都被覆盖（见 _covered）。"""
    source = notes_block(instruction)
    chk = DraftCheck()
    good: list[str] = []
    for n, d in enumerate(drafts, 1):
        if d.kind not in DRAFT_KINDS:
            chk.bad_kind.append(n)
        valid = [q for q in d.quotes if q.strip()]
        if not valid:
            chk.no_quote.append(n)
        for q in valid:
            if quote_in(q, source, min_len=1):
                good.append(normalize_ws(q))
            else:
                chk.bad_quotes.append((n, q))
    for line in source_lines(instruction):
        if not _covered(normalize_ws(line), good):
            chk.uncovered.append(line)
    return chk


def _covered(line: str, quotes: list[str], frac: float = 0.5) -> bool:
    """行包含在某条引文里；或者落在这行里的各条引文合起来覆盖了它的一半以上（一行拆成几条需求时）。"""
    if any(line in q for q in quotes):
        return True
    hit = [False] * len(line)
    for q in quotes:
        start = line.find(q) if q else -1
        while start >= 0:
            hit[start:start + len(q)] = [True] * len(q)
            start = line.find(q, start + 1)
    return bool(line) and sum(hit) >= frac * len(line)


def drafts_to_requirements(instruction: str, drafts: list[Draft]) -> list[Requirement]:
    """草稿 → 需求节点。只保留逐字存在的引文；没有有效引文的需求照样保留（reviewer 无法为它批准上报）。"""
    source = notes_block(instruction)
    reqs = []
    for n, d in enumerate(drafts, 1):
        quotes = [q.strip() for q in d.quotes if q.strip() and quote_in(q, source, min_len=1)]
        kind = d.kind if d.kind in DRAFT_KINDS else "change"
        reqs.append(Requirement(id=f"R{n}", text=d.statement.strip() or (quotes[0] if quotes else ""),
                                section=d.section, kind=kind, order=n, quotes=quotes))
    return reqs


def parse_json_object(text: str) -> dict:
    """模型回答里的第一个 JSON 对象（允许前后有文字或 ```json 围栏）。"""
    import json
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise ValueError("no JSON object in the answer")
    return json.loads(m.group(0))


def parse_drafts(text: str) -> list[Draft]:
    data = parse_json_object(text)
    items = data.get("requirements")
    if not isinstance(items, list) or not items:
        raise ValueError("the answer has no non-empty 'requirements' list")
    out = []
    for it in items:
        if not isinstance(it, dict):
            raise ValueError("each requirement must be an object")
        quotes = it.get("quotes", it.get("quote", []))
        quotes = [quotes] if isinstance(quotes, str) else [str(q) for q in (quotes or [])]
        out.append(Draft(statement=str(it.get("statement") or ""), quotes=quotes,
                         kind=str(it.get("kind") or "change").lower(), section=str(it.get("section") or "")))
    return out
