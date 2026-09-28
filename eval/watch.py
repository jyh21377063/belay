"""实时查看 agent 进度：把 agent 日志整理成可读的时间线。支持两种日志：

  claude-code.txt    Claude Code 的 stream-json（A、A-gate、PEE 组）
  transcript.jsonl   自研 worker 的轨迹（B 组、Belay），额外显示越界、读后被改、上下文清理与重建、explore 子 agent

  python -m eval.watch                      # 自动找最近在写的 trial，持续跟踪（Ctrl-C 退出）
  python -m eval.watch flat-smoke           # 指定 run_id（取其中最近更新的 trial）
  python -m eval.watch <trial 目录或日志文件路径>
  python -m eval.watch -n 50 --no-follow    # 只看最近 50 条，不跟踪
  python -m eval.watch flat-smoke --thinking   # 同时显示思考内容的开头（仅自研 worker）

日志位置：<results_root>/<run_id>/<benchmark>/<id>/<k>/pier/agent/<trial>/agent/{claude-code.txt,transcript.jsonl}
探索子 agent 的轨迹在同目录的 transcript-explore-<n>.jsonl，可以直接把文件路径传给本命令查看。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from eval.config import DEFAULT_RUNS, load_yaml, resolve_path

LOG_NAMES = ("claude-code.txt", "transcript.jsonl")
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "edit_file", "write_file"}


def results_root() -> Path:
    runs = load_yaml(DEFAULT_RUNS)
    return resolve_path(DEFAULT_RUNS.parent, runs["results_root"])


def find_log(target: str | None) -> Path:
    if target and Path(target).is_file():
        return Path(target)
    base = Path(target) if target and Path(target).is_dir() else results_root() / (target or "")
    logs = sorted((p for name in LOG_NAMES for p in base.rglob(name)), key=lambda p: p.stat().st_mtime)
    if not logs:
        sys.exit(f"在 {base} 下没有找到 {' 或 '.join(LOG_NAMES)}（agent 可能还在构建镜像，先看 trial.log）")
    return logs[-1]


def short(s, n=110) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def describe_tool(name: str, inp: dict) -> str:
    if isinstance(inp.get("todos"), list):                 # TodoWrite / todo_write：只显示进度
        todos = inp["todos"]
        done = sum(t.get("status") == "completed" for t in todos)
        doing = [t.get("content") or t.get("activeForm", "") for t in todos if t.get("status") == "in_progress"]
        now = f"，进行中：{short(doing[0], 60)}" if doing else ""
        return f"{name}: {done}/{len(todos)} 完成{now}"
    for key in ("command", "file_path", "pattern", "path", "url", "description", "prompt", "summary"):
        if key in inp:
            extra = f"  (在 {inp['path']})" if key == "pattern" and "path" in inp else ""
            return f"{name}: {short(inp[key])}{extra}"
    return f"{name}: {short(json.dumps(inp, ensure_ascii=False))}"


class State:
    def __init__(self, show_thinking: bool = False):
        self.turn = 0
        self.tools = 0
        self.errors = 0
        self.edited: set[str] = set()
        self.msg_usage: dict[str, dict] = {}   # 同一条回复会拆成多个事件，按消息 id 去重
        self.final_usage: dict | None = None   # result 事件给出的准确总数
        self.done = False
        # 以下仅自研 worker 的轨迹使用
        self.show_thinking = show_thinking
        self.own_usage: list[int] | None = None   # [输入含缓存, 缓存命中, 输出]
        self.context = 0
        self.elapsed = 0.0
        self.resets = 0
        self.explores = 0
        self.violations = 0

    def tokens(self) -> tuple[int, int, int]:
        if self.own_usage is not None:
            return tuple(self.own_usage)
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
                if c.get("name") in EDIT_TOOLS and inp.get("file_path"):
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


def _mmss(sec: float) -> str:
    return f"{int(sec // 60):>3}:{int(sec % 60):02d}"


def render_belay(ev: dict, st: State) -> list[str]:
    """自研 worker 的 transcript.jsonl（事件格式见 belay/worker/transcript.py）。"""
    t = ev.get("type")
    st.elapsed = ev.get("dt", st.elapsed)
    when = _mmss(st.elapsed)
    if st.own_usage is None:
        st.own_usage = [0, 0, 0]
    lines = []
    if t == "start":
        tools = ", ".join(ev.get("tools") or [])
        lines.append(f"── 会话开始  工具：{tools}")
    elif t == "assistant":
        purpose = ev.get("purpose", "turn")
        if purpose == "turn":
            st.turn += 1
        u = ev.get("usage") or {}
        st.own_usage[0] += u.get("input_tokens", 0) + u.get("cache_read_tokens", 0) + u.get("cache_write_tokens", 0)
        st.own_usage[1] += u.get("cache_read_tokens", 0)
        st.own_usage[2] += u.get("output_tokens", 0)
        st.context = ev.get("context", st.context)
        for c in ev.get("content") or []:
            kind = c.get("type")
            if kind == "thinking" and st.show_thinking and c.get("thinking", "").strip():
                lines.append(f"{when} [{st.turn:>3}] 🧠 {short(c['thinking'], 160)}")
            elif kind == "text" and c.get("text", "").strip():
                if purpose == "handoff":                       # 交接说明在 reset 事件里显示
                    continue
                icon = "📝 收尾报告" if purpose == "wrapup" else "💬"
                lines.append(f"{when} [{st.turn:>3}] {icon} {short(c['text'], 160)}")
            elif kind == "tool_use":
                st.tools += 1
                inp = c.get("input") or {}
                if c.get("name") in EDIT_TOOLS and inp.get("file_path"):
                    st.edited.add(inp["file_path"])
                lines.append(f"{when} [{st.turn:>3}] 🔧 {describe_tool(c.get('name', '?'), inp)}")
        if ev.get("stop_reason") == "max_tokens":
            lines.append(f"      ⚠️  回复达到输出上限，被截断")
    elif t == "tool_result":
        for r in ev.get("results") or []:
            if r.get("error"):
                st.errors += 1
                lines.append(f"      ⚠️  {r.get('name')}: {short(r.get('output', ''), 150)}")
            elif r.get("name") == "bash" and "[exit code" in (r.get("output") or "")[-40:]:
                lines.append(f"      ↳ {short((r.get('output') or '').strip().splitlines()[-1], 80)}")
    elif t == "event":
        kind = ev.get("kind")
        src = f"（{ev['source']}）" if ev.get("source") else ""
        if kind == "violation":
            st.violations += 1
            target = ev.get("command") or ev.get("path") or ""
            lines.append(f"      🚫 越界 {ev.get('category')} / {ev.get('action')}{src}: {short(target, 100)}")
        elif kind == "stale_edit":
            lines.append(f"      ♻️  读后被改，需要重新读取：{ev.get('path')}")
        elif kind == "explore_modified_worktree":
            lines.append(f"      ⚠️  探索子 agent 运行期间工作区被改动{src}")
        elif kind == "tool_crash":
            lines.append(f"      💥 工具异常 {ev.get('tool')}: {short(ev.get('error', ''), 120)}")
        else:
            lines.append(f"      · 事件 {kind}{src}")
    elif t == "clear":
        lines.append(f"{when}       ✂️  清理 {ev.get('cleared')} 条过期工具结果（上下文 {ev.get('context', 0):,}）")
    elif t == "reset":
        st.resets += 1
        lines.append(f"{when}       🔄 上下文重建 #{ev.get('resets')}：{short(ev.get('handoff', ''), 140)}")
    elif t == "subagent_start":
        st.explores += 1
        lines.append(f"{when}       🔍 explore#{ev.get('id')} 开始：{short(ev.get('description', ''), 60)} — "
                     f"{short(ev.get('question', ''), 100)}")
    elif t == "subagent_end":
        u = ev.get("usage") or {}
        tokens = u.get("input_tokens", 0) + u.get("cache_read_tokens", 0) + u.get("cache_write_tokens", 0)
        flag = "，工作区被改动" if ev.get("modified") else ""
        lines.append(f"{when}       🔍 explore#{ev.get('id')} 结束：{ev.get('status')}，{ev.get('turns')} 轮，"
                     f"输入 {tokens:,}，报告 {ev.get('report_chars')} 字符{flag}")
    elif t == "end":
        st.done = True
        lines.append(f"── agent 结束：{ev.get('status')}  轮数={ev.get('turns')}  重建={ev.get('resets')}  "
                     f"耗时={round(st.elapsed / 60, 1)}min  （接下来是评分）")
        if ev.get("summary"):
            lines.append(f"   summary：{short(ev['summary'], 300)}")
    return lines


def renderer_for(log: Path):
    return render_belay if log.name.startswith("transcript") else render


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
    state = "已结束" if st.done else f"日志 {age}s 前更新"
    if st.own_usage is not None:                 # 自研 worker：用量逐轮准确
        return (f"── {trial_name(log)} · {round(st.elapsed / 60, 1)}min · 第 {st.turn} 轮 · 工具调用 {st.tools} 次"
                f"（出错 {st.errors}） · 改过 {len(st.edited)} 个文件 · 上下文 {st.context:,} · 重建 {st.resets} · "
                f"explore {st.explores} · 越界 {st.violations} · token 输入 {inp:,} / 其中缓存 {cache:,} / "
                f"输出 {out:,} · {state}")
    tag = "（最终）" if st.final_usage else "（运行中，输出数偏低）"
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
    rend = renderer_for(log)
    for line in done.splitlines():
        try:
            out += rend(json.loads(line), st)
        except json.JSONDecodeError:
            continue
    return pos, out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m eval.watch")
    ap.add_argument("target", nargs="?", help="run_id、trial 目录或日志文件；默认最近更新的 trial")
    ap.add_argument("-n", type=int, default=30, help="先显示最近 N 条（默认 30）")
    ap.add_argument("--no-follow", action="store_true")
    ap.add_argument("--thinking", action="store_true", help="显示思考内容的开头（仅自研 worker 的轨迹）")
    a = ap.parse_args(argv)
    fixed_file = bool(a.target and Path(a.target).is_file())

    log = find_log(a.target)
    print(f"日志：{log}\n")
    st = State(a.thinking)
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
                    log, st = newest, State(a.thinking)
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
