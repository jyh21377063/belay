"""实时查看 agent 进度：把 Claude Code 的 stream-json 日志整理成可读的时间线。

  python -m eval.watch                      # 自动找最近在写的 trial，持续跟踪（Ctrl-C 退出）
  python -m eval.watch pilot-v1             # 指定 run_id（取其中最近更新的 trial）
  python -m eval.watch <trial 目录或 claude-code.txt 路径>
  python -m eval.watch -n 50 --no-follow    # 只看最近 50 条，不跟踪

日志位置：<results_root>/<run_id>/<benchmark>/<id>/<k>/pier/agent/<trial>/agent/claude-code.txt
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from eval.config import DEFAULT_RUNS, load_yaml, resolve_path

LOG_NAME = "claude-code.txt"


def results_root() -> Path:
    runs = load_yaml(DEFAULT_RUNS)
    return resolve_path(DEFAULT_RUNS.parent, runs["results_root"])


def find_log(target: str | None) -> Path:
    if target and Path(target).is_file():
        return Path(target)
    base = Path(target) if target and Path(target).is_dir() else results_root() / (target or "")
    logs = sorted(base.rglob(LOG_NAME), key=lambda p: p.stat().st_mtime)
    if not logs:
        sys.exit(f"在 {base} 下没有找到 {LOG_NAME}（agent 可能还在构建镜像，先看 trial.log）")
    return logs[-1]


def short(s, n=110) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def describe_tool(name: str, inp: dict) -> str:
    for key in ("command", "file_path", "pattern", "path", "url", "description", "prompt"):
        if key in inp:
            extra = f"  (在 {inp['path']})" if key == "pattern" and "path" in inp else ""
            return f"{name}: {short(inp[key])}{extra}"
    return f"{name}: {short(json.dumps(inp, ensure_ascii=False))}"


class State:
    def __init__(self):
        self.turn = 0
        self.tools = 0
        self.errors = 0
        self.inp = self.out = self.cache = 0
        self.edited: set[str] = set()


def render(ev: dict, st: State) -> list[str]:
    t = ev.get("type")
    lines = []
    if t == "system" and ev.get("subtype") == "init":
        lines.append(f"── 会话开始  model={ev.get('model')}  cwd={ev.get('cwd')}")
    elif t == "assistant":
        msg = ev.get("message", {})
        st.turn += 1
        u = msg.get("usage") or {}
        st.inp += u.get("input_tokens", 0)
        st.cache += u.get("cache_read_input_tokens", 0)
        st.out += u.get("output_tokens", 0)
        for c in msg.get("content", []):
            if c.get("type") == "text" and c.get("text", "").strip():
                lines.append(f"[{st.turn:>3}] 💬 {short(c['text'], 160)}")
            elif c.get("type") == "tool_use":
                st.tools += 1
                inp = c.get("input") or {}
                if c.get("name") in ("Edit", "Write", "MultiEdit", "NotebookEdit") and inp.get("file_path"):
                    st.edited.add(inp["file_path"])
                lines.append(f"[{st.turn:>3}] 🔧 {describe_tool(c.get('name', '?'), inp)}")
    elif t == "user":
        for c in (ev.get("message") or {}).get("content", []) or []:
            if isinstance(c, dict) and c.get("type") == "tool_result" and c.get("is_error"):
                st.errors += 1
                body = c.get("content")
                if isinstance(body, list):
                    body = " ".join(x.get("text", "") for x in body if isinstance(x, dict))
                lines.append(f"      ⚠️  {short(body, 150)}")
    elif t == "result":
        lines.append(f"── 结束：{ev.get('subtype')}  轮数={ev.get('num_turns')}  "
                     f"耗时={round((ev.get('duration_ms') or 0) / 60000, 1)}min")
    return lines


def status(st: State, log: Path) -> str:
    age = int(time.time() - log.stat().st_mtime)
    return (f"── 第 {st.turn} 轮 · 工具调用 {st.tools} 次（出错 {st.errors}） · 改过 {len(st.edited)} 个文件 · "
            f"token 输入 {st.inp:,} / 缓存 {st.cache:,} / 输出 {st.out:,} · 日志 {age}s 前更新")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m eval.watch")
    ap.add_argument("target", nargs="?", help="run_id、trial 目录或日志文件；默认最近更新的 trial")
    ap.add_argument("-n", type=int, default=30, help="先显示最近 N 条（默认 30）")
    ap.add_argument("--no-follow", action="store_true")
    a = ap.parse_args(argv)

    log = find_log(a.target)
    print(f"日志：{log}\n")
    st, backlog, pos = State(), [], 0
    with open(log, errors="replace") as f:
        for line in f:
            try:
                backlog += render(json.loads(line), st)
            except json.JSONDecodeError:
                continue
        pos = f.tell()
    print("\n".join(backlog[-a.n:]))
    print(status(st, log))
    if a.no_follow:
        return 0

    last_status = time.time()
    try:
        while True:
            with open(log, errors="replace") as f:
                f.seek(pos)
                chunk = f.read()
                # 只处理完整的行，半行留到下次
                if "\n" in chunk:
                    done, _, _ = chunk.rpartition("\n")
                    pos += len(done.encode()) + 1
                    for line in done.splitlines():
                        try:
                            out = render(json.loads(line), st)
                        except json.JSONDecodeError:
                            continue
                        if out:
                            print("\n".join(out), flush=True)
            if time.time() - last_status > 60:
                print(status(st, log), flush=True)
                last_status = time.time()
            time.sleep(2)
    except KeyboardInterrupt:
        print("\n" + status(st, log))
    return 0


if __name__ == "__main__":
    sys.exit(main())
