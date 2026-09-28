"""独立上下文的模型调用：Test Author 与 Reviewer。输出都是提议，由 decide() 校验后入图。

Test Author：复用 worker 循环（只读工具），工作目录是原始代码的副本；看得到需求原文、原始代码与请求方声明的
接口，看不到实现者的代码与对话。最终回复里的 python 代码块就是测试文件。
Reviewer：一次不带工具的模型调用；只看需求原文、diff、失败的检查与测试源码，输出 JSON。
"""
from __future__ import annotations

import copy
import json
import re
import time

from belay.env import Env
from belay.runtime.prompts import (REVIEWER_SYSTEM, TEST_AUTHOR_SYSTEM, reviewer_message, test_author_task)
from belay.tools import EXPLORE_TOOLS, Policy, get_tools
from belay.worker import Worker, WorkerConfig
from belay.worker.transcript import Transcript

_CODE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.S)
WRAPUP = ("You have reached the limit for this investigation. Write the complete test file now in a single "
          "```python code block, based on what you found. Do not call any tools.")


def extract_code(text: str) -> str | None:
    blocks = _CODE.findall(text or "")
    blocks = [b for b in blocks if "def test" in b]
    return blocks[-1].strip() + "\n" if blocks else None


def env_at(env: Env, workdir: str) -> Env:
    e = copy.copy(env)
    e.workdir = workdir
    return e


async def write_test(llm, env: Env, *, orig: str, selector: str, req_id: str, req_text: str, section: str,
                     task_context: str, interface: str, feedback: str, max_turns: int, deadline: float | None,
                     transcript: Transcript) -> tuple[str | None, str]:
    """返回（测试文件内容，错误说明）。"""
    platform = (await env.run("uname -sm", timeout=30)).output.strip() or "Linux"
    system = TEST_AUTHOR_SYSTEM.format(orig=orig, platform=platform, selector=selector)
    worker = Worker(llm, env_at(env, orig), tools=get_tools(EXPLORE_TOOLS), role="explore",
                    policy=Policy(git_write="deny", network="deny", disk_search="deny", harness_paths="off"),
                    config=WorkerConfig(max_turns=max_turns, deadline=deadline, reset_tokens=10 ** 12),
                    transcript=transcript, system_prompt=system)
    res = await worker.run(test_author_task(req_id, req_text, section, task_context, interface, feedback))
    text = res.final_text
    if res.status == "max_turns" or (res.status == "no_tool_call" and extract_code(text) is None
                                     and not _not_testable(text)):
        text = await worker.conclude(WRAPUP)
    code = extract_code(text)
    if code is None:
        why = _not_testable(text)
        if why:
            return None, f"the Test Author found nothing testable: {why}"
        return None, f"no test file in the Test Author's answer (status {res.status})"
    return code, ""


def _not_testable(text: str) -> str:
    m = re.search(r"NOT TESTABLE[:\s-]*(.*)", text or "", re.S)
    return (" ".join(m.group(1).split())[:300] or "no reason given") if m else ""


def parse_verdict(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise ValueError("no JSON object in the reviewer's answer")
    data = json.loads(m.group(0))
    decision = str(data.get("decision", "")).lower()
    if decision not in ("approve", "reject"):
        raise ValueError(f"unexpected decision {decision!r}")
    return {"approved": decision == "approve", "quote": str(data.get("quote") or ""),
            "reason": str(data.get("reason") or "")}


async def review(llm, *, kind: str, req_id: str | None, req_text: str, checks: list[str], reason: str, diff: str,
                 failure: str, test_source: str, record=None, retries: int = 1) -> dict:
    msg = reviewer_message(kind, req_id, req_text, checks, reason, diff, failure, test_source)
    last = ""
    messages = [{"role": "user", "content": msg}]
    for _ in range(retries + 1):
        resp = await llm.call(REVIEWER_SYSTEM, [], messages)
        last = resp.text
        if record:
            record({"t": time.time(), "prompt_chars": len(msg), "answer": last})
        try:
            return parse_verdict(last)
        except (ValueError, json.JSONDecodeError) as e:
            messages = messages + [{"role": "assistant", "content": resp.content or [{"type": "text", "text": "."}]},
                                   {"role": "user", "content": f"Your answer could not be parsed ({e}). Reply with "
                                                               "the JSON object only."}]
    raise ValueError(f"unparseable reviewer answer: {last[:300]}")
