"""会话内的打转检测（断路器，参照 OpenHands 的 StuckDetector）：纯函数，只看这个会话最近的工具调用。

只抓逐字的病态循环——环境坏了（命令每次超时、权限被拒）、工具调用格式不对、压缩之后反复读同样的东西——
不判断“思路对不对”。命中只给模型一句提醒（会话层附在工具结果后面），并记一条 stall_detected（action=hint），
不换会话、不改任何状态。

  repeat     同一动作得到同一观察，连续 REPEAT 次（全是出错的由 error 负责，不重复提醒）
  error      同一动作连续 ERROR_REPEAT 次都出错（工具报错，或 bash 退出码非 0 / 超时）
  alternate  两组“动作 + 观察”来回交替（A B A B A B），连续 ALTERNATE 步

动作 = 工具名 + 参数；观察 = 输出去掉耗时、地址、时间戳之后的摘要。每段连续只在长度正好达到阈值时报一次。
模型只说话不调用工具的情况不在这里：会话层已经有“先追问、再当作提交、几次之后结束会话”的处理。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Optional

REPEAT = 4
ERROR_REPEAT = 3
ALTERNATE = 6
KEEP = 2 * ALTERNATE                                # 会话只需保留最近这么多步

_EXIT = re.compile(r"\[exit code (-?\d+)")
_TIMEOUT = re.compile(r"\[Command timed out after \d+s")
_NOISE = [(re.compile(p), r) for p, r in (
    (r"\b\d+(?:\.\d+)?\s*(?:s|ms|sec|seconds)\b", "<t>"),          # 耗时
    (r"0x[0-9a-fA-F]+", "<addr>"),                                  # 地址
    (r"\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d(?:\.\d+)?", "<ts>"),      # 时间戳
)]


@dataclass(frozen=True)
class Step:
    name: str
    action: str                                     # 工具名 + 参数
    obs: str                                        # 输出摘要
    error: bool
    brief: str                                      # 给提醒用的简短描述


@dataclass(frozen=True)
class Loop:
    kind: str                                       # repeat | error | alternate
    n: int
    detail: str
    sig: str


def step(name: str, inp: Optional[dict], output: str, error: bool) -> Step:
    """一次工具调用 → Step。bash 的退出码非 0 或超时也算出错（工具本身不报错）。"""
    inp = inp if isinstance(inp, dict) else {}
    out = str(output or "")
    if name == "bash" and not error:
        tail = out[-400:]
        m = _EXIT.search(tail)
        error = bool((m and int(m.group(1)) != 0) or _TIMEOUT.search(tail))
    text = out
    for pat, rep in _NOISE:
        text = pat.sub(rep, text)
    action = name + ":" + json.dumps(inp, sort_keys=True, ensure_ascii=False, default=str)
    if name == "bash":
        brief = " ".join(str(inp.get("command") or "").split())[:120]
    else:
        brief = ", ".join(f"{k}={str(v)[:60]}" for k, v in sorted(inp.items())[:2])
    return Step(name=name, action=action, obs=hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16],
                error=error, brief=f"{name} {brief}".strip())


def _sig(kind: str, *parts: str) -> str:
    return f"loop:{kind}:" + hashlib.sha1("|".join(parts).encode("utf-8", "replace")).hexdigest()[:12]


def _tail_run(steps: list[Step], same) -> int:
    """最后一步往前，连续满足 same(前一步, 这一步) 的长度。"""
    n = 1
    for i in range(len(steps) - 1, 0, -1):
        if not same(steps[i - 1], steps[i]):
            break
        n += 1
    return n


def check(steps: list[Step]) -> Optional[Loop]:
    """在每一步之后调用：最后一步让某种循环正好达到阈值时返回它，否则 None。"""
    if not steps:
        return None
    last = steps[-1]
    if last.error:
        n = _tail_run(steps, lambda a, b: a.error and b.error and a.action == b.action)
        if n == ERROR_REPEAT:
            return Loop("error", n, last.brief, _sig("error", last.action))
    n = _tail_run(steps, lambda a, b: a.action == b.action and a.obs == b.obs)
    if n == REPEAT and not all(s.error for s in steps[-n:]):
        return Loop("repeat", n, last.brief, _sig("repeat", last.action, last.obs))
    if len(steps) >= ALTERNATE:
        n = 2
        for i in range(len(steps) - 3, -1, -1):
            a, b = steps[i], steps[i + 2]
            if a.action != b.action or a.obs != b.obs:
                break
            n += 1
        x, y = steps[-1], steps[-2]
        if n == ALTERNATE and (x.action, x.obs) != (y.action, y.obs):
            return Loop("alternate", n, f"{y.brief} / {x.brief}",
                        _sig("alternate", *sorted([x.action + x.obs, y.action + y.obs])))
    return None


def reminder(loop: Loop) -> str:
    """给模型的提醒（开头固定，方便统计）。不提时间。"""
    if loop.kind == "error":
        return (f"Loop check: the same call has failed {loop.n} times in a row ({loop.detail[:160]}). Running it again "
                "unchanged will fail the same way: read the error, then change the command or the code, or take a "
                "different route. If the environment itself is the obstacle (a command that hangs, a missing tool), "
                "work around it instead of retrying.")
    if loop.kind == "repeat":
        return (f"Loop check: you have made the same call {loop.n} times in a row and got the same result each time "
                f"({loop.detail[:160]}). Repeating it will not tell you anything new: decide what information you "
                "are missing and get it another way, or move on to the next step.")
    return (f"Loop check: your last {loop.n} calls alternate between the same two actions with the same results "
            f"({loop.detail[:200]}). This is going in circles: step back, decide what you are trying to find out, and "
            "change the approach.")
