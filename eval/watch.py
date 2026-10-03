"""实时查看 agent 进度：把 agent 日志整理成可读的时间线。支持两种日志：

  claude-code.txt    Claude Code 的 stream-json（A、A-gate、PEE 组）
  transcript.jsonl   自研 worker 的轨迹（B 组），额外显示越界、读后被改、上下文清理与重建、explore 子 agent
  belay/sessions/S<n>.jsonl   Belay 每个会话一份轨迹；一个会话结束后自动切到下一个会话
  belay/reviews/V<n>.jsonl    Belay（v8）复核者每次复核一份轨迹（--review 跟踪最新的一次）
  belay/events.jsonl          Belay 的事件日志：跟踪 Belay 时会穿插显示合并链与复核（⛓ 开头的行）

  python -m eval.watch                      # 自动找最近在写的 trial，持续跟踪（Ctrl-C 退出）
  python -m eval.watch flat-smoke           # 指定 run_id（取其中最近更新的 trial）
  python -m eval.watch <trial 目录或日志文件路径>
  python -m eval.watch -n 50 --no-follow    # 只看最近 50 条，不跟踪
  python -m eval.watch flat-smoke --thinking   # 同时显示思考内容的开头（仅自研 worker）
  python -m eval.watch v8-smoke --chain     # Belay：只看合并链（合并请求、复核结论、合并点、需求判定、交付）
  python -m eval.watch v8-smoke --review    # Belay：跟踪复核者的轨迹（最新一次复核；新复核开始时自动切换）

日志位置：<results_root>/<run_id>/<benchmark>/<id>/<k>/pier/agent/<trial>/agent/{claude-code.txt,transcript.jsonl,
belay/sessions/S<n>.jsonl}。Belay 在 setup 阶段做准备（基线双跑、规划）时还没有会话轨迹，看 belay/setup.json 是否已写出。
探索子 agent 的轨迹在同目录的 transcript-explore-<n>.jsonl，可以直接把文件路径传给本命令查看。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from eval.config import DEFAULT_RUNS, load_yaml, resolve_path

LOG_NAMES = ("claude-code.txt", "transcript.jsonl", "belay/sessions/S*.jsonl")
REVIEW_NAMES = ("belay/reviews/V*.jsonl",)
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "edit_file", "write_file"}


def results_root() -> Path:
    runs = load_yaml(DEFAULT_RUNS)
    return resolve_path(DEFAULT_RUNS.parent, runs["results_root"])


def find_log(target: str | None, names: tuple = LOG_NAMES) -> Path:
    if target and Path(target).is_file():
        return Path(target)
    base = Path(target) if target and Path(target).is_dir() else results_root() / (target or "")
    logs = sorted((p for name in names for p in base.rglob(name)), key=lambda p: p.stat().st_mtime)
    if not logs and names == REVIEW_NAMES:
        sys.exit(f"在 {base} 下还没有复核者轨迹（belay/reviews/V*.jsonl）：后台合并请求有节流（默认 10 分钟一次），"
                 "worker 提交或交接时才会立即复核。先用 --chain 看合并链进度")
    if not logs:
        prep = sorted(base.rglob("belay/events.jsonl"))
        hint = (f"Belay 还没有开始会话：可能还在 setup 阶段做准备（基线双跑、规划），看 {prep[-1].parent}/setup.json "
                "是否已写出、events.jsonl 是否在增长" if prep else "agent 可能还在构建镜像，先看 trial.log")
        sys.exit(f"在 {base} 下没有找到 {' 或 '.join(LOG_NAMES)}（{hint}）")
    return logs[-1]


def short(s, n=110) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def describe_tool(name: str, inp: dict) -> str:
    if name == "verdict":                                  # 复核者的结论
        merge = {True: "建议合并", False: "建议不合并"}.get(inp.get("merge"), "只判定需求")
        reqs = inp.get("requirements") or []
        return f"verdict: {merge}，判定 {len(reqs)} 条需求 — {short(inp.get('summary') or inp.get('reason') or '', 90)}"
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
        self.review = False                    # 复核者的轨迹（belay/reviews/V<n>.jsonl）
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
    elif t == "compact":
        lines.append(f"{when}       ✂️  压缩 L{ev.get('level')}：{ev.get('before', 0):,} → {ev.get('after', 0):,}")
    elif t in ("handoff", "soft_handoff"):
        what = "交接" if t == "handoff" else "到软阈值，等下一个自然停顿点再交接"
        lines.append(f"{when}       🔄 {what}（上下文 {ev.get('context', 0):,}）")
    elif t == "nudge":
        lines.append(f"{when}       ✋ 模型停下没调工具：追问一次")
    elif t == "implicit_submit":
        lines.append(f"{when}       📮 再次停下，当作提交（第 {ev.get('n')} 次）："
                     + ("被接受" if ev.get("accepted") else "交还清单（或改进阶段里接受），会话继续"))
    elif t == "end" and st.review:
        st.done = True
        lines.append(f"── 复核会话结束：{ev.get('status') or ev.get('reason')}  轮数={ev.get('turns')}  "
                     f"耗时={round(st.elapsed / 60, 1)}min  （结论与规则校验见 --chain）")
    elif t == "end" and "reason" in ev:                   # Belay 的会话结束：运行本身由 runtime 决定是否继续
        st.done = True
        lines.append(f"── 会话结束：{ev.get('reason')}  轮数={ev.get('turns')}  耗时={round(st.elapsed / 60, 1)}min"
                     "  （runtime 决定开新会话还是收尾）")
    elif t == "end":
        st.done = True
        lines.append(f"── agent 结束：{ev.get('status')}  轮数={ev.get('turns')}  重建={ev.get('resets')}  "
                     f"耗时={round(st.elapsed / 60, 1)}min  （接下来是评分）")
        if ev.get("summary"):
            lines.append(f"   summary：{short(ev['summary'], 300)}")
    return lines


# ---- Belay 的合并链（events.jsonl）

class Chain:
    """从 events.jsonl 累积的合并链摘要，显示在状态行里。"""

    def __init__(self):
        self.head = 0
        self.merges = 0
        self.requests = 0
        self.rejected = 0
        self.reviews = 0
        self.running: str | None = None
        self.done: dict[str, str] = {}         # 需求 -> 状态（带证据等级）
        self.delivered: str | None = None

    def summary(self) -> str:
        done = sum(1 for v in self.done.values() if v.startswith("done"))
        run = f"，复核 {self.running} 进行中" if self.running else ""
        out = (f"合并链：链头 #{self.head}（{self.merges} 次合并） · 合并请求 {self.requests}（未合并 {self.rejected}） · "
               f"复核 {self.reviews} 次{run} · 已判完成需求 {done}")
        return out + (f" · 已交付 {self.delivered}" if self.delivered else "")


def _items(xs, n=4) -> str:
    xs = list(xs or [])
    return ", ".join(map(str, xs[:n])) + (f" 等 {len(xs)} 个" if len(xs) > n else "")


def render_chain(ev: dict, ch: Chain) -> list[str]:
    """events.jsonl 的一条事件 → 合并链上的可读行（只挑与合并、复核、需求判定、交付有关的事件）。"""
    t, p = ev.get("type"), ev.get("payload") or {}
    pre = time.strftime("%H:%M:%S", time.localtime(ev.get("t") or 0)) + " ⛓ "
    if t == "run_started":
        v = p.get("version")
        warn = "" if v == 8 else "（不是 v8 的日志：没有合并链 / 复核者）"
        return [f"{pre}运行开始 Belay v{v or '?'}{warn}"]
    if t == "baseline_recorded":
        if p.get("available"):
            return [f"{pre}基线完成：回归门可用（全量约 {int(p.get('full_sec') or 0)}s）"]
        return [f"{pre}基线：没有可用的测试，回归门不可用 → 每次合并由复核者读代码、跑命令把关（可给分数）"]
    if t == "requirement_frozen":
        return [f"{pre}需求冻结：{len(p.get('requirements') or [])} 条"]
    if t == "merge_requested":
        ch.requests += 1
        summ = f" — {short(p['summary'], 80)}" if p.get("summary") else ""
        return [f"{pre}📦 合并请求 {p.get('attempt')}（{p.get('trigger')}，快照 s{p.get('snapshot')}，"
                f"基于合并点 #{p.get('base')}）{summ}"]
    if t == "merge_superseded":
        return [f"{pre}   {p.get('attempt')} 被更新的请求取代（{short(p.get('reason', ''), 60)}）"]
    if t == "review_started":
        ch.reviews += 1
        ch.running = p.get("review")
        what = f"合并请求 {p['attempt']}" if p.get("attempt") else f"判定合并点 #{p.get('checkpoint')} 上的需求"
        gate = p.get("gate") or {}
        regs = f"，回归门有 {len(gate['regressions'])} 个回归待复核者看" if gate.get("regressions") else ""
        retry = f"（重试 {p['retry_of']}）" if p.get("retry_of") else ""
        return [f"{pre}🔎 复核 {p.get('review')} 开始{retry}：{what}{regs}；轨迹 reviews/{p.get('review')}.jsonl"]
    if t == "merge_reviewed":
        if ch.running == p.get("review"):
            ch.running = None
        if p.get("failed"):
            return [f"{pre}⚠️  复核 {p.get('review')} 没给出有效结论：{short(p.get('error') or '', 120)}"]
        v = p.get("verdict") or {}
        merge = {True: "建议合并", False: "建议不合并", None: "只判定需求"}.get(v.get("merge"), "?")
        reqs = v.get("requirements") or []
        stat: dict[str, int] = {}
        for r in reqs:
            k = f"{r.get('status')}{'/' + r['level'] if r.get('level') and r.get('status') == 'done' else ''}"
            stat[k] = stat.get(k, 0) + 1
        judged = "，".join(f"{k} {n}" for k, n in stat.items()) or "未判定需求"
        out = [f"{pre}🧑‍⚖️ 复核 {p.get('review')} 结论：{merge}；跑了 {len(p.get('runs') or [])} 条命令；{judged}"
               + (f"；分数 {v['score']:g}" if isinstance(v.get("score"), (int, float)) else "")]
        if v.get("summary"):
            out.append(f"       摘要：{short(v['summary'], 140)}")
        if v.get("merge") is False and v.get("reason"):
            out.append(f"       理由：{short(v['reason'], 140)}")
        return out
    if t == "review_decided":
        notes = p.get("notes") or []
        tail = [f"       注：{short(n, 140)}" for n in notes[:2]]
        if p.get("merge") is True:
            return [f"{pre}   规则校验通过：{p.get('review')} 可以合并"] + tail
        if p.get("merge") is False:
            return [f"{pre}   规则不让合并（{p.get('review')}）：{short('; '.join(p.get('reasons') or []), 160)}"] + tail
        return [f"{pre}   {p.get('review')} 判定已记录"] + tail
    if t == "review_cancelled":
        if ch.running == p.get("review"):
            ch.running = None
        return [f"{pre}   复核 {p.get('review')} 取消：{short(p.get('reason', ''), 100)}"]
    if t == "waiver_granted":
        return [f"{pre}   豁免 {_items(p.get('tests'))}（{p.get('review')}）：{short(p.get('reason', ''), 100)}"]
    if t == "merged":
        cid = int(p.get("checkpoint") or 0)
        if cid == 0:
            return [f"{pre}合并点 #0 = 原始代码"]
        ch.head, ch.merges = cid, ch.merges + 1
        return [f"{pre}🟢 合并点 #{cid} ← {p.get('attempt')}（commit {str(p.get('commit'))[:10]}，"
                f"{len(p.get('files') or [])} 个文件）"]
    if t == "merge_rejected":
        ch.rejected += 1
        regs = f"；回归 {_items(p.get('regressions'))}" if p.get("regressions") else ""
        return [f"{pre}🔴 {p.get('attempt')} 未合并：{p.get('reason')}{regs}"]
    if t == "requirement_judged":
        st, lvl = p.get("status"), p.get("level")
        ch.done[p.get("requirement")] = f"{st}/{lvl}" if lvl else str(st)
        by = {"review": "复核者", "checks": "测试", "self": "worker 自述", "rollback": "回滚"}.get(p.get("by"), p.get("by"))
        miss = f" 缺：{short('; '.join(p.get('missing') or []), 80)}" if st == "open" and p.get("missing") else ""
        return [f"{pre}   需求 {p.get('requirement')} → {st}{'（' + lvl + '）' if lvl else ''}，由{by}{miss}"]
    if t == "submit_requested":
        return [f"{pre}📮 worker 提交 {p.get('submit')}（快照 s{p.get('snapshot')}）"]
    if t == "submit_updated":
        left = f"，还有 {len(p.get('open') or [])} 条需求未完成" if p.get("open") else ""
        return [f"{pre}📮 提交 {p.get('submit')} → {p.get('status')}{left}"]
    if t == "rollback":
        return [f"{pre}↩️  回滚到合并点 #{p.get('to')}"]
    if t == "stall_detected":
        return [f"{pre}⏸  停滞 {p.get('kind')} → {p.get('action')}：{short(p.get('detail', ''), 100)}"]
    if t == "deadline_reserve":
        return [f"{pre}⏰ 剩余时间只够收尾：停止 worker"]
    if t == "finalize_started":
        return [f"{pre}🏁 开始收尾（{p.get('reason')}）"]
    if t == "delivered":
        ch.delivered = f"#{p.get('checkpoint')}（{p.get('status')}）"
        score = f"，分数 {p['score']:g}" if isinstance(p.get("score"), (int, float)) else ""
        return [f"{pre}📦 交付合并点 #{p.get('checkpoint')}：{p.get('status')}{score}"]
    return []


def events_for(log: Path) -> Path | None:
    """Belay 的会话 / 复核轨迹 → 同一次运行的 events.jsonl。"""
    if log.parent.name in ("sessions", "reviews"):
        return log.parent.parent / "events.jsonl"
    if log.name == "events.jsonl":
        return log
    return None


def read_chain(path: Path | None, pos: int, ch: Chain) -> tuple[int, list[str]]:
    if path is None or not path.exists():
        return pos, []
    out: list[str] = []
    with open(path, "rb") as f:
        f.seek(pos)
        chunk = f.read()
    done, nl, _ = chunk.rpartition(b"\n")
    if not nl:
        return pos, out
    pos += len(done) + 1
    for line in done.decode("utf-8", errors="replace").splitlines():
        try:
            out += render_chain(json.loads(line), ch)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    return pos, out


def renderer_for(log: Path):
    belay = log.name.startswith("transcript") or log.parent.name in ("sessions", "reviews")
    return render_belay if belay else render


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


def new_state(log: Path, thinking: bool) -> State:
    st = State(thinking)
    st.review = log.parent.name == "reviews"
    return st


def find_events(target: str | None) -> Path:
    if target and Path(target).is_file():
        return events_for(Path(target)) or Path(target)
    base = Path(target) if target and Path(target).is_dir() else results_root() / (target or "")
    logs = sorted(base.rglob("belay/events.jsonl"), key=lambda p: p.stat().st_mtime)
    if not logs:
        sys.exit(f"在 {base} 下没有找到 belay/events.jsonl（Belay 还没开始，或这不是 Belay 组的运行）")
    return logs[-1]


def watch_chain(a) -> int:
    """--chain：只看合并链。"""
    path = find_events(a.target)
    print(f"事件日志：{path}\n")
    ch = Chain()
    pos, backlog = read_chain(path, 0, ch)
    print("\n".join(backlog[-a.n:]))
    print("── " + ch.summary(), flush=True)
    if a.no_follow:
        return 0
    last_status = last_scan = time.time()
    try:
        while True:
            pos, out = read_chain(path, pos, ch)
            if out:
                print("\n".join(out), flush=True)
            now = time.time()
            if now - last_scan > 10 and not (a.target and Path(a.target).is_file()) and \
                    now - path.stat().st_mtime > 60:
                last_scan = now
                newest = find_events(a.target)
                if newest != path and newest.stat().st_mtime > path.stat().st_mtime:
                    print("── " + ch.summary())
                    path, ch = newest, Chain()
                    print(f"\n════ 切换到下一道题\n事件日志：{path}\n", flush=True)
                    pos, out = read_chain(path, 0, ch)
                    print("\n".join(out[-a.n:]), flush=True)
            if now - last_status > 60:
                print("── " + ch.summary(), flush=True)
                last_status = now
            time.sleep(2)
    except KeyboardInterrupt:
        print("\n── " + ch.summary())
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m eval.watch")
    ap.add_argument("target", nargs="?", help="run_id、trial 目录或日志文件；默认最近更新的 trial")
    ap.add_argument("-n", type=int, default=30, help="先显示最近 N 条（默认 30）")
    ap.add_argument("--no-follow", action="store_true")
    ap.add_argument("--thinking", action="store_true", help="显示思考内容的开头（仅自研 worker 的轨迹）")
    ap.add_argument("--chain", action="store_true", help="Belay：只看合并链（合并请求、复核结论、合并点、需求判定、交付）")
    ap.add_argument("--review", action="store_true", help="Belay：跟踪复核者的轨迹（最新一次复核）")
    a = ap.parse_args(argv)
    if a.chain:
        return watch_chain(a)
    names = REVIEW_NAMES if a.review else LOG_NAMES
    fixed_file = bool(a.target and Path(a.target).is_file())

    log = find_log(a.target, names)
    print(f"日志：{log}\n")
    st = new_state(log, a.thinking)
    ev_path, ch = events_for(log), Chain()
    cpos, chain_backlog = read_chain(ev_path, 0, ch)
    if chain_backlog:
        print(f"合并链（{ev_path}，最近 {min(len(chain_backlog), 12)} 条；完整的用 --chain）：")
        print("\n".join(chain_backlog[-12:]) + "\n")
    pos, backlog = read_new(log, 0, st)
    print("\n".join(backlog[-a.n:]))

    def show_status() -> None:
        print(status(st, log) + (f"\n── {ch.summary()}" if ev_path is not None else ""), flush=True)

    show_status()
    if a.no_follow:
        return 0

    last_status = last_scan = time.time()
    try:
        while True:
            pos, out = read_new(log, pos, st)
            if out:
                print("\n".join(out), flush=True)
            cpos, cout = read_chain(ev_path, cpos, ch)
            if cout:
                print("\n".join(cout), flush=True)
            now = time.time()
            # 当前 trial 结束或长时间不动时，看看是否有新的 trial 开始了（复核轨迹：新的复核开始了）
            idle = now - log.stat().st_mtime > (5 if a.review else 60)
            if not fixed_file and now - last_scan > 10 and (st.done or idle):
                last_scan = now
                try:
                    newest = find_log(a.target, names)
                except SystemExit:
                    newest = log
                if newest != log and newest.stat().st_mtime > log.stat().st_mtime:
                    show_status()
                    same = newest.parent == log.parent and log.parent.name in ("sessions", "reviews")
                    log, st = newest, new_state(newest, a.thinking)
                    what = (f"下一次复核 {log.stem}" if st.review else f"下一个会话 {log.stem}") if same \
                        else f"下一道题：{trial_name(log)}"
                    print(f"\n════ 切换到{what}\n日志：{log}\n", flush=True)
                    if not same:
                        ev_path, ch = events_for(log), Chain()
                        cpos, _ = read_chain(ev_path, 0, ch)
                    pos, out = read_new(log, 0, st)
                    print("\n".join(out[-a.n:]), flush=True)
            if now - last_status > 60:
                show_status()
                last_status = now
            time.sleep(2)
    except KeyboardInterrupt:
        print()
        show_status()
    return 0


if __name__ == "__main__":
    sys.exit(main())
