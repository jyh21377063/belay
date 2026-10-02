"""集成测试用的 aux 模型替身：按系统提示区分角色（复核者、诊断者），不需要真实模型。

复核者会话：第一轮可以先 run 一条命令（run_first），然后调用 verdict；verdict 的内容由 policy 决定。
policy(opening, review_dir) -> dict（verdict）| str（只回一段文字，不调用工具：复核者没给出结论）。
默认的 oracle 看复核目录里的快照下结论：pkg/mod.py 里有 sub() → R3 完成（E1）；没有、且 worker 声明 R3 受阻 →
认可受阻；没有、且是 submit / 只判定的复核 → R3 没做完（缺 sub()）。总是批准合并（破坏由回归门把关）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, Optional, Union

from belay.llm import Response, Usage
from belay.runtime import prompts as PR

Policy = Callable[[str, Path], Union[dict, str]]


def review_dir_of(opening: str) -> Optional[Path]:
    m = re.search(r"Your working directory is (\S+?):", opening)
    return Path(m.group(1)) if m else None


def oracle(opening: str, review_dir: Path) -> dict:
    judge_only = "## Judge only" in opening
    asked = judge_only or "called submit" in opening
    try:
        mod = (review_dir / "pkg" / "mod.py").read_text()
    except OSError:
        mod = ""
    reqs = []
    if "def sub(" in mod:
        reqs.append({"id": "R3", "status": "done", "level": "E1", "evidence": ["pkg/mod.py defines sub()"]})
    elif "Declared blocked: R3" in opening:
        reqs.append({"id": "R3", "status": "blocked", "level": "E1", "reason": "the task does not define sub well"})
    elif asked:
        reqs.append({"id": "R3", "status": "not_done", "level": "E1", "missing": ["sub() is not defined"]})
    return {"merge": not judge_only, "reason": "nothing is broken", "summary": "change reviewed by the oracle",
            "requirements": reqs, "feedback": "" if not asked or "def sub(" in mod else "define sub(a, b)"}


DIAGNOSIS = {"suspects": [{"file": "pkg/mod.py", "hunk": "@@", "confidence": 0.9, "reason": "mul now adds"}],
             "intentional": {"likely": True, "requirement": "R2", "quote": "not in the task"},
             "suggestion": "restore a * b", "flaky_suspect": False}


class FakeAux:
    def __init__(self, policy: Policy = oracle, run_first: Optional[str] = None, diagnosis: Optional[dict] = None):
        self.policy = policy
        self.run_first = run_first
        self.diagnosis = diagnosis if diagnosis is not None else DIAGNOSIS
        self.calls: list[str] = []
        self.openings: list[str] = []
        self.requests: list[dict] = []
        self.n = 0

    async def call(self, system, tools, messages, tool_choice=None) -> Response:
        self.n += 1
        self.requests.append({"system": system, "messages": json.loads(json.dumps(messages)), "tools": tools})
        usage = Usage(input_tokens=500, output_tokens=50)
        if system == PR.REVIEWER_SYSTEM:
            self.calls.append("review")
            opening = messages[0]["content"]
            if len(messages) == 1:
                self.openings.append(opening)
                if self.run_first:
                    return Response([{"type": "tool_use", "id": f"r{self.n}", "name": "run",
                                      "input": {"command": self.run_first}}], "tool_use", usage)
            out = self.policy(opening, review_dir_of(opening) or Path("/nonexistent"))
            if isinstance(out, str):
                return Response([{"type": "text", "text": out}], "end_turn", usage)
            return Response([{"type": "tool_use", "id": f"v{self.n}", "name": "verdict", "input": out}], "tool_use",
                            usage)
        if system == PR.DIAGNOSE_SYSTEM:
            self.calls.append("diagnose")
            return Response([{"type": "text", "text": json.dumps(self.diagnosis)}], "end_turn", usage)
        self.calls.append("other")
        raise RuntimeError(f"unexpected aux call: {system[:60]!r}")
