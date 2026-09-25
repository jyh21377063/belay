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
        self.edited: set[str] = set()
        self.msg_usage: dict[str, dict] = {}   # 同一条回复会拆成多个事件，按消息 id 去重
        self.final_usage: dict | None = None   # result 事件给出的准确总数
        self.done = False

    def tokens(self) -> tuple[int, int, int]:
        if self.final_usage:
            u = self.final_usage
            return (u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
                    + u.get("cache_creation_input_tokens", 0), u.get("cache_read_input_tokens", 0),
                    u.get("output_tokens", 0))
        inp = cache = out = 0
        for u in self.msg_usage.values():
            cache += u.get("cache_read_input_tokens", 0)
            inp += u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
            out += u.get("output_tokens", 0)
        return inp, cache, out


def render(ev: dict, st: State) -> list[str]:
    t = ev.get("type")
    lines = []
    if t == "system" and ev.get("subtype") == "init":
        lines.append(f"── 会话开始  model={ev.get('model')}  cwd={ev.get('cwd')}")
    elif t == "assistant":
        msg = ev.get("message", {})
        mid = msg.get("id") or f"_{len(st.msg_usage)}"
        if mid not in st.msg_usage:
            st.turn += 1
        st.msg_usage[mid] = msg.get("usage") or st.msg_usage.get(mid) or {}
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
        st.done = True
        if isinstance(ev.get("usage"), dict):
            st.final_usage = ev["usage"]
        lines.append(f"── agent 结束：{ev.get('subtype')}  轮数={ev.get('num_turns')}  "
                     f"耗时={round((ev.get('duration_ms') or 0) / 60000, 1)}min"
                     "  （接下来是评分，然后进入下一道题）")
    return lines


def trial_name(log: Path) -> str:
    """…/<run_id>/<benchmark>/<id>/<k>/pier/agent/<trial>/agent/claude-code.txt → benchmark/id #k"""
    p = log.parts
    try:
        i = p.index("pier")
        return f"{p[i - 3]}/{p[i - 2]} #{p[i - 1]}"
    except (ValueError, IndexError):
        return str(log)


def status(st: State, log: Path) -> str:
    age = int(time.time() - log.stat().st_mtime)
    inp, cache, out = st.tokens()
    tag = "（最终）" if st.final_usage else "（运行中，输出数偏低）"
    state = "已结束" if st.done else f"日志 {age}s 前更新"
    return (f"── {trial_name(log)} · 第 {st.turn} 轮 · 工具调用 {st.tools} 次（出错 {st.errors}） · "
            f"改过 {len(st.edited)} 个文件 · token{tag} 输入 {inp:,} / 其中缓存 {cache:,} / 输出 {out:,} · {state}")


def read_new(log: Path, pos: int, st: State) -> tuple[int, list[str]]:
    out: list[str] = []
    with open(log, errors="replace") as f:
        f.seek(pos)
        chunk = f.read()
    if "\n" not in chunk:
        return pos, out
    done, _, _ = chunk.rpartition("\n")          # 只处理完整的行，半行留到下次
    pos += len(done.encode()) + 1
    for line in done.splitlines():
        try:
            out += render(json.loads(line), st)
        except json.JSONDecodeError:
            continue
    return pos, out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m eval.watch")
    ap.add_argument("target", nargs="?", help="run_id、trial 目录或日志文件；默认最近更新的 trial")
    ap.add_argument("-n", type=int, default=30, help="先显示最近 N 条（默认 30）")
    ap.add_argument("--no-follow", action="store_true")
    a = ap.parse_args(argv)
    fixed_file = bool(a.target and Path(a.target).is_file())

    log = find_log(a.target)
    print(f"日志：{log}\n")
    st = State()
    pos, backlog = read_new(log, 0, st)
    print("\n".join(backlog[-a.n:]))
    print(status(st, log), flush=True)
    if a.no_follow:
        return 0

    last_status = last_scan = time.time()
    try:
        while True:
            pos, out = read_new(log, pos, st)
            if out:
                print("\n".join(out), flush=True)
            now = time.time()
            # 当前 trial 结束或长时间不动时，看看是否有新的 trial 开始了
            if not fixed_file and now - last_scan > 10 and (st.done or now - log.stat().st_mtime > 60):
                last_scan = now
                try:
                    newest = find_log(a.target)
                except SystemExit:
                    newest = log
                if newest != log and newest.stat().st_mtime > log.stat().st_mtime:
                    print(status(st, log))
                    log, st = newest, State()
                    print(f"\n════ 切换到下一道题：{trial_name(log)}\n日志：{log}\n", flush=True)
                    pos, out = read_new(log, 0, st)
                    print("\n".join(out[-a.n:]), flush=True)
            if now - last_status > 60:
                print(status(st, log), flush=True)
                last_status = now
            time.sleep(2)
    except KeyboardInterrupt:
        print("\n" + status(st, log))
    return 0


if __name__ == "__main__":
    sys.exit(main())
